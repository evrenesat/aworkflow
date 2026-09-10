# Checkpoint 1 review — control draft readiness

Reviewed `cp1 v01` using current-worktree fallback: no checkpoint commit follows reachable base `fdc9ff2c7ac7593383a0856f9d2c82cb09ad3f9c`. Branch matches `aflow-verify-control-draft-refresh-readiness-20260910-20260910-222127`. Original and active plan: `plans/in-progress/verify-control-draft-refresh-readiness-20260910.md`. Scope: named saved-configuration test and DEVLOG. No production files changed.

Findings: none. Applied the material finding admission gate, exclusions, and proportionate-fix discipline. CP1 is approved through `cp1 v01`; the reviewer creates its approval commit in this turn. No fix overlay is needed.

Controlled evidence proves: list/capabilities pending → no control; list settled/capabilities pending → no control; capabilities settled/detail pending → enabled baseline8 and run-owned; detail settled → baseline8; real change handler edits17; hidden→visible refresh completes saved-team and harness/saved option attachment → exact17 and run-owned remain. The test and production initialization/refresh effects agree on this sequence. Existing deferred helpers and real event handlers are used, with no sleeps, timeout inflation, weakened assertions, or production special cases.

The original17→8 failure was not reproduced by this controlled ordering. Approval accepts the explicitly authorized honest non-reproduction limit: safe readiness preconditions and post-readiness refresh preservation are established, not historical causality.

Reviewer verification, one run per required command:
- `npm --prefix apps/aflow_app/web test -- --run src/components/RunDashboard.test.tsx`: 85 passed, exit0.
- `npm --prefix apps/aflow_app/web test -- --run`: 22 files, 331 passed in normal parallel mode, exit0.
- `npm --prefix apps/aflow_app/web run build`: passed, exit0.
- `git diff --check`: passed.

Exact output retained in `plans/reviews/control-draft-cp01/focused.txt`, `full-web.txt`, and `build.txt`. Exact source is the checkpoint's `apps/aflow_app/web/src/components/RunDashboard.test.tsx`, SHA256 `e2e409d8e9748459286ddacf1f989a7d52aae4481accbfc4647ca37f95025375`; an identical local snapshot remains at `plans/reviews/control-draft-cp01/RunDashboard.test.tsx.txt`. Assertions provide the bounded state observations; no separate runtime instrumentation or broader causal claim is implied.

The supplied worker result artifact is absent locally; reviewer verification supplies independent retained output. The prior review/overlay in the explicitly named old root and incident logs were inspected read-only, with historical output prefixed HISTORY:. No old bookkeeping was copied. Unchanged backend/browser checks are reused, not rerun. This test-only change requires no new browser acceptance claim.

The original checkpoint review state advances; the plan stays in progress for delivery. Publication to origin/main, exact-SHA CI, and live verification remain coordinator-owned and pending. No history rewrite, shared-tool change, other-worktree edit, or public post occurred.

No material findings
