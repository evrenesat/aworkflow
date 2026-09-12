from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace

from aflow.control_plane import RunProgressCount, RunProgressSummary, build_context_bundle
from aflow.control_plane.run_progress import (
    _ProgressBudget,
    _canonical_manager_records,
    project_run_progress,
    project_run_progress_detail,
    project_run_progress_summary,
)
from aflow.hotplug import HotplugTransactionV1, hotplug_transaction_id
from aflow.harnesses.base import HarnessInvocation
from aflow.plan import PlanSnapshot
from aflow.runlog import _turn_result_payload
from aflow.run_state import (
    ActiveImplementationScope,
    CheckpointRepartitionRecord,
    FinalizedTurnBoundary,
    ImplementationAttempt,
    PendingBoundaryDecision,
    PendingTeamOverride,
    ReviewRejectionRecord,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _hotplug_transaction(
    *, run_id: str, number: int, stage: str, source_model: str = "Recorded source model",
    target_model: str = "Recorded target model",
) -> HotplugTransactionV1:
    digest = f"{number:064x}"
    return HotplugTransactionV1(
        transaction_id=hotplug_transaction_id(run_id, digest, number),
        run_id=run_id,
        accepted_override_digest=digest,
        transaction_number=number,
        source_role="worker",
        target_role="worker",
        source_selector="codex.source-profile",
        target_selector="codex.target-profile",
        source_harness="codex",
        target_harness="codex",
        source_profile="source-profile",
        target_profile="target-profile",
        source_model_display=source_model,
        target_model_display=target_model,
        source_turn_number=7,
        stage=stage,  # type: ignore[arg-type]
    )


def _plan(*, total: int = 14, current: int | None = 4) -> str:
    sections: list[str] = []
    for index in range(1, total + 1):
        checked = current is None or index < current
        mark = "x" if checked else " "
        sections.append(
            f"### [{mark}] Checkpoint {index}: Stage {index}\n"
            f"- [{mark}] complete stage {index}\n"
        )
    return "# Plan\n\n" + "\n".join(sections)


def _scope(index: int = 4, name: str = "Checkpoint 4: Stage 4") -> dict[str, object]:
    return {
        "scope_id": f"original::checkpoint-{index}",
        "original_plan_path": "plans/in-progress/original.md",
        "checkpoint_index": index,
        "checkpoint_name": name,
        "opened_turn_number": 1,
        "awaiting_review": True,
    }


def _run(tmp_path: Path, *, metadata: dict[str, object] | None = None) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    original = repo / "plans" / "in-progress" / "original.md"
    overlay = repo / "execution" / "repair.md"
    original.parent.mkdir(parents=True)
    overlay.parent.mkdir(parents=True)
    original.write_text(_plan(), encoding="utf-8")
    overlay.write_text("# Repair overlay\n\n- [ ] fix the review finding\n", encoding="utf-8")
    run_dir = repo / ".aflow" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    payload: dict[str, object] = {
        "schema_version": 1,
        "repo_root": str(repo),
        "execution_repo_root": str(repo / "execution"),
        "original_plan_path": str(original),
        "active_plan_path": str(overlay),
        "plan_path": str(original),
    }
    if metadata:
        payload.update(metadata)
    _write_json(run_dir / "run.json", payload)
    return run_dir, original, overlay


def _write_turn(
    run_dir: Path,
    number: int,
    *,
    status: str,
    finished_at: str | None,
    step: str = "review",
    stdout: str = "review result",
) -> None:
    turn_dir = run_dir / "turns" / f"turn-{number:03d}"
    turn_dir.mkdir(parents=True)
    payload: dict[str, object] = {
        "turn_number": number,
        "step_name": step,
        "status": status,
        "returncode": 0,
        "started_at": "2026-09-10T00:00:00+00:00",
    }
    if finished_at is not None:
        payload["finished_at"] = finished_at
    _write_json(turn_dir / "result.json", payload)
    (turn_dir / "stdout.txt").write_text(stdout, encoding="utf-8")


def test_open_scope_uses_original_14_checkpoint_plan_and_keeps_overlay_separate(
    tmp_path: Path,
) -> None:
    run_dir, original, overlay = _run(
        tmp_path,
        metadata={"active_implementation_scope": _scope()},
    )
    before = {
        "run": (run_dir / "run.json").read_bytes(),
        "original": original.read_bytes(),
        "overlay": overlay.read_bytes(),
    }

    progress = build_context_bundle(run_dir).to_dict()["data"]["progress"]

    assert progress["availability"] == "partial"
    assert progress["current_checkpoint_ordinal"] == 4
    assert progress["current_checkpoint_title"] == "Checkpoint 4: Stage 4"
    assert progress["total_checkpoints"] == {"value": 14, "coverage": "complete"}
    assert len(progress["checkpoints"]) == 14
    assert progress["truncation"]["events_read"] == 0
    assert progress["reason_codes"] == ["history_partial"]
    assert progress["applied_changes"] == []
    assert progress["pending_changes"] == []
    assert (run_dir / "run.json").read_bytes() == before["run"]
    assert original.read_bytes() == before["original"]
    assert overlay.read_bytes() == before["overlay"]


def test_non_checkpoint_original_is_unavailable_instead_of_zero_total(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text("# A repair note\n\n- [ ] one item\n", encoding="utf-8")

    progress = project_run_progress(run_dir)

    assert progress["availability"] == "unavailable"
    assert progress["checkpoint"] is None
    assert progress["total"] is None
    assert progress["complete"] is None
    assert progress["reason"] == "non_checkpoint_plan"


def test_unreadable_original_is_unavailable(tmp_path: Path) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_bytes(b"\xff\xfe not utf-8")

    progress = project_run_progress(run_dir)

    assert progress["availability"] == "unavailable"
    assert progress["total"] is None
    assert progress["complete"] is None
    assert progress["reason"] == "unreadable_plan"


def test_known_scope_without_original_plan_is_partial_without_denominator(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(
        tmp_path,
        metadata={"active_implementation_scope": _scope()},
    )
    original.unlink()

    progress = project_run_progress(run_dir)

    assert progress["availability"] == "partial"
    assert progress["checkpoint"] == {"index": 4, "name": "Checkpoint 4: Stage 4"}
    assert progress["total"] is None
    assert progress["complete"] is None
    assert progress["reason"] == "missing_plan"


def test_invalid_scope_evidence_does_not_promote_direct_plan_total(
    tmp_path: Path,
) -> None:
    invalid_scope = {
        **_scope(),
        "envelope_artifact_path": "scopes/not-the-scope/envelope.json",
        "envelope_artifact_sha256": "a" * 64,
        "envelope_canonical_sha256": "b" * 64,
    }
    run_dir, _, _ = _run(
        tmp_path,
        metadata={"active_implementation_scope": invalid_scope},
    )

    progress = project_run_progress(run_dir)

    assert progress["availability"] == "partial"
    assert progress["checkpoint"] == {"index": 4, "name": "Checkpoint 4: Stage 4"}
    assert progress["total"] is None
    assert progress["reason"] == "invalid_evidence"


def test_invalid_captured_plan_state_stays_unavailable(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(
        tmp_path,
        metadata={
            "active_implementation_scope": _scope(),
            "manager_decision_number": 1,
        },
    )
    original.unlink()
    _write_json(
        run_dir / "manager" / "decision-001" / "boundary.json",
        {
            "captured_plan_state": {
                "active_repair_plan": True,
                "checkpoints": [],
                "is_complete": None,
            }
        },
    )

    progress = project_run_progress(run_dir)

    assert progress["availability"] == "partial"
    assert progress["checkpoint"] == {
        "index": 4,
        "name": "Checkpoint 4: Stage 4",
    }
    assert progress["total"] is None
    assert progress["complete"] is None
    assert progress["reason"] == "invalid_evidence"


def test_scope_without_identity_is_unavailable_instead_of_guessing(
    tmp_path: Path,
) -> None:
    scope = _scope()
    del scope["scope_id"]
    run_dir, _, _ = _run(tmp_path, metadata={"active_implementation_scope": scope})

    progress = project_run_progress(run_dir)

    assert progress["availability"] == "unavailable"
    assert progress["checkpoint"] is None
    assert progress["total"] is None
    assert progress["reason"] == "missing_scope"


def test_missing_all_progress_is_explicitly_unavailable(tmp_path: Path) -> None:
    run_dir, _, _ = _run(tmp_path)
    (run_dir / "run.json").write_text(
        json.dumps({"status": "running"}) + "\n", encoding="utf-8"
    )

    progress = project_run_progress(run_dir)

    assert progress == {
        "availability": "unavailable",
        "checkpoint": None,
        "total": None,
        "complete": None,
        "repairing": False,
        "overlay_path": None,
        "reason": "missing_plan",
        "last_finished_turn": None,
        "current_turn": None,
    }


def test_finished_review_stays_last_finished_when_new_turn_is_starting(
    tmp_path: Path,
) -> None:
    run_dir, _, _ = _run(tmp_path)
    _write_turn(
        run_dir,
        1,
        status="completed",
        finished_at="2026-09-10T00:00:05+00:00",
        stdout="review approved",
    )
    _write_turn(
        run_dir,
        2,
        status="starting",
        finished_at=None,
        step="implement",
        stdout="",
    )

    progress = project_run_progress(run_dir)

    assert progress["last_finished_turn"] == {
        "turn_number": 1,
        "step": "review",
        "status": "completed",
        "summary": "exit 0: review approved",
    }
    assert progress["current_turn"] == {
        "turn_number": 2,
        "step": "implement",
        "status": "starting",
        "summary": None,
    }


def test_status_without_finish_evidence_is_not_last_finished_turn(
    tmp_path: Path,
) -> None:
    run_dir, _, _ = _run(tmp_path)
    _write_turn(
        run_dir,
        1,
        status="completed",
        finished_at=None,
    )

    progress = project_run_progress(run_dir)

    assert progress["last_finished_turn"] is None
    assert progress["current_turn"] is None


def _canonical_plan(*, total: int = 11, checked: set[int] | None = None) -> str:
    checked = checked or set()
    sections: list[str] = []
    for index in range(1, total + 1):
        mark = "x" if index in checked else " "
        sections.append(
            f"### [{mark}] Checkpoint {index}: Stage {index}\n"
            f"- [{mark}] record stage {index}\n"
        )
    return "# Canonical plan\n\n" + "\n".join(sections)


def _write_canonical_turn(
    run_dir: Path,
    number: int,
    *,
    role: str,
    status: str,
    checkpoint_index: int,
    selector: str,
    outcome: str,
    finished_at: str | None = "2026-09-10T00:01:00+00:00",
    retry_attempt: int | None = None,
) -> None:
    turn_dir = run_dir / "turns" / f"turn-{number:03d}"
    turn_dir.mkdir(parents=True)
    payload: dict[str, object] = {
        "turn_number": number,
        "step_name": "implement" if role == "worker" else "review",
        "step_role": role,
        "status": status,
        "outcome": outcome,
        "checkpoint_index": checkpoint_index,
        "checkpoint_name": f"Checkpoint {checkpoint_index}: Stage {checkpoint_index}",
        "scope_id": f"original::checkpoint-{checkpoint_index}",
        "team": "repair-team" if role == "worker" and number == 52 else "team",
        "selector": selector,
        "harness": "local-harness",
        "model": f"model-{number}",
        "model_display": f"Model display {number}",
        "effort": "high",
        "started_at": "2026-09-10T00:00:00+00:00",
    }
    if finished_at is not None:
        payload["finished_at"] = finished_at
    if retry_attempt is not None:
        payload["retry_attempt"] = retry_attempt
        payload["was_retry"] = True
    _write_json(turn_dir / "result.json", payload)


def _write_production_turn(
    run_dir: Path,
    number: int,
    *,
    role: str = "worker",
    status: str = "completed",
    selector: str | None = None,
    finished_at: str | None = "2026-09-11T00:00:02+00:00",
    original_plan_path: Path | None = None,
    active_plan_path: Path | None = None,
    snapshot_before: PlanSnapshot | None = None,
    snapshot_after: PlanSnapshot | None = None,
) -> None:
    """Write the fields emitted by runlog._turn_result_payload."""
    payload: dict[str, object] = {
        "turn_number": number,
        "step_name": "implement" if role == "worker" else "review",
        "step_role": role,
        "status": status,
        "started_at": "2026-09-11T00:00:01+00:00",
    }
    if selector is not None:
        payload["selector"] = selector
    if finished_at is not None:
        payload["finished_at"] = finished_at
    if original_plan_path is not None:
        payload["original_plan_path"] = str(original_plan_path)
    if active_plan_path is not None:
        payload["active_plan_path"] = str(active_plan_path)
    if snapshot_before is not None:
        payload["snapshot_before"] = asdict(snapshot_before)
    if snapshot_after is not None:
        payload["snapshot_after"] = asdict(snapshot_after)
    _write_json(run_dir / "turns" / f"turn-{number:03d}" / "result.json", payload)


def _write_successful_production_review(
    run_dir: Path,
    number: int,
    *,
    selector: str,
    original_plan_path: Path,
    done: bool,
    transition: str,
) -> None:
    _write_production_turn(
        run_dir,
        number,
        role="reviewer",
        status="running",
        selector=selector,
        original_plan_path=original_plan_path,
    )
    result_path = run_dir / "turns" / f"turn-{number:03d}" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.update(
        {
            "returncode": 0,
            "review_rejection": None,
            "conditions": {
                "DONE": done,
                "NEW_PLAN_EXISTS": False,
                "MAX_TURNS_REACHED": False,
            },
            "chosen_transition": transition,
            "chosen_transition_condition": "DONE" if done else "NEW_PLAN_EXISTS || !DONE",
        }
    )
    _write_json(result_path, result)


def _write_production_retry_turn(
    run_dir: Path,
    number: int,
    *,
    original_plan_path: Path,
    status: str,
    retry_attempt: int | None,
    retry_next_turn: bool | None = None,
    was_retry: bool | None = None,
    role: str = "worker",
    source_run_id: str | None = None,
    scope_id: str | None = None,
    checkpoint_index: int = 1,
) -> None:
    snapshot = PlanSnapshot(
        current_checkpoint_name=f"Checkpoint {checkpoint_index}: Stage {checkpoint_index}",
        unchecked_checkpoint_count=2 - checkpoint_index,
        current_checkpoint_unchecked_step_count=1,
        is_complete=False,
        total_checkpoint_count=2,
        current_checkpoint_index=checkpoint_index,
    )
    payload = _turn_result_payload(
        turn_number=number,
        invocation=HarnessInvocation(
            label="retry-probe",
            argv=(),
            env={},
            prompt_mode="test",
            system_prompt="",
            user_prompt="",
            effective_prompt="",
        ),
        snapshot_before=snapshot,
        snapshot_after=None if retry_next_turn else snapshot,
        status=status,
        started_at=datetime(2026, 9, 11, 0, number, tzinfo=timezone.utc),
        finished_at=datetime(2026, 9, 11, 0, number, 1, tzinfo=timezone.utc),
        returncode=0,
        step_name="implement" if role == "worker" else "review",
        step_role=role,
        selector="codex.retry",
        original_plan_path=original_plan_path,
        retry_attempt=retry_attempt,
        retry_next_turn=retry_next_turn,
        was_retry=was_retry,
    )
    if source_run_id is not None:
        payload["source_run_id"] = source_run_id
    if scope_id is not None:
        payload["scope_id"] = scope_id
    _write_json(run_dir / "turns" / f"turn-{number:03d}" / "result.json", payload)


def test_canonical_projection_preserves_lineage_counts_identity_and_no_writes(
    tmp_path: Path,
) -> None:
    run_dir, original, overlay = _run(tmp_path)
    original.write_text(_canonical_plan(checked={1, 2, 3, 4}), encoding="utf-8")
    metadata: dict[str, object] = {
        "run_id": "run-1",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "status": "running",
        "activity": "active",
        "phase": "implementing",
        "history_complete": True,
        "active_implementation_scope": {
            "scope_id": "original::checkpoint-5",
            "original_plan_path": str(original),
            "checkpoint_index": 5,
            "checkpoint_name": "Checkpoint 5: Stage 5",
            "opened_turn_number": 50,
            "awaiting_review": False,
        },
        "implementation_attempts": {
            "original::checkpoint-1": [
                {"turn_number": 1, "step_name": "implement", "role": "worker", "selector": "selector.1", "outcome": "accepted", "checkpoint_index": 1}
            ],
            "original::checkpoint-2": [
                {"turn_number": 2, "step_name": "implement", "role": "worker", "selector": "selector.2", "outcome": "accepted", "checkpoint_index": 2}
            ],
            "original::checkpoint-3": [
                {"turn_number": 3, "step_name": "implement", "role": "worker", "selector": "selector.3", "outcome": "accepted", "checkpoint_index": 3}
            ],
            "original::checkpoint-4": [
                {"turn_number": 4, "step_name": "implement", "role": "worker", "selector": "selector.4", "outcome": "accepted", "checkpoint_index": 4}
            ],
            "original::checkpoint-5": [
                {"turn_number": 50, "step_name": "implement", "role": "worker", "selector": "selector.5", "outcome": "rejected", "checkpoint_index": 5},
                {"turn_number": 52, "step_name": "implement", "role": "worker", "selector": "selector.5-repair", "outcome": "progress", "checkpoint_index": 5, "retry_attempt": 1},
            ],
            "original::checkpoint-6": [
                {"turn_number": 62, "step_name": "implement", "role": "worker", "selector": "selector.6-repair", "outcome": "accepted", "checkpoint_index": 6},
            ],
            "original::checkpoint-7": [
                {"turn_number": 72, "step_name": "implement", "role": "worker", "selector": "selector.7-repair", "outcome": "accepted", "checkpoint_index": 7},
            ],
        },
        "review_rejection_history": [
            {
                "scope_id": "original::checkpoint-5",
                "rejection_number": 1,
                "source_run_id": "run-1",
                "review_turn_number": 51,
                "review_step_name": "review",
                "reviewer_selector": "reviewer.5",
                "checkpoint_index": 5,
                "checkpoint_name": "Checkpoint 5: Stage 5",
                "reviewed_implementation_turn_number": 50,
                "review_summary": "repair needed",
            },
            {
                "scope_id": "original::checkpoint-6",
                "rejection_number": 2,
                "source_run_id": "run-1",
                "review_turn_number": 61,
                "review_step_name": "review",
                "reviewer_selector": "reviewer.6",
                "checkpoint_index": 6,
                "checkpoint_name": "Checkpoint 6: Stage 6",
                "reviewed_implementation_turn_number": 60,
                "review_summary": "repair needed",
            },
            {
                "scope_id": "original::checkpoint-7",
                "rejection_number": 3,
                "source_run_id": "run-1",
                "review_turn_number": 71,
                "review_step_name": "review",
                "reviewer_selector": "reviewer.7",
                "checkpoint_index": 7,
                "checkpoint_name": "Checkpoint 7: Stage 7",
                "reviewed_implementation_turn_number": 70,
                "review_summary": "repair needed",
            },
        ],
        "approved_checkpoints": [
            {"checkpoint_index": index, "status": "approved", "decision_number": index}
            for index in range(1, 5)
        ],
        "applied_upgrades": [
            {"change_id": "upgrade-1", "kind": "worker_upgrade", "checkpoint_index": 3, "old_selector": "selector.3", "new_selector": "selector.3b", "old_model": "old-model", "new_model": "new-model", "reason": "capacity"},
            {"change_id": "upgrade-2", "kind": "team_upgrade", "checkpoint_index": 6, "old_team": "team", "new_team": "repair-team", "roles": ["worker", "reviewer"], "reason": "repair"},
        ],
        "pending_changes": [
            {"change_id": "pending-1", "kind": "configuration_change", "status": "pending", "target_team": "future-team"}
        ],
        "prompt": "must not appear in the projection",
    }
    for number, index, selector, outcome in (
        (11, 1, "reviewer.1", "approved"),
        (21, 2, "reviewer.2", "approved"),
        (31, 3, "reviewer.3", "approved"),
        (41, 4, "reviewer.4", "approved"),
        (51, 5, "reviewer.5", "rejected"),
        (61, 6, "reviewer.6", "rejected"),
        (71, 7, "reviewer.7", "rejected"),
    ):
        _write_canonical_turn(
            run_dir,
            number,
            role="reviewer",
            status="completed",
            checkpoint_index=index,
            selector=selector,
            outcome=outcome,
        )
    _write_canonical_turn(
        run_dir,
        52,
        role="worker",
        status="running",
        checkpoint_index=5,
        selector="selector.5-repair",
        outcome="progress",
        finished_at=None,
        retry_attempt=1,
    )

    before = {
        path: path.read_bytes()
        for path in [original, overlay, run_dir / "run.json"]
    }
    detail = project_run_progress_detail(
        run_dir,
        metadata=metadata,
        use_cache=False,
    )
    assert detail.total_checkpoints.value == 11
    assert detail.approved_checkpoints.value == 4
    assert detail.recorded_complete_checkpoints.value == 4
    assert detail.current_checkpoint_ordinal == 5
    assert detail.checkpoints[4].status == "repairing"
    assert detail.checkpoints[5].status == "recorded_complete"
    assert detail.checkpoints[6].status == "recorded_complete"
    assert all(checkpoint.status == "pending" for checkpoint in detail.checkpoints[7:])
    assert detail.worker_attempts.value == 8
    assert detail.repair_passes.value == 3
    assert detail.reviews.value == 7
    assert detail.runtime_retries.value == 1
    assert detail.applied_upgrades.value == 2
    assert detail.current_executor is not None
    assert detail.current_executor.selector == "selector.5-repair"
    assert detail.current_executor.model_display == "Model display 52"
    assert any(event.kind == "review_rejection" for event in detail.events)
    assert any(event.kind == "runtime_retry" for event in detail.events)
    assert len(detail.applied_changes) == 2
    assert len(detail.pending_changes) == 1
    assert "prompt" not in detail.to_dict()
    assert {path: path.read_bytes() for path in before} == before


def test_canonical_projection_distinguishes_known_zero_partial_and_cache_invalidation(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_canonical_plan(checked=set()), encoding="utf-8")
    metadata = {
        "run_id": "run-1",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "status": "running",
        "history_complete": True,
        "active_implementation_scope": None,
    }
    first = project_run_progress_detail(run_dir, metadata=metadata)
    warm = project_run_progress_detail(run_dir, metadata=metadata)
    assert first is warm
    assert first.worker_attempts == warm.worker_attempts == type(first.worker_attempts)(0, "complete")
    assert first.approved_checkpoints == type(first.approved_checkpoints)(0, "complete")

    metadata["status"] = "completed"
    second = project_run_progress_detail(run_dir, metadata=metadata)
    assert second.run_status == "completed"
    assert second is not first

    partial = project_run_progress_detail(
        run_dir,
        metadata={
            "run_id": "run-1",
            "repo_root": str(original.parents[2]),
            "original_plan_path": str(original),
            "status": "running",
        },
        use_cache=False,
    )
    assert partial.worker_attempts.value is None
    assert partial.worker_attempts.coverage == "unavailable"
    assert partial.availability == "partial"

    summary = project_run_progress_summary(run_dir, metadata=metadata, use_cache=False)
    assert not hasattr(summary, "events")


def test_canonical_progress_preserves_distinct_approval_records(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_canonical_plan(total=4, checked={1, 2, 3, 4}), encoding="utf-8")
    approvals = [
        {"checkpoint_index": index, "status": "approved", "decision_number": index}
        for index in range(1, 5)
    ]
    detail = project_run_progress_detail(
        run_dir,
        metadata={
            "run_id": "run-1",
            "repo_root": str(original.parents[2]),
            "original_plan_path": str(original),
            "history_complete": True,
            "approved_checkpoints": [*approvals, dict(approvals[2])],
        },
        use_cache=False,
    )
    summary = project_run_progress_summary(
        run_dir,
        metadata={
            "run_id": "run-1",
            "repo_root": str(original.parents[2]),
            "original_plan_path": str(original),
            "history_complete": True,
            "approved_checkpoints": [*approvals, dict(approvals[2])],
        },
        use_cache=False,
    )

    expected_states = {str(index): "approved" for index in range(1, 5)}
    approval_events = [event for event in detail.events if event.kind == "checkpoint_approval"]
    assert detail.approved_checkpoints == RunProgressCount(value=4, coverage="complete")
    assert summary.approved_checkpoints == RunProgressCount(value=4, coverage="complete")
    assert detail.to_dict()["approved_checkpoints"] == {"value": 4, "coverage": "complete"}
    assert summary.to_dict()["approved_checkpoints"] == {"value": 4, "coverage": "complete"}
    assert detail.checkpoint_states == expected_states
    assert summary.checkpoint_states == expected_states
    assert [checkpoint.status for checkpoint in detail.checkpoints] == [
        "approved",
        "approved",
        "approved",
        "approved",
    ]
    assert len(approval_events) == 4
    assert {event.decision_number for event in approval_events} == {1, 2, 3, 4}
    assert {event.checkpoint_id for event in approval_events} == {
        checkpoint.checkpoint_id for checkpoint in detail.checkpoints
    }
    assert len({event.event_id for event in approval_events}) == 4


def test_canonical_manager_routing_acceptance_is_not_checkpoint_approval(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_canonical_plan(total=1, checked=set()), encoding="utf-8")
    metadata: dict[str, object] = {
        "run_id": "run-1",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
        "review_rejection_history": [{
            "scope_id": "original::checkpoint-1",
            "source_run_id": "run-1",
            "review_turn_number": 1,
            "checkpoint_index": 1,
            "review_summary": "Repair is required before approval.",
            "outcome": "rejected",
        }],
    }
    for decision_number, trigger, action, reason in (
        (1, "post_turn", "continue", "Continue to first review."),
        (2, "reviewer_completed", "retry_current_step", "Retry after a recorded rejection."),
        (3, "reviewer_completed", "upgrade_next_implementation", "Upgrade the next implementation."),
    ):
        _write_json(
            run_dir / "manager" / f"decision-{decision_number:03d}" / "result.json",
            {
                "decision_number": decision_number,
                "finalized_turn_number": decision_number,
                "level": "lite",
                "trigger": trigger,
                "status": "accepted",
                "schema_version": 1,
                "action": action,
                "reason": reason,
            },
        )

    detail = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)

    assert detail.approved_checkpoints == RunProgressCount(value=0, coverage="complete")
    assert detail.checkpoint_states["1"] != "approved"
    assert all(event.kind != "checkpoint_approval" for event in detail.events)
    assert detail.to_dict()["approved_checkpoints"] == {"value": 0, "coverage": "complete"}

    metadata["review_approvals"] = [{
        "event_id": "review-approved-1",
        "checkpoint_index": 1,
        "review_approved": True,
        "turn_number": 4,
        "source_run_id": "run-1",
    }]
    approved = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)

    assert approved.approved_checkpoints == RunProgressCount(value=1, coverage="complete")
    assert approved.checkpoint_states == {"1": "approved"}
    assert any(
        event.event_id == "review-approved-1"
        and event.kind == "checkpoint_approval"
        and event.outcome == "approved"
        for event in approved.events
    )


def test_canonical_checkpoint_states_preserve_noncontiguous_identity(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_canonical_plan(total=3, checked={2}), encoding="utf-8")
    metadata = {
        "run_id": "run-1",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
        "active_implementation_scope": {**_scope(3, "Checkpoint 3: Stage 3"), "awaiting_review": False},
        "approved_checkpoints": [{"checkpoint_index": 2, "status": "approved"}],
    }

    detail = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    summary = project_run_progress_summary(run_dir, metadata=metadata, use_cache=False)

    expected = {"1": "pending", "2": "approved", "3": "pending"}
    assert detail.checkpoint_states == expected
    assert summary.checkpoint_states == expected
    assert [checkpoint.status for checkpoint in detail.checkpoints] == [
        "pending", "approved", "pending",
    ]


def test_canonical_checkpoint_states_keep_known_approval_with_partial_history(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_canonical_plan(total=3, checked=set()), encoding="utf-8")
    malformed = run_dir / "turns" / "turn-001" / "result.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text('{"turn_number": 1,', encoding="utf-8")
    metadata = {
        "run_id": "run-1",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
        "approved_checkpoints": [{"checkpoint_index": 2, "status": "approved"}],
    }

    detail = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)

    assert detail.availability == "partial"
    assert detail.checkpoint_states == {"1": "unknown", "2": "approved", "3": "unknown"}


def test_canonical_checkpoint_states_default_empty_and_omit_large_plans(
    tmp_path: Path,
) -> None:
    assert RunProgressSummary().checkpoint_states == {}
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_canonical_plan(total=31, checked={2}), encoding="utf-8")
    detail = project_run_progress_detail(
        run_dir,
        metadata={
            "run_id": "run-1",
            "repo_root": str(original.parents[2]),
            "original_plan_path": str(original),
            "history_complete": True,
        },
        use_cache=False,
    )

    assert detail.total_checkpoints == RunProgressCount(value=31, coverage="complete")
    assert detail.checkpoint_states == {}


def test_canonical_projection_keeps_unassigned_and_malformed_optional_history_explicit(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_canonical_plan(checked=set()), encoding="utf-8")
    metadata = {
        "run_id": "run-1",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
    }
    (run_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "event_id": "unassigned-1",
                "kind": "worker_attempt",
                "source_run_id": "run-1",
                "turn_number": 99,
                "prompt": "never expose this",
            }
        )
        + "\nnot-json\n",
        encoding="utf-8",
    )
    (run_dir / "publication.json").write_text("not-json\n", encoding="utf-8")

    detail = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)

    unassigned = next(event for event in detail.events if event.event_id == "unassigned-1")
    assert unassigned.checkpoint_id is None
    assert unassigned.reason == "Unassigned history"
    assert "never expose" not in detail.to_dict().__repr__()
    assert "malformed_optional_evidence" in detail.reason_codes
    assert all(stage.status == "unknown" for stage in detail.delivery)


def test_canonical_progress_preserves_uncertainty_after_unreadable_history(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_plan(total=1, current=None), encoding="utf-8")
    metadata = {
        "run_id": "run-1",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
    }
    _write_production_turn(run_dir, 1, selector="codex.old-worker")
    malformed = run_dir / "turns" / "turn-002" / "result.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text(
        '{"turn_number": 2, "step_role": "reviewer",', encoding="utf-8"
    )
    before = {
        path: path.read_bytes()
        for path in (original, run_dir / "run.json", malformed)
    }

    incomplete = project_run_progress_detail(run_dir, metadata=metadata, use_cache=True)

    assert incomplete.availability == "partial"
    assert incomplete.worker_attempts == RunProgressCount(value=1, coverage="partial")
    assert incomplete.reviews == RunProgressCount(value=0, coverage="partial")
    assert incomplete.last_executor is None
    retained = next(event for event in incomplete.events if event.turn_number == 1)
    assert retained.executor is not None
    assert retained.executor.selector == "codex.old-worker"
    assert incomplete.truncation.omitted_records >= 1
    assert {path: path.read_bytes() for path in before} == before

    without_flag = project_run_progress_detail(
        run_dir,
        metadata={key: value for key, value in metadata.items() if key != "history_complete"},
        use_cache=False,
    )
    assert without_flag.availability == "partial"
    assert without_flag.last_executor is None

    malformed.write_text("[]\n", encoding="utf-8")
    non_object = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    assert non_object.worker_attempts.coverage == "partial"
    assert non_object.reviews.coverage == "partial"
    assert non_object.last_executor is None

    (run_dir / "events.jsonl").write_text("not-json\n", encoding="utf-8")
    malformed_events = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    assert malformed_events.availability == "partial"
    assert malformed_events.worker_attempts.coverage == "partial"

    (run_dir / "events.jsonl").unlink()
    _write_production_turn(run_dir, 2, selector="codex.new-worker")
    restored = project_run_progress_detail(run_dir, metadata=metadata, use_cache=True)
    assert restored.availability == "complete"
    assert restored.worker_attempts == RunProgressCount(value=2, coverage="complete")
    assert restored.reviews == RunProgressCount(value=0, coverage="complete")
    assert restored.last_executor is not None
    assert restored.last_executor.selector == "codex.new-worker"

    malformed_events_dir, malformed_events_original, _ = _run(tmp_path / "malformed-events")
    malformed_events_original.write_text(_plan(total=1, current=1), encoding="utf-8")
    (malformed_events_dir / "events.jsonl").write_text("[]\nnot-json\n", encoding="utf-8")
    event_only = project_run_progress_detail(
        malformed_events_dir,
        metadata={
            "run_id": "malformed-events",
            "repo_root": str(malformed_events_original.parents[2]),
            "original_plan_path": str(malformed_events_original),
            "history_complete": True,
        },
        use_cache=False,
    )
    assert event_only.availability == "partial"
    assert event_only.worker_attempts == RunProgressCount(value=0, coverage="partial")
    assert event_only.reviews == RunProgressCount(value=0, coverage="partial")

    known_zero_dir, known_zero_original, _ = _run(tmp_path / "known-zero")
    known_zero_original.write_text(_plan(total=1, current=1), encoding="utf-8")
    known_zero = project_run_progress_detail(
        known_zero_dir,
        metadata={
            "run_id": "known-zero",
            "repo_root": str(known_zero_original.parents[2]),
            "original_plan_path": str(known_zero_original),
            "history_complete": True,
        },
        use_cache=False,
    )
    assert known_zero.availability == "complete"
    assert known_zero.worker_attempts == RunProgressCount(value=0, coverage="complete")
    assert known_zero.reviews == RunProgressCount(value=0, coverage="complete")


def test_canonical_projection_does_not_promote_plan_after_invalid_scope_evidence(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    metadata = {
        "run_id": "run-1",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "active_implementation_scope": {
            **_scope(),
            "envelope_artifact_path": "scopes/wrong/envelope.json",
            "envelope_artifact_sha256": "a" * 64,
            "envelope_canonical_sha256": "b" * 64,
        },
    }

    detail = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)

    assert detail.availability == "partial"
    assert detail.total_checkpoints.value is None
    assert "invalid_evidence" in detail.reason_codes


def test_canonical_projection_excludes_pure_harness_relaunch_from_attempts(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_canonical_plan(checked=set()), encoding="utf-8")
    metadata = {
        "run_id": "run-1",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
        "implementation_attempts": {
            "original::checkpoint-1": [
                {
                    "turn_number": 1,
                    "step_name": "implement",
                    "role": "worker",
                    "checkpoint_index": 1,
                    "selector": "selector.worker",
                    "outcome": "accepted",
                }
            ]
        },
    }
    _write_canonical_turn(
        run_dir,
        2,
        role="worker",
        status="retry-scheduled",
        checkpoint_index=1,
        selector="selector.worker",
        outcome="retry",
        retry_attempt=1,
    )

    detail = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)

    assert detail.worker_attempts.value == 1
    assert detail.runtime_retries.value == 1
    assert [event.kind for event in detail.events].count("runtime_retry") == 1


def test_canonical_progress_deduplicates_proven_retry_lifecycle(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_canonical_plan(total=2, checked=set()), encoding="utf-8")
    metadata = {
        "run_id": "run-current",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
    }
    _write_production_retry_turn(
        run_dir,
        1,
        original_plan_path=original,
        status="retry-scheduled",
        retry_attempt=1,
        retry_next_turn=True,
    )

    scheduled = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    scheduled_event = next(event for event in scheduled.events if event.kind == "runtime_retry")
    assert scheduled.runtime_retries.value == 1
    assert scheduled.checkpoints[0].runtime_retries.value == 1
    assert scheduled_event.executor is not None
    assert scheduled_event.executor.role == "worker"
    assert scheduled_event.executor.selector == "codex.retry"
    assert scheduled_event.source_run_id == "run-current"
    assert scheduled_event.turn_number == 1

    _write_production_retry_turn(
        run_dir,
        2,
        original_plan_path=original,
        status="completed",
        retry_attempt=1,
        was_retry=True,
    )
    completed = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    completed_events = [event for event in completed.events if event.kind == "runtime_retry"]
    assert completed.runtime_retries.value == 1
    assert completed.checkpoints[0].runtime_retries.value == 1
    assert [event.event_id for event in completed_events] == [scheduled_event.event_id]
    assert completed_events[0].turn_number == 1
    assert completed_events[0].source_reference["completion_turn_number"] == 2
    assert completed_events[0].executor is not None
    assert completed_events[0].executor.role == "worker"
    assert completed_events[0].executor.selector == "codex.retry"
    assert completed_events[0].source_run_id == "run-current"
    refreshed = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    assert [event.event_id for event in refreshed.events if event.kind == "runtime_retry"] == [
        scheduled_event.event_id
    ]

    _write_production_retry_turn(
        run_dir,
        3,
        original_plan_path=original,
        status="retry-scheduled",
        retry_attempt=1,
        retry_next_turn=True,
    )
    _write_production_retry_turn(
        run_dir,
        4,
        original_plan_path=original,
        status="completed",
        retry_attempt=1,
        was_retry=True,
    )
    _write_production_retry_turn(
        run_dir,
        5,
        original_plan_path=original,
        status="retry-scheduled",
        retry_attempt=1,
        retry_next_turn=True,
    )
    _write_production_retry_turn(
        run_dir,
        6,
        original_plan_path=original,
        status="retry-scheduled",
        retry_attempt=2,
        retry_next_turn=True,
    )
    _write_production_retry_turn(
        run_dir,
        7,
        original_plan_path=original,
        status="completed",
        retry_attempt=2,
        was_retry=True,
    )
    chained = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    retry_events = [event for event in chained.events if event.kind == "runtime_retry"]
    assert chained.runtime_retries.value == 4
    assert [event.turn_number for event in retry_events] == [1, 3, 5, 6]
    assert [event.source_reference.get("completion_turn_number") for event in retry_events] == [
        2,
        4,
        None,
        7,
    ]


def test_canonical_progress_retains_unproven_retry_pairs(tmp_path: Path) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_canonical_plan(total=2, checked=set()), encoding="utf-8")
    metadata = {
        "run_id": "run-current",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
    }
    _write_production_retry_turn(
        run_dir,
        1,
        original_plan_path=original,
        status="retry-scheduled",
        retry_attempt=1,
        retry_next_turn=True,
        scope_id="original::checkpoint-1",
    )
    _write_production_retry_turn(
        run_dir,
        2,
        original_plan_path=original,
        status="completed",
        retry_attempt=1,
        was_retry=True,
        scope_id="original::checkpoint-2",
        checkpoint_index=2,
    )
    _write_production_retry_turn(
        run_dir,
        3,
        original_plan_path=original,
        status="retry-scheduled",
        retry_attempt=1,
        retry_next_turn=True,
        source_run_id="inherited-run",
    )
    _write_production_retry_turn(
        run_dir,
        4,
        original_plan_path=original,
        status="completed",
        retry_attempt=1,
        was_retry=True,
    )
    _write_production_retry_turn(
        run_dir,
        5,
        original_plan_path=original,
        status="retry-scheduled",
        retry_attempt=1,
        retry_next_turn=True,
        role="reviewer",
        source_run_id="review-run",
    )
    _write_production_retry_turn(
        run_dir,
        6,
        original_plan_path=original,
        status="completed",
        retry_attempt=None,
        was_retry=True,
        role="reviewer",
        source_run_id="review-run",
    )
    _write_production_retry_turn(
        run_dir,
        7,
        original_plan_path=original,
        status="retry-scheduled",
        retry_attempt=1,
        retry_next_turn=True,
        role="reviewer",
        source_run_id="review-run",
    )
    _write_production_retry_turn(
        run_dir,
        8,
        original_plan_path=original,
        status="completed",
        retry_attempt=1,
        was_retry=True,
        role="reviewer",
        source_run_id="review-run",
    )

    detail = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    retries = [event for event in detail.events if event.kind == "runtime_retry"]

    assert len(retries) == 7
    assert detail.runtime_retries.value == 3
    assert detail.checkpoints[0].runtime_retries.value == 2
    assert detail.checkpoints[1].runtime_retries.value == 1
    assert [
        (event.source_run_id, event.source_reference.get("completion_turn_number"))
        for event in retries
        if event.source_run_id == "review-run"
    ] == [("review-run", None), ("review-run", None), ("review-run", 8)]
    reviewer_retries = [event for event in retries if event.source_run_id == "review-run"]
    assert [event.turn_number for event in reviewer_retries] == [5, 6, 7]
    assert all(event.executor is not None for event in reviewer_retries)
    assert [event.executor.role for event in reviewer_retries] == ["reviewer"] * 3
    assert [event.executor.selector for event in reviewer_retries] == ["codex.retry"] * 3


def test_canonical_delivery_does_not_infer_ci_or_live_from_published_receipt(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_canonical_plan(checked=set()), encoding="utf-8")
    (run_dir / "publication.json").write_text(
        json.dumps({"status": "published", "commit": "abc123"}) + "\n",
        encoding="utf-8",
    )
    metadata = {
        "run_id": "run-1",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
    }

    delivery = project_run_progress_detail(
        run_dir, metadata=metadata, use_cache=False
    ).delivery

    assert {stage.stage: stage.status for stage in delivery}["Publish"] == "succeeded"
    assert {stage.stage: stage.status for stage in delivery}["CI"] == "unknown"
    assert {stage.stage: stage.status for stage in delivery}["Live verification"] == "unknown"


def test_canonical_delivery_does_not_infer_merge_from_committed_lifecycle(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_canonical_plan(checked=set()), encoding="utf-8")
    _write_json(run_dir / "publication.json", {"plan_lifecycle": {"phase": "committed"}})
    delivery = project_run_progress_detail(
        run_dir,
        metadata={
            "run_id": "run-1",
            "repo_root": str(original.parents[2]),
            "original_plan_path": str(original),
            "history_complete": True,
        },
        use_cache=False,
    ).delivery

    statuses = {stage.stage: stage.status for stage in delivery}
    assert statuses["Merge"] == "unknown"
    assert statuses["Publish"] == "unknown"
    assert statuses["CI"] == "unknown"
    assert statuses["Live verification"] == "unknown"


def test_canonical_delivery_preserves_explicit_merge_receipts(tmp_path: Path) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_canonical_plan(checked=set()), encoding="utf-8")
    metadata = {
        "run_id": "run-1",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
    }

    for status in ("succeeded", "failed"):
        _write_json(run_dir / "publication.json", {"stages": {"Merge": {"status": status}}})
        delivery = project_run_progress_detail(
            run_dir, metadata=metadata, use_cache=False
        ).delivery
        assert {stage.stage: stage.status for stage in delivery}["Merge"] == status


def test_closed_scope_uses_next_original_checkpoint_and_ignores_overlay(
    tmp_path: Path,
) -> None:
    run_dir, original, overlay = _run(tmp_path)
    original.write_text(_plan(current=5), encoding="utf-8")
    _write_json(
        run_dir / "run.json",
        {
            "repo_root": str(original.parents[2]),
            "original_plan_path": str(original),
            "active_plan_path": str(overlay),
            "active_implementation_scope": None,
        },
    )
    stale_context = {
        "controller_state": {"active_implementation_scope": _scope()},
    }

    progress = project_run_progress(run_dir, manager_context=stale_context)

    assert progress["availability"] == "available"
    assert progress["checkpoint"] == {"index": 5, "name": "Checkpoint 5: Stage 5"}
    assert progress["total"] == 14
    assert progress["repairing"] is False
    assert progress["overlay_path"] is None


def test_canonical_progress_rejects_symlinked_artifact_ancestors(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path / "contained")
    metadata = {
        "run_id": "run-1",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
    }
    outside = tmp_path / "outside"
    _write_json(
        outside / "turn-001" / "result.json",
        {
            "turn_number": 1,
            "step_role": "worker",
            "status": "completed",
            "selector": "outside-private-selector",
            "reason": "outside-private-reason",
        },
    )
    _write_json(
        outside / "decision-001" / "result.json",
        {
            "decision_number": 1,
            "status": "accepted",
            "selector": "outside-manager-selector",
            "reason": "outside-manager-reason",
        },
    )
    (run_dir / "turns").mkdir()
    (run_dir / "manager").mkdir()
    os.symlink(outside / "turn-001", run_dir / "turns" / "turn-001")
    os.symlink(outside / "decision-001", run_dir / "manager" / "decision-001")
    _write_production_turn(
        run_dir,
        2,
        selector="contained-selector",
    )

    before = {
        path: path.read_bytes()
        for path in (original, run_dir / "run.json")
    }
    detail = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)

    assert detail.worker_attempts.value == 1
    assert detail.last_executor is not None
    assert detail.last_executor.selector == "contained-selector"
    serialized = json.dumps(detail.to_dict())
    assert "outside-private-selector" not in serialized
    assert "outside-private-reason" not in serialized
    assert "outside-manager-selector" not in serialized
    assert "outside-manager-reason" not in serialized
    assert {path: path.read_bytes() for path in before} == before

    intermediate, intermediate_original, _ = _run(tmp_path / "intermediate")
    intermediate_metadata = {
        "run_id": "run-1",
        "repo_root": str(intermediate_original.parents[2]),
        "original_plan_path": str(intermediate_original),
        "history_complete": True,
    }
    nested_outside = tmp_path / "nested-outside"
    _write_json(
        nested_outside / "turn-003" / "result.json",
        {
            "turn_number": 3,
            "step_role": "worker",
            "status": "completed",
            "selector": "outside-intermediate-selector",
        },
    )
    os.symlink(nested_outside, intermediate / "turns")
    intermediate_detail = project_run_progress_detail(
        intermediate,
        metadata=intermediate_metadata,
        use_cache=False,
    )
    assert intermediate_detail.worker_attempts.value == 0
    assert intermediate_detail.worker_attempts.coverage == "partial"
    assert "outside-intermediate-selector" not in json.dumps(
        intermediate_detail.to_dict()
    )


def test_canonical_progress_merges_production_attempt_and_result(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_plan(total=4, current=None), encoding="utf-8")
    repair = original.parents[2] / "execution" / "repair.md"
    metadata: dict[str, object] = {
        "run_id": "run-current",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "status": "running",
        "activity": "active",
        "history_complete": True,
        "active_implementation_scope": None,
        "implementation_attempts": {
            f"original::checkpoint-{number}": [asdict(ImplementationAttempt(
                turn_number=number, step_name="implement", role="worker",
                team="historical-team", selector=f"codex.historical-{number}",
                outcome="accepted",
            ))]
            for number in range(1, 5)
        },
    }
    for number in range(1, 5):
        _write_production_turn(
            run_dir, number, selector=f"codex.historical-{number}",
            original_plan_path=original, active_plan_path=repair,
            snapshot_before=PlanSnapshot(
                current_checkpoint_name=f"Checkpoint {number}: Stage {number}",
                current_checkpoint_index=number, unchecked_checkpoint_count=5 - number,
                current_checkpoint_unchecked_step_count=1, is_complete=False,
                total_checkpoint_count=4,
            ),
            # The post-turn snapshot deliberately points at the next checkpoint.
            snapshot_after=PlanSnapshot(
                current_checkpoint_name=(f"Checkpoint {number + 1}: Stage {number + 1}" if number < 4 else None),
                current_checkpoint_index=(number + 1 if number < 4 else None),
                unchecked_checkpoint_count=4 - number,
                current_checkpoint_unchecked_step_count=1 if number < 4 else 0,
                is_complete=number == 4, total_checkpoint_count=4,
            ),
        )
    finished = project_run_progress_detail(
        run_dir, metadata=metadata, use_cache=False
    )
    worker_events = [event for event in finished.events if event.kind == "worker_attempt"]
    assert finished.worker_attempts.value == 4
    assert finished.last_executor is not None
    assert finished.last_executor.team == "historical-team"
    assert finished.last_executor.selector == "codex.historical-4"
    assert finished.last_executor.ended_at == "2026-09-11T00:00:02+00:00"
    assert len(worker_events) == 4
    assert [event.turn_number for event in worker_events] == [1, 2, 3, 4]
    assert [event.checkpoint_id for event in worker_events] == [
        checkpoint.checkpoint_id for checkpoint in finished.checkpoints
    ]
    assert all(event.executor is not None and event.executor.team == "historical-team" for event in worker_events)

    mismatched = dict(metadata)
    _write_production_turn(
        run_dir, 5, selector="codex.wrong-plan", original_plan_path=repair,
        snapshot_before=PlanSnapshot(
            "Checkpoint 4: Stage 4", 1, 1, False, 4, current_checkpoint_index=4
        ),
    )
    mismatched["implementation_attempts"] = {
        **metadata["implementation_attempts"],
        "original::checkpoint-4": [asdict(ImplementationAttempt(
            5, "implement", "worker", "wrong-team", "codex.wrong-plan", "accepted"
        ))],
        "original::checkpoint-1": [
            {**asdict(ImplementationAttempt(1, "implement", "worker", "inherited-team", "codex.inherited", "accepted")), "source_run_id": "run-predecessor"}
        ],
    }
    mismatched_detail = project_run_progress_detail(run_dir, metadata=mismatched, use_cache=False)
    assert any(event.turn_number == 5 and event.checkpoint_id is None for event in mismatched_detail.events)
    assert any(event.source_run_id == "run-predecessor" and event.checkpoint_id is None for event in mismatched_detail.events)


def test_canonical_progress_ids_survive_recorded_completion(tmp_path: Path) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_plan(total=2, current=1), encoding="utf-8")
    metadata = {
        "run_id": "run-1",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
    }
    before = project_run_progress_detail(
        run_dir, metadata=metadata, use_cache=False
    )
    before_ids = tuple(item.checkpoint_id for item in before.checkpoints)
    before_identity = before.original_plan_identity

    original.write_text(_plan(total=2, current=None), encoding="utf-8")
    after = project_run_progress_detail(
        run_dir, metadata=metadata, use_cache=False
    )

    assert after.original_plan_identity == before_identity
    assert tuple(item.checkpoint_id for item in after.checkpoints) == before_ids


def test_canonical_progress_enforces_combined_latest_first_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_plan(total=3, current=None), encoding="utf-8")
    metadata = {
        "run_id": "run-1",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
    }
    for number in range(1, 7):
        _write_production_turn(
            run_dir,
            number,
            selector=f"turn-{number}",
        )
    events = [
        json.dumps(
            {
                "event_id": f"event-{number}",
                "kind": "history",
                "turn_number": number,
                "reason": f"event-{number}",
            }
        )
        for number in range(1, 7)
    ]
    (run_dir / "events.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "aflow.control_plane.run_progress._PROGRESS_MAX_RECORDS", 8
    )
    monkeypatch.setattr(
        Path,
        "rglob",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unbounded scan")),
    )

    cold = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    warm = project_run_progress_detail(run_dir, metadata=metadata, use_cache=True)
    warm_again = project_run_progress_detail(run_dir, metadata=metadata, use_cache=True)

    assert cold.truncation.records_read <= 8
    assert cold.truncation.omitted_records > 0
    assert any(event.reason is not None and "event-6" in event.reason for event in cold.events)
    assert cold.truncation.response_limit_records > 0
    assert any(
        executor.selector == "turn-6"
        for executor in (cold.current_executor, cold.last_executor)
        if executor is not None
    )
    assert warm.truncation.records_read == cold.truncation.records_read
    assert tuple(event.event_id for event in warm.events) == tuple(
        event.event_id for event in cold.events
    )
    assert tuple(event.event_id for event in warm_again.events) == tuple(
        event.event_id for event in warm.events
    )

    oversized = run_dir / "turns" / "turn-001" / "result.json"
    oversized_payload = json.loads(oversized.read_text(encoding="utf-8"))
    oversized_payload["reason"] = "old evidence " * 500
    _write_json(oversized, oversized_payload)
    monkeypatch.setattr(
        "aflow.control_plane.run_progress._PROGRESS_MAX_EVIDENCE_BYTES", 2_200
    )
    byte_bounded = project_run_progress_detail(
        run_dir, metadata=metadata, use_cache=False
    )
    assert byte_bounded.worker_attempts.coverage == "partial"
    assert byte_bounded.last_executor is not None
    assert byte_bounded.last_executor.selector == "turn-6"

    byte_root, byte_original, _ = _run(tmp_path / "equal-sized")
    byte_original.write_text(_plan(total=3, current=None), encoding="utf-8")
    byte_metadata = {
        "run_id": "equal-sized", "repo_root": str(byte_original.parents[2]),
        "original_plan_path": str(byte_original), "history_complete": True,
    }
    for number, padding in ((1, "a" * 80), (2, "b" * 80), (3, "c" * 120)):
        _write_production_turn(byte_root, number, selector=f"equal-{number}")
        payload_path = byte_root / "turns" / f"turn-{number:03d}" / "result.json"
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        payload["reason"] = padding
        _write_json(payload_path, payload)
    latest_size = (byte_root / "turns" / "turn-003" / "result.json").stat().st_size
    monkeypatch.setattr(
        "aflow.control_plane.run_progress._PROGRESS_MAX_EVIDENCE_BYTES",
        byte_original.stat().st_size + latest_size + 8,
    )
    equal_sized = project_run_progress_detail(byte_root, metadata=byte_metadata, use_cache=False)
    assert equal_sized.last_executor is not None
    assert equal_sized.last_executor.selector == "equal-3"
    assert equal_sized.truncation.omitted_records > 0

    optional_root, optional_original, _ = _run(tmp_path / "optional-history")
    optional_original.write_text(_plan(total=1, current=None), encoding="utf-8")
    _write_production_turn(optional_root, 1, selector="latest-worker")
    oversized_optional = {"payload": "x" * 600}
    _write_json(optional_root / "manager" / "decision-002" / "result.json", oversized_optional)
    (optional_root / "events.jsonl").write_text("x" * 600, encoding="utf-8")
    monkeypatch.setattr(
        "aflow.control_plane.run_progress._PROGRESS_MAX_JSON_BYTES", 512
    )
    monkeypatch.setattr(
        "aflow.control_plane.run_progress._PROGRESS_MAX_EVIDENCE_BYTES", 1_000
    )
    optional_detail = project_run_progress_detail(
        optional_root,
        metadata={
            "run_id": "optional-history", "repo_root": str(optional_original.parents[2]),
            "original_plan_path": str(optional_original), "history_complete": True,
        },
        use_cache=False,
    )
    assert optional_detail.last_executor is not None
    assert optional_detail.last_executor.selector == "latest-worker"
    assert optional_detail.worker_attempts.coverage == "partial"
    assert optional_detail.truncation.omitted_records >= 2

    turn_skip_root, turn_skip_original, _ = _run(tmp_path / "skip-oversized-turn")
    turn_skip_original.write_text(_plan(total=1, current=None), encoding="utf-8")
    _write_production_turn(turn_skip_root, 1, selector="retained-worker")
    _write_production_turn(turn_skip_root, 2, selector="oversized-worker")
    oversized_turn = turn_skip_root / "turns" / "turn-002" / "result.json"
    oversized_turn_payload = json.loads(oversized_turn.read_text(encoding="utf-8"))
    oversized_turn_payload["padding"] = "x" * 600
    _write_json(oversized_turn, oversized_turn_payload)
    turn_skip_detail = project_run_progress_detail(
        turn_skip_root,
        metadata={
            "run_id": "skip-oversized-turn",
            "repo_root": str(turn_skip_original.parents[2]),
            "original_plan_path": str(turn_skip_original),
            "history_complete": True,
        },
        use_cache=False,
    )
    assert turn_skip_detail.last_executor is None
    assert turn_skip_detail.truncation.omitted_records >= 1

    manager_skip_root, _, _ = _run(tmp_path / "skip-oversized-manager")
    _write_json(
        manager_skip_root / "manager" / "decision-001" / "result.json",
        {"decision_number": 1, "status": "accepted"},
    )
    _write_json(
        manager_skip_root / "manager" / "decision-002" / "result.json",
        {"padding": "x" * 600},
    )
    _write_json(
        manager_skip_root / "manager" / "decision-002" / "boundary.json",
        {"boundary": {"decision_number": 2}},
    )
    manager_budget = _ProgressBudget()
    manager_records = _canonical_manager_records(manager_skip_root, manager_budget)
    assert any(record.get("decision_number") == 1 for record in manager_records)
    retained_boundary = next(
        record
        for record in manager_records
        if record["_artifact"] == "manager/decision-002/result.json"
    )
    assert retained_boundary["_boundary"]["decision_number"] == 2
    assert manager_budget.omitted_records >= 1

    traversal_root, traversal_original, _ = _run(tmp_path / "bounded-discovery")
    traversal_original.write_text(_plan(total=1, current=None), encoding="utf-8")
    for number in range(1, 31):
        _write_production_turn(
            traversal_root,
            number,
            selector=f"discovery-{number}",
            status="running" if number == 3 else "completed",
            finished_at=None if number == 3 else "2026-09-11T00:00:02+00:00",
        )
    direct_entries = 0
    original_scandir = os.scandir

    class AscendingScan:
        def __init__(self) -> None:
            self.names = iter(f"turn-{number:03d}" for number in range(1, 31))

        def __enter__(self) -> "AscendingScan":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> "AscendingScan":
            return self

        def __next__(self) -> SimpleNamespace:
            nonlocal direct_entries
            direct_entries += 1
            return SimpleNamespace(name=next(self.names))

    def counted_scandir(path: os.PathLike[str] | str) -> os.ScandirIterator | AscendingScan:
        if Path(path) == traversal_root / "turns":
            return AscendingScan()
        return original_scandir(path)

    monkeypatch.setattr("aflow.control_plane.run_progress.os.scandir", counted_scandir)
    monkeypatch.setattr(
        "aflow.control_plane.run_progress._PROGRESS_MAX_EVIDENCE_BYTES", 8 * 1024 * 1024
    )
    monkeypatch.setattr(
        "aflow.control_plane.run_progress._PROGRESS_CACHE_DISCOVERY_LIMIT", 3
    )
    bounded = project_run_progress_detail(
        traversal_root,
        metadata={
            "run_id": "bounded-discovery", "repo_root": str(traversal_original.parents[2]),
            "original_plan_path": str(traversal_original), "history_complete": True,
            "activity": "active",
        },
        use_cache=False,
    )
    assert direct_entries <= 4
    assert bounded.worker_attempts.coverage == "partial"
    assert bounded.current_executor is None
    assert bounded.last_executor is None
    assert any(
        "may include newer entries" in notice
        and "lower bounds" in notice
        for notice in bounded.truncation.notices
    )
    assert bounded.truncation.omitted_records >= 1
    direct_entries = 0
    warm = project_run_progress_detail(
        traversal_root,
        metadata={
            "run_id": "bounded-discovery", "repo_root": str(traversal_original.parents[2]),
            "original_plan_path": str(traversal_original), "history_complete": True,
            "activity": "active",
        },
        use_cache=True,
    )
    assert direct_entries <= 4
    assert warm.current_executor is None
    assert warm.last_executor is None

    exact_root, exact_original, _ = _run(tmp_path / "complete-discovery")
    exact_original.write_text(_plan(total=1, current=None), encoding="utf-8")
    for number in range(1, 4):
        _write_production_turn(exact_root, number, selector=f"exact-{number}")
    exact = project_run_progress_detail(
        exact_root,
        metadata={
            "run_id": "complete-discovery",
            "repo_root": str(exact_original.parents[2]),
            "original_plan_path": str(exact_original),
            "history_complete": True,
        },
        use_cache=False,
    )
    assert exact.last_executor is not None
    assert exact.last_executor.selector == "exact-3"


def test_canonical_progress_projects_durable_pending_team_override(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_plan(total=2, current=2), encoding="utf-8")
    scope = {
        "scope_id": "original::checkpoint-2",
        "original_plan_path": str(original),
        "checkpoint_index": 2,
        "checkpoint_name": "Checkpoint 2: Stage 2",
    }
    pending_override = asdict(PendingTeamOverride(
        target_step="implement", role="worker", source_team="base-team",
        target_team="upgrade-team", selector="codex.upgrade",
        checkpoint_identity="target-plan::checkpoint-2", decision_number=7,
        scope_id=scope["scope_id"], target_plan_identity="target-plan::checkpoint-2",
    ))
    metadata: dict[str, object] = {
        "run_id": "run-1",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
        "active_implementation_scope": scope,
        "pending_step_team_override": pending_override,
    }
    detail = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    summary = project_run_progress_summary(run_dir, metadata=metadata, use_cache=False)

    assert detail.applied_upgrades.value == 0
    assert len(detail.pending_changes) == 1
    pending = detail.pending_changes[0]
    assert pending.kind == "team_upgrade"
    assert pending.old_team == "base-team"
    assert pending.new_team == "upgrade-team"
    assert pending.new_selector == "codex.upgrade"
    assert pending.checkpoint_id == detail.checkpoints[1].checkpoint_id
    assert summary.applied_upgrades == detail.applied_upgrades

    ordinary_boundary = asdict(PendingBoundaryDecision(
        finalized_turn_number=7, decision_number=8, action="continue",
        proposed_action="continue", proposed_transition="review",
        resolved_next_step="review", target_role="reviewer", target_team="base-team",
        target_selector="codex.base", checkpoint_identity="target-plan::checkpoint-2",
        applied=True, consumed=True, scope_id=scope["scope_id"],
    ))
    ordinary_metadata = {
        **metadata, "pending_step_team_override": None,
        "pending_boundary_decision": ordinary_boundary,
    }
    ordinary_detail = project_run_progress_detail(
        run_dir, metadata=ordinary_metadata, use_cache=False
    )
    assert ordinary_detail.applied_changes == ()
    assert ordinary_detail.pending_changes == ()

    for decision_number, action, next_step in (
        (12, "continue", "implement"),
        (13, "review", "review"),
        (14, "stop", None),
    ):
        ordinary_pending = asdict(PendingBoundaryDecision(
            finalized_turn_number=decision_number, decision_number=decision_number,
            action=action, proposed_action=action, proposed_transition=action,
            resolved_next_step=next_step, target_role="worker", target_team="base-team",
            target_selector="codex.base", checkpoint_identity="target-plan::checkpoint-2",
            scope_id=scope["scope_id"],
        ))
        pending_routing = project_run_progress_detail(
            run_dir,
            metadata={
                **metadata, "pending_step_team_override": None,
                "pending_boundary_decision": ordinary_pending,
            },
            use_cache=False,
        )
        assert pending_routing.applied_changes == ()
        assert pending_routing.pending_changes == ()

    retained_override = {
        **pending_override,
        "target_team": "base-team",
        "selector": "codex.base",
        "decision_number": 11,
    }
    retained_detail = project_run_progress_detail(
        run_dir,
        metadata={
            **metadata,
            "pending_step_team_override": retained_override,
            "pending_boundary_decision": None,
        },
        use_cache=False,
    )
    assert len(retained_detail.pending_changes) == 1
    assert retained_detail.pending_changes[0].kind == "configuration_change"
    assert retained_detail.pending_changes[0].old_team == "base-team"
    assert retained_detail.pending_changes[0].new_team == "base-team"

    applied_boundary = asdict(PendingBoundaryDecision(
        finalized_turn_number=6, decision_number=7, action="upgrade_next_implementation",
        proposed_action="continue", proposed_transition=None, resolved_next_step="implement",
        target_role="worker", target_team="upgrade-team", target_selector="codex.upgrade",
        checkpoint_identity="target-plan::checkpoint-2", applied=True, consumed=True,
        scope_id=scope["scope_id"],
    ))
    applied_metadata = {
        **metadata,
        "pending_step_team_override": {**pending_override, "consumed": True},
        "pending_boundary_decision": applied_boundary,
    }
    applied_detail = project_run_progress_detail(
        run_dir, metadata=applied_metadata, use_cache=False
    )
    assert applied_detail.applied_upgrades.value == 1
    assert len(applied_detail.applied_changes) == 1
    assert applied_detail.pending_changes == ()
    applied = applied_detail.applied_changes[0]
    assert applied.old_team == "base-team"
    assert applied.new_team == "upgrade-team"

    stale_override = {
        **pending_override,
        "decision_number": 9,
        "consumed": False,
    }
    consumed_boundary = asdict(PendingBoundaryDecision(
        finalized_turn_number=8, decision_number=9, action="upgrade_next_implementation",
        proposed_action="continue", proposed_transition=None, resolved_next_step="implement",
        target_role="worker", target_team="upgrade-team", target_selector="codex.upgrade",
        checkpoint_identity="target-plan::checkpoint-2", consumed=True,
        scope_id=scope["scope_id"],
    ))
    _write_json(
        run_dir / "manager" / "decision-009" / "boundary.json",
        {"boundary": {}, "run_metadata": {"pending_boundary_decision": consumed_boundary}},
    )
    reconciled = project_run_progress_detail(
        run_dir, metadata={
            **metadata, "pending_step_team_override": stale_override,
            "pending_boundary_decision": None,
        }, use_cache=False,
    )
    assert reconciled.pending_changes == ()
    reconciled_summary = project_run_progress_summary(
        run_dir, metadata={
            **metadata, "pending_step_team_override": stale_override,
            "pending_boundary_decision": None,
        }, use_cache=False,
    )
    assert reconciled_summary.applied_upgrades == reconciled.applied_upgrades

    missing_mapping = {
        **applied_boundary,
        "target_team": None,
        "target_selector": None,
        "applied": False,
        "consumed": False,
        "decision_number": 10,
    }
    missing_detail = project_run_progress_detail(
        run_dir,
        metadata={
            **metadata,
            "pending_step_team_override": None,
            "pending_boundary_decision": missing_mapping,
        },
        use_cache=False,
    )
    assert len(missing_detail.pending_changes) == 1
    assert missing_detail.pending_changes[0].old_team is None
    assert missing_detail.pending_changes[0].new_team is None


def test_canonical_progress_merges_production_reviewer_rejection(tmp_path: Path) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_plan(total=2, current=2), encoding="utf-8")
    rejection = asdict(ReviewRejectionRecord(
        scope_id="original::checkpoint-1",
        rejection_number=1,
        source_run_id="run-current",
        review_turn_number=2,
        review_step_name="review",
        reviewer_selector="codex.reviewer-recorded",
        checkpoint_index=1,
        checkpoint_name="Checkpoint 1: Stage 1",
        reviewed_implementation_turn_number=1,
        reviewed_worker_team="base-team",
        reviewed_worker_selector="codex.worker",
        review_summary="The implementation needs a repair.",
        repair_plan_summary="Repair the rejected implementation.",
        review_stdout_artifact_path="turns/turn-002/stdout.txt",
        repair_plan_path="plans/in-progress/repair.md",
    ))
    metadata = {
        "run_id": "run-current",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
        "review_rejection_history": [rejection],
    }
    _write_production_turn(
        run_dir,
        2,
        role="reviewer",
        selector="codex.reviewer-actual",
        original_plan_path=original,
    )
    detail = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    rejected = [event for event in detail.events if event.kind == "review_rejection"]
    assert detail.reviews.value == 1
    assert detail.checkpoints[0].reviews.value == 1
    assert len(rejected) == 1
    assert rejected[0].association == "checkpoint"
    assert rejected[0].outcome == "rejected"
    assert rejected[0].reason == "The implementation needs a repair."
    assert rejected[0].executor is not None
    assert rejected[0].executor.selector == "codex.reviewer-actual"
    assert rejected[0].started_at == "2026-09-11T00:00:01+00:00"
    assert rejected[0].ended_at == "2026-09-11T00:00:02+00:00"
    assert detail.repair_passes.value == 0

    rejection_only_root, rejection_only_original, _ = _run(tmp_path / "rejection-only")
    rejection_only = project_run_progress_detail(
        rejection_only_root,
        metadata={
            **metadata,
            "repo_root": str(rejection_only_original.parents[2]),
            "original_plan_path": str(rejection_only_original),
        },
        use_cache=False,
    )
    rejection_only_event = next(
        event for event in rejection_only.events if event.kind == "review_rejection"
    )
    assert rejection_only_event.outcome == "rejected"
    assert rejection_only_event.executor is not None
    assert rejection_only_event.executor.selector == "codex.reviewer-recorded"

    inherited_rejection = {**rejection, "source_run_id": "run-predecessor"}
    inherited = project_run_progress_detail(
        rejection_only_root,
        metadata={
            **metadata,
            "repo_root": str(rejection_only_original.parents[2]),
            "original_plan_path": str(rejection_only_original),
            "review_rejection_history": [inherited_rejection],
        },
        use_cache=False,
    )
    inherited_event = next(event for event in inherited.events if event.kind == "review_rejection")
    assert inherited_event.source_run_id == "run-predecessor"
    assert inherited_event.association == "checkpoint"
    assert inherited.reviews.value == 0


def test_canonical_progress_keeps_inherited_reviews_out_of_executor_summaries(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_plan(total=1, current=1), encoding="utf-8")
    inherited_rejection = asdict(ReviewRejectionRecord(
        scope_id="original::checkpoint-1",
        rejection_number=1,
        source_run_id="predecessor-run",
        review_turn_number=20,
        review_step_name="review",
        reviewer_selector="predecessor.reviewer",
        checkpoint_index=1,
        checkpoint_name="Checkpoint 1: Stage 1",
        reviewed_implementation_turn_number=19,
        reviewed_worker_team="predecessor-team",
        reviewed_worker_selector="predecessor.worker",
        review_summary="The predecessor requested a repair.",
        repair_plan_summary="Repair the inherited rejection in this successor.",
        review_stdout_artifact_path="turns/turn-020/stdout.txt",
        repair_plan_path="plans/in-progress/repair.md",
    ))
    metadata = {
        "run_id": "successor-run",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
        "review_rejection_history": [inherited_rejection],
    }
    _write_production_turn(
        run_dir,
        1,
        selector="successor.worker",
        original_plan_path=original,
    )

    detail = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    summary = project_run_progress_summary(run_dir, metadata=metadata, use_cache=False)
    assert detail.current_executor is None
    assert detail.last_executor is not None
    assert detail.last_executor.source_run_id == "successor-run"
    assert detail.last_executor.selector == "successor.worker"
    assert summary.last_executor == detail.last_executor
    assert detail.to_dict()["last_executor"]["selector"] == "successor.worker"
    assert detail.worker_attempts == RunProgressCount(value=1, coverage="complete")
    assert detail.reviews == RunProgressCount(value=0, coverage="complete")
    inherited_event = next(event for event in detail.events if event.kind == "review_rejection")
    assert inherited_event.source_run_id == "predecessor-run"
    assert inherited_event.executor is not None
    assert inherited_event.executor.selector == "predecessor.reviewer"

    inherited_only_dir, inherited_only_original, _ = _run(tmp_path / "inherited-only")
    inherited_only_original.write_text(_plan(total=1, current=1), encoding="utf-8")
    inherited_only = project_run_progress_detail(
        inherited_only_dir,
        metadata={
            **metadata,
            "repo_root": str(inherited_only_original.parents[2]),
            "original_plan_path": str(inherited_only_original),
        },
        use_cache=False,
    )
    assert inherited_only.current_executor is None
    assert inherited_only.last_executor is None

    active_dir, active_original, _ = _run(tmp_path / "active-successor")
    active_original.write_text(_plan(total=1, current=1), encoding="utf-8")
    _write_production_turn(
        active_dir,
        1,
        status="running",
        finished_at=None,
        selector="successor.active-worker",
        original_plan_path=active_original,
    )
    active = project_run_progress_detail(
        active_dir,
        metadata={
            **metadata,
            "repo_root": str(active_original.parents[2]),
            "original_plan_path": str(active_original),
            "activity": "active",
        },
        use_cache=False,
    )
    assert active.current_executor is not None
    assert active.current_executor.selector == "successor.active-worker"
    assert active.last_executor is None

    reviewer_dir, reviewer_original, _ = _run(tmp_path / "same-run-reviewer")
    reviewer_original.write_text(_plan(total=1, current=1), encoding="utf-8")
    _write_production_turn(
        reviewer_dir, 1, selector="successor.worker", original_plan_path=reviewer_original
    )
    _write_production_turn(
        reviewer_dir,
        2,
        role="reviewer",
        selector="successor.reviewer",
        original_plan_path=reviewer_original,
    )
    same_run_reviewer = project_run_progress_detail(
        reviewer_dir,
        metadata={
            **metadata,
            "repo_root": str(reviewer_original.parents[2]),
            "original_plan_path": str(reviewer_original),
            "review_rejection_history": [],
        },
        use_cache=False,
    )
    assert same_run_reviewer.last_executor is not None
    assert same_run_reviewer.last_executor.selector == "successor.reviewer"


def test_canonical_progress_preserves_repartition_history(tmp_path: Path) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_plan(total=2, current=1), encoding="utf-8")
    first_scope = ActiveImplementationScope(
        scope_id="original::checkpoint-1",
        original_plan_path=str(original),
        checkpoint_index=1,
        checkpoint_name="Checkpoint 1: Stage 1",
        opened_turn_number=1,
        current_partition_generation_id="generation-one",
    )
    attempts = {
        first_scope.scope_id: [asdict(ImplementationAttempt(
            turn_number=1,
            step_name="implement",
            role="worker",
            team="base-team",
            selector="codex.worker",
            outcome="completed",
        ))]
    }
    first_repartition = asdict(CheckpointRepartitionRecord(
        schema_version=1,
        decision_number=1,
        scope_id=first_scope.scope_id,
        generation_id="generation-one",
        envelope_sha256="envelope-one",
        envelope_artifact_sha256="artifact-one",
        source_plan_sha256="source-one",
        proposal_sha256="proposal-one",
        candidate_plan_sha256="candidate-one",
        partition_ids=("partition-one",),
        child_summaries=("First verified generation.",),
        current_disposition="continue",
        resolved_target_step="implement",
        resolved_target_role="worker",
        current_partition_id="partition-one",
        scope_pressure_reason=None,
        envelope_artifact_path="manager/envelope-one.json",
        proposal_artifact_path="manager/proposal-one.json",
        candidate_artifact_path="manager/candidate-one.json",
        mechanical_validation_artifact_path="manager/mechanical-one.json",
        semantic_verdict_artifact_path="manager/semantic-one.json",
    ))
    metadata = {
        "run_id": "run-current",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
        "active_implementation_scope": asdict(first_scope),
        "implementation_attempts": attempts,
    }
    _write_json(
        run_dir / "manager" / "decision-001" / "boundary.json",
        {"boundary": {"repartition_history": [first_repartition]}},
    )
    (run_dir / "events.jsonl").write_text(
        "\n".join(
            json.dumps({
                "event_id": f"ambiguous-{index}",
                "scope_id": "ambiguous-scope",
                "checkpoint_index": index,
                "kind": "history",
            })
            for index in (1, 2)
        ) + "\n",
        encoding="utf-8",
    )
    before = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    first_row = before.checkpoints[0]
    first_event = next(event for event in before.events if event.turn_number == 1)
    assert first_row.checkpoint_id == before.current_checkpoint_id
    assert first_row.generation_id == "generation-one"
    assert first_event.checkpoint_id == first_row.checkpoint_id
    assert [(change.status, change.kind, change.generation_id) for change in before.applied_changes] == [
        ("applied", "repartition", "generation-one")
    ]
    assert before.pending_changes == ()
    assert all(
        event.checkpoint_id is None
        for event in before.events
        if event.event_id in {"ambiguous-1", "ambiguous-2"}
    )
    assert all(
        event.association == "unassigned"
        for event in before.events
        if event.event_id in {"ambiguous-1", "ambiguous-2"}
    )

    second_scope = ActiveImplementationScope(
        scope_id="original::checkpoint-2",
        original_plan_path=str(original),
        checkpoint_index=2,
        checkpoint_name="Checkpoint 2: Stage 2",
        opened_turn_number=2,
    )
    second_repartition = asdict(CheckpointRepartitionRecord(
        schema_version=1,
        decision_number=2,
        scope_id=first_scope.scope_id,
        generation_id="generation-two",
        envelope_sha256="envelope-two",
        envelope_artifact_sha256="artifact-two",
        source_plan_sha256="source-two",
        proposal_sha256="proposal-two",
        candidate_plan_sha256="candidate-two",
        partition_ids=("partition-two",),
        child_summaries=("Second verified generation.",),
        current_disposition="continue",
        resolved_target_step="implement",
        resolved_target_role="worker",
        current_partition_id="partition-two",
        scope_pressure_reason=None,
        envelope_artifact_path="manager/envelope-two.json",
        proposal_artifact_path="manager/proposal-two.json",
        candidate_artifact_path="manager/candidate-two.json",
        mechanical_validation_artifact_path="manager/mechanical-two.json",
        semantic_verdict_artifact_path="manager/semantic-two.json",
    ))
    _write_json(
        run_dir / "manager" / "decision-002" / "boundary.json",
        {"boundary": {"repartition_history": [first_repartition, second_repartition]}},
    )
    after = project_run_progress_detail(
        run_dir,
        metadata={
            **metadata,
            "active_implementation_scope": asdict(second_scope),
        },
        use_cache=False,
    )
    retained_row = after.checkpoints[0]
    retained_event = next(event for event in after.events if event.turn_number == 1)
    assert retained_row.checkpoint_id == first_row.checkpoint_id
    assert retained_row.generation_id == "generation-one"
    assert retained_event.checkpoint_id == retained_row.checkpoint_id
    assert after.checkpoints[1].generation_id is None
    assert [(change.checkpoint_id, change.generation_id) for change in after.applied_changes] == [
        (retained_row.checkpoint_id, "generation-one"),
        (retained_row.checkpoint_id, "generation-two"),
    ]
    assert after.applied_upgrades.value == 0
    serialized_changes = after.to_dict()["applied_changes"]
    assert [change["generation_id"] for change in serialized_changes] == [
        "generation-one",
        "generation-two",
    ]
    applied_events = [event for event in after.events if event.kind == "applied_change"]
    assert len(applied_events) == 2
    assert {event.source_reference["decision_number"]: event.checkpoint_id for event in applied_events} == {
        1: retained_row.checkpoint_id,
        2: retained_row.checkpoint_id,
    }
    serialized_events = [
        event for event in after.to_dict()["events"] if event["kind"] == "applied_change"
    ]
    assert {
        event["source_reference"]["decision_number"]: event["checkpoint_id"]
        for event in serialized_events
    } == {
        1: retained_row.checkpoint_id,
        2: retained_row.checkpoint_id,
    }
    assert after.pending_changes == ()


def test_canonical_progress_retains_distinct_same_turn_applied_changes(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_plan(total=2, current=1), encoding="utf-8")
    first_change = {
        "change_id": "same-turn-checkpoint-one",
        "status": "applied",
        "kind": "worker_upgrade",
        "source_run_id": "run-source",
        "checkpoint_index": 1,
        "turn_number": 7,
        "decision_number": 101,
        "old_selector": "codex.worker",
        "new_selector": "codex.repair",
    }
    second_change = {
        "change_id": "same-turn-checkpoint-two",
        "status": "applied",
        "kind": "configuration_change",
        "source_run_id": "run-source",
        "checkpoint_index": 2,
        "turn_number": 7,
        "decision_number": 102,
        "old_team": "base-team",
        "new_team": "repair-team",
    }
    metadata = {
        "run_id": "run-current",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
        "applied_changes": [first_change, first_change, second_change],
    }

    before = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    refreshed = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)

    assert len(before.applied_changes) == 2
    assert before.applied_upgrades.value == 1
    assert before.total_checkpoints == refreshed.total_checkpoints
    assert before.applied_upgrades == refreshed.applied_upgrades
    before_events = [
        event for event in before.events if event.kind in {"applied_change", "applied_upgrade"}
    ]
    refreshed_events = [
        event
        for event in refreshed.events
        if event.kind in {"applied_change", "applied_upgrade"}
    ]
    assert len(before_events) == 2
    assert len({event.event_id for event in before_events}) == 2
    assert {
        event.source_reference["decision_number"]: event.checkpoint_id
        for event in before_events
    } == {
        101: before.checkpoints[0].checkpoint_id,
        102: before.checkpoints[1].checkpoint_id,
    }
    assert {
        event.source_reference["decision_number"]: event.event_id
        for event in before_events
    } == {
        event.source_reference["decision_number"]: event.event_id
        for event in refreshed_events
    }


def test_canonical_progress_preserves_recorded_hotplug_model_identity(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_plan(total=2, current=2), encoding="utf-8")
    applied = _hotplug_transaction(
        run_id="recorded-hotplug-run", number=1, stage="applied"
    ).to_dict()
    pending = _hotplug_transaction(
        run_id="recorded-hotplug-run", number=2, stage="accepted"
    ).to_dict()
    missing_model = _hotplug_transaction(
        run_id="recorded-hotplug-run", number=3, stage="applied"
    ).to_dict()
    missing_model.update({"source_selector": "codex.missing", "target_selector": "codex.missing-target"})
    missing_model.pop("source_model_display")
    missing_model.pop("target_model_display")
    metadata = {
        "run_id": "current-run",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
        "hotplug_history": [applied, applied, missing_model],
        "current_hotplug_transaction": pending,
        "pending_hotplug_transaction": pending,
    }

    detail = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    serialized = detail.to_dict()

    recorded = next(
        change for change in detail.applied_changes
        if change.old_selector == "codex.source-profile"
    )
    assert len(detail.applied_changes) == 2
    assert recorded.status == "applied"
    assert recorded.kind == "hotplug"
    assert recorded.old_selector == "codex.source-profile"
    assert recorded.new_selector == "codex.target-profile"
    assert recorded.old_model == "Recorded source model"
    assert recorded.new_model == "Recorded target model"
    assert recorded.turn_number is None
    assert detail.applied_upgrades.value == 0
    assert len(detail.pending_changes) == 1
    assert detail.pending_changes[0].status == "pending"
    assert detail.pending_changes[0].kind == "hotplug"
    assert detail.pending_changes[0].old_model == "Recorded source model"
    assert detail.pending_changes[0].new_model == "Recorded target model"
    unknown = next(
        change for change in detail.applied_changes if change.old_selector == "codex.missing"
    )
    assert unknown.old_model is None
    assert unknown.new_model is None
    applied_events = [event for event in detail.events if event.kind == "applied_change"]
    assert {event.source_run_id for event in applied_events} == {"recorded-hotplug-run"}
    assert any(
        change["kind"] == "hotplug"
        and change["old_model"] == "Recorded source model"
        and change["new_model"] == "Recorded target model"
        for change in serialized["applied_changes"]
    )


def test_canonical_progress_approves_proven_successful_review_transitions(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_plan(total=2, current=2), encoding="utf-8")
    scope = ActiveImplementationScope(
        scope_id="original::checkpoint-1",
        original_plan_path=str(original),
        checkpoint_index=1,
        checkpoint_name="Checkpoint 1: Stage 1",
        opened_turn_number=1,
        awaiting_review=True,
    )
    worker = asdict(ImplementationAttempt(
        turn_number=1,
        step_name="implement",
        role="worker",
        team="base-team",
        selector="codex.worker",
        outcome="completed",
    ))
    metadata = {
        "run_id": "run-current",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "activity": "inactive",
        "active_implementation_scope": asdict(scope),
        "implementation_attempts": {scope.scope_id: [worker]},
    }
    awaiting = project_run_progress_detail(
        run_dir, metadata={**metadata, "history_complete": True}, use_cache=False
    )
    assert awaiting.approved_checkpoints == RunProgressCount(value=0, coverage="complete")
    _write_json(
        run_dir / "manager" / "decision-001" / "boundary.json",
        {"boundary": asdict(FinalizedTurnBoundary(
            finalized_turn_number=1,
            artifact_path="turns/turn-001",
            trigger="worker_completed",
            terminal=False,
            proposed_action="transition",
            proposed_transition="review",
            current_step="implement",
            current_role="worker",
            baseline_team="base-team",
            actual_team="base-team",
            actual_selector="codex.worker",
            original_plan_path=str(original),
            active_plan_path=str(original),
            checkpoint_identity=None,
            active_implementation_scope=asdict(scope),
            implementation_attempts={scope.scope_id: [worker]},
        ))},
    )
    _write_successful_production_review(
        run_dir,
        2,
        selector="codex.reviewer",
        original_plan_path=original,
        done=False,
        transition="implement",
    )
    advanced_scope = ActiveImplementationScope(
        scope_id="original::checkpoint-2",
        original_plan_path=str(original),
        checkpoint_index=2,
        checkpoint_name="Checkpoint 2: Stage 2",
        opened_turn_number=3,
        awaiting_review=True,
    )
    partial = project_run_progress_detail(
        run_dir,
        metadata={**metadata, "active_implementation_scope": asdict(advanced_scope)},
        use_cache=False,
    )
    assert partial.approved_checkpoints == RunProgressCount(value=1, coverage="partial")
    complete = project_run_progress_detail(
        run_dir,
        metadata={
            **metadata,
            "history_complete": True,
            "active_implementation_scope": asdict(advanced_scope),
        },
        use_cache=False,
    )
    summary = project_run_progress_summary(
        run_dir,
        metadata={
            **metadata,
            "history_complete": True,
            "active_implementation_scope": asdict(advanced_scope),
        },
        use_cache=False,
    )
    approvals = [event for event in complete.events if event.kind == "checkpoint_approval"]
    assert complete.approved_checkpoints == RunProgressCount(value=1, coverage="complete")
    assert complete.checkpoint_states == {"1": "approved", "2": "pending"}
    assert complete.checkpoints[0].status == "approved"
    assert complete.current_checkpoint_ordinal == 2
    assert len(approvals) == 1
    assert approvals[0].checkpoint_id == complete.checkpoints[0].checkpoint_id
    assert approvals[0].association == "checkpoint"
    assert approvals[0].turn_number == 2
    assert summary.approved_checkpoints == complete.approved_checkpoints
    assert complete.to_dict()["approved_checkpoints"] == {"value": 1, "coverage": "complete"}
    refreshed = project_run_progress_detail(
        run_dir,
        metadata={
            **metadata,
            "history_complete": True,
            "active_implementation_scope": asdict(advanced_scope),
        },
        use_cache=False,
    )
    assert [event.event_id for event in refreshed.events if event.kind == "checkpoint_approval"] == [
        approvals[0].event_id
    ]

    final_dir, final_original, _ = _run(tmp_path / "final-success")
    final_original.write_text(_plan(total=1, current=None), encoding="utf-8")
    final_scope = ActiveImplementationScope(
        scope_id="original::checkpoint-1",
        original_plan_path=str(final_original),
        checkpoint_index=1,
        checkpoint_name="Checkpoint 1: Stage 1",
        opened_turn_number=1,
        awaiting_review=True,
    )
    _write_json(
        final_dir / "manager" / "decision-001" / "boundary.json",
        {"boundary": asdict(FinalizedTurnBoundary(
            finalized_turn_number=1,
            artifact_path="turns/turn-001",
            trigger="worker_completed",
            terminal=False,
            proposed_action="transition",
            proposed_transition="review",
            current_step="implement",
            current_role="worker",
            baseline_team="base-team",
            actual_team="base-team",
            actual_selector="codex.worker",
            original_plan_path=str(final_original),
            active_plan_path=str(final_original),
            checkpoint_identity=None,
            active_implementation_scope=asdict(final_scope),
            implementation_attempts={final_scope.scope_id: [worker]},
        ))},
    )
    _write_successful_production_review(
        final_dir,
        2,
        selector="codex.final-reviewer",
        original_plan_path=final_original,
        done=True,
        transition="END",
    )
    final = project_run_progress_detail(
        final_dir,
        metadata={
            "run_id": "final-success",
            "repo_root": str(final_original.parents[2]),
            "original_plan_path": str(final_original),
            "history_complete": True,
            "implementation_attempts": {final_scope.scope_id: [worker]},
        },
        use_cache=False,
    )
    assert final.approved_checkpoints == RunProgressCount(value=1, coverage="complete")
    assert final.checkpoints[0].status == "approved"

    result_path = run_dir / "turns" / "turn-002" / "result.json"
    successful_result = json.loads(result_path.read_text(encoding="utf-8"))
    for update in (
        {"review_rejection": {"reason": "repair required"}},
        {"conditions": {"DONE": False, "NEW_PLAN_EXISTS": False, "MAX_TURNS_REACHED": True}, "chosen_transition": "END"},
        {"returncode": 1, "status": "failed"},
        {"status": "stopped"},
    ):
        _write_json(result_path, {**successful_result, **update})
        negative = project_run_progress_detail(
            run_dir,
            metadata={
                **metadata,
                "history_complete": True,
                "active_implementation_scope": asdict(advanced_scope),
            },
            use_cache=False,
        )
        assert negative.approved_checkpoints == RunProgressCount(value=0, coverage="complete")
        assert not any(event.kind == "checkpoint_approval" for event in negative.events)
    _write_json(result_path, successful_result)

    unresolved_dir, unresolved_original, _ = _run(tmp_path / "unresolved-success")
    unresolved_original.write_text(_plan(total=1, current=None), encoding="utf-8")
    _write_successful_production_review(
        unresolved_dir,
        2,
        selector="codex.unresolved-reviewer",
        original_plan_path=unresolved_original,
        done=True,
        transition="END",
    )
    unresolved = project_run_progress_detail(
        unresolved_dir,
        metadata={
            "run_id": "unresolved-success",
            "repo_root": str(unresolved_original.parents[2]),
            "original_plan_path": str(unresolved_original),
            "history_complete": True,
        },
        use_cache=False,
    )
    assert unresolved.approved_checkpoints == RunProgressCount(value=0, coverage="complete")


def test_canonical_progress_distinguishes_awaiting_review(tmp_path: Path) -> None:
    run_dir, original, _ = _run(tmp_path)
    original.write_text(_plan(total=2, current=2), encoding="utf-8")
    scope = ActiveImplementationScope(
        scope_id="original::checkpoint-1",
        original_plan_path=str(original),
        checkpoint_index=1,
        checkpoint_name="Checkpoint 1: Stage 1",
        opened_turn_number=1,
        awaiting_review=True,
    )
    worker = asdict(ImplementationAttempt(
        turn_number=1,
        step_name="implement",
        role="worker",
        team="base-team",
        selector="codex.worker",
        outcome="completed",
    ))
    metadata = {
        "run_id": "run-current",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
        "active_implementation_scope": asdict(scope),
        "implementation_attempts": {scope.scope_id: [worker]},
        "activity": "inactive",
    }
    awaiting = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    assert awaiting.checkpoints[0].status == "reviewing"
    assert awaiting.checkpoints[0].awaiting_review is True
    assert awaiting.current_executor is None

    active_worker = project_run_progress_detail(
        run_dir,
        metadata={
            **metadata,
            "activity": "active",
            "implementation_attempts": {
                scope.scope_id: [{**worker, "status": "running", "outcome": "running"}]
            },
        },
        use_cache=False,
    )
    assert active_worker.checkpoints[0].status == "implementing"
    assert active_worker.checkpoints[0].awaiting_review is False

    _write_production_turn(
        run_dir,
        2,
        role="reviewer",
        status="running",
        selector="codex.reviewer",
        finished_at=None,
        original_plan_path=original,
        snapshot_before=PlanSnapshot(
            current_checkpoint_name="Checkpoint 2: Stage 2",
            unchecked_checkpoint_count=1,
            current_checkpoint_unchecked_step_count=1,
            is_complete=False,
            total_checkpoint_count=2,
            current_checkpoint_index=2,
        ),
    )
    active_reviewer = project_run_progress_detail(
        run_dir,
        metadata={**metadata, "activity": "active"},
        use_cache=False,
    )
    assert active_reviewer.checkpoints[0].status == "reviewing"
    assert active_reviewer.checkpoints[0].awaiting_review is False
    assert active_reviewer.checkpoints[0].reviews.value == 1
    reviewer_event = next(event for event in active_reviewer.events if event.kind == "review")
    assert reviewer_event.checkpoint_id == active_reviewer.checkpoints[0].checkpoint_id
    assert reviewer_event.scope_id == scope.scope_id
    assert active_reviewer.current_executor is not None
    assert active_reviewer.current_executor.selector == "codex.reviewer"

    advanced_scope = ActiveImplementationScope(
        scope_id="original::checkpoint-2",
        original_plan_path=str(original),
        checkpoint_index=2,
        checkpoint_name="Checkpoint 2: Stage 2",
        opened_turn_number=3,
        awaiting_review=True,
    )
    _write_json(
        run_dir / "manager" / "decision-001" / "boundary.json",
        {"boundary": asdict(FinalizedTurnBoundary(
            finalized_turn_number=1,
            artifact_path="turns/turn-001",
            trigger="worker_completed",
            terminal=False,
            proposed_action="transition",
            proposed_transition="review",
            current_step="implement",
            current_role="worker",
            baseline_team="base-team",
            actual_team="base-team",
            actual_selector="codex.worker",
            original_plan_path=str(original),
            active_plan_path=str(original),
            checkpoint_identity=None,
            active_implementation_scope=asdict(scope),
            implementation_attempts={scope.scope_id: [worker]},
        ))},
    )
    _write_production_turn(
        run_dir,
        2,
        role="reviewer",
        status="completed",
        selector="codex.reviewer",
        original_plan_path=original,
        snapshot_before=PlanSnapshot(
            current_checkpoint_name="Checkpoint 2: Stage 2",
            unchecked_checkpoint_count=1,
            current_checkpoint_unchecked_step_count=1,
            is_complete=False,
            total_checkpoint_count=2,
            current_checkpoint_index=2,
        ),
    )
    _write_json(
        run_dir / "manager" / "decision-002" / "boundary.json",
        {"boundary": asdict(FinalizedTurnBoundary(
            finalized_turn_number=2,
            artifact_path="turns/turn-002",
            trigger="review_completed",
            terminal=False,
            proposed_action="transition",
            proposed_transition="implement",
            current_step="review",
            current_role="reviewer",
            baseline_team="base-team",
            actual_team="base-team",
            actual_selector="codex.reviewer",
            original_plan_path=str(original),
            active_plan_path=str(original),
            checkpoint_identity=None,
            active_implementation_scope=None,
        ))},
    )
    historical = project_run_progress_detail(
        run_dir,
        metadata={
            **metadata,
            "activity": "inactive",
            "active_implementation_scope": asdict(advanced_scope),
        },
        use_cache=False,
    )
    assert historical.checkpoints[0].reviews.value == 1
    assert historical.checkpoints[1].reviews.value == 0
    historical_review = next(event for event in historical.events if event.kind == "review")
    assert historical_review.checkpoint_id == historical.checkpoints[0].checkpoint_id
    assert historical_review.association == "checkpoint"
    assert historical_review.scope_id == scope.scope_id
    assert historical_review.executor is not None
    assert historical_review.executor.selector == "codex.reviewer"
    assert historical.current_executor is None

    final_dir, final_original, _ = _run(tmp_path / "final-review")
    final_original.write_text(_plan(total=1, current=None), encoding="utf-8")
    final_scope = ActiveImplementationScope(
        scope_id="original::checkpoint-1",
        original_plan_path=str(final_original),
        checkpoint_index=1,
        checkpoint_name="Checkpoint 1: Stage 1",
        opened_turn_number=1,
        awaiting_review=True,
    )
    _write_production_turn(
        final_dir,
        2,
        role="reviewer",
        status="running",
        selector="codex.final-reviewer",
        finished_at=None,
        original_plan_path=final_original,
        snapshot_before=PlanSnapshot(
            current_checkpoint_name=None,
            unchecked_checkpoint_count=0,
            current_checkpoint_unchecked_step_count=0,
            is_complete=True,
            total_checkpoint_count=1,
            current_checkpoint_index=None,
        ),
    )
    final_detail = project_run_progress_detail(
        final_dir,
        metadata={
            "run_id": "final-review",
            "repo_root": str(final_original.parents[2]),
            "original_plan_path": str(final_original),
            "history_complete": True,
            "activity": "active",
            "active_implementation_scope": asdict(final_scope),
            "implementation_attempts": {final_scope.scope_id: [worker]},
        },
        use_cache=False,
    )
    assert final_detail.checkpoints[0].reviews.value == 1
    assert final_detail.checkpoints[0].awaiting_review is False
    assert final_detail.current_executor is not None
    assert final_detail.current_executor.selector == "codex.final-reviewer"

    _write_json(
        final_dir / "manager" / "decision-001" / "boundary.json",
        {"boundary": asdict(FinalizedTurnBoundary(
            finalized_turn_number=1,
            artifact_path="turns/turn-001",
            trigger="worker_completed",
            terminal=False,
            proposed_action="transition",
            proposed_transition="review",
            current_step="implement",
            current_role="worker",
            baseline_team="base-team",
            actual_team="base-team",
            actual_selector="codex.worker",
            original_plan_path=str(final_original),
            active_plan_path=str(final_original),
            checkpoint_identity=None,
            active_implementation_scope=asdict(final_scope),
            implementation_attempts={final_scope.scope_id: [worker]},
        ))},
    )
    _write_production_turn(
        final_dir,
        2,
        role="reviewer",
        status="completed",
        selector="codex.final-reviewer",
        original_plan_path=final_original,
        snapshot_before=PlanSnapshot(
            current_checkpoint_name=None,
            unchecked_checkpoint_count=0,
            current_checkpoint_unchecked_step_count=0,
            is_complete=True,
            total_checkpoint_count=1,
            current_checkpoint_index=None,
        ),
    )
    _write_json(
        final_dir / "manager" / "decision-002" / "boundary.json",
        {"boundary": asdict(FinalizedTurnBoundary(
            finalized_turn_number=2,
            artifact_path="turns/turn-002",
            trigger="review_completed",
            terminal=False,
            proposed_action="transition",
            proposed_transition="END",
            current_step="review",
            current_role="reviewer",
            baseline_team="base-team",
            actual_team="base-team",
            actual_selector="codex.final-reviewer",
            original_plan_path=str(final_original),
            active_plan_path=str(final_original),
            checkpoint_identity=None,
            active_implementation_scope=None,
        ))},
    )
    final_historical = project_run_progress_detail(
        final_dir,
        metadata={
            "run_id": "final-review",
            "repo_root": str(final_original.parents[2]),
            "original_plan_path": str(final_original),
            "history_complete": True,
            "activity": "inactive",
            "active_implementation_scope": None,
            "implementation_attempts": {final_scope.scope_id: [worker]},
        },
        use_cache=False,
    )
    assert final_historical.checkpoints[0].reviews.value == 1
    assert final_historical.current_executor is None

    conflict_dir, conflict_original, _ = _run(tmp_path / "conflicting-review")
    conflict_original.write_text(_plan(total=2, current=2), encoding="utf-8")
    conflict_scope = ActiveImplementationScope(
        scope_id="original::checkpoint-1",
        original_plan_path=str(conflict_original),
        checkpoint_index=1,
        checkpoint_name="Checkpoint 1: Stage 1",
        opened_turn_number=1,
        awaiting_review=True,
    )
    _write_production_turn(
        conflict_dir,
        1,
        selector="codex.worker",
        original_plan_path=conflict_original,
        snapshot_before=PlanSnapshot(
            current_checkpoint_name="Checkpoint 1: Stage 1",
            unchecked_checkpoint_count=2,
            current_checkpoint_unchecked_step_count=1,
            is_complete=False,
            total_checkpoint_count=2,
            current_checkpoint_index=1,
        ),
    )
    _write_json(
        conflict_dir / "manager" / "decision-001" / "boundary.json",
        {"boundary": asdict(FinalizedTurnBoundary(
            finalized_turn_number=1,
            artifact_path="turns/turn-001",
            trigger="worker_completed",
            terminal=False,
            proposed_action="transition",
            proposed_transition="review",
            current_step="implement",
            current_role="worker",
            baseline_team="base-team",
            actual_team="base-team",
            actual_selector="codex.worker",
            original_plan_path=str(conflict_original),
            active_plan_path=str(conflict_original),
            checkpoint_identity=None,
            active_implementation_scope=asdict(conflict_scope),
            implementation_attempts={conflict_scope.scope_id: [worker]},
        ))},
    )
    _write_json(
        conflict_dir / "turns" / "turn-002" / "result.json",
        {
            "turn_number": 2,
            "step_name": "review",
            "step_role": "reviewer",
            "status": "completed",
            "finished_at": "2026-09-11T00:00:02+00:00",
            "scope_id": "original::checkpoint-2",
            "original_plan_path": str(conflict_original),
        },
    )
    conflicting_closed = project_run_progress_detail(
        conflict_dir,
        metadata={
            "run_id": "run-1",
            "repo_root": str(conflict_original.parents[2]),
            "original_plan_path": str(conflict_original),
            "history_complete": True,
            "activity": "inactive",
            "active_implementation_scope": None,
            "implementation_attempts": {conflict_scope.scope_id: [worker]},
        },
        use_cache=False,
    )
    conflicting_closed_event = next(
        event for event in conflicting_closed.events if event.turn_number == 2
    )
    assert conflicting_closed_event.checkpoint_id is None
    assert conflicting_closed_event.association == "unassigned"

    _write_production_turn(
        conflict_dir,
        3,
        role="reviewer",
        status="completed",
        selector="codex.missing-reviewer",
        original_plan_path=conflict_original,
    )
    missing_closed = project_run_progress_detail(
        conflict_dir,
        metadata={
            "run_id": "run-1",
            "repo_root": str(conflict_original.parents[2]),
            "original_plan_path": str(conflict_original),
            "history_complete": True,
            "activity": "inactive",
            "active_implementation_scope": None,
            "implementation_attempts": {conflict_scope.scope_id: [worker]},
        },
        use_cache=False,
    )
    assert next(
        event for event in missing_closed.events if event.turn_number == 3
    ).checkpoint_id is None

    _write_json(
        run_dir / "turns" / "turn-003" / "result.json",
        {
            "turn_number": 3,
            "step_name": "review",
            "step_role": "reviewer",
            "status": "running",
            "scope_id": "original::checkpoint-2",
            "original_plan_path": str(original),
            "snapshot_before": {
                "current_checkpoint_name": "Checkpoint 1: Stage 1",
                "current_checkpoint_index": 1,
            },
        },
    )
    conflicting = project_run_progress_detail(
        run_dir,
        metadata={
            **metadata,
            "activity": "active",
            "active_implementation_scope": asdict(advanced_scope),
        },
        use_cache=False,
    )
    conflict_event = next(event for event in conflicting.events if event.turn_number == 3)
    assert conflict_event.checkpoint_id is None
    assert conflict_event.association == "unassigned"

    _write_production_turn(
        run_dir,
        4,
        role="reviewer",
        status="running",
        selector="codex.unproven-reviewer",
        finished_at=None,
    )
    unproven = project_run_progress_detail(
        run_dir,
        metadata={
            **metadata,
            "activity": "active",
            "active_implementation_scope": asdict(advanced_scope),
        },
        use_cache=False,
    )
    unproven_event = next(event for event in unproven.events if event.turn_number == 4)
    assert unproven_event.checkpoint_id is None
    assert unproven_event.association == "unassigned"


def test_canonical_progress_separates_whole_plan_unknown_and_outside_history(
    tmp_path: Path,
) -> None:
    run_dir, original, _ = _run(tmp_path / "whole-plan")
    original.write_text(_plan(total=2, current=None), encoding="utf-8")
    worker_snapshot = PlanSnapshot(
        current_checkpoint_name="Checkpoint 1: Stage 1",
        unchecked_checkpoint_count=1,
        current_checkpoint_unchecked_step_count=1,
        is_complete=False,
        total_checkpoint_count=2,
        current_checkpoint_index=1,
    )
    complete_snapshot = PlanSnapshot(
        current_checkpoint_name=None,
        unchecked_checkpoint_count=0,
        current_checkpoint_unchecked_step_count=0,
        is_complete=True,
        total_checkpoint_count=2,
        current_checkpoint_index=None,
    )
    _write_production_turn(
        run_dir,
        1,
        selector="codex.worker",
        original_plan_path=original,
        snapshot_before=worker_snapshot,
    )
    _write_production_turn(
        run_dir,
        2,
        role="reviewer",
        selector="codex.whole-plan-reviewer",
        original_plan_path=original,
        snapshot_before=complete_snapshot,
    )
    (run_dir / "events.jsonl").write_text(
        json.dumps({
            "event_id": "unknown-history",
            "kind": "history",
            "turn_number": 3,
            "reason": "association was not recorded",
        })
        + "\n",
        encoding="utf-8",
    )
    metadata = {
        "run_id": "run-1",
        "repo_root": str(original.parents[2]),
        "original_plan_path": str(original),
        "history_complete": True,
    }

    detail = project_run_progress_detail(run_dir, metadata=metadata, use_cache=False)
    whole_plan_review = next(event for event in detail.events if event.turn_number == 2)
    unknown_history = next(event for event in detail.events if event.event_id == "unknown-history")
    assert whole_plan_review.association == "whole_plan"
    assert whole_plan_review.checkpoint_id is None
    assert unknown_history.association == "unassigned"
    assert detail.checkpoints[0].reviews == RunProgressCount(value=0, coverage="complete")
    assert detail.reviews == RunProgressCount(value=1, coverage="complete")
    assert detail.approved_checkpoints == RunProgressCount(value=0, coverage="complete")
    assert "whole_plan_review" in detail.reason_codes
    assert "unassigned_history" in detail.reason_codes

    outside_dir, outside_original, _ = _run(tmp_path / "outside-returned")
    outside_original.write_text(_plan(total=501, current=None), encoding="utf-8")
    (outside_dir / "events.jsonl").write_text(
        json.dumps({
            "event_id": "outside-review",
            "kind": "review",
            "role": "reviewer",
            "turn_number": 2,
            "checkpoint_index": 501,
            "checkpoint_name": "Checkpoint 501: Stage 501",
            "scope_id": "original::checkpoint-501",
            "original_plan_path": str(outside_original),
        })
        + "\n",
        encoding="utf-8",
    )
    outside = project_run_progress_detail(
        outside_dir,
        metadata={
            "run_id": "run-1",
            "repo_root": str(outside_original.parents[2]),
            "original_plan_path": str(outside_original),
            "history_complete": True,
        },
        use_cache=False,
    )
    outside_event = next(event for event in outside.events if event.event_id == "outside-review")
    assert outside_event.association == "outside_returned"
    assert outside_event.checkpoint_id is None
    assert outside.truncation.omitted_checkpoints == 1
    assert outside.truncation.response_limit_checkpoints == 1
    assert outside.checkpoints[-1].reviews == RunProgressCount(value=0, coverage="partial")
    assert outside.approved_checkpoints == RunProgressCount(value=None, coverage="unavailable")
    assert "history_outside_returned" in outside.reason_codes
    assert "unassigned_history" not in outside.reason_codes

    ambiguous_dir, ambiguous_original, _ = _run(tmp_path / "ambiguous-approval")
    ambiguous_original.write_text(_plan(total=2, current=None), encoding="utf-8")
    (ambiguous_dir / "events.jsonl").write_text(
        json.dumps({
            "event_id": "ambiguous-scope-history",
            "kind": "history",
            "scope_id": "ambiguous-scope",
            "checkpoint_index": 2,
        })
        + "\n",
        encoding="utf-8",
    )
    ambiguous = project_run_progress_detail(
        ambiguous_dir,
        metadata={
            "run_id": "run-1",
            "repo_root": str(ambiguous_original.parents[2]),
            "original_plan_path": str(ambiguous_original),
            "history_complete": True,
            "approved_checkpoints": [{
                "approved": True,
                "scope_id": "ambiguous-scope",
                "checkpoint_index": 1,
            }],
        },
        use_cache=False,
    )
    assert ambiguous.approved_checkpoints == RunProgressCount(value=0, coverage="complete")
    assert all(checkpoint.status != "approved" for checkpoint in ambiguous.checkpoints)
    ambiguous_approval = next(
        event for event in ambiguous.events if event.kind == "checkpoint_approval"
    )
    assert ambiguous_approval.association == "unassigned"
