# Checkpoint 3 review — cp3 v01

Original plan: `plans/in-progress/remove-standalone-daemons-keep-ui-mcp-20260908.md`.
Active repair overlay: `plans/in-progress/remove-standalone-daemons-keep-ui-mcp-20260908-cp04-v01.md`; despite its filename, this repairs original checkpoint 3.
Branch: `aflow-remove-standalone-daemons-keep-ui-mcp-20260908-20260910-131652`.

Reviewed immediately preceding worker turn-007 and the complete uncommitted original checkpoint 3 slice against `ff25d251bcdbc5c4a82073fa067351a8a1c24aa8` (`cp2 v01`). Used the worktree fallback because no checkpoint 3 commit existed. Branch matches the original plan and its pre-handoff base remains reachable. Worker result evidence was read from the primary repository at `/root/code/agent_flow/.aflow/runs/20260910t131651z-d1e1ee36/turns/turn-007/result.json`. Existing dirty review artifacts belong to the previous rejection; original plan step changes are authorized progress bookkeeping.

Scope: package script removal, standalone daemon main removal, installer launcher and manifest removal, retained deployment executable validation, and direct tests. The prior installer finding is resolved: newly staged releases omit bin/aflowd and its manifest entry while retaining executable, hashed aflow and aflow-app-server launchers. Shared lifecycle classes, worker_main, service settings, MCP tools including preflight_run, live configuration and UI background controls are unchanged by this slice. Earlier approved checkpoints and checkpoint 4 are outside this review.

Verification: isolated HOME and UV cache, `uv run pytest -q tests/test_aflowd_deploy.py tests/test_aflowd_continuous_deploy.py tests/test_control_plane_resume.py tests/test_cli.py`: 233 passed and 129 subtests passed. ShellCheck passed for install.sh and validate-runtime.sh. Package metadata assertion and git diff --check passed. Installer obsolete-launcher search returned no matches. Retained executable rejection, stale integrity hashes and fresh staging are covered by the passing tests. aflowd.service still executes aflow-app-server.

Findings: none admitted by the material finding gate. No production edits made by the reviewer.

Checkpoint 3 approved as cp3 v01; only its original checkpoint header advances. The reviewer creates the checkpoint approval commit in this turn. Checkpoint 4 remains pending. No new fix plan is needed. Publication, exact-SHA CI and live activation remain unverified and coordinator-owned; local approval is not deployed usability.

No material findings
