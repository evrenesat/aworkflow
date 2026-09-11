from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


HELPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "aflow"
    / "bundled_skills"
    / "aflow-guard-development-run"
    / "scripts"
    / "aflow_guard_recovery.py"
)


def _helper_module():
    spec = importlib.util.spec_from_file_location("guard_recovery", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _receipt(tmp_path: Path) -> dict:
    helper = _helper_module()
    repository = tmp_path / "repo"
    repository.mkdir()
    plan = repository / "plan.md"
    plan.write_text("# unchanged plan\n", encoding="utf-8")
    plan_digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    choices_digest = hashlib.sha256(b"selected launch choices").hexdigest()
    return {
        "schema_version": helper.SCHEMA_VERSION,
        "evidence": {
            "source": "matched_launch_evidence",
            "matched": True,
            "authorization": {
                "authorized": True,
                "ownership_mode": "legacy",
                "launch_surface": "legacy_cli",
            },
            "request": {
                "startup_request_id": "startup-1",
                "idempotency_key": "idempotency-1",
            },
            "failure": {
                "status": "failed",
                "phase": "pre_controller",
                "kind": "missing_or_blank_git_tracking",
                "terminal": True,
                "missing_fields": ["plan_branch"],
            },
            "identity": {
                "predecessor_run_id": "run-1",
                "matched_run_id": "run-1",
                "startup_request_id": "startup-1",
            },
            "work": {
                "started_turns": 0,
                "finalized_turns": 0,
                "worker_changes": False,
                "branch_created": False,
                "worktree_created": False,
                "plan_content_unchanged": True,
                "plan_semantics_unchanged": True,
                "launch_choices_unchanged": True,
            },
            "ownership": {
                "controller": "none",
                "child": "none",
                "provider_session": "none",
            },
            "selected_launch": {
                "repository": str(repository),
                "plan": str(plan),
                "workflow": "checkpoint_delivery",
                "team": "codex",
                "start_step": "1",
                "max_turns": 4,
                "extra_instructions": [],
                "choices_digest": choices_digest,
                "original_plan_content_sha256": plan_digest,
                "branch": "aflow-neutral-recovery",
                "base_head": "a" * 40,
            },
            "derivation": {
                "mechanically_derivable": True,
                "method": "in_place_current_branch",
                "branch": "aflow-neutral-recovery",
                "base_head": "a" * 40,
            },
            "match": {
                "startup_request_id": "startup-1",
                "selected_plan": str(plan),
                "selected_choices_digest": choices_digest,
                "idempotency_key": "idempotency-1",
                "predecessor_run_id": "run-1",
            },
            "content": {
                "sha256": plan_digest,
                "unchanged": True,
                "semantic_unchanged": True,
            },
        },
    }


def _set(receipt: dict, path: str, value: object) -> None:
    target: dict = receipt
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


def test_one_eligible_receipt_claims_and_records_one_successor(tmp_path: Path) -> None:
    helper = _helper_module()
    receipt = _receipt(tmp_path)
    state_dir = tmp_path / "guard-state"
    state_dir.mkdir()
    calls: list[dict] = []
    replacement_key = helper.claim_attempt(receipt, state_dir)["record"]["request"][
        "replacement_idempotency_key"
    ]
    (state_dir / helper.ATTEMPT_FILE_NAME).unlink()

    def launch(request: dict) -> dict:
        calls.append(request)
        return {
            "acknowledged": True,
            "successor": {
                "run_id": "run-2",
                "idempotency_key": replacement_key,
            },
        }

    assert helper.evaluate_eligibility(receipt).eligible is True
    result = helper.attempt_recovery(receipt, state_dir, launch)

    assert result["status"] == "acknowledged"
    assert len(calls) == 1
    assert calls[0] == {
        **receipt["evidence"]["selected_launch"],
        "idempotency_key": replacement_key,
    }
    record = json.loads(
        (state_dir / helper.ATTEMPT_FILE_NAME).read_text(encoding="utf-8")
    )
    assert record["status"] == "acknowledged"
    assert record["correction_reason"] == "missing_or_blank_git_tracking"
    assert record["predecessor"] == {
        "run_id": "run-1",
        "startup_request_id": "startup-1",
    }
    assert record["request"]["predecessor_idempotency_key"] == "idempotency-1"
    assert record["request"]["replacement_idempotency_key"] == replacement_key
    assert replacement_key != "idempotency-1"
    assert record["original_plan_content_sha256"] == receipt["evidence"]["selected_launch"][
        "original_plan_content_sha256"
    ]
    assert record["selected"]["plan"] == receipt["evidence"]["selected_launch"]["plan"]
    assert record["successor"] == {
        "run_id": "run-2",
        "idempotency_key": replacement_key,
    }


def test_repeated_attempt_is_durable_and_never_launches_again(tmp_path: Path) -> None:
    helper = _helper_module()
    receipt = _receipt(tmp_path)
    state_dir = tmp_path / "guard-state"
    state_dir.mkdir()
    calls = 0

    def launch(_: dict) -> dict:
        nonlocal calls
        calls += 1
        return {"status": "failed"}

    first = helper.attempt_recovery(receipt, state_dir, launch)
    second = helper.attempt_recovery(receipt, state_dir, launch)

    assert first["status"] == "failed"
    assert second["status"] == "already_attempted"
    assert calls == 1


def test_changed_evidence_pauses_after_the_durable_claim(tmp_path: Path) -> None:
    helper = _helper_module()
    receipt = _receipt(tmp_path)
    state_dir = tmp_path / "guard-state"
    state_dir.mkdir()
    first = helper.claim_attempt(receipt, state_dir)
    changed = copy.deepcopy(receipt)
    changed["evidence"]["selected_launch"]["team"] = "different-team"

    second = helper.claim_attempt(changed, state_dir)

    assert first["status"] == "claimed"
    assert second["status"] == "blocked"
    assert "changed after" in second["reason"]


def test_incompatible_old_attempt_record_fails_closed(tmp_path: Path) -> None:
    helper = _helper_module()
    receipt = _receipt(tmp_path)
    state_dir = tmp_path / "guard-state"
    state_dir.mkdir()
    attempt_path = state_dir / helper.ATTEMPT_FILE_NAME
    old_record = {
        "schema_version": helper.SCHEMA_VERSION,
        "kind": "neutral_startup_recovery",
        "status": "claimed",
        "request": {
            "startup_request_id": "startup-1",
            "idempotency_key": "idempotency-1",
        },
        "receipt_digest": "a" * 64,
    }
    attempt_path.write_text(json.dumps(old_record), encoding="utf-8")

    result = helper.claim_attempt(receipt, state_dir)

    assert result["status"] == "blocked"
    assert "invalid" in result["reason"]
    assert json.loads(attempt_path.read_text(encoding="utf-8")) == old_record


def test_uncertain_response_is_read_only_until_same_key_is_acknowledged(
    tmp_path: Path,
) -> None:
    helper = _helper_module()
    receipt = _receipt(tmp_path)
    state_dir = tmp_path / "guard-state"
    state_dir.mkdir()
    calls = 0

    def launch(_: dict) -> dict:
        nonlocal calls
        calls += 1
        return {"status": "uncertain"}

    uncertain = helper.attempt_recovery(receipt, state_dir, launch)
    repeated = helper.attempt_recovery(receipt, state_dir, launch)
    replacement_key = uncertain["record"]["request"]["replacement_idempotency_key"]
    acknowledged = helper.record_acknowledged_successor(
        state_dir,
        replacement_key,
        {"run_id": "run-2", "idempotency_key": replacement_key},
    )

    assert uncertain["status"] == "uncertain"
    assert repeated["status"] == "already_attempted"
    assert acknowledged["status"] == "acknowledged"
    assert calls == 1


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        ("evidence.work.started_turns", 1, "started_turns"),
        ("evidence.work.finalized_turns", 1, "finalized_turns"),
        ("evidence.work.worker_changes", True, "worker_changes"),
        ("evidence.work.branch_created", True, "branch_created"),
        ("evidence.work.worktree_created", True, "worktree_created"),
        ("evidence.work.plan_content_unchanged", False, "plan content"),
        ("evidence.work.plan_semantics_unchanged", False, "plan content"),
        ("evidence.work.launch_choices_unchanged", False, "launch_choices"),
        ("evidence.ownership.controller", "active", "controller"),
        ("evidence.ownership.controller", "unknown", "controller"),
        ("evidence.ownership.child", "active", "child"),
        ("evidence.ownership.provider_session", "unknown", "provider_session"),
        ("evidence.failure.kind", "provider_failure", "supported Git Tracking"),
        ("evidence.failure.phase", "controller", "before controller"),
        ("evidence.failure.terminal", False, "terminal"),
        ("evidence.identity.matched_run_id", "wrong-run", "wrong predecessor"),
        ("evidence.match.selected_plan", "/other/plan.md", "selected plan"),
        ("evidence.derivation.mechanically_derivable", False, "mechanically"),
        ("evidence.authorization.authorized", False, "authorized"),
    ],
)
def test_material_disqualifiers_are_ineligible_and_do_not_claim(
    tmp_path: Path, path: str, value: object, reason: str
) -> None:
    helper = _helper_module()
    receipt = _receipt(tmp_path)
    _set(receipt, path, value)
    state_dir = tmp_path / "guard-state"
    state_dir.mkdir()

    eligibility = helper.evaluate_eligibility(receipt)
    result = helper.claim_attempt(receipt, state_dir)

    assert eligibility.eligible is False
    assert any(reason.lower() in item.lower() for item in eligibility.reasons)
    assert result["status"] == "ineligible"
    assert not (state_dir / helper.ATTEMPT_FILE_NAME).exists()


def test_missing_or_unknown_evidence_is_ineligible(tmp_path: Path) -> None:
    helper = _helper_module()
    receipt = _receipt(tmp_path)
    del receipt["evidence"]["ownership"]["controller"]
    state_dir = tmp_path / "guard-state"
    state_dir.mkdir()

    result = helper.claim_attempt(receipt, state_dir)

    assert result["status"] == "ineligible"
    assert "active or unknown" in " ".join(result["eligibility"]["reasons"])


def test_server_owned_runs_require_web_mcp_and_never_cli(tmp_path: Path) -> None:
    helper = _helper_module()
    receipt = _receipt(tmp_path)
    _set(receipt, "evidence.authorization.ownership_mode", "ui-server")
    _set(receipt, "evidence.authorization.launch_surface", "legacy_cli")

    decision = helper.evaluate_eligibility(receipt)

    assert decision.eligible is False
    assert any("web/MCP" in reason for reason in decision.reasons)


def test_server_owned_runs_can_use_only_the_advertised_web_mcp_surface(
    tmp_path: Path,
) -> None:
    helper = _helper_module()
    receipt = _receipt(tmp_path)
    _set(receipt, "evidence.authorization.ownership_mode", "aflowd")
    _set(receipt, "evidence.authorization.launch_surface", "web_mcp")

    decision = helper.evaluate_eligibility(receipt)

    assert decision.eligible is True


def test_uncertain_acknowledgement_cannot_record_a_different_key(tmp_path: Path) -> None:
    helper = _helper_module()
    receipt = _receipt(tmp_path)
    state_dir = tmp_path / "guard-state"
    state_dir.mkdir()
    helper.attempt_recovery(receipt, state_dir, lambda _: {"status": "uncertain"})

    result = helper.record_acknowledged_successor(
        state_dir,
        "different-key",
        {"run_id": "run-2", "idempotency_key": "different-key"},
    )

    assert result["status"] == "blocked"
    assert "does not match" in result["reason"]


def _run_recovery_cli(
    receipt_path: Path,
    state_dir: Path,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(HELPER_PATH),
            "--receipt",
            str(receipt_path),
            "--state-dir",
            str(state_dir),
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_cli_claim_records_acknowledged_successor(tmp_path: Path) -> None:
    receipt = _receipt(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    state_dir = tmp_path / "guard-state"
    state_dir.mkdir()

    claimed = _run_recovery_cli(receipt_path, state_dir)
    claimed_payload = json.loads(claimed.stdout)
    replacement_key = claimed_payload["record"]["request"]["replacement_idempotency_key"]
    successor_path = tmp_path / "successor.json"
    successor_path.write_text(
        json.dumps({"run_id": "run-cli-2", "idempotency_key": replacement_key}),
        encoding="utf-8",
    )
    acknowledged = _run_recovery_cli(
        receipt_path,
        state_dir,
        "--record-acknowledged-successor",
        str(successor_path),
    )

    assert claimed.returncode == 0
    assert claimed_payload["status"] == "claimed"
    assert acknowledged.returncode == 0
    assert json.loads(acknowledged.stdout)["status"] == "acknowledged"
    persisted = json.loads(
        (state_dir / _helper_module().ATTEMPT_FILE_NAME).read_text(encoding="utf-8")
    )
    assert persisted["status"] == "acknowledged"
    assert persisted["request"]["predecessor_idempotency_key"] == "idempotency-1"
    assert persisted["successor"]["idempotency_key"] == replacement_key


def test_cli_outcomes_never_enable_relaunch(tmp_path: Path) -> None:
    helper = _helper_module()
    receipt = _receipt(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    failed_state = tmp_path / "failed-state"
    failed_state.mkdir()
    failed_claim = _run_recovery_cli(receipt_path, failed_state)
    failed_key = json.loads(failed_claim.stdout)["record"]["request"]["replacement_idempotency_key"]
    failed = _run_recovery_cli(
        receipt_path,
        failed_state,
        "--outcome",
        "failed",
        "--replacement-idempotency-key",
        failed_key,
    )
    repeated_failed = _run_recovery_cli(
        receipt_path,
        failed_state,
        "--outcome",
        "failed",
        "--replacement-idempotency-key",
        failed_key,
    )
    assert json.loads(failed.stdout)["status"] == "failed"
    assert json.loads(repeated_failed.stdout)["status"] == "already_failed"
    assert _run_recovery_cli(receipt_path, failed_state).stdout
    assert json.loads(_run_recovery_cli(receipt_path, failed_state).stdout)["status"] == "already_attempted"

    state_dir = tmp_path / "uncertain-state"
    state_dir.mkdir()
    claim = _run_recovery_cli(receipt_path, state_dir)
    replacement_key = json.loads(claim.stdout)["record"]["request"]["replacement_idempotency_key"]
    uncertain_args = (
        "--outcome",
        "uncertain",
        "--replacement-idempotency-key",
        replacement_key,
    )
    uncertain = _run_recovery_cli(receipt_path, state_dir, *uncertain_args)
    repeated_uncertain = _run_recovery_cli(receipt_path, state_dir, *uncertain_args)
    changed_successor_path = tmp_path / "changed-successor.json"
    changed_successor_path.write_text(
        json.dumps({"run_id": "run-cli-changed", "idempotency_key": replacement_key}),
        encoding="utf-8",
    )
    changed_key = _run_recovery_cli(
        receipt_path,
        state_dir,
        "--outcome",
        "acknowledged",
        "--replacement-idempotency-key",
        "different-key",
        "--successor-json",
        str(changed_successor_path),
    )
    successor_path = tmp_path / "successor.json"
    successor_path.write_text(
        json.dumps({"run_id": "run-cli-2", "idempotency_key": replacement_key}),
        encoding="utf-8",
    )
    acknowledged = _run_recovery_cli(
        receipt_path,
        state_dir,
        "--outcome",
        "acknowledged",
        "--replacement-idempotency-key",
        replacement_key,
        "--successor-json",
        str(successor_path),
    )
    conflicting_path = tmp_path / "conflicting-successor.json"
    conflicting_path.write_text(
        json.dumps({"run_id": "run-cli-3", "idempotency_key": replacement_key}),
        encoding="utf-8",
    )
    conflicting = _run_recovery_cli(
        receipt_path,
        state_dir,
        "--outcome",
        "acknowledged",
        "--replacement-idempotency-key",
        replacement_key,
        "--successor-json",
        str(conflicting_path),
    )

    assert json.loads(uncertain.stdout)["status"] == "uncertain"
    assert json.loads(repeated_uncertain.stdout)["status"] == "already_uncertain"
    assert changed_key.returncode == 1
    assert json.loads(changed_key.stdout)["status"] == "blocked"
    assert acknowledged.returncode == 0
    assert conflicting.returncode == 1
    assert "conflicting" in json.loads(conflicting.stdout)["reason"]
    persisted = json.loads((state_dir / helper.ATTEMPT_FILE_NAME).read_text(encoding="utf-8"))
    assert persisted["successor"]["run_id"] == "run-cli-2"
    assert json.loads(_run_recovery_cli(receipt_path, state_dir).stdout)["status"] == "already_attempted"


def test_replacement_key_is_durable_and_distinct(tmp_path: Path) -> None:
    helper = _helper_module()
    receipt = _receipt(tmp_path)
    state_dir = tmp_path / "guard-state"
    state_dir.mkdir()
    launches: list[dict[str, object]] = []

    first = helper.attempt_recovery(
        receipt,
        state_dir,
        lambda request: launches.append(request) or {"status": "failed"},
    )
    repeated = helper.claim_attempt(receipt, state_dir)
    replacement_key = first["record"]["request"]["replacement_idempotency_key"]

    assert first["status"] == "failed"
    assert repeated["status"] == "already_attempted"
    assert launches == [{**receipt["evidence"]["selected_launch"], "idempotency_key": replacement_key}]
    assert replacement_key != receipt["evidence"]["request"]["idempotency_key"]
    assert repeated["record"]["request"]["replacement_idempotency_key"] == replacement_key
