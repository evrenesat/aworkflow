from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import importlib.util
from pathlib import Path
import json
import subprocess
import sys
from threading import Event

import pytest

from aflow.api.models import PreparedRun, StartupQuestion, StartupQuestionKind, StartupRequest
from aflow.config import (
    GoTransition,
    TeamConfig,
    WorkflowConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
)
from aflow.control_plane import (
    ControlConflictError,
    RepositoryNotFoundError,
    InMemoryUnitManager,
    LaunchManifest,
    RunStatus,
    UnitState,
    append_run_event,
    create_launch_manifest,
    read_events,
    write_launch_phase,
)
from aflow.daemon import (
    AflowDaemon,
    DaemonConfig,
    DaemonError,
    DaemonIdempotencyConflict,
    _startup_request_digest,
)


GUARD_RECOVERY_PATH = (
    Path(__file__).resolve().parents[1]
    / "aflow"
    / "bundled_skills"
    / "aflow-guard-development-run"
    / "scripts"
    / "aflow_guard_recovery.py"
)


def _guard_recovery_module():
    spec = importlib.util.spec_from_file_location("aflowd_guard_recovery", GUARD_RECOVERY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _workflow_config() -> WorkflowUserConfig:
    workflow = WorkflowConfig(
        steps={
            "implement": WorkflowStepConfig(
                role="worker",
                prompts=("p",),
                go=(GoTransition(to="END", when="DONE"),),
            )
        },
        first_step="implement",
    )
    return WorkflowUserConfig(
        roles={"worker": "codex.worker"},
        workflows={"managed": workflow},
        prompts={"p": "Work."},
    )




def _two_step_workflow_config() -> WorkflowUserConfig:
    implement = WorkflowStepConfig(
        role="worker",
        prompts=("p",),
        go=(GoTransition(to="review", when="DONE"),),
    )
    review = WorkflowStepConfig(
        role="worker",
        prompts=("p",),
        go=(GoTransition(to="END", when="DONE"),),
    )
    excluded = WorkflowStepConfig(role="worker", prompts=("p",))
    return WorkflowUserConfig(
        roles={"worker": "codex.worker"},
        workflows={
            "managed": WorkflowConfig(
                declared_steps={
                    "draft": excluded,
                    "implement": implement,
                    "review": review,
                },
                steps={"implement": implement, "review": review},
                first_step="implement",
                excluded_steps=("draft",),
            )
        },
        prompts={"p": "Work."},
    )


def _prepared_for_request(request: StartupRequest) -> PreparedRun:
    selected = "review" if request.start_step in {"2", "review"} else "implement"
    return PreparedRun(
        workflow_name="managed",
        repo_root=request.repo_root,
        plan_path=request.plan_path,
        config_path=request.config_path,
        max_turns=request.max_turns or 2,
        team=request.team,
        extra_instructions=request.extra_instructions,
        start_step=selected,
    )


def _review_workflow_config() -> WorkflowUserConfig:
    workflow = WorkflowConfig(
        steps={
            "review": WorkflowStepConfig(
                role="worker",
                prompts=("review_prompt",),
                go=(GoTransition(to="END", when="DONE"),),
            )
        },
        first_step="review",
    )
    return WorkflowUserConfig(
        roles={"worker": "codex.worker"},
        workflows={"managed": workflow},
        prompts={"review_prompt": "Use aflow-review-checkpoint."},
    )


def _daemon(tmp_path: Path, monkeypatch, units: InMemoryUnitManager) -> tuple[AflowDaemon, StartupRequest]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    plan_path = repo_root / "plan.md"
    plan_path.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n")
    config_path = repo_root / "aflow.toml"
    config_path.write_text("")
    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n")
    executable = repo_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    workflow_config = _workflow_config()
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda path: workflow_config)
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
        units=units,
    )
    daemon.start()
    request = StartupRequest(
        repo_root=repo_root,
        plan_path=plan_path,
        config_path=config_path,
        workflow_config=workflow_config,
        workflow_name="managed",
        start_step=None,
        max_turns=2,
        team=None,
    )
    return daemon, request


def _prepared(request: StartupRequest) -> PreparedRun:
    return PreparedRun(
        workflow_name="managed",
        repo_root=request.repo_root,
        plan_path=request.plan_path,
        config_path=request.config_path,
        max_turns=2,
        team=None,
        extra_instructions=(),
        start_step="implement",
    )


@pytest.mark.parametrize(
    "case",
    [
        "owner_stopped",
        "legacy",
        "read_error",
        "pending_startup",
        "prepared_step",
        "missing_unit",
        "active_unit",
    ],
)
def test_daemon_final_progress_all_status_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    units = InMemoryUnitManager()
    daemon, _request = _daemon(tmp_path, monkeypatch, units)
    repository = daemon.application.repository
    run_ids = {
        "owner_stopped": "owner-stopped",
        "legacy": "20260809T172123Z-abc12345",
        "read_error": "read-error",
        "pending_startup": "pending-startup",
        "prepared_step": "prepared-step",
        "missing_unit": "missing-unit",
        "active_unit": "active-unit",
    }
    run_id = run_ids[case]
    if case == "owner_stopped":
        base = RunStatus(
            run_id=run_id,
            status="owner_stopped",
            evidence={"recorded_status": "owner_stopped"},
        )
    elif case == "legacy":
        base = RunStatus(
            run_id=run_id,
            status="completed",
            ownership="legacy",
            evidence={"recorded_status": "completed"},
        )
    elif case == "pending_startup":
        base = RunStatus(
            run_id=run_id,
            status="needs_attention",
            evidence={
                "startup_state": "awaiting_startup_answer",
                "startup_question_valid": True,
            },
        )
    else:
        base = RunStatus(
            run_id=run_id,
            status="running",
            evidence={
                "has_run_metadata": True,
                "recorded_status": "running",
            },
        )
        if case == "prepared_step":
            base = replace(base, status="needs_attention")

    reads: list[tuple[str, bool]] = []

    def get_status(requested_run_id: str, *, include_progress: bool = True) -> RunStatus:
        reads.append((requested_run_id, include_progress))
        return base

    projections: list[RunStatus] = []

    def counted_projection(status, _run_dir, _metadata):
        projections.append(status)
        return status

    monkeypatch.setattr(repository, "get_run_status", get_status)
    monkeypatch.setattr(repository, "_with_progress", counted_projection)
    monkeypatch.setattr(daemon.service, "_can_resume", lambda _status: False)

    if case == "active_unit":
        units.units[f"aflow-run-{run_id}.service"] = UnitState(
            name=f"aflow-run-{run_id}.service",
            active_state="active",
            sub_state="running",
        )
    if case == "read_error":
        monkeypatch.setattr(
            daemon.service,
            "_read_record",
            lambda _run_id: (_ for _ in ()).throw(DaemonError("read failed")),
        )
    elif case == "pending_startup":
        monkeypatch.setattr(
            daemon.service,
            "_read_record",
            lambda _run_id: {
                "state": "awaiting_startup_answer",
                "question": {
                    "kind": StartupQuestionKind.PICK_STEP.value,
                    "message": "Choose a step",
                    "choices": ["implement"],
                },
            },
        )
    elif case == "prepared_step":
        monkeypatch.setattr(
            daemon.service,
            "_read_record",
            lambda _run_id: {
                "prepared": {
                    "start_step": "review",
                    "skipped_steps": ["implement"],
                }
            },
        )

    status = daemon.service.run_status(run_id)

    assert reads == [(run_id, False)]
    assert len(projections) == 1
    projected = projections[0]
    expected = {
        "owner_stopped": ("owner_stopped", "unknown"),
        "legacy": ("completed", "unknown"),
        "read_error": ("needs_attention", "unknown"),
        "pending_startup": ("awaiting_startup_answer", "unknown"),
        "prepared_step": ("needs_attention", "unknown"),
        "missing_unit": ("needs_attention", "unknown"),
        "active_unit": ("running", "active"),
    }[case]
    assert (projected.status, projected.activity) == expected
    assert status == projected
    if case == "pending_startup":
        assert projected.evidence["startup_question"]["run_id"] == run_id
    if case == "prepared_step":
        assert projected.selected_start_step == "review"
        assert projected.skipped_steps == ("implement",)


def _neutral_recovery_receipt(
    helper,
    request: StartupRequest,
    predecessor_run_id: str,
    predecessor_idempotency_key: str,
    base_head: str,
) -> dict[str, object]:
    plan_digest = hashlib.sha256(request.plan_path.read_bytes()).hexdigest()
    choices_digest = hashlib.sha256(b"daemon replacement choices").hexdigest()
    startup_request_id = f"startup-{predecessor_run_id}"
    return {
        "schema_version": helper.SCHEMA_VERSION,
        "evidence": {
            "source": "matched_launch_evidence",
            "matched": True,
            "authorization": {
                "authorized": True,
                "ownership_mode": "aflowd",
                "launch_surface": "web_mcp",
            },
            "request": {
                "startup_request_id": startup_request_id,
                "idempotency_key": predecessor_idempotency_key,
            },
            "failure": {
                "status": "failed",
                "phase": "pre_controller",
                "kind": "missing_or_blank_git_tracking",
                "terminal": True,
                "missing_fields": ["plan_branch", "pre_handoff_base_head"],
            },
            "identity": {
                "predecessor_run_id": predecessor_run_id,
                "matched_run_id": predecessor_run_id,
                "startup_request_id": startup_request_id,
            },
            "work": {
                "started_turns": 0,
                "finalized_turns": 0,
                "worker_changes": False,
                "branch_created": False,
                "worktree_created": False,
                "plan_content_unchanged": True,
                "plan_semantics_unchanged": True,
                "launch_choices_unchanged": True,
            },
            "ownership": {
                "controller": "none",
                "child": "none",
                "provider_session": "none",
            },
            "selected_launch": {
                "repository": str(request.repo_root),
                "plan": str(request.plan_path),
                "workflow": "managed",
                "team": "codex",
                "start_step": "review",
                "max_turns": request.max_turns or 2,
                "extra_instructions": [],
                "choices_digest": choices_digest,
                "original_plan_content_sha256": plan_digest,
                "branch": "main",
                "base_head": base_head,
            },
            "derivation": {
                "mechanically_derivable": True,
                "method": "in_place_current_branch",
                "branch": "main",
                "base_head": base_head,
            },
            "match": {
                "startup_request_id": startup_request_id,
                "selected_plan": str(request.plan_path),
                "selected_choices_digest": choices_digest,
                "idempotency_key": predecessor_idempotency_key,
                "predecessor_run_id": predecessor_run_id,
            },
            "content": {
                "sha256": plan_digest,
                "unchanged": True,
                "semantic_unchanged": True,
            },
        },
    }


@pytest.mark.parametrize(
    "invalid_state",
    ["started", "ambiguous", "no_head", "tracking_no_checkpoint"],
)
def test_daemon_git_tracking_preflight_failure_does_not_allocate_run_artifacts(
    tmp_path: Path,
    monkeypatch,
    invalid_state: str,
) -> None:
    from aflow.api.startup import (
        PLAN_ADMISSION_CHECKPOINT_SAFE_MESSAGE,
        PLAN_ADMISSION_ERROR_CODE,
        PLAN_ADMISSION_TRACKING_SAFE_MESSAGE,
    )
    from aflow.daemon import DaemonStartupError

    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    review_config = _review_workflow_config()
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda path: review_config)
    request = StartupRequest(
        repo_root=request.repo_root,
        plan_path=request.plan_path,
        config_path=request.config_path,
        workflow_config=review_config,
        workflow_name="managed",
        start_step=None,
        max_turns=2,
        team=None,
    )
    plan_path = request.plan_path

    if invalid_state in {"started", "ambiguous"}:
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=request.repo_root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=request.repo_root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=request.repo_root,
            check=True,
            capture_output=True,
        )
        readme = request.repo_root / "README.md"
        readme.write_text("ready\n")
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=request.repo_root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=request.repo_root,
            check=True,
            capture_output=True,
        )

    if invalid_state == "started":
        plan_path.write_text(
            "# Plan\n\n### [ ] Checkpoint 1: First\n- [x] started\n- [ ] step\n"
        )
    elif invalid_state == "ambiguous":
        plan_path.write_text(
            "# Plan\n\n## Git Tracking\n\n- Plan Branch: ``\n"
            "- Pre-Handoff Base HEAD: `abc`\n\n## Git Tracking\n\n"
            "- Plan Branch: ``\n- Pre-Handoff Base HEAD: `def`\n\n"
            "### [ ] Checkpoint 1: First\n- [ ] step\n"
        )
    elif invalid_state == "tracking_no_checkpoint":
        plan_path.write_text(
            "# Plan\n\n## Git Tracking\n\n"
            "- Plan Branch: ``\n- Pre-Handoff Base HEAD: ``\n"
        )
    original_bytes = plan_path.read_bytes()

    expected_exception = DaemonError if invalid_state == "no_head" else DaemonStartupError
    with pytest.raises(expected_exception) as caught:
        daemon.service.start(
            request,
            caller_scope="project:review",
            idempotency_key=f"invalid-{invalid_state}",
        )

    if isinstance(caught.value, DaemonStartupError):
        expected_message = (
            PLAN_ADMISSION_CHECKPOINT_SAFE_MESSAGE
            if invalid_state == "tracking_no_checkpoint"
            else PLAN_ADMISSION_TRACKING_SAFE_MESSAGE
        )
        assert caught.value.run_id is None
        assert caught.value.code == PLAN_ADMISSION_ERROR_CODE
        assert str(caught.value) == expected_message
    else:
        assert str(caught.value) == "startup plan preflight failed"

    assert plan_path.read_bytes() == original_bytes
    assert units.start_calls == []
    assert not (request.repo_root / ".aflow" / "launches").exists()
    assert not (request.repo_root / ".aflow" / "runs").exists()
    assert not (request.repo_root / ".aflow" / "last_run_id").exists()


@pytest.mark.parametrize(
    "recorded_base",
    [
        "0000000000000000000000000000000000000000",
        "",
    ],
    ids=["mismatch-started", "empty-base-started"],
)
def test_daemon_started_base_failure_preserves_reserved_typed_failure(
    tmp_path: Path,
    monkeypatch,
    recorded_base: str,
) -> None:
    from aflow.api.startup import (
        PLAN_ADMISSION_ERROR_CODE,
        PLAN_ADMISSION_STARTED_HISTORY_KIND,
        PLAN_ADMISSION_STARTED_HISTORY_SAFE_MESSAGE,
    )
    from aflow.daemon import DaemonStartupError

    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    review_config = _review_workflow_config()
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda path: review_config)
    request = replace(request, workflow_config=review_config)

    subprocess.run(
        ("git", "init", "-b", "main"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "config", "user.email", "test@test.com"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "config", "user.name", "Test"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    (request.repo_root / "README.md").write_text("ready\n")
    subprocess.run(
        ("git", "add", "README.md"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "commit", "-m", "init"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    request.plan_path.write_text(
        "# Plan\n\n"
        "## 2. Git Tracking\n\n"
        "- Plan Branch: ``\n"
        f"- Pre-Handoff Base HEAD: `{recorded_base}`\n\n"
        "### [ ] Checkpoint 1: First\n"
        "- [x] started\n"
        "- [ ] remaining\n"
    )

    with pytest.raises(DaemonStartupError) as first:
        daemon.service.start(
            request,
            caller_scope="project:review",
            idempotency_key="mismatch-started",
        )

    run_id = first.value.run_id
    assert run_id is not None
    assert first.value.code == PLAN_ADMISSION_ERROR_CODE
    assert str(first.value) == PLAN_ADMISSION_STARTED_HISTORY_SAFE_MESSAGE

    with pytest.raises(DaemonStartupError) as retry:
        daemon.service.start(
            request,
            caller_scope="project:review",
            idempotency_key="mismatch-started",
        )

    assert retry.value.run_id == run_id
    assert retry.value.code == PLAN_ADMISSION_ERROR_CODE
    assert str(retry.value) == PLAN_ADMISSION_STARTED_HISTORY_SAFE_MESSAGE
    assert units.start_calls == []
    assert [item.run_id for item in daemon.application.repository.list_runs(limit=100).runs] == [run_id]
    status = daemon.service.run_status(run_id)
    assert status.status == "needs_attention"
    assert status.reason == PLAN_ADMISSION_STARTED_HISTORY_SAFE_MESSAGE
    assert status.evidence["startup_failure"]["code"] == PLAN_ADMISSION_ERROR_CODE
    assert status.evidence["startup_failure"]["kind"] == PLAN_ADMISSION_STARTED_HISTORY_KIND


def test_guard_replacement_uses_new_key_after_terminal_metadata_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from aflow.api.startup import PLAN_ADMISSION_TRACKING_KIND, PlanAdmissionError
    from aflow.daemon import DaemonStartupError

    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    review_config = replace(
        _review_workflow_config(),
        teams={"codex": TeamConfig(roles={"worker": "codex.worker"})},
    )
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda path: review_config)
    request = replace(
        request,
        workflow_config=review_config,
        start_step="review",
        team="codex",
    )

    subprocess.run(
        ("git", "init", "-b", "main"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "config", "user.email", "test@test.com"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "config", "user.name", "Test"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    (request.repo_root / "README.md").write_text("ready\n")
    subprocess.run(
        ("git", "add", "README.md"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "commit", "-m", "init"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    request.plan_path.write_text(
        "# Plan\n\n"
        "## 2. Git Tracking\n\n"
        "- Plan Branch: ``\n"
        "- Pre-Handoff Base HEAD: ``\n\n"
        "### [ ] Checkpoint 1: First\n"
        "- [x] started\n"
        "- [ ] remaining\n"
    )
    predecessor_key = "daemon-predecessor"

    def reject_metadata(_: StartupRequest) -> PreparedRun:
        raise PlanAdmissionError(PLAN_ADMISSION_TRACKING_KIND)

    monkeypatch.setattr("aflow.daemon.prepare_startup", reject_metadata)
    with pytest.raises(DaemonStartupError) as first:
        daemon.service.start(
            request,
            caller_scope="project:guard",
            idempotency_key=predecessor_key,
        )
    predecessor_run_id = first.value.run_id
    assert predecessor_run_id is not None

    base_head = subprocess.check_output(
        ("git", "rev-parse", "HEAD"),
        cwd=request.repo_root,
        text=True,
    ).strip()
    helper = _guard_recovery_module()
    receipt = _neutral_recovery_receipt(
        helper,
        request,
        predecessor_run_id,
        predecessor_key,
        base_head,
    )
    state_dir = tmp_path / "guard-state"
    state_dir.mkdir()
    preparation_calls = 0

    def prepare_success(value: StartupRequest) -> PreparedRun:
        nonlocal preparation_calls
        preparation_calls += 1
        return _prepared_for_request(value)

    monkeypatch.setattr("aflow.daemon.prepare_startup", prepare_success)

    def launch(selected: dict[str, object]) -> dict[str, object]:
        replacement_key = selected["idempotency_key"]
        assert isinstance(replacement_key, str)
        assert replacement_key != predecessor_key
        started = daemon.service.start(
            request,
            caller_scope="project:guard",
            idempotency_key=replacement_key,
        )
        return {
            "status": "acknowledged",
            "successor": {
                "run_id": started.run_id,
                "idempotency_key": replacement_key,
            },
        }

    recovered = helper.attempt_recovery(receipt, state_dir, launch)
    repeated = helper.attempt_recovery(receipt, state_dir, launch)
    with pytest.raises(DaemonStartupError) as replay:
        daemon.service.start(
            request,
            caller_scope="project:guard",
            idempotency_key=predecessor_key,
        )
    replayed = replay.value

    replacement_key = recovered["record"]["request"]["replacement_idempotency_key"]
    persisted = json.loads(
        (state_dir / helper.ATTEMPT_FILE_NAME).read_text(encoding="utf-8")
    )
    assert recovered["status"] == "acknowledged"
    assert repeated["status"] == "already_attempted"
    assert replayed.run_id == predecessor_run_id
    assert recovered["record"]["request"]["predecessor_idempotency_key"] == predecessor_key
    assert replacement_key != predecessor_key
    assert persisted["successor"]["idempotency_key"] == replacement_key
    assert persisted["successor"]["run_id"] != predecessor_run_id
    assert preparation_calls == 1
    assert len(units.start_calls) == 1


@pytest.mark.parametrize("failure", ["backup", "value"])
def test_unclassified_preallocation_failures_remain_generic_and_artifact_free(
    tmp_path: Path,
    monkeypatch,
    failure: str,
) -> None:
    from aflow.daemon import DaemonStartupError

    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    review_config = _review_workflow_config()
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda path: review_config)
    request = replace(request, workflow_config=review_config)

    subprocess.run(
        ("git", "init", "-b", "main"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "config", "user.email", "test@test.com"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "config", "user.name", "Test"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    (request.repo_root / "README.md").write_text("ready\n")
    subprocess.run(
        ("git", "add", "README.md"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "commit", "-m", "init"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    sentinel = f"private-preflight-{failure}"
    if failure == "backup":
        def reject_backup(*args, **kwargs):
            raise PermissionError(sentinel)

        monkeypatch.setattr("aflow.workflow._backup_original_plan", reject_backup)
    else:
        monkeypatch.setattr("aflow.workflow._backup_original_plan", lambda *args, **kwargs: None)

        def reject_value(*args, **kwargs):
            raise ValueError(sentinel)

        monkeypatch.setattr(
            "aflow.workflow._prepare_required_git_tracking_before_allocation",
            reject_value,
        )

    with pytest.raises(DaemonError) as caught:
        daemon.service.start(
            request,
            caller_scope="project:review",
            idempotency_key=f"generic-preflight-{failure}",
        )

    assert not isinstance(caught.value, DaemonStartupError)
    assert str(caught.value) == "startup plan preflight failed"
    assert sentinel not in str(caught.value)
    assert units.start_calls == []
    assert not (request.repo_root / ".aflow" / "launches").exists()
    assert not (request.repo_root / ".aflow" / "runs").exists()


def test_unclassified_reserved_startup_value_error_remains_generic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from aflow.daemon import DaemonStartupError

    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    review_config = _review_workflow_config()
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda path: review_config)
    request = replace(request, workflow_config=review_config)

    subprocess.run(
        ("git", "init", "-b", "main"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "config", "user.email", "test@test.com"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "config", "user.name", "Test"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    (request.repo_root / "README.md").write_text("ready\n")
    subprocess.run(
        ("git", "add", "README.md"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "commit", "-m", "init"),
        cwd=request.repo_root,
        check=True,
        capture_output=True,
    )
    sentinel = "private-startup-value-error"

    def reject_preflight(*args, **kwargs):
        raise ValueError(sentinel)

    monkeypatch.setattr(
        "aflow.api.startup.preflight_pre_handoff_base_head_refresh",
        reject_preflight,
    )

    with pytest.raises(DaemonStartupError) as caught:
        daemon.service.start(
            request,
            caller_scope="project:review",
            idempotency_key="generic-reserved-preflight",
        )

    assert caught.value.run_id is not None
    assert caught.value.code == "startup_failed"
    assert str(caught.value) == "Startup base/history validation could not be completed."
    assert sentinel not in str(caught.value)
    assert units.start_calls == []


def test_daemon_persists_startup_question_then_launches_once_when_answered(
    tmp_path: Path, monkeypatch
) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    question = StartupQuestion(
        kind=StartupQuestionKind.PICK_STEP,
        message="Choose a step",
        choices=["implement"],
    )
    def prepare(value: StartupRequest) -> StartupQuestion:
        assert value.reserved_run_id is not None
        assert (tmp_path / "repo" / ".aflow" / "launches" / f"{value.reserved_run_id}.json").is_file()
        record = tmp_path / "repo" / ".aflow" / "start-requests" / f"{value.reserved_run_id}.json"
        assert '"state":"preparing"' in record.read_text()
        return question

    monkeypatch.setattr("aflow.daemon.prepare_startup", prepare)
    monkeypatch.setattr(
        "aflow.daemon.prepare_startup_with_answer",
        lambda question, request, answer: _prepared(request),
    )

    pending = daemon.service.start(request, caller_scope="project:one", idempotency_key="start-1")

    assert pending.run_id is not None
    assert pending.question_id == f"startup-{pending.run_id}-q1"
    assert units.start_calls == []
    record = tmp_path / "repo" / ".aflow" / "start-requests" / f"{pending.run_id}.json"
    assert '"state":"awaiting_startup_answer"' in record.read_text()
    assert (tmp_path / "repo" / ".aflow" / "launches" / f"{pending.run_id}.json").is_file()

    started = daemon.service.answer_startup(
        pending.question_id,
        "implement",
        caller_scope="project:one",
        idempotency_key="answer-1",
    )

    assert started.status == "running"
    assert len(units.start_calls) == 1
    name, argv, cwd = units.start_calls[0]
    assert name == f"aflow-run-{started.run_id}.service"
    assert argv[:2] == (str((tmp_path / "repo" / "release" / "bin" / "aflow").resolve()), "daemon-worker")
    assert cwd == (tmp_path / "repo").resolve()
    events = read_events(tmp_path / "repo" / ".aflow" / "runs" / started.run_id)
    attempt = next(event for event in events if event.event_type == "daemon_start_attempt")
    assert attempt.data["release_identity"] == "release-test"
    assert attempt.data["environment_file"]["path"] == str((tmp_path / "repo" / "aflowd.env").resolve())

    replay = daemon.service.answer_startup(
        pending.question_id,
        "implement",
        caller_scope="project:one",
        idempotency_key="answer-1",
    )
    assert replay.run_id == started.run_id
    assert replay.created is False
    assert len(units.start_calls) == 1


def test_daemon_replays_active_idempotent_start_and_restarts_by_reconciling_only(
    tmp_path: Path, monkeypatch
) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    monkeypatch.setattr("aflow.daemon.prepare_startup", _prepared)

    first = daemon.service.start(request, caller_scope="project:one", idempotency_key="start-1")
    replay = daemon.service.start(request, caller_scope="project:one", idempotency_key="start-1")
    restarted = AflowDaemon(daemon._config, units=units)
    restarted.start()

    assert first.run_id == replay.run_id
    assert replay.created is False
    assert len(units.start_calls) == 1
    assert restarted.ready is True
    assert len(units.start_calls) == 1


def test_daemon_recovers_launch_requested_record_before_child_start_without_recursion(
    tmp_path: Path, monkeypatch
) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    run_id = "launch-requested-gap"
    caller_scope = "project:one"
    idempotency_key = "start-1"
    manifest = daemon.service._initial_manifest_for(
        run_id=run_id,
        request=request,
        caller_scope=caller_scope,
        idempotency_key=idempotency_key,
    )
    manifest_path = tmp_path / "repo" / ".aflow" / "launches" / f"{run_id}.json"
    create_launch_manifest(tmp_path / "repo", manifest)
    immutable_manifest = manifest_path.read_text()
    prepared = _prepared(request)
    record = daemon.service._new_start_record(
        run_id=run_id,
        request=request,
        request_digest=_startup_request_digest(request),
        caller_scope=caller_scope,
        idempotency_key=idempotency_key,
        state="launch_requested",
        prepared=prepared,
    )
    record["manifest_request_digest"] = manifest.request_digest
    daemon.service._create_record(record)

    replayed = daemon.service.start(
        request,
        caller_scope=caller_scope,
        idempotency_key=idempotency_key,
    )
    repeated = daemon.service.start(
        request,
        caller_scope=caller_scope,
        idempotency_key=idempotency_key,
    )

    assert replayed.run_id == run_id
    assert replayed.status == "running"
    assert repeated.status == "running"
    assert len(units.start_calls) == 1
    assert manifest_path.read_text() == immutable_manifest


def test_daemon_replays_prior_answer_as_current_follow_up_question(tmp_path: Path, monkeypatch) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    recovery_question = StartupQuestion(
        kind=StartupQuestionKind.CONFIRM_RECOVERY,
        message="Recover the existing run?",
    )
    dirty_question = StartupQuestion(
        kind=StartupQuestionKind.CONFIRM_WORKTREE_DIRTY,
        message="Continue with a dirty worktree?",
    )
    answers: list[tuple[StartupQuestionKind, str | int | bool]] = []
    monkeypatch.setattr("aflow.daemon.prepare_startup", lambda value: recovery_question)

    def prepare_with_answer(
        question: StartupQuestion,
        value: StartupRequest,
        answer: str | int | bool,
    ) -> StartupQuestion:
        answers.append((question.kind, answer))
        assert question == recovery_question
        assert value.reserved_run_id is not None
        return dirty_question

    monkeypatch.setattr("aflow.daemon.prepare_startup_with_answer", prepare_with_answer)

    first = daemon.service.start(request, caller_scope="project:one", idempotency_key="start-1")
    follow_up = daemon.service.answer_startup(
        first.question_id,
        True,
        caller_scope="project:one",
        idempotency_key="answer-1",
    )
    replay = daemon.service.answer_startup(
        first.question_id,
        True,
        caller_scope="project:one",
        idempotency_key="answer-1",
    )

    assert first.question_id.endswith("-q1")
    assert follow_up.question_id.endswith("-q2")
    assert replay == follow_up
    assert answers == [(StartupQuestionKind.CONFIRM_RECOVERY, True)]
    assert units.start_calls == []
    with pytest.raises(DaemonIdempotencyConflict):
        daemon.service.answer_startup(
            first.question_id,
            False,
            caller_scope="project:one",
            idempotency_key="answer-1",
        )
    with pytest.raises(DaemonIdempotencyConflict):
        daemon.service.answer_startup(
            first.question_id,
            True,
            caller_scope="project:one",
            idempotency_key="answer-2",
        )


def test_daemon_serializes_startup_answers_across_service_instances(tmp_path: Path, monkeypatch) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    peer = AflowDaemon(daemon._config, units=units)
    peer.start()
    question = StartupQuestion(
        kind=StartupQuestionKind.PICK_STEP,
        message="Choose a step",
        choices=["implement"],
    )
    monkeypatch.setattr("aflow.daemon.prepare_startup", lambda value: question)
    preparation_entered = Event()
    release_preparation = Event()

    def prepare_with_answer(
        question: StartupQuestion,
        request: StartupRequest,
        answer: str | int | bool,
    ) -> PreparedRun:
        if preparation_entered.is_set():
            raise AssertionError("a second service read the stale startup question")
        preparation_entered.set()
        assert release_preparation.wait(timeout=2)
        return _prepared(request)

    monkeypatch.setattr(
        "aflow.daemon.prepare_startup_with_answer",
        prepare_with_answer,
    )
    pending = daemon.service.start(request, caller_scope="project:one", idempotency_key="start-1")
    peer_entered_record_lock = Event()
    peer_lock = peer.service._startup_record_lock

    @contextmanager
    def observe_peer_lock(run_id: str):
        peer_entered_record_lock.set()
        with peer_lock(run_id):
            yield

    monkeypatch.setattr(peer.service, "_startup_record_lock", observe_peer_lock)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            daemon.service.answer_startup,
            pending.question_id,
            "implement",
            caller_scope="project:one",
            idempotency_key="answer-1",
        )
        try:
            assert preparation_entered.wait(timeout=2)
            second = pool.submit(
                peer.service.answer_startup,
                pending.question_id,
                "implement",
                caller_scope="project:one",
                idempotency_key="answer-1",
            )
            assert peer_entered_record_lock.wait(timeout=2)
            assert units.start_calls == []
        finally:
            release_preparation.set()
        first_result = first.result(timeout=2)
        second_result = second.result(timeout=2)

    record = daemon.service._read_record(pending.run_id)
    assert first_result.status == second_result.status == "running"
    assert record["state"] == "unit_started"
    assert len(record["answered_questions"]) == 1
    assert len(units.start_calls) == 1


def test_daemon_serializes_answer_launch_with_idempotent_start_replay(tmp_path: Path, monkeypatch) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    peer = AflowDaemon(daemon._config, units=units)
    peer.start()
    question = StartupQuestion(
        kind=StartupQuestionKind.PICK_STEP,
        message="Choose a step",
        choices=["implement"],
    )
    monkeypatch.setattr("aflow.daemon.prepare_startup", lambda value: question)
    monkeypatch.setattr(
        "aflow.daemon.prepare_startup_with_answer",
        lambda question, request, answer: _prepared(request),
    )
    pending = daemon.service.start(request, caller_scope="project:one", idempotency_key="start-1")
    answer_ready_to_launch = Event()
    release_answer_launch = Event()
    launch_prepared = daemon.service._launch_prepared_locked

    def pause_answer_launch(record, prepared, *, created):
        answer_ready_to_launch.set()
        assert release_answer_launch.wait(timeout=2)
        return launch_prepared(record, prepared, created=created)

    monkeypatch.setattr(daemon.service, "_launch_prepared_locked", pause_answer_launch)
    peer_entered_record_lock = Event()
    peer_lock = peer.service._startup_record_lock

    @contextmanager
    def observe_peer_lock(run_id: str):
        peer_entered_record_lock.set()
        with peer_lock(run_id):
            yield

    monkeypatch.setattr(peer.service, "_startup_record_lock", observe_peer_lock)

    with ThreadPoolExecutor(max_workers=2) as pool:
        answer = pool.submit(
            daemon.service.answer_startup,
            pending.question_id,
            "implement",
            caller_scope="project:one",
            idempotency_key="answer-1",
        )
        try:
            assert answer_ready_to_launch.wait(timeout=2)
            replay = pool.submit(
                peer.service.start,
                request,
                caller_scope="project:one",
                idempotency_key="start-1",
            )
            assert peer_entered_record_lock.wait(timeout=2)
            assert units.start_calls == []
        finally:
            release_answer_launch.set()
        answer_result = answer.result(timeout=2)
        replay_result = replay.result(timeout=2)

    events = read_events(tmp_path / "repo" / ".aflow" / "runs" / pending.run_id)
    assert answer_result.run_id == replay_result.run_id == pending.run_id
    assert answer_result.status == replay_result.status == "running"
    assert len(units.start_calls) == 1
    assert [event.event_type for event in events].count("daemon_start_attempt") == 1


def test_daemon_start_rejects_symlinked_aflow_before_creating_record_paths(
    tmp_path: Path, monkeypatch
) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    repo_root = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo_root / ".aflow").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DaemonError, match="daemon lock directory is unsafe"):
        daemon.service.start(request, caller_scope="project:one", idempotency_key="start-1")

    assert list(outside.iterdir()) == []


def test_daemon_recovers_only_the_manifest_only_gap_for_the_same_request(
    tmp_path: Path, monkeypatch
) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    monkeypatch.setattr("aflow.daemon.prepare_startup", _prepared)
    manifest = daemon.service._initial_manifest_for(
        run_id="manifest-gap",
        request=request,
        caller_scope="project:one",
        idempotency_key="start-1",
    )
    create_launch_manifest(tmp_path / "repo", manifest)

    recovered = daemon.service.start(request, caller_scope="project:one", idempotency_key="start-1")

    assert recovered.run_id == "manifest-gap"
    assert recovered.created is False
    assert len(units.start_calls) == 1
    record = tmp_path / "repo" / ".aflow" / "start-requests" / "manifest-gap.json"
    assert '"state":"unit_started"' in record.read_text()


def test_daemon_uses_release_realpath_captured_before_current_symlink_changes(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    plan_path = repo_root / "plan.md"
    plan_path.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n")
    config_path = repo_root / "aflow.toml"
    config_path.write_text("")
    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n")
    release_a = repo_root / "releases" / "a"
    release_b = repo_root / "releases" / "b"
    for release in (release_a, release_b):
        executable = release / "bin" / "aflow"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    current = repo_root / "current"
    current.symlink_to(release_a, target_is_directory=True)
    workflow_config = _workflow_config()
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda path: workflow_config)
    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=current / "bin" / "aflow",
            environment_file=environment_file,
            release_identity="release-a",
        ),
        units=units,
    )
    current.unlink()
    current.symlink_to(release_b, target_is_directory=True)
    daemon.start()
    monkeypatch.setattr("aflow.daemon.prepare_startup", _prepared)
    request = StartupRequest(
        repo_root=repo_root,
        plan_path=plan_path,
        config_path=config_path,
        workflow_config=workflow_config,
        workflow_name="managed",
        start_step=None,
        max_turns=2,
        team=None,
    )

    started = daemon.service.start(request)

    assert units.start_calls[0][1][0] == str((release_a / "bin" / "aflow").resolve())
    attempt = next(event for event in read_events(repo_root / ".aflow" / "runs" / started.run_id) if event.event_type == "daemon_start_attempt")
    assert attempt.data["executable"] == str((release_a / "bin" / "aflow").resolve())


def test_daemon_owner_stop_persists_terminal_phase_and_requires_event_authorization(
    tmp_path: Path, monkeypatch
) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    monkeypatch.setattr("aflow.daemon.prepare_startup", _prepared)
    started = daemon.service.start(request)

    stopped = daemon.service.owner_stop(started.run_id, expected_revision=0)

    assert stopped.status == "owner_stopped"
    assert units.stop_calls == [f"aflow-run-{started.run_id}.service"]
    assert daemon.service.poll_events(started.run_id, authorizer=lambda action, status: True)


def test_daemon_typed_start_persists_canonical_step_and_redacted_instruction_digest(
    tmp_path: Path, monkeypatch
) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    workflow_config = _two_step_workflow_config()
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda path: workflow_config)
    sentinel = "private-runtime-guidance-4f91"
    request = replace(
        request,
        workflow_config=workflow_config,
        start_step="2",
        extra_instructions=(sentinel,),
    )
    monkeypatch.setattr("aflow.daemon.prepare_startup", _prepared_for_request)

    started = daemon.service.start(
        request,
        caller_scope="project:one",
        idempotency_key="typed-start",
    )

    manifest = daemon.application.repository.get_launch_manifest(started.run_id)
    assert manifest is not None
    assert manifest.start_step == "review"
    assert manifest.skipped_steps == ("implement",)
    status = daemon.service.run_status(started.run_id)
    assert status.selected_start_step == "review"
    assert status.skipped_steps == ("implement",)
    argv = units.start_calls[-1][1]
    assert argv[-1] == f"--extra-instruction={sentinel}"
    launch = request.repo_root / ".aflow" / "launches" / f"{started.run_id}.json"
    record = request.repo_root / ".aflow" / "start-requests" / f"{started.run_id}.json"
    events = request.repo_root / ".aflow" / "runs" / started.run_id / "events.jsonl"
    assert sentinel not in launch.read_text()
    assert sentinel not in record.read_text()
    assert sentinel not in events.read_text()


def test_daemon_exact_retry_restores_transient_instructions_after_prepared_crash(
    tmp_path: Path, monkeypatch
) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    sentinel = "private-retry-guidance-7c31"
    request = replace(request, extra_instructions=(sentinel,))
    monkeypatch.setattr("aflow.daemon.prepare_startup", _prepared_for_request)

    def crash_after_prepare(record, _prepared, *, created):
        assert created is True
        assert record["state"] == "prepared"
        raise RuntimeError("synthetic pre-unit crash")

    monkeypatch.setattr(
        daemon.service,
        "_launch_prepared_locked",
        crash_after_prepare,
    )
    with pytest.raises(RuntimeError, match="synthetic pre-unit crash"):
        daemon.service.start(
            request,
            caller_scope="project:one",
            idempotency_key="prepared-retry",
        )

    records = list(
        (request.repo_root / ".aflow" / "start-requests").glob("*.json")
    )
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["state"] == "prepared"
    run_id = record["run_id"]
    assert sentinel not in records[0].read_text()
    launch_path = (
        request.repo_root / ".aflow" / "launches" / f"{run_id}.json"
    )
    assert sentinel not in launch_path.read_text()

    fresh = AflowDaemon(daemon.service._config, units=units)
    fresh.start()
    with pytest.raises(DaemonIdempotencyConflict):
        fresh.service.start(
            replace(request, extra_instructions=("different guidance",)),
            caller_scope="project:one",
            idempotency_key="prepared-retry",
        )

    retried = fresh.service.start(
        request,
        caller_scope="project:one",
        idempotency_key="prepared-retry",
    )

    assert retried.run_id == run_id
    assert retried.created is False
    assert retried.status == "running"
    assert units.start_calls[-1][1][-1] == f"--extra-instruction={sentinel}"
    events_path = (
        request.repo_root / ".aflow" / "runs" / run_id / "events.jsonl"
    )
    assert sentinel not in records[0].read_text()
    assert sentinel not in launch_path.read_text()
    assert sentinel not in events_path.read_text()


def test_daemon_rejects_excluded_step_before_reserving_run(
    tmp_path: Path, monkeypatch
) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    workflow_config = _two_step_workflow_config()
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda path: workflow_config)
    request = replace(
        request,
        workflow_config=workflow_config,
        start_step="draft",
    )

    with pytest.raises(DaemonError, match="excluded"):
        daemon.service.start(
            request,
            caller_scope="project:one",
            idempotency_key="excluded-start",
        )

    assert units.start_calls == []
    launches = request.repo_root / ".aflow" / "launches"
    assert not launches.exists() or list(launches.glob("*.json")) == []


@pytest.mark.parametrize("failure_kind", ["preparation", "execution"])
def test_failed_restart_preserves_plan_and_source_failure(tmp_path, monkeypatch, failure_kind):
    from aflow.api.startup import StartupError
    from aflow.daemon import DaemonStartupError, DaemonAuthorizationError

    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    before_plan = request.plan_path.read_bytes()
    if failure_kind == "preparation":
        def reject(*args, **kwargs):
            raise StartupError("Confirmed fixture preparation failure")
        monkeypatch.setattr("aflow.daemon.prepare_startup", reject)
        with pytest.raises(DaemonStartupError) as caught:
            daemon.service.start(request, caller_scope="project:one", idempotency_key="failed-source")
        source_id = caught.value.run_id
    else:
        monkeypatch.setattr("aflow.daemon.prepare_startup", _prepared_for_request)
        source = daemon.service.start(request, caller_scope="project:one", idempotency_key="failed-source")
        source_id = source.run_id
        units.stop(f"aflow-run-{source_id}.service")
        directory = request.repo_root / ".aflow" / "runs" / source_id
        directory.mkdir(exist_ok=True)
        (directory / "run.json").write_text(json.dumps({"status": "failed", "failure_reason": "fixture runtime failure"}))
        write_launch_phase(request.repo_root, source_id, "failed")
    source_before = daemon.service.run_status(source_id)
    projection = daemon.service.restart_options(source_id, caller_scope="project:one")
    assert projection["eligible"] is True
    assert projection["options"]["workflow_name"] == "managed"
    assert projection["options"]["plan_path"] == "plan.md"
    assert request.plan_path.read_bytes() == before_plan
    with pytest.raises(DaemonAuthorizationError):
        daemon.service.restart_options(source_id, caller_scope="foreign")
    monkeypatch.setattr("aflow.daemon.prepare_startup", _prepared_for_request)
    successor = daemon.service.start(replace(request, restarted_from_run_id=source_id), caller_scope="project:one", idempotency_key="successor")
    assert daemon.service.restart_options(source_id, caller_scope="project:one")["eligible"] is False
    with pytest.raises(DaemonError, match="active successor"):
        daemon.service.start(replace(request, restarted_from_run_id=source_id), caller_scope="project:one", idempotency_key="duplicate-successor")
    replay = daemon.service.start(replace(request, restarted_from_run_id=source_id), caller_scope="project:one", idempotency_key="successor")
    assert successor.run_id == replay.run_id
    assert successor.run_id != source_id
    after = daemon.service.run_status(source_id)
    assert after.status == source_before.status
    assert after.reason == source_before.reason
    assert not any(event.event_type == "owner_stopped" for event in read_events(request.repo_root / ".aflow" / "runs" / source_id))
    assert request.plan_path.read_bytes() == before_plan


def test_daemon_restart_successor_requires_owner_stopped_inactive_source_and_keeps_lineage(
    tmp_path: Path, monkeypatch
) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    monkeypatch.setattr("aflow.daemon.prepare_startup", _prepared_for_request)
    source = daemon.service.start(
        request,
        caller_scope="project:one",
        idempotency_key="source-start",
    )
    successor_request = replace(
        request,
        restarted_from_run_id=source.run_id,
        max_turns=4,
    )

    with pytest.raises(DaemonError, match="explicit owner stop"):
        daemon.service.start(
            successor_request,
            caller_scope="project:one",
            idempotency_key="active-successor",
        )

    daemon.service.owner_stop(
        source.run_id,
        expected_revision=0,
        caller_scope="project:one",
        idempotency_key="source-stop",
    )
    original_start = units.start

    def fail_start(*_args, **_kwargs):
        raise RuntimeError("synthetic unit failure")

    monkeypatch.setattr(units, "start", fail_start)
    with pytest.raises(DaemonError, match="workflow unit failed to start"):
        daemon.service.start(
            successor_request,
            caller_scope="project:one",
            idempotency_key="failed-successor",
        )
    assert daemon.service.run_status(source.run_id).status == "owner_stopped"
    monkeypatch.setattr(units, "start", original_start)

    successor = daemon.service.start(
        successor_request,
        caller_scope="project:one",
        idempotency_key="stopped-successor",
    )

    replay = daemon.service.start(
        successor_request,
        caller_scope="project:one",
        idempotency_key="stopped-successor",
    )
    assert replay.run_id == successor.run_id
    assert replay.restarted_from_run_id == source.run_id
    assert successor.run_id != source.run_id
    assert successor.restarted_from_run_id == source.run_id
    manifest = daemon.application.repository.get_launch_manifest(successor.run_id)
    assert manifest is not None
    assert manifest.restarted_from_run_id == source.run_id
    status = daemon.service.run_status(successor.run_id)
    assert status.restarted_from_run_id == source.run_id
    source_events = daemon.service.poll_events(
        source.run_id,
        authorizer=lambda _action, _status: True,
    )
    assert any(
        event.event_type == "restart_successor_requested"
        and event.data["successor_run_id"] == successor.run_id
        for event in source_events
    )


def test_startup_answer_selected_later_step_is_reported_as_skipped_without_manifest_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    from aflow.control_plane import LaunchManifest
    from aflow.workflow import _daemon_manifest_matches_execution

    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    workflow_config = _two_step_workflow_config()
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda path: workflow_config)
    request = replace(request, workflow_config=workflow_config)
    monkeypatch.setattr(
        "aflow.daemon.prepare_startup",
        lambda _request: StartupQuestion(
            kind=StartupQuestionKind.PICK_STEP,
            message="Choose",
            choices=["implement", "review"],
        ),
    )
    monkeypatch.setattr(
        "aflow.daemon.prepare_startup_with_answer",
        lambda _question, value, _answer: replace(
            _prepared_for_request(value),
            start_step="review",
        ),
    )

    pending = daemon.service.start(
        request,
        caller_scope="project:one",
        idempotency_key="question-step",
    )
    started = daemon.service.answer_startup(
        pending.question_id,
        "review",
        caller_scope="project:one",
        idempotency_key="question-step-answer",
    )

    status = daemon.service.run_status(started.run_id)
    assert status.selected_start_step == "review"
    assert status.skipped_steps == ("implement",)
    manifest = daemon.application.repository.get_launch_manifest(started.run_id)
    assert manifest is not None
    assert manifest.start_step is None
    expected = LaunchManifest(
        **{
            **manifest.__dict__,
            "start_step": "review",
            "skipped_steps": ("implement",),
        }
    )
    assert _daemon_manifest_matches_execution(manifest, expected)


def test_daemon_restart_successor_rejects_self_and_cyclic_lineage(
    tmp_path: Path, monkeypatch
) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    with pytest.raises(DaemonError, match="cannot restart itself"):
        daemon.service._validate_restart_source(
            "same-run",
            successor_run_id="same-run",
            caller_scope="project:one",
        )

    for run_id, predecessor in (("cycle-a", "cycle-b"), ("cycle-b", "cycle-a")):
        create_launch_manifest(
            request.repo_root,
            LaunchManifest(
                run_id=run_id,
                project_root=str(request.repo_root.resolve()),
                plan_path=str(request.plan_path.resolve()),
                workflow_name="managed",
                max_turns=2,
                caller_scope="project:one",
                restarted_from_run_id=predecessor,
            ),
        )
    write_launch_phase(request.repo_root, "cycle-a", "owner_stopped")
    append_run_event(
        request.repo_root / ".aflow" / "runs" / "cycle-a",
        "owner_stopped",
        {"source": "daemon"},
    )

    with pytest.raises(DaemonError, match="cyclic"):
        daemon.service._validate_restart_source(
            "cycle-a",
            successor_run_id="fresh-run",
            caller_scope="project:one",
        )


def test_daemon_owner_stop_manifest_only_requires_exact_owner_and_revision(
    tmp_path: Path, monkeypatch
) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    run_id = "manifest-only-stop"
    manifest = daemon.service._initial_manifest_for(
        run_id=run_id,
        request=request,
        caller_scope="project:one",
        idempotency_key="start-1",
    )
    create_launch_manifest(request.repo_root, manifest)
    manifest_path = request.repo_root / ".aflow" / "launches" / f"{run_id}.json"
    run_dir = request.repo_root / ".aflow" / "runs" / run_id
    plan_bytes = request.plan_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()

    with pytest.raises(ControlConflictError) as conflict:
        daemon.service.owner_stop(
            run_id,
            expected_revision=1,
            caller_scope="project:one",
            idempotency_key="stale-stop",
        )
    assert conflict.value.current_revision == 0
    assert not run_dir.exists()

    with pytest.raises(PermissionError):
        daemon.service.owner_stop(
            run_id,
            expected_revision=0,
            caller_scope="project:other",
            idempotency_key="unauthorized-stop",
        )
    assert not run_dir.exists()

    unrelated_run_dir = request.repo_root / ".aflow" / "runs" / "other-run"
    unrelated_run_dir.mkdir()
    marker = unrelated_run_dir / "keep.txt"
    marker.write_text("keep me\n")
    run_dir.symlink_to(unrelated_run_dir, target_is_directory=True)
    with pytest.raises(DaemonError, match="run artifact path is unsafe"):
        daemon.service.owner_stop(
            run_id,
            expected_revision=0,
            caller_scope="project:one",
            idempotency_key="symlink-stop",
        )
    assert marker.read_text() == "keep me\n"
    assert not (unrelated_run_dir / "overrides.toml").exists()
    run_dir.unlink()
    marker.unlink()
    unrelated_run_dir.rmdir()

    missing_run_dir = request.repo_root / ".aflow" / "runs" / "missing-manifest"
    with pytest.raises(RepositoryNotFoundError):
        daemon.service.owner_stop(
            "missing-manifest",
            expected_revision=0,
            caller_scope="project:one",
            idempotency_key="missing-stop",
        )
    assert not missing_run_dir.exists()

    stopped = daemon.service.owner_stop(
        run_id,
        expected_revision=0,
        caller_scope="project:one",
        idempotency_key="stop-1",
    )
    replayed_stop = daemon.service.owner_stop(
        run_id,
        expected_revision=0,
        caller_scope="project:one",
        idempotency_key="stop-1",
    )
    replayed_start = daemon.service.start(
        request,
        caller_scope="project:one",
        idempotency_key="start-1",
    )

    assert stopped.status == "owner_stopped"
    assert stopped.revision == 1
    assert replayed_stop.status == "owner_stopped"
    assert replayed_stop.revision == 1
    assert replayed_start.status == "owner_stopped"
    assert run_dir.is_dir()
    assert (run_dir / "overrides.toml").is_file()
    assert not (run_dir / "run.json").exists()
    assert not (
        request.repo_root / ".aflow" / "start-requests" / f"{run_id}.json"
    ).exists()
    assert units.start_calls == []
    assert units.stop_calls == []
    assert request.plan_path.read_bytes() == plan_bytes
    assert manifest_path.read_bytes() == manifest_bytes


def test_daemon_owner_stop_pending_question_remains_terminal_for_reads_and_answers(
    tmp_path: Path, monkeypatch
) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    question = StartupQuestion(
        kind=StartupQuestionKind.PICK_STEP,
        message="Choose a step",
        choices=["implement"],
    )
    monkeypatch.setattr("aflow.daemon.prepare_startup", lambda value: question)
    pending = daemon.service.start(
        request,
        caller_scope="project:one",
        idempotency_key="start-1",
    )
    assert pending.run_id is not None
    assert pending.question_id is not None
    run_dir = request.repo_root / ".aflow" / "runs" / pending.run_id
    record_path = (
        request.repo_root / ".aflow" / "start-requests" / f"{pending.run_id}.json"
    )
    manifest_path = (
        request.repo_root / ".aflow" / "launches" / f"{pending.run_id}.json"
    )
    record_bytes = record_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()
    plan_bytes = request.plan_path.read_bytes()
    # The reservation creates the diagnostic compatibility directory, so the
    # run directory exists; no controller artifacts may exist before the answer.
    assert sorted(path.name for path in run_dir.iterdir()) == ["config"]
    assert (run_dir / "config" / "aflow.toml").is_file()

    stopped = daemon.service.owner_stop(
        pending.run_id,
        expected_revision=0,
        caller_scope="project:one",
        idempotency_key="stop-1",
    )
    monkeypatch.setattr(
        "aflow.daemon.prepare_startup_with_answer",
        lambda *args: pytest.fail("a stopped startup question must not be answered"),
    )
    status = daemon.service.run_status(pending.run_id)
    replayed_start = daemon.service.start(
        request,
        caller_scope="project:one",
        idempotency_key="start-1",
    )
    replayed_answer = daemon.service.answer_startup(
        pending.question_id,
        "implement",
        caller_scope="project:one",
        idempotency_key="answer-1",
    )

    assert stopped.status == "owner_stopped"
    assert status.status == "owner_stopped"
    assert replayed_start.status == "owner_stopped"
    assert replayed_answer.status == "owner_stopped"
    assert record_path.read_bytes() == record_bytes
    assert manifest_path.read_bytes() == manifest_bytes
    assert request.plan_path.read_bytes() == plan_bytes
    assert not (run_dir / "run.json").exists()
    assert units.start_calls == []
    assert units.stop_calls == []


@pytest.mark.parametrize("stage", ["preparation", "unit_launch"])
def test_startup_failure_survives_fresh_daemon_redacted_and_bounded(tmp_path, monkeypatch, stage):
    from aflow.api.startup import StartupError
    from aflow.daemon import DaemonStartupError

    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    message = "blocked token=private-value password=another-value Bearer bearer-value secret=\'multi word credential\' " + "x" * 6000

    def reject(*args, **kwargs):
        raise StartupError(message)

    monkeypatch.setattr("aflow.daemon.prepare_startup", reject if stage == "preparation" else _prepared)
    if stage == "unit_launch":
        monkeypatch.setattr(units, "start", reject)
    with pytest.raises(DaemonStartupError) as caught:
        daemon.service.start(request, caller_scope="project:one", idempotency_key="failure")
    run_id = caught.value.run_id
    payload = json.loads((request.repo_root / ".aflow" / "start-requests" / f"{run_id}.json").read_text())
    failure = payload["startup_failure"]
    assert failure["stage"] == stage
    assert len(failure["message"]) < 4200
    assert failure["timestamp"]
    assert "private-value" not in failure["message"]
    assert "another-value" not in failure["message"]
    assert "bearer-value" not in failure["message"]
    assert "multi word credential" not in failure["message"]
    fresh = AflowDaemon(daemon._config, units=units)
    fresh.start()
    status = fresh.service.run_status(run_id)
    assert status.status == "needs_attention"
    assert status.reason == failure["message"]
    assert status.plan_path == str(request.plan_path)
    assert status.started_at is None
    assert status.evidence["no_agent_started"] == (stage == "preparation")
