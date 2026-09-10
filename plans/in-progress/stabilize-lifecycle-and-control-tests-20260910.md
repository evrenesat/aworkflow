# Stabilize isolated Git lifecycle and control readiness tests

## Summary

CI34493456633 at24ab54e failed two Linux/Python3.12 jobs while equivalent matrix jobs passed: a temporary Git repo could not be removed because .git/objects became nonempty during cleanup, and a RunDashboard test never saw its mocked control rejection. Preserve meaningful assertions while isolating background Git maintenance and waiting for actual admitted control readiness. This is a narrow test reliability repair, not a production behavior rewrite.

## Git Tracking

- Plan Branch: `aflow-stabilize-lifecycle-and-control-tests-20260910-20260910-152132`
- Pre-Handoff Base HEAD: `24ab54eb707e487254c18c35b0b42c939757b43c`

## Review Log

- 2026-09-10: Approved Checkpoint 1 through `cp1 v01` on the recorded Plan Branch.
  Reviewed the immediately preceding worker's current worktree diff against
  `24ab54e`; no checkpoint commit boundary existed for this plan. The supplied
  worker result artifact was unavailable at its specified worktree-relative path.
  No material findings: repository-local maintenance settings cover the three
  shared synthetic builders without changing production code or assertions.
  Independent clean-Git-config verification: runtime 289 tests/43 subtests;
  harnesses 78 tests/9 subtests; traced targeted recovery 1 test passed with zero
  maintenance/auto-GC invocation attempts and two actual fixture-config Git
  invocations (`/tmp/aflow-cp1-review-git-process.trace`). Diff check passed.
  Checkpoint 2 remains pending; publication, exact-SHA CI and live activation
  remain coordinator-owned delivery gates.

## Done Means

Synthetic lifecycle repos do not launch automatic Git maintenance that can outlive their scope. The rejected-control test interacts only after the real control is enabled and baseline loaded, then proves rejection preserves draft/status. Full relevant suites and CI pass without skips, retries inside tests, arbitrary sleeps, or weakened assertions.

## Critical Invariants

Only test-owned repositories/configuration may change. Never alter real global Git settings, ignore cleanup failures, or delete external artifacts. Keep real CLI/Git behavior under test and exact control error/draft/status assertions. Preserve concurrent responsive UI work and current component semantics.

## Forbidden Implementations

No production special-case for pytest, global maintenance disable, rmtree(ignore_errors=True), repeated cleanup retries, timeout inflation, or blanket mock of Git/control handlers. Do not force a disabled control's change event as if it were real user input.

## Checkpoints

### [x] Checkpoint 1: Keep synthetic Git maintenance inside fixture ownership

**Goal:** Temporary repository cleanup is deterministic without background maintenance.

**Context:** Read nearest AGENTS; inspect `tests/_support.py` `_make_lifecycle_git_repo` and equivalent repo builders; `tests/test_runtime.py::WorkflowArtifactTests::test_terminal_backup_recovery_uses_original_team_for_merge_teardown`. Evidence `/root/code/evidence/aflow-dogfood-20260909/ci-24ab54e-failed.log`. A targeted strace confirmed multiple `git maintenance run --auto --no-quiet` children in this fixture; trace `git-cleanup-process-trace.log` and test `git-cleanup-targeted.log` are in the same evidence directory. The targeted test passed, so don't claim a deterministic production failure.

**Scope:** Existing synthetic Git fixture builders, directly related tests, DEVLOG. No production runtime edits.

**Steps:**

- [x] Configure automatic maintenance off repository-locally immediately after initializing lifecycle test repos (`maintenance.auto=false`, and `gc.auto=0` where needed to cover supported Git versions). Keep user identity and normal Git operations intact. Apply through the existing shared builder and directly demonstrated equivalent synthetic lifecycle builders, not a global environment hook.
- [x] Verify test-owned configuration and use a process trace or controlled Git invocation observation to confirm the targeted lifecycle no longer launches automatic maintenance. Do not add a test that merely repeats assignment literals; prove the command/lifetime effect. Retain actual workflow/recovery/merge assertions.
- [x] Run the focused lifecycle tests under clean user Git configuration, then the full runtime suite. Record the observed CI cleanup failure, isolated fixture change, and evidence in DEVLOG; no production architecture change is claimed.

**Dependencies:** None.

**Verification:** `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 uv run pytest -q tests/test_runtime.py --tb=short`; scoped fixture tests if added; `git diff --check`. Use available strace for one targeted invocation and report maintenance-child absence, not entire raw trace.

**Done When:** Tests retain their assertions and synthetic repo maintenance no longer escapes their lifetime. Report scoped diff/status.

**Blockers:** If evidence reveals another process writes after fixture exit, identify and fix its test ownership in scope; do not suppress cleanup errors or claim maintenance configuration proved an unrelated process fixed.

### [ ] Checkpoint 2: Wait for admitted controls before testing rejection

**Goal:** The test models an available user action and deterministically proves error handling.

**Context:** Inspect `apps/aflow_app/web/AGENTS.md`, `UI_GUIDELINES.md`, `RunDashboard.test.tsx` test "keeps a rejected control draft and run status while showing the server error", adjacent admitted-selector test, and RunDashboard.tsx control disabled/readiness/baseline initialization. The first failing test waits only for label existence, then fireEvent can dispatch change on a not-yet-admitted disabled select. The one CI retry passed core cleanup but failed the adjacent test "restarts a workflow change through owner stop, exact inactive proof, and a lineage-linked successor": waitForPreflightReady returned before the expected successor summary was available. Evidence ci-24ab54e-retry-failed.log is in the same evidence directory.

**Scope:** That test and directly equivalent demonstrated readiness errors in its immediate group; DEVLOG. No responsive layout changes, framework replacement, or production code unless a separately reproduced user-reachable defect is reported for its own plan.

**Steps:**

- [ ] Wait for the control team's actual baseline value and enabled admission before interaction. Change through the existing test interaction mechanism, verify the selected draft, and wait for the Save control to be enabled before clicking. Keep default timeout and no sleeps; element existence alone is insufficient readiness.
- [ ] Assert the mocked control request was sent for the exact run with the selected draft and current revision, then assert the exact error, retained draft and unchanged running status. Use a controlled deferred admission response if needed to prove no test action occurs before readiness; do not duplicate production initialization logic.
- [ ] Repair the demonstrated adjacent successor-restart readiness case too: wait for admitted plan/workflow controls, preserve the selected identities, wait for preflight matching that current selection and the actual enabled successor confirmation/summary, then assert the existing owner-stop, inactive-proof and lineage sequence. Do not treat a prior selection's preflight or absence of a loading label as proof the current draft is ready. Use existing helpers or a small shared test readiness helper tied to observed user state; no arbitrary delays. Check adjacent tests only for the same confirmed readiness interaction. Preserve all existing conflict, selector and request identity assertions. Run the complete web suite/build and add concise DEVLOG verification.

**Dependencies:** CP1 only for sequential ledger; implementation is otherwise independent.

**Verification:** `npm --prefix apps/aflow_app/web test -- --run src/components/RunDashboard.test.tsx`; `npm --prefix apps/aflow_app/web test -- --run`; `npm --prefix apps/aflow_app/web run build`; `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 uv run pytest -q --tb=short`; `git diff --check`.

**Done When:** Full checks pass without test retries or weaker assertions, and the test proves a real admitted action's rejection handling. Preserve responsive work when integrating accepted main.

**Blockers:** A real enabled UI losing user drafts is a production defect to report explicitly, not hide by waiting in tests.

## Behavioral Acceptance Tests

The targeted synthetic lifecycle completes and removes its own directory without surviving automatic maintenance. A control not yet admitted is not used by the test; after admission, its exact rejected request leaves draft and status intact and displays the error.

## Plan-to-Verification Matrix

Git lifetime: targeted process evidence plus runtime suite. Control readiness/error semantics: focused test plus full web suite. Delivery: existing exact-SHA CI matrix and deployment gate.

## Assumptions And Defaults

Automatic Git maintenance is outside the lifecycle test's purpose; disable it only in owned fixtures. Existing disabled-control behavior is authoritative. Keep unrelated caller-directory isolation work in its existing separate plan.
