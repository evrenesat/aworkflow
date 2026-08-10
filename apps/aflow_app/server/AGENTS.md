# Remote App Server Notes

- Keep planning-session routes and daemon-backed lifecycle routes separate, but share the header-only bearer dependency in `main.py`.
- Lifecycle endpoints must call `ControlPlaneService`, then daemon/application services. They must never start a workflow subprocess, write `.aflow` state, or invent run identities in the HTTP layer.
- `control_plane.projects` is an explicit project allowlist. Resolve only its configured paths; reject URL tokens, arbitrary roots, traversal, and unsafe plan paths.
- Transport models in `models.py` mirror the versioned canonical models in `aflow.control_plane`; update the contract tests with any canonical change.
- Keep responses, SSE payloads, and logs bounded/redacted. Do not add CORS middleware without an explicit trusted-origin design.
