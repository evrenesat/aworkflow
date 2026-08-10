from __future__ import annotations

from pathlib import Path

import pytest

from aflow.api import CapabilitySet, ContextBundle, RunControlRequest, StartRunResult
from aflow.control_plane import build_context_bundle


def test_public_control_plane_models_are_versioned_and_redact_secrets() -> None:
    capabilities = CapabilitySet(workflows=("managed",), controls=("owner_stop",))
    result = StartRunResult(run_id="control-run-7", created=True, status="manifest_only")
    control = RunControlRequest(expected_revision=2, role_selectors={"worker": "codex.high"})
    bundle = ContextBundle(
        run_id="control-run-7",
        level="lite",
        data={"authorization": "Bearer private", "visible": "ok"},
    )

    assert capabilities.to_dict()["schema_version"] == 1
    assert result.to_dict()["run_id"] == "control-run-7"
    assert control.to_dict()["expected_revision"] == 2
    assert bundle.to_dict()["data"]["authorization"] == "[redacted]"


def test_context_bundle_defaults_to_lite_and_requires_explicit_full_scope(tmp_path: Path) -> None:
    run_dir = tmp_path / ".aflow" / "runs" / "control-run-8"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text('{"status":"running","token":"private"}\n')

    lite = build_context_bundle(run_dir)
    assert lite.level == "lite"
    assert lite.to_dict()["data"]["run_metadata"]["token"] == "[redacted]"
    with pytest.raises(PermissionError, match="explicit"):
        build_context_bundle(run_dir, level="full")
    assert build_context_bundle(run_dir, level="full", full_scope=True).level == "full"
