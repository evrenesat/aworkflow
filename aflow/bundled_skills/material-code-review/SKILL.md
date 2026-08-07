---
name: material-code-review
description: Review a pull request, branch, commit, or diff for high-confidence material defects. Use for code-review requests where findings should be limited to concrete, reachable, actionable regressions introduced or worsened by the change, while suppressing speculative edge cases, style preferences, unrelated existing issues, optional hardening, and disproportionate redesign.
---

# Material Code Review

Perform a bounded review of the requested change. Optimize for useful signal, low false-positive rate, and proportionate fixes.

## Review workflow

1. Identify the requested base and changed revision.
2. Read the diff and enough surrounding code, tests, interfaces, and configuration to understand the changed behavior.
3. Infer the intended behavior from the task, repository contracts, tests, and established patterns. Do not invent requirements.
4. Generate candidate findings.
5. Apply the finding admission gate to every candidate.
6. Verify each surviving finding once for reachability, evidence, impact, and exact location.
7. Return the surviving findings and stop.

Keep review separate from implementation. Do not edit code, redesign adjacent systems, broaden the audit, or add infrastructure unless the user explicitly requests it.

For an ordinary review, do not spawn subagents. Use at most one subagent when the change contains genuinely independent technical domains and separate analysis has clear value.

## Finding admission gate

Report a finding only when every answer below is yes:

- Was the issue introduced or materially worsened by this change?
- Is there a concrete, reachable failure scenario?
- Is the scenario within documented, tested, or realistically supported usage?
- Is the claim supported by code, contracts, tests, configuration, logs, or another concrete artifact?
- Is the impact material enough that the author would probably change the patch before merging?
- Can the concern be stated precisely without depending on hypothetical future requirements?
- Is the proposed response proportionate to the demonstrated problem?

Reject the candidate when any answer is no.

Treat theoretical possibility alone as insufficient evidence. Require a plausible path from supported input or system state to the claimed failure.

## Priorities

Prioritize, in this order:

1. Incorrect behavior and regressions
2. Security or authorization failures
3. Crashes, corruption, data loss, and resource leaks
4. Concurrency, lifetime, ownership, and ordering errors
5. Broken public, persistence, protocol, or integration contracts
6. Material performance regressions on realistic workloads
7. Maintainability regressions with a concrete defect or change-risk mechanism

## Exclusions

Omit:

- formatting, naming, and stylistic preferences
- generalized robustness suggestions without a demonstrated failure
- speculative edge cases outside supported usage
- hypothetical future requirements
- unrelated pre-existing defects
- optional hardening presented as a correctness requirement
- requests for tests without a specific failure the test would detect
- architectural preferences with no demonstrated practical benefit
- broad refactors whose cost is disproportionate to the issue
- duplicate findings that share the same root cause

Treat complexity as a finding only when the change creates a concrete correctness, comprehension, coupling, or maintenance risk.

## Fix discipline

Recommend the smallest coherent correction that addresses the demonstrated defect and follows existing project patterns.

Prefer deletion, simplification, consolidation, or reuse when they solve the problem cleanly. Avoid new abstractions, extension points, dependencies, configuration, fallback paths, or infrastructure for possible future needs.

Do not turn a local defect into a repository-wide cleanup.

## Output

Return zero to five findings, ordered by severity and confidence. Never create findings to reach the maximum.

For each finding include:

- severity
- confidence
- file and exact line range
- concrete failure scenario
- impact
- evidence
- smallest proportionate fix

Keep each finding focused on one root cause.

After the findings, return exactly one verdict:

- `No material findings`
- `Material fixes required`
- `Material questions require human verification`

Zero findings is a successful review outcome. Stop after the verdict.
