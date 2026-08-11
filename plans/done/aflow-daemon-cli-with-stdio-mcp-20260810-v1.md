# Lightweight aflow daemon CLI with stdio MCP transport

## Summary

Add an `aflow daemon` subcommand that runs a long-lived per-project idle loop: it listens for `start_run` / `resume_run` / `control_run` / `owner_stop` requests over MCP (stdio by default, optional HTTP), launches aflow workflow runs as local subprocesses, and reconciles their state on a configurable poll interval — without requiring the production `aflowd` systemd deployment or the FastAPI web app.

## Git Tracking

- Plan Branch: `codex/finish-daemon-stdio-mcp-20260811`
- Pre-Handoff Base HEAD: `a87c1441947f643d3269f26caa9f53c494d3843b`

## Scope

- Now: `aflow daemon start [--mcp-transport stdio|http] [--mcp-port N] [--poll-interval S] [--foreground]`, `aflow daemon status`, `aflow daemon stop`; a `SubprocessUnitManager` adapter alongside the existing `SystemdUnitManager` and `InMemoryUnitManager`; a shared MCP tool registry (`aflow/mcp_control_plane.py`) consumed by both the CLI stdio transport and the FastAPI `/mcp` mount; `[daemon]` config section in `aflow.toml`.
- Later / Out of scope: `aflow daemon` without the FastMCP dependency (currently requires `fastmcp`; could be a lightweight json-rpc parser in a future change), removing the `aflowd` systemd deployment (it remains the production path), exposing the web dashboard or REST API from the CLI, `auto_start_projects` in `[daemon]`, email/notification integration, the ChatGPT guard skill integration.

## Done Means

`aflow daemon start --foreground` starts, prints a ready banner on stderr, enters the poll loop, and accepts a `start_run` MCP call over stdio. The call spawns `aflow daemon-worker --repo-root ... --config ... --run-id ...` as a subprocess. `aflow daemon status` reports owned runs. `aflow daemon stop` terminates the daemon and drains children. The existing `aflowd`, `aflow run`, `aflow install-skills`, `aflow analyze`, and `aflow show` commands are unaffected. The FastAPI `/mcp` endpoint uses the extracted shared registry but exposes the identical tool set.

### [x] Checkpoint 1: `aflow daemon` CLI with stdio MCP and subprocess unit adapter

**Scope:**

- Change: `aflow/config.py` (+`DaemonUserConfig` section), `aflow/control_plane/units.py` (+`SubprocessUnitManager`), `aflow/mcp_control_plane.py` (new, extracted from `apps/aflow_app/server/.../mcp_adapter.py`), `apps/aflow_app/server/.../mcp_adapter.py` (reduced to re-export), `aflow/cli.py` (+`aflow daemon` subcommand group), `aflow/daemon.py` (minor constructor or factory for subprocess units).
- Preserve: existing `SystemdUnitManager` contract and systemd‑run behavior; existing `InMemoryUnitManager` test fake; the full FastAPI `/mcp` tool surface (read/write tools, error codes, idempotency, revision CAS); existing `aflowd` entrypoint and all four user-facing CLI subcommands; the `DaemonConfig` dataclass field set and `DaemonController.serve_forever` loop.
- Exclude: changes to the web dashboard, REST API routes, SSE streaming, or deployment scripts; changes to `aflow-guard-development-run`; new dependencies beyond `fastmcp` (already present in the project).

**Steps:**

- [x] **`[daemon]` config section** — add `DaemonUserConfig` dataclass with `mcp_transport: Literal["stdio","http"] = "stdio"`, `mcp_port: int = 8765`, `poll_interval_seconds: float = 1.0`, `stop_timeout_seconds: float = 30.0`; register `"daemon"` as an allowed top-level key in `_parse_workflow_user_config`; add validation: `mcp_transport` must be `stdio` or `http`, `mcp_port` must be 1–65535, `poll_interval_seconds` > 0, `stop_timeout_seconds` ≥ 0. The config section is optional; absent → defaults.
- [x] **`SubprocessUnitManager`** — implement the `UnitManager` Protocol in `aflow/control_plane/units.py`. On `start(...)`: construct the `aflow daemon-worker --repo-root ... --config ... --run-id ...` argv, launch via `subprocess.Popen` with `start_new_session=True` (process-group ownership), record the `UnitState(name, active_state="active", sub_state="running", main_pid=process.pid)`. On `stop(name)`: send `SIGTERM` to the process group, wait for termination up to the configured `stop_timeout_seconds`, escalate to `SIGKILL` on timeout, return `inactive` state. On `get(name)`: report from the exact owned `Popen`; daemon shutdown drains and reaps all owned children.
- [x] **Extract `aflow/mcp_control_plane.py`** — move `create_control_plane_mcp()`, tool registrations, `_tool_result`, `_resource_result`, `_public_error_code`, `_bounded_limit`, `_reject_credential_arguments`, `_start_response`, and the annotation constants from `apps/aflow_app/server/.../mcp_adapter.py` into a new module `aflow/mcp_control_plane.py`. The function signature receives a `ControlPlaneServiceGetter` callable; remove the `fastmcp` import from the server app and re-export `create_control_plane_mcp` from `mcp_control_plane.py` through the adapter module. Keep the `apps/.../mcp_adapter.py` file as a thin compatibility re-export.
- [x] **`aflow daemon` CLI group** — add `start`, `status`, and `stop`; bind stdio to the foreground main thread, bind optional streamable HTTP to loopback, use an atomic mode-0600 process-birth pidfile, report only direct owned workers, and drain children on EOF/signals/stop. Detached HTTP waits for exact child pidfile ownership and bounded liveness before reporting success.
- [x] **Wire the existing `daemon-worker` path** — the local subprocess adapter uses the unchanged internal `daemon-worker` entry point; installed-entrypoint proof started one exact worker and observed it through daemon status.

**Verification:**

- Run: `uv run pytest -q tests/test_config.py -k daemon`
- Observe: `16 passed, 105 deselected`; defaults, overrides, finite numeric boundaries, unknown keys, and split-config preservation pass.
- Run: `uv run pytest -q tests/test_daemon_cli.py tests/test_systemd_units.py tests/test_control_plane_reconciliation.py tests/test_control_plane_services.py`
- Observe: `19 passed`; exact subprocess groups are drained/reaped even after a worker-leader crash, pidfile identity rejects reuse, and FastMCP stdio initializes, lists tools, calls capabilities, and exits on EOF.
- Run: `uv run python -c 'import asyncio; ... asyncio.run(mcp.list_tools())'`
- Observe: the public async FastMCP API reports exactly 13 tools; the app-server parity test compares their schemas and annotations plus all three resources.
- Run: `cd apps/aflow_app/server && uv run --extra dev pytest -q`
- Observe: `188 passed, 3 warnings`; REST/FastAPI MCP authorization, idempotency, CAS, SSE, and safe errors remain green.
- Run: `uv run pytest -q` in the candidate and clean `a87c144` worktrees.
- Observe: candidate `1231 passed, 180 subtests, 4 failed`; baseline `1206 passed, 180 subtests, 4 failed`, with the same four nodes and reasons.
- Run: installed `/root/.local/bin/aflow` through a FastMCP stdio client and detached loopback HTTP proof.
- Observe: 13 tools, successful capabilities, stdio run `20260811t183735z-72bca0a7`, one exact owned child, EOF drain with no pidfile/process, and HTTP status/stop cleanup. Redacted evidence: `/root/code/evidence/finish-daemon-stdio-mcp-20260811`.
- Run: current-release p100 REST/MCP, daemon-restart, idempotency, and startup-question acceptance.
- Observe: release `878da7c3e9ea018d1a5b17bce7e3fee98170711e`; marker run `20260811t191155z-af36c819` and startup run `20260811t192812z-ad9be518` completed with ordered REST/MCP parity.
- Run: `uv run pytest -q tests/test_control_plane_repository.py tests/test_control_plane_reconciliation.py tests/test_aflowd.py tests/test_control_plane_resume.py`.
- Observe: `23 passed`; durable terminal metadata now supersedes an older running reconciliation after daemon restart.

**Done When:**

- `aflow daemon start --foreground --mcp-transport stdio` starts, prints a ready message on stderr, and services MCP read tools.
- `aflow daemon status` reports at least the running daemon process and zero owned runs immediately after start.
- `aflow daemon stop` terminates a foreground daemon within `stop_timeout_seconds`; children are drained.
- `SubprocessUnitManager` passes the `UnitManager` Protocol contract as verified by the existing `InMemoryUnitManager` test pattern.
- The FastAPI `/mcp` endpoint still exposes the identical tool set after the extraction.
- No existing test regresses beyond the pre-existing environment failures.
