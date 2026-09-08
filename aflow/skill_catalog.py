"""Bundled skill inventory and package-resource lookup.

One module owns the bundled skill registry so the installer and the canonical
skill store share it without importing each other: this module depends only on
package resources. The public names that previously lived on
``aflow.skill_installer`` remain importable from there as re-exports, so the
existing CLI installer contract is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
try:
    from importlib.resources.abc import Traversable
except ImportError:
    from importlib.abc import Traversable


class SkillCatalogError(RuntimeError):
    """A bundled skill name or package resource lookup failed."""


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
    BundledSkillMetadata(name="aflow-manager", default=True),
    BundledSkillMetadata(name="aflow-repartition-checkpoint", default=True),
    BundledSkillMetadata(name="aflow-guard-development-run", default=True),
    BundledSkillMetadata(name="material-code-review", default=True),
    BundledSkillMetadata(name="aflow-assistant", default=False),
)

DEFAULT_BUNDLED_SKILL_NAMES = tuple(
    meta.name for meta in BUNDLED_SKILL_METADATA if meta.default
)

OPTIONAL_BUNDLED_SKILL_NAMES = tuple(
    meta.name for meta in BUNDLED_SKILL_METADATA if not meta.default
)

BUNDLED_SKILL_NAMES = tuple(sorted(meta.name for meta in BUNDLED_SKILL_METADATA))

_BUNDLED_SKILL_NAME_SET = frozenset(BUNDLED_SKILL_NAMES)


@dataclass(frozen=True)
class BundledSkill:
    name: str
    source: Traversable


def bundled_skills_root() -> Traversable:
    return resources.files("aflow").joinpath("bundled_skills")


def is_bundled_skill_name(name: object) -> bool:
    """Return True only for an exact registered bundled skill name."""
    return isinstance(name, str) and name in _BUNDLED_SKILL_NAME_SET


def validate_bundled_skill_name(name: str) -> None:
    """Reject anything that is not an exact registered bundled skill name."""
    if not is_bundled_skill_name(name):
        raise SkillCatalogError(
            f"Unknown bundled skill: {name}. "
            f"Valid skills are: {', '.join(BUNDLED_SKILL_NAMES)}"
        )


def bundled_skill_resource(name: str) -> Traversable:
    """Return the package-resource directory for one registered bundled skill."""
    validate_bundled_skill_name(name)
    skill_dir = bundled_skills_root().joinpath(name)
    if not skill_dir.is_dir() or not skill_dir.joinpath("SKILL.md").is_file():
        raise SkillCatalogError(f"Missing bundled skill resources: {name}")
    return skill_dir


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
        raise SkillCatalogError(f"Missing bundled skill resources: {missing_text}")
    return tuple(skills)
