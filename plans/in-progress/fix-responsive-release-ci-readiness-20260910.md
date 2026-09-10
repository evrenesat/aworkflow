# Resolve responsive release CI readiness failures

## Summary

Exact published revision1153d35 failed GitHub CI34515798488 on macOS: GuidedConfigForm starter workflow remained empty when asserted, and the phone responsive control journey submitted expected_revision0 plus the already accepted team after revision1 acknowledgement. Preserve the responsive release and repair these concrete acceptance failures. Existing live release remainscd78d53 until this gate passes. Evidence log is /root/code/evidence/aflow-dogfood-20260909/ci-1153d35-failed.log; inspect only relevant sections.

## Git Tracking

- Plan Branch: `aflow-fix-responsive-release-ci-readiness-20260910-20260910-185405`
- Pre-Handoff Base HEAD: `1153d35f3b2c5b43950c04d24447535b8d4df022`

## Done Means

Deterministic delayed-loading/order evidence explains both failures, the exact starter and control payload assertions pass after a minimal correction, and the reviewed source is ready for exact-SHA CI. Do not claim macOS CI or deployment from Linux checks.

## Critical Invariants

Preserve default workflow implement and branch trunk from detected defaults, exact starter action/draft callbacks, server revisions, run/project identity, idempotency keys, submitted team/role-selector values, rejected-draft retention and restart behavior. Preserve the approved responsive layout and clipboard guard. DEVLOG remains concise. Concurrent changelog and lifecycle bookkeeping implementations are outside scope.

## Forbidden Implementations

No sleeps, timeout inflation, serial-only CI workaround, retry-until-green, assertion weakening, arbitrary delayed enabling, broad UI refactor, revision fabrication, durable state edits or new services. No GitHub posts/comments/messages, live service/global configuration/shared tool changes, force push or other active worktree edits.

## Checkpoints

### [x] Checkpoint 1: Establish and correct readiness at the two failed boundaries

**Goal:** Starter defaults and acknowledged live-control state are the basis of the next user action.

**Context:** Run hostname, pwd, git status --short; read nearest AGENTS and UI_GUIDELINES.md. Inspect apps/aflow_app/web/src/components/GuidedConfigForm.tsx and its test around 'builds a starter draft from the empty pair using detected Git defaults'; inspect RunDashboard.tsx handleControl, selectedRun/list/detail refresh ownership and controlOverride. Inspect apps/aflow_app/server/tests/test_responsive_browser.py route_control_plane and test_responsive_live_controls_and_restart, especially first successful save then rejected second save. Read CI evidence around the two named failures, not all run history.

**Scope:** Those two test modules; minimal directly demonstrated production readiness/state-ownership correction in GuidedConfigForm.tsx or RunDashboard.tsx if required; DEVLOG and relevant existing architecture only for actual production behavior changes. No new unrelated module.

**Steps:**

- [x] Reproduce starter ordering with deferred postGlobalConfigForm resolution and React effect settlement. The Build starter button can exist before detected defaults populate. Establish observable Workflow=implement and Main branch=trunk before clicking, using existing waitFor/act utilities; preserve exact action and draft callbacks. If the component remains correct under the controlled ordering, use a test-only correction.
- [x] Retain a controlled comparison for live control revision0→acknowledged1→second edit. Inspect all list/detail/status/SSE responses and route coverage; the current fixture intercepts list/detail and mutations while background endpoints may still expose its original durable revision0. Exercise a delayed old read settling after acknowledgement. Distinguish incoherent test fixture data from a reachable stale-response overwrite in production. Record exact request/settlement sequence and payloads; report non-reproduction honestly.
- [x] If only the fixture is inconsistent, make its existing response sources agree on the acknowledged run and wait for the actual admitted/settled state before the second edit. If real stale-response overwrite is reproduced, apply the smallest existing per-run request/identity or revision ordering guard; never assume revisions increase across different run IDs or discard a legitimate current snapshot without evidence. Retain deterministic regression source/output. No broader run-cache redesign.
- [x] Preserve exact first request expected_revision0/max_turns12/team fast__team/worker selector reasonix.new and second request expected_revision1/max_turns13 only, nonempty idempotency keys, visible rejection and retained13. Keep native-option labels, no-team option, resize/Back/navigation, exact restart/successor retry and geometry checks intact. Do not rewrite expected payload to accept stale state.
- [x] Run focused checks then required full web/build and real Chromium/WebKit responsive tests once after corrections. Reuse passing unrelated backend evidence; no complete core suite for test-only UI changes. If a new related failure appears, repair its proven cause within this boundary and report it. Record exact commands, durable evidence and scope in DEVLOG.

**Dependencies:** Approved responsive and clipboard integration already on current main. Independent of concurrent changelog/bookkeeping work; coordinator owns serialized integration.

**Verification:**

- `npm --prefix apps/aflow_app/web test -- --run src/components/GuidedConfigForm.test.tsx src/components/RunDashboard.test.tsx`
- `npm --prefix apps/aflow_app/web test -- --run`
- `npm --prefix apps/aflow_app/web run build`
- `uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py`
- `AFLOW_TEST_BROWSER=webkit uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py`
- `git diff --check`

Expected: controlled delayed defaults and acknowledged-revision order produce correct exact actions; full required tests pass, both browser engines actually execute, no retries/skips or unrelated suite repetition. Existing screenshots can be reused if presentation is unchanged; inspect fresh failure/changed-state captures if semantics change. Physical keyboard remains unverified.

**Done When:** Both exact CI failures are addressed with durable causal evidence and preserved assertions. Approval, remote CI and live activation remain distinct.

**Blockers:** A true unavailable environment must be reported with evidence; passing reruns cannot replace the ordering comparison. Do not manufacture a production defect to satisfy the plan.

## Behavioral Acceptance Tests

Delayed detected defaults eventually populate implement/trunk and the exact build action uses them. After a successful revision1 control response, the next edit submits revision1 and only the new max-turn change. A stale response case is either coherently corrected in fixtures or blocked with a proven minimal production guard. Rejected draft, clipboard and responsive interactions retain their semantics.

## Plan-to-Verification Matrix

Starter defaults: deferred component evidence and focused/full web suite. Control state: retained ordering evidence, exact payload assertions, real Chromium/WebKit hosted journeys. Delivery: coordinator exact-SHA CI and actual deployed desktop/mobile checks.

## Assumptions And Defaults

Keep the previous verified live release while CI fails. Current owner grant permits reviewed origin/main publication and existing CI-gated private deployment without another permission request. This repair has delivery priority; other independent implementation may continue, but do not publish unrelated results over a known failing gate.

## Review Log

- 2026-09-10: Checkpoint 1 initial attempt reviewed via worktree fallback at base `1153d35`; no checkpoint commit exists. No material code findings. Reviewer before/after comparison confirms the stale detail overwrite and repaired exact second payload; source/output retained in `plans/reviews/responsive-readiness-cp01-ordering.txt`. Approval withheld because required full web suite remains 313/315; isolated passes do not prove unrelatedness. Checkpoint and final verification step remain unchecked. Focused follow-up: `fix-responsive-release-ci-readiness-20260910-cp01-v01.md`. No history or publication action.
- 2026-09-10: The focused readiness follow-up added only a hosted `Runs` heading wait to the logout rejection journey and the existing control-admission wait to the colliding-role journey. Focused web tests passed 163, the required full web suite passed 315/315 across 18 files, the build passed, prior Chromium/WebKit responsive evidence remains valid at 11 tests each, and `git diff --check` passed. The final verification step is checked; Checkpoint 1 remains reviewer-owned and unapproved.

- 2026-09-10: Reviewer approves checkpoint 1 through `cp1 v02` using current-worktree fallback at base `1153d35`; no earlier checkpoint boundary exists for this repair. No material findings. Verified corrected 315/315 full-web and build receipts, reused applicable 11/11 Chromium and WebKit evidence, and retained the old-read/acknowledgement comparison. Active overlay remains unchanged. Original plan stays in progress pending coordinator publication, exact-SHA CI and live verification.
