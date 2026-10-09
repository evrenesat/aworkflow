"""Current file operations for the server's configuration pair documents.

This thin adapter owns the protected document reads, the document-name to
path mapping, and the bounded server audit records.  The durable pair
transaction itself stays with the core owner in :mod:`aflow.config_pair`;
services call :func:`aflow.config_pair.commit_configuration_pair` under
:func:`aflow.config_pair.configuration_pair_lock` and use these helpers
only for reads and audit.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat as stat_module

from .config_validation import (
    CONFIG_DOCUMENT_NAMES,
    MAX_CONFIG_DOCUMENT_BYTES,
    ProjectConfigError,
)

_AUDIT_SCHEMA_VERSION = 1


def document_path(config_dir: Path, name: str) -> Path:
    """Map the only supported document names to their exact contained paths."""
    if name not in CONFIG_DOCUMENT_NAMES:
        raise ProjectConfigError("unsupported configuration document name")
    return config_dir / name


def read_protected_document(
    config_dir: Path, name: str
) -> tuple[bytes, str] | None:
    """Read one protected document; ``None`` when it does not exist yet."""
    path = document_path(config_dir, name)
    if path.is_symlink():
        raise ProjectConfigError(f"{name} must not be a symlink")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProjectConfigError(f"{name} is unavailable") from exc
    if not stat_module.S_ISREG(info.st_mode):
        raise ProjectConfigError(f"{name} must be a regular file")
    if info.st_nlink != 1:
        raise ProjectConfigError(f"{name} must not be a hard-linked file")
    if info.st_size > MAX_CONFIG_DOCUMENT_BYTES:
        raise ProjectConfigError(f"{name} exceeds the maximum supported size")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ProjectConfigError(f"{name} is unreadable") from exc
    if len(payload) > MAX_CONFIG_DOCUMENT_BYTES:
        raise ProjectConfigError(f"{name} exceeds the maximum supported size")
    try:
        return payload, payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectConfigError(f"{name} is not valid UTF-8 text") from exc


def append_audit_line(
    audit_path: Path,
    *,
    project_id: str,
    outcome: str,
    old_revision: str | None,
    new_revision: str | None,
    caller_scope: str,
) -> None:
    """Append one bounded, redacted audit record to server state."""
    record = {
        "schema_version": _AUDIT_SCHEMA_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "project_id": project_id,
        "outcome": outcome,
        "old_revision": old_revision,
        "new_revision": new_revision,
        "caller_scope": caller_scope,
    }
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(audit_path, flags, 0o600)
        try:
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Audit metadata is advisory; a save must never fail because of it.
        pass


__all__ = [
    "append_audit_line",
    "document_path",
    "read_protected_document",
]
