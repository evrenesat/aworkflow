from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
import time

import pytest

from datetime import datetime, timezone

from aflow.api.runner import execute_workflow as _real_execute_workflow
from aflow.cli import _bootstrap_resume_invocation
from aflow.run_state import ControllerConfig
from aflow.workflow import WorkflowError, run_workflow
from aflow.harnesses.codex import CodexAdapter
from aflow.process_identity import process_birth_identity
from tests.test_repair_upgrades import _workflow_config
from tests._support import _make_lifecycle_git_repo
from aflow.control_plane import (
    InMemoryUnitManager,
    RunControlRequest,
    compare_and_swap_overrides,
    write_launch_phase,
)
from aflow.control_plane.worker_diagnostics import (
    confirmed_inactive,
    worker_evidence,
)
from aflow.daemon import AflowDaemon, DaemonConfig, _unit_name, _worker_prepared, worker_main
from aflow.control_plane.units import UnitState

_REPO_ROOT = Path(__file__).resolve().parents[1]

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


# Issue #55: a managed owner stop that kills the controller mid-repair turn
# leaves run.json "running", a complete final-checkpoint snapshot, and an
# unfinalized "starting" worker repair receipt.  These fixtures produce that
# shape with a real workflow in a child controller, a real managed owner stop
# through the daemon, and a real SIGKILL of the controller.

_STOPPED_INITIAL = """# Plan

### [ ] Checkpoint 1: Only
- [ ] implement
"""
_STOPPED_IMPLEMENTED = """# Plan

### [x] Checkpoint 1: Only
- [x] implement
"""
_STOPPED_REPAIR = "# Repair\n\n### [ ] Checkpoint 1: Repair\n- [ ] fix validation\n"

_STOPPED_DRIVER = '''
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

root = Path(sys.argv[1])
source_id = sys.argv[2]
worktree = sys.argv[3] == "1"
plan = root / "plan.md"

from aflow.harnesses.codex import CodexAdapter
from aflow.run_state import ControllerConfig
from aflow.workflow import run_workflow
from tests.test_repair_upgrades import _workflow_config

config = _workflow_config(manager_enabled=False, threshold=1, review_checked_original=True)
config = replace(config, aflow=replace(config.aflow, worktree_root=str(root.parent / "worktrees")),
                 workflows={{"repair": replace(config.workflows["repair"],
                            setup=("worktree", "branch") if worktree else (),
                            main_branch="main" if worktree else None)}})
config = replace(config, prompts={{"p": "Active: {{ACTIVE_PLAN_PATH}}\\nRepair: {{NEW_PLAN_PATH}}"}})

IMPLEMENTED = {implemented!r}
REPAIR = {repair!r}

calls = []

def runner(argv, **kwargs):
    calls.append(1)
    source = next((root / ".aflow/runs").iterdir())
    turn = source / "turns" / f"turn-{{len(calls):03d}}"
    prompt = (turn / "effective-prompt.txt").read_text()
    execution = Path(kwargs["cwd"])
    active = Path(prompt.split("Active: ", 1)[1].splitlines()[0])
    repair = Path(prompt.split("Repair: ", 1)[1].splitlines()[0])
    if not active.is_relative_to(execution):
        active = execution / active.relative_to(root)
    if not repair.is_relative_to(execution):
        repair = execution / repair.relative_to(root)
    if len(calls) == 1:
        (execution / plan.name).write_text(IMPLEMENTED)
    elif len(calls) == 2:
        repair.write_text(REPAIR)
    else:
        (root / "repair-ready").write_text("ready")
        time.sleep(300)
    return subprocess.CompletedProcess(argv, 0, "done", "")

run_workflow(
    ControllerConfig(repo_root=root, plan_path=plan, max_turns=12,
                     reserved_run_id=source_id, caller_scope="project:one",
                     idempotency_key="source-key"),
    config, "repair", config_dir=root, snapshot_config=False,
    adapter=CodexAdapter(), runner=runner,
)
'''


@pytest.fixture(params=[False, True], ids=["in-place", "worktree"])
def stopped_repair(tmp_path, request, monkeypatch):
    """Produce a managed-stop killed controller mid-final-checkpoint repair."""
    worktree = request.param
    root = tmp_path / "repo"
    root.mkdir()
    if worktree:
        _make_lifecycle_git_repo(root)
        (root / ".gitignore").write_text(".aflow/\nplan*.md\naflow.toml\nworker.env\nrelease/\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "fixture ignores"], cwd=root, check=True, capture_output=True)
    plan = root / "plan.md"
    plan.write_text(_STOPPED_INITIAL)
    config_path = root / "aflow.toml"
    config_path.write_text("# fixture\n")
    config = _workflow_config(manager_enabled=False, threshold=1, review_checked_original=True)
    config = replace(config, aflow=replace(config.aflow, worktree_root=str(tmp_path / "worktrees")),
                     workflows={"repair": replace(config.workflows["repair"],
                                setup=("worktree", "branch") if worktree else (),
                                main_branch="main" if worktree else None)})
    config = replace(config, prompts={"p": "Active: {ACTIVE_PLAN_PATH}\nRepair: {NEW_PLAN_PATH}"})
    driver = tmp_path / "driver.py"
    driver.write_text(_STOPPED_DRIVER.format(implemented=_STOPPED_IMPLEMENTED, repair=_STOPPED_REPAIR))
    source_id = "stopped-repair-source"
    stderr_path = tmp_path / "driver.stderr"
    env = dict(os.environ, PYTHONPATH=str(_REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.Popen(
        [sys.executable, str(driver), str(root), source_id, "1" if worktree else "0"],
        env=env, cwd=str(root),
        stdout=subprocess.DEVNULL, stderr=stderr_path.open("wb"),
    )
    try:
        deadline = time.monotonic() + 90
        while not (root / "repair-ready").is_file():
            if proc.poll() is not None:
                raise AssertionError(
                    f"source controller exited before the repair turn: "
                    f"{stderr_path.read_text()[-2000:]!r}"
                )
            if time.monotonic() > deadline:
                raise AssertionError("source repair turn never started")
            time.sleep(0.05)
        env_file = root / "worker.env"
        env_file.write_text("AFLOW_FIXTURE=1\n")
        executable = root / "release/bin/aflow"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda _: config)
        units = InMemoryUnitManager()
        daemon = AflowDaemon(DaemonConfig(
            repo_root=root, config_path=config_path, aflow_executable=executable,
            environment_file=env_file, release_identity="fixture", stop_timeout_seconds=0,
        ), units=units)
        daemon.start()
        stopped = daemon.service.owner_stop(source_id, expected_revision=0, caller_scope="project:one")
        assert stopped.status == "owner_stopped"
        proc.kill()
        proc.wait(timeout=60)
        source = root / ".aflow/runs" / source_id
        payload = json.loads((source / "run.json").read_text())
        # The managed stop killed the controller mid-repair turn: the run
        # metadata stays "running", the final-checkpoint snapshot is complete,
        # and the repair receipt is unfinalized "starting" (no fabricated
        # failed/finished receipt).
        assert payload["status"] == "running"
        assert payload["last_snapshot"]["is_complete"] is True
        assert payload["active_implementation_scope"]["checkpoint_index"] == 1
        repair_receipt = json.loads((source / "turns/turn-003/result.json").read_text())
        assert repair_receipt["status"] == "starting"
        assert repair_receipt["snapshot_after"] is None
        # The stop intent is cleared through supported control so the fixture
        # hands tests the admitted shape; pending-stop coverage re-sets it.
        compare_and_swap_overrides(
            root, source_id, RunControlRequest(expected_revision=1, owner_stop=False)
        )
        # A managed-stopped source has portable unit receipts proving the unit
        # was stopped; this is the normal shape after a clean managed stop.
        _write_stopped_unit_receipts(source)
        return (root, plan, config_path, config, source, payload, daemon, units)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=60)


def stopped_bootstrap(case):
    root, plan, config_path, config, source, payload, daemon, units = case
    return _bootstrap_resume_invocation(
        repo_root=root, config_path=config_path, config_path_is_explicit=True,
        workflow_config=config, requested_run_id=source.name, workflow_arg=None,
        plan_file_arg=None, team_arg=None, start_step_arg=None, max_turns_arg=None,
        extra_instructions_arg=(), extra_instructions_provided=False,
        live_loader=lambda _: config,
    )


def _stopped_source_files(case):
    root, plan, config_path, config, source, payload, daemon, units = case
    return {k: v for k, v in files(source).items() if k != "events.jsonl"}


def test_stopped_repair_preserves_pending_repair_scope(stopped_repair):
    root, plan, config_path, config, source, payload, daemon, units = stopped_repair
    before = _stopped_source_files(stopped_repair)
    execution = Path(payload.get("execution_repo_root") or root)
    original = (execution / plan.name).read_bytes()
    context = stopped_bootstrap(stopped_repair).resume_context
    assert context.active_implementation_scope.checkpoint_index == 1
    assert context.review_repair_step == context.interrupted_step_name == "implement"
    assert context.resume_scope_reconciled is False
    assert context.active_plan_path == Path(payload["active_plan_path"])
    assert context.reviewer_rejection_count == 1
    assert len(context.review_rejection_history) == 1
    assert context.pending_step_team_override.selector == "codex.worker-base"
    assert context.implementation_attempts
    assert context.scope_envelope_bytes == (
        source / payload["active_implementation_scope"]["envelope_artifact_path"]
    ).read_bytes()
    assert _stopped_source_files(stopped_repair) == before
    assert (execution / plan.name).read_bytes() == original


def test_stopped_repair_successor_repairs_then_reviews(stopped_repair):
    root, plan, config_path, config, source, payload, daemon, units = stopped_repair
    execution = Path(payload.get("execution_repo_root") or root)
    before = _stopped_source_files(stopped_repair)
    original = (execution / plan.name).read_bytes()
    source_worktree = payload.get("worktree_path")
    context = stopped_bootstrap(stopped_repair).resume_context
    calls = []

    def runner(argv, **kwargs):
        calls.append(kwargs)
        successor = next(p for p in (root / ".aflow/runs").iterdir() if p != source)
        current = json.loads((successor / "run.json").read_text())
        assert current["resumed_from_run_id"] == source.name
        assert current["active_implementation_scope"]["checkpoint_index"] == 1
        assert current["review_rejection_history"] == payload["review_rejection_history"]
        if source_worktree:
            assert current["worktree_path"] == source_worktree
        if len(calls) == 1:
            prompt = (successor / "turns/turn-001/effective-prompt.txt").read_text()
            overlay = Path(prompt.split("Active: ", 1)[1].splitlines()[0])
            assert "worker-base" in argv
            overlay.write_text(overlay.read_text().replace("[ ]", "[x]"))
            (Path(kwargs["cwd"]) / "unfinished.py").write_text("# preserve dirty repair\n")
            return subprocess.CompletedProcess(argv, 0, "repair verified", "")
        assert current["current_step_name"] == "review"
        return subprocess.CompletedProcess(
            argv, 0, "AFLOW_STOP: reviewer reached before scope advancement", ""
        )

    with pytest.raises(WorkflowError, match="reviewer reached") as failure:
        run_workflow(
            ControllerConfig(repo_root=root, plan_path=plan, max_turns=12, keep_runs=1),
            config, "repair", config_dir=root, snapshot_config=False,
            adapter=CodexAdapter(), runner=runner, resume=context,
        )
    assert len(calls) == 2
    assert _stopped_source_files(stopped_repair) == before
    assert (execution / plan.name).read_bytes() == original
    successor_payload = json.loads((failure.value.run_dir / "run.json").read_text())
    assert successor_payload["resumed_from_run_id"] == source.name
    assert (Path(successor_payload.get("execution_repo_root") or root) / "unfinished.py").read_text() == (
        "# preserve dirty repair\n"
    )


def test_stopped_repair_managed_resume_idempotent(stopped_repair, monkeypatch):
    root, plan, config_path, config, source, payload, daemon, units = stopped_repair
    before = _stopped_source_files(stopped_repair)
    assert daemon.service.run_status(source.name).evidence["can_resume"] is True
    assert _stopped_source_files(stopped_repair) == before
    successor = daemon.service.resume(source.name, caller_scope="project:one", idempotency_key="stop-repair-resume")
    replay = daemon.service.resume(source.name, caller_scope="project:one", idempotency_key="stop-repair-resume")
    assert successor.run_id == replay.run_id
    assert len(units.start_calls) == 1
    record = daemon.service._read_record(successor.run_id)
    manifest = daemon.application.repository.get_launch_manifest(successor.run_id)
    prepared, context = _worker_prepared(record, manifest, root, config_path, config)
    assert context.review_repair_step == context.interrupted_step_name == "implement"
    assert context.active_implementation_scope.checkpoint_index == 1
    assert context.pending_step_team_override.selector == "codex.worker-base"
    assert context.reviewer_rejection_count == 1
    assert context.active_plan_path == Path(payload["active_plan_path"])
    assert _stopped_source_files(stopped_repair) == before


def test_stopped_repair_pending_stop_blocks_admission(stopped_repair):
    root, plan, config_path, config, source, payload, daemon, units = stopped_repair
    compare_and_swap_overrides(
        root, source.name, RunControlRequest(expected_revision=2, owner_stop=True)
    )
    assert daemon.service.run_status(source.name).evidence["can_resume"] is False
    with pytest.raises(ValueError):
        stopped_bootstrap(stopped_repair)
    with pytest.raises(Exception):
        daemon.service.resume(source.name, caller_scope="project:one", idempotency_key="blocked")
    assert len(units.start_calls) == 0
    assert list((root / ".aflow/runs").iterdir()) == [source]
    compare_and_swap_overrides(
        root, source.name, RunControlRequest(expected_revision=3, owner_stop=False)
    )
    assert daemon.service.run_status(source.name).evidence["can_resume"] is True


def test_stopped_repair_active_unit_blocks_admission(stopped_repair):
    root, plan, config_path, config, source, payload, daemon, units = stopped_repair
    units.start(_unit_name(source.name), ("fake",), cwd=root)
    assert daemon.service.run_status(source.name).evidence["can_resume"] is False
    with pytest.raises(Exception, match="active workflow unit"):
        daemon.service.resume(source.name, caller_scope="project:one", idempotency_key="blocked")
    assert list((root / ".aflow/runs").iterdir()) == [source]


@pytest.mark.parametrize("damage", [
    "missing_overlay", "external_overlay", "missing_envelope",
    "changed_original", "no_rejection", "finalized_repair",
    "malformed_turn_identity", "extra_turn", "contradictory_status",
])
def test_stopped_repair_rejects_unproven_evidence(stopped_repair, damage):
    root, plan, config_path, config, source, payload, daemon, units = stopped_repair
    execution = Path(payload.get("execution_repo_root") or root)
    review = json.loads((source / "turns/turn-002/result.json").read_text())
    repair = json.loads((source / "turns/turn-003/result.json").read_text())
    if damage == "missing_overlay":
        (execution / Path(payload["active_plan_path"]).relative_to(root)).unlink()
    elif damage == "external_overlay":
        review["new_plan_path"] = str(root / "unrelated.md")
    elif damage == "missing_envelope":
        (source / payload["active_implementation_scope"]["envelope_artifact_path"]).unlink()
    elif damage == "changed_original":
        (execution / plan.name).write_text(_STOPPED_INITIAL)
    elif damage == "no_rejection":
        payload["review_rejection_history"] = []
    elif damage == "finalized_repair":
        repair["status"] = "completed"
    elif damage == "malformed_turn_identity":
        repair["step_name"] = "review"
    elif damage == "extra_turn":
        (source / "turns/turn-004").mkdir()
    elif damage == "contradictory_status":
        payload["status"] = "failed"
    (source / "turns/turn-002/result.json").write_text(json.dumps(review))
    (source / "turns/turn-003/result.json").write_text(json.dumps(repair))
    (source / "run.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        stopped_bootstrap(stopped_repair)
    assert len(units.start_calls) == 0
    assert list((root / ".aflow/runs").iterdir()) == [source]


# Issue #55 defect repairs: stop phase, journaled stop event, and cleared
# intent identify a managed stop but never override current portable worker
# ownership; and a valid stopped first-turn successor must remain repairable
# through its inherited (possibly managed-stopped) receipt owner.


def _unit_receipt(source: Path, name: str, **fields) -> None:
    units = source / "units"
    units.mkdir(exist_ok=True)
    record = {
        "schema": 1,
        "run_id": source.name,
        "nonce": "fixture-nonce",
        **fields,
    }
    (units / name).write_text(json.dumps(record) + "\n", encoding="utf-8")


def _write_stopped_unit_receipts(source: Path) -> None:
    _unit_receipt(source, "start.json", unit=f"aflow-run-{source.name}.service")
    _unit_receipt(
        source,
        "stopped.json",
        result="stopped",
        at=datetime.now(timezone.utc).isoformat(),
    )


def _write_live_unit_receipts(source: Path) -> None:
    _unit_receipt(source, "start.json", unit=f"aflow-run-{source.name}.service")
    pid = os.getpid()
    _unit_receipt(
        source,
        "child.json",
        pid=pid,
        pgid=pid,
        process_birth=process_birth_identity(pid),
    )


def _case_worker_evidence(case):
    root, _, _, _, source, *_ = case
    return worker_evidence(root, source.name, f"aflow-run-{source.name}.service")


def test_stopped_repair_unknown_worker_refuses_before_allocation(stopped_repair):
    # A units directory without trusted receipts is an unknown observation,
    # not inactivity evidence.  The CLI public execution path is gated at
    # bootstrap, so no resume context (and therefore no run_workflow dispatch)
    # can be constructed; managed preview and managed resume refuse as well.
    root, plan, config_path, config, source, payload, daemon, units = stopped_repair
    # Remove the fixture's stopped receipts to simulate lost/untrusted receipts.
    for child in (source / "units").iterdir():
        child.unlink()
    before = _stopped_source_files(stopped_repair)
    assert _case_worker_evidence(stopped_repair) == {
        "active": None,
        "observation": "untrusted_receipts",
    }
    assert daemon.service.run_status(source.name).evidence["can_resume"] is False
    with pytest.raises(ValueError, match="not confirmed inactive"):
        stopped_bootstrap(stopped_repair)
    with pytest.raises(Exception):
        daemon.service.resume(
            source.name, caller_scope="project:one", idempotency_key="unknown-worker"
        )
    assert len(units.start_calls) == 0
    assert list((root / ".aflow/runs").iterdir()) == [source]
    assert _stopped_source_files(stopped_repair) == before


def test_stopped_repair_live_worker_refuses_before_allocation(stopped_repair):
    # Live controller/worker/provider ownership is active ownership and is
    # never admitted from the CLI or the managed path.
    root, plan, config_path, config, source, payload, daemon, units = stopped_repair
    # Replace the fixture's stopped receipts with live ownership evidence.
    for child in (source / "units").iterdir():
        child.unlink()
    _write_live_unit_receipts(source)
    before = _stopped_source_files(stopped_repair)
    assert _case_worker_evidence(stopped_repair)["active"] is True
    assert daemon.service.run_status(source.name).evidence["can_resume"] is False
    with pytest.raises(ValueError, match="not confirmed inactive"):
        stopped_bootstrap(stopped_repair)
    with pytest.raises(Exception):
        daemon.service.resume(
            source.name, caller_scope="project:one", idempotency_key="live-worker"
        )
    assert len(units.start_calls) == 0
    assert list((root / ".aflow/runs").iterdir()) == [source]
    assert _stopped_source_files(stopped_repair) == before


def test_stopped_repair_confirmed_inactive_worker_repairs_then_reviews(stopped_repair):
    # Proven inactive portable ownership keeps the stopped repair admitted:
    # the successor invokes the repair worker first, then the reviewer.
    root, plan, config_path, config, source, payload, daemon, units = stopped_repair
    execution = Path(payload.get("execution_repo_root") or root)
    original = (execution / plan.name).read_bytes()
    _write_stopped_unit_receipts(source)
    before = _stopped_source_files(stopped_repair)
    assert confirmed_inactive(_case_worker_evidence(stopped_repair)) is True
    assert daemon.service.run_status(source.name).evidence["can_resume"] is True
    context = stopped_bootstrap(stopped_repair).resume_context
    assert context.review_repair_step == context.interrupted_step_name == "implement"
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        successor = next(p for p in (root / ".aflow/runs").iterdir() if p != source)
        current = json.loads((successor / "run.json").read_text())
        assert current["resumed_from_run_id"] == source.name
        if len(calls) == 1:
            prompt = (successor / "turns/turn-001/effective-prompt.txt").read_text()
            overlay = Path(prompt.split("Active: ", 1)[1].splitlines()[0])
            assert "worker-base" in argv
            overlay.write_text(overlay.read_text().replace("[ ]", "[x]"))
            return subprocess.CompletedProcess(argv, 0, "repair verified", "")
        assert current["current_step_name"] == "review"
        return subprocess.CompletedProcess(
            argv, 0, "AFLOW_STOP: reviewer reached before scope advancement", ""
        )

    with pytest.raises(WorkflowError, match="reviewer reached"):
        run_workflow(
            ControllerConfig(repo_root=root, plan_path=plan, max_turns=12, keep_runs=2),
            config, "repair", config_dir=root, snapshot_config=False,
            adapter=CodexAdapter(), runner=runner, resume=context,
        )
    assert len(calls) == 2
    assert _stopped_source_files(stopped_repair) == before
    assert (execution / plan.name).read_bytes() == original


_SECOND_STOP_DRIVER = '''
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from aflow.cli import _bootstrap_resume_invocation
from aflow.harnesses.codex import CodexAdapter
from aflow.run_state import ControllerConfig
from aflow.workflow import run_workflow
from tests.test_repair_upgrades import _workflow_config

root = Path(sys.argv[1])
source_id = sys.argv[2]
worktree = sys.argv[3] == "1"
key = sys.argv[4]
marker = sys.argv[5]
config = _workflow_config(manager_enabled=False, threshold=1, review_checked_original=True)
config = replace(config, aflow=replace(config.aflow, worktree_root=str(root.parent / "worktrees")),
                 workflows={"repair": replace(config.workflows["repair"],
                            setup=("worktree", "branch") if worktree else (),
                            main_branch="main" if worktree else None)})
config = replace(config, prompts={"p": "Active: {ACTIVE_PLAN_PATH}\\nRepair: {NEW_PLAN_PATH}"})
context = _bootstrap_resume_invocation(
    repo_root=root, config_path=root / "aflow.toml", config_path_is_explicit=True,
    workflow_config=config, requested_run_id=source_id, workflow_arg=None,
    plan_file_arg=None, team_arg=None, start_step_arg=None, max_turns_arg=None,
    extra_instructions_arg=(), extra_instructions_provided=False,
    live_loader=lambda _: config,
).resume_context

def runner(argv, **kwargs):
    (root / marker).write_text("ready")
    time.sleep(300)
    return subprocess.CompletedProcess(argv, 0, "done", "")

run_workflow(
    ControllerConfig(repo_root=root, plan_path=root / "plan.md", max_turns=12,
                     caller_scope="project:one", idempotency_key=key),
    config, "repair", config_dir=root, snapshot_config=False,
    adapter=CodexAdapter(), runner=runner, resume=context,
)
'''


def _start_stopped_successor_driver(tmp_path, root, run_id, worktree_path, key, marker):
    driver = tmp_path / "stopped-successor-driver.py"
    driver.write_text(_SECOND_STOP_DRIVER, encoding="utf-8")
    stderr_path = tmp_path / f"{marker}.stderr"
    env = dict(
        os.environ,
        PYTHONPATH=str(_REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    )
    proc = subprocess.Popen(
        [sys.executable, str(driver), str(root), run_id,
         "1" if worktree_path else "0", key, marker],
        env=env, cwd=str(root),
        stdout=subprocess.DEVNULL, stderr=stderr_path.open("wb"),
    )
    deadline = time.monotonic() + 60
    while not (root / marker).is_file():
        if proc.poll() is not None:
            proc.kill()
            raise AssertionError(
                "successor controller exited before its repair turn: "
                f"{stderr_path.read_text()[-2000:]!r}"
            )
        if time.monotonic() > deadline:
            proc.kill()
            raise AssertionError("successor repair turn never started")
        time.sleep(0.05)
    return proc


def _stop_stopped_successor(case, proc, exclude, *, complete_snapshot: bool, daemon, units):
    root, plan, config_path, config = case[:4]
    proc.kill()
    proc.wait(timeout=60)
    successor = next(
        p for p in (root / ".aflow/runs").iterdir() if p not in exclude
    )
    daemon.service.owner_stop(successor.name, expected_revision=0, caller_scope="project:one")
    compare_and_swap_overrides(
        root, successor.name, RunControlRequest(expected_revision=1, owner_stop=False)
    )
    # A managed stop produces portable unit receipts for the stopped run.
    _write_stopped_unit_receipts(successor)
    next_payload = json.loads((successor / "run.json").read_text())
    assert next_payload["status"] == "running"
    assert next_payload["turns_completed"] == 0
    assert next_payload["active_turn"] == 1
    assert next_payload["last_snapshot"]["is_complete"] is complete_snapshot
    return (root, plan, config_path, config, successor, next_payload, daemon, units)


def test_second_stopped_repair_successor_repairs_then_reviews(stopped_repair, tmp_path):
    # A valid stopped first-turn successor (zero completed turns, starting
    # turn 1, inherited rejection) must resume through its managed-stopped
    # receipt owner, repair, then review, with lineage, overlay, worktree and
    # dirty edits preserved.
    root, plan, config_path, config, source, payload, daemon, units = stopped_repair
    execution = Path(payload.get("execution_repo_root") or root)
    worktree_path = payload.get("worktree_path")
    source_before = _stopped_source_files(stopped_repair)
    original = (execution / plan.name).read_bytes()
    proc = _start_stopped_successor_driver(
        tmp_path, root, source.name, worktree_path, "successor-key", "successor-ready"
    )
    try:
        second_case = _stop_stopped_successor(
            stopped_repair, proc, exclude=(source,), complete_snapshot=True,
            daemon=daemon, units=units,
        )
        successor = second_case[4]
        successor_before = _stopped_source_files(second_case)
        assert daemon.service.run_status(successor.name).evidence["can_resume"] is True
        context = stopped_bootstrap(second_case).resume_context
        assert context.active_implementation_scope.checkpoint_index == 1
        assert context.review_repair_step == context.interrupted_step_name == "implement"
        assert context.reviewer_rejection_count == 1
        assert len(context.review_rejection_history) == 1
        # The inherited rejection keeps its original receipt owner.
        assert context.review_rejection_history[0].source_run_id == source.name
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            third = next(
                p for p in (root / ".aflow/runs").iterdir() if p not in (source, successor)
            )
            current = json.loads((third / "run.json").read_text())
            assert current["resumed_from_run_id"] == successor.name
            assert current["review_rejection_history"] == payload["review_rejection_history"]
            if len(calls) == 1:
                prompt = (third / "turns/turn-001/effective-prompt.txt").read_text()
                overlay = Path(prompt.split("Active: ", 1)[1].splitlines()[0])
                assert "worker-base" in argv
                overlay.write_text(overlay.read_text().replace("[ ]", "[x]"))
                (Path(kwargs["cwd"]) / "dirty-repair.py").write_text("# preserve dirty repair\n")
                return subprocess.CompletedProcess(argv, 0, "repair verified", "")
            assert current["current_step_name"] == "review"
            return subprocess.CompletedProcess(
                argv, 0, "AFLOW_STOP: reviewer reached before scope advancement", ""
            )

        with pytest.raises(WorkflowError, match="reviewer reached") as failure:
            run_workflow(
                ControllerConfig(repo_root=root, plan_path=plan, max_turns=12, keep_runs=3),
                config, "repair", config_dir=root, snapshot_config=False,
                adapter=CodexAdapter(), runner=runner, resume=context,
            )
        assert len(calls) == 2
        third_payload = json.loads((failure.value.run_dir / "run.json").read_text())
        assert third_payload["resumed_from_run_id"] == successor.name
        if worktree_path:
            assert third_payload["worktree_path"] == worktree_path
        assert (
            Path(third_payload.get("execution_repo_root") or root) / "dirty-repair.py"
        ).read_text() == "# preserve dirty repair\n"
        assert _stopped_source_files(stopped_repair) == source_before
        assert _stopped_source_files(second_case) == successor_before
        assert (execution / plan.name).read_bytes() == original
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_mixed_stopped_failed_lineage_and_unproven_stopped_ancestor(checkpoint_repair, tmp_path, monkeypatch):
    # A stopped current source whose intermediate ancestor is managed-stopped
    # and whose inherited receipt owner is a failed source (issue 74 shape)
    # keeps the mixed valid lineage admitted; removing the stopped ancestor's
    # managed-stop evidence must refuse it.
    root, plan, config_path, config, source, payload = checkpoint_repair
    worktree_path = payload.get("worktree_path")
    env_file = root / "worker.env"
    env_file.write_text("AFLOW_FIXTURE=1\n")
    executable = root / "release/bin/aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda _: config)
    units = InMemoryUnitManager()
    daemon = AflowDaemon(DaemonConfig(
        repo_root=root, config_path=config_path, aflow_executable=executable,
        environment_file=env_file, release_identity="fixture", stop_timeout_seconds=0,
    ), units=units)
    daemon.start()
    procs = []
    try:
        first = _start_stopped_successor_driver(
            tmp_path, root, source.name, worktree_path, "mixed-key-1", "mixed-ready-1"
        )
        procs.append(first)
        first_case = _stop_stopped_successor(
            checkpoint_repair, first, exclude=(source,), complete_snapshot=False,
            daemon=daemon, units=units,
        )
        stopped_ancestor = first_case[4]
        second = _start_stopped_successor_driver(
            tmp_path, root, stopped_ancestor.name, worktree_path, "mixed-key-2", "mixed-ready-2"
        )
        procs.append(second)
        second_case = _stop_stopped_successor(
            first_case, second, exclude=(source, stopped_ancestor),
            complete_snapshot=False, daemon=daemon, units=units,
        )
        # Mixed valid lineage: stopped current source, managed-stopped
        # intermediate ancestor, failed receipt owner under the issue 74
        # contract.
        context = stopped_bootstrap(second_case).resume_context
        assert context.review_repair_step == context.interrupted_step_name == "implement"
        assert context.active_implementation_scope.checkpoint_index == 1
        assert context.reviewer_rejection_count == 2
        assert context.review_rejection_history[0].source_run_id == source.name
        # An unproven stopped ancestor is never converted into failed
        # evidence and must refuse before any allocation.
        (root / ".aflow" / "launches" / f"{stopped_ancestor.name}.state.json").unlink()
        with pytest.raises(ValueError):
            stopped_bootstrap(second_case)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def test_stopped_repair_no_receipts_cli_refuses_actionably(stopped_repair):
    # A managed-stopped source with no portable receipts at all (no units
    # directory) must refuse from the CLI with an actionable message directing
    # the caller to managed resume.  No successor is allocated.
    root, plan, config_path, config, source, payload, daemon, units = stopped_repair
    # Remove the units directory entirely (no portable receipts).
    import shutil
    shutil.rmtree(source / "units")
    before = _stopped_source_files(stopped_repair)
    assert worker_evidence(root, source.name, f"aflow-run-{source.name}.service") is None
    with pytest.raises(ValueError, match="no portable worker receipts"):
        stopped_bootstrap(stopped_repair)
    assert len(units.start_calls) == 0
    assert list((root / ".aflow/runs").iterdir()) == [source]
    assert _stopped_source_files(stopped_repair) == before


def test_stopped_repair_no_receipts_active_unit_both_refuse(stopped_repair):
    # No portable receipts plus an active managed unit: both managed
    # preview/resume and CLI/public execution refuse with zero successors
    # and zero dispatch.
    root, plan, config_path, config, source, payload, daemon, units = stopped_repair
    import shutil
    shutil.rmtree(source / "units")
    assert worker_evidence(root, source.name, f"aflow-run-{source.name}.service") is None
    # Activate the managed unit.
    units.start(_unit_name(source.name), ("fake",), cwd=root)
    assert units.get(_unit_name(source.name)).is_active
    before = _stopped_source_files(stopped_repair)
    # Managed preview refuses.
    assert daemon.service.run_status(source.name).evidence["can_resume"] is False
    # Managed resume refuses.
    with pytest.raises(Exception, match="active workflow unit"):
        daemon.service.resume(
            source.name, caller_scope="project:one", idempotency_key="v02-no-receipts-active"
        )
    # CLI refuses.
    with pytest.raises(ValueError, match="no portable worker receipts"):
        stopped_bootstrap(stopped_repair)
    assert len(units.start_calls) == 1  # only the one we started above
    assert list((root / ".aflow/runs").iterdir()) == [source]
    assert _stopped_source_files(stopped_repair) == before


def test_stopped_ancestor_no_portable_receipts_refuses(stopped_repair, tmp_path):
    # A stopped successor with confirmed-inactive current receipts and an
    # unknown stopped ancestor (no portable receipts) must refuse all
    # admission paths before allocation.  The valid current successor cannot
    # launder an ancestor's unknown ownership.
    root, _, _, _, source, payload, daemon, units = stopped_repair
    worktree_path = payload.get("worktree_path")
    proc = _start_stopped_successor_driver(
        tmp_path, root, source.name, worktree_path, "ancestor-noreceipt-key", "ancestor-noreceipt-ready"
    )
    try:
        second_case = _stop_stopped_successor(
            stopped_repair, proc, exclude=(source,), complete_snapshot=True,
            daemon=daemon, units=units,
        )
        successor = second_case[4]
        # The successor has confirmed-inactive receipts (written by
        # _stop_stopped_successor).  Now remove the SOURCE's (ancestor's)
        # portable receipts to simulate unknown ownership.
        import shutil
        shutil.rmtree(source / "units")
        assert worker_evidence(root, source.name, f"aflow-run-{source.name}.service") is None
        # The successor's own receipts are still valid.
        assert confirmed_inactive(_case_worker_evidence(second_case)) is True
        source_before = _stopped_source_files(stopped_repair)
        successor_before = _stopped_source_files(second_case)
        # CLI reconstruction refuses because the ancestor's ownership is
        # unknown (no portable receipts) during the lineage walk.
        with pytest.raises(ValueError):
            stopped_bootstrap(second_case)
        # No new run was allocated.
        assert set(p.name for p in (root / ".aflow" / "runs").iterdir()) == {source.name, successor.name}
        assert _stopped_source_files(stopped_repair) == source_before
        assert _stopped_source_files(second_case) == successor_before
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_stopped_ancestor_untrusted_receipts_refuses(stopped_repair, tmp_path):
    # A stopped successor with confirmed-inactive current receipts and a
    # stopped ancestor whose receipts are untrusted (units dir exists but is
    # empty) must refuse all admission paths before allocation.
    root, _, _, _, source, payload, daemon, units = stopped_repair
    worktree_path = payload.get("worktree_path")
    proc = _start_stopped_successor_driver(
        tmp_path, root, source.name, worktree_path, "ancestor-untrusted-key", "ancestor-untrusted-ready"
    )
    try:
        second_case = _stop_stopped_successor(
            stopped_repair, proc, exclude=(source,), complete_snapshot=True,
            daemon=daemon, units=units,
        )
        successor = second_case[4]
        # Clear the source's (ancestor's) receipts to make them untrusted.
        for child in (source / "units").iterdir():
            child.unlink()
        assert worker_evidence(root, source.name, f"aflow-run-{source.name}.service") == {
            "active": None, "observation": "untrusted_receipts",
        }
        # The successor's own receipts are still valid.
        assert confirmed_inactive(_case_worker_evidence(second_case)) is True
        source_before = _stopped_source_files(stopped_repair)
        successor_before = _stopped_source_files(second_case)
        # CLI reconstruction refuses because the ancestor's ownership is
        # unknown (untrusted receipts) during the lineage walk.
        with pytest.raises(ValueError):
            stopped_bootstrap(second_case)
        # No new run was allocated.
        assert set(p.name for p in (root / ".aflow" / "runs").iterdir()) == {source.name, successor.name}
        assert _stopped_source_files(stopped_repair) == source_before
        assert _stopped_source_files(second_case) == successor_before
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# Issue #55 cp01-v03: the saved boolean ownership exemption is replaced by a
# fresh identity-bound managed inactivity query, applied at preview,
# reservation, and worker boot for the exact source and every managed-stopped
# ancestor.  A consumed reservation nonce never proves inactivity.


def _worker_boot_env(monkeypatch, daemon, successor_id, config):
    """Point a real worker_main() boot at the fixture daemon and config."""
    state = json.loads(daemon.service._admission.state_path.read_text())
    nonce = state["reservations"][successor_id]["nonce"]
    monkeypatch.setenv("AFLOW_ADMISSION_RESERVATION_NONCE", nonce)
    monkeypatch.delenv("AFLOW_WORKER_NONCE", raising=False)
    monkeypatch.setattr("aflow.daemon.compose_control_plane",
                        lambda *args, **kwargs: daemon.application)
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda _: config)
    # The public runner re-reads the live config through its own default
    # loader; the fixture config must win there as well.
    monkeypatch.setattr("aflow.live_config.load_workflow_config", lambda _: config)
    return nonce


def _dispatching_execute(monkeypatch, runner):
    def execute(prepared, **kwargs):
        return _real_execute_workflow(prepared, adapter=CodexAdapter(), runner=runner, **kwargs)
    monkeypatch.setattr("aflow.daemon.execute_workflow", execute)


def test_managed_nonportable_source_repairs_then_reviews_at_worker_boot(stopped_repair, monkeypatch):
    # A managed-stopped source without any portable receipts (nonportable)
    # is admitted by fresh managed inactivity proof, and a real nonce-bearing
    # worker_main() boot repairs the interrupted checkpoint, then reviews,
    # preserving source evidence, original plan, scope, rejections, lineage
    # and worktree.
    root, plan, config_path, config, source, payload, daemon, units = stopped_repair
    shutil.rmtree(source / "units")
    assert worker_evidence(root, source.name, _unit_name(source.name)) is None
    assert units.get(_unit_name(source.name)) is None
    assert daemon.service.run_status(source.name).evidence["can_resume"] is True
    result = daemon.service.resume(source.name, caller_scope="project:one", idempotency_key="nonportable-boot")
    replay = daemon.service.resume(source.name, caller_scope="project:one", idempotency_key="nonportable-boot")
    assert replay.run_id == result.run_id
    assert len(units.start_calls) == 1
    record = daemon.service._read_record(result.run_id)
    manifest = daemon.application.repository.get_launch_manifest(result.run_id)
    # The fresh query admits the exact nonportable source at worker boot.
    prepared, context = _worker_prepared(
        record, manifest, root, config_path, config,
        managed_inactivity_check=daemon.service._managed_inactivity_check(),
    )
    assert context.review_repair_step == context.interrupted_step_name == "implement"
    assert context.active_implementation_scope.checkpoint_index == 1
    assert context.reviewer_rejection_count == 1
    assert context.active_plan_path == Path(payload["active_plan_path"])
    before = files(source)
    original = (Path(payload.get("execution_repo_root") or root) / plan.name).read_bytes()
    worktree_path = payload.get("worktree_path")
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        successor = root / ".aflow" / "runs" / result.run_id
        current = json.loads((successor / "run.json").read_text())
        assert current["resumed_from_run_id"] == source.name
        assert current["review_rejection_history"] == payload["review_rejection_history"]
        if worktree_path:
            assert current["worktree_path"] == worktree_path
        if len(calls) == 1:
            assert current["current_step_name"] == "implement"
            assert current["active_implementation_scope"]["checkpoint_index"] == 1
            prompt = (successor / "turns/turn-001/effective-prompt.txt").read_text()
            overlay = Path(prompt.split("Active: ", 1)[1].splitlines()[0])
            assert "worker-base" in argv
            overlay.write_text(overlay.read_text().replace("[ ]", "[x]"))
            (Path(kwargs["cwd"]) / "nonportable-dirty.py").write_text("# preserve dirty repair\n")
            return subprocess.CompletedProcess(argv, 0, "repair verified", "")
        assert current["current_step_name"] == "review"
        return subprocess.CompletedProcess(
            argv, 0, "AFLOW_STOP: nonportable source review reached", ""
        )

    _worker_boot_env(monkeypatch, daemon, result.run_id, config)
    _dispatching_execute(monkeypatch, runner)
    # worker_main() catches the reviewer stop and returns 1 by contract.
    assert worker_main(repo_root=root, config_path=config_path, run_id=result.run_id) == 1
    assert len(calls) == 2
    assert files(source) == before
    assert (Path(payload.get("execution_repo_root") or root) / plan.name).read_bytes() == original
    successor_payload = json.loads((root / ".aflow" / "runs" / result.run_id / "run.json").read_text())
    assert successor_payload["resumed_from_run_id"] == source.name
    assert successor_payload["active_implementation_scope"]["checkpoint_index"] == 1
    assert successor_payload["review_rejection_history"] == payload["review_rejection_history"]
    if worktree_path:
        assert successor_payload["worktree_path"] == worktree_path
    assert (
        Path(successor_payload.get("execution_repo_root") or root) / "nonportable-dirty.py"
    ).read_text() == "# preserve dirty repair\n"


def test_active_source_at_worker_boot_refuses_before_dispatch(stopped_repair, monkeypatch):
    # The exact source unit becomes active after a successful reservation and
    # before the daemon worker boots.  The parent nonce alone must not admit
    # it: the fresh query refuses, worker_main() returns 1, and no provider
    # is dispatched.
    root, plan, config_path, config, source, payload, daemon, units = stopped_repair
    shutil.rmtree(source / "units")
    result = daemon.service.resume(source.name, caller_scope="project:one", idempotency_key="boot-active")
    units.start(_unit_name(source.name), ("fake",), cwd=root)
    assert units.get(_unit_name(source.name)).is_active
    assert daemon.service.run_status(source.name).evidence["can_resume"] is False
    assert daemon.service._admission.predecessor_inactive_for_preview(source.name) is False
    before = files(source)
    runs_before = {p.name for p in (root / ".aflow" / "runs").iterdir()}
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "must never dispatch", "")

    _worker_boot_env(monkeypatch, daemon, result.run_id, config)
    _dispatching_execute(monkeypatch, runner)
    assert worker_main(repo_root=root, config_path=config_path, run_id=result.run_id) == 1
    assert len(calls) == 0
    assert files(source) == before
    assert {p.name for p in (root / ".aflow" / "runs").iterdir()} == runs_before


def test_managed_nonportable_ancestor_successor_repairs_then_reviews(stopped_repair, tmp_path, monkeypatch):
    # A stopped successor with its own inactive portable receipts and a
    # nonportable managed-stopped ancestor is admitted by the managed path
    # (fresh query proves the ancestor inactive) and repairs then reviews at
    # worker boot, while the CLI without managed authority refuses the same
    # evidence.
    root, plan, config_path, config, source, payload, daemon, units = stopped_repair
    worktree_path = payload.get("worktree_path")
    proc = _start_stopped_successor_driver(
        tmp_path, root, source.name, worktree_path,
        "nonportable-ancestor-key", "nonportable-ancestor-ready",
    )
    try:
        second_case = _stop_stopped_successor(
            stopped_repair, proc, exclude=(source,), complete_snapshot=True,
            daemon=daemon, units=units,
        )
        successor = second_case[4]
        shutil.rmtree(source / "units")
        assert worker_evidence(root, source.name, _unit_name(source.name)) is None
        assert confirmed_inactive(worker_evidence(root, successor.name, _unit_name(successor.name))) is True
        # The CLI without managed authority refuses the same evidence.
        with pytest.raises(ValueError):
            stopped_bootstrap(second_case)
        assert set(p.name for p in (root / ".aflow" / "runs").iterdir()) == {source.name, successor.name}
        assert daemon.service.run_status(successor.name).evidence["can_resume"] is True
        result = daemon.service.resume(successor.name, caller_scope="project:one", idempotency_key="nonportable-ancestor")
        assert len(units.start_calls) == 1
        record = daemon.service._read_record(result.run_id)
        manifest = daemon.application.repository.get_launch_manifest(result.run_id)
        before_successor = _stopped_source_files(second_case)
        before_source = files(source)
        original = (Path(payload.get("execution_repo_root") or root) / plan.name).read_bytes()
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            third = root / ".aflow" / "runs" / result.run_id
            current = json.loads((third / "run.json").read_text())
            assert current["resumed_from_run_id"] == successor.name
            assert current["review_rejection_history"] == payload["review_rejection_history"]
            assert current["active_implementation_scope"]["checkpoint_index"] == 1
            if worktree_path:
                assert current["worktree_path"] == worktree_path
            if len(calls) == 1:
                assert current["current_step_name"] == "implement"
                prompt = (third / "turns/turn-001/effective-prompt.txt").read_text()
                overlay = Path(prompt.split("Active: ", 1)[1].splitlines()[0])
                assert "worker-base" in argv
                overlay.write_text(overlay.read_text().replace("[ ]", "[x]"))
                (Path(kwargs["cwd"]) / "ancestor-dirty.py").write_text("# preserve dirty repair\n")
                return subprocess.CompletedProcess(argv, 0, "repair verified", "")
            assert current["current_step_name"] == "review"
            return subprocess.CompletedProcess(
                argv, 0, "AFLOW_STOP: nonportable ancestor review reached", ""
            )

        _worker_boot_env(monkeypatch, daemon, result.run_id, config)
        _dispatching_execute(monkeypatch, runner)
        assert worker_main(repo_root=root, config_path=config_path, run_id=result.run_id) == 1
        assert len(calls) == 2
        assert files(source) == before_source
        assert {k: v for k, v in files(successor).items() if k != "events.jsonl"} == {
            k: v for k, v in before_successor.items() if k != "events.jsonl"
        }
        assert (Path(payload.get("execution_repo_root") or root) / plan.name).read_bytes() == original
        third_payload = json.loads((root / ".aflow" / "runs" / result.run_id / "run.json").read_text())
        assert third_payload["resumed_from_run_id"] == successor.name
        assert third_payload["active_implementation_scope"]["checkpoint_index"] == 1
        assert third_payload["review_rejection_history"] == payload["review_rejection_history"]
        if worktree_path:
            assert third_payload["worktree_path"] == worktree_path
        assert (
            Path(third_payload.get("execution_repo_root") or root) / "ancestor-dirty.py"
        ).read_text() == "# preserve dirty repair\n"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


@pytest.mark.parametrize("damage", [
    "active", "foreign", "untrusted_receipts", "unknown", "transitional",
])
def test_nonportable_ancestor_ownership_change_refuses_before_allocation(
    stopped_repair, tmp_path, monkeypatch, damage
):
    # The same valid successor with a nonportable ancestor refuses managed
    # preview/resume before any allocation when the ancestor is freshly
    # active, foreign, carries present untrusted receipts, or its exact unit
    # is observed as unknown or transitional (possibly with a live MainPID).
    root, _, _, _, source, payload, daemon, units = stopped_repair
    worktree_path = payload.get("worktree_path")
    proc = _start_stopped_successor_driver(
        tmp_path, root, source.name, worktree_path,
        f"ancestor-{damage}-key", f"ancestor-{damage}-ready",
    )
    try:
        second_case = _stop_stopped_successor(
            stopped_repair, proc, exclude=(source,), complete_snapshot=True,
            daemon=daemon, units=units,
        )
        successor = second_case[4]
        if damage == "untrusted_receipts":
            for child in (source / "units").iterdir():
                child.unlink()
            assert worker_evidence(root, source.name, _unit_name(source.name)) == {
                "active": None, "observation": "untrusted_receipts",
            }
        else:
            shutil.rmtree(source / "units")
            assert worker_evidence(root, source.name, _unit_name(source.name)) is None
        if damage in {"unknown", "transitional"}:
            state = "unknown" if damage == "unknown" else "activating"
            units.units[_unit_name(source.name)] = UnitState(
                name=_unit_name(source.name), active_state=state,
                sub_state="start" if state == "activating" else "unknown",
                main_pid=os.getpid() if state == "activating" else None,
            )
        if damage == "active":
            units.start(_unit_name(source.name), ("fake",), cwd=root)
        elif damage == "foreign":
            class _ForeignUnits(InMemoryUnitManager):
                def get(self, name):
                    if name == _unit_name(source.name):
                        return UnitState(
                            name="aflow-run-foreign.service",
                            active_state="inactive", sub_state="dead",
                        )
                    return super().get(name)

            daemon.service._application = replace(
                daemon.service._application,
                units=_ForeignUnits(units=dict(units.units)),
            )
        source_before = files(source)
        successor_before = _stopped_source_files(second_case)
        runs_before = {p.name for p in (root / ".aflow" / "runs").iterdir()}
        starts_before = len(units.start_calls)
        if damage in {"active", "untrusted_receipts", "unknown", "transitional"}:
            assert daemon.service.run_status(successor.name).evidence["can_resume"] is False
        with pytest.raises(Exception):
            daemon.service.resume(
                successor.name, caller_scope="project:one",
                idempotency_key=f"ancestor-{damage}",
            )
        # The refusal happens before any successor unit start.
        assert len(units.start_calls) == starts_before
        assert {p.name for p in (root / ".aflow" / "runs").iterdir()} == runs_before
        assert files(source) == source_before
        assert _stopped_source_files(second_case) == successor_before
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# Issue #55 cp01-v04: a fresh managed inactivity answer requires an
# affirmative terminal observation.  A present exact unit that is unknown or
# transitional (possibly with a live MainPID) is not an inactivity answer and
# blocks preview, reservation, and nonce-bearing worker boot for the exact
# source and every managed-stopped ancestor.


@pytest.mark.parametrize("state", ["unknown", "activating", "deactivating"])
def test_noninactive_source_unit_refuses_before_allocation(stopped_repair, state):
    # A present exact source unit that does not affirm a terminal inactive
    # state blocks managed preview and reservation before any successor
    # allocation, in both in-place and worktree execution modes.
    root, _, _, _, source, payload, daemon, units = stopped_repair
    shutil.rmtree(source / "units")
    unit_name = _unit_name(source.name)
    units.units[unit_name] = UnitState(
        name=unit_name, active_state=state,
        sub_state="start" if state == "activating" else "unknown",
        main_pid=os.getpid() if state in {"activating", "deactivating"} else None,
    )
    before = files(source)
    runs_before = {p.name for p in (root / ".aflow" / "runs").iterdir()}
    assert daemon.service.run_status(source.name).evidence["can_resume"] is False
    with pytest.raises(Exception):
        daemon.service.resume(
            source.name, caller_scope="project:one", idempotency_key=f"v04-{state}",
        )
    assert len(units.start_calls) == 0
    assert {p.name for p in (root / ".aflow" / "runs").iterdir()} == runs_before
    assert files(source) == before


@pytest.mark.parametrize("state", ["unknown", "activating", "deactivating"])
def test_noninactive_source_at_worker_boot_refuses_before_dispatch(
    stopped_repair, monkeypatch, state
):
    # After a successful valid reservation, a nonterminal or unknown exact
    # source unit must block the nonce-bearing worker boot: worker_main()
    # returns 1 and no provider is dispatched.
    root, plan, config_path, config, source, payload, daemon, units = stopped_repair
    shutil.rmtree(source / "units")
    result = daemon.service.resume(
        source.name, caller_scope="project:one",
        idempotency_key=f"boot-v04-{state}",
    )
    assert len(units.start_calls) == 1
    unit_name = _unit_name(source.name)
    units.units[unit_name] = UnitState(
        name=unit_name, active_state=state,
        sub_state="start" if state == "activating" else "unknown",
        main_pid=os.getpid() if state in {"activating", "deactivating"} else None,
    )
    assert daemon.service.run_status(source.name).evidence["can_resume"] is False
    before = files(source)
    runs_before = {p.name for p in (root / ".aflow" / "runs").iterdir()}
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "must never dispatch", "")

    _worker_boot_env(monkeypatch, daemon, result.run_id, config)
    _dispatching_execute(monkeypatch, runner)
    assert worker_main(repo_root=root, config_path=config_path, run_id=result.run_id) == 1
    assert len(calls) == 0
    assert files(source) == before
    assert {p.name for p in (root / ".aflow" / "runs").iterdir()} == runs_before


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_terminal_source_unit_under_managed_authority_admits(stopped_repair, state):
    # A present exact source unit affirming a terminal inactive state keeps a
    # managed-stopped nonportable source admitted by the fresh managed query;
    # the reservation allocates exactly one successor.
    root, _, _, _, source, payload, daemon, units = stopped_repair
    shutil.rmtree(source / "units")
    unit_name = _unit_name(source.name)
    units.units[unit_name] = UnitState(
        name=unit_name, active_state=state, sub_state="dead",
    )
    assert daemon.service.run_status(source.name).evidence["can_resume"] is True
    result = daemon.service.resume(
        source.name, caller_scope="project:one",
        idempotency_key=f"v04-terminal-{state}",
    )
    assert result.run_id not in {source.name}
    assert len(units.start_calls) == 1


@pytest.mark.parametrize("state", ["unknown", "activating"])
def test_noninactive_ancestor_at_worker_boot_refuses_before_dispatch(
    stopped_repair, tmp_path, monkeypatch, state
):
    # A valid stopped successor is reserved against a nonportable
    # managed-stopped ancestor; if the ancestor's exact unit is later
    # observed as unknown or transitional, the nonce-bearing worker boot
    # dispatches zero providers and preserves both records.
    root, _, config_path, config, source, payload, daemon, units = stopped_repair
    worktree_path = payload.get("worktree_path")
    proc = _start_stopped_successor_driver(
        tmp_path, root, source.name, worktree_path,
        f"ancestor-boot-{state}-key", f"ancestor-boot-{state}-ready",
    )
    try:
        second_case = _stop_stopped_successor(
            stopped_repair, proc, exclude=(source,), complete_snapshot=True,
            daemon=daemon, units=units,
        )
        successor = second_case[4]
        shutil.rmtree(source / "units")
        assert daemon.service.run_status(successor.name).evidence["can_resume"] is True
        result = daemon.service.resume(
            successor.name, caller_scope="project:one",
            idempotency_key=f"ancestor-boot-{state}",
        )
        assert len(units.start_calls) == 1
        # The supported resume action journaled resume_requested; capture the
        # source/ancestor and successor records after that action for the
        # read-only worker boot comparison.
        before_source = files(source)
        before_successor = {k: v for k, v in files(successor).items() if k != "events.jsonl"}
        units.units[_unit_name(source.name)] = UnitState(
            name=_unit_name(source.name), active_state=state,
            sub_state="start" if state == "activating" else "unknown",
            main_pid=os.getpid() if state == "activating" else None,
        )
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "must never dispatch", "")

        _worker_boot_env(monkeypatch, daemon, result.run_id, config)
        _dispatching_execute(monkeypatch, runner)
        assert worker_main(repo_root=root, config_path=config_path, run_id=result.run_id) == 1
        assert len(calls) == 0
        assert files(source) == before_source
        assert {k: v for k, v in files(successor).items() if k != "events.jsonl"} == before_successor
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
