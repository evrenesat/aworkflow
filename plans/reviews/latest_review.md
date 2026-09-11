# Detached UI MCP discovery cumulative review

Approved. No findings passed the material-code-review admission gate; aflow-review-squash governs finalization.

Reviewed base `2053c498904b73aec6db83a5d85063f6d4ef09a5` through head `9148d6aae55cfff7ba3d76918074c73ce54f2bdd`: one new and one total commit, `cp1 v01`, covering the complete original plan. No prior findings or follow-up plans belong to this handoff.

Original and active plan: `plans/in-progress/ui-mcp-discovery-contract-20260911.md`; all steps complete. Worker evidence read from mapped primary artifact `/root/code/agent_flow/.aflow/runs/20260911t010336z-87577391/turns/turn-001/result.json`. All writes remain in this execution worktree.

Reviewed the full cumulative diff, surrounding lifecycle test, both web authoring registrars, adapter composition, and core/web registry and authenticated discovery assertions. Content search for `tools/list|list_tools|create_control_plane_mcp` covered root and server tests. Only the detached-server expectation was stale. Exactly seven approved names were added to exact-set equality, preserving 14 core tools, bearer auth, health, three resource templates, duplicate start and owned shutdown. Production and core-registry expectations are unchanged. Implementation scope is only tests/test_ui_cli.py and one DEVLOG entry.

Reviewer verification:
- `uv run pytest -q tests/test_ui_cli.py -k test_daemon_start_health_and_stop`: 1 passed, 14 deselected in 2.81s.
- `uv run pytest -q tests/test_ui_cli.py`: 15 passed in 5.26s.
- Existing fixtures isolate HOME/configuration, ports and owned processes.
- `git diff --check 2053c498904b73aec6db83a5d85063f6d4ef09a5..HEAD`: passed; initial worktree clean.

Approve feature-only squash after unchanged base, preserving reviewed implementation and including review artifacts. One handoff DEVLOG entry needs no compaction. No fix plan created and no stale handoff fix plans exist. Final commit identity, count and preservation checks are recorded in the original ignored plan after squash. Engine owns lifecycle move and normal merge; coordinator owns publication, exact-SHA CI and live acceptance. No delivery gate is claimed here.

HISTORY: Prior latest review concerns separate web MCP authoring work; archived byte-for-byte as `260911_0109.md`.

No material findings
