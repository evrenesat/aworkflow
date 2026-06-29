# Runtime Behavior

## How A Run Works

Each workflow step launches one fresh harness process.

At a high level:

1. `aflow` loads the selected workflow and reads the original plan file.
2. If the workflow has lifecycle setup, `aflow` inspects git state, optionally bootstraps an empty repo, runs lifecycle preflight, and creates the execution environment.
3. The run starts at the workflow's first declared step unless startup selected another step.
4. The engine renders prompts, resolves the step role through the selected team and global roles, and runs the harness CLI once.
5. After the harness returns, it re-reads the original plan and handles recovery or normal transitions.
6. The next matching `go` transition chooses the next step or stops at `END`.
7. If teardown includes `merge`, `aflow` invokes a merge handoff through the configured `team_lead` role and verifies the result.

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
- `ACTIVE_PLAN_PATH` changes only when the current harness step writes `NEW_PLAN_PATH`.
- Before the workflow starts, `aflow` copies the original plan into `<repo_root>/plans/backups/`.
- If matching backup content already exists, `aflow` reuses it.
- If the same backup name already exists with different content, `aflow` writes the next `_vNN` file.
- For worktree workflows, the original plan can be untracked or gitignored under `plans/`; it is still copied into the linked worktree and synced back after each turn.

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
