"""Shared bundled-skill installation service.

One service backs the ``aflow install-skills`` CLI and later callers: it
refreshes the selected bundled skills in the canonical account-level store
(``aflow.skill_store``) and then links each selected skill into every detected
or explicit harness destination with one absolute directory symlink per
(destination, skill) pair. Harnesses that share a destination (eight of them
share ``~/.agents/skills``) produce one shared operation per selected skill,
never one per harness. Nothing copies skill trees anymore: a saved skill edit
is immediately visible through every existing link.

Installations are previewable and idempotent. A batch stops on the first
mutation failure; completed, failed, and unattempted operations are reported
in a structured :class:`InstallResult` with bounded error codes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import stat
import sys
from typing import Callable

from .skill_catalog import (
    BundledSkill,
    BundledSkillMetadata,
    BUNDLED_SKILL_METADATA,
    BUNDLED_SKILL_NAMES,
    DEFAULT_BUNDLED_SKILL_NAMES,
    OPTIONAL_BUNDLED_SKILL_NAMES,
    SkillCatalogError,
    bundled_skills_root,
    validate_bundled_skill_name,
)
from .skill_catalog import discover_bundled_skills as _discover_bundled_skills
from .skill_store import SkillRefreshResult, SkillStore

__all__ = [
    "BundledSkill",
    "BundledSkillMetadata",
    "BUNDLED_SKILL_METADATA",
    "BUNDLED_SKILL_NAMES",
    "DEFAULT_BUNDLED_SKILL_NAMES",
    "OPTIONAL_BUNDLED_SKILL_NAMES",
    "HarnessInstallSpec",
    "InstallPlan",
    "InstallResult",
    "InstallerError",
    "InstallTarget",
    "LinkOperationResult",
    "PreviewRow",
    "SUPPORTED_HARNESS_INSTALL_SPECS",
    "build_install_plan",
    "bundled_skills_root",
    "detect_auto_targets",
    "discover_bundled_skills",
    "install_skills",
    "render_install_result",
    "render_preview",
]


@dataclass(frozen=True)
class HarnessInstallSpec:
    harness: str
    executable: str
    destination_template: str


# The exact harness map (Critical Invariants): eleven harnesses, eight of
# which share `~/.agents/skills`. Detection uses the harness CLI's executable
# name, including `kiro-cli` for kiro.
SUPPORTED_HARNESS_INSTALL_SPECS = (
    HarnessInstallSpec("claude", "claude", "~/.claude/skills"),
    HarnessInstallSpec("codex", "codex", "~/.agents/skills"),
    HarnessInstallSpec("copilot", "copilot", "~/.agents/skills"),
    HarnessInstallSpec("dsh", "dsh", "~/.agents/skills"),
    HarnessInstallSpec("gemini", "gemini", "~/.agents/skills"),
    HarnessInstallSpec("kiro", "kiro-cli", "~/.kiro/skills"),
    HarnessInstallSpec("muse", "muse", "~/.agents/skills"),
    HarnessInstallSpec("opencode", "opencode", "~/.agents/skills"),
    HarnessInstallSpec("pi", "pi", "~/.agents/skills"),
    HarnessInstallSpec("reasonix", "reasonix", "~/.agents/skills"),
    HarnessInstallSpec("zcode", "zcode", "~/.zcode/skills"),
)

STORE_OVERLAP_ERROR = "destination_overlaps_store"
REFRESH_ERROR = "refresh_failed"
LINK_ERROR = "link_failed"
FILE_COLLISION_ERROR = "file_collision"
DISPLACED_RENAME_ERROR = "displaced_rename_failed"
DISPLACED_CLEANUP_ERROR = "displaced_cleanup_failed"

LINKED = "linked"
ALREADY_LINKED = "already_linked"
FAILED = "failed"
UNATTEMPTED = "unattempted"


class InstallerError(RuntimeError):
    pass


def discover_bundled_skills(
    only_skills: tuple[str, ...] | None = None,
    include_optional: bool = False,
) -> tuple[BundledSkill, ...]:
    """Discover bundled skills, keeping the installer's bounded error type."""
    try:
        return _discover_bundled_skills(only_skills=only_skills, include_optional=include_optional)
    except SkillCatalogError as exc:
        raise InstallerError(str(exc)) from exc


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
    store_root: Path


@dataclass(frozen=True)
class LinkOperationResult:
    """Structured outcome of one (destination, skill) link operation."""

    harness: str
    skill: str
    destination: Path
    status: str  # linked | already_linked | failed | unattempted
    error_code: str | None = None
    error: str | None = None
    displaced_path: Path | None = None


@dataclass(frozen=True)
class InstallResult:
    """Structured shared-service result of one installation run."""

    mode: str
    store_root: Path
    cancelled: bool
    refresh: tuple[SkillRefreshResult, ...]
    operations: tuple[LinkOperationResult, ...]

    @property
    def succeeded(self) -> bool:
        if self.cancelled:
            return False
        return all(
            operation.status in {LINKED, ALREADY_LINKED} for operation in self.operations
        )

    @property
    def linked_count(self) -> int:
        return sum(1 for operation in self.operations if operation.status == LINKED)

    @property
    def already_linked_count(self) -> int:
        return sum(1 for operation in self.operations if operation.status == ALREADY_LINKED)

    @property
    def failed_count(self) -> int:
        return sum(1 for operation in self.operations if operation.status == FAILED)

    @property
    def unattempted_count(self) -> int:
        return sum(1 for operation in self.operations if operation.status == UNATTEMPTED)


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


def _reject_store_overlap(destination: Path, store_root: Path) -> None:
    destination_resolved = destination.expanduser().resolve()
    if (
        destination_resolved == store_root
        or store_root in destination_resolved.parents
        or destination_resolved in store_root.parents
    ):
        raise InstallerError(
            f"Destination overlaps the canonical skill store: {destination}"
        )


def _validate_selection(
    only_skills: tuple[str, ...] | None,
    include_optional: bool,
) -> None:
    if only_skills is not None and include_optional:
        raise InstallerError("Cannot combine --only with --include-optional.")
    if only_skills is not None and len(only_skills) == 0:
        raise InstallerError("--only requires at least one skill name.")
    if only_skills is not None:
        for skill_name in only_skills:
            try:
                validate_bundled_skill_name(skill_name)
            except SkillCatalogError as exc:
                raise InstallerError(str(exc)) from exc


def build_install_plan(
    destination: str | Path | None = None,
    *,
    only_skills: tuple[str, ...] | None = None,
    include_optional: bool = False,
    store: SkillStore | None = None,
) -> InstallPlan:
    store = store or SkillStore()
    _validate_selection(only_skills, include_optional)
    skills = discover_bundled_skills(only_skills=only_skills, include_optional=include_optional)
    if destination is None:
        targets = detect_auto_targets()
        mode = "auto"
    else:
        targets = (
            InstallTarget(
                harness="manual",
                executable="",
                destination=Path(destination).expanduser(),
            ),
        )
        mode = "manual"
    for target in targets:
        _reject_store_overlap(target.destination, store.root)
    preview_rows = tuple(
        PreviewRow(harness=target.harness, destination=target.destination, skill_name=skill.name)
        for target in targets
        for skill in skills
    )
    return InstallPlan(
        mode=mode,
        skills=skills,
        targets=targets,
        preview_rows=preview_rows,
        store_root=store.root,
    )


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
    lines.append(f"Canonical skill store: {plan.store_root}")
    lines.append("Bundled skills:")
    for skill in plan.skills:
        lines.append(f"- {skill.name}")
    unique_dests = _unique_destinations(plan.targets)
    total = len(unique_dests) * len(plan.skills)
    lines.append(f"Total link operations: {total}")
    return "\n".join(lines)


def render_install_result(result: InstallResult) -> str:
    if result.cancelled:
        return "Installation cancelled."
    lines: list[str] = []
    destination_count = len({operation.destination for operation in result.operations})
    skill_count = len({operation.skill for operation in result.operations})
    skill_label = "skill" if skill_count == 1 else "skills"
    destination_label = "destination" if destination_count == 1 else "destinations"
    lines.append(
        f"Linked {skill_count} bundled {skill_label} into {destination_count} {destination_label} "
        f"({result.linked_count} links created, {result.already_linked_count} already linked)."
    )
    for operation in result.operations:
        if operation.status == FAILED:
            message = f": {operation.error}" if operation.error else ""
            lines.append(
                f"{operation.skill} at {operation.destination}: failed "
                f"({operation.error_code}){message}"
            )
            if operation.displaced_path is not None:
                lines.append(
                    f"  displaced directory kept at owned path: {operation.displaced_path}"
                )
    if result.unattempted_count:
        lines.append(
            f"Batch stopped after a failure; {result.unattempted_count} operations unattempted."
        )
    return "\n".join(lines)


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
                raise InstallerError(
                    f"Destination path has a file in its parent chain: {ancestor}"
                )


def _unique_displaced_name(destination: Path, skill_name: str) -> Path:
    token = os.urandom(4).hex()
    return destination / f".{skill_name}.displaced.{os.getpid()}.{token}"


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _link_one(
    harness: str,
    skill_name: str,
    destination: Path,
    store_root: Path,
) -> LinkOperationResult:
    """Create or repair one absolute directory symlink; never copy or traverse."""

    def operation(
        status: str,
        error_code: str | None = None,
        error: str | None = None,
        displaced_path: Path | None = None,
    ) -> LinkOperationResult:
        return LinkOperationResult(
            harness=harness,
            skill=skill_name,
            destination=destination,
            status=status,
            error_code=error_code,
            error=error,
            displaced_path=displaced_path,
        )

    link_path = destination / skill_name
    target = store_root / skill_name
    try:
        existing = os.lstat(link_path)
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        return operation(FAILED, LINK_ERROR, f"cannot inspect {link_path}: {exc}")

    if existing is not None:
        if stat.S_ISLNK(existing.st_mode):
            try:
                current_target = os.readlink(link_path)
            except OSError as exc:
                return operation(FAILED, LINK_ERROR, f"cannot read link {link_path}: {exc}")
            if current_target == str(target):
                return operation(ALREADY_LINKED)
            # Replace a wrong or dangling link itself; never traverse it.
            try:
                os.unlink(link_path)
            except OSError as exc:
                return operation(
                    FAILED, LINK_ERROR, f"cannot replace link {link_path}: {exc}"
                )
        elif stat.S_ISDIR(existing.st_mode):
            displaced = _unique_displaced_name(destination, skill_name)
            try:
                os.rename(link_path, displaced)
            except OSError as exc:
                return operation(
                    FAILED,
                    DISPLACED_RENAME_ERROR,
                    f"cannot set aside existing directory {link_path}: {exc}",
                )
            try:
                os.symlink(str(target), link_path)
            except OSError as exc:
                try:
                    os.rename(displaced, link_path)
                except OSError:
                    pass
                return operation(
                    FAILED,
                    LINK_ERROR,
                    f"cannot link {link_path}: {exc}; existing directory restored",
                )
            try:
                shutil.rmtree(displaced)
            except OSError as exc:
                return operation(
                    FAILED,
                    DISPLACED_CLEANUP_ERROR,
                    f"could not remove the displaced directory {displaced}: {exc}",
                    displaced_path=displaced,
                )
            _fsync_directory(destination)
            return operation(LINKED)
        elif stat.S_ISREG(existing.st_mode):
            return operation(
                FAILED,
                FILE_COLLISION_ERROR,
                f"Destination path collides with an existing file: {link_path}",
            )
        else:
            return operation(
                FAILED,
                LINK_ERROR,
                f"Destination path is not a directory, link, or regular file: {link_path}",
            )

    try:
        os.symlink(str(target), link_path)
    except OSError as exc:
        return operation(FAILED, LINK_ERROR, f"cannot link {link_path}: {exc}")
    _fsync_directory(destination)
    return operation(LINKED)


def _execute_install(plan: InstallPlan, store: SkillStore) -> InstallResult:
    """Refresh each selected skill once, then link it; stop on first failure."""
    unique_dests = _unique_destinations(plan.targets)
    for dest in unique_dests:
        dest.mkdir(parents=True, exist_ok=True)

    # Deduplicate the eight harnesses sharing one destination into a single
    # operation per (destination, skill), keeping the first harness in map
    # order for reporting.
    operations: list[tuple[str, str, Path]] = []
    seen: set[tuple[Path, str]] = set()
    for target in plan.targets:
        for skill in plan.skills:
            key = (target.destination, skill.name)
            if key in seen:
                continue
            seen.add(key)
            operations.append((target.harness, skill.name, target.destination))

    results: list[LinkOperationResult] = []
    refresh_results: list[SkillRefreshResult] = []
    refreshed: set[str] = set()
    stopped = False
    for harness, skill_name, destination in operations:
        if stopped:
            results.append(
                LinkOperationResult(
                    harness=harness,
                    skill=skill_name,
                    destination=destination,
                    status=UNATTEMPTED,
                )
            )
            continue
        if skill_name not in refreshed:
            refresh = store.refresh_skills((skill_name,))[0]
            refresh_results.append(refresh)
            refreshed.add(skill_name)
            if refresh.status == "failed":
                results.append(
                    LinkOperationResult(
                        harness=harness,
                        skill=skill_name,
                        destination=destination,
                        status=FAILED,
                        error_code=REFRESH_ERROR,
                        error=refresh.error,
                    )
                )
                stopped = True
                continue
        result_operation = _link_one(harness, skill_name, destination, store.root)
        results.append(result_operation)
        if result_operation.status == FAILED:
            stopped = True
    return InstallResult(
        mode=plan.mode,
        store_root=store.root,
        cancelled=False,
        refresh=tuple(refresh_results),
        operations=tuple(results),
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
    store: SkillStore | None = None,
) -> InstallResult:
    """Preview, confirm, refresh canonical bundles, and link destinations."""
    if only_skills is not None:
        seen: set[str] = set()
        deduplicated: list[str] = []
        for skill in only_skills:
            if skill not in seen:
                seen.add(skill)
                deduplicated.append(skill)
        only_skills = tuple(deduplicated)
    store = store or SkillStore()
    if stdin is None:
        stdin = sys.stdin
    if stdout is None:
        stdout = sys.stdout
    plan = build_install_plan(
        destination,
        only_skills=only_skills,
        include_optional=include_optional,
        store=store,
    )
    _ensure_valid_targets(plan)
    print(render_preview(plan), file=stdout)
    if not yes:
        if not stdin.isatty():
            raise InstallerError("stdin is not interactive, rerun with --yes.")
        response = input_fn("Proceed with installation? [y/N]: ").strip().lower()
        if response not in {"y", "yes"}:
            cancelled = InstallResult(
                mode=plan.mode,
                store_root=store.root,
                cancelled=True,
                refresh=(),
                operations=(),
            )
            print(render_install_result(cancelled), file=stdout)
            return cancelled
    result = _execute_install(plan, store)
    print(render_install_result(result), file=stdout)
    return result
