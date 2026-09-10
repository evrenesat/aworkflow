"""Compatibility behavior for diagnostic run configuration snapshots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aflow.config import ConfigError, load_workflow_config
from aflow.live_config import LiveConfigError, load_live_config, load_live_config_for_run
from aflow.run_config_snapshot import (
    configuration_pair_lock,
    copy_run_config_snapshot,
    create_run_config_snapshot,
    load_run_config_snapshot,
    snapshot_directory,
)
from tests._support import _write_split_config

VALID_AFLOW = """\
[aflow]
default_workflow = "simple"
worktree_root = "trees"

[roles]
architect = "codex.default"

[harness.codex.profiles.default]
model = "test-model"

[prompts]
p = "Work from {ACTIVE_PLAN_PATH}."
"""

VALID_WORKFLOWS = """\
[workflow.simple]
[workflow.simple.steps.implement_plan]
role = "architect"
prompts = ["p"]
go = [{ to = "END", when = "DONE" }]
"""


@pytest.fixture()
def global_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    aflow_path, _ = _write_split_config(home, VALID_AFLOW, VALID_WORKFLOWS)
    return home, aflow_path


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    (repo / ".aflow" / "runs").mkdir(parents=True)
    return repo


def _write_pair(config_path: Path, aflow_text: str, workflows_text: str) -> Path:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(aflow_text, encoding="utf-8")
    config_path.with_name("workflows.toml").write_text(
        workflows_text, encoding="utf-8"
    )
    return config_path


def _snapshot(
    repo: Path,
    config_path: Path,
    run_id: str,
    *,
    fingerprint: str | None = "legacy-fingerprint",
):
    return create_run_config_snapshot(
        repo_root=repo,
        run_id=run_id,
        config_path=config_path,
        workflow_name="simple",
        fingerprint=fingerprint,
    )


def test_snapshot_captures_a_diagnostic_pair_and_preserves_relative_paths(
    tmp_path: Path, global_pair: tuple[Path, Path]
) -> None:
    _, config_path = global_pair
    repo = _repo(tmp_path)

    snapshot = _snapshot(repo, config_path, "run-1")
    manifest = json.loads((snapshot.directory / "snapshot.json").read_text())

    assert snapshot.config_path == snapshot.directory / "aflow.toml"
    assert (snapshot.directory / "workflows.toml").is_file()
    assert manifest["origin"]["config_path"] == str(config_path.resolve())
    assert manifest["origin"]["config_fingerprint"] == "legacy-fingerprint"
    assert "files" not in manifest
    copied = snapshot.config_path.read_text(encoding="utf-8")
    assert str((config_path.parent / "trees").resolve()) in copied
    assert 'worktree_root = "trees"' not in copied


def test_snapshot_does_not_admit_or_reject_on_fingerprint_differences(
    tmp_path: Path, global_pair: tuple[Path, Path]
) -> None:
    _, config_path = global_pair
    repo = _repo(tmp_path)

    first = _snapshot(repo, config_path, "run-2", fingerprint="first")
    same_run = _snapshot(repo, config_path, "run-2", fingerprint="different")

    assert same_run.directory == first.directory
    assert same_run.fingerprint == "first"

    copied = copy_run_config_snapshot(
        repo,
        source=first,
        run_id="run-3",
        workflow_name="other",
        fingerprint="different",
    )
    assert copied.origin_config_path == first.origin_config_path
    assert copied.fingerprint == first.fingerprint


def test_snapshot_loader_ignores_edited_or_missing_historical_copies(
    tmp_path: Path, global_pair: tuple[Path, Path]
) -> None:
    _, config_path = global_pair
    repo = _repo(tmp_path)
    snapshot = _snapshot(repo, config_path, "run-4")

    snapshot.config_path.write_text("historical copy was edited", encoding="utf-8")
    snapshot.workflows_path.unlink()
    loaded = load_run_config_snapshot(repo, "run-4")

    assert loaded is not None
    assert loaded.origin_config_path == str(config_path.resolve())
    assert loaded.fingerprint == "legacy-fingerprint"


def test_snapshot_manifest_without_old_fingerprint_remains_readable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    directory = snapshot_directory(repo, "legacy-run")
    directory.mkdir(parents=True)
    (directory / "snapshot.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "legacy-run",
                "origin": {"config_path": "/legacy/aflow.toml"},
                "files": {"aflow.toml": "old-digest"},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_run_config_snapshot(repo, "legacy-run")

    assert loaded is not None
    assert loaded.fingerprint is None
    assert loaded.origin_config_path == "/legacy/aflow.toml"


def test_current_source_uses_legacy_origin_not_snapshot_files(
    tmp_path: Path, global_pair: tuple[Path, Path]
) -> None:
    home, config_path = global_pair
    repo = _repo(tmp_path)
    snapshot = _snapshot(repo, config_path, "run-5")

    config_path.write_text(VALID_AFLOW.replace("test-model", "live-model"), encoding="utf-8")
    snapshot.config_path.unlink()
    snapshot.workflows_path.unlink()

    loaded = load_live_config_for_run(
        repo,
        "run-5",
        default_config_path=home / ".config" / "aflow" / "missing.toml",
    )

    assert loaded.source.kind == "legacy_snapshot_origin"
    assert loaded.config_path == config_path.resolve()
    assert loaded.workflow_config.harnesses["codex"].profiles["default"].model == "live-model"


def test_missing_legacy_origin_falls_back_to_current_default(
    tmp_path: Path, global_pair: tuple[Path, Path]
) -> None:
    _, config_path = global_pair
    repo = _repo(tmp_path)
    _snapshot(repo, config_path, "run-6")
    config_path.unlink()

    fallback = tmp_path / "current" / "aflow.toml"
    _write_pair(
        fallback,
        VALID_AFLOW.replace("test-model", "default-model"),
        VALID_WORKFLOWS,
    )
    loaded = load_live_config_for_run(repo, "run-6", default_config_path=fallback)

    assert loaded.source.kind == "default"
    assert loaded.config_path == fallback.resolve()
    assert loaded.workflow_config.harnesses["codex"].profiles["default"].model == "default-model"


def test_explicit_source_wins_over_saved_legacy_and_default_paths(
    tmp_path: Path, global_pair: tuple[Path, Path]
) -> None:
    _, legacy_path = global_pair
    explicit = tmp_path / "explicit" / "aflow.toml"
    saved = tmp_path / "saved" / "aflow.toml"
    default = tmp_path / "default" / "aflow.toml"
    for path, model in (
        (explicit, "explicit-model"),
        (saved, "saved-model"),
        (default, "default-model"),
    ):
        _write_pair(
            path,
            VALID_AFLOW.replace("test-model", model),
            VALID_WORKFLOWS,
        )

    loaded = load_live_config(
        explicit,
        saved_live_config_path=saved,
        legacy_snapshot_origin=legacy_path,
        default_config_path=default,
    )

    assert loaded.source.kind == "explicit"
    assert loaded.workflow_config.harnesses["codex"].profiles["default"].model == "explicit-model"


def test_missing_saved_source_is_actionable_and_never_uses_snapshot_or_default(
    tmp_path: Path, global_pair: tuple[Path, Path]
) -> None:
    _, legacy_path = global_pair
    default = tmp_path / "default" / "aflow.toml"
    _write_split_config(default.parent, VALID_AFLOW, VALID_WORKFLOWS)

    with pytest.raises(LiveConfigError, match="live workflow configuration .*does not exist"):
        load_live_config(
            saved_live_config_path=tmp_path / "missing" / "aflow.toml",
            legacy_snapshot_origin=legacy_path,
            default_config_path=default,
        )


def test_malformed_existing_live_source_is_not_replaced_by_default(
    tmp_path: Path, global_pair: tuple[Path, Path]
) -> None:
    _, legacy_path = global_pair
    malformed = tmp_path / "malformed" / "aflow.toml"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("[aflow\n", encoding="utf-8")
    default = tmp_path / "default" / "aflow.toml"
    _write_split_config(default.parent, VALID_AFLOW, VALID_WORKFLOWS)

    with pytest.raises(ConfigError, match="invalid TOML"):
        load_live_config(
            malformed,
            legacy_snapshot_origin=legacy_path,
            default_config_path=default,
        )


def test_live_loader_keeps_optional_workflow_sibling_behavior(tmp_path: Path) -> None:
    config_path = tmp_path / "single" / "aflow.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '[harness.codex.profiles.default]\nmodel = "single-model"\n',
        encoding="utf-8",
    )

    loaded = load_live_config(config_path)

    assert loaded.source.workflows_path == config_path.with_name("workflows.toml")
    assert loaded.workflow_config.harnesses["codex"].profiles["default"].model == "single-model"
    assert loaded.workflow_config.workflows == {}


def test_relative_explicit_source_resolves_from_the_selected_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "relative" / "aflow.toml"
    _write_pair(config_path, VALID_AFLOW, VALID_WORKFLOWS)
    monkeypatch.chdir(tmp_path)

    loaded = load_live_config(Path("relative") / "aflow.toml")

    assert loaded.config_path == config_path.resolve()
    assert loaded.source.workflows_path == config_path.with_name("workflows.toml")


def test_configuration_pair_lock_still_serializes_snapshot_and_live_reads(
    tmp_path: Path, global_pair: tuple[Path, Path]
) -> None:
    _, config_path = global_pair
    repo = _repo(tmp_path)
    acquired: list[str] = []
    import threading

    def blocker() -> None:
        with configuration_pair_lock(config_path.parent):
            acquired.append("blocker")

    with configuration_pair_lock(config_path.parent):
        thread = threading.Thread(target=blocker)
        thread.start()
        thread.join(timeout=0.3)
        assert acquired == []
    thread.join(timeout=5)

    assert acquired == ["blocker"]
    assert load_workflow_config(config_path).workflows["simple"].first_step == "implement_plan"
