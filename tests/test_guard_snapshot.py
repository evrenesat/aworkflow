from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


HELPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "aflow"
    / "bundled_skills"
    / "aflow-guard-development-run"
    / "scripts"
    / "aflow_guard_snapshot.py"
)


def _helper_module():
    spec = importlib.util.spec_from_file_location("guard_snapshot", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(
    *,
    status: str,
    plan: Path,
    team: str = "team",
) -> dict[str, object]:
    return {
        "status": status,
        "turns_completed": 0,
        "feature_branch": None,
        "worktree_path": None,
        "resumed_from_run_id": None,
        "plan_path": str(plan),
        "original_plan_path": str(plan),
        "workflow_name": "workflow",
        "team": team,
        "selected_start_step": "implement",
        "effective_max_turns": 40,
        "extra_instructions": [],
        "frozen_config": {
            "workflow_name": "workflow",
            "config_path": str(plan.parent / "aflow.toml"),
            "config_fingerprint": "f" * 64,
        },
        "last_snapshot": {"is_complete": False},
    }


def test_replacement_linkage_uses_original_recovery_fingerprint_and_is_fail_closed(
    tmp_path: Path,
) -> None:
    helper = _helper_module()
    repo = tmp_path / "repo"
    plan = repo / "plan.md"
    plan.parent.mkdir()
    plan.write_text("# plan\n", encoding="utf-8")
    predecessor_id = "20260802T231403Z-11be6e20"
    successor_id = "20260802T235429Z-dc254244"
    predecessor_path = repo / ".aflow" / "runs" / predecessor_id / "run.json"
    successor_path = repo / ".aflow" / "runs" / successor_id / "run.json"
    predecessor_path.parent.mkdir(parents=True)
    successor_path.parent.mkdir(parents=True)
    predecessor_path.write_text(
        json.dumps(_run(status="failed", plan=plan)), encoding="utf-8"
    )
    successor_path.write_text(
        json.dumps(_run(status="running", plan=plan)), encoding="utf-8"
    )

    original_fingerprint = "6c935122dc7c867483b974a3"
    unsafe_fingerprint = "158e067adc6025a764e34efa"
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "last_fingerprint": unsafe_fingerprint,
                "recovery_fingerprints": [original_fingerprint],
                "notified_fingerprints": [original_fingerprint],
                "replacement_successor": {
                    "predecessor_run_id": predecessor_id,
                    "successor_run_id": successor_id,
                    "predecessor_fingerprint": unsafe_fingerprint,
                },
            }
        ),
        encoding="utf-8",
    )
    controller = helper.ProcessRecord(
        pid=520120,
        ppid=1,
        pgid=520120,
        state="S",
        elapsed="00:01",
        command=f"/usr/bin/python /tmp/aflow run --plan {plan}",
        cwd=str(repo),
    )

    linked = helper.collect_snapshot(
        repo,
        predecessor_id,
        None,
        state_path,
        write_state=True,
        mark_recovery_attempt=False,
        mark_notified=False,
        process_records=[controller],
        replacement_successor_run_id=successor_id,
    )

    assert linked["classification"] == "replacement_linked"
    assert linked["fingerprint"] == original_fingerprint
    assert linked["replacement_successor"]["predecessor_fingerprint"] == original_fingerprint
    assert linked["replacement_successor"]["migrated_from_predecessor_fingerprint"] == unsafe_fingerprint
    repaired_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert repaired_state["last_fingerprint"] == unsafe_fingerprint
    assert repaired_state["replacement_successor"]["predecessor_fingerprint"] == original_fingerprint

    successor_path.write_text(
        json.dumps(_run(status="running", plan=plan, team="wrong-team")),
        encoding="utf-8",
    )
    before = state_path.read_bytes()
    with pytest.raises(ValueError, match="does not match predecessor"):
        helper.collect_snapshot(
            repo,
            predecessor_id,
            None,
            state_path,
            write_state=True,
            mark_recovery_attempt=False,
            mark_notified=False,
            process_records=[controller],
            replacement_successor_run_id=successor_id,
        )
    assert state_path.read_bytes() == before

    missing_identity = _run(status="running", plan=plan)
    missing_identity.pop("frozen_config")
    successor_path.write_text(json.dumps(missing_identity), encoding="utf-8")
    before = state_path.read_bytes()
    with pytest.raises(ValueError, match="successor frozen_config is missing or invalid"):
        helper.collect_snapshot(
            repo,
            predecessor_id,
            None,
            state_path,
            write_state=True,
            mark_recovery_attempt=False,
            mark_notified=False,
            process_records=[controller],
            replacement_successor_run_id=successor_id,
        )
    assert state_path.read_bytes() == before


@pytest.mark.parametrize(
    ("run_name", "missing_key"),
    [
        (run_name, missing_key)
        for run_name in ("predecessor", "successor")
        for missing_key in (
            "workflow_name",
            "team",
            "selected_start_step",
            "effective_max_turns",
            "extra_instructions",
            "frozen_config",
        )
    ],
)
def test_replacement_identity_rejects_missing_required_fields(
    tmp_path: Path,
    run_name: str,
    missing_key: str,
) -> None:
    helper = _helper_module()
    repo = tmp_path / "repo"
    plan = repo / "plan.md"
    plan.parent.mkdir()
    plan.write_text("# plan\n", encoding="utf-8")
    predecessor = _run(status="failed", plan=plan)
    successor = _run(status="running", plan=plan)
    selected = predecessor if run_name == "predecessor" else successor
    selected.pop(missing_key)

    errors = helper._replacement_identity_errors(repo, predecessor, successor)

    assert f"{run_name} {missing_key} is missing or invalid" in errors


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("workflow_name", ""),
        ("team", None),
        ("selected_start_step", ""),
        ("effective_max_turns", True),
        ("extra_instructions", "not-a-list"),
        ("frozen_config", {"workflow_name": "workflow"}),
    ],
)
def test_replacement_identity_rejects_malformed_required_fields(
    tmp_path: Path,
    field: str,
    invalid_value: object,
) -> None:
    helper = _helper_module()
    repo = tmp_path / "repo"
    plan = repo / "plan.md"
    plan.parent.mkdir()
    plan.write_text("# plan\n", encoding="utf-8")
    predecessor = _run(status="failed", plan=plan)
    successor = _run(status="running", plan=plan)
    predecessor[field] = invalid_value

    errors = helper._replacement_identity_errors(repo, predecessor, successor)

    assert f"predecessor {field} is missing or invalid" in errors


@pytest.mark.parametrize("run_name", ["predecessor", "successor"])
def test_replacement_identity_requires_explicit_original_plan_path(
    tmp_path: Path,
    run_name: str,
) -> None:
    helper = _helper_module()
    repo = tmp_path / "repo"
    plan = repo / "plan.md"
    plan.parent.mkdir()
    plan.write_text("# plan\n", encoding="utf-8")
    predecessor = _run(status="failed", plan=plan)
    successor = _run(status="running", plan=plan)
    selected = predecessor if run_name == "predecessor" else successor
    selected.pop("original_plan_path")

    errors = helper._replacement_identity_errors(repo, predecessor, successor)

    assert "successor original plan does not match predecessor" in errors


def test_replacement_identity_requires_existing_original_plan(tmp_path: Path) -> None:
    helper = _helper_module()
    repo = tmp_path / "repo"
    plan = repo / "missing-plan.md"
    repo.mkdir()

    errors = helper._replacement_identity_errors(
        repo,
        _run(status="failed", plan=plan),
        _run(status="running", plan=plan),
    )

    assert "predecessor original plan does not exist" in errors
