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
from aflow.runlog import load_run_json
from aflow.workflow import (
    WorkflowError,
    _select_transition,
    _terminal_resume_run_dir,
    evaluate_condition,
)

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


def _plan_identity(value: str, repo_root: Path) -> Path | None:
    """Normalize a recorded plan path to an absolute identity inside the repo.

    Relative paths are resolved against the primary checkout root, matching the
    receipt/run.json recording convention; a path that escapes the repository
    is unowned evidence and returns None.
    """
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repo_root / value
    try:
        path.resolve().relative_to(repo_root.resolve())
    except (OSError, ValueError):
        return None
    return path


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


def _post_transition_active_plan(
    *,
    original_plan_path: Path,
    active_plan_path: Path,
    new_plan_path: Path,
    new_plan_exists: bool,
    transition: Any,
) -> Path:
    """Mirror the controller's next-active selection for one finalized turn.

    After a turn the live controller selects the successor's active plan
    (``_select_next_active_plan_path``): the new overlay when NEW_PLAN_EXISTS
    was true, the same active plan when the selected transition preserves it,
    and the original plan otherwise.  A completed turn implies the live
    execution-checkout existence check passed, so the pure selection is exact.
    """
    if new_plan_exists:
        return new_plan_path
    if getattr(transition, "preserve_active_plan", False):
        return active_plan_path
    return original_plan_path


def _validated_budget_end_edge(
    *,
    step_name: object,
    saved_condition: object,
    done: bool,
    new_plan_exists: bool,
    steps: Mapping[str, Any],
) -> Any | None:
    """Validate one finalized receipt's recorded budget-driven END edge.

    Returns the exact ordered END transition when the recorded condition
    identifies exactly one END edge of the named step, that edge was selected
    only because the budget was exhausted (it stops matching once the budget
    is restored), and it is the edge the live controller's ordered
    first-match routing selects under the saved budget-exhausted conditions.
    Any other recorded END — missing or ambiguous condition evidence, a
    DONE-driven or unconditional END, or a contradiction with the configured
    routing — returns None.
    """
    if not isinstance(step_name, str) or not step_name:
        return None
    if not isinstance(saved_condition, str) or not saved_condition:
        return None
    step = steps.get(step_name)
    if step is None:
        return None
    transitions = tuple(getattr(step, "go", ()) or ())
    matching = [t for t in transitions if t.when == saved_condition]
    if len(matching) != 1 or matching[0].to != "END":
        return None

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

    # The saved END edge must have been selected under the saved
    # conditions, and only because the budget was exhausted.
    if not _evaluate(saved_condition, True) or _evaluate(saved_condition, False):
        return None
    # The saved END must be the edge the live controller would actually
    # select under the saved (budget-exhausted) conditions, using the same
    # ordered first-match routing (unconditional fallbacks included), not
    # an unordered scan of every matching conditional target.
    try:
        selected_end = _select_transition(
            transitions,
            step_path=step_name,
            done=done,
            new_plan_exists=new_plan_exists,
            max_turns_reached=True,
        )
    except WorkflowError:
        return None
    if selected_end.to != "END" or selected_end.when != saved_condition:
        return None
    return selected_end


def _receipt_plan_identities(
    receipt: Mapping[str, object], repo_root: Path
) -> tuple[Path, Path, bool] | None:
    """Strictly type one finalized receipt's plan identities.

    Returns ``(active, new, new_exists)`` as normalized owned identities, or
    None when the receipt's plan evidence is missing or mistyped.
    """
    conditions = receipt.get("conditions")
    if not isinstance(conditions, Mapping):
        return None
    new_exists = conditions.get("NEW_PLAN_EXISTS")
    if not isinstance(new_exists, bool):
        return None
    active_value = receipt.get("active_plan_path")
    new_value = receipt.get("new_plan_path")
    if not all(
        isinstance(value, str) and value for value in (active_value, new_value)
    ):
        return None
    active = _plan_identity(active_value, repo_root)
    new = _plan_identity(new_value, repo_root)
    if active is None or new is None:
        return None
    return active, new, new_exists


def _workspace_identity(
    run: Mapping[str, object],
) -> tuple[str | None, str | None]:
    """The recorded execution workspace: worktree and feature branch."""
    def _text(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    return (_text(run.get("worktree_path")), _text(run.get("feature_branch")))


def _derived_next_start_identity(
    predecessor: Mapping[str, object],
    *,
    steps: Mapping[str, Any],
    repo_root: Path,
    canonical_original_path: Path,
) -> Path | None:
    """Derive the next turn's starting active identity from one receipt.

    The predecessor receipt records the exact edge its ordered routing
    selected (target plus recorded condition) and its own saved conditions,
    so the controller's post-transition selection is recomputable exactly
    against the canonical original.  Contradictory or missing predecessor
    evidence returns None instead of guessing a known path.
    """
    pred_step_name = predecessor.get("step_name")
    pred_chosen = predecessor.get("chosen_transition")
    pred_condition = predecessor.get("chosen_transition_condition")
    if not isinstance(pred_step_name, str) or not pred_step_name:
        return None
    if pred_condition is not None and not isinstance(pred_condition, str):
        return None
    identities = _receipt_plan_identities(predecessor, repo_root)
    if identities is None:
        return None
    pred_active, pred_new, pred_new_exists = identities
    if pred_chosen is None:
        # A retry-scheduled turn replayed with the same active plan and
        # never selected a transition.
        if predecessor.get("retry_next_turn") is not True:
            return None
        return pred_active
    if not isinstance(pred_chosen, str) or pred_chosen == "END":
        # An END selection would have ended the run before the next turn.
        return None
    pred_step = steps.get(pred_step_name)
    if pred_step is None:
        return None
    pred_transitions = tuple(getattr(pred_step, "go", ()) or ())
    selected = [
        t for t in pred_transitions
        if t.to == pred_chosen and t.when == pred_condition
    ]
    if not selected:
        return None
    return _post_transition_active_plan(
        original_plan_path=canonical_original_path,
        active_plan_path=pred_active,
        new_plan_path=pred_new,
        new_plan_exists=pred_new_exists,
        transition=selected[0],
    )


def _inherited_start_identity(
    prev_run: Mapping[str, object],
    *,
    run_dir: Path,
    steps: Mapping[str, Any],
    repo_root: Path,
    canonical_original_path: Path,
) -> Path | None:
    """Derive the exact active identity a resumed run's first turn inherited.

    A resumed successor legitimately starts from the continuation identity
    its bootstrap derived from the recorded ``resumed_from_run_id``
    predecessor: the overlay the predecessor's last finalized turn created,
    otherwise the plan that turn started from — the same validated
    continuation semantics the budget bootstrap applies.  The derivation
    uses only that predecessor's durable evidence, bound to the same
    original plan, workflow and execution workspace, with the predecessor's
    own terminal active reconciled against its last receipt.  Across
    invocations a validated budget-driven END is a legitimate predecessor
    terminal and reconciles through the same budget-END edge semantics the
    classifier enforces for any boundary source; within one invocation an
    END never qualifies as a previous turn, and any non-budget or
    contradicted END rejects.  Missing, cyclic or contradictory lineage
    returns None instead of admitting a merely historical path.
    """
    parent_id = prev_run.get("resumed_from_run_id")
    if not isinstance(parent_id, str) or not parent_id:
        return None
    if parent_id == run_dir.name:
        # A self-referential lineage can never prove an inherited start.
        return None
    parent_dir = _terminal_resume_run_dir(repo_root, parent_id)
    if parent_dir is None:
        return None
    parent_run = load_run_json(parent_dir)
    if parent_run is None:
        return None
    if parent_run.get("workflow_name") != prev_run.get("workflow_name"):
        return None
    parent_original = parent_run.get("original_plan_path")
    if not isinstance(parent_original, str) or not parent_original:
        return None
    if _plan_identity(parent_original, repo_root) != canonical_original_path:
        return None
    if _workspace_identity(parent_run) != _workspace_identity(prev_run):
        return None
    parent_turns = parent_run.get("turns_completed")
    if not _strict_positive_int(parent_turns):
        return None
    parent_receipt = _load_receipt(parent_dir, parent_turns)
    if parent_receipt is None:
        return None
    parent_identities = _receipt_plan_identities(parent_receipt, repo_root)
    if parent_identities is None:
        return None
    parent_active, parent_new, parent_new_exists = parent_identities
    # The predecessor's own records must agree first: its terminal active is
    # the post-transition selection recomputed from its last finalized
    # receipt — the same reconciliation every admitted run satisfies.
    parent_terminal = parent_run.get("active_plan_path")
    if not isinstance(parent_terminal, str) or not parent_terminal:
        return None
    parent_terminal_identity = _plan_identity(parent_terminal, repo_root)
    if parent_terminal_identity is None:
        return None
    parent_chosen = parent_receipt.get("chosen_transition")
    if parent_chosen == "END":
        # A predecessor invocation may legitimately end at its validated
        # budget-driven END before the successor was bootstrapped.  Its
        # terminal active reconciles against the exact ordered END edge its
        # last finalized turn selected — the same budget-END semantics the
        # classifier enforces for any boundary source.  An END chosen for
        # any other reason, or contradicted END evidence, never qualifies.
        parent_conditions = parent_receipt.get("conditions")
        parent_done = (
            parent_conditions.get("DONE")
            if isinstance(parent_conditions, Mapping)
            else None
        )
        if not isinstance(parent_done, bool):
            return None
        selected_end = _validated_budget_end_edge(
            step_name=parent_receipt.get("step_name"),
            saved_condition=parent_receipt.get("chosen_transition_condition"),
            done=parent_done,
            new_plan_exists=parent_new_exists,
            steps=steps,
        )
        if selected_end is None:
            return None
        parent_expected_terminal = _post_transition_active_plan(
            original_plan_path=canonical_original_path,
            active_plan_path=parent_active,
            new_plan_path=parent_new,
            new_plan_exists=parent_new_exists,
            transition=selected_end,
        )
    else:
        # A nonterminal predecessor (or a retry-scheduled turn without a
        # selection) reconciles through the same-invocation derivation,
        # which keeps rejecting an END as an alleged previous turn.
        parent_expected_terminal = _derived_next_start_identity(
            parent_receipt,
            steps=steps,
            repo_root=repo_root,
            canonical_original_path=canonical_original_path,
        )
    if parent_terminal_identity != parent_expected_terminal:
        return None
    return parent_new if parent_new_exists else parent_active


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
    selected_edge: Any = None
    if chosen == "END":
        selected_end = _validated_budget_end_edge(
            step_name=step_name,
            saved_condition=saved_condition,
            done=done,
            new_plan_exists=new_plan_exists,
            steps=steps,
        )
        if selected_end is None:
            return None
        selected_edge = selected_end
        # With unchanged conditions and the budget not exhausted, the
        # controller must continue to a real next step; an END or missing
        # target is not a resumable budget exit.
        try:
            next_edge = _select_transition(
                transitions,
                step_path=step_name,
                done=done,
                new_plan_exists=new_plan_exists,
                max_turns_reached=False,
            )
        except WorkflowError:
            return None
        if next_edge.to == "END":
            return None
        next_step_name = next_edge.to
    else:
        next_step_name = chosen
        # The receipt names the exact edge the ordered first-match routing
        # selected: same target and same recorded condition.  A different
        # edge with the same target contradicts the recorded routing.
        saved_edge_matches = [
            t for t in transitions
            if t.to == next_step_name and t.when == saved_condition
        ]
        if not saved_edge_matches:
            return None
        selected_edge = saved_edge_matches[0]
        if saved_condition is not None and not _evaluate(
            saved_condition, max_turns_reached
        ):
            return None
    target_step = steps.get(next_step_name)
    if target_step is None:
        return None

    original_plan_value = prev_run.get("original_plan_path")
    if not isinstance(original_plan_value, str) or not original_plan_value:
        return None
    # Bind every plan identity to the canonical run record with strict,
    # owned, normalized identities.  The before-turn active overlay and the
    # after-turn new overlay are distinct snapshots, and each is validated
    # against its own boundary evidence: the after-turn new identity against
    # the canonical recorded new plan, the before-turn active identity
    # against the exact ordered predecessor/start evidence and the terminal
    # reconciliation below.  A receipt that contradicts the canonical run
    # record is rejected before any successor reservation or provider launch.
    # Ownership is not inferred from filenames, repair credit, or existence
    # alone.
    canonical_active_value = prev_run.get("active_plan_path")
    canonical_new_value = prev_run.get("new_plan_path")
    if not isinstance(canonical_active_value, str) or not canonical_active_value:
        return None
    if not isinstance(canonical_new_value, str) or not canonical_new_value:
        return None
    original_plan_path = _plan_identity(original_plan_value, repo_root)
    active_plan_path = _plan_identity(active_plan_value, repo_root)
    new_plan_path = _plan_identity(new_plan_value, repo_root)
    canonical_active_path = _plan_identity(canonical_active_value, repo_root)
    canonical_new_path = _plan_identity(canonical_new_value, repo_root)
    if (
        original_plan_path is None
        or active_plan_path is None
        or new_plan_path is None
        or canonical_active_path is None
        or canonical_new_path is None
    ):
        return None
    # The after-turn new identity is bound exactly to the canonical recorded
    # new plan (they are the same after-turn snapshot); an unrelated
    # substitution is rejected.
    if new_plan_path != canonical_new_path:
        return None
    # Reconcile the selected transition with the canonical terminal active:
    # the terminal record must equal the controller's own post-transition
    # selection computed from the finalized receipt under its saved
    # conditions (NEW_PLAN_EXISTS overlay, preserve-active, or original
    # fallback).  Contradictory canonical evidence rejects.
    if canonical_active_path != _post_transition_active_plan(
        original_plan_path=original_plan_path,
        active_plan_path=active_plan_path,
        new_plan_path=new_plan_path,
        new_plan_exists=new_plan_exists,
        transition=selected_edge,
    ):
        return None
    # Validate the receipt's starting active identity against the exact
    # ordered boundary evidence.  Occurrence in recorded history is not
    # proof that a path was active for this turn: a reserved new path or an
    # older overlay must never qualify for that reason alone.
    if turns_completed == 1:
        # Start evidence: a fresh run's first turn starts from the canonical
        # original plan, and a selected preserve-active transition without a
        # new overlay keeps its start identity into the terminal record
        # (already reconciled above, so the terminal active pins it).  A
        # resumed run may instead inherit the exact continuation identity
        # derived from its recorded predecessor; anything else is a
        # substitution of a merely historical path.
        preserve_start = (
            getattr(selected_edge, "preserve_active_plan", False)
            and not new_plan_exists
        )
        if active_plan_path != original_plan_path and not preserve_start:
            inherited_start = _inherited_start_identity(
                prev_run,
                run_dir=run_dir,
                steps=steps,
                repo_root=repo_root,
                canonical_original_path=original_plan_path,
            )
            if inherited_start is None or active_plan_path != inherited_start:
                return None
    else:
        # Ordered predecessor evidence: the finalized turn must start from
        # exactly the plan the controller selected at the end of the previous
        # turn, recomputed from that turn's own receipt.
        predecessor = _load_receipt(run_dir, turns_completed - 1)
        expected_start = (
            None
            if predecessor is None
            else _derived_next_start_identity(
                predecessor,
                steps=steps,
                repo_root=repo_root,
                canonical_original_path=original_plan_path,
            )
        )
        if expected_start is None or active_plan_path != expected_start:
            return None

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
