# aflow Remote App

A mobile-friendly workflow control interface for registered AFlow projects. It supports project registration, revisioned configuration and Markdown plan editing, durable run control, event streaming, and MCP. Agent selection stays in the normal AFlow harness configuration; the remote app contains no provider-specific planning client.

This is a separate subproject from the published `aworkflow` wheel. Full behavior and API details are in [Remote workflow control app](../../docs/remote-app.md).

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

The server requires Python 3.12 or newer and binds to `127.0.0.1:8765` by default. It reads the exact registered Git roots beneath `AFLOW_MANAGED_PROJECTS_ROOT`; no directory scan grants access. Supply bearer credentials only through the `Authorization` header.

Plan editing is limited to regular UTF-8 Markdown files beneath `plans/todo`, `plans/in-progress`, and `plans/done`. Updates and lifecycle moves use SHA-256 expected revisions.
