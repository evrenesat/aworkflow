"""Focused tests for portable process-birth identity observations."""

from __future__ import annotations

import os
import subprocess
import sys

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
            {"check": False, "capture_output": True, "text": True},
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
            {"check": False, "capture_output": True, "text": True},
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
