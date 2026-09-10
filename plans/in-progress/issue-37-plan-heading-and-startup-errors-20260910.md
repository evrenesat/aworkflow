# Recognize numbered Git Tracking and explain startup failures (issue 37)

## Summary

Issue37 reproduces an unchecked plan with `## 3. Git Tracking` and blank reserved fields being rejected as not pristine, while the UI calls it Ready — runnable and shows only operation_rejected. Preserve plan history, recognize the unambiguous numbered heading, and expose safe actionable startup validation instead of requiring shell logs. Source: https://github.com/evrenesat/aworkflow/issues/37 .

## Git Tracking

- Plan Branch: `aflow-issue-37-plan-heading-and-startup-errors-20260910-20260910-222325`
- Pre-Handoff Base HEAD: `fdc9ff2c7ac7593383a0856f9d2c82cb09ad3f9c`
- Review Log:
  - 2026-09-10: Reviewer approved checkpoint 2 through `cp2 v01` after the cp03-v01 repair overlay, reviewing the uncommitted worktree against approved cp1 `c0afa1f`. Both prior findings resolved. Verification: 56 core/daemon, 67 HTTP/MCP/auth, 110 parser/retry/library tests plus 23 subtests; Ruff and diff check passed. Checkpoint 3 remains pending.
  - 2026-09-10: Reviewer rejected checkpoint 2 v01 using the uncommitted worktree against `c0afa1f` (approved cp1 v01). Required suites passed (51 core/daemon and 64 API/MCP/auth), but blanket Git Tracking guidance misdirects malformed-plan correction and broad catches classify infrastructure errors as plan errors. Checkpoint 2 remains unapproved; repair overlay is `issue-37-plan-heading-and-startup-errors-20260910-cp03-v01.md` (filename supplied by controller, scope is checkpoint 2). No approval commit or history rewrite.
  - 2026-09-10: Reviewer approved checkpoint 1 through `cp1 v01` using the worktree against the pre-handoff base (no checkpoint commit existed). Required parser/runtime verification passed with process-local Git isolation; checkpoints 2 and 3 remain pending.

## Done Means

Canonical and unambiguous integer-dot-prefixed level-two Git Tracking headings use the same parser/startup rules, without rewriting stored bytes just to normalize the heading. Duplicate/conflicting metadata remains rejected. Known plan-validation rejection reaches the UI as a bounded safe explanation. Validation that precedes allocation creates no run; if an existing startup boundary has already reserved a run, preserve and return that exact identity with the safe rejection. Ready labels describe workflow state, not unperformed validation. The maintained plan skill recommends the canonical unnumbered heading.

## Critical Invariants

Preserve all field values, fences, checkpoint/review history, revision checks, resume and pristine-bootstrap safeguards. Numbered recognition does not authorize resetting metadata or inserting a second section. No phantom run on rejected pre-allocation start. An already-reserved failed preparation must remain identifiable and idempotent; never delete its history or allocate a second run merely to report the error. Never expose arbitrary exception strings, secrets, absolute private paths or raw plan contents through the API.

## Forbidden Implementations

No global regex accepting arbitrary headings, silent history reset, new validation service/store, duplicated startup validation in React, broad exception-detail exposure, or automatic plan launch. No public issue/comment writes; use the existing issue as read-only context.

## Checkpoints

### [x] Checkpoint 1: Recognize one numbered reserved heading consistently

**Goal:** The reported otherwise-valid plan has the same semantics as its canonical spelling.

**Context:** Read nearest AGENTS; inspect `aflow/plan.py` GIT_TRACKING_RE and all heading discovery/parser/mutator/pristine helpers; `aflow/workflow.py` required tracking pre-allocation preparation; `tests/test_plan.py`, `tests/test_runtime.py`, and parser/startup tests discovered by rg.

**Scope:** Shared heading recognition and focused plan/startup tests. No UI or unrelated Markdown parser changes.

**Steps:**

- [x] Extend the single shared level-two heading matcher to accept an optional ASCII integer followed by a dot and whitespace before Git Tracking (for example `## 3. Git Tracking`). Retain canonical behavior and existing fence handling. Do not broaden to arbitrary numbered headings or change source bytes merely to canonicalize presentation.
- [x] Ensure section presence/count, metadata parsing, field updates and pristine insertion all use the same recognition. Canonical plus numbered duplicate sections must fail through existing ambiguity rules; numbered populated metadata remains subject to the same branch/base/history checks.
- [x] Add canonical/numbered parity, fenced examples, duplicates, populated tracking, malformed prefix and nonpristine history cases. Exercise the reported blank-field/unstarted plan through pre-allocation startup with a disposable repo, showing valid allocation only after validation; rejected ambiguous cases allocate nothing.

**Dependencies:** None.

**Verification:** `uv run pytest -q tests/test_plan.py tests/test_runtime.py --tb=short`; run separately discovered startup parser module if applicable; `uv run ruff check aflow`; `git diff --check`.

**Done When:** Reported numbering works without weakening metadata safety or modifying unrelated source bytes. Report scoped Git status/diff.

**Blockers:** Ambiguous sections remain rejected; do not choose one by order.

### [x] Checkpoint 2: Carry safe plan admission explanations to HTTP clients

**Goal:** Known validation failures are actionable while arbitrary failures remain redacted.

**Context:** Inspect `aflow/daemon.py` pre-allocation plan preflight (currently wraps as DaemonError), workflow/plan errors, and `apps/aflow_app/server/src/aflow_app_server/main.py` rejected_operation_handler and existing structured error handlers. Inspect API/MCP error parity and authorization tests.

**Scope:** One narrow typed plan-admission error contract and its propagation through existing admission/server handling; related tests. No general exception framework or unrelated error aggregation.

**Steps:**

- [x] Give known plan-format/tracking admission failures a stable code and fixed safe corrective message using a narrow typed error (reuse an existing suitable type or extend the existing plan/startup error boundary). Annotate the source validation cases; do not infer categories by parsing arbitrary exception text. Include only safe requirement/correction text, bounded to300 characters, and preserve the detailed cause in private logs.
- [x] Preserve that typed failure through daemon/control-plane pre-allocation handling. Return HTTP422 with the existing structured detail code/message shape understood by ApiError. Keep all unrelated ValueError/DaemonError cases generic; do not echo path, branch value, submitted plan, tokens or arbitrary causes. If the validator already supplies several safe issues, retain that existing bounded list; do not build a new aggregator.
- [x] Test malformed/duplicate tracking rejection yields the specific safe correction, no launch/run allocation when validation precedes reservation, and no secrets/path sentinels in response. Cover already-reserved tracking/preflight failure separately: preserve its exact run ID and current failed-preparation state through existing HTTP/MCP error/result contracts, without another allocation on exact retry. Reuse the existing typed startup error boundary; no new storage or alternate lifecycle. Keep bearer authorization, idempotency, MCP mapping and generic-error redaction tests intact.

**Dependencies:** CP1.

**Verification:** `uv run pytest -q tests/test_control_plane_services.py tests/test_control_plane_resume.py --tb=short`; `uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_control_plane_api.py apps/aflow_app/server/tests/test_mcp.py apps/aflow_app/server/tests/test_auth.py`; `uv run ruff check aflow apps/aflow_app/server/src`; `git diff --check`.

**Done When:** An authenticated client can correct a known format rejection without shell logs, and arbitrary exception information remains private.

**Blockers:** Missing safe classification must fall back to the current generic error, not raw exception disclosure.

### [ ] Checkpoint 3: Show truthful plan state and actionable startup feedback

**Goal:** Browser users understand Ready and can correct a rejected start.

**Context:** Read web AGENTS/UI_GUIDELINES. Inspect PlanPanel.tsx badge, NewRunPage/RunDashboard error rendering, api.ts ApiError detail.message handling, bundled aflow-plan skill/templates, existing real-server browser fixtures.

**Scope:** Minimal label/error rendering changes, regression/browser tests, maintained bundled plan instructions and concise docs/DEVLOG. Preserve responsive shell/scroll contract; no new UI validator or modal workflow.

**Steps:**

- [ ] Replace folder-derived `Ready — runnable` with accurate Ready wording and brief contextual guidance that startup checks occur when starting; do not imply a preflight was performed. Preserve Draft/Done action gating and existing state transitions.
- [ ] Display the safe structured startup message in the existing form error surface, preserving the draft and selections. If a run was already reserved, offer its exact existing detail link and make the failed attempt clear; do not imply nothing was created or auto-submit a replacement. Existing generic fallback stays for unclassified errors. Do not parse server logs or recreate Git Tracking parsing in React.
- [ ] Document the exact recommended reserved heading `## Git Tracking` without numbering in the maintained bundled aflow-plan skill and relevant template. Show canonical fields; note supported numbered input only where useful. No automatic changes to real installed/canonical skills.
- [ ] Add component/API parsing tests and a real Chromium isolated-server journey: numbered pristine plan starts through validated recognition; duplicate or malformed metadata is rejected before allocation with visible correction; user can edit and retry through normal revision-aware controls. Verify Ready wording does not promise prior validation. Retain exact history/field bytes.
- [ ] Update DEVLOG and relevant concise format/API/usage documentation in the same pass. Root AGENTS unchanged.

**Dependencies:** CP2; reconcile accepted responsive presentation before integration if it completed concurrently.

**Verification:** `npm --prefix apps/aflow_app/web test -- --run`; `npm --prefix apps/aflow_app/web run build`; run the real browser module containing the new journey explicitly; `uv run pytest -q tests/test_plan.py --tb=short`; `git diff --check`.

**Done When:** The issue reproduction is usable in the browser, errors are safe and actionable, and parser/history/authorization constraints remain verified. Publication, CI and live verification follow the existing delivery boundary.

**Blockers:** Missing browser prerequisites are an explicit verification gap, not a passing skip.

## Behavioral Acceptance Tests

A valid numbered heading behaves like canonical input; duplicate sections or contradictory history remain rejected. A rejected start creates no phantom run and shows a safe correction. Ready reflects plan state only. Canonical generated skill guidance remains exact.

## Plan-to-Verification Matrix

Parser/metadata safety: CP1. Safe pre-allocation response and auth: CP2. User correction and truthful status: CP3 real browser. Prevention: canonical skill/template instructions.

## Assumptions And Defaults

Choose recognition of the narrow integer-dot spelling rather than modifying existing plan bytes. Keep the canonical authoring convention. Use existing structured API errors and presentation; no new validation architecture or plan migration.

## Current dogfood evidence — 2026-09-10 22:22 UTC

Actual linked recovery preparation20260910t221450z-21700b3a failed with Pre-Handoff Base HEAD mismatch_started after main advanced during concurrent integration. MCP returned only operation_rejected although a manifest-only needs_attention attempt existed. Safe structured error plus exact reserved ID is required for this existing boundary; do not retroactively claim no allocation, delete its state or leak raw exception/absolute paths. Earlier resume also returned operation_rejected when its last saved snapshot was already complete, confirmed by read-only bootstrap. Add a fixed safe known-plan-state explanation where this existing boundary can classify it; do not change resume admission or edit historical snapshots.

Current hosted PlanPanel already shows concise Draft/Ready/Done in the two-row header, without the old Ready—runnable badge. CP3 should verify truthful wording and add only missing contextual guidance/error behavior; do not restore obsolete toolbar/revision-label layout. Preserve live responsive Skills, generated Changelog and verified parent/worktree grouping at integration. Canonical skill/template changes are source-only, never edits/install of real account skills. No GitHub posts/comments/messages or live/global/shared-tool/other-worktree changes.
