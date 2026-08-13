from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


HELPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "aflow"
    / "bundled_skills"
    / "aflow-guard-development-run"
    / "scripts"
    / "aflow_guard_issue.py"
)


def _helper_module():
    spec = importlib.util.spec_from_file_location("guard_issue", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _payload() -> dict[str, object]:
    return {
        "title": "controller reports running without an owner",
        "fingerprint": "abcdef1234567890",
        "aflow_version": "a87c144",
        "expected": "A running state has one controller.",
        "actual": "Durable state remained running after the controller exited.",
        "impact": "The observer cannot establish ownership.",
        "reproduction": "Start a disposable run, terminate its controller, and read state.",
        "evidence": "Status running; controller count zero.",
        "workaround": "None.",
        "redaction_terms": ["private-project"],
    }


def test_valid_payload_renders_fingerprint_marker() -> None:
    helper = _helper_module()
    payload = helper.validate_payload(_payload())
    title, body, marker = helper.render_issue(payload)

    assert title.startswith("[AFlow guard]")
    assert marker in body
    assert "private-project" not in body


@pytest.mark.parametrize(
    "field,value",
    [
        ("evidence", "Authorization: Bearer abc123"),
        ("actual", "read /root/code/private-project/run.json"),
        ("reproduction", "open https://private.example/run"),
        ("impact", "private-project cannot continue"),
    ],
)
def test_sensitive_or_project_identifying_content_is_rejected(
    field: str, value: str
) -> None:
    helper = _helper_module()
    payload = _payload()
    payload[field] = value

    with pytest.raises(ValueError, match="sensitive|redaction"):
        helper.validate_payload(payload)


def test_project_identity_fields_are_rejected() -> None:
    helper = _helper_module()
    payload = _payload()
    payload["project_name"] = "private-project"

    with pytest.raises(ValueError, match="project-identifying"):
        helper.validate_payload(payload)
