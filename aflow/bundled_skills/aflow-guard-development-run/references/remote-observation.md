# Read-only remote observation

Use this reference for a run owned by either AFlow daemon. Capability discovery
is authoritative and occurs once during guard setup.

## Ownership surfaces

### Lightweight local daemon

- Start forms are `aflow daemon start --foreground` for stdio MCP and
  `aflow daemon start --mcp-transport http --mcp-port 8765` for loopback
  HTTP MCP.
- Status is `aflow daemon status --repo-root <repo>`.
- It owns one repository and reports only workers it directly owns.
- Stdio must remain attached. EOF stops the daemon and drains its workers.
- HTTP binds to `127.0.0.1` and may detach.
- It has no REST API, web UI, or systemd ownership. Never invent UI URLs.

Observe an existing instance only. Never start a temporary stdio daemon for a
tick and never close a daemon-owned stdio session.

### Remote aflowd

Remote `aflowd` exposes authenticated REST, React UI, SSE, and MCP, and owns
workflow units named `aflow-run-<run-id>.service`. Use only endpoints supplied
by the user, the deployment configuration, or capability discovery. The stable
product paths are `/ready`, `/mcp`, and
`/api/control-plane/projects/{project_id}/runs/{run_id}/events/stream`, but no
host, scheme, or port is a product default. Never guess an endpoint. Bearer
tokens remain in authorization headers and must never appear in URLs, prompts,
logs, or reports.

## MCP version 1 contract

The shared server is named `AFlow Control Plane`. Permit only these eight read
tools:

- `get_capabilities`
- `list_projects`
- `get_project_capabilities`
- `list_plans`
- `list_runs`
- `get_run`
- `get_run_events`
- `get_run_context` with Lite context only for a new anomaly

Never call these five writes:

- `start_run`
- `answer_startup`
- `control_run`
- `owner_stop`
- `resume_run`

Page limits are 1 through 1000 with a default of 100. Keep reads below the
default unless one exact anomaly requires otherwise.

## Tick policy

1. Pin project ID, run ID, MCP version, endpoint, caller scope, ownership mode,
   and advertised read operations. Never print or persist bearer values.
2. At setup, call `get_capabilities` once and reject missing or conflicting
   ownership/capability data.
3. On each healthy tick, perform only one exact `get_run` request.
4. When status or revision changes, reserve the next tick for one
   cursor-bounded `get_run_events` request. Do not combine both reads into
   every healthy tick.
5. Compare remote identity, canonical status, revision, and ownership with
   durable state or the ownership-matched local status.
6. Canonical `running` plus the matching owner is healthy.
   `needs_attention`, legacy/interrupted daemon ownership, missing owner, or
   disagreement is report-and-pause.
7. Report a web UI base URL or run URL only when AFlow advertises or
   deterministically returns it.
8. Treat unavailable or immature remote behavior as evidence. Never switch
   transport, search broadly for controllers, or mutate durable state.
