"""Small, identity-bound terminal proof retained after run directory pruning."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .models import LaunchManifest
from .persistence import (
    PersistenceError,
    _contained_directory,
    _write_atomic_bytes,
    validate_run_id,
)

SCHEMA_VERSION = 1
MAX_SUMMARY_BYTES = 2048
TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted", "owner_stopped"})
PROVENANCE = {"controller_run_json", "owner_stop_phase"}
FIELDS = frozenset({
    "schema_version", "run_id", "project_root", "intended_unit",
    "status", "ended_at", "provenance",
})


def _path(root: Path, run_id: str, *, create: bool) -> Path:
    valid = validate_run_id(run_id)
    project = Path(root).resolve(strict=True)
    if (project / ".aflow").is_symlink() or (project / ".aflow" / "run-summaries").is_symlink():
        raise PersistenceError("run summary directory is unsafe")
    if create:
        directory = _contained_directory(project, ".aflow", "run-summaries")
    else:
        directory = project / ".aflow" / "run-summaries"
        if not directory.exists() and not directory.is_symlink():
            return directory / f"{valid}.json"
        if directory.is_symlink() or not directory.is_dir():
            raise PersistenceError("run summary directory is unsafe")
        if directory.resolve(strict=True) != directory:
            raise PersistenceError("run summary directory is unsafe")
    path = directory / f"{valid}.json"
    if path.is_symlink():
        raise PersistenceError("run summary may not be a symlink")
    return path


def _validated(
    payload: object, root: Path, run_id: str, manifest: LaunchManifest
) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping) or set(payload) != FIELDS:
        return None
    expected_root = str(Path(root).resolve(strict=True))
    if (
        manifest.run_id != run_id
        or manifest.project_root != expected_root
        or manifest.intended_unit != f"aflow-run-{run_id}.service"
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("run_id") != run_id
        or payload.get("project_root") != expected_root
        or payload.get("intended_unit") != manifest.intended_unit
        or not isinstance(payload.get("status"), str)
        or payload.get("status") not in TERMINAL_STATUSES
        or not isinstance(payload.get("provenance"), str)
        or payload.get("provenance") not in PROVENANCE
        or (payload.get("status") == "owner_stopped")
        != (payload.get("provenance") == "owner_stop_phase")
        or (payload.get("ended_at") is not None and (
            not isinstance(payload.get("ended_at"), str)
            or not 0 < len(payload["ended_at"]) <= 64
        ))
    ):
        return None
    if payload["ended_at"] is not None:
        try:
            if datetime.fromisoformat(payload["ended_at"]).tzinfo is None:
                return None
        except ValueError:
            return None
    return dict(payload)


def read_terminal_summary(
    root: Path, run_id: str, manifest: LaunchManifest
) -> dict[str, Any] | None:
    """Return only an exact, bounded summary; invalid data never proves completion."""
    try:
        path = _path(root, run_id, create=False)
        if not path.exists() or not path.is_file() or path.stat().st_size > MAX_SUMMARY_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _validated(payload, root, run_id, manifest)
    except (OSError, ValueError, RuntimeError):
        return None


def retain_terminal_summary(
    root: Path, run_id: str, manifest: LaunchManifest, *,
    status: str, ended_at: str | None, provenance: str,
) -> bool:
    """Atomically publish validated proof and reread it before source deletion."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "project_root": str(Path(root).resolve(strict=True)),
        "intended_unit": manifest.intended_unit,
        "status": status,
        "ended_at": ended_at,
        "provenance": provenance,
    }
    if _validated(payload, root, run_id, manifest) is None:
        return False
    try:
        path = _path(root, run_id, create=True)
        if path.exists():
            return read_terminal_summary(root, run_id, manifest) == payload
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(encoded) > MAX_SUMMARY_BYTES:
            return False
        _write_atomic_bytes(path, encoded)
        return read_terminal_summary(root, run_id, manifest) == payload
    except (OSError, ValueError, RuntimeError):
        return False
