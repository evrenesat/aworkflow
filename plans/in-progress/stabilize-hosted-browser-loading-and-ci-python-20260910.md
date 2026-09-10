# Stabilize hosted browser loading and truthful CI Python selection

## Summary

Current mainbe19c4f failed exact CI34521042508 in Dashboard macOS: phone live controls inspected disabled state before admission, and archive→restore→delete clicked Confirm delete before the restore transition settled. The log also proves a job labelled Python3.11 ran its server environment with Python3.14; the server manifest requires>=3.12. Correct these concrete browser and CI verification boundaries without weakening product assertions.

## Git Tracking

- Plan Branch: `aflow-stabilize-hosted-browser-loading-and-ci-python-202-20260910-194923`
- Pre-Handoff Base HEAD: `be19c4ffffef80b4a53c8644a6908f8807557315`

## Done Means

Hosted browser journeys establish actual loaded/admitted/acknowledged state before assertions and subsequent actions; deterministic delayed responses cannot erase confirmation unnoticed. The Dashboard Python label, installed interpreter and test interpreter agree and satisfy its manifest. Current exact request, history and responsive contracts remain covered.

## Critical Invariants

Preserve control revisions and the accepted same-run lower-revision guard, history source/revision/idempotency, archive/restore/delete semantics, explicit confirmation, draft/focus/scroll retention and responsive geometry. Keep Linux/macOS coverage. Root CLI Python3.11/3.12 support remains unchanged; server jobs use supported3.12/3.13. Preserve concurrent changelog and completed lifecycle bookkeeping.

## Forbidden Implementations

No blanket timeout/sleep increase, retry-until-green, forced clicks, weakened assertions, skipping browser engines, serial-only workaround, automatic confirmation, silently changing server minimum Python, broad UI/state refactor, new scheduler or service. No GitHub posts/comments/messages, global config/shared tool repointing, other-worktree edits or force push.

## Checkpoints

### [x] Checkpoint 1: Make hosted actions and CI interpreter selection deterministic

**Goal:** The two observed browser failures and misleading interpreter selection are corrected at their real boundaries.

**Context:** Read nearest AGENTS and UI_GUIDELINES. Inspect apps/aflow_app/server/tests/test_responsive_browser.py and test_run_navigation_browser.py, especially test_responsive_live_controls_and_restart control admission and test_run_navigation_scroll_selection_and_history final archive/restore/delete sequence. Inspect RunDashboard.tsx mutateHistory/busyAction/historyConfirm and rendered control state. Inspect .github/workflows/ci.yml, root and server pyproject.toml. Read bounded failure sections of /root/code/evidence/aflow-dogfood-20260909/ci-be19c4f-failed.log.

**Scope:** The two server browser test modules, CI workflow, DEVLOG. A minimal production confirmation-ownership/busy-state correction in RunDashboard.tsx and focused component regression is permitted only if controlled delayed-response evidence proves a user-reachable defect. No unrelated browser redesign or new feature.

**Steps:**

- [x] Replace immediate enabled/disabled sampling before live-control admission with Playwright's existing retrying expect predicates on the exact controls, preserving the actual enabled requirement and all subsequent payload assertions. Audit the remaining asynchronous loading/mutation boundaries in these two journeys for the same concrete pattern; use exact loaded list/state/value/acknowledgement conditions, not generic networkidle or arbitrary delays. Keep direct geometry assertions after established layout readiness.
- [x] Reproduce the restore→delete confirmation ordering with a controlled held restore response. Determine whether the browser test acts before restore acknowledgement or production permits a later restore completion to erase a newer confirmation. Preserve the UI requirement that destructive actions have explicit confirmation. If only the fixture readiness is wrong, wait for restored state and settled mutation before the next action. If a reachable production race is demonstrated, guard only the responsible busy/confirmation ownership boundary and add a deterministic component regression; do not mask it by only making the test slower. Retain source/output and exact observed ordering.
- [x] Preserve archive disappearance, restore identity, delete confirmation, Deleted record after reload, all history revisions/keys, list position, Back and browser navigation checks. Remove existing fixed waits only where replaced by a proven observable readiness condition in the touched journeys; do not turn this into a repository-wide test rewrite.
- [x] Keep root core matrix3.11/3.12. Change Dashboard matrix to supported3.12/3.13, and set its UV_PYTHON from matrix.python-version so server uv sync and every uv run use that exact interpreter. Add a compact executed interpreter assertion against the declared matrix version before server tests; require mismatch to fail, not silently download another Python. Preserve both OSes, existing Node/build ordering, Chromium and designated Ubuntu3.12 WebKit artifacts. Inspect other existing CI server-environment invocations and pin their declared supported Python consistently; no dependency upgrades or manifest minimum change.
- [x] Run focused hosted tests and deterministic comparison, both browser engines for the two changed modules, and workflow syntax/matrix validation. If production/component code changes, also run focused and full web tests/build; otherwise reuse current passing frontend evidence and build only for browser assets. Update concise DEVLOG and CI runtime documentation if applicable. Record every failure and correction without passing-retry dismissal.

**Dependencies:** Current main includes responsive, clipboard, readiness58e67cf and lifecycle bookkeepingbe19c4f. This is delivery priority; changelog may continue independently but must retain these fixes at integration.

**Verification:**

- `npm --prefix apps/aflow_app/web run build`
- `uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py apps/aflow_app/server/tests/test_run_navigation_browser.py`
- `AFLOW_TEST_BROWSER=webkit uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py apps/aflow_app/server/tests/test_run_navigation_browser.py`
- If production/tests in web change: `npm --prefix apps/aflow_app/web test -- --run src/components/RunDashboard.test.tsx` and `npm --prefix apps/aflow_app/web test -- --run`.
- `actionlint .github/workflows/ci.yml` if available; parse matrix with existing YAML tools and assert root3.11/3.12 versus Dashboard3.12/3.13, both OSes, explicit interpreter selection and assertion. Preserve actual GitHub CI as separate evidence.
- `git diff --check`

Expected: retained controlled delayed restore evidence, both engines execute all selected journeys without skips, exact semantics/geometry retained, and runtime cannot differ from declared supported matrix. No unrelated core suite rerun for browser/CI-only changes.

**Done When:** Both browser failures have causal corrections and Dashboard CI truthfully selects a supported interpreter. Reviewer approval is separate from coordinator exact-SHA CI and real live browser verification.

## Behavioral Acceptance Tests

Delayed admission cannot fail an eventual enabled-control assertion. Delayed restore cannot lead to an unconfirmed destructive action or erase a later valid confirmation unnoticed. Restored record is the exact source selected for explicit delete; Deleted record survives reload. A Dashboard job labelled3.12 runs3.12 and labelled3.13 runs3.13, with incompatible selection failing clearly.

## Plan-to-Verification Matrix

Admission/history: controlled browser evidence and Chromium/WebKit journeys; component regression only for demonstrated production defect. CI fidelity: workflow validation plus executed interpreter assertion. Delivery: exact-SHA CI and live UI checked by coordinator.

## Assumptions And Defaults

Server manifest>=3.12 is authoritative; retain two supported Dashboard minor versions3.12/3.13 and the existing root CLI3.11/3.12 matrix. Current owner grant authorizes reviewed origin/main publication and existing private CI-gated deployment without another confirmation. Keep previous live release while gate fails; no deploy bypass.

## Additional exact CI evidence — 2026-09-10 20:08 UTC

Changelog main884b899 includes the prior fixes but exact CI34523507986 failed in Ubuntu3.12 WebKit test_responsive_route_matrix[desktop]: after workflow selection and clicking Advanced options, filling Run extra instructions timed out because the locator was absent. Log /root/code/evidence/aflow-dogfood-20260909/ci-884b899-failed.log around lines1243–1247 records it. This same-module loaded/selection/disclosure boundary is now explicitly included in the systematic audit. Preserve correct workflow selection and open disclosure ownership through asynchronous defaults/preflight loading. Establish the exact cause with controlled readiness evidence; no timeout inflation or passing-retry dismissal. A minimal directly demonstrated NewRunPage/RunDashboard disclosure-state correction is permitted if the product, rather than fixture ordering, is faulty. Preserve all accepted changelog UI/build assets at subsequent integration; no changelog redesign.

## Review Log

- 2026-09-10: Checkpoint 1 initial attempt reviewed using current-worktree fallback at base `be19c4f`; not approved. Navigation hard-codes Chromium, so claimed WebKit coverage does not satisfy both-module verification. Focused overlay: `stabilize-hosted-browser-loading-and-ci-python-20260910-cp01-v01.md`. Reviewer component check: 85 passed; diff whitespace check passed. Preserve implementation and original base; delivery gates remain coordinator-owned.

- 2026-09-10: Approved Checkpoint 1 through `cp1 v02` using current-worktree fallback from unchanged base `be19c4f`. Overlay v01 fixes both navigation engine selectors. Reviewer build, Chromium 13/13, WebKit 13/13, YAML matrix/interpreter/build-order/artifact validation and diff check passed; prior focused 85/full web 316 evidence reused. Causal source/output retained in `plans/reviews/hosted-browser-cp1-ordering.txt`. Stable overlay retained. Checkpoint approval is local; coordinator integration, origin/main publication, exact-SHA CI and live desktop/mobile verification remain pending.
