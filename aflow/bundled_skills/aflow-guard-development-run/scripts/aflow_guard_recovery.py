#!/usr/bin/env python3
"""Authorize one narrow, evidence-backed startup replacement.

This helper is deliberately separate from ``aflow_guard_snapshot.py``.  The
snapshot remains an observer-only process; this module accepts a bounded,
matched launch receipt and only records a durable attempt.  The caller owns the
normal launch surface and supplies it as a callback in tests or as the next
guard action described by the bundled skill.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Mapping


SCHEMA_VERSION = 1
ATTEMPT_SCHEMA_VERSION = 2
ATTEMPT_FILE_NAME = "neutral-startup-recovery.json"
MAX_RECEIPT_BYTES = 128 * 1024
MAX_STATE_BYTES = 128 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
BASE_HEAD_PATTERN = re.compile(r"^[0-9a-f]{40}$")
IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
REPLACEMENT_KEY_PREFIX = "aflow-neutral-replacement-"
REPLACEMENT_KEY_PATTERN = re.compile(
    rf"^{re.escape(REPLACEMENT_KEY_PREFIX)}[0-9a-f]{{64}}$"
)
OUTCOME_STATUSES = {"acknowledged", "failed", "uncertain"}
OWNERSHIP_MODES = {"legacy", "ui-server", "aflowd"}
LAUNCH_SURFACES = {"legacy_cli", "web_mcp"}
DERIVATION_METHODS = {"in_place_current_branch", "managed_worktree_selected_branch"}
MISSING_SUPPORT_FIELDS = {"plan_branch", "pre_handoff_base_head"}


@dataclass(frozen=True)
class Eligibility:
    """Fail-closed result for one matched launch receipt."""

    eligible: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "reasons": list(self.reasons)}


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_text(value: Any, *, identity: bool = False) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    return not identity or bool(IDENTITY_PATTERN.fullmatch(value))


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256_PATTERN.fullmatch(value))


def _absolute_path(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and "\x00" not in value and Path(value).is_absolute()


def _same_path_under(repository: str, plan: str) -> bool:
    try:
        repository_path = Path(repository).resolve()
        plan_path = Path(plan).resolve()
        return plan_path != repository_path and repository_path in plan_path.parents
    except OSError:
        return False


def _reason(reasons: list[str], value: str) -> None:
    if value not in reasons:
        reasons.append(value)


def evaluate_eligibility(receipt: Mapping[str, Any] | Any) -> Eligibility:
    """Validate the complete neutral-recovery receipt without changing state.

    The receipt intentionally duplicates no fact outside ``evidence``.  Every
    required value is matched to the selected launch request before this helper
    sees it.  Missing, malformed, or uncertain values are ineligible.
    """

    reasons: list[str] = []
    root = _mapping(receipt)
    if root is None:
        return Eligibility(False, ("receipt must be a JSON object",))
    if root.get("schema_version") != SCHEMA_VERSION:
        _reason(reasons, "unsupported receipt schema")

    evidence = _mapping(root.get("evidence"))
    if evidence is None:
        _reason(reasons, "matched launch evidence is missing")
        return Eligibility(False, tuple(reasons))
    if evidence.get("matched") is not True or evidence.get("source") != "matched_launch_evidence":
        _reason(reasons, "launch evidence is not positively matched")

    authorization = _mapping(evidence.get("authorization"))
    if authorization is None or authorization.get("authorized") is not True:
        _reason(reasons, "selected launch is not explicitly authorized")
    else:
        ownership_mode = authorization.get("ownership_mode")
        launch_surface = authorization.get("launch_surface")
        if ownership_mode not in OWNERSHIP_MODES:
            _reason(reasons, "ownership mode is missing or unsupported")
        if launch_surface not in LAUNCH_SURFACES:
            _reason(reasons, "launch surface is missing or unsupported")
        elif ownership_mode == "legacy" and launch_surface != "legacy_cli":
            _reason(reasons, "legacy runs require the legacy CLI launch surface")
        elif ownership_mode in {"ui-server", "aflowd"} and launch_surface != "web_mcp":
            _reason(reasons, "server-owned runs require the advertised web/MCP launch surface")

    request = _mapping(evidence.get("request"))
    if request is None:
        _reason(reasons, "startup request identity is missing")
        request = {}
    for key in ("startup_request_id", "idempotency_key"):
        if not _nonempty_text(request.get(key), identity=True):
            _reason(reasons, f"{key} is missing or malformed")

    failure = _mapping(evidence.get("failure"))
    if failure is None:
        _reason(reasons, "startup failure evidence is missing")
        failure = {}
    if failure.get("status") != "failed":
        _reason(reasons, "startup failure is not a failed terminal result")
    if failure.get("phase") != "pre_controller":
        _reason(reasons, "failure did not occur before controller creation")
    if failure.get("kind") != "missing_or_blank_git_tracking":
        _reason(reasons, "failure is not the supported Git Tracking metadata failure")
    if failure.get("terminal") is not True:
        _reason(reasons, "startup failure is not terminal")
    missing_fields = failure.get("missing_fields")
    if (
        not isinstance(missing_fields, list)
        or not missing_fields
        or any(field not in MISSING_SUPPORT_FIELDS for field in missing_fields)
    ):
        _reason(reasons, "missing support metadata fields are not positively identified")

    identity = _mapping(evidence.get("identity"))
    if identity is None:
        _reason(reasons, "predecessor or startup request identity is missing")
        identity = {}
    predecessor_run_id = identity.get("predecessor_run_id")
    matched_run_id = identity.get("matched_run_id")
    startup_request_id = request.get("startup_request_id")
    has_predecessor = _nonempty_text(predecessor_run_id, identity=True)
    has_request = _nonempty_text(startup_request_id, identity=True)
    if not has_predecessor and not has_request:
        _reason(reasons, "no predecessor or startup request identity is available")
    if has_predecessor:
        if not _nonempty_text(matched_run_id, identity=True) or matched_run_id != predecessor_run_id:
            _reason(reasons, "launch evidence is matched to the wrong predecessor run")
    elif matched_run_id is not None:
        _reason(reasons, "matched predecessor run is present without a predecessor identity")
    if identity.get("startup_request_id") != startup_request_id:
        _reason(reasons, "launch evidence has a mismatched startup request identity")

    work = _mapping(evidence.get("work"))
    if work is None:
        _reason(reasons, "zero-work evidence is missing")
        work = {}
    for key in ("started_turns", "finalized_turns"):
        if not _is_int(work.get(key)) or work.get(key) != 0:
            _reason(reasons, f"{key} is not exactly zero")
    for key in (
        "worker_changes",
        "branch_created",
        "worktree_created",
        "plan_content_unchanged",
        "plan_semantics_unchanged",
        "launch_choices_unchanged",
    ):
        expected = key not in {"worker_changes", "branch_created", "worktree_created"}
        if not _is_bool(work.get(key)) or work.get(key) != expected:
            if key in {"plan_content_unchanged", "plan_semantics_unchanged"}:
                _reason(reasons, "plan content or semantics changed")
            else:
                _reason(reasons, f"{key} is not a proven neutral value")

    ownership = _mapping(evidence.get("ownership"))
    if ownership is None:
        _reason(reasons, "owned process evidence is missing")
        ownership = {}
    for key in ("controller", "child", "provider_session"):
        if ownership.get(key) != "none":
            _reason(reasons, f"{key} is active or unknown")

    selected = _mapping(evidence.get("selected_launch"))
    if selected is None:
        _reason(reasons, "selected launch choices are missing")
        selected = {}
    repository = selected.get("repository")
    plan = selected.get("plan")
    if not _absolute_path(repository) or not _absolute_path(plan):
        _reason(reasons, "selected repository and plan must be absolute paths")
    elif not _same_path_under(repository, plan):
        _reason(reasons, "selected plan is outside the selected repository")
    for key in ("workflow", "team", "start_step", "branch"):
        if not _nonempty_text(selected.get(key)):
            _reason(reasons, f"selected launch {key} is missing")
    max_turns = selected.get("max_turns")
    if not _is_int(max_turns) or max_turns <= 0:
        _reason(reasons, "selected launch max_turns is missing or invalid")
    extra_instructions = selected.get("extra_instructions")
    if not isinstance(extra_instructions, list) or any(
        not isinstance(item, str) for item in extra_instructions
    ):
        _reason(reasons, "selected launch extra instructions are missing or invalid")
    if not _sha256(selected.get("choices_digest")):
        _reason(reasons, "selected launch choices digest is missing or invalid")
    if not _sha256(selected.get("original_plan_content_sha256")):
        _reason(reasons, "original plan content digest is missing or invalid")
    if not _nonempty_text(selected.get("base_head")) or not BASE_HEAD_PATTERN.fullmatch(
        selected.get("base_head", "")
    ):
        _reason(reasons, "mechanically derived base head is missing or invalid")

    derivation = _mapping(evidence.get("derivation"))
    if derivation is None:
        _reason(reasons, "branch/base derivation evidence is missing")
    else:
        if derivation.get("mechanically_derivable") is not True:
            _reason(reasons, "branch/base is not mechanically derivable")
        if derivation.get("method") not in DERIVATION_METHODS:
            _reason(reasons, "branch/base derivation method is unsupported")
        if derivation.get("branch") != selected.get("branch"):
            _reason(reasons, "derived branch does not match the selected launch")
        if derivation.get("base_head") != selected.get("base_head"):
            _reason(reasons, "derived base does not match the selected launch")

    match = _mapping(evidence.get("match"))
    if match is None:
        _reason(reasons, "launch evidence match receipt is missing")
    else:
        if match.get("startup_request_id") != request.get("startup_request_id"):
            _reason(reasons, "matched launch request does not match the selected request")
        if match.get("selected_plan") != selected.get("plan"):
            _reason(reasons, "matched launch plan does not match the selected plan")
        if match.get("selected_choices_digest") != selected.get("choices_digest"):
            _reason(reasons, "matched launch choices do not match the selected launch")
        if match.get("idempotency_key") != request.get("idempotency_key"):
            _reason(reasons, "matched launch idempotency key does not match the selected request")
        if match.get("predecessor_run_id") != matched_run_id:
            _reason(reasons, "matched launch predecessor does not match the selected run")

    content = _mapping(evidence.get("content"))
    if content is None:
        _reason(reasons, "original plan content evidence is missing")
    else:
        if content.get("sha256") != selected.get("original_plan_content_sha256"):
            _reason(reasons, "original plan content digest does not match selected evidence")
        if content.get("unchanged") is not True or content.get("semantic_unchanged") is not True:
            _reason(reasons, "plan content or semantics changed")

    return Eligibility(not reasons, tuple(reasons))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _replacement_idempotency_key(
    receipt_digest: str,
    predecessor_idempotency_key: str,
) -> str:
    """Derive the one stable request key for the claimed replacement."""

    material = (
        f"{SCHEMA_VERSION}\x00{predecessor_idempotency_key}\x00{receipt_digest}"
    ).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()
    replacement = f"{REPLACEMENT_KEY_PREFIX}{digest}"
    if replacement == predecessor_idempotency_key:
        replacement = (
            f"{REPLACEMENT_KEY_PREFIX}{digest[:-1]}"
            f"{('1' if digest[-1] == '0' else '0')}"
        )
    return replacement


def _bounded_json(path: Path, limit: int) -> Any:
    with path.open("rb") as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ValueError(f"{path} exceeds the bounded JSON limit")
    return json.loads(payload.decode("utf-8"))


def _validate_state_dir(state_dir: Path) -> None:
    try:
        metadata = state_dir.lstat()
    except OSError as exc:
        raise ValueError("guard state directory is unavailable") from exc
    if not metadata or not state_dir.is_dir() or state_dir.is_symlink():
        raise ValueError("guard state directory must be an existing real directory")


def _attempt_path(state_dir: Path) -> Path:
    _validate_state_dir(state_dir)
    path = state_dir / ATTEMPT_FILE_NAME
    try:
        if path.is_symlink():
            raise ValueError("guard recovery attempt record must not be a symlink")
    except OSError as exc:
        raise ValueError("guard recovery attempt record is unavailable") from exc
    return path


def _write_exclusive(path: Path, record: Mapping[str, Any]) -> None:
    payload = json.dumps(record, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    if len(payload) > MAX_STATE_BYTES:
        raise ValueError("guard recovery attempt record exceeds the bounded state limit")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_replacement(path: Path, record: Mapping[str, Any]) -> None:
    payload = json.dumps(record, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    if len(payload) > MAX_STATE_BYTES:
        raise ValueError("guard recovery attempt record exceeds the bounded state limit")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _read_attempt(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        if path.is_symlink():
            raise ValueError("guard recovery attempt record must not be a symlink")
        value = _bounded_json(path, MAX_STATE_BYTES)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("guard recovery attempt record is invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != ATTEMPT_SCHEMA_VERSION
        or value.get("kind") != "neutral_startup_recovery"
        or value.get("status") not in {"claimed", "acknowledged", "failed", "uncertain"}
        or not isinstance(value.get("request"), dict)
        or not _nonempty_text(value["request"].get("startup_request_id"), identity=True)
        or not _nonempty_text(
            value["request"].get("predecessor_idempotency_key"), identity=True
        )
        or not REPLACEMENT_KEY_PATTERN.fullmatch(
            str(value["request"].get("replacement_idempotency_key", ""))
        )
        or value["request"].get("replacement_idempotency_key")
        == value["request"].get("predecessor_idempotency_key")
        or not _sha256(value.get("receipt_digest"))
    ):
        raise ValueError("guard recovery attempt record is invalid")
    return value


def _selected_record(
    evidence: Mapping[str, Any], replacement_idempotency_key: str
) -> dict[str, Any]:
    selected = dict(evidence["selected_launch"])
    authorization = evidence["authorization"]
    request = evidence["request"]
    identity = evidence["identity"]
    return {
        "repository": selected["repository"],
        "plan": selected["plan"],
        "workflow": selected["workflow"],
        "team": selected["team"],
        "start_step": selected["start_step"],
        "max_turns": selected["max_turns"],
        "extra_instructions": list(selected["extra_instructions"]),
        "branch": selected["branch"],
        "base_head": selected["base_head"],
        "ownership_mode": authorization["ownership_mode"],
        "launch_surface": authorization["launch_surface"],
        "idempotency_key": replacement_idempotency_key,
        "predecessor_idempotency_key": request["idempotency_key"],
        "replacement_idempotency_key": replacement_idempotency_key,
        "startup_request_id": request["startup_request_id"],
        "predecessor_run_id": identity.get("predecessor_run_id"),
    }


def _claim_record(receipt: Mapping[str, Any]) -> dict[str, Any]:
    evidence = receipt["evidence"]
    selected = evidence["selected_launch"]
    identity = evidence["identity"]
    predecessor_idempotency_key = evidence["request"]["idempotency_key"]
    receipt_digest = _canonical_digest(receipt)
    replacement_idempotency_key = _replacement_idempotency_key(
        receipt_digest,
        predecessor_idempotency_key,
    )
    return {
        "schema_version": ATTEMPT_SCHEMA_VERSION,
        "kind": "neutral_startup_recovery",
        "status": "claimed",
        "claimed_at": _utc_now(),
        "request": {
            "startup_request_id": evidence["request"]["startup_request_id"],
            "predecessor_idempotency_key": predecessor_idempotency_key,
            "replacement_idempotency_key": replacement_idempotency_key,
        },
        "predecessor": {
            "run_id": identity.get("predecessor_run_id"),
            "startup_request_id": identity.get("startup_request_id"),
        },
        "correction_reason": "missing_or_blank_git_tracking",
        "original_plan_content_sha256": selected["original_plan_content_sha256"],
        "selected": _selected_record(evidence, replacement_idempotency_key),
        "receipt_digest": receipt_digest,
    }


def claim_attempt(receipt: Mapping[str, Any] | Any, state_dir: str | Path) -> dict[str, Any]:
    """Atomically claim the one allowed attempt, without launching anything."""

    eligibility = evaluate_eligibility(receipt)
    if not eligibility.eligible:
        return {"status": "ineligible", "eligibility": eligibility.as_dict()}
    try:
        path = _attempt_path(Path(state_dir))
        record = _claim_record(receipt)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        return {"status": "blocked", "reason": str(exc)}

    try:
        _write_exclusive(path, record)
    except FileExistsError:
        try:
            existing = _read_attempt(path)
        except ValueError as exc:
            return {"status": "blocked", "reason": str(exc)}
        if existing is None:
            return {"status": "blocked", "reason": "attempt record disappeared during claim"}
        if existing.get("receipt_digest") != record.get("receipt_digest"):
            return {
                "status": "blocked",
                "reason": "matched launch evidence changed after the attempt was claimed",
            }
        return {"status": "already_attempted", "attempt_path": str(path), "record": existing}
    except (OSError, ValueError) as exc:
        return {"status": "blocked", "reason": str(exc)}
    return {
        "status": "claimed",
        "attempt_path": str(path),
        "record": record,
        "launch_request": {
            **receipt["evidence"]["selected_launch"],
            "idempotency_key": record["request"]["replacement_idempotency_key"],
        },
    }


def _successor_identity(value: Any) -> dict[str, str] | None:
    successor = _mapping(value)
    if successor is None:
        return None
    safe: dict[str, str] = {}
    for key in ("run_id", "startup_request_id", "idempotency_key"):
        item = successor.get(key)
        if item is not None:
            if not _nonempty_text(item, identity=True):
                return None
            safe[key] = item
    if not safe or not (safe.get("run_id") or safe.get("startup_request_id")):
        return None
    return safe


def record_outcome(
    state_dir: str | Path,
    replacement_idempotency_key: str,
    outcome: str,
    successor: Mapping[str, Any] | Any = None,
) -> dict[str, Any]:
    """Record one claimed launch outcome without issuing another launch."""

    try:
        if outcome not in OUTCOME_STATUSES:
            return {"status": "blocked", "reason": "launch outcome is unsupported"}
        path = _attempt_path(Path(state_dir))
        record = _read_attempt(path)
        if record is None:
            return {"status": "blocked", "reason": "no durable recovery attempt exists"}
        if record["request"].get("replacement_idempotency_key") != replacement_idempotency_key:
            return {
                "status": "blocked",
                "reason": "replacement idempotency key does not match the claimed attempt",
            }

        safe_successor = None
        if outcome == "acknowledged":
            safe_successor = _successor_identity(successor)
            if safe_successor is None:
                return {
                    "status": "blocked",
                    "reason": "successor identity is missing or malformed",
                }
            if (
                safe_successor.get("idempotency_key") is not None
                and safe_successor["idempotency_key"] != replacement_idempotency_key
            ):
                return {
                    "status": "blocked",
                    "reason": "successor idempotency key does not match the replacement",
                }
        elif successor is not None:
            return {
                "status": "blocked",
                "reason": "only an acknowledged outcome may include a successor",
            }

        current_status = record.get("status")
        if current_status == "acknowledged":
            if outcome == "acknowledged" and record.get("successor") == safe_successor:
                return {"status": "already_acknowledged", "record": record}
            return {
                "status": "blocked",
                "reason": "conflicting launch outcome or successor is already recorded",
            }
        if current_status == "failed":
            if outcome == "failed":
                return {"status": "already_failed", "record": record}
            return {
                "status": "blocked",
                "reason": "a failed launch outcome is already recorded",
            }
        if current_status == "uncertain":
            if outcome == "uncertain":
                return {"status": "already_uncertain", "record": record}
            if outcome != "acknowledged":
                return {
                    "status": "blocked",
                    "reason": "an uncertain launch may only be acknowledged with its replacement key",
                }
        elif current_status != "claimed":
            return {"status": "blocked", "reason": "attempt record is not claimable"}

        updated = dict(record)
        updated["status"] = outcome
        updated[f"{outcome}_at"] = _utc_now()
        if outcome == "acknowledged":
            updated["successor"] = safe_successor
        _write_replacement(path, updated)
        return {"status": outcome, "attempt_path": str(path), "record": updated}
    except (OSError, TypeError, ValueError, KeyError) as exc:
        return {"status": "blocked", "reason": str(exc)}


def record_acknowledged_successor(
    state_dir: str | Path,
    replacement_idempotency_key: str,
    successor: Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Backward-compatible wrapper for a matching acknowledged outcome."""

    return record_outcome(
        state_dir,
        replacement_idempotency_key,
        "acknowledged",
        successor,
    )


def attempt_recovery(
    receipt: Mapping[str, Any] | Any,
    state_dir: str | Path,
    launch: Callable[[dict[str, Any]], Mapping[str, Any] | Any],
) -> dict[str, Any]:
    """Claim once, then invoke the caller's normal launch surface exactly once."""

    claimed = claim_attempt(receipt, state_dir)
    if claimed.get("status") != "claimed":
        return claimed
    try:
        response = launch(dict(claimed["launch_request"]))
    except Exception as exc:  # The launch boundary is intentionally fail-closed.
        response = {"status": "uncertain", "error_type": type(exc).__name__}

    if not isinstance(response, Mapping):
        outcome = "uncertain"
        successor = None
    else:
        acknowledged = response.get("acknowledged") is True or response.get("status") == "acknowledged"
        successor = _successor_identity(response.get("successor")) if acknowledged else None
        if acknowledged and successor is not None:
            if (
                successor.get("idempotency_key") is not None
                and successor["idempotency_key"]
                != claimed["record"]["request"]["replacement_idempotency_key"]
            ):
                outcome = "uncertain"
                successor = None
            else:
                outcome = "acknowledged"
        elif response.get("status") == "failed":
            outcome = "failed"
        else:
            outcome = "uncertain"
    result = record_outcome(
        state_dir,
        claimed["record"]["request"]["replacement_idempotency_key"],
        outcome,
        successor,
    )
    if result.get("status") == "blocked" and "reason" not in result:
        result["reason"] = "unable to persist launch outcome"
    return result


def _read_receipt(path: Path) -> Mapping[str, Any]:
    value = _bounded_json(path, MAX_RECEIPT_BYTES)
    if not isinstance(value, Mapping):
        raise ValueError("receipt must contain a JSON object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and atomically claim the bounded neutral guard exception."
    )
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument(
        "--record-acknowledged-successor",
        type=Path,
        metavar="JSON",
        help="Compatibility alias for an acknowledged outcome; never launches.",
    )
    parser.add_argument(
        "--outcome",
        choices=sorted(OUTCOME_STATUSES),
        help="Record one external launch outcome after the durable claim; never launches.",
    )
    parser.add_argument(
        "--replacement-idempotency-key",
        help="The replacement key returned by the durable claim record.",
    )
    parser.add_argument(
        "--successor-json",
        type=Path,
        metavar="JSON",
        help="JSON successor identity for an acknowledged outcome; never launches.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        receipt = _read_receipt(args.receipt)
        if args.record_acknowledged_successor is not None:
            if (
                args.outcome is not None
                or args.replacement_idempotency_key is not None
                or args.successor_json is not None
            ):
                result = {
                    "status": "blocked",
                    "reason": "legacy and explicit outcome options cannot be combined",
                }
            else:
                successor = _bounded_json(
                    args.record_acknowledged_successor, MAX_RECEIPT_BYTES
                )
                evidence = receipt.get("evidence")
                request = evidence.get("request") if isinstance(evidence, Mapping) else None
                predecessor_key = (
                    request.get("idempotency_key") if isinstance(request, Mapping) else ""
                )
                replacement_key = _replacement_idempotency_key(
                    _canonical_digest(receipt),
                    str(predecessor_key),
                )
                result = record_outcome(
                    args.state_dir,
                    replacement_key,
                    "acknowledged",
                    successor,
                )
        elif args.outcome is not None:
            if not args.replacement_idempotency_key:
                result = {
                    "status": "blocked",
                    "reason": "replacement idempotency key is required for an outcome",
                }
            elif args.outcome == "acknowledged" and args.successor_json is None:
                result = {
                    "status": "blocked",
                    "reason": "acknowledged outcome requires successor JSON",
                }
            elif args.outcome != "acknowledged" and args.successor_json is not None:
                result = {
                    "status": "blocked",
                    "reason": "only an acknowledged outcome accepts successor JSON",
                }
            else:
                successor = (
                    _bounded_json(args.successor_json, MAX_RECEIPT_BYTES)
                    if args.successor_json is not None
                    else None
                )
                result = record_outcome(
                    args.state_dir,
                    args.replacement_idempotency_key,
                    args.outcome,
                    successor,
                )
        else:
            result = claim_attempt(receipt, args.state_dir)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        result = {"status": "blocked", "reason": str(exc)}
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") in {
        "claimed",
        "already_attempted",
        "ineligible",
        "acknowledged",
        "already_acknowledged",
        "failed",
        "already_failed",
        "uncertain",
        "already_uncertain",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
