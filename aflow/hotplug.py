"""Durable provider-neutral state for live role-selector hotplugging."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
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
HANDOVER_MAX_BYTES = 8192
HANDOVER_HEADINGS = (
    "Objective And Checkpoint", "Completed Work", "Changed Files",
    "Verification", "Unfinished Work And Exact Next Action",
    "Decisions And Assumptions", "Hazards And Dirty State",
    "Relevant Paths And Artifacts",
)


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
    provider_operation_id: str | None = None
    idempotency_key: str | None = None
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
        for value_name in ("provider_operation_id", "idempotency_key"):
            value = getattr(self, value_name)
            if value is not None:
                _safe_text(value, limit=512)
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
        provider_operation_id = raw.get("provider_operation_id")
        idempotency_key = raw.get("idempotency_key")
        for name, value in (("failure", failure), ("remediation", remediation)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string or null")
        for name, value in (("provider_operation_id", provider_operation_id), ("idempotency_key", idempotency_key)):
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
                   failure=failure, remediation=remediation,
                   provider_operation_id=provider_operation_id,
                   idempotency_key=idempotency_key, created_at=created_at)


@dataclass(frozen=True)
class HandoverContextV1:
    """Bounded controller projection used for cross-harness bootstrap."""

    transaction_id: str
    source_selector: str
    target_selector: str
    objective: str
    checkpoint: Mapping[str, object]
    scope: Mapping[str, object]
    completed_work: tuple[str, ...]
    implementation_attempts: tuple[Mapping[str, object], ...]
    rejection_summary: str | None
    run_summary: Mapping[str, object]
    workspace_facts: Mapping[str, object]
    artifact_refs: tuple[str, ...]
    full_context_sha256: str
    schema_version: int = HOTPLUG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != HOTPLUG_SCHEMA_VERSION:
            raise ValueError("unsupported handover context schema version")
        for value in (self.transaction_id, self.source_selector, self.target_selector, self.objective, self.full_context_sha256):
            if _safe_text(value, limit=2048) is None:
                raise ValueError("handover context authority fields are required")
        if not _is_sha256(self.full_context_sha256):
            raise ValueError("full_context_sha256 must be a SHA-256 hex digest")
        for path in self.artifact_refs:
            _validate_artifact_reference(path)

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["artifact_refs"] = list(self.artifact_refs)
        result["completed_work"] = list(self.completed_work)
        result["implementation_attempts"] = [dict(item) for item in self.implementation_attempts]
        return result


def _bounded_context_value(value: object, *, limit: int = 2048) -> object:
    """Keep context JSON deterministic and prevent transcript/prompt leakage."""
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, Mapping):
        return {str(key): _bounded_context_value(child, limit=limit) for key, child in list(value.items())[:32]}
    if isinstance(value, (list, tuple)):
        return [_bounded_context_value(child, limit=limit) for child in list(value)[:32]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:limit]


def build_handover_context_v1(
    transaction: HotplugTransactionV1,
    full_context: Mapping[str, object],
    *,
    artifact_refs: tuple[str, ...] = (),
) -> HandoverContextV1:
    """Project Full evidence without manager authority, prompts, or secrets."""
    canonical = json.dumps(_bounded_context_value(full_context), sort_keys=True, separators=(",", ":"))
    plan_state = full_context.get("plan_state", {})
    scope = full_context.get("active_implementation_scope", {})
    attempts = full_context.get("implementation_attempts", ())
    rejections = full_context.get("latest_full_rejection", {})
    run_summary = full_context.get("run_summary", {})
    workspace = full_context.get("workspace_facts", {})
    completed = full_context.get("completed_work", ())
    return HandoverContextV1(
        transaction_id=transaction.transaction_id,
        source_selector=transaction.source_selector,
        target_selector=transaction.target_selector,
        objective="Continue the selected workflow checkpoint from the source worker boundary.",
        checkpoint=_bounded_context_value(plan_state) if isinstance(plan_state, Mapping) else {},
        scope=_bounded_context_value(scope) if isinstance(scope, Mapping) else {},
        completed_work=tuple(str(item)[:512] for item in completed if isinstance(item, str)) if isinstance(completed, (list, tuple)) else (),
        implementation_attempts=tuple(
            _bounded_context_value(item) for item in attempts if isinstance(item, Mapping)
        ) if isinstance(attempts, (list, tuple)) else (),
        rejection_summary=(
            str(rejections.get("summary", ""))[:2048]
            if isinstance(rejections, Mapping) and rejections.get("summary") else None
        ),
        run_summary=_bounded_context_value(run_summary) if isinstance(run_summary, Mapping) else {},
        workspace_facts=_bounded_context_value(workspace) if isinstance(workspace, Mapping) else {},
        artifact_refs=tuple(artifact_refs),
        full_context_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def validate_handover_output(output: str, *, max_bytes: int = HANDOVER_MAX_BYTES) -> str:
    if not isinstance(output, str):
        raise ValueError("handover output must be text")
    normalized = output.replace("\r\n", "\n").strip()
    if not normalized or len(normalized.encode("utf-8")) > max_bytes:
        raise ValueError("handover output is empty or exceeds the 8 KiB bound")
    matches = list(re.finditer(r"(?m)^## ([^\n]+)\n", normalized))
    headings = tuple(match.group(1).strip() for match in matches)
    if headings != HANDOVER_HEADINGS:
        raise ValueError("handover output must contain the exact required section order")
    for index, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        body = normalized[body_start:body_end].strip()
        if not body or re.fullmatch(r"(?:[-*]?\s*(?:tbd|n/?a|none|unknown)\.?\s*)+", body, re.IGNORECASE):
            raise ValueError(f"handover section '{match.group(1)}' is empty or placeholder")
    if any(marker in normalized.lower() for marker in ("chain of thought", "scratchpad", "hidden reasoning")):
        raise ValueError("handover output requests hidden reasoning")
    return normalized + "\n"


def render_handover_prompt(
    transaction: HotplugTransactionV1,
    context: HandoverContextV1,
) -> str:
    return (
        "Produce a bounded operational handover for the controller.\n"
        "Do not modify the repository, plan, or run artifacts. Do not disclose hidden reasoning.\n"
        f"Transaction: {transaction.transaction_id}\n"
        f"Target selector: {transaction.target_selector}\n"
        "Read the plan and worktree as authoritative over conflicting prose.\n\n"
        "Required Markdown sections:\n"
        + "\n".join(f"## {heading}" for heading in HANDOVER_HEADINGS)
        + "\n\nController continuity context artifact hash: "
        + context.full_context_sha256
        + "\nThe controller will provide the run-relative artifact reference after validation."
    )


def workspace_fingerprint(repo_root: Path, plan_paths: tuple[Path, ...] = ()) -> dict[str, object]:
    """Fingerprint tracked/untracked state and plan bytes for read-only enforcement."""
    def git(*args: str) -> str:
        result = subprocess.run(["git", *args], cwd=repo_root, capture_output=True, text=True, check=False)
        return result.stdout.strip()
    plans = {}
    for path in plan_paths:
        try:
            plans[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            plans[str(path)] = "<missing>"
    payload = {"head": git("rev-parse", "HEAD"), "status": git("status", "--porcelain=v1", "--untracked-files=all"), "plans": plans}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload


def write_handover_artifacts(
    run_dir: Path,
    transaction_number: int,
    context: HandoverContextV1,
    output: str,
    full_context: Mapping[str, object] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    normalized = validate_handover_output(output)
    prefix = f"hotplugs/hotplug-{transaction_number:03d}"
    output_ref, output_hash = write_hotplug_artifact(run_dir, f"{prefix}/handover.md", normalized)
    context_ref, context_hash = write_hotplug_artifact(run_dir, f"{prefix}/context.json", context.to_dict())
    full_ref, full_hash = write_hotplug_artifact(
        run_dir, f"{prefix}/full-context.json",
        _bounded_context_value(full_context if full_context is not None else context.to_dict()),
    )
    return (output_ref, context_ref, full_ref), (output_hash, context_hash, full_hash)


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


HOTPLUG_TERMINAL_STAGES = frozenset({"applied", "failed"})
HOTPLUG_AMBIGUOUS_STAGES = frozenset({"quiescing", "handover_starting", "target_starting"})


def _read_bound_artifact(run_dir: Path, relative_path: str, expected_hash: str) -> bytes:
    path = safe_hotplug_artifact_path(run_dir, relative_path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"hotplug artifact is missing: {relative_path}") from exc
    observed = hashlib.sha256(data).hexdigest()
    if observed != expected_hash:
        raise ValueError(
            f"hotplug artifact hash mismatch for {relative_path}: "
            f"expected {expected_hash}, observed {observed}"
        )
    return data


def validate_hotplug_resume_artifacts(run_dir: Path, transaction: HotplugTransactionV1) -> None:
    """Validate every artifact required to continue a persisted transaction."""
    if transaction.stage in HOTPLUG_TERMINAL_STAGES | {"accepted", "target_preflighted", "source_finalized"}:
        return
    if transaction.stage == "waiting_for_hotplug_recovery" and not transaction.artifact_paths:
        return
    if transaction.source_session is None:
        raise ValueError("hotplug handover stage requires an exact source session id")
    if transaction.stage in HOTPLUG_AMBIGUOUS_STAGES and not transaction.artifact_paths:
        return
    if len(transaction.artifact_paths) != 3 or len(transaction.artifact_hashes) != 3:
        raise ValueError("hotplug handover requires exactly three bound artifacts")
    for relative, digest in zip(transaction.artifact_paths, transaction.artifact_hashes):
        _read_bound_artifact(run_dir, relative, digest)
    try:
        json.loads(_read_bound_artifact(run_dir, transaction.artifact_paths[1], transaction.artifact_hashes[1]))
        json.loads(_read_bound_artifact(run_dir, transaction.artifact_paths[2], transaction.artifact_hashes[2]))
    except json.JSONDecodeError as exc:
        raise ValueError("hotplug continuity artifact is not valid JSON") from exc


def classify_hotplug_resume_stage(
    run_dir: Path, transaction: HotplugTransactionV1,
) -> HotplugTransactionV1:
    """Return the fail-closed resume state for a transaction boundary."""
    validate_hotplug_resume_artifacts(run_dir, transaction)
    if transaction.stage in HOTPLUG_AMBIGUOUS_STAGES:
        return replace(
            transaction,
            stage="waiting_for_hotplug_recovery",
            remediation=(
                "provider operation result is unavailable; inspect the durable "
                "provider/session operation before retrying"
            ),
        )
    return transaction


def copy_hotplug_resume_artifacts(
    source_run_dir: Path, target_run_dir: Path, transaction: HotplugTransactionV1,
) -> None:
    """Copy and hash-bind immutable hotplug artifacts into a successor run."""
    if not transaction.artifact_paths:
        return
    validate_hotplug_resume_artifacts(source_run_dir, transaction)
    for relative, digest in zip(transaction.artifact_paths, transaction.artifact_hashes):
        data = _read_bound_artifact(source_run_dir, relative, digest)
        new_relative, new_digest = write_hotplug_artifact(target_run_dir, relative, data)
        if new_relative != relative or new_digest != digest:
            raise ValueError(f"hotplug artifact copy changed bytes: {relative}")
