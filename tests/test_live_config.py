"""Tests for current-source resolution and locked pair loading."""

from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest

from aflow.config import ConfigError, load_workflow_config
from aflow.config_pair import TRANSACTION_RECORD_NAME, configuration_pair_lock
from aflow.live_config import (
    LiveConfigError,
    LiveConfigPairLockBusy,
    load_live_config,
    load_live_config_for_run,
)
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


def test_live_loader_reloads_upgrade_threshold_from_selected_pair(tmp_path: Path) -> None:
    config_path, workflows_path = _write_split_config(
        tmp_path / "selected", VALID_AFLOW, VALID_WORKFLOWS
    )

    initial = load_live_config(config_path)
    assert initial.workflow_config.workflows["simple"].upgrade_after_repairs == 1

    workflows_path.write_text(
        '[workflow]\nupgrade_after_repairs = 3\n\n' + VALID_WORKFLOWS,
        encoding="utf-8",
    )
    reloaded = load_live_config(config_path)

    assert reloaded.source.kind == "explicit"
    assert reloaded.workflow_config.workflows["simple"].upgrade_after_repairs == 3
    assert (
        reloaded.workflow_config.workflows["simple"].declared_upgrade_after_repairs
        is None
    )
    assert (
        reloaded.workflow_config.workflows["simple"].upgrade_after_repairs_source
        == "defaults"
    )

    workflows_path.write_text(
        '[workflow]\nupgrade_after_repairs = 0\n\n' + VALID_WORKFLOWS,
        encoding="utf-8",
    )
    zero = load_live_config(config_path).workflow_config.workflows["simple"]
    assert zero.upgrade_after_repairs == 0
    assert zero.declared_upgrade_after_repairs is None
    assert zero.upgrade_after_repairs_source == "defaults"


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


def _make_pending_pair(tmp_path: Path) -> Path:
    """Write a valid pair plus a prepared record for a newer generation."""
    from aflow.config_pair import _record_bytes

    config_path, _ = _write_split_config(
        tmp_path / "pair", VALID_AFLOW, VALID_WORKFLOWS
    )
    record = _record_bytes(
        "prepared",
        {
            "aflow.toml": VALID_AFLOW.encode(),
            "workflows.toml": VALID_WORKFLOWS.encode(),
        },
        {
            "aflow.toml": VALID_AFLOW.replace("model-a", "model-b").encode(),
            "workflows.toml": VALID_WORKFLOWS.encode(),
        },
    )
    (config_path.parent / TRANSACTION_RECORD_NAME).write_bytes(record)
    return config_path.parent


def _pending_symlinked_pair(tmp_path: Path) -> tuple[Path, Path]:
    """A valid prepared journal whose directory's aflow.toml is a leaf symlink.

    The leaf points at a valid pair in another directory; recovery of the
    original directory must stay authoritative instead of following the link.
    """
    pair_dir = _make_pending_pair(tmp_path)
    target, _ = _write_split_config(
        tmp_path / "other",
        VALID_AFLOW.replace("model-a", "operator-model"),
        VALID_WORKFLOWS,
    )
    link = pair_dir / "aflow.toml"
    link.unlink()
    link.symlink_to(target)
    return pair_dir, target


@pytest.mark.parametrize(
    "source_kind",
    ["explicit", "saved", "legacy_snapshot_origin", "default"],
)
@pytest.mark.parametrize("nonblocking", [False, True])
def test_pending_journal_with_leaf_symlink_fails_closed_for_every_source(
    tmp_path: Path, source_kind: str, nonblocking: bool
) -> None:
    pair_dir, target = _pending_symlinked_pair(tmp_path)
    journal = pair_dir / TRANSACTION_RECORD_NAME
    journal_bytes = journal.read_bytes()
    sibling_bytes = (pair_dir / "workflows.toml").read_bytes()
    target_bytes = target.read_bytes()
    target_sibling_bytes = target.with_name("workflows.toml").read_bytes()
    default, _ = _write_split_config(
        tmp_path / "default",
        VALID_AFLOW.replace("model-a", "default-model"),
        VALID_WORKFLOWS,
    )
    loaded_paths: list[Path] = []

    def loader(path: Path):
        loaded_paths.append(path)
        return load_workflow_config(path)

    kwargs = {
        "explicit": {"config_path": pair_dir / "aflow.toml"},
        "saved": {"saved_live_config_path": pair_dir / "aflow.toml"},
        "legacy_snapshot_origin": {
            "legacy_snapshot_origin": pair_dir / "aflow.toml",
            "default_config_path": default,
        },
        "default": {"default_config_path": pair_dir / "aflow.toml"},
    }[source_kind]

    with pytest.raises(LiveConfigError) as excinfo:
        load_live_config(loader=loader, nonblocking=nonblocking, **kwargs)

    # Bounded ConfigError-compatible failure before loader invocation, with no
    # fallback to the default pair (including the legacy branch).
    assert isinstance(excinfo.value, ConfigError)
    assert "symlink" in str(excinfo.value)
    assert loaded_paths == []
    # All evidence is preserved: journal, leaf symlink, target pair, sibling.
    assert journal.read_bytes() == journal_bytes
    assert (pair_dir / "aflow.toml").is_symlink()
    assert target.read_bytes() == target_bytes
    assert target.with_name("workflows.toml").read_bytes() == target_sibling_bytes
    assert (pair_dir / "workflows.toml").read_bytes() == sibling_bytes


def test_valid_leaf_symlink_without_journal_still_resolves_the_target(
    tmp_path: Path,
) -> None:
    target, _ = _write_split_config(tmp_path / "real", VALID_AFLOW, VALID_WORKFLOWS)
    linked = tmp_path / "linked"
    linked.mkdir()
    link = linked / "aflow.toml"
    link.symlink_to(target)

    loaded = load_live_config(link)

    assert loaded.source.kind == "explicit"
    assert loaded.source.config_path == target
    assert loaded.workflow_config.harnesses["codex"].profiles["default"].model == "model-a"


def test_missing_legacy_hint_without_journal_still_falls_through_to_default(
    tmp_path: Path,
) -> None:
    default, _ = _write_split_config(
        tmp_path / "default",
        VALID_AFLOW.replace("model-a", "default-model"),
        VALID_WORKFLOWS,
    )

    loaded = load_live_config(
        legacy_snapshot_origin=tmp_path / "old" / "aflow.toml",
        default_config_path=default,
    )

    assert loaded.source.kind == "default"
    assert loaded.workflow_config.harnesses["codex"].profiles["default"].model == "default-model"


def test_live_reader_recovers_prepared_pair_and_returns_old_generation(
    tmp_path: Path,
) -> None:
    pair_dir = _make_pending_pair(tmp_path)

    loaded = load_live_config(pair_dir / "aflow.toml")

    assert loaded.workflow_config.harnesses["codex"].profiles["default"].model == "model-a"
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()


def test_live_recovery_failure_is_config_error_blocking_and_nonblocking(
    tmp_path: Path,
) -> None:
    pair_dir = _make_pending_pair(tmp_path)
    (pair_dir / "aflow.toml").write_bytes(b"operator edit matching no generation")

    for kwargs in ({}, {"nonblocking": True}):
        with pytest.raises(ConfigError) as excinfo:
            load_live_config(pair_dir / "aflow.toml", **kwargs)
        assert not isinstance(excinfo.value, LiveConfigPairLockBusy)
        assert "changed while a configuration transaction was pending" in str(
            excinfo.value
        )

    # The reader failed closed: bytes and journal are preserved for a later
    # recoverable read, and no mixed pair was returned.
    assert (
        pair_dir / "aflow.toml"
    ).read_bytes() == b"operator edit matching no generation"
    assert (pair_dir / TRANSACTION_RECORD_NAME).is_file()


@pytest.mark.parametrize("digest", [None, 1, [], {}])
def test_non_string_record_digest_live_reader_is_config_error(
    tmp_path: Path, digest: object
) -> None:
    pair_dir = _make_pending_pair(tmp_path)
    record_path = pair_dir / TRANSACTION_RECORD_NAME
    data = json.loads(record_path.read_bytes())
    data["documents"]["aflow.toml"]["new"]["sha256"] = digest
    record_path.write_bytes(json.dumps(data, sort_keys=True).encode("utf-8"))

    with pytest.raises(ConfigError, match="malformed"):
        load_live_config(pair_dir / "aflow.toml")

    # The malformed journal is preserved, not consumed.
    assert record_path.read_bytes() == json.dumps(data, sort_keys=True).encode("utf-8")


def test_nonblocking_live_read_is_busy_not_unlocked(tmp_path: Path) -> None:
    config_path, _ = _write_split_config(
        tmp_path / "pair", VALID_AFLOW, VALID_WORKFLOWS
    )
    # A different thread holds the lock: flock is per open file description,
    # so same-thread reentry would (correctly) yield the held lock.
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with configuration_pair_lock(config_path.parent):
            held.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(timeout=10)
    try:
        with pytest.raises(LiveConfigPairLockBusy):
            load_live_config(config_path, nonblocking=True)
    finally:
        release.set()
        thread.join(timeout=10)
        assert not thread.is_alive()


def test_daemon_refresh_translates_recovery_failure_to_daemon_error(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from aflow.daemon import DaemonError, DaemonService

    pair_dir = _make_pending_pair(tmp_path)
    (pair_dir / "aflow.toml").write_bytes(b"operator edit matching no generation")
    service = DaemonService.__new__(DaemonService)
    service._config = SimpleNamespace(config_path=pair_dir / "aflow.toml")
    service._workflow_config = None

    with pytest.raises(DaemonError, match="workflow configuration is invalid"):
        service._refresh_workflow_config()


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
