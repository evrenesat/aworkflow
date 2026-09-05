# Remote App Server Notes

- Keep provider-independent plan routes and daemon-backed lifecycle routes separate, with the shared header-only bearer dependency in `main.py`.
- Lifecycle endpoints call `ControlPlaneService`, then daemon/application services. They do not start workflow subprocesses, write run state, or invent run identities in the HTTP layer.
- The versioned project registry is the explicit allowlist beneath one configured managed root. Resolve exact registry records only; reject URL tokens, arbitrary roots, traversal, and unsafe plan paths.
- `plan_service.py` edits only direct regular Markdown files in the three lifecycle directories. Preserve expected-revision checks and source bytes on rejected writes or moves.
- Transport models in `models.py` mirror canonical models in `aflow.control_plane`; update contract tests with canonical changes.
- Keep responses, SSE payloads, and logs bounded and redacted. Do not add CORS middleware without an explicit trusted-origin design.
