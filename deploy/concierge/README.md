# AFlow owner-issue concierge (p100)

This directory contains the inactive systemd units for the p100 owner-issue
concierge. The timer schedules a bounded, non-overlapping tick every 20
minutes (`OnCalendar=*:00,20,40`, host clock in UTC, `Persistent=true`).

The concierge **process** is the authoritative decision and execution
boundary. Each tick reads the queue through the local AFlow MCP endpoint
(`http://127.0.0.1:8765/mcp`, environment-variable-backed bearer
authentication), reads owner-issue evidence from the GitHub REST API
(`GITHUB_TOKEN` from the same environment file), follows the packaged
one-plan triage policy, and performs at most one bounded MCP action per
tick. The tick then runs one short-lived advisory Codex CLI review using
`gpt-6.1-sol` at `xhigh` reasoning effort; that review is strictly
read-only and never mutates AFlow state.

The concierge never edits implementation code, never uses a non-MCP AFlow
control channel, never performs blind recovery, never starts a second
active plan, and never persists raw transcripts. Only a bounded, redacted
status record (schema v2, including `action`, `outcome`, `mutating`,
`planner_model`, and `planner_effort`) is written.

The units are **not** activated from a worker checkout. Activation is a
post-delivery step on the p100 host.

## Units

- `aflow-concierge.timer` — `OnCalendar=*:00,20,40`, `Persistent=true`,
  triggers `aflow-concierge.service`.
- `aflow-concierge.service` — `Type=oneshot`, single-process lock
  (`/var/lib/aflowd/concierge/concierge.lock`), 15-minute internal timeout,
  systemd `TimeoutStartSec=16min`. Both are strictly below the 20-minute
  interval, so ticks never overlap.

The service reads `AFLOW_APP_TOKEN` from `EnvironmentFile=/etc/aflowd/aflowd.env`
on every start. The token value must never appear in unit files, prompts,
argv, status records, logs, or issues. `AFLOW_APP_TOKEN` authenticates only
the local AFlow MCP endpoint; Codex model authentication is separate and
lives in the private `CODEX_HOME` described below. The same environment file
may define `GITHUB_TOKEN`, which the concierge process uses only for
read-only owner-issue evidence from the GitHub REST API.

The tick runs from `WorkingDirectory=/root/code`. That parent directory is
not a Git repository on p100, and the advisory tick review is strictly
read-only, so the launcher passes `--skip-git-repo-check` to `codex exec`
instead of requiring a Git checkout.

The concierge serves the registered project root
`/root/code/agent-flow` (override with `--project-root`). The unit grants
`ReadWritePaths=/var/lib/aflowd /root/code/agent-flow` because the
read-only planner creates its isolated worktree under
`/root/code/agent-flow/.aflow/intake-planning/<claim>` while planning;
bounded planner workspace records live under
`/var/lib/aflowd/concierge/planner-records/`.

The unit sets `CODEX_HOME=/var/lib/aflowd/concierge/codex-home`. That home
must exist and hold a usable Codex login **before** the timer is enabled;
the installation procedure below provisions it and stops before activation
if the login preflight fails.

## Tick policy (one-plan triage)

Each tick reads the queue through MCP (`list_projects` by exact Git root,
paginated `list_runs`, `list_plans`, `read_plan` for every `todo`,
`in-progress`, `failed`, and `done` plan document, then `get_run` for
reconciliation) plus read-only GitHub open-issue evidence, and chooses at
most one action, in priority order:

1. Resume a confirmed-inactive failed run whose exact plan lineage is
   verified, reusing the same idempotency key.
2. Start exactly one launchable `todo`/`draft` plan with the
   `checkpoint_delivery` workflow and the explicit `xtx-mtp` team, after an
   exact-path preflight.
3. For the oldest open issue authored by GitHub user ID `591691` that no
   plan document already covers, author one plan with the read-only
   `gpt-6-astra` high-effort planner following `aflow-plan`. The plan must
   contain the issue URL, an acceptance mapping, exact verification
   commands, and safe defaults, and is validated before MCP plan create,
   update, or promote; the tick then preflights and starts once.

Duplicate detection is content-based: a plan covers an issue when its
document references the canonical issue URL
(`https://github.com/<owner>/<repo>/issues/<number>`). A plan whose
document cannot be read while owner issues are pending reports
`plan_evidence_unavailable` instead of guessing, and a duplicate detected
again immediately before `create_plan` aborts without authoring.

Before any mutating action the tick re-reads fresh evidence and re-runs the
triage decision; a changed state aborts the action as
`state_changed_before_action`. A mutating call whose outcome is uncertain
(timeout or transport failure) is reconciled by read-back (`get_run` /
paginated `list_runs`) before reporting failure.

Before `start_run`, the tick reads the project's MCP capabilities projection
and requires the project-local Git config to grant exactly
`aflow.publishRemote=origin` and `aflow.publishBranch=main`. The concierge
never reads Git directly; it trusts only that projection. If the projection
is absent or unreadable the tick reports `launch_evidence_unavailable`; if it
is present but not exactly `origin`/`main` it reports
`launch_evidence_contradictory`. In both cases no `start_run` is issued. The
global workflow config is still read, but only for resolved lifecycle
validation.

Any active or uncertain run defers the tick without mutation. Historical
`in-progress` plan files are not launch-ready. Issues from any other
account, including issue 50, are ignored. Ambiguous ownership or uncertain
provider outcomes are reported without a guessed action.

## Post-delivery installation

After this plan is reviewed, merged to `origin/main`, and the CI-gated
`aflow-ui-deploy` timer has deployed the reviewed commit to
`/opt/aflowd/current` on p100:

1. Verify the current release contains the reviewed commit and that
   `deploy/concierge/` exists under `/opt/aflowd/current/src`.
2. Verify the private host config is present and mode `0600`:
   `stat -c '%a' /etc/aflowd/aflowd.env`. It must define `AFLOW_APP_TOKEN=...`.
3. Verify the host clock is UTC: `timedatectl`.
4. Provision the private Codex home the unit points at
   (`CODEX_HOME=/var/lib/aflowd/concierge/codex-home`). The currently
   authenticated root home is `/root/.codex`; copy its login material into
   the private home. Login values are copied, never printed, and never
   tracked in this repository:

   ```sh
   sudo install -d -m 0755 /var/lib/aflowd/concierge
   sudo install -d -m 0700 /var/lib/aflowd/concierge/codex-home
   sudo cp -p /root/.codex/auth.json /var/lib/aflowd/concierge/codex-home/auth.json
   sudo chmod 0600 /var/lib/aflowd/concierge/codex-home/auth.json
   ```

   If the operator re-authenticates Codex later, repeat this copy so the
   configured home stays current.
5. Preflight the exact configured home and stop before activation if the
   login is not usable. Do not run `systemctl enable --now` while this
   check fails:

   ```sh
   sudo bash -c 'CODEX_HOME=/var/lib/aflowd/concierge/codex-home codex login status' \
     || { echo "aflow-concierge: no usable codex login in /var/lib/aflowd/concierge/codex-home; not enabling the timer" >&2; exit 1; }
   ```

6. Install and activate:

   ```sh
   sudo install -D -m 0644 /opt/aflowd/current/src/deploy/concierge/aflow-concierge.service /etc/systemd/system/aflow-concierge.service
   sudo install -D -m 0644 /opt/aflowd/current/src/deploy/concierge/aflow-concierge.timer /etc/systemd/system/aflow-concierge.timer
   sudo systemctl daemon-reload
   sudo systemctl enable --now aflow-concierge.timer
   ```

7. Dry-run one tick without touching AFlow state (sources the private file
   inside `sudo` so the token never appears on a command line):

   ```sh
   sudo bash -c 'set -a; . /etc/aflowd/aflowd.env; set +a; /opt/aflowd/current/venv/bin/python -P -c "from aflow.concierge import main; raise SystemExit(main())" --dry-run'
   ```

8. Run one bounded tick and inspect the bounded, redacted status:

   ```sh
   sudo systemctl start aflow-concierge.service
   sudo cat /var/lib/aflowd/concierge/status.json
   sudo journalctl -u aflow-concierge.service --since -30min
   ```

## Idempotency and stop

Re-running the install commands against the same reviewed release is safe:
`install -D` overwrites with identical bytes and re-enabling an active timer
is a no-op. To stop the concierge without touching AFlow state:

```sh
sudo systemctl disable --now aflow-concierge.timer
```

The service is `Type=oneshot` with no restart policy. A failed, timed-out,
or overflowed tick leaves a bounded status record and the timer simply
schedules the next tick. A tick that starts while another owns the lock
defers with `phase=deferred` and exits 0.
