---
name: aflow-guard-development-run
description: Monitor and audit an explicitly selected AFlow run without changing its implementation, recovery state, plan, configuration, or controller, except for one evidence-backed neutral startup correction under an explicit start-and-monitor authorization. Use when the user asks to guard, babysit, monitor, observe, production-check, or keep watch over an AFlow workflow or its explicitly authorized deployment. Launch new legacy CLI runs in tmux, check every 30 minutes, use remote or MCP features read-only when available, report confirmed AFlow defects as sanitized GitHub issues, audit terminal results, and then stop.
---

# Observe an AFlow Run

Observe the exact run selected by the user. Protect the implementation outcome
and Codex quota by remaining read-only after launch. A start-and-monitor request
has one narrower pre-launch exception described below; ordinary observation
remains read-only.

## Hard boundaries

- Monitor, investigate, report, audit, and pause. Never fix, retry, resume,
  replace, replan, steer, hotplug, edit, test, commit, merge, or alter the run
  during ordinary guard operation. The only exception is the bounded neutral
  startup case below.
- Never edit the guarded worktree, plan, `run.json`, configuration,
  overrides, controller, harness, deployment files, or AFlow source.
- Never call MCP or REST write operations during ordinary observation, including
  start, startup-answer, control, stop, resume, or steering. Remote features
  are observation-only except for the explicitly authorized neutral startup
  launch described below.
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

## One-shot neutral startup exception

Use this exception only when the same user request explicitly authorizes the
guard to start and monitor the selected workflow. Monitoring an existing run,
an unrequested retry, or a generic startup error does not authorize it.

The exception is eligible only when a bounded receipt from matched launch
evidence proves every item below. The receipt must be passed to
`scripts/aflow_guard_recovery.py`; the helper does not inspect missing files and
does not infer facts from their absence.

- The predecessor is terminal and failed before controller creation because
  required Git Tracking support metadata was missing or blank.
- Started turns and finalized turns are both exactly zero. The matched evidence
  proves no owned controller, child, provider session, branch, or worktree was
  created by the attempt.
- The selected plan content and launch choices are unchanged, and the original
  plan content digest is recorded. Unknown, malformed, or semantically changed
  evidence is ineligible.
- The selected repository, plan, workflow, team, start step, turn budget, extra
  instructions, and idempotency key are explicit. Branch and base are
  mechanically derivable and matched to the selected launch.
- The guard is authorized for the selected launch surface. A legacy run uses
  its supported `aflow run` launcher; a `ui-server` or `aflowd` run uses the
  advertised authenticated web/MCP start and startup-answer path.
  Never add a CLI or tmux controller to a server-owned run.

Claim the attempt in the guard's existing external state directory before any
launch:

```bash
python3 <skill-dir>/scripts/aflow_guard_recovery.py \
  --receipt <matched-launch-receipt.json> \
  --state-dir <existing-guard-state-directory>
```

The claim is atomic and durable. Its record preserves the predecessor's
`predecessor_idempotency_key` as lineage and assigns exactly one distinct
`replacement_idempotency_key`; the emitted `launch_request.idempotency_key`
is the replacement key. A second invocation returns `already_attempted` and
launches nothing. After a successful claim, use the normal selected launch path
with that replacement key so checkpoint 1's canonical metadata
preparation owns the correction; do not rewrite the plan in the guard.

Record the external outcome with the same replacement key, without launching
again. For an acknowledged normal launch, write its successor identity to a
bounded JSON file and run:

```bash
python3 <skill-dir>/scripts/aflow_guard_recovery.py \
  --receipt <matched-launch-receipt.json> \
  --state-dir <existing-guard-state-directory> \
  --outcome acknowledged \
  --replacement-idempotency-key <replacement-key> \
  --successor-json <successor-identity.json>
```

Use the same command with `--outcome failed` or `--outcome uncertain` and no
successor file when the normal launch reports that outcome. Repeated matching
records are idempotent; a changed key or conflicting successor is blocked.
Never replay an uncertain request: reconcile it only if the same durable
replacement key later returns the same acknowledged launch. Provider recovery,
configuration changes, implementation repair, branch repair, and scope resets
are outside this exception.

All other observer boundaries remain in force: one heartbeat, one bounded
investigation, no duplicate controller, no guarded-worktree or plan edits, and
no remote writes outside this single authorized startup path.

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
- `ui-server`: run owned by `aflow ui`/`aflow ui --daemon` persistent units
  and observed through its advertised authenticated MCP endpoint; or
- `aflowd`: production control-plane run owned by its exact systemd unit.

Never add a tmux or CLI controller to either server-owned mode.

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

For a `ui-server` run:

1. Use one authenticated `get_run` through the advertised `/mcp` or `/mcp/`
   endpoint, and corroborate the UI ownership record or exact persistent unit
   when host access is available.
2. Never use browser cookies or create a disposable transport; MCP is
   header-only and UI shutdown does not signal workflow workers.
3. Report only the endpoint and run identity advertised by the selected UI
   server; never guess a host, scheme, or port.

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

## Build a bounded diagnostic report input

When an explicit report is requested, read `references/report-input.md` and
run `scripts/aflow_guard_report_input.py` against one ownership-matched
observation. For `legacy`, that is the bounded snapshot. For `ui-server` or
`aflowd`, capture the unmodified schema-1 JSON returned by one authenticated
`get_run`, record its UTC response time, and invoke:

```bash
python3 <skill-dir>/scripts/aflow_guard_report_input.py \
  --canonical-observation <absolute-external-directory>/get-run.json \
  --repo <guarded-repo> \
  --run-id <run-id> \
  --ownership-mode <ui-server|aflowd> \
  --observed-at <UTC-ISO-8601-response-time> \
  --output <absolute-external-directory>/guard-report-input.json
```

The canonical response itself has no repository or observation timestamp; the
pinned command arguments bind those facts without changing the response. The
helper produces only the versioned, bounded JSON input for the deterministic
renderer: identity, current status/activity/ownership, available checkpoint
and finalized-turn facts, worker routing, and diagnosis fields.
Healthy observations remain silent. A new orphan is limited to the documented
identity- and time-matched cause ladder; unavailable evidence stays unknown,
provider completion remains a duplicate-operation risk, and the guard never
authorizes a relaunch or other recovery.

## Produce an explicit `:vr` report

Treat `:vr` as an explicit, report-only request for the exact run already pinned
by this guard. Never resolve “latest” or accept a repository, run ID, or
ownership mode different from the pinned values. The request does not authorize
recovery, notification, issue creation, email, provider access, or any write to
the guarded repository. It also does not route through generic visualization or
image-generation tools.

For a `legacy` run, take one read-only snapshot and keep every intermediate and
final artifact in an absolute directory outside the guarded repository. Use the
existing external guard-state path when one is already configured; `--no-write`
is mandatory and `--mark-notified` is forbidden for a report:

```bash
python3 <skill-dir>/scripts/aflow_guard_snapshot.py \
  --repo <guarded-repo> \
  --run-id <run-id> \
  --state-file <external-guard-state-file> \
  --no-write > <absolute-external-directory>/guard-snapshot.json

python3 <skill-dir>/scripts/aflow_guard_report_input.py \
  --snapshot <absolute-external-directory>/guard-snapshot.json \
  --repo <guarded-repo> \
  --run-id <run-id> \
  --output <absolute-external-directory>/guard-report-input.json

uv run --script <skill-dir>/scripts/aflow_guard_report.py \
  --input <absolute-external-directory>/guard-report-input.json \
  --output-dir <absolute-external-directory>
```

The report-input builder is mandatory: it validates the pinned identity and
bounded schema before the renderer accepts the input. For `ui-server` or
`aflowd`, use one authenticated `get_run` through the advertised MCP endpoint
and its canonical ownership-matched observation instead of the legacy process
snapshot, then pass that validated observation through the same builder and
renderer. Never substitute process inspection for server ownership or add a
controller to obtain a report.

Return the generated PNG inline using its exact absolute external path. The
normalized JSON is an optional evidence link:

```markdown
![AFlow guard report](/absolute/external/path/guard-report.png)

[Normalized report JSON](/absolute/external/path/guard-report.json)
```

The renderer produces only the bounded deterministic A4 PNG and normalized
JSON. Do not create HTML or PDF, send email, write guarded artifacts, or start
recovery as part of `:vr`.

### Diagnose a new anomaly before owner action

For a new anomaly fingerprint, produce one bounded diagnostic report before
requesting owner action. Separate confirmed facts from likely or unknown cause;
use `unknown` when evidence does not directly support a cause, and show the
missing or contradictory evidence. State the exact decision the owner must make
and preserve duplicate-operation risk when provider completion is unknown. The
guard may say to inspect the exact recorded operation and choose an action, but
must never recommend a blind relaunch. Healthy scheduled ticks remain silent,
and an unchanged fingerprint receives no second report or owner request.

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
