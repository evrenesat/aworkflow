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
- Run configuration is frozen at reservation into
  `.aflow/runs/<run_id>/config/`; never route worker, retry, or resume loads
  around the snapshot, and never let a partial snapshot launch a worker.

- The detached wrapper owns bounded redacted stdout/stderr tails and final exit receipts. Drain both pipes concurrently, retain stream identity, and keep early exception records nonce-bound. Never redirect actual worker diagnostics to DEVNULL or put raw output into public events.

- Manager evidence belongs to the primary repository's run directory, which may
  differ from its execution worktree. Schema-v3 context declares both artifact
  roots; preserve repository-relative versus run-relative reference semantics.

- Schema-v3 manager prompts use compact UTF-8 JSON under the existing 40 KiB
  hard limit. Formatting must not consume the evidence budget; retain all fields
  and reject genuinely oversized payloads before provider launch.
