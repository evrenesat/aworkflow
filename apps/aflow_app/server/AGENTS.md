# Remote App Server Notes

- Global configuration PATCH accepts ordered typed actions or edited documents,
  never both. Transform under the shared pair lock and validate the final pair;
  preserve PUT/MCP compatibility. Prompt text belongs only to the existing schema.
- Project scheduling GET/PATCH and MCP tools delegate to the shared
  `ProjectSettingsService` revisioned document. Queue projections read
  admission claims and capacity plus plan lifecycle/dependency evidence;
  transport code must not reserve slots or invent plan ownership.
- Restart options is read-only and delegates admission to the daemon. Never
  fabricate owner-stop evidence for failures or reset plan progress in transport.

- Keep provider-independent plan routes and daemon-backed lifecycle routes separate, with the shared bearer-or-browser-session dependency in `main.py`. MCP transports stay header-only: the `/mcp` mount never accepts the session cookie.
- Browser sessions (`browser_session.py`) are signed HMAC-SHA256 cookies derived from the current deployment token; they never contain it, expire after 30 days, roll forward only on `X-AFlow-Activity: 1` responses, and require exact same-origin `Origin` for cookie-authenticated unsafe methods. Session endpoints send `Cache-Control: no-store`.
- Lifecycle endpoints call `ControlPlaneService`, then daemon/application services. They do not start workflow subprocesses, write run state, or invent run identities in the HTTP layer.
- The app lifespan owns `PlanConsumer`: it scans registered primary projects,
  calls `ControlPlaneService.start_run` for eligible stable plans, and releases
  only scanner ownership on shutdown. Plan mutations may wake the scanner.
  Promotion can claim a ready plan before the promote response is inspected;
  direct manual starts must recheck the shared plan claim and capacity lock.
- The versioned project registry is the explicit allowlist beneath one configured managed root. Resolve exact registry records only; reject URL tokens, arbitrary roots, traversal, and unsafe plan paths.
- `plan_service.py` edits only direct regular Markdown files in the five lifecycle directories (`todo`, `in-progress`, `done`, `failed`, `needs-plan-change`). Preserve expected-revision checks and source bytes on rejected writes or moves. Failed/invalid classification and requeue use the shared lifecycle journal and admission guard; transport resumes recorded eligible lineage through the control plane.
- `skill_service.py` is a thin facade over the shared `aflow.skill_store` and
  `aflow.skill_installer` services: reads/saves never initialize, refresh, or
  install, and install reuses the default `install-skills --yes` selection
  (optional skills excluded). Skill state is account-local to the server
  process (`~/.config/aflow/skills`); refresh reads the package resources
  visible to the running server, so a package upgrade needs a normal server
  restart before reinstall reflects it.
- Transport models in `models.py` mirror canonical models in `aflow.control_plane`; update contract tests with canonical changes.
- Keep responses, SSE payloads, and logs bounded and redacted. Do not add CORS middleware without an explicit trusted-origin design.

- Reserved startup rejections use `DaemonStartupError` to return a bounded,
  sanitized message and run ID. Other exception responses remain opaque.
  The dedicated `ExtraInstructionsValidationError` is also safe to expose
  before run allocation, with its fixed code, field, and constraint message.
  Resume visibility uses the daemon's read-only admission preview; mutation
  endpoints still recheck all admission conditions.

- Worker failure status comes from the canonical receipt-aware repository projection on each read, including cached services. Keep worker-exit fields mirrored in transport models and diagnostic context authenticated and bounded.

- API daemon composition uses read-only reconciliation, including cold reads.
  History endpoints delegate to the additive history service; internal run
  enumeration and lifecycle artifacts must remain available after UI deletion.
