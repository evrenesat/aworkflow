from __future__ import annotations

import io
from pathlib import Path

import pytest
from importlib import resources

from aflow.skill_installer import (
    BUNDLED_SKILL_NAMES,
    DEFAULT_BUNDLED_SKILL_NAMES,
    OPTIONAL_BUNDLED_SKILL_NAMES,
    InstallerError,
    build_install_plan,
    detect_auto_targets,
    discover_bundled_skills,
    install_skills,
)


class _FakeStdin:
    def __init__(self, interactive: bool) -> None:
        self._interactive = interactive

    def isatty(self) -> bool:
        return self._interactive


def _write_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


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


def test_detect_auto_targets_selects_installed_executables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for executable in ("claude", "codex", "copilot", "gemini", "pi"):
        _write_executable(bin_dir / executable)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    targets = detect_auto_targets()

    assert [target.harness for target in targets] == ["claude", "codex", "copilot", "gemini", "pi"]
    assert targets[0].destination == Path("~/.claude/skills").expanduser()
    assert targets[1].destination == Path("~/.agents/skills").expanduser()
    assert targets[2].destination == Path("~/.agents/skills").expanduser()
    assert targets[3].destination == Path("~/.agents/skills").expanduser()
    assert targets[4].destination == Path("~/.agents/skills").expanduser()


def test_manual_destination_installs_default_child_directories(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    stdout = io.StringIO()

    copied = install_skills(destination=destination, yes=True, stdin=_FakeStdin(True), stdout=stdout)

    assert copied == 10
    for skill_name in DEFAULT_BUNDLED_SKILL_NAMES:
        skill_dir = destination / skill_name
        assert skill_dir.is_dir()
        assert skill_dir.joinpath("SKILL.md").is_file()
    for skill_name in OPTIONAL_BUNDLED_SKILL_NAMES:
        skill_dir = destination / skill_name
        assert not skill_dir.exists()
    assert "Manual install mode" in stdout.getvalue()
    assert "Total copy operations: 10" in stdout.getvalue()


def test_yes_skips_prompt(tmp_path: Path) -> None:
    destination = tmp_path / "skills"

    def explode(_: str) -> str:
        raise AssertionError("input should not be called when --yes is used")

    copied = install_skills(
        destination=destination,
        yes=True,
        stdin=_FakeStdin(False),
        input_fn=explode,
        stdout=io.StringIO(),
    )

    assert copied == len(DEFAULT_BUNDLED_SKILL_NAMES)


def test_confirmation_decline_performs_no_copies(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    stdout = io.StringIO()

    copied = install_skills(
        destination=destination,
        yes=False,
        stdin=_FakeStdin(True),
        input_fn=lambda _: "n",
        stdout=stdout,
    )

    assert copied == 0
    assert not destination.exists()
    assert "Installation cancelled." in stdout.getvalue()


def test_noninteractive_without_yes_returns_clear_error(tmp_path: Path) -> None:
    destination = tmp_path / "skills"

    with pytest.raises(InstallerError, match="rerun with --yes"):
        install_skills(destination=destination, yes=False, stdin=_FakeStdin(False), stdout=io.StringIO())


def test_preflight_rejects_destination_file_collisions(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    destination.write_text("not a directory", encoding="utf-8")

    with pytest.raises(InstallerError, match="Destination path is a file"):
        install_skills(destination=destination, yes=True, stdin=_FakeStdin(True), stdout=io.StringIO())


def test_overwrite_in_place_updates_existing_skill_files_without_pruning_extras(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    existing_skill_dir = destination / "aflow-plan"
    existing_skill_dir.mkdir(parents=True, exist_ok=True)
    existing_skill_file = existing_skill_dir / "SKILL.md"
    existing_skill_file.write_text("old content\n", encoding="utf-8")
    unrelated_file = destination / "notes.txt"
    unrelated_file.write_text("keep me\n", encoding="utf-8")

    copied = install_skills(destination=destination, yes=True, stdin=_FakeStdin(True), stdout=io.StringIO())

    assert copied == len(DEFAULT_BUNDLED_SKILL_NAMES)
    packaged_skill = resources.files("aflow").joinpath("bundled_skills", "aflow-plan", "SKILL.md").read_text(encoding="utf-8")
    assert existing_skill_file.read_text(encoding="utf-8") == packaged_skill
    assert unrelated_file.read_text(encoding="utf-8") == "keep me\n"


def test_auto_install_plan_uses_shared_agents_directory_for_codex_copilot_gemini_and_pi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for executable in ("codex", "copilot", "gemini", "pi"):
        _write_executable(bin_dir / executable)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    plan = build_install_plan()

    assert [target.harness for target in plan.targets] == ["codex", "copilot", "gemini", "pi"]
    assert plan.targets[0].destination == Path("~/.agents/skills").expanduser()
    assert plan.targets[1].destination == Path("~/.agents/skills").expanduser()
    assert plan.targets[2].destination == Path("~/.agents/skills").expanduser()
    assert plan.targets[3].destination == Path("~/.agents/skills").expanduser()


def test_auto_install_codex_copilot_gemini_and_pi_grouped_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from aflow.skill_installer import render_preview
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for executable in ("codex", "copilot", "gemini", "pi"):
        _write_executable(bin_dir / executable)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    plan = build_install_plan()
    preview = render_preview(plan)

    expanded_dest = str(Path("~/.agents/skills").expanduser())
    assert "codex, copilot, gemini, pi" in preview
    assert "Total copy operations: 10" in preview
    assert preview.count(expanded_dest) == 1


def test_auto_install_codex_copilot_gemini_and_pi_copies_only_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for executable in ("codex", "copilot", "gemini", "pi"):
        _write_executable(bin_dir / executable)
    monkeypatch.setenv("PATH", str(bin_dir))
    agents_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(agents_home))

    copied = install_skills(yes=True, stdin=_FakeStdin(True), stdout=__import__('io').StringIO())

    assert copied == 10
    shared_dest = Path("~/.agents/skills").expanduser()
    for skill_name in DEFAULT_BUNDLED_SKILL_NAMES:
        assert (shared_dest / skill_name).is_dir()
        assert (shared_dest / skill_name / "SKILL.md").is_file()
    for skill_name in OPTIONAL_BUNDLED_SKILL_NAMES:
        assert not (shared_dest / skill_name).exists()


def test_default_install_excludes_optional_skills(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    stdout = io.StringIO()

    copied = install_skills(destination=destination, yes=True, stdin=_FakeStdin(True), stdout=stdout)

    assert copied == 10
    for skill_name in DEFAULT_BUNDLED_SKILL_NAMES:
        skill_dir = destination / skill_name
        assert skill_dir.is_dir()
        assert skill_dir.joinpath("SKILL.md").is_file()
    for skill_name in OPTIONAL_BUNDLED_SKILL_NAMES:
        skill_dir = destination / skill_name
        assert not skill_dir.exists()


def test_include_optional_includes_optional_skills(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    stdout = io.StringIO()

    copied = install_skills(
        destination=destination,
        yes=True,
        include_optional=True,
        stdin=_FakeStdin(True),
        stdout=stdout,
    )

    assert copied == 11
    for skill_name in BUNDLED_SKILL_NAMES:
        skill_dir = destination / skill_name
        assert skill_dir.is_dir()
        assert skill_dir.joinpath("SKILL.md").is_file()


def test_only_installs_named_skill(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    stdout = io.StringIO()

    copied = install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-plan",),
        stdin=_FakeStdin(True),
        stdout=stdout,
    )

    assert copied == 1
    assert (destination / "aflow-plan").is_dir()
    assert (destination / "aflow-plan" / "SKILL.md").is_file()
    for skill_name in BUNDLED_SKILL_NAMES:
        if skill_name != "aflow-plan":
            assert not (destination / skill_name).exists()


def test_only_accepts_multiple_skills(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    stdout = io.StringIO()

    copied = install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-plan", "aflow-merge"),
        stdin=_FakeStdin(True),
        stdout=stdout,
    )

    assert copied == 2
    assert (destination / "aflow-plan").is_dir()
    assert (destination / "aflow-merge").is_dir()
    for skill_name in BUNDLED_SKILL_NAMES:
        if skill_name not in ("aflow-plan", "aflow-merge"):
            assert not (destination / skill_name).exists()


def test_only_deduplicates_preserving_order(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    stdout = io.StringIO()

    copied = install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-plan", "aflow-merge", "aflow-plan"),
        stdin=_FakeStdin(True),
        stdout=stdout,
    )

    assert copied == 2
    assert (destination / "aflow-plan").is_dir()
    assert (destination / "aflow-merge").is_dir()


def test_only_rejects_unknown_skill(tmp_path: Path) -> None:
    destination = tmp_path / "skills"

    with pytest.raises(InstallerError, match="Unknown bundled skill: not-a-skill"):
        install_skills(
            destination=destination,
            yes=True,
            only_skills=("not-a-skill",),
            stdin=_FakeStdin(True),
            stdout=io.StringIO(),
        )


def test_only_rejects_include_optional_combination(tmp_path: Path) -> None:
    destination = tmp_path / "skills"

    with pytest.raises(InstallerError, match="Cannot combine --only with --include-optional"):
        install_skills(
            destination=destination,
            yes=True,
            only_skills=("aflow-plan",),
            include_optional=True,
            stdin=_FakeStdin(True),
            stdout=io.StringIO(),
        )


def test_only_rejects_empty_list(tmp_path: Path) -> None:
    destination = tmp_path / "skills"

    with pytest.raises(InstallerError, match="--only requires at least one skill name"):
        install_skills(
            destination=destination,
            yes=True,
            only_skills=(),
            stdin=_FakeStdin(True),
            stdout=io.StringIO(),
        )


def test_discover_bundled_skills_respects_selection(tmp_path: Path) -> None:
    skills = discover_bundled_skills()
    assert tuple(skill.name for skill in skills) == DEFAULT_BUNDLED_SKILL_NAMES

    skills_with_optional = discover_bundled_skills(include_optional=True)
    assert tuple(skill.name for skill in skills_with_optional) == BUNDLED_SKILL_NAMES

    only_one = discover_bundled_skills(only_skills=("aflow-plan",))
    assert tuple(skill.name for skill in only_one) == ("aflow-plan",)


def test_build_install_plan_respects_selection(tmp_path: Path) -> None:
    default_plan = build_install_plan(tmp_path / "dest")
    assert len(default_plan.skills) == 10
    assert tuple(skill.name for skill in default_plan.skills) == DEFAULT_BUNDLED_SKILL_NAMES

    optional_plan = build_install_plan(tmp_path / "dest2", include_optional=True)
    assert len(optional_plan.skills) == 11
    assert tuple(skill.name for skill in optional_plan.skills) == BUNDLED_SKILL_NAMES

    only_plan = build_install_plan(tmp_path / "dest3", only_skills=("aflow-merge",))
    assert len(only_plan.skills) == 1
    assert only_plan.skills[0].name == "aflow-merge"


def test_only_aflow_assistant_copies_bundled_resources(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    stdout = io.StringIO()

    copied = install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-assistant",),
        stdin=_FakeStdin(True),
        stdout=stdout,
    )

    assert copied == 1
    assistant_dir = destination / "aflow-assistant"
    assert assistant_dir.is_dir()
    assert (assistant_dir / "SKILL.md").is_file()
    assert (assistant_dir / "references" / "engine-map.md").is_file()
    assert (assistant_dir / "scripts" / "analyze_runs.py").is_file()
    assert (assistant_dir / "references").is_dir()
    assert (assistant_dir / "scripts").is_dir()


def test_include_optional_copies_aflow_assistant_bundled_resources(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    stdout = io.StringIO()

    copied = install_skills(
        destination=destination,
        yes=True,
        include_optional=True,
        stdin=_FakeStdin(True),
        stdout=stdout,
    )

    assert copied == 11
    assistant_dir = destination / "aflow-assistant"
    assert assistant_dir.is_dir()
    assert (assistant_dir / "SKILL.md").is_file()
    assert (assistant_dir / "references" / "engine-map.md").is_file()
    assert (assistant_dir / "scripts" / "analyze_runs.py").is_file()


def test_recursive_copy_preserves_subdirectories_for_all_skills(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    stdout = io.StringIO()

    copied = install_skills(
        destination=destination,
        yes=True,
        only_skills=("aflow-assistant", "aflow-plan"),
        stdin=_FakeStdin(True),
        stdout=stdout,
    )

    assert copied == 2
    assistant_dir = destination / "aflow-assistant"
    assert (assistant_dir / "references").is_dir()
    assert (assistant_dir / "scripts").is_dir()
    assert (assistant_dir / "references" / "engine-map.md").is_file()
    assert (assistant_dir / "scripts" / "analyze_runs.py").is_file()
    plan_dir = destination / "aflow-plan"
    assert (plan_dir / "SKILL.md").is_file()
