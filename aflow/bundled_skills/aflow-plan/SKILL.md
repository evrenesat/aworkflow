---
name: aflow-plan
description: "Create a strict AFlow checkpoint handoff plan for coding work that will be implemented by another model, tool, or later session. Use when the user explicitly wants the aflow pattern or a checkpoint-based handoff plan."
---

# AFlow Handoff Plan

Use this skill only for aflow-style planning. It is designed to be installed as a static skill and driven by prompt context from the workflow engine or harness.

## Behavior

- Treat prompt-supplied concrete plan context as authoritative when it is present.
- If the prompt does not give a concrete path and there is exactly one safe target in the repo, use that narrow fallback. Otherwise stop and ask.
- Ask targeted questions about scope, constraints, dependencies, tradeoffs, and acceptance criteria.
- Confirm risky assumptions before locking the final plan.
- Keep questions concise and sequenced.
- Produce a strict checkpoint handoff plan, not a standard implementation plan.
- Keep the plan execution-model agnostic. The plan must work for checkpoint-scoped CP execution and for autonomous non-CP execution without declaring a mode field.
- Make the plan durable under crash, rerun, and later-session handoff. A fresh agent or thread must be able to resume from disk without relying on prior chat context.
- Treat the original handoff plan as the long-lived ledger under `plans/in-progress/` until the handoff is complete.
- Treat reviewer-created fix plans as temporary overlays for rejected work, not replacements for the original plan's long-lived state.
- Make the final plan self-sufficient. It should not rely on a separate heavy executor skill to supply missing workflow details later.
- Treat `Git Tracking` as lightweight support metadata in worktree-first workflows. The engine may populate or refresh branch/base values at startup, and the plan must not require manual git bookkeeping before execution begins.
- Treat the current execution repo root as authoritative for in-repo commands. Do not hardcode a guessed primary checkout path into plan steps, bootstrap commands, verification commands, or doc references.
- Treat `aflow` as the canonical spelling.
- If the user asks for planning but does not want aflow, do not use this skill.

## Core Rule

The plan must be decision complete. The implementer should not need to choose behavior, precedence, fallback, validation policy, or verification strategy on their own.

## Commit Ownership Rule

Checkpoint plans must separate implementation handoff state from review acceptance boundaries.

Requirements:

- In checkpoint-review workflows, implementers verify the assigned checkpoint and leave scoped changes uncommitted for reviewer validation. They must not create checkpoint commits, final approval commits, squash commits, or review-bookkeeping commits.
- Reviewer workflows own checkpoint approval commits and approval-grade git bookkeeping in checkpoint-review workflows.
- Autonomous non-review workflows may create checkpoint boundary commits only when no later reviewer workflow owns that boundary.
- Every checkpoint must spell out both the implementation handoff state and the review acceptance boundary so agents do not treat a reviewer-owned commit as an implementer task.
- If the selected workflow's commit ownership is ambiguous, stop and ask rather than guessing.

## Execution Root Safety Rule

For repository-local work, the plan must be safe under worktree-first execution.

Requirements:

- Treat the runtime-selected repo root or worktree as the source of truth for all in-repo commands.
- Prefer repo-relative commands and paths for files inside the repository.
- If a bootstrap command needs to anchor execution, use execution-root-safe discovery such as `git rev-parse --show-toplevel` rather than a guessed checkout path.
- Do not write plan commands that `cd` into a hardcoded host-specific checkout such as `/Users/.../repo` or `/home/.../repo` for repository-local work.
- Do not hardcode absolute paths to repository files in `Context Bootstrapping`, `Verification`, `Behavioral Acceptance Tests`, or the `Plan-to-Verification Matrix`.
- Absolute paths are allowed only for clearly external artifacts explicitly supplied by the user, environment, or harness and not mirrored inside the execution repo.
- When prompt context names both a primary checkout and a worktree, prefer commands that remain correct in the worktree.
- If the correct execution root is ambiguous, stop and ask instead of guessing a canonical checkout path.

## Required Plan Sections

Every final aflow plan must include these sections in substance, even if headers are adapted slightly for readability:

1. `Summary`
2. `Git Tracking`
3. `Done Means`
4. `Critical Invariants`
5. `Forbidden Implementations`
6. `Checkpoints`
7. `Behavioral Acceptance Tests`
8. `Plan-to-Verification Matrix`
9. `Assumptions And Defaults`

Do not omit any of them when they are needed to prevent implementation drift.

## aflow Output Contract

Produce a strict checkpoint plan for cross-model or later-session handoff.

Requirements:

- Use Markdown task lists (`- [ ]`) for checkpoints and internal steps.
- Define done at project and checkpoint level.
- Keep checkpoints atomic and independently verifiable.
- Write each checkpoint so a fresh agent or thread can resume from disk with no hidden chat context.
- Include explicit context bootstrapping commands before edits.
- Make bootstrap and verification commands execution-root-safe for worktree-first runs. For repository-local work, do not hardcode host-specific absolute checkout paths.
- List allowed files and forbidden files or systems per checkpoint.
- Include anti-shortcut constraints and preserved behaviors.
- Include scoped and non-regression verification commands per checkpoint.
- Require a git boundary per checkpoint and explicitly state which workflow role owns that boundary.
- Separate checkpoint implementation completion from checkpoint review acceptance. In checkpoint-review workflows, the implementation handoff state leaves verified scoped changes uncommitted; the reviewer acceptance boundary creates the `cpN vNN` commit.
- Define a review acceptance commit message format per checkpoint that includes the checkpoint/version prefix, the branch name, and a meaningful summary on the first line. Every checkpoint commit, including the first commit for that checkpoint, must use an explicit version starting at `v01`. Example first line: `cp1 v01 feature-branch: implement parser cleanup`
- Make checkpoint state durable enough for restart. Step checkboxes should reflect meaningful progress inside the checkpoint and help a later rerun or reviewer understand what is already done.
- Do not encode whether plan checkboxes are updated by the executor or the reviewer. The consuming execution or review skill owns that policy.
- Include explicit stop-and-escalate conditions.
- Require an explicit documentation impact review that updates relevant existing docs when the change warrants it.
- Require a `Git Tracking` section that captures `Plan Branch` and `Pre-Handoff Base HEAD` as lightweight support fields. These fields may start empty in a fresh plan and be populated by the workflow engine at startup. `Last Reviewed HEAD` and `Review Log` are optional support fields, not mandatory bookkeeping.
- Add `Critical Invariants` for rules that must hold across the entire implementation.
- Add `Forbidden Implementations` for shortcuts the implementer might otherwise take.
- Add `Behavioral Acceptance Tests` as observable outcomes, not just test commands.
- Add a `Plan-to-Verification Matrix` that maps each important requirement to one concrete verification method.

Use this checkpoint skeleton:

```markdown
### [ ] Checkpoint N: <name>

**Goal:**

- <narrow checkpoint outcome>

**Context Bootstrapping:**

- Run these commands before editing:
- `git rev-parse --show-toplevel`
- `<repo-relative or execution-root-safe command>`

**Scope & Blast Radius:**

- May create/modify: [files]
- Must not touch: [files/systems, including `plans/**` except read-only access to the assigned plan file and the minimal progress-tracking edits performed by the consuming execution or review workflow]
- Constraints: [anti-shortcuts + preserved behavior]
- Dirty-worktree contract: before handoff, `git diff --name-only` may list only files in `May create/modify` plus allowed plan progress edits; unrelated dirty files require `AFLOW_STOP`.

**Steps:**

- [ ] Step 1: ...
- [ ] Step 2: ...
- [ ] Step 3: ...

**Dependencies:**

- Depends on Checkpoint N-1.

**Verification:**

- Run scoped tests: `<exact repo-relative or execution-root-safe command>`
- Run non-regression tests: `<exact repo-relative or execution-root-safe command>`

**Implementation Done When:**

- Verification commands pass cleanly.
- Every checkpoint step has been validated against code, tests, or observable behavior before being checked off.
- <observable condition>
- Dirty worktree contains only scoped checkpoint changes and allowed plan progress edits.
- Before stopping, the implementer has run `git status --short`, `git diff --name-only`, and `git diff --stat`.
- In checkpoint-review workflows, the implementer has not created a checkpoint commit. Verified changes remain uncommitted for reviewer validation.

**Review Acceptance Boundary:**

- The reviewer accepts the implementation and creates a checkpoint-scoped git commit with first line:
  ```text
  cpN vNN <branch-name>: <meaningful summary>
  ```
  The first commit for a checkpoint must use `v01`. Later fix passes for the same checkpoint increment the version number, for example `cp4 v02`, `cp4 v03`, and so on.

**Stop and Escalate If:**

- <explicit failure mode — when this condition is irrecoverable, emit `AFLOW_STOP: <reason>` on its own line so the workflow engine fails immediately instead of looping>
- `git status --short` or `git diff --name-only` shows files outside the checkpoint scope or user-owned dirty changes not caused by this checkpoint.
```

Use this `Git Tracking` skeleton in the final plan:

```markdown
## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``
```

`Plan Branch` and `Pre-Handoff Base HEAD` are engine-owned support fields for fresh handoffs. The workflow engine may populate them at startup. Add optional fields such as `Last Reviewed HEAD` or `Review Log` only when they materially help later review or inconsistency resolution.

## Critical Invariants Guidance

Use `Critical Invariants` for statements that must remain true across all checkpoints.

Examples:

- No runtime path may be hardcoded outside config.
- The same canonical input must be reused across requests for cacheability.
- A deprecated path may not remain active after migration.

Each invariant must be:

- concrete
- testable
- important enough that violating it would materially change the implementation

## Forbidden Implementations Guidance

Use `Forbidden Implementations` to name likely shortcuts explicitly.

Examples:

- Do not silently fall back to a local absolute path.
- Do not hardcode a primary checkout path such as `/Users/.../repo` or `/home/.../repo` into checkpoint bootstrap or verification commands for repository-local work.
- Do not keep both old and new config sources live.
- Do not describe future-state docs as implemented behavior before code reaches parity.

If a shortcut is plausible and harmful, name it explicitly.

## Behavioral Acceptance Tests Guidance

Behavioral acceptance tests must describe observable outcomes.

Examples:

- "Given `start_when_ready`, inference begins after grid writing completes while summary generation is still running."
- "Given the same run, every summary request reuses the exact same transcript body."

Do not rely only on unit-test commands. The plan must state what a passing implementation does.

## Plan-to-Verification Matrix Guidance

Every important requirement must map to at least one concrete verification method.

Allowed verification types:

- exact test command
- exact grep or search command
- exact file existence or symlink check
- exact metadata assertion
- exact smoke command

Do not leave major requirements without verification coverage.

## Docs Parity Rule

Do not let the plan describe documentation changes as reflecting implemented behavior unless the corresponding checkpoint explicitly brings the code to that state in the same handoff.

If docs intentionally describe future state, the plan must say so explicitly and explain why.

## Git Tracking Rule

The `Git Tracking` section is lightweight support metadata for later review workflows and inconsistency resolution.

Requirements:

- Include `Plan Branch` and `Pre-Handoff Base HEAD` in every original handoff plan, but allow both fields to start empty in a fresh plan.
- Let the workflow engine populate or refresh those two fields at startup for pristine handoffs. Do not require manual git capture in the plan steps.
- When `Pre-Handoff Base HEAD` is populated, use the full SHA, not a short SHA.
- Treat `Pre-Handoff Base HEAD` as immutable for the life of the handoff, even after squashes.
- Treat checkpoint/version commit prefixes such as `cp4 v01`, `cp4 v02`, and `cp5 v01` as the primary human-readable tracking mechanism for review progress.
- Use `Last Reviewed HEAD` and `Review Log` only as optional support metadata. Do not require them in fresh plans, and do not make the handoff process depend on updating them after every review.
- If later review workflows do use `Review Log`, entries should name the checkpoint/version batch that was reviewed, and may include exact SHAs when useful.
- If a later plan revision changes scope, preserve the original base SHA unless the user explicitly restarts the handoff from a new baseline.

## Documentation Coverage Rule

Every final aflow plan must evaluate whether the change warrants documentation updates and, when it does, assign those updates to the relevant checkpoint(s).

Required guidance:

- Update `ARCHITECTURE.md` where the implemented change affects architecture, system boundaries, data flow, component responsibilities, or integration contracts already described there.
- Update `DEVLOG.md` where the project uses it to record implementation decisions, notable behavior changes, migrations, or operational follow-ups caused by the work.
- Update `AGENTS.md` only in affected subdirectories when the change alters coding-agent instructions, local workflow constraints, generated artifacts, or directory-specific implementation rules for that subdirectory.
- Do not modify the root `AGENTS.md` as part of the implementation handoff.
- Update relevant existing user-facing documentation when the change modifies user-visible behavior, supported workflows, flags, configuration, setup, or troubleshooting guidance.
- Treat README updates as opt-in to existing coverage only: the plan must instruct implementers to search the root `README.md` and any affected subdirectory `README.md` files for an already relevant section, and update that section if it exists.
- Do not add a new README section, a new README file, or new feature mention in README solely to document the change when no relevant existing section already covers that area.
- If no relevant README section exists, the plan must leave README untouched and direct any necessary documentation updates to a more appropriate existing doc instead, if one exists.
- Documentation updates must be scoped to the behavior actually implemented in the same handoff, not speculative future state.
- When the plan concludes that a documentation file does not need changes, it should say why, so the implementer does not have to guess.

## Clarification Standard

Before finalizing the plan, gather the same requirement clarity you would for any strong implementation plan:

- Ask targeted questions about scope, constraints, dependencies, tradeoffs, and acceptance criteria.
- Confirm risky assumptions before locking the final plan.
- Prefer explicit defaults over leaving choices open.

## Plan File Persistence

For every plan generated with this skill, persist the final aflow plan to disk.

Rules:

1. Ensure `plans/in-progress/` exists in the current project's root before writing the active plan. Create it if it does not exist.
2. Save the active original handoff plan under `plans/in-progress/`. Do not leave active plans directly under `plans/`.
3. Use a descriptive markdown filename that makes the handoff purpose obvious.
4. Avoid overwriting existing files.
5. Exception: allow overwrite only when the target file was created by the assistant in the same session.
6. If a same-name file already exists from a prior session, create a new variant name such as `-v2`, `-v3`, or a date suffix.
7. Architect or reviewer workflows must delete superseded fix plans whenever a newer fix plan is created, and must delete any remaining fix plans once a checkpoint is accepted so that only the original handoff plan remains in `plans/in-progress/` before the next normal checkpoint starts.
8. When every checkpoint is complete, leave the original handoff plan in `plans/in-progress/`. The workflow engine owns the final move to `plans/done/`.
