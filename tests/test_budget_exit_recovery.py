"""Budget-boundary delivery gating for #62.

Unfinished budget boundaries (pre-turn caps, budget-only END edges, and their
finalized replays) must exit without merge teardown, publication, or plan
moves, while preserving worktree, branch, plan, and review/repair evidence.
Reviewed completions keep delivering normally.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from aflow.config import load_workflow_config
from aflow.harnesses.base import HarnessInvocation
from aflow.plan import PlanSnapshot
from aflow.run_state import (
    ControllerConfig,
    PendingFinalizedTurn,
    ResumeContext,
    _mark_validated_resume_context,
)
from aflow.workflow import run_workflow
from tests._support import (
    _COMPLETE_PLAN,
    _VALID_PLAN,
    _git_commit_file,
    _make_lifecycle_git_repo,
    _run_git_in_test,
    _write_plan,
    _write_split_config,
)


def _record_inactive_direct_source(run_dir: Path) -> None:
    """Give synthetic resume sources the terminal record real CLI runs need."""
    metadata_path = run_dir / "run.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = (
        json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata_path.exists() else {}
    )
    metadata["status"] = "interrupted"
    metadata_path.write_text(json.dumps(metadata) + "\n", encoding="utf-8")


class RecordingAdapter:
    name = "codex"
    supports_effort = False
    manager_workspace_read = False

    def build_invocation(
        self,
        *,
        repo_root: Path,
        model: str | None,
        system_prompt: str,
        user_prompt: str,
        effort: str | None = None,
    ) -> HarnessInvocation:
        del repo_root, effort
        return HarnessInvocation(
            label="synthetic-codex",
            argv=("synthetic-codex",),
            env={},
            prompt_mode="synthetic",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            effective_prompt=f"{system_prompt}\n\n{user_prompt}",
        )


_SINGLE_STEP_WORKFLOWS = '''\
[workflow.live]
team = "base"
setup = ["worktree", "branch"]
teardown = ["merge", "rm_worktree"]
main_branch = "main"

[workflow.live.steps.work]
role = "worker"
prompts = ["p"]
go = [{ to = "END", when = "DONE || MAX_TURNS_REACHED" }, { to = "work" }]
'''

_REVIEW_WORKFLOWS = '''\
[workflow.live]
team = "base"
setup = ["worktree", "branch"]
teardown = ["merge", "rm_worktree"]
main_branch = "main"

[workflow.live.steps.work]
role = "worker"
prompts = ["p"]
go = [{ to = "review" }, { to = "END", when = "DONE" }]

[workflow.live.steps.review]
role = "worker"
prompts = ["p"]
go = [{ to = "work", when = "!DONE" }, { to = "END", when = "DONE" }, { to = "END", when = "MAX_TURNS_REACHED" }]
'''

_FINAL_REVIEW_WORKFLOWS = '''\
[workflow.live]
team = "base"
setup = ["worktree", "branch"]
teardown = ["merge", "rm_worktree"]
main_branch = "main"

[workflow.live.steps.work]
role = "worker"
prompts = ["p"]
go = [{ to = "final_review" }, { to = "END", when = "DONE" }]

[workflow.live.steps.final_review]
role = "final_reviewer"
prompts = ["p"]
go = [{ to = "work", when = "NEW_PLAN_EXISTS || !DONE" }, { to = "END", when = "DONE" }, { to = "END", when = "MAX_TURNS_REACHED" }]
'''

_REPAIR_PLAN = (
    "# Repair\n\n"
    "### [ ] Checkpoint 1: Repair\n"
    "- [ ] fix reviewer finding\n"
)

# The historical source left its strict original plan incomplete at the cap;
# the on-disk original must keep agreeing with that saved snapshot.
_PARTIAL_PLAN = (
    "# Plan\n\n"
    "### [x] Checkpoint 1: First\n"
    "- [x] step one\n\n"
    "### [ ] Checkpoint 2: Second\n"
    "- [ ] step two\n"
)

# An ordinary budget exit can leave a worker-edited worktree plan that
# differs from the committed primary checkout copy and stays incomplete.
_CHANGED_INCOMPLETE_PLAN = (
    "# Plan\n\n"
    "### [ ] Checkpoint 1: First\n"
    "- [ ] step one\n"
    "- [ ] step two edited in the worktree\n"
)


@pytest.fixture(
    params=["canonical", "symlink"],
    ids=["canonical-temp-root", "symlink-temp-root"],
)
def budget_temp_parent(tmp_path: Path, request) -> Path:
    """Give real bootstrap/dispatch tests both temporary-parent spellings.

    Production entrypoints supply canonical roots (_resolve_repo_root and
    RunRepository.__init__ resolve their inputs), so fixtures must do the
    same.  The symlink parameter creates a real directory-symlinked parent
    in the pytest process without touching the host TMPDIR, standing in for
    macOS /var -> /private/var aliases.
    """
    base = tmp_path.resolve() / f"parent-{request.param}"
    base.mkdir()
    if request.param == "canonical":
        return base
    alias = base.parent / f"{base.name}-alias"
    alias.symlink_to(base, target_is_directory=True)
    assert alias != alias.resolve()
    assert alias.resolve() == base
    return alias


def _write_budget_config(
    config_dir: Path,
    *,
    max_turns: int,
    worktree_root: Path,
    workflows_text: str,
) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    aflow_text = f'''\
[aflow]
default_workflow = "live"
max_turns = {max_turns}
worktree_root = "{worktree_root}"
team_lead = "senior_architect"

[harness.codex.profiles.base]
model = "model-base"

[harness.codex.profiles.other]
model = "model-other"

[roles]
worker = "codex.base"
final_reviewer = "codex.base"
senior_architect = "codex.base"

[teams.base]
worker = "codex.base"
final_reviewer = "codex.base"

[prompts]
p = "Work from {{ACTIVE_PLAN_PATH}}."
'''
    return _write_split_config(config_dir, aflow_text, workflows_text)[0]


# Every temporary root in this module is canonicalized with .resolve() before
# any child path is derived, matching the canonical roots the production
# entrypoints supply, so all fixture identity references share one canonical
# spelling even when the OS exposes temporary paths through a directory symlink.
def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _make_lifecycle_git_repo(repo_root, branch="main")
    # Isolate the fixture repo from the host's global gitignore (which
    # ignores ``plans``) so the plan files are trackable like a real repo.
    excludes = tmp_path / ".test-excludes"
    excludes.write_text("", encoding="utf-8")
    _run_git_in_test(
        ["config", "core.excludesfile", str(excludes)], cwd=repo_root
    )
    plan_path = repo_root / "plans" / "in-progress" / "plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    _write_plan(plan_path, _VALID_PLAN)
    _git_commit_file(repo_root, plan_path)
    return repo_root, plan_path


def _run_budget(
    config_path: Path,
    repo_root: Path,
    plan_path: Path,
    runner,
    *,
    max_turns: int,
    resume: ResumeContext | None = None,
    adapter: RecordingAdapter | None = None,
):
    config = ControllerConfig(
        repo_root=repo_root,
        plan_path=plan_path,
        max_turns=max_turns,
        team="base",
    )
    return run_workflow(
        config,
        load_workflow_config(config_path),
        "live",
        config_dir=config_path,
        working_dir=repo_root,
        snapshot_config=False,
        adapter=adapter or RecordingAdapter(),
        runner=runner,
        resume=resume,
    )


class _DeliverySpies:
    def __init__(self) -> None:
        self.merge_calls = 0
        self.deliver_calls = 0

    def merge_spy(self, *args, **kwargs):
        self.merge_calls += 1
        return ("merged", None)

    def deliver_spy(self, *args, **kwargs):
        self.deliver_calls += 1
        return kwargs.get("original_plan_path")

    def __enter__(self):
        patcher_merge = patch(
            "aflow.workflow._perform_merge_teardown", self.merge_spy
        )
        patcher_deliver = patch(
            "aflow.workflow._deliver_completed_plan", self.deliver_spy
        )
        patcher_merge.start()
        patcher_deliver.start()
        self._patchers = (patcher_merge, patcher_deliver)
        return self

    def __exit__(self, *exc_info):
        for patcher in self._patchers:
            patcher.stop()
        return False

    def assert_no_delivery(self):
        assert self.merge_calls == 0, "merge teardown ran on a budget exit"
        assert self.deliver_calls == 0, "plan delivery ran on a budget exit"


def _plan_path_in(cwd: Path) -> Path:
    return cwd / "plans" / "in-progress" / "plan.md"


def test_budget_exit_preserves_clean_incomplete_worktree(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path = _make_repo(root)
        worktree_root = root / "worktrees"
        worktree_root.mkdir()
        config_path = _write_budget_config(
            root / "config",
            max_turns=1,
            worktree_root=worktree_root,
            workflows_text=_SINGLE_STEP_WORKFLOWS,
        )

        def runner(argv, **kwargs):
            cwd = Path(kwargs["cwd"])
            (cwd / "feature.txt").write_text("wip\n", encoding="utf-8")
            _run_git_in_test(["add", "feature.txt"], cwd=cwd)
            rc, _, err = _run_git_in_test(
                ["commit", "-m", "wip feature"], cwd=cwd
            )
            assert rc == 0, err
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with _DeliverySpies() as spies:
            result = _run_budget(
                config_path,
                repo_root,
                plan_path,
                runner,
                max_turns=1,
            )

        spies.assert_no_delivery()
        assert result.status == "completed"
        assert result.end_reason == "max_turns_reached"
        assert not result.final_snapshot.is_complete

        run_json = json.loads(
            (result.run_dir / "run.json").read_text(encoding="utf-8")
        )
        assert run_json["status"] == "completed"
        assert run_json["end_reason"] == "max_turns_reached"
        assert "merge_status" not in run_json
        worktree_path = Path(run_json["worktree_path"])
        feature_branch = run_json["feature_branch"]

        # Worktree, branch, work, and plan location all survive the cap.
        assert worktree_path.is_dir()
        assert (worktree_path / "feature.txt").is_file()
        rc, _, _ = _run_git_in_test(
            ["cat-file", "-e", "main:feature.txt"], cwd=repo_root
        )
        assert rc != 0, "feature work leaked into main"
        rc, branches, _ = _run_git_in_test(
            ["branch", "--list", feature_branch], cwd=repo_root
        )
        assert rc == 0 and feature_branch in branches
        assert plan_path.is_file()
        assert plan_path.read_text(encoding="utf-8") == _VALID_PLAN


def test_budget_exit_preserves_dirty_incomplete_worktree(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path = _make_repo(root)
        worktree_root = root / "worktrees"
        worktree_root.mkdir()
        config_path = _write_budget_config(
            root / "config",
            max_turns=1,
            worktree_root=worktree_root,
            workflows_text=_SINGLE_STEP_WORKFLOWS,
        )

        def runner(argv, **kwargs):
            cwd = Path(kwargs["cwd"])
            (cwd / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with _DeliverySpies() as spies:
            result = _run_budget(
                config_path,
                repo_root,
                plan_path,
                runner,
                max_turns=1,
            )

        spies.assert_no_delivery()
        assert result.status == "completed"
        assert result.end_reason == "max_turns_reached"

        run_json = json.loads(
            (result.run_dir / "run.json").read_text(encoding="utf-8")
        )
        worktree_path = Path(run_json["worktree_path"])
        assert worktree_path.is_dir()
        assert (worktree_path / "dirty.txt").is_file()
        rc, status, _ = _run_git_in_test(
            ["status", "--porcelain"], cwd=worktree_path
        )
        assert rc == 0 and "dirty.txt" in status
        assert plan_path.read_text(encoding="utf-8") == _VALID_PLAN


def test_pre_turn_cap_after_complete_ledger_with_pending_review(
    tmp_path: Path,
) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path = _make_repo(root)
        worktree_root = root / "worktrees"
        worktree_root.mkdir()
        config_path = _write_budget_config(
            root / "config",
            max_turns=1,
            worktree_root=worktree_root,
            workflows_text=_REVIEW_WORKFLOWS,
        )

        def runner(argv, **kwargs):
            cwd = Path(kwargs["cwd"])
            _write_plan(_plan_path_in(cwd), _COMPLETE_PLAN)
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with _DeliverySpies() as spies:
            result = _run_budget(
                config_path,
                repo_root,
                plan_path,
                runner,
                max_turns=1,
            )

        spies.assert_no_delivery()
        assert result.status == "completed"
        assert result.end_reason == "max_turns_reached"
        # The ledger is complete, but the configured review has not run, so
        # the cap must not approve it.
        assert result.final_snapshot.is_complete

        run_json = json.loads(
            (result.run_dir / "run.json").read_text(encoding="utf-8")
        )
        assert run_json["end_reason"] == "max_turns_reached"
        assert "merge_status" not in run_json
        # The pending review boundary is preserved as durable evidence.
        assert run_json["current_step_name"] == "review"
        worktree_path = Path(run_json["worktree_path"])
        assert worktree_path.is_dir()
        assert plan_path.is_file()
        assert (
            _plan_path_in(worktree_path).read_text(encoding="utf-8")
            == _COMPLETE_PLAN
        )


def test_budget_exit_preserves_reviewer_repair_overlay(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path = _make_repo(root)
        worktree_root = root / "worktrees"
        worktree_root.mkdir()
        config_path = _write_budget_config(
            root / "config",
            max_turns=2,
            worktree_root=worktree_root,
            workflows_text=_FINAL_REVIEW_WORKFLOWS,
        )
        turns = 0

        def runner(argv, **kwargs):
            nonlocal turns
            turns += 1
            cwd = Path(kwargs["cwd"])
            if turns == 1:
                _write_plan(_plan_path_in(cwd), _COMPLETE_PLAN)
            else:
                overlay = cwd / "plans" / "in-progress" / "plan-cp01-v01.md"
                _write_plan(overlay, _REPAIR_PLAN)
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with _DeliverySpies() as spies:
            result = _run_budget(
                config_path,
                repo_root,
                plan_path,
                runner,
                max_turns=2,
            )

        spies.assert_no_delivery()
        assert turns == 2
        assert result.status == "completed"
        assert result.end_reason == "max_turns_reached"

        run_json = json.loads(
            (result.run_dir / "run.json").read_text(encoding="utf-8")
        )
        assert run_json["end_reason"] == "max_turns_reached"
        assert "merge_status" not in run_json
        # The reviewer's repair overlay survives as the pending active plan.
        assert Path(run_json["active_plan_path"]).name == "plan-cp01-v01.md"
        worktree_path = Path(run_json["worktree_path"])
        overlay = worktree_path / "plans" / "in-progress" / "plan-cp01-v01.md"
        assert overlay.is_file()
        assert overlay.read_text(encoding="utf-8") == _REPAIR_PLAN
        # The completed ledger and the worktree both survive the cap.
        assert (
            _plan_path_in(worktree_path).read_text(encoding="utf-8")
            == _COMPLETE_PLAN
        )
        assert plan_path.is_file()


def test_reviewed_completion_at_cap_still_delivers(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path = _make_repo(root)
        worktree_root = root / "worktrees"
        worktree_root.mkdir()
        config_path = _write_budget_config(
            root / "config",
            max_turns=2,
            worktree_root=worktree_root,
            workflows_text=_REVIEW_WORKFLOWS,
        )
        turns = 0

        def runner(argv, **kwargs):
            nonlocal turns
            turns += 1
            cwd = Path(kwargs["cwd"])
            if turns == 1:
                _write_plan(_plan_path_in(cwd), _COMPLETE_PLAN)
                (cwd / "feature.txt").write_text("done\n", encoding="utf-8")
                _run_git_in_test(["add", "feature.txt"], cwd=cwd)
                rc, _, err = _run_git_in_test(
                    ["commit", "-m", "feature"], cwd=cwd
                )
                assert rc == 0, err
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with _DeliverySpies() as spies:
            result = _run_budget(
                config_path,
                repo_root,
                plan_path,
                runner,
                max_turns=2,
            )

        # A genuine reviewed END at the limit is not a budget exit.
        assert result.status == "completed"
        assert result.end_reason == "done"
        assert result.final_snapshot.is_complete
        assert spies.merge_calls == 1
        assert spies.deliver_calls == 1


def test_finalized_replay_of_budget_only_end_does_not_merge(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path = _make_repo(root)
        worktree_root = root / "worktrees"
        worktree_root.mkdir()
        config_path = _write_budget_config(
            root / "config",
            max_turns=2,
            worktree_root=worktree_root,
            workflows_text=_SINGLE_STEP_WORKFLOWS,
        )

        worktree_path = worktree_root / "wt"
        _run_git_in_test(
            [
                "worktree",
                "add",
                "-b",
                "feature-62",
                str(worktree_path),
                "main",
            ],
            cwd=repo_root,
        )
        source_run = repo_root / ".aflow" / "runs" / "prior-budget-run"
        _record_inactive_direct_source(source_run)
        turn_dir = source_run / "turns" / "turn-001"
        turn_dir.mkdir(parents=True)
        snapshot = PlanSnapshot("First", 1, 1, False, 1, 1)
        (turn_dir / "result.json").write_text(
            json.dumps({
                "turn_number": 1,
                "status": "completed",
                "step_name": "work",
                "step_role": "worker",
                "selector": "codex.base",
                "returncode": 0,
                "active_plan_path": str(plan_path),
                "snapshot_before": snapshot.to_dict(),
                "snapshot_after": snapshot.to_dict(),
                "conditions": {
                    "DONE": False,
                    "NEW_PLAN_EXISTS": False,
                    "MAX_TURNS_REACHED": True,
                },
                "chosen_transition": "END",
                "chosen_transition_condition": "DONE || MAX_TURNS_REACHED",
            }),
            encoding="utf-8",
        )
        (turn_dir / "stdout.txt").write_text("capped", encoding="utf-8")
        (turn_dir / "stderr.txt").write_text("", encoding="utf-8")

        resume = _mark_validated_resume_context(ResumeContext(
            resumed_from_run_id="prior-budget-run",
            feature_branch="feature-62",
            worktree_path=worktree_path,
            main_branch="main",
            setup=("worktree", "branch"),
            teardown=("merge", "rm_worktree"),
            active_plan_path=plan_path,
            pending_finalized_turn=PendingFinalizedTurn(
                source_run_dir=source_run,
                turn_number=1,
                step_name="work",
                step_role="worker",
                selector="codex.base",
                active_plan_path=plan_path,
                new_plan_path=plan_path,
                snapshot_after=snapshot,
                conditions={
                    "DONE": False,
                    "NEW_PLAN_EXISTS": False,
                    "MAX_TURNS_REACHED": True,
                },
                chosen_transition="END",
                chosen_transition_condition="DONE || MAX_TURNS_REACHED",
            ),
        ))

        def runner(argv, **kwargs):
            raise AssertionError("replay must not start a new turn")

        with _DeliverySpies() as spies:
            result = _run_budget(
                config_path,
                repo_root,
                plan_path,
                runner,
                max_turns=2,
                resume=resume,
            )

        spies.assert_no_delivery()
        assert result.status == "completed"
        assert result.end_reason == "max_turns_reached"
        assert not result.final_snapshot.is_complete

        run_json = json.loads(
            (result.run_dir / "run.json").read_text(encoding="utf-8")
        )
        assert run_json["status"] == "completed"
        assert run_json["end_reason"] == "max_turns_reached"
        assert "merge_status" not in run_json
        # Nothing was merged and the recovery surface is intact.
        assert worktree_path.is_dir()
        rc, branches, _ = _run_git_in_test(
            ["branch", "--list", "feature-62"], cwd=repo_root
        )
        assert rc == 0 and "feature-62" in branches
        rc, _, _ = _run_git_in_test(
            ["log", "--oneline", "main..feature-62"], cwd=repo_root
        )
        assert rc == 0
        assert plan_path.is_file()
        assert plan_path.read_text(encoding="utf-8") == _VALID_PLAN


# ---------------------------------------------------------------------------
# Checkpoint #2: budget boundary classification and successor resume.
# ---------------------------------------------------------------------------

from aflow.budget_resume import (
    MERGE_CLEAN_STATE_PREFLIGHT_SIGNATURE,
    BudgetBoundary,
    classify_budget_boundary,
)
from aflow.cli import (
    _bootstrap_resume_invocation,
    _detect_resume_candidate,
    _resume_candidate_mismatch_reason,
)
from aflow.control_plane import InMemoryUnitManager, write_launch_phase
from aflow.daemon import (
    AflowDaemon,
    DaemonConfig,
    DaemonError,
    DaemonIdempotencyConflict,
    DurableRecoveryRejection,
)
from aflow.mcp_control_plane import create_control_plane_mcp
from aflow_app_server.control_plane_service import ControlPlaneService
from aflow_app_server.project_registry import ProjectRegistry


_HISTORICAL_WORKFLOWS = '''\
[workflow.live]
team = "base"
setup = ["worktree", "branch"]
teardown = ["merge", "rm_worktree"]
main_branch = "main"

[workflow.live.steps.work]
role = "worker"
prompts = ["p"]
go = [{ to = "final_review" }, { to = "END", when = "DONE" }]

[workflow.live.steps.final_review]
role = "final_reviewer"
prompts = ["p"]
go = [{ to = "END", when = "MAX_TURNS_REACHED" }, { to = "work", when = "NEW_PLAN_EXISTS || !DONE" }, { to = "END", when = "DONE" }]
'''

_HISTORICAL_MERGE_REASON = (
    MERGE_CLEAN_STATE_PREFLIGHT_SIGNATURE
    + "2 path(s) unclean: plans/in-progress/plan.md, src/app.py"
)

# The supported budget-driven END predecessor graph: the final reviewer's
# ordered routing selects the MAX_TURNS_REACHED END first, so a reviewer
# turn that exhausts the budget exits through that END.  This is the same
# graph the historical fixtures run, which keeps the historical budget-END
# evidence and this lineage on one validated shape.
_FINAL_REVIEW_BUDGET_END_WORKFLOWS = _HISTORICAL_WORKFLOWS


class PromptRecordingAdapter(RecordingAdapter):
    """Records each user prompt so successor prompts can be asserted."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def build_invocation(
        self,
        *,
        repo_root: Path,
        model: str | None,
        system_prompt: str,
        user_prompt: str,
        effort: str | None = None,
    ):
        self.prompts.append(user_prompt)
        return super().build_invocation(
            repo_root=repo_root,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            effort=effort,
        )


def _run_dir_bytes(root: Path) -> dict[str, bytes]:
    """Snapshot the authoritative run records (run.json + turn receipts).

    The resume protocol appends journal events to the source run, so the
    immutability guarantee covers the record the classifier and successor
    rely on, not the append-only event log.
    """
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*.json"))
        if path.name in {"run.json", "result.json"}
    }


def _real_bootstrap(repo_root: Path, config_path: Path, wf_config, run_id: str):
    """The production bootstrap, with the fixture config as live source."""
    return _bootstrap_resume_invocation(
        repo_root=repo_root,
        config_path=config_path,
        default_config_path=config_path,
        config_path_is_explicit=True,
        workflow_config=wf_config,
        requested_run_id=run_id,
        workflow_arg=None,
        plan_file_arg=None,
        team_arg=None,
        start_step_arg=None,
        max_turns_arg=None,
        extra_instructions_arg=(),
        extra_instructions_provided=False,
        live_loader=load_workflow_config,
    )


def _classify(run_dir: Path, config_path: Path, repo_root: Path) -> BudgetBoundary | None:
    return classify_budget_boundary(
        run_dir=run_dir,
        prev_run=json.loads((run_dir / "run.json").read_text(encoding="utf-8")),
        workflow_config=load_workflow_config(config_path),
        repo_root=repo_root,
    )


def _make_source_pre_turn_cap(root: Path, *, plan_text: str = _COMPLETE_PLAN):
    """Run the review workflow to a pre-turn budget cap (pending review).

    ``plan_text`` is what the source worker writes into the execution
    worktree's plan file, so a fixture can model a changed worktree plan
    that differs from the committed primary checkout copy.
    """
    repo_root, plan_path = _make_repo(root)
    worktree_root = root / "worktrees"
    worktree_root.mkdir()
    config_path = _write_budget_config(
        root / "config",
        max_turns=1,
        worktree_root=worktree_root,
        workflows_text=_REVIEW_WORKFLOWS,
    )

    def source_runner(argv, **kwargs):
        cwd = Path(kwargs["cwd"])
        _write_plan(_plan_path_in(cwd), plan_text)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    with _DeliverySpies() as spies:
        result = _run_budget(
            config_path, repo_root, plan_path, source_runner, max_turns=1,
        )
    spies.assert_no_delivery()
    assert result.status == "completed"
    assert result.end_reason == "max_turns_reached"
    return repo_root, plan_path, config_path, result


def _make_source_single_step(root: Path):
    """Run the single-step workflow to a pre-turn budget cap (incomplete).

    The graph is ``work -> END when DONE || MAX_TURNS_REACHED`` followed by an
    unconditional ``work`` fallback.  An incomplete one-turn budget exit from
    this graph must be admitted as a budget exit that resumes ``work``.
    """
    repo_root, plan_path = _make_repo(root)
    worktree_root = root / "worktrees"
    worktree_root.mkdir()
    config_path = _write_budget_config(
        root / "config",
        max_turns=1,
        worktree_root=worktree_root,
        workflows_text=_SINGLE_STEP_WORKFLOWS,
    )

    def source_runner(argv, **kwargs):
        cwd = Path(kwargs["cwd"])
        _write_plan(_plan_path_in(cwd), _CHANGED_INCOMPLETE_PLAN)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    with _DeliverySpies() as spies:
        result = _run_budget(
            config_path, repo_root, plan_path, source_runner, max_turns=1,
        )
    spies.assert_no_delivery()
    assert result.status == "completed"
    assert result.end_reason == "max_turns_reached"
    return repo_root, plan_path, config_path, result


def _bootstrap_with_args(
    repo_root: Path, config_path: Path, wf_config, run_id: str,
    *, max_turns_arg: int | None = None,
):
    """The production bootstrap with an optional explicit invocation limit."""
    return _bootstrap_resume_invocation(
        repo_root=repo_root,
        config_path=config_path,
        default_config_path=config_path,
        config_path_is_explicit=True,
        workflow_config=wf_config,
        requested_run_id=run_id,
        workflow_arg=None,
        plan_file_arg=None,
        team_arg=None,
        start_step_arg=None,
        max_turns_arg=max_turns_arg,
        extra_instructions_arg=(),
        extra_instructions_provided=False,
        live_loader=load_workflow_config,
    )


def test_single_step_budget_exit_starts_work(
    tmp_path: Path, budget_temp_parent: Path
) -> None:
    """An incomplete one-turn budget exit resumes work and then delivers."""
    with tempfile.TemporaryDirectory(dir=budget_temp_parent) as tmpdir:
        assert Path(tmpdir).parent == budget_temp_parent
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, result = _make_source_single_step(root)
        source_dir = result.run_dir
        source_before = _run_dir_bytes(source_dir)

        boundary = _classify(source_dir, config_path, repo_root)
        assert isinstance(boundary, BudgetBoundary)
        assert boundary.kind == "budget_exit"
        assert boundary.next_step_name == "work"
        assert boundary.saved_max_turns == 1

        bootstrap = _real_bootstrap(
            repo_root, config_path, load_workflow_config(config_path),
            source_dir.name,
        )
        assert bootstrap.start_step == "work"
        assert bootstrap.max_turns == 1
        context = bootstrap.resume_context
        assert isinstance(context.budget_continuation, BudgetBoundary)
        assert context.budget_continuation.kind == "budget_exit"
        assert context.interrupted_step_name == "work"

        def successor_runner(argv, **kwargs):
            cwd = Path(kwargs["cwd"])
            _write_plan(_plan_path_in(cwd), _COMPLETE_PLAN)
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with _DeliverySpies() as spies:
            successor = _run_budget(
                config_path,
                repo_root,
                bootstrap.plan_path,
                successor_runner,
                max_turns=bootstrap.max_turns,
                resume=context,
            )

        # The successor ran the pending work step first and then delivered.
        assert successor.status == "completed"
        assert successor.end_reason == "done"
        assert successor.run_dir != source_dir
        turn1 = json.loads(
            (successor.run_dir / "turns" / "turn-001" / "result.json").read_text(encoding="utf-8")
        )
        assert turn1["step_name"] == "work"
        assert spies.merge_calls == 1
        assert spies.deliver_calls == 1
        # The source run stayed byte-identical.
        assert _run_dir_bytes(source_dir) == source_before


def test_single_step_incompatible_saved_end_evidence_rejects(tmp_path: Path) -> None:
    """A saved END condition that does not identify its edge rejects."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, result = _make_source_single_step(root)
        source_dir = result.run_dir
        receipt_path = source_dir / "turns" / "turn-001" / "result.json"
        original = receipt_path.read_bytes()

        # Baseline: the intact single-step budget exit is admitted.
        assert _classify(source_dir, config_path, repo_root) is not None

        # A saved END condition no graph edge carries is incompatible evidence:
        # it must not be admitted as an unordered substitute for the configured
        # first-match routing contract.
        receipt = json.loads(original.decode(encoding="utf-8"))
        receipt["chosen_transition_condition"] = "MAX_TURNS_REACHED"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        try:
            assert _classify(source_dir, config_path, repo_root) is None
        finally:
            receipt_path.write_bytes(original)
        assert _classify(source_dir, config_path, repo_root) is not None


def test_budget_provenance_default_explicit_and_invocation(tmp_path: Path) -> None:
    """Successor budget provenance follows saved/invocation/default precedence.

    A default-derived saved limit falls through to the current live default
    (so future default changes keep working); an explicit saved limit and an
    explicit new invocation limit are honored with explicit provenance.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, result = _make_source_single_step(root)
        source_dir = result.run_dir
        run_json_path = source_dir / "run.json"

        def set_saved(explicit: bool) -> None:
            run_json = json.loads(run_json_path.read_text(encoding="utf-8"))
            run_json["max_turns"] = 1
            run_json["max_turns_explicit"] = explicit
            run_json_path.write_text(json.dumps(run_json), encoding="utf-8")

        # Live default changes to 7 for the successor.
        live_config = _write_budget_config(
            root / "config",
            max_turns=7,
            worktree_root=root / "worktrees",
            workflows_text=_SINGLE_STEP_WORKFLOWS,
        )

        # Default-derived saved limit 1 with live default 7 -> successor 7,
        # default provenance (not explicit).
        set_saved(False)
        bootstrap = _real_bootstrap(
            repo_root, live_config, load_workflow_config(live_config),
            source_dir.name,
        )
        assert bootstrap.max_turns == 7
        assert bootstrap.max_turns_explicit is False

        # The successor effective limit is the live default, not the saved 1.
        context = bootstrap.resume_context

        def successor_runner(argv, **kwargs):
            cwd = Path(kwargs["cwd"])
            _write_plan(_plan_path_in(cwd), _COMPLETE_PLAN)
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with _DeliverySpies():
            successor = _run_budget(
                live_config,
                repo_root,
                bootstrap.plan_path,
                successor_runner,
                max_turns=bootstrap.max_turns,
                resume=context,
            )
        assert json.loads(
            (successor.run_dir / "run.json").read_text(encoding="utf-8")
        )["effective_max_turns"] == 7
        assert successor.status == "completed"

        # Explicit saved limit 1 with live default 7 -> successor stays 1,
        # explicit provenance preserved (no silent increase).
        set_saved(True)
        bootstrap = _real_bootstrap(
            repo_root, live_config, load_workflow_config(live_config),
            source_dir.name,
        )
        assert bootstrap.max_turns == 1
        assert bootstrap.max_turns_explicit is True

        # Explicit new invocation limit 5 -> successor 5, explicit provenance.
        set_saved(False)
        bootstrap = _bootstrap_with_args(
            repo_root, live_config, load_workflow_config(live_config),
            source_dir.name, max_turns_arg=5,
        )
        assert bootstrap.max_turns == 5
        assert bootstrap.max_turns_explicit is True


def test_explicit_resume_of_pre_turn_cap_starts_review_first(tmp_path: Path) -> None:
    """A pre-turn budget exit resumes the pending review, then delivers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, result = _make_source_pre_turn_cap(root)
        source_dir = result.run_dir
        source_before = _run_dir_bytes(source_dir)

        boundary = _classify(source_dir, config_path, repo_root)
        assert isinstance(boundary, BudgetBoundary)
        assert boundary.kind == "budget_exit"
        assert boundary.next_step_name == "review"
        assert boundary.saved_max_turns == 1

        bootstrap = _real_bootstrap(
            repo_root, config_path, load_workflow_config(config_path),
            source_dir.name,
        )
        assert bootstrap.start_step == "review"
        assert bootstrap.max_turns == 1
        context = bootstrap.resume_context
        assert isinstance(context.budget_continuation, BudgetBoundary)
        assert context.budget_continuation.kind == "budget_exit"
        assert context.interrupted_step_name == "review"

        worktree_path = Path(
            json.loads(
                (source_dir / "run.json").read_text(encoding="utf-8")
            )["worktree_path"]
        )
        worktree_plan = worktree_path / "plans" / "in-progress" / "plan.md"

        adapter = PromptRecordingAdapter()

        def successor_runner(argv, **kwargs):
            cwd = Path(kwargs["cwd"])
            _write_plan(_plan_path_in(cwd), _COMPLETE_PLAN)
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with _DeliverySpies() as spies:
            successor = _run_budget(
                config_path,
                repo_root,
                bootstrap.plan_path,
                successor_runner,
                max_turns=bootstrap.max_turns,
                resume=context,
                adapter=adapter,
            )

        # The successor ran the pending review first and then delivered.
        assert successor.status == "completed"
        assert successor.end_reason == "done"
        assert successor.run_dir != source_dir
        turn1 = json.loads(
            (
                successor.run_dir / "turns" / "turn-001" / "result.json"
            ).read_text(encoding="utf-8")
        )
        assert turn1["step_name"] == "review"
        assert str(worktree_plan) in adapter.prompts[0]
        assert spies.merge_calls == 1
        assert spies.deliver_calls == 1
        # The source run stayed byte-identical.
        assert _run_dir_bytes(source_dir) == source_before


def test_explicit_resume_of_reviewer_overlay_starts_repair(tmp_path: Path) -> None:
    """A reviewer overlay at the cap becomes the successor's active plan."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path = _make_repo(root)
        worktree_root = root / "worktrees"
        worktree_root.mkdir()
        config_path = _write_budget_config(
            root / "config",
            max_turns=2,
            worktree_root=worktree_root,
            workflows_text=_FINAL_REVIEW_WORKFLOWS,
        )
        turns = 0

        def source_runner(argv, **kwargs):
            nonlocal turns
            turns += 1
            cwd = Path(kwargs["cwd"])
            if turns == 1:
                _write_plan(_plan_path_in(cwd), _COMPLETE_PLAN)
            else:
                overlay = cwd / "plans" / "in-progress" / "plan-cp01-v01.md"
                _write_plan(overlay, _REPAIR_PLAN)
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with _DeliverySpies() as spies:
            result = _run_budget(
                config_path, repo_root, plan_path, source_runner, max_turns=2,
            )
        spies.assert_no_delivery()
        source_dir = result.run_dir
        source_before = _run_dir_bytes(source_dir)

        boundary = _classify(source_dir, config_path, repo_root)
        assert isinstance(boundary, BudgetBoundary)
        assert boundary.kind == "budget_exit"
        assert boundary.next_step_name == "work"
        assert boundary.new_plan_exists is True
        assert boundary.overlay_path is not None
        assert boundary.overlay_path.name == "plan-cp01-v01.md"

        bootstrap = _real_bootstrap(
            repo_root, config_path, load_workflow_config(config_path),
            source_dir.name,
        )
        assert bootstrap.start_step == "work"
        assert bootstrap.max_turns == 2
        context = bootstrap.resume_context
        assert isinstance(context.budget_continuation, BudgetBoundary)
        assert context.active_plan_path.name == "plan-cp01-v01.md"

        adapter = PromptRecordingAdapter()
        complete_repair = (
            "# Repair\n\n"
            "### [x] Checkpoint 1: Repair\n"
            "- [x] fix reviewer finding\n"
        )
        s_turns = 0

        def successor_runner(argv, **kwargs):
            nonlocal s_turns
            s_turns += 1
            cwd = Path(kwargs["cwd"])
            if s_turns == 1:
                _write_plan(_plan_path_in(cwd), _COMPLETE_PLAN)
                _write_plan(
                    cwd / "plans" / "in-progress" / "plan-cp01-v01.md",
                    complete_repair,
                )
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with _DeliverySpies() as spies2:
            successor = _run_budget(
                config_path,
                repo_root,
                bootstrap.plan_path,
                successor_runner,
                max_turns=bootstrap.max_turns,
                resume=context,
                adapter=adapter,
            )

        assert s_turns == 2
        assert successor.status == "completed"
        assert successor.end_reason == "done"
        turn1 = json.loads(
            (
                successor.run_dir / "turns" / "turn-001" / "result.json"
            ).read_text(encoding="utf-8")
        )
        assert turn1["step_name"] == "work"
        # The overlay was synced into the successor's worktree and the first
        # prompt addressed it, not the original plan.
        successor_worktree = Path(
            json.loads(
                (successor.run_dir / "run.json").read_text(encoding="utf-8")
            )["worktree_path"]
        )
        overlay = successor_worktree / "plans" / "in-progress" / "plan-cp01-v01.md"
        assert overlay.is_file()
        assert str(overlay) in adapter.prompts[0]
        # The successor's reviewed completion delivers normally.
        assert spies2.merge_calls == 1
        assert spies2.deliver_calls == 1
        # The source run stayed byte-identical.
        assert _run_dir_bytes(source_dir) == source_before


def test_cap_on_repair_turn_started_from_overlay_classifies(tmp_path: Path) -> None:
    """A capped turn that provably started from a prior overlay admits.

    The reviewer turn created the repair overlay (NEW_PLAN_EXISTS), so the
    next work turn starts from that overlay.  Capping on the repair turn must
    classify with the overlay as the boundary active plan: the ordered
    predecessor evidence derives exactly that start identity, while the
    before-turn/after-turn identities of the preceding reviewer receipt stay
    distinct (original vs overlay).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path = _make_repo(root)
        worktree_root = root / "worktrees"
        worktree_root.mkdir()
        config_path = _write_budget_config(
            root / "config",
            max_turns=3,
            worktree_root=worktree_root,
            workflows_text=_FINAL_REVIEW_WORKFLOWS,
        )
        turns = 0

        def source_runner(argv, **kwargs):
            nonlocal turns
            turns += 1
            cwd = Path(kwargs["cwd"])
            if turns == 1:
                _write_plan(_plan_path_in(cwd), _COMPLETE_PLAN)
            elif turns == 2:
                _write_plan(
                    cwd / "plans" / "in-progress" / "plan-cp01-v01.md",
                    _REPAIR_PLAN,
                )
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with _DeliverySpies() as spies:
            result = _run_budget(
                config_path, repo_root, plan_path, source_runner, max_turns=3,
            )
        spies.assert_no_delivery()
        assert turns == 3
        assert result.status == "completed"
        assert result.end_reason == "max_turns_reached"

        source_dir = result.run_dir
        run_json = json.loads(
            (source_dir / "run.json").read_text(encoding="utf-8")
        )
        overlay = repo_root / "plans" / "in-progress" / "plan-cp01-v01.md"
        reserved_next = repo_root / "plans" / "in-progress" / "plan-cp01-v02.md"
        # The finalized work turn started from the reviewer's overlay; with
        # no new overlay that turn, the terminal record fell back to the
        # original plan and reserved the next follow-up path.
        assert run_json["active_plan_path"] == str(plan_path)
        assert run_json["new_plan_path"] == str(reserved_next)
        reviewer_receipt = json.loads(
            (source_dir / "turns" / "turn-002" / "result.json").read_text(
                encoding="utf-8"
            )
        )
        assert reviewer_receipt["active_plan_path"] == str(plan_path)
        assert reviewer_receipt["new_plan_path"] == str(overlay)
        assert reviewer_receipt["conditions"]["NEW_PLAN_EXISTS"] is True
        repair_receipt = json.loads(
            (source_dir / "turns" / "turn-003" / "result.json").read_text(
                encoding="utf-8"
            )
        )
        assert repair_receipt["active_plan_path"] == str(overlay)
        assert repair_receipt["new_plan_path"] == str(reserved_next)
        assert repair_receipt["conditions"]["NEW_PLAN_EXISTS"] is False

        boundary = _classify(source_dir, config_path, repo_root)
        assert isinstance(boundary, BudgetBoundary)
        assert boundary.kind == "budget_exit"
        assert boundary.new_plan_exists is False
        assert boundary.active_plan_path == overlay
        assert boundary.overlay_path is None
        assert boundary.original_plan_path == plan_path

        # The full bootstrap admits the same boundary and starts the pending
        # final review from the overlay the repair turn started from.
        bootstrap = _real_bootstrap(
            repo_root, config_path, load_workflow_config(config_path),
            source_dir.name,
        )
        assert bootstrap.start_step == "final_review"
        assert bootstrap.resume_context.active_plan_path == overlay


def _make_inherited_overlay_successor(
    root: Path, *, budget_end_source: bool = False
):
    """Run the two-controller lineage to the blocked inherited-start shape.

    The source run completes the original ledger and its final reviewer
    creates ``plan-cp01-v01.md`` before capping.  With ``budget_end_source``
    that reviewer turn exits through the supported budget-driven END edge
    (ordered MAX_TURNS_REACHED first); otherwise it selects the nonterminal
    repair edge.  The explicit bootstrap resumes either source with a single
    repair turn; that successor starts from the inherited overlay, caps, and
    leaves the pending final review blocked behind the first-turn identity
    evidence.
    """
    repo_root, plan_path = _make_repo(root)
    worktree_root = root / "worktrees"
    worktree_root.mkdir()
    config_path = _write_budget_config(
        root / "config",
        max_turns=2,
        worktree_root=worktree_root,
        workflows_text=(
            _FINAL_REVIEW_BUDGET_END_WORKFLOWS
            if budget_end_source
            else _FINAL_REVIEW_WORKFLOWS
        ),
    )
    turns = 0

    def source_runner(argv, **kwargs):
        nonlocal turns
        turns += 1
        cwd = Path(kwargs["cwd"])
        if turns == 1:
            _write_plan(_plan_path_in(cwd), _COMPLETE_PLAN)
        else:
            _write_plan(
                cwd / "plans" / "in-progress" / "plan-cp01-v01.md",
                _REPAIR_PLAN,
            )
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    with _DeliverySpies() as spies:
        source = _run_budget(
            config_path, repo_root, plan_path, source_runner, max_turns=2,
        )
    spies.assert_no_delivery()
    assert source.status == "completed"
    assert source.end_reason == "max_turns_reached"

    boot = _bootstrap_with_args(
        repo_root, config_path, load_workflow_config(config_path),
        source.run_dir.name, max_turns_arg=1,
    )
    assert boot.start_step == "work"
    assert boot.resume_context.active_plan_path.name == "plan-cp01-v01.md"

    def repair_runner(argv, **kwargs):
        # The repair worker runs once from the overlay and leaves the pending
        # review in place.
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    with _DeliverySpies() as repair_spies:
        successor = _run_budget(
            config_path, repo_root, boot.plan_path, repair_runner,
            max_turns=boot.max_turns, resume=boot.resume_context,
        )
    repair_spies.assert_no_delivery()
    assert successor.status == "completed"
    assert successor.end_reason == "max_turns_reached"

    successor_run = json.loads(
        (successor.run_dir / "run.json").read_text(encoding="utf-8")
    )
    receipt = json.loads(
        (successor.run_dir / "turns" / "turn-001" / "result.json").read_text(
            encoding="utf-8"
        )
    )
    overlay = repo_root / "plans" / "in-progress" / "plan-cp01-v01.md"
    # The untampered reproduction shape: the successor's own fresh budget
    # (one turn), the repair turn provably started from the inherited
    # overlay, and the terminal active fell back to the original plan on the
    # non-preserving final_review edge.
    assert successor_run["resumed_from_run_id"] == source.run_dir.name
    assert successor_run["turns_completed"] == 1
    assert successor_run["effective_max_turns"] == 1
    assert receipt["active_plan_path"] == str(overlay)
    assert successor_run["active_plan_path"] == str(plan_path)
    assert receipt["chosen_transition"] == "final_review"
    return repo_root, plan_path, config_path, source, successor, overlay


def test_inherited_overlay_start_survives_next_budget_exit(tmp_path: Path) -> None:
    """A successor capped on its first inherited-overlay turn stays resumable.

    The inherited repair overlay is a legitimate exact start: the next
    budget exit classifies, the real bootstrap admits the pending final
    reviewer with the validated overlay, and that reviewer completes and
    delivers normally.  No successor delivers early, accounting restarts
    fresh, and every predecessor evidence stays byte-identical.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, source, successor, overlay = (
            _make_inherited_overlay_successor(root)
        )
        wf_config = load_workflow_config(config_path)
        source_before = _run_dir_bytes(source.run_dir)
        successor_before = _run_dir_bytes(successor.run_dir)

        boundary = _classify(successor.run_dir, config_path, repo_root)
        assert isinstance(boundary, BudgetBoundary)
        assert boundary.kind == "budget_exit"
        assert boundary.next_step_name == "final_review"
        assert boundary.new_plan_exists is False
        assert boundary.overlay_path is None
        assert boundary.active_plan_path == overlay
        assert boundary.original_plan_path == plan_path
        # Read-only classification: both runs stay byte-identical.
        assert _run_dir_bytes(successor.run_dir) == successor_before
        assert _run_dir_bytes(source.run_dir) == source_before

        bootstrap = _real_bootstrap(
            repo_root, config_path, wf_config, successor.run_dir.name,
        )
        assert bootstrap.start_step == "final_review"
        context = bootstrap.resume_context
        assert isinstance(context.budget_continuation, BudgetBoundary)
        assert context.active_plan_path == overlay

        adapter = PromptRecordingAdapter()
        complete_repair = (
            "# Repair\n\n"
            "### [x] Checkpoint 1: Repair\n"
            "- [x] fix reviewer finding\n"
        )

        def reviewer_runner(argv, **kwargs):
            cwd = Path(kwargs["cwd"])
            _write_plan(
                cwd / "plans" / "in-progress" / "plan-cp01-v01.md",
                complete_repair,
            )
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with _DeliverySpies() as spies:
            final = _run_budget(
                config_path, repo_root, bootstrap.plan_path, reviewer_runner,
                max_turns=bootstrap.max_turns, resume=context,
                adapter=adapter,
            )
        assert final.status == "completed"
        assert final.end_reason == "done"
        final_turn = json.loads(
            (final.run_dir / "turns" / "turn-001" / "result.json").read_text(
                encoding="utf-8"
            )
        )
        assert final_turn["step_name"] == "final_review"
        assert final_turn["active_plan_path"] == str(overlay)
        final_worktree = Path(
            json.loads(
                (final.run_dir / "run.json").read_text(encoding="utf-8")
            )["worktree_path"]
        )
        assert (
            final_worktree / "plans" / "in-progress" / "plan-cp01-v01.md"
        ).is_file()
        # The pending final reviewer was addressed with the validated
        # overlay, and the repaired work delivered normally.
        assert str(final_worktree / "plans" / "in-progress" / "plan-cp01-v01.md") in (
            adapter.prompts[0]
        )
        assert spies.merge_calls == 1
        assert spies.deliver_calls == 1
        # The successor spent exactly its own single-turn budget, and both
        # predecessors kept their immutable evidence.
        assert _run_dir_bytes(source.run_dir) == source_before
        assert _run_dir_bytes(successor.run_dir) == successor_before


def test_inherited_overlay_start_survives_budget_end_predecessor(
    tmp_path: Path,
) -> None:
    """A successor of a validated budget-driven END stays resumable.

    The source's final reviewer exits through the supported MAX-driven END
    while creating the repair overlay; its successor runs one repair turn
    from the inherited overlay and caps on the non-preserving final_review
    edge.  That successor must still classify: the next bootstrap admits
    the pending final reviewer with the exact inherited overlay and fresh
    accounting, nothing delivers early, and the reviewer then completes and
    delivers normally.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, source, successor, overlay = (
            _make_inherited_overlay_successor(root, budget_end_source=True)
        )
        wf_config = load_workflow_config(config_path)
        source_before = _run_dir_bytes(source.run_dir)
        successor_before = _run_dir_bytes(successor.run_dir)

        # The predecessor invocation ended through the validated budget END.
        source_receipt = json.loads(
            (source.run_dir / "turns" / "turn-002" / "result.json").read_text(
                encoding="utf-8"
            )
        )
        assert source_receipt["chosen_transition"] == "END"
        assert (
            source_receipt["chosen_transition_condition"] == "MAX_TURNS_REACHED"
        )

        boundary = _classify(successor.run_dir, config_path, repo_root)
        assert isinstance(boundary, BudgetBoundary)
        assert boundary.kind == "budget_exit"
        assert boundary.next_step_name == "final_review"
        assert boundary.new_plan_exists is False
        assert boundary.overlay_path is None
        assert boundary.active_plan_path == overlay
        assert boundary.original_plan_path == plan_path
        # Read-only classification: both runs stay byte-identical.
        assert _run_dir_bytes(successor.run_dir) == successor_before
        assert _run_dir_bytes(source.run_dir) == source_before

        bootstrap = _real_bootstrap(
            repo_root, config_path, wf_config, successor.run_dir.name,
        )
        assert bootstrap.start_step == "final_review"
        context = bootstrap.resume_context
        assert isinstance(context.budget_continuation, BudgetBoundary)
        assert context.active_plan_path == overlay

        adapter = PromptRecordingAdapter()
        complete_repair = (
            "# Repair\n\n"
            "### [x] Checkpoint 1: Repair\n"
            "- [x] fix reviewer finding\n"
        )

        def reviewer_runner(argv, **kwargs):
            cwd = Path(kwargs["cwd"])
            _write_plan(
                cwd / "plans" / "in-progress" / "plan-cp01-v01.md",
                complete_repair,
            )
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        # In the END-first graph the budget END is the first ordered edge, so
        # a reviewer finishing exactly at its last budget turn would exit as
        # a budget exit; the explicit invocation grants one spare turn so the
        # reviewer's DONE edge is the one that delivers.
        with _DeliverySpies() as spies:
            final = _run_budget(
                config_path, repo_root, bootstrap.plan_path, reviewer_runner,
                max_turns=2, resume=context,
                adapter=adapter,
            )
        assert final.status == "completed"
        assert final.end_reason == "done"
        final_run = json.loads(
            (final.run_dir / "run.json").read_text(encoding="utf-8")
        )
        final_turn = json.loads(
            (final.run_dir / "turns" / "turn-001" / "result.json").read_text(
                encoding="utf-8"
            )
        )
        # Fresh accounting: the final reviewer spent its own first turn on
        # the pending review, resumed from the capped successor.
        assert final_run["turns_completed"] == 1
        assert final_run["resumed_from_run_id"] == successor.run_dir.name
        assert final_turn["step_name"] == "final_review"
        assert final_turn["active_plan_path"] == str(overlay)
        final_worktree = Path(final_run["worktree_path"])
        assert (
            final_worktree / "plans" / "in-progress" / "plan-cp01-v01.md"
        ).is_file()
        # The pending final reviewer was addressed with the validated
        # overlay, and the repaired work delivered normally.
        assert str(final_worktree / "plans" / "in-progress" / "plan-cp01-v01.md") in (
            adapter.prompts[0]
        )
        assert spies.merge_calls == 1
        assert spies.deliver_calls == 1
        # Every predecessor kept its immutable evidence.
        assert _run_dir_bytes(source.run_dir) == source_before
        assert _run_dir_bytes(successor.run_dir) == successor_before


def test_budget_end_predecessor_surfaces_admit_and_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every admission surface accepts the budget-END lineage; fakes reject.

    The shared classifier, the daemon preview, the explicit bootstrap, and
    managed durable replacement admission all accept the successor of a
    validated budget-driven END, while an arbitrary terminal END (a
    condition that does not stop matching once the budget is restored), a
    missing END condition, and contradicted predecessor evidence reject
    before any successor reservation.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, source, successor, overlay = (
            _make_inherited_overlay_successor(root, budget_end_source=True)
        )
        wf_config = load_workflow_config(config_path)
        successor_dir = successor.run_dir
        source_dir = source.run_dir
        source_receipt_path = source_dir / "turns" / "turn-002" / "result.json"
        original_source_receipt = source_receipt_path.read_bytes()
        successor_before = _run_dir_bytes(successor_dir)
        source_before = _run_dir_bytes(source_dir)
        _attach_launch_evidence(repo_root, successor_dir.name, "completed")

        daemon = _make_daemon(tmp_path, monkeypatch, repo_root, config_path, wf_config)
        _patch_inactive_worker_evidence(
            monkeypatch, daemon.application.repository
        )

        # Baseline: every surface admits the budget-END lineage.
        assert isinstance(
            _classify(successor_dir, config_path, repo_root), BudgetBoundary
        )
        status = daemon.service.run_status(
            successor_dir.name, include_resume_preview=True
        )
        assert status.evidence.get("can_resume") is True
        bootstrap = _real_bootstrap(
            repo_root, config_path, wf_config, successor_dir.name
        )
        assert bootstrap.start_step == "final_review"
        assert bootstrap.resume_context.active_plan_path == overlay

        def reject_case(label: str, *, mutate_source_receipt=None) -> None:
            receipt = json.loads(
                original_source_receipt.decode(encoding="utf-8")
            )
            if mutate_source_receipt is not None:
                mutate_source_receipt(receipt)
            source_receipt_path.write_text(
                json.dumps(receipt), encoding="utf-8"
            )
            try:
                assert (
                    _classify(successor_dir, config_path, repo_root) is None
                ), label
                tampered_status = daemon.service.run_status(
                    successor_dir.name, include_resume_preview=True
                )
                assert (
                    tampered_status.evidence.get("can_resume") is False
                ), label
                with pytest.raises(ValueError):
                    _real_bootstrap(
                        repo_root, config_path, wf_config, successor_dir.name
                    )
            finally:
                source_receipt_path.write_bytes(original_source_receipt)

        # An END condition that keeps matching with the budget restored is
        # not a budget-driven predecessor: only the budget-only edge
        # qualifies, and the recorded condition must identify exactly it.
        reject_case(
            "non-budget-end-condition",
            mutate_source_receipt=lambda r: r.__setitem__(
                "chosen_transition_condition", "DONE"
            ),
        )
        # A missing END condition identifies no edge at all.
        reject_case(
            "missing-end-condition",
            mutate_source_receipt=lambda r: r.__setitem__(
                "chosen_transition_condition", None
            ),
        )
        # The predecessor's terminal record must keep matching the post-END
        # selection: the overlay its last turn created.
        reject_case(
            "contradicted-terminal-overlay",
            mutate_source_receipt=lambda r: r["conditions"].__setitem__(
                "NEW_PLAN_EXISTS", False
            ),
        )

        # Restored: admission returns everywhere and nothing was mutated.
        assert isinstance(
            _classify(successor_dir, config_path, repo_root), BudgetBoundary
        )
        assert _run_dir_bytes(successor_dir) == successor_before
        assert _run_dir_bytes(source_dir) == source_before

        # Managed durable replacement admission of the intact successor
        # creates one bound continuation with the successor's own
        # single-turn budget, and both predecessors keep byte-identical
        # evidence.
        continuation = daemon.service.resume(
            successor_dir.name,
            caller_scope="local",
            idempotency_key="recover-budget-end-successor",
            recovery={
                "mode": "durable_evidence",
                "worker_selector": "codex.base",
            },
        )
        assert continuation.created is True
        assert continuation.run_id != successor_dir.name
        from aflow.control_plane.recovery import read_recovery_intent

        intent = read_recovery_intent(
            repo_root / ".aflow" / "runs" / continuation.run_id
        )
        assert intent.source_run_id == successor_dir.name
        assert intent.target_selector == "codex.base"
        successor_manifest = json.loads(
            (
                repo_root / ".aflow" / "launches" / f"{continuation.run_id}.json"
            ).read_text(encoding="utf-8")
        )
        assert successor_manifest["max_turns"] == 1
        assert _run_dir_bytes(successor_dir) == successor_before
        assert _run_dir_bytes(source_dir) == source_before


def test_inherited_start_contradictions_reject_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrong inherited-start claims never reach reservation or provider launch.

    The classifier, the daemon preview, the explicit bootstrap, and managed
    durable recovery all reject substituted, missing, cyclic, or
    contradicted lineage before any successor reservation, while the intact
    successor still admits a bound replacement.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, source, successor, overlay = (
            _make_inherited_overlay_successor(root)
        )
        wf_config = load_workflow_config(config_path)
        successor_dir = successor.run_dir
        source_dir = source.run_dir
        receipt_path = successor_dir / "turns" / "turn-001" / "result.json"
        run_json_path = successor_dir / "run.json"
        original_receipt = receipt_path.read_bytes()
        original_run_json = run_json_path.read_bytes()
        _attach_launch_evidence(repo_root, successor_dir.name, "completed")

        daemon = _make_daemon(tmp_path, monkeypatch, repo_root, config_path, wf_config)
        _patch_inactive_worker_evidence(
            monkeypatch, daemon.application.repository
        )

        def surface() -> tuple[list[str], list[str]]:
            return (
                sorted(
                    p.name
                    for p in (repo_root / ".aflow" / "runs").iterdir()
                    if p.is_dir()
                ),
                sorted(
                    p.name
                    for p in (repo_root / ".aflow" / "launches").iterdir()
                    if p.name.endswith(".json")
                    and not p.name.endswith(".state.json")
                ),
            )

        # Baseline: the intact inherited start admits on every surface.
        assert isinstance(
            _classify(successor_dir, config_path, repo_root), BudgetBoundary
        )
        status = daemon.service.run_status(
            successor_dir.name, include_resume_preview=True
        )
        assert status.evidence.get("can_resume") is True
        bootstrap = _real_bootstrap(
            repo_root, config_path, wf_config, successor_dir.name
        )
        assert bootstrap.start_step == "final_review"

        def reject_case(
            label: str,
            *,
            mutate_receipt=None,
            mutate_run=None,
        ) -> None:
            receipt = json.loads(original_receipt.decode(encoding="utf-8"))
            run = json.loads(original_run_json.decode(encoding="utf-8"))
            if mutate_receipt is not None:
                mutate_receipt(receipt)
            if mutate_run is not None:
                mutate_run(run)
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            run_json_path.write_text(json.dumps(run), encoding="utf-8")
            try:
                assert (
                    _classify(successor_dir, config_path, repo_root) is None
                ), label
                tampered_status = daemon.service.run_status(
                    successor_dir.name, include_resume_preview=True
                )
                assert (
                    tampered_status.evidence.get("can_resume") is False
                ), label
                with pytest.raises(ValueError):
                    _real_bootstrap(
                        repo_root, config_path, wf_config, successor_dir.name
                    )
                before = surface()
                with pytest.raises(
                    (DurableRecoveryRejection, DaemonError, ValueError)
                ):
                    daemon.service.resume(
                        successor_dir.name,
                        caller_scope="local",
                        idempotency_key=f"inherited-{label}",
                        recovery={
                            "mode": "durable_evidence",
                            "worker_selector": "codex.base",
                        },
                    )
                assert surface() == before, label
            finally:
                receipt_path.write_bytes(original_receipt)
                run_json_path.write_bytes(original_run_json)

        # A reserved-looking future overlay exists in both the primary
        # checkout and the execution worktree, but it is not the identity the
        # recorded predecessor derives for this first turn.
        reserved = repo_root / "plans" / "in-progress" / "plan-cp01-v02.md"
        reserved.write_text(_REPAIR_PLAN, encoding="utf-8")
        reserved_mirror = (
            Path(
                json.loads(original_run_json.decode(encoding="utf-8"))[
                    "worktree_path"
                ]
            )
            / "plans" / "in-progress" / "plan-cp01-v02.md"
        )
        reserved_mirror.parent.mkdir(parents=True, exist_ok=True)
        reserved_mirror.write_text(_REPAIR_PLAN, encoding="utf-8")
        reject_case(
            "reserved-new-substitute",
            mutate_receipt=lambda r: r.__setitem__(
                "active_plan_path", str(reserved)
            ),
        )

        # An unrelated existing plan is not an inherited start either.
        unrelated = repo_root / "plans" / "in-progress" / "unrelated.md"
        unrelated.write_text(
            "# Unrelated\n\n### [ ] c\n- [ ] s\n", encoding="utf-8"
        )
        unrelated_mirror = (
            Path(
                json.loads(original_run_json.decode(encoding="utf-8"))[
                    "worktree_path"
                ]
            )
            / "plans" / "in-progress" / "unrelated.md"
        )
        unrelated_mirror.write_text(
            "# Unrelated\n\n### [ ] c\n- [ ] s\n", encoding="utf-8"
        )
        reject_case(
            "unrelated-substitute",
            mutate_receipt=lambda r: r.__setitem__(
                "active_plan_path", str(unrelated)
            ),
        )

        # Missing lineage: the recorded predecessor does not exist.
        reject_case(
            "missing-lineage",
            mutate_run=lambda run: run.__setitem__(
                "resumed_from_run_id", "missing-predecessor"
            ),
        )

        # Cyclic lineage: a self-referential predecessor proves nothing.
        reject_case(
            "cyclic-lineage",
            mutate_run=lambda run: run.__setitem__(
                "resumed_from_run_id", successor_dir.name
            ),
        )

        # Contradictory predecessor evidence: the source's final reviewer
        # receipt must keep creating the overlay its terminal record names.
        source_receipt_path = source_dir / "turns" / "turn-002" / "result.json"
        original_source_receipt = source_receipt_path.read_bytes()
        source_receipt = json.loads(
            original_source_receipt.decode(encoding="utf-8")
        )
        source_receipt["conditions"]["NEW_PLAN_EXISTS"] = False
        source_receipt_path.write_text(
            json.dumps(source_receipt), encoding="utf-8"
        )
        try:
            assert (
                _classify(successor_dir, config_path, repo_root) is None
            ), "contradicted-source-overlay"
            with pytest.raises(ValueError):
                _real_bootstrap(
                    repo_root, config_path, wf_config, successor_dir.name
                )
        finally:
            source_receipt_path.write_bytes(original_source_receipt)

        # Restored: admission returns everywhere and nothing was mutated.
        assert isinstance(
            _classify(successor_dir, config_path, repo_root), BudgetBoundary
        )
        assert receipt_path.read_bytes() == original_receipt
        assert run_json_path.read_bytes() == original_run_json
        restored_status = daemon.service.run_status(
            successor_dir.name, include_resume_preview=True
        )
        assert restored_status.evidence.get("can_resume") is True

        # Managed replacement admission of the intact successor creates one
        # bound continuation with the successor's own single-turn budget,
        # and both predecessors keep byte-identical evidence.
        successor_before = _run_dir_bytes(successor_dir)
        source_before = _run_dir_bytes(source_dir)
        continuation = daemon.service.resume(
            successor_dir.name,
            caller_scope="local",
            idempotency_key="recover-inherited-successor",
            recovery={
                "mode": "durable_evidence",
                "worker_selector": "codex.base",
            },
        )
        assert continuation.created is True
        assert continuation.run_id != successor_dir.name
        from aflow.control_plane.recovery import read_recovery_intent

        intent = read_recovery_intent(
            repo_root / ".aflow" / "runs" / continuation.run_id
        )
        assert intent.source_run_id == successor_dir.name
        assert intent.target_selector == "codex.base"
        successor_manifest = json.loads(
            (
                repo_root / ".aflow" / "launches" / f"{continuation.run_id}.json"
            ).read_text(encoding="utf-8")
        )
        assert successor_manifest["max_turns"] == 1
        assert _run_dir_bytes(successor_dir) == successor_before
        assert _run_dir_bytes(source_dir) == source_before


def test_classifier_rejects_reviewed_completion(tmp_path: Path) -> None:
    """A reviewed END at the cap is a genuine completion, not a boundary."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path = _make_repo(root)
        worktree_root = root / "worktrees"
        worktree_root.mkdir()
        config_path = _write_budget_config(
            root / "config",
            max_turns=2,
            worktree_root=worktree_root,
            workflows_text=_REVIEW_WORKFLOWS,
        )
        turns = 0

        def runner(argv, **kwargs):
            nonlocal turns
            turns += 1
            cwd = Path(kwargs["cwd"])
            if turns == 1:
                _write_plan(_plan_path_in(cwd), _COMPLETE_PLAN)
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with _DeliverySpies() as spies:
            result = _run_budget(
                config_path, repo_root, plan_path, runner, max_turns=2,
            )
        assert result.status == "completed"
        assert result.end_reason == "done"
        assert spies.merge_calls == 1
        assert _classify(result.run_dir, config_path, repo_root) is None


def test_classifier_rejects_tampered_budget_shapes(tmp_path: Path) -> None:
    """Receipt, worktree, and turn-count damage all reject the boundary."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, result = _make_source_pre_turn_cap(root)
        source_dir = result.run_dir
        run_json = json.loads((source_dir / "run.json").read_text(encoding="utf-8"))
        worktree_path = Path(run_json["worktree_path"])

        # Baseline accepts.
        assert _classify(source_dir, config_path, repo_root) is not None

        # A tampered receipt no longer agrees with the final snapshot.
        receipt_path = source_dir / "turns" / "turn-001" / "result.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        tampered = json.loads(json.dumps(receipt))
        tampered["snapshot_after"]["is_complete"] = (
            not tampered["snapshot_after"]["is_complete"]
        )
        receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
        assert _classify(source_dir, config_path, repo_root) is None
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

        # A run count that points past the last receipt rejects.
        run_json_path = source_dir / "run.json"
        broken = json.loads(json.dumps(run_json))
        broken["turns_completed"] = 2
        run_json_path.write_text(json.dumps(broken), encoding="utf-8")
        assert _classify(source_dir, config_path, repo_root) is None
        run_json_path.write_text(json.dumps(run_json), encoding="utf-8")

        # A missing worktree rejects the budget_exit shape.
        _run_git_in_test(["worktree", "remove", "--force", str(worktree_path)],
                         cwd=repo_root)
        assert _classify(source_dir, config_path, repo_root) is None


def test_auto_scan_never_classifies_budget_exit(tmp_path: Path) -> None:
    """Unrequested resume prompts never admit a budget boundary."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, result = _make_source_pre_turn_cap(root)
        source_dir = result.run_dir
        wf_config = load_workflow_config(config_path)

        # Automatic candidate scanning has no bootstrap and must not admit
        # the completed budget exit.
        assert _detect_resume_candidate(
            repo_root,
            wf_config,
            "live",
            plan_path,
            "base",
            None,
            1,
            (),
            requested_run_id=source_dir.name,
        ) is None

        # The explicit bootstrap path does admit it.
        bootstrap = _real_bootstrap(repo_root, config_path, wf_config, source_dir.name)
        context = _detect_resume_candidate(
            repo_root,
            wf_config,
            "live",
            plan_path,
            "base",
            None,
            1,
            (),
            requested_run_id=source_dir.name,
            resume_bootstrap=bootstrap,
        )
        assert context is not None
        assert isinstance(context.budget_continuation, BudgetBoundary)


def _make_source_historical(root: Path):
    """Build the narrow historical merge-failure shape from a real run."""
    repo_root, plan_path = _make_repo(root)
    worktree_root = root / "worktrees"
    worktree_root.mkdir()
    config_path = _write_budget_config(
        root / "config",
        max_turns=4,
        worktree_root=worktree_root,
        workflows_text=_HISTORICAL_WORKFLOWS,
    )
    turns = 0

    def source_runner(argv, **kwargs):
        nonlocal turns
        turns += 1
        cwd = Path(kwargs["cwd"])
        if turns == 1:
            # The original plan stays incomplete through the cap: this is the
            # historical shape, not a completed ledger awaiting review.
            _write_plan(_plan_path_in(cwd), _PARTIAL_PLAN)
        elif turns == 4:
            _write_plan(
                cwd / "plans" / "in-progress" / "plan-cp01-v02.md",
                _REPAIR_PLAN,
            )
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    with _DeliverySpies() as spies:
        result = _run_budget(
            config_path, repo_root, plan_path, source_runner, max_turns=4,
        )
    spies.assert_no_delivery()
    assert turns == 4
    assert result.status == "completed"
    assert result.end_reason == "max_turns_reached"

    source_dir = result.run_dir
    run_json_path = source_dir / "run.json"
    run_json = json.loads(run_json_path.read_text(encoding="utf-8"))
    # Rewrite the terminal record into the historical shape: a failed merge
    # teardown over an accepted 4-turn budget under a 48-turn invocation.
    run_json["status"] = "failed"
    run_json["max_turns"] = 48
    run_json["effective_max_turns"] = 4
    run_json["merge_status"] = "failed"
    run_json["merge_failure_reason"] = _HISTORICAL_MERGE_REASON
    run_json["failure_reason"] = "merge teardown failed"
    run_json["last_accepted_override"] = {
        "status": "accepted",
        "digest": "d" * 64,
        "message": "accepted",
        "max_turns": 4,
    }
    run_json_path.write_text(json.dumps(run_json), encoding="utf-8")
    return repo_root, plan_path, config_path, result, run_json


def test_historical_merge_failure_budget_boundary(tmp_path: Path) -> None:
    """The one historical shape resumes the repair with the accepted budget."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, result, run_json = (
            _make_source_historical(root)
        )
        source_dir = result.run_dir
        source_before = _run_dir_bytes(source_dir)
        wf_config = load_workflow_config(config_path)

        boundary = _classify(source_dir, config_path, repo_root)
        assert isinstance(boundary, BudgetBoundary)
        assert boundary.kind == "historical_merge_failure"
        assert boundary.next_step_name == "work"
        assert boundary.effective_max_turns == 4
        assert boundary.saved_max_turns == 48
        assert boundary.overlay_path is not None
        assert boundary.overlay_path.name == "plan-cp01-v02.md"

        # Without budget continuation the historical shape is not resumable.
        mismatch = _resume_candidate_mismatch_reason(
            run_json,
            wf_config,
            repo_root,
            "live",
            plan_path,
            "base",
            None,
            48,
            (),
            run_dir=source_dir,
        )
        assert mismatch is not None

        bootstrap = _real_bootstrap(repo_root, config_path, wf_config, source_dir.name)
        assert bootstrap.start_step == "work"
        # The accepted override budget (4), not the saved invocation (48).
        assert bootstrap.max_turns == 4
        context = bootstrap.resume_context
        assert isinstance(context.budget_continuation, BudgetBoundary)
        assert context.budget_continuation.kind == "historical_merge_failure"
        assert context.active_plan_path.name == "plan-cp01-v02.md"

        complete_repair = (
            "# Repair\n\n"
            "### [x] Checkpoint 1: Repair\n"
            "- [x] fix reviewer finding\n"
        )
        s_turns = 0

        def successor_runner(argv, **kwargs):
            nonlocal s_turns
            s_turns += 1
            cwd = Path(kwargs["cwd"])
            if s_turns == 1:
                _write_plan(_plan_path_in(cwd), _COMPLETE_PLAN)
                _write_plan(
                    cwd / "plans" / "in-progress" / "plan-cp01-v02.md",
                    complete_repair,
                )
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with _DeliverySpies() as spies:
            successor = _run_budget(
                config_path,
                repo_root,
                bootstrap.plan_path,
                successor_runner,
                max_turns=bootstrap.max_turns,
                resume=context,
            )

        assert s_turns == 2
        assert successor.status == "completed"
        assert successor.end_reason == "done"
        turn1 = json.loads(
            (
                successor.run_dir / "turns" / "turn-001" / "result.json"
            ).read_text(encoding="utf-8")
        )
        assert turn1["step_name"] == "work"
        assert spies.merge_calls == 1
        assert spies.deliver_calls == 1
        # The source run stayed byte-identical.
        assert _run_dir_bytes(source_dir) == source_before


def test_historical_shape_negatives_reject(tmp_path: Path) -> None:
    """Arbitrary merge failures, missing worktrees, and tampering reject."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, result, run_json = (
            _make_source_historical(root)
        )
        source_dir = result.run_dir
        run_json_path = source_dir / "run.json"

        # Baseline accepts.
        assert _classify(source_dir, config_path, repo_root) is not None

        # An arbitrary merge failure reason is not the canonical signature.
        broken = json.loads(json.dumps(run_json))
        broken["merge_failure_reason"] = "merge failed: unknown"
        run_json_path.write_text(json.dumps(broken), encoding="utf-8")
        assert _classify(source_dir, config_path, repo_root) is None
        run_json_path.write_text(json.dumps(run_json), encoding="utf-8")

        # A failed merge without merge_status is not the historical shape.
        broken = json.loads(json.dumps(run_json))
        del broken["merge_status"]
        run_json_path.write_text(json.dumps(broken), encoding="utf-8")
        assert _classify(source_dir, config_path, repo_root) is None
        run_json_path.write_text(json.dumps(run_json), encoding="utf-8")

        # A missing worktree rejects the historical shape.
        worktree_path = Path(run_json["worktree_path"])
        _run_git_in_test(
            ["worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_root,
        )
        assert _classify(source_dir, config_path, repo_root) is None


def test_historical_complete_original_with_overlay_rejects(tmp_path: Path) -> None:
    """A complete original (issue #58 shape) is not the historical boundary."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, result, run_json = (
            _make_source_historical(root)
        )
        source_dir = result.run_dir
        run_json_path = source_dir / "run.json"
        worktree_path = Path(run_json["worktree_path"])
        original_in_worktree = worktree_path / "plans" / "in-progress" / "plan.md"

        # Baseline: incomplete strict original agrees with the saved snapshot.
        assert _classify(source_dir, config_path, repo_root) is not None

        # The #58 shape: the on-disk original plan is complete while a
        # historical repair overlay differs from it.  The saved snapshot no
        # longer matches the validated on-disk original, so reject.
        original_in_worktree.write_text(_COMPLETE_PLAN, encoding="utf-8")
        assert _classify(source_dir, config_path, repo_root) is None

        # A different incomplete original also breaks the identity binding.
        other_partial = (
            "# Plan\n\n"
            "### [ ] Checkpoint 9: Other\n"
            "- [ ] step\n"
        )
        original_in_worktree.write_text(other_partial, encoding="utf-8")
        assert _classify(source_dir, config_path, repo_root) is None

        # A missing on-disk original rejects before any reservation.
        original_in_worktree.unlink()
        assert _classify(source_dir, config_path, repo_root) is None
        original_in_worktree.write_text(_PARTIAL_PLAN, encoding="utf-8")
        assert _classify(source_dir, config_path, repo_root) is not None

        # A saved complete original snapshot (kept consistent with the
        # finalized receipt) rejects even when the on-disk plan is complete:
        # the historical exception requires an incomplete strict original.
        broken = json.loads(json.dumps(run_json))
        broken["last_snapshot"]["is_complete"] = True
        run_json_path.write_text(json.dumps(broken), encoding="utf-8")
        receipt_path = source_dir / "turns" / "turn-004" / "result.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["snapshot_after"]["is_complete"] = True
        original_in_worktree.write_text(_COMPLETE_PLAN, encoding="utf-8")
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        assert _classify(source_dir, config_path, repo_root) is None


def test_tampered_receipt_identity_rejects_every_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contradicted receipt role/selector/conditions never reach a launch.

    The classifier, the daemon preview, the explicit bootstrap, and managed
    durable recovery all reject before any successor reservation or provider
    launch, while the untouched fixture stays admitted.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, result, run_json = (
            _make_source_historical(root)
        )
        source_dir = result.run_dir
        run_json_path = source_dir / "run.json"
        receipt_path = source_dir / "turns" / "turn-004" / "result.json"
        sibling_path = source_dir / "turns" / "turn-002" / "result.json"
        original_receipt = receipt_path.read_bytes()
        original_run_json = run_json_path.read_bytes()
        wf_config = load_workflow_config(config_path)
        _attach_launch_evidence(repo_root, source_dir.name, "failed")

        daemon = _make_daemon(tmp_path, monkeypatch, repo_root, config_path, wf_config)
        _patch_inactive_worker_evidence(
            monkeypatch, daemon.application.repository
        )

        def surface() -> tuple[list[str], list[str]]:
            return (
                sorted(
                    p.name
                    for p in (repo_root / ".aflow" / "runs").iterdir()
                    if p.is_dir()
                ),
                sorted(
                    p.name
                    for p in (repo_root / ".aflow" / "launches").iterdir()
                    if p.name.endswith(".json")
                    and not p.name.endswith(".state.json")
                ),
            )

        # Baseline: the intact fixture is admitted everywhere.
        assert isinstance(_classify(source_dir, config_path, repo_root), BudgetBoundary)
        status = daemon.service.run_status(
            source_dir.name, include_resume_preview=True
        )
        assert status.evidence.get("can_resume") is True
        bootstrap = _real_bootstrap(repo_root, config_path, wf_config, source_dir.name)
        assert bootstrap.start_step == "work"

        def reject_case(label: str, mutate) -> None:
            receipt = json.loads(original_receipt.decode(encoding="utf-8"))
            mutate(receipt)
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            try:
                assert _classify(source_dir, config_path, repo_root) is None, label
                tampered_status = daemon.service.run_status(
                    source_dir.name, include_resume_preview=True
                )
                assert tampered_status.evidence.get("can_resume") is False, label
                with pytest.raises(ValueError):
                    _real_bootstrap(
                        repo_root, config_path, wf_config, source_dir.name
                    )
                before = surface()
                with pytest.raises(
                    (DurableRecoveryRejection, DaemonError, ValueError)
                ):
                    daemon.service.resume(
                        source_dir.name,
                        caller_scope="local",
                        idempotency_key=f"tampered-{label}",
                        recovery={
                            "mode": "durable_evidence",
                            "worker_selector": "codex.base",
                        },
                    )
                assert surface() == before, label
            finally:
                receipt_path.write_bytes(original_receipt)

        # The finalized role must be the saved source step's configured role.
        reject_case("role", lambda r: r.__setitem__("step_role", "architect"))
        # The step identity itself cannot be swapped under a kept role.
        reject_case("step", lambda r: r.__setitem__("step_name", "work"))
        # The selector must agree with the earlier receipt of the same step.
        reject_case("selector", lambda r: r.__setitem__("selector", "codex.other"))
        # Missing selector identity rejects.
        reject_case("selector-missing", lambda r: r.__setitem__("selector", ""))
        # DONE is bound to the saved snapshot, MAX to the effective limit.
        reject_case(
            "done", lambda r: r["conditions"].__setitem__("DONE", True)
        )
        reject_case(
            "max",
            lambda r: r["conditions"].__setitem__("MAX_TURNS_REACHED", False),
        )

        # The receipt's plan identities are bound to the canonical run record.
        # An existing unrelated plan file (present in both the primary checkout
        # and the execution worktree) must not be admitted as the after-turn
        # new identity or the before-turn active identity: ownership is not
        # inferred from filenames or existence alone.
        unrelated_name = "unrelated.md"
        unrelated_repo = (
            repo_root / "plans" / "in-progress" / unrelated_name
        )
        unrelated_repo.parent.mkdir(parents=True, exist_ok=True)
        unrelated_text = "# Unrelated\n\n### [ ] c\n- [ ] s\n"
        unrelated_repo.write_text(unrelated_text, encoding="utf-8")
        unrelated_wt = (
            Path(run_json["worktree_path"])
            / "plans" / "in-progress" / unrelated_name
        )
        unrelated_wt.parent.mkdir(parents=True, exist_ok=True)
        unrelated_wt.write_text(unrelated_text, encoding="utf-8")

        # After-turn new: substituting an existing unrelated overlay rejects.
        reject_case(
            "new-substitute",
            lambda r: r.__setitem__("new_plan_path", str(unrelated_repo)),
        )
        # Before-turn active: substituting an existing unrelated plan rejects.
        reject_case(
            "active-substitute",
            lambda r: r.__setitem__("active_plan_path", str(unrelated_repo)),
        )
        # A stale earlier overlay must not qualify as the finalized turn's
        # starting active merely because earlier receipts recorded it: the
        # reserved new path of turns 1-3 appears in history, but the finalized
        # turn provably started from the canonical original plan.
        stale_overlay = repo_root / "plans" / "in-progress" / "plan-cp01-v01.md"
        reject_case(
            "active-stale-overlay",
            lambda r: r.__setitem__("active_plan_path", str(stale_overlay)),
        )

        # Separately contradict the canonical record: with the receipt left
        # intact, a canonical new/active identity that no longer agrees with
        # the receipt's plan is rejected on every surface too.
        def reject_canonical_case(label: str, mutate_run) -> None:
            run = json.loads(original_run_json.decode(encoding="utf-8"))
            mutate_run(run)
            run_json_path.write_text(json.dumps(run), encoding="utf-8")
            try:
                assert _classify(source_dir, config_path, repo_root) is None, label
                tampered_status = daemon.service.run_status(
                    source_dir.name, include_resume_preview=True
                )
                assert tampered_status.evidence.get("can_resume") is False, label
                with pytest.raises(ValueError):
                    _real_bootstrap(
                        repo_root, config_path, wf_config, source_dir.name
                    )
                before = surface()
                with pytest.raises(
                    (DurableRecoveryRejection, DaemonError, ValueError)
                ):
                    daemon.service.resume(
                        source_dir.name,
                        caller_scope="local",
                        idempotency_key=f"tampered-{label}",
                        recovery={
                            "mode": "durable_evidence",
                            "worker_selector": "codex.base",
                        },
                    )
                assert surface() == before, label
            finally:
                run_json_path.write_bytes(original_run_json)

        # Canonical new contradicts the intact receipt's new identity: the
        # after-turn new is bound exactly to the canonical recorded new path.
        reject_canonical_case(
            "canonical-new-contradict",
            lambda run: run.__setitem__("new_plan_path", str(unrelated_repo)),
        )
        # The canonical terminal active is the controller's own
        # post-transition selection for the finalized turn; redirecting it to
        # another recorded identity contradicts that evidence and rejects.
        reject_canonical_case(
            "canonical-active-contradict",
            lambda run: run.__setitem__("active_plan_path", str(unrelated_repo)),
        )
        # The intact fixture is still admitted after all the mutations revert.
        assert isinstance(
            _classify(source_dir, config_path, repo_root), BudgetBoundary
        )

        # A contradicted earlier receipt of the same step is saved selector
        # evidence too: the finalized selector may not silently diverge.
        original_sibling = sibling_path.read_bytes()
        sibling = json.loads(original_sibling.decode(encoding="utf-8"))
        sibling["selector"] = "codex.other"
        sibling_path.write_text(json.dumps(sibling), encoding="utf-8")
        try:
            assert _classify(source_dir, config_path, repo_root) is None
        finally:
            sibling_path.write_bytes(original_sibling)
        assert isinstance(_classify(source_dir, config_path, repo_root), BudgetBoundary)

        # Boolean numeric impostors: Python's ``True == 1`` and ``False == 0``
        # must not let a boolean receipt turn number, boolean active turn, or
        # boolean returncode pose as strict finalized-turn evidence.  The
        # single-turn budget exit is the shape where those impostors match.
        boolean_root = root / "single-turn-budget-exit"
        boolean_root.mkdir()
        b_repo_root, b_plan_path, b_config_path, b_result = (
            _make_source_pre_turn_cap(boolean_root)
        )
        b_source_dir = b_result.run_dir
        b_run_json_path = b_source_dir / "run.json"
        b_receipt_path = b_source_dir / "turns" / "turn-001" / "result.json"
        b_original_receipt = b_receipt_path.read_bytes()
        b_original_run_json = b_run_json_path.read_bytes()
        b_wf_config = load_workflow_config(b_config_path)
        _attach_launch_evidence(b_repo_root, b_source_dir.name, "completed")

        b_daemon = _make_daemon(
            boolean_root, monkeypatch, b_repo_root, b_config_path, b_wf_config
        )
        _patch_inactive_worker_evidence(
            monkeypatch, b_daemon.application.repository
        )

        def boolean_surface() -> tuple[list[str], list[str]]:
            return (
                sorted(
                    p.name
                    for p in (b_repo_root / ".aflow" / "runs").iterdir()
                    if p.is_dir()
                ),
                sorted(
                    p.name
                    for p in (b_repo_root / ".aflow" / "launches").iterdir()
                    if p.name.endswith(".json")
                    and not p.name.endswith(".state.json")
                ),
            )

        # Baseline: the intact single-turn fixture is admitted everywhere.
        assert isinstance(
            _classify(b_source_dir, b_config_path, b_repo_root), BudgetBoundary
        )
        b_status = b_daemon.service.run_status(
            b_source_dir.name, include_resume_preview=True
        )
        assert b_status.evidence.get("can_resume") is True
        b_bootstrap = _real_bootstrap(
            b_repo_root, b_config_path, b_wf_config, b_source_dir.name
        )
        assert b_bootstrap.start_step == "review"

        def reject_boolean_case(label: str, mutate) -> None:
            receipt = json.loads(b_original_receipt.decode(encoding="utf-8"))
            run_json = json.loads(b_original_run_json.decode(encoding="utf-8"))
            mutate(receipt, run_json)
            b_receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            b_run_json_path.write_text(json.dumps(run_json), encoding="utf-8")
            try:
                assert (
                    _classify(b_source_dir, b_config_path, b_repo_root) is None
                ), label
                tampered_status = b_daemon.service.run_status(
                    b_source_dir.name, include_resume_preview=True
                )
                assert tampered_status.evidence.get("can_resume") is False, label
                with pytest.raises(ValueError):
                    _real_bootstrap(
                        b_repo_root, b_config_path, b_wf_config, b_source_dir.name
                    )
                before = boolean_surface()
                with pytest.raises(
                    (DurableRecoveryRejection, DaemonError, ValueError)
                ):
                    b_daemon.service.resume(
                        b_source_dir.name,
                        caller_scope="local",
                        idempotency_key=f"tampered-{label}",
                        recovery={
                            "mode": "durable_evidence",
                            "worker_selector": "codex.base",
                        },
                    )
                assert boolean_surface() == before, label
            finally:
                b_receipt_path.write_bytes(b_original_receipt)
                b_run_json_path.write_bytes(b_original_run_json)

        reject_boolean_case(
            "turn-number-bool", lambda r, j: r.__setitem__("turn_number", True)
        )
        reject_boolean_case(
            "active-turn-bool", lambda r, j: j.__setitem__("active_turn", True)
        )
        reject_boolean_case(
            "returncode-bool", lambda r, j: r.__setitem__("returncode", False)
        )

        # False-NEW substitution (the reproduced #62 finding): the reserved
        # new plan exists in both the primary checkout and the worktree
        # mirror, and the finalized receipt claims that reserved path as its
        # starting active.  A known path is not proof it was active for this
        # turn: turn one provably starts from the canonical original plan, so
        # the substitution rejects before reservation/provider launch.
        b_reserved = b_repo_root / "plans" / "in-progress" / "plan-cp01-v01.md"
        b_reserved.write_text(_REPAIR_PLAN, encoding="utf-8")
        b_reserved_mirror = (
            Path(
                json.loads(b_original_run_json.decode(encoding="utf-8"))[
                    "worktree_path"
                ]
            )
            / "plans" / "in-progress" / "plan-cp01-v01.md"
        )
        b_reserved_mirror.parent.mkdir(parents=True, exist_ok=True)
        b_reserved_mirror.write_text(_REPAIR_PLAN, encoding="utf-8")

        def _false_new_substitution(receipt, run_json) -> None:
            del run_json
            receipt["active_plan_path"] = str(b_reserved)

        reject_boolean_case("active-false-new", _false_new_substitution)

        # Redirecting the canonical terminal active to the reserved overlay
        # contradicts the controller's own post-transition selection and
        # rejects as well.
        def _canonical_active_contradiction(receipt, run_json) -> None:
            del receipt
            run_json["active_plan_path"] = str(b_reserved)

        reject_boolean_case(
            "canonical-active-contradict", _canonical_active_contradiction
        )

        # The intact single-turn fixture admits again after every restore.
        assert isinstance(
            _classify(b_source_dir, b_config_path, b_repo_root), BudgetBoundary
        )
        assert b_run_json_path.read_bytes() == b_original_run_json

        # The run record itself was never modified by any rejected surface.
        assert run_json_path.read_bytes() == original_run_json


def _make_daemon(
    tmp_path: Path,
    monkeypatch,
    repo_root: Path,
    config_path: Path,
    wf_config,
) -> AflowDaemon:
    environment_file = tmp_path / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
    executable = tmp_path / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    # The daemon's live config refresh uses the fixture config, but the
    # resume bootstrap runs the real production path (only the config loader
    # is shared).
    monkeypatch.setattr(
        "aflow.daemon.load_workflow_config", lambda _path: wf_config
    )
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            environment={"PATH": str(executable.parent)},
            stop_timeout_seconds=0,
        ),
        units=InMemoryUnitManager(),
    )
    daemon.start()
    return daemon


def _attach_launch_evidence(
    repo_root: Path, run_id: str, phase: str, caller_scope: str = "local"
) -> None:
    """The direct controller already published the launch manifest; align its
    caller scope with the daemon caller under test and set the fixture phase.
    (A daemon-launched production run carries the daemon caller scope.)"""
    manifest_path = repo_root / ".aflow" / "launches" / f"{run_id}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["caller_scope"] = caller_scope
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_launch_phase(repo_root, run_id, phase)


def test_daemon_preview_and_resume_admit_budget_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon preview and managed resume admit a validated budget exit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, result = _make_source_pre_turn_cap(root)
        source_dir = result.run_dir
        source_before = _run_dir_bytes(source_dir)
        wf_config = load_workflow_config(config_path)
        _attach_launch_evidence(repo_root, source_dir.name, "completed")

        daemon = _make_daemon(tmp_path, monkeypatch, repo_root, config_path, wf_config)
        status = daemon.service.run_status(
            source_dir.name, include_resume_preview=True
        )
        assert status.status == "completed"
        assert status.evidence.get("can_resume") is True

        # Without the preview the field is absent entirely.
        plain = daemon.service.run_status(source_dir.name,
                                          include_resume_preview=False)
        assert "can_resume" not in plain.evidence

        continuation = daemon.service.resume(
            source_dir.name,
            caller_scope="local",
            idempotency_key="resume-budget-exit",
        )
        assert continuation.created is True
        assert continuation.run_id != source_dir.name
        # The source run stayed byte-identical.
        assert _run_dir_bytes(source_dir) == source_before


def test_daemon_preview_rejects_reviewed_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewed completion stays non-resumable in the daemon preview."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path = _make_repo(root)
        worktree_root = root / "worktrees"
        worktree_root.mkdir()
        config_path = _write_budget_config(
            root / "config",
            max_turns=2,
            worktree_root=worktree_root,
            workflows_text=_REVIEW_WORKFLOWS,
        )
        turns = 0

        def runner(argv, **kwargs):
            nonlocal turns
            turns += 1
            cwd = Path(kwargs["cwd"])
            if turns == 1:
                _write_plan(_plan_path_in(cwd), _COMPLETE_PLAN)
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with _DeliverySpies():
            result = _run_budget(
                config_path, repo_root, plan_path, runner, max_turns=2,
            )
        assert result.end_reason == "done"
        wf_config = load_workflow_config(config_path)
        _attach_launch_evidence(repo_root, result.run_dir.name, "completed")

        daemon = _make_daemon(tmp_path, monkeypatch, repo_root, config_path, wf_config)
        status = daemon.service.run_status(
            result.run_dir.name, include_resume_preview=True
        )
        assert status.status == "completed"
        assert status.evidence.get("can_resume") is False


def test_daemon_durable_recovery_of_historical_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Durable recovery admits the historical shape with a selected target."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, result, run_json = (
            _make_source_historical(root)
        )
        source_dir = result.run_dir
        source_before = _run_dir_bytes(source_dir)
        wf_config = load_workflow_config(config_path)
        _attach_launch_evidence(repo_root, source_dir.name, "failed")

        daemon = _make_daemon(tmp_path, monkeypatch, repo_root, config_path, wf_config)
        repository = daemon.application.repository
        original_get_status = repository.get_run_status

        def get_run_status_with_worker(*args, **kwargs):
            status = original_get_status(*args, **kwargs)
            return _replace_evidence(status, {"active": False, "exit_code": 17})

        monkeypatch.setattr(
            repository, "get_run_status", get_run_status_with_worker
        )
        status = daemon.service.run_status(
            source_dir.name, include_resume_preview=True
        )
        assert status.status == "failed"
        assert status.evidence.get("can_resume") is True

        # The source request-audit stream is append-only; its prior records
        # must remain intact (no resume/recovery record is appended to the
        # source by a durable recovery, which starts a successor stream).
        source_events_before = (source_dir / "events.jsonl").read_bytes()
        source_launch = (
            repo_root / ".aflow" / "launches" / f"{source_dir.name}.json"
        ).read_bytes()

        continuation = daemon.service.resume(
            source_dir.name,
            caller_scope="local",
            idempotency_key="recover-historical",
            recovery={
                "mode": "durable_evidence",
                "worker_selector": "codex.base",
            },
        )
        assert continuation.created is True
        assert continuation.run_id != source_dir.name

        # One successor, one launch.  The source launch manifest is unchanged
        # and exactly one new launch manifest appears for the successor.
        successor_launch = (
            repo_root / ".aflow" / "launches" / f"{continuation.run_id}.json"
        )
        assert successor_launch.is_file()
        assert (
            repo_root / ".aflow" / "launches" / f"{source_dir.name}.json"
        ).read_bytes() == source_launch
        launch_manifests = sorted(
            p.name
            for p in (repo_root / ".aflow" / "launches").iterdir()
            if p.name.endswith(".json") and not p.name.endswith(".state.json")
        )
        assert len(launch_manifests) == 2

        # Inherited effective budget: the successor is bounded by the
        # accepted effective max (4), not the source's saved 48.  A later
        # revisioned control may raise it; the source bytes are not touched.
        successor_manifest = json.loads(successor_launch.read_text(encoding="utf-8"))
        assert successor_manifest["max_turns"] == 4

        # The durable recovery intent binds the repair worker and the exact
        # worktree overlay as evidence, so the successor's first provider is
        # the selected target reading that overlay (no source reviewer rerun).
        from aflow.control_plane.recovery import read_recovery_intent

        intent = read_recovery_intent(
            repo_root / ".aflow" / "runs" / continuation.run_id
        )
        assert intent.source_run_id == source_dir.name
        assert intent.target_run_id == continuation.run_id
        assert intent.source_selector == "codex.base"
        assert intent.target_selector == "codex.base"
        overlay_refs = [
            ref.path
            for ref in intent.evidence
            if ref.path.startswith("worktree-file:")
        ]
        assert overlay_refs, "successor must be bound to the worktree overlay"

        # No premature merge and no source reviewer rerun: the source record
        # (run.json + finalized turn receipts) is byte-identical, and its
        # merge status is unchanged.
        assert _run_dir_bytes(source_dir) == source_before
        assert json.loads(
            (source_dir / "run.json").read_text(encoding="utf-8")
        )["merge_status"] == "failed"
        assert (
            (source_dir / "events.jsonl").read_bytes() == source_events_before
        )

        # The recovery audit record is appended to the successor's own
        # append-only stream, starting with recovery_requested.
        successor_events = [
            line
            for line in (
                repo_root / ".aflow" / "runs" / continuation.run_id / "events.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        assert successor_events, "successor audit stream is missing"
        assert json.loads(successor_events[0])["event_type"] == "recovery_requested"

        # Idempotent replay with the same key returns the same successor.
        replay = daemon.service.resume(
            source_dir.name,
            caller_scope="local",
            idempotency_key="recover-historical",
            recovery={
                "mode": "durable_evidence",
                "worker_selector": "codex.base",
            },
        )
        assert replay.run_id == continuation.run_id
        assert replay.created is False

        # A competing request under a different key is rejected: the source
        # already owns a successor (duplicate ownership).
        with pytest.raises((DaemonIdempotencyConflict, DaemonError)):
            daemon.service.resume(
                source_dir.name,
                caller_scope="local",
                idempotency_key="recover-historical-competing",
                recovery={
                    "mode": "durable_evidence",
                    "worker_selector": "codex.other",
                },
            )

        # The source stayed immutable through replay and the rejected race.
        assert _run_dir_bytes(source_dir) == source_before


def _patch_inactive_worker_evidence(monkeypatch, repository) -> None:
    """Direct-controller fixtures carry no worker record; give the admission
    path the same confirmed-inactive evidence the daemon-level fixture uses."""
    original_get_status = repository.get_run_status

    def get_run_status_with_worker(*args, **kwargs):
        status = original_get_status(*args, **kwargs)
        return _replace_evidence(status, {"active": False, "exit_code": 17})

    monkeypatch.setattr(repository, "get_run_status", get_run_status_with_worker)


def _replace_evidence(status, worker: dict):
    from dataclasses import replace as _replace

    return _replace(
        status,
        evidence={**status.evidence, "worker": worker},
    )


def test_daemon_durable_recovery_admits_completed_budget_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validated completed no-delivery budget exit admits replacement."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, result = _make_source_pre_turn_cap(root)
        source_dir = result.run_dir
        source_before = _run_dir_bytes(source_dir)
        wf_config = load_workflow_config(config_path)
        _attach_launch_evidence(repo_root, source_dir.name, "completed")

        daemon = _make_daemon(tmp_path, monkeypatch, repo_root, config_path, wf_config)
        _patch_inactive_worker_evidence(
            monkeypatch, daemon.application.repository
        )

        continuation = daemon.service.resume(
            source_dir.name,
            caller_scope="local",
            idempotency_key="recover-budget-exit",
            recovery={
                "mode": "durable_evidence",
                "worker_selector": "codex.base",
            },
        )
        assert continuation.created is True
        assert continuation.run_id != source_dir.name

        # One successor, one launch: exactly one new launch manifest appears
        # for the successor alongside the source's own manifest.
        successor_launch = (
            repo_root / ".aflow" / "launches" / f"{continuation.run_id}.json"
        )
        assert successor_launch.is_file()
        launch_manifests = sorted(
            p.name
            for p in (repo_root / ".aflow" / "launches").iterdir()
            if p.name.endswith(".json") and not p.name.endswith(".state.json")
        )
        assert len(launch_manifests) == 2

        # The replacement binds the selected worker; the successor inherits
        # the source's effective budget (1), not an inflated limit.
        from aflow.control_plane.recovery import read_recovery_intent

        intent = read_recovery_intent(
            repo_root / ".aflow" / "runs" / continuation.run_id
        )
        assert intent.source_run_id == source_dir.name
        assert intent.target_selector == "codex.base"
        successor_manifest = json.loads(successor_launch.read_text(encoding="utf-8"))
        assert successor_manifest["max_turns"] == 1

        # The predecessor's canonical evidence stayed byte-identical and no
        # audit record was appended to its stream.
        assert _run_dir_bytes(source_dir) == source_before


def test_daemon_durable_recovery_rejects_generic_completed_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewed completion has no budget boundary and rejects replacement."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        repo_root, plan_path = _make_repo(root)
        worktree_root = root / "worktrees"
        worktree_root.mkdir()
        config_path = _write_budget_config(
            root / "config",
            max_turns=2,
            worktree_root=worktree_root,
            workflows_text=_REVIEW_WORKFLOWS,
        )
        turns = 0

        def runner(argv, **kwargs):
            nonlocal turns
            turns += 1
            cwd = Path(kwargs["cwd"])
            if turns == 1:
                _write_plan(_plan_path_in(cwd), _COMPLETE_PLAN)
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with _DeliverySpies():
            result = _run_budget(
                config_path, repo_root, plan_path, runner, max_turns=2,
            )
        assert result.end_reason == "done"
        wf_config = load_workflow_config(config_path)
        _attach_launch_evidence(repo_root, result.run_dir.name, "completed")

        daemon = _make_daemon(tmp_path, monkeypatch, repo_root, config_path, wf_config)
        _patch_inactive_worker_evidence(
            monkeypatch, daemon.application.repository
        )

        runs_before = sorted(
            p.name for p in (repo_root / ".aflow" / "runs").iterdir() if p.is_dir()
        )
        with pytest.raises(DurableRecoveryRejection):
            daemon.service.resume(
                result.run_dir.name,
                caller_scope="local",
                idempotency_key="recover-generic-completed",
                recovery={
                    "mode": "durable_evidence",
                    "worker_selector": "codex.base",
                },
            )

        # No provider launch and no successor reservation: the run and launch
        # surfaces are unchanged.
        assert sorted(
            p.name for p in (repo_root / ".aflow" / "runs").iterdir() if p.is_dir()
        ) == runs_before
        launch_manifests = sorted(
            p.name
            for p in (repo_root / ".aflow" / "launches").iterdir()
            if p.name.endswith(".json") and not p.name.endswith(".state.json")
        )
        assert launch_manifests == [f"{result.run_dir.name}.json"]


def _fake_provider_bin(root: Path, provider_log: Path) -> Path:
    """Install a fake ``codex`` provider that logs argv, cwd, and prompt."""
    fake_bin = root / "fake-provider-bin"
    fake_bin.mkdir()
    (fake_bin / "codex").write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "if '--help' in argv:\n"
        "    if 'resume' in argv:\n"
        "        print('resume [SESSION_ID]')\n"
        "        print('-m, --model')\n"
        "    else:\n"
        "        print('--json resume')\n"
        "    sys.exit(0)\n"
        "entry = {'argv': argv, 'cwd': os.getcwd(),"
        " 'prompt': sys.stdin.read()}\n"
        f"with open({str(provider_log)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(entry) + '\\n')\n"
        "print(json.dumps({'type': 'agent_message',"
        " 'thread_id': 'fake-recovery-session', 'text': 'ok'}))\n",
        encoding="utf-8",
    )
    (fake_bin / "codex").chmod(0o755)
    return fake_bin


def _source_worktree_plan(run_dir: Path) -> tuple[Path, Path]:
    """Return the recorded execution worktree root and its active plan file."""
    worktree = Path(
        json.loads((run_dir / "run.json").read_text(encoding="utf-8"))[
            "worktree_path"
        ]
    )
    return worktree, worktree / "plans" / "in-progress" / "plan.md"


def test_durable_recovery_binds_worktree_active_plan_and_rejects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Durable recovery binds the worktree active plan, not the primary copy.

    An ordinary budget exit with a pending reviewer keeps both plan files:
    the committed primary original and the worker-edited worktree plan the
    successor reads.  Admission must bind the worktree file so that changing
    its bytes after admission rejects the prepared worker with
    ``recovery_evidence_unavailable`` before any provider launch, while the
    unchanged plan still reaches the pending reviewer.
    """
    with tempfile.TemporaryDirectory() as drift_tmp, \
            tempfile.TemporaryDirectory() as clean_tmp:
        from aflow.control_plane.recovery import read_recovery_intent
        from aflow.daemon import (
            _validate_recovery_evidence_for_worker,
            worker_main,
        )

        # --- Admission binds the worktree plan, not the primary copy -------
        root = Path(drift_tmp).resolve()
        repo_root, plan_path, config_path, result = _make_source_pre_turn_cap(
            root, plan_text=_CHANGED_INCOMPLETE_PLAN,
        )
        source_dir = result.run_dir
        source_before = _run_dir_bytes(source_dir)
        worktree, worktree_plan = _source_worktree_plan(source_dir)
        assert worktree_plan.is_file()
        # The boundary synced the worker edit back to the primary checkout, so
        # both files exist with the same changed bytes; only the worktree file
        # is the one the successor's provider reads and edits after admission.
        assert worktree_plan.read_bytes() != _VALID_PLAN.encode("utf-8")
        assert worktree_plan.read_bytes() == plan_path.read_bytes()
        primary_before = plan_path.read_bytes()

        wf_config = load_workflow_config(config_path)
        _attach_launch_evidence(repo_root, source_dir.name, "completed")
        daemon = _make_daemon(tmp_path, monkeypatch, repo_root, config_path, wf_config)
        _patch_inactive_worker_evidence(
            monkeypatch, daemon.application.repository
        )

        continuation = daemon.service.resume(
            source_dir.name,
            caller_scope="local",
            idempotency_key="recover-worktree-plan-drift",
            recovery={
                "mode": "durable_evidence",
                "worker_selector": "codex.base",
            },
        )
        assert continuation.created is True

        intent = read_recovery_intent(
            repo_root / ".aflow" / "runs" / continuation.run_id
        )
        bound = next(
            ref
            for ref in intent.evidence
            if ref.path == f"worktree-file:{worktree_plan}"
        )
        assert bound.sha256 == hashlib.sha256(
            worktree_plan.read_bytes()
        ).hexdigest()

        # The admitted intent revalidates while the worktree plan is intact.
        _evidence_paths, workspace_evidence = _validate_recovery_evidence_for_worker(
            repo_root, intent
        )
        assert workspace_evidence["workspace"] == worktree.resolve()

        # --- Post-admission plan drift rejects before any provider launch --
        provider_log = root / "provider-calls.jsonl"
        fake_bin = _fake_provider_bin(root, provider_log)
        monkeypatch.setenv(
            "PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
        )
        reservation = daemon.service._admission.reservation(continuation.run_id)
        assert reservation is not None
        monkeypatch.setenv(
            "AFLOW_ADMISSION_RESERVATION_NONCE", reservation.nonce
        )

        drifted = worktree_plan.read_bytes() + b"\n<!-- post-admission drift -->\n"
        worktree_plan.write_bytes(drifted)

        with pytest.raises(DurableRecoveryRejection) as rejected:
            _validate_recovery_evidence_for_worker(repo_root, intent)
        assert rejected.value.code == "recovery_evidence_unavailable"

        assert worker_main(
            repo_root=repo_root,
            config_path=config_path,
            run_id=continuation.run_id,
        ) == 1
        assert not provider_log.exists(), "drifted plan reached a provider"
        # Recovery mutated no source evidence and no workspace files.
        assert _run_dir_bytes(source_dir) == source_before
        assert plan_path.read_bytes() == primary_before
        assert worktree_plan.read_bytes() == drifted

        # --- The unchanged worktree plan still reaches the reviewer --------
        clean_root = Path(clean_tmp).resolve()
        clean_repo, _clean_plan, clean_config, clean_source = (
            _make_source_pre_turn_cap(
                clean_root, plan_text=_CHANGED_INCOMPLETE_PLAN,
            )
        )
        clean_source_dir = clean_source.run_dir
        clean_source_before = _run_dir_bytes(clean_source_dir)
        clean_worktree, clean_worktree_plan = _source_worktree_plan(
            clean_source_dir
        )
        _attach_launch_evidence(clean_repo, clean_source_dir.name, "completed")
        (tmp_path / "clean-daemon").mkdir()
        clean_daemon = _make_daemon(
            tmp_path / "clean-daemon",
            monkeypatch,
            clean_repo,
            clean_config,
            load_workflow_config(clean_config),
        )
        _patch_inactive_worker_evidence(
            monkeypatch, clean_daemon.application.repository
        )
        clean_continuation = clean_daemon.service.resume(
            clean_source_dir.name,
            caller_scope="local",
            idempotency_key="recover-worktree-plan-clean",
            recovery={
                "mode": "durable_evidence",
                "worker_selector": "codex.base",
            },
        )
        assert clean_continuation.created is True
        clean_reservation = clean_daemon.service._admission.reservation(
            clean_continuation.run_id
        )
        assert clean_reservation is not None
        monkeypatch.setenv(
            "AFLOW_ADMISSION_RESERVATION_NONCE", clean_reservation.nonce
        )

        clean_provider_log = clean_root / "provider-calls.jsonl"
        clean_fake_bin = _fake_provider_bin(clean_root, clean_provider_log)
        monkeypatch.setenv(
            "PATH", str(clean_fake_bin) + os.pathsep + os.environ.get("PATH", "")
        )
        assert worker_main(
            repo_root=clean_repo,
            config_path=clean_config,
            run_id=clean_continuation.run_id,
        ) == 0

        calls = [
            json.loads(line)
            for line in clean_provider_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        # The inherited effective budget (1) bounds the successor to the
        # single pending reviewer turn before its own pre-turn budget exit.
        assert len(calls) == 1
        assert Path(calls[0]["cwd"]) == clean_worktree
        assert str(clean_worktree_plan) in calls[0]["prompt"]

        successor_run_json = json.loads(
            (clean_repo / ".aflow" / "runs" / clean_continuation.run_id / "run.json")
            .read_text(encoding="utf-8")
        )
        # The successor continues the recorded execution worktree and its
        # first turn is the pending review, not a source reviewer rerun.
        assert successor_run_json["worktree_path"] == str(clean_worktree)
        turns_root = (
            clean_repo / ".aflow" / "runs" / clean_continuation.run_id / "turns"
        )
        assert sorted(p.name for p in turns_root.iterdir()) == ["turn-001"]
        first_receipt = json.loads(
            (turns_root / "turn-001" / "result.json").read_text(encoding="utf-8")
        )
        assert first_receipt["step_name"] == "review"
        assert first_receipt["selector"] == "codex.base"
        # The successor stopped at its own inherited budget with no merge.
        assert successor_run_json["status"] == "completed"
        assert successor_run_json["end_reason"] == "max_turns_reached"
        assert successor_run_json["effective_max_turns"] == 1
        assert "merge_status" not in successor_run_json
        # The source stayed immutable through admission and the successor boot.
        assert _run_dir_bytes(clean_source_dir) == clean_source_before


def test_mcp_durable_recovery_dispatch_of_historical_shape(
    tmp_path: Path,
    budget_temp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registered MCP resume tool dispatches durable-evidence recovery.

    Exercises the real tool registry and project routing down to one
    successor/one launch bound to the repair worker and exact overlay, with
    the predecessor evidence unchanged.
    """
    with tempfile.TemporaryDirectory(dir=budget_temp_parent) as tmpdir:
        assert Path(tmpdir).parent == budget_temp_parent
        root = Path(tmpdir).resolve()
        repo_root, plan_path, config_path, result, run_json = (
            _make_source_historical(root)
        )
        source_dir = result.run_dir
        source_before = _run_dir_bytes(source_dir)
        source_events_before = (source_dir / "events.jsonl").read_bytes()
        # The service maps the MCP transport to bearer:{project_id}, so the
        # fixture's already-published launch manifest carries that scope.
        _attach_launch_evidence(
            repo_root,
            source_dir.name,
            "failed",
            caller_scope="bearer:budget-recovery",
        )
        source_launch = (
            repo_root / ".aflow" / "launches" / f"{source_dir.name}.json"
        ).read_bytes()

        environment_file = tmp_path / "aflowd.env"
        environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
        executable = tmp_path / "release" / "bin" / "aflow"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        units = InMemoryUnitManager()

        registry = ProjectRegistry(root, root / "registry.json")
        registry.register("budget-recovery", "Fixture project", "repo")
        service = ControlPlaneService(
            registry,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            daemon_factory=lambda config: AflowDaemon(config, units=units),
            workflow_config_path=config_path,
        )

        # Route the registered project to its daemon and give the admission
        # path confirmed-inactive worker evidence for the direct source.
        service.run_status("budget-recovery", source_dir.name)
        repository = service._projects["budget-recovery"].daemon.application.repository
        _patch_inactive_worker_evidence(monkeypatch, repository)

        mcp = create_control_plane_mcp(lambda: service)
        payload = asyncio.run(
            mcp.call_tool(
                "resume_run",
                {
                    "project_id": "budget-recovery",
                    "run_id": source_dir.name,
                    "idempotency_key": "mcp-recover-historical",
                    "recovery": {
                        "mode": "durable_evidence",
                        "worker_selector": "codex.base",
                    },
                },
            )
        )
        assert payload.is_error is False
        body = json.loads(payload.content[0].text)
        successor_run_id = body["run_id"]
        assert body["created"] is True
        assert successor_run_id != source_dir.name

        # One successor, one launch: the source launch manifest is unchanged
        # and exactly one new launch manifest appears for the successor.
        successor_launch = (
            repo_root / ".aflow" / "launches" / f"{successor_run_id}.json"
        )
        assert successor_launch.is_file()
        assert (
            repo_root / ".aflow" / "launches" / f"{source_dir.name}.json"
        ).read_bytes() == source_launch
        launch_manifests = sorted(
            p.name
            for p in (repo_root / ".aflow" / "launches").iterdir()
            if p.name.endswith(".json") and not p.name.endswith(".state.json")
        )
        assert len(launch_manifests) == 2

        # Inherited effective budget: the successor is bounded by the
        # accepted effective max (4), not the source's saved 48.
        successor_manifest = json.loads(successor_launch.read_text(encoding="utf-8"))
        assert successor_manifest["max_turns"] == 4

        # The dispatched recovery binds the repair worker and the exact
        # worktree overlay; no source reviewer rerun is scheduled.
        from aflow.control_plane.recovery import read_recovery_intent

        intent = read_recovery_intent(
            repo_root / ".aflow" / "runs" / successor_run_id
        )
        assert intent.source_run_id == source_dir.name
        assert intent.target_run_id == successor_run_id
        assert intent.target_selector == "codex.base"
        overlay_refs = [
            ref.path
            for ref in intent.evidence
            if ref.path.startswith("worktree-file:")
        ]
        assert any(
            ref.endswith("plan-cp01-v02.md") for ref in overlay_refs
        ), "successor must be bound to the exact worktree overlay"

        # The predecessor's canonical evidence and audit stream are unchanged.
        assert _run_dir_bytes(source_dir) == source_before
        assert (source_dir / "events.jsonl").read_bytes() == source_events_before

        # Idempotent replay through the same registered tool returns the same
        # successor without a second launch.
        replay = asyncio.run(
            mcp.call_tool(
                "resume_run",
                {
                    "project_id": "budget-recovery",
                    "run_id": source_dir.name,
                    "idempotency_key": "mcp-recover-historical",
                    "recovery": {
                        "mode": "durable_evidence",
                        "worker_selector": "codex.base",
                    },
                },
            )
        )
        assert replay.is_error is False
        replay_body = json.loads(replay.content[0].text)
        assert replay_body["run_id"] == successor_run_id
        assert replay_body["created"] is False
        assert _run_dir_bytes(source_dir) == source_before

        # Boot the prepared successor through the fake owned unit's recorded
        # worker argv and the real canonical controller entry (worker_main ->
        # execute_workflow) to its first fake provider.  The inherited
        # effective budget (4) is proven behaviorally: the successor runs
        # exactly four new turns and stops at its own no-delivery budget exit.
        daemon = service._projects["budget-recovery"].daemon
        assert len(units.start_calls) == 1
        unit_name, worker_argv, worker_cwd = units.start_calls[0]
        assert unit_name == f"aflow-run-{successor_run_id}.service"
        assert worker_argv[1] == "daemon-worker"
        assert worker_argv[-1] == successor_run_id
        assert Path(worker_argv[worker_argv.index("--repo-root") + 1]) == repo_root
        assert Path(worker_argv[worker_argv.index("--config") + 1]) == config_path
        assert worker_cwd == repo_root

        fake_bin = root / "fake-provider-bin"
        fake_bin.mkdir()
        provider_log = root / "provider-calls.jsonl"
        (fake_bin / "codex").write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "argv = sys.argv[1:]\n"
            "if '--help' in argv:\n"
            "    if 'resume' in argv:\n"
            "        print('resume [SESSION_ID]')\n"
            "        print('-m, --model')\n"
            "    else:\n"
            "        print('--json resume')\n"
            "    sys.exit(0)\n"
            "entry = {'argv': argv, 'cwd': os.getcwd(),"
            " 'prompt': sys.stdin.read()}\n"
            f"with open({str(provider_log)!r}, 'a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps(entry) + '\\n')\n"
            "print(json.dumps({'type': 'agent_message',"
            " 'thread_id': 'fake-repair-session', 'text': 'ok'}))\n",
            encoding="utf-8",
        )
        (fake_bin / "codex").chmod(0o755)
        monkeypatch.setenv(
            "PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
        )
        reservation = daemon.service._admission.reservation(successor_run_id)
        assert reservation is not None
        monkeypatch.setenv("AFLOW_ADMISSION_RESERVATION_NONCE", reservation.nonce)

        from aflow.daemon import worker_main

        worker_returncode = worker_main(
            repo_root=repo_root,
            config_path=config_path,
            run_id=successor_run_id,
        )
        assert worker_returncode == 0, "successor worker boot failed"

        calls = [
            json.loads(line)
            for line in provider_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        successor_run_json = json.loads(
            (repo_root / ".aflow" / "runs" / successor_run_id / "run.json")
            .read_text(encoding="utf-8")
        )
        successor_worktree = Path(successor_run_json["worktree_path"])
        assert len(calls) == 4
        # The successor continues the recorded execution worktree, so every
        # provider turn runs inside it.
        assert all(Path(call["cwd"]) == successor_worktree for call in calls)

        # The first provider is the selected repair worker (codex.base ->
        # model-base) reading the exact overlay synced into the execution
        # worktree, introduced by the durable-recovery replacement brief; the
        # source run's finalized reviewer turn is never re-executed.
        overlay_in_successor = (
            successor_worktree / "plans" / "in-progress" / "plan-cp01-v02.md"
        )
        assert "--model" in calls[0]["argv"]
        assert calls[0]["argv"][calls[0]["argv"].index("--model") + 1] == "model-base"
        assert str(overlay_in_successor) in calls[0]["prompt"]
        assert "Durable provider-recovery evidence" in calls[0]["prompt"]
        assert source_dir.name in calls[0]["prompt"]

        turns_root = repo_root / ".aflow" / "runs" / successor_run_id / "turns"
        assert sorted(p.name for p in turns_root.iterdir()) == [
            f"turn-{number:03d}" for number in range(1, 5)
        ]
        first_receipt = json.loads(
            (turns_root / "turn-001" / "result.json").read_text(encoding="utf-8")
        )
        assert first_receipt["step_name"] == "work"
        assert first_receipt["step_role"] == "worker"
        assert first_receipt["selector"] == "codex.base"
        assert first_receipt["active_plan_path"].endswith("plan-cp01-v02.md")
        last_receipt = json.loads(
            (turns_root / "turn-004" / "result.json").read_text(encoding="utf-8")
        )
        assert last_receipt["chosen_transition"] == "END"
        assert last_receipt["chosen_transition_condition"] == "MAX_TURNS_REACHED"

        # The successor stopped at its own inherited budget with no merge
        # teardown, publication, or plan move.
        assert successor_run_json["status"] == "completed"
        assert successor_run_json["end_reason"] == "max_turns_reached"
        assert successor_run_json["max_turns"] == 4
        assert successor_run_json["effective_max_turns"] == 4
        assert "merge_status" not in successor_run_json
        assert overlay_in_successor.is_file()

        # Still one successor and one launch, and the predecessor's canonical
        # evidence and audit stream stayed byte-identical through the boot.
        assert len(units.start_calls) == 1
        assert (
            sorted(
                p.name
                for p in (repo_root / ".aflow" / "launches").iterdir()
                if p.name.endswith(".json") and not p.name.endswith(".state.json")
            )
            # Run-id suffixes are random, so sort the expectation too.
            == sorted(
                [f"{source_dir.name}.json", f"{successor_run_id}.json"]
            )
        )
        assert _run_dir_bytes(source_dir) == source_before
        assert (source_dir / "events.jsonl").read_bytes() == source_events_before
        assert json.loads(
            (source_dir / "run.json").read_text(encoding="utf-8")
        )["merge_status"] == "failed"
