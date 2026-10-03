"""Shared strict validation for unfinished budget boundaries (issue #62).

This classifier is the single authority that decides whether a terminal run
left a safe, resumable budget boundary, and what the successor must start
from.  It is used by:

* explicit resume bootstrap (``aflow.cli._bootstrap_resume_invocation``),
* the daemon's read-only ``can_resume`` preview,
* managed launch revalidation (daemon ``resume`` admission).

Automatic candidate scanning never consults it, so the narrow historical
exception can never be picked up by an unrequested resume prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from aflow.plan import load_plan
from aflow.workflow import WorkflowError, evaluate_condition

#: Canonical clean-state preflight failure recorded when the merge teardown
#: refused to integrate over uncommitted work.
MERGE_CLEAN_STATE_PREFLIGHT_SIGNATURE = (
    "lifecycle teardown: merge handoff requires clean git state, but "
)

BudgetBoundaryKind = Literal["budget_exit", "historical_merge_failure"]


@dataclass(frozen=True)
class BudgetBoundary:
    """Validated evidence for one unfinished budget boundary.

    The descriptor is carried in the successor's resume context and keeps the
    source run immutable: it records what the source proved, never what the
    successor is allowed to change.
    """

    kind: BudgetBoundaryKind
    source_run_dir: Path
    turn_number: int
    source_step_name: str
    source_step_role: str
    source_selector: str
    done: bool
    new_plan_exists: bool
    saved_end_condition: str | None
    next_step_name: str
    original_plan_path: Path
    active_plan_path: Path
    new_plan_path: Path
    overlay_path: Path | None
    saved_max_turns: int
    effective_max_turns: int
    feature_branch: str
    main_branch: str
    worktree_path: Path


def _strict_positive_int(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 1
    )


def _strict_zero_int(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value == 0
    )


def _load_receipt(run_dir: Path, turn_number: int) -> Mapping[str, object] | None:
    receipt_path = run_dir / "turns" / f"turn-{turn_number:03d}" / "result.json"
    try:
        raw = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, Mapping) else None


def _is_clean_state_preflight_failure(reason: Any) -> bool:
    return (
        isinstance(reason, str)
        and reason.startswith(MERGE_CLEAN_STATE_PREFLIGHT_SIGNATURE)
    )


#: Persisted evidence that a supported selector control or recovery override
#: may have changed a role's effective selector during the run.  When any of
#: these fields is active, earlier receipts are not reliable selector evidence
#: for the finalized turn, so only the durable selector controls bind it.
_SELECTOR_CONTROL_FIELDS = (
    "current_hotplug_transaction",
    "pending_hotplug_transaction",
    "pending_step_team_override",
)
_SELECTOR_CONTROL_HISTORY_FIELDS = (
    "hotplug_history",
    "manager_history",
    "recovery_history",
)


def _selector_control_activity(prev_run: Mapping[str, object]) -> bool:
    return (
        any(prev_run.get(field) is not None for field in _SELECTOR_CONTROL_FIELDS)
        or any(
            isinstance(prev_run.get(field), list) and bool(prev_run.get(field))
            for field in _SELECTOR_CONTROL_HISTORY_FIELDS
        )
        or prev_run.get("recovery_summary") is not None
    )


def _saved_role_selectors(
    prev_run: Mapping[str, object], role: str
) -> list[str]:
    """Collect the saved durable selector evidence for one role.

    The persisted ``role_selectors`` control and active role sessions are the
    authoritative saved selector evidence; configuration defaults are never
    consulted (matching the daemon's source selector policy).
    """
    evidence: list[str] = []
    selectors = prev_run.get("role_selectors")
    if isinstance(selectors, Mapping):
        saved = selectors.get(role)
        if isinstance(saved, str) and saved:
            evidence.append(saved)
    sessions = prev_run.get("active_role_sessions")
    if isinstance(sessions, list):
        for session in sessions:
            if (
                isinstance(session, Mapping)
                and session.get("role") == role
                and session.get("status") == "active"
                and isinstance(session.get("selector"), str)
                and session["selector"]
            ):
                evidence.append(session["selector"])
    return evidence


def _worktree_mirror(
    primary_path: Path, worktree_value: str, repo_root: Path
) -> Path | None:
    """Map a recorded primary-checkout path into the recorded worktree.

    Receipts record plan paths relative to the primary checkout while the
    working files live in the run's worktree, so existence checks must look
    at both locations.
    """
    if not worktree_value:
        return None
    try:
        rel = primary_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return None
    return Path(worktree_value) / rel


def classify_budget_boundary(
    *,
    run_dir: Path,
    prev_run: Mapping[str, object],
    workflow_config: Any,
    repo_root: Path,
) -> BudgetBoundary | None:
    """Return the validated boundary, or None when the run is not one.

    Only these two shapes are admitted:

    * ``budget_exit``: a new no-delivery budget exit recorded with
      ``status=completed`` and ``end_reason=max_turns_reached`` (checkpoint
      one's delivery gate).
    * ``historical_merge_failure``: the one narrow historical shape
      (``status=failed``, incomplete strict original snapshot,
      ``end_reason=max_turns_reached``, ``merge_status=failed`` with the
      canonical clean-state preflight signature, and an intact recorded
      execution branch/worktree).

    Every other terminal shape returns None and is handled by the existing
    completed/failed policies.
    """

    if prev_run.get("end_reason") != "max_turns_reached":
        return None
    # Completion/publication receipts are out of scope for this boundary.
    if prev_run.get("completion_phase") is not None:
        return None
    if prev_run.get("failure_kind") is not None:
        return None
    status = prev_run.get("status")
    merge_status = prev_run.get("merge_status")
    if status == "completed":
        if merge_status is not None:
            return None
        kind: BudgetBoundaryKind = "budget_exit"
    elif status == "failed":
        if (
            merge_status != "failed"
            or "merge" not in (prev_run.get("lifecycle_teardown") or [])
            or not _is_clean_state_preflight_failure(
                prev_run.get("merge_failure_reason")
            )
        ):
            return None
        kind = "historical_merge_failure"
    else:
        return None

    # The historical shape must keep an intact recorded execution identity.
    if kind == "historical_merge_failure":
        feature_branch = prev_run.get("feature_branch")
        main_branch = prev_run.get("main_branch")
        if (
            not isinstance(feature_branch, str)
            or not feature_branch
            or not isinstance(main_branch, str)
            or not main_branch
        ):
            return None
        worktree_value = prev_run.get("worktree_path")
        if (
            isinstance(worktree_value, str)
            and worktree_value
            and not Path(worktree_value).is_dir()
        ):
            return None
    else:
        feature_branch = (
            prev_run.get("feature_branch")
            if isinstance(prev_run.get("feature_branch"), str)
            else ""
        )
        main_branch = (
            prev_run.get("main_branch")
            if isinstance(prev_run.get("main_branch"), str)
            else ""
        )
        worktree_value = (
            prev_run.get("worktree_path")
            if isinstance(prev_run.get("worktree_path"), str)
            else ""
        )
        if (
            "worktree" in (prev_run.get("lifecycle_setup") or [])
            and not (isinstance(worktree_value, str) and Path(worktree_value).is_dir())
        ):
            return None

    workflow_name = prev_run.get("workflow_name")
    if not isinstance(workflow_name, str) or not workflow_name:
        return None
    workflows = getattr(workflow_config, "workflows", None)
    if not isinstance(workflows, Mapping) or workflow_name not in workflows:
        return None
    wf = workflows[workflow_name]
    steps = getattr(wf, "steps", None)
    if not isinstance(steps, Mapping) or not steps:
        return None

    turns_completed = prev_run.get("turns_completed")
    if not _strict_positive_int(turns_completed):
        return None
    # No in-flight or later turn: the last receipt is the last evidence.
    # ``active_turn`` must be a strict positive integer when present; the
    # ``True == 1`` numeric equality must not admit a boolean impostor.
    active_turn = prev_run.get("active_turn")
    if active_turn is not None:
        if not _strict_positive_int(active_turn) or active_turn != turns_completed:
            return None
    if (run_dir / "turns" / f"turn-{turns_completed + 1:03d}").exists():
        return None

    last_snapshot = prev_run.get("last_snapshot")
    if (
        not isinstance(last_snapshot, Mapping)
        or not isinstance(last_snapshot.get("is_complete"), bool)
    ):
        return None

    receipt = _load_receipt(run_dir, turns_completed)
    if receipt is None:
        return None
    receipt_turn = receipt.get("turn_number")
    if (
        not _strict_positive_int(receipt_turn)
        or receipt_turn != turns_completed
        or receipt.get("status") != "completed"
        or not _strict_zero_int(receipt.get("returncode"))
    ):
        return None
    step_name = receipt.get("step_name")
    step_role = receipt.get("step_role")
    selector = receipt.get("selector")
    active_plan_value = receipt.get("active_plan_path")
    new_plan_value = receipt.get("new_plan_path")
    chosen = receipt.get("chosen_transition")
    if not all(
        isinstance(value, str) and value
        for value in (
            step_name,
            step_role,
            selector,
            active_plan_value,
            new_plan_value,
            chosen,
        )
    ):
        return None
    source_step = steps.get(step_name)
    if source_step is None:
        return None
    # The exact finalized turn must name the current configured role of the
    # saved source step; a receipt claiming another role contradicts the
    # canonical graph and is rejected before any successor decision.
    if step_role != getattr(source_step, "role", None):
        return None
    # Bind the receipt selector to the saved selector evidence: durable
    # selector controls bind it directly, and without any control activity
    # the earlier receipts of the same step are the saved selector evidence.
    if any(saved != selector for saved in _saved_role_selectors(prev_run, step_role)):
        return None
    if not _selector_control_activity(prev_run):
        for earlier_turn in range(1, turns_completed):
            earlier_receipt = _load_receipt(run_dir, earlier_turn)
            if earlier_receipt is None:
                return None
            if (
                earlier_receipt.get("step_name") != step_name
                or earlier_receipt.get("step_role") != step_role
            ):
                continue
            earlier_selector = earlier_receipt.get("selector")
            if not isinstance(earlier_selector, str) or not earlier_selector:
                return None
            if earlier_selector != selector:
                return None
    conditions = receipt.get("conditions")
    if not isinstance(conditions, Mapping):
        return None
    done = conditions.get("DONE")
    new_plan_exists = conditions.get("NEW_PLAN_EXISTS")
    max_turns_reached = conditions.get("MAX_TURNS_REACHED")
    if not all(
        isinstance(value, bool) for value in (done, new_plan_exists, max_turns_reached)
    ):
        return None
    # The receipt must agree with the run's final snapshot, and DONE is the
    # saved snapshot's completeness, not an independently trusted boolean.
    if receipt.get("snapshot_after") != last_snapshot:
        return None
    if done != last_snapshot.get("is_complete"):
        return None
    saved_max_turns = prev_run.get("max_turns")
    effective_max_turns = prev_run.get("effective_max_turns")
    if (
        not _strict_positive_int(saved_max_turns)
        or not _strict_positive_int(effective_max_turns)
    ):
        return None
    # MAX_TURNS_REACHED must be the authoritative effective limit reached by
    # the finalized turn, not a free-standing receipt claim.
    if max_turns_reached != (turns_completed >= effective_max_turns):
        return None

    transitions = tuple(getattr(source_step, "go", ()) or ())

    def _evaluate(expression: str, max_flag: bool) -> bool:
        try:
            return evaluate_condition(
                expression,
                done=done,
                new_plan_exists=new_plan_exists,
                max_turns_reached=max_flag,
            )
        except WorkflowError:
            return False

    saved_condition = receipt.get("chosen_transition_condition")
    if not isinstance(saved_condition, str):
        saved_condition = None
    if chosen == "END":
        if not saved_condition:
            return None
        matching = [t for t in transitions if t.when == saved_condition]
        if len(matching) != 1 or matching[0].to != "END":
            return None
        # The saved END edge must have been selected under the saved
        # conditions, and only because the budget was exhausted.
        if not _evaluate(saved_condition, True) or _evaluate(saved_condition, False):
            return None
        next_targets = [
            t.to
            for t in transitions
            if t is not matching[0]
            and t.when is not None
            and _evaluate(t.when, False)
            and t.to != "END"
        ]
        if len(next_targets) != 1:
            return None
        next_step_name = next_targets[0]
    else:
        next_step_name = chosen
        saved_matches = [t for t in transitions if t.to == next_step_name]
        if not saved_matches:
            return None
        if saved_condition is not None:
            if not any(t.when == saved_condition for t in saved_matches):
                return None
            if not _evaluate(saved_condition, max_turns_reached):
                return None
        else:
            if not any(t.when is None for t in saved_matches):
                return None
    target_step = steps.get(next_step_name)
    if target_step is None:
        return None

    original_plan_value = prev_run.get("original_plan_path")
    if not isinstance(original_plan_value, str) or not original_plan_value:
        return None
    original_plan_path = Path(original_plan_value).expanduser()
    if not original_plan_path.is_absolute():
        original_plan_path = repo_root / original_plan_path
    active_plan_path = Path(active_plan_value)
    new_plan_path = Path(new_plan_value)

    if kind == "historical_merge_failure":
        # The historical exception is bound to an incomplete strict *original*
        # plan, proven consistently by the canonical saved snapshot and the
        # validated on-disk original plan (the recorded path is
        # primary-checkout relative, so the worktree mirror is accepted).  A
        # complete original with a historical repair overlay (issue #58) is a
        # distinct final-review repair shape, not this boundary, and no
        # completion is inferred from filenames or repair credit.
        if last_snapshot.get("is_complete") is not False:
            return None
        # The terminal record must name the exact finalized source step.
        if prev_run.get("current_step_name") != step_name:
            return None
        mirror = _worktree_mirror(original_plan_path, worktree_value, repo_root)
        if worktree_value and mirror is not None:
            # The run executed in the recorded worktree, so its original plan
            # file is the on-disk identity the saved snapshot must match.
            original_on_disk = mirror
        else:
            original_on_disk = original_plan_path
        if not original_on_disk.is_file():
            return None
        try:
            disk_snapshot = load_plan(original_on_disk).snapshot
        except (OSError, ValueError):
            return None
        if disk_snapshot.is_complete is not False:
            return None
        if (
            disk_snapshot.total_checkpoint_count
            != last_snapshot.get("total_checkpoint_count")
            or disk_snapshot.current_checkpoint_name
            != last_snapshot.get("current_checkpoint_name")
        ):
            return None

    # NEW_PLAN_EXISTS=true binds the existing owned repair file to the
    # recorded new path; that overlay is the successor's active plan.  The
    # recorded path is primary-checkout relative, so accept the file in the
    # worktree mirror as well.
    overlay_path: Path | None = None
    if new_plan_exists:
        mirror = _worktree_mirror(new_plan_path, worktree_value, repo_root)
        if not new_plan_path.is_file() and not (
            mirror is not None and mirror.is_file()
        ):
            return None
        overlay_path = new_plan_path

    return BudgetBoundary(
        kind=kind,
        source_run_dir=run_dir,
        turn_number=turns_completed,
        source_step_name=step_name,
        source_step_role=step_role,
        source_selector=selector,
        done=done,
        new_plan_exists=new_plan_exists,
        saved_end_condition=saved_condition if chosen == "END" else None,
        next_step_name=next_step_name,
        original_plan_path=original_plan_path,
        active_plan_path=active_plan_path,
        new_plan_path=new_plan_path,
        overlay_path=overlay_path,
        saved_max_turns=saved_max_turns,
        effective_max_turns=effective_max_turns,
        feature_branch=feature_branch,
        main_branch=main_branch,
        worktree_path=Path(worktree_value) if worktree_value else run_dir,
    )


def resolve_successor_max_turns(
    *,
    accepted_override_max_turns: int | None,
    invocation_max_turns: int | None,
    live_default_max_turns: int | None,
) -> int:
    """Resolve the successor limit with the existing precedence.

    Priority: an accepted run max override, the explicit invocation limit
    (including the saved source invocation limit), then the current live
    default.  The historical shape inherits its accepted effective limit
    (4), not the saved invocation value (48).
    """
    for candidate in (
        accepted_override_max_turns,
        invocation_max_turns,
        live_default_max_turns,
    ):
        if _strict_positive_int(candidate):
            return candidate
    raise ValueError("no validated successor budget is available")
