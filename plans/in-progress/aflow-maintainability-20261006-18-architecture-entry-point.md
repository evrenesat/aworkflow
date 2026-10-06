# AFLOW-MAINT-20261006 · 18/18 · Group B — Publish a concise source-grounded maintainability map

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 18 of 18
- Pairing group: `AFLOW-MAINT-20261006-B`
- Group position: 09 of 09 (preference among ready members)
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A14; documentation closure for A01–A13
- Priority: P2
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

A contributor or AFlow concierge can locate launch, resume, state, settings, progress, provider and delivery owners from one short architecture entry point and can tell implemented facts from remaining work.

## MVP And Scope Boundary

**Now (MVP):** A contributor or AFlow concierge can locate launch, resume, state, settings, progress, provider and delivery owners from one short architecture entry point and can tell implemented facts from remaining work.

**Later / Out of scope:** Concierge implementation decomposition, broad manager-context refactoring, new documentation site/tooling, product behavior changes, rewriting history, and modifying root AGENTS.md.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: 01: aflow-maintainability-20261006-01-verification-baseline.md; 02: aflow-maintainability-20261006-02-configuration-crash-recovery.md; 03: aflow-maintainability-20261006-03-configuration-validation-ownership.md; 04: aflow-maintainability-20261006-04-transport-neutral-resume.md; 05: aflow-maintainability-20261006-05-execution-state-codecs.md; 06: aflow-maintainability-20261006-06-supervision-state-codecs.md; 07: aflow-maintainability-20261006-07-workflow-startup-boundaries.md; 08: aflow-maintainability-20261006-08-workflow-turn-execution.md; 09: aflow-maintainability-20261006-09-workflow-progression-delivery.md; 10: aflow-maintainability-20261006-10-dashboard-state-owners.md; 11: aflow-maintainability-20261006-11-settings-draft-save-owners.md; 12: aflow-maintainability-20261006-12-progress-evidence-pipeline.md; 13: aflow-maintainability-20261006-13-event-journal-read-cost.md; 14: aflow-maintainability-20261006-14-persistence-primitives.md; 15: aflow-maintainability-20261006-15-server-app-composition.md; 16: aflow-maintainability-20261006-16-generated-wire-contracts.md; 17: aflow-maintainability-20261006-17-provider-discovery-boundaries.md

No additional outside prerequisite is named for this member. Recheck current active work, accepted main and the pairing index before dispatch; a newly overlapping active change is a hold to reconcile, not permission to overwrite it.

**Pairing boundary — Group B:** Closing member of Group B, not an overlap candidate. Start only after all other 17 effort members are delivered and their integration evidence is current. Reconcile both groups into one source-grounded architecture map; earlier members still document their own changes at delivery.

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

- `ARCHITECTURE.md`
- `README.md`
- `DEVLOG.md`
- `aflow/AGENTS.md`
- `aflow/control_plane/AGENTS.md`
- `apps/aflow_app/server/AGENTS.md`
- `apps/aflow_app/web/AGENTS.md`
- `docs/configuration.md`
- `docs`

## Decisions And Preserved Behavior

1. Keep ARCHITECTURE.md as the short entry point, aiming at 250–350 lines with ownership, dependency direction, source-of-truth, supported entry points and links. Move detailed recovery/admission/progress/configuration/delivery material into domain documents without discarding operational invariants or historical evidence.
2. Document only code delivered by this effort and current verified dependencies. Each responsibility has one linked owning module and the main entry/caller direction; show compatibility facades separately from active owners.
3. Keep generic product behavior separate from p100 operation and legacy compatibility. Existing CI-gated deployment/rollback remains authoritative; source inspection is not deployed-usability evidence.
4. Close documentation coverage for all source findings in the effort index, recording exact implementation/verification/publication status. The index is coordination evidence, not a replacement for checkpoint plans, run state or publication receipts.
5. Respect aflow-plan's root-guidance boundary: do not edit root AGENTS.md. Explain any still-stale global status statement by linking verified implementation evidence in the architecture entry point and the nearer applicable guidance; record unresolved owner guidance separately.

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

### [ ] Checkpoint 1: Restructure the architecture map around actual owners

**Goal:** The main entry point directs each common maintenance task to a small relevant source set.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify ARCHITECTURE.md, new concise domain documents under docs, relevant README links and nearest guidance. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Reconcile the delivered modules and public entry points from plans 02–17 against current main. Map launch/resume/control, controller phases, codecs, configuration, projections, providers and publication.
- [ ] Move detailed invariants/history into linked domain documents; retain their source references and supported compatibility notes. Add a nested docs AGENTS.md only if a newly created domain directory has distinct responsibilities; prefer existing docs files where adequate.
- [ ] Update nearest AGENTS.md and README links to the actual owners. Label unverified live behavior and hardware-only UI checks explicitly; do not call pending work delivered.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
uv run pytest -q tests/test_docs.py
```

```bash
git diff --check
```

**Expected result:** A reader can find all named owners through ARCHITECTURE.md without reading the change history; links and invariants survive the move.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Audit effort coverage and comprehension

**Goal:** The single effort has a truthful completion map with no orphan findings or hidden unfinished acceptance.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify The effort index, documentation and bounded source/size audit evidence only. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Check every A01–A14 row against its member plans and actual source/test evidence. Keep deferred concierge/manager-context work explicitly out of scope, not silently complete.
- [ ] Record before/after maintained-code function/class/file sizes for extracted owners using Python AST and the TypeScript compiler API already installed in web node_modules; exclude generated types, vendored code and tests from production size counts.
- [ ] Review new or substantially rewritten functions above 80 lines, orchestration above about 120, classes/components above 300 and behavior files above 500. Record cohesive exceptions; reject wrapper-only movement, shared-all-locals contexts and new dependency inversions.
- [ ] Verify documentation and index links, exact published commits and delivery evidence. Update DEVLOG.md with the effort result and remaining limitations; do not launch unrelated queued plans.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
uv run pytest -q tests/test_docs.py
```

```bash
git diff --check
```

**Expected result:** Every finding is covered or explicitly deferred and the architecture map matches current source; no code/tests/deployments are falsely marked complete.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

## Final Verification

The execution agent completes this cumulative gate after the final checkpoint's code changes and before whole-plan approval. Use focused commands within earlier checkpoints. Reuse inspectable passing evidence only when source, tests, dependencies, configuration and environment fingerprints still match; do not rerun the same broad gate at every checkpoint. A future CI run does not replace this local gate.

From the execution root, run:

```bash
uv run pytest -q tests/test_docs.py
```

```bash
git diff --check
```

```bash
git diff --check
```

All commands must exit successfully with the relevant tests executed, not skipped because fixtures or browsers are missing. Existing CI OS/Python/browser/packaging gates remain required; these commands do not claim cross-platform execution locally.

**Real user/deployed surface:** Read the GitHub-rendered architecture entry point and follow its launch/resume/config/progress/publication links to actual owning source. This documentation-only plan needs no new provider run or manual service restart.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given a newcomer wants to change resume, config save, progress or delivery, the observable result is: The short entry point names the owner, callers, authority and focused tests.
- Given a concierge searches AFLOW-MAINT-20261006, the observable result is: It finds exactly this effort's index and 18 members, with their dependencies and evidence.
- Given a refactor only moves an oversized function behind a new class, the observable result is: The source/size audit rejects the claimed maintainability completion.
- Given some live acceptance is still unverified, the observable result is: The index retains that gap instead of declaring the effort deployed.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| A newcomer wants to change resume, config save, progress or delivery | Manual five-task navigation audit |
| A concierge searches AFLOW-MAINT-20261006 | Effort membership/link audit |
| A refactor only moves an oversized function behind a new class | Checkpoint 2 comprehension audit |
| Some live acceptance is still unverified | Receipt and acceptance reconciliation |

## Documentation Impact

This is the documentation consolidation pass; earlier plans still update their own changed behavior immediately.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/18/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

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
