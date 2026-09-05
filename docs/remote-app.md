# Remote workflow control app

The optional app in `apps/aflow_app/` controls registered AFlow projects through authenticated REST, MCP, and a compact web client. It manages project registration, the two canonical configuration documents, Markdown plans, and durable workflow runs. The app is a separate subproject and is not included in the `aworkflow` wheel.

Agent providers are selected by normal AFlow harness profiles when a workflow runs. Codex remains available as one optional engine harness adapter; the remote server has no provider-specific client or interactive chat surface.

## Boundaries

- Every project is an exact Git root explicitly recorded in the versioned project registry.
- Project paths are selected server-side from that registry. Requests cannot submit arbitrary roots.
- Plan routes edit only direct regular `.md` files under `plans/todo`, `plans/in-progress`, and `plans/done`.
- Configuration routes address only `.aflow/config/aflow.toml` and `.aflow/config/workflows.toml` as one revisioned pair.
- Run REST and MCP routes delegate to the same durable control-plane service.
- All API and MCP operations require an `Authorization: Bearer ...` header. Credentials in URLs or MCP payloads are rejected.
- `/health` reports process liveness. Authenticated `/ready` reports control-plane readiness.

## Run locally

```bash
npm --prefix apps/aflow_app/web install
npm --prefix apps/aflow_app/web run build
uv sync --project apps/aflow_app/server
AFLOW_APP_TOKEN=secret uv run --project apps/aflow_app/server aflow-app-server
```

The server binds to `127.0.0.1:8765` by default and serves the built web client from the same origin.

## Configuration

The server reads `~/.config/aflow/config.toml` and these environment overrides:

- `AFLOW_APP_TOKEN` or `AFLOW_APP_TOKEN_FILE`
- `AFLOW_APP_HOST` and `AFLOW_APP_PORT`
- `AFLOW_MANAGED_PROJECTS_ROOT`
- `AFLOW_PROJECT_REGISTRY_PATH`
- `AFLOW_CONFIG_AUDIT_PATH`
- `AFLOW_EXECUTABLE`
- `AFLOW_RELEASE_IDENTITY`
- `AFLOW_ENVIRONMENT_FILE`

A token file is reread per request so rotation does not require a restart. The managed root, registry, executable, release identity, and environment file are server-owned launch inputs. Per-project workflow settings live in the canonical project configuration pair.

```toml
[server]
bind_host = "127.0.0.1"
bind_port = 8765
auth_token_file = "/etc/aflowd/token"

[control_plane]
managed_projects_root = "/srv/code"
project_registry_path = "/var/lib/aflowd/projects.json"
config_audit_path = "/var/lib/aflowd/config-audit.jsonl"
aflow_executable = "/opt/aflowd/current/bin/aflow"
release_identity = "reviewed-commit"
environment_file = "/etc/aflowd/worker.env"
```

## Project and configuration API

- `GET /api/projects`
- `POST /api/projects`
- `GET /api/projects/{project_id}`
- `PATCH /api/projects/{project_id}`
- `DELETE /api/projects/{project_id}`
- `GET /api/projects/{project_id}/config`
- `PUT /api/projects/{project_id}/config`
- `POST /api/projects/{project_id}/config/validate`

Project creation and registration accept only normalized paths relative to the managed root. Unregister removes the registry record after active-unit checks and never deletes repository files.

Configuration reads return both exact texts, a combined SHA-256 revision, and bounded validation results. Saves require `expected_revision`, validate both candidates through the production loader, and atomically commit or restore the pair. New launches and configuration commits use the same per-project lock.

## Plan API

- `GET /api/projects/{project_id}/plans`
- `POST /api/projects/{project_id}/plans`
- `GET /api/projects/{project_id}/plans/{status}/{name}`
- `PUT /api/projects/{project_id}/plans/{status}/{name}`
- `POST /api/projects/{project_id}/plans/{status}/{name}/promote`

`status` is `todo`, `in_progress`, or `done`; filesystem directories remain `todo`, `in-progress`, and `done`. Creation starts in `todo`. Promotion follows `todo → in_progress → done`. Updates and promotion require the current SHA-256 revision. Each file is UTF-8 Markdown at most 256 KiB. Unsafe names, nesting, links, non-regular files, stale revisions, and occupied move targets are rejected without changing the source bytes.

The web client lists all three states, creates and edits plans with revisions, promotes them through the lifecycle, and opens in-progress plans in the run dashboard.

## Run control

The durable control-plane surface remains under `/api/control-plane`. It provides capabilities, plans, runs, event snapshots and streams, bounded context, startup answers, compare-and-swap controls, owner stop, and resume. State-changing retries use the `Idempotency-Key` header.

A start request accepts typed plan_path, workflow_name, team, start_step,
max_turns, bounded extra_instructions, and restarted_from_run_id. Unknown fields
fail validation. Capabilities expose the ordered declared/executable/excluded
workflow steps and admitted selectors so a client can construct valid choices.
A restart predecessor must first be stopped through owner stop and have no
active exact unit; a successful request creates a new run and exposes immutable
predecessor lineage. Resume continues the saved invocation and does not accept
replacement launch choices.

The `/mcp` streamable HTTP endpoint exposes the same canonical operations and bearer policy. The lightweight `aflow daemon` exposes the shared MCP contract without the web app.

## Development checks

```bash
uv run --project apps/aflow_app/server pytest -q
npm --prefix apps/aflow_app/web test -- --run
npm --prefix apps/aflow_app/web run build
uv run ruff check apps/aflow_app/server/src apps/aflow_app/server/tests
```
