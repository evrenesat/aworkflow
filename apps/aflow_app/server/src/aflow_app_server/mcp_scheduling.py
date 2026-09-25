"""MCP views of the shared project scheduling service."""

from __future__ import annotations

from collections.abc import Callable

from fastmcp import FastMCP

from aflow.mcp_control_plane import MCPToolResult

from .models import ProjectSchedulingPatchPayload
from .scheduling_service import SchedulingService


SchedulingServiceGetter = Callable[[], SchedulingService]

_READ = {
    "readOnlyHint": True, "destructiveHint": False,
    "idempotentHint": True, "openWorldHint": False,
}
_WRITE = {
    "readOnlyHint": False, "destructiveHint": False,
    "idempotentHint": False, "openWorldHint": False,
}


def register_scheduling_tools(
    mcp: FastMCP, get_service: SchedulingServiceGetter,
    tool_result: MCPToolResult,
) -> None:
    @mcp.tool(title="Read project scheduling", annotations=_READ, tags={"read", "settings"})
    def get_project_scheduling(project_id: str) -> dict[str, object]:
        """Read the project's effective settings and exact save revision."""
        return tool_result(
            lambda: get_service().read(project_id), {"project_id": project_id},
        )

    @mcp.tool(title="Patch project scheduling", annotations=_WRITE,
              tags={"write", "settings", "approval-required"})
    def patch_project_scheduling(
        project_id: str, payload: ProjectSchedulingPatchPayload,
    ) -> dict[str, object]:
        """Compare-and-swap either or both project scheduling settings."""
        return tool_result(
            lambda: get_service().update(
                project_id, expected_revision=payload.expected_revision,
                changes=payload.changes(),
            ),
            {"project_id": project_id, "payload": payload.model_dump(mode="json")},
        )

    @mcp.tool(title="Read project plan queue", annotations=_READ, tags={"read", "plans"})
    def get_project_queue(project_id: str) -> dict[str, object]:
        """Read plan outcomes, reasons, claims, dependencies and capacity."""
        return tool_result(
            lambda: get_service().queue(project_id), {"project_id": project_id},
        )
