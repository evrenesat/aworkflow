# Remote App Server Notes

- Keep provider-independent plan routes and daemon-backed lifecycle routes separate, with the shared bearer-or-browser-session dependency in `main.py`. MCP transports stay header-only: the `/mcp` mount never accepts the session cookie.
- Browser sessions (`browser_session.py`) are signed HMAC-SHA256 cookies derived from the current deployment token; they never contain it, expire after 30 days, roll forward only on `X-AFlow-Activity: 1` responses, and require exact same-origin `Origin` for cookie-authenticated unsafe methods. Session endpoints send `Cache-Control: no-store`.
- Lifecycle endpoints call `ControlPlaneService`, then daemon/application services. They do not start workflow subprocesses, write run state, or invent run identities in the HTTP layer.
- The versioned project registry is the explicit allowlist beneath one configured managed root. Resolve exact registry records only; reject URL tokens, arbitrary roots, traversal, and unsafe plan paths.
- `plan_service.py` edits only direct regular Markdown files in the three lifecycle directories. Preserve expected-revision checks and source bytes on rejected writes or moves.
- Transport models in `models.py` mirror canonical models in `aflow.control_plane`; update contract tests with canonical changes.
- Keep responses, SSE payloads, and logs bounded and redacted. Do not add CORS middleware without an explicit trusted-origin design.
