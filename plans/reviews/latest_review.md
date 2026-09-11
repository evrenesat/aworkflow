# Cumulative review — clear run navigation and compact settings

Original plan: `plans/in-progress/clear-run-and-settings-ui-20260909.md`.
Reviewed overlay: `plans/in-progress/clear-run-and-settings-ui-20260909-cp01-v01.md` (resolved; removed on approval).
Branch: `aflow-clear-run-and-settings-ui-20260909-20260911-012048`.
Unchanged Pre-Handoff Base HEAD: `36c7b4838ce8ea597da920b282d0b6f2f7443986`.
Reviewed implementation HEAD: `d4856eaea9cf0cb85d3d0ce75e7bee946d9db43a`.
HISTORY: Prior reviewed HEAD: `b2ed5a78326b13741d260567cc729178d53a2e42`.
New commits: 1. Total handoff commits reviewed: 4.
Coverage: cp1 v01 `405e76cf`, cp2 v01 `214dd86e`, cp3 v01 `b2ed5a78`, and follow-up cp01 v01 `d4856eae`.

## Previous finding disposition

R1 (P2, high confidence) is resolved. GlobalSettings now ignores Enter already consumed by Combobox and uses the authoritative raw draft value for unhandled Enter. Component tests verify partial-query selection of `luna_max`, exact saved actions, duplicate selection, free-typed addition and no write before Save. Fresh Chromium and WebKit browser regressions select the suggestion from `luna`, add exactly `codex.luna_max`, and verify the saved TOML contains no `codex.luna` profile. The correction is confined to the demonstrated identity defect.

## Cumulative review

Reviewed the entire unchanged base-to-HEAD diff and surrounding contracts, including all three checkpoints and the follow-up, against the amended original plan and UI_GUIDELINES.md. Incremental review of R1 did not replace cumulative review. Covered readable plan names and exact identities; Ongoing/Recent/attention ordering, pagination and loaded-history search; selected-run progress, concise failure/report and recovery controls; compact profile/header presentation; draft retention, Add/Enter, duplicate validation and explicit Save; launch defaults, worker/reviewer summary, Details, preflight and startup/retry identities. Existing MCP authoring, startup errors, history mutations and progress projection remain preserved.

Applied the material finding admission gate, exclusions and proportionate-fix discipline. No new actionable material defect survived the gate. No unresolved earlier finding remains. No production code was changed by the reviewer.

## Fresh verification

Evidence root: `.aflow/review-clear-run-settings/repeat/`, relative to the execution root.

- `npm --prefix apps/aflow_app/web test -- --run`: 351 passed, 22 files (`web.log`).
- `npm --prefix apps/aflow_app/web run build`: passed before browser checks (`build.log`).
- `AFLOW_TEST_BROWSER=chromium uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_run_navigation_browser.py`: 3 passed (`chromium.log`).
- Same run-navigation command with `AFLOW_TEST_BROWSER=webkit`: 3 passed (`webkit.log`).
- `uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_settings_browser.py`: 6 passed (`settings.log`).
- Settings command with `AFLOW_TEST_BROWSER=webkit` and `-k profile_combobox_enter`: 1 passed, 5 deselected (`settings-webkit.log`).
- Browser commands used separate `--basetemp` directories beneath the evidence root, disposable projects/configuration/HOME and owned ephemeral servers/ports.
- `git diff --check`: passed.

Inspected fresh Chromium light 1440x900 launch, Chromium dark 390x844 launch, WebKit light 390x844 Projects, and Chromium light/WebKit dark 1280x900 profile-selection screenshots. Launch Details and primary actions are visible; ordinary mobile Projects stays on one readable line while long identity remains accessible. Profile rows retain labelled controls in document flow. Physical mobile keyboard/browser-toolbar behavior was not exercised. Run screenshots are beneath `chromium/test_complete_navigation_and_l0/` and `webkit/test_complete_navigation_and_l0/`; profile screenshots are beneath `.aflow/review-clear-run-settings/r1/`.

The supplied worker artifact `.aflow/runs/20260911t012048z-da841459/turns/turn-005/result.json` was absent from this execution root; the decision uses actual committed code and fresh independent verification.

## Disposition

Approved for the single cumulative handoff squash. All four checkpoint/fix commits are included after the unchanged base, together with this already-tracked review record. The prior dirty review record was archived byte-for-byte; ignored private plans/evidence are not force-added. The resolved overlay is removed, no empty replacement is created, and the original plan stays in place for engine finalization. DEVLOG has only one new handoff entry, so no compaction is necessary. Final approved SHA and squash-count/blob/clean-state verification belong in original-plan tracking, avoiding a self-referential commit SHA here. Publication, combined integration, exact-SHA CI and live activation remain coordinator-owned and are not claimed.

No material findings

---

Retained target-branch review (before integration):

# Cumulative passive-refresh approval — 2026-09-11

Original plan: `plans/in-progress/passive-run-refresh-errors-20260911.md`.
Reviewed overlay: `plans/in-progress/passive-run-refresh-errors-20260911-cp01-v01.md` (resolved and removed; retained in external evidence).
Review base: `36c7b4838ce8ea597da920b282d0b6f2f7443986` (unchanged).
Reviewed HEAD: `c5308abbff48d12ab8812c6c30e7538b60928b1b`.
Coverage: complete cumulative base-to-HEAD implementation, `cp1 v01` (`ef54675d`) and `cp1 v02` (`c5308abb`); 1 new / 2 total handoff commits. All original checkpoint steps are complete.

## Findings and previous disposition

No material findings. Previous R1 is resolved: Refresh routes unresolved/failed discovery through the existing refreshNonce effect. A controlled regression waits for a second discovery call, releases the deferred response and verifies run-owned UI recovery and alert clearance. Repeated discovery failure remains visible alongside an unrelated rejected control action. No earlier finding remains unresolved.

Reviewed all four changed files and surrounding discovery, dashboard/history, selected-run, subscriptions, action handlers, startup links and restart flows. Plain local error state preserves existing request/selection guards. The deferred late-refresh regression preserves exact project/run identity, revision 1/2 payloads and rejected draft13 through late delivery, independent read failure/recovery and retry. Browser geometry and exact target/descendant hit identity are observed together after real trial actionability. Browser payload/revision/draft assertions are unchanged. No API, persistence, issue35 compatibility, MCP authoring or presentation changes were introduced. No additional candidate met the finding admission gate; no optional hardening or broad refactor is required.

## Verification evidence

Fresh commands at reviewed HEAD:

- `npm --prefix apps/aflow_app/web test -- --run src/components/RunDashboard.test.tsx`: 94 passed.
- `npm --prefix apps/aflow_app/web test -- --run`: 341 passed across 22 files.
- `npm --prefix apps/aflow_app/web run build`: passed.
- `AFLOW_TEST_BROWSER=chromium uv run --project apps/aflow_app/server pytest -q 'apps/aflow_app/server/tests/test_responsive_browser.py::test_responsive_route_matrix[phone-landscape]' 'apps/aflow_app/server/tests/test_responsive_browser.py::test_responsive_live_controls_and_restart[phone-live-controls]'`: 2 passed.
- Identical exact cases with `AFLOW_TEST_BROWSER=webkit`: 2 passed.
- Browser processes used separate temporary homes, disposable fixture state and ephemeral localhost ports. Existing deprecation warnings only.
- `git diff --check` and cumulative diff check passed.

Private source/output: `/root/code/evidence/aflow-dogfood-20260909/passive-refresh-review-v02-20260911/` contains focused/full/build/browser logs and the historical proof sources/output. HISTORY: the current late-refresh regression against a temporary copy of the pre-handoff component fails exactly at the missing rejection alert after deferred delivery (`late-refresh-base.log`). Production source was never replaced; temporary proof files were removed. The earlier R1 proof and baseline comparison remain in `/root/code/evidence/aflow-dogfood-20260909/passive-refresh-review-20260911/`. Worker receipt at `/root/code/agent_flow/.aflow/runs/20260911t013142z-ee85e57b/turns/turn-003/result.json` identifies committed cp1 v02 and its verification; reviewer checks independently pass.

## Approval and lifecycle

Reviewer approved and initially squashed to `e0f7b32559a5667cc63d8ef00801a5c9c617a6c0`. Exactly one accumulated handoff commit follows the unchanged base; its parent equals the base. The initial squash contains the three scoped code/test files and compacted DEVLOG. All three code/test blobs are byte-for-byte identical to reviewed HEAD. Two handoff DEVLOG entries were compacted to one; no production edits were made during review. The previous review was rotated byte-for-byte to `plans/reviews/260911_0209.md`. Coordinator delivery recovery includes this already-tracked approval report in the unpublished final handoff commit so merge starts clean. Code, test and DEVLOG blobs remain identical to the approved squash; the publication receipt records the final delivery SHA. Ignored review archives remain outside the commit.

The original plan remains in place with approved tracking. The resolved fix overlay was removed; no v02 or empty follow-up plan was created. Engine owns plan finalization. Coordinator owns integration and combined checks with concurrent presentation changes, remote publication, exact-SHA CI and live activation; none is claimed by this review. Physical mobile keyboard behavior and the full original macOS timing remain unverified.

No material findings
