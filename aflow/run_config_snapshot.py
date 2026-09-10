"""Compatibility snapshots and the lock for a configuration pair.

Snapshots are ordinary launch-time diagnostic copies.  They remain readable
for legacy inspection and may preserve their historical origin metadata, but
their copied TOML and old fingerprints never decide whether a current run may
execute.  New current-source reads belong in :mod:`aflow.live_config`, which
holds :func:`configuration_pair_lock` while parsing ``aflow.toml`` and its
optional sibling ``workflows.toml``.

Snapshot creation still uses exclusive atomic writes and resolves the
schema-defined relative ``worktree_root`` against the selected source
directory so old diagnostic copies retain their original meaning.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import time
from typing import Iterator

SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_DIR_NAME = "config"
SNAPSHOT_MANIFEST_NAME = "snapshot.json"
PAIR_LOCK_NAME = ".aflow-config-pair.lock"
DOCUMENT_NAMES = ("aflow.toml", "workflows.toml")
_RELATIVE_ROOT_LIMIT = 64 * 1024


class SnapshotError(RuntimeError):
    """A run configuration snapshot could not be created or trusted."""


@dataclass(frozen=True)
class RunConfigSnapshot:
    run_id: str
    directory: Path
    config_path: Path
    workflows_path: Path
    manifest: dict

    @property
    def fingerprint(self) -> str | None:
        """Return an old diagnostic fingerprint, when one was recorded."""
        origin = self.manifest.get("origin")
        if not isinstance(origin, dict):
            return None
        value = origin.get("config_fingerprint")
        return value if isinstance(value, str) else None

    @property
    def origin_config_path(self) -> str | None:
        """Return the legacy origin hint, if the manifest recorded one."""
        origin = self.manifest.get("origin")
        if not isinstance(origin, dict):
            return None
        value = origin.get("config_path")
        return value if isinstance(value, str) and value.strip() else None

    @property
    def workflow_name(self) -> str | None:
        origin = self.manifest.get("origin")
        if not isinstance(origin, dict):
            return None
        value = origin.get("workflow_name")
        return value if isinstance(value, str) and value.strip() else None


@contextmanager
def configuration_pair_lock(pair_dir: Path) -> Iterator[Path]:
    """Serialize snapshot reads and global saves for one configuration pair.

    The lock file lives next to the pair it guards, so global UI saves and
    run-reservation snapshots block each other but unrelated repositories or
    legacy project-local pairs remain independent. On a read-only file system
    (for example a hardened deployment whose unit cannot write the global
    configuration directory) the lock degrades to best-effort: an existing
    lock file is still honored, and with none available reads proceed
    unlocked because writes are impossible there anyway.
    """
    pair_dir = Path(pair_dir)
    try:
        pair_dir.mkdir(parents=True, exist_ok=True)
        lock_path = pair_dir / PAIR_LOCK_NAME
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        if exc.errno not in (errno.EROFS, errno.EPERM, errno.EACCES):
            raise
        existing = pair_dir / PAIR_LOCK_NAME
        if existing.is_file() and not existing.is_symlink():
            fd = os.open(existing, os.O_WRONLY | os.O_NOFOLLOW)
            lock_path = existing
        else:
            yield None
            return
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield lock_path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def snapshot_directory(repo_root: Path, run_id: str) -> Path:
    return Path(repo_root) / ".aflow" / "runs" / run_id / SNAPSHOT_DIR_NAME


def _read_stable(path: Path) -> bytes:
    """Read a file, rejecting unstable manual edits around the read."""
    try:
        first_stat = path.stat()
        if first_stat.st_size > _RELATIVE_ROOT_LIMIT:
            raise SnapshotError(f"configuration document is too large: {path}")
        payload = path.read_bytes()
        second_stat = path.stat()
    except OSError as exc:
        raise SnapshotError(f"unable to read configuration document {path}: {exc}") from exc
    identity = ("st_ino", "st_mtime_ns", "st_size")
    if any(getattr(first_stat, field) != getattr(second_stat, field) for field in identity):
        raise SnapshotError(f"configuration document changed while reading: {path}")
    return payload


def _write_exclusive(path: Path, payload: bytes) -> None:
    if path.exists():
        raise SnapshotError(f"refusing to overwrite existing snapshot file: {path}")
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(temp, path)
    except FileExistsError:
        temp.unlink(missing_ok=True)
        raise SnapshotError(f"snapshot file already exists: {path}") from None
    finally:
        temp.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _resolve_relative_paths(aflow_bytes: bytes, origin_dir: Path) -> bytes:
    """Rewrite schema-defined relative filesystem paths to absolute form.

    Only ``[aflow] worktree_root`` is a filesystem path in the schema; it is
    resolved against the original configuration location so the snapshot
    keeps its meaning wherever the global pair later moves. Prompt strings
    and repository-relative plan paths are never rewritten.
    """
    if b"worktree_root" not in aflow_bytes:
        return aflow_bytes
    import tomlkit

    document = tomlkit.parse(aflow_bytes.decode("utf-8"))
    aflow_section = document.get("aflow")
    if not isinstance(aflow_section, dict):
        return aflow_bytes
    raw_root = aflow_section.get("worktree_root")
    if not isinstance(raw_root, str) or not raw_root.strip():
        return aflow_bytes
    if raw_root.startswith("~") or os.path.isabs(raw_root):
        return aflow_bytes
    resolved = (origin_dir / raw_root).resolve()
    aflow_section["worktree_root"] = str(resolved)
    return tomlkit.dumps(document).encode("utf-8")


def create_run_config_snapshot(
    *,
    repo_root: Path,
    run_id: str,
    config_path: Path,
    workflow_name: str,
    fingerprint: str | None = None,
    loader=None,
) -> RunConfigSnapshot:
    """Capture the effective workflow pair for a durably reserved run.

    ``fingerprint`` is retained only as optional diagnostic metadata for old
    callers. It is never recomputed or compared. Creation is exclusive: a
    concurrent launch of the same run id reuses the existing snapshot.
    ``loader`` defaults to the production loader and still validates the pair
    before a diagnostic copy is published.
    """
    if loader is None:
        from aflow.config import load_workflow_config as loader  # noqa: F811
    origin_config = Path(config_path)
    if not origin_config.is_file() or origin_config.is_symlink():
        raise SnapshotError(f"workflow configuration is not a regular file: {origin_config}")
    origin_dir = origin_config.parent
    directory = snapshot_directory(repo_root, run_id)

    with configuration_pair_lock(origin_dir):
        existing = load_run_config_snapshot(repo_root, run_id)
        if existing is not None:
            return existing

        aflow_bytes = _read_stable(origin_config)
        workflows_path = origin_config.with_name("workflows.toml")
        workflows_bytes = (
            _read_stable(workflows_path) if workflows_path.is_file() else None
        )
        # Re-read after the stat-verified reads: the loader validates the exact
        # pair that is about to be serialized through the production validator.
        candidate_dir = directory.with_name(f".{run_id}.config.candidate")
        try:
            candidate_dir.mkdir(parents=True, exist_ok=True)
            resolved_aflow = _resolve_relative_paths(aflow_bytes, origin_dir)
            (candidate_dir / "aflow.toml").write_bytes(resolved_aflow)
            if workflows_bytes is not None:
                (candidate_dir / "workflows.toml").write_bytes(workflows_bytes)
            loader(candidate_dir / "aflow.toml")
            directory.mkdir(parents=True, exist_ok=True)
            _write_exclusive(directory / "aflow.toml", resolved_aflow)
            if workflows_bytes is not None:
                _write_exclusive(directory / "workflows.toml", workflows_bytes)
            manifest = {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "run_id": run_id,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "origin": {
                    "config_path": str(origin_config.resolve()),
                    "workflows_path": str(workflows_path.resolve())
                    if workflows_bytes is not None
                    else None,
                    "workflow_name": workflow_name,
                },
            }
            if fingerprint is not None:
                manifest["origin"]["config_fingerprint"] = fingerprint
            _write_exclusive(
                directory / SNAPSHOT_MANIFEST_NAME,
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n",
            )
        finally:
            for leftover in candidate_dir.iterdir():
                leftover.unlink(missing_ok=True)
            candidate_dir.rmdir()
    return load_run_config_snapshot(repo_root, run_id)  # type: ignore[return-value]


def copy_run_config_snapshot(
    repo_root: Path,
    *,
    source: RunConfigSnapshot,
    run_id: str,
    workflow_name: str,
    fingerprint: str | None = None,
) -> RunConfigSnapshot:
    """Give a resumed successor run its own snapshot copied from its source.

    The successor's manifest origin stays the source run's original
    configuration location for diagnostic continuity. ``fingerprint`` is
    accepted for compatibility but is never compared or recomputed.
    """
    directory = snapshot_directory(repo_root, run_id)
    with configuration_pair_lock(source.directory):
        existing = load_run_config_snapshot(repo_root, run_id)
        if existing is not None:
            return existing
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.mkdir(parents=True, exist_ok=True)
        for name in DOCUMENT_NAMES:
            origin = source.directory / name
            if origin.is_file():
                _write_exclusive(directory / name, origin.read_bytes())
        manifest = dict(source.manifest)
        manifest["run_id"] = run_id
        manifest["copied_from_run_id"] = source.run_id
        manifest["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_exclusive(
            directory / SNAPSHOT_MANIFEST_NAME,
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
    return load_run_config_snapshot(repo_root, run_id)  # type: ignore[return-value]


def load_run_config_snapshot(repo_root: Path, run_id: str) -> RunConfigSnapshot | None:
    """Return the snapshot for a run, or None for legacy runs without one."""
    directory = snapshot_directory(repo_root, run_id)
    manifest_path = directory / SNAPSHOT_MANIFEST_NAME
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError(
            f"run {run_id} has an unreadable configuration snapshot manifest: {exc}"
        ) from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("origin"), dict):
        raise SnapshotError(
            f"run {run_id} has a malformed configuration snapshot manifest"
        )
    return RunConfigSnapshot(
        run_id=run_id,
        directory=directory,
        config_path=directory / "aflow.toml",
        workflows_path=directory / "workflows.toml",
        manifest=manifest,
    )
