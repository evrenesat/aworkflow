"""Compatibility reads for the project and planning APIs."""

from __future__ import annotations

from pathlib import Path

from aflow.plan import load_plan

from .models import PlanInfo, PlanStatus


class AflowServiceError(Exception):
    """Error in the compatibility read adapter."""


class AflowService:
    """Expose legacy plan metadata without becoming a workflow owner.

    Lifecycle operations were deliberately moved to ``ControlPlaneService``.
    It composes daemon-owned services and is the only REST execution path.
    """

    def list_plans(self, project_path: Path) -> list[PlanInfo]:
        """List legacy planning metadata for the pre-existing application UI."""
        plans: list[PlanInfo] = []
        for directory, status in (
            (project_path / "plans" / "drafts", PlanStatus.DRAFT),
            (project_path / "plans" / "in-progress", PlanStatus.IN_PROGRESS),
        ):
            if not directory.exists():
                continue
            for plan_file in directory.glob("*.md"):
                info = self._get_plan_info(plan_file, status)
                if info is not None:
                    plans.append(info)
        return sorted(plans, key=lambda item: item.name)

    @staticmethod
    def _get_plan_info(plan_path: Path, status: PlanStatus) -> PlanInfo | None:
        try:
            snapshot = load_plan(plan_path).snapshot
        except Exception:
            return None
        return PlanInfo(
            name=plan_path.stem,
            path=plan_path,
            status=status,
            checkpoint_count=snapshot.total_checkpoint_count,
            unchecked_count=snapshot.unchecked_checkpoint_count,
            is_complete=snapshot.is_complete,
        )
