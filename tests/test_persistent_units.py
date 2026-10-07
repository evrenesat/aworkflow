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
import shutil
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


# -- real-process owned-session topology --------------------------------------
# These fixtures model the observed bug: a controller that is the leader of its
# own session spawns a ``timeout``-shaped wrapper in a *separate* process group
# (same session), which in turn launches a long-lived provider in that group.
# The legacy stop only signalled the controller's own group, leaving the
# wrapper and provider alive.  The session-aware stop must end every verified
# group in the owned session while leaving unrelated sessions alone.


def _write_topology(
    repo: Path,
    *,
    provider_body: str = "import time; time.sleep(120)",
    wrapper_waits: bool = True,
    controller_waits: bool = True,
) -> Path:
    """Write controller/wrapper/provider scripts modelling a timeout group.

    The controller is spawned by the fake wrapper with ``start_new_session``
    so it is the session leader.  It spawns the wrapper with
    ``process_group=0`` (a new group in the same session, like ``timeout``),
    and the wrapper spawns the provider in its own group.
    """
    provider = repo / "prov.py"
    wrapper = repo / "wrap.py"
    controller = repo / "ctrl.py"
    provider.write_text(provider_body, encoding="utf-8")
    wait_tail = ").wait()" if wrapper_waits else ")"
    wrapper.write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, {str(provider)!r}]{wait_tail}\n",
        encoding="utf-8",
    )
    controller_tail = ").wait()" if controller_waits else ")\nimport time; time.sleep(120)\n"
    controller.write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, {str(wrapper)!r}], process_group=0{controller_tail}",
        encoding="utf-8",
    )
    return controller


def _session_pids(session_leader_pid: int) -> set[int]:
    from aflow.process_identity import session_members

    members = session_members(session_leader_pid)
    if members is None:
        return set()
    return {member.pid for member in members}


def _spawn_decoy() -> subprocess.Popen:
    return subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_owner_stop_ends_separate_group_wrapper_and_provider(
    tmp_path, fake_aflow, repo, monkeypatch
) -> None:
    """Managed stop ends the controller, its timeout group, and the provider."""
    controller = _write_topology(repo)
    manager = PersistentUnitManager(executable=fake_aflow, stop_timeout_seconds=15)
    unit = "aflow-run-20260908t000000z-00000030.service"
    manager.start(unit, (sys.executable, str(controller)), cwd=repo)
    receipts = repo / ".aflow" / "runs" / "20260908t000000z-00000030" / "units"
    assert _wait_until(lambda: (receipts / "child.json").exists())
    child = json.loads((receipts / "child.json").read_text())
    leader = child["pid"]
    from aflow.process_identity import session_members

    # Wait until the provider is alive in a *separate* group within the session.
    def _has_separate_group() -> bool:
        members = session_members(leader) or []
        return len(members) >= 3 and len({m.pgid for m in members}) >= 2

    assert _wait_until(_has_separate_group), "topology did not form"
    owned_pids = _session_pids(leader)
    decoy = _spawn_decoy()
    try:
        terminal = manager.stop(unit)
        assert terminal is not None and not terminal.is_active
        assert terminal.result == "stopped"
        # Every owned session member (controller, wrapper, provider) is gone.
        assert _wait_until(lambda: all(_process_dead(pid) for pid in owned_pids))
        # The unrelated decoy session survives.
        assert decoy.poll() is None
    finally:
        decoy.kill()
        decoy.wait()


def test_owner_stop_escalates_kill_for_term_ignoring_provider(
    tmp_path, fake_aflow, repo, monkeypatch
) -> None:
    """A provider that ignores SIGTERM is escalated to SIGKILL and ends."""
    controller = _write_topology(
        repo,
        provider_body=(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(120)\n"
        ),
    )
    manager = PersistentUnitManager(executable=fake_aflow, stop_timeout_seconds=20)
    unit = "aflow-run-20260908t000000z-00000031.service"
    manager.start(unit, (sys.executable, str(controller)), cwd=repo)
    receipts = repo / ".aflow" / "runs" / "20260908t000000z-00000031" / "units"
    assert _wait_until(lambda: (receipts / "child.json").exists())
    child = json.loads((receipts / "child.json").read_text())
    leader = child["pid"]
    assert _wait_until(lambda: len(_session_pids(leader)) >= 3)
    owned_pids = _session_pids(leader)
    decoy = _spawn_decoy()
    try:
        terminal = manager.stop(unit)
        assert terminal is not None and not terminal.is_active
        assert terminal.result == "stopped"
        assert _wait_until(lambda: all(_process_dead(pid) for pid in owned_pids))
        assert decoy.poll() is None
    finally:
        decoy.kill()
        decoy.wait()


def test_owner_stop_kills_surviving_descendant_after_wrapper_exit(
    tmp_path, fake_aflow, repo, monkeypatch
) -> None:
    """A provider outliving its wrapper is still ended by the owned stop.

    The wrapper exits immediately, reparenting the provider to init while it
    remains in the owned session.  The controller stays alive so the stop has
    a live session anchor; the stop must rescan and end the survivor.
    """
    controller = _write_topology(repo, wrapper_waits=False, controller_waits=False)
    manager = PersistentUnitManager(executable=fake_aflow, stop_timeout_seconds=15)
    unit = "aflow-run-20260908t000000z-00000032.service"
    manager.start(unit, (sys.executable, str(controller)), cwd=repo)
    receipts = repo / ".aflow" / "runs" / "20260908t000000z-00000032" / "units"
    assert _wait_until(lambda: (receipts / "child.json").exists())
    child = json.loads((receipts / "child.json").read_text())
    leader = child["pid"]
    # The provider is alive in the session even though the wrapper has exited.
    assert _wait_until(lambda: len(_session_pids(leader)) >= 2)
    owned_pids = _session_pids(leader)
    decoy = _spawn_decoy()
    try:
        terminal = manager.stop(unit)
        assert terminal is not None and not terminal.is_active
        assert terminal.result == "stopped"
        assert _wait_until(lambda: all(_process_dead(pid) for pid in owned_pids))
        assert decoy.poll() is None
    finally:
        decoy.kill()
        decoy.wait()


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("timeout") is None,
    reason="real GNU timeout required",
)
def test_owner_stop_ends_real_gnu_timeout_group(
    tmp_path, fake_aflow, repo, monkeypatch
) -> None:
    """Managed stop ends a real ``timeout`` wrapper group and its provider."""
    provider = repo / "prov.py"
    provider.write_text("import time; time.sleep(120)", encoding="utf-8")
    controller = repo / "ctrl.py"
    controller.write_text(
        "import subprocess\n"
        f"subprocess.Popen(['timeout', '300', {sys.executable!r}, {str(provider)!r}],"
        " process_group=0).wait()\n",
        encoding="utf-8",
    )
    manager = PersistentUnitManager(executable=fake_aflow, stop_timeout_seconds=15)
    unit = "aflow-run-20260908t000000z-00000034.service"
    manager.start(unit, (sys.executable, str(controller)), cwd=repo)
    receipts = repo / ".aflow" / "runs" / "20260908t000000z-00000034" / "units"
    assert _wait_until(lambda: (receipts / "child.json").exists())
    child = json.loads((receipts / "child.json").read_text())
    leader = child["pid"]
    # The timeout wrapper and the provider must both be alive in the session.
    assert _wait_until(lambda: len(_session_pids(leader)) >= 3)
    owned_pids = _session_pids(leader)
    decoy = _spawn_decoy()
    try:
        terminal = manager.stop(unit)
        assert terminal is not None and not terminal.is_active
        assert terminal.result == "stopped"
        assert _wait_until(lambda: all(_process_dead(pid) for pid in owned_pids))
        assert decoy.poll() is None
    finally:
        decoy.kill()
        decoy.wait()


def test_owner_stop_fails_typed_on_incomplete_session_observation(
    tmp_path, fake_aflow, repo, monkeypatch
) -> None:
    """An incomplete session observation fails typed, never a stopped receipt.

    With the session-leader contract verified but the session observation
    unknown, the legacy controller group may still be stopped within bounds,
    but session cessation cannot be proven: the stop raises a typed error and
    writes no stopped receipt while the separate-group provider survives
    (admission stays occupied by the surviving bound provider).
    """
    controller = _write_topology(repo)
    manager = PersistentUnitManager(executable=fake_aflow, stop_timeout_seconds=15)
    unit = "aflow-run-20260908t000000z-00000033.service"
    manager.start(unit, (sys.executable, str(controller)), cwd=repo)
    receipts = repo / ".aflow" / "runs" / "20260908t000000z-00000033" / "units"
    assert _wait_until(lambda: (receipts / "child.json").exists())
    child = json.loads((receipts / "child.json").read_text())
    leader = child["pid"]
    assert _wait_until(lambda: len(_session_pids(leader)) >= 3)
    owned_pids = _session_pids(leader)
    survivor = owned_pids - {leader}
    # Force the session observation to fail closed (unknown).
    monkeypatch.setattr(
        "aflow.control_plane.persistent_units.session_members",
        lambda *args, **kwargs: None,
    )
    try:
        with pytest.raises(PersistentUnitError):
            manager.stop(unit)
        # No successful stopped receipt is written.
        assert not (receipts / "stopped.json").exists()
        # The controller group ended...
        assert _wait_until(lambda: _process_dead(leader))
        # ...but the separate-group provider survives (no group cleanup claimed).
        assert any(not _process_dead(pid) for pid in survivor)
    finally:
        for pid in survivor:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def test_owner_stop_fails_typed_on_failed_rescan_with_term_descendant(
    tmp_path, fake_aflow, repo, monkeypatch
) -> None:
    """A failed rescan after a TERM-handler descendant fails typed.

    The initial session snapshot succeeds.  The controller's TERM handler
    spawns an ordinary new-group, same-session child, then exits.  A
    subsequent rescan is forced unknown; the spawned descendant survives, so
    the stop must fail typed (no stopped receipt) and the unrelated decoy
    survives.  Broker occupancy is preserved.
    """
    import aflow.control_plane.persistent_units as pu

    provider = repo / "prov.py"
    provider.write_text("import time; time.sleep(120)", encoding="utf-8")
    wrapper = repo / "wrap.py"
    wrapper.write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, {str(provider)!r}]).wait()\n",
        encoding="utf-8",
    )
    # The controller is the session leader; its TERM handler spawns an
    # ordinary new-group, same-session child (modeling the observed bug) and
    # then exits, leaving that descendant alive.
    controller = repo / "ctrl.py"
    controller.write_text(
        "import subprocess, sys, signal\n"
        f"subprocess.Popen([sys.executable, {str(wrapper)!r}], process_group=0)\n"
        "def _h(s, f):\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],"
        " process_group=0)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, _h)\n"
        "import time\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    # The deadline must exceed the KILL-escalation window so the TERM phase
    # runs first (letting the controller's TERM handler spawn the descendant)
    # while every rescan stays unknown; the stop then fails typed at the
    # bounded deadline without ever observing (and killing) the descendant.
    manager = PersistentUnitManager(executable=fake_aflow, stop_timeout_seconds=5)
    unit = "aflow-run-20260908t000000z-00000035.service"
    manager.start(unit, (sys.executable, str(controller)), cwd=repo)
    receipts = repo / ".aflow" / "runs" / "20260908t000000z-00000035" / "units"
    assert _wait_until(lambda: (receipts / "child.json").exists())
    child = json.loads((receipts / "child.json").read_text())
    leader = child["pid"]
    # controller + wrapper + provider are alive in the session.
    assert _wait_until(lambda: len(_session_pids(leader)) >= 3)
    decoy = _spawn_decoy()
    real_members = pu.session_members

    def real_session_pids(pid: int) -> set[int]:
        members = real_members(pid)
        return {m.pid for m in members} if members is not None else set()

    calls = {"n": 0}

    def flaky_members(*args, **kwargs):
        calls["n"] += 1
        # First observation succeeds; every subsequent rescan is unknown.
        return real_members(*args, **kwargs) if calls["n"] == 1 else None

    monkeypatch.setattr(pu, "session_members", flaky_members)
    try:
        with pytest.raises(PersistentUnitError):
            manager.stop(unit)
        # No successful stopped receipt is written.
        assert not (receipts / "stopped.json").exists()
        # The controller (leader) is gone...
        assert _wait_until(lambda: _process_dead(leader))
        # ...but the TERM-handler descendant (a same-session child) survives.
        survivors = [pid for pid in real_session_pids(leader) if not _process_dead(pid)]
        assert survivors, "the TERM-handler descendant must survive the typed failure"
        # The unrelated decoy session survives.
        assert decoy.poll() is None
    finally:
        for pid in real_session_pids(leader):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        decoy.kill()
        decoy.wait()


# -- anchored rescan and positive-death stop semantics -------------------------
# Signal-recording regressions for the owned-session stop: every rescan and
# signal requires current ownership proof; lost anchors, reused identities,
# and incomplete observations can never signal unrelated work, and positively
# dead zombies are cessation rather than failure.


def _signal_recording_manager(fake_aflow: Path, stop_timeout_seconds: float = 5.0):
    manager = PersistentUnitManager(
        executable=fake_aflow, stop_timeout_seconds=stop_timeout_seconds
    )
    signals: list[list[object]] = []
    manager._signal_group = lambda pgid, sig: signals.append([pgid, sig.name])
    return manager, signals


def test_stop_session_groups_reused_initial_target_never_signalled(
    tmp_path, fake_aflow, monkeypatch
) -> None:
    """A stale initial group and an unanchored replacement are never signalled.

    The captured member is already gone (reused/moved); no live anchor
    authorizes any numeric-SID inventory, so the observed replacement session
    member is neither captured nor signalled and the stop fails typed.
    """
    from aflow.control_plane import persistent_units as pu
    from aflow.process_identity import SessionMember

    monkeypatch.setattr(pu, "_KILL_ESCALATION_SECONDS", 0.2)
    manager, signals = _signal_recording_manager(fake_aflow)
    stale = SessionMember(pid=900001, birth="linux-start-ticks:1", pgid=900001)
    replacement = SessionMember(pid=900002, birth="linux-start-ticks:2", pgid=900002)
    monkeypatch.setattr(
        pu, "session_member_live", lambda member, session_id, deadline: False
    )
    snapshots = {"calls": 0}

    def unanchored_inventory(session_id, deadline):
        snapshots["calls"] += 1
        return [replacement]

    monkeypatch.setattr(manager, "_session_snapshot", unanchored_inventory)
    monkeypatch.setattr(pu, "process_liveness", lambda pid, deadline: "unknown")
    assert (
        manager._stop_session_groups(900001, [stale], time.monotonic() + 1.0)
        is False
    )
    assert signals == []
    assert snapshots["calls"] == 0, "no unanchored numeric-SID inventory"


def test_stop_session_groups_lost_anchor_blocks_replacement_inventory(
    tmp_path, fake_aflow, monkeypatch
) -> None:
    """Once every captured anchor is lost, no inventory is trusted or signalled.

    The anchor is live for exactly one anchored rescan.  After the anchor is
    lost, further numeric-SID inventories may be observed but are never
    trusted: the replacement member is neither adopted, signalled, nor used
    to clear the stop.  The stop fails typed (unknown liveness).
    """
    from aflow.control_plane import persistent_units as pu
    from aflow.process_identity import SessionMember

    monkeypatch.setattr(pu, "_KILL_ESCALATION_SECONDS", 0.2)
    manager, signals = _signal_recording_manager(fake_aflow)
    anchor = SessionMember(pid=900001, birth="linux-start-ticks:1", pgid=900001)
    replacement = SessionMember(pid=900002, birth="linux-start-ticks:2", pgid=900002)
    live = {900001: True}
    monkeypatch.setattr(
        pu, "session_member_live",
        lambda member, session_id, deadline: live.get(member.pid, False),
    )
    snapshots = {"calls": 0}
    clock = {"now": 100.0}
    monkeypatch.setattr(pu.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pu.time, "sleep", lambda s: clock.update(now=clock["now"] + s))

    def anchored_then_lost(session_id, deadline):
        snapshots["calls"] += 1
        if snapshots["calls"] == 1:
            live[900001] = False  # the anchor is lost during the first rescan
            return [anchor]
        return [replacement]

    monkeypatch.setattr(manager, "_session_snapshot", anchored_then_lost)
    monkeypatch.setattr(pu, "process_liveness", lambda pid, deadline: "unknown")
    assert manager._stop_session_groups(900001, [anchor], 101.0) is False
    assert signals == [[900001, "SIGTERM"]], "the replacement must receive no signal"
    assert snapshots["calls"] > 1, "unanchored inventories are observed..."
    # At most one 50ms poll tick may overshoot the deadline; no probe may.
    assert clock["now"] <= 101.05, "...but stay within the shared budget"


def test_stop_session_groups_failed_rescan_then_anchor_loss_keeps_uncertainty(
    tmp_path, fake_aflow, monkeypatch
) -> None:
    """A failed rescan followed by anchor loss keeps the typed uncertainty.

    The first (anchored) rescan fails; the anchor then dies.  The pending
    observation uncertainty can never be cleared without a subsequent
    complete anchored observation, so the stop fails typed even though no
    member is known live.
    """
    from aflow.control_plane import persistent_units as pu
    from aflow.process_identity import SessionMember

    monkeypatch.setattr(pu, "_KILL_ESCALATION_SECONDS", 0.2)
    manager, signals = _signal_recording_manager(fake_aflow)
    anchor = SessionMember(pid=900001, birth="linux-start-ticks:1", pgid=900001)
    live = {900001: True}
    monkeypatch.setattr(
        pu, "session_member_live",
        lambda member, session_id, deadline: live.get(member.pid, False),
    )
    snapshots = {"calls": 0}

    def failing_rescan(session_id, deadline):
        snapshots["calls"] += 1
        live[900001] = False  # the anchor dies with the observation failing
        return None

    clock = {"now": 100.0}
    monkeypatch.setattr(pu.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pu.time, "sleep", lambda s: clock.update(now=clock["now"] + s))
    monkeypatch.setattr(manager, "_session_snapshot", failing_rescan)
    monkeypatch.setattr(pu, "process_liveness", lambda pid, deadline: "unknown")
    assert manager._stop_session_groups(900001, [anchor], 101.0) is False
    assert signals == [[900001, "SIGTERM"]]
    assert snapshots["calls"] >= 1
    # At most one 50ms poll tick may overshoot the deadline; no probe may.
    assert clock["now"] <= 101.05, "the pending-uncertainty loop stays bounded"


def test_stop_session_groups_unknown_birth_member_never_signalled(
    tmp_path, fake_aflow, monkeypatch
) -> None:
    """A missing-birth (zombie) member is never signalled; death is positive.

    The anchor's group is TERM-signalled; the unknown-birth member's group is
    not.  Once every captured identity is positively absent and every group
    is absent, the stop succeeds with no signal to the zombie-only group.
    """
    from aflow.control_plane import persistent_units as pu
    from aflow.process_identity import SessionMember

    monkeypatch.setattr(pu, "_KILL_ESCALATION_SECONDS", 0.2)
    manager, signals = _signal_recording_manager(fake_aflow)
    anchor = SessionMember(pid=900001, birth="linux-start-ticks:1", pgid=900001)
    unknown = SessionMember(pid=900002, birth=None, pgid=900002)
    live = {900001: True}
    monkeypatch.setattr(
        pu, "session_member_live",
        lambda member, session_id, deadline: live.get(member.pid, False),
    )
    snapshots = {"calls": 0}

    def one_rescan(session_id, deadline):
        snapshots["calls"] += 1
        live[900001] = False
        return [anchor, unknown]

    monkeypatch.setattr(manager, "_session_snapshot", one_rescan)
    monkeypatch.setattr(pu, "process_liveness", lambda pid, deadline: "absent")
    monkeypatch.setattr(pu, "process_group_state", lambda pgid, deadline: "absent")
    assert (
        manager._stop_session_groups(900001, [anchor, unknown], time.monotonic() + 1.0)
        is True
    )
    assert signals == [[900001, "SIGTERM"]]
    assert snapshots["calls"] == 1


def test_stop_session_groups_valid_anchored_late_descendant_signalled(
    tmp_path, fake_aflow, monkeypatch
) -> None:
    """A late descendant observed under a live anchor is captured and ended.

    The first anchored rescan observes a new same-session member; it is
    retained and TERM-signalled.  The subsequent anchored observation sees
    only positively dead members, so the stop succeeds with bounded signals.
    """
    from aflow.control_plane import persistent_units as pu
    from aflow.process_identity import SessionMember

    monkeypatch.setattr(pu, "_KILL_ESCALATION_SECONDS", 0.2)
    manager, signals = _signal_recording_manager(fake_aflow)
    anchor = SessionMember(pid=900001, birth="linux-start-ticks:1", pgid=900001)
    late = SessionMember(pid=900003, birth="linux-start-ticks:3", pgid=900003)
    live = {900001: True, 900003: True}
    monkeypatch.setattr(
        pu, "session_member_live",
        lambda member, session_id, deadline: live.get(member.pid, False),
    )
    calls = {"n": 0}

    def anchored_rescans(session_id, deadline):
        calls["n"] += 1
        if calls["n"] == 1:
            return [anchor, late]
        live.clear()
        return [
            SessionMember(pid=900001, birth=None, pgid=900001),
            SessionMember(pid=900003, birth=None, pgid=900003),
        ]

    monkeypatch.setattr(manager, "_session_snapshot", anchored_rescans)
    monkeypatch.setattr(pu, "process_liveness", lambda pid, deadline: "absent")
    monkeypatch.setattr(pu, "process_group_state", lambda pgid, deadline: "absent")
    assert (
        manager._stop_session_groups(900001, [anchor], time.monotonic() + 1.0)
        is True
    )
    assert signals == [[900001, "SIGTERM"], [900003, "SIGTERM"]]


def test_stop_session_groups_unknown_identity_retains_failure(
    tmp_path, fake_aflow, monkeypatch
) -> None:
    """An unproven (unknown-liveness) captured identity retains typed failure."""
    from aflow.control_plane import persistent_units as pu
    from aflow.process_identity import SessionMember

    monkeypatch.setattr(pu, "_KILL_ESCALATION_SECONDS", 0.2)
    manager, signals = _signal_recording_manager(fake_aflow)
    member = SessionMember(pid=900004, birth="linux-start-ticks:9", pgid=900004)
    monkeypatch.setattr(pu, "session_member_live", lambda m, s, deadline: False)
    monkeypatch.setattr(pu, "process_liveness", lambda pid, deadline: "unknown")
    assert (
        manager._stop_session_groups(900004, [member], time.monotonic() + 0.5)
        is False
    )
    assert signals == []


# -- contract separation and bounded fallback proof ----------------------------
# Signal-recording regressions for the controller-only fallback: uncertain
# modern stops never reach the unchecked legacy signal, every fallback
# signal is preceded by fresh bounded ownership proof, and no successful
# stopped receipt is written without positive cessation.


def _stop_fixture_unit(
    tmp_path: Path, *, stop_timeout_seconds: float = 0.2
) -> tuple[Path, PersistentUnitManager, str]:
    """Receipts for a recorded controller group with a fake identity."""
    run_id = "20261006t000000z-00000099"
    name = f"aflow-run-{run_id}.service"
    receipts = tmp_path / ".aflow" / "runs" / run_id / "units"
    receipts.mkdir(parents=True)
    (receipts / "start.json").write_text(
        json.dumps({"schema": 1, "run_id": run_id, "unit": name, "nonce": "fixture-only"})
    )
    (receipts / "child.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "nonce": "fixture-only",
                "pid": 900001,
                "pgid": 900001,
                "process_birth": "original-birth",
            }
        )
    )
    manager = PersistentUnitManager(
        executable=Path("/fixture-only"), stop_timeout_seconds=stop_timeout_seconds
    )
    manager._cwd_for = lambda unit: tmp_path
    return receipts, manager, name


def test_stop_lost_contract_identity_signals_nothing_and_writes_no_receipt(
    tmp_path, monkeypatch
) -> None:
    """Identity loss during contract validation is unknown, never legacy."""
    from aflow.control_plane import persistent_units as pu
    from aflow.control_plane.units import UnitState

    receipts, manager, name = _stop_fixture_unit(tmp_path)
    signals: list[list[object]] = []
    manager._signal_group = lambda pgid, sig: signals.append([pgid, sig.name])
    manager._observe = lambda r, deadline=None: UnitState(
        name=name, active_state="active", sub_state="running", main_pid=900001
    )
    # The bounded entry identity check passes; the contract's bounded
    # recheck observes the replaced identity, so the topology is unknown,
    # not legacy.
    alive_results = iter([True, False])
    monkeypatch.setattr(
        pu,
        "_bounded_process_alive",
        lambda pid, birth, deadline: next(alive_results, False),
    )
    with pytest.raises(PersistentUnitError, match="unconfirmed"):
        manager.stop(name)
    assert signals == [], "unknown topology must never signal"
    assert not (receipts / "stopped.json").exists()


def test_stop_failed_snapshot_with_identity_loss_never_signals_replacement(
    tmp_path, monkeypatch
) -> None:
    """A modern snapshot failure with a replaced controller is unconfirmed.

    The session-leader contract verified, the initial observation went
    incomplete, and the controller identity was replaced before the
    controller-group cleanup proof: no signal reaches the replacement and
    no successful stopped receipt is written.
    """
    from aflow.control_plane import persistent_units as pu
    from aflow.control_plane.units import UnitState

    receipts, manager, name = _stop_fixture_unit(tmp_path)
    signals: list[list[object]] = []
    manager._signal_group = lambda pgid, sig: signals.append([pgid, sig.name])
    manager._observe = lambda r, deadline=None: UnitState(
        name=name, active_state="active", sub_state="running", main_pid=900001
    )
    monkeypatch.setattr(pu, "_process_alive", lambda pid, birth: True)
    monkeypatch.setattr(
        pu, "_bounded_process_alive", lambda pid, birth, deadline: True
    )
    monkeypatch.setattr(pu.os, "getsid", lambda pid: 900001)
    monkeypatch.setattr(pu.os, "getpgid", lambda pid: 900001)
    monkeypatch.setattr(manager, "_session_snapshot", lambda sid, deadline: None)
    # The controller identity was replaced before the cleanup proof ran.
    monkeypatch.setattr(
        pu, "controller_group_owned", lambda pid, birth, sid, deadline: False
    )
    monkeypatch.setattr(pu, "process_liveness", lambda pid, deadline=None: "present")
    with pytest.raises(PersistentUnitError, match="incomplete"):
        manager.stop(name)
    assert signals == [], "the replacement target must receive no signal"
    assert not (receipts / "stopped.json").exists()


@pytest.mark.parametrize("liveness_after_term", ["unknown", "present"])
def test_stop_changed_identity_after_term_never_escalates(
    tmp_path, monkeypatch, liveness_after_term
) -> None:
    """A changed/unknown identity after TERM is never escalated to KILL."""
    from aflow.control_plane import persistent_units as pu
    from aflow.control_plane.units import UnitState

    receipts, manager, name = _stop_fixture_unit(tmp_path)
    signals: list[list[object]] = []
    manager._signal_group = lambda pgid, sig: signals.append([pgid, sig.name])
    manager._observe = lambda r, deadline=None: UnitState(
        name=name, active_state="active", sub_state="running", main_pid=900001
    )
    monkeypatch.setattr(pu, "_process_alive", lambda pid, birth: True)
    monkeypatch.setattr(
        pu, "_bounded_process_alive", lambda pid, birth, deadline: True
    )
    monkeypatch.setattr(pu.os, "getsid", lambda pid: 900002)  # non-leader SID
    monkeypatch.setattr(pu.os, "getpgid", lambda pid: 900001)
    # Fresh proof passes immediately before the TERM; the identity is
    # changed (or its birth unknown) before the KILL proof.
    owned = iter([True, False])
    monkeypatch.setattr(
        pu, "controller_group_owned", lambda pid, birth, sid, deadline: next(owned, False)
    )
    liveness = iter(["present", liveness_after_term])
    monkeypatch.setattr(
        pu, "process_liveness", lambda pid, deadline=None: next(liveness, liveness_after_term)
    )
    with pytest.raises(PersistentUnitError, match="did not terminate"):
        manager.stop(name)
    assert signals == [[900001, "SIGTERM"]], "no KILL to a changed identity"
    assert not (receipts / "stopped.json").exists()


def test_stop_valid_legacy_controller_group_succeeds(tmp_path, monkeypatch) -> None:
    """A genuine legacy controller-group topology retains its stop contract."""
    from aflow.control_plane import persistent_units as pu
    from aflow.control_plane.units import UnitState

    receipts, manager, name = _stop_fixture_unit(tmp_path)
    signals: list[list[object]] = []
    manager._signal_group = lambda pgid, sig: signals.append([pgid, sig.name])
    manager._observe = lambda r, deadline=None: UnitState(
        name=name, active_state="active", sub_state="running", main_pid=900001
    )
    monkeypatch.setattr(pu, "_process_alive", lambda pid, birth: True)
    monkeypatch.setattr(
        pu, "_bounded_process_alive", lambda pid, birth, deadline: True
    )
    monkeypatch.setattr(pu.os, "getsid", lambda pid: 900002)  # non-leader SID
    monkeypatch.setattr(pu.os, "getpgid", lambda pid: 900001)
    monkeypatch.setattr(
        pu, "controller_group_owned", lambda pid, birth, sid, deadline: True
    )
    liveness = iter(["present", "absent"])
    monkeypatch.setattr(
        pu, "process_liveness", lambda pid, deadline=None: next(liveness, "absent")
    )
    terminal = manager.stop(name)
    assert terminal is not None and not terminal.is_active
    assert terminal.result == "stopped"
    # Only the controller's own group: legacy success never claims cleanup
    # of any additional provider group.
    assert signals == [[900001, "SIGTERM"]]
    assert (receipts / "stopped.json").exists()


# -- bounded stop-path revalidation ---------------------------------------------
# Fake-clock regressions: every stop-path observation shares the outer
# deadline, uses bounded native probes (never the generic five-second
# fallback), and issues no signal after the deadline expires.


def test_stop_session_groups_bounded_darwin_revalidation_stays_in_budget(
    monkeypatch, tmp_path: Path
) -> None:
    """Slow Darwin birth probes share the outer deadline, never serial 5s."""
    from aflow import process_identity as pi
    from aflow.control_plane import persistent_units as pu

    monkeypatch.setattr(pu, "_KILL_ESCALATION_SECONDS", 0.2)
    manager, signals = _signal_recording_manager(tmp_path / "aflow")
    captured = [
        pi.SessionMember(pid, "ps-lstart:original", pid)
        for pid in (900001, 900002, 900003)
    ]
    clock = {"now": 100.0}
    probes: list[dict[str, object]] = []

    def slow_ps(argv, **kwargs):
        probes.append({"argv": list(argv), "timeout": kwargs["timeout"]})
        clock["now"] += float(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    def sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(pi.sys, "platform", "darwin")
    monkeypatch.setattr(pi.os, "getsid", lambda pid: 900001)
    monkeypatch.setattr(pi.os, "getpgid", lambda pid: pid)

    def read_missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(Path, "read_text", read_missing)
    monkeypatch.setattr(pi.subprocess, "run", slow_ps)
    for module in (pi, pu):
        monkeypatch.setattr(module.time, "monotonic", lambda: clock["now"])
        monkeypatch.setattr(module.time, "sleep", sleep)
    result = manager._stop_session_groups(900001, captured, deadline=101.0)
    assert result is False, "incomplete bounded work fails typed"
    assert signals == [], "no signal after the shared deadline expired"
    assert clock["now"] - 100.0 <= 1.0, "total work stays within the shared budget"
    assert probes, "at least one bounded probe was attempted"
    assert all(
        float(probe["timeout"]) <= 1.0 for probe in probes
    ), "no serial five-second probes"


def test_session_contract_darwin_birth_probe_bounded_to_observation_window(
    monkeypatch,
) -> None:
    """A Darwin contract birth probe spends at most the two-second
    observation window, never the entire outer stop deadline."""
    from aflow import process_identity as pi
    from aflow.control_plane import persistent_units as pu

    monkeypatch.setattr(pi.sys, "platform", "darwin")
    monkeypatch.setattr(pi.os, "getsid", lambda pid: 900010)
    monkeypatch.setattr(pi.os, "getpgid", lambda pid: 900010)
    probes: list[float] = []

    def slow_ps(argv, **kwargs):
        probes.append(float(kwargs["timeout"]))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(pi.subprocess, "run", slow_ps)
    contract = pu.session_contract(900010, "ps-lstart:original", time.monotonic() + 10.0)
    assert contract.topology == "unknown"
    assert probes, "a bounded birth probe was attempted"
    assert all(timeout <= 2.0 for timeout in probes), (
        "the contract probe never spends the 10-second outer window"
    )


def test_stop_session_groups_rescan_inventory_and_post_proof_share_one_deadline(
    fake_aflow, monkeypatch
) -> None:
    """A rescan inventory and its post-inventory proof share one 2s window.

    The inventory spends 0.8s of the original two-second operation deadline;
    the post-inventory birth proof is constrained to the remaining 1.2s of
    that same original deadline, never a renewed two-second window.
    """
    from aflow import process_identity as pi
    from aflow.control_plane import persistent_units as pu

    manager, signals = _signal_recording_manager(fake_aflow)
    captured = [pi.SessionMember(900001, "ps-lstart:original", 900001)]
    clock = {"now": 100.0}
    probes: list[dict[str, float]] = []

    def cheap_ps(argv, **kwargs):
        # Each bounded birth probe spends 0.2s and returns the exact birth.
        probes.append({"at": clock["now"], "timeout": float(kwargs["timeout"])})
        clock["now"] += 0.2
        return subprocess.CompletedProcess(argv, 0, "original\n", "")

    def rescan(session_id, deadline):
        # The inventory spends 0.8s of the shared operation window.
        clock["now"] += 0.8
        return [pi.SessionMember(900001, "ps-lstart:original", 900001)]

    liveness = {"n": 0}

    def scripted_liveness(pid, deadline):
        liveness["n"] += 1
        return "present" if liveness["n"] == 1 else "absent"

    monkeypatch.setattr(pi.sys, "platform", "darwin")
    monkeypatch.setattr(pi.os, "getsid", lambda pid: 900001)
    monkeypatch.setattr(pi.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(pi.subprocess, "run", cheap_ps)
    monkeypatch.setattr(manager, "_session_snapshot", rescan)
    monkeypatch.setattr(pu, "process_liveness", scripted_liveness)
    monkeypatch.setattr(pu, "process_group_state", lambda pgid, deadline: "absent")
    monkeypatch.setattr(pu.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pu.time, "sleep", lambda s: clock.update(now=clock["now"] + s))
    result = manager._stop_session_groups(900001, captured, deadline=110.0)
    assert result is True, "positive cessation succeeds inside the shared budget"
    assert signals and all(pgid == 900001 for pgid, _ in signals), (
        "only the proven group is ever signalled"
    )
    assert signals[0] == [900001, "SIGTERM"], "TERM-first escalation"
    assert len(probes) >= 3, "initial anchor, pre-signal proof, and post-inventory proof"
    # The first post-inventory proof is bounded by the original operation
    # deadline (created before the 0.8s inventory), not a renewed 2s window.
    assert probes[2]["timeout"] <= 1.2 + 0.001, (
        "post-inventory proof shares the original two-second deadline"
    )
    assert all(probe["timeout"] <= 2.0 for probe in probes), (
        "no probe exceeds the observation window"
    )


def test_stop_session_groups_no_signal_when_proof_finishes_at_deadline(
    fake_aflow, monkeypatch
) -> None:
    """An ownership proof that finishes at the deadline authorizes no signal.

    The pre-signal revalidation completes exactly at the shared operation
    deadline: no TERM or KILL is issued, and the stop only succeeds on the
    separate positive cessation evidence.
    """
    from aflow import process_identity as pi
    from aflow.control_plane import persistent_units as pu

    manager, signals = _signal_recording_manager(fake_aflow)
    clock = {"now": 100.0}
    captured = [pi.SessionMember(900001, "original", 900001)]

    def proof_to_deadline(member, session_id, deadline):
        # The revalidation finishes exactly at the shared operation deadline.
        clock["now"] = min(deadline, clock["now"] + 5.0)
        return True

    monkeypatch.setattr(pu, "session_member_live", proof_to_deadline)
    monkeypatch.setattr(manager, "_session_snapshot", lambda sid, deadline: [])
    monkeypatch.setattr(pu, "process_liveness", lambda pid, deadline: "absent")
    monkeypatch.setattr(pu, "process_group_state", lambda pgid, deadline: "absent")
    monkeypatch.setattr(pu.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pu.time, "sleep", lambda s: clock.update(now=clock["now"] + s))
    assert manager._stop_session_groups(900001, captured, 110.0) is False, (
        "no successful cessation once the shared deadline expires"
    )
    assert signals == [], "expired ownership proof authorizes no TERM or KILL"


# -- original session provenance across each inventory -------------------------
# Recorded-signal regressions: the receipt-bound controller and the proven
# captured set retain authority across every observation; a fresh inventory
# can never prove its own ancestry, and no replacement identity is signalled
# or adopted after the original anchor ceases.


def test_stop_initial_snapshot_replacement_never_signalled(
    tmp_path, monkeypatch
) -> None:
    """A controller that dies and whose SID is reused during the initial
    observation authorizes no inventory-derived signal and no receipt.

    The receipt-bound controller birth is revalidated after the initial
    snapshot; the replacement leader and provider observed under the reused
    numeric SID receive no signal and no successful stopped receipt is
    written.
    """
    from aflow.control_plane import persistent_units as pu
    from aflow.control_plane.units import UnitState
    from aflow.process_identity import SessionMember

    receipts, manager, name = _stop_fixture_unit(tmp_path, stop_timeout_seconds=5.0)
    signals: list[list[object]] = []
    manager._signal_group = lambda pgid, sig: signals.append([pgid, sig.name])
    manager._observe = lambda r, deadline=None: UnitState(
        name=name, active_state="active", sub_state="running", main_pid=900001
    )
    birth = {900001: "original-birth", 900011: None}

    def snapshot(session_id, deadline):
        # The recorded controller ceases during the bounded observation and
        # its numeric SID is reused by an unrelated session.
        birth[900001] = "replacement-leader"
        birth[900011] = "replacement-provider"
        return [
            SessionMember(pid=900001, birth="replacement-leader", pgid=900001),
            SessionMember(pid=900011, birth="replacement-provider", pgid=900011),
        ]

    monkeypatch.setattr(pu, "_process_alive", lambda pid, original: birth[pid] == original)
    monkeypatch.setattr(
        pu, "_bounded_process_alive", lambda pid, original, deadline: birth[pid] == original
    )
    monkeypatch.setattr(pu.os, "getsid", lambda pid: 900001)
    monkeypatch.setattr(pu.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(manager, "_session_snapshot", snapshot)
    monkeypatch.setattr(
        pu, "process_liveness",
        lambda pid, deadline: "present" if birth[pid] else "absent",
    )
    monkeypatch.setattr(
        pu, "process_group_state",
        lambda pgid, deadline: "present" if birth[pgid] else "absent",
    )
    with pytest.raises(PersistentUnitError, match="identity was lost"):
        manager.stop(name)
    assert signals == [], "no signal to a replacement session identity"
    assert not (receipts / "stopped.json").exists(), "no successful stop receipt"


def test_stop_session_groups_rescan_replacement_never_signalled(
    tmp_path, fake_aflow, monkeypatch
) -> None:
    """The captured anchor ceases during a rescan; the reused SID is untrusted.

    The fresh inventory's member belongs to an unrelated replacement session:
    it is neither adopted, signalled, nor used to confirm cessation, and the
    stop fails typed within the shared budget.
    """
    from aflow.control_plane import persistent_units as pu
    from aflow.process_identity import SessionMember

    manager, signals = _signal_recording_manager(fake_aflow)
    anchor = SessionMember(pid=900001, birth="original", pgid=900001)
    birth = {900001: "original", 900011: None}
    clock = {"now": 100.0}
    monkeypatch.setattr(pu.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pu.time, "sleep", lambda s: clock.update(now=clock["now"] + s))

    def snapshot(session_id, deadline):
        # The exact captured anchor ceases during the bounded observation;
        # its numeric SID is subsequently reused by an unrelated session.
        birth[900001] = None
        birth[900011] = "replacement-session-member"
        return [SessionMember(pid=900011, birth="replacement-session-member", pgid=900011)]

    monkeypatch.setattr(
        pu, "session_member_live",
        lambda member, session_id, deadline: birth.get(member.pid) is not None,
    )
    monkeypatch.setattr(manager, "_session_snapshot", snapshot)
    monkeypatch.setattr(
        pu, "process_liveness",
        lambda pid, deadline: "present" if birth.get(pid) else "absent",
    )
    monkeypatch.setattr(
        pu, "process_group_state",
        lambda pgid, deadline: "present" if birth.get(pgid) else "absent",
    )
    assert manager._stop_session_groups(900001, [anchor], 105.0) is False
    assert signals == [[900001, "SIGTERM"]], "only the proven anchor group is signalled"
    assert clock["now"] <= 105.05, "the loop stays within the shared budget"


def test_stop_session_groups_retained_anchor_after_first_exits(
    tmp_path, fake_aflow, monkeypatch
) -> None:
    """A different retained original member keeps the session anchored.

    The first captured anchor exits; a second captured member keeps the
    original session anchored, a late descendant observed under it is
    retained and signalled, and the stop succeeds on positive cessation.
    """
    from aflow.control_plane import persistent_units as pu
    from aflow.process_identity import SessionMember

    manager, signals = _signal_recording_manager(fake_aflow, stop_timeout_seconds=10.0)
    first = SessionMember(pid=900001, birth="linux-start-ticks:1", pgid=900001)
    second = SessionMember(pid=900002, birth="linux-start-ticks:2", pgid=900002)
    late = SessionMember(pid=900003, birth="linux-start-ticks:3", pgid=900003)
    live = {900001: True, 900002: True}
    # The late descendant is not live until it is observed under the retained
    # anchor and retained.
    live[900003] = False
    monkeypatch.setattr(
        pu, "session_member_live",
        lambda member, session_id, deadline: live.get(member.pid, False),
    )
    calls = {"n": 0}

    def anchored_rescans(session_id, deadline):
        calls["n"] += 1
        if calls["n"] == 1:
            live[900001] = False  # the first anchor exits under the second
            live[900003] = True  # the late descendant is retained under it
            return [second, late]
        live.clear()
        return []

    monkeypatch.setattr(manager, "_session_snapshot", anchored_rescans)
    monkeypatch.setattr(pu, "process_liveness", lambda pid, deadline: "absent")
    monkeypatch.setattr(pu, "process_group_state", lambda pgid, deadline: "absent")
    assert manager._stop_session_groups(900001, [first, second], time.monotonic() + 1.0) is True
    assert [sig for _, sig in signals] == ["SIGTERM"] * 3, (
        "every proven group is signalled exactly once"
    )
    assert {pgid for pgid, _ in signals} == {900001, 900002, 900003}, (
        "the retained anchor and its late descendant are signalled"
    )


def test_stop_session_groups_complete_observation_all_ceased_succeeds(
    tmp_path, fake_aflow, monkeypatch
) -> None:
    """A complete observation with everything positively ceased succeeds.

    With no unproven live/unknown additions, every captured member (including
    an unreaped zombie with no birth) positively ceased, and no pending
    uncertainty, cessation succeeds without a live anchor and with no signal.
    """
    from aflow.control_plane import persistent_units as pu
    from aflow.process_identity import SessionMember

    manager, signals = _signal_recording_manager(fake_aflow)
    member = SessionMember(pid=900001, birth="linux-start-ticks:1", pgid=900001)
    zombie = SessionMember(pid=900002, birth=None, pgid=900002)
    monkeypatch.setattr(pu, "session_member_live", lambda m, s, deadline: False)

    def empty_inventory(session_id, deadline):
        return []

    monkeypatch.setattr(manager, "_session_snapshot", empty_inventory)
    monkeypatch.setattr(pu, "process_liveness", lambda pid, deadline: "absent")
    monkeypatch.setattr(pu, "process_group_state", lambda pgid, deadline: "absent")
    assert manager._stop_session_groups(900001, [member, zombie], time.monotonic() + 1.0) is True
    assert signals == [], "positively dead members are never signalled"


def test_stop_session_groups_retained_pending_uncertainty_after_failed_observation(
    tmp_path, fake_aflow, monkeypatch
) -> None:
    """Pending uncertainty survives a failed observation plus anchor loss.

    A failed rescan leaves the uncertainty pending; after the anchor is lost
    a complete unanchored observation cannot clear it, so the stop stays
    unconfirmed even though the captured members are positively ceased.
    """
    from aflow.control_plane import persistent_units as pu
    from aflow.process_identity import SessionMember

    manager, signals = _signal_recording_manager(fake_aflow)
    anchor = SessionMember(pid=900001, birth="original", pgid=900001)
    birth = {900001: "original", 900011: None}
    clock = {"now": 100.0}
    monkeypatch.setattr(pu.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pu.time, "sleep", lambda s: clock.update(now=clock["now"] + s))
    snapshots = {"n": 0}

    def failing_then_unanchored(session_id, deadline):
        snapshots["n"] += 1
        if snapshots["n"] == 1:
            birth[900001] = None  # the anchor dies with the observation failing
            return None
        return [SessionMember(pid=900011, birth="replacement", pgid=900011)]

    monkeypatch.setattr(
        pu, "session_member_live",
        lambda member, session_id, deadline: birth.get(member.pid) is not None,
    )
    monkeypatch.setattr(manager, "_session_snapshot", failing_then_unanchored)
    monkeypatch.setattr(
        pu, "process_liveness",
        lambda pid, deadline: "present" if birth.get(pid) else "absent",
    )
    monkeypatch.setattr(
        pu, "process_group_state",
        lambda pgid, deadline: "present" if birth.get(pgid) else "absent",
    )
    assert manager._stop_session_groups(900001, [anchor], 102.0) is False
    assert signals == [[900001, "SIGTERM"]]
    assert clock["now"] <= 102.05, "the retained-uncertainty loop stays bounded"


# -- legacy escalation and bounded evidence ------------------------------------
# Fake-clock regressions: a genuine legacy TERM survivor is escalated to KILL
# inside the outer deadline, expired native evidence is never signal proof,
# and the stop contract never consumes the generic five-second probe.


def test_stop_controller_group_legacy_term_survivor_escalates_in_deadline(
    tmp_path, monkeypatch
) -> None:
    """A birth-stable legacy TERM survivor gets an in-deadline KILL.

    The controller ignores TERM for its whole phase; the KILL ownership proof
    and signal use a fresh budget bounded by the still-live outer deadline,
    and success is written only after positive cessation.
    """
    from aflow.control_plane import persistent_units as pu
    from aflow.control_plane.units import UnitState

    receipts, manager, name = _stop_fixture_unit(tmp_path, stop_timeout_seconds=10.0)
    clock = {"now": 100.0}
    signals: list[list[object]] = []
    manager._signal_group = lambda pgid, sig: signals.append(
        [pgid, sig.name, clock["now"]]
    )
    manager._observe = lambda r, deadline=None: UnitState(
        name=name, active_state="active", sub_state="running", main_pid=900001
    )
    monkeypatch.setattr(pu.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pu.time, "sleep", lambda s: clock.update(now=clock["now"] + s))
    monkeypatch.setattr(pu, "_process_alive", lambda pid, birth: True)
    monkeypatch.setattr(
        pu, "_bounded_process_alive", lambda pid, birth, deadline: True
    )
    monkeypatch.setattr(pu.os, "getsid", lambda pid: 900002)  # non-leader SID
    monkeypatch.setattr(pu.os, "getpgid", lambda pid: 900001)
    monkeypatch.setattr(
        pu, "controller_group_owned", lambda pid, birth, sid, deadline: True
    )
    # A genuine TERM survivor stays present for the entire TERM phase
    # (41 polls at 50 ms) and the post-phase check; positively absent only
    # after the KILL.
    liveness = iter(["present"] * 43 + ["absent"])
    monkeypatch.setattr(
        pu, "process_liveness", lambda pid, deadline=None: next(liveness, "absent")
    )
    terminal = manager.stop(name)
    assert terminal is not None and terminal.result == "stopped"
    assert [row[1] for row in signals] == ["SIGTERM", "SIGKILL"], (
        "TERM first, then a verified in-deadline KILL"
    )
    assert 100.0 < float(signals[1][2]) < 110.0, (
        "the KILL stays inside the outer deadline"
    )
    assert (receipts / "stopped.json").exists()


def test_stop_controller_group_identity_loss_before_kill_never_escalates(
    tmp_path, monkeypatch
) -> None:
    """A changed/missing identity before the KILL receives no KILL signal."""
    from aflow.control_plane import persistent_units as pu
    from aflow.control_plane.units import UnitState

    receipts, manager, name = _stop_fixture_unit(tmp_path, stop_timeout_seconds=1.0)
    signals: list[list[object]] = []
    manager._signal_group = lambda pgid, sig: signals.append([pgid, sig.name])
    manager._observe = lambda r, deadline=None: UnitState(
        name=name, active_state="active", sub_state="running", main_pid=900001
    )
    clock = {"now": 100.0}
    monkeypatch.setattr(pu.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pu.time, "sleep", lambda s: clock.update(now=clock["now"] + s))
    monkeypatch.setattr(pu, "_process_alive", lambda pid, birth: True)
    monkeypatch.setattr(
        pu, "_bounded_process_alive", lambda pid, birth, deadline: True
    )
    monkeypatch.setattr(pu.os, "getsid", lambda pid: 900002)  # non-leader SID
    monkeypatch.setattr(pu.os, "getpgid", lambda pid: 900001)
    owned = iter([True, False])  # proof before TERM passes; before KILL fails
    monkeypatch.setattr(
        pu, "controller_group_owned", lambda pid, birth, sid, deadline: next(owned, False)
    )
    monkeypatch.setattr(pu, "process_liveness", lambda pid, deadline=None: "present")
    with pytest.raises(PersistentUnitError, match="did not terminate"):
        manager.stop(name)
    assert signals == [[900001, "SIGTERM"]], "no KILL to a changed identity"
    assert not (receipts / "stopped.json").exists()


def test_stop_controller_group_expired_deadline_never_signals_late(
    tmp_path, monkeypatch
) -> None:
    """A native birth proof that finishes at the deadline issues no signal.

    The bounded proof consumes exactly the remaining budget (less than 50 ms
    here) and returns success at the deadline: the evidence is expired, so no
    TERM is issued and the stop is unconfirmed.
    """
    from aflow import process_identity as pi
    from aflow.control_plane import persistent_units as pu

    manager, signals = _signal_recording_manager(tmp_path / "aflow")
    clock = {"now": 100.0}
    probes: list[dict[str, object]] = []

    def late_ps(argv, **kwargs):
        probes.append({"argv": list(argv), "timeout": kwargs["timeout"]})
        clock["now"] += float(kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, "fixture-birth\n", "")

    monkeypatch.setattr(pi.sys, "platform", "darwin")
    monkeypatch.setattr(pu.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pu.time, "sleep", lambda s: clock.update(now=clock["now"] + s))
    monkeypatch.setattr(pi.os, "getsid", lambda pid: 900001)
    monkeypatch.setattr(pi.os, "getpgid", lambda pid: 900001)
    monkeypatch.setattr(pi.subprocess, "run", late_ps)
    result = manager._stop_controller_group(900001, "ps-lstart:fixture-birth", 100.05, 900001)
    assert result is False
    assert signals == [], "expired proof is never signal authority"
    assert probes, "a bounded probe was attempted"
    assert all(float(p["timeout"]) <= 0.05 for p in probes), (
        "no minimum floor above the remaining budget"
    )


def test_session_contract_darwin_observation_stays_in_budget(
    monkeypatch, tmp_path: Path
) -> None:
    """The stop contract's Darwin birth probe is bounded by the deadline.

    A stalling native birth probe consumes at most the remaining stop budget
    (never the generic five-second window) and the topology stays unknown.
    """
    from aflow import process_identity as pi
    from aflow.control_plane import persistent_units as pu

    manager = PersistentUnitManager(executable=tmp_path / "aflow")
    clock = {"now": 100.0}
    probes: list[dict[str, object]] = []

    def slow_ps(argv, **kwargs):
        probes.append({"argv": list(argv), "timeout": kwargs["timeout"]})
        clock["now"] += float(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(pi.sys, "platform", "darwin")
    monkeypatch.setattr(pu.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pi.os, "getsid", lambda pid: 900001)
    monkeypatch.setattr(pi.os, "getpgid", lambda pid: 900001)

    def read_missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(Path, "read_text", read_missing)
    monkeypatch.setattr(pi.subprocess, "run", slow_ps)
    contract = manager._session_contract(900001, "ps-lstart:fixture", 101.0)
    assert contract.topology == "unknown"
    assert clock["now"] - 100.0 <= 1.0, "the contract stays within the stop budget"
    assert probes and all(
        float(p["timeout"]) <= 1.0 for p in probes
    ), "no generic five-second probe in the stop contract"


def test_stop_session_groups_shared_group_proof_budget(
    monkeypatch, tmp_path: Path
) -> None:
    """Three uncertain members in one group share one bounded group proof.

    The multi-member group ownership proof shares the two-second operation
    budget: with a ten-second outer deadline the first group decision takes
    at most two seconds, no signal is issued, and no probe restarts the full
    two-second window per member.
    """
    from aflow import process_identity as pi
    from aflow.control_plane import persistent_units as pu

    manager, signals = _signal_recording_manager(tmp_path / "aflow")
    captured = [
        pi.SessionMember(pid, "ps-lstart:original", 900001)
        for pid in (900001, 900002, 900003)
    ]
    clock = {"now": 100.0}
    probes: list[dict[str, object]] = []
    first_anchor_at: list[float] = []
    original_anchor = manager._session_anchor

    def record_anchor(*args):
        first_anchor_at.append(clock["now"])
        return original_anchor(*args)

    def slow_ps(argv, **kwargs):
        probes.append({"at": clock["now"], "timeout": kwargs["timeout"]})
        clock["now"] += float(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    def sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(pi.sys, "platform", "darwin")
    monkeypatch.setattr(pi.os, "getsid", lambda pid: 900001)
    monkeypatch.setattr(pi.os, "getpgid", lambda pid: 900001)

    def read_missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(Path, "read_text", read_missing)
    monkeypatch.setattr(pi.subprocess, "run", slow_ps)
    monkeypatch.setattr(manager, "_session_anchor", record_anchor)

    def unanchored_inventory(session_id, deadline):
        return captured

    monkeypatch.setattr(manager, "_session_snapshot", unanchored_inventory)
    for module in (pi, pu):
        monkeypatch.setattr(module.time, "monotonic", lambda: clock["now"])
        monkeypatch.setattr(module.time, "sleep", sleep)
    result = manager._stop_session_groups(900001, captured, deadline=110.0)
    assert result is False
    assert signals == [], "an unproven group is never signalled"
    assert first_anchor_at, "a group ownership decision was attempted"
    assert first_anchor_at[0] - 100.0 <= 2.0, (
        "one group decision shares the two-second operation budget"
    )
    assert all(float(p["timeout"]) <= 2.0 for p in probes), (
        "no per-member two-second budget restarts"
    )


def test_stop_session_groups_inaccessible_linux_identity_fails_typed(
    monkeypatch, tmp_path: Path
) -> None:
    """Inaccessible Linux identity is unknown: no anchor, no signal, typed."""
    from aflow import process_identity as pi
    from aflow.control_plane import persistent_units as pu

    monkeypatch.setattr(pu, "_KILL_ESCALATION_SECONDS", 0.2)
    manager, signals = _signal_recording_manager(tmp_path / "aflow")
    captured = [
        pi.SessionMember(pid, "linux-start-ticks:1", pid) for pid in (900001, 900002)
    ]
    clock = {"now": 100.0}

    def sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(pi.sys, "platform", "linux")

    def read_inaccessible(*args, **kwargs):
        raise OSError("inaccessible")

    monkeypatch.setattr(Path, "read_text", read_inaccessible)
    monkeypatch.setattr(pi.os, "getsid", lambda pid: 900001)
    monkeypatch.setattr(pi.os, "getpgid", lambda pid: pid)
    for module in (pi, pu):
        monkeypatch.setattr(module.time, "monotonic", lambda: clock["now"])
        monkeypatch.setattr(module.time, "sleep", sleep)
    result = manager._stop_session_groups(900001, captured, deadline=101.0)
    assert result is False, "unknown identity retains typed failure"
    assert signals == []
    # At most one 50ms poll tick may overshoot the deadline; no probe may.
    assert clock["now"] - 100.0 <= 1.05, "no work beyond the shared budget"


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="procfs zombie liveness/absence contract is Linux-specific",
)
def test_owner_stop_ceases_unreaped_owned_zombie_without_signal(
    tmp_path, fake_aflow, repo, monkeypatch
) -> None:
    """A real fixture-owned unreaped zombie is positive cessation, not a member.

    The child exits in its own session and is deliberately held unreaped (a
    zombie) for the whole stop interval; the stop must succeed without
    signalling the zombie-only group, and the fixture reaps it in cleanup.
    """
    from aflow.process_identity import process_liveness, session_members

    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process_liveness(proc.pid) != "absent":
            time.sleep(0.01)
        assert process_liveness(proc.pid) == "absent"
        captured = session_members(proc.pid)
        assert captured is not None and len(captured) == 1, captured
        assert captured[0].pid == proc.pid and captured[0].birth is None
        manager, signals = _signal_recording_manager(fake_aflow)
        result = manager._stop_session_groups(proc.pid, captured, time.monotonic() + 2)
        assert result is True
        assert signals == [], "no signal to a zombie-only group"
    finally:
        proc.wait(timeout=5)  # explicit reap of the fixture-owned zombie


# -- stop entry and initial-proof deadline budgets -----------------------------
# The outer stop deadline exists before any stop-specific native identity
# observation: the entry observation/identity checks and the initial
# inventory's associated proofs share <=2s operation budgets that are never
# renewed, and an expired or unconfirmed identity fails typed without a
# successful stopped receipt.


def test_stop_entry_bounded_native_read_stays_in_one_second_stop_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 1s persistent stop entry never spends serial five-second probes.

    Each native Darwin birth read takes its full bounded timeout; with a
    one-second outer stop window the entry observation and identity recheck
    must stay inside that window and fail typed (unconfirmed identity) with
    no signal and no successful stopped receipt.
    """
    from aflow import process_identity as pi

    run_id = "20261006t000000z-00000100"
    unit = f"aflow-run-{run_id}.service"
    receipts = tmp_path / ".aflow" / "runs" / run_id / "units"
    receipts.mkdir(parents=True)
    (receipts / "start.json").write_text(
        json.dumps({"schema": 1, "run_id": run_id, "unit": unit, "nonce": "entry-v07"})
    )
    (receipts / "child.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "nonce": "entry-v07",
                "pid": 900001,
                "pgid": 900001,
                "process_birth": "ps-lstart:original",
            }
        )
    )
    manager = PersistentUnitManager(executable=Path("/fixture"), stop_timeout_seconds=1)
    manager._cwd_for = lambda name: tmp_path
    signals: list[object] = []
    manager._signal_group = lambda pgid, sig: signals.append((pgid, sig.name))

    clock = {"now": 100.0}
    probes: list[dict[str, object]] = []

    def native_ps(argv, **kwargs):
        # Each native birth read spends its full bounded timeout.
        probes.append({"at": clock["now"], "timeout": kwargs["timeout"]})
        clock["now"] += float(kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, "original\n", "")

    monkeypatch.setattr(pi.sys, "platform", "darwin")
    monkeypatch.setattr(pi.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pi.time, "sleep", lambda s: clock.update(now=clock["now"] + s))
    monkeypatch.setattr(pi.os, "getsid", lambda pid: 900001)
    monkeypatch.setattr(pi.os, "getpgid", lambda pid: 900001)
    monkeypatch.setattr(pi.subprocess, "run", native_ps)

    with pytest.raises(PersistentUnitError, match="unconfirmed"):
        manager.stop(unit)
    # Every entry probe is bounded by the one-second stop window; the whole
    # stop entry (observation + identity recheck) stays inside it.
    assert probes, "the entry identity check uses the bounded native probe"
    assert all(float(probe["timeout"]) <= 1.0 for probe in probes), (
        "no generic five-second probe consumes stop budget"
    )
    assert clock["now"] <= 101.0, "the 1s stop entry never exceeds its outer window"
    assert signals == [], "an unconfirmed identity receives no signal"
    assert not (receipts / "stopped.json").exists(), (
        "no successful stopped receipt without positive cessation"
    )


def test_stop_initial_snapshot_and_proof_share_one_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The initial inventory and its associated proofs share one <=2s budget.

    The v06 defect renewed the observation window after the initial
    inventory: the inventory, the original-controller revalidation, and the
    initial anchor/group proof each received the full outer stop deadline as
    a fresh two-second window.  The repair creates one absolute
    ``initial_operation = min(outer, now + 2s)`` before the first snapshot and
    threads that same value to the inventory, the controller revalidation, and
    the initial proof.  This test records the exact deadlines those three
    calls receive and asserts they are all the same shared value bounded by
    ``stop_start + 2s`` (never the larger outer deadline).
    """
    run_id = "20261006t000000z-00000101"
    unit = f"aflow-run-{run_id}.service"
    receipts = tmp_path / ".aflow" / "runs" / run_id / "units"
    receipts.mkdir(parents=True)
    (receipts / "start.json").write_text(
        json.dumps({"schema": 1, "run_id": run_id, "unit": unit, "nonce": "init-v07"})
    )
    (receipts / "child.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "nonce": "init-v07",
                "pid": 900001,
                "pgid": 900001,
                "process_birth": "ps-lstart:original",
            }
        )
    )
    manager = PersistentUnitManager(executable=Path("/fixture"), stop_timeout_seconds=10)
    manager._cwd_for = lambda name: tmp_path
    manager._signal_group = lambda pgid, sig: None

    clock = {"now": 100.0}
    seen: dict[str, float] = {}

    def native_ps(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "original\n", "")

    def snapshot(session_id, deadline):
        seen["snapshot"] = deadline
        return [pi.SessionMember(900001, "ps-lstart:original", 900001)]

    def still_owned(pid, birth, session_id, deadline):
        seen["controller"] = deadline
        return True

    def stop_groups(session_id, captured, deadline, **kwargs):
        seen["groups_outer"] = deadline
        seen["groups_initial"] = kwargs.get("initial_operation")
        return True

    from aflow import process_identity as pi

    monkeypatch.setattr(pi.sys, "platform", "darwin")
    monkeypatch.setattr(pi.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pi.time, "sleep", lambda s: clock.update(now=clock["now"] + s))
    monkeypatch.setattr(pi.os, "getsid", lambda pid: 900001)
    monkeypatch.setattr(pi.os, "getpgid", lambda pid: 900001)
    monkeypatch.setattr(pi.subprocess, "run", native_ps)
    monkeypatch.setattr(manager, "_session_snapshot", snapshot)
    monkeypatch.setattr(manager, "_controller_group_still_owned", still_owned)
    monkeypatch.setattr(manager, "_stop_session_groups", stop_groups)

    state = manager.stop(unit)
    assert state is not None and state.result == "stopped"

    # All three initial-proof calls receive the one shared initial operation,
    # bounded by stop_start + 2s, never the 10s outer deadline.
    assert seen["snapshot"] == seen["controller"] == seen["groups_initial"], (
        "inventory, controller revalidation, and initial proof share one deadline"
    )
    assert seen["groups_initial"] <= 102.0, "the shared initial budget is <=2s"
    assert seen["groups_initial"] < seen["groups_outer"], (
        "the initial proof is not given the full outer deadline"
    )


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


# -- stop-specific bounded startup observation (v08) ---------------------------
# With a nonce-bound start.json, a recorded live wrapper, no trusted child
# receipt yet, and no terminal receipt, a stop entry must never return
# inactive merely because the wrapper birth probe timed out or its identity
# is unavailable: expired/unknown/reused wrapper identity fails typed with
# no signal and no stopped.json inside the original entry operation.  Only
# positive wrapper absence keeps the genuine startup_lost answer, a
# positively observed live wrapper keeps active start-post, and trusted
# terminal receipts remain authoritative.  Ordinary `get` keeps the generic
# observation contract.


def _startup_unit(tmp_path: Path, run_id: str, start_payload: dict) -> tuple[str, Path]:
    unit = f"aflow-run-{run_id}.service"
    receipts = tmp_path / ".aflow" / "runs" / run_id / "units"
    receipts.mkdir(parents=True)
    (receipts / "start.json").write_text(json.dumps(start_payload), encoding="utf-8")
    return unit, receipts


def _startup_stop_manager(
    tmp_path: Path, signals: list[object]
) -> PersistentUnitManager:
    manager = PersistentUnitManager(executable=Path("/fixture"), stop_timeout_seconds=1)
    manager._cwd_for = lambda name: tmp_path
    manager._signal_group = lambda pgid, sig: signals.append((pgid, sig.name))
    return manager


def _patch_darwin_clock(monkeypatch, clock: dict[str, float]) -> None:
    from aflow import process_identity as pi

    monkeypatch.setattr(pi.sys, "platform", "darwin")
    monkeypatch.setattr(pi.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pi.time, "sleep", lambda s: clock.update(now=clock["now"] + s))


def test_stop_startup_bounded_birth_probe_timeout_fails_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out wrapper birth probe is unconfirmed, never inactive.

    The live wrapper's native birth probe takes 1.2s: it fits the generic
    five-second observation (get stays active start-post) but exhausts the
    one-second stop entry.  Stop must fail typed inside the original entry
    operation with no signal and no stopped.json, not return a false
    inactive/startup_lost while the wrapper can still finish startup.
    """
    from aflow import process_identity as pi

    unit, receipts = _startup_unit(
        tmp_path,
        "20261007t000000z-00000201",
        {
            "schema": 1,
            "run_id": "20261007t000000z-00000201",
            "unit": "aflow-run-20261007t000000z-00000201.service",
            "nonce": "startup-v08",
            "wrapper_pid": 900001,
            "wrapper_birth": "ps-lstart:original",
        },
    )
    manager = _startup_stop_manager(tmp_path, signals := [])
    clock = {"now": 100.0}
    probes: list[float] = []

    def native_ps(argv, **kwargs):
        # A live native birth probe completes in 1.2s: the generic
        # observation succeeds; the one-second stop probe times out.
        probes.append(float(kwargs["timeout"]))
        if kwargs["timeout"] < 1.2:
            clock["now"] += float(kwargs["timeout"])
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        clock["now"] += 1.2
        return subprocess.CompletedProcess(argv, 0, "original\n", "")

    _patch_darwin_clock(monkeypatch, clock)
    monkeypatch.setattr(pi.subprocess, "run", native_ps)

    before = manager.get(unit)
    assert before is not None and before.is_active and before.sub_state == "start-post"
    clock["now"] = 100.0
    probes.clear()

    with pytest.raises(PersistentUnitError, match="unconfirmed"):
        manager.stop(unit)
    assert probes, "the entry startup check uses the bounded native probe"
    assert all(timeout <= 1.0 for timeout in probes), (
        "no generic five-second probe consumes stop budget"
    )
    assert clock["now"] <= 101.0, "the 1s stop entry never exceeds its outer window"
    assert signals == [], "an unconfirmed startup identity receives no signal"
    assert not (receipts / "stopped.json").exists(), (
        "no successful stopped receipt without positive cessation"
    )


def test_stop_startup_bounded_missing_wrapper_identity_fails_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start.json without wrapper identity is unconfirmed, not lost."""
    unit, receipts = _startup_unit(
        tmp_path,
        "20261007t000000z-00000202",
        {
            "schema": 1,
            "run_id": "20261007t000000z-00000202",
            "unit": "aflow-run-20261007t000000z-00000202.service",
            "nonce": "startup-v08",
        },
    )
    manager = _startup_stop_manager(tmp_path, signals := [])
    clock = {"now": 100.0}
    probes: list[float] = []

    def native_ps(argv, **kwargs):
        probes.append(float(kwargs["timeout"]))
        return subprocess.CompletedProcess(argv, 0, "original\n", "")

    _patch_darwin_clock(monkeypatch, clock)
    from aflow import process_identity as pi

    monkeypatch.setattr(pi.subprocess, "run", native_ps)

    with pytest.raises(PersistentUnitError, match="unconfirmed"):
        manager.stop(unit)
    assert probes == [], "a missing wrapper identity is never probed or signalled"
    assert signals == []
    assert not (receipts / "stopped.json").exists()


def test_stop_startup_bounded_malformed_wrapper_identity_fails_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed recorded wrapper identity is unconfirmed, not lost."""
    unit, receipts = _startup_unit(
        tmp_path,
        "20261007t000000z-00000203",
        {
            "schema": 1,
            "run_id": "20261007t000000z-00000203",
            "unit": "aflow-run-20261007t000000z-00000203.service",
            "nonce": "startup-v08",
            "wrapper_pid": "not-a-pid",
            "wrapper_birth": "ps-lstart:original",
        },
    )
    manager = _startup_stop_manager(tmp_path, signals := [])
    clock = {"now": 100.0}
    probes: list[float] = []

    def native_ps(argv, **kwargs):
        probes.append(float(kwargs["timeout"]))
        return subprocess.CompletedProcess(argv, 0, "original\n", "")

    _patch_darwin_clock(monkeypatch, clock)
    from aflow import process_identity as pi

    monkeypatch.setattr(pi.subprocess, "run", native_ps)

    with pytest.raises(PersistentUnitError, match="unconfirmed"):
        manager.stop(unit)
    assert probes == [], "a malformed wrapper identity is unconfirmed without probing"
    assert signals == []
    assert not (receipts / "stopped.json").exists()


def test_stop_startup_bounded_reused_wrapper_pid_fails_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A still-present PID whose birth no longer matches is unconfirmed."""
    unit, receipts = _startup_unit(
        tmp_path,
        "20261007t000000z-00000204",
        {
            "schema": 1,
            "run_id": "20261007t000000z-00000204",
            "unit": "aflow-run-20261007t000000z-00000204.service",
            "nonce": "startup-v08",
            "wrapper_pid": 900001,
            "wrapper_birth": "ps-lstart:original",
        },
    )
    manager = _startup_stop_manager(tmp_path, signals := [])
    clock = {"now": 100.0}
    probes: list[float] = []

    def native_ps(argv, **kwargs):
        # The PID is still present but belongs to a different process.
        probes.append(float(kwargs["timeout"]))
        clock["now"] += 0.1
        return subprocess.CompletedProcess(argv, 0, "reused\n", "")

    _patch_darwin_clock(monkeypatch, clock)
    from aflow import process_identity as pi

    monkeypatch.setattr(pi.subprocess, "run", native_ps)

    with pytest.raises(PersistentUnitError, match="unconfirmed"):
        manager.stop(unit)
    assert probes and all(timeout <= 1.0 for timeout in probes)
    assert clock["now"] <= 101.0
    assert signals == [], "a reused wrapper PID receives no signal"
    assert not (receipts / "stopped.json").exists()


def test_stop_startup_bounded_genuinely_absent_wrapper_returns_startup_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive native wrapper absence keeps the genuine startup_lost stop."""
    unit, receipts = _startup_unit(
        tmp_path,
        "20261007t000000z-00000205",
        {
            "schema": 1,
            "run_id": "20261007t000000z-00000205",
            "unit": "aflow-run-20261007t000000z-00000205.service",
            "nonce": "startup-v08",
            "wrapper_pid": 900001,
            "wrapper_birth": "ps-lstart:original",
        },
    )
    manager = _startup_stop_manager(tmp_path, signals := [])
    clock = {"now": 100.0}
    probes: list[float] = []

    def native_ps(argv, **kwargs):
        # The native probe positively reports no such process.
        probes.append(float(kwargs["timeout"]))
        clock["now"] += 0.05
        return subprocess.CompletedProcess(argv, 1, "", "")

    _patch_darwin_clock(monkeypatch, clock)
    from aflow import process_identity as pi

    monkeypatch.setattr(pi.subprocess, "run", native_ps)

    result = manager.stop(unit)
    assert result is not None
    assert result.is_active is False and result.result == "startup_lost"
    assert probes and all(timeout <= 1.0 for timeout in probes)
    assert signals == []
    assert not (receipts / "stopped.json").exists()


def test_stop_startup_bounded_live_wrapper_keeps_active_start_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A positively observed live starting wrapper stays active start-post."""
    unit, _ = _startup_unit(
        tmp_path,
        "20261007t000000z-00000206",
        {
            "schema": 1,
            "run_id": "20261007t000000z-00000206",
            "unit": "aflow-run-20261007t000000z-00000206.service",
            "nonce": "startup-v08",
            "wrapper_pid": 900001,
            "wrapper_birth": "ps-lstart:original",
        },
    )
    manager = _startup_stop_manager(tmp_path, signals := [])
    clock = {"now": 100.0}

    def native_ps(argv, **kwargs):
        clock["now"] += 0.2
        return subprocess.CompletedProcess(argv, 0, "original\n", "")

    _patch_darwin_clock(monkeypatch, clock)
    from aflow import process_identity as pi

    monkeypatch.setattr(pi.subprocess, "run", native_ps)

    result = manager.stop(unit)
    assert result is not None
    assert result.is_active and result.sub_state == "start-post"
    assert clock["now"] <= 101.0
    assert signals == [], "stop never signals the still-starting wrapper"
    assert not (tmp_path / ".aflow" / "runs" / "20261007t000000z-00000206"
                / "units" / "stopped.json").exists()


def test_stop_startup_bounded_trusted_terminal_receipts_authorized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nonce-bound stopped/exit receipts remain authoritative stop answers."""
    from aflow import process_identity as pi

    unit, receipts = _startup_unit(
        tmp_path,
        "20261007t000000z-00000207",
        {
            "schema": 1,
            "run_id": "20261007t000000z-00000207",
            "unit": "aflow-run-20261007t000000z-00000207.service",
            "nonce": "startup-v08",
            "wrapper_pid": 900001,
            "wrapper_birth": "ps-lstart:original",
            "started_at": "2026-01-01T00:00:00Z",
        },
    )
    manager = _startup_stop_manager(tmp_path, signals := [])
    clock = {"now": 100.0}
    probes: list[float] = []

    def native_ps(argv, **kwargs):
        probes.append(float(kwargs["timeout"]))
        return subprocess.CompletedProcess(argv, 0, "original\n", "")

    _patch_darwin_clock(monkeypatch, clock)
    monkeypatch.setattr(pi.subprocess, "run", native_ps)

    (receipts / "stopped.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "nonce": "startup-v08",
                "at": "2026-01-01T00:00:05Z",
                "result": "stopped",
            }
        ),
        encoding="utf-8",
    )
    result = manager.stop(unit)
    assert result is not None
    assert result.is_active is False and result.result == "stopped"
    assert probes == [], "a trusted stopped receipt needs no wrapper probe"
    assert signals == []

    (receipts / "stopped.json").unlink()
    (receipts / "exit.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "nonce": "startup-v08",
                "returncode": 3,
                "at": "2026-01-01T00:00:06Z",
            }
        ),
        encoding="utf-8",
    )
    result = manager.stop(unit)
    assert result is not None
    assert result.is_active is False and result.result == "exit:3"
    assert probes == [], "a trusted exit receipt needs no wrapper probe"
    assert signals == []


def test_get_startup_retains_generic_observation_without_bounded_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordinary get keeps the generic wrapper observation contract."""
    from aflow import process_identity as pi

    unit, _ = _startup_unit(
        tmp_path,
        "20261007t000000z-00000208",
        {
            "schema": 1,
            "run_id": "20261007t000000z-00000208",
            "unit": "aflow-run-20261007t000000z-00000208.service",
            "nonce": "startup-v08",
            "wrapper_pid": 900001,
            "wrapper_birth": "ps-lstart:original",
        },
    )
    manager = PersistentUnitManager(executable=Path("/fixture"))
    manager._cwd_for = lambda name: tmp_path
    clock = {"now": 100.0}

    def native_ps(argv, **kwargs):
        # The generic probe takes its full five-second window.
        clock["now"] += float(kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, "original\n", "")

    _patch_darwin_clock(monkeypatch, clock)
    monkeypatch.setattr(pi.subprocess, "run", native_ps)

    state = manager.get(unit)
    assert state is not None
    assert state.is_active and state.sub_state == "start-post"
