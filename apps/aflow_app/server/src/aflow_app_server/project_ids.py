"""Pure project identifier and default display-name rules.

A registry ID is an internal path-safe label, deliberately separate from the
actual directory basename so existing folders with underscores, uppercase
letters, spaces, or non-ASCII names register without renaming.  The rules are
deterministic: the same relative path always yields the same generated ID.
"""

from __future__ import annotations

from collections.abc import Container
import hashlib
from pathlib import PurePosixPath
import re


PROJECT_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BASE_MAX_LENGTH = 48
_FALLBACK_BASE = "project"


def basename_of(relative_path: str) -> str:
    """Return the exact final path component, preserving its case."""
    return PurePosixPath(relative_path).parts[-1]


def slug_basename_id(relative_path: str) -> str | None:
    """Return the basename when it is already a path-safe slug, else None."""
    slug = basename_of(relative_path)
    if PROJECT_ID_RE.fullmatch(slug) is not None:
        return slug
    return None


def deterministic_project_id(relative_path: str) -> str:
    """Derive the stable safe ID for an unsafe or collided basename.

    The base is the ASCII-lowercased basename with non-alphanumeric runs
    collapsed to hyphens, stripped and truncated; the suffix binds the ID to
    the exact normalized relative path, which is hashed byte-for-byte without
    lowercasing so case-distinct roots stay distinct.
    """
    chars: list[str] = []
    for char in basename_of(relative_path):
        if "a" <= char <= "z" or "0" <= char <= "9":
            chars.append(char)
        elif "A" <= char <= "Z":
            chars.append(char.lower())
        elif chars and chars[-1] != "-":
            chars.append("-")
    base = (
        "".join(chars)[:_BASE_MAX_LENGTH].rstrip("-") or _FALLBACK_BASE
    )
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:12]
    return f"{base}-{digest}"


def allocate_project_id(relative_path: str, occupied: Container[str]) -> str | None:
    """Pick the ID for one new root, or None when no safe ID is free.

    A basename that is already a path-safe slug keeps that exact ID for
    compatibility; otherwise the deterministic generated ID is used.  There is
    deliberately no counter-based retry allocator.
    """
    slug = slug_basename_id(relative_path)
    if slug is not None and slug not in occupied:
        return slug
    generated = deterministic_project_id(relative_path)
    if generated in occupied:
        return None
    return generated


def default_display_name(relative_path: str) -> str:
    """Default display name: titled slug for safe basenames, exact otherwise."""
    slug = slug_basename_id(relative_path)
    if slug is not None:
        return slug.replace("-", " ").title()
    return basename_of(relative_path)
