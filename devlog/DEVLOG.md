## 2026-07-29 — Durable live review-rejection history

- Added controller-owned, resume-safe rejection records to turn artifacts and
  `run.json`, preserving legacy artifact analysis and manager upgrade timing.
- The Rich banner now lists every current-scope rejection and explains the
  latest re-implementation without parsing raw output during refresh.
- Added summary and rendering regression coverage; the root suite retains its
  existing app-server dependency collection baseline.

## 2026-07-29 — Finalized-turn manager-boundary resume

### What changed

- Detected a completed active turn that is newer than the controller boundary
  persisted in `run.json`.
- Restored its snapshot, repair plan, and transition on resume, then ran the
  missing manager gate against the immutable source-run artifacts before any
  new harness turn.
- Cleared stale one-hop state from the earlier boundary and avoided
  double-counting the recovered review rejection.

### Why

- A live controller was interrupted after a rejected CP11 review finalized but
  before its manager gate persisted. Resume used the older accepted CP10
  boundary and incorrectly launched the next checkpoint's baseline worker,
  skipping the CP11 repair upgrade.

## 2026-07-29 — Follow-up review rejection and reset-scope correction

### What changed

- Counted a reviewer-created follow-up plan as a rejection even when review
  reopens the original checkpoint instead of leaving its snapshot unchanged.
- Recomputed active-scope rejection counts from durable source-turn artifacts
  on resume, repairing runs written before the classifier correction.
- Cleared the live implementation-attempt index during owner-authorized scope
  reset so restarting the same checkpoint cannot collide with its old scope ID.

### Why

- A live CP9 rejection moved the original plan from CP10 back to CP9 and created
  a focused overlay. The unchanged-snapshot-only classifier reported zero
  rejections, so Lite incorrectly retained DS4-pro despite an eligible Terra
  upgrade.

## 2026-07-29 — Fenced Lite manager decision normalization

### What changed

- Accepted one exact `json`-tagged Markdown fence around an otherwise valid
  manager decision object.
- Continued rejecting prose, untagged or multiple fences, multiple objects,
  malformed JSON, and protocol-invalid decisions.
- Added parser and Reasonix Lite runtime coverage proving a fenced final
  response does not spend an unnecessary Full-manager call.

### Why

- A live Lite manager returned the correct closed-protocol decision inside a
  `json` fence despite the no-fence instruction. Strict transport parsing
  escalated healthy CP9 progress to Full unnecessarily.

## 2026-07-29 — Owner-authorized checkpoint repartition resume

### What changed

- Added `--resume-reset-scope`, valid only with an explicit `--resume RUN_ID`.
- Preserved worktree lifecycle identity, manager decision history, and
  historical implementation attempts while clearing obsolete checkpoint scope,
  pending routing, active repair overlay, interrupted step, and report pointer.
- Added CLI parsing and state-boundary regression coverage.

### Why

- A legitimate Full-manager stop can ask the owner to repartition an oversized
  checkpoint. Reusing the durable worktree must not also revive the rejected
  repair overlay and exhausted rejection state, and operators should not need
  to edit `run.json` manually.

## 2026-07-28 — Repair approval active-plan restoration

### What changed

- Generated follow-up paths against the execution worktree's existing overlays.
- Excluded pre-existing repair overlays from `NEW_PLAN_EXISTS` after review.
- Added a resumed-worktree regression proving an approved repair returns the
  next checkpoint worker to the original plan.
- Normalized already-affected durable runs on resume when a completed repair
  overlay remained active after its original checkpoint advanced, including
  the non-checkpoint checklist format used by focused repair plans.

### Why

- A resumed CP3 approval reused the existing `cp03-v01` overlay as its proposed
  follow-up path, misclassified it as newly created, and launched CP4 with the
  completed CP3 repair plan.

## 2026-07-28 — Durable lifecycle metadata preservation

### What changed

- Preserved existing worktree lifecycle identity when manager or status
  boundaries rewrite `run.json` without repeating the execution context.
- Added a regression covering the exact manager-style follow-up write.
- Persisted lifecycle identity immediately after setup and restored an
  interrupted `starting` step on resume instead of resetting to the CLI start
  step.

### Why

- A real interrupted run lost its worktree, branch, setup, and teardown fields
  after a manager boundary and was incorrectly rejected as non-resumable. Once
  repaired, resume also skipped the unfinished reviewer and restarted the
  implementor.

## 2026-07-28 — First-rejection worker upgrade policy

### What changed

- Tightened the inline and bundled interstep-manager policy so Lite selects an
  eligible one-edge worker upgrade immediately after the first reviewer
  rejection, before Full-manager escalation.
- Added prompt coverage for the DS4-pro to Terra-style upgrade boundary.

### Why

- A real run spent its first repair attempt on the baseline Lite worker even
  though the controller exposed a valid upgrade, leaving Full to upgrade only
  after the second rejection.

## 2026-07-28 — Legacy scoped-rejection resume correction

### What changed

- Recognized active implementation scopes without the durable carried-rejection
  field as legacy and discarded their potentially run-wide top-level count.
- Added state restoration and end-to-end resume coverage proving an earlier
  checkpoint's two rejections do not force Full for the current checkpoint.

### Why

- Pre-scoped run metadata could otherwise poison a resumed checkpoint and make
  Full stop it before that checkpoint accumulated its own rejection evidence.

## 2026-07-28 — Manager runtime review follow-up

### What changed

- Counted unchanged reviewer outcomes directly so the second rejection reaches
  Full before a third worker starts.
- Rebased active-scope turn numbering on resume and carried accumulated
  rejection progress without double-counting new-run reviews.
- Versioned new manager boundaries, captured immutable structured plan state,
  and restored exact legacy behavior for unversioned null-scope artifacts.
- Added real Rich-banner plus CLI regressions for accepted stop and invalid
  Lite-to-Full fallback, including the wrapper's later status line.

### Why

- Review found delayed non-convergence selection, lost resume progress, context
  drift for legacy null scopes and advanced plans, and insufficient terminal
  rendering coverage.

## 2026-07-28 — Manager runtime regression repair

### What changed

- Added Reasonix final-response-only argv for manager calls while preserving
  ordinary workflow transcripts and strict JSON parsing.
- Separated whole-run checkpoint stability from same-step stalls, and scoped
  reviewer rejection counters to the durable original-checkpoint opening turn.
- Finalized successful turn artifacts as `completed` with one shared finish
  timestamp and duration across artifacts, in-memory records, events, and
  manager context.
- Persisted and emitted manager-gate failures, stopped the live banner, then
  raised for the CLI to print the report once.
- Added focused transport, parser, analysis, scope, duration, and runtime
  regression coverage.

### Why

- Live workflow validation exposed noisy Reasonix stdout, false Full selection
  during normal step alternation, rejection history leaking into the next
  checkpoint, stale `running` turn results, and manager reports erased during
  terminal renderer teardown.

## 2026-07-28 — Reasonix and terminal manager hardening

### What changed

- Replaced the obsolete Reasonix `-dir` argument with the supported `--dir`
  spelling so the CLI reaches agent execution instead of failing argument
  parsing.
- Made `[manager].skill` effective in manager prompts and embedded exact normal
  and stop JSON shapes so strict protocol validation does not depend on an
  installed skill.
- Kept the original terminal workflow incident primary in deterministic manager
  reports while recording any secondary manager protocol error separately.
- Added adapter, prompt-contract, and terminal-report regression coverage for
  the observed zero-output Reasonix and malformed-stop response.

### Why

- Reasonix v1.17.15 rejects `-dir` before agent execution, and the prior terse
  manager prompt allowed a semantically correct stop decision to serialize
  `next_step_notes` and `stop_report` with invalid types.

## 2026-07-28 — Chained worker upgrades within one checkpoint

### What changed

- Added a durable original-checkpoint implementation scope that survives repair
  plan changes, repeated review cycles, and resume.
- Keyed actual worker attempt history by that scope, separated one-hop target
  plan validation from upgrade lineage, and normalized resumed histories to
  mutable lists.
- Exposed compact scope depth, teams, selectors, and the most recent reviewed
  worker in manager context.
- Added resolver, state/resume-boundary, context, and end-to-end runtime
  coverage for `default → strong → stronger`, followed by a baseline worker on
  the next checkpoint.
- Updated the README, configuration guide, runtime guide, architecture notes,
  manager skill, and bundled TOML example to describe the same chained,
  checkpoint-scoped behavior.

### Why

- Mutable repair-plan/checkpoint identities could lose the upgraded worker
  attempt and incorrectly offer the first upgrade edge again.

## 2026-07-27 — Interstep manager supervision

### What changed

- Documented the opt-in manager configuration, Lite and Full evidence boundary,
  role resolution, escalation, and the strict post-turn gate.
- Documented step-scoped quality upgrades separately from operational backup
  recovery, including resume-safe note and override consumption.
- Added artifact, report, CLI/API, default skill-installation, architecture,
  and assistant-diagnostics guidance for manager-supervised runs.
- Added immutable manager-boundary artifacts and rebuilt CLI/API manager
  analysis from those durable inputs, failing loudly when stored context
  drifts.
- Restored accepted one-hop routing before resume, routed operational recovery
  through legal manager actions, and send same-step caps directly to one Full
  terminal decision.

### Why

- Operators need a self-contained route from a stopped run to its report and
  reproducible context without scanning large raw streams.
- Keeping Lite free of plan prose and upgrades limited to one implementation
  attempt makes the cost and routing boundaries auditable.

### Verification

- Focused manager, recovery, resume, same-step, stop, merge, CLI, and API suites
  pass.
- The remote-app server suite passes in its project environment. The root
  affected suite retains pre-existing configuration, pre-handoff refresh, and
  bootstrap lifecycle failures.

## 2026-07-23 — Preserve active repair plans across review transitions

### What changed

- Added opt-in `preserve_active_plan` policy to non-`END` workflow transitions.
- Active-plan selection now gives a newly created plan precedence, preserves an
  existing repair plan only on opted-in transitions, and otherwise retains the
  compatibility behavior of resetting to the original plan.
- Added missing-file guards for preserving and resumed worktree paths and marked
  the bundled implementation/follow-up-to-review edges that require retention.

### Why

- Review-created repair plans were being discarded after a successful rework
  turn with `NEW_PLAN_EXISTS = false`, causing review/rework loops to alternate
  against the wrong plan.
- `NEW_PLAN_EXISTS` is a turn event; persistent active-plan state must be decided
  explicitly by the selected transition.

## 2026-04-07 — Workflow show command and excluded-step documentation (Checkpoint 4)

### What changed

- Added `aflow show [workflow_name]` to render workflow diagrams plus the applicable role/team relationships from the loaded config.
- Documented `exclude = ["step_name"]` in the bundled workflow examples so declared steps stay visible in `aflow show` and the live banner while being removed from the executable graph.
- Documented the issue summary link flow so the banner and affected turn cards point at `.aflow/runs/<run-id>/issues.md` when issues exist.

### Why

- The CLI needs a read-only way to inspect the same workflow semantics the live banner uses.
- Exclusions only make sense if the declared graph remains visible in the docs and diagrams.
- The issue summary path should be discoverable from the docs rather than reconstructed from runtime state.

## 2026-04-07 — Harness recovery docs and public analyze API (Checkpoint 5)

### What changed

- Documented config-driven harness recovery in README.md and ARCHITECTURE.md, including ordered recovery rules, `backup_team` chaining, progress gating, and the bundled `aflow-harness-recovery-lead` fallback skill.
- Updated the bundled skill inventory docs to list `aflow-harness-recovery-lead` as part of the nine default bundled skills.
- Added README coverage for the public `AnalyzeRequest` / `analyze_runs()` API so callers can use run analysis without shelling out to `aflow analyze`.

### Why

- Users need the documented recovery flow to match the runtime behavior that was added in earlier checkpoints.
- The public library surface should describe analysis entry points alongside the CLI entry point.

### Gotchas

- Recovery only applies when the harness turn did not advance the plan snapshot.
- The fallback recovery path expects the bundled skill contract, not ad hoc prose.

## 2026-04-07 — Audio transcription and documentation (Checkpoint 6)

### What changed

- Added `transcription.py` module with OpenAI-compatible transcription client
- Implemented `POST /api/transcribe` endpoint for browser-recorded audio clips
- Updated `AudioRecorder` component to record, upload, and insert transcripts
- Integrated audio recorder into `Composer` component with graceful degradation
- Added comprehensive tests for transcription client and API endpoint
- Updated documentation in README.md, ARCHITECTURE.md, and apps/aflow_app/README.md
- Added `python-multipart` and `pytest-asyncio` dependencies to server

### Why

- Voice input improves mobile usability for plan creation and chat
- Upload-based transcription is simpler than streaming and works with standard APIs
- Graceful degradation ensures text-only usage remains fully functional
- OpenAI-compatible API format supports multiple transcription backends

### Gotchas

- Transcription requires explicit configuration (URL and token) to be enabled
- Uploaded audio files are stored temporarily and cleaned up after transcription
- The transcription endpoint returns 503 when not configured, not 404
- Browser MediaRecorder API requires HTTPS in production (localhost is exempt)
- Audio recording requires microphone permission from the user

## 2026-04-07 — Remote app server scaffold (Checkpoint 3)

### What changed

- Created `apps/aflow_app/server/` as a separate Python subproject with FastAPI backend
- Implemented repository registry with file-backed JSON storage for managing multiple repos
- Added authenticated REST API endpoints for repos, plans, and workflow execution
- Integrated `aflow.api` library for startup preparation and workflow execution
- Implemented SSE streaming for execution events using `ExecutionObserver`
- Added comprehensive test suite for registry and API endpoints
- Server is excluded from root `aworkflow` wheel package

### Why

- Non-CLI callers need a way to manage multiple repos and execute workflows remotely
- The server demonstrates how to use `aflow` as a library rather than a CLI
- SSE provides real-time execution updates without terminal scraping
- Separate subproject allows independent versioning and deployment

### Gotchas

- The server requires explicit token authentication for all API operations
- Repository registry validates git roots but also accepts non-git directories
- Plan parsing failures are silently ignored when listing plans
- The server is designed for local/LAN use, not internet-facing deployment
- Server tests use direct global state injection rather than lifespan context managers

## 2026-04-07 — Library API surface and CLI shell

### What changed

- `aflow.api` public surface for startup preparation and workflow execution; CLI converted to a thin terminal adapter; structured execution events and observer pattern; documentation updates.

### Why

- Non-CLI callers (future daemon, web, Codex adapter) need to import and use aflow without invoking `aflow.cli.main()` or requiring a TTY.

### Gotchas

- The library API does not handle TTY prompts; callers must render `StartupQuestion` objects themselves.

## 2026-04-06 — Startup refresh guard for stale pre-handoff base HEAD

- `aflow` now checks live `## Git Tracking` metadata before startup prompts when the original plan contains it. A pristine plan with an empty or stale `Pre-Handoff Base HEAD` can ask for confirmation and refresh that field to the current `git rev-parse HEAD` value.
- The approved rewrite is deferred until after lifecycle setup creates the execution context, then applied before the first prompt is rendered. Started handoffs do not auto-refresh, and non-TTY startup still fails instead of guessing.

## 2026-04-06 — Interactive worktree resume

- `aflow run` now offers to resume the last unfinished matching worktree run when the same resolved invocation is rerun in a TTY. It resolves the prior run through the current shell's `.aflow/last_run_ids/<shell-id>` entry when available, then `AFLOW_LAST_RUN_ID`, then `.aflow/last_run_id`, and reuses the recorded feature branch and worktree path only when the workflow, plan path, team, step, turn limit, extra instructions, and lifecycle setup all match.

## 2026-04-06 — Optional bundled skill selection and analyzer docs parity

### What changed

- `aflow install-skills` now documents the default eight-skill install, optional bundled skill selection via `--include-optional`, and exact selection via repeatable `--only`. `BUNDLED_SKILL_NAMES` is now the full sorted inventory of bundled skill names, while the default install set stays unchanged.

- `aflow-assistant` is now documented as an optional bundled skill for setup help, aflow concepts, and evidence-first run debugging instead of being implied as part of the default install.

- `aflow analyze [RUN_ID] [--all]` is now documented in the top-level README and architecture reference as the supported analyzer entrypoint, and the run-id fallback chain is spelled out as explicit `RUN_ID`, current shell entry in `.aflow/last_run_ids/`, `AFLOW_LAST_RUN_ID`, then `.aflow/last_run_id`.

- `runlog.py` persists `.aflow/last_run_id` immediately when run paths are created, and also writes `.aflow/last_run_ids/<shell-id>` when a stable shell/session id is available, so the latest run remains discoverable even if the workflow later fails.

## 2026-04-06 — Auto-bootstrap empty repos from plan preamble

### What changed

- **Repo-state detection** — `aflow/git_status.py` gained `RepoState` (enum: `NO_GIT_BINARY`, `NOT_A_REPO`, `UNBORN`, `READY`) and `probe_repo_state(repo_root)` to classify the git state at startup without side effects. The lifecycle engine uses this before any preflight to decide whether bootstrap is needed.

- **Team-lead bootstrap handoff** — When a lifecycle workflow starts against a directory with no `.git/` or a repo with no commits, `run_workflow()` now invokes a bootstrap handoff before git-dependent preflight. The handoff reuses the same `[aflow].team_lead` resolution path as merge teardown. The agent is given the built-in `aflow-init-repo` skill instruction plus a derived `README.md` title and body, and runs from the primary checkout.

- **Deterministic README derivation** — `derive_readme_content(plan_text, file_stem)` extracts the README title from the first `# ...` heading (falling back to a humanized file stem) and the body from the `## Summary` section if present, otherwise the first prose paragraph after the title and before any checkpoint heading. Fenced code blocks, `## Git Tracking`, `## Done Means`, `## Critical Invariants`, and `## Forbidden` sections are skipped. If no usable paragraph is found, the fallback body is a single sentence naming the plan title.

- **Post-bootstrap verification** — After the init-repo agent returns, the engine checks: `HEAD` resolves to a commit, `HEAD` is on `main_branch`, `README.md` exists and is git-tracked, and the working tree has no tracked-file dirtiness. Untracked files from the pre-existing directory contents are acceptable. Only after all checks pass does normal lifecycle preflight and branch/worktree setup continue.

- **Removed obsolete unborn-branch guard** — The `_lifecycle_preflight_git` function no longer contains the "no commits yet; create an initial commit" error. That code path is now unreachable because bootstrap handles unborn repos before git-dependent preflight runs.

- **New bundled skill** — `aflow/bundled_skills/aflow-init-repo/SKILL.md` contains the contract for the bootstrap agent: initialize git if needed, repoint HEAD to `main_branch` if on a different unborn branch, write the provided `README.md`, stage only that file, commit with `Initial commit`, and emit `AFLOW_STOP:` on any ambiguity.

- **Documentation** — `README.md` now describes auto-bootstrap in the lifecycle startup section. `ARCHITECTURE.md` describes the new startup order: detect repo state, optionally bootstrap via team lead, then run normal lifecycle preflight and setup.

### Why

- Lifecycle workflows previously required the target repo to already have at least one commit. This was a friction point for new-project bootstrapping from a plan file, since users had to manually `git init` and commit before invoking `aflow`.
- The bootstrap reuses the team-lead agent rather than generating the initial commit inside engine code, which keeps the commit subject to the same model-driven process and avoids hardcoded README content.

### Gotchas

- **Bootstrap is local-only.** No remotes are added, no `git push` is performed. The initial commit exists only in the local repo.
- **Only `README.md` is committed.** Pre-existing files in the target directory are not staged or committed as part of bootstrap. The initial commit is minimal.
- **Committed repos are not affected.** Auto-bootstrap runs only when there are zero commits. A repo with even one commit goes through the normal preflight path without any bootstrap attempt.
- **Non-lifecycle workflows skip bootstrap.** Workflows with an empty `setup` do not trigger bootstrap and behave exactly as before, even if the working directory has no `.git/`.
- **If git is missing, lifecycle workflows fail early** with a clear error that does not mention remotes or network setup.

### Limits

- README derivation uses only the plan preamble (title + Summary section or first prose paragraph). Checkpoint content, step lists, and later sections are not parsed.
- No new configuration keys were introduced. Bootstrap uses the existing `[aflow].team_lead` and `main_branch` settings.

## 2026-04-05 — Explicit run flags and numeric start-step support

### What changed

- **Explicit flag-based CLI** — `aflow run` now accepts explicit `--plan`/`-p`, `--workflow`/`-w`, `--start-step`/`-ss`, and `--team`/`-t` flags in addition to the original positional forms. The `--max-turns`/`-mt` flag continues to work as before.

- **Mixed positional and flag input** — Plan and workflow can be specified via positional arguments, explicit flags, or a combination of both. When both sources provide the same field, they must resolve to the same canonical value or the CLI exits with a clear conflict error.

- **Intelligent two-positional resolution** — When two bare positional arguments are provided, they are resolved by meaning: the token matching an existing plan file is treated as the plan, and the token matching a configured workflow name is treated as the workflow. This allows both `workflow plan` and `plan workflow` forms to work without ambiguity. Single bare positionals remain unambiguous and always mean the plan path.

- **Numeric start-step selection** — `--start-step`/`-ss` now accepts either workflow step names (like `implement_plan`) or 1-based numeric indexes (like `2` for the second declared step). Numeric resolution happens after the workflow config is loaded, using the declared workflow step order. Out-of-range and invalid numeric indexes fail with clear bounds errors listing the valid range.

- **Canonical step name resolution** — All downstream execution receives the canonical workflow step name, never raw numeric tokens. This ensures run logs, status output, and workflow state consistently record and use step names.

- **Documentation updates** — `README.md` now shows all flag forms and mixed-order positional examples. `ARCHITECTURE.md` documents the new argument resolution rules and numeric index mapping.

### Why

- **Backward compatibility** — Single-positional plan-only invocations (`aflow run path.md`) continue to work exactly as before. Existing two-positional `workflow plan` forms remain valid.
- **Explicit is better than implicit** — Flags remove ambiguity when users want to specify both plan and workflow without relying on positional order or filename heuristics.
- **Numeric start-step is ergonomic** — Many workflows declare steps in a meaningful order; numeric selection like `-ss 2` is faster to type and remember than step names in some cases.
- **Fail-closed ambiguity handling** — When two positionals cannot be uniquely classified, the CLI errors instead of guessing, preventing silent mistakes.
- **Numeric to canonical conversion** — Resolving numeric selectors to step names before passing to the workflow engine keeps all downstream state using canonical names, avoiding confusion or inconsistencies.

### Gotchas

- **Positional argument changes are CLI-only** — The workflow engine still receives canonical step names in `ControllerConfig.start_step`. The numeric token is resolved in `cli.py` and never reaches downstream modules.
- **Numeric index is 1-based** — `--start-step 1` starts from the first declared step, `2` from the second, etc. `0`, negative numbers, and out-of-range values fail immediately with a clear error.
- **Ambiguous two-positionals error cleanly** — If both positionals could be plan files, both could be workflow names, or neither can be resolved, the CLI exits with a specific error message naming the problematic tokens and suggesting the use of explicit flags.
- **Duplicate values must match exactly** — If you provide both a positional and a flag for the same field (e.g., `aflow run -p path1 path2`), they must resolve to the same value. A mismatch causes an error naming the conflict.
- **Step order is fixed at config load time** — Numeric indexes map against the workflow's declared step order as loaded from config. Changes to the workflow definition between CLI invocation and execution do not retroactively change what a numeric index maps to during the current run.

### Limits and non-changes

- **Interactive step picking behavior is preserved** — When no explicit start step is given and the plan is partly complete, the interactive startup picker still prompts (only when stdin/stdout are TTYs) and uses the same interactive-only rules as before.
- **Single-positional ergonomics remain unchanged** — One bare positional argument is always treated as a plan path, even if it coincidentally matches a workflow name. This ensures `aflow run workflow_name` (when `workflow_name` is also a valid plan file path) unambiguously runs the plan file, not the workflow.

## 2026-04-04 — Worktree original-plan sync and plan-only dirtiness policy

### What changed

- **Dirty-policy classification** — `aflow/git_status.py` gained a helper to classify dirty paths into "plan-only" (files under `plans/` prefix) and "unrelated" (everything else). Worktree workflows now allow plan-only dirtiness at startup without prompting, while unrelated dirtiness still triggers the standard prompt/fail behavior.

- **Plan sync in worktree lifecycle** — `aflow/workflow.py` now syncs the original plan file into linked worktrees before prompt rendering (via `_sync_plan_to_worktree`) and back to the primary checkout after each turn, before post-turn state parsing (via `_sync_plan_from_worktree`). This allows untracked or gitignored plans under `plans/` to be available for worktree harness execution without requiring them to be git-tracked.

- **Removed git-tracked requirement** — The lifecycle preflight's `_is_git_tracked` gate for the original plan file is removed. The file must still exist and be under the primary repo root, but it may be untracked or gitignored.

- **Documentation updates** — `README.md` now describes plan-only dirtiness behavior, clarifies which workflows allow it, and documents that the original plan is copied into and synced back from linked worktrees. `ARCHITECTURE.md` now documents the refined preflight rule and sync points in the turn loop.

### Why

- **Untracked plans under `plans/` are a natural workflow state** for checkpoint-based handoffs where each agent iteration produces a new plan variant. Requiring them to be git-tracked was an artificial barrier, especially when `plans/backups/` and `plans/done/` are engine artifacts.
- **Plan-only dirtiness is safe to allow** because changes under `plans/` do not affect compiled code or normal workflows; they only affect `aflow` itself. Unrelated dirtiness (code changes, config, etc.) still blocks startup.
- **Original-plan-only sync remains the minimal copy contract.** Follow-up plans
  stay worktree-local, while their logical primary paths are retained in run
  metadata so a reused worktree can resume them safely.

### Gotchas

- **Original-plan-only sync:** This handoff syncs only the original plan file, not
  follow-up plans. A follow-up stays in its linked worktree; resume is supported
  only while that same worktree and saved active path remain available.
- **Transient follow-up plans:** Follow-up plans are not copied into the primary
  checkout. Reusing the recorded worktree can restore one; a missing saved file
  now fails clearly instead of silently resetting.
- **Dirty-path prefix matching:** The `plans/` classification uses exact `startswith("plans/")` checks, not substring matching. Paths like `plans_backup/` or `my-plans/foo` are treated as unrelated dirtiness and will block startup. This strict rule prevents accidental allow-listing of unintended directories.
- **Primary copy is the authority:** The original plan file in the primary checkout is the long-lived source of truth for plan state across runs. The worktree copy is a working copy that is synced back after each turn. If both the primary and worktree copies are edited externally between turns, the worktree copy wins (because it is synced after the harness returns).


## 2026-04-07 - Checkpoint 4: Codex Session Reuse and Plan Draft Persistence

Implemented Codex session management and plan draft persistence for the remote app server.

### What Changed

**New Components:**
- `codex_backend.py` — Adapter interface for Codex server integration with HTTP implementation
- `codex_routes.py` — FastAPI routes for Codex sessions and plan draft management
- `plan_store.py` — Plan draft save/load/promote operations for repositories

**API Endpoints Added:**
- Codex session listing, fetching, and messaging
- Plan draft save, load, delete, and promote operations
- In-progress plan listing

**Key Features:**
- Codex adapter interface supports session reuse and message history
- Plan drafts saved under `<repo>/plans/drafts/`
- Approved drafts promoted to `<repo>/plans/in-progress/`
- Content preserved verbatim during all operations
- Graceful degradation when Codex is not configured

### Implementation Details

**Dependency Injection:**
- Used FastAPI's `dependency_overrides` to inject global state into Codex routes
- Codex routes define placeholder dependency functions that are overridden by main app
- This allows routes to be defined at module level while still accessing request-scoped state

**Codex Backend:**
- Abstract `CodexBackend` protocol defines adapter interface
- `HttpCodexBackend` implements HTTP-based Codex server communication
- Normalizes external API responses into internal models
- Supports optional authentication via bearer token

**Plan Store:**
- Validates plan names to prevent path traversal
- Creates directories as needed
- Preserves content verbatim with normal newline normalization
- Promotion copies content without modification

### Testing

Added comprehensive test coverage:
- 11 tests for Codex backend with mocked HTTP client
- 20 tests for plan store operations
- 10 tests for Codex and plan draft API endpoints

All 71 server tests pass.

### Gotchas

- FastAPI dependency injection requires using `app.dependency_overrides` for module-level routers
- Direct function replacement doesn't work because routes are registered at import time
- Test client must set `raise_server_exceptions=True` to see actual errors during development

### Next Steps

Checkpoint 5 will add the mobile-first web client for repos, plans, sessions, and execution.
