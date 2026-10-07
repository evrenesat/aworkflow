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

from aflow.process_identity import (
    SESSION_OBSERVATION_SECONDS,
    controller_group_owned,
    process_birth_bounded,
    process_group_state,
)

#: Bounded KILL grace appended to the configured stop timeout so a verified
#: TERM survivor can be escalated and observed inside the shared stop window.
_STOP_KILL_GRACE_SECONDS = 2.0


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
    configured timeout). This adapter remains available to local control-plane
    callers without systemd; ``aflow ui`` uses the persistent adapter and the
    production ``SystemdUnitManager`` remains unchanged.
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
        # Imported lazily: persistent_units imports from this module, so a
        # top-level import would be circular.  Both adapters share the same
        # proven-session stop algorithm from that module.
        from aflow.control_plane import persistent_units as pu

        pid = process.pid
        # The outer stop deadline spans the configured TERM grace plus a
        # bounded KILL grace so a verified TERM survivor can be escalated and
        # observed inside the shared window.
        deadline = (
            time.monotonic() + self._stop_timeout_seconds + _STOP_KILL_GRACE_SECONDS
        )
        # Prove the controller's exact birth and dedicated session/group
        # topology before any signal: a live session leader uses the shared
        # proven-session stop algorithm (which also ends a separate-group
        # provider such as the local harness child), an exited leader drains
        # its owned group.  The birth probe is bounded by the shared stop
        # deadline: a timed-out or inaccessible read is unknown, never a
        # generic five-second fallback that spends stop budget.
        birth = process_birth_bounded(pid, deadline)
        contract = pu.session_contract(pid, birth, deadline)
        if contract.topology == "session":
            # The initial inventory and all associated proofs (original
            # controller revalidation, initial anchor/group signal proof)
            # share one <=2s operation deadline created before the first
            # snapshot; the outer deadline stays separate for later
            # rescans/escalation and is never renewed after the inventory.
            initial_operation = min(
                deadline, time.monotonic() + SESSION_OBSERVATION_SECONDS
            )
            captured = pu.session_snapshot(pid, initial_operation)
            if captured is None:
                # The session-leader contract is verified but the initial
                # observation was incomplete, so no session proof exists.  The
                # verified controller group may still be stopped within its
                # bounds (fresh ownership proof required before each signal),
                # but session cessation cannot be proven: fail typed.
                pu.stop_controller_group(
                    pid, birth, deadline, contract.session_id, self._signal_pgid
                )
                raise RuntimeError(
                    f"workflow unit {name} session observation was incomplete"
                )
            if not controller_group_owned(
                pid, birth, contract.session_id, initial_operation
            ):
                # The controller was verified only before the initial
                # observation; if it ceased (or its identity was reused)
                # during that observation the inventory cannot replace the
                # recorded authority: fail typed without any inventory-derived
                # signal.
                raise RuntimeError(
                    f"workflow unit {name} controller identity was lost during observation"
                )
            if not pu.stop_session_groups(
                pid,
                captured,
                deadline,
                signal_group=self._signal_pgid,
                snapshot=pu.session_snapshot,
                anchor=pu.session_anchor,
                ceased=pu.session_ceased,
                kill_escalation_seconds=self._stop_timeout_seconds,
                poll_interval=0.01,
                initial_operation=initial_operation,
            ):
                # Session proof existed and a live owned member (the
                # controller or a separate-group provider) survived the
                # bounded deadline: fail typed.  Never report inactive while
                # an owned group remains alive.
                raise RuntimeError(
                    f"workflow unit {name} owned session did not terminate"
                )
        else:
            if process.poll() is None:
                # The directly owned Popen is still live but its topology is
                # unknown (a timed-out or inaccessible birth/SID/PGID
                # observation): this is not an exited leader.  Never signal a
                # group on a reusable numeric PID, never infer success, and
                # never remove the unit: fail typed so the live controller
                # stays visible to the manager.
                raise RuntimeError(
                    f"workflow unit {name} controller topology is unknown"
                )
            # The owned Popen has positively exited (a zombie whose birth is
            # unobservable) before this stop.  Preserve the legacy
            # controller-group drain: signal the owned group and require
            # positive group absence before reporting success.
            if not self._drain_controller_group(pid, deadline):
                raise RuntimeError(
                    f"workflow unit {name} process group did not terminate"
                )
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

    def _signal_pgid(self, pgid: int, sig: signal.Signals) -> None:
        """Signal a verified numeric process group (the shared contract)."""
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return

    def _drain_controller_group(self, pid: int, deadline: float) -> bool:
        """Drain the owned group of an exited controller leader.

        The controller is a directly-owned Popen that has already exited (a
        zombie), so its birth is unobservable and no session proof exists.
        Signal the owned group (TERM, then KILL after the configured grace)
        and require positive group absence before reporting success; an
        unknown group state is never treated as absence.  Every group
        observation is bounded by one <=2s operation that never passes the
        outer stop deadline and is rechecked immediately before TERM, KILL
        and a successful absence result: expired evidence authorizes no
        signal and no positive cessation.
        """

        def _group_state() -> str:
            # One bounded operation per observation, never past the outer
            # stop deadline; expired evidence reports unknown.
            operation = min(deadline, time.monotonic() + SESSION_OBSERVATION_SECONDS)
            return process_group_state(pid, operation)

        if time.monotonic() < deadline:
            self._signal_pgid(pid, signal.SIGTERM)
        term_deadline = min(deadline, time.monotonic() + self._stop_timeout_seconds)
        while time.monotonic() < term_deadline and _group_state() == "present":
            time.sleep(0.01)
        if time.monotonic() < deadline and _group_state() == "present":
            self._signal_pgid(pid, signal.SIGKILL)
            while time.monotonic() < deadline and _group_state() == "present":
                time.sleep(0.01)
        return time.monotonic() < deadline and _group_state() == "absent"

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
