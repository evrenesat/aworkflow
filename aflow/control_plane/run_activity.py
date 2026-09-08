"""Read-only, shared evidence projection for public run activity."""

from dataclasses import replace
import os
from pathlib import Path
from .models import RunStatus


def preparation_owner() -> dict:
    from aflow.daemon_cli import _process_birth_identity
    return {"schema_version": 1, "pid": os.getpid(), "birth": _process_birth_identity(os.getpid()), "host_boot": _host_boot()}


def _host_boot():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return None


def preparation_active(owner: object) -> bool | None:
    from aflow.daemon_cli import _process_birth_identity
    if not isinstance(owner, dict) or owner.get("schema_version") != 1:
        return None
    if not owner.get("host_boot") or owner["host_boot"] != _host_boot():
        return None
    pid = owner.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1 or not owner.get("birth"):
        return None
    birth = _process_birth_identity(pid)
    return birth == owner["birth"] if birth is not None else None


def valid_startup_question(record) -> bool:
    from aflow.daemon import DaemonError, _question_from_record, _question_generation
    try:
        question = _question_from_record(record)
        _question_generation(record)
        return bool(question.message)
    except (DaemonError, ValueError, TypeError):
        return False


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
