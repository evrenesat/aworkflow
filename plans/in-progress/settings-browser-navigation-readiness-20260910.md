# Settings browser navigation readiness

## Summary

Repair the two observed browser-test readiness failures blocking delivery of accepted main fdc9ff2. CI 34536906819 failed on Ubuntu Python 3.12/3.13 waiting for a Skills desktop tab at a phone viewport, and on macOS Python 3.13 asserting Save before its header registration effect settled. Retain all behavior assertions; make the tests wait for the user-visible boundary they actually depend on.

## Git Tracking

- Plan Branch: `aflow-settings-browser-navigation-readiness-20260910-20260910-223306`
- Pre-Handoff Base HEAD: `66da3ab45c7eee1d5155362d296a453e1c8fb65d`

## Done Means

Both failing journeys pass using rendered navigation/actions, including real Chromium and WebKit, without weakening dirty draft, wrapping, geometry, paging, read-only or saved-value assertions. Changes are limited to the settings browser tests and concise DEVLOG evidence unless a concrete production defect is demonstrated and separately scoped.

## Critical Invariants

- Preserve mobile selector versus desktop tab behavior and current UI_GUIDELINES.md.
- Preserve exact draft bytes and all existing geometry/read-only/selection assertions.
- Use isolated test HOME, server ports and artifacts. Never save/install account skills, mutate other worktrees, repoint shared tools or restart live services.
- Primary coordinator owns combined delivery; preserve all accepted main history and concurrent issue37/control-draft changes.

## Forbidden Implementations

No sleeps, increased blanket timeout, retry-until-green, skipped tests, relaxed thresholds, production changes solely to satisfy a test, global test helper framework, CI gate bypass or public issue/comment/message posting.

## Checkpoints

### [x] Checkpoint 1: Await the actual Settings navigation and header actions

**Goal:** Fix the observed race in the existing real-browser journeys.

**Context:**

- Run `git rev-parse --show-toplevel` and read root AGENTS.md, UI_GUIDELINES.md and apps/aflow_app/server/AGENTS.md.
- Inspect `apps/aflow_app/server/tests/test_settings_browser.py`, especially `select_settings_section`, `test_changelog_settings_responsive_journey` and `test_dirty_mobile_skills_presentation_survives_changelog`.
- Inspect the Settings section selector/header registration implementation with `rg -n 'Settings section|Save all changes|registerHeader' apps/aflow_app/web/src`.
- Exact external failure evidence: `/root/code/evidence/aflow-dogfood-20260909/ci-fdc9ff2-failed.log`. Display only focused, bounded excerpts; prefix historical output with HISTORY: to prevent historical stop strings from becoming current signals.

**Scope:** Only `apps/aflow_app/server/tests/test_settings_browser.py` and `DEVLOG.md`. No production behavior or architecture change expected.

**Steps:**

- [x] Establish why the synchronous selector count can be zero immediately after mobile navigation, despite the later correct selector. Record the source boundary and exact observed CI evidence. Do not claim a locally reproduced failure unless it was actually reproduced.
- [x] In the existing `select_settings_section` helper, wait for either the visible named Settings section combobox or requested named tab before choosing the actual rendered control. Use Playwright's retrying visibility assertion on the union or an equivalently bounded semantic wait; do not choose the desktop branch merely from a pre-render zero count. Preserve accessible-role/name navigation.
- [x] Replace the immediate positive Save count assertion after Advanced TOML with a retrying visible/count assertion at the actual header action. Inspect immediately adjacent changed-journey assertions and wait for the actual completed paging/action state only where they depend on asynchronous render; retain exact expected counts and bytes.
- [x] Build the current web bundle, run the complete settings browser test file under Chromium and WebKit, and retain results. Note explicitly which tests use the selected engine and which existing tests remain hard-coded Chromium. No misleading claim that every test used WebKit.
- [x] Add a concise DEVLOG entry with failure cause, minimal corrections and exact verification; README/ARCHITECTURE need no change for test synchronization only. Mark progress only after verified results.

**Dependencies:** Accepted current main; independent of ongoing control-draft unit-test and issue37 parser/API work. Delivery repair has priority.

**Verification:**

- Run `cd apps/aflow_app/web && npm ci && npm run build`.
- Run `cd apps/aflow_app/server && uv sync --frozen --group dev`.
- Run `cd apps/aflow_app/server && AFLOW_TEST_BROWSER=chromium uv run pytest -q tests/test_settings_browser.py`.
- Run `cd apps/aflow_app/server && AFLOW_TEST_BROWSER=webkit uv run pytest -q tests/test_settings_browser.py`.
- Observe both previously failed journeys passing, unchanged layout/draft/read-only expectations, actual selected engines for engine-aware tests.
- Run `git status --short`, `git diff --name-only`, `git diff --stat` before handoff.

**Done When:** The scoped checks pass and exact evidence supports both fixes without retries masking unchanged failure.

**Blockers:** Report an actual production defect or unrelated dirty ownership rather than silently broadening scope.

## Behavioral Acceptance Tests

- Given a newly opened phone Settings view, selecting Skills awaits the mobile selector and opens the editor, then exact unsaved bytes/wrap/selection survive Changelog and Back.
- Given Changelog then Advanced TOML, the Save action eventually exists and is visible; returning to Changelog removes it. Exact read-only assertions remain.
- Paging exposes exactly 40 entries and preserves editor presentation on return.

## Plan-to-Verification Matrix

- Mobile initial navigation and dirty retention: `test_dirty_mobile_skills_presentation_survives_changelog`, Chromium and WebKit.
- Header action registration and read-only return: `test_changelog_settings_responsive_journey`, Chromium and WebKit.
- Shared helper compatibility: complete settings browser file plus current bundle build.

## Assumptions And Defaults

Use the existing UI and Playwright conventions. This is a bounded delivery-gate repair, not a settings redesign. Cross-platform final proof comes from exact-commit CI after review/publication; local Linux results alone do not prove macOS.

## Review Log

- 2026-09-10: Approved checkpoint 1 as `cp1 v01 aflow-settings-browser-navigation-readiness-20260910-20260910-223306: Await rendered Settings controls`. Reviewed current worktree fallback against `66da3ab45c7eee1d5155362d296a453e1c8fb65d`; no checkpoint commit existed for this run. Worker result artifact was absent at its supplied execution-root-relative path. Independent review found no material findings. Build passed; settings browser file passed once per requested engine (Chromium: 5 passed in 33.38s; WebKit selection: 5 passed in 35.41s). Only Changelog and dirty mobile Skills journeys select the engine; toolbar, draft-template, and skill-save/install tests remain Chromium. Checkpoint approval is local; coordinator publication, exact-commit CI and live verification remain pending.
