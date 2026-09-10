from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from aflow.api.models import StartupQuestion, StartupQuestionKind, StartupRequest
from aflow.config import (
    ConfigError,
    HarnessProfileConfig,
    TeamConfig,
    WorkflowConfig,
    WorkflowHarnessConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
)
from aflow.control_plane import (
    ContextService,
    ControlIdempotencyConflict,
    ControlService,
    ControlValidationError,
    LaunchManifest,
    RunControlRequest,
    RunRepository,
    read_events,
    ServiceAuthorizationError,
    StartupQuestionService,
    create_launch_manifest,
)
from aflow.run_state import load_override_request


def _owned_run(root: Path, run_id: str = "owned-run") -> None:
    create_launch_manifest(
        root,
        LaunchManifest(
            run_id=run_id,
            project_root="/project",
            plan_path="/project/plan.md",
            workflow_name="managed",
            max_turns=5,
            idempotency_key="request-1",
            caller_scope="caller:project",
        ),
    )
    run_dir = root / ".aflow" / "runs" / run_id
    run_dir.mkdir()
    (run_dir / "run.json").write_text('{"status":"running","token":"private"}\n')


def _control_config(*, include_base: bool = True) -> WorkflowUserConfig:
    teams = {
        "new": TeamConfig(roles={"worker": "reasonix.new"}),
    }
    if include_base:
        teams["base"] = TeamConfig(roles={"worker": "codex.test"})
    return WorkflowUserConfig(
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={"test": HarnessProfileConfig(model="test-model")}
            ),
            "reasonix": WorkflowHarnessConfig(
                profiles={"new": HarnessProfileConfig(model="new-model")}
            ),
        },
        roles={"worker": "codex.test"},
        teams=teams,
        workflows={
            "managed": WorkflowConfig(
                steps={"implement": WorkflowStepConfig(role="worker")},
                first_step="implement",
                team="base" if include_base else "new",
            )
        },
    )


def test_control_service_preserves_cas_and_deduplicates_idempotent_replay(tmp_path: Path) -> None:
    _owned_run(tmp_path)
    repository = RunRepository(tmp_path)
    service = ControlService(repository)
    request = RunControlRequest(expected_revision=0, max_turns=7)

    assert repository.get_run_status("owned-run").revision == 0
    first = service.apply("owned-run", request, caller_scope="actor", idempotency_key="key-1")
    replay = service.apply("owned-run", request, caller_scope="actor", idempotency_key="key-1")

    assert (first.revision, first.changed) == (1, True)
    assert replay == first
    assert repository.get_run_status("owned-run").revision == 1
    with pytest.raises(ControlIdempotencyConflict):
        service.apply(
            "owned-run",
            RunControlRequest(expected_revision=1, max_turns=8),
            caller_scope="actor",
            idempotency_key="key-1",
        )
    assert "max_turns = 7" in (tmp_path / ".aflow" / "runs" / "owned-run" / "overrides.toml").read_text()


def test_control_service_accepts_current_team_and_profile_replacements(tmp_path: Path) -> None:
    _owned_run(tmp_path)
    repository = RunRepository(tmp_path)
    service = ControlService(
        repository,
        config_loader=lambda: _control_config(),
    )

    result = service.apply(
        "owned-run",
        RunControlRequest(
            expected_revision=0,
            team="new",
            role_selectors={"worker": "reasonix.new"},
        ),
        caller_scope="actor",
        idempotency_key="new-target",
    )

    assert result.revision == 1
    loaded = load_override_request(
        tmp_path / ".aflow" / "runs" / "owned-run" / "overrides.toml"
    )
    assert loaded.request is not None
    assert loaded.request.team == "new"
    assert loaded.request.role_selectors == {"worker": "reasonix.new"}


def test_control_service_rejects_invalid_effective_targets_without_writing(
    tmp_path: Path,
) -> None:
    _owned_run(tmp_path)
    repository = RunRepository(tmp_path)
    service = ControlService(repository, config_loader=lambda: _control_config())
    service.apply(
        "owned-run",
        RunControlRequest(expected_revision=0, team="new", role_selectors={"worker": "reasonix.new"}),
        caller_scope="actor",
        idempotency_key="valid-target",
    )
    path = tmp_path / ".aflow" / "runs" / "owned-run" / "overrides.toml"
    original_bytes = path.read_bytes()
    original_events = read_events(path.parent)

    with pytest.raises(ControlValidationError) as error:
        service.apply(
            "owned-run",
            RunControlRequest(expected_revision=1, team="missing"),
            caller_scope="actor",
            idempotency_key="invalid-target",
        )

    assert error.value.field == "team"
    assert error.value.target == "missing"
    assert path.read_bytes() == original_bytes
    assert repository.get_run_status("owned-run").revision == 1
    assert read_events(path.parent) == original_events


def test_control_service_replacements_are_resolved_before_old_choices(
    tmp_path: Path,
) -> None:
    _owned_run(tmp_path)
    repository = RunRepository(tmp_path)
    current = [_control_config()]
    service = ControlService(repository, config_loader=lambda: current[0])
    service.apply(
        "owned-run",
        RunControlRequest(expected_revision=0, team="base", role_selectors={"worker": "codex.test"}),
        caller_scope="actor",
        idempotency_key="old-target",
    )
    current[0] = _control_config(include_base=False)

    result = service.apply(
        "owned-run",
        RunControlRequest(expected_revision=1, team="new", role_selectors={"worker": "reasonix.new"}),
        caller_scope="actor",
        idempotency_key="replacement-target",
    )

    assert result.revision == 2


def test_control_service_admits_pending_next_step_before_removed_status_step(
    tmp_path: Path,
) -> None:
    _owned_run(tmp_path)
    run_dir = tmp_path / ".aflow" / "runs" / "owned-run"
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "status": "running",
                "workflow_name": "managed",
                "current_step_name": "removed_step",
                "turns_completed": 0,
                "max_turns": 5,
            }
        )
    )
    (run_dir / "overrides.toml").write_text(
        'revision = 1\nnext_step = "implement"\n'
    )
    repository = RunRepository(tmp_path)
    service = ControlService(repository, config_loader=lambda: _control_config())

    result = service.apply(
        "owned-run",
        RunControlRequest(expected_revision=1, team="new"),
        caller_scope="actor",
        idempotency_key="pending-step-valid",
    )

    assert result.revision == 2
    loaded = load_override_request(run_dir / "overrides.toml")
    assert loaded.request is not None
    assert loaded.request.next_step == "implement"
    assert loaded.request.team == "new"


def test_control_service_rejects_invalid_pending_next_step_without_writing(
    tmp_path: Path,
) -> None:
    _owned_run(tmp_path)
    run_dir = tmp_path / ".aflow" / "runs" / "owned-run"
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "status": "running",
                "workflow_name": "managed",
                "current_step_name": "implement",
                "turns_completed": 0,
                "max_turns": 5,
            }
        )
    )
    (run_dir / "overrides.toml").write_text(
        'revision = 1\nnext_step = "removed_step"\n'
    )
    repository = RunRepository(tmp_path)
    service = ControlService(repository, config_loader=lambda: _control_config())
    override_path = run_dir / "overrides.toml"
    original_bytes = override_path.read_bytes()
    original_events = read_events(run_dir)

    with pytest.raises(ControlValidationError) as error:
        service.apply(
            "owned-run",
            RunControlRequest(expected_revision=1, team="new"),
            caller_scope="actor",
            idempotency_key="pending-step-invalid",
        )

    assert error.value.field == "next_step"
    assert error.value.target == "removed_step"
    assert override_path.read_bytes() == original_bytes
    assert repository.get_run_status("owned-run").revision == 1
    assert read_events(run_dir) == original_events


def test_control_service_owner_stop_does_not_load_broken_live_config(
    tmp_path: Path,
) -> None:
    _owned_run(tmp_path)
    repository = RunRepository(tmp_path)

    def broken_config() -> WorkflowUserConfig:
        raise ConfigError("malformed live configuration")

    result = ControlService(repository, config_loader=broken_config).apply(
        "owned-run",
        RunControlRequest(expected_revision=0, owner_stop=True),
        caller_scope="actor",
        idempotency_key="stop-broken-config",
    )

    assert result.owner_stop is True


def test_control_and_context_services_apply_authorization_and_read_only_legacy_rules(tmp_path: Path) -> None:
    _owned_run(tmp_path)
    repository = RunRepository(tmp_path)
    denied = ControlService(repository, authorizer=lambda action, status: False)
    with pytest.raises(ServiceAuthorizationError):
        denied.apply("owned-run", RunControlRequest(expected_revision=0, max_turns=3))

    context = ContextService(repository).get("owned-run")
    assert context.level == "lite"
    assert context.to_dict()["data"]["run_metadata"]["token"] == "[redacted]"
    with pytest.raises(PermissionError, match="explicit"):
        ContextService(repository).get("owned-run", level="full")

    legacy = tmp_path / ".aflow" / "runs" / "legacy-run"
    legacy.mkdir(parents=True)
    (legacy / "run.json").write_text(json.dumps({"status": "running"}))
    with pytest.raises(ServiceAuthorizationError, match="read-only"):
        ControlService(repository).apply("legacy-run", RunControlRequest(expected_revision=0, max_turns=3))


def test_startup_questions_are_opaque_transient_service_records(monkeypatch: pytest.MonkeyPatch) -> None:
    question = StartupQuestion(
        kind=StartupQuestionKind.PICK_STEP,
        message="Choose a step",
        choices=["implement", "review"],
    )
    monkeypatch.setattr("aflow.control_plane.services.prepare_startup", lambda request: question)
    service = StartupQuestionService()

    record = service.prepare(cast(StartupRequest, object()))

    assert record.kind == "pick_step"
    assert record.choices == ("implement", "review")
    assert service.list_questions() == (record,)
