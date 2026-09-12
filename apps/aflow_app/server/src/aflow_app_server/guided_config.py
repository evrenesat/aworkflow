"""Pure guided-settings projection and transform for the config TOML pair.

The form endpoint transforms only the submitted candidate pair.  It never
saves, initializes, or reloads a registered project: the existing atomic,
revision-checked ``PUT /config`` remains the only save boundary.  TOML edits
are performed with ``tomlkit`` on the action-owned table/key only, so
comments, ordering, and unknown fields survive every guided edit.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
import re
import tempfile
import tomllib
from typing import Any

import tomlkit
from tomlkit.container import Container, OutOfOrderTableProxy
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument


def _is_table(value: object) -> bool:
    """Accept regular and out-of-order tables when reading a document."""
    return isinstance(value, (Table, OutOfOrderTableProxy))

from aflow.config import (
    ConfigError,
    TEAM_DISPLAY_NAME_MAX_LENGTH,
    load_workflow_config,
    render_starter_documents,
    resolve_team_config,
    validate_starter_name,
)
from aflow.harnesses import ADAPTERS

from .models import (
    AddTeamAction,
    BuildStarterAction,
    GuidedConfigAction,
    SetDefaultManagerEnabledAction,
    SetDefaultWorkflowAction,
    SetGlobalRoleAction,
    SetMaxTurnsAction,
    RemoveTeamAction,
    SetTeamBaseAction,
    SetTeamDisplayNameAction,
    SetTeamRoleAction,
    SetTeamUpgradeAction,
    SetWorkflowDefaultTeamAction,
    SetWorkflowManagerEnabledAction,
    UpsertProfileAction,
)
from .project_config_service import (
    ConfigValidationIssue,
    check_document_text,
    validate_candidate_pair,
)

ZCODE_MODEL_NOTE = (
    "ZCode model and reasoning effort are configured in ZCode's project "
    "configuration; AFlow model/effort overrides are not supported by the "
    "ZCode CLI"
)
_SUGGESTION_NOTE = (
    "Bundled harness profiles and adapter names are labeled suggestions from "
    "the shipped AFlow configuration; they do not verify installed "
    "credentials, provider entitlement, or live availability"
)
STARTER_PLACEHOLDER_MODEL = "FILL_IN_MODEL"
_STARTER_PROFILE_PATH = ("harness", "starter", "profiles", "default")
_TEAM_RESERVED_KEYS = frozenset(
    {"roles", "prompts", "backup_team", "upgrade_to", "extends", "display_name"}
)

# Keep this catalog in the guided projection rather than configuration data:
# it is editor-only help and never participates in an action or TOML write.
# The server contract test compares these tokens with both replacement layers
# in ``aflow.workflow`` so new production substitutions cannot silently omit
# editor documentation.
PROMPT_TEMPLATE_VARIABLE_CATALOG: tuple[dict[str, object], ...] = (
    {
        "token": "{ORIGINAL_PLAN_PATH}",
        "description": "The original input plan path for this run.",
        "scope": "Named workflow step prompts and named merge prompts.",
        "absent_value": "Always receives the controller's original plan path when rendered.",
        "example": "plans/request.md",
        "applicable_prompt_types": ("workflow step prompt", "merge prompt"),
    },
    {
        "token": "{ACTIVE_PLAN_PATH}",
        "description": "The active plan path for the current workflow step.",
        "scope": "Named workflow step prompts and named merge prompts.",
        "absent_value": "Always receives the controller's active plan path when rendered.",
        "example": "plans/request-cp02-v01.md",
        "applicable_prompt_types": ("workflow step prompt", "merge prompt"),
    },
    {
        "token": "{NEW_PLAN_PATH}",
        "description": "The controller-provided destination for a possible follow-up plan.",
        "scope": "Named workflow step prompts and named merge prompts.",
        "absent_value": "Always receives the generated follow-up-plan destination; it is not a resolved run preview.",
        "example": "plans/request-cp02-v01.md",
        "applicable_prompt_types": ("workflow step prompt", "merge prompt"),
    },
    {
        "token": "{NEXT_CP}",
        "description": "The current unchecked checkpoint index in the active plan.",
        "scope": "Named workflow step prompts and named merge prompts.",
        "absent_value": "Renders as '-' when no unchecked checkpoint index is available.",
        "example": "2",
        "applicable_prompt_types": ("workflow step prompt", "merge prompt"),
    },
    {
        "token": "{WORK_ON_NEXT_CHECKPOINT_CMD}",
        "description": "An instruction to work only on the current checkpoint, not a shell command.",
        "scope": "Named workflow step prompts and named merge prompts.",
        "absent_value": "Renders as an empty string when no unchecked checkpoint index is available.",
        "example": "Work only on Checkpoint #2. Do not repeat earlier checkpoints, and do not skip ahead.",
        "applicable_prompt_types": ("workflow step prompt", "merge prompt"),
    },
    {
        "token": "{MAIN_BRANCH}",
        "description": "The configured branch that receives a lifecycle merge.",
        "scope": "Named merge prompts only; not substituted in ordinary step prompts.",
        "absent_value": "Not substituted outside a merge prompt.",
        "example": "main",
        "applicable_prompt_types": ("merge prompt",),
    },
    {
        "token": "{FEATURE_BRANCH}",
        "description": "The feature branch created for this lifecycle run.",
        "scope": "Named merge prompts only; not substituted in ordinary step prompts.",
        "absent_value": "Not substituted outside a merge prompt.",
        "example": "aflow/request-cp02",
        "applicable_prompt_types": ("merge prompt",),
    },
    {
        "token": "{PRIMARY_REPO_ROOT}",
        "description": "The primary checkout root used to coordinate the lifecycle merge.",
        "scope": "Named merge prompts only; not substituted in ordinary step prompts.",
        "absent_value": "Not substituted outside a merge prompt.",
        "example": "/workspace/project",
        "applicable_prompt_types": ("merge prompt",),
    },
    {
        "token": "{EXECUTION_REPO_ROOT}",
        "description": "The repository root where the workflow executes.",
        "scope": "Named merge prompts only; not substituted in ordinary step prompts.",
        "absent_value": "Not substituted outside a merge prompt.",
        "example": "/workspace/project",
        "applicable_prompt_types": ("merge prompt",),
    },
    {
        "token": "{FEATURE_WORKTREE_PATH}",
        "description": "The linked feature worktree path for this lifecycle run.",
        "scope": "Named merge prompts only; not substituted in ordinary step prompts.",
        "absent_value": "Renders as an empty string when the lifecycle has no feature worktree.",
        "example": "/workspace/project/.worktrees/request-cp02",
        "applicable_prompt_types": ("merge prompt",),
    },
)


class GuidedConfigError(RuntimeError):
    """A bounded, actionable guided-config rejection with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _table(container: Container | TOMLDocument, key: str, *, path: str) -> Table:
    value = container.get(key)
    if value is not None and not _is_table(value):
        raise GuidedConfigError("invalid_field_type", f"expected {path} to be a table")
    if value is None:
        value = tomlkit.table()
        container[key] = value
    return value


def _parse_document(name: str, text: str) -> TOMLDocument:
    try:
        return tomlkit.parse(text)
    except tomlkit.exceptions.ParseError as exc:
        raise GuidedConfigError(
            "syntax_error", f"{name} is not valid TOML: {exc}"
        ) from exc


def _syntax_issue(name: str, text: str) -> ConfigValidationIssue | None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        message = str(exc)
        match = re.search(r"at line (\d+)", message)
        line = int(match.group(1)) if match else None
        return ConfigValidationIssue(
            document=name, line=line, message=" ".join(message.split())[:300]
        )
    return None


def _selector_parts(selector: str) -> tuple[str, str]:
    # Production selectors split at the first dot, so profile names may
    # themselves contain dots (for example the bundled "glm-5.3").
    harness, dot, profile = selector.partition(".")
    if not dot or not harness or not profile:
        raise GuidedConfigError(
            "invalid_selector",
            "selector must be a fully qualified harness.profile value",
        )
    return harness, profile


def _configured_profiles(aflow_doc: TOMLDocument) -> dict[str, set[str]]:
    harness_table = aflow_doc.get("harness")
    profiles: dict[str, set[str]] = {}
    if not _is_table(harness_table):
        return profiles
    for harness_name, harness_value in harness_table.items():
        if not _is_table(harness_value):
            continue
        inner = harness_value.get("profiles")
        if not _is_table(inner):
            continue
        names = {str(key) for key, value in inner.items() if _is_table(value)}
        if names:
            profiles[str(harness_name)] = names
    return profiles


def _require_profile_reference(
    selector: str, profiles: dict[str, set[str]]
) -> tuple[str, str]:
    harness, profile = _selector_parts(selector)
    if profile not in profiles.get(harness, set()):
        raise GuidedConfigError(
            "unknown_profile",
            f"selector '{selector}' does not name a configured profile",
        )
    return harness, profile


def _role_selectors(aflow_doc: TOMLDocument) -> list[str]:
    selectors: list[str] = []
    roles_table = aflow_doc.get("roles")
    if _is_table(roles_table):
        for key, value in roles_table.items():
            if key != "prompts" and isinstance(value, str):
                selectors.append(value)
    teams_table = aflow_doc.get("teams")
    if _is_table(teams_table):
        for team_value in teams_table.values():
            if not _is_table(team_value):
                continue
            team_roles = team_value.get("roles")
            if _is_table(team_roles):
                selectors.extend(
                    value for value in team_roles.values() if isinstance(value, str)
                )
            selectors.extend(
                team_value[key]
                for key in _inline_team_role_keys(team_value)
                if isinstance(team_value[key], str)
            )
    return selectors


def _inline_team_role_keys(team: object) -> tuple[str, ...]:
    """Return legacy role keys stored directly in one team table."""
    if not _is_table(team):
        return ()
    return tuple(
        str(key) for key in team if str(key) not in _TEAM_RESERVED_KEYS
    )


def _move_inline_team_roles_to_table(team: Table) -> Table:
    """Normalize an explicitly edited mixed team into the supported role table."""
    roles = team.get("roles")
    if roles is None:
        roles = tomlkit.table()
        team["roles"] = roles
    elif not _is_table(roles):
        raise GuidedConfigError("invalid_field_type", "teams.roles must be a table")
    for key in _inline_team_role_keys(team):
        roles[key] = team[key]
        del team[key]
    return roles


def _cleanup_starter_profile(aflow_doc: TOMLDocument) -> None:
    """Sole automatic deletion: an unreferenced exact placeholder profile.

    Removes ``harness.starter.profiles.default`` only when no selector still
    references ``starter.default`` and the profile still exactly matches the
    generated ``model = "FILL_IN_MODEL"`` placeholder with no additional keys.
    Empty parents are removed only when they contain no other data.
    """
    if any(selector == "starter.default" for selector in _role_selectors(aflow_doc)):
        return
    harness_table = aflow_doc.get("harness")
    if not _is_table(harness_table):
        return
    starter = harness_table.get("starter")
    if not isinstance(starter, Table):
        return
    profiles = starter.get("profiles")
    if not isinstance(profiles, Table):
        return
    default = profiles.get("default")
    if not isinstance(default, Table):
        return
    keys = {str(key) for key in default.keys()}
    if keys != {"model"} or default.get("model") != STARTER_PLACEHOLDER_MODEL:
        return
    del profiles["default"]
    if len(profiles.value) == 0:
        del starter["profiles"]
    if len(starter.value) == 0:
        del harness_table["starter"]
    if len(harness_table.value) == 0:
        del aflow_doc["harness"]


def _workflow_names(workflows_doc: TOMLDocument) -> list[str]:
    workflow_table = workflows_doc.get("workflow")
    if not _is_table(workflow_table):
        return []
    return [
        str(key)
        for key, value in workflow_table.items()
        if str(key) not in {"setup", "teardown", "main_branch", "merge_prompt"}
        and _is_table(value)
    ]


def _require_workflow(workflows_doc: TOMLDocument, name: str) -> None:
    validate_starter_name(name)
    if name not in _workflow_names(workflows_doc):
        raise GuidedConfigError(
            "unknown_workflow",
            f"workflow '{name}' is not defined in workflows.toml",
        )


def _require_team(aflow_doc: TOMLDocument, name: str) -> Table:
    validate_starter_name(name)
    teams_table = aflow_doc.get("teams")
    if not _is_table(teams_table) or name not in teams_table:
        raise GuidedConfigError(
            "unknown_team", f"team '{name}' is not defined in aflow.toml"
        )
    team = teams_table.get(name)
    if not isinstance(team, Table):
        raise GuidedConfigError("invalid_field_type", f"teams.{name} must be a table")
    return team


def _team_reference_paths(
    aflow_doc: TOMLDocument,
    workflows_doc: TOMLDocument,
    team_name: str,
) -> tuple[str, ...]:
    """Return configured fields that still point at ``team_name``."""
    references: list[str] = []
    teams_table = aflow_doc.get("teams")
    if _is_table(teams_table):
        for source_name, source_value in teams_table.items():
            if not _is_table(source_value):
                continue
            for field in ("extends", "backup_team", "upgrade_to"):
                if source_value.get(field) == team_name:
                    references.append(f"teams.{source_name}.{field}")
    workflow_table = workflows_doc.get("workflow")
    if _is_table(workflow_table):
        for workflow_name in _workflow_names(workflows_doc):
            workflow_value = workflow_table.get(workflow_name)
            if _is_table(workflow_value) and workflow_value.get("team") == team_name:
                references.append(f"workflow.{workflow_name}.team")
    return tuple(references)


def _text_table(value: object, *, path: str) -> dict[str, str]:
    """Return a plain text mapping; a malformed prompt table is a bounded error.

    The production loader rejects these shapes at save time; the guided
    projection must surface the same diagnosis instead of raising ValueError
    (which would become HTTP 500 and hide the repair path).
    """
    if value is None:
        return {}
    if not _is_table(value):
        raise GuidedConfigError(
            "invalid_field_type", f"{path} must be a table of text values"
        )
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(item, str):
            raise GuidedConfigError(
                "invalid_field_type", f"{path}.{key} must be text"
            )
        result[str(key)] = item
    return result


def prompt_reference_arrays(workflows_doc):
    """Yield ``(path, array)`` for schema-defined named-prompt arrays only.

    Malformed step or reference shapes are skipped here; the production
    validation report is the authority that flags them as invalid.
    """
    root = workflows_doc.get("workflow")
    if not _is_table(root):
        return
    for name, workflow in root.items():
        if not _is_table(workflow):
            continue
        steps = workflow.get("steps")
        if not _is_table(steps):
            continue
        for step, value in steps.items():
            if _is_table(value) and isinstance(value.get("prompts"), list):
                yield f"workflow.{name}.steps.{step}.prompts", value["prompts"]


def merge_prompt_sites(workflows_doc):
    """Yield ``(path, parent, key)`` for merge_prompt name references.

    ``merge_prompt`` accepts a single name or an array of names; malformed
    shapes are skipped and flagged by the production validation instead.
    """
    root = workflows_doc.get("workflow")
    if not _is_table(root):
        return
    candidates = [("workflow", root)]
    for name, workflow in root.items():
        if _is_table(workflow):
            candidates.append((f"workflow.{name}", workflow))
    for prefix, table in candidates:
        value = table.get("merge_prompt")
        if isinstance(value, str) or isinstance(value, list):
            yield f"{prefix}.merge_prompt", table, "merge_prompt"


def apply_action_batch(aflow_text, workflows_text, actions):
    """Transform in order; persistence validates the complete final candidate."""
    for action in actions:
        result = guided_form_response(aflow_text, workflows_text, action)
        if result["syntax_issues"]:
            raise GuidedConfigError("invalid_toml", "Correct Advanced TOML before applying guided changes")
        aflow_text, workflows_text = result["aflow_toml"], result["workflows_toml"]
    return aflow_text, workflows_text


def _apply_action(
    action: GuidedConfigAction,
    aflow_doc: TOMLDocument,
    workflows_doc: TOMLDocument,
) -> None:
    from .models import SetPromptAction, RenamePromptAction, SetRolePromptAction, MoveRolePromptAction

    if isinstance(action, MoveRolePromptAction):
        _require_role_name(aflow_doc, action.role, must_exist=True)
        _require_role_name(aflow_doc, action.target_role, must_exist=True)
        source = _require_team(aflow_doc, action.team) if action.team else _table(aflow_doc, "roles", path="roles")
        target = _require_team(aflow_doc, action.target_team) if action.target_team else _table(aflow_doc, "roles", path="roles")
        source_prompts = _table(source, "prompts", path="prompts")
        target_prompts = _table(target, "prompts", path="prompts")
        if action.role not in source_prompts or action.target_role in target_prompts:
            raise GuidedConfigError("prompt_collision", "Move requires an existing override and an unused target")
        target_prompts[action.target_role] = source_prompts.pop(action.role)
        return

    if isinstance(action, SetPromptAction):
        prompts = _table(aflow_doc, "prompts", path="prompts")
        if action.text is None:
            prompts.pop(action.name, None)
        else:
            prompts[action.name] = action.text
        return
    if isinstance(action, RenamePromptAction):
        prompts = _text_table(aflow_doc.get("prompts"), path="prompts")
        if action.name not in prompts or action.new_name in prompts:
            raise GuidedConfigError("prompt_collision", "The source prompt must exist and the target must be unused")
        prompts_table = _table(aflow_doc, "prompts", path="prompts")
        prompts_table[action.new_name] = prompts_table.pop(action.name)
        for _, values in prompt_reference_arrays(workflows_doc):
            for index, value in enumerate(values):
                if value == action.name:
                    values[index] = action.new_name
        for _, parent, key in merge_prompt_sites(workflows_doc):
            value = parent[key]
            if isinstance(value, str):
                if value == action.name:
                    parent[key] = action.new_name
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    if item == action.name:
                        value[index] = action.new_name
        return
    if isinstance(action, SetRolePromptAction):
        _require_role_name(aflow_doc, action.role, must_exist=True)
        owner = _require_team(aflow_doc, action.team) if action.team else _table(aflow_doc, "roles", path="roles")
        prompts = _table(owner, "prompts", path="prompts")
        if action.text is None:
            prompts.pop(action.role, None)
        else:
            prompts[action.role] = action.text
        return
    if isinstance(action, BuildStarterAction):
        raise GuidedConfigError(
            "invalid_action_state", "build_starter is handled before parsing"
        )
    if isinstance(action, SetDefaultWorkflowAction):
        _require_workflow(workflows_doc, action.value)
        aflow_section = _table(aflow_doc, "aflow", path="aflow")
        aflow_section["default_workflow"] = action.value
        return
    if isinstance(action, SetMaxTurnsAction):
        aflow_section = _table(aflow_doc, "aflow", path="aflow")
        if action.value is None:
            aflow_section.pop("max_turns", None)
        else:
            aflow_section["max_turns"] = action.value
        return
    if isinstance(action, UpsertProfileAction):
        _apply_upsert_profile(action, aflow_doc)
        return
    if isinstance(action, SetGlobalRoleAction):
        _require_role_name(aflow_doc, action.role, must_exist=False)
        profiles = _configured_profiles(aflow_doc)
        _require_profile_reference(action.selector, profiles)
        roles = _table(aflow_doc, "roles", path="roles")
        roles[action.role] = action.selector
        _cleanup_starter_profile(aflow_doc)
        return
    if isinstance(action, AddTeamAction):
        validate_starter_name(action.team)
        existing_teams = aflow_doc.get("teams")
        if _is_table(existing_teams) and action.team in existing_teams:
            raise GuidedConfigError(
                "team_exists", f"team '{action.team}' already exists"
            )
        teams = _table(aflow_doc, "teams", path="teams")
        team_table = tomlkit.table()
        team_table["roles"] = tomlkit.table()
        teams[action.team] = team_table
        return
    if isinstance(action, SetTeamRoleAction):
        _require_role_name(aflow_doc, action.role, must_exist=True)
        team = _require_team(aflow_doc, action.team)
        if action.selector is None:
            team_roles = team.get("roles")
            if _inline_team_role_keys(team) and team_roles is not None:
                team_roles = _move_inline_team_roles_to_table(team)
            if _is_table(team_roles) and action.role in team_roles:
                del team_roles[action.role]
            if action.role in _inline_team_role_keys(team):
                # Legacy teams store role selectors directly beside metadata.
                # Restore inheritance must remove that real declaration too.
                del team[action.role]
            return
        profiles = _configured_profiles(aflow_doc)
        _require_profile_reference(action.selector, profiles)
        inline_keys = _inline_team_role_keys(team)
        if inline_keys and team.get("roles") is None:
            # Preserve a clean legacy representation while it is being edited;
            # creating a nested table here would make the loader reject the
            # untouched inline selectors as a mixed declaration.
            team[action.role] = action.selector
        else:
            if inline_keys:
                team_roles = _move_inline_team_roles_to_table(team)
            else:
                team_roles = _table(team, "roles", path=f"teams.{action.team}.roles")
            team_roles[action.role] = action.selector
        _cleanup_starter_profile(aflow_doc)
        return
    if isinstance(action, SetTeamBaseAction):
        team = _require_team(aflow_doc, action.team)
        if action.extends is None:
            team.pop("extends", None)
            return
        if action.extends == action.team:
            raise GuidedConfigError(
                "invalid_team_base",
                f"teams.{action.team}.extends cannot reference itself",
            )
        _require_team(aflow_doc, action.extends)
        team["extends"] = action.extends
        return
    if isinstance(action, SetTeamDisplayNameAction):
        team = _require_team(aflow_doc, action.team)
        if action.display_name is None:
            team.pop("display_name", None)
            return
        display_name = action.display_name.strip()
        if not display_name:
            # Guided blank input means remove the local label, matching the
            # draft editor contract rather than persisting an invalid value.
            team.pop("display_name", None)
            return
        if len(display_name) > TEAM_DISPLAY_NAME_MAX_LENGTH:
            raise GuidedConfigError(
                "invalid_team_display_name",
                f"teams.{action.team}.display_name must be at most "
                f"{TEAM_DISPLAY_NAME_MAX_LENGTH} characters",
            )
        team["display_name"] = display_name
        return
    if isinstance(action, RemoveTeamAction):
        _require_team(aflow_doc, action.team)
        references = _team_reference_paths(aflow_doc, workflows_doc, action.team)
        if references:
            shown = ", ".join(references[:8])
            if len(references) > 8:
                shown += ", ..."
            raise GuidedConfigError(
                "team_in_use",
                f"team '{action.team}' is still referenced by {shown}"[:300],
            )
        teams_table = aflow_doc.get("teams")
        if not _is_table(teams_table):  # pragma: no cover - _require_team checked it
            raise GuidedConfigError(
                "invalid_field_type", "teams must be a table"
            )
        del teams_table[action.team]
        return
    if isinstance(action, SetTeamUpgradeAction):
        team = _require_team(aflow_doc, action.team)
        if action.upgrade_to is None:
            # Omission means unchanged; an explicit null removes the link.
            team.pop("upgrade_to", None)
            return
        if action.upgrade_to == action.team:
            raise GuidedConfigError(
                "invalid_action_value", "a team cannot upgrade to itself"
            )
        _require_team(aflow_doc, action.upgrade_to)
        team["upgrade_to"] = action.upgrade_to
        return
    if isinstance(action, SetDefaultManagerEnabledAction):
        _require_manager_enabled_value(action.value)
        if action.value is None:
            # Null deletes only the flag; never create a table to delete from,
            # so removing an absent default stays a net no-op.
            _delete_key(workflows_doc.get("workflow"), "manager_enabled")
            return
        defaults_table = _table(workflows_doc, "workflow", path="workflow")
        defaults_table["manager_enabled"] = action.value
        return
    if isinstance(action, SetWorkflowManagerEnabledAction):
        _require_manager_enabled_value(action.value)
        _require_workflow(workflows_doc, action.workflow)
        root = workflows_doc.get("workflow")
        if not _is_table(root):  # pragma: no cover - guaranteed by _require_workflow
            raise GuidedConfigError("unknown_workflow", f"workflow '{action.workflow}' is not defined in workflows.toml")
        wf_table = root.get(action.workflow)
        if not _is_table(wf_table):  # pragma: no cover - guaranteed by _require_workflow
            raise GuidedConfigError("unknown_workflow", f"workflow '{action.workflow}' is not defined in workflows.toml")
        if action.value is None:
            # Null deletes only that workflow's override so it inherits again;
            # inherited values are never written back into the workflow table.
            _delete_key(wf_table, "manager_enabled")
            return
        # Explicit false is a real declaration: never use truthiness here.
        wf_table["manager_enabled"] = action.value
        return
    raise GuidedConfigError(
        "unknown_action", f"unsupported guided action '{action.type}'"
    )


def _require_manager_enabled_value(value: object) -> None:
    """Reject non-boolean flag values that bypassed strict model validation."""
    if value is not None and not isinstance(value, bool):
        raise GuidedConfigError(
            "invalid_action_value", "manager_enabled value must be a boolean or null"
        )


def _delete_key(table: object, key: str) -> None:
    """Delete one key from a parsed table without touching anything else.

    Uses membership plus ``del`` so both regular and out-of-order tables are
    supported; a missing table or key stays a no-op.
    """
    if _is_table(table) and key in table:
        del table[key]


def _require_role_name(
    aflow_doc: TOMLDocument, role: str, *, must_exist: bool
) -> None:
    """Validate a role target before any mutation of the submitted draft."""
    if role == "prompts":
        raise GuidedConfigError(
            "reserved_role",
            "'prompts' is reserved for the [roles.prompts] overrides table",
        )
    if must_exist:
        roles_table = aflow_doc.get("roles")
        if not _is_table(roles_table) or role not in roles_table:
            raise GuidedConfigError(
                "unknown_role",
                f"role '{role}' is not defined in the global [roles] table",
            )


def _apply_upsert_profile(action: UpsertProfileAction, aflow_doc: TOMLDocument) -> None:
    harness = action.harness
    profile = action.profile
    adapter = ADAPTERS.get(harness)
    if adapter is None:
        supported = ", ".join(sorted(ADAPTERS))
        raise GuidedConfigError(
            "unknown_harness",
            f"unsupported harness '{harness}'; supported harnesses are: {supported}",
        )
    submitted = action.model_fields_set
    for field in ("model", "effort"):
        if field not in submitted:
            continue
        if harness == "zcode":
            # Every supplied model/effort field is rejected, explicit null
            # included: ZCode owns both settings, so they cannot be cleared.
            raise GuidedConfigError("zcode_managed_by_zcode", ZCODE_MODEL_NOTE)
        value = getattr(action, field)
        if field == "effort" and value is not None and not adapter.supports_effort:
            raise GuidedConfigError(
                "effort_not_supported",
                f"harness '{harness}' does not support an effort setting",
            )
    harness_section = _table(aflow_doc, "harness", path="harness")
    harness_table = _table(harness_section, harness, path=f"harness.{harness}")
    profiles = _table(harness_table, "profiles", path=f"harness.{harness}.profiles")
    profile_table = profiles.get(profile)
    if profile_table is not None and not _is_table(profile_table):
        raise GuidedConfigError(
            "invalid_field_type",
            f"harness.{harness}.profiles.{profile} must be a table",
        )
    if profile_table is None:
        profile_table = tomlkit.table()
        profiles[profile] = profile_table
    if "model" in submitted:
        if action.model is None:
            profile_table.pop("model", None)
        else:
            profile_table["model"] = action.model
    if "effort" in submitted:
        if action.effort is None:
            profile_table.pop("effort", None)
        else:
            profile_table["effort"] = action.effort


def _manager_source(
    declared: bool | None, extends: str | None, known: set[str]
) -> str:
    """Label where one workflow's effective supervision comes from.

    ``"workflow"`` for an explicit override, ``"base:<name>"`` when an alias
    inherits its concrete base, otherwise ``"defaults"``.
    """
    if declared is not None:
        return "workflow"
    if extends is not None and extends in known:
        return f"base:{extends}"
    return "defaults"


def _fallback_effective(
    declared: bool | None,
    extends: str | None,
    declared_map: dict[str, tuple[bool | None, str | None]],
    default: bool | None,
) -> bool:
    """Best-effort precedence read when canonical resolution is unavailable.

    Mirrors the production declared → base → defaults → False order for a
    single alias hop (aliases cannot extend aliases); the validation report
    stays authoritative for the underlying error.
    """
    if declared is not None:
        return declared
    if extends is not None and extends in declared_map:
        base_declared = declared_map[extends][0]
        if base_declared is not None:
            return base_declared
    if default is not None:
        return default
    return False


def _projection(
    aflow_doc: TOMLDocument, workflows_doc: TOMLDocument, texts: tuple[str, str]
) -> dict[str, Any]:
    aflow_section = aflow_doc.get("aflow")
    default_workflow = None
    max_turns = None
    if _is_table(aflow_section):
        raw_workflow = aflow_section.get("default_workflow")
        if isinstance(raw_workflow, str):
            default_workflow = raw_workflow
        raw_turns = aflow_section.get("max_turns")
        if isinstance(raw_turns, int) and not isinstance(raw_turns, bool):
            max_turns = raw_turns
    harnesses: dict[str, dict[str, dict[str, str | None]]] = {}
    harness_table = aflow_doc.get("harness")
    if _is_table(harness_table):
        for harness_name, harness_value in harness_table.items():
            if not _is_table(harness_value):
                continue
            inner = harness_value.get("profiles")
            if not _is_table(inner):
                continue
            profiles: dict[str, dict[str, str | None]] = {}
            for profile_name, profile_value in inner.items():
                if not _is_table(profile_value):
                    continue
                profiles[str(profile_name)] = {
                    "model": (
                        profile_value.get("model")
                        if isinstance(profile_value.get("model"), str)
                        else None
                    ),
                    "effort": (
                        profile_value.get("effort")
                        if isinstance(profile_value.get("effort"), str)
                        else None
                    ),
                }
            if profiles:
                harnesses[str(harness_name)] = profiles
    roles: dict[str, str] = {}
    roles_table = aflow_doc.get("roles")
    if _is_table(roles_table):
        for key, value in roles_table.items():
            if key != "prompts" and isinstance(value, str):
                roles[str(key)] = value
    global_role_prompts = _text_table(
        roles_table.get("prompts") if _is_table(roles_table) else None,
        path="roles.prompts",
    )
    teams: dict[str, dict[str, Any]] = {}
    teams_table = aflow_doc.get("teams")
    if _is_table(teams_table):
        for team_name, team_value in teams_table.items():
            if not _is_table(team_value):
                continue
            team_roles: dict[str, str] = {}
            inner = team_value.get("roles")
            if _is_table(inner):
                team_roles = {
                    str(key): value
                    for key, value in inner.items()
                    if isinstance(value, str)
                }
            team_upgrade = team_value.get("upgrade_to")
            team_id = str(team_name)
            team_prompts = _text_table(
                team_value.get("prompts"),
                path=f"teams.{team_name}.prompts",
            )
            declared_roles = {**roles, **team_roles}
            declared_role_sources = {role: "global" for role in roles}
            declared_role_sources.update(
                {role: team_id for role in team_roles}
            )
            declared_prompts = {**global_role_prompts, **team_prompts}
            declared_prompt_sources = {
                role: "global" for role in global_role_prompts
            }
            declared_prompt_sources.update(
                {role: team_id for role in team_prompts}
            )
            teams[team_id] = {
                "roles": team_roles,
                "prompts": team_prompts,
                # backup_team stays a distinct recovery field and is never
                # surfaced as a quality-upgrade stage.
                "upgrade_to": (
                    team_upgrade if isinstance(team_upgrade, str) else None
                ),
                "extends": (
                    team_value.get("extends")
                    if isinstance(team_value.get("extends"), str)
                    else None
                ),
                "display_name": (
                    team_value.get("display_name")
                    if isinstance(team_value.get("display_name"), str)
                    else None
                ),
                "backup_team": (
                    team_value.get("backup_team")
                    if isinstance(team_value.get("backup_team"), str)
                    else None
                ),
                # Invalid drafts still expose a bounded best-effort effective
                # view; valid candidates replace it below with the canonical
                # resolver's projection and provenance.
                "effective_roles": declared_roles,
                "effective_prompts": declared_prompts,
                "role_sources": declared_role_sources,
                "prompt_sources": declared_prompt_sources,
            }
    workflow_default_teams: dict[str, str | None] = {}
    workflows: dict[str, dict[str, Any]] = {}
    # Declared `[workflow].manager_enabled` default; None when omitted.
    default_manager_enabled: bool | None = None
    # Per-workflow declared override plus the raw extends target, so the
    # source label can distinguish an explicit flag from base/default
    # inheritance without reimplementing canonical resolution.
    declared_manager: dict[str, tuple[bool | None, str | None]] = {}
    workflow_table = workflows_doc.get("workflow")
    if _is_table(workflow_table):
        raw_default = workflow_table.get("manager_enabled")
        if isinstance(raw_default, bool):
            default_manager_enabled = raw_default
        for wf_name in _workflow_names(workflows_doc):
            wf_table = workflow_table.get(wf_name)
            if not _is_table(wf_table):
                continue
            raw_team = wf_table.get("team")
            workflow_default_teams[wf_name] = (
                raw_team if isinstance(raw_team, str) else None
            )
            raw_manager = wf_table.get("manager_enabled")
            raw_extends = wf_table.get("extends")
            declared_manager[wf_name] = (
                raw_manager if isinstance(raw_manager, bool) else None,
                raw_extends if isinstance(raw_extends, str) else None,
            )
            declared: list[str] = []
            steps_table = wf_table.get("steps")
            if _is_table(steps_table):
                declared = [str(key) for key in steps_table.keys()]
            first_step: str | None = declared[0] if declared else None
            workflows[wf_name] = {
                "declared_steps": tuple(declared),
                "first_step": first_step,
                "executable_steps": None,
                "first_executable_step": None,
                "step_roles": None,
                "manager_enabled": declared_manager[wf_name][0],
                "effective_manager_enabled": False,
                "manager_enabled_source": "defaults",
            }
    report = validate_candidate_pair(*texts)
    config = None
    materialized: set[str] = set()
    if report.state != "invalid":
        with tempfile.TemporaryDirectory(prefix="aflow-guided-form-") as temporary:
            temp_dir = Path(temporary)
            (temp_dir / "aflow.toml").write_text(texts[0], encoding="utf-8")
            (temp_dir / "workflows.toml").write_text(texts[1], encoding="utf-8")
            try:
                config = load_workflow_config(temp_dir / "aflow.toml")
            except ConfigError:
                config = None
        if config is not None:
            for team_name, team_config in config.teams.items():
                summary = teams.get(team_name)
                if summary is None:
                    continue
                resolved = resolve_team_config(config, team_name)
                summary.update(
                    {
                        "roles": dict(team_config.roles),
                        "prompts": dict(team_config.role_prompts),
                        "upgrade_to": team_config.upgrade_to,
                        "extends": team_config.extends,
                        "display_name": team_config.display_name,
                        "backup_team": team_config.backup_team,
                        "effective_roles": resolved.effective_roles,
                        "effective_prompts": resolved.effective_prompts,
                        "role_sources": resolved.role_sources,
                        "prompt_sources": resolved.prompt_sources,
                    }
                )
            materialized = set(config.workflows)
            for wf_name, wf_config in config.workflows.items():
                summary = workflows.setdefault(
                    wf_name,
                    {
                        "declared_steps": (),
                        "first_step": None,
                        "executable_steps": None,
                        "first_executable_step": None,
                        "step_roles": None,
                        "manager_enabled": None,
                        "effective_manager_enabled": False,
                        "manager_enabled_source": "defaults",
                    },
                )
                # The materialized config resolves aliases ('extends') and
                # step exclusions, so every step/team summary follows the
                # production semantics rather than the raw draft tables.
                summary["declared_steps"] = tuple(wf_config.declared_steps)
                summary["first_step"] = next(iter(wf_config.declared_steps), None)
                summary["executable_steps"] = tuple(wf_config.steps)
                summary["first_executable_step"] = wf_config.first_step
                summary["step_roles"] = {
                    step_name: step_config.role
                    for step_name, step_config in wf_config.steps.items()
                }
                workflow_default_teams[wf_name] = wf_config.team
                # Effective supervision always comes from the canonical
                # resolver; the raw declaration only decides the source label.
                declared, extends = declared_manager.get(wf_name, (None, None))
                summary["manager_enabled"] = declared
                summary["effective_manager_enabled"] = bool(wf_config.manager_enabled)
                summary["manager_enabled_source"] = _manager_source(
                    declared, extends, set(declared_manager)
                )
    for wf_name, summary in workflows.items():
        if wf_name in materialized:
            continue
        # No canonical resolution exists for this workflow (semantically
        # invalid pair or unloadable config). Project the same declared →
        # base → defaults → False precedence best-effort so the client still
        # shows which declaration each workflow carries; the validation
        # report remains the authority on the underlying error.
        declared, extends = declared_manager.get(wf_name, (None, None))
        summary["manager_enabled"] = declared
        summary["manager_enabled_source"] = _manager_source(
            declared, extends, set(declared_manager)
        )
        summary["effective_manager_enabled"] = _fallback_effective(
            declared, extends, declared_manager, default_manager_enabled
        )
    named_prompts = _text_table(aflow_doc.get("prompts"), path="prompts")
    role_prompts = global_role_prompts
    usages: dict[str, list[str]] = {name: [] for name in named_prompts}
    for path, values in prompt_reference_arrays(workflows_doc):
        for name in named_prompts:
            if name in values:
                usages[name].append(path)
    for path, parent, key in merge_prompt_sites(workflows_doc):
        value = parent[key]
        for name in named_prompts:
            if (isinstance(value, str) and value == name) or (
                isinstance(value, list) and name in value
            ):
                usages[name].append(path)
    return {
        "default_workflow": default_workflow,
        "prompts": named_prompts,
        "role_prompts": role_prompts,
        "prompt_usages": {name: tuple(paths) for name, paths in usages.items()},
        "template_variables": PROMPT_TEMPLATE_VARIABLE_CATALOG,
        "max_turns": max_turns,
        "harnesses": harnesses,
        "roles": roles,
        "teams": teams,
        "workflow_default_teams": workflow_default_teams,
        "workflows": workflows,
        "default_manager_enabled": default_manager_enabled,
    }


def _choices(aflow_doc: TOMLDocument, workflows_doc: TOMLDocument) -> dict[str, Any]:
    profiles_by_harness = _configured_profiles(aflow_doc)
    selectors = sorted(
        f"{harness}.{profile}"
        for harness, profiles in profiles_by_harness.items()
        for profile in profiles
    )
    roles_table = aflow_doc.get("roles")
    roles = (
        sorted(str(key) for key in roles_table.keys() if str(key) != "prompts")
        if _is_table(roles_table)
        else []
    )
    teams_table = aflow_doc.get("teams")
    teams = (
        sorted(str(key) for key in teams_table.keys())
        if _is_table(teams_table)
        else []
    )
    return {
        "harnesses": tuple(sorted(profiles_by_harness)),
        "profiles": {
            harness: tuple(sorted(profiles))
            for harness, profiles in sorted(profiles_by_harness.items())
        },
        "selectors": tuple(selectors),
        "roles": tuple(roles),
        "teams": tuple(teams),
        "workflows": tuple(sorted(_workflow_names(workflows_doc))),
    }


def _bundled_suggestions() -> dict[str, Any]:
    bundled_text = resources.files("aflow").joinpath("aflow.toml").read_text(
        encoding="utf-8"
    )
    bundled = tomllib.loads(bundled_text)
    profiles: list[dict[str, str | None]] = []
    for harness_name, harness_value in bundled.get("harness", {}).items():
        for profile_name, profile_value in harness_value.get("profiles", {}).items():
            profiles.append(
                {
                    "harness": str(harness_name),
                    "profile": str(profile_name),
                    "model": profile_value.get("model"),
                    "effort": profile_value.get("effort"),
                }
            )
    harnesses = [
        {
            "name": name,
            "supports_effort": adapter.supports_effort,
            # ZCode model/effort are owned by ZCode's own configuration.
            "custom_model_supported": name != "zcode",
        }
        for name, adapter in sorted(ADAPTERS.items())
    ]
    return {
        "label": "suggestion",
        "harnesses": tuple(harnesses),
        "profiles": tuple(profiles),
        "note": _SUGGESTION_NOTE,
    }


def guided_form_response(
    aflow_text: str,
    workflows_text: str,
    action: GuidedConfigAction | None = None,
) -> dict[str, Any]:
    """Transform one candidate pair (plus optional action) without saving."""
    check_document_text("aflow.toml", aflow_text)
    check_document_text("workflows.toml", workflows_text)
    suggestions = _bundled_suggestions()

    if isinstance(action, BuildStarterAction):
        if aflow_text != "" or workflows_text != "":
            raise GuidedConfigError(
                "build_starter_requires_empty_pair",
                "build_starter is only available when both documents are empty",
            )
        try:
            new_aflow, new_workflows = render_starter_documents(
                action.workflow, action.team, action.main_branch
            )
        except ConfigError as exc:
            raise GuidedConfigError(
                "invalid_action_value", " ".join(str(exc).split())[:300]
            ) from exc
        texts = (new_aflow, new_workflows)
        return _finish(texts, (aflow_text, workflows_text), suggestions)

    syntax_issues = []
    for name, text in (("aflow.toml", aflow_text), ("workflows.toml", workflows_text)):
        issue = _syntax_issue(name, text)
        if issue is not None:
            syntax_issues.append(issue)
    if syntax_issues:
        # Never silently repair: return unchanged texts and no projection so
        # the client keeps its draft and edits via Advanced TOML.
        report = validate_candidate_pair(aflow_text, workflows_text)
        return {
            "aflow_toml": aflow_text,
            "workflows_toml": workflows_text,
            "changed": False,
            "validation": {
                "state": report.state,
                "issues": tuple(
                    {"document": i.document, "line": i.line, "message": i.message}
                    for i in report.issues
                ),
                "placeholders": report.placeholders,
                "workflows": (),
                "teams": (),
                "roles": (),
            },
            "form": None,
            "syntax_issues": tuple(
                {"document": i.document, "line": i.line, "message": i.message}
                for i in syntax_issues
            ),
            "choices": {
                "harnesses": (),
                "profiles": {},
                "selectors": (),
                "roles": (),
                "teams": (),
                "workflows": (),
            },
            "suggestions": suggestions,
            "starter_defaults": None,
        }

    aflow_doc = _parse_document("aflow.toml", aflow_text)
    workflows_doc = _parse_document("workflows.toml", workflows_text)
    try:
        if isinstance(action, SetWorkflowDefaultTeamAction):
            _require_workflow(workflows_doc, action.workflow)
            wf_table = _table(workflows_doc["workflow"], action.workflow, path=f"workflow.{action.workflow}")  # type: ignore[arg-type, index]
            if action.team is None:
                wf_table.pop("team", None)
            else:
                _require_team(aflow_doc, action.team)
                wf_table["team"] = action.team
        elif action is not None:
            _apply_action(action, aflow_doc, workflows_doc)
    except ConfigError as exc:
        # Starter-name and renderer validators raise ConfigError; a guided
        # action rejection must stay bounded instead of surfacing as HTTP 500.
        raise GuidedConfigError(
            "invalid_action_value", " ".join(str(exc).split())[:300]
        ) from exc

    new_aflow = (
        tomlkit.dumps(aflow_doc) if action is not None else aflow_text
    )
    new_workflows = (
        tomlkit.dumps(workflows_doc)
        if action is not None
        else workflows_text
    )
    texts = (new_aflow, new_workflows)
    return _finish(texts, (aflow_text, workflows_text), suggestions)


def _finish(
    texts: tuple[str, str],
    originals: tuple[str, str],
    suggestions: dict[str, Any],
) -> dict[str, Any]:
    aflow_doc = _parse_document("aflow.toml", texts[0])
    workflows_doc = _parse_document("workflows.toml", texts[1])
    report = validate_candidate_pair(*texts)
    try:
        form = _projection(aflow_doc, workflows_doc, texts)
    except GuidedConfigError:
        # A syntactically valid but mistyped prompt table keeps both documents
        # untouched: the production validation report carries the semantic
        # error and the client repairs the raw text through Advanced TOML.
        form = None
    return {
        "aflow_toml": texts[0],
        "workflows_toml": texts[1],
        "changed": texts != originals,
        "validation": {
            "state": report.state,
            "issues": tuple(
                {"document": i.document, "line": i.line, "message": i.message}
                for i in report.issues
            ),
            "placeholders": report.placeholders,
            "workflows": report.workflows,
            "teams": report.teams,
            "roles": report.roles,
        },
        "form": form,
        "syntax_issues": (),
        "choices": _choices(aflow_doc, workflows_doc),
        "suggestions": suggestions,
        "starter_defaults": None,
    }
