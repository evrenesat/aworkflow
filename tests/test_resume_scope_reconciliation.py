from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from aflow.cli import (
    ResumeScopeReconciliationError,
    _UNRESOLVED_WORKER_TRANSITION,
    _bootstrap_resume_invocation,
    _detect_resume_candidate,
    _failed_pending_review_step,
    _failed_worker_review_transition,
    _reconstruct_resume_context,
)
from aflow.workflow import (
    _bind_worker_receipt,
    _resume_relocation_from_provenance,
    pick_transition,
)
from aflow.config import (
    AflowSection,
    GoTransition,
    HarnessProfileConfig,
    TeamConfig,
    WorkflowConfig,
    WorkflowHarnessConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
)
from aflow.harnesses.codex import CodexAdapter
from aflow.run_state import ControllerConfig, ResumeContext
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

_SINGLE_CHECKPOINT_PLAN = """# Plan

### [ ] Checkpoint 1: Only
- [ ] only
"""
_SINGLE_CHECKPOINT_COMPLETE = _SINGLE_CHECKPOINT_PLAN.replace(
    "### [ ] Checkpoint 1: Only\n- [ ] only",
    "### [x] Checkpoint 1: Only\n- [x] only",
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


def _checkpoint_review_workflow_config() -> WorkflowUserConfig:
    """A workflow whose worker success transition requires checkpoint review.

    The worker's success successor is the reviewer *unconditionally*: every
    successful worker turn is reviewed before progression, independent of the
    DONE condition.  This is the shape the failed-worker classifier must treat
    as a required checkpoint review at both a mid-plan advance and the final
    checkpoint.
    """
    return WorkflowUserConfig(
        aflow=AflowSection(max_turns=5),
        roles={"worker": "codex.default", "reviewer": "codex.default"},
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={"default": HarnessProfileConfig(model="fixture")}
            )
        },
        workflows={
            "checkpoint_review": WorkflowConfig(
                steps={
                    "implement_plan": WorkflowStepConfig(
                        role="worker",
                        prompts=("implement",),
                        go=(
                            GoTransition(to="review_implementation"),
                        ),
                    ),
                    "review_implementation": WorkflowStepConfig(
                        role="reviewer",
                        prompts=("review",),
                        go=(
                            GoTransition(to="END", when="DONE && !NEW_PLAN_EXISTS"),
                            GoTransition(to="implement_plan", when="NEW_PLAN_EXISTS"),
                            GoTransition(to="implement_plan"),
                        ),
                    ),
                },
                first_step="implement_plan",
            )
        },
        prompts={
            "implement": "Implement from {ACTIVE_PLAN_PATH}.",
            "review": "Review {ORIGINAL_PLAN_PATH}.",
        },
    )


def _conditional_final_review_workflow_config() -> WorkflowUserConfig:
    """A supported graph that reviews a completed middle checkpoint with DONE=False.

    The worker's success transition sends DONE (the final checkpoint) to an
    architect final review, and the fallback (a mid-plan advance, DONE=False) to
    the checkpoint reviewer.  A mid-plan transport advance therefore normally
    routes to the checkpoint reviewer, which is the required checkpoint review
    the failed-worker classifier must select with the validated DONE=False.
    """
    return WorkflowUserConfig(
        aflow=AflowSection(max_turns=5),
        roles={
            "worker": "codex.default",
            "reviewer": "codex.default",
            "architect": "codex.default",
        },
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={"default": HarnessProfileConfig(model="fixture")}
            )
        },
        workflows={
            "conditional_final_review": WorkflowConfig(
                steps={
                    "implement_plan": WorkflowStepConfig(
                        role="worker",
                        prompts=("implement",),
                        go=(
                            GoTransition(to="final_review", when="DONE"),
                            GoTransition(to="review_implementation"),
                        ),
                    ),
                    "review_implementation": WorkflowStepConfig(
                        role="reviewer",
                        prompts=("review",),
                        go=(
                            GoTransition(to="END", when="DONE && !NEW_PLAN_EXISTS"),
                            GoTransition(to="implement_plan", when="NEW_PLAN_EXISTS"),
                            GoTransition(to="implement_plan"),
                        ),
                    ),
                    "final_review": WorkflowStepConfig(
                        role="architect",
                        prompts=("final",),
                        go=(GoTransition(to="END"),),
                    ),
                },
                first_step="implement_plan",
            )
        },
        prompts={
            "implement": "Implement from {ACTIVE_PLAN_PATH}.",
            "review": "Review {ORIGINAL_PLAN_PATH}.",
            "final": "Final review of {ORIGINAL_PLAN_PATH}.",
        },
    )


def _cumulative_reviewer_workflow_config() -> WorkflowUserConfig:
    """A cumulative graph that stays cumulative between checkpoints.

    The worker's success transition sends DONE (the final checkpoint) to the
    reviewer, and the fallback (a mid-plan advance, DONE=False) back to the
    worker.  A mid-plan transport advance therefore normally remains
    cumulative, so the failed-worker classifier must *not* insert a checkpoint
    review for it.
    """
    return WorkflowUserConfig(
        aflow=AflowSection(max_turns=5),
        roles={"worker": "codex.default", "reviewer": "codex.default"},
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={"default": HarnessProfileConfig(model="fixture")}
            )
        },
        workflows={
            "cumulative_reviewer": WorkflowConfig(
                steps={
                    "implement_plan": WorkflowStepConfig(
                        role="worker",
                        prompts=("implement",),
                        go=(
                            GoTransition(to="review_implementation", when="DONE"),
                            GoTransition(to="implement_plan"),
                        ),
                    ),
                    "review_implementation": WorkflowStepConfig(
                        role="reviewer",
                        prompts=("review",),
                        go=(
                            GoTransition(to="END", when="DONE && !NEW_PLAN_EXISTS"),
                            GoTransition(to="implement_plan", when="NEW_PLAN_EXISTS"),
                            GoTransition(to="implement_plan"),
                        ),
                    ),
                },
                first_step="implement_plan",
            )
        },
        prompts={
            "implement": "Implement from {ACTIVE_PLAN_PATH}.",
            "review": "Review {ORIGINAL_PLAN_PATH}.",
        },
    )


def _unresolvable_review_workflow_config() -> WorkflowUserConfig:
    """A worker step whose success transition cannot be resolved.

    The worker has no ``go`` transitions, so the success route is unresolvable.
    A failed worker that advanced its checklist under this shape must refuse
    before any provider launch; it must not close the retained scope.
    """
    return WorkflowUserConfig(
        aflow=AflowSection(max_turns=5),
        roles={"worker": "codex.default", "reviewer": "codex.default"},
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={"default": HarnessProfileConfig(model="fixture")}
            )
        },
        workflows={
            "unresolvable_review": WorkflowConfig(
                steps={
                    "implement_plan": WorkflowStepConfig(
                        role="worker",
                        prompts=("implement",),
                        go=(),
                    ),
                    "review_implementation": WorkflowStepConfig(
                        role="reviewer",
                        prompts=("review",),
                        go=(GoTransition(to="END"),),
                    ),
                },
                first_step="implement_plan",
            )
        },
        prompts={
            "implement": "Implement from {ACTIVE_PLAN_PATH}.",
            "review": "Review {ORIGINAL_PLAN_PATH}.",
        },
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


@pytest.fixture
def failed_worker_review_run(tmp_path: Path):
    """A failed worker that proved a one-checkpoint advance under a review workflow.

    The worker marks Checkpoint 1 complete, then the harness transport fails
    (returncode 124) before any transition is chosen.  The source stays failed
    with the checkpoint-review scope still open.
    """
    root = tmp_path / "repo"
    _make_git_repo(root)
    (root / "aflow.toml").write_text("", encoding="utf-8")
    plan_path = root / "plan.md"
    plan_path.write_text(_INITIAL_PLAN, encoding="utf-8")
    config = _checkpoint_review_workflow_config()
    calls: list[dict[str, object]] = []

    def runner(argv, **kwargs):
        calls.append(kwargs)
        plan_path.write_text(_CP2_PLAN, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 124, "", "harness transport failure")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(repo_root=root, plan_path=plan_path, max_turns=5),
            config,
            "checkpoint_review",
            config_dir=root,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=runner,
        )
    assert len(calls) == 1
    source_run = failure.value.run_dir
    assert source_run is not None
    payload = json.loads((source_run / "run.json").read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["last_snapshot"]["is_complete"] is False
    assert payload["last_snapshot"]["current_checkpoint_index"] == 2
    assert payload["active_implementation_scope"]["checkpoint_index"] == 1
    assert payload["active_implementation_scope"]["awaiting_review"] is not True
    return {
        "root": root,
        "plan": plan_path,
        "config": config,
        "source": source_run,
        "payload": payload,
        "source_hashes": _file_hashes(source_run),
    }


@pytest.fixture
def failed_worker_review_final_run(tmp_path: Path):
    """A failed worker that completed the last checkpoint of a review workflow."""
    root = tmp_path / "repo"
    _make_git_repo(root)
    (root / "aflow.toml").write_text("", encoding="utf-8")
    plan_path = root / "plan.md"
    plan_path.write_text(_SINGLE_CHECKPOINT_PLAN, encoding="utf-8")
    config = _checkpoint_review_workflow_config()

    def runner(argv, **kwargs):
        plan_path.write_text(_SINGLE_CHECKPOINT_COMPLETE, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 124, "", "harness transport failure")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(repo_root=root, plan_path=plan_path, max_turns=5),
            config,
            "checkpoint_review",
            config_dir=root,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=runner,
        )
    source_run = failure.value.run_dir
    assert source_run is not None
    payload = json.loads((source_run / "run.json").read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["last_snapshot"]["is_complete"] is True
    return {
        "root": root,
        "plan": plan_path,
        "config": config,
        "source": source_run,
        "payload": payload,
        "source_hashes": _file_hashes(source_run),
    }


def _resume_context_review(case: dict[str, object], payload: dict[str, object] | None = None):
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
        workflow_steps=config.workflows["checkpoint_review"].steps,
        effective_max_turns=5,
    )


def _bootstrap_review(case: dict[str, object]):
    return _bootstrap_resume_invocation(
        repo_root=case["root"],
        config_path=case["root"] / "aflow.toml",
        default_config_path=case["root"] / "aflow.toml",
        config_path_is_explicit=True,
        workflow_config=case["config"],
        requested_run_id=case["source"].name,
        workflow_arg=None,
        plan_file_arg=None,
        team_arg=None,
        start_step_arg=None,
        max_turns_arg=None,
        extra_instructions_arg=(),
        extra_instructions_provided=False,
        live_loader=lambda _path: case["config"],
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


def test_failed_worker_advanced_checklist_routes_to_checkpoint_reviewer(
    failed_worker_review_run,
):
    case = failed_worker_review_run
    context = _resume_context_review(case)

    assert context.failed_worker_review is not None
    review = context.failed_worker_review
    assert review.reviewer_step_name == "review_implementation"
    assert review.worker_step_name == "implement_plan"
    assert review.worker_turn_number == 1
    assert review.worker_returncode == 124
    assert context.pending_cumulative_review is None
    scope = context.active_implementation_scope
    assert scope is not None
    assert scope.checkpoint_index == 1
    assert scope.awaiting_review is True
    assert context.resume_scope_reconciled is False
    assert _file_hashes(case["source"]) == case["source_hashes"]


def test_failed_worker_advanced_checklist_final_checkpoint_routes_to_reviewer(
    failed_worker_review_final_run,
):
    case = failed_worker_review_final_run
    context = _resume_context_review(case)

    assert context.failed_worker_review is not None
    review = context.failed_worker_review
    assert review.reviewer_step_name == "review_implementation"
    assert review.worker_returncode == 124
    scope = context.active_implementation_scope
    assert scope is not None
    assert scope.checkpoint_index == 1
    assert scope.awaiting_review is True
    assert _file_hashes(case["source"]) == case["source_hashes"]


def test_failed_worker_advanced_checklist_final_checkpoint_bootstrap_passes_second_admission(
    failed_worker_review_final_run,
):
    """The final-checkpoint source passes both CLI admission stages.

    The bootstrap pre-classifier (stage 1) and the resume-candidate detector
    (stage 2, ``_detect_resume_candidate``) must both accept the final-checkpoint
    failed worker so the CLI launches the checkpoint reviewer rather than
    refusing the candidate at the second validation.
    """
    case = failed_worker_review_final_run
    bootstrap = _bootstrap_review(case)

    assert bootstrap.start_step == "review_implementation"
    assert bootstrap.start_step_override is True
    review = bootstrap.resume_context.failed_worker_review
    assert review is not None
    assert review.reviewer_step_name == "review_implementation"
    assert review.worker_returncode == 124
    assert (
        bootstrap.resume_context.active_implementation_scope.awaiting_review is True
    )
    assert _file_hashes(case["source"]) == case["source_hashes"]


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_receipt",
        "changed_plan_bytes",
        "multi_checkpoint_jump",
        "mismatched_snapshot",
        "active_owner",
        "zero_returncode",
        "done_mismatch",
    ),
)
def test_failed_worker_advanced_checklist_unverified_evidence_fails_closed(
    failed_worker_review_run, mutation: str
):
    case = failed_worker_review_run
    payload = json.loads(json.dumps(case["payload"]))
    if mutation == "missing_receipt":
        (case["source"] / "turns" / "turn-001" / "result.json").unlink()
    elif mutation == "changed_plan_bytes":
        case["plan"].write_text(
            _CP2_PLAN.replace(
                "### [ ] Checkpoint 2: Second\n",
                "### [ ] Checkpoint 2: Second\nchanged bytes\n",
            ),
            encoding="utf-8",
        )
    elif mutation == "multi_checkpoint_jump":
        payload["last_snapshot"]["current_checkpoint_index"] = 3
        payload["last_snapshot"]["current_checkpoint_name"] = "Checkpoint 3: Third"
    elif mutation == "mismatched_snapshot":
        result_path = case["source"] / "turns" / "turn-001" / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["snapshot_after"]["current_checkpoint_index"] = 3
        result_path.write_text(json.dumps(result), encoding="utf-8")
    elif mutation == "active_owner":
        payload["status"] = "running"
    elif mutation == "zero_returncode":
        result_path = case["source"] / "turns" / "turn-001" / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["returncode"] = 0
        result_path.write_text(json.dumps(result), encoding="utf-8")
    else:
        # A mid-plan transport failure must not report DONE=True.
        result_path = case["source"] / "turns" / "turn-001" / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["conditions"]["DONE"] = True
        result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(ResumeScopeReconciliationError):
        _resume_context_review(case, payload)



def test_failed_worker_advanced_checklist_bootstrap_starts_checkpoint_reviewer(
    failed_worker_review_run,
):
    case = failed_worker_review_run
    bootstrap = _bootstrap_review(case)

    assert bootstrap.start_step == "review_implementation"
    assert bootstrap.start_step_override is True
    review = bootstrap.resume_context.failed_worker_review
    assert review is not None
    assert review.reviewer_step_name == "review_implementation"
    assert review.worker_returncode == 124
    assert (
        bootstrap.resume_context.active_implementation_scope.awaiting_review is True
    )
    assert _file_hashes(case["source"]) == case["source_hashes"]


def _worker_artifact_read_path(prompt: str) -> str:
    marker = "- Worker artifact path (read this exact file): "
    for line in prompt.splitlines():
        if line.startswith(marker):
            return line[len(marker):].split(". This is the read location", 1)[0].strip()
    raise AssertionError("worker artifact path line missing from reviewer prompt")


def _read_worker_artifact_from(cwd: Path, path: Path) -> str:
    read = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write(open(sys.argv[1], 'rb').read().decode())",
            str(path),
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return read.stdout


def test_failed_worker_advanced_checklist_resume_reviews_before_any_worker(
    failed_worker_review_run,
):
    case = failed_worker_review_run
    context = _resume_context_review(case)
    source_result = case["source"] / "turns" / "turn-001" / "result.json"
    source_bytes = source_result.read_bytes()
    calls: list[str] = []
    reads: list[str] = []

    def reviewer(argv, **kwargs):
        prompt = kwargs["input"]
        calls.append(prompt)
        if "Worker artifact path (read this exact file): " not in prompt:
            # A later worker turn must never run before the reviewer; the
            # fixture stops the workflow there once the reviewer hands off.
            return subprocess.CompletedProcess(argv, 1, "", "worker must not run first")
        path = Path(_worker_artifact_read_path(prompt))
        assert path == source_result
        reads.append(_read_worker_artifact_from(Path(kwargs["cwd"]), path))
        return subprocess.CompletedProcess(argv, 0, "approved", "")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(
                repo_root=case["root"],
                plan_path=case["plan"],
                max_turns=5,
                keep_runs=1,
                start_step="review_implementation",
                start_step_explicit=True,
            ),
            case["config"],
            "checkpoint_review",
            config_dir=case["root"],
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=reviewer,
            resume=context,
        )

    # The first provider invocation is the bound checkpoint reviewer, carrying
    # the truthful failed-worker evidence; no worker ran beforehand.
    assert "verified failed worker result" in calls[0]
    assert "returncode 124" in calls[0]
    assert "checked boxes are implementation evidence, not approval" in calls[0]
    assert len(reads) == 1
    assert reads[0] == source_bytes.decode()
    result_turn = json.loads(
        (failure.value.run_dir / "turns" / "turn-001" / "result.json").read_text()
    )
    assert result_turn["step_role"] == "reviewer"
    assert result_turn["step_name"] == "review_implementation"
    assert _file_hashes(case["source"]) == case["source_hashes"]


def test_failed_worker_advanced_checklist_rejection_repairs_same_scope(
    failed_worker_review_run,
):
    case = failed_worker_review_run
    context = _resume_context_review(case)
    overlay = case["plan"].with_name("plan-cp01-v01.md")
    calls: list[str] = []

    def reject_then_fail(argv, **kwargs):
        calls.append(kwargs["input"])
        if len(calls) == 1:
            overlay.write_text(
                "# Follow-up\n\n### [ ] Checkpoint 1: Repair\n- [ ] repair\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                argv, 0, "rejected: repair needed", ""
            )
        return subprocess.CompletedProcess(argv, 1, "", "worker stopped for fixture")

    with pytest.raises(WorkflowError):
        run_workflow(
            ControllerConfig(
                repo_root=case["root"],
                plan_path=case["plan"],
                max_turns=5,
                keep_runs=1,
                start_step="review_implementation",
                start_step_explicit=True,
            ),
            case["config"],
            "checkpoint_review",
            config_dir=case["root"],
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=reject_then_fail,
            resume=context,
        )

    assert len(calls) == 2
    successor = sorted(
        path for path in (case["root"] / ".aflow" / "runs").iterdir()
        if path != case["source"]
    )[-1]
    review_turn = json.loads(
        (successor / "turns" / "turn-001" / "result.json").read_text()
    )
    assert review_turn["step_role"] == "reviewer"
    assert review_turn["review_rejection"] is not None
    assert review_turn["new_plan_path"].endswith("plan-cp01-v01.md")
    assert overlay.is_file()
    assert _file_hashes(case["source"]) == case["source_hashes"]


def test_failed_worker_advanced_checklist_classification_is_idempotent(
    failed_worker_review_run,
):
    case = failed_worker_review_run
    first = _resume_context_review(case)
    second = _resume_context_review(case)

    assert first.failed_worker_review is not None
    assert second.failed_worker_review is not None
    assert first.failed_worker_review == second.failed_worker_review
    # Repeated reconciliation reclassifies the same immutable evidence to the
    # same reviewer; it never fabricates a second successor scope.
    assert first.active_implementation_scope is not None
    assert second.active_implementation_scope is not None
    assert second.active_implementation_scope.scope_id == first.active_implementation_scope.scope_id
    assert first.failed_worker_review.reviewer_step_name == "review_implementation"
    assert _file_hashes(case["source"]) == case["source_hashes"]


def test_failed_worker_advanced_checklist_bootstrap_fails_closed_on_tampered_evidence(
    failed_worker_review_run,
):
    case = failed_worker_review_run
    result_path = case["source"] / "turns" / "turn-001" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["returncode"] = 0
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(ValueError, match="evidence is invalid"):
        _bootstrap_review(case)


def _make_failed_worker_run(
    tmp_path: Path,
    config: WorkflowUserConfig,
    workflow_name: str,
) -> dict[str, object]:
    """Build a failed worker run that advanced one checkpoint under a workflow.

    The worker marks Checkpoint 1 complete, then the harness transport fails
    (returncode 124) before any transition is chosen; the source stays failed
    with the checkpoint scope still open (awaiting_review not set).
    """
    root = tmp_path / "repo"
    _make_git_repo(root)
    (root / "aflow.toml").write_text("", encoding="utf-8")
    plan_path = root / "plan.md"
    plan_path.write_text(_INITIAL_PLAN, encoding="utf-8")

    def runner(argv, **kwargs):
        plan_path.write_text(_CP2_PLAN, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 124, "", "harness transport failure")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(repo_root=root, plan_path=plan_path, max_turns=5),
            config,
            workflow_name,
            config_dir=root,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=runner,
        )
    source_run = failure.value.run_dir
    assert source_run is not None
    payload = json.loads((source_run / "run.json").read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["last_snapshot"]["is_complete"] is False
    return {
        "root": root,
        "plan": plan_path,
        "config": config,
        "workflow": workflow_name,
        "source": source_run,
        "payload": payload,
        "source_hashes": _file_hashes(source_run),
    }


@pytest.mark.parametrize(
    ("config", "workflow", "expected"),
    [
        (
            _checkpoint_review_workflow_config(),
            "checkpoint_review",
            "review_implementation",
        ),
        (
            _conditional_final_review_workflow_config(),
            "conditional_final_review",
            "review_implementation",
        ),
        (
            _cumulative_reviewer_workflow_config(),
            "cumulative_reviewer",
            None,
        ),
    ],
    ids=["unconditional", "conditional_final_review", "cumulative"],
)
def test_failed_worker_advanced_checklist_selection_matches_normal_success(
    tmp_path: Path, config: WorkflowUserConfig, workflow: str, expected
):
    """The resume classifier must match normal success selection at DONE=False.

    A mid-plan transport advance reports DONE=False; the same condition must
    drive both the normal successful-worker transition and the resume
    classifier.  A reviewer target is a required checkpoint review; a worker
    target keeps the cumulative one-hop behavior.
    """
    case = _make_failed_worker_run(tmp_path, config, workflow)
    steps = case["config"].workflows[workflow].steps
    worker_step = steps["implement_plan"]
    normal_target = pick_transition(
        tuple(worker_step.go),
        step_path="workflow.implement_plan",
        done=False,
        new_plan_exists=False,
        max_turns_reached=False,
    )
    resume_selection = _failed_worker_review_transition(
        steps, "implement_plan", done=False
    )
    if expected is None:
        # Cumulative graph: the normal target is a worker, and the resume
        # classifier must not insert a checkpoint review.
        assert normal_target == "implement_plan"
        assert resume_selection is None
    else:
        assert normal_target == expected
        assert resume_selection == expected


@pytest.mark.parametrize(
    ("config", "workflow", "expected"),
    [
        (_checkpoint_review_workflow_config(), "checkpoint_review", "review_implementation"),
        (
            _conditional_final_review_workflow_config(),
            "conditional_final_review",
            None,
        ),
        (_cumulative_reviewer_workflow_config(), "cumulative_reviewer", "review_implementation"),
    ],
    ids=["unconditional", "conditional_final_review", "cumulative"],
)
def test_failed_worker_advanced_checklist_final_checkpoint_selection(
    tmp_path: Path, config: WorkflowUserConfig, workflow: str, expected: str | None
):
    """At the final checkpoint (DONE=True) the validated condition drives routing.

    The final-checkpoint source reports DONE=True; the same condition must
    select the same target in normal and resume selection.  A reviewer target is
    a required checkpoint review; a resolved final-review (architect) target
    keeps its existing non-checkpoint-review route.
    """
    root = tmp_path / "repo"
    _make_git_repo(root)
    (root / "aflow.toml").write_text("", encoding="utf-8")
    plan_path = root / "plan.md"
    plan_path.write_text(_SINGLE_CHECKPOINT_PLAN, encoding="utf-8")

    def runner(argv, **kwargs):
        plan_path.write_text(_SINGLE_CHECKPOINT_COMPLETE, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 124, "", "harness transport failure")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(repo_root=root, plan_path=plan_path, max_turns=5),
            config,
            workflow,
            config_dir=root,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=runner,
        )
    payload = json.loads((failure.value.run_dir / "run.json").read_text(encoding="utf-8"))
    assert payload["last_snapshot"]["is_complete"] is True
    steps = config.workflows[workflow].steps
    worker_step = steps["implement_plan"]
    normal_target = pick_transition(
        tuple(worker_step.go),
        step_path="workflow.implement_plan",
        done=True,
        new_plan_exists=False,
        max_turns_reached=False,
    )
    resume_selection = _failed_worker_review_transition(
        steps, "implement_plan", done=True
    )
    if expected is None:
        # The conditional graph routes DONE=True to the architect final review,
        # a deliberate non-checkpoint-review route; the resume classifier must
        # not insert a checkpoint review.
        assert normal_target == "final_review"
        assert resume_selection is None
    else:
        assert normal_target == expected
        assert resume_selection == expected


def test_failed_worker_advanced_checklist_unresolvable_transition_refuses(
    tmp_path: Path,
):
    """An unresolvable worker success transition refuses before any provider.

    A worker step with no success transitions cannot be routed; a failed worker
    that advanced its checklist under this shape must refuse cleanly (no
    successor, no scope closure) rather than fall back to cumulative behavior.
    """
    case = _make_failed_worker_run(
        tmp_path, _unresolvable_review_workflow_config(), "unresolvable_review"
    )
    steps = case["config"].workflows["unresolvable_review"].steps
    selection = _failed_worker_review_transition(
        steps, "implement_plan", done=False
    )
    assert selection is _UNRESOLVED_WORKER_TRANSITION
    with pytest.raises(ValueError, match="cannot be resolved"):
        _bootstrap_review(case)


def _resume_context_reviewer_retry(case: dict[str, object], b: Path, b_payload: dict[str, object]) -> ResumeContext:
    """Reconstruct the supported resume of a failed reviewer successor."""
    return _reconstruct_resume_context(
        resolved_run_id=Path(b.name),
        run_dir=b,
        prev_run=b_payload,
        plan_path=case["plan"].resolve(),
        frozen_run_identity=None,
        reset_scope=False,
        require_resume=True,
        workflow_steps=case["config"].workflows["checkpoint_review"].steps,
        effective_max_turns=5,
    )


def test_failed_worker_advanced_checklist_reviewer_retry_rebinds_failed_worker(
    tmp_path: Path,
):
    """A descendant reviewer retry rebinds the exact failed worker ancestor.

    worker124 -> reviewer124 (successor) -> resume: the failed worker must be
    bound through the immutable lineage, the resumed reviewer must reference
    the original worker receipt, approval must advance to the next worker, and
    the retained ancestor must survive pruning byte-for-byte.
    """
    case = _make_failed_worker_run(
        tmp_path, _checkpoint_review_workflow_config(), "checkpoint_review"
    )
    context = _resume_context_review(case)

    def reviewer_fail(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 124, "", "harness transport failure")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(
                repo_root=case["root"], plan_path=case["plan"], max_turns=5,
                keep_runs=1, start_step="review_implementation",
                start_step_explicit=True,
            ),
            case["config"],
            "checkpoint_review",
            config_dir=case["root"],
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=reviewer_fail,
            resume=context,
        )
    b = failure.value.run_dir
    b_payload = json.loads((b / "run.json").read_text(encoding="utf-8"))
    assert b_payload["status"] == "failed"
    assert b_payload["active_implementation_scope"]["awaiting_review"] is True
    assert b_payload["resumed_from_run_id"] == case["source"].name
    a_result_bytes = (
        case["source"] / "turns" / "turn-001" / "result.json"
    ).read_bytes()

    retry_context = _resume_context_reviewer_retry(case, b, b_payload)
    assert retry_context.failed_pending_review_step == "review_implementation"

    calls: list[str] = []

    def approve_then_work(argv, **kwargs):
        prompt = kwargs["input"]
        calls.append(prompt)
        if "Worker artifact path" in prompt:
            return subprocess.CompletedProcess(argv, 0, "approved", "")
        case["plan"].write_text(_CP3_PLAN, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 1, "", "worker stopped for fixture")

    with pytest.raises(WorkflowError) as failure2:
        run_workflow(
            ControllerConfig(
                repo_root=case["root"], plan_path=case["plan"], max_turns=5,
                keep_runs=1, start_step="review_implementation",
                start_step_explicit=True,
            ),
            case["config"],
            "checkpoint_review",
            config_dir=case["root"],
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=approve_then_work,
            resume=retry_context,
        )
    c = failure2.value.run_dir
    # The resumed reviewer references the original failed worker's receipt.
    assert case["source"].name in calls[0]
    t1 = json.loads((c / "turns" / "turn-001" / "result.json").read_text(encoding="utf-8"))
    assert t1["step_role"] == "reviewer"
    assert t1["step_name"] == "review_implementation"
    # The retained ancestor survives pruning byte-for-byte.
    assert (case["source"] / "run.json").is_file()
    assert (
        case["source"] / "turns" / "turn-001" / "result.json"
    ).read_bytes() == a_result_bytes


def test_failed_worker_advanced_checklist_final_reviewer_retry_rebinds_failed_worker(
    tmp_path: Path,
):
    """A final-checkpoint failed worker survives a failed reviewer retry.

    final worker124 (DONE=True, final completion proof) -> reviewer124 ->
    resume: the exact final-completion proof must rebind, the resumed reviewer
    references the original worker receipt, and approval ends the workflow
    without starting any next worker.
    """
    case = _make_final_failed_worker_run(tmp_path)
    context = _resume_context_review(case)

    def reviewer_fail(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 124, "", "harness transport failure")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(
                repo_root=case["root"], plan_path=case["plan"], max_turns=5,
                keep_runs=1, start_step="review_implementation",
                start_step_explicit=True,
            ),
            case["config"],
            "checkpoint_review",
            config_dir=case["root"],
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=reviewer_fail,
            resume=context,
        )
    b = failure.value.run_dir
    b_payload = json.loads((b / "run.json").read_text(encoding="utf-8"))
    assert b_payload["status"] == "failed"
    assert b_payload["active_implementation_scope"]["awaiting_review"] is True
    assert b_payload["resumed_from_run_id"] == case["source"].name

    retry_context = _resume_context_reviewer_retry(case, b, b_payload)
    assert retry_context.failed_pending_review_step == "review_implementation"

    calls: list[str] = []

    def approve(argv, **kwargs):
        calls.append(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0, "approved", "")

    result = run_workflow(
        ControllerConfig(
            repo_root=case["root"], plan_path=case["plan"], max_turns=5,
            keep_runs=1, start_step="review_implementation",
            start_step_explicit=True,
        ),
        case["config"],
        "checkpoint_review",
        config_dir=case["root"],
        snapshot_config=False,
        adapter=CodexAdapter(),
        runner=approve,
        resume=retry_context,
    )
    # The resumed reviewer references the original failed worker's receipt.
    assert case["source"].name in calls[0]
    c = result.run_dir
    t1 = json.loads((c / "turns" / "turn-001" / "result.json").read_text(encoding="utf-8"))
    assert t1["step_role"] == "reviewer"
    assert t1["step_name"] == "review_implementation"
    # Approval of the final checkpoint ends the workflow; no next worker ran.
    assert len(calls) == 1
    assert (case["source"] / "turns" / "turn-001" / "result.json").is_file()


def _make_final_failed_worker_run(tmp_path: Path) -> dict[str, object]:
    """A failed worker that completed the last checkpoint (DONE=True)."""
    root = tmp_path / "repo"
    _make_git_repo(root)
    (root / "aflow.toml").write_text("", encoding="utf-8")
    plan_path = root / "plan.md"
    plan_path.write_text(_SINGLE_CHECKPOINT_PLAN, encoding="utf-8")
    config = _checkpoint_review_workflow_config()

    def runner(argv, **kwargs):
        plan_path.write_text(_SINGLE_CHECKPOINT_COMPLETE, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 124, "", "harness transport failure")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(repo_root=root, plan_path=plan_path, max_turns=5),
            config,
            "checkpoint_review",
            config_dir=root,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=runner,
        )
    source_run = failure.value.run_dir
    payload = json.loads((source_run / "run.json").read_text(encoding="utf-8"))
    return {
        "root": root,
        "plan": plan_path,
        "config": config,
        "source": source_run,
        "payload": payload,
        "source_hashes": _file_hashes(source_run),
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "absent_ancestor",
        "foreign_ancestor",
        "tampered_ancestor",
        "tampered_envelope",
        "null_before_snapshot",
        "mismatched_before_snapshot",
        "before_snapshot_foreign_name",
        "before_snapshot_step_count",
        "missing_active_plan",
        "foreign_active_plan",
        "contradictory_conditions",
        "after_snapshot_is_before",
        "mismatched_selector",
        "ancestor_status_running",
        "ancestor_active_turn",
        "ancestor_current_step",
        "ancestor_end_reason",
    ],
)
@pytest.mark.parametrize("final_checkpoint", [False, True])
def test_failed_worker_advanced_checklist_reviewer_retry_lineage_refusals(
    tmp_path: Path, mutation: str, final_checkpoint: bool,
):
    """A missing/foreign/tampered ancestor or envelope refuses the retry.

    Covers both the middle checkpoint (exact advance) and the last checkpoint
    (final completion) proof for the strict failed-ancestor validation,
    including a before-snapshot that retains its index while contradicting
    the captured scope name/counts, and ancestor terminal metadata that
    contradicts the selected worker turn.
    """
    case = (
        _make_final_failed_worker_run(tmp_path)
        if final_checkpoint
        else _make_failed_worker_run(
            tmp_path, _checkpoint_review_workflow_config(), "checkpoint_review"
        )
    )
    context = _resume_context_review(case)

    def reviewer_fail(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 124, "", "harness transport failure")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(
                repo_root=case["root"], plan_path=case["plan"], max_turns=5,
                keep_runs=1, start_step="review_implementation",
                start_step_explicit=True,
            ),
            case["config"],
            "checkpoint_review",
            config_dir=case["root"],
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=reviewer_fail,
            resume=context,
        )
    b = failure.value.run_dir
    b_payload = json.loads((b / "run.json").read_text(encoding="utf-8"))
    a = case["source"]
    if mutation in (
        "ancestor_status_running",
        "ancestor_active_turn",
        "ancestor_current_step",
        "ancestor_end_reason",
    ):
        a_payload = json.loads((a / "run.json").read_text(encoding="utf-8"))
        if mutation == "ancestor_status_running":
            a_payload["status"] = "running"
        elif mutation == "ancestor_active_turn":
            a_payload["active_turn"] = a_payload["active_turn"] + 2
        elif mutation == "ancestor_current_step":
            a_payload["current_step_name"] = "review_implementation"
        else:
            a_payload["end_reason"] = "max_turns"
        (a / "run.json").write_text(json.dumps(a_payload), encoding="utf-8")
    elif mutation == "absent_ancestor":
        shutil.rmtree(a)
    elif mutation == "foreign_ancestor":
        # The lineage walk reads the owner chain from disk, so the foreign
        # link must be written to B's actual run.json.
        b_meta = json.loads((b / "run.json").read_text(encoding="utf-8"))
        b_meta["resumed_from_run_id"] = "20000000t000000z-foreign0"
        (b / "run.json").write_text(
            json.dumps(b_meta), encoding="utf-8"
        )
        b_payload = b_meta
    elif mutation == "tampered_ancestor":
        receipt_path = a / "turns" / "turn-001" / "result.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["returncode"] = 0
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    elif mutation == "tampered_envelope":
        a_payload = json.loads((a / "run.json").read_text(encoding="utf-8"))
        envelope_path = (
            a / a_payload["active_implementation_scope"]["envelope_artifact_path"]
        )
        envelope_path.write_bytes(envelope_path.read_bytes() + b"tampered")
    elif mutation in (
        "null_before_snapshot",
        "mismatched_before_snapshot",
        "before_snapshot_foreign_name",
        "before_snapshot_step_count",
        "missing_active_plan",
        "foreign_active_plan",
        "contradictory_conditions",
        "after_snapshot_is_before",
        "mismatched_selector",
    ):
        receipt_path = a / "turns" / "turn-001" / "result.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if mutation == "null_before_snapshot":
            receipt["snapshot_before"] = None
        elif mutation == "before_snapshot_foreign_name":
            # A decodable before-snapshot that retains its captured index but
            # names a foreign checkpoint: the full captured-scope snapshot
            # binding must refuse it.
            before = dict(receipt["snapshot_before"])
            before["current_checkpoint_name"] = "Checkpoint 99: Foreign"
            receipt["snapshot_before"] = before
        elif mutation == "before_snapshot_step_count":
            # A decodable before-snapshot that retains its captured index but
            # contradicts the captured scope's step counts.
            before = dict(receipt["snapshot_before"])
            before["current_checkpoint_unchecked_step_count"] += 3
            receipt["snapshot_before"] = before
        elif mutation == "mismatched_before_snapshot":
            # A decodable before-snapshot that contradicts the captured scope
            # checkpoint: the advanced (final completion) snapshot for a
            # middle checkpoint, or a foreign checkpoint index for the last.
            if final_checkpoint:
                before = dict(receipt["snapshot_before"])
                before["current_checkpoint_index"] = 2
                receipt["snapshot_before"] = before
            else:
                receipt["snapshot_before"] = dict(receipt["snapshot_after"])
        elif mutation == "missing_active_plan":
            receipt["active_plan_path"] = None
        elif mutation == "foreign_active_plan":
            receipt["active_plan_path"] = str(a / "foreign-overlay.md")
        elif mutation == "contradictory_conditions":
            receipt["conditions"] = {
                "DONE": True,
                "NEW_PLAN_EXISTS": True,
                "MAX_TURNS_REACHED": True,
            }
        elif mutation == "after_snapshot_is_before":
            receipt["snapshot_after"] = dict(receipt["snapshot_before"])
        else:
            receipt["selector"] = "codex.different"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ValueError, match="pending-review evidence is invalid"):
        _failed_pending_review_step(
            b,
            b_payload,
            workflow_steps=case["config"].workflows["checkpoint_review"].steps,
            relocation=None,
            plan_path=case["plan"].resolve(),
        )
    # The full reconstruction refuses the same contradictory ancestor before
    # any allocation or provider invocation.
    with pytest.raises(ValueError, match="pending-review evidence is invalid"):
        _resume_context_reviewer_retry(case, b, b_payload)
    # The strict binder returns no receipt and publishes no dependency for
    # the invalid proof.
    scope_data = b_payload["active_implementation_scope"]
    assert isinstance(scope_data, dict)
    attempts = (
        b_payload.get("implementation_attempts")
        if isinstance(b_payload.get("implementation_attempts"), dict)
        else {}
    ).get(scope_data["scope_id"], [])
    last_worker = [x for x in attempts if isinstance(x, dict) and x.get("role") == "worker"][-1]
    envelope_values = (
        scope_data.get("envelope_artifact_path"),
        scope_data.get("envelope_artifact_sha256"),
        scope_data.get("envelope_canonical_sha256"),
    )
    deps: set[str] = set()
    bound = _bind_worker_receipt(
        case["root"],
        b.name,
        last_worker["turn_number"],
        scope_id=scope_data["scope_id"],
        step_name=last_worker.get("step_name"),
        attempt_ordinal=last_worker.get("attempt_ordinal"),
        scope_checkpoint_index=scope_data.get("checkpoint_index"),
        scope_checkpoint_name=scope_data.get("checkpoint_name"),
        scope_original_plan=scope_data.get("original_plan_path"),
        scope_envelope=(
            tuple(envelope_values)  # type: ignore[arg-type]
            if all(isinstance(v, str) and v for v in envelope_values)
            else None
        ),
        strict=True,
        required_run_ids=deps,
    )
    assert bound is None
    assert not deps


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        capture_output=True,
    )


_REHOME_BRANCH = "feature/failed-worker-rehome"


def _plan_with_git_tracking(plan_text: str, base: str) -> str:
    return plan_text.replace(
        "# Plan\n\n",
        "# Plan\n\n"
        "## Git Tracking\n\n"
        f"- Plan Branch: `{_REHOME_BRANCH}`\n"
        f"- Pre-Handoff Base HEAD: `{base}`\n\n",
        1,
    )


def _make_git_tracked_failed_worker_run(
    tmp_path: Path, *, final_checkpoint: bool,
) -> dict[str, object]:
    """A failed worker run whose plan carries Git Tracking from the start.

    The Git Tracking section is part of the captured plan (and therefore of
    the immutable scope envelope), as it is for a real handoff plan; the
    current checkout is on the recorded main branch with a registered
    replacement feature worktree on the recorded plan branch.
    """
    root = tmp_path / "repo"
    _make_git_repo(root)
    (root / "aflow.toml").write_text("", encoding="utf-8")
    plan_path = root / "plan.md"
    if final_checkpoint:
        initial, advanced = _SINGLE_CHECKPOINT_PLAN, _SINGLE_CHECKPOINT_COMPLETE
    else:
        initial, advanced = _INITIAL_PLAN, _CP2_PLAN
    base = subprocess.check_output(
        ("git", "-C", str(root), "rev-parse", "HEAD"), text=True
    ).strip()
    plan_path.write_text(_plan_with_git_tracking(initial, base), encoding="utf-8")
    config = _checkpoint_review_workflow_config()

    def runner(argv, **kwargs):
        plan_path.write_text(_plan_with_git_tracking(advanced, base), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 124, "", "harness transport failure")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(repo_root=root, plan_path=plan_path, max_turns=5),
            config,
            "checkpoint_review",
            config_dir=root,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=runner,
        )
    source_run = failure.value.run_dir
    payload = json.loads((source_run / "run.json").read_text(encoding="utf-8"))
    _git(root, "branch", "-M", "main")
    _git(root, "add", "plan.md")
    _git(root, "commit", "-m", "fixture advanced checkpoint state")
    worktree = root.parent / "failed-worker-rehome-worktree"
    _git(root, "worktree", "add", "-b", _REHOME_BRANCH, str(worktree))
    return {
        "root": root,
        "plan": plan_path,
        "config": config,
        "source": source_run,
        "payload": payload,
        "worktree": worktree,
        "base": base,
        "source_hashes": _file_hashes(source_run),
    }


def _rewrite_failed_worker_to_old_identity(
    case: dict[str, object],
) -> tuple[Path, dict[str, str]]:
    """Rewrite the failed-worker source to a vanished old repo/worktree.

    The raw historical receipts and run metadata keep their old-location
    identities; the current checkout is the new location.  Returns the old
    repo root and the source directory's byte hashes.
    """
    root: Path = case["root"]
    source: Path = case["source"]
    old_repo = root.parent / "old-failed-worker-repo"
    old_worktree = root.parent / "old-failed-worker-worktree"
    result_path = source / "turns" / "turn-001" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["original_plan_path"] = str(old_repo / "plan.md")
    result["active_plan_path"] = str(old_worktree / "plan.md")
    result_path.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")

    payload = json.loads((source / "run.json").read_text(encoding="utf-8"))
    payload.update(
        {
            "repo_root": str(old_repo),
            "run_dir": str(old_repo / ".aflow" / "runs" / source.name),
            "plan_path": str(old_repo / "plan.md"),
            "original_plan_path": str(old_repo / "plan.md"),
            "active_plan_path": str(old_worktree / "plan.md"),
            "worktree_path": str(old_worktree),
            "feature_branch": _REHOME_BRANCH,
            "main_branch": "main",
            "lifecycle_setup": ["worktree", "branch"],
            "lifecycle_teardown": ["merge", "rm_worktree"],
            "workspace_state": {
                "branch": _REHOME_BRANCH,
                "head": subprocess.check_output(
                    ("git", "-C", str(case["worktree"]), "rev-parse", "HEAD"), text=True
                ).strip(),
                "dirty_worktree": "",
            },
        }
    )
    scope = payload["active_implementation_scope"]
    assert isinstance(scope, dict)
    scope["original_plan_path"] = str(old_repo / "plan.md")
    (source / "run.json").write_text(
        json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    return old_repo, _file_hashes(source)


@pytest.mark.parametrize("final_checkpoint", [False, True], ids=["middle", "final"])
def test_failed_worker_advanced_checklist_rehome_bootstrap_maps_receipt_identities_and_reviews_first(
    tmp_path: Path, final_checkpoint: bool,
):
    """A rehomed failed-worker source passes both CLI acceptance stages.

    With the original Git Tracking, a corresponding registered replacement
    feature worktree and main checkout, and a valid ``rehome_worktree``
    bootstrap, the raw historical receipt identities are mapped into the
    current identity space, the second CLI detection admits the source, and
    the same checkpoint reviewer starts first with its historical receipt
    unchanged while the original scope is retained.
    """
    case = _make_git_tracked_failed_worker_run(
        tmp_path, final_checkpoint=final_checkpoint
    )
    root: Path = case["root"]
    plan: Path = case["plan"]
    source: Path = case["source"]
    worktree: Path = case["worktree"]
    _old_repo, before = _rewrite_failed_worker_to_old_identity(case)

    bootstrap = _bootstrap_resume_invocation(
        repo_root=root,
        config_path=root / "aflow.toml",
        default_config_path=root / "aflow.toml",
        config_path_is_explicit=True,
        workflow_config=case["config"],
        requested_run_id=source.name,
        workflow_arg=None,
        plan_file_arg=None,
        team_arg=None,
        start_step_arg=None,
        max_turns_arg=None,
        extra_instructions_arg=(),
        extra_instructions_provided=False,
        rehome_worktree=worktree,
        live_loader=lambda _path: case["config"],
    )
    # The second CLI detection (full reconstruction) admits the rehomed
    # source through the shared strict failed-worker proof.
    assert bootstrap.start_step == "review_implementation"
    assert (
        bootstrap.resume_context.resume_relocation["current_worktree_root"]
        == str(worktree)
    )

    calls: list[str] = []

    def reviewer_then_worker(argv, **kwargs):
        prompt = kwargs["input"]
        calls.append(prompt)
        if "Worker artifact path" in prompt:
            return subprocess.CompletedProcess(argv, 0, "approved", "")
        plan.write_text(
            _plan_with_git_tracking(_CP3_PLAN, case["base"]), encoding="utf-8"
        )
        return subprocess.CompletedProcess(
            argv, 124, "", "harness transport failure"
        )

    try:
        result = run_workflow(
            ControllerConfig(
                repo_root=root,
                plan_path=plan,
                max_turns=5,
                keep_runs=1,
                start_step=bootstrap.start_step,
                start_step_explicit=True,
            ),
            case["config"],
            "checkpoint_review",
            config_dir=root,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=reviewer_then_worker,
            resume=replace(bootstrap.resume_context, teardown=()),
        )
        final_run_dir: Path = result.run_dir
    except WorkflowError as failure:
        final_run_dir = failure.run_dir

    # Reviewer-first: the first launch is the checkpoint reviewer referencing
    # the exact historical failed-worker artifact; no worker ran before it.
    assert len(calls) >= 1
    assert "Worker artifact path" in calls[0]
    assert source.name in calls[0]
    assert (
        _worker_artifact_read_path(calls[0])
        == str(source / "turns" / "turn-001" / "result.json")
    )
    if final_checkpoint:
        # Approval of the final checkpoint ends the workflow; no worker ran.
        assert len(calls) == 1
    else:
        # The mid-plan approval advances to the next worker only after the
        # review of the historical failed worker.
        assert len(calls) == 2
        assert "Worker artifact path" not in calls[1]
    # The retained source is byte-for-byte unchanged.
    assert _file_hashes(source) == before
    run_payload = json.loads((final_run_dir / "run.json").read_text(encoding="utf-8"))
    assert run_payload["resumed_from_run_id"] == source.name


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_active_plan",
        "foreign_active_plan",
        "unmapped_original_plan",
        "overlay_original_plan",
    ],
)
def test_failed_worker_advanced_checklist_rehome_refuses_unmapped_or_foreign_identities(
    tmp_path: Path, mutation: str,
):
    """Accepting a rehome never broadens the identity grant.

    Missing, external, unmapped, or overlay plan identities in the raw
    historical receipt refuse the rehomed bootstrap before any allocation,
    leaving the source byte-for-byte unchanged.
    """
    case = _make_git_tracked_failed_worker_run(tmp_path, final_checkpoint=False)
    root: Path = case["root"]
    source: Path = case["source"]
    worktree: Path = case["worktree"]
    old_repo, before = _rewrite_failed_worker_to_old_identity(case)
    result_path = source / "turns" / "turn-001" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if mutation == "missing_active_plan":
        result["active_plan_path"] = None
    elif mutation == "foreign_active_plan":
        result["active_plan_path"] = str(root.parent / "foreign-plan.md")
    elif mutation == "unmapped_original_plan":
        result["original_plan_path"] = str(root.parent / "elsewhere" / "plan.md")
    else:
        result["original_plan_path"] = str(old_repo / "unrelated-overlay.md")
    result_path.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    mutated = _file_hashes(source)

    with pytest.raises(ValueError):
        _bootstrap_resume_invocation(
            repo_root=root,
            config_path=root / "aflow.toml",
            default_config_path=root / "aflow.toml",
            config_path_is_explicit=True,
            workflow_config=case["config"],
            requested_run_id=source.name,
            workflow_arg=None,
            plan_file_arg=None,
            team_arg=None,
            start_step_arg=None,
            max_turns_arg=None,
            extra_instructions_arg=(),
            extra_instructions_provided=False,
            rehome_worktree=worktree,
            live_loader=lambda _path: case["config"],
        )

    # No successor was allocated and the source is byte-for-byte unchanged
    # by the refused bootstrap.
    assert len(list((root / ".aflow" / "runs").iterdir())) == 1
    assert _file_hashes(source) == mutated


def _rehome_reviewer_retry_case(
    tmp_path: Path, *, final_checkpoint: bool,
) -> dict[str, object]:
    """A rehomed failed-worker source whose reviewer successor failed.

    worker124 (old identity) -> rehome bootstrap -> reviewer124 successor,
    leaving a failed terminal successor with the original awaiting scope, the
    worker's recorded attempt, and the verified relocation provenance.
    """
    case = _make_git_tracked_failed_worker_run(
        tmp_path, final_checkpoint=final_checkpoint
    )
    root: Path = case["root"]
    plan: Path = case["plan"]
    source: Path = case["source"]
    worktree: Path = case["worktree"]
    _old_repo, _before = _rewrite_failed_worker_to_old_identity(case)

    bootstrap = _bootstrap_resume_invocation(
        repo_root=root,
        config_path=root / "aflow.toml",
        default_config_path=root / "aflow.toml",
        config_path_is_explicit=True,
        workflow_config=case["config"],
        requested_run_id=source.name,
        workflow_arg=None,
        plan_file_arg=None,
        team_arg=None,
        start_step_arg=None,
        max_turns_arg=None,
        extra_instructions_arg=(),
        extra_instructions_provided=False,
        rehome_worktree=worktree,
        live_loader=lambda _path: case["config"],
    )
    assert bootstrap.start_step == "review_implementation"

    def reviewer_fail(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 124, "", "harness transport failure")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan, max_turns=5, keep_runs=1,
                start_step="review_implementation", start_step_explicit=True,
            ),
            case["config"],
            "checkpoint_review",
            config_dir=root,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=reviewer_fail,
            resume=replace(bootstrap.resume_context, teardown=()),
        )
    b = failure.value.run_dir
    b_payload = json.loads((b / "run.json").read_text(encoding="utf-8"))
    assert b_payload["status"] == "failed"
    assert b_payload["active_implementation_scope"]["awaiting_review"] is True
    assert b_payload["resumed_from_run_id"] == source.name
    assert b_payload["resume_relocation"]["source_run_id"] == source.name
    case["b"] = b
    case["b_payload"] = b_payload
    return case


def _rehome_retry_bootstrap(case: dict[str, object], *, rehome_worktree: Path | None):
    return _bootstrap_resume_invocation(
        repo_root=case["root"],
        config_path=case["root"] / "aflow.toml",
        default_config_path=case["root"] / "aflow.toml",
        config_path_is_explicit=True,
        workflow_config=case["config"],
        requested_run_id=case["b"].name,
        workflow_arg=None,
        plan_file_arg=None,
        team_arg=None,
        start_step_arg=None,
        max_turns_arg=None,
        extra_instructions_arg=(),
        extra_instructions_provided=False,
        rehome_worktree=rehome_worktree,
        live_loader=lambda _path: case["config"],
    )


def _rehome_retry_detect(case: dict[str, object], bootstrap):
    return _detect_resume_candidate(
        repo_root=case["root"],
        workflow_config=case["config"],
        workflow_name="checkpoint_review",
        plan_path=case["plan"].resolve(),
        team=None,
        selected_start_step=bootstrap.start_step,
        max_turns=5,
        extra_instructions=(),
        requested_run_id=case["b"].name,
        require_resume=True,
        resume_bootstrap=bootstrap,
        allow_start_step_override=bootstrap.start_step_override,
    )


@pytest.mark.parametrize("final_checkpoint", [False, True], ids=["middle", "final"])
def test_failed_worker_advanced_checklist_rehome_reviewer_retry_ordinary(
    tmp_path: Path, final_checkpoint: bool,
):
    """An ordinary retry of a rehomed failed reviewer rebinds the worker.

    worker124 (old) -> rehome -> reviewer124 (successor) -> ordinary retry:
    both CLI acceptance stages admit the successor through the source's
    recorded provenance revalidated against the current registered roots, the
    resumed reviewer is first and references the exact historical worker
    artifact, and the retained lineage survives pruning byte-for-byte.
    """
    case = _rehome_reviewer_retry_case(tmp_path, final_checkpoint=final_checkpoint)
    root: Path = case["root"]
    plan: Path = case["plan"]
    source: Path = case["source"]
    b: Path = case["b"]
    source_before = _file_hashes(source)

    bootstrap = _rehome_retry_bootstrap(case, rehome_worktree=None)
    assert bootstrap.start_step == "review_implementation"
    assert (
        bootstrap.resume_context.failed_pending_review_step
        == "review_implementation"
    )
    # The ordinary retry carries the originally verified mapping, not a new
    # one, so a later descendant retry can rebind the same worker ancestor.
    assert (
        bootstrap.resume_context.resume_relocation
        == case["b_payload"]["resume_relocation"]
    )
    detected = _rehome_retry_detect(case, bootstrap)
    assert detected is not None
    assert detected.failed_pending_review_step == "review_implementation"
    # The same original scope is retained.
    b_scope = case["b_payload"]["active_implementation_scope"]
    scope = detected.active_implementation_scope
    assert scope is not None
    assert scope.scope_id == b_scope["scope_id"]
    assert scope.checkpoint_index == b_scope["checkpoint_index"]
    assert scope.checkpoint_name == b_scope["checkpoint_name"]

    calls: list[str] = []

    def approve_then_fail(argv, **kwargs):
        prompt = kwargs["input"]
        calls.append(prompt)
        if "Worker artifact path" in prompt:
            return subprocess.CompletedProcess(argv, 0, "approved", "")
        plan.write_text(
            _plan_with_git_tracking(_CP3_PLAN, case["base"]), encoding="utf-8"
        )
        return subprocess.CompletedProcess(argv, 124, "", "harness transport failure")

    try:
        result = run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan, max_turns=5, keep_runs=1,
                start_step="review_implementation", start_step_explicit=True,
            ),
            case["config"],
            "checkpoint_review",
            config_dir=root,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=approve_then_fail,
            resume=replace(bootstrap.resume_context, teardown=()),
        )
        c = result.run_dir
    except WorkflowError as failure:
        c = failure.run_dir

    # Reviewer-first: the resumed reviewer references the exact historical
    # failed-worker artifact; no worker ran before it.
    assert len(calls) >= 1
    assert "Worker artifact path" in calls[0]
    assert source.name in calls[0]
    assert (
        _worker_artifact_read_path(calls[0])
        == str(source / "turns" / "turn-001" / "result.json")
    )
    t1 = json.loads((c / "turns" / "turn-001" / "result.json").read_text(encoding="utf-8"))
    assert t1["step_role"] == "reviewer"
    if final_checkpoint:
        assert len(calls) == 1
    else:
        assert len(calls) == 2
        assert "Worker artifact path" not in calls[1]
    # The retained lineage survives keep_runs=1 pruning byte-for-byte.
    assert _file_hashes(source) == source_before
    assert (b / "run.json").is_file()
    c_payload = json.loads((c / "run.json").read_text(encoding="utf-8"))
    assert c_payload["resumed_from_run_id"] == b.name


@pytest.mark.parametrize("final_checkpoint", [False, True], ids=["middle", "final"])
def test_failed_worker_advanced_checklist_rehome_reviewer_retry_explicit_rehome(
    tmp_path: Path, final_checkpoint: bool,
):
    """A further rehome of the failed reviewer rebinds the worker ancestor.

    worker124 (old) -> rehome -> reviewer124 (successor) -> explicit rehome
    retry into a replacement worktree: the ancestor is bound through the
    source's recorded provenance revalidated against the replacement
    registered roots, the reviewer is first and references the exact
    historical worker artifact, and the lineage is unchanged.
    """
    case = _rehome_reviewer_retry_case(tmp_path, final_checkpoint=final_checkpoint)
    root: Path = case["root"]
    plan: Path = case["plan"]
    source: Path = case["source"]
    b: Path = case["b"]
    worktree: Path = case["worktree"]
    source_before = _file_hashes(source)

    # Register a replacement worktree on the recorded plan branch.
    _git(root, "worktree", "remove", str(worktree))
    worktree2 = root.parent / "failed-worker-retry-worktree"
    _git(root, "worktree", "add", str(worktree2), _REHOME_BRANCH)

    bootstrap = _rehome_retry_bootstrap(case, rehome_worktree=worktree2)
    assert bootstrap.start_step == "review_implementation"
    assert (
        bootstrap.resume_context.resume_relocation["current_worktree_root"]
        == str(worktree2)
    )
    detected = _rehome_retry_detect(case, bootstrap)
    assert detected is not None
    assert detected.failed_pending_review_step == "review_implementation"

    calls: list[str] = []

    def approve_then_fail(argv, **kwargs):
        prompt = kwargs["input"]
        calls.append(prompt)
        if "Worker artifact path" in prompt:
            return subprocess.CompletedProcess(argv, 0, "approved", "")
        plan.write_text(
            _plan_with_git_tracking(_CP3_PLAN, case["base"]), encoding="utf-8"
        )
        return subprocess.CompletedProcess(argv, 124, "", "harness transport failure")

    try:
        result = run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan, max_turns=5, keep_runs=1,
                start_step="review_implementation", start_step_explicit=True,
            ),
            case["config"],
            "checkpoint_review",
            config_dir=root,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=approve_then_fail,
            resume=replace(bootstrap.resume_context, teardown=()),
        )
        c = result.run_dir
    except WorkflowError as failure:
        c = failure.run_dir

    assert "Worker artifact path" in calls[0]
    assert source.name in calls[0]
    assert (
        _worker_artifact_read_path(calls[0])
        == str(source / "turns" / "turn-001" / "result.json")
    )
    if final_checkpoint:
        assert len(calls) == 1
    else:
        assert len(calls) == 2
        assert "Worker artifact path" not in calls[1]
    assert _file_hashes(source) == source_before
    assert (b / "run.json").is_file()
    c_payload = json.loads((c / "run.json").read_text(encoding="utf-8"))
    assert c_payload["resumed_from_run_id"] == b.name


def test_failed_worker_advanced_checklist_rehome_reviewer_retry_after_second_failure(
    tmp_path: Path,
):
    """A supported retry after a second reviewer failure references the worker.

    worker124 (old) -> rehome -> reviewer124 -> retry -> reviewer124 again:
    the originally verified mapping is preserved through the descendant, so
    the next supported retry still references the same worker ancestor and
    the whole retained lineage survives pruning.
    """
    case = _rehome_reviewer_retry_case(tmp_path, final_checkpoint=False)
    root: Path = case["root"]
    plan: Path = case["plan"]
    source: Path = case["source"]
    b: Path = case["b"]
    source_before = _file_hashes(source)

    bootstrap = _rehome_retry_bootstrap(case, rehome_worktree=None)
    detected = _rehome_retry_detect(case, bootstrap)
    assert detected is not None

    def reviewer_fail(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 124, "", "harness transport failure")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan, max_turns=5, keep_runs=1,
                start_step="review_implementation", start_step_explicit=True,
            ),
            case["config"],
            "checkpoint_review",
            config_dir=root,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=reviewer_fail,
            resume=replace(bootstrap.resume_context, teardown=()),
        )
    c = failure.value.run_dir
    c_payload = json.loads((c / "run.json").read_text(encoding="utf-8"))
    assert c_payload["status"] == "failed"
    assert c_payload["resumed_from_run_id"] == b.name
    # The originally verified mapping is preserved through the descendant.
    assert c_payload["resume_relocation"] == case["b_payload"]["resume_relocation"]

    case["b"] = c
    case["b_payload"] = c_payload
    bootstrap2 = _rehome_retry_bootstrap(case, rehome_worktree=None)
    assert bootstrap2.start_step == "review_implementation"
    assert (
        bootstrap2.resume_context.failed_pending_review_step
        == "review_implementation"
    )
    detected2 = _rehome_retry_detect(case, bootstrap2)
    assert detected2 is not None

    calls: list[str] = []

    def approve_then_fail(argv, **kwargs):
        prompt = kwargs["input"]
        calls.append(prompt)
        if "Worker artifact path" in prompt:
            return subprocess.CompletedProcess(argv, 0, "approved", "")
        plan.write_text(
            _plan_with_git_tracking(_CP3_PLAN, case["base"]), encoding="utf-8"
        )
        return subprocess.CompletedProcess(argv, 124, "", "harness transport failure")

    try:
        result = run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan, max_turns=5, keep_runs=1,
                start_step="review_implementation", start_step_explicit=True,
            ),
            case["config"],
            "checkpoint_review",
            config_dir=root,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=approve_then_fail,
            resume=replace(bootstrap2.resume_context, teardown=()),
        )
        d = result.run_dir
    except WorkflowError as failure2:
        d = failure2.run_dir

    assert "Worker artifact path" in calls[0]
    assert source.name in calls[0]
    assert (
        _worker_artifact_read_path(calls[0])
        == str(source / "turns" / "turn-001" / "result.json")
    )
    t1 = json.loads((d / "turns" / "turn-001" / "result.json").read_text(encoding="utf-8"))
    assert t1["step_role"] == "reviewer"
    assert _file_hashes(source) == source_before
    assert (b / "run.json").is_file()
    assert (c / "run.json").is_file()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_active_plan",
        "foreign_active_plan",
        "overlay_active_plan",
        "contradictory_ancestor_scope_plan",
        "stale_source_roots",
    ],
)
def test_failed_worker_advanced_checklist_rehome_reviewer_retry_refusals(
    tmp_path: Path, mutation: str,
):
    """Retrying a rehomed failed reviewer never broadens the identity grant.

    Missing, external, overlay, contradictory-ancestor, or stale-root
    identities refuse the retry bootstrap before any allocation, leaving the
    retained lineage byte-for-byte unchanged.
    """
    case = _rehome_reviewer_retry_case(tmp_path, final_checkpoint=False)
    root: Path = case["root"]
    source: Path = case["source"]
    b: Path = case["b"]

    if mutation in {
        "missing_active_plan",
        "foreign_active_plan",
        "overlay_active_plan",
    }:
        result_path = b / "turns" / "turn-001" / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if mutation == "missing_active_plan":
            result["active_plan_path"] = None
        elif mutation == "foreign_active_plan":
            result["active_plan_path"] = str(root.parent / "foreign-plan.md")
        else:
            result["active_plan_path"] = str(root / "plan-cp01-v01.md")
        result_path.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    elif mutation == "contradictory_ancestor_scope_plan":
        a_payload = json.loads((source / "run.json").read_text(encoding="utf-8"))
        a_payload["active_implementation_scope"]["original_plan_path"] = str(
            root.parent / "old-failed-worker-repo" / "other.md"
        )
        (source / "run.json").write_text(
            json.dumps(a_payload, sort_keys=True) + "\n", encoding="utf-8"
        )
    else:
        b_payload = json.loads((b / "run.json").read_text(encoding="utf-8"))
        b_payload["worktree_path"] = str(root.parent / "ghost-worktree")
        (b / "run.json").write_text(
            json.dumps(b_payload, sort_keys=True) + "\n", encoding="utf-8"
        )

    # The mutation itself is the invalid evidence under test; the refusal
    # must leave the lineage exactly as mutated, with no successor.
    mutated_source = _file_hashes(source)
    mutated_b = _file_hashes(b)

    with pytest.raises(ValueError):
        _rehome_retry_bootstrap(case, rehome_worktree=None)

    # No successor was allocated and the lineage is byte-for-byte unchanged
    # by the refused bootstrap.
    run_ids = {p.name for p in (root / ".aflow" / "runs").iterdir()}
    assert run_ids == {source.name, b.name}
    assert _file_hashes(source) == mutated_source
    assert _file_hashes(b) == mutated_b


def test_failed_worker_advanced_checklist_rehome_strict_binder_restores_ancestor_mapping(
    tmp_path: Path,
):
    """The strict binder rebinds the rehomed worker ancestor directly.

    The verified ancestor relocation is restored from the source's recorded
    provenance and revalidated against the current registered roots; the
    bound owner and its validated physical dependencies are published.
    """
    case = _rehome_reviewer_retry_case(tmp_path, final_checkpoint=False)
    root: Path = case["root"]
    source: Path = case["source"]
    b: Path = case["b"]
    worktree: Path = case["worktree"]
    b_payload = case["b_payload"]

    relocation = _resume_relocation_from_provenance(
        b_payload.get("resume_relocation")
    )
    assert relocation is not None
    scope_data = b_payload["active_implementation_scope"]
    attempts = b_payload["implementation_attempts"][scope_data["scope_id"]]
    worker_attempts = [a for a in attempts if a.get("role") == "worker"]
    assert worker_attempts
    last_worker = worker_attempts[-1]
    envelope_values = (
        scope_data.get("envelope_artifact_path"),
        scope_data.get("envelope_artifact_sha256"),
        scope_data.get("envelope_canonical_sha256"),
    )
    scope_envelope = (
        tuple(envelope_values)  # type: ignore[arg-type]
        if all(isinstance(v, str) and v for v in envelope_values)
        else None
    )
    deps: set[str] = set()
    bound = _bind_worker_receipt(
        root,
        b.name,
        last_worker["turn_number"],
        scope_id=scope_data["scope_id"],
        step_name=last_worker.get("step_name"),
        attempt_ordinal=last_worker.get("attempt_ordinal"),
        scope_checkpoint_index=scope_data.get("checkpoint_index"),
        scope_checkpoint_name=scope_data.get("checkpoint_name"),
        scope_original_plan=scope_data.get("original_plan_path"),
        scope_envelope=scope_envelope,
        strict=True,
        required_run_ids=deps,
        relocation=relocation,
        current_worktree_root=worktree,
    )
    assert bound == (source.name, source / "turns" / "turn-001" / "result.json")
    assert deps == {b.name, source.name}


def _team_upgrade_workflow_config() -> WorkflowUserConfig:
    """A checkpoint-review workflow on team ``base`` with a zero-repair upgrade.

    ``base.upgrade_to=strong`` and ``upgrade_after_repairs=0``: any scoped
    repair after a rejection must run on ``strong``.  The recovered failed
    worker attempt must preserve the source's executed team so this policy
    applies.
    """
    return WorkflowUserConfig(
        aflow=AflowSection(max_turns=5),
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={
                    "worker-base": HarnessProfileConfig(model="worker-base"),
                    "worker-strong": HarnessProfileConfig(model="worker-strong"),
                    "reviewer": HarnessProfileConfig(model="reviewer"),
                }
            )
        },
        teams={
            "base": TeamConfig(
                roles={"worker": "codex.worker-base", "reviewer": "codex.reviewer"},
                upgrade_to="strong",
            ),
            "strong": TeamConfig(
                roles={"worker": "codex.worker-strong"},
                extends="base",
            ),
        },
        workflows={
            "checkpoint_review": WorkflowConfig(
                team="base",
                upgrade_after_repairs=0,
                steps={
                    "implement_plan": WorkflowStepConfig(
                        role="worker",
                        prompts=("implement",),
                        go=(GoTransition(to="review_implementation"),),
                    ),
                    "review_implementation": WorkflowStepConfig(
                        role="reviewer",
                        prompts=("review",),
                        go=(
                            GoTransition(to="END", when="DONE && !NEW_PLAN_EXISTS"),
                            GoTransition(to="implement_plan", when="NEW_PLAN_EXISTS"),
                            GoTransition(to="implement_plan"),
                        ),
                    ),
                },
                first_step="implement_plan",
            )
        },
        prompts={
            "implement": "Implement from {ACTIVE_PLAN_PATH}.",
            "review": "Review {ORIGINAL_PLAN_PATH}.",
        },
    )


def test_failed_worker_advanced_checklist_recovery_preserves_team(tmp_path: Path):
    """The recovered worker attempt preserves the source's executed team.

    A transport-failed worker turn records no durable attempt of its own; the
    recovery carries a truthful non-accepted attempt whose team is the source
    run's historical executed team (not ``None``), so scoped repair applies the
    configured team routing and any configured upgrade.
    """
    config = _team_upgrade_workflow_config()
    case = _make_failed_worker_run(tmp_path, config, "checkpoint_review")
    context = _resume_context_review(case)

    scope = context.active_implementation_scope
    assert scope is not None
    attempts = context.implementation_attempts.get(scope.scope_id, [])
    worker_attempts = [a for a in attempts if a.role == "worker"]
    assert len(worker_attempts) == 1
    assert worker_attempts[0].team == "base"
    assert worker_attempts[0].selector == "codex.worker-base"


def test_failed_worker_advanced_checklist_team_upgrade_after_recovery(
    tmp_path: Path,
):
    """A post-recovery rejection repairs on the upgraded team, not the default.

    With ``upgrade_after_repairs=0`` and ``base.upgrade_to=strong``, a reviewer
    rejection of the retained scope must launch the ``strong`` worker
    (``codex.worker-strong``) rather than falling back to the base/default
    worker.
    """
    config = _team_upgrade_workflow_config()
    case = _make_failed_worker_run(tmp_path, config, "checkpoint_review")
    context = _resume_context_review(case)
    overlay = case["plan"].with_name("plan-cp01-v01.md")

    def reject_then_stop(argv, **kwargs):
        if "Worker artifact path" in kwargs["input"]:
            # Reject the retained scope; the repair worker must be ``strong``.
            overlay.write_text(
                "# Follow-up\n\n### [ ] Checkpoint 1: Repair\n- [ ] repair\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                argv, 0, "rejected: repair needed", ""
            )
        # The repair worker; the fixture stops it after the turn is recorded.
        return subprocess.CompletedProcess(argv, 1, "", "worker stopped for fixture")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(
                repo_root=case["root"],
                plan_path=case["plan"],
                max_turns=5,
                keep_runs=1,
                start_step="review_implementation",
                start_step_explicit=True,
            ),
            config,
            "checkpoint_review",
            config_dir=case["root"],
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=reject_then_stop,
            resume=context,
        )
    b = failure.value.run_dir
    # The rejection produced a repair worker on the upgraded team: the turn
    # receipt records the strong selector, not the base/default worker.
    repair_turn = json.loads(
        (b / "turns" / "turn-002" / "result.json").read_text(encoding="utf-8")
    )
    assert repair_turn["step_role"] == "worker"
    assert repair_turn.get("selector") == "codex.worker-strong"


def _prepare_worker_workflow_config() -> WorkflowUserConfig:
    """A checkpoint-review workflow with a preceding prepare worker step.

    ``prepare_worker`` runs first and returns 0 without completing the
    checkpoint, recording a durable scope-local attempt (ordinal 1).  Its
    configured successor ``implement_plan`` is the worker whose success
    transition requires the checkpoint reviewer; when it advances the
    checklist and transport-fails, the recovery must extend the retained
    history and allocate the next scope-local ordinal.
    """
    return WorkflowUserConfig(
        aflow=AflowSection(max_turns=5),
        roles={"worker": "codex.default", "reviewer": "codex.default"},
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={"default": HarnessProfileConfig(model="fixture")}
            )
        },
        workflows={
            "prepare_review": WorkflowConfig(
                steps={
                    "prepare_worker": WorkflowStepConfig(
                        role="worker",
                        prompts=("prepare",),
                        go=(GoTransition(to="implement_plan"),),
                    ),
                    "implement_plan": WorkflowStepConfig(
                        role="worker",
                        prompts=("implement",),
                        go=(GoTransition(to="review_implementation"),),
                    ),
                    "review_implementation": WorkflowStepConfig(
                        role="reviewer",
                        prompts=("review",),
                        go=(
                            GoTransition(to="END", when="DONE && !NEW_PLAN_EXISTS"),
                            GoTransition(to="implement_plan", when="NEW_PLAN_EXISTS"),
                            GoTransition(to="implement_plan"),
                        ),
                    ),
                },
                first_step="prepare_worker",
            )
        },
        prompts={
            "prepare": "Prepare from {ACTIVE_PLAN_PATH}.",
            "implement": "Implement from {ACTIVE_PLAN_PATH}.",
            "review": "Review {ORIGINAL_PLAN_PATH}.",
        },
    )


def _make_prepare_worker_run(tmp_path: Path) -> dict[str, object]:
    """A prepare worker (ordinal 1) followed by a failed checklist-advancing worker.

    ``prepare_worker`` returns 0 without completing the checkpoint, then
    ``implement_plan`` advances Checkpoint 1 and transport-fails (124).  The
    source stays failed with the checkpoint scope open and one retained
    scope-local attempt (the prepare worker, ordinal 1).
    """
    config = _prepare_worker_workflow_config()
    root = tmp_path / "repo"
    _make_git_repo(root)
    (root / "aflow.toml").write_text("", encoding="utf-8")
    plan_path = root / "plan.md"
    plan_path.write_text(_INITIAL_PLAN, encoding="utf-8")
    calls = {"n": 0}

    def runner(argv, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return subprocess.CompletedProcess(argv, 0, "prepared", "")
        plan_path.write_text(_CP2_PLAN, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 124, "", "harness transport failure")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(repo_root=root, plan_path=plan_path, max_turns=5),
            config,
            "prepare_review",
            config_dir=root,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=runner,
        )
    source_run = failure.value.run_dir
    assert source_run is not None
    payload = json.loads((source_run / "run.json").read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["last_snapshot"]["current_checkpoint_index"] == 2
    scope = payload["active_implementation_scope"]
    assert scope["checkpoint_index"] == 1
    assert scope["awaiting_review"] is not True
    # The prepare worker recorded exactly one durable attempt (ordinal 1); the
    # transport-failed implement_plan worker recorded no attempt of its own.
    attempts = payload["implementation_attempts"].get(scope["scope_id"], [])
    assert [a["attempt_ordinal"] for a in attempts] == [1]
    assert attempts[0]["step_name"] == "prepare_worker"
    return {
        "root": root,
        "plan": plan_path,
        "config": config,
        "source": source_run,
        "payload": payload,
        "source_hashes": _file_hashes(source_run),
    }


def _resume_context_prepare(case: dict[str, object], payload: dict[str, object] | None = None):
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
        workflow_steps=config.workflows["prepare_review"].steps,
        effective_max_turns=5,
    )


def test_failed_worker_advanced_checklist_retained_history_recovered_ordinal(
    tmp_path: Path,
):
    """A recovered failed worker appends after the retained scope history.

    The prepare worker leaves a durable attempt (ordinal 1) under the scope.
    The recovery must extend the decoded immutable tuple history into a fresh
    list, preserve the prior attempt, and allocate the next scope-local ordinal
    (2) without crashing bootstrap, the second admission, or reconstruction.
    """
    case = _make_prepare_worker_run(tmp_path)
    source = case["source"]
    config = case["config"]
    scope_id = case["payload"]["active_implementation_scope"]["scope_id"]

    # Stage 1: bootstrap does not crash and recovers ordinal 2.
    bootstrap = _bootstrap_resume_invocation(
        repo_root=case["root"],
        config_path=case["root"] / "aflow.toml",
        default_config_path=case["root"] / "aflow.toml",
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
        live_loader=lambda _path: config,
    )
    review = bootstrap.resume_context.failed_worker_review
    assert review is not None
    assert review.reviewer_step_name == "review_implementation"
    assert review.worker_step_name == "implement_plan"
    assert review.worker_turn_number == 2
    assert review.worker_returncode == 124
    assert bootstrap.start_step == "review_implementation"
    assert bootstrap.start_step_override is True
    recovered = bootstrap.resume_context.implementation_attempts.get(scope_id, [])
    assert [a.attempt_ordinal for a in recovered if a.role == "worker"] == [1, 2]
    assert recovered[0].step_name == "prepare_worker"
    assert recovered[0].turn_number == 1
    assert recovered[1].step_name == "implement_plan"
    assert recovered[1].turn_number == 2

    # Stage 2: the resume-candidate detector (second admission) agrees.
    detected = _detect_resume_candidate(
        repo_root=case["root"],
        workflow_config=config,
        workflow_name="prepare_review",
        plan_path=case["plan"].resolve(),
        team=None,
        selected_start_step=None,
        max_turns=5,
        extra_instructions=(),
        requested_run_id=source.name,
        require_resume=True,
        resume_bootstrap=bootstrap,
        allow_start_step_override=bootstrap.start_step_override,
    )
    assert detected is not None
    assert detected.failed_worker_review is not None
    detected_attempts = detected.implementation_attempts.get(scope_id, [])
    assert [a.attempt_ordinal for a in detected_attempts if a.role == "worker"] == [1, 2]

    # Reconstruction does not crash and preserves the retained history.
    context = _resume_context_prepare(case)
    assert context.failed_worker_review is not None
    context_attempts = context.implementation_attempts.get(scope_id, [])
    assert [a.attempt_ordinal for a in context_attempts if a.role == "worker"] == [1, 2]

    # Resumed execution: the first provider call is the reviewer referencing
    # the failed turn 2; no next worker runs first.
    calls: list[str] = []

    def reviewer(argv, **kwargs):
        calls.append(kwargs["input"])
        if "Worker artifact path (read this exact file): " not in kwargs["input"]:
            return subprocess.CompletedProcess(argv, 1, "", "worker must not run first")
        return subprocess.CompletedProcess(argv, 0, "approved", "")

    with pytest.raises(WorkflowError):
        run_workflow(
            ControllerConfig(
                repo_root=case["root"],
                plan_path=case["plan"],
                max_turns=5,
                keep_runs=1,
                start_step="review_implementation",
                start_step_explicit=True,
            ),
            config,
            "prepare_review",
            config_dir=case["root"],
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=reviewer,
            resume=context,
        )
    assert "returncode 124" in calls[0]
    assert "turn 2" in calls[0]
    assert _file_hashes(source) == case["source_hashes"]


def test_failed_worker_advanced_checklist_multiple_retained_ordinals(
    tmp_path: Path,
):
    """The recovered ordinal is allocated after multiple retained attempts.

    With two retained scope-local attempts (ordinals 1 and 2), the recovered
    failed-worker attempt must take the next ordinal (3), not restart at 1.
    """
    case = _make_prepare_worker_run(tmp_path)
    scope_id = case["payload"]["active_implementation_scope"]["scope_id"]
    payload = json.loads(json.dumps(case["payload"]))
    first = payload["implementation_attempts"][scope_id][0]
    extra = dict(first)
    extra["turn_number"] = 2
    extra["step_name"] = "implement_plan"
    extra["attempt_ordinal"] = 2
    extra["outcome"] = "progress"
    payload["implementation_attempts"][scope_id].append(extra)

    context = _resume_context_prepare(case, payload)
    assert context.failed_worker_review is not None
    recovered = context.implementation_attempts.get(scope_id, [])
    assert [a.attempt_ordinal for a in recovered if a.role == "worker"] == [1, 2, 3]
    assert recovered[-1].turn_number == 2
    assert recovered[-1].step_name == "implement_plan"


def test_failed_worker_advanced_checklist_retained_history_rejection_repair_retry(
    tmp_path: Path,
):
    """Retained-history source through rejection, repair success, reviewer124.

    The prepare worker leaves ordinal 1 and recovery adds the failed worker as
    ordinal 2.  A reviewer rejection references ordinal 2; the same-checkpoint
    repair worker succeeds on ordinal 3; a failed reviewer (124) resumes and
    binds the new successful local repair (successor turn 2), not the old
    failed ancestor.  The original scope and evidence survive keep_runs=1.
    """
    config = _prepare_worker_workflow_config()
    case = _make_prepare_worker_run(tmp_path)
    source = case["source"]
    context = _resume_context_prepare(case)
    scope_id = case["payload"]["active_implementation_scope"]["scope_id"]
    overlay = case["plan"].with_name("plan-cp01-v01.md")

    calls_b: list[str] = []
    overlay_complete = (
        "# Follow-up\n\n### [x] Checkpoint 1: Repair\n- [x] repair\n"
    )

    def reject_repair_then_fail(argv, **kwargs):
        n = len(calls_b)
        calls_b.append(kwargs["input"])
        if n == 0:
            # First reviewer: reject the retained scope (references ordinal 2).
            overlay.write_text(
                "# Follow-up\n\n### [ ] Checkpoint 1: Repair\n- [ ] repair\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "rejected: repair needed", "")
        if n == 1:
            # Repair worker: complete the checkpoint successfully.
            overlay.write_text(overlay_complete, encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "repaired", "")
        # Second reviewer: transport failure.
        return subprocess.CompletedProcess(argv, 124, "", "harness transport failure")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(
                repo_root=case["root"],
                plan_path=case["plan"],
                max_turns=5,
                keep_runs=1,
                start_step="review_implementation",
                start_step_explicit=True,
            ),
            config,
            "prepare_review",
            config_dir=case["root"],
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=reject_repair_then_fail,
            resume=context,
        )
    b = failure.value.run_dir
    b_payload = json.loads((b / "run.json").read_text(encoding="utf-8"))
    assert b_payload["status"] == "failed"
    assert b_payload["active_implementation_scope"]["awaiting_review"] is True
    # The rejection references the recovered failed-worker ordinal (2).
    review_turn = json.loads((b / "turns" / "turn-001" / "result.json").read_text())
    assert review_turn["step_role"] == "reviewer"
    assert review_turn.get("review_rejection") is not None
    rejection = review_turn["review_rejection"]
    assert rejection.get("reviewed_attempt_ordinal") == 2
    # The successful repair worker received the next ordinal (3).
    b_attempts = b_payload["implementation_attempts"].get(scope_id, [])
    worker_ordinals = [
        a["attempt_ordinal"] for a in b_attempts if a.get("role") == "worker"
    ]
    assert worker_ordinals == [1, 2, 3]
    repair_turn = json.loads((b / "turns" / "turn-002" / "result.json").read_text())
    assert repair_turn["step_role"] == "worker"
    assert repair_turn.get("status") == "completed"

    # Resume the failed reviewer; admission/prompt binding selects the new
    # successful local repair (B turn 2), not the old failed ancestor (A).
    b_result_bytes = (b / "turns" / "turn-002" / "result.json").read_bytes()
    calls_c: list[str] = []

    def approve(argv, **kwargs):
        calls_c.append(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0, "approved", "")

    retry_context = _reconstruct_resume_context(
        resolved_run_id=Path(b.name),
        run_dir=b,
        prev_run=b_payload,
        plan_path=case["plan"].resolve(),
        frozen_run_identity=None,
        reset_scope=False,
        require_resume=True,
        workflow_steps=config.workflows["prepare_review"].steps,
        effective_max_turns=5,
    )
    with pytest.raises(WorkflowError):
        run_workflow(
            ControllerConfig(
                repo_root=case["root"],
                plan_path=case["plan"],
                max_turns=5,
                keep_runs=1,
                start_step="review_implementation",
                start_step_explicit=True,
            ),
            config,
            "prepare_review",
            config_dir=case["root"],
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=approve,
            resume=retry_context,
        )
    # The resumed reviewer prompt references the successful repair worker's
    # receipt (B turn 2) and not the original failed ancestor run directory.
    assert b.name in calls_c[0]
    assert source.name not in calls_c[0]
    # The retained successful repair receipt survives pruning byte-for-byte.
    assert (b / "turns" / "turn-002" / "result.json").read_bytes() == b_result_bytes
