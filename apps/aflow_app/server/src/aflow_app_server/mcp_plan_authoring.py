"""Web-only MCP tools for revisioned Markdown plan authoring."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP

from aflow.mcp_control_plane import MCPToolResult

from .plan_service import PlanService, PlanServiceError, PlanStatus


_READ_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_NON_IDEMPOTENT_WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}

PlanServiceGetter = Callable[[], PlanService]
ControlPlaneServiceGetter = Callable[[], Any]


def register_plan_authoring_tools(
    mcp: FastMCP,
    get_plan_service: PlanServiceGetter,
    tool_result: MCPToolResult,
    get_control_plane_service: ControlPlaneServiceGetter | None = None,
) -> None:
    """Register browser-parity plan tools on the web MCP registry only."""

    @mcp.tool(
        title="Read an AFlow plan document",
        annotations=_READ_ANNOTATIONS,
        tags={"read", "plans"},
    )
    def read_plan(project_id: str, plan_status: PlanStatus, name: str) -> dict[str, object]:
        """Read one revisioned Markdown plan document from an allowlisted project."""
        return tool_result(
            lambda: get_plan_service().read(project_id, plan_status, name).to_dict(),
            {"project_id": project_id, "plan_status": plan_status, "name": name},
        )

    @mcp.tool(
        title="Create an AFlow plan draft",
        annotations=_NON_IDEMPOTENT_WRITE_ANNOTATIONS,
        tags={"write", "plans", "approval-required"},
    )
    def create_plan(
        project_id: str,
        name: str,
        content: str | None = None,
    ) -> dict[str, object]:
        """Create one todo plan; null content uses the browser draft template."""
        return tool_result(
            lambda: get_plan_service().create(project_id, name, content).to_dict(),
            {"project_id": project_id, "name": name, "content": content},
        )

    @mcp.tool(
        title="Create an AFlow follow-up draft from run evidence",
        annotations=_NON_IDEMPOTENT_WRITE_ANNOTATIONS,
        tags={"write", "plans", "approval-required"},
    )
    def create_plan_from_run(
        project_id: str,
        run_id: str,
        name: str | None = None,
    ) -> dict[str, object]:
        """Create an editable todo draft from a failed or attention-needed run."""
        def create() -> dict[str, object]:
            if get_control_plane_service is None:
                raise PlanServiceError("control-plane service is unavailable")
            control_plane = get_control_plane_service()
            return get_plan_service().create_plan_from_run(
                project_id,
                run_id,
                name,
                run_status_reader=control_plane.run_status,
                run_context_reader=control_plane.context,
            ).to_dict()

        return tool_result(
            create,
            {"project_id": project_id, "run_id": run_id, "name": name},
        )

    @mcp.tool(
        title="Update an AFlow plan document",
        annotations=_NON_IDEMPOTENT_WRITE_ANNOTATIONS,
        tags={"write", "plans", "approval-required"},
    )
    def update_plan(
        project_id: str,
        plan_status: PlanStatus,
        name: str,
        content: str,
        expected_revision: str,
    ) -> dict[str, object]:
        """Compare-and-swap one plan document and return its new revision."""
        return tool_result(
            lambda: get_plan_service()
            .update(project_id, plan_status, name, content, expected_revision)
            .to_dict(),
            {
                "project_id": project_id,
                "plan_status": plan_status,
                "name": name,
                "content": content,
                "expected_revision": expected_revision,
            },
        )

    @mcp.tool(
        title="Promote an AFlow plan document",
        annotations=_NON_IDEMPOTENT_WRITE_ANNOTATIONS,
        tags={"write", "plans", "approval-required"},
    )
    def promote_plan(
        project_id: str,
        plan_status: PlanStatus,
        name: str,
        expected_revision: str,
        target_name: str | None = None,
    ) -> dict[str, object]:
        """Move a plan to the next lifecycle directory with revision checking."""
        return tool_result(
            lambda: get_plan_service()
            .promote(project_id, plan_status, name, expected_revision, target_name)
            .to_dict(),
            {
                "project_id": project_id,
                "plan_status": plan_status,
                "name": name,
                "expected_revision": expected_revision,
                "target_name": target_name,
            },
        )

    @mcp.tool(
        title="List AFlow plan documents",
        annotations=_READ_ANNOTATIONS,
        tags={"read", "plans"},
    )
    def list_plan_documents(
        project_id: str,
        plan_status: PlanStatus | None = None,
    ) -> dict[str, object]:
        """List browser-compatible plan documents with status and revisions.

        Unlike ``list_plans``, which exposes lifecycle metadata for run
        clients, this tool lists the revisioned Markdown documents managed by
        the browser plan editor. Use ``read_plan`` to retrieve content.
        """
        return tool_result(
            lambda: {
                "plans": [
                    document.to_dict()
                    for document in get_plan_service().list(project_id, plan_status)
                ]
            },
            {"project_id": project_id, "plan_status": plan_status},
        )


__all__ = [
    "ControlPlaneServiceGetter",
    "PlanServiceGetter",
    "register_plan_authoring_tools",
]
