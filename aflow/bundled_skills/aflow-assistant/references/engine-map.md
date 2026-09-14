# AFlow Engine Map

Use this reference when the `aflow-assistant` skill is installed into a harness skill directory and the original `aflow` source checkout is not available. It is a high-level operational map of how the workflow engine is supposed to behave and where to look next.

## Big Picture

`aflow` is a plan-driven workflow engine.

At a high level:

1. The UI/MCP control plane or direct CLI resolves the workflow, plan, and
   startup questions. Managed starts reserve an identity and may await answers
   before a worker starts.
2. The workflow runner creates a run log directory under `.aflow/runs/`.
3. The runner invokes a harness subprocess for each turn.
4. The engine reloads the plan after each turn and decides the next transition.
5. Optional lifecycle steps handle repo bootstrap, branch/worktree setup, merge, and teardown.

The plan file on disk is the durable state for checkpoint progress and restart behavior. The run log is the durable evidence trail for what happened during execution.

## Interactive entry points and managed ownership

Use [MCP operations](mcp-operations.md) for the connected-agent operating
procedure. `aflow ui` serves both the dashboard and authenticated HTTP MCP;
there is no separate public daemon/MCP listener to start. Registered projects
define the allowed roots. Global workflow settings are shared, while plans,
run identities, and execution artifacts belong to the selected project.

For managed runs, start with canonical `get_run` and lite `get_run_context`.
They incorporate worker ownership and exit receipts that a raw `run.json` may
not yet reflect. A detached worker can outlive the UI server or MCP client;
reconnect and inspect ownership before starting or resuming anything.
An active controller and its launcher/child processes are one logical run,
not several independent runs.

Resume creates a lineage-linked successor with a new run ID and preserves the
predecessor. It reloads current configuration while checking lifecycle and
ownership identities. Frozen launch config copies are diagnostics. Managed
worktrees can hold execution code while the primary checkout owns durable
run artifacts and synchronized plans: follow returned paths, not the current
shell directory by assumption.

Worktree setup, merge, publication, and teardown depend on the resolved
workflow and repository settings. A terminal turn or checked plan does not
prove remote publication, CI success, or live activation. Inspect the
controller's delivery evidence and verify those stages when in scope.

## Runtime Call Flow

Conceptually the flow is:

1. `aflow run ...`
2. CLI startup and config loading
3. Plan parsing and optional startup recovery / startup questions
4. `run_workflow(...)`
5. Run-log directory creation
6. Optional lifecycle bootstrap or worktree/branch setup
7. Turn loop:
   - write turn-start artifacts
   - launch harness
   - capture stdout/stderr
   - detect `AFLOW_STOP`
   - sync plan back from worktree when needed
   - reload plan
   - decide transition
   - write turn-complete artifacts and update `run.json`
   - optionally run the read-only manager gate before applying the proposed action
8. Optional merge / teardown
9. Final status write

## Major Responsibilities

### CLI and startup

The CLI is responsible for:

- parsing command-line arguments
- loading user workflow config
- resolving positional workflow / plan arguments
- resolving `--start-step`
- handling startup recovery prompts for inconsistent checkpoint state
- handling startup prompts for dirty worktrees when required

If a startup question needs user input and stdin/stdout are not interactive TTYs, the CLI should fail clearly instead of guessing.

### Workflow execution

The workflow runner is responsible for:

- reading the active plan
- evaluating step transitions
- invoking the selected harness for each step
- managing retries for inconsistent checkpoint-state reloads
- syncing the original plan to and from linked worktrees
- performing lifecycle bootstrap and merge handoffs
- persisting run artifacts after every turn

### Run logging

The run-log subsystem persists:

- one `run.json` per run
- one `turns/turn-NNN/` directory per turn
- one `manager/decision-NNN/` directory per manager call, with exact context, prompts, response streams, and result
- `manager-report.md` when supervision stops a created run
- prompt artifacts
- stdout and stderr
- structured turn result metadata

For direct CLI runs, start here. For managed runs, inspect canonical MCP state
first, then use these local artifacts when the bounded context is insufficient.

### Banner and status UI

The rich banner is a user-interface layer. It is useful for live progress, but it is not the source of truth for postmortem debugging. Prefer `run.json` and turn `result.json` over banner assumptions.

## Artifact Layout

For a run rooted at `.aflow/runs/<run-id>/`:

- `run.json`
  - run-level status
  - workflow name
  - turns completed
  - failure reason
  - merge failure reason
  - startup recovery fields
  - original / active / new plan paths
  - lifecycle context such as worktree path and feature branch
- `turns/turn-NNN/result.json`
  - structured turn metadata
  - step name
  - return code
  - status
  - chosen transition
  - retry metadata
  - plan snapshots before and after the turn
- `turns/turn-NNN/stdout.txt`
- `turns/turn-NNN/stderr.txt`
  - raw harness output
- `turns/turn-NNN/system-prompt.txt`
- `turns/turn-NNN/user-prompt.txt`
- `turns/turn-NNN/effective-prompt.txt`
  - prompt construction evidence

## Expected Meanings Of Common Fields

### Manager supervision

Manager calls are read-only control gates, not workflow turns. They run after
a finalized turn and before its proposed next action, including END. The
compact run summary may point to pending notes, a one-step team override,
manager history, or a report. Start with the report when present, then run
`aflow analyze <run-id> --manager-context lite`; use Full only when the active
plan itself is needed. Raw trace paths in context are references, not an
invitation to scan every stream.

- `failure_reason`
  - top-level workflow failure before merge finalization
- `merge_failure_reason`
  - merge handoff failed after the main workflow logic reached a terminal path
- `status == "plan-invalid"` on a turn
  - the plan on disk could not be accepted after that turn
- `status == "retry-scheduled"` on a turn
  - the harness exited cleanly, but the resulting plan was in an inconsistent checkpoint state and the engine scheduled another attempt
- turn `status == "starting"` with no completed result
  - the turn is unfinished; inspect worker ownership before inferring a crash

## Common Failure Shapes

### Normal turns completed, then merge failed

Symptoms:

- turn sequence reaches a terminal transition
- `run.json.status == "failed"`
- `run.json.merge_failure_reason` is present

Interpretation:

- the implementation phase may be fine
- the failure is in merge verification, conflict handling, or dirty-state checks

### Original plan file missing after a turn

Symptoms:

- `failure_reason` mentions `original plan file is missing after the turn`

Interpretation:

- an agent or review flow moved or deleted the original handoff plan too early
- the engine expects the original plan to stay under `plans/in-progress/` until terminal success

### Alternating no-progress loop

Symptoms:

- multiple turns exit `0`
- step names alternate between implementation and review steps
- plan snapshots do not advance
- `NEW_PLAN_EXISTS` may stay true

Interpretation:

- the workflow is busy but not making state progress
- same-step protection alone may miss this if two steps alternate

### Review blocked on a precondition but no `AFLOW_STOP`

Symptoms:

- turn exits `0`
- stdout says the review is blocked or asks for user direction
- snapshots do not advance afterward

Interpretation:

- the agent surfaced a human-decision blocker in prose but did not emit `AFLOW_STOP`
- the engine may continue looping because the subprocess technically succeeded

### Abandoned or interrupted run

Symptoms:

- the latest turn directory has `result.json` with `status == "starting"`
- stdout/stderr or final result metadata are missing

Interpretation:

- confirm the worker is inactive before concluding the run terminated mid-turn
- inspect the last completed turn and the surrounding environment

## How To Use This Reference

1. Start with the `aflow analyze` command for one run:
   - `aflow analyze <run-id>`
   - or `aflow analyze --repo-root <repo> <run-id>`
2. Without an explicit ID, the analyzer checks shell/environment/last-run
   records before falling back to the latest substantive run. Verify which
   run was selected:
   - `aflow analyze`
   - or `aflow analyze --repo-root <repo>`
3. Use `--all` only when you actually want repeated patterns across several runs:
   - `aflow analyze --all`
4. Read `run.json`.
5. Read the most relevant turn `result.json`.
6. Only then open raw stdout/stderr and prompts.
7. Use the symptom map above before diving into source code.

## When To Escalate To Source Inspection

Escalate from this reference to a source checkout only when:

- the run artifacts contradict this reference
- the behavior appears to violate a documented invariant
- the likely bug is in transition logic, lifecycle setup, or logging internals
- you need exact function or test names for a code change

If the source checkout is available, inspect:

- workflow orchestration
- run-log writing
- plan parsing
- CLI startup behavior
- tests covering the same symptom

This reference is meant to remove the need for source inspection in the common debugging path, not to replace source code entirely.
