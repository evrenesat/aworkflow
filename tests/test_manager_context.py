from __future__ import annotations

import json
import hashlib

import pytest
from pathlib import Path

from aflow.analyzer import extract_aflow_stop
from aflow.api import AnalyzeRequest, analyze_runs
from aflow.manager_context import (
    DIAGNOSTIC_LIMIT,
    ORIGINAL_CHECKPOINT_AUTHORITY_VERSION,
    build_manager_context,
    build_manager_note_scope,
    extract_semantic_result,
    summarize_repair_plan,
    summarize_review_rejection,
    scoped_reviewer_rejection_count,
)
from aflow.manager import (
    ManagerDecisionV1,
    ManagerNoteAuthorityError,
    build_manager_prompts,
    build_manager_note_correction_prompts,
)
from aflow.stop_marker import (
    STRUCTURED_TRANSPORT_OUTPUT_SOURCE,
    detect_stop_marker,
)
from aflow.repartition import (
    EvidenceArtifactReferenceV2,
    create_envelope,
    create_envelope_v2,
    slice_checkpoint_source,
    write_envelope_atomic,
)


INCIDENT_SIGNAL_TEXT = "\n".join(
    (
        "Plan Branch: main; the current checkout is aflow-feature and requires me to stop and escalate",
        "Need your direction on one point",
        "the original plan file is missing after the turn",
        "merge verification cannot be completed safely",
    )
)
INCIDENT_SIGNAL_NAMES = [
    "branch_mismatch_review_block",
    "dirty_merge_verification",
    "needs_human_direction",
    "original_plan_missing",
]


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_note_correction_context_preserves_boundary_and_lite_redaction() -> None:
    proposed_scope = {
        "active_plan_identity": "plans/in-progress/repair.md::checkpoint-2",
        "constraints_complete": True,
        "allowed_paths": ["aflow/workflow.py"],
    }
    context = {
        "decision_number": 11,
        "level": "lite",
        "trigger": "review_rejected",
        "active_plan_content": None,
        "manager_note_scope": proposed_scope,
        "retry_manager_note_scope": None,
        "controller_state": {
            "eligible_actions": ["continue", "stop"],
            "proposed_next_step": "implement",
        },
    }
    original = ManagerDecisionV1(
        schema_version=1,
        action="continue",
        reason="The bounded repair remains actionable.",
        next_step_notes=("Use the v04 repair plan.",),
    )
    violation = ManagerNoteAuthorityError(
        "next_step_notes may not replace or select an active plan",
        category="plan_selection",
    )

    _, user_prompt = build_manager_note_correction_prompts(
        context, original_decision=original, violation=violation
    )
    payload = json.loads(
        user_prompt.split("MANAGER_NOTE_CORRECTION_JSON:\n", 1)[1]
    )

    assert payload["decision_number"] == 11
    assert payload["level"] == "lite"
    assert payload["trigger"] == "review_rejected"
    assert payload["eligible_actions"] == ["continue", "escalate_to_full", "stop"]
    assert payload["proposed_transition"] == "implement"
    assert payload["manager_note_scope"] == proposed_scope
    assert payload["target_plan_identity"] == proposed_scope["active_plan_identity"]
    assert "active_plan_content" not in payload
    assert "Secret implementation plan" not in user_prompt


def _snapshot(*, unchecked_steps: int = 1) -> dict[str, object]:
    return {
        "current_checkpoint_index": 1,
        "current_checkpoint_name": "Checkpoint 1: Context",
        "current_checkpoint_unchecked_step_count": unchecked_steps,
        "is_complete": False,
        "total_checkpoint_count": 1,
        "unchecked_checkpoint_count": 1,
    }


def _write_turn(
    run_dir: Path,
    number: int,
    *,
    step: str,
    role: str,
    stdout: str,
    stderr: str = "diagnostic only",
    status: str = "completed",
    returncode: int | None = 0,
    before: dict[str, object] | None = None,
    after: dict[str, object] | None = None,
    output_contract: str | None = None,
    semantic_output_source: str | None = None,
) -> None:
    turn_dir = run_dir / "turns" / f"turn-{number:03d}"
    turn_dir.mkdir(parents=True)
    result = {
        "turn_number": number,
        "step_name": step,
        "step_role": role,
        "status": status,
        "returncode": returncode,
        "selector": "codex.nano",
        "snapshot_before": before or _snapshot(),
        "snapshot_after": after or _snapshot(),
        "chosen_transition": "next",
    }
    if output_contract is not None:
        result["output_contract"] = output_contract
    if semantic_output_source is not None:
        result["semantic_output_source"] = semantic_output_source
    _write_json(turn_dir / "result.json", result)
    (turn_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    (turn_dir / "stderr.txt").write_text(stderr, encoding="utf-8")


def _run(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    plan = repo / "plans" / "in-progress" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Secret implementation plan\n\n### [ ] Checkpoint 1: Context\n- [ ] private instruction\n", encoding="utf-8")
    run_dir = repo / ".aflow" / "runs" / "run-1"
    _write_json(run_dir / "run.json", {"plan_path": str(plan), "active_plan_path": str(plan), "original_plan_path": str(plan), "team": "base", "turns_completed": 1, "max_turns": 5})
    return run_dir, plan


def _enveloped_boundary(run_dir: Path, plan: Path) -> dict[str, object]:
    """Create one controller-shaped selector-3 boundary with immutable scope evidence."""
    scope_id = "plans/in-progress/plan.md::checkpoint-1::context"
    envelope = create_envelope(
        scope_id=scope_id,
        original_plan_path="plans/in-progress/plan.md",
        plan_text=plan.read_text(encoding="utf-8"),
        checkpoint_index=1,
    )
    artifact = write_envelope_atomic(
        envelope,
        run_dir / "scopes" / envelope.scope_digest,
    )
    artifact_bytes = artifact.read_bytes()
    return {
        "context_schema_version": 3,
        "active_implementation_scope": {
            "scope_id": scope_id,
            "checkpoint_index": 1,
            "checkpoint_name": "Checkpoint 1: Context",
            "opened_turn_number": 1,
        },
        "envelope_artifact_path": (
            f"scopes/{envelope.scope_digest}/envelope.json"
        ),
        "envelope_artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        "envelope_canonical_sha256": envelope.canonical_envelope_sha256,
    }


def _repair_context_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, dict[str, object], str, str]:
    """Build a v2 scope boundary with an unheaded repair overlay."""
    repo = tmp_path / "repo"
    original = repo / "plans" / "in-progress" / "original.md"
    overlay = repo / "plans" / "in-progress" / "original-cp03-v01.md"
    original.parent.mkdir(parents=True)
    original_text = (
        "# Original plan\n\n"
        "### [x] Checkpoint 1: First\n- [x] first\n\n"
        "### [x] Checkpoint 2: Second\n- [x] second\n\n"
        "### [ ] Checkpoint 3: Third\n- [ ] third\n"
    )
    overlay_text = "# Repair overlay\n\n- [ ] repair the third checkpoint\n"
    original.write_text(original_text, encoding="utf-8")
    overlay.write_text(overlay_text, encoding="utf-8")
    run_dir = repo / ".aflow" / "runs" / "run-1"
    _write_json(run_dir / "run.json", {
        "plan_path": str(original),
        "active_plan_path": str(overlay),
        "original_plan_path": str(original),
        "team": "base",
        "turns_completed": 1,
        "max_turns": 5,
    })

    checkpoint_slice = slice_checkpoint_source(original_text, checkpoint_index=3)
    assert checkpoint_slice is not None
    checkpoint_text = checkpoint_slice.full_text
    plan_bytes = original_text.encode("utf-8")
    checkpoint_bytes = checkpoint_text.encode("utf-8")
    plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    checkpoint_sha256 = hashlib.sha256(checkpoint_bytes).hexdigest()
    plan_ref = EvidenceArtifactReferenceV2(
        kind="plan",
        path=f".aflow/runs/run-1/evidence/plans/{plan_sha256}.md",
        sha256=plan_sha256,
        byte_size=len(plan_bytes),
    )
    checkpoint_ref = EvidenceArtifactReferenceV2(
        kind="checkpoint",
        path=(
            f".aflow/runs/run-1/evidence/checkpoints/{checkpoint_sha256}.md"
        ),
        sha256=checkpoint_sha256,
        byte_size=len(checkpoint_bytes),
    )
    (run_dir / "evidence" / "plans").mkdir(parents=True)
    (run_dir / "evidence" / "checkpoints").mkdir(parents=True)
    (repo / plan_ref.path).write_bytes(plan_bytes)
    (repo / checkpoint_ref.path).write_bytes(checkpoint_bytes)
    scope_id = "plans/in-progress/original.md::checkpoint-3::third"
    envelope = create_envelope_v2(
        scope_id=scope_id,
        original_plan_path="plans/in-progress/original.md",
        plan_text=original_text,
        checkpoint_index=3,
        plan_ref=plan_ref,
        checkpoint_ref=checkpoint_ref,
    )
    artifact = write_envelope_atomic(
        envelope,
        run_dir / "scopes" / envelope.scope_digest,
    )
    artifact_bytes = artifact.read_bytes()
    boundary = {
        "context_schema_version": 4,
        "original_plan_path": str(original),
        "active_plan_path": str(overlay),
        "active_plan_content": overlay_text,
        "original_plan_content": original_text,
        "active_implementation_scope": {
            "scope_id": scope_id,
            "original_plan_path": str(original),
            "checkpoint_index": 3,
            "checkpoint_name": "Checkpoint 3: Third",
            "opened_turn_number": 1,
            "awaiting_review": True,
        },
        "envelope_artifact_path": (
            f"scopes/{envelope.scope_digest}/envelope.json"
        ),
        "envelope_artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        "envelope_canonical_sha256": envelope.canonical_envelope_sha256,
        "proposed_transition": "implement",
    }
    return (
        run_dir,
        repo,
        original,
        overlay,
        boundary,
        original_text,
        overlay_text,
    )


def test_lite_and_full_context_keep_plan_boundary(tmp_path: Path) -> None:
    run_dir, plan = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="complete semantic answer")

    lite = build_manager_context(run_dir, level="lite")
    full = build_manager_context(run_dir, level="full")

    assert lite["schema_version"] == 1
    assert lite["active_plan_content"] is None
    assert "Secret implementation plan" not in json.dumps(lite)
    assert full["active_plan_content"] == plan.read_text(encoding="utf-8")
    assert lite["finished_turn"]["semantic_result"]["result"] == "complete semantic answer"
    assert lite["finished_turn"]["raw_artifacts"][0]["path"] == "turns/turn-001/stdout.txt"


def test_context_exposes_compact_active_implementation_scope(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    _write_turn(run_dir, 1, step="review", role="reviewer", stdout="rejected")
    scope = {
        "scope_id": "plan.md::checkpoint-1::context",
        "checkpoint_index": 1,
        "checkpoint_name": "Context",
        "awaiting_review": True,
        "attempt_count": 2,
        "attempt_teams": ["base", "high"],
        "attempt_selectors": ["codex.mini", "codex.high"],
        "most_recent_team": "high",
        "upgrade_depth": 1,
    }

    context = build_manager_context(
        run_dir,
        boundary={
            "context_schema_version": 2,
            "active_implementation_scope": scope,
            "implementation_upgrade": {
                "available": True,
                "source_team": "high",
                "target_team": "max",
            },
        },
    )

    controller = context["controller_state"]
    assert controller["active_implementation_scope"] == scope
    assert controller["eligible_upgrade"]["source_team"] == "high"
    assert controller["eligible_upgrade"]["target_team"] == "max"


def test_structured_semantics_and_bounded_large_trace_reference(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    stream = '\n'.join((json.dumps({"role": "assistant", "content": "first"}), json.dumps({"type": "result", "result": "complete final answer"})))
    _write_turn(
        run_dir,
        1,
        step="implement",
        role="implementer",
        stdout=stream,
        output_contract="agent",
        semantic_output_source=STRUCTURED_TRANSPORT_OUTPUT_SOURCE,
    )
    turn_dir = run_dir / "turns" / "turn-001"
    (turn_dir / "stderr.txt").write_text("x" * (DIAGNOSTIC_LIMIT + 100), encoding="utf-8")

    context = build_manager_context(run_dir)

    assert extract_semantic_result(
        stream, output_source=STRUCTURED_TRANSPORT_OUTPUT_SOURCE
    ).result == "complete final answer"
    assert context["finished_turn"]["semantic_result"]["extraction"] == "structured_stream"
    assert len(context["finished_turn"]["diagnostics"]["stderr_excerpt"]) < DIAGNOSTIC_LIMIT + 100
    assert context["finished_turn"]["raw_artifacts"][1]["byte_size"] == DIAGNOSTIC_LIMIT + 100
    assert extract_semantic_result(
        '{"type": "unknown"}',
        output_source=STRUCTURED_TRANSPORT_OUTPUT_SOURCE,
    ).fallback is True


def test_structured_semantics_excludes_tool_and_echoed_prompt_events() -> None:
    stream = "\n".join(
        (
            '{"type":"thread.started","thread_id":"session-123"}',
            '{"type":"item.completed","thread_id":"session-123",'
            '"item":{"type":"command_execution",'
            '"aggregated_output":"AFLOW_STOP: HISTORY: old tool output"}}',
            '{"type":"message.completed","thread_id":"session-123",'
            '"role":"user","text":"AFLOW_STOP: HISTORY: echoed prompt"}',
            '{"type":"message.completed","thread_id":"session-123",'
            '"role":"assistant","text":"approved"}',
        )
    )
    assert extract_semantic_result(
        stream, output_source=STRUCTURED_TRANSPORT_OUTPUT_SOURCE
    ).result == "approved"


def test_agent_output_contract_keeps_diagnostic_markers_out_of_context(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    stderr = "tool transcript\nAFLOW_STOP: HISTORY: old tool output\n"
    _write_turn(
        run_dir,
        1,
        step="implement",
        role="implementer",
        stdout="approved final output\n",
        stderr=stderr,
        output_contract="agent",
    )

    context = build_manager_context(run_dir, boundary={"context_schema_version": 3})

    assert context["finished_turn"]["detected_stop"] == []
    assert context["scope_pressure_detected"] is False
    assert (run_dir / "turns" / "turn-001" / "stderr.txt").read_text(
        encoding="utf-8"
    ) == stderr


def test_agent_context_detects_structured_assistant_stop_only(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    stdout = "\n".join(
        (
            '{"type":"thread.started","thread_id":"session-123"}',
            '{"type":"item.completed","thread_id":"session-123",'
            '"item":{"type":"command_execution",'
            '"aggregated_output":"AFLOW_STOP: HISTORY: tool output"}}',
            '{"type":"message.completed","thread_id":"session-123",'
            '"role":"user","text":"AFLOW_STOP: HISTORY: echoed prompt"}',
            '{"type":"message.completed","thread_id":"session-123",'
            '"role":"assistant","text":"AFLOW_STOP: current assistant stop"}',
        )
    )
    _write_turn(
        run_dir,
        1,
        step="implement",
        role="implementer",
        stdout=stdout,
        stderr="AFLOW_STOP: HISTORY: diagnostic output\n",
        output_contract="agent",
        semantic_output_source=STRUCTURED_TRANSPORT_OUTPUT_SOURCE,
    )

    context = build_manager_context(run_dir, boundary={"context_schema_version": 3})

    assert context["finished_turn"]["detected_stop"] == [
        "current assistant stop"
    ]


def test_agent_context_keeps_scope_pressure_after_final_text_json(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    stdout = (
        "Diagnostic details:\n"
        "```json\n"
        '{"ok":false}\n'
        "```\n"
        "AFLOW_SCOPE_PRESSURE: split this checkpoint\n"
    )
    _write_turn(
        run_dir,
        1,
        step="implement",
        role="implementer",
        stdout=stdout,
        output_contract="agent",
    )

    context = build_manager_context(run_dir, boundary={"context_schema_version": 3})

    assert context["finished_turn"]["detected_stop"] == []
    assert context["scope_pressure_detected"] is True
    assert context["controller_state"]["scope_pressure_detected"] is True


def test_progress_detects_alternating_reviewer_non_convergence(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="attempt")
    _write_turn(run_dir, 2, step="review", role="reviewer", stdout="rejected")
    _write_turn(run_dir, 3, step="implement", role="implementer", stdout="attempt")
    _write_turn(run_dir, 4, step="review", role="reviewer", stdout="rejected")

    context = build_manager_context(run_dir)

    progress = context["controller_state"]["progress"]
    assert progress["unchanged_snapshot_turns"] == 4
    assert progress["same_step_stall_turns"] == 1
    assert context["controller_state"]["semantic_stall_count"] == 1
    assert progress["reviewer_rejection_count"] == 2
    assert progress["reviewer_non_convergence"] is True
    assert len(context["run_extract"]) == 4

    in_memory = build_manager_context(run_dir, turns=[json.loads((run_dir / "turns" / "turn-004" / "result.json").read_text(encoding="utf-8")) | {"_turn_dir": run_dir / "turns" / "turn-004"}], run_metadata={"plan_path": str(run_dir.parent.parent.parent / "plans" / "in-progress" / "plan.md")})
    assert in_memory["finished_turn"]["turn_number"] == 4


def test_followup_plan_reopening_checkpoint_counts_as_rejection(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    before = _snapshot()
    before["current_checkpoint_index"] = 2
    before["current_checkpoint_name"] = "Checkpoint 2: Next"
    after = _snapshot()
    _write_turn(
        run_dir,
        1,
        step="review",
        role="reviewer",
        stdout="rejected with a focused repair plan",
        before=before,
        after=after,
    )
    result_path = run_dir / "turns" / "turn-001" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["conditions"] = {"NEW_PLAN_EXISTS": True}
    _write_json(result_path, result)
    scope = {
        "scope_id": "plan.md::checkpoint-1::context",
        "opened_turn_number": 1,
        "carried_reviewer_rejection_count": 0,
    }

    context = build_manager_context(
        run_dir,
        boundary={
            "context_schema_version": 2,
            "active_implementation_scope": scope,
        },
    )

    assert context["controller_state"]["reviewer_rejection_count"] == 1
    assert scoped_reviewer_rejection_count(run_dir, scope) == 1


def test_progress_detects_long_unchanged_tail(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    for number in range(1, 5):
        _write_turn(run_dir, number, step="implement", role="implementer", stdout="no semantic plan change")

    context = build_manager_context(run_dir)

    assert context["controller_state"]["progress"]["unchanged_snapshot_turns"] == 4
    assert context["controller_state"]["progress"]["same_step_stall_turns"] == 4


def test_progress_is_scoped_to_open_implementation_scope(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    _write_turn(run_dir, 1, step="review", role="reviewer", stdout="CP1 rejected")
    _write_turn(run_dir, 2, step="implement", role="implementer", stdout="CP1 repair")
    _write_turn(run_dir, 3, step="review", role="reviewer", stdout="CP1 rejected again")
    _write_turn(run_dir, 4, step="implement", role="implementer", stdout="CP1 repair")
    _write_turn(run_dir, 5, step="implement", role="implementer", stdout="CP2 first attempt")

    context = build_manager_context(
        run_dir,
        boundary={
            "context_schema_version": 2,
            "active_implementation_scope": {
                "scope_id": "plan.md::checkpoint-2::second",
                "opened_turn_number": 5,
            }
        },
    )

    progress = context["controller_state"]["progress"]
    assert progress["reviewer_rejection_count"] == 0
    assert progress["reviewer_non_convergence"] is False
    assert context["controller_state"]["progress_scope"] == {
        "scope_id": "plan.md::checkpoint-2::second",
        "opened_turn_number": 5,
    }
    assert len(context["run_extract"]) == 5


def test_legacy_scope_boundary_preserves_legacy_progress_shape(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="attempt")
    _write_turn(run_dir, 2, step="review", role="reviewer", stdout="review")

    context = build_manager_context(
        run_dir,
        boundary={"active_implementation_scope": {"scope_id": "legacy-scope"}},
    )

    controller = context["controller_state"]
    assert controller["semantic_stall_count"] == 2
    assert "same_step_stall_turns" not in controller["progress"]
    assert "progress_scope" not in controller


def test_legacy_null_scope_boundary_preserves_stored_context_shape(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    _write_turn(run_dir, 1, step="review", role="reviewer", stdout="rejected")
    _write_turn(run_dir, 2, step="implement", role="implementer", stdout="repair")

    context = build_manager_context(
        run_dir,
        boundary={"active_implementation_scope": None},
    )

    controller = context["controller_state"]
    assert controller["reviewer_rejection_count"] == 1
    assert "same_step_stall_turns" not in controller["progress"]
    assert "progress_scope" not in controller


def test_current_run_explicit_null_scope_clears_reviewer_progress(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    _write_turn(run_dir, 1, step="review", role="reviewer", stdout="rejected")
    _write_turn(run_dir, 2, step="implement", role="implementer", stdout="repair")
    run_json_path = run_dir / "run.json"
    run_json = json.loads(run_json_path.read_text(encoding="utf-8"))
    run_json["active_implementation_scope"] = None
    _write_json(run_json_path, run_json)

    context = build_manager_context(run_dir)

    progress = context["controller_state"]["progress"]
    assert progress["reviewer_rejection_count"] == 0
    assert progress["reviewer_non_convergence"] is False


def test_resumed_scope_carries_prior_reviewer_rejections(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="resumed worker")

    context = build_manager_context(
        run_dir,
        boundary={
            "context_schema_version": 2,
            "active_implementation_scope": {
                "scope_id": "plan.md::checkpoint-1::context",
                "opened_turn_number": 1,
                "carried_reviewer_rejection_count": 2,
            },
        },
    )

    progress = context["controller_state"]["progress"]
    assert progress["reviewer_rejection_count"] == 2
    assert progress["reviewer_non_convergence"] is True


def test_context_tolerates_invalid_plan_and_prior_manager_artifacts(tmp_path: Path) -> None:
    run_dir, plan = _run(tmp_path)
    plan.write_text("### [x] Checkpoint 1: Context\n- [ ] broken state\n", encoding="utf-8")
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="AFLOW_STOP: deliberate stop")
    _write_json(run_dir / "manager" / "decision-001" / "result.json", {"decision_number": 1, "status": "accepted", "action": "retry_current_step", "reason": "synthetic"})

    context = build_manager_context(run_dir)

    assert "inconsistent checkpoint state" in context["plan_state"]["parse_error"]
    assert context["finished_turn"]["detected_stop"] == ["deliberate stop"]
    assert context["run_extract"][-1]["kind"] == "manager_decision"


def test_context_derives_duration_for_legacy_turn_artifact(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="done")
    result_path = run_dir / "turns" / "turn-001" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["started_at"] = "2026-07-28T10:00:00+00:00"
    result["finished_at"] = "2026-07-28T10:00:12.500000+00:00"
    _write_json(result_path, result)

    context = build_manager_context(run_dir)

    assert context["finished_turn"]["duration_seconds"] == 12.5


def test_legacy_null_scope_context_rebuild_uses_stored_plan_state(
    tmp_path: Path,
) -> None:
    run_dir, plan = _run(tmp_path)
    _write_turn(run_dir, 1, step="review", role="reviewer", stdout="rejected")
    boundary = {
        "active_implementation_scope": None,
        "proposed_transition": "implement",
    }
    run_metadata = {
        "plan_path": str(plan),
        "active_plan_path": str(plan),
        "original_plan_path": str(plan),
        "team": "base",
        "turns_completed": 1,
        "max_turns": 5,
    }
    stored = build_manager_context(
        run_dir,
        level="lite",
        trigger="post_turn",
        decision_number=1,
        run_metadata=run_metadata,
        boundary=boundary,
    )
    decision_dir = run_dir / "manager" / "decision-001"
    _write_json(decision_dir / "context.json", stored)
    _write_json(decision_dir / "result.json", {
        "decision_number": 1,
        "finalized_turn_number": 1,
        "level": "lite",
        "status": "accepted",
    })
    _write_json(decision_dir / "boundary.json", {
        "decision_number": 1,
        "trigger": "post_turn",
        "run_metadata": run_metadata,
        "boundary": boundary,
        "active_plan_content": None,
    })
    plan.write_text(
        "# Changed\n\n### [x] Checkpoint 1: Context\n- [x] advanced\n",
        encoding="utf-8",
    )

    rebuilt = analyze_runs(AnalyzeRequest(
        repo_root=run_dir.parent.parent.parent,
        run_id=run_dir.name,
        manager_context="lite",
        turn=1,
    ))

    assert rebuilt == stored


def test_schema_v2_analysis_rebuilds_validated_repartition_artifact_references(
    tmp_path: Path,
) -> None:
    run_dir, plan = _run(tmp_path)
    _write_turn(run_dir, 1, step="review", role="reviewer", stdout="approved")
    artifact_contents = {
        "scope/envelope.json": b'{"scope":"one"}\n',
        "manager/repartition/proposal.json": b'{"children":[]}\n',
        "manager/repartition/candidate.md": b"# candidate\n",
        "manager/repartition/mechanical.json": b'{"valid":true}\n',
        "manager/repartition/verdict.json": b'{"verdict":"accept"}\n',
    }
    for relative, content in artifact_contents.items():
        artifact = run_dir / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(content)
    record = {
        "schema_version": 1,
        "decision_number": 1,
        "scope_id": "scope-1",
        "generation_id": "gen-1",
        "envelope_sha256": "a" * 64,
        "envelope_artifact_sha256": hashlib.sha256(
            artifact_contents["scope/envelope.json"]
        ).hexdigest(),
        "source_plan_sha256": "b" * 64,
        "proposal_sha256": hashlib.sha256(
            artifact_contents["manager/repartition/proposal.json"]
        ).hexdigest(),
        "candidate_plan_sha256": hashlib.sha256(
            artifact_contents["manager/repartition/candidate.md"]
        ).hexdigest(),
        "partition_ids": ["part-1", "part-2"],
        "child_summaries": ["Part one", "Part two"],
        "current_disposition": "review_current_partition",
        "resolved_target_step": "review",
        "resolved_target_role": "reviewer",
        "current_partition_id": "part-1",
        "scope_pressure_reason": "split this checkpoint",
        "envelope_artifact_path": "scope/envelope.json",
        "proposal_artifact_path": "manager/repartition/proposal.json",
        "candidate_artifact_path": "manager/repartition/candidate.md",
        "mechanical_validation_artifact_path": (
            "manager/repartition/mechanical.json"
        ),
        "semantic_verdict_artifact_path": "manager/repartition/verdict.json",
    }
    boundary = {
        "context_schema_version": 3,
        "active_implementation_scope": None,
        "proposed_transition": "implement",
        "repartition_history": [record],
    }
    run_metadata = {
        "plan_path": str(plan),
        "active_plan_path": str(plan),
        "original_plan_path": str(plan),
        "team": "base",
        "turns_completed": 1,
        "max_turns": 5,
    }
    stored = build_manager_context(
        run_dir,
        level="full",
        trigger="post_turn",
        decision_number=2,
        run_metadata=run_metadata,
        boundary=boundary,
    )
    stored["controller_state"]["checkpoint_repartitions"] = [record]
    decision_dir = run_dir / "manager" / "decision-002"
    _write_json(decision_dir / "context.json", stored)
    _write_json(decision_dir / "result.json", {
        "decision_number": 2,
        "finalized_turn_number": 1,
        "level": "full",
        "status": "accepted",
    })
    _write_json(decision_dir / "boundary.json", {
        "decision_number": 2,
        "trigger": "post_turn",
        "run_metadata": run_metadata,
        "boundary": boundary,
        "active_plan_content": plan.read_text(encoding="utf-8"),
    })

    rebuilt = analyze_runs(AnalyzeRequest(
        repo_root=run_dir.parent.parent.parent,
        run_id=run_dir.name,
        manager_context="full",
        turn=1,
    ))

    assert rebuilt == stored
    assert rebuilt["controller_state"]["checkpoint_repartitions"] == [record]


def test_stop_parser_ignores_fences_and_placeholder_examples() -> None:
    text = "```text\nAFLOW_STOP: <reason>\n```\nAFLOW_STOP: <reason>\nAFLOW_STOP: actual blocker\n"
    assert extract_aflow_stop(text) == ["actual blocker"]
    assert detect_stop_marker(text, "AFLOW_STOP: stderr blocker") == "actual blocker"


def test_rejection_summaries_normalize_bound_and_extract_summary(tmp_path: Path) -> None:
    assert summarize_review_rejection("  first\n\n second  ") == "first second"
    assert summarize_review_rejection("x" * 481) == "x" * 479 + "…"
    assert summarize_review_rejection("é" * 481) == "é" * 479 + "…"
    assert summarize_review_rejection("") == (
        "Reviewer rejected this implementation; see the review stdout artifact for details."
    )
    repair = tmp_path / "repair.md"
    repair.write_text("# Repair\n\n## Summary\n  Fix   [red] output.\n\n## Steps\n- ignored\n", encoding="utf-8")
    assert summarize_repair_plan(repair) == "Fix [red] output."
    assert summarize_repair_plan(tmp_path / "missing.md") is None


# --- Schema v2 context tests ---


def test_schema_v1_preserved_for_selector_2_boundary(tmp_path: Path) -> None:
    """Selector-2 boundaries must produce byte-for-byte identical schema-v1."""
    run_dir, _ = _run(tmp_path)
    _write_turn(
        run_dir,
        1,
        step="implement",
        role="implementer",
        stdout="output",
        stderr=INCIDENT_SIGNAL_TEXT,
    )

    context_v1 = build_manager_context(run_dir)
    context_v2_boundary = build_manager_context(
        run_dir,
        boundary={"context_schema_version": 2},
    )

    assert context_v1["schema_version"] == 1
    assert context_v2_boundary["schema_version"] == 1
    # Same output (ignoring run_extract ordering which is already sort_keys=True)
    assert context_v1 == context_v2_boundary
    diagnostics = context_v1["finished_turn"]["diagnostics"]
    assert diagnostics["signals"] == INCIDENT_SIGNAL_NAMES
    assert "signal_provenance" not in diagnostics


def test_schema_v2_produced_for_selector_3_boundary(tmp_path: Path) -> None:
    """Selector-3 boundaries produce schema v2 with new evidence fields."""
    run_dir, _ = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="output")

    context = build_manager_context(
        run_dir,
        boundary={"context_schema_version": 3},
    )

    assert context["schema_version"] == 2
    assert "scope_pressure_detected" in context
    assert "change_surface_evidence" in context
    assert "active_scope_rejection_ledger" in context
    assert "manager_decisions" in context
    assert context["scope_pressure_detected"] is False


def test_schema_v2_detects_scope_pressure(tmp_path: Path) -> None:
    """Schema v2 context detects AFLOW_SCOPE_PRESSURE in turn output."""
    run_dir, _ = _run(tmp_path)
    _write_turn(
        run_dir, 1, step="implement", role="implementer",
        stdout="AFLOW_SCOPE_PRESSURE: checkpoint too large\nsome output",
    )

    context = build_manager_context(
        run_dir,
        boundary={"context_schema_version": 3},
    )

    assert context["schema_version"] == 2
    assert context["scope_pressure_detected"] is True


def test_schema_v2_includes_change_surface_evidence(tmp_path: Path) -> None:
    """Schema v2 includes change-surface progress evidence."""
    run_dir, _ = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="output")
    _write_turn(run_dir, 2, step="implement", role="implementer", stdout="output")

    context = build_manager_context(
        run_dir,
        boundary={"context_schema_version": 3},
    )

    evidence = context["change_surface_evidence"]
    assert evidence["unchanged_snapshot_turns"] == 2
    assert "reviewer_rejection_count" in evidence
    assert "reviewer_non_convergence" in evidence


def test_schema_v2_lite_context_omits_plan_content(tmp_path: Path) -> None:
    """Lite context in schema v2 must not include active/original plan content."""
    run_dir, _ = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="output")

    context = build_manager_context(
        run_dir,
        level="lite",
        boundary={"context_schema_version": 3},
    )

    assert context["active_plan_content"] is None
    assert context["original_plan_content"] is None
    assert context["plan_content_disclosure"] == {
        "active_plan_content": "intentionally_omitted",
        "original_plan_content": "intentionally_omitted",
    }
    assert "Secret implementation plan" not in json.dumps(context)
    assert "private instruction" not in json.dumps(context)


def test_schema_v2_classifies_diagnostics_by_stream_and_turn_outcome(
    tmp_path: Path,
) -> None:
    run_dir, _ = _run(tmp_path)
    _write_turn(
        run_dir,
        1,
        step="implement",
        role="implementer",
        stdout="completed successfully",
        stderr=INCIDENT_SIGNAL_TEXT + "x" * DIAGNOSTIC_LIMIT,
    )

    successful = build_manager_context(
        run_dir,
        level="lite",
        boundary={"context_schema_version": 3},
    )

    diagnostics = successful["finished_turn"]["diagnostics"]
    assert diagnostics["signals"] == []
    assert diagnostics["signal_provenance"] == []
    assert diagnostics["stderr_excerpt"].endswith(
        "\n[diagnostic excerpt truncated]"
    )
    assert successful["finished_turn"]["raw_artifacts"][1]["byte_size"] > (
        DIAGNOSTIC_LIMIT
    )

    _write_turn(
        run_dir,
        2,
        step="implement",
        role="implementer",
        stdout=INCIDENT_SIGNAL_TEXT,
        stderr="diagnostic only",
    )
    semantic = build_manager_context(
        run_dir,
        level="lite",
        boundary={"context_schema_version": 3},
    )["finished_turn"]["diagnostics"]
    assert semantic["signals"] == INCIDENT_SIGNAL_NAMES
    assert semantic["signal_provenance"] == [
        {
            "name": name,
            "source": "semantic_stdout",
            "trust_class": "semantic_output",
        }
        for name in INCIDENT_SIGNAL_NAMES
    ]

    _write_turn(
        run_dir,
        3,
        step="implement",
        role="implementer",
        stdout="failed",
        stderr=INCIDENT_SIGNAL_TEXT,
        status="failed",
        returncode=1,
    )
    failed = build_manager_context(
        run_dir,
        level="lite",
        boundary={"context_schema_version": 3},
    )["finished_turn"]["diagnostics"]
    assert failed["signals"] == INCIDENT_SIGNAL_NAMES
    assert failed["signal_provenance"] == [
        {
            "name": name,
            "source": "failure_stderr",
            "trust_class": "failure_diagnostic",
        }
        for name in INCIDENT_SIGNAL_NAMES
    ]


def test_lite_context_has_bounded_controller_owned_note_scope(tmp_path: Path) -> None:
    run_dir, plan = _run(tmp_path)
    plan.write_text(
        "# Secret implementation plan\n\n"
        "## Files\n\nMay modify only:\n\n"
        "- `aflow/manager.py`\n- `tests/test_runtime.py`\n\n"
        "### [ ] Checkpoint 1: Context\n- [ ] private instruction\n",
        encoding="utf-8",
    )
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="output")

    context = build_manager_context(
        run_dir,
        level="lite",
        boundary={"context_schema_version": 3, "target_plan_identity": "plan.md::checkpoint-1"},
        active_plan_content=plan.read_text(encoding="utf-8"),
    )

    assert context["manager_note_scope"] == {
        "active_plan_identity": "plan.md::checkpoint-1",
        "allowed_paths": ["aflow/manager.py", "tests/test_runtime.py"],
        "prohibited_paths": [],
        "authority": "controller_owned",
        "constraints_complete": True,
    }
    assert "Secret implementation plan" not in json.dumps(context)


def test_note_scope_collects_only_explicit_file_list_entries() -> None:
    scope = build_manager_note_scope(
        active_plan_identity="plan.md::checkpoint-1",
        active_plan_content=(
            "## Files\n\nMay modify only:\n"
            "- `aflow/manager.py`\n"
            "- `tests/test_runtime.py`: regression coverage\n\n"
            "Run `uv run pytest tests/test_runtime.py`.\n"
            "- Run `uv run pytest tests/test_runtime.py`.\n"
            "- May create/modify: `aflow/run_state.py`\n"
            "### [ ] Checkpoint 1\n"
        ),
    )

    assert scope["allowed_paths"] == [
        "aflow/manager.py", "tests/test_runtime.py", "aflow/run_state.py",
    ]
    assert scope["constraints_complete"] is True


def test_note_scope_marks_unrepresentable_explicit_constraints_incomplete() -> None:
    too_long_path = "a" * 241
    scope = build_manager_note_scope(
        active_plan_identity="plan.md::checkpoint-1",
        active_plan_content=f"May modify only:\n- `{too_long_path}`\n",
    )

    assert scope["allowed_paths"] == []
    assert scope["constraints_complete"] is False

    glob_scope = build_manager_note_scope(
        active_plan_identity="plan.md::checkpoint-1",
        active_plan_content="Must not touch:\n- `plans/**`\n",
    )
    assert glob_scope["prohibited_paths"] == []
    assert glob_scope["constraints_complete"] is False

    long_identity = "p" * 513
    bounded_identity_scope = build_manager_note_scope(
        active_plan_identity=long_identity,
        active_plan_content=None,
    )
    assert bounded_identity_scope["active_plan_identity"] == (
        "sha256:" + hashlib.sha256(long_identity.encode("utf-8")).hexdigest()
    )


def test_note_scope_parses_plain_paths_and_fails_closed_on_ambiguous_items() -> None:
    plain = build_manager_note_scope(
        active_plan_identity="plan.md::checkpoint-1",
        active_plan_content=(
            "May create or modify: aflow/manager.py, tests/test_manager.py\n"
            "Must not touch:\n- aflow/run_state.py\n"
        ),
    )
    assert plain["allowed_paths"] == ["aflow/manager.py", "tests/test_manager.py"]
    assert plain["prohibited_paths"] == ["aflow/run_state.py"]
    assert plain["constraints_complete"] is True

    extensionless = build_manager_note_scope(
        active_plan_identity="plan.md::checkpoint-1",
        active_plan_content=(
            "May modify only:\n"
            "- aflow/manager.py\n"
            "- Makefile\n"
            "- Dockerfile: container build\n"
            "Must not touch: LICENSE\n"
        ),
    )
    assert extensionless["allowed_paths"] == [
        "aflow/manager.py", "Makefile", "Dockerfile",
    ]
    assert extensionless["prohibited_paths"] == ["LICENSE"]
    assert extensionless["constraints_complete"] is True

    for text in (
        "May modify only:\n- plans/**\n",
        "May modify only:\n- /tmp/escape.py\n",
        "May modify only:\n- ../escape.py\n",
        "May modify only:\n- worker implementation\n",
        "May modify only:\n- aflow/manager.py\n- worker implementation\n",
    ):
        scope = build_manager_note_scope(
            active_plan_identity="plan.md::checkpoint-1",
            active_plan_content=text,
        )
        assert scope["constraints_complete"] is False


def test_lite_context_exposes_retry_scope_without_plan_prose(tmp_path: Path) -> None:
    run_dir, plan = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="output")
    proposed_scope = {
        "active_plan_identity": "proposed.md::checkpoint-1",
        "allowed_paths": ["proposed.py"],
        "prohibited_paths": [],
        "authority": "controller_owned",
        "constraints_complete": True,
    }
    retry_scope = {
        "active_plan_identity": "current.md::checkpoint-1",
        "allowed_paths": ["current.py"],
        "prohibited_paths": [],
        "authority": "controller_owned",
        "constraints_complete": True,
    }
    context = build_manager_context(
        run_dir,
        level="lite",
        boundary={
            "context_schema_version": 3,
            "manager_note_scope": proposed_scope,
            "retry_manager_note_scope": retry_scope,
        },
        active_plan_content=plan.read_text(encoding="utf-8"),
    )
    assert context["manager_note_scope"] == proposed_scope
    assert context["retry_manager_note_scope"] == retry_scope
    assert "Checkpoint One" not in json.dumps(context)


def test_schema_v2_full_includes_active_plan_content(tmp_path: Path) -> None:
    """Full context in schema v2 includes active plan content."""
    run_dir, _ = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="output")

    context = build_manager_context(
        run_dir,
        level="full",
        boundary={"context_schema_version": 3},
    )

    assert context["active_plan_content"] is not None
    assert "Secret implementation plan" in context["active_plan_content"]
    assert context["original_plan_content"] is not None
    assert context["plan_content_disclosure"] == {
        "active_plan_content": "included",
        "original_plan_content": "included",
    }


def test_schema_v2_full_marks_missing_plan_bodies_unavailable(tmp_path: Path) -> None:
    run_dir, plan = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="output")
    plan.unlink()

    context = build_manager_context(
        run_dir,
        level="full",
        boundary={"context_schema_version": 3},
    )

    assert not context["active_plan_content"]
    assert context["original_plan_content"] is None
    assert context["plan_content_disclosure"] == {
        "active_plan_content": "unavailable",
        "original_plan_content": "unavailable",
    }


def test_schema_v2_full_uses_validated_immutable_envelope_payload(tmp_path: Path) -> None:
    run_dir, plan = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="output")

    context = build_manager_context(
        run_dir,
        level="full",
        boundary=_enveloped_boundary(run_dir, plan),
    )

    envelope = context["envelope"]
    assert envelope is not None
    assert envelope["validated"] is True
    assert envelope["available"] is True
    assert envelope["plan_text"] == plan.read_text(encoding="utf-8")
    assert envelope["checkpoint_text"] == (
        "### [ ] Checkpoint 1: Context\n- [ ] private instruction\n"
    )
    assert envelope["plan_sha256"] == hashlib.sha256(
        plan.read_bytes()
    ).hexdigest()
    assert envelope["checkpoint_byte_start"] < envelope["checkpoint_byte_end"]
    assert envelope["checkpoint_line_start"] == 3
    assert envelope["heading_prefix"] == "### [ ] Checkpoint 1: Context\n"
    assert envelope["source_blocks"]
    assert context["original_plan_content"] == plan.read_text(encoding="utf-8")
    assert context["controller_state"]["repartition_evidence"] == {
        "status": "validated"
    }


def test_schema_v2_lite_redacts_validated_envelope_content(tmp_path: Path) -> None:
    run_dir, plan = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="output")

    context = build_manager_context(
        run_dir,
        level="lite",
        boundary=_enveloped_boundary(run_dir, plan),
    )

    envelope = context["envelope"]
    assert set(envelope) == {
        "available",
        "validated",
        "content_included",
        "artifact_path",
        "artifact_sha256",
        "canonical_envelope_sha256",
    }
    assert envelope["available"] is True
    assert envelope["validated"] is True
    assert envelope["content_included"] is False
    assert "Secret implementation plan" not in json.dumps(context)
    assert "private instruction" not in json.dumps(context)


def test_schema_v2_lite_redacts_exact_reviewer_output(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    exact_reviewer_output = "REJECTED: secret reviewer evidence"
    _write_turn(
        run_dir,
        1,
        step="review",
        role="reviewer",
        stdout=exact_reviewer_output,
    )

    context = build_manager_context(
        run_dir,
        level="lite",
        boundary={"context_schema_version": 3},
    )

    serialized = json.dumps(context)
    assert exact_reviewer_output not in serialized
    assert context["finished_turn"]["semantic_result"]["result"].startswith(
        "Reviewer output withheld"
    )
    assert context["finished_turn"]["diagnostics"]["stdout_excerpt"].startswith(
        "Reviewer output withheld"
    )
    assert context["run_extract"][0]["semantic_summary"].startswith(
        "Reviewer output withheld"
    )


def test_schema_v2_marks_incomplete_envelope_evidence_unavailable(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="output")

    context = build_manager_context(
        run_dir,
        level="full",
        boundary={
            "context_schema_version": 3,
            "active_implementation_scope": {
                "scope_id": "plans/in-progress/plan.md::checkpoint-1::context",
                "checkpoint_index": 1,
                "checkpoint_name": "Checkpoint 1: Context",
            },
            "envelope_artifact_path": "scopes/missing/envelope.json",
        },
    )

    assert context["envelope"] == {
        "available": False,
        "validated": False,
        "reason": (
            "the active implementation scope has incomplete immutable envelope "
            "references"
        ),
    }
    assert context["controller_state"]["repartition_evidence"]["status"] == "unavailable"


# --- Schema v2 with captured boundary inputs ---


def test_schema_v2_rejection_ledger_from_boundary(tmp_path: Path) -> None:
    """Active-scope rejection ledger populated from boundary rejection history."""
    run_dir, _ = _run(tmp_path)
    _write_turn(run_dir, 1, step="review", role="reviewer", stdout="rejected")

    context = build_manager_context(
        run_dir,
        boundary={
            "context_schema_version": 3,
            "active_implementation_scope": {
                "scope_id": "plan.md::checkpoint-1::context",
                "opened_turn_number": 1,
            },
            "review_rejection_history": [
                {
                    "scope_id": "plan.md::checkpoint-1::context",
                    "rejection_number": 1,
                    "source_run_id": "run-1",
                    "review_turn_number": 1,
                    "review_step_name": "review",
                    "reviewer_selector": "codex.nano",
                    "checkpoint_index": 1,
                    "checkpoint_name": "Context",
                    "reviewed_implementation_turn_number": 0,
                    "reviewed_worker_team": "base",
                    "reviewed_worker_selector": "codex.default",
                    "review_summary": "incomplete",
                    "repair_plan_summary": None,
                    "review_stdout_artifact_path": "turns/turn-001/stdout.txt",
                    "repair_plan_path": None,
                },
                {
                    "scope_id": "plan.md::checkpoint-1::context",
                    "rejection_number": 2,
                    "source_run_id": "run-1",
                    "review_turn_number": 2,
                    "review_step_name": "review",
                    "reviewer_selector": "codex.high",
                    "checkpoint_index": 1,
                    "checkpoint_name": "Context",
                    "reviewed_implementation_turn_number": 1,
                    "reviewed_worker_team": "high",
                    "reviewed_worker_selector": "codex.worker-high",
                    "review_summary": "still incomplete",
                    "repair_plan_summary": "fix x",
                    "review_stdout_artifact_path": "turns/turn-002/stdout.txt",
                    "repair_plan_path": "plans/repair.md",
                },
            ],
        },
    )

    assert context["schema_version"] == 2
    ledger = context["active_scope_rejection_ledger"]
    assert len(ledger) == 2
    assert ledger[0]["rejection_number"] == 1
    assert ledger[0]["reviewer_selector"] == "codex.nano"
    assert ledger[1]["rejection_number"] == 2
    assert ledger[1]["reviewed_worker_team"] == "high"


def test_schema_v2_implementation_attempts_from_boundary(tmp_path: Path) -> None:
    """Implementation attempts filtered to active scope from boundary."""
    run_dir, _ = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="worker", stdout="output")

    context = build_manager_context(
        run_dir,
        boundary={
            "context_schema_version": 3,
            "active_implementation_scope": {
                "scope_id": "plan.md::checkpoint-1::context",
                "opened_turn_number": 1,
            },
            "implementation_attempts": {
                "plan.md::checkpoint-1::context": [
                    {
                        "turn_number": 1,
                        "step_name": "implement",
                        "role": "worker",
                        "team": "base",
                        "selector": "codex.default",
                        "outcome": "progress",
                        "manager_decision_number": None,
                    },
                    {
                        "turn_number": 3,
                        "step_name": "implement",
                        "role": "worker",
                        "team": "high",
                        "selector": "codex.worker-high",
                        "outcome": "progress",
                        "manager_decision_number": 2,
                    },
                ],
                "other-scope": [
                    {"turn_number": 5, "step_name": "other", "role": "worker", "team": "base", "selector": "x", "outcome": "progress"},
                ],
            },
        },
    )

    assert context["schema_version"] == 2
    attempts = context["implementation_attempts"]
    assert attempts is not None
    assert attempts["scope_id"] == "plan.md::checkpoint-1::context"
    assert len(attempts["attempts"]) == 2
    assert attempts["attempts"][0]["team"] == "base"
    assert attempts["attempts"][1]["team"] == "high"


def test_schema_v2_bounded_manager_decisions_excludes_current(tmp_path: Path) -> None:
    """Manager decisions only include those strictly before the current decision."""
    run_dir, _ = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="output")
    _write_json(
        run_dir / "manager" / "decision-001" / "result.json",
        {"decision_number": 1, "status": "accepted", "action": "continue", "reason": "first"},
    )
    _write_json(
        run_dir / "manager" / "decision-002" / "result.json",
        {"decision_number": 2, "status": "accepted", "action": "continue", "reason": "second"},
    )

    context = build_manager_context(
        run_dir,
        boundary={"context_schema_version": 3},
        decision_number=2,
    )

    assert context["schema_version"] == 2
    decisions = context["manager_decisions"]
    assert len(decisions) == 1
    assert decisions[0]["decision_number"] == 1
    assert decisions[0]["reason"] == "first"


def test_schema_v2_full_includes_latest_rejection_detail(tmp_path: Path) -> None:
    """Full context includes exact reviewer output from latest rejection artifact."""
    run_dir, _ = _run(tmp_path)
    review_stdout = "REJECTED: missing edge case handling in the implementation"
    _write_turn(run_dir, 1, step="review", role="reviewer", stdout=review_stdout)

    context = build_manager_context(
        run_dir,
        level="full",
        boundary={
            "context_schema_version": 3,
            "active_implementation_scope": {
                "scope_id": "plan.md::checkpoint-1::context",
                "opened_turn_number": 1,
            },
            "review_rejection_history": [
                {
                    "scope_id": "plan.md::checkpoint-1::context",
                    "rejection_number": 1,
                    "source_run_id": "run-1",
                    "review_turn_number": 1,
                    "review_step_name": "review",
                    "reviewer_selector": "codex.nano",
                    "checkpoint_index": 1,
                    "checkpoint_name": "Context",
                    "reviewed_implementation_turn_number": 0,
                    "reviewed_worker_team": "base",
                    "reviewed_worker_selector": "codex.default",
                    "review_summary": "incomplete",
                    "repair_plan_summary": None,
                    "review_stdout_artifact_path": "turns/turn-001/stdout.txt",
                    "repair_plan_path": None,
                },
            ],
        },
    )

    assert context["schema_version"] == 2
    latest = context["controller_state"].get("latest_full_rejection")
    assert latest is not None
    assert latest["rejection_number"] == 1
    assert latest["exact_reviewer_output"] is not None
    assert "missing edge case" in latest["exact_reviewer_output"]


def test_schema_v2_scope_pressure_reason_in_controller_state(tmp_path: Path) -> None:
    """Scope pressure reason from boundary propagated to controller_state."""
    run_dir, _ = _run(tmp_path)
    _write_turn(
        run_dir, 1, step="implement", role="implementer",
        stdout="AFLOW_SCOPE_PRESSURE: checkpoint too large\noutput",
    )

    context = build_manager_context(
        run_dir,
        boundary={
            "context_schema_version": 3,
            "scope_pressure_reason": "checkpoint too large",
        },
    )

    assert context["schema_version"] == 2
    assert context["scope_pressure_detected"] is True
    cs = context["controller_state"]
    assert cs["scope_pressure_detected"] is True
    assert cs["scope_pressure_reason"] == "checkpoint too large"


# --- Schema-v3 reference-only contexts (selector >= 4) ---
from aflow.manager_context import (
    MANAGER_CONTEXT_SCHEMA_VERSION_V2,
    MANAGER_CONTEXT_SCHEMA_VERSION_V3,
    MANAGER_INLINE_CONTEXT_MAX_BYTES,
    MANAGER_INLINE_CONTEXT_TARGET_BYTES,
    TRUNCATION_MARKER,
)


def _big_plan(text: str) -> str:
    """Deterministic filler making the plan ~64 KiB with unique sentinels."""
    filler = "\n".join(
        f"- task row {index:05d} filler body" for index in range(2_600)
    )
    return text + "\n" + filler + "\n"


def test_v3_context_is_reference_only_and_within_target_bytes(tmp_path: Path) -> None:
    run_dir, plan = _run(tmp_path)
    plan_text = _big_plan(plan.read_text(encoding="utf-8"))
    plan.write_text(plan_text, encoding="utf-8")
    _write_turn(
        run_dir,
        1,
        step="implement",
        role="implementer",
        stdout=("WORKER-SEMANTIC-SENTINEL-77aa\n" * 20),
    )
    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary["context_schema_version"] = 4

    context = build_manager_context(
        run_dir,
        level="full",
        boundary=boundary,
        active_plan_content=plan_text,
        capture_evidence=True,
    )

    assert context["schema_version"] == MANAGER_CONTEXT_SCHEMA_VERSION_V3
    serialized = json.dumps(context, sort_keys=True)
    assert "PLAN-BODY" not in serialized
    assert "task row" not in serialized
    assert "active_plan_content" not in context
    assert "original_plan_content" not in context
    assert "envelope" not in context or "plan_text" not in json.dumps(context["envelope"])
    evidence = context["evidence"]
    active = evidence["active_plan"]
    assert active["available"] is True
    assert active["reference"]["kind"] == "plan"
    assert active["reference"]["path"].startswith(".aflow/runs/run-1/evidence/plans/")
    assert len(active["reference"]["sha256"]) == 64
    assert evidence["original_plan"]["shared_with"] == "active_plan"
    checkpoint = evidence["checkpoint"]
    assert checkpoint["available"] is True
    assert checkpoint["checkpoint_index"] == 1
    assert checkpoint["checkpoint_name"] == "Checkpoint 1: Context"
    assert checkpoint["line_start"] >= 1
    assert checkpoint["byte_start"] < checkpoint["byte_end"]
    assert checkpoint["reference"]["kind"] == "checkpoint"
    assert context["plan_content_disclosure"] == {
        "active_plan": "referenced",
        "original_plan": "referenced",
        "checkpoint": "referenced",
    }
    assert context["run_extract"] == []
    assert context["manager_decisions"] == []
    assert context["active_scope_rejection_ledger"] == []
    assert context["implementation_attempts"] == {}
    history = evidence["manager_history"]
    assert history["available"] is True
    assert history["reference"]["kind"] == "manager_history"
    assert history["reference"]["path"].endswith(".json")
    history_payload = json.loads(
        (tmp_path / "repo" / history["reference"]["path"]).read_text(
            encoding="utf-8"
        )
    )
    assert history_payload["schema_version"] == 1
    assert history_payload["source_run_id"] == run_dir.name
    assert history_payload["sections"]["run_extract"]
    assert history_payload["sections"]["latest_turn"]["semantic_result"][
        "result"
    ].startswith("WORKER-SEMANTIC-SENTINEL-77aa")
    assert context["history_summary"] == {
        "total_turns": 1,
        "total_decisions": 0,
        "total_implementation_attempts": 0,
        "total_active_scope_rejections": 0,
        "latest_decision_number": None,
        "latest_decision_action": None,
        "latest_rejection_number": None,
        "reference_available": True,
        "coverage": {
            "turns": {"count": 1, "range": {"start": 1, "end": 1}},
            "decisions": {"count": 0, "range": None},
            "implementation_attempts": {"count": 0, "range": None},
            "active_scope_rejections": {"count": 0, "range": None},
        },
    }
    # The exact bytes live only in the evidence store.
    plan_artifact = (
        tmp_path / "repo" / ".aflow" / "runs" / "run-1" / "evidence" / "plans"
        / f"{active['reference']['sha256']}.md"
    )
    assert plan_artifact.read_text(encoding="utf-8") == plan_text
    assert len(serialized.encode("utf-8")) <= MANAGER_INLINE_CONTEXT_TARGET_BYTES
    summary = context["controller_state"]["repartition_evidence"]["envelope_summary"]
    assert summary["schema_version"] == 1
    assert summary["canonical_envelope_sha256"]
    assert "plan_text" not in json.dumps(summary)


@pytest.mark.parametrize(
    ("stdout", "extraction", "expected"),
    [
        ("plain final result", "plain_text", "plain final result"),
        (
            '{"type":"result","result":"recognized final assistant"}',
            "structured_stream",
            "recognized final assistant",
        ),
    ],
)
def test_v3_full_history_preserves_recognized_and_plain_results(
    tmp_path: Path, stdout: str, extraction: str, expected: str
) -> None:
    run_dir, plan = _run(tmp_path)
    _write_turn(
        run_dir,
        1,
        step="implement",
        role="implementer",
        stdout=stdout,
        semantic_output_source=(
            STRUCTURED_TRANSPORT_OUTPUT_SOURCE
            if extraction == "structured_stream"
            else None
        ),
        output_contract=("agent" if extraction == "structured_stream" else None),
    )
    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary["context_schema_version"] = 4

    context = build_manager_context(
        run_dir,
        level="full",
        boundary=boundary,
        active_plan_content=plan.read_text(encoding="utf-8"),
        capture_evidence=True,
    )
    history_ref = context["evidence"]["manager_history"]["reference"]
    history_payload = json.loads(
        (tmp_path / "repo" / history_ref["path"]).read_text(encoding="utf-8")
    )
    record = history_payload["sections"]["run_extract"][0]
    latest = history_payload["sections"]["latest_turn"]
    assert record["semantic_extraction"] == extraction
    assert record["semantic_fallback"] is False
    assert record["semantic_summary"] == expected
    assert latest["semantic_result"]["extraction"] == extraction
    assert latest["semantic_result"]["fallback"] is False
    assert latest["semantic_result"]["result"] == expected


def test_v3_full_history_hides_unrecognized_structured_stream(
    tmp_path: Path,
) -> None:
    run_dir, plan = _run(tmp_path)
    private_session_sentinel = "PRIVATE_SESSION_SENTINEL_7b3a"
    stdout = json.dumps({
        "type": "session.started",
        "session_id": private_session_sentinel,
        "metadata": {"provider_private": True},
    })
    _write_turn(
        run_dir,
        1,
        step="implement",
        role="implementer",
        stdout=stdout,
        output_contract="agent",
        semantic_output_source=STRUCTURED_TRANSPORT_OUTPUT_SOURCE,
    )
    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary["context_schema_version"] = 4

    context = build_manager_context(
        run_dir,
        level="full",
        boundary=boundary,
        active_plan_content=plan.read_text(encoding="utf-8"),
        capture_evidence=True,
    )
    history_ref = context["evidence"]["manager_history"]["reference"]
    history_payload = json.loads(
        (tmp_path / "repo" / history_ref["path"]).read_text(encoding="utf-8")
    )
    run_extract = history_payload["sections"]["run_extract"]
    latest = history_payload["sections"]["latest_turn"]
    assert private_session_sentinel not in json.dumps(run_extract)
    assert private_session_sentinel not in json.dumps(latest)
    assert private_session_sentinel not in json.dumps(context)
    assert run_extract[0]["semantic_extraction"] == (
        "unrecognized_structured_stream"
    )
    assert run_extract[0]["semantic_fallback"] is True
    assert "referenced by artifact" in run_extract[0]["semantic_summary"]
    assert latest["semantic_result"]["extraction"] == (
        "unrecognized_structured_stream"
    )
    assert latest["semantic_result"]["fallback"] is True
    assert "referenced by artifact" in latest["semantic_result"]["result"]
    assert latest["raw_artifacts"][0]["path"] == "turns/turn-001/stdout.txt"
    assert latest["raw_artifacts"][0]["byte_size"] == len(stdout.encode("utf-8"))


@pytest.mark.parametrize("include_null_finalized_turn", [False, True])
def test_v3_history_handles_legacy_manager_turn_associations(
    tmp_path: Path, include_null_finalized_turn: bool
) -> None:
    run_dir, plan = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="done")
    manager_result = {
        "decision_number": 1,
        "level": "full",
        "trigger": "post_turn",
        "status": "accepted",
        "action": "continue",
        "reason": "The current implementation can continue.",
    }
    if include_null_finalized_turn:
        manager_result["finalized_turn_number"] = None
    _write_json(
        run_dir / "manager" / "decision-001" / "result.json", manager_result
    )
    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary["context_schema_version"] = 4
    plan_text = plan.read_text(encoding="utf-8")

    live = build_manager_context(
        run_dir,
        level="full",
        decision_number=2,
        boundary=boundary,
        active_plan_content=plan_text,
        capture_evidence=True,
    )
    history_ref = live["evidence"]["manager_history"]["reference"]
    history_path = tmp_path / "repo" / history_ref["path"]
    history_payload = json.loads(history_path.read_text(encoding="utf-8"))
    manager_record = history_payload["sections"]["manager_decisions"][0]
    assert manager_record["decision_number"] == 1
    assert manager_record["turn_number"] is None
    assert live["history_summary"]["total_turns"] == 1
    assert live["history_summary"]["total_decisions"] == 1
    assert live["history_summary"]["coverage"]["decisions"] == {
        "count": 1,
        "range": {"start": 1, "end": 1},
    }
    assert [
        (record["kind"], record.get("turn_number"))
        for record in history_payload["sections"]["run_extract"]
    ] == [("workflow_turn", 1), ("manager_decision", None)]

    decision_context = run_dir / "manager" / "decision-002" / "context.json"
    _write_json(decision_context, live)
    before = sorted(
        path.relative_to(run_dir).as_posix()
        for path in (run_dir / "evidence").rglob("*")
        if path.is_file()
    )
    rebuilt = build_manager_context(
        run_dir,
        level="full",
        decision_number=2,
        boundary=boundary,
        active_plan_content=plan_text,
        capture_evidence=False,
    )
    after = sorted(
        path.relative_to(run_dir).as_posix()
        for path in (run_dir / "evidence").rglob("*")
        if path.is_file()
    )
    assert rebuilt == live
    assert after == before


def test_v3_evidence_capture_is_idempotent_and_selector3_keeps_v2(tmp_path: Path) -> None:
    run_dir, plan = _run(tmp_path)
    plan_text = _big_plan(plan.read_text(encoding="utf-8"))
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="done")
    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary["context_schema_version"] = 4
    first = build_manager_context(
        run_dir, level="full", boundary=boundary,
        active_plan_content=plan_text, capture_evidence=True,
    )
    evidence_dir = tmp_path / "repo" / ".aflow" / "runs" / "run-1" / "evidence"
    plans_before = sorted((evidence_dir / "plans").iterdir())
    history_before = sorted((evidence_dir / "manager-history").iterdir())
    second = build_manager_context(
        run_dir, level="full", boundary=boundary,
        active_plan_content=plan_text, capture_evidence=True,
    )
    plans_after = sorted((evidence_dir / "plans").iterdir())
    history_after = sorted((evidence_dir / "manager-history").iterdir())
    assert second["evidence"] == first["evidence"]
    assert plans_after == plans_before
    assert history_after == history_before
    # Selector 3 continues to rebuild the exact v2 shape.
    boundary["context_schema_version"] = 3
    v2 = build_manager_context(
        run_dir, level="full", boundary=boundary,
        active_plan_content=plan_text, capture_evidence=True,
    )
    assert v2["schema_version"] == MANAGER_CONTEXT_SCHEMA_VERSION_V2
    assert v2["active_plan_content"] == plan_text
    assert v2["envelope"]["available"] is True


def test_v3_without_capture_discloses_unavailable_and_writes_nothing(tmp_path: Path) -> None:
    run_dir, plan = _run(tmp_path)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="done")
    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary["context_schema_version"] = 4
    context = build_manager_context(
        run_dir, level="full", boundary=boundary,
        active_plan_content=plan.read_text(encoding="utf-8"),
        capture_evidence=False,
    )
    evidence_store = tmp_path / "repo" / ".aflow" / "runs" / "run-1" / "evidence"
    assert not evidence_store.exists()
    assert context["evidence"]["active_plan"]["available"] is False
    assert context["plan_content_disclosure"]["active_plan"] == "unavailable"
    assert context["evidence"]["manager_history"]["available"] is False
    assert not (evidence_store / "manager-history").exists()


def test_v3_manager_history_reference_is_read_only_and_tamper_evident(
    tmp_path: Path,
) -> None:
    run_dir, plan = _run(tmp_path)
    plan_text = plan.read_text(encoding="utf-8")
    for number in range(1, 4):
        _write_turn(
            run_dir,
            number,
            step="implement",
            role="implementer",
            stdout=f"semantic result {number} — 日本語🚦",
        )
        if number < 3:
            _write_json(
                run_dir / "manager" / f"decision-{number:03d}" / "result.json",
                {
                    "decision_number": number,
                    "finalized_turn_number": number,
                    "level": "full",
                    "trigger": "post_turn",
                    "status": "accepted",
                    "action": "continue",
                    "reason": f"decision {number}",
                },
            )
    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary["context_schema_version"] = 4

    live = build_manager_context(
        run_dir,
        level="full",
        decision_number=3,
        boundary=boundary,
        active_plan_content=plan_text,
        capture_evidence=True,
    )
    history_ref = live["evidence"]["manager_history"]["reference"]
    history_path = tmp_path / "repo" / history_ref["path"]
    history_text = history_path.read_text(encoding="utf-8")
    history_payload = json.loads(history_text)
    assert history_payload["schema_version"] == 1
    assert history_payload["decision_number"] == 3
    assert len(history_payload["sections"]["run_extract"]) == 5
    assert len(history_payload["sections"]["manager_decisions"]) == 2
    assert history_payload["sections"]["latest_turn"]["semantic_result"][
        "result"
    ].endswith("日本語🚦")
    assert '"stdout":' not in history_text
    assert '"stderr":' not in history_text

    decision_context = run_dir / "manager" / "decision-003" / "context.json"
    _write_json(decision_context, live)
    evidence_files_before = sorted(
        path.name for path in (run_dir / "evidence" / "manager-history").iterdir()
    )
    rebuilt = build_manager_context(
        run_dir,
        level="full",
        decision_number=3,
        boundary=boundary,
        active_plan_content=plan_text,
        capture_evidence=False,
    )
    assert rebuilt == live
    assert sorted(
        path.name for path in (run_dir / "evidence" / "manager-history").iterdir()
    ) == evidence_files_before

    history_path.write_bytes(b"tampered history\n")
    unavailable = build_manager_context(
        run_dir,
        level="full",
        decision_number=3,
        boundary=boundary,
        active_plan_content=plan_text,
        capture_evidence=False,
    )
    assert unavailable["evidence"]["manager_history"]["available"] is False
    assert unavailable["history_summary"]["reference_available"] is False
    assert history_path.read_bytes() == b"tampered history\n"


def test_v3_manager_history_capture_failure_stops_live_context(
    tmp_path: Path, monkeypatch
) -> None:
    from aflow import runlog

    run_dir, plan = _run(tmp_path)
    plan_text = plan.read_text(encoding="utf-8")
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="done")
    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary["context_schema_version"] = 4
    original_store = runlog.store_evidence_artifact

    def fail_history_store(paths, *, kind, data):
        if kind == "manager_history":
            raise OSError("synthetic manager history storage failure")
        return original_store(paths, kind=kind, data=data)

    monkeypatch.setattr(runlog, "store_evidence_artifact", fail_history_store)
    with pytest.raises(OSError, match="synthetic manager history storage failure"):
        build_manager_context(
            run_dir,
            level="full",
            decision_number=1,
            boundary=boundary,
            active_plan_content=plan_text,
            capture_evidence=True,
        )


@pytest.mark.parametrize("level", ["lite", "full"])
def test_v3_reviewer_finished_turn_references_stdout_artifact(tmp_path: Path, monkeypatch, level) -> None:
    run_dir, plan = _run(tmp_path)
    plan_text = plan.read_text(encoding="utf-8")
    reviewer_stdout = "REVIEWER-EXACT-BODY-SENTINEL-9f9f\n" * 30
    _write_turn(run_dir, 1, step="review", role="reviewer", stdout=reviewer_stdout)
    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary["context_schema_version"] = 4

    worktree = tmp_path / "separate-worktree"
    worktree.mkdir()
    monkeypatch.chdir(worktree)
    context = build_manager_context(
        run_dir, level=level, boundary=boundary,
        active_plan_content=plan_text, capture_evidence=True,
    )

    serialized = json.dumps(context, sort_keys=True)
    assert "REVIEWER-EXACT-BODY-SENTINEL" not in serialized
    reviewer = context["evidence"].get("reviewer_stdout")
    assert reviewer is not None
    assert reviewer["artifact_path"] == "turns/turn-001/stdout.txt"
    assert reviewer["available"] is True
    assert reviewer["byte_size"] == len(reviewer_stdout.encode("utf-8"))
    roots = context["controller_state"]["artifact_roots"]
    assert Path(roots["run"]).is_absolute()
    assert Path(roots["repository"]).is_absolute()
    assert not Path(reviewer["artifact_path"]).exists()
    assert (Path(roots["run"]) / reviewer["artifact_path"]).read_text() == reviewer_stdout
    checkpoint_ref = context["evidence"]["checkpoint"]["reference"]
    checkpoint_path = Path(roots["repository"]) / checkpoint_ref["path"]
    assert not Path(checkpoint_ref["path"]).exists()
    import hashlib
    assert hashlib.sha256(checkpoint_path.read_bytes()).hexdigest() == checkpoint_ref["sha256"]
    result = context["finished_turn"]["semantic_result"]["result"]
    assert "referenced by artifact" in result


def test_v3_history_artifact_keeps_complete_run_extract(tmp_path: Path) -> None:
    run_dir, plan = _run(tmp_path)
    for number in range(1, 16):
        _write_turn(
            run_dir, number,
            step="implement", role="implementer",
            stdout=f"result row {number}",
        )
    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary["context_schema_version"] = 4

    context = build_manager_context(
        run_dir, level="full", boundary=boundary,
        active_plan_content=plan.read_text(encoding="utf-8"),
        capture_evidence=True,
    )

    assert context["run_extract"] == []
    assert context["history_summary"]["total_turns"] == 15
    history_ref = context["evidence"]["manager_history"]["reference"]
    history_payload = json.loads(
        (tmp_path / "repo" / history_ref["path"]).read_text(encoding="utf-8")
    )
    run_extract = history_payload["sections"]["run_extract"]
    assert len(run_extract) == 15
    assert run_extract[-1]["turn_number"] == 15


@pytest.mark.parametrize("history_size", [10, 100, 1_000])
def test_v3_long_history_is_bounded_with_resolvable_omission_references(
    tmp_path: Path, monkeypatch, history_size: int
) -> None:
    run_dir, plan = _run(tmp_path)
    for number in range(1, history_size + 1):
        _write_turn(
            run_dir,
            number,
            step="implement",
            role="implementer",
            stdout=f"workflow result {number}",
        )
        _write_json(
            run_dir / "manager" / f"decision-{number:03d}" / "result.json",
            {
                "decision_number": number,
                "finalized_turn_number": number,
                "level": "lite",
                "trigger": "post_turn",
                "status": "accepted",
                "action": "continue",
                "reason": f"manager decision {number}",
            },
        )

    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary["context_schema_version"] = 4
    repartition = {
        "schema_version": 1,
        "decision_number": history_size,
        "scope_id": "scope-1",
        "generation_id": "generation-1",
        "envelope_sha256": "a" * 64,
        "envelope_artifact_sha256": "b" * 64,
        "source_plan_sha256": "c" * 64,
        "proposal_sha256": "d" * 64,
        "candidate_plan_sha256": "e" * 64,
        "partition_ids": ["partition-1"],
        "child_summaries": ["Partition 1"],
        "current_disposition": "implement_current_partition",
        "resolved_target_step": "implement",
        "resolved_target_role": "worker",
        "current_partition_id": "partition-1",
        "scope_pressure_reason": None,
        "envelope_artifact_path": "scopes/scope-1/envelope.json",
        "proposal_artifact_path": "manager/decision-020/repartition/proposal.json",
        "candidate_artifact_path": "manager/decision-020/repartition/candidate.md",
        "mechanical_validation_artifact_path": "manager/decision-020/repartition/mechanical.json",
        "semantic_verdict_artifact_path": "manager/decision-020/repartition/verdict.json",
    }
    boundary["repartition_history"] = [repartition]

    separate_worktree = tmp_path / "separate-worktree"
    separate_worktree.mkdir()
    monkeypatch.chdir(separate_worktree)
    context = build_manager_context(
        run_dir,
        level="full",
        decision_number=history_size + 1,
        boundary=boundary,
        active_plan_content=plan.read_text(encoding="utf-8"),
        capture_evidence=True,
    )

    assert context["schema_version"] == MANAGER_CONTEXT_SCHEMA_VERSION_V3
    assert context["manager_decisions"] == []
    assert context["run_extract"] == []
    assert context["active_scope_rejection_ledger"] == []
    assert context["implementation_attempts"] == {}
    assert context["controller_state"]["checkpoint_repartitions"] == []
    assert context["history_summary"]["total_turns"] == history_size
    assert context["history_summary"]["total_decisions"] == history_size
    history_ref = context["evidence"]["manager_history"]["reference"]
    history_payload = json.loads(
        (tmp_path / "repo" / history_ref["path"]).read_text(encoding="utf-8")
    )
    assert len(history_payload["sections"]["run_extract"]) == history_size * 2
    assert len(history_payload["sections"]["manager_decisions"]) == history_size
    assert history_payload["sections"]["checkpoint_repartitions"] == [repartition]

    disclosure = context["history_disclosure"]
    assert disclosure["reduced_categories"] == []
    assert disclosure["reduction_order"] == []
    assert disclosure["omitted"] == []

    _, user_prompt = build_manager_prompts(context)
    assert len(user_prompt.encode("utf-8")) <= MANAGER_INLINE_CONTEXT_TARGET_BYTES


def test_v3_bounds_semantic_summaries_with_shared_marker(tmp_path: Path) -> None:
    run_dir, plan = _run(tmp_path)
    huge_result = "LONG-SEMANTIC-" + ("日本語🚦" * 1_500)
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout=huge_result)
    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary["context_schema_version"] = 4

    context = build_manager_context(
        run_dir, level="full", boundary=boundary,
        active_plan_content=plan.read_text(encoding="utf-8"),
        capture_evidence=True,
    )

    result = context["finished_turn"]["semantic_result"]["result"]
    assert TRUNCATION_MARKER in result
    assert len(result.encode("utf-8")) <= 512
    assert context["run_extract"] == []
    history_ref = context["evidence"]["manager_history"]["reference"]
    history_payload = json.loads(
        (tmp_path / "repo" / history_ref["path"]).read_text(encoding="utf-8")
    )
    assert history_payload["sections"]["latest_turn"]["semantic_result"][
        "result"
    ] == huge_result


def test_v3_later_boundary_fixture_preserves_unicode_review_and_repartition_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    """Exercise the sanitized shape of the later-boundary incident."""
    run_dir, plan = _run(tmp_path)
    plan_text = plan.read_text(encoding="utf-8")
    history_text = "historical revisión 東京 🚦 " + ("x" * 6_000)
    for number in range(1, 21):
        _write_turn(
            run_dir,
            number,
            step="review" if number == 20 else "implement",
            role="reviewer" if number == 20 else "implementer",
            stdout=(
                "レビュー rejected: " + history_text
                if number == 20
                else f"workflow result {number}: {history_text}"
            ),
        )
        _write_json(
            run_dir / "manager" / f"decision-{number:03d}" / "result.json",
            {
                "decision_number": number,
                "finalized_turn_number": number,
                "level": "lite",
                "trigger": "post_turn",
                "status": "accepted",
                "action": "continue",
                "reason": history_text,
            },
        )

    scope_id = "plans/in-progress/plan.md::checkpoint-1::incident"
    long_candidate_path = (
        "manager/decision-020/repartition/"
        + ("nested/" * 32)
        + "candidate.md"
    )
    rejection = {
        "scope_id": scope_id,
        "rejection_number": 3,
        "source_run_id": run_dir.name,
        "review_turn_number": 20,
        "review_step_name": "review",
        "reviewer_selector": "codex.review",
        "checkpoint_index": 1,
        "checkpoint_name": "Checkpoint 1: Context",
        "reviewed_implementation_turn_number": 19,
        "reviewed_worker_team": "base",
        "reviewed_worker_selector": "codex.worker",
        "review_summary": "レビュー requires one bounded repair.",
        "repair_plan_summary": "Keep the evidence boundary intact.",
        "review_stdout_artifact_path": "turns/turn-020/stdout.txt",
        "repair_plan_path": "plans/in-progress/repair-é.md",
    }
    repartition = {
        "schema_version": 1,
        "decision_number": 20,
        "scope_id": scope_id,
        "generation_id": "generation-20",
        "envelope_sha256": "a" * 64,
        "envelope_artifact_sha256": "b" * 64,
        "source_plan_sha256": "c" * 64,
        "proposal_sha256": "d" * 64,
        "candidate_plan_sha256": "e" * 64,
        "partition_ids": ["partition-1", "partition-2"],
        "child_summaries": ["Part 1: 修复", "Part 2: Verify"],
        "current_disposition": "review_current_partition",
        "resolved_target_step": "implement",
        "resolved_target_role": "implementer",
        "current_partition_id": "partition-1",
        "scope_pressure_reason": "the boundary needs two reviewable slices",
        "envelope_artifact_path": "scopes/incident/envelope.json",
        "proposal_artifact_path": long_candidate_path.replace(
            "candidate.md", "proposal.json"
        ),
        "candidate_artifact_path": long_candidate_path,
        "mechanical_validation_artifact_path": long_candidate_path.replace(
            "candidate.md", "mechanical.json"
        ),
        "semantic_verdict_artifact_path": long_candidate_path.replace(
            "candidate.md", "verdict.json"
        ),
    }
    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary.update({
        "context_schema_version": 4,
        "active_implementation_scope": {
            "scope_id": scope_id,
            "checkpoint_index": 1,
            "checkpoint_name": "Checkpoint 1: Context",
            "opened_turn_number": 1,
            "awaiting_review": True,
            "attempt_count": 3,
            "carried_reviewer_rejection_count": 2,
        },
        "review_rejection_history": [rejection],
        "repartition_history": [repartition],
        "evidence": "Lite escalated because rejection evidence needs Full review.",
    })
    separate_worktree = tmp_path / ("execution-" + ("é" * 16))
    separate_worktree.mkdir()
    monkeypatch.chdir(separate_worktree)

    context = build_manager_context(
        run_dir,
        level="full",
        trigger="lite_escalation",
        decision_number=21,
        run_metadata={
            "plan_path": str(plan),
            "active_plan_path": str(plan),
            "original_plan_path": str(plan),
            "team": "base",
            "turns_completed": 20,
            "max_turns": 25,
        },
        boundary=boundary,
        active_plan_content=plan_text,
        capture_evidence=True,
    )
    _, first_prompt = build_manager_prompts(context)
    _, second_prompt = build_manager_prompts(context)
    projected = json.loads(
        first_prompt.split("MANAGER_CONTEXT_JSON:\n", 1)[1]
    )

    assert first_prompt == second_prompt
    assert len(first_prompt.encode("utf-8")) <= MANAGER_INLINE_CONTEXT_TARGET_BYTES
    assert projected["trigger"] == "lite_escalation"
    assert projected["finished_turn"]["turn_number"] == 20
    assert projected["controller_state"]["lite_evidence"] == (
        "Lite escalated because rejection evidence needs Full review."
    )
    assert projected["controller_state"]["active_implementation_scope"][
        "awaiting_review"
    ] is True
    assert projected["controller_state"]["checkpoint_repartitions"] == []
    assert projected["active_scope_rejection_ledger"] == []
    assert projected["manager_decisions"] == []
    assert projected["run_extract"] == []
    assert projected["history_summary"]["total_decisions"] == 20
    assert projected["history_summary"]["total_active_scope_rejections"] == 1
    assert "review_summary" not in projected["controller_state"]["latest_full_rejection"]
    disclosure = projected["history_disclosure"]
    assert disclosure["omitted"] == []
    history_ref = projected["evidence"]["manager_history"]["reference"]
    history_payload = json.loads(
        (tmp_path / "repo" / history_ref["path"]).read_text(encoding="utf-8")
    )
    assert history_payload["sections"]["active_scope_rejection_ledger"] == [rejection]
    assert history_payload["sections"]["checkpoint_repartitions"] == [repartition]


def test_v3_decision_twenty_reconstruction_is_read_only_and_byte_stable(
    tmp_path: Path,
) -> None:
    run_dir, plan = _run(tmp_path)
    plan_text = plan.read_text(encoding="utf-8")
    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary["context_schema_version"] = 4
    run_metadata = {
        "plan_path": str(plan),
        "active_plan_path": str(plan),
        "original_plan_path": str(plan),
        "team": "base",
        "turns_completed": 20,
        "max_turns": 25,
    }
    for number in range(1, 21):
        _write_turn(
            run_dir,
            number,
            step="implement",
            role="implementer",
            stdout=f"decision boundary {number} — 測定",
        )
        if number < 20:
            _write_json(
                run_dir / "manager" / f"decision-{number:03d}" / "result.json",
                {
                    "decision_number": number,
                    "finalized_turn_number": number,
                    "level": "full",
                    "status": "accepted",
                    "action": "continue",
                    "reason": f"decision {number}",
                },
            )

    stored = build_manager_context(
        run_dir,
        level="full",
        trigger="post_turn",
        decision_number=20,
        run_metadata=run_metadata,
        boundary=boundary,
        turns=[
            json.loads(
                (run_dir / "turns" / f"turn-{number:03d}" / "result.json")
                .read_text(encoding="utf-8")
            )
            | {"_turn_dir": run_dir / "turns" / f"turn-{number:03d}"}
            for number in range(1, 21)
        ],
        active_plan_content=plan_text,
        capture_evidence=False,
    )
    decision_dir = run_dir / "manager" / "decision-020"
    _write_json(decision_dir / "context.json", stored)
    _write_json(decision_dir / "result.json", {
        "decision_number": 20,
        "finalized_turn_number": 20,
        "level": "full",
        "status": "accepted",
        "action": "continue",
    })
    _write_json(decision_dir / "boundary.json", {
        "decision_number": 20,
        "trigger": "post_turn",
        "run_metadata": run_metadata,
        "boundary": boundary,
        "active_plan_content": plan_text,
    })
    tracked_paths = [
        run_dir / "run.json",
        decision_dir / "context.json",
        decision_dir / "result.json",
        decision_dir / "boundary.json",
        *[
            run_dir / "turns" / f"turn-{number:03d}" / filename
            for number in range(1, 21)
            for filename in ("result.json", "stdout.txt", "stderr.txt")
        ],
    ]
    before = {
        path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in tracked_paths
    }
    _, before_prompt = build_manager_prompts(stored)
    before_bytes = len(before_prompt.encode("utf-8"))

    rebuilt = analyze_runs(AnalyzeRequest(
        repo_root=run_dir.parent.parent.parent,
        run_id=run_dir.name,
        manager_context="full",
        turn=20,
    ))

    _, after_prompt = build_manager_prompts(rebuilt)
    after_bytes = len(after_prompt.encode("utf-8"))
    after = {
        path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in tracked_paths
    }
    assert rebuilt == stored
    assert before_bytes == after_bytes, (
        f"decision-020 prompt bytes changed: before={before_bytes}, after={after_bytes}"
    )
    assert before_bytes <= MANAGER_INLINE_CONTEXT_MAX_BYTES
    assert after == before


def test_v3_live_capture_hash_mismatch_aborts_context_construction(
    tmp_path: Path,
) -> None:
    """Live capture must never downgrade store failures to unavailable."""
    run_dir, plan = _run(tmp_path)
    plan_text = plan.read_text(encoding="utf-8")
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="done")
    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary["context_schema_version"] = 4
    digest = hashlib.sha256(plan_text.encode("utf-8")).hexdigest()
    plan_store = run_dir / "evidence" / "plans"
    plan_store.mkdir(parents=True)
    (plan_store / f"{digest}.md").write_bytes(b"tampered bytes\n")

    with pytest.raises(ValueError, match="filename digest"):
        build_manager_context(
            run_dir, level="full", boundary=boundary,
            active_plan_content=plan_text, capture_evidence=True,
        )
    # The tampered artifact is never overwritten or reused.
    assert (plan_store / f"{digest}.md").read_bytes() == b"tampered bytes\n"

    # Historical rebuilds never write and keep reporting unavailable.
    context = build_manager_context(
        run_dir, level="full", boundary=boundary,
        active_plan_content=plan_text, capture_evidence=False,
    )
    assert context["schema_version"] == MANAGER_CONTEXT_SCHEMA_VERSION_V3
    assert context["evidence"]["active_plan"]["available"] is False


def test_v3_live_capture_symlinked_evidence_store_aborts_context_construction(
    tmp_path: Path,
) -> None:
    run_dir, plan = _run(tmp_path)
    plan_text = plan.read_text(encoding="utf-8")
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="done")
    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary["context_schema_version"] = 4
    outside = tmp_path / "outside"
    outside.mkdir()
    (run_dir / "evidence").mkdir(parents=True)
    plans_link = run_dir / "evidence" / "plans"
    plans_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        build_manager_context(
            run_dir, level="full", boundary=boundary,
            active_plan_content=plan_text, capture_evidence=True,
        )
    assert list(outside.iterdir()) == []


def test_v3_live_capture_checkpoint_store_failure_propagates_not_crash(
    tmp_path: Path,
) -> None:
    """A checkpoint capture failure must raise, never mark available with
    unbound references (previous duplicated-handler UnboundLocalError)."""
    run_dir, plan = _run(tmp_path)
    plan_text = plan.read_text(encoding="utf-8")
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="done")
    boundary = dict(_enveloped_boundary(run_dir, plan))
    boundary["context_schema_version"] = 4
    first = build_manager_context(
        run_dir, level="full", boundary=boundary,
        active_plan_content=plan_text, capture_evidence=True,
    )
    checkpoint_ref = first["evidence"]["checkpoint"]["reference"]
    checkpoint_store = (
        run_dir / "evidence" / "checkpoints" / f"{checkpoint_ref['sha256']}.md"
    )
    assert checkpoint_store.is_file()
    checkpoint_store.write_bytes(b"tampered checkpoint\n")

    with pytest.raises(ValueError, match="filename digest"):
        build_manager_context(
            run_dir, level="full", boundary=boundary,
            active_plan_content=plan_text, capture_evidence=True,
        )


def test_v3_live_capture_repair_overlay_keeps_original_checkpoint_authority(
    tmp_path: Path,
) -> None:
    """A repair overlay cannot hide the original checkpoint authority."""
    (
        run_dir,
        repo,
        original,
        overlay,
        boundary,
        original_text,
        overlay_text,
    ) = _repair_context_fixture(tmp_path)
    boundary = dict(boundary)
    boundary["original_checkpoint_authority_version"] = (
        ORIGINAL_CHECKPOINT_AUTHORITY_VERSION
    )
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="done")

    context = build_manager_context(
        run_dir, level="full", boundary=boundary,
        active_plan_content=overlay_text, capture_evidence=True,
    )
    plan_state = context["plan_state"]
    assert plan_state["original_plan_path"] == str(original)
    assert plan_state["active_plan_path"] == str(overlay)
    assert plan_state["active_repair_plan"] is True
    assert [item["name"] for item in plan_state["checkpoints"]] == [
        "Checkpoint 1: First",
        "Checkpoint 2: Second",
        "Checkpoint 3: Third",
    ]
    assert plan_state["current_checkpoint"]["index"] == 3
    assert plan_state["parse_error"] is None

    active_ref = context["evidence"]["active_plan"]["reference"]
    original_ref = context["evidence"]["original_plan"]["reference"]
    checkpoint = context["evidence"]["checkpoint"]
    assert (repo / active_ref["path"]).read_text(encoding="utf-8") == overlay_text
    assert (repo / original_ref["path"]).read_text(encoding="utf-8") == original_text
    assert checkpoint["available"] is True
    checkpoint_text = (repo / checkpoint["reference"]["path"]).read_text(
        encoding="utf-8"
    )
    assert "### [ ] Checkpoint 3: Third" in checkpoint_text
    assert "Repair overlay" not in checkpoint_text

    direct_boundary = dict(boundary)
    direct_boundary.pop("original_checkpoint_authority_version")
    direct = build_manager_context(
        run_dir,
        level="full",
        boundary=direct_boundary,
        active_plan_content=overlay_text,
        capture_evidence=False,
    )
    assert direct["plan_state"]["current_checkpoint"]["index"] == 3
    assert direct["evidence"]["checkpoint"]["available"] is True

    legacy_boundary = dict(boundary)
    legacy_boundary["context_schema_version"] = 3
    context_v2 = build_manager_context(
        run_dir,
        level="full",
        boundary=legacy_boundary,
        active_plan_content=overlay_text,
        capture_evidence=False,
    )
    assert context_v2["active_plan_content"] == overlay_text
    assert context_v2["original_plan_content"] == original_text
    assert context_v2["plan_state"]["current_checkpoint"]["index"] == 3


def test_v3_live_repair_invalid_envelope_stays_unavailable(
    tmp_path: Path,
) -> None:
    run_dir, _, _, overlay, boundary, _, overlay_text = _repair_context_fixture(tmp_path)
    _write_turn(run_dir, 1, step="review", role="reviewer", stdout="rejected")
    boundary = dict(boundary)
    boundary["envelope_artifact_sha256"] = "a" * 64

    context = build_manager_context(
        run_dir,
        level="full",
        boundary=boundary,
        active_plan_content=overlay_text,
        capture_evidence=True,
    )

    plan_state = context["plan_state"]
    assert plan_state["active_plan_path"] == str(overlay)
    assert plan_state["active_repair_plan"] is True
    assert plan_state["checkpoints"] == []
    assert plan_state["is_complete"] is None
    assert plan_state["parse_error"] == "invalid_evidence"
    assert context["evidence"]["checkpoint"]["available"] is False
    assert context["controller_state"]["repartition_evidence"]["status"] == (
        "unavailable"
    )

    legacy_boundary = dict(boundary)
    legacy_boundary["context_schema_version"] = 3
    context_v2 = build_manager_context(
        run_dir,
        level="full",
        boundary=legacy_boundary,
        active_plan_content=overlay_text,
        capture_evidence=False,
    )
    assert context_v2["original_plan_content"] is None
    assert context_v2["plan_state"]["parse_error"] == "invalid_evidence"


def test_v3_live_repair_malformed_original_is_not_replaced_by_overlay(
    tmp_path: Path,
) -> None:
    run_dir, _, original, overlay, boundary, _, overlay_text = _repair_context_fixture(
        tmp_path
    )
    malformed = "# Malformed original\n\n- [ ] ordinary task\n"
    original.write_text(malformed, encoding="utf-8")
    boundary = dict(boundary)
    for key in (
        "envelope_artifact_path",
        "envelope_artifact_sha256",
        "envelope_canonical_sha256",
    ):
        boundary.pop(key)
    boundary["original_plan_content"] = malformed
    _write_turn(run_dir, 1, step="review", role="reviewer", stdout="rejected")

    context = build_manager_context(
        run_dir,
        level="full",
        boundary=boundary,
        active_plan_content=overlay_text,
        capture_evidence=False,
    )

    plan_state = context["plan_state"]
    assert plan_state["original_plan_path"] == str(original)
    assert plan_state["active_plan_path"] == str(overlay)
    assert plan_state["active_repair_plan"] is True
    assert plan_state["checkpoints"] == []
    assert plan_state["is_complete"] is None
    assert plan_state["parse_error"] is not None
    assert "checkpoint" in plan_state["parse_error"].lower()


def test_v3_historical_repair_context_reconstructs_byte_identically(
    tmp_path: Path,
) -> None:
    (
        run_dir,
        repo,
        original,
        overlay,
        boundary,
        original_text,
        overlay_text,
    ) = _repair_context_fixture(tmp_path)
    boundary = dict(boundary)
    boundary["original_checkpoint_authority_version"] = (
        ORIGINAL_CHECKPOINT_AUTHORITY_VERSION
    )
    _write_turn(run_dir, 1, step="review", role="reviewer", stdout="rejected")
    stored = build_manager_context(
        run_dir,
        level="full",
        trigger="post_turn",
        decision_number=1,
        run_metadata={
            "plan_path": str(original),
            "active_plan_path": str(overlay),
            "original_plan_path": str(original),
            "team": "base",
            "turns_completed": 1,
            "max_turns": 5,
        },
        boundary=boundary,
        active_plan_content=overlay_text,
        capture_evidence=True,
    )
    boundary["captured_plan_state"] = stored["plan_state"]
    decision_dir = run_dir / "manager" / "decision-001"
    _write_json(decision_dir / "context.json", stored)
    _write_json(decision_dir / "result.json", {
        "decision_number": 1,
        "finalized_turn_number": 1,
        "level": "full",
        "status": "accepted",
        "action": "continue",
    })
    _write_json(decision_dir / "boundary.json", {
        "decision_number": 1,
        "trigger": "post_turn",
        "run_metadata": {
            "plan_path": str(original),
            "active_plan_path": str(overlay),
            "original_plan_path": str(original),
            "team": "base",
            "turns_completed": 1,
            "max_turns": 5,
        },
        "boundary": boundary,
        "active_plan_content": overlay_text,
    })
    tracked = [decision_dir / "context.json", decision_dir / "boundary.json"]
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in tracked
    }
    original.write_text("# Changed original\n\n- [ ] no authority\n", encoding="utf-8")
    overlay.write_text("# Changed overlay\n", encoding="utf-8")

    rebuilt = analyze_runs(AnalyzeRequest(
        repo_root=repo,
        run_id=run_dir.name,
        manager_context="full",
        turn=1,
    ))

    assert rebuilt == stored
    assert rebuilt["plan_state"]["current_checkpoint"]["index"] == 3
    assert (
        repo / rebuilt["evidence"]["active_plan"]["reference"]["path"]
    ).read_text(encoding="utf-8") == overlay_text
    assert (
        repo / rebuilt["evidence"]["original_plan"]["reference"]["path"]
    ).read_text(encoding="utf-8") == original_text
    after = {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in tracked
    }
    assert after == before


def test_v3_historical_pre_handoff_repair_context_preserves_captured_payload(
    tmp_path: Path,
) -> None:
    """Legacy schema-v3 captures retain unavailable evidence on rebuild."""
    (
        run_dir,
        repo,
        original,
        overlay,
        boundary,
        _,
        overlay_text,
    ) = _repair_context_fixture(tmp_path)
    _write_turn(run_dir, 1, step="review", role="reviewer", stdout="rejected")

    legacy_boundary = dict(boundary)
    legacy_boundary.pop("original_checkpoint_authority_version", None)
    metadata = {
        "plan_path": str(original),
        "active_plan_path": str(overlay),
        "original_plan_path": str(original),
        "team": "base",
        "turns_completed": 1,
        "max_turns": 5,
    }
    stored = build_manager_context(
        run_dir,
        level="full",
        trigger="post_turn",
        decision_number=1,
        run_metadata=metadata,
        boundary=legacy_boundary,
        active_plan_content=overlay_text,
        capture_evidence=True,
    )

    # These are the durable fields emitted by the pre-handoff builder. Keep
    # them explicit so this regression does not derive its expected legacy
    # payload by rebuilding it with the current implementation.
    legacy_plan_state = {
        "original_plan_path": str(original),
        "active_plan_path": str(overlay),
        "active_repair_plan": True,
        "checkpoints": [],
        "current_checkpoint": None,
        "is_complete": None,
        "parse_error": f"{overlay}: no checkpoint sections were found",
    }
    stored["plan_state"] = legacy_plan_state
    stored["manager_note_scope"]["active_plan_identity"] = str(overlay)
    stored["evidence"]["checkpoint"] = {
        "available": False,
        "reason": "the current checkpoint bytes are unavailable at this boundary",
    }
    stored["plan_content_disclosure"]["checkpoint"] = "unavailable"
    assert stored["history_summary"]["total_turns"] == 1

    legacy_boundary["captured_plan_state"] = legacy_plan_state
    decision_dir = run_dir / "manager" / "decision-001"
    _write_json(decision_dir / "context.json", stored)
    _write_json(decision_dir / "result.json", {
        "decision_number": 1,
        "finalized_turn_number": 1,
        "level": "full",
        "status": "accepted",
        "action": "continue",
    })
    _write_json(decision_dir / "boundary.json", {
        "decision_number": 1,
        "trigger": "post_turn",
        "run_metadata": metadata,
        "boundary": legacy_boundary,
        "active_plan_content": overlay_text,
    })
    tracked = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in run_dir.rglob("*")
        if path.is_file()
    }
    original.write_text("# Changed original\n\n- [ ] no authority\n", encoding="utf-8")
    overlay.write_text("# Changed overlay\n", encoding="utf-8")

    rebuilt = analyze_runs(AnalyzeRequest(
        repo_root=repo,
        run_id=run_dir.name,
        manager_context="full",
        turn=1,
    ))

    assert rebuilt == stored
    assert rebuilt["evidence"]["checkpoint"] == {
        "available": False,
        "reason": "the current checkpoint bytes are unavailable at this boundary",
    }
    assert rebuilt["plan_content_disclosure"]["checkpoint"] == "unavailable"
    after = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in run_dir.rglob("*")
        if path.is_file()
    }
    assert after == tracked
