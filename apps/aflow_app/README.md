# aflow Remote App

A mobile-friendly workflow control interface for registered AFlow projects. It supports project registration, revisioned configuration and Markdown plan editing, durable run control, event streaming, and MCP. Agent selection stays in the normal AFlow harness configuration; the remote app contains no provider-specific planning client.

The server sources also ship inside the published `aworkflow` wheel, where `aflow ui` serves them with bundled web assets and the shared global configuration. This subproject remains the development/test entry point. Full behavior and API details are in [Remote workflow control app](../../docs/remote-app.md).

## Run history and settings

All runs and project Runs offer Visible, Archived, and All history filters.
Archive preserves direct links and supports Restore. Delete record requires
confirmation and returns a Deleted record page on subsequent access; workflow
files, logs, snapshots, plans, and recovery/idempotency records remain intact.
Hiding an active run requires acknowledging that execution will continue.

Starting requires current activity evidence. Missing or untrusted activity is
Needs attention, separately listed outside Recent; confirmed execution failures
are Failed and preparation failures are Could not start.

Run history uses `POST .../runs/{run_id}/archive`, `POST .../restore`, and
`DELETE .../runs/{run_id}` beneath the existing project route. Send
`Idempotency-Key` and a JSON `expected_revision` (the run's `history_revision`),
plus `acknowledge_active: true` when hiding active work. Lists accept
`history=visible|archived|all`; deleted external reads return HTTP 410.

Runs and the Teams, Workflows, and Prompts editors have independent scrolling
menus. Settings keeps one shared draft and save action. Advanced TOML replaces
all guided tabs until switching back; Connection settings TOML stays in General.
Clear effort text to unset it, then Save all changes.

## Development

```bash
npm --prefix apps/aflow_app/web install
npm --prefix apps/aflow_app/web test -- --run
npm --prefix apps/aflow_app/web run build
uv sync --project apps/aflow_app/server
uv run --project apps/aflow_app/server pytest -q
```

Run the built app:

```bash
AFLOW_APP_TOKEN=secret uv run --project apps/aflow_app/server aflow-app-server
```

The server requires Python 3.11 or newer and binds to `127.0.0.1:8765` by default. It reads the exact registered Git roots beneath `AFLOW_MANAGED_PROJECTS_ROOT`; no directory scan grants access. Supply bearer credentials only through the `Authorization` header. All projects share the global workflow pair at `~/.config/aflow/aflow.toml` plus `workflows.toml`; per-project configuration is neither read nor written.

Plan editing is limited to regular UTF-8 Markdown files beneath `plans/todo`, `plans/in-progress`, and `plans/done`. Updates and lifecycle moves use SHA-256 expected revisions.
