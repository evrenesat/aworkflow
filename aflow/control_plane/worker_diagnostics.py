"""Read-only projection of owned portable-worker receipts; no recovery actions."""

from dataclasses import replace
from pathlib import Path

from .models import RunStatus, startup_failure
from .persistent_units import PersistentUnitManager, _nonce_matches, _read_json, _receipts_for, _terminal_receipt


def confirmed_inactive(worker) -> bool:
    return worker.get("active") is False and ("exit_code" in worker or worker.get("observation") == "stopped" or bool(worker.get("wrapper_error")))


def worker_evidence(root: Path, run_id: str, unit: str) -> dict | None:
    if unit != f"aflow-run-{run_id}.service":
        return None
    receipts = _receipts_for(unit, root)
    if receipts is None:
        if (root / ".aflow" / "runs" / run_id / "units").exists():
            return {"active": None, "observation": "untrusted_receipts"}
        return None
    observed = PersistentUnitManager(executable="aflow")._observe(receipts)
    result: dict = {"active": observed.is_active, "observation": observed.result}
    exit_record = _terminal_receipt(receipts, receipts.exit)
    if exit_record is not None:
        result.update(exit_code=exit_record["returncode"], exited_at=exit_record["at"])
        if exit_record.get("diagnostic_write_failed") is True:
            result["capture_error"] = "Diagnostic capture could not be fully persisted"
    for filename, key in (("diagnostic.json", "diagnostic"), ("error.json", "wrapper_error"), ("worker-error.json", "worker_error")):
        record = _read_json(receipts.directory / filename)
        if _nonce_matches(record, receipts.nonce):
            result[key] = {
                field: startup_failure("worker", value)["message"]
                for field, value in record.items()
                if field in {"stdout", "stderr", "message", "error", "stage", "timestamp"} and isinstance(value, str)
            }
    return result


def project_worker_status(status: RunStatus, root: Path) -> RunStatus:
    evidence = worker_evidence(root, status.run_id, status.unit_name or "")
    if evidence is None:
        return status
    status = replace(status, evidence={**status.evidence, "unit_active": evidence["active"], "worker": evidence})
    authoritative = status.status == "owner_stopped" or status.evidence.get("controller_terminal") is True
    code = evidence.get("exit_code")
    if evidence["active"] is None:
        return status if authoritative else replace(status, status="needs_attention", reason="Worker activity could not be confirmed")
    if evidence["active"]:
        if authoritative:
            return status
        if code is not None:
            return replace(status, status="needs_attention", reason="Active worker contradicts retained exit evidence")
        return status
    early = not status.evidence.get("has_run_metadata")
    detail = evidence.get("worker_error") or evidence.get("wrapper_error") or evidence.get("diagnostic") or {}
    reason = detail.get("message") or detail.get("error") or detail.get("stderr")
    if not reason and detail.get("stdout"):
        reason = "Last worker output: " + detail["stdout"]
    failure = {
        "stage": detail.get("stage") or ("startup" if early else "worker"),
        "reason": reason,
        "exit_code": code,
        "exited_at": evidence.get("exited_at"),
        "diagnostic_unavailable": not bool(reason),
    }
    if authoritative:
        if status.status in {"failed", "interrupted"} and code is not None:
            return replace(status, worker_exit={**failure, "stage": "controller", "reason": status.reason or reason, "diagnostic_unavailable": not bool(status.reason or reason)})
        return status
    if code is not None and code != 0:
        return replace(status, status="failed", reason=reason or (f"Worker exited during startup (code {code})" if early else f"Worker exited (code {code})"), worker_exit=failure)
    if evidence.get("wrapper_error"):
        return replace(status, status="failed", reason=reason, worker_exit=failure)
    if code == 0:
        return replace(status, status="needs_attention", reason="Worker exited successfully without a controller completion record", worker_exit=failure)
    return replace(status, status="needs_attention", reason="Worker activity could not be confirmed")
