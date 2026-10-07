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
from aflow.process_identity import process_liveness
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
from aflow import workflow
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
    bound_pids: list[int] = []

    def reject_binding(resource, invocation_id, controller, child_pid, child_birth, process_group):
        bound_pids.append(child_pid)
        return er.Outcome("rejected", reason="not_owner")

    monkeypatch.setattr(
        store,
        "register_child",
        reject_binding,
    )
    script = write_script(
        tmp_path,
        "child.py",
        "import time\n"
        "time.sleep(30)\n",
    )
    with pytest.raises(ResourceLeaseError) as excinfo:
        _run_process(make_invocation((sys.executable, str(script))), tmp_path, FakeBanner(), make_state(), lease=lease)
    assert excinfo.value.stage == "register_child"
    assert isinstance(excinfo.value.__cause__, ResourceLeaseError)
    assert owner_status(tmp_path, RESOURCE) == "unconfirmed"
    # Registration may fail before the child interpreter executes its script.
    # The attempted binding identifies the actual child even in that window.
    assert len(bound_pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(bound_pids[0], 0)


def test_run_process_owner_stop_tears_down_owned_group_and_releases_claim(tmp_path: Path) -> None:
    """Owner stop kills the owned group and releases the claim on positive absence.

    The previous behavior retained the claim as unconfirmed while the child
    kept running; the exclusive-stop checkpoint changes that so a managed
    stop demonstrably ends the harness child and frees the resource.
    """
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
    child_pid: int | None = None
    ready_deadline = time.monotonic() + 10

    def control() -> None:
        nonlocal child_pid
        polls.append(1)
        ready_pid = pid_file.read_text().strip() if pid_file.exists() else ""
        if ready_pid.isdecimal():
            child_pid = int(ready_pid)
            raise RuntimeError("owner stop requested")
        assert time.monotonic() < ready_deadline, "local test child did not become ready"

    with pytest.raises(RuntimeError, match="owner stop"):
        _run_process(
            make_invocation((sys.executable, str(script))),
            tmp_path, FakeBanner(), make_state(),
            control_callback=control, lease=lease,
        )
    assert polls
    # The owned group was torn down: the harness child is gone and the claim
    # was released on positive group absence (no residual owner in the journal).
    assert owner_status(tmp_path, RESOURCE) is None
    child_pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_run_process_owner_stop_retains_claim_when_group_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the owned group cannot be proven absent, ownership is retained.

    A wrapper exit with a surviving descendant in the owned group (or any
    failure to positively confirm group absence) must keep the claim
    unconfirmed rather than release a resource that is still in use.
    """
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
    # Simulate a teardown that cannot positively confirm group absence (for
    # example a surviving provider descendant in the owned group).
    monkeypatch.setattr(
        workflow,
        "_reap_owned_process",
        lambda proc, process_group, grace_seconds=5.0: False,
    )
    child_pid: int | None = None
    ready_deadline = time.monotonic() + 10

    def control() -> None:
        nonlocal child_pid
        ready_pid = pid_file.read_text().strip() if pid_file.exists() else ""
        if ready_pid.isdecimal():
            child_pid = int(ready_pid)
            raise RuntimeError("owner stop requested")
        assert time.monotonic() < ready_deadline, "local test child did not become ready"

    try:
        with pytest.raises(RuntimeError, match="owner stop"):
            _run_process(
                make_invocation((sys.executable, str(script))),
                tmp_path, FakeBanner(), make_state(),
                control_callback=control, lease=lease,
            )
        assert owner_status(tmp_path, RESOURCE) == "unconfirmed"
        child_pid = int(pid_file.read_text())
        os.kill(child_pid, 0)  # the child must still be running
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child_pid, 0)
            except ChildProcessError:
                pass


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="owned process-group teardown uses native process-group signals",
)
def test_run_process_owner_stop_unleased_interrupt_reaps_owned_group(
    tmp_path: Path,
) -> None:
    """A foreground Ctrl-C of an unleased controller ends the owned provider.

    Ordinary nonexclusive runs pass ``lease=None``, so the interrupt path
    must still tear down the spawned owned process group before the
    KeyboardInterrupt propagates. The controller runs the real
    ``_run_process`` in its own session so SIGINT targets only the fixture
    controller's foreground group, and an unrelated decoy session must
    survive.
    """
    import signal as _signal

    from aflow.process_identity import process_birth_identity

    provider_pid_file = tmp_path / "provider.json"
    provider_script = write_script(
        tmp_path,
        "provider.py",
        "import json, os, time\n"
        f"with open({str(provider_pid_file)!r}, 'w') as fh:\n"
        "    json.dump({'pid': os.getpid(), 'pgid': os.getpgid(0), 'sid': os.getsid(0)}, fh)\n"
        "time.sleep(30)\n",
    )
    controller_script = write_script(
        tmp_path,
        "controller.py",
        "import sys\n"
        "from pathlib import Path\n"
        "from aflow import workflow\n"
        "from aflow.harnesses.base import HarnessInvocation\n"
        "class Banner:\n"
        "    def update(self, state):\n"
        "        pass\n"
        "invocation = HarnessInvocation(\n"
        f"    label='fixture', argv=(sys.executable, {str(provider_script)!r}),\n"
        "    env={}, prompt_mode='stdin', system_prompt='',\n"
        "    user_prompt='', effective_prompt='')\n"
        "try:\n"
        f"    workflow._run_process(invocation, Path({str(tmp_path)!r}), Banner(), None, lease=None)\n"
        "except KeyboardInterrupt:\n"
        "    sys.exit(130)\n",
    )
    decoy = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    provider_pid: int | None = None
    provider_birth: str | None = None
    provider_sid: int | None = None
    provider_pgid: int | None = None
    controller = subprocess.Popen(
        [sys.executable, str(controller_script)],
        cwd=str(tmp_path),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert os.getpgid(controller.pid) == controller.pid
        ready_deadline = time.monotonic() + 10
        while time.monotonic() < ready_deadline and not provider_pid_file.exists():
            assert controller.poll() is None, (
                "fixture controller exited before provider readiness"
            )
            time.sleep(0.01)
        assert provider_pid_file.exists(), "fixture provider did not become ready"
        record = json.loads(provider_pid_file.read_text(encoding="utf-8"))
        provider_pid = record["pid"]
        provider_birth = process_birth_identity(provider_pid)
        provider_sid = record["sid"]
        provider_pgid = record["pgid"]
        assert provider_birth
        assert provider_sid == controller.pid and provider_pgid == provider_pid
        assert process_birth_identity(provider_pid) == provider_birth
        # Terminal Ctrl-C: SIGINT to the fixture controller's foreground group.
        os.killpg(controller.pid, _signal.SIGINT)
        assert controller.wait(timeout=10) == 130
        # Positive provider cessation is required before any emergency cleanup.
        cessation_deadline = time.monotonic() + 5
        while (
            time.monotonic() < cessation_deadline
            and process_liveness(provider_pid) == "present"
        ):
            time.sleep(0.01)
        assert process_liveness(provider_pid) == "absent"
        # The interrupt propagated only through the fixture controller group:
        # the unrelated decoy session survives.
        assert decoy.poll() is None
    finally:
        if controller.poll() is None:
            controller.terminate()
            controller.wait(timeout=5)
        if provider_pid is not None and process_liveness(provider_pid) == "present":
            assert process_birth_identity(provider_pid) == provider_birth
            assert os.getsid(provider_pid) == provider_sid
            assert os.getpgid(provider_pid) == provider_pgid
            os.kill(provider_pid, _signal.SIGKILL)
        if provider_pid is not None and sys.platform == "linux":
            # The test process subreaps the fixture orphan: reap exactly this
            # owned child, never an unrelated one.
            try:
                os.waitpid(provider_pid, 0)
            except ChildProcessError:
                pass
            assert process_liveness(provider_pid) == "absent"
        if decoy.poll() is None:
            decoy.kill()
            decoy.wait(timeout=5)


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="owned process-group teardown uses native process-group signals",
)
def test_run_process_owner_stop_unleased_startup_banner_interrupt_reaps_owned_group(
    tmp_path: Path,
) -> None:
    """A foreground Ctrl-C during startup banner output ends the provider.

    The real ``_run_process`` renders the real BannerRenderer against a full
    native output pipe, so ``banner.update`` blocks before the polling loop
    that v09's cleanup boundary protected.  SIGINT to the fixture controller
    foreground group must tear down and reap the owned provider group before
    the KeyboardInterrupt propagates (controller exit 130), and an unrelated
    decoy session must survive.
    """
    import signal as _signal

    from aflow.process_identity import process_birth_identity

    provider_pid_file = tmp_path / "provider.json"
    provider_script = write_script(
        tmp_path,
        "provider.py",
        "import json, os, time\n"
        f"with open({str(provider_pid_file)!r}, 'w') as fh:\n"
        "    json.dump({'pid': os.getpid(), 'pgid': os.getpgid(0), 'sid': os.getsid(0)}, fh)\n"
        "time.sleep(30)\n",
    )
    (tmp_path / "fixture-plan.md").write_text("# Fixture plan\n", encoding="utf-8")
    controller_script = write_script(
        tmp_path,
        "controller.py",
        "import os, sys\n"
        "from pathlib import Path\n"
        "from aflow import workflow\n"
        "from aflow.harnesses.base import HarnessInvocation\n"
        "from aflow.status import BannerRenderer\n"
        "from aflow.run_state import ControllerState\n"
        "from aflow.plan import PlanSnapshot\n"
        "read_fd, write_fd = os.pipe()\n"
        "os.set_blocking(write_fd, False)\n"
        "while True:\n"
        "    try:\n"
        "        os.write(write_fd, b'x' * 4096)\n"
        "    except BlockingIOError:\n"
        "        break\n"
        "os.set_blocking(write_fd, True)\n"
        "class PipeStream:\n"
        "    def write(self, text):\n"
        "        os.write(write_fd, text.encode())\n"
        "    def flush(self):\n"
        "        pass\n"
        "banner = BannerRenderer(config_max_turns=1, config_plan_path=Path('fixture-plan.md'), stream=PipeStream())\n"
        "state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))\n"
        "invocation = HarnessInvocation(\n"
        f"    label='fixture', argv=(sys.executable, {str(provider_script)!r}),\n"
        "    env={}, prompt_mode='stdin', system_prompt='',\n"
        "    user_prompt='', effective_prompt='')\n"
        "try:\n"
        f"    workflow._run_process(invocation, Path({str(tmp_path)!r}), banner, state, lease=None)\n"
        "except KeyboardInterrupt:\n"
        "    sys.exit(130)\n",
    )
    decoy = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    provider_pid: int | None = None
    provider_birth: str | None = None
    provider_sid: int | None = None
    provider_pgid: int | None = None
    controller = subprocess.Popen(
        [sys.executable, str(controller_script)],
        cwd=str(tmp_path),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert os.getpgid(controller.pid) == controller.pid
        # Portable POSIX readiness barrier: the provider is ready when it has
        # written its identity file inside the controller's owned session.
        ready_deadline = time.monotonic() + 10
        while time.monotonic() < ready_deadline and not provider_pid_file.exists():
            assert controller.poll() is None, (
                "fixture controller exited before provider readiness"
            )
            time.sleep(0.01)
        assert provider_pid_file.exists(), "fixture provider did not become ready"
        record = json.loads(provider_pid_file.read_text(encoding="utf-8"))
        provider_pid = record["pid"]
        provider_birth = process_birth_identity(provider_pid)
        provider_sid = record["sid"]
        provider_pgid = record["pgid"]
        assert provider_birth
        assert provider_sid == controller.pid and provider_pgid == provider_pid
        assert process_birth_identity(provider_pid) == provider_birth
        if sys.platform == "linux":
            # Linux-only wchan guard: the real startup banner must be blocked
            # on the full output pipe (before the polling loop) when the
            # interrupt lands, so the exercised window is the unleased
            # startup path, not the already-protected polling path.
            blocked_deadline = time.monotonic() + 10
            blocked = ""
            while time.monotonic() < blocked_deadline:
                assert controller.poll() is None, (
                    "fixture controller exited before banner write"
                )
                blocked = Path(f"/proc/{controller.pid}/wchan").read_text().strip()
                if "pipe" in blocked:
                    break
                time.sleep(0.01)
            assert "pipe" in blocked, (
                "real startup banner did not block on full output pipe"
            )
        # Terminal Ctrl-C: SIGINT to the fixture controller's foreground group.
        os.killpg(controller.pid, _signal.SIGINT)
        assert controller.wait(timeout=10) == 130
        # Positive provider cessation is required before any emergency
        # cleanup; the direct child was reaped by the controller before the
        # interrupt propagated, so the provider is absent, not a zombie.
        cessation_deadline = time.monotonic() + 5
        while (
            time.monotonic() < cessation_deadline
            and process_liveness(provider_pid) == "present"
        ):
            time.sleep(0.01)
        assert process_liveness(provider_pid) == "absent"
        # The interrupt propagated only through the fixture controller group:
        # the unrelated decoy session survives.
        assert decoy.poll() is None
    finally:
        if controller.poll() is None:
            controller.terminate()
            controller.wait(timeout=5)
        if provider_pid is not None and process_liveness(provider_pid) == "present":
            assert process_birth_identity(provider_pid) == provider_birth
            assert os.getsid(provider_pid) == provider_sid
            assert os.getpgid(provider_pid) == provider_pgid
            os.kill(provider_pid, _signal.SIGKILL)
        if provider_pid is not None and sys.platform == "linux":
            # The test process subreaps the fixture orphan: reap exactly this
            # owned child, never an unrelated one.
            try:
                os.waitpid(provider_pid, 0)
            except ChildProcessError:
                pass
            assert process_liveness(provider_pid) == "absent"
        if decoy.poll() is None:
            decoy.kill()
            decoy.wait(timeout=5)


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="owned process-group teardown uses native process-group signals",
)
def test_run_process_owner_stop_unleased_startup_binding_interrupt_single_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupt during child-binding observation reaches one real teardown.

    The real spawn succeeds and the child leads its own group; the binding
    observation then raises the injected KeyboardInterrupt.  The original
    interrupt must propagate unchanged after exactly one delegated call to
    the real owned-group teardown, which reaps the directly owned child.
    """
    script = write_script(
        tmp_path,
        "child.py",
        "import time\n"
        "time.sleep(30)\n",
    )
    interrupt = KeyboardInterrupt()
    real_binding = workflow.owned_child_binding
    real_reap = workflow._reap_owned_process
    reaps: list[tuple[int, int | None]] = []

    def interrupt_after_observation(process) -> tuple[int, str | None, int | None]:
        real_binding(process)
        raise interrupt

    def counting_reap(proc, process_group, grace_seconds: float = 5.0) -> bool:
        reaps.append((proc.pid, process_group))
        return real_reap(proc, process_group, grace_seconds=grace_seconds)

    monkeypatch.setattr(workflow, "owned_child_binding", interrupt_after_observation)
    monkeypatch.setattr(workflow, "_reap_owned_process", counting_reap)
    with pytest.raises(KeyboardInterrupt) as excinfo:
        _run_process(
            make_invocation((sys.executable, str(script))),
            tmp_path, FakeBanner(), make_state(),
        )
    # The identical original interrupt propagated after the teardown.
    assert excinfo.value is interrupt
    # Exactly one teardown of the real owned child/group, and the child led
    # its own group on the supported platforms.
    assert len(reaps) == 1
    child_pid, process_group = reaps[0]
    assert process_group == child_pid
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_run_process_owner_stop_unleased_callback_stop_preserves_exception_and_no_broker_state(
    tmp_path: Path,
) -> None:
    """An unleased callback stop tears down the owned group, keeps identity.

    The control callback's typed exception must propagate unchanged, the
    spawned child group must be positively ceased before it does, and no
    exclusive broker journal may be created for a nonexclusive run.
    """
    pid_file = tmp_path / "child.pid"
    script = write_script(
        tmp_path,
        "child.py",
        f"import os\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "import time\n"
        "time.sleep(30)\n",
    )
    ready_deadline = time.monotonic() + 10
    stop = RuntimeError("unleased stop requested")

    def control() -> None:
        if not pid_file.exists():
            assert time.monotonic() < ready_deadline, (
                "local test child did not become ready"
            )
            return
        raise stop

    with pytest.raises(RuntimeError, match="unleased stop requested") as excinfo:
        _run_process(
            make_invocation((sys.executable, str(script))),
            tmp_path, FakeBanner(), make_state(),
            control_callback=control,
        )
    # The callback exception propagated with preserved identity and type.
    assert excinfo.value is stop
    # The owned child was torn down before the exception propagated.
    child_pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    # No exclusive broker state was created for the unleased run.
    assert list(tmp_path.glob("*.json")) == []


def test_run_process_success_path_retains_claim_when_descendant_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrapper exit cannot release the resource while a descendant lives.

    On the normal (non-exception) exit path the claim is released only when
    the owned group is positively gone.  A surviving provider descendant in
    the same group keeps the claim unconfirmed.
    """
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    # The child spawns a grandchild in the same group, then exits.  The
    # direct child is reaped but the group (grandchild) is still present.
    script = write_script(
        tmp_path,
        "child.py",
        "import os, subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "import time; time.sleep(0.2)\n"  # let the grandchild start
    )
    # Force the group-absence probe to report the group as still present so
    # the release is refused without depending on real-timing races.
    monkeypatch.setattr(
        workflow,
        "_owned_group_absent",
        lambda _pgid, grace_seconds=5.0: False,
    )
    completed = _run_process(
        make_invocation((sys.executable, str(script))),
        tmp_path, FakeBanner(), make_state(),
        lease=lease,
    )
    assert completed.returncode == 0
    assert owner_status(tmp_path, RESOURCE) == "unconfirmed"
    # Clean up the surviving grandchild group (same group as the wrapper).
    # The wrapper's group id equals its own pid; find it via the store owner.
    owner = read_journal(tmp_path, RESOURCE).get("owner") or {}
    group_id = owner.get("process_group")
    if group_id:
        try:
            os.killpg(int(group_id), 9)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def test_run_process_success_path_releases_claim_when_group_gone(
    tmp_path: Path
) -> None:
    """A clean wrapper exit with no surviving descendant releases the claim."""
    store = make_store(tmp_path)
    lease = make_lease(store, "inv-1")
    completed = _run_process(
        make_invocation((sys.executable, "-c", "print('ok')")),
        tmp_path, FakeBanner(), make_state(),
        lease=lease,
    )
    assert completed.returncode == 0
    assert "ok" in completed.stdout
    assert owner_status(tmp_path, RESOURCE) is None
    assert read_journal(tmp_path, RESOURCE)["queue"] == []


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


# -- SubprocessUnitManager local-adapter stop ----------------------------------
# These selections restore the local-adapter stop coverage that the separate
# harness group (``process_group=0``) in ``_run_process`` broke: a managed
# local controller must end its controller group AND its separate-group
# provider before reporting successful cessation.


def _write_provider_script(
    root: Path, pid_file: Path, ignore_term: bool
) -> Path:
    """A real provider: record its pid, then block (optionally ignoring TERM)."""
    signal_lines = (
        "import signal\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        if ignore_term
        else ""
    )
    body = (
        "import os\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        f"{signal_lines}"
        "import time\n"
        "time.sleep(120)\n"
    )
    return write_script(root, "provider.py", body)


def _write_controller_script(root: Path, provider_script: Path, repo_root: Path) -> Path:
    """A real local controller that runs the real ``_run_process``.

    ``_run_process`` spawns the provider with ``process_group=0`` on Linux,
    so the provider leads its own process group inside the controller's
    session - exactly the topology the local adapter stop must tear down.
    """
    body = (
        "import sys\n"
        "from aflow.harnesses.base import HarnessInvocation\n"
        "from aflow.plan import PlanSnapshot\n"
        "from aflow.run_state import ControllerState\n"
        "from aflow.workflow import _run_process\n"
        "\n"
        "class _Banner:\n"
        "    def update(self, state):\n"
        "        pass\n"
        "\n"
        f"invocation = HarnessInvocation(\n"
        f"    label='provider',\n"
        f"    argv=(sys.executable, {str(provider_script)!r}),\n"
        f"    env={{}},\n"
        f"    prompt_mode='stdin',\n"
        f"    system_prompt='',\n"
        f"    user_prompt='',\n"
        f"    effective_prompt='',\n"
        f")\n"
        f"state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))\n"
        f"_run_process(invocation, {str(repo_root)!r}, _Banner(), state)\n"
    )
    return write_script(root, "controller.py", body)


def _spawn_decoy() -> subprocess.Popen:
    """An unrelated process in its own session; a correct stop leaves it alive."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_provider(pid_file: Path, timeout: float = 20.0) -> int:
    deadline = time.monotonic() + timeout
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pid_file.exists(), "local test provider did not become ready"
    return int(pid_file.read_text(encoding="utf-8").strip())


def _reap_owned_child(pid: int) -> None:
    """Reap an adopted fixture child in cleanup, using its owned identity.

    Only a direct child of this test process is reaped (``ChildProcessError``
    means the kernel already reparented it elsewhere); a positively dead
    unreaped zombie is otherwise left to its real parent.
    """
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


def _fault_inject_unknown_birth(
    pi: object, controller_pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make the native birth observation fail for the live controller.

    Linux: the procfs birth read returns no suffix.  Darwin: the bounded
    ``ps -o pid=,lstart= -p <pid>`` birth probe times out.  Both leave every
    other observation (and every other PID) untouched, so the contract
    decision sees no birth and the topology is unknown.
    """
    if sys.platform == "linux":
        monkeypatch.setattr(pi, "_proc_stat_suffix", lambda pid: None)
        return
    real_run = pi.subprocess.run  # type: ignore[attr-defined,union-attr]

    def flaky_run(argv, *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        parts = [str(part) for part in argv]
        # The single-PID bounded birth probe is ``ps -o pid=,lstart= -p <pid>``;
        # fail it for the controller and delegate every other probe.
        if (
            "lstart=" in parts
            and "-p" in parts
            and parts[parts.index("-p") + 1] == str(controller_pid)
        ):
            raise subprocess.TimeoutExpired(argv, 0.1)
        return real_run(argv, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pi.subprocess, "run", flaky_run)  # type: ignore[attr-defined,union-attr]



def test_subprocess_unit_stop_terminates_separate_group_provider(tmp_path: Path) -> None:
    """Ordinary stop ends the controller AND its separate-group provider.

    The provider runs in its own process group (``process_group=0``) inside
    the controller's session.  A correct local-adapter stop must positively
    cease both before reporting inactive, while an unrelated decoy survives.
    """
    from aflow.control_plane import SubprocessUnitManager

    provider_pid_file = tmp_path / "provider.pid"
    provider_script = _write_provider_script(tmp_path, provider_pid_file, ignore_term=False)
    controller_script = _write_controller_script(tmp_path, provider_script, tmp_path)
    manager = SubprocessUnitManager(stop_timeout_seconds=2.0)
    decoy = _spawn_decoy()
    state = manager.start(
        "aflow-run-local-stop.service",
        (sys.executable, str(controller_script)),
        cwd=tmp_path,
    )
    try:
        provider_pid = _wait_for_provider(provider_pid_file)
        controller_pid = state.main_pid
        assert controller_pid is not None
        # Exact fixture identities: the controller is the session leader and
        # the provider leads a distinct group inside the same session.
        assert os.getpgid(controller_pid) == controller_pid
        assert os.getsid(controller_pid) == controller_pid
        assert os.getpgid(provider_pid) == provider_pid
        assert provider_pid != controller_pid
        assert os.getsid(provider_pid) == controller_pid
        terminal = manager.stop(state.name)
        assert terminal is not None
        assert terminal.active_state == "inactive"
        # Complete owned cessation before stop success: both the controller
        # and the separate-group provider are positively ceased.  The
        # controller (a directly owned Popen) was reaped by the manager; the
        # separately grouped provider may still be an unreaped zombie whose
        # birth is unobservable, so its cessation is asserted through the
        # platform liveness contract, not an immediate ESRCH from kill(0).
        with pytest.raises(ProcessLookupError):
            os.kill(controller_pid, 0)
        assert process_liveness(provider_pid) == "absent"
        # The unrelated decoy in its own session must survive.
        assert decoy.poll() is None
    finally:
        manager.shutdown()
        _reap_owned_child(provider_pid)
        if decoy.poll() is None:
            decoy.kill()
            decoy.wait(timeout=5)


def test_subprocess_unit_stop_shutdown_drains_separate_group_provider(tmp_path: Path) -> None:
    """``shutdown`` applies the same session stop to every managed unit."""
    from aflow.control_plane import SubprocessUnitManager

    provider_pid_file = tmp_path / "provider.pid"
    provider_script = _write_provider_script(tmp_path, provider_pid_file, ignore_term=False)
    controller_script = _write_controller_script(tmp_path, provider_script, tmp_path)
    manager = SubprocessUnitManager(stop_timeout_seconds=2.0)
    state = manager.start(
        "aflow-run-local-shutdown.service",
        (sys.executable, str(controller_script)),
        cwd=tmp_path,
    )
    try:
        provider_pid = _wait_for_provider(provider_pid_file)
        controller_pid = state.main_pid
        assert os.getpgid(provider_pid) == provider_pid
        assert os.getsid(provider_pid) == controller_pid
        terminal = manager.shutdown()
        assert {s.name for s in terminal} == {"aflow-run-local-shutdown.service"}
        assert all(s.active_state == "inactive" for s in terminal)
        with pytest.raises(ProcessLookupError):
            os.kill(controller_pid, 0)
        # The provider may be an unreaped zombie: assert positive cessation
        # through the platform liveness contract, not an immediate ESRCH.
        assert process_liveness(provider_pid) == "absent"
    finally:
        manager.shutdown()
        _reap_owned_child(provider_pid)


def test_subprocess_unit_stop_escalates_kill_for_term_ignoring_provider(
    tmp_path: Path
) -> None:
    """A provider that ignores TERM is escalated to KILL before success.

    Readiness-gated: the provider must be up in its own group before the stop
    begins.  The separate-group TERM survivor is KILLed (its own group is
    proven owned) and positively ceased before the stop reports inactive.
    """
    from aflow.control_plane import SubprocessUnitManager

    provider_pid_file = tmp_path / "provider.pid"
    provider_script = _write_provider_script(tmp_path, provider_pid_file, ignore_term=True)
    controller_script = _write_controller_script(tmp_path, provider_script, tmp_path)
    manager = SubprocessUnitManager(stop_timeout_seconds=0.5)
    state = manager.start(
        "aflow-run-local-kill.service",
        (sys.executable, str(controller_script)),
        cwd=tmp_path,
    )
    try:
        provider_pid = _wait_for_provider(provider_pid_file)
        controller_pid = state.main_pid
        assert os.getpgid(provider_pid) == provider_pid
        assert os.getsid(provider_pid) == controller_pid
        terminal = manager.stop(state.name)
        assert terminal is not None
        assert terminal.active_state == "inactive"
        with pytest.raises(ProcessLookupError):
            os.kill(controller_pid, 0)
        # The TERM-ignoring provider is KILLed and positively ceased; it may
        # be an unreaped zombie, so assert through liveness, not kill(0).
        assert process_liveness(provider_pid) == "absent"
    finally:
        manager.shutdown()
        _reap_owned_child(provider_pid)


def test_subprocess_unit_stop_unknown_live_contract_raises_and_retains_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live controller with an unknown topology never stops successfully.

    The controller runs the real ``_run_process`` with a separate-group
    provider, and the native birth observation fails (inaccessible procfs
    inside the actual contract decision).  The unit must fail typed before
    any group signal, retain the unit, leave the provider and a decoy alive,
    and stop normally once observation is restored.
    """
    from aflow import process_identity as pi
    from aflow.control_plane import SubprocessUnitManager

    provider_pid_file = tmp_path / "provider.pid"
    provider_script = _write_provider_script(tmp_path, provider_pid_file, ignore_term=False)
    controller_script = _write_controller_script(tmp_path, provider_script, tmp_path)
    manager = SubprocessUnitManager(stop_timeout_seconds=2.0)
    decoy = _spawn_decoy()
    state = manager.start(
        "aflow-run-local-unknown.service",
        (sys.executable, str(controller_script)),
        cwd=tmp_path,
    )
    try:
        provider_pid = _wait_for_provider(provider_pid_file)
        controller_pid = state.main_pid
        assert os.getpgid(provider_pid) == provider_pid
        assert os.getsid(provider_pid) == controller_pid
        # Native birth observation fails for the live controller: the
        # contract decision sees no birth, so the topology is unknown.
        _fault_inject_unknown_birth(pi, controller_pid, monkeypatch)
        with pytest.raises(RuntimeError, match="controller topology is unknown"):
            manager.stop(state.name)
        # No group signal reached the live controller or its provider, the
        # decoy survives, and the unit is retained by the manager.
        assert os.getsid(controller_pid) == controller_pid
        assert os.getsid(provider_pid) == controller_pid
        assert decoy.poll() is None
        retained = manager.get(state.name)
        assert retained is not None
        assert retained.active_state == "active"
        # Once native observation is restored, the ordinary stop succeeds and
        # positively ceases the whole owned session.
        monkeypatch.undo()
        terminal = manager.stop(state.name)
        assert terminal is not None
        assert terminal.active_state == "inactive"
        with pytest.raises(ProcessLookupError):
            os.kill(controller_pid, 0)
        assert process_liveness(provider_pid) == "absent"
        assert decoy.poll() is None
    finally:
        manager.shutdown()
        _reap_owned_child(provider_pid)
        if decoy.poll() is None:
            decoy.kill()
            decoy.wait(timeout=5)


def test_subprocess_unit_stop_unknown_shutdown_raises_and_retains_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``shutdown`` shares the typed unknown-topology failure behavior."""
    from aflow import process_identity as pi
    from aflow.control_plane import SubprocessUnitManager

    provider_pid_file = tmp_path / "provider.pid"
    provider_script = _write_provider_script(tmp_path, provider_pid_file, ignore_term=False)
    controller_script = _write_controller_script(tmp_path, provider_script, tmp_path)
    manager = SubprocessUnitManager(stop_timeout_seconds=2.0)
    decoy = _spawn_decoy()
    state = manager.start(
        "aflow-run-local-unknown-shutdown.service",
        (sys.executable, str(controller_script)),
        cwd=tmp_path,
    )
    try:
        provider_pid = _wait_for_provider(provider_pid_file)
        controller_pid = state.main_pid
        assert os.getpgid(provider_pid) == provider_pid
        assert os.getsid(provider_pid) == controller_pid
        _fault_inject_unknown_birth(pi, controller_pid, monkeypatch)
        with pytest.raises(RuntimeError, match="controller topology is unknown"):
            manager.shutdown()
        # No group signal, decoy survives, unit retained.
        assert os.getsid(controller_pid) == controller_pid
        assert os.getsid(provider_pid) == controller_pid
        assert decoy.poll() is None
        retained = manager.get(state.name)
        assert retained is not None
        assert retained.active_state == "active"
        # Restored observation: shutdown completes the owned cessation.
        monkeypatch.undo()
        terminal = manager.shutdown()
        assert {s.name for s in terminal} == {"aflow-run-local-unknown-shutdown.service"}
        assert all(s.active_state == "inactive" for s in terminal)
        with pytest.raises(ProcessLookupError):
            os.kill(controller_pid, 0)
        assert process_liveness(provider_pid) == "absent"
        assert decoy.poll() is None
    finally:
        manager.shutdown()
        _reap_owned_child(provider_pid)
        if decoy.poll() is None:
            decoy.kill()
            decoy.wait(timeout=5)


@pytest.mark.skipif(
    sys.platform != "linux", reason="subreaper and zombie reaping are Linux-specific"
)
def test_subprocess_unit_stop_unreaped_zombie_provider_delayed_reap(
    tmp_path: Path,
) -> None:
    """A deliberately unreaped zombie provider is positively ceased.

    The test process becomes an owned child subreaper so the terminated
    provider is adopted here as a zombie; reaping is deliberately delayed
    until after the cessation assertion.  The assertion must therefore use
    the platform liveness contract (an unreaped zombie is positively ceased)
    rather than an immediate ESRCH from kill(0).
    """
    import ctypes

    from aflow.control_plane import SubprocessUnitManager

    PR_SET_CHILD_SUBREAPER = 36
    # prctl returns 0 on success and -1 (with errno) on failure.
    assert ctypes.CDLL("libc.so.6").prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0

    provider_pid_file = tmp_path / "provider.pid"
    provider_script = _write_provider_script(tmp_path, provider_pid_file, ignore_term=False)
    controller_script = _write_controller_script(tmp_path, provider_script, tmp_path)
    manager = SubprocessUnitManager(stop_timeout_seconds=2.0)
    state = manager.start(
        "aflow-run-local-zombie.service",
        (sys.executable, str(controller_script)),
        cwd=tmp_path,
    )
    try:
        provider_pid = _wait_for_provider(provider_pid_file)
        controller_pid = state.main_pid
        assert os.getpgid(provider_pid) == provider_pid
        assert os.getsid(provider_pid) == controller_pid
        terminal = manager.stop(state.name)
        assert terminal is not None
        assert terminal.active_state == "inactive"
        with pytest.raises(ProcessLookupError):
            os.kill(controller_pid, 0)
        # The adopted provider is a deliberately unreaped zombie: kill(0)
        # succeeds, yet it is positively ceased under the platform contract.
        os.kill(provider_pid, 0)
        assert process_liveness(provider_pid) == "absent"
    finally:
        manager.shutdown()
        # Delayed reaping of the owned fixture child, after all assertions.
        try:
            os.waitpid(provider_pid, 0)
        except ChildProcessError:
            pass


# -- local adapter stop: initial budget and exited-controller drain ----------
# The local adapter shares the persistent path's stop contract: one absolute
# initial operation (<=2s) created before the first snapshot is threaded to
# the inventory, the original-controller revalidation, and the initial proof;
# and an already-exited controller drains its group with bounded observations
# that never renew time past the outer deadline.


def test_local_adapter_initial_snapshot_and_proof_share_one_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local adapter's initial inventory and proofs share one <=2s budget.

    The v06 defect renewed the observation window after the initial inventory
    in both adapters; this records the exact deadlines the inventory, the
    original-controller revalidation, and the initial proof receive in the
    local path and asserts they are the one shared value bounded by
    ``stop_start + 2s``, never the larger outer deadline.
    """
    from aflow import process_identity as pi
    from aflow.control_plane import persistent_units as pu
    from aflow.control_plane import units as units_mod
    from aflow.control_plane.units import SubprocessUnitManager

    manager = SubprocessUnitManager(stop_timeout_seconds=10)

    class FakeProcess:
        pid = 900001

        def poll(self) -> int | None:
            return None

        def wait(self) -> int | None:
            return None

    manager._units["aflow-run-local-init.service"] = FakeProcess()
    seen: dict[str, float] = {}

    monkeypatch.setattr(
        units_mod, "process_birth_bounded", lambda pid, deadline: "ps-lstart:original"
    )
    monkeypatch.setattr(
        pu,
        "session_contract",
        lambda pid, birth, deadline: pu._SessionContract("session", session_id=900001),
    )

    def snapshot(session_id, deadline):
        seen["snapshot"] = deadline
        return [pi.SessionMember(900001, "ps-lstart:original", 900001)]

    def controller_owned(pid, birth, session_id, deadline):
        seen["controller"] = deadline
        return True

    def stop_groups(session_id, captured, deadline, **kwargs):
        seen["groups_outer"] = deadline
        seen["groups_initial"] = kwargs.get("initial_operation")
        return True

    monkeypatch.setattr(pu, "session_snapshot", snapshot)
    monkeypatch.setattr(units_mod, "controller_group_owned", controller_owned)
    monkeypatch.setattr(pu, "stop_session_groups", stop_groups)

    state = manager.stop("aflow-run-local-init.service")
    assert state is not None
    # All three initial-proof calls receive the one shared initial operation,
    # bounded by stop_start + 2s, never the 10s outer deadline.
    assert seen["snapshot"] == seen["controller"] == seen["groups_initial"], (
        "inventory, controller revalidation, and initial proof share one deadline"
    )
    assert seen["groups_initial"] <= seen["groups_outer"] - 7.0, (
        "the shared initial budget is <=2s, not the 10s outer deadline"
    )


def test_local_adapter_exited_controller_drain_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drained exited-controller group gets no late KILL or success.

    The controller has already exited with a recorded return code; the owned
    provider group is still present.  The stop budget is 20ms of TERM grace
    plus the 2.0s KILL grace (outer deadline 101.99); the first bounded group
    read takes 1.9s (completing in budget) and every later read takes 2.0s
    (completing at/after the outer deadline, i.e. expired evidence).  The
    in-budget initial TERM is the only signal: the KILL gate's fresh group
    read is expired, so no KILL is issued and no success is reported without
    positive group absence.
    """
    from aflow.control_plane import persistent_units as pu
    from aflow.control_plane import units as units_mod
    from aflow.control_plane.units import SubprocessUnitManager

    manager = SubprocessUnitManager(stop_timeout_seconds=0.02)

    class FakeProcess:
        pid = 900001

        def poll(self) -> int | None:
            return 0

        def wait(self) -> int | None:
            return 0

    manager._units["aflow-run-local-drain.service"] = FakeProcess()
    signals: list[tuple[int, object]] = []
    manager._signal_pgid = lambda pgid, sig: signals.append((pgid, sig.name))

    clock = {"now": 99.97}

    def bounded_birth(pid, deadline):
        clock["now"] += 0.020
        # The exited controller's birth read is within budget; the group is
        # positively present, so the drain path owns it.
        return "linux-start-ticks:1"

    reads = {"n": 0}

    def group_state(session_id, deadline):
        # The first bounded group read takes 1.9s (completing in budget);
        # every later read takes 2.0s (completing at/after the outer
        # deadline, i.e. expired evidence, never a group state).
        reads["n"] += 1
        clock["now"] += 1.9 if reads["n"] == 1 else 2.0
        if clock["now"] >= deadline:
            return "unknown"
        return "present"

    monkeypatch.setattr(units_mod, "process_birth_bounded", bounded_birth)
    # A vanished/exited controller has no observable session identity: the
    # contract is unknown, so the stop takes the exited-leader drain path.
    monkeypatch.setattr(pu.os, "getsid", lambda pid: None)
    monkeypatch.setattr(pu.os, "getpgid", lambda pid: None)
    monkeypatch.setattr(units_mod, "process_group_state", group_state)
    # The mock clock is the single source of time so the outer deadline
    # (103.92) genuinely expires.
    clock["now"] = 99.97
    monkeypatch.setattr(units_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(units_mod.time, "sleep", lambda s: clock.update(now=clock["now"] + s))

    with pytest.raises(RuntimeError, match="process group did not terminate"):
        manager.stop("aflow-run-local-drain.service")
    # The only signal is the in-budget initial TERM; no late KILL follows once
    # the budget expires, and no success is reported without positive absence.
    assert signals == [(900001, "SIGTERM")], "no KILL after the outer deadline expired"
    assert clock["now"] <= 101.99 + 2.0 + 1e-9, (
        "no read extends more than one bounded window past the outer deadline"
    )
