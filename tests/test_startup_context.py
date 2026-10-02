from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from aflow.config import (
    HarnessProfileConfig,
    WorkflowConfig,
    WorkflowHarnessConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
)
from aflow.api.models import PreparedRun, StartupQuestion, StartupQuestionKind, StartupRequest
from aflow.api.startup import PriorWorkStartupError, prepare_startup, prepare_startup_with_answer, StartupError
from aflow.control_plane import LaunchManifest, RunRepository, create_launch_manifest, project_plan_startup_context
from aflow.control_plane import startup_context as startup_module
from aflow.control_plane.models import RunPage
from aflow.control_plane.run_history import RunHistory
from aflow.plan_backups import create_plan_identity, move_plan_identity, plan_identity_for_path
from aflow.project_admission import ProjectAdmission
from aflow.api.startup import require_safe_fresh_worktree


def _plan(repo: Path, body: str) -> Path:
    path = repo / "plans" / "in-progress" / "plan.md"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    return path


def _tree_state(root: Path) -> dict[str, tuple[int, int, bytes | None]]:
    result = {}
    for path in root.rglob("*"):
        info = path.lstat()
        result[str(path.relative_to(root))] = (
            info.st_mtime_ns,
            info.st_ctime_ns,
            path.read_bytes() if path.is_file() and not path.is_symlink() else None,
        )
    return result


def test_partial_plan_without_run_directory_is_exact_and_read_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    done = "".join(f"### [x] Checkpoint {i}: Done {i}\n- [x] done\n" for i in range(1, 4))
    body = (
        "# Plan\n" + done
        + "### [ ] Checkpoint 4: Current\n- [x] completed task\n- [ ] remaining one\n- [ ] remaining two\n"
        + "### [ ] Checkpoint 5: Later\n- [ ] later\n"
        + "## User Acceptance Pending\n- [ ] device check\n"
    )
    path = _plan(repo, body)
    identity = create_plan_identity(repo, path)
    before = _tree_state(repo)

    context = project_plan_startup_context(repo, path)

    assert _tree_state(repo) == before
    assert not (repo / ".aflow").exists()
    assert context.availability == "available"
    assert context.plan_path == "plans/in-progress/plan.md"
    assert context.plan_identity == identity
    assert context.plan_revision == hashlib.sha256(body.encode()).hexdigest()
    assert context.total_checkpoints == 5
    assert context.recorded_complete_checkpoints == 3
    assert context.next_checkpoint is not None
    assert context.next_checkpoint.ordinal == 4
    assert context.next_checkpoint.checked_tasks == 1
    assert context.next_checkpoint.total_tasks == 3
    assert context.pending_tasks == ("remaining one", "remaining two")
    assert context.workflow_name is None
    assert context.selected_step is None
    assert context.recommendation is None
    assert "approved" not in json.dumps(context.to_dict()).lower()


def test_fences_and_other_sections_use_parser_scope(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    path = _plan(
        repo,
        "# Plan\n```\n### [x] Checkpoint 9: Fake\n- [ ] fake\n```\n"
        "### [ ] Checkpoint 1: Real\n- [ ] real one\n"
        "~~~\n- [ ] fake task\n~~~\n- [x] done\n"
        "## User Acceptance Pending\n- [ ] owner task\n"
        "### [ ] Not a checkpoint\n- [ ] another fake\n",
    )
    context = project_plan_startup_context(repo, path)
    assert context.total_checkpoints == 1
    assert context.next_checkpoint is not None
    assert context.next_checkpoint.total_tasks == 2
    assert context.pending_tasks == ("real one",)


def test_all_complete_has_no_next_checkpoint(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    path = _plan(repo, "### [x] Checkpoint 1: Done\n- [x] task\n")
    context = project_plan_startup_context(repo, path)
    assert context.availability == "available"
    assert context.recorded_complete_checkpoints == 1
    assert context.next_checkpoint is None
    assert context.pending_tasks == ()


def test_long_plan_keeps_exact_counts_and_late_next_checkpoint(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sections = []
    for index in range(1, 111):
        if index < 105:
            sections.append(f"### [x] Checkpoint {index}: Done\n- [x] task\n")
        elif index == 105:
            tasks = "".join(f"- [ ] task {i} {'🦋' * 100}\n" for i in range(7))
            sections.append(f"### [ ] Checkpoint {index}: {'🦋' * 100}\n" + tasks)
        else:
            sections.append(f"### [ ] Checkpoint {index}: Later\n- [ ] task\n")
    path = _plan(repo, "".join(sections))
    context = project_plan_startup_context(repo, path)
    assert context.total_checkpoints == 110
    assert context.recorded_complete_checkpoints == 104
    assert len(context.checkpoints) == 100
    assert context.checkpoint_outline_truncated
    assert context.next_checkpoint is not None
    assert context.next_checkpoint.ordinal == 105
    assert len(context.next_checkpoint.title.encode()) <= 256
    assert len(context.pending_tasks) == 5
    assert all(len(task.encode()) <= 256 for task in context.pending_tasks)
    assert context.pending_tasks_truncated
    assert context.text_truncated
    assert len(json.dumps(context.to_dict(), ensure_ascii=False).encode()) <= 48 * 1024
    selected = startup_module.with_startup_selection(
        context,
        workflow_name="managed",
        selected_step="implement",
        step_source="workflow_default",
    )
    assert selected.selected_step == "implement"
    assert len(json.dumps(selected.to_dict(), ensure_ascii=False).encode()) <= 48 * 1024


@pytest.mark.parametrize(
    ("body", "reason", "availability"),
    [
        ("# Notes\n- [ ] something\n", "non_checkpoint_plan", "not_applicable"),
        ("### [x] Checkpoint 1: Broken\n- [ ] task\n", "inconsistent_checkpoint_state", "partial"),
    ],
)
def test_non_checkpoint_and_inconsistent_plan_are_not_invented(
    tmp_path: Path, body: str, reason: str, availability: str
) -> None:
    repo = tmp_path / "repo"
    path = _plan(repo, body)
    context = project_plan_startup_context(repo, path)
    assert context.availability == availability
    assert reason in context.reason_codes
    if reason == "non_checkpoint_plan":
        assert context.next_checkpoint is None
        assert context.total_checkpoints is None
    else:
        # The inconsistent state is a display warning: the validated tolerant
        # recovery facts are carried, never invented counts.
        assert context.total_checkpoints == 1
        assert context.next_checkpoint is not None
        assert context.next_checkpoint.ordinal == 1
        assert context.next_checkpoint.heading_checked is True
        assert context.next_checkpoint.checked_tasks == 0
        assert context.next_checkpoint.total_tasks == 1
        assert context.pending_tasks == ("task",)
        assert context.recorded_complete_checkpoints == 1


def test_blank_task_text_retains_counts_but_omits_unreliable_text(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    path = _plan(repo, "### [ ] Checkpoint 1: Work\n- [ ] \n- [ ] named\n")
    context = project_plan_startup_context(repo, path)
    assert context.availability == "partial"
    assert context.next_checkpoint is not None
    assert context.next_checkpoint.total_tasks == 2
    assert context.pending_tasks == ()
    assert "pending_task_text_unavailable" in context.reason_codes


def test_missing_unsafe_invalid_and_oversized_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    path = _plan(repo, "### [ ] Checkpoint 1: Work\n- [ ] task\n")
    outside = tmp_path / "outside.md"
    outside.write_text(path.read_text())
    (path.parent / "link.md").symlink_to(outside)
    (repo / "linked-plans").symlink_to(path.parent, target_is_directory=True)
    assert project_plan_startup_context(repo, "plans/in-progress/missing.md").reason_codes == ("missing_plan",)
    for unsafe in (outside, path.parent / "link.md", "linked-plans/plan.md", "../outside.md"):
        context = project_plan_startup_context(repo, unsafe)
        assert context.availability == "unavailable"
        assert context.next_checkpoint is None
        assert context.reason_codes[0] in {"unsafe_plan_path", "unsafe_or_unreadable_plan"}
    path.write_bytes(b"\xff")
    assert project_plan_startup_context(repo, path).reason_codes == ("invalid_plan_encoding",)
    path.write_bytes(b"a" * (startup_module.MAX_PLAN_BYTES + 1))
    assert project_plan_startup_context(repo, path).reason_codes == ("plan_too_large",)


def test_concurrent_replacement_is_not_returned_as_stable_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    path = _plan(repo, "### [ ] Checkpoint 1: Original\n- [ ] task\n")
    original_read = startup_module._read_plan

    def replacing_read(root: Path, parts: tuple[str, ...], *, body: bool):
        if not body:
            replacement = path.with_suffix(".new")
            replacement.write_text("### [x] Checkpoint 1: Replaced\n- [x] task\n")
            os.replace(replacement, path)
        return original_read(root, parts, body=body)

    monkeypatch.setattr(startup_module, "_read_plan", replacing_read)
    context = project_plan_startup_context(repo, path)
    assert context.availability == "unavailable"
    assert context.reason_codes == ("plan_changed_during_read",)
    assert context.next_checkpoint is None
    assert context.plan_revision is None


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(("git", *args), cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _related_fixture(tmp_path: Path) -> tuple[Path, Path, Path, str, WorkflowConfig]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("base\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "base", cwd=repo)
    base = _git("rev-parse", "HEAD", cwd=repo)
    worktree = tmp_path / "previous-worktree"
    _git("worktree", "add", "-b", "previous-branch", str(worktree), "main", cwd=repo)
    body = (
        "# Plan\n## Git Tracking\n"
        "- Plan Branch: `previous-branch`\n"
        f"- Pre-Handoff Base HEAD: `{base}`\n"
        + "".join(f"### [x] Checkpoint {i}: Done\n- [x] task\n" for i in range(1, 4))
        + "### [ ] Checkpoint 4: Finish\n- [x] started\n- [ ] remaining\n"
    )
    plan = _plan(repo, body)
    workflow = WorkflowConfig(setup=("worktree", "branch"), main_branch="main")
    return repo, plan, worktree, base, workflow


def _record_previous(
    repo: Path, plan: Path, worktree: Path, *, run_id: str = "previous-run",
    status: str = "failed", branch: str = "previous-branch",
    identity: str | None = None, parent: str | None = None,
    original_path: Path | None = None,
) -> None:
    create_launch_manifest(repo, LaunchManifest(
        run_id=run_id, project_root=str(repo), plan_path=str(original_path or plan),
        workflow_name="checkpoint_delivery", max_turns=5,
        restarted_from_run_id=parent,
    ))
    run_dir = repo / ".aflow" / "runs" / run_id
    run_dir.mkdir(parents=True)
    metadata = {
        "schema_version": 1, "status": status, "repo_root": str(repo),
        "original_plan_path": str(original_path or plan), "current_step_name": "implement_plan",
        "feature_branch": branch, "main_branch": "main",
        "worktree_path": str(worktree), "execution_repo_root": str(worktree),
        "failure_reason": "Native macOS acceptance was previously unavailable.",
    }
    if identity:
        metadata["original_plan_identity"] = identity
    if parent:
        metadata["resumed_from_run_id"] = parent
    (run_dir / "run.json").write_text(json.dumps(metadata))


def _project_related(repo: Path, plan: Path, workflow: WorkflowConfig, **kwargs):
    return startup_module.project_startup_context(repo, plan, workflow=workflow, **kwargs)


def test_janny_shaped_previous_dirt_recommends_recovery_without_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    _record_previous(repo, plan, worktree)
    (worktree / "implementation.py").write_text("work = True\n")
    # A new reservation has no execution and must never become a related run.
    create_launch_manifest(repo, LaunchManifest(
        run_id="pending-run", project_root=str(repo), plan_path=str(plan),
        workflow_name="checkpoint_delivery", max_turns=5,
    ))
    (repo / ".aflow" / "project-admission.json").write_text("{\"sentinel\":true}\n")
    (repo / "config.toml").write_text("# current config\n")
    before = _tree_state(repo)
    admission = ProjectAdmission(repo)
    for method in ("snapshot", "plan_claims", "reconcile"):
        monkeypatch.setattr(admission, method, lambda: pytest.fail("preview reconciled admission"))

    context = _project_related(repo, plan, workflow, admission=admission, pending_run_id="pending-run")

    assert _tree_state(repo) == before
    assert context.next_checkpoint is not None and context.next_checkpoint.ordinal == 4
    assert context.recorded_complete_checkpoints == 3
    assert context.recommendation == "review_previous_run"
    assert context.related_runs_complete
    assert len(context.related_runs) == 1
    previous = context.related_runs[0]
    assert previous.run_id == "previous-run"
    assert previous.uncommitted_work is True
    assert previous.unmerged_work is False
    assert previous.worktree_verified and previous.branch_verified
    assert previous.failure_reason == "Native macOS acceptance was previously unavailable."
    assert previous.can_resume is None


def test_unmerged_commit_and_clean_integrated_work(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    _record_previous(repo, plan, worktree)
    (worktree / "implementation.py").write_text("work = True\n")
    _git("add", "implementation.py", cwd=worktree)
    _git("commit", "-m", "implementation", cwd=worktree)
    unmerged = _project_related(repo, plan, workflow)
    assert unmerged.recommendation == "review_previous_run"
    assert unmerged.related_runs[0].unmerged_work is True
    assert unmerged.related_runs[0].uncommitted_work is False

    _git("merge", "--ff-only", "previous-branch", cwd=repo)
    integrated = _project_related(repo, plan, workflow)
    assert integrated.recommendation == "start"
    assert integrated.related_runs[0].unmerged_work is False
    assert integrated.related_runs[0].uncommitted_work is False


def test_fresh_worktree_guard_rechecks_preserved_and_integrated_work(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    _record_previous(repo, plan, worktree)
    (worktree / "implementation.py").write_text("work = True\n")
    with pytest.raises(PriorWorkStartupError) as dirty:
        require_safe_fresh_worktree(repo, plan, workflow)
    assert dirty.value.code == "prior_work_requires_recovery"

    _git("add", "implementation.py", cwd=worktree)
    _git("commit", "-m", "implementation", cwd=worktree)
    with pytest.raises(PriorWorkStartupError) as unmerged:
        require_safe_fresh_worktree(repo, plan, workflow)
    assert unmerged.value.code == "prior_work_requires_recovery"

    _git("merge", "--ff-only", "previous-branch", cwd=repo)
    require_safe_fresh_worktree(repo, plan, workflow)

    _git("worktree", "remove", "--force", str(worktree), cwd=repo)
    _git("branch", "-D", "previous-branch", cwd=repo)
    # Removing both references makes the earlier baseline unverifiable.
    with pytest.raises(PriorWorkStartupError) as missing:
        require_safe_fresh_worktree(repo, plan, workflow)
    assert missing.value.code == "prior_work_unverified"


def test_startup_guard_cannot_be_bypassed_by_step_or_dirty_ack(tmp_path: Path) -> None:
    repo, plan, worktree, base, workflow = _related_fixture(tmp_path)
    workflow = replace(
        workflow,
        steps={
            "alpha": WorkflowStepConfig(role="worker", prompts=("p",)),
            "omega": WorkflowStepConfig(role="worker", prompts=("p",)),
        },
        first_step="alpha",
    )
    config = WorkflowUserConfig(workflows={"managed": workflow})
    request = StartupRequest(
        repo_root=repo, plan_path=plan, config_path=tmp_path / "config.toml",
        workflow_config=config, workflow_name="managed",
        start_step="omega", max_turns=None, team=None,
        extra_instructions=(), dirty_worktree_confirmed=True,
    )
    _record_previous(repo, plan, worktree)
    (worktree / "implementation.py").write_text("work = True\n")
    with pytest.raises(PriorWorkStartupError) as blocked:
        prepare_startup(request)
    assert blocked.value.code == "prior_work_requires_recovery"

    _git("add", "implementation.py", cwd=worktree)
    _git("commit", "-m", "implementation", cwd=worktree)
    _git("merge", "--ff-only", "previous-branch", cwd=repo)
    # A started plan also requires its separate Git Tracking base reconciliation.
    current_head = _git("rev-parse", "HEAD", cwd=repo)
    plan.write_text(
        plan.read_text().replace(
            f"Pre-Handoff Base HEAD: `{base}`",
            f"Pre-Handoff Base HEAD: `{current_head}`",
        )
    )
    prepared = prepare_startup(request)
    assert isinstance(prepared, PreparedRun)
    assert prepared.start_step == "omega"
    assert prepared.start_step_explicit is True

    implicit = prepare_startup(replace(request, start_step=None))
    assert isinstance(implicit, PreparedRun)
    assert implicit.start_step == "alpha"
    assert implicit.start_step_explicit is False


def test_inconsistent_plan_recovery_question_decline_and_confirm(tmp_path: Path) -> None:
    repo, plan, worktree, base, workflow = _related_fixture(tmp_path)
    workflow = replace(
        workflow,
        steps={
            "alpha": WorkflowStepConfig(role="worker", prompts=("p",)),
        },
        first_step="alpha",
    )
    body = (
        "# Plan\n## Git Tracking\n"
        "- Plan Branch: `previous-branch`\n"
        f"- Pre-Handoff Base HEAD: `{base}`\n"
        "### [x] Checkpoint 1: Done\n- [x] task\n"
        "### [x] Checkpoint 2: Broken\n- [ ] leftover task\n"
    )
    plan.write_text(body)

    # The preview carries the tolerant recovery facts with the explicit
    # warning, and with no prior work the recommendation is still start.
    preview = _project_related(repo, plan, workflow)
    assert preview.availability == "partial"
    assert preview.reason_codes[0] == "inconsistent_checkpoint_state"
    assert preview.recommendation == "start"
    assert preview.next_checkpoint is not None
    assert preview.next_checkpoint.ordinal == 2
    assert preview.next_checkpoint.heading_checked is True
    assert preview.pending_tasks == ("leftover task",)
    assert preview.plan_revision is not None

    # The daemon handoff / worker revalidation boundary admits a clean scan.
    require_safe_fresh_worktree(repo, plan, workflow)

    config = WorkflowUserConfig(
        workflows={"managed": workflow},
        harnesses={"test": WorkflowHarnessConfig(profiles={"default": HarnessProfileConfig(model="m")})},
        roles={"worker": "test.default"},
        prompts={"p": "do it"},
    )
    request = StartupRequest(
        repo_root=repo, plan_path=plan, config_path=tmp_path / "config.toml",
        workflow_config=config, workflow_name="managed",
        start_step=None, max_turns=None, team=None,
        extra_instructions=(), dirty_worktree_confirmed=True,
    )
    # Unconfirmed start reaches the existing explicit recovery question.
    question = prepare_startup(request)
    assert isinstance(question, StartupQuestion)
    assert question.kind == StartupQuestionKind.CONFIRM_RECOVERY

    # Decline stops.
    with pytest.raises(StartupError) as declined:
        prepare_startup_with_answer(question, request, False)
    assert "recovery" in str(declined.value).lower() and "declined" in str(declined.value).lower()

    # Confirmation prepares with retry evidence without rewriting the source.
    original = plan.read_bytes()
    prepared = prepare_startup_with_answer(question, request, True)
    assert isinstance(prepared, PreparedRun)
    assert prepared.startup_retry is not None
    assert isinstance(prepared.startup_retry.parse_error_str, str)
    assert prepared.startup_retry.parse_error_str
    assert plan.read_bytes() == original

    # Genuine prior dirty work still blocks even with confirmation.
    (tmp_path / "second").mkdir()
    second_repo, second_plan, second_worktree, second_base, _ = _related_fixture(
        tmp_path / "second"
    )
    _record_previous(second_repo, second_plan, second_worktree)
    (second_worktree / "implementation.py").write_text("work = True\n")
    second_plan.write_text(
        "# Plan\n## Git Tracking\n"
        "- Plan Branch: `previous-branch`\n"
        f"- Pre-Handoff Base HEAD: `{second_base}`\n"
        "### [x] Checkpoint 1: Broken\n- [ ] leftover task\n"
    )
    second_request = StartupRequest(
        repo_root=second_repo, plan_path=second_plan,
        config_path=tmp_path / "second" / "config.toml",
        workflow_config=config,
        workflow_name="managed",
        start_step=None, max_turns=None, team=None,
        extra_instructions=(), dirty_worktree_confirmed=True,
    )
    with pytest.raises(PriorWorkStartupError) as blocked:
        prepare_startup_with_answer(
            prepare_startup(second_request), second_request, True
        )
    assert blocked.value.code == "prior_work_requires_recovery"


def test_plan_only_dirt_is_not_implementation_work(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    _record_previous(repo, plan, worktree)
    synchronized_plan = worktree / "plans" / "in-progress" / "plan.md"
    synchronized_plan.parent.mkdir(parents=True)
    synchronized_plan.write_text(plan.read_text())
    context = _project_related(repo, plan, workflow)
    assert context.related_runs[0].uncommitted_work is False
    assert context.recommendation == "start"


def test_published_receipt_proves_integrated_work_after_worktree_removal(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    _record_previous(repo, plan, worktree, status="completed")
    (worktree / "implementation.py").write_text("work = True\n")
    _git("add", "implementation.py", cwd=worktree)
    _git("commit", "-m", "implementation", cwd=worktree)
    source = _git("rev-parse", "HEAD", cwd=worktree)
    _git("merge", "--ff-only", "previous-branch", cwd=repo)
    delivered = _git("rev-parse", "HEAD", cwd=repo)
    _git("worktree", "remove", "--force", str(worktree), cwd=repo)
    (repo / ".aflow" / "runs" / "previous-run" / "publication.json").write_text(json.dumps({
        "status": "published", "source_commit": source, "commit": delivered,
        "remote": "origin", "branch": "main",
    }))
    context = _project_related(repo, plan, workflow)
    assert context.recommendation == "start"
    assert context.related_runs[0].unmerged_work is False
    assert context.related_runs[0].uncommitted_work is False
    assert context.related_runs[0].branch_verified is True

    later_worktree = tmp_path / "later-worktree"
    _git("worktree", "add", str(later_worktree), "previous-branch", cwd=repo)
    (later_worktree / "implementation.py").write_text("work = 'later'\n")
    _git("add", "implementation.py", cwd=later_worktree)
    _git("commit", "-m", "later implementation", cwd=later_worktree)
    _git("worktree", "remove", str(later_worktree), cwd=repo)
    before = _tree_state(repo)

    advanced = _project_related(repo, plan, workflow)

    assert _tree_state(repo) == before
    assert advanced.recommendation == "review_previous_run"
    assert advanced.related_runs[0].worktree_verified is False
    assert advanced.related_runs[0].branch_verified is True
    assert advanced.related_runs[0].unmerged_work is True

    _git("branch", "-D", "previous-branch", cwd=repo)
    missing_ref = _project_related(repo, plan, workflow)
    assert missing_ref.recommendation == "inspect_previous_runs"
    assert missing_ref.related_runs[0].unmerged_work is None
    assert missing_ref.related_runs[0].uncommitted_work is None


def test_stable_identity_matches_moved_plan_and_rejects_conflicts(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    identity = create_plan_identity(repo, plan)
    old_path = repo / "plans" / "todo" / "plan.md"
    _record_previous(repo, plan, worktree, identity=identity, original_path=old_path)
    (worktree / "implementation.py").write_text("work = True\n")
    matched = _project_related(repo, plan, workflow)
    assert matched.recommendation == "review_previous_run"
    assert matched.related_runs[0].run_id == "previous-run"

    metadata_path = repo / ".aflow" / "runs" / "previous-run" / "run.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["original_plan_identity"] = "f" * 32
    metadata["original_plan_path"] = str(plan)
    metadata_path.write_text(json.dumps(metadata))
    mismatched = _project_related(repo, plan, workflow)
    assert mismatched.related_runs[0].run_id == "previous-run"
    assert mismatched.recommendation == "inspect_previous_runs"
    assert mismatched.availability == "partial"


def test_moved_plan_keeps_prior_work_evidence(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    identity = create_plan_identity(repo, plan)
    # Ordinary run metadata: the original path is recorded, no identity fields.
    _record_previous(repo, plan, worktree)
    (worktree / "implementation.py").write_text("work = True\n")
    failed_path = repo / "plans" / "failed" / "plan.md"
    failed_path.parent.mkdir(parents=True)
    failed_path.write_text(plan.read_text())
    plan.unlink()
    assert move_plan_identity(repo, source_plan_path=plan, destination_plan_path=failed_path)
    assert plan_identity_for_path(repo, failed_path) == identity

    # The moved plan's dirty retained work still blocks a fresh start.
    moved = _project_related(repo, failed_path, workflow)
    assert moved.recommendation == "review_previous_run"
    assert moved.related_runs[0].run_id == "previous-run"
    assert moved.related_runs[0].uncommitted_work is True

    # Clean, fully integrated work for the same moved plan allows a start.
    _git("add", "implementation.py", cwd=worktree)
    _git("commit", "-m", "implementation", cwd=worktree)
    _git("merge", "--ff-only", "previous-branch", cwd=repo)
    integrated = _project_related(repo, failed_path, workflow)
    assert integrated.recommendation == "start"
    assert integrated.related_runs[0].unmerged_work is False
    assert integrated.related_runs[0].uncommitted_work is False


def test_shared_alias_blocks_even_when_clean_and_integrated(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    # Identity A owns the path, then moves away.
    create_plan_identity(repo, plan)
    a_failed = repo / "plans" / "failed" / "a-plan.md"
    a_failed.parent.mkdir(parents=True)
    a_failed.write_text(plan.read_text())
    plan.unlink()
    assert move_plan_identity(repo, source_plan_path=plan, destination_plan_path=a_failed)
    # Identity B reuses A's former path and also moves away.
    b_plan = repo / "plans" / "in-progress" / "plan.md"
    b_plan.write_text(a_failed.read_text().replace("# Plan", "# Plan B"))
    identity_b = create_plan_identity(repo, b_plan)
    b_failed = repo / "plans" / "failed" / "b-plan.md"
    b_failed.write_text(b_plan.read_text())
    b_plan.unlink()
    assert move_plan_identity(repo, source_plan_path=b_plan, destination_plan_path=b_failed)
    assert plan_identity_for_path(repo, b_failed) == identity_b

    # Ordinary A-era run metadata records the alias now shared by A and B.
    _record_previous(repo, b_plan, worktree)
    (worktree / "implementation.py").write_text("work = True\n")
    dirty = _project_related(repo, b_failed, workflow)
    assert dirty.related_runs[0].run_id == "previous-run"
    assert dirty.recommendation == "inspect_previous_runs"
    assert dirty.availability == "partial"

    # Even clean, fully integrated work cannot make the shared alias certain.
    _git("add", "implementation.py", cwd=worktree)
    _git("commit", "-m", "implementation", cwd=worktree)
    _git("merge", "--ff-only", "previous-branch", cwd=repo)
    integrated = _project_related(repo, b_failed, workflow)
    assert integrated.related_runs[0].run_id == "previous-run"
    assert integrated.recommendation == "inspect_previous_runs"
    assert integrated.availability == "partial"


def test_reused_same_path_blocks_while_new_owner_remains(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    # Identity A owns plans/in-progress/plan.md, records an ordinary run
    # there, then moves away.
    create_plan_identity(repo, plan)
    _record_previous(repo, plan, worktree, run_id="a-era-run")
    a_failed = repo / "plans" / "failed" / "a-plan.md"
    a_failed.parent.mkdir(parents=True)
    a_failed.write_text(plan.read_text())
    plan.unlink()
    assert move_plan_identity(repo, source_plan_path=plan, destination_plan_path=a_failed)

    # Identity B is created at that same path and previewed while still there.
    b_plan = repo / "plans" / "in-progress" / "plan.md"
    b_plan.write_text(a_failed.read_text().replace("# Plan", "# Plan B"))
    create_plan_identity(repo, b_plan)

    # A-era dirty retained work blocks B's fresh start.
    (worktree / "implementation.py").write_text("work = True\n")
    dirty = _project_related(repo, b_plan, workflow)
    assert dirty.related_runs[0].run_id == "a-era-run"
    assert dirty.availability == "partial"
    assert dirty.recommendation == "inspect_previous_runs"
    with pytest.raises(PriorWorkStartupError) as dirty_guard:
        require_safe_fresh_worktree(repo, b_plan, workflow)
    assert dirty_guard.value.code == "prior_work_unverified"

    # Even clean, fully integrated A-era work stays uncertain: the recorded
    # path is owned by both identities, and Git Tracking cannot resolve that.
    _git("add", "implementation.py", cwd=worktree)
    _git("commit", "-m", "implementation", cwd=worktree)
    _git("merge", "--ff-only", "previous-branch", cwd=repo)
    integrated = _project_related(repo, b_plan, workflow)
    assert integrated.related_runs[0].run_id == "a-era-run"
    assert integrated.availability == "partial"
    assert integrated.recommendation == "inspect_previous_runs"
    with pytest.raises(PriorWorkStartupError) as integrated_guard:
        require_safe_fresh_worktree(repo, b_plan, workflow)
    assert integrated_guard.value.code == "prior_work_unverified"


def test_distinct_owner_run_is_excluded_from_other_plan_preview(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    # Identity A owns plans/in-progress/plan.md and records an ordinary run
    # there, then moves through the lifecycle; identity B owns a distinct
    # current path in the same repository. Neither path is reused.
    create_plan_identity(repo, plan)
    _record_previous(repo, plan, worktree)
    a_failed = repo / "plans" / "failed" / "a-plan.md"
    a_failed.parent.mkdir(parents=True)
    a_failed.write_text(plan.read_text())
    plan.unlink()
    assert move_plan_identity(repo, source_plan_path=plan, destination_plan_path=a_failed)

    # B's plan keeps A's Git Tracking section, so its metadata matches the
    # A-era run; matching Git Tracking must not create a related run.
    b_plan = repo / "plans" / "in-progress" / "b-plan.md"
    b_plan.write_text(a_failed.read_text())
    create_plan_identity(repo, b_plan)

    # A's dirty retained work must not block B: the recorded A-era path is
    # uniquely owned by A, so that run is unrelated to B's prior-work decision.
    (worktree / "implementation.py").write_text("work = True\n")
    dirty = _project_related(repo, b_plan, workflow)
    assert dirty.related_runs == ()
    assert dirty.recommendation == "start"
    require_safe_fresh_worktree(repo, b_plan, workflow)

    # Clean, fully integrated A work is likewise unrelated to B.
    _git("add", "implementation.py", cwd=worktree)
    _git("commit", "-m", "implementation", cwd=worktree)
    _git("merge", "--ff-only", "previous-branch", cwd=repo)
    integrated = _project_related(repo, b_plan, workflow)
    assert integrated.related_runs == ()
    assert integrated.recommendation == "start"


def test_explicit_conflicting_identity_over_shared_alias_blocks(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    identity_a = create_plan_identity(repo, plan)
    a_failed = repo / "plans" / "failed" / "a-plan.md"
    a_failed.parent.mkdir(parents=True)
    a_failed.write_text(plan.read_text())
    plan.unlink()
    assert move_plan_identity(repo, source_plan_path=plan, destination_plan_path=a_failed)
    b_plan = repo / "plans" / "in-progress" / "plan.md"
    b_plan.write_text(a_failed.read_text().replace("# Plan", "# Plan B"))
    create_plan_identity(repo, b_plan)
    b_failed = repo / "plans" / "failed" / "b-plan.md"
    b_failed.write_text(b_plan.read_text())
    b_plan.unlink()
    assert move_plan_identity(repo, source_plan_path=b_plan, destination_plan_path=b_failed)

    # A's explicit identity conflicts with B's current identity.
    _record_previous(repo, b_plan, worktree, identity=identity_a)
    (worktree / "implementation.py").write_text("work = True\n")
    blocked = _project_related(repo, b_failed, workflow)
    assert blocked.related_runs[0].run_id == "previous-run"
    assert blocked.recommendation == "inspect_previous_runs"
    assert blocked.availability == "partial"


def test_reused_alias_without_distinguishing_provenance_blocks(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    create_plan_identity(repo, plan)
    # A recorded path that is not part of the current plan's owned history.
    reused = repo / "plans" / "todo" / "plan.md"
    reused.parent.mkdir(parents=True)
    _record_previous(repo, plan, worktree, original_path=reused)
    (worktree / "implementation.py").write_text("work = True\n")
    failed_path = repo / "plans" / "failed" / "plan.md"
    failed_path.parent.mkdir(parents=True)
    failed_path.write_text(plan.read_text())
    plan.unlink()
    move_plan_identity(repo, source_plan_path=plan, destination_plan_path=failed_path)

    ambiguous = _project_related(repo, failed_path, workflow)
    assert ambiguous.related_runs[0].run_id == "previous-run"
    assert ambiguous.recommendation == "inspect_previous_runs"
    assert ambiguous.availability == "partial"


def test_same_name_different_plan_and_project_are_excluded(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    other_plan = repo / "plans" / "todo" / "plan.md"
    _record_previous(repo, plan, worktree, original_path=other_plan)
    assert _project_related(repo, plan, workflow).related_runs == ()
    other_repo = tmp_path / "other-project"
    other_repo.mkdir()
    _git("init", "-b", "main", cwd=other_repo)
    _git("config", "user.email", "test@example.com", cwd=other_repo)
    _git("config", "user.name", "Test", cwd=other_repo)
    (other_repo / "README.md").write_text("another project\n")
    _git("add", "README.md", cwd=other_repo)
    _git("commit", "-m", "base", cwd=other_repo)
    other = _plan(other_repo, plan.read_text())
    assert _project_related(other_repo, other, workflow).recommendation == "start"


def test_archived_is_readable_and_deleted_is_hidden_but_constrains_start(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    _record_previous(repo, plan, worktree)
    (worktree / "implementation.py").write_text("work = True\n")
    history = RunHistory(RunRepository(repo))
    history.mutate("previous-run", state="archived", expected_revision=0, idempotency_key="archive")
    archived = _project_related(repo, plan, workflow)
    assert archived.related_runs[0].history_state == "archived"
    history.mutate("previous-run", state="deleted", expected_revision=1, idempotency_key="delete")
    deleted = _project_related(repo, plan, workflow)
    assert deleted.related_runs == ()
    assert deleted.recommendation == "inspect_previous_runs"


def test_unknown_and_active_owners_take_precedence(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    _record_previous(repo, plan, worktree, status="running")
    unknown = _project_related(repo, plan, workflow)
    assert unknown.recommendation == "blocked"
    units = SimpleNamespace(get=lambda name: SimpleNamespace(name=name, is_active=True))
    active = _project_related(repo, plan, workflow, admission=ProjectAdmission(repo, unit_manager=units))
    assert active.recommendation == "open_existing_run"
    assert active.related_runs[0].activity == "active"


def test_validated_successor_collapses_but_independent_runs_stay_ambiguous(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    identity = create_plan_identity(repo, plan)
    _record_previous(repo, plan, worktree, identity=identity)
    _record_previous(repo, plan, worktree, run_id="successor-run", identity=identity, parent="previous-run")
    (worktree / "implementation.py").write_text("work = True\n")
    chain = _project_related(repo, plan, workflow)
    assert [item.run_id for item in chain.related_runs] == ["successor-run"]
    assert chain.recommendation == "review_previous_run"

    other_worktree = tmp_path / "independent-worktree"
    _git("worktree", "add", "-b", "independent-branch", str(other_worktree), "main", cwd=repo)
    _record_previous(repo, plan, other_worktree, run_id="independent-run", identity=identity, branch="independent-branch")
    ambiguous = _project_related(repo, plan, workflow)
    assert ambiguous.recommendation == "inspect_previous_runs"
    assert {item.run_id for item in ambiguous.related_runs} == {"successor-run", "independent-run"}


def test_active_predecessor_is_not_hidden_by_a_recorded_successor(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    identity = create_plan_identity(repo, plan)
    _record_previous(repo, plan, worktree, identity=identity, status="running")
    _record_previous(repo, plan, worktree, run_id="successor-run", identity=identity, parent="previous-run")
    units = SimpleNamespace(get=lambda name: SimpleNamespace(name=name, is_active="previous-run" in name))
    context = _project_related(repo, plan, workflow, admission=ProjectAdmission(repo, unit_manager=units))
    assert context.recommendation == "open_existing_run"
    assert {item.run_id for item in context.related_runs} == {"previous-run", "successor-run"}


def test_missing_worktree_timeout_and_incomplete_discovery_stay_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    _record_previous(repo, plan, worktree)
    _git("worktree", "remove", "--force", str(worktree), cwd=repo)
    missing = _project_related(repo, plan, workflow)
    assert missing.recommendation == "inspect_previous_runs"
    assert missing.availability == "partial"
    assert missing.related_runs[0].unmerged_work is False
    assert missing.related_runs[0].uncommitted_work is None

    _git("worktree", "add", str(worktree), "previous-branch", cwd=repo)
    original_git = startup_module._git
    monkeypatch.setattr(startup_module, "_git", lambda root, *args: (-1, b"") if args and args[0] == "status" else original_git(root, *args))
    timed_out = _project_related(repo, plan, workflow)
    assert timed_out.related_runs[0].uncommitted_work is None
    assert timed_out.recommendation == "inspect_previous_runs"
    assert "prior_work_unverified" in timed_out.reason_codes
    monkeypatch.setattr(startup_module, "_git", original_git)

    original_list = RunRepository.list_runs
    def truncated_list(self, **kwargs):
        page = original_list(self, **kwargs)
        return RunPage(runs=page.runs, next_cursor="more")
    monkeypatch.setattr(RunRepository, "list_runs", truncated_list)
    truncated = _project_related(repo, plan, workflow)
    assert truncated.related_runs_complete is False
    assert truncated.recommendation == "inspect_previous_runs"


def test_missing_branch_and_unreadable_run_evidence_do_not_suggest_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    _record_previous(repo, plan, worktree)
    original_git = startup_module._git
    monkeypatch.setattr(
        startup_module, "_git",
        lambda root, *args: (-1, b"") if args[:2] == ("rev-parse", "--verify") and "previous-branch" in args[-1] else original_git(root, *args),
    )
    missing_ref = _project_related(repo, plan, workflow)
    assert missing_ref.recommendation == "inspect_previous_runs"
    assert missing_ref.related_runs[0].unmerged_work is None
    monkeypatch.setattr(startup_module, "_git", original_git)
    (repo / ".aflow" / "runs" / "previous-run" / "run.json").write_text("{invalid")
    malformed = _project_related(repo, plan, workflow)
    assert malformed.related_runs_complete is False
    assert malformed.recommendation == "inspect_previous_runs"


def test_resume_is_previewed_only_for_the_single_related_leaf(tmp_path: Path) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    _record_previous(repo, plan, worktree)
    (worktree / "implementation.py").write_text("work = True\n")
    repository = RunRepository(repo)
    resume_calls: list[str] = []
    daemon = SimpleNamespace(
        _application=SimpleNamespace(repository=repository, units=SimpleNamespace(get=lambda name: None)),
        run_status=lambda run_id, **kwargs: repository.get_run_status(run_id, include_progress=False),
        _can_resume=lambda status: resume_calls.append(status.run_id) or True,
    )
    context = _project_related(repo, plan, workflow, daemon=daemon)
    assert context.related_runs[0].can_resume is True
    assert resume_calls == ["previous-run"]


def test_cold_preview_reads_without_admission_reconciliation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, plan, worktree, _, workflow = _related_fixture(tmp_path)
    _record_previous(repo, plan, worktree)
    (repo / ".aflow" / "project-admission.json").write_text("{\"sentinel\":true}\n")
    (repo / "config.toml").write_text("# config\n")
    before = _tree_state(repo)
    for method in ("snapshot", "plan_claims", "reconcile"):
        monkeypatch.setattr(ProjectAdmission, method, lambda self: pytest.fail("preview reconciled admission"))
    context = _project_related(repo, plan, workflow)
    assert context.related_runs[0].run_id == "previous-run"
    assert _tree_state(repo) == before


def test_malformed_plan_identity_metadata_does_not_prove_no_prior_work(tmp_path: Path) -> None:
    repo, plan, _, _, workflow = _related_fixture(tmp_path)
    identity = create_plan_identity(repo, plan)
    owner_path = repo / "plans" / "backups" / ".provenance" / ".owners" / f"{identity}.json"
    owner_path.write_text("{invalid")
    context = _project_related(repo, plan, workflow)
    assert context.recommendation == "blocked"
    assert context.related_runs_complete is False


def test_existing_checkout_workflow_keeps_normal_startup_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    plan = _plan(repo, "### [ ] Checkpoint 1: Work\n- [ ] task\n")
    context = _project_related(repo, plan, WorkflowConfig(setup=()))
    assert context.recommendation == "start"
    assert context.related_runs_complete is None


def _alias_related_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, str, WorkflowConfig]:
    """A repository under a regular directory plus a symlinked parent alias.

    The repository itself is not a symlink; only the parent directory has an
    alias spelling, reproducing the macOS ``/var`` versus ``/private/var``
    difference on Linux.
    """
    real_root = tmp_path / "real"
    real_root.mkdir()
    repo = real_root / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("base\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "base", cwd=repo)
    base = _git("rev-parse", "HEAD", cwd=repo)
    worktree = real_root / "previous-worktree"
    _git("worktree", "add", "-b", "previous-branch", str(worktree), "main", cwd=repo)
    body = (
        "# Plan\n## Git Tracking\n"
        "- Plan Branch: `previous-branch`\n"
        f"- Pre-Handoff Base HEAD: `{base}`\n"
        + "".join(f"### [x] Checkpoint {i}: Done\n- [x] task\n" for i in range(1, 4))
        + "### [ ] Checkpoint 4: Finish\n- [x] started\n- [ ] remaining\n"
    )
    plan = _plan(repo, body)
    workflow = WorkflowConfig(setup=("worktree", "branch"), main_branch="main")
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_root, target_is_directory=True)
    alias_repo = alias_parent / "repo"
    # A directory symlink below the project root that points back to the
    # root itself. It is a descendant of the root, never a root spelling.
    (repo / "return-to-root").symlink_to(repo, target_is_directory=True)
    return repo, plan, worktree, alias_repo, base, workflow


def _write_run_metadata(
    repo: Path, *, run_id: str = "previous-run", repo_root: Path, plan: Path,
    worktree: Path, branch: str = "previous-branch", identity: str | None = None,
) -> None:
    run_dir = repo / ".aflow" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema_version": 1, "status": "failed", "repo_root": str(repo_root),
        "original_plan_path": str(plan), "current_step_name": "implement_plan",
        "feature_branch": branch, "main_branch": "main",
        "worktree_path": str(worktree), "execution_repo_root": str(worktree),
        "failure_reason": "Earlier work could not be located.",
    }
    if identity:
        metadata["original_plan_identity"] = identity
    (run_dir / "run.json").write_text(json.dumps(metadata))


def test_alias_spelled_manifest_recorded_run_stays_related(tmp_path: Path) -> None:
    repo, plan, worktree, alias_repo, _, workflow = _alias_related_fixture(tmp_path)
    alias_plan = alias_repo / "plans" / "in-progress" / "plan.md"
    alias_worktree = alias_repo.parent / "previous-worktree"
    # Manifest-backed run: manifest uses the alias project root, the metadata
    # mixes canonical root with alias plan and worktree spellings.
    create_launch_manifest(repo, LaunchManifest(
        run_id="previous-run", project_root=str(alias_repo), plan_path=str(alias_plan),
        workflow_name="checkpoint_delivery", max_turns=5,
    ))
    _write_run_metadata(repo, repo_root=repo, plan=alias_plan, worktree=alias_worktree)
    (worktree / "implementation.py").write_text("work = True\n")
    context = _project_related(repo, plan, workflow)
    assert context.recommendation == "review_previous_run"
    assert context.related_runs_complete
    previous = context.related_runs[0]
    assert previous.run_id == "previous-run"
    assert previous.uncommitted_work is True
    assert previous.unmerged_work is False
    assert previous.worktree_verified and previous.branch_verified


def test_metadata_only_alias_recorded_run_stays_related(tmp_path: Path) -> None:
    repo, plan, worktree, alias_repo, _, workflow = _alias_related_fixture(tmp_path)
    alias_plan = alias_repo / "plans" / "in-progress" / "plan.md"
    alias_worktree = alias_repo.parent / "previous-worktree"
    # Metadata-only retained run: no launch manifest, every recorded path uses
    # the alias spelling.
    _write_run_metadata(repo, repo_root=alias_repo, plan=alias_plan, worktree=alias_worktree)
    (worktree / "implementation.py").write_text("work = True\n")
    context = _project_related(repo, plan, workflow)
    assert context.recommendation == "review_previous_run"
    assert context.related_runs_complete
    previous = context.related_runs[0]
    assert previous.run_id == "previous-run"
    assert previous.uncommitted_work is True
    assert previous.worktree_verified and previous.branch_verified


def test_alias_recorded_run_mixed_canonical_manifest_stays_related(tmp_path: Path) -> None:
    repo, plan, worktree, alias_repo, _, workflow = _alias_related_fixture(tmp_path)
    alias_plan = alias_repo / "plans" / "in-progress" / "plan.md"
    # Canonical manifest project root with alias-spelled metadata paths.
    create_launch_manifest(repo, LaunchManifest(
        run_id="previous-run", project_root=str(repo), plan_path=str(alias_plan),
        workflow_name="checkpoint_delivery", max_turns=5,
    ))
    _write_run_metadata(repo, repo_root=alias_repo, plan=alias_plan, worktree=alias_repo.parent / "previous-worktree")
    (worktree / "implementation.py").write_text("work = True\n")
    context = _project_related(repo, plan, workflow)
    assert context.recommendation == "review_previous_run"
    assert context.related_runs[0].run_id == "previous-run"
    assert context.related_runs[0].uncommitted_work is True


def test_alias_normalization_excludes_distinct_projects_and_paths(tmp_path: Path) -> None:
    repo, plan, worktree, alias_repo, _, workflow = _alias_related_fixture(tmp_path)
    # A different project with the same basename must not match by name or by
    # alias normalization.
    other_real = tmp_path / "other"
    other_real.mkdir()
    other = other_real / "repo"
    other.mkdir()
    _write_run_metadata(repo, repo_root=other, plan=plan, worktree=worktree)
    context = _project_related(repo, plan, workflow)
    assert context.related_runs == ()
    assert context.recommendation == "start"

    # A sibling path sharing a textual prefix of the verified root is a
    # distinct directory identity, not an alias.
    (repo / ".aflow" / "runs" / "previous-run" / "run.json").unlink()
    sibling = repo.parent / "repo-two"
    sibling.mkdir()
    _write_run_metadata(repo, repo_root=sibling, plan=plan, worktree=worktree)
    context = _project_related(repo, plan, workflow)
    assert context.related_runs == ()
    assert context.recommendation == "start"

    # Traversal components are rejected before any normalization.
    (repo / ".aflow" / "runs" / "previous-run" / "run.json").unlink()
    sibling.rmdir()
    other.rmdir()
    other_real.rmdir()
    _write_run_metadata(
        repo, repo_root=repo, plan=alias_repo / "plans" / ".." / "in-progress" / "plan.md",
        worktree=worktree,
    )
    context = _project_related(repo, plan, workflow)
    assert context.related_runs == ()
    assert context.recommendation == "start"


def test_descendant_symlinks_do_not_gain_exact_match_authority(tmp_path: Path) -> None:
    repo, plan, worktree, alias_repo, _, workflow = _alias_related_fixture(tmp_path)
    # A symlinked plan file beneath the root keeps its recorded name; the
    # target never gains exact-match authority for the current plan path.
    link = repo / "plans" / "in-progress" / "alt.md"
    link.symlink_to(plan)
    _write_run_metadata(repo, repo_root=repo, plan=alias_repo / "plans" / "in-progress" / "alt.md", worktree=worktree)
    context = _project_related(repo, plan, workflow)
    assert context.related_runs == ()
    assert context.recommendation == "start"
    link.unlink()
    (repo / ".aflow" / "runs" / "previous-run" / "run.json").unlink()

    # A symlinked directory below the root is compared by its recorded
    # spelling, not by its target.
    (repo / "plans-linked").symlink_to(repo / "plans", target_is_directory=True)
    _write_run_metadata(repo, repo_root=repo, plan=alias_repo / "plans-linked" / "in-progress" / "plan.md", worktree=worktree)
    context = _project_related(repo, plan, workflow)
    assert context.related_runs == ()
    assert context.recommendation == "start"


def test_descendant_root_symlink_cannot_match_current_plan(tmp_path: Path) -> None:
    repo, plan, worktree, alias_repo, _, workflow = _alias_related_fixture(tmp_path)
    assert (repo / "return-to-root").is_symlink()
    # The current plan has a durable identity, so a recorded path that
    # borrows the descendant symlink's target could otherwise become an
    # exact match for it.
    identity = create_plan_identity(repo, plan)
    assert identity is not None
    # No explicit plan identity is recorded; the original path crosses the
    # parent alias and the descendant root symlink.
    _write_run_metadata(
        repo, repo_root=repo,
        plan=alias_repo / "return-to-root" / "plans" / "in-progress" / "plan.md",
        worktree=worktree,
    )
    alias_context = _project_related(repo, plan, workflow)
    # The descendant symlink must not gain exact-match authority for the
    # current plan's durable identity.
    assert alias_context.related_runs == ()
    assert alias_context.recommendation == "start"

    # The canonical-spelled equivalent is unrelated because its verbatim
    # suffix has no durable owner; the alias spelling makes that same
    # decision.
    (repo / ".aflow" / "runs" / "previous-run" / "run.json").unlink()
    _write_run_metadata(
        repo, repo_root=repo,
        plan=repo / "return-to-root" / "plans" / "in-progress" / "plan.md",
        worktree=worktree,
    )
    canonical_context = _project_related(repo, plan, workflow)
    assert canonical_context.related_runs == ()
    assert canonical_context.recommendation == "start"
    assert canonical_context.recommendation == alias_context.recommendation


def test_alias_path_preserves_identity_decisions_and_missing_history(tmp_path: Path) -> None:
    repo, plan, worktree, alias_repo, _, workflow = _alias_related_fixture(tmp_path)
    alias_plan = alias_repo / "plans" / "in-progress" / "plan.md"
    identity = create_plan_identity(repo, plan)
    metadata_path = repo / ".aflow" / "runs" / "previous-run" / "run.json"

    # An explicit identity equal to the current plan identity is a definite
    # match through the alias spelling exactly as through the canonical one.
    _write_run_metadata(repo, repo_root=repo, plan=alias_plan, worktree=worktree, identity=identity)
    (worktree / "implementation.py").write_text("work = True\n")
    alias_context = _project_related(repo, plan, workflow)
    metadata_path.unlink()
    _write_run_metadata(repo, repo_root=repo, plan=plan, worktree=worktree, identity=identity)
    canonical_context = _project_related(repo, plan, workflow)
    assert alias_context.recommendation == canonical_context.recommendation == "review_previous_run"
    assert alias_context.availability == canonical_context.availability == "available"

    # An explicit conflicting identity produces the same decision through
    # the alias spelling as through the canonical one.
    metadata_path.unlink()
    _write_run_metadata(repo, repo_root=repo, plan=alias_plan, worktree=worktree, identity="f" * 32)
    alias_conflict = _project_related(repo, plan, workflow)
    metadata_path.unlink()
    _write_run_metadata(repo, repo_root=repo, plan=plan, worktree=worktree, identity="f" * 32)
    canonical_conflict = _project_related(repo, plan, workflow)
    assert alias_conflict.recommendation == canonical_conflict.recommendation
    assert alias_conflict.availability == canonical_conflict.availability

    # A historical alias-spelled original path whose file no longer exists
    # still matches through the durable identity history.
    metadata_path.unlink()
    _write_run_metadata(
        repo, repo_root=alias_repo, plan=alias_repo / "plans" / "in-progress" / "plan.md",
        worktree=worktree, identity=identity,
    )
    failed_path = repo / "plans" / "failed" / "plan.md"
    failed_path.parent.mkdir(parents=True)
    failed_path.write_text(plan.read_text())
    plan.unlink()
    assert move_plan_identity(repo, source_plan_path=plan, destination_plan_path=failed_path)
    assert plan_identity_for_path(repo, failed_path) == identity
    moved = _project_related(repo, failed_path, workflow)
    assert moved.recommendation == "review_previous_run"
    assert moved.related_runs[0].run_id == "previous-run"
    assert moved.related_runs[0].uncommitted_work is True


def test_historically_owned_symlinked_path_keeps_prior_run_related(tmp_path: Path) -> None:
    """A run recorded before the old plan path became a symlink stays a blocker.

    The plan moves through its lifecycle and the old path is later replaced
    by a symlink to the moved file. That symlink cannot erase the durable
    ownership of the recorded historical spelling, so the run stays related
    but identity-uncertain — with dirty or missing prior work — in both the
    canonical and the parent-alias spelling, and startup cannot prove a
    clean scan from the symlink.
    """
    repo, plan, worktree, alias_repo, _, workflow = _alias_related_fixture(tmp_path)
    alias_plan = alias_repo / "plans" / "in-progress" / "plan.md"
    alias_worktree = alias_repo.parent / "previous-worktree"
    create_plan_identity(repo, plan)
    # The prior run is recorded while the plan still lives at its original
    # regular path, with a dirty feature worktree and no explicit identity.
    (worktree / "implementation.py").write_text("work = True\n")
    _write_run_metadata(repo, repo_root=repo, plan=plan, worktree=worktree)
    failed_path = repo / "plans" / "failed" / "plan.md"
    failed_path.parent.mkdir(parents=True)
    failed_path.write_text(plan.read_text())
    plan.unlink()
    assert move_plan_identity(repo, source_plan_path=plan, destination_plan_path=failed_path)
    plan.symlink_to(failed_path)

    context = _project_related(repo, failed_path, workflow)
    assert context.recommendation != "start"
    assert context.related_runs_complete is False
    previous = context.related_runs[0]
    assert previous.run_id == "previous-run"
    assert previous.uncommitted_work is True

    # The parent-alias spelling of the same historical recorded path makes
    # the same decision.
    metadata_path = repo / ".aflow" / "runs" / "previous-run" / "run.json"
    metadata_path.unlink()
    _write_run_metadata(repo, repo_root=alias_repo, plan=alias_plan, worktree=alias_worktree)
    alias_context = _project_related(repo, failed_path, workflow)
    assert alias_context.recommendation != "start"
    assert alias_context.related_runs_complete is False
    assert alias_context.related_runs[0].run_id == "previous-run"
    assert alias_context.related_runs[0].uncommitted_work is True

    # Missing prior work keeps the same retained-run blocker.
    shutil.rmtree(worktree)
    missing_context = _project_related(repo, failed_path, workflow)
    assert missing_context.recommendation != "start"
    assert missing_context.related_runs_complete is False
    assert missing_context.related_runs[0].run_id == "previous-run"
    assert "prior_work_unverified" in missing_context.reason_codes
