# AFLOW-MAINT-20261006 · 01/18 · Group A — Make the existing validation baseline dependable

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 01 of 18
- Pairing group: `AFLOW-MAINT-20261006-A`
- Group position: 01 of 09 (preference among ready members)
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A10
- Priority: P1
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

Developers get dependable feedback before refactoring: fixture repositories retain required plan files despite operator Git ignores, and the existing web lint command runs successfully in CI.

## MVP And Scope Boundary

**Now (MVP):** Developers get dependable feedback before refactoring: fixture repositories retain required plan files despite operator Git ignores, and the existing web lint command runs successfully in CI.

**Later / Out of scope:** Repository-wide test rewrites, new lint rule sets, a whole-project type checker, unrelated hook rewrites and broad test-support cleanup. Move cohesive tests only with the production extractions in the other effort plans.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: None.

No additional outside prerequisite is named for this member. Recheck current active work, accepted main and the pairing index before dispatch; a newly overlapping active change is a hold to reconcile, not permission to overwrite it.

**Pairing boundary — Group A:** Opening member of Group A. Pair with 02: this member owns baseline CI and existing broad REST/MCP fixture helpers; 02 puts new configuration-pair transport tests in its own focused test module. After delivery, UI ownership passes to Group B and the later codec CI gate stays with 05–06 until handed to 16.

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

- `.github/workflows/ci.yml`
- `apps/aflow_app/server/tests/test_control_plane_api.py`
- `apps/aflow_app/server/tests/test_mcp.py`
- `apps/aflow_app/web/src/App.tsx`
- `apps/aflow_app/web/src/api.ts`
- `apps/aflow_app/web/src/components/PlanPanel.tsx`
- `apps/aflow_app/web/src/components/RunDashboard.tsx`
- `apps/aflow_app/web/src/components/GlobalSettings.test.tsx`
- `apps/aflow_app/web/src/components/GuidedConfigForm.test.tsx`

## Decisions And Preserved Behavior

1. The review's missing fixture-plan defect was repaired before conversion: _commit_fixture_repository now uses git add -f -A (e57d69ea and 87ba106c). Retain that bounded fix; add a regression proving it under a disposable global /plans/ ignore instead of replacing it with a new fixture framework.
2. On d2a9a5e9, npm run lint still reports 8 errors and 11 warnings. Fix the eight errors under existing rules. Hook warnings are review inputs, not permission to change request timing; this plan does not introduce --max-warnings=0 or disable rules.
3. Replace return-from-finally patterns with explicit guarded cleanup that preserves resolution/rejection. Replace the intentional infinite retry loop with an equivalent loop form accepted by the existing rule, without changing retry limits or cancellation.

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

### [ ] Checkpoint 1: Prove fixture independence and repair current lint errors

**Goal:** Existing tests and request cleanup keep the same observable behavior under the repaired baseline.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify The listed server fixtures and lint-error web files, plus adjacent focused tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Add a test using a task-owned Git config and excludes file containing /plans/; call the real fixture commit helper and assert the plan is tracked and present in the fixture worktree. Scope environment changes to the fixture subprocess/test; never write the operator's Git configuration.
- [ ] Repair the current eight lint errors at their reported sites. For finally cleanup in App and PlanPanel, preserve success, rejection, cancellation and superseded-request behavior in focused tests; retain API retry/cancel behavior and make never-reassigned bindings const.
- [ ] Keep pre-existing hook warnings documented with their exact source; make no speculative hook dependency or rendering changes. Preserve current history work when resolving the three dashboard const sites.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_control_plane_api.py apps/aflow_app/server/tests/test_mcp.py -k 'recovery or fixture'
```

```bash
npm --prefix apps/aflow_app/web run lint
```

```bash
npm --prefix apps/aflow_app/web test -- --run src/App.test.tsx src/api.test.ts src/components/PlanPanel.test.tsx src/components/RunDashboard.test.tsx
```

**Expected result:** Fixture recovery cases pass with the hostile ignore file; lint exits 0; cleanup never swallows a rejection or cancels a newer request.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Make the repaired command a required dashboard CI step

**Goal:** Every pull request exercises the same web lint baseline before build.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify .github/workflows/ci.yml and the existing contributor validation documentation. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Add npm run lint after npm ci in the existing dashboard job, before test/build. Preserve all current OS/Python, root test, browser and packaging gates.
- [ ] Record the existing baseline commands and explain that tests moved by later plans keep explicit imports and focused fixtures. Do not configure project-wide Git overrides to make tests pass.
- [ ] Update DEVLOG.md and the existing README validation section; ARCHITECTURE.md needs no structural change for this plan.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
npm --prefix apps/aflow_app/web run lint
```

```bash
git diff --check
```

**Expected result:** The workflow contains a non-optional lint step and retains every previous required gate.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

## Final Verification

The execution agent completes this cumulative gate after the final checkpoint's code changes and before whole-plan approval. Use focused commands within earlier checkpoints. Reuse inspectable passing evidence only when source, tests, dependencies, configuration and environment fingerprints still match; do not rerun the same broad gate at every checkpoint. A future CI run does not replace this local gate.

From the execution root, run:

```bash
uv run ruff check aflow apps/aflow_app/server/src
```

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_control_plane_api.py apps/aflow_app/server/tests/test_mcp.py
```

```bash
npm --prefix apps/aflow_app/web run lint
```

```bash
npm --prefix apps/aflow_app/web test -- --run
```

```bash
npm --prefix apps/aflow_app/web run build
```

```bash
git diff --check
```

All commands must exit successfully with the relevant tests executed, not skipped because fixtures or browsers are missing. Existing CI OS/Python/browser/packaging gates remain required; these commands do not claim cross-platform execution locally.

Verify installed packaging once for the accepted cumulative code state:

```bash
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/01/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/01/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

**Real user/deployed surface:** Inspect the published commit's existing Dashboard CI checks and confirm the new lint step actually ran. This plan adds a development gate; it does not claim a new production feature.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given a fixture uses a global /plans/ ignore, the observable result is: The real fixture helper still commits its plan and durable-evidence recovery finds the worktree copy.
- Given an async request rejects while cleanup is needed, the observable result is: The original rejection remains observable, and stale cleanup cannot clear a newer request.
- Given a PR introduces one of the existing lint errors, the observable result is: The required Dashboard lint step fails before build.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| A fixture uses a global /plans/ ignore | Checkpoint 1 hostile-ignore regression and server recovery suites |
| An async request rejects while cleanup is needed | Checkpoint 1 App/PlanPanel/API tests |
| A PR introduces one of the existing lint errors | CI workflow inspection and exact-commit CI run |

## Documentation Impact

Update DEVLOG.md and README validation commands only. Preserve root AGENTS.md and current runtime ownership documentation.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/01/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

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
