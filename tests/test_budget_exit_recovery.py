"""Budget-boundary delivery gating for #62.

Unfinished budget boundaries (pre-turn caps, budget-only END edges, and their
finalized replays) must exit without merge teardown, publication, or plan
moves, while preserving worktree, branch, plan, and review/repair evidence.
Reviewed completions keep delivering normally.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

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
        adapter=RecordingAdapter(),
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
        root = Path(tmpdir)
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
        root = Path(tmpdir)
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
        root = Path(tmpdir)
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
        root = Path(tmpdir)
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
        root = Path(tmpdir)
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
        root = Path(tmpdir)
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
