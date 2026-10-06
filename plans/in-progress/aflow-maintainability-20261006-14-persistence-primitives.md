# AFLOW-MAINT-20261006 · 14/18 · Group A — Consolidate small persistence primitives without moving domain locks

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 14 of 18
- Pairing group: `AFLOW-MAINT-20261006-A`
- Group position: 02 of 09 (preference among ready members)
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A08
- Priority: P2
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

Run artifacts, publication, admission, backups and resource journals use a small audited set of file operations while each domain retains its own validation, locks and transaction ordering.

## MVP And Scope Boundary

**Now (MVP):** Run artifacts, publication, admission, backups and resource journals use a small audited set of file operations while each domain retains its own validation, locks and transaction ordering.

**Later / Out of scope:** A universal persistence framework, moving domain locks, changing schemas/permissions, replacing the config pair transaction, making read APIs repair state, and migrating every file writer in the repository.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: 01: aflow-maintainability-20261006-01-verification-baseline.md

Outside prerequisite: exclusive-stop-and-dead-owner-reclamation-20261006.md must be delivered before migrating execution_resources.py. Run 20261006t083546z-50d6f9e9 was waiting for the exclusive worker at the 6 October 2026 08:48 UTC inspection. Preserve that repair's broker/owner-stop behavior and recheck delivery before dispatch.

**Pairing boundary — Group A:** Complete all six byte-primitive caller migrations before 05 changes the runlog writer and before Group B member 13 changes control-plane persistence. Keep config pair transaction/locking code out of scope and leave event append ownership to 13. This member is held until the external exclusive stop/dead-owner repair is delivered; 04 remains an independent ready Group A choice after 01.

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

- `aflow/runlog.py`
- `aflow/control_plane/persistence.py`
- `aflow/plan_backups.py`
- `aflow/publication.py`
- `aflow/project_admission.py`
- `aflow/execution_resources.py`
- `tests/test_runlog.py`
- `tests/test_plan_backups.py`
- `tests/test_publication.py`
- `tests/test_project_admission.py`
- `tests/test_execution_resources.py`

## Decisions And Preserved Behavior

1. Add aflow/file_io.py with explicit operations for exclusive regular-file creation, durable atomic replacement, directory synchronization and bounded regular-file reads. Use operation-specific signatures with explicit mode where needed; do not hide different guarantees behind boolean policy switches.
2. Before migrating a caller, record its creation/existence semantics, permissions, symlink/hardlink/containment rules, fsync order and exception mapping. Preserve those externally relevant contracts in its domain adapter; retain stricter existing checks.
3. Write owned temporary files in the target directory, fsync file bytes, atomically publish, then fsync the directory when durability is required. Exclusive creation never overwrites. Cleanup removes only the operation's exact uncommitted temporary file.
4. Domain owners keep locks, schema validation, compare-and-swap revision checks and transaction ordering. Single-file helpers do not make two-file saves atomic; plan 02's transaction remains its own authority.
5. Directory fsync portability keeps current explicitly supported platform behavior; never silently swallow a newly material I/O failure to make a helper universal.

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

### [ ] Checkpoint 1: Audit and extract primitives through run artifacts

**Goal:** One real caller uses small explicit file operations with equivalent failure behavior.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify new file_io.py, runlog.py, new tests/test_file_io.py and runlog tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Build a compact caller-contract table in ARCHITECTURE.md's linked persistence note; name real differences before choosing helper calls.
- [ ] Implement the four explicit operations and migrate runlog's matching paths. Add fault injection before/after file fsync, publish and directory sync plus mode/link/oversize/exclusive-exists cases.
- [ ] Retain domain validation and error mapping at the runlog boundary; no global cleanup or format changes.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
uv run pytest -q tests/test_file_io.py tests/test_runlog.py
```

**Expected result:** A rejected or interrupted operation has the same documented prior/new-file outcome, preserves permissions and never overwrites exclusive evidence.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Migrate publication and admission one caller at a time

**Goal:** Delivery and reservation keep their durable order and exact ownership checks.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify publication.py, project_admission.py and their focused suites. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Replace only equivalent low-level implementations with the audited primitives; retain publication receipt and admission lock ownership.
- [ ] Run each caller's failure/containment tests after its migration; add tests for any previously undocumented guarantee discovered in the contract audit.
- [ ] Verify failure does not report delivery or release an unknown claim, and no public error category changes.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
uv run pytest -q tests/test_file_io.py tests/test_publication.py tests/test_project_admission.py tests/test_plan_lifecycle.py
```

**Expected result:** Receipts and claims remain crash-safe, confined and authoritative under the same domain rules.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 3: Migrate backups, control-plane writes and resource journals

**Goal:** Remaining selected copies share mechanics without losing their distinct contracts.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify plan_backups.py, control_plane/persistence.py, execution_resources.py and matching tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Migrate selected matching byte writes/reads one caller at a time; preserve backup provenance, control-plane revisions and resource lock/lease ordering.
- [ ] Leave event append behavior with the current journal owner for the later plan 13 handoff; retain its public wrappers and do not fold append-only journals into atomic replacement primitives.
- [ ] Delete only now-unused duplicate helpers, keep necessary compatibility wrappers, and update nearest guidance and DEVLOG.md.

**Dependencies:** Checkpoint 2 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
uv run pytest -q tests/test_file_io.py tests/test_plan_backups.py tests/test_control_plane_services.py tests/test_control_plane_repository.py tests/test_execution_resources.py tests/test_execution_resource_processes.py
```

**Expected result:** Each migrated domain passes its fault/containment checks and still owns its locks and schema.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

## Final Verification

The execution agent completes this cumulative gate after the final checkpoint's code changes and before whole-plan approval. Use focused commands within earlier checkpoints. Reuse inspectable passing evidence only when source, tests, dependencies, configuration and environment fingerprints still match; do not rerun the same broad gate at every checkpoint. A future CI run does not replace this local gate.

From the execution root, run:

```bash
uv run ruff check aflow apps/aflow_app/server/src
```

```bash
uv run pytest -q tests/test_file_io.py tests/test_runlog.py tests/test_plan_backups.py tests/test_publication.py tests/test_plan_lifecycle.py tests/test_project_admission.py tests/test_control_plane_repository.py tests/test_control_plane_services.py tests/test_execution_resources.py tests/test_execution_resource_processes.py tests/test_runtime.py
```

```bash
git diff --check
```

All commands must exit successfully with the relevant tests executed, not skipped because fixtures or browsers are missing. Existing CI OS/Python/browser/packaging gates remain required; these commands do not claim cross-platform execution locally.

Verify installed packaging once for the accepted cumulative code state:

```bash
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/14/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/14/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

**Real user/deployed surface:** Run a disposable controller-to-publication path with a local bare remote and an exclusive-resource fake child; inspect receipts, backups and lease state through existing readers. Production verification is normal health and read-only artifact/status checks.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given exclusive receipt creation targets an existing file, the observable result is: It rejects without changing bytes.
- Given a replace or fsync fails, the observable result is: Caller preserves its documented atomic/durable boundary and returns the same error class.
- Given path escapes or unsafe links reach a guarded caller, the observable result is: Existing containment/link rejection remains in force.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| Exclusive receipt creation targets an existing file | File-I/O/publication fault tests |
| A replace or fsync fails | Per-caller injected failures |
| Path escapes or unsafe links reach a guarded caller | Per-caller containment suites |

## Documentation Impact

Keep a short contract/owner map for selected callers; distinguish single-file durability from plan 02's pair transaction.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/14/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

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
