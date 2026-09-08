# Control-plane persistence

- This package owns additive durable control-plane artifacts only: launch manifests,
  launch phases, ordered run events, and revisioned `overrides.toml` writes.
- `.aflow/runs`, `run.json`, and the workflow controller remain authoritative for
  workflow state. Do not add a second run database or bypass existing override
  parsing/validation.
- Keep on-disk payloads versioned, bounded, and secret-safe. New artifacts must use
  atomic/exclusive writes and reject path escapes.

- Startup failures stay in the existing atomic start-request record. Repository
  projection may expose their bounded safe message but must preserve active-unit
  and controller-terminal authority. Manifest timestamps are submission times;
  execution timing comes from controller metadata and terminal launch evidence.

- Portable worker diagnostics are a read-only projection of contained, schema/nonce/unit-validated receipts. Keep worker exit time separate from workflow execution time; no exit code alone proves completion. Recovery requires confirmed owned inactivity. GETs must never repair historical receipts or run controllers.
