"""Focused failure-path coverage for the disposable installed-wheel smoke."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


SMOKE_UI_PATH = Path(__file__).resolve().parents[1] / "scripts" / "smoke_ui.py"


def _smoke_ui_module():
    spec = importlib.util.spec_from_file_location("smoke_ui_under_test", SMOKE_UI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _installed(module, tmp_path: Path):
    installed = object.__new__(module.InstalledWheel)
    installed.home = tmp_path / "home"
    installed.workdir = tmp_path / "cwd"
    installed.home.mkdir()
    installed.workdir.mkdir()
    (installed.home / "code" / "smoke-a").mkdir(parents=True)
    (installed.home / "code" / "smoke-b").mkdir(parents=True)
    installed.token = "smoke-token"
    installed.host = "127.0.0.1"
    installed.port = 4567
    installed.executable = tmp_path / "aflow"
    installed._owned_runs = {}
    installed._cleanup_session = None
    installed._cleanup_invocation_token = "invocation-token"
    return installed


def _write_start_receipt(installed, project_id: str, run_id: str, nonce: str) -> None:
    receipt_dir = (
        installed.home
        / "code"
        / project_id
        / ".aflow"
        / "runs"
        / run_id
        / "units"
    )
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "start.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "run_id": run_id,
                "unit": f"aflow-run-{run_id}.service",
                "nonce": nonce,
            }
        ),
        encoding="utf-8",
    )


def _owner_stop_responder(installed, calls: list[tuple[str, dict]]):
    run_url = f"{installed.base_url()}/api/control-plane/projects/smoke-a/runs/run-a"

    def request(url: str, **kwargs):
        calls.append((url, kwargs))
        if url == run_url:
            return 200, {}, b'{"status":"running","revision":7}'
        if url == f"{run_url}/owner-stop":
            return 200, {}, b'{"status":"owner_stopped"}'
        raise AssertionError(f"unexpected smoke request: {url}")

    return request


def test_post_launch_failure_cleans_held_run_and_preserves_original_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _smoke_ui_module()
    installed = _installed(module, tmp_path)
    _write_start_receipt(installed, "smoke-a", "run-a", "nonce-a")
    installed.remember_owned_run("smoke-a", "run-a")
    installed.remember_session("session-a")
    calls: list[tuple[str, dict]] = []
    request = _owner_stop_responder(installed, calls)
    events: list[str] = []
    cleanup = installed.cleanup_owned_runs

    def cleanup_before_ui_stop() -> None:
        events.append("worker-cleanup")
        cleanup(request=request)

    monkeypatch.setattr(installed, "cleanup_owned_runs", cleanup_before_ui_stop)
    monkeypatch.setattr(installed, "stop", lambda: events.append("ui-stop"))

    with pytest.raises(RuntimeError, match="injected post-launch failure"):
        try:
            raise RuntimeError("injected post-launch failure")
        finally:
            module.finalize_invocation(installed)

    assert events == ["worker-cleanup", "ui-stop"]
    assert not installed._owned_runs
    assert calls[0][0].endswith("/smoke-a/runs/run-a")
    assert calls[1][0].endswith("/smoke-a/runs/run-a/owner-stop")
    assert calls[1][1]["payload"] == {"expected_revision": 7}
    cleanup_key = calls[1][1]["extra_headers"]["Idempotency-Key"]
    assert cleanup_key.startswith("smoke-cleanup-invocation-token-smoke-a-run-a-")
    assert installed.home.is_dir(), "failure diagnostics must remain available"
    assert all("unrelated" not in url for url, _ in calls)


def test_partial_launch_only_cleans_successfully_recorded_owned_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _smoke_ui_module()
    installed = _installed(module, tmp_path)
    _write_start_receipt(installed, "smoke-a", "run-a", "nonce-a")
    installed.remember_owned_run("smoke-a", "run-a")
    installed.remember_session("session-a")
    calls: list[tuple[str, dict]] = []
    request = _owner_stop_responder(installed, calls)
    cleanup = installed.cleanup_owned_runs
    monkeypatch.setattr(
        installed,
        "cleanup_owned_runs",
        lambda: cleanup(request=request),
    )
    monkeypatch.setattr(installed, "stop", lambda: None)

    with pytest.raises(RuntimeError, match="second launch failed"):
        try:
            raise RuntimeError("second launch failed")
        finally:
            module.finalize_invocation(installed)

    assert len(calls) == 2
    assert all("smoke-a/runs/run-a" in url for url, _ in calls)
    assert all("smoke-b" not in url and "unrelated" not in url for url, _ in calls)


def test_finalize_invocation_stops_workers_before_disposable_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _smoke_ui_module()
    events: list[str] = []

    class FakeInstalled:
        def cleanup_owned_runs(self) -> None:
            events.append("worker-cleanup")

        def stop(self) -> None:
            events.append("ui-stop")

    module.finalize_invocation(FakeInstalled())

    assert events == ["worker-cleanup", "ui-stop"]
