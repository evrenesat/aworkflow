"""Shared in-memory validation for submitted configuration pair text.

This module owns the server's configuration exception types, report
shapes, revision, and bounds helpers.  Candidate text is validated through
the core pure parser :func:`aflow.config.parse_workflow_pair` — one parse,
one sibling merge, one semantic validation pass, and no candidate files —
so submitted text and filesystem inputs produce equivalent reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
import tomllib

from aflow.config import (
    ConfigError,
    find_placeholders,
    parse_workflow_pair,
)

CONFIG_DOCUMENT_NAMES = ("aflow.toml", "workflows.toml")
MAX_CONFIG_DOCUMENT_BYTES = 256 * 1024
MAX_VALIDATION_ISSUES = 20
MAX_ISSUE_MESSAGE_CHARS = 300
_TOML_LINE_RE = re.compile(r"line (\d+)")


class ProjectConfigError(RuntimeError):
    """A bounded, actionable project configuration failure."""


class ProjectConfigRevisionConflict(ProjectConfigError):
    """The submitted expected revision no longer matches the committed pair."""

    def __init__(self, current_revision: str) -> None:
        super().__init__("configuration revision does not match the committed revision")
        self.current_revision = current_revision


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


def _candidate_parse_issue(exc: ConfigError) -> ConfigValidationIssue:
    """Sanitize one pure-parser failure into a bounded diagnostic.

    The core parser reports only the parsed document names, so no
    filesystem paths can reach the public report.
    """
    raw = str(exc)
    document: str | None = None
    for name in CONFIG_DOCUMENT_NAMES:
        if name in raw:
            document = name
            break
    return ConfigValidationIssue(
        document=document,
        line=_line_number(raw),
        message=_bounded_message(raw),
    )


def validate_candidate_pair(
    aflow_text: str, workflows_text: str
) -> ConfigValidationReport:
    """Validate both candidate documents together in memory.

    One call to the core pure parser performs TOML parsing, the sibling
    merge, and the single semantic validation pass; no candidate files are
    created.
    """
    check_document_text("aflow.toml", aflow_text)
    check_document_text("workflows.toml", workflows_text)
    issues: list[ConfigValidationIssue] = []
    for name, text in (("aflow.toml", aflow_text), ("workflows.toml", workflows_text)):
        issue = _syntax_issue(name, text)
        if issue is not None:
            issues.append(issue)
    config = None
    if not issues:
        try:
            config = parse_workflow_pair(
                aflow_text, workflows_text, source_dir=Path.cwd()
            )
        except ConfigError as exc:
            issues.append(_candidate_parse_issue(exc))
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
