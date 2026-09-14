# AFlow run troubleshooting

Use this reference for local evidence inspection when MCP state and lite context
are insufficient, or when diagnosing a direct CLI run. For server-managed runs,
start with canonical MCP state: receipt-aware status may be more current than
raw `run.json`. Keep exact project/run identity throughout the investigation.

## Fast Triage Workflow

1. Identify the run.
   - If the user gives a run path, use it.
   - If the analyzer cannot identify the intended run, list recent directories
     and inspect their summaries before selecting one:
     ```bash
     fd -td -d 1 . .aflow/runs | sort | tail -n 5
     ```
   - If the user gave a run id, analyze that run first.
   - If no run was named, the analyzer checks shell/environment/last-run
     records before falling back to the latest substantive run. Verify and
     report the selected run; pass its ID explicitly for subsequent commands.
   - Only use `--all` when you actually need corpus-level signal first.

2. Read the run summary first.
   - Start with:
     ```bash
     jq '{status, workflow_name, current_step_name, turns_completed, end_reason, failure_reason, merge_status, merge_failure_reason, selected_start_step, startup_recovery_used, startup_recovery_reason, pending_retry_step_name, pending_retry_attempt, pending_retry_limit, original_plan_path, active_plan_path, new_plan_path, execution_repo_root, worktree_path, main_branch, feature_branch}' .aflow/runs/<run-id>/run.json
     ```
   - This usually tells you whether the problem is:
     - harness or process failure
     - explicit `AFLOW_STOP`
     - plan parse or retry issue
     - merge failure
     - startup, preflight, or bootstrap problem
     - max-turns exhaustion or ordinary workflow completion

3. Narrow to the relevant turn before reading raw output.
   - Inspect turn summaries:
     ```bash
     rg -n '"status"|"error"|"end_reason"|"chosen_transition"|"retry_|"returncode"' .aflow/runs/<run-id>/turns/*/result.json
     ```
   - Then open the most relevant turn file with `bat --paging=never`.

4. Search raw stdout and stderr only after the failing turn is known.
   - Use targeted search instead of scrolling:
     ```bash
     rg -n -C 2 'AFLOW_STOP|Traceback|error:|Exception|failed|inconsistent checkpoint state|startup aborted|merge verification failed' .aflow/runs/<run-id>/turns/*/stdout.txt .aflow/runs/<run-id>/turns/*/stderr.txt
     ```

5. Inspect prompts only when the evidence points there.
   - Open `effective-prompt.txt` first if the model seems to have been given the wrong context.
   - Use `user-prompt.txt` to check appended retry guidance, plan paths, or follow-up plan instructions.
   - Use `system-prompt.txt` only when harness or system contract differences matter.

6. Compare plan snapshots when the issue is plan state drift.
   - `result.json` already records `snapshot_before` and `snapshot_after`.
   - If the failure mentions inconsistent checkpoint state, read:
     - the turn `result.json` `error` field
     - the active plan on disk
     - the previous successful turn's `snapshot_after`
   - In worktree mode, remember the durable source of truth is the primary-checkout plan path after sync-back.

## Manager-Supervised Runs

When a run has manager history or a manager report path, start with the
self-contained manager report and the analyzer context before opening raw
streams. Use `aflow analyze <run-id> --manager-context lite` for the safe
compact reconstruction; use Full only when active-plan content is necessary
and authorized. The manager decision directories contain exact contexts and
responses, while public status and events intentionally contain only compact
metadata. Open referenced stdout or stderr only when that extracted evidence
is insufficient.

## Failure Classification

For managed runs, canonical state and worker receipts distinguish confirmed
inactivity from ambiguous ownership. The raw artifact patterns below are
diagnostic signals, not permission to restart an apparently stalled worker.
Manager decisions are interstep gates, not worker turns.

- `run.json.failure_reason`
  - Top-level workflow failure before completion. Start here.
- `run.json.merge_failure_reason`
  - Merge handoff failed after the main workflow logic completed.
- `AFLOW_STOP: ...` in `stdout.txt`, `stderr.txt`, or turn `result.json.error`
  - The agent explicitly escalated. Treat the stop reason as first-class evidence, then verify whether the stop was justified.
- `result.json.status == "retry-scheduled"`
  - The harness returned successfully but the rewritten plan was not parseable as a consistent checkpoint state. Inspect that turn's `error`, then inspect the active plan and retry context.
- `result.json.status == "plan-invalid"`
  - The engine could not continue because the plan on disk was invalid after the turn.
- `returncode != 0` with little or no structured error
  - Start with `stderr.txt`, then inspect the invocation and prompt artifacts.
- `status == "completed"` with `end_reason == "max_turns_reached"`
  - Not a crash. Inspect repeated turn summaries to see which step looped and what evidence never changed.
- `startup_recovery_used == true`
  - The run resumed from an inconsistent checkpoint-state startup condition. Treat that as context, not necessarily as the new root cause.
- Repeated turns with identical plan snapshots
  - Suspect a no-progress loop, especially if the step names alternate between implement and review steps.
- `stdout.txt` says the review is blocked or asks for direction, but no `AFLOW_STOP` exists
  - Treat that as a blocked-review signal. The engine may keep running because the subprocess still exited successfully.
- Latest turn `result.json` has `status == "starting"` with no finalized result
  - It may still be executing. Check canonical ownership and worker receipts;
    infer interruption only after confirming the worker is no longer active.

## Noise Reduction Rules

- Do not start by reading every turn directory. Use `run.json` and `result.json` to decide where to zoom in.
- Filter out low-signal test-noise runs early. A common pattern is `workflow_name == "other"` with `turns_completed == 0` and `end_reason == "already_complete"`.
- Do not scan full prompts unless the turn summary or failure text suggests a prompt or placeholder problem.
- Do not confuse banner behavior with harness stderr. The live banner is a UI concern, the saved `stderr.txt` is the harness subprocess output.
- Do not treat whitespace-only `stdout.txt` or `stderr.txt` as meaningful evidence.
- Ignore old run directories unless the user asks for regression comparison or the latest run looks incomplete.
- Ignore `env.json` unless the problem smells like PATH, executable discovery, or missing environment configuration.
- Ignore `plans/backups/` unless the bug involves plan overwrite, sync, or recovery behavior.
- Ignore git noise under `.aflow/` when diagnosing source changes. Run artifacts are intentionally untracked operational files.

## Source Checkout Lookup Map

When logs are not enough and the source checkout is available, search these files in this order:

1. `ARCHITECTURE.md`
   - Run artifact layout, lifecycle order, retry behavior, banner behavior, and bundled skills.
2. `README.md`
   - User-visible CLI and operational contract.
3. `aflow/runlog.py`
   - Exact `run.json` and `result.json` fields.
4. `aflow/workflow.py`
   - Stop-marker detection, retry scheduling, plan reload flow, merge verification, worktree sync, and failure summaries.
5. `aflow/cli.py`
   - Startup recovery, argument resolution, interactivity rules, and install-skills entrypoints.
6. `aflow/plan.py`
   - Plan parsing and inconsistent checkpoint-state rules.
7. `tests/`
   - Expected behavior split by subsystem. Search for the symptom text first,
     then open only the matching test module.

Use `rg` to jump directly to the behavior in question. Examples:

```bash
rg -n "AFLOW_STOP|retry-scheduled|plan-invalid|merge_failure_reason|startup_recovery" aflow tests
rg -n "run.json|result.json|stdout.txt|stderr.txt|effective-prompt" aflow/runlog.py aflow/workflow.py ARCHITECTURE.md README.md
rg -n "inconsistent checkpoint state|startup aborted|max_turns_reached" aflow tests
```

## Response Contract

When reporting findings to the user:

- Name the exact run directory and turn(s) used as evidence.
- If you used `aflow analyze`, say so and cite the extracted signals, focus turns, and artifact paths rather than pasting full raw output.
- Separate:
  - confirmed facts from artifact files
  - inferences about likely cause
  - next debugging step or code fix
- Prefer a short evidence trail over a long transcript dump.
- If the likely cause is in engine code, point to the exact file and symbol that governs the behavior.
- If the run looks correct and the user's expectation is wrong, say that directly and cite the relevant artifact or doc.
