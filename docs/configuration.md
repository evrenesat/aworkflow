# Configuration

Config is split across two TOML files:

- `aflow.toml` for global settings, harness profiles, role mappings, team overrides, error handling, and prompt templates.
- `workflows.toml` for workflow definitions and workflow aliases.

On first run, `aflow` creates both files under `~/.config/aflow/` from packaged defaults and exits so you can edit them.

## `[aflow]` Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `default_workflow` | string | - | Workflow to run when none is specified on the CLI. |
| `keep_runs` | int | `20` | Number of run log directories to retain under `.aflow/runs/`. |
| `max_turns` | int | `15` | Hard cap on turns for a run. `--max-turns` / `-mt` overrides it for one invocation. |
| `retry_inconsistent_checkpoint_state` | int | `0` | Automatic retry count when a harness exits cleanly but leaves a checkpoint heading checked while tasks remain unchecked. The packaged bootstrap file ships `1`, so a freshly bootstrapped config effectively defaults to 1. |
| `banner_files_limit` | int | `10` | Maximum changed files shown in the live banner before `+N more`. |
| `max_same_step_turns` | int | `5` | Maximum consecutive turns the same step can be selected in multi-step workflows. `0` disables it. |
| `team_lead` | string | - | Role name used for merge handoff and fallback harness recovery. Required for workflows with `merge` teardown. |
| `branch_prefix` | string | - | Feature branch prefix template. Combined with a sanitized plan stem and timestamp suffix. |
| `worktree_prefix` | string | - | Linked worktree directory prefix template. |
| `worktree_root` | string | - | Root directory where linked worktrees are created. Must not be inside the primary repo root. Supports `~`. |

Each concrete workflow can also set `retry_inconsistent_checkpoint_state` to override the global value.

## Example

```toml
# aflow.toml
[aflow]
default_workflow = "medium"
keep_runs = 10
max_turns = 12
retry_inconsistent_checkpoint_state = 1
team_lead = "senior_architect"
branch_prefix = "aflow-{PLAN_NAME}"
worktree_prefix = "aflow-{PLAN_NAME}"
worktree_root = "~/code/worktrees"

[harness.codex.profiles.sol-high]
model = "gpt-5.6-sol"
effort = "high"

[harness.codex.profiles.sol-med]
model = "gpt-5.6-sol"
effort = "medium"

[harness.codex.profiles.luna-max]
model = "gpt-5.6-luna"
effort = "max"

[harness.reasonix.profiles.ds4-flash]
model = "deepseek-flash"

[harness.reasonix.profiles.ds4-pro]
model = "deepseek-pro"

[harness.opencode.profiles."glm-5.3"]
model = "zai-coding-plan/glm-5.3"

[harness.opencode.profiles."glm-5.3-flash"]
model = "zai-coding-plan/glm-5.3-flash"

[roles]
architect = "codex.sol-high"
worker = "codex.sol-med"
reviewer = "codex.sol-high"
senior_architect = "codex.sol-high"
manager_lite = "codex.luna-max"
manager_full = "codex.sol-high"

[teams.codex1]
backup_team = "7teen"

[teams.codex1.roles]
worker = "codex.sol-med"

[teams.7teen.roles]
worker = "codex.luna-max"

[error_handling.harness_error_recovery]
max_consecutive_recoveries = 3
team_lead_skill = "aflow-harness-recovery-lead"

[[error_handling.harness_error_recovery.rules]]
action = "retry_same_team_after_delay"
match = ["throttled", "rate limit"]
delay_seconds = 30

[prompts]
simple_implementation = "Work from {ACTIVE_PLAN_PATH}. Use 'aflow-execute-plan' skill."
simple_merge = "Merge into {MAIN_BRANCH}. Feature branch: {FEATURE_BRANCH}."
```

```toml
# workflows.toml
[workflow]
setup = ["worktree", "branch"]
teardown = ["merge", "rm_worktree"]
main_branch = "main"

[workflow.ralph.steps.implement_plan]
role = "worker"
prompts = ["simple_implementation"]
go = [
  { to = "END", when = "DONE || MAX_TURNS_REACHED" },
  { to = "implement_plan" },
]

[workflow.ralph_jr]
extends = "ralph"
team = "7teen"
setup = ["branch"]
teardown = ["merge"]
merge_prompt = ["simple_merge"]
```

## Roles, Teams, and Harness Profiles

- A step `role` names a key from `[roles]`.
- `harness.<name>.profiles.<profile>` tables set `model` and optional `effort`.
- Global roles map to fully qualified `harness.profile` selectors.
- Team tables override a subset of global roles. Missing roles fall back to `[roles]`.
- Team tables can set `backup_team`, naming the next team to try when deterministic harness recovery switches away from the active team.
- Team tables can also set `upgrade_to`, a separate quality/capability edge that the manager may select for exactly one next implementation attempt.
- Backup and upgrade chains are each validated at config load: targets must exist, cannot point to themselves, and cannot form cycles.

Role prompts add static system guidance without changing the existing role
selector interface:

```toml
[roles.prompts]
reviewer = "Use the material-code-review skill during code review."

[teams.codex1.prompts]
reviewer = "Replacement reviewer guidance for codex1."
```

For an ordinary workflow step, the active team's prompt for the step role
replaces the global role prompt; a missing team prompt falls back to
`[roles.prompts]`, then to an empty system prompt. Team switches, upgrades, and
overrides therefore select both the role's model mapping and prompt on the next
workflow invocation. Inconsistent-checkpoint retries retain the prompt for the
applicable role and team. Manager, merge, initialization, recovery, and other
lifecycle calls do not inherit role prompts.

Prompt keys must name roles declared in `[roles]`, and values must be non-empty
strings. Role prompts are static instructions: workflow placeholders and
`file://` expansion are not applied. Global and team prompt maps are included
in the frozen run fingerprint, and the resolved system prompt is persisted with
each ordinary turn's durable prompt artifacts.

## Interstep Manager Supervision

Manager supervision is an opt-in control gate for existing configurations. A
freshly bootstrapped config enables it, while a config with no `[manager]`
section preserves the prior workflow, recovery, turn-count, and merge behavior.
Add the following roles and section to opt in safely:

```toml
[roles]
# Existing roles remain here.
manager_lite = "codex.nano"
manager_full = "codex.high"

[manager]
enabled = true
lite_role = "manager_lite"
full_role = "manager_full"
full_after_stalled_turns = 2
skill = "aflow-manager"
repartition_skill = "aflow-repartition-checkpoint"
```

`lite_role` and `full_role` are required when `enabled = true`, must be
non-empty role names, and resolve through the run's baseline team before the
global `[roles]` map. This means they use normal harness/profile selection;
the manager is never routed through a temporary implementation upgrade.
`full_after_stalled_turns` defaults to `2` and must be at least `1`. It counts
consecutive finalized executions of the same workflow step whose plan snapshot
did not change. An unchanged `implement → review` sequence is progress, not a
two-turn stall. Two unchanged reviewer outcomes select Full immediately after
the second rejection, but only within the currently open original-checkpoint
scope.
`skill` defaults to `aflow-manager`. Each manager prompt requests that configured
skill when the harness supports skills and also carries the complete strict JSON
contract inline, so protocol correctness does not depend on skill installation.
`repartition_skill` defaults to `aflow-repartition-checkpoint`. The same resolved
`full_role` performs both strict read-only repartition subcalls; no third role is
introduced. The controller freezes both skill names in the run configuration.

At a first scoped reviewer rejection, Lite decides by cause: it may retain the
same worker for a bounded repair, use one eligible `upgrade_to` edge for a
capability/convergence failure, or escalate structural uncertainty. An exposed
edge is not mandatory. The second rejection in the same open scope selects Full
directly with the complete rejection and attempt history.

Full alone may select `repartition_current_checkpoint`, and only when the
controller exposes it for a live scope with an immutable envelope. A worker or
reviewer `AFLOW_SCOPE_PRESSURE` marker forces Full evaluation but does not
mandate a split. File counts, line counts, elapsed time, and generated volume
are evidence rather than gates. The Full manager is read-only; the controller
owns rendering, validation, application, and routing. `AFLOW_STOP` remains
terminal for semantic, safety, ownership, destructive, and other hard blockers.

Lite is the normal cost-aware supervisor. It receives the finished turn's
complete semantic result, compact run history, structured plan state, routing
state, and bounded diagnostic excerpts, but never plan prose or prompt bodies.
Full receives the same context plus the complete current active-plan Markdown.
The controller chooses Full directly after the configured same-step stall
threshold, after repeated reviewer-to-implementer non-convergence within one
open checkpoint scope, and for explicit stops, invalid plans, or ambiguous
failures. Lite
can also request one immediate Full decision at the same boundary.

### Team upgrade routes

Use `upgrade_to` only for a manager-selected quality escalation, not for
operational recovery:

```toml
[teams.codex1]
backup_team = "fallback"
upgrade_to = "codexmax"

[teams.codex1.roles]
worker = "codex.nano"

[teams.codexmax]
upgrade_to = "codexultra"

[teams.codexmax.roles]
worker = "codex.mini"

[teams.codexultra.roles]
worker = "codex.high"

[teams.fallback.roles]
worker = "codex.nano"
```

For `upgrade_next_implementation`, the controller follows one `upgrade_to`
edge from the team that made the most recent implementation attempt (or the
baseline team), resolves the proposed implementation role on the target team,
and requires a different selector. The override is persisted and consumed only
when that exact next implementation step starts. Reviewers, managers, later
steps, and normal routing immediately return to the baseline team. Explicit
chains are allowed, but each manager decision advances only one edge. Repeated
rejection preserves the original checkpoint's review scope, so the next edge is
resolved from the actual upgraded worker that was just reviewed. Approval closes
that scope; the next checkpoint starts from baseline routing while prior attempt
history remains available for analysis.

In this example, successive manager decisions can route one rejected checkpoint
through `codex1 → codexmax → codexultra`. They cannot skip directly from
`codex1` to `codexultra`, and `fallback` remains an independent operational
recovery route.

`backup_team` remains the immediate operational fallback for a failed harness
retry. It is selected by recovery rules or the manager's
`switch_to_backup_and_retry` action; it is not an alias for `upgrade_to`
and does not change quality-upgrade routing.

## Workflows

- Bare `[workflow]` in `workflows.toml` is the lifecycle defaults table, not a runnable workflow.
- Concrete workflows live under `[workflow.<name>]`.
- Alias workflows use `extends = "base_workflow"` and may set an optional `team`.
- Alias workflows inherit steps from the base workflow and cannot redefine `steps`.
- Aliases also cannot set `retry_inconsistent_checkpoint_state` (it is always
  inherited from the base) and cannot extend another alias — only concrete
  base workflows. Alias cycles are rejected at config load.
- `exclude = ["step_name"]` removes steps from execution while keeping them visible in `aflow show` and the live banner. Alias exclusions are applied after inheritance.
- Concrete workflows start at their first declared step unless `--start-step` overrides that.
- `prompts` must be a non-empty array of prompt keys.
- `go` transitions are checked in declaration order. First match wins.
- A transition without `when` is an unconditional fallback.
- `preserve_active_plan` is an optional transition boolean that defaults to
  `false`. When `true`, a non-`END` transition keeps the current active plan if
  the turn did not create a replacement. A newly created plan always takes
  precedence. Preservation is invalid on transitions to `END`.

Repair implementation can therefore return to review with the same overlay:

```toml
go = [
  { to = "END", when = "MAX_TURNS_REACHED" },
  { to = "review_checkpoint", preserve_active_plan = true },
]
```

Accepted lifecycle combinations are:

- `([], [])` - no lifecycle; execution and resume use the primary checkout
- `(["branch"], ["merge"])` - branch-only flow; a clean completed feature
  branch is switched back to `main` and fast-forwarded by the engine
- `(["worktree", "branch"], ["merge", "rm_worktree"])` - linked worktree flow

When teardown includes `merge`, config validation requires `[aflow].team_lead` and verifies the role can resolve through the effective team or global roles.

## Conditions

Supported condition symbols:

- `DONE` - true when the original user-supplied plan file is complete after the current step finishes.
- `NEW_PLAN_EXISTS` - true only when the current step created the generated candidate file at `NEW_PLAN_PATH`; it does not describe an earlier active repair plan.
- `MAX_TURNS_REACHED` - true only on the last allowed turn.

Boolean expressions support `&&`, `||`, `!`, and parentheses.

## Prompt Templates

Prompt values can be inline text or `file://` paths:

- absolute: `file:///path/to/prompt.txt`
- config-relative: `file://prompts/implementation.txt`
- cwd-relative: `file://./local-prompt.txt`

Workflow prompt placeholders:

- `{ORIGINAL_PLAN_PATH}`
- `{ACTIVE_PLAN_PATH}`
- `{NEW_PLAN_PATH}`

Merge prompt placeholders:

- `{MAIN_BRANCH}`
- `{FEATURE_BRANCH}`
- `{PRIMARY_REPO_ROOT}`
- `{EXECUTION_REPO_ROOT}`
- `{FEATURE_WORKTREE_PATH}`

Those placeholders belong in workflow prompt templates. Bundled skills under `aflow/bundled_skills/` are static guidance files and should not contain unresolved workflow variables.

## Harness Error Recovery

Harness error recovery lives under `[error_handling.harness_error_recovery]`.

Rules:

- `rules` are checked in declaration order and first match wins.
- Matching requires every string in `match` to appear in stdout/stderr evidence, case-insensitively.
- Supported actions are `retry_same_team_after_delay`, `switch_to_backup_team_and_retry`, and `fail_immediately`.
- `delay_seconds` is accepted only for retry and switch actions, defaulting to `0`.
- `max_consecutive_recoveries` caps deterministic and team-lead-recommended recoveries together.
- `team_lead_skill` is parsed for compatibility, but the recovery handoff currently runs through `[aflow].team_lead`.
- Recovery only runs when the turn did not advance the plan snapshot.
- If no rule matches and the process exit code is non-zero, manager-enabled runs send the ambiguous boundary to Full supervision; manager-disabled runs retain the existing team-lead recovery behavior when `[aflow].team_lead` is configured.
- If no rule matches and no team lead is configured, `aflow` skips recovery.
- Backup-team switches use `teams.<team>.backup_team` only for the immediate retry path; later normal workflow steps return to normal team resolution.

The recovery handoff expects strict JSON with:

- `action`
- `delay_seconds`
- `reason`
- `suggested_keywords`
- `suggested_action`
