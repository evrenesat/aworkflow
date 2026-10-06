# AFLOW-MAINT-20261006 · 02/18 · Group B — Make configuration pair saves recover after process death

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 02 of 18
- Pairing group: `AFLOW-MAINT-20261006-B`
- Group position: 01 of 09 (preference among ready members)
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A01
- Priority: P1
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

After an interrupted settings save, every cooperating AFlow reader sees one complete old or new TOML pair, so launches and live reloads never consume a mixed configuration.

## MVP And Scope Boundary

**Now (MVP):** After an interrupted settings save, every cooperating AFlow reader sees one complete old or new TOML pair, so launches and live reloads never consume a mixed configuration.

**Later / Out of scope:** Configuration cleanup, generation directories, symlink-based publication, new databases, changes to editable TOML paths, or pretending two independently edited files are an OS-level atomic object.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: None.

No additional outside prerequisite is named for this member. Recheck current active work, accepted main and the pairing index before dispatch; a newly overlapping active change is a hold to reconcile, not permission to overwrite it.

**Pairing boundary — Group B:** Opening member of Group B; compatible with 01. Keep configuration code and new pair-recovery tests here. Put added REST/MCP parity cases in apps/aflow_app/server/tests/test_config_pair_parity.py; do not concurrently rewrite the broad test_control_plane_api.py/test_mcp.py fixture helpers owned by 01. The configuration transaction stays independent of 14's byte primitives.

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

- `aflow/run_config_snapshot.py`
- `aflow/live_config.py`
- `aflow/config.py`
- `apps/aflow_app/server/src/aflow_app_server/global_config_service.py`
- `apps/aflow_app/server/src/aflow_app_server/project_config_service.py`
- `tests/test_run_config_snapshot.py`
- `tests/test_live_config.py`
- `tests/test_config.py`
- `apps/aflow_app/server/tests/test_project_config_service.py`
- `apps/aflow_app/server/tests/test_global_config_patch.py`

## Decisions And Preserved Behavior

1. Use one core transaction owner, aflow/config_pair.py, beside the current pair-lock responsibility. Keep the public aflow.toml and workflows.toml paths and exact text/comment preservation. One bounded version-1 write-ahead record in the same directory stores each old/new payload (or missing-old marker), content digests and prepared/committed state; it contains no arbitrary target paths and is mode 0600. Name the record .aflow-config-pair.transaction.json. Its exact top-level fields are schema_version=1, phase (prepared or committed), and documents keyed only by the two canonical filenames; each document has old (null for formerly missing, otherwise {base64, sha256}) and new ({base64, sha256}). Enforce existing per-document byte limits after decoding and a record limit of 6 * MAX_CONFIG_DOCUMENT_BYTES + 16 KiB; reject unknown schema versions, fields and invalid digests.
2. Under the existing pair lock: recover first; validate candidate and expected revision; durably write prepared record; fsync staged files; replace both documents and fsync the directory; atomically mark the record committed and fsync; then remove the record and fsync. Before the committed marker, recovery restores both old documents; after it, recovery installs/verifies both new documents. Repeating recovery is idempotent. Before the durable committed marker, an ordinary write error invokes old-pair recovery while holding the lock. Once that marker is durable, cleanup failure leaves the committed record for a later read and emits a bounded cleanup diagnostic; it must not report that the acknowledged generation was rolled back.
3. Before restoring, each current document must be missing or match its recorded old/new digest. A third-party edit, malformed record, unsafe link or failed recovery blocks the read/save with a bounded explicit error and preserves the record and file bytes; do not overwrite unknown edits.
4. Both blocking and nonblocking live reads, global service reads/saves, snapshot capture and supported filesystem config loading use this owner before parsing. Keep one lock acquisition per operation; an internal already-locked reader prevents recursive flock deadlocks. Read-only directories with no pending transaction retain current read behavior; a pending transaction that cannot be recovered fails closed.
5. Missing workflows.toml remains a supported pre-save state. Rejected candidate or revision conflict does not create a transaction or alter either document. Do not log candidate TOML or journal payloads.

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

### [ ] Checkpoint 1: Recover one interrupted pair through the core reader

**Goal:** The pair owner can deterministically restore a valid generation across each crash boundary.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify New aflow/config_pair.py; lock re-exports in run_config_snapshot.py; config/live readers; new tests/test_config_pair.py. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Move the existing pair-lock implementation behind the core owner while retaining old imports as compatibility re-exports. Implement bounded record parsing, exact document-name allowlisting, safe reads and the prepared/committed recovery rules above. Implement the core commit operation as well as recovery so the subprocess tests exercise real staged writes before the server caller is migrated in checkpoint 2.
- [ ] Add a real child-process crash harness using disposable directories and os._exit at record publication, each document replacement, directory sync, commit-marker publication and cleanup. Restart a fresh reader for every case and during recovery itself.
- [ ] Route the smallest live reader through recovery first. Add unknown-edit, missing-file, invalid-record, readonly-with-pending-record and concurrent lock tests; establish no mixed pair is returned or interpreted.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
uv run pytest -q tests/test_config_pair.py tests/test_live_config.py tests/test_run_config_snapshot.py
```

**Expected result:** Every pre-commit interruption restores old bytes; every post-commit interruption recovers new bytes; ambiguous external edits are preserved and rejected.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Use the transaction for all supported settings and execution reads

**Goal:** REST/MCP saves and normal launches share the same crash-safe owner.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify global_config_service.py, project_config_service compatibility helpers, config.py, live_config.py, run_config_snapshot.py, directly affected configuration tests and new server tests/test_config_pair_parity.py. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Replace _commit_pair_locked with a call into the transaction owner while preserving revision validation, ordered PATCH actions, audit outcomes and ordinary OSError rollback behavior.
- [ ] Trace load_workflow_config callers with rg and move all supported pair reads under the same recovery boundary, without nesting the same flock. Keep current-source selection and nonblocking control-loop behavior.
- [ ] Add REST/MCP save/read parity coverage in the new tests/test_config_pair_parity.py server module and a fresh-process live-config read after a killed save; keep broad REST/MCP fixture helpers owned by 01 unchanged. Update docs/configuration.md, ARCHITECTURE.md and DEVLOG.md with recovery and manual-edit conflict behavior.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
uv run pytest -q tests/test_config.py tests/test_live_config.py tests/test_run_config_snapshot.py
```

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_project_config_service.py apps/aflow_app/server/tests/test_global_config_patch.py apps/aflow_app/server/tests/test_mcp.py apps/aflow_app/server/tests/test_config_pair_parity.py -k 'config'
```

**Expected result:** A renamed prompt and its workflow reference recover together; stale revisions and invalid saves preserve exact bytes; REST/MCP still return the same public errors.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

## Final Verification

The execution agent completes this cumulative gate after the final checkpoint's code changes and before whole-plan approval. Use focused commands within earlier checkpoints. Reuse inspectable passing evidence only when source, tests, dependencies, configuration and environment fingerprints still match; do not rerun the same broad gate at every checkpoint. A future CI run does not replace this local gate.

From the execution root, run:

```bash
uv run ruff check aflow apps/aflow_app/server/src
```

```bash
uv run pytest -q tests/test_config_pair.py tests/test_config.py tests/test_live_config.py tests/test_live_config_runtime.py tests/test_run_config_snapshot.py tests/test_runtime.py
```

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_project_config_service.py apps/aflow_app/server/tests/test_global_config_patch.py apps/aflow_app/server/tests/test_guided_config.py apps/aflow_app/server/tests/test_mcp.py apps/aflow_app/server/tests/test_config_pair_parity.py
```

```bash
git diff --check
```

All commands must exit successfully with the relevant tests executed, not skipped because fixtures or browsers are missing. Existing CI OS/Python/browser/packaging gates remain required; these commands do not claim cross-platform execution locally.

Verify installed packaging once for the accepted cumulative code state:

```bash
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/02/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/02/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

**Real user/deployed surface:** Use a disposable server/config directory for the killed-save HTTP-to-live-loader path; never kill or corrupt the shared production settings service. After normal CI-gated deployment, read the real global settings and readiness through the authenticated UI and verify normal operation without changing owner settings.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given saver dies after first replacement but before commit marker, the observable result is: Fresh launch/read restores and validates the complete old pair.
- Given saver dies after committed record is durable, the observable result is: Fresh read completes/verifies the entire new pair, then removes recovery record.
- Given operator changes a document to unrelated bytes while recovery is pending, the observable result is: Read/save fails explicitly, preserving edits and transaction evidence.
- Given a settings revision is stale or TOML invalid, the observable result is: Neither original document nor transaction state changes.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| Saver dies after first replacement but before commit marker | Checkpoint 1 subprocess crash matrix |
| Saver dies after committed record is durable | Checkpoint 1 crash/recovery matrix |
| Operator changes a document to unrelated bytes while recovery is pending | Conflict and readonly recovery tests |
| A settings revision is stale or TOML invalid | REST/MCP revision and validation tests |

## Documentation Impact

Document the transaction owner, commit point, bounded errors and recovery/manual-edit policy. Configuration behavior cleanup remains plan 03.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/02/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

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
