"""Strict protocol and routing contracts for interstep manager supervision.

This module deliberately contains no workflow-loop integration.  It provides
validated inputs, decisions, routing helpers, and durable-report rendering for
the controller integration that follows in a later checkpoint.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import deepcopy
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Literal, Mapping

from .config import ConfigError, WorkflowUserConfig, resolve_team_config
from .manager_context import (
    MANAGER_CONTEXT_SCHEMA_VERSION_V3,
    MANAGER_INLINE_CONTEXT_MAX_BYTES,
    MANAGER_INLINE_CONTEXT_TARGET_BYTES,
)
from .skill_catalog import is_bundled_skill_name
from .skill_store import (
    SkillStore,
    SkillStoreError,
    parse_skill_document,
    validate_skill_document,
)


ManagerLevel = Literal["lite", "full"]
ManagerNoteAuthorityCategory = Literal[
    "plan_selection",
    "file_scope",
    "mandatory_implementation",
]
ManagerAction = Literal[
    "continue",
    "retry_current_step",
    "upgrade_next_implementation",
    "switch_to_backup_and_retry",
    "escalate_to_full",
    "repartition_current_checkpoint",
    "stop",
]
_ACTIONS = frozenset({
    "continue",
    "retry_current_step",
    "upgrade_next_implementation",
    "switch_to_backup_and_retry",
    "escalate_to_full",
    "repartition_current_checkpoint",
    "stop",
})
_DECISION_KEYS = frozenset({"schema_version", "action", "reason", "next_step_notes", "stop_report"})
_STOP_REPORT_KEYS = frozenset({"summary", "root_cause", "evidence", "attempts", "workspace_state", "next_actions"})
MAX_MANAGER_NOTES = 8
MAX_MANAGER_NOTE_LENGTH = 1_000
# These rules intentionally classify authority from normalized note text rather
# than asking another model to infer intent.  A path is ordinary evidence until
# a rule below pairs it with scope or plan-control language.
_SCOPE_AUTHORITY_RULES = (
    re.compile(
        r"\b(?:restrict|limit|confine|scope)\s+(?:edits?|changes?|work)\b|"
        r"\b(?:changes?|work)\s+(?:are|is)\s+confined\b|"
        r"\bkeep\s+(?:changes?|work)\s+scoped\b",
    ),
    re.compile(
        r"\b(?:sole|only)\s+(?:permitted|allowed)\s+(?:file|path)s?\b|"
        r"\bonly\s+(?:the\s+)?(?:files?|paths?|dirty\s+(?:files?|set))\b|"
        r"\bonly\s+(?:change|edit|touch|modify)\b",
    ),
    re.compile(
        r"\b(?:allow(?:ed)?\s+(?:files?|paths?)|allowlist|permit(?:ted)?\s+"
        r"(?:files?|paths?)|may\s+modify)\b",
    ),
    re.compile(
        r"\b(?:never\s+(?:modify|change|edit|touch)|must\s+not\s+touch|"
        r"do\s+not\s+(?:modify|change|edit|touch)|don't\s+(?:modify|change|edit|touch)|"
        r"outside\s+(?:the\s+)?(?:dirty\s+)?(?:files?|paths?|set))\b",
    ),
    re.compile(r"\b(?:current\s+)?dirty\s+(?:files?|set)\b"),
)
_PROHIBITION_AUTHORITY_RE = re.compile(
    r"\b(?:never\s+(?:modify|change|edit|touch)|must\s+not\s+touch|"
    r"do\s+not\s+(?:modify|change|edit|touch)|don't\s+(?:modify|change|edit|touch)|"
    r"outside\s+(?:the\s+)?(?:dirty\s+)?(?:files?|paths?|set))\b"
)
_PLAN_REFERENCE_RE = re.compile(r"\b(?:active|repair|current)?\s*plan\b")
_PLAN_SELECTION_RE = re.compile(
    r"\b(?:use|follow|switch(?:\s+to)?|replace|adopt|work\s+from)\b"
)
_DIRECT_PLAN_SELECTION_RE = re.compile(
    r"\b(?:use|follow|switch(?:\s+to)?|replace|adopt|work\s+from)\s+"
    r"(?:the\s+)?`?plans/[a-z0-9][a-z0-9._/-]*`?"
)
_IMPLEMENTATION_REQUIREMENT_RE = re.compile(
    r"\bensure\s+(?:the\s+)?(?:worker|implementation)\b|"
    r"\b(?:worker|implementation)\s+(?:must|should|needs?\s+to|is\s+required\s+to)\b|"
    r"\brequire\s+(?:the\s+)?(?:worker|implementation)\s+to\b"
)
_PATH_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_./-])([A-Za-z0-9][A-Za-z0-9._/-]*)(?![A-Za-z0-9_./-])")
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_SINGLE_JSON_FENCE = re.compile(
    r"\A\s*```json[ \t]*\r?\n(?P<body>.*?)\r?\n```[ \t]*\s*\Z",
    re.DOTALL,
)


class ManagerDecisionError(ValueError):
    """Raised when manager output is not a legal closed-protocol decision."""


class ManagerNoteAuthorityError(ManagerDecisionError):
    """A machine-classified manager note violation with unchanged legacy text."""

    def __init__(
        self,
        message: str,
        *,
        category: ManagerNoteAuthorityCategory,
    ) -> None:
        super().__init__(message)
        self.category = category

    @property
    def correctable(self) -> bool:
        """Only plan-selection wording is eligible for one correction attempt."""
        return self.category == "plan_selection"


@dataclass(frozen=True)
class ManagerStopReport:
    summary: str
    root_cause: str
    evidence: tuple[str, ...]
    attempts: str
    workspace_state: str
    next_actions: tuple[str, ...]


@dataclass(frozen=True)
class ManagerDecisionV1:
    schema_version: Literal[1]
    action: ManagerAction
    reason: str
    next_step_notes: tuple[str, ...] = ()
    stop_report: ManagerStopReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ManagerRoleResolution:
    level: ManagerLevel
    role: str
    team: str | None
    selector: str


@dataclass(frozen=True)
class EligibleImplementationUpgrade:
    available: bool
    source_team: str | None
    target_team: str | None
    role: str
    source_selector: str | None
    target_selector: str | None
    reason: str | None = None


def _nonempty_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManagerDecisionError(f"{field} must be a non-empty string")
    return value.strip()


def _text_list(value: object, *, field: str, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ManagerDecisionError(f"{field} must be an array of non-empty strings")
    items = tuple(_nonempty_text(item, field=f"{field}[{index}]") for index, item in enumerate(value))
    if required and not items:
        raise ManagerDecisionError(f"{field} must not be empty")
    return items


def _parse_stop_report(value: object) -> ManagerStopReport:
    if not isinstance(value, Mapping):
        raise ManagerDecisionError("stop_report must be an object")
    unknown = sorted(set(value) - _STOP_REPORT_KEYS)
    missing = sorted(_STOP_REPORT_KEYS - set(value))
    if unknown or missing:
        details = []
        if unknown:
            details.append(f"unknown fields: {', '.join(unknown)}")
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        raise ManagerDecisionError("invalid stop_report (" + "; ".join(details) + ")")
    return ManagerStopReport(
        summary=_nonempty_text(value["summary"], field="stop_report.summary"),
        root_cause=_nonempty_text(value["root_cause"], field="stop_report.root_cause"),
        evidence=_text_list(value["evidence"], field="stop_report.evidence", required=True),
        attempts=_nonempty_text(value["attempts"], field="stop_report.attempts"),
        workspace_state=_nonempty_text(value["workspace_state"], field="stop_report.workspace_state"),
        next_actions=_text_list(value["next_actions"], field="stop_report.next_actions", required=True),
    )


def parse_manager_decision(text: str) -> ManagerDecisionV1:
    """Parse one JSON object, tolerating only one exact ``json`` fence."""
    fenced = _SINGLE_JSON_FENCE.fullmatch(text)
    payload = fenced.group("body") if fenced is not None else text
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ManagerDecisionError(f"manager response is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, Mapping):
        raise ManagerDecisionError("manager response must be one JSON object")
    unknown = sorted(set(value) - _DECISION_KEYS)
    missing = sorted(_DECISION_KEYS - set(value))
    if unknown or missing:
        details = []
        if unknown:
            details.append(f"unknown fields: {', '.join(unknown)}")
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        raise ManagerDecisionError("invalid manager decision (" + "; ".join(details) + ")")
    if value["schema_version"] != 1:
        raise ManagerDecisionError("schema_version must be 1")
    action = value["action"]
    if action not in _ACTIONS:
        raise ManagerDecisionError(f"action must be one of: {', '.join(sorted(_ACTIONS))}")
    notes = _text_list(value["next_step_notes"], field="next_step_notes")
    if any(len(note) > MAX_MANAGER_NOTE_LENGTH for note in notes):
        raise ManagerDecisionError("next_step_notes exceeds protocol bounds")
    # Advisory notes must never spend a Full-manager call by themselves. Keep
    # the bounded prefix; action-specific validation below still rejects notes
    # for stop, Lite escalation, and accepted END.
    notes = notes[:MAX_MANAGER_NOTES]
    stop_report = None if value["stop_report"] is None else _parse_stop_report(value["stop_report"])
    decision = ManagerDecisionV1(
        schema_version=1,
        action=action,
        reason=_nonempty_text(value["reason"], field="reason"),
        next_step_notes=notes,
        stop_report=stop_report,
    )
    return validate_manager_decision(decision)


def validate_manager_decision(
    decision: ManagerDecisionV1,
    *,
    level: ManagerLevel | None = None,
    eligible_actions: set[str] | frozenset[str] | None = None,
    proposed_transition: str | None = None,
) -> ManagerDecisionV1:
    """Reject syntactically valid but controller-illegal manager decisions."""
    if decision.action == "stop":
        if decision.stop_report is None:
            raise ManagerDecisionError("stop requires stop_report")
        if decision.next_step_notes:
            raise ManagerDecisionError("stop must not include next_step_notes")
    elif decision.stop_report is not None:
        raise ManagerDecisionError("stop_report is only allowed for stop")
    if decision.action == "escalate_to_full":
        if level is not None and level != "lite":
            raise ManagerDecisionError("escalate_to_full is only legal for Lite")
        if decision.next_step_notes:
            raise ManagerDecisionError("escalate_to_full must not include next_step_notes")
    if decision.action == "repartition_current_checkpoint":
        if level is not None and level != "full":
            raise ManagerDecisionError("repartition_current_checkpoint is only legal for Full")
        if decision.next_step_notes:
            raise ManagerDecisionError("repartition_current_checkpoint must not include next_step_notes")
    if decision.action == "continue" and proposed_transition == "END" and decision.next_step_notes:
        raise ManagerDecisionError("accepted END must not include next_step_notes")
    if eligible_actions is not None and decision.action not in eligible_actions:
        raise ManagerDecisionError(f"action '{decision.action}' is not eligible at this control boundary")
    return decision


def validate_manager_note_authority(
    notes: tuple[str, ...],
    *,
    scope: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Validate manager advice against controller-owned scope facts.

    A path in an ordinary defect or test-focus note is evidence, not scope
    authority. Explicit allowlists and prohibitions are allowed only when they
    exactly restate the corresponding controller constraint; a subset would
    narrow the active plan. Plans and mandatory implementation requirements
    remain controller-owned regardless of the scope summary.
    """
    plan_selection_violation: ManagerNoteAuthorityError | None = None
    for note in notes:
        normalized = _normalize_authority_text(note)
        if (
            _DIRECT_PLAN_SELECTION_RE.search(normalized)
            or (
                _PLAN_REFERENCE_RE.search(normalized)
                and _PLAN_SELECTION_RE.search(normalized)
            )
        ):
            plan_selection_violation = plan_selection_violation or (
                _note_authority_error(
                    scope,
                    "replace or select an active plan",
                    category="plan_selection",
                )
            )
        if any(rule.search(normalized) for rule in _SCOPE_AUTHORITY_RULES):
            _validate_file_scope_claim(note, scope)
        if _IMPLEMENTATION_REQUIREMENT_RE.search(normalized):
            _raise_note_authority_error(
                scope,
                "impose a mandatory implementation requirement",
                category="mandatory_implementation",
            )
    if plan_selection_violation is not None:
        raise plan_selection_violation
    return notes


def _normalize_authority_text(note: str) -> str:
    """Normalize control-language matching; path extraction uses the source note."""
    return re.sub(r"\s+", " ", note.strip().lower())


def _raise_note_authority_error(
    scope: Mapping[str, Any] | None,
    detail: str,
    *,
    category: ManagerNoteAuthorityCategory,
) -> None:
    raise _note_authority_error(scope, detail, category=category)


def _note_authority_error(
    scope: Mapping[str, Any] | None,
    detail: str,
    *,
    category: ManagerNoteAuthorityCategory,
) -> ManagerNoteAuthorityError:
    identity = scope.get("active_plan_identity") if isinstance(scope, Mapping) else None
    suffix = f" for active plan {identity}" if isinstance(identity, str) else ""
    return ManagerNoteAuthorityError(
        f"next_step_notes may not {detail}{suffix}",
        category=category,
    )


def _normalized_scope_paths(
    scope: Mapping[str, Any] | None,
    key: str,
) -> frozenset[str] | None:
    if not isinstance(scope, Mapping) or scope.get("constraints_complete") is not True:
        return None
    raw_paths = scope.get(key)
    if not isinstance(raw_paths, (list, tuple)):
        return None
    normalized: set[str] = set()
    for raw_path in raw_paths:
        if not isinstance(raw_path, str):
            return None
        path = _normalize_note_path(raw_path)
        if path is None:
            return None
        normalized.add(path)
    return frozenset(normalized)


def _normalize_note_path(value: str) -> str | None:
    path = value.strip().strip(".,:;()[]{}")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", path)
    ):
        return None
    return path


def _note_paths(note: str) -> tuple[frozenset[str], bool]:
    """Return asserted paths and whether all path-shaped assertions parsed."""
    candidates = list(_CODE_SPAN_RE.findall(note))
    unquoted = _CODE_SPAN_RE.sub(" ", note)
    candidates.extend(
        candidate
        for candidate in _PATH_TOKEN_RE.findall(unquoted)
        if "/" in candidate or "." in candidate
    )
    paths: set[str] = set()
    malformed = False
    for candidate in candidates:
        normalized = _normalize_note_path(candidate)
        if normalized is None:
            malformed = True
        else:
            paths.add(normalized)
    return frozenset(paths), malformed


def _validate_file_scope_claim(
    note: str,
    scope: Mapping[str, Any] | None,
) -> None:
    is_prohibition = (
        _PROHIBITION_AUTHORITY_RE.search(_normalize_authority_text(note)) is not None
    )
    scope_key = "prohibited_paths" if is_prohibition else "allowed_paths"
    authoritative_paths = _normalized_scope_paths(scope, scope_key)
    asserted_paths, malformed = _note_paths(note)
    if (
        authoritative_paths is None
        or malformed
        or not asserted_paths
        or asserted_paths != authoritative_paths
    ):
        _raise_note_authority_error(
            scope,
            "assert file or scope authority",
            category="file_scope",
        )


def resolve_manager_role(
    config: WorkflowUserConfig,
    *,
    level: ManagerLevel,
    baseline_team: str | None,
    workflow_name: str,
) -> ManagerRoleResolution:
    workflow = config.workflows.get(workflow_name)
    if workflow is None:
        raise ValueError(f"unknown workflow '{workflow_name}'")
    if not workflow.manager_enabled:
        raise ValueError(f"manager supervision is disabled for workflow '{workflow_name}'")
    role = config.manager.lite_role if level == "lite" else config.manager.full_role
    if role is None:
        raise ValueError(f"manager {level} role is not configured")
    selector = _resolve_role(role, baseline_team, config)
    return ManagerRoleResolution(level=level, role=role, team=baseline_team, selector=selector)


def _resolve_role(role: str, team_name: str | None, config: WorkflowUserConfig) -> str:
    if team_name is not None and team_name not in config.teams:
        raise ValueError(f"unknown team '{team_name}'")
    try:
        selector = resolve_team_config(config, team_name).effective_roles.get(role)
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc
    if selector is None:
        raise ValueError(f"role '{role}' cannot be resolved")
    return selector


def eligible_implementation_upgrade(
    config: WorkflowUserConfig,
    *,
    role: str,
    baseline_team: str | None,
    most_recent_implementation_team: str | None = None,
    is_implementation_attempt: bool = True,
) -> EligibleImplementationUpgrade:
    """Resolve exactly one quality-upgrade edge without mutating routing state."""
    source_team = most_recent_implementation_team or baseline_team
    if not is_implementation_attempt:
        return EligibleImplementationUpgrade(False, source_team, None, role, None, None, "next step is not an implementation attempt")
    if source_team is None:
        return EligibleImplementationUpgrade(False, None, None, role, None, None, "baseline team is not configured")
    source = config.teams.get(source_team)
    if source is None:
        return EligibleImplementationUpgrade(False, source_team, None, role, None, None, "source team is not configured")
    target_team = source.upgrade_to
    if target_team is None:
        return EligibleImplementationUpgrade(False, source_team, None, role, None, None, "source team does not configure upgrade_to")
    target = config.teams.get(target_team)
    if target is None:
        return EligibleImplementationUpgrade(False, source_team, target_team, role, None, None, "upgrade target is not configured")
    try:
        source_selector = _resolve_role(role, source_team, config)
        target_selector = _resolve_role(role, target_team, config)
    except ValueError as exc:
        return EligibleImplementationUpgrade(False, source_team, target_team, role, None, None, str(exc))
    if source_selector == target_selector:
        return EligibleImplementationUpgrade(False, source_team, target_team, role, source_selector, target_selector, "upgrade target resolves to the same selector")
    return EligibleImplementationUpgrade(True, source_team, target_team, role, source_selector, target_selector)


def resolve_manager_skill_body(
    skill_name: str,
    *,
    store: SkillStore | None = None,
) -> str:
    """Return the validated Markdown body for one manager skill, read live.

    Bundled names resolve through the canonical store (saved bytes when
    present, otherwise the packaged resource). An explicitly configured
    non-bundled name resolves read-only from
    ``<canonical-root>/<safe-name>/SKILL.md`` and must exist as a valid
    contained regular file; it is never searched in harness directories and
    never falls back to built-in prose. Every read validates the complete
    document and strips YAML frontmatter using the store validator's parsed
    boundary. Callers read once per invocation and never cache the result, so
    a save between two invocations changes the next invocation's bytes.
    """
    if not isinstance(skill_name, str) or not skill_name:
        raise SkillStoreError("manager skill name must be a nonempty string")
    if is_bundled_skill_name(skill_name):
        active = store if store is not None else SkillStore()
        document = active.read(skill_name)
        body = parse_skill_document(skill_name, document.content).body
    else:
        body = _read_custom_skill_body(skill_name, store=store)
    if not body.strip():
        raise SkillStoreError(
            f"manager skill '{skill_name}' has an empty Markdown body"
        )
    return body


def _read_custom_skill_body(
    skill_name: str,
    *,
    store: SkillStore | None = None,
) -> str:
    """Read one explicitly configured non-bundled skill document, read-only."""
    if len(skill_name) > 255 or "\x00" in skill_name:
        raise SkillStoreError(f"unsafe skill name: {skill_name!r}")
    if skill_name in {".", ".."} or "/" in skill_name or "\\" in skill_name:
        raise SkillStoreError(f"unsafe skill name: {skill_name!r}")
    root = store.root if store is not None else SkillStore().root
    marker = Path(root) / ".metadata" / f"{skill_name}.refresh.json"
    try:
        marker_stat = os.lstat(marker)
    except OSError:
        marker_stat = None
    if marker_stat is not None:
        raise SkillStoreError(
            f"manager skill '{skill_name}' has an interrupted refresh "
            "transaction; run an explicit refresh to recover it before invoking"
        )
    skill_dir = Path(root) / skill_name
    try:
        directory_stat = os.lstat(skill_dir)
    except OSError:
        directory_stat = None
    if directory_stat is None:
        raise SkillStoreError(
            f"manager skill '{skill_name}' has no saved canonical document "
            f"at {skill_dir / 'SKILL.md'}"
        )
    if stat.S_ISLNK(directory_stat.st_mode):
        raise SkillStoreError(
            f"canonical skill directory must not be a symbolic link: {skill_name}"
        )
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise SkillStoreError(
            f"canonical skill path is not a directory: {skill_name}"
        )
    document_path = skill_dir / "SKILL.md"
    try:
        document_stat = os.lstat(document_path)
    except OSError:
        document_stat = None
    if document_stat is None:
        raise SkillStoreError(
            f"manager skill '{skill_name}' has no saved canonical document "
            f"at {document_path}"
        )
    if stat.S_ISLNK(document_stat.st_mode):
        raise SkillStoreError(
            f"canonical SKILL.md must not be a symbolic link: {skill_name}"
        )
    if not stat.S_ISREG(document_stat.st_mode):
        raise SkillStoreError(
            f"canonical SKILL.md is not a regular file: {skill_name}"
        )
    try:
        descriptor = os.open(document_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise SkillStoreError(
            f"cannot read manager skill '{skill_name}': {exc}"
        ) from exc
    try:
        payload = b"".join(iter(lambda: os.read(descriptor, 65536), b""))
    except OSError as exc:
        raise SkillStoreError(
            f"cannot read manager skill '{skill_name}': {exc}"
        ) from exc
    finally:
        os.close(descriptor)
    validate_skill_document(skill_name, payload)
    return parse_skill_document(skill_name, payload.decode("utf-8")).body


def _serialize_manager_user_prompt(
    runtime: Mapping[str, Any], context: Mapping[str, Any]
) -> str:
    """Serialize the exact manager user-prompt wire payload once."""
    context_json = (
        json.dumps(
            dict(context),
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=False,
        )
        if context.get("schema_version") == MANAGER_CONTEXT_SCHEMA_VERSION_V3
        else json.dumps(dict(context), indent=2, sort_keys=True)
    )
    return (
        "MANAGER_RUNTIME_JSON:\n"
        + json.dumps(runtime, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        + "\nMANAGER_CONTEXT_JSON:\n"
        + context_json
        + "\n"
    )


_HISTORY_REDUCTION_DETAILS: dict[str, tuple[str, str]] = {
    "run_extract_manager_decisions": (
        "decision_number",
        "manager/decision-{decision_number:03d}",
    ),
    "manager_decisions": (
        "decision_number",
        "manager/decision-{decision_number:03d}",
    ),
    "workflow_turns": (
        "turn_number",
        "turns/turn-{turn_number:03d}",
    ),
    "active_scope_rejection_ledger": (
        "review_turn_number",
        "turns/turn-{review_turn_number:03d}",
    ),
    "implementation_attempts": (
        "turn_number",
        "turns/turn-{turn_number:03d}",
    ),
    "checkpoint_repartitions": (
        "decision_number",
        "manager/decision-{decision_number:03d}",
    ),
}


def _merge_omitted_ranges(
    existing: Any, numbers: list[int]
) -> tuple[int, list[dict[str, int]]]:
    intervals: list[tuple[int, int]] = []
    if isinstance(existing, list):
        for item in existing:
            if not isinstance(item, Mapping):
                continue
            start, end = item.get("start"), item.get("end")
            if (
                isinstance(start, int)
                and not isinstance(start, bool)
                and isinstance(end, int)
                and not isinstance(end, bool)
                and start <= end
            ):
                intervals.append((start, end))
    intervals.extend((number, number) for number in numbers)
    if not intervals:
        return 0, []
    intervals.sort()
    merged: list[list[int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    ranges = [{"start": start, "end": end} for start, end in merged]
    return sum(end - start + 1 for start, end in merged), ranges


def _record_history_reduction(
    context: dict[str, Any],
    disclosure: dict[str, Any],
    *,
    category: str,
    records: list[Mapping[str, Any]],
) -> None:
    number_key, relative_path_pattern = _HISTORY_REDUCTION_DETAILS[category]
    numbers = [
        value
        for record in records
        for value in (record.get(number_key),)
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    if not numbers:
        return
    controller = context.get("controller_state")
    roots = controller.get("artifact_roots") if isinstance(controller, Mapping) else None
    artifact_root = (
        roots.get("run")
        if isinstance(roots, Mapping) and isinstance(roots.get("run"), str)
        else None
    )
    source_run_id = context.get("run_id")
    source_run_id = source_run_id if isinstance(source_run_id, str) else "unknown"
    omitted = disclosure.setdefault("omitted", [])
    if not isinstance(omitted, list):
        omitted = []
        disclosure["omitted"] = omitted
    descriptor = next(
        (
            item
            for item in omitted
            if isinstance(item, dict)
            and item.get("source_run_id") == source_run_id
            and item.get("category") == category
        ),
        None,
    )
    if descriptor is None:
        descriptor = {
            "source_run_id": source_run_id,
            "category": category,
            "artifact_root": artifact_root,
            "relative_path_pattern": relative_path_pattern,
            "omitted_count": 0,
            "omitted_ranges": [],
        }
        omitted.append(descriptor)
    elif descriptor.get("artifact_root") is None and artifact_root is not None:
        descriptor["artifact_root"] = artifact_root
    count, ranges = _merge_omitted_ranges(
        descriptor.get("omitted_ranges"), sorted(set(numbers))
    )
    descriptor["omitted_count"] = count
    descriptor["omitted_ranges"] = ranges
    reduced_categories = disclosure.setdefault("reduced_categories", [])
    if isinstance(reduced_categories, list) and category not in reduced_categories:
        reduced_categories.append(category)
    reduction_order = disclosure.setdefault("reduction_order", [])
    if isinstance(reduction_order, list) and category not in reduction_order:
        reduction_order.append(category)


def _update_history_retained_counts(
    context: dict[str, Any], disclosure: dict[str, Any]
) -> None:
    run_extract = context.get("run_extract")
    manager_decisions = context.get("manager_decisions")
    controller = context.get("controller_state")
    repartitions = controller.get("checkpoint_repartitions") if isinstance(controller, Mapping) else None
    run_records = [item for item in run_extract if isinstance(item, Mapping)] if isinstance(run_extract, (list, tuple)) else []
    manager_records = [item for item in manager_decisions if isinstance(item, Mapping)] if isinstance(manager_decisions, (list, tuple)) else []
    ledger = context.get("active_scope_rejection_ledger")
    ledger_records = [item for item in ledger if isinstance(item, Mapping)] if isinstance(ledger, (list, tuple)) else []
    attempts = context.get("implementation_attempts")
    attempt_records = []
    if isinstance(attempts, Mapping):
        raw_attempts = attempts.get("attempts")
        if isinstance(raw_attempts, (list, tuple)):
            attempt_records = [item for item in raw_attempts if isinstance(item, Mapping)]
    disclosure["retained_counts"] = {
        "run_extract": len(run_records),
        "run_extract_manager_decisions": sum(
            item.get("kind") == "manager_decision" for item in run_records
        ),
        "manager_decisions": len(manager_records),
        "workflow_turns": sum(
            item.get("kind") == "workflow_turn" for item in run_records
        ),
        "checkpoint_repartitions": (
            len(repartitions) if isinstance(repartitions, list) else 0
        ),
        "active_scope_rejection_ledger": len(ledger_records),
        "implementation_attempts": len(attempt_records),
    }


def _reduce_v3_history_for_budget(
    runtime: Mapping[str, Any], context: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Fit a v3 manifest to the 16 KiB target without dropping authority.

    New contexts already project historical rows into disk-backed evidence.
    This reducer also keeps saved/pre-projection v3 contexts readable: when a
    legacy manifest is larger than the target, it removes optional historical
    rows in a deterministic order and only then drops the optional latest-turn
    prose.  Current routing, scope, snapshot, budget, and artifact-reference
    facts remain in the candidate; an irreducibly large candidate is rejected
    by ``build_manager_prompts`` rather than silently enlarged.
    """
    initial_prompt = _serialize_manager_user_prompt(runtime, context)
    if len(initial_prompt.encode("utf-8")) <= MANAGER_INLINE_CONTEXT_TARGET_BYTES:
        return context

    controller = context.get("controller_state")
    finished = context.get("finished_turn")
    finished_semantic = (
        finished.get("semantic_result")
        if isinstance(finished, Mapping)
        else None
    )
    has_reducible_content = any(
        isinstance(item, Mapping)
        for item in context.get("run_extract", ())
    ) or any(
        isinstance(item, Mapping)
        for item in context.get("manager_decisions", ())
    ) or any(
        isinstance(item, Mapping)
        for item in context.get("active_scope_rejection_ledger", ())
    ) or (
        isinstance(context.get("implementation_attempts"), Mapping)
        and bool(context["implementation_attempts"].get("attempts"))
    ) or (
        isinstance(controller, Mapping)
        and any(
            isinstance(item, Mapping)
            for item in controller.get("checkpoint_repartitions", ())
        )
    ) or (
        isinstance(finished_semantic, Mapping)
        and isinstance(finished_semantic.get("result"), str)
        and bool(finished_semantic.get("result"))
    ) or (
        isinstance(finished, Mapping)
        and isinstance(finished.get("error"), str)
        and bool(finished.get("error"))
    )
    raw_disclosure = context.get("history_disclosure")
    if isinstance(raw_disclosure, Mapping) and raw_disclosure.get("omitted"):
        has_reducible_content = True
    if not has_reducible_content:
        return context

    candidate = deepcopy(dict(context))
    raw_disclosure = candidate.get("history_disclosure")
    disclosure = (
        deepcopy(dict(raw_disclosure))
        if isinstance(raw_disclosure, Mapping)
        else {"reduction_order": [], "reduced_categories": [], "omitted": []}
    )
    candidate["history_disclosure"] = disclosure

    def fits() -> bool:
        return len(_serialize_manager_user_prompt(runtime, candidate).encode("utf-8")) <= MANAGER_INLINE_CONTEXT_TARGET_BYTES

    run_extract = [
        dict(item)
        for item in candidate.get("run_extract", [])
        if isinstance(item, Mapping)
    ]
    duplicate_manager_records = [
        item for item in run_extract if item.get("kind") == "manager_decision"
    ]
    if duplicate_manager_records:
        run_extract = [
            item for item in run_extract if item.get("kind") != "manager_decision"
        ]
        candidate["run_extract"] = run_extract
        _record_history_reduction(
            candidate,
            disclosure,
            category="run_extract_manager_decisions",
            records=duplicate_manager_records,
        )
        _update_history_retained_counts(candidate, disclosure)
        if fits():
            return candidate

    manager_decisions = [
        dict(item)
        for item in candidate.get("manager_decisions", [])
        if isinstance(item, Mapping)
    ]
    manager_decisions.sort(
        key=lambda item: (
            item.get("decision_number", 0),
            item.get("turn_number", 0),
        )
    )
    candidate["manager_decisions"] = manager_decisions
    while manager_decisions and not fits():
        removed = manager_decisions.pop(0)
        _record_history_reduction(
            candidate,
            disclosure,
            category="manager_decisions",
            records=[removed],
        )
        _update_history_retained_counts(candidate, disclosure)
    if fits():
        return candidate

    while run_extract and not fits():
        workflow_candidates = [
            (index, item)
            for index, item in enumerate(run_extract)
            if item.get("kind") in {None, "workflow_turn"}
        ]
        workflow_index = min(
            workflow_candidates,
            key=lambda pair: (
                pair[1].get("turn_number", pair[1].get("number", 0)),
                pair[0],
            ),
            default=(None, None),
        )
        if workflow_index[0] is None:
            break
        removed = run_extract.pop(workflow_index[0])
        candidate["run_extract"] = run_extract
        _record_history_reduction(
            candidate,
            disclosure,
            category="workflow_turns",
            records=[removed],
        )
        _update_history_retained_counts(candidate, disclosure)
    if fits():
        return candidate

    ledger = [
        dict(item)
        for item in candidate.get("active_scope_rejection_ledger", [])
        if isinstance(item, Mapping)
    ]
    candidate["active_scope_rejection_ledger"] = ledger
    while ledger and not fits():
        removed = ledger.pop(0)
        _record_history_reduction(
            candidate,
            disclosure,
            category="active_scope_rejection_ledger",
            records=[removed],
        )
        _update_history_retained_counts(candidate, disclosure)
    if fits():
        return candidate

    attempts_value = candidate.get("implementation_attempts")
    attempts = [
        dict(item)
        for item in attempts_value.get("attempts", [])
        if isinstance(item, Mapping)
    ] if isinstance(attempts_value, Mapping) else []
    if isinstance(attempts_value, Mapping):
        candidate["implementation_attempts"] = {
            "scope_id": attempts_value.get("scope_id"),
            "attempts": attempts,
        }
    while attempts and not fits():
        removed = attempts.pop(0)
        _record_history_reduction(
            candidate,
            disclosure,
            category="implementation_attempts",
            records=[removed],
        )
        candidate["implementation_attempts"] = {
            "scope_id": (
                attempts_value.get("scope_id")
                if isinstance(attempts_value, Mapping)
                else None
            ),
            "attempts": attempts,
        }
        _update_history_retained_counts(candidate, disclosure)
    if fits():
        return candidate

    controller = candidate.get("controller_state")
    repartitions = [
        dict(item)
        for item in controller.get("checkpoint_repartitions", [])
        if isinstance(item, Mapping)
    ] if isinstance(controller, Mapping) else []
    if isinstance(controller, dict) and "checkpoint_repartitions" in controller:
        controller["checkpoint_repartitions"] = repartitions
    while repartitions and not fits():
        removed = repartitions.pop(0)
        _record_history_reduction(
            candidate,
            disclosure,
            category="checkpoint_repartitions",
            records=[removed],
        )
        _update_history_retained_counts(candidate, disclosure)
    if fits():
        return candidate

    # A latest semantic result and error are useful, but optional once the
    # fixed current facts and evidence references have been retained.
    finished = candidate.get("finished_turn")
    if isinstance(finished, dict):
        semantic = finished.get("semantic_result")
        if isinstance(semantic, dict):
            semantic.pop("result", None)
        finished.pop("error", None)
        diagnostics = finished.get("diagnostics")
        if isinstance(diagnostics, dict):
            diagnostics.pop("signal_provenance", None)
            diagnostics.pop("stdout_excerpt", None)
            diagnostics.pop("stderr_excerpt", None)
    if fits():
        return candidate

    # Old v3 manifests may carry omission descriptors from the pre-artifact
    # reducer. They are not required current facts and can be discarded from
    # the prompt candidate after their rows are gone.
    if isinstance(candidate.get("history_disclosure"), dict):
        candidate["history_disclosure"].pop("omitted", None)
    if fits():
        return candidate

    # No required current field is removed here.  The caller raises a bounded
    # prelaunch error if this candidate still exceeds the 16 KiB target.
    _update_history_retained_counts(candidate, disclosure)
    return candidate


def build_manager_prompts(
    context: Mapping[str, Any],
    *,
    skill_name: str = "aflow-manager",
    store: SkillStore | None = None,
) -> tuple[str, str]:
    level = context.get("level")
    if level not in {"lite", "full"}:
        raise ValueError("manager context must declare level 'lite' or 'full'")
    controller = (
        context.get("controller_state")
        if isinstance(context.get("controller_state"), Mapping)
        else {}
    )
    eligible_actions = {
        str(action)
        for action in controller.get("eligible_actions", ())
        if isinstance(action, str) and action
    }
    if level == "lite":
        eligible_actions.add("escalate_to_full")
    proposed_transition = controller.get("proposed_next_step")
    # The system instruction is the live skill Markdown body: behavioral prose
    # lives in exactly one editable source and Python never overrides it with
    # competing inline authority. Python contributes only structured runtime
    # data below; the output contract, schemas, and limits stay enforced in
    # code (parse/validate) and documented in the skill.
    system_prompt = resolve_manager_skill_body(skill_name, store=store)
    runtime = {
        "mode": "decide",
        "skill_name": skill_name,
        "level": level,
        "eligible_actions": sorted(eligible_actions),
        "proposed_transition": proposed_transition,
        "limits": {
            "max_notes": MAX_MANAGER_NOTES,
            "max_note_length": MAX_MANAGER_NOTE_LENGTH,
        },
    }
    prompt_context = (
        _reduce_v3_history_for_budget(runtime, context)
        if context.get("schema_version") == MANAGER_CONTEXT_SCHEMA_VERSION_V3
        else context
    )
    user_prompt = _serialize_manager_user_prompt(runtime, prompt_context)
    if context.get("schema_version") == MANAGER_CONTEXT_SCHEMA_VERSION_V3:
        prompt_bytes = len(user_prompt.encode("utf-8"))
        if prompt_bytes > MANAGER_INLINE_CONTEXT_MAX_BYTES:
            # Preserve the established 40 KiB diagnostic for the hard guard;
            # the target-specific error below handles the ordinary bounded
            # projection case.
            enforce_manager_inline_context_budget(prompt_context, user_prompt)
        if prompt_bytes > MANAGER_INLINE_CONTEXT_TARGET_BYTES:
            field_counts = " ".join(
                f"{key}={count}"
                for key, count in _per_field_utf8_byte_counts(prompt_context)
            )
            raise ManagerInlineContextLimitError(
                "manager inline context required current facts exceed the "
                f"{MANAGER_INLINE_CONTEXT_TARGET_BYTES}-byte target: "
                f"total_bytes={prompt_bytes}; {field_counts}",
                context=prompt_context,
                user_prompt=user_prompt,
                permitted_bytes=MANAGER_INLINE_CONTEXT_TARGET_BYTES,
            )
    enforce_manager_inline_context_budget(prompt_context, user_prompt)
    return system_prompt, user_prompt


def _per_field_utf8_byte_counts(context: Mapping[str, Any]) -> list[tuple[str, int]]:
    """Deterministic per-top-level-field byte counts without field content."""
    counts: list[tuple[str, int]] = []
    for key, value in dict(context).items():
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        counts.append((str(key), len(encoded)))
    counts.sort(key=lambda item: (item[0],))
    return counts


class ManagerInlineContextLimitError(ValueError):
    """Raised before any provider process starts when the compact manifest
    exceeds the deterministic hard limit.

    The message carries total bytes and per-top-level-field byte counts only:
    no field content, prompt text, secret-bearing paths, or environment data.
    """

    def __init__(
        self,
        message: str,
        *,
        context: Mapping[str, Any] | None = None,
        user_prompt: str | None = None,
        permitted_bytes: int | None = None,
    ) -> None:
        super().__init__(message)
        self.context = deepcopy(dict(context)) if context is not None else None
        self.user_prompt = user_prompt
        self.attempted_bytes = (
            len(user_prompt.encode("utf-8"))
            if isinstance(user_prompt, str)
            else None
        )
        self.permitted_bytes = (
            permitted_bytes
            if isinstance(permitted_bytes, int) and permitted_bytes > 0
            else MANAGER_INLINE_CONTEXT_MAX_BYTES
        )
        self.field_byte_counts = (
            dict(_per_field_utf8_byte_counts(context))
            if context is not None
            else {}
        )


def enforce_manager_inline_context_budget(
    context: Mapping[str, Any], user_prompt: str
) -> None:
    """Enforce the 40 KiB prelaunch hard limit on the exact UTF-8 user prompt."""
    total_bytes = len(user_prompt.encode("utf-8"))
    if total_bytes <= MANAGER_INLINE_CONTEXT_MAX_BYTES:
        return
    field_counts = " ".join(
        f"{key}={count}" for key, count in _per_field_utf8_byte_counts(context)
    )
    raise ManagerInlineContextLimitError(
        "manager inline context exceeds the "
        f"{MANAGER_INLINE_CONTEXT_MAX_BYTES}-byte hard limit: "
        f"total_bytes={total_bytes}; {field_counts}",
        context=context,
        user_prompt=user_prompt,
    )


def manager_prompt_metrics(
    context: Mapping[str, Any],
    *,
    system_prompt: str,
    user_prompt: str,
    argv: Any = None,
) -> dict[str, int]:
    """Non-sensitive prompt metrics for the persisted manager result.

    ``referenced_artifact_bytes`` are bytes the manager may read from the
    evidence store; they are not model-input bytes and callers must label
    them accordingly in analysis/UI output.
    """
    argv_tuple = tuple(argv or ())
    argv_bytes = sum(len(str(argument).encode("utf-8")) for argument in argv_tuple)
    referenced_artifact_count = 0
    referenced_artifact_bytes = 0
    if context.get("schema_version") == MANAGER_CONTEXT_SCHEMA_VERSION_V3:
        evidence = context.get("evidence")
        if isinstance(evidence, Mapping):
            for entry in evidence.values():
                if not isinstance(entry, Mapping) or entry.get("available") is not True:
                    continue
                if entry.get("artifact_path") is not None:
                    size = entry.get("byte_size")
                    if isinstance(size, int):
                        referenced_artifact_count += 1
                        referenced_artifact_bytes += size
                    continue
                reference = entry.get("reference")
                if isinstance(reference, Mapping) and isinstance(
                    reference.get("byte_size"), int
                ):
                    referenced_artifact_count += 1
                    referenced_artifact_bytes += reference["byte_size"]
    return {
        "system_prompt_bytes": len(system_prompt.encode("utf-8")),
        "user_prompt_bytes": len(user_prompt.encode("utf-8")),
        "argv_bytes": argv_bytes,
        "referenced_artifact_count": referenced_artifact_count,
        "referenced_artifact_bytes": referenced_artifact_bytes,
    }


def build_manager_note_correction_prompts(
    context: Mapping[str, Any],
    *,
    original_decision: ManagerDecisionV1,
    violation: ManagerNoteAuthorityError,
    skill_name: str = "aflow-manager",
    store: SkillStore | None = None,
) -> tuple[str, str]:
    """Build one compact correction request from the immutable call boundary.

    The system instruction is the live configured manager skill body (which
    covers the note-correction mode); Python contributes only the structured
    correction payload below.
    """
    if not violation.correctable:
        raise ValueError("only plan_selection note violations are correctable")
    level = context.get("level")
    if level not in {"lite", "full"}:
        raise ValueError("manager context must declare level 'lite' or 'full'")
    controller = (
        context.get("controller_state")
        if isinstance(context.get("controller_state"), Mapping)
        else {}
    )
    retry_action = original_decision.action in {
        "retry_current_step",
        "switch_to_backup_and_retry",
    }
    selected_scope = (
        context.get("retry_manager_note_scope")
        if retry_action and isinstance(context.get("retry_manager_note_scope"), Mapping)
        else context.get("manager_note_scope")
    )
    note_scope = dict(selected_scope) if isinstance(selected_scope, Mapping) else None
    target_plan_identity = (
        note_scope.get("active_plan_identity")
        if isinstance(note_scope, Mapping)
        else None
    )
    eligible_actions = [
        action
        for action in controller.get("eligible_actions", ())
        if isinstance(action, str) and action
    ]
    if level == "lite" and "escalate_to_full" not in eligible_actions:
        eligible_actions.append("escalate_to_full")
    payload = {
        "mode": "note_correction",
        "skill_name": skill_name,
        "decision_number": context.get("decision_number"),
        "level": level,
        "trigger": context.get("trigger"),
        "eligible_actions": sorted(eligible_actions),
        "proposed_transition": controller.get("proposed_next_step"),
        "manager_note_scope": note_scope,
        "target_plan_identity": target_plan_identity,
        "original_decision": original_decision.to_dict(),
        "violation": {
            "category": violation.category,
            "message": str(violation),
        },
    }
    system_prompt = resolve_manager_skill_body(skill_name, store=store)
    user_prompt = (
        "MANAGER_NOTE_CORRECTION_JSON:\n"
        + json.dumps(payload, indent=2, sort_keys=True)
        + "\n"
    )
    return system_prompt, user_prompt


def build_manager_note_correction_result(
    base_result: Mapping[str, Any],
    *,
    original_violation: ManagerNoteAuthorityError,
    correction_artifact_path: str,
    correction_status: str,
    final_decision: ManagerDecisionV1 | None,
    error: str | None = None,
) -> dict[str, Any]:
    """Render the canonical one-decision result after a correction sub-attempt."""
    result = dict(base_result)
    result.update({
        "attempt_count": 2,
        "correction_attempted": True,
        "original_violation": {
            "category": original_violation.category,
            "message": str(original_violation),
        },
        "correction": {
            "artifact_path": correction_artifact_path,
            "status": correction_status,
        },
    })
    if final_decision is None:
        result["status"] = "invalid"
        result["error"] = error or "manager note correction failed"
        for key in _DECISION_KEYS:
            result.pop(key, None)
        result["action"] = "invalid"
        result["reason"] = result["error"]
    else:
        result.update({"status": "accepted", **final_decision.to_dict()})
        result.pop("error", None)
    return result


def validate_manager_note_correction(
    original_decision: ManagerDecisionV1,
    corrected_decision: ManagerDecisionV1,
) -> ManagerDecisionV1:
    """Reject any correction that changes a non-note decision field."""
    for field in ("schema_version", "action", "reason", "stop_report"):
        if getattr(corrected_decision, field) != getattr(original_decision, field):
            raise ManagerDecisionError(
                f"manager note correction changed immutable field '{field}'"
            )
    return corrected_decision


def build_repartition_prompts(
    payload: Mapping[str, Any],
    *,
    mode: Literal["propose", "validate"],
    skill_name: str = "aflow-repartition-checkpoint",
    correction_findings: tuple[str, ...] = (),
    store: SkillStore | None = None,
) -> tuple[str, str]:
    """Build one strict, read-only Full repartition subcall.

    The system instruction is the live repartition skill Markdown body, which
    carries the mode contracts, semantic rules, and bounded-correction terms
    for ``propose``, ``validate``, and correction attempts. Python contributes
    only the structured call payload below; schemas stay enforced in code
    (proposal/verdict parsers plus mechanical validation).
    """
    if mode not in {"propose", "validate"}:
        raise ValueError("repartition mode must be 'propose' or 'validate'")
    system_prompt = resolve_manager_skill_body(skill_name, store=store)
    user_payload = dict(payload)
    user_payload["mode"] = mode
    user_payload["skill_name"] = skill_name
    if correction_findings:
        user_payload["correction_findings"] = list(correction_findings)
    user_prompt = (
        f"REPARTITION_{mode.upper()}_CONTEXT_JSON:\n"
        + json.dumps(user_payload, indent=2, sort_keys=True)
        + "\n"
    )
    return system_prompt, user_prompt


def render_manager_stop_report(
    *,
    context: Mapping[str, Any],
    stop_report: ManagerStopReport | None = None,
    failure_reason: str | None = None,
) -> str:
    """Render a self-contained report from manager output or local context."""
    finished_turn = context.get("finished_turn") if isinstance(context.get("finished_turn"), Mapping) else {}
    controller = context.get("controller_state") if isinstance(context.get("controller_state"), Mapping) else {}
    plan_state = context.get("plan_state") if isinstance(context.get("plan_state"), Mapping) else {}
    current_checkpoint: Any = plan_state.get("current_checkpoint")
    if current_checkpoint is None:
        for snapshot_key in ("snapshot_before", "snapshot_after"):
            snapshot = finished_turn.get(snapshot_key)
            if not isinstance(snapshot, Mapping):
                continue
            current_checkpoint = snapshot.get("current_checkpoint_name")
            if current_checkpoint is None:
                current_checkpoint = snapshot.get("current_checkpoint")
            if current_checkpoint is not None:
                break
    if isinstance(current_checkpoint, Mapping):
        current_checkpoint = (
            current_checkpoint.get("name")
            or current_checkpoint.get("checkpoint_name")
            or current_checkpoint.get("index")
        )
    if stop_report is None:
        failure_kind = context.get("failure_kind")
        if not isinstance(failure_kind, str):
            failure_kind = controller.get("failure_kind")
        budget_failure = failure_kind == "manager_input_budget"
        manager_failure_reason = context.get("manager_failure_reason")
        if not isinstance(manager_failure_reason, str) or not manager_failure_reason.strip():
            manager_failure_reason = controller.get("manager_failure_reason")
        if not isinstance(manager_failure_reason, str) or not manager_failure_reason.strip():
            manager_failure_reason = (
                "Manager input exceeds its byte budget before provider launch."
            )
        terminal_incident = bool(controller.get("terminal"))
        incident_reason = next((
            str(value)
            for value in (
                controller.get("lite_evidence"),
                finished_turn.get("error"),
            )
            if isinstance(value, str) and value.strip()
        ), None)
        protocol_failure = (
            failure_reason
            or "Manager supervision could not continue the workflow safely."
        )
        summary = (
            incident_reason
            if terminal_incident and incident_reason is not None
            else protocol_failure
        )
        if budget_failure:
            root_cause = (
                f"The controller reached a terminal workflow incident. "
                f"{manager_failure_reason} The manager provider was not launched."
                if terminal_incident and incident_reason is not None
                else (
                    f"{manager_failure_reason} "
                    "The manager provider was not launched."
                )
            )
        else:
            root_cause = (
                f"The controller reached a terminal workflow incident. "
                f"The manager response was also unavailable, invalid, or illegal: "
                f"{protocol_failure}"
                if terminal_incident and incident_reason is not None
                else "Manager output was unavailable, invalid, or illegal for the current controller boundary."
            )
        evidence_items = (
            finished_turn.get("error"),
            finished_turn.get("status"),
            context.get("trigger"),
            incident_reason,
            f"Manager decision error: {protocol_failure}",
        )
        report = ManagerStopReport(
            summary=summary,
            root_cause=root_cause,
            evidence=tuple(dict.fromkeys(
                str(item) for item in evidence_items if item
            )),
            attempts=f"Manager decision {context.get('decision_number', 'unknown')} at {context.get('level', 'unknown')} level.",
            workspace_state=f"Baseline team: {controller.get('baseline_team')}; active plan: {plan_state.get('active_plan_path')}; turn: {finished_turn.get('turn_number')}.",
            next_actions=("Inspect the stored manager context and result artifacts.", "Correct the configuration or controller condition, then resume from the durable run state."),
        )
    else:
        report = stop_report
    evidence = report.evidence or ("No additional evidence was supplied.",)
    lines = [
        "# AFlow manager report",
        "",
        "## Summary",
        report.summary,
        "",
        "## Likely root cause",
        report.root_cause,
        "",
        "## Evidence",
        *(f"- {item}" for item in evidence),
        "",
        "## Attempts",
        report.attempts,
        "",
        "## Workspace and plan state",
        report.workspace_state,
        f"- Terminal status: {'terminal incident' if controller.get('terminal') else finished_turn.get('status', 'unknown')}",
        f"- Original plan: {plan_state.get('original_plan_path', 'unknown')}",
        f"- Active plan: {plan_state.get('active_plan_path', 'unknown')}",
        f"- Checkpoint: {current_checkpoint if current_checkpoint is not None else 'unknown'}",
        f"- Proposed controller action: {controller.get('proposed_action', 'unknown')}",
        f"- Branch: {controller.get('workspace_state', {}).get('branch', 'unknown') if isinstance(controller.get('workspace_state'), Mapping) else 'unknown'}",
        f"- HEAD: {controller.get('workspace_state', {}).get('head', 'unknown') if isinstance(controller.get('workspace_state'), Mapping) else 'unknown'}",
        f"- Dirty worktree: {controller.get('workspace_state', {}).get('dirty_worktree', 'unknown') if isinstance(controller.get('workspace_state'), Mapping) else 'unknown'}",
        f"- Merge state: {controller.get('workspace_state', {}).get('merge_state', 'unknown') if isinstance(controller.get('workspace_state'), Mapping) else 'unknown'}",
        "",
        "## Next actions",
        *(f"- {item}" for item in report.next_actions),
        "",
        "## Artifact references",
        f"- Run ID: {context.get('run_id', 'unknown')}",
        f"- Finished turn: {finished_turn.get('turn_number', 'unknown')}",
        f"- Active plan: {plan_state.get('active_plan_path', 'unknown')}",
        f"- Turn artifacts: {', '.join(str(item.get('path')) for item in finished_turn.get('raw_artifacts', ()) if isinstance(item, Mapping)) or 'unknown'}",
        f"- Manager decision: manager/decision-{int(context.get('decision_number', 0) or 0):03d}",
    ]
    return "\n".join(lines) + "\n"
