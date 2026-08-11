from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from aflow.daemon import DaemonError
from aflow.daemon_cli import (
    DaemonPidRecord,
    _claim_pidfile,
    _owned_run_processes,
    _release_pidfile,
    daemon_status,
    daemon_stop,
    pidfile_path,
)


def test_pidfile_is_atomic_private_and_rejects_second_owner(tmp_path: Path) -> None:
    record = _claim_pidfile(tmp_path)
    try:
        path = pidfile_path(tmp_path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()
        assert daemon_status(tmp_path) == 0
        with pytest.raises(DaemonError, match="already running"):
            _claim_pidfile(tmp_path)
    finally:
        assert _release_pidfile(tmp_path, record)
    assert not pidfile_path(tmp_path).exists()


def test_stop_refuses_malformed_or_reused_pid_identity(tmp_path: Path) -> None:
    path = pidfile_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not-json\n", encoding="utf-8")
    assert daemon_stop(tmp_path, stop_timeout_seconds=0) == 2
    assert path.read_text(encoding="utf-8") == "not-json\n"

    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "process_birth": "definitely-not-this-process",
                "repo_root": str(tmp_path.resolve()),
            }
        ),
        encoding="utf-8",
    )
    assert daemon_stop(tmp_path, stop_timeout_seconds=0) == 2
    assert path.exists()


def test_stop_removes_provably_dead_stale_pidfile(tmp_path: Path) -> None:
    path = pidfile_path(tmp_path)
    path.parent.mkdir(parents=True)
    record = DaemonPidRecord(
        pid=2_000_000_000,
        process_birth="linux-start-ticks:1",
        repo_root=str(tmp_path.resolve()),
    )
    path.write_text(json.dumps(record.__dict__), encoding="utf-8")
    assert daemon_stop(tmp_path, stop_timeout_seconds=0) == 1
    assert not path.exists()


def test_status_reports_only_direct_owned_daemon_workers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record = _claim_pidfile(tmp_path)
    worker = subprocess.Popen(
        (
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            "daemon-worker",
            "--repo-root",
            str(tmp_path.resolve()),
            "--run-id",
            "owned-run",
        )
    )
    unrelated = (tmp_path / ".aflow" / "runs" / "legacy-run")
    unrelated.mkdir(parents=True)
    try:
        assert _owned_run_processes(os.getpid(), tmp_path) == (("owned-run", worker.pid),)
        assert daemon_status(tmp_path) == 0
        output = capsys.readouterr().out
        assert "owned runs: 1" in output
        assert f"owned-run pid={worker.pid}" in output
        assert "legacy-run" not in output
    finally:
        worker.terminate()
        worker.wait(timeout=2)
        _release_pidfile(tmp_path, record)


def test_fastmcp_stdio_initializes_lists_tools_and_exits_on_input_close(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config_path = tmp_path / "aflow.toml"
    config_path.write_text("", encoding="utf-8")
    stderr_log = tmp_path / "daemon.stderr"
    transport = StdioTransport(
        command=sys.executable,
        args=[
            "-m",
            "aflow.cli",
            "daemon",
            "start",
            "--foreground",
            "--repo-root",
            str(repo_root),
            "--config",
            str(config_path),
            "--mcp-transport",
            "stdio",
            "--poll-interval",
            "0.05",
            "--stop-timeout",
            "0",
        ],
        env={"PYTHONPATH": str(Path(__file__).parents[1])},
        cwd=str(repo_root),
        keep_alive=False,
        log_file=stderr_log,
    )

    async def exercise() -> None:
        async with Client(transport, timeout=10) as client:
            tools = await client.list_tools()
            assert len(tools) == 13
            assert {tool.name for tool in tools} >= {"get_capabilities", "start_run"}
            result = await client.call_tool("get_capabilities")
            assert result.is_error is False

    asyncio.run(exercise())
    deadline = time.monotonic() + 2.0
    while pidfile_path(repo_root).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not pidfile_path(repo_root).exists()
    assert "Daemon ready" in stderr_log.read_text(encoding="utf-8")
