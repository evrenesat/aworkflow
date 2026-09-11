from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

from aflow.control_plane.models import RunStatus
from aflow.control_plane.run_activity import project_activity


SCRIPT_DIR = (
    Path(__file__).resolve().parents[1]
    / "aflow"
    / "bundled_skills"
    / "aflow-guard-development-run"
    / "scripts"
)
INPUT_HELPER_PATH = SCRIPT_DIR / "aflow_guard_report_input.py"


def _helper():
    spec = importlib.util.spec_from_file_location("guard_report_input", INPUT_HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _snapshot(
    tmp_path: Path,
    *,
    classification: str = "terminal_success",
    latest_result: dict[str, object] | None = None,
) -> dict[str, object]:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_id = "20260911T120000Z-abcd1234"
    if latest_result is None:
        latest_result = {
            "name": "turn-001",
            "path": "turns/turn-001/result.json",
            "turn_number": 1,
            "status": "completed",
            "finished_at": "2026-09-11T12:00:05+00:00",
            "finalized": True,
            "step": "implement",
            "summary": "implemented the bounded change",
            "selector": "codex.worker",
        }
    return {
        "schema_version": 3,
        "observed_at": "2026-09-11T12:01:00+00:00",
        "repo": str(repo),
        "run_id": run_id,
        "ownership": "legacy",
        "activity": "inactive",
        "classification": classification,
        "changed_since_previous": True,
        "notification_already_sent": False,
        "run": {
            "status": "completed" if classification == "terminal_success" else "running",
            "current_step": "implement",
            "last_snapshot": {
                "current_checkpoint_index": 1,
                "current_checkpoint_name": "Checkpoint 1: Normalize",
                "total_checkpoint_count": 3,
                "is_complete": classification == "terminal_success",
            },
        },
        "progress": {
            "availability": "available",
            "checkpoint": {"index": 1, "name": "Checkpoint 1: Normalize"},
            "total": 3,
            "last_finished_turn": {
                "turn_number": 1,
                "step": "implement",
                "status": "completed",
                "summary": "implemented the bounded change",
            },
            "current_turn": None,
        },
        "latest_result": latest_result,
        "processes": {"controller_count": 0, "child_pids": [101, 102]},
    }


def _identity(snapshot: dict[str, object]) -> dict[str, str]:
    return {
        "repository": str(Path(snapshot["repo"]).resolve()),
        "run_id": str(snapshot["run_id"]),
    }


def _canonical_projection(
    helper, tmp_path: Path, run: RunStatus
) -> dict[str, object]:
    repository = tmp_path / "canonical-repository"
    repository.mkdir(parents=True)
    return helper.build_canonical_report_input(
        project_activity(run).to_dict(),
        repository=repository,
        run_id=run.run_id,
        ownership_mode="aflowd",
        observed_at="2026-09-11T12:01:00+00:00",
    )


def test_build_report_input_has_one_bounded_schema_and_is_repeatable(tmp_path: Path) -> None:
    helper = _helper()
    snapshot = _snapshot(tmp_path)
    before = {
        path: path.read_bytes()
        for path in (tmp_path / "repo").rglob("*")
        if path.is_file()
    }

    first = helper.build_report_input(snapshot)
    second = helper.build_report_input(snapshot)

    assert first == second
    assert set(first) == helper._REPORT_KEYS
    assert first["repository"] == str((tmp_path / "repo").resolve())
    assert first["run_id"] == snapshot["run_id"]
    assert first["status"] == "completed"
    assert first["activity"] == "inactive"
    assert first["ownership"] == "legacy"
    assert first["checkpoint_index"] == 1
    assert first["checkpoint_name"] == "Checkpoint 1: Normalize"
    assert first["checkpoint_total"] == 3
    assert first["step"] == "implement"
    assert first["finalized_turn_summary"] == "implemented the bounded change"
    assert first["worker_selector"] == "codex.worker"
    assert first["worker_model"] is None
    assert first["worker_effort"] is None
    assert first["diagnosis"]["confidence"] == "unknown"
    assert len(json.dumps(first, ensure_ascii=False).encode()) <= helper.MAX_REPORT_INPUT_BYTES

    after = {
        path: path.read_bytes()
        for path in (tmp_path / "repo").rglob("*")
        if path.is_file()
    }
    assert after == before


def test_new_orphan_uses_only_identity_matched_bounded_cause_ladder(tmp_path: Path) -> None:
    helper = _helper()
    snapshot = _snapshot(tmp_path, classification="orphaned_controller", latest_result=None)
    snapshot["run"]["status"] = "running"
    identity = _identity(snapshot)
    causes = {
        "controller_exit": {
            **identity,
            "unit": f"aflow-run-{snapshot['run_id']}.service",
            "exit_code": -9,
            "at": snapshot["observed_at"],
            "reason": "controller terminated",
        },
        "host_termination": {
            **identity,
            "confirmed": True,
            "kind": "oom",
            "reason": "host OOM record",
            "at": snapshot["observed_at"],
        },
        "failure_boundary": {
            **identity,
            "recorded": True,
            "stage": "controller",
            "reason": "controller failure boundary",
            "at": snapshot["observed_at"],
        },
    }

    report = helper.build_report_input(snapshot, cause_evidence=causes)
    diagnosis = report["diagnosis"]

    assert diagnosis["likely_cause"] == "controller/unit exited with code -9"
    assert diagnosis["confidence"] == "confirmed"
    assert diagnosis["supporting_evidence"]
    assert diagnosis["duplicate_operation_risk"] is True
    assert "Inspect the exact recorded provider operation" in diagnosis["owner_action"]
    assert "do not relaunch blindly" in diagnosis["owner_action"]
    assert "host OOM" not in diagnosis["likely_cause"]


def test_orphan_without_confirmed_cause_keeps_contradiction_and_unknown_visible(
    tmp_path: Path,
) -> None:
    helper = _helper()
    snapshot = _snapshot(tmp_path, classification="orphaned_controller", latest_result=None)
    snapshot["run"]["status"] = "running"
    identity = _identity(snapshot)
    causes = {
        "controller_exit": {
            **identity,
            "unit": f"aflow-run-{snapshot['run_id']}.service",
            "exit_code": 0,
            "at": snapshot["observed_at"],
        },
        "host_termination": {
            **identity,
            "permission": "denied",
            "kind": "oom",
            "at": snapshot["observed_at"],
        },
    }

    diagnosis = helper.build_report_input(snapshot, cause_evidence=causes)["diagnosis"]

    assert diagnosis["likely_cause"] is None
    assert diagnosis["confidence"] == "unknown"
    assert any("exit code 0" in item for item in diagnosis["contradicting_evidence"])
    assert any("unavailable" in item for item in diagnosis["unknowns"])
    assert diagnosis["duplicate_operation_risk"] is True


def test_confirmed_host_termination_is_a_direct_cause_for_a_new_orphan(
    tmp_path: Path,
) -> None:
    helper = _helper()
    snapshot = _snapshot(tmp_path, classification="orphaned_controller", latest_result=None)
    snapshot["run"]["status"] = "running"
    identity = _identity(snapshot)

    diagnosis = helper.build_report_input(
        snapshot,
        cause_evidence={
            "host_termination": {
                **identity,
                "confirmed": True,
                "kind": "oom",
                "reason": "host OOM record",
                "at": snapshot["observed_at"],
            }
        },
    )["diagnosis"]

    assert diagnosis["likely_cause"] == "host OOM record"
    assert diagnosis["confidence"] == "confirmed"
    assert any("Host termination/OOM evidence" in item for item in diagnosis["supporting_evidence"])


def test_quota_failure_remains_failure_without_an_invented_cause(tmp_path: Path) -> None:
    helper = _helper()
    snapshot = _snapshot(
        tmp_path,
        classification="terminal_failed",
        latest_result={
            "name": "turn-001",
            "status": "failed",
            "finished_at": "2026-09-11T12:00:05+00:00",
            "finalized": True,
            "step": "implement",
            "error": "provider quota exceeded",
        },
    )
    snapshot["run"]["status"] = "failed"

    report = helper.build_report_input(snapshot)

    assert report["status"] == "failed"
    assert "quota exceeded" in report["finalized_turn_summary"]
    assert report["diagnosis"]["likely_cause"] is None
    assert report["diagnosis"]["confidence"] == "unknown"
    assert "failed turn" in report["diagnosis"]["owner_action"]
    assert "does not authorize recovery" in report["diagnosis"]["owner_action"]


def test_compact_finalized_result_rejects_starting_and_wrong_identity_without_artifacts(
    tmp_path: Path,
) -> None:
    helper = _helper()
    starting = _snapshot(
        tmp_path,
        latest_result={
            "name": "turn-002",
            "status": "starting",
            "summary": "still launching",
        },
    )
    starting["progress"]["last_finished_turn"] = None

    report = helper.build_report_input(starting)

    assert report["finalized_turn_summary"] is None
    assert report["worker_selector"] is None

    wrong_root = tmp_path / "wrong-identity"
    wrong_root.mkdir()
    wrong_identity = _snapshot(
        wrong_root,
        latest_result={
            "name": "turn-001",
            "status": "completed",
            "finished_at": "2026-09-11T12:00:05+00:00",
            "finalized": True,
            "summary": "other run result",
            "repository": str(tmp_path / "other-repository"),
            "run_id": "other-run",
        },
    )
    wrong_identity["progress"]["last_finished_turn"] = None

    with pytest.raises(helper.ReportInputError, match="compact finalized result"):
        helper.build_report_input(wrong_identity)


@pytest.mark.parametrize(
    ("changed_since_previous", "notification_already_sent", "is_new"),
    (
        (True, False, True),
        (False, False, False),
        (True, True, False),
        (False, True, False),
    ),
)
def test_orphan_duplicate_risk_survives_explicit_repeat_reports(
    tmp_path: Path,
    changed_since_previous: bool,
    notification_already_sent: bool,
    is_new: bool,
) -> None:
    helper = _helper()
    snapshot = _snapshot(tmp_path, classification="orphaned_controller", latest_result=None)
    snapshot["run"]["status"] = "running"
    snapshot["changed_since_previous"] = changed_since_previous
    snapshot["notification_already_sent"] = notification_already_sent
    snapshot["provider_operation_unknown"] = True

    diagnosis = helper.build_report_input(snapshot)["diagnosis"]

    assert diagnosis["duplicate_operation_risk"] is True
    assert "Inspect the exact recorded provider operation" in diagnosis["owner_action"]
    assert "do not relaunch blindly" in diagnosis["owner_action"]
    if is_new:
        assert not any("Cause inspection is limited" in item for item in diagnosis["unknowns"])
    else:
        assert any("Cause inspection is limited" in item for item in diagnosis["unknowns"])


def test_incomplete_and_canonical_needs_attention_require_owner_decisions(
    tmp_path: Path,
) -> None:
    helper = _helper()
    incomplete = _snapshot(tmp_path, classification="terminal_incomplete")
    incomplete["run"]["status"] = "completed"
    incomplete_report = helper.build_report_input(incomplete)

    assert "failed turn or ownership evidence" in incomplete_report["diagnosis"]["owner_action"]

    canonical_root = tmp_path / "canonical"
    canonical_root.mkdir()
    canonical = _snapshot(canonical_root, latest_result=None)
    canonical["ownership"] = "control_plane"
    canonical["canonical_observation"] = {
        **_identity(canonical),
        "status": "needs_attention",
        "activity": "inactive",
        "ownership": "control_plane",
        "status_reason_code": "worker_failed",
        "evidence": {"reason": "worker ended without a terminal receipt"},
    }
    canonical_report = helper.build_report_input(canonical)

    assert canonical_report["status"] == "needs_attention"
    assert "failed turn or ownership evidence" in canonical_report["diagnosis"]["owner_action"]
    assert any(
        "Canonical status reason: worker_failed" in fact
        for fact in canonical_report["diagnosis"]["confirmed_facts"]
    )

    healthy_root = tmp_path / "healthy"
    healthy_root.mkdir()
    healthy = helper.build_report_input(_snapshot(healthy_root))
    assert healthy["diagnosis"]["owner_action"] == "No owner action is indicated by this observation."


@pytest.mark.parametrize(
    ("name", "run", "action_fragment"),
    (
        (
            "startup-answer",
            RunStatus(
                run_id="review-fixture",
                status="starting",
                evidence={
                    "unit_active": False,
                    "startup_state": "awaiting_startup_answer",
                    "startup_question_valid": True,
                },
            ),
            "answer the existing pinned startup question",
        ),
        (
            "waiting-input",
            RunStatus(
                run_id="review-fixture",
                status="running",
                evidence={
                    "recorded_status": "waiting_for_input",
                    "has_run_metadata": True,
                    "unit_active": True,
                },
            ),
            "supply the requested input",
        ),
        (
            "waiting-override",
            RunStatus(
                run_id="review-fixture",
                status="running",
                evidence={
                    "recorded_status": "waiting_for_valid_override",
                    "has_run_metadata": True,
                    "unit_active": True,
                },
            ),
            "correct the requested override",
        ),
    ),
)
def test_canonical_pending_input_states_require_owner_action(
    tmp_path: Path, name: str, run: RunStatus, action_fragment: str
) -> None:
    helper = _helper()
    case_root = tmp_path / name
    case_root.mkdir()

    report = _canonical_projection(helper, case_root, run)

    assert action_fragment in report["diagnosis"]["owner_action"]
    assert "does not authorize input execution" in report["diagnosis"]["owner_action"]
    assert report["diagnosis"]["likely_cause"] is None
    assert report["diagnosis"]["confidence"] == "unknown"


def test_canonical_healthy_and_unrecognized_states_have_truthful_guidance(
    tmp_path: Path,
) -> None:
    helper = _helper()
    running = _canonical_projection(
        helper,
        tmp_path / "running",
        RunStatus(
            run_id="running-fixture",
            status="running",
            evidence={"has_run_metadata": True, "unit_active": True},
        ),
    )
    completed = _canonical_projection(
        helper,
        tmp_path / "completed",
        RunStatus(
            run_id="completed-fixture",
            status="completed",
            evidence={"controller_terminal": True, "unit_active": False},
        ),
    )
    unrecognized = _canonical_projection(
        helper,
        tmp_path / "unrecognized",
        RunStatus(
            run_id="queued-fixture",
            status="queued",
            evidence={"controller_terminal": True, "unit_active": False},
        ),
    )

    assert running["diagnosis"]["owner_action"] == "No owner action is indicated by this observation."
    assert completed["diagnosis"]["owner_action"] == "No owner action is indicated by this observation."
    assert "Inspect the bounded status" in unrecognized["diagnosis"]["owner_action"]


def test_canonical_missing_unit_keeps_unresolved_operation_risk(tmp_path: Path) -> None:
    helper = _helper()
    missing_unit = RunStatus(
        run_id="missing-unit-fixture",
        status="running",
        evidence={
            "recorded_status": "running",
            "has_run_metadata": True,
            "unit_active": False,
            "unit_observation": "missing",
        },
    )
    report = _canonical_projection(helper, tmp_path / "missing-unit", missing_unit)

    assert report["status"] == "needs_attention"
    assert report["diagnosis"]["duplicate_operation_risk"] is True
    assert report["diagnosis"]["likely_cause"] is None
    assert report["diagnosis"]["confidence"] == "unknown"
    assert "Inspect the exact recorded provider operation" in report["diagnosis"]["owner_action"]
    assert "do not relaunch blindly" in report["diagnosis"]["owner_action"]
    assert any("missing unit does not establish" in item for item in report["diagnosis"]["unknowns"])

    terminal_root = tmp_path / "terminal-provider"
    terminal_root.mkdir()
    terminal = _canonical_projection(
        helper,
        terminal_root,
        RunStatus(
            run_id="terminal-provider-fixture",
            status="running",
            evidence={
                **missing_unit.evidence,
                "provider_operation": {"status": "completed"},
            },
        ),
    )
    assert terminal["diagnosis"]["duplicate_operation_risk"] is False


def test_server_owned_observation_is_canonical_and_process_counts_are_ignored(
    tmp_path: Path,
) -> None:
    helper = _helper()
    snapshot = _snapshot(tmp_path, latest_result=None)
    snapshot["ownership"] = "control_plane"
    snapshot["canonical_observation"] = {
        **_identity(snapshot),
        "status": "running",
        "activity": "inactive",
        "ownership": "control_plane",
        "current_step": "implement",
    }
    snapshot["processes"] = {"controller_count": 1}

    report = helper.build_report_input(snapshot)

    assert report["status"] == "running"
    assert report["activity"] == "inactive"
    assert report["ownership"] == "control_plane"


def test_canonical_observation_with_wrong_pinned_ownership_is_rejected(
    tmp_path: Path,
) -> None:
    helper = _helper()
    snapshot = _snapshot(tmp_path, latest_result=None)
    snapshot["ownership"] = "legacy"
    snapshot["canonical_observation"] = {
        **_identity(snapshot),
        "status": "running",
        "activity": "active",
        "ownership": "control_plane",
    }

    with pytest.raises(helper.ReportInputError, match="pinned ownership"):
        helper.build_report_input(snapshot)


def test_server_owned_process_snapshot_without_canonical_status_is_rejected(
    tmp_path: Path,
) -> None:
    helper = _helper()
    snapshot = _snapshot(tmp_path)
    snapshot["ownership"] = "ui-server"
    snapshot.pop("activity")

    with pytest.raises(helper.ReportInputError, match="canonical status"):
        helper.build_report_input(snapshot)


def test_missing_ownership_does_not_fallback_to_process_liveness(tmp_path: Path) -> None:
    helper = _helper()
    snapshot = _snapshot(tmp_path)
    snapshot.pop("ownership")
    snapshot.pop("activity")
    snapshot["classification"] = "active_progress"

    report = helper.build_report_input(snapshot)

    assert report["ownership"] == "unavailable"
    assert report["activity"] == "unavailable"


def test_wrong_identity_and_unredacted_schema_are_rejected(tmp_path: Path) -> None:
    helper = _helper()
    snapshot = _snapshot(tmp_path)
    wrong = dict(snapshot)
    wrong["run_id"] = "another-run"
    wrong["latest_result"] = {
        **snapshot["latest_result"],
        "repository": str(Path(snapshot["repo"]).resolve()),
        "run_id": "different-run",
        "finished_at": snapshot["observed_at"],
        "status": "completed",
    }
    with pytest.raises(helper.ReportInputError, match="run_id"):
        helper.build_report_input(wrong, finalized_evidence=wrong["latest_result"])

    missing_identity = dict(snapshot["latest_result"])
    missing_identity.pop("run_id", None)
    with pytest.raises(helper.ReportInputError, match="pinned identity"):
        helper.build_report_input(snapshot, finalized_evidence=missing_identity)

    malformed = helper.build_report_input(snapshot)
    malformed["diagnosis"]["owner_action"] = "password=not-secret"
    with pytest.raises(helper.ReportInputError, match="unredacted secret"):
        helper.validate_report_input(malformed)


def test_oversized_and_secret_values_are_redacted_before_validation(tmp_path: Path) -> None:
    helper = _helper()
    snapshot = _snapshot(tmp_path)
    snapshot["latest_result"]["summary"] = "password=DO-NOT-RETAIN " + "x" * 2_000
    snapshot["latest_result"]["selector"] = "s" * 2_000

    report = helper.build_report_input(snapshot)

    assert "DO-NOT-RETAIN" not in str(report)
    assert len(report["finalized_turn_summary"]) <= helper.MAX_STRING_LENGTH
    assert len(report["worker_selector"]) <= helper.MAX_STRING_LENGTH
    for key in ("confirmed_facts", "supporting_evidence", "contradicting_evidence", "unknowns"):
        assert len(report["diagnosis"][key]) <= helper.MAX_ARRAY_ITEMS


def test_invalid_snapshot_schema_and_unavailable_remote_source_stay_unknown(
    tmp_path: Path,
) -> None:
    helper = _helper()
    invalid = _snapshot(tmp_path)
    invalid["schema_version"] = 99
    with pytest.raises(helper.ReportInputError, match="snapshot schema"):
        helper.build_report_input(invalid)

    unavailable_root = tmp_path / "unavailable-source"
    unavailable_root.mkdir()
    snapshot = _snapshot(
        unavailable_root,
        classification="orphaned_controller",
        latest_result=None,
    )
    snapshot["run"]["status"] = "running"
    identity = _identity(snapshot)
    diagnosis = helper.build_report_input(
        snapshot,
        cause_evidence={
            "harness_session": {
                **identity,
                "available": False,
                "permission": "unavailable",
                "supported": True,
                "terminal": True,
                "terminal_status": "failed",
                "at": snapshot["observed_at"],
            }
        },
    )["diagnosis"]

    assert diagnosis["likely_cause"] is None
    assert diagnosis["confidence"] == "unknown"
    assert any("unavailable" in item for item in diagnosis["unknowns"])


def test_finalized_evidence_with_newer_starting_record_is_not_replaced(
    tmp_path: Path,
) -> None:
    helper = _helper()
    snapshot = _snapshot(tmp_path)
    snapshot["latest_result"] = {
        "name": "turn-001",
        "path": "turns/turn-001/result.json",
        "status": "completed",
        "finished_at": "2026-09-11T12:00:05+00:00",
        "finalized": True,
        "summary": "finished result remains selected",
    }
    snapshot["progress"]["current_turn"] = {
        "turn_number": 2,
        "step": "review",
        "status": "starting",
        "summary": None,
    }
    snapshot["progress"]["last_finished_turn"]["summary"] = "finished result remains selected"

    report = helper.build_report_input(snapshot)

    assert report["finalized_turn_summary"] == "finished result remains selected"
    assert report["step"] == "review"
