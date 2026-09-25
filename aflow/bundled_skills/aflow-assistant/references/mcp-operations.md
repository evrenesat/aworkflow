# Interactive AFlow MCP operations

Use this reference to operate AFlow from an interactive agent. Discover the
connected server's tool schemas before making calls: older deployments may
expose fewer tools or different optional fields. Examples below describe
arguments, not a particular agent's tool-name prefix.

## Connection and discovery

The supported server is `aflow ui`, which serves the dashboard and authenticated
HTTP MCP at the same configured address plus `/mcp` or `/mcp/`. There is no
separate MCP port or standalone daemon to start. `aflow ui --status` inspects
local server ownership; `aflow ui --daemon` starts it when startup is authorized.
Do not restart a shared service just to diagnose a missing client connection.

Configure the MCP client's HTTP URL and an environment-backed bearer token.
The token must match the UI server's `[server] auth_token` or `auth_token_file`.
Authentication uses the `Authorization: Bearer …` header; browser login cookies
do not authenticate MCP. Never put credentials in URLs, tool arguments, plans,
or transcript output. Use the client's own supported configuration schema;
do not assume all clients accept the same TOML settings. In a source checkout,
`apps/aflow_app/server/aflow-control-plane.mcp.example.toml` is a secret-free
client example, not a place to store a real token.

Discover tools, then use `list_projects` and `get_project_capabilities` for the
exact project. `get_capabilities` summarizes every registered project. Use only
returned project IDs and plan identities; never infer them from display names
or supply arbitrary filesystem roots. Missing projects need registration via
the existing browser flow, not a fabricated MCP registration call. Skill
installation also remains a separate operation.

The UI registry includes lifecycle, authoring, and scheduling tools plus
read-only resource templates. A lifecycle-only registry does not promise
authoring or scheduling support. The live schemas and capabilities decide
what is available; an installed reference is not evidence that a tool is
connected.

## Read tools

- `get_capabilities()` and `list_projects()` discover the managed project set.
- `get_project_capabilities(project_id)` reads one project's capabilities.
- `list_plans(project_id, limit=100, cursor=None)` returns lifecycle plan
  metadata for launch selection. It does not return editable Markdown content.
- `list_runs(project_id, limit=100, cursor=None)` returns a versioned run page.
- `get_run(project_id, run_id)` reads canonical run state, revisions, and
  available status evidence. Retain the exact project/run pair.
- `get_run_events(project_id, run_id, after_sequence=None, limit=100)` reads
  ordered events. Advance `after_sequence` from received events; avoid rereading
  the entire history. Limits are bounded, at most 1000 for these lifecycle reads.
- `get_run_context(project_id, run_id, level="lite", full_scope=false)` reads
  bounded context. Start with lite; full requires `level="full"` and
  `full_scope=true`, and should be used only when the task needs that scope.
- `preflight_run(project_id, plan_path, ...)` checks launch admission and
  dirtiness without allocating a run. It accepts launch options plus `offset`
  and `limit` for dirty-path pages. Inspect all relevant pages, and repeat when
  the plan, launch options, or repository state changes. Preflight does not
  reserve a run or eliminate startup questions.
- `get_global_config()` reads the revisioned `aflow.toml`/`workflows.toml`
  pair and its configuration view. These settings are shared across registered
  projects; read them to select existing workflows and teams.
- `get_project_scheduling(project_id)` reads the revisioned automatic-consumption
  toggle and concurrent-implementation limit (defaults: enabled and two).
  `get_project_queue(project_id)` reads ready, held, claimed, and capacity
  evidence. Consult it after promotion and before a manual start.
- `list_plan_documents(project_id, plan_status=None)` lists editable document
  identities and revisions. `read_plan(project_id, plan_status, name)` reads
  the Markdown. Status values are `todo`, `in_progress`, `done`, `failed`, and
  `needs_plan_change`, stored under the matching five `plans/` directories
  (`in_progress` uses `in-progress`; `needs_plan_change` uses
  `needs-plan-change`).

Resource templates provide equivalent read-only views:

```text
aflow://projects/{project_id}/capabilities
aflow://projects/{project_id}/runs/{run_id}
aflow://projects/{project_id}/runs/{run_id}/context/lite
```

Use resource URIs only through the client's actual resource interface when
available. Reads never authorize a subsequent mutation.

## Author a plan and select configuration

1. Inspect the project and requested outcome. Use `get_global_config` to read
   existing workflow/team choices; select their stable IDs. A display label is
   not a team ID. Inspect actual lifecycle setup/teardown and publication
   settings rather than assuming a workflow name guarantees delivery.
2. For a new plan, call `create_plan(project_id, name, content)` with a simple
   Markdown filename. Omitted/null content uses a draft template that still
   needs completion. Names cannot contain directories or traversal; documents
   must be direct regular `.md` files, at most 256 KiB of UTF-8 content.
3. For a failed or `needs_attention` run, inspect its exact `get_run` and lite
   context before `create_plan_from_run(project_id, run_id, name=None)`. This
   creates an editable todo draft from bounded evidence. It does not diagnose
   the root cause, implement a fix, resume the run, or close an issue for you.
4. Read the draft. Complete scope, invariants, checkpoint tasks, exact
   verification commands and expected behavior. Use `aflow-plan` if installed
   when authoring a checkpoint handoff; retain the user's requested scope.
5. Call `update_plan(project_id, plan_status, name, content,
   expected_revision)` with the revision from the latest read. Read back the
   result and inspect the actual checkpoint content before launch.
6. Use `promote_plan(project_id, plan_status, name, expected_revision,
   target_name=None)` to move to the next lifecycle stage. It has no arbitrary
   destination-status argument. Promotion from todo makes the plan ready in
   `in_progress`; the default-on consumer may start it immediately. Inspect the
   queue's claim/run before a manual start. Promotion is not proof of review or
   delivery. Do not move an
   active controller's source plan to done manually.
7. Resolve the resulting path from returned document/lifecycle metadata using
   `list_plan_documents` and `list_plans`; retain the returned name after any
   rename. Inspect `get_project_queue` and existing runs first; preflight the
   exact ready plan before starting it if no claim exists. Corrected inactive
   failed or `needs_plan_change` plans use `requeue_plan` with their latest
   revision and a retained idempotency key; they are never retried implicitly.

Only change global settings when the requested work requires that change and
authorization covers its shared effect. Prefer per-run workflow/team/max-turn
arguments for a one-run choice. `patch_global_config` takes one nested
`payload`, containing `expected_revision` and either an ordered `actions` list
or a `documents` map keyed by `aflow.toml` and/or `workflows.toml`, never both.
Discover the typed actions instead of guessing their fields. For example, an
authorized global default change can use this argument shape:

```json
{
  "payload": {
    "expected_revision": "<revision from get_global_config>",
    "actions": [{"type": "set_max_turns", "value": 20}]
  }
}
```

The service validates and commits the pair atomically. Existing runs observe
current settings at applicable safe boundaries or resume. A launch snapshot
is diagnostic provenance, not a frozen execution configuration.

## Start and answer startup questions

Check `list_runs` for an existing active run of the selected plan before
dispatch. Each logical run has one controller; do not start a CLI controller
alongside a server-managed run. Preflight with the intended options, then call:

```text
start_run(project_id, plan_path, idempotency_key,
          workflow_name=None, team=None, start_step=None, max_turns=None,
          extra_instructions=None, restarted_from_run_id=None,
          dirty_worktree_confirmed=false)
```

Use one unique idempotency key for this logical start and retain its exact
arguments. Omit optional fields unless chosen deliberately. Never invent
`restarted_from_run_id`, mark dirty state confirmed without authorization, or
guess a reviewer/start step to bypass admission. Extra instructions accept at
most eight non-empty strings, 512 characters each, 4096 total, with no NULs.
They are bounded run-wide guidance; substantial specifications belong in the
plan. Check these constraints before allocation.

The result may reserve a run and return a startup question instead of starting
a worker (`awaiting_startup_answer`). Retain its run and question IDs. Resolve
the actual question using existing user authorization/preferences, or ask when
a material choice is missing. Submit its expected answer type through
`answer_startup(project_id, question_id, answer, idempotency_key)` using a new
key for that answer. More questions may follow. Do not call `start_run` again
with a new key to escape a pending question. Report reservation separately
from observed execution.

## Monitor, control, stop, and resume

Monitor the exact run using canonical state, incremental events, and lite
context. Use event-driven notifications where available or bounded checks at
an interval suited to the task. Report meaningful progress, failure, required
input, or completion. Avoid repeatedly dumping unchanged transcripts. Client
disconnects and UI shutdown do not imply the workflow stopped: detached
workers have their own ownership and receipts. Verify state after reconnecting.

`control_run(project_id, run_id, expected_revision, idempotency_key, ...)`
supports `max_turns`, `team`, `role_selectors`, `unsafe_changes`, and
`owner_stop`. Read the current integer run revision before controlling it.
Use only controls supported by capabilities/current schema; do not use
`unsafe_changes` as a generic configuration escape hatch. Team or role
changes follow the engine's safe-boundary/hotplug semantics, so verify the
resulting state rather than promising an immediate mid-turn change.

Two distinct stop operations:

- `control_run(..., owner_stop=true)` requests stopping after the current
  worker/reviewer call reaches the existing boundary. It does not guarantee
  completion of the whole checkpoint.
- `owner_stop(project_id, run_id, expected_revision, idempotency_key)`
  immediately interrupts the exact active unit and terminally stops the run.
  Use when immediate interruption is intended and authorized.

Neither operation approves a checkpoint. Clarify an ambiguous stop request
when the timing would materially affect ongoing work.

For recovery, inspect the failure, plan, current configuration, and admission
evidence first. `resume_run(project_id, run_id, idempotency_key,
extra_instructions=None, recovery=None)` creates a new lineage-linked successor
run. Track the returned successor ID; the predecessor remains unchanged.
Omitted/null extra instructions inherit, a list replaces, and `[]` clears them.
Ordinary resume omits `recovery`. When explicitly choosing another configured
worker and supported by the live schema, durable-evidence recovery uses:

```json
{"mode": "durable_evidence", "worker_selector": "<configured selector>"}
```

The source must be confirmed inactive. The replacement gets bounded durable
evidence in a fresh provider session, not hidden context from the old session.
Do not fabricate inactivity/owner-stop receipts or edit `.aflow` files to pass
admission. A rejected recovery requires resolving the stated condition, not
launching competing controllers or repeatedly trying different identities.
Legacy runs lacking managed ownership may be read-only; follow their actual
capabilities rather than converting them by hand.

## Conflicts, timeouts, and bounded retries

Lifecycle writes use idempotency keys. Retry an uncertain transport result
with the same key and identical arguments; changing the payload with the same
key yields `idempotency_conflict`. Read back the exact run/question when
possible. Do not allocate a new key merely because the first response was lost.

Plan/global-config authoring writes are non-idempotent and have no lifecycle
idempotency-key argument. After a lost response, read the exact document,
source/target lifecycle location, or config pair to determine whether the
write succeeded before retrying. Do not create duplicate follow-up drafts.

On `revision_conflict`, reread and reconcile the current content/control state
with the intended change. Document/config revisions are opaque strings; run
control revisions are integers. Never increment a revision locally or blindly
replace another editor's changes. A reconciled lifecycle control is a new
logical request with a new key, not a replay with a changed payload.

Bound transient retries. Stop and report unresolved state if read-back cannot
establish the outcome. `project_not_found`, `run_not_found`, `plan_not_found`,
`plan_exists`, `invalid_plan`, `invalid_extra_instructions`,
`restart_required`, `operation_forbidden`, and `operation_rejected` require
interpreting the condition, not endless retries. Authentication failures need
client/server credential configuration; unavailable authoring tools need
capability/version inspection. Generic internal errors are intentionally
opaque: collect sanitized evidence instead of requesting secrets in chat.

## Completion evidence

Report implementation/review, merge, remote publication, CI, and live
activation separately when those stages are in scope. Checked boxes, a done
folder, a successful transport call, or reaching a turn limit do not prove
delivered behavior. Preserve the active plan, managed worktrees, and successor
lineage until the controller's configured lifecycle completes. Follow the
project's publication rules; the skill itself grants no publication authority.

## Source lookup when available

- `aflow/mcp_control_plane.py`: lifecycle schemas, resources, bounds, annotations.
- `apps/aflow_app/server/src/aflow_app_server/mcp_adapter.py`: UI composition.
- `mcp_plan_authoring.py`, `plan_service.py` in that server directory: plan tools.
- `mcp_config_authoring.py`, `models.py`: global config payload and typed actions.
- `apps/aflow_app/server/tests/test_mcp.py`: discovery, authored launch,
  cross-transport revision checks, startup, stop/resume, and recovery behavior.

These are optional source pointers. The normal connected-agent workflow uses
live discovery and tool results and does not require the source checkout.
