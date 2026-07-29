from __future__ import annotations

import json
from pathlib import Path

from aflow.analyzer import extract_aflow_stop
from aflow.api import AnalyzeRequest, analyze_runs
from aflow.manager_context import (
    DIAGNOSTIC_LIMIT,
    build_manager_context,
    extract_semantic_result,
    summarize_repair_plan,
    summarize_review_rejection,
    scoped_reviewer_rejection_count,
)
from aflow.stop_marker import detect_stop_marker


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
