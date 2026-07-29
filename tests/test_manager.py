from __future__ import annotations

import json
from pathlib import Path

import pytest

from aflow.config import (
    HarnessProfileConfig,
    ManagerConfig,
    TeamConfig,
    WorkflowHarnessConfig,
    WorkflowUserConfig,
)
from aflow.manager import (
    MAX_MANAGER_NOTES,
    ManagerDecisionError,
    ManagerStopReport,
    build_manager_prompts,
    eligible_implementation_upgrade,
    parse_manager_decision,
    render_manager_stop_report,
    resolve_manager_role,
    validate_manager_decision,
)
from aflow.plan import PlanSnapshot
from aflow.run_state import (
    ActiveImplementationScope,
    ControllerConfig,
    ControllerState,
    ExecutionContext,
    ImplementationAttempt,
    ManagerDecisionSummary,
    PendingManagerNotes,
    PendingTeamOverride,
    ReviewRejectionRecord,
    manager_resume_fields,
    manager_state_payload,
    restore_manager_state,
)
from aflow.runlog import create_run_paths, write_manager_artifacts, write_run_metadata
from aflow.workflow import _implementation_upgrade_depth, _mutable_implementation_attempts


def _config(*, upgraded_selector: str = "codex.high") -> WorkflowUserConfig:
    return WorkflowUserConfig(
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={
                    "nano": HarnessProfileConfig(model="nano"),
                    "mini": HarnessProfileConfig(model="mini"),
                    "high": HarnessProfileConfig(model="high"),
                    "max": HarnessProfileConfig(model="max"),
                }
            )
        },
        roles={
            "worker": "codex.mini",
            "manager_lite": "codex.nano",
            "manager_full": "codex.high",
        },
        teams={
            "base": TeamConfig(roles={"worker": "codex.mini"}, upgrade_to="high"),
            "high": TeamConfig(roles={"worker": upgraded_selector}, upgrade_to="max"),
            "max": TeamConfig(roles={"worker": "codex.max"}),
        },
        manager=ManagerConfig(enabled=True, lite_role="manager_lite", full_role="manager_full"),
    )


def _decision(**overrides: object) -> str:
    payload: dict[str, object] = {
        "schema_version": 1,
        "action": "continue",
        "reason": "The proposed transition has sufficient evidence.",
        "next_step_notes": ["Keep the retry narrowly scoped."],
        "stop_report": None,
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_decision_protocol_rejects_unknown_fields_and_illegal_combinations() -> None:
    with pytest.raises(ManagerDecisionError, match="unknown fields"):
        parse_manager_decision(_decision(extra=True))
    with pytest.raises(ManagerDecisionError, match="stop requires stop_report"):
        parse_manager_decision(_decision(action="stop", next_step_notes=[]))
    with pytest.raises(ManagerDecisionError, match="accepted END"):
        validate_manager_decision(
            parse_manager_decision(_decision()), proposed_transition="END"
        )
    with pytest.raises(ManagerDecisionError, match="not eligible"):
        validate_manager_decision(
            parse_manager_decision(_decision()), eligible_actions={"stop"}
        )


@pytest.mark.parametrize(
    "noisy",
    [
        "\x1b[2mthinking\x1b[0m\n" + _decision(),
        "Manager decision:\n```json\n" + _decision() + "\n```",
        "```\n" + _decision() + "\n```",
        _decision() + "\n" + _decision(),
    ],
)
def test_decision_protocol_rejects_transport_noise(noisy: str) -> None:
    with pytest.raises(ManagerDecisionError, match="not valid JSON|single JSON object"):
        parse_manager_decision(noisy)


def test_decision_protocol_accepts_one_exact_json_fence() -> None:
    decision = parse_manager_decision("```json\n" + _decision() + "\n```\n")

    assert decision.action == "continue"
    assert decision.reason == "The proposed transition has sufficient evidence."


def test_decision_protocol_bounds_surplus_advisory_notes_without_escalation() -> None:
    notes = [f"Repair note {index}." for index in range(MAX_MANAGER_NOTES + 1)]

    decision = parse_manager_decision(_decision(
        action="upgrade_next_implementation",
        next_step_notes=notes,
    ))

    assert decision.action == "upgrade_next_implementation"
    assert decision.next_step_notes == tuple(notes[:MAX_MANAGER_NOTES])


def test_stop_protocol_and_report_rendering_are_self_contained() -> None:
    decision = parse_manager_decision(_decision(
        action="stop",
        next_step_notes=[],
        stop_report={
            "summary": "The workflow is stalled.",
            "root_cause": "Two attempts produced the same invalid plan state.",
            "evidence": ["turn 4 had no plan delta"],
            "attempts": "Two worker attempts and one review attempt ran.",
            "workspace_state": "Branch is clean; the active plan remains in progress.",
            "next_actions": ["Inspect the manager artifact.", "Repair the active plan."],
        },
    ))
    report = render_manager_stop_report(
        context={"run_id": "run-1", "finished_turn": {"turn_number": 4}, "plan_state": {"active_plan_path": "plans/a.md"}},
        stop_report=decision.stop_report,
    )
    assert "# AFlow manager report" in report
    assert "Two attempts produced" in report
    assert "plans/a.md" in report


def test_terminal_fallback_report_preserves_incident_before_protocol_error() -> None:
    report = render_manager_stop_report(
        context={
            "run_id": "run-1",
            "decision_number": 1,
            "level": "full",
            "trigger": "ambiguous_failure",
            "finished_turn": {
                "turn_number": 1,
                "status": "harness-failed",
                "error": None,
            },
            "controller_state": {
                "terminal": True,
                "baseline_team": "ds4_lite",
                "lite_evidence": "harness 'reasonix' exited with code 2",
            },
            "plan_state": {"active_plan_path": "plans/a.md"},
        },
        failure_reason="next_step_notes must be an array of non-empty strings",
    )
    assert "## Summary\nharness 'reasonix' exited with code 2" in report
    assert "The controller reached a terminal workflow incident." in report
    assert "Manager decision error: next_step_notes must be an array" in report


def test_manager_role_and_one_edge_upgrade_use_baseline_routing_only() -> None:
    config = _config()
    resolved = resolve_manager_role(config, level="lite", baseline_team="base")
    upgrade = eligible_implementation_upgrade(
        config, role="worker", baseline_team="base"
    )
    assert resolved.selector == "codex.nano"
    assert upgrade.available is True
    assert upgrade.source_team == "base"
    assert upgrade.target_team == "high"
    assert upgrade.target_selector == "codex.high"
    assert config.teams["base"].upgrade_to == "high"


def test_upgrade_is_unavailable_when_target_selector_does_not_change() -> None:
    upgrade = eligible_implementation_upgrade(
        _config(upgraded_selector="codex.mini"), role="worker", baseline_team="base"
    )
    assert upgrade.available is False
    assert "same selector" in str(upgrade.reason)


def test_upgrade_advances_from_most_recent_team_and_stops_at_chain_end() -> None:
    config = _config()
    second = eligible_implementation_upgrade(
        config,
        role="worker",
        baseline_team="base",
        most_recent_implementation_team="high",
    )
    assert second.available is True
    assert second.source_team == "high"
    assert second.target_team == "max"
    assert second.target_selector == "codex.max"

    exhausted = eligible_implementation_upgrade(
        config,
        role="worker",
        baseline_team="base",
        most_recent_implementation_team="max",
    )
    assert exhausted.available is False
    assert exhausted.source_team == "max"
    assert "does not configure upgrade_to" in str(exhausted.reason)


def test_upgrade_depth_counts_edges_not_same_team_retries() -> None:
    config = _config()
    assert _implementation_upgrade_depth(
        config, baseline_team="base", most_recent_team="base"
    ) == 0
    assert _implementation_upgrade_depth(
        config, baseline_team="base", most_recent_team="high"
    ) == 1
    assert _implementation_upgrade_depth(
        config, baseline_team="base", most_recent_team="max"
    ) == 2


def test_prompts_preserve_only_the_supplied_context_level() -> None:
    context = {
        "level": "lite",
        "active_plan_content": None,
        "run_id": "run-1",
        "controller_state": {"eligible_actions": ["continue", "stop"]},
    }
    system, user = build_manager_prompts(context, skill_name="custom-manager")
    assert "LITE" in system
    assert "configured manager skill 'custom-manager'" in system
    assert "Eligible actions at this boundary: continue, escalate_to_full, stop." in system
    assert "next_step_notes must always be an array" in system
    assert f"use at most {MAX_MANAGER_NOTES} notes" in system
    assert '"stop_report":{"summary":' in system
    assert user.startswith("MANAGER_CONTEXT_JSON:\n")
    assert json.loads(user.removeprefix("MANAGER_CONTEXT_JSON:\n")) == context


def test_lite_prompt_requires_eligible_upgrade_after_first_reviewer_rejection() -> None:
    context = {
        "level": "lite",
        "active_plan_content": None,
        "run_id": "run-1",
        "controller_state": {
            "reviewer_rejection_count": 1,
            "eligible_actions": [
                "continue",
                "stop",
                "upgrade_next_implementation",
            ],
            "eligible_upgrade": {
                "available": True,
                "source_team": "ds4_pro",
                "target_team": "terra_xhigh",
            },
        },
    }

    system, _ = build_manager_prompts(context)

    assert "Choose upgrade_next_implementation now" in system
    assert "or escalate to Full before applying this upgrade" in system


def test_manager_artifacts_and_state_round_trip_payload(tmp_path: Path) -> None:
    paths = create_run_paths(ControllerConfig(repo_root=tmp_path, plan_path=tmp_path / "plan.md"))
    artifact = write_manager_artifacts(
        paths,
        decision_number=1,
        context={"schema_version": 1, "level": "lite"},
        system_prompt="system",
        user_prompt="user",
        stdout="{}",
        result={"action": "continue"},
    )
    assert artifact.context.relative_to(paths.run_dir).as_posix() == "manager/decision-001/context.json"
    assert json.loads(artifact.result.read_text(encoding="utf-8"))["action"] == "continue"

    state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 1, False))
    state.manager_decision_number = 1
    state.manager_history.append(ManagerDecisionSummary(1, "lite", "normal", "continue", "safe", "manager/decision-001"))
    state.semantic_stall_count = 1
    state.reviewer_rejection_count = 2
    state.implementation_attempts["cp-2"] = 2
    state.active_implementation_scope = ActiveImplementationScope(
        "plan.md::checkpoint-2::second",
        "plan.md",
        2,
        "Second",
        3,
        awaiting_review=True,
        carried_reviewer_rejection_count=1,
    )
    state.review_rejection_history.append(ReviewRejectionRecord(
        "plan.md::checkpoint-2::second", 1, "source-run", 4, "review", "codex.review",
        2, "Second", 3, "base", "codex.worker", "Needs fixes", None,
        "turns/turn-004/stdout.txt", None,
    ))
    state.pending_manager_notes = PendingManagerNotes("implement", ("focus",), 1)
    state.pending_step_team_override = PendingTeamOverride("implement", "worker", "base", "high", "codex.high", "cp-2", 1)
    state.last_manager_report_path = "manager-report.md"
    payload = manager_state_payload(state)
    assert payload["manager_history"][0]["action"] == "continue"
    assert payload["pending_step_team_override"]["target_team"] == "high"
    restored = ControllerState(last_snapshot=PlanSnapshot(None, 0, 1, False))
    restore_manager_state(restored, payload)
    assert restored.manager_history == state.manager_history
    assert restored.active_implementation_scope == state.active_implementation_scope
    assert (
        restored.active_implementation_scope.carried_reviewer_rejection_count
        == 1
    )
    assert restored.pending_manager_notes == state.pending_manager_notes
    assert restored.pending_step_team_override == state.pending_step_team_override
    assert restored.review_rejection_history == state.review_rejection_history
    assert manager_resume_fields(payload)["review_rejection_history"] == tuple(state.review_rejection_history)
    payload["review_rejection_history"].append({"scope_id": "malformed"})
    malformed_restored = ControllerState(last_snapshot=PlanSnapshot(None, 0, 1, False))
    restore_manager_state(malformed_restored, payload)
    assert malformed_restored.review_rejection_history == state.review_rejection_history
    assert manager_resume_fields(payload)["pending_manager_notes"] == state.pending_manager_notes
    write_run_metadata(
        paths,
        ControllerConfig(repo_root=tmp_path, plan_path=tmp_path / "plan.md"),
        state,
        status="running",
    )
    run_json = json.loads(paths.run_json.read_text(encoding="utf-8"))
    assert run_json["last_manager_report_path"] == "manager-report.md"
    assert run_json["manager_history"][0]["artifact_path"] == "manager/decision-001"
    assert run_json["review_rejection_history"][0]["source_run_id"] == "source-run"


def test_manager_style_metadata_write_preserves_worktree_lifecycle(tmp_path: Path) -> None:
    config = ControllerConfig(repo_root=tmp_path, plan_path=tmp_path / "plan.md")
    paths = create_run_paths(config)
    state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 1, False))
    worktree = tmp_path / "worktrees" / "feature"
    execution_context = ExecutionContext(
        primary_repo_root=tmp_path,
        execution_repo_root=worktree,
        main_branch="main",
        feature_branch="feature/test",
        worktree_path=worktree,
        setup=("worktree", "branch"),
        teardown=("merge", "rm_worktree"),
    )
    write_run_metadata(
        paths,
        config,
        state,
        status="running",
        execution_context=execution_context,
    )

    write_run_metadata(paths, config, state, status="running")

    run_json = json.loads(paths.run_json.read_text(encoding="utf-8"))
    assert run_json["execution_repo_root"] == str(worktree)
    assert run_json["feature_branch"] == "feature/test"
    assert run_json["main_branch"] == "main"
    assert run_json["worktree_path"] == str(worktree)
    assert run_json["lifecycle_setup"] == ["worktree", "branch"]
    assert run_json["lifecycle_teardown"] == ["merge", "rm_worktree"]


def test_resume_attempt_history_is_tolerant_and_live_conversion_is_mutable() -> None:
    payload = {
        "implementation_attempts": {
            "scope-1": [{
                "turn_number": 1,
                "step_name": "implement",
                "role": "worker",
                "team": "base",
                "selector": "codex.mini",
                "outcome": "progress",
            }],
        },
    }
    fields = manager_resume_fields(payload)
    attempts = fields["implementation_attempts"]["scope-1"]
    assert isinstance(attempts, tuple)
    assert attempts == (
        ImplementationAttempt(1, "implement", "worker", "base", "codex.mini", "progress"),
    )
    live_attempts = _mutable_implementation_attempts(fields["implementation_attempts"])
    assert isinstance(live_attempts["scope-1"], list)
    live_attempts["scope-1"].append(
        ImplementationAttempt(2, "implement", "worker", "high", "codex.high", "progress")
    )
    assert len(live_attempts["scope-1"]) == 2

    legacy = manager_resume_fields({"implementation_attempts": {"checkpoint-1": 2}})
    assert legacy["active_implementation_scope"] is None
    assert legacy["implementation_attempts"]["checkpoint-1"] == ()


def test_legacy_scope_does_not_restore_run_wide_reviewer_rejections() -> None:
    payload = {
        "reviewer_rejection_count": 2,
        "active_implementation_scope": {
            "scope_id": "plan.md::checkpoint-2::second",
            "original_plan_path": "plan.md",
            "checkpoint_index": 2,
            "checkpoint_name": "Second",
            "opened_turn_number": 5,
            "awaiting_review": False,
        },
    }

    restored = ControllerState(last_snapshot=PlanSnapshot(None, 0, 1, False))
    restore_manager_state(restored, payload)
    fields = manager_resume_fields(payload)

    assert restored.reviewer_rejection_count == 0
    assert fields["reviewer_rejection_count"] == 0
    scope = fields["active_implementation_scope"]
    assert isinstance(scope, ActiveImplementationScope)
    assert scope.opened_turn_number == 5
    assert scope.carried_reviewer_rejection_count == 0
