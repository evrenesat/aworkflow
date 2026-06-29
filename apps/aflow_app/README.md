# aflow Remote App

A mobile-first remote management interface for AFlow workflows.

The remote app provides project discovery, Codex thread reuse for plan creation, plan draft save/load/promote workflows, execution monitoring via SSE, and optional audio transcription. It is a separate subproject from the main `aworkflow` package and is not included in the published wheel.

Full documentation lives in [../../docs/remote-app.md](../../docs/remote-app.md).

## Running

Build the frontend:

```bash
cd apps/aflow_app/web
npm install
npm run build
```

Run the backend:

```bash
cd apps/aflow_app/server
uv sync
AFLOW_APP_TOKEN=secret uv run aflow-app-server
```

Open `http://127.0.0.1:8765/`.

## Development

```bash
cd apps/aflow_app/web
npm run dev
npm test -- --run
```

```bash
cd apps/aflow_app/server
uv run --extra dev pytest -q
```

## Configuration

The app reads environment variables and `~/.config/aflow/config.toml`. Environment variables override file values.

Common variables:

- `AFLOW_APP_TOKEN` - required API token.
- `AFLOW_APP_HOST` - bind host, default `127.0.0.1`.
- `AFLOW_APP_PORT` - bind port, default `8765`.
- `AFLOW_APP_PROJECTS_HOME` - root scanned for local git projects, default `~/code`.
- `AFLOW_CODEX_APP_SERVER_URL` - optional Codex app-server websocket URL.
- `AFLOW_TRANSCRIPTION_URL` and `AFLOW_TRANSCRIPTION_TOKEN` - optional transcription endpoint.

See [Remote App Configuration](../../docs/remote-app.md#configuration) for the full table and behavioral notes.
