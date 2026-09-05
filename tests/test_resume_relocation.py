from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from aflow.cli import _bootstrap_resume_invocation, _resume_team_override_blocker
from aflow.config import (AflowSection, GoTransition, HarnessProfileConfig, TeamConfig,
    WorkflowConfig, WorkflowHarnessConfig, WorkflowStepConfig, WorkflowUserConfig)
from aflow.resume_relocation import prepare_resume_relocation
from aflow.run_state import ActiveImplementationScope, ControllerConfig
from aflow.runlog import (
    RunPaths, capture_checkpoint_evidence, capture_plan_evidence,
    create_run_paths, resolve_envelope_texts,
)
from aflow.workflow import (
    _freeze_run_identity, _rebase_scope_envelope_evidence,
    load_scope_evidence_for_resume,
)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(("git", "-C", str(root), *args), text=True).strip()


@pytest.fixture
def relocated(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "AFlow Test")
    git(root, "config", "user.email", "aflow-test@example.invalid")
    (root / "README.md").write_text("Test\n")
    (root / ".gitignore").write_text(".aflow/\n")
    git(root, "add", "README.md", ".gitignore")
    git(root, "commit", "-m", "Initial")
    base = git(root, "rev-parse", "HEAD")
    text = f"# Plan\n\n## Git Tracking\n\n- Plan Branch: `feature/saved`\n- Pre-Handoff Base HEAD: `{base}`\n\n### [ ] Checkpoint 1: Test\n- [ ] work\n"
    (root / "plan.md").write_text(text)
    git(root, "add", "plan.md")
    git(root, "commit", "-m", "Plan")
    worktree = tmp_path / "worktree"
    git(root, "worktree", "add", "-b", "feature/saved", str(worktree))
    config = WorkflowUserConfig(
        aflow=AflowSection(default_workflow="managed", team_lead="worker"),
        harnesses={"codex": WorkflowHarnessConfig(profiles={"test": HarnessProfileConfig(model="fixture")})},
        roles={"worker": "codex.test"}, teams={"base": TeamConfig(), "other": TeamConfig()},
        prompts={"p": "Work from {ACTIVE_PLAN_PATH}"},
        workflows={"managed": WorkflowConfig(
            steps={"implement": WorkflowStepConfig(role="worker", prompts=("p",), go=(GoTransition("END"),))},
            first_step="implement", setup=("worktree", "branch"),
            teardown=("merge", "rm_worktree"), main_branch="main",
        )},
    )
    config_path = root / "aflow.toml"
    config_path.write_text("")
    frozen = _freeze_run_identity("managed", config, config_dir=config_path)
    run = {
        "schema_version": 2, "repo_root": "/old/repo", "run_dir": "/old/repo/.aflow/runs/saved-run",
        "workflow_name": "managed", "plan_path": "/old/repo/plan.md",
        "original_plan_path": "/old/repo/plan.md", "active_plan_path": "/old/worktree/plan.md",
        "team": "base", "selected_start_step": "implement", "max_turns": 15, "effective_max_turns": 15,
        "extra_instructions": [], "lifecycle_setup": ["worktree", "branch"],
        "lifecycle_teardown": ["merge", "rm_worktree"], "feature_branch": "feature/saved",
        "worktree_path": "/old/worktree", "main_branch": "main", "status": "failed",
        "last_snapshot": {"is_complete": False},
        "frozen_config": {"workflow_name": frozen.workflow_name, "config_path": "/old/repo/aflow.toml",
                          "config_fingerprint": frozen.config_fingerprint},
        "manager_decision_number": 0, "manager_history": [], "semantic_stall_count": 0,
        "reviewer_rejection_count": 0, "implementation_attempts": {}, "active_implementation_scope": None,
        "review_rejection_history": [], "pending_manager_notes": None, "pending_step_team_override": None,
        "pending_boundary_decision": None, "pending_repartition": None, "repartition_history": [],
        "scope_pressure_reason": None, "last_manager_report_path": None, "hotplug_schema_version": 1,
        "role_selectors": {}, "current_hotplug_transaction": None, "pending_hotplug_transaction": None,
        "active_role_sessions": [], "hotplug_transaction_number": 0, "hotplug_history": [],
    }
    source = root / ".aflow/runs/saved-run"
    source.mkdir(parents=True)
    (source / "run.json").write_text(json.dumps(run))
    return root, worktree, config_path, config, run, source


def bootstrap(relocated, **kwargs):
    root, worktree, config_path, config, _, _ = relocated
    args = dict(repo_root=root, config_path=config_path, workflow_config=config,
        requested_run_id="saved-run", workflow_arg=None, plan_file_arg=None, team_arg=None,
        start_step_arg=None, max_turns_arg=None, extra_instructions_arg=(),
        extra_instructions_provided=False, rehome_worktree=worktree)
    args.update(kwargs)
    return _bootstrap_resume_invocation(**args)


def hashes(root: Path):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def test_explicit_rehome_bootstrap_preserves_source_and_maps_execution_paths(relocated):
    root, worktree, _, _, run, source = relocated
    before = hashes(source)
    result = bootstrap(relocated)
    assert result.plan_path == root / "plan.md"
    assert result.resume_context.worktree_path == worktree
    assert result.resume_context.active_plan_path == worktree / "plan.md"
    assert result.resume_context.resume_relocation["source_repo_root"] == "/old/repo"
    assert hashes(source) == before
    assert json.loads((source / "run.json").read_text()) == run


def test_same_copy_without_rehome_remains_rejected(relocated):
    with pytest.raises(ValueError, match="not readable|different repo root"):
        bootstrap(relocated, rehome_worktree=None)


@pytest.mark.parametrize("replacement", ["relative", "primary", "missing", "symlink", "wrong-branch", "unregistered"])
def test_rehome_rejects_unsafe_worktrees_without_allocation(relocated, replacement):
    root, worktree, _, _, run, source = relocated
    before = hashes(source)
    candidate = worktree
    if replacement == "relative":
        candidate = Path("relative")
    elif replacement == "primary":
        candidate = root
    elif replacement == "missing":
        candidate = root.parent / "missing"
    elif replacement == "symlink":
        candidate = root.parent / "link"
        candidate.symlink_to(worktree, target_is_directory=True)
    elif replacement == "wrong-branch":
        git(worktree, "checkout", "-b", "other-branch")
    elif replacement == "unregistered":
        candidate = root.parent / "independent"
        candidate.mkdir()
        git(candidate, "init")
    with pytest.raises(ValueError):
        bootstrap(relocated, rehome_worktree=candidate)
    assert hashes(source) == before
    assert len(list((root / ".aflow/runs").iterdir())) == 1


@pytest.mark.parametrize("marker", ["MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-apply"])
def test_rehome_rejects_in_progress_git_operation(relocated, marker):
    root, worktree, *_ = relocated
    git_dir = Path(git(worktree, "rev-parse", "--absolute-git-dir"))
    (git_dir / marker).write_text("in progress")
    with pytest.raises(ValueError, match="in-progress Git operation"):
        bootstrap(relocated)


def test_rehome_does_not_rewrite_prose_frozen_config_or_external_history(relocated):
    root, worktree, _, _, run, _ = relocated
    source = copy.deepcopy(run)
    source["manager_history"] = [{"artifact_path": "/outside/evidence", "reason": "/old/repo/plan.md"}]
    source["pending_manager_notes"] = {"target_plan_identity": "/old/worktree/plan.md::checkpoint-1"}
    relocation = prepare_resume_relocation(source, source_run_id="saved-run", current_repo_root=root,
                                           replacement_worktree=worktree)
    mapped = relocation.payload(source)
    assert mapped["manager_history"] == source["manager_history"]
    assert mapped["frozen_config"] == source["frozen_config"]
    assert mapped["pending_manager_notes"]["target_plan_identity"] == f"{worktree}/plan.md::checkpoint-1"
    assert source["pending_manager_notes"]["target_plan_identity"].startswith("/old/")


def test_rehome_preserved_source_is_not_pruned_with_keep_runs_one(relocated):
    root, _, _, _, _, source = relocated
    before = hashes(source)
    paths = create_run_paths(ControllerConfig(repo_root=root, plan_path=root / "plan.md", keep_runs=1),
                             preserved_run_ids=frozenset({source.name}))
    assert paths.run_dir != source
    assert hashes(source) == before


def test_named_resume_can_override_configured_team_and_records_provenance(relocated):
    root, worktree, config_path, _, run, source = relocated
    result = bootstrap(relocated, team_arg="other")
    assert result.team == "other"
    assert result.resume_context.resumed_from_team == "base"
    assert result.resume_context.resume_team_override == "other"
    assert result.resume_context.frozen_run_identity.config_path == str(config_path.resolve())
    assert result.run_json["team"] == "base"
    assert json.loads((source / "run.json").read_text()) == run


def test_resume_team_override_rejects_unknown_configured_team(relocated):
    with pytest.raises(ValueError, match="resume team mismatch.*unknown team"):
        bootstrap(relocated, team_arg="missing")


@pytest.mark.parametrize(
    "field",
    [
        "pending_manager_notes",
        "pending_step_team_override",
        "pending_boundary_decision",
        "pending_repartition",
        "current_hotplug_transaction",
        "pending_hotplug_transaction",
    ],
)
def test_resume_team_override_reports_exact_pending_field(relocated, field):
    _, _, _, _, run, source = relocated
    run[field] = {}
    assert _resume_team_override_blocker(source, run) == field


def test_resume_team_override_reports_unapplied_routing_state(relocated):
    _, _, _, _, run, source = relocated
    run["override_result"] = {
        "status": "accepted", "digest": "a" * 64, "message": "pending",
        "applied": False,
    }
    assert _resume_team_override_blocker(source, run) == "override_result"


def test_schema_v2_scope_evidence_is_bound_and_rebased_for_continuation(tmp_path):
    repo = tmp_path / "repo"
    source = repo / ".aflow" / "runs" / "source-run"
    source.mkdir(parents=True)
    plan = repo / "plan.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan_text = "# Plan\n\n### [ ] Checkpoint 1: Test\n- [ ] work\n"
    plan.write_text(plan_text, encoding="utf-8")
    paths = RunPaths(
        repo_root=repo, runs_root=repo / ".aflow" / "runs", run_dir=source,
        turns_dir=source / "turns", manager_dir=source / "manager",
        run_json=source / "run.json",
    )
    plan_ref = capture_plan_evidence(paths, plan_text)
    checkpoint_text = "### [ ] Checkpoint 1: Test\n- [ ] work\n"
    checkpoint_ref = capture_checkpoint_evidence(paths, checkpoint_text)
    from aflow.repartition import EvidenceArtifactReferenceV2, create_envelope_v2, parse_envelope_bytes, slice_checkpoint_source, write_envelope_atomic
    sliced = slice_checkpoint_source(plan_text, checkpoint_index=1)
    assert sliced is not None
    envelope = create_envelope_v2(
        scope_id=f"{plan}::checkpoint-1::Test",
        original_plan_path=plan, plan_text=plan_text, checkpoint_index=1,
        repo_root=repo,
        plan_ref=EvidenceArtifactReferenceV2(
            kind=plan_ref.kind, path=plan_ref.path,
            sha256=plan_ref.sha256, byte_size=plan_ref.byte_size,
        ),
        checkpoint_ref=EvidenceArtifactReferenceV2(
            kind=checkpoint_ref.kind, path=checkpoint_ref.path,
            sha256=checkpoint_ref.sha256, byte_size=checkpoint_ref.byte_size,
        ),
    )
    envelope_path = write_envelope_atomic(envelope, source / "scopes" / envelope.scope_digest)
    envelope_bytes = envelope_path.read_bytes()
    scope = ActiveImplementationScope(
        scope_id=envelope.scope_id, original_plan_path=str(plan),
        checkpoint_index=1, checkpoint_name=envelope.checkpoint_name,
        opened_turn_number=1,
        envelope_artifact_path=envelope_path.relative_to(source).as_posix(),
        envelope_artifact_sha256=hashlib.sha256(envelope_bytes).hexdigest(),
        envelope_canonical_sha256=envelope.canonical_envelope_sha256,
    )
    artifacts = load_scope_evidence_for_resume(source, scope, envelope_bytes)
    assert artifacts[plan_ref.path.removeprefix(f".aflow/runs/{source.name}/")] == plan_text.encode()
    assert len(artifacts) == 2
    continuation = repo / ".aflow" / "runs" / "continuation"
    continuation.mkdir()
    continuation_paths = RunPaths(
        repo_root=repo, runs_root=repo / ".aflow" / "runs", run_dir=continuation,
        turns_dir=continuation / "turns", manager_dir=continuation / "manager",
        run_json=continuation / "run.json",
    )
    for relative, data in artifacts.items():
        target = continuation / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    rebased = _rebase_scope_envelope_evidence(continuation_paths, parse_envelope_bytes(envelope_bytes))
    assert resolve_envelope_texts(continuation_paths, rebased)[0] == plan_text
    assert envelope_bytes == envelope_path.read_bytes()
