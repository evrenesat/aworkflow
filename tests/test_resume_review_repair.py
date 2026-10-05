from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aflow.cli import _bootstrap_resume_invocation
from aflow.config import (
    AflowSection,
    GoTransition,
    HarnessProfileConfig,
    WorkflowConfig,
    WorkflowHarnessConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
)
from aflow.run_state import ControllerConfig
from aflow.workflow import run_workflow
from aflow.control_plane import (
    InMemoryUnitManager,
    write_launch_phase,
)
from aflow.daemon import AflowDaemon, DaemonConfig, _worker_prepared
from tests._support import _make_lifecycle_git_repo


@pytest.fixture(params=[False, True], ids=["in-place", "worktree"])
def interrupted_repair(tmp_path, request):
    root = tmp_path / "repo"
    root.mkdir()
    if request.param:
        _make_lifecycle_git_repo(root)
        (root / ".gitignore").write_text(
            ".aflow/\nplan*.md\naflow.toml\nworker.env\nrelease/\n"
        )
        subprocess.run(
            ["git", "add", ".gitignore"], cwd=root, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "fixture ignores"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    plan = root / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: Work\n- [ ] implement\n")
    config_path = root / "aflow.toml"
    config_path.write_text("# fixture\n")
    steps = {
        "implement": WorkflowStepConfig(
            role="senior_architect",
            prompts=("p",),
            go=(GoTransition(to="final_review"),),
        ),
        "final_review": WorkflowStepConfig(
            role="architect",
            prompts=("p",),
            go=(
                GoTransition(to="followup", when="NEW_PLAN_EXISTS"),
                GoTransition(to="END"),
            ),
        ),
        "followup": WorkflowStepConfig(
            role="senior_architect",
            prompts=("p",),
            go=(GoTransition(to="final_review", preserve_active_plan=True),),
        ),
    }
    config = WorkflowUserConfig(
        aflow=AflowSection(max_turns=8, worktree_root=str(tmp_path / "worktrees")),
        roles={"senior_architect": "codex.fixture", "architect": "codex.fixture"},
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={"fixture": HarnessProfileConfig(model="fixture")}
            )
        },
        workflows={
            "repair": WorkflowConfig(
                steps=steps,
                first_step="implement",
                setup=("worktree", "branch") if request.param else (),
                main_branch="main" if request.param else None,
            )
        },
        prompts={
            "p": "Work on {ACTIVE_PLAN_PATH}. Create review repairs at {NEW_PLAN_PATH}."
        },
    )
    calls = []

    def worker(argv, **kwargs):
        calls.append(kwargs)
        source = next((root / ".aflow/runs").iterdir())
        turn = source / "turns" / f"turn-{len(calls):03d}"
        execution = Path(
            json.loads((source / "run.json").read_text()).get("execution_repo_root")
            or root
        )
        if len(calls) == 1:
            owned_plan = execution / plan.name
            owned_plan.write_text(owned_plan.read_text().replace("[ ]", "[x]"))
        elif len(calls) == 2:
            # Follow the actual prompt rather than fabricate run-state fields.
            prompt = (turn / "effective-prompt.txt").read_text()
            overlay = Path(
                prompt.split("Create review repairs at ", 1)[1].split(".md", 1)[0]
                + ".md"
            )
            overlay.write_text("# Repair\n\n- [ ] fix independent signing\n")
        else:
            (execution / "unfinished.py").write_text("# preserved interrupted repair\n")
            raise RuntimeError("fixture worker interruption")
        return subprocess.CompletedProcess(argv, 0, "done", "")

    with pytest.raises(RuntimeError, match="fixture worker interruption"):
        run_workflow(
            ControllerConfig(
                repo_root=root,
                plan_path=plan,
                max_turns=8,
                reserved_run_id="review-repair-source",
                idempotency_key="source-key",
                caller_scope="project:one",
            ),
            config,
            "repair",
            config_dir=root,
            snapshot_config=False,
            runner=worker,
        )
    source = next((root / ".aflow/runs").iterdir())
    payload = json.loads((source / "run.json").read_text())
    return root, plan, config_path, config, source, payload


def bootstrap(case):
    root, plan, config_path, config, source, payload = case
    return _bootstrap_resume_invocation(
        repo_root=root,
        config_path=config_path,
        config_path_is_explicit=True,
        workflow_config=config,
        requested_run_id=source.name,
        workflow_arg=None,
        plan_file_arg=None,
        team_arg=None,
        start_step_arg=None,
        max_turns_arg=None,
        extra_instructions_arg=(),
        extra_instructions_provided=False,
        live_loader=lambda _: config,
    )


def test_resume_runs_repair_then_final_review_and_preserves_source(interrupted_repair):
    root, plan, config_path, config, source, payload = interrupted_repair
    before = {str(p): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    resumed = bootstrap(interrupted_repair)
    assert resumed.resume_context.interrupted_step_name == "followup"
    assert resumed.resume_context.active_plan_path == Path(payload["active_plan_path"])
    execution = Path(payload.get("execution_repo_root") or root)
    assert (
        execution / "unfinished.py"
    ).read_text() == "# preserved interrupted repair\n"
    calls = []

    def worker(argv, **kwargs):
        calls.append(kwargs)
        successor = next(p for p in (root / ".aflow/runs").iterdir() if p != source)
        result = json.loads(
            (successor / "turns" / f"turn-{len(calls):03d}" / "result.json").read_text()
        )
        if len(calls) == 1:
            assert result["step_name"] == "followup"
            (execution / Path(result["active_plan_path"]).relative_to(root)).write_text(
                "# Repair\n\n- [x] fix independent signing\n"
            )
        else:
            assert result["step_name"] == "final_review"
        return subprocess.CompletedProcess(argv, 0, "done", "")

    result = run_workflow(
        ControllerConfig(repo_root=root, plan_path=plan, max_turns=8),
        config,
        "repair",
        config_dir=root,
        snapshot_config=False,
        runner=worker,
        resume=resumed.resume_context,
    )
    assert len(calls) == 2
    assert result.status == "completed"
    assert {str(p): p.read_bytes() for p in source.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    "damage",
    [
        "missing_overlay",
        "other_plan",
        "finalized_repair",
        "wrong_transition",
        "completed_source",
    ],
)
def test_rejects_unproven_review_repair(interrupted_repair, damage):
    root, plan, config_path, config, source, payload = interrupted_repair
    review = source / "turns/turn-002/result.json"
    receipt = json.loads(review.read_text())
    if damage == "missing_overlay":
        execution = Path(payload.get("execution_repo_root") or root)
        (execution / Path(payload["active_plan_path"]).relative_to(root)).unlink()
    elif damage == "other_plan":
        receipt["new_plan_path"] = str(root / "unrelated.md")
    elif damage == "wrong_transition":
        receipt["chosen_transition"] = "END"
    elif damage == "completed_source":
        payload["status"] = "completed"
    else:
        attempt = source / "turns/turn-003/result.json"
        value = json.loads(attempt.read_text())
        value["status"] = "completed"
        attempt.write_text(json.dumps(value))
    review.write_text(json.dumps(receipt))
    (source / "run.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="not resumable"):
        bootstrap(interrupted_repair)


@pytest.mark.parametrize("lost_worker", [False, True])
def test_managed_resume_preview_worker_preparation_and_idempotency(
    interrupted_repair, monkeypatch, lost_worker
):
    root, plan, config_path, config, source, payload = interrupted_repair
    env = root / "worker.env"
    env.write_text("AFLOW_FIXTURE=1\n")
    executable = root / "release/bin/aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda _: config)
    units = InMemoryUnitManager()
    write_launch_phase(root, source.name, "failed")
    if lost_worker:
        from aflow.control_plane import worker_diagnostics
        units_dir = source / "units"
        units_dir.mkdir()
        for name, value in {
            "start.json": {"run_id": source.name, "unit": f"aflow-run-{source.name}.service", "wrapper_pid": 99999991, "wrapper_birth": "fixture-wrapper"},
            "child.json": {"pid": 99999992, "pgid": 99999992, "process_birth": "fixture-child"},
        }.items():
            (units_dir / name).write_text(json.dumps({"schema": 1, "nonce": "owned", **value}))
        monkeypatch.setattr(worker_diagnostics, "process_liveness", lambda _: "absent")
        def absent_group(*_args):
            raise ProcessLookupError
        monkeypatch.setattr(worker_diagnostics.os, "killpg", absent_group)
        write_launch_phase(root, source.name, "owner_stopped")
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=env,
            release_identity="fixture",
            stop_timeout_seconds=0,
        ),
        units=units,
    )
    daemon.start()
    before = (source / "run.json").read_bytes()
    assert daemon.service.run_status(source.name).evidence["can_resume"] is True
    successor = daemon.service.resume(
        source.name, caller_scope="project:one", idempotency_key="resume-repair"
    )
    replay = daemon.service.resume(
        source.name, caller_scope="project:one", idempotency_key="resume-repair"
    )
    assert successor.run_id == replay.run_id
    assert len(units.start_calls) == 1
    record = daemon.service._read_record(successor.run_id)
    manifest = daemon.application.repository.get_launch_manifest(successor.run_id)
    prepared, context = _worker_prepared(record, manifest, root, config_path, config)
    assert context.interrupted_step_name == context.review_repair_step == "followup"
    assert context.active_plan_path == Path(payload["active_plan_path"])
    assert (source / "run.json").read_bytes() == before
