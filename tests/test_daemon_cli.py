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

import aflow.daemon_cli as daemon_cli
from aflow.daemon import DaemonError
from aflow.daemon_cli import (
    DaemonPidRecord,
    _claim_pidfile,
    _owned_run_processes,
    _owned_run_processes_from_ps,
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
    config_path = tmp_path / "aflow.toml"
    worker = subprocess.Popen(
        (
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            "daemon-worker",
            "--repo-root",
            str(tmp_path.resolve()),
            "--config",
            str(config_path.resolve()),
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


def _ps_row(pid: int, ppid: int, command: str) -> str:
    return f"{pid} {ppid} {command}"


def _worker_command(repo_root: Path, config_path: Path, run_id: str) -> str:
    return (
        f"/usr/bin/aflow daemon-worker --repo-root {repo_root} "
        f"--config {config_path} --run-id {run_id}"
    )


def test_owned_run_processes_uses_real_ps_fallback_with_spaced_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo with spaces"
    repo_root.mkdir()
    config_path = tmp_path / "config with spaces" / "aflow.toml"
    config_path.parent.mkdir()
    config_path.write_text("", encoding="utf-8")
    worker = subprocess.Popen(
        (
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            "daemon-worker",
            "--repo-root",
            str(repo_root.resolve()),
            "--config",
            str(config_path.resolve()),
            "--run-id",
            "portable-run",
        )
    )
    monkeypatch.setattr(daemon_cli, "_owned_run_processes_from_proc", lambda *_: None)
    try:
        assert _owned_run_processes(os.getpid(), repo_root) == (
            ("portable-run", worker.pid),
        )
    finally:
        worker.terminate()
        worker.wait(timeout=2)


def test_ps_parser_returns_sorted_direct_owned_workers_and_ignores_other_shapes(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo with spaces"
    config_path = tmp_path / "config with spaces" / "aflow.toml"
    other_root = tmp_path / "other repo"
    output = "\n".join(
        (
            _ps_row(500, 1, "/usr/bin/aflow daemon start"),
            _ps_row(
                503,
                500,
                _worker_command(repo_root.resolve(), config_path.resolve(), "z-run"),
            ),
            _ps_row(
                501,
                500,
                _worker_command(repo_root.resolve(), config_path.resolve(), "a-run"),
            ),
            _ps_row(
                504,
                499,
                _worker_command(
                    repo_root.resolve(), config_path.resolve(), "not-direct"
                ),
            ),
            _ps_row(505, 500, "/bin/sh -c sleep 30"),
            _ps_row(
                506, 500, "/usr/bin/aflow run --repo-root /tmp/legacy --run-id legacy"
            ),
            _ps_row(
                507,
                500,
                _worker_command(
                    other_root.resolve(), config_path.resolve(), "other-run"
                ),
            ),
        )
    )

    assert _owned_run_processes_from_ps(output, 500, repo_root) == (
        ("a-run", 501),
        ("z-run", 503),
    )


@pytest.mark.parametrize(
    "bad_row",
    (
        "not a process row",
        "500 1",
        "0 1 command",
        "500 -1 command",
        "pid 1 command",
        "500 parent command",
        "500 1",
    ),
)
def test_ps_parser_rejects_malformed_process_rows(tmp_path: Path, bad_row: str) -> None:
    output = "\n".join((_ps_row(500, 1, "daemon"), bad_row))
    with pytest.raises(DaemonError, match="ambiguous"):
        _owned_run_processes_from_ps(output, 500, tmp_path)


def test_ps_parser_rejects_duplicate_pid_and_missing_daemon_row(tmp_path: Path) -> None:
    duplicate = "\n".join(
        (
            _ps_row(500, 1, "daemon"),
            _ps_row(501, 500, "child"),
            _ps_row(501, 500, "duplicate"),
        )
    )
    missing = _ps_row(501, 500, "child")
    for output in (duplicate, missing):
        with pytest.raises(DaemonError, match="ambiguous"):
            _owned_run_processes_from_ps(output, 500, tmp_path)


@pytest.mark.parametrize(
    "command",
    (
        "/usr/bin/aflow daemon-worker --config /tmp/config --run-id valid",
        "/usr/bin/aflow daemon-worker --repo-root /tmp/repo --run-id valid",
        "/usr/bin/aflow daemon-worker --config /tmp/config --repo-root /tmp/repo --run-id valid",
        "/usr/bin/aflow daemon-worker --repo-root /tmp/repo --config /tmp/config",
        "/usr/bin/aflow daemon-worker --unexpected value --repo-root /tmp/repo --config /tmp/config --run-id valid",
        "/usr/bin/aflow daemon-worker --repo-root /tmp/repo --config /tmp/config --run-id valid --extra",
        "/usr/bin/aflow daemon-worker --repo-root /tmp/repo --config relative.toml --run-id valid",
        "/usr/bin/aflow daemon-worker --repo-root relative-repo --config /tmp/config --run-id valid",
        "/usr/bin/aflow daemon-worker --repo-root /tmp/repo --config /tmp/config --run-id INVALID",
        "/usr/bin/aflow daemon-worker /tmp/repo --repo-root /tmp/repo --config /tmp/config --run-id valid",
        "/usr/bin/aflow daemon-worker --repo-root /tmp/repo --config /tmp/config --run-id valid daemon-worker",
        "/usr/bin/aflow daemon-worker --repo-root /tmp/repo --config /tmp/repo --config /tmp/config --run-id valid",
    ),
)
def test_ps_parser_rejects_malformed_direct_worker_candidates(
    tmp_path: Path, command: str
) -> None:
    output = "\n".join((_ps_row(500, 1, "daemon"), _ps_row(501, 500, command)))
    with pytest.raises(DaemonError, match="ambiguous"):
        _owned_run_processes_from_ps(output, 500, tmp_path)


def test_ps_parser_rejects_marker_like_path_contents(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo --config embedded"
    command = _worker_command(repo_root, tmp_path / "config.toml", "valid")
    output = "\n".join((_ps_row(500, 1, "daemon"), _ps_row(501, 500, command)))
    with pytest.raises(DaemonError, match="ambiguous"):
        _owned_run_processes_from_ps(output, 500, tmp_path)


@pytest.mark.parametrize(
    "failure",
    (
        FileNotFoundError("secret process output"),
        subprocess.TimeoutExpired("ps", 5, output="secret process output"),
    ),
)
def test_ps_runner_maps_expected_failures_to_bounded_errors(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    def fake_run(*args: object, **kwargs: object) -> object:
        assert args == (("ps", "-ww", "-axo", "pid=,ppid=,command="),)
        assert kwargs == {
            "check": False,
            "capture_output": True,
            "text": True,
            "timeout": 5.0,
        }
        raise failure

    monkeypatch.setattr(daemon_cli.subprocess, "run", fake_run)
    with pytest.raises(DaemonError) as caught:
        daemon_cli._read_ps_process_table()
    assert "secret process output" not in str(caught.value)


def test_ps_runner_maps_nonzero_and_empty_results_to_bounded_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args[0], 1, stdout="secret process output", stderr="secret"
        )

    monkeypatch.setattr(daemon_cli.subprocess, "run", fake_run)
    with pytest.raises(DaemonError) as caught:
        daemon_cli._read_ps_process_table()
    assert "secret process output" not in str(caught.value)

    monkeypatch.setattr(
        daemon_cli.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="", stderr=""
        ),
    )
    monkeypatch.setattr(daemon_cli, "_owned_run_processes_from_proc", lambda *_: None)
    with pytest.raises(DaemonError, match="ambiguous"):
        _owned_run_processes(os.getpid(), tmp_path)


def test_status_is_ambiguous_when_portable_inventory_is_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _claim_pidfile(tmp_path)
    monkeypatch.setattr(
        daemon_cli,
        "_owned_run_processes_from_proc",
        lambda *_: None,
    )
    monkeypatch.setattr(
        daemon_cli,
        "_read_ps_process_table",
        lambda: (_ for _ in ()).throw(DaemonError("ps process table unavailable")),
    )
    try:
        assert daemon_status(tmp_path) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("daemon status is ambiguous:")
        assert "owned runs: 0" not in captured.err
    finally:
        _release_pidfile(tmp_path, record)


def test_status_is_ambiguous_when_daemon_identity_changes_during_inventory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _claim_pidfile(tmp_path)
    states = iter(("matching", "mismatched"))
    monkeypatch.setattr(daemon_cli, "_record_process_state", lambda _: next(states))
    monkeypatch.setattr(daemon_cli, "_owned_run_processes", lambda *_: ())
    try:
        assert daemon_status(tmp_path) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == (
            "daemon status is ambiguous: daemon identity changed during worker inspection\n"
        )
        assert "owned runs: 0" not in captured.err
    finally:
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
