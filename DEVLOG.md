# DEVLOG

## 2026-08-31 — Stop shipping test support and pytest at runtime (PR-15)

- Base SHA: `059b1fb5ad6654f18935b20db7538663361310ad`.
- Moved the shared helper bundle from `aflow/_test_support.py` to
  `tests/_support.py`, removed its 7,369 blank padding lines and dead direct
  execution guard, and preserved the 19-helper wildcard re-export contract.
  The helper changed from 7,644 physical/275 nonblank lines to 504/457.
- Moved `pytest>=8.0.0` from runtime dependencies to the default `dev` group;
  the lockfile keeps it development-only. Root collection stayed at `1,353`
  and the full suite passed with `1353 passed, 216 subtests passed`.
- The rebuilt wheel omits the test helper and `pytest` runtime requirement; a
  fresh Python 3.11 wheel-only install printed `runtime-package-clean`.

## 2026-08-30 — Lift repartition application coordinator out of the workflow closure (PR-14)

- Lifted the 274-line pending-repartition reconciliation and application
  closure, replacing its 12 captured names with eight stable coordinator
  fields and five per-call path/step/team values.
- Added a small live-state bridge returning and assigning the active plan path
  and current step, preserving the unchanged manager-gate and startup-resume
  consumers. Consolidated cycle and application persistence through one
  explicit `_persist_pending_repartition()` helper, preserving the
  post-application path/step-before-final-write and event ordering.
- Base SHA: `3474eeb65e4b80b2561844e72378f666c184dcb2`.
- Verification: repartition `100 passed`; repartition-filtered runtime `7
  passed, 249 deselected, 6 subtests passed`; control-plane resume `4 passed`;
  manager `42 passed`; manager-filtered runtime `35 passed, 221 deselected, 7
  subtests passed`; root `1353 passed, 216 subtests passed`; app server `186
  passed` with three existing dependency warnings; web `34 passed`; web lint
  zero errors with five known React hook warnings; structural AST and
  behavioral-equivalence ledgers passed; Python compileall, package build, and
  web production build passed.

## 2026-08-28 — Lift repartition cycle executor out of the workflow closure (PR-13)

- Lifted the 462-line nested repartition proposal, mechanical-validation, and
  semantic-validation cycle into one private, frozen, module-level
  `_RepartitionCycleExecutor`.
- Replaced its ten captured names with eight stable constructor fields plus the
  two per-call plan paths, `original_plan_path` and `active_plan_path`.
- Kept proposal, correction, candidate, verdict, artifact, persistence, and
  durable transaction contracts unchanged. Invocation, persistence, and plan
  application remain live nested callbacks; automatic repartition remains
  opt-in.
- Base SHA: `388838a794bc713ae9f7c8da8126273a7ff72eb0`.
- Verification: repartition `100 passed`; repartition-filtered runtime
  `7 passed, 249 deselected, 6 subtests passed`; control-plane resume `4
  passed`; manager `42 passed`; manager-filtered runtime `35 passed, 221
  deselected, 7 subtests passed`; root `1353 passed, 216 subtests passed`;
  app server `186 passed` with three existing deprecation warnings; web `34
  passed`; web lint zero errors with five existing React hook warnings; Python
  compileall, package build, and web production build passed. Structural
  ledger and diff checks passed: one executor construction and callback
  injection, eight executor fields, manager gate 11 fields and three manager
  calls, manager executor 11 fields and five call sites, unchanged 274-line
  application helper, and zero legacy closure references.
- No live workflow, editable-tool retarget, service restart, deployment, or
  unrelated file change occurred.

## 2026-08-28 — Refresh manager executor team after live overrides

- Removed `baseline_team_name` from the frozen `_ManagerCallExecutor`
  constructor and passed the current boundary team into all five manager calls,
  so manager metadata and Lite/Full profile resolution follow a team override
  accepted after the workflow has started.
- Strengthened the manager team-override regression to apply the override after
  turn one and verify the manager model changes from the base team to the live
  team while upgrade routing remains correct.

## 2026-08-28 — Refresh manager gate baseline after team overrides (PR-12 follow-up)

- Removed `baseline_team_name` from the frozen `_ManagerGateCoordinator`
  constructor and passed the current boundary value into all three `run()`
  call sites, preserving team-override updates for manager context, upgrade
  eligibility, and target routing.
- Added a manager-enabled team-override regression covering the hotplug chain
  and the executed upgrade selector.

## 2026-08-28 — Lift manager gate coordinator out of the workflow closure (PR-12)

- Lifted the 436-line nested manager gate closure into one private, frozen,
  module-level `_ManagerGateCoordinator`. Its 11 stable constructor fields
  exclude the mutable baseline team; the five boundary inputs are passed per
  call: `baseline_team_name`, `original_plan_path`, `active_plan_path`,
  `new_plan_path`, and `runtime_current_step_name`.
- Kept the manager level helper module-level with an explicit stalled-turn
  threshold, while repartition execution remains nested and automatic
  repartition remains opt-in. Manager action eligibility, Lite/Full selection,
  recovery, resume, durable artifacts, events, and routing contracts are
  unchanged.
- Base SHA: `03992439d006fda19a86f96c73e86ccaae4a8b3b`.
- Focused verification: manager `42 passed`; manager-filtered runtime `34
  passed, 221 deselected, 7 subtests passed`; repartition/control-plane resume
  `104 passed`; runtime/runlog/CLI `434 passed, 171 subtests passed`;
  compileall, structural ledger, callback-equivalence, and diff checks passed.
- Full verification: root `1352 passed, 216 subtests passed`; app server `186
  passed` with three existing dependency deprecation warnings; web `34 passed`;
  web lint zero errors with five existing React hook warnings; Python
  compileall, package build, and web production build passed. Structural
  ledger: one coordinator class/construction and three coordinator calls with
  11 fields and five per-call mutable inputs; one module helper; unchanged
  nested repartition callbacks; one executor construction and five executor
  calls.

## 2026-08-27 — Lift manager call execution out of the workflow closure (PR-11)

- Lifted the 388-line nested `_run_manager_call()` closure into one private,
  frozen, module-level `_ManagerCallExecutor`. Its 16 captured names are now
  12 stable constructor fields plus three per-call mutable inputs:
  `original_plan_path`, `active_plan_path`, and `current_step_name`.
- Lifted the repository fingerprint helper with explicit repository and plan
  paths. Lite/Full decisions, correction eligibility, note scopes, artifacts,
  events, history, failure handling, repartition, and resume contracts remain
  unchanged; gate policy remains inside `run_workflow()`.
- Base SHA: `b674915759b6389498bf370a74fa1e02bf2b92f1`.
- Verification: manager `42 passed`; manager-filtered runtime `34 passed,
  221 deselected, 7 subtests passed`; repartition/control-plane resume
  `104 passed`; runtime/runlog/CLI `434 passed, 171 subtests passed`; root
  `1352 passed, 216 subtests passed`; app server `186 passed` with three
  existing dependency deprecation warnings; web `34 passed`; web lint zero
  errors with five existing React hook warnings; compileall, package build, and
  web production build passed. Structural ledger: one executor class and
  construction, five executor calls, four fingerprint calls, and no
  `_run_manager_call` references.

## 2026-08-27 — Consolidate standard workflow failure finalization (PR-10)

- Consolidated the 13 standard post-context terminal failure sequences into one
  private finalizer, preserving summaries, plan identity, snapshots, banner
  ordering, and all five exception cause chains; the two writer-fallback
  snapshots remain implicit at their original call sites.
- Left the 15 special failed writes explicit, including startup, preflight,
  manager/event, merge, pruning, and max-turn responsibilities.
- Base SHA: `1a9e8b1df478d0ccf8d79b3e2234da80f07a43a4`.
- Focused verification: runtime/runlog/CLI `434 passed, 171 subtests passed`;
  manager/repartition/control-plane resume `146 passed`; compileall and diff
  checks passed.
- Full verification: root `1352 passed, 216 subtests passed`; app server `186
  passed` with three existing dependency deprecation warnings; web `34 passed`;
  web lint completed with zero errors and five existing React hook warnings;
  package and web production builds passed.

## 2026-08-26 — Bind workflow run-metadata persistence (PR-09)

- Replaced 56 workflow calls that repeated run paths, controller config/state,
  workflow name, and predecessor run ID with one frozen `RunMetadataWriter`;
  mutable plan paths, execution context, current step, snapshots, counters,
  failures, and terminal metadata remain explicit at every write.
- Preserved schema-v2 validation and atomic replacement, resolved-team
  precedence, lifecycle fields, manager/hotplug state, repartition, resume, and
  terminal persistence contracts. The standalone writer function and alias
  surface were removed.
- Base SHA: `aaa8dbc67fcac522023d910c4213679e18cd909d` (merged PR #21).
- Focused verification: runlog `28 passed`; runtime/CLI `405 passed, 171
  subtests passed`; manager/repartition/control-plane/runlog `174 passed`;
  compileall and diff checks passed.
- Full verification: root `1351 passed, 216 subtests passed`; app server `186
  passed`; web `34 passed`; web lint completed with zero errors and five known
  hook warnings; Python/package and web production builds passed.

## 2026-08-23 — Require complete current resume state (PR-08)

- Bumped durable controller metadata to schema version 2. Fresh writers now
  emit the authoritative original plan, explicit team/lifecycle fields, frozen
  configuration identity, complete manager state, hotplug state, and active
  scope envelope references.
- Explicit, `--resume` auto-selected, and daemon resumes admit only exact
  integer schema 2 with complete metadata. Older metadata remains readable for
  analysis but is rejected before plan lookup, startup questions, allocation,
  or continuation; no migration or rewrite is attempted.
- Base SHA after PR 7: `30213fc4ed70b5847e1cf78154f943f5b3a957f9`; PR 7 merge SHA:
  `30213fc4ed70b5847e1cf78154f943f5b3a957f9` (PR-19).

## 2026-08-23 — Restore the supported cross-platform test matrix (F-07)

- The documented root suite now has a truthful Linux/macOS boundary: Linux runs
  the p100 deployment tests, while macOS skips only that 15-test Linux module.
- Repaired ownership guidance, executable Reasonix, Rich transition, and
  AFlow-owned terminal-state test expectations without changing production
  behavior.
- Added reusable Ubuntu/macOS Python 3.11 CI and made package publication wait
  for the same workflow before building or uploading.

## 2026-08-23 — Make local daemon worker status portable and truthful (F-06)

- Local daemon status preserves Linux procfs ownership inspection and uses one
  bounded, strict process-table snapshot on systems without usable procfs.
- Only exact direct `daemon-worker` children for the verified repository are
  counted; malformed or untrusted ownership evidence returns explicit
  ambiguity instead of a false zero, with a daemon birth-identity recheck
  before success output.
- Added real forced-fallback and deterministic parser coverage for spaced paths,
  multiple workers, unrelated/legacy processes, malformed snapshots, and
  ownership races.

## 2026-08-22 — Match resumed legacy controllers by durable lineage (F-05)

- Resumed legacy guards now bind a successor's durable predecessor to one exact
  explicit `--resume` CLI option.
- Free run-ID substrings and post-`--` text no longer establish ownership.
- Wrapper deduplication, descendant selection, classifications, and observer-only
  authority remain unchanged.
- Deterministic tests cover lineage positives, wrappers, descendants, duplicates,
  and collision/spoof cases.

## 2026-08-22 — Bootstrap Git Tracking before run allocation (F-03)

- Fresh pristine review plans now receive one minimal Git Tracking section
  before run identity, launch persistence, start events, or run paths exist.
- The controller preserves the exact pre-insertion backup, atomically writes
  and reloads the plan, verifies its checkpoint snapshot, and records either
  current `HEAD` or the verified empty-repository bootstrap commit.
- Startup rejects prepared-plan snapshot drift before writing, lifecycle branch
  synchronization rewrites only the parsed live section, and daemon starts run
  the same normalization boundary before reserving launch identity. Workers
  recompute an empty-repository deferred base fill after daemon preparation.
- Removed the unreachable base-HEAD confirmation enum and dispatch paths;
  existing pristine-section refresh remains automatic and noninteractive.
- Added deterministic plan, runtime, lifecycle, public-library, and CLI
  coverage with injected runners only; no model or provider call is required.

## 2026-08-22 — Fail-closed headless Reasonix ACP (F-02)

- Owned new and resumed Reasonix sessions now negotiate the exact
  `tool_approval=yolo` select option before model, effort, and prompt.
- Every configuration update must return a complete, unambiguous state that
  still acknowledges all applied values; missing, rejected, or reset approval
  closes and terminalizes the run before prompting without retry.
- Added deterministic new/resume ordering, malformed and rejected state, reset
  detection, process-close, and durable workflow-failure coverage. No live or
  paid model prompt was run.

## 2026-08-22 — Truthful turn and run termination (F-01/F-04)

- Finalize ordinary catchable post-start exceptions into one terminal turn and
  a failed, resumable run with bounded, redacted evidence and no generic retry.
- Require a complete post-turn original-plan snapshot before `END`, merge,
  worktree removal, or successful completion; incomplete max-turn and ordinary
  `END` preserve routing evidence and lifecycle state but fail.
- Added owned-session, observer, hotplug-resume, manager-on/off, CLI, and real
  worktree regressions. Harness negotiation, Git Tracking bootstrap, guard,
  daemon/API/UI, and architecture rewrites remain intentionally excluded.

## 2026-08-23 — Remove deprecated unscoped execution routes

- Removed `POST /api/executions`, `GET /api/executions/{run_id}`, and
  `GET /api/executions/{run_id}/events`, including the `find_run()`
  cross-project compatibility scan and adapter models; no redirect, tombstone,
  or compatibility alias remains.
- Lifecycle REST is now project-scoped under `/api/control-plane`; clients
  carry `project_id`, while REST and MCP continue to share the same durable
  control-plane application and services.

## 2026-08-11 — Add lightweight stdio MCP daemon

- Added `aflow daemon start|status|stop` for a single local project, with stdio
  MCP by default and optional loopback HTTP, separate from production `aflowd`.
- Added exact subprocess-unit ownership, process-group drain/escalation, and an
  atomic mode-0600 pidfile bound to process-birth identity.
- Moved the 13-tool, three-resource MCP registry into the core package while
  retaining the FastAPI adapter's header-auth and safe-error boundary.
- Added config validation, split-config preservation, public FastMCP parity,
  and stdio initialization/EOF cleanup regressions.
- Live restart proof fixed reconciliation precedence so a durable terminal
  controller record and matching launch phase supersede an older daemon
  observation instead of being misreported as needs_attention.

## 2026-08-10 — Document daemon-backed control-plane boundary

- Documented the release-pinned p100 service, strict project allowlist,
  header-only bearer transport, and the separation between daemon workflow
  ownership and HTTP/MCP/UI transports.
- Recorded reconciliation semantics: a collected or failed workflow unit is
  `needs_attention`, never automatic completion or restart; owner stop and
  explicit linked resume remain durable operator actions.
- Corrected remote-app documentation that described the retired in-memory
  execution map and query-token SSE behavior.
- Production-installed the exact release on p100 and exercised authenticated
  REST/MCP parity, idempotent start, safe-boundary CAS steering, startup
  questions, daemon restart, hard workflow death and explicit resume, owner
  stop, token rotation, legacy-run read integrity, and versioned rollback.
- Live failures led to focused fixes for writable daemon state, Tailscale
  address inspection, TOML validation, readiness rollback, Python safe-path
  entrypoints, hard-kill reconciliation, startup-question status,
  cross-surface bearer ownership, and release-pinned rollback.
- Review transitions now enter the rejection ledger only when a new repair
  plan exists, preventing approvals from influencing worker-upgrade routing.


## 2026-08-09 — Durable live worker role hotplug

- Added provider-neutral worker selector hotplug with durable transaction
  stages, exact same-harness resume, and cross-harness read-only operational
  handover.
- Resume copies and verifies handover/projection/Full-context artifacts before
  pruning; ambiguous provider boundaries remain waiting unless a durable
  operation result proves a matching target completion or not-started retry.
- Status/events/analyze expose safe transaction stages, selectors, capability
  path, relative artifact references, and hashes while omitting prompts, notes,
  environment, and raw session identifiers.
- Capability evidence was protocol/fixture based; no paid live Reasonix prompt
  smoke is claimed.

## 2026-08-08 — Forward Reasonix effort independently

- Reasonix profiles now pass their configured effort through the CLI's native
  `--effort` option instead of requiring a nonexistent model-name variant.
- Adapter coverage fixes the DeepSeek V4 Flash/max invocation contract; the
  deployment configuration selects Sol 5.6/medium for `ds4_flash_max` reviews.

## 2026-08-08 — Prefer fastest-safe guard completion

- Guarded recovery now chooses the smallest reversible, scope-reducing option
  when it preserves the plan's usable outcome and does not weaken safety,
  authorization, acceptance, or approved cost boundaries.
- Plan-less exact `aflow run --resume RUN_ID` controllers are recognized only
  when the resume ID matches durable lineage and the controller cwd matches the
  guarded repository; self-tests reject the same argv from another repository.

## 2026-08-07 — Add role-scoped workflow prompts and material review

- Added validated global and team role prompts with team replacement, global
  fallback, retry persistence, and frozen-run identity coverage.
- Ordinary workflow turns now persist resolved role guidance as their system
  prompt; manager and lifecycle invocations retain their existing prompt paths.
- Bundled `material-code-review` as a default skill and enabled its guidance for
  the packaged reviewer role.

## 2026-08-07 — Complete and resume every lifecycle mode

- Branch-only merge teardown now switches a clean primary checkout from the
  expected feature branch to `main` before attempting the engine-owned
  fast-forward, while unrelated checked-out branches still fail closed.
- Explicit and interactive resume now reconstruct no-lifecycle, branch-only,
  and linked-worktree execution contexts from mode-specific durable metadata.
- Regression coverage exercises ordinary branch-only completion plus
  environment-preflight recovery without synthetic worktree paths.

## 2026-08-07 — Preserve guard replacement lineage

- Replacement linkage now retains the original recovery fingerprint, validates
  complete predecessor/successor identity and one live successor controller,
  and migrates unsafe same-successor linkage without rewriting observation
  history.
- Missing or malformed identity fields and absent original plans fail closed
  before guardian state is written.

## 2026-08-07 — Add adapter-neutral harness environment preflight

- Added a bounded, secret-safe executable check for real harness launches and
  an optional adapter capability for local prerequisites.
- Reasonix now uses `reasonix doctor --json` to detect enforced bash sandboxing
  without guessing configuration files; missing `bwrap` is reported with fixed
  remediation and no synthetic turn or manager artifact.
- Workflow, manager, correction, repartition, recovery, bootstrap, and
  agent-required merge boundaries share terminal run-level handling. Injected
  runners remain ready by default; explicit resume re-evaluates the pending
  invocation. AFlow reports prerequisites but does not install packages or
  validate provider health.
- The guardian remains the fallback for legacy and unsupported failures.

## 2026-08-06 — Make clean-END manager eligibility deterministic

- Clean controller-proposed `END` boundaries now omit `stop` from Lite
  eligibility while preserving the existing action set for Full.
- Ineligible or invalid Lite output and explicit Lite escalation restore Full
  eligibility for one decision from the same finalized turn; accepted Full and
  non-clean-boundary `stop` decisions still fail through the existing report,
  event, and banner path.
- Focused manager and runtime regressions cover Lite `stop` to Full `continue`,
  both Full-stop fallback routes, direct Full selection, and ordinary non-END
  Lite `stop` without interpreting manager prose.
## 2026-08-02 — Correct manager plan-selection notes at one boundary

- An otherwise valid manager response whose only authority defect is
  plan-selection wording now receives one same-profile correction sub-attempt
  before persistence; only advisory notes may change.
- Original and corrected traces remain under one decision directory, while
  routing, history, observer events, and turn accounting retain one logical
  manager decision. Mutation, boundary drift, immutable-field changes, launch
  failure, invalid JSON, and a second authority failure stop without retry.
- Verification passed: focused context (`1` test), focused runtime (`6` tests,
  `5` subtests), manager/context (`74` tests), broader manager runtime (`26`
  tests, `7` subtests), and documentation (`20` tests), plus compilation and
  diff hygiene.

## 2026-08-02 — Trust turn diagnostics by stream and outcome

- Text-signal evidence now records semantic stdout separately from
  failure-eligible stderr. A successful turn's stderr transcript cannot turn
  echoed plan, branch, merge, or owner-action phrases into structural
  failures; explicit stops and structured failure evidence remain unchanged.
- Lite manager context intentionally omits plan prose, labels that redaction,
  and gives durable controller-owned plan/workspace facts precedence over
  contradictory text. The additive provenance and disclosure fields preserve
  existing sorted signal-name lists and schema-v1 output.
- A managed-worktree runtime regression confirms the incident-shaped turn gets
  one accepted post-turn decision, launches checkpoint review, and retains the
  implementation change and worktree without a false stop or cleanup.

## 2026-08-02 — Bundle same-task AFlow guard

- Moved `aflow-guard-development-run` into AFlow's canonical bundled-skill
  inventory and made it a default install for supported harnesses.
- Guard heartbeats and actionable reports now stay in the task that requested
  supervision; the bundled helper retains pinned-run and provenance checks.

## 2026-08-02 — Bind pending repartition resume identity and paths

- Non-reset explicit and AUTO resume now require the restored active scope,
  envelope, manager decision boundary, derived generation ID, executable target,
  and canonical artifact keys before startup; internal symlink aliases fail
  closed, while reset scope keeps pending state opaque.

## 2026-08-02 — Fail-closed pending repartition resume validation

- Explicit and AUTO resume now reject present malformed or stage-incomplete
  pending repartition state, validate every carried artifact reference, and
  bind its bytes before startup; reset-scope resume continues to discard this
  checkpoint-scoped state without interpreting its metadata or artifacts.

## 2026-08-02 — Complete resume context before startup

- Explicit and AUTO resume now decode the selected run's complete worktree,
  lifecycle, manager, scope, pending-artifact, and override context before
  startup questions; post-startup checks reuse that same loaded context.

## 2026-08-02 — Plan-optional durable resume bootstrap

- Resume now resolves one explicit or shell-selected durable run before startup
  preparation, reconstructs omitted plan and invocation identity from `run.json`,
  and rejects conflicting repeats or unsafe metadata without creating state.
- `original_plan_path` is authoritative, with `plan_path` retained only as a
  legacy fallback. Fresh runs still require `plan_file`.
- Resume also preserves an explicitly empty `--` instruction suffix as caller
  input and rejects path-shaped run IDs before loading durable metadata.

## 2026-08-02 — Frozen configuration identity on resume

- Schema-versioned resume metadata now requires a complete `frozen_config` and
  rejects workflow, canonical config-path, or configuration-fingerprint drift
  before startup questions or new durable state.
- The execution boundary repeats the comparison after reloading configuration,
  while schema-less legacy metadata without a frozen identity retains its older
  scalar/lifecycle compatibility path.

## 2026-08-02

- Standardized p100 self-hosted development on an editable uv tool installation:
  run `uv tool install -e . --force` from the intended AFlow checkout, then invoke
  `aflow` directly. `uv run aflow` is not a supported development launcher;
  `uv run` remains available for tests and other project-scoped checks.

## 2026-08-01 — Codex prompt stdin transport

- Checkpoint 1 moves Codex effective prompts from argv into stdin while keeping
  prompt artifacts and injected-runner behavior observable.
- Large-prompt and early-stdin-close runtime fixtures pass; launch-failure
  normalization remains planned for Checkpoint 2.

## 2026-08-01 — Harness launch failures use terminal paths

- Checkpoint 2 normalizes process-creation `OSError`s from default and injected
  harness execution into bounded nonzero results: 127 for missing executables
  and 126 for other launch failures.
- Manager artifacts, worker turn artifacts, and lifecycle failure metadata now
  use their existing nonzero-result handling without storing a traceback or
  prompt-bearing launch diagnostic.
