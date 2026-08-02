---
name: aflow-guard-development-run
description: Guard any explicitly requested AFlow run in the task that requested supervision, with one attached heartbeat automation, bounded durable-state snapshots, one-shot safe recovery, automatic scheduler shutdown, actionable same-task reporting, and evidence or fix-plan extraction for improving AFlow itself. Use whenever the user asks to babysit, guard, monitor, or keep an AFlow workflow running, whether it is ordinary product work or an AFlow development experiment. Do not invoke automatically for an unguarded run; external guardianship is temporary resilience scaffolding until AFlow is stable enough not to need it.
---

# Guard an AFlow Run

Guard the exact AFlow run the user selected. The run may implement any project;
it does not need to develop AFlow. Keep it progressing while treating anomalies
as bounded evidence for improving AFlow's own resilience.

## Non-negotiable invariants

- Use the task that requested the guard as the sole guard and report task, and
  attach exactly one heartbeat automation to it. Never create a secondary task
  for setup, scheduled beats, or actionable reporting.
- Never use a project cron automation unless the user explicitly asks for a new
  task per tick: every cron tick creates a standalone task even when it returns
  `DONT_NOTIFY`.
- Keep heartbeat turns bounded, do not reread prior tick history, and keep
  healthy results silent.
- Persist the initiating task ID as both compatibility routing IDs in guardian
  state and in the heartbeat prompt. Never rely on conversational memory for
  task identity.
- Treat keeping the workflow running and collecting actionable AFlow evidence
  as equally important outcomes. Preserve evidence even when recovery succeeds.
- Create or leave active a schedule only while an AFlow controller for the
  guarded run is verified active.
- At the end of every scheduled tick, either verify an active controller or
  pause the automation in that same tick.
- Never infer liveness from Screen alone. A reusable Screen window may contain
  only a shell after AFlow exits.
- Never infer pinned-run liveness from repository cwd alone. Require an exact
  plan match and, for resumed runs, a matching current/predecessor resume ID;
  fail closed when legacy process evidence is ambiguous.
- Never create a second controller, edit an active feature worktree, suspend a
  controller with a live child, or discard dirty work.
- Give one recovery attempt to one unchanged incident fingerprint. Do not loop
  retries or checkpoint-scope resets.
- Treat line and file budgets as scope-pressure evidence, not semantic owner
  boundaries.
- Ask for owner input only for changed meaning, conflicting requirements,
  destructive handling, ambiguous ownership, or inability to preserve accepted
  work.

## Establish the guard

1. Read the nearest `AGENTS.md` files in the guarded project and, when known,
   the AFlow source checkout. Do not reject a guarded project because it is not
   the AFlow source repository.
2. Resolve an exact run ID and inspect its `.aflow/runs/<run-id>/run.json`.
   Never guard “the latest run” without first pinning the resulting ID.
3. Record the guarded repository, optional AFlow source checkout, run ID, plan,
   worktree, feature branch, workflow, team, start step, maximum turns, and
   optional Screen session. Treat `run.json` as read-only.
4. Run `scripts/aflow_guard_snapshot.py` once. Do not schedule unless it reports
   an active controller. If the run is recoverable, recover it first and
   schedule only after the continuation is verified active.
5. Search for `automation_update` and `list_projects` when they are not loaded.
6. Read the initiating task ID from `CODEX_THREAD_ID`. If it is missing or not
   a UUID, do not schedule; explain that durable same-task identity cannot be
   established.
7. Inspect existing automations for the exact repository and run ID. Reuse a
   matching heartbeat only when it targets the initiating task. Pause any
   matching cron or heartbeat attached to another task before enabling the
   selected guard; never leave two schedules active.
8. In the initiating task, run the snapshot with its task ID supplied as both
   compatibility routing IDs. Then create a heartbeat attached to that same
   task, normally every ten minutes. Set failed-runs-only notifications. Do not
   create another task or worktree.
9. Verify all of the following before claiming the guard is active: both
   persisted routing IDs equal the initiating task ID, the heartbeat targets
   that ID, its prompt contains that exact ID, and the pinned AFlow controller
   is still active. If any check fails, do not enable a scheduler.

Use a unique automation name containing the project name and pinned run ID.
Keep its prompt short:

```text
Use $aflow-guard-development-run for one scheduled tick guarding AFlow run
RUN_ID in REPO, with optional AFlow source AFLOW_REPO and Screen SESSION.
This task is THREAD_ID; keep actionable reports in this same task. Stay silent
while healthy, preserve anomaly evidence for improving AFlow, and recover only
within the skill policy. At tick end, pause this automation unless an AFlow
controller is verified active.
```

Run the heartbeat in the initiating local task because the guard must observe
the real process tree, Screen session, run artifacts, and managed worktree.
Use absolute repository paths in every tick so the task's current directory
cannot redirect inspection.

## Perform one scheduled tick

Run the bundled snapshot script first:

```bash
python3 <skill-dir>/scripts/aflow_guard_snapshot.py \
  --repo <guarded-repo> \
  --run-id <run-id> \
  --screen-session <optional-session> \
  --expected-helper-sha256 <validated-helper-sha256> \
  --report-thread-id <task-id> \
  --guard-thread-id <same-task-id>
```

The two routing options are compatibility names in the existing helper. Supply
the same initiating task ID to both. Do not overwrite or silently migrate
persisted legacy split-task routing; pause its old schedule and report the
migration requirement in the initiating task.

Resolve and hash the exact helper before scheduling. Persist its absolute path
and expected SHA-256 in the heartbeat prompt. Every tick must pass that hash;
the helper emits invoked/resolved paths, SHA-256, build ID, Git provenance, and
the comparison result. A missing, malformed, or mismatched expected hash is
`invalid_state`: pause and notify without using process evidence.

The script emits bounded JSON and stores deduplication state outside the
repository under the Codex runtime directory. It refuses state-changing calls
from a task other than the persisted task. Treat routing mismatch as
`invalid_state`, pause, and report visibly in the same task. Otherwise act on
its classification. In process evidence, `controller_pids` contains one
representative PID per proven logical controller and `wrapper_pids` separately
records recognized `uv run` launcher parents. Ambiguous ancestry remains
multiple controllers:

| Classification | Required action |
| --- | --- |
| `active_progress` | Return exactly `DONT_NOTIFY`. |
| `active_waiting_child` | Return exactly `DONT_NOTIFY`; a live child is progress evidence. |
| `active_waiting` | Stay quiet for two intervals, then inspect one bounded boundary. |
| `recoverable_orphan` | Attempt one safe resume, verify it, then update or pause the guard. |
| `terminal_success` | Pause the automation and notify once with completion evidence. |
| `terminal_failed` / `terminal_incomplete` | Diagnose once, recover if safe, otherwise write a fix plan and pause. |
| `unsafe_inconsistent` / `unsafe_duplicate_controllers` / `invalid_state` | Do not mutate; pause and notify once. |

Healthy ticks perform only the snapshot call. Do not emit commentary, read
transcripts, inspect git history, rebuild manager context, create an inbox item,
or create another task. Return exactly `DONT_NOTIFY`.

For an anomaly, inspect only:

- selected `run.json` fields
- the newest finalized turn `result.json`
- the newest manager `boundary.json` and `result.json`
- at most the final 12 KiB of the relevant stdout or stderr
- controller descendants, Screen presence, and concise git status

Do not load whole stdout/stderr files, repeated unchanged artifacts, or prior
scheduled-task history.

## Recover safely

Mark an attempted incident before recovery:

```bash
python3 <skill-dir>/scripts/aflow_guard_snapshot.py \
  --repo <guarded-repo> --run-id <run-id> --mark-recovery-attempt
```

Recover only when all of these are true:

- no matching controller is active
- the recorded worktree, branch, and original/active plan paths can be
  reconciled without guessing or destructive edits
- the primary AFlow checkout has no conflicting dirty changes
- the stopped state is resumable under AFlow's documented contract
- this fingerprint has not already received a recovery attempt

Reconstruct the resume invocation from durable frozen configuration; do not
guess flags. Reuse the validated Screen session/window when possible, leaving a
shell available after exit. Immediately re-run the snapshot and require exactly
one active controller.

When resume creates a continuation run:

1. resolve it by durable `resumed_from_run_id` and creation evidence
2. verify its controller, plan, worktree, team, and starting step
3. update the automation prompt to the new exact run ID while preserving all
   other automation fields
4. keep the automation active only after that verification

Do not use `--resume-reset-scope` automatically. It is permitted only when the
initial user request explicitly authorizes that recovery policy and the reset
preserves agreed meaning and accepted work. It still receives only one attempt
for the incident fingerprint.

## Capture evidence for improving AFlow

Distinguish an AFlow defect from a project implementation failure. AFlow defects
include controller, manager protocol, state persistence, resume, lifecycle,
active-plan, upgrade routing, repartition transaction, terminal-report, and
harness-adapter failures.

For every new anomalous fingerprint, preserve a compact incident record beside
the guardian state, even if a safe recovery succeeds. Include the classification,
run/checkpoint/step, bounded artifact paths, process evidence, recovery action,
and outcome. Do not create incident records for healthy observations.

Promote an incident to an AFlow fix plan when it identifies a confirmed defect,
a recurring edge case, or a manager/recovery policy gap worth addressing:

1. Load `references/aflow-defect-plan.md`.
2. Write one decision-complete plan per incident fingerprint. Prefer
   `<aflow-source>/plans/in-progress/guard-discovered-<date>-<slug>.md`; if that
   checkout is unsafe or unavailable, write it beside the guardian state and
   report the path.
3. Include exact durable artifact references and a minimal reproduction. Do not
   paste complete transcripts.
4. Record any safe operational workaround separately from the product fix.
5. Default to plan-only. Implement a narrow AFlow repair during a scheduled
   tick only when the user's original guard request explicitly authorizes
   fixing AFlow code and the primary checkout is unambiguously safe.
6. For an authorized code fix, follow repository planning requirements, run
   focused manager/runtime tests, reinstall the verified AFlow checkout, and
   resume only after confirming no active conflicting harness.

Do not mistake a normal project-level implementation or test failure for an
AFlow bug. Still record how AFlow classified, reported, recovered, or stopped
around that failure; those orchestration observations may expose a separate
AFlow edge case.

Do not create duplicate incident records or plans for an unchanged fingerprint.
Update the existing artifact with new evidence instead.

## Pause and notify

Resolve the current automation from injected metadata or its exact unique name.
Use `automation_update` with its full preserved fields and `status=PAUSED`.
Pause rather than delete so the audit remains available.

Pause immediately when:

- the run completed
- a failed or incomplete run was not recovered during the current tick
- recovery did not produce exactly one verified controller
- semantic owner input is required
- state or ownership is unsafe
- the user explicitly stops guarding

Mark a notification fingerprint with the snapshot script before reporting.
Leave only actionable events visibly in the same task: terminal success,
recovery performed, an unrecovered failure, unsafe state, required owner
decision, or a newly written fix plan. Include the run ID, checkpoint/step,
evidence, recovery attempted, fix-plan path when present, and exact next action.
Never route guard reports to another task.

For an actionable event, return the concise report directly in the initiating
task instead of `DONT_NOTIFY`. Pause first when required. Healthy observations
still return exactly `DONT_NOTIFY`.

## Verification for AFlow source repairs

For manager, resume, recovery, terminal-report, or upgrade fixes, start with the
focused manager/context/runtime tests. Run app-server tests in their own project
environment, then compile and check the diff. Report focused results separately
from broader baseline failures; never call a broader suite green when known
baseline failures remain.
