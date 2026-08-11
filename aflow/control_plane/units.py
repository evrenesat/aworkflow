"""A small, fakeable boundary around exact systemd unit observation."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import os
import re
import signal
import subprocess
import time
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

    def start(
        self,
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment_file: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> UnitState:
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

    def start(
        self,
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment_file: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> UnitState:
        if not name.startswith("aflow-run-") or not name.endswith(".service"):
            raise ValueError("workflow unit name must use the aflow-run-<id>.service form")
        if not argv or not all(isinstance(value, str) and value for value in argv):
            raise ValueError("workflow unit argv must be a non-empty string tuple")
        working_directory = Path(cwd).resolve()
        if not working_directory.is_dir():
            raise ValueError("workflow unit working directory must exist")
        environment_arguments = _environment_arguments(environment)
        environment_property: tuple[str, ...] = ()
        if environment_file is not None:
            resolved_environment = Path(environment_file).resolve()
            if Path(environment_file).is_symlink() or not resolved_environment.is_file():
                raise ValueError("workflow environment file must be a regular non-symlink file")
            environment_property = (f"--property=EnvironmentFile={resolved_environment}",)
        command = (
            "systemd-run",
            f"--unit={name}",
            "--property=Restart=no",
            "--collect",
            f"--working-directory={working_directory}",
            *environment_property,
            *environment_arguments,
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


def _environment_file_entries(environment_file: Path) -> Mapping[str, str]:
    """Parse a bounded KEY=VALUE environment file (mode-0600 token pattern)."""
    resolved = Path(environment_file).resolve()
    if Path(environment_file).is_symlink() or not resolved.is_file():
        raise ValueError("workflow environment file must be a regular non-symlink file")
    entries: dict[str, str] = {}
    for raw_line in resolved.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ValueError(f"workflow environment file line is not KEY=VALUE: {line}")
        if _ENVIRONMENT_NAME_RE.fullmatch(key) is None:
            raise ValueError(f"workflow environment file contains an unsafe key: {key}")
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"workflow environment file value for {key} contains control characters")
        entries[key] = value
    return entries


class SubprocessUnitManager:
    """Local-process unit adapter for the UnitManager protocol.

    Spawns each workflow as an independent subprocess in its own process
    group. ``stop`` signals the whole group (SIGTERM, then SIGKILL after the
    configured timeout). Intended for terminal ``aflow daemon`` use on hosts
    without systemd; the production ``SystemdUnitManager`` is unchanged.
    """

    def __init__(self, *, stop_timeout_seconds: float = 30.0) -> None:
        if (
            isinstance(stop_timeout_seconds, bool)
            or not isinstance(stop_timeout_seconds, (int, float))
            or not math.isfinite(stop_timeout_seconds)
            or stop_timeout_seconds < 0
        ):
            raise ValueError("subprocess unit stop timeout must be non-negative")
        self._stop_timeout_seconds = float(stop_timeout_seconds)
        self._units: dict[str, subprocess.Popen[str]] = {}

    def start(
        self,
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment_file: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> UnitState:
        if _UNIT_NAME_RE.fullmatch(name) is None:
            raise ValueError("workflow unit name must use the aflow-run-<id>.service form")
        if not argv or not all(isinstance(value, str) and value for value in argv):
            raise ValueError("workflow unit argv must be a non-empty string tuple")
        working_directory = Path(cwd).resolve()
        if not working_directory.is_dir():
            raise ValueError("workflow unit working directory must exist")
        if name in self._units and self._units[name].poll() is None:
            raise RuntimeError(f"workflow unit {name} is already active")
        env = dict(os.environ)
        if environment_file is not None:
            env.update(_environment_file_entries(environment_file))
        for key, value in (environment or {}).items():
            if _ENVIRONMENT_NAME_RE.fullmatch(key) is None:
                raise ValueError("unit environment must contain safe uppercase string entries")
            if not isinstance(value, str) or "\x00" in value or "\n" in value or "\r" in value:
                raise ValueError("unit environment must contain safe uppercase string entries")
            env[key] = value
        process = subprocess.Popen(
            list(argv),
            cwd=working_directory,
            env=env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._units[name] = process
        return UnitState(
            name=name,
            active_state="active",
            sub_state="running",
            main_pid=process.pid,
        )

    def stop(self, name: str) -> UnitState | None:
        process = self._units.get(name)
        if process is None:
            return None
        self._signal_group(process, signal.SIGTERM)
        deadline = time.monotonic() + self._stop_timeout_seconds
        while self._group_is_alive(process):
            process.poll()
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        if self._group_is_alive(process):
            self._signal_group(process, signal.SIGKILL)
            kill_deadline = time.monotonic() + 2.0
            while self._group_is_alive(process) and time.monotonic() < kill_deadline:
                process.poll()
                time.sleep(0.01)
        if self._group_is_alive(process):
            raise RuntimeError(f"workflow unit {name} process group did not terminate")
        process.wait()
        del self._units[name]
        return self._terminal_state(name, process)

    def shutdown(self) -> tuple[UnitState, ...]:
        """Drain and reap every subprocess still owned by this manager."""
        terminal: list[UnitState] = []
        for name in tuple(self._units):
            state = self.stop(name)
            if state is not None:
                terminal.append(state)
        return tuple(terminal)

    stop_all = shutdown

    def get(self, name: str) -> UnitState | None:
        process = self._units.get(name)
        if process is None:
            return None
        return_code = process.poll()
        if return_code is None:
            return UnitState(
                name=name,
                active_state="active",
                sub_state="running",
                main_pid=process.pid,
            )
        return self._terminal_state(name, process)

    def _signal_group(self, process: subprocess.Popen[str], sig: signal.Signals) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return

    def _group_is_alive(self, process: subprocess.Popen[str]) -> bool:
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _terminal_state(self, name: str, process: subprocess.Popen[str]) -> UnitState:
        return_code = process.poll()
        return UnitState(
            name=name,
            active_state="inactive",
            sub_state="dead",
            main_pid=process.pid,
            result="success" if return_code == 0 else f"exit:{return_code}",
        )


class InMemoryUnitManager:
    """Deterministic fake used by unit tests; it never spawns a process."""
    def __init__(self, units: Mapping[str, UnitState] | None = None) -> None:
        self.units = dict(units or {})
        self.start_calls: list[tuple[str, tuple[str, ...], Path]] = []
        self.stop_calls: list[str] = []

    def start(
        self,
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment_file: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> UnitState:
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


_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_UNIT_NAME_RE = re.compile(
    r"^aflow-run-[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.service$"
)


def _environment_arguments(environment: Mapping[str, str] | None) -> tuple[str, ...]:
    """Render an explicit, bounded environment allowlist for a unit command."""
    if environment is None:
        return ()
    rendered: list[str] = []
    for key in sorted(environment):
        value = environment[key]
        if (
            not isinstance(key, str)
            or _ENVIRONMENT_NAME_RE.fullmatch(key) is None
            or not isinstance(value, str)
            or "\x00" in value
            or "\n" in value
            or "\r" in value
        ):
            raise ValueError("unit environment must contain safe uppercase string entries")
        rendered.append(f"--setenv={key}={value}")
    return tuple(rendered)
