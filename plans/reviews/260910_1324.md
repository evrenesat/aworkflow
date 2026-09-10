# Checkpoint 1 review: lifecycle backup preflight

Reviewed checkpoint/version: `cp1 v01` on
`aflow-fix-lifecycle-backup-preflight-ci-20260910-20260910-124847`.

The original and active plan are both
`plans/in-progress/fix-lifecycle-backup-preflight-ci-20260910.md`.
The review uses the current-worktree fallback: this plan has no checkpoint
commit yet. The target is the immediately preceding worker's six-file diff
against `0eda48f2bd03a0a9550a96b549680a09c8339fc6`, not older approved commits.

Scope: untracked lifecycle backup confirmation classification, consistent
startup/lifecycle callers and merge boundary reuse, focused protection tests,
branch-only startup/resume regression, and DEVLOG. Raw status/dirty evidence,
tracked changes, rename endpoints, conflicts, Git operation blockers and
inspection failures retain their existing handling. Responsive UI work is
outside this review and was untouched.

Verification performed independently in this review:

- Clean Git configuration and disposable HOME/config: 97 focused tests and
  21 subtests passed.
- Same isolation for the full suite: 1,779 tests and 219 subtests passed.
- `uv run ruff check aflow apps/aflow_app/server/src` passed.
- `git diff --check` passed.

Findings: none admitted by the material-code-review gate.
Checkpoint 1 is approved; this review creates the `cp1 v01` approval commit
and advances the original plan's review log. No fix overlay is required.
Normal publication follows the approval commit under the standing grant.
Exact-SHA CI and live deployment verification belong to the coordinator;
local approval is not deployment evidence.

No material findings
