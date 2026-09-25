---
name: aflow-assistant
description: "Help interactive agents use AFlow: discover and connect its MCP tools, author checkpoint plans, select workflows and teams, launch and monitor runs, control or resume execution, and diagnose setup or run failures. Use for operating AFlow or explaining its behavior; checkpoint workers and reviewers should follow their assigned execution/review skill."
---

# AFlow Assistant

Help the user operate AFlow within their requested scope. Prefer the connected
web/MCP control plane for managed workflows and follow project-specific routing.
AFlow drives agent CLIs through checkpointed Markdown plans; a workflow step is
an engine node, a turn is one harness call, and a checkpoint is reviewable plan
work. Resuming creates a successor run, not a second controller for the old run.

## Choose the relevant guide

- **Connect, discover, plan, launch, monitor, control, or resume through MCP:**
  read [MCP operations](references/mcp-operations.md). It covers the current
  UI-hosted interface, argument shapes, startup questions, retry/revision
  contracts, and end-to-end operating procedures.
- **Diagnose a failed, stalled, or confusing run:**
  get canonical run state and lite context first for managed runs. Read
  [run troubleshooting](references/run-troubleshooting.md) for targeted local
  artifact inspection, analyzer commands, failure classification, and source
  lookup when needed.
- **Explain architecture or locate durable evidence without a checkout:**
  read [engine map](references/engine-map.md).
- **Explain configuration, roles/teams, workflow transitions, sessions,
  hotplug, manager supervision, plan parsing, or CLI behavior:**
  search the relevant section of [engine features](references/engine-features.md).
  Use live tool schemas and current configuration for deployment-specific choices.

Read only the resources needed for the task. All references travel with this
skill. Source-code access is optional; do not direct users to nonexistent local
repository files.

## Operating rules

1. Establish the actual host, registered project, exact plan/run, user outcome,
   and authorization. Read applicable project instructions before local work.
   Discover available MCP tools; do not invent a missing tool or assume the
   connected deployment matches this skill's version.
2. Use existing user choices for workflow, team, budget, and stop behavior.
   Ask only for missing material decisions. An authorized start/control request
   does not need another conversational confirmation unless required by the
   actual client or project policy; honor any enforced tool approval boundary.
   Reading status does not authorize starting, stopping, or changing a run.
3. Inspect current configuration and resolved lifecycle behavior. Use exact
   configured IDs. Prefer launch options for one-run choices; global settings
   affect other projects and active runs at applicable boundaries.
4. Keep one controller per logical run. Check existing state before launch or
   resume; never run a parallel CLI recovery against a managed active worker.
   Preserve plan files, unrelated dirtiness, managed worktrees, receipts, and
   lineage. Do not hand-edit durable state to bypass an admission failure.
5. Use returned revisions and retain lifecycle idempotency keys. A timeout
   is an uncertain outcome, not permission to submit a duplicate start.
   Reconcile stale edits with current content and bound retries.
6. Treat the plan as checkpoint progress authority and canonical managed state
   as operational authority. A transcript claim or exit code alone is not
   completion evidence. Verify implementation, review, publication, CI, and
   activation separately when required by the task.
7. Monitoring is read-only unless further actions are authorized. Prefer
   incremental events or bounded checks and report meaningful changes. Use an
   explicitly requested guard skill only when available; ordinary operation
   does not automatically create a guardian, scheduler, or recovery loop.

## Typical interactive journey

1. Discover tools and registered projects, inspect project capabilities, and
   read current global configuration to select an existing workflow/team.
2. Identify a ready plan or author a todo draft with clear scope, invariants,
   checkpoint tasks, verification commands, and observable acceptance. If
   available, use `aflow-plan` for its full checkpoint handoff contract.
3. Read and revise the draft, then promote it to `in_progress`. Resolve its
   returned lifecycle path. Promotion may already trigger the server's default-on
   plan consumer: inspect current queue claims and runs before any manual start,
   then preflight the exact plan only if it remains unclaimed.
4. If a manual start is still needed, start once with a retained idempotency
   key. Handle the returned startup question, if any, using `answer_startup`;
   a reserved run is not yet proof that a worker started.
5. Follow the returned run ID with state, incremental events, and lite context.
   Resolve failures using evidence. Apply authorized control or resume through
   the managed interface and track a successor's new ID.
6. Verify terminal results and any required delivery gates. Report the exact
   outcome and remaining blocker, with concise evidence links.

## Local setup and CLI fallback

Use local CLI operations when that is the user's selected interface or needed
for setup/debugging and allowed by project guidance. An unavailable MCP
connection does not itself justify launching a duplicate CLI workflow.

- Inspect `aflow --help`, `aflow show`, or `aflow show <workflow>` to check
  installed commands and configuration. These are not provider-health checks.
- `aflow ui` owns dashboard/MCP server lifecycle; `aflow ui --status` reads
  its ownership state. Starting/stopping that service is distinct from run
  control. Do not revive obsolete standalone daemon/MCP launch instructions.
- For an authorized direct run, use the installed `aflow run` entry point
  with the selected plan/workflow/team. Startup may require a TTY; do not
  fabricate answers or assume a noninteractive fallback. Follow project rules
  for editable installs; do not repoint a shared AFlow installation while
  another controller depends on it.
- For local triage, prefer `aflow analyze --repo-root <repo> <run-id>`.
  Without an explicit ID, selection follows shell/environment/last-run records
  before falling back to the latest substantive run. Always report the run
  actually selected. Use `--all` only for requested cross-run analysis.

This skill is optional in the bundled catalog. To install just it under the
executing account's shared agent directory from an up-to-date AFlow package:

```bash
aflow install-skills ~/.agents/skills --only aflow-assistant --yes
```

Installation explicitly refreshes the selected canonical skill under
`~/.config/aflow/skills/` and links it into `~/.agents/skills/`. It preserves
locally edited or unknown-baseline canonical trees, so successful installation
alone does not prove those bytes were updated. Inspect the refresh result and
the resolved skill tree. Preserve local edits before reconciling them; do not
delete a customized store entry merely to make the install appear current.
The skill's presence does not configure or authenticate an MCP connection.

## Response contract

Lead with the observed outcome or next required decision. Include the exact
project/run and successor identity where relevant. Distinguish verified facts
from inference, reservation from execution, requested control from applied
control, and local approval from delivery. Link concise evidence rather than
dumping logs. State unavailable tools or access plainly and continue independent
authorized work; do not imply actions succeeded when only instructions were
prepared.
