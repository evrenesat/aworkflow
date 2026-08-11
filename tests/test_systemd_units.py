from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import time

import pytest

from aflow.control_plane import SubprocessUnitManager, SystemdUnitManager


def test_systemd_workflow_unit_uses_fixed_argv_restart_never_and_environment_file(
    tmp_path: Path,
) -> None:
    environment_file = tmp_path / "aflowd.env"
    environment_file.write_text("AFLOW_TOKEN=not-inspected-here\n")
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[0] == "systemd-run":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(
            argv,
            0,
            "Id=aflow-run-owned-run.service\nActiveState=active\nSubState=running\n",
            "",
        )

    state = SystemdUnitManager(runner=runner).start(
        "aflow-run-owned-run.service",
        ("/opt/aflowd/releases/test/bin/aflow", "daemon-worker", "--run-id", "owned-run"),
        cwd=tmp_path,
        environment_file=environment_file,
        environment={"LANG": "C", "PATH": "/opt/aflowd/releases/test/bin"},
    )

    assert state.is_active
    command = calls[0]
    assert command[:4] == (
        "systemd-run",
        "--unit=aflow-run-owned-run.service",
        "--property=Restart=no",
        "--collect",
    )
    assert f"--working-directory={tmp_path.resolve()}" in command
    assert f"--property=EnvironmentFile={environment_file.resolve()}" in command
    assert "--setenv=LANG=C" in command
    assert "--setenv=PATH=/opt/aflowd/releases/test/bin" in command
    assert "--property=Restart=always" not in command
    assert command[-4:] == ("/opt/aflowd/releases/test/bin/aflow", "daemon-worker", "--run-id", "owned-run")


def test_systemd_workflow_unit_rejects_unsafe_environment_and_identity(tmp_path: Path) -> None:
    manager = SystemdUnitManager(runner=lambda *args, **kwargs: subprocess.CompletedProcess((), 0, "", ""))

    with pytest.raises(ValueError, match="aflow-run"):
        manager.start("other.service", ("/bin/true",), cwd=tmp_path)
    with pytest.raises(ValueError, match="environment"):
        manager.start(
            "aflow-run-owned-run.service",
            ("/bin/true",),
            cwd=tmp_path,
            environment={"bad-key": "value"},
        )


def test_subprocess_unit_tracks_exact_process_and_environment(tmp_path: Path) -> None:
    output = tmp_path / "value.txt"
    environment_file = tmp_path / "daemon.env"
    environment_file.write_text("FROM_FILE=one\n", encoding="utf-8")
    manager = SubprocessUnitManager(stop_timeout_seconds=1.0)
    state = manager.start(
        "aflow-run-owned-run.service",
        (
            sys.executable,
            "-c",
            "import os,pathlib,time; "
            f"pathlib.Path({str(output)!r}).write_text(os.environ['FROM_FILE'] + os.environ['FROM_OVERLAY']); "
            "time.sleep(30)",
        ),
        cwd=tmp_path,
        environment_file=environment_file,
        environment={"FROM_OVERLAY": "two"},
    )
    try:
        assert state.is_active
        assert state.main_pid is not None
        assert os.getpgid(state.main_pid) == state.main_pid
        deadline = time.monotonic() + 2.0
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert output.read_text(encoding="utf-8") == "onetwo"
        assert manager.get(state.name) == state
    finally:
        terminal = manager.stop(state.name)
    assert terminal is not None
    assert terminal.active_state == "inactive"
    assert manager.get(state.name) is None


def test_subprocess_unit_shutdown_escalates_and_reaps_all(tmp_path: Path) -> None:
    manager = SubprocessUnitManager(stop_timeout_seconds=0.05)
    states = []
    try:
        for run_id in ("first", "second"):
            states.append(
                manager.start(
                    f"aflow-run-{run_id}.service",
                    (
                        sys.executable,
                        "-c",
                        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
                    ),
                    cwd=tmp_path,
                )
            )
        time.sleep(0.1)
        terminal = manager.shutdown()
    finally:
        manager.shutdown()
    assert {state.name for state in terminal} == {
        "aflow-run-first.service",
        "aflow-run-second.service",
    }
    assert all(state.active_state == "inactive" for state in terminal)
    for state in states:
        assert state.main_pid is not None
        with pytest.raises(ProcessLookupError):
            os.kill(state.main_pid, 0)


def test_subprocess_unit_drains_group_after_worker_leader_exits(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    manager = SubprocessUnitManager(stop_timeout_seconds=0.2)
    state = manager.start(
        "aflow-run-crashed-worker.service",
        (
            sys.executable,
            "-c",
            "import pathlib,subprocess; "
            "child=subprocess.Popen(['/bin/sleep','30']); "
            f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))",
        ),
        cwd=tmp_path,
    )
    try:
        deadline = time.monotonic() + 2.0
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2.0
        while manager.get(state.name).is_active and time.monotonic() < deadline:
            time.sleep(0.01)
        assert manager.get(state.name).active_state == "inactive"
        terminal = manager.stop(state.name)
    finally:
        manager.shutdown()
    assert terminal is not None
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("worker descendant survived process-group drain")


def test_subprocess_unit_rejects_invalid_identity_and_environment_file(tmp_path: Path) -> None:
    manager = SubprocessUnitManager()
    environment_file = tmp_path / "daemon.env"
    environment_file.write_text("VALUE=one\n", encoding="utf-8")
    symlink = tmp_path / "daemon-link.env"
    symlink.symlink_to(environment_file)
    with pytest.raises(ValueError, match="aflow-run"):
        manager.start("aflow-run-bad_name.service", ("/bin/true",), cwd=tmp_path)
    with pytest.raises(ValueError, match="non-symlink"):
        manager.start(
            "aflow-run-valid.service",
            ("/bin/true",),
            cwd=tmp_path,
            environment_file=symlink,
        )
