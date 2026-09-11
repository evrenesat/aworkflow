"""Real browser parity journeys for truthful run progress."""

from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import re

import pytest
from playwright.sync_api import expect, sync_playwright

from test_control_plane_api import (
    PROJECT_ID,
    _close_issue35_repair_scope,
    _seed_issue35_progress_fixture,
    live_server,
)
from test_control_plane_api import control_client  # noqa: F401
from test_responsive_browser import _browser, _login
from test_responsive_browser import (
    _assert_document_moves,
    _assert_header_and_flow,
    _assert_last_action_hit_test,
    _assert_no_horizontal_overflow,
    _compact,
    _clear_test_text_zoom,
    _double_visible_text,
    _set_theme_preference,
)
from aflow.run_state import CheckpointRepartitionRecord, FinalizedTurnBoundary
from aflow.hotplug import HotplugTransactionV1, hotplug_transaction_id


VISUAL_PROGRESS_RUN_ID = "visual-progress-history"
VISUAL_PROGRESS_TERMINAL_ID = "visual-progress-terminal"
VISUAL_PROGRESS_STALE_ID = "visual-progress-stale"
VISUAL_PROGRESS_UNKNOWN_ID = "visual-progress-unknown"
REVIEW_EVIDENCE_RUN_ID = "visual-review-evidence"

VISUAL_CHECKPOINT_NAMES = {
    1: "Checkpoint 1: Establish the bounded evidence contract",
    2: "Checkpoint 2: Preserve exact executor identity",
    3: "Checkpoint 3: Explain approved and recorded completion",
    4: "Checkpoint 4: Retain earlier history during refresh",
    5: "Checkpoint 5: Inspect the long-lived executor and delivery history without losing context",
    6: "Checkpoint 6: Keep pending work visible while the run advances",
    7: "Checkpoint 7: Surface bounded evidence when history is incomplete",
    8: "Checkpoint 8: Keep delivery stages separate from approval",
    9: "Checkpoint 9: Preserve run controls beside historical evidence",
    10: "Checkpoint 10: Report unavailable optional artifacts honestly",
    11: "Checkpoint 11: Finish the documented delivery boundary",
}


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _visual_hotplug_transaction(*, stage: str) -> dict[str, object]:
    digest = "a" * 64
    return HotplugTransactionV1(
        transaction_id=hotplug_transaction_id(REVIEW_EVIDENCE_RUN_ID, digest, 1),
        run_id=REVIEW_EVIDENCE_RUN_ID,
        accepted_override_digest=digest,
        transaction_number=1,
        source_role="worker",
        target_role="worker",
        source_selector="codex.recorded-source",
        target_selector="codex.recorded-target",
        source_harness="codex",
        target_harness="codex",
        source_profile="recorded-source",
        target_profile="recorded-target",
        source_model_display="Recorded source model",
        target_model_display="Recorded target model",
        stage=stage,  # type: ignore[arg-type]
    ).to_dict()


def _visual_plan(checked: set[int]) -> str:
    sections = []
    for index, name in VISUAL_CHECKPOINT_NAMES.items():
        mark = "x" if index in checked else " "
        sections.append(f"### [{mark}] {name}\n- [{mark}] record stage {index}\n")
    return "# Visual progress browser fixture\n\n" + "\n".join(sections)


def _visual_scope(index: int) -> dict[str, object]:
    return {
        "scope_id": f"original::checkpoint-{index}",
        "original_plan_path": "plans/in-progress/visual-progress-plan.md",
        "checkpoint_index": index,
        "checkpoint_name": VISUAL_CHECKPOINT_NAMES[index],
        "opened_turn_number": 5,
        "awaiting_review": False,
    }


def _visual_invocation(
    run_id: str,
    index: int,
    turn: int,
    *,
    selector: str,
    model_display: str,
    status: str,
    outcome: str,
    repair: bool = False,
    was_retry: bool = False,
) -> dict[str, object]:
    # Match asdict(ImplementationAttempt(...)); scope is supplied only by the
    # enclosing durable implementation_attempts mapping. The matching turn
    # result is written separately in the production runlog shape below.
    record: dict[str, object] = {
        "turn_number": turn,
        "step_name": "implement",
        "role": "worker",
        "team": "repair-team" if repair else "base-team",
        "selector": selector,
        "outcome": outcome,
        "manager_decision_number": None,
    }
    if was_retry:
        record["retry_attempt"] = 1
    return record


def _write_visual_turn(
    run_dir: Path,
    turn: int,
    *,
    selector: str,
    status: str,
    role: str = "worker",
    was_retry: bool = False,
    retry_attempt: int | None = None,
    retry_next_turn: bool = False,
    original_plan_path: Path | None = None,
    checkpoint_index: int | None = None,
) -> None:
    payload: dict[str, object] = {
        "turn_number": turn,
        "step_name": "implement" if role == "worker" else "review",
        "step_role": role,
        "selector": selector,
        "status": status,
        "started_at": f"2026-09-11T10:{turn:02d}:00Z",
    }
    if status not in {"running", "starting"}:
        payload["finished_at"] = f"2026-09-11T10:{turn:02d}:05Z"
    if was_retry:
        payload["was_retry"] = True
        payload["reason"] = "harness retry"
    if retry_attempt is not None or was_retry:
        payload["retry_attempt"] = retry_attempt or 1
    if retry_next_turn:
        payload["retry_next_turn"] = True
    if original_plan_path is not None and checkpoint_index is not None:
        payload.update(
            {
                "original_plan_path": str(original_plan_path),
                "snapshot_before": {
                    "current_checkpoint_name": VISUAL_CHECKPOINT_NAMES[checkpoint_index],
                    "current_checkpoint_index": checkpoint_index,
                    "unchecked_checkpoint_count": 12 - checkpoint_index,
                    "current_checkpoint_unchecked_step_count": 1,
                    "is_complete": False,
                    "total_checkpoint_count": 11,
                },
                "snapshot_after": {
                    "current_checkpoint_name": VISUAL_CHECKPOINT_NAMES.get(checkpoint_index + 1),
                    "current_checkpoint_index": checkpoint_index + 1 if checkpoint_index < 11 else None,
                    "unchecked_checkpoint_count": 11 - checkpoint_index,
                    "current_checkpoint_unchecked_step_count": 1 if checkpoint_index < 11 else 0,
                    "is_complete": checkpoint_index == 11,
                    "total_checkpoint_count": 11,
                },
            }
        )
    _write_json(run_dir / "turns" / f"turn-{turn:03d}" / "result.json", payload)


def _write_successful_visual_review(
    run_dir: Path, turn: int, *, original_plan_path: Path, checkpoint_index: int
) -> None:
    _write_visual_turn(
        run_dir,
        turn,
        selector="codex.reviewer-success",
        status="running",
        role="reviewer",
        original_plan_path=original_plan_path,
        checkpoint_index=checkpoint_index,
    )
    result_path = run_dir / "turns" / f"turn-{turn:03d}" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.update(
        {
            "returncode": 0,
            "finished_at": f"2026-09-11T10:{turn:02d}:05Z",
            "review_rejection": None,
            "conditions": {
                "DONE": False,
                "NEW_PLAN_EXISTS": False,
                "MAX_TURNS_REACHED": False,
            },
            "chosen_transition": "implement",
            "chosen_transition_condition": "NEW_PLAN_EXISTS || !DONE",
        }
    )
    _write_json(result_path, result)


def _write_visual_boundary(
    run_dir: Path,
    decision: int,
    turn: int,
    *,
    role: str,
    scope: dict[str, object] | None,
    selector: str,
    attempts: dict[str, object] | None = None,
) -> None:
    original_plan_path = scope.get("original_plan_path") if scope is not None else None
    _write_json(
        run_dir / "manager" / f"decision-{decision:03d}" / "boundary.json",
        {"boundary": asdict(FinalizedTurnBoundary(
            finalized_turn_number=turn,
            artifact_path=f"turns/turn-{turn:03d}",
            trigger=f"{role}_completed",
            terminal=False,
            proposed_action="transition",
            proposed_transition="review" if role == "worker" else "implement",
            current_step="implement" if role == "worker" else "review",
            current_role=role,
            baseline_team="base-team",
            actual_team="base-team",
            actual_selector=selector,
            original_plan_path=str(original_plan_path) if original_plan_path is not None else None,
            active_plan_path=str(original_plan_path) if original_plan_path is not None else None,
            checkpoint_identity=None,
            active_implementation_scope=scope,
            implementation_attempts=attempts or {},
        ))},
    )


def _verified_repartition(scope_id: str, generation_id: str, decision_number: int) -> dict[str, object]:
    return asdict(CheckpointRepartitionRecord(
        schema_version=1,
        decision_number=decision_number,
        scope_id=scope_id,
        generation_id=generation_id,
        envelope_sha256=f"envelope-{generation_id}",
        envelope_artifact_sha256=f"artifact-{generation_id}",
        source_plan_sha256=f"source-{generation_id}",
        proposal_sha256=f"proposal-{generation_id}",
        candidate_plan_sha256=f"candidate-{generation_id}",
        partition_ids=(f"partition-{generation_id}",),
        child_summaries=(f"Verified {generation_id}.",),
        current_disposition="continue",
        resolved_target_step="implement",
        resolved_target_role="worker",
        current_partition_id=f"partition-{generation_id}",
        scope_pressure_reason=None,
        envelope_artifact_path=f"manager/envelope-{generation_id}.json",
        proposal_artifact_path=f"manager/proposal-{generation_id}.json",
        candidate_artifact_path=f"manager/candidate-{generation_id}.json",
        mechanical_validation_artifact_path=f"manager/mechanical-{generation_id}.json",
        semantic_verdict_artifact_path=f"manager/semantic-{generation_id}.json",
    ))


def _seed_visual_progress_fixture(root: Path) -> dict[str, object]:
    original = root / "plans" / "in-progress" / "visual-progress-plan.md"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text(_visual_plan({1, 2, 3, 4}), encoding="utf-8")

    run_dir = root / ".aflow" / "runs" / VISUAL_PROGRESS_RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    scope = _visual_scope(5)
    attempts = {
        f"original::checkpoint-{index}": [
            _visual_invocation(
                VISUAL_PROGRESS_RUN_ID,
                index,
                index,
                selector="codex.worker",
                model_display="Worker model",
                status="completed",
                outcome="accepted",
            )
        ]
        for index in range(1, 5)
    }
    attempts["original::checkpoint-5"] = [
        _visual_invocation(
            VISUAL_PROGRESS_RUN_ID,
            5,
            5,
            selector="codex.worker",
            model_display="Worker model",
            status="completed",
            outcome="completed",
        ),
        _visual_invocation(
            VISUAL_PROGRESS_RUN_ID,
            5,
            7,
            selector="codex.repair",
            model_display="Repair model",
            status="running",
            outcome="repairing",
            repair=True,
        ),
        _visual_invocation(
            VISUAL_PROGRESS_RUN_ID,
            5,
            8,
            selector="codex.repair",
            model_display="Repair model",
            status="harness-retry",
            outcome="retrying",
        ),
    ]
    for turn, selector, status, was_retry in (
        (1, "codex.worker", "completed", False),
        (2, "codex.worker", "completed", False),
        (3, "codex.worker", "completed", False),
        (4, "codex.worker", "completed", False),
        (5, "codex.worker", "completed", False),
        (7, "codex.repair", "running", False),
        (8, "codex.repair", "harness-retry", True),
    ):
        _write_visual_turn(
            run_dir,
            turn,
            selector=selector,
            status=status,
            was_retry=was_retry,
            original_plan_path=original,
            checkpoint_index=5 if turn > 5 else turn,
        )
    metadata: dict[str, object] = {
        "run_id": VISUAL_PROGRESS_RUN_ID,
        "status": "running",
        "repo_root": str(root),
        "original_plan_path": str(original),
        "active_plan_path": str(original),
        "plan_path": str(original),
        "workflow_name": "managed",
        "team": "base-team",
        "current_step_name": "implement",
        "turns_completed": 7,
        "max_turns": 12,
        "run_started_at": "2026-09-11T09:00:00Z",
        "activity": "active",
        "phase": "implementing",
        "progress_history_complete": True,
        "active_implementation_scope": scope,
        "implementation_attempts": attempts,
        "approved_checkpoints": [
            {"checkpoint_index": index, "status": "approved", "decision_number": index}
            for index in range(1, 5)
        ],
        "review_rejection_history": [
            {
                "scope_id": "original::checkpoint-5",
                "source_run_id": VISUAL_PROGRESS_RUN_ID,
                "review_turn_number": 6,
                "review_step_name": "review",
                "reviewer_selector": "codex.reviewer",
                "model_display": "Reviewer model",
                "team": "review-team",
                "checkpoint_index": 5,
                "checkpoint_name": VISUAL_CHECKPOINT_NAMES[5],
                "review_summary": "Review rejected the first implementation; repair is required.",
                "outcome": "rejected",
            }
        ],
        "applied_changes": [
            {
                "change_id": "visual-applied-upgrade",
                "status": "applied",
                "kind": "team_upgrade",
                "roles": ["worker", "reviewer"],
                "old_team": "base-team",
                "new_team": "repair-team",
                "old_selector": "codex.worker",
                "new_selector": "codex.repair",
                "old_model": "Worker model",
                "new_model": "Repair model",
                "old_effort": "medium",
                "new_effort": "high",
                "turn_number": 7,
                "scope_id": "original::checkpoint-5",
                "checkpoint_index": 5,
                "checkpoint_name": VISUAL_CHECKPOINT_NAMES[5],
                "reason": "Explicit repair capacity upgrade",
                "recorded_at": "2026-09-11T10:07:05Z",
            }
        ],
        "pending_changes": [
            {
                "change_id": "visual-pending-change",
                "status": "pending",
                "kind": "team_change",
                "roles": ["reviewer"],
                "old_team": "base-team",
                "new_team": "next-team",
                "reason": "Applies on the next safe turn",
            }
        ],
    }
    _write_json(run_dir / "run.json", metadata)
    _write_json(
        run_dir / "publication.json",
        {
            "stages": {
                "Final review": {"status": "succeeded", "recorded_at": "2026-09-11T10:08:00Z"},
                "Merge": {"status": "not_applicable", "reason": "No merge receipt"},
                "Publish": {"status": "pending", "reason": "Publication is owner-controlled"},
                "CI": {"status": "unknown", "reason": "No CI receipt"},
                "Live verification": {"status": "unknown", "reason": "No live verification receipt"},
            }
        },
    )

    terminal_plan = root / "plans" / "done" / "visual-terminal-plan.md"
    terminal_plan.parent.mkdir(parents=True, exist_ok=True)
    terminal_plan.write_text(_visual_plan(set(VISUAL_CHECKPOINT_NAMES)), encoding="utf-8")
    terminal_dir = root / ".aflow" / "runs" / VISUAL_PROGRESS_TERMINAL_ID
    terminal_dir.mkdir(parents=True, exist_ok=True)
    terminal_attempts = {
        f"original::checkpoint-{index}": [
            _visual_invocation(
                VISUAL_PROGRESS_TERMINAL_ID,
                index,
                index,
                selector="codex.worker",
                model_display="Terminal worker",
                status="completed",
                outcome="accepted",
            )
        ]
        for index in VISUAL_CHECKPOINT_NAMES
    }
    terminal_metadata = {
        "run_id": VISUAL_PROGRESS_TERMINAL_ID,
        "status": "completed",
        "repo_root": str(root),
        "original_plan_path": str(terminal_plan),
        "active_plan_path": str(terminal_plan),
        "plan_path": str(terminal_plan),
        "workflow_name": "managed",
        "team": "base-team",
        "current_step_name": "done",
        "turns_completed": 11,
        "max_turns": 12,
        "run_started_at": "2026-09-11T08:00:00Z",
        "phase": "completed",
        "progress_history_complete": True,
        "active_implementation_scope": None,
        "implementation_attempts": terminal_attempts,
        "approved_checkpoints": [
            {"checkpoint_index": index, "status": "approved", "decision_number": index}
            for index in VISUAL_CHECKPOINT_NAMES
        ],
    }
    _write_json(terminal_dir / "run.json", terminal_metadata)
    _write_json(
        terminal_dir / "publication.json",
        {
            "stages": {
                "Final review": {"status": "succeeded"},
                "Merge": {"status": "succeeded"},
                "Publish": {"status": "pending", "reason": "Awaiting owner publication"},
                "CI": {"status": "unknown", "reason": "Not reported"},
                "Live verification": {"status": "unknown", "reason": "Not reported"},
            }
        },
    )

    stale_plan = root / "plans" / "in-progress" / "visual-stale-plan.md"
    stale_plan.write_text(_visual_plan({1, 2, 3, 4}), encoding="utf-8")
    stale_dir = root / ".aflow" / "runs" / VISUAL_PROGRESS_STALE_ID
    stale_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        stale_dir / "run.json",
        {
            "run_id": VISUAL_PROGRESS_STALE_ID,
            "status": "running",
            "repo_root": str(root),
            "original_plan_path": str(stale_plan),
            "active_plan_path": str(stale_plan),
            "plan_path": str(stale_plan),
            "workflow_name": "managed",
            "run_started_at": "2026-09-01T08:00:00Z",
            "progress_history_complete": False,
            "phase": "waiting",
        },
    )

    unknown_dir = root / ".aflow" / "runs" / VISUAL_PROGRESS_UNKNOWN_ID
    unknown_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        unknown_dir / "run.json",
        {
            "run_id": VISUAL_PROGRESS_UNKNOWN_ID,
            "status": "completed",
            "repo_root": str(root),
            "original_plan_path": str(root / "plans" / "in-progress" / "removed-plan.md"),
            "active_plan_path": str(root / "plans" / "in-progress" / "removed-plan.md"),
            "plan_path": str(root / "plans" / "in-progress" / "removed-plan.md"),
            "workflow_name": "managed",
        },
    )
    return {
        "run_dir": run_dir,
        "run_json": run_dir / "run.json",
        "initial_metadata": metadata,
        "active": True,
        "terminal_id": VISUAL_PROGRESS_TERMINAL_ID,
        "stale_id": VISUAL_PROGRESS_STALE_ID,
        "unknown_id": VISUAL_PROGRESS_UNKNOWN_ID,
    }


def _advance_visual_progress(fixture: dict[str, object]) -> None:
    run_json = fixture["run_json"]
    assert isinstance(run_json, Path)
    metadata = json.loads(run_json.read_text(encoding="utf-8"))
    assert isinstance(metadata, dict)
    metadata.update(
        {
            "current_step_name": "review",
            "turns_completed": 9,
            "phase": "reviewing",
            "active_implementation_scope": _visual_scope(6),
        }
    )
    attempts = metadata["implementation_attempts"]
    assert isinstance(attempts, dict)
    attempts["original::checkpoint-6"] = [
        _visual_invocation(
            VISUAL_PROGRESS_RUN_ID,
            6,
            9,
            selector="codex.current",
            model_display="Current worker",
            status="running",
            outcome="implementing",
        )
    ]
    _write_json(run_json, metadata)


def _reset_visual_progress(fixture: dict[str, object]) -> None:
    run_json = fixture["run_json"]
    initial = fixture["initial_metadata"]
    assert isinstance(run_json, Path)
    assert isinstance(initial, dict)
    _write_json(run_json, initial)


def _seed_checkpoint_history_review_fixture(root: Path) -> dict[str, object]:
    original = root / "plans" / "in-progress" / "visual-review-evidence.md"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text(_visual_plan({3}), encoding="utf-8")
    run_dir = root / ".aflow" / "runs" / REVIEW_EVIDENCE_RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    first_scope = {
        **_visual_scope(1),
        "original_plan_path": str(original),
        "awaiting_review": True,
        "current_partition_generation_id": "generation-one",
    }
    _write_visual_turn(
        run_dir,
        1,
        selector="codex.worker",
        status="completed",
        original_plan_path=original,
        checkpoint_index=1,
    )
    _write_visual_turn(
        run_dir,
        2,
        selector="codex.reviewer-actual",
        status="completed",
        role="reviewer",
        original_plan_path=original,
        checkpoint_index=2,
    )
    metadata: dict[str, object] = {
        "run_id": REVIEW_EVIDENCE_RUN_ID,
        "status": "running",
        "repo_root": str(root),
        "original_plan_path": str(original),
        "active_plan_path": str(original),
        "plan_path": str(original),
        "workflow_name": "managed",
        "team": "base-team",
        "current_step_name": "review",
        "turns_completed": 2,
        "max_turns": 8,
        "activity": "inactive",
        "phase": "reviewing",
        "progress_history_complete": True,
        "active_implementation_scope": first_scope,
        "approved_checkpoints": [{"checkpoint_index": 3, "status": "approved"}],
        "hotplug_history": [_visual_hotplug_transaction(stage="applied")],
        "implementation_attempts": {
            "original::checkpoint-1": [
                _visual_invocation(
                    REVIEW_EVIDENCE_RUN_ID,
                    1,
                    1,
                    selector="codex.worker",
                    model_display="Worker model",
                    status="completed",
                    outcome="completed",
                )
            ]
        },
        "review_rejection_history": [{
            "scope_id": "original::checkpoint-1",
            "source_run_id": REVIEW_EVIDENCE_RUN_ID,
            "review_turn_number": 2,
            "review_step_name": "review",
            "reviewer_selector": "codex.reviewer-recorded",
            "checkpoint_index": 1,
            "checkpoint_name": VISUAL_CHECKPOINT_NAMES[1],
            "review_summary": "The reviewer requested a repair.",
            "review_stdout_artifact_path": "turns/turn-002/stdout.txt",
        }],
        "repartition_history": [_verified_repartition(
            "original::checkpoint-1", "generation-one", 1
        )],
    }
    run_json = run_dir / "run.json"
    _write_json(run_json, metadata)
    _write_json(run_dir / "publication.json", {"plan_lifecycle": {"phase": "committed"}})
    _write_json(
        run_dir / "manager" / "decision-003" / "result.json",
        {
            "decision_number": 3,
            "finalized_turn_number": 1,
            "level": "lite",
            "trigger": "post_turn",
            "status": "accepted",
            "schema_version": 1,
            "action": "continue",
            "reason": "Continue to first review.",
        },
    )
    attempts = metadata["implementation_attempts"]
    assert isinstance(attempts, dict)
    _write_visual_boundary(
        run_dir,
        1,
        1,
        role="worker",
        scope=first_scope,
        selector="codex.worker",
        attempts=attempts,
    )
    _write_visual_boundary(
        run_dir,
        2,
        2,
        role="reviewer",
        scope=first_scope,
        selector="codex.reviewer-actual",
    )
    return {"run_json": run_json, "active": False}


def _advance_checkpoint_history_review_fixture(fixture: dict[str, object]) -> None:
    run_json = fixture["run_json"]
    assert isinstance(run_json, Path)
    metadata = json.loads(run_json.read_text(encoding="utf-8"))
    assert isinstance(metadata, dict)
    metadata.update(
        {
            "activity": "inactive",
            "phase": "reviewing",
            "turns_completed": 5,
            "active_implementation_scope": {
                **_visual_scope(2),
                "original_plan_path": metadata["original_plan_path"],
                "awaiting_review": True,
                "current_partition_generation_id": "generation-two",
            },
            "repartition_history": [
                *metadata["repartition_history"],
                _verified_repartition("original::checkpoint-1", "generation-two", 2),
            ],
        }
    )
    attempts = metadata.get("implementation_attempts")
    assert isinstance(attempts, dict)
    attempts["original::checkpoint-1"].append(
        _visual_invocation(
            REVIEW_EVIDENCE_RUN_ID,
            1,
            3,
            selector="codex.worker-repair",
            model_display="Repair worker model",
            status="completed",
            outcome="completed",
            repair=True,
        )
    )
    attempts["original::checkpoint-2"] = [
        _visual_invocation(
            REVIEW_EVIDENCE_RUN_ID,
            2,
            5,
            selector="codex.worker-cp2",
            model_display="Worker model",
            status="completed",
            outcome="completed",
        )
    ]
    original_plan_path = metadata.get("original_plan_path")
    assert isinstance(original_plan_path, str)
    first_scope = {
        **_visual_scope(1),
        "original_plan_path": original_plan_path,
        "awaiting_review": True,
        "current_partition_generation_id": "generation-one",
    }
    _write_visual_turn(
        run_json.parent,
        3,
        selector="codex.worker-repair",
        status="completed",
        original_plan_path=Path(original_plan_path),
        checkpoint_index=1,
    )
    _write_visual_boundary(
        run_json.parent,
        3,
        3,
        role="worker",
        scope=first_scope,
        selector="codex.worker-repair",
        attempts=attempts,
    )
    _write_visual_turn(
        run_json.parent,
        4,
        selector="codex.reviewer-success",
        status="completed",
        role="reviewer",
        original_plan_path=Path(original_plan_path),
        checkpoint_index=2,
    )
    _write_visual_boundary(
        run_json.parent,
        4,
        4,
        role="reviewer",
        scope=None,
        selector="codex.reviewer-success",
    )
    _write_visual_turn(
        run_json.parent,
        5,
        selector="codex.worker-cp2",
        status="completed",
        original_plan_path=Path(original_plan_path),
        checkpoint_index=2,
    )
    second_scope = {
        **_visual_scope(2),
        "original_plan_path": original_plan_path,
        "awaiting_review": True,
    }
    _write_visual_boundary(
        run_json.parent,
        5,
        5,
        role="worker",
        scope=second_scope,
        selector="codex.worker-cp2",
        attempts=attempts,
    )
    _write_successful_visual_review(
        run_json.parent,
        6,
        original_plan_path=Path(original_plan_path),
        checkpoint_index=2,
    )
    _write_visual_turn(
        run_json.parent,
        9,
        selector="codex.worker-repair",
        status="retry-scheduled",
        retry_attempt=1,
        retry_next_turn=True,
        original_plan_path=Path(original_plan_path),
        checkpoint_index=1,
    )
    metadata["active_implementation_scope"] = {
        **_visual_scope(4),
        "original_plan_path": original_plan_path,
        "awaiting_review": True,
    }
    _write_json(run_json, metadata)


def _complete_checkpoint_history_retry(fixture: dict[str, object]) -> None:
    run_json = fixture["run_json"]
    assert isinstance(run_json, Path)
    metadata = json.loads(run_json.read_text(encoding="utf-8"))
    assert isinstance(metadata, dict)
    original_plan_path = metadata.get("original_plan_path")
    assert isinstance(original_plan_path, str)
    _write_visual_turn(
        run_json.parent,
        10,
        selector="codex.worker-repair",
        status="completed",
        was_retry=True,
        original_plan_path=Path(original_plan_path),
        checkpoint_index=1,
    )


def _activate_checkpoint_history_reviewer(fixture: dict[str, object]) -> None:
    run_json = fixture["run_json"]
    assert isinstance(run_json, Path)
    metadata = json.loads(run_json.read_text(encoding="utf-8"))
    assert isinstance(metadata, dict)
    metadata.update({"activity": "active", "phase": "reviewing"})
    original_plan_path = metadata.get("original_plan_path")
    assert isinstance(original_plan_path, str)
    attempts = metadata.get("implementation_attempts")
    assert isinstance(attempts, dict)
    attempts["original::checkpoint-4"] = [
        _visual_invocation(
            REVIEW_EVIDENCE_RUN_ID,
            4,
            11,
            selector="codex.worker-cp4",
            model_display="Worker model",
            status="completed",
            outcome="completed",
        )
    ]
    scope = metadata.get("active_implementation_scope")
    assert isinstance(scope, dict)
    _write_visual_turn(
        run_json.parent,
        11,
        selector="codex.worker-cp4",
        status="completed",
        original_plan_path=Path(original_plan_path),
        checkpoint_index=4,
    )
    _write_visual_boundary(
        run_json.parent,
        11,
        11,
        role="worker",
        scope=scope,
        selector="codex.worker-cp4",
        attempts=attempts,
    )
    _write_visual_turn(
        run_json.parent,
        12,
        selector="codex.reviewer-active",
        status="running",
        role="reviewer",
        original_plan_path=Path(original_plan_path),
        checkpoint_index=3,
    )
    _write_json(run_json, metadata)
    fixture["active"] = True


def _inject_malformed_newest_review_turn(fixture: dict[str, object]) -> None:
    run_json = fixture["run_json"]
    assert isinstance(run_json, Path)
    malformed = run_json.parent / "turns" / "turn-013" / "result.json"
    malformed.parent.mkdir(parents=True, exist_ok=True)
    malformed.write_text(
        '{"turn_number": 13, "step_role": "reviewer",', encoding="utf-8"
    )


LONG_SUMMARY_TOKEN = "commit" + "0123456789abcdef" * 12


def _screenshot_path(tmp_path: Path, state: str, width: int, height: int) -> Path:
    artifact_root = Path(os.environ.get("AFLOW_BROWSER_ARTIFACT_DIR", str(tmp_path)))
    artifact_root.mkdir(parents=True, exist_ok=True)
    engine = os.environ.get("AFLOW_TEST_BROWSER", "chromium").strip().lower()
    return artifact_root / f"run-progress-{engine}-{state}-{width}x{height}.png"


def _geometry_path(tmp_path: Path, width: int, height: int) -> Path:
    artifact_root = Path(os.environ.get("AFLOW_BROWSER_ARTIFACT_DIR", str(tmp_path)))
    artifact_root.mkdir(parents=True, exist_ok=True)
    engine = os.environ.get("AFLOW_TEST_BROWSER", "chromium").strip().lower()
    return artifact_root / f"run-summary-wrapping-{engine}-{width}x{height}.json"


def _summary_geometry(page) -> dict[str, object]:
    return page.evaluate(
        """() => {
            const paragraph = [...document.querySelectorAll(
                '.run-detail .dashboard-section > p'
            )].find(element => element.textContent?.startsWith('Last finished summary:'))
            if (!paragraph) throw new Error('last finished summary paragraph not found')
            const rect = paragraph.getBoundingClientRect()
            return {
                viewportWidth: innerWidth,
                documentWidth: document.documentElement.scrollWidth,
                bodyWidth: document.body.scrollWidth,
                documentHeight: document.scrollingElement?.scrollHeight ?? 0,
                viewportHeight: innerHeight,
                paragraphClientWidth: paragraph.clientWidth,
                paragraphScrollWidth: paragraph.scrollWidth,
                paragraphRight: rect.right,
                overflowing: [...document.querySelectorAll('*')]
                    .map(element => ({
                        tag: element.tagName,
                        className: String(element.className || ''),
                        text: element.textContent?.trim().slice(0, 120),
                        right: element.getBoundingClientRect().right,
                    }))
                    .filter(element => element.right > innerWidth + 1)
                    .slice(0, 8),
            }
        }"""
    )


def _disable_summary_wrapping(page) -> None:
    page.evaluate(
        """() => {
            const style = document.createElement('style')
            style.dataset.testSummaryWrapping = 'disabled'
            style.textContent = '.run-detail .dashboard-section > p { overflow-wrap: normal !important; }'
            document.head.append(style)
        }"""
    )


def _enable_summary_wrapping(page) -> None:
    page.evaluate(
        """() => document.querySelector('style[data-test-summary-wrapping="disabled"]')?.remove()"""
    )


def _assert_shell_contract(page, width: int, height: int) -> None:
    metrics = page.evaluate(
        """() => ({
            width: innerWidth,
            scrollWidth: document.documentElement.scrollWidth,
            documentHeight: document.scrollingElement?.scrollHeight ?? 0,
            viewportHeight: innerHeight,
        })"""
    )
    assert metrics["scrollWidth"] <= metrics["width"] + 1, metrics
    assert metrics["documentHeight"] >= metrics["viewportHeight"], metrics
    main = page.locator(".workspace-main").bounding_box()
    assert main and main["height"] > 0, main
    if width >= 960 and height >= 600:
        assert page.locator(".app-header-row-one").is_visible()
        assert page.locator(".app-header-row-two").is_visible()
        assert page.locator(".header-slot-context").is_visible()
        return

    menu_button = page.get_by_role("button", name="Menu", exact=True)
    menu_button.click()
    menu = page.get_by_role("menu", name="Workspace navigation")
    expect(menu).to_be_visible()
    expect(menu.get_by_role("menuitem", name="Runs", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()


def _assert_selected_run_identity(page, run_id: str) -> None:
    expect(
        page.locator(".run-detail").get_by_role("button", name=run_id, exact=True)
    ).to_be_visible()


def test_checkpoint_history_review_evidence_and_generation_refresh(
    control_client,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep merged review evidence and a parent-row selection through refresh."""
    _, root, _, _ = control_client
    fixture = _seed_checkpoint_history_review_fixture(root)

    from aflow.control_plane import repository as repository_module

    original_activity = repository_module.project_activity

    def project_fixture_activity(status):
        projected = original_activity(status)
        if status.run_id != REVIEW_EVIDENCE_RUN_ID:
            return projected
        activity = "active" if fixture["active"] is True else "inactive"
        return replace(
            projected,
            status="running",
            activity=activity,
            status_reason_code=f"execution_{activity}",
            reason=None,
            evidence={**projected.evidence, "unit_active": activity == "active"},
        )

    monkeypatch.setattr(repository_module, "project_activity", project_fixture_activity)
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))
    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            _login(page, url)
            page.goto(f"{url}/?view=all-runs")
            page.get_by_role("heading", name="All runs", exact=True).wait_for()
            search = page.get_by_label("Search loaded runs", exact=True)
            search.fill(REVIEW_EVIDENCE_RUN_ID)
            row = page.locator("button.global-run-row").filter(
                has_text=REVIEW_EVIDENCE_RUN_ID
            ).first
            row.wait_for()
            markers = row.locator(".compact-run-progress-segment")
            expect(markers).to_have_count(11)
            expect(markers.nth(0)).to_have_class(re.compile(r"\bcurrent\b"))
            expect(markers.nth(1)).to_have_class(re.compile(r"\bpending\b"))
            expect(markers.nth(2)).to_have_class(re.compile(r"\bapproved\b"))
            row.click()
            history = page.locator('.checkpoint-history[aria-label="Checkpoint history"]')
            history.wait_for()
            serialized = page.evaluate(
                """async ({ projectId, runId }) => {
                  const response = await fetch(
                    `/api/control-plane/projects/${projectId}/runs/${runId}/context?level=full&full_scope=true`,
                  )
                  if (!response.ok) throw new Error(`context ${response.status}`)
                  return response.json()
                }""",
                {"projectId": PROJECT_ID, "runId": REVIEW_EVIDENCE_RUN_ID},
            )
            events = serialized["data"]["progress"]["events"]
            assert not any(
                event["kind"] == "checkpoint_approval"
                and event["decision_number"] == 3
                for event in events
            )
            navigation = history.locator(".checkpoint-history-navigation")
            assert navigation.get_by_role(
                "button", name="Unassigned or omitted history", exact=True
            ).count() == 0
            first_checkpoint = navigation.get_by_role(
                "button", name=re.compile(r"Checkpoint 1: Establish the bounded evidence contract")
            )
            expect(navigation.get_by_role(
                "button", name=re.compile(r"Checkpoint 2: Preserve exact executor identity")
            )).to_contain_text("Pending")
            expect(navigation.get_by_role(
                "button", name=re.compile(r"Checkpoint 3: Explain approved and recorded completion")
            )).to_contain_text("Approved")
            expect(first_checkpoint).to_contain_text("Awaiting review")
            expect(first_checkpoint).to_contain_text("1 review")
            first_checkpoint.click()
            detail_heading = history.locator(".checkpoint-history-detail-heading")
            expect(detail_heading).to_contain_text("Checkpoint 1: Establish the bounded evidence contract")
            expect(detail_heading).to_contain_text("Awaiting review")
            expect(history.locator(".checkpoint-history-timeline")).to_contain_text("Review rejection")
            expect(history.locator(".checkpoint-history-timeline")).to_contain_text("Rejected")

            delivery = history.locator("details.checkpoint-history-disclosure").filter(
                has_text="Delivery evidence"
            )
            delivery.locator("summary").click()
            expect(delivery).to_contain_text("Merge · Unknown")
            assert "Merge · Succeeded" not in delivery.inner_text()

            page.get_by_role("button", name="← Back to Checkpoints", exact=True).click()
            expect(navigation).to_be_visible()
            first_checkpoint.click()
            expect(detail_heading).to_contain_text("Checkpoint 1: Establish the bounded evidence contract")
            _advance_checkpoint_history_review_fixture(fixture)
            page.get_by_role("button", name="More", exact=True).click()
            page.get_by_role("menuitem", name="Refresh", exact=True).click()
            expect(detail_heading).to_contain_text("Checkpoint 1: Establish the bounded evidence contract")
            expect(history.locator(".checkpoint-history-timeline")).to_contain_text("Review rejection")
            expect(history.locator(".checkpoint-history-timeline")).to_contain_text("codex.reviewer-success")
            applied_changes = history.locator(
                ".checkpoint-history-timeline .checkpoint-history-event"
            ).filter(has_text="Applied change")
            expect(applied_changes).to_have_count(2)
            expect(applied_changes.filter(has_text="decision 1")).to_have_count(1)
            expect(applied_changes.filter(has_text="decision 2")).to_have_count(1)
            changes = history.locator("details.checkpoint-history-disclosure").filter(
                has_text="Team & change history"
            )
            changes.locator("summary").click()
            expect(changes).to_contain_text("generation generation-one")
            expect(changes).to_contain_text("generation generation-two")
            expect(changes).to_contain_text("Hotplug")
            expect(changes).to_contain_text("Recorded source model")
            expect(changes).to_contain_text("Recorded target model")
            expect(changes).to_contain_text("Applied")

            scheduled = page.evaluate(
                """async ({ projectId, runId }) => {
                  const response = await fetch(
                    `/api/control-plane/projects/${projectId}/runs/${runId}/context?level=full&full_scope=true`,
                  )
                  if (!response.ok) throw new Error(`context ${response.status}`)
                  return response.json()
                }""",
                {"projectId": PROJECT_ID, "runId": REVIEW_EVIDENCE_RUN_ID},
            )["data"]["progress"]
            scheduled_retries = [event for event in scheduled["events"] if event["kind"] == "runtime_retry"]
            assert scheduled["runtime_retries"]["value"] == 1
            assert scheduled["checkpoints"][0]["runtime_retries"]["value"] == 1
            assert len(scheduled_retries) == 1
            retry_event_id = scheduled_retries[0]["event_id"]

            _complete_checkpoint_history_retry(fixture)
            page.get_by_role("button", name="More", exact=True).click()
            page.get_by_role("menuitem", name="Refresh", exact=True).click()
            completed_retry = page.evaluate(
                """async ({ projectId, runId }) => {
                  const response = await fetch(
                    `/api/control-plane/projects/${projectId}/runs/${runId}/context?level=full&full_scope=true`,
                  )
                  if (!response.ok) throw new Error(`context ${response.status}`)
                  return response.json()
                }""",
                {"projectId": PROJECT_ID, "runId": REVIEW_EVIDENCE_RUN_ID},
            )["data"]["progress"]
            completed_retries = [
                event for event in completed_retry["events"] if event["kind"] == "runtime_retry"
            ]
            assert completed_retry["runtime_retries"]["value"] == 1
            assert completed_retry["checkpoints"][0]["runtime_retries"]["value"] == 1
            assert [event["event_id"] for event in completed_retries] == [retry_event_id]
            expect(detail_heading).to_contain_text("Checkpoint 1: Establish the bounded evidence contract")
            expect(
                history.locator(".checkpoint-history-timeline .checkpoint-history-event").filter(
                    has_text="Runtime retry"
                )
            ).to_have_count(1)
            retry_event = history.locator(
                ".checkpoint-history-timeline .checkpoint-history-event"
            ).filter(has_text="Runtime retry")
            retry_link = retry_event.get_by_role(
                "link", name="Retried invocation: turn 10", exact=True
            )
            expect(retry_link).to_have_attribute("href", re.compile(r"^#checkpoint-event-"))
            retry_target = retry_link.get_attribute("href")
            assert retry_target is not None
            expect(history.locator(retry_target)).to_contain_text("Worker attempt")
            target_turn = history.locator(retry_target).locator("dt").filter(
                has_text=re.compile(r"^Turn$")
            )
            expect(target_turn).to_have_count(1)
            expect(target_turn.locator("xpath=following-sibling::dd[1]")).to_have_text("10")
            expect(history.locator(retry_target)).to_contain_text("codex.worker-repair")

            page.get_by_role("button", name="← Back to Checkpoints", exact=True).click()
            expect(first_checkpoint).to_contain_text("2 reviews")
            second_checkpoint = navigation.get_by_role(
                "button", name=re.compile(r"Checkpoint 2: Preserve exact executor identity")
            )
            expect(second_checkpoint).to_contain_text("Approved")
            first_checkpoint.click()
            applied_changes = history.locator(
                ".checkpoint-history-timeline .checkpoint-history-event"
            ).filter(has_text="Applied change")
            expect(applied_changes).to_have_count(2)
            expect(applied_changes.filter(has_text="decision 1")).to_have_count(1)
            expect(applied_changes.filter(has_text="decision 2")).to_have_count(1)
            page.get_by_role("button", name="← Back to Checkpoints", exact=True).click()
            _activate_checkpoint_history_reviewer(fixture)
            page.get_by_role("button", name="More", exact=True).click()
            page.get_by_role("menuitem", name="Refresh", exact=True).click()
            expect(second_checkpoint).to_contain_text("Approved")
            third_checkpoint = navigation.get_by_role(
                "button", name=re.compile(r"Checkpoint 3: Explain approved and recorded completion")
            )
            expect(third_checkpoint).to_contain_text("Approved")
            fourth_checkpoint = navigation.get_by_role(
                "button", name=re.compile(r"Checkpoint 4: Retain earlier history during refresh")
            )
            expect(fourth_checkpoint).to_contain_text("Reviewing")
            fourth_checkpoint.click()
            expect(detail_heading).to_contain_text("Reviewing")
            expect(history).to_contain_text("codex.reviewer-active")
            _inject_malformed_newest_review_turn(fixture)
            page.get_by_role("button", name="More", exact=True).click()
            page.get_by_role("menuitem", name="Refresh", exact=True).click()
            expect(history.locator(".checkpoint-history-heading .status-pill")).to_have_text("Partial evidence")
            expect(history.locator(".checkpoint-history-summary")).to_contain_text("At least 5 attempts")
            expect(detail_heading).to_contain_text("Reviewing")
            expect(history.locator(".checkpoint-history-timeline")).to_contain_text("codex.reviewer-active")
            expect(
                history.locator(".checkpoint-history-summary dt").filter(
                    has_text="Last executor"
                ).locator("xpath=following-sibling::dd")
            ).to_have_text("Not reported")
            _assert_document_moves(page)
            page.screenshot(path=str(_screenshot_path(tmp_path, "review-evidence", 390, 844)), full_page=True)
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("width", "height"),
    ((1280, 720), (390, 844)),
    ids=("desktop", "mobile-390"),
)
def test_run_progress_transport_and_browser_parity(
    control_client,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
) -> None:
    _, root, _, _ = control_client
    fixture = _seed_issue35_progress_fixture(root)
    run_id = fixture["run_id"]
    missing_run_id = fixture["missing_run_id"]
    assert isinstance(run_id, str)
    assert isinstance(missing_run_id, str)

    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))
    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            _login(page, url)
            page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={run_id}")
            page.get_by_role("heading", name="Repair overlay", exact=True).wait_for()
            _assert_selected_run_identity(page, run_id)

            expect(
                page.get_by_text(
                    "CP4 of 14 · Unknown — Checkpoint 4: Stage 4", exact=True
                )
            ).to_be_visible()
            expect(
                page.get_by_text(
                    "Legacy execution record. Workflow controls are unavailable; history controls remain available.",
                    exact=True,
                )
            ).to_be_visible()
            detail_text = page.locator(".run-detail").inner_text()
            assert "? of 0" not in detail_text
            assert "All 0 checkpoints complete" not in detail_text
            _assert_shell_contract(page, width, height)
            page.screenshot(
                path=str(_screenshot_path(tmp_path, "repair", width, height)),
                full_page=True,
            )

            _close_issue35_repair_scope(fixture)
            page.get_by_role("button", name="More", exact=True).click()
            page.get_by_role("menuitem", name="Refresh", exact=True).click()
            expect(
                page.get_by_text(
                    "CP5 of 14 · Unknown — Checkpoint 5: Stage 5", exact=True
                )
            ).to_be_visible()
            detail_text = page.locator(".run-detail").inner_text()
            assert "? of 0" not in detail_text
            assert "All 0 checkpoints complete" not in detail_text
            page.screenshot(
                path=str(_screenshot_path(tmp_path, "next", width, height)),
                full_page=True,
            )

            page.goto(
                f"{url}/?project={PROJECT_ID}&view=runs&run={missing_run_id}"
            )
            page.get_by_role("heading", name="Missing evidence", exact=True).wait_for()
            _assert_selected_run_identity(page, missing_run_id)
            expect(
                page.locator(".checkpoint-history .status-pill")
            ).to_have_text("Evidence unavailable")
            detail_text = page.locator(".run-detail").inner_text()
            assert "? of 0" not in detail_text
            assert "All 0 checkpoints complete" not in detail_text
            page.screenshot(
                path=str(_screenshot_path(tmp_path, "unavailable", width, height)),
                full_page=True,
            )
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("width", "height"),
    (
        pytest.param(320, 568, id="phone-small"),
        pytest.param(390, 844, id="phone"),
        pytest.param(768, 1024, id="tablet-portrait"),
        pytest.param(844, 390, id="tablet-landscape"),
        pytest.param(1280, 720, id="desktop-short"),
        pytest.param(1440, 900, id="desktop"),
        pytest.param(390, 420, id="phone-short"),
    ),
)
def test_canonical_run_progress_visual_journey(
    control_client,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
) -> None:
    """Exercise the integrated overview → history → checkpoint journey."""
    _, root, _, _ = control_client
    fixture = _seed_visual_progress_fixture(root)

    from aflow.control_plane import repository as repository_module

    original_activity = repository_module.project_activity

    def project_fixture_activity(status):
        projected = original_activity(status)
        if status.run_id != VISUAL_PROGRESS_RUN_ID:
            return projected
        if fixture["active"]:
            return replace(
                projected,
                status="running",
                activity="active",
                status_reason_code="execution_active",
                reason=None,
                evidence={**projected.evidence, "unit_active": True},
            )
        return replace(projected, activity="inactive")

    monkeypatch.setattr(repository_module, "project_activity", project_fixture_activity)
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            _login(page, url)
            for theme in ("light", "dark"):
                if theme == "dark":
                    _reset_visual_progress(fixture)
                _set_theme_preference(page, theme)
                page.goto(f"{url}/?view=all-runs")
                page.get_by_role("heading", name="All runs", exact=True).wait_for()
                search = page.get_by_label("Search loaded runs", exact=True)
                search.fill(VISUAL_PROGRESS_RUN_ID)
                row = page.locator("button.global-run-row").filter(has_text=VISUAL_PROGRESS_RUN_ID).first
                row.wait_for()
                page.screenshot(
                    path=str(_screenshot_path(tmp_path, f"{theme}-overview", width, height)),
                    full_page=True,
                )
                row.click()

                history = page.locator('.checkpoint-history[aria-label="Checkpoint history"]')
                history.wait_for()
                expect(history).to_contain_text("4 / 11 approved")
                expect(history).to_contain_text(re.compile(r"CP5 of 11\s+·\s+Implementing"))
                expect(history).to_contain_text("6 attempts")
                expect(history).to_contain_text("1 repair pass")
                expect(history).to_contain_text("1 runtime retry")
                expect(history).to_contain_text("1 upgrade")
                expect(history.locator(".checkpoint-history-navigation")).to_contain_text("Repairing")
                expect(history).to_contain_text("codex.repair")
                _assert_header_and_flow(page)
                page.screenshot(
                    path=str(_screenshot_path(tmp_path, f"{theme}-history", width, height)),
                    full_page=True,
                )

                navigation = history.locator(".checkpoint-history-navigation")
                current_button = navigation.get_by_role("button", name=re.compile(r"Checkpoint 5: Inspect the long-lived executor"))
                current_button.wait_for()
                current_button.click()
                detail_heading = history.locator(".checkpoint-history-detail-heading h5")
                expect(detail_heading).to_contain_text("Checkpoint 5: Inspect the long-lived executor")
                timeline = history.locator(".checkpoint-history-timeline")
                expect(timeline).to_contain_text("Worker attempt")
                expect(timeline).to_contain_text("Review rejection")
                expect(timeline).to_contain_text("Repair attempt")
                expect(timeline).to_contain_text("Runtime retry")

                team_changes = history.locator("details.checkpoint-history-disclosure").filter(
                    has_text="Team & change history"
                )
                team_changes.locator("summary").click()
                expect(team_changes).to_contain_text("Applied changes")
                expect(team_changes).to_contain_text("base-team → repair-team")
                expect(team_changes).to_contain_text("Pending changes")
                expect(team_changes).to_contain_text("next-team")

                delivery = history.locator("details.checkpoint-history-disclosure").filter(
                    has_text="Delivery evidence"
                )
                delivery.locator("summary").click()
                expect(delivery).to_contain_text("CI · Unknown")
                expect(delivery).to_contain_text("Live verification · Unknown")
                assert "CI · Succeeded" not in delivery.inner_text()
                assert "Live verification · Succeeded" not in delivery.inner_text()

                evidence = history.locator("details.checkpoint-history-disclosure").filter(
                    has_text="Count definitions & evidence"
                )
                evidence.locator("summary").click()
                expect(evidence).to_contain_text("Counts come from the bounded canonical evidence projection")
                expect(evidence).to_contain_text("Complete evidence")
                _assert_document_moves(page)
                _assert_last_action_hit_test(page)
                if (width, height) == (390, 844) and theme == "light":
                    _double_visible_text(page)
                    try:
                        _assert_no_horizontal_overflow(page)
                        _assert_last_action_hit_test(page)
                    finally:
                        _clear_test_text_zoom(page)
                page.screenshot(
                    path=str(_screenshot_path(tmp_path, f"{theme}-checkpoint", width, height)),
                    full_page=True,
                )

                checkpoint_four = navigation.get_by_role(
                    "button", name=re.compile(r"Checkpoint 4: Retain earlier history")
                )
                if _compact(page):
                    page.get_by_role("button", name="← Back to Checkpoints", exact=True).click()
                    navigation.wait_for(state="visible")
                checkpoint_four.scroll_into_view_if_needed()
                checkpoint_four.focus()
                list_scroll = page.evaluate("() => document.scrollingElement?.scrollTop ?? 0")
                checkpoint_four.press("Enter")
                expect(detail_heading).to_contain_text("Checkpoint 4: Retain earlier history")
                if _compact(page):
                    page.get_by_role("button", name="← Back to Checkpoints", exact=True).click()
                    navigation.wait_for(state="visible")
                    page.wait_for_function(
                        "expected => Math.abs((document.scrollingElement?.scrollTop ?? 0) - expected) <= 4",
                        arg=list_scroll,
                    )
                    restored_scroll = page.evaluate("() => document.scrollingElement?.scrollTop ?? 0")
                    assert abs(restored_scroll - list_scroll) <= 4, {
                        "before": list_scroll,
                        "after": restored_scroll,
                    }
                    checkpoint_four.click()
                    expect(detail_heading).to_contain_text("Checkpoint 4: Retain earlier history")

                _advance_visual_progress(fixture)
                page.get_by_role("button", name="More", exact=True).click()
                page.get_by_role("menuitem", name="Refresh", exact=True).click()
                expect(history).to_contain_text(re.compile(r"CP6 of 11\s+·\s+Reviewing"))
                expect(detail_heading).to_contain_text("Checkpoint 4: Retain earlier history")

                if _compact(page):
                    page.get_by_role("button", name="← Back to Checkpoints", exact=True).click()
                    navigation.wait_for(state="visible")
                navigation.get_by_role("button", name=re.compile(r"Checkpoint 6: Keep pending work visible")).click()
                expect(detail_heading).to_contain_text("Checkpoint 6: Keep pending work visible")
                if _compact(page):
                    page.get_by_role("button", name="← Back to Checkpoints", exact=True).click()
                    navigation.wait_for(state="visible")

                if width == 1280 and height == 720 and theme == "light":
                    page.goto(f"{url}/?view=all-runs")
                    search = page.get_by_label("Search loaded runs", exact=True)
                    search.fill(str(fixture["terminal_id"]))
                    terminal_row = page.locator("button.global-run-row").filter(
                        has_text=str(fixture["terminal_id"])
                    ).first
                    terminal_row.wait_for()
                    terminal_row.click()
                    terminal_history = page.locator('.checkpoint-history[aria-label="Checkpoint history"]')
                    terminal_history.wait_for()
                    expect(terminal_history).to_contain_text("11 / 11 approved")
                    terminal_delivery = terminal_history.locator(
                        "details.checkpoint-history-disclosure"
                    ).filter(has_text="Delivery evidence")
                    terminal_delivery.locator("summary").click()
                    expect(terminal_delivery).to_contain_text("Publish · Pending")
                    expect(terminal_delivery).to_contain_text("CI · Unknown")
                    assert "CI · Succeeded" not in terminal_delivery.inner_text()

                    page.goto(f"{url}/?view=all-runs")
                    search = page.get_by_label("Search loaded runs", exact=True)
                    search.fill(str(fixture["stale_id"]))
                    stale_row = page.locator("button.global-run-row").filter(
                        has_text=str(fixture["stale_id"])
                    ).first
                    stale_row.wait_for()
                    stale_row.click()
                    stale_history = page.locator('.checkpoint-history[aria-label="Checkpoint history"]')
                    stale_history.wait_for()
                    expect(stale_history).to_contain_text("Partial evidence")
                    expect(stale_history).to_contain_text("Unknown / 11 approved")

                    page.goto(f"{url}/?view=all-runs")
                    search = page.get_by_label("Search loaded runs", exact=True)
                    search.fill(str(fixture["unknown_id"]))
                    unknown_row = page.locator("button.global-run-row").filter(
                        has_text=str(fixture["unknown_id"])
                    ).first
                    unknown_row.wait_for()
                    unknown_row.click()
                    unknown_history = page.locator('.checkpoint-history[aria-label="Checkpoint history"]')
                    unknown_history.wait_for()
                    expect(unknown_history).to_contain_text("Evidence unavailable")
                    expect(unknown_history).to_contain_text("Unknown checkpoints")
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("width", "height"),
    ((320, 568), (390, 844), (1280, 720)),
    ids=("phone-320", "phone-390", "desktop"),
)
def test_run_summary_wraps_without_document_overflow(
    control_client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
) -> None:
    _, root, _, _ = control_client
    fixture = _seed_issue35_progress_fixture(root)
    run_id = fixture["run_id"]
    run_dir = fixture["run_dir"]
    run_json = fixture["run_json"]
    assert isinstance(run_id, str)
    assert isinstance(run_dir, Path)
    assert isinstance(run_json, Path)
    metadata = json.loads(run_json.read_text(encoding="utf-8"))
    assert isinstance(metadata, dict)
    # Keep the disposable shell focused on the finalized progress prose; the
    # active fixture's unavailable-control hint is a separate narrow-screen
    # presentation case.
    metadata["status"] = "completed"
    run_json.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    finished_stdout = run_dir / "turns" / "turn-003" / "stdout.txt"
    synthetic_summary = f"review approved with exact evidence {LONG_SUMMARY_TOKEN}\n"
    finished_stdout.write_text(synthetic_summary, encoding="utf-8")

    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))
    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            _login(page, url)
            page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={run_id}")
            page.get_by_role("heading", name="Repair overlay", exact=True).wait_for()
            summary = page.locator(
                ".run-detail .dashboard-section > p"
            ).filter(has_text="Last finished summary").first
            summary.wait_for()
            expect(summary).to_contain_text(LONG_SUMMARY_TOKEN)

            _disable_summary_wrapping(page)
            before = _summary_geometry(page)
            _enable_summary_wrapping(page)
            after = _summary_geometry(page)
            (geometry_path := _geometry_path(tmp_path, width, height)).write_text(
                json.dumps({"before_no_wrap": before, "after_wrap": after}, indent=2) + "\n",
                encoding="utf-8",
            )

            if width <= 390:
                assert before["documentWidth"] > width + 1, before
                assert before["paragraphScrollWidth"] > before["paragraphClientWidth"], before
            assert after["documentWidth"] <= width + 1, {"path": geometry_path, **after}
            assert after["bodyWidth"] <= width + 1, {"path": geometry_path, **after}
            assert after["paragraphRight"] <= width + 1, {"path": geometry_path, **after}
            assert after["documentHeight"] >= height, after
            assert LONG_SUMMARY_TOKEN in page.locator(".run-detail").inner_text()

            run_detail = page.locator(".run-detail")
            expect(run_detail.get_by_title("Copy run ID", exact=True)).to_be_visible()
            actions = run_detail.get_by_role("button", name="More run actions", exact=True)
            expect(actions).to_be_visible()
            actions.click()
            menu = page.get_by_role("menu", name="More run actions", exact=True)
            expect(menu).to_be_visible()
            page.keyboard.press("Escape")
            expect(menu).to_be_hidden()
        finally:
            browser.close()
