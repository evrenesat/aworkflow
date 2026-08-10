# aflow Remote App

A mobile-first remote management interface for AFlow workflows.

The remote app provides project discovery, provider-neutral planning-session reuse across devices, plan draft save/load/promote workflows, execution monitoring via SSE, and optional audio transcription. Planning sessions expose provider selection, provider-advertised model and reasoning controls, archive and interruption actions, approvals, and attachment uploads when supported. Codex is implemented behind this boundary with `codex-app-server-sdk`; staged file and image metadata is supplied through deterministic turn instructions rather than claimed as native SDK attachment support. It is a separate subproject from the main `aworkflow` package and is not included in the published wheel.

Full documentation lives in [../../docs/remote-app.md](../../docs/remote-app.md).

The server also exposes a daemon-backed control-plane API under
`/api/control-plane`. It reads and mutates run state only through AFlow's
shared daemon/control-plane services; the HTTP process does not own workflow
processes. The former `/api/executions` endpoints remain deprecated
compatibility aliases for allowlisted projects.

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

The server requires Python 3.12 or newer. Uploaded planning attachments are
stored under shared aflow-managed configuration storage outside project
repositories.

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

- `AFLOW_APP_TOKEN` - required API token when no token file is configured.
- `AFLOW_APP_TOKEN_FILE` - optional token file reread for every request, so a
  bearer token can rotate without restarting the server.
- `AFLOW_APP_HOST` - bind host, default `127.0.0.1`.
- `AFLOW_APP_PORT` - bind port, default `8765`.
- `AFLOW_APP_PROJECTS_HOME` - root scanned for local git projects, default `~/code`.
- `AFLOW_PLANNING_PROVIDERS` - optional JSON provider configuration; Codex is the default provider when no explicit list is configured.
- `AFLOW_PLANNING_DEFAULT_PROVIDER` - optional default provider id for new planning sessions.
- `AFLOW_PLANNING_ATTACHMENT_ROOT` - shared attachment storage outside project repositories.
- `AFLOW_CODEX_APP_SERVER_URL` - legacy compatibility alias for the Codex provider endpoint.
- `AFLOW_TRANSCRIPTION_URL` and `AFLOW_TRANSCRIPTION_TOKEN` - optional transcription endpoint.

See [Remote App Configuration](../../docs/remote-app.md#configuration) for the full table and behavioral notes.

### Control-plane allowlist

The lifecycle API is disabled until projects are explicitly configured in the
same `config.toml`. Supply bearer credentials only in the `Authorization`
header; query-string tokens are rejected, including for SSE. Health at
`/health` is process liveness only; `/ready` and all API details require bearer
authentication.

```toml
[[control_plane.projects]]
id = "my-project"
root = "/srv/code/my-project"
config_path = "/srv/code/my-project/aflow.toml"
aflow_executable = "/opt/aflow/bin/aflow"
environment_file = "/etc/aflow/my-project.env"
release_identity = "aflow-0.1.12"
```

The service resolves this static allowlist at startup, reconciles each daemon
without starting a workflow, and returns a bounded event snapshot through the
header-authenticated SSE endpoint. Start, startup-answer, control, owner-stop,
and resume writes accept `Idempotency-Key`; controls require an
`expected_revision` compare-and-swap field.
