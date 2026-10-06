from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest

from aflow.cli import _bootstrap_resume_invocation
from aflow.run_state import ControllerConfig
from aflow.workflow import WorkflowError, run_workflow
from aflow.harnesses.codex import CodexAdapter
from tests.test_repair_upgrades import _workflow_config
from tests._support import _make_lifecycle_git_repo
from aflow.control_plane import InMemoryUnitManager, write_launch_phase
from aflow.daemon import AflowDaemon, DaemonConfig, _worker_prepared

_INITIAL = """# Plan

### [ ] Checkpoint 1: First
- [ ] first

### [ ] Checkpoint 2: Second
- [ ] second
"""
_IMPLEMENTED = _INITIAL.replace("### [ ] Checkpoint 1: First\n- [ ] first", "### [x] Checkpoint 1: First\n- [x] first")
_REPAIR = "# Repair\n\n### [ ] Checkpoint 1: Repair\n- [ ] fix validation\n"


@pytest.fixture(params=[(False, False), (True, False), (False, True), (True, True)], ids=["in-place", "worktree", "in-place-flat", "worktree-flat"])
def checkpoint_repair(tmp_path, request):
    worktree, flat_overlay = request.param
    repair_text = _REPAIR if not flat_overlay else "# Repair\n\n- [ ] fix validation\n"
    root = tmp_path / "repo"
    root.mkdir()
    if worktree:
        _make_lifecycle_git_repo(root)
        (root / ".gitignore").write_text(".aflow/\nplan*.md\naflow.toml\nworker.env\nrelease/\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "fixture ignores"], cwd=root, check=True, capture_output=True)
    plan = root / "plan.md"
    plan.write_text(_INITIAL)
    config_path = root / "aflow.toml"
    config_path.write_text("# fixture\n")
    config = _workflow_config(manager_enabled=False, threshold=1, review_checked_original=True)
    config = replace(config, aflow=replace(config.aflow, worktree_root=str(tmp_path / "worktrees")),
                     workflows={"repair": replace(config.workflows["repair"],
                                setup=("worktree", "branch") if worktree else (),
                                main_branch="main" if worktree else None)})
    config = replace(config, prompts={"p": "Active: {ACTIVE_PLAN_PATH}\nRepair: {NEW_PLAN_PATH}"})
    calls = []

    def runner(argv, **kwargs):
        calls.append(kwargs)
        source = next((root / ".aflow/runs").iterdir())
        turn = source / "turns" / f"turn-{len(calls):03d}"
        prompt = (turn / "effective-prompt.txt").read_text()
        execution = Path(kwargs["cwd"])
        active = Path(prompt.split("Active: ", 1)[1].splitlines()[0])
        repair = Path(prompt.split("Repair: ", 1)[1].splitlines()[0])
        if not active.is_relative_to(execution):
            active = execution / active.relative_to(root)
        if not repair.is_relative_to(execution):
            repair = execution / repair.relative_to(root)
        if len(calls) == 1:
            (execution / plan.name).write_text(_IMPLEMENTED)
        elif len(calls) in {2, 4}:
            repair.write_text(repair_text)
        elif len(calls) == 3:
            active.write_text(repair_text.replace("[ ]", "[x]"))
        else:
            (execution / "unfinished.py").write_text("# preserve dirty repair\n")
            return subprocess.CompletedProcess(argv, 0, "AFLOW_STOP: required native validation resource busy", "")
        return subprocess.CompletedProcess(argv, 0, "done", "")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(repo_root=root, plan_path=plan, max_turns=12,
                             reserved_run_id="checkpoint-repair-source",
                             caller_scope="project:one", idempotency_key="source-key"),
            config, "repair", config_dir=root, snapshot_config=False,
            adapter=CodexAdapter(), runner=runner,
        )
    source = failure.value.run_dir
    payload = json.loads((source / "run.json").read_text())
    assert len(calls) == 5
    assert payload["last_snapshot"]["current_checkpoint_index"] == 2
    assert payload["active_implementation_scope"]["checkpoint_index"] == 1
    assert len(payload["review_rejection_history"]) == 2
    return root, plan, config_path, config, source, payload


def bootstrap(case):
    root, plan, config_path, config, source, payload = case
    return _bootstrap_resume_invocation(
        repo_root=root, config_path=config_path, config_path_is_explicit=True,
        workflow_config=config, requested_run_id=source.name, workflow_arg=None,
        plan_file_arg=None, team_arg=None, start_step_arg=None, max_turns_arg=None,
        extra_instructions_arg=(), extra_instructions_provided=False,
        live_loader=lambda _: config,
    )


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_preview_preserves_rejected_checkpoint_repair(checkpoint_repair):
    root, plan, config_path, config, source, payload = checkpoint_repair
    before = files(source)
    execution = Path(payload.get("execution_repo_root") or root)
    original = (execution / plan.name).read_bytes()
    context = bootstrap(checkpoint_repair).resume_context
    assert context.active_implementation_scope.checkpoint_index == 1
    assert context.review_repair_step == context.interrupted_step_name == "implement"
    assert context.resume_scope_reconciled is False
    assert context.active_plan_path == Path(payload["active_plan_path"])
    assert context.reviewer_rejection_count == 2
    assert len(context.review_rejection_history) == 2
    assert context.pending_step_team_override.selector == "codex.worker-high"
    assert context.implementation_attempts
    assert context.scope_envelope_bytes == (source / payload["active_implementation_scope"]["envelope_artifact_path"]).read_bytes()
    assert files(source) == before
    assert (execution / plan.name).read_bytes() == original
    assert (execution / "unfinished.py").read_text() == "# preserve dirty repair\n"


def test_successor_repairs_same_scope_then_reviews_without_pruning_source(checkpoint_repair):
    root, plan, config_path, config, source, payload = checkpoint_repair
    execution = Path(payload.get("execution_repo_root") or root)
    overlay = execution / Path(payload["active_plan_path"]).relative_to(root)
    before = files(source)
    original = (execution / plan.name).read_bytes()
    context = bootstrap(checkpoint_repair).resume_context
    calls = []

    def runner(argv, **kwargs):
        calls.append(kwargs)
        successor = next(p for p in (root / ".aflow/runs").iterdir() if p != source)
        current = json.loads((successor / "run.json").read_text())
        assert current["active_implementation_scope"]["checkpoint_index"] == 1
        assert current["review_rejection_history"] == payload["review_rejection_history"]
        assert (execution / plan.name).read_bytes() == original
        if len(calls) == 1:
            prompt = (successor / "turns/turn-001/effective-prompt.txt").read_text()
            assert f"Active: {overlay}" in prompt
            assert "worker-high" in argv
            overlay.write_text(overlay.read_text().replace("[ ]", "[x]"))
            return subprocess.CompletedProcess(argv, 0, "repair verified", "")
        assert current["current_step_name"] == "review"
        return subprocess.CompletedProcess(argv, 0, "AFLOW_STOP: reviewer reached before scope advancement", "")

    with pytest.raises(WorkflowError, match="reviewer reached"):
        run_workflow(
            ControllerConfig(repo_root=root, plan_path=plan, max_turns=12, keep_runs=1),
            config, "repair", config_dir=root, snapshot_config=False,
            adapter=CodexAdapter(), runner=runner, resume=context,
        )
    assert len(calls) == 2
    assert files(source) == before
    assert (execution / "unfinished.py").read_text() == "# preserve dirty repair\n"


def test_managed_preview_worker_bootstrap_and_idempotency(checkpoint_repair, monkeypatch):
    root, plan, config_path, config, source, payload = checkpoint_repair
    env = root / "worker.env"
    env.write_text("AFLOW_FIXTURE=1\n")
    executable = root / "release/bin/aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda _: config)
    units = InMemoryUnitManager()
    write_launch_phase(root, source.name, "failed")
    daemon = AflowDaemon(DaemonConfig(
        repo_root=root, config_path=config_path, aflow_executable=executable,
        environment_file=env, release_identity="fixture", stop_timeout_seconds=0,
    ), units=units)
    daemon.start()
    before = files(source)
    assert daemon.service.run_status(source.name).evidence["can_resume"] is True
    assert files(source) == before
    successor = daemon.service.resume(source.name, caller_scope="project:one", idempotency_key="repair-resume")
    replay = daemon.service.resume(source.name, caller_scope="project:one", idempotency_key="repair-resume")
    assert successor.run_id == replay.run_id
    assert len(units.start_calls) == 1
    record = daemon.service._read_record(successor.run_id)
    manifest = daemon.application.repository.get_launch_manifest(successor.run_id)
    prepared, context = _worker_prepared(record, manifest, root, config_path, config)
    assert context.review_repair_step == context.interrupted_step_name == "implement"
    assert context.active_implementation_scope.checkpoint_index == 1
    assert context.pending_step_team_override.selector == "codex.worker-high"
    assert context.reviewer_rejection_count == 2
    assert context.active_plan_path == Path(payload["active_plan_path"])
    # The managed API appends resume_requested to its operational event log.
    # Immutable predecessor evidence and plan artifacts retain their exact bytes.
    after = files(source)
    assert {k: v for k, v in after.items() if k != "events.jsonl"} == {
        k: v for k, v in before.items() if k != "events.jsonl"
    }


@pytest.mark.parametrize("damage", [
    "other_overlay", "forged_rejection", "wrong_worker", "finalized_repair",
    "active_owner", "extra_turn", "missing_overlay", "pending_control",
    "wrong_scope", "changed_original", "wrong_override", "completed_overlay", "partition_override",
    "missing_upgrade", "changed_scope_bytes",
])
def test_rejects_unproven_checkpoint_repair(checkpoint_repair, damage):
    root, plan, config_path, config, source, payload = checkpoint_repair
    review_path = source / "turns/turn-004/result.json"
    repair_path = source / "turns/turn-005/result.json"
    review = json.loads(review_path.read_text())
    repair = json.loads(repair_path.read_text())
    execution = Path(payload.get("execution_repo_root") or root)
    if damage == "other_overlay":
        review["new_plan_path"] = str(root / "unrelated.md")
    elif damage == "forged_rejection":
        review["review_rejection"]["scope_id"] = "forged"
    elif damage == "wrong_worker":
        repair["active_plan_path"] = str(plan)
    elif damage == "finalized_repair":
        repair["status"] = "completed"
    elif damage == "active_owner":
        payload["status"] = "running"
    elif damage == "extra_turn":
        (source / "turns/turn-006").mkdir()
    elif damage == "missing_overlay":
        (execution / Path(payload["active_plan_path"]).relative_to(root)).unlink()
    elif damage == "pending_control":
        payload["active_implementation_scope"]["current_partition_id"] = "unresolved"
    elif damage == "completed_overlay":
        overlay = execution / Path(payload["active_plan_path"]).relative_to(root)
        overlay.write_text(overlay.read_text().replace("[ ]", "[x]"))
    elif damage == "partition_override":
        payload["pending_step_team_override"]["repartition_partition_id"] = "unresolved"
    elif damage == "missing_upgrade":
        payload["pending_step_team_override"] = None
    elif damage == "changed_scope_bytes":
        (execution / plan.name).write_text(_IMPLEMENTED.replace("- [x] first", "- [x] different task"))
    elif damage == "wrong_scope":
        payload["active_implementation_scope"]["original_plan_path"] = str(root / "other.md")
    elif damage == "changed_original":
        (execution / plan.name).write_text(_INITIAL)
    else:
        payload["pending_step_team_override"]["target_step"] = "review"
    review_path.write_text(json.dumps(review))
    repair_path.write_text(json.dumps(repair))
    (source / "run.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        bootstrap(checkpoint_repair)



def test_repeated_repair_interruptions_preserve_rejection_owner(checkpoint_repair):
    root, plan, config_path, config, source, payload = checkpoint_repair
    original_owner = source
    original_bytes = files(source)
    case = checkpoint_repair
    for number in range(3):
        context = bootstrap(case).resume_context
        assert context.reviewer_rejection_count == 2
        assert context.pending_step_team_override.selector == "codex.worker-high"
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            assert "worker-high" in argv
            return subprocess.CompletedProcess(argv, 0, "AFLOW_STOP: validation resource busy again", "")

        with pytest.raises(WorkflowError, match="resource busy again") as failure:
            run_workflow(
                ControllerConfig(repo_root=root, plan_path=plan, max_turns=12, keep_runs=1),
                config, "repair", config_dir=root, snapshot_config=False,
                adapter=CodexAdapter(), runner=runner, resume=context,
            )
        assert len(calls) == 1
        successor = failure.value.run_dir
        successor_payload = json.loads((successor / "run.json").read_text())
        assert successor_payload["turns_completed"] == 0
        assert files(original_owner) == original_bytes
        case = root, plan, config_path, config, successor, successor_payload
    assert bootstrap(case).resume_context.active_implementation_scope.checkpoint_index == 1



@pytest.mark.parametrize("parent", ["unrelated-run", "..", "cycle"])
def test_inherited_repair_rejects_unproven_lineage(checkpoint_repair, parent):
    root, plan, config_path, config, source, payload = checkpoint_repair
    context = bootstrap(checkpoint_repair).resume_context
    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(repo_root=root, plan_path=plan, max_turns=12, keep_runs=1),
            config, "repair", config_dir=root, snapshot_config=False,
            adapter=CodexAdapter(), resume=context,
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 0, "AFLOW_STOP: validation resource busy again", ""
            ),
        )
    successor = failure.value.run_dir
    successor_payload = json.loads((successor / "run.json").read_text())
    successor_payload["resumed_from_run_id"] = successor.name if parent == "cycle" else parent
    (successor / "run.json").write_text(json.dumps(successor_payload))
    case = root, plan, config_path, config, successor, successor_payload
    with pytest.raises(ValueError):
        bootstrap(case)
