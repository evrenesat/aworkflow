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
    load_workflow_config,
    render_starter_documents,
    validate_starter_name,
)
from aflow.harnesses import ADAPTERS

from .models import (
    AddTeamAction,
    BuildStarterAction,
    GuidedConfigAction,
    SetDefaultWorkflowAction,
    SetGlobalRoleAction,
    SetMaxTurnsAction,
    SetTeamRoleAction,
    SetWorkflowDefaultTeamAction,
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
            if not isinstance(team_roles, Table):
                continue
            selectors.extend(
                value for value in team_roles.values() if isinstance(value, str)
            )
    return selectors


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


def _apply_action(
    action: GuidedConfigAction,
    aflow_doc: TOMLDocument,
    workflows_doc: TOMLDocument,
) -> None:
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
        profiles = _configured_profiles(aflow_doc)
        _require_profile_reference(action.selector, profiles)
        team = _require_team(aflow_doc, action.team)
        team_roles = _table(team, "roles", path=f"teams.{action.team}.roles")
        team_roles[action.role] = action.selector
        _cleanup_starter_profile(aflow_doc)
        return
    raise GuidedConfigError(
        "unknown_action", f"unsupported guided action '{action.type}'"
    )


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
    teams: dict[str, dict[str, dict[str, str]]] = {}
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
            teams[str(team_name)] = {"roles": team_roles}
    workflow_default_teams: dict[str, str | None] = {}
    workflows: dict[str, dict[str, Any]] = {}
    workflow_table = workflows_doc.get("workflow")
    if _is_table(workflow_table):
        for wf_name in _workflow_names(workflows_doc):
            wf_table = workflow_table.get(wf_name)
            if not _is_table(wf_table):
                continue
            raw_team = wf_table.get("team")
            workflow_default_teams[wf_name] = (
                raw_team if isinstance(raw_team, str) else None
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
            }
    report = validate_candidate_pair(*texts)
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
            for wf_name, wf_config in config.workflows.items():
                summary = workflows.setdefault(
                    wf_name,
                    {
                        "declared_steps": (),
                        "first_step": None,
                        "executable_steps": None,
                        "first_executable_step": None,
                    },
                )
                # The materialized config resolves aliases ('extends') and
                # step exclusions, so every step/team summary follows the
                # production semantics rather than the raw draft tables.
                summary["declared_steps"] = tuple(wf_config.declared_steps)
                summary["first_step"] = next(iter(wf_config.declared_steps), None)
                summary["executable_steps"] = tuple(wf_config.steps)
                summary["first_executable_step"] = wf_config.first_step
                workflow_default_teams[wf_name] = wf_config.team
    return {
        "default_workflow": default_workflow,
        "max_turns": max_turns,
        "harnesses": harnesses,
        "roles": roles,
        "teams": teams,
        "workflow_default_teams": workflow_default_teams,
        "workflows": workflows,
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
        "form": _projection(aflow_doc, workflows_doc, texts),
        "syntax_issues": (),
        "choices": _choices(aflow_doc, workflows_doc),
        "suggestions": suggestions,
        "starter_defaults": None,
    }
