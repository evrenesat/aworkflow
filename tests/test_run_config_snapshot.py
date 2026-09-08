"""Tests for the frozen per-run configuration snapshots.

Covers reservation-time capture, fingerprint verification against the launch
manifest, stability checks around reads, relative worktree-root resolution,
resume through the snapshot after global changes, and copy semantics for
resumed successor runs.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import textwrap

import pytest

from aflow.config import load_workflow_config
from aflow.run_config_snapshot import (
    SnapshotError,
    configuration_pair_lock,
    copy_run_config_snapshot,
    create_run_config_snapshot,
    load_run_config_snapshot,
    snapshot_directory,
)
from aflow.workflow import _freeze_run_identity
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


def test_snapshot_captures_the_effective_pair_and_manifest(tmp_path, global_pair):
    home, config_path = global_pair
    repo = _repo(tmp_path)
    workflow_config = load_workflow_config(config_path)
    fingerprint = _freeze_run_identity("simple", workflow_config, config_dir=config_path).config_fingerprint

    snapshot = create_run_config_snapshot(
        repo_root=repo,
        run_id="20260908t000000z-00000001",
        config_path=config_path,
        workflow_name="simple",
        fingerprint=fingerprint,
    )

    run_dir = snapshot_directory(repo, "20260908t000000z-00000001")
    assert snapshot.config_path == run_dir / "aflow.toml"
    assert (run_dir / "workflows.toml").is_file()
    manifest = json.loads((run_dir / "snapshot.json").read_text(encoding="utf-8"))
    assert manifest["origin"]["config_fingerprint"] == fingerprint
    assert manifest["origin"]["config_path"] == str(config_path.resolve())
    assert manifest["files"]["aflow.toml"]
    # The snapshot loads through the production loader with the same fingerprint.
    loaded = load_workflow_config(snapshot.config_path)
    recomputed = _freeze_run_identity("simple", loaded, config_dir=config_path)
    assert recomputed.config_fingerprint == fingerprint


def test_snapshot_rejects_a_fingerprint_mismatch_without_writing(tmp_path, global_pair):
    home, config_path = global_pair
    repo = _repo(tmp_path)
    with pytest.raises(SnapshotError, match="not a regular file|fingerprint|configuration"):
        create_run_config_snapshot(
            repo_root=repo,
            run_id="20260908t000000z-00000002",
            config_path=config_path,
            workflow_name="simple",
            fingerprint="0" * 64,
        )
    assert not snapshot_directory(repo, "20260908t000000z-00000002").exists()


def test_snapshot_resolution_makes_relative_worktree_root_absolute(tmp_path, global_pair):
    home, config_path = global_pair
    repo = _repo(tmp_path)
    workflow_config = load_workflow_config(config_path)
    fingerprint = _freeze_run_identity("simple", workflow_config, config_dir=config_path).config_fingerprint
    snapshot = create_run_config_snapshot(
        repo_root=repo,
        run_id="20260908t000000z-00000003",
        config_path=config_path,
        workflow_name="simple",
        fingerprint=fingerprint,
    )
    text = snapshot.config_path.read_text(encoding="utf-8")
    assert "worktree_root" in text
    resolved_line = next(
        line for line in text.splitlines() if "worktree_root" in line
    )
    # The relative origin value resolves against the original config location.
    assert str((config_path.parent / "trees").resolve()) in resolved_line
    assert 'worktree_root = "trees"' not in text


def test_snapshot_reuses_an_identical_existing_snapshot(tmp_path, global_pair):
    home, config_path = global_pair
    repo = _repo(tmp_path)
    workflow_config = load_workflow_config(config_path)
    fingerprint = _freeze_run_identity("simple", workflow_config, config_dir=config_path).config_fingerprint
    run_id = "20260908t000000z-00000004"
    first = create_run_config_snapshot(
        repo_root=repo, run_id=run_id, config_path=config_path,
        workflow_name="simple", fingerprint=fingerprint,
    )
    second = create_run_config_snapshot(
        repo_root=repo, run_id=run_id, config_path=config_path,
        workflow_name="simple", fingerprint=fingerprint,
    )
    assert first.directory == second.directory
    # A different fingerprint for the same run is a hard conflict.
    with pytest.raises(SnapshotError, match="different fingerprint"):
        create_run_config_snapshot(
            repo_root=repo, run_id=run_id, config_path=config_path,
            workflow_name="simple", fingerprint="1" * 64,
        )


def test_snapshot_detects_unstable_manual_edits(tmp_path, global_pair, monkeypatch):
    home, config_path = global_pair
    repo = _repo(tmp_path)
    real_read = Path.read_bytes

    reads = {"n": 0}

    def flaky_read(self, *args, **kwargs):
        payload = real_read(self, *args, **kwargs)
        if self == config_path:
            reads["n"] += 1
            if reads["n"] == 1:
                # A concurrent manual edit between the two stat+read passes.
                config_path.write_text(VALID_AFLOW.replace('"test-model"', '"other"'), encoding="utf-8")
        return payload

    monkeypatch.setattr(Path, "read_bytes", flaky_read)
    with pytest.raises(SnapshotError, match="changed while reading"):
        create_run_config_snapshot(
            repo_root=repo, run_id="20260908t000000z-00000005",
            config_path=config_path, workflow_name="simple",
            fingerprint="2" * 64,
        )
    assert not snapshot_directory(repo, "20260908t000000z-00000005").exists()


def test_global_edit_after_snapshot_keeps_original_runnable(tmp_path, global_pair):
    """Config A stays resumable after global config B is saved (scenario 3)."""
    home, config_path = global_pair
    repo = _repo(tmp_path)
    workflow_config_a = load_workflow_config(config_path)
    fingerprint_a = _freeze_run_identity("simple", workflow_config_a, config_dir=config_path).config_fingerprint
    run_id = "20260908t000000z-00000006"
    snapshot = create_run_config_snapshot(
        repo_root=repo, run_id=run_id, config_path=config_path,
        workflow_name="simple", fingerprint=fingerprint_a,
    )

    # Save configuration B globally (different model), even delete the workflow.
    home.joinpath(".config/aflow/aflow.toml").write_text(
        VALID_AFLOW.replace("default_workflow = \"simple\"", "default_workflow = \"other\"")
                   .replace('"test-model"', '"model-b"'),
        encoding="utf-8",
    )
    home.joinpath(".config/aflow/workflows.toml").write_text(
        VALID_WORKFLOWS.replace("simple", "other"), encoding="utf-8"
    )

    # The frozen snapshot still loads workflow A with fingerprint A.
    loaded = load_workflow_config(snapshot.config_path)
    assert "simple" in loaded.workflows
    recomputed = _freeze_run_identity("simple", loaded, config_dir=Path(snapshot.origin_config_path))
    assert recomputed.config_fingerprint == fingerprint_a
    # The current global config no longer provides workflow 'simple' at all,
    # so a legacy current-config comparison cannot silently substitute it.
    current = load_workflow_config(config_path)
    assert "simple" not in current.workflows


def test_copy_run_config_snapshot_preserves_origin(tmp_path, global_pair):
    home, config_path = global_pair
    repo = _repo(tmp_path)
    workflow_config = load_workflow_config(config_path)
    fingerprint = _freeze_run_identity("simple", workflow_config, config_dir=config_path).config_fingerprint
    source = create_run_config_snapshot(
        repo_root=repo, run_id="20260908t000000z-00000007",
        config_path=config_path, workflow_name="simple", fingerprint=fingerprint,
    )
    copied = copy_run_config_snapshot(
        repo, source=source, run_id="20260908t000000z-00000008",
        workflow_name="simple", fingerprint=fingerprint,
    )
    assert copied.fingerprint == fingerprint
    assert copied.origin_config_path == source.origin_config_path
    manifest = json.loads((copied.directory / "snapshot.json").read_text(encoding="utf-8"))
    assert manifest["copied_from_run_id"] == source.run_id
    with pytest.raises(SnapshotError, match="does not match the source run snapshot"):
        copy_run_config_snapshot(
            repo, source=source, run_id="20260908t000000z-00000009",
            workflow_name="simple", fingerprint="3" * 64,
        )


def test_configuration_pair_lock_serializes_saves_and_snapshots(tmp_path, global_pair):
    home, config_path = global_pair
    acquired: list[str] = []
    import threading

    def blocker():
        with configuration_pair_lock(config_path.parent):
            acquired.append("blocker")

    with configuration_pair_lock(config_path.parent):
        thread = threading.Thread(target=blocker)
        thread.start()
        thread.join(timeout=0.3)
        assert acquired == []  # still held
    thread.join(timeout=5)
    assert acquired == ["blocker"]
