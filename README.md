# AFlow

AFlow is a local controller for plan-driven coding workflows. It reads a
checkpointed Markdown plan, runs configured steps through installed agent CLIs,
and uses the updated plan to decide what runs next.

The Python package is named `aworkflow`; it installs `aflow` and `aworkflow`
as equivalent workflow commands.

The remote app opens on All runs: every ongoing run across registered projects
a separate Needs attention group for unconfirmed outcomes, and the latest 10
terminal runs (the count is a browser preference). Projects
have separate Runs, Plans, and New run pages. Global Settings offers Agents &
Roles, Teams, Workflows, Prompts, General, and a read-only Changelog tab with
one changed-only save coordinator. Changelog shows concise date-grouped release
titles from the built artifact, initially 20 entries at a time.
Appearance follows the system by default; Light and Dark overrides
persist in the browser. Startup failures retain their safe explanation after
reload, and runtime starts only from execution evidence. Starting requires a
currently verified preparation owner or workflow unit. Archive hides history
until restored; Delete record permanently hides it while retaining workflow
files and recovery evidence. Neither action stops a workflow.
Runs, Teams, Workflows, and Prompts use separate navigation/editor scroll panes.
Advanced TOML is a settings-wide mode; guided drafts share one save coordinator.
Failed runs offer Resume when saved execution supports it, or Restart with
editable launch options. Restart creates a new attempt using current settings
and retains plan progress; Resume reloads the current configuration source and
keeps only the predecessor's execution and lifecycle facts. A launch-time
snapshot, when present, is diagnostic compatibility evidence rather than an
execution gate.

## How it works

1. A workflow defines steps, transitions, roles, and optional git lifecycle
   actions.
2. A team maps each role to a configured harness profile.
3. Each step starts a fresh agent CLI process with the relevant plan and
   instructions.
4. AFlow re-reads the plan, records the turn, and follows the next matching
   transition.

## Plan backup history

Workflow snapshots retain their existing backup names, bytes, and
byte-identical deduplication. Atomic provenance records keep exact capture
paths, events, run/turn references, follow-up origins, and lifecycle moves.
Service-created drafts receive a fresh current identity when a filename is
reused, so shared backup bytes do not transfer another plan's Ready baseline.
The read-only history view reports baseline status for the selected plan and
keeps temporary follow-up evidence attached to its original plan through
successful moves. Each displayed event uses that plan's own capture reference
or explicit Ready-promotion metadata; missing historical timing stays
unavailable instead of borrowing another body's first capture. Missing or
conflicting metadata remains unknown; restoration and reset are not part of
this interface.

Configuration is read as a consistent `aflow.toml`/`workflows.toml` pair at
reservation, startup, resume, and each safe turn boundary. A saved explicit
team, max-turns value, or start step keeps that choice; omitted defaults follow
the current source. A completed call keeps the settings it started with, while
the next boundary sees valid edits to prompts, models, roles, manager policy,
limits, and workflow steps. Invalid current TOML or an unusable selected target
fails clearly before a harness starts; an invalid remote control is rejected
without changing the prior accepted control.

The dirty-worktree checkbox is a separate startup choice. An existing-checkout
run sees acknowledged source changes, while a fresh worktree starts from the
selected committed tree and leaves source changes in place. Configuration edits
do not require a second acknowledgment or a controller restart.

AFlow invokes existing CLIs rather than provider APIs. The selected harness must
already be installed and authenticated. Adapters are included for Claude,
Codex, Copilot, DSH (ACP), Gemini, Kiro, Muse, OpenCode, Pi, Reasonix, and ZCode.
ZCode uses its own project model/reasoning configuration; see the
[ZCode profile setup](docs/configuration.md#zcode-profiles).

Before starting a harness, AFlow checks that its required local tools are
available. If a requirement is missing, AFlow stops and explains what is
needed. It does not install or repair anything, and it does not verify
authentication, quota, network access, or provider health.

## Install

AFlow requires Python 3.11 or newer.

```bash
uv tool install aworkflow
```

The first invocation creates `~/.config/aflow/aflow.toml` and
`~/.config/aflow/workflows.toml`, then exits so they can be reviewed.

```bash
aflow
aflow install-skills
aflow show
```

Configuration is split across two TOML files: `aflow.toml` contains harness
profiles, roles, teams, prompts, and controller settings. `workflows.toml`
contains workflow graphs and lifecycle defaults.

## Read-only guard reports

The bundled `aflow-guard-development-run` skill treats an explicit `:vr`
request as a report for the already pinned run. It takes one
ownership-matched, read-only observation, runs
`aflow_guard_report_input.py`, and then runs the bundled Pillow renderer through
`uv run --script aflow_guard_report.py`. The renderer writes only a deterministic
1240×1754 A4 `guard-report.png` and its normalized `guard-report.json`.

Keep snapshots, inputs, and reports in an absolute directory outside the guarded
repository. Healthy scheduled observations stay silent; `:vr` does not authorize
recovery, notifications, email, provider access, or generic visualization/image
generation. UI-server and `aflowd` runs use their authenticated canonical MCP
`get_run` observation, with pinned repository/run/ownership metadata and a
captured response time, rather than legacy process inspection.

## Run a plan

```bash
aflow run path/to/plan.md
aflow run --workflow hard --plan path/to/plan.md
aflow run --team TEAM_NAME --max-turns 10 path/to/plan.md
aflow run path/to/plan.md -- keep changes limited to the requested scope
```

If no workflow is named, AFlow uses `aflow.default_workflow` from the
configuration.

## Run progress in the web view

The authenticated web application opens All runs, where an owner can select a
run and inspect its checkpoint history. The selected run view uses the same
read-only progress projection as the list summary: checkpoint position,
approval totals, attempt history, executor identity, applied or pending
changes, and delivery evidence remain tied to the selected project and run.
An active reviewer is distinct from a queued review, and each verified
repartition generation remains listed beneath its stable original checkpoint.

Counts are evidence-qualified. A partial count is a lower bound, and missing
or conflicting evidence is shown as unavailable rather than treated as zero.
Approval is separate from a checked plan item, and compact markers use only
identity-specific checkpoint evidence rather than aggregate approval counts. A
committed lifecycle does not by itself prove a successful merge, and a pending
or unknown CI/live stage is never presented as success. Historical executor names and models are
shown exactly as recorded. Refresh and compact Back retain the selected
checkpoint when its identity remains available; the observer view does not
launch workflows or mutate controller state.

Status and progress are printed as readable, append-only blocks on stderr. A
run emits one identity and plan header, preparation and turn updates, and a
final summary. On a capable TTY, only each block's heading is bold; redirected
streams, `TERM=dumb` or missing/empty `TERM`, and any `NO_COLOR` setting remain
plain. The renderer never refreshes the screen, captures keyboard input, or
emits cursor, alternate-screen, polling, or heartbeat output. Full paths to
saved turn logs remain visible. Structured observer events and `.aflow/runs`
artifacts are the machine-facing alternatives. `aflow show` prints plain ASCII
workflow graphs, roles, and teams. The CLI is a portable launcher and log
stream; the remote web application is the interactive dashboard.

## Reusable NO-OP smoke plan

From a disposable Git repository, run:

```bash
aflow noop-plan
aflow run --plan aflow-noop-plan.md --team TEAM_NAME --workflow WORKFLOW_NAME
```

The first command copies a bundled three-checkpoint plan into the current
directory and creates `/tmp/aflow_noop_plan/worker.playbook`,
`reviewer.playbook`, and `checkpoints/`. Defaults complete immediately: workers
touch `CP_N.txt`, reviewers pass. Agents still use the selected workflow's
normal plan bookkeeping, review, commit, and merge behavior.

Edit playbooks to inject behavior (semicolon-separated actions, one entry per CP):

```text
# worker.playbook
CP1: cleanup; done
CP2: sleep 2; done
CP3: fail BLAH BLAH

# reviewer.playbook
CP1: sleep 60; pass
CP2: sleep 0; pass
CP3: reject Retry after the operator changes this entry
```

`sleep` invokes the system command; `fail MESSAGE` reports `AFLOW_STOP` and
halts; reviewer `reject MESSAGE` requests the normal repair/review path.
Injected hard failures include `AFLOW_NOOP_MOCK_FAILURE`; fixture errors and
provider failures do not, and must not be counted as successful fault injection.
Worker `incomplete MESSAGE` leaves the checkpoint pending without a hard stop,
so a reviewer/manager can exercise a configured quality-upgrade chain. Use a
distinct message such as `MOCK_CAPABILITY_FAILURE` and change the playbook to
`done` when the upgraded worker starts. Upgrades remain manager decisions;
this action does not force routing or disguise real harness/provider errors.
Missing entries succeed without delay. A successful worker touches its marker
after all actions; `cleanup` deletes only numbered CP markers. Playbooks are
read fresh each invocation and are never repaired by the agent. The plan uses
`aflow noop-step worker|reviewer N --state-dir PATH` to execute these actions;
the helper reports status but never edits the plan or commits.

Use `--checkpoints N`, `--output PATH`, and `--state-dir PATH` to customize the
fixture. Re-running preserves playbooks and markers. `--force` replaces the
copied plan; `--reset` also resets playbooks and numbered markers. Use separate
state directories for concurrent runs. This tests agents and workflow routing,
not a deterministic harness exit code: agents must relay injected failures.
The generator records the current branch when run inside Git, which supports
in-place workflows; branch/worktree workflows replace it at startup. If you
generate outside Git, initialize a branch and regenerate before an in-place run.

## Serve the web UI from any directory

```bash
aflow ui
```

One command installs nothing extra and serves the AFlow web UI. The first
launch asks for a UI password and the projects root (default `~/code`),
writes them to `~/.config/aflow/config.toml` (mode 0600), and serves the UI
with the same global workflows and teams the CLI uses. The UI binds
`0.0.0.0:8765` after setup, so other devices on your network or Tailscale can
open it directly over HTTP.

```bash
aflow ui --daemon   # start in the background and return once ready
aflow ui --status   # report the running server
aflow ui --stop     # stop the UI; running workflows are not signalled
aflow ui --host 127.0.0.1 --port 8765   # per-process overrides
```

Workflows started from the UI keep running when the UI stops or restarts and
reattach when it returns. Each run may retain a launch-time configuration
snapshot as diagnostic provenance; execution uses the current global pair at
reservation, startup, resume, and safe turn boundaries.
Normal installations ship the UI inside the `aworkflow` wheel and never need
Node; editable development installs build the web assets automatically.
The release Changelog is generated deterministically from the root `DEVLOG.md`
as part of every web build; run `npm --prefix apps/aflow_app/web run changelog`
to regenerate the ignored JSON and Markdown artifacts in a checkout. DEVLOG is
the only authored changelog source. Built wheels carry the same release data as
`aflow/ui_web/changelog.json`, so installed clients do not need Node or a
repository checkout to read it. Settings renders that packaged data without a
runtime repository or configuration request; switching to Changelog does not
discard dirty settings or skill drafts.

## Use MCP through the UI server

`aflow ui` serves the existing authenticated HTTP MCP endpoint from the same
FastAPI application as the dashboard. Start the server in the foreground or
background, then connect to its configured URL plus `/mcp` (the equivalent
`/mcp/` spelling is supported):

```bash
aflow ui
aflow ui --daemon
aflow ui --status
aflow ui --stop
```

Send the configured `[server] auth_token` (or `auth_token_file`) as an
`Authorization: Bearer <token>` header. Do not put credentials in URLs, JSON
arguments, or browser cookies; MCP is header-only. The [secret-free client
template](apps/aflow_app/server/aflow-control-plane.mcp.example.toml) uses an
environment-backed token and approves write tools.

The shared lifecycle registry exposes these 14 tools:

- `get_capabilities` — list capabilities for every allowlisted project.
- `list_projects` — list registered projects.
- `get_project_capabilities` — inspect one project's capabilities.
- `list_plans` — page through a project's plans.
- `list_runs` — page through a project's run status.
- `get_run` — read one run's canonical state.
- `get_run_events` — read a bounded run-event tail.
- `get_run_context` — read bounded run context.
- `preflight_run` — inspect launch dirtiness without allocating a run.
- `start_run` — reserve and start a run, or return its startup question.
- `answer_startup` — answer a pending startup question.
- `control_run` — apply a revision-checked run control; `owner_stop=true`
  requests the existing stop-after-current-turn boundary intent.
- `owner_stop` — immediately interrupt the exact active unit and stop a run.
- `resume_run` — create an idempotent continuation of a run.

The UI-server registry also exposes these web authoring tools over the same
authenticated MCP connection:

- `get_global_config` — read the one `aflow.toml`/`workflows.toml` pair shared by all registered projects.
- `patch_global_config` — apply typed actions or an exact document edit with an expected revision; the pair is validated and committed atomically.
- `read_plan` — read one revisioned Markdown plan document.
- `create_plan` — create a bounded draft in `plans/todo`.
- `create_plan_from_run` — create a bounded editable follow-up draft from the exact failed or attention-needed run evidence.
- `update_plan` — compare-and-swap one plan document.
- `promote_plan` — move a plan through `todo` → `in_progress` → `done` with its revision.
- `list_plan_documents` — list plan documents with their status and revisions; use `list_plans` for lifecycle metadata used by run control.

Three read-only resource templates expose project capabilities, run state, and
lite run context. Lifecycle writes require client approval and use their
existing idempotency/revision contracts. Authoring writes require approval and
use the plan or global-pair expected revision where applicable; reads are
read-only/idempotent, while create, update, promote, and settings patch are
non-idempotent mutations. Reusing a lifecycle idempotency key returns the
original result, while a changed request is rejected. `preflight_run` is
read-only and reports bounded dirty-path pages before a launch; `start_run`
preserves the existing startup-question flow.

A typical authored launch is: discover the tools, read global settings, apply
a typed settings patch with its current revision, then either call
`create_plan` or read the exact failed/attention-needed run with `get_run` and
`get_run_context` before calling `create_plan_from_run`. Read the draft, update
its completion criteria with the returned revision, promote it, and use
`list_plan_documents`, `preflight_run`, and `list_plans` before calling
`start_run`. If a browser or another MCP client changes a document first,
reread it and retry with the returned revision; the server never silently
retries stale writes. Follow-up drafting is bounded evidence-to-plan support:
automatic adaptation, scoring, rollback, and issue-tracker closure are not
part of this change. Global settings affect all registered projects at their
next safe boundary or resume. A run's launch snapshot is diagnostic
provenance, not an execution configuration gate.
Project registration and skill installation remain separate existing browser
actions; they are not implied MCP authoring operations.

HTTP connection loss, UI restart, and `aflow ui --stop` do not stop workflow
workers. Network reachability follows the server's existing bind and private
Tailscale Serve settings; MCP does not open another port. There is no
standalone `aflow daemon` command, stdio transport, or `aflowd` executable.
The retained systemd `aflowd.service` deployment runs `aflow-app-server` and
keeps its existing service/state paths.

## Modern multi-project boundary

The current UI service implements multi-project operation through one
versioned registry beneath the configured managed root. Registry records store
normalized relative paths and are admitted only when the exact path is a
non-symlink Git root. REST, MCP, plan authoring, and lifecycle calls resolve
the requested project ID through that registry for every operation; identical
plan names and run-history names therefore remain distinct by project root.

All registered projects intentionally share one validated global
`aflow.toml`/`workflows.toml` pair and one server-selected executable,
environment file, and release identity. The immutable launch manifest records
the exact project and plan identity. The daemon-owned start record and launch
event record the selected runtime identity, while transport payloads do not
accept executable, environment-file, or arbitrary-environment overrides.

Checkpoint 1 verifies the two-project REST/MCP boundary, negative unknown,
traversal, foreign-root, and unsafe-registration cases, release identity
propagation, secret-free rejection responses, and the installed-wheel restart
journey with Chromium. The old root-owned `projects.toml`, standalone service,
and mandatory frozen per-run configuration requirements are superseded by this
registry/service/live-global-settings design; they are not restored here.
Production deployment, rollback, real provider execution, and migration of a
retired service remain separately coordinated concerns.

A minimal plan has checkpoint headings and task items:

```md
# Plan

### [ ] Checkpoint 1: Add the command
- [ ] implement the entry point
- [ ] add focused tests

### [ ] Checkpoint 2: Document the behavior
- [ ] update the user documentation
```

The plan file is the source of truth for progress. A checkpoint is complete only
when its heading and all tasks in that section are checked. User-owned manual
acceptance belongs in a separate top-level `## User Acceptance Pending` section
after the checkpoints, remains pending until evidence is supplied, and does not
change implementation completion.

On the first launch of a pristine plan, review workflows automatically add the
minimal controller-owned `## Git Tracking` section before the first checkpoint.
Ready repositories record the current commit; an empty-repository lifecycle
records the verified bootstrap commit before the first ordinary turn. Started,
resumed, recovery, malformed, or ambiguous plans are not silently repaired.

New plans should keep the canonical unnumbered heading and controller-owned fields:

```markdown
## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``
```

One integer-dot-prefixed heading such as `## 3. Git Tracking` remains supported for
existing plans without rewriting their history; duplicate tracking sections remain
invalid.

## Included workflows

- `ralph`: repeat one implementation step without a review phase.
- `review_implement_review`: review the plan, implement it, then review and
  squash the completed work.
- `review_implement_cp_review`: review each checkpoint and finish with a
  no-squash audit of the full plan.
- `medium`: alias for `review_implement_review` and the packaged default.
- `hard`: alias for `review_implement_cp_review`.

Run `aflow show [WORKFLOW]` to inspect the effective steps, transitions, roles,
and teams.

## Runs and git lifecycle

The packaged lifecycle creates a feature branch and linked worktree, runs the
workflow there, then performs the configured merge handoff and removes the
worktree after successful completion. Lifecycle behavior is configurable per
workflow.

Structured run state and logs are written under `.aflow/runs/<run-id>/` in the
primary checkout.

```bash
aflow analyze <run-id>
aflow run --resume <run-id>
```

Resume accepts an eligible, complete schema-v2 run in any supported lifecycle
mode: no lifecycle, branch-only, or linked worktree. Most resumptions continue
an incomplete plan; a narrowly validated completed run may instead retry only
a failed terminal merge. AFlow reconstructs the saved plan, invocation, and
lifecycle identity when no plan is supplied. Older run metadata remains
readable for analysis but is not migrated or resumable. Detailed compatibility,
recovery, supervision, and next-turn override rules are documented separately.

Use `--continue-from-current` for an accepted, partially completed plan:

```bash
aflow run --continue-from-current path/to/plan.md
```

The current symbolic branch must match the plan's Git Tracking Plan Branch, and
its full HEAD must match Pre-Handoff Base HEAD. The plan must contain both a
completed and an unchecked checkpoint. AFlow starts a normal nested lifecycle
from that branch, records the continuation branch and HEAD in `run.json`, and
merges the generated feature branch back to the same branch. The option is
explicit and cannot be combined with `--resume` or `--resume-reset-scope`.

For a repository and registered managed worktree copied under new absolute
paths, relocation is explicit and fail-closed:

```bash
aflow run --resume RUN_ID --resume-rehome-worktree /path/to/registered-worktree
aflow run --resume RUN_ID --team TEAM_NAME
```

The first command requires the current primary checkout on the recorded main
branch, the recorded feature branch and pre-handoff commit to exist, and the
replacement path to be the exact registered feature worktree. AFlow remaps
only repository-owned resume paths, carries validated scope-v2 evidence into
the continuation, preserves the source run byte-for-byte, and records
`resume_relocation` in the new `run.json`. It never discovers or creates a
replacement worktree.

A different baseline team is accepted only for an explicitly named resume and
a configured team. Pending manager notes, team-step overrides, finalized turns,
boundary or repartition transactions, hotplug transactions, and unapplied
owner routing changes name the blocking field and stop before allocation. The
continuation records `resumed_from_team` and `resume_team_override`; automatic
and same-team resumes retain strict team equality.

### Live worker role hotplug

An override may change a worker selector at the next safe worker boundary:

```toml
[roles]
worker = "codex.strong"
```

The current workflow configuration validates selectors before accepting the
digest. `team` and manager-owned one-turn upgrades retain their existing
precedence; a run-local `[roles]` change is applied only after the current
worker turn is durable. Same-harness changes require exact session resume and
model capability. Cross-harness changes require a read-only operational
handover; they do not migrate hidden context. Unsupported provider capabilities,
invalid artifacts, capability drift, and ambiguous crashes fail closed.

`aflow analyze <run-id>` reports hotplug stage, selectors, relative artifact
paths, hashes, and whether a provider operation is present. It intentionally
omits raw prompts, notes, environment, and provider session identifiers.

## Local development

Develop AFlow from a checkout through the same installed entry point used in
normal operation:

```bash
uv tool install -e . --force
aflow run path/to/plan.md
aflow ui --help
uv run ruff check aflow apps/aflow_app/server/src
uv run pytest -q
```

CI checks only production Python for unused imports, redefinitions, unresolved
names, and unused locals.

`uv run pytest -q` is the supported root test command on Linux and macOS. Linux
runs the systemd deployment tests; macOS skips that Linux-only module and runs
all core tests. Pull requests and pushes to `main` run Python 3.11 on Ubuntu
and macOS, and the package build waits for both platform test jobs. Publication
calls the same reusable CI workflow before uploading a package.

The optional remote workflow-control app lives in `apps/aflow_app/` and is
not included in the published wheel. Its Python 3.12+ server manages registered
projects, revisioned configuration and Markdown plans, and durable runs through
the canonical REST API and SSE stream, with the same authenticated MCP registry
mounted at `/mcp` and `/mcp/` by the UI server.
The web client is the interactive dashboard: typed run starts
(plan, workflow, team, start step, max turns, bounded extra instructions),
SSE progress with reconnect-safe snapshots, capability-gated compare-and-swap
controls for max turns/team/selectors, explicit resume, and guided
stop-then-start workflow changes with `restarted_from_run_id` lineage. Provider
choice stays in normal engine harness profiles; Codex is one optional harness
adapter. A remote ACP interface is deferred.

The Projects page keeps each registered primary checkout as the project card.
When the server's read-only Git projection verifies registered linked worktrees,
they appear in a collapsed Worktrees disclosure with their exact names and
paths; selecting one keeps its own project and run identity. A normal
`checkpoint_delivery` launch uses its internal execution checkout without an
extra registration step. Legacy recovery of an already-existing checkout may
require registering that checkout first; when its parent is also registered,
the UI presents it beneath that parent.

Private deployments keep the backend on `127.0.0.1:8765` and publish it to the
tailnet through Tailscale Serve. After enabling Serve, use
`tailscale serve status --json` to discover the advertised MagicDNS HTTPS
address. Follow the [private deployment runbook](deploy/aflowd/README.md) for
installation, activation, verification, and rollback.

## Documentation

- [Installation and bundled skills](docs/installation.md)
- [CLI usage and plan format](docs/cli-usage.md)
- [Configuration, roles, teams, and workflows](docs/configuration.md)
- [Runtime behavior, artifacts, resume, and recovery](docs/runtime-behavior.md)
- [Python library API](docs/library-api.md)
- [Remote app](docs/remote-app.md)
- [Architecture](ARCHITECTURE.md)

Worker failures before controller startup are now visible from validated detached-worker receipts. Diagnostics provides a readable summary and expandable raw details; the page Refresh updates run data together. The browser-local recent-run count is edited only in Settings → General.

### Publish completed workflows

An explicitly enabled repository publishes successful workflows to its existing
remote branch before moving the plan to Done. This also covers in-place runs:

```sh
git config --local aflow.publishRemote origin
git config --local aflow.publishBranch main
```

These local Git settings grant publication for this repository; neither setting
is enabled by a clone. Configure both, or unset both to disable publication.
The controller requires a clean completed checkout, fetches the target, and uses
a normal push. Concurrent accepted remote changes merge in a separate temporary
checkout; conflicts preserve that checkout and fail delivery rather than overwrite
history. `publication.json` in the run directory records the source and published
commit or a failed delivery. Existing CI/CD then validates and deploys main.
Local approval, remote publication, passing CI, and live deployment are distinct
outcomes; a successful push alone does not establish live availability.
