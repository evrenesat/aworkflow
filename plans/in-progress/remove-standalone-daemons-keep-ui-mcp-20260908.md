# Remove standalone daemon entry points; retain UI-server MCP

## Summary

Remove `aflow daemon` and the `aflowd` executable without compatibility aliases or migration commands. Keep the existing authenticated HTTP MCP endpoint in the web UI server, including all 14 tools. The endpoint already exists; this is primarily removal, dependency cleanup, verification, and documentation, not a new MCP implementation.

Preserve `aflow ui --daemon`, `--status`, `--stop`, the `aflow-app-server` entry point, the systemd deployment named `aflowd.service`, and shared workflow internals including `AflowDaemon`, `DaemonService`, and `daemon-worker`. The systemd service actually executes `aflow-app-server`.

## Git Tracking

- Plan Branch: `aflow-remove-standalone-daemons-keep-ui-mcp-20260908-20260910-131652`
- Pre-Handoff Base HEAD: `186fba177b079281d1ebaccdd65dcf54c33ff4bb`
- Review Log: Checkpoint 1 approved through `cp1 v01` on 2026-09-10. Reviewed the immediately preceding worker changes using the worktree fallback against the pre-handoff base; 87 checkpoint tests passed. Checkpoints 2–4 remain unreviewed.

## Done Means

- `aflow daemon` is an unknown command; fresh package metadata has no `aflowd` executable. Neither has a compatibility shim.
- Standalone daemon listener, lifecycle CLI, and `[daemon]` user configuration are removed. Obsolete configuration is rejected through normal unknown-section validation, not migrated or silently accepted.
- The UI server serves the existing 14 MCP tools and three resource templates at `/mcp` and `/mcp/`, with existing bearer authentication and HTTP contracts.
- UI foreground/background/status/stop behavior and independently owned workflow lifetimes are preserved.
- Deployment validation accepts a release without `bin/aflowd`, while retaining its other executable, integrity, and configuration checks.
- Documentation explains UI-server MCP setup, discovery, tool purposes, and ownership. All checkpoints and acceptance checks are verified.

## Critical Invariants

- Preserve one MCP registry in `aflow/mcp_control_plane.py`, mounted through the existing server adapter. No new listener, port, stdio bridge, or duplicate tool implementation.
- Preserve exactly: `get_capabilities`, `list_projects`, `get_project_capabilities`, `list_plans`, `list_runs`, `get_run`, `get_run_events`, `get_run_context`, `start_run`, `answer_startup`, `control_run`, `owner_stop`, `resume_run`, `preflight_run`. Preserve tool schemas, resource templates, annotations, error mapping, idempotency, revisions, and startup/resume semantics.
- MCP uses the existing server bind settings and configured token in the Authorization header. Preserve cookie-only rejection, URL/payload credential rejection, project allowlisting, contained plan paths, and bounded/redacted responses.
- Preserve the portable process-birth identity algorithm, including Linux start ticks, zombie handling, and the `ps` fallback. PID existence alone never establishes ownership.
- UI shutdown does not stop independently owned workflow workers. No manipulation of live processes, installed tools, user configuration, `.aflow` state, managed worktrees, deployed releases, or systemd units is part of this handoff.
- The accepted live-configuration implementation is now committed, including `aflow/control_plane/run_activity.py` and MCP `preflight_run`. Preserve all current live-source, dirty-worktree-preflight and explicit-choice semantics. Use a clean workflow-managed worktree under the existing primary project; preserve other active controllers and execution roots.

## Forbidden Implementations

- Deleting `aflow/daemon.py`, its shared lifecycle classes, or `daemon-worker` because they contain the word daemon.
- Removing or renaming `aflow ui --daemon`, `deploy/aflowd/`, `aflowd.service`, deployment state paths, or deployment timers.
- Leaving shared consumers importing deleted `daemon_cli.py`, or retaining that module solely as a compatibility wrapper.
- Keeping `[daemon]` as an ignored configuration table or adding a replacement MCP configuration source.
- Adding browser-cookie authentication to MCP, weakening authorization for local requests, or changing server network exposure.
- Reimplementing existing MCP tools, adding new tools/UI screens, or rewriting historical completed plans.
- Running privileged deployment scripts or reinstalling the shared editable tool to test this change against live workflows.

## Checkpoints

### [x] Checkpoint 1: Move shared process identity out of standalone CLI

**Goal:**

- Shared UI and workflow code no longer depends on standalone daemon CLI code.

**Context:**

- Run: `git rev-parse --show-toplevel` and `git status --short`.
- Inspect: `AGENTS.md`, `aflow/AGENTS.md`, `aflow/control_plane/AGENTS.md`, `aflow/daemon_cli.py`, `aflow/ui_cli.py`, `aflow/control_plane/persistent_units.py`, `aflow/control_plane/run_activity.py`, `tests/test_persistent_units.py`, `tests/test_ui_cli.py`.
- Run: `rg -n 'daemon_cli|process_birth_identity' aflow tests`.
- Preserve: current process identity return values, failure behavior, and ownership decisions.

**Scope:**

- May create `aflow/process_identity.py` and `tests/test_process_identity.py`; modify the above consumers and directly affected tests.
- Must not alter workflow state/reconciliation semantics or UI lifecycle behavior.

**Steps:**

- [x] Move `_process_birth_identity` into `aflow/process_identity.py` as `process_birth_identity`, preserving its implementation. Change the temporary standalone CLI consumer to import it under its existing private name until checkpoint 2 deletes that consumer.
- [x] Retain the existing `aflow.ui_cli.process_birth_identity` wrapper but delegate to the new helper. Update all shared production and test imports, including `run_activity.py` if present at execution time.
- [x] Add focused tests for current-process identity, nonpositive/dead PID, Linux zombie/start-tick behavior, and `ps` success/failure fallback using controlled fixtures; verify the ownership consumers still reject reused identities.

**Dependencies:** None.

**Verification:**

- Run: `uv run pytest -q tests/test_process_identity.py tests/test_ui_cli.py tests/test_persistent_units.py tests/test_worker_diagnostics.py tests/test_run_history.py tests/test_daemon_cli.py`.
- Run: `rg -n 'from aflow.daemon_cli|import aflow.daemon_cli' aflow tests`.
- Observe: remaining imports are confined to standalone daemon-specific tests; UI and shared workflow code use the new helper.

**Done When:**

- Identity and consumer tests pass with unchanged ownership behavior.
- Every completed step is validated against code, tests, or observable behavior; changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if current ownership behavior cannot be preserved or unrelated dirty files make change ownership ambiguous.

### [ ] Checkpoint 2: Remove the standalone daemon command and configuration

**Goal:**

- Remove the public standalone command, transports, and its dedicated user configuration.

**Context:**

- Inspect: `aflow/cli.py`, `aflow/config.py`, `aflow/daemon_cli.py`, `tests/test_cli.py`, `tests/test_config.py`, `tests/test_daemon_cli.py` and nearest instructions.
- Run: `rg -n 'DaemonUserConfig|_parse_daemon_config|mcp_transport|mcp_port|daemon_cli' aflow tests`.
- Preserve: `daemon-worker` parser/dispatch and UI `args.daemon` behavior.

**Scope:**

- May modify CLI/configuration and direct tests; delete `aflow/daemon_cli.py` and obsolete `tests/test_daemon_cli.py` after retaining shared coverage in checkpoint 1.
- Must not modify shared daemon services or remove dependencies still required by the UI/MCP server.

**Steps:**

- [ ] Remove `daemon` subparser and start/status/stop options and dispatch. Remove only imports rendered unused. Do not remove the `daemon-worker` subcommand.
- [ ] Delete standalone daemon implementation, including stdio/HTTP serving, single-repository routing, pidfile ownership, detach, and stop/status helpers.
- [ ] Remove `DaemonUserConfig`, its field on `WorkflowUserConfig`, parsing, top-level allowlist admission, and propagation during split-file merges. Preserve internal `DaemonConfig` and server lifecycle settings.
- [ ] Replace standalone configuration tests with normal-config loading/non-regression and `[daemon]` rejection tests for both single-file and split-file loading. Remove obsolete standalone tests; add CLI tests asserting `daemon`, `daemon start`, `daemon status`, and `daemon stop` reject with argparse status 2 without creating runtime state. Assert UI background and worker parsers remain accepted without launching them.

**Dependencies:** Checkpoint 1.

**Verification:**

- Run: `uv run pytest -q tests/test_cli.py tests/test_config.py tests/test_ui_cli.py tests/test_process_identity.py tests/test_persistent_units.py tests/test_run_config_snapshot.py`.
- Run: `rg -n 'daemon_cli|DaemonUserConfig|_parse_daemon_config|mcp_transport|mcp_port' aflow`.
- Observe: search has no matches (exit 1 expected); new negative command/config tests pass, existing configuration and UI tests pass.

**Done When:**

- Standalone command/configuration are unavailable and shared consumers still function.
- Every completed step is validated against code, tests, or observable behavior; changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if new consumers require a removed standalone-only interface or unrelated dirty files make change ownership ambiguous.

### [ ] Checkpoint 3: Remove the aflowd executable while preserving deployment

**Goal:**

- Package and release validation no longer require the standalone reconciliation executable.

**Context:**

- Inspect: `pyproject.toml`, `aflow/daemon.py` (particularly `main`), `deploy/aflowd/validate-runtime.sh`, `deploy/aflowd/aflowd.service`, `tests/test_aflowd_deploy.py`, `tests/test_aflowd_continuous_deploy.py`.
- Run: `rg -n 'aflowd|daemon:main|from aflow.daemon import main' pyproject.toml aflow deploy tests`.
- Preserve: all shared lifecycle classes/functions and `worker_main`; systemd service executes `aflow-app-server` with its existing settings.

**Scope:**

- May modify package script metadata, standalone `main` and its unused imports, release executable checks, and direct packaging/deployment tests.
- Must not rename deployment paths/services or execute installation, restart, rollback, or publication actions.

**Steps:**

- [ ] Remove `aflowd = "aflow.daemon:main"` from project scripts and remove the standalone `main` implementation. Retain shared `AflowDaemon` lifecycle methods and other consumers; clean only now-unused imports and executable-specific wording.
- [ ] Change the runtime validator's required executable list from `aflow aflowd aflow-app-server` to `aflow aflow-app-server`. Adjust only other explicit executable requirements found by the search; keep service names and deployment state unchanged.
- [ ] Update release fixtures to omit `bin/aflowd`. Prove valid fixtures pass without it and missing `aflow` or `aflow-app-server` still fail. Add a package metadata assertion that project scripts omit `aflowd` while retaining `aflow` and existing other scripts; use `tomllib` rather than reinstalling into the shared environment.

**Dependencies:** Checkpoint 2.

**Verification:**

- Run: `uv run pytest -q tests/test_aflowd_deploy.py tests/test_aflowd_continuous_deploy.py tests/test_control_plane_resume.py tests/test_cli.py`.
- Run: `shellcheck deploy/aflowd/validate-runtime.sh`.
- Run: `uv run python -c 'import tomllib; from pathlib import Path; s=tomllib.loads(Path("pyproject.toml").read_text())["project"]["scripts"]; assert "aflowd" not in s; assert s["aflow"] == "aflow.cli:main"'`.
- Observe: fixture validation accepts absent `aflowd`, still checks remaining required executables/integrity, and systemd ExecStart remains `aflow-app-server`.

**Done When:**

- Removed executable has no package registration or standalone entry function; deployment tests pass.
- Every completed step is validated against code, tests, or observable behavior; changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if an actual production code path invokes the removed executable beyond the identified validation checks, or unrelated dirty files make change ownership ambiguous.

### [ ] Checkpoint 4: Verify and document UI-server HTTP MCP

**Goal:**

- Make the existing authenticated UI-server MCP surface the documented access path and prove its contract survives removal.

**Context:**

- Inspect: `apps/aflow_app/server/AGENTS.md`, `aflow/mcp_control_plane.py`, server `src/aflow_app_server/{main,mcp_adapter,control_plane_service}.py`, server `tests/{test_mcp,test_auth,test_control_plane_api}.py`, and `aflow-control-plane.mcp.example.toml`.
- Inspect: `README.md`, `ARCHITECTURE.md`, `DEVLOG.md`, `apps/aflow_app/README.md`, `deploy/aflowd/README.md`, `docs/ui-global-config-handoff.md`.
- Preserve: existing HTTP mount, registry location, service routing, authentication, schemas and lifecycle semantics.

**Scope:**

- May update MCP adapter explanatory text, targeted integration tests, existing client example comments, and relevant documentation.
- Production MCP behavior changes are unnecessary unless removal reveals a direct import/wiring regression; fix only that regression. Do not build a new adapter or UI screen.

**Steps:**

- [ ] Retain shared and server registry construction. Update stale descriptions claiming the registry backs a standalone listener; explain the UI-server HTTP mount and shared domain services.
- [ ] Use existing `test_mcp.py` fixtures to verify all 14 names and three resource templates, authenticated discovery/read behavior, startup questions, write idempotency, revision conflicts, stop/resume, and REST parity. Retain existing coverage rather than duplicate it. Fill only identified gaps for both mount spellings and preserved auth failures.
- [ ] Verify the actual application wired by `aflow ui` remains the same MCP-serving app via existing UI/server composition tests. Keep UI lifecycle tests proving server stop does not signal workflow processes; never carry over standalone client-EOF shutdown semantics.
- [ ] Replace the README standalone daemon section with UI-server MCP usage: start `aflow ui` or `aflow ui --daemon`; connect to the server's URL plus `/mcp`; supply the configured server token in the Authorization header; discover projects/plans, then start and inspect runs. List all 14 tools with short purposes, explain write idempotency/revision arguments, and link the existing secret-free client template. Explain that HTTP connection loss does not stop runs, network reachability follows existing server settings, and there is no standalone stdio transport. Do not introduce new client-specific configuration keys.
- [ ] Remove active README `[daemon]` and standalone help examples. Update architecture module listing and ownership explanation, server README, and deployment README to distinguish retained `aflowd.service` from removed executable. Update obsolete helper/test references in `docs/ui-global-config-handoff.md` without rewriting its unrelated design. Record a concise DEVLOG entry noting removal and lack of backward compatibility. Update `aflow/AGENTS.md` with the new shared helper responsibility if useful; root AGENTS remains unchanged. Other nested instructions need no changes unless their responsibilities actually change.
- [ ] Audit current user-facing documentation for removed-command recommendations. Leave historical DEVLOG entries and prior plan ledgers intact. Audit full accumulated changes for unrelated edits.

**Dependencies:** Checkpoints 1–3.

**Verification:**

- Run: `uv run --project apps/aflow_app/server pytest -c apps/aflow_app/server/pyproject.toml -q apps/aflow_app/server/tests/test_mcp.py apps/aflow_app/server/tests/test_auth.py apps/aflow_app/server/tests/test_control_plane_api.py`.
- Run: `uv run pytest -q tests/test_ui_cli.py tests/test_persistent_units.py tests/test_cli.py tests/test_config.py tests/test_process_identity.py tests/test_control_plane_resume.py tests/test_run_config_snapshot.py tests/test_aflowd_deploy.py tests/test_aflowd_continuous_deploy.py`.
- Run: `uv run ruff check aflow/cli.py aflow/config.py aflow/process_identity.py aflow/ui_cli.py aflow/daemon.py aflow/mcp_control_plane.py aflow/control_plane/persistent_units.py aflow/control_plane/run_activity.py apps/aflow_app/server/src/aflow_app_server/mcp_adapter.py`.
- Run: `rg -n 'aflow daemon|daemon_cli|\[daemon\]|aflowd' README.md ARCHITECTURE.md apps/aflow_app/README.md deploy/aflowd/README.md docs/ui-global-config-handoff.md`.
- Observe: matches only explain removals or retained internal/systemd concepts; no active standalone usage instructions. Tests confirm unchanged MCP contracts and UI lifecycle. Distinguish pre-existing lint/test failures from regressions; do not repair unrelated work.

**Done When:**

- Authenticated HTTP MCP discovery and existing lifecycle tests pass, documentation describes implemented behavior, and no standalone feature is required.
- Every completed step is validated against code, tests, or observable behavior; changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if preserving the 14 tools requires a broader lifecycle/auth redesign or unrelated dirty files make change ownership ambiguous.

## Behavioral Acceptance Tests

1. Given normal CLI arguments, `daemon` and its former subcommands fail with status 2 and create no daemon pidfile or environment file. UI background/status/stop parsing still works.
2. Given a fresh package script table, `aflowd` is absent and `aflow` remains. A valid deployment fixture without `bin/aflowd` passes validation; missing either retained required executable fails.
3. Given config containing `[daemon]`, both single and split loading reject the unsupported section. Config without it retains existing defaults and workflow merge behavior.
4. Given a valid server token, MCP discovery at either mount spelling returns exactly the 13 existing tools and three resource templates. Read results and mutations retain existing REST parity.
5. Given missing/wrong token or browser-cookie-only credentials, MCP rejects access. URL/body credentials remain rejected, and project/path restrictions remain enforced.
6. Given duplicate write requests with the same idempotency key, no duplicate run/action occurs. Stale revision changes fail; valid startup answers and explicit stop/resume retain existing outcomes and lineage.
7. Given an independently running UI worker, UI-server shutdown does not signal it; restarting or reconnecting to the server retains access to durable status. Use controlled worker fixtures, not real agent runs.
8. Given Linux or fallback process observations, identity values and reused-PID rejection remain equivalent after helper relocation.

## Plan-to-Verification Matrix

| Requirement | Concrete verification |
|---|---|
| Standalone CLI/config removal | Checkpoint 2 CLI rejection, config tests and source search |
| Shared identity preserved | Checkpoint 1 helper and ownership-consumer tests |
| Executable removed, deployment kept | Checkpoint 3 metadata assertion and deployment fixtures |
| All tools/resources retained | Checkpoint 4 MCP registry and HTTP contract tests |
| Auth and mutation safety preserved | Checkpoint 4 MCP/auth/control-plane tests |
| UI background and worker lifetime preserved | UI CLI and persistent-unit tests |
| Clear client setup and removed docs | Checkpoint 4 documentation review and scoped search |

## Assumptions And Defaults

- User decisions: remove only standalone modes, retain `aflow ui --daemon`; keep existing 14 tools when inexpensive; HTTP only; unchanged bearer authentication; no backward compatibility; also remove `aflowd` executable while preserving systemd deployment and shared internals.
- Inspected code already mounts the shared tools in the UI server, so retaining the registry in place is the smallest implementation. No feature expansion is needed.
- Removing dedicated `[daemon]` configuration follows feature removal and no-compatibility scope. No edits or migrations to real user files or old run snapshots are authorized.
- Preserve internal names and existing service deployment names; this handoff is not a wholesale daemon terminology rename.
- Existing client template and server settings remain authoritative. Do not change host, port, token source, approval metadata, or project selection behavior.
- Root README, AGENTS, DEVLOG and ARCHITECTURE already exist. No project bootstrap or new directories beyond this plan are needed.
- Original planning used an earlier dirty checkout; execution now starts from accepted published main. Reinspect the actual clean baseline. The private live service is aflow-ui.service on8765; legacy deploy/aflowd source/tests remain compatibility coverage, not instructions to reactivate the disabled service. No live service changes belong to workers.
- Leave this original ledger in `plans/in-progress/`; check boxes only after verification. The workflow engine owns its eventual move.
