# Checkpoint 2 v01 review — issue 37

Scope: original checkpoint 2, safe plan admission through daemon, HTTP and MCP. Active overlay `issue-37-plan-heading-and-startup-errors-20260910-cp03-v01.md` is a checkpoint 2 repair despite its filename. Reviewed the uncommitted worktree against approved cp1 `c0afa1f`, using the worktree fallback because no checkpoint 2 commit boundary exists for this attempt. Branch matches Git Tracking and pre-handoff base `fdc9ff2c7ac7593383a0856f9d2c82cb09ad3f9c` is reachable. Worker evidence was read from the primary repository's `.aflow/runs/20260910t222324z-5514f100/turns/turn-005/result.json`; review writes use the mapped execution worktree.

## Findings

None. Both prior findings are resolved: missing checkpoints receive format guidance, started-base mismatch/empty-base failures receive history reconciliation guidance, and unclassified backup/I/O/value failures retain generic handling. Known source conditions select fixed safe messages; exact retries reconstruct the selected variant without trusting arbitrary persisted message text.

Real daemon tests cover started-base mismatch and empty-base failures, preserving the exact reserved ID, needs_attention state, and idempotency. HTTP/MCP tests cover typed transport and reserved retry identity; HTTP also covers malformed/duplicate rejection before allocation and generic failure redaction. These are compositional checks: transport reserved-failure tests inject the typed exception rather than reproduce the real started-base scenario end to end.

## Verification

- `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 uv run pytest -q tests/test_control_plane_services.py tests/test_control_plane_resume.py tests/test_aflowd.py --tb=short`: 56 passed.
- `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_control_plane_api.py apps/aflow_app/server/tests/test_mcp.py apps/aflow_app/server/tests/test_auth.py`: 67 passed, three dependency deprecation warnings.
- `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 uv run pytest -q tests/test_plan.py tests/test_retry.py tests/test_library_api.py --tb=short`: 110 passed, 23 subtests passed.
- `uv run ruff check aflow apps/aflow_app/server/src` and `git diff --check`: passed.

Checkpoint 2 approved through reviewer-owned `cp2 v01`; only checkpoint 2 review state advances. Checkpoint 1 stays approved and checkpoint 3 stays pending. No production edits by this reviewer, whole-plan squash, or new fix plan. Stable repair overlay retained. Publication, exact-SHA CI/deployment and live UI proof remain coordinator-owned. Physical keyboard remains unverified.

No material findings
