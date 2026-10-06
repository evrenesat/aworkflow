# AFLOW-MAINT-20261006 · 05/18 — Give execution identity and metadata one codec per responsibility

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 05 of 18
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A04 (identity, turn, update semantics)
- Priority: P2
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

Execution identity, turn metadata and completion updates are decoded and written consistently without silently losing worktree, scope, failure or lineage fields.

## MVP And Scope Boundary

**Now (MVP):** Execution identity, turn metadata and completion updates are decoded and written consistently without silently losing worktree, scope, failure or lineage fields.

**Later / Out of scope:** Manager/repartition/hotplug codecs (plan 06), on-disk migrations, one universal state container, replacing the authoritative run.json, and a new validation library.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: 04: aflow-maintainability-20261006-04-transport-neutral-resume.md

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

- `aflow/run_state.py`
- `aflow/runlog.py`
- `aflow/resume_service.py`
- `aflow/resume_evidence.py`
- `aflow/control_plane/repository.py`
- `tests/test_run_state.py`
- `tests/test_runlog.py`
- `tests/test_resume_relocation.py`
- `tests/test_control_plane_repository.py`
- `pyproject.toml`
- `uv.lock`
- `.github/workflows/ci.yml`

## Decisions And Preserved Behavior

1. Create aflow/run_identity_codec.py for execution/config/lineage/worktree identity and aflow/run_turn_codec.py for current/finalized turn and completion/failure metadata. Each owns field names, validation and serialization for its group; keep existing flat durable keys through adapters.
2. Strict execution decode returns typed validated records or a bounded error. Observer decode returns typed partial evidence plus reason codes and never authorizes execution. Preserve supported historical defaults exactly; malformed present authority is different from an absent historical optional field.
3. Introduce an internal UNCHANGED sentinel for metadata patch values. UNCHANGED preserves prior bytes/meaning; explicit None clears only currently clearable fields; a value replaces. Keep existing RunMetadataWriter.write argument behavior through a compatibility adapter, so old call sites do not suddenly clear sticky evidence.
4. Use a recorded field-ownership inventory to ensure every migrated authoritative field has one writer/decoder owner. Preserve unknown forward-compatible keys wherever current writers preserve them; do not add a generic reflective codec.
5. Introduce mypy only as a development check for the two new pure codec modules, pinned through uv.lock; use --follow-imports=skip --disallow-untyped-defs to keep the initial gate scoped. Preserve the existing Ruff/test/compile gates and do not turn unrelated legacy typing into this plan's work.

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

### [ ] Checkpoint 1: Extract execution identity and turn codecs without format changes

**Goal:** Supported records round-trip and invalid current authority cannot become a valid resume.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify run_state.py; new run_identity_codec.py/run_turn_codec.py; resume evidence adapter; new tests/test_execution_state_codecs.py. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Characterize current identity, current-turn, completion and failure fixtures and assign every touched durable key to one group before extraction. Preserve schema versions and legacy absence rules.
- [ ] Implement explicit typed encode, strict decode and tolerant observer decode functions for those groups; adapt existing state/resume constructors at one boundary.
- [ ] Add field-by-field round-trip tests, malformed type/identity tests and historical partial-record fixtures; compare normalized serialized content against the pre-extraction contract.
- [ ] Add mypy to the dev dependency group with uv add --group dev 'mypy>=1.18,<2'; add the exact scoped codec check below to the existing Python CI job. Do not enable project-wide strict checking or suppress errors in the new codec interfaces.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
uv run pytest -q tests/test_execution_state_codecs.py tests/test_run_state.py tests/test_runlog.py tests/test_resume_relocation.py
```

```bash
uv run mypy --follow-imports=skip --disallow-untyped-defs aflow/run_identity_codec.py aflow/run_turn_codec.py
```

**Expected result:** Every owned key survives valid round trips; observer omissions carry reasons; malformed execution identity fails closed.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Make update preservation and clearing explicit

**Goal:** Metadata writes cannot accidentally erase durable evidence through ambiguous omission.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify runlog.py, codec update adapters, direct writer call sites and observer adapters. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Implement typed update objects using UNCHANGED. Migrate RunMetadataWriter's identity/turn/completion preservation logic to group-owned patch functions while preserving its old public/default behavior.
- [ ] Add tests for omitted worktree/scope/lineage/failure fields, permitted explicit clearing and rejected clearing of identity; verify unknown keys retain current compatibility behavior.
- [ ] Move related tests into explicit small codec fixtures while retaining real write/resume/read integration. Update ARCHITECTURE.md, aflow/AGENTS.md and DEVLOG.md.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
uv run pytest -q tests/test_execution_state_codecs.py tests/test_runlog.py tests/test_control_plane_repository.py tests/test_control_plane_resume.py
```

**Expected result:** Omission preserves evidence; permitted clearing is deliberate; strict and tolerant consumers share codecs without sharing authority.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

## Final Verification

The execution agent completes this cumulative gate after the final checkpoint's code changes and before whole-plan approval. Use focused commands within earlier checkpoints. Reuse inspectable passing evidence only when source, tests, dependencies, configuration and environment fingerprints still match; do not rerun the same broad gate at every checkpoint. A future CI run does not replace this local gate.

From the execution root, run:

```bash
uv run ruff check aflow apps/aflow_app/server/src
```

```bash
uv run pytest -q tests/test_execution_state_codecs.py tests/test_run_state.py tests/test_runlog.py tests/test_control_plane_repository.py tests/test_control_plane_resume.py tests/test_resume_service.py tests/test_resume_relocation.py tests/test_resume_scope_reconciliation.py tests/test_runtime.py
```

```bash
uv run mypy --follow-imports=skip --disallow-untyped-defs aflow/run_identity_codec.py aflow/run_turn_codec.py
```

```bash
git diff --check
```

All commands must exit successfully with the relevant tests executed, not skipped because fixtures or browsers are missing. Existing CI OS/Python/browser/packaging gates remain required; these commands do not claim cross-platform execution locally.

Verify installed packaging once for the accepted cumulative code state:

```bash
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/05/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/05/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

**Real user/deployed surface:** Run a disposable fake-harness workflow, write/finish its turn, then read it through the normal control-plane repository and prepare continuation through plan 04's service. Compare identity, outcome and lineage end to end; production smoke is a read-only run-detail check after normal deployment.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given a status-only update omits worktree and prior failure metadata, the observable result is: Required prior evidence survives exactly according to the existing sticky-field contract.
- Given a historical optional field is missing, the observable result is: Display remains partial with reasons, while supported resume defaults remain supported.
- Given current identity contains a malformed run/worktree/scope relationship, the observable result is: Strict admission rejects it and tolerant display never upgrades it into authority.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| A status-only update omits worktree and prior failure metadata | Checkpoint 2 omission/clear tests |
| A historical optional field is missing | Codec historical fixtures |
| Current identity contains a malformed run/worktree/scope relationship | Strict versus observer tests |

## Documentation Impact

Add a compact field-owner/version-policy map; explain explicit preserve/clear behavior and strict-versus-observer use.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/05/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

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
