# AFLOW-MAINT-20261006 · 04/18 · Group A — Move shared resume operations out of the CLI

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 04 of 18
- Pairing group: `AFLOW-MAINT-20261006-A`
- Group position: 03 of 09 (preference among ready members)
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A03
- Priority: P1
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

CLI, REST and MCP continuation use one transport-neutral service with the same safety decisions; changing CLI argument handling cannot change daemon recovery.

## MVP And Scope Boundary

**Now (MVP):** CLI, REST and MCP continuation use one transport-neutral service with the same safety decisions; changing CLI argument handling cannot change daemon recovery.

**Later / Out of scope:** New resume behavior, altered budget/team precedence, controller phase extraction, new saved-state schema, public endpoint changes, and unrelated daemon decomposition.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: 01: aflow-maintainability-20261006-01-verification-baseline.md

No additional outside prerequisite is named for this member. Recheck current active work, accepted main and the pairing index before dispatch; a newly overlapping active change is a hold to reconcile, not permission to overwrite it.

**Pairing boundary — Group A:** Own resume orchestration, startup record decoding and migration of REST/MCP resume test fixtures. Group B member 15 waits for this delivery before moving those shared fixtures into app instances. Do not move server composition, configuration editing, control-plane progress or event-journal ownership here.

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

- `aflow/cli.py`
- `aflow/daemon.py`
- `aflow/control_plane/run_activity.py`
- `aflow/run_state.py`
- `tests/test_cli.py`
- `tests/test_control_plane_resume.py`
- `tests/test_resume_checkpoint_repair.py`
- `tests/test_resume_review_repair.py`
- `tests/test_resume_scope_reconciliation.py`
- `apps/aflow_app/server/tests/test_control_plane_api.py`
- `apps/aflow_app/server/tests/test_mcp.py`

## Decisions And Preserved Behavior

1. Create aflow/resume_service.py for request orchestration and typed ResumeRequest/PreparedResume results, aflow/resume_evidence.py for durable reads/liveness/workspace validation, and aflow/resume_scope.py for scope/repair reconciliation. Split the existing large bootstrap and context reconstruction by these responsibilities rather than moving them wholesale.
2. Keep all existing public CLI flags and current service result/error behavior. Explicit successor max-turn/team choices retain current precedence; omission inherits the supported live-source and lineage rules. Review/scope evidence and exact owned inactivity continue to gate continuation.
3. Move startup-question record decoding from private daemon helpers to aflow/startup_records.py. It parses/validates records; it cannot reserve a run, accept a question or repair files. run_activity consumes that lower-level decoder.
4. CLI handles argument/interactive decisions then calls the service. Daemon/worker preparation calls it directly. Preserve temporary compatibility aliases for imported CLI helpers while migrating tests, but no application, worker or observer code may import aflow.cli.

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

### [ ] Checkpoint 1: Extract the shared resume preparation service

**Goal:** One saved-run input yields the same prepared continuation through the new service and existing CLI.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify cli.py; new resume_service.py, resume_evidence.py, resume_scope.py; focused resume tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Add explicit request/result dataclasses with current supported choices and validated evidence results. Move filesystem/process observations into resume_evidence and pure scope/repair decisions into resume_scope; keep orchestration small and do not pass a dict of all CLI locals.
- [ ] Migrate CLI bootstrap/reconstruction to the service with compatibility forwarding functions. Preserve pending finalized turns, cumulative review, interrupted checkpoint repair, completion-only recovery and the recently repaired successor budget behavior.
- [ ] Move cohesive unit tests to new tests/test_resume_service.py with explicit fixtures/imports; retain integrated real-repository tests. Add accepted, rejected and partial/unknown evidence cases before removing old internals.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
uv run pytest -q tests/test_resume_service.py tests/test_cli.py tests/test_resume_scope_reconciliation.py tests/test_resume_checkpoint_repair.py tests/test_resume_review_repair.py tests/test_resume_manager_budget.py
```

**Expected result:** Prepared paths, scope, budgets, replay obligations and bounded rejection reasons match current behavior.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Route daemon and observers through lower-level owners

**Goal:** Shared recovery and startup-record interpretation no longer depend on entry-point modules.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify daemon.py, run_activity.py, new startup_records.py and related REST/MCP tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Replace daemon's three private CLI resume imports and any worker-path CLI imports found by rg with direct service calls; keep transport response adaptation at existing boundaries.
- [ ] Move _question_from_record/_question_generation decoding into startup_records and use it from daemon/run_activity with unchanged freshness/identity rules.
- [ ] Replace tests patching CLI internals for HTTP/MCP with service-boundary fixtures. Add an AST import-direction test covering local and top-level imports and run parity cases for ordinary, owner-stopped, unknown-liveness, repair-pending and completion-only continuation.
- [ ] Update ARCHITECTURE.md, aflow/AGENTS.md and DEVLOG.md with shared ownership, without changing public CLI usage.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
uv run pytest -q tests/test_control_plane_resume.py tests/test_startup_context.py tests/test_resume_pending_review.py tests/test_budget_exit_recovery.py tests/test_resume_service.py
```

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_control_plane_api.py apps/aflow_app/server/tests/test_mcp.py -k 'resume or restart or startup'
```

**Expected result:** CLI/REST/MCP preserve equivalent continuation decisions; no application or observer imports aflow.cli.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

## Final Verification

The execution agent completes this cumulative gate after the final checkpoint's code changes and before whole-plan approval. Use focused commands within earlier checkpoints. Reuse inspectable passing evidence only when source, tests, dependencies, configuration and environment fingerprints still match; do not rerun the same broad gate at every checkpoint. A future CI run does not replace this local gate.

From the execution root, run:

```bash
uv run ruff check aflow apps/aflow_app/server/src
```

```bash
uv run pytest -q tests/test_resume_service.py tests/test_cli.py tests/test_control_plane_resume.py tests/test_resume_checkpoint_repair.py tests/test_resume_review_repair.py tests/test_resume_scope_reconciliation.py tests/test_resume_pending_review.py tests/test_resume_relocation.py tests/test_resume_manager_budget.py tests/test_budget_exit_recovery.py tests/test_runtime.py
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
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/04/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/04/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

**Real user/deployed surface:** Exercise installed CLI help and the existing disposable REST/MCP continuation fixture with a fake harness, then inspect the resulting single successor and lineage. After deployment, verify read-only restart options on a known inactive run; do not resume owner workloads just for a refactor smoke test.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given the same inactive saved run is resumed through CLI, REST and MCP, the observable result is: All select equivalent scope, source configuration and inherited/explicit budgets.
- Given unit liveness is unknown or scope evidence conflicts, the observable result is: All entry points reject continuation without allocating a competing controller.
- Given an interrupted reviewed repair or delivery-only continuation is eligible, the observable result is: The specific pending obligation resumes; approved implementation is not replayed.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| The same inactive saved run is resumed through CLI, REST and MCP | Service parity and integrated transport tests |
| Unit liveness is unknown or scope evidence conflicts | Control-plane and evidence rejection tests |
| An interrupted reviewed repair or delivery-only continuation is eligible | Repair and completion resume suites |

## Documentation Impact

Record the new application-operation and record-codec owners. CLI public behavior stays documented as before.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/04/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

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
