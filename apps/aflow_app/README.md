# aflow Remote App

A mobile-friendly workflow control interface for registered AFlow projects. It supports project registration, revisioned configuration and Markdown plan editing, durable run control, event streaming, and MCP. Agent selection stays in the normal AFlow harness configuration; the remote app contains no provider-specific planning client.

The server sources also ship inside the published `aworkflow` wheel, where `aflow ui` serves them with bundled web assets and the shared global configuration. This subproject remains the development/test entry point. Full behavior and API details are in [Remote workflow control app](../../docs/remote-app.md).

## MCP through the UI server

The FastAPI application mounts the shared, authenticated MCP registry at both
`/mcp` and `/mcp/`. Run `aflow ui` or `aflow ui --daemon`, then connect to the
configured server URL plus one of those paths with an
`Authorization: Bearer <token>` header. MCP does not accept the browser
session cookie, URL credentials, or credentials in tool arguments. The
[secret-free client template](server/aflow-control-plane.mcp.example.toml)
uses an environment-backed token.

The registry retains all 14 control-plane tools, adds the web authoring tools
for global settings and revisioned Markdown plans, and exposes three resource
templates. The root [MCP documentation](../../README.md#use-mcp-through-the-ui-server)
lists their exact scope and mutation annotations. Global settings are shared
by all registered projects and take effect at the next safe boundary or
resume; plan edits remain project-scoped. Project registration and skill
installation remain separate browser actions. HTTP disconnects and UI
shutdown do not signal independently owned workflow workers.

For a failed or attention-needed run, the equivalent follow-up sequence is
`get_run` → `get_run_context` → `create_plan_from_run` → `read_plan` →
`update_plan` → `promote_plan` → `preflight_run` → `start_run`. The created
Markdown is editable, bounded evidence rather than an automatic diagnosis or
approval. Automatic adaptation, scoring, rollback, and closure of the broad
older issue are outside this small evidence-to-plan change; the browser's
Create follow-up draft action also never launches until the existing explicit
Run action is used.

For an active owned run, the dashboard's **Stop after current turn** action and
MCP `control_run(..., owner_stop=true)` save the existing revisioned boundary
intent. The current worker or reviewer call may finish before the canonical run
becomes `owner_stopped`; the intent never invokes the unit-stop endpoint and
does not approve a checkpoint. **Stop now** and MCP `owner_stop` remain the
separate immediate, interrupting action.

## Run history and settings

All runs and project Runs offer Visible, Archived, and All history filters.
Archive preserves direct links and supports Restore. Delete record requires
confirmation and returns a Deleted record page on subsequent access; workflow
files, logs, snapshots, plans, and recovery/idempotency records remain intact.
Hiding an active run requires acknowledging that execution will continue.

Starting requires current activity evidence. Missing or untrusted activity is
Needs attention, separately listed outside Recent; confirmed execution failures
are Failed and preparation failures are Could not start.

Run details use the server's read-only progress projection. A verified
checkpoint shows its name and total; a repair overlay is shown separately with
its filename. If the original plan or scope evidence is missing or unreadable,
the dashboard says Progress unavailable instead of displaying a zero total.
Current turns and the last finalized turn are separate, so a starting turn is
never presented as finished.

Ordinary Resume remains the primary action for a normal continuation. For a
failed, interrupted, or owner-stopped run with confirmed inactive ownership,
Recover with another worker is the explicit alternative: choose a worker from
the current settings, and the server submits `mode = durable_evidence` to
create a distinct successor. The saved plan, exact worktree, scope, results,
and recorded evidence remain the source of truth; private context from the old
provider session is unavailable, so inspect the successor's Diagnostics link
and evidence before continuing. An active or uncertain source is rejected,
with the source selection and recovery draft preserved; there is no silent
fallback to ordinary resume or automatic worker failover.

REST context reads and MCP `get_run_context` use the same authenticated,
read-only context bundle, including this progress projection. The server
regression fixture and the built dashboard journey exercise that parity in
Chromium and WebKit at desktop and 390px mobile widths. Browser keyboard
events cover the responsive menu contract; physical mobile keyboard behavior
still requires separate device verification.

Run history uses `POST .../runs/{run_id}/archive`, `POST .../restore`, and
`DELETE .../runs/{run_id}` beneath the existing project route. Send
`Idempotency-Key` and a JSON `expected_revision` (the run's `history_revision`),
plus `acknowledge_active: true` when hiding active work. Lists accept
`history=visible|archived|all`; deleted external reads return HTTP 410.

The shell uses two compact header rows on desktop and a Menu/current-page/action
header on compact screens. Runs, Plans, and Settings use document scrolling;
compact lists open one detail surface with a labelled Back action. Settings
keeps one shared draft and save action. New Run keeps Plan, Workflow, and Team
choices visible, summarizes worker upgrades and reviewer assignment, and puts
verbose role and executable-step tables under a keyboard-accessible Details
disclosure. Advanced TOML replaces all guided tabs until switching back;
Connection settings TOML stays in General. Clear effort text to unset it, then
Save all changes.

The Skills tab (between Prompts and General) edits bundled `SKILL.md` files
with per-skill drafts that survive skill, tab, and Advanced TOML navigation.
Saving writes workflow configuration first, then dirty skills in name order,
then connection/password settings last; each skill clears only on its own
acknowledgement. Saving skill text updates every installed link at once, and
takes effect on the next manager invocation (workflow settings instead apply
to new runs). Install/reinstall all runs the shared default installer once,
stays disabled while any skill has unsaved edits, and reloads clean baselines
afterwards. Optional skills are labeled, excluded from the default install,
and install only through existing CLI options.

The Workflows tab exposes manager supervision next to the existing
default-team controls. Defaults shows Enabled/Disabled for the
`[workflow].manager_enabled` declaration (omitted displays as Disabled
(default) and writes nothing until changed); each workflow shows
Inherit/Enabled/Disabled with its effective value and source (this workflow,
a base, or defaults). Explicit `false` stays `false`, including on aliases,
and the launch-default workflow never affects inheritance. Changing a
declaration refreshes the effective preview from the server projection while
keeping pending edits, and saves emit only the net `set_default_manager_enabled`
/ `set_workflow_manager_enabled` actions. Supervision flags apply at the next
 safe boundary, including active runs; snapshots are diagnostic compatibility
 artifacts only.

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

The server requires Python 3.11 or newer and binds to `127.0.0.1:8765` by default. It reads the exact registered Git roots beneath `AFLOW_MANAGED_PROJECTS_ROOT`; no directory scan grants access. Supply bearer credentials only through the `Authorization` header. All projects share the global workflow pair at `~/.config/aflow/aflow.toml` plus `workflows.toml`; per-project configuration is neither read nor written.

Plan editing is limited to regular UTF-8 Markdown files beneath `plans/todo`, `plans/in-progress`, and `plans/done`. Updates and lifecycle moves use SHA-256 expected revisions. Creating a draft without content (or with explicit null) starts from the packaged checkpoint skeleton in `aflow/templates/draft-plan.md` — one open checkpoint with implementation/verification tasks and blank Git Tracking fields to edit; any explicit string, including empty text, is kept byte-for-byte. The skeleton adds no readiness validator and never blocks saving, promotion, or launch.

The first supported `todo` → `in_progress` promotion preserves the exact draft
bytes as the known initial Ready baseline. Existing and changed backup bodies
remain in `plans/backups/`; their small atomic JSON provenance sidecars live in
`plans/backups/.provenance/` and record exact lifecycle path aliases, capture
events, timestamps, hashes, and available run/turn identity. The read-only
`GET /api/projects/{project_id}/plans/{status}/{name}/backups` route returns
bounded pages (50 by default, 200 maximum) for the exact current plan path.
Identical bodies and repeated capture references are deduplicated, nothing is
automatically deleted, and restore/reset behavior remains explicitly deferred.
In the Plans editor, expand the collapsed Backup history disclosure to see
Baseline (the initial Ready capture), later Snapshot, Follow-up, or Unknown
origin labels with capture details and bounded pages. History is read-only:
unknown original baselines cannot be safely reset, and restore/reset remains a
future scope.

Skills settings (`/api/skills`) list exactly the bundled skill registry with revision, edit, and link state. Saving edits only the account-local canonical `SKILL.md` (`~/.config/aflow/skills/<name>/`) and never installs; reinstalling via `POST /api/skills/install` runs the same default installer as `aflow install-skills --yes` (optional skills excluded). After upgrading the server package, restart it before reinstalling so refresh reads the new bundled resources.
