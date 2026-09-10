from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Collection
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal


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


WorktreeExecutionMode = Literal["same_checkout", "new_worktree"]


@dataclass(frozen=True)
class WorktreeStatusItem:
    """One decoded record from ``git status --porcelain=v1 -z``."""

    path: str
    original_path: str | None
    index_status: str
    worktree_status: str


@dataclass(frozen=True)
class WorktreePreflight:
    """Read-only working-tree state used by startup and lifecycle checks."""

    checkout_path: Path
    execution_mode: WorktreeExecutionMode
    dirty: bool
    requires_confirmation: bool
    blockers: tuple[str, ...]
    total_items: int
    items: tuple[WorktreeStatusItem, ...]


class WorktreeInspectionError(RuntimeError):
    """Git status could not be inspected reliably."""


AFLOW_OWNED_PATH_ROOTS = (".aflow",)


_GIT_C_STYLE_ESCAPE_BYTES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
}


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

    encoded_path = bytearray()
    contents = atom[1:-1]
    index = 0
    while index < len(contents):
        char = contents[index]
        if char != "\\":
            try:
                encoded_path.extend(os.fsencode(char))
            except UnicodeEncodeError:
                return None
            index += 1
            continue

        index += 1
        if index >= len(contents):
            return None
        escape = contents[index]
        escaped_byte = _GIT_C_STYLE_ESCAPE_BYTES.get(escape)
        if escaped_byte is not None:
            encoded_path.append(escaped_byte)
            index += 1
            continue

        if escape not in "01234567":
            return None
        octal_escape = contents[index:index + 3]
        if len(octal_escape) != 3 or any(
            digit not in "01234567" for digit in octal_escape
        ):
            return None
        encoded_path.append(int(octal_escape, 8))
        index += 3

    return os.fsdecode(bytes(encoded_path))


class WorktreeStatusParseError(ValueError):
    """A status stream was not valid NUL-delimited porcelain-v1 output."""


def parse_porcelain_status(
    output: bytes | str,
) -> tuple[WorktreeStatusItem, ...]:
    """Decode NUL-delimited porcelain-v1 records without splitting filenames.

    Git emits the destination followed by the source for rename and copy
    records when ``-z`` is enabled.  The parser keeps that distinction in the
    typed item while decoding filenames with the filesystem surrogateescape
    policy, so spaces, newlines, and non-UTF-8 bytes remain representable.
    """
    if isinstance(output, str):
        output_bytes = os.fsencode(output)
    elif isinstance(output, bytes):
        output_bytes = output
    else:
        raise TypeError("porcelain status output must be bytes or str")

    records = output_bytes.split(b"\0")
    items: list[WorktreeStatusItem] = []
    record_index = 0
    while record_index < len(records):
        record = records[record_index]
        record_index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise WorktreeStatusParseError(
                "porcelain status record has no XY status separator"
            )

        try:
            index_status, worktree_status = record[:2].decode("ascii")
        except UnicodeDecodeError as exc:
            raise WorktreeStatusParseError(
                "porcelain status record has non-ASCII XY status"
            ) from exc
        xy = index_status + worktree_status
        if index_status not in " MARDCTU?!" or worktree_status not in " MARDCTU?!":
            raise WorktreeStatusParseError(
                f"porcelain status record has invalid XY status {xy!r}"
            )

        path_bytes = record[3:]
        if not path_bytes:
            raise WorktreeStatusParseError("porcelain status record has an empty path")
        path = os.fsdecode(path_bytes)
        original_path: str | None = None
        if "R" in (index_status, worktree_status) or "C" in (
            index_status,
            worktree_status,
        ):
            if record_index >= len(records) or not records[record_index]:
                raise WorktreeStatusParseError(
                    "rename or copy status record has no original path"
                )
            original_path = os.fsdecode(records[record_index])
            record_index += 1

        items.append(
            WorktreeStatusItem(
                path=path,
                original_path=original_path,
                index_status=index_status,
                worktree_status=worktree_status,
            )
        )

    return tuple(
        sorted(
            items,
            key=lambda item: (item.path, item.original_path or ""),
        )
    )


# Keep the suffix-bearing name available for callers that want to make the
# NUL framing explicit while retaining one parser implementation.
parse_porcelain_status_z = parse_porcelain_status


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


def classify_status_items_by_prefix(
    items: Collection[WorktreeStatusItem],
    prefix: str = "plans/",
    *,
    ignore_lifecycle_owned: bool = False,
    ignore_untracked: bool = False,
) -> tuple[list[str], list[str]]:
    """Classify decoded status records without losing rename source paths."""
    plan_paths: list[str] = []
    non_plan_paths: list[str] = []

    for item in items:
        if ignore_untracked and item.index_status == "?" and item.worktree_status == "?":
            continue
        paths = (item.original_path, item.path) if item.original_path else (item.path,)
        _classify_paths_into(
            paths,
            prefix=prefix,
            ignore_lifecycle_owned=ignore_lifecycle_owned,
            plan_paths=plan_paths,
            non_plan_paths=non_plan_paths,
        )

    return plan_paths, non_plan_paths


def _classify_paths_into(
    paths: Collection[str],
    *,
    prefix: str,
    ignore_lifecycle_owned: bool,
    plan_paths: list[str],
    non_plan_paths: list[str],
) -> None:
    """Apply the shared lifecycle/plan path classification to decoded paths."""
    for path in paths:
        if ignore_lifecycle_owned and is_lifecycle_owned_path(path):
            continue
        if path.startswith(prefix):
            plan_paths.append(path)
        else:
            non_plan_paths.append(path)


def _git_directory(repo_root: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError) as exc:
        raise WorktreeInspectionError(
            f"cannot resolve the Git directory for '{repo_root}': {exc}"
        ) from exc
    if result.returncode != 0 or not result.stdout.strip():
        detail = result.stderr.strip() or "git rev-parse --git-dir failed"
        raise WorktreeInspectionError(
            f"cannot resolve the Git directory for '{repo_root}': {detail}"
        )
    git_dir = Path(result.stdout.strip())
    return git_dir if git_dir.is_absolute() else repo_root / git_dir


def _in_progress_git_operation_markers(repo_root: Path) -> tuple[str, ...]:
    git_dir = _git_directory(repo_root)

    markers = (
        "MERGE_HEAD",
        "REBASE_HEAD",
        "rebase-merge",
        "rebase-apply",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "sequencer",
    )
    active: list[str] = []
    for marker in markers:
        if (git_dir / marker).exists():
            active.append(marker)
    return tuple(active)


_UNMERGED_STATUS_CODES = frozenset({
    "AA",
    "AU",
    "DD",
    "DU",
    "UA",
    "UD",
    "UU",
})


def preflight_worktree(
    repo_root: Path,
    execution_mode: WorktreeExecutionMode = "same_checkout",
    *,
    allow_untracked: bool = False,
) -> WorktreePreflight:
    """Inspect one checkout and return the shared startup/lifecycle result.

    A failed Git inspection raises ``WorktreeInspectionError``.  Callers must
    not turn an unavailable or malformed status stream into a clean result.
    """
    if execution_mode not in {"same_checkout", "new_worktree"}:
        raise ValueError(f"unsupported worktree execution mode: {execution_mode}")

    checkout_path = Path(repo_root).resolve()
    try:
        result = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            cwd=str(checkout_path),
            capture_output=True,
            check=False,
        )
    except (OSError, FileNotFoundError) as exc:
        raise WorktreeInspectionError(
            f"git status inspection failed for '{checkout_path}': {exc}"
        ) from exc
    if result.returncode != 0:
        stderr = result.stderr
        if isinstance(stderr, bytes):
            stderr = os.fsdecode(stderr)
        detail = str(stderr).strip() or "git status returned a failure"
        raise WorktreeInspectionError(
            f"git status inspection failed for '{checkout_path}': {detail}"
        )

    try:
        items = parse_porcelain_status(result.stdout)
    except (TypeError, WorktreeStatusParseError) as exc:
        raise WorktreeInspectionError(
            f"git status inspection returned malformed porcelain data for "
            f"'{checkout_path}': {exc}"
        ) from exc

    plan_paths, non_plan_paths = classify_status_items_by_prefix(
        items,
        ignore_lifecycle_owned=True,
        ignore_untracked=allow_untracked,
    )
    effective_items = tuple(
        item
        for item in items
        if not (
            allow_untracked
            and item.index_status == "?"
            and item.worktree_status == "?"
        )
    )
    blockers = [
        f"unresolved conflict: {item.path}"
        for item in items
        if item.index_status + item.worktree_status in _UNMERGED_STATUS_CODES
        or "U" in (item.index_status, item.worktree_status)
    ]
    blockers.extend(
        f"in-progress Git operation ({marker} exists)"
        for marker in _in_progress_git_operation_markers(checkout_path)
    )

    dirty = bool(effective_items)
    requires_confirmation = dirty and (
        bool(plan_paths or non_plan_paths)
        if execution_mode == "same_checkout"
        else bool(non_plan_paths)
    )
    return WorktreePreflight(
        checkout_path=checkout_path,
        execution_mode=execution_mode,
        dirty=dirty,
        requires_confirmation=requires_confirmation,
        blockers=tuple(blockers),
        total_items=len(items),
        items=items,
    )


def probe_worktree(repo_root: Path) -> WorktreeProbe | None:
    """Return dirty-state summary, or None when git is unavailable."""
    try:
        result = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            cwd=str(repo_root),
            capture_output=True,
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

    try:
        items = parse_porcelain_status(result.stdout)
    except (TypeError, WorktreeStatusParseError):
        return None

    for item in items:
        xy = item.index_status + item.worktree_status
        path = item.path

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

    is_dirty = bool(items)
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

        _classify_paths_into(
            paths,
            prefix=prefix,
            ignore_lifecycle_owned=ignore_lifecycle_owned,
            plan_paths=plan_paths,
            non_plan_paths=non_plan_paths,
        )

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
