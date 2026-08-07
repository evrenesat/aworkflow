# CLI Usage

## Run

Positional forms:

```bash
aflow run path/to/plan.md
aflow run workflow_name path/to/plan.md
aflow run path/to/plan.md workflow_name
aflow run --resume
aflow run --resume 20260407T120000Z-abc123
aflow run --start-step implement_plan path/to/plan.md
aflow run -ss 2 path/to/plan.md
aflow run --team 7teen path/to/plan.md
aflow run -mt 10 path/to/plan.md
aflow run path/to/plan.md -- keep edits small and update docs if behavior changes
```

Explicit flag forms:

```bash
aflow run --plan path/to/plan.md
aflow run -p path/to/plan.md -w workflow_name
aflow run --resume -p path/to/plan.md -w workflow_name
aflow run --resume 20260407T120000Z-abc123 -p path/to/plan.md -w workflow_name
aflow run --plan path/to/plan.md --workflow workflow_name --start-step implement_plan
aflow run -p path/to/plan.md -w workflow_name -ss 2 -t 7teen -mt 10
```

Mixed forms:

```bash
aflow run -p path/to/plan.md workflow_name
aflow run --workflow workflow_name path/to/plan.md
```

If the workflow name is omitted, `aflow` uses `aflow.default_workflow` from config.

Important flags:

- `--plan` / `-p` specifies the plan file path.
- `--workflow` / `-w` specifies the workflow name.
- `--team` / `-t` selects a team and overrides any team set in the workflow config.
- `--max-turns` / `-mt` overrides `[aflow].max_turns` for that invocation.
- `--resume [RUN_ID]` forces resume mode.
- A plan is optional in resume mode. When omitted, the selected run's saved
  original plan and invocation identity are reused; repeated values must match
  that durable identity.
- `--resume-reset-scope` requires an explicit `--resume RUN_ID` and starts the
  reused worktree from a fresh checkpoint scope on the invocation's plan.
- `--start-step` / `-ss` starts from a workflow step name or 1-based step index.

When two bare positional arguments are given, `aflow` resolves them by checking which token is an existing plan file and which token is a configured workflow name. If both tokens could match both categories, or neither can be resolved safely, the command exits with a clear ambiguity error. A single bare positional is always treated as the plan path for backward compatibility.

Extra CLI instructions after `--` are appended to the rendered step prompt.

## Startup Prompts

If you omit `--start-step` and the plan is partly complete, `aflow` prompts you to pick a step when the workflow has more than one step.

Interactive-only startup prompts include:

- selecting a start step for partly complete plans
- refreshing stale `Pre-Handoff Base HEAD` metadata on pristine handoffs
- recovering from an `inconsistent_checkpoint_state` parse error
- confirming dirty-worktree startup when required
- accepting implicit auto-resume for a previous worktree run

When one of those prompts is needed and stdin/stdout are not TTYs, `aflow` exits with a clear error instead of guessing.

If you pass `--start-step` on a plan that is already complete, `aflow` exits with a clear error instead of ignoring the flag.

## Resume

Worktree workflows have two resume paths:

- Plain `aflow run` can offer an interactive auto-resume prompt when a compatible prior run is found.
- `aflow run --resume [RUN_ID]` makes resume mandatory. With no `RUN_ID`, `aflow` must resolve a previous run from shell-local state or fail. With a `RUN_ID`, it resumes that exact run or fails.

Resume resolves and validates the selected run before startup preparation. Its
`original_plan_path` is authoritative, with `plan_path` accepted only for
legacy metadata, and the saved plan must still be readable. A caller may repeat
the plan, workflow, team, start step, max-turns, or extra instructions only
when the value is compatible with the saved invocation; conflicting values
fail without creating a new run. Fresh `aflow run` invocations still require a
plan.

Modern schema-versioned run metadata also records a frozen configuration
identity. Resume requires its workflow name, canonical configuration path, and
fingerprint to match the currently resolved workflow, roles, teams, harness
profiles, manager policy, and error-handling configuration. This check happens
before startup questions or new run state. Schema-less legacy metadata without
`frozen_config` remains resumable under the older scalar and lifecycle checks.

Lookup order for a previous run is:

1. `.aflow/last_run_ids/<shell-id>` when a stable shell/session id is available
2. `AFLOW_LAST_RUN_ID`
3. `.aflow/last_run_id`

A prior run is resumable only when all of these are true:

- the run used a worktree lifecycle and recorded a feature branch plus worktree path
- saved status is `failed` or `running`
- `last_snapshot.is_complete` is not `true`
- the run did not already enter merge teardown
- the invocation still matches on repo root, workflow name, absolute plan path, effective team, selected start step, max turns, extra instructions, and lifecycle setup

If resume is accepted, `aflow` reuses the recorded feature branch and worktree path. The plan file on disk remains the source of truth for checkpoint progress.
If the source run has a durable `starting` turn, resume retries that unfinished
workflow step rather than returning to the invocation's original
`--start-step`.

After an owner intentionally repartitions or replaces the active checkpoint,
use `--resume RUN_ID --resume-reset-scope`. This keeps the source run's
worktree, branch, lifecycle commands, and manager decision history, while the
source run retains its historical implementation-attempt audit. The new run
returns to the invocation's original plan and clears its live attempt index,
interrupted-step pointer, active implementation scope, scoped stall/rejection
counters, pending notes/upgrades/boundary decisions, and stale manager-report
pointer. The explicit run id prevents an accidental reset of an implicitly
selected run.

## Analyze

`aflow analyze` inspects run logs under `.aflow/runs/`.

```bash
aflow analyze <RUN_ID>
aflow analyze --repo-root path/to/repo <RUN_ID>
aflow analyze
aflow analyze --repo-root path/to/repo
aflow analyze --all
aflow analyze <RUN_ID> --manager-context lite
aflow analyze <RUN_ID> --manager-context full --turn 3
```

Single-run resolution uses the same lookup order as resume: explicit `RUN_ID`, shell-local last run id, `AFLOW_LAST_RUN_ID`, then `.aflow/last_run_id`. `--all` switches to corpus mode.

`--manager-context lite|full` rebuilds the same read-only versioned context
that manager supervision used for a finalized workflow turn. `--turn N` selects
that turn and otherwise defaults to the latest finalized turn. These options
require single-run mode and never invoke a manager or alter run artifacts. Lite
excludes plan prose; Full includes the complete active-plan body. For a stopped
run, read `.aflow/runs/<RUN_ID>/manager-report.md` first; it is designed to
explain the incident without requiring raw logs.

## Harness launch troubleshooting

A real launch can stop before any normal turn or manager artifact when local
environment preflight finds a prerequisite problem:

| Reason code | Meaning | Owner action |
| --- | --- | --- |
| harness_executable_missing | The selected argv[0] is not executable on the invocation PATH. | Install the trusted package that provides it and verify it in the AFlow environment. |
| reasonix_sandbox_bwrap_missing | Reasonix reports enforced bash sandboxing, but bwrap is not executable on that PATH. | Install the trusted host package that provides bubblewrap, then verify bwrap. |

The failure is recorded at run level with no synthetic turn, manager decision,
recovery callback, or repartition attempt. Earlier artifacts remain intact.
After remediating the environment, explicitly resume the exact run:

    aflow run --resume RUN_ID

This recovery applies to no-lifecycle, branch-only, and linked-worktree
workflows. No-lifecycle runs resume in the primary repository; branch-only runs
require the recorded feature branch to remain checked out for an ordinary
resume; linked-worktree runs retain strict path and registration validation.

AFlow reports these conditions; it does not install packages, weaken sandbox
settings, repair configuration, or validate authentication, network reachability,
quota, provider health, model availability, or arbitrary dependency health.
The guardian remains the fallback for older runs and unanticipated failures
outside the safe preflight contract.

## Show

`aflow show` prints workflow diagrams and the role/team relationships they use.

```bash
aflow show
aflow show review_implement_cp_review
```

With no workflow argument, it prints a shared roles/teams section followed by every workflow in config order. With a workflow name, it prints only that workflow plus the roles and teams that apply to it. Steps listed in `exclude = [...]` stay visible in gray because `aflow show` uses the declared graph, not only the executable step map.

## Plan Format

`aflow` reads a Markdown plan from disk and derives progress from checkpoint headings plus unchecked task items inside each checkpoint.

```md
# Plan

### [ ] Checkpoint 1: Wire The CLI
- [ ] add the command entrypoint
- [ ] cover it with tests

### [ ] Checkpoint 2: Update Docs
- [ ] document the final behavior
```

Parser rules:

- Checkpoint headings must start with `### [ ] Checkpoint ...` or `### [x] Checkpoint ...`.
- Only task items under a checkpoint section count toward that checkpoint's remaining work.
- A checked checkpoint heading cannot contain unchecked task items.
- If no checkpoint sections are found, the run fails before starting.

## Harnesses

`aflow` expects provider CLIs to already be installed and authenticated. It does not manage provider auth or SDK setup.

Supported harness adapters:

| Harness | CLI behavior | Effort support |
|---------|--------------|----------------|
| `codex` | `codex exec --dangerously-bypass-approvals-and-sandbox` | Yes |
| `claude` | `claude -p --permission-mode bypassPermissions --dangerously-skip-permissions` | Yes |
| `copilot` | `copilot -p ... -s --allow-all --no-ask-user` | Yes |
| `gemini` | `gemini --prompt ... --approval-mode yolo --sandbox=false` | No |
| `kiro` | `kiro-cli chat --no-interactive --trust-all-tools` | No |
| `opencode` | `opencode run --format default --dir <repo-root>` | No |
| `reasonix` | `reasonix run --dir <repo-root> [--model MODEL]` | No |
| `pi` | `pi --print --tools read,bash,edit,write,grep,find,ls` | Yes |
