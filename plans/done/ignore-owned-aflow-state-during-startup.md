# aflow Plan: Ignore Owned Runtime State During Startup Dirtiness Checks

## Summary

Make worktree-workflow startup ignore only aflow-owned `.aflow/` runtime artifacts, matching teardown behavior, so a prior run does not block a fresh run in a repository that does not commit or globally ignore `.aflow/`. Continue rejecting every unrelated path outside `plans/`.

## Git Tracking

- Plan Branch: `aflow-ignore-owned-aflow-state-during-startup-20260802-184036`
- Pre-Handoff Base HEAD: `c7e933980b2a3fd3131f3fc3cc85b58646fa7132`
- Review Log:
  - Checkpoint 1 worktree fallback rejected on 2026-08-02: porcelain arrow parsing could reinterpret an unrelated quoted path as `.aflow` state, and destination-only rename filtering could hide an unrelated rename source. The first repair plan was superseded and removed after implementation.
  - Checkpoint 1 repair worktree fallback rejected on 2026-08-02: record-level parsing now handles quoted arrows, both rename/copy paths, and malformed records conservatively, but merge teardown still ignores a tracked rename or copy whose destination is the active original plan. The superseding repair is specified in `plans/todo/ignore-owned-aflow-state-during-startup-cp01-v02.md`.
  - Checkpoint 1 approved through `cp1 v01` (`24b5018`) on 2026-08-02 after the focused repair limited the active-plan allowance to single-path records, kept tracked rename/copy records blocking in both directions, and reported both decoded endpoints.
  - 2026-08-02: `aflow-review-final` approved the completed handoff without a follow-up fix plan. The final review covered 0 new commits since the checkpoint approval and 1 total implementation commit since `c7e9339`; exact `.aflow` ownership filtering, both startup gates, conservative porcelain parsing, rename/copy endpoint handling, merge-teardown compatibility, regression coverage, and documentation were re-inspected. All plan-specific checks passed. The complete core suite reported 958 passed and 165 passing subtests with the same 16 failures reproduced at the pre-handoff base; unrestricted root collection remained constrained by absent optional app-server dependencies.

## aflow-review-final

- Status: approved
- Reviewed Through: `cp1 v01`
- Reviewed Range: `c7e933980b2a3fd3131f3fc3cc85b58646fa7132..24b50184f291267c1761950d1904c6c19be49819`
- New Commits Since Last Review: `0`
- Total Implementation Commits Since Pre-Handoff Base HEAD: `1`

## Done Means

- Fresh worktree workflows can start when `git status --porcelain --untracked-files=all` contains only `plans/**` and `.aflow/**` paths.
- Any other untracked or modified path still fails with the existing actionable non-plan-dirtiness error.
- Resume, baseline capture, merge safety, and user Git ignore files remain unchanged.

## Critical Invariants

- Ignore only the exact `.aflow` path or descendants under `.aflow/`; do not use substring, suffix, basename, or glob-like matching.
- Do not edit `.gitignore`, `.git/info/exclude`, global Git config, or repository contents as part of startup.
- Do not hide `.aflow` changes from generic status/reporting APIs; apply ownership filtering only where aflow decides whether its own state blocks lifecycle startup/teardown.

## Forbidden Implementations

- Do not suppress all dot-directories, all untracked paths, or all paths outside `plans/`.
- Do not run `git clean`, delete old runs, or automatically add ignore rules.
- Do not maintain separate startup and teardown ownership lists that can drift.

## Checkpoints

### [x] Checkpoint 1: Centralize owned-path filtering for lifecycle dirtiness

**Goal:**

- Use one tested repository-relative path classifier for aflow-owned lifecycle artifacts at both startup and merge teardown.

**Context:**

- Run: `git rev-parse --show-toplevel`
- Inspect: `aflow/git_status.py`, `aflow/api/startup.py`, `aflow/workflow.py`, `tests/test_git_status.py`, `tests/test_library_api.py`, and `tests/test_runtime.py`
- Preserve: plan-only startup allowance, exact error wording where practical, and teardown allowances for `.aflow/**`, `plans/backups/**`, and the active original plan.

**Scope:**

- May create or modify: the inspected production/test files plus `ARCHITECTURE.md` and `devlog/DEVLOG.md` if they document lifecycle ownership.
- Must not touch: run retention, pruning, Git ignore files, harness adapters, or workflow/team configuration.
- Constraints: normalize porcelain rename records and quoted paths using existing parsing behavior; compare repository-relative POSIX paths only.

**Steps:**

- [x] Add a shared predicate/classifier for aflow-owned repository paths, with an explicit mode or caller-supplied set when startup and teardown legitimately differ.
- [x] Update `_check_worktree_dirtiness` so `.aflow` and `.aflow/**` are excluded before reporting non-plan dirt while `plans/**` retain their existing allowed classification.
- [x] Refactor `_is_ignored_merge_status_line` to use the same owned-path predicate without changing its `plans/backups/**` and active-plan behavior.
- [x] Add table-driven tests for exact `.aflow`, descendants, deceptive names such as `.aflow-copy`, nested `src/.aflow`, quoted paths, renames, plan paths, and ordinary modified/untracked files.
- [x] Add startup/runtime regression tests proving an existing failed run directory no longer blocks a fresh controller and unrelated dirt still fails before worktree creation.
- [x] Record the ownership-boundary decision in existing architecture/devlog documentation only if those files already cover lifecycle dirtiness.

**Dependencies:**

- None.

**Verification:**

- Run: `uv run pytest -q tests/test_git_status.py tests/test_library_api.py -k 'dirt or startup or aflow'`
- Run: `uv run pytest -q tests/test_runtime.py -k 'worktree and (dirty or status or merge)'`
- Run: `uv run python -m compileall -q aflow`
- Observe: `.aflow/runs/example/run.json` does not block startup, while `.aflow-copy/file`, `src/.aflow/file`, and `notes.txt` do.

**Done When:**

- Owned-state filtering is shared, narrowly tested, and changes no unrelated status or Git lifecycle behavior.
- Every completed step is validated against code, tests, or observable behavior.
- Verification passes and the changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if porcelain parsing cannot distinguish a deceptive path without switching to `git status -z`; make that parser change explicit rather than guessing.
- Stop and report if unrelated dirty files make change ownership ambiguous.

## Behavioral Acceptance Tests

- Given an otherwise clean repository containing an untracked `.aflow/runs/old/run.json` and an untracked plan, when a fresh worktree workflow starts, then startup proceeds to normal plan/lifecycle preparation without modifying ignore files.
- Given the same repository plus untracked `notes.txt`, when startup runs, then it exits nonzero, names `notes.txt`, and creates no worktree/controller.
- Given deceptive `.aflow-copy/file` or `src/.aflow/file`, when startup runs, then those paths remain unrelated dirt and block startup.
- Given merge teardown with aflow-owned artifacts, when status is evaluated, then current merge allowances remain unchanged.

## Plan-to-Verification Matrix

- Exact owned-path boundary: table-driven classifier tests.
- Fresh-run regression: startup/library test with a prior `.aflow/runs` tree.
- Unrelated-dirt rejection: negative startup tests and no-worktree assertion.
- Teardown compatibility: focused runtime merge/status tests.

## Assumptions And Defaults

- `.aflow/` is wholly owned by aflow runtime state; a repository that intentionally tracks files there is outside the current lifecycle contract and should be handled as a separate compatibility decision.
- Plans remain allowed startup dirt under the existing `plans/` rule; this change does not broaden that rule.
- The operational `.git/info/exclude` workaround is local-only and may be removed after the product fix, but removal is not part of this plan.
