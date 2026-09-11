# Pending-review fixture portability — cumulative approval

Original/active plan: `plans/in-progress/pending-review-fixture-git-isolation-20260911.md`.
Branch: `aflow-pending-review-fixture-git-isolation-20260911-20260911-143512`.
Unchanged review base: `44cd2579ba6304ac083f4c6b8f12ebb6f767a5dd`.
Reviewed HEAD: `deb7bc61a6400a919531a389addcb9721956a5fc`.
Coverage: **1 new / 1 total commit**, `cp1 v01`, entire checkpoint 1 and full base-to-HEAD diff.

## Findings and prior disposition

No material findings. No previous findings or fix overlays belong to this handoff;
the prior latest record concerned other plans. Applied the material admission
gate, exclusions, and proportionate-fix discipline to the full implementation.

Only tests/test_resume_pending_review.py and DEVLOG.md changed. The fixture
commits a narrow `.aflow/` ignore before workflow execution and sets local
core.excludesFile to an empty fixture-owned file. Linked worktrees inherit the
ignore and repository configuration. The commit helper explicitly records plan
backup/provenance files without broad ignores. All original assertions remain.
Production branch/HEAD/status and receipt validation, including the existing
config-pair lock exception, are unchanged. Evidence paths are invocation-owned.

## Verification

Independent verification on the reviewed HEAD:

```sh
GIT_CONFIG_GLOBAL=/dev/null uv run pytest -q tests/test_resume_pending_review.py --basetemp=/root/code/evidence/aflow-dogfood-20260909/pending-review-portability-review-20260911/reviewer-verification
# 12 passed in 4.15s
git diff --check 44cd2579 HEAD
# passed
```

This covers all four positive cases, changed-worktree rejection, and positive/
negative linked-worktree relocation. Parent-repository worker artifact
`.aflow/runs/20260911t143511z-949f808d/turns/turn-001/result.json`
also records a passing module and separate 4-positive/8-negative-relocation runs.
No full local suite was run; CI owns full suites.

## Finalization

Approve one accumulated unpublished commit at the unchanged base, including this
tracked review. Preserve reviewed implementation and DEVLOG blobs. DEVLOG has
one handoff entry and needs no compaction. No stale fix plan exists and no new
fix plan is needed. Keep the original ignored plan for engine finalization and
record the final SHA there after commit creation. Verify one final commit and
clean tracked state. Publication, CI and activation remain controller/coordinator
gates, not claimed by this review.

Prior review rotated byte-for-byte to `plans/reviews/260911_1445.md`. Do not force-add private artifacts.

No material findings

---

# Startup preflight readiness — cumulative approval review

Original and active plan: `plans/in-progress/startup-preflight-readiness-20260911.md`.
Branch: `aflow-startup-preflight-readiness-20260911-20260911-142854`.
Unchanged Pre-Handoff Base HEAD: `44cd2579ba6304ac083f4c6b8f12ebb6f767a5dd`.
Reviewed HEAD: `ac04c143ed8bf692d48877f5ecab59eee4383b17`.
Coverage: **1 new / 1 total commit**, all of checkpoint 1, `cp1 v01`.
No earlier findings or fix overlays belong to this handoff. The previous latest
report concerns other handoffs; no unresolved findings carry forward.

## Findings

None admitted under the material finding gate. Reviewed the complete base-to-HEAD
diff and surrounding launch, inspection, pagination, acknowledgment and startup
question code. Current request identity gates both presentation and launch;
pending inspection hides actionable acknowledgment, late responses are rejected,
and unrelated edits retain acknowledgment while requiring fresh inspection.
Errors and blockers still prevent launch. The browser helper waits for completed
inspection, including dirty new-worktree results without a checkbox. Server
rechecks and existing Ready/corrected-plan assertions remain intact. Concurrent
stop-after-turn and recovery controls are outside this patch and must be preserved
by the integration owner.

## Verification

Independently passed on reviewed HEAD:

```sh
npm --prefix apps/aflow_app/web test -- --run src/components/RunDashboard.test.tsx -t 'preflight|inspection|acknowledgment|dirty|continuation|readiness'
# 12 passed, 86 skipped; includes the new controlled deferred-response case
npm --prefix apps/aflow_app/web run build
# passed
 git diff --check 44cd2579ba6304ac083f4c6b8f12ebb6f767a5dd HEAD
# passed
```

Reused verified worker command-completion evidence in the parent repository's
`.aflow/runs/20260911t142854z-d991d143/turns/turn-001/transport.stdout`
and completed `result.json`:

```sh
uv run --project apps/aflow_app/server pytest -q --basetemp=/root/code/evidence/aflow-dogfood-20260909/startup-preflight-review-20260911/chromium-after-cp1 apps/aflow_app/server/tests/test_plan_startup_browser.py::test_chromium_preserves_ready_state_and_retries_a_corrected_plan
# exit 0; 1 passed, 3 dependency deprecation warnings in 4.95s
```

No production/browser-test changes followed that successful journey. Private
artifact paths occur in invocation options, not committed portable test code.
The baseline browser attempt failed at the Auth token locator, before readiness;
it does not demonstrate the CI cause. The controlled component scenario establishes
pending/current/stale behavior. Exact intermittent CI causation remains unproven;
the passing journey alone is not proof of platform-specific repair. Full suites
remain CI-owned.

## Disposition

Approve for one final unpublished handoff commit at the unchanged base, including
this tracked review record. Preserve all five reviewed implementation/documentation
blobs. DEVLOG already has one handoff entry, so compaction is unnecessary. No fix
plan is needed or stale overlay present. Keep the ignored original plan in place
for engine finalization and record the final approved SHA there after committing.
Do not force-add the private archive, plan, or evidence. Verify exactly one commit
after the base and no tracked edits outside the approved commit. Publication,
exact-SHA CI and live activation remain subsequent controller-owned delivery gates.

Previous report rotated byte-for-byte to `plans/reviews/260911_1443.md`.

No material findings
