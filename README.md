# AFlow

AFlow is a local controller for plan-driven coding workflows. It reads a
checkpointed Markdown plan, runs configured steps through installed agent CLIs,
and uses the updated plan to decide what runs next.

The Python package is named `aworkflow`; it installs both `aflow` and
`aworkflow` as equivalent commands.

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
Codex, Copilot, Gemini, Kiro, OpenCode, Pi, and Reasonix.

Before a real harness process starts, AFlow performs a bounded,
adapter-neutral environment preflight. It verifies the selected executable and
lets an adapter add a local prerequisite check. Reasonix uses reasonix doctor
--json to report enforced sandboxing that requires an executable bwrap.
AFlow reports missing prerequisites and never installs packages, repairs
configuration, or checks authentication, quota, network, or provider health.

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
identity. This local mode does not serve REST or the React dashboard and does
not replace the production `aflowd` deployment.

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

Resume reuses a compatible unfinished worktree run and reconstructs its saved
plan and invocation identity when no plan is supplied. Detailed compatibility,
recovery, supervision, and next-turn override rules are documented separately.

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
uv run pytest -q
```

The optional remote management app lives in `apps/aflow_app/` and is not
included in the published wheel. Its server requires Python 3.12+ and exposes a
provider-neutral planning-session API; Codex integration is implemented through
`codex-app-server-sdk` behind that boundary.

## Documentation

- [Installation and bundled skills](docs/installation.md)
- [CLI usage and plan format](docs/cli-usage.md)
- [Configuration, roles, teams, and workflows](docs/configuration.md)
- [Runtime behavior, artifacts, resume, and recovery](docs/runtime-behavior.md)
- [Python library API](docs/library-api.md)
- [Remote app](docs/remote-app.md)
- [p100 control-plane deployment](deploy/aflowd/README.md)
- [Architecture](ARCHITECTURE.md)
