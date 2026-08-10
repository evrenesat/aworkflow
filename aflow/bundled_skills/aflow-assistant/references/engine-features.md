# AFlow Engine Features Reference

Complete reference for the AFlow engine surface: configuration, workflows,
harnesses, sessions, hotplug, manager supervision, recovery, repartition, run
artifacts, CLI, skills, and plan format.

This reference is shipped with the `aflow-assistant` skill and is usable
without the aflow source checkout. When a statement here disagrees with what
you observe in a real run, treat the run artifacts as authoritative and
escalate to source inspection (`aflow/workflow.py`, `aflow/config.py`,
`aflow/hotplug.py`, `aflow/run_state.py`, `aflow/plan.py`).

## 1. Architecture Overview

`aflow` is a plan-driven workflow engine that drives existing agent CLIs
("harnesses") through checkpointed Markdown plans.

1. The CLI (`aflow run ...`) resolves workflow name, plan path, start step,
   team, max turns, and resume/startup questions.
2. Config is loaded from `~/.config/aflow/aflow.toml` plus a sibling
   `workflows.toml` (see Section 2).
3. The plan parser derives progress from `### [ ] Checkpoint` headings and
   unchecked task items (Section 14).
4. The workflow runner creates a run directory under `.aflow/runs/<run-id>/`,
   optionally sets up a lifecycle environment (worktree/branch), then runs the
   turn loop: render prompts → resolve role → harness subprocess → detect
   `AFLOW_STOP` / `AFLOW_SCOPE_PRESSURE` → reload plan → pick transition →
   persist artifacts → optional manager gate.
5. On teardown, a merge handoff runs through the configured `team_lead` role.

Key concepts:

- **Run**: one execution lineage, identified by a run id, with a durable
  `.aflow/runs/<run-id>/run.json` controller snapshot. Resume creates a new
  successor run directory and records `resumed_from_run_id`.
- **Turn**: one harness invocation for one workflow step. Turn artifacts live
  under `turns/turn-NNN/`. Manager calls are not turns.
- **Step**: a named workflow node with a role, prompt keys, and `go`
  transitions (Section 3).
- **Checkpoint**: a `### [ ] Checkpoint N: ...` plan section with task items.
- **Role/selector**: roles map to `harness.profile` selectors; teams override
  role mappings per run routing.
- **Plan paths**: `ORIGINAL_PLAN_PATH` (user plan), `ACTIVE_PLAN_PATH` (plan
  used for the current step), `NEW_PLAN_PATH` (per-turn generated repair
  plan `<stem>-cpNN-vNN.<suffix>`).

## 2. Configuration System

Config is split across two TOML files under `~/.config/aflow/` (first run
copies packaged defaults and exits):

- `aflow.toml` — `[aflow]`, `[harness.*.profiles.*]`, `[roles]`,
  `[roles.prompts]`, `[teams.*]`, `[manager]`, `[error_handling.*]`,
  `[prompts]`.
- `workflows.toml` — `[workflow]` lifecycle defaults plus
  `[workflow.<name>]` definitions and aliases.

Unknown keys are rejected with a clear `ConfigError`; validation runs after
parsing (unknown role/team/harness/profile/prompt references, team graph
cycles, manager role resolution, lifecycle combinations).

### `[aflow]` — global engine options

| Key | Type | Default (dataclass) | Notes |
|-----|------|---------------------|-------|
| `default_workflow` | string | - | Used when the CLI names no workflow. Must name a configured workflow. |
| `keep_runs` | int | `20` | Run directories retained under `.aflow/runs/` before pruning oldest. Must be ≥ 1. |
| `max_turns` | int | `15` | Hard turn cap. `--max-turns` overrides per invocation. Must be ≥ 1. |
| `retry_inconsistent_checkpoint_state` | int | `0` | Automatic retries when a harness exits cleanly but leaves the plan in an inconsistent checkpoint state. The packaged bootstrap file ships `1`. |
| `banner_files_limit` | int | `10` | Max changed files shown in the live banner before collapsing. Must be ≥ 1. |
| `max_same_step_turns` | int | `5` | Max consecutive selections of the same step in multi-step workflows. `0` disables. Must be ≥ 0. |
| `team_lead` | string | - | Role used for merge handoff and (manager-disabled) fallback harness recovery. Required for `merge` teardown; must resolve through the effective team or global roles. |
| `branch_prefix` | string | - | Feature branch prefix; combined with a sanitized plan stem and timestamp. |
| `worktree_prefix` | string | - | Linked worktree directory prefix. |
| `worktree_root` | string | - | Root for linked worktrees. Must not be inside the primary repo root. Supports `~`. |

### `[harness.<name>.profiles.<profile>]`

- `model` (string, optional) — harness model identifier.
- `effort` (string, optional) — passed through to harnesses that support it.
- Only harnesses in the adapter registry are accepted (Section 4).
- The placeholder `FILL_IN_MODEL` is detected by `find_placeholders` (used by
  startup guidance to tell the user which profile keys still need a model).

### `[roles]` and `[roles.prompts]`

- `[roles]` maps role names to fully qualified `harness.profile` selectors
  (must contain a `.`).
- `[roles.prompts]` maps role names to static system guidance strings.
  Values must be non-empty; keys must name roles in `[roles]`. Role prompts
  are static: no workflow placeholders and no `file://` expansion.
- The resolved role prompt for a step = active team's prompt for that role →
  global `[roles.prompts]` → empty. Manager, merge, bootstrap, and recovery
  calls do not inherit role prompts.

### `[teams.<name>]`

| Key | Type | Notes |
|-----|------|-------|
| `roles` | table | Overrides a subset of global roles; missing roles fall back to `[roles]`. |
| `prompts` | table | Team-level role prompt overrides (same shape as `[roles.prompts]`). |
| `backup_team` | string | Next team for deterministic harness-recovery retries (operational fallback). |
| `upgrade_to` | string | Quality/capability escalation edge selectable by manager `upgrade_next_implementation` (one hop per decision). |

Legacy inline role keys (`worker = "codex.high"` directly under
`[teams.<name>]`) are accepted only when no `roles` table is present.
Backup/upgrade chains are validated at load: targets must exist, cannot be
self-references, and cannot form cycles.

### `[manager]`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `enabled` | bool | `false` | Opt-in interstep supervision. Fresh bootstraps ship `true`. |
| `lite_role` | string | - | Required when enabled; resolves through baseline team then global roles. |
| `full_role` | string | - | Required when enabled; same resolution rule. Also performs repartition subcalls. |
| `full_after_stalled_turns` | int | `2` | Consecutive unchanged same-step executions before Full is chosen. Must be ≥ 1. |
| `skill` | string | `"aflow-manager"` | Skill name requested in manager prompts (frozen into run config). |
| `repartition_skill` | string | `"aflow-repartition-checkpoint"` | Skill name for repartition proposal/validation subcalls. |

### `[error_handling.harness_error_recovery]`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `rules` | array of tables | `[]` | Deterministic rules, checked in declaration order; first match wins. |
| `max_consecutive_recoveries` | int | `3` | Hard cap on recovery attempts in one run (deterministic + team-lead together). |
| `team_lead_skill` | string | `"aflow-harness-recovery-lead"` | Parsed for compatibility; the handoff currently runs through `[aflow].team_lead` regardless. |

Each rule table:

- `action` (required): one of `retry_same_team_after_delay`,
  `switch_to_backup_team_and_retry`, `fail_immediately`.
- `match` (required, non-empty array): every string must appear in combined
  stdout+stderr, case-insensitively.
- `delay_seconds` (optional, ≥ 0): allowed only for retry/switch actions;
  `0` means no wait.

### `[prompts]`

Named prompt templates referenced by step `prompts` keys and by
`merge_prompt`. Values can be inline text or `file://` paths
(`file:///abs/path.txt`, `file://prompts/x.txt` config-relative,
`file://./x.txt` cwd-relative). Workflow placeholders: `{ORIGINAL_PLAN_PATH}`,
`{ACTIVE_PLAN_PATH}`, `{NEW_PLAN_PATH}`. Merge prompt placeholders:
`{MAIN_BRANCH}`, `{FEATURE_BRANCH}`, `{PRIMARY_REPO_ROOT}`,
`{EXECUTION_REPO_ROOT}`, `{FEATURE_WORKTREE_PATH}`.

### `[workflow]` lifecycle defaults (in workflows.toml)

Bare `[workflow]` is the defaults table, not a runnable workflow:

- `setup` (array, default `[]`)
- `teardown` (array, default `[]`)
- `main_branch` (string, default none)
- `merge_prompt` (array of prompt keys, default `[]`)

Accepted lifecycle combinations (validated):

- `setup=[]`, `teardown=[]` — no lifecycle; execution and resume use the
  primary checkout.
- `setup=["branch"]`, `teardown=["merge"]` — branch-only; completed feature
  branch switches back to `main` and is fast-forwarded.
- `setup=["worktree","branch"]`, `teardown=["merge","rm_worktree"]` — linked
  worktree flow.

`merge` teardown requires `[aflow].team_lead` resolvable through the
workflow's team or global roles.

### Config validation and identity

- `validate_workflow_config` checks: default workflow exists; every role
  selector is fully qualified and references a configured harness/profile;
  role prompts reference known roles; team roles/prompts resolve; backup and
  upgrade chains are acyclic; manager roles resolve; step roles and prompt
  keys exist; lifecycle combos are valid; merge teardown has a resolvable
  team lead.
- Runs freeze a fingerprint of the resolved workflow, roles, teams, harness
  profiles, manager policy, and error-handling config into `run.json`.
  Resume requires the frozen identity to match the currently loaded config.

## 3. Workflow Definition

### Steps

Each `[workflow.<name>.steps.<step>]` requires exactly:

- `role` (string) — key into `[roles]`.
- `prompts` (non-empty array) — keys into `[prompts]`, rendered in order.
- `go` (non-empty array) — transition rules, checked top to bottom; first
  match wins.

The first declared step becomes the workflow's first step (after exclusion
filtering). `--start-step` accepts a step name or 1-based index.

### Transitions (`go`)

Each entry is an inline table:

- `to` (required) — a step name matching `^[a-zA-Z_][a-zA-Z0-9_]*$` or the
  literal `END`. Non-`END` targets must reference a known step.
- `when` (optional) — condition expression (Section "Conditions").
- `preserve_active_plan` (optional bool, default `false`) — see below.
  Invalid on transitions to `END`.

A transition without `when` is an unconditional fallback. No match → the run
fails with an illegal-transition incident.

### Conditions

Supported symbols: `DONE`, `NEW_PLAN_EXISTS`, `MAX_TURNS_REACHED`, with
operators `&&`, `||`, `!`, `(`, `)`. Anything else is a config error.

- `DONE` — the original user plan is complete after the current step.
- `NEW_PLAN_EXISTS` — true only when the current step created the generated
  candidate file at `NEW_PLAN_PATH`. It is a current-turn event; it can be
  `false` while an earlier repair plan remains active.
- `MAX_TURNS_REACHED` — true only on the last allowed turn.

### `preserve_active_plan`

When a non-`END` transition has `preserve_active_plan = true` and the turn
did not create a new plan, the current `ACTIVE_PLAN_PATH` is retained for the
next step. A newly created plan always wins. If the active plan is missing
from the execution checkout, the preserving transition fails before the next
harness call.

### Aliases: `extends` and `exclude`

- `extends = "base_workflow"` creates an alias workflow. Aliases:
  - cannot redefine `steps` (config error),
  - cannot set `retry_inconsistent_checkpoint_state`,
  - cannot extend another alias (only concrete bases),
  - inherit steps plus any exclusions; may override `team`, `setup`,
    `teardown`, `main_branch`, `merge_prompt`.
  - Cycles are detected at config load.
- `exclude = ["step_name"]` removes steps from the executable graph while
  keeping them visible in `aflow show` and the live banner (gray).
  - Unknown step names are config errors; duplicate names are rejected.
  - After exclusions there must be at least one executable step; the first
    remaining step becomes the first step.
  - Alias exclusions are applied after inheritance (order-preserving union).

### Per-workflow overrides

- `retry_inconsistent_checkpoint_state` overrides the global `[aflow]` value.
- `team` sets the workflow's baseline team (CLI `--team` overrides per run).

## 4. Harnesses and Profiles

Eight adapters. Profiles map a harness name to `model`/`effort`. The engine
requires provider CLIs to be installed and authenticated; it does not manage
auth.

| Harness | CLI invocation shape | Effort | Session driver |
|---------|----------------------|--------|----------------|
| `codex` | `codex exec --dangerously-bypass-approvals-and-sandbox -C <repo> [--model M] [-c model_reasoning_effort='E'] -` | Yes | Yes (probed from `--help` output) |
| `claude` | `claude -p --permission-mode bypassPermissions --dangerously-skip-permissions` | Yes | No |
| `copilot` | `copilot -p ... -s --allow-all --no-ask-user` | Yes | No |
| `gemini` | `gemini --prompt ... --approval-mode yolo --sandbox=false` | No | No |
| `kiro` | `kiro-cli chat --no-interactive --trust-all-tools` | No | No |
| `opencode` | `opencode run --format default --dir <repo>` | No | No |
| `pi` | `pi --print --tools read,bash,edit,write,grep,find,ls` | Yes | No |
| `reasonix` | `reasonix run --dir <repo> [--model M] [--effort E]` | Yes | Yes (ACP negotiate) |

Non-session harnesses get a fresh subprocess per turn. Codex and Reasonix can
additionally drive owned interactive sessions (Section 6).

Environment preflight: before every real launch, AFlow resolves the adapter
invocation and runs a bounded local preflight (executable resolution plus
adapter-specific checks, e.g. Reasonix `doctor --json` / bwrap). Blocked
launches fail with `harness_environment_preflight` or
`harness_executable_missing` and never create a synthetic turn.

## 5. Session Capabilities

`SessionCapabilities` is a frozen dataclass with six boolean flags
(`aflow/harnesses/session.py`):

| Flag | Gates |
|------|-------|
| `session_identity` | Structured session output with an invocation-specific session id is available. |
| `followup_turn` | The harness can continue an existing session. |
| `resume_with_model` | The harness can resume an exact session id with a model override. |
| `mid_turn_steer` | Concurrent mid-turn steering is available (currently never advertised). |
| `read_only_teardown` | Provider-enforced read-only teardown/handover is available. |
| `idempotent_turn_start` | Idempotency keys for turn start are supported. |

Probing:

- **Codex**: `probe_codex_capabilities(exec_help, resume_help)` inspects the
  installed binary's `--help` output — `--json` ⇒ `session_identity`;
  `resume` + `[SESSION_ID]` ⇒ `followup_turn`; plus `--model` in resume help
  ⇒ `resume_with_model`. `idempotent_turn_start` is always false.
- **Reasonix**: negotiated at runtime from the ACP `initialize` handshake
  (`agentCapabilities`/`capabilities.methods`): `session/new`/`session/open`
  ⇒ `session_identity`; `session/prompt`/`session/send` ⇒ `followup_turn`;
  `session/update_config`/`session/set_config` ⇒ `resume_with_model`;
  `idempotency_key` method ⇒ `idempotent_turn_start`. `mid_turn_steer` and
  `read_only_teardown` are deliberately never advertised.
- All other harnesses expose `NO_SESSION_CAPABILITIES` (all false).

Session output contract: JSONL events on stdout, exactly one
invocation-specific session id, one optional provider operation id, and a
structured final response event type (`message.completed`,
`response.completed`, `item.completed`, `result`, `final`, `agent_message`).

## 6. Hotplug System (Live Role-Selector Overrides)

Hotplug is the run-local role-selector switching surface: an owner writes
`.aflow/runs/<run-id>/overrides.toml`, and the controller picks it up at the
next pre-turn boundary, creating a durable hotplug transaction.

### Override file grammar

The complete supported grammar (`overrides.toml`), all keys optional but at
least one required:

```toml
next_step = "implement_plan"
team = "strong"
max_turns = 20
roles = { worker = "codex.high" }   # run-local role-selector hotplug
notes = ["Re-run the focused regression before broader tests."]
```

- `next_step` must name an executable step in the frozen workflow.
- `team` must be configured and able to resolve the target step's role.
- `max_turns` must be positive and not below completed turns.
- `roles` maps role names to fully qualified `harness.profile` selectors. It
  becomes authoritative for the next worker turn and drives hotplug
  transactions (below). Selectors are validated against the frozen config.
- `notes` is an array of non-empty strings appended only to the next worker
  prompt.

The file is read once per pre-turn boundary, never while a harness runs; the
accepted digest is durable and never applied twice. Invalid content leaves
the run `waiting_for_valid_override` without launching. There is no live
config reload or `run.json` direct edit — the override file is the only
supported future-turn control surface. When `roles` is changed, the controller
creates a hotplug transaction instead of a plain override.

### Transaction model

A hotplug transaction (`HotplugTransactionV1`, schema version 1) records:
`transaction_id` (`<run-id>:hotplug-<NNN>:<digest>`), source/target
role, selector, harness, profile, model display, `source_turn_number`,
`source_session` (exact `HarnessSessionRefV1` with status
`active|handed_over|closed`), `capability_path`, `stage`, bound
`artifact_paths`/`artifact_hashes` (SHA-256), `provider_operation_id`,
`idempotency_key`, `failure`, `remediation`, `created_at`.

`capability_path` is decided when the transaction is accepted:

- `native_resume` — source and target harness are the same; the target turn
  resumes the exact active source session (requires `resume_with_model` or an
  owned executor; otherwise the boundary fails).
- `handover_required` — cross-harness; a bounded read-only source brief is
  produced before the target starts (see below).

### Stages

The durable stage machine (`HOTPLUG_STAGES`, schema-validated):

1. `accepted` — request consumed at the boundary after the source turn
   finalized; target selectors become authoritative for the next worker turn.
2. `target_preflighted` — target environment proven usable.
3. `quiescing` — source side is being quieted.
4. `source_finalized` — source session/state finalized.
5. `handover_starting` — handover brief generation started.
6. `handover_ready` — handover artifacts written and hash-bound.
7. `target_starting` — target invocation starting.
8. `applied` — terminal success; target turn finalized, transaction closed.
9. `failed` — terminal failure; source selector/session retained.
10. `waiting_for_hotplug_recovery` — fail-closed resume state when a run
    stops in an ambiguous stage (`quiescing`, `handover_starting`,
    `target_starting`).

In the live engine the observable transitions are: `accepted` →
(`handover_ready` for cross-harness) → `applied` or `failed`. The intermediate
stages exist for durable resume validation: terminal = `applied`/`failed`;
ambiguous = `quiescing`/`handover_starting`/`target_starting`.

### Cross-harness handover

`handover_required` paths require the source driver to advertise
`followup_turn` and `read_only_teardown` and an exact active source session.
Flow:

1. Target preflight must pass before any source-side side effect.
2. A Full manager context is built (or the driver's `build_full_context`),
   projected into a bounded `HandoverContextV1` (checkpoint, scope, completed
   work, implementation attempts, rejection summary, run summary, workspace
   facts, artifact refs, `full_context_sha256`). No manager authority,
   prompts, or secrets.
3. The source session produces a handover brief via a read-only prompt with
   exactly eight required Markdown sections: `Objective And Checkpoint`,
   `Completed Work`, `Changed Files`, `Verification`,
   `Unfinished Work And Exact Next Action`, `Decisions And Assumptions`,
   `Hazards And Dirty State`, `Relevant Paths And Artifacts`. Output must be
   ≤ 8 KiB, contain no placeholder bodies, and must not request hidden
   reasoning.
4. The workspace/plan fingerprint is compared before/after; any change fails
   the handover.
5. Three artifacts are written and hash-bound under
   `.aflow/runs/<run-id>/hotplugs/hotplug-<NNN>/`:
   `handover.md`, `context.json`, `full-context.json` (bounded). The
   transaction moves to `handover_ready`; the target turn receives the brief
   appended to its prompt.

### Apply and failure semantics

- On target turn success, the transaction records the target's
  `provider_operation_id`/`idempotency_key` and moves to `applied`; the
  transaction is removed from live state and appended to `hotplug_history`
  (bounded to the last 16).
- On any failure before terminal state, `_fail_hotplug_target` reverts the
  role selector to the source, restores the source session to `active`,
  records `failed` with the failure reason, and keeps the source as the live
  worker.
- An override injected before the first worker turn (no live source session)
  is recorded as `applied` without creating a live transaction boundary.

### Resume reconciliation

On resume, a pending transaction is reconciled before any harness starts:

- Selectors must still resolve to the recorded harness/profile (config drift
  fails closed).
- Ambiguous stages fail closed to `waiting_for_hotplug_recovery` with a
  remediation message; the run stops with `status =
  waiting_for_hotplug_recovery`.
- If the stage is ambiguous but a `provider_operation_id` exists and the
  session driver supports `reconcile_provider_operation`, evidence is
  reconciled: a valid `SessionResult` recovers the target session and marks
  the transaction `applied`; `"not_started"` evidence retries once
  (`handover_ready` if artifacts exist, else `accepted`).
- Handover artifacts are copied hash-bound into the successor run
  (`copy_hotplug_resume_artifacts`).

Artifact safety: all hotplug paths must be run-relative, must not escape the
run directory, and must not traverse symlinks; writes are atomic
(tmp + fsync + rename) and refuse to overwrite.

### Observability

- Events: `hotplug_requested`, `hotplug_stage_changed`, `hotplug_applied`,
  `hotplug_failed` (ExecutionEventType).
- The live banner shows `hotplug <stage>: <source_selector> -> <target_selector>
  (<capability>) | active <selector>` while a transaction is live.
- `aflow analyze` reports a hotplug summary: current/pending transactions,
  normalized history (source/target selector, capability_path, stage,
  artifact paths/hashes presence, provider operation presence, remediation),
  and active session count.

## 7. Manager Supervision

With `[manager].enabled = true`, a read-only manager runs after every
finalized workflow turn and before applying the controller's proposed
transition, recovery, retry, or `END`. Manager calls are not turns: they do
not advance `turns_completed`, consume `max_turns`, create checkpoint
commits, or trigger same-step caps.

Levels:

- **Lite** — receives semantic results, plan snapshots, structured state,
  controller/routing counters, compact history, and bounded diagnostics.
  Never plan prose, prompts, or raw trace bodies.
- **Full** — Lite context plus the complete active-plan Markdown. Chosen
  directly after: consecutive unchanged executions of the same step
  (≥ `full_after_stalled_turns`), the second reviewer rejection within one
  open original-checkpoint scope, explicit stop markers, invalid plans,
  ambiguous failures, illegal transitions, same-step cap, max-turns, merge
  failure, and scope pressure. Lite can escalate once to Full at the same
  boundary.

Allowed actions: accept proposal, retry, `upgrade_next_implementation`
(one `upgrade_to` hop from the most recently reviewed worker team),
`switch_to_backup_and_retry`, escalate Lite→Full, stop, and (Full only)
`repartition_current_checkpoint`. The manager cannot choose arbitrary
nodes/roles/teams/selectors. Invalid Lite output gets one Full attempt;
invalid Full output stops with a deterministic report. Repository/plan/git/
config/run-control mutation by the manager is detected and stops the run.

Notes discipline: at most 8 advisory notes of ≤ 1000 chars each; injected once
into the next step's prompt and cleared when that step durably starts.
Manager-selected upgrades are persisted, one-hop, and consumed only by the
next eligible implementation invocation. Both survive resume before
consumption and never replay afterward.

Review scopes: worker attempts group under a stable original-checkpoint
scope (opened at the scope's opening turn, carries an immutable envelope).
Approval, original-checkpoint advance, or completion closes the scope;
repeated rejection keeps it open so upgrade edges advance one at a time.
Rejection counters count only turns at/after the scope opening turn.

## 8. Error Recovery (Harness Failures)

Runs after a harness returns, before normal transition handling, and only
when the turn did not advance the plan snapshot.

1. Deterministic `[error_handling.harness_error_recovery]` rules checked in
   order; first match wins. Actions:
   - `retry_same_team_after_delay` — retry same step/selector after
     `delay_seconds`.
   - `switch_to_backup_team_and_retry` — use `teams.<active>.backup_team`
     for the retry turn only.
   - `fail_immediately` — fail the run without another recovery turn.
2. If no rule matches and the process exited non-zero:
   - manager-enabled runs route the ambiguous boundary to Full supervision;
   - manager-disabled runs ask the configured `[aflow].team_lead` role for a
     strict JSON decision via the `aflow-harness-recovery-lead` contract
     (`action`, `delay_seconds`, `reason`, `suggested_keywords`,
     `suggested_action`).

The run fails when recovery exceeds `max_consecutive_recoveries` or a
backup-team chain is invalid. Recovery runs on a separate retry turn.
`team_lead_skill` is parsed but the handoff always uses `[aflow].team_lead`.

## 9. Repartition (Automatic Checkpoint Splitting)

Full-manager-driven splitting of an oversized active checkpoint, available
only for a live scope with a validated immutable envelope (captured before
the first worker starts; legacy scopes are never backfilled).

Flow when Full returns `repartition_current_checkpoint`:

1. Capture the exact boundary source and corrective evidence.
2. Ask the read-only Full role for a strict proposal (via
   `aflow-repartition-checkpoint`).
3. Render and mechanically validate an unchecked candidate.
4. Ask Full again for an independent semantic verdict (changed meaning,
   weakened acceptance, new business decisions, contradictory guidance, lost
   corrective evidence, unsupported review-readiness claims all reject).
5. Apply an accepted candidate via atomic per-copy replacement plus a durable
   multi-copy transaction (`PendingRepartitionV1` stages:
   `semantically_validated` → `execution_plan_applied` → `primary_plan_applied`
   → `applied`).

Only one bounded correction is allowed after a mechanical or semantic
rejection; a second rejected candidate stops with workspace and evidence
preserved. Artifacts live under
`.aflow/runs/<run-id>/manager/decision-NNN/repartition/attempt-NN/` (envelope,
source plan, evidence, prompts, stdout/stderr, proposal, candidate,
mechanical result, semantic verdict, hashes, result).

Resume reconciles partial plan-copy transactions before any harness starts,
never replays an applied copy, and validates carried artifact hashes. The
first child keeps the parent scope; later children open normally.

## 10. Scope Pressure

`AFLOW_SCOPE_PRESSURE: <reason>` in a finalized turn's stdout/stderr is a
**nonterminal** structural signal (same fence/placeholder and stop-marker
precedence rules as `AFLOW_STOP`; `<reason>` placeholders and fenced examples
are ignored; stdout wins over stderr).

Semantics:

- Forces Full evaluation at the manager boundary (`_manager_level_for_boundary`
  ⇒ `full`), but Full may still continue, upgrade, repartition, or stop.
- Not a numeric size gate; file/line counts are evidence, not gates.
- Requires an enabled manager and a valid immutable envelope; manager-disabled
  workflows fail clearly on real scope pressure.
- A real `AFLOW_STOP` is terminal and defeats a simultaneous scope-pressure
  marker.
- The reason is persisted (`scope_pressure_reason`), surfaced in manager
  context, analysis output, and the banner, and recorded in repartition
  records.

## 11. Run Artifacts

`.aflow/runs/<run-id>/`:

- `run.json` — schema-versioned controller snapshot: status, workflow name,
  current step, turns completed, `end_reason`, `failure_reason`,
  `merge_failure_reason`, startup recovery fields, retry summary,
  lifecycle context (`execution_repo_root`, `worktree_path`, `main_branch`,
  `feature_branch`), plan paths (`original_plan_path`, `active_plan_path`,
  `new_plan_path`), frozen config identity, manager history, pending
  override, hotplug state, scope/repartition state. Written atomically.
- `turns/turn-NNN/` — per turn: `result.json` (step name, selector, status,
  return code, error, `end_reason`, chosen transition, evaluated conditions,
  retry metadata, `review_rejection` object, plan snapshots before/after),
  `stdout.txt`, `stderr.txt`, `system-prompt.txt`, `user-prompt.txt`,
  `effective-prompt.txt`, `env.json`, plus `transport.stdout` for owned
  sessions.
- `manager/decision-NNN/` — manager calls: `boundary.json` inputs, exact
  context, prompts, stdout, stderr, parsed result; `note-authority-correction/`
  when a note correction ran; `repartition/attempt-NN/` for repartition
  evidence.
- `manager-report.md` — self-contained report for manager stop, protocol or
  mutation failure, explicit stop, or other terminal incident after run
  creation.
- `hotplugs/hotplug-<NNN>/` — `handover.md`, `context.json`,
  `full-context.json` (hash-bound).
- `issues.md` — accumulated issues.
- `overrides.toml` — owner-written control surface (Section 6).

Observer events (`ExecutionEventType`): `run_started`, `status_changed`,
`turn_started`, `turn_finished`, `manager_started`, `manager_decided`,
`checkpoint_repartitioned`, `question_required`, `run_completed`,
`run_failed`, `hotplug_requested`, `hotplug_stage_changed`, `hotplug_applied`,
`hotplug_failed`.

Turn statuses to know: `starting` (interrupted/abandoned if no final result),
`retry-scheduled` (clean exit but inconsistent checkpoint state),
`plan-invalid`, `completed`. Run statuses: `running`, `completed`, `failed`,
`waiting_for_valid_override`, `waiting_for_hotplug_recovery`.

`end_reason` values: `already_complete`, `done`, `max_turns_reached`,
`transition_end`.

## 12. CLI Surface

```
aflow run [--plan/-p PLAN] [--workflow/-w WF] [--max-turns/-mt N]
          [--team/-t TEAM] [--start-step/-ss STEP] [--resume [RUN_ID]]
          [--resume-reset-scope] [positionals...] [-- EXTRA...]
aflow install-skills [DESTINATION] [--yes] [--include-optional] [--only SKILL]
aflow analyze [RUN_ID] [--all] [--repo-root REPO] [--limit N]
              [--include-noise] [--manager-context lite|full] [--turn N]
aflow show [WORKFLOW_NAME]
```

`run` details:

- Positionals: 1 positional = plan file; 2+ = resolved by checking which token
  is an existing file vs. a configured workflow name (ambiguity exits with a
  clear error). After `--`, everything is extra instructions appended to the
  rendered step prompt.
- `--max-turns` overrides `[aflow].max_turns` for the invocation.
- `--team` overrides the workflow team for the run.
- `--start-step` accepts a step name or 1-based index; rejected if the plan is
  already complete.
- `--resume` (optional RUN_ID, default `AUTO`) forces resume; plain `aflow
  run` may offer an interactive auto-resume. Resolution order:
  `.aflow/last_run_ids/<shell-id>`, `AFLOW_LAST_RUN_ID`, `.aflow/last_run_id`.
  Resumable runs: worktree lifecycle, status `failed`/`running`, plan not
  complete, no merge teardown entered, invocation identity matches.
- `--resume-reset-scope` requires an explicit RUN_ID; reuses worktree +
  manager history but restarts from the invocation's original plan with a
  fresh checkpoint scope.
- Startup prompts (interactive-only, fail clearly without TTYs): start-step
  selection for partly complete plans, stale `Pre-Handoff Base HEAD` refresh,
  inconsistent-checkpoint-state recovery, dirty-worktree confirmation,
  implicit auto-resume acceptance.

`install-skills` details: destination omitted → auto-detect supported harness
CLIs on PATH and install into each harness's global skill directory
(`codex`/`copilot`/`gemini`/`pi` → `~/.agents/skills`, `kiro` →
`~/.kiro/skills`, `opencode` → `~/.config/opencode/skills`, `claude` →
`~/.claude/skills`). `--yes` skips the confirmation prompt (required for
non-interactive stdin). `--include-optional` adds optional skills;
`--only SKILL` installs exactly the named skill(s) and cannot be combined
with `--include-optional`.

`analyze` details:

- `RUN_ID` optional; single-run resolution follows the resume lookup order;
  otherwise latest substantive run.
- `--all` corpus mode over multiple runs.
- `--limit N` caps corpus run directories (default 20).
- `--include-noise` keeps low-signal test-noise runs (filtered by default;
  noise = `workflow_name == "other"`, `turns_completed == 0`,
  `end_reason == "already_complete"`).
- `--manager-context lite|full` rebuilds the read-only versioned manager
  context for a finalized turn (single-run mode only, never invokes a
  manager); `--turn N` selects the turn (default latest).
- Detected signals: blocked review preconditions, missing original plans,
  dirty merge verification, plan-invalid, retry-scheduled, interrupted runs,
  alternating no-progress loops, reviewer non-convergence, harness recovery
  presence/actions, hotplug summaries, environment preflight failures.

`show` details: no argument → shared roles/teams section plus every workflow
in config order; with a workflow name → that workflow plus applicable
roles/teams. `exclude`d steps stay visible in gray (declared graph).

Note: `--dry-run` and `aflow config` subcommands do not exist in this engine.

## 13. Skill System

`aflow install-skills` copies bundled skills from `aflow/bundled_skills/`
into harness skill directories.

Default skills (13):

- `aflow-plan` — create a checkpoint handoff plan.
- `aflow-execute-plan` — execute an entire plan autonomously.
- `aflow-execute-checkpoint` — execute exactly one checkpoint.
- `aflow-review-squash` — review, approve and squash, or create a fix plan.
- `aflow-review-checkpoint` — review one checkpoint.
- `aflow-review-final` — final no-squash review.
- `aflow-merge` — local-only merge handoff.
- `aflow-init-repo` — pre-lifecycle bootstrap for empty repositories.
- `aflow-harness-recovery-lead` — team-lead fallback recovery decisions.
- `aflow-manager` — read-only Lite/Full interstep supervision.
- `aflow-repartition-checkpoint` — strict Full proposal + semantic validation.
- `aflow-guard-development-run` — same-task heartbeat supervision.
- `material-code-review` — material-defect review discipline.

Optional skill (1):

- `aflow-assistant` — setup help, AFlow concepts, evidence-first run
  debugging (this skill).

Skill names are frozen into run config for manager/repartition prompts, but
manager and repartition prompts also carry their complete JSON contracts
inline, so a missing static skill does not weaken protocol validation.

## 14. Plan Format

`aflow` reads a Markdown plan from disk and derives progress from checkpoint
headings plus unchecked task items inside each checkpoint.

```md
# Plan

### [ ] Checkpoint 1: Wire The CLI
- [ ] add the command entrypoint
- [ ] cover it with tests

### [x] Checkpoint 2: Update Docs
- [x] document the final behavior
```

Parser rules:

- Checkpoint headings match `^### [ ] Checkpoint ...` or `^### [x] Checkpoint
  ...` (the `[X]` uppercase spelling is tolerated). Sections must be ordered.
- Only task items under a checkpoint section count toward that checkpoint's
  remaining work.
- A checked checkpoint heading cannot contain unchecked task items — this is
  the "inconsistent checkpoint state" that triggers
  `retry_inconsistent_checkpoint_state` retries.
- If no checkpoint sections are found, the run fails before starting.
- Snapshots record: current checkpoint name/index, unchecked checkpoint
  count, current checkpoint unchecked step count, total checkpoint count.

Plan metadata: a `## Git Tracking` section (outside fenced blocks, at most
one live section) carries `Plan Branch`, `Pre-Handoff Base HEAD`, `Last
Reviewed Head`, and a review log. `Pre-Handoff Base HEAD` refresh is one of
the interactive startup prompts. Plans also get `Plan Branch:` lines rewritten
during lifecycle setup; the engine tracks `plans/in-progress/` as the durable
plan location and `plans/backups/` for original-plan backups (reused when
content matches, `_vNN` versions otherwise).

## 15. MCP Control Plane (aflowd)

The optional daemon control plane (`aflowd`, `aflow/daemon.py`) exposes the
same durable AFlow run state over authenticated REST, web UI, and an MCP
server. It is an additive transport over the engine; direct `aflow run`
workflows remain unchanged and are never guessed into daemon-owned state.

### Daemon and app config

`aflowd` CLI: `--repo-root` (default cwd), `--config` (required app TOML),
`--aflow-executable`, `--environment-file` (required, bearer-token
EnvironmentFile; the p100 deploy tooling additionally enforces mode 0600),
`--release-identity`, `--once`.

The app TOML carries `[control_plane]` with a `[[control_plane.projects]]`
allowlist (project id, repo root, config path, aflow executable, environment
file, release identity, environment). Every served
project must be allowlisted; requests cannot supply arbitrary roots,
executables, environment files, or plan locations. Writes are journaled under
durable idempotency scopes in `.aflow/launches/` and run as independent
`systemd-run` workflow units.

### MCP server

The stateless FastMCP server ("AFlow Control Plane", version 1,
`mask_error_details=True`) is mounted at `/mcp` (and `/mcp/`) on the FastAPI
app (`apps/aflow_app/server/src/aflow_app_server/mcp_adapter.py`,
`main.py`). Bearer authentication; credentials belong only in the client's
bearer-token environment variable — literal bearer tokens, `token=`/
`authorization=` patterns, query parameters, fragments, and userinfo in URLs
or tool arguments are rejected.

**Read tools** (read-only, idempotent annotations; bounded pages — `limit`
must be 1–1000, default 100 — plus bounded cursors):

- `get_capabilities` — versioned capabilities for every allowlisted project.
- `list_projects` — the configured project allowlist.
- `get_project_capabilities(project_id)` — one project's versioned capabilities.
- `list_plans(project_id, limit=100, cursor=None)` — bounded plan metadata.
- `list_runs(project_id, limit=100, cursor=None)` — bounded, versioned run status page.
- `get_run(project_id, run_id)` — one run's state.
- `get_run_events(project_id, run_id, ...)` — bounded ordered event tail.
- `get_run_context(project_id, run_id, ...)` — context snapshot.

**Write tools** (require client approval; all take an `idempotency_key`; a
replayed key returns the recorded effect, a reused key with different input is
rejected):

- `start_run(project_id, plan_path, idempotency_key, workflow_name=None,
  team=None, start_step=None, max_turns=None)` — reserve and start one
  daemon-owned workflow; when startup needs an answer the response carries
  the startup question and the run state becomes `awaiting_startup_answer`.
- `answer_startup(project_id, question_id, answer, idempotency_key)` — submit
  one authenticated answer for a pending startup question.
- `control_run(project_id, run_id, expected_revision, idempotency_key,
  max_turns=None, team=None, role_selectors=None, unsafe_changes=None)` —
  compare-and-swap control; `expected_revision` is required.
- `owner_stop(project_id, run_id, expected_revision, idempotency_key)` —
  explicit terminal owner stop (destructive; not a generic control flag).
- `resume_run(project_id, run_id, idempotency_key)` — explicit
  lineage-linked continuation with a new run id for a stopped run.

**Resources:**

- `AFlow project capabilities` (project_id)
- `AFlow run state` (project_id, run_id)
- `AFlow lite run context` (project_id, run_id)

**Stable public error codes** (ToolError/ResourceError messages):
`project_not_found`, `control_plane_unavailable`, `run_not_found`,
`idempotency_conflict`, `revision_conflict`, `restart_required`,
`operation_forbidden`, `operation_rejected`, `internal_error`.

### Client configuration

```toml
[mcp_servers.aflow_control_plane]
url = "http://<host>:8765/mcp"
required = false
bearer_token_env_var = "AFLOW_CONTROL_PLANE_TOKEN"
default_tools_approval_mode = "writes"

[mcp_servers.aflow_control_plane.tools.start_run]
approval_mode = "approve"
# ... same for answer_startup, control_run, owner_stop, resume_run
```

Validate a client config with
`python3 deploy/aflowd/validate-mcp-config.py ~/.codex/config.toml` (see the
example at `deploy/aflowd/aflow-control-plane.mcp.example.toml`).

### Semantics

- Failed or ambiguous daemon-owned units are reported `needs_attention` and
  are never auto-restarted; explicit `resume_run` creates one linked
  continuation.
- Legacy runs without the control-plane manifest are read-only and reported
  as legacy/interrupted.
- Loss of the client, MCP connection, or SSH transport has no lifecycle
  effect; daemon restarts do not stop workflow units.
- `aflow-guard-development-run` remains opt-in supervision for exact
  explicitly-guarded runs (typically direct-CLI workflows), not a second
  daemon controller or an automatic recovery loop for daemon-owned units.

## 16. Fast-Facts Summary

- Stop marker: `AFLOW_STOP: <reason>` — terminal, defeats scope pressure.
- Scope pressure: `AFLOW_SCOPE_PRESSURE: <reason>` — nonterminal, forces Full.
- Same-step cap: `max_same_step_turns` (default 5, `0` disables), applies to
  multi-step workflows only.
- Override file: `.aflow/runs/<run-id>/overrides.toml` — `next_step`, `team`,
  `max_turns`, `roles`, `notes`.
- Hotplug: `roles` override ⇒ transaction; `capability_path` =
  `native_resume` (same harness) or `handover_required` (cross-harness).
- Manager: Lite/Full; Full for stalls, second scoped rejection, stops,
  invalid plans, ambiguous failures, scope pressure.
- Recovery actions: `retry_same_team_after_delay`,
  `switch_to_backup_team_and_retry`, `fail_immediately`.
- Resume: new successor run dir; frozen config identity must match; hotplug
  and repartition transactions reconcile before any harness.
- MCP: stateless FastMCP server at `/mcp` on the aflowd control plane — 8 read
  tools, 5 write tools (idempotency-keyed, client approval), 3 resources;
  bearer token only via `AFLOW_CONTROL_PLANE_TOKEN` env var (Section 15).
