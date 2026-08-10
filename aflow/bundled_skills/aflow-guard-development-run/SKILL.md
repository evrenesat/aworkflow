---
name: aflow-guard-development-run
description: Guard any explicitly requested AFlow run in the task that requested supervision, with one attached heartbeat automation, bounded durable-state snapshots, one-shot safe recovery, automatic scheduler shutdown, multi-format visual reporting, optional completion/owner-action/reviewer-turn email delivery, worker-upgrade strategy auditing, and evidence or fix-plan extraction for improving AFlow itself. Use whenever the user asks to babysit, guard, monitor, or keep an AFlow workflow running, whether it is ordinary product work or an AFlow development experiment. Do not invoke automatically for an unguarded run; external guardianship is temporary resilience scaffolding until AFlow is stable enough not to need it.
---

# Guard an AFlow Run

Guard the exact AFlow run the user selected. The run may implement any project;
it does not need to develop AFlow. Keep it progressing while treating anomalies
as bounded evidence for improving AFlow's own resilience.

## Non-negotiable invariants

- A daemon-owned run (one with control-plane launch ownership) is observed and
  controlled through the authenticated control plane. Never attach a ChatGPT
  heartbeat guard to it, and use the server's explicit owner-stop or resume
  operation rather than a local recovery attempt.
- Legacy/manual runs and an explicitly requested SRE stabilization retain this
  guard path. The daemon transition changes no other guard authority.
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
- Treat the selected team as the entry team for worker resolution, not as a
  fixed roster or proof that one worker model will handle the whole run. Resolve
  the frozen worker selector, full `upgrade_to` chain, per-step overrides, and
  observed worker turns before describing model behavior.
- Never count architect, senior-architect, reviewer, reworker, or manager turns
  as worker-model samples. Never label an unsampled configured worker successful
  or unsuccessful.
- Render in-task PNG and downloadable PDF reports as the same single A4
  dashboard page. Use a tight multi-box grid with data-derived progress,
  status, upgrade-chain, and worker-sample graphs; keep the standalone HTML as
  the expandable full-detail surface.
- Prefer the fastest safe completion when a blocker offers several valid
  interpretations and one preserves the plan's core outcome with less scope,
  risk, downtime, or cost. Choose that reversible minimum without pausing for
  owner input, record the decision in the active plan or recovery evidence, and
  keep the deferred enhancement explicit.
- Ask for owner input only for changed meaning, conflicting requirements,
  destructive handling, ambiguous ownership, or inability to preserve accepted
  work.
- Send email only when the user explicitly requests it. Default to completion
  and owner-decision/action reports. Enable reviewer-turn reports only as a
  separate explicit opt-in; never infer that preference from ordinary guard
  authorization. Never email healthy progress or automatically recoverable
  friction. Email delivery is independently throttled to at most one report
  per two hours for a repeated event fingerprint; this does not slow or pause
  the guard heartbeat, retry, recovery, or controller-liveness checks.

## Establish the guard

1. Read the nearest `AGENTS.md` files in the guarded project and, when known,
   the AFlow source checkout. Do not reject a guarded project because it is not
   the AFlow source repository.
2. Resolve an exact run ID and inspect its `.aflow/runs/<run-id>/run.json`.
   Never guard “the latest run” without first pinning the resulting ID.
3. Record the guarded repository, optional AFlow source checkout, run ID, plan,
   worktree, feature branch, workflow, entry team, starting worker selector,
   complete frozen worker-upgrade chain, start step, maximum turns, and optional
   Screen session. Treat `run.json` as read-only.
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
   task, normally every five minutes. Set failed-runs-only notifications. The
   five-minute heartbeat is the guard's retry and liveness cadence; it is
   independent of the optional two-hour email-delivery throttle. Do not
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

## Resolve the worker strategy

Interpret a requested worker model or effort as the starting worker role unless
the user explicitly requests a fixed single-model run. Resolve candidate entry
teams whose effective `worker` selector matches that starting role. When more
than one matches, prefer the unique valid entry team that preserves the requested
starting worker and exposes the intended non-cyclic `upgrade_to` chain; do not
silently choose a terminal same-named team merely because its name resembles the
worker profile.

From frozen configuration, follow `teams.<team>.upgrade_to` until the chain ends.
Also inspect durable manager decisions and `pending_step_team_override` when an
actionable report needs the realized strategy. Distinguish:

1. configured worker candidates;
2. workers actually sampled by finalized `role=worker` turns; and
3. upgrades or overrides actually applied.

If the resolved entry team has no upgrade path, say so before claiming adaptive
intelligence or effort. If the user requested dynamic scaling and another unique
entry team begins with the same requested worker while providing that path, use
that entry team. If no such configuration exists, report the exact configuration
gap instead of inventing a worker chain or waiting through repeated rejections.

After repeated material rejections from the same worker, audit whether AFlow
used the next configured team. A configured-but-unapplied upgrade is orchestration
evidence, not proof that the entry worker was intentionally the only candidate.
Do not interrupt a live child or create a second controller; preserve the run and
promote a repeatable missing-upgrade condition through the normal AFlow-defect
evidence path.

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

Exception: when reviewer-turn email is explicitly enabled and the snapshot
shows a finalized reviewer boundary newer than the last delivered fingerprint,
inspect only that newest finalized reviewer `result.json`, render and send its
bounded report, record delivery after Gmail succeeds, then return exactly
`DONT_NOTIFY` if the tick is otherwise healthy. Do not reread earlier reviewer
turns or send catch-up duplicates when this mode is first enabled.

For an anomaly, inspect only:

- selected `run.json` fields
- the newest finalized turn `result.json`
- the newest manager `boundary.json` and `result.json`
- at most the final 12 KiB of the relevant stdout or stderr
- controller descendants, Screen presence, and concise git status

Do not load whole stdout/stderr files, repeated unchanged artifacts, or prior
scheduled-task history.

## Recover safely

### Resolve bounded product ambiguity

Use fastest-safe completion as the default tie-breaker when all of these hold:

- the selected behavior preserves the plan's primary usable outcome;
- it is the smallest reversible choice and does not weaken security, privacy,
  authorization, licensing, data integrity, or required acceptance checks;
- it introduces no destructive migration, public publication, new credential
  authority, or spend beyond an approved ceiling;
- it does not contradict an explicit user preference; and
- the omitted behavior can remain a named later enhancement.

Prefer scope reduction over inventing transformation behavior. For example,
when a pinned audio model only supports source-duration covers and custom
duration would require undefined trimming, looping, padding, or time-stretching,
retain source duration, display it clearly, defer custom duration, revise the
plan accordingly, and continue. A prior request for an ASAP beta or minimum
downtime strengthens this tie-breaker.

Do not use this rule to reinterpret a core product requirement, bypass a safety
or acceptance gate, spend without authority, discard accepted work, or make an
irreversible choice. Those cases still require owner input.

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

## Recover a safe transient environment failure

terminal_transient_environment is narrower than terminal_failed. Use it only
when the bounded terminal artifact tail proves the known
missing_reasonix_bubblewrap fingerprint: a Reasonix sandbox requires
bubblewrap or bwrap, and the executable is absent or unavailable. Pass that
kind explicitly to the snapshot helper; it never infers a remediation from a
generic failure.

A prerequisite remediation is allowed once for that unchanged incident only
when the original guard authority covers it and the guarded host policy permits
the exact additive change. Confirm the host, package source, package name, and
post-install executable before changing anything. For this fingerprint, that
means the trusted host package manager's bubblewrap package and a verified
bwrap inside the guarded execution environment. Do not update unrelated
packages, alter sandbox policy, change Reasonix configuration, or retry a
different failure fingerprint. Mark the recovery attempt before remediation.

If the predecessor is not resumable, a replacement is permitted only when all
of these are true:

- the predecessor is terminal failed, has turns_completed equal to zero, no
  active controller, no feature branch, no worktree path, and no continuation
  parent;
- its original plan exists, and run.json has complete frozen configuration,
  workflow, team, selected start step, effective turn budget, and extra
  instructions;
- the exact replacement command is reconstructed from those durable fields,
  with explicit plan, workflow, team, start-step, and max-turns flags; include
  saved extra instructions after the option separator;
- the frozen configuration of the newly created run exactly equals the
  predecessor's frozen configuration, and the original plan, workflow, team,
  start step, turn budget, and extra instructions also exactly match;
- no matching predecessor or successor controller is active before launch, and
  the successor snapshot proves exactly one logical controller immediately
  after launch.

This is a zero-turn, no-mutation replacement, not a resume, not a scope reset,
and not a fresh choice of flags. If any field is absent, differs, or requires
interpretation, pause and write a fix plan instead.

Persist the linkage only after the successor's own snapshot proves one
controller. Record the predecessor state with the
--replacement-successor-run-id option using the same helper hash and same-task
routing IDs. This is a dedicated linkage operation: it must return
replacement_linked and must not classify the active successor as an active
predecessor controller. It derives the sole shared recovery and notification
fingerprint as predecessor provenance; if historical evidence is ambiguous,
supply --replacement-recovery-fingerprint with a value already present in both
lists. Validation failure returns invalid_state and writes neither observation
state nor linkage state.

On success the helper atomically stores predecessor ID, successor ID, original
recovery fingerprint, successor-controller evidence, and timestamp. A
same-successor record with an unsafe observation fingerprint is migrated once
to the original recovery fingerprint. Then repin the existing heartbeat to the
successor run ID, preserving every other automation field. Re-check the prompt,
both routing IDs, the successor's frozen configuration, and
exactly-one-controller evidence before leaving the automation active.

If the harness adapter does not emit enough durable data to make this
comparison or replacement mechanical, or if the recovery policy cannot safely
express the environment remediation, promote that gap to an AFlow fix plan and
pause. Do not paper over it with guessed flags or a second controller.

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

When the user requested email reporting, read
`references/reporting-and-email.md` before producing or sending a report. Use
`scripts/aflow_guard_report.py --bundle-dir` to render the same bounded evidence
as Markdown, interactive standalone HTML, a mobile-readable single-A4 PNG, a
matching single-page A4 PDF, and mobile-first email HTML. The script accepts
`completed`, `needs_owner_action`,
and explicitly enabled `reviewer_turn` states and rejects secret-bearing fields
or common credential patterns.

For reviewer-turn delivery, send only after a finalized reviewer result exists.
Use the exact finalized turn ID plus verdict as the notification fingerprint,
record successful delivery beside guardian deduplication state, and never send
the same fingerprint twice. Include checkpoint, verdict, review model, duration,
material findings, next workflow action, and current model sample size when
known. A reviewer email does not pause the guard or turn an otherwise healthy
tick into an actionable in-task notification.

Keep the initiating task as the authoritative report surface. Email is an
additional delivery channel, not a replacement. Show the same report in the
task by embedding the generated PNG and linking the interactive HTML and PDF.
Do not rely on an interactive visualization reference as the only task report.
Use only a short delivery sentence around those artifacts so rendering does not
consume model tokens. Attach the HTML or PDF to email only when useful. Resolve the recipient from the
user's explicit instruction or a previously authorized destination; never
guess, harvest contacts, or hard-code a project-wide address. Use the Gmail
connector only for the requested outbound send and identity verification. Do
not search or read inbox contents for report delivery.

For an actionable event, return the concise report directly in the initiating
task instead of `DONT_NOTIFY`. Pause first when required. Healthy observations
still return exactly `DONT_NOTIFY`.

## Verification for AFlow source repairs

For manager, resume, recovery, terminal-report, or upgrade fixes, start with the
focused manager/context/runtime tests. Run app-server tests in their own project
environment, then compile and check the diff. Report focused results separately
from broader baseline failures; never call a broader suite green when known
baseline failures remain.
