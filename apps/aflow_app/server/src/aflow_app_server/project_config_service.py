"""Atomic, revision-checked project configuration text service.

The service owns exactly two documents per registered project,
``.aflow/config/aflow.toml`` and ``.aflow/config/workflows.toml``.  It never
reads or writes another project file, never starts or stops workflow units,
and delegates every run-state question to the daemon-backed control plane.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat as stat_module
import tempfile
import tomllib

from aflow.config import (
    ConfigError,
    find_placeholders,
    load_workflow_config,
    validate_workflow_config,
)

from .control_plane_service import (
    ControlPlaneService,
    ControlPlaneUnavailableError,
    ProjectNotAllowedError,
)
from .project_registry import ProjectRegistry, ProjectRegistryError


CONFIG_DOCUMENT_NAMES = ("aflow.toml", "workflows.toml")
MAX_CONFIG_DOCUMENT_BYTES = 256 * 1024
MAX_VALIDATION_ISSUES = 20
MAX_ISSUE_MESSAGE_CHARS = 300
MAX_BLOCKING_RUNS = 20
_AUDIT_SCHEMA_VERSION = 1
_TOML_LINE_RE = re.compile(r"line (\d+)")

# Nonterminal, startup-gated, stopping, and launch-incomplete runs always
# block a save.  ``failed``/``interrupted`` runs block only while the current
# on-disk fingerprint still matches their frozen manifest fingerprint, because
# only then can explicit resume still reach them.
_CONFIG_BLOCKING_RUN_STATUSES = frozenset(
    {
        "running",
        "awaiting_startup_answer",
        "stopping",
        "needs_attention",
        "waiting_for_valid_override",
        "manifest_only",
        "launch_requested",
        "launch_started",
        "unit_started",
        "prepared",
    }
)
_CONFIG_RESUME_ELIGIBLE_RUN_STATUSES = frozenset({"failed", "interrupted"})
_CONFIG_TERMINAL_RUN_STATUSES = frozenset({"completed", "done", "owner_stopped"})

_DOCUMENT_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class ProjectConfigError(RuntimeError):
    """A bounded, actionable project configuration failure."""


class ProjectConfigRevisionConflict(ProjectConfigError):
    """The submitted expected revision no longer matches the committed pair."""

    def __init__(self, current_revision: str) -> None:
        super().__init__("configuration revision does not match the committed revision")
        self.current_revision = current_revision


class ProjectConfigRunBlocked(ProjectConfigError):
    """A nonterminal or still-resumable owned run depends on the current pair."""

    def __init__(self, blocking_runs: tuple[tuple[str, str], ...]) -> None:
        super().__init__(
            "configuration save is blocked by nonterminal or resumable runs"
        )
        self.blocking_runs = blocking_runs


@dataclass(frozen=True)
class ConfigValidationIssue:
    """One bounded diagnostic; messages never contain filesystem paths."""

    document: str | None
    line: int | None
    message: str


@dataclass(frozen=True)
class ConfigValidationReport:
    """Combined-pair validation summary with parsed, bounded names only."""

    state: str  # "ready" | "configuration_required" | "invalid"
    issues: tuple[ConfigValidationIssue, ...]
    placeholders: tuple[str, ...]
    workflows: tuple[str, ...]
    teams: tuple[str, ...]
    roles: tuple[str, ...]


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


def combined_revision(aflow_bytes: bytes, workflows_bytes: bytes) -> str:
    """Compute the compare-and-swap revision over the exact pair bytes."""
    digest = hashlib.sha256()
    digest.update(b"aflow.toml\x00")
    digest.update(aflow_bytes)
    digest.update(b"\x00workflows.toml\x00")
    digest.update(workflows_bytes)
    return digest.hexdigest()


def _bounded_message(message: str) -> str:
    collapsed = " ".join(message.split())
    if len(collapsed) > MAX_ISSUE_MESSAGE_CHARS:
        return collapsed[:MAX_ISSUE_MESSAGE_CHARS] + "[truncated]"
    return collapsed


def _line_number(message: str) -> int | None:
    match = _TOML_LINE_RE.search(message)
    return int(match.group(1)) if match else None


def _syntax_issue(name: str, text: str) -> ConfigValidationIssue | None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        message = str(exc)
        return ConfigValidationIssue(
            document=name,
            line=_line_number(message),
            message=_bounded_message(message),
        )
    return None


def _candidate_load_issue(temporary: Path, exc: ConfigError) -> ConfigValidationIssue:
    """Sanitize one production loader failure into a bounded diagnostic."""
    raw = str(exc)
    document: str | None = None
    for name in CONFIG_DOCUMENT_NAMES:
        if str(temporary / name) in raw:
            document = name
            break
    message = raw
    for name in CONFIG_DOCUMENT_NAMES:
        message = message.replace(str(temporary / name), name)
    message = message.replace(str(temporary), "<candidate>")
    return ConfigValidationIssue(
        document=document,
        line=_line_number(message),
        message=_bounded_message(message),
    )


def check_document_text(name: str, text: str) -> bytes:
    """Apply candidate size and content bounds before any parse or write."""
    if not isinstance(text, str):
        raise ProjectConfigError(f"{name} must be UTF-8 text")
    try:
        payload = text.encode("utf-8")
    except UnicodeEncodeError as exc:  # pragma: no cover - str is always encodable
        raise ProjectConfigError(f"{name} is not valid UTF-8 text") from exc
    if len(payload) > MAX_CONFIG_DOCUMENT_BYTES:
        raise ProjectConfigError(f"{name} exceeds the maximum supported size")
    if "\x00" in text:
        raise ProjectConfigError(f"{name} must not contain NUL characters")
    return payload


def validate_candidate_pair(
    aflow_text: str, workflows_text: str
) -> ConfigValidationReport:
    """Validate both candidate documents together through the production loader."""
    check_document_text("aflow.toml", aflow_text)
    check_document_text("workflows.toml", workflows_text)
    issues: list[ConfigValidationIssue] = []
    for name, text in (("aflow.toml", aflow_text), ("workflows.toml", workflows_text)):
        issue = _syntax_issue(name, text)
        if issue is not None:
            issues.append(issue)
    config = None
    if not issues:
        with tempfile.TemporaryDirectory(prefix="aflow-project-config-") as temporary:
            temp_dir = Path(temporary)
            (temp_dir / "aflow.toml").write_text(aflow_text, encoding="utf-8")
            (temp_dir / "workflows.toml").write_text(workflows_text, encoding="utf-8")
            try:
                config = load_workflow_config(temp_dir / "aflow.toml")
            except ConfigError as exc:
                issues.append(_candidate_load_issue(temp_dir, exc))
            if config is not None:
                # The production loader already enforces semantic validation;
                # the explicit pass keeps diagnostics structured if that
                # enforcement ever moves behind a flag.
                for message in validate_workflow_config(config):
                    issues.append(
                        ConfigValidationIssue(
                            document=None,
                            line=None,
                            message=_bounded_message(message),
                        )
                    )
    issues = issues[:MAX_VALIDATION_ISSUES]
    if issues:
        return ConfigValidationReport(
            state="invalid",
            issues=tuple(issues),
            placeholders=(),
            workflows=(),
            teams=(),
            roles=(),
        )
    assert config is not None
    placeholders = tuple(find_placeholders(config))
    return ConfigValidationReport(
        state="configuration_required" if placeholders else "ready",
        issues=(),
        placeholders=placeholders,
        workflows=tuple(sorted(config.workflows)),
        teams=tuple(sorted(config.teams)),
        roles=tuple(sorted(config.roles)),
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
        self._assert_no_blocking_runs(project_id, root)
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

    def _assert_no_blocking_runs(self, project_id: str, root: Path) -> None:
        snapshots = self._control_plane.owned_run_snapshots(project_id)
        if not snapshots:
            return
        config = None
        if any(
            status in _CONFIG_RESUME_ELIGIBLE_RUN_STATUSES
            for _, status, _, _ in snapshots
        ):
            try:
                config = load_workflow_config(root / ".aflow" / "config" / "aflow.toml")
            except ConfigError:
                config = None
        blockers: list[tuple[str, str]] = []
        for run_id, status, workflow_name, manifest_fingerprint in snapshots:
            if status in _CONFIG_BLOCKING_RUN_STATUSES:
                blockers.append((run_id, status))
                continue
            if status in _CONFIG_RESUME_ELIGIBLE_RUN_STATUSES:
                if (
                    config is not None
                    and workflow_name is not None
                    and manifest_fingerprint is not None
                    and self._current_fingerprint(config, root, workflow_name)
                    == manifest_fingerprint
                ):
                    blockers.append((run_id, status))
                continue
            if status not in _CONFIG_TERMINAL_RUN_STATUSES:
                # An unrecognized status fails closed: saving could strand it.
                blockers.append((run_id, status))
        if blockers:
            raise ProjectConfigRunBlocked(tuple(sorted(blockers)[:MAX_BLOCKING_RUNS]))

    @staticmethod
    def _current_fingerprint(
        config: object, root: Path, workflow_name: str
    ) -> str | None:
        from aflow.workflow import _freeze_run_identity

        try:
            identity = _freeze_run_identity(
                workflow_name,
                config,  # type: ignore[arg-type]
                config_dir=root / ".aflow" / "config" / "aflow.toml",
            )
        except (KeyError, ValueError, TypeError):
            return None
        return identity.config_fingerprint

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
                self._restore_document(first_target, previous[0])
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

    def _restore_document(self, target: Path, previous_bytes: bytes | None) -> None:
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

    @staticmethod
    def _failure_outcome(exc: Exception) -> str:
        if isinstance(exc, ProjectConfigRevisionConflict):
            return "revision_conflict"
        if isinstance(exc, ProjectConfigRunBlocked):
            return "run_blocked"
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
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            flags = (
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(self._audit_path, flags, 0o600)
            try:
                os.write(descriptor, line)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            # Audit metadata is advisory; a save must never fail because of it.
            pass
