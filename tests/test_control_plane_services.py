from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from aflow.api.models import StartupQuestion, StartupQuestionKind, StartupRequest
from aflow.control_plane import (
    ContextService,
    ControlIdempotencyConflict,
    ControlService,
    LaunchManifest,
    RunControlRequest,
    RunRepository,
    ServiceAuthorizationError,
    StartupQuestionService,
    create_launch_manifest,
)


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
