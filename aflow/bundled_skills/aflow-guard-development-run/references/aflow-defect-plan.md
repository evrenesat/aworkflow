# AFlow Guardian Defect Plan

Use this template only after evidence identifies a new AFlow defect or edge
case. Create one plan per incident fingerprint and update it when later evidence
belongs to the same incident.

```markdown
# AFlow guard incident: <short title>

Date: <ISO timestamp>
Incident fingerprint: <fingerprint>
Guarded repository: <absolute path>
AFlow source repository: <absolute path>
Run: <run id>
Continuation lineage: <source and continuation run ids or none>

## Objective

State the AFlow behavior that must become resilient. Describe the observable
outcome, not merely the desired code edit.

## Impact and recovery status

- Guarded checkpoint and workflow step:
- Controller/process state:
- Preserved worktree and plan state:
- Operational workaround attempted:
- Scheduler state after this incident:

## Evidence

List bounded, exact references:

- `run.json` fields:
- newest turn artifacts:
- newest manager artifacts:
- process and Screen evidence:
- focused log tail:
- relevant source locations:

Do not paste complete transcripts or repeat unchanged evidence.

## Classification

Choose the narrowest category:

- controller/lifecycle
- resume/durable state
- active-plan transition
- manager protocol/routing
- review or upgrade scope
- repartition transaction
- terminal reporting
- harness adapter/recovery
- other AFlow edge case

Explain why this is an AFlow defect rather than a guarded-project
implementation failure or a semantic owner decision.

## Minimal reproduction

Give deterministic setup, exact command, triggering state, and expected versus
actual behavior. Prefer a fixture-based reproduction over requiring the
original large project.

## Root-cause hypothesis

Identify the suspected state transition or invariant violation. Separate facts
from inference and name what evidence would falsify the hypothesis.

## Proposed changes

List exact source, test, and documentation files to inspect or change. State
the responsibility of each file and preserve legacy/resume compatibility.

## Implementation steps

Provide a sequential implementation path that does not require another model to
make design decisions. Include persistence-before-routing, atomicity, or
worktree-safety requirements when applicable.

## Verification

Start with exact focused commands and expected observable results. For
manager-supervision changes, include the affected manager/context/runtime
tests, CLI/API analysis when relevant, app-server tests in their project
environment, `python3 -m compileall -q aflow`, and `git diff --check`.

Report focused passes separately from broader baseline failures. Identify the
comparison commit or branch for any claimed baseline.

## Recovery and rollout

Describe reinstall/resume steps, how to prevent duplicate controllers, how to
verify the continuation's run identity, and how to roll back without losing
accepted or dirty work.

## Non-goals and owner boundary

State what the repair must not change. Ask for owner input only if meaning,
requirements, destructive handling, or ownership is genuinely ambiguous.
```
