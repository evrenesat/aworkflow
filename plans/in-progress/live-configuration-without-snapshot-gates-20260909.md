# Live configuration without snapshot gates

## Summary

Make current saved configuration authoritative before every turn and on resume. Remove configuration snapshot checksum enforcement, configuration fingerprint equality gates, and restrictions limiting teams/profiles to the launch-time catalog. Do not replace them with configuration revisions, import transactions, approval prompts, or opt-in flags. Also add the owner-requested dirty-worktree preflight and explicit continuation checkbox to the New Run form; that acknowledgment addresses existing uncommitted files, not config changes.

Owner decision: global settings changes automatically affect existing runs at their next turn, including resume. Triggering incident: doublangu run `20260909t003443z-2e7ece2a` accepted `team = "MusparkGLM"` through controls, completed its current review, then stopped because the team existed globally but not in its snapshot. The new behavior must work for newly added profiles too, not just aliases of existing profiles.

Scope is configuration authority and its launch/runtime/resume/control/UI consumers, dirty-worktree launch choice, and the concrete restart-instruction/review-target failures described below. This is not a repository-wide deletion of checksums or recovery validation. Execution is authorized through the AFlow web control plane. Verification must not edit real run state, restart unrelated workflows, reinstall the shared editable tool, deploy, or publish as part of this handoff.

Second observed incident: run `20260909t063845z-f9aa2610` approved CP6 as `8164bea`, implemented CP7, then turn 004 reviewed CP6 again and excluded CP7's dirty files. The manager stopped before CP8 because CP7 had no verdict. Operator error contributed: the restart used CP6-specific instructions in the run-wide `--` text, replayed on every turn. AFlow's generic review prompt gave only plan paths; its bundled review skill preferred the latest checkpoint commit even when it was already approved. CLI resume additionally rejected replacement/empty instructions. The latter restriction was fixed locally during recovery (205 tests plus 127 subtests passed); preserve that change and finish its public-interface coverage below. No production run artifacts need copying into tests.

Inspected baseline: `3a4c4f9160d0401434bdb71054e46a3220c89cd1`, including existing dirty changes. Bootstrap: `git rev-parse --show-toplevel`; `git status --short`; `rg --files -g AGENTS.md`; read root and applicable nested guidance. Existing README.md, AGENTS.md, ARCHITECTURE.md and DEVLOG.md satisfy bootstrap requirements.

Existing uncommitted changes include CLI/daemon resume identity fixes, workflow and manager budget/context fixes, documentation, and related tests including untracked `tests/test_resume_manager_budget.py`. Preserve unrelated behavior; deliberately supersede only their frozen-config enforcement. Reinspect the actual diff before implementation. Another pending plan, `plans/in-progress/skills-manager-workflow-settings-20260908.md`, assumes frozen workflow settings: this plan supersedes that assumption only, without changing that plan's skill-management scope. If its workflow-level manager setting lands first, reload that resolved setting too.

## Git Tracking

- Plan Branch: `codex/aflow-dogfood-20260909`
- Pre-Handoff Base HEAD: `576d91e50df6423f5e9a21b9fc5cfa90bdec5dba`
- Last Reviewed Checkpoint: `cp2 v01` — approved.

### Review Log

- 2026-09-10: Approved `cp2 v01` from the immediately preceding worker's repaired worktree against CP1 `7454db7`; no pending CP2 commit existed, so worktree fallback was used. Both prior P2 findings are resolved. Required CP2 plus library/API suites: 288 passed, 131 subtests; metadata regressions: 2 passed; unfinished-step and finalized reviewer/worker replay: 3 passed; `git diff --check` passed. No material findings. The `cp03-v01` CP2 repair overlay is resolved; CP3 remains unchecked. Manager 40 KiB guard and 16 KiB compact target retained.

- 2026-09-10: CP2 pending worker worktree reviewed against approved CP1 `7454db7`; rejected for two P2 defects: missing ControllerState start-step provenance breaks metadata writes, and explicit resume correction equal to the original start is ignored. Implementation step markers retained; approval unchecked. Repair overlay `live-configuration-without-snapshot-gates-20260909-cp03-v01.md` is CP2 scope despite filename. Required suite: 251 passed, 125 subtests. Expanded API/runtime: 309 passed, 49 subtests, two metadata regressions and one stale frozen-gate assertion. See `plans/reviews/latest_review.md`. Review CP2 after repair; do not advance to CP3.

- 2026-09-10: Approved `cp1 v01` using the pending worker worktree against `576d91e`; preceding checkpoint commits belong to older plans. CP1 implementation markers were already checked; CP2 remains unchecked. No material findings. Verification: `uv run pytest tests/test_live_config.py tests/test_run_config_snapshot.py tests/test_config.py tests/test_run_state.py tests/test_runlog.py -q` — 211 passed, 7 subtests passed; `git diff --check` passed. Scope: current-source loader, snapshot compatibility, optional state/provenance serialization and focused tests. Launch/resume integration remains CP2.


## Done Means

- [ ] Saving settings changes future turns without restarting the controller; active calls finish with the settings they started with.
- [ ] New teams and new profiles work through direct overrides, CLI resume and remote controls. Invalid remote requests are rejected before writing overrides and do not stop healthy work.
- [ ] CLI/UI/MCP launch and resume use current configuration, regardless of old snapshot contents, absent snapshot files, old hashes, or changed config location.
- [ ] Future prompts, models, effort, role mappings, manager settings, retry policy and workflow steps use the refreshed configuration. No launch-time cached consumer silently wins.
- [ ] Actual worktree/branch ownership, plan progress, completed-turn evidence and duplicate-controller protection remain correct.
- [ ] Legacy runs resume without manual migration. Tests and documentation describe these behaviors without frozen-config restrictions.
- [ ] New Run lists the dirty paths before launch and offers an unchecked “Continue despite uncommitted changes” checkbox. Checked acknowledgment permits supported launches without clearing, stashing, committing or discarding the user's files; unchecked launches cannot silently bypass the choice.
- [ ] Reviews receive the actual pending checkpoint target instead of rediscovering an already-approved commit. Resume can replace or clear stale instructions, and a one-turn recovery note can reach a reviewer without repeating in later checkpoints.

## Critical Invariants

1. **One execution source.** Normal CLI runs follow their selected config path (explicit `--config` when supplied, otherwise existing configured default); daemon/UI/MCP follow their configured current source path. Persist this source separately from diagnostic copies. Resume uses an explicitly supplied current path first, otherwise the saved live source for new CLI runs; legacy CLI metadata may provide `snapshot.json` origin as a path hint only, then the normal current default if no usable hint exists. UI/MCP always use their configured current source, including cross-host resume. A missing explicitly selected/saved live file is an actionable config error, never permission to execute a historical copy. A legacy snapshot path is not a live source unless explicitly supplied by the operator. Explicitly selecting an editable copy is allowed; no checksum comparison applies. Relative paths resolve against the selected file's directory.
2. **One settings read per turn.** Use the existing pair lock when reading `aflow.toml` and sibling `workflows.toml`; parse/validate the pair into one object before use. Reload before the loop's turn-limit decision, pending override validation and runtime resolution. That object governs the entire turn, including its transition decision and post-turn supervision/recovery. Edits during the turn apply at the next boundary. No filesystem watcher, background poller or config version store. A resume's first turn follows the same rule.
3. **Refresh derived objects.** Replace `wf`, config-derived limits/policies and cached invokers from that object. Do not reset progress, counters, manager budgets, scope state, pending notes, or retry attempts when refreshing dependencies. Current workflow name and pending step remain run identity/progress, not defaults that a global edit can silently replace. Edits to that workflow's future step definitions and graph are allowed. Finish a started turn under its original graph; resolve its chosen next step in the next loaded graph.
4. **Explicit choices beat defaults.** Accepted run-local role overrides beat team mappings, which beat global mappings. An explicit run/resume team remains selected by name but its definition is live; existing temporary runtime team choices retain their existing duration/precedence and resolve through current definitions. Without an explicit choice, follow the current workflow team default. Explicit run/control max-turn limits beat the live global default; increasing a default can permit the next turn, lowering it to the completed count ends normally without another turn. Persist only the small provenance needed to distinguish explicit team/limit choices from defaults. Legacy runs lacking provenance retain recorded values as explicit, while resolving their team definition live. Existing explicit controls must not become stale copies of profile definitions.
5. **Keep execution facts.** Never relocate a created worktree, change its merge target, rerun setup, change continuation ancestry, or repeat completed lifecycle actions because defaults changed. Existing execution context owns already allocated paths, branches, setup/teardown obligations and merge target; new runs use current lifecycle defaults. Prompt/model settings used by future lifecycle invocations can refresh. Workflow switching, arbitrary progress edits and lifecycle migration are outside this request.
6. **Material validation only.** Keep TOML/schema/reference validation, executable availability checks before launch, selected-workflow/step existence, exact run/repository ownership, plan/progress consistency, atomic writes, concurrent-edit CAS, idempotent launch/control requests, secret redaction and access controls. Their benefits are runnable commands, avoiding lost work/duplicate processes and protecting the actual repository. Remove hash/path-equality checks whose sole purpose is preventing config changes. Snapshot hashes only detect byte changes; they do not distinguish intended edits from corruption and are not a security boundary against writers who can edit both files.
7. **Failure behavior.** Invalid current configuration or a removed selected team/step produces the existing pre-turn failure path with a concise file/field-specific explanation; launch no next harness and preserve progress so correction plus resume works. Do not execute old settings silently. A valid `next_step`/team/role correction in the pending override must be considered before declaring its replaced target missing. Handle owner stop before attempting a config reload, so broken TOML cannot prevent stopping.
8. **Bad controls do not kill good work.** API/CLI control admission rejects an invalid proposed override without replacing the previous file/revision. Revalidate at the execution boundary because settings may change meanwhile. For a newly invalid pending override, record its rejection once per existing digest, retain last accepted run choices and continue if those choices resolve in the current config. If the underlying current choices are themselves unusable, use invariant 7. Directly edited malformed overrides follow the same nonfatal rejection rule. A rejected override stays editable and is retried when corrected; it is never treated as accepted. Keep genuine hotplug/session recovery failures distinct.
9. **History is descriptive.** Existing snapshots and legacy `frozen_config`/manifest fingerprint fields remain readable but never admit/block execution. Stop generating per-file snapshot checksums. Diagnostic copies, if retained for compatibility, are ordinary launch-time copies and never authoritative; inability to read/write those optional copies cannot block an otherwise valid run. Keep required run-state persistence failures fatal. Use existing per-turn model/effort/prompt/argv artifacts to explain actual execution; do not fabricate historical evidence from current settings.
10. **Dirty-worktree choice.** Preflight is read-only and reports the actual source checkout and execution mode. Parse `git status --porcelain=v1 -z --untracked-files=all`, including staged/unstaged/untracked/deleted/renamed paths, preserving unusual filename characters and rename source paths. Ignore only existing lifecycle-owned internal artifacts under the current classifier; report plan edits too, even when they do not require confirmation. Return `requires_confirmation` using existing plan-only exemptions but replace the non-plan-worktree hard prohibition with the same explicit choice used by CLI startup. Unmerged entries and in-progress Git operations remain actual blockers; a checkbox does not solve conflicts.
11. **Acknowledgment semantics.** Carry the existing `dirty_worktree_confirmed: bool = False` through the launch payload, durable startup request and final workflow preflight. Preflight allocates no run, worktree, unit or files. At actual launch recheck current status: false plus confirmation-required dirt returns the existing structured dirty-worktree startup question, not a terminal failed run; true permits dirt without another confirmation. It covers current dirt for that exact submitted project/workflow/plan selection, not a hashed byte inventory. No acknowledgment token/checksum/revision system. Reset the checkbox when that selection or restart source changes. Launch identity must include the boolean so idempotent replay cannot silently change the request's choice.
12. **Do not lose dirty work.** A same-checkout workflow sees existing changes. A new-worktree workflow starts from its selected commit and leaves source dirt untouched; show that distinction next to the list before launch. Do not silently transfer source dirt, stash it, reset it, commit it, or skip lifecycle safety checks other than the acknowledged dirt prohibition. Before branch checkout/create operations that could overwrite changes, retain Git's own refusal and give the actual error. Keep dirty merge/teardown protection: launch acknowledgment does not authorize later deletion or overwriting of unrelated files.
13. **Review target is pending work.** For checkpoint review, use the existing implementation scope awaiting review, including its original checkpoint identity, even when implementation markers advance to the next unchecked checkpoint. Supply that identity, original checkpoint heading, active overlay (if any), and the relevant worker-attempt artifact in the review prompt. For a legacy/recovered run with no active scope, use the latest finalized worker attempt's pre-turn checkpoint and existing plan review state to identify pending implemented-but-unapproved work. Do not use `current_checkpoint_index - 1` or the newest approval commit as a guessed target. Explicit operator requests for a different review remain allowed and are labeled as such; engine context must not silently override direct user intent.
14. **Instructions have an understandable lifetime.** Global extra instructions remain global; do not auto-detect checkpoint numbers in prose or silently expire user text. Resume omission inherits them, an explicitly supplied list replaces them, and an empty list clears them. CLI bare `--` represents empty. Existing override `notes` are one-turn recovery instructions: when `next_step` is supplied they apply to that selected step, including reviewer; without `next_step` preserve the existing next-worker behavior. Consume them only when the targeted invocation is durably started using existing override consumption state, not on unrelated steps, preflight failure or rendering error. On later turns/resume after consumption, they do not replay. No new recovery-note database or checkpoint approval system.

## Forbidden Implementations

- Replacing frozen snapshots with immutable configuration revisions, team-import graphs, an approval gate, a second configuration database, or a `--force`/`--live-config` escape hatch.
- Removing checksum checks while still loading snapshots on worker/retry/resume paths; accepting a new team only when its profile was originally present.
- Removing every use of SHA-256: override deduplication, idempotency keys, concurrent-edit revisions, artifact checks and non-config identity have separate concrete uses and are outside scope.
- Catching config errors and falling back to launch-time config, silently ignoring valid changes, or calling a reloaded file authoritative while retaining stale `wf`, invokers, adapters, session settings or retry prompts.
- Restarting active harnesses on global save, discarding session continuity unnecessarily, resetting budgets/progress on reload, changing recorded repository facts, or weakening duplicate-worker protection.
- Requiring legacy users to edit manifests, regenerate checksums, start from checkpoint 1, or create a new plan to resume.
- Rewriting unrelated dirty files, root AGENTS.md, pending plans, real configuration under the user's home, or real `.aflow` state. Use temporary fixtures and fake harnesses for verification.
- Implementing the checkbox only in React while startup or `_lifecycle_preflight` still unconditionally rejects dirt; treating a preflight request as launch; automatically cleaning/stashing a checkout; transferring dirt to a fresh worktree without a separate user request.
- Fixing stale recovery prose by repeating another fixed checkpoint number on every turn; selecting the latest approval commit as pending review; changing all global instructions into expiring notes; treating a worker's checked implementation boxes as proof of approval; removing the manager's useful stop for a missing review.

## Checkpoints

All checkpoints require verified internal checkboxes, passing scoped verification, and scope inspection with `git status --short`, `git diff --name-only`, `git diff --stat` before handoff. Stop for conflicting ownership of an intended edit or a material unresolved behavior not answered here; unrelated dirty files alone do not block work. Preserve them. Do not implement additional product policies to resolve an ambiguity.

### [x] Checkpoint 1: Establish current-source loading and retire snapshot integrity authority

**Goal:** One reusable loader/source resolver, with backward-readable metadata and no snapshot checksum enforcement.

**Context:** Run `git rev-parse --show-toplevel`. Inspect `aflow/config.py`, `aflow/run_config_snapshot.py`, `aflow/run_state.py`, `aflow/runlog.py`, `tests/test_run_config_snapshot.py`, `tests/test_config.py`. Preserve split-config parsing and pair-save locking.

**Scope:** Those components and focused tests; may create `aflow/live_config.py` and `tests/test_live_config.py`. Do not integrate process launch or UI yet. Keep helpers small and use existing dataclasses/serializers.

**Steps:**

- [x] Implement source resolution and locked current-pair loading according to invariants 1–2. Explicitly reject a missing live file instead of accepting `load_workflow_config`'s empty-default result for an existing run. Preserve optional sibling behavior supported by the existing loader.
- [x] Add backward-compatible optional live-source and explicit-choice provenance fields to existing run state. Old fingerprint fields may remain optional/deprecated serialization fields; no new producer needs to compute a config hash. Read legacy snapshot origin without validating file hashes or loading the copied TOML as a fallback.
- [x] Simplify snapshot helpers: remove checksum calculation/enforcement and fingerprint-match admission. Keep compatibility/optional diagnostic functions still needed by callers; remove unused code once consumers are migrated. Preserve atomic config-pair saves and relative-path semantics.
- [x] Replace immutability-only tests with behavioral tests for edited/missing historical copies, legacy origins, explicit current paths and malformed live TOML. Document the helper contract in its module.

**Dependencies:** None.

**Verification:** `uv run pytest tests/test_live_config.py tests/test_run_config_snapshot.py tests/test_config.py tests/test_run_state.py -q`; `git diff --check`.

**Done When:** Current-source reads are deterministic; historical hash disagreement never makes the helper reject a usable current configuration. Tests cover both legacy and new metadata. Every completed step is verified and scope checks pass.

**Blockers:** Ambiguous dirty-line ownership; do not remove concurrent unrelated resume fixes.

### [x] Checkpoint 2: Launch and resume from current configuration everywhere

**Goal:** Direct CLI, detached workers and remote resume no longer depend on frozen configuration identity.

**Context:** Run `git rev-parse --show-toplevel`. Inspect `aflow/cli.py` (`_resolve_resume_bootstrap` and identity parsing), `aflow/daemon.py` (reservation, startup answers, worker boot, resume), `aflow/workflow.py` startup and `_daemon_manifest_matches_execution`, `aflow/resume_relocation.py`, `aflow/control_plane/models.py`, `aflow/control_plane/repository.py`.

**Scope:** Startup/resume wiring, compatibility projections, tests. Do not implement turn-loop reload or redesign launch ownership.

**Steps:**

- [x] Route reservation, preparation, startup answers, worker boot and resume to checkpoint 1's current source. Worker argv uses the live source. Revalidate actual selected workflow/step against current settings at worker launch; a global edit after reservation is allowed without a fingerprint equality check. Prepared prompts/choices must not force execution of removed steps.
- [x] Remove config hash and config-path equality from `_frozen_identity_mismatch` callers and launch-manifest matching. Retain checks of exact run/project/plan/unit/idempotency/continuation facts. Do not relax those checks by returning unconditional success.
- [x] Stop copying predecessor snapshots as execution configuration. Legacy `frozen_config` is optional for configuration admission, not a prerequisite for reading valid saved progress. Preserve existing original-plan, execution-context, scope, lineage and controller inactivity checks and current resume budget fixes.
- [x] Persist live-source/choice provenance through CLI, daemon startup records and successor runs; explicit resume team/step corrections are validated against current settings before rejecting an obsolete saved selection. Keep existing remote API payload compatibility.
- [x] Update `docs/runtime-behavior.md` resume sections and applicable `aflow/AGENTS.md` snapshot instructions to match this implemented change.

**Dependencies:** Checkpoint 1.

**Verification:** `uv run pytest tests/test_run_config_snapshot.py tests/test_control_plane_resume.py tests/test_resume_relocation.py tests/test_resume_manager_budget.py tests/test_daemon_cli.py tests/test_cli.py tests/test_control_plane_repository.py -q`; `git diff --check`.

**Done When:** Fake worker launches and resumed successors use edited live settings despite changed/missing snapshot files; duplicate/foreign worker and wrong-worktree tests still reject. Every completed step is verified and scope checks pass.

**Blockers:** Unrelated in-flight process ownership changes; do not test by resuming production runs.

### [ ] Checkpoint 3: Reload configuration and resolve overrides at each turn boundary

**Goal:** The controller's next turn executes current saved settings with deterministic defaults and override precedence.

**Context:** Run `git rev-parse --show-toplevel`. Inspect `aflow/workflow.py` turn loop, `_apply_boundary_override`, `RetryContext` consumers and pre-turn failures; `aflow/run_state.py`, `aflow/runlog.py`, `tests/test_runtime.py`.

**Scope:** Controller/domain and its durable small provenance fields; focused tests in `tests/test_live_config_runtime.py`. Session-driver/cached-manager integration follows in checkpoint 4.

**Steps:**

- [ ] Move boundary control flow so owner stop is honored, current configuration loads, pending controls resolve, then limits and next-step execution are evaluated. Use one current object for the complete turn; update `wf` and derived controller settings while preserving actual execution context.
- [ ] Implement invariant 4 default/explicit provenance and runtime resolution. Add/remove future steps and change role/prompt definitions without full-config mismatch checks. Missing targets cause the stated actionable pre-turn failure; valid pending target corrections take precedence over checking replaced saved targets.
- [ ] Remove frozen-catalog validation from team/role overrides. Reject a newly invalid request once per digest, preserve the last accepted choices separately from last request result, and continue usable work. Test rejected direct TOML and accepted-then-deleted target cases. Keep one-shot notes/next-step and existing owner-stop semantics.
- [ ] For a pending retry, preserve saved valid plan context, paths, error appendix and attempt accounting, but resolve current role/model/effort and render current configured prompt templates using that saved context. Do not require reparsing the broken plan to pick up a config edit. Retain the context fields needed for template rendering rather than treating `base_user_prompt` as current configuration. Existing captured failure evidence remains unchanged.
- [ ] Test live limit increase/decrease at the loop edge, default team changes versus explicit choices, graph edits, selected-target deletion, malformed current config, correction/resume, and owner stop with broken config.

**Dependencies:** Checkpoints 1–2.

**Verification:** `uv run pytest tests/test_live_config_runtime.py tests/test_runtime.py tests/test_run_state.py -q`; `git diff --check`.

**Done When:** A fake multi-turn runner edits real temporary TOML during turn 1 and observes the change only on turn 2; retry prompts/settings also refresh. No fallback to historical configuration occurs. Every completed step is verified and scope checks pass.

**Blockers:** A persisted retry shape cannot reconstruct required template inputs: report the exact missing field rather than silently freezing retry prompts.

### [ ] Checkpoint 4: Refresh sessions and supervision without losing continuity

**Goal:** Live configuration reaches actual model calls, including same-name profile edits and cached supervisory components.

**Context:** Run `git rev-parse --show-toplevel`. Inspect `aflow/workflow.py` adapter/session setup, `_poll_live_control`, hotplug preparation, manager/repartition invoker construction; `aflow/hotplug.py`, `aflow/manager.py`, `aflow/manager_context.py`, `tests/test_hotplug.py` and `tests/test_manager.py`.

**Scope:** Execution integration and tightly related tests; no new provider integration, manager policy, upgrade routing or recovery protocol.

**Steps:**

- [ ] Refresh config-dependent invokers and closures from the boundary object, including enabled flags, manager role/skill selections, retry policy and prompt resolution. Preserve existing manager history, budgets and pending state; follow existing timing/routing semantics.
- [ ] Detect effective execution changes using resolved harness/model/effort values, not selector-name equality alone. An edited profile with the same name must change the next invocation's actual arguments. Retain an unchanged session; use existing supported model-switch or handover/new-session behavior for a changed execution target. Never mutate the in-flight call on save.
- [ ] Reuse existing session/hotplug continuity handling for cross-harness changes and pending transactions. Already-started handovers finish or recover under their captured invocation facts; subsequent target execution must reconcile current settings before launch. Do not duplicate the transaction machinery for configuration reloads. Keep current source session data long enough to hand over even when its profile was deleted from the catalog.
- [ ] Add fake-driver tests for newly added profile/harness selection, same-selector model/effort edit, changed prompt, unchanged session reuse, manager settings refresh without budget reset, and resume/retry during an existing handover.

**Dependencies:** Checkpoint 3.

**Verification:** `uv run pytest tests/test_live_config_runtime.py tests/test_hotplug.py tests/test_manager.py tests/test_manager_context.py tests/test_resume_manager_budget.py -q`; `git diff --check`.

**Done When:** Captured fake provider requests reflect live settings; prior turn evidence and continuity remain valid. Every completed step is verified and scope checks pass.

**Blockers:** A provider's existing driver cannot enact a changed setting or hand over: report its concrete limitation; do not silently reuse the old model or invent an unverified provider capability.

### [ ] Checkpoint 5: Validate controls against current configuration before writing

**Goal:** All remote controls accept new valid teams/profiles and reject unusable requests immediately without interrupting the run.

**Context:** Run `git rev-parse --show-toplevel`. Read `aflow/control_plane/AGENTS.md`, `apps/aflow_app/server/AGENTS.md`. Inspect `aflow/control_plane/services.py`, `application.py`, `persistence.py`, `capabilities.py`, `aflow/mcp_control_plane.py`, `apps/aflow_app/server/src/aflow_app_server/control_plane_service.py` and its exception mapping.

**Scope:** Shared control service and HTTP/MCP adapters/contracts/tests. Do not write controller state from transport or add a new endpoint/versioned config payload.

**Steps:**

- [ ] Inject the configured live source through the existing composition root. Share pure override-target validation with runtime so team/profile/current step rules agree; no provider subprocess in admission. Resolve request replacements before validating old selections; allow owner stop without loading TOML.
- [ ] After authorization/idempotent replay and before CAS persistence, load current config and validate the proposed effective controls. Invalid requests return the existing validation-error shape with actionable field/target details. No file bytes, revision or accepted-control event changes on rejection. Preserve stale-revision and request-idempotency semantics.
- [ ] Make capability/option reads use current definitions. HTTP and MCP use the same service behavior and existing error contract; correct field mapping where needed. Boundary revalidation remains mandatory because a later edit can invalidate an admitted request, handled nonfatally by checkpoint 3.
- [ ] Add service and HTTP/MCP tests for new team plus new profile, unknown target rejection, config edit between acceptance/application, stale revision, replay and owner stop with invalid config.

**Dependencies:** Checkpoints 2–3; use checkpoint 4 integration for end-to-end provider assertions.

**Verification:** `uv run pytest tests/test_control_plane_services.py tests/test_control_plane_capabilities.py tests/test_run_state.py apps/aflow_app/server/tests/test_control_plane_api.py -q`; `git diff --check`. Discover existing MCP tests with `rg --files tests apps/aflow_app/server/tests | rg 'mcp'` and run the returned relevant files with `uv run pytest`.

**Done When:** Invalid submissions leave a healthy controller and its prior override intact; current new teams are admitted consistently through both transports. Every completed step is verified and scope checks pass.

**Blockers:** Conflicting live-source injection ownership; do not fall back to a hardcoded home or repository path.

### [ ] Checkpoint 6: Make UI controls reflect live configuration

**Goal:** The user can save a team/profile, select it for an existing run, and understand when the change takes effect.

**Context:** Run `git rev-parse --show-toplevel`; read `apps/aflow_app/web/AGENTS.md`. Inspect `apps/aflow_app/web/src/components/RunDashboard.tsx`, its test, `api.ts`, current committed-settings loading and refresh ownership.

**Scope:** Existing settings/run control integration and tests; transport types only if checkpoint 5 changes require mirroring. No settings redesign or new acknowledgment flow.

**Steps:**

- [ ] Refresh committed options through the existing save/navigation/Refresh path; do not use snapshot catalogs. Preserve the user's draft and exact selected project/run identity. Ensure new saved teams/profiles appear without restarting the UI service.
- [ ] Show concise text that saved settings apply at the next turn and on resume. Distinguish selected pending controls from the last executed role/model evidence; no fingerprint displayed as an execution restriction.
- [ ] On rejected control submission, retain the draft, show the server's actionable error, leave previous selection/status intact and preserve established retry/idempotency behavior. No confirmation dialog for ordinary changes.
- [ ] Test save-new-team then select-on-existing-run, settings refresh without lost draft, rejection without a fabricated stopped status, and pending versus last-executed model display.

**Dependencies:** Checkpoint 5.

**Verification:** `npm --prefix apps/aflow_app/web test -- --run`; `npm --prefix apps/aflow_app/web run build`; `git diff --check`. Use a fake/local test backend for a Chromium smoke of Settings save → existing Run controls → accepted/rejected feedback; no production mutations.

**Done When:** Existing-run controls expose current options and accurately show success/error timing. Every completed step is verified and scope checks pass.

**Blockers:** No browser test environment available: record that limitation rather than claiming the smoke passed.

### [ ] Checkpoint 7: Shared dirty-worktree preflight and acknowledged startup

**Goal:** Inspect dirty paths once through shared domain logic and honor explicit continuation throughout CLI and lifecycle preparation.

**Context:** Run `git rev-parse --show-toplevel`. Inspect `aflow/api/startup.py` (`_check_worktree_dirtiness`, `prepare_startup`, `prepare_startup_with_answer`), `aflow/api/models.py`, `aflow/workflow.py` (`_lifecycle_preflight`), `aflow/git_status.py`, `aflow/run_state.py`. Existing non-plan dirt in a worktree workflow raises before the normal confirmation question; a second guard exists in lifecycle preflight.

**Scope:** Shared startup/domain models and lifecycle startup wiring; focused tests in new `tests/test_dirty_worktree_preflight.py`. No HTTP or UI code yet, no merge/teardown relaxation.

**Steps:**

- [ ] Add a small typed read-only preflight result containing `checkout_path`, `execution_mode` (`same_checkout` or `new_worktree`), `dirty`, `requires_confirmation`, `blockers`, `total_items`, and ordered items `{path, original_path?, index_status, worktree_status}`. Use one NUL-aware status parser and the existing lifecycle-artifact classification. Handle Git failures as inspection errors, never as clean status. Sort by path for predictable display.
- [ ] Make startup reuse this result. Return the existing `CONFIRM_WORKTREE_DIRTY` question for confirmation-required dirt when unacknowledged, including worktree workflows currently hard-rejected. Preserve CLI yes/no handling; a positive answer carries the existing boolean and a negative answer aborts normally.
- [ ] Thread the boolean through prepared/controller launch inputs and the duplicate lifecycle startup check. Bypass only acknowledged dirty-path prohibitions, not branch/HEAD identity, unresolved-conflict, in-progress-operation or teardown checks. Recheck at final preparation. A fresh worktree receives the selected committed tree; source modifications remain untouched.
- [ ] Test tracked/staged/untracked/deleted/renamed files and newline/space filenames; plan-only/lifecycle-owned exclusions; clean checkout; positive/negative acknowledgment; dirt appearing after preflight; unresolved conflicts; and fresh-worktree launch preserving source bytes.

**Dependencies:** Checkpoint 2 startup changes; independent of runtime session integration.

**Verification:** `uv run pytest tests/test_dirty_worktree_preflight.py tests/test_cli.py tests/test_runtime.py -q`; `git diff --check`.

**Done When:** The shared result accurately lists dirt; CLI confirmation can start an eligible dirty workflow and lifecycle validation honors it without altering dirty files. Every completed step is verified and scope checks pass.

**Blockers:** If an existing lifecycle action would overwrite dirty files despite acknowledgment, retain that specific refusal and report it; do not implement automatic cleanup.

### [ ] Checkpoint 8: Expose read-only preflight and launch acknowledgment through the control plane

**Goal:** Remote callers can inspect dirt before reserving a run and submit the same choice as CLI callers.

**Context:** Run `git rev-parse --show-toplevel`; read control-plane/server nested guidance. Inspect `aflow/daemon.py` startup serialization, `aflow/control_plane/models.py`, `aflow/mcp_control_plane.py`, and server `control_plane_service.py`, `models.py`, `main.py` under `apps/aflow_app/server/src/aflow_app_server/`.

**Scope:** Domain-service/API wiring and transport contracts/tests. No frontend yet and no new ownership/database layer.

**Steps:**

- [ ] Add authenticated read-only `POST /api/control-plane/projects/{project_id}/runs/preflight` before any conflicting dynamic run route. Accept the same launch-selection fields as StartRunPayload; inspection ignores acknowledgment. Resolve project/plan/workflow through existing registry and path rules, delegate to checkpoint 7, and never call reservation/launch/startup-write functions. Report only repository-relative dirty paths plus the inspected checkout path, never file contents.
- [ ] Bound response items with `offset` (default 0) and `limit` (default 200, maximum 1000) request fields and `next_offset` response field alongside `total_items`; all items remain inspectable through subsequent calls. No cursor database or content hashes. Each call describes the current read; no promise of an immutable inventory.
- [ ] Add optional `dirty_worktree_confirmed` defaulting false to the existing start payload and shared service arguments; carry it through daemon request serialization, request digest/replay and preparation. Use the same boolean on restart-prefilled new launches. Preserve older clients: unacknowledged dirt yields the existing answerable startup-question response rather than direct failure. No worker launches before required confirmation.
- [ ] Expose the same read-only preflight and boolean through the existing MCP tools, using the shared service; mirror canonical models and validation errors. Keep authentication, registry constraints, response bounds and idempotency behavior.
- [ ] Add service/HTTP/MCP tests proving preflight creates no artifacts or units, paging covers all files, true/false launch behavior, conflict refusal, replay with a changed boolean rejected as a different request, and inspection/launch races rechecked correctly.

**Dependencies:** Checkpoint 7; coordinate existing control-service changes from checkpoint 5 without duplicating source resolution.

**Verification:** `uv run pytest tests/test_dirty_worktree_preflight.py tests/test_control_plane_services.py tests/test_daemon_cli.py apps/aflow_app/server/tests/test_control_plane_api.py -q`; run relevant existing MCP tests discovered in checkpoint 5; `git diff --check`.

**Done When:** An authenticated remote caller obtains a full inspectable dirty list without creating a run, then launches with an explicit boolean and no unconditional dirty-worktree failure. Every completed step is verified and scope checks pass.

**Blockers:** An existing startup endpoint cannot return its structured question through a transport: fix that mapping in scope rather than return a generic failed run.

### [ ] Checkpoint 9: Add dirty-worktree list and continuation checkbox to New Run

**Goal:** New Run makes the dirt and its launch effect visible before the user chooses to continue.

**Context:** Run `git rev-parse --show-toplevel`; read web guidance. Inspect `apps/aflow_app/web/src/components/NewRunPage.tsx`, its RunDashboard-owned state/launch wiring, `api.ts`, and existing component tests. Reuse the actual shared launch form for ordinary and restart-prefilled starts.

**Scope:** New Run presentation/state/API types and frontend tests, not a new file editor or cleanup tool.

**Steps:**

- [ ] Fetch read-only preflight when project/plan/workflow selection becomes valid and through a visible Refresh action. Discard late results for a different selection. Render dirty items as status plus literal filename, include rename source, and provide “Show more” using `next_offset`. Never claim a clean tree on loading/inspection error.
- [ ] When acknowledgment is required, show an unchecked checkbox labeled “Continue despite uncommitted changes”. Place the checkout/execution-mode explanation beside the list: existing-checkout work sees the dirt; a new worktree starts from the selected commit and leaves those changes here. No generic danger modal or extra confirmation button.
- [ ] Disable Start while preflight is loading/failed/blocked or required acknowledgment is unchecked. Submit `dirty_worktree_confirmed` with Start; reset it for changed project/plan/workflow/restart source, not on unrelated draft edits. Retain existing mutation-key handling. Recheck server response: if previously clean dirt appears at launch, render the returned dirty question/list/checkbox inline and use the existing startup-answer mechanism to continue without allocating another run.
- [ ] Test clean launch, each dirty category display, paging, unchecked/checked behavior, selection changes during loading, source-dirt-new-worktree explanation, server-side inspection failure, and the clean-preflight/dirty-launch race. Verify keyboard checkbox/Start behavior in Chromium with a fake backend and temporary repositories.

**Dependencies:** Checkpoints 6 and 8.

**Verification:** `npm --prefix apps/aflow_app/web test -- --run`; `npm --prefix apps/aflow_app/web run build`; `git diff --check`; Chromium New Run smoke as specified above, with no production mutations.

**Done When:** The user sees the dirty list in New Run and can explicitly continue through one checkbox; real source files remain untouched by preflight and submission. Every completed step is verified and scope checks pass.

**Blockers:** Missing browser test environment must be recorded; do not claim screenshots/interactive checks were performed.

### [ ] Checkpoint 10: Give checkpoint reviewers the pending implementation target

**Goal:** A normal checkpoint review examines the just-implemented unapproved checkpoint, including worktree-first attempts, rather than repeating an earlier approval.

**Context:** Run `git rev-parse --show-toplevel`. Inspect `aflow/workflow.py` scope opening/awaiting-review state, review prompt assembly and `_close_implementation_scope`; `aflow/run_state.py` implementation scope/attempt metadata; `aflow/bundled_skills/aflow-review-checkpoint/SKILL.md`; `tests/test_runtime.py`, `tests/test_skill_install.py`. Review the incident description in Summary; use synthetic fixtures, not external recovery files.

**Scope:** Controller prompt context and bundled review instructions, with focused tests. No new manager selection, approval protocol, commit naming scheme, review database or hard gate based on arbitrary prose.

**Steps:**

- [ ] Implement invariant 13 using the existing awaiting-review scope and finalized worker evidence. Append a compact “Checkpoint under review” section to actual reviewer prompts with the original checkpoint index/name, active overlay when present and worker artifact reference. Do this for both ordinary and resumed review invocations; do not render `{NEXT_CP}` as review target when it names future implementation work.
- [ ] For recovered runs without a scope, read existing worker result metadata and the original plan's existing review markers/log to choose the pending target. If no unambiguous target exists, explain the missing information and allow an explicit user target rather than quietly repeating the newest commit. Preserve incomplete/non-checkpoint plan behavior outside checkpoint review.
- [ ] Update bundled reviewer target selection: explicit user target first; otherwise supplied pending target; otherwise next implemented-but-unapproved checkpoint from existing ledger/evidence. A `cpN vNN` commit already recorded approved is a base for pending dirty work, not the default review target. Retain commit-boundary review when that commit actually represents the pending checkpoint. Do not edit installed user skill copies as part of source implementation.
- [ ] Test CP6 approved/CP7 dirty/first unchecked CP8; a pending repair overlay; normal worker→review scope; and stopped-run resume without active scope. Assert captured review prompt selects CP7 and its original heading and does not select CP6 or CP8. Keep review rejection/next-step behavior unchanged.

**Dependencies:** Checkpoints 3–4 for refreshed prompt/session assembly. This checkpoint is independent of dirty-worktree UI.

**Verification:** `uv run pytest tests/test_runtime.py tests/test_run_state.py tests/test_skill_install.py -q`; `git diff --check`. A fake multi-turn worker/reviewer fixture must capture target progression across two successive checkpoints.

**Done When:** Prompt evidence identifies pending work even when the plan has advanced implementation markers; bundled selection rules agree with it. Every completed step is verified and scope checks pass.

**Blockers:** No unambiguous pending target in a legacy fixture: expose that actual ambiguity rather than invent a numeric fallback or new approval ledger.

### [ ] Checkpoint 11: Deliver one-turn recovery notes to the selected step

**Goal:** A one-turn recovery note can target a reviewer without leaking into later turns.

**Context:** Run `git rev-parse --show-toplevel`. Inspect existing override note consumption in `aflow/workflow.py` and `aflow/run_state.py`, associated runtime tests and `docs/runtime-behavior.md`. Read relevant nested guidance. Local CLI replacement/clearing is already fixed and must be preserved.

**Scope:** Prompt-note consumption and its existing durable state plus tests/docs. Do not modify HTTP/MCP schemas in this checkpoint.

**Steps:**

- [ ] Extend existing `notes` + `next_step` processing so reviewer-targeted recovery notes appear in that review invocation and are consumed with it. Without `next_step`, keep next-worker delivery. Reuse existing durable request/result and pending-note fields, adding only a small target-step field if needed for resume; legacy absent target means next worker. Avoid replay after a completed targeted turn, including a successor resume; retain notes after a prelaunch failure so recovery is not lost.
- [ ] Test reviewer notes delivered once; unrelated step does not consume them; preflight failure does not consume; completed review plus resume does not replay; actual successor prompt preserves global text independently. Preserve existing worker-note tests and predecessor evidence.
- [ ] Document run-wide versus one-turn lifetimes in `docs/runtime-behavior.md`, the assistant engine reference and existing CLI help examples. Use `next_step = "review_checkpoint"` plus `notes` for one review's recovery directions; do not suggest putting a fixed checkpoint recovery instruction into global text.

**Dependencies:** Checkpoint 3 boundary overrides. Compatible with checkpoint 10 but does not depend on its target inference.

**Verification:** `uv run pytest tests/test_run_state.py tests/test_runtime.py tests/test_control_plane_resume.py -q`; `uv run ruff check aflow`; `git diff --check`.

**Done When:** A review-only note reaches the selected reviewer and does not leak into later checkpoints. Every completed step is verified and scope checks pass.

**Blockers:** Conflicting existing note-consumption behavior must be resolved using the stated lifetime rules, not by silently dropping or replaying notes.

### [ ] Checkpoint 12: Allow instruction replacement through remote resume

**Goal:** Explicit REST/MCP resume provides the same inherit/replace/clear semantics as the corrected CLI.

**Context:** Run `git rev-parse --show-toplevel`. Read relevant nested guidance. Inspect local CLI changes in `_bootstrap_resume_invocation`, `_detect_resume_candidate` and `_resume_candidate_mismatch_reason`; resume services in `aflow/daemon.py`, `aflow/mcp_control_plane.py` and server `models.py`/`control_plane_service.py`/`main.py`.

**Scope:** Existing resume service and transport contracts/tests; no new UI editor and no note-state changes.

**Steps:**

- [ ] Preserve and verify the local CLI fix: omitted `--` text inherits, `-- replacement` replaces, bare `--` clears. Keep predecessor `run.json` and old turn artifacts untouched; the successor records its effective text. Keep automatic fresh-run candidate matching unchanged.
- [ ] Add optional `extra_instructions` to the existing explicit resume request/service interfaces: omitted or null inherits, a bounded string list replaces, `[]` clears. Reuse existing fresh-launch string/count validation and request-idempotency calculation; carry the choice through daemon bootstrap into the actual worker. Do not compare replacement text against predecessor identity as a rejection condition. Retain workflow/plan/repository ownership checks and current UI omission behavior.
- [ ] Test inheritance/replacement/clearing through CLI, daemon, HTTP and MCP; assert actual successor instructions and captured prompt, predecessor unchanged, invalid payload rejected and changed replacement text not accepted under an already-used different request's idempotency key.
- [ ] Update existing resume documentation and assistant reference with the same three-way semantics; distinguish these run-wide instructions from checkpoint 11's one-turn notes.

**Dependencies:** Checkpoint 2 current-source resume wiring; independent of checkpoint 11 note delivery.

**Verification:** `uv run pytest tests/test_cli.py tests/test_control_plane_resume.py tests/test_control_plane_services.py apps/aflow_app/server/tests/test_control_plane_api.py -q`; relevant existing MCP contract tests; `uv run ruff check aflow apps/aflow_app/server/src`; `git diff --check`.

**Done When:** A remote caller can clear or replace stale instructions without changing the worktree or predecessor, matching tested CLI semantics. Every completed step is verified and scope checks pass.

**Blockers:** Conflicting request ownership must use the existing idempotency contract; do not bypass authorization or rewrite predecessor metadata.

### [ ] Checkpoint 13: Verify legacy recovery and remove stale restriction contracts

**Goal:** Demonstrate the original failure is fixed across entrypoints and remove leftover execution gates and misleading documentation.

**Context:** Run `git rev-parse --show-toplevel`. Inspect `README.md`, `ARCHITECTURE.md`, `DEVLOG.md`, `docs/configuration.md`, `docs/runtime-behavior.md`, `aflow/AGENTS.md`, `aflow/bundled_skills/aflow-assistant/references/engine-features.md`; search `rg -n 'frozen.configuration|frozen.config|config_fingerprint|snapshot.*checksum|snapshot.*modified|outside frozen' aflow apps/aflow_app tests docs README.md ARCHITECTURE.md`.

**Scope:** Integration tests, documentation and removal of dead config-only compatibility gates/helpers. No unrelated fingerprint removal, no root AGENTS.md edit and no changes to other pending plans.

**Steps:**

- [ ] Add a sanitized temporary fixture reproducing the incident: old snapshot lacks MusparkGLM, live config adds it and a new profile, revisioned team control arrives during a fake review, next execution uses the new target. Repeat through stopped-run CLI and daemon resume; preserve pending checkpoint/progress.
- [ ] Cover absent/malformed snapshot manifests, disagreeing old hashes, live config relocation and an explicit edited config copy. Historical damage must not affect current-source launch. Missing/malformed current config must still fail clearly without a harness launch.
- [ ] Audit every search hit: remove remaining config equality/catalog gates and tests requiring them; keep deprecated serialized names only where needed for compatibility. Preserve non-config ownership/security/idempotency hashes. Remove unused snapshot/fingerprint helpers rather than maintaining competing authority paths.
- [ ] Update the documents above in the same pass, with a concise DEVLOG entry. Explain precedence, next-turn timing, legacy explicit-value default, retained lifecycle facts, invalid-request behavior and correction/resume. Document dirty-worktree preflight/checkbox and the difference between using an existing checkout and starting a fresh worktree from committed content. Update an existing relevant README section only; if none needs changing, record why. Update nested guidance that demands snapshot execution. Leave historical completed plans alone.
- [ ] Run final verification once; fix failures in scope, then repeat only affected checks. Record actual results and limitations in the plan ledger.

**Dependencies:** Checkpoints 1–12.

**Verification:** `uv run pytest tests apps/aflow_app/server/tests -q`; `uv run ruff check aflow apps/aflow_app/server/src`; `npm --prefix apps/aflow_app/web test -- --run`; `npm --prefix apps/aflow_app/web run build`; `git diff --check`.

**Done When:** The reproduced run continues/resumes using current settings with no hash repair, import step or fresh plan. Source audit shows no snapshot/config hash gates remain. Every completed step is verified and scope checks pass.

**Blockers:** Pre-existing unrelated suite failures must be identified separately, not fixed by expanding scope or weakening tests.

## Behavioral Acceptance Tests

- **A1 — Original incident:** Given a running review and an old snapshot without MusparkGLM, add the team and a new profile globally and submit the team control. The review finishes under its original model; the next turn uses the new profile and never enters `waiting_for_valid_override` for frozen-catalog absence.
- **A2 — Automatic edits:** Edit the current profile's model/effort without renaming it, plus its prompt. Next invocation's recorded actual arguments and prompt change; the in-flight call does not. A second unchanged boundary reuses compatible session state.
- **A3 — Defaults versus explicit intent:** Change workflow default team and global max turns. Default-following runs update; explicitly selected values remain, with live definitions. Increase a default at its old limit and another turn can start; lower it to the completed count and no extra call starts.
- **A4 — Resume compatibility:** Existing run with old, edited, missing or broken snapshot artifacts resumes from current configuration and saved progress through CLI/UI/MCP. Changed source location alone is not a mismatch. Explicit target corrections can repair a removed saved team/step.
- **A5 — Material invalidity:** Invalid live TOML, unknown current workflow or uncorrected deleted active target launches no new harness and names the real problem. Correct it and resume successfully. Owner stop still works with broken TOML.
- **A6 — Control errors:** Unknown team/profile submitted remotely returns validation error and leaves file/revision unchanged. If a once-valid pending request becomes invalid before application, it is recorded rejected once and usable previous choices continue. A corrected request applies normally.
- **A7 — Retry and supervision:** Edit settings after a failed call. Retry uses the new role/profile/prompt with saved valid plan context and retained attempts. Manager configuration refreshes without resetting budget/history. Pending handover recovers without duplicate model calls or discarded continuity.
- **A8 — Workflow and repository facts:** Edit a future step definition/graph and observe it at the next applicable turn. Changing worktree-root/branch/merge defaults never relocates the existing worktree or repeats setup. Same-run duplicate launch and wrong-worktree resume remain rejected.
- **A9 — Consistent reads and controls:** Save the split pair concurrently with a boundary load; observe a complete old or new pair. Stale control revisions cannot overwrite newer edits. Replayed launch requests cannot create another controller.
- **A10 — Historical evidence:** Read completed-turn artifacts after a settings edit; they retain the actual old model/prompt. New-turn artifacts contain the new execution details. No misleading claim that the historical launch copy describes all turns.
- **A11 — Dirty preflight:** Select a new-run plan with staged, unstaged, renamed and untracked files. Preflight lists their exact paths/statuses, creates no run or files, and leaves Start disabled until the checkbox is selected. Submit checked and the eligible run starts with all source dirt preserved; unmerged conflicts remain blocked.
- **A12 — Dirty scope/races:** Change project after checking the box: it resets and stale preflight results are discarded. Dirt appearing after a clean preflight yields an inline answerable question, not a dead failed run. Checked fresh-worktree launch starts from its selected commit while explicitly leaving source dirt in the source checkout.
- **A13 — Pending review target:** CP6 has an approval commit; worker completes CP7 as dirty changes and plan points at CP8. The next reviewer prompt names CP7 and its worker attempt, treating CP6 as the base. Repeat after resume with no active scope; do not re-review CP6 merely because it is the latest checkpoint commit.
- **A14 — Recovery instruction lifetime:** Resume with omitted/changed/empty instructions and observe inherited/replaced/cleared successor text, with predecessor unchanged. Submit review-targeted notes: exactly that review receives them; no later worker/reviewer or resumed successor replays consumed text.

## Plan-to-Verification Matrix

| Requirement | Verification |
| --- | --- |
| Current source, no snapshot integrity gates | CP1–2 and CP13; A4–A5 |
| Automatic next-turn config and precedence | CP3; A1–A3, A8 |
| Actual session/retry/manager refresh | CP3–4; A2, A7 |
| Immediate nonfatal control rejection | CP3 and CP5; A6, A9 |
| Current UI choices and clear timing | CP6 DOM tests and Chromium smoke; A1, A6 |
| Preserve progress, ownership and concurrency | CP2–5 regressions; A4, A8–A9 |
| Dirty-worktree choice without lost work | CP7 domain and CP8 transport tests; A11–A12 |
| New Run dirty list and checkbox | CP9 frontend tests and Chromium smoke; A11–A12 |
| Correct pending review target | CP10 runtime prompt/skill tests; A13 |
| Scoped recovery directions | CP11 runtime/note-lifetime tests; A14 |
| Replaceable resume instructions | CP12 CLI/API tests; A14 |
| Accurate history and documentation | CP13 source/doc audit and integration; A10 |

## Assumptions And Defaults

- The owner explicitly chose automatic current-settings reload for existing runs and resume. No config opt-in, enterprise reproducibility mode, extra config confirmation or revision framework is wanted. The owner separately and explicitly requested a dirty-worktree preflight list and continuation checkbox in New Run; implement that choice without extending it to configuration changes.
- Configuration edits are intentional operator actions. Keeping valid TOML/reference checks has a direct execution benefit; preventing valid drift does not. Diagnostic byte checksums are removed from config snapshots; unrelated integrity checks are not included in this scope.
- Existing selected workflow, plan progress and actual repository/lifecycle facts continue to identify the work. Flexibility in future execution settings does not imply moving/deleting a live worktree or swapping to another plan/workflow.
- Invalid global configuration can genuinely make the next turn unexecutable; a clear failure with resumable state is the default. Invalid optional control requests alone do not justify stopping otherwise usable work.
- Existing tests use injected in-memory configs and fake runners. Preserve that explicit test seam; do not let a missing-file test bypass become a production fallback. New live-reload acceptance tests must exercise actual temporary TOML reads.
- All commands run from the implementation checkout. No paid provider calls, production run recovery, shared-tool replacement, deployment, public publishing or broad policy cleanup is part of verification.
