# AFLOW-MAINT-20261006 · 10/18 · Group B — Give the run dashboard explicit history, observation and action owners

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 10 of 18
- Pairing group: `AFLOW-MAINT-20261006-B`
- Group position: 07 of 09 (preference among ready members)
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A05 (RunDashboard)
- Priority: P2
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

Users keep the same selected run, drafts, pending actions and quiet refresh behavior while the dashboard becomes a set of small owners for history, selected-run reads, launch review and mutations.

## MVP And Scope Boundary

**Now (MVP):** Users keep the same selected run, drafts, pending actions and quiet refresh behavior while the dashboard becomes a set of small owners for history, selected-run reads, launch review and mutations.

**Later / Out of scope:** New UI features, visual redesign, another state library, backend progress optimization, settings refactoring, changing All runs traversal or undoing the current on-demand history work.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: 01: aflow-maintainability-20261006-01-verification-baseline.md

Outside prerequisite: run-history-fast-on-demand-20261005.md must be delivered, including its remaining selected-status/on-demand behavior and browser acceptance. At the 6 October 2026 08:48 UTC inspection, successor 20261006t060326z-d4fe4b41 was owner_stopped after a worker timeout, with checkpoints 3–4 still unchecked. Verify current origin/main, publication receipt and delivery evidence before dispatch; this snapshot is not permission to resume it.

**Pairing boundary — Group B:** Group B owns dashboard/history hooks, API-client use and browser acceptance after 01's baseline edits. No runtime, persistence or provider implementation belongs here. The external history plan is a hard hold until delivered; do not use this refactor to finish its missing functionality. Other ready Group B members may proceed while it is held.

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

- `apps/aflow_app/web/src/components/RunDashboard.tsx`
- `apps/aflow_app/web/src/components/RunDashboard.test.tsx`
- `apps/aflow_app/web/src/api.ts`
- `apps/aflow_app/web/src/App.tsx`
- `apps/aflow_app/web/src/components/CheckpointHistory.tsx`
- `apps/aflow_app/server/tests/test_run_navigation_browser.py`
- `apps/aflow_app/server/tests/test_startup_context_browser.py`

## Decisions And Preserved Behavior

1. Create domain-specific hooks adjacent to the dashboard: useRunHistory, useSelectedRunObservation, useRunActions and useLaunchReview. Keep their owning shell mounted above navigable/hidden views; views render explicit state and invoke commands.
2. History generation/cursor/page ownership stays distinct from exact selected-run identity. Selected status commits independently of optional events/context; first rich history read remains demand-driven after the accepted prerequisite plan.
3. Pending action keys are indexed by exact project/run/action and survive row navigation; acknowledgement or definitive rejection clears them. Restart source stays independent of the currently selected history row; deletion tombstones absorb stale responses.
4. One visible Refresh coordinates the existing reads, retains valid partial data and rejects stale generations. Hidden dashboards suspend subscriptions/timers without losing state. No deep-context response may be downgraded by a late Lite response.
5. Preserve two compact header rows, document scrolling, mobile list/detail/Back, keyboard focus and light/dark presentation under UI_GUIDELINES.md.

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

### [ ] Checkpoint 1: Extract history and selected-run observation

**Goal:** Read ownership is explicit while paging, partial results and selection stay stable.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify RunDashboard.tsx, new useRunHistory/useSelectedRunObservation hooks and focused hook tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Start from the delivered fast-on-demand history implementation. Move its existing state and request-generation rules as cohesive owners rather than reimplementing pagination.
- [ ] Keep exact selected-run status independent of list visibility and optional events; preserve pinned off-page selection, bounded visited-page behavior and on-demand detail.
- [ ] Add late-response, partial read, hidden-view, equal-refresh and load-older/newer tests at the hook boundary while retaining dashboard integration tests.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
npm --prefix apps/aflow_app/web test -- --run src/components/RunDashboard.test.tsx src/App.test.tsx src/api.test.ts
```

**Expected result:** Paging and selected status preserve accepted behavior; unchanged refresh does not flicker, shift focus or erase useful partial data.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Extract launch review and durable action ownership

**Goal:** Navigation cannot lose unresolved actions or change their source identity.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify useRunActions/useLaunchReview, RunDashboard rendering components and existing tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Move launch-review request identity/acknowledgement and stop/restart/archive/delete state into the specified owners. Keep unresolved keys and restart source above navigable views.
- [ ] Split view rendering along existing summary/history/startup/actions sections with explicit props; do not add wrappers that change scroll ownership or remount the state owners.
- [ ] Test switch-away/switch-back during pending restart, late deletion responses and cancelled confirmation. Update nearest web guidance, ARCHITECTURE.md and DEVLOG.md.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
npm --prefix apps/aflow_app/web test -- --run src/components/RunDashboard.test.tsx src/App.test.tsx
```

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_run_navigation_browser.py apps/aflow_app/server/tests/test_run_followup_browser.py apps/aflow_app/server/tests/test_startup_context_browser.py
```

**Expected result:** Only the explicitly confirmed exact action executes; unresolved keys/drafts survive navigation and stale rows cannot resurrect deletions.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 3: Verify the extracted dashboard in both browser engines

**Goal:** The real responsive user path matches the accepted dashboard behavior.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify Existing dashboard browser tests and bounded screenshots; fix only regressions introduced by this extraction. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Run the full cumulative web gate and both browser-engine journeys from Final Verification. Inspect populated light/dark screenshots across the required viewport matrix.
- [ ] Verify quiet refresh, keyboard/focus retention, list/detail/Back, resize, partial failures and the selected-only rich-history request pattern.
- [ ] Record source/build fingerprints and the deployed read-only dashboard check; do not mark visual acceptance from component tests alone.

**Dependencies:** Checkpoint 2 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_run_navigation_browser.py apps/aflow_app/server/tests/test_responsive_browser.py
```

```bash
AFLOW_TEST_BROWSER=webkit uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_run_navigation_browser.py apps/aflow_app/server/tests/test_responsive_browser.py
```

**Expected result:** Chromium/WebKit journeys pass and inspected screenshots retain the project layout, focus and selection requirements.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

## Final Verification

The execution agent completes this cumulative gate after the final checkpoint's code changes and before whole-plan approval. Use focused commands within earlier checkpoints. Reuse inspectable passing evidence only when source, tests, dependencies, configuration and environment fingerprints still match; do not rerun the same broad gate at every checkpoint. A future CI run does not replace this local gate.

From the execution root, run:

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
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_run_navigation_browser.py apps/aflow_app/server/tests/test_run_followup_browser.py apps/aflow_app/server/tests/test_startup_context_browser.py apps/aflow_app/server/tests/test_run_progress_browser.py apps/aflow_app/server/tests/test_responsive_browser.py
```

```bash
AFLOW_TEST_BROWSER=webkit uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_run_navigation_browser.py apps/aflow_app/server/tests/test_run_followup_browser.py apps/aflow_app/server/tests/test_startup_context_browser.py apps/aflow_app/server/tests/test_run_progress_browser.py apps/aflow_app/server/tests/test_responsive_browser.py
```

```bash
git diff --check
```

All commands must exit successfully with the relevant tests executed, not skipped because fixtures or browsers are missing. Existing CI OS/Python/browser/packaging gates remain required; these commands do not claim cross-platform execution locally.

Verify installed packaging once for the accepted cumulative code state:

```bash
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/10/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/10/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

Browser acceptance includes 320×568, 390×844, 768×1024, 844×390, 1280×720, 1440×900 and reduced 390×420; populated light/dark, long labels/content, partial failures, enlarged text, keyboard/focus, hit-testing, document scrolling and list/detail/Back. Inspect screenshots, not only geometry assertions. Report physical mobile keyboard/toolbar behavior separately from emulation.

**Real user/deployed surface:** After existing CI-gated deployment, open real project Runs, page history, select an off-page run, open/close detail, refresh and navigate away/back in desktop and mobile emulation. Read-only checks suffice; action tests use disposable fixtures.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given user leaves Runs while a restart is pending, then returns, the observable result is: Exact source and unresolved mutation key survive; no duplicate restart is submitted.
- Given old status/context or deleted-row responses arrive late, the observable result is: Current identity/detail depth/tombstones win; stale evidence is ignored.
- Given user refreshes unchanged content or resizes to mobile, the observable result is: Selection, focus, draft and document scroll remain stable; hidden views are quiet.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| User leaves Runs while a restart is pending, then returns | Action integration and follow-up browsers |
| Old status/context or deleted-row responses arrive late | Hook and dashboard tests |
| User refreshes unchanged content or resizes to mobile | Both-engine responsive/navigation journeys |

## Documentation Impact

Document each hook's sole owner, input/result contract and generation/identity rules. Keep UI_GUIDELINES.md acceptance rules intact.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/10/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

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
