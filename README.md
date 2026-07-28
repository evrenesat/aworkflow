# aworkflow

`aflow` is a workflow engine that runs plan-driven coding workflows through existing agent CLIs such as Codex, Claude, Gemini, Kiro, OpenCode, Copilot, Pi, and Reasonix.

It does not call provider APIs directly. It shells out to the harnesses you already use. The main use case is a stricter loop where a stronger model plans or reviews, a fast cheap model implements the current checkpoint, and the run keeps moving until the original plan is done or reaches `END`.

## Why?

I kept wanting two things. First, a clean, repeatable way to make a detailed plan with a capable model, implement the current checkpoint with a fast cheap model, review it, and sometimes improve the plan again with a stronger model. I was doing that manually, so I wanted to automate it.

Second, I don't want to stick to a single provider harness. The best value keeps changing, and a lot of free or included usage is tied to the provider CLI, not an API budget. `aflow` is a reliable wrapper for that workflow.

## Install

Requires Python `3.11+`.

```bash
uv tool install aworkflow
```

That installs the `aworkflow` package and exposes the `aflow` and `aworkflow` commands on your `PATH`.

From a local checkout:

```bash
uv run python -m aflow run path/to/plan.md
```

## Quick Start

1. Install the package.
2. Run `aflow run path/to/plan.md` once to bootstrap config under `~/.config/aflow/`.
3. Edit `aflow.toml` and `workflows.toml` for your harness profiles, roles, teams, and workflows.
4. Install bundled harness skills with `aflow install-skills`.
5. Run a checkpoint plan:

```bash
aflow run path/to/plan.md
aflow run --plan path/to/plan.md --workflow review_implement_cp_review
aflow analyze
```

### Interstep supervision

Freshly bootstrapped configuration enables a lightweight manager between
workflow turns. It accepts or changes the controller's already-proposed next
action, and can stop a looping or ambiguous run with a self-contained report.
For a rejected implementation, configured team `upgrade_to` edges can raise
only the next worker invocation one level at a time; repeated rejection can
advance farther along the chain, while the next checkpoint starts again from
the baseline team.
Existing configurations remain compatible: supervision stays off until you add
a `[manager]` section and the corresponding manager roles. See
[Configuration](docs/configuration.md#interstep-manager-supervision) for the
safe opt-in example, and [Runtime Behavior](docs/runtime-behavior.md#interstep-manager-supervision)
for lifecycle and artifact details.

Minimal plan:

```md
# Plan

### [ ] Checkpoint 1: Wire The CLI
- [ ] add the command entrypoint
- [ ] cover it with tests

### [ ] Checkpoint 2: Update Docs
- [ ] document the final behavior
```

## Documentation

- [Installation and Skills](docs/installation.md) - package install, first-run config bootstrap, and bundled skill installation.
- [CLI Usage](docs/cli-usage.md) - `run`, `analyze`, `show`, resume, startup prompts, plan format, and harness adapters.
- [Configuration](docs/configuration.md) - TOML layout, workflows, manager supervision, roles, teams, lifecycle settings, prompts, and recovery rules.
- [Runtime Behavior](docs/runtime-behavior.md) - lifecycle setup/teardown, manager gates, worktrees, retries, artifacts, live status, and success reporting.
- [Library API](docs/library-api.md) - public Python startup, execution, events, and analysis API.
- [Remote App](docs/remote-app.md) - FastAPI/React app for projects, Codex threads, plan drafts, executions, transcription, and API endpoints.
- [Architecture](ARCHITECTURE.md) - module-level implementation notes and data flow.

## Remote App

A separate mobile-first web application is available under `apps/aflow_app/` for remote workflow management. It provides project discovery, Codex thread reuse for plan creation, plan draft save/load/promote flows, real-time execution monitoring via SSE, and optional audio transcription for voice input.

The remote app is not included in the published `aworkflow` wheel. It is designed for authenticated desktop-hosted local/LAN use.

See [Remote App](docs/remote-app.md) and [apps/aflow_app/README.md](apps/aflow_app/README.md).

## Shipped Workflows

The bundled config includes these ready-to-use workflows:

- `ralph` - single-step implementation loop, no review.
- `ralph_jr` - `ralph` with the `7teen` team, branch-only lifecycle, and a custom `merge_prompt`.
- `review_implement_review` - review, implement, then review again with `aflow-review-squash`; approval squashes post-handoff commits into one final commit.
- `review_implement_cp_review` - checkpoint-scoped review with `aflow-review-checkpoint` and a final no-squash audit with `aflow-review-final`.
- `hard` - alias for `review_implement_cp_review`.
- `jr` - alias for `review_implement_cp_review` with the `7teen` team.

All workflows except `ralph_jr` inherit the worktree+branch lifecycle from the `[workflow]` defaults table. The lifecycle creates a local feature branch and linked worktree before the first step, then invokes a local merge handoff after successful completion.
