from __future__ import annotations

import ast
import os
import shutil
import subprocess
import tempfile
from collections.abc import Collection
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class RepoState(Enum):
    """Lifecycle-startup classification of the git state at a given path."""
    NO_GIT_BINARY = "no_git_binary"
    NOT_A_REPO = "not_a_repo"
    UNBORN = "unborn"
    READY = "ready"


def probe_repo_state(repo_root: Path) -> RepoState:
    """Classify git state at repo_root without side effects.

    Returns one of:
    - NO_GIT_BINARY: git binary not found or not executable
    - NOT_A_REPO: path exists but is not inside any git repository
    - UNBORN: git repo exists but has no commits yet (unborn HEAD)
    - READY: git repo exists and has at least one commit
    """
    if shutil.which("git") is None:
        return RepoState.NO_GIT_BINARY

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return RepoState.NO_GIT_BINARY

    if result.returncode != 0:
        return RepoState.NOT_A_REPO

    try:
        head_result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return RepoState.NO_GIT_BINARY

    if head_result.returncode != 0:
        return RepoState.UNBORN

    return RepoState.READY


@dataclass(frozen=True)
class GitBaseline:
    head_sha: str | None
    tree_oid: str


@dataclass(frozen=True)
class GitSummary:
    modified_count: int
    added_count: int
    removed_count: int
    lines_added: int
    lines_removed: int
    commit_count: int
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class WorktreeProbe:
    is_dirty: bool
    modified_count: int
    added_count: int
    removed_count: int
    sample_paths: tuple[str, ...]


AFLOW_OWNED_PATH_ROOTS = (".aflow",)


def _porcelain_path_field(line: str) -> str | None:
    if len(line) < 4 or line[2] != " ":
        return None
    return line[3:]


def _decode_porcelain_path_atom(atom: str) -> str | None:
    if not atom:
        return None
    if not atom.startswith('"'):
        return atom if '"' not in atom else None
    if not atom.endswith('"'):
        return None

    escaped = False
    for index, char in enumerate(atom[1:], start=1):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"' and index != len(atom) - 1:
            return None
    if escaped:
        return None

    try:
        decoded = ast.literal_eval(atom)
    except (SyntaxError, ValueError):
        return None
    return decoded if isinstance(decoded, str) else None


def _rename_copy_atoms(path_field: str) -> tuple[str, str] | None:
    separators: list[int] = []
    in_quotes = False
    escaped = False
    index = 0
    while index < len(path_field):
        char = path_field[index]
        if escaped:
            escaped = False
        elif in_quotes and char == "\\":
            escaped = True
        elif char == '"':
            in_quotes = not in_quotes
        elif not in_quotes and path_field.startswith(" -> ", index):
            separators.append(index)
            index += len(" -> ") - 1
        index += 1

    if in_quotes or escaped or len(separators) != 1:
        return None
    separator = separators[0]
    return path_field[:separator], path_field[separator + len(" -> "):]


def porcelain_status_paths(line: str) -> tuple[str, ...] | None:
    """Return every repo-relative path from one porcelain-v1 status record."""
    path_field = _porcelain_path_field(line)
    if path_field is None:
        return None

    xy = line[:2]
    if "R" not in xy and "C" not in xy:
        path = _decode_porcelain_path_atom(path_field)
        return (path,) if path is not None else None

    atoms = _rename_copy_atoms(path_field)
    if atoms is None:
        return None
    source = _decode_porcelain_path_atom(atoms[0])
    destination = _decode_porcelain_path_atom(atoms[1])
    if source is None or destination is None:
        return None
    return source, destination


def porcelain_status_path(line: str) -> str | None:
    """Return the destination repo-relative path from a porcelain-v1 record."""
    paths = porcelain_status_paths(line)
    return paths[-1] if paths else None


def is_lifecycle_owned_path(
    path: str,
    *,
    additional_roots: Collection[str] = (),
) -> bool:
    """Return whether a repo-relative POSIX path belongs to aflow lifecycle state."""
    for root in (*AFLOW_OWNED_PATH_ROOTS, *additional_roots):
        normalized_root = root.rstrip("/")
        if path == normalized_root or path.startswith(f"{normalized_root}/"):
            return True
    return False


def probe_worktree(repo_root: Path) -> WorktreeProbe | None:
    """Return dirty-state summary, or None when git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return None

    if result.returncode != 0:
        return None

    modified_count = 0
    added_count = 0
    removed_count = 0
    sample_paths: list[str] = []

    for line in result.stdout.splitlines():
        if len(line) < 3:
            continue
        xy = line[:2]
        path = line[3:]

        if len(sample_paths) < 3:
            sample_paths.append(path)

        if "?" in xy:
            added_count += 1
        elif "D" in xy:
            removed_count += 1
        elif "A" in xy:
            added_count += 1
        else:
            modified_count += 1

    is_dirty = bool(result.stdout.strip())
    return WorktreeProbe(
        is_dirty=is_dirty,
        modified_count=modified_count,
        added_count=added_count,
        removed_count=removed_count,
        sample_paths=tuple(sample_paths),
    )


def _create_tree_snapshot(repo_root: Path) -> str | None:
    """Create a tree OID from the full working tree using a temporary index file."""
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_index = os.path.join(tmp_dir, "index")
            env = {**os.environ, "GIT_INDEX_FILE": tmp_index}
            add_result = subprocess.run(
                ["git", "add", "-A"],
                cwd=str(repo_root),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if add_result.returncode != 0:
                return None

            tree_result = subprocess.run(
                ["git", "write-tree"],
                cwd=str(repo_root),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if tree_result.returncode != 0:
                return None

            return tree_result.stdout.strip()
    except (OSError, FileNotFoundError):
        return None


def capture_baseline(repo_root: Path) -> GitBaseline | None:
    """Capture the current HEAD SHA and working-tree OID as a workflow-start baseline."""
    try:
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        head_sha: str | None = None
        if head_result.returncode == 0:
            head_sha = head_result.stdout.strip()

        tree_oid = _create_tree_snapshot(repo_root)
        if tree_oid is None:
            return None

        return GitBaseline(head_sha=head_sha, tree_oid=tree_oid)
    except (OSError, FileNotFoundError):
        return None


def classify_dirtiness_by_prefix(
    porcelain_output: str,
    prefix: str = "plans/",
    *,
    ignore_lifecycle_owned: bool = False,
) -> tuple[list[str], list[str]]:
    """Classify repo-relative paths from git porcelain output by prefix.

    Returns (paths_under_prefix, paths_outside_prefix).
    Both lists contain repo-relative paths from the porcelain output.
    When requested, aflow-owned lifecycle paths are omitted from both lists.
    """
    plan_paths: list[str] = []
    non_plan_paths: list[str] = []

    for line in porcelain_output.splitlines():
        if not line:
            continue
        paths = porcelain_status_paths(line)
        if paths is None:
            path_field = _porcelain_path_field(line)
            non_plan_paths.append(path_field if path_field is not None else line)
            continue

        for path in paths:
            if ignore_lifecycle_owned and is_lifecycle_owned_path(path):
                continue
            if path.startswith(prefix):
                plan_paths.append(path)
            else:
                non_plan_paths.append(path)

    return plan_paths, non_plan_paths


def summarize_since_baseline(repo_root: Path, baseline: GitBaseline) -> GitSummary | None:
    """Compare current working-tree state to baseline and return a delta summary."""
    try:
        current_tree = _create_tree_snapshot(repo_root)
        if current_tree is None:
            return None

        if current_tree == baseline.tree_oid:
            return GitSummary(
                modified_count=0,
                added_count=0,
                removed_count=0,
                lines_added=0,
                lines_removed=0,
                commit_count=0,
                changed_paths=(),
            )

        name_status = subprocess.run(
            ["git", "diff", "--name-status", "--no-renames", baseline.tree_oid, current_tree],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if name_status.returncode != 0:
            return None

        numstat = subprocess.run(
            ["git", "diff", "--numstat", "--no-renames", baseline.tree_oid, current_tree],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if numstat.returncode != 0:
            return None

        modified_count = 0
        added_count = 0
        removed_count = 0
        changed_paths: list[str] = []

        for line in name_status.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) < 2:
                continue
            status, path = parts
            changed_paths.append(path)
            status = status.strip()
            if status == "M":
                modified_count += 1
            elif status == "A":
                added_count += 1
            elif status == "D":
                removed_count += 1

        lines_added = 0
        lines_removed = 0
        for line in numstat.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 2:
                continue
            try:
                if parts[0] != "-":
                    lines_added += int(parts[0])
                if parts[1] != "-":
                    lines_removed += int(parts[1])
            except ValueError:
                pass

        commit_count = 0
        if baseline.head_sha is not None:
            rev_list = subprocess.run(
                ["git", "rev-list", "--count", f"{baseline.head_sha}..HEAD"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
            if rev_list.returncode == 0:
                try:
                    commit_count = int(rev_list.stdout.strip())
                except ValueError:
                    pass

        return GitSummary(
            modified_count=modified_count,
            added_count=added_count,
            removed_count=removed_count,
            lines_added=lines_added,
            lines_removed=lines_removed,
            commit_count=commit_count,
            changed_paths=tuple(changed_paths),
        )
    except (OSError, FileNotFoundError):
        return None
