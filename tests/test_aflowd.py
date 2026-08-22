from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import subprocess
from threading import Event

import pytest

from aflow.api.models import PreparedRun, StartupQuestion, StartupQuestionKind, StartupRequest
from aflow.config import GoTransition, WorkflowConfig, WorkflowStepConfig, WorkflowUserConfig
from aflow.control_plane import InMemoryUnitManager, create_launch_manifest, read_events
from aflow.daemon import (
    AflowDaemon,
    DaemonConfig,
    DaemonError,
    DaemonIdempotencyConflict,
    _startup_request_digest,
)


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


@pytest.mark.parametrize("invalid_state", ["started", "ambiguous", "no_head"])
def test_daemon_git_tracking_preflight_failure_does_not_allocate_run_artifacts(
    tmp_path: Path,
    monkeypatch,
    invalid_state: str,
) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon(tmp_path, monkeypatch, units)
    review_config = _review_workflow_config()
    daemon.service._workflow_config = review_config
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
    original_bytes = plan_path.read_bytes()

    with pytest.raises(DaemonError, match="startup plan preflight failed"):
        daemon.service.start(
            request,
            caller_scope="project:review",
            idempotency_key=f"invalid-{invalid_state}",
        )

    assert plan_path.read_bytes() == original_bytes
    assert units.start_calls == []
    assert not (request.repo_root / ".aflow" / "launches").exists()
    assert not (request.repo_root / ".aflow" / "runs").exists()
    assert not (request.repo_root / ".aflow" / "last_run_id").exists()


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
