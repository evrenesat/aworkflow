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
