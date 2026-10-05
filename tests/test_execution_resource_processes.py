"""Focused lifetime tests binding exclusive leases to real child processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from aflow import execution_resources as er
from aflow.execution_resources import (
    ClaimSpec,
    ControllerIdentity,
    ExecutionLease,
    ExecutionResourceStore,
    Outcome,
    ProcessEvidence,
    ResourceLeaseError,
)
from aflow.harnesses import dsh, strands
from aflow.harnesses.base import HarnessInvocation
from aflow.harnesses.reasonix import ReasonixAcpProcess, ReasonixAcpDriver
from aflow.harnesses.session import (
    NoSessionDriver,
    SessionCapabilities,
    SessionRequest,
    session_driver_accepts_lifecycle,
)
from aflow.plan import PlanSnapshot
from aflow.run_state import ControllerState
from aflow.workflow import _run_injected_runner, _run_process

RESOURCE = "a" * 64
BOOT = "boot:test-uuid"
CTRL = ControllerIdentity(pid=1000, birth="linux-start-ticks:1000", boot=BOOT)


# -- helpers ------------------------------------------------------------------


def make_controller(pid: int = 1000) -> ControllerIdentity:
    return ControllerIdentity(pid=pid, birth=f"linux-start-ticks:{pid}", boot=BOOT)


def make_spec(invocation: str) -> ClaimSpec:
    return ClaimSpec(
        project_root=f"/tmp/project-{invocation}",
        run_id=f"run-{invocation}",
        invocation_id=invocation,
        kind="turn",
        role="worker",
        selector=f"selector-{invocation}",
    )


def make_store(
    root: Path,
    *,
    liveness: dict[int, str] | None = None,
    group: str = "absent",
) -> ExecutionResourceStore:
    liveness = liveness or {}

    def process_evidence(pid: int) -> ProcessEvidence:
        state = liveness.get(pid, "present")
        return ProcessEvidence(
            liveness=state, birth=f"linux-start-ticks:{pid}" if state == "present" else None
        )

    return ExecutionResourceStore(
        root,
        process_evidence=process_evidence,
        group_evidence=lambda _pgid: group,  # type: ignore[arg-type]
        boot_provider=lambda: BOOT,
    )


def read_journal(root: Path, resource: str) -> dict:
    return json.loads((root / f"{resource}.json").read_text(encoding="utf-8"))


def owner_status(root: Path, resource: str) -> str | None:
    owner = read_journal(root, resource).get("owner")
    return owner["status"] if owner is not None else None


def acquire(store: ExecutionResourceStore, invocation: str, controller: ControllerIdentity = CTRL) -> None:
    assert store.enqueue(RESOURCE, make_spec(invocation), controller).state == "queued"
    assert store.try_acquire(RESOURCE, invocation, controller).state == "acquired"


def make_lease(
    store: ExecutionResourceStore, invocation: str, controller: ControllerIdentity = CTRL
) -> ExecutionLease:
    acquire(store, invocation, controller)
    return ExecutionLease(store, RESOURCE, invocation, controller)


def make_invocation(argv: tuple[str, ...]) -> HarnessInvocation:
    return HarnessInvocation(
        label="test-harness",
        argv=argv,
        env={},
        prompt_mode="stdin",
        system_prompt="",
        user_prompt="",
        effective_prompt="",
    )


class FakeBanner:
    def update(self, state: ControllerState) -> None:
        pass


def make_state() -> ControllerState:
    return ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))


def write_script(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.write_text(body, encoding="utf-8")
    return path


class RecordingLease:
    """Wrap a real lease and record the exact lifecycle event order."""

    def __init__(self, lease: ExecutionLease) -> None:
        self.lease = lease
        self.events: list[str] = []

    def mark_launching(self) -> None:
        self.events.append("mark_launching")
        self.lease.mark_launching()

    def bind_child(self, pid: int, birth: str | None = None, process_group: int | None = None) -> None:
        self.events.append(f"bind_child:{pid}")
        self.lease.bind_child(pid, birth, process_group)

    def complete(self) -> None:
        self.events.append("complete")
        self.lease.complete()

    def mark_unconfirmed(self) -> None:
        self.events.append("mark_unconfirmed")
        self.lease.mark_unconfirmed()


def spawn_stub_child(seconds: float = 120.0) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def kill_child(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.kill()
        child.wait(timeout=5)


# -- _run_process --------------------------------------------------------------


@pytest.mark.parametrize("dispatch", ["process", "injected"])
def test_failed_launch_intent_releases_known_unlaunched_claim(tmp_path, monkeypatch, dispatch):
    store = make_store(tmp_path)
    lease = make_lease(store, "intent-failure")
    monkeypatch.setattr(store, "mark_launching", lambda *args: Outcome("rejected", reason="journal_io_failure"))
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("dispatch must not precede durable launch intent")

    invocation = make_invocation((sys.executable, "-c", "print('unused')"))
    with pytest.raises(ResourceLeaseError) as error:
        if dispatch == "process":
            monkeypatch.setattr(subprocess, "Popen", forbidden)
            _run_process(invocation, tmp_path, FakeBanner(), make_state(), lease=lease)
        else:
            _run_injected_runner(forbidden, invocation, tmp_path, lease=lease)
    assert error.value.stage == "mark_launching" and error.value.reason == "journal_io_failure"
    assert calls == []
    journal = read_journal(tmp_path, RESOURCE)
    assert journal["owner"] is None and journal["queue"] == []


def test_run_process_success_binds_child_and_releases_claim(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    script = write_script(
        tmp_path,
        "child.py",
        "print('visible stdout')\n",
    )
    completed = _run_process(make_invocation((sys.executable, str(script))), tmp_path, FakeBanner(), make_state(), lease=lease)
    assert completed.returncode == 0
    assert owner_status(tmp_path, RESOURCE) is None
    assert read_journal(tmp_path, RESOURCE)["queue"] == []


def test_run_process_registers_the_exact_child_before_exit(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    pid_file = tmp_path / "child.pid"
    script = write_script(
        tmp_path,
        "child.py",
        f"import os\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "import time\n"
        "time.sleep(2)\n",
    )
    done: list[Any] = []
    import threading

    thread = threading.Thread(
        target=lambda: done.append(
            _run_process(
                make_invocation((sys.executable, str(script))),
                tmp_path, FakeBanner(), make_state(), lease=lease,
            )
        )
    )
    child_pid: int | None = None
    thread.start()
    try:
        # The journal may record the child's PID before the child writes
        # child.pid; wait with bounded deadlines for both observations.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            owner = read_journal(tmp_path, RESOURCE).get("owner")
            if (
                owner is not None
                and owner["status"] == "running"
                and owner["child_pid"] is not None
                and pid_file.exists()
            ):
                break
            time.sleep(0.02)
        else:
            pytest.fail("child was never registered as running alongside its PID file")
        child_pid = int(pid_file.read_text())
        assert owner["child_pid"] == child_pid
        assert owner["child_birth"] is not None
        assert owner["process_group"] is not None
    finally:
        # The child sleeps for 2s, so a failed assertion must not outlive it.
        thread.join(timeout=15)
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
    assert done and done[0].returncode == 0
    assert owner_status(tmp_path, RESOURCE) is None


def test_run_process_nonzero_exit_releases_claim(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    script = write_script(tmp_path, "fail.py", "import sys\nsys.exit(3)\n")
    completed = _run_process(make_invocation((sys.executable, str(script))), tmp_path, FakeBanner(), make_state(), lease=lease)
    assert completed.returncode == 3
    assert owner_status(tmp_path, RESOURCE) is None


def test_run_process_spawn_failure_releases_claim_without_child(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    missing = tmp_path / "definitely-not-an-executable"
    completed = _run_process(
        make_invocation((str(missing),)), tmp_path, FakeBanner(), make_state(), lease=lease
    )
    assert completed.returncode == 127
    assert "failed to start" in completed.stderr
    assert owner_status(tmp_path, RESOURCE) is None
    assert read_journal(tmp_path, RESOURCE)["queue"] == []


def test_run_process_bind_failure_reaps_child_and_retains_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    monkeypatch.setattr(
        store,
        "register_child",
        lambda *args, **kwargs: er.Outcome("rejected", reason="not_owner"),
    )
    pid_file = tmp_path / "child.pid"
    script = write_script(
        tmp_path,
        "child.py",
        f"import os\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "import time\n"
        "time.sleep(30)\n",
    )
    with pytest.raises(ResourceLeaseError) as excinfo:
        _run_process(make_invocation((sys.executable, str(script))), tmp_path, FakeBanner(), make_state(), lease=lease)
    assert excinfo.value.stage == "register_child"
    assert isinstance(excinfo.value.__cause__, ResourceLeaseError)
    assert owner_status(tmp_path, RESOURCE) == "unconfirmed"
    child_pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_run_process_owner_stop_retains_claim_while_child_runs(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    pid_file = tmp_path / "child.pid"
    script = write_script(
        tmp_path,
        "child.py",
        f"import os\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "import time\n"
        "time.sleep(30)\n",
    )
    polls = []

    def control() -> None:
        polls.append(1)
        raise RuntimeError("owner stop requested")

    child_pid: int | None = None
    try:
        with pytest.raises(RuntimeError, match="owner stop"):
            _run_process(
                make_invocation((sys.executable, str(script))),
                tmp_path, FakeBanner(), make_state(),
                control_callback=control, lease=lease,
            )
        assert polls
        assert owner_status(tmp_path, RESOURCE) == "unconfirmed"
        child_pid = int(pid_file.read_text())
        os.kill(child_pid, 0)  # the child must still be running
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass


# -- _run_injected_runner ------------------------------------------------------


def test_injected_runner_success_releases_claim(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    completed = _run_injected_runner(subprocess.run, make_invocation((sys.executable, "-c", "pass")), tmp_path, lease=lease)
    assert completed.returncode == 0
    assert owner_status(tmp_path, RESOURCE) is None


def test_injected_runner_launch_error_retains_claim(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")

    def runner(argv, **kwargs):
        raise FileNotFoundError(2, "no such harness")

    # An opaque injected runner may have spawned a child before raising, so
    # even FileNotFoundError retains the claim instead of releasing it.
    with pytest.raises(ResourceLeaseError) as excinfo:
        _run_injected_runner(runner, make_invocation(("missing",)), tmp_path, lease=lease)
    assert excinfo.value.stage == "injected_runner"
    assert isinstance(excinfo.value.__cause__, FileNotFoundError)
    assert owner_status(tmp_path, RESOURCE) == "unconfirmed"
    assert store.enqueue(RESOURCE, make_spec("inv-2"), make_controller(2000)).state == "queued"
    assert store.try_acquire(RESOURCE, "inv-2", make_controller(2000)).state == "queued"


def test_unleased_injected_runner_launch_error_still_normalizes(tmp_path: Path) -> None:
    def runner(argv, **kwargs):
        raise FileNotFoundError(2, "no such harness")

    completed = _run_injected_runner(runner, make_invocation(("missing",)), tmp_path)
    assert completed.returncode == 127
    assert "failed to start" in completed.stderr


def test_injected_runner_postlaunch_failure_retains_claim(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")

    def runner(argv, **kwargs):
        raise RuntimeError("runner exploded after spawn")

    with pytest.raises(ResourceLeaseError) as excinfo:
        _run_injected_runner(runner, make_invocation(("argv",)), tmp_path, lease=lease)
    assert excinfo.value.stage == "injected_runner"
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert owner_status(tmp_path, RESOURCE) == "unconfirmed"


def test_injected_runner_oserror_after_real_spawn_retains_claim(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    child = spawn_stub_child()

    def runner(argv, **kwargs):
        raise OSError("runner I/O failed after spawning its child")

    try:
        with pytest.raises(ResourceLeaseError) as excinfo:
            _run_injected_runner(runner, make_invocation(("argv",)), tmp_path, lease=lease)
        assert excinfo.value.stage == "injected_runner"
        assert isinstance(excinfo.value.__cause__, OSError)
        assert child.poll() is None  # the spawned child is still alive
        assert owner_status(tmp_path, RESOURCE) == "unconfirmed"
        # The unconfirmed owner blocks the second claimant instead of
        # letting it acquire during the first invocation.
        assert store.enqueue(RESOURCE, make_spec("inv-2"), make_controller(2000)).state == "queued"
        assert store.try_acquire(RESOURCE, "inv-2", make_controller(2000)).state == "queued"
    finally:
        kill_child(child)


def test_legacy_paths_without_lease_do_not_touch_store(tmp_path: Path) -> None:
    store_root = tmp_path / "store"
    script = write_script(tmp_path, "ok.py", "print('hi')\n")
    completed = _run_process(make_invocation((sys.executable, str(script))), tmp_path, FakeBanner(), make_state())
    assert completed.returncode == 0
    injected = _run_injected_runner(subprocess.run, make_invocation((sys.executable, "-c", "pass")), tmp_path)
    assert injected.returncode == 0
    assert not store_root.exists()


# -- lease unit behavior --------------------------------------------------------


def test_lease_requires_the_owning_controller(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    acquire(store, "inv-1", make_controller(1000))
    lease = ExecutionLease(store, RESOURCE, "inv-1", make_controller(9999))
    with pytest.raises(ResourceLeaseError):
        lease.mark_launching()


def test_lease_contention_deadline_stops_retrying(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    acquire(store, "inv-1")
    lease = ExecutionLease(store, RESOURCE, "inv-1", CTRL, contention_deadline_seconds=0.05)
    attempts = []

    def fake_transition(operation):
        attempts.append(1)
        return er.Outcome("contended", reason="lock_contended")

    lease._transition = fake_transition  # type: ignore[method-assign]
    with pytest.raises(ResourceLeaseError):
        lease.mark_launching()
    assert attempts


# -- reconciliation through the lease -------------------------------------------


def test_launch_intent_without_child_is_retained_after_controller_loss(tmp_path: Path) -> None:
    store = make_store(tmp_path, liveness={CTRL.pid: "absent"})
    acquire(store, "inv-1")
    assert store.mark_launching(RESOURCE, "inv-1", CTRL).state == "launching"
    assert store.reconcile(RESOURCE).state == "unchanged"
    owner = read_journal(tmp_path, RESOURCE)["owner"]
    assert owner["status"] == "launching"
    assert owner["child_pid"] is None
    # A second claimant stays blocked behind the ambiguous claim.
    assert store.enqueue(RESOURCE, make_spec("inv-2"), make_controller(2000)).state == "queued"
    assert store.try_acquire(RESOURCE, "inv-2", make_controller(2000)).state == "queued"


def test_surviving_child_blocks_reclamation_until_positive_termination(tmp_path: Path) -> None:
    child = spawn_stub_child()
    try:
        store = make_store(
            tmp_path,
            liveness={CTRL.pid: "absent", child.pid: "present"},
            group="present",
        )
        acquire(store, "inv-1")
        assert store.mark_launching(RESOURCE, "inv-1", CTRL).state == "launching"
        assert store.register_child(
            RESOURCE, "inv-1", CTRL, child.pid, "linux-start-ticks:child", child.pid
        ).state == "running"
        # The controller is gone but the exact child is still running: keep.
        assert store.reconcile(RESOURCE).state == "unchanged"
        assert owner_status(tmp_path, RESOURCE) == "running"
        assert store.enqueue(RESOURCE, make_spec("inv-2"), make_controller(2000)).state == "queued"
        assert store.try_acquire(RESOURCE, "inv-2", make_controller(2000)).state == "queued"
        # Positive termination evidence reclaims the resource.
        child.terminate()
        child.wait(timeout=5)
        reclaimed = make_store(tmp_path, liveness={CTRL.pid: "absent", child.pid: "absent"}, group="absent")
        assert reclaimed.reconcile(RESOURCE).state == "reclaimed"
        assert owner_status(tmp_path, RESOURCE) is None
    finally:
        kill_child(child)


# -- ACP driver lifecycle hooks -------------------------------------------------


class FakeOwnedAcpProcess:
    """ACP protocol fake whose ``.process`` is a real owned child."""

    def __init__(
        self,
        process: subprocess.Popen,
        *,
        fail_prompt: bool = False,
        timeout_prompt: bool = False,
        uncloseable: bool = False,
        agent_name: str = "deepseek-harness-acp",
    ) -> None:
        self.process = process
        self.fail_prompt = fail_prompt
        self.timeout_prompt = timeout_prompt
        self.uncloseable = uncloseable
        self.agent_name = agent_name
        self.received: list[Mapping[str, Any]] = []
        self.raw_lines: list[str] = []
        self.closed = False

    def initialize(self) -> Mapping[str, Any]:
        return {
            "result": {
                "protocolVersion": 1,
                "agentInfo": {"name": self.agent_name},
                "agentCapabilities": {"sessionCapabilities": {}},
            }
        }

    def request(self, method: str, params: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        if method in ("session/new", "session/resume", "session/set_config_option", "session/close"):
            return {"result": {"sessionId": "session-1", "configOptions": []}}
        if method == "session/prompt":
            if self.fail_prompt:
                raise RuntimeError("provider exploded mid-turn")
            if self.timeout_prompt:
                raise TimeoutError("ACP session/prompt response timed out")
            self.received.append(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": "session-1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "OK"},
                        },
                    },
                }
            )
            return {"result": {"stopReason": "end_turn"}}
        raise AssertionError(f"unexpected ACP method {method}")

    def close(self) -> None:
        if self.uncloseable:
            raise OSError("cleanup failed to signal child")
        self.closed = True
        self.process.terminate()
        self.process.wait(timeout=5)


def make_dsh_driver() -> dsh.DshAcpDriver:
    return dsh.DshAcpDriver(SessionCapabilities())


def make_request() -> SessionRequest:
    return SessionRequest(Path("/repo"), "dsh.glm", None, None, "SYSTEM", "USER")


def test_dsh_lifecycle_hook_order_and_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    recording = RecordingLease(lease)
    child = spawn_stub_child()
    fake = FakeOwnedAcpProcess(child)
    try:
        monkeypatch.setattr(dsh.DshAcpProcess, "start", staticmethod(lambda **kwargs: fake))
        driver = make_dsh_driver()
        request = make_request()
        result = driver.execute_session(request, driver.build_invocation(request), lifecycle=recording)
        assert result.result.final_output == "OK"
        assert recording.events == ["mark_launching", f"bind_child:{child.pid}", "complete"]
        assert fake.closed
        assert child.poll() is not None
        assert owner_status(tmp_path, RESOURCE) is None
    finally:
        kill_child(child)


def test_dsh_provider_error_reaps_and_releases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    recording = RecordingLease(lease)
    child = spawn_stub_child()
    fake = FakeOwnedAcpProcess(child, fail_prompt=True)
    try:
        monkeypatch.setattr(dsh.DshAcpProcess, "start", staticmethod(lambda **kwargs: fake))
        driver = make_dsh_driver()
        request = make_request()
        with pytest.raises(RuntimeError, match="provider exploded"):
            driver.execute_session(request, driver.build_invocation(request), lifecycle=recording)
        assert fake.closed
        assert child.poll() is not None
        assert recording.events[-1] == "complete"
        assert owner_status(tmp_path, RESOURCE) is None
    finally:
        kill_child(child)


def test_dsh_cleanup_failure_retains_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    recording = RecordingLease(lease)
    child = spawn_stub_child()
    fake = FakeOwnedAcpProcess(child, uncloseable=True)
    try:
        monkeypatch.setattr(dsh.DshAcpProcess, "start", staticmethod(lambda **kwargs: fake))
        driver = make_dsh_driver()
        request = make_request()
        with pytest.raises(ResourceLeaseError) as excinfo:
            driver.execute_session(request, driver.build_invocation(request), lifecycle=recording)
        assert excinfo.value.stage == "cleanup"
        assert recording.events[-1] == "mark_unconfirmed"
        assert owner_status(tmp_path, RESOURCE) == "unconfirmed"
        os.kill(child.pid, 0)  # the child is still running
    finally:
        kill_child(child)


def test_strands_lifecycle_hook_order_and_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    recording = RecordingLease(lease)
    child = spawn_stub_child()
    fake = FakeOwnedAcpProcess(child, agent_name="@strands-agents/cli")
    try:
        monkeypatch.setattr(strands.StrandsAcpProcess, "start", staticmethod(lambda **kwargs: fake))
        driver = strands.StrandsAcpDriver()
        request = make_request()
        result = driver.execute_session(request, driver.build_invocation(request), lifecycle=recording)
        assert result.result.final_output == "OK"
        assert recording.events == ["mark_launching", f"bind_child:{child.pid}", "complete"]
        assert child.poll() is not None
        assert owner_status(tmp_path, RESOURCE) is None
    finally:
        kill_child(child)


def _tool_approval_option(current: str) -> dict[str, Any]:
    return {
        "id": "tool_approval",
        "type": "select",
        "currentValue": current,
        "options": [{"value": "default"}, {"value": "yolo"}],
    }


class FakeReasonixAcpProcess:
    """Reasonix ACP protocol fake whose ``.process`` is a real owned child."""

    def __init__(
        self,
        process: subprocess.Popen,
        *,
        timeout_prompt: bool = False,
        uncloseable: bool = False,
    ) -> None:
        self.process = process
        self.timeout_prompt = timeout_prompt
        self.uncloseable = uncloseable
        self.notifications: list[Mapping[str, Any]] = []
        self.last_request_id: int | None = None
        self.closed = False
        self._next_id = 1

    def _respond(self, result: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}

    def initialize(self) -> Mapping[str, Any]:
        return self._respond({"agentCapabilities": {"sessionCapabilities": {}}})

    def request(self, method: str, params: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        if method == "session/new":
            return self._respond({
                "sessionId": "session-1",
                "configOptions": [_tool_approval_option("default")],
            })
        if method == "session/set_config_option":
            return self._respond({
                "sessionId": "session-1",
                "configOptions": [_tool_approval_option(str(params["value"]))],
            })
        if method == "session/prompt":
            if self.timeout_prompt:
                self.last_request_id = self._next_id
                self._next_id += 1
                raise TimeoutError("Reasonix ACP session/prompt response timed out")
            self.last_request_id = self._next_id
            response_id = self._next_id
            self._next_id += 1
            self.notifications.append({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "session-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "OK"},
                    },
                },
            })
            return {"jsonrpc": "2.0", "id": response_id, "result": {"stopReason": "end_turn"}}
        raise AssertionError(f"unexpected ACP method {method}")

    def close(self) -> None:
        if self.uncloseable:
            raise OSError("cleanup failed to signal child")
        self.closed = True
        self.process.terminate()
        self.process.wait(timeout=5)


def make_reasonix_driver() -> ReasonixAcpDriver:
    return ReasonixAcpDriver(SessionCapabilities(), frozenset())


def test_reasonix_lifecycle_hook_order_and_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    recording = RecordingLease(lease)
    child = spawn_stub_child()
    fake = FakeReasonixAcpProcess(child)
    try:
        monkeypatch.setattr(ReasonixAcpProcess, "start", staticmethod(lambda **kwargs: fake))
        driver = make_reasonix_driver()
        request = make_request()
        result = driver.execute_session(request, driver.build_invocation(request), lifecycle=recording)
        assert result.result.final_output == "OK"
        assert recording.events == ["mark_launching", f"bind_child:{child.pid}", "complete"]
        assert fake.closed
        assert child.poll() is not None
        assert owner_status(tmp_path, RESOURCE) is None
    finally:
        kill_child(child)


def test_reasonix_transport_timeout_reaps_and_releases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    recording = RecordingLease(lease)
    child = spawn_stub_child()
    fake = FakeReasonixAcpProcess(child, timeout_prompt=True)
    try:
        monkeypatch.setattr(ReasonixAcpProcess, "start", staticmethod(lambda **kwargs: fake))
        driver = make_reasonix_driver()
        request = make_request()
        with pytest.raises(TimeoutError, match="timed out"):
            driver.execute_session(request, driver.build_invocation(request), lifecycle=recording)
        assert fake.closed
        assert child.poll() is not None
        assert recording.events[-1] == "complete"
        assert owner_status(tmp_path, RESOURCE) is None
    finally:
        kill_child(child)


def test_reasonix_cleanup_failure_retains_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    recording = RecordingLease(lease)
    child = spawn_stub_child()
    fake = FakeReasonixAcpProcess(child, uncloseable=True)
    try:
        monkeypatch.setattr(ReasonixAcpProcess, "start", staticmethod(lambda **kwargs: fake))
        driver = make_reasonix_driver()
        request = make_request()
        with pytest.raises(ResourceLeaseError) as excinfo:
            driver.execute_session(request, driver.build_invocation(request), lifecycle=recording)
        assert excinfo.value.stage == "cleanup"
        assert recording.events[-1] == "mark_unconfirmed"
        assert owner_status(tmp_path, RESOURCE) == "unconfirmed"
        os.kill(child.pid, 0)  # the child is still running
    finally:
        kill_child(child)


def test_dsh_transport_timeout_reaps_and_releases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    recording = RecordingLease(lease)
    child = spawn_stub_child()
    fake = FakeOwnedAcpProcess(child, timeout_prompt=True)
    try:
        monkeypatch.setattr(dsh.DshAcpProcess, "start", staticmethod(lambda **kwargs: fake))
        driver = make_dsh_driver()
        request = make_request()
        with pytest.raises(TimeoutError, match="timed out"):
            driver.execute_session(request, driver.build_invocation(request), lifecycle=recording)
        assert fake.closed
        assert child.poll() is not None
        assert recording.events[-1] == "complete"
        assert owner_status(tmp_path, RESOURCE) is None
    finally:
        kill_child(child)


def test_strands_transport_timeout_reaps_and_releases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    recording = RecordingLease(lease)
    child = spawn_stub_child()
    fake = FakeOwnedAcpProcess(child, timeout_prompt=True, agent_name="@strands-agents/cli")
    try:
        monkeypatch.setattr(strands.StrandsAcpProcess, "start", staticmethod(lambda **kwargs: fake))
        driver = strands.StrandsAcpDriver()
        request = make_request()
        with pytest.raises(TimeoutError, match="timed out"):
            driver.execute_session(request, driver.build_invocation(request), lifecycle=recording)
        assert fake.closed
        assert child.poll() is not None
        assert recording.events[-1] == "complete"
        assert owner_status(tmp_path, RESOURCE) is None
    finally:
        kill_child(child)


def test_strands_cleanup_failure_retains_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    recording = RecordingLease(lease)
    child = spawn_stub_child()
    fake = FakeOwnedAcpProcess(child, uncloseable=True, agent_name="@strands-agents/cli")
    try:
        monkeypatch.setattr(strands.StrandsAcpProcess, "start", staticmethod(lambda **kwargs: fake))
        driver = strands.StrandsAcpDriver()
        request = make_request()
        with pytest.raises(ResourceLeaseError) as excinfo:
            driver.execute_session(request, driver.build_invocation(request), lifecycle=recording)
        assert excinfo.value.stage == "cleanup"
        assert recording.events[-1] == "mark_unconfirmed"
        assert owner_status(tmp_path, RESOURCE) == "unconfirmed"
        os.kill(child.pid, 0)  # the child is still running
    finally:
        kill_child(child)


@pytest.mark.parametrize("driver_kind", ["reasonix", "dsh", "strands"])
def test_acp_missing_executable_releases_claim_and_allows_next_claimant(
    tmp_path: Path, driver_kind: str
) -> None:
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    missing = str(tmp_path / "definitely-missing-executable")
    if driver_kind == "reasonix":
        driver: Any = make_reasonix_driver()
        driver.executable = missing
    elif driver_kind == "dsh":
        driver = make_dsh_driver()
        driver.executable = missing
    else:
        driver = strands.StrandsAcpDriver(executable=missing)
    request = make_request()
    with pytest.raises(FileNotFoundError):
        driver.execute_session(request, driver.build_invocation(request), lifecycle=lease)
    # No childless launch intent remains in the durable journal.
    assert owner_status(tmp_path, RESOURCE) is None
    assert read_journal(tmp_path, RESOURCE)["queue"] == []
    # A second claimant can now acquire the released resource.
    assert store.enqueue(RESOURCE, make_spec("inv-2"), make_controller(2000)).state == "queued"
    assert store.try_acquire(RESOURCE, "inv-2", make_controller(2000)).state == "acquired"


def test_drivers_without_a_lifecycle_seam_are_rejected_before_launch() -> None:
    assert session_driver_accepts_lifecycle(NoSessionDriver()) is False
    assert session_driver_accepts_lifecycle(object()) is False
    assert session_driver_accepts_lifecycle(make_dsh_driver()) is True
    assert session_driver_accepts_lifecycle(strands.StrandsAcpDriver()) is True
    assert (
        session_driver_accepts_lifecycle(
            ReasonixAcpDriver(SessionCapabilities(), frozenset())
        )
        is True
    )


def test_reasonix_close_reaps_the_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aflow.harnesses.reasonix.REASONIX_ACP_CLOSE_GRACE_SECONDS", 0.2)
    monkeypatch.setattr("aflow.harnesses.reasonix.REASONIX_ACP_CLOSE_TERMINATE_SECONDS", 5.0)
    monkeypatch.setattr("aflow.harnesses.reasonix.REASONIX_ACP_CLOSE_KILL_SECONDS", 5.0)
    peer = write_script(
        tmp_path, "stubborn.py", "import time\ntime.sleep(120)\n"
    )
    popen = subprocess.Popen(
        [sys.executable, str(peer)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        owned = ReasonixAcpProcess(popen)
        owned.close()
        assert popen.poll() is not None
    finally:
        kill_child(popen)
