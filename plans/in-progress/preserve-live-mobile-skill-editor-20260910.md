# Preserve the live mobile Skills editor

## Summary

Release14464a9 passed exact CI34530541966 and deployed21:16UTC. Actual 390x844 Chromium using real bundled aflow-plan found two gaps: after an unsaved newline the editor top is316.56 rather than<=280, and Skills→Changelog→Skills returns to the list instead of the previously open editor. Draft bytes survive. Fix these exact live usability defects while retaining the now-working responsive shell and automatic changelog.

## Git Tracking

- Plan Branch: `aflow-preserve-live-mobile-skill-editor-20260910-20260910-212239`
- Pre-Handoff Base HEAD: `14464a9bb6a44705dbe5560f5af92ad09fb90ad8`

## Done Means

On390x844, an opened dirty bundled skill editor starts byy280 with>=280px initially visible editable height; title/status/Back/wrap controls remain clear and usable. Switching Settings sections returns to the same open Skills editor with exact draft and presentation state. Explicit Back still returns to the list and restores its position. Desktop limits and read-only Changelog remain intact.

## Critical Invariants

GlobalSettings is still the sole skill draft/save owner. No server saves during verification, no new config keys or API. Preserve exact skill bytes, unsaved warnings, conflicts, install availability, read-only Changelog actions, one document scroll owner, keyboard focus and44px touch targets. Long errors and enlarged text flow naturally rather than being hidden to satisfy geometry.

## Forbidden Implementations

No font shrinking, clipping labels/errors, fixed body scroll, removing unsaved state, duplicate save/draft state, unconditional detail opening that breaks first entry or explicit Back, or broad Settings rewrite. No public posts/messages, real global config/skill saves/install, other-worktree changes, service/shared-tool edits or force push.

## Checkpoints

### [x] Checkpoint 1: Retain Skills presentation and compact its detail heading

**Goal:** The actual live phone editing journey meets the approved layout and state requirements.

**Context:** Read nearest AGENTS and UI_GUIDELINES. Inspect GlobalSettings.tsx Skills conditional branch, SkillsSettings.tsx skill-editor-header/title/control and local navigationVersion, SidebarEditorLayout.tsx local detailOpen/restore/focus state, styles.css skill/header/compact rules and existing component/browser tests. External live evidence: /root/code/evidence/aflow-dogfood-20260909/live-14464a9-chromium-390-dark-skills.png and corresponding -changelog.png. The task-local verify_live_release.py reproduces the journey without saving.

**Scope:** Those four UI modules/styles and focused existing component/browser coverage; DEVLOG and concise architecture notes if needed. No other page redesign.

**Steps:**

- [x] Preserve SkillsSettings/its SidebarEditorLayout presentation across Settings tab changes by retaining the existing component instance in a correctly hidden wrapper rather than creating another draft owner or deriving open-detail from selected!=empty. Keep initial phone entry at the list; an explicitly opened detail remains open across Changelog/General roundtrips, and an explicitly closed detail stays at the list. Hidden content must not be reachable by keyboard/accessibility or steal document scroll/focus. If needed add a small active/visible presentation prop to suppress hidden focus/scroll effects; no new navigation framework.
- [x] Compact the phone detail heading by placing Back alongside the selected skill title/status, instead of giving Back a full separate row. Use a small optional shared-layout heading slot and normal flex/grid layout if needed; other layout consumers keep existing behavior. Visible Back text may be concise, but retain its full existing accessible name and44px target. Keep status prose wrapping and the SKILL.md/wrap control readable. Preserve the desktop title/control arrangement. Do not hide meaningful status or errors merely to meet the limit.
- [x] Reproduce with a real-shaped bundled default aflow-plan summary, installed=false, a dirty long Markdown draft, normal unsaved header state and all current Settings sections including Changelog. At390x844 verify textarea top<=280 and initial visible height>=280; at1440x900 and1280x720 retain desktop header<=112/content<=128/editor<=208. Include320x568/768x1024/844x390/390x420 and text enlargement with natural error flow, no horizontal overflow and reachable final controls.
- [x] Add focused regression for open detail→edit→Changelog→Skills preserving exact bytes, open view and wrap preference; then Back→Changelog→Skills preserves list/selection/restore point. Assert hidden Skills cannot receive focus or move the document when another tab is active. Keep Changelog read-only/paging and General draft ownership intact. Prefer existing browser fixtures and exact readiness predicates, not sleeps.
- [x] Run full web/build and relevant real Chromium/WebKit Settings/changelog/responsive journeys once after changes, inspect fresh light/dark screenshots with dirty real-shaped data, and record concise DEVLOG evidence. Physical keyboard remains unverified. Do not mutate actual live settings or installed skills; coordinator repeats the read/unsaved-draft smoke after deployment.

**Dependencies:** Live responsive/changelog release14464a9. Independent of parent/worktree presentation work; preserve both histories during serialized integration.

**Verification:**

- `npm --prefix apps/aflow_app/web test -- --run`
- `npm --prefix apps/aflow_app/web run build`
- `uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_settings_browser.py apps/aflow_app/server/tests/test_responsive_browser.py -k 'settings or responsive or changelog'`
- Same command with `AFLOW_TEST_BROWSER=webkit`.
- `git diff --check`

Expected: no skips or false zero collection, actual dirty mobile geometry and retained editor/list states, all existing exact semantic assertions remain. Reuse unchanged backend results; no unrelated core suite.

**Done When:** Both live defects have passing regression/screenshot evidence. Coordinator must verify exact CI, activation and the actual live unsaved-draft journey before claiming finished.

## Behavioral Acceptance Tests

Open aflow-plan on phone, append newline without saving, see useful editor byy280, visit Changelog and page20→40, return directly to the same editor/draft, then explicitly Back and verify later tab roundtrip stays at the list. No hidden panel focus or document jump. Desktop and short-screen controls remain usable.

## Plan-to-Verification Matrix

Geometry: real-shaped dirty fixtures plus Chromium/WebKit seven viewports and screenshots. State: component/browser tab and Back roundtrips. Save boundary: unchanged single GlobalSettings owner and no live writes. Delivery: coordinator exact CI/live smoke.

## Assumptions And Defaults

This is a bounded continuation of approved responsive work; use Terra High with Astra Medium review. Retain shared familiar visual style and minimal APIs. Standing grant permits reviewed origin/main publication and existing CI-gated private deployment without another confirmation.

## Review Log

- 2026-09-10: Checkpoint 1 approved through `cp1 v01` on the recorded Plan Branch. Reviewed current worktree against the unchanged Pre-Handoff Base HEAD because no checkpoint commit existed for this attempt. Zero material findings; reviewer creates the checkpoint approval commit. Web build, 325 web tests on rerun, and both required browser invocations (16 each) passed. Initial web run had one non-repeating RunDashboard draft-refresh failure; retained in the review artifact. See `plans/reviews/latest_review.md` for evidence and limitations. Local checkpoint review is complete; keep this plan in progress pending coordinator publication, exact-SHA CI, activation and actual live dirty-draft verification.
