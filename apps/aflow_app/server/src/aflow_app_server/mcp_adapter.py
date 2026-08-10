"""FastMCP transport adapter for the daemon-owned control plane."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Literal, Mapping

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError, ToolError

from aflow.control_plane import (
    ControlConflictError,
    ControlIdempotencyConflict,
    RepositoryNotFoundError,
    RestartRequiredControlError,
    RunControlRequest,
    RunIdentityError,
    ServiceAuthorizationError,
    StartupQuestionRecord,
)
from aflow.control_plane.persistence import PersistenceError
from aflow.daemon import DaemonAuthorizationError, DaemonError, DaemonIdempotencyConflict

from .control_plane_service import (
    ControlPlaneService,
    ControlPlaneUnavailableError,
    ProjectNotAllowedError,
)


ControlPlaneServiceGetter = Callable[[], ControlPlaneService]

_READ_TOOL_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_WRITE_TOOL_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_OWNER_STOP_TOOL_ANNOTATIONS = {
    **_WRITE_TOOL_ANNOTATIONS,
    "destructiveHint": True,
}
_CREDENTIAL_VALUE = re.compile(
    r"(?i)\bbearer[ \t]+[^\s]+|(?:token|access_token|authorization)=[^&\s]+"
)


def _public_error_code(exc: Exception) -> str:
    """Map internal failures to stable MCP-safe error codes."""
    if isinstance(exc, ProjectNotAllowedError):
        return "project_not_found"
    if isinstance(exc, ControlPlaneUnavailableError):
        return "control_plane_unavailable"
    if isinstance(exc, RepositoryNotFoundError):
        return "run_not_found"
    if isinstance(exc, (ControlIdempotencyConflict, DaemonIdempotencyConflict)):
        return "idempotency_conflict"
    if isinstance(exc, ControlConflictError):
        return "revision_conflict"
    if isinstance(exc, RestartRequiredControlError):
        return "restart_required"
    if isinstance(exc, (DaemonAuthorizationError, ServiceAuthorizationError)):
        return "operation_forbidden"
    if isinstance(exc, (RunIdentityError, PersistenceError, DaemonError, ValueError)):
        return "operation_rejected"
    return "internal_error"


def _tool_result(
    operation: Callable[[], dict[str, Any]], arguments: Mapping[str, object] | None = None
) -> dict[str, Any]:
    try:
        _reject_credential_arguments(arguments or {})
        return operation()
    except Exception as exc:
        raise ToolError(_public_error_code(exc)) from None


def _resource_result(
    operation: Callable[[], dict[str, Any]], arguments: Mapping[str, object] | None = None
) -> dict[str, Any]:
    try:
        _reject_credential_arguments(arguments or {})
        return operation()
    except Exception as exc:
        raise ResourceError(_public_error_code(exc)) from None


def _bounded_limit(limit: int) -> int:
    if not 1 <= limit <= 1_000:
        raise ValueError("limit must be between 1 and 1000")
    return limit


def _reject_credential_arguments(arguments: Mapping[str, object]) -> None:
    if any(_contains_credential(value) for value in arguments.values()):
        raise ValueError("credential-like MCP arguments are not allowed")


def _contains_credential(value: object) -> bool:
    if isinstance(value, str):
        return _CREDENTIAL_VALUE.search(value) is not None
    if isinstance(value, Mapping):
        return any(_contains_credential(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_credential(item) for item in value)
    return False


def _bounded_cursor(cursor: str | None) -> str | None:
    if cursor is not None and len(cursor) > 64:
        raise ValueError("cursor must be at most 64 characters")
    return cursor


def _bounded_idempotency_key(idempotency_key: str | None) -> str | None:
    if idempotency_key is not None and len(idempotency_key) > 256:
        raise ValueError("idempotency key must be at most 256 characters")
    return idempotency_key


def _start_response(result: object) -> dict[str, Any]:
    if isinstance(result, StartupQuestionRecord):
        return {"startup_question": result.to_dict()}
    return {"result": result.to_dict()}  # type: ignore[union-attr]


def create_control_plane_mcp(get_service: ControlPlaneServiceGetter) -> FastMCP:
    """Create the stateless MCP adapter over one shared service instance."""
    mcp = FastMCP(
        "AFlow Control Plane",
        version="1",
        instructions=(
            "Use only configured project IDs. Write tools mutate daemon-owned "
            "AFlow control-plane state and require client approval."
        ),
        mask_error_details=True,
    )

    @mcp.tool(
        title="List AFlow project capabilities",
        annotations=_READ_TOOL_ANNOTATIONS,
        tags={"read"},
    )
    def get_capabilities() -> dict[str, Any]:
        """Return versioned capabilities for every allowlisted project."""
        return _tool_result(
            lambda: {
                "projects": {
                    project.project_id: get_service().capabilities(project.project_id).to_dict()
                    for project in get_service().projects()
                }
            }
        )

    @mcp.tool(
        title="List AFlow projects",
        annotations=_READ_TOOL_ANNOTATIONS,
        tags={"read"},
    )
    def list_projects() -> dict[str, Any]:
        """Return the configured project allowlist."""
        return _tool_result(
            lambda: {"projects": [project.to_dict() for project in get_service().projects()]}
        )

    @mcp.tool(
        title="Get AFlow project capabilities",
        annotations=_READ_TOOL_ANNOTATIONS,
        tags={"read"},
    )
    def get_project_capabilities(project_id: str) -> dict[str, Any]:
        """Return one allowlisted project's versioned capabilities."""
        return _tool_result(
            lambda: get_service().capabilities(project_id).to_dict(),
            {"project_id": project_id},
        )

    @mcp.tool(
        title="List AFlow plans",
        annotations=_READ_TOOL_ANNOTATIONS,
        tags={"read"},
    )
    def list_plans(
        project_id: str,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List bounded plan metadata for an allowlisted project."""
        return _tool_result(
            lambda: {
                "plans": [
                    plan.to_dict()
                    for plan in get_service().list_plans(
                        project_id,
                        limit=_bounded_limit(limit),
                        cursor=_bounded_cursor(cursor),
                    )
                ]
            },
            {"project_id": project_id, "limit": limit, "cursor": cursor},
        )

    @mcp.tool(
        title="List AFlow runs",
        annotations=_READ_TOOL_ANNOTATIONS,
        tags={"read"},
    )
    def list_runs(
        project_id: str,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List a bounded, versioned page of run status."""
        return _tool_result(
            lambda: (
                get_service()
                .list_runs(
                    project_id,
                    limit=_bounded_limit(limit),
                    cursor=_bounded_cursor(cursor),
                )
                .to_dict()
            ),
            {"project_id": project_id, "limit": limit, "cursor": cursor},
        )

    @mcp.tool(
        title="Get AFlow run state",
        annotations=_READ_TOOL_ANNOTATIONS,
        tags={"read"},
    )
    def get_run(project_id: str, run_id: str) -> dict[str, Any]:
        """Return canonical state for one run."""
        return _tool_result(
            lambda: get_service().run_status(project_id, run_id).to_dict(),
            {"project_id": project_id, "run_id": run_id},
        )

    @mcp.tool(
        title="Get AFlow run events",
        annotations=_READ_TOOL_ANNOTATIONS,
        tags={"read"},
    )
    def get_run_events(
        project_id: str,
        run_id: str,
        after_sequence: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return a bounded tail of ordered durable run events."""
        return _tool_result(
            lambda: {
                "events": [
                    event.to_dict()
                    for event in get_service().events(
                        project_id,
                        run_id,
                        after_sequence=(
                            after_sequence
                            if after_sequence is None or after_sequence >= 0
                            else _reject_after_sequence()
                        ),
                        limit=_bounded_limit(limit),
                    )
                ]
            },
            {
                "project_id": project_id,
                "run_id": run_id,
                "after_sequence": after_sequence,
                "limit": limit,
            },
        )

    @mcp.tool(
        title="Get AFlow run context",
        annotations=_READ_TOOL_ANNOTATIONS,
        tags={"read"},
    )
    def get_run_context(
        project_id: str,
        run_id: str,
        level: Literal["lite", "full"] = "lite",
        full_scope: bool = False,
    ) -> dict[str, Any]:
        """Return bounded run context, requiring explicit full-scope acknowledgement."""
        return _tool_result(
            lambda: (
                get_service()
                .context(
                    project_id,
                    run_id,
                    level=level,
                    full_scope=_validated_full_scope(level, full_scope),
                )
                .to_dict()
            ),
            {
                "project_id": project_id,
                "run_id": run_id,
                "level": level,
                "full_scope": full_scope,
            },
        )

    @mcp.tool(
        title="Start an AFlow run",
        annotations=_WRITE_TOOL_ANNOTATIONS,
        tags={"write", "approval-required"},
    )
    def start_run(
        project_id: str,
        plan_path: str,
        idempotency_key: str,
        workflow_name: str | None = None,
        team: str | None = None,
        start_step: str | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        """Reserve and start one daemon-owned workflow, or return its startup question."""
        return _tool_result(
            lambda: _start_response(
                get_service().start_run(
                    project_id,
                    plan_path=plan_path,
                    workflow_name=workflow_name,
                    team=team,
                    start_step=start_step,
                    max_turns=max_turns,
                    idempotency_key=_bounded_idempotency_key(idempotency_key),
                    caller_scope="mcp",
                )
            ),
            {
                "project_id": project_id,
                "plan_path": plan_path,
                "idempotency_key": idempotency_key,
                "workflow_name": workflow_name,
                "team": team,
                "start_step": start_step,
                "max_turns": max_turns,
            },
        )

    @mcp.tool(
        title="Submit an AFlow startup answer",
        annotations=_WRITE_TOOL_ANNOTATIONS,
        tags={"write", "approval-required"},
    )
    def answer_startup(
        project_id: str,
        question_id: str,
        answer: str | int | bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Submit one authenticated answer for a pending startup question."""
        return _tool_result(
            lambda: _start_response(
                get_service().answer_startup(
                    project_id,
                    question_id,
                    answer,
                    idempotency_key=_bounded_idempotency_key(idempotency_key),
                    caller_scope="mcp",
                )
            ),
            {
                "project_id": project_id,
                "question_id": question_id,
                "answer": answer,
                "idempotency_key": idempotency_key,
            },
        )

    @mcp.tool(
        title="Control an AFlow run",
        annotations=_WRITE_TOOL_ANNOTATIONS,
        tags={"write", "approval-required"},
    )
    def control_run(
        project_id: str,
        run_id: str,
        expected_revision: int,
        idempotency_key: str,
        max_turns: int | None = None,
        team: str | None = None,
        role_selectors: Mapping[str, str] | None = None,
        unsafe_changes: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        """Apply one compare-and-swap safe control request to a run."""
        return _tool_result(
            lambda: _control_response(
                get_service().control(
                    project_id,
                    run_id,
                    RunControlRequest(
                        expected_revision=_validated_revision(expected_revision),
                        max_turns=max_turns,
                        team=team,
                        role_selectors=role_selectors or {},
                        unsafe_changes=unsafe_changes or {},
                    ),
                    idempotency_key=_bounded_idempotency_key(idempotency_key),
                    caller_scope="mcp",
                )
            ),
            {
                "project_id": project_id,
                "run_id": run_id,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
                "max_turns": max_turns,
                "team": team,
                "role_selectors": role_selectors or {},
                "unsafe_changes": unsafe_changes or {},
            },
        )

    @mcp.tool(
        title="Stop an AFlow run as owner",
        annotations=_OWNER_STOP_TOOL_ANNOTATIONS,
        tags={"write", "approval-required"},
    )
    def owner_stop(
        project_id: str,
        run_id: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Request a terminal owner stop for one run."""
        return _tool_result(
            lambda: (
                get_service()
                .owner_stop(
                    project_id,
                    run_id,
                    expected_revision=_validated_revision(expected_revision),
                    idempotency_key=_bounded_idempotency_key(idempotency_key),
                    caller_scope="mcp",
                )
                .to_dict()
            ),
            {
                "project_id": project_id,
                "run_id": run_id,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
            },
        )

    @mcp.tool(
        title="Resume an AFlow run",
        annotations=_WRITE_TOOL_ANNOTATIONS,
        tags={"write", "approval-required"},
    )
    def resume_run(
        project_id: str,
        run_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Create the explicit, lineage-linked continuation for a stopped run."""
        return _tool_result(
            lambda: (
                get_service()
                .resume(
                    project_id,
                    run_id,
                    idempotency_key=_bounded_idempotency_key(idempotency_key),
                    caller_scope="mcp",
                )
                .to_dict()
            ),
            {
                "project_id": project_id,
                "run_id": run_id,
                "idempotency_key": idempotency_key,
            },
        )

    @mcp.resource(
        "aflow://projects/{project_id}/capabilities",
        name="AFlow project capabilities",
        mime_type="application/json",
        tags={"read"},
        meta={"schema_version": 1},
    )
    def project_capabilities_resource(project_id: str) -> dict[str, Any]:
        """Expose canonical project capabilities at a stable resource URI."""
        return _resource_result(
            lambda: get_service().capabilities(project_id).to_dict(),
            {"project_id": project_id},
        )

    @mcp.resource(
        "aflow://projects/{project_id}/runs/{run_id}",
        name="AFlow run state",
        mime_type="application/json",
        tags={"read"},
        meta={"schema_version": 1},
    )
    def run_state_resource(project_id: str, run_id: str) -> dict[str, Any]:
        """Expose canonical run state at a stable resource URI."""
        return _resource_result(
            lambda: get_service().run_status(project_id, run_id).to_dict(),
            {"project_id": project_id, "run_id": run_id},
        )

    @mcp.resource(
        "aflow://projects/{project_id}/runs/{run_id}/context/lite",
        name="AFlow lite run context",
        mime_type="application/json",
        tags={"read"},
        meta={"schema_version": 1},
    )
    def lite_context_resource(project_id: str, run_id: str) -> dict[str, Any]:
        """Expose bounded lite context at a stable resource URI."""
        return _resource_result(
            lambda: (
                get_service()
                .context(project_id, run_id, level="lite", full_scope=False)
                .to_dict()
            ),
            {"project_id": project_id, "run_id": run_id},
        )

    return mcp


def _reject_after_sequence() -> int:
    raise ValueError("after sequence must be non-negative")


def _validated_full_scope(level: str, full_scope: bool) -> bool:
    if level == "full" and not full_scope:
        raise ValueError("full context scope is required")
    return full_scope


def _validated_revision(revision: int) -> int:
    if revision < 0:
        raise ValueError("expected revision must be non-negative")
    return revision


def _control_response(result: tuple[Any, Any]) -> dict[str, Any]:
    write_result, run = result
    return {
        "revision": write_result.revision,
        "changed": write_result.changed,
        "owner_stop": write_result.owner_stop,
        "run": run.to_dict(),
    }
