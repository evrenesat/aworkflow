# Repair combined responsive integration acceptance

## Scope and Git Tracking

- Original plan: `plans/in-progress/integrate-reviewed-responsive-ui-20260910.md`.
- Failed review: integration CP1 v01, current-worktree fallback; this is a non-checkpoint repair overlay.
- Plan Branch: `aflow-integrate-reviewed-responsive-ui-20260910-20260910-161156`.
- Repair Base HEAD: `52597409b17c3e1050f1576bfe027580b5656d50`.
- Recovery snapshot HEAD: `5b52d3d37095014c92dfb6791685a3f0aaa2a9fc`, preserving parents `52597409b17c3e1050f1576bfe027580b5656d50` and `61d5988f98584ffa50ce0a6e02a0f5404a423276`.
- `.git/MERGE_HEAD` is intentionally absent under the original-plan recovery amendment; the snapshot preserves both histories without an active merge state.
- Work only in this workflow-managed integration root. Preserve the recovery snapshot, both source histories and the unstaged repair overlay; do not commit, abort, reset, rebase, or touch another worktree. The reviewer owns approval and any later merge/publication step.

## Objective

Close the observed Chromium readiness failure and supply the original CP1 requirement for real-browser live control and restart/successor acceptance. This is a bounded verification repair, not a responsive redesign or a reopening of the original six approved checkpoints.

## Evidence and required outcomes

- HISTORY: Current reviewer Chromium run: 12 passed, one failed at `apps/aflow_app/server/tests/test_responsive_browser.py:322`, `test_responsive_route_matrix[tablet]`: plan count was zero after waiting only for New plan filename. Log: `/tmp/integration-review-chromium.log`. `PlanPanel.tsx` initializes an empty list, loads it asynchronously, and renders the filename header independently. Wait for loaded fixture content before the existing count/geometry checks; preserve their meaning.
- HISTORY: The failing assertion is unchanged from approved source `61d5988`; this is a real combined acceptance failure, not evidence of a newly introduced product defect. A passing retry alone cannot close it.
- HISTORY: The browser fixtures seed completed runs (`test_responsive_browser.py:43-51`); current journeys inspect detail and ordinary launch/preflight, but never operate live max-turn/team/selector controls or restart/successor actions. Original CP1's checked acceptance step is therefore unsupported. Component tests do not establish action reachability within the hosted shell.
- HISTORY: Existing local `origin/main` reference includes accepted clipboard repair `c14f1f2` and completion `cd78d53`. Preserve it at subsequent coordinator/reviewer delivery; do not duplicate that repair or modify its separate execution root. Do not claim this snapshot already includes that repair until the coordinator reconciles accepted history.

## Ordered repair tasks

1. Read root/web/server AGENTS and UI_GUIDELINES; verify hostname, working directory, recovery-snapshot HEAD, parent history, intentional MERGE_HEAD absence, and staged/unstaged scope. Preserve the original plan's pre-handoff base and all other plan histories.
2. In `apps/aflow_app/server/tests/test_responsive_browser.py`, replace filename-input readiness as the prerequisite for the plan-count assertion with a wait for the expected fixture plan rows or an equivalent exact loaded-list condition. Keep the count >=40 and all document/geometry/action checks. Verify the wait covers a delayed plan-list response; use deterministic response control if needed, no fixed sleeps or timeout inflation. Do not change production PlanPanel merely to satisfy the test.
3. Extend this browser module, reusing `test_control_plane_api.control_client` and `live_server`, with a disposable current-run fixture that advertises real supported live controls and restart capabilities. Reuse server-shaped response contracts and existing `RunDashboard.test.tsx` request examples. Intercept mutations so no provider, production controller, or external service starts. Keep fixtures, ports, HOME/state, and screenshots isolated.
4. Through the actual App shell, open the exact run, change live max turns, team and role selector, and assert the exact project/run/revision and submitted values. Include a rejected save with visible error and retained draft. Navigate list/detail/Back, away and back, and resize between desktop and compact presentation while retaining those values. Verify the final action is scroll-reachable and hit-testable and no extra toolbar or nested document scroller appears.
5. Through Restart with changes, exercise exact source stop, confirmed inactivity, and distinct successor submission. Verify request source identity, plan/workflow/team/selector choices as supported by the existing contract, and dirty-preflight acknowledgement. Include an unresolved successor outcome and exact retry with unchanged identity/body rather than starting a replacement. Assert disabled/busy states and retained pending state across navigation. Use loaded/admitted state waits, never guessed time delays.
6. Run the new journeys in both Chromium and WebKit at 1280x720, 390x844 and 844x390, preserving the existing full seven-viewport matrix. Inspect fresh light/dark screenshots of opened live controls and restart/successor state as well as existing Skills/profile/Plans/New Run surfaces. Report physical keyboard/browser-toolbar checks as unverified without real hardware.
7. Run the commands below. Record every failure and its cause; do not dismiss failures through retries, skips, or relaxed assertions. Make only a minimal demonstrated integration correction if acceptance exposes a product defect. Add a concise DEVLOG repair note and update this overlay's evidence; leave original CP1 approval to the reviewer, retaining the recovery snapshot.

## Verification commands

```sh
npm --prefix apps/aflow_app/web test -- --run
npm --prefix apps/aflow_app/web run build
GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 uv run pytest -q tests apps/aflow_app/server/tests --tb=short
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_settings_browser.py apps/aflow_app/server/tests/test_run_navigation_browser.py apps/aflow_app/server/tests/test_responsive_browser.py
AFLOW_TEST_BROWSER=webkit uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py
uv run ruff check aflow apps/aflow_app/server/src
git diff --check
git diff --cached --check
git rev-parse HEAD
test ! -e "$(git rev-parse --git-path MERGE_HEAD)"
git ls-files -u
```

Both browser engines launched successfully during review; if prerequisites are missing later, install them with the original plan's Playwright command and report failures explicitly. Expected: all required tests pass without skips, meaningful loaded-state screenshots, no unresolved index entries, recovery-snapshot HEAD and parent history preserved, and MERGE_HEAD absent by design for reviewer approval. Existing CI/publication/live activation remain separate downstream gates. No settings, services, shared tool installation, GitHub issues/comments, or messages to others.

## HISTORY: Repair evidence (2026-09-10)

- HISTORY: The plan-row readiness repair waits for at least 40 rendered fixture rows including `long-plan-39.md`; the existing count, geometry, document-scroll and action checks remain unchanged.
- HISTORY: The hosted Chromium journey now creates one real control-plane run with the disposable unit manager, advertises the repository's admitted `running` metadata, and intercepts browser mutations. It asserts exact live-control project/run/revision/payload values, rejected-save error with retained draft, compact list/detail/Back, away/back and resize retention, hit testing, owner stop, source inactivity, dirty preflight, unresolved successor and unchanged exact retry identity/body.
- HISTORY: Full responsive Chromium passed 11 tests, and responsive WebKit passed 11 tests. The combined Chromium settings/navigation/responsive command passed 16 tests. The existing seven-viewport matrix remains in the responsive module.
- HISTORY: `npm --prefix apps/aflow_app/web test -- --run` first exposed one parallel Vitest worker-order failure in the existing `RunDashboard.test.tsx` successor text assertion (308/309 passed). The focused test, full RunDashboard file, exact suite rerun (309/309), and serial `--no-file-parallelism --maxWorkers=1` diagnostic (309/309) passed; no skip, timeout change, assertion relaxation or production correction was used. Keep the parallel-suite scheduling anomaly visible to reviewers.
- HISTORY: The production web build passed; the full backend command passed 2046 tests and 223 subtests; Ruff passed. Physical keyboard and browser-toolbar behavior remain unverified without real hardware. No commit was created; the earlier pending-merge state is superseded by the authorized recovery snapshot recorded below.

## HISTORY: Recovery snapshot and focused acceptance follow-up (2026-09-10)

HISTORY: The original-plan UI revision authorizes recovery snapshot `5b52d3d37095014c92dfb6791685a3f0aaa2a9fc` after capacity failure and pending-merge resume refusal; its parents preserve accepted base `5259740` and responsive source `61d5988`.
HISTORY: `.git/MERGE_HEAD` is absent by design. No reset, abort, rebase, force push, checkpoint commit, or second merge is authorized by this overlay; the unstaged repair bytes remain under reviewer control.
HISTORY: The focused acceptance repair makes the disposable hosted fixture wait for and select raw `fast__team` while retaining the visible `No team` option and colliding `fast_team` label, and makes the successor confirmation checks use the rendered confirmation container and exact enabled button.
HISTORY: The first post-edit full backend invocation exposed a real native-option readiness defect in all three new live-control viewports: Playwright's default visible-state wait cannot observe an attached `<option>`, and the run recorded `2043 passed`, `3 failed`, `223 subtests`. The correction changed only those waits to exact `state="attached"`; no delay, timeout, skip or assertion weakening was used.
HISTORY: After that correction, the combined Chromium browser command passed 16 tests, responsive WebKit passed 11 tests, the full backend command passed `2046`, `223 subtests`, the web suite passed `309`, the production build passed, and Ruff passed.
HISTORY: Physical keyboard and browser-toolbar behavior remain unverified without real hardware. Clipboard history `c14f1f2`/`cd78d53` remains separate and must be preserved by coordinator-owned delivery; no publication, CI receipt or live activation is claimed.
