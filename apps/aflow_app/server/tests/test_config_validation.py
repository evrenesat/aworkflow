"""Focused tests for the in-memory configuration candidate validator.

AFLOW-MAINT-20261006-03 checkpoint 1: submitted text is validated through
the core pure parser — one parse, one semantic pass, and zero candidate
files — producing reports equivalent to the filesystem path.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from aflow.config import ConfigError, load_workflow_config, parse_workflow_pair
from aflow_app_server.config_validation import (
    MAX_CONFIG_DOCUMENT_BYTES,
    ConfigValidationReport,
    ProjectConfigError,
    check_document_text,
    validate_candidate_pair,
)

AFLOW = """[aflow]
default_workflow = "simple"

[roles]
architect = "codex.default"

[harness.codex.profiles.default]
model = "model-a"

[prompts]
p = "Work."
"""

WORKFLOWS = """[workflow.simple]
[workflow.simple.steps.implement]
role = "architect"
prompts = ["p"]
go = [{ to = "END" }]
"""


def test_valid_pair_reports_ready_with_parsed_names() -> None:
    report = validate_candidate_pair(AFLOW, WORKFLOWS)
    assert isinstance(report, ConfigValidationReport)
    assert report.state == "ready"
    assert report.issues == ()
    assert report.placeholders == ()
    assert report.workflows == ("simple",)
    assert report.roles == ("architect",)
    assert report.teams == ()


def test_equivalent_text_and_filesystem_inputs_agree(tmp_path: Path) -> None:
    (tmp_path / "aflow.toml").write_text(AFLOW, encoding="utf-8")
    (tmp_path / "workflows.toml").write_text(WORKFLOWS, encoding="utf-8")
    loaded = load_workflow_config(tmp_path / "aflow.toml")
    parsed = parse_workflow_pair(AFLOW, WORKFLOWS, source_dir=tmp_path)
    assert parsed == loaded

    report = validate_candidate_pair(AFLOW, WORKFLOWS)
    assert report.state == "ready"
    assert report.workflows == tuple(sorted(loaded.workflows))
    assert report.roles == tuple(sorted(loaded.roles))
    assert report.placeholders == ()


def test_cross_document_missing_prompt_is_one_semantic_issue(
    tmp_path: Path,
) -> None:
    bad_workflows = WORKFLOWS.replace('prompts = ["p"]', 'prompts = ["missing"]')
    report = validate_candidate_pair(AFLOW, bad_workflows)
    assert report.state == "invalid"
    issue = report.issues[0]
    assert "references unknown prompt 'missing'" in issue.message
    assert issue.document is None
    assert issue.line is None

    # The filesystem loader rejects the same pair with the identical text.
    (tmp_path / "aflow.toml").write_text(AFLOW, encoding="utf-8")
    (tmp_path / "workflows.toml").write_text(bad_workflows, encoding="utf-8")
    with pytest.raises(ConfigError) as file_exc:
        load_workflow_config(tmp_path / "aflow.toml")
    assert issue.message == str(file_exc.value)


def test_syntax_line_error_is_named_bounded_and_path_free() -> None:
    report = validate_candidate_pair("a = 1\nb = \n", WORKFLOWS)
    assert report.state == "invalid"
    issue = report.issues[0]
    assert issue.document == "aflow.toml"
    assert issue.line == 2
    assert "line 2" in issue.message
    assert len(issue.message) <= 300 + len("[truncated]")
    assert "aflow-project-config" not in issue.message
    assert "/tmp" not in issue.message

    sibling_report = validate_candidate_pair(AFLOW, "a = 1\nb = \n")
    assert sibling_report.issues[0].document == "workflows.toml"
    assert sibling_report.issues[0].line == 2


def test_size_and_nul_bounds_reject_before_parse() -> None:
    oversized = "a = " + "x" * MAX_CONFIG_DOCUMENT_BYTES
    with pytest.raises(ProjectConfigError, match="exceeds the maximum supported size"):
        validate_candidate_pair(oversized, "")
    with pytest.raises(ProjectConfigError, match="must not contain NUL characters"):
        validate_candidate_pair("a = 1\x00\n", "")
    assert check_document_text("aflow.toml", "a = 1") == b"a = 1"
    with pytest.raises(ProjectConfigError, match="must be UTF-8 text"):
        check_document_text("aflow.toml", 1)  # type: ignore[arg-type]


def test_validate_candidate_pair_creates_no_candidate_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[str] = []

    def deny(*names: str):
        def _deny(*args: object, **kwargs: object) -> object:
            created.extend(names)
            raise AssertionError(f"candidate validation created {names}")

        return _deny

    monkeypatch.setattr(tempfile, "TemporaryDirectory", deny("TemporaryDirectory"))
    monkeypatch.setattr(Path, "write_text", deny("Path.write_text"))
    monkeypatch.setattr(tempfile, "mkstemp", deny("mkstemp"))

    assert validate_candidate_pair(AFLOW, WORKFLOWS).state == "ready"
    assert validate_candidate_pair("a = 1\nb = \n", WORKFLOWS).state == "invalid"
    assert validate_candidate_pair(
        AFLOW, WORKFLOWS.replace('prompts = ["p"]', 'prompts = ["missing"]')
    ).state == "invalid"
    assert created == []


# ---------------------------------------------------------------------------
# AFLOW-MAINT-20261006-03 checkpoint 1 repair: BOM and NUL-root regressions
# ---------------------------------------------------------------------------

BOM = "\ufeff"


def _escaped_nul_root_aflow() -> str:
    # The TOML source carries the escape sequence \u0000 (six characters),
    # which decodes to an embedded NUL in the parsed value. The raw document
    # text has no literal NUL byte, so the bounds check must not reject it.
    return AFLOW.replace(
        'default_workflow = "simple"',
        'default_workflow = "simple"\nworktree_root = "trees\\u0000tail"',
    )


def test_bom_prefixed_documents_are_rejected_equivalently(tmp_path: Path) -> None:
    """Filesystem and text paths reject a BOM with the same diagnostics."""
    # BOM-prefixed [aflow] main document.
    bom_aflow = BOM + AFLOW
    with pytest.raises(ConfigError) as text_exc:
        parse_workflow_pair(bom_aflow, WORKFLOWS, source_dir=tmp_path)
    assert "invalid TOML in aflow.toml" in str(text_exc.value)
    assert "line 1" in str(text_exc.value)

    (tmp_path / "aflow.toml").write_bytes(bom_aflow.encode("utf-8"))
    (tmp_path / "workflows.toml").write_text(WORKFLOWS, encoding="utf-8")
    with pytest.raises(ConfigError) as file_exc:
        load_workflow_config(tmp_path / "aflow.toml")
    assert str(text_exc.value) == str(file_exc.value)

    report = validate_candidate_pair(bom_aflow, WORKFLOWS)
    assert report.state == "invalid"
    assert report.issues[0].document == "aflow.toml"
    assert report.issues[0].line == 1

    # BOM-only sibling document.
    bom_only = BOM
    with pytest.raises(ConfigError) as sibling_text_exc:
        parse_workflow_pair(AFLOW, bom_only, source_dir=tmp_path)
    assert "invalid TOML in workflows.toml" in str(sibling_text_exc.value)
    assert "line 1" in str(sibling_text_exc.value)

    (tmp_path / "aflow.toml").write_text(AFLOW, encoding="utf-8")
    (tmp_path / "workflows.toml").write_bytes(bom_only.encode("utf-8"))
    with pytest.raises(ConfigError) as sibling_file_exc:
        load_workflow_config(tmp_path / "aflow.toml")
    assert str(sibling_text_exc.value) == str(sibling_file_exc.value)

    sibling_report = validate_candidate_pair(AFLOW, bom_only)
    assert sibling_report.state == "invalid"
    assert sibling_report.issues[0].document == "workflows.toml"
    assert sibling_report.issues[0].line == 1

    # Equivalent inputs without a BOM still succeed.
    assert validate_candidate_pair(AFLOW, WORKFLOWS).state == "ready"


def test_escaped_nul_root_reports_ready_like_baseline() -> None:
    """An escaped NUL in the root value yields the baseline structured report."""
    escaped_nul_aflow = _escaped_nul_root_aflow()
    assert "\\u0000" in escaped_nul_aflow
    assert "\x00" not in escaped_nul_aflow

    report = validate_candidate_pair(escaped_nul_aflow, WORKFLOWS)
    assert report.state == "ready"
    assert report.issues == ()
    assert report.workflows == ("simple",)

    # A literal NUL in submitted text is still rejected at the bounds check.
    with pytest.raises(ProjectConfigError, match="must not contain NUL characters"):
        validate_candidate_pair("a = 1\x00\n", WORKFLOWS)


def test_valid_candidate_performs_one_semantic_validation_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid candidate runs exactly one semantic validation pass."""
    import aflow.config as aflow_config

    calls: list[object] = []
    original = aflow_config.validate_workflow_config

    def counting(config):
        calls.append(config)
        return original(config)

    monkeypatch.setattr(aflow_config, "validate_workflow_config", counting, raising=True)
    report = validate_candidate_pair(AFLOW, WORKFLOWS)
    assert report.state == "ready"
    assert len(calls) == 1


def test_rest_validation_route_reports_escaped_nul_root(tmp_path: Path) -> None:
    """The live REST route returns the structured report, not operation_rejected."""
    from fastapi.testclient import TestClient

    from aflow_app_server.global_config_service import GlobalConfigService
    from aflow_app_server.main import (
        app,
        get_global_config_service,
        verify_token,
    )

    service = GlobalConfigService(
        config_dir=tmp_path / "config",
        audit_path=tmp_path / "audit.log",
    )
    # Override only authentication and the global service dependency; the
    # production lifespan/startup is never entered.
    app.dependency_overrides[get_global_config_service] = lambda: service
    app.dependency_overrides[verify_token] = lambda: "test-token"
    try:
        client = TestClient(app)
        response = client.post(
            "/api/config/validate",
            json={
                "aflow_toml": _escaped_nul_root_aflow(),
                "workflows_toml": WORKFLOWS,
            },
        )
    finally:
        app.dependency_overrides.pop(get_global_config_service, None)
        app.dependency_overrides.pop(verify_token, None)

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "ready"
    assert body["issues"] == []
    assert "operation_rejected" not in response.text
