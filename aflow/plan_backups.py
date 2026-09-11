"""Durable, bounded provenance records for plan backup bodies.

Backup bodies intentionally keep their established names and deduplication
rules.  This module stores only the metadata needed to explain one selected
body; it never stores prompts, environment values, or provider output.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping
from uuid import uuid4


PROVENANCE_SCHEMA_VERSION = 1
PROVENANCE_DIRECTORY_NAME = ".provenance"
PLAN_IDENTITY_DIRECTORY_NAME = ".owners"
SUPPORTED_BACKUP_EVENTS = frozenset(
    {
        "startup_preparation",
        "workflow_start",
        "resume_start",
        "before_followup_turn",
        "ready_promotion",
    }
)
SUPPORTED_BACKUP_KINDS = frozenset({"snapshot", "follow_up"})
_REQUIRED_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "backup_filename",
        "content_sha256",
        "kind",
        "source_plan_path",
        "original_plan_path",
        "historical_origin",
        "first_capture_timestamp",
        "first_capture_event",
        "first_run_id",
        "baseline_status",
        "baseline_reference",
        "capture_references",
    }
)
_OPTIONAL_RECORD_KEYS = frozenset(
    {
        "path_aliases",
        "original_plan_aliases",
        "original_plan_identity_ids",
        "baseline_plan_identity_id",
        "baseline_references",
    }
)
_REFERENCE_KEYS = frozenset({"event", "run_id", "turn_number", "source_path"})
_REFERENCE_OPTIONAL_KEYS = frozenset(
    {"plan_identity_id", "original_plan_identity_id", "timestamp"}
)
_REFERENCE_DEDUP_KEYS = (
    "event",
    "run_id",
    "turn_number",
    "source_path",
    "plan_identity_id",
    "original_plan_identity_id",
)
_BASELINE_REFERENCE_KEYS = frozenset(
    {"event", "source_path", "destination_path", "timestamp"}
)
_BASELINE_OWNERSHIP_KEYS = frozenset(
    {
        "plan_identity_id",
        "event",
        "source_path",
        "destination_path",
        "timestamp",
    }
)
_PLAN_IDENTITY_KEYS = frozenset(
    {"schema_version", "plan_identity_id", "current_path", "owned_paths"}
)


class BackupProvenanceError(OSError):
    """A backup provenance path or record cannot be safely used."""


def _raise(message: str) -> None:
    raise BackupProvenanceError(message)


def _validate_directory(path: Path, label: str, *, allow_missing: bool) -> bool:
    if path.is_symlink():
        _raise(f"{label} must not be a symlink: {path}")
    if path.exists() and not path.is_dir():
        _raise(f"{label} is not a directory: {path}")
    if not path.exists() and not allow_missing:
        return False
    return True


def _backup_directory(repo_root: Path, *, create: bool) -> Path | None:
    root = Path(repo_root)
    if root.is_symlink() or not root.is_dir():
        _raise(f"repository root is not a regular directory: {root}")

    plans_dir = root / "plans"
    backups_dir = plans_dir / "backups"
    _validate_directory(plans_dir, "plans directory", allow_missing=create)
    _validate_directory(backups_dir, "backup directory", allow_missing=create)
    if not backups_dir.exists():
        if not create:
            return None
        try:
            backups_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackupProvenanceError(
                f"failed to create backup directory: {backups_dir}: {exc}"
            ) from exc
    _validate_directory(plans_dir, "plans directory", allow_missing=False)
    _validate_directory(backups_dir, "backup directory", allow_missing=False)
    return backups_dir


def prepare_backup_directory(repo_root: Path) -> Path:
    """Create and validate the existing backup-body directory."""
    result = _backup_directory(Path(repo_root), create=True)
    assert result is not None
    return result


def _validate_backup_path(backup_dir: Path, backup_path: Path) -> Path:
    path = Path(backup_path)
    if path.name in {"", ".", ".."}:
        _raise(f"backup body has an invalid filename: {path}")
    if path.is_symlink():
        _raise(f"backup body must not be a symlink: {path}")
    try:
        relative = path.resolve().relative_to(backup_dir.resolve())
    except (OSError, ValueError) as exc:
        raise BackupProvenanceError(
            f"backup body escapes the backup directory: {path}"
        ) from exc
    if len(relative.parts) != 1 or relative.name != path.name:
        _raise(f"backup body must be a direct child of the backup directory: {path}")
    if not path.is_file():
        _raise(f"backup body is not a regular file: {path}")
    return path


def provenance_sidecar_path(
    repo_root: Path,
    backup_path: Path,
    *,
    create: bool,
) -> Path | None:
    """Return the exact sidecar path, optionally creating its safe directory."""
    backup_dir = _backup_directory(Path(repo_root), create=create)
    if backup_dir is None:
        return None
    backup_body = _validate_backup_path(backup_dir, Path(backup_path))
    provenance_dir = _provenance_directory(Path(repo_root), create=create)
    if provenance_dir is None:
        return None

    sidecar = provenance_dir / f"{backup_body.name}.json"
    if sidecar.is_symlink():
        _raise(f"provenance sidecar must not be a symlink: {sidecar}")
    if sidecar.exists() and not sidecar.is_file():
        _raise(f"provenance sidecar is not a regular file: {sidecar}")
    return sidecar


def _provenance_directory(repo_root: Path, *, create: bool) -> Path | None:
    backup_dir = _backup_directory(Path(repo_root), create=create)
    if backup_dir is None:
        return None
    provenance_dir = backup_dir / PROVENANCE_DIRECTORY_NAME
    _validate_directory(provenance_dir, "provenance directory", allow_missing=create)
    if not provenance_dir.exists():
        if not create:
            return None
        try:
            provenance_dir.mkdir(exist_ok=False)
        except OSError as exc:
            raise BackupProvenanceError(
                f"failed to create provenance directory: {provenance_dir}: {exc}"
            ) from exc
    _validate_directory(provenance_dir, "provenance directory", allow_missing=False)
    return provenance_dir


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BackupProvenanceError(f"cannot hash backup body {path}: {exc}") from exc
    return digest.hexdigest()


def _path_value(path: Path, *, require_file: bool) -> str:
    if path.is_symlink():
        _raise(f"provenance source path must not be a symlink: {path}")
    if require_file and not path.is_file():
        _raise(f"provenance source path is not a regular file: {path}")
    try:
        return str(path.resolve())
    except OSError as exc:
        raise BackupProvenanceError(
            f"cannot resolve provenance source path {path}: {exc}"
        ) from exc


def _validate_capture_inputs(
    *,
    kind: str,
    event: str,
    source_plan_path: Path,
    original_plan_path: Path | None,
    run_id: str | None,
    turn_number: int | None,
) -> tuple[str, str | None, str | None]:
    if kind not in SUPPORTED_BACKUP_KINDS:
        _raise(f"unsupported backup provenance kind: {kind!r}")
    if event not in SUPPORTED_BACKUP_EVENTS:
        _raise(f"unsupported backup provenance event: {event!r}")
    if run_id is not None and (
        not isinstance(run_id, str) or not run_id or run_id.strip() != run_id
    ):
        _raise("backup provenance run_id must be null or a non-empty identifier")
    if turn_number is not None and (
        not isinstance(turn_number, int)
        or isinstance(turn_number, bool)
        or turn_number < 1
    ):
        _raise("backup provenance turn_number must be null or a positive integer")
    if kind == "follow_up" and original_plan_path is None:
        _raise("follow-up backup provenance requires the original plan path")
    source_value = _path_value(Path(source_plan_path), require_file=True)
    original_value = (
        _path_value(Path(original_plan_path), require_file=False)
        if original_plan_path is not None
        else None
    )
    return source_value, original_value, run_id


def _capture_reference(
    *,
    event: str,
    run_id: str | None,
    turn_number: int | None,
    source_path: str,
    plan_identity_id: str | None = None,
    original_plan_identity_id: str | None = None,
    timestamp: str | None = None,
) -> dict[str, object]:
    reference: dict[str, object] = {
        "event": event,
        "run_id": run_id,
        "turn_number": turn_number,
        "source_path": source_path,
    }
    if plan_identity_id is not None:
        reference["plan_identity_id"] = plan_identity_id
    if original_plan_identity_id is not None:
        reference["original_plan_identity_id"] = original_plan_identity_id
    if timestamp is not None:
        reference["timestamp"] = timestamp
    return reference


def _valid_alias(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and Path(value).is_absolute()
    )


def _validate_baseline_reference(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != _BASELINE_REFERENCE_KEYS:
        return False
    return (
        value.get("event") == "ready_promotion"
        and _valid_alias(value.get("source_path"))
        and _valid_alias(value.get("destination_path"))
        and isinstance(value.get("timestamp"), str)
        and bool(value.get("timestamp", "").strip())
    )


def _validate_baseline_ownership(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != _BASELINE_OWNERSHIP_KEYS:
        return False
    return (
        _valid_plan_identity_id(value.get("plan_identity_id"))
        and value.get("event") == "ready_promotion"
        and _valid_alias(value.get("source_path"))
        and _valid_alias(value.get("destination_path"))
        and isinstance(value.get("timestamp"), str)
        and bool(value.get("timestamp", "").strip())
    )


def _new_record(
    *,
    backup_filename: str,
    content_sha256: str,
    kind: str,
    source_plan_path: str,
    original_plan_path: str | None,
    historical_origin_known: bool,
    event: str,
    run_id: str | None,
    turn_number: int | None,
    source_plan_identity_id: str | None,
    original_plan_identity_id: str | None,
    capture_timestamp: str,
) -> dict[str, object]:
    timestamp = capture_timestamp
    reference = _capture_reference(
        event=event,
        run_id=run_id,
        turn_number=turn_number,
        source_path=source_plan_path,
        plan_identity_id=source_plan_identity_id,
        original_plan_identity_id=original_plan_identity_id,
        timestamp=timestamp,
    )
    record: dict[str, object] = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "backup_filename": backup_filename,
        "content_sha256": content_sha256,
        "kind": kind,
        "source_plan_path": source_plan_path,
        "original_plan_path": original_plan_path,
        "historical_origin": "known" if historical_origin_known else "unknown",
        "first_capture_timestamp": timestamp,
        "first_capture_event": event,
        "first_run_id": run_id,
        "baseline_status": "unknown",
        "baseline_reference": None,
        "capture_references": [reference],
        "path_aliases": [source_plan_path],
    }
    if original_plan_identity_id is not None:
        record["original_plan_identity_ids"] = [original_plan_identity_id]
    return record


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _validate_reference(value: object) -> bool:
    if not isinstance(value, Mapping) or not _REFERENCE_KEYS <= set(value):
        return False
    if set(value) - (_REFERENCE_KEYS | _REFERENCE_OPTIONAL_KEYS):
        return False
    event = value.get("event")
    run_id = value.get("run_id")
    turn_number = value.get("turn_number")
    source_path = value.get("source_path")
    if event not in SUPPORTED_BACKUP_EVENTS:
        return False
    if run_id is not None and (
        not isinstance(run_id, str) or not run_id or run_id.strip() != run_id
    ):
        return False
    if turn_number is not None and (
        not isinstance(turn_number, int)
        or isinstance(turn_number, bool)
        or turn_number < 1
    ):
        return False
    if not isinstance(source_path, str) or not source_path.strip():
        return False
    identity = value.get("plan_identity_id")
    if identity is not None and not _valid_plan_identity_id(identity):
        return False
    original_identity = value.get("original_plan_identity_id")
    if original_identity is not None and not _valid_plan_identity_id(
        original_identity
    ):
        return False
    timestamp = value.get("timestamp")
    return timestamp is None or (
        isinstance(timestamp, str) and bool(timestamp.strip())
    )


def _same_capture_reference(
    left: Mapping[str, object], right: Mapping[str, object]
) -> bool:
    """Compare logical capture identity while ignoring presentation timing."""
    return all(left.get(key) == right.get(key) for key in _REFERENCE_DEDUP_KEYS)


def _validate_record(
    value: object,
    *,
    backup_filename: str,
    content_sha256: str,
) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    keys = set(value)
    if not _REQUIRED_RECORD_KEYS <= keys <= (
        _REQUIRED_RECORD_KEYS | _OPTIONAL_RECORD_KEYS
    ):
        return None
    if value.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        return None
    if value.get("backup_filename") != backup_filename:
        return None
    if value.get("content_sha256") != content_sha256 or not _is_sha256(
        value.get("content_sha256")
    ):
        return None
    if value.get("kind") not in SUPPORTED_BACKUP_KINDS:
        return None
    for field_name in (
        "source_plan_path",
        "first_capture_timestamp",
        "first_capture_event",
        "historical_origin",
    ):
        field_value = value.get(field_name)
        if not isinstance(field_value, str) or not field_value.strip():
            return None
    if value.get("first_capture_event") not in SUPPORTED_BACKUP_EVENTS:
        return None
    if value.get("historical_origin") not in {"known", "unknown"}:
        return None
    original_path = value.get("original_plan_path")
    if original_path is not None and (
        not isinstance(original_path, str) or not original_path.strip()
    ):
        return None
    first_run_id = value.get("first_run_id")
    if first_run_id is not None and (
        not isinstance(first_run_id, str)
        or not first_run_id
        or first_run_id.strip() != first_run_id
    ):
        return None
    if value.get("baseline_status") not in {"known", "unknown"}:
        return None
    baseline_reference = value.get("baseline_reference")
    baseline_identity = value.get("baseline_plan_identity_id")
    if baseline_identity is not None and not _valid_plan_identity_id(
        baseline_identity
    ):
        return None
    baseline_references = value.get("baseline_references")
    if baseline_references is not None and (
        not isinstance(baseline_references, list)
        or not baseline_references
        or not all(
            _validate_baseline_ownership(reference)
            for reference in baseline_references
        )
    ):
        return None
    if value.get("baseline_status") == "known":
        if not _validate_baseline_reference(baseline_reference):
            return None
        if baseline_references is not None:
            assert isinstance(baseline_references, list)
            if baseline_identity is not None and not any(
                reference["plan_identity_id"] == baseline_identity
                for reference in baseline_references
            ):
                return None
            if not all(
                {
                    reference["source_path"],
                    reference["destination_path"],
                } <= set(value.get("path_aliases", ()))
                for reference in baseline_references
            ):
                return None
    elif baseline_reference is not None or baseline_references is not None:
        return None
    references = value.get("capture_references")
    if not isinstance(references, list) or not all(
        _validate_reference(reference) for reference in references
    ):
        return None
    original_aliases = value.get("original_plan_aliases")
    if original_aliases is not None and (
        not isinstance(original_aliases, list)
        or not original_aliases
        or not all(_valid_alias(alias) for alias in original_aliases)
    ):
        return None
    original_identity_ids = value.get("original_plan_identity_ids")
    if original_identity_ids is not None and (
        not isinstance(original_identity_ids, list)
        or not original_identity_ids
        or not all(_valid_plan_identity_id(identity) for identity in original_identity_ids)
        or len(set(original_identity_ids)) != len(original_identity_ids)
    ):
        return None
    aliases = value.get("path_aliases", [value.get("source_plan_path")])
    if not isinstance(aliases, list) or not aliases or not all(
        _valid_alias(alias) for alias in aliases
    ):
        return None
    if value.get("baseline_status") == "known":
        assert isinstance(baseline_reference, Mapping)
        if not {
            baseline_reference["source_path"],
            baseline_reference["destination_path"],
        } <= set(aliases):
            return None
    payload = dict(value)
    payload["path_aliases"] = list(dict.fromkeys(aliases))
    if isinstance(original_aliases, list):
        payload["original_plan_aliases"] = list(dict.fromkeys(original_aliases))
    if isinstance(original_identity_ids, list):
        payload["original_plan_identity_ids"] = list(dict.fromkeys(original_identity_ids))
    return payload


def _read_valid_record(
    sidecar: Path,
    *,
    backup_filename: str,
    content_sha256: str,
) -> dict[str, object] | None:
    if sidecar.is_symlink() or not sidecar.is_file():
        return None
    try:
        value = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        return None
    return _validate_record(
        value,
        backup_filename=backup_filename,
        content_sha256=content_sha256,
    )


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    data = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError:
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _valid_plan_identity_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _owner_directory(repo_root: Path, *, create: bool) -> Path | None:
    provenance_dir = _provenance_directory(Path(repo_root), create=create)
    if provenance_dir is None:
        return None
    owner_dir = provenance_dir / PLAN_IDENTITY_DIRECTORY_NAME
    _validate_directory(owner_dir, "plan identity directory", allow_missing=create)
    if not owner_dir.exists():
        if not create:
            return None
        try:
            owner_dir.mkdir(exist_ok=False)
        except OSError as exc:
            raise BackupProvenanceError(
                f"failed to create plan identity directory: {owner_dir}: {exc}"
            ) from exc
    _validate_directory(owner_dir, "plan identity directory", allow_missing=False)
    return owner_dir


def _validate_plan_identity(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping) or set(value) != _PLAN_IDENTITY_KEYS:
        return None
    identity = value.get("plan_identity_id")
    current_path = value.get("current_path")
    owned_paths = value.get("owned_paths")
    if value.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        return None
    if not _valid_plan_identity_id(identity):
        return None
    if current_path is not None and not _valid_alias(current_path):
        return None
    if not isinstance(owned_paths, list) or not owned_paths or not all(
        _valid_alias(path) for path in owned_paths
    ):
        return None
    if len(set(owned_paths)) != len(owned_paths):
        return None
    if current_path is not None and current_path not in owned_paths:
        return None
    return {
        "schema_version": value["schema_version"],
        "plan_identity_id": identity,
        "current_path": current_path,
        "owned_paths": list(owned_paths),
    }


def _read_plan_identity(path: Path) -> dict[str, object] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        return None
    identity = _validate_plan_identity(value)
    if identity is None:
        return None
    if path.stem != identity["plan_identity_id"]:
        return None
    return identity


def _plan_identities(repo_root: Path, *, strict: bool) -> tuple[dict[str, object], ...]:
    owner_dir = _owner_directory(Path(repo_root), create=False)
    if owner_dir is None:
        return ()
    identities: list[dict[str, object]] = []
    for path in sorted(owner_dir.iterdir(), key=lambda item: item.name):
        if path.name.startswith(".") and path.name.endswith(".tmp"):
            continue
        identity = _read_plan_identity(path)
        if identity is None:
            if strict:
                _raise(f"plan identity metadata is invalid: {path}")
            return ()
        identities.append(identity)
    return tuple(identities)


def plan_identity_for_path(repo_root: Path, plan_path: Path) -> str | None:
    """Return the unique current owner of one exact plan path, if known."""
    try:
        plan_value = _path_value(Path(plan_path), require_file=False)
        identities = _plan_identities(Path(repo_root), strict=False)
    except (BackupProvenanceError, OSError):
        return None
    matches = [
        identity
        for identity in identities
        if identity.get("current_path") == plan_value
    ]
    if len(matches) != 1:
        return None
    identity = matches[0].get("plan_identity_id")
    return identity if isinstance(identity, str) else None


def create_plan_identity(repo_root: Path, plan_path: Path) -> str:
    """Create a fresh current owner for a newly created plan file."""
    plan_value = _path_value(Path(plan_path), require_file=True)
    owner_dir = _owner_directory(Path(repo_root), create=True)
    assert owner_dir is not None
    identities = list(_plan_identities(Path(repo_root), strict=True))
    for identity in identities:
        if identity.get("current_path") != plan_value:
            continue
        payload = dict(identity)
        payload["current_path"] = None
        _write_atomic_json(
            owner_dir / f"{identity['plan_identity_id']}.json", payload
        )
    identity_id = uuid4().hex
    payload = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "plan_identity_id": identity_id,
        "current_path": plan_value,
        "owned_paths": [plan_value],
    }
    _write_atomic_json(owner_dir / f"{identity_id}.json", payload)
    return identity_id


def ensure_plan_identity(repo_root: Path, plan_path: Path) -> str:
    """Return a plan's owner, creating one only when it has none."""
    existing = plan_identity_for_path(repo_root, plan_path)
    if existing is not None:
        return existing
    return create_plan_identity(repo_root, plan_path)


def bind_unowned_plan_history(
    repo_root: Path,
    plan_path: Path,
    *,
    plan_identity_id: str | None = None,
) -> int:
    """Adopt unowned exact-path evidence when service ownership begins.

    This boundary is intentionally narrower than lifecycle alias matching. It
    binds only references that point at the exact current source path and
    follow-up records whose exact original path is that source. Existing
    references for another identity make that individual association
    ambiguous, so the unowned evidence is left unassociated.
    """
    source_value = _path_value(Path(plan_path), require_file=False)
    current_owner_id = plan_identity_for_path(repo_root, Path(plan_path))
    owner_id = (
        current_owner_id if plan_identity_id is None else plan_identity_id
    )
    if not _valid_plan_identity_id(owner_id) or current_owner_id != owner_id:
        _raise(f"plan identity ownership is not unique: {source_value}")
    assert isinstance(owner_id, str)

    updated = 0
    for backup_path, record in list_backup_provenance(repo_root):
        references = record.get("capture_references")
        if not isinstance(references, list):
            continue
        updated_references = [dict(reference) for reference in references]
        payload = dict(record)
        changed = False

        exact_source_references = [
            reference
            for reference in updated_references
            if reference.get("source_path") == source_value
        ]
        source_identity_ids = {
            reference.get("plan_identity_id")
            for reference in exact_source_references
            if reference.get("plan_identity_id") is not None
        }
        if exact_source_references and source_identity_ids <= {owner_id}:
            for reference in updated_references:
                if (
                    reference.get("source_path") == source_value
                    and reference.get("plan_identity_id") is None
                ):
                    reference["plan_identity_id"] = owner_id
                    changed = True

        original_path = record.get("original_plan_path")
        original_identity_ids = record.get("original_plan_identity_ids", [])
        if not isinstance(original_identity_ids, list):
            original_identity_ids = []
        original_identity_ids = {
            identity
            for identity in original_identity_ids
            if identity is not None
        }
        original_identity_ids.update(
            reference.get("original_plan_identity_id")
            for reference in updated_references
            if reference.get("original_plan_identity_id") is not None
        )
        if (
            record.get("kind") == "follow_up"
            and original_path == source_value
            and original_identity_ids <= {owner_id}
        ):
            for reference in updated_references:
                if reference.get("original_plan_identity_id") is None:
                    reference["original_plan_identity_id"] = owner_id
                    changed = True
            stored_original_ids = record.get("original_plan_identity_ids", [])
            if not isinstance(stored_original_ids, list):
                stored_original_ids = []
            if owner_id not in stored_original_ids:
                payload["original_plan_identity_ids"] = [
                    *stored_original_ids,
                    owner_id,
                ]
                changed = True

        if not changed:
            continue
        payload["capture_references"] = updated_references
        sidecar = provenance_sidecar_path(repo_root, backup_path, create=False)
        if sidecar is None:
            continue
        try:
            _write_atomic_json(sidecar, payload)
        except OSError as exc:
            raise BackupProvenanceError(
                f"failed to bind existing provenance sidecar {sidecar}: {exc}"
            ) from exc
        updated += 1
    return updated


def move_plan_identity(
    repo_root: Path,
    *,
    source_plan_path: Path,
    destination_plan_path: Path,
) -> bool:
    """Move the unique current owner after an exact successful file move."""
    source_value = _path_value(Path(source_plan_path), require_file=False)
    destination_value = _path_value(Path(destination_plan_path), require_file=True)
    owner_dir = _owner_directory(Path(repo_root), create=False)
    if owner_dir is None:
        return False
    identities = list(_plan_identities(Path(repo_root), strict=True))
    matches = [
        identity
        for identity in identities
        if identity.get("current_path") == source_value
    ]
    if not matches:
        return False
    if len(matches) != 1:
        _raise(f"plan identity ownership is ambiguous: {source_value}")
    identity = matches[0]
    owned_paths = identity["owned_paths"]
    assert isinstance(owned_paths, list)
    payload = dict(identity)
    payload["current_path"] = destination_value
    payload["owned_paths"] = [
        *owned_paths,
        *((destination_value,) if destination_value not in owned_paths else ()),
    ]
    _write_atomic_json(owner_dir / f"{identity['plan_identity_id']}.json", payload)
    return True


def revert_plan_identity_move(
    repo_root: Path,
    *,
    source_plan_path: Path,
    destination_plan_path: Path,
) -> None:
    """Restore current ownership after a file move is rolled back."""
    source_value = _path_value(Path(source_plan_path), require_file=False)
    destination_value = _path_value(Path(destination_plan_path), require_file=True)
    owner_dir = _owner_directory(Path(repo_root), create=False)
    if owner_dir is None:
        return
    identities = list(_plan_identities(Path(repo_root), strict=True))
    matches = [
        identity
        for identity in identities
        if identity.get("current_path") == destination_value
    ]
    if not matches:
        return
    if len(matches) != 1:
        _raise(f"plan identity ownership is ambiguous: {destination_value}")
    identity = matches[0]
    payload = dict(identity)
    payload["current_path"] = source_value
    owned_paths = identity["owned_paths"]
    assert isinstance(owned_paths, list)
    payload["owned_paths"] = [
        *owned_paths,
        *((source_value,) if source_value not in owned_paths else ()),
    ]
    _write_atomic_json(owner_dir / f"{identity['plan_identity_id']}.json", payload)


def record_backup_provenance(
    repo_root: Path,
    backup_path: Path,
    *,
    kind: str,
    source_plan_path: Path,
    original_plan_path: Path | None,
    event: str,
    run_id: str | None = None,
    turn_number: int | None = None,
    historical_origin_known: bool = True,
) -> dict[str, object]:
    """Record one backup capture and return the durable record payload."""
    source_value, original_value, run_value = _validate_capture_inputs(
        kind=kind,
        event=event,
        source_plan_path=Path(source_plan_path),
        original_plan_path=(
            Path(original_plan_path) if original_plan_path is not None else None
        ),
        run_id=run_id,
        turn_number=turn_number,
    )
    sidecar = provenance_sidecar_path(repo_root, backup_path, create=True)
    assert sidecar is not None
    backup_body = Path(backup_path)
    content_sha256 = _sha256_file(backup_body)
    source_identity_id = plan_identity_for_path(repo_root, Path(source_plan_path))
    original_identity_id = (
        plan_identity_for_path(repo_root, Path(original_plan_path))
        if original_plan_path is not None
        else None
    )
    existing = _read_valid_record(
        sidecar,
        backup_filename=backup_body.name,
        content_sha256=content_sha256,
    )
    capture_timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    reference = _capture_reference(
        event=event,
        run_id=run_value,
        turn_number=turn_number,
        source_path=source_value,
        plan_identity_id=source_identity_id,
        original_plan_identity_id=original_identity_id,
        timestamp=capture_timestamp,
    )
    if existing is None:
        payload = _new_record(
            backup_filename=backup_body.name,
            content_sha256=content_sha256,
            kind=kind,
            source_plan_path=source_value,
            original_plan_path=original_value,
            historical_origin_known=historical_origin_known,
            event=event,
            run_id=run_value,
            turn_number=turn_number,
            source_plan_identity_id=source_identity_id,
            original_plan_identity_id=original_identity_id,
            capture_timestamp=capture_timestamp,
        )
    else:
        references = existing["capture_references"]
        assert isinstance(references, list)
        if any(
            isinstance(existing_reference, Mapping)
            and _same_capture_reference(existing_reference, reference)
            for existing_reference in references
        ):
            return existing
        payload = dict(existing)
        payload["capture_references"] = [*references, reference]
        if original_identity_id is not None:
            identity_ids = existing.get("original_plan_identity_ids", [])
            if not isinstance(identity_ids, list):
                identity_ids = []
            if original_identity_id not in identity_ids:
                payload["original_plan_identity_ids"] = [
                    *identity_ids,
                    original_identity_id,
                ]
    try:
        _write_atomic_json(sidecar, payload)
    except OSError as exc:
        raise BackupProvenanceError(
            f"failed to write backup provenance sidecar {sidecar}: {exc}"
        ) from exc
    return payload


def list_backup_provenance(
    repo_root: Path,
) -> tuple[tuple[Path, dict[str, object]], ...]:
    """List only backup bodies whose sidecars and hashes validate."""
    try:
        backup_dir = _backup_directory(Path(repo_root), create=False)
        if backup_dir is None:
            return ()
        provenance_dir = backup_dir / PROVENANCE_DIRECTORY_NAME
        if not _validate_directory(
            provenance_dir, "provenance directory", allow_missing=False
        ):
            return ()
        if not provenance_dir.exists():
            return ()
        entries: list[tuple[Path, dict[str, object]]] = []
        for backup_body in sorted(backup_dir.iterdir(), key=lambda item: item.name):
            if backup_body.name == PROVENANCE_DIRECTORY_NAME:
                continue
            if backup_body.is_symlink() or not backup_body.is_file():
                continue
            record = _read_valid_record(
                provenance_dir / f"{backup_body.name}.json",
                backup_filename=backup_body.name,
                content_sha256=_sha256_file(backup_body),
            )
            if record is not None:
                entries.append((backup_body, record))
        return tuple(entries)
    except (BackupProvenanceError, OSError, UnicodeError, ValueError, TypeError):
        return ()


def backup_provenance_for_plan(
    repo_root: Path,
    plan_path: Path,
) -> tuple[tuple[Path, dict[str, object]], ...]:
    """Return records explicitly associated with one exact plan path."""
    try:
        plan_value = _path_value(Path(plan_path), require_file=False)
    except (BackupProvenanceError, OSError):
        return ()
    owner_id = plan_identity_for_path(repo_root, Path(plan_path))
    matches: list[tuple[Path, dict[str, object]]] = []
    for backup_path, record in list_backup_provenance(repo_root):
        if owner_id is not None:
            references = record.get("capture_references", [])
            original_identity_ids = record.get("original_plan_identity_ids", [])
            source_match = isinstance(references, list) and any(
                isinstance(reference, Mapping)
                and reference.get("plan_identity_id") == owner_id
                for reference in references
            )
            original_match = (
                isinstance(original_identity_ids, list)
                and owner_id in original_identity_ids
            )
            matches_owner = source_match or original_match
        else:
            references = record.get("capture_references", [])
            original_aliases = record.get("original_plan_aliases", [])
            aliases = record.get("path_aliases", [record["source_plan_path"]])
            source_match = isinstance(references, list) and any(
                isinstance(reference, Mapping)
                and reference.get("source_path") == plan_value
                for reference in references
            )
            original_match = (
                record.get("original_plan_path") == plan_value
                or (
                    isinstance(original_aliases, list)
                    and plan_value in original_aliases
                )
            )
            legacy_match = isinstance(aliases, list) and plan_value in aliases
            matches_owner = source_match or original_match or legacy_match
        if matches_owner:
            matches.append((backup_path, record))
    return tuple(matches)


def _baseline_ownerships(record: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    references = record.get("baseline_references")
    if isinstance(references, list):
        return tuple(
            reference
            for reference in references
            if isinstance(reference, Mapping)
        )
    identity = record.get("baseline_plan_identity_id")
    baseline = record.get("baseline_reference")
    if _valid_plan_identity_id(identity) and isinstance(baseline, Mapping):
        return ({**baseline, "plan_identity_id": identity},)
    return ()


def baseline_status_for_plan(
    repo_root: Path,
    plan_path: Path,
    record: Mapping[str, object],
) -> str:
    """Project one record's baseline status onto its selected current owner."""
    owner_id = plan_identity_for_path(repo_root, Path(plan_path))
    if owner_id is None or record.get("baseline_status") != "known":
        return "unknown"
    return (
        "known"
        if any(
            reference.get("plan_identity_id") == owner_id
            for reference in _baseline_ownerships(record)
        )
        else "unknown"
    )


def _baseline_reference_for_plan(
    repo_root: Path,
    plan_path: Path,
    record: Mapping[str, object],
) -> Mapping[str, object] | None:
    owner_id = plan_identity_for_path(repo_root, Path(plan_path))
    if owner_id is None or record.get("baseline_status") != "known":
        return None
    for reference in _baseline_ownerships(record):
        if reference.get("plan_identity_id") == owner_id:
            return reference
    return None


def backup_reference_for_plan(
    repo_root: Path,
    plan_path: Path,
    record: Mapping[str, object],
) -> dict[str, object] | None:
    """Return the selected plan's validated event details, if available."""
    try:
        plan_value = _path_value(Path(plan_path), require_file=False)
    except (BackupProvenanceError, OSError):
        return None

    baseline_reference = _baseline_reference_for_plan(repo_root, Path(plan_path), record)
    if baseline_reference is not None:
        run_id: object = None
        turn_number: object = None
        references = record.get("capture_references")
        if isinstance(references, list):
            for reference in references:
                if (
                    isinstance(reference, Mapping)
                    and reference.get("plan_identity_id")
                    == baseline_reference.get("plan_identity_id")
                    and reference.get("event") == "ready_promotion"
                    and reference.get("source_path")
                    == baseline_reference.get("source_path")
                ):
                    run_id = reference.get("run_id")
                    turn_number = reference.get("turn_number")
                    break
        return {
            "event": baseline_reference["event"],
            "timestamp": baseline_reference["timestamp"],
            "run_id": run_id,
            "turn_number": turn_number,
        }

    references = record.get("capture_references")
    if not isinstance(references, list):
        return None
    owner_id = plan_identity_for_path(repo_root, Path(plan_path))
    if owner_id is not None:
        association_key = (
            "original_plan_identity_id"
            if record.get("kind") == "follow_up"
            else "plan_identity_id"
        )
        for reference in references:
            if (
                isinstance(reference, Mapping)
                and reference.get(association_key) == owner_id
            ):
                return dict(reference)
        return None

    association_key = (
        "original_plan_path"
        if record.get("kind") == "follow_up"
        else "source_path"
    )
    if record.get("kind") == "follow_up":
        if record.get("original_plan_path") != plan_value:
            return None
        return dict(references[0]) if isinstance(references[0], Mapping) else None
    for reference in references:
        if (
            isinstance(reference, Mapping)
            and reference.get(association_key) == plan_value
        ):
            return dict(reference)
    return None


def has_known_baseline(repo_root: Path, plan_path: Path) -> bool:
    """Return true only when one unambiguous known baseline owns the path."""
    records = backup_provenance_for_plan(repo_root, plan_path)
    known = [
        record
        for _, record in records
        if baseline_status_for_plan(repo_root, plan_path, record) == "known"
    ]
    return len(known) == 1


def mark_ready_baseline(
    repo_root: Path,
    backup_path: Path,
    *,
    source_plan_path: Path,
    destination_plan_path: Path,
) -> dict[str, object]:
    """Qualify one captured body from an explicit todo→Ready promotion."""
    source_value = _path_value(Path(source_plan_path), require_file=False)
    destination_value = _path_value(Path(destination_plan_path), require_file=True)
    sidecar = provenance_sidecar_path(repo_root, backup_path, create=False)
    if sidecar is None:
        _raise("baseline capture has no provenance sidecar")
    body = Path(backup_path)
    content_sha256 = _sha256_file(body)
    existing = _read_valid_record(
        sidecar,
        backup_filename=body.name,
        content_sha256=content_sha256,
    )
    if existing is None:
        _raise("baseline capture has invalid provenance")
    source_identity_id = plan_identity_for_path(repo_root, Path(source_plan_path))
    if source_identity_id is None:
        _raise("baseline source has no unique plan identity")
    references = existing.get("capture_references")
    if not isinstance(references, list) or not any(
        isinstance(reference, Mapping)
        and reference.get("source_path") == source_value
        and reference.get("plan_identity_id") == source_identity_id
        for reference in references
    ):
        _raise("baseline capture is not bound to the promoted plan identity")
    aliases = existing.get("path_aliases", [existing["source_plan_path"]])
    if not isinstance(aliases, list):
        _raise("baseline capture has invalid path history")
    timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    ownerships = list(_baseline_ownerships(existing))
    current_ownership = {
        "plan_identity_id": source_identity_id,
        "event": "ready_promotion",
        "source_path": source_value,
        "destination_path": destination_value,
        "timestamp": timestamp,
    }
    if not any(
        ownership.get("plan_identity_id") == source_identity_id
        and ownership.get("source_path") == source_value
        and ownership.get("destination_path") == destination_value
        for ownership in ownerships
    ):
        ownerships.append(current_ownership)
    payload = dict(existing)
    payload["baseline_status"] = "known"
    if existing.get("baseline_status") != "known":
        payload["baseline_reference"] = {
            "event": "ready_promotion",
            "source_path": source_value,
            "destination_path": destination_value,
            "timestamp": timestamp,
        }
    payload["baseline_references"] = ownerships
    payload["baseline_plan_identity_id"] = ownerships[0]["plan_identity_id"]
    payload["path_aliases"] = list(
        dict.fromkeys([*aliases, source_value, destination_value])
    )
    try:
        _write_atomic_json(sidecar, payload)
    except OSError as exc:
        raise BackupProvenanceError(
            f"failed to write baseline provenance sidecar {sidecar}: {exc}"
        ) from exc
    return payload


def record_plan_lifecycle_move(
    repo_root: Path,
    *,
    source_plan_path: Path,
    destination_plan_path: Path,
) -> int:
    """Record an exact owned move without making capture paths authoritative."""
    source_value = _path_value(Path(source_plan_path), require_file=False)
    destination_value = _path_value(Path(destination_plan_path), require_file=True)
    updated = 0
    for backup_path, record in list_backup_provenance(repo_root):
        aliases = record.get("path_aliases", [record["source_plan_path"]])
        original_aliases = record.get("original_plan_aliases", [])
        source_match = isinstance(aliases, list) and source_value in aliases
        original_match = record.get("original_plan_path") == source_value or (
            isinstance(original_aliases, list) and source_value in original_aliases
        )
        if not source_match and not original_match:
            continue
        payload = dict(record)
        changed = False
        if source_match and destination_value not in aliases:
            payload["path_aliases"] = [*aliases, destination_value]
            changed = True
        if original_match:
            if not isinstance(original_aliases, list) or not original_aliases:
                original_aliases = [record["original_plan_path"]]
            if destination_value not in original_aliases:
                payload["original_plan_aliases"] = [
                    *original_aliases,
                    destination_value,
                ]
                changed = True
        if not changed:
            continue
        sidecar = provenance_sidecar_path(repo_root, backup_path, create=False)
        if sidecar is None:
            continue
        try:
            _write_atomic_json(sidecar, payload)
        except OSError as exc:
            raise BackupProvenanceError(
                f"failed to update lifecycle provenance sidecar {sidecar}: {exc}"
            ) from exc
        updated += 1
    move_plan_identity(
        repo_root,
        source_plan_path=source_plan_path,
        destination_plan_path=destination_plan_path,
    )
    return updated


def revert_plan_lifecycle_move(
    repo_root: Path,
    *,
    source_plan_path: Path,
    destination_plan_path: Path,
    baseline_backup_path: Path | None = None,
) -> None:
    """Undo metadata written for a move that the caller rolled back."""
    source_value = _path_value(Path(source_plan_path), require_file=False)
    destination_value = _path_value(Path(destination_plan_path), require_file=True)
    baseline_path = (
        Path(baseline_backup_path).resolve()
        if baseline_backup_path is not None
        else None
    )
    moved_identity_id = plan_identity_for_path(
        repo_root, Path(destination_plan_path)
    )
    for backup_path, record in list_backup_provenance(repo_root):
        aliases = record.get("path_aliases", [record["source_plan_path"]])
        if not isinstance(aliases, list):
            continue
        payload = dict(record)
        changed = False
        if destination_value in aliases:
            remaining_aliases = [
                alias for alias in aliases if alias != destination_value
            ]
            payload["path_aliases"] = remaining_aliases or [source_value]
            changed = True
        original_aliases = record.get("original_plan_aliases")
        if isinstance(original_aliases, list) and destination_value in original_aliases:
            remaining_original_aliases = [
                alias for alias in original_aliases if alias != destination_value
            ]
            original_path = record.get("original_plan_path")
            if isinstance(original_path, str):
                remaining_original_aliases = [
                    *remaining_original_aliases,
                    *((original_path,) if original_path not in remaining_original_aliases else ()),
                ]
            payload["original_plan_aliases"] = list(
                dict.fromkeys(remaining_original_aliases)
            )
            changed = True
        if (
            baseline_path is not None
            and Path(backup_path).resolve() == baseline_path
            and record.get("baseline_status") == "known"
        ):
            ownerships = list(_baseline_ownerships(record))
            remaining_ownerships = [
                ownership
                for ownership in ownerships
                if not (
                    ownership.get("source_path") == source_value
                    and ownership.get("destination_path") == destination_value
                    and (
                        moved_identity_id is None
                        or ownership.get("plan_identity_id") == moved_identity_id
                    )
                )
            ]
            if len(remaining_ownerships) != len(ownerships):
                changed = True
                if remaining_ownerships:
                    payload["baseline_references"] = remaining_ownerships
                    payload["baseline_plan_identity_id"] = remaining_ownerships[0][
                        "plan_identity_id"
                    ]
                    first = remaining_ownerships[0]
                    payload["baseline_reference"] = {
                        key: first[key]
                        for key in _BASELINE_REFERENCE_KEYS
                    }
                else:
                    payload["baseline_status"] = "unknown"
                    payload["baseline_reference"] = None
                    payload.pop("baseline_references", None)
                    payload.pop("baseline_plan_identity_id", None)
            else:
                baseline_reference = record.get("baseline_reference")
                if (
                    moved_identity_id is None
                    and isinstance(baseline_reference, Mapping)
                    and baseline_reference.get("source_path") == source_value
                    and baseline_reference.get("destination_path") == destination_value
                ):
                    payload["baseline_status"] = "unknown"
                    payload["baseline_reference"] = None
                    payload.pop("baseline_references", None)
                    payload.pop("baseline_plan_identity_id", None)
                    changed = True
        if not changed:
            continue
        sidecar = provenance_sidecar_path(repo_root, backup_path, create=False)
        if sidecar is None:
            continue
        try:
            _write_atomic_json(sidecar, payload)
        except OSError as exc:
            raise BackupProvenanceError(
                f"failed to revert lifecycle provenance sidecar {sidecar}: {exc}"
            ) from exc
    revert_plan_identity_move(
        repo_root,
        source_plan_path=source_plan_path,
        destination_plan_path=destination_plan_path,
    )


def read_backup_provenance(
    repo_root: Path,
    backup_path: Path,
) -> dict[str, object] | None:
    """Read one record, returning ``None`` for missing or malformed metadata."""
    try:
        sidecar = provenance_sidecar_path(repo_root, backup_path, create=False)
        if sidecar is None:
            return None
        backup_body = Path(backup_path)
        return _read_valid_record(
            sidecar,
            backup_filename=backup_body.name,
            content_sha256=_sha256_file(backup_body),
        )
    except (BackupProvenanceError, OSError, UnicodeError, ValueError, TypeError):
        return None
