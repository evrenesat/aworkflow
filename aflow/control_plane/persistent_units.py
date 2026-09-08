"""Portable persistent subprocess units for the ``aflow ui`` launcher.

Unlike :class:`~aflow.control_plane.units.SubprocessUnitManager` (which owns
its units in memory and drains them on shutdown) and
:class:`~aflow.control_plane.units.SystemdUnitManager` (which requires
systemd), this adapter makes each workflow unit outlive the server that
launched it on both Linux and macOS.

Every launch spawns a small detached wrapper (the private ``aflow
ui-worker`` dispatch of the installed executable) that owns its workflow
subprocess group and writes bounded identity/exit evidence under the run's
durable ``units/`` directory:

- ``start.json``  written by the manager: run/unit/nonce/wrapper identity
- ``child.json``  written by the wrapper: workflow process PID/group identity
- ``exit.json``   written by the wrapper: terminal result
- ``error.json``  written by the wrapper: bounded startup-failure evidence
- ``stopped.json`` written by the manager after an explicit stop

Observation and signalling always require the recorded process-birth
identity to match the live process, never PID existence alone, so stale or
reused records become observable ownership errors instead of duplicate
launches or unrelated signals. ``shutdown`` is intentionally a no-op: UI
shutdown must not signal workflow groups.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid
from typing import Mapping

from .units import UnitState, _ENVIRONMENT_NAME_RE, _environment_file_entries

_UNIT_NAME_RE = re.compile(
    r"^aflow-run-(?P<run_id>[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)\.service$"
)
_START_WAIT_SECONDS = 10.0
_STOP_TIMEOUT_SECONDS = 30.0
_RECEIPT_SCHEMA = 1


class PersistentUnitError(RuntimeError):
    """A persistent unit launch, observation, or stop failed safely."""


@dataclass(frozen=True)
class _UnitReceipts:
    directory: Path
    run_id: str
    name: str
    start: dict
    nonce: str

    @property
    def child(self) -> Path:
        return self.directory / "child.json"

    @property
    def exit(self) -> Path:
        return self.directory / "exit.json"

    @property
    def error(self) -> Path:
        return self.directory / "error.json"

    @property
    def stopped(self) -> Path:
        return self.directory / "stopped.json"


def _write_receipt(path: Path, payload: Mapping[str, object], *, exclusive: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    if exclusive:
        flags |= os.O_EXCL
    fd = os.open(temp, flags, 0o644)
    try:
        os.write(fd, json.dumps(dict(payload), indent=2, sort_keys=True).encode("utf-8") + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    if exclusive:
        try:
            os.link(temp, path)
        except FileExistsError:
            temp.unlink(missing_ok=True)
            raise PersistentUnitError(
                f"run {path.parent.parent.name} already holds a launch claim; "
                "refusing a duplicate workflow unit"
            ) from None
        finally:
            temp.unlink(missing_ok=True)
    else:
        os.replace(temp, path)


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _birth_identity(pid: int) -> str | None:
    from aflow.daemon_cli import _process_birth_identity

    return _process_birth_identity(pid)


def _process_alive(pid: int, birth: str) -> bool:
    return _birth_identity(pid) == birth


def _receipts_for(name: str, cwd: Path) -> _UnitReceipts | None:
    match = _UNIT_NAME_RE.fullmatch(name)
    if match is None:
        raise ValueError("workflow unit name must use the aflow-run-<id>.service form")
    run_id = match.group("run_id")
    directory = Path(cwd) / ".aflow" / "runs" / run_id / "units"
    start = _read_json(directory / "start.json")
    if start is None:
        return None
    nonce = start.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        return None
    return _UnitReceipts(
        directory=directory,
        run_id=run_id,
        name=name,
        start=start,
        nonce=nonce,
    )


def _nonce_matches(receipt: dict | None, nonce: str) -> bool:
    return isinstance(receipt, dict) and receipt.get("nonce") == nonce


class PersistentUnitManager:
    """UnitManager adapter whose workflow units survive server shutdown."""

    def __init__(
        self,
        *,
        executable: Path | str,
        projects_root: Path | None = None,
        stop_timeout_seconds: float = _STOP_TIMEOUT_SECONDS,
    ) -> None:
        self._executable = Path(executable)
        self._projects_root = (
            Path(projects_root).resolve() if projects_root is not None else None
        )
        self._stop_timeout_seconds = float(stop_timeout_seconds)
        self._receipt_roots: dict[str, Path] = {}

    # ------------------------------------------------------------------ start

    def start(
        self,
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment_file: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> UnitState:
        match = _UNIT_NAME_RE.fullmatch(name)
        if match is None:
            raise ValueError("workflow unit name must use the aflow-run-<id>.service form")
        if not argv or not all(isinstance(value, str) and value for value in argv):
            raise ValueError("workflow unit argv must be a non-empty string tuple")
        working_directory = Path(cwd).resolve()
        if not working_directory.is_dir():
            raise ValueError("workflow unit working directory must exist")
        if not os.access(self._executable, os.X_OK):
            raise PersistentUnitError(
                f"the aflow executable is not runnable: {self._executable}"
            )
        self._receipt_roots[name] = working_directory
        run_id = match.group("run_id")
        receipt_dir = working_directory / ".aflow" / "runs" / run_id / "units"
        nonce = uuid.uuid4().hex
        env = dict(os.environ)
        if environment_file is not None:
            env.update(_environment_file_entries(environment_file))
        for key, value in (environment or {}).items():
            if (
                _ENVIRONMENT_NAME_RE.fullmatch(key) is None
                or not isinstance(value, str)
                or any(marker in value for marker in ("\x00", "\n", "\r"))
            ):
                raise ValueError("unit environment must contain safe uppercase string entries")
            env[key] = value
        wrapper_argv = [
            str(self._executable),
            "ui-worker",
            "--receipt-dir",
            str(receipt_dir),
            "--nonce",
            nonce,
            "--",
            *argv,
        ]
        birth = _birth_identity(os.getpid())
        if birth is None:
            raise PersistentUnitError("cannot establish the launching process identity")
        # The exclusive launch claim spans preparation: a concurrent start or
        # restart of the same run cannot create a second worker.
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_receipt(
            receipt_dir / "start.json",
            {
                "schema": _RECEIPT_SCHEMA,
                "run_id": run_id,
                "unit": name,
                "nonce": nonce,
                "launcher_birth": birth,
                "argv_digest": hashlib.sha256(
                    "\x00".join(argv).encode("utf-8")
                ).hexdigest(),
                "started_at": started_at,
            },
            exclusive=True,
        )
        process: subprocess.Popen | None = None
        try:
            process = subprocess.Popen(
                wrapper_argv,
                cwd=str(working_directory),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            (receipt_dir / "start.json").unlink(missing_ok=True)
            raise PersistentUnitError(
                f"failed to launch the workflow wrapper for {run_id}: {exc}"
            ) from exc
        wrapper_birth = _birth_identity(process.pid)
        _write_receipt(
            receipt_dir / "start.json",
            {
                "schema": _RECEIPT_SCHEMA,
                "run_id": run_id,
                "unit": name,
                "nonce": nonce,
                "launcher_birth": birth,
                "wrapper_pid": process.pid,
                "wrapper_birth": wrapper_birth,
                "argv_digest": hashlib.sha256(
                    "\x00".join(argv).encode("utf-8")
                ).hexdigest(),
                "started_at": started_at,
            },
            exclusive=False,
        )
        deadline = time.monotonic() + _START_WAIT_SECONDS
        while time.monotonic() < deadline:
            child = _read_json(receipt_dir / "child.json")
            if _nonce_matches(child, nonce):
                return UnitState(
                    name=name,
                    active_state="active",
                    sub_state="running",
                    main_pid=int(child["pid"]),
                )
            failure = _read_json(receipt_dir / "error.json")
            if _nonce_matches(failure, nonce):
                raise PersistentUnitError(
                    f"workflow unit {name} failed during startup: {failure.get('error')}"
                )
            if process.poll() is not None:
                break
            time.sleep(0.05)
        if not _nonce_matches(_read_json(receipt_dir / "child.json"), nonce):
            raise PersistentUnitError(
                f"workflow unit {name} did not report its process identity in time; "
                f"see {receipt_dir / 'wrapper.log'}"
            )
        child = _read_json(receipt_dir / "child.json")
        return UnitState(
            name=name,
            active_state="active",
            sub_state="running",
            main_pid=int(child["pid"]),
        )

    # -------------------------------------------------------------------- get

    def get(self, name: str) -> UnitState | None:
        receipts = _receipts_for(name, self._cwd_for(name))
        if receipts is None:
            return None
        return self._observe(receipts)

    # The UnitManager protocol has no cwd parameter on get/stop; the manager
    # remembers the launch cwd and, for units launched by earlier processes
    # (reattachment), rediscovers them under the configured projects root.
    _receipt_roots: dict[str, Path]

    def _cwd_for(self, name: str) -> Path:
        root = self._receipt_roots.get(name)
        if root is not None:
            return root
        match = _UNIT_NAME_RE.fullmatch(name)
        if match is None:
            raise ValueError("workflow unit name must use the aflow-run-<id>.service form")
        run_id = match.group("run_id")
        candidates = [Path.cwd()]
        if self._projects_root is not None:
            candidates.insert(0, self._projects_root)
        for base in candidates:
            if not base.is_dir():
                continue
            try:
                entries = sorted(base.iterdir())
            except OSError:
                continue
            for project_dir in entries:
                units_dir = project_dir / ".aflow" / "runs" / run_id / "units"
                if _read_json(units_dir / "start.json") is not None:
                    resolved = project_dir.resolve()
                    self._receipt_roots[name] = resolved
                    return resolved
        return Path("/")

    def register_project_root(self, repo_root: Path) -> None:
        """Make a project's run receipts observable by this manager."""
        root = Path(repo_root).resolve()
        runs = root / ".aflow" / "runs"
        if not runs.is_dir():
            return
        for run_dir in runs.iterdir():
            start_path = run_dir / "units" / "start.json"
            start = _read_json(start_path)
            if not isinstance(start, dict):
                continue
            unit = start.get("unit")
            if isinstance(unit, str) and _UNIT_NAME_RE.fullmatch(unit):
                self._receipt_roots[unit] = root

    def _observe(self, receipts: _UnitReceipts) -> UnitState:
        stopped = _read_json(receipts.stopped)
        if _nonce_matches(stopped, receipts.nonce):
            return UnitState(
                name=receipts.name,
                active_state="inactive",
                sub_state="dead",
                result="stopped",
            )
        exit_receipt = _read_json(receipts.exit)
        if _nonce_matches(exit_receipt, receipts.nonce):
            code = exit_receipt.get("returncode")
            result = "success" if code == 0 else f"exit:{code}"
            return UnitState(
                name=receipts.name,
                active_state="inactive",
                sub_state="dead",
                result=result,
            )
        child = _read_json(receipts.child)
        if not _nonce_matches(child, receipts.nonce):
            # No trusted child identity yet: either the wrapper is still
            # starting or it died before recording the workflow process.
            wrapper_pid = receipts.start.get("wrapper_pid")
            wrapper_birth = receipts.start.get("wrapper_birth")
            if (
                isinstance(wrapper_pid, int)
                and isinstance(wrapper_birth, str)
                and _process_alive(wrapper_pid, wrapper_birth)
            ):
                return UnitState(
                    name=receipts.name, active_state="active", sub_state="start-post"
                )
            return UnitState(
                name=receipts.name,
                active_state="inactive",
                sub_state="dead",
                result="startup_lost",
            )
        try:
            pid = int(child["pid"])
        except (KeyError, TypeError, ValueError):
            return UnitState(
                name=receipts.name,
                active_state="inactive",
                sub_state="dead",
                result="startup_lost",
            )
        birth = child.get("process_birth")
        if not isinstance(birth, str) or not _process_alive(pid, birth):
            # The wrapper writes exit.json after the child exits; reaching
            # here means the wrapper died without recording a terminal result.
            return UnitState(
                name=receipts.name,
                active_state="inactive",
                sub_state="dead",
                result="ownership_lost",
            )
        return UnitState(
            name=receipts.name,
            active_state="active",
            sub_state="running",
            main_pid=pid,
        )

    # ------------------------------------------------------------------- stop

    def stop(self, name: str) -> UnitState | None:
        receipts = _receipts_for(name, self._cwd_for(name))
        if receipts is None:
            return None
        current = self._observe(receipts)
        if not current.is_active:
            return current
        child = _read_json(receipts.child)
        if not _nonce_matches(child, receipts.nonce):
            return current
        try:
            pid = int(child["pid"])
        except (KeyError, TypeError, ValueError):
            return current
        birth = child.get("process_birth")
        if not isinstance(birth, str) or not _process_alive(pid, birth):
            return self._observe(receipts)
        if pid != int(child.get("pgid", pid)):
            raise PersistentUnitError(
                f"workflow unit {name} recorded mismatched process group identity"
            )
        self._signal_group(pid, signal.SIGTERM)
        deadline = time.monotonic() + self._stop_timeout_seconds
        while _process_alive(pid, birth) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _process_alive(pid, birth):
            self._signal_group(pid, signal.SIGKILL)
            kill_deadline = time.monotonic() + 2.0
            while _process_alive(pid, birth) and time.monotonic() < kill_deadline:
                time.sleep(0.05)
        if _process_alive(pid, birth):
            raise PersistentUnitError(
                f"workflow unit {name} process group did not terminate"
            )
        _write_receipt(
            receipts.stopped,
            {
                "schema": _RECEIPT_SCHEMA,
                "nonce": receipts.nonce,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "result": "stopped",
            },
            exclusive=False,
        )
        return UnitState(
            name=name,
            active_state="inactive",
            sub_state="dead",
            result="stopped",
        )

    def _signal_group(self, pgid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return

    # --------------------------------------------------------------- shutdown

    def shutdown(self) -> tuple[UnitState, ...]:
        """Intentionally drain nothing: persistent units outlive the server."""
        return ()

    stop_all = shutdown
