#!/usr/bin/env python3
"""Prepare or roll back the explicit single-project registry migration."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Any

_SCHEMA_VERSION = 1
_PROJECT_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_RELEASE_ID = re.compile(r"^[0-9a-f]{40}$")


class MigrationError(RuntimeError):
    pass


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _regular_file(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise MigrationError(f"{label} must be a regular non-symlink file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise MigrationError(f"{label} is unreadable") from exc


def _contained_path(root: Path, path: Path, label: str) -> Path:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise MigrationError(f"{label} must be contained by the project root") from exc
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise MigrationError(f"{label} contains a symlink component")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise MigrationError(f"{label} is unavailable or outside the project root") from exc
    return resolved


def _exact_git_root(project_root: Path) -> None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(project_root), "rev-parse", "--show-toplevel"),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MigrationError("project Git identity could not be verified") from exc
    if completed.returncode != 0:
        raise MigrationError("project root must be a Git repository")
    try:
        observed = Path(completed.stdout.strip()).resolve(strict=True)
    except OSError as exc:
        raise MigrationError("project Git identity could not be verified") from exc
    if os.path.normcase(str(observed)) != os.path.normcase(str(project_root)):
        raise MigrationError("project path must name the exact Git repository root")


def _validate_release(release: Path) -> None:
    if release.is_symlink() or not release.is_dir() or _RELEASE_ID.fullmatch(release.name) is None:
        raise MigrationError("release must be an immutable commit-addressed directory")
    manifest = release / "release-manifest.sha256"
    raw = _regular_file(manifest, "release manifest").decode("utf-8")
    lines = raw.splitlines()
    if not lines or lines[0] != f"source_commit={release.name}":
        raise MigrationError("release manifest identity is stale")
    for line in lines[1:]:
        digest, separator, relative_text = line.partition("  ")
        relative = PurePosixPath(relative_text)
        if (
            separator != "  "
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise MigrationError("release manifest entry is invalid")
        candidate = release.joinpath(*relative.parts)
        if _hash(_regular_file(candidate, "release snapshot file")) != digest:
            raise MigrationError("release snapshot hashes are stale")


def _load_registry(path: Path, managed_root: Path) -> tuple[dict[str, Any], bytes | None]:
    if not path.exists():
        return {"schema_version": _SCHEMA_VERSION, "projects": []}, None
    data = _regular_file(path, "project registry")
    try:
        payload = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError("project registry is unreadable or corrupt") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "projects"}:
        raise MigrationError("project registry shape is invalid")
    if payload["schema_version"] != _SCHEMA_VERSION or not isinstance(payload["projects"], list):
        raise MigrationError("project registry schema version is unsupported")
    ids: set[str] = set()
    roots: list[Path] = []
    for record in payload["projects"]:
        expected = {"schema_version", "id", "display_name", "relative_root", "created_at", "updated_at"}
        if not isinstance(record, dict) or set(record) != expected:
            raise MigrationError("project registry record shape is invalid")
        if (
            type(record["schema_version"]) is not int
            or record["schema_version"] != _SCHEMA_VERSION
            or not isinstance(record["id"], str)
            or _PROJECT_ID.fullmatch(record["id"]) is None
            or not isinstance(record["display_name"], str)
            or not record["display_name"].strip()
            or len(record["display_name"]) > 200
            or not isinstance(record["relative_root"], str)
        ):
            raise MigrationError("project registry record is invalid")
        try:
            created = datetime.fromisoformat(record["created_at"])
            updated = datetime.fromisoformat(record["updated_at"])
        except (TypeError, ValueError) as exc:
            raise MigrationError("project registry timestamps are invalid") from exc
        if created.tzinfo is None or updated.tzinfo is None or updated < created:
            raise MigrationError("project registry timestamps are invalid")
        if record["id"] in ids:
            raise MigrationError("project registry contains duplicate ids")
        relative = PurePosixPath(record["relative_root"])
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise MigrationError("project registry contains an unsafe root")
        root = managed_root
        for part in relative.parts:
            root /= part
            if root.is_symlink():
                raise MigrationError("project registry root contains a symlink")
        try:
            root = root.resolve(strict=True)
            root.relative_to(managed_root)
        except (OSError, ValueError) as exc:
            raise MigrationError("project registry root is unavailable") from exc
        _exact_git_root(root)
        if any(root == other or root in other.parents or other in root.parents for other in roots):
            raise MigrationError("project registry contains duplicate or overlapping roots")
        ids.add(record["id"])
        roots.append(root)
    return payload, data


def _config_action(project_root: Path, aflow_data: bytes, workflows_data: bytes) -> tuple[str, Path]:
    config_dir = project_root / ".aflow" / "config"
    if config_dir.is_symlink():
        raise MigrationError("destination config directory must not be a symlink")
    aflow_destination = config_dir / "aflow.toml"
    workflows_destination = config_dir / "workflows.toml"
    existing = (aflow_destination.exists(), workflows_destination.exists())
    if existing == (True, True):
        if (
            _regular_file(aflow_destination, "destination aflow.toml") != aflow_data
            or _regular_file(workflows_destination, "destination workflows.toml") != workflows_data
        ):
            raise MigrationError("destination config exists with different bytes; refusing overwrite")
        return "reuse", config_dir
    if any(existing):
        raise MigrationError("destination config pair is incomplete; refusing overwrite")
    if config_dir.exists():
        if not config_dir.is_dir() or any(config_dir.iterdir()):
            raise MigrationError("destination config directory is not empty; refusing overwrite")
    return "create", config_dir


def _transaction_payload(**values: Any) -> bytes:
    return (json.dumps({"schema_version": _SCHEMA_VERSION, **values}, indent=2, sort_keys=True) + "\n").encode()


def _restore_transaction(transaction_dir: Path, *, require_current: bool) -> None:
    manifest_path = transaction_dir / "transaction.json"
    manifest = json.loads(_regular_file(manifest_path, "migration transaction").decode("utf-8"))
    if manifest.get("schema_version") != _SCHEMA_VERSION:
        raise MigrationError("migration transaction version is unsupported")
    registry = Path(manifest["registry_path"])
    config_dir = Path(manifest["config_dir"])
    aflow_destination = config_dir / "aflow.toml"
    workflows_destination = config_dir / "workflows.toml"

    if require_current:
        current_registry = _regular_file(registry, "current project registry")
        if _hash(current_registry) != manifest["registry_after_sha256"]:
            raise MigrationError("project registry changed after migration; refusing rollback")
    before = None
    if manifest["registry_existed"]:
        before = _regular_file(transaction_dir / "registry.before.json", "registry backup")
        if _hash(before) != manifest["registry_before_sha256"]:
            raise MigrationError("registry backup hash is invalid")
    if manifest["config_action"] == "create" and config_dir.exists():
        if config_dir.is_symlink() or not config_dir.is_dir():
            raise MigrationError("migrated config directory changed; refusing rollback")
        names = {path.name for path in config_dir.iterdir()}
        if names != {"aflow.toml", "workflows.toml"}:
            raise MigrationError("migrated config directory has new content; refusing rollback")
        if (
            _hash(_regular_file(aflow_destination, "migrated aflow.toml")) != manifest["aflow_sha256"]
            or _hash(_regular_file(workflows_destination, "migrated workflows.toml")) != manifest["workflows_sha256"]
        ):
            raise MigrationError("migrated config changed; refusing rollback")

    if manifest["config_action"] == "create" and config_dir.exists():
        aflow_destination.unlink()
        workflows_destination.unlink()
        config_dir.rmdir()
    if manifest["registry_existed"]:
        assert before is not None
        _write_atomic(registry, before)
    elif registry.exists():
        if require_current and _hash(_regular_file(registry, "current project registry")) != manifest["registry_after_sha256"]:
            raise MigrationError("project registry changed after migration; refusing rollback")
        registry.unlink()
        directory = os.open(registry.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    manifest["status"] = "rolled_back"
    manifest["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
    _write_atomic(manifest_path, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())


def prepare(args: argparse.Namespace) -> Path | None:
    managed_source = Path(args.managed_projects_root).expanduser()
    if managed_source.is_symlink() or not managed_source.is_dir():
        raise MigrationError("managed projects root must be a real directory")
    managed_root = managed_source.resolve(strict=True)
    project_source = Path(args.project_root).expanduser()
    if project_source.is_symlink() or not project_source.is_dir():
        raise MigrationError("project root must be a real directory")
    project_root = project_source.resolve(strict=True)
    try:
        relative_root = project_root.relative_to(managed_root).as_posix()
    except ValueError as exc:
        raise MigrationError("project root must be below the managed projects root") from exc
    if not relative_root or relative_root == ".":
        raise MigrationError("managed projects root cannot itself be registered")
    _exact_git_root(project_root)
    if _PROJECT_ID.fullmatch(args.project_id) is None:
        raise MigrationError("project id must be a path-safe slug")
    if not args.display_name.strip() or len(args.display_name) > 200 or any(marker in args.display_name for marker in ("\x00", "\n", "\r")):
        raise MigrationError("project display name is invalid")

    aflow_source = _contained_path(project_root, Path(args.source_aflow_config), "source aflow.toml")
    workflows_source = _contained_path(project_root, Path(args.source_workflows_config), "source workflows.toml")
    aflow_data = _regular_file(aflow_source, "source aflow.toml")
    workflows_data = _regular_file(workflows_source, "source workflows.toml")
    try:
        tomllib.loads(aflow_data.decode("utf-8"))
        tomllib.loads(workflows_data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise MigrationError("source config pair must be valid UTF-8 TOML") from exc
    action, config_dir = _config_action(project_root, aflow_data, workflows_data)

    release = Path(args.release).resolve(strict=True)
    _validate_release(release)
    registry = Path(args.registry_path).expanduser().absolute()
    if registry.is_symlink() or not registry.parent.is_dir() or registry.parent.is_symlink():
        raise MigrationError("project registry parent must be a real directory")
    payload, registry_before = _load_registry(registry, managed_root)
    for record in payload["projects"]:
        existing_root = managed_root.joinpath(*PurePosixPath(record["relative_root"]).parts)
        if record["id"] == args.project_id:
            raise MigrationError("project id is already registered")
        if project_root == existing_root or project_root in existing_root.parents or existing_root in project_root.parents:
            raise MigrationError("project root overlaps an existing registration")
    timestamp = datetime.now(timezone.utc).isoformat()
    payload["projects"].append(
        {
            "schema_version": _SCHEMA_VERSION,
            "id": args.project_id,
            "display_name": args.display_name,
            "relative_root": relative_root,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    )
    payload["projects"].sort(key=lambda record: record["id"])
    registry_after = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()

    print(f"release snapshot: {release.name}")
    print(f"project registration: {args.project_id} -> {relative_root}")
    print(f"config action: {action} at {config_dir}")
    print(f"registry action: {'update' if registry_before is not None else 'create'} at {registry}")
    if not args.apply:
        print("dry-run: migration validation passed; no files changed")
        return None

    backup_root = Path(args.backup_root).expanduser().absolute()
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    transaction_dir = Path(tempfile.mkdtemp(prefix="migration-", dir=backup_root))
    transaction_dir.chmod(0o700)
    manifest_path = transaction_dir / "transaction.json"
    manifest: dict[str, Any] = {
        "status": "prepared",
        "created_at": timestamp,
        "release_identity": release.name,
        "managed_projects_root": str(managed_root),
        "project_root": str(project_root),
        "project_id": args.project_id,
        "config_dir": str(config_dir),
        "config_action": action,
        "aflow_sha256": _hash(aflow_data),
        "workflows_sha256": _hash(workflows_data),
        "registry_path": str(registry),
        "registry_existed": registry_before is not None,
        "registry_before_sha256": _hash(registry_before) if registry_before is not None else None,
        "registry_after_sha256": _hash(registry_after),
    }
    if registry_before is not None:
        _write_atomic(transaction_dir / "registry.before.json", registry_before)
    _write_atomic(manifest_path, _transaction_payload(**manifest))

    try:
        if action == "create":
            aflow_dir = project_root / ".aflow"
            if aflow_dir.is_symlink():
                raise MigrationError("project .aflow path must not be a symlink")
            aflow_dir.mkdir(mode=0o700, exist_ok=True)
            if config_dir.exists():
                config_dir.rmdir()
            staging = Path(tempfile.mkdtemp(prefix=".config.migration-", dir=aflow_dir))
            try:
                _write_atomic(staging / "aflow.toml", aflow_data)
                _write_atomic(staging / "workflows.toml", workflows_data)
                os.replace(staging, config_dir)
                directory = os.open(aflow_dir, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
            manifest["status"] = "config_published"
            _write_atomic(manifest_path, _transaction_payload(**manifest))
        _write_atomic(registry, registry_after)
        manifest["status"] = "complete"
        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_atomic(manifest_path, _transaction_payload(**manifest))
    except BaseException:
        _restore_transaction(transaction_dir, require_current=False)
        raise

    print(f"migration transaction: {transaction_dir}")
    return transaction_dir


def rollback(args: argparse.Namespace) -> None:
    transaction_dir = Path(args.transaction).expanduser().resolve(strict=True)
    if transaction_dir.is_symlink() or not transaction_dir.is_dir():
        raise MigrationError("migration transaction must be a real directory")
    if not args.apply:
        manifest = json.loads(_regular_file(transaction_dir / "transaction.json", "migration transaction").decode("utf-8"))
        print(f"dry-run: would roll back project {manifest.get('project_id')} from {transaction_dir}")
        return
    _restore_transaction(transaction_dir, require_current=True)
    print(f"migration rolled back: {transaction_dir}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    prepare_command = commands.add_parser("prepare")
    prepare_command.add_argument("--release", required=True)
    prepare_command.add_argument("--managed-projects-root", required=True)
    prepare_command.add_argument("--project-root", required=True)
    prepare_command.add_argument("--project-id", required=True)
    prepare_command.add_argument("--display-name", required=True)
    prepare_command.add_argument("--source-aflow-config", required=True)
    prepare_command.add_argument("--source-workflows-config", required=True)
    prepare_command.add_argument("--registry-path", required=True)
    prepare_command.add_argument("--backup-root", required=True)
    prepare_command.add_argument("--apply", action="store_true")
    prepare_command.set_defaults(handler=prepare)
    rollback_command = commands.add_parser("rollback")
    rollback_command.add_argument("--transaction", required=True)
    rollback_command.add_argument("--apply", action="store_true")
    rollback_command.set_defaults(handler=rollback)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        args.handler(args)
    except (MigrationError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"aflowd migration failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
