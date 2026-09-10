# CLI Usage

## UI

```bash
aflow ui                     # foreground; Ctrl+C stops the UI server only
aflow ui --daemon            # detached; waits until the HTTP server is ready
aflow ui --status            # report the server and its log location
aflow ui --stop              # stop the server; workflow units keep running
aflow ui --host H --port P   # override [server] bind settings for this process
```

The first launch configures the shared credential and projects root into
`~/.config/aflow/config.toml`. Without a terminal, missing settings produce
exact configuration instructions and a nonzero exit. `--daemon`, `--status`,
and `--stop` are mutually exclusive; a second start reports the existing
server and exits successfully. A port held by another program is an
actionable error.

Workflows launched by the UI run as detached worker units that survive UI
shutdown and reconnect on restart; each run freezes its configuration at
launch (see docs/runtime-behavior.md).

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
aflow run --team TEAM_NAME path/to/plan.md
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
aflow run -p path/to/plan.md -w workflow_name -ss 2 -t TEAM_NAME -mt 10
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
- `--run-id RUN_ID` supplies a canonical identity already reserved by a
  control-plane caller. Normal interactive CLI runs should let AFlow allocate
  the run id.
- `--resume [RUN_ID]` forces resume mode.
- A plan is optional in resume mode. When omitted, the selected run's saved
  original plan and invocation identity are reused; repeated values must match
  that durable identity.
- `--resume-reset-scope` requires an explicit `--resume RUN_ID` and starts the
  reused lifecycle context from a fresh checkpoint scope on the invocation's
  plan.
- `--start-step` / `-ss` starts from a workflow step name or 1-based step index.

When two bare positional arguments are given, `aflow` resolves them by checking which token is an existing plan file and which token is a configured workflow name. If both tokens could match both categories, or neither can be resolved safely, the command exits with a clear ambiguity error. A single bare positional is always treated as the plan path for backward compatibility.

Extra CLI instructions after `--` are appended to the rendered step prompt.

REST and MCP starts use the same typed choices: plan path, workflow, team,
start step, max turns, bounded extra instructions, and an optional stopped-run
predecessor. Numeric start steps remain 1-based and are stored as canonical
step names. Selecting a later executable step reports the earlier executable
steps as skipped.

## Startup Prompts

If you omit `--start-step` and the plan is partly complete, `aflow` prompts you to pick a step when the workflow has more than one step.

Interactive-only startup prompts include:

- selecting a start step for partly complete plans
- recovering from an `inconsistent_checkpoint_state` parse error
- confirming dirty-worktree startup when required
- accepting implicit auto-resume for a compatible previous run

For a pristine fresh review plan, an empty or stale `Pre-Handoff Base HEAD` is
refreshed automatically to the verified current commit; it is not an
interactive question. Started, resumed, malformed, or ambiguous plans are not
silently refreshed.

When one of those prompts is needed and stdin/stdout are not TTYs, `aflow` exits with a clear error instead of guessing.

If you pass `--start-step` on a plan that is already complete, `aflow` exits with a clear error instead of ignoring the flag.

## Resume

Eligible prior runs have two resume paths in every supported lifecycle mode:

- Plain `aflow run` can offer an interactive auto-resume prompt when a compatible prior run is found.
- `aflow run --resume [RUN_ID]` makes resume mandatory. With no `RUN_ID`, `aflow` must resolve a previous run from shell-local state or fail. With a `RUN_ID`, it resumes that exact run or fails.

Resume resolves and validates the selected run before startup preparation. Its
`original_plan_path` is authoritative and must be present; `plan_path` is not a
resume fallback, and the saved plan must still be readable. A caller may repeat
the plan, workflow, team, start step, or max-turns only when the value is
compatible with the saved invocation; conflicting values fail without
creating a new run. Extra instructions use three-way resume semantics: omit
them to inherit, pass text after `--` to replace them, or pass a bare `--` to
clear them. Fresh `aflow run` invocations still require a plan.

A remote successor restart is separate from resume. It creates a fresh run
with normal startup validation and records restarted_from_run_id; the source
must be daemon-owned, explicitly owner-stopped, and have no active exact unit.
Use resume to continue the saved invocation without changing its launch
identity.

Only schema-version `2` run metadata is resumable. Older, missing, boolean,
string, and future schema values are readable for inspection but are rejected
before plan lookup, startup questions, allocation, or daemon continuation; AFlow
does not migrate or rewrite them. Current metadata must also contain the full
manager, lifecycle, frozen-configuration, hotplug, and active-scope envelope
state. Its frozen configuration identity—workflow name, canonical configuration
path, and fingerprint—must match the currently resolved workflow, roles, teams,
harness profiles, manager policy, and error-handling configuration.

Lookup order for a previous run is:

1. `.aflow/last_run_ids/<shell-id>` when a stable shell/session id is available
2. `AFLOW_LAST_RUN_ID`
3. `.aflow/last_run_id`

A prior run is resumable only when all of these are true:

- its lifecycle identity is complete: no Git identity for no-lifecycle,
  main/feature branches for branch-only, or main/feature branches plus a
  registered path for linked-worktree runs
- saved status is `failed`, `running`, or `waiting_for_valid_override`
- normally, `last_snapshot.is_complete` is not `true` and the run has not
  entered merge teardown
- the invocation still matches on repo root, workflow name, absolute plan path, effective team, selected start step, max turns, extra instructions, and lifecycle setup

The sole completed-plan exception is a failed terminal integration. It must
record a complete snapshot, `end_reason = "transition_end"`, failed merge
status and reason, and configured merge teardown. That resume retries only the
merge/teardown phase and launches no ordinary workflow harness.

If resume is accepted, `aflow` reuses the recorded lifecycle context: the
primary checkout for no-lifecycle runs, the checked-out feature branch for
branch-only runs, or the registered worktree and feature branch for
linked-worktree runs. The plan file on disk remains the source of truth for
checkpoint progress.
If the source run has a durable `starting` turn, resume retries that unfinished
workflow step rather than returning to the invocation's original
`--start-step`.

After an owner intentionally repartitions or replaces the active checkpoint,
use `--resume RUN_ID --resume-reset-scope`. This keeps the source run's
lifecycle context, including its feature branch and worktree when present, plus
its manager decision history. The source run retains its historical
implementation-attempt audit. The new run returns to the invocation's original
plan and clears its live attempt index, interrupted-step pointer, active
implementation scope, scoped stall/rejection counters, pending
notes/upgrades/boundary decisions, and stale manager-report pointer. The
explicit run id prevents an accidental reset of an implicitly selected run.

### Rehome and baseline-team continuation

A copied run can be resumed under a new absolute repository root only with an
explicit source id and a supplied replacement worktree:

```bash
aflow run --resume RUN_ID --resume-rehome-worktree /path/to/registered-worktree
```

The current primary checkout must be on the saved main branch. The replacement
must already be registered by that checkout, use the saved feature branch, and
be free of merge, rebase, cherry-pick, and revert operations. AFlow does not
create branches or worktrees and does not infer relocation for `AUTO`. Only
paths inside the recorded repository or worktree roots are remapped. The source
run, plans, manager artifacts, and envelope bytes remain unchanged; schema-v2
plan/checkpoint evidence is validated before pruning and copied into the
continuation.

The same named resume may explicitly change the future baseline team:

```bash
aflow run --resume RUN_ID --team TEAM_NAME
```

`TEAM_NAME` must be configured. The change is rejected before allocation when
`pending_manager_notes`, `pending_step_team_override`, `pending_finalized_turn`,
`pending_boundary_decision`, `pending_repartition`, hotplug state, or unapplied
owner routing state is present. The continuation records
`resumed_from_team` and `resume_team_override`; historical selectors and source
metadata are retained. Applied run-local selectors and active native sessions
are not carried into the continuation, so the target team's worker, reviewer,
and manager selectors govern future turns.

## Analyze

`aflow analyze` inspects run logs under `.aflow/runs/`.

```bash
aflow analyze <RUN_ID>
aflow analyze --repo-root path/to/repo <RUN_ID>
aflow analyze
aflow analyze --repo-root path/to/repo
aflow analyze --all
aflow analyze --all --limit 50
aflow analyze --all --include-noise
aflow analyze <RUN_ID> --manager-context lite
aflow analyze <RUN_ID> --manager-context full --turn 3
```

Single-run resolution uses the same lookup order as resume: explicit `RUN_ID`, shell-local last run id, `AFLOW_LAST_RUN_ID`, then `.aflow/last_run_id`. `--all` switches to corpus mode.

Corpus mode flags:

- `--limit N` caps the number of run directories considered (default `20`).
- `--include-noise` keeps low-signal test-noise runs instead of filtering
  them out (noise = `workflow_name == "other"` with `turns_completed == 0`
  and `end_reason == "already_complete"`).

`--manager-context lite|full` rebuilds the same read-only versioned context
that manager supervision used for a finalized workflow turn. `--turn N` selects
that turn and otherwise defaults to the latest finalized turn. These options
require single-run mode and never invoke a manager or alter run artifacts. Live
schema-v3 Lite and Full contexts keep plan and checkpoint content in declared
run-local evidence references; Full may provide richer bounded scope and
rejection evidence. For a stopped
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

## Status Output

`aflow run` writes plain, append-only status records to stderr while a workflow
executes. Each meaningful state transition, turn finalization, and the final
summary produces one deterministic `key=value` record line:

```text
aflow time=2026-09-05T12:00:00Z event=update run=20260905T120000Z-abc123 status="running turn 3" workflow=managed step=implement checkpoint=2/5 checkpoint_name="Checkpoint 2: Implement" turn=3/10 team=base role=worker:codex.default transition=review outcome=completed git="M 1, A 0, D 0 | +12/-3 | 2 commits" artifact=.aflow/runs/<run-id>/turns/0003/stdout.txt
```

Identical consecutive snapshots are deduplicated. Output is the same ordered,
copyable stream for interactive terminals and redirected logs, with no ANSI
styling, cursor movement, or keyboard capture; only startup questions remain
interactive. Display values are bounded, but durable artifact references are
never truncated, and control bytes are flattened so pasted logs stay safe.
Engine exit codes and final success/failure messages on stdout are unchanged.

## Show

`aflow show` prints workflow graphs and the role/team relationships they use as
plain ASCII text.

```bash
aflow show
aflow show review_implement_cp_review
```

With no workflow argument, it prints a shared roles/teams section followed by
every workflow in config order. With a workflow name, it prints only that
workflow plus the roles and teams that apply to it (the workflow's default team
is marked `(default)`). Each declared step is labeled `[executable]` or
`[excluded]` with words rather than color, so steps listed in
`exclude = [...]` stay visible in the declared graph. Transitions print as
`go -> <target>`, with `[terminal]` marking END and `when <condition>` shown
after conditional transitions.

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

- Checkpoint headings must start with `### [ ] Checkpoint ...` or `### [x] Checkpoint ...` (the `[X]` uppercase spelling is also accepted).
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
| `muse` | `muse exec --yolo --workspace <repo-root>` | Yes |
| `opencode` | `opencode run --format default --dir <repo-root>` | No |
| `reasonix` | `reasonix run --dir <repo-root> [--model MODEL] [--effort EFFORT]` | Yes |
| `pi` | `pi --print --tools read,bash,edit,write,grep,find,ls` | Yes |
