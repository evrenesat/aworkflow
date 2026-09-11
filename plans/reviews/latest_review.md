# Issue 40 — All runs loading cumulative approval

Original/active plan: `plans/in-progress/issue-40-all-runs-loading-20260911.md`.
Branch: `aflow-issue-40-all-runs-loading-20260911-20260911-144836`.
Unchanged Pre-Handoff Base HEAD: `7ffe5c6a5b3d51ab042e623e927d9e69a864bca6`.
Reviewed HEAD: `fddca378970f1f34171c436e426f8fa3f28efe37`.
Coverage: **1 new / 1 total commit**, `cp1 v01`, all of checkpoint 1 and the
complete base-to-HEAD implementation. No prior findings or fix overlays belong
to this handoff; the former latest record concerns other plans.

## Findings

No material findings under the admission gate, exclusions and proportionate-fix
discipline. Reviewed all five changed files, the global fetch/selection helpers,
registry owner in App, scoped guidance and UI acceptance requirements.
Initial registry and run requests suppress premature empty groups and counts;
completion is keyed to sorted project IDs and history. Abort guards, hidden-page
suspension, existing polling cadence, exact row identity, search and history
selection remain intact. Failure exits initial loading; partial success and
ordinary refresh retain rows. A visible CSS spinner, status text, aria-busy and
reduced-motion static presentation satisfy the requested loading behavior.
No production startup readiness or stop/recovery controls changed.

## Verification

Independent focused verification on the reviewed HEAD:

```sh
npm --prefix apps/aflow_app/web test -- --run src/components/GlobalRunOverview.test.tsx src/globalRuns.test.ts
# 16 passed across 2 files
git diff --check 7ffe5c6a5b3d51ab042e623e927d9e69a864bca6 HEAD
# passed
```

Reused final successful command-completion evidence from the parent repository's
`.aflow/runs/20260911t144835z-9d933c6f/turns/turn-001/transport.stdout`
and completed `result.json`. Final implementation was unchanged after these checks:

```sh
npm --prefix apps/aflow_app/web run build
# exit 0; tsc and Vite passed
AFLOW_TEST_BROWSER=chromium uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py::test_global_run_overview_loading_journey --basetemp=/root/code/evidence/aflow-dogfood-20260909/issue40-review-20260911/chromium-final2
# exit 0; 1 passed in 4.32s
AFLOW_TEST_BROWSER=webkit uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py::test_global_run_overview_loading_journey --basetemp=/root/code/evidence/aflow-dogfood-20260909/issue40-review-20260911/webkit-final2
# exit 0; 1 passed in 4.79s
```

Browser checks deterministically hold registry, initial runs and refresh responses;
assert animation and reduced-motion computed styles; verify populated, empty and
error outcomes. Independently inspected retained Chromium desktop loading and
WebKit phone refresh screenshots: status is visible, desktop pending has no empty
claims, and phone refresh preserves readable usable rows with compact headers.
Evidence stays in invocation-owned temp roots; portable test code uses tmp_path.
No full suite was run; CI owns full suites. Physical phone hardware was not tested.

## Disposition and finalization

Approve for one unpublished accumulated commit after the unchanged base, including
this tracked review record. Preserve the five reviewed implementation/DEVLOG blobs.
DEVLOG has one handoff entry, so no compaction is needed. No new fix plan is needed
and no stale issue-40 fix plan exists. Preserve the ignored original plan for engine
finalization and record the final approved SHA there after creating the commit.
Verify exactly one final commit, no changed implementation blobs and clean tracked
Git state. Publication, exact-SHA CI and live activation remain subsequent engine/
coordinator delivery gates. Preserve concurrent startup and stop/recovery work at
integration.

Prior review rotated byte-for-byte to `plans/reviews/260911_1507.md`; no ignored private
archive, plan or evidence is force-added.

No material findings
