"""FastAPI server for the remote app."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .aflow_service import AflowService
import aflow_app_server.codex_routes as codex_routes_module
from .config import ServerConfig
from .models import ExecutionRequest, ExecutionStatus
from .planning import PlanningService, ProviderRegistry
from .planning.providers import CodexProvider
from .planning.registry import UnavailablePlanningProvider
from .project_catalog import ProjectCatalog
from .plan_store import PlanStore
from .transcription import TranscriptionClient, TranscriptionError, create_transcription_client


# Global state
_config: ServerConfig | None = None
_project_catalog: ProjectCatalog | None = None
_service: AflowService | None = None
_transcription_client: TranscriptionClient | None = None
_planning_registry: ProviderRegistry | None = None
_planning_service: PlanningService | None = None
_seen_plugin_probe_fingerprints: set[str] = set()


class AccessLogPathFilter(logging.Filter):
    """Suppress noisy access logs for known local probe endpoints."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = getattr(record, "args", ())
        if len(args) >= 3 and args[2] == "/api/plugin/events":
            return False
        return True


def _plugin_probe_logging_enabled() -> bool:
    """Enable one-time probe fingerprint logging when debugging local traffic."""
    value = os.environ.get("AFLOW_APP_LOG_PLUGIN_PROBES", "")
    return value.lower() in {"1", "true", "yes", "on"}


def _body_preview(body: bytes, limit: int = 200) -> str:
    """Return a safe body preview for diagnostic logs."""
    if not body:
        return ""
    preview = body[:limit].decode("utf-8", errors="replace")
    if len(body) > limit:
        preview += "..."
    return preview


def _maybe_log_plugin_probe(request: Request, body: bytes) -> None:
    """Log a one-time fingerprint for localhost plugin probe traffic."""
    user_agent = request.headers.get("user-agent", "")
    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")
    content_type = request.headers.get("content-type", "")
    content_length = request.headers.get("content-length", "")
    body_hash = hashlib.sha256(body).hexdigest()[:12]
    fingerprint = "|".join(
        [
            request.method,
            request.url.path,
            user_agent,
            origin,
            referer,
            content_type,
            content_length,
            body_hash,
        ]
    )
    if fingerprint in _seen_plugin_probe_fingerprints:
        return
    _seen_plugin_probe_fingerprints.add(fingerprint)

    logging.getLogger("aflow_app_server.plugin_probe").warning(
        "Blocked localhost probe: method=%s path=%s ua=%r origin=%r referer=%r content_type=%r content_length=%r body_sha256=%s body_preview=%r",
        request.method,
        request.url.path,
        user_agent,
        origin,
        referer,
        content_type,
        content_length,
        body_hash,
        _body_preview(body),
    )


def get_config() -> ServerConfig:
    """Get the server configuration."""
    if _config is None:
        raise RuntimeError("Server not initialized")
    return _config


def get_project_catalog() -> ProjectCatalog:
    """Get the project catalog."""
    if _project_catalog is None:
        raise RuntimeError("Server not initialized")
    return _project_catalog


def get_service() -> AflowService:
    """Get the aflow service."""
    if _service is None:
        raise RuntimeError("Server not initialized")
    return _service


def get_transcription_client() -> TranscriptionClient:
    """Get the transcription client."""
    if _transcription_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Transcription service not configured",
        )
    return _transcription_client


def get_planning_service() -> PlanningService:
    """Get the application-lifespan provider-neutral planning service."""
    if _planning_service is None:
        raise RuntimeError("Server not initialized")
    return _planning_service


security = HTTPBearer(auto_error=False)


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    token: str | None = Query(default=None),
    config: ServerConfig = Depends(get_config),
) -> str:
    """Verify the bearer token."""
    provided_token = credentials.credentials if credentials else token
    if provided_token != config.auth_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
        )
    return provided_token


def get_plan_store_factory(project_catalog: ProjectCatalog = Depends(get_project_catalog)):
    """Factory for creating plan stores."""
    def _get_plan_store(project_id: str) -> PlanStore:
        project = project_catalog.get_project(project_id)
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found",
            )
        return PlanStore(project.current_path)
    return _get_plan_store


def _get_web_dist_dir() -> Path:
    """Resolve the built web app directory."""
    override = os.environ.get("AFLOW_APP_WEB_DIST")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2].parent / "web" / "dist"


def _get_web_file(path: str) -> Path:
    """Resolve a requested frontend file safely within the dist directory."""
    dist_dir = _get_web_dist_dir().resolve()
    candidate = (dist_dir / path).resolve()
    try:
        candidate.relative_to(dist_dir)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found") from exc
    return candidate


def _build_uvicorn_log_config() -> dict[str, Any]:
    """Keep normal access logs but drop noisy local probe traffic."""
    from uvicorn.config import LOGGING_CONFIG

    log_config = copy.deepcopy(LOGGING_CONFIG)
    log_config.setdefault("filters", {})
    log_config["filters"]["suppress_plugin_events"] = {
        "()": "aflow_app_server.main.AccessLogPathFilter",
    }
    access_handler = log_config.setdefault("handlers", {}).setdefault("access", {})
    access_handler["filters"] = [*access_handler.get("filters", []), "suppress_plugin_events"]
    return log_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize server state on startup."""
    global _config, _project_catalog, _service, _transcription_client
    global _planning_registry, _planning_service

    _config = ServerConfig.from_env()
    errors = _config.validate()
    if errors:
        raise RuntimeError(f"Configuration errors: {', '.join(errors)}")

    _project_catalog = ProjectCatalog(
        _config.projects_home,
        _config.project_overrides_path,
        legacy_registry_path=_config.repo_registry_path,
    )
    _service = AflowService()
    _transcription_client = create_transcription_client(
        _config.transcription_url,
        _config.transcription_token,
    )
    providers = []
    for provider in _config.planning_providers:
        if not provider.enabled:
            continue
        if provider.kind == "codex":
            providers.append(
                CodexProvider(
                    provider.id,
                    provider.display_name,
                    server_url=provider.server_url,
                    server_token=provider.server_token,
                    operation_timeout_seconds=_config.planning_operation_timeout_seconds,
                    execution_policy=_config.planning_execution_policy,
                )
            )
        else:
            providers.append(
                UnavailablePlanningProvider(provider.id, provider.display_name)
            )
    _planning_registry = ProviderRegistry(
        providers,
        operation_timeout_seconds=_config.planning_operation_timeout_seconds,
    )
    await _planning_registry.start()
    _planning_service = PlanningService(
        _planning_registry,
        default_provider_id=_config.default_planning_provider_id,
    )
    app.state.planning_service = _planning_service

    try:
        yield
    finally:
        await _planning_registry.close()
        # Cleanup
        app.state.planning_service = None
        _planning_service = None
        _planning_registry = None
        _config = None
        _project_catalog = None
        _service = None
        _transcription_client = None


app = FastAPI(
    title="aflow Remote App Server",
    description="Remote management server for aflow workflows",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def block_local_plugin_probe(request: Request, call_next):
    """Intercept noisy local plugin probe traffic before routing."""
    if request.url.path == "/api/plugin/events":
        body = await request.body()
        if _plugin_probe_logging_enabled():
            _maybe_log_plugin_probe(request, body)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return await call_next(request)

# Override codex_routes dependencies using FastAPI's dependency override system
app.dependency_overrides[codex_routes_module._get_config] = get_config
app.dependency_overrides[codex_routes_module._get_project_catalog] = get_project_catalog
app.dependency_overrides[codex_routes_module._get_planning_service] = get_planning_service

# Include Codex routes with auth
app.include_router(codex_routes_module.router, dependencies=[Depends(verify_token)])


# Request/Response models
class UpdateProjectRequest(BaseModel):
    display_name: str | None = None
    current_path: str | None = None
    alias: str | None = None


class ExecuteRequest(BaseModel):
    project_id: str
    plan_path: str
    workflow_name: str | None = None
    team: str | None = None
    start_step: str | None = None
    max_turns: int | None = None
    extra_instructions: str | None = None


class StartupResponse(BaseModel):
    prepared: bool
    question: dict[str, Any] | None = None
    error: str | None = None
    run_id: str | None = None


# Project endpoints
@app.get("/api/projects")
async def list_projects(
    _: str = Depends(verify_token),
    project_catalog: ProjectCatalog = Depends(get_project_catalog),
) -> list[dict[str, Any]]:
    """List all discovered projects."""
    sessions = ()
    if _planning_service is not None:
        sessions, _ = await _planning_service.list_sessions()
    projects = project_catalog.list_projects(sessions=sessions)
    return [project.to_dict() for project in projects]


@app.get("/api/projects/{project_id}")
async def get_project(
    project_id: str,
    _: str = Depends(verify_token),
    project_catalog: ProjectCatalog = Depends(get_project_catalog),
) -> dict[str, Any]:
    """Get a specific project."""
    sessions = ()
    if _planning_service is not None:
        sessions, _ = await _planning_service.list_sessions()
    project = project_catalog.get_project(project_id, sessions=sessions)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project.to_dict()


@app.patch("/api/projects/{project_id}")
async def update_project(
    project_id: str,
    request: UpdateProjectRequest,
    _: str = Depends(verify_token),
    project_catalog: ProjectCatalog = Depends(get_project_catalog),
) -> dict[str, Any]:
    """Update a project's override metadata."""
    project = project_catalog.update_project(
        project_id,
        display_name=request.display_name,
        current_path=request.current_path,
        alias=request.alias,
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project.to_dict()


# Plan endpoints
@app.get("/api/projects/{project_id}/plans")
async def list_plans(
    project_id: str,
    _: str = Depends(verify_token),
    project_catalog: ProjectCatalog = Depends(get_project_catalog),
    service: AflowService = Depends(get_service),
) -> list[dict[str, Any]]:
    """List all plan files for a project."""
    project = project_catalog.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    plans = service.list_plans(project.current_path)
    return [plan.to_dict() for plan in plans]


# Execution endpoints
@app.post("/api/executions")
async def start_execution(
    request: ExecuteRequest,
    _: str = Depends(verify_token),
    project_catalog: ProjectCatalog = Depends(get_project_catalog),
    service: AflowService = Depends(get_service),
) -> StartupResponse:
    """Start a workflow execution."""
    project = project_catalog.get_project(request.project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    exec_request = ExecutionRequest(
        project_id=request.project_id,
        plan_path=request.plan_path,
        workflow_name=request.workflow_name,
        team=request.team,
        start_step=request.start_step,
        max_turns=request.max_turns,
        extra_instructions=request.extra_instructions,
    )

    result = service.prepare_execution(project.current_path, exec_request)

    if result.error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result.error)

    if result.question:
        return StartupResponse(prepared=False, question=result.question)

    if result.prepared_run:
        run_id = await service.execute_workflow_async(result.prepared_run, request.project_id)
        return StartupResponse(prepared=True, run_id=run_id)

    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Unexpected startup state")


@app.get("/api/executions/{run_id}")
async def get_execution_status(
    run_id: str,
    _: str = Depends(verify_token),
    service: AflowService = Depends(get_service),
) -> dict[str, Any]:
    """Get the status of a workflow execution."""
    exec_status = service.get_run_status(run_id)
    if exec_status is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return exec_status.to_dict()


@app.get("/api/executions/{run_id}/events")
async def stream_execution_events(
    run_id: str,
    _: str = Depends(verify_token),
    service: AflowService = Depends(get_service),
) -> EventSourceResponse:
    """Stream execution events via SSE."""
    queue = service.get_event_queue(run_id)
    if queue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    async def event_generator():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield {
                    "data": json.dumps(_event_to_client_payload(event)),
                }
                if isinstance(event, (RunCompletedEvent, RunFailedEvent)):
                    break
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": "{}"}
            except Exception as e:
                yield {"event": "error", "data": json.dumps({"error": str(e)})}
                break

    return EventSourceResponse(event_generator())


def _event_to_dict(event: Any) -> dict[str, Any]:
    """Convert an execution event to a dictionary."""
    result = {"event_type": event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type)}

    for field_name in dir(event):
        if field_name.startswith("_") or field_name == "event_type":
            continue
        value = getattr(event, field_name, None)
        if value is not None and not callable(value):
            if isinstance(value, Path):
                result[field_name] = str(value)
            elif hasattr(value, "isoformat"):
                result[field_name] = value.isoformat()
            elif hasattr(value, "value"):
                result[field_name] = value.value
            else:
                result[field_name] = value

    return result


def _event_to_client_payload(event: Any) -> dict[str, Any]:
    """Convert an execution event to the frontend's SSE payload shape."""
    raw_type = event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type)
    event_type = "status_update" if raw_type == "status_changed" else raw_type
    timestamp = getattr(event, "timestamp", None)
    if hasattr(timestamp, "isoformat"):
        timestamp_value = timestamp.isoformat()
    else:
        timestamp_value = datetime.now(timezone.utc).isoformat()

    data = _event_to_dict(event)
    data.pop("event_type", None)
    data.pop("timestamp", None)

    return {
        "type": event_type,
        "timestamp": timestamp_value,
        "data": data,
    }


# Transcription endpoints
class TranscriptionResponse(BaseModel):
    text: str


@app.post("/api/transcribe")
async def transcribe_audio(
    file: UploadFile = File(...),
    _: str = Depends(verify_token),
    client: TranscriptionClient = Depends(get_transcription_client),
) -> TranscriptionResponse:
    """Transcribe an uploaded audio file."""
    if not file:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No file provided")

    temp_file = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            content = await file.read()
            temp_file.write(content)
            temp_path = Path(temp_file.name)

        text = await client.transcribe(temp_path)
        return TranscriptionResponse(text=text)

    except TranscriptionError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        if temp_file:
            try:
                Path(temp_file.name).unlink(missing_ok=True)
            except Exception:
                pass


# Health check (no auth required)
@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def serve_web_root() -> FileResponse:
    """Serve the built web app root."""
    index_path = _get_web_file("index.html")
    if not index_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Web app is not built. Run `npm run build` in `apps/aflow_app/web`.",
        )
    return FileResponse(index_path)


@app.get("/{path:path}", include_in_schema=False)
async def serve_web_path(path: str) -> FileResponse:
    """Serve built frontend assets and SPA routes."""
    asset_path = _get_web_file(path)
    if asset_path.is_file():
        return FileResponse(asset_path)

    index_path = _get_web_file("index.html")
    if index_path.exists():
        return FileResponse(index_path)

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Web app is not built. Run `npm run build` in `apps/aflow_app/web`.",
    )


def run_server() -> None:
    """Run the server (entry point for CLI)."""
    import uvicorn

    config = ServerConfig.from_env()
    errors = config.validate()
    if errors:
        print(f"Configuration errors: {', '.join(errors)}")
        raise SystemExit(1)

    uvicorn.run(
        "aflow_app_server.main:app",
        host=config.bind_host,
        port=config.bind_port,
        reload=False,
        log_config=_build_uvicorn_log_config(),
    )
