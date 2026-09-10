import json
from pathlib import Path
import subprocess

import pytest

from aflow.publication import (
    PlanLifecycleError,
    PublicationError,
    finalize_completed_plan,
    publish_completed_run,
)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()


def commit(root, name, text):
    (root / name).write_text(text)
    git(root, "add", name)
    git(root, "commit", "-qm", name)
    return git(root, "rev-parse", "HEAD")


def _lifecycle_repo(
    tmp_path, *, ignored_done=False, source_name="plan.md", tracked=True, autocrlf=False
):
    root = tmp_path / "lifecycle-repo"
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=main", str(root)],
        check=True,
    )
    for key, value in [("user.name", "Lifecycle Test"), ("user.email", "lifecycle@example.invalid")]:
        git(root, "config", key, value)
    if autocrlf:
        git(root, "config", "core.autocrlf", "true")
    ignore_lines = [
        ".aflow/\n",
        "!plans/\n",
        "!plans/in-progress/\n",
    ]
    if ignored_done:
        ignore_lines.append("plans/done/\n")
    else:
        ignore_lines.append("!plans/done/\n")
    (root / ".gitignore").write_text("".join(ignore_lines), encoding="utf-8")
    source = root / "plans" / "in-progress" / source_name
    source.parent.mkdir(parents=True)
    (root / "unrelated.txt").write_text("base unrelated\n", encoding="utf-8")
    if tracked:
        source.write_bytes(b"# Complete\n\n### [x] Checkpoint 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    if autocrlf:
        source.unlink()
        git(root, "checkout", "--", source.relative_to(root).as_posix())
    if not tracked:
        source.write_bytes(b"# Complete\n\n### [x] Checkpoint 1\n")
    return root, source


def _lifecycle_commit_paths(root, commit_sha):
    output = subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "diff-tree",
            "--no-commit-id",
            "--no-renames",
            "--name-only",
            "-r",
            "-z",
            f"{commit_sha}^",
            commit_sha,
        ]
    )
    return {
        path.decode(errors="surrogateescape")
        for path in output.split(b"\0")
        if path
    }


def _receipt(run):
    return json.loads((run / "publication.json").read_text(encoding="utf-8"))


def _run_dir(root):
    run = root / ".aflow" / "runs" / "run-1"
    run.mkdir(parents=True)
    return run


@pytest.fixture
def repos(tmp_path):
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    other = tmp_path / "other"
    subprocess.run(["git", "init", "--bare", "-q", "--initial-branch=main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(source)], check=True, capture_output=True)
    for key, value in [("user.name", "Test"), ("user.email", "test@example.invalid")]:
        git(source, "config", key, value)
    commit(source, "base", "base")
    git(source, "push", "-q", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    for key, value in [("user.name", "Test"), ("user.email", "test@example.invalid")]:
        git(other, "config", key, value)
    git(source, "checkout", "-qb", "feature")
    git(source, "config", "aflow.publishRemote", "origin")
    git(source, "config", "aflow.publishBranch", "main")
    return source, remote, other, tmp_path / "run"


def test_opt_out_is_noop(tmp_path):
    assert publish_completed_run(tmp_path, tmp_path / "run") is None
    assert not (tmp_path / "run").exists()


def test_completed_commit_reaches_main_and_repeat_is_idempotent(repos):
    source, remote, _, run = repos
    head = commit(source, "feature", "approved")
    run.mkdir()
    lifecycle = {"phase": "moved", "source": "plans/in-progress/plan.md"}
    (run / "publication.json").write_text(
        json.dumps({"plan_lifecycle": lifecycle, "future_field": "retain"}),
        encoding="utf-8",
    )
    assert publish_completed_run(source, run) == head
    assert git(remote, "rev-parse", "main") == head
    assert git(source, "branch", "--show-current") == "feature"
    assert publish_completed_run(source, run) == head
    receipt = json.loads((run / "publication.json").read_text())
    assert receipt["status"] == "published"
    assert receipt["plan_lifecycle"] == lifecycle
    assert receipt["future_field"] == "retain"


def test_dirty_execution_is_not_published(repos):
    source, remote, _, run = repos
    old = git(remote, "rev-parse", "main")
    commit(source, "feature", "approved")
    (source / "unfinished").write_text("do not publish")
    with pytest.raises(PublicationError):
        publish_completed_run(source, run)
    assert git(remote, "rev-parse", "main") == old
    assert (source / "unfinished").read_text() == "do not publish"
    assert json.loads((run / "publication.json").read_text())["status"] == "failed"


def test_remote_changes_merge_outside_execution_checkout(repos):
    source, remote, other, run = repos
    head = commit(source, "feature", "approved")
    upstream = commit(other, "remote", "accepted upstream")
    git(other, "push", "-q", "origin", "main")
    published = publish_completed_run(source, run)
    assert git(remote, "rev-parse", "main") == published
    assert git(source, "rev-parse", "HEAD") == head
    assert git(source, "merge-base", head, published) == head
    assert git(source, "merge-base", upstream, published) == upstream
    assert not (source / "remote").exists()


def test_conflict_preserves_both_histories_and_reports_checkout(repos):
    source, remote, other, run = repos
    head = commit(source, "base", "local change")
    upstream = commit(other, "base", "remote change")
    git(other, "push", "-q", "origin", "main")
    with pytest.raises(PublicationError, match="preserved merge checkout"):
        publish_completed_run(source, run)
    receipt = json.loads((run / "publication.json").read_text())
    assert Path(receipt["checkout"]).is_dir()
    assert git(source, "rev-parse", "HEAD") == head
    assert git(remote, "rev-parse", "main") == upstream


def test_partial_configuration_cannot_silently_skip_publication(repos):
    source, _, _, run = repos
    git(source, "config", "--unset", "aflow.publishBranch")
    with pytest.raises(PublicationError):
        publish_completed_run(source, run)


def test_tracked_plan_to_ignored_done_commits_only_source_deletion(tmp_path):
    root, source = _lifecycle_repo(tmp_path, ignored_done=True)
    run = _run_dir(root)
    original_receipt = {
        "status": "published",
        "commit": "reviewed-code-sha",
        "future_field": {"retain": True},
    }
    (run / "publication.json").write_text(json.dumps(original_receipt), encoding="utf-8")
    source_relative = source.relative_to(root).as_posix()
    destination = root / "plans" / "done" / source.name

    result = finalize_completed_plan(root, source, run)

    assert result.commit
    assert result.destination == destination
    assert destination.read_bytes() == b"# Complete\n\n### [x] Checkpoint 1\n"
    assert not source.exists()
    assert _lifecycle_commit_paths(root, result.commit) == {source_relative}
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""
    assert _receipt(run)["status"] == original_receipt["status"]
    assert _receipt(run)["commit"] == original_receipt["commit"]
    assert _receipt(run)["future_field"] == original_receipt["future_field"]
    lifecycle = _receipt(run)["plan_lifecycle"]
    assert lifecycle["source"] == source_relative
    assert lifecycle["destination"] == "plans/done/plan.md"
    assert lifecycle["phase"] == "committed"
    assert lifecycle["complete"] is True


def test_tracked_plan_to_tracked_done_commits_exact_source_and_destination(tmp_path):
    root, source = _lifecycle_repo(tmp_path)
    run = _run_dir(root)
    result = finalize_completed_plan(root, source, run)

    destination = root / "plans" / "done" / source.name
    assert result.commit
    assert destination.read_bytes() == b"# Complete\n\n### [x] Checkpoint 1\n"
    assert not source.exists()
    assert _lifecycle_commit_paths(root, result.commit) == {
        "plans/in-progress/plan.md",
        "plans/done/plan.md",
    }
    assert git(root, "ls-files", "--error-unmatch", "--", "plans/done/plan.md")
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""


def test_wholly_untracked_plan_moves_without_a_lifecycle_commit(tmp_path):
    root, source = _lifecycle_repo(tmp_path, ignored_done=True, tracked=False)
    run = _run_dir(root)
    head = git(root, "rev-parse", "HEAD")

    result = finalize_completed_plan(root, source, run)

    assert result.commit is None
    assert result.source_tracked is False
    assert not source.exists()
    assert (root / "plans" / "done" / source.name).is_file()
    assert git(root, "rev-parse", "HEAD") == head
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""
    assert _receipt(run)["plan_lifecycle"]["phase"] == "moved"


def test_plain_directory_move_does_not_bootstrap_git(tmp_path):
    root = tmp_path / "plain"
    source = root / "plans" / "in-progress" / "plan.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"# Complete\n\n### [x] Checkpoint 1\n")
    run = root / "run"

    result = finalize_completed_plan(root, source, run)

    assert result.commit is None
    assert (root / "plans" / "done" / source.name).is_file()
    assert not source.exists()
    assert not (root / ".git").exists()


def test_repeat_finalization_verifies_the_recorded_commit_without_replaying_it(tmp_path):
    root, source = _lifecycle_repo(tmp_path, ignored_done=True)
    run = _run_dir(root)
    first = finalize_completed_plan(root, source, run)
    history_before = git(root, "rev-list", "--count", "HEAD")

    second = finalize_completed_plan(root, source, run)

    assert second.commit == first.commit
    assert second.moved is False
    assert git(root, "rev-list", "--count", "HEAD") == history_before
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""


def test_identical_preexisting_done_copy_is_reused_and_source_is_removed(tmp_path):
    root, source = _lifecycle_repo(tmp_path, ignored_done=True)
    destination = root / "plans" / "done" / source.name
    destination.parent.mkdir(parents=True)
    destination.write_bytes(source.read_bytes())
    run = _run_dir(root)

    result = finalize_completed_plan(root, source, run)

    assert result.commit
    assert destination.read_bytes() == b"# Complete\n\n### [x] Checkpoint 1\n"
    assert not source.exists()
    assert _lifecycle_commit_paths(root, result.commit) == {"plans/in-progress/plan.md"}


def test_conflicting_done_bytes_are_refused_without_receipt_or_commit(tmp_path):
    root, source = _lifecycle_repo(tmp_path, ignored_done=True)
    destination = root / "plans" / "done" / source.name
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"different\n")
    run = _run_dir(root)
    head = git(root, "rev-parse", "HEAD")

    with pytest.raises(PlanLifecycleError, match="different bytes"):
        finalize_completed_plan(root, source, run)

    assert source.is_file()
    assert source.read_bytes() == b"# Complete\n\n### [x] Checkpoint 1\n"
    assert destination.read_bytes() == b"different\n"
    assert git(root, "rev-parse", "HEAD") == head
    assert not (run / "publication.json").exists()


def test_unrelated_staged_changes_are_refused_before_the_move(tmp_path):
    root, source = _lifecycle_repo(tmp_path, ignored_done=True)
    unrelated = root / "staged.txt"
    unrelated.write_text("keep staged\n", encoding="utf-8")
    git(root, "add", "--", "staged.txt")
    run = _run_dir(root)

    with pytest.raises(PlanLifecycleError, match="unrelated.*staged"):
        finalize_completed_plan(root, source, run)

    assert source.is_file()
    assert not (root / "plans" / "done" / source.name).exists()
    assert git(root, "diff", "--cached", "--name-only") == "staged.txt"
    assert unrelated.read_text(encoding="utf-8") == "keep staged\n"


def test_unrelated_unstaged_changes_survive_an_exact_lifecycle_commit(tmp_path):
    root, source = _lifecycle_repo(tmp_path, ignored_done=True)
    unrelated = root / "unrelated.txt"
    unrelated.write_text("keep unstaged\n", encoding="utf-8")
    run = _run_dir(root)

    result = finalize_completed_plan(root, source, run)

    assert result.commit
    assert _lifecycle_commit_paths(root, result.commit) == {
        "plans/in-progress/plan.md",
    }
    assert unrelated.read_text(encoding="utf-8") == "keep unstaged\n"
    assert git(root, "diff", "--", "unrelated.txt")
    status = subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
        text=True,
    )
    assert status == " M unrelated.txt\n"


@pytest.mark.parametrize("name", ["plan*.md", "plan[?].md"])
def test_pathspec_metacharacters_do_not_stage_a_done_neighbor(tmp_path, name):
    root, source = _lifecycle_repo(tmp_path, source_name=name)
    neighbor = root / "plans" / "done" / "plan-other.md"
    neighbor.parent.mkdir(parents=True, exist_ok=True)
    neighbor.write_text("neighbor base\n", encoding="utf-8")
    git(root, "add", "--", "plans/done/plan-other.md")
    git(root, "commit", "-qm", "neighbor")
    neighbor.write_text("neighbor unstaged\n", encoding="utf-8")
    run = _run_dir(root)

    result = finalize_completed_plan(root, source, run)

    assert result.commit
    assert _lifecycle_commit_paths(root, result.commit) == {
        f"plans/in-progress/{name}",
        f"plans/done/{name}",
    }
    assert (root / "plans" / "done" / name).read_bytes() == b"# Complete\n\n### [x] Checkpoint 1\n"
    assert neighbor.read_text(encoding="utf-8") == "neighbor unstaged\n"
    status = subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
        text=True,
    )
    assert status == " M plans/done/plan-other.md\n"


def test_unusual_plan_filename_is_used_as_a_literal_pathspec(tmp_path):
    name = "plan with spaces 'and-é'.md"
    root, source = _lifecycle_repo(tmp_path, source_name=name)
    run = _run_dir(root)

    result = finalize_completed_plan(root, source, run)

    assert result.commit
    assert _lifecycle_commit_paths(root, result.commit) == {
        f"plans/in-progress/{name}",
        f"plans/done/{name}",
    }
    assert (root / "plans" / "done" / name).read_bytes() == b"# Complete\n\n### [x] Checkpoint 1\n"


def test_incomplete_plan_is_not_moved_or_recorded(tmp_path):
    root, source = _lifecycle_repo(tmp_path, ignored_done=True)
    run = _run_dir(root)

    with pytest.raises(PlanLifecycleError, match="complete plan"):
        finalize_completed_plan(root, source, run, is_complete=False)

    assert source.is_file()
    assert not (root / "plans" / "done" / source.name).exists()
    assert not (run / "publication.json").exists()


@pytest.mark.parametrize("interrupted_phase", ["prepared", "moved", "commit_pending", "committed"])
def test_recorded_interruption_phases_resume_once_without_a_second_commit(tmp_path, interrupted_phase):
    root, source = _lifecycle_repo(tmp_path, ignored_done=True)
    run = _run_dir(root)

    def interrupt(phase):
        if phase == interrupted_phase:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        finalize_completed_plan(root, source, run, phase_hook=interrupt)

    interrupted_receipt = _receipt(run)["plan_lifecycle"]
    assert interrupted_receipt["phase"] == interrupted_phase
    first = finalize_completed_plan(root, source, run)
    history_after_retry = git(root, "rev-list", "--count", "HEAD")
    second = finalize_completed_plan(root, source, run)

    assert first.commit == second.commit
    assert history_after_retry == "2"
    assert git(root, "rev-list", "--count", "HEAD") == history_after_retry
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""


def test_recorded_source_divergence_is_refused_on_retry(tmp_path):
    root, source = _lifecycle_repo(tmp_path, ignored_done=True)
    run = _run_dir(root)

    def interrupt(phase):
        if phase == "prepared":
            source.write_bytes(b"changed after receipt\n")
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        finalize_completed_plan(root, source, run, phase_hook=interrupt)
    with pytest.raises(PlanLifecycleError, match="diverged"):
        finalize_completed_plan(root, source, run)

    assert source.read_bytes() == b"changed after receipt\n"
    assert not (root / "plans" / "done" / source.name).exists()


@pytest.mark.parametrize(
    ("ignored_done", "interrupt_event", "expected_paths"),
    [
        (True, "after_source_stage", {"plans/in-progress/plan.md"}),
        (False, "after_source_stage", {"plans/in-progress/plan.md"}),
        (
            False,
            "after_destination_stage",
            {"plans/in-progress/plan.md", "plans/done/plan.md"},
        ),
    ],
)
def test_actual_staging_interruption_resumes_from_exact_receipt_intent(
    tmp_path, ignored_done, interrupt_event, expected_paths
):
    root, source = _lifecycle_repo(tmp_path, ignored_done=ignored_done)
    run = _run_dir(root)

    def interrupt(event):
        if event == interrupt_event:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        finalize_completed_plan(root, source, run, stage_hook=interrupt)

    interrupted = _receipt(run)["plan_lifecycle"]
    assert interrupted["phase"] == "moved"
    assert interrupted["staging"]["phase"] == (
        "source_pending"
        if interrupt_event == "after_source_stage"
        else "destination_pending"
    )
    assert not source.exists()
    assert (root / "plans" / "done" / source.name).read_bytes() == (
        b"# Complete\n\n### [x] Checkpoint 1\n"
    )
    assert set(git(root, "diff", "--cached", "--no-renames", "--name-only").splitlines()) == expected_paths

    first = finalize_completed_plan(root, source, run)
    history_after_retry = git(root, "rev-list", "--count", "HEAD")
    second = finalize_completed_plan(root, source, run)

    assert first.commit == second.commit
    assert first.commit
    assert history_after_retry == "2"
    assert git(root, "rev-list", "--count", "HEAD") == history_after_retry
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""
    expected_commit_paths = expected_paths | (
        {"plans/done/plan.md"} if not ignored_done else set()
    )
    assert _lifecycle_commit_paths(root, first.commit) == expected_commit_paths


def test_autocrlf_uses_the_clean_destination_blob_during_interrupted_move(tmp_path):
    root, source = _lifecycle_repo(tmp_path, autocrlf=True)
    run = _run_dir(root)
    raw_bytes = b"# Complete\r\n\r\n### [x] Checkpoint 1\r\n"
    assert source.read_bytes() == raw_bytes
    source_relative = source.relative_to(root).as_posix()
    source_blob = git(root, "ls-files", "--stage", "--", source_relative).split()[1]
    normalized_bytes = subprocess.check_output(
        ["git", "-C", str(root), "cat-file", "-p", source_blob]
    )
    assert normalized_bytes == b"# Complete\n\n### [x] Checkpoint 1\n"

    def interrupt(event):
        if event == "after_destination_stage":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        finalize_completed_plan(root, source, run, stage_hook=interrupt)

    destination = root / "plans" / "done" / source.name
    interrupted = _receipt(run)["plan_lifecycle"]
    assert interrupted["staging"]["destination_expected"] == source_blob
    assert destination.read_bytes() == raw_bytes
    destination_relative = destination.relative_to(root).as_posix()
    destination_blob = git(
        root, "ls-files", "--stage", "--", destination_relative
    ).split()[1]
    assert destination_blob == source_blob
    assert set(
        git(root, "diff", "--cached", "--no-renames", "--name-only").splitlines()
    ) == {source_relative, destination_relative}
    assert git(root, "rev-list", "--count", "HEAD") == "1"

    first = finalize_completed_plan(root, source, run)
    assert first.commit
    assert destination.read_bytes() == raw_bytes
    assert git(root, "rev-parse", f"{first.commit}:{destination_relative}") == source_blob
    assert _lifecycle_commit_paths(root, first.commit) == {
        source_relative,
        destination_relative,
    }
    assert git(root, "rev-list", "--count", "HEAD") == "2"
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""

    second = finalize_completed_plan(root, source, run)
    assert second.commit == first.commit
    assert git(root, "rev-list", "--count", "HEAD") == "2"
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""


def test_partial_staging_refuses_a_conflicting_owned_blob(tmp_path):
    root, source = _lifecycle_repo(tmp_path, ignored_done=True)
    run = _run_dir(root)

    def interrupt(event):
        if event == "after_source_stage":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        finalize_completed_plan(root, source, run, stage_hook=interrupt)

    alternate = root / "alternate.txt"
    alternate.write_text("conflicting index bytes\n", encoding="utf-8")
    alternate_blob = git(root, "hash-object", "-w", "--", "alternate.txt")
    alternate.unlink()
    source_relative = source.relative_to(root).as_posix()
    git(root, "update-index", "--add", "--cacheinfo", f"100644,{alternate_blob},{source_relative}")

    with pytest.raises(PlanLifecycleError, match="source index identity diverged"):
        finalize_completed_plan(root, source, run)

    assert alternate_blob in git(root, "ls-files", "--stage", "--", source_relative)
    assert set(git(root, "diff", "--cached", "--name-only").splitlines()) == {source_relative}
    assert git(root, "rev-list", "--count", "HEAD") == "1"
    assert (root / "plans" / "done" / source.name).is_file()


def test_partial_staging_refuses_new_unrelated_staged_paths(tmp_path):
    root, source = _lifecycle_repo(tmp_path, ignored_done=True)
    run = _run_dir(root)

    def interrupt(event):
        if event == "after_source_stage":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        finalize_completed_plan(root, source, run, stage_hook=interrupt)

    unrelated = root / "unrelated.txt"
    unrelated.write_text("new unrelated staged bytes\n", encoding="utf-8")
    git(root, "add", "--", "unrelated.txt")

    with pytest.raises(PlanLifecycleError, match="unrelated.*staged"):
        finalize_completed_plan(root, source, run)

    assert set(git(root, "diff", "--cached", "--name-only").splitlines()) == {
        "plans/in-progress/plan.md",
        "unrelated.txt",
    }
    assert unrelated.read_text(encoding="utf-8") == "new unrelated staged bytes\n"
    assert (root / "plans" / "done" / source.name).is_file()


@pytest.mark.parametrize('reject', [False, True])
def test_workflow_publishes_before_done_and_keeps_failures_in_progress(tmp_path, monkeypatch, reject):
    from tests._support import ControllerConfig, WorkflowError, run_workflow, _VALID_PLAN, _COMPLETE_PLAN, _write_plan
    from tests.test_runtime import _resume_override_workflow_config
    plan = tmp_path / 'plans/in-progress/test.md'
    plan.parent.mkdir(parents=True)
    _write_plan(plan, _VALID_PLAN)
    observed = []
    def publish(root, run_dir, **kwargs):
        assert plan.exists()
        observed.append(run_dir)
        if reject:
            raise PublicationError('push rejected')
        return 'a' * 40
    monkeypatch.setattr('aflow.workflow.publish_completed_run', publish)
    def runner(argv, **kwargs):
        _write_plan(plan, _COMPLETE_PLAN)
        return subprocess.CompletedProcess(argv, 0, 'completed', '')
    def execute():
        return run_workflow(ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=1),
                            _resume_override_workflow_config(), 'resume_override',
                            config_dir=tmp_path, snapshot_config=False, runner=runner)
    if reject:
        with pytest.raises(WorkflowError, match='push rejected'):
            execute()
        assert plan.exists()
        assert json.loads((observed[0] / 'run.json').read_text())['status'] == 'failed'
    else:
        execute()
        assert not plan.exists()
        assert (tmp_path / 'plans/done/test.md').exists()
    assert len(observed) == 1
