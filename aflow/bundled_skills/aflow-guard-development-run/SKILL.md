---
name: aflow-guard-development-run
description: Monitor and audit an explicitly selected AFlow run without changing its implementation, recovery state, plan, configuration, or controller. Use when the user asks to guard, babysit, monitor, observe, production-check, or keep watch over an AFlow workflow or its explicitly authorized deployment. Launch new legacy CLI runs in tmux, check every 30 minutes, use remote or MCP features read-only when available, report confirmed AFlow defects as sanitized GitHub issues, audit terminal results, and then stop.
---

# Observe an AFlow Run

Observe the exact run selected by the user. Protect the implementation outcome
and Codex quota by remaining read-only after launch.

## Hard boundaries

- Monitor, investigate, report, audit, and pause. Never fix, retry, resume,
  replace, replan, steer, hotplug, edit, test, commit, merge, or alter the run.
- Never edit the guarded worktree, plan, `run.json`, configuration,
  overrides, controller, harness, deployment files, or AFlow source.
- Never call MCP or REST write operations, including start, startup-answer,
  control, stop, resume, or steering. Remote features are observation-only.
- Keep exactly one 30-minute heartbeat attached to the initiating Codex task.
  Never create a secondary task or one cron task per tick.
- Keep healthy ticks silent. Do not reread task history or unchanged artifacts.
- Give one bounded investigation and one report to each new anomaly fingerprint.
  An unchanged anomaly consumes only the normal snapshot on later ticks.
- Store only compact routing and deduplication state outside the repository.
- Treat tmux presence as an attachment aid, never as controller-liveness proof.
- Pause after terminal audit, terminal failure, an orphaned controller, unsafe
  ownership, duplicate controllers, invalid state, deployment result, or user
  stop.
- Do not create local defect plans or incident reports. Confirmed AFlow engine
  defects belong only in sanitized GitHub issues.
- Deployment is the sole post-launch mutation and is allowed only when the user
  explicitly authorized it for this run and the terminal audit passed.

## Launch a new legacy run

Use this section only when the user asks to start the run. Guard an existing run
in place; never restart it merely to put it in tmux.

1. Follow the guarded repository's instructions and identify its authoritative
   host and checkout.
2. Verify the intended AFlow checkout is the installed editable tool source.
3. Build the exact approved `aflow run ...` argv. Never use
   `uv run aflow`.
4. Create a unique session such as `aflow-<project>-<UTC timestamp>`.
   Start the command from the authoritative repository using one shell-escaped
   command string; do not interpolate untrusted text.
5. Pin the run ID emitted by that launch. Never select an ambiguous latest run.
6. Report the session and these commands immediately:

```bash
tmux attach-session -t <session>
# Detach while attached: Ctrl-B, then D
tmux capture-pane -p -S -200 -t <session>
pgrep -af 'aflow.* run'
```

For this version, do not launch through MCP. Preserve the selected run's
existing ownership:

- `legacy`: direct `aflow run` controller, optionally attached to tmux;
- `local-daemon`: lightweight `aflow daemon` worker owned by that daemon; or
- `aflowd`: production control-plane run owned by its exact systemd unit.

Never add a tmux or CLI controller to either daemon-owned mode.

## Establish the guard

1. Pin the absolute repository, exact run ID, plan, ownership mode, optional
   tmux session, initiating task ID, and optional advertised remote endpoint.
2. Establish liveness through the ownership-matched surface described in
   `references/remote-observation.md`. Use the bundled snapshot only for
   `legacy` runs. Schedule only an active, uniquely owned run.
3. Read `CODEX_THREAD_ID`. If it is missing or not a UUID, do not
   schedule.
4. Find existing schedules for the exact repository and run ID. Pause stale or
   duplicate guards before creating the one selected heartbeat.
5. Create one heartbeat attached to the initiating task every 30 minutes with
   failed-runs-only notifications.
6. Put the exact task ID, absolute paths, run ID, tmux session, and optional
   remote identity in the heartbeat prompt.

Use this short prompt:

```text
Use $aflow-guard-development-run for one observer-only tick on AFlow run RUN_ID
in REPO. This task is THREAD_ID. Resolve the pinned ownership mode and use one
ownership-matched read-only observation. Return DONT_NOTIFY while healthy, and
never fix, retry, resume, steer, edit, test, or alter implementation. Report a
new anomaly once. Audit terminal success, perform only an explicitly authorized
deployment, then pause this automation.
```

## Perform one 30-minute tick

First resolve and pin exactly one ownership mode. Do not infer daemon ownership
from a process name alone.

For a `legacy` run, run the snapshot:

```bash
python3 <skill-dir>/scripts/aflow_guard_snapshot.py   --repo <guarded-repo>   --run-id <run-id>   --tmux-session <optional-session>   --thread-id <initiating-task-id>
```

For a `local-daemon` run:

1. Use `aflow daemon status --repo-root <guarded-repo>` to corroborate the
   existing daemon and its directly owned worker.
2. Use the daemon's already configured MCP transport for one `get_run`.
   Never create a disposable stdio connection: closing stdin stops that daemon
   and drains its workers.
3. Do not report a web UI URL. The lightweight daemon has no REST or web UI.

For an `aflowd` run:

1. Use one authenticated `get_run` through the advertised MCP endpoint.
2. Corroborate liveness with the exact advertised
   `aflow-run-<run-id>.service` unit when host access is available.
3. Do not run the legacy process snapshot or broad `pgrep` searches.

Capability discovery belongs to guard setup, not every tick. On a healthy
daemon-owned tick, call only `get_run`. If status or revision changed, use
the next tick for at most one cursor-bounded `get_run_events` request. Never
load Full context.

Treat canonical `running` state plus its matching owner as healthy. Treat
`needs_attention`, legacy/interrupted daemon ownership, a missing owner, or
remote/durable disagreement as a new anomaly: report once and pause without
mutation.

For `legacy`, apply the snapshot classification exactly:

| Classification | Action |
| --- | --- |
| `active_progress` | Return exactly `DONT_NOTIFY`. |
| `active_waiting_child` | Return exactly `DONT_NOTIFY`. |
| `active_waiting` | Return exactly `DONT_NOTIFY`; investigate only when this is a new anomaly fingerprint. |
| `orphaned_controller` | Investigate once, report, and pause. |
| `terminal_success` | Audit once, report, optionally deploy, and pause. |
| `terminal_failed` / `terminal_incomplete` | Investigate once, report, and pause. |
| `unsafe_inconsistent` / `unsafe_duplicate_controllers` / `invalid_state` | Report and pause without mutation. |


For a new anomaly, inspect only:

- selected durable run fields;
- the newest finalized `result.json`;
- controller descendants and tmux presence; and
- at most the final 4 KiB of the relevant stdout or stderr.

Do not inspect Git history, old turns, complete transcripts, previous scheduled
task history, or repeated unchanged evidence.

## Report an AFlow engine defect

Distinguish a project implementation/test failure from an AFlow controller,
manager, persistence, lifecycle, terminal-report, control-plane, or harness
adapter defect. File only confirmed engine defects.

Read `references/aflow-defect-issue.md`, then use
`scripts/aflow_guard_issue.py`. Supply project identifiers as redaction
terms, validate before network access, search all existing issues for the
fingerprint, and create at most one issue in `evrenesat/aworkflow`.

If evidence cannot be made reproducible without private project details, or
GitHub authentication fails, report the filing block in the initiating task.
Do not write a local substitute.

## Audit terminal success

Perform one evidence audit without changing the project:

1. Confirm durable terminal success and no active controller.
2. Check plan checkpoint completion and final-review status.
3. Check recorded verification evidence; do not rerun tests or builds unless the
   guard request explicitly authorized fresh verification.
4. Check expected branch, worktree, and commit identity without modifying Git.
5. Report pass, fail, or unavailable evidence. Never convert a failed audit into
   repair work.

## Deploy only when explicitly authorized

Deployment must have been explicitly requested for this guarded run.

1. Require a passing terminal audit.
2. Resolve an existing project-owned deployment playbook and inventory from
   the guarded repository's documentation or the user's explicit handoff. Do
   not assume a private repository or filesystem path; fail closed when the
   mapping is absent or ambiguous.
3. Do not edit the playbook, inventory, variables, application, or credentials.
4. Run the repository's existing syntax check, execute the playbook once, and
   run only its predefined bounded smoke check.
5. On success or failure, report the exact playbook, target, and bounded result,
   then pause. Do not retry, repair, alter infrastructure, or improvise rollback
   beyond behavior already encoded by the playbook.
