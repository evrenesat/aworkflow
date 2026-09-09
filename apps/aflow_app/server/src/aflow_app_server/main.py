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
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.routing import Match, Mount, get_route_path
from starlette.types import Scope

from aflow.config import ConfigError, validate_starter_main_branch
from aflow.control_plane import (
    ControlConflictError,
    ControlIdempotencyConflict,
    RepositoryNotFoundError,
    RestartRequiredControlError,
    RunIdentityError,
    ServiceAuthorizationError,
)
from aflow.control_plane.persistence import PersistenceError
from aflow.control_plane.run_history import DeletedRunError
from aflow.daemon import DaemonAuthorizationError, DaemonError, DaemonIdempotencyConflict, DaemonStartupError

from .browser_session import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    encode_session,
    verify_session,
)
from .config import ServerConfig, global_config_dir
from .control_plane_service import (
    ControlPlaneServiceConfig,
    ControlPlaneService,
    ControlPlaneUnavailableError,
    ProjectNotAllowedError,
)
from .global_config_service import GlobalConfigService
from .models import GlobalConfigPatchPayload, CanonicalTransportModel
from .guided_config import GuidedConfigError, guided_form_response
from .mcp_adapter import create_control_plane_mcp
from .models import (
    BuildStarterAction,
    CapabilityResponse,
    ConfigValidationIssueModel,
    ConfigValidationModel,
    ContextResponse,
    ControlResponse,
    EventResponse,
    EventTailResponse,
    GlobalCapabilitiesResponse,
    GuidedStarterDefaults,
    OwnerStopPayload,
    PlanListResponse,
    PlanResponse,
    ProjectConfigFormPayload,
    ProjectConfigFormResponse,
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
from aflow.skill_store import (
    SkillRefreshIncomplete,
    SkillRevisionConflict,
    SkillStoreError,
    SkillValidationError,
)
from .skill_service import SkillInstallError, SkillNotFound, SkillService
from .models import (
    SkillDetailModel,
    SkillInstallOperationModel,
    SkillInstallPayload,
    SkillInstallResponse,
    SkillLinkStatusModel,
    SkillRefreshModel,
    SkillSavePayload,
    SkillSummaryModel,
    SkillValidateEntryResult,
    SkillValidatePayload,
    SkillValidateResponse,
)


# Global state
_config: ServerConfig | None = None
_configured_config: ServerConfig | None = None
_credential_provider: Any = None
_global_config_service: Any = None
_project_registry: ProjectRegistry | None = None
_plan_service: PlanService | None = None
_control_plane_service: ControlPlaneService | None = None
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
    """Get the server configuration, honoring live global credential rotation.

    When the app is served by ``aflow ui``, configuration comes from the
    global ``config.toml`` and is re-read whenever the file changes so a
    password rotation invalidates sessions immediately. The legacy
    deployment entry point keeps its startup-time environment config.
    """
    if _credential_provider is not None:
        return _credential_provider.config()
    if _config is None:
        raise RuntimeError("Server not initialized")
    return _config


def configure_server(config: ServerConfig, *, config_dir: Path | None = None) -> None:
    """Inject the resolved configuration used by ``aflow ui``.

    Must be called before the app lifespan runs. The lifespan uses this
    configuration instead of the environment-driven legacy path, and a
    global credential provider keeps authentication rotation live.
    """
    global _configured_config, _credential_provider
    from .config import GlobalCredentialProvider, global_config_dir

    _configured_config = config
    _credential_provider = GlobalCredentialProvider(config_dir=config_dir or global_config_dir())


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


def get_global_config_service() -> "GlobalConfigService":
    """Return the shared global workflow pair service."""
    if _global_config_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "control_plane_unavailable"},
        )
    return _global_config_service


security = HTTPBearer(auto_error=False)


def _verify_bearer_token(provided_token: str | None, config: ServerConfig) -> str:
    """Verify a header bearer credential and return the matched token value.

    Returning the exact value that passed the constant-time comparison lets
    callers that mint derived credentials (the browser session) use the same
    token epoch that authenticated the request, instead of rereading the
    possibly rotated deployment token.
    """
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
    return expected


def _bearer_token_from_header(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, token = authorization.strip().partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token:
        return None
    return token


async def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    config: ServerConfig = Depends(get_config),
) -> str:
    """Authenticate a request by explicit header bearer or browser session.

    A supplied Authorization header must be a valid bearer credential; an
    invalid or malformed header is rejected rather than falling back to the
    cookie. A session cookie is accepted only without an Authorization header,
    and cookie-authenticated unsafe methods additionally require an exact
    same-origin Origin header. MCP transports keep header-only authentication.
    """
    authorization = request.headers.get("authorization")
    if authorization is not None:
        return _verify_bearer_token(_bearer_token_from_header(authorization), config)
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized"},
        )
    try:
        auth_token = config.current_auth_token()
    except ValueError:
        auth_token = ""
    try:
        if not auth_token:
            raise ValueError("authentication token unavailable")
        verify_session(cookie, auth_token)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized"},
        )
    if request.method in _UNSAFE_METHODS and not _same_origin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "origin_required"},
        )
    return "authenticated"


_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _same_origin(request: Request) -> bool:
    """Require an Origin header exactly matching the request's own origin."""
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return False
    parsed = urlsplit(origin)
    if not parsed.scheme or not parsed.hostname:
        return False
    url = request.url
    default_ports = {"http": 80, "https": 443}
    origin_port = parsed.port or default_ports.get(parsed.scheme)
    request_port = url.port or default_ports.get(url.scheme)
    return (
        parsed.scheme == url.scheme
        and parsed.hostname == url.hostname
        and origin_port == request_port
    )


def _set_session_cookie(response: Response, value: str, max_age: int, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        value,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )


def _cookie_secure_for_request(request: Request) -> bool:
    """Secure cookies only on HTTPS requests, per the effective request scheme.

    Direct HTTP access (LAN/Tailscale) needs a non-Secure cookie; client-
    supplied forwarded headers are deliberately not trusted.
    """
    return request.url.scheme == "https"


def get_project_service() -> ProjectService:
    """Return the registry-backed create/register/unregister service."""
    if _project_registry is None or _control_plane_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "control_plane_unavailable"},
        )
    return ProjectService(_project_registry, _control_plane_service)


def _get_web_dist_dir() -> Path:
    """Resolve the built web app directory.

    An explicit ``AFLOW_APP_WEB_DIST`` override wins (legacy deployments).
    Otherwise the assets bundled in the installed ``aflow`` package at
    ``aflow/ui_web/`` are authoritative; the checkout's ``web/dist`` remains
    a development fallback.
    """
    override = os.environ.get("AFLOW_APP_WEB_DIST")
    if override:
        return Path(override).expanduser()
    from aflow.ui_assets import published_assets_dir

    bundled = published_assets_dir()
    if bundled is not None:
        return bundled
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
    global _config, _project_registry, _plan_service, _control_plane_service
    global _global_config_service

    if _configured_config is not None:
        _config = _configured_config
    else:
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
    service_config = ControlPlaneServiceConfig(
        registry=project_registry,
        aflow_executable=_config.aflow_executable,
        environment_file=_config.environment_file,
        release_identity=_config.release_identity,
        environment=_config.control_plane_environment,
    )
    if _configured_config is not None:
        # The aflow ui launcher path uses portable persistent units so
        # workflow subprocesses outlive this server on Linux and macOS.
        from dataclasses import replace

        from aflow.control_plane.persistent_units import PersistentUnitManager

        service_config = replace(
            service_config,
            unit_manager_factory=lambda: PersistentUnitManager(
                executable=_config.aflow_executable,
                projects_root=_config.managed_projects_root,
            ),
        )
    _control_plane_service = ControlPlaneService(service_config)
    _control_plane_service.start()
    from .config import global_config_dir

    _global_config_service = GlobalConfigService(
        config_dir=global_config_dir(),
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
        _global_config_service = None


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

@app.middleware("http")
async def renew_browser_session(request: Request, call_next):
    """Roll a valid session cookie forward on marked user activity.

    Only a cookie-authenticated response that explicitly carries
    X-AFlow-Activity: 1 is renewed, so background polling and SSE traffic
    never extend an unattended session. Failed authentication, logout, and
    MCP transports are never renewed.
    """
    response = await call_next(request)
    path = request.url.path
    if (
        request.headers.get("x-aflow-activity") == "1"
        and not (path == "/api/session" and request.method in {"POST", "DELETE"})
        and not (path == "/mcp" or path.startswith("/mcp/"))
        and response.status_code < status.HTTP_400_BAD_REQUEST
    ):
        cookie = request.cookies.get(SESSION_COOKIE_NAME)
        config = get_config()
        if cookie and config is not None:
            try:
                auth_token = config.current_auth_token()
            except ValueError:
                auth_token = ""
            if auth_token:
                try:
                    verify_session(cookie, auth_token)
                except ValueError:
                    return response
                _set_session_cookie(
                    response,
                    encode_session(auth_token),
                    SESSION_MAX_AGE_SECONDS,
                    secure=_cookie_secure_for_request(request),
                )
    return response


app.dependency_overrides[plan_routes_module._get_plan_service] = get_plan_service
app.include_router(plan_routes_module.router, dependencies=[Depends(verify_token)])
app.router.routes.append(_MCPMount("/mcp", app=mcp_http_app, name="mcp"))


def _error_response(status_code: int, code: str, **extra: Any) -> JSONResponse:
    """Return a compact public error envelope with no exception text."""
    return JSONResponse(status_code=status_code, content={"detail": {"code": code, **extra}})


@app.exception_handler(DaemonStartupError)
async def startup_failure_handler(_: Request, exception: DaemonStartupError) -> JSONResponse:
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT, "startup_failed",
        message=str(exception), run_id=exception.run_id,
    )


@app.exception_handler(ProjectNotAllowedError)
async def project_not_allowed_handler(_: Request, __: ProjectNotAllowedError) -> JSONResponse:
    return _error_response(status.HTTP_404_NOT_FOUND, "project_not_found")


@app.exception_handler(ControlPlaneUnavailableError)
async def control_plane_unavailable_handler(
    _: Request, __: ControlPlaneUnavailableError
) -> JSONResponse:
    return _error_response(status.HTTP_503_SERVICE_UNAVAILABLE, "control_plane_unavailable")


@app.exception_handler(DeletedRunError)
async def deleted_run_handler(_: Request, __: DeletedRunError) -> JSONResponse:
    return _error_response(410, "run_deleted", message="Deleted record. Workflow files and recovery data are retained.")


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


@app.exception_handler(SkillNotFound)
async def skill_not_found_handler(_: Request, __: SkillNotFound) -> JSONResponse:
    return _error_response(status.HTTP_404_NOT_FOUND, "skill_not_found")


@app.exception_handler(SkillRevisionConflict)
async def skill_revision_conflict_handler(
    _: Request, exc: SkillRevisionConflict
) -> JSONResponse:
    return _error_response(
        status.HTTP_409_CONFLICT,
        "revision_conflict",
        current_revision=exc.current_revision,
    )


@app.exception_handler(SkillInstallError)
async def skill_install_rejected_handler(
    _: Request, exc: SkillInstallError
) -> JSONResponse:
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        exc.code,
        message=" ".join(str(exc).split())[:300],
    )


@app.exception_handler(SkillValidationError)
async def skill_invalid_handler(
    _: Request, exc: SkillValidationError
) -> JSONResponse:
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "skill_invalid",
        message=" ".join(str(exc).split())[:300],
    )


@app.exception_handler(SkillRefreshIncomplete)
async def skill_refresh_incomplete_handler(
    _: Request, exc: SkillRefreshIncomplete
) -> JSONResponse:
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "skill_refresh_incomplete",
        message=" ".join(str(exc).split())[:300],
    )


@app.exception_handler(SkillStoreError)
async def skill_error_handler(_: Request, exc: SkillStoreError) -> JSONResponse:
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "skill_error",
        message=" ".join(str(exc).split())[:300],
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


@app.exception_handler(GuidedConfigError)
async def guided_config_rejected_handler(
    _: Request, exc: GuidedConfigError
) -> JSONResponse:
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "guided_config_rejected",
        reason=exc.code,
        message=" ".join(str(exc).split())[:300],
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
async def rejected_operation_handler(_: Request, exception: Exception) -> JSONResponse:
    logger = logging.getLogger("aflow_app_server.rejected")
    if not any(isinstance(item, SensitiveDataFilter) for item in logger.filters):
        logger.addFilter(SensitiveDataFilter())
    logger.warning(
        "rejected operation type=%s detail=%s",
        type(exception).__name__,
        exception,
    )
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
    history: Literal["visible", "archived", "all"] = Query(default="visible"),
    limit: int = Query(default=100, ge=1, le=1_000),
    cursor: str | None = Query(default=None, max_length=64),
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
) -> RunListResponse:
    page = service.list_runs(project_id, limit=limit, cursor=cursor, history=history)
    return RunListResponse(
        runs=tuple(RunStatusResponse.from_canonical(run) for run in page.runs),
        next_cursor=page.next_cursor,
        schema_version=page.schema_version,
    )


class RunHistoryRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    acknowledge_active: bool = False


@app.post("/api/control-plane/projects/{project_id}/runs/{run_id}/archive", tags=["control-plane"])
def archive_run_record(project_id: str, run_id: str, body: RunHistoryRequest,
                      idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=256),
                      _: str = Depends(verify_token), service: ControlPlaneService = Depends(get_control_plane_service)):
    return service.change_history(project_id, run_id, state="archived", idempotency_key=idempotency_key, **body.model_dump())


@app.post("/api/control-plane/projects/{project_id}/runs/{run_id}/restore", tags=["control-plane"])
def restore_run_record(project_id: str, run_id: str, body: RunHistoryRequest,
                      idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=256),
                      _: str = Depends(verify_token), service: ControlPlaneService = Depends(get_control_plane_service)):
    return service.change_history(project_id, run_id, state="visible", idempotency_key=idempotency_key, **body.model_dump())


@app.delete("/api/control-plane/projects/{project_id}/runs/{run_id}", tags=["control-plane"])
def delete_run_record(project_id: str, run_id: str, body: RunHistoryRequest,
                      idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=256),
                      _: str = Depends(verify_token), service: ControlPlaneService = Depends(get_control_plane_service)):
    return service.change_history(project_id, run_id, state="deleted", idempotency_key=idempotency_key, **body.model_dump())


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

            try:
                pending_events = service.events(
                    project_id, run_id, after_sequence=cursor, limit=limit
                )
            except DeletedRunError:
                return

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


@app.get("/api/control-plane/projects/{project_id}/runs/{run_id}/restart-options", tags=["control-plane"])
def control_plane_restart_options(
    project_id: str,
    run_id: str,
    _: str = Depends(verify_token),
    service: ControlPlaneService = Depends(get_control_plane_service),
):
    return service.restart_options(project_id, run_id)


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
    """Typed create/register contract with no executable, absolute path, or argv fields.

    Project-level starter configuration fields are removed: every project
    uses the shared global configuration.
    """
    mode: str
    path: str
    display_name: str | None = None
    main_branch: str = "main"
    initialize_git: bool = False


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
        initialize_git=request.initialize_git,
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


# Global configuration endpoints.  Exactly two workflow documents are
# addressable; no route, payload, or response field can name a third file.
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
    "/api/config",
    response_model=ProjectConfigResponse,
    tags=["settings"],
)
def get_global_config(
    _: str = Depends(verify_token),
    service: GlobalConfigService = Depends(get_global_config_service),
) -> ProjectConfigResponse:
    """Return both exact global configuration texts with their revision.

    Changes to this shared configuration affect new runs in all projects;
    existing runs keep the configuration snapshot they were launched with.
    """
    return _config_response(service.read())


@app.patch("/api/config", response_model=ProjectConfigResponse, tags=["settings"])
def patch_global_config(
    payload: GlobalConfigPatchPayload,
    _: str = Depends(verify_token),
    service: GlobalConfigService = Depends(get_global_config_service),
) -> ProjectConfigResponse:
    return _config_response(service.patch(payload))


@app.put(
    "/api/config",
    response_model=ProjectConfigResponse,
    tags=["settings"],
)
def save_global_config(
    payload: ProjectConfigSavePayload,
    _: str = Depends(verify_token),
    service: GlobalConfigService = Depends(get_global_config_service),
) -> ProjectConfigResponse:
    """Validate and atomically commit both documents as one revisioned pair."""
    return _config_response(
        service.save(
            payload.aflow_toml,
            payload.workflows_toml,
            payload.expected_revision,
            caller_scope="rest",
        )
    )


@app.post(
    "/api/config/validate",
    response_model=ConfigValidationModel,
    tags=["settings"],
)
def validate_global_config(
    payload: ProjectConfigValidatePayload,
    _: str = Depends(verify_token),
    service: GlobalConfigService = Depends(get_global_config_service),
) -> ConfigValidationModel:
    """Validate a candidate pair through the production loader without saving."""
    return _config_validation_response(
        service.validate_candidate(payload.aflow_toml, payload.workflows_toml)
    )


def _skill_summary_response(entry) -> SkillSummaryModel:
    return SkillSummaryModel(
        name=entry.name,
        default=entry.default,
        revision=entry.revision,
        source=entry.source,  # type: ignore[arg-type]
        edited=entry.edited,
        installed=entry.installed,
        links=tuple(
            SkillLinkStatusModel(
                destination=link.destination,
                harnesses=link.harnesses,
                detected_harnesses=link.detected_harnesses,
                linked=link.linked,
            )
            for link in entry.links
        ),
        detected_harnesses=entry.detected_harnesses,
    )


def _skill_service() -> SkillService:
    """Build the account-local skill facade for the running server process."""
    return SkillService()


@app.get("/api/skills", response_model=tuple[SkillSummaryModel, ...], tags=["settings"])
def list_skills(_: str = Depends(verify_token)) -> tuple[SkillSummaryModel, ...]:
    """List exactly the registered bundled skills with revision/edit/link state.

    Reads are pure: nothing is initialized, refreshed, or installed.
    """
    return tuple(_skill_summary_response(entry) for entry in _skill_service().list_entries())


@app.get("/api/skills/{name}", response_model=SkillDetailModel, tags=["settings"])
def read_skill(name: str, _: str = Depends(verify_token)) -> SkillDetailModel:
    """Return one bundled skill entry plus its effective SKILL.md content."""
    entry, content = _skill_service().read_entry(name)
    summary = _skill_summary_response(entry)
    return SkillDetailModel(**summary.model_dump(mode="python"), content=content)


@app.put("/api/skills/{name}", response_model=SkillDetailModel, tags=["settings"])
def save_skill(
    name: str,
    payload: SkillSavePayload,
    _: str = Depends(verify_token),
) -> SkillDetailModel:
    """Compare-and-swap one canonical SKILL.md without installing anything."""
    entry, content = _skill_service().save_entry(
        name, payload.content, payload.expected_revision
    )
    summary = _skill_summary_response(entry)
    return SkillDetailModel(**summary.model_dump(mode="python"), content=content)


@app.post(
    "/api/skills/validate",
    response_model=SkillValidateResponse,
    tags=["settings"],
)
def validate_skills(
    payload: SkillValidatePayload,
    _: str = Depends(verify_token),
) -> SkillValidateResponse:
    """Prevalidate save candidates read-only; PUT repeats checks under its lock."""
    results = _skill_service().validate_batch(
        (
            {
                "name": entry.name,
                "content": entry.content,
                "expected_revision": entry.expected_revision,
            }
            for entry in payload.entries
        )
    )
    return SkillValidateResponse(
        entries=tuple(
            SkillValidateEntryResult(
                name=result.name,
                ok=result.ok,
                current_revision=result.current_revision,
                error_code=result.error_code,
                error=result.error,
            )
            for result in results
        )
    )


@app.post(
    "/api/skills/install",
    response_model=SkillInstallResponse,
    tags=["settings"],
)
def install_skills_route(
    payload: SkillInstallPayload | None = None,
    _: str = Depends(verify_token),
) -> SkillInstallResponse:
    """Refresh and link the default bundled skills via the shared installer.

    The request body stays empty; selection, detection, and deduplication
    match CLI ``install-skills --yes`` (optional skills excluded). Saving
    never installs: a saved-but-uninstalled skill only links after this call.
    """
    del payload
    result = _skill_service().install_default()
    return SkillInstallResponse(
        mode=result.mode,
        succeeded=result.succeeded,
        cancelled=result.cancelled,
        refresh=tuple(
            SkillRefreshModel(
                name=item.name,
                status=item.status,
                changed=item.changed,
                edited=item.edited,
                error=item.error,
            )
            for item in result.refresh
        ),
        operations=tuple(
            SkillInstallOperationModel(
                harness=item.harness,
                skill=item.skill,
                destination=str(item.destination),
                status=item.status,
                error_code=item.error_code,
                error=item.error,
                displaced_path=str(item.displaced_path)
                if item.displaced_path is not None
                else None,
            )
            for item in result.operations
        ),
    )


# Global transport settings (config.toml).  The credential is write-only: it
# never appears in responses, validation errors, logs, or audit records.
class SettingsResponse(BaseModel):
    bind_host: str
    bind_port: int
    managed_projects_root: str
    password_set: bool
    revision: str
    advanced_toml: str
    restart: dict[str, bool]


class SettingsSavePayload(BaseModel):
    expected_revision: str
    advanced_toml: str | None = None
    managed_projects_root: str | None = None
    bind_host: str | None = None
    bind_port: int | None = None
    password: str | None = None


_SETTINGS_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_SETTINGS_SECRET_KEYS = ("auth_token", "auth_token_file")


def _settings_file(config_dir: Path) -> Path:
    return config_dir / "config.toml"


def _settings_revision(config_dir: Path) -> str:
    try:
        payload = _settings_file(config_dir).read_bytes()
    except OSError:
        payload = b""
    return hashlib.sha256(payload).hexdigest()


def _advanced_settings_toml(config_dir: Path) -> str:
    """Render config.toml without any credential key for the advanced editor."""
    import tomlkit

    path = _settings_file(config_dir)
    if not path.is_file():
        return ""
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    server = document.get("server")
    if isinstance(server, tomlkit.items.Table):
        for key in _SETTINGS_SECRET_KEYS:
            if key in server:
                server.remove(key)
    return tomlkit.dumps(document)


def _apply_advanced_settings(config_dir: Path, text: str) -> dict[str, object]:
    """Parse credential-free advanced TOML into bounded settings updates."""
    import tomlkit

    check_document_text_size = len(text.encode("utf-8"))
    if check_document_text_size > 64 * 1024:
        raise ProjectConfigError("advanced settings exceed the supported size")
    document = tomlkit.parse(text)
    updates: dict[str, object] = {}
    server = document.get("server")
    if server is not None and not isinstance(server, dict):
        raise ProjectConfigError("[server] must be a table")
    for key in _SETTINGS_SECRET_KEYS:
        if isinstance(server, dict) and key in server:
            raise ProjectConfigError(
                f"the advanced settings editor must not contain [server] {key}; "
                "use the password field to change the credential"
            )
    if isinstance(server, dict):
        if "bind_host" in server:
            updates["bind_host"] = str(server["bind_host"])
        if "bind_port" in server:
            updates["bind_port"] = int(server["bind_port"])
    control_plane = document.get("control_plane")
    if control_plane is not None and not isinstance(control_plane, dict):
        raise ProjectConfigError("[control_plane] must be a table")
    if isinstance(control_plane, dict) and "managed_projects_root" in control_plane:
        updates["managed_projects_root"] = str(control_plane["managed_projects_root"])
    return updates


class SettingsPreviewPayload(CanonicalTransportModel):
    advanced_toml: str = Field(max_length=65536)


@app.post("/api/settings/preview", tags=["settings"])
def preview_settings(payload: SettingsPreviewPayload, _: str = Depends(verify_token)):
    try:
        return _apply_advanced_settings(global_config_dir(), payload.advanced_toml)
    except (ValueError, TypeError) as exc:
        raise ProjectConfigError("Invalid connection settings TOML") from exc


def _settings_response() -> SettingsResponse:
    """Build the redacted settings report from live and on-disk state."""
    live = _config
    if live is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "control_plane_unavailable"},
        )
    current = get_config()
    config_dir = global_config_dir()
    try:
        password_set = bool(current.current_auth_token())
    except ValueError:
        password_set = False
    restart = {
        "bind_host": current.bind_host != live.bind_host,
        "bind_port": current.bind_port != live.bind_port,
        "managed_projects_root": (
            current.managed_projects_root != live.managed_projects_root
        ),
    }
    root_text = str(current.managed_projects_root)
    home_text = str(Path.home())
    if root_text.startswith(home_text + "/"):
        root_text = "~" + root_text[len(home_text):]
    return SettingsResponse(
        bind_host=current.bind_host,
        bind_port=current.bind_port,
        managed_projects_root=root_text,
        password_set=password_set,
        revision=_settings_revision(config_dir),
        advanced_toml=_advanced_settings_toml(config_dir),
        restart=restart,
    )


@app.get("/api/settings", response_model=SettingsResponse, tags=["settings"])
def get_settings(
    _: str = Depends(verify_token),
) -> SettingsResponse:
    """Report transport settings without ever revealing the credential."""
    return _settings_response()


@app.put("/api/settings", response_model=SettingsResponse, tags=["settings"])
def save_settings(
    payload: SettingsSavePayload,
    _: str = Depends(verify_token),
) -> SettingsResponse:
    """Save transport settings; binding and root changes need a restart.

    A password change takes effect immediately: session signing derives from
    the credential, so old sessions are invalidated on the next request.
    """
    from .config import update_global_settings

    config_dir = global_config_dir()
    current_revision = _settings_revision(config_dir)
    if not _SETTINGS_HEX_RE.fullmatch(payload.expected_revision):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_revision"},
        )
    if payload.expected_revision != current_revision:
        raise ProjectConfigRevisionConflict(current_revision)
    updates: dict[str, object] = {}
    if payload.advanced_toml is not None:
        try:
            updates.update(_apply_advanced_settings(config_dir, payload.advanced_toml))
        except Exception as exc:
            if isinstance(exc, ProjectConfigError):
                raise
            raise ProjectConfigError(
                f"advanced settings could not be applied: {exc}"
            ) from exc
    if payload.managed_projects_root is not None:
        updates["managed_projects_root"] = payload.managed_projects_root
    if payload.bind_host is not None:
        updates["bind_host"] = payload.bind_host
    if payload.bind_port is not None:
        updates["bind_port"] = payload.bind_port
    try:
        update_global_settings(
            config_dir,
            auth_token=payload.password,
            managed_projects_root=updates.get("managed_projects_root"),
            bind_host=updates.get("bind_host"),
            bind_port=updates.get("bind_port"),
        )
    except ValueError as exc:
        raise ProjectConfigError(str(exc)) from exc
    return _settings_response()


# Browser session endpoints. Login exchanges the deployment bearer for a
# signed HttpOnly cookie; logout always clears it. No-store on all of them.
@app.post("/api/session")
def login_session(
    request: Request,
    response: Response,
    verified_token: str = Depends(verify_token),
) -> dict[str, bool]:
    """Verify a bearer login credential and start a rolling browser session.

    The cookie is signed with the exact token value that authenticated the
    submitted bearer, so a rotation concurrent with login can only issue a
    session for the submitted epoch, which the rotation itself invalidates.
    """
    if request.headers.get("authorization") is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized"},
        )
    if not _same_origin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "origin_required"},
        )
    response.headers["Cache-Control"] = "no-store"
    _set_session_cookie(
        response,
        encode_session(verified_token),
        SESSION_MAX_AGE_SECONDS,
        secure=_cookie_secure_for_request(request),
    )
    return {"authenticated": True}


@app.get("/api/session")
def session_status(
    response: Response,
    _: str = Depends(verify_token),
) -> dict[str, bool]:
    """Report an authenticated header bearer or browser session."""
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": True}


@app.delete("/api/session", status_code=status.HTTP_204_NO_CONTENT)
def logout_session(request: Request, response: Response) -> None:
    """Expire the browser session cookie; idempotent for absent sessions."""
    if not _same_origin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "origin_required"},
        )
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=_cookie_secure_for_request(request),
        samesite="strict",
    )


_BRANCH_PROBE_TIMEOUT_SECONDS = 15.0


def _probe_main_branch(root: Path) -> GuidedStarterDefaults:
    """Read the registered root's current branch with a bounded Git probe."""
    branch: str | None = None
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), "symbolic-ref", "--short", "HEAD"),
            check=False,
            capture_output=True,
            text=True,
            timeout=_BRANCH_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    if completed is not None and completed.returncode == 0:
        candidate = completed.stdout.strip()
        if candidate and not candidate.startswith("-"):
            try:
                # Advertise a probed branch only when starter rendering would
                # accept it; otherwise fall back to the labeled default.
                branch = validate_starter_main_branch(candidate)
            except ConfigError:
                branch = None
    if branch is None:
        return GuidedStarterDefaults(
            workflow="implement",
            main_branch="main",
            main_branch_source="fallback",
        )
    return GuidedStarterDefaults(
        workflow="implement", main_branch=branch, main_branch_source="git_head"
    )


@app.post(
    "/api/config/form",
    response_model=ProjectConfigFormResponse,
    tags=["settings"],
)
def global_config_form(
    payload: ProjectConfigFormPayload,
    _: str = Depends(verify_token),
) -> ProjectConfigFormResponse:
    """Transform a candidate global pair through the pure guided form; never saves.

    The endpoint accepts no ``expected_revision`` and performs no write.  The
    global configuration is not tied to one repository, so starter defaults
    are only probed against a registered project when that project is named
    explicitly via ``starter_project_id``.
    """
    starter_defaults: GuidedStarterDefaults | None = None
    wants_starter = (
        payload.action is None or isinstance(payload.action, BuildStarterAction)
    ) and payload.aflow_toml == "" and payload.workflows_toml == ""
    if wants_starter:
        starter_defaults = GuidedStarterDefaults(
            workflow="implement", main_branch="main", main_branch_source="fallback"
        )
    result = guided_form_response(
        payload.aflow_toml, payload.workflows_toml, payload.action
    )
    response = ProjectConfigFormResponse.model_validate(result)
    if starter_defaults is not None:
        response = response.model_copy(update={"starter_defaults": starter_defaults})
    return response


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
        detail=(
            "Web app assets are unavailable. Normal aworkflow installations "
            "bundle them; for a development checkout run "
            "`npm run build` in `apps/aflow_app/web`."
        ),
    )


def run_server(config: ServerConfig | None = None) -> None:
    """Run the server (entry point for the legacy ``aflow-app-server`` CLI).

    With an explicit configuration (the ``aflow ui`` path) the app serves
    with that resolved configuration; without one the environment-driven
    deployment configuration is used unchanged.
    """
    import uvicorn

    if config is None:
        config = ServerConfig.from_env()
        errors = config.validate()
        if errors:
            print(f"Configuration errors: {', '.join(errors)}")
            raise SystemExit(1)
    else:
        configure_server(config)

    uvicorn.run(
        "aflow_app_server.main:app",
        host=config.bind_host,
        port=config.bind_port,
        reload=False,
        log_config=_build_uvicorn_log_config(),
    )
