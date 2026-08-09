from __future__ import annotations

import json
from pathlib import Path
import subprocess

from aflow.control_plane import (
    InMemoryUnitManager,
    LaunchManifest,
    ReconciliationService,
    RunRepository,
    SystemdUnitManager,
    UnitState,
    create_launch_manifest,
    read_events,
    write_launch_phase,
)


def _manifest(run_id: str) -> LaunchManifest:
    return LaunchManifest(
        run_id=run_id,
        project_root="/project",
        plan_path="/project/plan.md",
        workflow_name="managed",
        max_turns=5,
        idempotency_key="request-1",
        caller_scope="caller:project",
    )


def _owned_running(root: Path, run_id: str = "owned-run", *, status: str = "running") -> Path:
    create_launch_manifest(root, _manifest(run_id))
    run_dir = root / ".aflow" / "runs" / run_id
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"status": status}))
    write_launch_phase(root, run_id, "unit_started")
    return run_dir


def test_reconciliation_classifies_active_missing_and_ambiguous_units_without_starting(tmp_path: Path) -> None:
    _owned_running(tmp_path, "active-run")
    _owned_running(tmp_path, "missing-run")
    _owned_running(tmp_path, "dead-run")
    _owned_running(tmp_path, "rebooted-run")
    _owned_running(tmp_path, "completed-run", status="completed")
    _owned_running(tmp_path, "ambiguous-run")
    expected = "aflow-run-active-run.service"
    units = InMemoryUnitManager(
        {
            expected: UnitState(name=expected, active_state="active", sub_state="running"),
            "aflow-run-ambiguous-run.service": UnitState(
                name="another.service", active_state="active", sub_state="running"
            ),
            "aflow-run-dead-run.service": UnitState(
                name="aflow-run-dead-run.service", active_state="failed", sub_state="failed"
            ),
        }
    )
    service = ReconciliationService(RunRepository(tmp_path), units)

    assert service.reconcile_run("active-run").status == "running"
    assert service.reconcile_run("missing-run").status == "interrupted"
    assert service.reconcile_run("dead-run").status == "interrupted"
    assert service.reconcile_run("rebooted-run").status == "interrupted"
    assert service.reconcile_run("completed-run").status == "completed"
    assert service.reconcile_run("ambiguous-run").status == "needs_attention"
    assert RunRepository(tmp_path).get_run_status("missing-run").status == "interrupted"
    assert units.start_calls == []


def test_reconciliation_classifies_crash_windows_and_never_mutates_legacy_runs(tmp_path: Path) -> None:
    create_launch_manifest(tmp_path, _manifest("manifest-only"))
    (tmp_path / ".aflow" / "launches" / "manifest-only.state.json").unlink()
    create_launch_manifest(tmp_path, _manifest("launch-requested"))
    write_launch_phase(tmp_path, "launch-requested", "launch_requested")
    legacy = tmp_path / ".aflow" / "runs" / "legacy-run"
    legacy.mkdir(parents=True)
    legacy_file = legacy / "run.json"
    legacy_file.write_text('{"status":"running"}\n')
    before = legacy_file.read_bytes()
    service = ReconciliationService(RunRepository(tmp_path), InMemoryUnitManager())

    assert service.reconcile_run("manifest-only").status == "manifest_only"
    assert service.reconcile_run("launch-requested").status == "needs_attention"
    legacy_result = service.reconcile_run("legacy-run")

    assert legacy_result.ownership == "legacy"
    assert legacy_result.status == "interrupted"
    assert legacy_file.read_bytes() == before
    assert not (legacy / "events.jsonl").exists()


def test_startup_and_periodic_reconciliation_are_idempotent_observations(tmp_path: Path) -> None:
    run_dir = _owned_running(tmp_path)
    unit_name = "aflow-run-owned-run.service"
    service = ReconciliationService(
        RunRepository(tmp_path),
        InMemoryUnitManager(
            {unit_name: UnitState(name=unit_name, active_state="active", sub_state="running")}
        ),
    )

    assert service.reconcile_startup()[0].status == "running"
    after_startup = read_events(run_dir)
    assert service.reconcile_periodic()[0].status == "running"

    assert read_events(run_dir) == after_startup


def test_systemd_adapter_uses_bounded_argv_without_a_shell() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            "Id=aflow-run-owned-run.service\nActiveState=active\nSubState=running\nMainPID=42\n",
            "",
        )

    state = SystemdUnitManager(runner=runner).get("aflow-run-owned-run.service")

    assert state is not None and state.main_pid == 42
    assert calls == [
        (
            "systemctl",
            "show",
            "aflow-run-owned-run.service",
            "--no-page",
            "--property=Id,ActiveState,SubState,InvocationID,Result,MainPID",
        )
    ]
