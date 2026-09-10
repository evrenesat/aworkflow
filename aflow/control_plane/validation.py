"""Pure validation for live run-control targets.

The controller and remote control service must agree about which current
workflow, team, role, and profile selections are executable.  This module
contains only configuration-data checks; it never reads files, starts a
provider, or inspects controller state.
"""

from __future__ import annotations

from collections.abc import Mapping

from aflow.config import WorkflowUserConfig


_NON_OVERRIDABLE_ROLES = frozenset(
    {"manager", "lifecycle", "initialization", "merge", "recovery"}
)


class ControlValidationError(ValueError):
    """A proposed live control does not resolve in the current config."""

    code = "validation_error"

    def __init__(self, *, field: str, target: str, message: str) -> None:
        self.field = field
        self.target = target
        self.message = message
        super().__init__(message)


def _selector_error(
    selector: str,
    config: WorkflowUserConfig,
    *,
    step_path: str,
) -> None:
    if "." not in selector:
        raise ControlValidationError(
            field="role_selectors",
            target=selector,
            message=(
                "step profile must be fully qualified (harness.profile) "
                f"in {step_path}, got '{selector}'"
            ),
        )
    harness_name, _, profile_name = selector.partition(".")
    if not harness_name or not profile_name:
        raise ControlValidationError(
            field="role_selectors",
            target=selector,
            message=f"invalid profile selector '{selector}' in {step_path}",
        )
    harness = config.harnesses.get(harness_name)
    if harness is None:
        raise ControlValidationError(
            field="role_selectors",
            target=selector,
            message=(
                f"workflow step references unknown harness '{harness_name}' "
                f"in {step_path}"
            ),
        )
    if profile_name not in harness.profiles:
        raise ControlValidationError(
            field="role_selectors",
            target=selector,
            message=(
                f"workflow step references unknown profile '{profile_name}' "
                f"for harness '{harness_name}' in {step_path}"
            ),
        )


def _step_selector(
    config: WorkflowUserConfig,
    *,
    role: str,
    team: str | None,
    valid_role_selectors: Mapping[str, str],
) -> str | None:
    """Resolve one step's selector using run-local then team precedence."""
    selector = valid_role_selectors.get(role)
    if selector is not None:
        return selector
    selector = config.roles.get(role)
    if team is not None:
        team_config = config.teams[team]
        selector = team_config.roles.get(role, selector)
    if selector is None and "." in role:
        return role
    return selector


def _validate_role_selectors(
    selectors: Mapping[str, str],
    allowed_roles: set[str],
    configured_selectors: set[str],
) -> None:
    unknown_roles = sorted(set(selectors) - allowed_roles)
    if unknown_roles:
        role = unknown_roles[0]
        raise ControlValidationError(
            field=f"role_selectors.{role}",
            target=role,
            message=(
                "roles contains undeclared ordinary roles: "
                + ", ".join(unknown_roles)
            ),
        )
    unknown_selectors = sorted(set(selectors.values()) - configured_selectors)
    if unknown_selectors:
        selector = unknown_selectors[0]
        role = next(
            role
            for role, value in sorted(selectors.items())
            if value == selector
        )
        raise ControlValidationError(
            field=f"role_selectors.{role}",
            target=selector,
            message=(
                "roles contains selectors not configured in current configuration: "
                + ", ".join(unknown_selectors)
            ),
        )


def _validate_max_turns(
    max_turns: int | None,
    completed_turns: int | None,
) -> None:
    if max_turns is not None and max_turns < 1:
        raise ControlValidationError(
            field="max_turns",
            target=str(max_turns),
            message="max_turns must be positive",
        )
    if (
        max_turns is not None
        and completed_turns is not None
        and max_turns < completed_turns
    ):
        raise ControlValidationError(
            field="max_turns",
            target=str(max_turns),
            message=(
                f"max_turns ({max_turns}) cannot be below completed turns "
                f"({completed_turns})"
            ),
        )


def validate_override_targets(
    config: WorkflowUserConfig,
    *,
    workflow_name: str,
    step_name: str | None,
    team: str | None,
    role_selectors: Mapping[str, str] | None = None,
    max_turns: int | None = None,
    completed_turns: int | None = None,
    step_field: str = "current_step",
) -> None:
    """Validate the effective live targets for one control boundary.

    ``role_selectors`` must already contain the effective replacement map;
    callers that accept partial requests merge replacements before calling
    this function.  Unknown request selectors are reported after the current
    step is resolved so a replacement can repair an obsolete selection.
    """
    workflow = config.workflows.get(workflow_name)
    if workflow is None:
        raise ControlValidationError(
            field="workflow",
            target=workflow_name,
            message=f"workflow '{workflow_name}' is not configured",
        )

    target_step = step_name or workflow.first_step
    if target_step is None:
        raise ControlValidationError(
            field=step_field,
            target="",
            message=f"workflow '{workflow_name}' has no executable steps",
        )
    step = workflow.steps.get(target_step)
    if step is None:
        raise ControlValidationError(
            field=step_field,
            target=target_step,
            message=(
                f"{step_field} '{target_step}' is not an executable step in "
                f"workflow '{workflow_name}'"
            ),
        )

    if team is not None and team not in config.teams:
        raise ControlValidationError(
            field="team",
            target=team,
            message=f"team '{team}' is not configured",
        )

    effective_selectors = dict(role_selectors or {})
    allowed_roles = {
        candidate.role
        for candidate in workflow.steps.values()
        if candidate.role not in _NON_OVERRIDABLE_ROLES and "." not in candidate.role
    }
    configured_selectors = {
        f"{harness_name}.{profile_name}"
        for harness_name, harness in config.harnesses.items()
        for profile_name in harness.profiles
    }
    valid_selectors = {
        role: selector
        for role, selector in effective_selectors.items()
        if role in allowed_roles and selector in configured_selectors
    }

    # With no team replacement, the historical runtime path reported malformed
    # role replacements before trying to resolve a direct step selector.  Keep
    # that observable order while still sharing all checks with the service.
    if team is None:
        _validate_max_turns(max_turns, completed_turns)
        _validate_role_selectors(
            effective_selectors, allowed_roles, configured_selectors
        )

    step_path = f"workflow.{workflow_name}.steps.{target_step}"
    try:
        selector = _step_selector(
            config,
            role=step.role,
            team=team,
            valid_role_selectors=valid_selectors,
        )
        if selector is None:
            raise ControlValidationError(
                field=step_field,
                target=target_step,
                message=(
                    f"workflow step references unknown role '{step.role}' "
                    f"in {step_path}"
                ),
            )
        _selector_error(selector, config, step_path=step_path)
    except ControlValidationError as exc:
        if team is not None:
            raise ControlValidationError(
                field="team",
                target=team,
                message=(
                    f"team '{team}' is incompatible with step '{target_step}': "
                    f"{exc}"
                ),
            ) from exc
        raise

    if team is not None:
        _validate_max_turns(max_turns, completed_turns)
        _validate_role_selectors(
            effective_selectors, allowed_roles, configured_selectors
        )
