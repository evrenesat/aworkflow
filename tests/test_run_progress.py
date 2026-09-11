from __future__ import annotations

import json
from pathlib import Path

from aflow.control_plane import build_context_bundle
from aflow.control_plane.run_progress import project_run_progress


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


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

    assert progress["availability"] == "available"
    assert progress["checkpoint"] == {"index": 4, "name": "Checkpoint 4: Stage 4"}
    assert progress["total"] == 14
    assert progress["complete"] is False
    assert progress["repairing"] is True
    assert progress["overlay_path"] == str(overlay)
    assert progress["reason"] is None
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
