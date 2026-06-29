from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
try:
    from importlib.resources.abc import Traversable
except ImportError:
    from importlib.abc import Traversable
from pathlib import Path
import shutil
import sys
from typing import Callable


@dataclass(frozen=True)
class BundledSkillMetadata:
    name: str
    default: bool


BUNDLED_SKILL_METADATA = (
    BundledSkillMetadata(name="aflow-plan", default=True),
    BundledSkillMetadata(name="aflow-execute-plan", default=True),
    BundledSkillMetadata(name="aflow-execute-checkpoint", default=True),
    BundledSkillMetadata(name="aflow-review-squash", default=True),
    BundledSkillMetadata(name="aflow-review-checkpoint", default=True),
    BundledSkillMetadata(name="aflow-review-final", default=True),
    BundledSkillMetadata(name="aflow-merge", default=True),
    BundledSkillMetadata(name="aflow-init-repo", default=True),
    BundledSkillMetadata(name="aflow-harness-recovery-lead", default=True),
    BundledSkillMetadata(name="aflow-assistant", default=False),
)

DEFAULT_BUNDLED_SKILL_NAMES = tuple(
    meta.name for meta in BUNDLED_SKILL_METADATA if meta.default
)

OPTIONAL_BUNDLED_SKILL_NAMES = tuple(
    meta.name for meta in BUNDLED_SKILL_METADATA if not meta.default
)

BUNDLED_SKILL_NAMES = tuple(sorted(meta.name for meta in BUNDLED_SKILL_METADATA))


@dataclass(frozen=True)
class HarnessInstallSpec:
    harness: str
    executable: str
    destination_template: str


SUPPORTED_HARNESS_INSTALL_SPECS = (
    HarnessInstallSpec("claude", "claude", "~/.claude/skills"),
    HarnessInstallSpec("codex", "codex", "~/.agents/skills"),
    HarnessInstallSpec("copilot", "copilot", "~/.agents/skills"),
    HarnessInstallSpec("gemini", "gemini", "~/.agents/skills"),
    HarnessInstallSpec("kiro", "kiro-cli", "~/.kiro/skills"),
    HarnessInstallSpec("opencode", "opencode", "~/.config/opencode/skills"),
    HarnessInstallSpec("pi", "pi", "~/.agents/skills"),
)


class InstallerError(RuntimeError):
    pass


@dataclass(frozen=True)
class BundledSkill:
    name: str
    source: Traversable


@dataclass(frozen=True)
class InstallTarget:
    harness: str
    executable: str
    destination: Path


@dataclass(frozen=True)
class PreviewRow:
    harness: str
    destination: Path
    skill_name: str


@dataclass(frozen=True)
class InstallPlan:
    mode: str
    skills: tuple[BundledSkill, ...]
    targets: tuple[InstallTarget, ...]
    preview_rows: tuple[PreviewRow, ...]


def bundled_skills_root() -> Traversable:
    return resources.files("aflow").joinpath("bundled_skills")


def discover_bundled_skills(
    only_skills: tuple[str, ...] | None = None,
    include_optional: bool = False,
) -> tuple[BundledSkill, ...]:
    root = bundled_skills_root()
    skills: list[BundledSkill] = []
    missing: list[str] = []

    skill_names_to_discover: tuple[str, ...]
    if only_skills is not None:
        skill_names_to_discover = only_skills
    else:
        skill_names_to_discover = BUNDLED_SKILL_NAMES if include_optional else DEFAULT_BUNDLED_SKILL_NAMES

    for skill_name in skill_names_to_discover:
        skill_dir = root.joinpath(skill_name)
        skill_md = skill_dir.joinpath("SKILL.md")
        if not skill_dir.is_dir() or not skill_md.is_file():
            missing.append(skill_name)
            continue
        skills.append(BundledSkill(name=skill_name, source=skill_dir))
    if missing:
        missing_text = ", ".join(missing)
        raise InstallerError(f"Missing bundled skill resources: {missing_text}")
    return tuple(skills)


def detect_auto_targets() -> tuple[InstallTarget, ...]:
    targets: list[InstallTarget] = []
    for spec in SUPPORTED_HARNESS_INSTALL_SPECS:
        if shutil.which(spec.executable) is None:
            continue
        targets.append(
            InstallTarget(
                harness=spec.harness,
                executable=spec.executable,
                destination=Path(spec.destination_template).expanduser(),
            )
        )
    if not targets:
        raise InstallerError(
            "No supported aflow harness CLIs were found on PATH. "
            "Rerun with --yes and a destination path, or install a supported harness first."
        )
    return tuple(targets)


def build_install_plan(
    destination: str | Path | None = None,
    *,
    only_skills: tuple[str, ...] | None = None,
    include_optional: bool = False,
) -> InstallPlan:
    skills = discover_bundled_skills(only_skills=only_skills, include_optional=include_optional)
    if destination is None:
        targets = detect_auto_targets()
        mode = "auto"
    else:
        targets = (InstallTarget(harness="manual", executable="", destination=Path(destination).expanduser()),)
        mode = "manual"
    preview_rows = tuple(
        PreviewRow(harness=target.harness, destination=target.destination, skill_name=skill.name)
        for target in targets
        for skill in skills
    )
    return InstallPlan(mode=mode, skills=skills, targets=targets, preview_rows=preview_rows)


def _unique_destinations(targets: tuple[InstallTarget, ...]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for target in targets:
        if target.destination not in seen:
            seen.add(target.destination)
            result.append(target.destination)
    return result


def render_preview(plan: InstallPlan) -> str:
    lines: list[str] = []
    if plan.mode == "auto":
        lines.append("Auto install mode")
        lines.append("Detected harness destinations:")
        dest_to_harnesses: dict[Path, list[str]] = {}
        for target in plan.targets:
            dest_to_harnesses.setdefault(target.destination, []).append(target.harness)
        for dest, harnesses in dest_to_harnesses.items():
            lines.append(f"- {', '.join(harnesses)} -> {dest}")
    else:
        lines.append("Manual install mode")
        lines.append(f"Destination root: {plan.targets[0].destination}")
    lines.append("Bundled skills:")
    for skill in plan.skills:
        lines.append(f"- {skill.name}")
    unique_dests = _unique_destinations(plan.targets)
    total = len(unique_dests) * len(plan.skills)
    lines.append(f"Total copy operations: {total}")
    return "\n".join(lines)


def _format_success_summary(plan: InstallPlan, copied_count: int) -> str:
    target_count = len(plan.targets)
    target_label = "destination" if target_count == 1 else "destinations"
    skill_label = "skill" if len(plan.skills) == 1 else "skills"
    return f"Installed {len(plan.skills)} bundled {skill_label} into {target_count} {target_label} ({copied_count} copies total)."


def _ensure_valid_targets(plan: InstallPlan) -> None:
    if not plan.targets:
        raise InstallerError("No install targets selected.")
    for skill in plan.skills:
        skill_md = skill.source.joinpath("SKILL.md")
        if not skill.source.is_dir() or not skill_md.is_file():
            raise InstallerError(f"Bundled skill '{skill.name}' is missing SKILL.md.")
    for target in plan.targets:
        if target.destination.exists() and not target.destination.is_dir():
            raise InstallerError(f"Destination path is a file: {target.destination}")
        for ancestor in target.destination.parents:
            if ancestor.exists() and not ancestor.is_dir():
                raise InstallerError(f"Destination path has a file in its parent chain: {ancestor}")
        for skill in plan.skills:
            skill_destination = target.destination / skill.name
            if skill_destination.exists() and not skill_destination.is_dir():
                raise InstallerError(
                    f"Destination path collides with an existing file: {skill_destination}"
                )


def _copy_traversable_tree(source: Traversable, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        child_destination = destination / child.name
        try:
            if child.is_dir():
                _copy_traversable_tree(child, child_destination)
                continue
            child_destination.parent.mkdir(parents=True, exist_ok=True)
            with child.open("rb") as source_file, child_destination.open("wb") as destination_file:
                shutil.copyfileobj(source_file, destination_file)
        except OSError as exc:
            raise InstallerError(f"Failed to copy '{child_destination}': {exc}") from exc


def _copy_plan(plan: InstallPlan) -> int:
    unique_dests = _unique_destinations(plan.targets)
    copied = 0
    for dest in unique_dests:
        dest.mkdir(parents=True, exist_ok=True)
    for dest in unique_dests:
        for skill in plan.skills:
            try:
                _copy_traversable_tree(skill.source, dest / skill.name)
            except InstallerError as exc:
                raise InstallerError(f"Failed while copying into {dest}: {exc}") from exc
            copied += 1
    return copied


def _validate_selection(
    only_skills: tuple[str, ...] | None,
    include_optional: bool,
) -> None:
    if only_skills is not None and include_optional:
        raise InstallerError("Cannot combine --only with --include-optional.")
    if only_skills is not None and len(only_skills) == 0:
        raise InstallerError("--only requires at least one skill name.")
    if only_skills is not None:
        valid_names = {meta.name for meta in BUNDLED_SKILL_METADATA}
        for skill_name in only_skills:
            if skill_name not in valid_names:
                raise InstallerError(
                    f"Unknown bundled skill: {skill_name}. "
                    f"Valid skills are: {', '.join(sorted(valid_names))}"
                )


def install_skills(
    destination: str | Path | None = None,
    *,
    yes: bool = False,
    only_skills: tuple[str, ...] | None = None,
    include_optional: bool = False,
    stdin=None,
    input_fn: Callable[[str], str] = input,
    stdout=None,
) -> int:
    if only_skills is not None:
        seen: set[str] = set()
        deduplicated: list[str] = []
        for skill in only_skills:
            if skill not in seen:
                seen.add(skill)
                deduplicated.append(skill)
        only_skills = tuple(deduplicated)
    _validate_selection(only_skills, include_optional)
    if stdin is None:
        stdin = sys.stdin
    if stdout is None:
        stdout = sys.stdout
    plan = build_install_plan(destination, only_skills=only_skills, include_optional=include_optional)
    _ensure_valid_targets(plan)
    print(render_preview(plan), file=stdout)
    if not yes:
        if not stdin.isatty():
            raise InstallerError("stdin is not interactive, rerun with --yes.")
        response = input_fn("Proceed with installation? [y/N]: ").strip().lower()
        if response not in {"y", "yes"}:
            print("Installation cancelled.", file=stdout)
            return 0
    copied_count = _copy_plan(plan)
    print(_format_success_summary(plan, copied_count), file=stdout)
    return copied_count
