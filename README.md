# AFlow

AFlow is a local controller for plan-driven coding workflows. It reads a
checkpointed Markdown plan, runs configured steps through installed agent CLIs,
and uses the updated plan to decide what runs next.

The Python package is named `aworkflow`; it installs `aflow` and `aworkflow`
as equivalent workflow commands. It also installs the optional `aflowd`
durable control-plane service entry point.

## How it works

1. A workflow defines steps, transitions, roles, and optional git lifecycle
   actions.
2. A team maps each role to a configured harness profile.
3. Each step starts a fresh agent CLI process with the relevant plan and
   instructions.
4. AFlow re-reads the plan, records the turn, and follows the next matching
   transition.

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
profiles, roles, teams, prompts, controller settings, and the optional
`[daemon]` section. `workflows.toml` contains workflow graphs and lifecycle
defaults.

## Run a plan

```bash
aflow run path/to/plan.md
aflow run --workflow hard --plan path/to/plan.md
aflow run --team TEAM_NAME --max-turns 10 path/to/plan.md
aflow run path/to/plan.md -- keep changes limited to the requested scope
```

If no workflow is named, AFlow uses `aflow.default_workflow` from the
configuration.

Status and progress are printed as plain, append-only `key=value` records on
stderr. Interactive terminals, redirected logs, and `TERM=dumb` environments
receive the same ordered, copyable lines with no cursor movement, ANSI styling,
or keyboard capture, and one final summary record per run. `aflow show` prints
plain ASCII workflow graphs, roles, and teams. The CLI is a portable launcher
and log stream; the remote web application is the interactive dashboard.

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
reattach when it returns. Each run freezes a copy of the workflow
configuration at launch, so later configuration edits affect only new runs.
Normal installations ship the UI inside the `aworkflow` wheel and never need
Node; editable development installs build the web assets automatically.

## Run the lightweight local daemon

`aflow daemon` exposes the same 13 control-plane MCP tools without the remote
web app, FastAPI, or systemd. Stdio is the default and must stay attached to its
client; optional HTTP binds only to loopback.

```bash
aflow daemon start --foreground
aflow daemon start --mcp-transport http --mcp-port 8765
aflow daemon status
aflow daemon stop
```

The daemon owns one repository. Each workflow runs in its own subprocess group;
client EOF, `daemon stop`, SIGINT, and SIGTERM drain those owned children. A
mode-0600 pidfile binds stop/status operations to the daemon's process-birth
identity. Status reports only direct workers for the verified repository, using
Linux procfs or a portable process-table fallback. If ownership inspection is
unavailable or untrusted, status returns an ambiguous nonzero result instead
of claiming zero workers. This local mode does not serve REST or the optional
remote app's React dashboard.

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
when its heading and all tasks in that section are checked.

On the first launch of a pristine plan, review workflows automatically add the
minimal controller-owned `## Git Tracking` section before the first checkpoint.
Ready repositories record the current commit; an empty-repository lifecycle
records the verified bootstrap commit before the first ordinary turn. Started,
resumed, recovery, malformed, or ambiguous plans are not silently repaired.

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

The frozen workflow configuration validates selectors before accepting the
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
aflow daemon --help
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
the canonical REST API and SSE stream, with MCP as an optional adapter.
The web client is the interactive dashboard: typed run starts
(plan, workflow, team, start step, max turns, bounded extra instructions),
SSE progress with reconnect-safe snapshots, capability-gated compare-and-swap
controls for max turns/team/selectors, explicit resume, and guided
stop-then-start workflow changes with `restarted_from_run_id` lineage. Provider
choice stays in normal engine harness profiles; Codex is one optional harness
adapter. A remote ACP interface is deferred.

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
