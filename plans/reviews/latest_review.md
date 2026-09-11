# Recovery browser portability — cumulative approval review

Original and active plan: `plans/in-progress/recovery-browser-portable-artifacts-20260911.md`.
Review base (unchanged Pre-Handoff Base HEAD): `05bf725040bc03fee2a42eca151f609a8d82b10b`.
Reviewed HEAD: `c65b0dad414e202a5c3d55eb542685aa94633610`.
Branch: `aflow-recovery-browser-portable-artifacts-20260911-20260911-140026`.
Coverage: **2 new / 2 total commits**, checkpoint 1 in full: `cp1 v01`
(`8fef32c11a0b8603342f699cfc71f16d6720f775`) and `cp1 v01 fix`
(`c65b0dad414e202a5c3d55eb542685aa94633610`).

## Findings and previous disposition

No material findings. No prior findings or follow-up overlays belong to this
handoff; the previous latest report concerns other handoffs. Applied the material
finding admission gate, exclusions and proportionate-fix discipline to the full
base-to-HEAD diff and surrounding test code.

The only executable change adds pytest's existing tmp_path fixture to the journey
and derives evidence_root from it. Screenshot naming, creation, printed artifact
references, recovery and identity assertions, standalone/controller/transport
checks are preserved. No production changes or permission workarounds exist.
DEVLOG has one accurate handoff entry, so compaction is unnecessary.

## Verification evidence

Reused the exact worker browser runs recorded in the parent repository at
`.aflow/runs/20260911t140026z-d06cc7d7/turns/turn-001/transport.stdout`
and its completed `result.json`. Both command completion records report exit 0:

```sh
AFLOW_TEST_BROWSER=chromium uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py::test_durable_recovery_ui_journey --basetemp=/root/code/evidence/aflow-dogfood-20260909/recovery-browser-portability-review-20260911/chromium
AFLOW_TEST_BROWSER=webkit uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py::test_durable_recovery_ui_journey --basetemp=/root/code/evidence/aflow-dogfood-20260909/recovery-browser-portability-review-20260911/webkit
```

Chromium: **1 passed in 7.32s**. WebKit: **1 passed in 7.85s**.
Each emitted three dependency deprecation warnings. Independently confirmed both
retained PNGs beneath each basetemp's `test_durable_recovery_ui_journ0/issue36-recovery/`.
Only documentation changed after these runs; no repetition is needed. Worker
built the unchanged web bundle once because this worktree lacked dist.

Independent source audit `rg -n '/root/code/evidence' apps/aflow_app/server/tests tests`
returned no matches. `git diff --check 05bf725040bc03fee2a42eca151f609a8d82b10b HEAD`
passed. Full suites remain CI-owned.

## Approval finalization

Approve and squash both commits into one unpublished handoff commit at the
unchanged base, including this tracked review record. Preserve all other reviewed
blobs. Record the final approved SHA and clean-state/count verification in the
ignored original plan, leaving it in place for engine finalization. No fix plan is
needed or stale handoff fix plan present. Private plans, archived review and
evidence are not force-added. Merge, origin/main publication, exact-SHA CI and
live activation are subsequent controller-owned gates, not claimed here.

Previous latest review rotated byte-for-byte to `plans/reviews/260911_1409.md`.

No material findings
