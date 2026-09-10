"""Tests for current-source resolution and locked pair loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from aflow.config import ConfigError
from aflow.live_config import LiveConfigError, load_live_config, load_live_config_for_run
from aflow.run_config_snapshot import create_run_config_snapshot
from tests._support import _write_split_config

VALID_AFLOW = """\
[aflow]
default_workflow = "simple"

[roles]
architect = "codex.default"

[harness.codex.profiles.default]
model = "model-a"

[prompts]
p = "Work."
"""

VALID_WORKFLOWS = """\
[workflow.simple]
[workflow.simple.steps.implement]
role = "architect"
prompts = ["p"]
go = [{ to = "END" }]
"""


def test_loader_reads_the_selected_pair_once_and_reports_provenance(tmp_path: Path) -> None:
    config_path, _ = _write_split_config(
        tmp_path / "selected", VALID_AFLOW, VALID_WORKFLOWS
    )

    loaded = load_live_config(config_path)

    assert loaded.source.kind == "explicit"
    assert loaded.source.config_path == config_path.resolve()
    assert loaded.source.workflows_path == config_path.with_name("workflows.toml")
    assert loaded.workflow_config.workflows["simple"].first_step == "implement"


def test_relative_filesystem_settings_use_the_selected_file_directory(
    tmp_path: Path,
) -> None:
    config_path, _ = _write_split_config(
        tmp_path / "selected",
        VALID_AFLOW.replace("default_workflow", "worktree_root = \"trees\"\ndefault_workflow"),
        VALID_WORKFLOWS,
    )

    loaded = load_live_config(config_path)

    assert loaded.workflow_config.aflow.worktree_root == str(
        (config_path.parent / "trees").resolve()
    )


def test_missing_selected_file_does_not_become_empty_configuration(tmp_path: Path) -> None:
    with pytest.raises(LiveConfigError, match="live workflow configuration .*does not exist"):
        load_live_config(tmp_path / "missing" / "aflow.toml")


def test_saved_live_path_is_strict_but_missing_legacy_hint_falls_through(
    tmp_path: Path,
) -> None:
    default, _ = _write_split_config(
        tmp_path / "default", VALID_AFLOW.replace("model-a", "default-model"), VALID_WORKFLOWS
    )

    with pytest.raises(LiveConfigError, match="saved source"):
        load_live_config(
            saved_live_config_path=tmp_path / "missing" / "aflow.toml",
            legacy_snapshot_origin=tmp_path / "old" / "aflow.toml",
            default_config_path=default,
        )

    loaded = load_live_config(
        legacy_snapshot_origin=tmp_path / "old" / "aflow.toml",
        default_config_path=default,
    )
    assert loaded.source.kind == "default"
    assert loaded.workflow_config.harnesses["codex"].profiles["default"].model == "default-model"


def test_existing_malformed_current_toml_is_not_replaced_by_default(
    tmp_path: Path,
) -> None:
    malformed = tmp_path / "malformed" / "aflow.toml"
    malformed.parent.mkdir()
    malformed.write_text("[aflow\n", encoding="utf-8")
    default, _ = _write_split_config(tmp_path / "default", VALID_AFLOW, VALID_WORKFLOWS)

    with pytest.raises(ConfigError, match="invalid TOML"):
        load_live_config(malformed, default_config_path=default)


def test_run_loader_uses_live_origin_after_snapshot_copy_is_removed(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    config_path, _ = _write_split_config(source_dir, VALID_AFLOW, VALID_WORKFLOWS)
    repo = tmp_path / "repo"
    (repo / ".aflow" / "runs").mkdir(parents=True)
    snapshot = create_run_config_snapshot(
        repo_root=repo,
        run_id="legacy-run",
        config_path=config_path,
        workflow_name="simple",
        fingerprint="old",
    )
    config_path.write_text(VALID_AFLOW.replace("model-a", "model-live"), encoding="utf-8")
    snapshot.config_path.unlink()
    snapshot.workflows_path.unlink()

    loaded = load_live_config_for_run(repo, "legacy-run")

    assert loaded.source.kind == "legacy_snapshot_origin"
    assert loaded.workflow_config.harnesses["codex"].profiles["default"].model == "model-live"
