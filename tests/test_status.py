from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

from aflow.plan import PlanSnapshot
from aflow.run_state import (
    ControllerState,
    FrozenRunIdentity,
    ManagerDecisionSummary,
    OverrideResult,
    PendingManagerNotes,
    PendingTeamOverride,
)
from aflow.status import _RICH_AVAILABLE, _build_summary_table


pytestmark = pytest.mark.skipif(not _RICH_AVAILABLE, reason="rich status rendering is unavailable")


def test_status_summary_surfaces_compact_manager_state() -> None:
    from rich.console import Console

    state = ControllerState(last_snapshot=PlanSnapshot("Checkpoint 1: Test", 2, 1, False, 2, 1))
    state.manager_history.append(ManagerDecisionSummary(
        decision_number=2, level="full", trigger="lite_escalation",
        action="upgrade_next_implementation", reason="retry needs a stronger team",
        artifact_path="manager/decision-002",
    ))
    state.pending_manager_notes = PendingManagerNotes("implement", ("Focus on the failing test.",), 2)
    state.pending_step_team_override = PendingTeamOverride(
        target_step="implement", role="worker", source_team="base", target_team="strong",
        selector="codex.high", checkpoint_identity="plans/in-progress/plan.md", decision_number=2,
    )
    state.last_manager_report_path = "manager-report.md"

    table = _build_summary_table(
        workflow_name="test", config_harness=None, config_model=None, config_effort=None,
        config_max_turns=5, config_plan_path=Path("plan.md"), original_plan_path=None,
        active_plan_path=None, new_plan_path=None, state=state, git_summary=None,
        banner_files_limit=10,
    )
    output = StringIO()
    Console(file=output, force_terminal=False).print(table)

    rendered = output.getvalue()
    assert "Manager" in rendered
    assert "full / lite_escalation / upgrade_next_implementation" in rendered
    assert "pending for implement" in rendered
    assert "implement: strong" in rendered
    assert "manager-report.md" in rendered


def test_status_summary_surfaces_safe_override_diagnostics() -> None:
    from rich.console import Console

    state = ControllerState(
        last_snapshot=PlanSnapshot(
            "Checkpoint 1: Test",
            2,
            1,
            False,
            2,
            1,
        )
    )
    state.frozen_run_identity = FrozenRunIdentity(
        workflow_name="test",
        config_path="/config",
        config_fingerprint="1234567890abcdef",
    )
    state.override_file_present = True
    state.override_result = OverrideResult(
        status="rejected",
        digest="abc",
        message="team is incompatible",
    )

    table = _build_summary_table(
        workflow_name="test",
        config_harness=None,
        config_model=None,
        config_effort=None,
        config_max_turns=5,
        config_plan_path=Path("plan.md"),
        original_plan_path=None,
        active_plan_path=None,
        new_plan_path=None,
        state=state,
        git_summary=None,
        banner_files_limit=10,
    )
    output = StringIO()
    Console(file=output, force_terminal=False).print(table)
    rendered = output.getvalue()

    assert "State Schema" in rendered
    assert "1234567890ab" in rendered
    assert "Override File" in rendered
    assert "rejected: team is incompatible" in rendered
    assert "correct overrides.toml and resume" in rendered
