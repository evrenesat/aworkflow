# UI data-latency discovery

This report attributes the existing UI's useful-data delay under bounded
isolated fixtures. It contains no product optimization and makes no claim
that loading became faster. Values are observations, not SLAs.

## Scope and identity

- Product source SHA: 304a39fd24b2d330d2533d041f3eb2776504086c; browser runner SHA: 37f83864be6b97ff40e420e62b181e1e07414091; deployed SHA: 304a39fd24b2d330d2533d041f3eb2776504086c.
- Browser: chromium 151.0.7922.34; host: codex; seed: 20260912; profile seed: 20260912.
- Server profiles used disposable fixture repositories, fresh isolated service composition, an in-memory unit observer and serial calls. No systemd unit, workflow, shared AFlow installation, live state or cache was changed.
- Browser summaries are grouped by fixture, route and sample kind. The selected identity is held while run count or selected history changes; generated file counts remain in the machine-readable fixture metadata.
- The referenced owner workflow duration is recorded separately from measured request latency; the UI runner did not measure workflow execution. F1 selected-readiness observations and F5 document-relative/action-origin comparisons from the prior baseline are superseded; corrected samples use the browser action clock.

## Before-optimization browser baseline

Useful-data readiness requires an actual record: selected identity plus an accepted direct status observation whose value agrees with the rendered status, checkpoint summary values from the selected run, and a completed Refresh request generation containing list/status/events JSON plus accepted current rendering. Empty headings, old DOM, loading panels, unrelated mutations and response arrival alone are rejected.
Refresh dependency attribution follows refreshPage awaits loadDashboard; loadDashboard awaits its Promise.all and the committed config projection before loadSelectedRun: GET /api/config and POST /api/config/form, along with the loadDashboard readiness/capability/plan prerequisites, are blocking prerequisites; selected list/status/events are displayed-data requirements; context, stream and restart metadata remain optional. In the retained warm Refresh samples, the median counts are 3 displayed-data requests, 4 blocking prerequisites, 7 action-required requests total and 1 optional requests. /ready is mapped but omitted from these counts because the retained api_request_flow is API-only.
A bounded real-fixture regression held GET /api/config after the Refresh list JSON completed: selected status/events did not start and useful Refresh did not accept stale data until configuration was released; the released GET/POST configuration pair was followed by current selected status/events and accepted useful content. Initial selected-route configuration remains optional in the mapping because this correction is scoped to the Refresh owner.

The corrected baseline contains five warm samples per route for each isolated 100/1000-run and 8/80-event fixture, separately labelled fresh-process samples, and a bounded ten-cycle in-app selection capture. It does not call document reloads listener-accumulation evidence. Browser decoded bytes, HTTP content-length/encoded bytes and direct canonical serializer bytes are different measurements.

### Corrected fixture scaling

| Fixture | Route | Runs | Selected events | Warm n | Useful median | Decoded API body median | HTTP/encoded body median | Errors |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| large-long-history | all_runs | 1000 | 80 | 5 | 24165.4 ms | 2338830.0 B | 2355012.0 B | 0 |
| large-short-history | all_runs | 1000 | 8 | 5 | 24119.8 ms | 2339830.0 B | 2339830.0 B | 0 |
| small-long-history | all_runs | 100 | 80 | 5 | 2150.0 ms | 234144.0 B | 247882.0 B | 0 |
| small-short-history | all_runs | 100 | 8 | 5 | 2175.8 ms | 234244.0 B | 234244.0 B | 0 |
| large-long-history | project_runs | 1000 | 80 | 5 | 2678.1 ms | 250571.0 B | 250571.0 B | 0 |
| large-short-history | project_runs | 1000 | 8 | 5 | 2690.7 ms | 250677.0 B | 250677.0 B | 0 |
| small-long-history | project_runs | 100 | 80 | 5 | 2325.5 ms | 250565.0 B | 250565.0 B | 0 |
| small-short-history | project_runs | 100 | 8 | 5 | 2368.8 ms | 250671.0 B | 250671.0 B | 0 |
| large-long-history | selected_run_summary | 1000 | 80 | 5 | 2922.1 ms | 261107.0 B | 261107.0 B | 0 |
| large-short-history | selected_run_summary | 1000 | 8 | 5 | 2908.6 ms | 241555.0 B | 241555.0 B | 0 |
| small-long-history | selected_run_summary | 100 | 80 | 5 | 2641.1 ms | 261101.0 B | 261101.0 B | 0 |
| small-short-history | selected_run_summary | 100 | 8 | 5 | 2596.0 ms | 241549.0 B | 241549.0 B | 0 |
| large-long-history | checkpoint_history | 1000 | 80 | 5 | 8373.8 ms | 552461.0 B | 552461.0 B | 0 |
| large-short-history | checkpoint_history | 1000 | 8 | 5 | 8186.2 ms | 493702.0 B | 493702.0 B | 0 |
| small-long-history | checkpoint_history | 100 | 80 | 5 | 7236.5 ms | 552449.0 B | 552449.0 B | 0 |
| small-short-history | checkpoint_history | 100 | 8 | 5 | 7164.8 ms | 493690.0 B | 493690.0 B | 0 |

## Server profiles and causal measurements

list_runs was profiled because the isolated browser waterfall identified the run-list path as a repeated backend cost. Each row reports a median derived from successful raw calls; the cProfile call is overhead-bearing and retained separately.

| Fixture / operation | Core median | Profiled CPU | Direct serializer bytes | Python calls | Repository _with_progress | Nested summary/detail | Reads / stats |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| small-short-history / list_runs | 678.5 ms | 2598.2 ms | 232448 B | 3754907 | 300 | 300 / 300 | 400 bytes / 700 text / 24327 stat |
| small-short-history / list_plans | 5.2 ms | 5.5 ms | 143 B | 5490 | 0 | 0 / 0 | 0 bytes / 0 text / 38 stat |
| small-short-history / selected_detail | 7.7 ms | 14.7 ms | 2323 B | 19073 | 2 | 2 / 2 | 1 bytes / 4 text / 121 stat |
| small-short-history / selected_events | 13.0 ms | 27.3 ms | 2161 B | 31794 | 3 | 3 / 3 | 3 bytes / 7 text / 208 stat |
| small-short-history / selected_context_lite | 16.5 ms | 28.0 ms | 6791 B | 38431 | 2 | 2 / 3 | 3 bytes / 6 text / 187 stat |
| small-short-history / selected_context_full | 14.1 ms | 25.4 ms | 6791 B | 38423 | 2 | 2 / 3 | 3 bytes / 6 text / 187 stat |
| small-long-history / list_runs | 747.1 ms | 2643.9 ms | 232349 B | 3790701 | 300 | 300 / 300 | 400 bytes / 700 text / 24327 stat |
| small-long-history / list_plans | 3.5 ms | 5.4 ms | 143 B | 5490 | 0 | 0 / 0 | 0 bytes / 0 text / 38 stat |
| small-long-history / selected_detail | 8.7 ms | 16.7 ms | 2323 B | 20955 | 2 | 2 / 2 | 1 bytes / 4 text / 121 stat |
| small-long-history / selected_events | 15.7 ms | 38.7 ms | 21814 B | 68010 | 3 | 3 / 3 | 3 bytes / 7 text / 208 stat |
| small-long-history / selected_context_lite | 17.7 ms | 46.4 ms | 26444 B | 99335 | 2 | 2 / 3 | 3 bytes / 6 text / 187 stat |
| small-long-history / selected_context_full | 17.3 ms | 45.1 ms | 26444 B | 99335 | 2 | 2 / 3 | 3 bytes / 6 text / 187 stat |
| large-short-history / list_runs | 1000.0 ms | 3118.5 ms | 232454 B | 4710699 | 300 | 300 / 300 | 400 bytes / 700 text / 29727 stat |
| large-short-history / list_plans | 3.5 ms | 5.3 ms | 143 B | 5490 | 0 | 0 / 0 | 0 bytes / 0 text / 38 stat |
| large-short-history / selected_detail | 7.1 ms | 14.6 ms | 2323 B | 19073 | 2 | 2 / 2 | 1 bytes / 4 text / 121 stat |
| large-short-history / selected_events | 11.5 ms | 21.1 ms | 2161 B | 31794 | 3 | 3 / 3 | 3 bytes / 7 text / 208 stat |
| large-short-history / selected_context_lite | 12.9 ms | 22.6 ms | 6791 B | 38423 | 2 | 2 / 3 | 3 bytes / 6 text / 187 stat |
| large-short-history / selected_context_full | 11.3 ms | 25.9 ms | 6791 B | 38423 | 2 | 2 / 3 | 3 bytes / 6 text / 187 stat |
| large-long-history / list_runs | 984.8 ms | 3230.3 ms | 232355 B | 4746483 | 300 | 300 / 300 | 400 bytes / 700 text / 29727 stat |
| large-long-history / list_plans | 4.6 ms | 5.4 ms | 143 B | 5490 | 0 | 0 / 0 | 0 bytes / 0 text / 38 stat |
| large-long-history / selected_detail | 8.1 ms | 15.5 ms | 2323 B | 20945 | 2 | 2 / 2 | 1 bytes / 4 text / 121 stat |
| large-long-history / selected_events | 16.3 ms | 39.8 ms | 21814 B | 68010 | 3 | 3 / 3 | 3 bytes / 7 text / 208 stat |
| large-long-history / selected_context_lite | 17.1 ms | 48.2 ms | 26444 B | 99335 | 2 | 2 / 3 | 3 bytes / 6 text / 187 stat |
| large-long-history / selected_context_full | 17.3 ms | 49.1 ms | 26444 B | 99335 | 2 | 2 / 3 | 3 bytes / 6 text / 187 stat |

For direct fixtures small-short-history and large-short-history, disabling only the additive projection changes core medians from 678.5 ms to 503.4 ms (25.8% lower), and from 1000.0 ms to 704.0 ms (29.6% lower). The patched behavior was never retained.
Changing only the list fixture changes core wall by 47.4% and profiled Python calls by 25.4%. The repository projection count is one named _with_progress entry point; nested summary/detail counts are visible separately and are not added.
Projection gate: measured before = 300 repository calls for the 100-row page; future target = 100; the no-projection counterfactual is 0. This is a measurement, not a claim that the current product meets the target.

Selected-history scaling holds the list fixture and selected run identity constant while varying only selected event count:

| Operation | Short core | Long core | Core change | Short serializer bytes | Long serializer bytes | Byte change |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| selected_events (8→80 events) | 13.0 ms | 15.7 ms | 20.6% | 2161 B | 21814 B | 909.4% |
| selected_context_lite (8→80 events) | 16.5 ms | 17.7 ms | 7.3% | 6791 B | 26444 B | 289.4% |
| selected_context_full (8→80 events) | 14.1 ms | 17.3 ms | 22.4% | 6791 B | 26444 B | 289.4% |

The selected-events and context measurements support a scale-sensitive experiment, but no optimization is executed here. No lock/queue contention probe was added because isolated calls were serial.

## Browser sequencing, rendering and cache observations

- The runner records latest-request generation, accepted useful rendering, overlap-aware completion, paint/long-task/measure metadata, and fixture identity without retaining response bodies. Required barriers and optional-after-useful classifications use browser action-relative timestamps; Python elapsed durations remain separately labelled.
- per-cycle in-app selection captures retain request/error counts; this bounded ten-cycle sample does not establish monotonic listener/request growth. The paired optional-request capture is a request-ownership experiment only; no browser optimization is proposed from it.
- Cache observation covered 1962 completed API responses: cache-control on 160, age on 0, and etag on 0; 21 had zero recorded transfer bytes. Header values are not retained and cache hits were not inferred.

## Ranked findings

1. Run-list history scanning and additive progress projection (high confidence; estimated medium fix).
   Contribution: Unprofiled list_runs core median is 678.5 ms at 100 runs and 1000.0 ms at 1000 runs; the in-memory no-progress counterfactual reduces those medians by 25.8% and 29.6%.
   Affected routes/data: all_runs, project_runs, selected_run_summary, checkpoint_history; 100 versus 1000 runs; selected run identity and selected history are held at run-0000 and 8 events.
   Causal evidence: list-size profile plus restored in-memory progress-disabled counterfactual
   Source mapping: aflow/control_plane/repository.py:379 list_history; aflow/control_plane/repository.py:228 get_run_status; aflow/control_plane/repository.py:190 _with_progress; aflow/control_plane/run_progress.py:5137 project_run_progress_summary; apps/aflow_app/server/src/aflow_app_server/control_plane_service.py:252 list_runs
   Follow-up status: proposed for the separate plan.

2. Selected history increases event/context work and payload (high confidence; estimated small-to-medium fix).
   Contribution: At 100 runs, selected_events core median changes from 13.0 ms for 8 events to 15.7 ms for 80, while canonical serializer bytes grow from 2161 to 21814.
   Affected routes/data: selected_run_summary, checkpoint_history; small-short-history versus small-long-history; 100 runs and selected run run-0000 held constant.
   Causal evidence: event-count counterfactual fixture with list size and selected identity held constant
   Source mapping: aflow/control_plane/persistence.py:490 read_events; aflow/control_plane/persistence.py:608 build_context_bundle; apps/aflow_app/server/src/aflow_app_server/control_plane_service.py:287 events; apps/aflow_app/server/src/aflow_app_server/control_plane_service.py:304 context
   Follow-up status: proposed for the separate plan.

3. Selected-route eager duplicate/support request fan-out (medium confidence; estimated medium fix).
   Contribution: Corrected browser coverage records 4 duplicate API paths for the selected summary and 7 for checkpoint history in the retained/paired observation; the runner now records each in-app selection cycle separately, so this remains an observation rather than a claimed optimization cause.
   Affected routes/data: selected_run_summary, checkpoint_history; four isolated fixture scales/shapes, five warm samples per route and a ten-cycle in-app selection bound.
   Causal evidence: corrected paired optional-request capture; no useful-data improvement is dispatched from the variable counterfactual
   Source mapping: apps/aflow_app/web/src/components/RunDashboard.tsx:1343 loadDashboard; apps/aflow_app/web/src/components/RunDashboard.tsx:1439 loadSelectedRun; apps/aflow_app/web/src/components/RunDashboard.tsx:1304 subscribeToRunEvents; apps/aflow_app/web/src/components/RunDashboard.tsx:1628 loadContext
   Follow-up status: observation only; do not dispatch as an optimization without the plan gate.

4. Context response body delivery is network/client-dominant in the waterfall (medium-high confidence; estimated unknown fix).
   Contribution: Corrected browser Resource Timing retains encoded/decoded body and header-size observations separately; direct Python context profiles remain a separate canonical-serializer measurement, so no Python optimization is inferred from browser transfer time.
   Affected routes/data: selected_run_summary, checkpoint_history; isolated Chromium waterfall grouped by fixture and route; no remote network claim.
   Causal evidence: browser Resource Timing versus direct service profile; no Python optimization is justified by this evidence
   Source mapping: scripts/perf/profile_ui_data_load.py:BrowserCapture; apps/aflow_app/web/src/api.ts:55 fetchJson
   Follow-up status: observation only; do not dispatch as an optimization without the plan gate.

Plausible hypotheses ruled out or left explicitly unresolved:

- browser main-thread long tasks or named React/parse commit work: corrected instrumented Chromium samples retain paint entries, long-task observations and named measures by fixture; no forced main-thread finding is claimed without a positive sample Status: not evidenced in this bounded run.
- lock, queue or event-loop contention: all direct profiles were serial over an in-memory unit observer Status: not measured; do not claim absence under concurrent remote load.
- Git/subprocess work dominates read latency: profile IO counters show only bounded Git subprocess observations per request-shaped operation; no Git span appears among ranked hot functions Status: ruled out for this fixture read path.
- listener/request growth over the ten-cycle bound: per-cycle in-app selection captures retain request/error counts; this bounded ten-cycle sample does not establish monotonic listener/request growth Status: inconclusive beyond the measured in-app selection cycles.

## Follow-up handoff

The separate evidence-backed implementation plan is at plans/in-progress/ui-data-latency-improvements-20260912.md. Isolated implementation dispatch is ready for review; the owner-facing baseline is explicitly BLOCKED until the missing authorized URL/credential/reference identity measurement is supplied. This discovery worker did not execute that plan.

## Reproduction and evidence

The machine-readable baseline retains corrected browser raw samples plus a superseded_evidence copy of the prior canonical evidence (52 grouped summaries); prior selected-readiness, mixed-clock timing and Refresh optional-role metrics are audit-only. The cp2 object contains all sanitized profile samples, cProfile rows/top functions, fixture-grouped browser observations, paired counterfactual, comparisons, findings and commands. Response/request bodies, auth material, cookies, prompts, transcripts and private fixture paths are absent.

    uv run --frozen --project apps/aflow_app/server python scripts/perf/profile_ui_data_load.py --scenario isolated-smoke --repeats 1 --navigation-cycles 0 --artifact-dir <external-artifact-dir> --report-dir <external-smoke-report-dir>
    uv run --frozen --project apps/aflow_app/server python scripts/perf/profile_ui_data_load.py --scenario isolated --seed 20260912 --repeats 5 --navigation-cycles 10 --artifact-dir <external-browser-artifact-dir> --report-dir <external-browser-report-dir>
    uv run --frozen --project apps/aflow_app/server python scripts/perf/profile_ui_data_load.py --scenario isolated --seed 20260912 --repeats 1 --navigation-cycles 0 --block-optional-requests --artifact-dir <external-browser-pair-counterfactual-artifact-dir> --report-dir <external-browser-pair-counterfactual-report-dir>
    uv run --frozen --project apps/aflow_app/server python scripts/perf/profile_control_plane_reads.py --scenario isolated --product-source-sha 304a39fd24b2d330d2533d041f3eb2776504086c --seed 20260912 --repeats 5 --artifact-dir <external-profile-artifact-dir>
    uv run --frozen --project apps/aflow_app/server python scripts/perf/analyze_ui_data_latency.py --baseline <external-browser-report-dir>/baseline.json --profile <external-profile-artifact-dir>/control-plane-profile.json --browser-baseline <external-browser-pair-normal-report-dir>/baseline.json --browser-counterfactual <external-browser-pair-counterfactual-report-dir>/baseline.json --product-source-sha 304a39fd24b2d330d2533d041f3eb2776504086c --report-dir plans/research/ui-data-latency-20260912

Validation: secrets absent = True; product source mutated = False; live profile = False; raw response bodies retained = False; raw timing samples retained = True.
Live coverage: BLOCKED — authorized owner URL, in-memory credential and exact reference run identity. Four-second symptom status: four-second owner-facing symptom remains unmeasured.

The next implementation worker must repeat focused same-fixture measurements and semantic regressions from the separate plan. Full suites, serialized integration, publication, CI and live activation remain coordinator-owned.
