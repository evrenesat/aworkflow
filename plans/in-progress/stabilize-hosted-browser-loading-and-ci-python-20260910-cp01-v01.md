# Fix Checkpoint 1 verification: use the selected browser for navigation

## Objective

Close the review finding without changing reviewed production behavior. Both navigation tests always launch Chromium, so the claimed 13 WebKit tests do not verify the history journey in WebKit as required.

## Git Tracking

- Plan Branch: `aflow-stabilize-hosted-browser-loading-and-ci-python-202-20260910-194923`
- Fix Base HEAD: `be19c4ffffef80b4a53c8644a6908f8807557315`
- Original Plan: `plans/in-progress/stabilize-hosted-browser-loading-and-ci-python-20260910.md`
- Review: `plans/reviews/latest_review.md`

Non-checkpoint overlay over six existing dirty implementation files. Preserve them and this overlay filename. Reviewer owns approval and the checkpoint commit.

## Scope and constraints

Edit `apps/aflow_app/server/tests/test_run_navigation_browser.py` and the current checkpoint DEVLOG entry. Read root/server AGENTS.md and UI_GUIDELINES.md. Preserve production/component changes, every assertion, exact request identities/revisions/idempotency, confirmation semantics, restored selection, deleted reload, responsive geometry, focus, scroll and navigation checks.

No dependencies, sleeps, retries, forced clicks, skips, weakened assertions, broad test rewrite, other-worktree edits, live service changes, global configuration or shared tool repointing. Keep root CI 3.11/3.12, Dashboard 3.12/3.13 with explicit UV_PYTHON/assertion, both OSes, WebKit designation/artifacts and build ordering. No GitHub posts/comments/messages. Prefix EVERY historical artifact output line with HISTORY: before display. Retain existing causal restore source/output and identify its exact location in the evidence handoff when available.

## Sequential implementation

1. Confirm host, checkout and branch; inspect `git status --short`. Preserve dirty implementation and ignored plans. Locate launch sites using `rg -n 'chromium.launch|AFLOW_TEST_BROWSER|def _browser' apps/aflow_app/server/tests/test_run_navigation_browser.py apps/aflow_app/server/tests/test_responsive_browser.py`.
2. Reuse the existing responsive module `_browser(playwright)` helper at both navigation launch sites, following existing cross-test-module import conventions. It validates AFLOW_TEST_BROWSER, defaults to Chromium, and passes --no-sandbox only to Chromium. No new helper framework is needed.
3. Build browser assets and run both commands below. Record actual selected engines and counts. Preserve any failure and diagnose its observable boundary within the two authorized modules; do not dismiss a failure with a passing retry. Do not broaden production edits absent controlled evidence and original-plan authorization.
4. Correct DEVLOG: distinguish the previous mixed-engine invocation from newly verified actual WebKit execution. Preserve previous failures/corrections and causal evidence. Reuse unrelated passing core/frontend evidence.
5. Leave checkpoint approval to the reviewer. Preserve original plan and stable overlay filename; do not merge/publish or modify runtime lifecycle state.

## Verification commands

- `npm --prefix apps/aflow_app/web run build`
- `AFLOW_TEST_BROWSER=chromium uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py apps/aflow_app/server/tests/test_run_navigation_browser.py`
- `AFLOW_TEST_BROWSER=webkit uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py apps/aflow_app/server/tests/test_run_navigation_browser.py`
- `git diff --check`

Expected: all 13 selected tests execute without skips in each actual engine, including the held restore response, explicit delete and deleted-record reload. Record real results if counts change for a justified reason. Do not rerun unrelated core/full frontend suites for this browser-test-only correction. Existing workflow validation remains applicable because this overlay does not alter CI.

## Acceptance and delivery

Actual Chromium and WebKit coverage, truthful evidence, unchanged exact behavioral assertions and no additional production scope. Coordinator owns integration preserving responsive/clipboard/revision guards, changelog assets and lifecycle history, followed by origin/main publication, exact-SHA CI and live desktop/mobile verification.
