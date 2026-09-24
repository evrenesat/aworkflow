"""Cross-process project admission for workflow implementations.

The control plane owns launch manifests, startup records, units, and worker
receipts.  This module adds the small piece of state needed to make a capacity
decision before any of those boundaries start execution work: a nonce-bound
reservation journal protected by one lock in the project's primary checkout.

Reservations are deliberately supplemental.  A reservation can never turn an
ambiguous run into an inactive one; reconciliation only releases it when the
canonical run evidence proves that the logical run is no longer able to
execute.  Existing runs without a reservation are therefore included in the
capacity snapshot instead of being silently ignored.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Any, Literal
from uuid import uuid4

from aflow.control_plane.persistence import RunIdentityError, validate_run_id
from aflow.control_plane.repository import (
    MAX_READ_ARTIFACT_BYTES,
    RepositoryError,
    RepositoryNotFoundError,
    RunRepository,
)
from aflow.control_plane.worker_diagnostics import confirmed_inactive
from aflow.process_identity import process_birth_identity, process_liveness
from aflow.plan_dependencies import PlanDependencies, PlanDependencyBlocked, PlanDependencyError
from aflow.project_settings import (
    ProjectSettingsError,
    ProjectSettingsService,
    resolve_project_identity,
)


ADMISSION_SCHEMA_VERSION = 1
ADMISSION_STATE_FILENAME = "project-admission.json"
ADMISSION_LOCK_FILENAME = "project-admission.lock"
MAX_ADMISSION_STATE_BYTES = 4 * 1024 * 1024
MAX_RESERVATIONS = 4_096
MAX_SAFE_ADMISSION_MESSAGE_LENGTH = 256
_ADMISSION_SETTINGS_SAFE_MESSAGE = (
    "project admission settings are unavailable or invalid"
)
_SOURCE_PROVENANCE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?$")

ReservationState = Literal["reserved", "starting", "active", "uncertain", "released"]
_LIVE_RESERVATION_STATES = frozenset({"reserved", "starting", "active", "uncertain"})
_RESERVATION_STATES = _LIVE_RESERVATION_STATES | {"released"}
_BIRTH_IDENTITY_PREFIXES = ("linux-start-ticks:", "ps-lstart:")


class ProjectAdmissionError(RuntimeError):
    """The shared admission journal cannot safely answer a request."""

    code = "project_admission_error"

    @property
    def safe_message(self) -> str:
        """Return a bounded, terminal-safe projection of the admission error."""
        message = "".join(
            character if character.isprintable() else " "
            for character in str(self)
        )
        message = " ".join(message.split())
        if not message:
            message = "project admission could not be completed safely"
        if len(message) > MAX_SAFE_ADMISSION_MESSAGE_LENGTH:
            return message[: MAX_SAFE_ADMISSION_MESSAGE_LENGTH - 3] + "..."
        return message


class ProjectAdmissionSafetyError(ProjectAdmissionError):
    """Admission evidence or its filesystem boundary is unsafe."""


class ProjectAdmissionConflict(ProjectAdmissionError):
    """A reservation nonce or logical-run identity does not match."""


class ProjectPlanClaimConflict(ProjectAdmissionConflict):
    """A different run still owns the requested plan."""

    code = "project_plan_claim_conflict"


class ProjectPlanDependencyBlocked(ProjectAdmissionConflict):
    """A known series predecessor has not completed delivery."""

    code = "project_plan_dependency_blocked"


class ProjectCapacityReached(ProjectAdmissionError):
    """A manual launch reached the bounded project implementation limit."""

    code = "project_capacity_reached"
    message = "project implementation capacity is currently full"

    def __init__(
        self,
        *,
        limit: int,
        occupied: int,
        automatic: bool = False,
    ) -> None:
        self.limit = limit
        self.occupied = occupied
        self.automatic = automatic
        # Keep transport diagnostics bounded and independent of run/provider
        # text.  Automatic callers use the same typed conflict to defer.
        super().__init__(f"{self.message} ({occupied}/{limit})")

    @property
    def safe_message(self) -> str:
        return str(self)


@dataclass(frozen=True)
class _DirectResumeAdmissionProof:
    """Opaque evidence issued by the validated direct controller boundary."""

    source_run_id: str
    _token: object


_DIRECT_RESUME_ADMISSION_TOKEN = object()


def _validate_source_provenance_id(source_run_id: str) -> str:
    """Validate a historical source identity without making it a new run id."""
    if (
        not isinstance(source_run_id, str)
        or len(source_run_id) > 64
        or _SOURCE_PROVENANCE_RE.fullmatch(source_run_id) is None
    ):
        raise ProjectAdmissionSafetyError(
            "admission source provenance is not a safe path component"
        )
    return source_run_id


def _make_direct_resume_admission_proof(
    source_run_id: str,
) -> _DirectResumeAdmissionProof:
    """Create proof for workflow.py after its direct resume checks complete."""
    return _DirectResumeAdmissionProof(
        source_run_id=_validate_source_provenance_id(source_run_id),
        _token=_DIRECT_RESUME_ADMISSION_TOKEN,
    )


@dataclass(frozen=True)
class AdmissionReservation:
    """One durable, nonce-bound logical-run reservation."""

    run_id: str
    nonce: str
    state: ReservationState
    source_run_id: str | None = None
    plan_key: str | None = None
    idempotency_key: str | None = None
    claim_retained: bool = False
    bound: bool = False
    owner_pid: int | None = None
    owner_birth: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        if self.source_run_id is not None:
            _validate_source_provenance_id(self.source_run_id)
        if self.plan_key is not None and (
            not isinstance(self.plan_key, str)
            or not self.plan_key
            or len(self.plan_key) > 4_096
            or Path(self.plan_key).is_absolute()
            or any(part in {"", ".", ".."} for part in self.plan_key.split("/"))
            or Path(self.plan_key).as_posix() != self.plan_key
        ):
            raise ProjectAdmissionSafetyError("admission plan identity is invalid")
        if (
            not isinstance(self.nonce, str)
            or not 16 <= len(self.nonce) <= 128
            or any(character not in "0123456789abcdef" for character in self.nonce)
        ):
            raise ProjectAdmissionSafetyError("admission reservation nonce is invalid")
        if self.state not in _RESERVATION_STATES:
            raise ProjectAdmissionSafetyError("admission reservation state is invalid")
        if not isinstance(self.claim_retained, bool) or not isinstance(self.bound, bool):
            raise ProjectAdmissionSafetyError("admission reservation flags are invalid")
        if self.owner_pid is not None and (
            not isinstance(self.owner_pid, int) or isinstance(self.owner_pid, bool) or self.owner_pid < 1
        ):
            raise ProjectAdmissionSafetyError("admission reservation owner is invalid")
        if self.owner_birth is not None and (
            not isinstance(self.owner_birth, str) or not self.owner_birth or len(self.owner_birth) > 256
        ):
            raise ProjectAdmissionSafetyError("admission reservation owner identity is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "nonce": self.nonce,
            "state": self.state,
            "source_run_id": self.source_run_id,
            "plan_key": self.plan_key,
            "idempotency_key": self.idempotency_key,
            "claim_retained": self.claim_retained,
            "bound": self.bound,
            "owner_pid": self.owner_pid,
            "owner_birth": self.owner_birth,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class AdmissionSnapshot:
    """Capacity accounting returned after one locked reconciliation pass."""

    limit: int
    reserved_count: int
    starting_count: int
    active_count: int
    uncertain_count: int
    unmanaged_run_ids: tuple[str, ...] = ()
    claim_retained_run_ids: tuple[str, ...] = ()

    @property
    def occupied_count(self) -> int:
        return (
            self.reserved_count
            + self.starting_count
            + self.active_count
            + self.uncertain_count
        )

    @property
    def available_slots(self) -> int:
        return max(0, self.limit - self.occupied_count)

    @property
    def reserved_or_active_count(self) -> int:
        """Compatibility spelling for callers interested in all live claims."""
        return self.occupied_count

    def to_dict(self) -> dict[str, object]:
        return {
            "limit": self.limit,
            "reserved_count": self.reserved_count,
            "starting_count": self.starting_count,
            "active_count": self.active_count,
            "uncertain_count": self.uncertain_count,
            "occupied_count": self.occupied_count,
            "available_slots": self.available_slots,
            "unmanaged_run_ids": list(self.unmanaged_run_ids),
            "claim_retained_run_ids": list(self.claim_retained_run_ids),
        }


@dataclass(frozen=True)
class _RunEvidence:
    """Internal classification of one canonical logical run."""

    state: Literal["absent", "reserved", "starting", "active", "uncertain", "inactive"]
    claim_retained: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_nonce() -> str:
    return uuid4().hex


def _birth_identity_scheme(identity: str) -> str | None:
    """Return the trusted observation scheme encoded in a birth identity."""
    for prefix in _BIRTH_IDENTITY_PREFIXES:
        if identity.startswith(prefix):
            return prefix
    return None


def _safe_project_directory(root: Path) -> Path:
    """Create and validate the primary checkout's private state directory."""
    root = Path(root).resolve()
    if not root.is_dir() or root.is_symlink():
        raise ProjectAdmissionSafetyError("project admission root is unavailable")
    directory = root / ".aflow"
    if directory.is_symlink():
        raise ProjectAdmissionSafetyError("project admission directory must not be a symlink")
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ProjectAdmissionSafetyError(
                "project admission directory is unavailable"
            ) from exc
        try:
            metadata = directory.lstat()
        except OSError as exc:
            raise ProjectAdmissionSafetyError(
                "project admission directory is unavailable"
            ) from exc
    except OSError as exc:
        raise ProjectAdmissionSafetyError("project admission directory is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or directory.is_symlink():
        raise ProjectAdmissionSafetyError("project admission path must be a directory")
    try:
        resolved = directory.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectAdmissionSafetyError("project admission path escapes the project") from exc
    return directory


def _read_json_file(path: Path) -> Mapping[str, Any] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProjectAdmissionSafetyError("project admission state is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ProjectAdmissionSafetyError("project admission state must be a regular file")
    if metadata.st_size > MAX_ADMISSION_STATE_BYTES:
        raise ProjectAdmissionSafetyError("project admission state exceeds the size limit")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ProjectAdmissionSafetyError("project admission state is unreadable") from exc
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            raw = handle.read(MAX_ADMISSION_STATE_BYTES + 1)
    except OSError as exc:
        raise ProjectAdmissionSafetyError("project admission state is unreadable") from exc
    if len(raw) > MAX_ADMISSION_STATE_BYTES:
        raise ProjectAdmissionSafetyError("project admission state exceeds the size limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectAdmissionSafetyError("project admission state is malformed") from exc
    if not isinstance(value, Mapping):
        raise ProjectAdmissionSafetyError("project admission state must be an object")
    return value


def _optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 4_096:
        raise ProjectAdmissionSafetyError(f"admission {field} is invalid")
    return value


def _decode_reservation(value: object) -> AdmissionReservation:
    if not isinstance(value, Mapping):
        raise ProjectAdmissionSafetyError("admission reservation is malformed")
    try:
        run_id = validate_run_id(value["run_id"])
        nonce = value["nonce"]
        state = value["state"]
        source_run_id = value.get("source_run_id")
        plan_key = value.get("plan_key")
        idempotency_key = value.get("idempotency_key")
        claim_retained = value.get("claim_retained", False)
        bound = value.get("bound", False)
        owner_pid = value.get("owner_pid")
        owner_birth = value.get("owner_birth")
        created_at = value.get("created_at", "")
        updated_at = value.get("updated_at", "")
    except (KeyError, TypeError, ValueError) as exc:
        raise ProjectAdmissionSafetyError("admission reservation is malformed") from exc
    if source_run_id is not None:
        try:
            source_run_id = _validate_source_provenance_id(source_run_id)
        except (TypeError, ValueError, ProjectAdmissionError) as exc:
            raise ProjectAdmissionSafetyError("admission source run is invalid") from exc
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 4_096
    ):
        raise ProjectAdmissionSafetyError("admission idempotency key is invalid")
    if not isinstance(created_at, str) or not isinstance(updated_at, str):
        raise ProjectAdmissionSafetyError("admission reservation timestamps are invalid")
    if owner_pid is not None and (
        not isinstance(owner_pid, int) or isinstance(owner_pid, bool) or owner_pid < 1
    ):
        raise ProjectAdmissionSafetyError("admission reservation owner is invalid")
    if owner_birth is not None and (
        not isinstance(owner_birth, str) or not owner_birth or len(owner_birth) > 256
    ):
        raise ProjectAdmissionSafetyError("admission reservation owner identity is invalid")
    try:
        return AdmissionReservation(
            run_id=run_id,
            nonce=nonce,  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
            source_run_id=source_run_id,  # type: ignore[arg-type]
            plan_key=plan_key,  # type: ignore[arg-type]
            idempotency_key=idempotency_key,  # type: ignore[arg-type]
            claim_retained=claim_retained,  # type: ignore[arg-type]
            bound=bound,  # type: ignore[arg-type]
            owner_pid=owner_pid,  # type: ignore[arg-type]
            owner_birth=owner_birth,  # type: ignore[arg-type]
            created_at=created_at,
            updated_at=updated_at,
        )
    except (TypeError, ValueError) as exc:
        raise ProjectAdmissionSafetyError("admission reservation is invalid") from exc


def _decode_state(root: Path, value: Mapping[str, Any] | None) -> dict[str, AdmissionReservation]:
    if value is None:
        return {}
    if set(value) != {"schema_version", "project_root", "reservations"}:
        raise ProjectAdmissionSafetyError("project admission state has an invalid shape")
    if value.get("schema_version") != ADMISSION_SCHEMA_VERSION:
        raise ProjectAdmissionSafetyError("project admission state schema is unsupported")
    if value.get("project_root") != str(root):
        raise ProjectAdmissionSafetyError("project admission state belongs to another project")
    raw_reservations = value.get("reservations")
    if not isinstance(raw_reservations, Mapping):
        raise ProjectAdmissionSafetyError("project admission reservations are invalid")
    decoded: dict[str, AdmissionReservation] = {}
    for key, raw in raw_reservations.items():
        if not isinstance(key, str):
            raise ProjectAdmissionSafetyError("project admission reservation key is invalid")
        reservation = _decode_reservation(raw)
        if key != reservation.run_id:
            raise ProjectAdmissionSafetyError("project admission reservation identity is invalid")
        if key in decoded:
            raise ProjectAdmissionSafetyError("project admission contains duplicate reservations")
        decoded[key] = reservation
    return decoded


def _state_payload(root: Path, reservations: Mapping[str, AdmissionReservation]) -> bytes:
    payload = {
        "schema_version": ADMISSION_SCHEMA_VERSION,
        "project_root": str(root),
        "reservations": {
            run_id: reservation.to_dict()
            for run_id, reservation in sorted(reservations.items())
        },
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


class ProjectAdmission:
    """Reserve and reconcile one project's implementation capacity."""

    def __init__(self, project_root: Path, *, unit_manager: object | None = None) -> None:
        try:
            self._settings = ProjectSettingsService(Path(project_root))
        except ProjectSettingsError as exc:
            raise ProjectAdmissionSafetyError(_ADMISSION_SETTINGS_SAFE_MESSAGE) from exc
        self._root = self._settings.primary_root
        self._checkout_root = self._settings.checkout_root
        self._unit_manager = unit_manager

    @property
    def primary_root(self) -> Path:
        return self._root

    @property
    def state_path(self) -> Path:
        return self._root / ".aflow" / ADMISSION_STATE_FILENAME

    @property
    def lock_path(self) -> Path:
        return self._root / ".aflow" / ADMISSION_LOCK_FILENAME

    def acquire(
        self,
        run_id: str,
        *,
        plan_path: Path | None = None,
        idempotency_key: str | None = None,
        source_run_id: str | None = None,
        # Kept for compatibility with the first CP4 callers.  This value is
        # intentionally not an authority: predecessor evidence is read below
        # while the shared admission lock is held.
        predecessor_inactive_proven: bool = False,
        automatic: bool = False,
        _direct_resume_proof: object | None = None,
    ) -> AdmissionReservation:
        """Atomically reserve one slot, or replay its existing reservation."""
        valid_run_id = validate_run_id(run_id)
        direct_resume_proof: _DirectResumeAdmissionProof | None = None
        if _direct_resume_proof is not None:
            if not isinstance(_direct_resume_proof, _DirectResumeAdmissionProof):
                raise ProjectAdmissionConflict("direct resume admission proof is invalid")
            if (
                _direct_resume_proof._token is not _DIRECT_RESUME_ADMISSION_TOKEN
                or source_run_id != _direct_resume_proof.source_run_id
            ):
                raise ProjectAdmissionConflict(
                    "direct resume admission proof does not match the source"
                )
            direct_resume_proof = _direct_resume_proof
            source_run_id = direct_resume_proof.source_run_id
        elif source_run_id is not None:
            # Direct-controller provenance may use historical identities that
            # predate the canonical control-plane run-ID grammar.  Keep this
            # path contained and bounded without applying the daemon's
            # canonical validator to a read-only predecessor identity.
            source_run_id = _validate_source_provenance_id(source_run_id)
        if source_run_id == valid_run_id:
            raise ProjectAdmissionConflict("a reservation cannot restart itself")
        plan_key = (
            self._plan_key(
                plan_path, root=self._checkout_root,
                require_file=direct_resume_proof is None,
            )
            if plan_path is not None else None
        )
        self._validate_key(idempotency_key)
        with self._locked():
            reservations = self._load_locked(preserve_run_ids={valid_run_id})
            reservations = self._reconcile_locked(reservations)
            # The direct-resume marker validates provenance and permits the
            # historical source plan claim, but cannot prove current liveness.
            # Recheck ownership under this lock before either exemption.
            self._require_inactive_predecessor_locked(source_run_id)
            current = reservations.get(valid_run_id)
            if current is not None:
                self._assert_request_matches(
                    current,
                    idempotency_key=idempotency_key,
                    source_run_id=source_run_id,
                    plan_key=plan_key,
                )
                if current.state in _LIVE_RESERVATION_STATES:
                    return current

            self._require_unique_successor_locked(
                source_run_id,
                valid_run_id,
                reservations,
            )
            if plan_key is not None:
                self._require_unique_plan_claim_locked(
                    plan_key, source_run_id, valid_run_id, reservations,
                    direct_resume_proven=direct_resume_proof is not None,
                )
                try:
                    checkout_root = self._root
                    verified_roots = (self._root,)
                    if plan_key.startswith("plans/") and plan_path is not None:
                        try:
                            selected_path = Path(plan_path).resolve(
                                strict=direct_resume_proof is None
                            )
                        except (OSError, RuntimeError, ValueError) as exc:
                            raise ProjectAdmissionSafetyError(
                                "dependency plan checkout is unavailable"
                            ) from exc
                        verified_roots = self._repository_roots()
                        checkout_root = next(
                            (
                                root for root in verified_roots
                                if selected_path == root / plan_key
                            ),
                            None,
                        )
                        if checkout_root is None:
                            raise ProjectAdmissionSafetyError(
                                "dependency plan checkout is not verified"
                            )
                    PlanDependencies(self._root).require_ready(
                        plan_key, checkout_root=checkout_root,
                        evidence_roots=verified_roots,
                    )
                except PlanDependencyBlocked as exc:
                    raise ProjectPlanDependencyBlocked(str(exc)) from exc
                except PlanDependencyError as exc:
                    raise ProjectAdmissionSafetyError(str(exc)) from exc

            evidence = self._run_evidence(valid_run_id)
            snapshot = self._snapshot_locked(
                reservations,
                exclude_run_id=valid_run_id,
                extra_evidence=(valid_run_id, evidence),
            )
            if snapshot.occupied_count >= snapshot.limit:
                raise ProjectCapacityReached(
                    limit=snapshot.limit,
                    occupied=snapshot.occupied_count,
                    automatic=automatic,
                )

            # A replacement of a released reservation reuses its journal key;
            # a new logical run needs one additional entry.  Make that room
            # while the admission lock is still held, before _save_locked can
            # enforce the bounded history limit.
            reservations = self._compact_released(
                reservations,
                preserve_run_ids={valid_run_id},
                reserve_slots=0 if current is not None else 1,
            )

            now = _now()
            reservation = AdmissionReservation(
                run_id=valid_run_id,
                nonce=_new_nonce(),
                state="reserved",
                source_run_id=source_run_id,
                plan_key=plan_key,
                idempotency_key=idempotency_key,
                claim_retained=(
                    current.claim_retained if current is not None else evidence.claim_retained
                ),
                bound=evidence.state != "absent",
                owner_pid=os.getpid(),
                owner_birth=process_birth_identity(os.getpid()),
                created_at=now,
                updated_at=now,
            )
            reservations[valid_run_id] = reservation
            self._save_locked(reservations)
            return reservation

    def ensure(
        self,
        run_id: str,
        *,
        plan_path: Path | None = None,
        idempotency_key: str | None = None,
        source_run_id: str | None = None,
        # See acquire(): canonical evidence, not this compatibility argument,
        # decides whether a resume predecessor is safe to replace.
        predecessor_inactive_proven: bool = False,
        automatic: bool = False,
    ) -> AdmissionReservation:
        """Return a live reservation, reacquiring a released plan claim."""
        valid_run_id = validate_run_id(run_id)
        if source_run_id is not None:
            source_run_id = validate_run_id(source_run_id)
            if source_run_id == valid_run_id:
                raise ProjectAdmissionConflict("a reservation cannot restart itself")
        plan_key = self._plan_key(plan_path, root=self._checkout_root, require_file=True) if plan_path is not None else None
        with self._locked():
            reservations = self._load_locked(preserve_run_ids={valid_run_id})
            reservations = self._reconcile_locked(reservations)
            self._require_inactive_predecessor_locked(source_run_id)
            current = reservations.get(valid_run_id)
            if current is not None and current.state in _LIVE_RESERVATION_STATES:
                self._assert_request_matches(
                    current,
                    idempotency_key=idempotency_key,
                    source_run_id=source_run_id,
                    plan_key=plan_key,
                )
                return current
        return self.acquire(
            valid_run_id,
            plan_path=plan_path,
            idempotency_key=idempotency_key,
            source_run_id=source_run_id,
            automatic=automatic,
        )

    def _require_unique_successor_locked(
        self,
        source_run_id: str | None,
        requested_run_id: str,
        reservations: Mapping[str, AdmissionReservation],
    ) -> None:
        """Keep one unresolved continuation owner for each predecessor."""
        if source_run_id is None:
            return
        evidence = self._all_run_evidence()
        for run_id, reservation in reservations.items():
            if run_id == requested_run_id or reservation.source_run_id != source_run_id:
                continue
            observed = evidence.get(run_id, _RunEvidence("absent"))
            if (
                reservation.state in _LIVE_RESERVATION_STATES
                or reservation.claim_retained
                or observed.state not in {"absent", "inactive"}
                or (reservation.bound and observed.state == "absent")
            ):
                raise ProjectAdmissionConflict(
                    "resume predecessor already has an unresolved successor"
                )

        # Journal entries can be compacted after a confirmed release, and
        # older runs may never have had one. Recover their lineage from the
        # canonical launch, startup, and controller artifacts before admitting
        # a distinct successor.
        for root in self._repository_roots():
            repository = RunRepository(root)
            try:
                run_ids, legacy_run_ids = self._scan_repository_run_ids(root)
                startup_root = self._contained_existing_path(
                    root, root / ".aflow" / "start-requests"
                )
                if startup_root is not None:
                    if not startup_root.is_dir():
                        raise RepositoryError("startup directory is not a directory")
                    for path in startup_root.glob("*.json"):
                        run_ids.add(validate_run_id(path.stem))
                for run_id in run_ids:
                    if run_id == requested_run_id:
                        continue
                    lineage: set[str] = set()
                    if run_id not in legacy_run_ids:
                        manifest = repository.get_launch_manifest(run_id)
                        if manifest is not None and manifest.restarted_from_run_id:
                            lineage.add(manifest.restarted_from_run_id)
                        startup = repository._startup_record(run_id)
                        if startup.get("resumed_from_run_id") is not None:
                            lineage.add(_validate_source_provenance_id(
                                startup["resumed_from_run_id"]
                            ))
                    metadata_path = self._contained_existing_path(
                        root, root / ".aflow" / "runs" / run_id / "run.json"
                    )
                    if metadata_path is not None:
                        metadata = _read_json_file(metadata_path)
                        if metadata is not None and metadata.get("resumed_from_run_id") is not None:
                            lineage.add(_validate_source_provenance_id(
                                metadata["resumed_from_run_id"]
                            ))
                    if source_run_id not in lineage:
                        continue
                    observed = evidence.get(run_id, _RunEvidence("uncertain"))
                    if observed.state != "inactive" or observed.claim_retained:
                        raise ProjectAdmissionConflict(
                            "resume predecessor already has an unresolved successor"
                        )
            except (RepositoryError, RunIdentityError, OSError) as exc:
                raise ProjectAdmissionSafetyError(
                    "successor lineage evidence is unavailable"
                ) from exc

    def _plan_key(
        self, plan_path: Path | str, *, root: Path, require_file: bool = False,
        project_roots: tuple[Path, ...] | None = None,
    ) -> str:
        """Identify a verified plan within this project or at an external path."""
        if not isinstance(plan_path, (str, Path)) or not Path(plan_path).is_absolute():
            raise ProjectAdmissionSafetyError("admission plan path is invalid")
        try:
            resolved = Path(plan_path).resolve(strict=require_file)
            if require_file and not resolved.is_file():
                raise ProjectAdmissionSafetyError("admission plan is not a file")
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProjectAdmissionSafetyError(
                "admission plan is unavailable"
            ) from exc
        key = ""
        for candidate in (root, *(project_roots or self._repository_roots())):
            try:
                key = resolved.relative_to(candidate.resolve()).as_posix()
                break
            except ValueError:
                continue
        if not key:
            key = "@external/" + hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
        if not key or key == "." or len(key) > 4_096:
            raise ProjectAdmissionSafetyError("admission plan identity is invalid")
        return key

    def _require_unique_plan_claim_locked(
        self,
        plan_key: str,
        source_run_id: str | None,
        requested_run_id: str | None,
        reservations: Mapping[str, AdmissionReservation],
        *,
        direct_resume_proven: bool = False,
    ) -> None:
        """Admit at most one unresolved owner of a project-relative plan."""
        evidence = self._all_run_evidence()
        artifact_claims: list[tuple[str, str]] = []
        publication_claims: set[str] = set()
        lineage_parents: dict[str, str] = {}
        project_roots = self._repository_roots()
        try:
            for root in project_roots:
                repository = RunRepository(root)
                run_ids, legacy_run_ids = self._scan_repository_run_ids(root)
                startup_root = self._contained_existing_path(
                    root, root / ".aflow" / "start-requests"
                )
                if startup_root is not None:
                    if not startup_root.is_dir():
                        raise RepositoryError("startup directory is not a directory")
                    for path in startup_root.glob("*.json"):
                        run_ids.add(validate_run_id(path.stem))
                for run_id in run_ids:
                    manifest = (
                        repository.get_launch_manifest(run_id)
                        if run_id not in legacy_run_ids else None
                    )
                    startup = (
                        repository._startup_record(run_id)
                        if run_id not in legacy_run_ids else {}
                    )
                    metadata_path = self._contained_existing_path(
                        root, root / ".aflow" / "runs" / run_id / "run.json"
                    )
                    metadata = _read_json_file(metadata_path) if metadata_path else None
                    if isinstance(metadata, Mapping) and isinstance(
                        metadata.get("resumed_from_run_id"), str
                    ):
                        lineage_parents[run_id] = _validate_source_provenance_id(
                            metadata["resumed_from_run_id"]
                        )
                    if isinstance(metadata, Mapping) and (
                        metadata.get("failure_kind") == "completion_publication"
                        and metadata.get("status") != "completed"
                    ):
                        publication_claims.add(run_id)
                    request = startup.get("request")
                    prepared = startup.get("prepared")
                    raw_plan = manifest.plan_path if manifest is not None else None
                    if raw_plan is None and isinstance(request, Mapping):
                        raw_plan = request.get("plan_path")
                    if raw_plan is None and isinstance(prepared, Mapping):
                        raw_plan = prepared.get("plan_path")
                    if raw_plan is None and isinstance(metadata, Mapping):
                        raw_plan = metadata.get("original_plan_path") or metadata.get("plan_path")
                    if raw_plan is not None:
                        artifact_claims.append((
                            run_id,
                            self._plan_key(raw_plan, root=root, project_roots=project_roots),
                        ))
        except (RepositoryError, RunIdentityError, OSError) as exc:
            raise ProjectAdmissionSafetyError(
                "plan ownership evidence is unavailable"
            ) from exc

        lineage_ancestors: set[str] = set()
        if direct_resume_proven and source_run_id is not None:
            predecessor = source_run_id
            while predecessor not in lineage_ancestors:
                lineage_ancestors.add(predecessor)
                predecessor = lineage_parents.get(predecessor)
                if predecessor is None:
                    break
            source_keys = {
                key for run_id, key in artifact_claims if run_id == source_run_id
            }
            if source_keys and plan_key not in source_keys:
                raise ProjectAdmissionConflict(
                    "resume plan does not match its validated source"
                )

        for run_id, reservation in reservations.items():
            if run_id == requested_run_id or reservation.plan_key != plan_key:
                continue
            if run_id == source_run_id and direct_resume_proven:
                continue
            observed = evidence.get(run_id, _RunEvidence("absent"))
            if (
                reservation.state in _LIVE_RESERVATION_STATES
                or (reservation.claim_retained and (
                    observed.state != "inactive" or observed.claim_retained
                ))
                or (reservation.bound and observed.state == "absent")
            ):
                raise ProjectPlanClaimConflict(
                    "plan already has an unresolved run claim"
                )

        for run_id, key in artifact_claims:
            if run_id == requested_run_id or key != plan_key:
                continue
            if run_id == source_run_id and direct_resume_proven:
                continue
            observed = evidence.get(run_id, _RunEvidence("uncertain"))
            if (
                run_id in lineage_ancestors
                and run_id in publication_claims
                and observed.state == "inactive"
            ):
                continue
            if (
                run_id in publication_claims
                or observed.state != "inactive"
                or observed.claim_retained
            ):
                raise ProjectPlanClaimConflict(
                    "plan already has an unresolved run claim"
                )

    def _require_inactive_predecessor_locked(self, source_run_id: str | None) -> None:
        """Require authoritative inactive evidence for every resume admission."""
        if source_run_id is None:
            return
        evidence = self._run_evidence(source_run_id)
        if (
            evidence.state != "inactive"
            or not self._authoritative_inactive_predecessor_locked(source_run_id)
        ):
            raise ProjectAdmissionConflict(
                "resume predecessor inactivity is not proven "
                f"for {source_run_id} ({evidence.state})"
            )

    def predecessor_inactive_for_preview(self, source_run_id: str) -> bool:
        """Project the admission inactivity check without locking or writing state.

        This is only a hint to callers. Reservation repeats the same check
        under the project lock before permitting a successor.
        """
        try:
            self._require_inactive_predecessor_locked(validate_run_id(source_run_id))
        except ProjectAdmissionConflict:
            return False
        return True

    @contextmanager
    def plan_lifecycle_guard(
        self, plan_path: Path, *, source_run_id: str | None = None,
        require_file: bool = True,
    ) -> Iterator[None]:
        """Hold admission while classifying an unclaimed original plan.

        A terminal source can retain historical plan artifacts, but it must
        first be confirmed inactive. Other live or uncertain owners prevent a
        lifecycle move until their ownership is resolved.
        """
        source = (
            _validate_source_provenance_id(source_run_id)
            if source_run_id is not None else None
        )
        plan_key = self._plan_key(
            plan_path, root=self._checkout_root, require_file=require_file
        )
        with self._locked():
            reservations = self._reconcile_locked(self._load_locked())
            self._require_inactive_predecessor_locked(source)
            self._require_unique_plan_claim_locked(
                plan_key, source, None, reservations,
                direct_resume_proven=source is not None,
            )
            yield

    def _authoritative_inactive_predecessor_locked(self, source_run_id: str) -> bool:
        """Accept only current, identity-matched inactivity evidence.

        Reconciliation observations are deliberately not an authority here:
        missing or failed-unit projections are ``needs_attention`` and an old
        observation can be followed by a new launch attempt.  The current
        repository projection must instead contain a terminal controller
        record, a held startup claim, a confirmed preparation failure, or a
        nonce-bound worker receipt for the exact canonical unit.
        """
        expected_unit = f"aflow-run-{source_run_id}.service"
        for root in self._repository_roots():
            repository = RunRepository(root)
            try:
                canonical_source_run_id = validate_run_id(source_run_id)
            except RunIdentityError:
                # Generic safe legacy identities are read from their
                # contained run directory only.  Do not route them through
                # the control-plane repository's canonical/unit namespace.
                legacy_run_dir = self._contained_existing_path(
                    root,
                    root / ".aflow" / "runs" / source_run_id,
                )
                if legacy_run_dir is None:
                    continue
                try:
                    legacy_evidence = self._legacy_directory_evidence(
                        root, source_run_id
                    )
                except RepositoryError as exc:
                    raise ProjectAdmissionSafetyError(
                        "resume predecessor evidence is unavailable"
                    ) from exc
                if legacy_evidence.state == "inactive":
                    return True
                continue
            try:
                status = repository.get_run_status(
                    canonical_source_run_id,
                    include_progress=False,
                )
            except RepositoryNotFoundError:
                continue
            except RepositoryError as exc:
                raise ProjectAdmissionSafetyError(
                    "resume predecessor evidence is unavailable"
                ) from exc
            status_evidence = (
                status.evidence if isinstance(status.evidence, Mapping) else {}
            )
            if status.ownership == "legacy" and (
                status_evidence.get("recorded_status")
                in {"completed", "failed", "interrupted", "owner_stopped"}
            ):
                # Older direct-controller runs have no immutable launch
                # manifest or workflow unit.  Their current terminal run.json
                # record is nevertheless identity-matched by the repository
                # run id and remains the compatibility authority for resume.
                return True
            if status.ownership != "control_plane" or status.unit_name != expected_unit:
                continue
            try:
                manifest = repository.get_launch_manifest(source_run_id)
            except RepositoryError as exc:
                raise ProjectAdmissionSafetyError(
                    "resume predecessor evidence is unavailable"
                ) from exc
            try:
                manifest_root = Path(manifest.project_root).resolve() if manifest else None
            except (OSError, RuntimeError, ValueError) as exc:
                raise ProjectAdmissionSafetyError(
                    "resume predecessor evidence is unavailable"
                ) from exc
            if (
                manifest is None
                or manifest.run_id != source_run_id
                or manifest.intended_unit != expected_unit
                or manifest_root != root.resolve()
            ):
                continue
            evidence = status_evidence
            if status.status == "owner_stopped" and status.launch_phase == "owner_stopped":
                return True
            if (
                status.status in {"completed", "failed", "interrupted"}
                and evidence.get("controller_terminal") is True
            ):
                # The controller's terminal run record is authoritative even
                # when the independent launch-phase marker still describes
                # the last dispatch boundary (for example after a worker
                # exits before it can publish the terminal phase).
                return True
            worker = evidence.get("worker")
            if isinstance(worker, Mapping) and confirmed_inactive(worker):
                return True
            if (
                status.status == "awaiting_startup_answer"
                and evidence.get("startup_question_valid") is True
                and evidence.get("preparation_active") is not True
            ):
                return True
            failure = evidence.get("startup_failure")
            if (
                isinstance(failure, Mapping)
                and failure.get("stage") == "preparation"
                and evidence.get("has_run_metadata") is not True
                and status.launch_phase in {None, "manifest_only"}
            ):
                return True
        return False

    def bind(self, run_id: str, nonce: str) -> AdmissionReservation:
        """Bind a pre-launch reservation to its canonical launch evidence."""
        return self._transition(run_id, nonce, state=None, bound=True)

    def mark_starting(self, run_id: str, nonce: str) -> AdmissionReservation:
        """Record that the exact launch boundary is being entered."""
        return self._transition(run_id, nonce, state="starting", bound=True)

    def consume(self, run_id: str, nonce: str) -> AdmissionReservation:
        """Consume the parent's reservation at daemon-worker/controller boot."""
        return self._transition(run_id, nonce, state="active", bound=True)

    def release(
        self,
        run_id: str,
        nonce: str,
        *,
        reason: str = "",
        claim_retained: bool = False,
    ) -> AdmissionReservation:
        """Release only when canonical evidence proves the logical run inactive."""
        if reason and len(reason) > 256:
            raise ProjectAdmissionSafetyError("admission release reason is too long")
        valid_run_id = validate_run_id(run_id)
        with self._locked():
            reservations = self._load_locked(preserve_run_ids={valid_run_id})
            current = self._require_nonce(reservations, valid_run_id, nonce)
            if current.state == "released":
                return current
            evidence = self._run_evidence(valid_run_id)
            if not current.bound and evidence.state in {
                "reserved",
                "starting",
                "active",
                "uncertain",
            }:
                # A pre-bind caller may have failed after publishing part of
                # the canonical identity.  Do not turn that evidence into an
                # unmanaged run by releasing only the supplemental claim.
                raise ProjectAdmissionSafetyError(
                    "cannot release an unbound reservation while run evidence exists"
                )
            if current.bound and evidence.state not in {"inactive", "absent"}:
                raise ProjectAdmissionSafetyError(
                    "cannot release a reservation without proven inactive ownership"
                )
            if current.bound and evidence.state == "absent":
                # A bound run with no current artifact is an identity gap, not
                # proof that a worker stopped.  Keep the slot until a terminal
                # receipt or controller record is visible.
                raise ProjectAdmissionSafetyError(
                    "cannot release a reservation while run ownership is unknown"
                )
            now = _now()
            released = replace(
                current,
                state="released",
                claim_retained=current.claim_retained or claim_retained,
                updated_at=now,
            )
            reservations[valid_run_id] = released
            self._save_locked(reservations)
            return released

    def reconcile(self) -> AdmissionSnapshot:
        """Reconcile reservations and canonical runs under the project lock."""
        with self._locked():
            reservations = self._load_locked()
            reservations = self._reconcile_locked(reservations)
            return self._snapshot_locked(reservations)

    def snapshot(self) -> AdmissionSnapshot:
        """Return current capacity after one fail-closed evidence pass."""
        return self.reconcile()

    def reservation(self, run_id: str) -> AdmissionReservation | None:
        """Read one reservation after reconciliation, without exposing secrets."""
        valid_run_id = validate_run_id(run_id)
        with self._locked():
            reservations = self._load_locked(preserve_run_ids={valid_run_id})
            reservations = self._reconcile_locked(reservations)
            return reservations.get(valid_run_id)

    def _validate_key(self, value: str | None) -> None:
        if value is not None and (not isinstance(value, str) or not value or len(value) > 4_096):
            raise ProjectAdmissionSafetyError("admission idempotency key is invalid")

    def _assert_request_matches(
        self,
        reservation: AdmissionReservation,
        *,
        idempotency_key: str | None,
        source_run_id: str | None,
        plan_key: str | None,
    ) -> None:
        if (
            reservation.idempotency_key != idempotency_key
            or reservation.source_run_id != source_run_id
            or (reservation.plan_key is not None and reservation.plan_key != plan_key)
        ):
            raise ProjectAdmissionConflict(
                "logical run is already reserved for a different launch request"
            )

    @contextmanager
    def _locked(self) -> Iterator[None]:
        directory = _safe_project_directory(self._root)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(directory / ADMISSION_LOCK_FILENAME, flags, 0o600)
        except OSError as exc:
            raise ProjectAdmissionSafetyError("project admission lock is unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ProjectAdmissionSafetyError("project admission lock must be a regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _load_locked(
        self,
        *,
        preserve_run_ids: set[str] | frozenset[str] = frozenset(),
    ) -> dict[str, AdmissionReservation]:
        value = _read_json_file(self.state_path)
        reservations = _decode_state(self._root, value)
        compacted = self._compact_released(
            reservations,
            preserve_run_ids=preserve_run_ids,
        )
        if compacted != reservations:
            self._save_locked(compacted)
        return compacted

    @staticmethod
    def _compact_released(
        reservations: dict[str, AdmissionReservation],
        *,
        preserve_run_ids: set[str] | frozenset[str],
        reserve_slots: int = 0,
    ) -> dict[str, AdmissionReservation]:
        """Drop disposable released history before adding a reservation.

        Live ownership and held plan claims are durable capacity/replay
        identities.  The requested run id is also retained for the duration
        of the locked operation so a retry can validate or replace the exact
        reservation it is replaying.  Other released records are history and
        can be removed once the bounded journal needs room.
        """
        if reserve_slots not in {0, 1}:
            raise ProjectAdmissionSafetyError("invalid admission compaction request")
        target_size = MAX_RESERVATIONS - reserve_slots
        if target_size < 0:
            raise ProjectAdmissionSafetyError("project admission reservation limit is invalid")
        if len(reservations) <= target_size:
            return reservations

        retained = {
            run_id: reservation
            for run_id, reservation in reservations.items()
            if (
                reservation.state != "released"
                or reservation.claim_retained
                or run_id in preserve_run_ids
            )
        }
        if len(retained) > target_size:
            raise ProjectAdmissionSafetyError("project admission reservation history is full")
        return retained

    def _save_locked(self, reservations: Mapping[str, AdmissionReservation]) -> None:
        if len(reservations) > MAX_RESERVATIONS:
            raise ProjectAdmissionSafetyError("project admission reservation history is full")
        path = self.state_path
        if path.is_symlink():
            raise ProjectAdmissionSafetyError("project admission state must not be a symlink")
        payload = _state_payload(self._root, reservations)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            raise ProjectAdmissionSafetyError("project admission state write failed") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _transition(
        self,
        run_id: str,
        nonce: str,
        *,
        state: ReservationState | None,
        bound: bool,
    ) -> AdmissionReservation:
        valid_run_id = validate_run_id(run_id)
        with self._locked():
            reservations = self._load_locked(preserve_run_ids={valid_run_id})
            current = self._require_nonce(reservations, valid_run_id, nonce)
            if current.state == "released":
                raise ProjectAdmissionConflict("admission reservation has already been released")
            updated = replace(
                current,
                state=current.state if state is None else state,
                bound=current.bound or bound,
                updated_at=_now(),
            )
            reservations[valid_run_id] = updated
            self._save_locked(reservations)
            return updated

    def _require_nonce(
        self,
        reservations: Mapping[str, AdmissionReservation],
        run_id: str,
        nonce: str,
    ) -> AdmissionReservation:
        current = reservations.get(run_id)
        if current is None:
            raise ProjectAdmissionConflict("admission reservation does not exist")
        if current.nonce != nonce:
            raise ProjectAdmissionConflict("admission reservation nonce does not match")
        return current

    def _reconcile_locked(
        self, reservations: dict[str, AdmissionReservation]
    ) -> dict[str, AdmissionReservation]:
        changed = False
        for run_id, reservation in tuple(reservations.items()):
            if reservation.state == "released":
                continue
            evidence = self._run_evidence(run_id)
            if evidence.state == "inactive":
                if (
                    reservation.state == "reserved"
                    and evidence.claim_retained
                    and reservation.claim_retained
                    and not self._unbound_owner_is_definitely_inactive(reservation)
                    and reservation.owner_pid is not None
                ):
                    # An answer/launch caller has deliberately reacquired a
                    # held plan claim.  Keep it through that short boundary;
                    # an owner crash is still recoverable because its birth
                    # identity will no longer be present on the next process.
                    updated = replace(
                        reservation,
                        claim_retained=True,
                        updated_at=reservation.updated_at,
                    )
                else:
                    updated = replace(
                        reservation,
                        state="released",
                        claim_retained=reservation.claim_retained or evidence.claim_retained,
                        updated_at=_now(),
                    )
            elif (
                evidence.state == "absent"
                and not reservation.bound
                and self._unbound_owner_is_definitely_inactive(reservation)
            ):
                # The reservation never became a logical run.  This is the
                # only crash recovery path that can release without a terminal
                # unit/controller receipt.
                updated = replace(
                    reservation,
                    state="released",
                    updated_at=_now(),
                )
            else:
                target_state: ReservationState
                if evidence.state in {"reserved", "starting", "active", "uncertain"}:
                    target_state = evidence.state
                elif reservation.state == "reserved":
                    target_state = "reserved"
                else:
                    target_state = "uncertain"
                updated = replace(
                    reservation,
                    state=target_state,
                    updated_at=reservation.updated_at if target_state == reservation.state else _now(),
                )
            if updated != reservation:
                reservations[run_id] = updated
                changed = True
        if changed:
            self._save_locked(reservations)
        return reservations

    @staticmethod
    def _unbound_owner_is_definitely_inactive(
        reservation: AdmissionReservation,
    ) -> bool:
        """Prove a pre-manifest owner died without a timeout/PID gap.

        Birth identities are comparable only within the same observation
        scheme.  A source change is an observation gap, so liveness is the
        only remaining evidence that can release the reservation.
        """
        if reservation.owner_pid is None:
            return False
        observed_birth = process_birth_identity(reservation.owner_pid)
        if observed_birth is not None and reservation.owner_birth is not None:
            observed_scheme = _birth_identity_scheme(observed_birth)
            stored_scheme = _birth_identity_scheme(reservation.owner_birth)
            if observed_scheme is not None and observed_scheme == stored_scheme:
                return observed_birth != reservation.owner_birth
        # Missing, different, or unknown schemes cannot prove PID reuse.  A
        # positive absence result is still sufficient to release the claim.
        return process_liveness(reservation.owner_pid) == "absent"

    def _read_settings(self):
        """Read scheduling settings behind the admission transport boundary."""
        try:
            return self._settings.read()
        except ProjectSettingsError as exc:
            raise ProjectAdmissionSafetyError(_ADMISSION_SETTINGS_SAFE_MESSAGE) from exc

    def _snapshot_locked(
        self,
        reservations: Mapping[str, AdmissionReservation],
        *,
        exclude_run_id: str | None = None,
        extra_evidence: tuple[str, _RunEvidence] | None = None,
    ) -> AdmissionSnapshot:
        counts = {state: 0 for state in ("reserved", "starting", "active", "uncertain")}
        claim_retained: list[str] = []
        for run_id, reservation in reservations.items():
            if run_id == exclude_run_id or reservation.state == "released":
                if reservation.claim_retained and run_id != exclude_run_id:
                    claim_retained.append(run_id)
                continue
            counts[reservation.state] += 1
            if reservation.claim_retained:
                claim_retained.append(run_id)

        occupied_run_ids = {
            run_id
            for run_id, reservation in reservations.items()
            if reservation.state in _LIVE_RESERVATION_STATES
        }
        if exclude_run_id is not None:
            occupied_run_ids.discard(exclude_run_id)
        all_evidence = self._all_run_evidence()
        if extra_evidence is not None:
            all_evidence[extra_evidence[0]] = extra_evidence[1]
        unmanaged: list[str] = []
        for run_id, evidence in all_evidence.items():
            if run_id in occupied_run_ids or run_id == exclude_run_id:
                continue
            if evidence.state in {"reserved", "starting", "active", "uncertain"}:
                unmanaged.append(run_id)
        unmanaged = sorted(set(unmanaged))
        unmanaged_counts = {
            state: sum(1 for run_id in unmanaged if all_evidence[run_id].state == state)
            for state in ("reserved", "starting", "active", "uncertain")
        }
        return AdmissionSnapshot(
            limit=self._read_settings().max_concurrent_implementations,
            reserved_count=counts["reserved"] + unmanaged_counts["reserved"],
            starting_count=counts["starting"] + unmanaged_counts["starting"],
            active_count=counts["active"] + unmanaged_counts["active"],
            uncertain_count=counts["uncertain"] + unmanaged_counts["uncertain"],
            unmanaged_run_ids=tuple(unmanaged),
            claim_retained_run_ids=tuple(sorted(set(claim_retained))),
        )

    def _all_run_evidence(self) -> dict[str, _RunEvidence]:
        evidence: dict[str, _RunEvidence] = {}
        for root in self._repository_roots():
            repository = RunRepository(root)
            try:
                run_ids, legacy_run_ids = self._scan_repository_run_ids(root)
                if legacy_run_ids:
                    for run_id in sorted(run_ids - legacy_run_ids):
                        status = repository.get_run_status(
                            run_id,
                            include_progress=False,
                        )
                        candidate = self._classify_status(status)
                        prior = evidence.get(status.run_id)
                        if prior is None or self._evidence_rank(candidate) > self._evidence_rank(prior):
                            evidence[status.run_id] = candidate
                    for run_id in sorted(legacy_run_ids):
                        candidate = self._legacy_directory_evidence(root, run_id)
                        prior = evidence.get(run_id)
                        if prior is None or self._evidence_rank(candidate) > self._evidence_rank(prior):
                            evidence[run_id] = candidate
                else:
                    cursor: str | None = None
                    while True:
                        page = repository.list_runs(
                            limit=1_000,
                            cursor=cursor,
                            include_progress=False,
                        )
                        for status in page.runs:
                            candidate = self._classify_status(status)
                            prior = evidence.get(status.run_id)
                            if prior is None or self._evidence_rank(candidate) > self._evidence_rank(prior):
                                evidence[status.run_id] = candidate
                        if page.next_cursor is None:
                            break
                        cursor = page.next_cursor
            except RepositoryError as exc:
                raise ProjectAdmissionSafetyError(str(exc)) from exc
        return evidence

    @staticmethod
    def _contained_existing_path(root: Path, path: Path) -> Path | None:
        """Resolve one repository artifact without following it outside root."""
        if not path.exists() and not path.is_symlink():
            return None
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root.resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            raise RepositoryError("repository artifact escapes project root") from exc
        return resolved

    def _scan_repository_run_ids(
        self, root: Path
    ) -> tuple[set[str], set[str]]:
        """Find run identities, allowing only safe legacy directory names."""
        root = Path(root).resolve()
        run_ids: set[str] = set()
        legacy_run_ids: set[str] = set()

        runs_root = self._contained_existing_path(root, root / ".aflow" / "runs")
        if runs_root is not None and runs_root.is_dir():
            try:
                paths = tuple(runs_root.iterdir())
            except OSError as exc:
                raise RepositoryError("runs directory is unreadable") from exc
            for path in paths:
                resolved = self._contained_existing_path(root, path)
                if resolved is None or not resolved.is_dir():
                    continue
                try:
                    run_id = validate_run_id(path.name)
                except RunIdentityError:
                    try:
                        run_id = _validate_source_provenance_id(path.name)
                    except ProjectAdmissionSafetyError as legacy_exc:
                        raise RepositoryError(
                            "runs directory contains an invalid identity"
                        ) from legacy_exc
                    legacy_run_ids.add(run_id)
                run_ids.add(run_id)

        launches_root = self._contained_existing_path(
            root, root / ".aflow" / "launches"
        )
        if launches_root is not None and launches_root.is_dir():
            try:
                paths = tuple(launches_root.glob("*.json"))
            except OSError as exc:
                raise RepositoryError("launch directory is unreadable") from exc
            for path in paths:
                if path.name.endswith(".state.json"):
                    continue
                resolved = self._contained_existing_path(root, path)
                if resolved is None or not resolved.is_file():
                    continue
                try:
                    run_ids.add(validate_run_id(path.stem))
                except RunIdentityError as exc:
                    raise RepositoryError(
                        "launch directory contains an invalid identity"
                    ) from exc

        return run_ids, legacy_run_ids

    def _legacy_directory_evidence(self, root: Path, run_id: str) -> _RunEvidence:
        """Project admission evidence for a safe, direct-controller run dir."""
        run_dir = self._contained_existing_path(
            root, root / ".aflow" / "runs" / run_id
        )
        if run_dir is None or not run_dir.is_dir():
            raise RepositoryError("legacy run directory is unavailable")

        metadata_path = run_dir / "run.json"
        if not metadata_path.exists():
            return _RunEvidence("uncertain")
        if metadata_path.is_symlink():
            raise RepositoryError("run metadata may not be a symlink")
        try:
            if metadata_path.stat().st_size > MAX_READ_ARTIFACT_BYTES:
                raise RepositoryError("run metadata exceeds the bounded read limit")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except RepositoryError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RepositoryError("run metadata is unreadable") from exc
        if not isinstance(metadata, Mapping):
            raise RepositoryError("run metadata is not an object")
        if metadata.get("status") in {
            "completed",
            "failed",
            "interrupted",
            "owner_stopped",
        }:
            return _RunEvidence("inactive")
        return _RunEvidence("uncertain")

    def _repository_roots(self) -> tuple[Path, ...]:
        """Find the primary checkout and verified linked worktree roots."""
        roots: list[Path] = [self._root]
        if self._checkout_root != self._root:
            roots.append(self._checkout_root)
        # Use the validated checkout as the Git command root.  In a linked
        # worktree, or when the primary checkout was created with
        # --separate-git-dir, .git is a regular file rather than a directory;
        # Git can still enumerate the complete linked-worktree set from that
        # checkout.
        git_entry = self._checkout_root / ".git"
        if git_entry.is_dir() or git_entry.is_file():
            try:
                completed = subprocess.run(
                    (
                        "git",
                        "-C",
                        str(self._checkout_root),
                        "--no-optional-locks",
                        "worktree",
                        "list",
                        "--porcelain",
                    ),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
                raise ProjectAdmissionSafetyError(
                    "Git worktree identity is unavailable"
                ) from exc
            if completed.returncode != 0:
                raise ProjectAdmissionSafetyError("Git worktree identity is invalid")
            for line in completed.stdout.splitlines():
                if not line.startswith("worktree "):
                    continue
                candidate = Path(line.removeprefix("worktree ")).expanduser()
                if not candidate.is_dir() or candidate.is_symlink():
                    continue
                try:
                    identity = resolve_project_identity(candidate)
                except ProjectSettingsError as exc:
                    raise ProjectAdmissionSafetyError(
                        "Git worktree identity is invalid"
                    ) from exc
                if identity.primary_root == self._root and identity.checkout_root not in roots:
                    roots.append(identity.checkout_root)
                if len(roots) >= 256:
                    raise ProjectAdmissionSafetyError("too many Git worktrees for admission")
        return tuple(roots)

    @staticmethod
    def _evidence_rank(evidence: _RunEvidence) -> int:
        return {
            "absent": 0,
            "inactive": 1,
            "reserved": 2,
            "starting": 3,
            "uncertain": 4,
            "active": 5,
        }[evidence.state]

    def _run_evidence(self, run_id: str) -> _RunEvidence:
        return self._all_run_evidence().get(run_id, _RunEvidence("absent"))

    def _classify_status(self, status: Any) -> _RunEvidence:
        evidence = status.evidence if isinstance(status.evidence, Mapping) else {}
        unit_active = evidence.get("unit_active")
        unit = None
        if self._unit_manager is not None and status.unit_name:
            try:
                unit = self._unit_manager.get(status.unit_name)
            except Exception as exc:
                raise ProjectAdmissionSafetyError(
                    "workflow unit evidence is unavailable"
                ) from exc
            if unit is not None and getattr(unit, "name", None) != status.unit_name:
                raise ProjectAdmissionSafetyError("workflow unit identity is ambiguous")
            if unit is not None:
                unit_active = getattr(unit, "is_active", None)

        if unit_active is True or evidence.get("preparation_active") is True:
            return _RunEvidence("active")
        if status.status == "awaiting_startup_answer" and evidence.get(
            "startup_question_valid"
        ):
            # A question with no active preparation owner is a held plan claim,
            # not an implementation occupying a slot.  A currently confirmed
            # owner remains active and was handled above.
            if evidence.get("preparation_active") is not True:
                return _RunEvidence("inactive", claim_retained=True)
            return _RunEvidence("starting")
        if status.status == "owner_stopped" or evidence.get("controller_terminal") is True:
            return _RunEvidence("inactive")
        failure = evidence.get("startup_failure")
        if isinstance(failure, Mapping) and (
            failure.get("stage") == "preparation"
            and evidence.get("has_run_metadata") is not True
        ):
            # A preparation failure is authoritative only when no execution
            # boundary was entered.  Unit-start failures can be ambiguous
            # after dispatch or timeout and therefore retain the reservation.
            return _RunEvidence("inactive")
        worker = evidence.get("worker")
        if isinstance(worker, Mapping) and confirmed_inactive(worker):
            return _RunEvidence("inactive")
        if status.ownership == "legacy" and (
            status.status in {"completed", "failed", "interrupted"}
            or evidence.get("recorded_status") in {
                "completed",
                "failed",
                "interrupted",
                "owner_stopped",
            }
        ):
            return _RunEvidence("inactive")
        if status.status == "manifest_only" and status.launch_phase in {None, "manifest_only"}:
            return _RunEvidence("reserved")
        if status.launch_phase == "launch_requested" or evidence.get("startup_state") == "preparing":
            return _RunEvidence("starting")
        if status.status in {
            "running",
            "paused",
            "waiting_for_input",
            "waiting_for_valid_override",
        } or status.launch_phase in {"launch_started", "unit_started"}:
            return _RunEvidence("uncertain")
        # A non-terminal needs-attention state can be a crashed launch or a
        # stale owner.  It is intentionally charged until explicit terminal
        # evidence or a new startup question proves it inactive.
        if status.status == "needs_attention":
            return _RunEvidence("uncertain")
        return _RunEvidence("uncertain")
