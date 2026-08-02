"""API models for the remote app server."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class PlanStatus(str, Enum):
    """Status of a plan file."""

    DRAFT = "draft"
    IN_PROGRESS = "in_progress"


@dataclass
class RepoInfo:
    """Information about a registered repository."""

    id: str
    name: str
    path: Path
    is_git_root: bool
    registered_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": str(self.path),
            "is_git_root": self.is_git_root,
            "registered_at": self.registered_at.isoformat(),
        }


@dataclass(frozen=True)
class ProjectInfo:
    """Information about a discovered project."""

    id: str
    display_name: str
    current_path: Path
    historical_aliases: tuple[Path, ...]
    detection_source: str
    linked_thread_count: int
    is_git_root: bool
    registered_at: datetime

    @property
    def name(self) -> str:
        """Backward-compatible alias for the display name."""
        return self.display_name

    @property
    def path(self) -> Path:
        """Backward-compatible alias for the current path."""
        return self.current_path

    def to_dict(self) -> dict[str, Any]:
        aliases = [str(alias) for alias in self.historical_aliases]
        payload = {
            "id": self.id,
            "display_name": self.display_name,
            "current_path": str(self.current_path),
            "historical_aliases": aliases,
            "detection_source": self.detection_source,
            "linked_thread_count": self.linked_thread_count,
            "is_git_root": self.is_git_root,
            "registered_at": self.registered_at.isoformat(),
        }
        payload.update(
            {
                "name": self.display_name,
                "path": str(self.current_path),
                "aliases": aliases,
            }
        )
        return payload


@dataclass
class PlanInfo:
    """Information about a plan file."""

    name: str
    path: Path
    status: PlanStatus
    checkpoint_count: int
    unchecked_count: int
    is_complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "status": self.status.value,
            "checkpoint_count": self.checkpoint_count,
            "unchecked_count": self.unchecked_count,
            "is_complete": self.is_complete,
        }


@dataclass
class ExecutionRequest:
    """Request to start a workflow execution."""

    project_id: str
    plan_path: str
    workflow_name: str | None = None
    team: str | None = None
    start_step: str | None = None
    max_turns: int | None = None
    extra_instructions: str | None = None


@dataclass
class ExecutionStatus:
    """Status of a workflow execution."""

    run_id: str
    project_id: str
    plan_path: str
    workflow_name: str | None
    status: str
    turns_completed: int
    current_step: str | None
    started_at: datetime
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project_id": self.project_id,
            "plan_path": self.plan_path,
            "workflow_name": self.workflow_name,
            "status": self.status,
            "turns_completed": self.turns_completed,
            "current_step": self.current_step,
            "started_at": self.started_at.isoformat(),
            "error": self.error,
        }
