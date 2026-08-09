"""Durable provider-neutral state for live role-selector hotplugging."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal, Mapping
from uuid import uuid4

HOTPLUG_SCHEMA_VERSION = 1
HOTPLUG_STAGES = (
    "accepted", "target_preflighted", "quiescing", "source_finalized",
    "handover_starting", "handover_ready", "target_starting", "applied",
    "failed", "waiting_for_hotplug_recovery",
)
HotplugStage = Literal[
    "accepted", "target_preflighted", "quiescing", "source_finalized",
    "handover_starting", "handover_ready", "target_starting", "applied",
    "failed", "waiting_for_hotplug_recovery",
]
_MAX_HISTORY = 16
_SHA256_HEX = 64


def _safe_text(value: object | None, *, limit: int = 512) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("hotplug text values must be strings")
    value = value.strip()
    if not value or len(value) > limit or "\n" in value or "\r" in value:
        raise ValueError("hotplug text value is empty, oversized, or multiline")
    return value


def _required_text(raw: Mapping[str, Any], key: str, *, limit: int = 512) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"hotplug field '{key}' must be a string")
    checked = _safe_text(value, limit=limit)
    if checked is None:
        raise ValueError(f"hotplug field '{key}' is required")
    return checked


def _required_int(raw: Mapping[str, Any], key: str, *, positive: bool = False) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"hotplug field '{key}' must be an integer")
    if positive and value < 1:
        raise ValueError(f"hotplug field '{key}' must be positive")
    return value


def hotplug_transaction_id(run_id: str, accepted_digest: str, number: int) -> str:
    if _safe_text(run_id) is None or _safe_text(accepted_digest) is None:
        raise ValueError("run_id and accepted_digest are required")
    if number < 1:
        raise ValueError("hotplug transaction numbers start at one")
    return f"{run_id}:hotplug-{number:03d}:{accepted_digest}"


@dataclass(frozen=True)
class HarnessSessionRefV1:
    session_id: str
    role: str
    selector: str
    harness: str
    profile: str
    model_display: str
    status: Literal["active", "handed_over", "closed"] = "active"
    schema_version: int = HOTPLUG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool) or self.schema_version != HOTPLUG_SCHEMA_VERSION:
            raise ValueError("unsupported hotplug session schema version")
        for field_name in ("session_id", "role", "selector", "harness", "profile", "model_display"):
            if _safe_text(getattr(self, field_name)) is None:
                raise ValueError(f"{field_name} is required")
        if self.status not in {"active", "handed_over", "closed"}:
            raise ValueError("invalid hotplug session status")

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "session_id": self.session_id,
                "role": self.role, "selector": self.selector, "harness": self.harness,
                "profile": self.profile, "model_display": self.model_display,
                "status": self.status}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], *, strict: bool = False) -> "HarnessSessionRefV1":
        if not isinstance(raw, Mapping):
            raise ValueError("hotplug session must be a mapping")
        if strict and "schema_version" not in raw:
            raise ValueError("modern hotplug session requires schema_version")
        if strict and "status" not in raw:
            raise ValueError("modern hotplug session requires status")
        schema_version = raw.get("schema_version", 1)
        if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != HOTPLUG_SCHEMA_VERSION:
            raise ValueError("unsupported hotplug session schema version")
        status = raw.get("status", "active")
        if not isinstance(status, str):
            raise ValueError("hotplug session status must be a string")
        return cls(schema_version=schema_version,
                   session_id=_required_text(raw, "session_id"),
                   role=_required_text(raw, "role"),
                   selector=_required_text(raw, "selector"),
                   harness=_required_text(raw, "harness"),
                   profile=_required_text(raw, "profile"),
                   model_display=_required_text(raw, "model_display"),
                   status=status)


@dataclass(frozen=True)
class HotplugTransactionV1:
    transaction_id: str
    run_id: str
    accepted_override_digest: str
    transaction_number: int
    source_role: str
    target_role: str
    source_selector: str
    target_selector: str
    source_harness: str
    target_harness: str
    source_profile: str
    target_profile: str
    source_model_display: str
    target_model_display: str
    source_turn_number: int | None = None
    source_session: HarnessSessionRefV1 | None = None
    capability_path: Literal["native_resume", "handover_required"] | None = None
    stage: HotplugStage = "accepted"
    artifact_paths: tuple[str, ...] = ()
    artifact_hashes: tuple[str, ...] = ()
    failure: str | None = None
    remediation: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    schema_version: int = HOTPLUG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool)
                or self.schema_version != HOTPLUG_SCHEMA_VERSION or self.stage not in HOTPLUG_STAGES):
            raise ValueError("invalid hotplug transaction schema or stage")
        if self.transaction_id != hotplug_transaction_id(self.run_id, self.accepted_override_digest, self.transaction_number):
            raise ValueError("hotplug transaction identity does not match its inputs")
        if self.capability_path not in {None, "native_resume", "handover_required"}:
            raise ValueError("invalid hotplug capability path")
        if len(self.artifact_paths) != len(self.artifact_hashes):
            raise ValueError("artifact paths and hashes must have equal lengths")
        if self.transaction_number < 1 or self.source_turn_number is not None and self.source_turn_number < 1:
            raise ValueError("hotplug transaction numbers and source turns must be positive")
        if not _is_sha256(self.accepted_override_digest):
            raise ValueError("accepted_override_digest must be a SHA-256 hex digest")
        if any(not _is_sha256(value) for value in self.artifact_hashes):
            raise ValueError("artifact hashes must be SHA-256 hex digests")
        for path in self.artifact_paths:
            _validate_artifact_reference(path)
        for value_name in ("failure", "remediation"):
            value = getattr(self, value_name)
            if value is not None:
                _safe_text(value, limit=1024)
        for value in (self.source_role, self.target_role, self.source_selector, self.target_selector,
                      self.source_harness, self.target_harness, self.source_profile, self.target_profile,
                      self.source_model_display, self.target_model_display):
            _safe_text(value)

    def to_dict(self) -> dict[str, object]:
        result = {key: value for key, value in self.__dict__.items()
                  if key != "source_session"}
        result["artifact_paths"] = list(self.artifact_paths)
        result["artifact_hashes"] = list(self.artifact_hashes)
        result["source_session"] = self.source_session.to_dict() if self.source_session else None
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], *, strict: bool = False) -> "HotplugTransactionV1":
        if not isinstance(raw, Mapping):
            raise ValueError("hotplug transaction must be a mapping")
        if strict and "schema_version" not in raw:
            raise ValueError("modern hotplug transaction requires schema_version")
        if strict and "stage" not in raw:
            raise ValueError("modern hotplug transaction requires stage")
        schema_version = raw.get("schema_version", 1)
        if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != HOTPLUG_SCHEMA_VERSION:
            raise ValueError("unsupported hotplug transaction schema version")
        artifact_paths = raw.get("artifact_paths", [])
        artifact_hashes = raw.get("artifact_hashes", [])
        if not isinstance(artifact_paths, list) or not all(isinstance(value, str) for value in artifact_paths):
            raise ValueError("artifact_paths must be a list of strings")
        if not isinstance(artifact_hashes, list) or not all(isinstance(value, str) for value in artifact_hashes):
            raise ValueError("artifact_hashes must be a list of strings")
        source_turn_number = raw.get("source_turn_number")
        if source_turn_number is not None and (not isinstance(source_turn_number, int) or isinstance(source_turn_number, bool)):
            raise ValueError("source_turn_number must be an integer or null")
        session = raw.get("source_session")
        if session is not None and not isinstance(session, Mapping):
            raise ValueError("source_session must be a mapping or null")
        stage = raw.get("stage", "accepted")
        capability_path = raw.get("capability_path")
        if stage not in HOTPLUG_STAGES or capability_path not in {None, "native_resume", "handover_required"}:
            raise ValueError("invalid hotplug transaction enum")
        failure = raw.get("failure")
        remediation = raw.get("remediation")
        for name, value in (("failure", failure), ("remediation", remediation)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string or null")
        created_at = raw.get("created_at")
        if not isinstance(created_at, str) or not created_at or len(created_at) > 128 or "\n" in created_at or "\r" in created_at:
            raise ValueError("created_at must be a bounded single-line string")
        try:
            parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("created_at must be ISO-8601") from exc
        if parsed_created_at.tzinfo is None or parsed_created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return cls(schema_version=schema_version,
                   transaction_id=_required_text(raw, "transaction_id"),
                   run_id=_required_text(raw, "run_id"),
                   accepted_override_digest=_required_text(raw, "accepted_override_digest"),
                   transaction_number=_required_int(raw, "transaction_number", positive=True),
                   source_role=_required_text(raw, "source_role"), target_role=_required_text(raw, "target_role"),
                   source_selector=_required_text(raw, "source_selector"), target_selector=_required_text(raw, "target_selector"),
                   source_harness=_required_text(raw, "source_harness"), target_harness=_required_text(raw, "target_harness"),
                   source_profile=_required_text(raw, "source_profile"), target_profile=_required_text(raw, "target_profile"),
                   source_model_display=_required_text(raw, "source_model_display"), target_model_display=_required_text(raw, "target_model_display"),
                   source_turn_number=source_turn_number,
                   source_session=HarnessSessionRefV1.from_dict(session, strict=strict) if session is not None else None,
                   capability_path=capability_path, stage=stage,
                   artifact_paths=tuple(artifact_paths), artifact_hashes=tuple(artifact_hashes),
                   failure=failure, remediation=remediation, created_at=created_at)


def hotplug_artifact_dir(run_dir: Path, transaction_number: int) -> Path:
    if transaction_number < 1:
        raise ValueError("hotplug transaction numbers start at one")
    return run_dir / "hotplugs" / f"hotplug-{transaction_number:03d}"


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and value == value.lower() and len(value) == _SHA256_HEX and all(
        character in "0123456789abcdef" for character in value
    )


def _validate_artifact_reference(relative_path: str) -> None:
    relative = Path(relative_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("hotplug artifact path must be a safe run-relative path")
    if any(part in {"", "."} for part in relative.parts):
        raise ValueError("hotplug artifact path contains an invalid component")


def safe_hotplug_artifact_path(run_dir: Path, relative_path: str) -> Path:
    _validate_artifact_reference(relative_path)
    relative = Path(relative_path)
    root = run_dir.resolve()
    if run_dir.exists() and run_dir.is_symlink():
        raise ValueError("hotplug run directory must not be a symlink")
    if run_dir.exists() and not run_dir.is_dir():
        raise ValueError("hotplug run directory must be a directory")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("hotplug artifact path escapes the run directory") from exc
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError("hotplug artifact path contains a symlink component")
    return candidate


def write_hotplug_artifact(run_dir: Path, relative_path: str,
                           content: Mapping[str, object] | str | bytes) -> tuple[str, str]:
    path = safe_hotplug_artifact_path(run_dir, relative_path)
    if path.exists():
        raise FileExistsError(f"hotplug artifact already exists: {relative_path}")
    if isinstance(content, Mapping):
        data = (json.dumps(dict(content), indent=2, sort_keys=True) + "\n").encode()
    elif isinstance(content, str):
        data = content.encode()
    else:
        data = content
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return path.relative_to(run_dir.resolve()).as_posix(), hashlib.sha256(data).hexdigest()


def bounded_hotplug_history(history: tuple[HotplugTransactionV1, ...] | list[HotplugTransactionV1]) -> tuple[HotplugTransactionV1, ...]:
    return tuple(history[-_MAX_HISTORY:])
