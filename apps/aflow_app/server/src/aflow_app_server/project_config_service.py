"""Atomic, revision-checked project configuration text service.

The service owns exactly two documents per registered project,
``.aflow/config/aflow.toml`` and ``.aflow/config/workflows.toml``.  It never
reads or writes another project file, never starts or stops workflow units,
and does not gate a valid save on the state or historical configuration of a
workflow run.

The configuration exception types, report shapes, revision and bounds
helpers, and the in-memory candidate validator live in
:mod:`aflow_app_server.config_validation`; this module re-exports them so
the compatibility import path keeps working.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat as stat_module
import tempfile

from .config_validation import (
    CONFIG_DOCUMENT_NAMES,
    MAX_CONFIG_DOCUMENT_BYTES,
    MAX_ISSUE_MESSAGE_CHARS,  # noqa: F401  compatibility re-export
    MAX_VALIDATION_ISSUES,
    ConfigValidationIssue,  # noqa: F401  compatibility re-export
    ConfigValidationReport,
    ProjectConfigError,
    ProjectConfigRevisionConflict,
    check_document_text,  # noqa: F401  compatibility re-export
    combined_revision,
    validate_candidate_pair,
)
from .control_plane_service import (
    ControlPlaneService,
    ControlPlaneUnavailableError,
    ProjectNotAllowedError,
)
from .project_registry import ProjectRegistry, ProjectRegistryError


_AUDIT_SCHEMA_VERSION = 1
_DOCUMENT_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ProjectConfigSnapshot:
    """The exact committed text pair plus its combined revision and report."""

    project_id: str
    revision: str
    documents: tuple[str, ...]
    aflow_toml: str
    workflows_toml: str
    validation: ConfigValidationReport


def document_path(config_dir: Path, name: str) -> Path:
    """Map the only supported document names to their exact contained paths."""
    if name not in CONFIG_DOCUMENT_NAMES:
        raise ProjectConfigError("unsupported configuration document name")
    return config_dir / name


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_protected_document(
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


def _restore_document(target: Path, previous_bytes: bytes | None) -> None:
    try:
        if previous_bytes is None:
            # The document did not exist before the failed transaction.
            target.unlink(missing_ok=True)
            return
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".restore-tmp", dir=target.parent
        )
        temp = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(previous_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    except OSError as exc:
        raise ProjectConfigError(
            "the previous configuration could not be restored"
        ) from exc


def _append_audit_line(
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


class ProjectConfigService:
    """Read, validate, and atomically save the two project config documents."""

    def __init__(
        self,
        registry: ProjectRegistry,
        control_plane: ControlPlaneService,
        *,
        audit_path: Path,
    ) -> None:
        self._registry = registry
        self._control_plane = control_plane
        self._audit_path = audit_path.expanduser().absolute()

    def read(self, project_id: str) -> ProjectConfigSnapshot:
        """Return the exact committed texts, revision, and validation report."""
        with self._control_plane.project_lock(project_id):
            root = self._project_root(project_id)
            return self._snapshot(project_id, root)

    def validate_candidate(
        self,
        project_id: str,
        aflow_text: str,
        workflows_text: str,
    ) -> ConfigValidationReport:
        """Validate one candidate pair without reading or writing any file."""
        self._project_root(project_id)
        return validate_candidate_pair(aflow_text, workflows_text)

    def save(
        self,
        project_id: str,
        aflow_text: str,
        workflows_text: str,
        expected_revision: str,
        *,
        caller_scope: str = "rest",
    ) -> ProjectConfigSnapshot:
        """Commit both documents as one compare-and-swap, rollback-safe pair."""
        if caller_scope not in {"rest", "mcp"}:
            raise ValueError("unsupported configuration transport scope")
        if (
            not isinstance(expected_revision, str)
            or _DOCUMENT_HEX_RE.fullmatch(expected_revision) is None
        ):
            raise ProjectConfigError("expected_revision must be a SHA-256 hex digest")
        with self._control_plane.project_lock(project_id):
            revisions: dict[str, str | None] = {"old": None, "new": None}
            try:
                snapshot = self._save_locked(
                    project_id,
                    aflow_text,
                    workflows_text,
                    expected_revision,
                    revisions,
                )
            except Exception as exc:
                self._append_audit(
                    project_id=project_id,
                    outcome=self._failure_outcome(exc),
                    old_revision=revisions["old"],
                    new_revision=revisions["new"],
                    caller_scope=caller_scope,
                )
                raise
            self._append_audit(
                project_id=project_id,
                outcome="saved",
                old_revision=revisions["old"],
                new_revision=revisions["new"],
                caller_scope=caller_scope,
            )
            # Future-run capabilities reload from the committed files.  A
            # recomposition failure never rewrites the committed pair; project
            # readiness surfaces the unavailable control plane.
            try:
                self._control_plane.reload_project_config(project_id)
            except Exception:
                pass
            return snapshot

    def _save_locked(
        self,
        project_id: str,
        aflow_text: str,
        workflows_text: str,
        expected_revision: str,
        revisions: dict[str, str | None],
    ) -> ProjectConfigSnapshot:
        root = self._project_root(project_id)
        config_dir = self._validated_config_dir(root)
        aflow_doc, workflows_doc = self._current_documents(config_dir)
        current_revision = combined_revision(
            aflow_doc[0] if aflow_doc else b"",
            workflows_doc[0] if workflows_doc else b"",
        )
        revisions["old"] = current_revision
        if expected_revision != current_revision:
            raise ProjectConfigRevisionConflict(current_revision)
        report = validate_candidate_pair(aflow_text, workflows_text)
        if report.state == "invalid":
            first = report.issues[0]
            location = first.document or "configuration"
            raise ProjectConfigError(
                f"candidate configuration is invalid: {location}: {first.message}"
            )
        if report.placeholders:
            raise ProjectConfigError(
                "candidate configuration still contains placeholder selectors: "
                + ", ".join(report.placeholders[:MAX_VALIDATION_ISSUES])
            )
        aflow_bytes = aflow_text.encode("utf-8")
        workflows_bytes = workflows_text.encode("utf-8")
        new_revision = combined_revision(aflow_bytes, workflows_bytes)
        self._commit_pair(
            config_dir,
            previous=(
                aflow_doc[0] if aflow_doc else None,
                workflows_doc[0] if workflows_doc else None,
            ),
            payloads=(aflow_bytes, workflows_bytes),
        )
        committed_aflow, committed_workflows = self._current_documents(config_dir)
        committed_revision = combined_revision(
            committed_aflow[0] if committed_aflow else b"",
            committed_workflows[0] if committed_workflows else b"",
        )
        if committed_revision != new_revision:
            raise ProjectConfigError("committed configuration could not be verified")
        revisions["new"] = committed_revision
        return self._snapshot(project_id, root)

    def _project_root(self, project_id: str) -> Path:
        if self._registry.get(project_id) is None:
            raise ProjectNotAllowedError("project is not registered")
        try:
            _, root = self._registry.resolve(project_id)
        except ProjectRegistryError as exc:
            raise ProjectConfigError("project root is unavailable") from exc
        return root

    @staticmethod
    def _validated_config_dir(root: Path) -> Path:
        current = root
        for part in (".aflow", "config"):
            current = current / part
            if current.is_symlink():
                raise ProjectConfigError(
                    "configuration directory contains a symlink component"
                )
            if current.exists() and not current.is_dir():
                raise ProjectConfigError(
                    "configuration path component must be a directory"
                )
        if current.exists() and not current.is_dir():
            raise ProjectConfigError("configuration path must be a directory")
        return current

    @staticmethod
    def _read_document(config_dir: Path, name: str) -> tuple[bytes, str] | None:
        """Read one protected document; ``None`` when it does not exist yet."""
        return _read_protected_document(config_dir, name)

    def _current_documents(
        self, config_dir: Path
    ) -> tuple[tuple[bytes, str] | None, tuple[bytes, str] | None]:
        """Return both documents, or ``None`` for a document that is absent."""
        return (
            self._read_document(config_dir, "aflow.toml"),
            self._read_document(config_dir, "workflows.toml"),
        )

    def _snapshot(self, project_id: str, root: Path) -> ProjectConfigSnapshot:
        config_dir = self._validated_config_dir(root)
        aflow_doc, workflows_doc = self._current_documents(config_dir)
        aflow_bytes = aflow_doc[0] if aflow_doc else b""
        workflows_bytes = workflows_doc[0] if workflows_doc else b""
        aflow_text = aflow_doc[1] if aflow_doc else ""
        workflows_text = workflows_doc[1] if workflows_doc else ""
        report = validate_candidate_pair(aflow_text, workflows_text)
        if (aflow_doc is None or workflows_doc is None) and report.state == "ready":
            # Mirror the engine readiness classifier: a document that does not
            # exist yet keeps the project configuration_required even though
            # empty candidate text would validate.
            report = ConfigValidationReport(
                state="configuration_required",
                issues=report.issues,
                placeholders=report.placeholders,
                workflows=report.workflows,
                teams=report.teams,
                roles=report.roles,
            )
        return ProjectConfigSnapshot(
            project_id=project_id,
            revision=combined_revision(aflow_bytes, workflows_bytes),
            documents=CONFIG_DOCUMENT_NAMES,
            aflow_toml=aflow_text,
            workflows_toml=workflows_text,
            validation=report,
        )

    def _commit_pair(
        self,
        config_dir: Path,
        *,
        previous: tuple[bytes | None, bytes | None],
        payloads: tuple[bytes, bytes],
    ) -> None:
        created_dir = not config_dir.exists()
        if created_dir:
            config_dir.mkdir(parents=True)
        staged: list[Path] = []
        try:
            for name, payload in zip(CONFIG_DOCUMENT_NAMES, payloads):
                descriptor, temp_name = tempfile.mkstemp(
                    prefix=f".{name}.", suffix=".tmp", dir=config_dir
                )
                temp = Path(temp_name)
                staged.append(temp)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            first_temp, second_temp = staged
            first_target = document_path(config_dir, CONFIG_DOCUMENT_NAMES[0])
            second_target = document_path(config_dir, CONFIG_DOCUMENT_NAMES[1])
            os.replace(first_temp, first_target)
            try:
                os.replace(second_temp, second_target)
            except OSError as exc:
                _restore_document(first_target, previous[0])
                _fsync_directory(config_dir)
                raise ProjectConfigError(
                    "workflows.toml replacement failed; the previous aflow.toml was restored"
                ) from exc
            _fsync_directory(config_dir)
            if created_dir:
                _fsync_directory(config_dir.parent)
        finally:
            for temp in staged:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass

    def _restore_document_for_target(
        self, target: Path, previous_bytes: bytes | None
    ) -> None:
        _restore_document(target, previous_bytes)

    @staticmethod
    def _failure_outcome(exc: Exception) -> str:
        if isinstance(exc, ProjectConfigRevisionConflict):
            return "revision_conflict"
        if isinstance(exc, ControlPlaneUnavailableError):
            return "run_state_unavailable"
        if isinstance(exc, ProjectConfigError):
            return "rejected"
        return "error"

    def _append_audit(
        self,
        *,
        project_id: str,
        outcome: str,
        old_revision: str | None,
        new_revision: str | None,
        caller_scope: str,
    ) -> None:
        """Append one bounded, redacted audit record to server state."""
        _append_audit_line(
            self._audit_path,
            project_id=project_id,
            outcome=outcome,
            old_revision=old_revision,
            new_revision=new_revision,
            caller_scope=caller_scope,
        )
