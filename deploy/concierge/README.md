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

Each tick builds a complete bounded inventory through MCP (`list_projects`
by exact Git root, `list_runs` paged with the opaque `next_cursor`,
`list_plans` paged by exact last path until the final empty page,
`list_plan_documents` joined to the lifecycle rows by exact canonical path,
and `read_plan` for every plan document, including `failed` and
`needs-plan-change`), then reads the project queue, fresh `get_run` and
`get_run_context` recovery evidence for inactive recoverable runs, and
read-only GitHub open-issue evidence, and chooses at most one action, in
priority order:

1. Defer on active or uncertain shared occupancy.
2. Repair a failed exact-SHA delivery using only an admissible repair resume
   backed by structured publication-failure evidence, or a ready repair plan
   that names the current `origin/main` SHA and the failed gate.
 3. Resume one safe failed lineage: a unique lineage leaf whose fresh
    `get_run` detail proves inactive activity, `failed` / `interrupted` /
    `stopped` status, `control_plane` ownership, exact canonical
    original-plan identity, and `evidence.can_resume=true`, with a readable
    plan document containing an unchecked strict checkpoint, and no conflicting
    queue-row run identity. Lineage parents are read from list rows or bounded
    lite `get_run_context` metadata; ambiguous multi-run lineage is held.
    Owner stops, restart/requeue-required metadata, and `transition_end` runs
    with unchecked checkpoints are not resumed.
4. Start exactly one ready `in_progress` plan with an unchecked strict
   checkpoint, a readable document, exactly one admissible canonical queue
   row, satisfied `done` prerequisites, and valid owner-issue references,
   using the
   `checkpoint_delivery` workflow and the explicit `xtx-mtp` team after an
   exact-path preflight. The concierge never directly starts `todo`,
   `draft`, `failed`, `needs_plan_change`, or `done` plans. Legacy `draft`
   rows remain in the joined inventory as held rows and never suppress an
   independent admissible ready plan.
5. For the oldest open issue authored by GitHub user ID `591691` that no
   plan document already covers, author one plan with the read-only
   `gpt-6-astra` high-effort planner following `aflow-plan`. The plan must
   contain the issue URL, an acceptance mapping, exact verification
   commands, and safe defaults, and is validated before MCP plan create,
   update, or promote; the tick then preflights and starts once.

Candidates are ranked deterministically: plans referencing open owner issues
rank before operator-promoted plans, then by eligible issue
`(created_at, number)`, plan `modified_at`, canonical plan path, and run ID.
Input or page order never selects work. Candidate-local ambiguity is
reported without blocking independent safe candidates.

Duplicate detection is content-based: a plan covers an issue when its
document references the canonical issue URL
(`https://github.com/<owner>/<repo>/issues/<number>`). A plan whose
document cannot be read while owner issues are pending reports
`plan_evidence_unavailable` instead of guessing, and a duplicate detected
again immediately before `create_plan` aborts without authoring. The
open-issue feed is paged with a five-page cap; a missing repository
identity, a feed read error, a repeated page, or an unconsumed
continuation at that cap reports `github_evidence_unavailable` with zero
mutations before defect filing, resume, start, or plan authoring, and
never permits selection from partial issue evidence.

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

Occupancy is one conservative classifier applied at initial triage, fresh
selection, post-planning, and timeout reconciliation. Any active run
(active activity, active unit, or active preparation) defers the tick
without mutation. Canonical terminal runs (`completed`, `failed`, `interrupted`, `stopped`,
`owner_stopped`) whose activity is canonical (`unknown` or `inactive`) and
which have no conflicting active evidence are history and never grant resume. A terminal row whose activity is missing or malformed is
not provably historical and remains blocking until fresh canonical `get_run`
detail resolves it. Uncertain, paused, waiting, malformed, or
missing-evidence runs remain blocking. A run that verifiably failed before
any agent started (manifest-only launch phase, no agent started, unit
observation missing, no active preparation, and canonical queue capacity
available) holds only its own plan and does not block independent work, but
only when the fresh `get_run` detail carries a nonempty plan path that
exactly matches the list row's plan path; a missing or mismatched identity
cannot establish which plan the startup record holds and keeps the run
blocking. A verified startup hold is never answered, discarded, or retried
by the tick. A missing,
malformed, or conflicting inventory page, a duplicate identity, a repeated
or non-advancing cursor, or an unconsumed continuation at the page cap
reports a bounded evidence gap with zero mutations, and a partial inventory
is never treated as complete. An `in_progress` plan is launch-ready only
when its document has an unchecked strict checkpoint and it has exactly one
admissible canonical queue row. Issues from any other account, including
issue 50, are ignored.
Ambiguous ownership or uncertain provider outcomes are reported without a
guessed action.

## Delivery gate and defect reporting

After triage, and before any fresh dispatch, the tick evaluates an exact-SHA
delivery gate for the current remote `main` tip SHA and reports three facts
separately. The remote tip is resolved with a bounded read-only
`git ls-remote` (never a stale local `origin/main` tracking ref, and never a
fetch or shared-ref mutation); an unreachable remote yields `pending`
evidence.

- **CI**: `green`, `red`, or `pending`, from only the deploy poller's
  qualifying CI workflow runs for that exact SHA (exact `head_sha`,
  `event=push`, `head_branch=main`, `path=.github/workflows/ci.yml`). An
  unrelated workflow on the same SHA neither greens a failing CI run nor
  reds a successful one. Among qualifying runs the latest attempt by
  `(run_number, run_attempt)` decides: a completed `success` is `green`, a
  completed `failure`/`cancelled` is `red`, and a non-terminal latest run
  is `pending`. An earlier attempt never overrides a later one.
- **Deploy phase**: the `phase` from
  `/var/lib/aflowd/deploy/status.json` (for example `deployed`,
  `waiting_for_ci`, `failed`).
- **Live release**: `installed`, `stale`, or `missing`, from the
  `/opt/aflowd/current` release directory name compared against the remote
  `main` tip.

The projection is best-effort and bounded: any missing or unreadable source
collapses to absent evidence, which is reported as `pending`, never as
success. A **failed** exact-SHA delivery (red CI, failed live verification,
or a failed deploy phase) admits only a matching repair candidate: an
admissible recovery resume with structured delivery-stage or
`run_metadata` publication-failure evidence, or a ready `in_progress`
repair plan whose document names the current `origin/main` SHA and the
failed gate. Every unrelated `start`, `resume`, and `plan_and_start`
candidate is suppressed and the tick reports `delivery_gate_failed` until
the release is repaired. The CI and live-release facts are always reported
separately in the bounded status record, so a green CI with a stale live
release is visible as `pending`, not `ok`.

Defect filing is driven exclusively by the control plane's validated
`defect_confirmation` projection. The tick reads each observed run through
the live MCP `get_run` tool and files a defect only when the run carries a
strictly validated `defect_confirmation` mapping (schema version 1,
`engine_internal_assertion`, `controller`, a bounded package-relative
component, a bounded site, and a 64-hex signature). Missing, malformed, or
legacy data is ignored, so generic `startup_failed`, `controller_failed`,
implementation, environmental, admission, provider, configuration, or
uncertain failures file nothing. Filing deduplicates by trusted defect
signature rather than run instance. Each tick processes the distinct
confirmed signatures in stable order, searches the target repository's
issues (open and closed) before filing, files the first signature not
already filed in that repository, and stops for the tick; a matching
fingerprint in another repository never satisfies this repository's
deduplication. A failed dedup search or filing stays report-only, an
already-filed signature never blocks a different unfiled one, and once
every distinct signature is filed the tick continues to the eligible safe
action.
Any issue body carries only bounded sanitized evidence and never includes
transcripts, tokens, stack traces, or raw logs.

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
