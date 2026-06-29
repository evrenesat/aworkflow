# Installation and Skills

## Package Install

`aworkflow` requires Python `3.11+`.

Install the published package with `uv`:

```bash
uv tool install aworkflow
```

That exposes both commands:

- `aflow`
- `aworkflow`

From a local checkout, run without installing:

```bash
uv run python -m aflow run path/to/plan.md
```

## First Run

`aflow` reads `~/.config/aflow/aflow.toml` and a sibling `workflows.toml`.

If the files do not exist, `aflow` copies the packaged defaults into place, prints both paths, and exits. That happens even if you run bare `aflow` with no subcommand, so you can edit the generated files before the first real workflow run.

Example:

```bash
aflow run path/to/plan.md
# Config bootstrapped at ~/.config/aflow/aflow.toml
# Review the copied config files and adjust them if needed, then run again
```

## Install Bundled Skills

`aflow install-skills` copies the default bundled skills, including `aflow-harness-recovery-lead`, into harness skill directories. The optional `aflow-assistant` skill is not installed unless you ask for it.

Auto mode:

```bash
aflow install-skills
```

Manual destination:

```bash
aflow install-skills ~/.claude/skills
```

Selection flags:

- `--include-optional` installs the default bundled skills plus optional bundled skills, including `aflow-assistant`.
- `--only SKILL` installs exactly the named skill. It can be repeated and does not include the default set unless you name each skill explicitly.

Auto-install destination map:

| Harness | Destination |
|---------|-------------|
| `codex` | `~/.agents/skills` |
| `copilot` | `~/.agents/skills` |
| `gemini` | `~/.agents/skills` |
| `pi` | `~/.agents/skills` |
| `kiro` | `~/.kiro/skills` |
| `opencode` | `~/.config/opencode/skills` |
| `claude` | `~/.claude/skills` |

## Bundled Skill Inventory

Default skills:

- `aflow-plan` - create a checkpoint handoff plan.
- `aflow-execute-plan` - execute an entire plan autonomously.
- `aflow-execute-checkpoint` - execute exactly one checkpoint.
- `aflow-review-squash` - review completed work, approve and squash, or create a fix plan.
- `aflow-review-checkpoint` - review one checkpoint, approve, or create a fix plan.
- `aflow-review-final` - final review without squash.
- `aflow-merge` - local-only merge handoff.
- `aflow-init-repo` - pre-lifecycle bootstrap for empty repositories.
- `aflow-harness-recovery-lead` - team-lead fallback for harness recovery decisions.

Optional skills:

- `aflow-assistant` - setup help, AFlow concepts, and evidence-first run debugging.
