# Remote App

The remote app is a mobile-first FastAPI/React interface for managing AFlow workflows across local projects. It lives under `apps/aflow_app/` and is separate from the published `aworkflow` wheel.

It is designed for authenticated desktop-hosted local/LAN use, not direct internet exposure.

## Capabilities

- Discover local git projects under a configured projects home.
- Associate provider-qualified planning sessions with projects by current path and historical aliases.
- Discover planning providers and their readiness, models, reasoning controls, and optional capabilities.
- Start, resume, fork, rename, archive, and send turns to planning sessions when the selected provider supports those actions.
- Review pending approvals, interrupt active turns, and stage file or image attachments when supported.
- Save plans from planning-session turns as drafts.
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
│   │   ├── project_catalog.py # project discovery and planning-session association
│   │   ├── project_overrides.py # persistent names, moved paths, aliases
│   │   ├── aflow_service.py   # aflow library integration
│   │   ├── planning_routes.py # provider-neutral planning and plan-draft routes
│   │   ├── planning/          # service, provider registry, models, attachments
│   │   │   └── providers/
│   │   │       └── codex.py   # SDK-backed Codex provider adapter
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
| `AFLOW_PLANNING_PROVIDERS` | `planning.providers` | default Codex provider | JSON provider list; replaces the file provider list when set. |
| `AFLOW_PLANNING_DEFAULT_PROVIDER` | `planning.default_provider_id` | `codex` when present | Provider used when a new-session request omits `provider_id`. |
| `AFLOW_PLANNING_CODEX_URL` | Codex entry in `planning.providers` | - | Preferred environment override for the Codex provider endpoint. |
| `AFLOW_PLANNING_CODEX_TOKEN` | Codex entry in `planning.providers` | - | Preferred environment override for the Codex provider token. |
| `AFLOW_PLANNING_OPERATION_TIMEOUT_SECONDS` | `planning.operation_timeout_seconds` | `30` | Timeout for bounded provider operations. |
| `AFLOW_PLANNING_EXECUTION_POLICY` | `planning.execution_policy` | `full_access` | Server-owned execution policy; currently only `full_access` is valid. |
| `AFLOW_PLANNING_ATTACHMENT_ROOT` | `planning.attachment_root` | `<config_dir>/attachments` | Shared attachment storage root outside project repositories. |
| `AFLOW_PLANNING_ATTACHMENT_MAX_FILE_SIZE_BYTES` | `planning.attachment_max_file_size_bytes` | `26214400` | Maximum size of one attachment. |
| `AFLOW_PLANNING_ATTACHMENT_MAX_COUNT_PER_TURN` | `planning.attachment_max_count_per_turn` | `10` | Maximum attachment references in one turn. |
| `AFLOW_PLANNING_ATTACHMENT_MAX_TOTAL_SIZE_BYTES_PER_TURN` | `planning.attachment_max_total_size_bytes_per_turn` | `52428800` | Maximum total attachment bytes referenced by one turn. |
| `AFLOW_CODEX_APP_SERVER_URL`, `AFLOW_CODEX_URL` | `codex_app_server.server_url`, `codex.url` | - | Legacy compatibility inputs for the default Codex entry. |
| `AFLOW_CODEX_APP_SERVER_TOKEN`, `AFLOW_CODEX_TOKEN` | `codex_app_server.server_token`, `codex.token` | - | Legacy compatibility inputs for the default Codex entry. |
| `AFLOW_TRANSCRIPTION_URL` | `transcription.server_url` | - | Optional transcription service URL. |
| `AFLOW_TRANSCRIPTION_TOKEN` | `transcription.server_token` | - | Optional transcription service token. |

Provider ids must be unique path-safe slugs, and the default must name an enabled provider. New planning environment values override new planning file values; provider-neutral configuration takes precedence over the legacy Codex compatibility reads. Attachment limits must be positive. Attachment storage is rejected if it overlaps an authorized project repository.

Example:

```toml
[server]
bind_host = "127.0.0.1"
bind_port = 8765
auth_token = "your-secret-token"

[project_catalog]
projects_home = "~/code"
project_overrides_path = "~/.config/aflow/project_overrides.json"

[planning]
default_provider_id = "codex"
attachment_root = "~/.config/aflow/attachments"
operation_timeout_seconds = 30
execution_policy = "full_access"

[[planning.providers]]
id = "codex"
kind = "codex"
display_name = "Codex"
server_url = "ws://localhost:8080"
server_token = "provider-token"

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
- working directories reported by configured planning providers
- persisted overrides in `project_overrides.json`

Project records have stable IDs. Display names, current paths, and aliases are stored in the overrides file.

Important behaviors:

- Linked git worktrees are canonicalized back to their primary checkout when git can identify the common directory.
- Moving a project path keeps the old path as a historical alias.
- Historical aliases are used before current-path matching so existing planning sessions remain linked after a project move.
- Planning-session enumeration is failure-isolated. An unavailable provider does not hide local projects or sessions from healthy providers.
- Project paths are server-authoritative. Starting a session uses the selected project's current path; clients cannot choose an arbitrary working directory. Resume and fork operations also move provider activity to that current path after ownership is checked against the current path and historical aliases.
- The older repo registry file (`repos.json`) is migrated into `project_overrides.json` when the overrides file does not yet exist.

Project detection source values:

- `local_git_root`
- `planning_session_cwd`
- `local_git_root+planning_session_cwd`
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

The frontend shows a `Save plan draft` action on session turns only when the rendered turn text looks like plan Markdown. The current heuristic requires at least one `# ...` heading and at least one `## ...` heading. Saving from a session uses an automatic name like `plan-YYYY-MM-DDTHH-MM-SS`.

Draft storage and workflow execution remain app-owned project features. They are not owned by, or stored in, a planning provider.

## Planning Sessions

Planning is exposed through a provider-neutral API. Each session is identified by the pair `provider_id` and `provider_session_id`; provider-local ids are never treated as globally unique. Provider discovery reports readiness plus capabilities such as models, reasoning levels and summaries, fork, archive, approvals, interruption, output schemas, and supported attachment kinds. A failed or disabled provider reports bounded status without suppressing healthy providers.

Session collection routes are project-scoped. The server lists provider sessions and keeps only those whose reported working directory belongs to the project's current path or a historical alias. Active and archived sessions are separate views, sorted by `updated_at` with provider-qualified identity as the deterministic tie-breaker. Existing sessions therefore remain discoverable across project moves and across devices.

Starting a session accepts an optional provider id, model, and provider-advertised reasoning level. The configured default provider is used when no provider is supplied. The server supplies the project's current path; session and turn requests do not accept path or execution-policy overrides. Resume and fork similarly use the server-authorized current project path.

Frontend controls are derived from the selected provider's advertised capabilities:

- model and reasoning selectors show provider-advertised values while preserving a historical session model for display;
- active and archived list modes make both Archive and Unarchive reachable;
- pending command or file-change approvals can be accepted, declined, or cancelled;
- an active turn can be interrupted when the provider advertises interruption;
- file upload controls appear only for advertised `file` or `image` kinds, and failed uploads are not silently submitted with a turn.

Attachments are multipart uploads staged in the shared aflow-managed attachment store and scoped to the exact project/provider/session namespace. A turn references uploaded `attachment_id` values; cross-session, missing, duplicate, deleted, over-count, and over-size references are rejected before provider work starts. Attachments survive archive/unarchive, explicit deletion is blocked while an attachment is leased by an in-flight turn, and the client may list or delete staged items.

Codex is the first concrete provider. Its adapter uses `codex-app-server-sdk` for session lifecycle, turns, models, approvals, interruption, and archive operations. Because the SDK turn helper is text-first, Codex file and image attachments are not presented as native SDK transport: the adapter appends a deterministic, id-sorted manifest containing the staged paths and untrusted metadata to the user's unchanged text.

Sending a turn supports Cmd/Ctrl+Enter. The UI polls the selected session while work is active, refreshes approvals when supported, and treats `completed`, `failed`, and `interrupted` turns as terminal.

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

Planning providers:

- `GET /api/planning/providers`
- `GET /api/planning/providers/{provider_id}/models`
- `GET /api/planning/providers/{provider_id}/reasoning-options`

Planning sessions:

- `GET /api/projects/{project_id}/planning/sessions?archived={boolean}`
- `POST /api/projects/{project_id}/planning/sessions`
- `GET /api/projects/{project_id}/planning/providers/{provider_id}/sessions/{provider_session_id}`
- `POST /api/projects/{project_id}/planning/providers/{provider_id}/sessions/{provider_session_id}/resume`
- `POST /api/projects/{project_id}/planning/providers/{provider_id}/sessions/{provider_session_id}/fork`
- `PATCH /api/projects/{project_id}/planning/providers/{provider_id}/sessions/{provider_session_id}`
- `POST /api/projects/{project_id}/planning/providers/{provider_id}/sessions/{provider_session_id}/archive`
- `POST /api/projects/{project_id}/planning/providers/{provider_id}/sessions/{provider_session_id}/unarchive`
- `POST /api/projects/{project_id}/planning/providers/{provider_id}/sessions/{provider_session_id}/turns`
- `POST /api/projects/{project_id}/planning/providers/{provider_id}/sessions/{provider_session_id}/turns/{turn_id}/interrupt`
- `GET /api/projects/{project_id}/planning/providers/{provider_id}/sessions/{provider_session_id}/approvals`
- `POST /api/projects/{project_id}/planning/providers/{provider_id}/sessions/{provider_session_id}/approvals/{approval_id}`
- `GET /api/projects/{project_id}/planning/providers/{provider_id}/sessions/{provider_session_id}/attachments`
- `POST /api/projects/{project_id}/planning/providers/{provider_id}/sessions/{provider_session_id}/attachments`
- `DELETE /api/projects/{project_id}/planning/providers/{provider_id}/sessions/{provider_session_id}/attachments/{attachment_id}`

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
