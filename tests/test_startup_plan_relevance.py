"""End-to-end plan-relevance startup coverage (issue #63).

These tests exercise the real repository, a real Git multi-plan history, and
the real ``prepare_startup`` / ``require_safe_fresh_worktree`` admission path.
Only the unit/provider boundary is faked (``InMemoryUnitManager``), so the
tests prove that a disjoint known plan's history no longer blocks another
selected plan, that protected same-plan work still blocks before any
provider call or worktree creation, and that startup side effects never
touch the protected plan's durable records.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from aflow.api.models import PreparedRun, StartupQuestion, StartupRequest
from aflow.api.startup import (
    PriorWorkStartupError,
    prepare_startup,
    require_safe_fresh_worktree,
)
from aflow.config import (
    GoTransition,
    WorkflowConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
)
from aflow.control_plane import (
    InMemoryUnitManager,
    LaunchManifest,
    create_launch_manifest,
)
from aflow.control_plane.models import StartRunResult
from aflow.control_plane.startup_context import project_startup_context
from aflow.daemon import AflowDaemon, DaemonConfig, DaemonStartupError
from aflow.plan_backups import create_plan_identity, plan_identity_alias_owners
from aflow.project_admission import ProjectAdmission
from aflow.project_settings import ProjectSettings, ProjectSettingsService


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True)


def _workflow_config() -> WorkflowUserConfig:
    implement = WorkflowStepConfig(
        role="worker",
        prompts=("p",),
        go=(GoTransition(to="END", when="DONE"),),
    )
    workflow = WorkflowConfig(
        setup=("worktree", "branch"),
        teardown=("merge", "rm_worktree"),
        main_branch="main",
        steps={"implement": implement},
        first_step="implement",
    )
    return WorkflowUserConfig(
        roles={"worker": "codex.worker"},
        workflows={"managed": workflow},
        prompts={"p": "Implement the selected checkpoint."},
    )


def _repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "README.md").write_text("base\n")
    plans = repo / "plans" / "in-progress"
    plans.mkdir(parents=True)
    a_plan = plans / "plan-a.md"
    b_plan = plans / "plan-b.md"
    a_plan.write_text("# Plan A\n\n### [ ] Checkpoint 1: A\n- [ ] a step\n")
    b_plan.write_text("# Plan B\n\n### [ ] Checkpoint 1: B\n- [ ] b step\n")
    # A retired plan path that no identity owns; it seeds the identity-free
    # completed legacy record used by the multi-plan acceptance case.
    legacy = repo / "plans" / "archive"
    legacy.mkdir()
    legacy_plan = legacy / "legacy-a.md"
    legacy_plan.write_text("# Legacy A\n\n### [ ] Checkpoint 1: legacy\n- [ ] legacy step\n")
    _git(["add", "-f", "README.md", "plans"], repo)
    _git(["commit", "-m", "plans"], repo)
    return repo, a_plan, b_plan


def _worktree(repo: Path, name: str, branch: str, tmp_path: Path) -> Path:
    target = tmp_path / name
    _git(["worktree", "add", "-b", branch, str(target), "main"], repo)
    return target


def _worktrees(repo: Path) -> list[str]:
    output = subprocess.check_output(
        ("git", "worktree", "list"), cwd=repo, text=True
    )
    return [line.split()[0] for line in output.splitlines() if line]


def _record_history(
    repo: Path,
    plan: Path,
    *,
    run_id: str,
    identity: str | None = None,
    worktree: Path | None = None,
    branch: str | None = None,
    status: str = "failed",
) -> None:
    create_launch_manifest(
        repo,
        LaunchManifest(
            run_id=run_id,
            project_root=str(repo),
            plan_path=str(plan),
            workflow_name="managed",
            max_turns=5,
        ),
    )
    run_dir = repo / ".aflow" / "runs" / run_id
    run_dir.mkdir(parents=True)
    record: dict[str, object] = {
        "schema_version": 1,
        "status": status,
        "repo_root": str(repo),
        "original_plan_path": str(plan),
        "current_step_name": "implement",
        "main_branch": "main",
        "failure_reason": "earlier run stopped",
    }
    if identity is not None:
        record["original_plan_identity"] = identity
    if branch is not None:
        record["feature_branch"] = branch
    if worktree is not None:
        record["worktree_path"] = str(worktree)
        record["execution_repo_root"] = str(worktree)
    (run_dir / "run.json").write_text(json.dumps(record))


def _request(config_path: Path, repo: Path, plan: Path) -> StartupRequest:
    return StartupRequest(
        repo_root=repo,
        plan_path=plan,
        config_path=config_path,
        workflow_config=_workflow_config(),
        workflow_name="managed",
        start_step=None,
        max_turns=2,
        team=None,
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    state: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            state[str(path.relative_to(root))] = path.read_bytes()
    return state


def _durable_evidence(
    repo: Path, *, run_id: str, worktree: Path, plans: tuple[Path, ...]
) -> dict[str, bytes]:
    """Byte snapshot of one plan's durable evidence and the repository refs.

    Captures the run metadata directory, launch receipt/manifest, plan
    identity owner records, the named plan files, every branch ref plus HEAD,
    and the entire retained worktree including its dirty files, so later
    comparisons prove byte equality instead of single-field survival.
    """
    state: dict[str, bytes] = {}

    def add(path: Path) -> None:
        if path.is_file():
            state[str(path)] = path.read_bytes()

    for path in sorted((repo / ".aflow" / "runs" / run_id).rglob("*")):
        add(path)
    for path in sorted((repo / ".aflow" / "launches").glob(f"*{run_id}*")):
        add(path)
    owners = repo / "plans" / "backups" / ".provenance" / ".owners"
    for path in sorted(owners.rglob("*")):
        add(path)
    for plan in plans:
        add(plan)
    git_dir = repo / ".git"
    for path in sorted(git_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(git_dir)
        if relative.parts[0] == "refs" or path.name in {"HEAD", "packed-refs"}:
            add(path)
    for path in sorted(worktree.rglob("*")):
        add(path)
    return state


def _assert_durable_evidence_preserved(
    repo: Path,
    *,
    run_id: str,
    worktree: Path,
    plans: tuple[Path, ...],
    before: dict[str, bytes],
) -> None:
    """Compare captured evidence bytes after daemon work.

    Every file must stay byte-identical except the run's event journal, where
    the new daemon's own fail-closed startup reconciliation may append only
    deduplicated ``reconciled`` observation records; the prior journal bytes
    must survive verbatim as a prefix.
    """
    after = _durable_evidence(repo, run_id=run_id, worktree=worktree, plans=plans)
    journal = str(repo / ".aflow" / "runs" / run_id / "events.jsonl")
    lock = str(repo / ".aflow" / "runs" / run_id / ".events.lock")
    assert {
        key: value for key, value in after.items() if key not in {journal, lock}
    } == {
        key: value for key, value in before.items() if key not in {journal, lock}
    }
    observed_lock = after.get(lock)
    assert observed_lock in (None, b"", before.get(lock))
    prior = before.get(journal, b"")
    appended = after.get(journal, prior)
    assert appended.startswith(prior)
    for line in appended[len(prior):].splitlines():
        assert json.loads(line)["event_type"] == "reconciled"


def _daemon(
    tmp_path: Path, repo: Path, monkeypatch, units: InMemoryUnitManager | None = None
) -> AflowDaemon:
    # The configuration lives outside the repository so its pair lock file
    # never counts as worktree dirtiness, matching production deployments.
    config_path = tmp_path / "aflow.toml"
    config_path.write_text("")
    executable = tmp_path / "aflow"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    environment_file = tmp_path / "env"
    environment_file.write_text("")
    config = DaemonConfig(
        repo_root=repo,
        config_path=config_path,
        aflow_executable=executable,
        environment_file=environment_file,
        release_identity=hashlib.sha256(b"test").hexdigest(),
    )
    monkeypatch.setattr(
        "aflow.daemon.load_workflow_config",
        lambda *_args, **_kwargs: _workflow_config(),
    )
    daemon = AflowDaemon(config, units=units or InMemoryUnitManager())
    daemon.start()
    return daemon


def _plan_a_history(repo: Path, a_plan: Path, tmp_path: Path) -> tuple[str, Path]:
    a_identity = create_plan_identity(repo, a_plan)
    a_worktree = _worktree(repo, "a-worktree", "plan-a-branch", tmp_path)
    _record_history(
        repo,
        a_plan,
        run_id="a-run",
        identity=a_identity,
        worktree=a_worktree,
        branch="plan-a-branch",
    )
    (a_worktree / "a-work.py").write_text("a = 1\n")
    return a_identity, a_worktree


def test_disjoint_known_plan_history_keeps_selection_selectable_end_to_end(
    tmp_path: Path, monkeypatch
) -> None:
    repo, a_plan, b_plan = _repo(tmp_path)
    _, a_worktree = _plan_a_history(repo, a_plan, tmp_path)
    # A second piece of disjoint A-side history: an identity-free completed
    # legacy run recorded at a different path whose verified owner set is
    # empty, alongside the explicit-ID run and the dirty preserved worktree.
    legacy_plan = repo / "plans" / "archive" / "legacy-a.md"
    _record_history(repo, legacy_plan, run_id="legacy-run", status="completed")
    assert plan_identity_alias_owners(repo, legacy_plan) == frozenset()
    # The selected plan carries a fresh identity so the matcher must prove
    # disjointness through ownership, not through the identity-free fallback.
    create_plan_identity(repo, b_plan)
    workflow = _workflow_config().workflows["managed"]

    state_before = _snapshot(repo)
    evidence_before = _durable_evidence(
        repo, run_id="a-run", worktree=a_worktree, plans=(a_plan, b_plan)
    )

    # Read-only startup inspection stays clean for the disjoint selection
    # and writes nothing at all.
    context = project_startup_context(repo, b_plan, workflow=workflow)
    assert context.recommendation == "start"
    assert context.related_runs == ()
    assert context.related_runs_complete is True
    assert "prior_work_unverified" not in context.reason_codes
    require_safe_fresh_worktree(repo, b_plan, workflow)
    prepared = prepare_startup(_request(tmp_path / "aflow.toml", repo, b_plan))
    assert not isinstance(prepared, StartupQuestion)
    assert isinstance(prepared, PreparedRun)
    assert prepared.workflow_name == "managed"
    assert _snapshot(repo) == state_before
    assert _durable_evidence(
        repo, run_id="a-run", worktree=a_worktree, plans=(a_plan, b_plan)
    ) == evidence_before

    # The daemon admits the disjoint plan and starts exactly one unit.
    units = InMemoryUnitManager()
    daemon = _daemon(tmp_path, repo, monkeypatch, units)
    started = daemon.service.start(
        _request(tmp_path / "aflow.toml", repo, b_plan),
        caller_scope="local",
        idempotency_key="start-b",
    )
    assert isinstance(started, StartRunResult)
    assert started.status == "running"
    assert len(units.start_calls) == 1

    # The same idempotency key replays the same run without a new unit.
    again = daemon.service.start(
        _request(tmp_path / "aflow.toml", repo, b_plan),
        caller_scope="local",
        idempotency_key="start-b",
    )
    assert isinstance(again, StartRunResult)
    assert again.run_id == started.run_id
    assert again.created is False
    assert len(units.start_calls) == 1

    # The unrelated successful startup left plan A's durable evidence -
    # run metadata, launch receipt/manifest, owner records, both plan
    # files, branch refs, and the dirty retained worktree - byte-identical,
    # added no controller state for A, and created no worktree. Only plan
    # B's new-run artifacts and the new daemon's deduplicated reconciled
    # observation in A's event journal were written.
    _assert_durable_evidence_preserved(
        repo,
        run_id="a-run",
        worktree=a_worktree,
        plans=(a_plan, b_plan),
        before=evidence_before,
    )
    assert _worktrees(repo) == [str(repo), str(a_worktree)]
    # A gained no controller state: the only start request is plan B's own.
    requests = sorted(
        path.name for path in (repo / ".aflow" / "start-requests").glob("*.json")
    )
    assert requests == [f"{started.run_id}.json"]
    started_b_record = json.loads(
        (repo / ".aflow" / "start-requests" / f"{started.run_id}.json").read_text()
    )
    assert started_b_record["state"] == "unit_started"


def test_protected_same_plan_work_blocks_before_provider_and_worktree(
    tmp_path: Path, monkeypatch
) -> None:
    repo, a_plan, b_plan = _repo(tmp_path)
    b_identity = create_plan_identity(repo, b_plan)
    b_worktree = _worktree(repo, "b-worktree", "plan-b-branch", tmp_path)
    _record_history(
        repo,
        b_plan,
        run_id="b-run",
        identity=b_identity,
        worktree=b_worktree,
        branch="plan-b-branch",
    )
    (b_worktree / "b-work.py").write_text("b = 1\n")
    workflow = _workflow_config().workflows["managed"]
    worktrees_before = _worktrees(repo)
    evidence_before = _durable_evidence(
        repo, run_id="b-run", worktree=b_worktree, plans=(a_plan, b_plan)
    )

    with pytest.raises(PriorWorkStartupError) as guard:
        require_safe_fresh_worktree(repo, b_plan, workflow)
    assert guard.value.code == "prior_work_requires_recovery"

    with pytest.raises(PriorWorkStartupError) as preparation:
        prepare_startup(_request(tmp_path / "aflow.toml", repo, b_plan))
    assert preparation.value.code == "prior_work_requires_recovery"

    units = InMemoryUnitManager()
    daemon = _daemon(tmp_path, repo, monkeypatch, units)
    with pytest.raises(DaemonStartupError) as daemon_error:
        daemon.service.start(
            _request(tmp_path / "aflow.toml", repo, b_plan),
            caller_scope="local",
            idempotency_key="start-b-again",
        )
    assert daemon_error.value.code == "prior_work_requires_recovery"

    # The protected plan's durable evidence stayed byte-identical across
    # every rejected startup attempt, apart from the new daemon's own
    # deduplicated reconciled observation in the event journal.
    _assert_durable_evidence_preserved(
        repo,
        run_id="b-run",
        worktree=b_worktree,
        plans=(a_plan, b_plan),
        before=evidence_before,
    )
    # No provider call, no new worktree, and no controller state for the
    # rejected startup: only bounded failed-preparation bookkeeping (the
    # reserved start request record and launch intent) remains.
    assert units.start_calls == []
    assert _worktrees(repo) == worktrees_before
    records = [
        json.loads(record_path.read_text())
        for record_path in sorted(
            (repo / ".aflow" / "start-requests").glob("*.json")
        )
    ]
    assert records, "rejected startup left no bounded preparation bookkeeping"
    for payload in records:
        if payload.get("state") == "unit_started":
            pytest.fail("rejected startup created controller state")
        assert payload["state"] == "needs_attention"
        assert payload["startup_failure"]["code"] == "prior_work_requires_recovery"


def test_capacity_rejection_is_separate_from_history_relevance(
    tmp_path: Path, monkeypatch
) -> None:
    repo, a_plan, b_plan = _repo(tmp_path)
    _plan_a_history(repo, a_plan, tmp_path)
    workflow = _workflow_config().workflows["managed"]

    # History relevance for the disjoint selection is clean: the protected
    # plan's history must not itself make the selection ambiguous.
    context = project_startup_context(repo, b_plan, workflow=workflow)
    assert context.recommendation == "start"

    settings = ProjectSettingsService(repo)
    settings.update(
        ProjectSettings(max_concurrent_implementations=1),
        expected_revision=settings.read().revision,
    )
    units = InMemoryUnitManager()
    daemon = _daemon(tmp_path, repo, monkeypatch, units)
    admission = ProjectAdmission(repo, unit_manager=units)
    occupied = admission.acquire("occupied-run", idempotency_key="occupied")

    with pytest.raises(DaemonStartupError) as error:
        daemon.service.start(
            _request(tmp_path / "aflow.toml", repo, b_plan),
            caller_scope="local",
            idempotency_key="start-b",
        )
    # The rejection is the capacity boundary, not prior-work ambiguity.
    assert error.value.code == "project_capacity_reached"
    assert "prior_work" not in str(error.value)
    assert units.start_calls == []
    admission.release(occupied.run_id, occupied.nonce)
