"""Exercise the installed detached wrapper and canonical reads on disposable runs."""

import json
import os
import shutil
import sys
import time

import pytest

from aflow.control_plane import LaunchManifest, RunRepository, create_launch_manifest
from aflow.control_plane.persistent_units import PersistentUnitManager
from aflow.control_plane.persistence import build_context_bundle, write_launch_phase


def reserve(root, run_id="diagnostic-test"):
    unit = f"aflow-run-{run_id}.service"
    create_launch_manifest(root, LaunchManifest(run_id=run_id, project_root=str(root), plan_path=str(root / "plan.md"), workflow_name="test", max_turns=1, idempotency_key=run_id, caller_scope="test", intended_unit=unit))
    run = root / ".aflow" / "runs" / run_id
    run.mkdir(parents=True, exist_ok=True)
    write_launch_phase(root, run_id, "unit_started")
    return unit, run


def wait_exit(run):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if (run / "units/exit.json").exists():
            return json.loads((run / "units/exit.json").read_text())
        time.sleep(.02)
    pytest.fail("detached wrapper did not retain exit evidence")


@pytest.mark.parametrize("code", [0, 1, -15])
def test_real_detached_child_output_and_exit(tmp_path, code):
    unit, run = reserve(tmp_path)
    script = "import os,sys\n" + "for i in range(2000):\n os.write(1,b'output line\\n'*30); os.write(2,b'error line\\n'*30)\n" + "print('retained-marker password=DO-NOT-RETAIN',file=sys.stderr,flush=True)\n"
    script += "os.kill(os.getpid(),15)" if code < 0 else f"sys.exit({code})"
    manager = PersistentUnitManager(executable=shutil.which("aflow"))
    manager.start(unit, (sys.executable, "-c", script), cwd=tmp_path)
    receipt = wait_exit(run)
    assert receipt["returncode"] == code
    diagnostic = (run / "units/diagnostic.json").read_text()
    assert "retained-marker" in diagnostic
    assert "DO-NOT-RETAIN" not in diagnostic
    assert len(diagnostic.encode()) < 12000
    status = RunRepository(tmp_path).get_run_status(run.name)
    assert status.status == ("needs_attention" if code == 0 else "failed")
    assert status.worker_exit["exit_code"] == code
    assert status.started_at is None
    assert "retained-marker" in status.worker_exit["reason"]
    assert "retained-marker" in str(build_context_bundle(run).data)


def test_historical_exit_and_untrusted_evidence(tmp_path):
    unit, run = reserve(tmp_path)
    receipts = run / "units"
    receipts.mkdir()
    start = {"schema": 1, "run_id": run.name, "unit": unit, "nonce": "owned"}
    (receipts / "start.json").write_text(json.dumps(start))
    exit_record = {"schema": 1, "nonce": "owned", "returncode": 1, "at": "2026-09-08T19:08:16Z"}
    (receipts / "exit.json").write_text(json.dumps(exit_record))
    repository = RunRepository(tmp_path)
    before = {p: p.read_bytes() for p in receipts.iterdir()}
    for _ in range(2):
        status = repository.get_run_status(run.name)
        assert status.status == "failed"
        assert status.worker_exit["diagnostic_unavailable"] is True
        assert status.ended_at is None
    assert before == {p: p.read_bytes() for p in receipts.iterdir()}
    for changes in ({"nonce": "foreign"}, {"schema": 2}, {"returncode": True}, {"at": "invalid"}):
        (receipts / "exit.json").write_text(json.dumps({**exit_record, **changes}))
        assert repository.get_run_status(run.name).status == "needs_attention"
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(exit_record))
    (receipts / "exit.json").unlink()
    (receipts / "exit.json").symlink_to(outside)
    assert repository.get_run_status(run.name).status == "needs_attention"


def test_live_identity_wins_over_old_exit(tmp_path):
    from aflow.control_plane.persistent_units import _birth_identity
    unit, run = reserve(tmp_path)
    receipts = run / "units"
    receipts.mkdir()
    for filename, data in {
        "start.json": {"run_id": run.name, "unit": unit},
        "child.json": {"pid": os.getpid(), "pgid": os.getpid(), "process_birth": _birth_identity(os.getpid())},
        "exit.json": {"returncode": 1, "at": "2026-09-08T19:08:16Z"},
    }.items():
        (receipts / filename).write_text(json.dumps({"schema": 1, "nonce": "owned", **data}))
    status = RunRepository(tmp_path).get_run_status(run.name)
    assert status.status == "needs_attention"
    assert status.evidence["unit_active"] is True
    assert status.worker_exit is None


def test_real_daemon_worker_retains_configuration_exception(tmp_path):
    unit, run = reserve(tmp_path)
    executable = shutil.which("aflow")
    manager = PersistentUnitManager(executable=executable)
    config = tmp_path / "aflow.toml"
    config.write_text("[invalid toml")
    manager.start(unit, (executable, "daemon-worker", "--repo-root", str(tmp_path), "--config", str(config), "--run-id", run.name), cwd=tmp_path)
    assert wait_exit(run)["returncode"] == 1
    failure = json.loads((run / "units/worker-error.json").read_text())
    assert failure["stage"] == "configuration"
    status = RunRepository(tmp_path).get_run_status(run.name)
    assert status.worker_exit["stage"] == "configuration"
    assert status.worker_exit["diagnostic_unavailable"] is False


def test_controller_completion_and_owner_stop_are_authoritative(tmp_path):
    unit, run = reserve(tmp_path)
    manager = PersistentUnitManager(executable=shutil.which("aflow"))
    manager.start(unit, (sys.executable, "-c", "raise SystemExit(1)"), cwd=tmp_path)
    wait_exit(run)
    (run / "run.json").write_text(json.dumps({"status": "completed"}))
    assert RunRepository(tmp_path).get_run_status(run.name).status == "completed"
    write_launch_phase(tmp_path, run.name, "owner_stopped")
    assert RunRepository(tmp_path).get_run_status(run.name).status == "owner_stopped"


@pytest.mark.parametrize("change", [{"nonce": "foreign"}, {"schema": 2}, {"run_id": "foreign"}, {"unit": "aflow-run-foreign.service"}])
def test_foreign_launch_claim_never_establishes_failure(tmp_path, change):
    unit, run = reserve(tmp_path)
    receipts = run / "units"
    receipts.mkdir()
    (receipts / "start.json").write_text(json.dumps({"schema": 1, "run_id": run.name, "unit": unit, "nonce": "owned", **change}))
    (receipts / "exit.json").write_text(json.dumps({"schema": 1, "nonce": "owned", "returncode": 1, "at": "2026-09-08T19:08:16Z"}))
    status = RunRepository(tmp_path).get_run_status(run.name)
    assert status.status == "needs_attention"
    assert status.worker_exit is None


class _FakeStream:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _ControlledChild:
    def __init__(self, poll_results):
        self.pid = 424242
        self.returncode = None
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self._poll_results = list(poll_results)
        self.poll_calls = 0
        self.wait_calls = 0

    def poll(self):
        self.poll_calls += 1
        result = self._poll_results.pop(0)
        if result is not None:
            self.returncode = result
        return result

    def wait(self):
        self.wait_calls += 1
        assert self.returncode is not None
        return self.returncode


def _prepare_child_json_failure(tmp_path, monkeypatch, child):
    from types import SimpleNamespace
    from aflow import ui_cli

    unit, run = reserve(tmp_path)
    receipts = run / "units"
    receipts.mkdir()
    (receipts / "start.json").write_text(json.dumps({"schema": 1, "run_id": run.name, "unit": unit, "nonce": "owned"}))
    original = ui_cli._write_worker_receipt

    def fail_child(directory, name, payload):
        if name == "child.json":
            raise OSError("fixture child receipt failure")
        return original(directory, name, payload)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ui_cli, "_write_worker_receipt", fail_child)
    monkeypatch.setattr(ui_cli.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(ui_cli, "process_birth_identity", lambda _pid: "fake-birth")
    return ui_cli, SimpleNamespace(
        receipt_dir=receipts,
        nonce="owned",
        worker_argv=["fake-worker"],
    ), run, receipts


def _assert_child_failure_receipts(run, receipts):
    error = json.loads((receipts / "error.json").read_text())
    exit_record = json.loads((receipts / "exit.json").read_text())
    assert error["schema"] == 1
    assert error["nonce"] == "owned"
    assert error["stage"] == "wrapper_receipt"
    assert error["message"] == "Could not persist worker process identity"
    assert exit_record["schema"] == 1
    assert exit_record["nonce"] == "owned"
    assert exit_record["returncode"] == 0
    assert RunRepository(run.parent.parent.parent).get_run_status(run.name).status == "failed"


def test_child_receipt_failure_observes_exit_before_signalling(tmp_path, monkeypatch):
    child = _ControlledChild([0])
    ui_cli, args, run, receipts = _prepare_child_json_failure(tmp_path, monkeypatch, child)
    signal_calls = []

    def unexpected_signal(*call_args):
        signal_calls.append(call_args)
        pytest.fail("an already-exited child must not be signalled")

    monkeypatch.setattr(ui_cli.os, "killpg", unexpected_signal)

    assert ui_cli.handle_ui_worker_command(args) == 1
    assert signal_calls == []
    assert child.poll_calls == 1
    assert child.wait_calls == 1
    assert child.stdout.closed and child.stderr.closed
    _assert_child_failure_receipts(run, receipts)


def test_child_receipt_failure_rechecks_exit_after_signal_error(tmp_path, monkeypatch):
    child = _ControlledChild([None, 0])
    ui_cli, args, run, receipts = _prepare_child_json_failure(tmp_path, monkeypatch, child)
    signal_calls = []

    def signal_after_exit(pid, sig):
        signal_calls.append((pid, sig))
        raise PermissionError("signal raced with child exit")

    monkeypatch.setattr(ui_cli.os, "killpg", signal_after_exit)

    assert ui_cli.handle_ui_worker_command(args) == 1
    assert signal_calls == [(child.pid, ui_cli.signal.SIGKILL)]
    assert child.poll_calls == 2
    assert child.wait_calls == 1
    assert child.stdout.closed and child.stderr.closed
    _assert_child_failure_receipts(run, receipts)


def test_child_receipt_failure_propagates_signal_error_while_live(tmp_path, monkeypatch):
    child = _ControlledChild([None, None])
    ui_cli, args, run, receipts = _prepare_child_json_failure(tmp_path, monkeypatch, child)
    signal_calls = []

    def deny_live_signal(pid, sig):
        signal_calls.append((pid, sig))
        raise PermissionError("live child signal denied")

    monkeypatch.setattr(ui_cli.os, "killpg", deny_live_signal)

    with pytest.raises(PermissionError, match="live child signal denied"):
        ui_cli.handle_ui_worker_command(args)
    assert signal_calls == [(child.pid, ui_cli.signal.SIGKILL)]
    assert child.poll_calls == 2
    assert child.wait_calls == 0
    assert not child.stdout.closed and not child.stderr.closed
    assert not (receipts / "error.json").exists()
    assert not (receipts / "exit.json").exists()
    child.stdout.close()
    child.stderr.close()


@pytest.mark.parametrize("failed_file", ["diagnostic.json", "child.json"])
def test_diagnostic_write_failure_keeps_exit_and_returns_failure(tmp_path, monkeypatch, failed_file):
    from types import SimpleNamespace
    from aflow import ui_cli
    unit, run = reserve(tmp_path)
    receipts = run / "units"
    receipts.mkdir()
    (receipts / "start.json").write_text(json.dumps({"schema": 1, "run_id": run.name, "unit": unit, "nonce": "owned"}))
    original = ui_cli._write_worker_receipt
    def fail_diagnostic(directory, name, payload):
        if name == failed_file:
            raise OSError("fixture write failure")
        return original(directory, name, payload)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ui_cli, "_write_worker_receipt", fail_diagnostic)
    result = ui_cli.handle_ui_worker_command(SimpleNamespace(receipt_dir=receipts, nonce="owned", worker_argv=[sys.executable, "-c", "print('child success')"]))
    assert result == 1
    receipt = json.loads((receipts / "exit.json").read_text())
    if failed_file == "diagnostic.json":
        assert receipt["returncode"] == 0
        assert receipt["diagnostic_write_failed"] is True
    else:
        assert (receipts / "error.json").exists()
        assert RunRepository(tmp_path).get_run_status(run.name).status == "failed"


def test_controller_completion_wins_even_with_conflicting_live_receipt(tmp_path, monkeypatch):
    from aflow.control_plane.models import RunStatus
    from aflow.control_plane.worker_diagnostics import project_worker_status
    monkeypatch.setattr('aflow.control_plane.worker_diagnostics.worker_evidence', lambda *_: {'active': True, 'exit_code': 1})
    run = RunStatus(run_id='completed', status='completed', evidence={'controller_terminal': True})
    assert project_worker_status(run, tmp_path).status == 'completed'
