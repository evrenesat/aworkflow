# AFLOW-MAINT-20261006 · 12/18 · Group B — Separate evidence reading from progress interpretation

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 12 of 18
- Pairing group: `AFLOW-MAINT-20261006-B`
- Group position: 08 of 09 (preference among ready members)
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A06
- Priority: P2
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

The same durable evidence produces truthful summary and detail through small read/normalize/reduce/project owners, with duplicate request-local reads removed and historical compatibility preserved.

## MVP And Scope Boundary

**Now (MVP):** The same durable evidence produces truthful summary and detail through small read/normalize/reduce/project owners, with duplicate request-local reads removed and historical compatibility preserved.

**Later / Out of scope:** New database or persisted progress snapshots, changing progress meaning, removing legacy response fields, global UI redesign, broad manager-context rewrite or claiming latency wins from line counts.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: 06: aflow-maintainability-20261006-06-supervision-state-codecs.md; 13: aflow-maintainability-20261006-13-event-journal-read-cost.md

Outside prerequisite: run-history-fast-on-demand-20261005.md must be delivered with its compact overview/demand-driven detail contract. At the 6 October 2026 08:48 UTC inspection, successor 20261006t060326z-d4fe4b41 was owner_stopped and checkpoints 3–4 remained unchecked. Verify current delivery evidence before dispatch; client paging and recovery of that run remain outside this plan.

**Pairing boundary — Group B:** Requires 06's delivered codec boundary and 13's delivered event owner (which also requires 05/14). From this point Group B owns progress/evidence/context projection changes in control_plane/repository.py and persistence.py; Group A consumers use their unchanged interfaces. No changes to run-state codecs, runlog, file_io.py or the event append implementation. The external history delivery gate remains mandatory.

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

- `aflow/control_plane/run_progress.py`
- `aflow/control_plane/persistence.py`
- `aflow/control_plane/repository.py`
- `aflow/manager_context.py`
- `tests/test_run_progress.py`
- `tests/test_manager_context.py`
- `tests/test_control_plane_repository.py`
- `apps/aflow_app/server/tests/test_run_progress_browser.py`

## Decisions And Preserved Behavior

1. Create contained evidence reading, normalized evidence records and pure reduction as sibling owners in control_plane (progress_evidence.py, progress_records.py, progress_reduce.py). run_progress.py remains the public compatibility/projection facade and existing bounded cache owner.
2. Read every required evidence artifact at most once within one summary/detail request by carrying a request-local validated snapshot. Separate file identity/byte-budget/containment from semantic authority. Cache keys retain current identity and freshness checks; no new persistent cache. Sharing physical reads must retain each existing projection's logical coverage/byte-budget semantics; it cannot silently lower a consumer's budget or drop facts merely because another consumer used the snapshot first.
3. Preserve unavailable/partial/complete, unknown versus truthful zero, original plan/checkpoint identity, review approval authority, successor lineage and execution/publication/activation distinctions. Historical legacy output remains a narrow adapter from the common normalized evidence; preserve current differences where semantics are not equivalent.
4. Summary reduction computes its required totals/coverage without constructing checkpoint/attempt display arrays or manager prompt context solely for presentation. It may still need to inspect complete relevant history for truthful totals; do not promise constant-time cold summaries.
5. Use deterministic 10/1000-record fixtures and count opens/parsed bytes/object construction before and after. Targets: zero duplicate reads of the same artifact per request, zero detail-array construction on summary-only calls, and no greater bytes than baseline for the same returned facts. Record elapsed time only as supporting local evidence, never a production latency claim.

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

### [ ] Checkpoint 1: Normalize validated evidence behind existing outputs

**Goal:** Evidence safety and interpretation can be tested separately without changing public results.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify progress_evidence.py, progress_records.py, run_progress.py and focused progress tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Capture baseline summary/detail/legacy results and artifact read counts with 10/1000-record fixtures using existing representative evidence shapes; add them to tests/test_run_progress.py or a focused new test_progress_pipeline.py.
- [ ] Extract containment, budgets and parsing into the reader; normalize records with exact authority/identity reasons. Pass the immutable request snapshot to reducers rather than rediscovering files.
- [ ] Keep current public functions and cache invalidation behavior; add unsafe paths, missing/torn/malformed evidence and prior-success/current-failure cases.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
uv run pytest -q tests/test_progress_pipeline.py tests/test_run_progress.py tests/test_control_plane_repository.py tests/test_manager_context.py
```

**Expected result:** Normalized evidence is bounded/read-only and existing response semantics remain equivalent, including partial coverage.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Share semantic reduction and keep summary construction lean

**Goal:** Summary/detail/legacy consumers share authority rules without duplicate I/O or detail-only work.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify progress_reduce.py, run_progress.py, persistence.py context composition and affected tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Split the oversized detail reducer into pure cohesive operations for plan/checkpoint identity, attempts/review authority, coverage/counts and delivery; project existing wire shapes through explicit adapters.
- [ ] Build summary fields without constructing detail presentation arrays or unrelated manager context. Share request evidence between context bundle consumers while retaining legacy fields and supported fallbacks.
- [ ] Add deterministic read-byte/open/construction counters and demonstrate the stated targets, with equal returned facts. Add a source/import check that pure reduction cannot write/read files, start processes or change durable state.
- [ ] Update ARCHITECTURE.md, control-plane AGENTS.md and DEVLOG.md with evidence authority, cache invalidation and measured limits.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
uv run pytest -q tests/test_progress_pipeline.py tests/test_run_progress.py tests/test_control_plane_services.py tests/test_manager_context.py tests/test_run_history.py
```

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_control_plane_api.py apps/aflow_app/server/tests/test_mcp.py -k 'progress or context or history'
```

**Expected result:** No duplicate artifact opens per request and no summary-only detail arrays; valid zeroes and partial/unknown distinctions are unchanged.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

## Final Verification

The execution agent completes this cumulative gate after the final checkpoint's code changes and before whole-plan approval. Use focused commands within earlier checkpoints. Reuse inspectable passing evidence only when source, tests, dependencies, configuration and environment fingerprints still match; do not rerun the same broad gate at every checkpoint. A future CI run does not replace this local gate.

From the execution root, run:

```bash
uv run ruff check aflow apps/aflow_app/server/src
```

```bash
uv run pytest -q tests/test_progress_pipeline.py tests/test_run_progress.py tests/test_control_plane_repository.py tests/test_control_plane_services.py tests/test_run_history.py tests/test_run_list_identity.py tests/test_manager_context.py
```

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_control_plane_api.py apps/aflow_app/server/tests/test_mcp.py apps/aflow_app/server/tests/test_run_progress_browser.py
```

```bash
AFLOW_TEST_BROWSER=webkit uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_run_progress_browser.py
```

```bash
git diff --check
```

All commands must exit successfully with the relevant tests executed, not skipped because fixtures or browsers are missing. Existing CI OS/Python/browser/packaging gates remain required; these commands do not claim cross-platform execution locally.

Verify installed packaging once for the accepted cumulative code state:

```bash
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/12/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/12/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

**Real user/deployed surface:** On a disposable API app, request the same selected run's compact overview and opened history and compare identity, counts, coverage and delivery facts. After deployment, inspect one active and one historical run read-only; record any unavailable evidence without backfilling it.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given history is incomplete but contains a valid current failure, the observable result is: Summary/detail display the failure with partial coverage, never an old success as current truth.
- Given a valid complete run has zero reviewed checkpoints, the observable result is: Zero is shown as zero; unavailable evidence remains unknown.
- Given summary and detail are assembled from one request snapshot, the observable result is: Each artifact is read once and summary constructs no detail arrays.
- Given a projection runs over unsafe or malformed evidence, the observable result is: It remains read-only and exposes bounded partial/unavailable reasons.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| History is incomplete but contains a valid current failure | Semantic fixture matrix and browser progress path |
| A valid complete run has zero reviewed checkpoints | Progress reducer tests |
| Summary and detail are assembled from one request snapshot | Deterministic read/construction counters |
| A projection runs over unsafe or malformed evidence | Containment/immutability tests |

## Documentation Impact

Document evidence readers, normalized records, pure reducers, compatibility projection and the exact measured performance limits.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/12/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

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
