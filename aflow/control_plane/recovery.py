"""Durable, provider-neutral intent for explicit replacement recovery.

This module deliberately contains no harness or provider calls.  The daemon
uses these small immutable records to validate a caller's explicit recovery
choice and to publish the evidence binding that a later worker can consume.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Literal

from aflow.recovery_request import (
    DurableEvidenceRecoveryRequest,
    RECOVERY_MODE_DURABLE_EVIDENCE,
    RecoveryRequest,
    RecoveryValidationError,
)


RECOVERY_SCHEMA_VERSION = 1
RECOVERY_ARTIFACT_PATH = "recovery/recovery-intent.json"
RECOVERY_MAX_BYTES = 32_768
RECOVERY_MAX_EVIDENCE = 32
RECOVERY_MAX_TEXT = 512


class RecoveryPersistenceError(ValueError):
    """A recovery artifact cannot be published without overwriting state."""


def _safe_text(value: object, *, field: str, limit: int = RECOVERY_MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise RecoveryValidationError(f"recovery field '{field}' must be a string")
    if not value or value != value.strip() or len(value) > limit:
        raise RecoveryValidationError(
            f"recovery field '{field}' is empty, padded, or oversized"
        )
    if "\x00" in value or "\n" in value or "\r" in value:
        raise RecoveryValidationError(
            f"recovery field '{field}' contains control characters"
        )
    return value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(payload: Mapping[str, Any], *, trailing_newline: bool) -> bytes:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return encoded + (b"\n" if trailing_newline else b"")


@dataclass(frozen=True)
class RecoveryEvidenceReference:
    """One exact file or workspace fact bound into a recovery intent."""

    path: str
    sha256: str
    size: int = 0
    kind: Literal["file", "workspace"] = "file"

    def __post_init__(self) -> None:
        _safe_text(self.path, field="evidence.path", limit=2048)
        if len(self.sha256) != 64 or self.sha256 != self.sha256.lower():
            raise RecoveryValidationError("recovery evidence hash is invalid")
        if any(character not in "0123456789abcdef" for character in self.sha256):
            raise RecoveryValidationError("recovery evidence hash is invalid")
        if not isinstance(self.size, int) or isinstance(self.size, bool) or self.size < 0:
            raise RecoveryValidationError("recovery evidence size is invalid")
        if self.kind not in {"file", "workspace"}:
            raise RecoveryValidationError("recovery evidence kind is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class RecoveryIntent:
    """Versioned successor-owned provenance for one recovery request."""

    source_run_id: str
    target_run_id: str
    source_selector: str
    target_selector: str
    evidence: tuple[RecoveryEvidenceReference, ...]
    source_session_context_transferred: bool = False
    mode: Literal["durable_evidence"] = RECOVERY_MODE_DURABLE_EVIDENCE
    schema_version: int = RECOVERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _safe_text(self.source_run_id, field="source_run_id")
        _safe_text(self.target_run_id, field="target_run_id")
        _safe_text(self.source_selector, field="source_selector")
        _safe_text(self.target_selector, field="target_selector")
        if self.mode != RECOVERY_MODE_DURABLE_EVIDENCE:
            raise RecoveryValidationError("recovery intent mode is invalid")
        if self.schema_version != RECOVERY_SCHEMA_VERSION:
            raise RecoveryValidationError("unsupported recovery intent schema version")
        if self.source_session_context_transferred is not False:
            raise RecoveryValidationError(
                "source session context transfer must remain false"
            )
        if not isinstance(self.evidence, tuple) or not self.evidence:
            raise RecoveryValidationError("recovery intent requires evidence")
        if len(self.evidence) > RECOVERY_MAX_EVIDENCE:
            raise RecoveryValidationError("recovery intent contains too much evidence")
        if any(not isinstance(item, RecoveryEvidenceReference) for item in self.evidence):
            raise RecoveryValidationError("recovery intent evidence is malformed")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "source_run_id": self.source_run_id,
            "target_run_id": self.target_run_id,
            "source_selector": self.source_selector,
            "target_selector": self.target_selector,
            "evidence": [item.to_dict() for item in self.evidence],
            "source_session_context_transferred": (
                self.source_session_context_transferred
            ),
        }


def recovery_intent_digest(intent: RecoveryIntent) -> str:
    """Return the deterministic digest used for replay identity checks."""
    return _sha256(_canonical_json(intent.to_dict(), trailing_newline=False))


def recovery_artifact_digest(intent: RecoveryIntent) -> str:
    """Return the digest of the exact bytes written to the artifact."""
    return _sha256(_canonical_json(intent.to_dict(), trailing_newline=True))


def _artifact_path(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    if run_dir.is_symlink() or (run_dir.exists() and not run_dir.is_dir()):
        raise RecoveryPersistenceError("recovery run directory is unsafe")
    root = run_dir.resolve()
    relative = Path(RECOVERY_ARTIFACT_PATH)
    path = root / relative
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise RecoveryPersistenceError("recovery artifact escapes its run") from exc
    return path


def persist_recovery_intent(
    run_dir: Path,
    intent: RecoveryIntent,
) -> tuple[str, str]:
    """Publish one immutable intent, accepting only an identical replay."""
    path = _artifact_path(run_dir)
    encoded = _canonical_json(intent.to_dict(), trailing_newline=True)
    if len(encoded) > RECOVERY_MAX_BYTES:
        raise RecoveryPersistenceError("recovery intent exceeds its size limit")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise RecoveryPersistenceError("recovery artifact is unsafe")
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise RecoveryPersistenceError("recovery artifact cannot be read") from exc
        if existing != encoded:
            raise RecoveryPersistenceError(
                "recovery artifact already exists with different bytes"
            )
        return RECOVERY_ARTIFACT_PATH, _sha256(existing)

    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise RecoveryPersistenceError("recovery artifact directory is unsafe")
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
                raise RecoveryPersistenceError(
                    "recovery artifact concurrently published with different bytes"
                ) from None
        descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise RecoveryPersistenceError("recovery artifact could not be published") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return RECOVERY_ARTIFACT_PATH, _sha256(encoded)


def read_recovery_intent(run_dir: Path) -> RecoveryIntent:
    """Read and strictly validate a previously published recovery intent."""
    path = _artifact_path(run_dir)
    if path.is_symlink() or not path.is_file():
        raise RecoveryPersistenceError("recovery artifact is missing or unsafe")
    try:
        if path.stat().st_size > RECOVERY_MAX_BYTES:
            raise RecoveryPersistenceError("recovery intent exceeds its size limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except RecoveryPersistenceError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise RecoveryPersistenceError("recovery artifact is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise RecoveryPersistenceError("recovery artifact must contain an object")
    expected = {
        "schema_version",
        "mode",
        "source_run_id",
        "target_run_id",
        "source_selector",
        "target_selector",
        "evidence",
        "source_session_context_transferred",
    }
    if set(payload) != expected:
        raise RecoveryPersistenceError("recovery artifact fields are not canonical")
    evidence_value = payload.get("evidence")
    if not isinstance(evidence_value, list):
        raise RecoveryPersistenceError("recovery artifact evidence is malformed")
    evidence: list[RecoveryEvidenceReference] = []
    for raw in evidence_value:
        if not isinstance(raw, Mapping):
            raise RecoveryPersistenceError("recovery artifact evidence is malformed")
        if set(raw) != {"path", "sha256", "size", "kind"}:
            raise RecoveryPersistenceError("recovery artifact evidence is not canonical")
        try:
            evidence.append(
                RecoveryEvidenceReference(
                    path=raw["path"],
                    sha256=raw["sha256"],
                    size=raw["size"],
                    kind=raw["kind"],
                )
            )
        except (KeyError, TypeError, RecoveryValidationError) as exc:
            raise RecoveryPersistenceError(
                "recovery artifact evidence is invalid"
            ) from exc
    try:
        return RecoveryIntent(
            schema_version=payload["schema_version"],
            mode=payload["mode"],
            source_run_id=payload["source_run_id"],
            target_run_id=payload["target_run_id"],
            source_selector=payload["source_selector"],
            target_selector=payload["target_selector"],
            evidence=tuple(evidence),
            source_session_context_transferred=payload[
                "source_session_context_transferred"
            ],
        )
    except (KeyError, RecoveryValidationError) as exc:
        raise RecoveryPersistenceError("recovery artifact is invalid") from exc


__all__ = [
    "RECOVERY_ARTIFACT_PATH",
    "RECOVERY_MODE_DURABLE_EVIDENCE",
    "RECOVERY_SCHEMA_VERSION",
    "DurableEvidenceRecoveryRequest",
    "RecoveryEvidenceReference",
    "RecoveryIntent",
    "RecoveryPersistenceError",
    "RecoveryRequest",
    "RecoveryValidationError",
    "persist_recovery_intent",
    "read_recovery_intent",
    "recovery_artifact_digest",
    "recovery_intent_digest",
]
