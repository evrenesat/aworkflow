from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from collections.abc import Mapping
import tomllib

from .harnesses import ADAPTERS

VALID_CONDITION_SYMBOLS = frozenset({"DONE", "NEW_PLAN_EXISTS", "MAX_TURNS_REACHED"})


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class HarnessProfileConfig:
    model: str | None = None
    effort: str | None = None


def _config_path() -> Path:
    return Path.home() / ".config" / "aflow" / "aflow.toml"


def _require_table(value: object, *, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"expected {path} to be a table")
    return value


def _require_text(value: object, *, path: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"expected {path} to be a string")
    text = value.strip()
    if not text:
        raise ConfigError(f"{path} must not be empty")
    return text


def _optional_text(value: object | None, *, path: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, path=path)


def _parse_profile_table(raw: Mapping[str, object], *, path: str) -> HarnessProfileConfig:
    allowed = {"model", "effort"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unsupported keys in {path}: {', '.join(unknown)}")
    return HarnessProfileConfig(
        model=_optional_text(raw.get("model"), path=f"{path}.model"),
        effort=_optional_text(raw.get("effort"), path=f"{path}.effort"),
    )


DEFAULT_KEEP_RUNS = 20


DEFAULT_MAX_TURNS = 15


DEFAULT_MAX_SAME_STEP_TURNS = 5


@dataclass(frozen=True)
class AflowSection:
    default_workflow: str | None = None
    keep_runs: int = DEFAULT_KEEP_RUNS
    max_turns: int = DEFAULT_MAX_TURNS
    retry_inconsistent_checkpoint_state: int = 0
    banner_files_limit: int = 10
    max_same_step_turns: int = DEFAULT_MAX_SAME_STEP_TURNS
    team_lead: str | None = None
    worktree_prefix: str | None = None
    branch_prefix: str | None = None
    worktree_root: str | None = None


WORKFLOW_LIFECYCLE_KEYS = frozenset({"setup", "teardown", "main_branch", "merge_prompt"})

VALID_LIFECYCLE_COMBOS: frozenset[tuple[tuple[str, ...], tuple[str, ...]]] = frozenset({
    ((), ()),
    (("branch",), ("merge",)),
    (("worktree", "branch"), ("merge", "rm_worktree")),
})


@dataclass(frozen=True)
class WorkflowLifecycleDefaults:
    setup: tuple[str, ...] = ()
    teardown: tuple[str, ...] = ()
    main_branch: str | None = None
    merge_prompt: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowHarnessConfig:
    profiles: dict[str, HarnessProfileConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class TeamConfig:
    roles: dict[str, str] = field(default_factory=dict)
    role_prompts: dict[str, str] = field(default_factory=dict)
    backup_team: str | None = None
    upgrade_to: str | None = None


@dataclass(frozen=True)
class ManagerConfig:
    enabled: bool = False
    lite_role: str | None = None
    full_role: str | None = None
    full_after_stalled_turns: int = 2
    skill: str = "aflow-manager"
    repartition_skill: str = "aflow-repartition-checkpoint"


@dataclass(frozen=True)
class HarnessErrorRecoveryRuleConfig:
    action: str
    match: tuple[str, ...] = ()
    delay_seconds: int = 0


@dataclass(frozen=True)
class HarnessErrorRecoveryConfig:
    rules: tuple[HarnessErrorRecoveryRuleConfig, ...] = ()
    max_consecutive_recoveries: int = 3
    team_lead_skill: str = "aflow-harness-recovery-lead"


@dataclass(frozen=True)
class ErrorHandlingConfig:
    harness_error_recovery: HarnessErrorRecoveryConfig = field(
        default_factory=HarnessErrorRecoveryConfig
    )


@dataclass(frozen=True)
class GoTransition:
    to: str
    when: str | None = None
    preserve_active_plan: bool = False


@dataclass(frozen=True)
class WorkflowStepConfig:
    role: str
    prompts: tuple[str, ...] = ()
    go: tuple[GoTransition, ...] = ()


@dataclass(frozen=True)
class WorkflowConfig:
    # declared_steps preserves the original ordered graph for rendering.
    declared_steps: dict[str, WorkflowStepConfig] = field(default_factory=dict)
    steps: dict[str, WorkflowStepConfig] = field(default_factory=dict)
    first_step: str | None = None
    excluded_steps: tuple[str, ...] = ()
    retry_inconsistent_checkpoint_state: int | None = None
    team: str | None = None
    extends: str | None = None
    # Lifecycle fields: None means "not explicitly set on this workflow" (inherit from base/defaults).
    # After _materialize_workflows these are always resolved to non-None tuples.
    setup: tuple[str, ...] | None = None
    teardown: tuple[str, ...] | None = None
    main_branch: str | None = None
    merge_prompt: tuple[str, ...] | None = None


@dataclass(frozen=True)
class WorkflowUserConfig:
    aflow: AflowSection = field(default_factory=AflowSection)
    harnesses: dict[str, WorkflowHarnessConfig] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    role_prompts: dict[str, str] = field(default_factory=dict)
    teams: dict[str, TeamConfig] = field(default_factory=dict)
    manager: ManagerConfig = field(default_factory=ManagerConfig)
    error_handling: ErrorHandlingConfig = field(default_factory=ErrorHandlingConfig)
    workflows: dict[str, WorkflowConfig] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)


def _validate_condition_symbols(expression: str, *, path: str) -> None:
    tokens = re.findall(
        r"[A-Za-z_][A-Za-z0-9_]*"
        r"|&&"
        r"|\|\|"
        r"|!==?"
        r"|<>|<=|>=|==|[=<>+\-*/%]"
        r"|!"
        r"|[()]"
        r"|\S",
        expression,
    )
    allowed = VALID_CONDITION_SYMBOLS | {"&&", "||", "!", "(", ")"}
    invalid_tokens = sorted(set(t for t in tokens if t not in allowed))
    if invalid_tokens:
        raise ConfigError(
            f"unsupported tokens in {path}: {', '.join(invalid_tokens)}"
        )


def _parse_aflow_section(raw: Mapping[str, object], *, path: str) -> AflowSection:
    allowed = {
        "default_workflow",
        "keep_runs",
        "max_turns",
        "retry_inconsistent_checkpoint_state",
        "banner_files_limit",
        "max_same_step_turns",
        "team_lead",
        "worktree_prefix",
        "branch_prefix",
        "worktree_root",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unsupported keys in {path}: {', '.join(unknown)}")
    keep_runs = DEFAULT_KEEP_RUNS
    if "keep_runs" in raw:
        value = raw["keep_runs"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"{path}.keep_runs must be a positive integer")
        keep_runs = value
    max_turns = DEFAULT_MAX_TURNS
    if "max_turns" in raw:
        value = raw["max_turns"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"{path}.max_turns must be a positive integer")
        max_turns = value
    retry_inconsistent_checkpoint_state = 0
    if "retry_inconsistent_checkpoint_state" in raw:
        value = raw["retry_inconsistent_checkpoint_state"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(
                f"{path}.retry_inconsistent_checkpoint_state must be a non-negative integer"
            )
        retry_inconsistent_checkpoint_state = value
    banner_files_limit = 10
    if "banner_files_limit" in raw:
        value = raw["banner_files_limit"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(
                f"{path}.banner_files_limit must be a positive integer"
            )
        banner_files_limit = value
    max_same_step_turns = DEFAULT_MAX_SAME_STEP_TURNS
    if "max_same_step_turns" in raw:
        value = raw["max_same_step_turns"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(
                f"{path}.max_same_step_turns must be a non-negative integer"
            )
        max_same_step_turns = value
    return AflowSection(
        default_workflow=_optional_text(
            raw.get("default_workflow"), path=f"{path}.default_workflow"
        ),
        keep_runs=keep_runs,
        max_turns=max_turns,
        retry_inconsistent_checkpoint_state=retry_inconsistent_checkpoint_state,
        banner_files_limit=banner_files_limit,
        max_same_step_turns=max_same_step_turns,
        team_lead=_optional_text(raw.get("team_lead"), path=f"{path}.team_lead"),
        worktree_prefix=_optional_text(
            raw.get("worktree_prefix"), path=f"{path}.worktree_prefix"
        ),
        branch_prefix=_optional_text(
            raw.get("branch_prefix"), path=f"{path}.branch_prefix"
        ),
        worktree_root=_optional_text(
            raw.get("worktree_root"), path=f"{path}.worktree_root"
        ),
    )


def _parse_selector_value(
    value: object,
    *,
    path: str,
) -> str:
    selector = _require_text(value, path=path)
    if "." not in selector:
        raise ConfigError(
            f"expected {path} to be a fully qualified harness.profile selector"
        )
    harness_name, _, profile_name = selector.partition(".")
    if not harness_name or not profile_name:
        raise ConfigError(f"invalid selector '{selector}' in {path}")
    return selector


def _parse_workflow_harness(
    raw: Mapping[str, object], *, path: str
) -> WorkflowHarnessConfig:
    allowed = {"profiles"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unsupported keys in {path}: {', '.join(unknown)}")
    profiles: dict[str, HarnessProfileConfig] = {}
    profiles_value = raw.get("profiles")
    if profiles_value is not None:
        profiles_table = _require_table(profiles_value, path=f"{path}.profiles")
        for profile_name, profile_value in profiles_table.items():
            profile_key = _require_text(
                profile_name, path=f"{path}.profiles key"
            )
            profile_table = _require_table(
                profile_value, path=f"{path}.profiles.{profile_key}"
            )
            profiles[profile_key] = _parse_profile_table(
                profile_table, path=f"{path}.profiles.{profile_key}"
            )
    return WorkflowHarnessConfig(profiles=profiles)


def _parse_team_config(raw: Mapping[str, object], *, path: str) -> TeamConfig:
    reserved_keys = {"roles", "prompts", "backup_team", "upgrade_to"}
    inline_role_keys = [key for key in raw if key not in reserved_keys]
    roles_value = raw.get("roles")
    if roles_value is not None and inline_role_keys:
        inline_roles = ", ".join(sorted(inline_role_keys))
        raise ConfigError(
            f"mixed legacy inline role keys and [roles] table in {path}: {inline_roles}"
        )
    roles: dict[str, str] = {}
    if roles_value is not None:
        roles_table = _require_table(roles_value, path=f"{path}.roles")
        roles = _parse_role_map(roles_table, path=f"{path}.roles")
    else:
        for role_name in inline_role_keys:
            roles[role_name] = _parse_selector_value(
                raw[role_name], path=f"{path}.{role_name}"
            )
    role_prompts: dict[str, str] = {}
    prompts_value = raw.get("prompts")
    if prompts_value is not None:
        prompts_table = _require_table(prompts_value, path=f"{path}.prompts")
        role_prompts = _parse_role_prompt_map(
            prompts_table, path=f"{path}.prompts"
        )
    return TeamConfig(
        roles=roles,
        role_prompts=role_prompts,
        backup_team=_optional_text(raw.get("backup_team"), path=f"{path}.backup_team"),
        upgrade_to=_optional_text(raw.get("upgrade_to"), path=f"{path}.upgrade_to"),
    )


def _parse_manager_config(raw: Mapping[str, object], *, path: str) -> ManagerConfig:
    allowed = {"enabled", "lite_role", "full_role", "full_after_stalled_turns", "skill", "repartition_skill"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unsupported keys in {path}: {', '.join(unknown)}")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{path}.enabled must be a boolean")
    lite_role = _optional_text(raw.get("lite_role"), path=f"{path}.lite_role")
    full_role = _optional_text(raw.get("full_role"), path=f"{path}.full_role")
    if enabled and lite_role is None:
        raise ConfigError(f"{path}.lite_role is required when {path}.enabled is true")
    if enabled and full_role is None:
        raise ConfigError(f"{path}.full_role is required when {path}.enabled is true")
    full_after_stalled_turns = raw.get("full_after_stalled_turns", 2)
    if (
        not isinstance(full_after_stalled_turns, int)
        or isinstance(full_after_stalled_turns, bool)
        or full_after_stalled_turns < 1
    ):
        raise ConfigError(f"{path}.full_after_stalled_turns must be an integer at least 1")
    skill = _optional_text(raw.get("skill"), path=f"{path}.skill") or "aflow-manager"
    repartition_skill = _optional_text(raw.get("repartition_skill"), path=f"{path}.repartition_skill") or "aflow-repartition-checkpoint"
    return ManagerConfig(
        enabled=enabled,
        lite_role=lite_role,
        full_role=full_role,
        full_after_stalled_turns=full_after_stalled_turns,
        skill=skill,
        repartition_skill=repartition_skill,
    )


def _parse_role_map(
    raw: Mapping[str, object], *, path: str
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for key, value in raw.items():
        role_name = _require_text(key, path=f"{path} key")
        mapping[role_name] = _parse_selector_value(
            value, path=f"{path}.{role_name}"
        )
    return mapping


def _parse_role_prompt_map(
    raw: Mapping[str, object], *, path: str
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for key, value in raw.items():
        role_name = _require_text(key, path=f"{path} key")
        mapping[role_name] = _require_text(value, path=f"{path}.{role_name}")
    return mapping


def _parse_harness_error_recovery_rule(
    raw: Mapping[str, object], *, path: str
) -> HarnessErrorRecoveryRuleConfig:
    allowed = {"action", "match", "delay_seconds"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unsupported keys in {path}: {', '.join(unknown)}")
    if "action" not in raw:
        raise ConfigError(f"missing 'action' in {path}")
    action = _require_text(raw["action"], path=f"{path}.action")
    allowed_actions = {
        "retry_same_team_after_delay",
        "switch_to_backup_team_and_retry",
        "fail_immediately",
    }
    if action not in allowed_actions:
        actions = ", ".join(sorted(allowed_actions))
        raise ConfigError(
            f"{path}.action must be one of: {actions}"
        )
    if "match" not in raw:
        raise ConfigError(f"missing 'match' in {path}")
    match_value = raw["match"]
    if not isinstance(match_value, list):
        raise ConfigError(f"expected {path}.match to be an array")
    match = tuple(
        _require_text(item, path=f"{path}.match[{i}]")
        for i, item in enumerate(match_value)
    )
    if not match:
        raise ConfigError(f"{path}.match must not be empty")
    delay_seconds = 0
    if "delay_seconds" in raw:
        if action == "fail_immediately":
            raise ConfigError(
                f"{path}.delay_seconds is only allowed for retry and switch actions"
            )
        value = raw["delay_seconds"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"{path}.delay_seconds must be a non-negative integer")
        delay_seconds = value
    return HarnessErrorRecoveryRuleConfig(
        action=action,
        match=match,
        delay_seconds=delay_seconds,
    )


def _parse_harness_error_recovery_config(
    raw: Mapping[str, object], *, path: str
) -> HarnessErrorRecoveryConfig:
    allowed = {"rules", "max_consecutive_recoveries", "team_lead_skill"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unsupported keys in {path}: {', '.join(unknown)}")
    rules: list[HarnessErrorRecoveryRuleConfig] = []
    rules_value = raw.get("rules")
    if rules_value is not None:
        if not isinstance(rules_value, list):
            raise ConfigError(f"expected {path}.rules to be an array of tables")
        for i, rule_value in enumerate(rules_value):
            rule_path = f"{path}.rules[{i}]"
            rule_table = _require_table(rule_value, path=rule_path)
            rules.append(
                _parse_harness_error_recovery_rule(rule_table, path=rule_path)
            )
    max_consecutive_recoveries = 3
    if "max_consecutive_recoveries" in raw:
        value = raw["max_consecutive_recoveries"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(
                f"{path}.max_consecutive_recoveries must be a non-negative integer"
            )
        max_consecutive_recoveries = value
    team_lead_skill = _optional_text(
        raw.get("team_lead_skill"), path=f"{path}.team_lead_skill"
    )
    if team_lead_skill is None:
        team_lead_skill = "aflow-harness-recovery-lead"
    return HarnessErrorRecoveryConfig(
        rules=tuple(rules),
        max_consecutive_recoveries=max_consecutive_recoveries,
        team_lead_skill=team_lead_skill,
    )


def _parse_error_handling_config(
    raw: Mapping[str, object], *, path: str
) -> ErrorHandlingConfig:
    allowed = {"harness_error_recovery"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unsupported keys in {path}: {', '.join(unknown)}")
    recovery = HarnessErrorRecoveryConfig()
    recovery_value = raw.get("harness_error_recovery")
    if recovery_value is not None:
        recovery_table = _require_table(
            recovery_value, path=f"{path}.harness_error_recovery"
        )
        recovery = _parse_harness_error_recovery_config(
            recovery_table, path=f"{path}.harness_error_recovery"
        )
    return ErrorHandlingConfig(harness_error_recovery=recovery)


def _parse_go_transitions(
    raw_list: object, *, path: str
) -> tuple[GoTransition, ...]:
    if not isinstance(raw_list, list):
        raise ConfigError(f"expected {path}.go to be an array")
    transitions: list[GoTransition] = []
    for i, entry in enumerate(raw_list):
        entry_path = f"{path}.go[{i}]"
        if not isinstance(entry, Mapping):
            raise ConfigError(f"expected {entry_path} to be an inline table")
        if "to" not in entry:
            raise ConfigError(f"missing 'to' in {entry_path}")
        to_value = _require_text(entry["to"], path=f"{entry_path}.to")
        if to_value != "END" and not re.match(
            r"^[a-zA-Z_][a-zA-Z0-9_]*$", to_value
        ):
            raise ConfigError(
                f"invalid transition target '{to_value}' in {entry_path}.to"
            )
        when_value = entry.get("when")
        when_str: str | None = None
        if when_value is not None:
            when_str = _require_text(when_value, path=f"{entry_path}.when")
            _validate_condition_symbols(when_str, path=entry_path)
        preserve_active_plan = entry.get("preserve_active_plan", False)
        if not isinstance(preserve_active_plan, bool):
            raise ConfigError(
                f"expected {entry_path}.preserve_active_plan to be a boolean"
            )
        if to_value == "END" and preserve_active_plan:
            raise ConfigError(
                f"{entry_path}.preserve_active_plan cannot be true when "
                f"{entry_path}.to is 'END'"
            )
        transitions.append(
            GoTransition(
                to=to_value,
                when=when_str,
                preserve_active_plan=preserve_active_plan,
            )
        )
    return tuple(transitions)


def _parse_lifecycle_array(value: object, *, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"expected {path} to be an array")
    return tuple(
        _require_text(item, path=f"{path}[{i}]") for i, item in enumerate(value)
    )


def _parse_excluded_steps(value: object, *, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"expected {path} to be an array")
    excluded_steps: list[str] = []
    seen: set[str] = set()
    for i, item in enumerate(value):
        step_name = _require_text(item, path=f"{path}[{i}]")
        if step_name in seen:
            raise ConfigError(f"duplicate step '{step_name}' in {path}")
        seen.add(step_name)
        excluded_steps.append(step_name)
    return tuple(excluded_steps)


def _parse_workflow_steps(
    raw: Mapping[str, object], *, path: str
) -> WorkflowConfig:
    steps: dict[str, WorkflowStepConfig] = {}
    first_step: str | None = None
    for step_name, step_value in raw.items():
        step_key = _require_text(step_name, path=f"{path} key")
        if first_step is None:
            first_step = step_key
        step_table = _require_table(step_value, path=f"{path}.{step_key}")
        allowed = {"role", "prompts", "go"}
        unknown = sorted(set(step_table) - allowed)
        if unknown:
            raise ConfigError(
                f"unsupported keys in {path}.{step_key}: {', '.join(unknown)}"
            )
        if "role" not in step_table:
            raise ConfigError(f"missing 'role' in {path}.{step_key}")
        role_value = _require_text(
            step_table["role"], path=f"{path}.{step_key}.role"
        )
        if "prompts" not in step_table:
            raise ConfigError(f"missing 'prompts' in {path}.{step_key}")
        prompts_value = step_table["prompts"]
        if not isinstance(prompts_value, list):
            raise ConfigError(f"expected {path}.{step_key}.prompts to be an array")
        if not prompts_value:
            raise ConfigError(f"{path}.{step_key}.prompts must not be empty")
        prompts = tuple(
            _require_text(p, path=f"{path}.{step_key}.prompts[{i}]")
            for i, p in enumerate(prompts_value)
        )
        if "go" not in step_table:
            raise ConfigError(f"missing 'go' in {path}.{step_key}")
        go_transitions = _parse_go_transitions(
            step_table["go"], path=f"{path}.{step_key}"
        )
        if not go_transitions:
            raise ConfigError(f"{path}.{step_key}.go must not be empty")
        steps[step_key] = WorkflowStepConfig(
            role=role_value,
            prompts=prompts,
            go=go_transitions,
        )
    return WorkflowConfig(
        declared_steps=steps,
        steps=dict(steps),
        first_step=first_step,
    )


def _parse_workflow_definition(
    raw: Mapping[str, object], *, path: str
) -> WorkflowConfig:
    allowed = {
        "steps",
        "exclude",
        "retry_inconsistent_checkpoint_state",
        "extends",
        "team",
        "setup",
        "teardown",
        "main_branch",
        "merge_prompt",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unsupported keys in {path}: {', '.join(unknown)}")
    wf_team = _optional_text(raw.get("team"), path=f"{path}.team")
    wf_extends = _optional_text(raw.get("extends"), path=f"{path}.extends")
    wf_retry: int | None = None
    if "retry_inconsistent_checkpoint_state" in raw:
        if wf_extends is not None:
            raise ConfigError(
                f"{path}.retry_inconsistent_checkpoint_state is not allowed on alias workflows"
            )
        retry_value = raw["retry_inconsistent_checkpoint_state"]
        if not isinstance(retry_value, int) or isinstance(retry_value, bool) or retry_value < 0:
            raise ConfigError(
                f"{path}.retry_inconsistent_checkpoint_state must be a non-negative integer"
            )
        wf_retry = retry_value
    wf_setup: tuple[str, ...] | None = None
    if "setup" in raw:
        wf_setup = _parse_lifecycle_array(raw["setup"], path=f"{path}.setup")
    wf_teardown: tuple[str, ...] | None = None
    if "teardown" in raw:
        wf_teardown = _parse_lifecycle_array(raw["teardown"], path=f"{path}.teardown")
    wf_main_branch: str | None = _optional_text(
        raw.get("main_branch"), path=f"{path}.main_branch"
    )
    wf_merge_prompt: tuple[str, ...] | None = None
    if "merge_prompt" in raw:
        wf_merge_prompt = _parse_lifecycle_array(
            raw["merge_prompt"], path=f"{path}.merge_prompt"
        )
    steps: dict[str, WorkflowStepConfig] = {}
    first_step: str | None = None
    if wf_extends is None:
        if "steps" not in raw:
            raise ConfigError(f"missing 'steps' in {path}")
        steps_table = _require_table(raw["steps"], path=f"{path}.steps")
        wf_config = _parse_workflow_steps(steps_table, path=f"{path}.steps")
        _validate_workflow_transitions(wf_config.declared_steps, path=path)
        steps = wf_config.declared_steps
        first_step = wf_config.first_step
    else:
        if "steps" in raw:
            raise ConfigError(f"{path} may not redefine 'steps' when using 'extends'")
    excluded_steps: tuple[str, ...] = ()
    if "exclude" in raw:
        excluded_steps = _parse_excluded_steps(raw["exclude"], path=f"{path}.exclude")
    return WorkflowConfig(
        declared_steps=steps,
        steps=dict(steps),
        first_step=first_step,
        excluded_steps=excluded_steps,
        retry_inconsistent_checkpoint_state=wf_retry,
        team=wf_team,
        extends=wf_extends,
        setup=wf_setup,
        teardown=wf_teardown,
        main_branch=wf_main_branch,
        merge_prompt=wf_merge_prompt,
    )


def _parse_workflow_lifecycle_defaults(
    raw: Mapping[str, object], *, path: str
) -> WorkflowLifecycleDefaults:
    setup: tuple[str, ...] = ()
    teardown: tuple[str, ...] = ()
    main_branch: str | None = None
    merge_prompt: tuple[str, ...] = ()
    if "setup" in raw:
        setup = _parse_lifecycle_array(raw["setup"], path=f"{path}.setup")
    if "teardown" in raw:
        teardown = _parse_lifecycle_array(raw["teardown"], path=f"{path}.teardown")
    if "main_branch" in raw:
        main_branch = _require_text(raw["main_branch"], path=f"{path}.main_branch")
    if "merge_prompt" in raw:
        merge_prompt = _parse_lifecycle_array(
            raw["merge_prompt"], path=f"{path}.merge_prompt"
        )
    return WorkflowLifecycleDefaults(
        setup=setup,
        teardown=teardown,
        main_branch=main_branch,
        merge_prompt=merge_prompt,
    )


def _parse_workflow_tables(
    raw: Mapping[str, object], *, path: str
) -> tuple[WorkflowLifecycleDefaults, dict[str, WorkflowConfig]]:
    defaults = _parse_workflow_lifecycle_defaults(raw, path=path)
    workflows: dict[str, WorkflowConfig] = {}
    for wf_name, wf_value in raw.items():
        if wf_name in WORKFLOW_LIFECYCLE_KEYS:
            continue
        wf_key = _require_text(wf_name, path=f"{path} key")
        wf_table = _require_table(wf_value, path=f"{path}.{wf_key}")
        workflows[wf_key] = _parse_workflow_definition(
            wf_table, path=f"{path}.{wf_key}"
        )
    return defaults, workflows


def _validate_workflow_transitions(
    steps: Mapping[str, WorkflowStepConfig],
    *,
    path: str,
) -> None:
    known_steps = set(steps)
    for step_name, step_config in steps.items():
        for index, transition in enumerate(step_config.go):
            if transition.to != "END" and transition.to not in known_steps:
                raise ConfigError(
                    f"transition target '{transition.to}' in "
                    f"{path}.steps.{step_name}.go[{index}] does not reference a known step"
                )


def _merge_excluded_steps(
    base_excluded: tuple[str, ...], local_excluded: tuple[str, ...]
) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*base_excluded, *local_excluded)))


def _apply_excluded_steps(
    declared_steps: dict[str, WorkflowStepConfig],
    excluded_steps: tuple[str, ...],
    *,
    path: str,
) -> tuple[dict[str, WorkflowStepConfig], str]:
    known_steps = set(declared_steps)
    for index, step_name in enumerate(excluded_steps):
        if step_name not in known_steps:
            raise ConfigError(
                f"{path}.exclude[{index}] references unknown step '{step_name}'"
            )
    excluded_set = set(excluded_steps)
    executable_steps = {
        step_name: step_config
        for step_name, step_config in declared_steps.items()
        if step_name not in excluded_set
    }
    if not executable_steps:
        raise ConfigError(
            f"{path}.first_step cannot be resolved after applying exclusions"
        )
    first_step = next(iter(executable_steps))
    return executable_steps, first_step


def _materialize_workflows(
    raw_workflows: dict[str, WorkflowConfig],
    *,
    lifecycle_defaults: WorkflowLifecycleDefaults,
    path: str,
) -> dict[str, WorkflowConfig]:
    resolved: dict[str, WorkflowConfig] = {}
    resolving: set[str] = set()

    def resolve(name: str) -> WorkflowConfig:
        if name in resolved:
            return resolved[name]
        if name in resolving:
            raise ConfigError(f"workflow alias cycle detected at {path}.{name}")
        if name not in raw_workflows:
            raise ConfigError(f"workflow alias references unknown workflow '{name}'")
        resolving.add(name)
        raw_wf = raw_workflows[name]
        if raw_wf.extends is None:
            declared_steps = dict(raw_wf.declared_steps or raw_wf.steps)
            excluded_steps = raw_wf.excluded_steps
            steps, first_step = _apply_excluded_steps(
                declared_steps,
                excluded_steps,
                path=f"{path}.{name}",
            )
            concrete = WorkflowConfig(
                declared_steps=declared_steps,
                steps=steps,
                first_step=first_step,
                excluded_steps=excluded_steps,
                retry_inconsistent_checkpoint_state=raw_wf.retry_inconsistent_checkpoint_state,
                team=raw_wf.team,
                extends=None,
                setup=raw_wf.setup if raw_wf.setup is not None else lifecycle_defaults.setup,
                teardown=raw_wf.teardown if raw_wf.teardown is not None else lifecycle_defaults.teardown,
                main_branch=raw_wf.main_branch if raw_wf.main_branch is not None else lifecycle_defaults.main_branch,
                merge_prompt=raw_wf.merge_prompt if raw_wf.merge_prompt is not None else lifecycle_defaults.merge_prompt,
            )
            _validate_workflow_transitions(concrete.steps, path=f"{path}.{name}")
        else:
            base_name = raw_wf.extends
            if base_name not in raw_workflows:
                raise ConfigError(
                    f"workflow '{name}' extends unknown workflow '{base_name}'"
                )
            base_raw = raw_workflows[base_name]
            if base_raw.extends is not None:
                raise ConfigError(
                    f"workflow '{name}' cannot extend alias workflow '{base_name}'"
                )
            base = resolve(base_name)
            declared_steps = dict(base.declared_steps)
            excluded_steps = _merge_excluded_steps(base.excluded_steps, raw_wf.excluded_steps)
            steps, first_step = _apply_excluded_steps(
                declared_steps,
                excluded_steps,
                path=f"{path}.{name}",
            )
            concrete = WorkflowConfig(
                declared_steps=declared_steps,
                steps=steps,
                first_step=first_step,
                excluded_steps=excluded_steps,
                retry_inconsistent_checkpoint_state=base.retry_inconsistent_checkpoint_state,
                team=raw_wf.team if raw_wf.team is not None else base.team,
                extends=None,
                setup=raw_wf.setup if raw_wf.setup is not None else base.setup,
                teardown=raw_wf.teardown if raw_wf.teardown is not None else base.teardown,
                main_branch=raw_wf.main_branch if raw_wf.main_branch is not None else base.main_branch,
                merge_prompt=raw_wf.merge_prompt if raw_wf.merge_prompt is not None else base.merge_prompt,
            )
            _validate_workflow_transitions(concrete.steps, path=f"{path}.{name}")
        resolving.remove(name)
        resolved[name] = concrete
        return concrete

    for workflow_name in raw_workflows:
        resolve(workflow_name)
    return resolved


def _parse_workflow_user_config(
    raw: Mapping[str, object], *, path: Path, allowed_top_level_keys: set[str]
) -> WorkflowUserConfig:
    unknown = sorted(set(raw) - allowed_top_level_keys)
    if unknown:
        raise ConfigError(
            f"unsupported top-level keys in {path}: {', '.join(unknown)}"
        )
    aflow = AflowSection()
    if "aflow" in raw:
        aflow_table = _require_table(raw["aflow"], path=f"{path}.aflow")
        aflow = _parse_aflow_section(aflow_table, path=f"{path}.aflow")
    harnesses: dict[str, WorkflowHarnessConfig] = {}
    if "harness" in raw:
        harnesses_table = _require_table(raw["harness"], path=f"{path}.harness")
        for harness_name, harness_value in harnesses_table.items():
            harness_key = _require_text(harness_name, path=f"{path}.harness key")
            if harness_key not in ADAPTERS:
                supported = ", ".join(sorted(ADAPTERS))
                raise ConfigError(
                    f"unsupported harness '{harness_key}' in {path}, "
                    f"supported harnesses are: {supported}"
                )
            harness_table = _require_table(
                harness_value, path=f"{path}.harness.{harness_key}"
            )
            harnesses[harness_key] = _parse_workflow_harness(
                harness_table, path=f"{path}.harness.{harness_key}"
            )
    roles: dict[str, str] = {}
    role_prompts: dict[str, str] = {}
    if "roles" in raw:
        roles_table = _require_table(raw["roles"], path=f"{path}.roles")
        roles = _parse_role_map(
            {key: value for key, value in roles_table.items() if key != "prompts"},
            path=f"{path}.roles",
        )
        prompts_value = roles_table.get("prompts")
        if prompts_value is not None:
            prompts_table = _require_table(
                prompts_value, path=f"{path}.roles.prompts"
            )
            role_prompts = _parse_role_prompt_map(
                prompts_table, path=f"{path}.roles.prompts"
            )
    error_handling = ErrorHandlingConfig()
    if "error_handling" in raw:
        error_handling_table = _require_table(
            raw["error_handling"], path=f"{path}.error_handling"
        )
        error_handling = _parse_error_handling_config(
            error_handling_table, path=f"{path}.error_handling"
        )
    manager = ManagerConfig()
    if "manager" in raw:
        manager_table = _require_table(raw["manager"], path=f"{path}.manager")
        manager = _parse_manager_config(manager_table, path=f"{path}.manager")
    teams: dict[str, TeamConfig] = {}
    if "teams" in raw:
        teams_table = _require_table(raw["teams"], path=f"{path}.teams")
        for team_name, team_value in teams_table.items():
            team_key = _require_text(team_name, path=f"{path}.teams key")
            team_table = _require_table(team_value, path=f"{path}.teams.{team_key}")
            teams[team_key] = _parse_team_config(
                team_table, path=f"{path}.teams.{team_key}"
            )
    workflows: dict[str, WorkflowConfig] = {}
    if "workflow" in raw:
        workflows_table = _require_table(raw["workflow"], path=f"{path}.workflow")
        lifecycle_defaults, raw_workflows = _parse_workflow_tables(
            workflows_table, path=f"{path}.workflow"
        )
        workflows = _materialize_workflows(
            raw_workflows,
            lifecycle_defaults=lifecycle_defaults,
            path=f"{path}.workflow",
        )
    prompts: dict[str, str] = {}
    if "prompts" in raw:
        prompts_table = _require_table(raw["prompts"], path=f"{path}.prompts")
        for prompt_name, prompt_value in prompts_table.items():
            prompt_key = _require_text(
                prompt_name, path=f"{path}.prompts key"
            )
            prompts[prompt_key] = _require_text(
                prompt_value, path=f"{path}.prompts.{prompt_key}"
            )
    return WorkflowUserConfig(
        aflow=aflow,
        harnesses=harnesses,
        roles=roles,
        role_prompts=role_prompts,
        teams=teams,
        manager=manager,
        error_handling=error_handling,
        workflows=workflows,
        prompts=prompts,
    )


def load_workflow_config(
    config_path: Path | None = None,
) -> WorkflowUserConfig:
    path = config_path or _config_path()
    if not path.exists():
        return WorkflowUserConfig()
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"unable to read config file {path}: {exc}") from exc
    sibling_path = path.with_name("workflows.toml")
    aflow_allowed_top_level_keys = {"aflow", "harness", "roles", "teams", "prompts", "error_handling", "manager"}
    if sibling_path.exists():
        config = _parse_workflow_user_config(
            raw, path=path, allowed_top_level_keys=aflow_allowed_top_level_keys
        )
        try:
            with sibling_path.open("rb") as handle:
                sibling_raw = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(
                f"invalid TOML in {sibling_path}: {exc}"
            ) from exc
        except OSError as exc:
            raise ConfigError(
                f"unable to read config file {sibling_path}: {exc}"
            ) from exc
        sibling_config = _parse_workflow_user_config(
            sibling_raw, path=sibling_path, allowed_top_level_keys={"workflow"}
        )
        merged = WorkflowUserConfig(
            aflow=config.aflow,
            harnesses=config.harnesses,
            roles=config.roles,
            role_prompts=config.role_prompts,
            teams=config.teams,
            manager=config.manager,
            error_handling=config.error_handling,
            workflows={**config.workflows, **sibling_config.workflows},
            prompts={**config.prompts, **sibling_config.prompts},
        )
    else:
        merged = _parse_workflow_user_config(
            raw, path=path, allowed_top_level_keys=aflow_allowed_top_level_keys
        )
    errors = validate_workflow_config(merged)
    if errors:
        raise ConfigError("; ".join(errors))
    return merged


def load_config(config_path: Path | str | None = None) -> WorkflowUserConfig:
    """Load the AFlow configuration through the public concise alias."""
    return load_workflow_config(Path(config_path) if isinstance(config_path, str) else config_path)


def _bootstrap_config_files(config_path: Path | None = None) -> tuple[Path, tuple[Path, ...]]:
    path = config_path or _config_path()
    workflows_path = path.with_name("workflows.toml")
    created_paths: list[Path] = []

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        config_text = resources.files("aflow").joinpath("aflow.toml").read_text(
            encoding="utf-8"
        )
        path.write_text(config_text, encoding="utf-8")
        created_paths.append(path)

    if not workflows_path.exists():
        workflows_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_text = resources.files("aflow").joinpath("workflows.toml").read_text(
            encoding="utf-8"
        )
        workflows_path.write_text(workflow_text, encoding="utf-8")
        created_paths.append(workflows_path)

    return path, tuple(created_paths)


def bootstrap_config(config_path: Path | None = None) -> Path:
    path, _ = _bootstrap_config_files(config_path)
    return path


def find_placeholders(config: WorkflowUserConfig) -> list[str]:
    placeholders: list[str] = []
    for harness_name, harness_config in config.harnesses.items():
        for profile_name, profile_config in harness_config.profiles.items():
            if profile_config.model == "FILL_IN_MODEL":
                placeholders.append(
                    f"harness.{harness_name}.profiles.{profile_name}.model"
                )
    return sorted(placeholders)


def validate_workflow_config(
    config: WorkflowUserConfig,
) -> list[str]:
    errors: list[str] = []
    if config.aflow.default_workflow is not None:
        if config.aflow.default_workflow not in config.workflows:
            errors.append(
                f"aflow.default_workflow references unknown workflow "
                f"'{config.aflow.default_workflow}'"
            )
    for role_name, selector in config.roles.items():
        if "." not in selector:
            errors.append(
                f"roles.{role_name} must be a fully qualified harness.profile selector"
            )
            continue
        harness_name, _, profile_name = selector.partition(".")
        if harness_name not in config.harnesses:
            errors.append(
                f"roles.{role_name} references unknown harness '{harness_name}'"
            )
        elif profile_name not in config.harnesses[harness_name].profiles:
            errors.append(
                f"roles.{role_name} references unknown profile '{profile_name}' "
                f"for harness '{harness_name}'"
            )
    for role_name in config.role_prompts:
        if role_name not in config.roles:
            errors.append(
                f"roles.prompts.{role_name} references unknown role '{role_name}'"
            )
    for team_name, team_config in config.teams.items():
        for role_key, selector in team_config.roles.items():
            if role_key not in config.roles:
                errors.append(
                    f"teams.{team_name}.{role_key} references unknown role '{role_key}'"
                )
            if "." not in selector:
                errors.append(
                    f"teams.{team_name}.{role_key} must be a fully qualified harness.profile selector"
                )
                continue
            harness_name, _, profile_name = selector.partition(".")
            if harness_name not in config.harnesses:
                errors.append(
                    f"teams.{team_name}.{role_key} references unknown harness '{harness_name}'"
                )
            elif profile_name not in config.harnesses[harness_name].profiles:
                errors.append(
                    f"teams.{team_name}.{role_key} references unknown profile '{profile_name}' "
                    f"for harness '{harness_name}'"
                )
        for role_key in team_config.role_prompts:
            if role_key not in config.roles:
                errors.append(
                    f"teams.{team_name}.prompts.{role_key} references unknown role "
                    f"'{role_key}'"
                )
    def validate_team_graph(attribute: str) -> None:
        for team_name, team_config in config.teams.items():
            target = getattr(team_config, attribute)
            if target is not None and target not in config.teams:
                errors.append(
                    f"teams.{team_name}.{attribute} references unknown team '{target}'"
                )
        visited: set[str] = set()
        visiting: set[str] = set()

        def walk_team_chain(team_name: str, chain: list[str]) -> None:
            if team_name in visiting:
                cycle_start = chain.index(team_name)
                cycle = chain[cycle_start:] + [team_name]
                errors.append(
                    f"teams.{team_name}.{attribute} forms a cycle: {' -> '.join(cycle)}"
                )
                return
            if team_name in visited:
                return
            visiting.add(team_name)
            chain.append(team_name)
            target = getattr(config.teams[team_name], attribute)
            if target is not None and target in config.teams:
                walk_team_chain(target, chain)
            chain.pop()
            visiting.remove(team_name)
            visited.add(team_name)

        for team_name in config.teams:
            walk_team_chain(team_name, [])

    validate_team_graph("backup_team")
    validate_team_graph("upgrade_to")

    if config.manager.enabled:
        assert config.manager.lite_role is not None
        assert config.manager.full_role is not None
        manager_roles = (config.manager.lite_role, config.manager.full_role)
        for role in manager_roles:
            if role not in config.roles and not any(role in team.roles for team in config.teams.values()):
                errors.append(
                    f"manager role '{role}' cannot be resolved through global or team roles"
                )

    for wf_name, wf_config in config.workflows.items():
        if wf_config.extends is not None:
            errors.append(
                f"workflow.{wf_name}.extends should not be present after materialization"
            )
        known_roles = set(config.roles)
        if config.manager.enabled:
            assert config.manager.lite_role is not None
            assert config.manager.full_role is not None
            team_roles = config.teams.get(wf_config.team).roles if wf_config.team in config.teams else {}
            for manager_role in (config.manager.lite_role, config.manager.full_role):
                if manager_role not in team_roles and manager_role not in config.roles:
                    errors.append(
                        f"workflow.{wf_name} manager role '{manager_role}' cannot be resolved through "
                        f"team '{wf_config.team}' or global roles"
                    )
        for step_name, step_config in wf_config.steps.items():
            role_name = step_config.role
            if role_name not in known_roles:
                errors.append(
                    f"workflow.{wf_name}.steps.{step_name}.role references unknown role "
                    f"'{role_name}'"
                )
                continue
        for step_name, step_config in wf_config.steps.items():
            for prompt_index, prompt_key in enumerate(step_config.prompts):
                if prompt_key not in config.prompts:
                    errors.append(
                        f"workflow.{wf_name}.steps.{step_name}.prompts[{prompt_index}] "
                        f"references unknown prompt '{prompt_key}'"
                    )
        wf_setup = wf_config.setup or ()
        wf_teardown = wf_config.teardown or ()
        if (wf_setup, wf_teardown) not in VALID_LIFECYCLE_COMBOS:
            errors.append(
                f"workflow.{wf_name} has unsupported lifecycle combination "
                f"setup={list(wf_setup)} teardown={list(wf_teardown)}"
            )
        for mp_index, mp_key in enumerate(wf_config.merge_prompt or ()):
            if mp_key not in config.prompts:
                errors.append(
                    f"workflow.{wf_name}.merge_prompt[{mp_index}] "
                    f"references unknown prompt '{mp_key}'"
                )
        if "merge" in wf_teardown:
            team_lead_role = config.aflow.team_lead
            if not team_lead_role:
                errors.append(
                    f"workflow.{wf_name} uses merge teardown but [aflow].team_lead is not set"
                )
            else:
                effective_team = wf_config.team
                if effective_team is not None and effective_team in config.teams:
                    team_roles = config.teams[effective_team].roles
                    if team_lead_role not in team_roles and team_lead_role not in config.roles:
                        errors.append(
                            f"workflow.{wf_name} uses merge but team_lead role "
                            f"'{team_lead_role}' cannot be resolved through team "
                            f"'{effective_team}' or global roles"
                        )
                elif team_lead_role not in config.roles:
                    errors.append(
                        f"workflow.{wf_name} uses merge but team_lead role "
                        f"'{team_lead_role}' cannot be resolved through global roles"
                    )
    return errors
