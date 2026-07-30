from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

from aflow.plan import PlanSnapshot
from aflow.run_state import (
    CheckpointRepartitionRecord,
    ControllerState,
    FrozenRunIdentity,
    ManagerDecisionSummary,
    OverrideResult,
    PendingManagerNotes,
    PendingRepartitionV1,
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


def test_status_summary_surfaces_literal_repartition_observability() -> None:
    from rich.console import Console

    state = ControllerState(
        last_snapshot=PlanSnapshot("Checkpoint 1 / Partition 1", 1, 1, False, 2, 2)
    )
    state.scope_pressure_reason = "[bold red]split safely[/bold red]"
    state.pending_repartition = PendingRepartitionV1(
        schema_version=1,
        decision_number=4,
        scope_id="scope-1",
        stage="failed",
        envelope_sha256="a" * 64,
        source_plan_sha256="b" * 64,
        failed_stage="validate",
    )
    state.repartition_history.append(CheckpointRepartitionRecord(
        schema_version=1,
        decision_number=3,
        scope_id="scope-1",
        generation_id="gen-123",
        envelope_sha256="a" * 64,
        envelope_artifact_sha256="c" * 64,
        source_plan_sha256="b" * 64,
        proposal_sha256="d" * 64,
        candidate_plan_sha256="e" * 64,
        partition_ids=("part-1", "part-2"),
        child_summaries=("Part one", "Part two"),
        current_disposition="review_current_partition",
        resolved_target_step="review",
        resolved_target_role="reviewer",
        current_partition_id="part-1",
        scope_pressure_reason="[bold red]split safely[/bold red]",
        envelope_artifact_path="scope/envelope.json",
        proposal_artifact_path="manager/proposal.json",
        candidate_artifact_path="manager/candidate.md",
        mechanical_validation_artifact_path="manager/mechanical.json",
        semantic_verdict_artifact_path="manager/verdict.json",
    ))

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

    assert "[bold red]split safely[/bold red]" in rendered
    assert "failed / failed: validate" in rendered
    assert "gen-123 / 2 parts / review_current_partition" in rendered
    assert "manager/candidate.md" in rendered
