from __future__ import annotations

import json
import hashlib
from pathlib import Path

from aflow.analyzer import extract_aflow_stop
from aflow.api import AnalyzeRequest, analyze_runs
from aflow.manager_context import (
    DIAGNOSTIC_LIMIT,
    build_manager_context,
    build_manager_note_scope,
    extract_semantic_result,
    summarize_repair_plan,
    summarize_review_rejection,
    scoped_reviewer_rejection_count,
)
from aflow.stop_marker import detect_stop_marker
from aflow.repartition import create_envelope, write_envelope_atomic


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _snapshot(*, unchecked_steps: int = 1) -> dict[str, object]:
    return {
        "current_checkpoint_index": 1,
        "current_checkpoint_name": "Checkpoint 1: Context",
        "current_checkpoint_unchecked_step_count": unchecked_steps,
        "is_complete": False,
        "total_checkpoint_count": 1,
        "unchecked_checkpoint_count": 1,
    }


def _write_turn(run_dir: Path, number: int, *, step: str, role: str, stdout: str, before: dict[str, object] | None = None, after: dict[str, object] | None = None) -> None:
    turn_dir = run_dir / "turns" / f"turn-{number:03d}"
    turn_dir.mkdir(parents=True)
    _write_json(turn_dir / "result.json", {
        "turn_number": number,
        "step_name": step,
        "step_role": role,
        "status": "completed",
        "returncode": 0,
        "selector": "codex.nano",
        "snapshot_before": before or _snapshot(),
        "snapshot_after": after or _snapshot(),
        "chosen_transition": "next",
    })
    (turn_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    (turn_dir / "stderr.txt").write_text("diagnostic only", encoding="utf-8")


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
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout=stream)
    turn_dir = run_dir / "turns" / "turn-001"
    (turn_dir / "stderr.txt").write_text("x" * (DIAGNOSTIC_LIMIT + 100), encoding="utf-8")

    context = build_manager_context(run_dir)

    assert extract_semantic_result(stream).result == "complete final answer"
    assert context["finished_turn"]["semantic_result"]["extraction"] == "structured_stream"
    assert len(context["finished_turn"]["diagnostics"]["stderr_excerpt"]) < DIAGNOSTIC_LIMIT + 100
    assert context["finished_turn"]["raw_artifacts"][1]["byte_size"] == DIAGNOSTIC_LIMIT + 100
    assert extract_semantic_result('{"type": "unknown"}').fallback is True


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
    _write_turn(run_dir, 1, step="implement", role="implementer", stdout="output")

    context_v1 = build_manager_context(run_dir)
    context_v2_boundary = build_manager_context(
        run_dir,
        boundary={"context_schema_version": 2},
    )

    assert context_v1["schema_version"] == 1
    assert context_v2_boundary["schema_version"] == 1
    # Same output (ignoring run_extract ordering which is already sort_keys=True)
    assert context_v1 == context_v2_boundary


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
