"""Atomic, revision-checked global workflow configuration service.

The global service owns the one shared workflow pair in the global AFlow
configuration directory (``~/.config/aflow/aflow.toml`` plus its sibling
``workflows.toml``).  It reuses the project service's validation and revision
machinery and delegates every durable pair write to the core transaction
owner in :mod:`aflow.config_pair`, which records a bounded write-ahead
journal, replaces both documents, and recovers an interrupted generation
under the shared configuration lock.  Reads and saves run pending-transaction
recovery first, so the service never observes a mixed pair.  A save never
blocks because some project has a running or resumable run; existing runs
reload the committed source at their next boundary, and only an invalid
candidate or stale revision rejects the save.

A third-party edit that conflicts with a pending recovery rejects the read
or save with a bounded :class:`ProjectConfigError` and preserves both the
edited bytes and the transaction record.
"""

from __future__ import annotations

from pathlib import Path
import threading

from aflow.config_pair import (
    ConfigPairError,
    commit_configuration_pair,
    configuration_pair_lock,
    recover_pending_transaction,
)

from .project_config_service import (
    CONFIG_DOCUMENT_NAMES,
    ProjectConfigError,
    ProjectConfigRevisionConflict,
    ProjectConfigSnapshot,
    _DOCUMENT_HEX_RE,
    _append_audit_line,
    _read_protected_document,
    combined_revision,
    validate_candidate_pair,
)


class GlobalConfigService:
    """Read, validate, and atomically save the shared workflow pair."""

    def __init__(self, *, config_dir: Path, audit_path: Path) -> None:
        self._config_dir = config_dir.expanduser().absolute()
        self._audit_path = audit_path.expanduser().absolute()
        self._lock = threading.RLock()

    @property
    def config_dir(self) -> Path:
        return self._config_dir

    @property
    def pair_path(self) -> Path:
        return self._config_dir / "aflow.toml"

    def read(self) -> ProjectConfigSnapshot:
        """Return the exact committed texts, revision, and validation report."""
        with self._lock, configuration_pair_lock(self._config_dir):
            self._recover()
            return self._snapshot()

    def validate_candidate(
        self,
        aflow_text: str,
        workflows_text: str,
    ) -> object:
        """Validate one candidate pair without reading or writing any file."""
        return validate_candidate_pair(aflow_text, workflows_text)

    def save(
        self,
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
            raise ProjectConfigError(
                "expected_revision must be a SHA-256 hex digest"
            )
        with self._lock, configuration_pair_lock(self._config_dir):
            revisions: dict[str, str | None] = {"old": None, "new": None}
            try:
                snapshot = self._save_locked(
                    aflow_text, workflows_text, expected_revision, revisions
                )
            except Exception as exc:
                _append_audit_line(
                    self._audit_path,
                    project_id="global",
                    outcome=_failure_outcome(exc),
                    old_revision=revisions["old"],
                    new_revision=revisions["new"],
                    caller_scope=caller_scope,
                )
                raise
            _append_audit_line(
                self._audit_path,
                project_id="global",
                outcome="saved",
                old_revision=revisions["old"],
                new_revision=revisions["new"],
                caller_scope=caller_scope,
            )
            return snapshot

    def patch(self, payload, *, caller_scope: str = "rest") -> ProjectConfigSnapshot:
        if caller_scope not in {"rest", "mcp"}:
            raise ValueError("unsupported configuration transport scope")
        from .guided_config import apply_action_batch

        with self._lock, configuration_pair_lock(self._config_dir):
            self._recover()
            current = self._snapshot()
            if payload.expected_revision != current.revision:
                raise ProjectConfigRevisionConflict(current.revision)
            if payload.actions is not None:
                texts = apply_action_batch(current.aflow_toml, current.workflows_toml, payload.actions)
            else:
                texts = (payload.documents.get("aflow.toml", current.aflow_toml),
                         payload.documents.get("workflows.toml", current.workflows_toml))
            if texts == (current.aflow_toml, current.workflows_toml):
                return current
            revisions = {"old": current.revision, "new": None}
            try:
                saved = self._save_locked(*texts, current.revision, revisions)
            except Exception as exc:
                _append_audit_line(self._audit_path, project_id="global", outcome=_failure_outcome(exc),
                                   old_revision=revisions["old"], new_revision=revisions["new"], caller_scope=caller_scope)
                raise
            _append_audit_line(self._audit_path, project_id="global", outcome="saved",
                               old_revision=revisions["old"], new_revision=revisions["new"], caller_scope=caller_scope)
            return saved

    def _save_locked(
        self,
        aflow_text: str,
        workflows_text: str,
        expected_revision: str,
        revisions: dict[str, str | None],
    ) -> ProjectConfigSnapshot:
        self._recover()
        aflow_doc = _read_protected_document(self._config_dir, "aflow.toml")
        workflows_doc = _read_protected_document(self._config_dir, "workflows.toml")
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
            from .project_config_service import MAX_VALIDATION_ISSUES

            raise ProjectConfigError(
                "candidate configuration still contains placeholder selectors: "
                + ", ".join(report.placeholders[:MAX_VALIDATION_ISSUES])
            )
        aflow_bytes = aflow_text.encode("utf-8")
        workflows_bytes = workflows_text.encode("utf-8")
        new_revision = combined_revision(aflow_bytes, workflows_bytes)
        _commit_pair_locked(
            self._config_dir,
            payloads=(aflow_bytes, workflows_bytes),
        )
        committed_aflow = _read_protected_document(self._config_dir, "aflow.toml")
        committed_workflows = _read_protected_document(
            self._config_dir, "workflows.toml"
        )
        committed_revision = combined_revision(
            committed_aflow[0] if committed_aflow else b"",
            committed_workflows[0] if committed_workflows else b"",
        )
        if committed_revision != new_revision:
            raise ProjectConfigError("committed configuration could not be verified")
        revisions["new"] = committed_revision
        return self._snapshot()

    def _snapshot(self) -> ProjectConfigSnapshot:
        aflow_doc = _read_protected_document(self._config_dir, "aflow.toml")
        workflows_doc = _read_protected_document(self._config_dir, "workflows.toml")
        aflow_bytes = aflow_doc[0] if aflow_doc else b""
        workflows_bytes = workflows_doc[0] if workflows_doc else b""
        aflow_text = aflow_doc[1] if aflow_doc else ""
        workflows_text = workflows_doc[1] if workflows_doc else ""
        report = validate_candidate_pair(aflow_text, workflows_text)
        if (aflow_doc is None or workflows_doc is None) and report.state == "ready":
            from .project_config_service import ConfigValidationReport

            report = ConfigValidationReport(
                state="configuration_required",
                issues=report.issues,
                placeholders=report.placeholders,
                workflows=report.workflows,
                teams=report.teams,
                roles=report.roles,
            )
        return ProjectConfigSnapshot(
            project_id="global",
            revision=combined_revision(aflow_bytes, workflows_bytes),
            documents=CONFIG_DOCUMENT_NAMES,
            aflow_toml=aflow_text,
            workflows_toml=workflows_text,
            validation=report,
        )

    def _recover(self) -> None:
        """Complete a pending pair transaction; the caller holds the pair lock.

        Recovery failures reject the read or save with the service's bounded
        :class:`ProjectConfigError` contract while preserving the edited
        bytes and the transaction record for a later recoverable read.
        """
        try:
            recover_pending_transaction(self._config_dir)
        except ConfigPairError as exc:
            raise ProjectConfigError(
                f"configuration pair recovery failed: {exc}"
            ) from exc


def _commit_pair_locked(
    config_dir: Path,
    *,
    payloads: tuple[bytes, bytes],
) -> None:
    """Commit the pair through the transaction owner; the caller holds the lock.

    The core owner re-enters the shared pair lock (one effective flock per
    process), records the old and new generations, replaces both documents,
    and restores the old generation on any pre-commit failure.  Core
    failures surface as the service's bounded :class:`ProjectConfigError`
    contract so REST/MCP keep their public errors and audit outcomes.
    """
    try:
        commit_configuration_pair(
            config_dir,
            payloads=dict(zip(CONFIG_DOCUMENT_NAMES, payloads)),
        )
    except ConfigPairError as exc:
        raise ProjectConfigError(str(exc)) from exc


def _failure_outcome(exc: Exception) -> str:
    if isinstance(exc, ProjectConfigRevisionConflict):
        return "revision_conflict"
    if isinstance(exc, ProjectConfigError):
        return "rejected"
    return "error"
