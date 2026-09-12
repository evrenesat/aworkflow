"""Tests for the portable persistent unit adapter.

Uses a fake ``aflow`` executable whose ``ui-worker`` dispatch mimics the real
wrapper contract (receipt writes, child process group), so the receipts,
liveness rules, exact-stop behavior, and shutdown no-op are exercised without
spawning real workflow controllers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from aflow.control_plane.persistent_units import (
    PersistentUnitError,
    PersistentUnitManager,
)
from tests._support import _write_workflow_harness_script


@pytest.fixture()
def fake_aflow(tmp_path: Path) -> Path:
    """A fake installed aflow whose ui-worker mirrors the wrapper contract."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "aflow"
    executable.write_text(
        textwrap.dedent(
            """
            #!{python}
            from __future__ import annotations
            import argparse, json, os, subprocess, sys, time
            from pathlib import Path
            """.format(python=sys.executable)
        ).lstrip("\n")
        + textwrap.dedent(
            """
            argv = sys.argv[1:]
            if argv and argv[0] == "ui-worker":
                argv = argv[1:]
            if "--" in argv:
                split = argv.index("--")
                flags, worker_argv = argv[:split], argv[split + 1:]
            else:
                flags, worker_argv = argv, []
            parser = argparse.ArgumentParser()
            parser.add_argument("--receipt-dir", type=Path, required=True)
            parser.add_argument("--nonce", required=True)
            args = parser.parse_args(flags)
            args.worker_argv = worker_argv
            receipt_dir = args.receipt_dir
            inner = [a for a in args.worker_argv if a != "--"]

            start = json.loads((receipt_dir / "start.json").read_text())
            if start.get("nonce") != args.nonce:
                sys.exit(3)

            def write(name, payload):
                temp = receipt_dir / f".{name}.tmp"
                temp.write_text(json.dumps(payload, indent=2))
                os.replace(temp, receipt_dir / name)

            def _birth(pid):
                from aflow.process_identity import process_birth_identity
                return process_birth_identity(pid)

            if inner and inner[0] == "FAIL":
                write("error.json", {"schema": 1, "nonce": args.nonce, "error": inner[1]})
                sys.exit(127)

            if inner and inner[0] == "FAIL_UNTIL_RELEASE":
                ready_path = Path(inner[1])
                release_path = Path(inner[2])
                write("error.json", {"schema": 1, "nonce": args.nonce, "error": inner[3]})
                ready_path.touch()
                while not release_path.exists():
                    time.sleep(0.05)
                sys.exit(127)

            child = subprocess.Popen(
                inner, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            write("child.json", {
                "schema": 1, "nonce": args.nonce, "pid": child.pid,
                "pgid": child.pid, "process_birth": _birth(child.pid),
            })
            code = child.wait()
            write("exit.json", {
                "schema": 1, "nonce": args.nonce, "returncode": code,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            sys.exit(code if code >= 0 else 128 - code)
            """
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _harness(repo: Path, name: str = "worker") -> Path:
    return _write_workflow_harness_script(repo, name)


def _long_running_child() -> tuple[str, str]:
    """A workflow stand-in that stays alive until explicitly stopped."""
    return (sys.executable, "-c", "import time; time.sleep(120)")


def _spawn_env(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the deterministic fake harness its required environment."""
    monkeypatch.setenv("AFLOW_TEST_SCENARIO", "noop")
    monkeypatch.setenv("AFLOW_TEST_PLAN_PATH", str(repo / "plan.md"))
    monkeypatch.setenv("AFLOW_TEST_COUNT_FILE", str(repo / "count"))
    (repo / "plan.md").write_text("# Plan\n", encoding="utf-8")


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _process_owned(pid: int, birth: str) -> bool:
    from aflow.process_identity import process_birth_identity

    return process_birth_identity(pid) == birth


def _unit_is_inactive(manager: PersistentUnitManager, name: str) -> bool:
    state = manager.get(name)
    return state is not None and not state.is_active


def _write_terminal_receipt(
    root: Path,
    unit: str,
    *,
    nonce: str,
    returncode: int = 0,
) -> Path:
    run_id = unit[len("aflow-run-") : -len(".service")]
    receipt_dir = root / ".aflow" / "runs" / run_id / "units"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    (receipt_dir / "start.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "run_id": run_id,
                "unit": unit,
                "nonce": nonce,
                "started_at": "2026-09-12T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (receipt_dir / "exit.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "nonce": nonce,
                "returncode": returncode,
                "at": "2026-09-12T00:00:01Z",
            }
        ),
        encoding="utf-8",
    )
    return receipt_dir


def test_start_writes_receipts_and_reports_active(tmp_path, fake_aflow, repo, monkeypatch):
    _spawn_env(repo, monkeypatch)
    _harness(repo)
    manager = PersistentUnitManager(executable=fake_aflow)
    state = manager.start(
        "aflow-run-20260908t000000z-0000000a.service",
        (str(repo / "bin" / "worker"),),
        cwd=repo,
    )
    assert state.is_active
    receipts = repo / ".aflow" / "runs" / "20260908t000000z-0000000a" / "units"
    start = json.loads((receipts / "start.json").read_text())
    assert start["unit"] == "aflow-run-20260908t000000z-0000000a.service"
    assert start["nonce"]
    child = json.loads((receipts / "child.json").read_text())
    assert child["nonce"] == start["nonce"]
    assert child["pid"] == child["pgid"]
    manager.stop("aflow-run-20260908t000000z-0000000a.service")


def test_get_observes_a_detached_run_after_manager_loss(tmp_path, fake_aflow, repo, monkeypatch):
    """A returning UI process rediscovers receipts and observes the worker."""
    unit = "aflow-run-20260908t000000z-0000000b.service"
    first = PersistentUnitManager(executable=fake_aflow)
    first.start(unit, _long_running_child(), cwd=repo)

    # The UI restarted: a fresh manager with only the projects root.
    second = PersistentUnitManager(executable=fake_aflow, projects_root=repo.parent)
    observed = second.get(unit)
    assert observed is not None and observed.is_active

    # Explicit stop works across the restart and writes the terminal receipt.
    terminal = second.stop(unit)
    assert terminal is not None and not terminal.is_active
    assert second.get(unit).result == "stopped"  # type: ignore[union-attr]
    assert not _wait_until(lambda: False, timeout=0.01)


def test_worker_exit_is_reported_as_a_terminal_result(tmp_path, fake_aflow, repo, monkeypatch):
    """A workflow finishing while the UI is absent shows its real result."""
    _spawn_env(repo, monkeypatch)
    _harness(repo)
    manager = PersistentUnitManager(executable=fake_aflow)
    unit = "aflow-run-20260908t000000z-0000000c.service"
    manager.start(unit, (str(repo / "bin" / "worker"),), cwd=repo)
    assert _wait_until(lambda: manager.get(unit) is not None)
    terminal = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = manager.get(unit)
        if state is not None and not state.is_active and state.result == "success":
            terminal = state
            break
        time.sleep(0.05)
    assert terminal is not None


def test_startup_failure_yields_actionable_error_without_active_unit(tmp_path, fake_aflow, repo):
    _harness(repo)
    manager = PersistentUnitManager(executable=fake_aflow)
    unit = "aflow-run-20260908t000000z-0000000d.service"
    with pytest.raises(PersistentUnitError, match="boom"):
        manager.start(unit, ("FAIL", "boom"), cwd=repo)
    assert _wait_until(lambda: _unit_is_inactive(manager, unit))
    state = manager.get(unit)
    assert state is not None and not state.is_active


def test_startup_failure_observes_live_wrapper_until_release(tmp_path, fake_aflow, repo):
    _harness(repo)
    manager = PersistentUnitManager(executable=fake_aflow)
    unit = "aflow-run-20260908t000000z-0000000d-barrier.service"
    receipts = repo / ".aflow" / "runs" / "20260908t000000z-0000000d-barrier" / "units"
    ready = repo / "startup-error-ready"
    release = repo / "startup-error-release"
    wrapper_pid = None
    wrapper_birth = None
    try:
        with pytest.raises(PersistentUnitError, match="boom"):
            manager.start(
                unit,
                ("FAIL_UNTIL_RELEASE", str(ready), str(release), "boom"),
                cwd=repo,
            )
        start = json.loads((receipts / "start.json").read_text())
        wrapper_pid = start["wrapper_pid"]
        wrapper_birth = start["wrapper_birth"]
        assert _wait_until(ready.exists)
        assert _process_owned(wrapper_pid, wrapper_birth)

        held = manager.get(unit)
        assert held is not None and held.is_active
        assert held.sub_state == "start-post"

        release.touch()
        assert _wait_until(lambda: _unit_is_inactive(manager, unit))
        state = manager.get(unit)
        assert state is not None and not state.is_active
    finally:
        release.touch()
        if isinstance(wrapper_pid, int) and isinstance(wrapper_birth, str):
            if not _wait_until(lambda: not _process_owned(wrapper_pid, wrapper_birth)):
                if _process_owned(wrapper_pid, wrapper_birth):
                    try:
                        os.kill(wrapper_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                assert _wait_until(lambda: not _process_owned(wrapper_pid, wrapper_birth))


def test_duplicate_start_is_refused(tmp_path, fake_aflow, repo, monkeypatch):
    _spawn_env(repo, monkeypatch)
    _harness(repo)
    manager = PersistentUnitManager(executable=fake_aflow)
    unit = "aflow-run-20260908t000000z-0000000e.service"
    manager.start(unit, (str(repo / "bin" / "worker"),), cwd=repo)
    with pytest.raises(PersistentUnitError, match="launch claim"):
        manager.start(unit, (str(repo / "bin" / "worker"),), cwd=repo)
    manager.stop(unit)


def test_stop_signals_only_the_owned_process_group(tmp_path, fake_aflow, repo, monkeypatch):
    """The stop path signals the recorded child group, not unrelated processes."""
    _spawn_env(repo, monkeypatch)
    _harness(repo)
    manager = PersistentUnitManager(executable=fake_aflow)
    unit = "aflow-run-20260908t000000z-0000000f.service"
    _state = manager.start(unit, (str(repo / "bin" / "worker"),), cwd=repo)
    receipts = repo / ".aflow" / "runs" / "20260908t000000z-0000000f" / "units"
    child = json.loads((receipts / "child.json").read_text())

    # A unrelated decoy process is never signalled.
    decoy = subprocess.Popen(
        ["sleep", "30"], start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        terminal = manager.stop(unit)
        assert terminal is not None and not terminal.is_active
        assert _wait_until(lambda: _process_dead(child["pid"]))
        assert decoy.poll() is None  # untouched
    finally:
        decoy.kill()
        decoy.wait()


def _process_dead(pid: int) -> bool:
    from aflow.process_identity import process_birth_identity

    return process_birth_identity(pid) is None


def test_shutdown_is_a_documented_no_op(tmp_path, fake_aflow, repo, monkeypatch):
    manager = PersistentUnitManager(executable=fake_aflow)
    unit = "aflow-run-20260908t000000z-00000010.service"
    state = manager.start(unit, _long_running_child(), cwd=repo)
    assert state.is_active
    # UI shutdown drains nothing: the workflow survives.
    assert manager.shutdown() == ()
    assert manager.get(unit) is not None and manager.get(unit).is_active  # type: ignore[union-attr]
    # Reattachment via a fresh manager proves the workflow outlived it.
    fresh = PersistentUnitManager(executable=fake_aflow, projects_root=repo.parent)
    observed = fresh.get(unit)
    assert observed is not None and observed.is_active
    fresh.stop(unit)


def test_unit_name_and_argv_validation(tmp_path, fake_aflow, repo):
    manager = PersistentUnitManager(executable=fake_aflow)
    with pytest.raises(ValueError, match="aflow-run"):
        manager.start("not-a-unit.service", ("x",), cwd=repo)
    with pytest.raises(ValueError, match="argv"):
        manager.start("aflow-run-20260908t000000z-00000011.service", (), cwd=repo)
    with pytest.raises(ValueError, match="working directory"):
        manager.start(
            "aflow-run-20260908t000000z-00000012.service",
            ("x",),
            cwd=tmp_path / "missing",
        )


def test_bound_unit_lookup_does_not_scan_sibling_projects(
    tmp_path: Path, fake_aflow: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = tmp_path / "primary"
    sibling = tmp_path / "sibling"
    primary.mkdir()
    sibling.mkdir()
    for index in range(40):
        (sibling / f"unrelated-{index:02d}").mkdir()
    manager = PersistentUnitManager(executable=fake_aflow, projects_root=tmp_path)
    manager.bind_project_root(primary)
    unit = "aflow-run-20260912t000000z-00000020.service"

    def fail_enumeration(*_args, **_kwargs):
        pytest.fail("bound receipt lookup enumerated a project root")

    monkeypatch.setattr(Path, "iterdir", fail_enumeration)
    for _ in range(4):
        assert manager.get(unit) is None


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize(
    "name",
    [
        "not-a-unit.service",
        "aflow-run-UPPER.service",
        "aflow-run-a..service",
    ],
)
def test_bound_unit_lookup_rejects_malformed_names(
    bound: bool,
    name: str,
    tmp_path: Path,
    fake_aflow: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    manager = PersistentUnitManager(executable=fake_aflow, projects_root=tmp_path)
    if bound:
        manager.bind_project_root(root)

    def fail_access(*_args, **_kwargs):
        pytest.fail("malformed unit name reached receipt or project discovery")

    monkeypatch.setattr(Path, "iterdir", fail_access)
    with pytest.raises(ValueError, match="aflow-run"):
        manager.get(name)


def test_bound_unit_lookup_observes_receipt_created_after_miss(
    tmp_path: Path, fake_aflow: Path
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    unit = "aflow-run-20260912t000000z-00000021.service"
    manager = PersistentUnitManager(executable=fake_aflow, projects_root=tmp_path)
    manager.bind_project_root(root)

    assert manager.get(unit) is None
    _write_terminal_receipt(root, unit, nonce="late-receipt")

    observed = manager.get(unit)
    assert observed is not None
    assert observed.name == unit
    assert observed.active_state == "inactive"
    assert observed.sub_state == "dead"
    assert observed.result == "success"


def test_bound_unit_lookup_does_not_cross_same_name_projects(
    tmp_path: Path, fake_aflow: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = tmp_path / "primary"
    sibling = tmp_path / "sibling"
    primary.mkdir()
    sibling.mkdir()
    unit = "aflow-run-20260912t000000z-00000022.service"
    _write_terminal_receipt(sibling, unit, nonce="sibling-nonce", returncode=17)
    manager = PersistentUnitManager(executable=fake_aflow, projects_root=tmp_path)
    manager.bind_project_root(primary)
    signalled: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        manager,
        "_signal_group",
        lambda *args: signalled.append(args),
    )

    assert manager.get(unit) is None
    assert manager.stop(unit) is None
    assert signalled == []


def test_bound_unit_manager_rejects_rebind_and_foreign_start_before_side_effects(
    tmp_path: Path, fake_aflow: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = tmp_path / "primary"
    foreign = tmp_path / "foreign"
    primary.mkdir()
    foreign.mkdir()
    manager = PersistentUnitManager(executable=fake_aflow)
    manager.bind_project_root(primary)
    manager.bind_project_root(primary.resolve())
    with pytest.raises(PersistentUnitError, match="different project root"):
        manager.bind_project_root(foreign)
    assert manager._bound_project_root == primary.resolve()
    assert manager._receipt_roots == {}

    mapped = PersistentUnitManager(executable=fake_aflow)
    unit = "aflow-run-20260912t000000z-00000023.service"
    mapped._receipt_roots[unit] = foreign.resolve()
    with pytest.raises(PersistentUnitError, match="outside the project root"):
        mapped.bind_project_root(primary)
    assert mapped._bound_project_root is None
    assert mapped._receipt_roots == {unit: foreign.resolve()}

    receipt_dir = foreign / ".aflow" / "runs" / "20260912t000000z-00000023" / "units"

    def fail_launch(*_args, **_kwargs):
        pytest.fail("foreign start reached subprocess launch")

    monkeypatch.setattr("aflow.control_plane.persistent_units.subprocess.Popen", fail_launch)
    with pytest.raises(PersistentUnitError, match="outside that root"):
        manager.start(unit, ("echo",), cwd=foreign)
    assert manager._receipt_roots == {}
    assert not receipt_dir.exists()


def test_bound_unit_receipts_use_daemon_root_not_execution_worktree(
    tmp_path: Path,
    fake_aflow: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "primary"
    execution_worktree = tmp_path / "execution-worktree"
    primary.mkdir()
    execution_worktree.mkdir()
    _spawn_env(primary, monkeypatch)
    _harness(primary)
    unit = "aflow-run-20260912t000000z-00000024.service"
    execution_receipts = _write_terminal_receipt(
        execution_worktree,
        unit,
        nonce="execution-nonce",
        returncode=23,
    )
    manager = PersistentUnitManager(executable=fake_aflow, projects_root=tmp_path)
    manager.bind_project_root(primary)

    assert manager.get(unit) is None
    execution_start = json.loads((execution_receipts / "start.json").read_text())
    started = manager.start(unit, (str(primary / "bin" / "worker"),), cwd=primary)
    primary_receipts = primary / ".aflow" / "runs" / "20260912t000000z-00000024" / "units"
    primary_start = json.loads((primary_receipts / "start.json").read_text())
    assert started.name == unit
    assert primary_start["nonce"] != execution_start["nonce"]
    assert manager._cwd_for(unit) == primary.resolve()
    assert manager.get(unit) is not None
    manager.stop(unit)
