"""Web-only MCP tools for global workflow configuration authoring."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP

from aflow.mcp_control_plane import MCPToolResult

from .config_response import config_response
from .global_config_service import GlobalConfigService
from .models import GlobalConfigPatchPayload


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

GlobalConfigServiceGetter = Callable[[], GlobalConfigService]


def register_global_config_tools(
    mcp: FastMCP,
    get_global_config_service: GlobalConfigServiceGetter,
    tool_result: MCPToolResult,
) -> None:
    """Register browser-parity tools for the one global workflow pair."""

    @mcp.tool(
        title="Read global AFlow configuration",
        annotations=_READ_ANNOTATIONS,
        tags={"read", "settings"},
    )
    def get_global_config() -> dict[str, Any]:
        """Read settings shared by all registered projects.

        The returned snapshot is diagnostic. A committed change is observed by
        existing runs at their next safe boundary or resume, according to the
        same live-configuration semantics as the browser settings page.
        """
        return tool_result(
            lambda: config_response(
                get_global_config_service().read()
            ).model_dump(mode="json"),
            {},
        )

    @mcp.tool(
        title="Patch global AFlow configuration",
        annotations=_NON_IDEMPOTENT_WRITE_ANNOTATIONS,
        tags={"write", "settings", "approval-required"},
    )
    def patch_global_config(payload: GlobalConfigPatchPayload) -> dict[str, Any]:
        """Apply one typed revisioned edit to settings shared by all projects.

        The payload accepts either an ordered guided action batch or an exact
        map of the two editable documents, never both. The service validates
        and commits the pair atomically; existing runs observe changes at the
        next safe boundary or resume, and stale revisions must be reread.
        """
        payload_arguments = payload.model_dump(mode="json")
        return tool_result(
            lambda: config_response(
                get_global_config_service().patch(payload, caller_scope="mcp")
            ).model_dump(mode="json"),
            {"payload": payload_arguments},
        )


__all__ = ["GlobalConfigServiceGetter", "register_global_config_tools"]
