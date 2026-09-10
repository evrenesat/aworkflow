# Resolve dashboard clipboard verification failure (issue 38)

## Summary

GitHub issue38 records a required focused RunDashboard run failing to observe clipboard success, while the full suite passed. Resolve the selection/clipboard feedback timing with deterministic evidence; preserve sanitized URLs and error handling. Source issue: https://github.com/evrenesat/aworkflow/issues/38 . This is an unresolved verification gap, not proof of a production bug.

## Git Tracking

- Plan Branch: `aflow-issue-38-stable-dashboard-clipboard-20260910-20260910-155213`
- Pre-Handoff Base HEAD: `52597409b17c3e1050f1576bfe027580b5656d50`

## Done Means

The focused clipboard journey and full web suite pass without sleeps, retries, timeout inflation or weakened assertions. Copy uses the confirmed selected project/run, excludes ambient query/hash/secret data, and displays success/failure for the correct selection. Evidence distinguishes a fixture readiness problem from a reproduced production race.

## Critical Invariants

Preserve workspace URL sanitization, selected-run validation, and exact clipboard payload. Error messages must not leak the URL or secret data. Copying a pending/unvalidated selection must not fabricate a confirmed run. Preserve responsive work in its separate root; no broad layout/control changes.

## Forbidden Implementations

No generic longer timeout, assertion removal, unconditional clipboard success, production pytest special-case, global clipboard mutation left unrestored, or unrelated UI rewrite. Do not post issues/comments/messages; existing issue38 is read-only context and plan state is synchronized through AFlow.

## Checkpoints

### [x] Checkpoint 1: Prove and fix clipboard selection/readiness ownership

**Goal:** Clipboard verification is deterministic and reflects actual user-visible selection.

**Context:** Read nearest AGENTS and UI_GUIDELINES. Inspect RunDashboard.tsx handleCopyLink, copyState and selection-reset effect; RunDashboard.test.tsx test "copies the sanitized dashboard link and reports clipboard failure without hidden data", fixture loading/reset and mocked clipboard ownership. Existing focused evidence `/tmp/aflow-cp2-review-focused.log` may be available; source GitHub issue is authoritative if that temp artifact was removed.

**Scope:** Clipboard and confirmed-selection readiness in RunDashboard.tsx/RunDashboard.test.tsx, plus DEVLOG. Coordinator explicitly expands scope to the observed missing Confirm resume transition in `reuses a resume key after an uncertain failure`, including minimal fixture readiness or directly reproduced confirmation-ownership correction. Preserve exact resume keys, source/continuation identities, confirmation requirements and API semantics; no broader resume feature, responsive layout or URL-contract changes.

**Steps:**

- [x] Reproduce the journey with controlled deferred list/detail and clipboard promises to establish the ordering that loses success feedback. Distinguish history-row presence from confirmed detail selection and its settled effects. Use actual component outputs and existing callbacks, not private state inspection. Preserve the original failure evidence even if the normal focused invocation passes.
- [x] If the test acts before confirmed selection, wait for that actual observable readiness using existing test utilities and controlled act/promise resolution. Do not wait for unrelated team/control configuration merely to create delay. Scope and restore navigator.clipboard and location mutations on all exits.
- [x] If deterministic delayed clipboard completion after selection change reproduces a real stale-feedback defect, bind completion feedback to the copied selection/request identity using the smallest existing ref/state pattern. A newer selection clears old feedback; an old promise must not relabel the new selection as copied. Preserve ordinary success/failure and actual copied bytes. If no production defect is proven, change only fixture readiness and explicitly document that limit.
- [x] Keep exact success text, call count, exact sanitized URL, and rejection assertions. Cover delayed readiness and, if production correction is needed, selection change while copy is pending. Retain secret/query/hash exclusions; do not replace real handlers with mocks.
- [x] Resolve the observed resume-confirmation verification dependency with retained controlled list/detail settlement evidence. Compare the accepted baseline and pending location/clipboard fixture setup under the same ordering. Correct actual readiness in the test, or the smallest proven user-reachable selection-initialization reset of confirmation, while retaining the uncertain-response retry key and all existing assertions. This correction is explicitly authorized even if independent of clipboard setup. Preserve pending clipboard work; update the v02 repair overlay to this expanded original authority rather than stopping again for its obsolete narrower scope.
- [x] Run required focused/full checks and build, inspect scope, and record concise DEVLOG evidence and outcome. No public issue update is authorized by this plan.

#### Checkpoint 1 execution evidence

- The pre-fix deferred selection-change test observed the old clipboard promise
  relabel the newly selected run as copied; the request-generation guard made
  that same test pass without changing copied bytes or success/failure text.
- Verification passed: `npm --prefix apps/aflow_app/web test -- --run src/components/RunDashboard.test.tsx` (79), `npm --prefix apps/aflow_app/web test -- --run` (292 across 16 files), `npm --prefix apps/aflow_app/web run build`, and `git diff --check`.
- Scoped files: `apps/aflow_app/web/src/components/RunDashboard.tsx`, `apps/aflow_app/web/src/components/RunDashboard.test.tsx`, and `DEVLOG.md`. No public issue update or checkpoint commit was made.

#### Checkpoint 1 v01 readiness correction — 2026-09-10

- The uncertain-resume test now controls list and direct-detail promises, waits for
  the settled selected-run output (`Review · 2`), verifies the enabled Resume action,
  and only then enters confirmation. This retains the exact source run, confirmation
  requirement, `connection lost` assertion, two API calls, and reused idempotency key.
- The correction is test-only. The production diff remains limited to clipboard
  request generations; the responsive work, URL contract, resume API, source/
  continuation identities, and clipboard/location cleanup are unchanged.
- Required verification after the correction passed: focused RunDashboard tests
  (79), full web tests (292 across 16 files), web production build, and
  `git diff --check`. No sleep, retry, timeout change, or weakened assertion was used.
- Checkpoint 1 remains unapproved and uncommitted for reviewer handoff; publication,
  exact-SHA CI, and live verification remain coordinator-owned.

#### Checkpoint 1 v03 retained comparison evidence — 2026-09-10

- Comparison source: `apps/aflow_app/web/src/components/RunDashboard.test.tsx`,
  with provenance and scenario description in
  `plans/notes/issue-38-stable-dashboard-clipboard-cp01-v03-comparison-source.md`.
  The accepted baseline was recovered with the exact `git show
  52597409b17c3e1050f1576bfe027580b5656d50:apps/aflow_app/web/src/components/RunDashboard.test.tsx`
  command; v01/v02/v03 plan paths remain intact.
- Output: `plans/notes/issue-38-stable-dashboard-clipboard-cp01-v03-comparison-output.txt`.
  Four deterministic cases compare accepted-baseline and pending location/clipboard
  setup, clicking Resume before and after direct-detail settlement. All cases observe
  list settlement, direct-detail settlement, visible/enabled Resume, and Confirm after
  clicking. Both setups produced the same result; the setup-specific failure did not
  reproduce, so no clipboard causal link is claimed.
- Existing source/continuation identities, resume keys, confirmation requirements,
  exact sanitized URLs, privacy/error assertions, and clipboard/location restoration
  remain unchanged. No production correction was added for this evidence pass.
- The comparison changed only test coverage. Required verification after that change
  passed once: focused RunDashboard tests (83), full web tests (296 across 16 files),
  web production build, and `git diff --check`.

**Dependencies:** Accepted test reliability e7ad130; carry accepted main history forward. Independent of responsive presentation but reconcile its component changes at integration.

**Verification:** `npm --prefix apps/aflow_app/web test -- --run src/components/RunDashboard.test.tsx`; `npm --prefix apps/aflow_app/web test -- --run`; `npm --prefix apps/aflow_app/web run build`; `git diff --check`.

**Done When:** Both focused and full tests pass, deterministic ordering coverage validates the selected correction, and no URL or failure assertions are weakened. Report status/diff names/stat and whether production code changed with its proven trigger.

**Blockers:** Other confirmed selection/admission-readiness failures in this same dashboard group may receive the same narrowly evidenced correction, preserving their semantics. A different production behavior remains an explicit verification gap. Do not hide failures with retries or weaken assertions.

## Behavioral Acceptance Tests

After confirmed run selection, Copy writes exactly the sanitized project/run link and shows success. Clipboard rejection reports failure without embedding secrets/URL. Deferred selection/clipboard operations do not cause a test to act on unconfirmed state or show old-selection success on a newer selection.

## Plan-to-Verification Matrix

Readiness: deferred list/detail journey. Clipboard payload/privacy: exact URL and exclusion assertions. Feedback ownership: controlled clipboard resolution. Regression: focused and full web suites/build.

## Assumptions And Defaults

Prefer a test-only correction when evidence establishes fixture ordering; change production only for a deterministic user-reachable race. No new clipboard abstraction or dependency is needed.

## Review Log

### Checkpoint 1 v01 — 2026-09-10

Approval withheld. Current-worktree fallback reviews only the immediately preceding
worker's three-file diff against `52597409b17c3e1050f1576bfe027580b5656d50`; no checkpoint
commit exists for this plan. Branch matches and base is reachable.
No material clipboard finding. Focused: 79 passed. Full: 291 passed, 1 failed,
`reuses a resume key after an uncertain failure`, RunDashboard.test.tsx:1310,
missing `Confirm resume`. Build and diff check passed. No retries.
Causation by the clipboard patch is unproven; this is the plan's explicit required
verification gap, not authority to repair unrelated resume behavior.
Follow-up: `issue-38-stable-dashboard-clipboard-20260910-cp01-v01.md`.
No approval commit or publication. Supplied worker result.json is absent here;
actual diff and fresh checks support this review.
HISTORY: Worker-recorded full-suite success above is superseded for approval by this review's failed check.
HISTORY: /tmp/aflow-cp2-review-focused.log retains the original clipboard assertion failure (77 passed, 1 failed).

### Checkpoint 1 v01 follow-up execution — 2026-09-10

- HISTORY: The review's full-suite evidence remains 291 passed and 1 failed at
  `RunDashboard.test.tsx:1310`, missing `Confirm resume`; focused coverage was
  79 passed. No retry was used to replace that result.
- A temporary controlled list/detail experiment passed 1 test with 79 skipped:
  observable Resume appeared after list settlement, Confirm appeared after the
  click, and Confirm survived direct-detail settlement. This did not reproduce
  the reported ordering failure.
- The reported journey precedes the modified clipboard test, and the source
  diff has no resume/control changes. Clipboard/location cleanup restores
  ownership on exit. No causal clipboard fixture defect was demonstrated; no
  overlay code correction was made.
- Checkpoint 1 remains unapproved with its required full-suite step unchecked;
  coordinator-owned full-suite verification is the remaining dependency.

## Coordinator scope amendment (2026-09-10)

The original run stopped after v02 review because full-suite resume readiness was outside the earlier clipboard-only overlay. The added step above is now authorized. Retain causal experiment source/output, existing code and Git Tracking; it is unnecessary to invent a causal link to clipboard changes before fixing the directly observed readiness failure. No implementation approval has been granted yet.

### Checkpoint 1 v01 approval — 2026-09-10

Reviewer approved the current-worktree slice against reachable base
`52597409b17c3e1050f1576bfe027580b5656d50`; no issue38 checkpoint commit existed.
Approval label: `cp1 v01 aflow-issue-38-stable-dashboard-clipboard-20260910-20260910-155213: Retain clipboard ownership and readiness evidence`.
Zero material findings. The retained four-case baseline/pending comparison passed
again in review, and its complete command output replaces the earlier excerpt in
`plans/notes/issue-38-stable-dashboard-clipboard-cp01-v03-comparison-output.txt`.
Both setups preserved confirmation before/after detail settlement. Non-reproduction
does not establish the cause of the earlier failure. Source provenance was checked
against the accepted base. Exact URL/privacy and resume identity/key/confirmation
assertions remain intact. `git diff --check` passed. No code/test edits in this
review; reuse the documented post-comparison focused83/full296/build verification.
The v01/v02/v03 overlays remain preserved. Checkpoint approval is complete locally;
plan delivery remains pending coordinator-owned origin/main publication, exact-SHA
CI, and live verification. No new fix overlay is required.
