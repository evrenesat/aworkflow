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
switches current atomically, and then performs an authenticated /ready request.
A failed readiness check restores the prior current target and aflowd.service;
without a prior target it stops and disables only aflowd.

The service is intentionally bound to 100.103.69.9:8765; it does not bind
loopback or eth0. SSH remains the installation and emergency transport. The
service hardening leaves only the explicit project and /var/lib/aflowd
writable, while retaining the systemd access necessary to create independent
workflow units.

## Operation, rollback, and rotation

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

## Mac MCP client

Copy aflow-control-plane.mcp.example.toml into the Mac Codex configuration and
provide AFLOW_CONTROL_PLANE_TOKEN from OS secret storage or the launch
environment. The example keeps required=false and requires approval for every
write. Validate the final configuration before use:

    python3 deploy/aflowd/validate-mcp-config.py ~/.codex/config.toml

The validator rejects query strings, fragments, userinfo, or a literal bearer
token in the URL.
