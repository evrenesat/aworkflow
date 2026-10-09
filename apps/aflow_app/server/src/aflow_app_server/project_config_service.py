"""Compatibility facade for the legacy project configuration import path.

The current owners are:

- :mod:`aflow.config_pair` — the durable pair lock, write-ahead
  transaction, and crash recovery;
- :mod:`aflow_app_server.config_validation` — the configuration exception
  types, report and snapshot shapes, revision and bounds helpers, and the
  in-memory candidate validator;
- :mod:`aflow_app_server.config_documents` — protected document reads and
  bounded audit records.

:class:`ProjectConfigService` keeps the project-scoped read/save facade
under its existing tests and visibly delegates parsing, validation, and
every durable pair write to those owners.  The legacy module-level names
remain available here only as forwarding aliases for compatibility
callers; active server code imports the current owners directly.
"""

from __future__ import annotations

from pathlib import Path

from aflow.config_pair import (
    ConfigPairError,
    commit_configuration_pair,
    configuration_pair_lock,
    recover_pending_transaction,
)

from .config_documents import (
    append_audit_line,
    document_path,  # noqa: F401  compatibility re-export
    read_protected_document,
)
from .config_validation import (
    CONFIG_DOCUMENT_NAMES,
    MAX_CONFIG_DOCUMENT_BYTES,  # noqa: F401  compatibility re-export
    MAX_ISSUE_MESSAGE_CHARS,  # noqa: F401  compatibility re-export
    MAX_VALIDATION_ISSUES,
    DOCUMENT_HEX_RE,
    ConfigValidationIssue,  # noqa: F401  compatibility re-export
    ConfigValidationReport,
    ProjectConfigError,
    ProjectConfigRevisionConflict,
    ProjectConfigSnapshot,
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

# Compatibility aliases for the legacy module-level helper spellings.  Each
# name is the exact current-owner object, not a copy or wrapper; active
# server code imports the current owners directly.
_DOCUMENT_HEX_RE = DOCUMENT_HEX_RE
_append_audit_line = append_audit_line
_read_protected_document = read_protected_document


class ProjectConfigService:
    """Read, validate, and atomically save the two project config documents.

    The service owns exactly two documents per registered project,
    ``.aflow/config/aflow.toml`` and ``.aflow/config/workflows.toml``.  It
    never reads or writes another project file, never starts or stops
    workflow units, and does not gate a valid save on the state or
    historical configuration of a workflow run.
    """

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
            config_dir = self._validated_config_dir(root)
            with configuration_pair_lock(config_dir):
                self._recover(config_dir)
                return self._snapshot(project_id, config_dir)

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
            or DOCUMENT_HEX_RE.fullmatch(expected_revision) is None
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
        with configuration_pair_lock(config_dir):
            self._recover(config_dir)
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
            self._commit_pair(config_dir, payloads=(aflow_bytes, workflows_bytes))
            committed_aflow, committed_workflows = self._current_documents(config_dir)
            committed_revision = combined_revision(
                committed_aflow[0] if committed_aflow else b"",
                committed_workflows[0] if committed_workflows else b"",
            )
            if committed_revision != new_revision:
                raise ProjectConfigError("committed configuration could not be verified")
            revisions["new"] = committed_revision
            return self._snapshot(project_id, config_dir)

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

    def _current_documents(
        self, config_dir: Path
    ) -> tuple[tuple[bytes, str] | None, tuple[bytes, str] | None]:
        """Return both documents, or ``None`` for a document that is absent."""
        return (
            read_protected_document(config_dir, "aflow.toml"),
            read_protected_document(config_dir, "workflows.toml"),
        )

    def _snapshot(
        self, project_id: str, config_dir: Path
    ) -> ProjectConfigSnapshot:
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

    @staticmethod
    def _commit_pair(config_dir: Path, *, payloads: tuple[bytes, bytes]) -> None:
        """Commit the pair through the core transaction owner; the pair lock is held.

        The core owner re-enters the shared pair lock (one effective flock per
        process), records the old and new generations, replaces both
        documents, and restores the old generation on any pre-commit
        failure.  Core failures surface as the service's bounded
        :class:`ProjectConfigError` contract so REST/MCP keep their public
        errors and audit outcomes.
        """
        try:
            commit_configuration_pair(
                config_dir,
                payloads=dict(zip(CONFIG_DOCUMENT_NAMES, payloads)),
            )
        except ConfigPairError as exc:
            raise ProjectConfigError(str(exc)) from exc

    def _recover(self, config_dir: Path) -> None:
        """Complete a pending pair transaction; the caller holds the pair lock.

        Recovery failures reject the read or save with the service's bounded
        :class:`ProjectConfigError` contract while preserving the edited
        bytes and the transaction record for a later recoverable read.
        """
        try:
            recover_pending_transaction(config_dir)
        except ConfigPairError as exc:
            raise ProjectConfigError(
                f"configuration pair recovery failed: {exc}"
            ) from exc

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
        append_audit_line(
            self._audit_path,
            project_id=project_id,
            outcome=outcome,
            old_revision=old_revision,
            new_revision=new_revision,
            caller_scope=caller_scope,
        )
