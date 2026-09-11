# Cumulative review — readable run browser contract

Original and active plan: `plans/in-progress/readable-run-browser-contract-20260911.md`.
Branch: `aflow-readable-run-browser-contract-20260911-20260911-031806`.
Unchanged Pre-Handoff Base HEAD: `c786d8cbf27137b206549c021f5fcb825905c2d8`.
Reviewed HEAD: `1399bf86b9e0c89e64c759b8a480b1d1b9a6efed`.
Coverage: checkpoint 1, `cp1 v01`; 1 new commit, 1 total handoff commit.

## Findings and previous disposition

No material findings. This is the first review of this handoff; there are no prior findings or follow-up plans to resolve. The previous latest review belongs to earlier integrated work and is archived byte-for-byte.

Reviewed the complete base-to-HEAD diff, all four changed files, surrounding fixture/navigation/startup code, shipped run presentation and metadata, and the original plan and UI guidelines. The tests independently assert literal readable titles, exact run IDs in detail metadata and URL, and exact All runs project/status/title/ID rows. Fixture39 remains covered through project history, and the visible global fixture00 is explicitly selected. The startup filename expectations were demonstrably stale and their correction is within the bounded audit scope.

The cumulative diff preserves viewport/theme coverage, geometry, scrolling, focus, hit tests, drafts, exact mutation payloads and revisions. It introduces no production changes, sleeps, timeout inflation, skips or relaxed geometry assertions. Source search for `run-detail h3|long-plan-39|Test project · Completed` found no remaining stale filename-as-run-heading assertions; remaining filename selectors refer to plan-editor rows or retained exact technical paths. Applied the material finding admission gate, exclusions and proportionate-fix discipline; no candidate requires a fix.

## Verification

External evidence root: `/root/code/evidence/aflow-dogfood-20260909/readable-run-browser-review-20260911/`.

- `npm --prefix apps/aflow_app/web run build`: passed (`reviewer-build.log`).
- `AFLOW_TEST_BROWSER=chromium uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py apps/aflow_app/server/tests/test_run_navigation_browser.py apps/aflow_app/server/tests/test_plan_startup_browser.py --basetemp=/root/code/evidence/aflow-dogfood-20260909/readable-run-browser-review-20260911/reviewer-chromium`: 22 passed, 3 existing deprecation warnings (`reviewer-chromium.log`).
- `AFLOW_TEST_BROWSER=webkit uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py apps/aflow_app/server/tests/test_run_navigation_browser.py --basetemp=/root/code/evidence/aflow-dogfood-20260909/readable-run-browser-review-20260911/reviewer-webkit`: 21 passed, 3 existing deprecation warnings (`reviewer-webkit.log`).
- `git diff --check c786d8cb..HEAD`: passed.
- Inspected reviewer Chromium light and worker full2 WebKit dark 390×844 run-detail screenshots: readable Long plan 39 and exact responsive-run-39 are visible with header and Back controls. Physical mobile keyboard/browser-toolbar behavior was not exercised.

The worker receipt is available at `/root/code/agent_flow/.aflow/runs/20260911t031806z-0ef5f8cf/turns/turn-001/result.json`; it is absent from this worktree. Final worker full2 logs report 21 passes per engine. Earlier failed diagnostic logs remain retained; they do not substitute for the fresh passing verification above.

## Approval and lifecycle

Approved for the single cumulative handoff squash after the unchanged base, including this intentional already-tracked reviewer record. Preserve all reviewed test and DEVLOG blobs. Only one DEVLOG entry belongs to this handoff, so no compaction is needed. No fix plan is required or created. Ignored private plan/evidence/archive files are not force-added. The original plan remains in place for engine finalization; final approved SHA, exact commit count, blob preservation and clean tracked-state verification are recorded there after the squash.

The managed workflow owns merge/rm_worktree and publication to origin/main. Existing publication settings resolve to origin/main. Publication, exact-SHA CI and live activation are pending and are not claimed by this review. Shared tool installation, controllers and concurrent semantic-stop work were untouched.

No material findings
