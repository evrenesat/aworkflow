"""Compatibility re-export of the shared control-plane MCP registry.

The tool registry now lives in ``aflow.mcp_control_plane`` so the same
surface backs the FastAPI `/mcp` mount and the ``aflow daemon`` listener.
This module keeps the app-specific error-code mapping and the historical
import path (``from .mcp_adapter import create_control_plane_mcp``).
"""

from __future__ import annotations

from collections.abc import Callable

from aflow.mcp_control_plane import (
    ControlPlaneServiceGetter,
    create_control_plane_mcp as _create_control_plane_mcp,
)

from .control_plane_service import (
    ControlPlaneService,
    ControlPlaneUnavailableError,
    ProjectNotAllowedError,
)

__all__ = ["ControlPlaneServiceGetter", "create_control_plane_mcp"]


def create_control_plane_mcp(
    get_service: Callable[[], ControlPlaneService],
) -> "object":
    """Create the stateless MCP registry with the app error-code mapping."""
    return _create_control_plane_mcp(
        get_service,
        extra_error_codes={
            ProjectNotAllowedError: "project_not_found",
            ControlPlaneUnavailableError: "control_plane_unavailable",
        },
    )
