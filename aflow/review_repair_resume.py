"""Recognize an interrupted repair created by a completed whole-plan review."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping


def pending_review_repair_step(
    *,
    run_dir: Path,
    prev_run: Mapping[str, object],
    workflow_steps: Mapping,
    repo_root: Path,
    plan_path: Path,
) -> str | None:
    """Bind the repair to the last review receipt without changing source state.

    The original checkpoint snapshot stays complete during a final-review
    repair. Its completion is not permission to deliver or to discard the
    review's overlay. Inactivity and lifecycle ownership remain the caller's
    admission checks; dirty repair edits are intentionally preserved.
    """
    from .plan import load_plan
    from .workflow import WorkflowError, _select_transition

    if prev_run.get("status") not in {"running", "failed", "owner_stopped"}:
        return None
    snapshot = prev_run.get("last_snapshot")
    if not isinstance(snapshot, Mapping) or snapshot.get("is_complete") is not True:
        return None
    if any(
        prev_run.get(key) is not None
        for key in (
            "merge_status",
            "completion_phase",
            "completion_receipt",
            "publication_receipt",
            "approved_commit",
            "active_implementation_scope",
            "pending_boundary_decision",
            "pending_repartition",
        )
    ):
        return None
    completed = prev_run.get("turns_completed")
    active = prev_run.get("active_turn")
    if type(completed) is not int or completed < 1 or type(active) is not int:
        return None
    if active not in {completed, completed + 1}:
        return None

    def read_receipt(number: int) -> Mapping:
        value = json.loads(
            (run_dir / "turns" / f"turn-{number:03d}" / "result.json").read_text()
        )
        if not isinstance(value, Mapping) or value.get("turn_number") != number:
            raise ValueError("invalid turn receipt")
        return value

    def identity(value: object) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("missing plan identity")
        path = Path(value)
        path = (path if path.is_absolute() else repo_root / path).resolve()
        path.relative_to(repo_root.resolve())
        return path

    try:
        original = identity(prev_run.get("original_plan_path"))
        overlay = identity(prev_run.get("active_plan_path"))
        if (
            original != plan_path.resolve()
            or overlay == original
            or overlay.parent != original.parent
        ):
            return None
        receipt = read_receipt(completed)
        if (
            receipt.get("status") != "completed"
            or type(receipt.get("returncode")) is not int
            or receipt["returncode"] != 0
        ):
            return None
        if receipt.get("snapshot_after") != snapshot:
            return None
        if (
            identity(receipt.get("original_plan_path")) != original
            or identity(receipt.get("new_plan_path")) != overlay
        ):
            return None
        conditions = receipt.get("conditions")
        if conditions != {
            "DONE": True,
            "NEW_PLAN_EXISTS": True,
            "MAX_TURNS_REACHED": False,
        } or any(type(v) is not bool for v in conditions.values()):
            return None
        review = workflow_steps.get(receipt.get("step_name"))
        if (
            review is None
            or review.role not in {"reviewer", "architect"}
            or receipt.get("step_role") != review.role
        ):
            return None
        selected = _select_transition(
            review.go,
            step_path="resume review repair",
            done=True,
            new_plan_exists=True,
            max_turns_reached=False,
        )
        step = selected.to
        repair = workflow_steps.get(step)
        if repair is None or repair.role not in {"worker", "senior_architect"}:
            return None
        if (
            receipt.get("chosen_transition") != step
            or receipt.get("chosen_transition_condition") != selected.when
            or prev_run.get("current_step_name") != step
        ):
            return None
        # A later finalized result supersedes this review. Only its single
        # unfinalized repair attempt may coexist with the review receipt.
        turns = run_dir / "turns"
        for child in turns.glob("turn-*"):
            number = int(child.name.removeprefix("turn-"))
            if number <= completed:
                continue
            if number != completed + 1:
                return None
            attempt = read_receipt(number)
            if (
                attempt.get("status") not in {"starting", "harness-failed"}
                or attempt.get("snapshot_after") is not None
                or attempt.get("step_name") != step
                or attempt.get("step_role") != repair.role
            ):
                return None
            if (
                identity(attempt.get("original_plan_path")) != original
                or identity(attempt.get("active_plan_path")) != overlay
            ):
                return None
        execution_root = repo_root
        if "worktree" in (prev_run.get("lifecycle_setup") or []):
            execution_root = Path(str(prev_run["worktree_path"]))
        owned_original = execution_root / original.relative_to(repo_root.resolve())
        owned_overlay = execution_root / overlay.relative_to(repo_root.resolve())
        if (
            not load_plan(owned_original).snapshot.is_complete
            or not owned_overlay.read_text(encoding="utf-8").strip()
        ):
            return None
    except (OSError, ValueError, KeyError, TypeError, WorkflowError):
        return None
    return step
