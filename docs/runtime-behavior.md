# Runtime Behavior

## How A Run Works

Each workflow step launches one fresh harness process.

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

## Loop Limits

`max_turns` is the hard turn cap. The runner executes a fixed `1..max_turns` loop, so a workflow cannot exceed that number of turns even if transitions keep routing back.

On the last allowed turn:

- `MAX_TURNS_REACHED` evaluates true.
- If a transition routes to `END`, the run completes successfully with end reason `max_turns_reached` unless `DONE` is also true.
- If no transition routes to `END`, the run fails with a max-turns error.

`max_same_step_turns` limits consecutive selection of the same step in multi-step workflows. The streak resets only after a different step actually executes. Single-step workflows are not affected.

## Interstep Manager Supervision

With `[manager].enabled = true`, AFlow runs a read-only manager after every
finalized workflow turn and before applying the controller's proposed
transition, recovery, retry, or `END`. A manager call is not a workflow turn:
it does not increase `turns_completed`, consume `max_turns`, create a checkpoint
commit, or trigger same-step caps.

The manager receives a reproducible, versioned context built from durable
artifacts. Lite receives semantic results, plan snapshots and structured state,
controller/routing counters, compact history, and bounded diagnostics. It does
not receive active-plan content, prompts, or raw trace bodies. Full adds the
complete active-plan Markdown. Full is chosen directly for semantic stalls,
second reviewer rejection/non-convergence, explicit stop markers, invalid
plans, and ambiguous failures; Lite can escalate once to Full at the same
boundary.

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

## Live Status

While a step is running, `aflow` shows a Rich status panel on stderr. The elapsed timer refreshes every second, and git stats refresh every 10 seconds.

Fields include:

- elapsed time
- run id and resumed-from run id when present
- workflow and current step
- harness, model, and effort
- checkpoint progress and turn count
- original and active plan paths
- workflow graph
- turn history with stdout/stderr artifact links when non-empty
- git summary since workflow start
- issues link when issues exist
- current run status

The git summary is based on a baseline captured at workflow start, so pre-existing dirty state is excluded. If git is unavailable, git rows are omitted and the workflow still runs.

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
  inputs and reports any stored-context drift.
- `manager-report.md` for a manager stop, manager protocol/mutation failure,
  explicit workflow stop, or another terminal incident after run creation

Manager artifacts remain separate from `turns/` so turn accounting stays
stable. `run.json` contains only compact manager history, pending one-hop state,
and the latest report path; raw traces are referenced by stable run-relative
paths and byte sizes rather than copied into manager prompts. On failure the
CLI prints the same self-contained report that is persisted, including evidence,
attempts, plan/workspace state, and suggested next actions.
- stdout and stderr
- plan snapshots before and after each step
- evaluated conditions and chosen transitions
- terminal `end_reason`
- `issues.md` when issues accumulate

Turn directories are created before the harness process launches and finalized in place afterward. If a harness crashes after the turn directory is created, partial logs are still inspectable.

Older run directories are pruned according to `keep_runs`.

## Success Reporting

When a workflow finishes successfully, `aflow` prints one stdout line naming the workflow, turn count, and stop reason.

Machine-readable `end_reason` values:

- `already_complete`
- `done`
- `max_turns_reached`
- `transition_end`

`transition_end` covers successful `END` transitions when the plan is still incomplete and the chosen transition is not driven by `DONE` or `MAX_TURNS_REACHED`, including unconditional `go = [{ to = "END" }]`.
