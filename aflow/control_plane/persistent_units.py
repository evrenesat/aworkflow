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
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid
from typing import Callable, Mapping

from aflow.process_identity import (
    SESSION_OBSERVATION_SECONDS,
    SessionMember,
    controller_group_owned,
    process_birth_bounded,
    process_birth_identity,
    process_birth_proof,
    process_group_state,
    process_liveness,
    session_member_live,
    session_members,
)

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
class _SessionContract:
    """Verified controller topology: session, legacy, or unknown."""

    topology: str
    session_id: int | None = None


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
    if path.is_symlink() or not path.is_file():
        return None
    try:
        if path.stat().st_size > 65_536:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _birth_identity(pid: int) -> str | None:
    return process_birth_identity(pid)


def _process_alive(pid: int, birth: str) -> bool:
    return _birth_identity(pid) == birth


def _bounded_process_alive(pid: int, birth: str, deadline: float) -> bool:
    """Bounded birth-verified liveness for deadline-bound stop paths.

    Delegates to the shared bounded birth probe (native procfs on Linux,
    a single bounded ``ps`` probe on Darwin): the exact recorded birth is
    required, and an expired deadline, a vanished, zombie, unobservable, or
    expired-read identity is never alive.  The generic five-second probe is
    never used here.
    """
    if time.monotonic() >= deadline:
        return False
    observed = process_birth_bounded(pid, deadline)
    return observed is not None and observed == birth


#: Bounded KILL-escalation window at the end of a stop deadline.
_KILL_ESCALATION_SECONDS = 2.0

#: Bounded TERM grace phase of a legacy controller-group stop.
_TERM_GRACE_SECONDS = 2.0


def _pid_present(pid: int) -> bool:
    """Cheap null-signal presence check used to gate birth revalidation."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, ValueError):
        return True
    return True


def session_contract(
    pid: int, birth: str, deadline: float
) -> _SessionContract:
    """Classify the controller's positively observed topology.

    ``session``: the controller is the live, birth-verified leader of its
    own session (modern wrapper).  ``legacy``: a positively observed genuine
    legacy controller-group topology - live exact birth, actual PGID equal to
    the recorded controller group, and a positively observed non-leader SID.
    Everything else (a disappearing controller, birth mismatch or missing
    birth, failed syscall, inaccessible identity, an expired stop deadline, or
    a non-self group) is ``unknown`` and is never collapsed into legacy.

    The liveness proof is bounded by the shared stop ``deadline`` (native
    procfs on Linux, a single bounded ``ps`` birth probe on Darwin); the
    generic five-second probe never consumes stop budget.
    """
    try:
        sid = os.getsid(pid)
        pgid = os.getpgid(pid)
    except (OSError, ValueError, AttributeError):
        return _SessionContract("unknown")
    # The contract birth probe shares the stop's bounded observation window,
    # never the entire outer stop deadline.
    if not _bounded_process_alive(
        pid, birth, min(deadline, time.monotonic() + SESSION_OBSERVATION_SECONDS)
    ):
        return _SessionContract("unknown")
    if sid == pid and pgid == pid:
        return _SessionContract("session", session_id=pid)
    if pgid == pid and sid != pid:
        return _SessionContract("legacy", session_id=sid)
    return _SessionContract("unknown")


def session_snapshot(
    session_id: int, deadline: float
) -> list[SessionMember] | None:
    """One bounded initial/rescan inventory under the shared stop deadline.

    The observation window is the raw remaining stop budget (capped at the
    two-second observation bound) with no minimum floor; an expired deadline
    yields ``None`` (unknown) without any further work.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return session_members(
        session_id,
        deadline_seconds=min(SESSION_OBSERVATION_SECONDS, remaining),
    )


def session_anchor(
    session_id: int,
    members: list[SessionMember],
    deadline: float,
) -> SessionMember | None:
    """Revalidate one proven captured member as the live anchor.

    Only a member of the proven set - the initial capture plus members
    observed under an earlier live anchor - whose exact birth, PGID, and SID
    all still match authorizes a further numeric-SID inventory.  Missing
    birth (zombie), reused PIDs, and unproven identities are never anchors:
    a fresh inventory can never prove its own ancestry.  Revalidation is
    bounded by the shared outer deadline and uses the native per-member
    checks, never an unbounded per-member fallback.
    """
    for member in members:
        if time.monotonic() >= deadline:
            return None
        if member.birth is not None and session_member_live(member, session_id, deadline):
            return member
    return None


def session_ceased(
    members: list[SessionMember],
    deadline: float,
    unproven_pids: frozenset[int] = frozenset(),
) -> bool:
    """Positive cessation of every captured identity and group.

    A positively dead member counts as ceased, including an unreaped zombie
    whose birth is unobservable (liveness reports absent).  A live or unknown
    identity, or a present or unknown group, retains failure; a missing birth
    is never equated with death on its own.

    Additionally, every unproven live identity from the last complete
    observation must be positively absent: a replacement session that reused
    the numeric SID is not part of the owned session and can never count as
    ceased on the owned members' behalf.
    """
    for pid in sorted(unproven_pids):
        if time.monotonic() >= deadline:
            return False
        if process_liveness(pid, deadline) != "absent":
            return False
    for member in members:
        if time.monotonic() >= deadline:
            return False
        if process_liveness(member.pid, deadline) != "absent":
            return False
    for pgid in sorted({member.pgid for member in members}):
        if time.monotonic() >= deadline:
            return False
        if process_group_state(pgid, deadline) != "absent":
            return False
    return True


def stop_controller_group(
    pid: int,
    birth: str,
    deadline: float,
    session_id: int | None,
    signal_group: Callable[[int, signal.Signals], None],
) -> bool:
    """Legacy stop: signal only the controller's own process group.

    Fresh bounded birth and group proof (plus the positively observed SID
    when one was verified) is required immediately before each TERM and KILL;
    a reused, moved, missing, or inaccessible identity receives no signal and
    the stop is unconfirmed.  Cessation is positive: an unknown liveness is
    never treated as absence.

    TERM is sent first and the controller is observed through a bounded TERM
    grace phase inside the outer deadline.  A genuine TERM survivor is
    escalated to KILL only after the same original birth/group/session is
    re-proved immediately before the KILL with a fresh operation bounded by
    the still-live outer deadline: an expired TERM-phase deadline is never
    passed as the KILL budget and the outer bound is never extended to issue a
    late signal.  Prompt success is retained when TERM suffices; changed,
    missing, or unknown ownership before the KILL receives no KILL.
    """
    if not controller_group_owned(pid, birth, session_id, deadline):
        return process_liveness(pid, deadline) == "absent"
    if time.monotonic() >= deadline:
        return process_liveness(pid, deadline) == "absent"
    signal_group(pid, signal.SIGTERM)
    # Bounded TERM phase: observe until positive cessation or the shorter of
    # the TERM grace and the remaining outer deadline.
    term_deadline = min(deadline, time.monotonic() + _TERM_GRACE_SECONDS)
    while (
        process_liveness(pid, deadline) == "present"
        and time.monotonic() < term_deadline
    ):
        time.sleep(0.05)
    if process_liveness(pid, deadline) != "present":
        # Positively ceased (or unknown) during the TERM phase.
        return process_liveness(pid, deadline) == "absent"
    # A genuine TERM survivor: prove the same original birth/group/session
    # immediately before the KILL with a fresh budget bounded by the
    # still-live outer deadline.
    if not controller_group_owned(pid, birth, session_id, deadline):
        return False
    if time.monotonic() >= deadline:
        # No time remains inside the outer bound: never issue a late KILL.
        return False
    signal_group(pid, signal.SIGKILL)
    while (
        process_liveness(pid, deadline) == "present"
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)
    return process_liveness(pid, deadline) == "absent"


def stop_session_groups(
    session_id: int,
    captured: list[SessionMember],
    deadline: float,
    *,
    signal_group: Callable[[int, signal.Signals], None],
    snapshot: Callable[[int, float], list[SessionMember] | None],
    anchor: Callable[
        [int, list[SessionMember], float], SessionMember | None
    ],
    ceased: Callable[[list[SessionMember], float, frozenset[int]], bool],
    kill_escalation_seconds: float = _KILL_ESCALATION_SECONDS,
    poll_interval: float = 0.05,
    initial_operation: float | None = None,
) -> bool:
    """Terminate every verified group inside the owned session.

    The initial capture is anchored by the verified session-leader contract
    and forms the proven set.  A numeric-SID inventory is only authorized
    while a proven member is live, and each inventory is trusted only after a
    live member of the proven set is revalidated against its exact birth,
    PGID, and SID AFTER the inventory completes; only same-session
    descendants observed under that post-inventory live anchor join the proven
    set and are signalled.  The fresh inventory can never prove its own
    ancestry: with no post-inventory live anchor no newly observed member is
    adopted, signalled, or used to clear prior uncertainty.  Before each
    actual signal a live proven member of the target group is revalidated;
    groups whose members are all positively dead (for example unreaped
    zombies) are never signalled.  TERM is sent once per verified group; a
    group is escalated to KILL only after its own TERM grace elapses inside
    the outer deadline and a live proven member is revalidated immediately
    before the KILL.  Every ownership proof, group proof, anchor selection,
    and cessation decision shares one operation deadline of at most two
    seconds (or the remaining outer deadline), never a fresh per-member
    window.  Incomplete observations stay pending uncertainty until a
    subsequent complete anchored observation clears them; an unanchored
    inventory can never clear them.  Returns True only on positive cessation
    of every captured identity and group and of every unproven live identity
    from the last complete observation, with no pending uncertainty.

    The ``signal_group``, ``snapshot``, ``anchor``, and ``ceased`` callbacks
    let each unit adapter reuse this single proven-session algorithm with its
    own signal function while the ownership/liveness decisions stay in this
    module's namespace.

    ``initial_operation`` (private to the unit adapters) shares the caller's
    pre-inventory operation deadline with the initial anchor/group signal
    proof so the initial snapshot and its associated proofs never receive
    renewed time; existing direct helper callers keep the current behavior
    of creating a fresh per-operation window.
    """
    members = list(captured)
    proven: set[int] = {member.pid for member in members}
    term_signalled: dict[int, float] = {}
    kill_signalled: set[int] = set()
    pending_uncertainty = False
    last_complete: list[SessionMember] = []

    def _operation_deadline() -> float:
        # One shared observation/proof budget per operation: at most the
        # two-second observation window, and never past the outer stop
        # deadline.
        return min(deadline, time.monotonic() + SESSION_OBSERVATION_SECONDS)

    def _unproven_live_pids() -> frozenset[int]:
        return frozenset(
            member.pid for member in last_complete if member.pid not in proven
        )

    def _proven_members() -> list[SessionMember]:
        return [member for member in members if member.pid in proven]

    def _signal_group_if_owned(
        pgid: int, inventory: list[SessionMember], operation: float
    ) -> None:
        # Revalidate a live proven member of the target group immediately
        # before each actual signal; positively dead members (zombies) and
        # unproven identities are never signalled, and no signal is issued
        # after the shared outer deadline has expired.
        if time.monotonic() >= operation or time.monotonic() >= deadline:
            return
        if not any(
            session_member_live(member, session_id, operation)
            for member in inventory
            if member.pgid == pgid and member.pid in proven
        ):
            return
        # The ownership proof may have completed at/after the shared deadline:
        # expired evidence authorizes no signal, TERM or KILL.
        if time.monotonic() >= operation or time.monotonic() >= deadline:
            return
        if pgid not in term_signalled:
            signal_group(pgid, signal.SIGTERM)
            term_signalled[pgid] = time.monotonic()
            return
        # TERM-first escalation: a group already TERMed is KILLed only after
        # its own TERM grace elapses, with fresh revalidation and while time
        # remains inside the outer deadline.
        if (
            pgid not in kill_signalled
            and time.monotonic() - term_signalled[pgid]
            >= kill_escalation_seconds
            and time.monotonic() < deadline
        ):
            signal_group(pgid, signal.SIGKILL)
            kill_signalled.add(pgid)

    # Signal the initial captured groups immediately so the controller chain
    # begins exiting before the first rescan; the whole initial proof shares
    # one operation deadline and each target group is revalidated against the
    # anchored initial capture first.
    if time.monotonic() >= deadline:
        return False
    # The initial anchor/group proof shares the caller's pre-inventory
    # operation deadline when provided (the initial snapshot's budget), so
    # the inventory and its proofs are never given renewed time; direct
    # helper callers keep the current per-operation behavior.
    if initial_operation is not None:
        operation = min(initial_operation, deadline)
    else:
        operation = _operation_deadline()
    if anchor(session_id, _proven_members(), operation) is not None:
        for pgid in sorted({member.pgid for member in members}):
            _signal_group_if_owned(pgid, members, operation)
    # Observe while something proven is still live or a signal was already
    # issued: with nothing live and nothing signalled there is no anchored
    # observation to authorize and the final cessation decision below decides
    # the outcome.
    while time.monotonic() < deadline and (
        term_signalled
        or anchor(session_id, _proven_members(), _operation_deadline())
        is not None
    ):
        # One shared operation deadline for the rescan inventory and all of
        # its post-inventory proof: the window is created before the snapshot
        # and never renewed after it.
        operation = _operation_deadline()
        session = snapshot(session_id, operation)
        if session is None:
            # Incomplete: the uncertainty stays pending until a subsequent
            # complete anchored observation clears it.  No inventory exists,
            # so no adoption or signal is authorized.
            pending_uncertainty = True
            time.sleep(poll_interval)
            continue
        last_complete = list(session)
        # The fresh inventory is trusted only after a proven member is
        # revalidated live against it; the anchor must survive the
        # observation, not merely precede it.
        anchor_member = anchor(session_id, _proven_members(), operation)
        if anchor_member is None:
            # No live proven anchor after the inventory: no adoption, signal,
            # or uncertainty clearing is authorized.  Success still requires
            # positive cessation of every captured identity and group and of
            # every unproven live identity from this complete observation.
            if not pending_uncertainty and ceased(
                members, operation, _unproven_live_pids()
            ):
                return True
            time.sleep(poll_interval)
            continue
        pending_uncertainty = False
        # Retain newly observed same-session descendants captured under the
        # live anchor; they join the proven set.
        known = {member.pid for member in members}
        new_members = [member for member in session if member.pid not in known]
        members.extend(new_members)
        proven.update(member.pid for member in new_members)
        for pgid in sorted({member.pgid for member in session}):
            _signal_group_if_owned(pgid, session, operation)
        if ceased(members, operation, _unproven_live_pids()):
            return True
        time.sleep(poll_interval)
    # Final bounded decision: with no pending uncertainty, positive cessation
    # of every captured identity and group and of every unproven live identity
    # from the last complete observation is success; anything else fails
    # typed rather than infer success.
    if not pending_uncertainty and ceased(
        members,
        min(deadline, initial_operation) if initial_operation is not None else deadline,
        _unproven_live_pids(),
    ):
        return True
    return False


def _receipts_for(name: str, cwd: Path) -> _UnitReceipts | None:
    match = _UNIT_NAME_RE.fullmatch(name)
    if match is None:
        raise ValueError("workflow unit name must use the aflow-run-<id>.service form")
    run_id = match.group("run_id")
    directory = Path(cwd) / ".aflow" / "runs" / run_id / "units"
    if any(part.is_symlink() for part in (directory, *directory.parents)):
        return None
    start = _read_json(directory / "start.json")
    if start is None or type(start.get("schema")) is not int or start.get("schema") != 1 or start.get("run_id") != run_id or start.get("unit") != name:
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
    return isinstance(receipt, dict) and type(receipt.get("schema")) is int and receipt.get("schema") == 1 and receipt.get("nonce") == nonce


def _terminal_receipt(receipts: _UnitReceipts, path: Path) -> dict | None:
    record = _read_json(path)
    if not _nonce_matches(record, receipts.nonce):
        return None
    try:
        at = datetime.fromisoformat(record["at"].replace("Z", "+00:00"))
        if at.tzinfo is None:
            return None
        if receipts.start.get("started_at"):
            started = datetime.fromisoformat(receipts.start["started_at"].replace("Z", "+00:00"))
            if started.tzinfo is None or at < started:
                return None
    except (KeyError, AttributeError, TypeError, ValueError):
        return None
    if path.name == "exit.json" and type(record.get("returncode")) is not int:
        return None
    if path.name == "stopped.json" and record.get("result") != "stopped":
        return None
    return record


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
        self._bound_project_root: Path | None = None

    def bind_project_root(self, repo_root: Path) -> None:
        """Bind receipt lookup to one validated daemon repository root."""
        source = Path(repo_root)
        try:
            root = source.resolve(strict=True)
        except OSError as exc:
            raise PersistentUnitError(
                "persistent unit project root must be an existing directory"
            ) from exc
        if not root.is_dir():
            raise PersistentUnitError(
                "persistent unit project root must be an existing directory"
            )
        if (
            self._bound_project_root is not None
            and self._bound_project_root != root
        ):
            raise PersistentUnitError(
                "persistent unit manager is already bound to a different project root"
            )
        for mapped_root in self._receipt_roots.values():
            try:
                Path(mapped_root).relative_to(root)
            except ValueError as exc:
                raise PersistentUnitError(
                    "persistent unit manager has a receipt mapping outside the project root"
                ) from exc
        self._bound_project_root = root

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
        if (
            self._bound_project_root is not None
            and working_directory != self._bound_project_root
        ):
            raise PersistentUnitError(
                "persistent unit manager is bound to a different project root; "
                "refusing to start outside that root"
            )
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
        match = _UNIT_NAME_RE.fullmatch(name)
        if match is None:
            raise ValueError("workflow unit name must use the aflow-run-<id>.service form")
        if self._bound_project_root is not None:
            return self._bound_project_root
        root = self._receipt_roots.get(name)
        if root is not None:
            return root
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

    def _observe(
        self, receipts: _UnitReceipts, deadline: float | None = None
    ) -> UnitState:
        # The generic observation contract is unchanged for ordinary `get`
        # (deadline is None).  Stop passes its entry operation deadline so
        # stop-specific birth checks use the bounded native probe and never
        # spend the generic five-second probe against a shorter stop budget.
        def _alive(pid: int, birth: str) -> bool:
            if deadline is None:
                return _process_alive(pid, birth)
            return _bounded_process_alive(pid, birth, deadline)

        child = _read_json(receipts.child)
        if _nonce_matches(child, receipts.nonce):
            pid, birth = child.get("pid"), child.get("process_birth")
            if type(pid) is int and pid > 0 and child.get("pgid") == pid and isinstance(birth, str) and _alive(pid, birth):
                return UnitState(name=receipts.name, active_state="active", sub_state="running", main_pid=pid)
        stopped = _terminal_receipt(receipts, receipts.stopped)
        if _nonce_matches(stopped, receipts.nonce):
            return UnitState(
                name=receipts.name,
                active_state="inactive",
                sub_state="dead",
                result="stopped",
            )
        exit_receipt = _terminal_receipt(receipts, receipts.exit)
        if _nonce_matches(exit_receipt, receipts.nonce) and type(exit_receipt.get("returncode")) is int:
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
            if deadline is None:
                # Ordinary `get` keeps the generic observation contract.
                if (
                    isinstance(wrapper_pid, int)
                    and isinstance(wrapper_birth, str)
                    and _alive(wrapper_pid, wrapper_birth)
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
            # Stop-specific bounded startup observation: a timeout, missing
            # or malformed wrapper identity, or a reused still-present PID is
            # unconfirmed, never inactive; only a positively observed live
            # wrapper stays active and only positive wrapper absence is a
            # genuine startup loss.
            if not (isinstance(wrapper_pid, int) and isinstance(wrapper_birth, str)):
                return UnitState(
                    name=receipts.name,
                    active_state="inactive",
                    sub_state="dead",
                    result="startup_unconfirmed",
                )
            proof = process_birth_proof(wrapper_pid, deadline)
            if proof.status == "observed":
                if proof.birth == wrapper_birth:
                    return UnitState(
                        name=receipts.name, active_state="active", sub_state="start-post"
                    )
                return UnitState(
                    name=receipts.name,
                    active_state="inactive",
                    sub_state="dead",
                    result="startup_unconfirmed",
                )
            return UnitState(
                name=receipts.name,
                active_state="inactive",
                sub_state="dead",
                result="startup_lost" if proof.status == "absent" else "startup_unconfirmed",
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
        if type(child.get("pid")) is not int or pid < 1 or child.get("pgid") != pid or not isinstance(birth, str) or not _alive(pid, birth):
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
        # The outer stop deadline is created before any stop-specific native
        # identity observation: the entry observation and identity checks
        # share one <=2s operation budget and never spend the generic
        # five-second probe against a shorter stop deadline.
        deadline = time.monotonic() + self._stop_timeout_seconds
        entry_operation = min(deadline, time.monotonic() + SESSION_OBSERVATION_SECONDS)
        current = self._observe(receipts, entry_operation)
        if not current.is_active:
            # A trusted terminal receipt (stopped/exit) is authoritative.
            # An ownership_lost answer only means the recorded child was not
            # positively observed alive inside the entry budget (vanished,
            # zombie, or unobservable): fail typed rather than fabricating an
            # inactive stop answer.  A startup_unconfirmed answer means the
            # recorded wrapper's birth proof timed out, was unavailable, or
            # its identity is missing/reused: the wrapper may still finish
            # startup, so stop fails typed with no signal and no
            # stopped.json.  Only positive wrapper absence (startup_lost) is
            # a genuine inactive stop answer.
            if current.result in ("ownership_lost", "startup_unconfirmed"):
                raise PersistentUnitError(
                    f"workflow unit {name} controller identity is unconfirmed"
                )
            return current
        child = _read_json(receipts.child)
        if not _nonce_matches(child, receipts.nonce):
            return current
        try:
            pid = int(child["pid"])
        except (KeyError, TypeError, ValueError):
            return current
        birth = child.get("process_birth")
        if not isinstance(birth, str) or not _bounded_process_alive(
            pid, birth, entry_operation
        ):
            # A live/unknown identity or a timed-out entry read inside the
            # bounded stop entry is unconfirmed: typed failure, no signal,
            # and no successful stopped receipt.
            raise PersistentUnitError(
                f"workflow unit {name} controller identity is unconfirmed"
            )
        if pid != int(child.get("pgid", pid)):
            raise PersistentUnitError(
                f"workflow unit {name} recorded mismatched process group identity"
            )
        contract = self._session_contract(pid, birth, deadline)
        if contract.topology == "session":
            # The initial inventory and all associated proofs (original
            # controller revalidation, initial anchor/group signal proof)
            # share one <=2s operation deadline created before the first
            # snapshot; the outer deadline stays separate for later
            # rescans/escalation and is never renewed after the inventory.
            initial_operation = min(
                deadline, time.monotonic() + SESSION_OBSERVATION_SECONDS
            )
            captured = self._session_snapshot(pid, initial_operation)
            if captured is None:
                # The session-leader contract is verified but the initial
                # observation was incomplete, so no session proof exists.
                # The verified controller group may still be stopped within
                # its bounds (fresh ownership proof required before each
                # signal), but session cessation cannot be proven: fail
                # typed and never write a successful stopped receipt.
                self._stop_controller_group(pid, birth, deadline, contract.session_id)
                raise PersistentUnitError(
                    f"workflow unit {name} session observation was incomplete"
                )
            if not self._controller_group_still_owned(
                pid, birth, contract.session_id, initial_operation
            ):  # shares the pre-inventory operation budget
                # The receipt-bound controller was verified only before the
                # initial observation.  If it ceased (or its identity was
                # reused) during that observation, the inventory's own birth
                # identities cannot replace the recorded authority: fail
                # typed without any inventory-derived signal and without a
                # successful stopped receipt.
                raise PersistentUnitError(
                    f"workflow unit {name} session leader identity was lost during observation"
                )
            if not self._stop_session_groups(
                pid, captured, deadline, initial_operation=initial_operation
            ):
                # Session proof existed and live owned members survived the
                # bounded deadline (or an anchored rescan went incomplete):
                # fail typed.  Never fall back to the weaker controller-only
                # check after a full observation.
                raise PersistentUnitError(
                    f"workflow unit {name} owned session did not terminate"
                )
        elif contract.topology == "legacy":
            # A positively observed genuine legacy controller-group
            # topology: stop only the controller's own group, as before.
            if not self._stop_controller_group(pid, birth, deadline, contract.session_id):
                raise PersistentUnitError(
                    f"workflow unit {name} process group did not terminate"
                )
        else:
            # Unknown topology: a disappearing controller, birth mismatch or
            # missing birth, failed syscall, or inaccessible identity.  No
            # signal is issued and no successful stopped receipt is written.
            raise PersistentUnitError(
                f"workflow unit {name} controller identity is unconfirmed"
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

    def _session_contract(
        self, pid: int, birth: str, deadline: float
    ) -> _SessionContract:
        """Classify the controller's positively observed topology.

        Delegates to the shared :func:`session_contract` so both unit
        adapters classify the same topology under the same bounded contract.
        """
        return session_contract(pid, birth, deadline)

    def _session_snapshot(
        self, session_id: int, deadline: float
    ) -> list[SessionMember] | None:
        """Bounded initial/rescan inventory under the shared stop deadline."""
        return session_snapshot(session_id, deadline)

    def _controller_group_still_owned(
        self,
        pid: int,
        birth: str,
        session_id: int | None,
        deadline: float,
    ) -> bool:
        """Fresh bounded proof that the recorded controller still owns its group.

        The proof is bounded to the caller's operation deadline (the shared
        pre-inventory budget on the initial proof, never a renewed window).
        """
        return controller_group_owned(pid, birth, session_id, deadline)

    def _stop_controller_group(
        self,
        pid: int,
        birth: str,
        deadline: float,
        session_id: int | None = None,
    ) -> bool:
        """Legacy stop: signal only the controller's own process group.

        Delegates to the shared :func:`stop_controller_group` with this
        manager's signal function so both adapters stop a legacy controller
        group under the same bounded deadline.
        """
        return stop_controller_group(
            pid, birth, deadline, session_id, self._signal_group
        )

    def _session_anchor(
        self,
        session_id: int,
        members: list[SessionMember],
        deadline: float,
    ) -> SessionMember | None:
        """Revalidate one proven captured member as the live anchor."""
        return session_anchor(session_id, members, deadline)

    def _session_ceased(
        self,
        members: list[SessionMember],
        deadline: float,
        unproven_pids: frozenset[int] = frozenset(),
    ) -> bool:
        """Positive cessation of every captured identity and group."""
        return session_ceased(members, deadline, unproven_pids)

    def _stop_session_groups(
        self,
        session_id: int,
        captured: list[SessionMember],
        deadline: float,
        *,
        initial_operation: float | None = None,
    ) -> bool:
        """Terminate every verified group inside the owned session.

        Delegates to the shared :func:`stop_session_groups` with this
        manager's signal/snapshot/anchor/ceased callbacks so both adapters
        run the same proven-session algorithm.  ``initial_operation`` shares
        the caller's pre-inventory budget with the initial anchor/group proof
        so the inventory and its proofs are never given renewed time.
        """
        return stop_session_groups(
            session_id,
            captured,
            deadline,
            signal_group=self._signal_group,
            snapshot=self._session_snapshot,
            anchor=self._session_anchor,
            ceased=self._session_ceased,
            kill_escalation_seconds=_KILL_ESCALATION_SECONDS,
            initial_operation=initial_operation,
        )

    # --------------------------------------------------------------- shutdown

    def shutdown(self) -> tuple[UnitState, ...]:
        """Intentionally drain nothing: persistent units outlive the server."""
        return ()

    stop_all = shutdown
