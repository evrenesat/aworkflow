"""A small, fakeable boundary around exact systemd unit observation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Callable, Mapping, Protocol


@dataclass(frozen=True)
class UnitState:
    """The bounded identity evidence reconciliation is allowed to consume."""

    name: str
    active_state: str
    sub_state: str
    invocation_id: str | None = None
    result: str | None = None
    main_pid: int | None = None

    @property
    def is_active(self) -> bool:
        return self.active_state == "active"


class UnitManager(Protocol):
    """Unit operations used by control-plane services, without shell execution."""

    def start(self, name: str, argv: tuple[str, ...], *, cwd: Path) -> UnitState:
        ...

    def stop(self, name: str) -> UnitState | None:
        ...

    def get(self, name: str) -> UnitState | None:
        ...


class SystemdUnitManager:
    """Production adapter using fixed ``systemctl``/``systemd-run`` argv vectors."""

    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._runner = runner or subprocess.run

    def start(self, name: str, argv: tuple[str, ...], *, cwd: Path) -> UnitState:
        command = (
            "systemd-run",
            f"--unit={name}",
            "--property=Restart=no",
            "--collect",
            f"--working-directory={cwd}",
            *argv,
        )
        completed = self._runner(command, check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            raise RuntimeError(f"systemd-run failed for {name}: {completed.stderr.strip()}")
        state = self.get(name)
        if state is None:
            raise RuntimeError(f"systemd-run did not expose unit {name}")
        return state

    def stop(self, name: str) -> UnitState | None:
        self._runner(("systemctl", "stop", name), check=False, capture_output=True, text=True)
        return self.get(name)

    def get(self, name: str) -> UnitState | None:
        completed = self._runner(
            (
                "systemctl",
                "show",
                name,
                "--no-page",
                "--property=Id,ActiveState,SubState,InvocationID,Result,MainPID",
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            return None
        fields = _systemctl_fields(completed.stdout)
        observed_name = fields.get("Id")
        if not observed_name:
            return None
        raw_pid = fields.get("MainPID")
        return UnitState(
            name=observed_name,
            active_state=fields.get("ActiveState", "unknown"),
            sub_state=fields.get("SubState", "unknown"),
            invocation_id=fields.get("InvocationID") or None,
            result=fields.get("Result") or None,
            main_pid=int(raw_pid) if raw_pid and raw_pid.isdigit() else None,
        )


class InMemoryUnitManager:
    """Deterministic fake used by unit tests; it never spawns a process."""

    def __init__(self, units: Mapping[str, UnitState] | None = None) -> None:
        self.units = dict(units or {})
        self.start_calls: list[tuple[str, tuple[str, ...], Path]] = []
        self.stop_calls: list[str] = []

    def start(self, name: str, argv: tuple[str, ...], *, cwd: Path) -> UnitState:
        self.start_calls.append((name, argv, cwd))
        state = UnitState(name=name, active_state="active", sub_state="running")
        self.units[name] = state
        return state

    def stop(self, name: str) -> UnitState | None:
        self.stop_calls.append(name)
        previous = self.units.get(name)
        if previous is None:
            return None
        state = UnitState(
            name=name,
            active_state="inactive",
            sub_state="dead",
            invocation_id=previous.invocation_id,
            result="success",
        )
        self.units[name] = state
        return state

    def get(self, name: str) -> UnitState | None:
        return self.units.get(name)


def _systemctl_fields(stdout: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value
    return fields
