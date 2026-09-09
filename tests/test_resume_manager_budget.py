import json
from pathlib import Path

import pytest

from aflow.cli import (
    _completed_manager_budget_boundary_pending,
    _pending_finalized_resume_turn,
)


@pytest.fixture
def budget_failure(tmp_path):
    run = tmp_path / ".aflow/runs/example"
    turn = run / "turns/turn-001/result.json"
    turn.parent.mkdir(parents=True)
    turn.write_text(json.dumps({
        "turn_number": 1, "returncode": 0, "step_name": "implement",
        "step_role": "worker", "selector": "muse.spark",
        "active_plan_path": str(tmp_path / "plan.md"),
        "new_plan_path": str(tmp_path / "fix.md"),
        "chosen_transition": "review", "chosen_transition_condition": "DONE",
        "conditions": {"DONE": True, "NEW_PLAN_EXISTS": False, "MAX_TURNS_REACHED": False},
        "snapshot_after": {"is_complete": True, "total_checkpoint_count": 10,
                           "unchecked_checkpoint_count": 0,
                           "current_checkpoint_unchecked_step_count": 0},
    }))
    decision = run / "manager/decision-001/result.json"
    decision.parent.mkdir(parents=True)
    decision.write_text(json.dumps({
        "decision_number": 1, "finalized_turn_number": 1,
        "status": "invalid", "failure_stage": "prelaunch", "trigger": "post_turn",
        "error": "manager inline context exceeds the 32768-byte hard limit: total_bytes=33576",
    }))
    state = {"run_dir": str(run), "status": "failed", "active_turn": 1,
             "turns_completed": 1, "manager_decision_number": 1}
    return tmp_path, run, decision, state


def test_completed_budget_failure_restores_review_boundary(budget_failure):
    root, run, _, state = budget_failure
    assert _completed_manager_budget_boundary_pending(state, root)
    pending = _pending_finalized_resume_turn(run, state)
    assert pending.chosen_transition == "review"
    assert pending.snapshot_after.is_complete


@pytest.mark.parametrize("change", [
    {"failure_stage": "provider"}, {"finalized_turn_number": 2},
    {"error": "invalid manager decision"}, {"status": "completed"},
])
def test_unrelated_or_mismatched_manager_failure_is_not_replayed(budget_failure, change):
    root, run, decision, state = budget_failure
    payload = json.loads(decision.read_text())
    payload.update(change)
    decision.write_text(json.dumps(payload))
    assert not _completed_manager_budget_boundary_pending(state, root)
    assert _pending_finalized_resume_turn(run, state) is None


def test_consumed_boundary_and_missing_turn_are_not_replayed(budget_failure):
    root, run, _, state = budget_failure
    state["pending_boundary_decision"] = {"finalized_turn_number": 1}
    assert not _completed_manager_budget_boundary_pending(state, root)
    state.pop("pending_boundary_decision")
    (run / "turns/turn-001/result.json").unlink()
    assert not _completed_manager_budget_boundary_pending(state, root)
