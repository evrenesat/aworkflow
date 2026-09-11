#!/usr/bin/env python3
"""Build one bounded, mechanically assembled input for a guard report.

The helper accepts a single ownership-matched snapshot and optional structured
evidence supplied by that observer.  It never queries a provider, scans a
transcript, changes the guarded repository, or authorizes recovery.  The
result is deliberately a small JSON contract for a later renderer.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping


SNAPSHOT_SCHEMA_VERSION = 3
CANONICAL_RUN_SCHEMA_VERSION = 1
REPORT_INPUT_SCHEMA_VERSION = 1
MAX_REPORT_INPUT_BYTES = 64 * 1024
MAX_STRING_LENGTH = 1_024
MAX_ARRAY_ITEMS = 8
MAX_CAUSE_SOURCE_BYTES = 4 * 1024
CAUSE_WINDOW_SECONDS = 10 * 60

_ACTIVE_TURN_STATUSES = frozenset({"starting", "running", "active", "in_progress"})
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SECRET_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*bearer\s+|"
    r"(?:token|secret|password|api[_-]?key)\s*[:=]\s*[\"']?)([^\s,\"']+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,]+")
_UNREDACTED_SECRET_RE = re.compile(
    r"(?i)(?:authorization\s*[:=]\s*bearer\s+|"
    r"(?:token|secret|password|api[_-]?key)\s*[:=]\s*[\"']?)(?!\[redacted\])"
    r"[^\s,\"']+"
)
_UNREDACTED_BEARER_RE = re.compile(r"(?i)\bbearer\s+(?!\[redacted\])[^\s,]+")
_SECRET_KEY_PARTS = (
    "authorization",
    "credential",
    "cookie",
    "password",
    "secret",
    "session_id",
    "token",
    "api_key",
)
_CAUSE_KEYS = (
    "controller_exit",
    "host_termination",
    "harness_session",
    "failure_boundary",
)
_TERMINAL_SESSION_STATUSES = frozenset({
    "failed",
    "failure",
    "killed",
    "terminated",
    "interrupted",
    "crashed",
    "exited",
    "error",
})
_CANONICAL_OWNERSHIP_MODES = frozenset({"ui-server", "aflowd"})
_FINALIZED_RESULT_STATUSES = frozenset({
    "completed",
    "complete",
    "success",
    "succeeded",
    "failed",
    "failure",
    "error",
    "cancelled",
    "canceled",
    "interrupted",
    "stopped",
    "terminated",
    "rejected",
    "blocked",
})
_HEALTHY_REPORT_STATUSES = frozenset({"completed", "complete", "success", "succeeded"})


class ReportInputError(ValueError):
    """A snapshot or report input cannot satisfy the bounded contract."""


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _redact_text(text: str) -> str:
    """Reuse the repository's redaction implementation when installed."""
    try:
        from aflow.control_plane.models import startup_failure

        result = startup_failure("guard_report", text)
        message = result.get("message") if isinstance(result, Mapping) else None
        if isinstance(message, str):
            return message
    except (ImportError, OSError, TypeError, ValueError):
        pass
    text = _SECRET_RE.sub(r"\1[redacted]", text)
    return _BEARER_RE.sub("Bearer [redacted]", text)


def _safe_text(value: object, *, key: str | None = None) -> str | None:
    if not isinstance(value, str):
        return None
    if key is not None and any(part in key.casefold() for part in _SECRET_KEY_PARTS):
        return "[redacted]"
    text = _redact_text(" ".join(value.split()).strip())
    if not text:
        return None
    if len(text) > MAX_STRING_LENGTH:
        return text[: MAX_STRING_LENGTH - 1] + "…"
    return text


def _safe_identity_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ReportInputError(f"{label} is missing or malformed")
    if len(value) > MAX_STRING_LENGTH:
        raise ReportInputError(f"{label} is too long")
    return value


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _safe_timestamp(value: object, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ReportInputError("observation time is missing")
        return None
    if not isinstance(value, str) or _parse_timestamp(value) is None:
        raise ReportInputError("observation time is not a timezone-aware ISO timestamp")
    return value.strip()


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ReportInputError("report input contains non-JSON evidence") from exc


def _bounded_strings(values: object) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [
        text
        for value in list(values)[:MAX_ARRAY_ITEMS]
        if (text := _safe_text(value)) is not None
    ]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _identity_from_snapshot(snapshot: Mapping[str, Any]) -> tuple[Path, str]:
    repository_value = snapshot.get("repository", snapshot.get("repo"))
    repository = _safe_identity_text(repository_value, "repository")
    repository_path = Path(repository).expanduser()
    if not repository_path.is_absolute():
        raise ReportInputError("repository must be an absolute path")
    try:
        repository_root = repository_path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ReportInputError("repository path cannot be resolved") from exc

    run_id = _safe_identity_text(snapshot.get("run_id"), "run_id")
    if not _IDENTITY_RE.fullmatch(run_id):
        raise ReportInputError("run_id is not a safe pinned identity")
    return repository_root, run_id


def _identity_matches(
    value: Mapping[str, Any], repository: Path, run_id: str
) -> bool:
    nested = _mapping(value.get("identity"))
    if nested is not None:
        value = nested
    supplied_run_id = value.get("run_id")
    if supplied_run_id is not None and supplied_run_id != run_id:
        return False
    supplied_repository = next(
        (
            value.get(key)
            for key in ("repository", "repo", "repository_root", "project_root")
            if value.get(key) is not None
        ),
        None,
    )
    if supplied_repository is None:
        return True
    if not isinstance(supplied_repository, str) or "\x00" in supplied_repository:
        return False
    try:
        return Path(supplied_repository).expanduser().resolve(strict=False) == repository
    except (OSError, RuntimeError, ValueError):
        return False


def _has_explicit_identity(value: Mapping[str, Any]) -> bool:
    identity = _mapping(value.get("identity"))
    candidate = identity if identity is not None else value
    return candidate.get("run_id") is not None and any(
        candidate.get(key) is not None
        for key in ("repository", "repo", "repository_root", "project_root")
    )


def _ownership_family(value: object) -> str | None:
    if value == "legacy":
        return "legacy"
    if value in {"ui-server", "aflowd", "control_plane"}:
        return "server"
    return None


def _canonical_observation(
    snapshot: Mapping[str, Any], repository: Path, run_id: str
) -> tuple[Mapping[str, Any], bool]:
    for key in ("canonical_observation", "canonical_run", "run_status"):
        candidate = _mapping(snapshot.get(key))
        if candidate is not None:
            if not _has_explicit_identity(candidate):
                raise ReportInputError(f"{key} is missing the pinned identity")
            if not _identity_matches(candidate, repository, run_id):
                raise ReportInputError(f"{key} does not match the pinned identity")
            if not isinstance(candidate.get("status"), str) or not isinstance(
                candidate.get("activity"), str
            ):
                raise ReportInputError(f"{key} is missing canonical status or activity")
            pinned_family = _ownership_family(
                snapshot.get("ownership_mode", snapshot.get("ownership"))
            )
            observed_family = _ownership_family(candidate.get("ownership"))
            if pinned_family is not None and observed_family is not None and pinned_family != observed_family:
                raise ReportInputError(f"{key} does not match the pinned ownership")
            return candidate, True

    ownership_hint = snapshot.get("ownership_mode", snapshot.get("ownership"))
    if ownership_hint in {"ui-server", "aflowd", "control_plane"}:
        # A top-level canonical observation is accepted only when its liveness
        # fields are present.  Process counts are never a fallback here.
        if not isinstance(snapshot.get("status"), str) or not isinstance(
            snapshot.get("activity"), str
        ):
            raise ReportInputError(
                "server-owned snapshots require canonical status and activity"
            )
        return snapshot, True
    return snapshot, False


def _activity(
    snapshot: Mapping[str, Any],
    observation: Mapping[str, Any],
    *,
    canonical: bool,
) -> str:
    if not canonical and snapshot.get("ownership_mode", snapshot.get("ownership")) != "legacy":
        return "unavailable"
    value = observation.get("activity", snapshot.get("activity"))
    if isinstance(value, str) and value in {"active", "inactive", "unknown", "unavailable"}:
        return value
    if canonical:
        return "unknown"
    classification = snapshot.get("classification")
    if classification in {"active_progress", "active_waiting_child", "active_waiting"}:
        return "active"
    if classification in {
        "orphaned_controller",
        "terminal_success",
        "terminal_failed",
        "terminal_incomplete",
    }:
        return "inactive"
    return "unknown"


def _run_mapping(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    run = _mapping(snapshot.get("run"))
    return run if run is not None else {}


def _progress_values(
    snapshot: Mapping[str, Any],
    observation: Mapping[str, Any],
    run: Mapping[str, Any],
) -> tuple[int | None, str | None, int | None, str | None, Mapping[str, Any] | None, Mapping[str, Any] | None]:
    progress = _mapping(snapshot.get("progress"))
    if progress is None:
        progress = _mapping(observation.get("progress"))
    if progress is None:
        progress = _mapping(run.get("progress"))
    progress = progress or {}

    checkpoint = _mapping(progress.get("checkpoint"))
    index_value = progress.get("checkpoint_index")
    name_value = progress.get("checkpoint_name")
    total_value = progress.get("checkpoint_total", progress.get("total"))
    if checkpoint is not None:
        index_value = checkpoint.get("index", index_value)
        name_value = checkpoint.get("name", name_value)
    last_snapshot = _mapping(run.get("last_snapshot"))
    if last_snapshot is not None:
        if index_value is None:
            index_value = last_snapshot.get("current_checkpoint_index")
        if name_value is None:
            name_value = last_snapshot.get("current_checkpoint_name")
        if total_value is None:
            total_value = last_snapshot.get("total_checkpoint_count")

    index = index_value if _is_int(index_value) and index_value > 0 else None
    total = total_value if _is_int(total_value) and total_value > 0 else None
    if index is not None and total is not None and index > total:
        index = None
        name_value = None
    name = _safe_text(name_value, key="checkpoint_name")

    current_turn = _mapping(progress.get("current_turn"))
    if current_turn is None:
        current_turn = _mapping(observation.get("current_turn"))
    last_finished = _mapping(progress.get("last_finished_turn"))
    if last_finished is None:
        last_finished = _mapping(observation.get("last_finished_turn"))
    step_value = None
    if current_turn is not None:
        step_value = current_turn.get("step", current_turn.get("step_name"))
    if step_value is None:
        step_value = observation.get("current_step", observation.get("current_step_name"))
    if step_value is None:
        step_value = run.get("current_step", run.get("current_step_name"))
    return index, name, total, _safe_text(step_value, key="step"), current_turn, last_finished


def _result_is_finalized(result: Mapping[str, Any]) -> bool:
    status = result.get("status")
    normalized = status.strip().casefold() if isinstance(status, str) else ""
    return (
        normalized in _FINALIZED_RESULT_STATUSES
        and normalized not in _ACTIVE_TURN_STATUSES
        and _parse_timestamp(result.get("finished_at")) is not None
    )


def _result_path(
    repository: Path, run_id: str, latest: Mapping[str, Any]
) -> Path | None:
    run_dir = repository / ".aflow" / "runs" / run_id
    try:
        if run_dir.is_symlink() or not _inside(run_dir.resolve(strict=False), repository):
            return None
    except (OSError, RuntimeError):
        return None
    relative = latest.get("path")
    if not isinstance(relative, str) or not relative:
        name = latest.get("name")
        if not isinstance(name, str) or re.fullmatch(r"turn-[0-9]{1,9}", name) is None:
            return None
        relative = f"turns/{name}/result.json"
    candidate = run_dir / relative
    try:
        resolved = candidate.resolve(strict=False)
        relative_to_run = resolved.relative_to(run_dir.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return None
    if (
        len(relative_to_run.parts) != 3
        or relative_to_run.parts[0] != "turns"
        or re.fullmatch(r"turn-[0-9]{1,9}", relative_to_run.parts[1]) is None
        or relative_to_run.parts[2] != "result.json"
        or candidate.is_symlink()
    ):
        return None
    return resolved


def _read_finalized_result(
    snapshot: Mapping[str, Any],
    repository: Path,
    run_id: str,
    supplied: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    latest = _mapping(snapshot.get("latest_result"))
    if supplied is not None:
        if not isinstance(supplied, Mapping) or not _has_explicit_identity(supplied):
            raise ReportInputError("finalized evidence is missing the pinned identity")
        if not _identity_matches(supplied, repository, run_id):
            raise ReportInputError(
                "finalized evidence run_id or repository does not match the pinned identity"
            )
        return supplied if _result_is_finalized(supplied) else None

    if latest is None or latest.get("finalized") is False:
        latest = None
    if latest is None:
        return None
    if not _identity_matches(latest, repository, run_id):
        raise ReportInputError(
            "compact finalized result does not match the pinned identity"
        )
    if not _result_is_finalized(latest):
        return None

    path = _result_path(repository, run_id, latest)
    if path is not None and path.is_file() and not path.is_symlink():
        try:
            if path.stat().st_size <= MAX_REPORT_INPUT_BYTES:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, Mapping):
                    if not _identity_matches(value, repository, run_id):
                        raise ReportInputError(
                            "finalized result does not match the pinned identity"
                        )
                    if _result_is_finalized(value):
                        return value
                    return None
        except ReportInputError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    return latest


def _result_summary(result: Mapping[str, Any] | None) -> str | None:
    if result is None:
        return None
    for key in ("summary", "finalized_turn_summary", "semantic_summary"):
        summary = _safe_text(result.get(key), key=key)
        if summary is not None:
            return summary
    parts: list[str] = []
    status = _safe_text(result.get("status"), key="status")
    if status is not None:
        parts.append(status)
    for key, label in (("verdict", "verdict"), ("role", "role"), ("step", "step"), ("step_name", "step")):
        value = _safe_text(result.get(key), key=key)
        if value is not None and f"{label}={value}" not in parts:
            parts.append(f"{label}={value}")
    error = _safe_text(result.get("error"), key="error")
    if error is not None and status not in {"completed", "success"}:
        parts.append(error)
    return _safe_text("; ".join(parts), key="finalized_turn_summary") if parts else None


def _worker_mapping(
    snapshot: Mapping[str, Any], observation: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    for source in (observation, snapshot):
        worker = _mapping(source.get("worker"))
        if worker is not None:
            return worker
        evidence = _mapping(source.get("evidence"))
        if evidence is not None:
            worker = _mapping(evidence.get("worker"))
            if worker is not None:
                return worker
        worker_exit = _mapping(source.get("worker_exit"))
        if worker_exit is not None:
            return worker_exit
    return None


def _worker_values(
    snapshot: Mapping[str, Any],
    observation: Mapping[str, Any],
    result: Mapping[str, Any] | None,
) -> tuple[str | None, str | None, str | None]:
    worker = _worker_mapping(snapshot, observation)
    if worker is None:
        worker = result
    worker = worker or {}

    def value(*keys: str) -> str | None:
        for key in keys:
            text = _safe_text(worker.get(key), key=key)
            if text is not None:
                return text
        return None

    return (
        value("selector", "worker_selector"),
        value("model", "worker_model", "resolved_model"),
        value("effort", "worker_effort", "resolved_effort"),
    )


def _incident_time(
    snapshot: Mapping[str, Any],
    observation: Mapping[str, Any],
    run: Mapping[str, Any],
    result: Mapping[str, Any] | None,
    observed_at: str | None,
) -> datetime | None:
    for source in (snapshot, observation, run, result or {}):
        for key in ("incident_at", "ended_at", "finished_at", "exited_at", "at"):
            parsed = _parse_timestamp(source.get(key))
            if parsed is not None:
                return parsed
    return _parse_timestamp(observed_at)


def _source_mapping(
    snapshot: Mapping[str, Any],
    observation: Mapping[str, Any],
    key: str,
    *,
    repository: Path,
    run_id: str,
) -> Mapping[str, Any] | None:
    diagnostic = _mapping(snapshot.get("diagnostic_evidence"))
    if diagnostic is not None:
        value = diagnostic.get(key)
        return value if isinstance(value, Mapping) else None
    value = None
    if not isinstance(value, Mapping):
        value = observation.get(key)
    if not isinstance(value, Mapping):
        value = snapshot.get(key)
    if isinstance(value, Mapping):
        return value
    if key != "controller_exit":
        return None
    ownership = observation.get("ownership", snapshot.get("ownership_mode"))
    if ownership not in {"control_plane", "ui-server", "aflowd"}:
        return None
    worker = _worker_mapping(snapshot, observation)
    if worker is None:
        worker = _mapping(observation.get("worker_exit"))
    if worker is None:
        evidence = _mapping(observation.get("evidence"))
        worker = _mapping(evidence.get("worker_exit")) if evidence is not None else None
    code = worker.get("exit_code") if worker is not None else None
    exited_at = worker.get("exited_at") if worker is not None else None
    if not _is_int(code) or not isinstance(exited_at, str):
        return None
    unit = observation.get("unit_name", snapshot.get("unit_name"))
    if unit is not None and unit != f"aflow-run-{run_id}.service":
        return None
    return {
        "run_id": run_id,
        "repository": str(repository),
        "unit": f"aflow-run-{run_id}.service",
        "exit_code": code,
        "at": exited_at,
        "reason": worker.get("reason"),
    }


def _source_is_usable(
    source: Mapping[str, Any],
    *,
    repository: Path,
    run_id: str,
    incident_at: datetime | None,
) -> bool:
    if source.get("available") is False or source.get("permission") in {"denied", "unavailable"}:
        return False
    identity = _mapping(source.get("identity")) or source
    if (
        identity.get("run_id") is None
        or not any(
            identity.get(key) is not None
            for key in ("repository", "repo", "repository_root", "project_root")
        )
        or source.get("matched") is False
        or not _identity_matches(source, repository, run_id)
    ):
        return False
    if incident_at is None:
        return False
    source_at = None
    for key in ("at", "timestamp", "exited_at", "finished_at"):
        source_at = _parse_timestamp(source.get(key))
        if source_at is not None:
            break
    if source_at is None or abs((source_at - incident_at).total_seconds()) > CAUSE_WINDOW_SECONDS:
        return False
    try:
        return len(_json_bytes(source)) <= MAX_CAUSE_SOURCE_BYTES
    except ReportInputError:
        return False


def _evidence_text(source: Mapping[str, Any], label: str) -> str:
    detail = None
    for key in ("reason", "message", "status", "kind", "stage"):
        detail = _safe_text(source.get(key), key=key)
        if detail is not None:
            break
    at = next(
        (_safe_text(source.get(key), key=key) for key in ("at", "timestamp", "exited_at") if source.get(key) is not None),
        None,
    )
    suffix = f" at {at}" if at is not None else ""
    return f"{label}{(': ' + detail) if detail else ''}{suffix}"


def _diagnosis(
    snapshot: Mapping[str, Any],
    observation: Mapping[str, Any],
    *,
    repository: Path,
    run_id: str,
    status: str | None,
    activity: str,
    ownership: str,
    progress_available: bool,
    finalized_summary: str | None,
    result: Mapping[str, Any] | None,
    observed_at: str | None,
) -> dict[str, Any]:
    classification = snapshot.get("classification")
    facts: list[str] = []
    for label, value in (
        ("Status", status),
        ("Activity", activity),
        ("Ownership", ownership),
    ):
        if value not in {None, "unavailable"}:
            facts.append(f"{label}: {value}")
    if isinstance(classification, str) and classification:
        facts.append(f"Snapshot classification: {_safe_text(classification)}")
    status_reason = _safe_text(observation.get("status_reason_code"), key="status_reason_code")
    if status_reason is not None:
        facts.append(f"Canonical status reason: {status_reason}")
    canonical_evidence = _mapping(observation.get("evidence"))
    if canonical_evidence is not None:
        for key, label in (
            ("reason", "Canonical evidence"),
            ("message", "Canonical evidence"),
            ("failure_reason", "Canonical failure evidence"),
        ):
            detail = _safe_text(canonical_evidence.get(key), key=key)
            if detail is not None:
                facts.append(f"{label}: {detail}")
                break
    if progress_available:
        facts.append("Checkpoint progress was available in the ownership-matched observation.")
    else:
        facts.append("Checkpoint progress was unavailable in the ownership-matched observation.")

    unknowns: list[str] = []
    if finalized_summary is None:
        unknowns.append("No finalized worker summary was available.")
    if activity in {"unknown", "unavailable"}:
        unknowns.append("Current activity could not be confirmed.")
    if not progress_available:
        unknowns.append("Checkpoint index, name, or total could not be confirmed.")

    new_orphan = (
        classification == "orphaned_controller"
        and snapshot.get("changed_since_previous") is not False
        and snapshot.get("notification_already_sent") is not True
    )
    supporting: list[str] = []
    contradicting: list[str] = []
    causes: list[tuple[str, str]] = []
    if new_orphan:
        incident_at = _incident_time(
            snapshot, observation, _run_mapping(snapshot), result, observed_at
        )
        for key, label in (
            ("controller_exit", "Matching controller/unit exit evidence"),
            ("host_termination", "Host termination/OOM evidence"),
            ("harness_session", "Supported harness session terminal status"),
            ("failure_boundary", "Recorded controller failure boundary"),
        ):
            source = _source_mapping(
                snapshot,
                observation,
                key,
                repository=repository,
                run_id=run_id,
            )
            if source is None:
                unknowns.append(f"{label} was unavailable or was not supplied.")
                continue
            if not _source_is_usable(
                source,
                repository=repository,
                run_id=run_id,
                incident_at=incident_at,
            ):
                unknowns.append(f"{label} was unavailable or did not match the pinned incident.")
                continue
            if key == "controller_exit":
                unit = source.get("unit", source.get("unit_name"))
                if unit != f"aflow-run-{run_id}.service":
                    unknowns.append(
                        f"{label} was unavailable or did not match the pinned unit."
                    )
                    continue
            if key == "failure_boundary" and source.get("recorded") is not True:
                unknowns.append(
                    f"{label} was unavailable or was not a recorded controller boundary."
                )
                continue
            if isinstance(source.get("contradicting_evidence"), (list, tuple)):
                contradicting.extend(_bounded_strings(source["contradicting_evidence"]))
            if isinstance(source.get("contradicts"), (list, tuple)):
                contradicting.extend(_bounded_strings(source["contradicts"]))
            evidence = _evidence_text(source, label)
            supporting.append(evidence)

            if key == "controller_exit":
                code = source.get("returncode", source.get("exit_code"))
                if _is_int(code) and code != 0:
                    causes.append((f"controller/unit exited with code {code}", "confirmed"))
                elif code == 0:
                    contradicting.append(
                        "Matching controller/unit evidence reports exit code 0; it does not establish a failure cause."
                    )
            elif key == "host_termination":
                kind = _safe_text(source.get("kind"), key="kind")
                reason = _safe_text(source.get("reason"), key="reason")
                combined = f"{kind or ''} {reason or ''}".casefold()
                if source.get("confirmed") is True and any(
                    marker in combined for marker in ("oom", "out of memory", "terminated", "killed")
                ):
                    causes.append((reason or kind or "host termination was recorded", "confirmed"))
            elif key == "harness_session":
                terminal = _safe_text(
                    source.get("terminal_status", source.get("status")), key="status"
                )
                if (
                    source.get("supported") is True
                    and source.get("terminal") is True
                    and terminal is not None
                    and terminal.casefold() in _TERMINAL_SESSION_STATUSES
                ):
                    causes.append((f"harness session ended with status {terminal}", "confirmed"))
            elif key == "failure_boundary":
                # A boundary proves where the controller stopped, not why the
                # provider or host stopped.  Keep that distinction visible.
                continue
        if not supporting:
            unknowns.append("No identity-matched cause evidence was available within the incident window.")
    elif classification == "orphaned_controller":
        unknowns.append("Cause inspection is limited to the first report for a new orphan anomaly.")

    likely_cause: str | None = None
    confidence = "unknown"
    if causes:
        likely_cause, confidence = causes[0]
    elif classification == "orphaned_controller":
        unknowns.append("The cause remains unknown; absence of an accessible source is not failure certainty.")

    provider = _mapping(snapshot.get("provider_operation"))
    if provider is None:
        provider = _mapping(observation.get("provider_operation"))
    if provider is None and canonical_evidence is not None:
        provider = _mapping(canonical_evidence.get("provider_operation"))
    provider_status = provider.get("status") if provider is not None else None
    provider_known = provider_status in {"completed", "failed", "acknowledged", "cancelled"}
    provider_unknown = snapshot.get("provider_operation_unknown") is True or (
        provider is not None and provider_status in {None, "unknown", "uncertain", "in_progress"}
    )
    canonical_missing_unit = (
        ownership == "control_plane"
        and str(status or "").casefold() == "needs_attention"
        and status_reason == "unit_missing"
        and canonical_evidence is not None
        and canonical_evidence.get("recorded_status") == "running"
        and canonical_evidence.get("has_run_metadata") is True
        and canonical_evidence.get("unit_active") is False
        and canonical_evidence.get("unit_observation") == "missing"
    )
    duplicate_risk: bool | None = None
    is_orphan = classification == "orphaned_controller"
    if is_orphan or canonical_missing_unit:
        duplicate_risk = not provider_known
    elif provider_known:
        duplicate_risk = False
    elif provider_unknown:
        duplicate_risk = None

    if duplicate_risk is True:
        owner_action = (
            "Inspect the exact recorded provider operation and choose an owner action; "
            "do not relaunch blindly."
        )
        facts.append("Provider-operation completion is unknown; duplicate-operation risk is present.")
        if canonical_missing_unit:
            unknowns.append(
                "The missing unit does not establish why execution stopped."
            )
    elif classification in {"orphaned_controller", "unsafe_duplicate_controllers", "unsafe_inconsistent"}:
        owner_action = (
            "Inspect the bounded ownership evidence and choose the documented owner action; "
            "the guard does not authorize recovery."
        )
    elif (
        classification in {"terminal_failed", "terminal_incomplete"}
        or str(status or "").casefold()
        in {"failed", "failure", "error", "incomplete", "needs_attention"}
        or status_reason in {"needs_attention", "terminal_failed", "terminal_incomplete"}
    ):
        owner_action = (
            "Inspect the failed turn or ownership evidence and decide the next step; "
            "the guard does not authorize recovery."
        )
    elif status == "awaiting_startup_answer" or status_reason == "startup_answer_required":
        owner_action = (
            "Inspect and answer the existing pinned startup question through the owning interface; "
            "the guard does not authorize input execution."
        )
    elif status == "waiting_for_input":
        owner_action = (
            "Inspect and supply the requested input through the owning interface; "
            "the guard does not authorize input execution."
        )
    elif status == "waiting_for_valid_override":
        owner_action = (
            "Inspect and correct the requested override through the owning interface; "
            "the guard does not authorize input execution."
        )
    elif status is None or activity in {"unknown", "unavailable"}:
        owner_action = "Establish the missing ownership or evidence before choosing an action."
    elif (
        str(status or "").casefold() in _HEALTHY_REPORT_STATUSES
        or (str(status or "").casefold() == "running" and activity == "active")
    ):
        owner_action = "No owner action is indicated by this observation."
    else:
        owner_action = (
            "Inspect the bounded status and ownership evidence and decide the next step; "
            "the guard does not authorize recovery."
        )

    return {
        "confirmed_facts": _bounded_strings(facts),
        "likely_cause": _safe_text(likely_cause, key="likely_cause"),
        "supporting_evidence": _bounded_strings(supporting),
        "contradicting_evidence": _bounded_strings(contradicting),
        "unknowns": _bounded_strings(unknowns),
        "confidence": confidence,
        "duplicate_operation_risk": duplicate_risk,
        "owner_action": _safe_text(owner_action, key="owner_action"),
    }


_REPORT_KEYS = frozenset({
    "schema_version",
    "repository",
    "run_id",
    "observed_at",
    "status",
    "activity",
    "ownership",
    "checkpoint_index",
    "checkpoint_name",
    "checkpoint_total",
    "step",
    "finalized_turn_summary",
    "worker_selector",
    "worker_model",
    "worker_effort",
    "diagnosis",
})
_DIAGNOSIS_KEYS = frozenset({
    "confirmed_facts",
    "likely_cause",
    "supporting_evidence",
    "contradicting_evidence",
    "unknowns",
    "confidence",
    "duplicate_operation_risk",
    "owner_action",
})


def _validate_text_field(value: object, label: str, *, nullable: bool = True) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value or len(value) > MAX_STRING_LENGTH or "\x00" in value:
        raise ReportInputError(f"{label} is not bounded text")


def validate_report_input(
    value: object,
    *,
    expected_repository: Path | str | None = None,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    """Validate one normalized report object without changing it."""
    root = _mapping(value)
    if root is None or set(root) != _REPORT_KEYS:
        raise ReportInputError("report input has an unsupported schema shape")
    if root.get("schema_version") != REPORT_INPUT_SCHEMA_VERSION:
        raise ReportInputError("report input schema version is unsupported")

    repository = root.get("repository")
    _validate_text_field(repository, "repository", nullable=False)
    assert isinstance(repository, str)
    repository_path = Path(repository)
    if not repository_path.is_absolute():
        raise ReportInputError("report repository must be absolute")
    if expected_repository is not None:
        expected = Path(expected_repository).expanduser().resolve(strict=False)
        if repository_path.resolve(strict=False) != expected:
            raise ReportInputError("report repository does not match the pinned repository")

    run_id = root.get("run_id")
    _validate_text_field(run_id, "run_id", nullable=False)
    assert isinstance(run_id, str)
    if not _IDENTITY_RE.fullmatch(run_id):
        raise ReportInputError("report run_id is not a safe identity")
    if expected_run_id is not None and run_id != expected_run_id:
        raise ReportInputError("report run_id does not match the pinned run")

    _safe_timestamp(root.get("observed_at"))
    for key in (
        "status",
        "ownership",
        "checkpoint_name",
        "step",
        "finalized_turn_summary",
        "worker_selector",
        "worker_model",
        "worker_effort",
    ):
        _validate_text_field(root.get(key), key)
    if root.get("activity") not in {"active", "inactive", "unknown", "unavailable"}:
        raise ReportInputError("activity is not a supported value")
    for key in ("checkpoint_index", "checkpoint_total"):
        value_item = root.get(key)
        if value_item is not None and (not _is_int(value_item) or value_item < 1):
            raise ReportInputError(f"{key} is not a positive integer or null")
    index = root.get("checkpoint_index")
    total = root.get("checkpoint_total")
    if index is not None and total is not None and index > total:
        raise ReportInputError("checkpoint index exceeds checkpoint total")

    diagnosis = _mapping(root.get("diagnosis"))
    if diagnosis is None or set(diagnosis) != _DIAGNOSIS_KEYS:
        raise ReportInputError("diagnosis has an unsupported schema shape")
    for key in ("confirmed_facts", "supporting_evidence", "contradicting_evidence", "unknowns"):
        items = diagnosis.get(key)
        if not isinstance(items, list) or len(items) > MAX_ARRAY_ITEMS:
            raise ReportInputError(f"diagnosis.{key} is not a bounded array")
        for item in items:
            _validate_text_field(item, f"diagnosis.{key}", nullable=False)
    for key in ("likely_cause", "owner_action"):
        _validate_text_field(diagnosis.get(key), f"diagnosis.{key}")
    if diagnosis.get("confidence") not in {"confirmed", "likely", "unknown"}:
        raise ReportInputError("diagnosis.confidence is unsupported")
    risk = diagnosis.get("duplicate_operation_risk")
    if risk is not None and not isinstance(risk, bool):
        raise ReportInputError("diagnosis.duplicate_operation_risk must be boolean or null")

    encoded = _json_bytes(root)
    if len(encoded) > MAX_REPORT_INPUT_BYTES:
        raise ReportInputError("normalized report input exceeds 64 KiB")
    encoded_text = encoded.decode("utf-8", errors="replace")
    if _UNREDACTED_SECRET_RE.search(encoded_text) or _UNREDACTED_BEARER_RE.search(
        encoded_text
    ):
        raise ReportInputError("report input contains an unredacted secret")
    return dict(root)


def _normalized_report(
    *,
    repository: Path,
    run_id: str,
    observed_at: str | None,
    status: str | None,
    activity: str,
    ownership: str | None,
    checkpoint_index: int | None,
    checkpoint_name: str | None,
    checkpoint_total: int | None,
    step: str | None,
    finalized_turn_summary: str | None,
    worker_selector: str | None,
    worker_model: str | None,
    worker_effort: str | None,
    diagnosis: Mapping[str, Any],
) -> dict[str, Any]:
    return validate_report_input(
        {
            "schema_version": REPORT_INPUT_SCHEMA_VERSION,
            "repository": str(repository),
            "run_id": run_id,
            "observed_at": observed_at,
            "status": status,
            "activity": activity,
            "ownership": ownership,
            "checkpoint_index": checkpoint_index,
            "checkpoint_name": checkpoint_name,
            "checkpoint_total": checkpoint_total,
            "step": step,
            "finalized_turn_summary": finalized_turn_summary,
            "worker_selector": worker_selector,
            "worker_model": worker_model,
            "worker_effort": worker_effort,
            "diagnosis": dict(diagnosis),
        },
        expected_repository=repository,
        expected_run_id=run_id,
    )


def _pinned_canonical_identity(
    repository_value: Path | str | object,
    run_value: object,
    ownership_mode: object,
    observed_at: object,
) -> tuple[Path, str, str, str]:
    repository_text = _safe_identity_text(
        str(repository_value) if isinstance(repository_value, Path) else repository_value,
        "pinned repository",
    )
    repository = Path(repository_text).expanduser()
    if not repository.is_absolute():
        raise ReportInputError("pinned repository must be an absolute path")
    try:
        repository = repository.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ReportInputError("pinned repository cannot be resolved") from exc
    run_id = _safe_identity_text(run_value, "pinned run_id")
    if not _IDENTITY_RE.fullmatch(run_id):
        raise ReportInputError("pinned run_id is not a safe identity")
    if ownership_mode not in _CANONICAL_OWNERSHIP_MODES:
        raise ReportInputError("canonical ownership mode must be ui-server or aflowd")
    captured_at = _safe_timestamp(observed_at, required=True)
    assert captured_at is not None
    return repository, run_id, str(ownership_mode), captured_at


def build_canonical_report_input(
    canonical_observation: Mapping[str, Any] | object,
    *,
    repository: Path | str,
    run_id: str,
    ownership_mode: str,
    observed_at: str,
) -> dict[str, Any]:
    """Normalize one captured schema-v1 ``get_run`` response without extra reads."""
    observation = _mapping(canonical_observation)
    if observation is None:
        raise ReportInputError("canonical observation must be a JSON object")
    pinned_repository, pinned_run_id, _mode, captured_at = _pinned_canonical_identity(
        repository, run_id, ownership_mode, observed_at
    )
    if observation.get("schema_version") != CANONICAL_RUN_SCHEMA_VERSION:
        raise ReportInputError("canonical observation schema version is unsupported")
    if observation.get("run_id") != pinned_run_id:
        raise ReportInputError("canonical observation does not match the pinned run")
    if observation.get("ownership") != "control_plane":
        raise ReportInputError("canonical observation ownership must be control_plane")
    if not isinstance(observation.get("status"), str) or not observation["status"].strip():
        raise ReportInputError("canonical observation is missing status")
    if observation.get("activity") not in {"active", "inactive", "unknown"}:
        raise ReportInputError("canonical observation has unsupported activity")
    if not isinstance(observation.get("status_reason_code"), str):
        raise ReportInputError("canonical observation is missing status reason code")
    evidence = observation.get("evidence")
    if evidence is not None and not isinstance(evidence, Mapping):
        raise ReportInputError("canonical observation evidence is not an object")
    worker_exit = observation.get("worker_exit")
    if worker_exit is not None and not isinstance(worker_exit, Mapping):
        raise ReportInputError("canonical observation worker exit is not an object")

    status = _safe_text(observation.get("status"), key="status")
    activity = str(observation["activity"])
    step = _safe_text(observation.get("current_step"), key="current_step")
    selector, model, effort = _worker_values({}, observation, None)
    diagnosis = _diagnosis(
        observation,
        observation,
        repository=pinned_repository,
        run_id=pinned_run_id,
        status=status,
        activity=activity,
        ownership="control_plane",
        progress_available=False,
        finalized_summary=None,
        result=None,
        observed_at=captured_at,
    )
    return _normalized_report(
        repository=pinned_repository,
        run_id=pinned_run_id,
        observed_at=captured_at,
        status=status,
        activity=activity,
        ownership="control_plane",
        checkpoint_index=None,
        checkpoint_name=None,
        checkpoint_total=None,
        step=step,
        finalized_turn_summary=None,
        worker_selector=selector,
        worker_model=model,
        worker_effort=effort,
        diagnosis=diagnosis,
    )


def build_report_input(
    snapshot: Mapping[str, Any] | object,
    *,
    finalized_evidence: Mapping[str, Any] | None = None,
    cause_evidence: Mapping[str, Any] | None = None,
    expected_repository: Path | str | None = None,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    """Normalize a pinned snapshot and permitted finalized/cause evidence."""
    root = _mapping(snapshot)
    if root is None:
        raise ReportInputError("snapshot must be a JSON object")
    if root.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ReportInputError("snapshot schema version is unsupported")
    repository, run_id = _identity_from_snapshot(root)
    if expected_repository is not None:
        expected = Path(expected_repository).expanduser().resolve(strict=False)
        if repository != expected:
            raise ReportInputError("snapshot repository does not match the pinned repository")
    if expected_run_id is not None and run_id != expected_run_id:
        raise ReportInputError("snapshot run_id does not match the pinned run")

    observed_at = _safe_timestamp(root.get("observed_at"))
    observation, canonical = _canonical_observation(root, repository, run_id)
    if not _identity_matches(observation, repository, run_id):
        raise ReportInputError("observation does not match the pinned identity")
    run = _run_mapping(root)

    status_value = observation.get("status", run.get("status"))
    status = _safe_text(status_value, key="status")
    ownership = _safe_text(
        observation.get(
            "ownership", root.get("ownership_mode", root.get("ownership"))
        ),
        key="ownership",
    )
    if ownership is None:
        ownership = "control_plane" if canonical else "unavailable"
    activity = _activity(root, observation, canonical=canonical)
    (
        checkpoint_index,
        checkpoint_name,
        checkpoint_total,
        step,
        _current_turn,
        last_finished,
    ) = _progress_values(root, observation, run)

    result = _read_finalized_result(root, repository, run_id, finalized_evidence)
    finalized_summary = _result_summary(result)
    if finalized_summary is None and last_finished is not None:
        finalized_summary = _safe_text(last_finished.get("summary"), key="summary")
    if step is None and last_finished is not None:
        step = _safe_text(last_finished.get("step"), key="step")
    selector, model, effort = _worker_values(root, observation, result)
    progress_available = checkpoint_index is not None or checkpoint_name is not None or checkpoint_total is not None

    # The caller may supply a structured cause bundle; it is attached to the
    # already validated snapshot and is never treated as free-form report text.
    if cause_evidence is not None:
        if not isinstance(cause_evidence, Mapping):
            raise ReportInputError("cause evidence must be a structured JSON object")
        if any(
            key not in _CAUSE_KEYS
            or (value is not None and not isinstance(value, Mapping))
            for key, value in cause_evidence.items()
        ):
            raise ReportInputError("cause evidence has an unsupported source schema")
        root_for_diagnosis = dict(root)
        root_for_diagnosis["diagnostic_evidence"] = cause_evidence
    else:
        root_for_diagnosis = root
    diagnosis = _diagnosis(
        root_for_diagnosis,
        observation,
        repository=repository,
        run_id=run_id,
        status=status,
        activity=activity,
        ownership=ownership,
        progress_available=progress_available,
        finalized_summary=finalized_summary,
        result=result,
        observed_at=observed_at,
    )
    return _normalized_report(
        repository=repository,
        run_id=run_id,
        observed_at=observed_at,
        status=status,
        activity=activity,
        ownership=ownership,
        checkpoint_index=checkpoint_index,
        checkpoint_name=checkpoint_name,
        checkpoint_total=checkpoint_total,
        step=step,
        finalized_turn_summary=finalized_summary,
        worker_selector=selector,
        worker_model=model,
        worker_effort=effort,
        diagnosis=diagnosis,
    )


def _read_json(path: Path, *, max_bytes: int = MAX_REPORT_INPUT_BYTES * 4) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReportInputError(f"input is not a regular file: {path}")
    try:
        if path.stat().st_size > max_bytes:
            raise ReportInputError(f"input exceeds the bounded read limit: {path.name}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except ReportInputError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReportInputError(f"input JSON is unreadable: {path.name}") from exc
    if not isinstance(value, Mapping):
        raise ReportInputError(f"input JSON must be an object: {path.name}")
    return value


def _write_external(path: Path, payload: bytes, repository: Path) -> None:
    destination = path.expanduser()
    try:
        resolved = destination.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ReportInputError("output path cannot be resolved") from exc
    if _inside(resolved, repository):
        raise ReportInputError("report input output must be outside the guarded repository")
    destination = resolved
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        mode="wb", prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False
    )
    temporary_name = Path(temporary.name)
    try:
        with temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
    finally:
        temporary_name.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot", type=Path)
    source.add_argument("--canonical-observation", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--finalized-evidence", type=Path)
    parser.add_argument("--cause-evidence", type=Path)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--ownership-mode", choices=sorted(_CANONICAL_OWNERSHIP_MODES))
    parser.add_argument("--observed-at")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.canonical_observation is not None:
            if args.repo is None or args.run_id is None or args.ownership_mode is None or args.observed_at is None:
                raise ReportInputError(
                    "canonical observation requires --repo, --run-id, --ownership-mode, and --observed-at"
                )
            if args.finalized_evidence is not None or args.cause_evidence is not None:
                raise ReportInputError(
                    "canonical observation does not accept separate finalized or cause evidence"
                )
            repository = args.repo.expanduser().resolve(strict=False)
            report = build_canonical_report_input(
                _read_json(args.canonical_observation),
                repository=repository,
                run_id=args.run_id,
                ownership_mode=args.ownership_mode,
                observed_at=args.observed_at,
            )
        else:
            assert args.snapshot is not None
            snapshot = _read_json(args.snapshot)
            finalized = (
                _read_json(args.finalized_evidence) if args.finalized_evidence is not None else None
            )
            causes = (
                _read_json(args.cause_evidence) if args.cause_evidence is not None else None
            )
            report = build_report_input(
                snapshot,
                finalized_evidence=finalized,
                cause_evidence=causes,
                expected_repository=args.repo,
                expected_run_id=args.run_id,
            )
            repository, _ = _identity_from_snapshot(snapshot)
        payload = _json_bytes(report) + b"\n"
        if args.output is None:
            sys.stdout.buffer.write(payload)
        else:
            _write_external(args.output, payload, repository)
    except (OSError, ReportInputError) as exc:
        print(f"aflow guard report input: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
