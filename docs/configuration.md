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
| `banner_files_limit` | int | `10` | Maximum changed files shown in status records before `+N more`. |
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

[teams.standard]
display_name = "Standard"
backup_team = "fallback"
upgrade_to = "high"

[teams.standard.roles]
worker = "codex.sol-med"

[teams.high.roles]
worker = "codex.sol-high"

[teams.fallback.roles]
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
team = "high"
setup = ["branch"]
teardown = ["merge"]
merge_prompt = ["simple_merge"]
```

## Roles, Teams, and Harness Profiles

- A step `role` names a key from `[roles]`.
- `harness.<name>.profiles.<profile>` tables set `model` and optional `effort`.
- Global roles map to fully qualified `harness.profile` selectors.
- Team tables override a subset of global roles. Missing roles fall back to
  `[roles]`, or to the selected team's direct base declaration when one is
  configured.
- A team may declare `extends = "base_team_id"` and inherit only that base's
  role and prompt declarations. The base must be a team without its own
  `extends`; missing bases, self-references, cycles, and deeper inheritance
  are rejected while loading. Role and prompt maps resolve independently:
  child declaration, then base declaration, then global declaration.
- `display_name` is an optional trimmed label of at most 128 characters. It is
  never an identity or a reference; team IDs remain case-sensitive and stable.
  When omitted, presentation uses the existing machine-name formatter.
- In the guided family editor, labels preserve spaces while being typed and are
  trimmed only when committed. Effective roles, prompts, and their sources are
  refreshed from the server for unsaved edits; a failed refresh keeps the
  declarations editable. Restoring a legacy inline role removes that actual
  declaration, while other metadata and role entries remain intact.
- Team tables can set `backup_team`, naming the next team to try when deterministic harness recovery switches away from the active team.
- Team tables can also set `upgrade_to`, a separate quality/capability edge that the manager may select for exactly one next implementation attempt.
- `extends`, `backup_team`, and `upgrade_to` are independent links. Backup and
  upgrade targets must exist, cannot point to themselves, and cannot form
  cycles; neither route is inherited, and inheritance does not imply an
  upgrade stage.

For example, a child can override only the worker while reusing the base's
reviewer and prompts:

```toml
[teams.product]
display_name = "Product development"
upgrade_to = "product_stronger"

[teams.product.roles]
worker = "codex.standard"
reviewer = "codex.review"

[teams.product_stronger]
display_name = "Stronger worker"
extends = "product"

[teams.product_stronger.roles]
worker = "codex.strong"
```

The current flat form remains valid; the family form is an explicit, opt-in
storage change. For example, a before/after migration can keep the same IDs
while making the shared reviewer declaration visible:

```toml
# Current flat teams: every stage is standalone.
[teams.product]
upgrade_to = "product_stronger"
[teams.product.roles]
worker = "codex.standard"
reviewer = "codex.review"

[teams.product_stronger]
[teams.product_stronger.roles]
worker = "codex.strong"
reviewer = "codex.review"
```

```toml
# Proposed family: the child stores only its worker difference.
[teams.product]
display_name = "Product development"
upgrade_to = "product_stronger"
[teams.product.roles]
worker = "codex.standard"
reviewer = "codex.review"

[teams.product_stronger]
display_name = "Stronger worker"
extends = "product"
[teams.product_stronger.roles]
worker = "codex.strong"
```

This conversion is never inferred from names or similar values. Inheritance is
one direct level only: a child may extend a root Base, but a child cannot extend
another child or form a cycle. `display_name` is presentation text, not an ID;
references, workflow defaults, backup links and `upgrade_to` continue to use the
case-sensitive stable IDs. The family editor refuses to delete a stage while a
workflow/default, inheritance, upgrade or backup reference still names it, and
rewires only the reviewed predecessor edge after confirmation. `extends` shares
role/prompt declarations; it does not inherit backup or upgrade routes. A
selected family/stage in the UI is still submitted as the exact selected team
ID, while an omitted launch value continues to follow the saved workflow
default.

Role prompts add static system guidance without changing the existing role
selector interface:

```toml
[roles.prompts]
reviewer = "Use the material-code-review skill during code review."

[teams.standard.prompts]
reviewer = "Replacement reviewer guidance for the standard team."
```

For an ordinary workflow step, the active team's prompt for the step role
replaces the global role prompt; a missing prompt falls back to the direct
base's prompt and then `[roles.prompts]`, finally to an empty system prompt.
Team switches, upgrades, and overrides therefore select both the role's model
mapping and prompt on the next workflow invocation. Inconsistent-checkpoint
retries retain the prompt for the applicable role and team. Manager, merge,
initialization, recovery, and other lifecycle calls do not independently add
role prompts.

Prompt keys must name roles declared in `[roles]`, and values must be non-empty
strings. Role prompts are static instructions: workflow placeholders and
`file://` expansion are not applied. Global and team prompt maps may be
included in the diagnostic launch fingerprint, and the resolved system prompt
is persisted with each ordinary turn's durable prompt artifacts. The current
pair is reloaded at the next safe boundary, so a valid prompt edit affects the
next invocation without changing an in-flight call.

## Interstep Manager Supervision

Manager supervision is an opt-in control gate per workflow. A freshly
bootstrapped config enables it through the `[workflow]` defaults, while a
config with no `manager_enabled` flag anywhere preserves the prior workflow,
recovery, turn-count, and merge behavior. Add the following roles and sections
to opt in safely:

```toml
[roles]
# Existing roles remain here.
manager_lite = "codex.luna-max"
manager_full = "codex.sol-high"

[manager]
lite_role = "manager_lite"
full_role = "manager_full"
full_after_stalled_turns = 2
skill = "aflow-manager"
repartition_skill = "aflow-repartition-checkpoint"
```

```toml
# workflows.toml
[workflow]
manager_enabled = true

[workflow.ralph]
manager_enabled = false
```

Omission is disabled: a workflow without the flag inherits its concrete base
workflow's value (for aliases), else the `[workflow]` default, else disabled.
Resolution is by presence, never truthiness, so an explicit `false` overrides
an inherited `true`. The launch-default workflow is unrelated to inheritance.
Only actual TOML booleans are accepted, and no per-step flag exists. The old
global `[manager].enabled` is rejected with a message naming this replacement.
Saving a flag affects the next safe boundary for active and resumed runs;
subsequent manager calls read current saved manager settings and skill text.

`lite_role` and `full_role` are required for each enabled workflow, must be
non-empty role names, and resolve through the run's baseline team before the
global `[roles]` map. This means they use normal harness/profile selection;
the manager is never routed through a temporary implementation upgrade.
`full_after_stalled_turns` defaults to `2` and must be at least `1`. It counts
consecutive finalized executions of the same workflow step whose plan snapshot
did not change. An unchanged `implement → review` sequence is progress, not a
two-turn stall. Two unchanged reviewer outcomes select Full immediately after
the second rejection, but only within the currently open original-checkpoint
scope.
`skill` defaults to `aflow-manager`. The controller reads that configured
skill's current canonical `SKILL.md` bytes immediately before every ordinary,
note-correction, proposal, validation, and correction invocation and passes the
complete validated Markdown body as the system instruction; Python contributes
only structured runtime data (mode, level, eligible actions, proposed
transition, findings, and limits). A save between two invocations changes the
next invocation's bytes, while the configured skill names, role routing, and
  workflow configuration is reloaded at each safe boundary. An explicitly
configured non-bundled name resolves read-only from the account skill store
and must exist as a valid document; a missing or invalid skill fails the call
before any provider is invoked, and the engine never falls back to built-in
prose. Output parsing, eligibility checks, note authority, and repartition
validation remain enforced in code.
`repartition_skill` defaults to `aflow-repartition-checkpoint`. The same resolved
`full_role` performs both strict read-only repartition subcalls; no third role is
introduced. The controller resolves both skill names from the current source at
each invocation boundary.

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

Lite is the normal cost-aware supervisor. Live schema-v3 contexts provide
bounded semantic results, compact run history, structured plan state, routing
state, bounded diagnostic excerpts, and controller-declared evidence
references; neither level receives plan prose or prompt bodies inline. Full
may provide richer bounded scope and rejection evidence, while plan and
checkpoint content remains available only through the declared references.
The controller chooses Full directly after the configured same-step stall
threshold, after repeated reviewer-to-implementer non-convergence within one
open checkpoint scope, and for explicit stops, invalid plans, or ambiguous
failures. Lite can also request one immediate Full decision at the same
boundary.

### Team upgrade routes

Use `upgrade_to` only for a manager-selected quality escalation, not for
operational recovery:

```toml
[teams.standard]
backup_team = "fallback"
upgrade_to = "high"

[teams.standard.roles]
worker = "codex.sol-med"

[teams.high.roles]
worker = "codex.sol-high"

[teams.fallback.roles]
worker = "codex.luna-max"
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

In this example, a manager decision can route one rejected checkpoint from
`standard` to `high`. Because `high` has no further `upgrade_to` edge, it cannot
escalate again; `fallback` remains an independent operational recovery route.

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
- `exclude = ["step_name"]` removes steps from execution while keeping them visible in `aflow show` and status records. Alias exclusions are applied after inheritance.
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

## Global workflow configuration

All projects use one shared configuration pair: `~/.config/aflow/aflow.toml`
plus its sibling `workflows.toml`. There is no per-project configuration
layer; project-local `.aflow/config` documents from older setups are preserved
byte-for-byte but ignored for new runs.

Readiness is global: a project is ready exactly when the shared pair parses,
passes validation, and contains no `FILL_IN_MODEL` placeholders. Missing or
invalid shared workflows surface one global setup path in the remote app,
never a per-project starter wizard.

## Server settings (config.toml)

Transport settings for `aflow ui` and the remote app live in the existing
server configuration file `~/.config/aflow/config.toml`:

```toml
[server]
bind_host = "0.0.0.0"   # written explicitly by first-run setup
bind_port = 8765
auth_token = "..."      # or auth_token_file = "/path/to/file"

[control_plane]
managed_projects_root = "~/code"
```

`aflow ui` writes missing keys on first launch (password asked twice without
echo, projects root confirmed with `~/code` as the default) and stores the
file with mode 0600. The credential is the existing shared login/bearer
credential; rotating it invalidates browser sessions immediately because the
session signing key derives from it. Unrelated keys, tables, and comments in
an existing `config.toml` are preserved on writes.

## Remote configuration editing

The remote app edits the shared workflow pair as one revisioned pair:

- GET /api/config returns the exact UTF-8 text of aflow.toml and
  workflows.toml plus their combined SHA-256 revision.
- POST /api/config/validate validates a candidate pair without writing it.
- PUT /api/config requires both complete documents and the current revision.
  A stale revision, invalid pair, or placeholder selector rejects the write
  before either file changes. Running runs never block a valid save: active
  calls finish with their starting settings and the next turn or resume reads
  the saved pair. A run may retain a launch-time snapshot for diagnostics, but
  it is not an execution or admission gate.
- A successful write stages and fsyncs both documents atomically with
  rollback handling and appends redacted revision metadata to the audit log.
  The save and run-reservation snapshots share one configuration lock, so a
  launch observes either the old pair or the new pair, never half of each.

## Server settings API

- GET /api/settings reports bind host/port, the projects root, whether a
  password is set, the config.toml revision, an advanced TOML rendering that
  omits both credential keys, and which keys need a restart to take effect.
- PUT /api/settings accepts a revision, optional advanced TOML (credential
  keys are rejected there; use the write-only password field), the projects
  root, and an optional new password. Password changes apply immediately;
  binding and root changes need `aflow ui --stop` plus a restart. Responses
  and errors never contain the credential.

## DSH ACP profiles

DSH `0.1.2-rc.1` supplies the `acp` profile used by AFlow. Install the
provider bundle into that profile as well as any independently used headless
profile. For Z.AI, inherit `ZAI_API_KEY` in the AFlow process environment;
do not store the credential in AFlow configuration. Ensure the ACP profile's
model catalog advertises the selected model and reasoning effort.

```toml
[harness.dsh.profiles.glm-flash-max]
model = "zai/glm-5.3-flash"
effort = "max"

[harness.dsh.profiles.glm-max]
model = "zai/glm-5.3"
effort = "max"
```

These selectors can serve ordinary worker and reviewer steps. AFlow verifies
the model and effort through ACP before sending the prompt. Execution
uses DSH full-access mode (`DSH_PERMISSION_MODE=danger-full-access`),
matching AFlow's unattended VM model and keeping shared `/tmp` artifacts visible.
Session resume and model changes are capability-gated. Manager and lifecycle roles should
use another harness: per-call DSH model overrides require the session driver,
and the plain headless adapter rejects them instead of using global defaults.
No mid-turn steering or idempotent turn start is advertised.

## ZCode profiles

Install and authenticate the native ZCode CLI first. Configure the desired
provider, model, and reasoning in ZCode itself, for example in the execution
repository's .zcode/config.json. AFlow does not create or overwrite that file
or copy credentials. Ensure this configuration is available in the actual
execution worktree when using a Git lifecycle.

Use an empty profile and select it for the worker role:

```toml
[harness.zcode.profiles.default]
# ZCode owns the model and reasoning settings.

[roles]
worker = "zcode.default"
```

Omit model and effort from the AFlow profile: the verified 0.16.5 CLI exposes
no per-call overrides for either setting. AFlow rejects unsupported overrides
instead of silently selecting another model.

The adapter invokes zcode with --cwd, --mode yolo, --no-color, and --prompt.
It captures plain final output and does not open the interactive TUI. It does
not claim native session resume, usage accounting, or model identity beyond
the selected ZCode configuration. Keep secrets out of tracked project config.
