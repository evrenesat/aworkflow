---
name: aflow-review-squash
description: "Review a completed autonomous AFlow plan, compare the full accumulated implementation against the original plan, and either squash the whole handoff into one final commit or create a focused fix plan for the remaining failed checkpoints or behaviors."
---

# Review Squash AFlow Implementation

Use this skill only for the final review pass of work produced under a aflow plan that includes `Git Tracking`. It is meant to be installed as a static skill and driven by prompt context from the workflow engine.

## Behavior

- Load the active original aflow plan before reviewing code or history.
- If the prompt already names the original plan, use that directly. Otherwise fall back to the repo's original-plan selection rules.
- Assume the happy path is a completed autonomous run. Review the whole accumulated handoff, not just one checkpoint batch.
- Treat files under `plans/` as architect or reviewer-owned artifacts. If an implementation commit modifies plan files unexpectedly, reject that work unless the user explicitly asked for plan-file commits from the implementer.
- Treat checkpoint/version commit prefixes such as `cp4 v01`, `cp4 v02`, and `cp5 v01` as the primary review-tracking mechanism. Use exact SHAs as supporting evidence, not as the only way to understand state.
- Treat `Git Tracking` as lightweight support metadata. In worktree-first plans, `Plan Branch` and `Pre-Handoff Base HEAD` may have been auto-populated by the engine, while `Last Reviewed HEAD` and `Review Log` may be absent.
- Treat prompt-supplied concrete review context as authoritative when it is present. Use repo discovery only when the prompt leaves a target ambiguous.
- If the original plan still has unchecked checkpoints, do not repurpose this skill for routine checkpoint review. Rerun the autonomous executor unless the user explicitly asks for a different workflow.
- If the full accumulated work is acceptable, approve and squash once at the whole-plan level.
- If the full accumulated work is not acceptable, do not squash. Create a focused non-checkpoint fix plan for the failed checkpoints or behaviors instead of a whole-plan redo.
- If the implementation is behaviorally correct, this review turn owns all approval-grade git/tracking chores needed for squash approval.
- Treat `aflow` as the canonical spelling.
- Compact `DEVLOG.md` to one handoff entry only when a squash actually happens and multiple handoff entries exist.

## Core Rule

The original plan file is the source of truth for long-lived review state. Fix plans are temporary non-checkpoint overlays for rejected work, not replacements for the original plan.

## Required Inputs

Following plan paths should be provided by the prompt;

ORIGINAL_PLAN: This is the original implementation plan.
ACTIVE_PLAN: This maybe same as the original plan file, or could be a transient follow-up plan focused on fixing of review findings.
NEW_PLAN_PATH: This is the path for a possible follow-up plan for the findings of your review.    
If you need to create a follow-up fix plan, write it exactly to `NEW_PLAN_PATH`. Do not invent a different filename.

Before reviewing or squashing, identify the active original aflow plan under `plans/in-progress/`.

Selection rules:

1. If the user names a plan file, use it.
2. Otherwise search `plans/in-progress/` for original plan files containing `Pre-Handoff Base HEAD`.
3. Filter candidates to the current branch recorded in `Plan Branch`.
4. Ignore temporary fix plans when choosing the original long-lived plan.
5. If exactly one original plan remains, use it.
6. If multiple original plans remain, stop and ask the user which plan to use.

## Review Workflow

1. Read the original plan's `Git Tracking` section and checkpoint state.
2. Confirm the current branch matches `Plan Branch`.
3. Confirm the original plan is effectively complete or that the user explicitly asked for a non-standard review.
4. Determine the review start point in this order:
   - honor an explicit user instruction such as "last review onward"
   - otherwise use the latest reviewed range recorded in `Review Log`
   - otherwise use `Last Reviewed HEAD` when present and still useful
   - otherwise use `Pre-Handoff Base HEAD`
5. Review the full accumulated implementation from that start point through `HEAD`.
6. Report:
   - the number of new commits since the last review
   - the total number of commits since `Pre-Handoff Base HEAD`
   - the checkpoints and behaviors covered by the accumulated run
7. Review the actual code state, not just commit messages.
8. When reporting or updating review state, prefer checkpoint/version labels such as "reviewed through `cp5 v01`". Include exact SHAs only when they materially help disambiguate the history.

## Approval Path

If the accumulated work looks correct:

1. Use the original plan's `Pre-Handoff Base HEAD` as the squash anchor for the final accumulated handoff.
2. Produce the final accumulated commit, delete stale fix plans, and repair `Git Tracking` / `Review Log` state yourself in this review turn when approving.
3. Update the original plan's `Git Tracking` and `Review Log` to capture the approved and squashed result.
4. Delete any remaining fix plans for that handoff unless the user explicitly asked to keep them.
5. Rewrite history so every commit after the squash anchor becomes one final accumulated commit.
6. Use a non-interactive workflow. Prefer `git reset --soft <squash-base>` followed by a new commit over interactive rebase.
7. Write a fresh final commit message whose first line includes the branch name and a meaningful summary of the accumulated handoff.
8. If `DEVLOG.md` exists and multiple handoff-related entries were added or updated during the handoff, compact them to one entry that matches the final squashed change.
9. If the original plan's checkpoints are all complete, leave the original plan in `plans/in-progress/`. The workflow engine finalizes the original plan location after terminal success.
10. Treat dirty changes in plan files that are intentionally part of the final handoff state as part of finalization, not as unrelated worktree noise. Still stop if truly unrelated dirty changes remain and make the squash ambiguous.

## Rejection Path

If the accumulated work is not acceptable:

1. Do not squash commits.
2. Create a new aflow fix plan that covers only the failed checkpoints or behaviors against the current `HEAD`.
3. Ensure `plans/in-progress/` exists before writing the fix plan. Create it if it does not exist.
4. Write the fix plan exactly to `NEW_PLAN_PATH`. Keep the file focused on the failed checkpoints or behaviors from this review.
5. The fix plan must be self-contained, non-checkpoint, and must not require the implementer to read prior chat context.
6. When creating a new fix plan, delete older superseded fix plans for the same original handoff by default unless the user explicitly asks to keep them.
7. After creating the new fix plan, `plans/in-progress/` should contain only the original handoff plan plus that newest fix plan for the same handoff.
8. Update the original plan:
   - append a `Review Log` entry only when it materially helps later review or ambiguity resolution
   - update `Last Reviewed HEAD` only when it clearly helps and does not create brittle bookkeeping pressure
9. Keep `Pre-Handoff Base HEAD` unchanged.
10. Do not compact `DEVLOG.md`.

## Stop And Escalate If

- no original aflow plan with `Git Tracking` can be found
- the current branch does not match the plan's `Plan Branch`
- `Pre-Handoff Base HEAD` is missing or no longer reachable
- the original plan is still mid-flight and the correct next action is to resume the autonomous executor
- the worktree has unrelated dirty changes that make the review or squash ambiguous
- multiple original plan files are plausible and the active one cannot be determined safely

## Verification

Before finishing, verify:

- the reviewed range is correct relative to the original plan's checkpoint/version review history and any helpful support metadata such as `Last Reviewed HEAD` or `Pre-Handoff Base HEAD`
- the reported commit counts match git history
- after approval, the branch contains exactly one accumulated handoff commit after `Pre-Handoff Base HEAD`
- after approval, no stale fix plans remain in `plans/in-progress/`
- after approval, all approval-grade git/tracking chores were completed by the reviewer in the same turn
- after approval, the final squashed commit includes the branch name plus a meaningful summary
- after final approval, the original handoff plan stays intact long enough for the workflow engine to finalize it
- after rejection, no history rewrite occurred
- after rejection, `plans/in-progress/` contains only the original handoff plan plus the newest focused fix plan for that handoff
- after rejection, superseded older fix plans were deleted unless the user asked to keep them
- `DEVLOG.md` was compacted only when a squash occurred and multiple relevant entries existed
