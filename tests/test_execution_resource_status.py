"""Read-only execution-resource waiting status projection.

The durable wait record lives in the run state; the control plane must
project a bounded, redacted view into run status evidence only while the
exact controller is confirmed active and running.  Terminal, stopped,
inactive, and unknown evidence keeps precedence, and malformed or stale
records are ignored without raising.
"""

from __future__ import annotations

import json
from pathlib import Path

from aflow.config import execution_resource_key
from aflow.control_plane import (
    LaunchManifest,
    RunRepository,
    create_launch_manifest,
    write_launch_phase,
)
from aflow.control_plane.models import RunStatus
from aflow.control_plane.run_activity import (
    execution_resource_wait_message,
    execution_resource_wait_projection,
    project_activity,
)
from aflow.execution_resources import ControllerIdentity
from aflow.workflow import _resource_wait_record

# The durable resource key is derived from the declared tuple, never a
# user-facing label.
_WAIT_RESOURCE_KEY = execution_resource_key("codex", "gpt-5-codex", "high")


def _manifest(run_id: str) -> LaunchManifest:
    return LaunchManifest(
        run_id=run_id,
        project_root="/project",
        plan_path="/project/plan.md",
        workflow_name="managed",
        max_turns=5,
        idempotency_key="request-1",
        caller_scope="caller:project",
    )


def _wait_record(**overrides: object) -> dict:
    # Build the exact durable shape the controller writes, including the
    # controller process identity that the projection must redact.
    record: dict = _resource_wait_record(
        resource=_WAIT_RESOURCE_KEY,
        label="codex / gpt-5-codex / effort high",
        ticket=1,
        reason=None,
        step_name="implement",
        role="worker",
        invocation_id="inv-1",
        kind="workflow",
        selector="codex.gpt-5-codex",
        wait_started_at="2026-10-04T10:00:00Z",
        controller=ControllerIdentity(pid=4242, birth="birth-xyz", boot="boot-1"),
    )
    record.update(overrides)
    return record


def _active_run(
    *,
    status: str = "running",
    unit_active: object = True,
    wait: dict | None = None,
    recorded_status: str | None = None,
    controller_terminal: bool = False,
) -> RunStatus:
    evidence: dict = {
        "has_run_metadata": True,
        "recorded_status": recorded_status if recorded_status is not None else status,
        "unit_active": unit_active,
        "controller_terminal": controller_terminal,
    }
    if wait is not None:
        evidence["execution_resource_wait"] = wait
    return RunStatus(
        run_id="wait-run",
        status=status,
        unit_name="aflow-run-wait-run.service",
        launch_phase="unit_started",
        workflow_name="managed",
        team="base",
        evidence=evidence,
    )


def test_active_running_controller_projects_bounded_waiting_status() -> None:
    projected = project_activity(_active_run(wait=_wait_record()))

    assert projected.status == "running"
    assert projected.activity == "active"
    assert projected.status_reason_code == "execution_resource_wait"
    assert projected.reason == "Waiting for codex / gpt-5-codex / effort high (exclusive)"

    record = projected.evidence["execution_resource_wait"]
    assert record == {
        "version": 1,
        "resource": _WAIT_RESOURCE_KEY,
        "label": "codex / gpt-5-codex / effort high",
        "invocation_id": "inv-1",
        "kind": "workflow",
        "role": "worker",
        "selector": "codex.gpt-5-codex",
        "step": "implement",
        "ticket": 1,
        "wait_started_at": "2026-10-04T10:00:00Z",
    }
    for hidden in ("controller_pid", "controller_birth", "controller_boot"):
        assert hidden not in record
    blob = json.dumps(projected.evidence)
    assert "4242" not in blob
    assert "birth-xyz" not in blob
    assert "boot-1" not in blob


def test_unconfirmed_owner_keeps_the_exact_suffixed_message() -> None:
    projected = project_activity(
        _active_run(wait=_wait_record(reason="owner_unconfirmed", owner_unconfirmed=True))
    )

    assert projected.status_reason_code == "execution_resource_wait"
    assert projected.reason == (
        "Waiting for codex / gpt-5-codex / effort high (exclusive); "
        "previous execution could not be confirmed stopped"
    )
    record = projected.evidence["execution_resource_wait"]
    assert record["owner_unconfirmed"] is True
    assert record["reason"] == "owner_unconfirmed"


def test_no_wait_record_keeps_the_normal_active_status() -> None:
    projected = project_activity(_active_run())

    assert projected.status == "running"
    assert projected.status_reason_code == "execution_active"
    assert projected.reason is None
    assert "execution_resource_wait" not in projected.evidence


def test_terminal_evidence_takes_precedence_over_a_stale_wait_record() -> None:
    projected = project_activity(
        _active_run(status="completed", unit_active=False, controller_terminal=True, wait=_wait_record())
    )

    assert projected.status == "completed"
    assert projected.status_reason_code == "controller_completed"
    assert projected.reason != "Waiting for codex / gpt-5-codex / effort high (exclusive)"


def test_owner_stop_takes_precedence_over_a_stale_wait_record() -> None:
    projected = project_activity(_active_run(status="owner_stopped", unit_active=False, wait=_wait_record()))

    assert projected.status == "owner_stopped"
    assert projected.status_reason_code == "controller_owner_stopped"
    assert "Waiting for" not in (projected.reason or "")


def test_inactive_controller_ignores_the_wait_record() -> None:
    projected = project_activity(_active_run(unit_active=False, wait=_wait_record()))

    assert projected.status == "needs_attention"
    assert projected.status_reason_code != "execution_resource_wait"
    assert projected.reason != "Waiting for codex / gpt-5-codex / effort high (exclusive)"


def test_unknown_controller_ignores_the_wait_record() -> None:
    projected = project_activity(_active_run(unit_active=None, wait=_wait_record()))

    assert projected.status_reason_code != "execution_resource_wait"
    assert projected.reason != "Waiting for codex / gpt-5-codex / effort high (exclusive)"


def test_waiting_never_masks_a_paused_or_waiting_execution() -> None:
    for raw in ("paused", "waiting_for_input", "waiting_for_valid_override"):
        projected = project_activity(_active_run(status=raw, recorded_status=raw, wait=_wait_record()))
        assert projected.status == raw, raw
        assert projected.status_reason_code != "execution_resource_wait", raw


def test_projection_redacts_controller_process_identity_and_bounds_fields() -> None:
    projected = execution_resource_wait_projection(_wait_record())
    assert projected is not None
    for hidden in ("controller_pid", "controller_birth", "controller_boot"):
        assert hidden not in projected

    assert execution_resource_wait_projection(_wait_record(version=2)) is None
    assert execution_resource_wait_projection(_wait_record(resource="x" * 257)) is None
    assert execution_resource_wait_projection(_wait_record(owner_unconfirmed="yes")) is None
    assert execution_resource_wait_projection("Waiting for something") is None
    assert execution_resource_wait_projection(None) is None
    assert execution_resource_wait_projection([1, 2, 3]) is None


def test_projection_accepts_integer_and_null_tickets() -> None:
    for good in (1, 42, None):
        projected = execution_resource_wait_projection(_wait_record(ticket=good))
        assert projected is not None, good
        assert projected["ticket"] == good, good


def test_projection_rejects_malformed_tickets() -> None:
    for bad in ("1", 0, -1, True, 1.5, [1], ""):
        assert execution_resource_wait_projection(_wait_record(ticket=bad)) is None, bad


def test_wait_message_matches_the_projection_contract() -> None:
    record = execution_resource_wait_projection(_wait_record())
    assert record is not None
    assert execution_resource_wait_message(record) == "Waiting for codex / gpt-5-codex / effort high (exclusive)"
    unconfirmed = execution_resource_wait_projection(_wait_record(owner_unconfirmed=True))
    assert unconfirmed is not None
    assert execution_resource_wait_message(unconfirmed).endswith(
        "; previous execution could not be confirmed stopped"
    )


def _owned_running(root: Path, run_id: str, *, status: str = "running", wait: dict | None = None) -> Path:
    create_launch_manifest(root, _manifest(run_id))
    run_dir = root / ".aflow" / "runs" / run_id
    run_dir.mkdir()
    payload: dict = {"status": status}
    if wait is not None:
        payload["execution_resource_wait"] = wait
    (run_dir / "run.json").write_text(json.dumps(payload))
    write_launch_phase(root, run_id, "unit_started")
    return run_dir


def test_repository_projects_the_bounded_record_into_status_evidence(tmp_path: Path) -> None:
    _owned_running(tmp_path, "wait-run", wait=_wait_record())
    status = RunRepository(tmp_path).get_run_status("wait-run")

    record = status.evidence.get("execution_resource_wait")
    assert record is not None
    assert record["label"] == "codex / gpt-5-codex / effort high"
    assert "controller_pid" not in record
    assert "controller_birth" not in record
    assert "controller_boot" not in record


def test_repository_omits_malformed_wait_records_without_raising(tmp_path: Path) -> None:
    for index, record in enumerate([
        _wait_record(version=2),
        {key: value for key, value in _wait_record().items() if key != "label"},
        _wait_record(label=7),
        _wait_record(owner_unconfirmed="yes"),
        "Waiting for something",
    ]):
        _owned_running(tmp_path, f"bad-run-{index}", wait=record)  # type: ignore[arg-type]
        status = RunRepository(tmp_path).get_run_status(f"bad-run-{index}")
        assert "execution_resource_wait" not in status.evidence, index
