"""Tests for the ``aflow ui`` launcher.

Covers first-run setup (interactive and non-TTY), the ownership record,
status/stop semantics, duplicate-start reporting, and a full background
start → health → stop lifecycle against a real server on a private port.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from aflow import ui_cli
from aflow.installation import detect_installation


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(ui_cli, "READY_WAIT_SECONDS", 30.0)
    # Under pytest, sys.argv[0] resolution would pick the test runner; pin the
    # installation to the real aflow entry point for spawned children.
    from aflow.installation import Installation

    venv_aflow = Path(sys.executable).parent / "aflow"
    monkeypatch.setattr(
        ui_cli, "detect_installation",
        lambda: Installation(
            kind="wheel", checkout_root=None,
            release_identity="test", executable=venv_aflow,
        ),
    )
    return home


def _write_config(home: Path, *, token: str = "test-token-123", root: str = "~/code", bind_host: str | None = None) -> Path:
    from aflow_app_server.config import update_global_settings

    return update_global_settings(
        home / ".config" / "aflow",
        auth_token=token,
        managed_projects_root=root,
        bind_host=bind_host,
    )


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestFirstRunSetup:
    def test_missing_credentials_without_tty_fails_with_exact_instructions(self, home, capsys) -> None:
        with pytest.raises(ui_cli.UIError) as excinfo:
            ui_cli.run_first_run_setup(home / ".config" / "aflow", interactive=False)
        message = str(excinfo.value)
        assert "auth_token" in message
        assert "managed_projects_root" in message
        assert str(home / ".config" / "aflow" / "config.toml") in message

    def test_interactive_setup_writes_token_root_and_explicit_bind_host(self, home, monkeypatch) -> None:
        config_dir = home / ".config" / "aflow"
        monkeypatch.setattr("builtins.input", lambda *a: "")
        import getpass

        passwords = iter(["secret-password", "secret-password"])
        monkeypatch.setattr(getpass, "getpass", lambda *a: next(passwords))
        ui_cli.run_first_run_setup(config_dir, interactive=True)

        import tomllib

        config_file = config_dir / "config.toml"
        raw = tomllib.loads(config_file.read_text(encoding="utf-8"))
        assert raw["server"]["auth_token"] == "secret-password"
        # Decision 4: first-run setup writes bind_host = "0.0.0.0" explicitly.
        assert raw["server"]["bind_host"] == "0.0.0.0"
        assert raw["control_plane"]["managed_projects_root"] == "~/code"
        assert (config_file.stat().st_mode & 0o777) == 0o600

    def test_fully_configured_file_is_never_touched_twice(self, home, monkeypatch) -> None:
        config_file = _write_config(home, bind_host="127.0.0.1")
        before = config_file.read_bytes()
        ui_cli.run_first_run_setup(config_file.parent, interactive=False)
        assert config_file.read_bytes() == before

    def test_setup_preserves_unrelated_toml_content(self, home, monkeypatch) -> None:
        config_dir = home / ".config" / "aflow"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.toml"
        config_file.write_text(
            '# my comment\n[server]\nbind_port = 9999\n'
            '[control_plane]\nmanaged_projects_root = "~/code"\n',
            encoding="utf-8",
        )
        import getpass

        passwords = iter(["setup-secret", "setup-secret"])
        monkeypatch.setattr(getpass, "getpass", lambda *a: next(passwords))
        ui_cli.run_first_run_setup(config_dir, interactive=True)
        text = config_file.read_text(encoding="utf-8")
        assert "# my comment" in text
        assert "bind_port = 9999" in text
        import tomllib

        raw = tomllib.loads(text)
        assert raw["server"]["auth_token"] == "setup-secret"
        assert raw["control_plane"]["managed_projects_root"] == "~/code"

    def test_empty_replacement_credential_is_rejected(self, home) -> None:
        from aflow_app_server.config import update_global_settings

        with pytest.raises(ValueError, match="non-empty"):
            update_global_settings(home / ".config" / "aflow", auth_token="   ")


class TestOwnershipRecord:
    def test_status_reports_not_running_and_exit_zero(self, home, capsys) -> None:
        assert ui_cli._status() == 0
        assert "not running" in capsys.readouterr().out

    def test_stop_with_stale_reused_record_removes_it_without_signalling(self, home, capsys) -> None:
        record = ui_cli.UIRecord(
            pid=1, process_birth="definitely-not-live", host="127.0.0.1", port=1,
            started_at="x", mode="background",
        )
        ui_cli.ui_state_dir().mkdir(parents=True, exist_ok=True)
        ui_cli._record_path().write_text(record.to_json())
        assert ui_cli.stop_ui() == 0
        assert not ui_cli._record_path().exists()
        assert "stale record removed" in capsys.readouterr().out

    def test_duplicate_start_reports_existing_and_exits_zero(self, home, monkeypatch, capsys) -> None:
        _write_config(home)
        record = ui_cli.UIRecord.current(host="127.0.0.1", port=_free_port(), mode="background")
        ui_cli.ui_state_dir().mkdir(parents=True, exist_ok=True)
        ui_cli._record_path().write_text(record.to_json())
        # A matching live record without HTTP: claim refuses with the exact hint.
        with pytest.raises(ui_cli.UIError, match="aflow ui --stop"):
            ui_cli.claim_ui_record(record)
        capsys.readouterr()

    def test_dead_record_is_replaced_by_a_new_owner(self, home) -> None:
        record = ui_cli.UIRecord(
            pid=os.getpid(), process_birth="not-my-birth", host="127.0.0.1",
            port=1, started_at="x", mode="background",
        )
        ui_cli.ui_state_dir().mkdir(parents=True, exist_ok=True)
        ui_cli._record_path().write_text(record.to_json())
        replacement = ui_cli.UIRecord.current(host="127.0.0.1", port=2, mode="background")
        ui_cli.claim_ui_record(replacement)
        assert ui_cli.read_ui_record() is not None
        assert ui_cli.read_ui_record().pid == os.getpid()  # type: ignore[union-attr]


class TestPortConflicts:
    def test_port_probe_allows_restart_after_a_closed_connection(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            port = server.getsockname()[1]
            server.listen(1)
            with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
                accepted, _ = server.accept()
                accepted.shutdown(socket.SHUT_WR)
                accepted.close()
                assert client.recv(1) == b""
        assert ui_cli._port_bound(port) is False

    def test_port_bound_detects_an_occupied_port(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(1)
        try:
            assert ui_cli._port_bound(port) is True
        finally:
            sock.close()
        assert ui_cli._port_bound(port) is False


class TestWorkerWrapper:
    def test_nonce_mismatch_refuses_to_start(self, tmp_path, capsys) -> None:
        receipt_dir = tmp_path / "units"
        receipt_dir.mkdir()
        (receipt_dir / "start.json").write_text(json.dumps({"nonce": "expected"}))

        from types import SimpleNamespace

        args = SimpleNamespace(
            receipt_dir=receipt_dir,
            nonce="wrong",
            worker_argv=[sys.executable, "-c", "print('should not run')"],
        )
        assert ui_cli.handle_ui_worker_command(args) == 2

    def test_wrapper_records_child_identity_and_exit(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        receipt_dir = tmp_path / ".aflow/runs/test-worker/units"
        receipt_dir.mkdir(parents=True)
        (receipt_dir / "start.json").write_text(json.dumps({"schema": 1, "run_id": "test-worker", "unit": "aflow-run-test-worker.service", "nonce": "good"}))

        from types import SimpleNamespace

        args = SimpleNamespace(
            receipt_dir=receipt_dir,
            nonce="good",
            worker_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        )

        # Stop the child via its recorded identity to prove stop semantics.
        import threading

        result: dict[str, int] = {}

        def run() -> None:
            result["code"] = ui_cli.handle_ui_worker_command(args)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        child: dict | None = None
        while time.monotonic() < deadline:
            path = receipt_dir / "child.json"
            if path.is_file():
                child = json.loads(path.read_text())
                break
            time.sleep(0.05)
        assert child is not None and child["nonce"] == "good"
        os.kill(child["pid"], 15)
        thread.join(timeout=10)
        exit_receipt = json.loads((receipt_dir / "exit.json").read_text())
        assert exit_receipt["nonce"] == "good"
        assert result["code"] != 0  # terminated by signal


class TestBackgroundLifecycle:
    def test_daemon_start_health_and_stop(self, home, monkeypatch) -> None:
        """Full acceptance path: setup → detached start → /health → --stop."""
        _write_config(home)
        port = _free_port()
        # The editable checkout would build web assets; tests skip the build.
        marker = Path("/tmp/pytest-ui-assets-marker")
        monkeypatch.setattr(
            ui_cli, "_prepare_assets",
            lambda: marker,
        )
        assert detect_installation().executable.is_file()

        import argparse as argparse_module

        args = argparse_module.Namespace(
            daemon=True, status=False, stop=False,
            host="127.0.0.1", port=port, ui_internal_serve=False,
        )
        started_at = time.monotonic()
        assert ui_cli.handle_ui_command(args) == 0
        assert time.monotonic() - started_at < 60

        record = ui_cli.read_ui_record()
        assert record is not None
        assert record.port == port
        # A ready record requires the actual HTTP server to accept requests.
        assert ui_cli._probe_health("127.0.0.1", port)

        # A second start reports the existing server and exits successfully.
        args_again = argparse_module.Namespace(
            daemon=True, status=False, stop=False,
            host="127.0.0.1", port=port, ui_internal_serve=False,
        )
        assert ui_cli.handle_ui_command(args_again) == 0

        # Stop targets the UI only.
        stop_args = argparse_module.Namespace(
            daemon=False, status=False, stop=True,
            host=None, port=None, ui_internal_serve=False,
        )
        assert ui_cli.handle_ui_command(stop_args) == 0
        assert ui_cli.read_ui_record() is None
        assert not ui_cli._probe_health("127.0.0.1", port)
        marker.unlink(missing_ok=True)

    def test_status_after_daemon_start_reports_running(self, home, monkeypatch, capsys) -> None:
        _write_config(home)
        port = _free_port()
        marker = Path("/tmp/pytest-ui-assets-marker-2")
        monkeypatch.setattr(ui_cli, "_prepare_assets", lambda: marker)
        import argparse as argparse_module

        args = argparse_module.Namespace(
            daemon=True, status=False, stop=False,
            host="127.0.0.1", port=port, ui_internal_serve=False,
        )
        try:
            assert ui_cli.handle_ui_command(args) == 0
            status_args = argparse_module.Namespace(
                daemon=False, status=True, stop=False,
                host=None, port=None, ui_internal_serve=False,
            )
            assert ui_cli.handle_ui_command(status_args) == 0
            out = capsys.readouterr().out
            assert "running" in out
            assert f"http://127.0.0.1:{port}" in out
        finally:
            stop_args = argparse_module.Namespace(
                daemon=False, status=False, stop=True,
                host=None, port=None, ui_internal_serve=False,
            )
            ui_cli.handle_ui_command(stop_args)
            marker.unlink(missing_ok=True)
