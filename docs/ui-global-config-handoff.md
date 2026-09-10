# Handoff: shared global configuration and one-command AFlow UI

## 1. Objective and authority

An AFlow user on Linux or macOS installs AFlow once, then runs `aflow ui` from any directory. The command serves the usable web UI, prompts for a password on first use, and uses the same global workflows and teams as the CLI. Registering a project does not require recreating configuration. Workflows survive UI shutdown/restart, and configuration edits affect only new runs.

This is a self-contained implementation handoff, not an AFlow checkpoint plan. No implementation is authorized by this document alone. The receiving session must receive implementation authorization, follow the applicable repository instructions, and preserve concurrent work.

Prepared against commit `b8041574e1ebe41035b35577d3c63f494f81d818`. The Mac and p100 checkouts matched at initial inspection. p100 later became unreachable; the user explicitly authorized writing this plan in the Mac reference checkout. That exception authorizes this planning artifact, not future local builds, implementation, or deployment. Recheck p100 availability and instructions before implementation.

## 2. Confirmed product decisions

1. All projects use only the global configuration in the home directory of the account running AFlow. There is no optional project configuration or project override layer.
2. The command is `aflow ui`, available from any working directory.
3. Foreground is the default; Ctrl+C stops the UI server. A separate `--daemon` flag starts it in the background.
4. Other devices can connect by default. Direct HTTP on LAN/Tailscale is acceptable; no HTTPS reverse proxy is required.
5. First launch asks for a password. The password is stored as the existing shared credential (`[server] auth_token`, or `auth_token_file`) in the existing global `~/.config/aflow/config.toml`.
6. The projects root is the existing global directory setting (`[control_plane] managed_projects_root`) in that same file.
7. The UI edits shared workflows, teams, and settings; changes apply across projects.
8. Normal AFlow installations include built UI assets and do not need Node/npm. Editable development installations build the UI automatically when needed.
9. Running workflows continue after UI shutdown or restart and reconnect when the UI returns.
10. Global settings remain editable during runs. Existing runs retain their original configuration; new runs use the saved configuration.
11. Windows is out of scope.
12. `aflow ui` and the existing app server are one product surface and share one configuration: the existing `~/.config/aflow/config.toml`. No `[ui]` table is added to `aflow.toml`.

## 3. Scope and selected implementation contracts

### 3.1 Now

Deliver configuration parity, packaging, the launcher, foreground/background UI lifecycle, durable workflow ownership on both platforms, global editing, and end-to-end checks together. Include logs, readiness/status output, automated validation/release packaging, and documented rollback. A server that merely serves HTML is not completion.

Retain the existing project registry as the explicit allowlist beneath the configured projects root. Global configuration does not grant access to every directory automatically. Project registration, plan editing, run controls, and harness selection remain the existing product paths.

### 3.2 Later / out of scope

No Windows support, multi-user accounts, project-specific configuration, config inheritance, database, new scheduler, automatic start at login/boot, certificate provisioning, public-internet deployment, cross-machine config synchronization, or automatic source upgrades. Do not rewrite the existing production systemd deployment. This delivery must work without systemd on either supported platform.

### 3.3 CLI and first-run contract

Implement these public forms:

```text
aflow ui
aflow ui --daemon
aflow ui --status
aflow ui --stop
aflow ui --host 127.0.0.1 --port 8765
```

`--daemon`, `--status`, and `--stop` are mutually exclusive. Status and stop do not build assets, prompt, or start anything. Stop targets the UI only; stopping a workflow remains an explicit run control. A second start reports the existing server URL/status and exits successfully rather than starting a second owner. A port occupied by another program is an actionable error, not evidence that AFlow is running.

Default port is `8765` and projects root `~/code` (both existing `config.toml` defaults). Per decision 4, first-run setup writes `bind_host = "0.0.0.0"` explicitly; when the key is absent from `config.toml`, the code default remains `127.0.0.1` so an existing deployment never widens its binding silently. Resolve `~` on the server host, never against the caller's current directory. First interactive launch asks for the password twice without echo and asks the user to confirm or change the projects root, showing `~/code` as the default. If both are already configured, do not prompt again. Complete setup before detaching for `--daemon`. Without a TTY, missing required settings produce exact configuration instructions and a nonzero exit; do not invent credentials or hang on stdin.

Startup prints build/setup progress when applicable, the configured root, foreground/background mode, local URL, network-access instructions, and the log location. Do not display `0.0.0.0` as a usable browser URL or promise firewall/Tailscale routing that has not been verified. A ready message requires the actual HTTP server to be accepting requests. Bind/build/config failures must fail the command visibly; detached startup waits for a bounded readiness response and returns failure with a log path if it cannot start.

### 3.4 One global configuration source

UI transport settings live in the existing server configuration file `~/.config/aflow/config.toml`, using its existing keys. Workflow/team settings remain the existing global pair `~/.config/aflow/aflow.toml` and sibling `workflows.toml`, unchanged. The project registry `projects.json`, the config audit log, and UI runtime state (`ui/` subdirectory: PID/birth record, lock, logs, readiness) stay in `~/.config/aflow`:

```toml
# ~/.config/aflow/config.toml — existing file, existing keys
[server]
bind_host = "0.0.0.0"   # written explicitly by first-run setup (decision 4)
bind_port = 8765
auth_token = "…"        # set interactively on first launch, or here / via auth_token_file

[control_plane]
managed_projects_root = "~/code"
```

Use `[server] auth_token` (or `auth_token_file`) as the existing shared login/bearer credential, preserving the current single-credential authentication model. Do not introduce accounts or another authentication service. First-run setup fills only missing keys in an existing `config.toml`, preserving unrelated TOML/comments. Store files containing the credential with mode `0600`, and never pass it in process arguments, logs, URLs, audit entries, or worker environments. Do not include it in run snapshots. Keep current session signing, constant-time credential comparison, expiry, rotation invalidation, same-origin checks, and MCP header-only authentication; rotating the token already invalidates old sessions because the session key is derived from it.

The global settings editor exposes the credential as a write-only replace field plus a `password_set` boolean. Its Advanced TOML representation omits both `[server]` credential keys; omitting them preserves the stored value. Reject empty replacement credentials. Use a dedicated optional password-update field in the save request rather than a magic masked string. Read/save/validate responses and validation errors must not reveal the credential, including when it was supplied in candidate TOML. Explicit manual file editing remains supported, and the settings page notes that the legacy `aflow-app-server` entry point reads the same file.

Launcher host/port flags override `[server] bind_host`/`bind_port` for that process. No per-project or launcher alternate workflow config option is added. The new command does not depend on `AFLOW_APP_CONFIG_DIR`, `AFLOW_EXECUTABLE`, `AFLOW_ENVIRONMENT_FILE`, `AFLOW_RELEASE_IDENTITY`, or manually clearing `VIRTUAL_ENV`: it uses the default global directory, resolves the installed executable and release identity from the installation executing the command, and prepares private runtime files automatically. An unrelated active virtual environment must not select a different AFlow installation. Reuse the existing `aflow_app_server.config` parser (lightweight, no server-app imports) for reading and writing these settings rather than duplicating key names.

The existing low-level `aflow-app-server` deployment configuration remains as a documented compatibility entry point sharing the same `config.toml`, registry, and audit log; it must use the same global workflow resolver and editor semantics and keeps its environment-variable ceremony. Explicit legacy deployment paths are not inputs required by `aflow ui`. Do not silently import alternate config directories or rewrite project-local files. If a deployment used a non-default `AFLOW_APP_CONFIG_DIR`, document that `aflow ui` uses the default global directory. Preserve old project-local config bytes and ignore them for new runs. Existing run ownership and history must not be discarded.

The UI settings page is global, reachable without selecting or registering a project, and says that changes affect new runs in all projects. New registrations immediately use global capabilities. Missing/invalid global workflows lead to a single global setup/error path, never a project starter wizard. Reuse the CLI's packaged defaults for absent workflow files, rather than creating the provider-neutral per-project starter. Do not overwrite an existing config pair.

Workflow/team changes become visible to new-run selection immediately after save. Binding and projects-root changes are saved with an explicit restart-required message; the running server keeps its old binding and root until restart. Password changes invalidate old sessions immediately and require login again. Changing the root does not move repositories or silently rebind registry entries: existing records outside the new root become unavailable with an explanation, while valid records and run history remain intact.

### 3.5 HTTP browser behavior

Direct access to `http://<server-address>:8765` must support login, authenticated page reload, writes, SSE, renewal, and logout from another device. Set the session cookie's Secure attribute according to the effective request scheme; HTTP needs a non-Secure cookie. Retain HttpOnly, SameSite=Strict, exact Origin checks on unsafe cookie-authenticated requests, and Cache-Control: no-store. Do not blindly trust client-supplied forwarded headers; the direct HTTP path needs none. Keep credential-bearing requests and responses out of access logs and caches. Update the server AGENTS.md to reflect this intentional change to its previous always-Secure rule.

### 3.6 Frozen configuration for existing runs

A fingerprint is not a saved configuration. Capture the complete effective workflow configuration once when a new run intent is durably reserved, before startup questions or worker launch. Persist a versioned, immutable, engine-owned configuration pair under that run's `.aflow/runs/<run_id>/config/`, excluding unrelated transport secrets and server/UI settings, which live in `config.toml` and are not part of the workflow pair. Preserve the effective meaning of schema-defined relative filesystem paths by resolving them against the original global configuration location before serializing the snapshot; do not rewrite arbitrary prompt strings or repository-relative plan paths. Record its origin and fingerprint in existing launch metadata, and use the snapshot consistently for startup answers, worker launch, retries, resume, and run inspection.

Snapshot creation and global UI saves share one configuration lock, so a launch sees either the old pair or the new pair, never half of each. Reuse the existing pair validation, expected-revision check, rollback behavior, and atomic/exclusive persistence helpers. Do not alter the workflow pair's canonical fingerprint computation; UI/server settings must never enter it, so fingerprints recorded before this change still match. Recheck file identities/content around reads to reject unstable manual edits. A failed snapshot must never launch a worker; partial reservations remain diagnosable through existing launch phases. Do not add a second run database or bypass the production config validator.

Global saves cannot be blocked just because another project has a running or resumable snapshotted run. Refresh future-run configuration/capabilities without replacing a live daemon's run ownership or rewriting its original inputs. A pending startup question belongs to its already-frozen intent. Resume uses the original snapshot; an explicit fresh/restart run uses current global settings and retains existing lineage rules. Existing explicit per-run overrides retain their present semantics and reference that run's frozen profiles.

Extend the same snapshot contract to newly started CLI and local-daemon runs, since they also share the global files. Do not change CLI workflow/team selection behavior. Preserve legacy runs that predate snapshots: only backfill from their recorded original source when the existing frozen fingerprint proves an exact match. If the original config cannot be recovered, explain that resume is unavailable; never substitute the current global config silently. A still-running legacy worker must not be terminated just to make the new UI consistent.

### 3.7 Portable ownership and restart behavior

Use a dedicated persistent subprocess unit adapter for `aflow ui` on both Linux and macOS. Retain SystemdUnitManager for the existing production deployment and the current local-daemon shutdown behavior for its existing command. Do not turn the current in-memory SubprocessUnitManager into a supposedly persistent adapter by simply skipping its shutdown method.

Implement the UI adapter behind the existing UnitManager protocol, with durable identity/exit records beneath the existing per-run durable directory. Each launched run has a small detached worker wrapper that owns its workflow subprocess group, writes bounded startup/error/exit evidence, and outlives the UI. Implement the wrapper as a private, undocumented dispatch of the installed executable (for example `aflow ui-worker`) that receives the run manifest and launches the existing worker startup path (today's `daemon-worker` entry) reading the frozen snapshot. Use the installed AFlow executable for the wrapper and worker; do not start another web server or controller per project. The wrapper must record failures before the normal controller starts, as well as normal exits.

Persist the run ID, intended unit, invocation nonce, process birth identity, PID/group identity, and terminal result with versioned atomic writes. Reuse the portable birth-identity helper in `aflow/process_identity.py`; require matching invocation and run/root identity before observing/adopting/signalling a process. Do not rely on `/proc` or PID existence alone on macOS. Hold an exclusive per-run launch claim across preparation so concurrent starts/restarts cannot launch duplicate workers. Keep the run metadata as the source of truth, not process-table name matching.

A new UI process reconciles those exact receipts and the existing run state. It can display, monitor, and explicitly stop a pre-existing worker, including its subprocess group. UI Ctrl+C, `--stop`, and server crashes do not signal workflow groups. A workflow may finish while the UI is absent; the returning UI must report the real terminal result and events. Missing or conflicting process identity becomes an observable ownership error, never an excuse to start a duplicate or signal another process.

UI background mode itself uses one per-user PID/birth record and lock in `~/.config/aflow/ui/`, detached with standard Python subprocess facilities. Foreground and background share this ownership record. Keep private rotating/bounded UI logs there. Implement a small readiness/status file or local probe tied to that invocation, excluding credentials. Do not install launchd/systemd services or require sudo. Reboot survival is not promised; after reboot reconcile stale receipts into accurate stopped/interrupted states.

### 3.8 Packaging and editable development

Ship the server Python package and compiled web assets in the normal `aworkflow` wheel. Keep the existing server source location and import name `aflow_app_server`; map it into the root distribution using Hatch configuration rather than relocating the entire server tree. Put packaged web assets at a stable package-resource location such as `aflow/ui_web/`, loaded with importlib.resources. Root dependencies must include server runtime dependencies. The server subproject remains a development/test entry point with root-package dependency ownership, not a second required end-user installation.

Retain Python 3.11+ support: adjust the server's declared minimum and test its code/dependencies on 3.11 and 3.12 rather than silently raising the CLI minimum. Keep web dependencies and npm lockfile authoritative. Add a build helper and Hatch hook that build/verify assets for wheel/sdist creation; include the required web/server sources in the sdist and prove a wheel built from that sdist works. The released wheel must never invoke npm at runtime.

Detect an editable install through distribution metadata (direct_url.json), resolve its source checkout there, and validate the known manifests. Never infer the AFlow checkout from the current directory. The new launcher may import the server's lightweight config module early but imports the server application itself only after preparation. For editable installs, run `npm ci` when dependencies are missing or the lockfile changed, then the existing TypeScript/Vite build when source/build-input fingerprints changed or assets are missing. Cache a successful fingerprint only after a successful build; never serve stale assets after a failed rebuild. Serialize concurrent development builds and publish a completed asset directory only after success. With unchanged sources, skip npm/network work and rebuilding. State required Node/npm versions from the existing CI toolchain; missing developer prerequisites produce one actionable error, not a remote install script.

Normal installation remains one standard installation of `aworkflow`, followed by `aflow ui`; no npm, `uv sync`, environment-file creation, or manual environment variables are part of that user path.

## 4. Source map and verified gaps

1. `aflow/config.py`: `_config_path`, `load_workflow_config`, bootstrap helpers, and project_configuration_state. The default is global (`~/.config/aflow/aflow.toml`), but explicit paths bypass it. UI settings do not live here: no `[ui]` table or loader change is needed; keep the workflow pair's validation and fingerprint behavior untouched.
2. `aflow/cli.py`: build_parser and main dispatch. Add UI dispatch before workflow bootstrap/argument handling. Preserve `aflow run`, `show`, and `daemon` behavior.
3. `aflow/process_identity.py`: the portable PID birth-identity helper is shared by UI and workflow ownership consumers. The retained internal daemon lifecycle remains separate from the removed standalone command.
4. `aflow/daemon.py`, `aflow/api/startup.py`, `aflow/api/runner.py`, `aflow/workflow.py`, `aflow/run_state.py`, `aflow/runlog.py`: runner reloads config by path; frozen identity currently stores path/hash, and worker/resume checks compare current config. Audit every load/compare before introducing snapshots.
5. `aflow/control_plane/{application,units,reconciliation,persistence,repository,models}.py`: composition defaults to SystemdUnitManager; SubprocessUnitManager tracks Popen instances only in memory. Extend the existing durable contracts, not HTTP handlers.
6. `apps/aflow_app/server/src/aflow_app_server/config.py`: the server's own global config parser (`~/.config/aflow` via `AFLOW_APP_CONFIG_DIR`; `config.toml` with `[server]` bind/auth and `[control_plane] managed_projects_root`; `projects.json`; `config_audit.jsonl`) — reuse it for UI settings. `project_service.py`, `project_config_service.py`, and `control_plane_service.py` hardcode project-local `.aflow/config` resolution (readiness, save, reload); config save blocks active/resumable runs and reloads one project. `main.py` resolves web assets relative to the checkout and always sets Secure cookies; `browser_session.py` derives the session key from the auth token.
7. `apps/aflow_app/web/src/App.tsx`, `components/GuidedConfigForm.tsx`, and the associated settings/API components: remove project setup gating and move shared settings outside project selection. Find all related contracts with `rg -n 'configuration_required|/config|initialize_config|global role defaults' apps/aflow_app`.
8. `pyproject.toml`, `uv.lock`, server pyproject/lock, web package.json/package-lock.json, `.github/workflows/ci.yml`, `.github/workflows/publish-package.yml`: root wheel currently excludes the server and web assets; dashboard checks currently run on Linux only.

## 5. Sequential implementation work

1. **Establish ownership and baseline.** On the authorized working host, read nearest AGENTS.md files, inspect branch/HEAD/dirt/worktrees and active AFlow/UI controllers. Preserve unrelated files (p100 had untracked `.reasonix/` and `reasonix.toml` at last successful inspection). Create an isolated feature branch; do not repoint the shared editable tool beneath active runs. Record baseline failures only after reproducing them on unchanged source.
2. **Add the global contract.** Reuse `aflow_app_server.config` (ServerConfig) as the single UI settings source behind a shared resolver used by CLI, readiness, web config, and daemon routing; make UI runtime inputs (executable, release identity, environment file) automatic. Implement lossless first-run writes to `config.toml` (`auth_token`, `managed_projects_root`, explicit `bind_host`) and credential redaction. Test global-only registration with conflicting old local configs preserved.
3. **Persist the effective config at run reservation.** Add immutable run snapshots and route all startup/worker/resume loads through them, including newly launched CLI/local-daemon runs. Retain source-path semantics, fingerprints, launch idempotency, legacy matching, and existing override rules. Prove config A remains runnable/resumable after global config B is saved before removing config-save guards.
4. **Implement portable persistent units.** Add `aflow/control_plane/persistent_units.py` and a private installed worker-wrapper dispatch. Keep the protocol and existing systemd/local-daemon adapters intact. Add durable ownership/exit receipts, reattachment, exact stop, and startup failure logging. Prove a detached test workflow survives server exit on both platforms before integrating the full editor.
5. **Switch UI global settings and launch routing.** Refactor project_config_service.py into a global service, with global routes GET/PUT `/api/config`, POST `/api/config/validate`, and POST `/api/config/form`, replacing the current project-scoped endpoints (`GET/PUT /api/projects/{id}/config`, `POST /api/projects/{id}/config/validate`, `POST /api/projects/{id}/config/form`) with the same operations. Update transport models/client calls together. Any retained project-config API compatibility route must delegate to this same global service and never write local config. Remove the project starter fields from create/register flows. Use a global revision/lock, allow edits during snapshotted runs, and refresh only future capabilities. Preserve unrelated registry, plan, and run functions.
6. **Package and launch.** Add `aflow/ui_cli.py`, a focused asset-build helper, and the root packaging hook/config described above. Implement `aflow ui` dispatch, setup prompt, foreground/background ownership, status/stop, readiness, and logs. Update run_server to accept resolved configuration without an environment-variable ceremony. Ensure app creation/lifespan sees the same settings instance. Wire the persistent adapter specifically into this launcher path.
7. **Complete browser behavior.** Move settings to global navigation; show workflow/team choices from global config, scope and restart-required messages, write-only password editing, and clear missing/invalid-global-config feedback. Adapt HTTP cookies and test real remote-address login, renewal, writes, SSE, and logout. Do not use browser localStorage for the password.
8. **Validate, document, and prepare delivery.** Run the commands and acceptance scenarios below. Update README.md, ARCHITECTURE.md, DEVLOG.md, docs/{installation,cli-usage,configuration,remote-app,runtime-behavior}.md, apps/aflow_app/README.md, and affected AGENTS.md rules. Document the exact command, first-run experience, state/log locations, original-run settings, migration, and rollback. Add nested guidance only for meaningful new modules. Commit eligible changes and prepare a discoverable PR; obtain the user's required public-publishing approval before pushing or publishing.

## 6. Verification commands

Run in the authorized implementation checkout. These are development checks, not end-user setup instructions. Run focused tests during the corresponding steps, then complete the full checks once integration is ready.

```sh
uv sync --frozen
uv run ruff check aflow apps/aflow_app/server/src
uv run pytest -q tests/test_config.py tests/test_cli.py tests/test_process_identity.py tests/test_control_plane_resume.py tests/test_run_state.py tests/test_resume_relocation.py tests/test_systemd_units.py
uv run pytest -q tests/test_ui_cli.py tests/test_run_config_snapshot.py tests/test_persistent_units.py
uv run pytest -q
uv run python -m compileall -q aflow apps/aflow_app/server/src
uv sync --project apps/aflow_app/server --frozen --group dev
uv run --project apps/aflow_app/server pytest -q
npm --prefix apps/aflow_app/web ci
npm --prefix apps/aflow_app/web run lint
npm --prefix apps/aflow_app/web test -- --run
npm --prefix apps/aflow_app/web run build
uv build
uv run playwright install chromium
uv run --group dev python scripts/smoke_ui.py
```

The three named new test files are required deliverables. Extend existing server tests for config, registration, authentication, and control-plane API behavior; extend App/GuidedConfigForm tests for global navigation and removal of project setup. Add Python Playwright to the root development dependency group for a focused browser smoke test of the actual login/run/restart flow, not to the runtime dependencies. Automated browser tests must drive a non-loopback HTTP address — bind `0.0.0.0` and browse to the host's primary non-loopback IP or the runner hostname — because Chromium accepts Secure cookies over `http://localhost`, which would mask exactly the Secure-cookie regression this check targets. Add the exact runnable smoke entry point shown above; it must create private temporary state/projects, use deterministic fake harnesses, avoid live user config, and return nonzero on failure. It must select the newly built wheel explicitly, validate its identity, and print which artifact it tested.

CI must run server, launcher, persistent-unit, packaging, and installed-wheel smoke checks on both ubuntu-latest and macos-latest, covering Python 3.11 and 3.12 (tiered matrix). Run the full Playwright browser HTTP flow on Linux only, plus one minimal macOS browser login/cookie check on a single Python version. Retain existing unrelated test jobs. Update the reusable CI and publish workflow so artifact publication depends on those checks and wheel asset verification; building only the core package is insufficient. Test wheel-from-sdist as well as wheel-from-checkout.

For installed proof, use a task-private uv tool directory/home in automated checks, install the built wheel with the installed-entry-point mechanism, change into an unrelated temporary directory, remove Node/npm from that test's PATH, and invoke `aflow ui`. Do not replace the developer's shared installed AFlow to run this test. Also prove an editable install resolves its checkout from an unrelated working directory, builds once, skips unchanged builds, rebuilds after a web edit, and exits visibly on a broken build. The smoke script owns only its temporary homes, processes, and files and cleans up its exact identities.

## 7. Required observable acceptance scenarios

1. **Existing CLI user:** global workflows/teams appear immediately after project registration, with no local config creation and no Configuration Required warning for that project. CLI/UI select the same effective workflow, team, harness profiles, and defaults. A deliberately different local pair is ignored and byte-for-byte preserved.
2. **Fresh installation:** built wheel starts from an unrelated directory without Node/npm; setup prompts before binding, persists password/root once, and subsequent launches need neither setup commands nor prompts. Noninteractive missing credentials fail cleanly.
3. **Two projects and editing:** start A in project one, leave its startup question pending, save config B globally, answer A, and start B in project two. A uses its original configuration; B uses the new one. Edit again during both runs without blocking. Resume A after UI restart using its snapshot even if A's workflow/profile was removed globally. A fresh restart uses current config and ordinary lineage.
4. **Snapshot failure/races:** invalid/unstable config, concurrent saves/launches, failed writes, and a stale revision never produce a partially configured worker. Failed saves preserve the prior pair and secrets; legacy fingerprint mismatch does not silently resume under new settings. Test relative-path semantics explicitly.
5. **UI lifecycle:** foreground Ctrl+C and `--daemon` followed by `--stop` stop only the UI. A running deterministic multi-step workflow continues advancing while the UI is absent. Restart reconnects exactly once, displays intervening events/completion, and can explicitly stop the reattached workflow. Repeat for an abruptly killed UI process on Linux and macOS.
6. **Ownership/failure evidence:** simultaneous UI starts, stale/reused PID records, concurrent run starts, worker startup failure, ordinary worker exit, and a server bind failure yield accurate status/logs and no duplicate controller. No unrelated process is signalled. Child-process stop behavior is verified after reattachment.
7. **HTTP browser use:** from a separate LAN/Tailscale browser when available, log in at the server's actual HTTP address, reload, edit a plan/global settings, start a run, observe events, and log out. Wrong passwords and cross-origin writes fail. Password rotation invalidates old sessions; no secret appears in API config responses, logs, snapshots, URL, or browser storage. Automated tests use a non-loopback HTTP address (primary non-loopback IP or hostname) to catch Secure-cookie regressions; record any unavailable physical second-device check explicitly.
8. **Global runtime settings:** changing root/host/port reports restart required, does not switch live ownership, and takes effect after restart. Root changes preserve registry records and refuse out-of-root access. A password-only edit does not alter running workflow behavior.
9. **Delivery artifact:** install the produced wheel on both OSes, run from outside the source tree with no Node/npm, and complete a fake workflow through the browser. A wheel rebuilt from the sdist passes the same asset smoke check. CI and release packaging use this validated artifact.

## 8. Delivery and rollback

Update the existing automated package-release path; do not create a new deployment service. Build and validate the distributable before any approved publication. Document installation of a specific validated wheel/version, post-install status/HTTP/run smoke checks, and rollback to the prior package/config backup. Publishing a release or changing a live installation requires separate user authorization.

Before switching a real installation, record its executable/release, config pair, active runs, and UI ownership. Do not upgrade/repoint an editable executable beneath active workers; choose a safe handover after runs finish or preserve their exact installed execution environment. Preserve original config/local files and all run data. An older binary may not understand new snapshots/receipts: rollback must never hand a new-format active run to it or delete those artifacts. Stop only the new UI, allow its workers to finish, then use the prior version with the preserved compatible config. Expose this limitation in the release notes rather than claiming arbitrary live downgrade support.

The implementation handoff report must list the resulting commit/PR, exact tested OS/Python versions, completed acceptance scenarios, baseline failures with reproduction evidence, and any remaining manual second-device check. Do not claim completion from unit tests or a web build alone.

## 9. Planning artifact status

This document records all confirmed product choices and selected implementation contracts; it does not claim the behavior is implemented. Revised 2026-09-07 after a codebase review: decision 12 (share the existing `config.toml`, no `[ui]` table) and the tiered CI matrix were confirmed by the user; source-map attributions, the worker-wrapper dispatch, global route names, the fingerprint-stability rule, and the non-loopback browser-test mechanism were tightened. It is stored under docs because the author's global ignore rules exclude new plans files. Only this handoff document is part of the planning change. The repository is public, so the committed plan must await explicit user approval before a branch push/PR publication.
