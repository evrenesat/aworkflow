from __future__ import annotations

import json
from pathlib import Path

import pytest

from aflow.api import CapabilitySet, ContextBundle, RunControlRequest, StartRunResult
from aflow.control_plane import (
    RunProgressCheckpoint,
    RunProgressDetail,
    build_context_bundle,
)
import aflow.api.models as models
import aflow.control_plane as control_plane


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
    payload = bundle.to_dict()
    assert payload["data"]["authorization"] == "[redacted]"
    assert "Bearer private" not in json.dumps(payload)


def test_context_bundle_redacts_nested_sensitive_keys_and_values() -> None:
    nested_token = "nested-private-token"
    nested_cookie = "nested-private-cookie"
    progress_secret = "progress-private-secret"
    bundle = ContextBundle(
        run_id="control-run-9",
        level="full",
        data={
            "visible": "ok",
            "metadata": {
                "api_key": nested_token,
                "details": {"cookie": nested_cookie, "label": "safe"},
            },
            "progress": {
                "token": progress_secret,
                "checkpoint": "visible progress detail",
            },
        },
    )

    payload = bundle.to_dict()

    assert payload["data"]["metadata"]["api_key"] == "[redacted]"
    assert payload["data"]["metadata"]["details"]["cookie"] == "[redacted]"
    assert payload["data"]["progress"]["token"] == "[redacted]"
    assert payload["data"]["metadata"]["details"]["label"] == "safe"
    serialized = json.dumps(payload)
    assert nested_token not in serialized
    assert nested_cookie not in serialized
    assert progress_secret not in serialized


def test_context_bundle_preserves_large_progress_detail_history() -> None:
    detail = RunProgressDetail(
        checkpoints=tuple(
            RunProgressCheckpoint(f"checkpoint-{index}")
            for index in range(129)
        ),
    )
    bundle = ContextBundle(
        run_id="control-run-10",
        level="full",
        data={"progress": detail, "visible": "ok"},
    )

    progress = bundle.to_dict()["data"]["progress"]

    assert len(progress["checkpoints"]) == 129
    assert progress["checkpoints"][-1]["checkpoint_id"] == "checkpoint-128"
    assert progress["checkpoints"][-1] != "[truncated: item limit]"


def test_public_control_plane_models_are_explicitly_reexported() -> None:
    shared_names = (
        "CapabilitySet",
        "ContextBundle",
        "LaunchManifest",
        "RunControlRequest",
        "RunEvent",
        "RunStatus",
        "StartRunResult",
    )

    assert set(shared_names).issubset(models.__all__)
    for name in shared_names:
        assert getattr(models, name) is getattr(control_plane, name)


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
