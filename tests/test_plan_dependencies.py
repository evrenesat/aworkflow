from __future__ import annotations

import json
from pathlib import Path

import pytest

from aflow.plan_backups import ensure_plan_identity, move_plan_identity
from aflow.plan_dependencies import PlanDependencies, PlanDependencyError, parse_sequence_name
from aflow.project_admission import ProjectAdmission, ProjectPlanDependencyBlocked


def _plan(root: Path, location: str, name: str) -> Path:
    path = root / "plans" / location / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Plan\n", encoding="utf-8")
    return path


def _published_delivery(root: Path, name: str, *, status: str = "published") -> None:
    run = root / ".aflow" / "runs" / "delivered-run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "publication.json").write_text(json.dumps({
        "status": status, "commit": "a" * 40, "source_commit": "b" * 40,
        "remote": "origin", "branch": "main",
        "plan_lifecycle": {
            "phase": "committed", "complete": True,
            "source": f"plans/in-progress/{name}",
            "destination": f"plans/done/{name}",
        },
    }), encoding="utf-8")


def test_exact_sequence_filename_contract() -> None:
    assert parse_sequence_name("feature_P01_intro.md") == ("feature", 1)
    assert parse_sequence_name("feature_P003_finish.md") == ("feature", 3)
    assert parse_sequence_name("unrelated.md") is None
    for name in (
        "feature_P00_intro.md", "feature_P1_intro.md", "feature_P01.md",
        "feature_P01_intro_P02_more.md", "_P01_intro.md",
    ):
        with pytest.raises(PlanDependencyError, match="correction"):
            parse_sequence_name(name)


def test_known_lower_member_blocks_only_its_series_and_gap_is_allowed(tmp_path: Path) -> None:
    first = _plan(tmp_path, "todo", "feature_P01_intro.md")
    later = _plan(tmp_path, "in-progress", "feature_P03_finish.md")
    unrelated = _plan(tmp_path, "in-progress", "other_P07_work.md")
    admission = ProjectAdmission(tmp_path)
    with pytest.raises(ProjectPlanDependencyBlocked, match="predecessor"):
        admission.acquire("later", plan_path=later, idempotency_key="later")
    assert admission.snapshot().occupied_count == 0
    admission.acquire("other", plan_path=unrelated, idempotency_key="other")
    assert admission.snapshot().occupied_count == 1
    first.unlink()
    with pytest.raises(ProjectPlanDependencyBlocked, match="predecessor"):
        admission.acquire("later", plan_path=later, idempotency_key="later")


def test_done_name_without_receipt_does_not_release_successor(tmp_path: Path) -> None:
    first = _plan(tmp_path, "in-progress", "feature_P01_intro.md")
    later = _plan(tmp_path, "in-progress", "feature_P03_finish.md")
    admission = ProjectAdmission(tmp_path)
    with pytest.raises(ProjectPlanDependencyBlocked):
        admission.acquire("later", plan_path=later)
    dependencies = PlanDependencies(tmp_path)
    assert dependencies.blocking_predecessor("plans/in-progress/feature_P03_finish.md") == first.name
    done = tmp_path / "plans" / "done" / first.name
    done.parent.mkdir()
    first.rename(done)
    assert move_plan_identity(tmp_path, source_plan_path=first, destination_plan_path=done)
    with pytest.raises(ProjectPlanDependencyBlocked):
        admission.acquire("later", plan_path=later)
    _published_delivery(tmp_path, first.name, status="pending")
    with pytest.raises(ProjectPlanDependencyBlocked):
        admission.acquire("later", plan_path=later)
    _published_delivery(tmp_path, first.name)
    inventory_before = dependencies.path.read_bytes()
    assert dependencies.blocking_predecessor("plans/in-progress/feature_P03_finish.md") is None
    assert dependencies.path.read_bytes() == inventory_before
    admitted = admission.acquire("later", plan_path=later, idempotency_key="later")
    assert admitted.run_id == "later"
    admission.release(admitted.run_id, admitted.nonce)
    (tmp_path / ".aflow" / "runs" / "delivered-run" / "publication.json").unlink()
    assert dependencies.blocking_predecessor("plans/in-progress/feature_P03_finish.md") == first.name
    with pytest.raises(ProjectPlanDependencyBlocked):
        admission.acquire("another-later", plan_path=later, idempotency_key="another")


def test_retained_identity_history_blocks_deleted_predecessor(tmp_path: Path) -> None:
    first = _plan(tmp_path, "failed", "feature_P01_intro.md")
    ensure_plan_identity(tmp_path, first)
    first.unlink()
    later = _plan(tmp_path, "in-progress", "feature_P03_finish.md")
    with pytest.raises(ProjectPlanDependencyBlocked, match="predecessor"):
        ProjectAdmission(tmp_path).acquire("later", plan_path=later)


def test_failed_and_malformed_and_duplicate_members_hold_only_their_series(tmp_path: Path) -> None:
    _plan(tmp_path, "failed", "feature_P01_intro.md")
    later = _plan(tmp_path, "in-progress", "feature_P03_finish.md")
    admission = ProjectAdmission(tmp_path)
    with pytest.raises(ProjectPlanDependencyBlocked):
        admission.acquire("later", plan_path=later)
    _plan(tmp_path, "needs-plan-change", "feature_P1_invalid.md")
    with pytest.raises(ProjectPlanDependencyBlocked, match="correction"):
        admission.acquire("later", plan_path=later)
    another = _plan(tmp_path, "todo", "feature_P03_duplicate.md")
    with pytest.raises(ProjectPlanDependencyBlocked, match="correction"):
        admission.acquire("later", plan_path=later)
    another.unlink()
    with pytest.raises(ProjectPlanDependencyBlocked, match="correction"):
        admission.acquire("later", plan_path=later)


def test_new_lower_member_does_not_interrupt_reserved_successor(tmp_path: Path) -> None:
    later = _plan(tmp_path, "in-progress", "feature_P03_finish.md")
    admission = ProjectAdmission(tmp_path)
    first = admission.acquire("later", plan_path=later, idempotency_key="same")
    _plan(tmp_path, "todo", "feature_P01_intro.md")
    assert admission.acquire("later", plan_path=later, idempotency_key="same") == first
