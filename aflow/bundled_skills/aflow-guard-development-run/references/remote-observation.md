# Read-only remote observation

Use this reference for a run owned by an AFlow UI/server deployment. Capability
discovery is authoritative and occurs once during guard setup.

## Ownership surfaces

### UI server

- Start forms are `aflow ui` and `aflow ui --daemon`; observe an existing UI
  server rather than starting a second owner.
- Use the advertised URL plus `/mcp` or `/mcp/` with the configured bearer
  token in the `Authorization` header. Browser cookies are not accepted.
- It owns the server process record while persistent workflow units remain
  independently owned and observable after UI shutdown.
- Never guess a host, scheme, or port, and never stop the UI or a workflow from
  an observation tick.

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

The shared server is named `AFlow Control Plane`. Permit only these nine read
tools:

- `get_capabilities`
- `list_projects`
- `get_project_capabilities`
- `list_plans`
- `list_runs`
- `get_run`
- `get_run_events`
- `get_run_context` with Lite context only for a new anomaly
- `preflight_run` with bounded dirty-worktree pages only when needed

Never call these five writes during ordinary observation:

- `start_run`
- `answer_startup`
- `control_run`
- `owner_stop`
- `resume_run`

The only write exception is the explicitly authorized neutral startup case in
the parent guard skill. Its matched receipt must be claimed before launch and
must select this same advertised server surface. It may issue exactly one
`start_run` request and, when that normal path returns a startup question,
exactly one matching `answer_startup` request. The request must keep the
original plan, workflow, team, start step, turn budget, extra instructions,
branch/base derivation, and predecessor key as lineage. The `Idempotency-Key`
header for the one replacement request is the durable
`replacement_idempotency_key` emitted by the claim; do not reuse the
predecessor key and do not generate another replacement key. Never use
`control_run`, `owner_stop`, or `resume_run`; never create a CLI/tmux
controller for a `ui-server` or `aflowd` run. An uncertain response is
report-and-pause unless that same replacement key later exposes the already
acknowledged launch.

This exception does not change the tick contract: healthy ticks still call only
`get_run`, and all other remote observation remains read-only.

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
