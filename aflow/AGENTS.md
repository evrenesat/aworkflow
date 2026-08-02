# AFlow Package Guidance

- `aflow` is interactive-first.
- Startup step picking and startup recovery can require a TTY.
- If a new startup flow would need interactive input, do not invent a non-interactive fallback.
- Treat the plan file on disk as the source of truth for startup and retry behavior.
- For AFlow development, install the intended checkout with `uv tool install -e . --force`, then exercise the installed `aflow` entry point. Never use `uv run aflow`; reserve `uv run` for tests, linters, and other project-scoped development commands.
