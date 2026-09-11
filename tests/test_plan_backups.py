from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aflow import plan_backups
from aflow.plan_backups import (
    PROVENANCE_DIRECTORY_NAME,
    read_backup_provenance,
)
from aflow.workflow import (
    WorkflowError,
    _backup_active_followup_plan,
    _backup_original_plan,
    move_completed_plan_to_done,
)


def _sidecar_path(repo_root: Path, backup_path: Path) -> Path:
    return (
        repo_root
        / "plans"
        / "backups"
        / PROVENANCE_DIRECTORY_NAME
        / f"{backup_path.name}.json"
    )


def _record(repo_root: Path, backup_path: Path) -> dict[str, object]:
    record = read_backup_provenance(repo_root, backup_path)
    assert record is not None
    return record


def _reference_without_timing(reference: dict[str, object]) -> dict[str, object]:
    return {
        key: reference[key]
        for key in ("event", "run_id", "turn_number", "source_path")
    }


def test_snapshot_capture_reuses_body_and_records_startup_and_resume(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    source = tmp_path / "plans" / "in-progress" / "plan.md"
    source.parent.mkdir(parents=True)
    source_bytes = b"# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n"
    source.write_bytes(source_bytes)

    first = _backup_original_plan(
        repo_root,
        source,
        event="startup_preparation",
    )
    second = _backup_original_plan(
        repo_root,
        source,
        event="resume_start",
        run_id="run-002",
    )
    duplicate = _backup_original_plan(
        repo_root,
        source,
        event="resume_start",
        run_id="run-002",
    )

    assert first == second == duplicate
    assert first.read_bytes() == source_bytes
    record = _record(repo_root, first)
    assert record["schema_version"] == 1
    assert record["backup_filename"] == first.name
    assert record["content_sha256"] == hashlib.sha256(source_bytes).hexdigest()
    assert record["kind"] == "snapshot"
    assert record["source_plan_path"] == str(source.resolve())
    assert record["original_plan_path"] is None
    assert record["historical_origin"] == "known"
    assert record["first_capture_event"] == "startup_preparation"
    assert record["first_run_id"] is None
    assert record["baseline_status"] == "unknown"
    assert record["baseline_reference"] is None
    references = record["capture_references"]
    assert isinstance(references, list)
    assert [
        _reference_without_timing(reference)
        for reference in references
    ] == [
        {
            "event": "startup_preparation",
            "run_id": None,
            "turn_number": None,
            "source_path": str(source.resolve()),
        },
        {
            "event": "resume_start",
            "run_id": "run-002",
            "turn_number": None,
            "source_path": str(source.resolve()),
        },
    ]
    assert all(
        isinstance(reference.get("timestamp"), str)
        and reference["timestamp"]
        for reference in references
    )


def test_changed_snapshot_preserves_old_and_new_body_bytes(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    source = tmp_path / "plan.md"
    first_bytes = b"first snapshot\n"
    second_bytes = b"second snapshot\n"
    source.write_bytes(first_bytes)

    first = _backup_original_plan(repo_root, source, event="workflow_start")
    source.write_bytes(second_bytes)
    second = _backup_original_plan(repo_root, source, event="workflow_start")

    assert first.name == "plan.md"
    assert second.name == "plan_v02.md"
    assert first.read_bytes() == first_bytes
    assert second.read_bytes() == second_bytes
    assert _record(repo_root, first)["content_sha256"] == hashlib.sha256(first_bytes).hexdigest()
    assert _record(repo_root, second)["content_sha256"] == hashlib.sha256(second_bytes).hexdigest()


def test_followup_capture_preserves_source_and_original_paths(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    original = tmp_path / "original.md"
    followup = tmp_path / "plan-cp01-v01.md"
    original.write_text("# Original\n", encoding="utf-8")
    followup_bytes = b"# Follow-up\n"
    followup.write_bytes(followup_bytes)

    backup = _backup_active_followup_plan(
        repo_root,
        original,
        followup,
        event="before_followup_turn",
        run_id="run-003",
        turn_number=4,
    )
    assert backup is not None
    assert backup.read_bytes() == followup_bytes
    record = _record(repo_root, backup)
    assert record["kind"] == "follow_up"
    assert record["source_plan_path"] == str(followup.resolve())
    assert record["original_plan_path"] == str(original.resolve())
    assert record["first_capture_event"] == "before_followup_turn"
    assert record["first_run_id"] == "run-003"
    references = record["capture_references"]
    assert isinstance(references, list)
    assert _reference_without_timing(references[0]) == {
        "event": "before_followup_turn",
        "run_id": "run-003",
        "turn_number": 4,
        "source_path": str(followup.resolve()),
    }
    assert isinstance(references[0].get("timestamp"), str)


def test_missing_or_corrupt_sidecar_is_unknown_and_new_capture_is_explicit(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    source = tmp_path / "plan.md"
    source.write_text("legacy body\n", encoding="utf-8")
    backup_dir = repo_root / "plans" / "backups"
    backup_dir.mkdir(parents=True)
    backup = backup_dir / source.name
    backup.write_bytes(source.read_bytes())

    assert read_backup_provenance(repo_root, backup) is None
    first = _backup_original_plan(
        repo_root,
        source,
        event="startup_preparation",
    )
    first_record = _record(repo_root, first)
    assert first_record["historical_origin"] == "unknown"
    references = first_record["capture_references"]
    assert isinstance(references, list)
    assert _reference_without_timing(references[0]) == {
        "event": "startup_preparation",
        "run_id": None,
        "turn_number": None,
        "source_path": str(source.resolve()),
    }
    assert isinstance(references[0].get("timestamp"), str)

    sidecar = _sidecar_path(repo_root, backup)
    sidecar.write_text("{not-json", encoding="utf-8")
    assert read_backup_provenance(repo_root, backup) is None
    _backup_original_plan(repo_root, source, event="resume_start", run_id="run-004")
    second_record = _record(repo_root, backup)
    assert second_record["historical_origin"] == "unknown"
    assert second_record["first_capture_event"] == "resume_start"
    assert second_record["first_run_id"] == "run-004"


def test_provenance_rejects_escaping_and_symlinked_paths(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    source = tmp_path / "plan.md"
    source.write_text("body\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()

    backup_dir = repo_root / "plans" / "backups"
    backup_dir.mkdir(parents=True)
    (backup_dir / "plan.md").write_bytes(source.read_bytes())
    provenance_dir = backup_dir / PROVENANCE_DIRECTORY_NAME
    provenance_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkflowError, match="provenance directory.*symlink"):
        _backup_original_plan(repo_root, source, event="workflow_start")
    assert list(outside.iterdir()) == []

    unsafe_repo = tmp_path / "unsafe-repo"
    unsafe_repo.mkdir()
    unsafe_plans = tmp_path / "unsafe-plans"
    unsafe_plans.mkdir()
    (unsafe_repo / "plans").symlink_to(unsafe_plans, target_is_directory=True)
    with pytest.raises(WorkflowError, match="plans directory.*symlink"):
        _backup_original_plan(unsafe_repo, source, event="workflow_start")
    assert list(unsafe_plans.iterdir()) == []

    source_target = tmp_path / "source-target.md"
    source_target.write_text("target\n", encoding="utf-8")
    source_link = tmp_path / "source-link.md"
    source_link.symlink_to(source_target)
    with pytest.raises(WorkflowError, match="must not be a symlink"):
        _backup_original_plan(tmp_path / "safe-repo", source_link, event="workflow_start")


def test_failed_provenance_write_fails_backup_without_claiming_saved_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    source = tmp_path / "plan.md"
    source.write_text("body\n", encoding="utf-8")

    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError("metadata unavailable")

    monkeypatch.setattr(plan_backups, "_write_atomic_json", fail_write)
    with pytest.raises(WorkflowError, match="failed to back up"):
        _backup_original_plan(repo_root, source, event="workflow_start")

    backup = repo_root / "plans" / "backups" / "plan.md"
    assert backup.read_bytes() == source.read_bytes()
    assert not _sidecar_path(repo_root, backup).exists()
    assert read_backup_provenance(repo_root, backup) is None


def test_controller_done_move_records_exact_destination_alias(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    source = repo_root / "plans" / "in-progress" / "plan.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"plan bytes\n")
    backup = _backup_original_plan(repo_root, source, event="workflow_start")

    destination = move_completed_plan_to_done(repo_root, source)

    assert destination == repo_root / "plans" / "done" / "plan.md"
    record = _record(repo_root, backup)
    assert str(destination.resolve()) in record["path_aliases"]
    assert backup.read_bytes() == b"plan bytes\n"
