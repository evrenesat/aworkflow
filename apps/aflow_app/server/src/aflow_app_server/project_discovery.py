"""Bounded read-only discovery of existing Git roots beneath the managed root.

Discovery only enumerates bounded directory metadata and runs fixed,
timeout-bounded Git identity probes.  It never follows symlinks, never opens
candidate working-tree files or configuration, and grants no authorization:
the project registry remains the only operational allowlist.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import subprocess
from typing import Any

from .project_ids import allocate_project_id, default_display_name
from .project_registry import ProjectRegistry


DISCOVERY_SCHEMA_VERSION = 1
MAX_VISITED_ENTRIES = 500
MAX_CANDIDATES = 100
GIT_PROBE_TIMEOUT_SECONDS = 15.0
MAX_DEPTH = 2
# Exact skip names are case-sensitive on p100; hidden and _archive* names are
# skipped by prefix.
_EXACT_SKIP_NAMES = frozenset({"worktrees", "node_modules", ".venv", "evidence"})


class ProjectDiscoveryUnavailable(RuntimeError):
    """The managed root itself cannot be scanned, so no result exists."""


@dataclass(frozen=True)
class DiscoveredCandidate:
    """One bounded Git-root observation with its Add pre-check."""

    relative_path: str
    display_name: str
    registered_project_id: str | None
    addable: bool
    add_blocker: str | None


def _skipped_name(name: str) -> bool:
    return (
        name.startswith(".")
        or name.startswith("_archive")
        or name in _EXACT_SKIP_NAMES
    )


def _git_stdout(target: Path, *argv: str) -> str | None:
    """Run one fixed Git probe with a bounded timeout; None on any failure."""
    try:
        completed = subprocess.run(
            ("git", "-C", str(target), *argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _is_git_root(target: Path) -> bool:
    """A non-symlink .git entry plus an exact toplevel identity probe."""
    git_entry = target / ".git"
    if git_entry.is_symlink() or not git_entry.exists():
        return False
    observed = _git_stdout(target, "rev-parse", "--show-toplevel")
    if observed is None:
        return False
    try:
        observed_root = Path(observed).resolve(strict=True)
        resolved_target = target.resolve(strict=True)
    except OSError:
        return False
    return os.path.normcase(str(observed_root)) == os.path.normcase(str(resolved_target))


def _has_commit_head(target: Path) -> bool:
    return _git_stdout(target, "rev-parse", "--verify", "--quiet", "HEAD") is not None


def discover_projects(registry: ProjectRegistry) -> dict[str, Any]:
    """Return bounded Git-root candidates beneath the managed root.

    Registry corruption propagates as an error; only the managed root itself
    being unavailable yields no partial result (the caller maps that to 503).
    """
    managed_root = registry.managed_root
    if managed_root.is_symlink() or not managed_root.is_dir():
        raise ProjectDiscoveryUnavailable("managed projects root is unavailable")
    # Registry corruption stays an error here; discovery never substitutes
    # another allowlist.
    records = registry.list_records()
    # Match the registry's own path identity: normcase is a no-op for case
    # on p100, so distinct-cased roots stay distinct there.
    registered_by_root = {
        os.path.normcase(record.relative_root): record for record in records
    }
    registered_ids = {record.id for record in records}
    declared_roots = [
        managed_root.joinpath(*PurePosixPath(record.relative_root).parts)
        for record in records
    ]

    visited = 0
    skipped_unreadable = 0
    truncated = False
    stopped = False
    roots: list[tuple[Path, str]] = []
    queue: deque[tuple[Path, str]] = deque([(managed_root, "")])

    while queue and not stopped:
        directory, prefix = queue.popleft()
        try:
            iterator = os.scandir(directory)
        except OSError:
            if directory == managed_root:
                # An unreadable root yields no trustworthy result at all, not
                # an empty candidate list.
                raise ProjectDiscoveryUnavailable(
                    "managed projects root is unavailable"
                ) from None
            skipped_unreadable += 1
            continue
        with iterator:
            while True:
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                except OSError:
                    skipped_unreadable += 1
                    break
                visited += 1
                if visited > MAX_VISITED_ENTRIES:
                    # This entry was yielded but cannot be inspected; it and
                    # everything after it stay unprocessed.
                    truncated = True
                    stopped = True
                    break
                if _skipped_name(entry.name):
                    continue
                if entry.is_symlink():
                    continue
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                except OSError:
                    skipped_unreadable += 1
                    continue
                if not is_directory:
                    continue
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                child = Path(entry.path)
                if _is_git_root(child):
                    if len(roots) >= MAX_CANDIDATES:
                        truncated = True
                        stopped = True
                        break
                    roots.append((child, relative))
                    # Descent stops at a Git root.
                    continue
                if not prefix:
                    # Only direct children (depth 1) are scanned further, so
                    # grandchildren (depth 2) are the deepest candidates.
                    queue.append((child, relative))

    candidates: list[DiscoveredCandidate] = []
    for path, relative in roots:
        record = registered_by_root.get(os.path.normcase(relative))
        if record is not None:
            candidates.append(DiscoveredCandidate(
                relative_path=relative,
                display_name=record.display_name,
                registered_project_id=record.id,
                addable=False,
                add_blocker="already registered",
            ))
            continue
        candidate_key = os.path.normcase(str(path))
        overlap = any(
            os.path.normcase(str(declared)) == candidate_key
            or path in declared.parents
            or declared in path.parents
            for declared in declared_roots
        )
        if overlap:            blocker = "overlaps a registered project root"
        elif not _has_commit_head(path):
            blocker = "repository HEAD does not point to a commit"
        elif allocate_project_id(relative, registered_ids) is None:
            # The same ID rule ProjectService uses at registration time: only
            # an unallocatable ID blocks Add, never the basename itself.
            blocker = "project id is already registered"
        else:
            blocker = None
        candidates.append(DiscoveredCandidate(
            relative_path=relative,
            display_name=default_display_name(relative),
            registered_project_id=None,
            addable=blocker is None,
            add_blocker=blocker,
        ))

    candidates.sort(key=lambda candidate: candidate.relative_path)
    return {
        "schema_version": DISCOVERY_SCHEMA_VERSION,
        "managed_root": str(managed_root),
        "candidates": [
            {
                "relative_path": candidate.relative_path,
                "display_name": candidate.display_name,
                "registered_project_id": candidate.registered_project_id,
                "addable": candidate.addable,
                "add_blocker": candidate.add_blocker,
            }
            for candidate in candidates
        ],
        "visited_entries": min(visited, MAX_VISITED_ENTRIES),
        "skipped_unreadable": skipped_unreadable,
        "truncated": truncated,
        "limits": {
            "max_visited_entries": MAX_VISITED_ENTRIES,
            "max_candidates": MAX_CANDIDATES,
        },
    }
