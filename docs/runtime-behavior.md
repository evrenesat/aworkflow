# Runtime Behavior

## How A Run Works

Each workflow step launches one fresh harness process.
Codex and Reasonix receive their combined system and user prompt through
standard input, so large Full-manager contexts and implementation prompts are
not constrained by the operating system's per-argument size limit. Reasonix
one-shot invocations are flags-only on argv (`reasonix run --dir <repo>
--model <model> [--effort <effort>]` and `--print` for final output); the exact
prompt is supplied as `stdin_text`, never as an argv element, so a prompt can
never fail `execve` with `E2BIG`. Durable prompt artifacts still record the
exact effective prompt, and owned Reasonix ACP sessions are unchanged.

At a high level:

1. `aflow` loads the selected workflow and reads the original plan file.
2. If the workflow has lifecycle setup, `aflow` inspects git state, optionally bootstraps an empty repo, runs lifecycle preflight, and creates the execution environment.
3. The run starts at the workflow's first declared step unless startup selected another step.
4. The engine renders prompts, resolves the step role through the selected team and global roles, and runs the harness CLI once.
5. After the harness returns, it re-reads the original plan, computes the proposed recovery or normal transition, and durably finalizes the turn artifacts.
6. When manager supervision is enabled, the manager accepts or changes that proposed control action before it is applied, including a proposed `END`.
7. The next matching `go` transition then chooses the next step or stops at `END`.
8. If teardown includes `merge`, `aflow` invokes a merge handoff through the configured `team_lead` role and verifies the result.

At run start, `aflow` prints the new run ID immediately. Resumed runs also show which prior run they came from.

## Fresh Review Plan Git Tracking

Before allocating a run ID, launch manifest, run directory, or start event,
AFlow backs up and validates the startup plan. A fresh review workflow whose
plan is pristine and has no live `## Git Tracking` section receives exactly one
minimal section immediately before its first live checkpoint. Normal
repositories record the verified current `HEAD`. Eligible empty repositories
temporarily receive an empty base, then record the exact verified bootstrap
commit and actual lifecycle branch before the first ordinary worker turn.

Insertion is limited to fresh plans with no checked checkpoint or task and no
orphan tracking fields. Started, resumed, recovery, ambiguous, malformed, and
no-HEAD/non-bootstrap inputs fail before durable run allocation and leave the
original plan unchanged. Existing sections retain automatic pristine base-HEAD
refresh behavior and are never duplicated or rebuilt. There is no interactive
base-refresh confirmation.

## Harness Environment Preflight

Before a real model-backed launch, AFlow resolves the adapter invocation and
runs a bounded local preflight with the same working directory and merged
environment that the process would receive. The common check resolves the
primary executable. An adapter may add a deterministic, read-only check; the
Reasonix adapter runs reasonix doctor --json and reports
reasonix_sandbox_bwrap_missing only when that diagnostic says
sandbox.bash = enforce and bwrap is not executable on the same PATH.

The durable classification is harness_environment_preflight. The other stable blocker is harness_executable_missing. A blocked invocation
writes status = failed, failure_kind = environment_preflight, and an
allow-listed environment_preflight object to run.json. It creates no
synthetic turn, manager decision, correction, recovery, or repartition attempt.
A zero-turn blocker leaves no turn-001; a later blocker preserves earlier
artifacts and pending routing state. The CLI reports the required executable
and fixed remediation, but AFlow does not install bubblewrap or any harness,
edit configuration, authenticate, or test network, quota, provider health, or
model availability.

After manual remediation, use aflow run --resume RUN_ID to retry the same
pending invocation. Ready results are not cached. A custom injected runner
without an explicit preflight probe remains ready by contract; tests and library
clients can pass a deterministic probe when they want the boundary exercised.
Process-creation races after a successful preflight still use the existing
126/127 launch-error normalization. The guardian remains the fallback for
legacy runs and failures outside the safe local preflight boundary.

## Manager Contexts and Evidence Budget

Manager contexts are versioned. Selectors below 4 rebuild historical schema
v1/v2 shapes exactly; new runtime boundaries use `context_schema_version = 4`
and produce reference-only schema-v3 contexts.

Before a live schema-v3 boundary, the controller captures the exact active
plan and current checkpoint into the run-local content-addressed evidence
store (`.aflow/runs/<run-id>/evidence/{plans,checkpoints}/<sha256>.md`);
equal active/original bytes share one artifact and unchanged bytes create no
new files on later decisions. Historical rebuilds never write evidence and
disclose content as unavailable when the exact bytes are not already stored.
The inline manager manifest carries repository-relative artifact paths,
SHA-256 hashes, byte sizes, and checkpoint line/byte ranges instead of plan
bodies, base64 evidence, or reviewer transcripts. Reviewer stdout is
referenced through its durable turn artifact.

- The exact UTF-8 inline user prompt targets 16 KiB (`MANAGER_INLINE_CONTEXT_TARGET_BYTES`)
  and is hard-limited to 40 KiB (`MANAGER_INLINE_CONTEXT_MAX_BYTES`) before any
  provider process starts. Exceeding the hard limit fails closed with a fixed
  error containing total bytes and per-top-level-field byte counts only.
- Bounded semantic fields (results, reasons, rejection summaries, diagnostic
  excerpts) are capped at 2,000 characters (`MANAGER_SUMMARY_MAX_CHARS`) with
  one shared deterministic truncation marker. Schema-v3 manager decisions are
  selected by decision number and workflow turns by turn number, with explicit
  fields for both domains. The context retains at most 12 manager decisions and
  12 mixed run-extract records. Omitted history is disclosed through measured
  `history_disclosure` descriptors containing the source run, artifact root,
  existing relative path pattern, omitted count, and merged numeric ranges.
  When the exact final wire prompt remains over the hard cap, optional history
  is removed oldest-first: duplicate manager rows in the run extract, older
  manager decisions, then older workflow turns. Current boundary authority and
  durable references are never replaced by those historical projections.
- Non-sensitive prompt metrics persist in the manager result:
  `system_prompt_bytes`, `user_prompt_bytes`, `argv_bytes`,
  `referenced_artifact_count`, `referenced_artifact_bytes`. Referenced
  artifact bytes are evidence the manager may read from disk; they are not
  model-input bytes and analysis output labels them as such.
- Managers need repository reads: an adapter must advertise the fail-closed
  `manager_workspace_read` capability (`HarnessAdapter.manager_workspace_read`)
  or the manager boundary fails before invocation. The manager system prompt
  instructs reading the referenced checkpoint artifact first, reading the
  referenced active/full plan only when needed for the legal decision, treating
  evidence as controller-declared (no searching for alternate plan files), and
  remaining read-only.

## Lifecycle and Worktrees

For worktree flows, run artifacts stay under the primary checkout, while normal steps execute inside the linked worktree. The original plan is copied into the worktree before prompts are rendered and synced back to the primary checkout after each turn.

Lifecycle preflight validates:

- branch name collisions
- worktree path collisions
- correct startup branch
- configured `main_branch` points to a local commit
- dirty-worktree constraints for the selected lifecycle

If the directory has no git repository, or a git repository with no commits, lifecycle workflows can auto-bootstrap it. The team lead agent initializes a local repository on `main_branch`, writes a `README.md` derived from the plan preamble, creates the initial commit, and then normal lifecycle preflight continues.

For already committed repositories, bootstrap is skipped.

## Plan Paths

Plan-path behavior is strict:

- `ORIGINAL_PLAN_PATH` is always the user-supplied plan file.
- `DONE` is computed from `ORIGINAL_PLAN_PATH`, not from a generated follow-up plan.
- `NEW_PLAN_PATH` is generated once per turn with the format `<stem>-cpNN-vNN.<suffix>`.
- `ACTIVE_PLAN_PATH` starts as the original plan path.
- After a successful turn, a newly written `NEW_PLAN_PATH` becomes active first.
  Otherwise, a selected transition with `preserve_active_plan = true` retains
  the current active path. All other transitions reset it to the original plan.
- `NEW_PLAN_EXISTS` remains a current-turn event and can be false while an
  earlier repair plan remains active.
- A preserving transition fails before the next harness call if its active plan
  is missing from the execution checkout.
- Turn `result.json` records the plan used to render that turn. Run-level
  `run.json` records the plan selected for the next turn.
- Before the workflow starts, `aflow` copies the original plan into `<repo_root>/plans/backups/`.
- If matching backup content already exists, `aflow` reuses it.
- If the same backup name already exists with different content, `aflow` writes the next `_vNN` file.
- For worktree workflows, the original plan can be untracked or gitignored under `plans/`; it is still copied into the linked worktree and synced back after each turn.
- Worktree prompts and existence checks use the execution-checkout copy while
  run metadata stores the corresponding primary-checkout logical path.
- Resume restores the saved active logical path and verifies its worktree copy
  before rendering the first resumed prompt.

In normal checkouts, ignore `.aflow/`, `.aflow/runs/`, and `plans/backups/` in git. Those are engine artifacts.

## Retries

`retry_inconsistent_checkpoint_state` controls automatic retries when a harness exits cleanly but leaves the plan invalid by marking a checkpoint heading complete while unchecked steps remain.

A scheduled retry:

- skips the pre-turn plan reload
- reuses the last valid snapshot and saved prompt context
- reuses the same `ACTIVE_PLAN_PATH`, `NEW_PLAN_PATH`, and resolved selector
- appends the exact parse error to the retry prompt
- counts toward `max_turns`

Startup recovery for an initial inconsistent plan uses the same retry machinery after an interactive confirmation.

## Harness Failure Recovery

Harness recovery runs after a harness returns and before normal transition handling. It is progress-gated: if the plan snapshot changed, recovery is skipped and normal transitions continue.

If no plan progress occurred:

1. Configured deterministic rules are checked first.
2. If no deterministic rule matches and the process exited non-zero, the configured team lead can be asked for a strict recovery decision through `aflow-harness-recovery-lead`.
3. Recovery actions run on a separate retry turn.

The run fails if recovery exceeds `max_consecutive_recoveries` or a backup-team chain is invalid.

## Disk-backed run state and boundary overrides

Every new `.aflow/runs/<run-id>/run.json` is a schema-version `2` controller
snapshot. It records the selected workflow, authoritative original plan path,
resolved configuration directory, frozen configuration fingerprint, complete
lifecycle identity, manager authority, hotplug authority, and active-scope
envelope references. AFlow writes this file through a flushed same-directory
temporary file and atomic replacement, so an interrupted update cannot expose
partial JSON. Older or malformed metadata remains readable for inspection but
is never migrated or resumed.

`run.json` is controller-owned output. To request a safe future-turn change,
create or edit exactly:

```text
.aflow/runs/<run-id>/overrides.toml
```

The complete supported grammar is:

```toml
next_step = "implement_plan"
team = "strong"
max_turns = 20
roles = { worker = "codex.sol-high" }
notes = ["Re-run the focused regression before broader tests."]
```

All keys are optional, but the file must contain at least one. `next_step` must
name an executable step in the frozen workflow. `team` must be configured and
able to resolve the target step's role. `max_turns` must be positive and cannot
be below the number of completed turns. `notes` is an array of non-empty
strings and is appended only to the next worker prompt.
`roles` maps role names to fully qualified `harness.profile` selectors and is
the run-local role-selector hotplug surface: it overrides the frozen role
routing for the next worker turn (validated against the frozen config) and
creates a durable hotplug transaction instead of a plain override. Same-harness
switches resume the exact active source session (`native_resume`); cross-harness
switches require a bounded read-only handover brief before the target starts
(`handover_required`). See the Live Role-Selector Hotplug section below.

AFlow reads this file once at the pre-turn boundary, never while a harness is
running. It retains the exact source and its hash in the controller record,
records acceptance or rejection atomically before routing or launch, and never
deletes or rewrites the file. Broad status and analysis output redact the source
and note contents. An unchanged
accepted digest is not applied twice; editing the file creates a new request.
Invalid TOML, unknown keys, incompatible routing, and invalid limits leave the
run in `waiting_for_valid_override` without launching another harness. Correct
the same file and resume the recorded run id.

Resume always creates a new durable successor under
`.aflow/runs/<successor-run-id>/` and records `resumed_from_run_id`; it does not
reuse or edit the predecessor's `run.json`. Before the successor's first harness
launch, AFlow inspects only the explicitly selected predecessor:

```text
.aflow/runs/<predecessor-run-id>/overrides.toml
```

There is no latest-run or modification-time scan. With no persisted result, an
absent file means normal resume and a present file becomes the first-boundary
request, including malformed content that must be rejected. An accepted but
unapplied result uses its already-persisted values once. For an applied result,
an unchanged digest is consumed and never replays `next_step`, `team`,
`max_turns`, or `notes`; changed content is one new request.

A rejected selected-predecessor request remains launch-blocking. Present content
is revalidated, so a correction can proceed, but a missing or unreadable
required file persists `waiting_for_valid_override` with a corrective message
and launches no harness. Correct the `overrides.toml` in the exact run you
select, then resume that same run id with its recorded invocation identity.
Deleting the file is not cancellation. A rejected resume attempt is itself a
new successor generation; if you select that successor for another resume, its
own run directory is the predecessor source.

Once a predecessor request is accepted and applied, cross-run ownership is
released. Later boundaries read only the successor's
`.aflow/runs/<successor-run-id>/overrides.toml`. The accepted digest/result stays
durable to prevent replay across further resume generations. `team` becomes the
successor's effective baseline, `max_turns` remains the effective limit,
`next_step` affects only the applying boundary, and `notes` reach only the next
worker prompt.

Protected state has no override syntax. For example, this is rejected because
`active_turn` is not a supported key:

```toml
active_turn = 0
```

Active/completed turn history, plan lineage, lifecycle/worktree ownership,
manager decisions, the workflow graph, and configuration files cannot be
changed through this surface. There is no live config reload, file watcher,
daemon, database, or supported direct-edit workflow for `run.json`.

## Live Role-Selector Hotplug

A `roles` override in `overrides.toml` (or a manager-selected upgrade that
changes the worker selector) creates a durable hotplug transaction instead of
a plain override. Hotplug is the run-local role-selector switching surface: it
changes which harness/profile selector runs the next worker turn while the run
stays alive.

### Session capabilities

Only session-capable harnesses participate in hotplug. The engine probes each
adapter's `SessionCapabilities` (six flags: `session_identity`,
`followup_turn`, `resume_with_model`, `mid_turn_steer`, `read_only_teardown`,
`idempotent_turn_start`):

- `codex` capabilities are probed from the installed binary's `--help`
  surface (`--json` support, `resume [SESSION_ID]`, and `--model` in resume
  help).
- `reasonix` capabilities are negotiated at runtime from the ACP
  `initialize` handshake (`session/new`, `session/prompt`,
  `session/update_config`, `idempotency_key`, ...).
- All other adapters advertise no session capabilities and always start a
  fresh subprocess.

Every owned Reasonix ACP turn is configured before its first model prompt.
After `session/new` or exact `session/resume`, AFlow requires the complete
`configOptions` state, verifies that the exact select option
`tool_approval` advertises `yolo`, and sends `tool_approval=yolo` before any
requested model and effort selections. Every `session/set_config_option`
response must acknowledge the complete current state; AFlow rechecks all
previous selections after each dependent update so model or effort cannot
silently reset approval. Missing, malformed, rejected, or unacknowledged state
closes the owned process and fails before `session/prompt`. The ordinary durable
turn/run failure boundary records that negotiation failure once without retry.

### Transaction and capability path

Each transaction records source/target role, selector, harness, profile,
model display, the exact source session, `capability_path`, a stage, and
SHA-256-bound artifacts. `capability_path` is decided when the override is
accepted:

- `native_resume` — source and target harness are the same. The target turn
  resumes the exact active source session; this requires a session driver
  with exact resume (`resume_with_model` or an owned executor), otherwise the
  boundary fails closed.
- `handover_required` — cross-harness switch. The target environment is
  preflighted first, then the source session produces a bounded read-only
  handover brief (≤ 8 KiB, eight required Markdown sections, no hidden
  reasoning, workspace fingerprint unchanged). The brief, its bounded context
  projection, and a bounded full-context snapshot are written hash-bound under
  `.aflow/runs/<run-id>/hotplugs/hotplug-<NNN>/` (`handover.md`,
  `context.json`, `full-context.json`) and appended to the target turn's
  prompt.

### Stages

The durable stage machine is: `accepted` → `target_preflighted` → `quiescing`
→ `source_finalized` → `handover_starting` → `handover_ready` →
`target_starting` → `applied`, with `failed` and
`waiting_for_hotplug_recovery` as terminal/fail-closed exits. In the live
engine the observable transitions are `accepted` → (`handover_ready` for
cross-harness) → `applied` or `failed`; the intermediate stages exist for
durable resume validation. Terminal stages are `applied` and `failed`;
`quiescing`, `handover_starting`, and `target_starting` are ambiguous and fail
closed on resume to `waiting_for_hotplug_recovery` with a remediation message
(the run stops with `status = waiting_for_hotplug_recovery` until the
provider operation is reconciled or the owner intervenes).

On target success the transaction records the target's provider operation id
and idempotency key and moves to `applied`. On failure the controller reverts
the selector to the source, restores the source session to `active`, records
`failed` with the failure reason, and keeps the source worker live. History is
bounded to the last 16 transactions. Resume reconciles a pending transaction
before any harness starts: recorded selectors must still resolve to the same
harness/profile, handover artifacts are copied hash-bound into the successor
run, and a recoverable ambiguous operation is reconciled through the session
driver's `reconcile_provider_operation` when available. All hotplug artifact
writes are atomic and refuse to overwrite; paths must stay inside the run
directory.

Hotplug events (`hotplug_requested`, `hotplug_stage_changed`,
`hotplug_applied`, `hotplug_failed`) are emitted to observers. Status records
include `status=... hotplug <stage>: <source_selector> -> <target_selector>
(<capability>) | active <selector>` while a transaction is live, and
`aflow analyze` reports
the current/pending transactions, normalized history, capability paths, and
active session count.
## Frozen run configuration snapshots

Every durably reserved run captures the complete effective workflow pair under
`.aflow/runs/<run_id>/config/` before startup questions or worker launch. The
snapshot records its origin paths and the canonical fingerprint in a versioned
`snapshot.json`; schema-defined relative paths (such as `[aflow] worktree_root`)
are resolved against the original configuration location before serialization,
while prompt strings and repository-relative plan paths are never rewritten.
Startup answers, worker launch, retries, resume, and run inspection all read
the snapshot, so later global configuration edits affect only new runs.
Snapshot creation shares one configuration lock with global saves, so a launch
sees either the old pair or the new pair, never a mixture; a failed or
fingerprint-mismatched snapshot fails the launch before any worker starts.
Legacy runs without a snapshot keep their recorded identity checks, and a
missing or damaged snapshot makes resume unavailable with an explanation
instead of silently substituting current configuration.

## UI process lifecycle and persistent units

`aflow ui` serves the web app in the foreground by default; `--daemon`
detaches with a per-user ownership record, lock, bounded log, and readiness
file under `~/.config/aflow/ui/`. `--stop` verifies the recorded
process-birth identity before signalling and never touches workflow units.
Workflow units launched by the UI run through a portable persistent unit
adapter (Linux and macOS): each unit is a detached private `aflow ui-worker`
wrapper that owns its workflow subprocess group and writes bounded
identity/exit receipts under the run's durable `units/` directory
(`start.json`, `child.json`, `exit.json`, `error.json`, plus `stopped.json`
after an explicit stop). Observation and signalling always require matching
process-birth identity, never PID existence alone, so stale or reused records
become observable ownership errors instead of duplicate launches or unrelated
signals. A returning UI reconciles the exact receipts: it can display,
monitor, and explicitly stop a pre-existing worker, and a workflow that
finished while the UI was absent reports its real terminal result.
`shutdown` in this adapter is a documented no-op — UI shutdown never signals
workflow groups. The systemd unit manager remains the production-deployment
adapter and the local-daemon subprocess adapter is unchanged.

## Daemon control plane and direct CLI

The lightweight local daemon starts with `aflow daemon start --foreground`.
It owns one repository and exposes the shared 13-tool MCP registry over stdio;
closing MCP input stops the daemon. Optional HTTP runs on `127.0.0.1` only and
may be detached. `aflow daemon status` verifies the pidfile's process-birth
identity and reports only direct `daemon-worker` children for the verified
repository, using Linux procfs or one bounded portable process-table snapshot.
It rechecks the daemon's process-birth identity after worker inspection and
before printing success. If ownership inspection is unavailable or untrusted,
status is ambiguous and returns 2 rather than claiming zero workers; it does
not inspect every legacy run directory. `aflow daemon stop`, SIGINT, and
SIGTERM drain each child process group with SIGTERM followed by bounded SIGKILL
escalation. A malformed pidfile or reused PID is ambiguous and never
authorizes a signal.

The optional remote control plane is a separate, allowlisted deployment over
the same durable AFlow run state. `aflowd.service` uses systemd workflow units,
serves authenticated REST, React, and FastAPI `/mcp`, and survives client
disconnects. The lightweight daemon does not serve those surfaces. FastAPI
bearer authorization remains header-only and server-owned even though both MCP
transports share the core registry.

A normal `aflow run ...` invocation remains the direct local/developer
interface and keeps its existing lifecycle, plan, and resume behavior. Do not
expect a direct-CLI run without a daemon launch manifest to become a mutable
control-plane run: it is deliberately observed as legacy/interrupted rather
than guessed into daemon ownership.

For a daemon-owned run, use the authenticated REST, UI, or MCP resume operation
after reconciliation reports `needs_attention`. Do not restart an exact
workflow unit to recover it: the daemon records the ambiguity and an explicit
resume creates a new linked continuation. An owner stop is terminal.

A recorded controller failure or interruption can also resume directly once
the worker and unit are confirmed inactive, even when the launch receipt still
says `launch_started`. A killed launch without controller terminal evidence
must first be reconciled to `needs_attention`.

REST and MCP fresh starts share one typed StartupRequest. Capability discovery
lists each workflow's declared, executable, and excluded steps, canonical first
step, default team, configured roles/teams, admitted selectors, and public
status vocabulary. A numeric start-step input resolves against executable
steps and only its canonical name is durable. Earlier executable steps appear
as skipped in status and run events.

A workflow change uses a fresh successor start with restarted_from_run_id.
The daemon accepts that lineage only after the same-project, same-caller source
has explicit owner-stop evidence and its exact unit is inactive. The successor
gets a new run ID and normal frozen-config validation. Resume remains strict
continuation of the saved invocation. Bounded extra instructions affect the
request digest and worker prompt but their text is omitted from control-plane
manifests, start records, events, and status.

For CLI resume, omitted extra instructions retain the predecessor's text.
`aflow run --resume RUN_ID -- replacement text` replaces it for the successor;
`aflow run --resume RUN_ID --` clears it. The predecessor and its prompt
artifacts remain unchanged. Use run-wide instructions for continuing rules,
not a recovery action that becomes stale after its checkpoint is approved.

`aflow-guard-development-run` remains opt-in supervision for the exact run a
user explicitly asks it to guard, particularly normal direct-CLI and legacy
workflows. It is not a second daemon controller, a release-health monitor, or
an automatic recovery loop for daemon-owned units. The deployed service and its
operator-defined rollback procedure own service availability; the daemon owns
its allowlisted workflow reconciliation.

## Loop Limits

`max_turns` is the hard turn cap. The runner checks the effective limit before
each launch, allowing a validated boundary override to raise or reduce a future
limit without changing an in-flight turn.

On the last allowed turn:

- `MAX_TURNS_REACHED` evaluates true.
- The selected transition is still recorded, including an `END` selected by
  `MAX_TURNS_REACHED`.
- If the original plan remains incomplete, the run fails with a max-turns
  error whether or not the transition selected `END`.
- A max-turn `END` is successful only when the post-turn original-plan
  snapshot is complete.

`max_same_step_turns` limits consecutive selection of the same step in multi-step workflows. The streak resets only after a different step actually executes. Single-step workflows are not affected.

## Interstep Manager Supervision

When the selected workflow has supervision enabled (`manager_enabled`), AFlow
runs a read-only manager after every finalized workflow turn and before
applying the controller's proposed transition, recovery, retry, or `END`.
A manager call is not a workflow turn:
it does not increase `turns_completed`, consume `max_turns`, create a checkpoint
commit, or trigger same-step caps.

The manager receives a reproducible, versioned context built from durable
artifacts. Live schema-v3 Lite and Full contexts carry bounded semantic
results, plan snapshots and structured state, controller/routing counters,
compact history, bounded diagnostics, and controller-declared evidence
references. Neither level receives active-plan or checkpoint content inline,
prompts, or raw trace bodies; Full may receive richer bounded scope and
rejection evidence. Full is chosen directly after consecutive unchanged
executions of the same workflow step, the second reviewer rejection within one
open original-checkpoint scope, explicit stop markers, invalid plans, and
ambiguous failures. Alternating implementation and review steps do not select
Full merely because their plan snapshots are unchanged. Lite can escalate once
to Full at the same boundary.

The strict manager protocol permits only controller actions: accept the
proposal, retry, select an eligible implementation upgrade, select an eligible
backup retry, escalate Lite to Full, or stop. It cannot choose arbitrary nodes,
roles, teams, selectors, or business logic. Invalid or unavailable Lite output
gets one Full attempt. Invalid or unavailable Full output stops the run with a
deterministic report instead of starting another manager loop. A manager that
changes repository, plan, git, config, or run-control state is detected and
also stops the run. The configured manager skill name and complete response
schema are included in every manager prompt. If a terminal workflow incident is
followed by an invalid manager response, the deterministic report retains the
workflow incident as its summary and records the manager protocol error
separately instead of masking the original failure.
The prompt exposes the advisory-note bounds: at most eight notes and 1,000
characters per note. If an otherwise valid non-terminal decision returns more
notes, the controller retains the first eight instead of spending a Full call
solely on surplus advice. Invalid note types, empty notes, overlong notes, and
notes forbidden for the selected action remain protocol errors.
One narrow exception preserves the same fail-closed authority: when an
otherwise valid response fails only because its notes select or reference a
plan, the same manager profile receives one compact correction request before
the logical decision is persisted. It must return the complete schema with
`schema_version`, `action`, `reason`, and `stop_report` unchanged; only
`next_step_notes` may differ. The controller rechecks the original target,
scope, eligible actions, and repository fingerprint. A failed correction is
terminal and cannot trigger another correction or Lite-to-Full escalation.

At the first reviewer rejection in an open implementation scope, Lite decides
from the cause: it can keep the same worker for a bounded repair, select one
available upgrade edge for a capability/convergence problem, or escalate
structural ambiguity. An available edge never mandates an upgrade. The second
rejection in that same scope selects Full directly with the immutable envelope,
all ordered rejections and attempts, the latest exact rejection, original and
active plan bodies, and prior decisions.
After the configured chain is exhausted, a manager `continue` into another
repair attempt retains the most recently reviewed worker team. It cannot fall
back to the baseline team while the original-checkpoint scope remains open.
Approval closes that scope, and only the next checkpoint starts on baseline.

Reasonix manager invocations request its native final-response mode with
`--print`, so strict manager JSON is not mixed with progress and metrics output.
Ordinary Reasonix worker and reviewer turns omit `--print` and retain their
diagnostic transcripts. The manager parser accepts either the bare JSON object
or one exact `json`-tagged Markdown fence containing that object because some
affordable models fence an otherwise valid final response. Prose, untagged or
multiple fences, multiple objects, malformed JSON, and protocol-invalid objects
remain errors.

`continue` with notes stores immutable notes for the selected next step. They
are injected once and cleared only when that step durably starts. A manager
selected `upgrade_to` override is likewise persisted, affects only the next
eligible implementation invocation, and is consumed only when that invocation
starts. Both pending states survive a resume before consumption and are not
replayed after consumption. See [Configuration](configuration.md#team-upgrade-routes)
for routing rules and the distinction from `backup_team`.

Worker attempts are grouped under a stable original-checkpoint review scope.
That scope survives worker completion, repair-plan creation or replacement,
repeated reviewer rejection, and resume. Manager context shows the actual teams
and selectors used in the scope and resolves at most one next upgrade edge from
the most recently reviewed worker. Reviewer approval, original-checkpoint
advance without a pending review, or plan completion closes the scope and clears
its unconsumed one-hop state. Historical attempts remain in run metadata, but
the next checkpoint opens a new scope and begins with the baseline worker.
Rejection counters are computed only from turns at or after that scope's durable
opening turn, so prior-checkpoint repairs cannot stop a new checkpoint before
its first review. A rejected review is counted from its own unchanged finalized
outcome or from its creation of a new focused follow-up plan. The latter also
counts when the reviewer reopens the original checkpoint, so the first
rejection exposes cause-based Lite choices and the second rejection selects
Full before another worker starts.
For newly finalized reviewer turns, the controller stores an explicit
`review_rejection` object (or JSON `null`) in `result.json` and retains the
scope history in `run.json`. It records bounded plain-text reviewer and repair
summaries plus artifact paths; legacy artifacts without that key retain the
older compatibility inference.
When a run resumes into a fresh run directory, the scope opening turn is rebased
to the new run and its prior rejection count is carried separately. Legacy
scopes written before that carried counter existed discard the old top-level
run-wide count rather than treating earlier-checkpoint rejections as current.

### Automatic checkpoint repartition

A new implementation scope captures an immutable envelope after startup-owned
plan synchronization and metadata refresh but before its first worker starts.
The envelope contains exact original-plan and current-checkpoint UTF-8 bytes,
hashes, spans, and deterministic source blocks. It is reused across repair
overlays, upgrades, and resume. Generated summaries are non-authoritative.
Legacy active scopes without an envelope are never backfilled and cannot use
automatic repartition.

`AFLOW_SCOPE_PRESSURE: <reason>` is a nonterminal structural signal. With an
enabled manager and a valid envelope it forces Full evaluation, but Full can
still continue, upgrade, repartition, or stop. It is not a numeric size gate.
A real `AFLOW_STOP` wins when both markers occur and remains terminal.
Manager-disabled workflows fail clearly on real scope pressure rather than
silently ignoring it.

When Full returns `repartition_current_checkpoint`, the controller:

1. captures the exact boundary source and corrective evidence;
2. asks the configured read-only Full role for a strict proposal;
3. renders and mechanically validates an unchecked candidate;
4. asks Full again for an independent semantic verdict; and
5. applies an accepted candidate through atomic per-copy replacement plus a
   durable multi-copy transaction.

Only one bounded correction is allowed after a mechanical or semantic
rejection. Malformed protocol output stops immediately; a second rejected
candidate stops with the workspace and all evidence preserved. Mechanical block
coverage cannot prove semantic equivalence, so the separate Full verdict rejects
changed meaning, weakened acceptance, new business decisions, contradictory
guidance, lost corrective evidence, or unsupported review-readiness claims.

Artifacts live under
`.aflow/runs/<run-id>/manager/decision-NNN/repartition/attempt-NN/` and include
the envelope, source plan, corrective evidence, proposal/validation prompts and
stdout/stderr, strict proposal, rendered candidate, mechanical result, semantic
verdict, hashes, and result. `run.json` records pending stage and applied
history; status and terminal reports link to these paths without embedding full
plan text.

Resume validates carried artifacts before pruning a source run, reconciles any
partial primary/worktree plan-copy transaction, and never starts a harness or
replays an applied copy until reconciliation is complete. The first child keeps
the parent scope and can retain the last worker only for an
`implement_current_partition` disposition. Review approval closes that scope;
the next inserted child opens normally with the baseline worker. The proposal
cannot route later children, approve code, commit work, or discard dirty
implementation.

Every successful finalized turn is written with `status: "completed"`, a single
authoritative finish timestamp, and its duration even while the overall
`run.json` status remains `running`. Manager stop reports are persisted, emitted
to observers, and raised only after the final status record is written; the
CLI then prints the complete report exactly once.

New manager boundaries carry an explicit context schema version and the
structured plan state captured at invocation time. Historical analysis therefore
does not depend on mutable plan files. Unversioned boundaries retain their
legacy context shape, including an explicit null implementation scope.

When manager supervision is disabled, AFlow retains its legacy transition and
deterministic recovery behavior. Deterministic harness-recovery matches remain
cheap proposed actions for an enabled manager to accept; only unmatched
ambiguous failures are routed to Full supervision.

Other early stop causes:

- the plan is already complete before any turn starts
- a step transitions to `END`
- no `go` transition matches
- the harness exits non-zero and recovery does not handle it
- the original plan becomes unreadable or invalid
- the same-step cap triggers

## Dirty Worktree

`aflow run` checks git working tree state before starting.

For worktree workflows, dirty files under `plans/` are allowed. Dirty files outside `plans/` require interactive confirmation, or fail in non-interactive mode.

For branch-only and no-lifecycle workflows, the worktree must be clean before starting unless interactive confirmation accepts the dirty state.

The interactive prompt accepts `y` or `yes`; any other input exits with code `1`.

## Status Output

While a workflow runs, `aflow` writes readable, append-only status blocks to
stderr. The sequence is deterministic: one run identity and plan header,
preparation, meaningful status changes, finalized-turn blocks, and one final
summary. Identical consecutive snapshots are deduplicated. There is no
background refresh, poll tick, heartbeat, cursor movement, alternate screen,
or keyboard capture; interactive startup questions remain interactive when
stdin and stdout are TTYs.

Each block uses a heading followed by indented labels and values. Depending on
the available state, the blocks show:

- run identity, workflow, team, original plan, and resume lineage
- preparation state, current step, checkpoint title and progress
- turn number, role, selector, harness/model, outcome, transition, and duration
- active/generated plan changes, manager/review/repartition diagnostics, and
  git changes since the workflow-start baseline
- full paths to turn stdout/stderr, issue, manager, review, and repartition
  artifacts

Display prose is bounded and control bytes are flattened, while dynamic
Unicode remains readable. Durable artifact references are never truncated. If
git is unavailable, git fields are omitted and the workflow still runs. A
failed or closed stderr disables further status output instead of failing the
run.

Only the first line of each block uses terminal emphasis. Bold is enabled when
the actual output stream reports `isatty()`, `TERM` is present and not
`dumb`, and `NO_COLOR` is absent. Otherwise the same content is emitted with
no escape bytes; `FORCE_COLOR` does not override this fallback. Styling does
not affect wrapping, deduplication, ordering, or the structured machine
interfaces. Consumers that need stable machine data should use observer events
or the durable JSON under `.aflow/runs/`, not parse the human stream.

The following synthetic fixtures show the before/after shape without provider
transcript content. The CP4 fixture first has no checkpoint metadata:

```text
AFlow run 20260909t003427z-ea0bff22
  Workflow: cumulative_delivery
  Team:     MusparkGLM
  Plan:
    /full/path/skills-manager-workflow-settings-cp4-restart-20260909.md

Preparing run
  Waiting for checkpoint metadata
```

After the same fixture identifies CP4, the running block is:

```text
12:00:00 UTC - Turn 1 of 40 - Running
  Checkpoint 4 of 10
  Manager instructions come from live skill Markdown
  Team:     MusparkGLM
  Worker: muse / muse-spark-1.3-contributor / high
  Step:     implement_plan
```

After CP4 finalizes and before CP5 starts, the cached completed-turn label and
the next running label remain separate:

```text
12:00:00 UTC - Turn 1 of 40 - Finished
  Checkpoint 4 of 10
  CP4 worker

12:00:00 UTC - Turn 2 of 40 - Running
  Checkpoint 5 of 10
  CP5 worker
```

The failure fixture keeps its bounded reason and complete stderr path while
omitting a stdout reference that was not supplied:

```text
AFlow run failure-run - Failed
  Status:   failed
  Turns:    0 of 5
  Checkpoint: Checkpoint 4 of 10
  Last turn: 4 (implement_plan, harness-failed)
  Reason: DSH ACP request failed: Usage limit reached for 5 hour.
  Elapsed:  0s
  Plans:
    original: plans/demo.md
  Logs:
    stderr: /full/path/to/turn/stderr.txt
```

Successful CLI explanations remain on stdout, while the human narrative is
on stderr. A pre-turn failure still writes the run header and terminal failure
summary before raising; provider exceptions and renderer/output failures keep
their existing finalization and persistence paths.

`aflow show` renders plain ASCII workflow inspection output: shared or
workflow-applicable roles and teams, then each declared step labeled
`[executable]` or `[excluded]` with `go ->` transitions, `[terminal]` END
markers, and `when <condition>` annotations.

## Run Logs

Each workflow invocation writes structured artifacts under one `.aflow/runs/<run-id>/` directory.

Saved data includes:

- top-level `run.json`
- turn directories under `turns/turn-NNN/`
- system, user, and effective prompts
- argv and environment metadata
- manager decisions under `manager/decision-NNN/`, each with immutable
  `boundary.json` inputs, exact context, system/user prompts, stdout, stderr,
  and parsed result. Historical analysis rebuilds the context from the boundary
  inputs and reports any stored-context drift. When note-authority correction
  runs, its exact prompts and streams live additively under
  `note-authority-correction/`; the root `result.json` links that attempt and
  remains the canonical accepted-or-invalid result for one decision number.
- `manager-report.md` for a manager stop, manager protocol/mutation failure,
  explicit workflow stop, or another terminal incident after run creation

Manager artifacts remain separate from `turns/` so turn accounting stays
stable. `run.json` contains only compact manager history, pending one-hop state,
and the latest report path; raw traces are referenced by stable run-relative
paths and byte sizes rather than copied into manager prompts. On failure the
CLI prints the same self-contained report that is persisted, including evidence,
attempts, plan/workspace state, and suggested next actions.

Lifecycle identity in `run.json` is durable across manager and status metadata
writes. A boundary that does not repeat the execution context preserves the
recorded worktree path, feature branch, main branch, setup, and teardown so an
interrupted managed run remains resumable.
When the source run has an active turn whose result is still `starting`, the
resume context restores that exact step. The original CLI start step remains
invocation metadata and is not used to skip an interrupted review or worker.
If the active turn is already finalized but `run.json` still points at the
previous manager boundary, resume restores that turn's snapshot, new repair
plan, and selected transition. It runs the missing manager boundary against
the source-run artifacts before launching any new harness turn, so a first
rejection still upgrades the repair worker and cannot skip to the next
checkpoint.
For worktree runs, follow-up plan version selection and post-turn creation
detection use the execution checkout. A repair plan that existed before the
review is not treated as a new review result: approval restores the original
plan for the next checkpoint, while a newly created higher-version repair plan
becomes active.
Resume repairs older affected run state when the original checkpoint has
advanced, the saved checkpoint or checklist-style repair overlay is complete,
and the scope is no longer awaiting review. The stale scope and one-hop routing
state are cleared before the next worker invocation.
For a manual owner-directed scope reset, CLI
`--resume RUN_ID --resume-reset-scope` preserves the reused lifecycle context
(including the feature branch and worktree when present) and historical
manager/attempt audit trail while explicitly discarding the saved repair
overlay, interrupted step, active implementation scope, scoped counters, live
attempt index, pending one-hop actions, and prior terminal report pointer.
The source run remains the immutable attempt audit record. The next worker
therefore opens a new scope from the invocation's original plan and baseline
team. This manual compatibility path is distinct from the automatic
scope-preserving repartition transaction above.

- stdout and stderr
- plan snapshots before and after each step
- evaluated conditions and chosen transitions
- terminal `end_reason`
- `issues.md` when issues accumulate

Turn directories are created before the harness process launches and finalized
in place afterward. Once a durable turn starts, every ordinary catchable
post-start exception finalizes that turn exactly once and leaves the run
failed and resumable. Persisted exception evidence contains only the exception
class and a single-line, redacted message capped at 512 characters; AFlow does
not store a traceback or automatically retry an unexpected provider boundary.

Older run directories are pruned according to `keep_runs`.

## Success Reporting

When a workflow finishes successfully, `aflow` prints one stdout line naming the workflow, turn count, and stop reason.

Machine-readable `end_reason` values:

- `already_complete`
- `done`
- `max_turns_reached`
- `transition_end`

`transition_end` covers a successful `END` transition that is not driven by
`DONE` or `MAX_TURNS_REACHED`, including an unconditional
`go = [{ to = "END" }]`, but only when the post-turn original-plan snapshot is
complete. An incomplete `END` fails without an `end_reason`; lifecycle branch,
worktree, plan, and run state remain available for explicit resume, and merge
teardown is not entered.

A failed terminal merge is the sole complete-plan resume case. The durable run
must record `transition_end`, a complete snapshot, failed merge metadata with a
reason, and configured merge teardown. Its successor retries only terminal
integration; it does not launch another checkpoint or workflow harness.

### Resume after manager context-budget rejection

A failed run whose recorded manager result identifies a post-turn, prelaunch context-budget rejection may resume its saved finalized-turn boundary, including when all implementation checkpoints are checked. Admission validates the matching manager decision and turn artifacts and refuses already-consumed boundaries. Resume retries supervision before following the saved transition; it does not rerun the completed worker or treat pending review as finished.

The manager result keeps `failure_stage = "prelaunch"` for admission compatibility
and records `failure_kind = "manager_input_budget"` plus the precise budget
reason. Its bounded `diagnostic` metadata records attempted and permitted prompt
bytes, per-field byte counts, history-reduction counts, digests and sizes for
the rejected context/prompt, and the retained artifact paths. Complete
`rejected-context.json` and `rejected-user-prompt.txt` bodies are written
atomically in the same decision directory only when their combined UTF-8 size
is at most 256 KiB. Larger bodies are omitted with `body_omitted = true`; a
partial write records exactly which body, if any, was retained. The existing
`context.json` remains the provider-safe fallback and retains the known
checkpoint, finished-turn, and stdout/stderr references.
The generated `manager-report.md` diagnoses this case as manager input exceeding
the byte budget before provider launch; it does not describe the absent response
as invalid or unavailable manager output. Terminal workflow incident evidence is
reported separately when present.
