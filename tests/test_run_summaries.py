from __future__ import annotations

import json
from pathlib import Path

import pytest

from aflow.control_plane import (
    InMemoryUnitManager, LaunchManifest, RunRepository, UnitState,
    append_run_event, create_launch_manifest,
    write_launch_phase,
)
from aflow.project_admission import ProjectAdmission, ProjectAdmissionConflict
from aflow.runlog import prune_old_runs


def _managed(root: Path, run_id: str, status: str, *, phase: str = "launch_started") -> Path:
    create_launch_manifest(root, LaunchManifest(
        run_id=run_id, project_root=str(root.resolve()),
        plan_path=str(root / "plan.md"), workflow_name="managed", max_turns=2,
        idempotency_key=run_id, caller_scope="test",
    ))
    write_launch_phase(root, run_id, phase)
    run_dir = root / ".aflow" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({"schema_version": 2, "status": status}))
    return run_dir


@pytest.mark.parametrize("outcome", ("completed", "failed", "interrupted"))
def test_pruned_controller_outcome_remains_inactive(outcome: str, tmp_path: Path) -> None:
    run_dir = _managed(tmp_path, "terminal-run", outcome)

    prune_old_runs(run_dir.parent, keep_runs=0)

    assert not run_dir.exists()
    fresh = RunRepository(tmp_path).get_run_status("terminal-run", include_progress=False)
    assert fresh.status == outcome
    assert fresh.evidence["recorded_status"] == outcome
    assert fresh.evidence["controller_terminal"] is True
    assert fresh.evidence["retained_terminal_summary"] is True
    assert fresh.evidence.get("publication") is None
    assert ProjectAdmission(tmp_path).snapshot().occupied_count == 0
    with pytest.raises(ProjectAdmissionConflict):
        ProjectAdmission(tmp_path).acquire("resumed-run", source_run_id="terminal-run")


def test_owner_stop_retains_explicit_outcome(tmp_path: Path) -> None:
    run_dir = _managed(tmp_path, "stopped-run", "running", phase="owner_stopped")
    append_run_event(run_dir, "owner_stopped", {
        "source": "daemon", "unit_name": "aflow-run-stopped-run.service",
    })
    prune_old_runs(run_dir.parent, keep_runs=0)
    assert not run_dir.exists()
    status = RunRepository(tmp_path).get_run_status("stopped-run", include_progress=False)
    assert status.status == status.launch_phase == "owner_stopped"
    assert status.evidence["controller_terminal"] is False
    assert ProjectAdmission(tmp_path).snapshot().occupied_count == 0


def test_owner_stop_without_event_is_not_pruned(tmp_path: Path) -> None:
    run_dir = _managed(tmp_path, "stopped-run", "running", phase="owner_stopped")
    prune_old_runs(run_dir.parent, keep_runs=0)
    assert run_dir.exists()


def test_fresh_active_unit_overrides_retained_terminal_proof(tmp_path: Path) -> None:
    run_dir = _managed(tmp_path, "terminal-run", "failed")
    prune_old_runs(run_dir.parent, keep_runs=0)
    unit = "aflow-run-terminal-run.service"
    units = InMemoryUnitManager({
        unit: UnitState(name=unit, active_state="active", sub_state="running"),
    })
    assert ProjectAdmission(tmp_path, unit_manager=units).snapshot().occupied_count == 1


def test_active_and_uncertain_runs_survive_retention(tmp_path: Path) -> None:
    active = _managed(tmp_path, "active-run", "running")
    uncertain = _managed(tmp_path, "uncertain-run", "running", phase="completed")
    prune_old_runs(active.parent, keep_runs=0)
    assert active.exists() and uncertain.exists()
    summaries = tmp_path / ".aflow" / "run-summaries"
    assert not summaries.exists() or list(summaries.glob("*.json")) == []


def test_failed_summary_write_keeps_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _managed(tmp_path, "terminal-run", "completed")

    def fail_write(*_args: object) -> None:
        raise OSError("storage unavailable")

    monkeypatch.setattr("aflow.control_plane.run_summaries._write_atomic_bytes", fail_write)
    prune_old_runs(run_dir.parent, keep_runs=0)
    assert run_dir.exists()


def test_symlinked_summary_destination_keeps_source(tmp_path: Path) -> None:
    run_dir = _managed(tmp_path, "terminal-run", "completed")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".aflow" / "run-summaries").symlink_to(outside)
    prune_old_runs(run_dir.parent, keep_runs=0)
    assert run_dir.exists()
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("field,value", (
    ("project_root", "/another-project"),
    ("run_id", "another-run"),
    ("intended_unit", "aflow-run-another-run.service"),
    ("schema_version", 2),
    ("status", "running"),
))
def test_invalid_summary_cannot_prove_terminal(
    field: str, value: object, tmp_path: Path,
) -> None:
    run_dir = _managed(tmp_path, "terminal-run", "completed")
    prune_old_runs(run_dir.parent, keep_runs=0)
    summary = tmp_path / ".aflow" / "run-summaries" / "terminal-run.json"
    payload = json.loads(summary.read_text())
    payload[field] = value
    summary.write_text(json.dumps(payload))
    status = RunRepository(tmp_path).get_run_status("terminal-run", include_progress=False)
    assert status.status == "needs_attention"
    assert status.evidence.get("retained_terminal_summary") is None
    assert ProjectAdmission(tmp_path).snapshot().occupied_count == 1


def test_symlinked_and_malformed_summary_rejected(tmp_path: Path) -> None:
    run_dir = _managed(tmp_path, "terminal-run", "completed")
    prune_old_runs(run_dir.parent, keep_runs=0)
    summary = tmp_path / ".aflow" / "run-summaries" / "terminal-run.json"
    summary.write_text("{")
    assert RunRepository(tmp_path).get_run_status("terminal-run", include_progress=False).status == "needs_attention"
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"status": "completed"}))
    summary.unlink()
    summary.symlink_to(outside)
    assert RunRepository(tmp_path).get_run_status("terminal-run", include_progress=False).status == "needs_attention"
    summary.unlink()
    summary.write_text("x" * 2049)
    assert RunRepository(tmp_path).get_run_status("terminal-run", include_progress=False).status == "needs_attention"


def test_old_launch_marker_without_summary_remains_uncertain(tmp_path: Path) -> None:
    create_launch_manifest(tmp_path, LaunchManifest(
        run_id="old-run", project_root=str(tmp_path.resolve()),
        plan_path=str(tmp_path / "plan.md"), workflow_name="managed", max_turns=2,
    ))
    write_launch_phase(tmp_path, "old-run", "completed")
    status = RunRepository(tmp_path).get_run_status("old-run", include_progress=False)
    assert status.status == "needs_attention"
    assert ProjectAdmission(tmp_path).snapshot().occupied_count == 1


def test_old_owner_stop_marker_without_summary_remains_uncertain(tmp_path: Path) -> None:
    create_launch_manifest(tmp_path, LaunchManifest(
        run_id="old-run", project_root=str(tmp_path.resolve()),
        plan_path=str(tmp_path / "plan.md"), workflow_name="managed", max_turns=2,
    ))
    write_launch_phase(tmp_path, "old-run", "owner_stopped")
    assert RunRepository(tmp_path).get_run_status("old-run", include_progress=False).status == "needs_attention"
    assert ProjectAdmission(tmp_path).snapshot().occupied_count == 1
