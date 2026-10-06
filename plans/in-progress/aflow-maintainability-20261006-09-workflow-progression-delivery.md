# AFLOW-MAINT-20261006 · 09/18 · Group A — Extract checkpoint progression and completion delivery

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 09 of 18
- Pairing group: `AFLOW-MAINT-20261006-A`
- Group position: 09 of 09 (preference among ready members)
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A02 (progression and completion)
- Priority: P2
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

The workflow entry point reads as a short sequence of phases, and checkpoint approval, repair and publication remain separate explicit decisions with durable receipts.

## MVP And Scope Boundary

**Now (MVP):** The workflow entry point reads as a short sequence of phases, and checkpoint approval, repair and publication remain separate explicit decisions with durable receipts.

**Later / Out of scope:** New review or retry policies, lifecycle schema changes, redesigning manager/repartition, altering plan completion definitions, or replacing the existing CI/CD/rollback path.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: 08: aflow-maintainability-20261006-08-workflow-turn-execution.md

No additional outside prerequisite is named for this member. Recheck current active work, accepted main and the pairing index before dispatch; a newly overlapping active change is a hold to reconcile, not permission to overwrite it.

**Pairing boundary — Group A:** Own workflow progression/completion and publication/lifecycle integration. Do not reopen the control-plane projection, configuration, server fixture or CI files already handed to Group B. Continue using their existing public contracts and verify integrated behavior after serialized publication.

The preferred group position is not an extra dependency. The concierge chooses one ready member from each group; never start this member while its prerequisite is merely implementing or in review. Delivery requires accepted origin/main commits, required CI and applicable live acceptance. Check the [effort pairing index](../notes/aflow-maintainability-20261006-index.md) for the current handoff and hold evidence.

Checkpoint scope lists are upper bounds narrowed by the pairing boundary above. Do not reopen files handed to the other group or add a cross-group production edit without first recording a concrete ordering handoff. Shared domain documentation still updates with each delivery; preserve both sides and serialize integration. Use an isolated execution root and one controller for the member's lineage. These prerequisites do not authorize implementing any other plan in this run.

## Context Bootstrap

Source review: c60a980796ed35c2d31f5a2a75dc5f1219789951. Conversion inspected current p100 main d2a9a5e96fe317191a30a0da2f35c10baaf7fdeb. The reviewed report's exact bytes are preserved in the linked source; subsequent accepted behavior takes precedence over historical line numbers.

From the assigned p100 execution checkout, run:

```bash
git rev-parse --show-toplevel
git status --short
git log -1 --format='%H %s'
rg --files --hidden -g AGENTS.md -g '!**/.git/**' aflow apps tests scripts docs plans
```

Read root and nearest applicable AGENTS.md before edits. Read UI_GUIDELINES.md for affected browser/client work. Inspect these owners and their directly named tests; files introduced by prerequisite plans must come from their delivered commits:

- `aflow/workflow.py`
- `aflow/publication.py`
- `aflow/plan_lifecycle.py`
- `aflow/runlog.py`
- `tests/test_runtime.py`
- `tests/test_resume_checkpoint_repair.py`
- `tests/test_resume_review_repair.py`
- `tests/test_publication.py`
- `tests/test_plan_lifecycle.py`

## Decisions And Preserved Behavior

1. Create aflow/workflow_progression.py for pure next-step/checkpoint/review decisions and aflow/workflow_completion.py for terminal reporting and existing receipt-backed delivery orchestration. State commits still use the existing controller and writers.
2. Preserve ordering of finalized turn, checkpoint/review obligation, manager decision and selected next step. A completed worker is not reviewer approval; exhausted budget is not a completed plan; a pending repair survives restart.
3. Completion continues to publish approved implementation first, then record/move the original plan and publish required bookkeeping. Existing receipt lineage determines delivery-only resume; push/merge failure cannot be reported as delivered.
4. After plans 07–09, _run_workflow_unchecked should visibly sequence startup, boundary preparation, one turn, progression and completion. Aim for an orchestration body near 120 lines; a justified exception must name the cohesive responsibility. Merely moving thousands of lines into a class or shared mutable context fails acceptance.

## Done Means

- The stated user/developer path works through the real affected boundary and its distinguishing acceptance cases pass.
- Changed functions/modules have explicit small input/result contracts and responsibilities; moving a large closure into a wrapper or shared-all-locals context is not completion.
- The complete Final Verification gate passes for the relevant source/dependency/environment state. Local verification, remote publication, CI and live acceptance are recorded separately.
- Approved implementation reaches origin/main through the existing delivery path, with exact-commit CI and applicable activation/health evidence. This authoring task only queues the plan.

## Critical Invariants

- Preserve exact run/project/plan/checkpoint/worktree identities, durable lineage and unknown ownership states. Read projections cannot become execution authority.
- Preserve supported public signatures, wire and saved-state shapes unless a decision above explicitly names the permitted internal adapter.
- Preserve existing current-source config, explicit team/budget choices, review obligations and receipt-backed publication.
- Keep state/process/file ownership with the named domain; preserve unrelated changes and active controllers.

## Forbidden Implementations

- A parallel source of state truth, generic framework, new scheduler/database or catch-all mutable context.
- Weakened validation, cast-to-Any escapes, disabled existing checks or changed tests that merely approve a regression.
- Broad cleanup, reset, force-push, shared editable-tool repointing, live owner-setting edits or restarting another run to simplify verification.
- Treating a worker exit, completed checkbox or successful unit test as proof of review, publication or deployed usability.

## Checkpoints

### [ ] Checkpoint 1: Make checkpoint and review progression independently testable

**Goal:** A finalized turn produces one explicit next obligation without mixing filesystem effects.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify workflow_progression.py, workflow.py and new tests/test_workflow_progression.py. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Extract pure transition selection, checkpoint/review status and pending-obligation decisions using plan 06's typed records. Keep effect execution in the existing controller sequence.
- [ ] Move cohesive progression tests from test_runtime.py with explicit fixtures; retain integrated scope/repartition/repair tests. Include accepted worker pending review, rejected review pending repair, budget exhaustion and finalized-turn resume.
- [ ] Verify no new state authority or duplicate review counter has appeared.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
uv run pytest -q tests/test_workflow_progression.py tests/test_resume_checkpoint_repair.py tests/test_resume_review_repair.py tests/test_resume_pending_review.py tests/test_repair_upgrades.py tests/test_budget_exit_recovery.py
```

**Expected result:** Each input leads to the same checkpoint/review/repair obligation as current behavior, including restart.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Isolate receipt-backed terminal delivery and simplify orchestration

**Goal:** Completion is readable, resumable and never confused with provider exit or local approval.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify workflow_completion.py, workflow.py, publication/plan lifecycle integration tests and docs. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Extract _finish_normal_terminal, owner-stop terminal reporting and calls into publication/lifecycle owners into small typed operations; preserve owner-stop's distinct semantics.
- [ ] Reduce the main loop to explicit phase calls and state applications, deleting now-unused closure helpers and duplicated bookkeeping. Keep effects in their established order and retain public imports/signatures.
- [ ] Add failure/restart tests for rejected push, receipt-before-plan-move interruption, bookkeeping publication and delivery-only continuation. Audit line/function boundaries of all new workflow owners and record justified exceptions.
- [ ] Update ARCHITECTURE.md and aflow/AGENTS.md with the completed phase map, and DEVLOG.md with verification and remaining exclusions.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
uv run pytest -q tests/test_workflow_progression.py tests/test_publication.py tests/test_plan_lifecycle.py tests/test_control_plane_resume.py tests/test_budget_exit_recovery.py
```

**Expected result:** Only receipt-backed published work reaches done; delivery resume completes missing phases without repeating approved worker/reviewer work.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

## Final Verification

The execution agent completes this cumulative gate after the final checkpoint's code changes and before whole-plan approval. Use focused commands within earlier checkpoints. Reuse inspectable passing evidence only when source, tests, dependencies, configuration and environment fingerprints still match; do not rerun the same broad gate at every checkpoint. A future CI run does not replace this local gate.

From the execution root, run:

```bash
uv run ruff check aflow apps/aflow_app/server/src
```

```bash
uv run pytest -q
```

```bash
uv run python -m compileall -q aflow apps/aflow_app/server/src
```

```bash
git diff --check
```

All commands must exit successfully with the relevant tests executed, not skipped because fixtures or browsers are missing. Existing CI OS/Python/browser/packaging gates remain required; these commands do not claim cross-platform execution locally.

Verify installed packaging once for the accepted cumulative code state:

```bash
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/09/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/09/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

**Real user/deployed surface:** Use a disposable bare Git remote and fake worker/reviewer to complete one checkpoint, reject/retry publication, and resume delivery-only. Observe the publication receipt and plan move end to end. After actual deployment, inspect a real completed run's separate execution/publication/activation statuses without creating production work.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given worker says complete but review/repair is pending, the observable result is: The controller retains that obligation instead of ending the plan.
- Given push is rejected after local approval, the observable result is: Plan remains undelivered and recovery resumes delivery rather than worker implementation.
- Given process dies between publication receipt and lifecycle bookkeeping, the observable result is: One lineage receipt owner completes the remaining phase without duplicate execution.
- Given a contributor traces the controller, the observable result is: Five small phase owners reveal decisions and state changes without reading a giant context object.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| Worker says complete but review/repair is pending | Progression/repair tests |
| Push is rejected after local approval | Publication and completion-only tests |
| Process dies between publication receipt and lifecycle bookkeeping | Receipt/restart integration |
| A contributor traces the controller | Source/AST inspection and ownership documentation |

## Documentation Impact

Publish the final workflow phase/dependency map and preserve operational receipt, replay and uncertainty invariants.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/09/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

For builds/browser/dependency writes, use a conservative estimate of up to 2 GiB additional allocation per active member (wheel, isolated environment and captures, not a measured result), plus the required 20 GiB free reserve and 5% free inodes. Reuse valid caches and account for concurrent members. Before heavy writes, run:

```bash
df -B1 . "${TMPDIR:-/tmp}"
df -i . "${TMPDIR:-/tmp}"
ssh -F ~/.ssh/storage-headroom.conf storage-backing
```

Check actual output mounts and shared backing headroom; stop heavy allocation at 80% backing metadata use. If the estimate is insufficient, record a revised evidence-backed requirement before allocating. Remove only this member's disposable scratch after extracting proof; preserve failed/unique artifacts and lineage.

## Assumptions And Defaults

- The task is a behavior-preserving maintainability effort except for explicitly named correctness/quality/performance repairs.
- Python standard-library dataclasses and the existing React/FastAPI stack remain the defaults; only explicitly specified development tools may be added.
- Upstream/other-plan fixes already delivered at execution time are prerequisites to preserve, not changes to replay or undo.
- The original review is evidence and scope input, not authority to implement excluded ideas or operate production.
- This plan is self-contained for its member scope; the index supplies effort coordination, not missing behavior decisions.
