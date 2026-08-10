from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from aflow.control_plane import SystemdUnitManager


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
