"""Durable account-local broker for exclusive execution resources.

The store serializes one capacity-one resource per derived combination key
across controller processes on the same host/account.  Every operation is
nonblocking: journal-lock contention, a busy resource, and a full queue all
return an outcome that a controller-owned, control-aware wait loop can
service.  The broker never calls providers, invokes harnesses, or rewrites
workflow state; it validates, fences, and persists claims only.

Crash boundaries: claims move ``queued -> reserved -> launching -> running
-> removed``.  A ``reserved`` claim cannot execute until a nonce-checked
durable ``launching`` write succeeds, and a ``running`` claim is released
only after positive cessation evidence.  Uncertain observations remain
uncertain; a corrupted journal fails closed for that resource instead of
being reset.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import stat as stat_module
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from aflow.process_identity import (
    host_boot_identity,
    process_birth_identity,
    process_group_state,
    process_liveness,
)

__all__ = [
    "ClaimSpec",
    "ControllerIdentity",
    "ExecutionLease",
    "ExecutionResourceAdmission",
    "ExecutionResourceStore",
    "MAX_JOURNAL_BYTES",
    "MAX_OUTSTANDING_CLAIMS",
    "Outcome",
    "ProcessEvidence",
    "RevalidationVerdict",
    "ResourceLeaseError",
    "TurnReprepareRequired",
    "JOURNAL_VERSION",
    "UNCHANGED",
    "owned_child_binding",
]

JOURNAL_VERSION = 1
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_OUTSTANDING_CLAIMS = 4096
DEFAULT_STORE_ROOT_PARTS = (".config", "aflow", "execution-resources")

_RESOURCE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")

ClaimStatus = Literal["queued", "reserved", "launching", "running", "unconfirmed"]
OwnerStatus = Literal["reserved", "launching", "running", "unconfirmed"]
GroupState = Literal["absent", "present", "unknown"]
OutcomeState = Literal[
    "queued",
    "acquired",
    "contended",
    "rejected",
    "cancelled",
    "released",
    "retained",
    "reclaimed",
    "unchanged",
    "launching",
    "running",
]

_CLAIM_STATUSES = frozenset({"queued", "reserved", "launching", "running", "unconfirmed"})
_OWNER_STATUSES = frozenset({"reserved", "launching", "running", "unconfirmed"})


@dataclass(frozen=True)
class ControllerIdentity:
    """PID/birth/host-boot identity of the controller owning a claim."""

    pid: int
    birth: str
    boot: str


@dataclass(frozen=True)
class ClaimSpec:
    """Caller-supplied claim identity; the broker assigns the ticket."""

    project_root: str
    run_id: str
    invocation_id: str
    kind: str
    role: str
    selector: str


@dataclass(frozen=True)
class ProcessEvidence:
    """Bounded observation of one process: liveness plus birth when present."""

    liveness: Literal["present", "absent", "unknown"]
    birth: str | None


@dataclass(frozen=True)
class Outcome:
    """Nonblocking operation result for the controller wait loop."""

    state: OutcomeState
    ticket: int | None = None
    reason: str | None = None


class _StoreError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ResourceLeaseError(Exception):
    """Resource-specific lifetime failure.

    Typed so callers can bypass catch-all provider recovery, manager-output
    validation, and correction escalation: a lease failure is an admission
    problem, not a model result.
    """

    def __init__(
        self,
        stage: str,
        reason: str,
        *,
        context: BaseException | None = None,
    ) -> None:
        super().__init__(f"execution resource {stage} failed: {reason}")
        self.stage = stage
        self.reason = reason
        if context is not None:
            self.__cause__ = context


class TurnReprepareRequired(Exception):
    """A live configuration change requires re-preparing the current turn.

    ``retained_invocation_id`` carries the claim identity forward when the
    queue position must survive the re-prepare (payload-only edits); ``None``
    re-prepares a fresh invocation that re-enters the destination queue at
    the tail.
    """

    def __init__(self, retained_invocation_id: str | None, reason: str) -> None:
        super().__init__(reason)
        self.retained_invocation_id = retained_invocation_id
        self.reason = reason


@dataclass(frozen=True)
class RevalidationVerdict:
    """Workflow-owned revalidation result for one admission control check.

    ``kind="reprepare"`` with ``retained_invocation_id`` set keeps the claim
    (and its ticket) across the re-prepare; ``None`` cancels it so the next
    prepare enqueues a fresh invocation at the tail.

    ``kind="retry"`` asks the engine to yield (with stop handling) and
    re-run the same final check: the current configuration pair could not be
    read under a lock-consistent load, so no launch may be decided from it.
    """

    kind: Literal["unchanged", "reprepare", "retry"]
    retained_invocation_id: str | None = None
    reason: str | None = None


UNCHANGED = RevalidationVerdict(kind="unchanged")


def owned_child_binding(process: Any) -> tuple[int, str | None, int | None]:
    """Observe the exact owned child: PID, birth identity, and real group.

    The group is the child's *actual* process-group identity, which differs
    per launch topology (a new session leader versus the parent's group).
    An unobservable group stays ``None`` and fails closed at reconciliation.
    """
    pid = process.pid
    try:
        pgid: int | None = os.getpgid(pid)
    except OSError:
        pgid = None
    return pid, process_birth_identity(pid), pgid


def _default_process_evidence(pid: int) -> ProcessEvidence:
    liveness = process_liveness(pid)
    birth = process_birth_identity(pid) if liveness == "present" else None
    return ProcessEvidence(liveness=liveness, birth=birth)


def _default_group_evidence(pgid: int | None) -> GroupState:
    # Shared with the owned-group teardown so lease termination evidence and
    # stop signalling agree on what counts as a live group member (zombies
    # are already dead and must not hold a claim open).
    return process_group_state(pgid)


def _valid_resource_id(resource: str) -> bool:
    return isinstance(resource, str) and bool(_RESOURCE_ID_PATTERN.fullmatch(resource))


def _claim_to_dict(
    spec: ClaimSpec,
    ticket: int,
    controller: ControllerIdentity,
    status: ClaimStatus,
) -> dict:
    return {
        "status": status,
        "project_root": spec.project_root,
        "run_id": spec.run_id,
        "invocation_id": spec.invocation_id,
        "kind": spec.kind,
        "role": spec.role,
        "selector": spec.selector,
        "ticket": ticket,
        "controller": {
            "pid": controller.pid,
            "birth": controller.birth,
            "boot": controller.boot,
        },
        "child_pid": None,
        "child_birth": None,
        "process_group": None,
    }


def _claim_from_dict(raw: object) -> dict | None:
    """Validate one journal claim record; malformed records fail closed."""
    if not isinstance(raw, dict):
        return None
    for key in (
        "project_root",
        "run_id",
        "invocation_id",
        "kind",
        "role",
        "selector",
    ):
        if not isinstance(raw.get(key), str):
            return None
    if raw.get("status") not in _CLAIM_STATUSES:
        return None
    ticket = raw.get("ticket")
    if not isinstance(ticket, int) or isinstance(ticket, bool) or ticket < 1:
        return None
    controller = raw.get("controller")
    if not isinstance(controller, dict):
        return None
    if not isinstance(controller.get("pid"), int) or isinstance(controller.get("pid"), bool):
        return None
    if controller.get("pid") < 1:
        return None
    for key in ("birth", "boot"):
        if not isinstance(controller.get(key), str) or not controller.get(key):
            return None
    for key in ("child_pid", "process_group"):
        value = raw.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            return None
    if raw.get("child_birth") is not None and not isinstance(raw.get("child_birth"), str):
        return None
    return raw


def _identity_matches(claim: dict, controller: ControllerIdentity) -> bool:
    recorded = claim["controller"]
    return (
        recorded["pid"] == controller.pid
        and recorded["birth"] == controller.birth
        and recorded["boot"] == controller.boot
    )


def _spec_matches(claim: dict, spec: ClaimSpec) -> bool:
    return (
        claim["project_root"] == spec.project_root
        and claim["run_id"] == spec.run_id
        and claim["kind"] == spec.kind
        and claim["role"] == spec.role
        and claim["selector"] == spec.selector
    )


def _find_claim(journal: dict, invocation_id: str) -> dict | None:
    owner = journal["owner"]
    if owner is not None and owner["invocation_id"] == invocation_id:
        return owner
    for claim in journal["queue"]:
        if claim["invocation_id"] == invocation_id:
            return claim
    return None


class ExecutionResourceStore:
    """Nonblocking durable FIFO broker over one account-local store root."""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        process_evidence: Callable[[int], ProcessEvidence] | None = None,
        group_evidence: Callable[[int | None], GroupState] | None = None,
        boot_provider: Callable[[], str | None] | None = None,
    ) -> None:
        if root is None:
            self._root = Path.home().joinpath(*DEFAULT_STORE_ROOT_PARTS)
        else:
            self._root = Path(root)
        self._process_evidence = process_evidence or _default_process_evidence
        self._group_evidence = group_evidence or _default_group_evidence
        self._boot_provider = boot_provider or host_boot_identity

    # -- identity ----------------------------------------------------------

    def current_controller_identity(self) -> ControllerIdentity | None:
        """Identity of this controller, or None when birth/boot is unknown."""
        pid = os.getpid()
        birth = process_birth_identity(pid)
        boot = self._boot_provider()
        if not birth or not boot:
            return None
        return ControllerIdentity(pid=pid, birth=birth, boot=boot)

    # -- paths -------------------------------------------------------------

    def _lock_path(self, resource: str) -> Path:
        return self._root / f"{resource}.lock"

    def _journal_path(self, resource: str) -> Path:
        return self._root / f"{resource}.json"

    # -- locking -----------------------------------------------------------

    @contextmanager
    def _resource_lock(self, resource: str) -> Iterator[int | None]:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self._lock_path(resource), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise _StoreError("store_io_failure") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(descriptor)
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                yield None
                return
            raise _StoreError("lock_io_failure") from exc
        try:
            yield descriptor
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    # -- journal I/O -------------------------------------------------------

    def _read_journal(self, resource: str) -> tuple[dict | None, str | None]:
        path = self._journal_path(resource)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return self._empty_journal(resource), None
        except OSError:
            return None, "journal_io_failure"
        if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISREG(info.st_mode):
            return None, "journal_not_regular"
        if info.st_size > MAX_JOURNAL_BYTES:
            return None, "journal_size_exceeded"
        try:
            raw = path.read_bytes()
            data = json.loads(raw)
        except (OSError, UnicodeError, ValueError):
            return None, "malformed_journal"
        journal = self._validate_journal(resource, data)
        if journal is None:
            return None, "malformed_journal"
        return journal, None

    @staticmethod
    def _empty_journal(resource: str) -> dict:
        return {
            "version": JOURNAL_VERSION,
            "resource": resource,
            "revision": 0,
            "next_ticket": 1,
            "owner": None,
            "queue": [],
        }

    @staticmethod
    def _validate_journal(resource: str, data: object) -> dict | None:
        if not isinstance(data, dict):
            return None
        if data.get("version") != JOURNAL_VERSION:
            return None
        if data.get("resource") != resource:
            return None
        revision = data.get("revision")
        next_ticket = data.get("next_ticket")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            return None
        if (
            not isinstance(next_ticket, int)
            or isinstance(next_ticket, bool)
            or next_ticket < 1
        ):
            return None
        owner = data.get("owner")
        queue = data.get("queue")
        if owner is not None:
            owner = _claim_from_dict(owner)
            if owner is None or owner["status"] not in _OWNER_STATUSES:
                return None
        if not isinstance(queue, list):
            return None
        claims = list(queue)
        if owner is not None:
            claims.append(owner)
        if len(claims) > MAX_OUTSTANDING_CLAIMS:
            return None
        for queued in queue:
            if not isinstance(queued, dict) or queued.get("status") != "queued":
                return None
        tickets: set[int] = set()
        invocations: set[str] = set()
        for claim in claims:
            claim = _claim_from_dict(claim)
            if claim is None:
                return None
            if claim["ticket"] in tickets:
                return None
            if claim["invocation_id"] in invocations:
                return None
            tickets.add(claim["ticket"])
            invocations.add(claim["invocation_id"])
        if tickets and next_ticket <= max(tickets):
            # A stale next_ticket would mint a duplicate ticket on the next
            # enqueue; the journal is malformed, not repairable by a rewrite.
            return None
        return {
            "version": JOURNAL_VERSION,
            "resource": resource,
            "revision": revision,
            "next_ticket": next_ticket,
            "owner": owner,
            "queue": queue,
        }

    def _write_journal(self, resource: str, journal: dict) -> None:
        payload = json.dumps(journal, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_JOURNAL_BYTES:
            raise _StoreError("journal_size_exceeded")
        path = self._journal_path(resource)
        descriptor, temporary = tempfile.mkstemp(
            dir=self._root, prefix=f".{resource}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    # -- operations ---------------------------------------------------------

    def enqueue(self, resource: str, spec: ClaimSpec, controller: ControllerIdentity | None) -> Outcome:
        if not _valid_resource_id(resource):
            return Outcome("rejected", reason="invalid_resource_id")
        if controller is None:
            return Outcome("rejected", reason="controller_identity_unavailable")
        try:
            with self._resource_lock(resource) as lock:
                if lock is None:
                    return Outcome("contended", reason="lock_contended")
                journal, error = self._read_journal(resource)
                if journal is None:
                    return Outcome("rejected", reason=error)
                existing = _find_claim(journal, spec.invocation_id)
                if existing is not None:
                    if _identity_matches(existing, controller) and _spec_matches(existing, spec):
                        if existing["status"] == "reserved":
                            # Idempotent re-admission of this caller's own
                            # unlaunched reservation: a payload-only
                            # reprepare keeps the claim (and its ticket)
                            # and must not fall back to waiting on itself.
                            return Outcome("acquired", ticket=existing["ticket"])
                        return Outcome("queued", ticket=existing["ticket"])
                    return Outcome("rejected", reason="identity_conflict")
                claims = len(journal["queue"]) + (1 if journal["owner"] is not None else 0)
                if claims >= MAX_OUTSTANDING_CLAIMS:
                    return Outcome("rejected", reason="capacity")
                ticket = journal["next_ticket"]
                journal["next_ticket"] = ticket + 1
                journal["queue"].append(_claim_to_dict(spec, ticket, controller, "queued"))
                journal["revision"] += 1
                try:
                    self._write_journal(resource, journal)
                except _StoreError as exc:
                    return Outcome("rejected", reason=exc.reason)
                except OSError:
                    return Outcome("rejected", reason="io_failure")
        except _StoreError as exc:
            return Outcome("rejected", reason=exc.reason)
        return Outcome("queued", ticket=ticket)

    def try_acquire(self, resource: str, invocation_id: str, controller: ControllerIdentity) -> Outcome:
        if not _valid_resource_id(resource):
            return Outcome("rejected", reason="invalid_resource_id")
        try:
            with self._resource_lock(resource) as lock:
                if lock is None:
                    return Outcome("contended", reason="lock_contended")
                journal, error = self._read_journal(resource)
                if journal is None:
                    return Outcome("rejected", reason=error)
                claim = _find_claim(journal, invocation_id)
                if claim is None:
                    return Outcome("rejected", reason="unknown_invocation")
                if not _identity_matches(claim, controller):
                    return Outcome("rejected", reason="identity_conflict")
                if claim["status"] != "queued":
                    return Outcome("queued", ticket=claim["ticket"], reason="busy")
                owner = journal["owner"]
                if owner is not None:
                    reason = "owner_unconfirmed" if owner["status"] == "unconfirmed" else "busy"
                    return Outcome("queued", ticket=claim["ticket"], reason=reason)
                head = min(journal["queue"], key=lambda item: item["ticket"])
                if head["invocation_id"] != invocation_id:
                    return Outcome("queued", ticket=claim["ticket"], reason="busy")
                journal["queue"] = [item for item in journal["queue"] if item is not claim]
                claim["status"] = "reserved"
                journal["owner"] = claim
                journal["revision"] += 1
                try:
                    self._write_journal(resource, journal)
                except _StoreError as exc:
                    return Outcome("rejected", reason=exc.reason)
                except OSError:
                    return Outcome("rejected", reason="io_failure")
        except _StoreError as exc:
            return Outcome("rejected", reason=exc.reason)
        return Outcome("acquired", ticket=claim["ticket"])

    def cancel(self, resource: str, invocation_id: str, controller: ControllerIdentity) -> Outcome:
        if not _valid_resource_id(resource):
            return Outcome("rejected", reason="invalid_resource_id")
        try:
            with self._resource_lock(resource) as lock:
                if lock is None:
                    return Outcome("contended", reason="lock_contended")
                journal, error = self._read_journal(resource)
                if journal is None:
                    return Outcome("rejected", reason=error)
                claim = _find_claim(journal, invocation_id)
                if claim is None:
                    return Outcome("rejected", reason="unknown_invocation")
                if not _identity_matches(claim, controller):
                    return Outcome("rejected", reason="identity_conflict")
                if claim["status"] not in ("queued", "reserved"):
                    return Outcome("rejected", reason="not_cancellable")
                if journal["owner"] is claim:
                    journal["owner"] = None
                else:
                    journal["queue"] = [item for item in journal["queue"] if item is not claim]
                journal["revision"] += 1
                ticket = claim["ticket"]
                try:
                    self._write_journal(resource, journal)
                except _StoreError as exc:
                    return Outcome("rejected", reason=exc.reason)
                except OSError:
                    return Outcome("rejected", reason="io_failure")
        except _StoreError as exc:
            return Outcome("rejected", reason=exc.reason)
        return Outcome("cancelled", ticket=ticket)

    def mark_launching(self, resource: str, invocation_id: str, controller: ControllerIdentity) -> Outcome:
        return self._owned_transition(
            resource,
            invocation_id,
            controller,
            allowed=("reserved",),
            mutate=lambda claim: claim.__setitem__("status", "launching"),
            success="launching",
        )

    def register_child(
        self,
        resource: str,
        invocation_id: str,
        controller: ControllerIdentity,
        child_pid: int,
        child_birth: str | None,
        process_group: int | None,
    ) -> Outcome:
        if child_pid < 1:
            return Outcome("rejected", reason="invalid_child")
        return self._owned_transition(
            resource,
            invocation_id,
            controller,
            allowed=("launching",),
            mutate=lambda claim: claim.update(
                {
                    "child_pid": child_pid,
                    "child_birth": child_birth,
                    "process_group": process_group,
                    "status": "running",
                }
            ),
            success="running",
        )

    def record_completion(
        self,
        resource: str,
        invocation_id: str,
        controller: ControllerIdentity,
        *,
        unconfirmed: bool = False,
    ) -> Outcome:
        if unconfirmed:
            return self._owned_transition(
                resource,
                invocation_id,
                controller,
                allowed=("reserved", "launching", "running", "unconfirmed"),
                mutate=lambda claim: claim.__setitem__("status", "unconfirmed"),
                success="retained",
                reason="owner_unconfirmed",
            )
        return self._owned_transition(
            resource,
            invocation_id,
            controller,
            allowed=("reserved", "launching", "running", "unconfirmed"),
            mutate=lambda claim: None,
            success="released",
            release=True,
        )

    def reconcile(
        self,
        resource: str,
        *,
        process_evidence: Callable[[int], ProcessEvidence] | None = None,
        group_evidence: Callable[[int | None], GroupState] | None = None,
    ) -> Outcome:
        if not _valid_resource_id(resource):
            return Outcome("rejected", reason="invalid_resource_id")
        evidence = process_evidence or self._process_evidence
        groups = group_evidence or self._group_evidence
        boot = self._boot_provider()

        # Sample observations outside the lock, then recheck under lock.
        journal, error = self._read_journal(resource)
        if journal is None:
            return Outcome("rejected", reason=error)
        revision = journal["revision"]
        claims = list(journal["queue"])
        if journal["owner"] is not None:
            claims.append(journal["owner"])
        removed: set[str] = set()
        reason: str | None = None
        for claim in claims:
            if self._reconcile_claim(claim, boot, evidence, groups) == "remove":
                removed.add(claim["invocation_id"])
                if claim is journal["owner"]:
                    reason = "owner_reclaimed"
                else:
                    reason = "dead_waiter_removed"
        if not removed:
            return Outcome("unchanged")
        try:
            with self._resource_lock(resource) as lock:
                if lock is None:
                    return Outcome("contended", reason="lock_contended")
                journal, error = self._read_journal(resource)
                if journal is None:
                    return Outcome("rejected", reason=error)
                if journal["revision"] != revision:
                    return Outcome("unchanged", reason="revision_changed")
                changed = False
                if journal["owner"] is not None and journal["owner"]["invocation_id"] in removed:
                    journal["owner"] = None
                    changed = True
                kept_queue = [claim for claim in journal["queue"] if claim["invocation_id"] not in removed]
                if len(kept_queue) != len(journal["queue"]):
                    journal["queue"] = kept_queue
                    changed = True
                if not changed:
                    return Outcome("unchanged")
                journal["revision"] += 1
                try:
                    self._write_journal(resource, journal)
                except _StoreError as exc:
                    return Outcome("rejected", reason=exc.reason)
                except OSError:
                    return Outcome("rejected", reason="io_failure")
        except _StoreError as exc:
            return Outcome("rejected", reason=exc.reason)
        return Outcome("reclaimed", reason=reason)

    # -- helpers -------------------------------------------------------------

    def _owned_transition(
        self,
        resource: str,
        invocation_id: str,
        controller: ControllerIdentity,
        *,
        allowed: tuple[str, ...],
        mutate: Callable[[dict], None],
        success: OutcomeState,
        reason: str | None = None,
        release: bool = False,
    ) -> Outcome:
        try:
            with self._resource_lock(resource) as lock:
                if lock is None:
                    return Outcome("contended", reason="lock_contended")
                journal, error = self._read_journal(resource)
                if journal is None:
                    return Outcome("rejected", reason=error)
                owner = journal["owner"]
                if owner is None or owner["invocation_id"] != invocation_id:
                    return Outcome("rejected", reason="not_owner")
                if not _identity_matches(owner, controller):
                    return Outcome("rejected", reason="identity_conflict")
                if owner["status"] not in allowed:
                    return Outcome("rejected", reason="not_owner")
                if release:
                    journal["owner"] = None
                else:
                    mutate(owner)
                journal["revision"] += 1
                ticket = owner["ticket"]
                try:
                    self._write_journal(resource, journal)
                except _StoreError as exc:
                    return Outcome("rejected", reason=exc.reason)
                except OSError:
                    return Outcome("rejected", reason="io_failure")
        except _StoreError as exc:
            return Outcome("rejected", reason=exc.reason)
        return Outcome(success, ticket=ticket, reason=reason)





    @staticmethod
    def _reconcile_claim(
        claim: dict,
        boot: str | None,
        evidence: Callable[[int], ProcessEvidence],
        groups: Callable[[int | None], GroupState],
    ) -> str:
        controller = claim["controller"]
        if boot is not None and controller["boot"] != boot:
            return "remove"
        observed = evidence(controller["pid"])
        if observed.liveness == "present":
            if observed.birth is not None and observed.birth != controller["birth"]:
                gone = True
            else:
                return "keep"
        elif observed.liveness == "absent":
            gone = True
        else:
            return "keep"
        if not gone:
            return "keep"
        if claim["status"] in ("queued", "reserved"):
            return "remove"
        if claim["child_pid"] is None:
            # Ambiguous spawn window or a child that was never bound: stay
            # occupied rather than manufacture an idle resource.
            return "keep"
        child = evidence(claim["child_pid"])
        child_gone = child.liveness == "absent" or (
            # A present PID with a different observed birth is a reused PID:
            # the recorded child is confirmed inactive. Without a stored child
            # birth the presence stays uncertain and the claim is retained.
            child.liveness == "present"
            and claim["child_birth"] is not None
            and child.birth is not None
            and child.birth != claim["child_birth"]
        )
        if not child_gone:
            return "keep"
        if groups(claim["process_group"]) != "absent":
            return "keep"
        return "remove"


class ExecutionLease:
    """Bind one durable claim to the actual lifetime of an owned invocation.

    The controller owns the admission wait loop and acquires the reservation
    before constructing the lease.  The lease then drives the claim through
    ``launching -> running -> released`` (or ``unconfirmed`` retention), with
    every transition fenced by the invocation nonce.  It never enqueues,
    acquires, cancels, calls providers, or rewrites workflow state; a bounded
    contention retry only re-issues the same nonce-checked transition.
    """

    def __init__(
        self,
        store: ExecutionResourceStore,
        resource: str,
        invocation_id: str,
        controller: ControllerIdentity,
        *,
        sleeper: Callable[[float], None] | None = None,
        contention_deadline_seconds: float = 5.0,
    ) -> None:
        self._store = store
        self._resource = resource
        self._invocation_id = invocation_id
        self._controller = controller
        self._sleeper = sleeper or time.sleep
        self._contention_deadline_seconds = contention_deadline_seconds
        self._launch_marked = False

    # -- transitions ---------------------------------------------------------

    def _transition(self, operation: Callable[[], Outcome]) -> Outcome:
        deadline = time.monotonic() + self._contention_deadline_seconds
        while True:
            outcome = operation()
            if outcome.state != "contended" or time.monotonic() >= deadline:
                return outcome
            self._sleeper(0.01)

    def _require(
        self, stage: str, expected: str, operation: Callable[[], Outcome]
    ) -> None:
        outcome = self._transition(operation)
        if outcome.state != expected:
            raise ResourceLeaseError(stage, outcome.reason or outcome.state)

    def mark_launching(self) -> None:
        """Persist durable launch intent before any model-bearing process starts.

        Idempotent: the durable ``reserved -> launching`` transition happens
        on the first call only (the controller orders it before
        ``_start_turn``); later calls at the dispatch seam are no-ops, so a
        single claim transition is recorded per invocation.
        """
        if self._launch_marked:
            return
        self._require(
            "mark_launching",
            "launching",
            lambda: self._store.mark_launching(
                self._resource, self._invocation_id, self._controller
            ),
        )
        self._launch_marked = True

    def bind_child(
        self,
        child_pid: int,
        child_birth: str | None = None,
        process_group: int | None = None,
    ) -> None:
        """Register the exact owned child before prompting it."""
        self._require(
            "register_child",
            "running",
            lambda: self._store.register_child(
                self._resource,
                self._invocation_id,
                self._controller,
                child_pid,
                child_birth,
                process_group,
            ),
        )

    def complete(self) -> None:
        """Release only after the owned process has been reaped (or launch never occurred)."""
        self._require(
            "release",
            "released",
            lambda: self._store.record_completion(
                self._resource, self._invocation_id, self._controller
            ),
        )

    def mark_unconfirmed(self) -> None:
        """Retain ownership when cessation cannot be positively confirmed."""
        self._require(
            "retain",
            "retained",
            lambda: self._store.record_completion(
                self._resource,
                self._invocation_id,
                self._controller,
                unconfirmed=True,
            ),
        )


class ExecutionResourceAdmission:
    """Shared control-aware admission loop for exclusive resources.

    Gates one invocation (a workflow turn, or an auxiliary model-bearing
    call) against the durable FIFO broker:

    * enqueues the claim idempotently (by invocation id),
    * polls ``try_acquire`` at ``poll_interval`` while servicing controls at
      least once per ``control_interval`` -- owner stop first, then live
      configuration revalidation -- including journal-lock contention,
    * during the wait, runs the store's bounded safe reconciliation for the
      exact resource at most once per ``control_interval`` (including the
      initial wait), after controls and before the next acquisition attempt;
      a proven-dead owner or dead queued predecessor is reclaimed and a
      fresh ``try_acquire`` follows, while unchanged/contended/revision
      results keep normal waiting,
    * cancels the claim and raises :class:`TurnReprepareRequired` when a
      live change requires re-preparation,
    * performs a final stop/config/control check after acquisition and
      before :meth:`ExecutionLease.mark_launching`, releasing an unlaunched
      reservation when the route identity changed while a payload-only edit
      rebuilds within the same reservation (ticket retained),
    * emits ``on_waiting`` / ``on_acquired`` / ``on_cancelled`` hooks so the
      controller can persist wait metadata and structured events.

    The engine never parses configuration, calls providers, or rewrites
    workflow state; those stay in the injected callbacks.  A ``resource`` of
    ``None`` is unrestricted and returns ``None`` without touching the
    broker.
    """

    def __init__(
        self,
        store: ExecutionResourceStore,
        *,
        poll_interval: float = 0.25,
        control_interval: float = 1.0,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        contention_deadline_seconds: float = 5.0,
    ) -> None:
        if poll_interval <= 0 or control_interval <= 0 or contention_deadline_seconds <= 0:
            raise ValueError("admission intervals must be positive")
        self._store = store
        self._poll_interval = poll_interval
        self._control_interval = control_interval
        self._sleeper = sleeper or time.sleep
        self._clock = clock or time.monotonic
        self._contention_deadline_seconds = contention_deadline_seconds

    # -- public ------------------------------------------------------------

    def admit(
        self,
        *,
        resource: str | None,
        spec: ClaimSpec,
        controller: ControllerIdentity | None,
        stop_check: Callable[[], None],
        revalidate: Callable[[bool], RevalidationVerdict],
        on_waiting: Callable[[str | None, int | None], None] | None = None,
        on_acquired: Callable[[int | None], None] | None = None,
        on_cancelled: Callable[[str], None] | None = None,
    ) -> ExecutionLease | None:
        """Admit one invocation, returning the lease to launch (or ``None``).

        Raises :class:`ResourceLeaseError` for fail-closed admission failures
        (no identity, rejected claim), the caller's ``stop_check`` exception
        (for example an owner stop, after the claim is cancelled), and
        :class:`TurnReprepareRequired` when the live configuration requires
        re-preparation (after the claim is cancelled or the unlaunched
        reservation is released).
        """
        if resource is None:
            return None
        if controller is None:
            raise ResourceLeaseError(
                "admission", "durable controller identity is unavailable"
            )
        outcome = self._store.enqueue(resource, spec, controller)
        if outcome.state == "rejected":
            raise ResourceLeaseError(
                "admission", outcome.reason or "claim rejected"
            )
        ticket = outcome.ticket
        acquired = outcome.state == "acquired"
        last_reason = outcome.reason
        if not acquired:
            self._service_controls(resource, spec, controller, stop_check, revalidate, on_cancelled)
            self._reconcile_dead_owner(resource)
            last_control_at = self._clock()
            if on_waiting is not None:
                on_waiting(last_reason, ticket)
            while not acquired:
                if self._clock() - last_control_at >= self._control_interval:
                    self._service_controls(
                        resource, spec, controller, stop_check, revalidate, on_cancelled
                    )
                    self._reconcile_dead_owner(resource)
                    last_control_at = self._clock()
                if ticket is None:
                    # The enqueue hit journal-lock contention: retry it.
                    outcome = self._store.enqueue(resource, spec, controller)
                    if outcome.state == "rejected":
                        raise ResourceLeaseError(
                            "admission", outcome.reason or "claim rejected"
                        )
                    ticket = outcome.ticket
                    acquired = outcome.state == "acquired"
                    if not acquired and on_waiting is not None and (
                        ticket is not None or outcome.reason != last_reason
                    ):
                        # A lock-contended enqueue had no ticket. Publish
                        # its recovered queue position before yielding again.
                        on_waiting(outcome.reason, ticket)
                    last_reason = outcome.reason
                else:
                    outcome = self._store.try_acquire(
                        resource, spec.invocation_id, controller
                    )
                    if outcome.state == "rejected":
                        raise ResourceLeaseError(
                            "admission", outcome.reason or "acquire rejected"
                        )
                    acquired = outcome.state == "acquired"
                    if not acquired and on_waiting is not None and (
                        outcome.reason != last_reason
                    ):
                        on_waiting(outcome.reason, outcome.ticket)
                        last_reason = outcome.reason
                if not acquired:
                    self._sleeper(self._poll_interval)

        # Final control check after acquisition, before durable launch
        # intent.  A changed route identity releases the unlaunched
        # reservation and re-prepares; a payload-only edit re-prepares with
        # the retained ticket.
        self._service_controls_final(
            resource, spec, controller, stop_check, revalidate, on_cancelled
        )
        lease = ExecutionLease(
            self._store, resource, spec.invocation_id, controller
        )
        if on_acquired is not None:
            on_acquired(ticket)
        # The caller owns durable launch intent: the dispatch seam calls
        # ``lease.mark_launching()`` immediately before the model-bearing
        # process starts.  The engine never launches on its own.
        return lease

    # -- internals ---------------------------------------------------------

    def _reconcile_dead_owner(self, resource: str) -> None:
        """Run one bounded safe reconciliation of the exact resource.

        Called at most once per ``control_interval`` while waiting, after
        stop/config controls and before the next acquisition attempt.  The
        store samples process/group evidence without holding the journal
        lock and fences the write behind a locked revision recheck, so the
        result is advisory: ``reclaimed`` only lets the next
        ``try_acquire`` run, while ``unchanged``, ``contended`` and
        ``revision_changed`` keep normal waiting.  A rejected store
        operation (for example a corrupt journal) fails closed with a typed
        resource error and never rewrites the journal.  A reconciliation
        result is not ownership and cannot launch a provider.
        """
        outcome = self._store.reconcile(resource)
        if outcome.state == "rejected":
            raise ResourceLeaseError(
                "admission", outcome.reason or "reconciliation_rejected"
            )

    def _service_controls(
        self,
        resource: str,
        spec: ClaimSpec,
        controller: ControllerIdentity,
        stop_check: Callable[[], None],
        revalidate: Callable[[bool], RevalidationVerdict],
        on_cancelled: Callable[[str], None] | None,
    ) -> None:
        """Service owner stop first, then live configuration revalidation."""
        try:
            stop_check()
        except BaseException:
            self._cancel_claim(resource, spec, controller)
            raise
        try:
            verdict = revalidate(final=False)
        except BaseException:
            # Mid-wait revalidation is expected to degrade to UNCHANGED;
            # anything else is fail-closed with the claim cancelled.
            self._cancel_claim(resource, spec, controller)
            raise
        if verdict.kind == "reprepare":
            if verdict.retained_invocation_id is None:
                # The route identity changed: the claim on this resource is
                # obsolete and must be cancelled so the journal is clean.
                self._cancel_claim(resource, spec, controller)
                if on_cancelled is not None:
                    on_cancelled(verdict.reason or "reprepare")
            # A retained invocation id keeps the queued claim (and its
            # ticket): re-admission reuses it idempotently after the turn is
            # re-prepared with the refreshed configuration.
            raise TurnReprepareRequired(
                verdict.retained_invocation_id, verdict.reason or "reprepare"
            )

    def _service_controls_final(
        self,
        resource: str,
        spec: ClaimSpec,
        controller: ControllerIdentity,
        stop_check: Callable[[], None],
        revalidate: Callable[[bool], RevalidationVerdict],
        on_cancelled: Callable[[str], None] | None,
    ) -> None:
        """Final stop/config/control check before ``mark_launching``."""
        while True:
            try:
                stop_check()
            except BaseException:
                self._release_reservation(resource, spec, controller)
                if on_cancelled is not None:
                    on_cancelled("stop_before_launch")
                raise
            try:
                verdict = revalidate(final=True)
            except BaseException:
                self._release_reservation(resource, spec, controller)
                if on_cancelled is not None:
                    on_cancelled("config_failure_before_launch")
                raise
            if verdict.kind == "retry":
                # The current configuration pair was lock-contended: the
                # reservation stays, the engine yields (with stop handling)
                # and re-runs the real final check.
                self._sleeper(self._poll_interval)
                continue
            if verdict.kind == "reprepare":
                if verdict.retained_invocation_id is None:
                    # The route identity changed: the unlaunched reservation
                    # is released and the turn re-prepares at the
                    # destination tail.
                    self._release_reservation(resource, spec, controller)
                    if on_cancelled is not None:
                        on_cancelled(verdict.reason or "reprepare")
                # A payload-only edit (retained invocation id) rebuilds
                # within the same reservation: the claim and its ticket are
                # kept and re-admission reuses the reservation idempotently.
                raise TurnReprepareRequired(
                    verdict.retained_invocation_id,
                    verdict.reason or "reprepare",
                )
            return

    def _cancel_claim(
        self,
        resource: str,
        spec: ClaimSpec,
        controller: ControllerIdentity,
    ) -> None:
        """Confirm removal before reporting cancellation or changing routes."""
        self._withdraw_claim(resource, spec, controller)

    def _release_reservation(
        self,
        resource: str,
        spec: ClaimSpec,
        controller: ControllerIdentity,
    ) -> None:
        """Remove only an acquired, positively unlaunched reservation."""
        self._withdraw_claim(resource, spec, controller)

    def _withdraw_claim(
        self,
        resource: str,
        spec: ClaimSpec,
        controller: ControllerIdentity,
    ) -> None:
        deadline = self._clock() + self._contention_deadline_seconds
        while True:
            try:
                outcome = self._store.cancel(resource, spec.invocation_id, controller)
            except Exception as exc:
                raise ResourceLeaseError("withdrawal", "store_failure", context=exc) from exc
            if outcome.state == "cancelled" or (
                outcome.state == "rejected" and outcome.reason == "unknown_invocation"
            ):
                # Unknown invocation is a locked, validated read proving the
                # claim is absent (including a contended enqueue that never
                # wrote it). Never turn not_cancellable into an unsafe release
                # of an invocation which may already have launched.
                return
            if outcome.state != "contended" or self._clock() >= deadline:
                raise ResourceLeaseError("withdrawal", outcome.reason or outcome.state)
            self._sleeper(min(self._poll_interval, self._control_interval, 0.01))
