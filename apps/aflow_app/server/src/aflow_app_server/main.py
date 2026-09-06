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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from starlette.routing import Match, Mount, get_route_path
from starlette.types import Scope

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

from .config import ServerConfig
from .control_plane_service import (
    ControlPlaneServiceConfig,
    ControlPlaneService,
    ControlPlaneUnavailableError,
    ProjectNotAllowedError,
)
from .mcp_adapter import create_control_plane_mcp
from .models import (
    CapabilityResponse,
    ConfigValidationIssueModel,
    ConfigValidationModel,
    ContextResponse,
    ControlResponse,
    EventResponse,
    EventTailResponse,
    GlobalCapabilitiesResponse,
    OwnerStopPayload,
    PlanListResponse,
    PlanResponse,
    ProjectConfigResponse,
    ProjectConfigSavePayload,
    ProjectConfigValidatePayload,
    ProjectDiscoveryResponse,
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
from .plan_service import (
    PlanProjectNotFound,
    PlanRevisionConflict,
    PlanService,
    PlanServiceError,
)
import aflow_app_server.plan_routes as plan_routes_module
from .project_config_service import (
    ConfigValidationReport,
    ProjectConfigError,
    ProjectConfigService,
    ProjectConfigRevisionConflict,
    ProjectConfigRunBlocked,
    ProjectConfigSnapshot,
)
from .project_discovery import ProjectDiscoveryUnavailable, discover_projects
from .project_registry import ProjectRegistry, ProjectRegistryError
from .project_service import (
    ProjectRequest,
    ProjectService,
    ProjectServiceError,
    project_readiness,
)


# Global state
_config: ServerConfig | None = None
_project_registry: ProjectRegistry | None = None
_plan_service: PlanService | None = None
_control_plane_service: ControlPlaneService | None = None
_project_config_service: ProjectConfigService | None = None
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
_MCP_CREDENTIAL_TEXT = re.compile(
    r"(?i)\bbearer[ \t]+[^\s]+|(?:token|access_token|authorization)=[^&\s]+"
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


def _mcp_payload_contains_credential(body: bytes) -> bool:
    """Reject bearer-shaped MCP JSON values before a transport can echo them."""
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return _contains_credential_value(payload)


def _contains_credential_value(value: object) -> bool:
    if isinstance(value, str):
        return _MCP_CREDENTIAL_TEXT.search(value) is not None
    if isinstance(value, dict):
        return any(_contains_credential_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_credential_value(item) for item in value)
    return False


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


def get_project_registry() -> ProjectRegistry:
    """Return the canonical project registry."""
    if _project_registry is None:
        raise RuntimeError("Server not initialized")
    return _project_registry


def get_plan_service() -> PlanService:
    """Return revisioned filesystem plan management."""
    if _plan_service is None:
        raise RuntimeError("Server not initialized")
    return _plan_service


def get_control_plane_service() -> ControlPlaneService:
    """Return the daemon-backed service after lifespan reconciliation."""
    if _control_plane_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "control_plane_unavailable"},
        )
    return _control_plane_service


def get_project_config_service() -> ProjectConfigService:
    """Return the registry-backed config text service."""
    if _project_config_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "control_plane_unavailable"},
        )
    return _project_config_service


security = HTTPBearer(auto_error=False)


def _verify_bearer_token(provided_token: str | None, config: ServerConfig) -> str:
    """Verify a header bearer credential for every authenticated transport."""
    try:
        expected = config.current_auth_token()
    except ValueError:
        expected = ""
    if not expected or not provided_token or not hmac.compare_digest(provided_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "authenticated"


def _bearer_token_from_header(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, token = authorization.strip().partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token:
        return None
    return token


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    config: ServerConfig = Depends(get_config),
) -> str:
    """Verify one header-only bearer token using constant-time comparison."""
    provided_token = (
        credentials.credentials
        if credentials and credentials.scheme.lower() == "bearer"
        else None
    )
    return _verify_bearer_token(provided_token, config)


def get_project_service() -> ProjectService:
    """Return the registry-backed create/register/unregister service."""
    if _project_registry is None or _control_plane_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "control_plane_unavailable"},
        )
    return ProjectService(_project_registry, _control_plane_service)


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


mcp_server = create_control_plane_mcp(get_control_plane_service)
mcp_http_app = mcp_server.http_app(path="/", json_response=True, stateless_http=True)


class _MCPMount(Mount):
    """Make the Streamable HTTP endpoint available at both `/mcp` and `/mcp/`."""

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] in {"http", "websocket"} and get_route_path(scope) == self.path:
            root_path = scope.get("root_path", "")
            return Match.FULL, {
                "path_params": dict(scope.get("path_params", {})),
                "app_root_path": scope.get("app_root_path", root_path),
                "root_path": root_path + self.path,
                "path": "/",
                "endpoint": self.app,
            }
        return super().matches(scope)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize canonical project, plan, config, and run services."""
    global _config, _project_registry, _plan_service, _control_plane_service, _project_config_service

    _config = ServerConfig.from_env()
    errors = _config.validate()
    if errors:
        raise RuntimeError(f"Configuration errors: {', '.join(errors)}")
    project_registry = ProjectRegistry(
        _config.managed_projects_root,
        _config.project_registry_path,
    )
    _project_registry = project_registry
    _plan_service = PlanService(project_registry)
    _control_plane_service = ControlPlaneService(ControlPlaneServiceConfig(
        registry=project_registry,
        aflow_executable=_config.aflow_executable,
        environment_file=_config.environment_file,
        release_identity=_config.release_identity,
        environment=_config.control_plane_environment,
    ))
    _control_plane_service.start()
    _project_config_service = ProjectConfigService(
        project_registry,
        _control_plane_service,
        audit_path=_config.config_audit_path,
    )
    try:
        async with mcp_http_app.lifespan(app):
            yield
    finally:
        _config = None
        _project_registry = None
        _plan_service = None
        _control_plane_service = None
        _project_config_service = None


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
    if request.url.path == "/mcp" or request.url.path.startswith("/mcp/"):
        if _mcp_payload_contains_credential(await request.body()):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"detail": {"code": "token_payload_rejected"}},
            )
        try:
            _verify_bearer_token(
                _bearer_token_from_header(request.headers.get("authorization")),
                get_config(),
            )
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )
    if request.url.path == "/api/plugin/events":
        body = await request.body()
        if _plugin_probe_logging_enabled():
            _maybe_log_plugin_probe(request, body)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return await call_next(request)

app.dependency_overrides[plan_routes_module._get_plan_service] = get_plan_service
app.include_router(plan_routes_module.router, dependencies=[Depends(verify_token)])
app.router.routes.append(_MCPMount("/mcp", app=mcp_http_app, name="mcp"))


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


@app.exception_handler(ProjectConfigRevisionConflict)
async def config_revision_conflict_handler(
    _: Request, exc: ProjectConfigRevisionConflict
) -> JSONResponse:
    return _error_response(
        status.HTTP_409_CONFLICT,
        "revision_conflict",
        current_revision=exc.current_revision,
    )


@app.exception_handler(PlanProjectNotFound)
async def plan_project_not_found_handler(
    _: Request, __: PlanProjectNotFound
) -> JSONResponse:
    return _error_response(status.HTTP_404_NOT_FOUND, "project_not_found")


@app.exception_handler(PlanRevisionConflict)
async def plan_revision_conflict_handler(
    _: Request, exc: PlanRevisionConflict
) -> JSONResponse:
    return _error_response(
        status.HTTP_409_CONFLICT,
        "revision_conflict",
        current_revision=exc.current_revision,
    )


@app.exception_handler(ProjectConfigRunBlocked)
async def config_save_blocked_handler(
    _: Request, exc: ProjectConfigRunBlocked
) -> JSONResponse:
    return _error_response(
        status.HTTP_409_CONFLICT,
        "config_save_blocked",
        blocking_runs=[
            {"run_id": run_id, "status": run_status}
            for run_id, run_status in exc.blocking_runs
        ],
    )


@app.exception_handler(DaemonAuthorizationError)
@app.exception_handler(ServiceAuthorizationError)
async def operation_forbidden_handler(_: Request, __: Exception) -> JSONResponse:
    return _error_response(status.HTTP_403_FORBIDDEN, "operation_forbidden")


@app.exception_handler(RunIdentityError)
@app.exception_handler(PersistenceError)
@app.exception_handler(DaemonError)
@app.exception_handler(ValueError)
@app.exception_handler(ProjectRegistryError)
@app.exception_handler(ProjectServiceError)
@app.exception_handler(ProjectConfigError)
@app.exception_handler(PlanServiceError)
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
        project_errors={
            project_id: error
            for project_id, error in service.readiness().items()
            if error is not None
        },
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
            project_id: CapabilityResponse.from_canonical(capabilities)
            for project_id, capabilities in service.ready_capabilities().items()
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
        extra_instructions=payload.extra_instructions,
        restarted_from_run_id=payload.restarted_from_run_id,
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


class ProjectCreateRequest(BaseModel):
    """Typed create/register contract with no executable, absolute path, or argv fields."""
    mode: str
    path: str
    display_name: str | None = None
    main_branch: str = "main"
    initial_workflow: str | None = None
    initial_team: str | None = None
    initialize_git: bool = False
    initialize_config: bool = False


def _project_payload(registry: ProjectRegistry, project_id: str) -> dict[str, Any] | None:
    record = registry.get(project_id)
    if record is None:
        return None
    try:
        _, root = registry.resolve(project_id)
        is_git_root = True
    except ProjectRegistryError:
        root = registry.declared_root(project_id)
        is_git_root = False
    return {
        "id": record.id,
        "display_name": record.display_name,
        "current_path": str(root),
        "is_git_root": is_git_root,
        "registered_at": record.created_at.isoformat(),
        "readiness": project_readiness(root, is_git_root=is_git_root),
    }


@app.get("/api/projects")
def list_projects(
    _: str = Depends(verify_token),
    registry: ProjectRegistry = Depends(get_project_registry),
) -> list[dict[str, Any]]:
    """List only explicitly registered projects."""
    return [payload for record in registry.list_records() if (payload := _project_payload(registry, record.id))]


@app.get(
    "/api/project-discovery",
    response_model=ProjectDiscoveryResponse,
    tags=["projects"],
)
def get_project_discovery(
    _: str = Depends(verify_token),
    registry: ProjectRegistry = Depends(get_project_registry),
) -> ProjectDiscoveryResponse:
    """List bounded existing Git roots; read-only and never an allowlist."""
    try:
        payload = discover_projects(registry)
    except ProjectDiscoveryUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "discovery_unavailable"},
        )
    return ProjectDiscoveryResponse.model_validate(payload)


@app.post("/api/projects", status_code=status.HTTP_201_CREATED)
def create_project(
    request: ProjectCreateRequest,
    _: str = Depends(verify_token),
    project_service: ProjectService = Depends(get_project_service),
) -> dict[str, Any]:
    if request.mode not in {"create", "register"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"code": "unsupported_project_mode"})
    return project_service.create_or_register(ProjectRequest(
        mode=request.mode,  # type: ignore[arg-type]
        relative_path=request.path,
        display_name=request.display_name,
        main_branch=request.main_branch,
        initial_workflow=request.initial_workflow,
        initial_team=request.initial_team,
        initialize_git=request.initialize_git,
        initialize_config=request.initialize_config,
    ))


@app.delete("/api/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def unregister_project(
    project_id: str,
    _: str = Depends(verify_token),
    project_service: ProjectService = Depends(get_project_service),
) -> Response:
    if not project_service.unregister(project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/projects/{project_id}")
def get_project(
    project_id: str,
    _: str = Depends(verify_token),
    registry: ProjectRegistry = Depends(get_project_registry),
) -> dict[str, Any]:
    project = _project_payload(registry, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


@app.patch("/api/projects/{project_id}")
def update_project(
    project_id: str,
    request: UpdateProjectRequest,
    _: str = Depends(verify_token),
    registry: ProjectRegistry = Depends(get_project_registry),
) -> dict[str, Any]:
    if request.alias is not None:
        raise ProjectRegistryError("project roots and aliases are not mutable")
    if request.current_path is not None:
        submitted = Path(request.current_path).expanduser()
        declared = registry.declared_root(project_id)
        _, resolved = registry.resolve(project_id)
        if not submitted.is_absolute() or submitted not in {declared, resolved}:
            raise ProjectRegistryError("project roots and aliases are not mutable")
    if request.display_name is not None and registry.rename(project_id, request.display_name) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    project = _project_payload(registry, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


# Project configuration endpoints.  Exactly two documents are addressable;
# no route, payload, or response field can name a third file.
def _config_validation_response(report: ConfigValidationReport) -> ConfigValidationModel:
    return ConfigValidationModel(
        state=report.state,  # type: ignore[arg-type]
        issues=tuple(
            ConfigValidationIssueModel(
                document=issue.document, line=issue.line, message=issue.message
            )
            for issue in report.issues
        ),
        placeholders=report.placeholders,
        workflows=report.workflows,
        teams=report.teams,
        roles=report.roles,
    )


def _config_response(snapshot: ProjectConfigSnapshot) -> ProjectConfigResponse:
    return ProjectConfigResponse(
        project_id=snapshot.project_id,
        revision=snapshot.revision,
        documents=snapshot.documents,
        aflow_toml=snapshot.aflow_toml,
        workflows_toml=snapshot.workflows_toml,
        validation=_config_validation_response(snapshot.validation),
    )


@app.get(
    "/api/projects/{project_id}/config",
    response_model=ProjectConfigResponse,
    tags=["projects"],
)
def get_project_config(
    project_id: str,
    _: str = Depends(verify_token),
    service: ProjectConfigService = Depends(get_project_config_service),
) -> ProjectConfigResponse:
    """Return both exact configuration texts with their combined revision."""
    return _config_response(service.read(project_id))


@app.put(
    "/api/projects/{project_id}/config",
    response_model=ProjectConfigResponse,
    tags=["projects"],
)
def save_project_config(
    project_id: str,
    payload: ProjectConfigSavePayload,
    _: str = Depends(verify_token),
    service: ProjectConfigService = Depends(get_project_config_service),
) -> ProjectConfigResponse:
    """Validate and atomically commit both documents as one revisioned pair."""
    return _config_response(
        service.save(
            project_id,
            payload.aflow_toml,
            payload.workflows_toml,
            payload.expected_revision,
            caller_scope="rest",
        )
    )


@app.post(
    "/api/projects/{project_id}/config/validate",
    response_model=ConfigValidationModel,
    tags=["projects"],
)
def validate_project_config(
    project_id: str,
    payload: ProjectConfigValidatePayload,
    _: str = Depends(verify_token),
    service: ProjectConfigService = Depends(get_project_config_service),
) -> ConfigValidationModel:
    """Validate a candidate pair through the production loader without saving."""
    return _config_validation_response(
        service.validate_candidate(project_id, payload.aflow_toml, payload.workflows_toml)
    )


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
    if path == "api" or path.startswith("api/") or path == "mcp" or path.startswith("mcp/"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
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
