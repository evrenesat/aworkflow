import json
from pathlib import Path
import subprocess

import pytest

from aflow.publication import PublicationError, publish_completed_run


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()


def commit(root, name, text):
    (root / name).write_text(text)
    git(root, "add", name)
    git(root, "commit", "-qm", name)
    return git(root, "rev-parse", "HEAD")


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
    assert publish_completed_run(source, run) == head
    assert git(remote, "rev-parse", "main") == head
    assert git(source, "branch", "--show-current") == "feature"
    assert publish_completed_run(source, run) == head
    assert json.loads((run / "publication.json").read_text())["status"] == "published"


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
