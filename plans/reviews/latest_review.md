# Checkpoint 1 review — issue 37

Checkpoint/version: `cp1 v01`, Recognize one numbered reserved heading consistently.
Branch: `aflow-issue-37-plan-heading-and-startup-errors-20260910-20260910-222325`.
Original/active plan: `plans/in-progress/issue-37-plan-heading-and-startup-errors-20260910.md`.

Reviewed the current worktree fallback against pre-handoff base `fdc9ff2c7ac7593383a0856f9d2c82cb09ad3f9c`. No checkpoint implementation commit existed and HEAD equaled that base; the reviewer owns the approval commit. Worker evidence was read from the primary repository's mapped `.aflow/runs/20260910t222324z-5514f100/turns/turn-001/result.json`; plan bookkeeping uses this execution worktree.

Scope: shared optional ASCII integer-dot heading recognition in `aflow/plan.py`, with tests in `tests/test_plan.py` and `tests/test_runtime.py`. Presence/count, metadata parsing, updates, insertion guards and pristine checks share recognition. Coverage includes canonical/numbered parity, fences, populated metadata, malformed prefixes, duplicates, nonpristine history, one successful blank-field allocation and ambiguous rejection before allocation. Heading bytes are preserved.

Findings: none admitted under the material-code-review gate. No production correction required.

Verification:
- `uv run pytest -q tests/test_plan.py tests/test_runtime.py --tb=short`: 347 passed, 60 subtests passed; two unchanged lifecycle fixtures failed because the host global Git excludes file ignores `plans` during fixture commits.
- `GIT_CONFIG_GLOBAL=/dev/null uv run pytest -q tests/test_plan.py tests/test_runtime.py --tb=short`: 349 passed, 60 subtests passed. Isolation was process-local; no host configuration changed.
- `GIT_CONFIG_GLOBAL=/dev/null uv run pytest -q tests/test_library_api.py -k 'git_tracking' --tb=short`: 2 passed, 31 deselected.
- `uv run ruff check aflow` and `git diff --check`: passed.

Decision: checkpoint 1 approved through `cp1 v01`; its completion marks are confirmed by the reviewer. Checkpoints 2 and 3 remain unchecked; the original plan stays in progress. No fix overlay needed. HTTP/MCP feedback and browser correction remain later checkpoint work. Publication, exact-SHA CI, deployment and live UI proof remain coordinator-owned. Physical keyboard remains unverified.

No material findings
