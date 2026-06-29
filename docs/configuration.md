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
| `retry_inconsistent_checkpoint_state` | int | `0` | Automatic retry count when a harness exits cleanly but leaves a checkpoint heading checked while tasks remain unchecked. |
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

[harness.codex.profiles.high]
model = "gpt-5.4"
effort = "high"

[roles]
architect = "codex.high"
worker = "codex.high"
reviewer = "codex.high"
senior_architect = "codex.high"

[teams.codex1]
backup_team = "7teen"

[teams.codex1.roles]
worker = "codex.high"

[teams.7teen.roles]
worker = "codex.nano"

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
- Team tables can set `backup_team`, naming the next team to try when harness recovery switches away from the active team.
- Backup chains are validated at config load: targets must exist, cannot point to themselves, and cannot form cycles.

## Workflows

- Bare `[workflow]` in `workflows.toml` is the lifecycle defaults table, not a runnable workflow.
- Concrete workflows live under `[workflow.<name>]`.
- Alias workflows use `extends = "base_workflow"` and may set an optional `team`.
- Alias workflows inherit steps from the base workflow and cannot redefine `steps`.
- `exclude = ["step_name"]` removes steps from execution while keeping them visible in `aflow show` and the live banner. Alias exclusions are applied after inheritance.
- Concrete workflows start at their first declared step unless `--start-step` overrides that.
- `prompts` must be a non-empty array of prompt keys.
- `go` transitions are checked in declaration order. First match wins.
- A transition without `when` is an unconditional fallback.

Accepted lifecycle combinations are:

- `([], [])` - no lifecycle
- `(["branch"], ["merge"])` - branch-only flow
- `(["worktree", "branch"], ["merge", "rm_worktree"])` - linked worktree flow

When teardown includes `merge`, config validation requires `[aflow].team_lead` and verifies the role can resolve through the effective team or global roles.

## Conditions

Supported condition symbols:

- `DONE` - true when the original user-supplied plan file is complete after the current step finishes.
- `NEW_PLAN_EXISTS` - true when the current step created the generated candidate file at `NEW_PLAN_PATH`.
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
- If no rule matches and the process exit code is non-zero, `aflow` escalates to team-lead recovery when `[aflow].team_lead` is configured.
- If no rule matches and no team lead is configured, `aflow` skips recovery.
- Backup-team switches use `teams.<team>.backup_team` only for the immediate retry path; later normal workflow steps return to normal team resolution.

The recovery handoff expects strict JSON with:

- `action`
- `delay_seconds`
- `reason`
- `suggested_keywords`
- `suggested_action`
