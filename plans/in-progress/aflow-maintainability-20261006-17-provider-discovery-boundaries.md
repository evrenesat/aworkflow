# AFLOW-MAINT-20261006 · 17/18 — Move provider discovery and explicit session capabilities into adapters

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 17 of 18
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A12
- Priority: P2
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

Adding or adjusting a provider's session support no longer requires provider-name branches in the workflow coordinator, while process and permission behavior stays provider-owned.

## MVP And Scope Boundary

**Now (MVP):** Adding or adjusting a provider's session support no longer requires provider-name branches in the workflow coordinator, while process and permission behavior stays provider-owned.

**Later / Out of scope:** New providers, protocol or permission policy changes, replacing all ACP implementations with one engine, enabling session support for unproven adapters, or paid/live-provider behavior changes.

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

- `aflow/workflow.py`
- `aflow/harnesses/base.py`
- `aflow/harnesses/session.py`
- `aflow/harnesses/codex.py`
- `aflow/harnesses/reasonix.py`
- `aflow/harnesses/dsh.py`
- `aflow/harnesses/strands.py`
- `tests/test_harnesses.py`
- `tests/test_harness_sessions.py`
- `tests/test_dsh.py`
- `tests/test_strands.py`

## Decisions And Preserved Behavior

1. Add an adapter-owned discovery method discover_session_driver(*, repo_root) returning SessionDriver or None. Copy each existing provider's discovery behavior into its adapter: executable lookup, Codex help checks, ACP initialize negotiation and cleanup stay equivalent.
2. Define an explicit exclusive-lifetime capability on built-in drivers. Keep legacy signature inspection in one compatibility wrapper for older injected drivers; unknown/opaque support continues to reject exclusive dispatch before launch. Do not infer support solely from provider name.
3. Common mechanics are limited to the proven duplicate DSH/Strands newline JSON send and exact-child bounded shutdown/reaping operations, extracted into small helpers only after their existing cases characterize the same behavior. Reasonix retains its bounded nonblocking frame reader and request semantics; no forced transport merger.
4. Keep each adapter's notification/request matching, permission responses, model selection, environment variables, session mutation and cancellation policy local. Discovery negotiates capabilities only and never prompts, opens a session or changes a model.
5. Do not change lease lifetime: launch intent before spawn, exact child bind before prompt, release only after reaping, retain claim when cessation is unconfirmed.

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

### [ ] Checkpoint 1: Move discovery behind the adapter contract

**Goal:** The workflow requests discovery without knowing provider names or executables.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify base.py, built-in adapters, workflow.py and harness tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Introduce the discovery method with a no-session default for adapters without current support. Move each branch of _discover_session_driver to its existing provider adapter without changing commands, timeouts, fallbacks or cleanup.
- [ ] Keep an injected test-driver path and migrate workflow tests to the adapter seam. Add a source/import-boundary check that workflow no longer branches on codex/reasonix/dsh/strands for discovery.
- [ ] Test missing executable, unsupported capability, malformed initialization and negotiation cleanup without live network/provider requests.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
uv run pytest -q tests/test_harnesses.py tests/test_harness_sessions.py tests/test_dsh.py tests/test_strands.py
```

**Expected result:** Supported/unsupported outcomes match baseline; discovery creates no user session and leaves no negotiation child alive.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Make lifetime support explicit and share only proven mechanics

**Goal:** Process ownership stays testable without spreading capability reflection or provider rules.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify session.py, adapter drivers, new small acp_process_io.py helpers and fake-process tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Add explicit built-in lifetime capability declarations and one legacy injected-driver adapter; remove repeated runtime reflection outside that adapter. Preserve fail-closed rejection for unsupported exclusive work.
- [ ] Extract only matching newline send and bounded exact-child close/reap mechanics for DSH/Strands; keep provider callbacks, permission responses and frame readers local. If the current behavior differs at a point, retain the domain wrapper instead of changing it to fit a generic helper.
- [ ] Add fake-process contract tests for full stderr, malformed response, cancellation, exact-child shutdown/reaping and unconfirmed lease retention. Keep Reasonix's partial-frame bounds and correlated-response cases.
- [ ] Update aflow/AGENTS.md, ARCHITECTURE.md and DEVLOG.md with adapter and compatibility ownership.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
uv run pytest -q tests/test_harness_sessions.py tests/test_harnesses.py tests/test_dsh.py tests/test_strands.py tests/test_execution_resource_processes.py tests/test_exclusive_execution.py
```

**Expected result:** Discovery has no coordinator provider branches; built-ins declare lifetime support and fake-process failures preserve exact ownership and existing protocol behavior.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

## Final Verification

The execution agent completes this cumulative gate after the final checkpoint's code changes and before whole-plan approval. Use focused commands within earlier checkpoints. Reuse inspectable passing evidence only when source, tests, dependencies, configuration and environment fingerprints still match; do not rerun the same broad gate at every checkpoint. A future CI run does not replace this local gate.

From the execution root, run:

```bash
uv run ruff check aflow apps/aflow_app/server/src
```

```bash
uv run pytest -q tests/test_harnesses.py tests/test_harness_sessions.py tests/test_dsh.py tests/test_strands.py tests/test_zcode.py tests/test_execution_resource_processes.py tests/test_exclusive_execution.py tests/test_hotplug.py tests/test_runtime.py
```

```bash
git diff --check
```

All commands must exit successfully with the relevant tests executed, not skipped because fixtures or browsers are missing. Existing CI OS/Python/browser/packaging gates remain required; these commands do not claim cross-platform execution locally.

Verify installed packaging once for the accepted cumulative code state:

```bash
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/17/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/17/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

**Real user/deployed surface:** Exercise adapter discovery and one owned-session turn through disposable fake executables using normal workflow code. Live provider checks are required only if a proposed edit changes provider-visible behavior; that would be an out-of-scope change to resolve before continuing.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given a supported adapter negotiates capabilities, the observable result is: Workflow receives the same supported driver without provider-specific branches.
- Given a legacy injected driver cannot prove lifetime support, the observable result is: Exclusive launch is rejected before spawning.
- Given an owned child hangs or emits malformed data, the observable result is: Existing errors/cancellation remain provider-specific and no unrelated child is killed or lease released early.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| A supported adapter negotiates capabilities | Adapter discovery and import tests |
| A legacy injected driver cannot prove lifetime support | Compatibility/exclusive dispatch tests |
| An owned child hangs or emits malformed data | Fake-process contract suites |

## Documentation Impact

Document the adapter discovery seam, built-in lifetime capability and explicit legacy compatibility boundary.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/17/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

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
