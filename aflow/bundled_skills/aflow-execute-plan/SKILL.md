---
name: aflow-execute-plan
description: "Lightweight execution reinforcement for an existing AFlow plan. Use when an agent must resume from the first unchecked checkpoint and keep the plan synchronized with verified progress."
---

# Execute AFlow Plan

Use this skill only to execute an existing aflow plan autonomously. Treat `aflow` as the canonical spelling. This skill is intentionally lightweight, the plan itself should already carry the detailed execution contract.

The plan file is the source of truth. Do not rely on chat memory when the plan, repository state, test output, or git history disagree.

## Test and command completion

- Prefer foreground commands and the execution tool's process/session handle.
  Read the command's actual exit status and bounded output before deciding to
  wait, retry, or report success. A launcher exit or a successful `tail` is not
  the test result; preserve the tested command's status when piping output.
- If background execution is necessary, retain the exact child handle/PID and
  collect its exit status (`wait "$child_pid"` only from its owning shell).
  Across tool calls, use a supported persistent session or an owned completion
  receipt tied to that invocation; a vanished PID alone does not prove success.
- Never use broad `pgrep -f` command-text matching to decide whether your test
  finished: it can match the polling shell itself or another run. Do not use
  fixed long sleeps or detached `nohup` polling in place of child completion.
- Use only timeout options supported by the installed runner. A usage error
  means the command already failed; diagnose it immediately. Prefer an existing
  execution-tool timeout or a verified platform timeout wrapper when needed.
- Once required checks have finished, record the result and continue the
  handoff. Do not rerun matching successful checks merely to fill a wait.
  A whole-harness exclusive resource remains occupied during shell/test waits;
  never manually release its lease while the harness can still issue requests.

## Storage and evidence hygiene

- Before heavy writes, measure free bytes and inodes and, where applicable, backing capacity; compare against the plan's stated peak allocation and reserve.
- Serialize heavy campaigns through the environment's shared lock; do not start a parallel heavy campaign that the lock exists to prevent.
- If space is low, do not immediately `AFLOW_STOP`: inspect bounded category usage, reuse valid caches and artifacts, and remove only known disposable owned intermediates (or native-manager unused caches) within authorization. Rerun the capacity check and continue in the same attempt.
- Honor installed native cleanup helpers where available; do not invent destructive recovery, expand disks, or delete other owners' data.
- Never delete active work, unique data, retained base or release images, models, session history, AFlow lineage, or proofs required for unresolved failures.
- If no safe recovery suffices, report the actual free space, the conservative required peak/reserve, the actions tried, and the concrete remaining external decision.
- Keep primary logs, exact commands and results, revision/dirty fingerprints, and hashes inspectable. Bound verbose logs and captures at production. Select compact useful evidence; make no per-review copies or repeated full captures.
- Clean per-case QEMU writable disks after guest exit/unmount and successful evidence extraction; retain failed or unique images until diagnosed. A task may dispose of scratch it explicitly created as disposable, never inherited operation directories simply because they are old.
- Reuse valid prior verification and caches; do not run clean builds or full suites merely because a new checkpoint started.

## Plan Shape

- If the active plan is the original handoff plan with checkpoint headings, execute it checkpoint by checkpoint.
- If the active plan is a review-generated follow-up plan without checkpoint headings, execute that focused plan as written.
- Do not invent checkpoint structure in a review-generated non-checkpoint plan.

## Core Rules

- For original checkpointed plans, execute one checkpoint at a time in order.
- For original checkpointed plans, start at the first unchecked checkpoint unless the prompt explicitly says otherwise.
- Re-read the plan from disk before each checkpoint and again after verification.
- Treat the on-disk plan as the source of truth for progress, commit boundaries, and completion.
- For checkpointed plans, treat step checkboxes as required workflow state, not optional notes.
- For checkpointed plans, after implementation and checkpoint-level verification pass, validate each step in the active checkpoint one by one against the actual code, tests, and observable behavior before checking it off.
- Do not check off a step just because the checkpoint appears complete overall. Each step must be explicitly confirmed.
- Do not mark a checkpoint complete before every step in that checkpoint has been individually validated and checked off, and the required verification still passes.
- Treat a top-level `## User Acceptance Pending` section after the checkpoints as handoff information, not executable work. Do not check it off or retry the worker for pending owner-only checks; implementation defects and required automated verification failures remain blocking.
- Do not create checkpoint commits, final approval commits, squash commits, or review-bookkeeping commits. Reviewer workflows own all commit creation and approval-grade git bookkeeping.
- Do not hide verified work behind stale plan state or ambiguous git state.
- Stop and escalate when the plan is ambiguous, contradictory, unsafe, or buried under unrelated dirty changes.

## Required Inputs

Following plan paths should be provided by the prompt;

ORIGINAL_PLAN: This is the original implementation plan.
ACTIVE_PLAN: This maybe same as the original plan file, or could be a transient follow-up plan focused on fixing of review findings.

Before acting, identify:

- the active plan file
- whether it is a checkpointed handoff plan or a focused non-checkpoint follow-up plan
- for checkpointed plans, the first incomplete checkpoint (`### [ ] Checkpoint ...`)
- any `Git Tracking`, `Dependencies`, `Verification`, `Done When`, or `Stop and Escalate If` instructions attached to that checkpoint

If the prompt already names a concrete plan file, use it. If not, discover the single active original plan under `plans/in-progress/` when that is unambiguous. If the plan file is missing, multiple candidate plans exist, or the next checkpoint cannot be identified safely, stop and ask for clarification.

## Execution Loop

- For checkpointed plans:
  - Read the active checkpoint fully before editing code.
  - Implement only that checkpoint's scope.
  - Run the exact verification commands from the plan.
  - If verification passes, re-read the checkpoint steps and validate them one by one in order against the implemented code and verification evidence.
  - Check off only the steps you explicitly validated. If any step is not clearly satisfied, leave it unchecked, fix the implementation, and re-run verification as needed.
  - Check off the checkpoint itself only after every step in that checkpoint is checked and the checkpoint still satisfies its `Done When` conditions.
  - Only then update the plan state.
  - Leave the verified checkpoint work uncommitted for review to validate and commit.
  - Only then move on to the next unchecked checkpoint.
- For review-generated non-checkpoint follow-up plans:
  - Execute the focused plan fully as written.
  - Run the exact verification commands from that plan.
  - If the focused plan uses task-list steps, keep them synchronized with validated progress using the same step-by-step standard.
  - Do not look for or invent checkpoint headings.
  - Finish only after the focused follow-up scope is verified and the plan state is synchronized. Do not create approval-grade commits yourself.

Do not use this skill to invent a second execution spec. The plan should already define the checkpoint details, verification, and commit policy.

## Git Workflow

- Before stopping, check `git status --short`, `git diff --name-only`, and `git diff --stat` so the reviewer inherits an accurate dirty worktree.
- The dirty worktree must contain only the active checkpoint's scoped changes plus allowed plan progress edits. If unrelated dirty files are present, stop and escalate instead of leaving an ambiguous handoff.
- Leave commit creation, squash/rewrite decisions, fix-plan cleanup, and `Git Tracking` / `Review Log` approval bookkeeping to reviewer workflows.
- Do not rewrite history unless the plan explicitly asks for it.

## Verification Standard

- Run the exact required verification commands for the active checkpoint.
- Treat the checkpoint as incomplete until those commands succeed.
- After a failure, use small targeted diagnostic reruns to locate the cause, then rerun the required commands affected by the fix. Keep failed or interrupted checks marked as unverified.
- Do not replace required checks with weaker smoke tests.
- Do not add routine broad regression suites to a checkpoint when its plan assigns them to cumulative final review. If an existing explicit full-suite command appears misplaced, report it to the coordinator; run it as written unless the plan is formally revised before the attempt. Never silently substitute a focused check.
- If the plan names observable acceptance criteria in addition to commands, confirm both the commands and the behavior.

## Step Validation Standard

- After checkpoint-level verification passes, review every step in the active checkpoint in order.
- For each step, confirm the step's promised outcome is actually implemented, not just partially implied by neighboring work.
- Use concrete evidence for each step such as code inspection, test output, command results, or direct behavioral checks named in the plan.
- Update the plan so the step checkbox state reflects what you validated on disk.
- If a step cannot be confidently validated, treat the checkpoint as still in progress.
- Do not leave a checkpoint checked while any of its steps remain unchecked.

## Stop And Escalate If

- the next checkpoint is unclear
- the plan conflicts with the repository's actual structure
- required files, commands, or dependencies are missing
- the worktree contains unrelated dirty changes that make the checkpoint or commit ambiguous
- the plan requires a destructive git action you were not explicitly authorized to perform
- verification still fails after reasonable diagnosis and the failure suggests the plan is wrong or incomplete

When any of the above conditions is irrecoverable and cannot be resolved within the current turn, emit exactly the following line on its own in stdout or stderr before stopping:

```
AFLOW_STOP: <reason>
```

The workflow engine detects this line and fails the run immediately with the extracted reason instead of spending more turns on the same blocker. Do not emit this sentinel for normal plan-not-done states; use it only for hard blockers that require human intervention.
