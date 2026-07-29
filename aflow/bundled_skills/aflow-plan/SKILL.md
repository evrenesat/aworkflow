---
name: aflow-plan
description: "Create a strict AFlow checkpoint handoff plan for coding work that will be implemented by another model, tool, or later session. Use when the user explicitly wants the aflow pattern or a checkpoint-based handoff plan."
---

# AFlow Handoff Plan

Create a generic, self-contained implementation plan organized into durable checkpoints. Plan only; do not implement.

## Core Behavior

- Treat prompt-supplied scope and plan paths as authoritative. If the target is ambiguous, ask.
- Ask only the questions needed to resolve behavior, constraints, tradeoffs, dependencies, and acceptance criteria.
- Confirm risky assumptions and record safe defaults explicitly.
- Inspect the repository, applicable `AGENTS.md` files, existing architecture, tests, and documentation before finalizing.
- Make the plan decision complete: a fresh worker must not need prior chat or need to choose behavior, precedence, fallback, validation, ownership, or verification strategy.
- Make the plan self-sufficient; do not rely on another skill or workflow role to supply missing implementation guidance.
- Keep the generated plan execution-model agnostic and worker-facing. Do not add manager selection, retry or upgrade routing, repair-overlay policy, workflow transitions, or review protocol.
- Use repo-relative or execution-root-safe paths and commands. Never hardcode a guessed primary checkout path for repository-local work.
- Use absolute paths only for external artifacts explicitly supplied by the user or environment and not mirrored in the repository.
- Keep the original handoff plan as the durable progress ledger.
- Use `aflow` as the canonical spelling.

## Planning Workflow

1. Resolve the requested outcome, exclusions, preserved behavior, assumptions, and acceptance criteria.
2. Map affected components, interfaces, dependencies, risks, tests, generated artifacts, and documentation.
3. Decide cross-cutting invariants and forbidden shortcuts.
4. Draft checkpoints in dependency order.
5. Audit every checkpoint for semantic cohesion, size, independent verifiability, and decision completeness.
6. Re-audit the entire remaining plan after any checkpoint is split, merged, reordered, or materially revised.
7. Persist the final plan under `plans/in-progress/`.

## Checkpoint Shaping

- Size checkpoints by reviewable change surface, not feature theme, bullet count, or total checkpoint count.
- Prefer one coherent outcome spanning no more than two tightly coupled production layers. This is a heuristic, not a quota.
- Split independently deliverable persistence or migrations, domain/state logic, concurrency or security boundaries, API/contracts, UI, external-tool integration, and build/deployment work.
- Treat multiple stateful, security-sensitive, generated-contract, or external-process boundaries as complexity even when the projected diff is small.
- Use file and line estimates only as secondary scope-pressure signals. Estimate maintained production code separately from tests, documentation, generated files, and vendored output; generated volume alone must not force a split.
- Split whenever a subset can be implemented, verified, and accepted without the rest, or when one worker would need to coordinate several unrelated implementation threads.
- When uncertain, choose the smallest stable dependency seam that preserves the agreed outcome and ordering.
- After shaping, confirm every checkpoint has a narrow outcome, bounded scope, explicit dependencies, observable completion criteria, and proportionate verification.

## Required Plan Content

Include these sections in substance, adapting headings when that improves clarity:

1. `Summary`
2. `Git Tracking`
3. `Done Means`
4. `Critical Invariants`
5. `Forbidden Implementations`
6. `Checkpoints`
7. `Behavioral Acceptance Tests`
8. `Plan-to-Verification Matrix`
9. `Assumptions And Defaults`

Keep the content concise. Omit repetition, background already captured elsewhere in the plan, and instructions owned by execution or review tooling.

## Output Contract

- Use Markdown task lists (`- [ ]`) for checkpoints and meaningful internal steps.
- Define project-level and checkpoint-level completion as observable behavior.
- Make each checkpoint independently implementable and verifiable.
- State exact behavior, interfaces, defaults, precedence, validation, error handling, and preserved compatibility where relevant.
- Include context bootstrapping commands, allowed and excluded scope, dependencies, exact verification commands, and documentation impact.
- Name plausible harmful shortcuts in `Forbidden Implementations`.
- Express behavioral acceptance tests as inputs and observable outcomes, not only test commands.
- Map every important requirement to at least one concrete verification method.
- Keep progress durable: checkboxes must represent verified work rather than activity.
- Include only the minimal `Git Tracking` fields shown below; the runtime populates them.

```markdown
## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``
```

Use this checkpoint skeleton:

```markdown
### [ ] Checkpoint N: <narrow outcome>

**Goal:**

- <one coherent implementation outcome>

**Context:**

- Run: `git rev-parse --show-toplevel`
- Inspect: `<repo-relative files or commands>`
- Preserve: <existing behavior or contract>

**Scope:**

- May create or modify: <files/components>
- Must not touch: <files/systems>
- Constraints: <decisions, interfaces, anti-shortcuts>

**Steps:**

- [ ] <decision-complete implementation step>
- [ ] <decision-complete implementation step>

**Dependencies:**

- <prior checkpoint, external prerequisite, or none>

**Verification:**

- Run: `<exact scoped command>`
- Run: `<exact non-regression or integration command>`
- Observe: <expected behavior>

**Done When:**

- <observable completion condition>
- Every completed step is validated against code, tests, or observable behavior.
- Verification passes and the changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if <genuine unresolved product, safety, ownership, or environment ambiguity>.
- Stop and report if unrelated dirty files make change ownership ambiguous.
```

## Global Guidance

### Critical Invariants

Use concrete, testable rules that must hold across all checkpoints. Include only invariants whose violation would materially change the implementation.

### Forbidden Implementations

Name shortcuts that are plausible and harmful, such as retaining two competing config sources, silently falling back to a local absolute path, bypassing validation, or describing future behavior as already implemented.

### Acceptance And Verification

- State observable behavior for each major requirement.
- Prefer given/when/then or an equally explicit input/action/outcome form.
- Use exact test, smoke, search, file, metadata, or build checks.
- Include negative and failure-path coverage where behavior depends on rejection, recovery, security, or concurrency.
- Keep verification proportionate to the checkpoint while preserving meaningful non-regression coverage.

### Documentation

- Update `ARCHITECTURE.md` when structure, boundaries, responsibilities, data flow, or integration contracts change.
- Update `DEVLOG.md` when the project records notable implementation decisions, migrations, behavior changes, or operational follow-ups there.
- Update nested `AGENTS.md` only when local agent instructions or directory responsibilities change; do not modify the root `AGENTS.md`.
- Update existing user-facing documentation when setup, configuration, workflows, flags, behavior, or troubleshooting changes.
- Update an existing relevant README section when one exists; do not invent unrelated README coverage.
- Keep documentation aligned with behavior implemented in the same handoff. If a document needs no change, state why.

### Assumptions And Defaults

Record safe defaults and their basis. Ask instead of defaulting when a choice would materially change behavior, compatibility, security, or scope.

## Plan Persistence

1. Ensure `plans/in-progress/` exists in the execution repository.
2. Save the original plan there with a descriptive Markdown filename.
3. Do not overwrite a prior-session file; add a version or date suffix. A file created in the current session may be corrected in place.
4. Leave the original plan in `plans/in-progress/` after all checkpoints are complete; the workflow engine owns the final move to `plans/done/`.
