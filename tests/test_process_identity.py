"""Focused tests for portable process-birth identity observations."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from aflow import process_identity
from aflow import ui_cli
from aflow.control_plane import persistent_units, run_activity


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


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX group teardown")
def test_terminate_owned_group_escalates_proven_term_survivor(tmp_path: Path) -> None:
    """A proven owned TERM survivor is escalated to KILL and the group ends."""
    import time as _time

    def _provider_sleep(body: str, path: Path) -> None:
        path.write_text(body, encoding="utf-8")

    # Case 1: a wrapper that waits for its provider; TERM ends the whole group.
    provider1 = tmp_path / "p1.py"
    _provider_sleep("import time; time.sleep(30)", provider1)
    wrapper1 = tmp_path / "w1.py"
    wrapper1.write_text(
        f"import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, {str(provider1)!r}]).wait()\n",
        encoding="utf-8",
    )
    wrapper = subprocess.Popen(
        [sys.executable, str(wrapper1)],
        process_group=0,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert process_identity.terminate_owned_group(
        wrapper, wrapper.pid, grace_seconds=5.0, kill_seconds=1.0
    ) is True
    assert wrapper.poll() is not None

    # Case 2: a wrapper that is alive initially and dies on TERM, leaving a
    # TERM-ignoring provider in the same owned group.  The captured,
    # birth/PGID/SID-validated survivor is escalated to KILL, the group ends,
    # and teardown reports True (wrapper exit alone does not disable escalation).
    provider2 = tmp_path / "p2.py"
    _provider_sleep(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n",
        provider2,
    )
    provider_pid_file = tmp_path / "provider.pid"
    wrapper2_script = tmp_path / "w2.py"
    wrapper2_script.write_text(
        f"import subprocess, sys\n"
        f"p = subprocess.Popen([sys.executable, {str(provider2)!r}])\n"
        f"open({str(provider_pid_file)!r}, 'w').write(str(p.pid))\n"
        f"p.wait()\n",
        encoding="utf-8",
    )
    wrapper2 = subprocess.Popen(
        [sys.executable, str(wrapper2_script)],
        process_group=0,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = _time.monotonic() + 10
    while not provider_pid_file.exists() and _time.monotonic() < deadline:
        _time.sleep(0.02)
    provider_pid = int(provider_pid_file.read_text())
    assert process_identity.terminate_owned_group(
        wrapper2, wrapper2.pid, grace_seconds=1.0, kill_seconds=1.0
    ) is True
    assert wrapper2.poll() is not None
    # The proven TERM-ignoring survivor was escalated to KILL and has ceased.
    assert process_identity.process_liveness(provider_pid) == "absent"


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
