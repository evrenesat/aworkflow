# Installation and Skills

## Package Install

`aworkflow` requires Python `3.11+`.

Install the published package with `uv`:

```bash
uv tool install aworkflow
```

That exposes two equivalent workflow commands:

- `aflow`
- `aworkflow`

It also installs `aflowd`, the optional durable control-plane entry point used
by systemd-backed remote deployments. Most users should start with `aflow`.

From a local checkout, install the checkout as the editable tool used for
normal operation:

```bash
uv tool install -e . --force
aflow --help
```

## Web UI

The wheel bundles the compiled web UI, so `aflow ui` works with no Node/npm
and no extra installation step:

```bash
uv tool install aworkflow
cd /any/working/directory
aflow ui          # first run asks for a password and the projects root
```

First-run setup writes `~/.config/aflow/config.toml` (mode 0600) with the
shared credential, an explicit `bind_host = "0.0.0.0"`, and the projects
root. Later launches need no prompts; `aflow ui --daemon` returns once the
HTTP server is accepting requests. Editable development installs build the
web assets automatically from `apps/aflow_app/web` when their sources change.

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

`aflow install-skills` refreshes the default bundled skills, including
`aflow-harness-recovery-lead`, the read-only `aflow-manager`, and the strict
read-only `aflow-repartition-checkpoint`, plus `material-code-review`, in the
canonical skill store and links them into every detected supported harness
skill directory. Each link is an absolute directory symlink to
`~/.config/aflow/skills/<name>`, so a saved skill edit is immediately visible
through every linked harness without reinstalling. The default
`aflow-guard-development-run` skill launches new
legacy runs in tmux, then attaches one observer-only 30-minute heartbeat to the
task that requested supervision. It stays silent while healthy, never repairs
or steers implementation, audits the terminal result, and then stops. Remote
MCP use is read-only and ownership-aware: direct legacy controllers use the
bounded local snapshot, lightweight `aflow daemon` runs use daemon status
plus its configured MCP transport, and production `aflowd` runs use the
advertised authenticated control-plane endpoint and exact systemd unit.
The lightweight daemon has no REST or web UI; production `aflowd` may
advertise both. Deployment requires explicit per-run authorization. The
optional `aflow-assistant` skill is not installed
unless you ask for it. Keep the legacy recovery skill installed even when using
manager supervision: manager-disabled configurations retain that recovery path.
Manager and repartition prompts use the live saved skill bytes as their system
instruction, so editing a skill changes the next manager invocation. A missing
or invalid skill fails the call before any provider is invoked; saved edits
never weaken the code-enforced protocol validation.

Auto mode:

```bash
aflow install-skills
```

Manual destination:

```bash
aflow install-skills ~/.claude/skills
```

Manual destinations may not overlap the canonical skill store, and arbitrary
other paths are accepted. Existing real skill directories at a destination are
set aside and replaced by the link; unrelated destination entries are never
touched, and the legacy `~/.config/opencode/skills` copy location is not
migrated.

Selection flags:

- `--include-optional` installs the default bundled skills plus optional bundled skills, including `aflow-assistant`.
- `--only SKILL` installs exactly the named skill. It can be repeated and does not include the default set unless you name each skill explicitly.
- `--yes` skips the confirmation prompt. It is required for non-interactive
  stdin: without it, an unattended install aborts and tells you to rerun with
  `--yes`.

Auto-install destination map:

| Harness | Executable | Destination |
|---------|------------|-------------|
| `claude` | `claude` | `~/.claude/skills` |
| `kiro` | `kiro-cli` | `~/.kiro/skills` |
| `zcode` | `zcode` | `~/.zcode/skills` |
| `codex` | `codex` | `~/.agents/skills` |
| `copilot` | `copilot` | `~/.agents/skills` |
| `dsh` | `dsh` | `~/.agents/skills` |
| `gemini` | `gemini` | `~/.agents/skills` |
| `muse` | `muse` | `~/.agents/skills` |
| `opencode` | `opencode` | `~/.agents/skills` |
| `pi` | `pi` | `~/.agents/skills` |
| `reasonix` | `reasonix` | `~/.agents/skills` |

The eight harnesses that share `~/.agents/skills` are deduplicated into one
destination operation per selected skill. Repeating an installation is safe:
correct links are left alone, wrong or dangling links are replaced, and the
batch stops at the first failure with the remaining operations reported as
unattempted.

## Canonical Skill Store and Refresh Behavior

AFlow keeps canonical skill content per OS account under
`~/.config/aflow/skills/<name>/`. The first time a bundled skill is saved or
explicitly refreshed, its complete packaged directory (including references,
agents, and scripts with their permission intent) is materialized there and a
baseline of the packaged file hashes is recorded under
`~/.config/aflow/skills/.metadata/`. Reads never initialize, refresh, or
reinstall anything; the effective `SKILL.md` is the saved canonical document
when one exists and is valid, otherwise the packaged copy.

Explicit reinstallation refreshes skills through the same store:

- A skill whose files exactly match its recorded baseline is considered
  unedited. Reinstallation replaces it with the currently packaged files and
  records the new baseline, so unedited skills pick up package upgrades.
- A skill with any changed, extra, missing, or otherwise customized file is
  edited. The whole skill directory is preserved exactly as saved — both its
  `SKILL.md` and its supporting files — and the upgrade leaves it untouched.
  Reverting a skill to the exact recorded baseline makes it unedited again.
- An existing skill directory without recorded metadata is adopted only when
  it exactly matches the current package; otherwise it is preserved and
  protected with an unknown baseline rather than guessed at or overwritten.

Refreshes are explicit store operations, serialized with saves under one
lock. An interrupted refresh is detected through a store transaction marker:
reads of that skill fail with an incomplete-refresh error until the next
explicit refresh verifiably restores the prior content, so uncertain content
is never silently overwritten.

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
- `aflow-manager` - read-only Lite/Full interstep supervision.
- `aflow-repartition-checkpoint` - strict Full proposal and independent semantic validation for scope-preserving splits.
- `aflow-guard-development-run` - observer-only monitoring for an exact legacy or daemon-owned AFlow run.
- `material-code-review` - high-confidence, material-defect review guidance with a proportionate-fix gate.

Optional skills:

- `aflow-assistant` - setup help, AFlow concepts, and evidence-first run debugging.
