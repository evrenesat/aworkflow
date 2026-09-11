"""Tests for the shared symlink installation service.

Covers the exact eleven-harness destination map and executable detection, one
deduplicated operation per shared destination, preview/confirmation flows,
canonical refresh plus absolute directory links, idempotence, legacy-copy
replacement without importing edits, hostile and dangling links, file
collisions, partial failures with unattempted reporting, destination/store
overlap rejection, and the existing CLI selection flags. The subprocess smoke
exercises the installed ``aflow`` entry point with a disposable HOME and PATH.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from importlib import resources

import aflow.skill_installer as skill_installer_module
from aflow.skill_installer import (
    BUNDLED_SKILL_NAMES,
    DEFAULT_BUNDLED_SKILL_NAMES,
    OPTIONAL_BUNDLED_SKILL_NAMES,
    InstallerError,
    InstallResult,
    SkillStore,
    build_install_plan,
    detect_auto_targets,
    discover_bundled_skills,
    install_skills,
)
from tests.test_skill_store import _package_skill_bytes

ALL_HARNESSES = (
    "claude",
    "codex",
    "copilot",
    "dsh",
    "gemini",
    "kiro-cli",
    "muse",
    "opencode",
    "pi",
    "reasonix",
    "zcode",
)

SHARED_HARNESSES = ("codex", "copilot", "dsh", "gemini", "muse", "opencode", "pi", "reasonix")


class _FakeStdin:
    def __init__(self, interactive: bool) -> None:
        self._interactive = interactive

    def isatty(self) -> bool:
        return self._interactive


def _write_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def _is_link_to(path: Path, target: Path) -> bool:
    return path.is_symlink() and os.readlink(path) == str(target)


class _patched_environ:
    """Temporarily set environment variables with exact restoration."""

    def __init__(self, **values: str) -> None:
        self._values = values
        self._previous: dict[str, str | None] = {}

    def __enter__(self) -> "_patched_environ":
        for name, value in self._values.items():
            self._previous[name] = os.environ.get(name)
            os.environ[name] = value
        return self

    def __exit__(self, *exc_info) -> None:
        for name, previous in self._previous.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous


@pytest.fixture()
def disposable_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_bundled_skill_inventory_is_sorted_full_union() -> None:
    assert BUNDLED_SKILL_NAMES == tuple(
        sorted(DEFAULT_BUNDLED_SKILL_NAMES + OPTIONAL_BUNDLED_SKILL_NAMES)
    )


def test_discover_bundled_skills_uses_package_resources() -> None:
    skills = discover_bundled_skills()
    assert tuple(skill.name for skill in skills) == DEFAULT_BUNDLED_SKILL_NAMES
    bundled_root = resources.files("aflow").joinpath("bundled_skills")
    for skill in skills:
        skill_dir = bundled_root.joinpath(skill.name)
        assert skill_dir.is_dir()
        assert skill_dir.joinpath("SKILL.md").is_file()


def test_destination_map_covers_exactly_eleven_harnesses() -> None:
    specs = skill_installer_module.SUPPORTED_HARNESS_INSTALL_SPECS
    assert [spec.harness for spec in specs] == [
        "claude",
        "codex",
        "copilot",
        "dsh",
        "gemini",
        "kiro",
        "muse",
        "opencode",
        "pi",
        "reasonix",
        "zcode",
    ]
    by_harness = {spec.harness: spec for spec in specs}
    assert by_harness["claude"].destination_template == "~/.claude/skills"
    assert by_harness["kiro"].destination_template == "~/.kiro/skills"
    assert by_harness["kiro"].executable == "kiro-cli"
    assert by_harness["zcode"].destination_template == "~/.zcode/skills"
    shared = [
        spec.harness
        for spec in specs
        if spec.destination_template == "~/.agents/skills"
    ]
    assert shared == list(SHARED_HARNESSES)


def test_detect_auto_targets_selects_installed_executables_in_map_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for executable in ("claude", "codex", "copilot", "gemini", "pi"):
        _write_executable(bin_dir / executable)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    targets = detect_auto_targets()

    assert [target.harness for target in targets] == ["claude", "codex", "copilot", "gemini", "pi"]
    assert targets[0].destination == Path("~/.claude/skills").expanduser()
    assert all(target.destination == Path("~/.agents/skills").expanduser() for target in targets[1:])


def test_detect_auto_targets_reports_every_eleven_harness_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for executable in ALL_HARNESSES:
        _write_executable(bin_dir / executable)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    targets = detect_auto_targets()

    assert [target.harness for target in targets] == [
        "claude",
        "codex",
        "copilot",
        "dsh",
        "gemini",
        "kiro",
        "muse",
        "opencode",
        "pi",
        "reasonix",
        "zcode",
    ]
    assert {target.destination for target in targets} == {
        Path("~/.claude/skills").expanduser(),
        Path("~/.agents/skills").expanduser(),
        Path("~/.kiro/skills").expanduser(),
        Path("~/.zcode/skills").expanduser(),
    }


def test_auto_install_requires_a_detected_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "empty-bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    with pytest.raises(InstallerError, match="No supported aflow harness CLIs"):
        detect_auto_targets()


def test_manual_destination_links_default_skills_into_the_store(
    disposable_home: Path, tmp_path: Path
) -> None:
    store = SkillStore(tmp_path / "store")
    destination = tmp_path / "skills"

    result = install_skills(
        destination=destination,
        yes=True,
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )

    assert isinstance(result, InstallResult)
    assert result.succeeded is True
    assert result.linked_count == len(DEFAULT_BUNDLED_SKILL_NAMES)
    for skill_name in DEFAULT_BUNDLED_SKILL_NAMES:
        link = destination / skill_name
        assert _is_link_to(link, store.root / skill_name), skill_name
        assert (link / "SKILL.md").is_file()
    for skill_name in OPTIONAL_BUNDLED_SKILL_NAMES:
        assert not (destination / skill_name).exists()
    # The canonical store was initialized with real content, not links.
    store_md = store.root / "aflow-plan" / "SKILL.md"
    assert store_md.is_file() and not store_md.is_symlink()
    assert store_md.read_bytes() == _package_skill_bytes("aflow-plan")


def test_install_preview_declares_links_and_store_without_mutating(
    disposable_home: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "skills"
    stdout = io.StringIO()

    install_skills(
        destination=destination,
        yes=False,
        stdin=_FakeStdin(True),
        input_fn=lambda _: "n",
        stdout=stdout,
        store=SkillStore(tmp_path / "store"),
    )

    text = stdout.getvalue()
    assert "Manual install mode" in text
    assert f"Total link operations: {len(DEFAULT_BUNDLED_SKILL_NAMES)}" in text
    assert f"Canonical skill store: {(tmp_path / 'store').resolve()}" in text
    assert not destination.exists()
    assert not (tmp_path / "store").exists()


def test_yes_skips_prompt(disposable_home: Path, tmp_path: Path) -> None:
    def explode(_: str) -> str:
        raise AssertionError("input should not be called when --yes is used")

    result = install_skills(
        destination=tmp_path / "skills",
        yes=True,
        stdin=_FakeStdin(False),
        input_fn=explode,
        stdout=io.StringIO(),
        store=SkillStore(tmp_path / "store"),
    )
    assert result.succeeded is True


def test_confirmation_decline_reports_cancelled(disposable_home: Path, tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    stdout = io.StringIO()

    result = install_skills(
        destination=destination,
        yes=False,
        stdin=_FakeStdin(True),
        input_fn=lambda _: "n",
        stdout=stdout,
        store=SkillStore(tmp_path / "store"),
    )

    assert result.cancelled is True
    assert result.succeeded is False
    assert not destination.exists()
    assert "Installation cancelled." in stdout.getvalue()


def test_noninteractive_without_yes_returns_clear_error(
    disposable_home: Path, tmp_path: Path
) -> None:
    with pytest.raises(InstallerError, match="rerun with --yes"):
        install_skills(
            destination=tmp_path / "skills",
            yes=False,
            stdin=_FakeStdin(False),
            stdout=io.StringIO(),
            store=SkillStore(tmp_path / "store"),
        )


def test_preflight_rejects_destination_file_collisions(
    disposable_home: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "skills"
    destination.write_text("not a directory", encoding="utf-8")

    with pytest.raises(InstallerError, match="Destination path is a file"):
        install_skills(
            destination=destination,
            yes=True,
            stdin=_FakeStdin(True),
            stdout=io.StringIO(),
            store=SkillStore(tmp_path / "store"),
        )


def test_legacy_copy_is_replaced_without_importing_its_edits(
    disposable_home: Path, tmp_path: Path
) -> None:
    store = SkillStore(tmp_path / "store")
    destination = tmp_path / "skills"
    legacy_dir = destination / "aflow-plan"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "SKILL.md").write_text("old locally edited content\n", encoding="utf-8")
    (legacy_dir / "legacy-extra.md").write_text("old supporting note\n", encoding="utf-8")
    unrelated = destination / "notes.txt"
    unrelated.write_text("keep me\n", encoding="utf-8")

    result = install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-plan",),
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )

    assert result.succeeded is True
    link = destination / "aflow-plan"
    assert _is_link_to(link, store.root / "aflow-plan")
    assert (link / "SKILL.md").read_bytes() == _package_skill_bytes("aflow-plan"), (
        "legacy edits are never imported into the canonical store"
    )
    assert not (link / "legacy-extra.md").exists()
    assert unrelated.read_text(encoding="utf-8") == "keep me\n"


def test_install_is_idempotent_and_reports_already_linked(
    disposable_home: Path, tmp_path: Path
) -> None:
    store = SkillStore(tmp_path / "store")
    destination = tmp_path / "skills"
    selection = ("aflow-plan", "aflow-merge")
    first = install_skills(
        destination=destination,
        yes=True,
        only_skills=selection,
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )
    assert first.linked_count == 2

    second = install_skills(
        destination=destination,
        yes=True,
        only_skills=selection,
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )

    assert second.succeeded is True
    assert second.linked_count == 0
    assert second.already_linked_count == 2


def test_saved_edits_are_visible_through_multiple_links_without_reinstall(
    disposable_home: Path, tmp_path: Path
) -> None:
    store = SkillStore(tmp_path / "store")
    destination = tmp_path / "skills"
    install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-plan", "aflow-manager"),
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )

    original = _package_skill_bytes("aflow-manager").decode("utf-8")
    document = store.read("aflow-manager")
    edited = original.replace(
        "Use this skill only when the AFlow engine invokes you as its interstep manager.",
        "Locally edited manager instructions for the propagation test.",
    )
    store.save("aflow-manager", edited, document.revision)

    assert (destination / "aflow-manager" / "SKILL.md").read_text(encoding="utf-8") == edited


def test_wrong_and_dangling_links_are_repaired_without_traversing(
    disposable_home: Path, tmp_path: Path
) -> None:
    store = SkillStore(tmp_path / "store")
    destination = tmp_path / "skills"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("untouched\n", encoding="utf-8")
    destination.mkdir(parents=True)
    (destination / "aflow-plan").symlink_to(outside)
    (destination / "aflow-manager").symlink_to(tmp_path / "does-not-exist")

    result = install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-plan", "aflow-manager"),
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )

    assert result.succeeded is True
    assert result.linked_count == 2
    assert _is_link_to(destination / "aflow-plan", store.root / "aflow-plan")
    assert _is_link_to(destination / "aflow-manager", store.root / "aflow-manager")
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "untouched\n"


def test_regular_file_collision_is_rejected_as_a_failed_operation(
    disposable_home: Path, tmp_path: Path
) -> None:
    store = SkillStore(tmp_path / "store")
    destination = tmp_path / "skills"
    destination.mkdir(parents=True)
    (destination / "aflow-plan").write_text("a plain file\n", encoding="utf-8")

    result = install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-plan",),
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )

    assert result.succeeded is False
    assert result.operations[0].status == "failed"
    assert result.operations[0].error_code == "file_collision"
    assert (destination / "aflow-plan").read_text(encoding="utf-8") == "a plain file\n"
    assert not (destination / "aflow-plan").is_symlink()


def test_batch_stops_on_first_failure_and_reports_unattempted(
    disposable_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SkillStore(tmp_path / "store")
    destination = tmp_path / "skills"
    real_symlink = os.symlink

    def failing_symlink(target, link_path, *args, **kwargs):
        if Path(link_path).name == "aflow-merge":
            raise OSError("simulated link failure")
        return real_symlink(target, link_path, *args, **kwargs)

    monkeypatch.setattr(skill_installer_module.os, "symlink", failing_symlink)
    result = install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-plan", "aflow-merge", "aflow-manager"),
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )
    monkeypatch.undo()

    assert result.succeeded is False
    statuses = {operation.skill: operation.status for operation in result.operations}
    assert statuses == {
        "aflow-plan": "linked",
        "aflow-merge": "failed",
        "aflow-manager": "unattempted",
    }
    failed = result.operations[1]
    assert failed.error_code == "link_failed"
    assert _is_link_to(destination / "aflow-plan", store.root / "aflow-plan")
    assert not (destination / "aflow-manager").exists()
    # Repeating the installation safely finishes it.
    retry = install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-plan", "aflow-merge", "aflow-manager"),
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )
    assert retry.succeeded is True
    assert retry.already_linked_count == 1
    assert retry.linked_count == 2


def test_displaced_directory_cleanup_failure_reports_exact_owned_path(
    disposable_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SkillStore(tmp_path / "store")
    destination = tmp_path / "skills"
    legacy = destination / "aflow-plan"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("legacy\n", encoding="utf-8")
    real_rmtree = shutil.rmtree

    def failing_rmtree(path, *args, **kwargs):
        if ".displaced." in str(path):
            raise OSError("simulated cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(skill_installer_module.shutil, "rmtree", failing_rmtree)
    result = install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-plan",),
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )
    monkeypatch.undo()

    assert result.succeeded is False
    operation = result.operations[0]
    assert operation.status == "failed"
    assert operation.error_code == "displaced_cleanup_failed"
    assert operation.displaced_path is not None
    assert ".displaced." in str(operation.displaced_path)
    assert operation.displaced_path.is_dir()
    assert (operation.displaced_path / "SKILL.md").read_text(encoding="utf-8") == "legacy\n"
    assert _is_link_to(destination / "aflow-plan", store.root / "aflow-plan")


def test_link_failure_restores_the_displaced_directory(
    disposable_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SkillStore(tmp_path / "store")
    destination = tmp_path / "skills"
    legacy = destination / "aflow-plan"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("legacy\n", encoding="utf-8")
    real_symlink = os.symlink

    def failing_symlink(target, link_path, *args, **kwargs):
        if Path(link_path).name == "aflow-plan":
            raise OSError("simulated link failure")
        return real_symlink(target, link_path, *args, **kwargs)

    monkeypatch.setattr(skill_installer_module.os, "symlink", failing_symlink)
    result = install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-plan",),
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )
    monkeypatch.undo()

    assert result.succeeded is False
    operation = result.operations[0]
    assert operation.status == "failed"
    assert "existing directory restored" in (operation.error or "")
    assert not legacy.is_symlink()
    assert (legacy / "SKILL.md").read_text(encoding="utf-8") == "legacy\n"


def test_destination_overlapping_the_store_is_rejected(
    disposable_home: Path, tmp_path: Path
) -> None:
    store = SkillStore(tmp_path / "store")

    with pytest.raises(InstallerError, match="overlaps the canonical skill store"):
        install_skills(
            destination=store.root,
            yes=True,
            stdin=_FakeStdin(True),
            stdout=io.StringIO(),
            store=store,
        )
    with pytest.raises(InstallerError, match="overlaps the canonical skill store"):
        install_skills(
            destination=store.root / "nested",
            yes=True,
            stdin=_FakeStdin(True),
            stdout=io.StringIO(),
            store=store,
        )
    with pytest.raises(InstallerError, match="overlaps the canonical skill store"):
        install_skills(
            destination=tmp_path,
            yes=True,
            stdin=_FakeStdin(True),
            stdout=io.StringIO(),
            store=SkillStore(tmp_path / "nested" / "store"),
        )


def test_canonical_refresh_error_fails_before_linking_that_skill(
    disposable_home: Path, tmp_path: Path
) -> None:
    def invalid_package(name: str) -> list[tuple[str, bytes, int]]:
        payload = (
            "---\nname: a-completely-different-skill\n"
            'description: "Mismatched frontmatter."\n---\n\nBody.\n'
        )
        return [("SKILL.md", payload.encode("utf-8"), 0o644)]

    store = SkillStore(tmp_path / "store", package_tree_loader=invalid_package)
    destination = tmp_path / "skills"

    result = install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-plan",),
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )

    assert result.succeeded is False
    assert result.refresh[0].status == "failed"
    operation = result.operations[0]
    assert operation.status == "failed"
    assert operation.error_code == "refresh_failed"
    assert not (destination / "aflow-plan").exists(), (
        "a canonical refresh error must fail before linking the skill"
    )
    assert not (destination / "aflow-plan").is_symlink()


def test_shared_destination_installs_one_operation_per_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for executable in SHARED_HARNESSES:
        _write_executable(bin_dir / executable)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store = SkillStore(tmp_path / "store")

    result = install_skills(
        yes=True,
        only_skills=("aflow-plan",),
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )

    shared = Path("~/.agents/skills").expanduser()
    assert result.succeeded is True
    assert len(result.operations) == 1, "eight harnesses share one destination operation"
    assert result.operations[0].destination == shared
    assert result.operations[0].harness == "codex", (
        "the first map-order harness represents the shared destination"
    )
    assert _is_link_to(shared / "aflow-plan", store.root / "aflow-plan")


def test_auto_install_plan_groups_shared_harness_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for executable in SHARED_HARNESSES:
        _write_executable(bin_dir / executable)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    plan = build_install_plan(store=SkillStore(tmp_path / "store"))
    preview = skill_installer_module.render_preview(plan)

    shared = str(Path("~/.agents/skills").expanduser())
    assert "codex, copilot, dsh, gemini, muse, opencode, pi, reasonix" in preview
    assert f"Total link operations: {len(DEFAULT_BUNDLED_SKILL_NAMES)}" in preview
    assert preview.count(shared) == 1


def test_only_installs_named_skill_as_link(disposable_home: Path, tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "store")
    destination = tmp_path / "skills"

    result = install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-plan",),
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )

    assert result.succeeded is True
    assert _is_link_to(destination / "aflow-plan", store.root / "aflow-plan")
    for skill_name in BUNDLED_SKILL_NAMES:
        if skill_name != "aflow-plan":
            assert not (destination / skill_name).exists()


def test_only_accepts_multiple_skills_and_deduplicates(
    disposable_home: Path, tmp_path: Path
) -> None:
    store = SkillStore(tmp_path / "store")
    destination = tmp_path / "skills"

    result = install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-plan", "aflow-merge", "aflow-plan"),
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )

    assert result.succeeded is True
    assert result.linked_count == 2
    assert _is_link_to(destination / "aflow-plan", store.root / "aflow-plan")
    assert _is_link_to(destination / "aflow-merge", store.root / "aflow-merge")


def test_only_rejects_unknown_skill(disposable_home: Path, tmp_path: Path) -> None:
    with pytest.raises(InstallerError, match="Unknown bundled skill: not-a-skill"):
        install_skills(
            destination=tmp_path / "skills",
            yes=True,
            only_skills=("not-a-skill",),
            stdin=_FakeStdin(True),
            stdout=io.StringIO(),
            store=SkillStore(tmp_path / "store"),
        )


def test_only_rejects_include_optional_combination(disposable_home: Path, tmp_path: Path) -> None:
    with pytest.raises(InstallerError, match="Cannot combine --only with --include-optional"):
        install_skills(
            destination=tmp_path / "skills",
            yes=True,
            only_skills=("aflow-plan",),
            include_optional=True,
            stdin=_FakeStdin(True),
            stdout=io.StringIO(),
            store=SkillStore(tmp_path / "store"),
        )


def test_only_rejects_empty_list(disposable_home: Path, tmp_path: Path) -> None:
    with pytest.raises(InstallerError, match="--only requires at least one skill name"):
        install_skills(
            destination=tmp_path / "skills",
            yes=True,
            only_skills=(),
            stdin=_FakeStdin(True),
            stdout=io.StringIO(),
            store=SkillStore(tmp_path / "store"),
        )


def test_include_optional_links_supporting_resources_through_the_link(
    disposable_home: Path, tmp_path: Path
) -> None:
    store = SkillStore(tmp_path / "store")
    destination = tmp_path / "skills"

    result = install_skills(
        destination=destination,
        yes=True,
        include_optional=True,
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )

    assert result.succeeded is True
    assert result.linked_count == len(BUNDLED_SKILL_NAMES)
    assistant = destination / "aflow-assistant"
    assert _is_link_to(assistant, store.root / "aflow-assistant")
    assert (assistant / "SKILL.md").is_file()
    assert (assistant / "references" / "engine-map.md").is_file()
    assert (assistant / "scripts" / "analyze_runs.py").is_file()
    guard = destination / "aflow-guard-development-run"
    assert (guard / "agents" / "openai.yaml").is_file()
    assert (guard / "scripts" / "aflow_guard_issue.py").is_file()
    assert (guard / "scripts" / "aflow_guard_recovery.py").is_file()
    assert (guard / "scripts" / "aflow_guard_report_input.py").is_file()
    assert (guard / "scripts" / "aflow_guard_report.py").is_file()
    assert (guard / "references" / "report-input.md").is_file()
    assert (guard / "scripts" / "assets" / "DejaVuSans.ttf").is_file()
    assert (guard / "scripts" / "assets" / "DejaVuSans-Bold.ttf").is_file()
    assert not (guard / "references" / "reporting-and-email.md").exists()


def test_default_install_excludes_optional_skills(disposable_home: Path, tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "store")
    destination = tmp_path / "skills"

    result = install_skills(
        destination=destination,
        yes=True,
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )

    assert result.linked_count == len(DEFAULT_BUNDLED_SKILL_NAMES)
    for skill_name in OPTIONAL_BUNDLED_SKILL_NAMES:
        assert not (destination / skill_name).exists()


def test_old_opencode_location_is_never_migrated(disposable_home: Path, tmp_path: Path) -> None:
    legacy_opencode = Path("~/.config/opencode/skills").expanduser()
    legacy_skill = legacy_opencode / "aflow-plan"
    legacy_skill.mkdir(parents=True)
    (legacy_skill / "SKILL.md").write_text("old copy\n", encoding="utf-8")
    store = SkillStore(tmp_path / "store")
    destination = tmp_path / "skills"

    install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-plan",),
        stdin=_FakeStdin(True),
        stdout=io.StringIO(),
        store=store,
    )

    assert (legacy_skill / "SKILL.md").read_text(encoding="utf-8") == "old copy\n"
    assert not _is_link_to(legacy_skill, store.root / "aflow-plan")


def _installed_aflow_source() -> Path | None:
    executable = shutil.which("aflow")
    if executable is None:
        return None
    script = Path(executable)
    try:
        shebang = script.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, UnicodeDecodeError):
        return None
    if not shebang.startswith("#!"):
        return None
    python = shebang[2:].strip()
    try:
        completed = subprocess.run(
            [python, "-c", "import aflow; print(aflow.__file__)"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return Path(completed.stdout.strip()).resolve()


def test_installed_entry_point_smoke_links_all_eleven_harness_destinations(
    tmp_path: Path,
) -> None:
    """Disposable-HOME CLI smoke through the installed ``aflow`` executable.

    Skipped only when no ``aflow`` executable is installed at all; when one is
    installed from a different source checkout the test fails loudly rather
    than validating stale code. Link targets and counts are asserted, never
    harness invocations.
    """
    import aflow

    executable = shutil.which("aflow")
    if executable is None:
        pytest.skip("no installed aflow entry point on PATH")
    source = _installed_aflow_source()
    expected_source = Path(aflow.__file__).resolve()
    assert source == expected_source, (
        f"installed aflow entry point imports {source}, not this checkout "
        f"({expected_source}); rerun `uv tool install -e . --force` from this worktree"
    )

    home = tmp_path / "smoke-home"
    home.mkdir()
    bin_dir = tmp_path / "smoke-bin"
    bin_dir.mkdir()
    for executable_name in ALL_HARNESSES:
        _write_executable(bin_dir / executable_name)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = str(bin_dir)

    def run_install(*arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [executable, "install-skills", *arguments],
            capture_output=True,
            text=True,
            env=env,
            timeout=600,
        )

    first = run_install("--yes")
    assert first.returncode == 0, first.stdout + first.stderr
    store_root = home / ".config" / "aflow" / "skills"
    destinations = [
        home / ".claude" / "skills",
        home / ".agents" / "skills",
        home / ".kiro" / "skills",
        home / ".zcode" / "skills",
    ]
    for destination in destinations:
        for skill_name in DEFAULT_BUNDLED_SKILL_NAMES:
            assert _is_link_to(
                destination / skill_name, store_root / skill_name
            ), destination / skill_name
        assert not (destination / "aflow-assistant").exists()
    assert len(destinations) * len(DEFAULT_BUNDLED_SKILL_NAMES) == 52

    # Saved canonical Markdown is identical through every created link.
    packaged = _package_skill_bytes("aflow-plan")
    for destination in destinations:
        assert (destination / "aflow-plan" / "SKILL.md").read_bytes() == packaged

    only = run_install("--yes", "--only", "aflow-plan")
    assert only.returncode == 0, only.stdout + only.stderr
    assert "0 links created, 4 already linked" in only.stdout

    optional = run_install("--yes", "--include-optional")
    assert optional.returncode == 0, optional.stdout + optional.stderr
    for destination in destinations:
        assert _is_link_to(destination / "aflow-assistant", store_root / "aflow-assistant")
        assert (destination / "aflow-assistant" / "references" / "engine-map.md").is_file()
