"""Read-only, shared evidence projection for public run activity."""

from collections.abc import Mapping
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
from aflow.process_identity import process_birth_identity
from .models import RunStatus


def preparation_owner() -> dict:
    return {"schema_version": 1, "pid": os.getpid(), "birth": process_birth_identity(os.getpid()), "host_boot": _host_boot()}


def _host_boot():
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
                capture_output=True, text=True, timeout=5, check=True,
            )
            return result.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return None


def preparation_active(owner: object) -> bool | None:
    if not isinstance(owner, dict) or owner.get("schema_version") != 1:
        return None
    if not owner.get("host_boot") or owner["host_boot"] != _host_boot():
        return None
    pid = owner.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1 or not owner.get("birth"):
        return None
    birth = process_birth_identity(pid)
    return birth == owner["birth"] if birth is not None else None


def valid_startup_question(record) -> bool:
    from aflow.daemon import DaemonError, _question_from_record, _question_generation
    try:
        question = _question_from_record(record)
        _question_generation(record)
        return bool(question.message)
    except (DaemonError, ValueError, TypeError):
        return False


_EXECUTION_RESOURCE_WAIT_CODE = "execution_resource_wait"

# Only the bounded, owner-safe fields of the durable wait record are projected
# to status consumers. Controller process identity and every other runlog
# field stay in the run directory.
_WAIT_REQUIRED_FIELDS: tuple[str, ...] = (
    "resource",
    "label",
    "invocation_id",
    "kind",
    "role",
    "selector",
    "step",
    "wait_started_at",
)
_WAIT_OPTIONAL_FIELDS: tuple[str, ...] = ("reason",)


def execution_resource_wait_projection(record: object) -> dict[str, object] | None:
    """Validate a durable execution-resource wait record for status display.

    Returns a projected copy containing only the safe display fields, or
    ``None`` when the record is missing, stale (a different schema version),
    or malformed.  Projection never interprets the record beyond this shape
    check; terminal, stopped, unknown, and inactive controller evidence are
    handled separately by :func:`project_activity`.
    """
    if not isinstance(record, Mapping) or record.get("version") != 1:
        return None
    projected: dict[str, object] = {"version": 1}
    for field in _WAIT_REQUIRED_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            return None
        projected[field] = value.strip()
    # The durable ticket is a positive integer (FIFO order) or explicit null
    # when the broker has not yet assigned one.  Strings, booleans, and
    # non-positive numbers are malformed and fail the projection closed.
    ticket = record.get("ticket")
    if ticket is not None and not (isinstance(ticket, int) and not isinstance(ticket, bool) and ticket > 0):
        return None
    projected["ticket"] = ticket
    for field in _WAIT_OPTIONAL_FIELDS:
        value = record.get(field)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            return None
        projected[field] = value.strip()
    owner_unconfirmed = record.get("owner_unconfirmed")
    if owner_unconfirmed is not None and not isinstance(owner_unconfirmed, bool):
        return None
    if owner_unconfirmed is True:
        projected["owner_unconfirmed"] = True
    return projected


def execution_resource_wait_message(record: Mapping[str, object]) -> str:
    """Owner-facing neutral message for a validated wait record."""
    label = str(record.get("label") or "resource")
    message = f"Waiting for {label} (exclusive)"
    if record.get("owner_unconfirmed") is True or record.get("reason") == "owner_unconfirmed":
        message += "; previous execution could not be confirmed stopped"
    return message


def project_activity(run: RunStatus) -> RunStatus:
    evidence = run.evidence
    active = evidence.get("unit_active")
    if active is None and evidence.get("startup_state") == "preparing":
        active = evidence.get("preparation_active")
    activity = "active" if active is True else "inactive" if active is False else "unknown"
    status, reason, code = run.status, run.reason, "activity_unknown"
    raw = evidence.get("recorded_status")
    worker = evidence.get("worker") or {}
    if status == "owner_stopped" or evidence.get("controller_terminal") or (run.ownership == "legacy" and status in {"completed", "failed", "interrupted"} and raw == status):
        code = "controller_" + status
    elif run.worker_exit and status == "failed":
        code = "worker_failed" if evidence.get("has_run_metadata") else "startup_failed"
    elif active is True and worker.get("exit_code") is not None:
        status, code, reason = "needs_attention", "conflicting_evidence", "Active worker contradicts retained exit evidence"
    elif active is True:
        status = raw if raw in {"paused", "waiting_for_input", "waiting_for_valid_override"} else "running" if evidence.get("has_run_metadata") else "launch_started"
        code, reason = "execution_active" if evidence.get("has_run_metadata") else "launch_active", None
        # A confirmed active controller may report the bounded, neutral
        # execution-resource wait.  Terminal, stopped, unknown, and inactive
        # evidence above take precedence, so this never masks a stronger
        # state; a stale record on an inactive controller is ignored.
        if status == "running":
            wait_record = execution_resource_wait_projection(evidence.get("execution_resource_wait"))
            if wait_record is not None:
                code = _EXECUTION_RESOURCE_WAIT_CODE
                reason = execution_resource_wait_message(wait_record)
                # Publish only the bounded projection, never the raw record.
                run = replace(run, evidence={**evidence, "execution_resource_wait": wait_record})
    elif evidence.get("startup_failure"):
        code = "startup_failed"
    elif evidence.get("startup_state") == "awaiting_startup_answer" and evidence.get("startup_question_valid"):
        status, code = "awaiting_startup_answer", "startup_answer_required"
    elif run.ownership == "control_plane" and raw in {"paused", "waiting_for_input", "waiting_for_valid_override"} and not worker:
        status, code = raw, "execution_waiting"
    else:
        status = "needs_attention"
        if worker.get("exit_code") == 0:
            code = "missing_completion"
        elif worker.get("observation") == "untrusted_receipts":
            code = "untrusted_activity"
        elif evidence.get("startup_state") == "preparing":
            code = "preparation_unconfirmed"
            reason = "The preparation owner is no longer confirmed active; startup may have been abandoned."
        elif evidence.get("unit_observation") == "unavailable":
            code, reason = "observation_unavailable", "The workflow unit could not be inspected; current activity is unknown."
        elif evidence.get("unit_observation") == "missing":
            code, reason = "unit_missing", "No current workflow unit was found and no normal outcome was recorded."
        elif evidence.get("reconciled") and not worker and evidence.get("startup_state") != "needs_attention":
            reason = "A previous unit observation is recorded, but current workflow activity is unconfirmed."
        reason = reason or "Current workflow activity and a normal outcome could not be confirmed."
    return replace(run, status=status, activity=activity, status_reason_code=code, reason=reason)
