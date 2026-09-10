# Issue 38 checkpoint 1 v03 comparison source

## Provenance

The accepted baseline setup was recovered with:

`git show 52597409b17c3e1050f1576bfe027580b5656d50:apps/aflow_app/web/src/components/RunDashboard.test.tsx`

HISTORY: In that baseline, the shared test setup cleared `window.location.search`
HISTORY: and `window.location.hash`; the clipboard test installed a configurable
HISTORY: `navigator.clipboard.writeText` mock without an exit restoration.

The pending setup under comparison captures the original location and clipboard
descriptor, clears the URL with `history.replaceState`, installs the configurable
clipboard mock, and restores both in `afterEach`. The comparison source isolates
both setup variants with the same per-case restoration.

## Retained source

The executable comparison is retained in
`apps/aflow_app/web/src/components/RunDashboard.test.tsx`:

- `installResumeComparisonSetup` applies `accepted-baseline` or `pending` setup.
- Four parameterized cases use the same `needs_attention` source run, deferred
  list/detail promises, and `review` detail marker.
- Each case verifies list settlement, visible and enabled Resume, pending detail
  before settlement, direct-detail settlement, and Confirm visibility after the
  click.
- The `before-detail-settlement` cases click Resume before detail resolves, then
  verify Confirm survives detail settlement. The `after-detail-settlement` cases
  resolve and observe detail first, then click the enabled Resume action.
- Each case restores location and clipboard state in `finally`; no sleep, retry,
  timeout change, or assertion weakening is used.

## Exact comparison command

`npm --prefix apps/aflow_app/web test -- --run src/components/RunDashboard.test.tsx -t "retains confirmed resume readiness" --reporter verbose`

Output is retained in
`plans/notes/issue-38-stable-dashboard-clipboard-cp01-v03-comparison-output.txt`.
