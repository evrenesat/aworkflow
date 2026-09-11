# Cumulative resume scope review — 2026-09-11

Branch: `aflow-cumulative-resume-scope-reconciliation-recovery-20-20260911-123621`.
Original plan: `plans/in-progress/cumulative-resume-scope-reconciliation-recovery-20260911.md`.
Active repair overlay reviewed: `cumulative-resume-scope-reconciliation-recovery-20260911-cp01-v01.md`.
Unchanged Pre-Handoff Base HEAD: `441796fbe90e8e197b00f00ad891373df1aac344`.
Reviewed HEAD: `1222829fe0e315cb23ee0ae71fc101d5962a6cf5`.
Full cumulative coverage: **1 new / 3 total commits**, adopted `cp1 v01` (998c04bb, matching c8df060d), `cp2 v01` (e38017e0), and `cp1 v02` (1222829f). Reviewed both original checkpoints, the repair, and all changed production code, tests, and documentation from the original base.

## Previous findings

F1 (P2, high confidence), introduced by e38017e0: resolved in 1222829f. Worker receipt paths no longer enter the premature metadata identity loop. Strict relocation-aware worker-path validation remains intact. The registered linked-worktree regression reaches exactly one reviewer with zero initial worker calls and unchanged predecessor hashes; an unmapped overlay path is rejected. No unresolved prior findings remain.

## Current findings

None admitted under the material-code-review gate. The cumulative implementation binds one-checkpoint progression to captured evidence, preserves source lineage and scoped routing constraints, and admits verified completed work directly to full review. Review rejection retains the repair overlay flow. Existing JSONL framing and recovery API production files are unchanged by the handoff.

## Verification evidence

Independently passed on reviewed HEAD:

```sh
uv run pytest -q tests/test_resume_pending_review.py tests/test_resume_relocation.py tests/test_resume_scope_reconciliation.py --basetemp=/tmp/aflow-review-v02-tests
# 50 passed
uv run ruff check aflow
# All checks passed
git diff --check 441796fb HEAD
# passed
```

Reused valid retained evidence from the prior cumulative review: daemon reviewer-step persistence/idempotency (1 passing node), REST/MCP parity (2 passing nodes), and the exact stale-CP2/final-worker-starts-CP3 approval probe. The follow-up only changes premature receipt path validation; the new linked-worktree positive/negative tests verify that correction. Retained CP1 proof covers resume-manager-budget/relocation (34), affected runtime (8), and manager-context (3) cases. Evidence resides under the private cumulative-scope-review-20260911 directory. Actual3492a076 eligibility diagnosis remains historical read-only evidence. CI owns full suites.

## Disposition

Approved for one accumulated squash onto the unchanged base. Consolidate the two handoff devlog entries, include this already-tracked reviewer record, remove the resolved fix overlay, and preserve all reviewed code/test blobs. The original ignored plan stays in place for engine finalization and records the final approved SHA after commit creation. No empty follow-up plan, private evidence, or ignored archive is added. Publication, CI, and live activation remain controller/coordinator delivery stages.

No material findings

---

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
