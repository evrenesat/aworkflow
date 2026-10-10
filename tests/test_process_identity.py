"""Focused tests for portable process-birth identity observations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from aflow import process_identity
from aflow import ui_cli
from aflow.control_plane import persistent_units, run_activity


class _IsolatedOs:
    """A module-local stand-in for the ``os`` module used only by this test
    module's own fixture code.

    Every ordinary attribute delegates to the real ``os`` module, so
    production process probes (which import ``os`` themselves) and
    independently owned subprocess timeout cleanup (which uses the real
    ``os.kill``) are never intercepted.  Only this module's ``kill`` reference
    is replaced, so the fixture's attempted signals are recorded and the
    supplied policy decides their outcome (a rejected nonzero signal stays
    fatal).  This isolates the fixture's signal interception from the
    process-global ``os`` module object.
    """

    def __init__(self, real_os: object, policy) -> None:
        object.__setattr__(self, "_real", real_os)
        object.__setattr__(self, "_policy", policy)

    def __getattr__(self, name: str) -> object:
        return getattr(object.__getattribute__(self, "_real"), name)

    def kill(self, pid: int, signum: int) -> None:
        object.__getattribute__(self, "_policy")(pid, signum)


def _install_fixture_signal_interceptor(
    monkeypatch: pytest.MonkeyPatch, policy
) -> None:
    """Patch this module's own ``os`` binding to ``_IsolatedOs``.

    The real ``os`` module is captured before the binding is replaced, so the
    proxy delegates every ordinary attribute (``getpgid``, ``getsid``, ...) to
    the genuine module while only ``kill`` is intercepted.  Production
    process probes and ``subprocess`` retain the real ``os.kill``.
    """
    monkeypatch.setattr(sys.modules[__name__], "os", _IsolatedOs(real_os=os, policy=policy))


def _proc_stat(*, state: str = "S", start_ticks: str = "4242") -> str:
    suffix = [state, *[str(index) for index in range(1, 19)], start_ticks]
    return f"123 (fixture process) {' '.join(suffix)}"


def _mock_proc_reads(
    monkeypatch: pytest.MonkeyPatch,
    target_stat: str | BaseException,
    *,
    self_stat: str | BaseException | None = None,
) -> None:
    if self_stat is None:
        self_stat = _proc_stat()

    def fake_read(path, *, encoding: str) -> str:
        assert encoding == "utf-8"
        value = self_stat if str(path) == "/proc/self/stat" else target_stat
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(process_identity.Path, "read_text", fake_read)


def test_current_process_has_a_stable_identity() -> None:
    identity = process_identity.process_birth_identity(os.getpid())

    assert identity is not None
    assert identity.startswith(("linux-start-ticks:", "ps-lstart:"))


@pytest.mark.parametrize("pid", [0, -1])
def test_nonpositive_pid_is_not_alive(monkeypatch: pytest.MonkeyPatch, pid: int) -> None:
    def fail(*args, **kwargs):
        raise AssertionError("nonpositive PIDs must not be probed")

    monkeypatch.setattr(process_identity.Path, "read_text", fail)
    monkeypatch.setattr(process_identity.subprocess, "run", fail)

    assert process_identity.process_birth_identity(pid) is None


@pytest.mark.skipif(sys.platform != "linux", reason="/proc start-tick behavior is Linux-specific")
@pytest.mark.parametrize(
    ("state", "expected"),
    [("S", "linux-start-ticks:4242"), ("Z", None)],
)
def test_linux_proc_identity_uses_start_ticks_and_rejects_zombies(
    monkeypatch: pytest.MonkeyPatch, state: str, expected: str | None
) -> None:
    suffix = [state, *[str(index) for index in range(1, 19)], "4242"]

    class FakeProcStat:
        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            return f"123 (fixture process) {' '.join(suffix)}"

    monkeypatch.setattr(process_identity, "Path", lambda _: FakeProcStat())
    ps_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    signal0_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_identity.subprocess,
        "run",
        lambda *args, **kwargs: ps_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        process_identity.os,
        "kill",
        lambda pid, signal: signal0_calls.append((pid, signal)),
    )

    assert process_identity.process_birth_identity(123) == expected
    assert ps_calls == []
    assert signal0_calls == []


def test_missing_linux_pid_avoids_ps_when_absence_is_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    _mock_proc_reads(monkeypatch, FileNotFoundError("missing target"))
    signal0_calls: list[tuple[int, int]] = []
    ps_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_kill(pid: int, signal: int) -> None:
        signal0_calls.append((pid, signal))
        raise ProcessLookupError("missing target")

    def fake_run(*args, **kwargs):
        ps_calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 1, stdout="", stderr="")

    monkeypatch.setattr(process_identity.os, "kill", fake_kill)
    monkeypatch.setattr(process_identity.subprocess, "run", fake_run)

    pids = [123, 456, 789]
    assert [process_identity.process_birth_identity(pid) for pid in pids] == [
        None,
        None,
        None,
    ]
    assert signal0_calls == [(pid, 0) for pid in pids]
    assert ps_calls == []


@pytest.mark.parametrize(
    "signal0_outcome",
    ["success", PermissionError, OSError],
    ids=["success", "permission-denied", "other-os-error"],
)
def test_missing_linux_pid_keeps_fallback_when_existence_is_uncertain(
    monkeypatch: pytest.MonkeyPatch, signal0_outcome: str | type[OSError]
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    _mock_proc_reads(monkeypatch, FileNotFoundError("missing target"))
    signal0_calls: list[tuple[int, int]] = []
    ps_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_kill(pid: int, signal: int) -> None:
        signal0_calls.append((pid, signal))
        if signal0_outcome != "success":
            raise signal0_outcome("existence uncertain")

    def fake_run(*args, **kwargs):
        ps_calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args[0], 0, stdout="Mon Sep  8 12:34:56 2026\n", stderr=""
        )

    monkeypatch.setattr(process_identity.os, "kill", fake_kill)
    monkeypatch.setattr(process_identity.subprocess, "run", fake_run)

    assert (
        process_identity.process_birth_identity(123)
        == "ps-lstart:Mon Sep  8 12:34:56 2026"
    )
    assert signal0_calls == [(123, 0)]
    assert ps_calls == [
        (
            (("ps", "-o", "lstart=", "-p", "123"),),
            {"check": False, "capture_output": True, "text": True, "timeout": 5},
        )
    ]


@pytest.mark.parametrize(
    "self_observation",
    [
        FileNotFoundError("missing procfs"),
        PermissionError("procfs denied"),
        "malformed proc stat",
    ],
    ids=["missing", "permission-denied", "malformed"],
)
def test_missing_proc_uses_fallback_when_procfs_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, self_observation: str | BaseException
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    _mock_proc_reads(
        monkeypatch,
        FileNotFoundError("missing target"),
        self_stat=self_observation,
    )
    signal0_calls: list[tuple[int, int]] = []
    ps_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_run(*args, **kwargs):
        ps_calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args[0], 0, stdout="Mon Sep  8 12:34:56 2026\n", stderr=""
        )

    monkeypatch.setattr(
        process_identity.os,
        "kill",
        lambda pid, signal: signal0_calls.append((pid, signal)),
    )
    monkeypatch.setattr(process_identity.subprocess, "run", fake_run)

    assert (
        process_identity.process_birth_identity(123)
        == "ps-lstart:Mon Sep  8 12:34:56 2026"
    )
    assert signal0_calls == []
    assert len(ps_calls) == 1


@pytest.mark.parametrize(
    "target_observation",
    [PermissionError("target denied"), IndexError("malformed target")],
    ids=["permission-denied", "malformed"],
)
def test_target_permission_or_malformed_proc_keeps_ps_fallback(
    monkeypatch: pytest.MonkeyPatch, target_observation: BaseException
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    _mock_proc_reads(
        monkeypatch,
        target_observation,
        self_stat=AssertionError("generic target failures must not inspect self"),
    )
    signal0_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_identity.os,
        "kill",
        lambda pid, signal: signal0_calls.append((pid, signal)),
    )
    monkeypatch.setattr(
        process_identity.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="Mon Sep  8 12:34:56 2026\n", stderr=""
        ),
    )

    assert (
        process_identity.process_birth_identity(123)
        == "ps-lstart:Mon Sep  8 12:34:56 2026"
    )
    assert signal0_calls == []


def test_non_linux_missing_proc_keeps_ps_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "darwin")
    _mock_proc_reads(
        monkeypatch,
        FileNotFoundError("missing target"),
        self_stat=AssertionError("non-Linux fallback must not inspect self"),
    )
    signal0_calls: list[tuple[int, int]] = []
    ps_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_run(*args, **kwargs):
        ps_calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args[0], 0, stdout="Mon Sep  8 12:34:56 2026\n", stderr=""
        )

    monkeypatch.setattr(
        process_identity.os,
        "kill",
        lambda pid, signal: signal0_calls.append((pid, signal)),
    )
    monkeypatch.setattr(process_identity.subprocess, "run", fake_run)

    assert (
        process_identity.process_birth_identity(123)
        == "ps-lstart:Mon Sep  8 12:34:56 2026"
    )
    assert signal0_calls == []
    assert len(ps_calls) == 1


def test_process_appearing_after_missing_observation_is_read_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    target_observations: list[str | BaseException] = [
        FileNotFoundError("missing target"),
        _proc_stat(start_ticks="9876"),
    ]
    target_reads: list[str] = []
    self_reads: list[str] = []

    def fake_read(path, *, encoding: str) -> str:
        assert encoding == "utf-8"
        path_text = str(path)
        if path_text == "/proc/self/stat":
            self_reads.append(path_text)
            return _proc_stat()
        target_reads.append(path_text)
        observation = target_observations.pop(0)
        if isinstance(observation, BaseException):
            raise observation
        return observation

    signal0_calls: list[tuple[int, int]] = []
    ps_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_kill(pid: int, signal: int) -> None:
        signal0_calls.append((pid, signal))
        raise ProcessLookupError("missing target")

    def fake_run(*args, **kwargs):
        ps_calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 1, stdout="", stderr="")

    monkeypatch.setattr(process_identity.Path, "read_text", fake_read)
    monkeypatch.setattr(process_identity.os, "kill", fake_kill)
    monkeypatch.setattr(process_identity.subprocess, "run", fake_run)

    assert process_identity.process_birth_identity(321) is None
    assert process_identity.process_birth_identity(321) == "linux-start-ticks:9876"
    assert target_reads == ["/proc/321/stat", "/proc/321/stat"]
    assert self_reads == ["/proc/self/stat"]
    assert signal0_calls == [(321, 0)]
    assert ps_calls == []


def _missing_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    class MissingProcStat:
        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            raise OSError("no proc entry")

    monkeypatch.setattr(process_identity, "Path", lambda _: MissingProcStat())


def test_ps_fallback_returns_birth_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    _missing_proc(monkeypatch)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args[0], 0, stdout="Mon Sep  8 12:34:56 2026\n", stderr=""
        )

    monkeypatch.setattr(process_identity.subprocess, "run", fake_run)

    assert (
        process_identity.process_birth_identity(123)
        == "ps-lstart:Mon Sep  8 12:34:56 2026"
    )
    assert calls == [
        (
            (("ps", "-o", "lstart=", "-p", "123"),),
            {"check": False, "capture_output": True, "text": True, "timeout": 5},
        )
    ]


def test_ps_fallback_failure_and_dead_pid_return_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _missing_proc(monkeypatch)
    monkeypatch.setattr(
        process_identity.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout="secret process output\n", stderr="secret"
        ),
    )

    assert process_identity.process_birth_identity(2_000_000_000) is None


def test_process_liveness_reports_confirmed_absence_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    _mock_proc_reads(monkeypatch, FileNotFoundError("missing target"))
    monkeypatch.setattr(process_identity, "_linux_procfs_is_usable", lambda: True)
    monkeypatch.setattr(
        process_identity.os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(ProcessLookupError("gone")),
    )

    assert process_identity.process_liveness(123) == "absent"


def test_process_liveness_keeps_unavailable_identity_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    _mock_proc_reads(monkeypatch, PermissionError("target denied"))
    monkeypatch.setattr(
        process_identity.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout="", stderr="permission denied"
        ),
    )

    assert process_identity.process_liveness(123) == "unknown"


def test_host_boot_identity_reads_linux_boot_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    value = "11223344-5566-7788-99aa-bbccddeeff00"

    class FakeBootFile:
        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            return f"{value}\n"

    monkeypatch.setattr(process_identity, "Path", lambda _: FakeBootFile())
    assert process_identity.host_boot_identity() == value


@pytest.mark.skipif(sys.platform != "linux", reason="real boot id file is Linux-specific")
def test_host_boot_identity_is_observable_on_this_host() -> None:
    assert process_identity.host_boot_identity() is not None


@pytest.mark.parametrize("failure", [OSError("no sysctl"), subprocess.TimeoutExpired("sysctl", 5)])
def test_host_boot_identity_darwin_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "darwin")

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(process_identity.subprocess, "run", fail)
    assert process_identity.host_boot_identity() is None


def test_host_boot_identity_darwin_boot_session_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "darwin")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="boot-session-uuid\n")

    monkeypatch.setattr(process_identity.subprocess, "run", fake_run)
    assert process_identity.host_boot_identity() == "boot-session-uuid"
    assert calls == [
        (
            ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
            {"capture_output": True, "text": True, "timeout": 5, "check": False},
        )
    ]


def test_host_boot_identity_unsupported_platform_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "win32")
    assert process_identity.host_boot_identity() is None


def test_ownership_consumers_reject_reused_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    record = ui_cli.UIRecord(
        pid=123,
        process_birth="birth-a",
        host="127.0.0.1",
        port=8765,
        started_at="now",
        mode="background",
    )
    monkeypatch.setattr(ui_cli, "process_birth_identity", lambda pid: "birth-b")
    assert ui_cli._record_state(record) == "mismatched"

    monkeypatch.setattr(
        persistent_units, "process_birth_identity", lambda pid: "birth-b"
    )
    assert not persistent_units._process_alive(123, "birth-a")

    monkeypatch.setattr(run_activity, "_host_boot", lambda: "boot-a")
    monkeypatch.setattr(run_activity, "process_birth_identity", lambda pid: "birth-b")
    assert run_activity.preparation_active(
        {"schema_version": 1, "pid": 123, "birth": "birth-a", "host_boot": "boot-a"}
    ) is False


# -- session topology helpers -------------------------------------------------


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX session APIs")
def test_session_members_captures_separate_group_descendants(tmp_path: Path) -> None:
    """A session with a separate-group child is fully observed with births."""
    child = tmp_path / "child.py"
    child.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    leader_script = tmp_path / "leader.py"
    leader_script.write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}], process_group=0).wait()\n",
        encoding="utf-8",
    )
    leader = subprocess.Popen(
        [sys.executable, str(leader_script)],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # Wait for the child to appear in the session as a separate group.
        import time as _time

        deadline = _time.monotonic() + 10
        members = []
        while _time.monotonic() < deadline:
            members = process_identity.session_members(leader.pid) or []
            if len(members) >= 2 and len({m.pgid for m in members}) >= 2:
                break
            _time.sleep(0.05)
        assert members, "no session members observed"
        by_pid = {m.pid: m for m in members}
        assert leader.pid in by_pid
        child_pid = next(pid for pid in by_pid if pid != leader.pid)
        # The child leads its own group within the leader's session.
        assert by_pid[leader.pid].pgid == leader.pid
        assert by_pid[child_pid].pgid == child_pid
        assert by_pid[leader.pid].birth is not None
        assert by_pid[child_pid].birth is not None
    finally:
        leader.kill()
        leader.wait()


def test_session_member_live_rejects_reused_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A captured member whose PID was reused (birth changed) is never live."""
    member = process_identity.SessionMember(pid=123, birth="linux-start-ticks:100", pgid=123)
    monkeypatch.setattr(process_identity.os, "getsid", lambda pid: 123)
    monkeypatch.setattr(process_identity.os, "getpgid", lambda pid: 123)
    # Reused PID: the live birth identity differs from the captured one.
    monkeypatch.setattr(process_identity, "process_birth_identity", lambda pid: "linux-start-ticks:999")
    assert process_identity.session_member_live(member, 123) is False
    # Matching birth => live.
    monkeypatch.setattr(process_identity, "process_birth_identity", lambda pid: "linux-start-ticks:100")
    assert process_identity.session_member_live(member, 123) is True
    # An unproven (zombie) birth is never live.
    zombie = process_identity.SessionMember(pid=123, birth=None, pgid=123)
    assert process_identity.session_member_live(zombie, 123) is False


def test_session_member_live_rejects_wrong_session_group_or_dead_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = process_identity.SessionMember(pid=123, birth="b", pgid=123)
    # Wrong session.
    monkeypatch.setattr(process_identity.os, "getsid", lambda pid: 999)
    monkeypatch.setattr(process_identity.os, "getpgid", lambda pid: 123)
    assert process_identity.session_member_live(member, 123) is False
    # Wrong group.
    monkeypatch.setattr(process_identity.os, "getsid", lambda pid: 123)
    monkeypatch.setattr(process_identity.os, "getpgid", lambda pid: 999)
    assert process_identity.session_member_live(member, 123) is False
    # A dead PID (getpgid raises) is not live.
    def gone(pid):
        raise ProcessLookupError("gone")

    monkeypatch.setattr(process_identity.os, "getpgid", gone)
    assert process_identity.session_member_live(member, 123) is False


def test_process_group_state_reports_present_absent_unknown() -> None:
    assert process_identity.process_group_state(None) == "unknown"
    assert process_identity.process_group_state(0) == "unknown"
    assert process_identity.process_group_state(-5) == "unknown"
    if sys.platform not in ("linux", "darwin"):
        pytest.skip("group state is POSIX-only")
    leader = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert process_identity.process_group_state(leader.pid) == "present"
    finally:
        leader.kill()
        leader.wait()
    assert process_identity.process_group_state(leader.pid) == "absent"


def _publish_marker_atomic(script_lines: list[str], ready: Path, tmp: Path) -> None:
    """Append the provider's atomic readiness publication lines.

    The complete PID is written to a task-owned sibling temporary file and
    then ``os.replace``d onto the readiness path, so the marker can never be
    observed half-written: existence implies complete content.
    """
    script_lines.append("import os, time\n")
    script_lines.append("from pathlib import Path\n")
    script_lines.append(f"Path({str(tmp)!r}).write_text(str(os.getpid()))\n")
    script_lines.append(f"os.replace({str(tmp)!r}, {str(ready)!r})\n")


@dataclass
class _FixtureProof:
    """Ownership proof captured inside the protected fixture lifetime."""

    wrapper_birth: str | None
    wrapper_pgid: int
    session_id: int | None
    provider: process_identity.SessionMember | None = None


def _capture_wrapper_proof(wrapper: subprocess.Popen[bytes]) -> _FixtureProof:
    """Record the live direct wrapper's birth, fixture PGID, and SID."""
    try:
        session_id = os.getsid(wrapper.pid)
    except (OSError, ValueError):
        session_id = None
    return _FixtureProof(
        wrapper_birth=process_identity.process_birth_identity(wrapper.pid),
        wrapper_pgid=wrapper.pid,
        session_id=session_id,
    )


def _wait_for_provider_ready(
    ready: Path,
    wrapper: subprocess.Popen[bytes],
    proof: _FixtureProof,
    *,
    deadline_seconds: float = 10.0,
) -> int:
    """Bounded wait for a provider-owned, atomically published readiness PID.

    The marker is published by the provider itself only after its own
    initialization (sibling temp write + atomic ``os.replace``), so a
    wrapper-created PID can never race handler installation and the marker
    can never be observed half-written.  An absent, empty, or non-numeric
    marker is treated as not ready until the monotonic deadline; marker
    existence alone never authorizes teardown.  A complete PID is readiness
    only when it leads the fixture's recorded group and revalidates its
    observed birth, SID, and PGID against the recorded session; a live
    same-session process in another group, an unobserved birth, or a failed
    revalidation is rejected before any member is retained, so a marker PID
    alone is never identity proof.  Retained proof precedes the remaining
    readiness assertions, so cleanup ownership is independent of them.
    """
    deadline = time.monotonic() + deadline_seconds
    content = ""
    provider_pid: int | None = None
    while time.monotonic() < deadline:
        try:
            content = ready.read_text()
        except OSError:
            content = ""
        else:
            try:
                provider_pid = int(content)
            except ValueError:
                provider_pid = None
            if provider_pid is not None and provider_pid >= 1:
                break
        time.sleep(0.02)
    if provider_pid is None:
        assert False, (
            "provider readiness marker missing or incomplete "
            f"(observed content: {content!r}; "
            f"wrapper status: {wrapper.poll()})"
        )
    # Retain positively validated provider proof before any assertion that
    # could reject wrapper/provider readiness; an unproven member is not
    # retained, so a marker PID alone is never identity proof.  The marker
    # PID must lead the fixture's recorded group: a live same-session
    # process in another group is not the provider, and an unobserved birth
    # or a failed SID/birth/PGID revalidation against the recorded session
    # is never readiness.
    provider_pgid: int | None = None
    try:
        provider_pgid = os.getpgid(provider_pid)
    except (OSError, ValueError):
        provider_pgid = None
    provider_birth: str | None = None
    if provider_pgid is not None and provider_pgid == proof.wrapper_pgid:
        provider_birth = process_identity.process_birth_identity(provider_pid)
    member: process_identity.SessionMember | None = None
    if (
        provider_pgid is not None
        and provider_pgid == proof.wrapper_pgid
        and provider_birth is not None
        and proof.session_id is not None
    ):
        candidate = process_identity.SessionMember(
            pid=provider_pid, birth=provider_birth, pgid=provider_pgid
        )
        if process_identity.session_member_live(candidate, proof.session_id):
            member = candidate
    proof.provider = member
    assert member is not None, (
        "provider readiness PID "
        f"{provider_pid} is not a live member of the recorded fixture group "
        f"(observed pgid: {provider_pgid}, "
        f"expected: {proof.wrapper_pgid}, "
        f"observed birth: {provider_birth!r}, "
        f"session: {proof.session_id})"
    )
    assert wrapper.poll() is None, "wrapper exited before provider readiness"
    assert (
        process_identity.process_liveness(provider_pid) == "present"
    ), "provider was not live at readiness"
    return provider_pid


_CLEANUP_BUDGET_SECONDS = 2.0


def _cleanup_owned_fixture(
    wrapper: subprocess.Popen[bytes] | "_ExitedWrapper",
    proof: _FixtureProof | None,
) -> bool:
    """Bounded, ownership-scoped fixture cleanup.

    While the live direct wrapper still owns its recorded group, a fresh
    bounded ``controller_group_owned`` proof (captured wrapper birth/PGID/SID)
    immediately before the signal authorizes KILL of the exact fixture group,
    which also ceases a provider whose readiness PID was never published.  A
    retained provider is signalled only when ``session_member_live``
    revalidates its captured birth/PGID/SID immediately before that signal;
    an absent, stale, expired, reused, or unknown proof authorizes no
    group/provider signal.  The direct child is reaped with a bounded
    ``wait(timeout=...)``; success additionally requires positive cessation
    of the recorded fixture group and any retained provider, observed with
    ``process_group_state`` and ``process_liveness`` inside the same
    two-second budget.  When readiness retained no PID the group's positive
    absence is observed, never assumed; a present, unknown, or expired
    cessation observation returns False and reports unconfirmed cleanup.
    On unconfirmed cleanup the evidence is reported and the caller retains
    the original failure.
    """
    import signal as _signal

    deadline = time.monotonic() + _CLEANUP_BUDGET_SECONDS
    if proof is not None and wrapper.poll() is None and proof.wrapper_birth:
        if process_identity.controller_group_owned(
            wrapper.pid, proof.wrapper_birth, proof.session_id, deadline
        ):
            try:
                os.kill(-wrapper.pid, _signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    if proof is not None and proof.provider is not None:
        member = proof.provider
        if process_identity.process_liveness(member.pid, deadline) != "absent":
            if process_identity.session_member_live(
                member, proof.session_id, deadline
            ):
                try:
                    os.kill(member.pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    if wrapper.poll() is None:
        try:
            wrapper.kill()
        except (OSError, ValueError):
            pass
    try:
        # The reap spends only the remaining shared cleanup budget: a slow
        # identity probe above must never renew the allowance.
        wrapper.wait(timeout=max(0.0, deadline - time.monotonic()))
        reaped = wrapper.poll() is not None
    except subprocess.TimeoutExpired:
        reaped = False
    # Positive cessation of the recorded fixture group and any retained
    # provider is required before success can be claimed.  When readiness
    # retained no PID the group's positive absence is observed inside the
    # same deadline rather than assumed; a present, unknown, or expired
    # observation is unconfirmed cleanup, never a default success.
    group_confirmed = proof is None
    provider_confirmed = True
    if proof is not None:
        retained = proof.provider
        group_state = "present"
        provider_state = "absent"
        while time.monotonic() < deadline:
            group_state = process_identity.process_group_state(
                proof.wrapper_pgid, deadline
            )
            provider_state = (
                process_identity.process_liveness(retained.pid, deadline)
                if retained is not None
                else "absent"
            )
            if group_state == "absent" and provider_state == "absent":
                break
            time.sleep(0.02)
        group_confirmed = group_state == "absent"
        provider_confirmed = provider_state == "absent"
    confirmed = reaped and provider_confirmed and group_confirmed
    if not confirmed:
        # Report the evidence and let the caller retain the original failure
        # rather than masking it or claiming no leak.
        print(
            "fixture cleanup unconfirmed: wrapper reaped="
            f"{reaped}, fixture group positively absent={group_confirmed}, "
            f"provider positively ceased={provider_confirmed}",
            file=sys.stderr,
        )
    return confirmed


class _ExitedWrapper:
    """A duck-typed already-reaped wrapper for cleanup contract tests."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.kill_calls = 0

    def poll(self) -> int:
        return 0

    def kill(self) -> None:
        self.kill_calls += 1

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX group teardown")
def test_terminate_owned_group_escalates_proven_term_survivor(tmp_path: Path) -> None:
    """A proven owned TERM survivor is escalated to KILL and the group ends."""
    # Case 1: an initialized TERM-responsive provider plus a waiting wrapper;
    # TERM ceases the whole owned group.
    provider1 = tmp_path / "p1.py"
    ready1 = tmp_path / "p1.ready"
    lines1: list[str] = []
    _publish_marker_atomic(lines1, ready1, tmp_path / "p1.ready.tmp")
    lines1.append("time.sleep(30)\n")
    provider1.write_text("".join(lines1), encoding="utf-8")
    wrapper1 = tmp_path / "w1.py"
    wrapper1.write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, {str(provider1)!r}]).wait()\n",
        encoding="utf-8",
    )
    wrapper = subprocess.Popen(
        [sys.executable, str(wrapper1)],
        process_group=0,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proof = _capture_wrapper_proof(wrapper)
    provider1_pid: int | None = None
    try:
        provider1_pid = _wait_for_provider_ready(ready1, wrapper, proof)
        assert process_identity.terminate_owned_group(
            wrapper, wrapper.pid, grace_seconds=5.0, kill_seconds=1.0
        ) is True
        assert wrapper.poll() is not None
        assert process_identity.process_liveness(provider1_pid) == "absent"
    finally:
        _cleanup_owned_fixture(wrapper, proof)

    # Case 2: an initialized TERM-ignoring provider survives the wrapper's
    # TERM exit and is escalated to KILL through proven ownership.  The
    # provider installs its TERM handler before writing its own readiness
    # marker, and a child-owned TERM-observation marker proves the handler ran
    # before the proven survivor's KILL cessation.
    provider2 = tmp_path / "p2.py"
    ready2 = tmp_path / "p2.ready"
    seen2 = tmp_path / "p2.seen"
    # The provider installs its TERM-observation handler before its atomic
    # readiness publication.
    lines2: list[str] = [
        "import signal\n",
        "def _term(s, f):\n",
        f"    Path({str(seen2)!r}).touch()\n",
        "    time.sleep(30)\n",
        "signal.signal(signal.SIGTERM, _term)\n",
    ]
    _publish_marker_atomic(lines2, ready2, tmp_path / "p2.ready.tmp")
    lines2.append("time.sleep(30)\n")
    provider2.write_text("".join(lines2), encoding="utf-8")
    wrapper2_script = tmp_path / "w2.py"
    wrapper2_script.write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, {str(provider2)!r}]).wait()\n",
        encoding="utf-8",
    )
    wrapper2 = subprocess.Popen(
        [sys.executable, str(wrapper2_script)],
        process_group=0,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proof2 = _capture_wrapper_proof(wrapper2)
    provider2_pid: int | None = None
    try:
        provider2_pid = _wait_for_provider_ready(ready2, wrapper2, proof2)
        assert process_identity.terminate_owned_group(
            wrapper2, wrapper2.pid, grace_seconds=1.0, kill_seconds=1.0
        ) is True
        assert wrapper2.poll() is not None
        # The ignore handler observed TERM before the proven survivor ceased.
        assert seen2.exists(), "TERM-observation marker missing"
        assert process_identity.process_liveness(provider2_pid) == "absent"
    finally:
        _cleanup_owned_fixture(wrapper2, proof2)


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX group teardown")
def test_provider_readiness_waits_for_complete_pid_marker(tmp_path: Path) -> None:
    """An incomplete readiness marker causes a bounded readiness failure, never
    an immediate parse error, and leaves no live owned process behind."""
    ready = tmp_path / "p.ready"
    provider = tmp_path / "p.py"
    # The provider starts but never completes the publication; the ready path
    # is pre-created empty to reproduce the write boundary the old helper
    # parsed immediately after an exists() check.
    provider.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    wrapper_script = tmp_path / "w.py"
    wrapper_script.write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, {str(provider)!r}]).wait()\n",
        encoding="utf-8",
    )
    wrapper = subprocess.Popen(
        [sys.executable, str(wrapper_script)],
        process_group=0,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proof = _capture_wrapper_proof(wrapper)
    ready.write_text("")
    try:
        start = time.monotonic()
        with pytest.raises(AssertionError, match="readiness marker"):
            _wait_for_provider_ready(ready, wrapper, proof, deadline_seconds=0.5)
        elapsed = time.monotonic() - start
        # The empty marker was polled until the deadline: a bounded readiness
        # failure, not an immediate int() parse error.
        assert elapsed >= 0.4
        # An incomplete marker retains no provider identity proof.
        assert proof.provider is None
    finally:
        confirmed = _cleanup_owned_fixture(wrapper, proof)
    assert confirmed
    assert wrapper.poll() is not None
    assert process_identity.process_group_state(wrapper.pid) == "absent"


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX group teardown")
def test_owned_fixture_cleanup_after_readiness_failure(tmp_path: Path) -> None:
    """A readiness failure after a real provider starts leaves both owned
    processes ceased and the wrapper reaped, without the provider's readiness
    PID ever being published."""
    import time as _time

    provider_pid_file = tmp_path / "p.pid"
    ready = tmp_path / "p.ready"
    provider = tmp_path / "p.py"
    # The provider atomically publishes its own PID on an independent
    # side channel (sibling temp + os.replace); the final readiness marker
    # is deliberately never written, so the helper receives no readiness or
    # provider proof.
    side_channel: list[str] = []
    _publish_marker_atomic(side_channel, provider_pid_file, tmp_path / "p.pid.tmp")
    side_channel.append("time.sleep(30)\n")
    provider.write_text("".join(side_channel), encoding="utf-8")
    wrapper_script = tmp_path / "w.py"
    wrapper_script.write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, {str(provider)!r}]).wait()\n",
        encoding="utf-8",
    )
    wrapper = subprocess.Popen(
        [sys.executable, str(wrapper_script)],
        process_group=0,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proof = _capture_wrapper_proof(wrapper)
    try:
        # The real provider starts (its own side-channel PID file proves it),
        # but the readiness marker is never published.  The bounded startup
        # reader accepts only complete valid content and tolerates an
        # incomplete (open-before-write) publication until the deadline:
        # no immediate parse error.
        deadline = _time.monotonic() + 10
        provider_pid: int | None = None
        while _time.monotonic() < deadline:
            try:
                content = provider_pid_file.read_text()
            except OSError:
                content = ""
            else:
                try:
                    provider_pid = int(content)
                except ValueError:
                    provider_pid = None
                if provider_pid is not None and provider_pid >= 1:
                    break
            _time.sleep(0.02)
        assert provider_pid is not None, "provider never started"
        assert process_identity.process_liveness(provider_pid) == "present"
        with pytest.raises(AssertionError, match="readiness marker"):
            _wait_for_provider_ready(ready, wrapper, proof, deadline_seconds=0.5)
    finally:
        confirmed = _cleanup_owned_fixture(wrapper, proof)
    assert confirmed
    assert wrapper.poll() is not None
    assert process_identity.process_liveness(provider_pid) == "absent"
    assert process_identity.process_group_state(wrapper.pid) == "absent"


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX group teardown")
def _wait_for_survivor_group_present(
    survivor: subprocess.Popen[bytes],
    deadline_seconds: float = 10.0,
) -> None:
    """Bounded positive observation that the task-owned survivor leads a
    present group.

    The existing ten-second deadline bounds the loop in addition to the
    group-state condition: once it expires, ``process_group_state`` reports
    ``unknown`` and the observation fails with a clear assertion instead of
    spinning while the child remains live.  Present, absent, and unknown
    observations keep their meanings throughout.
    """
    deadline = time.monotonic() + deadline_seconds
    state = process_identity.process_group_state(survivor.pid, deadline)
    while state != "present" and time.monotonic() < deadline:
        if survivor.poll() is not None:
            break
        time.sleep(0.02)
        state = process_identity.process_group_state(survivor.pid, deadline)
    assert state == "present", (
        "survivor group did not become present before the startup deadline "
        f"(last state: {state}, survivor status: {survivor.poll()})"
    )


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX subprocess cleanup")
def test_fixture_signal_interceptor_leaves_subprocess_probe_timeout_cleanup_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the fixture signal interceptor is installed at this module's own
    ``os`` reference, an independently owned child that exceeds a bounded
    ``subprocess.run`` timeout is cleaned up by Python through the *real*
    ``os.kill``.  The interceptor must not observe that cleanup: the ordinary
    ``TimeoutExpired`` outcome is preserved, the owned child is reaped by
    ``subprocess.run``, and no fixture signal is recorded.

    A process-global ``os.kill`` patch (the old approach) would intercept the
    timeout cleanup and raise the fixture's unauthorized-signal assertion
    instead of the ordinary ``TimeoutExpired``; this regression fails under
    that coupling and passes only while the interceptor is isolated.
    """
    signals: list[tuple[int, int]] = []

    def intercept(pid: int, signum: int) -> None:
        signals.append((pid, signum))
        if signum == 0:
            raise ProcessLookupError("no such process")
        raise AssertionError(f"unauthorized signal {signum} to pid {pid}")

    _install_fixture_signal_interceptor(monkeypatch, intercept)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        # A temporary short-lived Python child that deliberately outlives the
        # bounded timeout; ``subprocess.run`` kills and reaps it on expiry.
        subprocess.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=0.3,
        )
    elapsed = time.monotonic() - started
    # The ordinary bounded timeout outcome, not the fixture's fatal assertion.
    assert elapsed < _CLEANUP_BUDGET_SECONDS
    # The interceptor is isolated: Python's own timeout cleanup used the real
    # ``os.kill``, so no fixture signal (null probe or otherwise) was recorded.
    assert signals == []


@pytest.mark.parametrize(
    ("group_obs", "provider_obs", "expected"),
    [
        ("absent", "absent", True),
        ("present", "present", False),
        ("present", "absent", False),
        ("absent", "present", False),
        ("unknown", "absent", False),
        ("absent", "unknown", False),
        ("unknown", "unknown", False),
    ],
    ids=[
        "positive-absence",
        "live-group-and-provider",
        "live-group",
        "live-provider",
        "unknown-group",
        "unknown-provider",
        "unknown-both",
    ],
)
def test_owned_fixture_cleanup_rejects_unproven_provider(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    group_obs: str,
    provider_obs: str,
    expected: bool,
) -> None:
    """Deterministic cleanup-decision checks at the process observation
    boundaries.

    Positive cessation requires *both* the fixture group and the retained
    provider to be positively absent; a present, unknown, or expired
    observation of either is unconfirmed cleanup, never success.  Rejected
    (live, reused, unknown, expired, or wrong-group/session) provider evidence
    authorizes no group/provider signal, and a rejected nonzero fixture signal
    remains an assertion failure.  The interceptor is isolated at this module's
    own ``os`` reference, and the observation boundaries are supplied
    deterministically so a synthetic assertion test never spends the cleanup
    budget on a live ``ps`` loop.  Real process/group integration (readiness,
    escalation, survivor cleanup) is proven by the separate live-survivor
    tests, which this decision test supplements rather than replaces; mocked
    absence here does not assert native process behavior.
    """
    member_pid = 2_000_000_000  # above any realistic pids_max: never a live target
    member = process_identity.SessionMember(
        pid=member_pid, birth="linux-start-ticks:1", pgid=member_pid
    )
    signals: list[tuple[int, int]] = []

    def intercept(pid: int, signum: int) -> None:
        signals.append((pid, signum))
        if signum == 0:
            # A null probe is not a signal.
            raise ProcessLookupError("no such process")
        # A rejected fixture signal remains an assertion failure.
        raise AssertionError(f"unauthorized signal {signum} to pid {pid}")

    # Isolate the interceptor at this module's own ``os`` reference: production
    # process probes and independently owned subprocess cleanup keep the real
    # ``os.kill``.
    _install_fixture_signal_interceptor(monkeypatch, intercept)
    # Deterministic observation boundaries: no live ``ps`` loop in a synthetic
    # assertion test.
    monkeypatch.setattr(
        process_identity, "process_group_state", lambda pgid, deadline=None: group_obs
    )
    monkeypatch.setattr(
        process_identity, "process_liveness", lambda pid, deadline=None: provider_obs
    )

    wrapper = _ExitedWrapper(member_pid)
    proof = _FixtureProof(
        wrapper_birth="linux-start-ticks:1",
        wrapper_pgid=member_pid,
        session_id=member_pid,
        provider=member,
    )
    confirmed = _cleanup_owned_fixture(wrapper, proof)

    # No authorized fixture signal: rejected evidence is never signalled, and
    # the reaped wrapper authorizes no group signal.
    assert [call for call in signals if call[1] != 0] == []
    assert wrapper.kill_calls == 0
    assert confirmed is expected
    err = capsys.readouterr().err
    if expected:
        # Positive absence of both group and provider: no unconfirmed report.
        assert "fixture cleanup unconfirmed" not in err
    else:
        # Unconfirmed cleanup preserves its diagnostic rather than masking it.
        assert "fixture cleanup unconfirmed" in err


@pytest.mark.parametrize(
    "reject_case",
    [
        "unknown-birth",
        "reused-birth",
        "expired-birth",
        "wrong-group",
        "wrong-session",
    ],
)
def test_owned_fixture_cleanup_rejects_unproven_provider_evidence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reject_case: str,
) -> None:
    """Deterministic rejected-proof decisions through real validation.

    An unknown captured provider birth, a reused PID birth, an expired
    bounded birth, a mismatched recorded group, or a mismatched session is
    never live provider proof: the real ``_cleanup_owned_fixture`` and
    ``process_identity.session_member_live`` reject each case at its
    intended ownership gate, authorize no fixture signal and no wrapper
    kill, and return unconfirmed cleanup with its diagnostic.  Present
    group/provider observations keep any absence shortcut from replacing
    ownership rejection, and the deterministic native boundaries (provider
    liveness, getsid/getpgid, bounded birth) plus the already-reaped
    synthetic wrapper and virtual clock spend no real time and no live
    probe in this decision test.  Real process/group integration remains
    proven by the live-survivor tests, which this test supplements.
    """
    member_pid = 2_000_000_000  # above any realistic pids_max: never a live target
    session_id = 4242
    if reject_case == "unknown-birth":
        # An unproven (never-observed) birth is never live evidence.
        member = process_identity.SessionMember(
            pid=member_pid, birth=None, pgid=member_pid
        )
    else:
        member = process_identity.SessionMember(
            pid=member_pid, birth="linux-start-ticks:1", pgid=member_pid
        )

    signals: list[tuple[int, int]] = []
    boundary_calls: list[str] = []

    def intercept(pid: int, signum: int) -> None:
        signals.append((pid, signum))
        # A rejected nonzero fixture signal remains an assertion failure.
        raise AssertionError(f"unauthorized signal {signum} to pid {pid}")

    _install_fixture_signal_interceptor(monkeypatch, intercept)
    # Deterministic observation boundaries: present group and provider
    # observations keep the cessation loop from taking an absence shortcut
    # in place of ownership rejection.
    monkeypatch.setattr(
        process_identity, "process_group_state", lambda pgid, deadline=None: "present"
    )
    monkeypatch.setattr(
        process_identity, "process_liveness", lambda pid, deadline=None: "present"
    )
    # Deterministic native identity boundaries for the synthetic target.
    def fake_getsid(pid: int) -> int:
        boundary_calls.append("getsid")
        return session_id + 1 if reject_case == "wrong-session" else session_id

    def fake_getpgid(pid: int) -> int:
        boundary_calls.append("getpgid")
        return member_pid + 7 if reject_case == "wrong-group" else member_pid

    def fake_birth_bounded(pid: int, deadline: float) -> str | None:
        boundary_calls.append("birth")
        if reject_case == "reused-birth":
            return "linux-start-ticks:999"
        if reject_case == "expired-birth":
            return None
        return "linux-start-ticks:1"

    monkeypatch.setattr(process_identity.os, "getsid", fake_getsid)
    monkeypatch.setattr(process_identity.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(process_identity, "process_birth_bounded", fake_birth_bounded)
    # A deterministic accelerated clock bounds the synthetic cessation loop
    # on virtual time without changing the helper's two-second allowance.
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    wrapper = _ExitedWrapper(member_pid)
    proof = _FixtureProof(
        wrapper_birth="linux-start-ticks:1",
        wrapper_pgid=member_pid,
        session_id=session_id,
        provider=member,
    )
    confirmed = _cleanup_owned_fixture(wrapper, proof)

    # Rejected proof authorizes no fixture signal and no wrapper kill.
    assert signals == []
    assert wrapper.kill_calls == 0
    assert confirmed is False
    # Unconfirmed cleanup preserves its diagnostic rather than masking it.
    assert "fixture cleanup unconfirmed" in capsys.readouterr().err
    # Each case reached its intended ownership gate, so the matrix cannot
    # collapse to a single getsid lookup failure for the synthetic target.
    if reject_case == "unknown-birth":
        assert boundary_calls == []
    elif reject_case == "wrong-session":
        assert boundary_calls == ["getsid"]
    elif reject_case == "wrong-group":
        assert boundary_calls == ["getsid", "getpgid"]
    else:  # reused-birth and expired-birth
        assert boundary_calls == ["getsid", "getpgid", "birth"]


@dataclass
class _WrongGroupOutcome:
    """Observed outcome of one wrong-group readiness fixture run."""

    wrapper: subprocess.Popen[bytes]
    proof: _FixtureProof
    decoy_pid: int | None
    signals: list[tuple[int, int]]
    cleanup_confirmed: bool
    body_failure: AssertionError | None


def _run_wrong_group_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    wrapper_birth_known: bool = True,
    decoy_birth_known: bool = True,
) -> _WrongGroupOutcome:
    """Drive the actual wrong-group readiness fixture end to end.

    A wrapper's child moves itself into a new group of the same session and
    atomically publishes that (wrong-group) PID as the readiness marker.  The
    readiness proof's provider stays unset on rejection and is never reused
    for teardown; a separate teardown proof carries the independently
    observed descendant evidence, so only a fresh positive
    ``controller_group_owned`` authorizes the fixture-group signal and only a
    fresh positive ``session_member_live`` authorizes the descendant signal.
    ``wrapper_birth_known=False`` drops the captured wrapper birth so no
    fixture-group signal may be authorized; ``decoy_birth_known=False`` makes
    the decoy birth observation unknown, so the original setup assertion
    stays the primary failure and unconfirmed cleanup is reported, never
    asserted away.
    """
    ready = tmp_path / "p.ready"
    decoy_script = tmp_path / "decoy.py"
    decoy_lines: list[str] = ["import os\n", "os.setpgid(0, 0)\n"]
    _publish_marker_atomic(decoy_lines, ready, tmp_path / "p.ready.tmp")
    decoy_lines.append("time.sleep(30)\n")
    decoy_script.write_text("".join(decoy_lines), encoding="utf-8")
    wrapper_script = tmp_path / "w.py"
    wrapper_script.write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, {str(decoy_script)!r}]).wait()\n",
        encoding="utf-8",
    )
    wrapper = subprocess.Popen(
        [sys.executable, str(wrapper_script)],
        process_group=0,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proof = _capture_wrapper_proof(wrapper)
    if not wrapper_birth_known:
        proof.wrapper_birth = None
    decoy_pids: set[int] = set()
    if not decoy_birth_known:
        real_birth = process_identity.process_birth_identity

        def fake_birth(pid: int) -> str | None:
            return None if pid in decoy_pids else real_birth(pid)

        monkeypatch.setattr(process_identity, "process_birth_identity", fake_birth)
    signals: list[tuple[int, int]] = []

    def intercept(pid: int, signum: int) -> None:
        signals.append((pid, signum))
        if signum == 0:
            raise ProcessLookupError("no such process")
        # Swallowed: the fixture group stays live so the decoy's survival
        # is observable and no real signal can reach any process.

    # Isolate the interceptor at this module's own ``os`` reference so the
    # real wrapper/decoy subprocesses' own timeout cleanup keeps the real
    # ``os.kill`` while only the fixture's attempted signals are recorded.
    _install_fixture_signal_interceptor(monkeypatch, intercept)
    # The fixture's direct wrapper kill keeps the old recorded/suppressed
    # behavior: route only this created wrapper instance's ``kill`` method
    # through the interceptor, so the wrapper's exact PID and SIGKILL are
    # observed and suppressed while the fixture group stays present until
    # teardown.  The Popen class and process-global ``os.kill`` are not
    # patched; the instance method is restored by the existing
    # ``monkeypatch.undo()`` before the real teardown.
    def _intercepted_wrapper_kill() -> None:
        intercept(wrapper.pid, signal.SIGKILL)

    monkeypatch.setattr(wrapper, "kill", _intercepted_wrapper_kill)
    decoy_pid: int | None = None
    decoy_member: process_identity.SessionMember | None = None
    body_failure: AssertionError | None = None
    cleanup_confirmed = False
    try:
        # Bounded startup read of the provider-owned atomic marker, using the
        # same pattern readiness itself uses: the complete decoy PID must be
        # retained before the expected-rejection assertion is entered.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                content = ready.read_text()
            except OSError:
                content = ""
            else:
                try:
                    decoy_pid = int(content)
                except ValueError:
                    decoy_pid = None
                if decoy_pid is not None and decoy_pid >= 1:
                    break
            time.sleep(0.02)
        assert decoy_pid is not None, "decoy readiness marker never published"
        decoy_pids.add(decoy_pid)
        # Independent positive task-owned decoy identity, revalidated before
        # the expected-rejection assertion.  Its own PGID differs from the
        # wrapper's fixture group but its SID is the recorded fixture
        # session.  This teardown evidence must never set the readiness
        # proof's provider, which stays unset on rejection.
        decoy_pgid = os.getpgid(decoy_pid)
        assert decoy_pgid != wrapper.pid, "decoy never left the fixture group"
        assert os.getsid(decoy_pid) == proof.session_id, (
            "decoy is not in the fixture session"
        )
        decoy_birth = process_identity.process_birth_identity(decoy_pid)
        assert decoy_birth is not None, "decoy birth was not observed"
        decoy_member = process_identity.SessionMember(
            pid=decoy_pid, birth=decoy_birth, pgid=decoy_pgid
        )
        assert process_identity.session_member_live(decoy_member, proof.session_id)
        with pytest.raises(AssertionError, match="not a live member"):
            _wait_for_provider_ready(ready, wrapper, proof)
        # The wrong-group PID was never retained as provider proof.
        assert proof.provider is None
        # The fixture group (live wrapper plus decoy) is present, so cleanup
        # must stay unconfirmed while the intercepted group signal never
        # landed.
        assert _cleanup_owned_fixture(wrapper, proof) is False
        # The decoy survived: it was never signalled as a provider.
        assert process_identity.process_liveness(decoy_pid) == "present"
    except AssertionError as exc:
        # The original body failure remains the primary exception; the
        # finalizer below must not replace it with a teardown assertion.
        body_failure = exc
    finally:
        # Restore signal interception before the real task-owned teardown.
        monkeypatch.undo()
        # Task-owned teardown of the fixture this run created through the
        # ownership-scoped helper.  The decoy leads its own group, so it is
        # ceased only through its own revalidated identity, independently of
        # the fixture group; a missing, unknown, or unrevalidated identity
        # authorizes no signal, and the original failure is preserved.
        teardown_proof = _FixtureProof(
            wrapper_birth=proof.wrapper_birth,
            wrapper_pgid=wrapper.pid,
            session_id=proof.session_id,
            provider=decoy_member,
        )
        cleanup_confirmed = _cleanup_owned_fixture(wrapper, teardown_proof)
        if decoy_member is None and decoy_pid is not None:
            # The marker PID is reporting evidence only, never signalling
            # identity: absence must be positively established.
            if process_identity.process_liveness(decoy_pid) != "absent":
                cleanup_confirmed = False
                print(
                    "fixture cleanup unconfirmed: decoy marker PID "
                    f"{decoy_pid} has no captured identity, so its "
                    "absence cannot be positively established",
                    file=sys.stderr,
                )
    return _WrongGroupOutcome(
        wrapper=wrapper,
        proof=proof,
        decoy_pid=decoy_pid,
        signals=signals,
        cleanup_confirmed=cleanup_confirmed,
        body_failure=body_failure,
    )


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX group teardown")
def test_provider_readiness_rejects_live_wrong_group_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete readiness PID of a live same-session process in another
    group is rejected: it is never retained as provider proof and never
    signalled; cleanup stays unconfirmed while the fixture group is present.
    The task-owned decoy's independent positive identity is captured and
    revalidated before the expected-rejection assertion, so a readiness that
    wrongly accepts it still leaves both processes ceased and reaped."""
    outcome = _run_wrong_group_rejection(tmp_path, monkeypatch)
    assert outcome.body_failure is None
    # The wrong-group PID was never retained as provider proof.
    assert outcome.proof.provider is None
    # Both task-owned processes (fixture group plus decoy) are positively
    # ceased and the direct child was reaped.
    assert outcome.cleanup_confirmed is True
    assert outcome.wrapper.poll() is not None
    assert outcome.decoy_pid is not None
    assert process_identity.process_liveness(outcome.decoy_pid) == "absent"
    # Any attempted non-null signal went only to the proven-owned fixture
    # group or the direct task-owned wrapper, never to the decoy or any
    # other PID.
    wrapper_pid = outcome.wrapper.pid
    assert all(
        pid in (wrapper_pid, -wrapper_pid)
        for pid, signum in outcome.signals
        if signum != 0
    )
    assert outcome.decoy_pid not in {pid for pid, _ in outcome.signals}


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX group teardown")
def test_provider_readiness_rejects_live_wrong_group_pid_unknown_wrapper_birth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an unknown captured wrapper birth the wrong-group rejection is
    unchanged and no fixture-group signal is ever authorized: every group
    signal is intercepted and none is attempted, and the decoy is ceased
    only through its own revalidated identity."""
    outcome = _run_wrong_group_rejection(
        tmp_path, monkeypatch, wrapper_birth_known=False
    )
    assert outcome.body_failure is None
    assert outcome.proof.provider is None
    wrapper_pid = outcome.wrapper.pid
    # The unknown wrapper birth authorizes no fixture-group signal.
    assert all(pid != -wrapper_pid for pid, _ in outcome.signals)
    # Only the direct task-owned wrapper was signalled, never the decoy.
    assert all(pid == wrapper_pid for pid, signum in outcome.signals if signum != 0)
    assert outcome.cleanup_confirmed is True
    assert outcome.wrapper.poll() is not None
    assert outcome.decoy_pid is not None
    assert process_identity.process_liveness(outcome.decoy_pid) == "absent"
    assert outcome.decoy_pid not in {pid for pid, _ in outcome.signals}


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX group teardown")
def test_provider_readiness_rejects_live_wrong_group_pid_unknown_decoy_birth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With an unknown descendant birth, the original setup assertion stays
    the primary failure: no descendant or group signal is attempted without
    proof, unconfirmed cleanup is reported, and no teardown assertion
    replaces the originating error.  The post-driver lifetime is protected
    by a guard that revalidates the independently retained task-owned
    decoy identity, so any post-driver assertion failure still ceases the
    decoy and leaves the body assertion as the primary exception."""
    outcome = _run_wrong_group_rejection(tmp_path, monkeypatch, decoy_birth_known=False)
    # Independent positive task-owned decoy identity, retained after the
    # driver's unknown-birth interception is undone and before any
    # assertion that can fail.  Birth/PGID/SID are retained, never a raw
    # PID, and the rejected readiness proof is never altered to authorize
    # cleanup; a missing observation stays unproven.
    decoy_pid = outcome.decoy_pid
    decoy_member: process_identity.SessionMember | None = None
    if decoy_pid is not None and outcome.proof.session_id is not None:
        decoy_pgid = os.getpgid(decoy_pid)
        decoy_birth = process_identity.process_birth_identity(decoy_pid)
        if (
            decoy_pgid == decoy_pid
            and decoy_birth is not None
            and os.getsid(decoy_pid) == outcome.proof.session_id
        ):
            candidate = process_identity.SessionMember(
                pid=decoy_pid, birth=decoy_birth, pgid=decoy_pgid
            )
            if process_identity.session_member_live(
                candidate, outcome.proof.session_id
            ):
                decoy_member = candidate
    try:
        # The original birth assertion is the primary failure, preserved
        # verbatim.
        assert outcome.body_failure is not None
        assert "decoy birth was not observed" in str(outcome.body_failure)
        # No signal was attempted at all while interception was active:
        # without a captured birth the descendant is never signalled, and
        # the body never reached an in-body cleanup call.
        assert outcome.signals == []
        # The proven fixture group signal ceased the wrapper, but the
        # unproven decoy survived: unconfirmed cleanup is reported, not
        # asserted away.
        assert outcome.wrapper.poll() is not None
        assert decoy_pid is not None
        assert process_identity.process_liveness(decoy_pid) == "present"
        assert outcome.cleanup_confirmed is False
        assert "fixture cleanup unconfirmed" in capsys.readouterr().err
    finally:
        # The independent guard reuses the ownership-scoped cleanup
        # helper: a fresh session_member_live immediately precedes any
        # descendant signal, the reaped wrapper authorizes no group
        # signal, and no guard assertion replaces the body exception.
        if decoy_member is not None:
            _cleanup_owned_fixture(
                outcome.wrapper,
                _FixtureProof(
                    wrapper_birth=outcome.proof.wrapper_birth,
                    wrapper_pgid=outcome.proof.wrapper_pgid,
                    session_id=outcome.proof.session_id,
                    provider=decoy_member,
                ),
            )
        elif decoy_pid is not None:
            # The marker PID is reporting evidence only, never signalling
            # identity: absence must be positively established.
            if process_identity.process_liveness(decoy_pid) != "absent":
                print(
                    "fixture cleanup unconfirmed: decoy marker PID "
                    f"{decoy_pid} has no captured identity, so its "
                    "absence cannot be positively established",
                    file=sys.stderr,
                )
    # Success-only cessation assertions after the protected lifetime, so
    # an assertion in the body remains the primary exception.
    assert decoy_pid is not None
    assert process_identity.process_liveness(decoy_pid) == "absent"


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX group teardown")
def test_unknown_decoy_guard_protects_post_driver_assertion_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The unknown-decoy test's guard ceases the task-owned decoy even when
    a post-driver assertion receives the documented unknown-liveness
    result: the original setup failure stays the primary failure, the
    wrapper is reaped, and the decoy is positively absent.  The
    regression independently retains the decoy's identity before the
    observation is injected, and its own protecting guard revalidates and
    cleans the task-owned process if the repair fails; it never signals an
    unknown or reused identity."""
    real_driver = _run_wrong_group_rejection
    real_liveness = process_identity.process_liveness
    state: dict[str, object] = {}

    def observe_driver(*args: object, **kwargs: object) -> _WrongGroupOutcome:
        outcome = real_driver(*args, **kwargs)
        state["outcome"] = outcome
        pid = outcome.decoy_pid
        assert pid is not None
        # The regression's own task-owned identity, retained and
        # revalidated before the unknown observation is injected; it is
        # guard evidence only, never readiness proof.
        birth = process_identity.process_birth_identity(pid)
        pgid = os.getpgid(pid)
        sid = os.getsid(pid)
        assert birth is not None
        assert pgid == pid
        assert sid == outcome.proof.session_id
        member = process_identity.SessionMember(pid=pid, birth=birth, pgid=pgid)
        assert process_identity.session_member_live(member, sid)
        assert real_liveness(pid) == "present"
        state["member"] = member
        state["sid"] = sid

        def one_unknown(target: int, deadline: float | None = None) -> str:
            # The documented unknown-liveness result, injected exactly
            # once at the test's first post-driver liveness assertion.
            if target == pid and not state.get("injected"):
                state["injected"] = True
                return "unknown"
            return real_liveness(target, deadline)

        monkeypatch.setattr(process_identity, "process_liveness", one_unknown)
        return outcome

    monkeypatch.setattr(
        sys.modules[__name__], "_run_wrong_group_rejection", observe_driver
    )
    try:
        inner = pytest.MonkeyPatch()
        try:
            with pytest.raises(AssertionError) as excinfo:
                test_provider_readiness_rejects_live_wrong_group_pid_unknown_decoy_birth(
                    tmp_path, inner, capsys
                )
        finally:
            inner.undo()
        outcome = state["outcome"]
        # The injected unknown observation deterministically failed the
        # post-driver liveness assertion, and that assertion propagated as
        # the primary exception: the guard ran in the protected lifetime
        # without replacing it.
        assert state.get("injected") is True
        assert "unknown" in str(excinfo.value)
        # The original setup failure is preserved verbatim.
        assert outcome.body_failure is not None
        assert "decoy birth was not observed" in str(outcome.body_failure)
        # The guard cleaned up: the wrapper is reaped and the decoy is
        # positively absent.
        assert outcome.wrapper.poll() is not None
        assert outcome.decoy_pid is not None
        assert real_liveness(outcome.decoy_pid) == "absent"
    finally:
        # The regression's own protecting guard: if the repair failed the
        # task-owned decoy is still live, so revalidate its captured
        # identity and cease only that proven process.
        member = state.get("member")
        if member is not None:
            sid = state["sid"]
            deadline = time.monotonic() + _CLEANUP_BUDGET_SECONDS
            if real_liveness(member.pid, deadline) != "absent":
                assert process_identity.session_member_live(member, sid, deadline)
                os.kill(member.pid, 9)
            while (
                real_liveness(member.pid, deadline) != "absent"
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            assert real_liveness(member.pid) == "absent"


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX group teardown")
def test_owned_fixture_cleanup_unknown_wrapper_birth_never_claims_ceased(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With missing readiness and an unknown wrapper birth, direct wrapper
    reaping cannot claim a surviving provider ceased: no group signal is
    authorized and cleanup reports unconfirmed."""
    import time as _time

    provider_pid_file = tmp_path / "p.pid"
    provider = tmp_path / "p.py"
    side_channel: list[str] = []
    _publish_marker_atomic(side_channel, provider_pid_file, tmp_path / "p.pid.tmp")
    side_channel.append("time.sleep(30)\n")
    provider.write_text("".join(side_channel), encoding="utf-8")
    wrapper_script = tmp_path / "w.py"
    wrapper_script.write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, {str(provider)!r}]).wait()\n",
        encoding="utf-8",
    )
    wrapper = subprocess.Popen(
        [sys.executable, str(wrapper_script)],
        process_group=0,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proof = _capture_wrapper_proof(wrapper)
    # Unknown wrapper birth: the fresh group-ownership re-proof cannot
    # succeed, so no group signal may be authorized.
    proof.wrapper_birth = None
    signals: list[tuple[int, int]] = []
    real_kill = os.kill

    def intercept(pid: int, signum: int) -> None:
        signals.append((pid, signum))
        if signum == 0:
            raise ProcessLookupError("no such process")
        if pid == wrapper.pid:
            # Only the direct task-owned wrapper's bounded reap is allowed.
            real_kill(wrapper.pid, signum)
            return
        raise AssertionError(f"unauthorized signal {signum} to pid {pid}")

    # Isolate the interceptor at this module's own ``os`` reference so the
    # real wrapper/provider subprocesses' own timeout cleanup keeps the real
    # ``os.kill`` while only the fixture's attempted signals are recorded.
    _install_fixture_signal_interceptor(monkeypatch, intercept)
    # Route only this wrapper instance's ``kill`` through the policy so the
    # allowed direct-wrapper reap stays recorded: the policy observes the
    # wrapper's exact PID and delivers the real kill, while unrelated
    # ``Popen`` instances keep the real ``os.kill``.
    def _intercepted_wrapper_kill() -> None:
        intercept(wrapper.pid, signal.SIGKILL)

    monkeypatch.setattr(wrapper, "kill", _intercepted_wrapper_kill)
    provider_pid: int | None = None
    provider_member: process_identity.SessionMember | None = None
    provider_sid: int | None = None
    try:
        deadline = _time.monotonic() + 10
        while _time.monotonic() < deadline:
            try:
                content = provider_pid_file.read_text()
            except OSError:
                content = ""
            else:
                try:
                    provider_pid = int(content)
                except ValueError:
                    provider_pid = None
                if provider_pid is not None and provider_pid >= 1:
                    break
            _time.sleep(0.02)
        assert provider_pid is not None, "provider never started"
        # Independent positive task-owned provider identity, revalidated
        # against the fixture session before the cleanup assertion.  This
        # teardown evidence must never become the readiness proof's
        # provider.
        provider_pgid = os.getpgid(provider_pid)
        provider_sid = os.getsid(provider_pid)
        provider_birth = process_identity.process_birth_identity(provider_pid)
        assert provider_pgid == wrapper.pid, "provider is not in the fixture group"
        assert provider_sid == proof.session_id, (
            "provider is not in the fixture session"
        )
        assert provider_birth is not None, "provider birth was not observed"
        provider_member = process_identity.SessionMember(
            pid=provider_pid, birth=provider_birth, pgid=provider_pgid
        )
        assert process_identity.session_member_live(provider_member, provider_sid)
        # The provider is a live member of the fixture group; readiness was
        # never published, so cleanup retains no provider proof.
        assert process_identity.process_liveness(provider_pid) == "present"
        confirmed = _cleanup_owned_fixture(wrapper, proof)
        # Reaping the direct wrapper alone never confirms cessation of the
        # surviving provider and its group.
        assert confirmed is False
        assert wrapper.poll() is not None
        assert process_identity.process_liveness(provider_pid) == "present"
    finally:
        monkeypatch.undo()
        # Task-owned teardown through the ownership-scoped helper: the
        # deliberately unknown wrapper birth authorizes no group signal, and
        # the independently observed provider identity is the only descendant
        # evidence.  A missing, unknown, or unrevalidated identity authorizes
        # no signal; the original failure is preserved.
        teardown_proof = _FixtureProof(
            wrapper_birth=None,
            wrapper_pgid=wrapper.pid,
            session_id=proof.session_id,
            provider=provider_member,
        )
        cleanup_confirmed = _cleanup_owned_fixture(wrapper, teardown_proof)
        if provider_member is None and provider_pid is not None:
            # The marker PID is reporting evidence only, never signalling
            # identity: absence must be positively established.
            if process_identity.process_liveness(provider_pid) != "absent":
                cleanup_confirmed = False
                print(
                    "fixture cleanup unconfirmed: provider marker PID "
                    f"{provider_pid} has no captured identity, so its "
                    "absence cannot be positively established",
                    file=sys.stderr,
                )
    # Reached only when the body succeeded: the task-owned fixture is
    # positively ceased and the direct child was reaped.
    assert cleanup_confirmed is True
    # Only the direct task-owned wrapper was signalled (its bounded reap);
    # the surviving provider was never signalled.
    assert all(pid == wrapper.pid for pid, signum in signals if signum != 0)
    assert provider_pid is not None
    assert provider_pid not in {pid for pid, _ in signals}
    # Bounded observation of the teardown cessation.
    deadline = _time.monotonic() + 5
    while (
        process_identity.process_liveness(provider_pid) != "absent"
        and _time.monotonic() < deadline
    ):
        _time.sleep(0.02)
    assert process_identity.process_liveness(provider_pid) == "absent"


def test_cleanup_owned_fixture_wait_uses_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow identity probe leaves only the remaining cleanup allowance for
    the direct child's bounded reap; an already-expired budget passes a zero
    allowance.  No renewed second budget is ever granted."""

    class _PendingWrapper:
        pid = 0

        def __init__(self) -> None:
            self.kill_calls = 0
            self.wait_timeouts: list[float] = []

        def poll(self) -> int | None:
            return None

        def kill(self) -> None:
            self.kill_calls += 1

        def wait(self, timeout: float | None = None) -> int:
            self.wait_timeouts.append(timeout)
            return 0

    for probe_seconds in (1.6, 2.5):
        # A deterministic accelerated clock: the fake probe spends virtual
        # time, so no real long sleep is needed.
        clock = [0.0]
        monkeypatch.setattr(time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(
            time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        )
        monkeypatch.setattr(
            process_identity,
            "controller_group_owned",
            lambda pid, birth, session, probe_deadline: (
                clock.__setitem__(0, clock[0] + probe_seconds),
                False,
            )[1],
        )
        wrapper = _PendingWrapper()
        proof = _FixtureProof(
            wrapper_birth="linux-start-ticks:1",
            wrapper_pgid=0,
            session_id=0,
        )
        _cleanup_owned_fixture(wrapper, proof)
        assert wrapper.wait_timeouts == [
            max(0.0, _CLEANUP_BUDGET_SECONDS - probe_seconds)
        ]


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX group teardown")
def test_owned_fixture_startup_unknown_observation_is_bounded_and_reaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistent unknown group observation fails at the bounded startup
    deadline, the original assertion failure is preserved, and the
    task-owned direct child is reaped."""
    # A deterministic accelerated clock: a persistent unknown observation
    # spins on virtual time only, never a real multi-second sleep.
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(
        process_identity,
        "process_group_state",
        lambda pgid, deadline=None: "unknown",
    )
    script = tmp_path / "survivor.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    survivor = subprocess.Popen(
        [sys.executable, str(script)],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with pytest.raises(AssertionError, match="startup deadline"):
            _wait_for_survivor_group_present(survivor)
        # Bounded completion: the virtual clock stopped at the deadline.
        assert clock[0] <= 10.0 + 0.02
    finally:
        # The same direct-child cleanup an early startup failure must reach:
        # restore interception, then cease and reap the direct child within
        # the existing cleanup allowance.
        monkeypatch.undo()
        survivor.kill()
        try:
            survivor.wait(timeout=_CLEANUP_BUDGET_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    assert survivor.poll() is not None
    assert process_identity.process_liveness(survivor.pid) == "absent"


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX group teardown")
def test_terminate_owned_group_holds_grace_for_delayed_provider_cleanup(
    tmp_path: Path,
) -> None:
    """A wrapper's TERM exit never expires the provider's grace deadline.

    The readiness-gated provider's TERM handler performs a delayed cleanup
    (0.3 s) inside the 1.0 s grace.  The wrapper exits almost immediately on
    TERM; the provider must still complete its cleanup (marker written, no
    KILL) before teardown reports success.
    """
    import time as _time

    ready = tmp_path / "ready"
    seen = tmp_path / "seen"
    done = tmp_path / "done"
    provider = tmp_path / "provider.py"
    provider.write_text(
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "def _term(s, f):\n"
        f"    Path({str(seen)!r}).touch()\n"
        "    time.sleep(0.3)\n"
        f"    Path({str(done)!r}).touch()\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, _term)\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, {str(provider)!r}]).wait()\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(wrapper)],
        process_group=0,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = _time.monotonic() + 10
        while not ready.exists() and _time.monotonic() < deadline:
            _time.sleep(0.02)
        assert ready.exists(), "provider readiness marker missing"
        start = _time.monotonic()
        assert process_identity.terminate_owned_group(
            proc, proc.pid, grace_seconds=1.0, kill_seconds=1.0
        ) is True
        elapsed = _time.monotonic() - start
        # The provider observed TERM and finished its delayed cleanup inside
        # the grace window: a KILL would have prevented the done marker, and
        # the teardown outlasted the cleanup despite the wrapper's exit.
        assert seen.exists() and done.exists()
        assert elapsed >= 0.3
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX group teardown")
def test_terminate_owned_group_kills_term_ignoring_provider_only_after_grace(
    tmp_path: Path,
) -> None:
    """A TERM-ignoring provider is KILLed only at the grace expiry.

    The wrapper exits on TERM almost immediately while its provider ignores
    TERM; the provider must survive the wrapper's exit and be escalated to
    KILL only once the full grace deadline has elapsed.
    """
    import signal as _signal
    import time as _time

    ready = tmp_path / "ready"
    provider = tmp_path / "provider.py"
    provider.write_text(
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(provider)!r}])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(wrapper)],
        process_group=0,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    provider_pid: int | None = None
    try:
        deadline = _time.monotonic() + 10
        while not ready.exists() and _time.monotonic() < deadline:
            _time.sleep(0.02)
        assert ready.exists(), "provider readiness marker missing"
        provider_pid = int(ready.read_text())
        start = _time.monotonic()
        assert process_identity.terminate_owned_group(
            proc, proc.pid, grace_seconds=1.0, kill_seconds=1.0
        ) is True
        elapsed = _time.monotonic() - start
        # The KILL waited for the full grace deadline even though the
        # wrapper exited almost immediately on TERM.
        assert elapsed >= 0.8
        assert process_identity.process_liveness(provider_pid) == "absent"
    finally:
        if provider_pid is not None:
            try:
                os.kill(provider_pid, _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX group teardown")
def test_terminate_owned_group_refuses_unproven_survivor(tmp_path: Path) -> None:
    """An unproven same-group survivor is not signalled; ownership is retained.

    A member that was not part of the captured membership (spawned during
    teardown in the same group, with a fresh birth) is not proven owned, so
    KILL escalation is refused and teardown reports False while the survivor
    stays alive.
    """
    import time as _time

    grandchild_pid_file = tmp_path / "gc.pid"
    # The provider's TERM handler spawns a grandchild in the same group (a
    # fresh birth not present in the pre-TERM capture) and then exits, leaving
    # that unproven survivor behind.
    provider3 = tmp_path / "p3.py"
    provider3.write_text(
        "import subprocess, sys, signal, time\n"
        "def _h(s, f):\n"
        "    p = subprocess.Popen([sys.executable, '-c',\n"
        "        'import signal, time; '\n"
        "        'signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])\n"
        f"    open({str(grandchild_pid_file)!r}, 'w').write(str(p.pid))\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, _h)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    wrapper3_script = tmp_path / "w3.py"
    # The wrapper's TERM handler briefly blocks so the provider (and its
    # handler that spawns the unproven grandchild) is fully dead/zombied
    # before the KILL decision is evaluated; it does not wait on the child
    # here (a Popen.wait inside a signal handler would deadlock the lock the
    # main body already holds).
    wrapper3_script.write_text(
        "import subprocess, sys, signal, time, os\n"
        f"subprocess.Popen([sys.executable, {str(provider3)!r}])\n"
        "def _h(s, f):\n"
        "    time.sleep(0.6)\n"
        "    os._exit(0)\n"
        "signal.signal(signal.SIGTERM, _h)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    wrapper3 = subprocess.Popen(
        [sys.executable, str(wrapper3_script)],
        process_group=0,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Give the provider a moment to be a live member of the group.
    _time.sleep(0.5)
    try:
        assert process_identity.terminate_owned_group(
            wrapper3, wrapper3.pid, grace_seconds=1.0, kill_seconds=0.5
        ) is False
    finally:
        try:
            gc_pid = int(grandchild_pid_file.read_text())
        except (FileNotFoundError, ValueError):
            gc_pid = None
        if gc_pid is not None:
            try:
                os.kill(gc_pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            wrapper3.kill()
        except (ProcessLookupError, OSError):
            pass
        _time.sleep(0.2)


@pytest.mark.skipif(sys.platform != "linux", reason="/proc observation is Linux-specific")
def test_session_members_ignores_unrelated_sessions() -> None:
    """Members of other sessions are never captured for a given session id."""
    leader = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    other = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        members = process_identity.session_members(leader.pid)
        assert members is not None
        pids = {m.pid for m in members}
        assert leader.pid in pids
        assert other.pid not in pids
    finally:
        leader.kill()
        leader.wait()
        other.kill()
        other.wait()


# ---------------------------------------------------------------------------
# Darwin native-output portability fixtures
#
# These exercise the actual native Apple ``ps`` output contract (padded PIDs,
# no numeric ``sid`` keyword) and the batch birth-identity parser on any host,
# so the Darwin path is validated locally even when native macOS execution is
# reserved for exact-SHA CI.
# ---------------------------------------------------------------------------


def _fake_ps(monkeypatch: pytest.MonkeyPatch, *, listing: str, births: str = "") -> None:
    def fake_run(argv, *args, **kwargs):
        argv = tuple(argv)
        if "pid=,pgid=,stat=" in argv:
            return subprocess.CompletedProcess(list(argv), 0, listing, "")
        if "pid=,lstart=" in argv:
            return subprocess.CompletedProcess(list(argv), 0, births, "")
        raise AssertionError(f"unexpected ps argv: {argv}")

    monkeypatch.setattr(process_identity.subprocess, "run", fake_run)


def _darwin_deadline() -> float:
    return time.monotonic() + 2.0


def test_darwin_batch_birth_parsing_preserves_padded_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Right-aligned PID columns and multi-word lstart strings parse cleanly."""
    out = "  123 Tue Oct  6 10:00:00 2026\n  456 Tue Oct  6 10:00:01 2026\n"
    _fake_ps(monkeypatch, listing="", births=out)
    assert process_identity._ps_lstart_batch([123, 456], _darwin_deadline()) == {
        123: "ps-lstart:Tue Oct  6 10:00:00 2026",
        456: "ps-lstart:Tue Oct  6 10:00:01 2026",
    }


def test_darwin_batch_birth_malformed_row_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A birth row missing its lstart string is ambiguous, not empty."""
    _fake_ps(monkeypatch, listing="", births="  123 Tue Oct  6 10:00:00 2026\n  456\n")
    assert process_identity._ps_lstart_batch([123, 456], _darwin_deadline()) is None


def test_darwin_session_members_avoids_sid_and_captures_separate_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native ps is never asked for an unsupported ``sid``; separate groups are kept."""
    seen: dict[str, object] = {}

    def fake_run(argv, *args, **kwargs):
        argv = tuple(argv)
        if "pid=,pgid=,stat=" in argv:
            seen["listing_argv"] = argv
            return subprocess.CompletedProcess(
                list(argv), 0, "  100 100 S\n  200 200 S\n  300 300 S\n", ""
            )
        if "pid=,lstart=" in argv:
            return subprocess.CompletedProcess(
                list(argv),
                0,
                "  100 Tue Oct  6 10:00:00 2026\n"
                "  200 Tue Oct  6 10:00:01 2026\n"
                "  300 Tue Oct  6 10:00:02 2026\n",
                "",
            )
        raise AssertionError(f"unexpected ps argv: {argv}")

    monkeypatch.setattr(process_identity.subprocess, "run", fake_run)
    monkeypatch.setattr(
        process_identity.os, "getsid", lambda pid: {100: 100, 200: 100, 300: 999}[pid]
    )
    monkeypatch.setattr(
        process_identity.os, "getpgid", lambda pid: {100: 100, 200: 200, 300: 300}[pid]
    )
    members = process_identity._darwin_session_members(
        100, _darwin_deadline(), 4096, 1_048_576
    )
    # The native inventory must not request an unsupported numeric sid keyword.
    assert seen["listing_argv"] == ("ps", "-axo", "pid=,pgid=,stat=")
    # 300 belongs to a different session and is excluded; 100/200 keep groups.
    by_pid = {m.pid: m for m in members}
    assert set(by_pid) == {100, 200}
    assert by_pid[100].pgid == 100 and by_pid[200].pgid == 200
    assert by_pid[100].birth == "ps-lstart:Tue Oct  6 10:00:00 2026"
    assert by_pid[200].birth == "ps-lstart:Tue Oct  6 10:00:01 2026"


def test_darwin_session_members_disappeared_omitted_inaccessible_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vanished member is omitted; an inaccessible member makes it unknown."""

    def fake_run(argv, *args, **kwargs):
        argv = tuple(argv)
        if "pid=,pgid=,stat=" in argv:
            return subprocess.CompletedProcess(
                list(argv), 0, "  100 100 S\n  200 200 S\n  300 300 S\n", ""
            )
        if "pid=,lstart=" in argv:
            return subprocess.CompletedProcess(
                list(argv),
                0,
                "  100 Tue Oct  6 10:00:00 2026\n  300 Tue Oct  6 10:00:02 2026\n",
                "",
            )
        raise AssertionError(f"unexpected ps argv: {argv}")

    monkeypatch.setattr(process_identity.subprocess, "run", fake_run)

    def _getpgid(pid):
        if pid == 200:
            raise ProcessLookupError("vanished")
        return {100: 100, 300: 300}[pid]

    # 200 vanished (ProcessLookupError) -> omitted; 100 and 300 are captured.
    monkeypatch.setattr(
        process_identity.os,
        "getsid",
        lambda pid: (
            _raise_process_lookup(200, pid) or {100: 100, 300: 100}[pid]
        ),
    )
    monkeypatch.setattr(process_identity.os, "getpgid", _getpgid)
    members = process_identity._darwin_session_members(
        100, _darwin_deadline(), 4096, 1_048_576
    )
    assert {m.pid for m in members} == {100, 300}

    # An inaccessible member (PermissionError) -> the whole observation is unknown.
    def _getsid_inaccessible(pid):
        if pid == 200:
            raise PermissionError("inaccessible")
        return {100: 100, 300: 100}[pid]

    monkeypatch.setattr(process_identity.os, "getsid", _getsid_inaccessible)
    assert (
        process_identity._darwin_session_members(100, _darwin_deadline(), 4096, 1_048_576)
        is None
    )


def _raise_process_lookup(pid: int, target: int):
    if pid == target:
        raise ProcessLookupError("vanished")
    return None


def test_darwin_session_members_respects_byte_and_member_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Byte and member bounds fail the observation, never a partial list."""
    big_listing = "\n".join(f"  {i} {i} S" for i in range(1, 5000))

    def fake_run_big(argv, *args, **kwargs):
        argv = tuple(argv)
        if "pid=,pgid=,stat=" in argv:
            return subprocess.CompletedProcess(list(argv), 0, big_listing, "")
        raise AssertionError(f"unexpected ps argv: {argv}")

    monkeypatch.setattr(process_identity.subprocess, "run", fake_run_big)
    assert process_identity._darwin_session_members(100, _darwin_deadline(), 4096, 50) is None

    small_listing = "\n".join(f"  {i} {i} S" for i in range(1, 6))

    def fake_run_small(argv, *args, **kwargs):
        argv = tuple(argv)
        if "pid=,pgid=,stat=" in argv:
            return subprocess.CompletedProcess(list(argv), 0, small_listing, "")
        raise AssertionError(f"unexpected ps argv: {argv}")

    monkeypatch.setattr(process_identity.subprocess, "run", fake_run_small)
    monkeypatch.setattr(process_identity.os, "getsid", lambda pid: 100)
    monkeypatch.setattr(process_identity.os, "getpgid", lambda pid: pid)
    assert (
        process_identity._darwin_session_members(100, _darwin_deadline(), 4, 1_048_576)
        is None
    )


def test_darwin_session_members_exhausted_budget_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No remaining observation budget yields unknown, not an empty session."""
    _fake_ps(monkeypatch, listing="  100 100 S\n", births="  100 Tue Oct  6 10:00:00 2026\n")
    monkeypatch.setattr(process_identity.os, "getsid", lambda pid: 100)
    monkeypatch.setattr(process_identity.os, "getpgid", lambda pid: pid)
    assert (
        process_identity._darwin_session_members(100, time.monotonic() - 1.0, 4096, 1_048_576)
        is None
    )


def test_darwin_session_members_zombie_has_no_birth(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zombie member is captured with no birth, never a live identity."""

    def fake_run(argv, *args, **kwargs):
        argv = tuple(argv)
        if "pid=,pgid=,stat=" in argv:
            return subprocess.CompletedProcess(
                list(argv), 0, "  100 100 S\n  200 200 Z\n", ""
            )
        if "pid=,lstart=" in argv:
            return subprocess.CompletedProcess(
                list(argv),
                0,
                "  100 Tue Oct  6 10:00:00 2026\n  200 Tue Oct  6 10:00:01 2026\n",
                "",
            )
        if argv == ("ps", "-o", "lstart=", "-p", "100"):
            return subprocess.CompletedProcess(
                list(argv), 0, "Tue Oct  6 10:00:00 2026\n", ""
            )
        raise AssertionError(f"unexpected ps argv: {argv}")

    monkeypatch.setattr(process_identity.subprocess, "run", fake_run)
    monkeypatch.setattr(process_identity.os, "getsid", lambda pid: 100)
    monkeypatch.setattr(process_identity.os, "getpgid", lambda pid: pid)
    members = process_identity._darwin_session_members(
        100, _darwin_deadline(), 4096, 1_048_576
    )
    by_pid = {m.pid: m for m in members}
    assert set(by_pid) == {100, 200}
    assert by_pid[100].birth == "ps-lstart:Tue Oct  6 10:00:00 2026"
    # A zombie is not live work and must never carry a usable birth identity.
    assert by_pid[200].birth is None
    assert process_identity.session_member_live(by_pid[200], 100) is False
    # A real host PID must not leak into the birth revalidation: with no
    # procfs the bounded ps probe is the only identity source.
    def fake_read(path, *, encoding: str) -> str:
        raise FileNotFoundError(path)

    monkeypatch.setattr(process_identity.Path, "read_text", fake_read)
    assert process_identity.session_member_live(by_pid[100], 100) is True


def test_darwin_group_state_distinguishes_live_zombie_and_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live member keeps a group present; a zombie-only group is absent.

    The kill(-pgid, 0) probe cannot make this distinction: an unreaped
    zombie keeps the numeric group alive to it, so the native scan must use
    the per-process state field.
    """
    monkeypatch.setattr(process_identity.sys, "platform", "darwin")

    def with_listing(listing: str) -> None:
        def fake_run(argv, *args, **kwargs):
            argv = tuple(argv)
            if "pid=,pgid=,stat=" in argv:
                return subprocess.CompletedProcess(list(argv), 0, listing, "")
            raise AssertionError(f"unexpected ps argv: {argv}")

        monkeypatch.setattr(process_identity.subprocess, "run", fake_run)

    # A live member of the target group: present.
    with_listing("  500 500 S\n  600 500 S\n  700 700 R\n")
    assert process_identity.process_group_state(500) == "present"
    # A zombie-only target group: positively absent (zombies are not live work).
    with_listing("  500 500 Z\n  600 500 Z\n  700 700 R\n")
    assert process_identity.process_group_state(500) == "absent"
    # No members at all: absent.
    with_listing("  700 700 R\n")
    assert process_identity.process_group_state(500) == "absent"


def test_darwin_group_state_malformed_incomplete_expired_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed rows, failed probes, over-bounded listings, and a scan that
    finishes at/after the shared deadline all stay unknown, never absent."""
    monkeypatch.setattr(process_identity.sys, "platform", "darwin")

    def with_listing(listing: str) -> None:
        def fake_run(argv, *args, **kwargs):
            argv = tuple(argv)
            if "pid=,pgid=,stat=" in argv:
                return subprocess.CompletedProcess(list(argv), 0, listing, "")
            raise AssertionError(f"unexpected ps argv: {argv}")

        monkeypatch.setattr(process_identity.subprocess, "run", fake_run)

    # A row missing its state field is ambiguous, not an empty group.
    with_listing("  500 500\n")
    assert process_identity.process_group_state(500) == "unknown"
    # A nonzero native exit is an observation failure.
    def failed_run(argv, *args, **kwargs):
        return subprocess.CompletedProcess(list(argv), 1, "", "ps: error")

    monkeypatch.setattr(process_identity.subprocess, "run", failed_run)
    assert process_identity.process_group_state(500) == "unknown"
    # A probe that times out is unknown.
    def timed_out_run(argv, *args, **kwargs):
        raise subprocess.TimeoutExpired(list(argv), float(kwargs["timeout"]))

    monkeypatch.setattr(process_identity.subprocess, "run", timed_out_run)
    assert process_identity.process_group_state(500) == "unknown"
    # An over-bounded listing with no target member fails the whole
    # observation (a live target member is positive presence, like Linux).
    big_listing = "\n".join(f"  {i} 9999 S" for i in range(1, 4098))
    with_listing(big_listing)
    assert process_identity.process_group_state(500) == "unknown"
    # A scan that consumes the entire remaining budget is expired evidence.
    clock = {"now": 100.0}
    monkeypatch.setattr(process_identity.time, "monotonic", lambda: clock["now"])

    def consume_full_budget(argv, *args, **kwargs):
        clock["now"] += float(kwargs["timeout"])
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(process_identity.subprocess, "run", consume_full_budget)
    assert process_identity.process_group_state(500, 101.0) == "unknown"


def test_darwin_group_absent_polls_zombie_only_within_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive cessation after KILL: a zombie-only group is absent inside the
    shared kill deadline; a live member polls to the deadline and stays unconfirmed."""
    monkeypatch.setattr(process_identity.sys, "platform", "darwin")

    def with_listing(listing: str) -> None:
        def fake_run(argv, *args, **kwargs):
            argv = tuple(argv)
            if "pid=,pgid=,stat=" in argv:
                return subprocess.CompletedProcess(list(argv), 0, listing, "")
            raise AssertionError(f"unexpected ps argv: {argv}")

        monkeypatch.setattr(process_identity.subprocess, "run", fake_run)

    with_listing("  500 500 Z\n")
    assert process_identity._group_absent(500, time.monotonic() + 1.0) is True
    with_listing("  500 500 S\n")
    assert process_identity._group_absent(500, time.monotonic() + 0.2) is False


def test_darwin_liveness_zombie_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreaped zombie is positively ceased on Darwin; live work is present.

    A bare ``ps -o pid=`` probe reports a zombie as present, so the liveness
    probe must read the native state field.
    """
    monkeypatch.setattr(process_identity.sys, "platform", "darwin")

    def with_output(stdout: str, rc: int = 0, stderr: str = "") -> None:
        def fake_run(argv, *args, **kwargs):
            argv = tuple(argv)
            if "pid=,stat=" in argv:
                return subprocess.CompletedProcess(list(argv), rc, stdout, stderr)
            raise AssertionError(f"unexpected ps argv: {argv}")

        monkeypatch.setattr(process_identity.subprocess, "run", fake_run)

    with_output("  100 Z\n")
    assert process_identity.process_liveness(100) == "absent"
    with_output("  100 S\n")
    assert process_identity.process_liveness(100) == "present"
    # A positively vanished PID: absent.
    with_output("", rc=1)
    assert process_identity.process_liveness(100) == "absent"
    # A row without its state field is unknown, never present.
    with_output("  100\n")
    assert process_identity.process_liveness(100) == "unknown"


def test_darwin_zombie_only_session_is_ceased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract regression: captured members left as unreaped zombies in
    zombie-only groups are positively ceased, the exact contract that
    ``stop_session_groups`` requires to report a terminated owned session."""
    from aflow.process_identity import SessionMember

    monkeypatch.setattr(process_identity.sys, "platform", "darwin")

    def fake_run(argv, *args, **kwargs):
        argv = tuple(argv)
        if "pid=,stat=" in argv:
            pid = argv[-1]
            return subprocess.CompletedProcess(list(argv), 0, f"  {pid} Z\n", "")
        if "pid=,pgid=,stat=" in argv:
            return subprocess.CompletedProcess(
                list(argv), 0, "  100 100 Z\n  200 200 Z\n", ""
            )
        raise AssertionError(f"unexpected ps argv: {argv}")

    monkeypatch.setattr(process_identity.subprocess, "run", fake_run)
    members = [
        SessionMember(pid=100, birth=None, pgid=100),
        SessionMember(pid=200, birth=None, pgid=200),
    ]
    assert persistent_units.session_ceased(members, time.monotonic() + 2.0) is True
    # A live member keeps the contract unconfirmed.
    def live_run(argv, *args, **kwargs):
        argv = tuple(argv)
        if "pid=,stat=" in argv:
            pid = argv[-1]
            return subprocess.CompletedProcess(list(argv), 0, f"  {pid} S\n", "")
        if "pid=,pgid=,stat=" in argv:
            return subprocess.CompletedProcess(
                list(argv), 0, "  100 100 S\n  200 200 Z\n", ""
            )
        raise AssertionError(f"unexpected ps argv: {argv}")

    monkeypatch.setattr(process_identity.subprocess, "run", live_run)
    assert persistent_units.session_ceased(members, time.monotonic() + 2.0) is False


# -- bounded evidence expiry ----------------------------------------------------
# A successful native observation that finishes at/after the shared stop
# deadline is expired evidence: never a birth identity, never liveness proof.


def test_bounded_birth_probe_finishing_at_deadline_is_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A birth probe that returns exactly at the deadline yields no identity."""
    import subprocess as _subprocess

    monkeypatch.setattr(process_identity.sys, "platform", "darwin")
    clock = {"now": 100.0}
    monkeypatch.setattr(process_identity.time, "monotonic", lambda: clock["now"])

    def consume_full_budget(argv, **kwargs):
        clock["now"] += float(kwargs["timeout"])
        return _subprocess.CompletedProcess(argv, 0, "Tue Oct  6 10:00:00 2026\n", "")

    monkeypatch.setattr(process_identity.subprocess, "run", consume_full_budget)
    assert process_identity.process_birth_bounded(100, 101.0) is None


def test_bounded_liveness_finishing_at_deadline_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A liveness probe that returns exactly at the deadline stays unknown."""
    import subprocess as _subprocess

    monkeypatch.setattr(process_identity.sys, "platform", "darwin")
    clock = {"now": 100.0}
    monkeypatch.setattr(process_identity.time, "monotonic", lambda: clock["now"])

    def consume_full_budget(argv, **kwargs):
        clock["now"] += float(kwargs["timeout"])
        return _subprocess.CompletedProcess(argv, 0, "100\n", "")

    monkeypatch.setattr(process_identity.subprocess, "run", consume_full_budget)
    assert process_identity.process_liveness(100, 101.0) == "unknown"


def test_bounded_probe_timeout_has_no_minimum_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounded probe window never floors above the remaining budget."""
    clock = {"now": 100.0}
    monkeypatch.setattr(process_identity.time, "monotonic", lambda: clock["now"])
    assert process_identity._bounded_probe_timeout(100.03) == pytest.approx(0.03)
    assert process_identity._bounded_probe_timeout(100.0) is None
    assert process_identity._bounded_probe_timeout(105.0) == 2.0


# -- native reads finishing at/after the shared stop deadline ------------------


def test_linux_birth_read_finishing_at_or_after_deadline_is_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Linux procfs birth read that finishes at/after the deadline is
    expired evidence; a read finishing before it is a valid birth."""
    clock = {"now": 100.0}
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    monkeypatch.setattr(process_identity.time, "monotonic", lambda: clock["now"])
    suffix = ["S", "cmd"] + ["0"] * 17 + ["12345", "0"]

    def late_suffix(pid):
        clock["now"] = 100.03
        return list(suffix)

    monkeypatch.setattr(process_identity, "_proc_stat_suffix", late_suffix)
    # 30 ms remaining: the read finishes exactly at the deadline.
    assert process_identity.process_birth_bounded(100, 100.03) is None
    # The read finishes after the deadline.
    clock["now"] = 100.0
    assert process_identity.process_birth_bounded(100, 100.02) is None
    # A read that finishes before the deadline is a valid birth.
    clock["now"] = 100.0
    assert process_identity.process_birth_bounded(100, 100.10) == (
        "linux-start-ticks:12345"
    )


def test_linux_liveness_read_finishing_at_deadline_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Linux procfs liveness read that finishes at the deadline is unknown."""
    clock = {"now": 100.0}
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    monkeypatch.setattr(process_identity.time, "monotonic", lambda: clock["now"])

    def late_read(self, encoding=None):
        clock["now"] = 100.03
        return "123 (cmd) S " + " ".join(str(i) for i in range(1, 20))

    monkeypatch.setattr(process_identity.Path, "read_text", late_read)
    # The read finishes exactly at the deadline: expired, never absence.
    assert process_identity.process_liveness(100, 100.03) == "unknown"
    # The same read finishing before the deadline is valid evidence.
    clock["now"] = 100.0
    assert process_identity.process_liveness(100, 100.10) == "present"


def test_linux_group_scan_finishing_at_deadline_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Linux group scan whose final read finishes at the deadline is
    unknown, never positive presence or absence."""
    clock = {"now": 100.0}
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    monkeypatch.setattr(process_identity.time, "monotonic", lambda: clock["now"])

    def late_suffix(pid):
        clock["now"] = 100.03
        return ["S", "cmd", "777", "1"] + ["0"] * 16

    class _Entry:
        name = "100"

        def exists(self) -> bool:
            return True

    monkeypatch.setattr(process_identity, "_proc_stat_suffix", late_suffix)
    monkeypatch.setattr(
        process_identity.Path, "iterdir", lambda self: [_Entry()]
    )
    assert process_identity.process_group_state(777, 100.03) == "unknown"
    clock["now"] = 100.0
    assert process_identity.process_group_state(777, 100.10) == "present"
    clock["now"] = 100.0
    assert process_identity.process_group_state(999, 100.03) == "unknown"


def test_expired_evidence_never_authorizes_cessation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired liveness/group evidence (unknown) never counts as cessation."""
    from aflow.process_identity import SessionMember

    clock = {"now": 100.0}
    monkeypatch.setattr(persistent_units.time, "monotonic", lambda: clock["now"])
    member = SessionMember(pid=900001, birth="linux-start-ticks:1", pgid=900001)
    monkeypatch.setattr(persistent_units, "process_liveness",
                        lambda pid, deadline: "unknown")
    monkeypatch.setattr(persistent_units, "process_group_state",
                        lambda pgid, deadline: "unknown")
    assert persistent_units.session_ceased([member], 101.0) is False


@pytest.mark.skipif(sys.platform != "linux", reason="procfs group-scan expiry is Linux-specific")
def test_terminate_owned_group_expired_absence_scan_keeps_claim_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired Linux group-absence scan in the kill phase never reports success.

    A real TERM-ignoring child owns the group through the (short) grace
    window, so teardown reaches the kill phase.  The final group-absence
    scan takes 40ms while the kill-phase deadline allows only 30ms: the scan
    completes after the deadline, so its result is expired evidence.  The
    teardown must return False (the claim stays unconfirmed) without
    renewing the kill deadline, even though the group would scan absent.
    """
    import time as _time

    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(child_script)],
        process_group=0,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Let the interpreter install its TERM handler before teardown.
        _time.sleep(0.3)
        assert proc.poll() is None
        assert process_identity.process_group_state(proc.pid) == "present"

        clock = {"now": 200.0}
        monkeypatch.setattr(process_identity.time, "monotonic", lambda: clock["now"])
        monkeypatch.setattr(
            process_identity.time,
            "sleep",
            lambda s: clock.update(now=clock["now"] + s),
        )

        def slow_scan(pgid, deadline=None):
            # One 40ms procfs scan that starts inside the 30ms kill-phase
            # deadline and therefore completes after it.
            clock["now"] += 0.040
            if deadline is not None and clock["now"] >= deadline:
                return "unknown"
            return "absent"

        monkeypatch.setattr(process_identity, "process_group_state", slow_scan)

        assert (
            process_identity.terminate_owned_group(
                proc, proc.pid, grace_seconds=0.05, kill_seconds=0.03
            )
            is False
        ), "an expired absence scan is never positive cessation"
    finally:
        proc.kill()
        proc.wait()
