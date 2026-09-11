from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
import subprocess

import pytest

from aflow.cli import (
    ResumeScopeReconciliationError,
    _bootstrap_resume_invocation,
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
from aflow.harnesses.preflight import NoOpHarnessPreflightProbe
from aflow.run_state import ControllerConfig
from aflow.workflow import WorkflowError, run_workflow
from tests._support import _make_git_repo


_INITIAL_PLAN = """# Cumulative plan

### [ ] Checkpoint 1: First
- [ ] first

### [ ] Checkpoint 2: Second
- [ ] second-a
- [ ] second-b

### [ ] Checkpoint 3: Third
- [ ] third
"""
_CP2_PLAN = _INITIAL_PLAN.replace(
    "### [ ] Checkpoint 1: First\n- [ ] first",
    "### [x] Checkpoint 1: First\n- [x] first",
)
_COMPLETE_PLAN = _CP2_PLAN.replace(
    "### [ ] Checkpoint 2: Second\n- [ ] second-a\n- [ ] second-b",
    "### [x] Checkpoint 2: Second\n- [x] second-a\n- [x] second-b",
).replace(
    "### [ ] Checkpoint 3: Third\n- [ ] third",
    "### [x] Checkpoint 3: Third\n- [x] third",
)


def _workflow_config() -> WorkflowUserConfig:
    return WorkflowUserConfig(
        aflow=AflowSection(max_turns=5),
        roles={"worker": "codex.default", "reviewer": "codex.default"},
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
                        prompts=("implement",),
                        go=(
                            GoTransition(
                                to="review_implementation",
                                when="DONE",
                                preserve_active_plan=True,
                            ),
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


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args), cwd=root, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _commit_plan(root: Path, message: str) -> None:
    subprocess.run(
        ("git", "add", "aflow.toml", "plan.md"),
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-m",
            message,
        ),
        cwd=root,
        check=True,
        capture_output=True,
    )


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


class _BlockAfterWorkers(NoOpHarnessPreflightProbe):
    def __init__(self) -> None:
        self.calls = 0

    def resolve_executable(self, command: str, *, env):
        self.calls += 1
        return command if self.calls <= 2 else None


@pytest.fixture
def pending_review_run(tmp_path: Path) -> dict[str, object]:
    root = tmp_path / "repo"
    _make_git_repo(root)
    (root / "aflow.toml").write_text("", encoding="utf-8")
    plan = root / "plan.md"
    plan.write_text(_INITIAL_PLAN, encoding="utf-8")
    _commit_plan(root, "fixture cumulative plan")
    config = _workflow_config()
    calls: list[dict[str, object]] = []

    def runner(argv, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            plan.write_text(_CP2_PLAN, encoding="utf-8")
            _commit_plan(root, "fixture checkpoint 1")
        else:
            plan.write_text(_COMPLETE_PLAN, encoding="utf-8")
            _commit_plan(root, "fixture cumulative implementation")
        return subprocess.CompletedProcess(argv, 0, "completed", "")

    with pytest.raises(WorkflowError) as failure:
        run_workflow(
            ControllerConfig(repo_root=root, plan_path=plan, max_turns=5),
            config,
            "cumulative",
            config_dir=root,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=runner,
            preflight_probe=_BlockAfterWorkers(),
        )
    source = failure.value.run_dir
    assert source is not None
    source_payload = json.loads((source / "run.json").read_text(encoding="utf-8"))
    assert len(calls) == 2
    assert source_payload["status"] == "failed"
    assert source_payload["last_snapshot"]["is_complete"] is True
    assert source_payload["active_implementation_scope"]["awaiting_review"] is True

    units = source / "units"
    units.mkdir()
    (units / "start.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "run_id": source.name,
                "unit": f"aflow-run-{source.name}.service",
                "nonce": "fixture-nonce",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (units / "exit.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "run_id": source.name,
                "nonce": "fixture-nonce",
                "returncode": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    source_payload["workspace_state"] = {
        "branch": _git(root, "symbolic-ref", "--short", "HEAD"),
        "head": _git(root, "rev-parse", "HEAD"),
        "dirty_worktree": "",
    }
    (source / "run.json").write_text(
        json.dumps(source_payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    source_payload = json.loads((source / "run.json").read_text(encoding="utf-8"))
    return {
        "root": root,
        "plan": plan,
        "config": config,
        "source": source,
        "payload": source_payload,
        "source_hashes": _file_hashes(source),
    }


def _resume_context(case: dict[str, object]):
    config = case["config"]
    source = case["source"]
    return _reconstruct_resume_context(
        resolved_run_id=Path(source.name),
        run_dir=source,
        prev_run=case["payload"],
        plan_path=case["plan"].resolve(),
        frozen_run_identity=None,
        reset_scope=False,
        require_resume=True,
        workflow_steps=config.workflows["cumulative"].steps,
        effective_max_turns=5,
    )


def _prepare_relocated_pending_review(case: dict[str, object], *, overlay: bool = False):
    root = case["root"]
    plan = case["plan"]
    source = case["source"]
    _git(root, "branch", "-M", "main")
    base = _git(root, "rev-parse", "HEAD")
    plan_text = plan.read_text(encoding="utf-8")
    assert "# Cumulative plan\n\n" in plan_text
    plan.write_text(
        plan_text.replace(
            "# Cumulative plan\n\n",
            "# Cumulative plan\n\n"
            "## Git Tracking\n\n"
            "- Plan Branch: `feature/pending-review`\n"
            f"- Pre-Handoff Base HEAD: `{base}`\n\n",
            1,
        ),
        encoding="utf-8",
    )
    _git(root, "add", "plan.md")
    _git(root, "commit", "-m", "fixture linked worktree metadata")
    worktree = root.parent / "pending-review-worktree"
    _git(root, "worktree", "add", "-b", "feature/pending-review", str(worktree))

    old_repo = root.parent / "source-repo"
    old_worktree = root.parent / "source-worktree"
    result_path = source / "turns" / "turn-002" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    new_plan_name = Path(result["new_plan_path"]).name
    result["original_plan_path"] = str(
        root.parent / "unrelated-overlay.md" if overlay else old_repo / "plan.md"
    )
    result["active_plan_path"] = str(old_worktree / "plan.md")
    result["new_plan_path"] = str(old_worktree / new_plan_name)
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
            "feature_branch": "feature/pending-review",
            "main_branch": "main",
            "lifecycle_setup": ["worktree", "branch"],
            "lifecycle_teardown": ["merge", "rm_worktree"],
            "workspace_state": {
                "branch": "feature/pending-review",
                "head": _git(worktree, "rev-parse", "HEAD"),
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
    return root, plan, source, worktree, old_repo, _file_hashes(source)


def _relocated_pending_review_bootstrap(case: dict[str, object], worktree: Path):
    root = case["root"]
    config = case["config"]
    return _bootstrap_resume_invocation(
        repo_root=root,
        config_path=root / "aflow.toml",
        default_config_path=root / "aflow.toml",
        config_path_is_explicit=True,
        workflow_config=config,
        requested_run_id=case["source"].name,
        workflow_arg=None,
        plan_file_arg=None,
        team_arg=None,
        start_step_arg=None,
        max_turns_arg=None,
        extra_instructions_arg=(),
        extra_instructions_provided=False,
        rehome_worktree=worktree,
        live_loader=lambda _path: config,
    )


def test_exact_stale_scope_and_finished_worker_classify_as_pending_review(
    pending_review_run,
):
    case = pending_review_run
    context = _resume_context(case)

    assert context.pending_cumulative_review is not None
    assert context.pending_cumulative_review.reviewer_step_name == "review_implementation"
    assert context.pending_cumulative_review.snapshot_before.current_checkpoint_index == 2
    assert context.interrupted_step_name == "review_implementation"
    assert context.active_implementation_scope is not None
    assert context.active_implementation_scope.checkpoint_index == 2
    assert context.active_implementation_scope.awaiting_review is True
    assert _file_hashes(case["source"]) == case["source_hashes"]


def test_relocated_pending_review_maps_worker_receipts_and_reviews_first(
    pending_review_run,
):
    case = pending_review_run
    root, plan, source, worktree, old_repo, before = _prepare_relocated_pending_review(case)
    bootstrap = _relocated_pending_review_bootstrap(case, worktree)

    assert bootstrap.start_step == "review_implementation"
    assert bootstrap.resume_context.resume_relocation["current_worktree_root"] == str(worktree)
    (root / ".aflow-config-pair.lock").unlink(missing_ok=True)
    calls: list[str] = []

    def reviewer(argv, **kwargs):
        calls.append(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0, "approved", "")

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
        "cumulative",
        config_dir=root,
        snapshot_config=False,
        adapter=CodexAdapter(),
        runner=reviewer,
        resume=replace(bootstrap.resume_context, teardown=()),
    )

    assert len(calls) == 1
    assert "complete original plan and all accumulated implementation commits" in calls[0]
    review_turn = json.loads(
        (result.run_dir / "turns" / "turn-001" / "result.json").read_text(
            encoding="utf-8"
        )
    )
    assert review_turn["step_role"] == "reviewer"
    assert review_turn["step_name"] == "review_implementation"
    source_result = json.loads(
        (source / "turns" / "turn-002" / "result.json").read_text(encoding="utf-8")
    )
    assert source_result["original_plan_path"] == str(old_repo / "plan.md")
    assert _file_hashes(source) == before


def test_relocated_pending_review_rejects_unmapped_overlay_worker_path(
    pending_review_run,
):
    case = pending_review_run
    _root, _plan, source, worktree, _old_repo, before = _prepare_relocated_pending_review(
        case, overlay=True
    )

    with pytest.raises(ValueError, match="pending cumulative review evidence is invalid"):
        _relocated_pending_review_bootstrap(case, worktree)

    assert _file_hashes(source) == before


def test_pending_review_approval_launches_one_reviewer_and_no_worker(
    pending_review_run,
):
    case = pending_review_run
    context = _resume_context(case)
    calls: list[str] = []

    def reviewer(argv, **kwargs):
        calls.append(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0, "approved", "")

    result = run_workflow(
        ControllerConfig(
            repo_root=case["root"],
            plan_path=case["plan"],
            max_turns=5,
            keep_runs=1,
            start_step="review_implementation",
            start_step_explicit=True,
        ),
        case["config"],
        "cumulative",
        config_dir=case["root"],
        snapshot_config=False,
        adapter=CodexAdapter(),
        runner=reviewer,
        resume=context,
    )

    assert len(calls) == 1
    assert "complete original plan and all accumulated implementation commits" in calls[0]
    assert "checked boxes are not approval" in calls[0]
    result_turn = json.loads(
        (result.run_dir / "turns" / "turn-001" / "result.json").read_text(
            encoding="utf-8"
        )
    )
    assert result_turn["step_role"] == "reviewer"
    assert result_turn["step_name"] == "review_implementation"
    assert _file_hashes(case["source"]) == case["source_hashes"]


def test_bootstrap_seeds_the_configured_reviewer_step(
    pending_review_run,
):
    case = pending_review_run
    bootstrap = _bootstrap_resume_invocation(
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

    assert bootstrap.start_step == "review_implementation"
    assert bootstrap.start_step_override is True
    assert bootstrap.resume_context.pending_cumulative_review is not None


def test_pending_review_rejection_preserves_overlay_and_normal_rework_flow(
    pending_review_run,
):
    case = pending_review_run
    context = _resume_context(case)
    calls: list[str] = []
    overlay = case["plan"].with_name("plan-cp01-v01.md")

    def reject_then_fail(argv, **kwargs):
        calls.append(kwargs["input"])
        if len(calls) == 1:
            overlay.write_text(
                "# Follow-up\n\n### [ ] Checkpoint 2: Repair\n- [ ] repair\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "rejected: repair needed", "")
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
            "cumulative",
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
        (successor / "turns" / "turn-001" / "result.json").read_text(
            encoding="utf-8"
        )
    )
    assert review_turn["step_role"] == "reviewer"
    assert review_turn["review_rejection"] is not None
    assert review_turn["new_plan_path"].endswith("plan-cp01-v01.md")
    assert overlay.is_file()
    assert _file_hashes(case["source"]) == case["source_hashes"]


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_exit",
        "wrong_before",
        "wrong_owner",
        "active_owner",
        "approved",
        "changed_worktree",
    ),
)
def test_pending_review_rejects_unverified_or_terminal_evidence(
    pending_review_run, mutation: str
):
    case = pending_review_run
    payload = json.loads(json.dumps(case["payload"]))
    if mutation == "missing_exit":
        (case["source"] / "units" / "exit.json").unlink()
    elif mutation == "wrong_before":
        result_path = case["source"] / "turns" / "turn-002" / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["snapshot_before"]["current_checkpoint_index"] = 1
        result_path.write_text(json.dumps(result), encoding="utf-8")
    elif mutation == "wrong_owner":
        start_path = case["source"] / "units" / "start.json"
        start = json.loads(start_path.read_text(encoding="utf-8"))
        start["unit"] = "aflow-run-other.service"
        start_path.write_text(json.dumps(start), encoding="utf-8")
    elif mutation == "active_owner":
        payload["status"] = "running"
    elif mutation == "approved":
        payload["approved_commit"] = "already-approved"
    else:
        (case["root"] / "uncommitted-marker").write_text("changed\n", encoding="utf-8")

    with pytest.raises(ResumeScopeReconciliationError):
        _reconstruct_resume_context(
            resolved_run_id=Path(case["source"].name),
            run_dir=case["source"],
            prev_run=payload,
            plan_path=case["plan"].resolve(),
            frozen_run_identity=None,
            reset_scope=False,
            require_resume=True,
            workflow_steps=case["config"].workflows["cumulative"].steps,
            effective_max_turns=5,
        )
