from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from aflow.cli import (
    ResumeScopeReconciliationError,
    _reconstruct_resume_context,
)
from aflow.config import (
    AflowSection,
    GoTransition,
    HarnessProfileConfig,
    WorkflowConfig,
    WorkflowHarnessConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
)
from aflow.harnesses.codex import CodexAdapter
from aflow.run_state import ControllerConfig
from aflow.workflow import WorkflowError, run_workflow
from tests._support import _make_git_repo


_CP2_PLAN = """# Plan

### [x] Checkpoint 1: First
- [x] first

### [ ] Checkpoint 2: Second
- [ ] second-a
- [ ] second-b

### [ ] Checkpoint 3: Third
- [ ] third
"""

_INITIAL_PLAN = _CP2_PLAN.replace(
    "### [x] Checkpoint 1: First\n- [x] first",
    "### [ ] Checkpoint 1: First\n- [ ] first",
)

_CP3_PLAN = _CP2_PLAN.replace(
    "### [ ] Checkpoint 2: Second\n- [ ] second-a\n- [ ] second-b",
    "### [x] Checkpoint 2: Second\n- [x] second-a\n- [x] second-b",
)
_COMPLETE_PLAN = _CP3_PLAN.replace(
    "### [ ] Checkpoint 3: Third\n- [ ] third",
    "### [x] Checkpoint 3: Third\n- [x] third",
)


def _workflow_config() -> WorkflowUserConfig:
    return WorkflowUserConfig(
        aflow=AflowSection(max_turns=5),
        roles={"worker": "codex.default"},
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={"default": HarnessProfileConfig(model="fixture")}
            )
        },
        workflows={
            "cumulative": WorkflowConfig(
                steps={
                    "implement_plan": WorkflowStepConfig(
                        role="worker",
                        prompts=("p",),
                        go=(
                            GoTransition(to="END", when="DONE"),
                            GoTransition(to="implement_plan"),
                        ),
                    )
                },
                first_step="implement_plan",
            )
        },
        prompts={"p": "Work from {ACTIVE_PLAN_PATH}."},
    )


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def failed_cumulative_run(tmp_path: Path):
    root = tmp_path / "repo"
    _make_git_repo(root)
    plan_path = root / "plan.md"
    plan_path.write_text(_INITIAL_PLAN, encoding="utf-8")
    config = _workflow_config()
    calls: list[dict[str, object]] = []

    def runner(argv, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            plan_path.write_text(_CP2_PLAN, encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "completed", "")
        plan_path.write_text(_CP3_PLAN, encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 1, "", "session result validation failed"
        )

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(repo_root=root, plan_path=plan_path, max_turns=5),
            config,
            "cumulative",
            config_dir=root,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=runner,
        )
    assert calls
    source_run = failure.value.run_dir
    assert source_run is not None
    payload = json.loads((source_run / "run.json").read_text(encoding="utf-8"))
    return {
        "root": root,
        "plan": plan_path,
        "config": config,
        "source": source_run,
        "payload": payload,
        "source_hashes": _file_hashes(source_run),
    }


def _resume_context(case: dict[str, object], payload: dict[str, object] | None = None):
    plan_path = case["plan"]
    source = case["source"]
    config = case["config"]
    return _reconstruct_resume_context(
        resolved_run_id=Path(source.name),
        run_dir=source,
        prev_run=payload if payload is not None else case["payload"],
        plan_path=plan_path.resolve(),
        frozen_run_identity=None,
        reset_scope=False,
        require_resume=True,
        workflow_steps=config.workflows["cumulative"].steps,
        effective_max_turns=5,
    )


def test_verified_progression_opens_exact_next_scope_once_and_preserves_predecessor(
    failed_cumulative_run,
):
    case = failed_cumulative_run
    source = case["source"]
    context = _resume_context(case)

    assert context.active_implementation_scope is None
    assert context.active_plan_path == case["plan"].resolve()
    assert context.resume_scope_reconciled is True
    assert context.manager_decision_number == case["payload"]["manager_decision_number"]
    assert context.implementation_attempts
    assert _file_hashes(source) == case["source_hashes"]

    calls: list[dict[str, object]] = []

    def resume_runner(argv, **kwargs):
        calls.append(kwargs)
        case["plan"].write_text(_COMPLETE_PLAN, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "completed", "")

    result = run_workflow(
        ControllerConfig(
            repo_root=case["root"],
            plan_path=case["plan"],
            max_turns=5,
            keep_runs=1,
        ),
        case["config"],
        "cumulative",
        config_dir=case["root"],
        snapshot_config=False,
        adapter=CodexAdapter(),
        runner=resume_runner,
        resume=context,
    )

    assert len(calls) == 1
    turn = json.loads(
        (result.run_dir / "turns" / "turn-001" / "result.json").read_text()
    )
    assert turn["snapshot_before"]["current_checkpoint_index"] == 3
    envelopes = list((result.run_dir / "scopes").glob("*/envelope.json"))
    assert len(envelopes) == 1
    assert json.loads(envelopes[0].read_text())["checkpoint_index"] == 3
    assert _file_hashes(source) == case["source_hashes"]


def test_same_scope_incomplete_retry_keeps_scope(failed_cumulative_run):
    case = failed_cumulative_run
    case["plan"].write_text(_CP2_PLAN, encoding="utf-8")
    payload = case["payload"]
    payload["last_snapshot"] = {
        "current_checkpoint_index": 2,
        "current_checkpoint_name": "Checkpoint 2: Second",
        "current_checkpoint_unchecked_step_count": 2,
        "unchecked_checkpoint_count": 2,
        "total_checkpoint_count": 3,
        "is_complete": False,
    }
    context = _resume_context(case, payload)
    assert context.active_implementation_scope is not None
    assert context.active_implementation_scope.checkpoint_index == 2


@pytest.mark.parametrize(
    "mutation",
    ["missing_receipt", "wrong_before", "changed_plan", "changed_bytes"],
)
def test_progression_with_unsupported_evidence_fails_closed(
    failed_cumulative_run, mutation
):
    case = failed_cumulative_run
    source = case["source"]
    result_path = source / "turns" / "turn-002" / "result.json"
    if mutation == "missing_receipt":
        result_path.unlink()
    elif mutation == "wrong_before":
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["snapshot_before"]["current_checkpoint_index"] = 1
        result_path.write_text(json.dumps(result), encoding="utf-8")
    elif mutation == "changed_plan":
        case["plan"].write_text(_CP2_PLAN, encoding="utf-8")
    else:
        case["plan"].write_text(
            _CP3_PLAN.replace(
                "### [ ] Checkpoint 3: Third\n",
                "### [ ] Checkpoint 3: Third\nchanged source bytes\n",
            ),
            encoding="utf-8",
        )

    with pytest.raises(ResumeScopeReconciliationError, match="cannot reconcile"):
        _resume_context(case)


@pytest.mark.parametrize("field", ["awaiting_review", "current_partition_id"])
def test_pending_scope_state_is_not_discarded(failed_cumulative_run, field):
    case = failed_cumulative_run
    payload = json.loads(json.dumps(case["payload"]))
    scope = payload["active_implementation_scope"]
    scope[field] = True if field == "awaiting_review" else "partition-a"

    with pytest.raises(ResumeScopeReconciliationError, match="cannot reconcile"):
        _resume_context(case, payload)


def test_active_source_owner_is_not_reconciled(failed_cumulative_run):
    case = failed_cumulative_run
    payload = json.loads(json.dumps(case["payload"]))
    payload["status"] = "running"

    with pytest.raises(ResumeScopeReconciliationError, match="cannot reconcile"):
        _resume_context(case, payload)


def test_missing_or_tampered_scope_envelope_remains_rejected(failed_cumulative_run):
    case = failed_cumulative_run
    scope = case["payload"]["active_implementation_scope"]
    envelope_path = case["source"] / scope["envelope_artifact_path"]
    envelope_path.write_bytes(envelope_path.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="invalid scope envelope reference"):
        _resume_context(case)
