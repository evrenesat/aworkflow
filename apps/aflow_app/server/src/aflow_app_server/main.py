"""FastAPI server for the remote app."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from aflow.control_plane import (
    ControlConflictError,
    ControlIdempotencyConflict,
    RepositoryNotFoundError,
    RestartRequiredControlError,
    RunIdentityError,
    ServiceAuthorizationError,
)
from aflow.control_plane.persistence import PersistenceError
from aflow.daemon import DaemonAuthorizationError, DaemonError, DaemonIdempotencyConflict

import aflow_app_server.planning_routes as planning_routes_module
from .aflow_service import AflowService
from .config import ServerConfig
from .control_plane_service import (
    ControlPlaneService,
    ControlPlaneUnavailableError,
    ProjectNotAllowedError,
)
from .models import (
    CapabilityResponse,
    ContextResponse,
    ControlResponse,
    EventResponse,
    EventTailResponse,
    GlobalCapabilitiesResponse,
    OwnerStopPayload,
    PlanListResponse,
    PlanResponse,
    ProjectListResponse,
    ProjectResponse,
    RunControlPayload,
    RunListResponse,
    RunStatusResponse,
    ReadinessResponse,
    StartResponse,
    StartRunPayload,
    StartRunResponse,
    StartupAnswerPayload,
    StartupQuestionResponse,
)
from .planning import AttachmentStore, PlanningService, ProviderRegistry
from .planning.providers import CodexProvider
from .planning.registry import UnavailablePlanningProvider
from .project_catalog import ProjectCatalog
from .plan_store import PlanStore
from .transcription import TranscriptionClient, TranscriptionError, create_transcription_client


# Global state
_config: ServerConfig | None = None
_project_catalog: ProjectCatalog | None = None
_service: AflowService | None = None
_control_plane_service: ControlPlaneService | None = None
_transcription_client: TranscriptionClient | None = None
_planning_registry: ProviderRegistry | None = None
_planning_service: PlanningService | None = None
_seen_plugin_probe_fingerprints: set[str] = set()

_EVENT_STREAM_POLL_INTERVAL_SECONDS = 0.1


class AccessLogPathFilter(logging.Filter):
    """Suppress probe noise and redact rejected URL credentials in access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = list(getattr(record, "args", ()))
        if len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            if path.partition("?")[0] == "/api/plugin/events":
                return False
            args[2] = _redact_url_credentials(path)
            record.args = tuple(args)
        if len(args) >= 3 and args[2] == "/api/plugin/events":
            return False
        return True


_SECRET_TEXT = re.compile(
    r"(?i)(authorization\s*[:=]\s*bearer\s+|(?:token|secret|password|api[_-]?key)\s*[:=]\s*[\"']?)([^\s\",'&]+)"
)


def _redact_text(value: str) -> str:
    """Keep diagnostic log messages useful without retaining bearer material."""
    return _SECRET_TEXT.sub(r"\1[redacted]", value)


def _redact_url_credentials(value: str) -> str:
    """Remove credential-like query values before an access logger formats them."""
    return re.sub(
        r"(?i)([?&](?:token|access_token|authorization)=)[^&\s]*",
        r"\1[redacted]",
        value,
    )


class SensitiveDataFilter(logging.Filter):
    """Redact accidental token-like text before an application logger emits it."""

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        record.msg = _redact_text(rendered)
        record.args = ()
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
    return _redact_text(preview)


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

    logger = logging.getLogger("aflow_app_server.plugin_probe")
    if not any(isinstance(item, SensitiveDataFilter) for item in logger.filters):
        logger.addFilter(SensitiveDataFilter())
    logger.warning(
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


def get_control_plane_service() -> ControlPlaneService:
    """Return the daemon-backed service after lifespan reconciliation."""
    if _control_plane_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "control_plane_unavailable"},
        )
    return _control_plane_service


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
    config: ServerConfig = Depends(get_config),
) -> str:
    """Verify one header-only bearer token using constant-time comparison."""
    try:
        expected = config.current_auth_token()
    except ValueError:
        expected = ""
    provided_token = credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else ""
    if not expected or not hmac.compare_digest(provided_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "authenticated"


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
    global _config, _project_catalog, _service, _control_plane_service, _transcription_client
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
    _control_plane_service = ControlPlaneService(_config.control_plane_projects)
    _control_plane_service.start()
    _transcription_client = create_transcription_client(
        _config.transcription_url,
        _config.transcription_token,
    )
    attachment_store = AttachmentStore(
        _config.attachment_root,
        max_file_size_bytes=_config.attachment_max_file_size_bytes,
        max_count_per_turn=_config.attachment_max_count_per_turn,
        max_total_size_bytes_per_turn=(
            _config.attachment_max_total_size_bytes_per_turn
        ),
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
                    attachment_store=attachment_store,
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
        attachment_store=attachment_store,
    )
    app.state.planning_service = _planning_service
    app.state.attachment_store = attachment_store

    try:
        yield
    finally:
        await _planning_registry.close()
        # Cleanup
        app.state.planning_service = None
        app.state.attachment_store = None
        _planning_service = None
        _planning_registry = None
        _config = None
        _project_catalog = None
        _service = None
        _control_plane_service = None
        _transcription_client = None


app = FastAPI(
    title="aflow Remote App Server",
    description="Remote management server for aflow workflows",
    version="0.1.0",
    lifespan=lifespan,
    redirect_slashes=False,
)


@app.middleware("http")
async def block_local_plugin_probe(request: Request, call_next):
    """Reject URL credentials before routing and intercept local probe traffic."""
    if any(
        key.casefold() in {"token", "access_token", "authorization"}
        for key in request.query_params
    ):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": {"code": "token_query_rejected"}},
        )
    if request.url.path == "/api/plugin/events":
        body = await request.body()
        if _plugin_probe_logging_enabled():
            _maybe_log_plugin_probe(request, body)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return await call_next(request)

app.dependency_overrides[planning_routes_module._get_project_catalog] = get_project_catalog
app.dependency_overrides[planning_routes_module._get_planning_service] = get_planning_service

app.include_router(planning_routes_module.router, dependencies=[Depends(verify_token)])


def _error_response(status_code: int, code: str, **extra: Any) -> JSONResponse:
    """Return a compact public error envelope with no exception text."""
    return JSONResponse(status_code=status_code, content={"detail": {"code": code, **extra}})


@app.exception_handler(ProjectNotAllowedError)
async def project_not_allowed_handler(_: Request, __: ProjectNotAllowedError) -> JSONResponse:
    return _error_response(status.HTTP_404_NOT_FOUND, "project_not_found")


@app.exception_handler(ControlPlaneUnavailableError)
async def control_plane_unavailable_handler(
    _: Request, __: ControlPlaneUnavailableError
) -> JSONResponse:
    return _error_response(status.HTTP_503_SERVICE_UNAVAILABLE, "control_plane_unavailable")


@app.exception_handler(RepositoryNotFoundError)
async def run_not_found_handler(_: Request, __: RepositoryNotFoundError) -> JSONResponse:
    return _error_response(status.HTTP_404_NOT_FOUND, "run_not_found")


@app.exception_handler(DaemonIdempotencyConflict)
@app.exception_handler(ControlIdempotencyConflict)
async def idempotency_conflict_handler(_: Request, __: Exception) -> JSONResponse:
    return _error_response(status.HTTP_409_CONFLICT, "idempotency_conflict")


@app.exception_handler(ControlConflictError)
async def control_conflict_handler(_: Request, exc: ControlConflictError) -> JSONResponse:
    return _error_response(
        status.HTTP_409_CONFLICT,
        "revision_conflict",
        current_revision=exc.current_revision,
    )


@app.exception_handler(RestartRequiredControlError)
async def restart_required_handler(_: Request, exc: RestartRequiredControlError) -> JSONResponse:
    return _error_response(
        status.HTTP_409_CONFLICT,
        "restart_required",
        fields=list(exc.fields),
    )


@app.exception_handler(DaemonAuthorizationError)
@app.exception_handler(ServiceAuthorizationError)
async def operation_forbidden_handler(_: Request, __: Exception) -> JSONResponse:
    return _error_response(status.HTTP_403_FORBIDDEN, "operation_forbidden")


@app.exception_handler(RunIdentityError)
@app.exception_handler(PersistenceError)
@app.exception_handler(DaemonError)
@app.exception_handler(ValueError)
async def rejected_operation_handler(_: Request, __: Exception) -> JSONResponse:
    return _error_response(status.HTTP_422_UNPROCESSABLE_CONTENT, "operation_rejected")


@app.exception_handler(Exception)
async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger = logging.getLogger("aflow_app_server.errors")
    if not any(isinstance(item, SensitiveDataFilter) for item in logger.filters):
        logger.addFilter(SensitiveDataFilter())
    logger.error("unexpected server error type=%s", type(exc).__name__)
    return _error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error")


def _start_response(result: object) -> StartResponse:
    """Adapt daemon start/answer unions without claiming unit ownership for a question."""
    if hasattr(result, "question_id"):
        return StartResponse(
            startup_question=StartupQuestionResponse.from_canonical(result),
        )
    return StartResponse(result=StartRunResponse.from_canonical(result))


# Daemon-backed control-plane endpoints.  These use one strict allowlist and
# never inspect `.aflow` artifacts or invoke workflow code from the HTTP layer.
@app.get("/ready", response_model=ReadinessResponse, tags=["control-plane"])
def ready(
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
) -> ReadinessResponse:
    return ReadinessResponse(
        ready=service.ready,
        projects=tuple(project.project_id for project in service.projects()),
    )


@app.get(
    "/api/control-plane/capabilities",
    response_model=GlobalCapabilitiesResponse,
    tags=["control-plane"],
)
def global_capabilities(
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
) -> GlobalCapabilitiesResponse:
    return GlobalCapabilitiesResponse(
        projects={
            project.project_id: CapabilityResponse.from_canonical(
                service.capabilities(project.project_id)
            )
            for project in service.projects()
        }
    )


@app.get(
    "/api/control-plane/projects",
    response_model=ProjectListResponse,
    tags=["control-plane"],
)
def control_plane_projects(
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
) -> ProjectListResponse:
    return ProjectListResponse(
        projects=tuple(ProjectResponse.from_canonical(project) for project in service.projects())
    )


@app.get(
    "/api/control-plane/projects/{project_id}/capabilities",
    response_model=CapabilityResponse,
    tags=["control-plane"],
)
def project_capabilities(
    project_id: str,
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
) -> CapabilityResponse:
    return CapabilityResponse.from_canonical(service.capabilities(project_id))


@app.get(
    "/api/control-plane/projects/{project_id}/plans",
    response_model=PlanListResponse,
    tags=["control-plane"],
)
def control_plane_plans(
    project_id: str,
    limit: int = Query(default=100, ge=1, le=1_000),
    cursor: str | None = Query(default=None, max_length=64),
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
) -> PlanListResponse:
    return PlanListResponse(
        plans=tuple(
            PlanResponse.from_canonical(plan)
            for plan in service.list_plans(project_id, limit=limit, cursor=cursor)
        )
    )


@app.get(
    "/api/control-plane/projects/{project_id}/runs",
    response_model=RunListResponse,
    tags=["control-plane"],
)
def control_plane_runs(
    project_id: str,
    limit: int = Query(default=100, ge=1, le=1_000),
    cursor: str | None = Query(default=None, max_length=64),
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
) -> RunListResponse:
    page = service.list_runs(project_id, limit=limit, cursor=cursor)
    return RunListResponse(
        runs=tuple(RunStatusResponse.from_canonical(run) for run in page.runs),
        next_cursor=page.next_cursor,
        schema_version=page.schema_version,
    )


@app.get(
    "/api/control-plane/projects/{project_id}/runs/{run_id}/events",
    response_model=EventTailResponse,
    tags=["control-plane"],
)
def event_tail(
    project_id: str,
    run_id: str,
    after_sequence: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=1_000),
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
) -> EventTailResponse:
    return EventTailResponse(
        events=tuple(
            EventResponse.from_canonical(event)
            for event in service.events(
                project_id, run_id, after_sequence=after_sequence, limit=limit
            )
        )
    )


@app.get(
    "/api/control-plane/projects/{project_id}/runs/{run_id}/events/stream",
    tags=["control-plane"],
)
async def event_stream(
    request: Request,
    project_id: str,
    run_id: str,
    after_sequence: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=1_000),
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
) -> EventSourceResponse:
    initial_events = service.events(
        project_id, run_id, after_sequence=after_sequence, limit=limit
    )

    async def event_generator():
        cursor = after_sequence
        pending_events = initial_events

        while True:
            if pending_events:
                snapshot = EventTailResponse(
                    events=tuple(
                        EventResponse.from_canonical(event) for event in pending_events
                    )
                )
                for event in snapshot.events:
                    cursor = event.sequence
                yield {"event": "events", "data": snapshot.model_dump_json()}
                pending_events = ()
                continue

            if await request.is_disconnected():
                return

            await asyncio.sleep(_EVENT_STREAM_POLL_INTERVAL_SECONDS)

            if await request.is_disconnected():
                return

            pending_events = service.events(
                project_id, run_id, after_sequence=cursor, limit=limit
            )

    return EventSourceResponse(event_generator())


@app.get(
    "/api/control-plane/projects/{project_id}/runs/{run_id}/context",
    response_model=ContextResponse,
    tags=["control-plane"],
)
def run_context(
    project_id: str,
    run_id: str,
    level: str = Query(default="lite", pattern="^(lite|full)$"),
    full_scope: bool = Query(default=False),
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
) -> ContextResponse:
    if level == "full" and not full_scope:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "full_context_scope_required"},
        )
    return ContextResponse.from_canonical(
        service.context(project_id, run_id, level=level, full_scope=full_scope)
    )


@app.get(
    "/api/control-plane/projects/{project_id}/runs/{run_id}",
    response_model=RunStatusResponse,
    tags=["control-plane"],
)
def control_plane_run(
    project_id: str,
    run_id: str,
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
) -> RunStatusResponse:
    return RunStatusResponse.from_canonical(service.run_status(project_id, run_id))


@app.post(
    "/api/control-plane/projects/{project_id}/runs",
    response_model=StartResponse,
    tags=["control-plane"],
)
def start_run(
    project_id: str,
    payload: StartRunPayload,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=256),
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
) -> StartResponse:
    result = service.start_run(
        project_id,
        plan_path=payload.plan_path,
        workflow_name=payload.workflow_name,
        team=payload.team,
        start_step=payload.start_step,
        max_turns=payload.max_turns,
        idempotency_key=idempotency_key,
    )
    adapted = _start_response(result)
    if adapted.startup_question is not None:
        response.status_code = status.HTTP_202_ACCEPTED
    elif adapted.result is not None:
        response.status_code = status.HTTP_201_CREATED if adapted.result.created else status.HTTP_200_OK
    return adapted


@app.post(
    "/api/control-plane/projects/{project_id}/startup-answers/{question_id}",
    response_model=StartResponse,
    tags=["control-plane"],
)
def answer_startup(
    project_id: str,
    question_id: str,
    payload: StartupAnswerPayload,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=256),
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
) -> StartResponse:
    adapted = _start_response(
        service.answer_startup(
            project_id,
            question_id,
            payload.answer,
            idempotency_key=idempotency_key,
        )
    )
    if adapted.startup_question is not None:
        response.status_code = status.HTTP_202_ACCEPTED
    return adapted


@app.patch(
    "/api/control-plane/projects/{project_id}/runs/{run_id}/control",
    response_model=ControlResponse,
    tags=["control-plane"],
)
def control_run(
    project_id: str,
    run_id: str,
    payload: RunControlPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=256),
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
) -> ControlResponse:
    if payload.owner_stop:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "owner_stop_endpoint_required"},
        )
    result, run = service.control(
        project_id, run_id, payload.to_canonical(), idempotency_key=idempotency_key
    )
    return ControlResponse(
        revision=result.revision,
        changed=result.changed,
        owner_stop=result.owner_stop,
        run=RunStatusResponse.from_canonical(run),
    )


@app.post(
    "/api/control-plane/projects/{project_id}/runs/{run_id}/owner-stop",
    response_model=RunStatusResponse,
    tags=["control-plane"],
)
def owner_stop(
    project_id: str,
    run_id: str,
    payload: OwnerStopPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=256),
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
) -> RunStatusResponse:
    return RunStatusResponse.from_canonical(
        service.owner_stop(
            project_id,
            run_id,
            expected_revision=payload.expected_revision,
            idempotency_key=idempotency_key,
        )
    )


@app.post(
    "/api/control-plane/projects/{project_id}/runs/{run_id}/resume",
    response_model=StartRunResponse,
    tags=["control-plane"],
)
def resume_run(
    project_id: str,
    run_id: str,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=256),
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
) -> StartRunResponse:
    result = StartRunResponse.from_canonical(
        service.resume(project_id, run_id, idempotency_key=idempotency_key)
    )
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return result


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
def start_execution(
    request: ExecuteRequest,
    response: Response,
    _: str = Depends(verify_token),
    control_plane: ControlPlaneService = Depends(get_control_plane_service),
) -> StartupResponse:
    """Deprecated compatibility route backed by the daemon control plane."""
    response.headers["Deprecation"] = "true"
    if request.extra_instructions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "prompt_instructions_not_supported"},
        )
    result = control_plane.start_run(
        request.project_id,
        plan_path=request.plan_path,
        workflow_name=request.workflow_name,
        team=request.team,
        start_step=request.start_step,
        max_turns=request.max_turns,
        idempotency_key=None,
    )
    adapted = _start_response(result)
    if adapted.startup_question is not None:
        return StartupResponse(
            prepared=False,
            question=adapted.startup_question.model_dump(mode="json"),
            run_id=adapted.startup_question.run_id,
        )
    assert adapted.result is not None
    return StartupResponse(
        prepared=adapted.result.status == "running",
        run_id=adapted.result.run_id,
    )


@app.get("/api/executions/{run_id}")
def get_execution_status(
    run_id: str,
    response: Response,
    _: str = Depends(verify_token),
    control_plane: ControlPlaneService = Depends(get_control_plane_service),
) -> dict[str, Any]:
    """Deprecated compatibility status lookup over allowlisted daemons."""
    response.headers["Deprecation"] = "true"
    _, run = control_plane.find_run(run_id)
    return RunStatusResponse.from_canonical(run).model_dump(mode="json")


@app.get("/api/executions/{run_id}/events")
async def stream_execution_events(
    run_id: str,
    response: Response,
    _: str = Depends(verify_token),
    control_plane: ControlPlaneService = Depends(get_control_plane_service),
) -> EventSourceResponse:
    """Deprecated header-authenticated SSE alias with one bounded event snapshot."""
    response.headers["Deprecation"] = "true"
    project_id, _ = control_plane.find_run(run_id)
    snapshot = EventTailResponse(
        events=tuple(
            EventResponse.from_canonical(event)
            for event in control_plane.events(
                project_id, run_id, after_sequence=None, limit=100
            )
        )
    )

    async def event_generator():
        yield {"event": "events", "data": snapshot.model_dump_json()}

    return EventSourceResponse(event_generator())


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
