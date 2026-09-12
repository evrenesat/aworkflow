# Implement evidence-backed UI data-latency improvements

## Summary

Implement only the evidence-backed work that survives review of the corrected
CP2 discovery report at `plans/research/ui-data-latency-20260912/report.md`,
using the matching machine baseline and product source identity
`304a39fd24b2d330d2533d041f3eb2776504086c`. The run-list projection finding
is ready for a bounded implementation experiment. The selected-event finding
is a scale observation, but event pagination is blocked until a same-fixture
HTTP experiment proves that the initial event page is on the useful-data
critical path and that older access is required. This plan is implementation
work only; it does not claim that discovery made loading faster.

The current browser request-fan-out counterfactual is inconclusive. Do not
turn eager stream/restart deferral or blanket context caching into a fix from
that observation. The context endpoint remains an explicit Diagnostics read;
its loopback body-delivery finding is retained as a network/client observation.

The owner-facing baseline is explicitly BLOCKED: no authorized owner URL,
in-memory credential or exact reference run identity was available, and the
four-second symptom remains unmeasured. Do not turn loopback evidence into a
remote SLA or deployment claim.

## Git Tracking

- Plan Branch: coordinator-assigned at dispatch
- Pre-Handoff Base HEAD: `304a39fd24b2d330d2533d041f3eb2776504086c`
- Corrected discovery runner source: `1d9e732135edb3de15f3daf85c4d43df91049144`

## Done Means

- Each dispatched checkpoint preserves existing semantics, passes focused
  tests, and records same-fixture before/after evidence with the actual
  implementation source SHA. Every latency, count and payload gate names its
  fixture, row/history shape, route/request and raw-data key.
- Direct canonical serializer bytes, HTTP content-length/encoded bytes and
  browser decoded bytes remain separate measurements. The superseded 12-run
  smoke payload is never a bound for a 100-row response.
- Corrected browser summaries are grouped by fixture, route and sample kind;
  report only medians and observed ranges from the bounded samples. Do not
  claim p95, cold-disk behavior or a remote-network result.
- The final web build and focused local server/browser tests pass. Full suites,
  serialized integration, publication, CI and live activation remain outside
  this plan's local verification.
- Delivery is implementation-complete when all checkpoint gates pass. Product
  owner acceptance, if later requested on a physical or remote deployment, is
  reported separately and is not fabricated from loopback measurements.

## Critical Invariants

- `RunStatus` remains authoritative for current status, activity, worker/unit
  ownership, startup questions, continuation eligibility, active-run warnings,
  lineage and partial-source behavior. Progress remains an additive,
  failure-tolerant projection and is computed from the final activity/status
  view exactly once per returned list row.
- `visible`, `archived` and `all` history filters, cursor ordering, history
  revisions, legacy records, deleted-record handling, auth, configuration
  visibility and pagination remain unchanged at the public run-list boundary.
- Event sequence order is strict and lossless within the existing bounded
  client window. `after_sequence` remains the live-stream cursor; the new
  `before_sequence` cursor is exclusive, cannot be combined with
  `after_sequence`, and never duplicates an event across adjacent pages.
- A selected run owns all of its status, event-page, stream and context
  responses. A late response from a deselected run or superseded request may
  not overwrite status, events, notices, loading state or diagnostics for the
  current selection. Stream reconnects continue from the latest accepted
  sequence and never authorize from a query token.
- Existing authentication and full-context authorization are unchanged.
  Event-page metadata contains only public sequence/page facts; no response
  body, prompt, transcript, token, cookie or private path is added to browser
  or benchmark artifacts.
- Read work remains bounded and serial per request. No background index,
  unbounded scan, cross-request cache, polling/backoff change or shared
  installation/state mutation is introduced.

## Forbidden Implementations

- Do not remove the final daemon/unit/activity read, use stale list snapshots
  as current status, or hide malformed/partial progress errors to improve a
  benchmark.
- Do not replace history filters/cursors with client filtering, drop archived
  or legacy rows, weaken deleted/active-run safeguards, or change ownership,
  freshness, continuation, auth or full-scope semantics.
- Do not make the event endpoint return only a smaller tail without a usable
  older-page contract. Do not change event sequence meaning, silently discard
  pages, or use a cache/TTL to avoid invalidation design.
- Do not defer or abort `/events/stream`, restart metadata, context or other
  requests based solely on the inconclusive one-sample CP2 counterfactual.
  Do not convert a loopback network/client observation into a Python
  optimization.
- Do not increase concurrency, run a live load/stress test, modify `.aflow`
  state, repoint the shared editable AFlow installation, restart another
  service, claim a remote-network SLA, or treat a faster shell/skeleton paint
  as useful-data improvement.

## Checkpoints

### [ ] Checkpoint 1: Deduplicate final status/progress work in run listing

**Goal:**

- Keep the `/api/control-plane/projects/{project_id}/runs` response identical
  in meaning while removing the duplicate status/progress projections shown by
  CP2's `list_runs` profile.

**Context:**

- Run: `git rev-parse --show-toplevel`
- Inspect: `aflow/control_plane/repository.py:190 _with_progress`,
  `:228 get_run_status`, `:379 list_history`; `aflow/daemon.py:922
  run_status`; `apps/aflow_app/server/src/aflow_app_server/control_plane_service.py:240
  _status` and `:252 list_runs`.
- Corrected baseline keys: for fixture `small-short-history`, operation
  `list_runs`, the five successful raw calls are at
  `.cp2.profile.fixtures[name=small-short-history].operations[name=list_runs].baseline.samples[]`;
  their core median is `678.473 ms`, and the direct canonical serializer is
  `.profiled.payload_bytes = 232448`. The separately retained profiled call
  reports `300` repository `_with_progress` calls, `300` nested summary calls
  and `300` nested detail calls. The matching `large-short-history` core
  median is `999.964 ms`, also with one named repository count of `300`.
  The no-projection counterfactual is separately recorded as `0` in
  `.cp2.comparisons.list_size.projection_gate`; these are measurements, not a
  claim that the current product meets the future target of `100` calls for a
  100-row page.

**Scope:**

- May create or modify: `aflow/control_plane/repository.py`, `aflow/daemon.py`,
  `apps/aflow_app/server/src/aflow_app_server/control_plane_service.py`, and
  focused tests in `tests/test_control_plane_repository.py`,
  `tests/test_aflowd.py` and
  `apps/aflow_app/server/tests/test_control_plane_api.py`.
- Must not touch: web product files, event/context transport contracts,
  controller processes, shared AFlow installation, live projects or caches.
- Keep `RunRepository.list_history()`'s existing direct-caller behavior and
  default `get_run_status(run_id)` behavior. Add a narrow identity/history page
  helper, `list_history_page(limit, cursor, history)`, that applies the same
  `_run_ids()` ordering, `RunHistory.read()` filtering, revision metadata and
  next cursor but returns selected `(run_id, history_state, history_revision)`
  without status/progress projection. `list_history()` may continue to use its
  current status-returning path for compatibility.
- Give `RunRepository.get_run_status` an explicit internal
  `include_progress: bool = True` path. `AflowDaemon.run_status` must read the
  raw status with progress disabled, perform its existing unit/startup/worker
  and `project_activity` reconciliation, then apply `repository.with_progress`
  once on every return branch. The default public repository call still
  returns progress as today.
- Make `ControlPlaneService._status` return the daemon's finalized status
  without a second `with_progress`, and make `list_runs` use
  `list_history_page` followed by exactly one `_status` call for each selected
  identity before attaching history metadata. Do not change the route's JSON
  fields or ordering.
- Keep the history scan bounded at the existing page limit. The target is one
  final status/progress projection per returned row and no more than one extra
  metadata read per row beyond current final-status semantics. Any payload
  guard must compare the same 100-row HTTP endpoint observation in
  `.cp2.browser_analysis.endpoint_stats_by_fixture`, not the superseded smoke
  response or the direct serializer size.

**Steps:**

- [ ] Add focused repository/daemon/service tests that count the final
  progress projection and assert one projection per returned row, while also
  covering visible/archived/all filters, cursor continuation, legacy rows,
  active unit evidence, startup-question projection, malformed optional
  progress, and unchanged history revision fields.
- [ ] Implement the identity-page and raw/final status split described above;
  ensure every early return in `AflowDaemon.run_status` receives the same
  single final progress projection and that direct repository callers retain
  the default result.
- [ ] Run the focused API tests and inspect representative JSON for status,
  activity, progress availability, history state/revision, active-run
  warnings, auth failures and cursor boundaries. Confirm no fixture files are
  written by the read path.
- [ ] Repeat the isolated 100-run/1,000-run profile with the same seed
  `20260912`, selected history 8, other history 2 and file-count bounds. Use
  the new implementation source SHA, an external artifact directory and
  unprofiled serial medians plus one separately labelled cProfile call; compare
  both core latency and call/projection/payload bounds to the CP2 baseline.

**Dependencies:**

- CP2 discovery report and profile at
  `plans/research/ui-data-latency-20260912/`; no dependency on the unrelated
  readability worktree.

**Verification:**

- Run: `uv run --frozen pytest -q tests/test_control_plane_repository.py tests/test_aflowd.py apps/aflow_app/server/tests/test_control_plane_api.py`
- Run: `uv run --frozen ruff check aflow/control_plane/repository.py aflow/daemon.py apps/aflow_app/server/src/aflow_app_server/control_plane_service.py tests/test_control_plane_repository.py tests/test_aflowd.py apps/aflow_app/server/tests/test_control_plane_api.py`
- Run: `PROFILE_ARTIFACT_DIR="$(mktemp -d)"; uv run --frozen --project apps/aflow_app/server python scripts/perf/profile_control_plane_reads.py --scenario isolated --product-source-sha "$(git rev-parse HEAD)" --seed 20260912 --repeats 3 --artifact-dir "$PROFILE_ARTIFACT_DIR"`
- Observe: the profile records one final progress projection per returned row, core medians at or below the CP2 baselines (stretch target: at least 20% lower for both 100-run and 1,000-run fixtures), unchanged response shape/ordering and no product/live-state mutation. A failed stretch target is a finding to report; it does not authorize a second unplanned optimization.
- Before handoff, run `git status --short`, `git diff --name-only` and `git diff --stat`; changed files must stay within this checkpoint.

**Done When:**

- The run-list endpoint has one finalized status/progress projection per
  returned row, retains all current semantic and authorization behavior, and
  meets the response/work bounds with same-fixture evidence.
- Every completed step is validated against focused tests or the isolated
  profile, and no web/event/context behavior was changed.
- Verification passes and the changed files remain within scope.

**Blockers:**

- Stop and report if preserving a daemon/unit/startup semantic requires a
  second progress projection or if a fixture exposes contradictory status
  ownership that the current contract does not resolve.
- Stop and report if unrelated dirty files make ownership of the server or
  focused test changes ambiguous.

### [ ] Checkpoint 2: Bound selected event history without losing access

**Goal:**

- Reduce selected-run event transport and dependency delay while keeping the
  live stream, sequence ordering and explicit historical evidence available.

**Context:**

- Inspect: `aflow/control_plane/persistence.py:490 read_events`,
  `aflow/control_plane/repository.py:396 tail_events`, `aflow/daemon.py:1012
  poll_events`, `apps/aflow_app/server/src/aflow_app_server/control_plane_service.py:287
  events` and `apps/aflow_app/server/src/aflow_app_server/main.py:1065
  event_tail`/`:1088 event_stream`; transport models at
  `apps/aflow_app/server/src/aflow_app_server/models.py:317 EventTailResponse`.
- Inspect web owners at `apps/aflow_app/web/src/api.ts:437 listRunEvents` and
  `:601 subscribeToRunEvents`, `apps/aflow_app/web/src/types.ts:463
  RunEventTail`, and `apps/aflow_app/web/src/components/RunDashboard.tsx:242
  mergeEvents`, `:1272` selected-run effect, `:1439 loadSelectedRun`, and
  `:3198` Activity timeline.
- Corrected direct-profile baseline holds the 100-run fixture and selected
  identity `run-0000` constant: `selected_events` is `2161 B` and `12.999 ms`
  core for 8 events versus `21814 B` and `15.681 ms` for 80, from the matching
  `.cp2.profile.fixtures[].operations[].baseline.samples[]` and
  `.profiled.payload_bytes` keys. Lite context is a separate explicit
  Diagnostics read (`6791 B`/`16.519 ms` versus `26444 B`/`17.719 ms`) and is
  unchanged by this handoff. These are not HTTP wire-size limits.

**Scope:**

- May create or modify: `aflow/control_plane/persistence.py`,
  `aflow/control_plane/repository.py`, `aflow/daemon.py`,
  `aflow/control_plane/models.py`, the control-plane server model/service/
  route files named above, `apps/aflow_app/web/src/api.ts`,
  `apps/aflow_app/web/src/types.ts`, `apps/aflow_app/web/src/components/RunDashboard.tsx`,
  and focused server/API/web tests including
  `tests/test_control_plane_repository.py`,
  `apps/aflow_app/server/tests/test_control_plane_api.py`,
  `apps/aflow_app/web/src/api.test.ts` and
  `apps/aflow_app/web/src/components/RunDashboard.test.tsx`.
- Must not touch: `build_context_bundle` output, context authorization,
  `CheckpointHistory` evidence projection, restart/control semantics, stream
  authentication, unrelated UI layout, live services or shared state.
- Add a canonical event-page result carrying `events`, `has_older` and an
  exclusive `next_before_sequence`. Keep the existing tuple-returning
  `tail_events`/`poll_events`/`ControlPlaneService.events` adapters for the
  stream and existing callers; add the page form for the HTTP tail route.
  `before_sequence` selects events with sequence strictly less than the
  cursor; adjacent pages must not overlap. Reject a request containing both
  cursors with the existing validation/error mechanism. Preserve the current
  bounded journal read and `after_sequence` stream behavior.
- If the dispatch gate passes, extend `EventTailResponse` and `RunEventTail`
  with defaulted page metadata so existing stream frames and clients remain
  compatible. The HTTP route may accept an exclusive `before_sequence`; the
  SSE route remains after-cursor-only. Set the initial page and older-page
  size from the measured gate (8 is only the current experiment hypothesis).
  Keep the existing 100-event client window/resource cap and deduplicate by
  sequence; do not claim access beyond the server's bounded journal read.
- Split `loadSelectedRun` ownership so the status response can commit the
  selected summary as soon as it resolves; the event page updates the timeline
  independently under the same selected-run/request guard. A failed event
  page leaves status and already accepted events visible and sets the existing
  stale-timeline notice. Older-page requests use a separate bounded loading
  state and are ignored after selection changes. Keep stream reconnects and
  current 10-second status refresh behavior unchanged.
- Dispatch gate: do not prescribe an event-page byte/event bound from the
  direct serializer observation. First measure the same `small-long-history`
  or `large-long-history` fixture's actual HTTP event endpoint, selected
  identity, request role and decoded/encoded/content-length fields. If that
  experiment does not show event-page critical-path impact, mark this
  checkpoint BLOCKED rather than implementing speculative pagination. If it
  passes, set the page/event and latency guardrails from that exact raw key;
  any 8-event/4,096-byte or 10% value is a future hypothesis, not current
  evidence.

**Steps:**

- [ ] Add repository, daemon/service and API tests for initial tail, exclusive
  older cursors, empty/short journals, cursor overlap, both-cursor rejection,
  sequence integrity, authentication, legacy/deleted/not-found behavior and
  unchanged SSE `after_sequence` delivery.
- [ ] Implement the bounded event-page contract and default-compatible
  transport metadata; update the web API/types and dashboard page state,
  `Load older events` interaction and sequence deduplication without changing
  the context payload or stream auth/backoff contract.
- [ ] Add a delayed-response browser regression using the existing deferred
  test helpers: hold the initial event page while the status resolves, assert
  the selected run's useful summary is visible first, change selection while
  the old page is pending, release the old page, and assert it cannot appear
  in the new run's timeline. Also assert repeated older pages return all
  same-fixture event sequences without duplicates and that an event-page
  failure leaves status visible with a retry/stale notice.
- [ ] Run the web API/dashboard tests and production build, then repeat the
  isolated server profile and instrumented browser smoke on the same seed and
  fixture bounds. Inspect decoded page bytes, event sequences, request roles,
  status-before-event ordering, stream state and no-secret validation.

**Dependencies:**

- Checkpoint 1 is preferred for combined server measurements, but this
  checkpoint's event-page tests and transport work are independently
  verifiable. Do not broaden it into a run-list or context optimization.

**Verification:**

- Run: `uv run --frozen pytest -q tests/test_control_plane_repository.py apps/aflow_app/server/tests/test_control_plane_api.py`
- Run: `npm --prefix apps/aflow_app/web test -- --run src/api.test.ts src/components/RunDashboard.test.tsx`
- Run: `npm --prefix apps/aflow_app/web run build`
- Run: `uv run --frozen ruff check aflow/control_plane/persistence.py aflow/control_plane/repository.py aflow/daemon.py aflow/control_plane/models.py apps/aflow_app/server/src/aflow_app_server/models.py apps/aflow_app/server/src/aflow_app_server/control_plane_service.py apps/aflow_app/server/src/aflow_app_server/main.py`
- Observe only the gate supported by same-fixture evidence: status/summary can
  be accepted before a held event page, late old-run events are ignored, and
  older pages recover the full sequence without duplicates. No context, auth,
  stream or active-run semantics regress. Do not report a 4,096-byte bound
  until the corresponding HTTP raw observation exists.
- Before handoff, run `git status --short`, `git diff --name-only` and `git diff --stat`; changed files must stay within this checkpoint.

**Done When:**

- Selected event transport is bounded with a compatible, authenticated,
  cursorable older-page contract; useful summary ownership is race-safe; and
  same-fixture payload/latency evidence meets the stated guardrails.
- Every completed step is validated by focused tests, the build or retained
  isolated evidence, with no context optimization or browser fan-out claim
  inferred from CP2's inconclusive counterfactual.
- Verification passes and the changed files remain within scope.

**Blockers:**

- Stop and report if a compatible older-page contract cannot preserve event
  sequences or if stream reconnects can miss events under a held response.
- Stop and report if the existing full-context or authentication contract is
  required to change to make the event page smaller.

## Finding-to-source mapping

- Run-list projection: `aflow/control_plane/repository.py` (`_with_progress`,
  `get_run_status`, `list_history`), `aflow/daemon.py` (`run_status`) and
  `apps/aflow_app/server/src/aflow_app_server/control_plane_service.py`
  (`_status`, `list_runs`). Compare
  `.cp2.profile.fixtures[name].operations[name=list_runs].baseline.samples[]`,
  `.profiled.payload_bytes`, the named projection counts and
  `.cp2.comparisons.list_size.projection_gate`.
- Selected history: `aflow/control_plane/persistence.py` (`read_events`),
  `aflow/control_plane/repository.py` (`tail_events`), `aflow/daemon.py`
  (`poll_events`), server event models/routes and web `api.ts`, `types.ts` and
  `RunDashboard.tsx`. Compare the `selected_events` 8/80 operations for the
  same 100-run fixture and selected `run-0000`; use endpoint-level browser
  network records for HTTP sizes.
- Request fan-out and context body delivery remain observations mapped to the
  selected-run owners in `RunDashboard.tsx` and `api.ts`; they are not product
  work items without a new bounded causal experiment.

## Behavioral Acceptance Tests

- Given 100 and 1,000-run fixtures with the same selected run, when the project
  run list is read with the same history filter and limit, then row order,
  status/activity/evidence/progress, history metadata and cursors match the
  pre-change contract while final progress projection count is one per row.
- Given visible, archived, all-history, legacy and active-run records, when
  list and selected-status reads occur, then filtering, freshness, ownership,
  active warnings, continuation eligibility and authorization remain unchanged.
- Given an event-page gate that passes on a named same-fixture HTTP endpoint,
  when the dashboard opens and then requests older pages, then the measured
  page/event bound is respected and the union of accepted pages contains the
  fixture's sequences exactly once. Without that gate, the checkpoint is
  BLOCKED and no pagination claim is made.
- Given a held event response or an old selected-run response, when status for
  the current run resolves or selection changes, then useful current-run data
  remains visible and no late old-run status/events/notices overwrite it.
- Given an event-page failure, expired auth, missing run or deleted run, then
  existing error, session-expiry and deleted/not-found behavior remains
  visible without erasing the last authoritative status.

## Plan-to-Verification Matrix

| Requirement | Verification |
| --- | --- |
| One final status/progress projection per listed row | Focused repository/daemon/service call-count tests plus CP2-shaped cProfile JSON |
| History/status/ownership/auth semantics | Focused repository, daemon and control-plane API tests with visible/archived/all, active, legacy and failure cases |
| Exclusive event pagination and stream compatibility | Repository/API cursor tests and existing SSE after-cursor test |
| Bounded selected payload and useful-data ordering | Same-fixture profile, browser artifact inspection and delayed-response RunDashboard test |
| Stale response/error preservation | RunDashboard deferred-response tests and API failure tests |
| No cache/context/fan-out overreach | Diff scope review, unchanged context tests, `git diff --check` and report comparison |
| Portable, safe delivery | External artifact options, redaction assertions, build/Ruff, and status/name/stat checks |

## Assumptions And Defaults

- The CP2 source identity and fixture seed are authoritative for comparison:
  product source `304a39fd24b2d330d2533d041f3eb2776504086c`, seed `20260912`,
  100/1,000 runs, selected history 8/80, other history 2 and the recorded
  generated-file bounds. Re-run measurements on the implementation revision;
  do not compare mixed revisions as one sample.
- The existing 100-event client window and 1,000-event server read bound are
  safety/resource limits, not a promise that an unbounded journal is rendered
  at once. The explicit older-page control is the only new historical access
  path.
- Any CP1 20% list improvement or CP2 selected-route stretch is a measured
  target, not a universal SLA. Host load, Chromium version and uncontrolled
  remote networks must be reported separately; the CP2 event stretch is not
  actionable until its same-fixture gate passes.
- Context remains an intentional Diagnostics request with its current Lite /
  Full and `full_scope` semantics. No cache is introduced because CP2 only
  observed header presence/zero-transfer facts and did not measure
  invalidation correctness.
- Focused local tests, same-fixture profiles and the web build are worker-owned;
  full suites and CI are coordinator-owned. Use external temporary artifact
  directories supplied to runner options and never commit host-specific paths.
