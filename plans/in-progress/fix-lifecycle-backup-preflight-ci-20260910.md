# Fix lifecycle backup preflight in clean CI environments

## Summary

Restore successful branch-only lifecycle setup when AFlow creates its own untracked plan backup between initial admission and phase-B preflight. Exact published commit0eda48f fails six runtime tests on Linux/macOS and Python3.11/3.12; deployment is blocked. A diagnostic reproduction shows initial same_checkout preflight clean, followed by only `.aflow/**` and `plans/backups/my-test-plan.md` untracked. The latter incorrectly requires dirty-worktree confirmation. Existing merge handling already ignores untracked plans/backups; tracked changes must remain protected.

## Git Tracking

- Plan Branch: `aflow-fix-lifecycle-backup-preflight-ci-20260910-20260910-124847`
- Pre-Handoff Base HEAD: `0eda48f2bd03a0a9550a96b549680a09c8339fc6`

## Done Means

Branch-only setup and resume pass without depending on developer Git ignore configuration. Untracked lifecycle backup artifacts do not prompt; actual source/plan edits, tracked backup changes, conflicts, Git operations, and malformed inspection still receive existing protection. Exact-SHA CI must pass before deployment is claimed.

## Critical Invariants

- Preserve current live-config semantics, explicit dirty consent, worktree ownership, and existing merge behavior.
- Only untracked entries wholly within the exact `plans/backups/` boundary qualify for the backup exemption. No tracked/renamed/conflicted item is exempt. Retain existing `.aflow` handling.
- Keep raw preflight items/dirty facts available; adjust the confirmation classification consistently rather than deleting evidence from API responses.
- Use isolated synthetic repos and Git configuration. Never mutate real global config, services, controllers, or shared uv installation.

## Forbidden Implementations

No global ignore edits, fixture-only ignore to hide the production failure, broad `plans/` exemption, blanket dirty confirmation, suppressed failures/skips, or CI gate weakening. No changes to responsive UI work.

## Checkpoints

### [x] Checkpoint 1: Exclude untracked lifecycle backups from dirty confirmation

**Goal:** Preserve trustworthy dirty admission while allowing AFlow's own backup creation.

**Context:** Read root and nearest AGENTS. Inspect `aflow/git_status.py` (`preflight_worktree`, classification, `is_lifecycle_owned_path`), `aflow/workflow.py` (`_backup_plan_copy`, `_preflight_lifecycle_git`, `_is_ignored_merge_status_line`), `tests/test_dirty_worktree_preflight.py`, `tests/test_runtime.py` WorkflowLifecycleRuntimeTests, `tests/_support.py`, and `.github/workflows/ci.yml`. Diagnostic artifacts are in `/root/code/evidence/aflow-dogfood-20260909/ci-preflight-paths.log` and `ci-0eda48f-failed.log`; show only bounded relevant lines.

**Scope:** Small shared classification fix in git_status.py, directly required lifecycle caller adjustment only if needed, focused tests in dirty preflight/runtime modules, DEVLOG. No other feature or test-suite redesign.

**Steps:**

- [x] Reproduce with `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 uv run pytest -q tests/test_runtime.py::WorkflowLifecycleRuntimeTests::test_branch_name_does_not_contain_literal_placeholder --tb=short`. Confirm actual untracked backup is the confirmation cause before editing.
- [x] Extend the existing shared confirmation classification with a narrowly named rule for untracked lifecycle backups under `plans/backups`, matching existing merge ownership semantics. Preserve raw item reporting and all blockers. Do not exempt a rename if either endpoint is outside that boundary, or any tracked/modified/deleted/unmerged backup. Preserve plan versus source distinction for same_checkout and new_worktree modes.
- [x] Add outcome tests: backup-only + `.aflow` metadata does not require consent; same_checkout actual plan/source dirtiness still does; new_worktree source dirtiness still does; tracked modified backup and rename out remain dirty; `plans/backups-other` is not exempt; conflicts still block. Reuse existing fixtures instead of duplicating the whole matrix.
- [x] Add/adjust one real branch-only lifecycle regression with clean Git config and no ignore file, proving startup, unchanged source protection, and successful existing resume expectations. Fix only directly exposed lifecycle consistency defects; no masking with ignore files.
- [x] Run exact focused and full checks below. Record concise DEVLOG diagnosis, corrected behavior, verification and CI follow-up. Other docs need updates only if their described ownership contract changes; root AGENTS stays unchanged.

**Dependencies:** Approved live-config implementation0eda48f. Independent responsive run continues in another worktree.

**Verification:**

- `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 uv run pytest -q tests/test_runtime.py::WorkflowLifecycleRuntimeTests tests/test_dirty_worktree_preflight.py tests/test_git_status.py --tb=short`
- `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 uv run pytest -q --tb=short`
- `uv run ruff check aflow apps/aflow_app/server/src`
- `git diff --check`

**Done When:** Exact tests pass and regressions prove both automatic backup admission and actual dirtiness protection. Report git status, diff names/stat. Subsequent reviewed publication must trigger exact-SHA green CI; keep local and live outcomes separate.

**Blockers:** Escalate only if actual ownership cannot be established or the observed defect demands changes outside this narrow contract. Do not stop for routine reproducible test failures; diagnose and fix in scope.

## Behavioral Acceptance Tests

Given a clean synthetic repo with no user Git config, AFlow creates its backup then starts branch execution without false dirty consent. Given genuine tracked edits or conflicts, existing consent/blockers remain. Given a similarly named sibling directory, it receives no exemption.

## Plan-to-Verification Matrix

- Backup admission: focused classifier tests and real branch-only regression.
- Safety preservation: tracked/rename/source/plan/conflict negative checks.
- Cross-environment delivery: clean-Git full test suite, then existing Linux/macOS CI matrix.

## Assumptions And Defaults

The untracked backup ownership contract already exists in merge handling; this aligns preflight. No new public configuration or migration is needed. Preserve newer accepted main changes on normal integration and use the existing project without registering another worktree project.

## Review Log

- 2026-09-10: Approved Checkpoint 1 as `cp1 v01` using the immediately preceding
  worker's six-file worktree diff against `0eda48f2bd03a0a9550a96b549680a09c8339fc6`.
  No checkpoint commit existed for this plan; older approved checkpoint commits
  were excluded. No material findings. The original plan retains checkpoint authority.
- Reviewer independently verified 97 focused tests / 21 subtests and 1,779 full
  tests / 219 subtests with disposable HOME/config and clean Git configuration;
  Ruff and `git diff --check` passed. No production implementation edits were
  needed during review. Checkpoint approved; publication follows the approval
  commit. Coordinator owns exact-SHA CI and live activation verification.
