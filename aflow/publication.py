"""Explicit repository-local publication at the successful workflow boundary."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


class PublicationError(RuntimeError):
    pass


class PlanLifecycleError(PublicationError):
    """A completed-plan move could not be proven safe to continue."""


@dataclass(frozen=True)
class CompletedPlanLifecycle:
    """Result of one verified completed-plan filesystem/Git lifecycle."""

    destination: Path
    moved: bool
    commit: str | None
    source_tracked: bool
    phase: str


_PLAN_LIFECYCLE_KEY = "plan_lifecycle"
_PLAN_LIFECYCLE_SCHEMA = 1
_PLAN_LIFECYCLE_COMMIT_MESSAGE = "chore/plans: Record completed plan lifecycle move"
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


def _load_receipt(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError("publication receipt is not readable JSON") from exc
    if not isinstance(value, dict):
        raise PublicationError("publication receipt must contain a JSON object")
    return value


def _write_atomic_receipt(path: Path, receipt: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            pass
        else:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _git(root: Path, *args: str, optional: bool = False) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], text=True, capture_output=True,
            timeout=120, env=None,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublicationError(f"git {args[0]} failed; inspect the publication checkout") from exc
    if result.returncode and not (optional and result.returncode in (1, 128)):
        # Git output may contain credential-bearing URLs. Keep failures bounded.
        raise PublicationError(f"git {args[0]} failed; inspect the publication checkout")
    return result.stdout.strip()


def _git_bytes(root: Path, *args: str, optional: bool = False) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True,
            timeout=120, env=None,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublicationError(f"git {args[0]} failed; inspect the publication checkout") from exc
    if result.returncode and not (optional and result.returncode in (1, 128)):
        raise PublicationError(f"git {args[0]} failed; inspect the publication checkout")
    return result.stdout


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise PlanLifecycleError("could not read the completed plan bytes") from exc
    return size, digest.hexdigest()


def _plan_paths(root: Path, plan_path: Path) -> tuple[Path, Path, str, str]:
    root = root.resolve()
    source = (root / plan_path if not plan_path.is_absolute() else plan_path).resolve()
    plans_root = (root / "plans").resolve()
    in_progress_root = plans_root / "in-progress"
    try:
        relative = source.relative_to(in_progress_root)
        source_relative = source.relative_to(root).as_posix()
    except ValueError as exc:
        raise PlanLifecycleError(
            "completed plan is not under plans/in-progress"
        ) from exc
    destination = plans_root / "done" / relative
    return source, destination, source_relative, destination.relative_to(root).as_posix()


def _is_git_checkout(root: Path) -> bool:
    if shutil.which("git") is None:
        return False
    try:
        top_level = _git(root, "rev-parse", "--show-toplevel", optional=True)
    except PublicationError as exc:
        raise PlanLifecycleError("could not inspect the completed plan checkout") from exc
    if not top_level:
        return False
    try:
        return Path(top_level).resolve() == root.resolve()
    except OSError as exc:
        raise PlanLifecycleError("could not resolve the completed plan checkout") from exc


def _index_blob(root: Path, relative_path: str) -> str | None:
    output = _git_bytes(
        root,
        "--literal-pathspecs",
        "ls-files",
        "--stage",
        "-z",
        "--",
        relative_path,
        optional=True,
    )
    records = [record for record in output.split(b"\0") if record]
    if not records:
        return None
    if len(records) != 1:
        raise PlanLifecycleError(
            f"cannot finalize a plan with unresolved Git index entries: {relative_path}"
        )
    try:
        metadata, _ = records[0].split(b"\t", 1)
        mode, blob, stage = metadata.split()
    except ValueError as exc:
        raise PlanLifecycleError("Git returned an invalid plan index entry") from exc
    if stage != b"0" or mode not in {b"100644", b"100755", b"120000"}:
        raise PlanLifecycleError("the completed plan is not a regular Git blob")
    try:
        return blob.decode("ascii")
    except UnicodeDecodeError as exc:
        raise PlanLifecycleError("Git returned an invalid plan blob identity") from exc


def _staged_paths(root: Path) -> frozenset[str]:
    output = _git_bytes(
        root,
        "diff",
        "--cached",
        "--no-renames",
        "--name-only",
        "-z",
        "--",
    )
    return frozenset(
        os.fsdecode(path)
        for path in output.split(b"\0")
        if path
    )


def _is_ignored(root: Path, relative_path: str) -> bool:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "check-ignore",
                "-q",
                "--no-index",
                "--",
                relative_path,
            ],
            capture_output=True,
            timeout=120,
            env=None,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PlanLifecycleError("could not inspect the Done plan storage policy") from exc
    if result.returncode not in {0, 1}:
        raise PlanLifecycleError("could not inspect the Done plan storage policy")
    return result.returncode == 0


@dataclass(frozen=True)
class _GitLifecycleState:
    head: str | None
    source_blob: str | None
    destination_blob: str | None
    staged_paths: frozenset[str]

    @property
    def source_tracked(self) -> bool:
        return self.source_blob is not None

    @property
    def destination_tracked(self) -> bool:
        return self.destination_blob is not None


def _git_lifecycle_state(
    root: Path,
    *,
    source_relative: str,
    destination_relative: str,
) -> _GitLifecycleState | None:
    if not _is_git_checkout(root):
        return None
    return _GitLifecycleState(
        head=_git(root, "rev-parse", "--verify", "HEAD", optional=True) or None,
        source_blob=_index_blob(root, source_relative),
        destination_blob=_index_blob(root, destination_relative),
        staged_paths=_staged_paths(root),
    )


def _lifecycle_record(receipt: dict[str, object]) -> dict[str, object] | None:
    value = receipt.get(_PLAN_LIFECYCLE_KEY)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PlanLifecycleError("publication receipt has an invalid plan lifecycle")
    source = value.get("source")
    destination = value.get("destination")
    phase = value.get("phase")
    if not isinstance(source, str) or not isinstance(destination, str) or not isinstance(phase, str):
        raise PlanLifecycleError("publication receipt lacks exact plan lifecycle identity")
    if phase not in {"prepared", "moved", "commit_pending", "committed"}:
        raise PlanLifecycleError("publication receipt has an unknown plan lifecycle phase")
    if value.get("complete") is False:
        raise PlanLifecycleError("publication receipt does not prove a complete plan")
    for field in ("source_tracked", "destination_tracked"):
        if field in value and not isinstance(value[field], bool):
            raise PlanLifecycleError("publication receipt has invalid plan tracking identity")
    commit_paths = value.get("commit_paths")
    if commit_paths is not None and (
        not isinstance(commit_paths, list)
        or any(not isinstance(path, str) for path in commit_paths)
        or len(set(commit_paths)) != len(commit_paths)
    ):
        raise PlanLifecycleError("publication receipt has invalid lifecycle commit paths")
    staging = value.get("staging")
    if staging is not None:
        if not isinstance(staging, dict):
            raise PlanLifecycleError("publication receipt has invalid lifecycle staging")
        staging_phase = staging.get("phase")
        if not isinstance(staging_phase, str) or staging_phase not in {
            "source_pending",
            "destination_pending",
            "ready",
        }:
            raise PlanLifecycleError("publication receipt has an unknown lifecycle staging phase")
        for field in (
            "source_before",
            "destination_before",
            "destination_expected",
            "destination_after",
        ):
            identity = staging.get(field)
            if identity is not None and (
                not isinstance(identity, str) or _SHA_RE.fullmatch(identity) is None
            ):
                raise PlanLifecycleError("publication receipt has invalid lifecycle index identity")
        for field in (
            "source_should_stage",
            "destination_should_stage",
            "source_staged_before",
            "destination_staged_before",
        ):
            if type(staging.get(field)) is not bool:
                raise PlanLifecycleError("publication receipt has invalid lifecycle staging intent")
    return dict(value)


def _lifecycle_staging(record: dict[str, object]) -> dict[str, object] | None:
    value = record.get("staging")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PlanLifecycleError("publication receipt has invalid lifecycle staging")
    return value


def _record_phase(
    receipt_path: Path,
    receipt: dict[str, object],
    record: dict[str, object],
    phase: str,
    phase_hook: Callable[[str], None] | None,
) -> None:
    record["phase"] = phase
    receipt[_PLAN_LIFECYCLE_KEY] = dict(record)
    _write_atomic_receipt(receipt_path, receipt)
    if phase_hook is not None:
        phase_hook(phase)


def _save_lifecycle(
    receipt_path: Path,
    receipt: dict[str, object],
    record: dict[str, object],
) -> None:
    receipt[_PLAN_LIFECYCLE_KEY] = dict(record)
    _write_atomic_receipt(receipt_path, receipt)


def _require_record_bytes(
    path: Path,
    record: dict[str, object],
    *,
    label: str,
) -> tuple[int, str]:
    size = record.get("content_size")
    digest = record.get("content_sha256")
    if type(size) is not int or size < 0 or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PlanLifecycleError("publication receipt lacks exact completed-plan byte identity")
    actual = _file_identity(path)
    if actual != (size, digest):
        raise PlanLifecycleError(f"{label} bytes diverged from the recorded completed plan")
    return actual


def _working_tree_blob(root: Path, relative_path: str) -> str:
    """Return the Git blob identity for one regular working-tree file."""
    blob = _git(
        root,
        "hash-object",
        f"--path={relative_path}",
        "--",
        relative_path,
        optional=True,
    )
    if _SHA_RE.fullmatch(blob) is None:
        raise PlanLifecycleError("Git did not return the completed plan blob identity")
    return blob


def _source_stage_complete(
    state: _GitLifecycleState,
    *,
    source_relative: str,
    staging: dict[str, object],
) -> bool:
    before = staging.get("source_before")
    should_stage = staging.get("source_should_stage")
    staged_before = staging.get("source_staged_before")
    current = state.source_blob
    staged = source_relative in state.staged_paths
    if type(should_stage) is not bool or type(staged_before) is not bool:
        raise PlanLifecycleError("publication receipt has invalid lifecycle staging intent")
    if should_stage and not isinstance(before, str):
        raise PlanLifecycleError("publication receipt lacks the source index identity")
    if not should_stage:
        if current != before or (staged and not staged_before):
            raise PlanLifecycleError("source index identity diverged during lifecycle staging")
        return True
    if staged:
        if current is None:
            return True
        if current == before and staged_before:
            return False
        raise PlanLifecycleError("source index identity diverged during lifecycle staging")
    if current != before:
        raise PlanLifecycleError("source index identity diverged during lifecycle staging")
    return False


def _destination_stage_complete(
    state: _GitLifecycleState,
    *,
    destination_relative: str,
    staging: dict[str, object],
) -> bool:
    before = staging.get("destination_before")
    expected = staging.get("destination_expected")
    should_stage = staging.get("destination_should_stage")
    staged_before = staging.get("destination_staged_before")
    if type(should_stage) is not bool or type(staged_before) is not bool:
        raise PlanLifecycleError("publication receipt has invalid lifecycle staging intent")
    current = state.destination_blob
    staged = destination_relative in state.staged_paths
    if not should_stage:
        if current != before:
            raise PlanLifecycleError("Done plan index identity diverged during lifecycle staging")
        if staged and (not staged_before or current != expected):
            raise PlanLifecycleError("staged Done plan blob diverged during lifecycle staging")
        return True
    if not isinstance(expected, str):
        raise PlanLifecycleError("publication receipt lacks the Done plan index identity")
    if staged:
        if current != expected:
            raise PlanLifecycleError("staged Done plan blob diverged during lifecycle staging")
        return True
    if current != before:
        raise PlanLifecycleError("Done plan index identity diverged during lifecycle staging")
    return False


def _commit_paths(root: Path, commit: str) -> frozenset[str]:
    if _SHA_RE.fullmatch(commit) is None:
        return frozenset()
    output = _git_bytes(
        root,
        "diff-tree",
        "--no-commit-id",
        "--no-renames",
        "--name-only",
        "-r",
        "-z",
        f"{commit}^",
        commit,
        optional=True,
    )
    return frozenset(os.fsdecode(path) for path in output.split(b"\0") if path)


def _tree_blob(root: Path, commit: str, relative_path: str) -> str | None:
    output = _git_bytes(
        root,
        "--literal-pathspecs",
        "ls-tree",
        "-z",
        commit,
        "--",
        relative_path,
        optional=True,
    )
    records = [record for record in output.split(b"\0") if record]
    if not records:
        return None
    try:
        metadata, _ = records[0].split(b"\t", 1)
        mode, object_type, blob = metadata.split()
    except ValueError as exc:
        raise PlanLifecycleError("Git returned an invalid lifecycle tree entry") from exc
    if object_type != b"blob" or mode not in {b"100644", b"100755", b"120000"}:
        return None
    try:
        return blob.decode("ascii")
    except UnicodeDecodeError as exc:
        raise PlanLifecycleError("Git returned an invalid lifecycle tree identity") from exc


def _recorded_commit_paths(
    record: dict[str, object],
    *,
    owned_paths: frozenset[str],
) -> frozenset[str] | None:
    value = record.get("commit_paths")
    if value is None:
        return None
    if not isinstance(value, list):
        raise PlanLifecycleError("publication receipt has invalid lifecycle commit paths")
    paths = frozenset(value)
    if not paths or not paths <= owned_paths:
        raise PlanLifecycleError("publication receipt has invalid lifecycle commit ownership")
    return paths


def _commit_matches(
    root: Path,
    commit: str | None,
    *,
    parent: str | None,
    owned_paths: frozenset[str],
    expected_paths: frozenset[str] | None = None,
    source_relative: str | None = None,
    destination_relative: str | None = None,
    source_tracked: bool = False,
    destination_blob: str | None = None,
) -> bool:
    if commit is None or parent is None or _SHA_RE.fullmatch(commit) is None:
        return False
    if _git(root, "rev-parse", "--verify", f"{commit}^{{commit}}", optional=True) != commit:
        return False
    if _git(root, "rev-parse", "--verify", f"{commit}^", optional=True) != parent:
        return False
    if _git(root, "show", "-s", "--format=%s", commit, optional=True) != _PLAN_LIFECYCLE_COMMIT_MESSAGE:
        return False
    changed_paths = _commit_paths(root, commit)
    if not changed_paths or not changed_paths <= owned_paths:
        return False
    if expected_paths is not None and changed_paths != expected_paths:
        return False
    if source_tracked and source_relative is not None and _tree_blob(root, commit, source_relative) is not None:
        return False
    if destination_blob is not None and destination_relative is not None:
        if _tree_blob(root, commit, destination_relative) != destination_blob:
            return False
    return True


def _move_plan_bytes(source: Path, destination: Path, expected: tuple[int, str]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or destination.exists():
        if not _regular_file(destination) or _file_identity(destination) != expected:
            raise PlanLifecycleError("Done plan destination already contains different bytes")
        try:
            source.unlink()
        except OSError as exc:
            raise PlanLifecycleError("could not remove the completed in-progress plan") from exc
    else:
        try:
            shutil.move(str(source), str(destination))
        except OSError as exc:
            raise PlanLifecycleError("could not move the completed plan to Done") from exc
    if not _regular_file(destination) or _file_identity(destination) != expected:
        raise PlanLifecycleError("completed plan bytes changed during the Done move")
    if source.exists():
        raise PlanLifecycleError("completed plan move did not remove the in-progress source")


def finalize_completed_plan(
    root: Path,
    plan_path: Path,
    run_dir: Path,
    *,
    is_complete: bool = True,
    phase_hook: Callable[[str], None] | None = None,
    stage_hook: Callable[[str], None] | None = None,
) -> CompletedPlanLifecycle:
    """Move one complete plan and record only its owned Git lifecycle changes.

    ``phase_hook`` and ``stage_hook`` are intentionally limited to tests and
    crash-injection tools. Durable state is written before each phase callback,
    and staging intent is written before each Git index mutation, so a raised
    interruption can resume without replaying the move or creating a second
    commit.
    """
    if not is_complete:
        raise PlanLifecycleError("completed plan lifecycle requires a complete plan")

    root = Path(root)
    run_dir = Path(run_dir)
    source, destination, source_relative, destination_relative = _plan_paths(root, Path(plan_path))
    receipt_path = run_dir / "publication.json"
    receipt = _load_receipt(receipt_path)
    record = _lifecycle_record(receipt)
    git_state = _git_lifecycle_state(
        root,
        source_relative=source_relative,
        destination_relative=destination_relative,
    )
    owned_paths = frozenset({source_relative, destination_relative})
    if git_state is not None and git_state.staged_paths - owned_paths:
        raise PlanLifecycleError(
            "cannot finalize the plan with unrelated pre-existing staged changes"
        )
    if git_state is not None and record is None and git_state.staged_paths:
        raise PlanLifecycleError(
            "cannot establish lifecycle ownership over pre-existing staged changes"
        )

    source_present = _regular_file(source)
    destination_present = _regular_file(destination)
    if destination.is_symlink() or (destination.exists() and not destination_present):
        raise PlanLifecycleError("Done plan destination is not a regular file")

    moved = False
    if record is None:
        if not source_present:
            if destination_present:
                raise PlanLifecycleError(
                    "cannot establish ownership of an already-moved Done plan"
                )
            raise PlanLifecycleError("completed plan source file does not exist")
        expected = _file_identity(source)
        if destination_present and _file_identity(destination) != expected:
            raise PlanLifecycleError("Done plan destination already contains different bytes")
        record = {
            "schema": _PLAN_LIFECYCLE_SCHEMA,
            "phase": "prepared",
            "source": source_relative,
            "destination": destination_relative,
            "complete": True,
            "content_size": expected[0],
            "content_sha256": expected[1],
            "source_tracked": git_state.source_tracked if git_state is not None else False,
            "source_blob": git_state.source_blob if git_state is not None else None,
            "destination_tracked": (
                git_state.destination_tracked if git_state is not None else False
            ),
            "parent": git_state.head if git_state is not None else None,
        }
        _record_phase(receipt_path, receipt, record, "prepared", phase_hook)
        _move_plan_bytes(source, destination, expected)
        moved = True
        _record_phase(receipt_path, receipt, record, "moved", phase_hook)
    else:
        if record["source"] != source_relative or record["destination"] != destination_relative:
            raise PlanLifecycleError("publication receipt belongs to a different plan move")
        phase = record["phase"]
        staging = _lifecycle_staging(record)
        if git_state is not None and phase != "committed":
            recorded_source_blob = record.get("source_blob")
            if recorded_source_blob is not None and not isinstance(recorded_source_blob, str):
                raise PlanLifecycleError("publication receipt has invalid source blob identity")
            recorded_commit_paths = _recorded_commit_paths(record, owned_paths=owned_paths)
            source_is_expected_to_be_staged = (
                (
                    phase == "commit_pending"
                    and recorded_commit_paths is not None
                    and source_relative in recorded_commit_paths
                )
                or (
                    staging is not None
                    and staging.get("source_should_stage") is True
                )
            )
            source_tracked_record = record.get("source_tracked")
            if source_tracked_record is True and recorded_source_blob is None:
                raise PlanLifecycleError("publication receipt lacks source blob identity")
            if (
                source_tracked_record is True
                and not source_is_expected_to_be_staged
                and git_state.source_blob != recorded_source_blob
            ) or (
                source_tracked_record is False
                and git_state.source_blob is not None
            ):
                raise PlanLifecycleError("Git source blob diverged after its recorded lifecycle phase")
        if source_present:
            if phase != "prepared":
                raise PlanLifecycleError("completed plan source diverged after its recorded move")
            _require_record_bytes(source, record, label="in-progress source")
        if phase == "prepared":
            if not source_present and not destination_present:
                raise PlanLifecycleError("recorded plan move has neither source nor Done bytes")
            if source_present:
                _move_plan_bytes(
                    source,
                    destination,
                    (record["content_size"], record["content_sha256"]),
                )
                moved = True
            else:
                _require_record_bytes(destination, record, label="Done destination")
            _record_phase(receipt_path, receipt, record, "moved", phase_hook)
        elif phase in {"moved", "commit_pending", "committed"}:
            if source_present or not destination_present:
                raise PlanLifecycleError("recorded Done move no longer matches its source identity")
            _require_record_bytes(destination, record, label="Done destination")

    staging = _lifecycle_staging(record)
    if record["phase"] == "committed":
        if git_state is None:
            raise PlanLifecycleError("recorded lifecycle commit cannot be verified outside Git")
        commit = record.get("commit")
        parent = record.get("parent")
        expected_paths = _recorded_commit_paths(record, owned_paths=owned_paths)
        destination_blob = record.get("destination_blob_after")
        if destination_blob is not None and not isinstance(destination_blob, str):
            raise PlanLifecycleError("publication receipt has invalid destination blob identity")
        if not isinstance(commit, str) or not isinstance(parent, str) or not _commit_matches(
            root,
            commit,
            parent=parent,
            owned_paths=owned_paths,
            expected_paths=expected_paths,
            source_relative=source_relative,
            destination_relative=destination_relative,
            source_tracked=bool(record.get("source_tracked")),
            destination_blob=destination_blob,
        ):
            raise PlanLifecycleError("publication receipt does not prove the lifecycle commit")
        return CompletedPlanLifecycle(
            destination=destination,
            moved=moved,
            commit=commit,
            source_tracked=bool(record.get("source_tracked")),
            phase="committed",
        )

    if git_state is None:
        return CompletedPlanLifecycle(
            destination=destination,
            moved=moved,
            commit=None,
            source_tracked=bool(record.get("source_tracked")),
            phase="moved",
        )

    source_tracked = bool(record.get("source_tracked"))
    destination_tracked = bool(record.get("destination_tracked"))
    if not source_tracked and not destination_tracked:
        return CompletedPlanLifecycle(
            destination=destination,
            moved=moved,
            commit=None,
            source_tracked=False,
            phase="moved",
        )

    parent = record.get("parent")
    if parent is not None and not isinstance(parent, str):
        raise PlanLifecycleError("publication receipt has an invalid lifecycle parent")
    if parent is None:
        parent = git_state.head
        record["parent"] = parent
    if parent is None:
        raise PlanLifecycleError(
            "cannot create a lifecycle commit before the repository has a HEAD"
        )

    current_head = _git(root, "rev-parse", "--verify", "HEAD", optional=True) or None
    if current_head != parent:
        candidate = current_head if current_head is not None else None
        expected_paths = _recorded_commit_paths(record, owned_paths=owned_paths)
        destination_blob = record.get("destination_blob_after")
        if destination_blob is not None and not isinstance(destination_blob, str):
            raise PlanLifecycleError("publication receipt has invalid destination blob identity")
        if _commit_matches(
            root,
            candidate,
            parent=parent,
            owned_paths=owned_paths,
            expected_paths=expected_paths,
            source_relative=source_relative,
            destination_relative=destination_relative,
            source_tracked=source_tracked,
            destination_blob=destination_blob,
        ):
            record["commit"] = candidate
            _record_phase(receipt_path, receipt, record, "committed", phase_hook)
            return CompletedPlanLifecycle(
                destination=destination,
                moved=moved,
                commit=candidate,
                source_tracked=source_tracked,
                phase="committed",
            )
        raise PlanLifecycleError("Git HEAD diverged before the owned lifecycle commit")

    current_state = git_state
    if staging is None and record["phase"] != "commit_pending":
        destination_expected = None
        if destination_tracked:
            destination_expected = _working_tree_blob(root, destination_relative)
        elif not _is_ignored(root, destination_relative):
            destination_expected = _working_tree_blob(root, destination_relative)
        destination_should_stage = destination_expected is not None and (
            not destination_tracked
            or destination_expected != git_state.destination_blob
        )
        source_should_stage = source_tracked and git_state.source_blob is not None
        staging = {
            "phase": (
                "source_pending"
                if source_should_stage
                else "destination_pending"
                if destination_should_stage
                else "ready"
            ),
            "source_before": git_state.source_blob,
            "destination_before": git_state.destination_blob,
            "destination_expected": destination_expected,
            "destination_after": None,
            "source_should_stage": source_should_stage,
            "destination_should_stage": destination_should_stage,
            "source_staged_before": source_relative in git_state.staged_paths,
            "destination_staged_before": destination_relative in git_state.staged_paths,
        }
        record["staging"] = staging
        _save_lifecycle(receipt_path, receipt, record)
    elif staging is not None:
        recorded_source_blob = record.get("source_blob")
        if staging.get("source_before") != recorded_source_blob:
            raise PlanLifecycleError("lifecycle staging source identity changed")
        if staging.get("source_should_stage") is not (source_tracked and recorded_source_blob is not None):
            raise PlanLifecycleError("lifecycle staging source intent changed")

    if staging is not None:
        if current_state is None:
            raise PlanLifecycleError("lifecycle staging cannot be recovered outside Git")
        source_done = _source_stage_complete(
            current_state,
            source_relative=source_relative,
            staging=staging,
        )
        if staging["phase"] == "source_pending":
            if not source_done:
                if stage_hook is not None:
                    stage_hook("before_source_stage")
                _git(
                    root,
                    "--literal-pathspecs",
                    "add",
                    "-u",
                    "--",
                    source_relative,
                )
                if stage_hook is not None:
                    stage_hook("after_source_stage")
                current_state = _git_lifecycle_state(
                    root,
                    source_relative=source_relative,
                    destination_relative=destination_relative,
                )
                if current_state is None:
                    raise PlanLifecycleError("lifecycle staging cannot be inspected in Git")
                source_done = _source_stage_complete(
                    current_state,
                    source_relative=source_relative,
                    staging=staging,
                )
                if not source_done:
                    raise PlanLifecycleError("Git did not retain the staged source deletion")
            staging["phase"] = (
                "destination_pending"
                if staging["destination_should_stage"]
                else "ready"
            )
            _save_lifecycle(receipt_path, receipt, record)
        elif not source_done:
            raise PlanLifecycleError("lifecycle staging is missing its source deletion")

        if current_state is None:
            raise PlanLifecycleError("lifecycle staging cannot be recovered outside Git")
        if staging["phase"] == "destination_pending":
            destination_done = _destination_stage_complete(
                current_state,
                destination_relative=destination_relative,
                staging=staging,
            )
            if not destination_done:
                if stage_hook is not None:
                    stage_hook("before_destination_stage")
                _git(
                    root,
                    "--literal-pathspecs",
                    "add",
                    "-A",
                    "--",
                    destination_relative,
                )
                if stage_hook is not None:
                    stage_hook("after_destination_stage")
                current_state = _git_lifecycle_state(
                    root,
                    source_relative=source_relative,
                    destination_relative=destination_relative,
                )
                if current_state is None:
                    raise PlanLifecycleError("lifecycle staging cannot be inspected in Git")
                destination_done = _destination_stage_complete(
                    current_state,
                    destination_relative=destination_relative,
                    staging=staging,
                )
                if not destination_done:
                    raise PlanLifecycleError("Git did not retain the staged Done plan")
            staging["phase"] = "ready"
            if destination_relative in current_state.staged_paths:
                staging["destination_after"] = current_state.destination_blob
            _save_lifecycle(receipt_path, receipt, record)
        elif staging["phase"] == "ready":
            _destination_stage_complete(
                current_state,
                destination_relative=destination_relative,
                staging=staging,
            )

    if current_state is None:
        raise PlanLifecycleError("lifecycle staging cannot be recovered outside Git")
    staged_paths = current_state.staged_paths
    if staged_paths - owned_paths:
        raise PlanLifecycleError("lifecycle staging produced an unrelated Git path")
    if not staged_paths:
        return CompletedPlanLifecycle(
            destination=destination,
            moved=moved,
            commit=None,
            source_tracked=source_tracked,
            phase="moved",
        )

    recorded_commit_paths = _recorded_commit_paths(record, owned_paths=owned_paths)
    if recorded_commit_paths is not None and staged_paths != recorded_commit_paths:
        raise PlanLifecycleError("staged lifecycle paths diverged from the recorded ownership")
    record["commit_paths"] = sorted(staged_paths)
    if destination_relative in staged_paths:
        destination_blob = _index_blob(root, destination_relative)
        if destination_blob is None:
            raise PlanLifecycleError("Git did not retain the staged Done plan blob")
        record["destination_blob_after"] = destination_blob

    if record["phase"] != "commit_pending":
        _record_phase(receipt_path, receipt, record, "commit_pending", phase_hook)
    staged_after_phase = _staged_paths(root)
    if staged_after_phase != staged_paths:
        raise PlanLifecycleError("staged lifecycle paths changed before commit")
    head_after_phase = _git(root, "rev-parse", "--verify", "HEAD", optional=True) or None
    if head_after_phase != parent:
        raise PlanLifecycleError("Git HEAD diverged before the owned lifecycle commit")
    _git(
        root,
        "commit",
        "-m",
        _PLAN_LIFECYCLE_COMMIT_MESSAGE,
    )
    commit = _git(root, "rev-parse", "--verify", "HEAD")
    if not _commit_matches(
        root,
        commit,
        parent=parent,
        owned_paths=owned_paths,
        expected_paths=staged_paths,
        source_relative=source_relative,
        destination_relative=destination_relative,
        source_tracked=source_tracked,
        destination_blob=record.get("destination_blob_after"),
    ):
        raise PlanLifecycleError("lifecycle commit did not contain only the owned plan paths")
    record["commit"] = commit
    _record_phase(receipt_path, receipt, record, "committed", phase_hook)
    return CompletedPlanLifecycle(
        destination=destination,
        moved=moved,
        commit=commit,
        source_tracked=source_tracked,
        phase="committed",
    )


def publish_completed_run(root: Path, run_dir: Path, *, source_ref: str = "HEAD") -> str | None:
    """Publish only after verified plan completion; never mutate the execution tree.

    Local Git configuration is an explicit owner grant, not tracked project code.
    Both aflow.publishRemote and aflow.publishBranch are required to opt in.
    """
    remote = _git(root, "config", "--local", "--get", "aflow.publishRemote", optional=True)
    branch = _git(root, "config", "--local", "--get", "aflow.publishBranch", optional=True)
    if not remote and not branch:
        return None
    path = run_dir / "publication.json"
    receipt = _load_receipt(path)
    receipt.update(status="pending", remote=remote, branch=branch)

    def save() -> None:
        _write_atomic_receipt(path, receipt)

    worktree: Path | None = None
    try:
        if not remote or not branch or remote.startswith("-") or branch.startswith("-"):
            raise PublicationError("configure both aflow.publishRemote and aflow.publishBranch")
        if remote not in _git(root, "remote").splitlines():
            raise PublicationError("publication remote is not a configured Git remote")
        _git(root, "check-ref-format", f"refs/heads/{branch}")
        if _git(root, "status", "--porcelain", "--untracked-files=normal"):
            raise PublicationError("publication requires a clean completed execution checkout")
        candidate = _git(root, "rev-parse", f"{source_ref}^{{commit}}")
        receipt["source_commit"] = candidate
        save()
        _git(root, "fetch", remote, f"refs/heads/{branch}")
        published = _git(root, "rev-parse", "FETCH_HEAD^{commit}")
        base = _git(root, "merge-base", candidate, published)
        if base == candidate:
            receipt.update(status="published", commit=published)
            save()
            return published
        if base != published:
            # Reconcile accepted remote history outside every active checkout.
            worktree = Path(tempfile.mkdtemp(prefix="aflow-publication-")) / "checkout"
            receipt["checkout"] = str(worktree)
            save()
            _git(root, "worktree", "add", "--detach", str(worktree), candidate)
            _git(worktree, "merge", "--no-edit", published)
            candidate = _git(worktree, "rev-parse", "HEAD")
        # Normal fast-forward-only remote admission. Never force or overwrite main.
        _git(root, "push", remote, f"{candidate}:refs/heads/{branch}")
        receipt.update(status="published", commit=candidate)
        save()
        if worktree is not None:
            _git(root, "worktree", "remove", str(worktree))
        return candidate
    except (PublicationError, subprocess.TimeoutExpired, OSError) as exc:
        receipt.update(status="failed", error=type(exc).__name__)
        save()
        detail = f"; preserved merge checkout: {worktree}" if worktree else ""
        raise PublicationError(f"publication to {remote}/{branch} did not finish{detail}") from exc
