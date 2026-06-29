# Remote App

The remote app is a mobile-first FastAPI/React interface for managing AFlow workflows across local projects. It lives under `apps/aflow_app/` and is separate from the published `aworkflow` wheel.

It is designed for authenticated desktop-hosted local/LAN use, not direct internet exposure.

## Capabilities

- Discover local git projects under a configured projects home.
- Link Codex threads to projects by current path and historical aliases.
- Start, resume, fork, rename, and send turns to Codex app-server threads.
- Save Codex-generated plans as drafts.
- Load, delete, and promote drafts into executable in-progress plans.
- Start AFlow executions from in-progress plans.
- Stream execution events over Server-Sent Events.
- Optionally transcribe browser-recorded audio with an OpenAI-compatible Whisper endpoint.

## Project Structure

```text
apps/aflow_app/
├── server/                    # FastAPI backend
│   ├── src/aflow_app_server/
│   │   ├── config.py          # server configuration
│   │   ├── project_catalog.py # project discovery and Codex thread association
│   │   ├── project_overrides.py # persistent names, moved paths, aliases
│   │   ├── aflow_service.py   # aflow library integration
│   │   ├── codex_routes.py    # Codex thread and plan-draft routes
│   │   ├── plan_store.py      # draft and in-progress plan files
│   │   ├── transcription.py   # optional audio transcription
│   │   └── main.py            # FastAPI app and static frontend serving
│   └── tests/
└── web/                       # React/Vite frontend
    ├── src/
    │   ├── components/
    │   ├── api.ts
    │   ├── types.ts
    │   ├── App.tsx
    │   └── main.tsx
    └── tests/
```

## Configuration

Configuration is loaded from environment variables and `~/.config/aflow/config.toml`. Environment variables override file values.

| Environment variable | Config key | Default | Description |
|----------------------|------------|---------|-------------|
| `AFLOW_APP_CONFIG_DIR` | - | `~/.config/aflow` | Directory containing `config.toml`. |
| `AFLOW_APP_HOST` | `server.bind_host` | `127.0.0.1` | Bind host. |
| `AFLOW_APP_PORT` | `server.bind_port` | `8765` | Bind port. |
| `AFLOW_APP_TOKEN` | `server.auth_token` | - | Required auth token. |
| `AFLOW_APP_REGISTRY_PATH` | `server.repo_registry_path` | `<config_dir>/repos.json` | Legacy repo registry path used for migration. |
| `AFLOW_APP_PROJECTS_HOME` | `project_catalog.projects_home` or `projects.projects_home` | `~/code` | Root scanned recursively for git repositories. |
| `AFLOW_APP_PROJECT_OVERRIDES_PATH` | `project_catalog.project_overrides_path` or `projects.project_overrides_path` | `<config_dir>/project_overrides.json` | Persistent project metadata store. |
| `AFLOW_APP_WEB_DIST` | - | `apps/aflow_app/web/dist` | Override directory for built frontend assets. |
| `AFLOW_CODEX_APP_SERVER_URL` | `codex_app_server.server_url` | - | Codex app-server websocket URL. |
| `AFLOW_CODEX_APP_SERVER_TOKEN` | `codex_app_server.server_token` | - | Codex app-server token. |
| `AFLOW_CODEX_URL` | `codex.url` | - | Backward-compatible Codex URL alias. |
| `AFLOW_CODEX_TOKEN` | `codex.token` | - | Backward-compatible Codex token alias. |
| `AFLOW_TRANSCRIPTION_URL` | `transcription.server_url` | - | Optional transcription service URL. |
| `AFLOW_TRANSCRIPTION_TOKEN` | `transcription.server_token` | - | Optional transcription service token. |

Example:

```toml
[server]
bind_host = "127.0.0.1"
bind_port = 8765
auth_token = "your-secret-token"

[project_catalog]
projects_home = "~/code"
project_overrides_path = "~/.config/aflow/project_overrides.json"

[codex_app_server]
server_url = "ws://localhost:8080"
server_token = "codex-token"

[transcription]
server_url = "https://api.openai.com/v1"
server_token = "openai-api-key"
```

## Running

Build the web app once:

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

Open:

```text
http://127.0.0.1:8765/
```

The backend serves the built frontend from `apps/aflow_app/web/dist`, so a separate frontend server is not needed for normal use. If the frontend has not been built, `/` and SPA routes return a clear 404 telling you to run `npm run build`.

Frontend development:

```bash
cd apps/aflow_app/web
npm install
npm run dev
npm run build
npm run preview
npm test -- --run
```

`npm run dev` starts Vite on `http://localhost:3000` and proxies API requests to `http://127.0.0.1:8765`.

Server tests:

```bash
cd apps/aflow_app/server
uv run --extra dev pytest -q
```

## Authentication

All API endpoints except `/health` require the configured token.

Normal requests use:

```text
Authorization: Bearer <token>
```

The frontend stores the token in browser `localStorage` under `aflow_auth_token`. Logout clears that key.

Execution event streams also accept `?token=<token>` because browser `EventSource` cannot set custom authorization headers.

## Projects

The project catalog merges three sources:

- local git roots discovered under `projects_home`
- Codex thread working directories, when Codex app-server is configured
- persisted overrides in `project_overrides.json`

Project records have stable IDs. Display names, current paths, and aliases are stored in the overrides file.

Important behaviors:

- Linked git worktrees are canonicalized back to their primary checkout when git can identify the common directory.
- Moving a project path keeps the old path as a historical alias.
- Historical aliases are used before current-path matching so old Codex threads remain linked after a project move.
- Codex thread enumeration is optional enrichment. If Codex app-server is unavailable, local projects still list normally.
- The older repo registry file (`repos.json`) is migrated into `project_overrides.json` when the overrides file does not yet exist.

Project detection source values:

- `local_git_root`
- `codex_thread_cwd`
- `local_git_root+codex_thread_cwd`
- `override`

## Plans

The app recognizes plan files in:

- `plans/drafts/*.md`
- `plans/in-progress/*.md`

Draft behavior:

- Saving a draft writes content verbatim to `plans/drafts/<name>.md`.
- Draft names cannot be empty and cannot contain `/` or `\`.
- `.md` is added automatically when omitted.
- Listing drafts returns sorted stems without `.md`.
- Promoting a draft copies the draft content into `plans/in-progress/<target>.md`.
- Promotion does not delete the source draft.
- If no promotion target is supplied, the draft name is reused.
- Existing target files are overwritten by promotion.

Plan listing parses each plan with AFlow's normal plan parser. Invalid plan files are silently omitted from the plan list rather than shown as broken entries.

The frontend shows a `Save plan draft` action on thread turns only when the rendered turn text looks like plan Markdown. The current heuristic requires at least one `# ...` heading and at least one `## ...` heading. Saving from a thread uses an automatic name like `plan-YYYY-MM-DDTHH-MM-SS`.

## Codex Threads

Codex routes are project-scoped under `/api/projects/{project_id}/threads`.

Thread listing queries the selected project's current path and historical aliases, deduplicates by thread id, and returns `backend_status`:

- `ready`
- `not_configured`
- `uninitialized`
- `error`

If listing fails because Codex app-server is unavailable, the API returns an empty thread list plus the backend status instead of failing the whole project view.

Starting, resuming, forking, and sending turns default `cwd` to the selected project's current path unless the request supplies one.

The thread APIs expose Codex options such as model, model provider, service tier, approval policy, reasoning effort, summaries, personality, and extended-history persistence when supported by the connected Codex app-server.

Frontend thread behavior:

- Threads are sorted by `updated_at`, newest first.
- Selecting a project automatically selects the newest matched thread when one exists.
- If the selected thread's `cwd` differs from the project's current path, the UI marks it as stale and offers `Resume here` and `Fork here`. Both actions run the next Codex operation in the project's current path.
- Sending a turn supports Cmd/Ctrl+Enter.
- After sending a turn, the UI polls the thread every second for up to 15 seconds. Terminal statuses are `completed`, `failed`, `cancelled`, `canceled`, and `aborted`.
- If the poll does not observe a terminal status in time, the UI reports a timeout even though the underlying Codex turn may still finish later.

## Executions

Executions are started with a project id and a plan path. The server joins the requested plan path to the project path before preparing startup.

Optional execution fields:

- `workflow_name`
- `team`
- `start_step`
- `max_turns`
- `extra_instructions`

Startup uses the public `aflow` library API. If startup needs an interactive question, the API returns `prepared: false` with the question payload. If startup is ready, the server launches the workflow in a background task and returns an app-level run id.

The app-level run id is an 8-character UUID prefix used for UI tracking and SSE. It is separate from the `.aflow/runs/<run-id>` directory created by the engine itself.

Execution status is stored in memory. Restarting the server loses the app-level status map and event queues, while engine run artifacts remain on disk under the project.

Status note: the current app marks a run as `completed` only when the engine end reason is `done`; other successful engine end reasons can appear as `failed` in app status with the end reason as the error string. Check `.aflow/runs/` for the authoritative engine result when that distinction matters.

Execution events are streamed from `/api/executions/{run_id}/events` as SSE. The server emits `ping` events after 30 seconds of inactivity and closes the stream after run completion or failure.

## Audio Transcription

Audio transcription is optional. When transcription is not configured, text input remains functional and `/api/transcribe` returns `503`.

When configured, the transcription client supports OpenAI-compatible Whisper-style APIs. Uploaded audio is written to a temporary file for transcription and deleted afterward.

The frontend records browser audio as `audio/webm` with `MediaRecorder`. The record button is visible even when the server is not configured for transcription; in that case the upload fails with a user-facing "not configured" message. Successful transcription appends the returned text to the composer input.

Configure with:

```bash
export AFLOW_TRANSCRIPTION_URL="https://api.openai.com/v1"
export AFLOW_TRANSCRIPTION_TOKEN="your-openai-api-key"
```

## API Reference

Health:

- `GET /health` - health check, no auth required.

Projects:

- `GET /api/projects`
- `GET /api/projects/{project_id}`
- `PATCH /api/projects/{project_id}`
- `GET /api/projects/{project_id}/plans`

Codex threads:

- `GET /api/projects/{project_id}/threads`
- `GET /api/projects/{project_id}/threads/{thread_id}`
- `POST /api/projects/{project_id}/threads`
- `POST /api/projects/{project_id}/threads/{thread_id}/resume`
- `POST /api/projects/{project_id}/threads/{thread_id}/fork`
- `PATCH /api/projects/{project_id}/threads/{thread_id}/name`
- `POST /api/projects/{project_id}/threads/{thread_id}/turns`

Plan drafts:

- `GET /api/projects/{project_id}/plans/drafts`
- `POST /api/projects/{project_id}/plans/drafts`
- `GET /api/projects/{project_id}/plans/drafts/{name}`
- `DELETE /api/projects/{project_id}/plans/drafts/{name}`
- `POST /api/projects/{project_id}/plans/promote`
- `GET /api/projects/{project_id}/plans/in-progress`

Executions:

- `POST /api/executions`
- `GET /api/executions/{run_id}`
- `GET /api/executions/{run_id}/events`

Transcription:

- `POST /api/transcribe`

Static frontend:

- `GET /`
- `GET /{path:path}`

Local probe handling:

- `POST /api/plugin/events` returns `204` and is intentionally suppressed from normal access logs. Set `AFLOW_APP_LOG_PLUGIN_PROBES=1` to log one fingerprint per unique probe while debugging.

## Security Notes

- The server requires a bearer token for all API operations except `/health`.
- Do not expose the server to the internet without additional security controls.
- Browser tokens are stored in local storage, so use a dedicated token and trusted browser profile.
- Bind to `127.0.0.1` unless you intentionally need LAN access.
