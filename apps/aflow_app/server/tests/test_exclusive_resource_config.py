"""Checkpoint 1: exclusive flag through raw/guided configuration editing."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from aflow_app_server.global_config_service import GlobalConfigService
from aflow_app_server.guided_config import guided_form_response
from aflow_app_server.models import (
    GuidedProfileSummary,
    GlobalConfigPatchPayload,
    ProjectConfigFormResponse,
    UpsertProfileAction,
)
from aflow_app_server.project_config_service import ProjectConfigError

AFLOW = '''# preserve this comment
[aflow]
default_workflow = "demo"
[harness.codex.profiles.worker]
model = "test"
effort = "high"
[roles]
worker = "codex.worker"
[prompts]
work = "Do {task}."
'''

WORKFLOWS = '''[workflow]
merge_prompt = ["work"]
[workflow.demo]
merge_prompt = ["work", "work"]
[workflow.demo.steps.implement]
role = "worker"
prompts = ["work"]
go = [{to="END", when="DONE"}]
'''


@pytest.fixture
def service(tmp_path: Path):
    (tmp_path / "aflow.toml").write_text(AFLOW)
    (tmp_path / "workflows.toml").write_text(WORKFLOWS)
    return GlobalConfigService(config_dir=tmp_path, audit_path=tmp_path / "audit.jsonl")


def patch(service, **fields):
    return service.patch(GlobalConfigPatchPayload(expected_revision=service.read().revision, **fields))


def _form(aflow_text: str = AFLOW) -> ProjectConfigFormResponse:
    result = guided_form_response(aflow_text, WORKFLOWS)
    return ProjectConfigFormResponse.model_validate(result)


def test_guided_profile_summary_defaults_exclusive_false() -> None:
    assert GuidedProfileSummary().exclusive is False


def test_form_projection_reports_declared_exclusive() -> None:
    marked = _form(AFLOW.replace('effort = "high"\n', 'effort = "high"\nexclusive = true\n'))
    assert marked.form is not None
    assert marked.form.harnesses["codex"]["worker"].exclusive is True
    unmarked = _form()
    assert unmarked.form is not None
    assert unmarked.form.harnesses["codex"]["worker"].exclusive is False


def test_form_projection_ignores_coerced_exclusive() -> None:
    marked = _form(AFLOW.replace('effort = "high"\n', 'effort = "high"\nexclusive = "true"\n'))
    assert marked.form is not None
    assert marked.form.harnesses["codex"]["worker"].exclusive is False


def test_guided_exclusive_only_true_writes_flag_and_preserves_fields(service) -> None:
    result = patch(service, actions=[
        dict(type="upsert_profile", harness="codex", profile="worker", exclusive=True),
    ])
    assert "exclusive = true" in result.aflow_toml
    assert 'model = "test"' in result.aflow_toml
    assert 'effort = "high"' in result.aflow_toml
    assert "# preserve this comment" in result.aflow_toml
    assert result.workflows_toml == WORKFLOWS


def test_guided_exclusive_only_false_persists_false(service) -> None:
    marked = patch(service, actions=[
        dict(type="upsert_profile", harness="codex", profile="worker", exclusive=True),
    ])
    unmarked = service.patch(GlobalConfigPatchPayload(
        expected_revision=marked.revision,
        actions=[dict(type="upsert_profile", harness="codex", profile="worker", exclusive=False)],
    ))
    assert "exclusive = false" in unmarked.aflow_toml


def test_guided_exclusive_null_removes_key(service) -> None:
    marked = patch(service, actions=[
        dict(type="upsert_profile", harness="codex", profile="worker", exclusive=True),
    ])
    unmarked = service.patch(GlobalConfigPatchPayload(
        expected_revision=marked.revision,
        actions=[dict(type="upsert_profile", harness="codex", profile="worker", exclusive=None)],
    ))
    assert "exclusive" not in unmarked.aflow_toml
    assert 'model = "test"' in unmarked.aflow_toml


def test_model_effort_only_action_preserves_exclusive_flag(service) -> None:
    marked = patch(service, actions=[
        dict(type="upsert_profile", harness="codex", profile="worker", exclusive=True),
    ])
    edited = service.patch(GlobalConfigPatchPayload(
        expected_revision=marked.revision,
        actions=[dict(type="upsert_profile", harness="codex", profile="worker", effort="medium")],
    ))
    assert "exclusive = true" in edited.aflow_toml
    assert 'effort = "medium"' in edited.aflow_toml
    assert 'model = "test"' in edited.aflow_toml


@pytest.mark.parametrize("value", [1, "true", 0])
def test_upsert_action_rejects_coerced_exclusive(value) -> None:
    with pytest.raises(ValidationError):
        UpsertProfileAction(
            type="upsert_profile", harness="codex", profile="worker", exclusive=value  # type: ignore[arg-type]
        )


def test_zcode_profile_accepts_exclusive_flag(service) -> None:
    result = patch(service, actions=[
        dict(type="upsert_profile", harness="zcode", profile="worker", exclusive=True),
    ])
    assert "exclusive = true" in result.aflow_toml


def test_raw_save_accepts_exclusive_flag(service) -> None:
    text = AFLOW.replace('effort = "high"\n', 'effort = "high"\nexclusive = true\n')
    result = service.save(text, WORKFLOWS, service.read().revision)
    assert "exclusive = true" in result.aflow_toml
    assert (service.config_dir / "aflow.toml").read_text(encoding="utf-8") == text


def test_raw_rejected_save_leaves_bytes_and_revision_unchanged(service) -> None:
    original = service.read()
    bad = AFLOW.replace('effort = "high"\n', 'effort = "high"\nexclusive = "true"\n')
    with pytest.raises(ProjectConfigError):
        service.save(bad, WORKFLOWS, original.revision)
    after = service.read()
    assert after.aflow_toml == original.aflow_toml
    assert after.workflows_toml == original.workflows_toml
    assert after.revision == original.revision
