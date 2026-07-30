"""Strict protocol and routing contracts for interstep manager supervision.

This module deliberately contains no workflow-loop integration.  It provides
validated inputs, decisions, routing helpers, and durable-report rendering for
the controller integration that follows in a later checkpoint.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from typing import Any, Literal, Mapping

from .config import WorkflowUserConfig


ManagerLevel = Literal["lite", "full"]
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
_SINGLE_JSON_FENCE = re.compile(
    r"\A\s*```json[ \t]*\r?\n(?P<body>.*?)\r?\n```[ \t]*\s*\Z",
    re.DOTALL,
)


class ManagerDecisionError(ValueError):
    """Raised when manager output is not a legal closed-protocol decision."""


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


def resolve_manager_role(
    config: WorkflowUserConfig,
    *,
    level: ManagerLevel,
    baseline_team: str | None,
) -> ManagerRoleResolution:
    if not config.manager.enabled:
        raise ValueError("manager supervision is disabled")
    role = config.manager.lite_role if level == "lite" else config.manager.full_role
    if role is None:
        raise ValueError(f"manager {level} role is not configured")
    selector = _resolve_role(role, baseline_team, config)
    return ManagerRoleResolution(level=level, role=role, team=baseline_team, selector=selector)


def _resolve_role(role: str, team_name: str | None, config: WorkflowUserConfig) -> str:
    selector = config.roles.get(role)
    if team_name is not None:
        team = config.teams.get(team_name)
        if team is None:
            raise ValueError(f"unknown team '{team_name}'")
        selector = team.roles.get(role, selector)
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


def build_manager_prompts(
    context: Mapping[str, Any],
    *,
    skill_name: str = "aflow-manager",
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
    eligible_text = ", ".join(sorted(eligible_actions)) or "none"
    reviewer_rejections = int(controller.get("reviewer_rejection_count", 0) or 0)
    eligible_upgrade = (
        controller.get("eligible_upgrade")
        if isinstance(controller.get("eligible_upgrade"), Mapping)
        else {}
    )
    normal_shape = json.dumps({
        "schema_version": 1,
        "action": "continue",
        "reason": "Concise non-empty evidence-based reason.",
        "next_step_notes": [],
        "stop_report": None,
    }, separators=(",", ":"))
    stop_shape = json.dumps({
        "schema_version": 1,
        "action": "stop",
        "reason": "Concise non-empty evidence-based reason.",
        "next_step_notes": [],
        "stop_report": {
            "summary": "Non-empty summary.",
            "root_cause": "Non-empty root cause.",
            "evidence": ["At least one non-empty evidence string."],
            "attempts": "Non-empty attempts summary.",
            "workspace_state": "Non-empty workspace and plan summary.",
            "next_actions": ["At least one non-empty next action."],
        },
    }, separators=(",", ":"))
    system_prompt = "\n".join((
        "You are the AFlow interstep manager.",
        f"Supervision level: {level.upper()}.",
        f"Use the configured manager skill '{skill_name}' when it is available.",
        "The inline protocol below is authoritative even when that skill is unavailable.",
        "You are read-only: do not edit source, plans, git state, configuration, or run files.",
        "Accept or alter only the controller action exposed as eligible in the supplied context.",
        "Do not choose workflow nodes, teams, selectors, or business logic.",
        *(
            (
                "When Lite, evaluate the rejection cause before choosing an action:",
                "- For a bounded omission with a valid repair overlay, continue with the same worker.",
                "- For broad misunderstanding or capability gaps, upgrade_next_implementation.",
                "- For structural ambiguity or scope pressure, escalate_to_full.",
                "Continue and upgrade are both legal first-rejection actions; neither is forced.",
            )
            if level == "lite"
            else ()
        ),
        f"Eligible actions at this boundary: {eligible_text}.",
        *(
            (
                "When Full, repartition_current_checkpoint splits the current "
                "checkpoint into smaller children. Use it only for structural "
                "oversize/indivisibility that stalls the scope. Do not name "
                "workflow steps, teams, or business logic. next_step_notes must be [].",
            )
            if level == "full" and "repartition_current_checkpoint" in eligible_actions
            else ()
        ),
        "Return exactly one JSON object with schema_version, action, reason, next_step_notes, and stop_report.",
        "schema_version must be the number 1. reason must be a non-empty string.",
        (
            "next_step_notes must always be an array of non-empty strings, never a string or null; "
            f"use at most {MAX_MANAGER_NOTES} notes and at most "
            f"{MAX_MANAGER_NOTE_LENGTH} characters per note."
        ),
        "For stop, escalate_to_full, and accepted END, next_step_notes must be [].",
        "For every non-stop action, stop_report must be null.",
        "For stop, stop_report must be an object with exactly summary, root_cause, evidence, attempts, workspace_state, and next_actions; evidence and next_actions must be non-empty arrays of non-empty strings and the other fields must be non-empty strings.",
        f"Non-stop response shape: {normal_shape}",
        f"Stop response shape: {stop_shape}",
        "No Markdown fences or explanatory text.",
    ))
    user_prompt = (
        "MANAGER_CONTEXT_JSON:\n"
        + json.dumps(dict(context), indent=2, sort_keys=True)
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
    if stop_report is None:
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
        f"- Checkpoint: {plan_state.get('current_checkpoint', 'unknown')}",
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
