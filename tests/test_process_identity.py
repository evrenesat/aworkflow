"""Focused tests for portable process-birth identity observations."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from aflow import process_identity
from aflow import ui_cli
from aflow.control_plane import persistent_units, run_activity


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

    assert process_identity.process_birth_identity(123) == expected


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
