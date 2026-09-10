from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aflow.api.models import (
    PreparedRun,
    StartupQuestion,
    StartupQuestionKind,
    StartupRequest,
)
from aflow.api.startup import StartupError, prepare_startup, prepare_startup_with_answer
from aflow.config import AflowSection, GoTransition, WorkflowConfig, WorkflowStepConfig, WorkflowUserConfig
from aflow.git_status import (
    RepoState,
    WorktreeInspectionError,
    WorktreePreflight,
    classify_status_items_by_prefix,
    parse_porcelain_status,
    preflight_worktree,
)
from aflow.workflow import (
    WorkflowError,
    _do_lifecycle_setup,
    _lifecycle_preflight,
    _lifecycle_preflight_git,
)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.parent.mkdir(parents=True, exist_ok=True)
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "AFlow test")
    _git(repo, "config", "core.excludesFile", "/dev/null")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


def _lifecycle_workflow() -> WorkflowConfig:
    return WorkflowConfig(
        steps={
            "step": WorkflowStepConfig(
                role="worker",
                go=(GoTransition(to="END"),),
            )
        },
        first_step="step",
        setup=("worktree", "branch"),
        teardown=("merge", "rm_worktree"),
        main_branch="main",
    )


def _startup_request(
    repo: Path,
    plan: Path,
    worktree_root: Path,
    *,
    workflow: WorkflowConfig | None = None,
) -> StartupRequest:
    workflow = workflow or _lifecycle_workflow()
    config = WorkflowUserConfig(
        aflow=AflowSection(default_workflow="test", worktree_root=str(worktree_root)),
        workflows={"test": workflow},
    )
    return StartupRequest(
        repo_root=repo,
        plan_path=plan,
        config_path=repo / "aflow.toml",
        workflow_config=config,
        workflow_name="test",
        start_step=None,
        max_turns=None,
        team=None,
    )


def test_nul_parser_preserves_newlines_and_rename_source() -> None:
    items = parse_porcelain_status(
        b" M line\nname.txt\0R  renamed name.txt\0old\nname.txt\0?? \xff.txt\0"
    )

    assert [item.path for item in items] == [
        "line\nname.txt",
        "renamed name.txt",
        "\udcff.txt",
    ]
    renamed = next(item for item in items if item.path == "renamed name.txt")
    assert renamed.original_path == "old\nname.txt"
    assert (renamed.index_status, renamed.worktree_status) == ("R", " ")


def test_preflight_lists_status_kinds_and_safe_path_exclusions(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    for name in ("modified.txt", "staged.txt", "deleted.txt", "rename.txt"):
        (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", "modified.txt", "staged.txt", "deleted.txt", "rename.txt")
    _git(repo, "commit", "-m", "status fixtures")

    (repo / "modified.txt").write_text("changed\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged change\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    (repo / "deleted.txt").unlink()
    _git(repo, "mv", "rename.txt", "renamed file.txt")
    (repo / "untracked\nname.txt").write_text("untracked\n", encoding="utf-8")
    (repo / "plans" / "in-progress").mkdir(parents=True)
    (repo / "plans" / "in-progress" / "plan file.md").write_text("plan\n", encoding="utf-8")
    (repo / ".aflow" / "runs").mkdir(parents=True)
    (repo / ".aflow" / "runs" / "run.json").write_text("{}\n", encoding="utf-8")

    result = preflight_worktree(repo, execution_mode="new_worktree")

    assert result.checkout_path == repo.resolve()
    assert result.execution_mode == "new_worktree"
    assert result.dirty is True
    assert result.requires_confirmation is True
    assert result.total_items == len(result.items)
    assert [item.path for item in result.items] == sorted(item.path for item in result.items)
    assert {item.path for item in result.items} >= {
        "modified.txt",
        "staged.txt",
        "deleted.txt",
        "renamed file.txt",
        "untracked\nname.txt",
        "plans/in-progress/plan file.md",
        ".aflow/runs/run.json",
    }
    renamed = next(item for item in result.items if item.path == "renamed file.txt")
    assert renamed.original_path == "rename.txt"
    assert (renamed.index_status, renamed.worktree_status) == ("R", " ")

    plan_paths, non_plan_paths = classify_status_items_by_prefix(
        result.items,
        ignore_lifecycle_owned=True,
    )
    assert "plans/in-progress/plan file.md" in plan_paths
    assert ".aflow/runs/run.json" not in non_plan_paths
    assert "untracked\nname.txt" in non_plan_paths


def test_new_worktree_preflight_does_not_confirm_plan_or_lifecycle_dirt(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "plans" / "in-progress").mkdir(parents=True)
    (repo / "plans" / "in-progress" / "plan.md").write_text("plan\n", encoding="utf-8")
    (repo / ".aflow" / "runs").mkdir(parents=True)
    (repo / ".aflow" / "runs" / "run.json").write_text("{}\n", encoding="utf-8")

    result = preflight_worktree(repo, execution_mode="new_worktree")

    assert result.dirty is True
    assert result.requires_confirmation is False
    assert result.blockers == ()

    same_checkout = preflight_worktree(repo, execution_mode="same_checkout")
    assert same_checkout.requires_confirmation is True

    clean_repo = _make_repo(tmp_path / "clean")
    clean = preflight_worktree(clean_repo, execution_mode="same_checkout")
    assert clean.dirty is False
    assert clean.requires_confirmation is False
    assert clean.total_items == 0


def test_preflight_reports_inspection_failure_instead_of_clean(tmp_path: Path) -> None:
    with pytest.raises(WorktreeInspectionError, match="git status inspection failed"):
        preflight_worktree(tmp_path / "not-a-repository")


def test_preflight_reports_conflicts_and_in_progress_operations(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    marker = Path(_git(repo, "rev-parse", "--git-dir").stdout.strip())
    if not marker.is_absolute():
        marker = repo / marker
    (marker / "MERGE_HEAD").write_text("synthetic\n", encoding="utf-8")
    try:
        result = preflight_worktree(repo, execution_mode="new_worktree")
        assert any("in-progress Git operation" in blocker for blocker in result.blockers)
    finally:
        (marker / "MERGE_HEAD").unlink()

    (repo / "conflict.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "conflict.txt")
    _git(repo, "commit", "-m", "conflict base")
    _git(repo, "checkout", "-b", "other")
    (repo / "conflict.txt").write_text("other\n", encoding="utf-8")
    _git(repo, "add", "conflict.txt")
    _git(repo, "commit", "-m", "other change")
    _git(repo, "checkout", "main")
    (repo / "conflict.txt").write_text("main\n", encoding="utf-8")
    _git(repo, "add", "conflict.txt")
    _git(repo, "commit", "-m", "main change")
    merge = _git(repo, "merge", "other", check=False)
    assert merge.returncode != 0
    try:
        result = preflight_worktree(repo)
        assert any("unresolved conflict: conflict.txt" in blocker for blocker in result.blockers)
    finally:
        _git(repo, "merge", "--abort", check=False)


def test_startup_dirty_worktree_confirmation_carries_ack_and_negative_aborts(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    plan = repo / "plans" / "in-progress" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n",
        encoding="utf-8",
    )
    note = repo / "notes.txt"
    note.write_bytes(b"owner bytes\n")
    request = _startup_request(repo, plan, tmp_path / "worktrees")
    before = _git(repo, "status", "--porcelain=v1", "-z").stdout

    question = prepare_startup(request)
    assert isinstance(question, StartupQuestion)
    assert question.kind == StartupQuestionKind.CONFIRM_WORKTREE_DIRTY
    assert "notes.txt" in question.message

    with pytest.raises(StartupError, match="aborted due to dirty worktree"):
        prepare_startup_with_answer(question, request, False)
    assert _git(repo, "status", "--porcelain=v1", "-z").stdout == before
    assert note.read_bytes() == b"owner bytes\n"

    prepared = prepare_startup_with_answer(question, request, True)
    assert isinstance(prepared, PreparedRun)
    assert prepared.dirty_worktree_confirmed is True
    assert note.read_bytes() == b"owner bytes\n"


def test_lifecycle_startup_defers_git_inspection_for_bootstrap_directory(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "new-project"
    repo.mkdir()
    plan = repo / "plans" / "in-progress" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n",
        encoding="utf-8",
    )

    request = _startup_request(repo, plan, tmp_path / "worktrees")

    prepared = prepare_startup(request)

    assert isinstance(prepared, PreparedRun)
    assert prepared.dirty_worktree_confirmed is False


def test_lifecycle_startup_keeps_existing_checkout_inspection_errors_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    plan = repo / "plans" / "in-progress" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n",
        encoding="utf-8",
    )
    request = _startup_request(repo, plan, tmp_path / "worktrees")

    def fail_preflight(*args: object, **kwargs: object) -> WorktreePreflight:
        raise WorktreeInspectionError("git status inspection failed: synthetic")

    monkeypatch.setattr("aflow.api.startup.preflight_worktree", fail_preflight)

    with pytest.raises(StartupError, match="worktree preflight inspection failed"):
        prepare_startup(request)


def test_acknowledgment_survives_prepared_daemon_and_runner_handoffs(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import patch

    from aflow.api.runner import RunnerConfig, WorkflowRunner
    from aflow.daemon import _prepared_from_payload, _prepared_payload

    workflow = _lifecycle_workflow()
    config = WorkflowUserConfig(
        aflow=AflowSection(worktree_root=str(tmp_path / "worktrees")),
        workflows={"test": workflow},
    )
    prepared = PreparedRun(
        workflow_name="test",
        repo_root=tmp_path,
        plan_path=tmp_path / "plan.md",
        config_path=tmp_path / "aflow.toml",
        max_turns=1,
        team=None,
        extra_instructions=(),
        start_step="step",
        dirty_worktree_confirmed=True,
    )

    payload = _prepared_payload(prepared)
    restored = _prepared_from_payload(
        payload,
        repo_root=tmp_path,
        config_path=prepared.config_path,
    )
    assert restored.dirty_worktree_confirmed is True

    with (
        patch(
            "aflow.live_config.load_live_config",
            return_value=SimpleNamespace(workflow_config=config),
        ),
        patch("aflow.api.runner.run_workflow", return_value=object()) as run,
    ):
        WorkflowRunner(RunnerConfig(prepared_run=restored)).run()

    assert run.call_args.kwargs["dirty_worktree_confirmed"] is True
    assert run.call_args.kwargs["config"].dirty_worktree_confirmed is True


def test_lifecycle_recheck_honors_ack_and_fresh_worktree_preserves_source_bytes(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    plan_path = repo / "plan.md"
    plan_path.write_text("# Plan\n\n### [ ] Checkpoint 1\n- [ ] step\n", encoding="utf-8")
    _git(repo, "add", "plan.md")
    _git(repo, "commit", "-m", "plan")
    note = repo / "owner.txt"
    source_bytes = b"source\x00bytes\n"
    workflow = _lifecycle_workflow()
    lifecycle_root = tmp_path / "worktrees"
    aflow = AflowSection(worktree_root=str(lifecycle_root))

    lifecycle = _lifecycle_preflight(
        repo,
        plan_path,
        workflow,
        aflow,
        RepoState.READY,
        dirty_worktree_confirmed=True,
    )
    assert lifecycle is not None

    note.write_bytes(source_bytes)
    with pytest.raises(WorkflowError, match="non-plan dirtiness"):
        _lifecycle_preflight_git(
            repo,
            lifecycle.main_branch,
            lifecycle.feature_branch,
            True,
            lifecycle.worktree_path,
        )
    _lifecycle_preflight_git(
        repo,
        lifecycle.main_branch,
        lifecycle.feature_branch,
        True,
        lifecycle.worktree_path,
        dirty_worktree_confirmed=True,
    )

    execution = _do_lifecycle_setup(repo, lifecycle)
    try:
        assert execution.execution_repo_root != repo
        assert note.read_bytes() == source_bytes
        assert plan_path.read_bytes() == b"# Plan\n\n### [ ] Checkpoint 1\n- [ ] step\n"
        assert not (execution.execution_repo_root / "owner.txt").exists()
    finally:
        _git(repo, "worktree", "remove", "--force", str(execution.execution_repo_root), check=False)
        _git(repo, "branch", "-D", lifecycle.feature_branch, check=False)


def test_preflight_ignores_git_operation_in_unselected_linked_worktree(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    plan = repo / "plan.md"
    plan.write_text(
        "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n",
        encoding="utf-8",
    )
    _git(repo, "add", "plan.md")
    _git(repo, "commit", "-m", "plan")
    (repo / "conflict.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "conflict.txt")
    _git(repo, "commit", "-m", "conflict base")

    _git(repo, "checkout", "-b", "other")
    (repo / "conflict.txt").write_text("other\n", encoding="utf-8")
    _git(repo, "add", "conflict.txt")
    _git(repo, "commit", "-m", "other change")
    _git(repo, "checkout", "main")
    (repo / "conflict.txt").write_text("main\n", encoding="utf-8")
    _git(repo, "add", "conflict.txt")
    _git(repo, "commit", "-m", "main change")
    _git(repo, "branch", "linked")

    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", str(linked), "linked")
    merge = _git(repo, "merge", "other", check=False)
    assert merge.returncode != 0

    try:
        linked_result = preflight_worktree(linked, execution_mode="same_checkout")
        assert linked_result.dirty is False
        assert linked_result.blockers == ()

        same_checkout_workflow = WorkflowConfig(
            steps={
                "step": WorkflowStepConfig(
                    role="worker",
                    go=(GoTransition(to="END"),),
                )
            },
            first_step="step",
        )
        prepared = prepare_startup(
            _startup_request(
                linked,
                linked / "plan.md",
                tmp_path / "worktrees",
                workflow=same_checkout_workflow,
            )
        )
        assert isinstance(prepared, PreparedRun)

        primary_result = preflight_worktree(repo, execution_mode="same_checkout")
        assert any(
            "unresolved conflict: conflict.txt" in blocker
            for blocker in primary_result.blockers
        )
        assert any(
            "in-progress Git operation (MERGE_HEAD exists)" in blocker
            for blocker in primary_result.blockers
        )
    finally:
        _git(repo, "merge", "--abort", check=False)
        _git(repo, "worktree", "remove", "--force", str(linked), check=False)
        _git(repo, "branch", "-D", "linked", check=False)
        _git(repo, "branch", "-D", "other", check=False)
