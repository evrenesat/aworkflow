# Responsive document scrolling and full mobile editing

## Summary

Restore usable task space across AFlow on phones, tablets, short landscape windows, and desktop. Use document scrolling, compact controls, and list → detail navigation on narrow/short screens. Preserve full editing and run-control functionality. The owner explicitly selected full mobile functionality and normal page scrolling with a Back action.

Baseline on live `9d11b373f8820209fa3c827002840267696caf6c`: Skills detail has zero height at390×844 and844×390; at1280×720 its layout begins at y523 and is181px tall. Phone Prompts detail is70px tall. The root document is locked, workspace overflow is hidden, and nested pane/textarea scrollers compete. Existing browser tests enforce those panes instead of testing usable task space.

This plan supersedes the scrolling/layout directions in `clear-run-and-settings-ui-20260909.md`, particularly its owning profile scroller, mobile horizontal tab strip, and independent-pane acceptance. Do not duplicate already-delivered compact controls. That plan's run naming/history ordering/progress/launch-summary semantics remain separate work; this plan does not mark them complete. Reconcile active overlapping changes before implementation; do not interrupt other workflows.

## Git Tracking

- Plan Branch: `aflow-responsive-document-scroll-and-mobile-editing-2026-20260910-110307`
- Pre-Handoff Base HEAD: `ee1757579b216afe9c02f3ca763038cae59a8498`
- Last Approved Checkpoint: `cp5 v02`

### Review Log

- 2026-09-10: Approved `cp1 v01` against CP1's original scope using the latest
  worker's nine-file worktree diff from `ee17575`. No checkpoint commit existed;
  current-worktree fallback was used. The checked CP1 heading was worker
  completion, not prior approval or authority to review CP2. No material
  findings. Reviewer verification: 279 web tests, production build, both
  Chromium modules (4 passed), and `git diff --check`. CP2–6 remain unchecked.
  Publication, exact-SHA CI and live proof remain pending workflow delivery.

- 2026-09-10: Rejected CP2 attempt v01 using the current-worktree fallback
  from approved `cp1 v01` (`4bf09ae`); no CP2 commit exists. The immediately
  preceding worker diff is CP2, despite the supplied cp03 fix filename. P2:
  passive default-run URL synchronization is treated as explicit detail entry,
  hiding history on an ordinary compact Runs visit. Confirmed in disposable
  Chromium at 390×844 without a run URL parameter. 174 targeted web tests,
  production build, existing four browser checks and diff check passed; the
  additional first-entry reproduction failed. CP2 remains unapproved, CP3–6
  remain unchecked. Repair overlay: `responsive-document-scroll-and-mobile-editing-20260910-cp03-v01.md`.
  No approval commit, publication, CI or live deployment proof in this review.

- 2026-09-10: Rejected the subsequent uncommitted CP2 repair attempt against
  original CP2, using current-worktree fallback from cp1 v01 (`4bf09ae`). The
  passive-default entry defect is repaired, but explicit selection from All
  runs now leaves compact detail hidden because that callback never supplies
  the new App navigation intent. Confirmed in disposable Chromium at 390×844.
  Build, four existing Chromium tests and diff check passed; targeted web suite
  initially passed 176/177, with the existing restart test passing in a rerun
  of all 67 RunDashboard tests. CP2 remains unapproved; CP3–6 remain unchecked.
  Replacement non-checkpoint repair overlay:
  `responsive-document-scroll-and-mobile-editing-20260910-cp02-v01.md`.
  Superseded cp03 overlay removed. No approval commit or history rewrite;
  publication, exact-SHA CI and live proof remain pending workflow delivery.

- 2026-09-10: Rejected CP2 repair overlay cp02-v01 against original CP2 using
  current-worktree fallback from cp1 v01 (`4bf09ae`); no CP2 commit exists.
  The All runs callback still lacks explicit-entry intent and the requested
  All runs regression tests are absent. Fresh disposable Chromium at 390×844
  again reached the exact run URL with detail hidden. The 177 targeted web tests
  and production build passed. CP2 stays unapproved; CP3–6 remain unchecked.
  Superseded cp02-v01 with non-checkpoint repair overlay
  `responsive-document-scroll-and-mobile-editing-20260910-cp02-v02.md`.
  No approval commit, history rewrite, publication, CI or live proof.

- 2026-09-10: Approved `cp2 v02`, reviewing active cp02-v02 repair and the
  accumulated CP2 work against original CP2. No CP2 commit existed; used the
  current-worktree fallback from approved cp1 v01 (`4bf09ae`). Explicit All
  runs and launch entry now supply App navigation intent; passive default
  selection remains list-first. No material findings. Reviewer verification:
  178 targeted web tests, production build, four disposable Chromium tests
  (including fresh/repeat All runs and Back focus/scroll), and diff check passed.
  Reviewer owns the cp2 v02 approval commit. Only CP2 advances; CP3–6 remain
  unchecked. Publication, exact-SHA CI and live proof remain pending normal
  workflow delivery; broader task-space budgets and WebKit belong to later CPs.

- 2026-09-10: Rejected the immediately preceding uncommitted CP3 attempt,
  using current-worktree fallback from approved cp2 v02 (`c3cee36`); no CP3
  commit exists. The worker-checked CP3 heading was completion intent, not
  approval or authority to review CP4. Four high-confidence P2 findings:
  Settings tabs wrap at 960px (header bottom y158); Advanced TOML → Install
  skills leaves installation hidden; Hide cannot dismiss retained installation
  outcomes; the Runs browser test still targets old compact navigation.
  Verification: 97 targeted web tests, build, three Settings Chromium tests and
  diff check passed. Runs Chromium failed at its obsolete All runs selector;
  a disposable browser probe confirmed all three UI findings. CP3 remains
  unapproved; CP4–6 remain unchecked. Focused non-checkpoint repair overlay:
  `responsive-document-scroll-and-mobile-editing-20260910-cp04-v01.md` (CP3 scope).
  No approval commit/history rewrite, publication, CI or live activation proof.

- 2026-09-10: Rejected the immediately preceding uncommitted CP3 repair
  (active cp04-v01 overlay), using current-worktree fallback from approved
  cp2 v02 (`c3cee36`); no CP3 commit exists. Prior four findings are repaired.
  One high-confidence P2 remains: hosted successor-recovery test still seeks
  the removed Cancel button at RunDashboard.test.tsx:689, failing before exact
  request-identity assertions. Full suite: 294 passed, 1 failed; isolated rerun
  confirms it. Targeted100 tests, build, four disposable Chromium tests, extra
  six-page geometry at1280/1440 (header112/content124) and diff check passed.
  CP3 remains unapproved; CP4–6 unchecked. Replaced cp04-v01 with focused
  non-checkpoint cp03-v01 repair. No approval commit/history rewrite,
  publication, exact-SHA CI or live activation proof.

- 2026-09-10: Approved `cp3 v02` against original CP3 and active cp03-v01
  repair overlay. Current-worktree fallback from approved cp2 v02 (`c3cee36`)
  was used because no CP3 commit existed. Reviewed accumulated header slots,
  page contributions, compact navigation, Settings sections, guarded installation
  disclosure and hosted successor recovery. No material findings. Full web suite
  passed295 on repeat; initial run passed294 with one colliding-role control
  test failure, followed by all67 dashboard tests passing. Build, four disposable
  Chromium tests, six-page geometry at1280/1440 (header112/content124), Skills
  editor position/height checks and diff check passed. Reviewer creates cp3 v02
  approval commit; only CP3 advances. CP4–6 remain unchecked. Publication,
  exact-SHA CI and live activation remain pending normal workflow delivery.

- 2026-09-10: Rejected the immediately preceding uncommitted CP4 attempt v01
  against original CP4, using current-worktree fallback from approved cp3 v02
  (`8246325`); no CP4 commit exists. P2: the absolute Wrap lines toolbar covers
  text and intercepts caret clicks after native textarea scrolling, confirmed
  in disposable Chromium with real Skills assets, geometry and screenshot.
  Full web suite297, build, four existing Chromium tests and diff check passed;
  two additional disposable probes completed. CP4 remains unapproved; CP5–6
  unchecked. Focused non-checkpoint repair overlay uses supplied cp05-v01 path.
  No approval commit/history rewrite, publication, exact-SHA CI or live proof.

- 2026-09-10: Approved `cp4 v02` against original CP4 and active cp05-v01
  non-checkpoint repair. Current-worktree fallback from approved cp3 v02
  (`8246325`) was used because no CP4 commit existed. Reviewed accumulated
  native editors, normal-flow wrapping controls, profile presentation, combobox
  placement and exact draft/save behavior. No material findings. Reviewer
  verification: full web suite297, build, four disposable Chromium tests,
  inspected dark scrolled-editor screenshot and diff check passed. Skills
  geometry, deep-scroll hit-testing/caret movement, long Markdown/TOML bytes
  and partial-save retention passed. Reviewer creates cp4 v02 approval commit;
  only CP4 advances. CP5–6 remain unchecked. Publication, exact-SHA CI and live
  proof remain pending normal workflow delivery.

- 2026-09-10: Rejected the immediately preceding uncommitted CP5 attempt v01
  against original CP5 using current-worktree fallback from approved cp4 v02
  (`12c9af1`); no CP5 commit exists. P2: compact launch tables retain native
  table-caption layout, squeezing captions to 59–67px within 246px tables and
  wasting 180px of phone task space. Confirmed with disposable Chromium geometry,
  inspected screenshot and a browser-only CSS correction. Full web suite297,
  build, five Chromium tests, additional Plans/New Run geometry and diff check
  passed. CP5 remains unapproved; CP6 unchecked. Focused non-checkpoint repair:
  `responsive-document-scroll-and-mobile-editing-20260910-cp06-v01.md` (CP5 scope).
  No approval commit/history rewrite, publication, exact-SHA CI or live proof.

- 2026-09-10: Approved `cp5 v02` against original CP5 and active cp06-v01
  non-checkpoint repair overlay. Used current-worktree fallback from approved
  cp4 v02 (`12c9af1`) because no CP5 commit existed. Reviewed accumulated
  remaining-route presentation, compact launch tables/captions, Plans Back,
  and App-owned alert focus without changing domain ownership. No material
  findings. Reviewer verification: full web suite297, production build, five
  disposable Chromium tests and diff check passed. Inspected the fresh320px
  launch screenshot; caption width assertions passed at320×568 and844×390,
  with ordinary desktop table layout retained. Reviewer creates cp5 v02
  approval commit; only CP5 advances. CP6 remains unchecked. Publication,
  exact-SHA CI and live proof remain pending normal workflow delivery.

## Done Means

- Across the whole desktop app, all persistent navigation/context/actions fit two compact header rows (approximately56 CSS px each,112px combined). Main content begins by y128 at1280×720 and1440×900, default text size and secondary menus/help closed. This replaces the previous allowance for a separate Settings heading/action/tab stack.

- Every page uses document scrolling. Full mobile settings, Skills/prompts/TOML editing, Plans, Runs, New Run, Projects, and login remain functional.
- Narrow/short screens show a list or detail, with Back preserving drafts, selection, and list position. Wide screens show a useful two-column layout without a scrolling detail wrapper.
- At1280×720, an opened Skills textarea begins no lower than y208; at390×844 it begins no lower than y280 and exposes at least280px of editable height, with default text size and help collapsed. Larger text/error messages flow naturally instead of being clipped to meet this budget.
- No ordinary content requires horizontal page scrolling at320 CSS px. No zero-height editor, hidden last action, focus obstruction, or responsive state loss.
- Real Chromium and WebKit checks, screenshots, and complete web tests/build verify behavior. Remote publication, CI, and live deployment follow the repository's existing delivery boundary; local screenshots are not live-release proof.

## Critical Invariants

- Read and follow `UI_GUIDELINES.md` and root/web AGENTS. The owner explicitly requires two compact desktop header rows and mobile hamburger menus. Shared shell slots own presentation; pages contribute context/navigation/actions without moving their draft/save ownership.

- `GlobalSettings` remains the sole draft/save coordinator. Preserve per-domain/per-skill acknowledgement, revision conflicts, prompt Undo, installer behavior, and unsaved-navigation guards.
- Preserve exact project/run/plan identities, URL validation, browser Back/Forward guards, startup question semantics, mutation/idempotency keys, hidden-dashboard suspension, history tombstones, and SSE behavior. Responsive presentation must not issue mutations or create additional subscriptions.
- Document is the primary vertical scroll owner. No vertical scrolling on main/workspace, Settings body, editor detail, profile cards, or plan list. Native textareas, open combobox/menu lists, disclosed raw payloads, and a desktop navigation sidebar are the only permitted exceptions.
- Compact mode is `(max-width:959px), (max-height:599px)`. It uses one visible list/detail surface. Wide/tall mode uses both columns. Share this media query between CSS and the small layout helper; subscribe/clean up its matchMedia listener, do not introduce resize polling.
- One mounted editor/draft owner per item. Hidden presentation must be removed from keyboard/accessibility traversal. Resizing must preserve text, cursor where feasible, selected identities, and pending actions.
- Keep the existing React/CSS stack, themes, color tokens, and44px primary touch targets. Inputs use at least16px text. Technical text remains available and copyable; wrapping never rewrites saved content.

## Forbidden Implementations

- Global `overflow:hidden/clip`, body height locks, arbitrary larger min-heights, nested flex scroll overrides, or hiding content to make width assertions pass.
- Stacking a viewport-sized list above a viewport-constrained editor on phones; fixed footer overlays covering the keyboard/focused control; multiple stacked sticky bars.
- A new state store, UI framework, router, code editor, device detection, user-agent sniffing, backend API, settings schema, or virtualized list for this change.
- Shrinking text/touch targets, removing advanced actions, auto-installing skills, changing TOML/newlines through wrapping, or resetting unsaved state on resize/navigation.
- Counting DOM-only tests or browser loading screenshots as usability proof; deleting existing semantic tests merely because layout changes require new selectors.

## Checkpoints

### [x] Checkpoint 1: Restore a single document scroll owner

**Goal:** All existing content remains reachable when the viewport is short.

**Context:** Run `git rev-parse --show-toplevel`, `git status --short`, `git rev-parse HEAD`; read applicable AGENTS. Inspect `apps/aflow_app/web/src/styles.css`, `App.tsx`, `components/SidebarEditorLayout.tsx`, `components/PlanPanel.tsx`; search `rg -n 'overflow|height:|scrollIntoView' apps/aflow_app/web/src` and existing browser tests.

**Scope:** Shell/layout CSS, App layout classes, PlanPanel inline sizing, shared layout, associated tests and architecture notes. No domain or server implementation.

**Steps:**

- [x] Remove html/body/root/shell100%-height and overflow locks. Use natural height and `min-height:100dvh` with100vh fallback for the shell. Main and detail wrappers grow in document flow; remove workspace-panes ownership rules rather than layering contradictory overrides.
- [x] Remove PlanPanel inline height/overflow ownership. Ensure flex/grid children have `min-width:0`, fields fit their containers, and ordinary long labels/paths wrap. Preserve native textarea scroll behavior.
- [x] Wide/tall sidebar uses `minmax(12rem,16rem) minmax(0,1fr)` with a16px gap. Its navigation may be sticky at16px, max-height `calc(100dvh - 32px)`, and overflow-y:auto; detail is never height-constrained. Compact mode temporarily stacks natural-height blocks untilCP2; no35dvh rows or hidden content.
- [x] Remove blanket `overscroll-behavior:contain` on editors/navigation. Scope overflow exceptions to named components; document each remaining non-native vertical exception in CSS. Preserve bounded combobox options/raw payload blocks.
- [x] Rewrite existing browser assertions tied to independent detail scrolling to check document movement and last-control reachability. Retain draft, selection, history, and combobox assertions. Add natural-scroll regression on long Settings and plan content in existing browser fixtures.
- [x] Update web AGENTS and ARCHITECTURE to replace bounded-viewport guidance with the new ownership contract; update DEVLOG. Do not modify root AGENTS.

**Dependencies:** None; account for already-delivered overlapping code without reverting it.

**Verification:** `npm --prefix apps/aflow_app/web test -- --run`; `npm --prefix apps/aflow_app/web run build`; `uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_settings_browser.py apps/aflow_app/server/tests/test_run_navigation_browser.py`; `git diff --check`.

**Done When:** Wheel over ordinary content moves the document, and the last form control is reachable at390×844 and844×390. Existing semantic browser checks still pass. Report `git status --short`, `git diff --name-only`, `git diff --stat`.

**Blockers:** Report genuinely ambiguous overlapping dirty-file ownership; unrelated dirty files outside scope do not justify stopping.

### [x] Checkpoint 2: Add narrow list/detail navigation without losing state

**Goal:** Use available phone space for one task while retaining the wide layout.

**Context:** Inspect `SidebarEditorLayout.tsx`, its call sites via `rg -n 'SidebarEditorLayout' apps/aflow_app/web/src`, and selection state in GlobalSettings/PromptsSettings/SkillsSettings/RunDashboard. Inspect App query and navigation tests.

**Scope:** Shared layout/helper, minimal props at consumers, tests. No draft/state architecture rewrite.

**Steps:**

- [x] Extend the existing layout with an accessible list label, explicit user-open signal (reuse navigationVersion), and explicit-detail-entry flag for a run URL. Local presentation state controls list/detail; domain owners retain selection/drafts. Compact first entry shows list unless explicitly deep-linked; wide mode preserves existing selected detail behavior.
- [x] Selection from a compact list opens detail with “Back to Skills/Teams/Workflows/Prompts/Run history”. Back does not clear selection or drafts, issue a save, or change selected run. Keep the editor mounted/hidden when needed to preserve state, with actual hidden semantics and no focusable hidden controls.
- [x] Capture list document position and clicked-item identity before opening. On Back restore that position and focus the same row, or the list heading if removed. On explicit opening focus detail heading using preventScroll and move document to its start once. Passive refresh/polling never resets focus/scroll. Re-clicking the selected row still opens it.
- [x] Resizing an open detail to wide and back retains detail; resizing an untouched wide default selection to compact uses list. Changing Settings section resets presentation to its list while retaining per-section drafts/selection. Do not add URL parameters/history entries for local Settings selection. Existing browser Back/Forward behavior remains guarded by App.
- [x] Add focused component tests for first entry, repeat selection, Back, deep link, removed row, dirty resize, and inactive focusability. Extend real browser navigation checks to verify document position and focus after Back.

**Dependencies:** CP1.

**Verification:** `npm --prefix apps/aflow_app/web test -- --run src/App.test.tsx src/components/GlobalSettings.test.tsx src/components/SkillsSettings.test.tsx src/components/PromptsSettings.test.tsx src/components/RunDashboard.test.tsx src/components/SidebarEditorLayout.test.tsx` (create the last file); build web; run the two browser modules fromCP1.

**Done When:** List/detail/back and resize work without lost drafts, changed request identities, or accidental refresh focus changes. Report Git scope checks as inCP1.

**Blockers:** If a consumer's selection is only inferred from list order, keep domain behavior and add an explicit UI open signal; never fabricate identity.

### [x] Checkpoint 3: Compact the shell and Settings action hierarchy

**Goal:** Navigation and occasional actions stop consuming the editor's viewport.

**Context:** Inspect App header/nav, GlobalSettings toolbar/tabs, SkillsSettings install card, styles, and tests.

**Scope:** App shell/action slots, page-level context/action contributions across GlobalSettings, RunDashboard, GlobalRunOverview, ProjectPicker and PlanPanel, small shared disclosure if needed, tests. This checkpoint changes only header composition; CP5 retains body/journey work. Preserve existing action handlers and owners.

**Steps:**

- [x] Implement shared header slots across the app: desktop row1 contains AFlow/project context, global destinations and account action; row2 contains concise page context, local sections/filters, the primary action and More. Target44px controls plus compact padding for two rows of about56px each,112px combined. Main content begins by y128. Remove duplicate page headings/action bars from page bodies. Render page-owned action nodes into these slots using a small shared composition API/context; do not duplicate action state/handlers. Register/clear contributions on page changes so hidden dashboards cannot supply stale actions.
- [x] Mobile/compact mode uses a visible hamburger icon with accessible name Menu, current context and primary action. Expanded Menu is in document flow with existing destinations and Logout, `aria-expanded`/`aria-controls`, Escape-to-close and focus return; no modal/body lock. Preserve current project identity. Local navigation uses a labelled selector/menu rather than multiple ambiguous hamburger buttons.
- [x] Settings uses row2 for its context, sections, Save all changes and More, with no separate title/reload/tab rows below. More exposes Reload, Advanced/Guided and Install skills using existing safeguards. When controls cannot fit, collapse section tabs to a labelled selector and secondary actions to More before creating a third row; preserve readable labels/targets. At most row2 may stick to top0 on screens ≥600px high; row1 scrolls away. Reserve flow space and focus offsets. Short screens use static actions. Enlarged text may flow or use compact presentation, never clip to meet the budget.
- [x] Compact Settings section selector is a labelled native select with all current sections, including later-added sections such as Changelog. Wide mode keeps keyboard tabs. Render only the active navigation variant; both use the same tab state and preserve all drafts. Do not hardcode six sections in acceptance tests.
- [x] Move Skills installation behind the row2 More → Install skills action, which reveals an in-flow installation disclosure in the task area only when requested. No permanent install card or separate disclosure-trigger row. One short description inside; default install action and disabled-while-dirty behavior unchanged. Installation failure expands the disclosure and announces the error; subsequent tab navigation retains the result as currently owned. Remove CLI implementation prose from the default editing surface, retain useful instructions within help.
- [x] Use8/12/16px gaps and12px compact page padding. No larger borders/cards around single toolbar actions. Preserve44px touch targets; no icon-only unlabeled controls. Test all actions with keyboard and narrow wrapping.

**Dependencies:** CP2.

**Verification:** `npm --prefix apps/aflow_app/web test -- --run src/App.test.tsx src/components/GlobalSettings.test.tsx src/components/SkillsSettings.test.tsx`; build web; `uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_settings_browser.py`; `git diff --check`.

**Done When:** At1280×720 and1440×900 all authenticated pages have at most112px of persistent header chrome and main content starts by y128. Skills textarea starts by y208 on desktop and y280 at390×844 with default text/help collapsed. Menus/selectors expose every existing destination/action, and dirty Reload remains guarded. Report Git scope checks.

**Blockers:** If new sections/actions were added concurrently, include them through the shared registry rather than dropping them to meet the space budget.

### [x] Checkpoint 4: Make settings and text editors readable on phones

**Goal:** Editing works without sideways prose reading or nested form scrollers.

**Context:** GlobalSettings/profile rows, SkillsSettings, PromptsSettings, SettingsPanel, PlanPanel textareas, Combobox, styles, settingsDraft tests.

**Scope:** Form/editor presentation, small shared textarea wrapper only if duplication warrants it, tests. No saved-format/parser changes.

**Steps:**

- [x] Text editors use soft visual wrapping by default, with a labelled Wrap lines toggle for Markdown/TOML/raw text; turning wrapping off permits horizontal scrolling only inside that native editor. Never transform the value or persisted bytes. Use16px minimum input text, width100%, sensible padding, and default height `clamp(18rem,50dvh,36rem)`. Allow native vertical scrolling and desktop resize; parent remains natural flow.
- [x] Group wide profile rows into name/model/effort columns; compact mode stacks labelled fields. Natural row height, concise visible labels, full accessible identity; preserve custom models, unset effort, unsupported-effort behavior, add/remove/Enter behavior, and errors. No per-card scrollers or faux disabled inputs.
- [x] Keep Save/error/conflict/partial acknowledgement and prompt Undo reachable in document flow. Maintain textarea drafts across tabs, Advanced/Guided, Back, resize, failed save, and partial skill save. Ensure the save action scrolls into view through ordinary keyboard traversal.
- [x] Combobox popovers stay within viewport inline size and use their existing bounded options scroller; selection works by touch/keyboard near the lower edge. Add explicit scroll-into-view only for user navigation/error focus, never render/poll effects.
- [x] Browser checks use long Markdown/TOML lines and20k-character content, several profile rows, error messages, and partial-save responses. Verify exact stored text, wrapping, textarea scroll versus document scroll, and at least280px visible textarea height at390×844 initial detail.

**Dependencies:** CP3.

**Verification:** `npm --prefix apps/aflow_app/web test -- --run src/components/GlobalSettings.test.tsx src/components/SkillsSettings.test.tsx src/components/PromptsSettings.test.tsx src/components/Combobox.test.tsx src/settingsDraft.test.ts src/App.test.tsx`; build web; run Settings browser module; `git diff --check`.

**Done When:** Long prose is readable without lateral page movement; phone editing/save/rejection retains exact text and no parent scroll trap. Report Git scope checks.

**Blockers:** Preserve unsupported provider configuration semantics; do not invent controls to fill grid cells.

### [x] Checkpoint 5: Apply the same responsive contract to remaining journeys

**Goal:** Runs, New Run, Plans, Projects, All runs, and login remain fully operable.

**Context:** RunDashboard, GlobalRunOverview, ProjectPicker, ProjectCreateForm, PlanPanel, App login/guards, styles, existing browser fixtures.

**Scope:** Presentation/interaction fixes needed by the contract, component/browser tests. No new run ordering, progress projection, launch semantics, registry changes, or backend work.

**Steps:**

- [x] Apply the shared list/detail behavior to Runs; explicit run links open detail, Back retains history filter/loaded pages/position. Detail summary/actions/form sections follow document flow; Technical/Raw remain disclosures with only raw blocks allowed local overflow.
- [x] Keep New Run controls and Start/Cancel in a natural single-column compact form. Stack tables into labelled rows when their meaning survives it; confine truly two-dimensional raw data to an explicit overflow block. Preserve workflow/team/plan selection, advanced settings, startup questions and restart confirmations. No action may be hidden merely to shorten the page.
- [x] Plans retains its existing list/editor navigation but gains document scrolling, a consistent Back action, wrapping editor and reachable Save/Ready/Run actions. Preserve revision conflicts and dirty confirmations. Collapse New plan creation behind a labelled action if needed, keeping filename validation and flow unchanged.
- [x] Check Projects create/open/configuration states, All runs populated/empty/partial-failure states, login/error, and unsaved/logout confirmations. Wrap long names/paths, stack narrow controls, and ensure newly shown alerts scroll/focus into view without covering content. Existing App guards keep ownership; do not introduce competing modal state.
- [x] Extend meaningful browser journeys with disposable fixture data for plan creation/edit/save, launch/startup question, run detail/back/history refresh, and project selection. Do not launch or alter production runs for tests.

**Dependencies:** CP4.

**Verification:** `npm --prefix apps/aflow_app/web test -- --run`; build web; `uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_run_navigation_browser.py apps/aflow_app/server/tests/test_settings_browser.py`; `git diff --check`.

**Done When:** All listed flows reach their final action at320px width and844×390 without horizontal page scrolling, lost input, or hidden controls. Report Git scope checks.

**Blockers:** Real execution/provider errors outside presentation remain visible; do not change lifecycle to make a visual test pass.

#### Completion note

- Added compact/short-screen flow rules for New Run forms, launch previews, responsive choice tables, run metadata, long route content, and plan Back navigation without changing lifecycle or launch semantics.
- Kept App-owned login/logout and unsaved-navigation guards authoritative; newly shown login/logout/guard alerts are focusable and remain in document flow.
- Extended the disposable Chromium journey with plan create/edit/save, project creation-form reachability and selection, intercepted startup-question launch, populated All runs detail/Back, and logout failure checks at320×844 and844×390. No worker or production run was started.
- Local verification: full web suite297/297, production build, browser modules5/5, and `git diff --check` passed. Changes remain uncommitted; publication, CI, and live proof are not part of this checkpoint execution.

### [ ] Checkpoint 6: Verify task usability across browsers and viewport changes

**Goal:** Prevent recurrence with behavioral geometry checks and inspected artifacts.

**Context:** Existing live-server fixtures and browser tests, `.github/workflows/ci.yml`, web AGENTS, ARCHITECTURE, DEVLOG, relevant README.

**Scope:** `apps/aflow_app/server/tests/test_responsive_browser.py` (new), existing browser tests/fixtures, minimal CI test step, documentation, small layout corrections exposed by these checks. No unrelated redesign.

**Steps:**

- [ ] Add parameterized Chromium journeys across320×568,390×844,768×1024,844×390,1280×720,1440×900 and390×420. Reuse authenticated disposable live-server fixtures and built real assets; deterministic long names,40-list entries, long text, validation/partial-save failures. Wait for loaded task controls before measuring.
- [ ] Assert the shared two-row desktop header budget on Settings, Runs, All runs, Plans, Projects and New Run; expand/collapse secondary menus and verify every action remains reachable. At enlarged text sizes require reflow instead of enforcing fixed heights. Assert document scrolling on ordinary content, no unauthorized vertical scrollers, no horizontal page overflow, positive visible detail/editor height, task-space budgets under normal text conditions, last action reachability, and click hit-testing after scrolling. Check list/detail/back position, focus and dirty resize. Do not rely on scrollbar counts alone or inject artificial height as the main acceptance fixture.
- [ ] Run phone journeys in WebKit using the same module with `AFLOW_TEST_BROWSER=webkit` (default chromium; reject unsupported values). Add one Ubuntu/Python3.12 dashboard CI step to install WebKit and run this module in WebKit, leaving other jobs' browser matrix unchanged. Missing required browser is a failed prerequisite, not a passing skip.
- [ ] Check200% text enlargement and320 CSS px reflow, keyboard Tab/Shift-Tab/Enter/Escape, all section selectors, touch-sized controls, safe-area padding on any sticky element, and visible focused fields after390×844→390×420 resize. No stacked sticky bars. Record a physical iOS/Android keyboard/browser-toolbar check if available; otherwise explicitly report it unverified rather than equating emulation with hardware.
- [ ] Capture representative light/dark screenshots for Skills list/detail, profile editing, Run history/detail, New Run and Plans, including landscape. Inspect images for clipping, sparse hierarchy, accidental whitespace and obscured actions. Persist artifacts through the existing test/CI artifact mechanism; add a narrowly scoped upload step if none exists.
- [ ] Update web AGENTS/ARCHITECTURE/DEVLOG with the implemented scroll contract and acceptance evidence, and existing README usage if navigation help changes. Root AGENTS stays unchanged. Mention superseded old-layout acceptance without marking unrelated plans done.

**Dependencies:** CP1–5.

**Verification:**

- `npm --prefix apps/aflow_app/web test -- --run`
- `npm --prefix apps/aflow_app/web run build`
- `uv run --project apps/aflow_app/server playwright install chromium webkit`
- `uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_settings_browser.py apps/aflow_app/server/tests/test_run_navigation_browser.py apps/aflow_app/server/tests/test_responsive_browser.py`
- `AFLOW_TEST_BROWSER=webkit uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py`
- `git diff --check`; inspect screenshots; report Git status/diff scope.

**Done When:** Required browser/test/build checks pass, screenshots are inspected, observed limitations are explicit, and no pre-existing semantic guarantees were weakened. After normal delivery, verify the exact deployed revision and live Settings→Skills read-only without editing production content; report live proof separately from local acceptance.

**Blockers:** Missing browser binaries or an unavailable live environment is an explicit verification gap, not permission to claim success. Do not bypass CI or restart active workers.

## Behavioral Acceptance Tests

- Given1280×720 or1440×900 at default text size, navigate every authenticated page: global navigation, page context, sections and primary actions occupy only two header rows, at most112px combined, with task content starting by y128. Skills text begins by y208. Reload/Advanced/Install remain accessible through More without persistent extra rows.
- Given compact mode, open the hamburger Menu by touch and keyboard, navigate and close with Escape: destinations are complete, focus returns correctly, and no body scroll lock or stale hidden-page action appears.

- Given390×844 and long skill text, open Skills then an item: list disappears, Back/editor/Save are reachable, editor top≤280 and visible height≥280 under default conditions. Back restores the chosen row and list position without discarding text.
- Given320px width or844×390, traverse every page/section and last action by document scroll; neither detail nor editor collapses. Long content does not force sideways page scrolling.
- Given dirty Markdown/TOML, toggle wrapping, resize, visit another section and return: exact bytes and edits remain; failed save preserves them, partial save clears only acknowledged domains.
- Given a scrolled Run detail and incoming refresh, document position/focus/selection stay stable. Back restores history; an explicit run link opens its exact detail.
- Given keyboard navigation or enlarged text, menus/selectors/errors and final actions remain visible and operable; short viewport switches sticky controls to normal flow.
- Given project creation, plan save/Ready, New Run startup question, and restart confirmation in isolated fixtures, responsive changes preserve existing request identity and outcomes.

## Plan-to-Verification Matrix

| Requirement | Verification |
| --- | --- |
| Document scroll and permitted exceptions | CP1/6 browser wheel, scroll-owner and last-control checks |
| Full phone list/detail navigation | CP2 component + real browser Back/deep-link/resize tests |
| Two-row desktop header and reclaimed task space | CP3/4/6 per-page chrome/content/editor budgets and inspected screenshots |
| Exact drafts/save/conflicts | CP4 existing semantic tests + long-content browser journey |
| All routes usable | CP5/6 loaded real-browser journeys with isolated fixtures |
| Focus/reflow/touch/short height | CP6 Chromium/WebKit, text enlargement, keyboard/resize tests |
| Delivery truthfulness | Existing publication boundary, exact-SHA CI and live read-only check |

## Assumptions And Defaults

- Execution is authorized by the owner. Use checkpoint_delivery and DogfoodLuna through the existing primary project; no extra project registration. The workflow owns branch/worktree setup and reviewed delivery to origin/main.
- Keep familiar visual identity. Compactness comes from hierarchy and fewer persistent controls, not smaller input text or hidden functionality.
- Compact mode defaults were chosen to cover tablet widths and landscape/keyboard-reduced height; use one shared breakpoint contract, not device detection.
- Browser Back/Forward retains current App semantics; local list Back does not add a new URL/history system.
- Layout is independent of issue35 progress work. Parallel execution is authorized in separate workflow-managed roots. Preserve the active live-configuration run; reconcile accepted main changes at integration without dropping either behavior or merging unfinished unrelated work.
- Source guidance: W3C WCAG2.2 Reflow (320 CSS px), Focus Not Obscured, and Target Size Minimum. Product touch target remains44px, above the24px minimum-with-exceptions. These checks are not a claim of full WCAG conformance.
