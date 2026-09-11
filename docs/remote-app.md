# Remote workflow control app

The optional app in `apps/aflow_app/` controls registered AFlow projects through authenticated REST, MCP, and a compact web client. It manages project registration, the two canonical configuration documents, Markdown plans, and durable workflow runs. The app is a separate subproject and is not included in the `aworkflow` wheel.

Agent providers are selected by normal AFlow harness profiles when a workflow runs. Codex remains available as one optional engine harness adapter; the remote server has no provider-specific client or interactive chat surface.

## Boundaries

- Every project is an exact Git root explicitly recorded in the versioned project registry.
- Project paths are selected server-side from that registry. Requests cannot submit arbitrary roots.
- Plan routes edit only direct regular `.md` files under `plans/todo`, `plans/in-progress`, and `plans/done`.
- Configuration routes address the one shared global pair `~/.config/aflow/aflow.toml` and `workflows.toml` as one revisioned pair; project-local configuration is never written or read.
- Run REST and MCP routes delegate to the same durable control-plane service.
- REST accepts an `Authorization: Bearer ...` header or, for the web client, a signed HttpOnly session cookie whose Secure attribute follows the effective request scheme, so direct HTTP from other devices works. Credentials in URLs or MCP payloads are rejected; `/mcp` stays header-only.
- `/health` reports process liveness. Authenticated `/ready` reports control-plane readiness.

## Run locally

```bash
npm --prefix apps/aflow_app/web install
npm --prefix apps/aflow_app/web run build
uv sync --project apps/aflow_app/server
AFLOW_APP_TOKEN=secret uv run --project apps/aflow_app/server aflow-app-server
```

The server binds to `127.0.0.1:8765` by default and serves the built web client from the same origin. `aflow ui` is the end-user launcher for this server; it resolves the global configuration automatically and needs no environment ceremony. The legacy `aflow-app-server` entry point keeps the environment-variable deployment path below.

## Web workspace

The web client is a same-origin workspace for exactly one selected registered project at a time. Projects selects the workspace; a project row (name and path) opens it. Runs and Plans appear only after project selection; global Settings remains available without a project. The upper-left header reads `AFlow · <selected project>` when a project is selected and `AFlow` otherwise; Projects remains the way to switch. Any project-readiness guidance appears inside the affected page (Plans, Runs, New run, or Settings), never as a separate project bar.

Login sends the deployment bearer once in the `Authorization` header to `POST /api/session`; the server answers with a signed HttpOnly, Secure, SameSite=Strict cookie. Reloads and new same-origin tabs restore the session from that cookie (`GET /api/session`) without re-entering the token. The session rolls forward for 30 days from real visible dashboard activity (the client marks such requests with `X-AFlow-Activity: 1`, at most once per minute); background polling and the event stream never renew it. Expiry, an invalid cookie, or a server token rotation ends the session and the client shows sign-in again while preserving the current project and view. Logout (`DELETE /api/session`) expires the cookie immediately — use it on a shared browser. The bearer token is never stored in localStorage, sessionStorage, URLs, or readable cookies; it lives only in the login request. Header-only REST and MCP clients are unaffected.

Projects view: shows added projects and available Git projects beneath the server's managed root. Type a name or relative path to filter both lists. Discovery examines two directory levels with bounded results; it does not search your Mac or automatically register anything. Add registers an existing exact Git root without modifying files or history. Blocked candidates explain what needs fixing; truncated or failed scans offer guidance and retry. The explicit create/register form remains available, including discovered path suggestions. Existing directory names need not change: internal registry IDs are independent of display names. Unregister removes only the registry record after confirmation and preserves files, Git history and plans.

Settings view: guided widgets are the default. A project with no configuration offers Build starter draft. Set up profiles, assign roles, choose workflow/team defaults and review validation; changes stay in a draft until Save both files or Save and continue to Plans succeeds. Available choices come from the current configuration; bundled model/profile suggestions are labeled suggestions. Searchable controls support Arrow keys, Enter and Escape. Custom values are accepted only where the field says so. ZCode model and effort remain configured in ZCode, with an external-configuration label rather than an invented model list.

Advanced TOML exposes the same two documents, not a second configuration source. Use it for uncommon workflow graphs and prompts. Validate checks without saving. Both save actions commit the pair with the current revision. Errors and stale-revision conflicts preserve the draft; reload requires explicit discard. Unsaved navigation is guarded, and a successful ready save can continue to Plans.

Plans view: Draft, Ready and Done correspond to plans/todo, plans/in-progress and plans/done. Create and edit Markdown, Save, then Move to Ready when it is ready to run. Run this plan opens Runs with that exact Ready plan selected. Draft and Done plans cannot launch. Failed saves preserve text, and promotion never silently overwrites a destination. The first supported promotion preserves the exact draft as the known Ready baseline; a read-only Backup history route exposes bounded provenance summaries without restoring or deleting evidence.

Runs shows history and selected-run details, with a prominent New run action opening a separate project-scoped page (`view=new-run`). Cancel returns to Runs; successful creation opens the exact returned run. Creation and startup-answer request identities survive navigation. The newest returned run is selected only when no run is already selected or requested. Plan, workflow, team and start-step widgets show available choices. The launch preview uses the saved configuration and resolves each step's role and profile, including team overrides. Missing choices and invalid turn limits explain why Start run is unavailable. Run settings distinguish pending changes from applied values; low-level unit, revision, ownership and raw context/event information is under Diagnostics.

Project run list and selected-run overview:

- Each run row shows its ID, status, workflow, current step, skipped-step count, and successor marker for runs that record `restarted_from_run_id`.
- The overview shows status/reason, plan, workflow/team, `restarted_from_run_id` plus any successors visible in the run list (lineage), start time with live elapsed time for nonterminal runs, current step with `selected_start_step` and `skipped_steps`, the workflow's `excluded_steps` from capabilities, checkpoint name/index/count from the bounded plan state, turn/max, unit name with launch phase and reconciliation state, and recent bounded events.
- A bounded-outcomes section summarizes the latest manager decision, the last finalized harness turn (step, status, exit code, bounded result), and the current live overrides (`max_turns`, `team`, role selectors) read from the latest `control_changed` journal event.

Live event stream (SSE):

- The last durable run snapshot and timeline stay visible whenever the stream is disconnected, and connection state is displayed separately from run state. Events replay from the last seen sequence and duplicates are dropped. A successful reconnect triggers one canonical status refresh; a failed connection never implies completed, stopped, or any other run transition, and an explicit refresh button always re-reads the canonical status.

Start form:

- Typed fields only: plan, workflow, team, start step, max turns, and optional extra instructions (one bounded line per instruction, at most 8 lines of 512 characters). There is no raw argv or shell input anywhere.
- Workflow and team selectors show the resolved effective name with a small Default indicator before focus (for example `checkpoint_delivery · Default`); focusing switches to a blank search query, blur/Escape restore the resolved label, and the open list offers a real-text "Use default …" row. An explicit selection is submitted verbatim; following a default stays an omission in the request, so the server applies the global default workflow / workflow default team.
- A concise Team members table resolves each role relevant to the workflow (team override first, then the global fallback) with selector, model/effort, and source; other configured roles stay in a disclosure. The Worker upgrade chain lists every `upgrade_to` stage in order with each stage's own worker selector/model/effort and an expandable member view; the last stage is marked, `backup_team` failure recovery never appears as a stage, and malformed graphs surface actionable configuration errors.
- Start-step choices come from the selected workflow's capability-admitted executable steps, labeled with their 1-based index; the form explains exactly which earlier executable steps will be recorded as skipped. The server accepts the step name or a 1-based numeric string and stores the canonical name.
- The server answers a start with either a run or a startup question. All three question kinds are handled in the UI: `pick_step` (choose one of the offered steps) and the `confirm_recovery` / `confirm_worktree_dirty` confirmations (explicit confirm/decline sent as booleans). No run is displayed as started while a question is open.
- Exact retries reuse the same idempotency key; changing the draft replaces the key. Every failure keeps the draft in the form.

Safe live controls:

- Max turns, team, and per-role agent selectors are offered only where capabilities admit them as safe; team and selector values are selects limited to capability-admitted values, never free text.
- Applications send the expected revision and an idempotency key. The UI reports a control as recorded only after the returned canonical revision, explains that the engine applies it at the next safe boundary between turns, and refreshes from the returned status. A stale revision keeps every local edit in place and re-reads the canonical revision for retry; a `restart_required` reply directs the user to the guided restart instead.
- Active owned runs expose **Stop after current turn** beside the secondary
  **Stop now** action. The former sends the existing revisioned
  `owner_stop=true` control intent and remains visibly pending while the
  current worker/reviewer call finishes; it never calls the unit-stop endpoint
  or implies checkpoint approval. The latter keeps the existing immediate
  owner-stop endpoint and interrupts the exact active unit after confirmation.
- REST `PATCH .../control` and MCP `control_run(..., owner_stop=true)` share the
  same boundary intent and idempotency behavior. Fresh run reads include the
  bounded pending owner-stop projection; only canonical finalized status and
  ownership render a stopped run.

Guided workflow change (restart):

- Selecting a different workflow for a nonterminal owned run is a guided stop-then-start, never an in-place mutation. After explicit confirmation, the UI issues the owner stop with the expected revision, then polls the canonical status until that exact source run reports `owner_stopped` status **and** launch phase (the engine's own precondition) before submitting the preserved typed start draft with `restarted_from_run_id`. The wait is bounded.
- If the stop is rejected, the revision changes, inactivity cannot be proven, the network fails, or the successor start fails, automation halts: no retry loops, no overlapping units, the draft stays in the form, and the authoritative source state is shown for an explicitly renewed attempt.
- Explicit resume remains the separate same-workflow action for `needs_attention` runs. The current UI intentionally omits the optional run-wide instruction editor; authenticated REST and MCP callers may inherit, replace, or clear instructions. Legacy runs are read-only and offer no controls. Owner stop and workflow restart keep their explicit confirmations; Diagnostics shows a deterministic summary. Opening Raw details loads the best supported context level (sending `level=full&full_scope=true` where supported) without extra acknowledgement. The shared page Refresh retries failures; stale responses are rejected when selection changes.

Run links contain only registered project ID, view and optional run ID. Reload and browser back/forward restore the requested workspace. An older linked run is fetched directly even when absent from the first page. Invalid project/run links show guidance instead of selecting another run; unknown views and legacy Overview links normalize to Runs for valid projects. Copy link uses only validated identifiers. Tokens, configuration text and prompts never belong in links.

## Owner acceptance after deployment

1. Use the actual private HTTPS address after deployment; confirm the installed release. Sign in once, refresh twice and open a new tab: the session should restore without another token.
2. In Projects, open an already-added project and confirm Runs visibly opens. Navigate Settings → Plans → Runs, then back/forward, checking the selected project remains clear.
3. Prepare a disposable Git project with a committed README beneath the managed root. Find it by typing, click Add, and confirm its original files and commit are preserved.
4. Build a starter draft. Choose the installed harness/profile and assign the worker role using keyboard suggestions. ZCode requires its existing host-side model configuration. Review help and validation, save, and confirm the project becomes Ready.
5. Create a safe one-checkpoint Markdown plan that writes a disposable text file. Save and Move to Ready, use Run this plan, inspect the exact step/profile preview and start it. Follow the returned run, reload its link and verify both successful completion and the expected file contents.
6. Check wide and narrow layouts, available choices, disabled-action explanations, progress and Diagnostics. Copy the run link and reopen it. Logout, then reload the other tab and confirm it requires sign-in.
7. Unregister the disposable project after saving evidence; unregister must preserve its files. Keep the previous release available for rollback using the deployment runbook if acceptance fails.

The App journey test mocks API responses and checks UI contracts, including recovery from a failed save. Server tests verify real validation, containment and persistence. Neither substitutes for the owner’s deployed browser and worker-output checks above.

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

A token file is reread per request so rotation does not require a restart, and when `aflow ui` serves the app the whole global `config.toml` is re-read on change, so a password rotation applies immediately. The managed root, registry, executable, release identity, and environment file are server-owned launch inputs; `aflow ui` resolves the executable and release identity from its own installation and prepares a private worker environment file automatically. All projects share the canonical global workflow pair (`~/.config/aflow/aflow.toml` plus `workflows.toml`).

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

- `GET /api/project-discovery` (read-only bounded candidates)
- `GET /api/projects`
- `POST /api/projects`
- `GET /api/projects/{project_id}`
- `PATCH /api/projects/{project_id}`
- `DELETE /api/projects/{project_id}`
- `GET /api/config` (the shared global pair)
- `PUT /api/config`
- `PATCH /api/config` (expected revision plus ordered typed actions or a partial documents map, exclusively)
- `POST /api/config/validate`
- `POST /api/config/form` (pure projection or one draft operation; does not save)
- `GET /api/settings` and `PUT /api/settings` (transport settings; write-only password; restart-required reporting)
- `POST /api/settings/preview` (pure projection of Advanced connection settings into supported fields)

Project creation and registration accept only normalized paths relative to the managed root. Existing folders are added without renaming: the registry ID is an internal path-safe label, so basenames with underscores, uppercase letters, spaces, or non-ASCII characters get a deterministic safe ID (safe-slug basenames keep their exact name), and the discovered display name uses the actual folder name. Registration never modifies directory contents — no project-local configuration is written, because every project immediately uses the shared global configuration — and unregister removes the registry record after active-unit checks and never deletes repository files.

Configuration reads return both exact texts, a combined SHA-256 revision, and bounded validation results. Saves require `expected_revision`, validate both candidates through the production loader, and atomically commit or restore the pair. Saves are never blocked by runs: active calls finish with their starting settings, and each next turn or resume reloads the current pair. A run may retain an immutable launch-time snapshot for compatibility diagnostics (under the shared lock with saves), but edits apply to existing runs at their next safe boundary. Settings changes to bind host/port or the projects root report restart-required; password changes invalidate sessions immediately.

Settings is global, even with no registered projects. Its freely selectable tabs
are Agents & Roles, Teams, Workflows, Prompts, and General. Save all changes sends
only net guided operations or deliberately edited Advanced documents, then only
changed server fields. Password rotation is last. If server settings fail after
workflow configuration saves, the UI reports partial success and retains the
remaining edits. Revision conflicts require explicit reload/reapply.

Model and supported effort fields accept custom text. Named prompts in `[prompts]`
support creation, editing, rename with exact workflow-reference updates (including
`merge_prompt` names and arrays), and deletion when unreferenced. Referenced prompts
show a compact "Used by …" disclosure and offer no deletion; an unreferenced prompt
keeps a small per-prompt More menu whose **Delete prompt…** action requires explicit
confirmation, states that removal happens on Save all, and offers Undo until save —
the server still rejects any referenced deletion. Role/team prompt overrides are
literal text in `[roles.prompts]` and `[teams.<name>.prompts]`; removing a team
override restores global inheritance. Installed skills and arbitrary files are
outside the editor.

Each prompt text editor shows a **Template variables** reference. Named workflow
and merge prompts document the plan/checkpoint variables (`{ORIGINAL_PLAN_PATH}`,
`{ACTIVE_PLAN_PATH}`, `{NEW_PLAN_PATH}`, `{NEXT_CP}`, and
`{WORK_ON_NEXT_CHECKPOINT_CMD}`); merge-only execution variables are labeled by
scope. Examples are illustrative rather than values from a selected run. Role
and team overrides remain literal text and are not passed through this renderer.

Teams are created with an explicit **Add team** form (button or Enter) that adds the
team to the unsaved draft immediately — it becomes selectable everywhere before Save
all, and `add_team` actions precede their dependent edits in one changes-only batch.
Each team editor shows the team name once in its heading, labels roles plainly
(Worker, Reviewer, …), and exposes an **Upgrade to** selector editing the existing
`[teams.<name>].upgrade_to` link (`set_team_upgrade`; null removes the link). The
full ordered worker chain is displayed with per-stage worker selector/model/effort;
cycles and missing targets show field-level errors, and production graph checks stay
authoritative. `backup_team` remains a separate recovery field and never appears as
an upgrade stage. A mistyped prompt table (for example `prompts = "wrong"`) no longer
breaks the guided view: the saved documents stay editable under Advanced TOML with
the production validation diagnosis, and confirmed reload discards every pending edit
including the password.

## Plan API

- `GET /api/projects/{project_id}/plans`
- `POST /api/projects/{project_id}/plans`
- `GET /api/projects/{project_id}/plans/{status}/{name}`
- `PUT /api/projects/{project_id}/plans/{status}/{name}`
- `POST /api/projects/{project_id}/plans/{status}/{name}/promote`
- `GET /api/projects/{project_id}/plans/{status}/{name}/backups`

`status` is `todo`, `in_progress`, or `done`; filesystem directories remain `todo`, `in-progress`, and `done`. Creation starts in `todo`. Promotion follows `todo → in_progress → done`. Updates and promotion require the current SHA-256 revision. Each file is UTF-8 Markdown at most 256 KiB. Unsafe names, nesting, links, non-regular files, stale revisions, and occupied move targets are rejected without changing the source bytes.

The web client lists all three states, creates and edits plans with revisions, promotes them through the lifecycle, and opens in-progress plans in the run dashboard.

## Run control

The durable control-plane surface remains under `/api/control-plane`. It provides capabilities, plans, runs, event snapshots and streams, bounded context, startup answers, compare-and-swap controls, owner stop, and resume. State-changing retries use the `Idempotency-Key` header.

A start request accepts typed plan_path, workflow_name, team, start_step,
max_turns, bounded extra_instructions, and restarted_from_run_id. Unknown fields
fail validation. Capabilities expose the ordered declared/executable/excluded
workflow steps and admitted selectors so a client can construct valid choices.
A restart predecessor must have confirmed failure or explicit owner stop and no
active exact unit; a successful request creates a new run and exposes immutable
predecessor lineage. Resume continues the saved invocation. Its optional JSON
`extra_instructions` field uses the same three-way semantics as the CLI:
omitted or `null` inherits, a bounded string list replaces, and `[]` clears for
the successor. The predecessor remains unchanged, and a reused idempotency key
must carry the same effective instructions.

`GET /api/control-plane/projects/{project_id}/runs/{run_id}/restart-options`
projects eligibility, a bounded reason, and original launch choices. Restart
opens New run with an immutable source identity and permits the same workflow.
An active source still follows stop/confirm-inactive before launch. Failed
inactive sources require no artificial owner-stop. A new attempt retains plan
progress and freezes current committed settings; it never restores backups.
Extra instructions that were not durably retained must be re-entered explicitly.
Reset Plan and backup restoration remain deferred to issue #34; existing and
changed backup evidence is retained and identical bodies remain deduplicated.

The default All runs view includes every ongoing run plus the latest N other
runs across all registered projects. N defaults to 10 and is saved only under
`aflow.recentRunsLimit` in this browser; it does not delete history. Paused and
input-waiting runs count as ongoing. Visible views refresh every ten seconds,
with partial/stale results labelled and each row linking to its exact run.

The `/mcp` and `/mcp/` streamable HTTP endpoints expose the same canonical
operations and bearer policy through the UI server. They share the REST
control-plane service, and HTTP disconnects do not stop independently owned
workflow workers.

## Development checks

```bash
uv run --project apps/aflow_app/server pytest -q
npm --prefix apps/aflow_app/web test -- --run
npm --prefix apps/aflow_app/web run build
uv run ruff check apps/aflow_app/server/src apps/aflow_app/server/tests
```

### Worker failures and diagnostics

A detached worker that exits before controller metadata exists reports **Could not start**, its known exit code and worker exit time. Historical failures whose output was discarded say **Original worker error was not retained**. Unconfirmed worker activity reports **Needs attention** and does not admit recovery. A successful process exit alone is not workflow completion. Restart with options rechecks ownership and lineage; Resume still requires saved continuation state. Status reads do not rewrite run files.

New workers retain bounded, redacted recent stdout/stderr in their owned receipt directory and structured early exceptions where possible. Overlong output lines are omitted; these are diagnostic tails, not complete logs. The single page **Refresh** updates the list, selected status, timeline and diagnostics. Automatic updates continue while visible. **Diagnostics summary** uses known fields; **Raw details** expands bounded artifacts by source. API/MCP Lite and Full remain compatible transport options.

Set **Recent non-running runs shown** only in **Settings → General** (default 10). It remains browser-local under `aflow.recentRunsLimit` and does not change retention. Prompt deletion Undo survives Settings tabs and Advanced/guided navigation, supports multiple deletions, and remains available after a failed save; acknowledged save or explicit discard clears it. Conflicting keys must be resolved before Undo can restore the prompt.

Settings tabs and Save remain pinned within the workspace scroller through the full form height, including long prompt lists on mobile.
