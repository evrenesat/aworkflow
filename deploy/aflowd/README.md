# p100 private AFlow control-plane deployment

This directory is the environment-specific systemd deployment for p100. It
installs one immutable, commit-addressed release, binds the backend to
`127.0.0.1:8765`, and uses private Tailscale Serve HTTPS as the user-facing
entry point. REST plus SSE is the canonical control contract. MCP is optional;
the same authenticated MCP registry is mounted by the UI/server application at
`/mcp` and `/mcp/`. ACP is deferred. Codex is only an optional engine harness.

The `aflowd.service` unit is retained as the systemd deployment boundary and
continues to execute `aflow-app-server`. The standalone `aflowd` package
executable is removed and is not staged in release `bin/` directories. Local
users should run `aflow ui`; this deployment runbook covers the retained
systemd service, release paths, registry, and state directories.

## MCP access

After authenticated readiness succeeds, connect to the private HTTPS URL from
Tailscale Serve plus `/mcp` (or `/mcp/`) and send the configured bearer token
in the `Authorization` header. Use the [secret-free client template](../../apps/aflow_app/server/aflow-control-plane.mcp.example.toml);
never put the token in a URL or request body. The endpoint exposes the shared
14-tool registry and three resource templates without opening another port.

The deployment uses one managed project root and one versioned registry at
`/var/lib/aflowd/projects.json`. Registry records contain relative project roots.
Every project owns exactly `.aflow/config/aflow.toml` and
`.aflow/config/workflows.toml`. The shared release-local `aflow` executable,
release identity, child environment, and mode-0600 bearer EnvironmentFile stay
in the server config and are never copied into a registry record.

## 1. Prepare and inspect

Keep `/etc/aflowd/aflowd.env` outside the repository, owned by the service user,
mode 0600, with one opaque `AFLOW_APP_TOKEN=...` line. Do not put the token in a
URL, snapshot, log, or project config.

Run the read-only preflight before staging or migration. Name the current HTTP
backend explicitly because the pre-migration release may still use its former
Tailscale address; the new release always uses loopback. The project config root
must contain the existing two-document pair that will be migrated.

```sh
sudo deploy/aflowd/preflight.sh \
  --output-dir /root/code/evidence/aflowd-rollout/preflight \
  --current-backend-url http://CURRENT_PRIVATE_ADDRESS:8765 \
  --project-config-root /root/code/PROJECT/existing-config
```

Preflight snapshots the current unit, release link, registry if present, config
pair, bearer file metadata, Tailscale status, and
`tailscale serve get-config --all`. It authenticates to the current service only
for bounded reads of `/ready`, projects, and paginated run status. It records
only project/run IDs and status fields. Any active systemd workflow unit, CLI or
local-daemon controller, nonterminal/`needs_attention`/`manifest_only` run,
project readiness error, pagination overflow, or API ambiguity stops the
rollout. Resolve ownership through the existing control-plane workflow before
rerunning preflight; do not hand-edit its result.

## 2. Stage the immutable release

The installer builds from one exact commit. `--stage-only` validates the built
web/server entrypoints, release config, registry if present, and a manifest that
hashes all deployed entrypoints, config, and web entrypoint. It does not switch
`current` or touch systemd.

```sh
sudo deploy/aflowd/install.sh \
  --source /path/to/reviewed/aflow \
  --commit FULL_40_CHARACTER_COMMIT \
  --managed-projects-root /root/code \
  --project-registry-path /var/lib/aflowd/projects.json \
  --stage-only --apply
```

An existing release directory is reused only when its config and complete
manifest still match. A stale, partial, or differently configured release is
rejected; use a new reviewed source commit instead of modifying a release.

## 3. Migrate one existing project

First run the command without `--apply`. The source files must be regular UTF-8
TOML files inside the exact Git project root. Existing destination files are
accepted only when both already exist with identical bytes; no file is
overwritten.

```sh
sudo /opt/aflowd/releases/FULL_40_CHARACTER_COMMIT/src/deploy/aflowd/migrate-registry.py prepare \
  --release /opt/aflowd/releases/FULL_40_CHARACTER_COMMIT \
  --managed-projects-root /root/code \
  --project-root /root/code/PROJECT \
  --project-id PROJECT_SLUG \
  --display-name 'Project display name' \
  --source-aflow-config /root/code/PROJECT/existing-config/aflow.toml \
  --source-workflows-config /root/code/PROJECT/existing-config/workflows.toml \
  --registry-path /var/lib/aflowd/projects.json \
  --backup-root /var/lib/aflowd/migrations
```

Repeat with `--apply` after checking every printed path. The command validates
the accepted release snapshot, stages both config documents together, publishes
the registry last with fsync/replace, and prints its transaction directory. On a
normal error it restores the prior registry and removes only the unchanged pair
it created. Existing `.aflow/runs`, plans, Git history, and unrelated `.aflow`
content are never removed. Additional projects can be seeded by repeating the
same command with a distinct ID and non-overlapping relative root.

## 4. Switch the service and enable private HTTPS

Use the same fresh preflight snapshot. The service writes only beneath the
configured managed root and `/var/lib/aflowd`. Workflow units retain independent
`Restart=no` ownership; restarting `aflowd.service` does not restart them.

```sh
sudo deploy/aflowd/install.sh \
  --source /path/to/reviewed/aflow \
  --commit FULL_40_CHARACTER_COMMIT \
  --managed-projects-root /root/code \
  --project-registry-path /var/lib/aflowd/projects.json \
  --preflight-snapshot /root/code/evidence/aflowd-rollout/preflight \
  --apply
```

The installer switches the release-pinned service and checks authenticated
readiness through `http://127.0.0.1:8765/ready`. It restores the former service
and `current` link if restart or readiness fails.

Apply the owned private HTTPS mapping using the preflight Serve snapshot:

```sh
sudo deploy/aflowd/serve-private-https.sh \
  --snapshot /root/code/evidence/aflowd-rollout/preflight/tailscale-serve.before.json \
  --evidence-dir /root/code/evidence/aflowd-rollout/serve \
  --apply
```

The exact command is
`tailscale serve --bg --https=443 127.0.0.1:8765`. The script does not call
Funnel, reset Serve, or edit tailnet grants. It validates the private HTTPS 443
target, absence of Funnel in status, and the machine MagicDNS name. Discover the
stable URL from `serve/private-url.txt`; direct Tailscale-IP HTTP is no longer a
supported entry point.

Verify the deployed source and network contract:

```sh
sudo deploy/aflowd/status.sh
sudo tailscale serve status --json
```

Then complete authenticated browser/client acceptance for project, config,
plan, start, SSE status, safe control, stop, and successor restart. Keep tokens,
prompts, and config bodies out of shared evidence.

## 5. Continuous deployment of validated main

An optional bounded poller installs validated main automatically when no
workflow is active. It is enabled only by the explicit owner step below;
nothing deploys until then.

**Trusted boundary.** `continuous-deploy.py` deploys exactly one kind of
candidate: the exact fetched `main` tip of
`https://github.com/evrenesat/aworkflow.git` that descends from the current
release and has a completed, successful `ci.yml` run for a push to `main` with
that exact head SHA (checked against the public GitHub Actions API, no
credentials). Missing, pending, failed, or identity-mismatched CI, divergence,
unknown current releases, and network errors defer with a sanitized reason and
never touch the running service. Candidate code does not execute until every
gate has passed.

**Run preservation.** While any workflow unit, controller, or nonterminal run
is active, the candidate's own `preflight.sh` refuses the rollout and the
poller defers; the release, controller, and runs stay untouched, and a later
safe poll proceeds automatically.

**Existing machinery stays authoritative.** The poller owns no deployment or
rollback engine. After the gates pass it detaches its dedicated clean clone at
the candidate and invokes that candidate's `preflight.sh` and `install.sh`
with the production defaults (release root `/opt/aflowd`, unit
`/etc/systemd/system/aflowd.service`, loopback `http://127.0.0.1:8765`,
environment `/etc/aflowd/aflowd.env`, managed root `/root/code`, registry
`/var/lib/aflowd/projects.json`, project config
`/root/code/aflow-control-plane-proof-20260811/.aflow/config`), so readiness
checks and installer rollback behave exactly as in a manual rollout. The
poller's clone lives under `/var/lib/aflowd/deploy/source`; an unexpected or
dirty source defers and is never reset or cleaned. Each attempted deployment
keeps its preflight snapshot and installer log under
`/var/lib/aflowd/deploy/attempts/`; refused or failed preflight temporaries
are discarded. A nonzero preflight defers only when it wrote a valid
schema-1 snapshot recording `safe_to_rollout: false`; missing, malformed, or
safe-but-failed preflight output fails the poll instead. An installer that
outlives its bounded timeout has its whole process group terminated before
the poll reports failure, including a direct `--retry-failed` run. Every poll
writes an atomic
`/var/lib/aflowd/deploy/status.json` (phase, reason, commits, attempt paths)
and prints the same phase/reason to the journal; `status.sh` surfaces both.
A successful install records the deployed candidate as `current_commit`
immediately, and a failed final status write makes the poll exit nonzero so
systemd or the direct caller sees the operational failure.

**Failed candidates never retry on their own.** A failed rollout records
`last_failed_commit` in `status.json`; the same candidate is skipped until a
new candidate appears or an operator explicitly reattempts it.

**Bootstrap versus acceptance.** Installing the timer is only the bootstrap.
Actual continuous deployment acceptance on p100 means the owner observes a
timer-triggered deferral during an active run and then a validated-main
transition after workflows finish. Publishing `main` publicly remains a
separate, explicit owner authorization; this poller deploys only what is
already on the public main branch with green CI.

Install, inspect, disable, and reattempt:

```sh
sudo deploy/aflowd/install-continuous-deploy.sh                 # dry-run plan
sudo deploy/aflowd/install-continuous-deploy.sh --apply         # install units + enable timer
sudo deploy/aflowd/status.sh                                    # shows timer + deployment status
sudo systemctl disable --now aflowd-deploy.timer                # stop polling
sudo /opt/aflowd/current/src/deploy/aflowd/continuous-deploy.py --retry-failed
```

## Rollback

Restore Serve first. The script compares the current all-service config with the
exact post-change snapshot before restoring the pre-change snapshot. Any drift
blocks rollback so unrelated mappings are preserved for operator review.

```sh
sudo deploy/aflowd/serve-private-https.sh --rollback \
  --snapshot /root/code/evidence/aflowd-rollout/preflight/tailscale-serve.before.json \
  --expected-current /root/code/evidence/aflowd-rollout/serve/tailscale-serve.after-config.json \
  --apply
sudo /opt/aflowd/releases/FULL_40_CHARACTER_COMMIT/src/deploy/aflowd/rollback.sh \
  --release PRIOR_40_CHARACTER_COMMIT \
  --service-snapshot /root/code/evidence/aflowd-rollout/preflight/aflowd.service.before
sudo /opt/aflowd/releases/FULL_40_CHARACTER_COMMIT/src/deploy/aflowd/migrate-registry.py rollback \
  --transaction /var/lib/aflowd/migrations/EXACT_TRANSACTION --apply
```

Migration rollback refuses to overwrite a changed registry or changed/new
project config content. Release rollback validates the selected manifest and
that the preflight unit snapshot pins its executable and config to that release,
then atomically restores the exact unit bytes and `current` link before restarting
only `aflowd.service`. `uninstall-emergency.sh` stops and disables only
the daemon; it never deletes releases, registry state, runs, plans, projects, or
secrets.

For diagnosis, use `status.sh` and bounded `journalctl -u aflowd.service` output.
A missing, failed, or ambiguous workflow unit stays `needs_attention`; use the
existing owner/control-plane lifecycle rather than manually restarting the unit.

Preflight treats a durable `owner_stopped` launch phase as terminal even when
an older server still projects its historical startup question. Active workflow
units and controllers continue to block rollout. The question record is preserved.
