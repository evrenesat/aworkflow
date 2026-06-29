# CLI Usage

## Run

Positional forms:

```bash
aflow run path/to/plan.md
aflow run workflow_name path/to/plan.md
aflow run path/to/plan.md workflow_name
aflow run --resume path/to/plan.md
aflow run --resume 20260407T120000Z-abc123 path/to/plan.md
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

## Analyze

`aflow analyze` inspects run logs under `.aflow/runs/`.

```bash
aflow analyze <RUN_ID>
aflow analyze --repo-root path/to/repo <RUN_ID>
aflow analyze
aflow analyze --repo-root path/to/repo
aflow analyze --all
```

Single-run resolution uses the same lookup order as resume: explicit `RUN_ID`, shell-local last run id, `AFLOW_LAST_RUN_ID`, then `.aflow/last_run_id`. `--all` switches to corpus mode.

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
| `reasonix` | `reasonix run -dir <repo-root> [--model MODEL]` | No |
| `pi` | `pi --print --tools read,bash,edit,write,grep,find,ls` | Yes |
