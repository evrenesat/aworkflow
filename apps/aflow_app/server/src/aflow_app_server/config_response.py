"""Shared response conversion for the global configuration transports."""

from __future__ import annotations

from .models import (
    ConfigValidationIssueModel,
    ConfigValidationModel,
    ProjectConfigResponse,
)
from .project_config_service import ConfigValidationReport, ProjectConfigSnapshot


def config_validation_response(report: ConfigValidationReport) -> ConfigValidationModel:
    """Convert the service validation report to the canonical API model."""
    return ConfigValidationModel(
        state=report.state,  # type: ignore[arg-type]
        issues=tuple(
            ConfigValidationIssueModel(
                document=issue.document,
                line=issue.line,
                message=issue.message,
            )
            for issue in report.issues
        ),
        placeholders=report.placeholders,
        workflows=report.workflows,
        teams=report.teams,
        roles=report.roles,
    )


def config_response(snapshot: ProjectConfigSnapshot) -> ProjectConfigResponse:
    """Convert a global configuration snapshot to the REST/MCP response."""
    return ProjectConfigResponse(
        project_id=snapshot.project_id,
        revision=snapshot.revision,
        documents=snapshot.documents,
        aflow_toml=snapshot.aflow_toml,
        workflows_toml=snapshot.workflows_toml,
        validation=config_validation_response(snapshot.validation),
    )


__all__ = ["config_response", "config_validation_response"]
