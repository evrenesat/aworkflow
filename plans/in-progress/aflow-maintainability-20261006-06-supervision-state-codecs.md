# AFLOW-MAINT-20261006 · 06/18 · Group A — Make supervision and recovery records explicit and round-trippable

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 06 of 18
- Pairing group: `AFLOW-MAINT-20261006-A`
- Group position: 05 of 09 (preference among ready members)
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A04 (manager, scope, overrides, hotplug, provider recovery)
- Priority: P2
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

Manager, repair, override, hotplug and provider-recovery records have explicit typed owners, so restoring a run preserves pending obligations without repeating several handwritten projections.

## MVP And Scope Boundary

**Now (MVP):** Manager, repair, override, hotplug and provider-recovery records have explicit typed owners, so restoring a run preserves pending obligations without repeating several handwritten projections.

**Later / Out of scope:** Changing manager decisions, routing, retry policy or provider behavior; new disk schemas; controller execution extraction; combining independent pending operations into one new state machine.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: 05: aflow-maintainability-20261006-05-execution-state-codecs.md

Outside prerequisite: inherited-hotplug-completion-repair-20261006.md must be delivered before codec extraction touches hotplug/recovery state. Its controller 20261006t025109z-d5afd011 was implementing a checkpoint repair at the 6 October 2026 08:48 UTC inspection; checked original-plan boxes alone did not establish completion. Recheck accepted commits, publication and delivery gates.

**Pairing boundary — Group A:** Complete durable supervision/recovery codecs and the scoped CI typing list before Group B members 12 and 16 start. Do not modify control-plane repository/progress projections, canonical public models or server transport fixtures; preserve the compatibility wrappers consumed there. The hotplug repair outside this effort must be delivered first.

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

- `aflow/run_state.py`
- `aflow/hotplug.py`
- `aflow/recovery.py`
- `aflow/runlog.py`
- `aflow/resume_scope.py`
- `aflow/resume_service.py`
- `tests/test_run_state.py`
- `tests/test_hotplug.py`
- `tests/test_durable_provider_recovery.py`
- `tests/test_repartition.py`
- `tests/test_resume_manager_budget.py`
- `.github/workflows/ci.yml`

## Decisions And Preserved Behavior

1. Keep the existing manager payload and hotplug/recovery disk formats. Use separate aflow/supervision_codec.py, aflow/hotplug_codec.py and aflow/recovery_codec.py owners with explicit typed encode/strict-decode/observer-decode functions.
2. Replace manager_resume_fields_strict validating then re-reading via tolerant projection with one strict typed decode and projections from that result. Tolerant history still returns partial reasons and must never be passed as validated resume authority.
3. Represent mutually dependent pending fields as tagged in-memory records only where current validation already defines those variants: no pending operation, pending boundary, pending repartition, and hotplug/recovery operation stages. Independent manager/override/hotplug obligations remain independently representable; do not invent mutual exclusions.
4. Preserve exact checkpoint/scope/partition identity, review rejection ordinals, finalized-turn replay ownership, hotplug transaction number and session identity, recovery consumed state, and successor budget semantics. Reuse plan 05's UNCHANGED contract for updates.

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

### [ ] Checkpoint 1: Give manager and checkpoint authority one decoder

**Goal:** One strict manager/scope decode supplies restoration and resume projection.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify run_state.py, new supervision_codec.py, resume_scope.py and supervision/repartition tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Move manager payload encode/restore/strict-validation field rules into cohesive functions by active scope, boundary, notes/team override, review history and repartition. Use the existing typed records; avoid one giant moved validator.
- [ ] Wire controller restoration and resume-field projection to the validated typed result; keep existing compatibility entry points as wrappers and historical observer decode as a separate result type.
- [ ] Add round-trip and distinguishing invalid cases for stale checkpoint/partition notes, malformed pending boundaries and rejection history. Keep the latest checkpoint-repair and cumulative-review behavior.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
uv run pytest -q tests/test_run_state.py tests/test_repartition.py tests/test_resume_scope_reconciliation.py tests/test_resume_checkpoint_repair.py tests/test_resume_review_repair.py tests/test_resume_manager_budget.py
```

**Expected result:** Valid manager records restore identically; stale or mismatched pending authority is rejected once, not silently dropped.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Separate hotplug and durable-recovery codecs

**Goal:** Session switches and provider-recovery obligations retain exact identity and consumption state.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify hotplug.py, recovery.py, recovery_request.py and recovery_runtime.py, new hotplug_codec.py/recovery_codec.py, runlog.py adapters and related tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Extract hotplug current/pending transaction, active sessions and history encoding/decoding without changing lifecycle behavior. Keep domain transitions with hotplug.py.
- [ ] Extract durable provider-recovery intent/brief/consumption decoding from the existing owner located with rg --files aflow | rg 'recovery'; keep persisted schema and validation authority unchanged.
- [ ] Add per-domain historical/malformed/round-trip tests plus a combined record with simultaneous independent obligations. Migrate relevant tests away from broad support imports and document field ownership/version policy.
- [ ] Extend plan 05's narrow CI type gate to supervision_codec.py, hotplug_codec.py and recovery_codec.py; keep unrelated historical modules outside the initial type-check surface.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
uv run pytest -q tests/test_hotplug.py tests/test_durable_provider_recovery.py tests/test_durable_provider_recovery_runtime.py tests/test_runlog.py tests/test_run_state.py
```

**Expected result:** An unconsumed handover/recovery remains pending through restart; malformed authority cannot trigger provider work; independent obligations coexist.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

## Final Verification

The execution agent completes this cumulative gate after the final checkpoint's code changes and before whole-plan approval. Use focused commands within earlier checkpoints. Reuse inspectable passing evidence only when source, tests, dependencies, configuration and environment fingerprints still match; do not rerun the same broad gate at every checkpoint. A future CI run does not replace this local gate.

From the execution root, run:

```bash
uv run ruff check aflow apps/aflow_app/server/src
```

```bash
uv run pytest -q tests/test_run_state.py tests/test_runlog.py tests/test_hotplug.py tests/test_repartition.py tests/test_durable_provider_recovery.py tests/test_durable_provider_recovery_runtime.py tests/test_resume_service.py tests/test_resume_checkpoint_repair.py tests/test_resume_review_repair.py tests/test_resume_scope_reconciliation.py tests/test_resume_manager_budget.py tests/test_runtime.py
```

```bash
uv run mypy --follow-imports=skip --disallow-untyped-defs aflow/run_identity_codec.py aflow/run_turn_codec.py aflow/supervision_codec.py aflow/hotplug_codec.py aflow/recovery_codec.py
```

```bash
git diff --check
```

All commands must exit successfully with the relevant tests executed, not skipped because fixtures or browsers are missing. Existing CI OS/Python/browser/packaging gates remain required; these commands do not claim cross-platform execution locally.

Verify installed packaging once for the accepted cumulative code state:

```bash
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/06/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/06/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

**Real user/deployed surface:** Use the existing disposable hotplug and durable-recovery runtime fixtures through the real controller with fake providers, verify one consumed operation and retained lineage, then inspect a historical partial record through the normal read projection.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given manager notes name an older scope or repartition generation, the observable result is: Strict resume rejects the stale authority; history can expose bounded partial evidence.
- Given a pending hotplug/recovery record is saved and restored, the observable result is: Its exact target, session and unconsumed obligation survive once.
- Given independent review and override obligations exist together, the observable result is: The codec preserves both instead of choosing one or clearing either.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| Manager notes name an older scope or repartition generation | Checkpoint 1 invalid-scope fixtures |
| A pending hotplug/recovery record is saved and restored | Checkpoint 2 round-trip and runtime suites |
| Independent review and override obligations exist together | Combined-obligation regression |

## Documentation Impact

Update ARCHITECTURE.md and nearest aflow guidance with one owner per supervision record and explicit version/compatibility policy.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/06/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

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
