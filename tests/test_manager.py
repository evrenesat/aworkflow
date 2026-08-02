from __future__ import annotations

from dataclasses import replace
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
    ManagerNoteAuthorityError,
    ManagerStopReport,
    build_manager_note_correction_prompts,
    build_manager_note_correction_result,
    build_manager_prompts,
    build_repartition_prompts,
    eligible_implementation_upgrade,
    parse_manager_decision,
    render_manager_stop_report,
    resolve_manager_role,
    validate_manager_decision,
    validate_manager_note_authority,
    validate_manager_note_correction,
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
    PendingRepartitionV1,
    PendingTeamOverride,
    ReviewRejectionRecord,
    manager_resume_fields,
    manager_state_payload,
    restore_manager_state,
)
from aflow.runlog import (
    create_repartition_attempt_paths,
    create_run_paths,
    write_manager_artifacts,
    write_manager_note_correction_artifacts,
    write_repartition_artifact,
    write_run_metadata,
)
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


def test_repartition_only_legal_for_full() -> None:
    decision = parse_manager_decision(_decision(action="repartition_current_checkpoint", next_step_notes=[]))
    validate_manager_decision(decision, level="full")
    with pytest.raises(ManagerDecisionError, match="only legal for Full"):
        validate_manager_decision(decision, level="lite")


def test_repartition_rejects_notes_and_stop_report() -> None:
    with pytest.raises(ManagerDecisionError, match="must not include next_step_notes"):
        parse_manager_decision(_decision(action="repartition_current_checkpoint"))
    with pytest.raises(ManagerDecisionError, match="stop_report is only allowed for stop"):
        parse_manager_decision(_decision(
            action="repartition_current_checkpoint",
            next_step_notes=[],
            stop_report={"summary": "x", "root_cause": "x", "evidence": ["x"], "attempts": "x", "workspace_state": "x", "next_actions": ["x"]},
        ))


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


def test_note_authority_compares_scope_claims_but_keeps_advisory_evidence() -> None:
    scope = {
        "active_plan_identity": "plan.md::checkpoint-1",
        "allowed_paths": ["aflow/manager.py", "tests/test_runtime.py"],
        "prohibited_paths": ["aflow/run_state.py"],
        "constraints_complete": True,
    }
    assert validate_manager_note_authority(
        ("The failure occurs in tests/test_runtime.py; focus verification there.",),
        scope=scope,
    ) == ("The failure occurs in tests/test_runtime.py; focus verification there.",)
    assert validate_manager_note_authority(
        ("Keep changes scoped to the allowed files `aflow/manager.py` and `tests/test_runtime.py`.",),
        scope=scope,
    ) == ("Keep changes scoped to the allowed files `aflow/manager.py` and `tests/test_runtime.py`.",)
    assert validate_manager_note_authority(
        ("Must not touch `aflow/run_state.py`.",),
        scope=scope,
    ) == ("Must not touch `aflow/run_state.py`.",)

    with pytest.raises(ManagerDecisionError, match="may not assert file"):
        validate_manager_note_authority(
            (
                "Keep changes scoped to the allowed file `aflow/manager.py`.",
            ),
            scope=scope,
        )
    with pytest.raises(ManagerDecisionError, match="may not assert file"):
        validate_manager_note_authority(("Use the eight dirty files and nothing else.",), scope=scope)
    with pytest.raises(ManagerDecisionError, match="mandatory implementation requirement"):
        validate_manager_note_authority(
            ("Ensure the worker implements the missing routing behavior.",),
            scope=scope,
        )


def test_note_authority_preserves_case_sensitive_path_identity() -> None:
    scope = {
        "active_plan_identity": "plan.md::checkpoint-1",
        "allowed_paths": ["README.md"],
        "prohibited_paths": [],
        "constraints_complete": True,
    }

    assert validate_manager_note_authority(
        ("Restrict edits to README.md.",),
        scope=scope,
    ) == ("Restrict edits to README.md.",)
    with pytest.raises(ManagerDecisionError, match="may not assert file"):
        validate_manager_note_authority(
            ("Restrict edits to readme.md.",),
            scope=scope,
        )


def test_note_authority_rejects_restrictive_paraphrases_but_allows_advice() -> None:
    scope = {
        "active_plan_identity": "plan.md::checkpoint-1",
        "allowed_paths": ["a.py", "b.py"],
        "prohibited_paths": ["c.py"],
        "constraints_complete": True,
    }
    restrictive_notes = (
        "Restrict edits to a.py.",
        "Limit work to a.py.",
        "Never modify a.py.",
        "The sole permitted file is a.py.",
        "Changes are confined to a.py.",
    )
    for note in restrictive_notes:
        with pytest.raises(ManagerDecisionError, match="may not assert file"):
            validate_manager_note_authority((note,), scope=scope)
    for verb in ("use", "follow", "switch to", "replace", "adopt", "work from"):
        with pytest.raises(ManagerDecisionError, match="replace or select"):
            validate_manager_note_authority(
                (f"{verb.title()} the repair plan.",), scope=scope
            )
    for note in (
        "Work from plans/new-repair.md.",
        "Adopt `plans/new-repair.md`.",
        "Switch to plans/new-repair.md.",
    ):
        with pytest.raises(ManagerDecisionError, match="replace or select"):
            validate_manager_note_authority((note,), scope=scope)
    assert validate_manager_note_authority(
        (
            "The defect is in a.py; focus the verification on tests/test_a.py.",
            "Ensure the regression test is exercised.",
        ),
        scope=scope,
    ) == (
        "The defect is in a.py; focus the verification on tests/test_a.py.",
        "Ensure the regression test is exercised.",
    )


def test_note_authority_errors_have_structured_category_and_correctability() -> None:
    scope = {
        "active_plan_identity": "plan.md::checkpoint-1",
        "allowed_paths": ["a.py", "b.py"],
        "prohibited_paths": ["c.py"],
        "constraints_complete": True,
    }
    cases = (
        ("Follow the repair plan.", "plan_selection", True),
        ("Restrict edits to a.py.", "file_scope", False),
        (
            "The implementation must add a second manager call.",
            "mandatory_implementation",
            False,
        ),
    )
    for note, category, correctable in cases:
        with pytest.raises(ManagerNoteAuthorityError) as raised:
            validate_manager_note_authority((note,), scope=scope)
        assert raised.value.category == category
        assert raised.value.correctable is correctable

    with pytest.raises(ManagerDecisionError) as parser_error:
        parse_manager_decision("not-json")
    assert not isinstance(parser_error.value, ManagerNoteAuthorityError)


def test_note_authority_structuring_preserves_legacy_messages() -> None:
    scope = {
        "active_plan_identity": "plan.md::checkpoint-1",
        "allowed_paths": ["a.py", "b.py"],
        "prohibited_paths": [],
        "constraints_complete": True,
    }
    expected = (
        ("Use the repair plan.", "next_step_notes may not replace or select an active plan for active plan plan.md::checkpoint-1"),
        ("Limit work to a.py.", "next_step_notes may not assert file or scope authority for active plan plan.md::checkpoint-1"),
        ("Ensure the worker adds a test.", "next_step_notes may not impose a mandatory implementation requirement for active plan plan.md::checkpoint-1"),
    )
    for note, message in expected:
        with pytest.raises(ManagerNoteAuthorityError) as raised:
            validate_manager_note_authority((note,), scope=scope)
        assert str(raised.value) == message


def test_note_authority_mixed_violations_are_not_correctable() -> None:
    scope = {
        "active_plan_identity": "plan.md::checkpoint-1",
        "allowed_paths": ["a.py", "b.py"],
        "prohibited_paths": [],
        "constraints_complete": True,
    }
    mixed_cases = (
        (("Follow the repair plan and restrict edits to a.py.",), "file_scope"),
        (("Follow the repair plan.", "Restrict edits to a.py."), "file_scope"),
        (("Restrict edits to a.py.", "Follow the repair plan."), "file_scope"),
        (
            (
                "Follow the repair plan.",
                "The implementation must add another manager call.",
            ),
            "mandatory_implementation",
        ),
        (
            (
                "The implementation must add another manager call.",
                "Follow the repair plan.",
            ),
            "mandatory_implementation",
        ),
    )
    for notes, category in mixed_cases:
        with pytest.raises(ManagerNoteAuthorityError) as raised:
            validate_manager_note_authority(notes, scope=scope)
        assert raised.value.category == category
        assert raised.value.correctable is False


def test_note_authority_legal_scope_does_not_hide_mandatory_requirement() -> None:
    scope = {
        "active_plan_identity": "plan.md::checkpoint-1",
        "allowed_paths": ["a.py", "b.py"],
        "prohibited_paths": [],
        "constraints_complete": True,
    }

    with pytest.raises(ManagerNoteAuthorityError) as raised:
        validate_manager_note_authority(
            (
                "The implementation must add another manager call; restrict edits "
                "to a.py and b.py.",
            ),
            scope=scope,
        )
    assert raised.value.category == "mandatory_implementation"
    assert raised.value.correctable is False


def test_note_authority_plan_selection_only_keeps_correction_contract() -> None:
    scope = {
        "active_plan_identity": "plan.md::checkpoint-1",
        "allowed_paths": ["a.py", "b.py"],
        "prohibited_paths": [],
        "constraints_complete": True,
    }

    with pytest.raises(ManagerNoteAuthorityError) as raised:
        validate_manager_note_authority(("Follow the repair plan.",), scope=scope)
    assert raised.value.category == "plan_selection"
    assert raised.value.correctable is True
    assert str(raised.value) == (
        "next_step_notes may not replace or select an active plan for active plan "
        "plan.md::checkpoint-1"
    )


def test_note_correction_prompt_is_compact_and_preserves_decision_fields() -> None:
    context = {
        "decision_number": 11,
        "level": "lite",
        "trigger": "reviewer_rejection",
        "active_plan_content": "SECRET PLAN PROSE",
        "manager_note_scope": {
            "active_plan_identity": "plans/main.md::checkpoint-1",
            "allowed_paths": ["a.py"],
            "prohibited_paths": [],
            "constraints_complete": True,
        },
        "controller_state": {
            "eligible_actions": ["continue", "stop"],
            "proposed_next_step": "implement",
            "mutable_workspace_state": "excluded",
        },
    }
    original = parse_manager_decision(_decision(
        reason="Keep the accepted action.",
        next_step_notes=["Follow the repair plan."],
    ))
    violation = ManagerNoteAuthorityError(
        "next_step_notes may not replace or select an active plan",
        category="plan_selection",
    )

    system, user = build_manager_note_correction_prompts(
        context,
        original_decision=original,
        violation=violation,
    )
    payload = json.loads(user.removeprefix("MANAGER_NOTE_CORRECTION_JSON:\n"))

    assert payload["decision_number"] == 11
    assert payload["level"] == "lite"
    assert payload["trigger"] == "reviewer_rejection"
    assert payload["eligible_actions"] == ["continue", "escalate_to_full", "stop"]
    assert payload["proposed_transition"] == "implement"
    assert payload["target_plan_identity"] == "plans/main.md::checkpoint-1"
    assert payload["original_decision"] == json.loads(json.dumps(original.to_dict()))
    assert payload["violation"]["category"] == "plan_selection"
    assert "SECRET PLAN PROSE" not in user
    assert "mutable_workspace_state" not in user
    for verb in ("use", "follow", "switch to", "replace", "adopt", "work from"):
        assert f"'{verb}'" in system
    assert "Preserve schema_version, action, reason, and stop_report exactly" in system
    assert "only rewrite or remove next_step_notes" in system
    assert "observable requirement" in system
    assert "verification evidence" in system

    immutable = original.to_dict()
    rewritten = {**immutable, "next_step_notes": ["The focused regression test passes."]}
    assert {key: rewritten[key] for key in ("schema_version", "action", "reason", "stop_report")} == {
        key: immutable[key] for key in ("schema_version", "action", "reason", "stop_report")
    }


def test_note_correction_prompt_rejects_noncorrectable_categories() -> None:
    with pytest.raises(ValueError, match="only plan_selection"):
        build_manager_note_correction_prompts(
            {"level": "full", "controller_state": {}},
            original_decision=parse_manager_decision(_decision()),
            violation=ManagerNoteAuthorityError(
                "next_step_notes may not assert file or scope authority",
                category="file_scope",
            ),
        )


def test_note_correction_rejects_changes_to_immutable_decision_fields() -> None:
    original = parse_manager_decision(_decision(
        reason="Immutable reason.",
        next_step_notes=["Follow the repair plan."],
    ))
    corrected = replace(
        original,
        next_step_notes=("The focused regression test passes.",),
    )
    assert validate_manager_note_correction(original, corrected) == corrected

    mutations = {
        "schema_version": replace(corrected, schema_version=2),
        "action": replace(corrected, action="retry_current_step"),
        "reason": replace(corrected, reason="Changed reason."),
        "stop_report": replace(
            corrected,
            stop_report=ManagerStopReport(
                summary="x",
                root_cause="x",
                evidence=("x",),
                attempts="x",
                workspace_state="x",
                next_actions=("x",),
            ),
        ),
    }
    for field, mutation in mutations.items():
        with pytest.raises(ManagerDecisionError, match=field):
            validate_manager_note_correction(original, mutation)


def test_note_correction_root_result_keeps_one_decision_legacy_contract() -> None:
    violation = ManagerNoteAuthorityError(
        "next_step_notes may not replace or select an active plan",
        category="plan_selection",
    )
    corrected = parse_manager_decision(_decision(
        reason="Immutable reason.",
        next_step_notes=["The focused regression test passes."],
    ))
    base = {
        "decision_number": 11,
        "level": "full",
        "trigger": "reviewer_rejection",
        "status": "invalid",
        "error": str(violation),
    }

    accepted = build_manager_note_correction_result(
        base,
        original_violation=violation,
        correction_artifact_path=(
            "manager/decision-011/note-authority-correction"
        ),
        correction_status="accepted",
        final_decision=corrected,
    )
    assert accepted["decision_number"] == 11
    assert accepted["status"] == "accepted"
    assert accepted["action"] == "continue"
    assert accepted["reason"] == "Immutable reason."
    assert accepted["attempt_count"] == 2
    assert accepted["correction_attempted"] is True
    assert accepted["original_violation"] == {
        "category": "plan_selection",
        "message": str(violation),
    }
    assert accepted["correction"] == {
        "artifact_path": "manager/decision-011/note-authority-correction",
        "status": "accepted",
    }

    invalid = build_manager_note_correction_result(
        base,
        original_violation=violation,
        correction_artifact_path=(
            "manager/decision-011/note-authority-correction"
        ),
        correction_status="invalid",
        final_decision=None,
        error="correction changed action",
    )
    assert invalid["decision_number"] == 11
    assert invalid["status"] == "invalid"
    assert invalid["action"] == "invalid"
    assert invalid["reason"] == "correction changed action"
    assert invalid["attempt_count"] == 2


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
    assert "next_step_notes are advisory evidence only" in system
    assert f"use at most {MAX_MANAGER_NOTES} notes" in system
    assert '"stop_report":{"summary":' in system
    assert user.startswith("MANAGER_CONTEXT_JSON:\n")
    assert json.loads(user.removeprefix("MANAGER_CONTEXT_JSON:\n")) == context


def test_lite_prompt_cause_based_first_rejection_does_not_force_upgrade() -> None:
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

    # Cause-based policy: both continue and upgrade are legal.
    assert "neither is forced" in system
    assert "continue with the same worker" in system
    assert "upgrade_next_implementation" in system
    # The old forced-upgrade language must NOT appear.
    assert "Choose upgrade_next_implementation now" not in system


def test_full_prompt_includes_repartition_guidance() -> None:
    context = {
        "level": "full",
        "active_plan_content": None,
        "run_id": "run-1",
        "controller_state": {
            "reviewer_rejection_count": 2,
            "eligible_actions": [
                "continue",
                "repartition_current_checkpoint",
                "stop",
            ],
        },
    }

    system, _ = build_manager_prompts(context)

    assert "repartition_current_checkpoint splits the current" in system
    assert "Do not name workflow steps, teams, or business logic" in system
    assert "next_step_notes must be []" in system


def test_repartition_subcall_prompts_have_authoritative_strict_contracts() -> None:
    payload = {"envelope": {"canonical_envelope_sha256": "a" * 64}}
    propose_system, propose_user = build_repartition_prompts(
        payload,
        mode="propose",
        skill_name="custom-repartition",
        correction_findings=("Keep the verification obligation.",),
    )
    validate_system, validate_user = build_repartition_prompts(
        payload, mode="validate",
    )

    assert "inline contract and rules below are authoritative" in propose_system
    assert "single correction attempt" in propose_system
    assert "custom-repartition" in propose_system
    assert propose_user.startswith("REPARTITION_PROPOSE_CONTEXT_JSON:\n")
    assert json.loads(
        propose_user.removeprefix("REPARTITION_PROPOSE_CONTEXT_JSON:\n")
    )["correction_findings"] == ["Keep the verification obligation."]
    assert "Independently compare the exact envelope" in validate_system
    assert validate_user.startswith("REPARTITION_VALIDATE_CONTEXT_JSON:\n")


def test_pending_repartition_and_attempt_artifacts_round_trip(tmp_path: Path) -> None:
    config = ControllerConfig(repo_root=tmp_path, plan_path=tmp_path / "plan.md")
    paths = create_run_paths(config)
    manager_dir = paths.manager_dir / "decision-001"
    manager_dir.mkdir(parents=True)
    attempt = create_repartition_attempt_paths(
        paths, decision_number=1, attempt_number=1,
    )
    write_repartition_artifact(attempt.source_plan, "# exact source\n")
    write_repartition_artifact(attempt.result, {"status": "accepted"})
    with pytest.raises(FileExistsError):
        write_repartition_artifact(attempt.result, {"status": "overwritten"})

    state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 1, False))
    state.pending_repartition = PendingRepartitionV1(
        schema_version=1,
        decision_number=1,
        scope_id="scope-1",
        stage="semantically_validated",
        envelope_sha256="a" * 64,
        source_plan_sha256="b" * 64,
        attempt_count=1,
        generation_id="gen-" + "c" * 64,
        candidate_plan_sha256="d" * 64,
        latest_attempt_path="manager/decision-001/repartition/attempt-001",
        proposal_artifact_path=(
            "manager/decision-001/repartition/attempt-001/proposal.json"
        ),
        candidate_artifact_path=(
            "manager/decision-001/repartition/attempt-001/candidate-plan.md"
        ),
        semantic_verdict_artifact_path=(
            "manager/decision-001/repartition/attempt-001/semantic-verdict.json"
        ),
    )
    payload = manager_state_payload(state)
    restored = ControllerState(last_snapshot=PlanSnapshot(None, 0, 1, False))
    restore_manager_state(restored, payload)

    assert restored.pending_repartition == state.pending_repartition
    assert manager_resume_fields(payload)["pending_repartition"] == state.pending_repartition
    assert attempt.source_plan.read_text(encoding="utf-8") == "# exact source\n"
    assert json.loads(attempt.result.read_text(encoding="utf-8"))["status"] == "accepted"


def test_manager_and_note_correction_artifacts_round_trip_payload(tmp_path: Path) -> None:
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

    correction = write_manager_note_correction_artifacts(
        paths,
        decision_number=1,
        system_prompt="correction system",
        user_prompt="correction user",
        stdout="corrected stdout",
        stderr="corrected stderr",
        result={"status": "accepted"},
    )
    assert correction.directory.relative_to(paths.run_dir).as_posix() == (
        "manager/decision-001/note-authority-correction"
    )
    assert correction.system_prompt.read_text(encoding="utf-8") == "correction system"
    assert correction.user_prompt.read_text(encoding="utf-8") == "correction user"
    assert correction.stdout.read_text(encoding="utf-8") == "corrected stdout"
    assert correction.stderr.read_text(encoding="utf-8") == "corrected stderr"
    assert json.loads(correction.result.read_text(encoding="utf-8")) == {
        "status": "accepted"
    }
    assert artifact.stdout.read_text(encoding="utf-8") == "{}"
    with pytest.raises(FileExistsError):
        write_manager_note_correction_artifacts(
            paths,
            decision_number=1,
            system_prompt="overwrite",
            user_prompt="overwrite",
        )

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
