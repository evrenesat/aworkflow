# p100 private AFlow control-plane deployment

This directory is the environment-specific systemd deployment for p100. It
installs one immutable, commit-addressed release, binds the backend to
`127.0.0.1:8765`, and uses private Tailscale Serve HTTPS as the user-facing
entry point. REST plus SSE is the canonical control contract. MCP is optional;
ACP is deferred. Codex is only an optional engine harness.

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

## Rollback

Restore Serve first. The script compares the current all-service config with the
exact post-change snapshot before restoring the pre-change snapshot. Any drift
blocks rollback so unrelated mappings are preserved for operator review.

```sh
sudo deploy/aflowd/serve-private-https.sh --rollback \
  --snapshot /root/code/evidence/aflowd-rollout/preflight/tailscale-serve.before.json \
  --expected-current /root/code/evidence/aflowd-rollout/serve/tailscale-serve.after-config.json \
  --apply
sudo deploy/aflowd/rollback.sh --release PRIOR_40_CHARACTER_COMMIT
sudo /opt/aflowd/releases/FULL_40_CHARACTER_COMMIT/src/deploy/aflowd/migrate-registry.py rollback \
  --transaction /var/lib/aflowd/migrations/EXACT_TRANSACTION --apply
```

Migration rollback refuses to overwrite a changed registry or changed/new
project config content. Release rollback validates the selected manifest and
restarts only `aflowd.service`. `uninstall-emergency.sh` stops and disables only
the daemon; it never deletes releases, registry state, runs, plans, projects, or
secrets.

For diagnosis, use `status.sh` and bounded `journalctl -u aflowd.service` output.
A missing, failed, or ambiguous workflow unit stays `needs_attention`; use the
existing owner/control-plane lifecycle rather than manually restarting the unit.
