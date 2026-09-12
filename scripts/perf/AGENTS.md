# UI data-latency discovery runners

This directory contains bounded, read-only discovery tooling for the hosted
aflow UI. The runners may create disposable fixture repositories below a
temporary directory, but must not modify product source, live .aflow state,
shared AFlow installations, caches, or controller state.

profile_ui_data_load.py is the current entrypoint. Use an isolated scenario
for reproducible local evidence. Shareable collection requires a clean
committed runner source; product source and runner source are recorded
separately. Live profiling requires an explicit owner URL and an in-memory
credential supplied through an environment variable or an interactive prompt;
it is serial and capped at three warm navigations per route. Never record
authorization headers, cookies, request bodies, HAR bodies, prompts,
transcripts, or private fixture paths in shareable reports.

The runner's normal smoke command is:

    npm --prefix apps/aflow_app/web run build
    uv run --frozen --project apps/aflow_app/server python scripts/perf/profile_ui_data_load.py --scenario isolated-smoke --repeats 1 --navigation-cycles 0 --artifact-dir <external-artifact-dir> --report-dir <external-smoke-report-dir>

Full suites and remote/live runs are coordinator-owned. Discovery output is
evidence, not a product optimization or a claim that loading became faster.
Useful readiness requires actual selected identity/status data, returned
checkpoint values and an explicit current request generation whose selected
status and events responses complete before their accepted rendering is
observed. The transient direct status field must agree with the rendered
status label, so a stale list row cannot satisfy the selected milestone;
semantic DOM mutation is retained as timing evidence but is not required when
React reuses unchanged data.
Refresh additionally requires its latest list/status/events requests and
actual checkpoint rendering. An unchanged detail may satisfy Refresh when
the current direct status agrees with the selected identity; an unrelated
mutation or response arrival alone does not. Headings, empty/loading panels
and stale DOM remain rejected.

Browser fetch timestamps retain their document-relative `performance.now()`
values, plus action-relative fields derived from an explicit browser action
origin and accepted-useful marker. Analyzer barrier and optional-after-useful
comparisons use only those normalized fields; Python wall durations remain
separately labelled.

The isolated scale run uses 100/1,000-run fixtures, a stable selected
`run-0000`, newest visible global-row identity, independent selected history of
8/80 events, five warm samples, one fresh-process sample and ten in-app
selection cycles. It does not use document reloads as listener-growth
evidence. The larger run-list wait is a bounded runner timeout for useful
content, not a product or SLA target.

CP2 attribution uses profile_control_plane_reads.py for serial, in-memory
control-plane cProfile/pstats measurements and analyze_ui_data_latency.py to
merge those profiles with the instrumented browser baseline. Both are
isolated-only discovery tools: pass private artifact directories through
--artifact-dir, keep the product source SHA explicit, retain raw successful
per-call timings and explicit failures, and do not attach them to live
workers or shared controller state. Analyzer endpoint evidence is grouped by
fixture, route, sample kind and path; direct serializer, HTTP encoded and
browser decoded sizes are never conflated. Do not report p95/cold claims from
these bounded samples, and document unavailable owner-facing evidence as
BLOCKED rather than inferring it from loopback. The analyzer writes the
sanitized report and baseline under plans/research/; it does not change product
source.
