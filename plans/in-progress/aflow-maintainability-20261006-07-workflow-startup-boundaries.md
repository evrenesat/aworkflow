# AFLOW-MAINT-20261006 · 07/18 — Extract workflow startup and safe-boundary preparation

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 07 of 18
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A02 (startup and boundary preparation)
- Priority: P2
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

A contributor can follow startup and safe-boundary preparation in small owners while the controller retains exactly one authority for run state, admission and live configuration.

## MVP And Scope Boundary

**Now (MVP):** A contributor can follow startup and safe-boundary preparation in small owners while the controller retains exactly one authority for run state, admission and live configuration.

**Later / Out of scope:** Turn execution, checkpoint progression and terminal delivery (plans 08–09), manager algorithms, new lifecycle states, process supervision redesign and resource policy changes.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: 02: aflow-maintainability-20261006-02-configuration-crash-recovery.md; 06: aflow-maintainability-20261006-06-supervision-state-codecs.md

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
- `aflow/live_config.py`
- `aflow/project_admission.py`
- `aflow/run_state.py`
- `tests/test_runtime.py`
- `tests/test_live_config_runtime.py`
- `tests/test_startup_plan_relevance.py`
- `tests/test_dirty_worktree_preflight.py`

## Decisions And Preserved Behavior

1. Use aflow/workflow_startup.py for preparation before the first executable turn and aflow/workflow_boundary.py for current-source reload and accepted boundary overrides. Keep public run_workflow signatures stable; do not move the whole controller into either file.
2. Preparation returns explicit small records containing execution paths, selected config/step and admission handoff outcome; domain state remains ControllerState and the existing durable writers. Helpers receive only the authority they use, not an omnibus context or dozens of callbacks.
3. Preserve current ordering: validate plan and prior-work evidence before creating a fresh worktree; validate current selected config/step before provider work; honor owner stop and safe-boundary overrides according to existing tests; never replace an unknown unit/admission owner.
4. Use plan 02's recovered config reader and plan 06's validated records. Keep lifecycle preparation, nonblocking admission/control servicing and pure step/override decisions visibly separate.

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

### [ ] Checkpoint 1: Extract startup preparation as one explicit operation

**Goal:** Fresh and resumed startup use small cohesive code without changing lifecycle or admission order.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify workflow.py, new workflow_startup.py and new tests/test_workflow_startup.py. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Characterize startup decisions with existing fresh/resume/dirty/prior-work tests, including failing before worktree setup. Move lifecycle path selection and preparation into bounded functions returning typed results.
- [ ] Move effects (Git/worktree preparation, metadata writes and admission handoff) into a readable sequence; keep pure validation separate and retain the same error types and rollback boundaries.
- [ ] Move cohesive startup tests from test_runtime.py with explicit imports/fixtures, retain integrated cases and add an import-direction check preventing startup from depending on CLI or importing workflow to reach private helpers.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
uv run pytest -q tests/test_workflow_startup.py tests/test_startup_plan_relevance.py tests/test_dirty_worktree_preflight.py tests/test_resume_service.py
```

**Expected result:** Invalid prior-work evidence cannot create a new execution root; accepted startup uses exactly the same plan and admission identity.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Extract live configuration and override decisions at boundaries

**Goal:** The controller applies a single explicit boundary result while continuing to service stop/control requests.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify workflow_boundary.py, workflow.py and live-config/override runtime tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Extract _reload_live_configuration_at_boundary and the pure decision parts of _apply_boundary_override. Return explicit accepted/rejected/deferred outcomes plus their state changes; keep provider handover with the existing owner.
- [ ] Preserve the existing no-lock/no-read behavior during nonblocking config contention and owner-stop precedence. Carry team, max-turns, notes and role-selector explicitness without introducing another truth store.
- [ ] Add narrow tests of boundary decisions plus integrated control-wait cases; document owners and ordering in ARCHITECTURE.md, aflow/AGENTS.md and DEVLOG.md.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
uv run pytest -q tests/test_workflow_startup.py tests/test_live_config_runtime.py tests/test_live_config.py tests/test_hotplug.py tests/test_execution_resources.py
```

**Expected result:** Live edits take effect only at supported boundaries; a busy config lock cannot stop owner-stop servicing; rejected overrides preserve prior accepted state.

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
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/07/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/07/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

**Real user/deployed surface:** Exercise fresh startup and an interrupted-run continuation in disposable repositories using the existing fake harnesses; observe selected paths and one admission owner in durable metadata. Production acceptance is read-only status and current-source readiness after normal deployment.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given fresh start discovers unreconciled prior work, the observable result is: It rejects before new worktree/admission side effects.
- Given the config pair is locked while owner stop arrives, the observable result is: The controller continues servicing control and stops at its defined boundary.
- Given a valid override changes team/budget at a safe boundary, the observable result is: Exactly the accepted changes are persisted; rejected input does not replace them.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| Fresh start discovers unreconciled prior work | Startup/prior-work integration tests |
| The config pair is locked while owner stop arrives | Nonblocking live-config runtime tests |
| A valid override changes team/budget at a safe boundary | Override/hotplug/runtime tests |

## Documentation Impact

Explain startup and boundary owners and state mutation order; do not describe the remaining controller phases as already extracted.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/07/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

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
