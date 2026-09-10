# CP12 checkpoint review

Reviewed: `cp12 v01` — Allow instruction replacement through remote resume.
Original and active plan: `plans/in-progress/live-configuration-without-snapshot-gates-20260909.md`.
Base: approved CP11 `bb95fef`. Used immediately preceding worker worktree fallback: no pending CP12 commit exists and there are zero intervening implementation commits. CP13 is not the review target despite advanced implementation markers.

Scope: CLI inherit/replace/clear preservation; remote resume transport and service interfaces; daemon bootstrap and worker argument delivery; bounded validation; request idempotency; predecessor preservation; instruction lifetime documentation. Existing worker plan checkbox changes are checkpoint bookkeeping adopted by this review. No production-code changes were made by the reviewer.

Findings: zero. Applied the material finding admission gate, exclusions and proportionate-fix discipline. No concrete introduced material defect survived review.

Verification:
- `uv run pytest tests/test_cli.py tests/test_control_plane_resume.py tests/test_control_plane_services.py apps/aflow_app/server/tests/test_control_plane_api.py apps/aflow_app/server/tests/test_mcp.py -q`: 221 passed, 125 subtests; one Starlette deprecation warning.
- `uv run pytest tests/test_daemon_cli.py -q`: 33 passed.
- `uv run pytest tests/test_runtime.py -q -k 'extra_instructions or targeted_note or review_note'`: 1 passed, 286 deselected.
- `uv run ruff check aflow apps/aflow_app/server/src` and `git diff --check`: passed.
- Branch matches original plan; pre-handoff base remains reachable.

Verification used synthetic providers and temporary fixtures. Transport tests mock bootstrap and capture worker arguments; existing CLI tests verify real bootstrap semantics and the runtime test verifies prompt delivery. No real providers, global configuration/skills, services, deployment or publishing were used.

Approved CP12; advance original review state through `cp12 v01`, retaining CP13 unchecked and the original pre-handoff base. Reviewer creates the checkpoint approval commit without squash. Manager 40 KiB hard guard and compact 16 KiB summary remain intact. No repair overlay is needed.

No material findings
