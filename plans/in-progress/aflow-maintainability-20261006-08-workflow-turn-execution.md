# AFLOW-MAINT-20261006 · 08/18 — Extract one workflow turn with explicit process and lease ownership

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 08 of 18
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A02 (one-turn execution)
- Priority: P2
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

One workflow turn has a readable input/result contract and independently testable lifecycle, while logs, retry behavior and exclusive-resource ownership remain intact.

## MVP And Scope Boundary

**Now (MVP):** One workflow turn has a readable input/result contract and independently testable lifecycle, while logs, retry behavior and exclusive-resource ownership remain intact.

**Later / Out of scope:** Changing retry/fallback decisions, manager policy, lease admission algorithms, provider protocol semantics, checkpoint progression or delivery.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: 07: aflow-maintainability-20261006-07-workflow-startup-boundaries.md; 17: aflow-maintainability-20261006-17-provider-discovery-boundaries.md

No outside implementation prerequisite was identified at authoring; recheck current active work and accepted main before dispatch.

These are launch prerequisites, not instructions to implement predecessor plans in this run. Ordinal filenames do not imply serial execution. Shared-file overlap alone is not a dependency: use isolated execution roots, preserve both accepted changes and serialize integration. Do not launch this member from an obsolete main or start another controller for its existing lineage.

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
- `aflow/runlog.py`
- `aflow/harnesses/session.py`
- `aflow/execution_resources.py`
- `tests/test_runtime.py`
- `tests/test_harness_sessions.py`
- `tests/test_execution_resource_processes.py`
- `tests/test_durable_provider_recovery_runtime.py`

## Decisions And Preserved Behavior

1. Add aflow/workflow_turn.py for one turn's preparation, start, provider invocation and finalization, with a typed TurnInput and TurnOutcome using existing record/domain types. Keep retry/harness-recovery policy in its existing owner; extract policy separately only to reduce an existing oversized function without changing decisions.
2. The existing durable start artifact precedes provider launch. Finalization records exactly one result and preserves bounded separate stdout/stderr, unexpected exception evidence, and event order. The coordinator applies explicit outcome/state changes.
3. Lease ownership follows the existing launch-intent/bound-child/reaped-or-unconfirmed contract. Cancellation/timeout or unknown cessation never proves resource release. Use plan 17's adapter-owned discovery, without provider-name branches in the controller.
4. Do not hide all loop locals in TurnInput. Inputs cover one immutable selected step/config/prompt and explicit process/log interfaces; outcomes cover the completed turn and next policy inputs, not arbitrary controller mutation.

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

### [ ] Checkpoint 1: Make start and finalization a bounded lifecycle

**Goal:** A started turn always has one faithful finalization or explicit retained failure evidence.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify workflow_turn.py, workflow.py, runlog integration and new tests/test_workflow_turn.py. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Extract _start_turn, _finalize_turn_record, bounded unexpected-failure handling and their direct artifact/event operations into cohesive functions.
- [ ] Define typed request/outcome records and move focused tests out of broad runtime fixtures; retain real temporary-artifact integration.
- [ ] Test failure before launch, exception after durable start, normal completion and cancellation. Verify started/finished event identities and exactly-once finalization semantics.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
uv run pytest -q tests/test_workflow_turn.py tests/test_runlog.py tests/test_worker_diagnostics.py
```

**Expected result:** Every started turn has truthful identity and failure/success artifacts, with no duplicate completion or lost stderr.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Route provider execution and resource lifetime through the turn owner

**Goal:** The main loop consumes one turn result without owning protocol mechanics.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify workflow_turn.py, workflow.py, session integration and relevant runtime tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Move the selected session/subprocess invocation path behind the new operation using existing adapter contracts. Preserve preflight, durable recovery, hotplug handover, retry inputs, and late owner-stop behavior.
- [ ] Keep lease acquisition with admission and final ownership transfer explicit: bind exact child before prompting; release only after confirmed reaping; preserve unknown claims on failure.
- [ ] Add integrated fake-process regressions for stream draining, startup failure, timeout/cancel, process birth mismatch and unconfirmed cessation. Update ownership docs and DEVLOG.md.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
uv run pytest -q tests/test_workflow_turn.py tests/test_harness_sessions.py tests/test_execution_resource_processes.py tests/test_exclusive_execution.py tests/test_durable_provider_recovery_runtime.py tests/test_hotplug.py
```

**Expected result:** Turn output and retry inputs match baseline; no competing exclusive child starts while an earlier child may remain alive.

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
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/08/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/08/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

**Real user/deployed surface:** Run a disposable fake-provider turn through run_workflow and the normal control-plane read path; inspect start/final receipts and separate stream tails. Existing fake-process tests prove failure/reaping behavior; no paid provider run is required for this behavior-preserving extraction.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given provider crashes after the turn start was persisted, the observable result is: The final failure includes bounded stream evidence and retains the same turn identity.
- Given cancellation cannot prove the child ceased, the observable result is: The lease remains claimed and status explains the unresolved ownership.
- Given a turn is successful or safely retryable, the observable result is: The coordinator receives the same typed outcome and transition inputs as before.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| Provider crashes after the turn start was persisted | Turn/worker diagnostics integration |
| Cancellation cannot prove the child ceased | Exclusive execution/process tests |
| A turn is successful or safely retryable | Runtime/retry/hotplug regression suites |

## Documentation Impact

Document the one-turn contract, process/lease responsibility and ordering of durable start, invocation and finalization.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/08/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

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
