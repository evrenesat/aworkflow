# p100 AFlow control-plane deployment

install.sh creates one immutable, commit-addressed release under
/opt/aflowd/releases/<commit>. It defaults to a dry-run and never uses a
developer checkout as a daemon or workflow entrypoint. The rendered
aflowd.service runs only the release-pinned aflow-app-server; each workflow is
a separate systemd-run unit with Restart=no, so restarting the control plane
does not restart or stop a workflow.

## Prepare and install

Keep /etc/aflowd/aflowd.env outside the repository, owned by the service user,
mode 0600, and containing one opaque AFLOW_APP_TOKEN=... line. The default
allowlist is exactly /root/code/aflow-control-plane with
/root/code/aflow-control-plane/aflow/aflow.toml.

    deploy/aflowd/install.sh --source /path/to/reviewed/aflow --commit <40-char-commit>
    sudo deploy/aflowd/install.sh --source /path/to/reviewed/aflow --commit <40-char-commit> --apply

The dry-run prints the exact source, release destination, service, bind
address, allowlist, and rollback target. --apply verifies that 100.103.69.9 is
on tailscale0, builds the server and web app in the staged release, validates
all three entrypoints and their hashes, renders a release-pinned config/service,
switches current atomically, and then polls an authenticated /ready request.
Release entrypoints use Python safe-path mode so the service working directory
cannot shadow the immutable package. A failed readiness check restores the
prior current target and the prior release-pinned aflowd.service together;
without a prior target it stops and disables only aflowd.

The service is intentionally bound to 100.103.69.9:8765; it does not bind
loopback or eth0. SSH remains the installation and emergency transport. The
service hardening leaves only the explicit project and /var/lib/aflowd
writable, while retaining the systemd access necessary to create independent
workflow units.

## Operation, rollback, and rotation
Rollback moves both current and aflowd.service ExecStart to the selected
immutable release before restarting only the daemon.


    sudo deploy/aflowd/status.sh
    sudo deploy/aflowd/rollback.sh --release <40-char-prior-commit>
    sudo deploy/aflowd/uninstall-emergency.sh

uninstall-emergency.sh only stops/disables aflowd; it never deletes releases,
runs, manifests, worktrees, plans, or /etc/aflowd secrets.

To rotate the bearer, atomically replace the mode-0600 environment file and
restart only the daemon:

    sudo install -o root -g root -m 0600 /secure/new-aflowd.env /etc/aflowd/aflowd.env
    sudo systemctl restart aflowd.service

New workflow units receive the rotated environment. Existing workflow units are
not restarted.

## Diagnosis and ownership

Start with `sudo deploy/aflowd/status.sh`; it prints the resolved `current`
release before the service status. Use `sudo journalctl -u aflowd.service -n
100 --no-pager` for a local diagnosis, but redact output before sharing it. If
an install fails readiness, the installer has already restored the prior
release/service state. Do not repair a failed install by manually repointing
`current`; use the explicit rollback command once the prior 40-character
release identity is known.

`aflowd.service` owns only the control-plane process. It may restart without
touching a workflow unit. A workflow unit with a failed, missing, or ambiguous
identity is intentionally reported as `needs_attention`; do not use
`systemctl restart` on that unit. Authenticate to the control plane and request
an explicit resume, which creates a linked continuation rather than reviving
the old identity. Owner stop is terminal.

The optional `aflow-guard-development-run` skill remains scoped to an
explicitly guarded normal/legacy AFlow workflow. It is not a daemon watchdog
and must not create a competing controller or heartbeat automation for a
daemon-owned workflow.

## Mac MCP client

Copy aflow-control-plane.mcp.example.toml into the Mac Codex configuration and
provide AFLOW_CONTROL_PLANE_TOKEN from OS secret storage or the launch
environment. The example keeps required=false and requires approval for every
write. Validate the final configuration before use:

    python3 deploy/aflowd/validate-mcp-config.py ~/.codex/config.toml

The validator rejects query strings, fragments, userinfo, or a literal bearer
token in the URL.
