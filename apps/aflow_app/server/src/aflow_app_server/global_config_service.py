"""Atomic, revision-checked global workflow configuration service.

The global service owns the one shared workflow pair in the global AFlow
configuration directory (``~/.config/aflow/aflow.toml`` plus its sibling
``workflows.toml``).  It reuses the project service's validation, revision,
and rollback-safe commit machinery, serializes pair writes through the shared
configuration lock, and never blocks a save because some project has a running
or resumable run. Existing runs reload the committed source at their next
boundary; only an invalid candidate or stale revision rejects the save.
"""

from __future__ import annotations

from pathlib import Path
import os
import tempfile
import threading

from aflow.run_config_snapshot import configuration_pair_lock

from .project_config_service import (
    CONFIG_DOCUMENT_NAMES,
    ProjectConfigError,
    ProjectConfigRevisionConflict,
    ProjectConfigSnapshot,
    _DOCUMENT_HEX_RE,
    _append_audit_line,
    _fsync_directory,
    _read_protected_document,
    _restore_document,
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

    def patch(self, payload) -> ProjectConfigSnapshot:
        from .guided_config import apply_action_batch

        with self._lock, configuration_pair_lock(self._config_dir):
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
                                   old_revision=revisions["old"], new_revision=revisions["new"], caller_scope="rest")
                raise
            _append_audit_line(self._audit_path, project_id="global", outcome="saved",
                               old_revision=revisions["old"], new_revision=revisions["new"], caller_scope="rest")
            return saved

    def _save_locked(
        self,
        aflow_text: str,
        workflows_text: str,
        expected_revision: str,
        revisions: dict[str, str | None],
    ) -> ProjectConfigSnapshot:
        from .project_config_service import _read_protected_document

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
            previous=(
                aflow_doc[0] if aflow_doc else None,
                workflows_doc[0] if workflows_doc else None,
            ),
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


def _commit_pair_locked(
    config_dir: Path,
    *,
    previous: tuple[bytes | None, bytes | None],
    payloads: tuple[bytes, bytes],
) -> None:
    """Replace the pair on disk; the caller holds the shared pair lock."""
    from .project_config_service import (
        document_path,
    )

    config_dir.mkdir(parents=True, exist_ok=True)
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
        if previous[0] != payloads[0]:
            os.replace(first_temp, first_target)
        try:
            if previous[1] != payloads[1]:
                os.replace(second_temp, second_target)
        except OSError as exc:
            _restore_document(first_target, previous[0])
            _fsync_directory(config_dir)
            raise ProjectConfigError(
                "workflows.toml replacement failed; the previous aflow.toml was restored"
            ) from exc
        _fsync_directory(config_dir)
    finally:
        for temp in staged:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def _failure_outcome(exc: Exception) -> str:
    if isinstance(exc, ProjectConfigRevisionConflict):
        return "revision_conflict"
    if isinstance(exc, ProjectConfigError):
        return "rejected"
    return "error"
