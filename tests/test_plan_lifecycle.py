from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from aflow.plan_backups import plan_identity_for_path
from aflow.plan_lifecycle import PlanLifecycle, PlanLifecycleConflict, PlanLifecycleError
from aflow.project_admission import ProjectAdmission, ProjectPlanClaimConflict
from aflow.publication import PublicationError, _require_no_other_failed_publication
from aflow.run_state import ControllerConfig
from aflow.workflow import WorkflowError, run_workflow
from aflow.harnesses.codex import CodexAdapter
from tests._support import _make_simple_wf_config


_PLAN = (
    b"# Plan\n\n## Git Tracking\n\n- Plan Branch: `feature/test`\n"
    b"- Pre-Handoff Base HEAD: `abc123`\n\n"
    b"### [ ] Checkpoint 1: Work\n- [ ] do it\n"
)


def _source(root: Path, name: str = "original.md") -> Path:
    path = root / "plans" / "in-progress" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_PLAN)
    return path


def test_failure_move_preserves_bytes_identity_and_lineage(tmp_path: Path) -> None:
    source = _source(tmp_path)
    revision = hashlib.sha256(_PLAN).hexdigest()
    lifecycle = PlanLifecycle(tmp_path)
    destination = lifecycle.move(
        source, "failed", expected_revision=revision,
        reason_code="terminal_execution_failure", reason="Worker stopped",
        source_run_id="source-run",
    )
    assert not source.exists()
    assert destination.read_bytes() == _PLAN
    identity = plan_identity_for_path(tmp_path, destination)
    assert identity is not None
    record = lifecycle.record_for(destination)
    assert record is not None
    assert record["identity"] == identity
    assert record["original_path"] == str(source)
    assert record["current_location"] == "failed"
    assert record["source_run_id"] == "source-run"
    assert lifecycle.move(
        source, "failed", expected_revision=revision,
        reason_code="terminal_execution_failure", reason="Worker stopped",
        source_run_id="source-run",
    ) == destination


def test_collision_and_revision_mismatch_preserve_source(tmp_path: Path) -> None:
    source = _source(tmp_path)
    lifecycle = PlanLifecycle(tmp_path)
    destination = lifecycle.path("needs_plan_change", source.name, create=True)
    destination.write_bytes(b"other")
    with pytest.raises(PlanLifecycleConflict, match="destination"):
        lifecycle.move(
            source, "needs_plan_change", expected_revision=hashlib.sha256(_PLAN).hexdigest(),
            reason_code="invalid_plan", reason="Invalid plan",
        )
    assert source.read_bytes() == _PLAN
    destination.unlink()
    with pytest.raises(PlanLifecycleConflict, match="revision"):
        lifecycle.move(
            source, "needs_plan_change", expected_revision="0" * 64,
            reason_code="invalid_plan", reason="Invalid plan",
        )
    assert source.read_bytes() == _PLAN


def test_recovery_rejects_unrelated_same_bytes_at_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    lifecycle = PlanLifecycle(tmp_path)
    monkeypatch.setattr(lifecycle, "_complete", lambda record, path: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        lifecycle.move(
            source, "failed", expected_revision=hashlib.sha256(_PLAN).hexdigest(),
            reason_code="terminal_execution_failure", reason="Failed",
        )
    os.link(source, tmp_path / "retained-source-inode")
    source.unlink()
    destination = lifecycle.path("failed", source.name, create=True)
    destination.write_bytes(_PLAN)
    with pytest.raises(PlanLifecycleConflict, match="destination"):
        PlanLifecycle(tmp_path).recover()
    assert destination.read_bytes() == _PLAN


def test_live_plan_claim_blocks_lifecycle_move(tmp_path: Path) -> None:
    source = _source(tmp_path)
    admission = ProjectAdmission(tmp_path)
    admission.acquire("active-run", plan_path=source, idempotency_key="claim")
    with pytest.raises(ProjectPlanClaimConflict):
        with admission.plan_lifecycle_guard(source):
            PlanLifecycle(tmp_path).move(
                source, "failed", expected_revision=hashlib.sha256(_PLAN).hexdigest(),
                reason_code="terminal_execution_failure", reason="Failed",
            )
    assert source.read_bytes() == _PLAN


def test_failed_publication_blocks_unrelated_delivery_but_allows_its_resume(
    tmp_path: Path,
) -> None:
    runs = tmp_path / ".aflow" / "runs"
    failed = runs / "failed-run"
    failed.mkdir(parents=True)
    failed_receipt = {"status": "failed", "remote": "origin", "branch": "main"}
    (failed / "publication.json").write_text(
        json.dumps(failed_receipt), encoding="utf-8"
    )
    (failed / "run.json").write_text('{}', encoding="utf-8")
    unrelated = runs / "other-run"
    unrelated.mkdir()
    with pytest.raises(PublicationError, match="earlier failed publication"):
        _require_no_other_failed_publication(unrelated)
    other_published = runs / "other-published"
    other_published.mkdir()
    (other_published / "publication.json").write_text(
        json.dumps({
            "status": "published", "commit": "a" * 40,
            "source_commit": "a" * 40,
            "remote": "origin", "branch": "main",
        }), encoding="utf-8",
    )
    with pytest.raises(PublicationError, match="earlier failed publication"):
        _require_no_other_failed_publication(unrelated)
    resumed = runs / "resumed-run"
    resumed.mkdir()
    (resumed / "run.json").write_text(
        json.dumps({"resumed_from_run_id": "failed-run"}), encoding="utf-8"
    )
    _require_no_other_failed_publication(resumed)
    resumed_receipt_path = resumed / "publication.json"
    for status in ("pending", "failed"):
        resumed_receipt_path.write_text(
            json.dumps({"status": status, "remote": "origin", "branch": "main"}),
            encoding="utf-8",
        )
        with pytest.raises(PublicationError, match="earlier failed publication"):
            _require_no_other_failed_publication(unrelated)
    successful = {
        "status": "published", "commit": "b" * 40,
        "source_commit": "b" * 40,
        "remote": "origin", "branch": "main",
    }
    for changes in (
        {"commit": "invalid"}, {"source_commit": "invalid"}, {"branch": "other"},
    ):
        resumed_receipt_path.write_text(
            json.dumps({**successful, **changes}), encoding="utf-8"
        )
        with pytest.raises(PublicationError, match="earlier failed publication"):
            _require_no_other_failed_publication(unrelated)
    resumed_receipt_path.write_text(json.dumps(successful), encoding="utf-8")
    _require_no_other_failed_publication(unrelated)
    assert json.loads((failed / "publication.json").read_text()) == failed_receipt


def test_published_grandchild_repairs_failed_publication_gate(tmp_path: Path) -> None:
    runs = tmp_path / ".aflow" / "runs"
    failed = runs / "failed-run"
    middle = runs / "middle-run"
    published = runs / "published-run"
    unrelated = runs / "unrelated-run"
    for path in (failed, middle, published, unrelated):
        path.mkdir(parents=True)
    (failed / "publication.json").write_text('{"status":"failed"}', encoding="utf-8")
    (middle / "run.json").write_text(
        '{"resumed_from_run_id":"failed-run"}', encoding="utf-8"
    )
    (published / "run.json").write_text(
        '{"resumed_from_run_id":"middle-run"}', encoding="utf-8"
    )
    (published / "publication.json").write_text(
        json.dumps({
            "status": "published", "commit": "c" * 40,
            "source_commit": "c" * 40,
            "remote": "origin", "branch": "main",
        }), encoding="utf-8",
    )
    _require_no_other_failed_publication(unrelated)


def test_publication_gate_rejects_unsafe_receipt_even_after_repair(tmp_path: Path) -> None:
    runs = tmp_path / ".aflow" / "runs"
    failed = runs / "failed-run"
    published = runs / "published-run"
    unrelated = runs / "unrelated-run"
    for path in (failed, published, unrelated):
        path.mkdir(parents=True)
    (failed / "publication.json").write_text('{"status":"failed"}', encoding="utf-8")
    (published / "run.json").write_text(
        '{"resumed_from_run_id":"failed-run"}', encoding="utf-8"
    )
    (published / "publication.json").symlink_to(tmp_path / "outside.json")
    with pytest.raises(PublicationError, match="receipt is unsafe"):
        _require_no_other_failed_publication(unrelated)


def test_prepared_journal_recovers_same_bytes_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    lifecycle = PlanLifecycle(tmp_path)
    original_complete = lifecycle._complete

    def interrupt(record: dict[str, object], path: Path) -> Path:
        raise RuntimeError("synthetic crash after durable intent")

    monkeypatch.setattr(lifecycle, "_complete", interrupt)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        lifecycle.move(
            source, "needs_plan_change", expected_revision=hashlib.sha256(_PLAN).hexdigest(),
            reason_code="invalid_plan", reason="Invalid plan",
        )
    monkeypatch.setattr(lifecycle, "_complete", original_complete)
    recovered = lifecycle.recover()
    assert len(recovered) == 1
    assert recovered[0].read_bytes() == _PLAN
    assert not source.exists()
    assert lifecycle.recover() == ()


def test_prepared_move_recovers_through_parent_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    alias = tmp_path.with_name(f"{tmp_path.name}-alias")
    alias.symlink_to(tmp_path, target_is_directory=True)
    lifecycle = PlanLifecycle(tmp_path)
    alias_source = alias / "plans" / "in-progress" / source.name
    original_complete = lifecycle._complete
    monkeypatch.setattr(
        lifecycle, "_complete",
        lambda record, path: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    try:
        with pytest.raises(RuntimeError, match="crash"):
            lifecycle.move(
                alias_source, "failed", expected_revision=hashlib.sha256(_PLAN).hexdigest(),
                reason_code="terminal_execution_failure", reason="Failed",
            )
        monkeypatch.setattr(lifecycle, "_complete", original_complete)
        assert lifecycle.recover_for_path(alias_source) == (
            tmp_path / "plans" / "failed" / source.name,
        )
        alias_destination = alias / "plans" / "failed" / source.name
        assert alias_destination.read_bytes() == _PLAN
        assert lifecycle.record_for(alias_destination)["current_location"] == "failed"
        assert lifecycle.recover_for_path(alias_source) == ()
    finally:
        alias.unlink()


def test_replay_finishes_move_after_file_link_but_before_identity_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    revision = hashlib.sha256(_PLAN).hexdigest()
    lifecycle = PlanLifecycle(tmp_path)
    from aflow import plan_lifecycle

    real_update = plan_lifecycle.record_plan_lifecycle_move
    monkeypatch.setattr(
        plan_lifecycle, "record_plan_lifecycle_move",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("crash")),
    )
    with pytest.raises(PlanLifecycleError, match="identity"):
        lifecycle.move(
            source, "failed", expected_revision=revision,
            reason_code="terminal_execution_failure", reason="Failed",
        )
    assert not source.exists()
    monkeypatch.setattr(plan_lifecycle, "record_plan_lifecycle_move", real_update)
    destination = lifecycle.move(
        source, "failed", expected_revision=revision,
        reason_code="terminal_execution_failure", reason="Failed",
    )
    assert destination.read_bytes() == _PLAN
    assert lifecycle.record_for(destination)["current_location"] == "failed"


def test_rejects_symlinked_plan_and_destination(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_bytes(_PLAN)
    source = _source(tmp_path)
    source.unlink()
    source.symlink_to(outside)
    lifecycle = PlanLifecycle(tmp_path)
    with pytest.raises(PlanLifecycleError):
        lifecycle.move(
            source, "failed", expected_revision=hashlib.sha256(_PLAN).hexdigest(),
            reason_code="terminal_execution_failure", reason="Failed",
        )
    assert outside.read_bytes() == _PLAN


def test_parent_alias_does_not_admit_symlinked_final_plan(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_bytes(_PLAN)
    source = _source(tmp_path)
    source.unlink()
    source.symlink_to(outside)
    alias = tmp_path.with_name(f"{tmp_path.name}-alias")
    alias.symlink_to(tmp_path, target_is_directory=True)
    lifecycle = PlanLifecycle(tmp_path)
    try:
        alias_source = alias / "plans" / "in-progress" / source.name
        with pytest.raises(PlanLifecycleError, match="symlink"):
            lifecycle.move(
                alias_source, "failed", expected_revision=hashlib.sha256(_PLAN).hexdigest(),
                reason_code="terminal_execution_failure", reason="Failed",
            )
        with pytest.raises(PlanLifecycleError, match="symlink"):
            lifecycle.recover_for_path(alias_source)
        with pytest.raises(PlanLifecycleError, match="symlink"):
            lifecycle.record_for(alias_source)
        assert outside.read_bytes() == _PLAN
        assert not (tmp_path / "plans" / "failed" / source.name).exists()
    finally:
        alias.unlink()


def test_lifecycle_rejects_outside_and_missing_parent(tmp_path: Path) -> None:
    source = _source(tmp_path)
    lifecycle = PlanLifecycle(tmp_path)
    outside = tmp_path.parent / "other-repository" / "plans" / "in-progress" / source.name
    missing = tmp_path / "plans" / "missing" / source.name
    for path in (outside, missing):
        with pytest.raises(PlanLifecycleError):
            lifecycle.move(
                path, "failed", expected_revision=hashlib.sha256(_PLAN).hexdigest(),
                reason_code="terminal_execution_failure", reason="Failed",
            )
        with pytest.raises(PlanLifecycleError):
            lifecycle.recover_for_path(path)
        with pytest.raises(PlanLifecycleError):
            lifecycle.record_for(path)
    assert source.read_bytes() == _PLAN


def test_journal_reason_is_bounded_and_safe(tmp_path: Path) -> None:
    source = _source(tmp_path)
    lifecycle = PlanLifecycle(tmp_path)
    with pytest.raises(PlanLifecycleError, match="reason"):
        lifecycle.move(
            source, "failed", expected_revision=hashlib.sha256(_PLAN).hexdigest(),
            reason_code="failed", reason="secret\nnext line",
        )
    assert source.read_bytes() == _PLAN
    assert not list((tmp_path / ".aflow").glob("plan-lifecycle/*.json"))


def test_controller_failure_moves_only_original_after_terminal_evidence(tmp_path: Path) -> None:
    source = _source(tmp_path)

    def fail_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "worker failed")

    with pytest.raises(WorkflowError):
        run_workflow(
            ControllerConfig(repo_root=tmp_path, plan_path=source, max_turns=1),
            _make_simple_wf_config(), "simple", config_dir=tmp_path,
            snapshot_config=False, adapter=CodexAdapter(), runner=fail_runner,
        )
    destination = tmp_path / "plans" / "failed" / source.name
    assert destination.read_bytes() == _PLAN
    assert not source.exists()
    record = PlanLifecycle(tmp_path).record_for(destination)
    assert record is not None
    assert record["reason_code"] == "terminal_execution_failure"
    assert record["source_run_id"] is not None


def test_invalid_checkpoint_plan_moves_to_needs_correction(tmp_path: Path) -> None:
    source = _source(tmp_path)
    malformed = _PLAN.replace(b"### [ ] Checkpoint", b"### [x] Checkpoint")
    source.write_bytes(malformed)

    with pytest.raises(WorkflowError):
        run_workflow(
            ControllerConfig(repo_root=tmp_path, plan_path=source, max_turns=1),
            _make_simple_wf_config(), "simple", config_dir=tmp_path,
            snapshot_config=False, adapter=CodexAdapter(),
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
        )
    destination = tmp_path / "plans" / "needs-plan-change" / source.name
    assert destination.read_bytes() == malformed
    assert not source.exists()
