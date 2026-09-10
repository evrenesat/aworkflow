# AFlow Package Guidance

- `aflow` is interactive-first.
- Harness execution normally runs in a dedicated VM. Prefer each harness's
  YOLO/full-access mode for unattended work; preserve access to the host's
  workflow artifacts and shared external state (including `/tmp`).
- Startup step picking and startup recovery can require a TTY.
- If a new startup flow would need interactive input, do not invent a non-interactive fallback.
- Treat the plan file on disk as the source of truth for startup and retry behavior.
- For AFlow development, install the intended checkout with `uv tool install -e . --force`, then exercise the installed `aflow` entry point. Never use `uv run aflow`; reserve `uv run` for tests, linters, and other project-scoped development commands.

- `aflow ui` owns the UI process lifecycle: foreground/`--daemon` ownership
  records live under `~/.config/aflow/ui/`, and `--stop` signals only the
  recorded UI process. The private `aflow ui-worker` wrapper owns one
  workflow process group and writes receipts under the run's durable
  `units/` directory; its `shutdown` contract with `PersistentUnitManager`
  is a no-op by design, so UI shutdown never signals workflow subprocesses.
- Run configuration is loaded from the current selected source at reservation,
  startup preparation, worker boot, and resume. Persist the source path and
  whether team, max-turn, and start-step choices were explicit. A
  `.aflow/runs/<run_id>/config/` copy and frozen fingerprint are optional
  compatibility diagnostics only: they are never required for admission and
  never replace the current source. Revalidate the workflow and selected step
  before a worker starts, and preserve exact run, project, plan, unit,
  idempotency, continuation, and lifecycle identity checks.

- The detached wrapper owns bounded redacted stdout/stderr tails and final exit receipts. Drain both pipes concurrently, retain stream identity, and keep early exception records nonce-bound. Never redirect actual worker diagnostics to DEVNULL or put raw output into public events.

- Manager evidence belongs to the primary repository's run directory, which may
  differ from its execution worktree. Schema-v3 context declares both artifact
  roots; preserve repository-relative versus run-relative reference semantics.

- Schema-v3 manager prompts use compact UTF-8 JSON under the existing 40 KiB
  hard limit. Formatting must not consume the evidence budget; retain all fields
  and reject genuinely oversized payloads before provider launch.

- The canonical skill store (`~/.config/aflow/skills/`, resolved through the
  executing account's `Path.home()`) is owned by `aflow/skill_store.py`; the
  bundled registry lives in `aflow/skill_catalog.py`. Reads are pure and must
  never create, initialize, refresh, or reinstall anything. The effective
  `SKILL.md` is saved canonical bytes when valid, otherwise the package
  resource; a malformed canonical document is an error, never a silent
  fallback. Saves are SHA-256-revision compare-and-swap under the single
  store lock in `.metadata/`, which sits outside every installed skill
  directory alongside version-1 baseline metadata. Saves materialize the
  complete bundled tree on first initialization only; afterwards they replace
  `SKILL.md` atomically and never touch supporting files or the recorded
  package baseline. Never edit package resources or a repository checkout on
  behalf of a store request, and never traverse a symlinked store entry.
  Manager prompt builders (`aflow/manager.py`) read the configured skill's
  validated Markdown body live once per invocation and pass it as the system
  instruction; Python contributes only structured runtime data, and skill
  failures become prelaunch failures before any provider starts.
