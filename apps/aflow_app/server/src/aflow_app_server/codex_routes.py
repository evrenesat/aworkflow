"""Temporary legacy thread routes backed by the provider-neutral planning service."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .planning import (
    AuthorizedProjectContext,
    PlanningService,
    ProviderOperationError,
    Session,
    SessionKey,
    StartSessionRequest,
    Turn,
)
from .planning.models import StartTurnRequest as PlanningStartTurnRequest
from .project_catalog import ProjectCatalog
from .plan_store import PlanStore, PlanStoreError


router = APIRouter(prefix="/api/projects/{project_id}", tags=["projects"])


class LegacyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartThreadRequest(LegacyRequest):
    cwd: str | None = None
    model: str | None = None
    model_provider: str | None = None
    service_tier: str | None = None
    approval_policy: str | None = None
    experimental_raw_events: bool = False
    persist_extended_history: bool = False

    @model_validator(mode="after")
    def reject_route_owned_values(self) -> StartThreadRequest:
        if self.cwd is not None:
            raise ValueError("cwd is selected by the project route")
        return self


class ResumeThreadRequest(StartThreadRequest):
    pass


class ForkThreadRequest(StartThreadRequest):
    pass


class SetThreadNameRequest(LegacyRequest):
    name: str = Field(min_length=1)


class StartTurnRequest(LegacyRequest):
    input: list[dict[str, Any]] = Field(min_length=1)
    cwd: str | None = None
    approval_policy: str | None = None
    model: str | None = None
    service_tier: str | None = None
    effort: str | None = None
    summary: str | None = None
    personality: str | None = None

    @model_validator(mode="after")
    def reject_route_owned_values(self) -> StartTurnRequest:
        if self.cwd is not None:
            raise ValueError("cwd is selected by the project route")
        for item in self.input:
            if item.get("type") != "text" or not isinstance(item.get("text"), str):
                raise ValueError("only text input is supported by this compatibility route")
        return self


class SaveDraftRequest(BaseModel):
    name: str
    content: str


class PromotePlanRequest(BaseModel):
    draft_name: str
    target_name: str | None = None


def _get_config():
    """Compatibility dependency overridden by the main application."""
    raise RuntimeError("Config dependency not initialized")


def _get_project_catalog() -> ProjectCatalog:
    raise RuntimeError("Project catalog dependency not initialized")


def _get_planning_service() -> PlanningService:
    raise RuntimeError("Planning service dependency not initialized")


def _key(thread_id: str) -> SessionKey:
    return SessionKey(provider_id="codex", provider_session_id=thread_id)


def _provider_error(error: ProviderOperationError) -> HTTPException:
    code = error.error.code.value
    status_code = {
        "invalid_request": status.HTTP_400_BAD_REQUEST,
        "session_not_found": status.HTTP_404_NOT_FOUND,
        "conflict": status.HTTP_409_CONFLICT,
        "capability_unsupported": status.HTTP_422_UNPROCESSABLE_ENTITY,
        "provider_timeout": status.HTTP_504_GATEWAY_TIMEOUT,
    }.get(code, status.HTTP_502_BAD_GATEWAY)
    return HTTPException(status_code=status_code, detail=error.error.message)


def _legacy_status(session: Session) -> Any:
    if session.status.value == "idle":
        return {"type": "active", "activeFlags": []}
    return {"type": session.status.value, "activeFlags": []}


def _legacy_turn(turn: Turn) -> dict[str, Any]:
    status_value = {
        "running": "inProgress",
        "waiting_for_approval": "inProgress",
        "interrupted": "failed",
    }.get(turn.status.value, turn.status.value)
    return {
        "id": turn.turn_id,
        "status": status_value,
        "items": list(turn.items),
        "error": turn.error.model_dump(mode="json") if turn.error else None,
    }


def _legacy_thread(session: Session, *, summary: bool = False) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "id": session.provider_session_id,
        "preview": session.preview.strip()[:120],
        "updated_at": session.updated_at.isoformat() if session.updated_at else now,
        "cwd": session.cwd,
        "source": "codex",
        "name": session.title,
        "status": _legacy_status(session),
    }
    if summary:
        return payload
    payload.update(
        {
            "ephemeral": False,
            "model_provider": "openai",
            "created_at": session.created_at.isoformat() if session.created_at else now,
            "path": None,
            "cli_version": "",
            "agent_nickname": None,
            "agent_role": None,
            "git_info": None,
            "turns": [_legacy_turn(turn) for turn in session.turns],
        }
    )
    return payload


def _legacy_mutation(session: Session) -> dict[str, Any]:
    return {
        "thread": _legacy_thread(session),
        "model": session.model,
        "model_provider": "openai",
        "service_tier": None,
        "cwd": session.cwd,
        "approval_policy": "never",
        "approvals_reviewer": {},
        "sandbox": {"type": "danger-full-access"},
        "reasoning_effort": session.reasoning_level,
    }


def _lookup_paths(project: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(path) for path in (project.current_path, *project.historical_aliases)))


def _ensure_owned(
    project: Any, session: Session, project_catalog: ProjectCatalog
) -> None:
    projects = project_catalog.list_projects()
    if not session.cwd or not project_catalog.project_owns_path(
        project, session.cwd, projects=projects
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")


async def _require_owned_session(
    project: Any,
    thread_id: str,
    project_catalog: ProjectCatalog,
    planning_service: PlanningService,
    *,
    include_turns: bool = False,
) -> Session:
    session = await planning_service.read_session(
        _key(thread_id), include_turns=include_turns
    )
    _ensure_owned(project, session, project_catalog)
    return session


@router.get("/threads")
async def list_threads(
    project_id: str,
    cwd: str | None = None,
    search_term: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    source_kinds: list[str] | None = None,
    archived: bool | None = None,
    project_catalog: ProjectCatalog = Depends(_get_project_catalog),
    planning_service: PlanningService = Depends(_get_planning_service),
) -> dict[str, Any]:
    project = project_catalog.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    paths = _lookup_paths(project)
    if cwd is not None and cwd not in paths:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cwd is not owned by project")
    if source_kinds and "codex" not in source_kinds and "app-server" not in source_kinds:
        return {"threads": [], "next_cursor": None, "backend_status": _backend_status(True)}
    sessions_by_id: dict[str, Session] = {}
    try:
        for path in ((cwd,) if cwd is not None else paths):
            sessions, _ = await planning_service.list_sessions(
                provider_id="codex", cwd=path, archived=archived
            )
            for session in sessions:
                if search_term and search_term.casefold() not in (
                    f"{session.title or ''} {session.preview}".casefold()
                ):
                    continue
                sessions_by_id[session.provider_session_id] = session
        sessions = list(sessions_by_id.values())
        if limit is not None:
            sessions = sessions[:limit]
        return {
            "threads": [_legacy_thread(session, summary=True) for session in sessions],
            "next_cursor": None,
            "backend_status": _backend_status(True),
        }
    except ProviderOperationError as error:
        return {
            "threads": [],
            "next_cursor": None,
            "backend_status": _backend_status(False, error.error.message),
        }


def _backend_status(ready: bool, message: str | None = None) -> dict[str, str | None]:
    if ready:
        return {"state": "ready", "message": None, "detail": None}
    return {
        "state": "error",
        "message": message or "Planning provider is unavailable.",
        "detail": None,
    }


@router.get("/threads/{thread_id}")
async def read_thread(
    project_id: str,
    thread_id: str,
    include_turns: bool = True,
    project_catalog: ProjectCatalog = Depends(_get_project_catalog),
    planning_service: PlanningService = Depends(_get_planning_service),
) -> dict[str, Any]:
    project = project_catalog.get_project_fast(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    try:
        session = await _require_owned_session(
            project,
            thread_id,
            project_catalog,
            planning_service,
            include_turns=include_turns,
        )
        return _legacy_thread(session)
    except ProviderOperationError as error:
        raise _provider_error(error) from error


@router.post("/threads")
async def start_thread(
    project_id: str,
    request: StartThreadRequest,
    project_catalog: ProjectCatalog = Depends(_get_project_catalog),
    planning_service: PlanningService = Depends(_get_planning_service),
) -> dict[str, Any]:
    project = project_catalog.get_project_fast(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    try:
        session = await planning_service.start_session(
            AuthorizedProjectContext(project_id=project.id, cwd=str(project.current_path)),
            StartSessionRequest(model=request.model),
            provider_id="codex",
        )
        return _legacy_mutation(session)
    except ProviderOperationError as error:
        raise _provider_error(error) from error


@router.post("/threads/{thread_id}/resume")
async def resume_thread(
    project_id: str,
    thread_id: str,
    request: ResumeThreadRequest,
    project_catalog: ProjectCatalog = Depends(_get_project_catalog),
    planning_service: PlanningService = Depends(_get_planning_service),
) -> dict[str, Any]:
    project = project_catalog.get_project_fast(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    try:
        await _require_owned_session(
            project, thread_id, project_catalog, planning_service
        )
        session = await planning_service.resume_session(
            _key(thread_id), cwd=str(project.current_path)
        )
        return _legacy_mutation(session)
    except ProviderOperationError as error:
        raise _provider_error(error) from error


@router.post("/threads/{thread_id}/fork")
async def fork_thread(
    project_id: str,
    thread_id: str,
    request: ForkThreadRequest,
    project_catalog: ProjectCatalog = Depends(_get_project_catalog),
    planning_service: PlanningService = Depends(_get_planning_service),
) -> dict[str, Any]:
    project = project_catalog.get_project_fast(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    try:
        await _require_owned_session(
            project, thread_id, project_catalog, planning_service
        )
        session = await planning_service.fork_session(
            _key(thread_id), cwd=str(project.current_path)
        )
        return _legacy_mutation(session)
    except ProviderOperationError as error:
        raise _provider_error(error) from error


@router.patch("/threads/{thread_id}/name")
async def set_thread_name(
    project_id: str,
    thread_id: str,
    request: SetThreadNameRequest,
    project_catalog: ProjectCatalog = Depends(_get_project_catalog),
    planning_service: PlanningService = Depends(_get_planning_service),
) -> dict[str, str]:
    project = project_catalog.get_project_fast(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    try:
        await _require_owned_session(
            project, thread_id, project_catalog, planning_service
        )
        await planning_service.set_session_name(_key(thread_id), request.name)
        return {"status": "ok"}
    except ProviderOperationError as error:
        raise _provider_error(error) from error


@router.post("/threads/{thread_id}/turns")
async def start_turn(
    project_id: str,
    thread_id: str,
    request: StartTurnRequest,
    project_catalog: ProjectCatalog = Depends(_get_project_catalog),
    planning_service: PlanningService = Depends(_get_planning_service),
) -> dict[str, Any]:
    project = project_catalog.get_project_fast(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    text = "\n".join(str(item["text"]) for item in request.input)
    try:
        await _require_owned_session(
            project, thread_id, project_catalog, planning_service
        )
        turn = await planning_service.start_turn(
            _key(thread_id),
            PlanningStartTurnRequest(
                text=text,
                model=request.model,
                reasoning_level=request.effort,
                reasoning_summary=request.summary,
            ),
        )
        return _legacy_turn(turn)
    except ProviderOperationError as error:
        raise _provider_error(error) from error


@router.post("/plans/drafts", status_code=status.HTTP_201_CREATED)
async def save_draft(
    project_id: str,
    request: SaveDraftRequest,
    project_catalog: ProjectCatalog = Depends(_get_project_catalog),
) -> dict[str, Any]:
    """Save a plan as a draft."""
    project = project_catalog.get_project(project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    store = PlanStore(project.current_path)
    try:
        path = store.save_draft(request.name, request.content)
        return {
            "name": request.name,
            "path": str(path),
            "status": "draft",
        }
    except PlanStoreError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error


@router.get("/plans/drafts")
async def list_drafts(
    project_id: str,
    project_catalog: ProjectCatalog = Depends(_get_project_catalog),
) -> list[str]:
    """List all draft plans for a repository."""
    project = project_catalog.get_project(project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    store = PlanStore(project.current_path)
    return store.list_drafts()


@router.get("/plans/drafts/{name}")
async def load_draft(
    project_id: str,
    name: str,
    project_catalog: ProjectCatalog = Depends(_get_project_catalog),
) -> dict[str, str]:
    """Load a draft plan."""
    project = project_catalog.get_project(project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    store = PlanStore(project.current_path)
    try:
        content = store.load_draft(name)
        return {"name": name, "content": content}
    except PlanStoreError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error


@router.delete("/plans/drafts/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_draft(
    project_id: str,
    name: str,
    project_catalog: ProjectCatalog = Depends(_get_project_catalog),
) -> None:
    """Delete a draft plan."""
    project = project_catalog.get_project(project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    store = PlanStore(project.current_path)
    if not store.delete_draft(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Draft not found",
        )


@router.post("/plans/promote")
async def promote_plan(
    project_id: str,
    request: PromotePlanRequest,
    project_catalog: ProjectCatalog = Depends(_get_project_catalog),
) -> dict[str, Any]:
    """Promote a draft plan to in-progress status."""
    project = project_catalog.get_project(project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    store = PlanStore(project.current_path)
    try:
        path = store.promote_to_in_progress(request.draft_name, request.target_name)
        return {
            "name": path.stem,
            "path": str(path),
            "status": "in_progress",
        }
    except PlanStoreError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error


@router.get("/plans/in-progress")
async def list_in_progress(
    project_id: str,
    project_catalog: ProjectCatalog = Depends(_get_project_catalog),
) -> list[str]:
    """List all in-progress plans for a repository."""
    project = project_catalog.get_project(project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    store = PlanStore(project.current_path)
    return store.list_in_progress()
