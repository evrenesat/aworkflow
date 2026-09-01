---
name: aflow-assistant
description: "Investigate aflow setup problems, failed or confusing workflow runs, run-log evidence, plan parsing and retry issues, lifecycle/bootstrap/merge failures, and questions about aflow concepts or interface behavior. Use when an agent needs to explain what happened in an aflow run, gather high-signal evidence from `.aflow/runs/` efficiently, or reason about the engine from bundled references when a source checkout is unavailable."
---

# AFlow Assistant

Use this skill when helping a user understand, debug, or recover an aflow run. Treat run artifacts, the current plan file, and the current repo state as the source of truth. Start with evidence on disk, not with a theory.

## Bundled Resources

- `aflow analyze`
  - CLI command that deterministically extracts a single-run report by default and can also summarize a corpus when asked. It pulls out high-signal patterns such as merge failures, missing original plans, blocked review preconditions, interrupted runs, and alternating no-progress loops.
- `references/engine-map.md`
  - High-level engine and artifact map for installed-skill scenarios where the original `aflow` source checkout is not present.
- `references/engine-features.md`
  - Complete engine features reference: configuration system (every key,
    type, and default), workflow/step/condition model, harnesses, session
    capabilities, the hotplug system (stages, capability paths, handover),
    manager supervision, error recovery, repartition, scope pressure, run
    artifacts, CLI surface, bundled skills, plan format, and the MCP control
    plane (aflowd).

Use these bundled resources first. They are part of the shipped skill and remain available after installation even when the repo source tree is not.

## Core Rules

- Start from the most relevant run directory. If the user does not name one, inspect the latest run first.
- If the user names a run or wants fast triage, run `aflow analyze <run-id>` for that one run before opening raw turn artifacts.
- Use corpus mode only when the user wants repeated patterns across several runs.
- Read `run.json` before reading raw `stdout.txt` or `stderr.txt`.
- Read `turns/turn-NNN/result.json` before opening full prompt or transcript artifacts.
- Use `jq`, `rg`, `fd`, and `bat --paging=never` to narrow the search before reading large files.
- Distinguish confirmed facts from hypotheses. Quote file paths and exact fields when reporting what happened.
- For worktree workflows, remember that run artifacts live under the primary checkout while normal execution may have happened in `execution_repo_root` / `worktree_path`.
- Do not mutate `.aflow/runs/` artifacts. They are debugging evidence.
- If the evidence is incomplete, say exactly which file, field, or command is missing.
- Do not assume the original `aflow` repo checkout exists just because the skill is installed. Use `references/engine-map.md` when repo source files are absent.

## Installed-Skill First Workflow

1. For a specific run, prefer:
   ```bash
   aflow analyze <run-id>
   ```
   or:
   ```bash
   aflow analyze --repo-root <repo> <run-id>
   ```
2. If the user only gives a repo and no run ID, the analyzer defaults to the latest substantive run:
   ```bash
   aflow analyze
   ```
   or with explicit repo root:
   ```bash
   aflow analyze --repo-root <repo>
   ```
3. Use corpus mode only for repeated-pattern hunting:
   ```bash
   aflow analyze --all
   ```
4. Read `references/engine-map.md` before assuming you need direct source-code access.
5. For "how is this configured / what does this feature do" questions, read
   `references/engine-features.md` — it covers the full config surface,
   workflows, harnesses, hotplug, manager, recovery, repartition, artifacts,
   CLI, and skills without source access.
6. Use repo source files only when a source checkout is actually present and you need deeper implementation detail than the bundled reference provides.

## What Lives Where

- `.aflow/runs/<run-id>/run.json`
  - Run-level summary: status, workflow name, step name, turn counts, failure reason, merge status, startup recovery, retry summary, lifecycle context, and active/original/new plan paths.
- `.aflow/runs/<run-id>/turns/turn-NNN/result.json`
  - Turn-level summary: step name, selector, status, return code, error, retry metadata, conditions, chosen transition, and plan snapshots before/after the turn.
- `.aflow/runs/<run-id>/turns/turn-NNN/stdout.txt`
- `.aflow/runs/<run-id>/turns/turn-NNN/stderr.txt`
  - Raw harness output for the turn.
- `.aflow/runs/<run-id>/turns/turn-NNN/system-prompt.txt`
- `.aflow/runs/<run-id>/turns/turn-NNN/user-prompt.txt`
- `.aflow/runs/<run-id>/turns/turn-NNN/effective-prompt.txt`
  - Useful when the failure looks like a prompt-construction or missing-context problem, not as the first debugging stop.
- `plans/in-progress/`
  - Current plan source of truth.
- `plans/backups/`
  - Relevant only when investigating plan sync or backup behavior.
- `references/engine-map.md`
  - Installed bundled architecture summary. Use this first when the skill is installed outside the original repo.
- `references/engine-features.md`
  - Complete engine surface reference: config keys and defaults, workflows,
    harnesses, session capabilities, hotplug stages and capability paths,
    manager, recovery, repartition, scope pressure, run artifacts, CLI,
    skills, and the MCP control plane. Use it to answer "how does X work /
    what is configurable" without opening source files.
- `aflow analyze`
  - CLI command for deterministic single-run extraction first, with optional corpus mode.
- `README.md`
- `ARCHITECTURE.md`
  - User-facing and architectural contract for lifecycle, run artifacts, retry behavior, banner behavior, and bundled skills.
- `aflow/workflow.py`
- `aflow/runlog.py`
- `aflow/plan.py`
- `aflow/cli.py`
- `aflow/status.py`
  - Code-level source of truth when logs show an engine-path question.
- `tests/`
  - Focused behavior tests split by subsystem, including `test_runtime.py`,
    `test_cli.py`, `test_runlog.py`, `test_plan.py`, and `test_config.py`.
    Search for the failure string or field name before guessing.

## Fast Triage Workflow

1. Identify the run.
   - If the user gives a run path, use it.
   - Otherwise list recent runs and start with the newest:
     ```bash
     fd -td -d 1 . .aflow/runs | sort | tail -n 5
     ```
   - If the user gave a run id, analyze that run first.
   - If no run was named, the analyzer defaults to the latest substantive run under the repo.
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
  - Treat the run as interrupted or abandoned mid-turn rather than completed or cleanly failed.

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

## Bundled Engine Map First

When the skill is installed into a harness directory, the original repo files
such as `README.md`, `ARCHITECTURE.md`, `aflow/workflow.py`, and `tests/` may
not exist at all. In that case:

1. Read `references/engine-map.md`.
2. Use `aflow analyze` plus `run.json` and turn `result.json` as the primary evidence path.
3. Consult `references/engine-features.md` for configuration, feature, and CLI questions the map does not cover.
4. Escalate to source-code lookup only if the user is working inside an actual `aflow` source checkout.

Do not tell the user to inspect source files that are not present in their environment.

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
