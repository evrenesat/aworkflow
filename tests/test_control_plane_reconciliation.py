from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from aflow.control_plane import (
    InMemoryUnitManager,
    LaunchManifest,
    ReconciliationResult,
    ReconciliationService,
    RunPage,
    RunRepository,
    RunStatus,
    SystemdUnitManager,
    UnitState,
    create_launch_manifest,
    read_events,
    write_launch_phase,
)
from aflow.control_plane.run_history import RunHistory


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
    assert service.reconcile_run("missing-run").status == "needs_attention"
    assert service.reconcile_run("dead-run").status == "needs_attention"
    assert service.reconcile_run("rebooted-run").status == "needs_attention"
    assert service.reconcile_run("completed-run").status == "completed"
    assert service.reconcile_run("ambiguous-run").status == "needs_attention"
    assert RunRepository(tmp_path).get_run_status("missing-run").status == "needs_attention"
    assert units.start_calls == []


@pytest.mark.parametrize("persist", [False, True])
def test_reconcile_all_reads_each_inclusive_status_once_without_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persist: bool,
) -> None:
    _owned_running(tmp_path, "active-run")
    _owned_running(tmp_path, "deleted-run")
    _owned_running(tmp_path, "missing-run")
    _owned_running(tmp_path, "terminal-run", status="completed")
    legacy = tmp_path / ".aflow" / "runs" / "legacy-run"
    legacy.mkdir(parents=True)
    (legacy / "run.json").write_text('{"status":"running"}\n', encoding="utf-8")
    RunHistory(RunRepository(tmp_path)).mutate(
        "deleted-run",
        state="deleted",
        expected_revision=0,
        idempotency_key="delete-deleted-run",
    )

    class PagedRepository(RunRepository):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.list_calls: list[tuple[int, str | None, bool]] = []
            self.status_reads: list[tuple[str, bool]] = []

        def list_runs(
            self,
            *,
            limit: int = 100,
            cursor: str | None = None,
            include_progress: bool = True,
        ) -> RunPage:
            self.list_calls.append((limit, cursor, include_progress))
            return super().list_runs(
                limit=min(limit, 2),
                cursor=cursor,
                include_progress=include_progress,
            )

        def get_run_status(
            self, run_id: str, *, include_progress: bool = True
        ) -> RunStatus:
            self.status_reads.append((run_id, include_progress))
            return super().get_run_status(run_id, include_progress=include_progress)

    repository = PagedRepository(tmp_path)

    def fail_projection(*args, **kwargs):
        raise AssertionError("reconciliation must not project progress")

    monkeypatch.setattr(repository, "_with_progress", fail_projection)
    active_name = "aflow-run-active-run.service"
    units = InMemoryUnitManager(
        {
            active_name: UnitState(
                name=active_name,
                active_state="active",
                sub_state="running",
            )
        }
    )

    results = ReconciliationService(repository, units).reconcile_all(persist=persist)

    assert results == (
        ReconciliationResult(
            "active-run",
            "running",
            "exact workflow unit is active",
            unit_name=active_name,
            observed_unit_state="active",
        ),
        ReconciliationResult(
            "deleted-run",
            "needs_attention",
            "launch or unit evidence is inactive or ambiguous",
            unit_name="aflow-run-deleted-run.service",
            observed_unit_state="missing",
        ),
        ReconciliationResult(
            "legacy-run",
            "needs_attention",
            "legacy run has no control-plane ownership evidence",
            ownership="legacy",
        ),
        ReconciliationResult(
            "missing-run",
            "needs_attention",
            "launch or unit evidence is inactive or ambiguous",
            unit_name="aflow-run-missing-run.service",
            observed_unit_state="missing",
        ),
        ReconciliationResult(
            "terminal-run",
            "completed",
            "durable controller terminal state retained",
            unit_name="aflow-run-terminal-run.service",
        ),
    )
    assert repository.status_reads == [
        (run_id, False)
        for run_id in ("active-run", "deleted-run", "legacy-run", "missing-run", "terminal-run")
    ]
    assert repository.list_calls == [
        (1_000, None, False),
        (1_000, "deleted-run", False),
        (1_000, "missing-run", False),
    ]
    assert units.start_calls == []
    assert units.stop_calls == []


def test_reconcile_run_uses_raw_status_without_changing_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owned_running(tmp_path)
    repository = RunRepository(tmp_path)
    original_get_status = repository.get_run_status
    reads: list[tuple[str, bool]] = []

    def counted_status(run_id: str, *, include_progress: bool = True) -> RunStatus:
        reads.append((run_id, include_progress))
        return original_get_status(run_id, include_progress=include_progress)

    def fail_projection(*args, **kwargs):
        raise AssertionError("reconciliation must not project progress")

    monkeypatch.setattr(repository, "get_run_status", counted_status)
    monkeypatch.setattr(repository, "_with_progress", fail_projection)
    unit_name = "aflow-run-owned-run.service"
    units = InMemoryUnitManager(
        {
            unit_name: UnitState(
                name=unit_name,
                active_state="active",
                sub_state="running",
            )
        }
    )

    result = ReconciliationService(repository, units).reconcile_run("owned-run")

    assert reads == [("owned-run", False)]
    assert result == ReconciliationResult(
        "owned-run",
        "running",
        "exact workflow unit is active",
        unit_name=unit_name,
        observed_unit_state="active",
    )


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

    assert service.reconcile_run("manifest-only").status == "needs_attention"
    assert service.reconcile_run("launch-requested").status == "needs_attention"
    legacy_result = service.reconcile_run("legacy-run")

    assert legacy_result.ownership == "legacy"
    assert legacy_result.status == "needs_attention"
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


def test_reconcile_persistence_modes_preserve_bytes_and_deduplicate(tmp_path: Path) -> None:
    active_dir = _owned_running(tmp_path, "active-run")
    terminal_dir = _owned_running(tmp_path, "terminal-run", status="completed")
    legacy_dir = tmp_path / ".aflow" / "runs" / "legacy-run"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "run.json").write_text('{"status":"running"}\n', encoding="utf-8")
    active_name = "aflow-run-active-run.service"
    service = ReconciliationService(
        RunRepository(tmp_path),
        InMemoryUnitManager(
            {
                active_name: UnitState(
                    name=active_name,
                    active_state="active",
                    sub_state="running",
                )
            }
        ),
    )

    def snapshot() -> dict[Path, bytes]:
        return {
            path.relative_to(tmp_path): path.read_bytes()
            for path in tmp_path.rglob("*")
            if path.is_file()
        }

    expected = (
        ReconciliationResult(
            "active-run",
            "running",
            "exact workflow unit is active",
            unit_name=active_name,
            observed_unit_state="active",
        ),
        ReconciliationResult(
            "legacy-run",
            "needs_attention",
            "legacy run has no control-plane ownership evidence",
            ownership="legacy",
        ),
        ReconciliationResult(
            "terminal-run",
            "completed",
            "durable controller terminal state retained",
            unit_name="aflow-run-terminal-run.service",
        ),
    )
    before = snapshot()

    assert service.reconcile_all(persist=False) == expected
    assert snapshot() == before

    assert service.reconcile_all(persist=True) == expected
    after_first_persist = snapshot()
    changed_files = {
        path
        for path in set(before) | set(after_first_persist)
        if before.get(path) != after_first_persist.get(path)
    }
    assert changed_files == {
        Path(".aflow/runs/active-run/.events.lock"),
        Path(".aflow/runs/active-run/events.jsonl"),
        Path(".aflow/runs/terminal-run/.events.lock"),
        Path(".aflow/runs/terminal-run/events.jsonl"),
    }
    for run_dir, result in (
        (active_dir, expected[0]),
        (terminal_dir, expected[2]),
    ):
        events = read_events(run_dir)
        assert len(events) == 1
        assert events[0].event_type == "reconciled"
        assert events[0].data == {
            "status": result.status,
            "reason": result.reason,
            "unit_name": result.unit_name,
            "observed_unit_state": result.observed_unit_state,
        }
    assert read_events(legacy_dir) == []

    persisted_events = {
        run_dir: read_events(run_dir)
        for run_dir in (active_dir, terminal_dir, legacy_dir)
    }
    assert service.reconcile_all(persist=True) == expected
    assert snapshot() == after_first_persist
    assert {
        run_dir: read_events(run_dir)
        for run_dir in (active_dir, terminal_dir, legacy_dir)
    } == persisted_events
    assert service._units.start_calls == []
    assert service._units.stop_calls == []


def test_collected_unit_requires_durable_aflow_terminal_state_before_completion(tmp_path: Path) -> None:
    create_launch_manifest(tmp_path, _manifest("collected-run"))
    write_launch_phase(tmp_path, "collected-run", "completed")
    service = ReconciliationService(RunRepository(tmp_path), InMemoryUnitManager())

    assert service.reconcile_run("collected-run").status == "needs_attention"

    run_dir = tmp_path / ".aflow" / "runs" / "collected-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text('{"status":"completed"}')
    assert service.reconcile_run("collected-run").status == "completed"


def test_durable_terminal_state_supersedes_stale_running_reconciliation(
    tmp_path: Path,
) -> None:
    run_dir = _owned_running(tmp_path)
    unit_name = "aflow-run-owned-run.service"
    active = ReconciliationService(
        RunRepository(tmp_path),
        InMemoryUnitManager(
            {unit_name: UnitState(name=unit_name, active_state="active", sub_state="running")}
        ),
    )
    assert active.reconcile_run("owned-run").status == "running"
    assert read_events(run_dir)[-1].data["status"] == "running"

    (run_dir / "run.json").write_text('{"status":"completed"}')
    write_launch_phase(tmp_path, "owned-run", "completed")
    after_restart = ReconciliationService(
        RunRepository(tmp_path),
        InMemoryUnitManager(),
    )

    assert after_restart.reconcile_run("owned-run").status == "completed"
    assert RunRepository(tmp_path).get_run_status("owned-run").status == "completed"


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


def test_active_unit_retains_waiting_execution_status(tmp_path):
    run_dir = _owned_running(tmp_path, status="waiting_for_valid_override")
    from aflow.control_plane import append_run_event
    append_run_event(run_dir, "reconciled", {"status": "running", "reason": "exact workflow unit is active"})
    name = "aflow-run-owned-run.service"
    units = InMemoryUnitManager({name: UnitState(name=name, active_state="active", sub_state="running")})
    repository = RunRepository(tmp_path)
    result = ReconciliationService(repository, units).reconcile_run("owned-run")
    assert result.status == "waiting_for_valid_override"
    assert repository.get_run_status("owned-run").status == "waiting_for_valid_override"
    assert units.start_calls == []


def test_reconcile_observes_changed_units_and_terminal_evidence_on_next_call(
    tmp_path: Path,
) -> None:
    _owned_running(tmp_path, "changed-run")
    _owned_running(tmp_path, "waiting-run", status="waiting_for_valid_override")
    terminal_dir = _owned_running(tmp_path, "terminal-run")
    changed_name = "aflow-run-changed-run.service"
    waiting_name = "aflow-run-waiting-run.service"
    units = InMemoryUnitManager(
        {
            changed_name: UnitState(
                name=changed_name,
                active_state="active",
                sub_state="running",
            ),
            waiting_name: UnitState(
                name=waiting_name,
                active_state="active",
                sub_state="running",
            ),
        }
    )
    service = ReconciliationService(RunRepository(tmp_path), units)

    assert service.reconcile_run("changed-run", persist=False) == ReconciliationResult(
        "changed-run",
        "running",
        "exact workflow unit is active",
        unit_name=changed_name,
        observed_unit_state="active",
    )
    units.units[changed_name] = UnitState(
        name=changed_name,
        active_state="failed",
        sub_state="failed",
    )
    assert service.reconcile_run("changed-run", persist=False) == ReconciliationResult(
        "changed-run",
        "needs_attention",
        "launch or unit evidence is inactive or ambiguous",
        unit_name=changed_name,
        observed_unit_state="failed",
    )

    assert service.reconcile_run("waiting-run", persist=False) == ReconciliationResult(
        "waiting-run",
        "waiting_for_valid_override",
        "exact workflow unit is active",
        unit_name=waiting_name,
        observed_unit_state="active",
    )

    assert service.reconcile_run("terminal-run", persist=False) == ReconciliationResult(
        "terminal-run",
        "needs_attention",
        "launch or unit evidence is inactive or ambiguous",
        unit_name="aflow-run-terminal-run.service",
        observed_unit_state="missing",
    )
    (terminal_dir / "run.json").write_text('{"status":"completed"}', encoding="utf-8")
    write_launch_phase(tmp_path, "terminal-run", "completed")
    assert service.reconcile_run("terminal-run", persist=False) == ReconciliationResult(
        "terminal-run",
        "completed",
        "durable controller terminal state retained",
        unit_name="aflow-run-terminal-run.service",
    )
    assert units.start_calls == []
    assert units.stop_calls == []
