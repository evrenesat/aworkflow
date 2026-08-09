# Control-plane persistence

- This package owns additive durable control-plane artifacts only: launch manifests,
  launch phases, ordered run events, and revisioned `overrides.toml` writes.
- `.aflow/runs`, `run.json`, and the workflow controller remain authoritative for
  workflow state. Do not add a second run database or bypass existing override
  parsing/validation.
- Keep on-disk payloads versioned, bounded, and secret-safe. New artifacts must use
  atomic/exclusive writes and reject path escapes.
