"""Tests for shared, revisioned project scheduling settings."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path
import subprocess

import pytest

from aflow.project_settings import (
    DEFAULT_AUTO_CONSUME_PLANS,
    DEFAULT_MAX_CONCURRENT_IMPLEMENTATIONS,
    EMPTY_PROJECT_SETTINGS_REVISION,
    ProjectSettings,
    ProjectSettingsCorruptionError,
    ProjectSettingsPathError,
    ProjectSettingsRevisionConflict,
    ProjectSettingsService,
    ProjectSettingsValidationError,
)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _committed_git_repo(root: Path) -> Path:
    root.mkdir()
    subprocess.run(("git", "init", "-q", "-b", "main", str(root)), check=True)
    _git(root, "config", "user.name", "Project settings test")
    _git(root, "config", "user.email", "project-settings@example.test")
    (root / "README.md").write_text("project\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _committed_separate_git_repo(
    root: Path,
    *,
    configure_primary_hint: bool = True,
) -> tuple[Path, Path]:
    root.mkdir()
    git_dir = root.parent / "external-git" / f"{root.name}.git"
    git_dir.parent.mkdir()
    subprocess.run(
        (
            "git",
            "init",
            "-q",
            "--separate-git-dir",
            str(git_dir),
            "-b",
            "main",
            str(root),
        ),
        check=True,
    )
    _git(root, "config", "user.name", "Project settings test")
    _git(root, "config", "user.email", "project-settings@example.test")
    (root / "README.md").write_text("project\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")
    if configure_primary_hint:
        _git(root, "config", "core.worktree", str(root))
    return root, git_dir


def test_missing_settings_use_defaults_without_write_on_read(tmp_path: Path) -> None:
    service = ProjectSettingsService(tmp_path)

    snapshot = service.read()

    assert snapshot.auto_consume_plans is DEFAULT_AUTO_CONSUME_PLANS
    assert (
        snapshot.max_concurrent_implementations
        == DEFAULT_MAX_CONCURRENT_IMPLEMENTATIONS
    )
    assert snapshot.persisted is False
    assert snapshot.source == "defaults"
    assert snapshot.revision == EMPTY_PROJECT_SETTINGS_REVISION
    assert not (tmp_path / ".aflow").exists()


def test_legacy_aflow_directory_does_not_migrate_or_change_defaults(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".aflow" / "config" / "aflow.toml"
    config.parent.mkdir(parents=True)
    config.write_text("legacy = true\n", encoding="utf-8")
    before = config.read_bytes()

    snapshot = ProjectSettingsService(tmp_path).read()

    assert snapshot.settings == ProjectSettings()
    assert config.read_bytes() == before
    assert not (tmp_path / ".aflow" / "project-settings.json").exists()
    assert not (tmp_path / ".aflow" / "project-settings.lock").exists()


def test_save_round_trips_canonical_payload_and_revision(tmp_path: Path) -> None:
    service = ProjectSettingsService(tmp_path)
    initial = service.read()
    expected = ProjectSettings(auto_consume_plans=False, max_concurrent_implementations=7)

    saved = service.save(expected, expected_revision=initial.revision)

    payload = (tmp_path / ".aflow" / "project-settings.json").read_bytes()
    assert json.loads(payload) == expected.to_dict()
    assert saved.settings == expected
    assert saved.persisted is True
    assert saved.source == "file"
    assert saved.revision == hashlib.sha256(payload).hexdigest()
    assert list((tmp_path / ".aflow").glob("*.tmp")) == []


def test_update_can_change_one_field_without_resetting_the_other(tmp_path: Path) -> None:
    service = ProjectSettingsService(tmp_path)
    saved = service.save(ProjectSettings(False, 4), expected_revision=service.read().revision)

    updated = service.update(
        expected_revision=saved.revision,
        max_concurrent_implementations=9,
    )

    assert updated.settings == ProjectSettings(False, 9)


def test_compare_and_swap_rejects_stale_revision(tmp_path: Path) -> None:
    first = ProjectSettingsService(tmp_path)
    second = ProjectSettingsService(tmp_path)
    first_read = first.read()
    second_read = second.read()

    committed = first.save(ProjectSettings(True, 3), expected_revision=first_read.revision)

    with pytest.raises(ProjectSettingsRevisionConflict) as raised:
        second.save(ProjectSettings(False, 8), expected_revision=second_read.revision)

    assert raised.value.current_revision == committed.revision
    assert first.read().settings == ProjectSettings(True, 3)


@pytest.mark.parametrize(
    ("auto_consume_plans", "max_concurrent_implementations"),
    [
        (1, 2),
        (False, 0),
        (True, -1),
        (True, 1.5),
        (True, 1_001),
    ],
)
def test_invalid_values_are_rejected(
    auto_consume_plans: object,
    max_concurrent_implementations: object,
) -> None:
    with pytest.raises(ProjectSettingsValidationError):
        ProjectSettings(auto_consume_plans, max_concurrent_implementations)  # type: ignore[arg-type]


def test_invalid_update_revision_is_rejected_before_writing(tmp_path: Path) -> None:
    with pytest.raises(ProjectSettingsValidationError, match="SHA-256"):
        ProjectSettingsService(tmp_path).update(
            expected_revision="not-a-revision",
            auto_consume_plans=False,
        )
    assert not (tmp_path / ".aflow").exists()


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        '{"schema_version": 0, "auto_consume_plans": true, "max_concurrent_implementations": 2}',
        '{"schema_version": 1, "auto_consume_plans": true, "max_concurrent_implementations": 0}',
        '{"schema_version": 1, "auto_consume_plans": true, "max_concurrent_implementations": 2, "extra": 1}',
        '{"schema_version": 1, "auto_consume_plans": true, "max_concurrent_implementations": 2, "max_concurrent_implementations": 3}',
    ],
)
def test_malformed_existing_settings_are_not_silently_replaced(
    tmp_path: Path,
    payload: str,
) -> None:
    settings_path = tmp_path / ".aflow" / "project-settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(payload, encoding="utf-8")
    before = settings_path.read_bytes()

    with pytest.raises(ProjectSettingsCorruptionError):
        ProjectSettingsService(tmp_path).read()

    assert settings_path.read_bytes() == before


def test_symlinked_settings_paths_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "settings.json").write_text("{}\n", encoding="utf-8")
    root = tmp_path / "project"
    root.mkdir()
    (root / ".aflow").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectSettingsPathError):
        ProjectSettingsService(root).read()


def test_symlinked_settings_file_and_lock_are_rejected(tmp_path: Path) -> None:
    directory = tmp_path / ".aflow"
    directory.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    settings_path = directory / "project-settings.json"
    settings_path.symlink_to(outside)

    with pytest.raises(ProjectSettingsPathError):
        ProjectSettingsService(tmp_path).read()

    settings_path.unlink()
    (directory / "project-settings.lock").symlink_to(outside)
    with pytest.raises(ProjectSettingsPathError):
        ProjectSettingsService(tmp_path).read()


def test_linked_worktrees_share_primary_project_settings(tmp_path: Path) -> None:
    primary = _committed_git_repo(tmp_path / "primary")
    child = tmp_path / "child-worktree"
    _git(primary, "worktree", "add", "-q", "-b", "child", str(child))

    primary_service = ProjectSettingsService(primary)
    child_service = ProjectSettingsService(child)
    saved = primary_service.save(
        ProjectSettings(False, 6),
        expected_revision=primary_service.read().revision,
    )

    assert child_service.checkout_root == child.resolve()
    assert child_service.primary_root == primary.resolve()
    assert child_service.settings_path == primary / ".aflow" / "project-settings.json"
    assert child_service.read().settings == saved.settings
    assert not (child / ".aflow" / "project-settings.json").exists()

    child_saved = child_service.update(
        expected_revision=saved.revision,
        auto_consume_plans=True,
    )
    assert primary_service.read().settings == ProjectSettings(True, 6)
    assert child_saved.revision == primary_service.read().revision


def test_external_git_metadata_worktrees_share_verified_primary_settings(
    tmp_path: Path,
) -> None:
    primary, _ = _committed_separate_git_repo(tmp_path / "primary")
    child = tmp_path / "child-worktree"
    _git(primary, "worktree", "add", "-q", "-b", "child", str(child))

    primary_service = ProjectSettingsService(primary)
    child_service = ProjectSettingsService(child)
    assert child_service.primary_root == primary.resolve()
    assert child_service.settings_path == primary_service.settings_path

    saved = primary_service.save(
        ProjectSettings(False, 6),
        expected_revision=primary_service.read().revision,
    )
    child_snapshot = child_service.read()

    assert child_snapshot.settings == saved.settings
    assert child_snapshot.revision == saved.revision
    assert not (child / ".aflow" / "project-settings.json").exists()

    child_saved = child_service.update(
        expected_revision=child_snapshot.revision,
        auto_consume_plans=True,
    )
    assert primary_service.read().settings == ProjectSettings(True, 6)
    assert child_saved.revision == primary_service.read().revision


def test_external_git_metadata_without_verified_primary_fails_closed(
    tmp_path: Path,
) -> None:
    primary, git_dir = _committed_separate_git_repo(
        tmp_path / "primary",
        configure_primary_hint=False,
    )
    child = tmp_path / "child-worktree"
    _git(primary, "worktree", "add", "-q", "-b", "child", str(child))

    primary_service = ProjectSettingsService(primary)
    saved = primary_service.save(
        ProjectSettings(False, 5),
        expected_revision=primary_service.read().revision,
    )
    primary_bytes = primary_service.settings_path.read_bytes()

    with pytest.raises(ProjectSettingsPathError):
        ProjectSettingsService(child)

    assert primary_service.settings_path.read_bytes() == primary_bytes
    assert primary_service.read().revision == saved.revision
    assert not (child / ".aflow").exists()
    assert not (git_dir.parent / ".aflow").exists()


@pytest.mark.parametrize("entry_kind", ("malformed_file", "empty_directory", "dangling_symlink"))
def test_existing_broken_git_entry_fails_closed_without_child_settings(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    primary = _committed_git_repo(tmp_path / "primary")
    primary_service = ProjectSettingsService(primary)
    saved = primary_service.save(
        ProjectSettings(False, 4),
        expected_revision=primary_service.read().revision,
    )
    primary_bytes = primary_service.settings_path.read_bytes()
    child = tmp_path / "broken-child"
    child.mkdir()
    git_entry = child / ".git"
    if entry_kind == "malformed_file":
        git_entry.write_text("gitdir: /does/not/exist\n", encoding="utf-8")
    elif entry_kind == "empty_directory":
        git_entry.mkdir()
    else:
        git_entry.symlink_to(tmp_path / "missing-git-entry")

    with pytest.raises(ProjectSettingsPathError):
        ProjectSettingsService(child)

    assert not (child / ".aflow").exists()
    assert primary_service.settings_path.read_bytes() == primary_bytes
    assert primary_service.read().revision == saved.revision


def _concurrent_save(
    root: str,
    expected_revision: str,
    auto_consume_plans: bool,
    max_concurrent_implementations: int,
    barrier: multiprocessing.synchronize.Barrier,
    results: multiprocessing.queues.Queue,
) -> None:
    try:
        barrier.wait(timeout=10)
        service = ProjectSettingsService(Path(root))
        saved = service.save(
            ProjectSettings(auto_consume_plans, max_concurrent_implementations),
            expected_revision=expected_revision,
        )
    except ProjectSettingsRevisionConflict as exc:
        results.put(("conflict", exc.current_revision))
    except BaseException as exc:  # pragma: no cover - failure is reported below
        results.put(("error", type(exc).__name__, str(exc)))
    else:
        results.put(("saved", saved.revision))


def test_independent_process_saves_have_one_winner(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    expected_revision = ProjectSettingsService(root).read().revision
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_save,
            args=(root.as_posix(), expected_revision, False, 3, barrier, results),
        ),
        context.Process(
            target=_concurrent_save,
            args=(root.as_posix(), expected_revision, True, 8, barrier, results),
        ),
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)

    assert [process.exitcode for process in processes] == [0, 0]
    outcomes = [results.get(timeout=2) for _ in processes]
    assert sorted(item[0] for item in outcomes) == ["conflict", "saved"]
    assert ProjectSettingsService(root).read().settings in {
        ProjectSettings(False, 3),
        ProjectSettings(True, 8),
    }
