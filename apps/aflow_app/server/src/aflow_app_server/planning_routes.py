"""Provider-neutral planning-session and plan-draft HTTP routes."""

from __future__ import annotations

from datetime import datetime, timezone
import sys
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .planning import (
    Attachment,
    AttachmentKind,
    AttachmentNamespace,
    AuthorizedProjectContext,
    PlanningService,
    PendingApproval,
    PlanningError,
    ProviderOperationError,
    ProviderReadiness,
    ProviderState,
    Session,
    SessionKey,
    StartSessionRequest,
)
from .planning.models import ApprovalDecision, PlanningErrorCode, StartTurnRequest, Turn
from .project_catalog import ProjectCatalog
from .plan_store import PlanStore, PlanStoreError


router = APIRouter(tags=["planning"])


def _get_project_catalog() -> ProjectCatalog:
    raise RuntimeError("Project catalog dependency not initialized")


def _get_planning_service() -> PlanningService:
    raise RuntimeError("Planning service dependency not initialized")


class ApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartPlanningSessionRequest(ApiRequest):
    provider_id: str | None = Field(
        default=None, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
    )
    model: str | None = None
    reasoning_level: str | None = None


class RenameSessionRequest(ApiRequest):
    name: str = Field(min_length=1)


class ApprovalResponseRequest(ApiRequest):
    decision: Literal["accept", "decline", "cancel"]


class SaveDraftRequest(BaseModel):
    name: str
    content: str


class PromotePlanRequest(BaseModel):
    draft_name: str
    target_name: str | None = None


class ProvidersResponse(BaseModel):
    providers: tuple[ProviderReadiness, ...]


class ProviderModelsResponse(BaseModel):
    provider_id: str
    models: tuple[str, ...]


class ReasoningOptionsResponse(BaseModel):
    provider_id: str
    reasoning_levels: tuple[str, ...]
    reasoning_summaries: tuple[str, ...]


class SessionListResponse(BaseModel):
    sessions: tuple[Session, ...]
    providers: tuple[ProviderReadiness, ...]
    next_cursor: str | None = None


class StatusResponse(BaseModel):
    status: str


class ArchivedResponse(BaseModel):
    archived: bool


class PendingApprovalsResponse(BaseModel):
    approvals: tuple[PendingApproval, ...]


class AttachmentsResponse(BaseModel):
    attachments: tuple[Attachment, ...]


_ERROR_STATUSES = {
    PlanningErrorCode.INVALID_REQUEST: status.HTTP_400_BAD_REQUEST,
    PlanningErrorCode.PROVIDER_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    PlanningErrorCode.SESSION_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    PlanningErrorCode.ATTACHMENT_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    PlanningErrorCode.CONFLICT: status.HTTP_409_CONFLICT,
    PlanningErrorCode.ATTACHMENT_IN_USE: status.HTTP_409_CONFLICT,
    PlanningErrorCode.CAPABILITY_UNSUPPORTED: status.HTTP_422_UNPROCESSABLE_CONTENT,
    PlanningErrorCode.ATTACHMENT_LIMIT_EXCEEDED: status.HTTP_413_CONTENT_TOO_LARGE,
    PlanningErrorCode.PROVIDER_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    PlanningErrorCode.PROVIDER_TIMEOUT: status.HTTP_504_GATEWAY_TIMEOUT,
    PlanningErrorCode.INTERNAL_ERROR: status.HTTP_502_BAD_GATEWAY,
}


def provider_http_error(exc: ProviderOperationError) -> HTTPException:
    """Map the bounded provider error without exposing its cause or payload."""
    return HTTPException(
        status_code=_ERROR_STATUSES[exc.error.code],
        detail=exc.error.model_dump(mode="json"),
    )


def unexpected_provider_http_error(provider_id: str | None = None) -> HTTPException:
    """Return a fixed error envelope for failures outside the provider contract."""
    current = sys.exception()
    if isinstance(current, HTTPException):
        return current
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "code": PlanningErrorCode.INTERNAL_ERROR.value,
            "message": "Planning operation failed unexpectedly.",
            "provider_id": provider_id,
            "retryable": False,
        },
    )


def _project(project_id: str, catalog: ProjectCatalog, *, fast: bool = True):
    project = (
        catalog.get_project_fast(project_id) if fast else catalog.get_project(project_id)
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


def _key(provider_id: str, provider_session_id: str) -> SessionKey:
    try:
        return SessionKey(
            provider_id=provider_id, provider_session_id=provider_session_id
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid provider-qualified session identity",
        ) from exc


def _owned(project: Any, session: Session, catalog: ProjectCatalog) -> None:
    if not session.cwd or not catalog.project_owns_path(project, session.cwd):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Planning session not found"
        )


async def require_owned_session(
    project: Any,
    key: SessionKey,
    catalog: ProjectCatalog,
    service: PlanningService,
    *,
    include_turns: bool = False,
) -> Session:
    session = await service.read_session(key, include_turns=include_turns)
    if session.key != key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Planning session not found"
        )
    _owned(project, session, catalog)
    return session


def _session_sort_key(session: Session) -> tuple[float, str, str]:
    timestamp = session.updated_at or datetime.min.replace(tzinfo=timezone.utc)
    return (-timestamp.timestamp(), session.provider_id, session.provider_session_id)


def _namespace(project: Any, key: SessionKey) -> AttachmentNamespace:
    return AttachmentNamespace(
        project_id=project.id, key=key, project_cwd=str(project.current_path)
    )


@router.get("/api/planning/providers")
async def list_providers(
    service: PlanningService = Depends(_get_planning_service),
) -> ProvidersResponse:
    try:
        return {"providers": await service.provider_statuses()}
    except Exception as exc:
        raise unexpected_provider_http_error() from exc


@router.get("/api/planning/providers/{provider_id}/models")
async def list_provider_models(
    provider_id: str,
    service: PlanningService = Depends(_get_planning_service),
) -> ProviderModelsResponse:
    try:
        return {"provider_id": provider_id, "models": await service.list_models(provider_id)}
    except ProviderOperationError as exc:
        raise provider_http_error(exc) from exc
    except Exception as exc:
        raise unexpected_provider_http_error(provider_id) from exc


@router.get("/api/planning/providers/{provider_id}/reasoning-options")
async def list_reasoning_options(
    provider_id: str,
    service: PlanningService = Depends(_get_planning_service),
) -> ReasoningOptionsResponse:
    try:
        statuses = await service.provider_statuses()
    except Exception as exc:
        raise unexpected_provider_http_error(provider_id) from exc
    readiness = next((item for item in statuses if item.provider_id == provider_id), None)
    if readiness is None:
        try:
            service.registry.get(provider_id)
        except ProviderOperationError as exc:
            raise provider_http_error(exc) from exc
        raise provider_http_error(
            ProviderOperationError(
                PlanningError(
                    code=PlanningErrorCode.PROVIDER_UNAVAILABLE,
                    message="Planning provider is unavailable.",
                    provider_id=provider_id,
                    retryable=True,
                )
            )
        )
    if readiness.state in {ProviderState.UNAVAILABLE, ProviderState.DISABLED}:
        error = readiness.error or PlanningError(
            code=PlanningErrorCode.PROVIDER_UNAVAILABLE,
            message="Planning provider is unavailable.",
            provider_id=provider_id,
            retryable=True,
        )
        raise provider_http_error(ProviderOperationError(error))
    return {
        "provider_id": provider_id,
        "reasoning_levels": readiness.capabilities.reasoning_levels,
        "reasoning_summaries": readiness.capabilities.reasoning_summaries,
    }


@router.get("/api/projects/{project_id}/planning/sessions")
async def list_sessions(
    project_id: str,
    archived: bool | None = Query(default=None),
    catalog: ProjectCatalog = Depends(_get_project_catalog),
    service: PlanningService = Depends(_get_planning_service),
) -> SessionListResponse:
    project = _project(project_id, catalog)
    try:
        sessions, providers = await service.list_sessions(archived=archived)
    except ProviderOperationError as exc:
        raise provider_http_error(exc) from exc
    except Exception as exc:
        raise unexpected_provider_http_error() from exc
    owned = [session for session in sessions if session.cwd and catalog.project_owns_path(project, session.cwd)]
    owned.sort(key=_session_sort_key)
    return {"sessions": owned, "providers": providers, "next_cursor": None}


@router.post(
    "/api/projects/{project_id}/planning/sessions",
    status_code=status.HTTP_201_CREATED,
)
async def start_session(
    project_id: str,
    request: StartPlanningSessionRequest,
    catalog: ProjectCatalog = Depends(_get_project_catalog),
    service: PlanningService = Depends(_get_planning_service),
) -> Session:
    project = _project(project_id, catalog)
    try:
        return await service.start_session(
            AuthorizedProjectContext(project_id=project.id, cwd=str(project.current_path)),
            StartSessionRequest(
                model=request.model, reasoning_level=request.reasoning_level
            ),
            provider_id=request.provider_id,
        )
    except ProviderOperationError as exc:
        raise provider_http_error(exc) from exc
    except Exception as exc:
        raise unexpected_provider_http_error(request.provider_id) from exc


_DETAIL = "/api/projects/{project_id}/planning/providers/{provider_id}/sessions/{provider_session_id}"


@router.get(_DETAIL)
async def read_session(
    project_id: str,
    provider_id: str,
    provider_session_id: str,
    include_turns: bool = True,
    catalog: ProjectCatalog = Depends(_get_project_catalog),
    service: PlanningService = Depends(_get_planning_service),
) -> Session:
    project = _project(project_id, catalog)
    try:
        return await require_owned_session(
            project, _key(provider_id, provider_session_id), catalog, service,
            include_turns=include_turns,
        )
    except ProviderOperationError as exc:
        raise provider_http_error(exc) from exc
    except Exception as exc:
        raise unexpected_provider_http_error(provider_id) from exc


async def _resume_or_fork(
    project_id: str, provider_id: str, provider_session_id: str,
    catalog: ProjectCatalog, service: PlanningService, *, fork: bool,
) -> Session:
    project = _project(project_id, catalog)
    key = _key(provider_id, provider_session_id)
    if fork:
        service.require_capability(provider_id, "fork")
    await require_owned_session(project, key, catalog, service)
    if fork:
        return await service.fork_session(key, cwd=str(project.current_path))
    return await service.resume_session(key, cwd=str(project.current_path))


@router.post(_DETAIL + "/resume")
async def resume_session(
    project_id: str, provider_id: str, provider_session_id: str,
    catalog: ProjectCatalog = Depends(_get_project_catalog),
    service: PlanningService = Depends(_get_planning_service),
) -> Session:
    try:
        return await _resume_or_fork(project_id, provider_id, provider_session_id, catalog, service, fork=False)
    except ProviderOperationError as exc:
        raise provider_http_error(exc) from exc
    except Exception as exc:
        raise unexpected_provider_http_error(provider_id) from exc


@router.post(_DETAIL + "/fork", status_code=status.HTTP_201_CREATED)
async def fork_session(
    project_id: str, provider_id: str, provider_session_id: str,
    catalog: ProjectCatalog = Depends(_get_project_catalog),
    service: PlanningService = Depends(_get_planning_service),
) -> Session:
    try:
        return await _resume_or_fork(project_id, provider_id, provider_session_id, catalog, service, fork=True)
    except ProviderOperationError as exc:
        raise provider_http_error(exc) from exc
    except Exception as exc:
        raise unexpected_provider_http_error(provider_id) from exc


@router.patch(_DETAIL)
async def rename_session(
    project_id: str, provider_id: str, provider_session_id: str,
    request: RenameSessionRequest,
    catalog: ProjectCatalog = Depends(_get_project_catalog),
    service: PlanningService = Depends(_get_planning_service),
) -> StatusResponse:
    project = _project(project_id, catalog)
    key = _key(provider_id, provider_session_id)
    try:
        await require_owned_session(project, key, catalog, service)
        await service.set_session_name(key, request.name)
        return {"status": "ok"}
    except ProviderOperationError as exc:
        raise provider_http_error(exc) from exc
    except Exception as exc:
        raise unexpected_provider_http_error(provider_id) from exc


async def _set_archived(
    project_id: str, provider_id: str, provider_session_id: str,
    catalog: ProjectCatalog, service: PlanningService, *, archived: bool,
) -> ArchivedResponse:
    project = _project(project_id, catalog)
    key = _key(provider_id, provider_session_id)
    service.require_capability(provider_id, "archive")
    await require_owned_session(project, key, catalog, service)
    await service.set_archived(key, archived=archived)
    return {"archived": archived}


@router.post(_DETAIL + "/archive")
async def archive_session(
    project_id: str, provider_id: str, provider_session_id: str,
    catalog: ProjectCatalog = Depends(_get_project_catalog),
    service: PlanningService = Depends(_get_planning_service),
) -> ArchivedResponse:
    try:
        return await _set_archived(project_id, provider_id, provider_session_id, catalog, service, archived=True)
    except ProviderOperationError as exc:
        raise provider_http_error(exc) from exc
    except Exception as exc:
        raise unexpected_provider_http_error(provider_id) from exc


@router.post(_DETAIL + "/unarchive")
async def unarchive_session(
    project_id: str, provider_id: str, provider_session_id: str,
    catalog: ProjectCatalog = Depends(_get_project_catalog),
    service: PlanningService = Depends(_get_planning_service),
) -> ArchivedResponse:
    try:
        return await _set_archived(project_id, provider_id, provider_session_id, catalog, service, archived=False)
    except ProviderOperationError as exc:
        raise provider_http_error(exc) from exc
    except Exception as exc:
        raise unexpected_provider_http_error(provider_id) from exc


@router.post(_DETAIL + "/turns", status_code=status.HTTP_201_CREATED)
async def start_turn(
    project_id: str, provider_id: str, provider_session_id: str,
    request: StartTurnRequest,
    catalog: ProjectCatalog = Depends(_get_project_catalog),
    service: PlanningService = Depends(_get_planning_service),
) -> Turn:
    project = _project(project_id, catalog)
    key = _key(provider_id, provider_session_id)
    try:
        service.validate_turn_capabilities(provider_id, request)
        await require_owned_session(project, key, catalog, service)
        return await service.start_turn(
            key, request,
            context=AuthorizedProjectContext(project_id=project.id, cwd=str(project.current_path)),
        )
    except ProviderOperationError as exc:
        raise provider_http_error(exc) from exc
    except Exception as exc:
        raise unexpected_provider_http_error(provider_id) from exc


@router.post(_DETAIL + "/turns/{turn_id}/interrupt")
async def interrupt_turn(
    project_id: str, provider_id: str, provider_session_id: str, turn_id: str,
    catalog: ProjectCatalog = Depends(_get_project_catalog),
    service: PlanningService = Depends(_get_planning_service),
) -> StatusResponse:
    project = _project(project_id, catalog)
    key = _key(provider_id, provider_session_id)
    try:
        service.require_capability(provider_id, "interruption")
        await require_owned_session(project, key, catalog, service)
        await service.interrupt_turn(key, turn_id)
        return {"status": "interrupted"}
    except ProviderOperationError as exc:
        raise provider_http_error(exc) from exc
    except Exception as exc:
        raise unexpected_provider_http_error(provider_id) from exc


@router.get(_DETAIL + "/approvals")
async def list_pending_approvals(
    project_id: str, provider_id: str, provider_session_id: str,
    catalog: ProjectCatalog = Depends(_get_project_catalog),
    service: PlanningService = Depends(_get_planning_service),
) -> PendingApprovalsResponse:
    project = _project(project_id, catalog)
    key = _key(provider_id, provider_session_id)
    try:
        service.require_capability(provider_id, "approvals")
        await require_owned_session(project, key, catalog, service)
        approvals = tuple(
            approval
            for approval in service.pending_approvals(provider_id)
            if approval.key == key
        )
        return {"approvals": approvals}
    except ProviderOperationError as exc:
        raise provider_http_error(exc) from exc
    except Exception as exc:
        raise unexpected_provider_http_error(provider_id) from exc


@router.post(_DETAIL + "/approvals/{approval_id}")
async def respond_to_approval(
    project_id: str, provider_id: str, provider_session_id: str, approval_id: str,
    request: ApprovalResponseRequest,
    catalog: ProjectCatalog = Depends(_get_project_catalog),
    service: PlanningService = Depends(_get_planning_service),
) -> StatusResponse:
    project = _project(project_id, catalog)
    key = _key(provider_id, provider_session_id)
    try:
        service.require_capability(provider_id, "approvals")
        await require_owned_session(project, key, catalog, service)
        await service.respond_to_approval(
            key, ApprovalDecision(approval_id=approval_id, decision=request.decision)
        )
        return {"status": "recorded"}
    except ProviderOperationError as exc:
        raise provider_http_error(exc) from exc
    except Exception as exc:
        raise unexpected_provider_http_error(provider_id) from exc


@router.post(_DETAIL + "/attachments", status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    project_id: str, provider_id: str, provider_session_id: str,
    file: UploadFile = File(...),
    kind: AttachmentKind = Form(AttachmentKind.FILE),
    catalog: ProjectCatalog = Depends(_get_project_catalog),
    service: PlanningService = Depends(_get_planning_service),
) -> Attachment:
    project = _project(project_id, catalog)
    key = _key(provider_id, provider_session_id)
    try:
        service.require_capability(provider_id, "attachments")
        await require_owned_session(project, key, catalog, service)
        if service.attachment_store is None:
            raise HTTPException(status_code=503, detail="Attachment storage is unavailable")
        return service.attachment_store.upload(
            _namespace(project, key), filename=file.filename or "attachment",
            kind=kind, media_type=file.content_type, content=file.file,
        )
    except ProviderOperationError as exc:
        raise provider_http_error(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise unexpected_provider_http_error(provider_id) from exc
    finally:
        await file.close()


@router.get(_DETAIL + "/attachments")
async def list_attachments(
    project_id: str, provider_id: str, provider_session_id: str,
    catalog: ProjectCatalog = Depends(_get_project_catalog),
    service: PlanningService = Depends(_get_planning_service),
) -> AttachmentsResponse:
    project = _project(project_id, catalog)
    key = _key(provider_id, provider_session_id)
    try:
        service.require_capability(provider_id, "attachments")
        await require_owned_session(project, key, catalog, service)
        if service.attachment_store is None:
            raise HTTPException(status_code=503, detail="Attachment storage is unavailable")
        return {"attachments": service.attachment_store.list(_namespace(project, key))}
    except ProviderOperationError as exc:
        raise provider_http_error(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise unexpected_provider_http_error(provider_id) from exc


@router.delete(_DETAIL + "/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    project_id: str, provider_id: str, provider_session_id: str, attachment_id: str,
    catalog: ProjectCatalog = Depends(_get_project_catalog),
    service: PlanningService = Depends(_get_planning_service),
) -> None:
    project = _project(project_id, catalog)
    key = _key(provider_id, provider_session_id)
    try:
        service.require_capability(provider_id, "attachments")
        await require_owned_session(project, key, catalog, service)
        if service.attachment_store is None:
            raise HTTPException(status_code=503, detail="Attachment storage is unavailable")
        service.attachment_store.delete(_namespace(project, key), attachment_id)
    except ProviderOperationError as exc:
        raise provider_http_error(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise unexpected_provider_http_error(provider_id) from exc


# Plan-draft URLs intentionally remain unchanged; they are not provider operations.
@router.post("/api/projects/{project_id}/plans/drafts", status_code=status.HTTP_201_CREATED)
async def save_draft(project_id: str, request: SaveDraftRequest, catalog: ProjectCatalog = Depends(_get_project_catalog)) -> dict[str, Any]:
    project = _project(project_id, catalog, fast=False)
    try:
        path = PlanStore(project.current_path).save_draft(request.name, request.content)
        return {"name": request.name, "path": str(path), "status": "draft"}
    except PlanStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/projects/{project_id}/plans/drafts")
async def list_drafts(project_id: str, catalog: ProjectCatalog = Depends(_get_project_catalog)) -> list[str]:
    project = _project(project_id, catalog, fast=False)
    return PlanStore(project.current_path).list_drafts()


@router.get("/api/projects/{project_id}/plans/drafts/{name}")
async def load_draft(project_id: str, name: str, catalog: ProjectCatalog = Depends(_get_project_catalog)) -> dict[str, str]:
    project = _project(project_id, catalog, fast=False)
    try:
        return {"name": name, "content": PlanStore(project.current_path).load_draft(name)}
    except PlanStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/api/projects/{project_id}/plans/drafts/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_draft(project_id: str, name: str, catalog: ProjectCatalog = Depends(_get_project_catalog)) -> None:
    project = _project(project_id, catalog, fast=False)
    if not PlanStore(project.current_path).delete_draft(name):
        raise HTTPException(status_code=404, detail="Draft not found")


@router.post("/api/projects/{project_id}/plans/promote")
async def promote_plan(project_id: str, request: PromotePlanRequest, catalog: ProjectCatalog = Depends(_get_project_catalog)) -> dict[str, Any]:
    project = _project(project_id, catalog, fast=False)
    try:
        path = PlanStore(project.current_path).promote_to_in_progress(request.draft_name, request.target_name)
        return {"name": path.stem, "path": str(path), "status": "in_progress"}
    except PlanStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/projects/{project_id}/plans/in-progress")
async def list_in_progress(project_id: str, catalog: ProjectCatalog = Depends(_get_project_catalog)) -> list[str]:
    project = _project(project_id, catalog, fast=False)
    return PlanStore(project.current_path).list_in_progress()
