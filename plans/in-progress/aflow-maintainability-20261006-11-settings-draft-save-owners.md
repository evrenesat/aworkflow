# AFLOW-MAINT-20261006 · 11/18 · Group B — Extract the settings draft owner and save coordinator

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 11 of 18
- Pairing group: `AFLOW-MAINT-20261006-B`
- Group position: 03 of 09 (preference among ready members)
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A05 (GlobalSettings)
- Priority: P2
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

Users can edit across settings tabs and recover from partial saves without losing drafts, while save order and acknowledgement rules live in one small, testable owner.

## MVP And Scope Boundary

**Now (MVP):** Users can edit across settings tabs and recover from partial saves without losing drafts, while save order and acknowledgement rules live in one small, testable owner.

**Later / Out of scope:** Changing save order, adding settings domains, changing auth/credential storage, another state library, changing TOML actions or redesigning forms.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: 01: aflow-maintainability-20261006-01-verification-baseline.md; 02: aflow-maintainability-20261006-02-configuration-crash-recovery.md

No additional outside prerequisite is named for this member. Recheck current active work, accepted main and the pairing index before dispatch; a newly overlapping active change is a hold to reconcile, not permission to overwrite it.

**Pairing boundary — Group B:** Group B owns draft/save coordination and its existing client tests. Consume 02's delivered save/revision behavior without changing configuration transactions or Group A runtime code. Preserve API signatures so later wire generation and simultaneous runtime work remain independent.

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

- `apps/aflow_app/web/src/components/GlobalSettings.tsx`
- `apps/aflow_app/web/src/components/GlobalSettings.test.tsx`
- `apps/aflow_app/web/src/settingsDraft.ts`
- `apps/aflow_app/web/src/components/SkillsSettings.tsx`
- `apps/aflow_app/web/src/components/TeamFamiliesSettings.tsx`
- `apps/aflow_app/server/tests/test_settings_browser.py`
- `apps/aflow_app/server/tests/test_settings_reload_browser.py`

## Decisions And Preserved Behavior

1. Create useSettingsDraft for cross-tab values, baselines, revisions and dirty guards, and a pure saveSettingsDraft coordinator that emits per-domain acknowledgements. Keep the owner mounted above tabs; preserve the existing TeamFamiliesSettings/wizard mounted owner and reset-version behavior.
2. Preserve the verified write order: validate the frozen skill text/revision pairs and dirty domain inputs before writes; workflow configuration first, project scheduling second, dirty skills in sorted name order third, and server/connection settings including password last. Preserve epoch rejection checks and per-domain acknowledgement at each step; no parallel saves or reordering.
3. Only acknowledged domains clear their dirty state and advance baselines. A conflict/failure keeps the failed and unattempted edits and their original revisions. Browser-only preferences never become TOML/server config; skill drafts never enter TOML.
4. Prompt-delete Undo, selected exact skill identity, per-skill revision, install-disabled-while-dirty and separate recent-count input commit on blur/Enter remain with the shared owner.

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

### [ ] Checkpoint 1: Extract shared draft and per-domain acknowledgement state

**Goal:** Navigation and reload preserve the same dirty ownership and baselines.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify GlobalSettings.tsx, settingsDraft.ts, new useSettingsDraft.ts and focused tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Characterize all current persisted domains and their baselines; extract typed domain draft state and actions without collapsing independently acknowledged revisions.
- [ ] Keep owner and wizard mounted across tabs with actual hidden semantics. Preserve Undo, dirty/navigation guard, explicit discard/reset and separate input edit buffers.
- [ ] Add tests for config unavailable with usable skill drafts, stale reloads, exact-skill revision conflicts, tab switches and resize.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
npm --prefix apps/aflow_app/web test -- --run src/components/GlobalSettings.test.tsx src/settingsDraft.test.ts
```

**Expected result:** Only explicit reset/discard or acknowledged save clears drafts; navigation and partial read errors retain them.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Extract and characterize one save operation

**Goal:** Partial acknowledgement is visible and no successful domain is saved twice on retry.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify New saveSettingsDraft.ts, GlobalSettings.tsx and existing API/domain tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Lock the verified order (workflow config, project scheduling, sorted skills, server/connection settings and password last) into a focused test, then move it unchanged to a coordinator with typed inputs/results and injected existing API calls.
- [ ] Apply acknowledgements to the draft owner one domain at a time. After a mid-sequence failure, keep later edits and their revisions; a retry sends only still-dirty net changes.
- [ ] Preserve sorted skill ordering and install gating; separate tab rendering from save/state orchestration. Document owners and ordering in web AGENTS.md, ARCHITECTURE.md and DEVLOG.md.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
npm --prefix apps/aflow_app/web test -- --run src/components/GlobalSettings.test.tsx src/settingsDraft.test.ts src/api.test.ts
```

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_settings_browser.py apps/aflow_app/server/tests/test_settings_reload_browser.py
```

**Expected result:** Config success followed by skill failure clears only config; unsaved skills and subsequent domains stay editable and retryable.

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
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_settings_browser.py apps/aflow_app/server/tests/test_settings_reload_browser.py apps/aflow_app/server/tests/test_team_editor_priority_browser.py apps/aflow_app/server/tests/test_responsive_browser.py
```

```bash
AFLOW_TEST_BROWSER=webkit uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_settings_browser.py apps/aflow_app/server/tests/test_settings_reload_browser.py apps/aflow_app/server/tests/test_team_editor_priority_browser.py apps/aflow_app/server/tests/test_responsive_browser.py
```

```bash
git diff --check
```

All commands must exit successfully with the relevant tests executed, not skipped because fixtures or browsers are missing. Existing CI OS/Python/browser/packaging gates remain required; these commands do not claim cross-platform execution locally.

Verify installed packaging once for the accepted cumulative code state:

```bash
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/11/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/11/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

Browser acceptance includes 320×568, 390×844, 768×1024, 844×390, 1280×720, 1440×900 and reduced 390×420; populated light/dark, long labels/content, partial failures, enlarged text, keyboard/focus, hit-testing, document scrolling and list/detail/Back. Inspect screenshots, not only geometry assertions. Report physical mobile keyboard/toolbar behavior separately from emulation.

**Real user/deployed surface:** Run the full save/partial-failure/retry path on disposable settings in both browsers; inspect loaded light/dark desktop/mobile layouts. After deployment, read real Settings and navigate tabs without saving owner preferences.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given workflow config saves, then a skill save conflicts, the observable result is: Only the config domain is acknowledged; failed/unattempted drafts retain original revisions.
- Given user navigates tabs with dirty skills and a prompt Undo action, the observable result is: Both survive; install remains disabled until all skill edits are clean.
- Given configuration projection is unavailable, the observable result is: Existing skill drafts remain usable with truthful partial failure feedback.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| Workflow config saves, then a skill save conflicts | Save-coordinator partial-ack test |
| User navigates tabs with dirty skills and a prompt Undo action | Draft/component/browser tests |
| Configuration projection is unavailable | Draft and settings reload tests |

## Documentation Impact

State the exact existing save sequence and per-domain acknowledgement contract; retain all UI geometry and mobile acceptance requirements.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/11/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

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
