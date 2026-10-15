# Benchmark: show the actual terminal run outcome

## Summary

Fix [AFlow issue #94](https://github.com/evrenesat/aworkflow/issues/94): a finished
run should lead with what happened, rather than “No current work” and
“Recorded update: Transition end”. This is a real defect, combining evidence
selection, React composition, asynchronous UI state, and browser acceptance.

Frozen implementation base: `ebc64fb78a745f33908c7979fb6eb9a66d8c129e`.
This refreshes the [earlier full benchmark assignment](https://github.com/evrenesat/aworkflow/issues/94#issuecomment-6075054395)
against current main. It does not inherit the older helper-only benchmark's
verification exemptions. No reference implementation is supplied.

Planning evidence on 15 October 2026:
- A `turn_finished` event with summary “Fixed stale refresh selection.” followed
  by `run_completed {end_reason: "transition_end"}` selects the latter and
  displays “Transition end”.
- A single event containing both that mechanical end reason and summary
  “Implemented the result view.” also displays “Transition end”.
- Existing runPresentation and RunOverview tests: 35 passed, 2 files.
- RunDashboard still constructs terminal “No current work”; RunOverview always
  renders its Current work section. Browser behavior has not been newly verified
  during planning.

## MVP And Scope Boundary

**Now (MVP):** preserve useful recorded results, show an honest terminal Outcome
first, retain active-run presentation and existing failure distinctions, and
prove the behavior in the real built frontend with disposable backend fixtures.

**Later / Out of scope:** API/schema/controller changes, new requests or event
storage, broad dashboard redesign, benchmark runner infrastructure, model/provider
configuration, production deployment, and unrelated defects.

The benchmark participant implements two checkpoints in an isolated p100
checkout at the frozen base. Do not inspect sibling trials, later solutions or
previous benchmark implementations. Do not repoint the shared editable aflow
installation, start workflows, modify global settings, or contact production.
Use existing repository dependencies and fixtures.

The plan remains on a planning-only branch until deliberately selected.
Do not merge this ready-plan document into the production auto-consumed queue
merely to store it. The benchmark coordinator prepares isolated trials and owns
publication of their committed results to discoverable branches/PRs. Do not
merge competing trials into production. Production acceptance and closure of
#94 require a separately selected implementation and the existing exact-SHA
CI/deployment/health gates; a benchmark pass does not close the issue.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Done Means

All behavioral criteria B1–B12 below pass, the cumulative verification gate
passes without skipped required cases, and the committed result is discoverable.
Screenshots demonstrate usable outcomes rather than merely an absence of errors.
A timeout, skipped browser, unverified case or unexplained regression is not a pass.

## Critical Invariants

1. Canonical RunStatus/activity decides terminal versus active presentation.
   Reuse `isTerminalInactiveRun`; do not infer termination from an event alone.
   Preserve pending startup and outcome-unrecorded behavior.
2. Event input is already chronological. Preserve array order, object identity,
   public helper signatures, raw payloads, and existing text length bounds.
3. A completion event never proves review approval, publication, CI or deployment.
   Old successful work must not explain a current failure.
4. Preserve post-review delivery, controller-failure and reviewer-failure branches,
   sanitized short messages, partial-count qualifiers, Diagnostics and raw evidence.
5. Preserve exact project/run selection, request-generation ownership, document
   scrolling, open disclosures and keyboard focus on unchanged refreshes.
6. Existing UI_GUIDELINES apply. No root AGENTS edits, new dependencies or new
   state-management/data-fetching subsystem.

## Forbidden Implementations

- Treating every sentence containing “completed” as mechanical.
- Choosing any earlier nonempty string regardless of newer failure/stop evidence.
- Falling back to an old success as the reason for a failed/stopped run.
- Fabricating approval, zero counts, delivery or deployment.
- Replacing the overview with a screenshot/demo, editing the frozen design demo,
  deleting tests, weakening failure assertions, hiding errors to satisfy geometry,
  or adding sleeps to conceal a request race.
- Broad component rewrites, new polling, controller operations or shared runtime
  installation changes.

## Behavior Contract

### Summary extraction

Use the existing field order: failure_reason, end_reason, summary, outcome,
reason, error, message, semantic_summary, result.

A substantive value is a nonblank string that passes the existing heading filter
and is not exactly one of `transition_end`, `completed`, `done`, `finished`
after trim, lowercasing and replacing each run of spaces/hyphens with underscore.
Filter before choosing the first value; keep the existing whitespace collapse,
240-character bound and field-specific formatting. Thus “Completed three fixes”
remains substantive. Keep the existing truthful unavailable-result fallback.
Raw details retain every original value.

### Event selection

Preserve existing terminal-event eligibility and outcome classification. Scan
newest to oldest without sorting or mutation.

- A newest eligible failure or stop wins even without text.
- Defer only a successful `run_completed` or `run_finished` event that has no
  substantive payload text. Remember the newest such deferred event.
- Continue past deferred lifecycle events to the newest substantive success or
  other eligible nondeferred success; return the original event object.
- If an older failure/stop is reached after deferring a newer successful
  lifecycle event, return the newer deferred event. Do not resurrect an old
  failure as the outcome of a newer completion.
- With no better candidate return the newest deferred event, or null when no
  eligible event exists.
- A `run_completed` carrying status `failed` is a problem, never a deferred
  success. Substantive text never overrides a failure/stop's classification.

### Dashboard authority and layout

For terminal inactive runs, replace the Current work slot with one Outcome
section (`data-ui-fidelity-anchor="outcome"`) before Recent activity.
Retain the active Current work anchor and layout unchanged for nonterminal runs.

Outcome begins with canonical status. Preserve the existing specialized order:
post-review delivery failure, controller failure, reviewer failure, then generic
terminal presentation. Move their existing diagnostic messages/links into Outcome
without weakening wording or exposing raw host paths.

For a generic failed/stopped run, prefer a substantive sanitized canonical reason,
then a matching problem event's substantive summary. Do not use a successful event
or success-oriented manager text as its explanation. If neither is available say
“Failure reason unavailable.” or “Stop reason unavailable.” respectively.

For a completed run, prefer the selected substantive event summary; otherwise use
the current run's existing manager resultText then finishedSummary only if
substantive. If none exists, state “Completed is recorded, but no result text was
retained.” Completion is not approval.

Terminal runs have no separate Latest result section: put their result and its
existing disclosure in Outcome. Keep other distinct progress/delivery facts in
their existing locations. This avoids fuzzy ReactNode deduplication and duplicated
short answers. Preserve full selected-event detail and existing manager-result
access, and keep other events inspectable through existing activity/Diagnostics.

Reuse current request and disclosure owners. Same-run refresh preserves state;
changing run must never display the previous run's outcome, including when its
older async response arrives late. Do not introduce a second cache or new loader.

## Checkpoints

### [ ] Checkpoint 1: Select truthful substantive terminal evidence

**Goal:** fix result selection and short summaries as one bounded presentation rule.

**Context:**
- Run `hostname; git rev-parse --show-toplevel; git status --short; git rev-parse HEAD`.
- Read root AGENTS.md, apps/aflow_app/web/AGENTS.md and UI_GUIDELINES.md.
- Read apps/aflow_app/web/src/runPresentation.ts, its test file, and RunEvent
  in apps/aflow_app/web/src/types.ts. Inspect RUN_EVENT_TEXT_FIELDS,
  eventText, runEventOutcome, presentRunEvent and latestRunResultEvent.
- Read README.md, ARCHITECTURE.md and DEVLOG.md for context; preserve existing
  architecture and interfaces.

**Scope:** only runPresentation.ts and runPresentation.test.ts, plus this plan's
verified progress. No backend or transport changes.

**Steps:**
- [ ] Add focused regression tests proving B1–B6 fail where applicable on the
  frozen base before changing production code; record actual failures.
- [ ] Implement the exact summary and selection contracts above with a small
  shared substantive-text predicate. Do not duplicate the field policy in
  separate helpers or alter event eligibility as unrelated cleanup.
- [ ] Cover mixed case/spacing/hyphens, blank/non-string/heading-only values,
  length bounds, original object identity, frozen inputs, empty arrays, and
  the chronological conflict cases.
- [ ] Run the scoped gate; inspect the diff and mark only verified steps complete.

**Dependencies:** coordinator provides a clean isolated frozen-base checkout with
lockfile-compatible dependencies.

**Verification:**
```sh
(cd apps/aflow_app/web && ./node_modules/.bin/vitest run src/runPresentation.test.ts)
git diff --check
```

**Done When:** new and existing helper tests pass; the two recorded reproductions
select useful text while newer problems and genuine missing evidence stay truthful.

**Blockers:** report an incorrect base, conflicting dirty files or missing setup;
do not work around them by using a sibling trial or changing shared runtime.

### [ ] Checkpoint 2: Integrate Outcome and prove the rendered journey

**Goal:** users see the correct terminal outcome immediately, with stable interaction.

**Context:**
- Read RunDashboard.tsx around overviewCurrentWork, overviewLatestResult,
  readableFailure and the RunOverview call; read RunOverview.tsx and both tests.
- Read apps/aflow_app/server/AGENTS.md and
  apps/aflow_app/server/tests/test_ui_followup_fidelity_browser.py, especially
  test_ui_followup_run_detail and test_ui_post_review_delivery_failure.
- Reuse existing control_client, live_server, disposable configuration,
  _browser, _fidelity_artifact_dir, login and fixture helpers.
- Inspect docs/ui-reference/calm-workspace/demo.html only as design context.

**Scope:** the three frontend production files named above and their tests;
narrow styles in the existing stylesheet only if required; the existing browser
module and tightly related fixture helpers; one concise DEVLOG.md entry.
No root AGENTS, design demo, API, action or controller edits.

**Steps:**
- [ ] Add an optional Outcome slot to RunOverview. When supplied, render it in
  place of Current work and suppress terminal Latest result. Existing callers
  without Outcome retain current behavior and anchors.
- [ ] Compose Outcome in RunDashboard from the documented authority and wording.
  Preserve specialized failure notices, existing disclosures and Diagnostics.
  Reuse or narrowly export CP1's text policy rather than maintaining a second
  normalization rule.
- [ ] Add dashboard/overview tests for B7–B10. Include an explicit late response
  from run A after selecting B, and same-run refresh with evidence open/focused.
  Keep existing startup, unknown-outcome, resource-wait and delivery tests.
- [ ] Add test_terminal_outcome_overview in the existing browser module using the
  real built frontend and disposable run fixtures. Cover B11–B12: populated
  completed, failed/stopped and active controls; no invented approval; full raw
  transition_end only in disclosed evidence; keyboard and pointer operation;
  unchanged refresh; switching runs and compact Back navigation.
- [ ] Parameterize the outcome journey over 320x568, 390x844, 768x1024, 844x390,
  1280x720, 1440x900 and 390x420 in light/dark. Include a long recorded result
  and 200% text enlargement at 390x844. Assert readable wrapping, no horizontal
  page overflow, document scroll ownership and reachable/hit-testable controls.
  Preserve touch-target requirements. Inspect populated screenshots from both
  browser engines; report physical-device behavior as unverified.
- [ ] Update DEVLOG with implemented behavior and actual verification. README and
  ARCHITECTURE need no change: setup and boundaries remain unchanged.
- [ ] Run Final Verification once after final edits, inspect diff scope and
  preserve evidence and committed results for coordinator delivery.

**Dependencies:** Checkpoint 1.

**Verification while developing:**
```sh
(cd apps/aflow_app/web && ./node_modules/.bin/vitest run src/runPresentation.test.ts src/components/RunOverview.test.tsx src/components/RunDashboard.test.tsx)
git diff --check
```
The browser matrix and broad web checks belong to the cumulative gate below;
do not duplicate the full gate at every intermediate edit.

**Done When:** B7–B12 and cumulative gates pass; Outcome is first and honest,
active/startup/unknown states remain correct, and refresh/navigation retain
ownership and interaction state. Run `git status --short`,
`git diff --name-only` and `git diff --stat` before handoff.

**Blockers:** report material failures outside scope or unavailable browser setup.
Do not count skipped cases as passing or repair unrelated infrastructure.

## Final Verification

The implementer runs this gate after the final change; the coordinator checks
the retained evidence against the submitted commit. Reuse unchanged valid results.
Commands run at the trial repository root.

```sh
npm --prefix apps/aflow_app/web run lint
npm --prefix apps/aflow_app/web test -- --run
npm --prefix apps/aflow_app/web run build
uv run ruff check aflow apps/aflow_app/server/src
uv run python -m compileall -q apps/aflow_app/server/tests/test_ui_followup_fidelity_browser.py
mkdir -p artifacts/terminal-outcome-benchmark/chromium artifacts/terminal-outcome-benchmark/webkit
AFLOW_BROWSER_ARTIFACT_DIR="$PWD/artifacts/terminal-outcome-benchmark/chromium" AFLOW_TEST_BROWSER=chromium uv run --directory apps/aflow_app/server pytest -q tests/test_ui_followup_fidelity_browser.py -k 'terminal_outcome_overview or ui_followup_run_detail or ui_post_review_delivery_failure'
AFLOW_BROWSER_ARTIFACT_DIR="$PWD/artifacts/terminal-outcome-benchmark/webkit" AFLOW_TEST_BROWSER=webkit uv run --directory apps/aflow_app/server pytest -q tests/test_ui_followup_fidelity_browser.py -k 'terminal_outcome_overview or ui_followup_run_detail or ui_post_review_delivery_failure'
git diff --check
git status --short
git diff --name-only
git diff --stat
```

Expected: lint, all web tests, TypeScript/build and both named browser runs pass;
the new matrix is collected in both engines; no required case skips; no browser
page errors or unintended mutating requests. Login and fixture configuration use
existing allowlists. Visual inspection confirms hierarchy, wrapping, focus,
document movement and reachable evidence controls. Updated expected terminal
anchors are allowed; active anchors and failure protections remain enforced.

The ordinary repository CI remains required for a submitted implementation PR;
do not bypass it. It is recorded separately from this local benchmark gate.
Production release/health/rollback verification is outside the benchmark and
must not be claimed from these tests.

## Behavioral Acceptance Tests

- **B1:** substantive turn result, then bare transition_end completion -> original
  turn event selected; useful text is the summary.
- **B2:** one event with mechanical end_reason and useful summary -> useful
  summary wins; raw end_reason remains unchanged in detail.
- **B3:** mechanical-only success, blank/non-string/heading-only text -> truthful
  fallback, never invented approval; empty event list -> null.
- **B4:** success then newer failure/stop, even textless -> newer problem wins.
  run_completed with status failed is not treated as success.
- **B5:** old failure then newer substantive success -> success; old failure then
  newer bare completed -> newer completion, not old failure.
- **B6:** exact normalized mechanical tokens are skipped; “Completed three fixes”
  survives; length bound, input order and object identity are unchanged.
- **B7:** terminal inactive completed/failed/stopped -> one Outcome before Recent
  activity, no Current work filler and no redundant Latest result.
- **B8:** failed canonical status plus old successful event -> canonical failure
  reason or truthful missing-reason fallback, never old success as explanation.
  Reviewer failure and post-review delivery failure remain distinct; Diagnostics
  works and collapsed prose does not leak the fixture's private path.
- **B9:** running, paused, startup question, resource wait and outcome-unrecorded
  cases retain existing truthful presentation; no status inferred from old events.
- **B10:** unchanged refresh preserves run, open disclosure and focus; selecting B
  before A's late response prevents A's text appearing in B.
- **B11:** Chromium and WebKit, all seven viewports and both themes -> readable,
  reachable Outcome/evidence with document scrolling; text enlargement and long
  content remain usable. No unauthorized writes or page errors.
- **B12:** full raw payload can be opened by keyboard/pointer; selected run and
  disclosure survive refresh; compact Back restores list navigation; screenshots
  substantiate the result.

## Plan-to-Verification Matrix

| Requirement | Required proof |
| --- | --- |
| B1–B6 evidence semantics | CP1 regression tests, including baseline failures |
| B7–B9 truthful composition | Dashboard/overview tests and delivery browser case |
| B10 stale responses and state | Deferred-response component tests and refresh journey |
| B11–B12 rendered usability | New browser matrix, existing active/delivery checks, inspected screenshots |
| Scope and regressions | Full web gate, build, diff audit and ordinary PR CI |
| Durable result | Commit SHA and discoverable result branch/PR; #94 remains open pending production acceptance |

## Assumptions And Defaults

- This is a medium-sized frontend defect benchmark, not a 30-minute promise.
  There are two implementation checkpoints, three main production files,
  existing tests/fixtures, and no external provider dependency.
- Coordinator preparation is outside the measured implementation attempt:
  freeze source, provide identical plan bytes/tooling and warm lockfile-compatible
  dependencies/browsers; keep per-trial ports, state, HOME and artifacts isolated.
  Record exact model/harness configuration externally; this assignment is model
  agnostic. Do not alter shared installations to meet the benchmark.
- Record elapsed implementation time, tokens/cost when available, checkpoint
  outcomes, repair count, B1–B12 results and final gate results separately.
  Hard pass means every B criterion and gate passes; do not average away failures.
- Retain one bounded command/result log and screenshots under
  artifacts/terminal-outcome-benchmark (untracked, never stage raw evidence).
  Target ordinary proof below 100 MiB; retain failures until diagnosed.
  Reuse dependency caches. For new dependency/build allocation, preflight space
  and the applicable reserve; p100 had only about 12 GiB free during planning,
  so coordinator setup must resolve capacity before heavy writes. No broad prune.
- Expected maintained production change is hundreds of lines or fewer, not a
  rewrite; tests can be larger. This is a sizing estimate, not a diff quota.
- The plan does not authorize launching any trial. Preserve the original
  checkpoint ledger; check boxes only after verification. Workflow tooling owns
  any eventual lifecycle move.
