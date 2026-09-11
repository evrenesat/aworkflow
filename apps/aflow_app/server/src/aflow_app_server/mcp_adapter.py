"""FastAPI adapter for the shared UI-server MCP registry.

The tool registry lives in ``aflow.mcp_control_plane`` and is mounted by the
UI server at both ``/mcp`` and ``/mcp/``. This module keeps the app-specific
error-code mapping and the stable import path
(``from .mcp_adapter import create_control_plane_mcp``).
"""

from __future__ import annotations

from collections.abc import Callable

from fastmcp import FastMCP

from aflow.mcp_control_plane import (
    ControlPlaneServiceGetter,
    MCPToolResult,
    MCPToolRegistrar,
    create_control_plane_mcp as _create_control_plane_mcp,
)

from .control_plane_service import (
    ControlPlaneService,
    ControlPlaneUnavailableError,
    ProjectNotAllowedError,
)
from .guided_config import GuidedConfigError
from .mcp_config_authoring import (
    GlobalConfigServiceGetter,
    register_global_config_tools,
)
from .mcp_plan_authoring import (
    PlanServiceGetter,
    register_plan_authoring_tools,
)
from .plan_service import (
    PlanAlreadyExists,
    PlanInvalid,
    PlanNotFound,
    PlanProjectNotFound,
    PlanRevisionConflict,
    PlanServiceError,
)
from .project_config_service import ProjectConfigError, ProjectConfigRevisionConflict

__all__ = [
    "ControlPlaneServiceGetter",
    "GlobalConfigServiceGetter",
    "PlanServiceGetter",
    "create_control_plane_mcp",
]


def create_control_plane_mcp(
    get_service: Callable[[], ControlPlaneService],
    *,
    get_plan_service: PlanServiceGetter | None = None,
    get_global_config_service: GlobalConfigServiceGetter | None = None,
) -> FastMCP:
    """Create the stateless web MCP registry with app-specific composition.

    Without the optional getters this retains the reusable lifecycle-only
    registry contract. The UI server supplies the getters to compose the
    browser-compatible authoring tools without importing the web package into
    the transport-neutral registry.
    """
    registrar: MCPToolRegistrar | None = None
    if get_plan_service is not None or get_global_config_service is not None:

        def register_web_tools(mcp: FastMCP, tool_result: MCPToolResult) -> None:
            if get_plan_service is not None:
                register_plan_authoring_tools(
                    mcp,
                    get_plan_service,
                    tool_result,
                    get_service,
                )
            if get_global_config_service is not None:
                register_global_config_tools(
                    mcp, get_global_config_service, tool_result
                )

        registrar = register_web_tools
    return _create_control_plane_mcp(
        get_service,
        extra_error_codes={
            ProjectNotAllowedError: "project_not_found",
            ControlPlaneUnavailableError: "control_plane_unavailable",
            PlanProjectNotFound: "project_not_found",
            PlanRevisionConflict: "revision_conflict",
            PlanNotFound: "plan_not_found",
            PlanInvalid: "invalid_plan",
            PlanAlreadyExists: "plan_exists",
            PlanServiceError: "operation_rejected",
            ProjectConfigRevisionConflict: "revision_conflict",
            GuidedConfigError: "operation_rejected",
            ProjectConfigError: "operation_rejected",
        },
        register_tools=registrar,
    )
