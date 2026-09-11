# Issue34 cumulative review — 2026-09-11

Branch: `aflow-issue-34-backup-provenance-20260909-20260911-075113`.
Original plan: `plans/in-progress/issue-34-backup-provenance-20260909.md`.
Active overlay reviewed: `issue-34-backup-provenance-20260909-cp01-v03.md`.
Unchanged Pre-Handoff Base HEAD: `b46e441ca758383df60eefe571d097c5ce597ad9`.
Reviewed HEAD: `4398aa75e4e8799248e77afb1487b6ec9ab9b251`.
Coverage: **1 new / 6 total commits**: cp1 v01 `2fc052ef`, cp2 v01 `a4eb60f3`, cp3 v01 `ac11d471`, cp1 v02 `8587d517`, cp1 v03 `f4813743`, cp1 v04 `4398aa75`. Reviewed the entire original-base-through-HEAD implementation across all 21 changed files against the original requirements. Incremental inspection was supplementary.

## Previous findings

- F1 resolved: fresh service identities isolate reused paths and baseline ownership while preserving body deduplication.
- F2 resolved: exact unowned source/original history survives service identity adoption, rename, rollback and follow-up cleanup.
- F3 resolved: allocated follow-up captures use the actual run directory identity; retained multi-turn/two-run tests assert references and deduplication.
- F4 resolved: snapshot wording no longer contradicts the plan's known baseline; both browser journeys preserve the actual unsaved draft.
- F5 resolved: selected-owner references and Ready-promotion metadata supply event/run/turn/timestamp without borrowing another owner's details; absent historical timestamps remain null.
- F6 resolved: controller completion prepares the unique owner before the file move and binds exact unowned capture associations. The real-controller regression verifies snapshot and cleaned-up follow-up details in Done, pagination, identical destination reuse, conflicting destination rejection, unknown baseline and unchanged bodies.

## Findings and verification

No admitted material findings. Applied the material-code-review admission gate, exclusions and proportionate-fix discipline.

- Independently ran `GIT_CONFIG_GLOBAL=/dev/null uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_plan_store.py -k 'backup or baseline or external or followup or shared_body or controller'`: **13 passed, 11 deselected**, one existing Starlette deprecation warning.
- Reused retained verification for reviewed HEAD in `fix-cp01-v03.md`: 7 backup helper tests; 3 directly affected runtime nodes (with the corrected `WorkflowArtifactTests` class); 2 daemon preallocation cases; 2 MCP parity/journey cases; 24 plan-store cases; ruff and diff checks. The owner explicitly requests focused verification and reuse of valid evidence.
- Reused unchanged frontend evidence in `fix-cp01-v02.md`: 12 component tests, production build and isolated Chromium/WebKit backup journeys. Inspected both latest 390×844 screenshots, showing compact headers, readable Baseline/Snapshot history, wrapping and the actual unsaved draft. Reviewed the browser assertions; no physical-device claim.
- Source-searched all backup wrappers/call sites, daemon preparation and MCP promotion contracts. Reviewed metadata validation/atomic writes, retained body naming and deduplication, current-owner association, service rollback, controller completion, authorized bounded REST pages, exact revisions, frontend request guards/drafts and cumulative documentation. Semantic marker contracts are unchanged.
- `git diff --check b46e441..HEAD` passed. Git confirms the branch, reachable unchanged base and 1 new / 6 total commits. Only the expected tracked reviewer record was dirty before finalization.

Evidence root: `/root/code/evidence/aflow-dogfood-20260909/issue34-review-20260911/`.

## Approval and finalization

Approved for one cumulative squash onto the unchanged base. Include this already-tracked review record and the compacted handoff DEVLOG in the single unpublished commit. Preserve all other reviewed implementation blobs. Rotate the prior report byte-for-byte without force-adding the ignored archive; remove the superseded v03 overlay and leave the original plan in place. Record the final approved SHA only in original-plan tracking, avoiding a self-referential commit amendment.

Finalization checks require exactly one commit after the base, no tracked edits outside that commit, unchanged implementation blobs and no stale issue34 fix plans. Controller/coordinator merge, publication, exact-SHA CI and live activation remain separate pending delivery gates. No external comments or issue closure.

---

Retained target-branch review:

# Run detail navigation readiness — cumulative approval review

Original and active plan: `plans/in-progress/run-detail-navigation-readiness-20260911.md`.
Unchanged Pre-Handoff Base HEAD: `833f26cb2bf554341836ce48626755b0f1a0fc4e`.
Reviewed HEAD: `ae9a3197c1758012f0b963f230af7dd8917ecfc9`.
Coverage: 1 new / 1 total commit, `cp1 v01`, all of checkpoint 1 and the
complete base-to-HEAD implementation in test_responsive_browser.py and DEVLOG.md.
No previous findings or follow-up plans exist for this handoff. The previous
latest report concerns managed-worktree branch binding and is archived
byte-for-byte, not treated as an unresolved finding for this repair.

## Findings

No material findings. The helper awaits the requested exact title and Copy run
ID with Playwright string expectations, then the exact URL run query value
with strict equality. Default timeouts remain untouched. The run39-to-run00
caller retains its exact fixture identities. The run-navigation suite imports
this same helper; no duplicate helper repair is needed. Production UI, project,
geometry, draft and payload assertions are unchanged. No code was edited in review.

## Verification

Reused retained worker evidence, as explicitly authorized, under
`/root/code/evidence/aflow-dogfood-20260909/run-detail-readiness-review-20260911/`:

- `web-build-final.log`: npm production build passed (TypeScript and Vite).
- `chromium-desktop-cases-final.log`: requested desktop and desktop-tall
  responsive route cases passed, 2 passed in 15.45s.
- `webkit-desktop-cases-final.log`: same two cases passed, 2 passed in 18.56s.

Earlier retained failures used an unsupported `exact` keyword to `to_have_text`;
that keyword is absent from committed code and final logs pass.
The prompt-referenced worktree-local worker result.json is absent; retained
external logs provide the authorized verification evidence. No full suites
were rerun. Independently checked the cumulative diff and working-tree
`git diff --check`; both passed. Reviewed the helper, navigation callers,
and shared import in test_run_navigation_browser.py.

## Disposition

Approved for one unpublished final handoff commit after the unchanged base,
including this tracked report and preserving all reviewed implementation blobs.
No fix plan is needed; no stale fix plan exists for this handoff. DEVLOG already
contains one relevant entry. Original plan stays in place for engine finalization;
final approved SHA and finalization checks are recorded there after commit.
Ignored plans, archive and external evidence are not force-added.
Merge, origin/main publication, exact-SHA CI and live activation remain the
managed controller/coordinator delivery gates, not outcomes of this local review.

No material findings
