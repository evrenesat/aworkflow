# AFLOW-MAINT-20261006 · 13/18 — Avoid reparsing unchanged event journals

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 13 of 18
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A07
- Priority: P2
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

Repeated event polling and appends by the same process stop reparsing the unchanged history, while sequence, cursor, crash and corruption behavior remain compatible.

## MVP And Scope Boundary

**Now (MVP):** Repeated event polling and appends by the same process stop reparsing the unchanged history, while sequence, cursor, crash and corruption behavior remain compatible.

**Later / Out of scope:** Database migration, a durable index/schema, SSE cadence changes, stronger cursor/backfill semantics than the current last-1000 window, and optimizing unrelated status reads.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: 01: aflow-maintainability-20261006-01-verification-baseline.md

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

- `aflow/control_plane/persistence.py`
- `aflow/control_plane/repository.py`
- `aflow/control_plane/services.py`
- `tests/test_control_plane_repository.py`
- `tests/test_control_plane_services.py`
- `apps/aflow_app/server/tests/test_control_plane_api.py`

## Decisions And Preserved Behavior

1. Use a bounded process-local cache owned by a new event_journal.py sibling, shared by short-lived EventJournal instances. Retain public EventJournal/read_events/append_run_event imports through persistence.py. No on-disk index is needed for the first useful improvement.
2. First access or any externally changed identity/size/mtime_ns/ctime_ns performs full validation with current _parse_events rules. An unchanged signature may reuse the validated sequence count and last 1000 records. A successful append under the existing file lock updates the cache from the exact validated prior signature and confirmed resulting signature; an uncertain write invalidates it.
3. Unknown external growth, same-size edits, replacement, truncation or restart triggers full replay rather than trusting the old prefix. Do not claim safe suffix-only validation when another writer might also have edited old bytes.
4. Tail remains read-only; one incomplete final line remains non-authoritative. Append preserves current torn-tail repair and fsync ordering, rejects interior corruption and allocates the next sequence while locked. Keep the current repository cursor semantics: filter the validated last-1000 window then return the requested tail.
5. Limit cache to 32 journals and 16 MiB of retained encoded event bytes, evict least recently used entries, and skip caching an oversized entry. Memory eviction changes cost only, never returned data. Guard in-process cache updates with a short lock; never hold that lock while waiting for the file lock.
6. Deterministic targets at 10 and 1000 retained events: after initial validation, ten unchanged tail(limit=1) calls parse zero journal bytes; sequential appends by this process parse zero prior-prefix bytes after each successful cached append. External changes legitimately revalidate the full prefix.

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

### [ ] Checkpoint 1: Cache fully validated unchanged tails

**Goal:** Unchanged polls avoid disk parsing without weakening integrity.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify new control_plane/event_journal.py, persistence compatibility facade and new tests/test_event_journal.py. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Move event parsing and journal ownership into cohesive functions, retaining byte/sequence/error behavior. Add file-signature-before/after checks so unstable observations are never cached.
- [ ] Implement the bounded shared LRU described above and record deterministic parsed-byte counters in tests only, using 10/1000-event fixtures.
- [ ] Test repeated new EventJournal instances, cache eviction, zero/new cursor behavior, same-size interior edit, replacement, truncation, and malformed/incomplete tail. Reads must not repair files.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
uv run pytest -q tests/test_event_journal.py tests/test_control_plane_repository.py tests/test_control_plane_services.py
```

**Expected result:** Warm unchanged polls parse zero bytes, while every detectable changed journal is fully revalidated and corruption remains visible.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Reuse validated append state under the existing file lock

**Goal:** Repeated appends avoid quadratic prefix parsing without losing concurrent-writer safety.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify event_journal.py append path, persistence facade and event/SSE tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Reuse cached state only when the locked current signature matches exactly; otherwise replay all complete records. Append/fsync and update cache from the resulting signature; invalidate on partial writes or exceptions.
- [ ] Keep torn-tail truncation/repair confined to append and sequence allocation serialized. Add independent-process writers and child crash tests, including cache warmup followed by external corruption.
- [ ] Preserve tail_events cursor/limit semantics and existing SSE/status composition. Update control-plane AGENTS.md, ARCHITECTURE.md and DEVLOG.md with cost guarantees and cold/external-change limitations.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
uv run pytest -q tests/test_event_journal.py tests/test_control_plane_repository.py tests/test_control_plane_services.py
```

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_control_plane_api.py -k 'event or sse'
```

**Expected result:** Sequences stay monotonic under multiple writers; warm same-process appends never parse prior history; other changes trigger safe full validation.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

## Final Verification

The execution agent completes this cumulative gate after the final checkpoint's code changes and before whole-plan approval. Use focused commands within earlier checkpoints. Reuse inspectable passing evidence only when source, tests, dependencies, configuration and environment fingerprints still match; do not rerun the same broad gate at every checkpoint. A future CI run does not replace this local gate.

From the execution root, run:

```bash
uv run ruff check aflow apps/aflow_app/server/src
```

```bash
uv run pytest -q tests/test_event_journal.py tests/test_control_plane_repository.py tests/test_control_plane_services.py tests/test_control_plane_reconciliation.py tests/test_run_progress.py
```

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_control_plane_api.py apps/aflow_app/server/tests/test_mcp.py
```

```bash
git diff --check
```

All commands must exit successfully with the relevant tests executed, not skipped because fixtures or browsers are missing. Existing CI OS/Python/browser/packaging gates remain required; these commands do not claim cross-platform execution locally.

Verify installed packaging once for the accepted cumulative code state:

```bash
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/13/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/13/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

**Real user/deployed surface:** Use the existing disposable SSE API fixture to receive events from a fake run while polling concurrently, then restart the reader and verify ordering/cursors. After deployment, inspect a real stream read-only and report the measured fixture improvement separately from live latency.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given a 1000-record journal is polled unchanged ten times, the observable result is: After its first validation, zero additional journal bytes are parsed.
- Given one process appends many events after warmup, the observable result is: No unchanged prefix is reparsed, and sequence numbers remain consecutive.
- Given another process replaces, truncates or edits the file, the observable result is: The cache is invalidated; interior corruption is rejected.
- Given a reader encounters one torn final line, the observable result is: It returns only complete events and leaves bytes untouched; append retains its repair contract.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| A 1000-record journal is polled unchanged ten times | Deterministic byte-count test |
| One process appends many events after warmup | Append cost/sequence tests |
| Another process replaces, truncates or edits the file | Multi-process and corruption tests |
| A reader encounters one torn final line | Torn-tail tests |

## Documentation Impact

Document cache ownership/bounds, invalidation and the preserved last-1000 cursor contract; do not advertise fully incremental cross-process validation.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/13/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

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
