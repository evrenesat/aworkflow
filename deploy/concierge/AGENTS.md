# Concierge deployment guidance

- These units are inactive deployment material. Do not enable
  `aflow-concierge.timer` from a worker checkout or from a test.
- The service reads `AFLOW_APP_TOKEN` from
  `EnvironmentFile=/etc/aflowd/aflowd.env` on every start. Never add token
  values to unit files, prompts, argv, status records, logs, or issues.
  `AFLOW_APP_TOKEN` authenticates only the local AFlow MCP endpoint; Codex
  model authentication is separate and lives in `CODEX_HOME`.
- The unit authenticates Codex through
  `CODEX_HOME=/var/lib/aflowd/concierge/codex-home`, a private `0700` home
  that must exist and report a usable `codex login status` before timer
  enablement. The installation procedure in [README.md](README.md) stops
  before `systemctl enable --now` when the home is absent or unauthenticated.
  Never print or track Codex login material.
- The tick runs from `WorkingDirectory=/root/code` with
  `--skip-git-repo-check`; that parent directory is not a Git repository on
  p100 and the advisory tick agent is strictly read-only. The concierge
  **process** is the authoritative boundary: it performs the bounded MCP
  mutations and writes only its private planner workspace
  (`/var/lib/aflowd/concierge`) and the planner worktree under
  `/root/code/agent-flow/.aflow/intake-planning/`.
- The concierge may only interact with AFlow state through the local MCP
  endpoint and with GitHub through read-only REST evidence. It never edits
  implementation code and never starts a second active plan. Duplicate
  detection matches canonical issue URLs inside plan document content, not
  plan record fields.
- Keep the internal tick timeout (15 minutes) and `TimeoutStartSec`
  (16 minutes) strictly below the 20-minute `OnCalendar` interval.
- The tick lock is single-process: a concurrent tick defers with
  `phase=deferred` instead of overlapping.
- Post-delivery activation is owner-authorized and documented in
  [README.md](README.md).
