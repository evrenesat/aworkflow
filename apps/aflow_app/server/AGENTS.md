# Remote App Server Notes

- Global configuration PATCH accepts ordered typed actions or edited documents,
  never both. Transform under the shared pair lock and validate the final pair;
  preserve PUT/MCP compatibility. Prompt text belongs only to the existing schema.
- Restart options is read-only and delegates admission to the daemon. Never
  fabricate owner-stop evidence for failures or reset plan progress in transport.

- Keep provider-independent plan routes and daemon-backed lifecycle routes separate, with the shared bearer-or-browser-session dependency in `main.py`. MCP transports stay header-only: the `/mcp` mount never accepts the session cookie.
- Browser sessions (`browser_session.py`) are signed HMAC-SHA256 cookies derived from the current deployment token; they never contain it, expire after 30 days, roll forward only on `X-AFlow-Activity: 1` responses, and require exact same-origin `Origin` for cookie-authenticated unsafe methods. Session endpoints send `Cache-Control: no-store`.
- Lifecycle endpoints call `ControlPlaneService`, then daemon/application services. They do not start workflow subprocesses, write run state, or invent run identities in the HTTP layer.
- The versioned project registry is the explicit allowlist beneath one configured managed root. Resolve exact registry records only; reject URL tokens, arbitrary roots, traversal, and unsafe plan paths.
- `plan_service.py` edits only direct regular Markdown files in the three lifecycle directories. Preserve expected-revision checks and source bytes on rejected writes or moves.
- Transport models in `models.py` mirror canonical models in `aflow.control_plane`; update contract tests with canonical changes.
- Keep responses, SSE payloads, and logs bounded and redacted. Do not add CORS middleware without an explicit trusted-origin design.

- Reserved startup rejections use `DaemonStartupError` to return a bounded,
  sanitized message and run ID. Other exception responses remain opaque.
  Resume visibility uses the daemon's read-only admission preview; mutation
  endpoints still recheck all admission conditions.

- Worker failure status comes from the canonical receipt-aware repository projection on each read, including cached services. Keep worker-exit fields mirrored in transport models and diagnostic context authenticated and bounded.

- API daemon composition uses read-only reconciliation, including cold reads.
  History endpoints delegate to the additive history service; internal run
  enumeration and lifecycle artifacts must remain available after UI deletion.
