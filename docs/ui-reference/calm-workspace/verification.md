# Calm-workspace fidelity verification

## Scope

Checkpoint 10 was verified in the workflow-managed worktree on 2026-09-23.
These are local disposable-fixture results only. No shared service, live run,
primary checkout, or deployment was restarted or changed.

- Base HEAD before this uncommitted checkpoint: `a2531c2f363d633e9086ec51bda5b68bb23a898a`
- Frozen reference: `demo.html`
- Frozen reference SHA-256: `2465c0ac2bfef90af2b98a537ad5ac3e931b927c23a78379a8c532ce4dcbadf4`
- Reference surface: `#af-frame`; demo bar/footer excluded

## Capture evidence

The final authenticated built-app fixture capture was run once per engine:

```sh
AFLOW_TEST_BROWSER=chromium uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_ui_demo_fidelity_browser.py::test_ui_demo_fixture_captures_authenticated_built_app
AFLOW_TEST_BROWSER=webkit uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_ui_demo_fidelity_browser.py::test_ui_demo_fixture_captures_authenticated_built_app
```

Each run passed `1 passed, 3 warnings` and emitted 18 production captures for
running, paused, and completed variants at 1280×720, 1440×900, and 390×844 in
light and dark themes. Both manifests report the frozen reference SHA and
`legacy_warning: false` for every capture. The paired disposable artifact roots
from the final runs are:

- Chromium: `/tmp/pytest-of-root/pytest-3661/test_ui_demo_fixture_captures_0/`
- WebKit: `/tmp/pytest-of-root/pytest-3662/test_ui_demo_fixture_captures_0/`

Representative inspected comparisons used the reference renders under
`/root/.codex/visualizations/2026/09/22/01a0c848-478f-70d2-892c-56c9987f3bf0/fidelity-audit/checkpoint-browser-artifacts/pytest-3658/test_ui_demo_reference_renders0/`
and the final production running captures in the first disposable root above,
including desktop/mobile light and dark PNGs.

## Observable contract

The final production manifest records these representative measurements:

- Desktop content container begins within y=128; title, Current work, Latest
  result, and Recent activity remain above the 720px fold.
- At 390×844, Latest result begins at y≈398 in Chromium and y≈422 in WebKit;
  it is visible without interaction in both engines.
- Default run rows measure 59.375px, inside the 56–72px target; sibling boxes
  remain stable while desktop hover or mobile touch previews open.
- Semantic anchors occur in order: title, current work, latest result, recent
  activity, mobile recent activity, first disclosure.
- The desktop and mobile menu/preview checks include Escape dismissal,
  focus/hit-testing, touch preview, and final-action reachability.

Visual inspection found the same compact overview hierarchy in light and dark
reference/production comparisons. The measured desktop title/current/latest
anchors are y=153/213/285 in both engines versus y=133/206/295 in the frozen
surface; the mobile Chromium equivalents are y=199/305/398 and the WebKit
equivalents are y=199/305/422, versus y=141/291/405 in the frozen surface.
Those larger-than-16px offsets are explained by the authenticated shell's
project/run navigation and the real fixture's event/disclosure data, not by a
hidden overflow workaround: the production container still begins by y=128,
the order is unchanged, and the screenshots were inspected side by side.
Production intentionally includes the authenticated two-row shell,
project/run navigation, and richer populated `Recorded progress details`,
checkpoint, delivery, and technical evidence below the overview; the frozen
demo is a representative surface with fewer data fields. These differences
were inspected and are not treated as hidden defects. No unresolved material
top-level ordering or density discrepancy was found.

## Final verification results

- `npm --prefix apps/aflow_app/web test -- --run`: 28 files, 568 tests passed.
- `npm --prefix apps/aflow_app/web run build`: passed; only the existing large
  JavaScript chunk warning was emitted.
- `uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests`:
  458 passed, 3 known deprecation warnings.
- The final Chromium combined responsive/run-progress/fidelity gate passed 69
  tests with 3 warnings.
- The final WebKit combined responsive/run-progress/fidelity gate passed 69
  tests with 3 warnings.
- Focused settings journey passed 2 tests; the route matrix passed 2 tests per
  engine; delayed-stop review passed in both engines.

The viewport matrix exercised by the responsive and fidelity suites is
320×568, 390×844, 768×1024, 844×390, 1280×720, 1440×900, and 390×420, with
enlarged text, long labels, open menus, failures, focus restoration, list/detail
Back, refresh retention, and save/conflict/recovery journeys. Physical mobile
keyboard behavior remains separate from this emulated evidence. These local
results do not establish deployment or live usability.

## 2026-09-25 — Recent-first project sidebar, Checkpoint 2

The populated browser fixture contains 120 older runs without a recorded
outcome plus recent running, failed, and completed runs with long plan titles.
`test_recent_runs_are_on_first_history_page_without_extra_fetch` writes
`run-sidebar-{light,dark}-{320x568,390x844,1280x720,1440x900}.png` under
its pytest temporary directory in each engine. All 16 captures were inspected
against the frozen light desktop/phone reference renders and the dark reference
surface. The reference uses a running detail example; this fixture selects the
newest failed run, so its detail correctly shows a failure. The authenticated
two-row header and richer real detail remain known surface differences.

At every captured size, the first three rows are the recent failed, completed,
and running runs. Their statuses are complete and readable, the long desktop
titles use at most two lines, and the phone titles wrap naturally. The 97 older
outcome gaps appear under one closed, counted disclosure instead of a wall of
repeated badges. The 320px phone still shows the three rows, disclosure, and
Load more control above the fold. Browser assertions also cover no horizontal
document overflow or page errors, a 44px preview target, no sibling shift on
preview, touch access, selected old-run Back/focus/scroll restoration, and
unique identities after Load more. No material sidebar hierarchy or clipping
gap was found in the rendered comparison. Physical mobile keyboard behavior
and hosted live data remain separate delivery checks.

Local Checkpoint 2 gates: 653 web tests, production build, 37 Chromium and 37
WebKit follow-up/navigation browser cases, 512 full server tests, and
`git diff --check` all passed. The built-app browser evidence is local; it
does not establish publication or live activation.

Checkpoint 2 count follow-up: the 123-run fixture's first page has a live
cursor, so its heading now reads `100 loaded` above the `97 loaded` outcome-gap
disclosure and Load more control. After the final page it reads `123 recorded`.
The 1280×720 light Chromium and WebKit populated captures were inspected with
the corrected heading. A focused pinned-run component assertion confirms that
the direct-link row does not increase the loaded count. The 653-test web suite,
production build, focused Chromium and WebKit browser cases, and diff check
passed locally. The current `origin/main` delivery gate is red; this evidence
does not authorize publication or establish live activation.

Review update: `origin/main` `46224dfa` passed all 12 CI jobs. The focused
count repair passed Chromium/WebKit checks and the full web suite with one
Vitest worker (653 tests); the default parallel suite timed out on one
130-row refresh test, which passed alone. Publication and live comparison
remain pending.

Final cumulative review integrated both checkpoint commits with `46224dfa`,
preserving concurrent scheduling and plan-consumer behavior. Combined checks
passed: 26 repository/history, 73 API, 659 web tests (one worker), production
build, 40 Chromium and 39 WebKit browser cases, and diff checks. The browser
sets include navigation, row fidelity and equal/changed refresh; Chromium also
rerendered the frozen reference. Populated light/dark desktop/phone captures
were inspected; no material sidebar gap was found. The first WebKit attempt
ended with SIGTERM after one passing case; the complete rerun passed.
These are local integrated-code results. New exact-SHA CI and live activation
remain separate delivery gates; the running UI still served `46224dfa` during
review. The frozen demo remains unchanged.
