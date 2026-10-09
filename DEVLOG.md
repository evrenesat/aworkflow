# DEVLOG

## 2026-10-09 — configuration validation ownership: global editor and legacy facade delegate to current owners (AFLOW-MAINT-20261006 · 03)

- Checkpoint 2 of the configuration validation ownership plan. The active
  global configuration code no longer imports the legacy
  `project_config_service` module: `global_config_service.py`,
  `config_response.py`, `guided_config.py`, `mcp_adapter.py`, and `main.py`
  now import the exception types, report/snapshot shapes, revision/bounds
  helpers, and the in-memory candidate validator from
  `aflow_app_server/config_validation.py`, and protected document reads plus
  audit records from the new thin `aflow_app_server/config_documents.py`
  adapter. `config_validation.py` additionally owns the
  `ProjectConfigSnapshot` shape and the `DOCUMENT_HEX_RE` expected-revision
  bound that previously lived in the legacy module.
- `project_config_service.py` is now an explicit compatibility facade: it
  re-exports the legacy module-level names and keeps the project-scoped
  `ProjectConfigService` read/save behavior, but its duplicated private
  commit machinery (mkstemp staging, manual restore, fsync helpers) is
  removed. Project saves and reads now run under the shared
  `aflow.config_pair` pair lock, complete pending-transaction recovery
  first, and commit through
  `aflow.config_pair.commit_configuration_pair` — the same transaction
  owner the global editor uses. No new routes were registered and the
  public save/validate contracts are unchanged.
- New import-boundary tests in
  `apps/aflow_app/server/tests/test_global_config_patch.py` prove the active
  global configuration modules do not import the legacy project editing
  path and that `GlobalConfigService` visibly delegates to
  `aflow.config_pair`, `config_validation`, and `config_documents`.
- Test injection points moved with the implementation: the project-service
  rollback/torn-read/size-bounds tests now patch the current owners (global
  `os.replace`, `config_documents.MAX_CONFIG_DOCUMENT_BYTES`) instead of
  attributes of the removed private code, and the second-file-failure test
  changes both documents (the transaction owner only replaces changed
  documents, so a workflows-only-identical candidate never reached the
  injected failure). All behavioral assertions are unchanged.
- Review repair (cp01-v01): the legacy helper spellings
  `_read_protected_document`, `_append_audit_line`, and `_DOCUMENT_HEX_RE`
  were missing from the facade; they are restored as explicit aliases of the
  exact current-owner objects (`config_documents.read_protected_document`,
  `config_documents.append_audit_line`,
  `config_validation.DOCUMENT_HEX_RE`), so old
  `from aflow_app_server.project_config_service import ...` imports resolve
  again without copied implementations. A focused identity regression test
  (`test_legacy_config_helper_aliases_keep_current_owner_identity`) fails
  without the aliases and passes with them.
- Documentation: `ARCHITECTURE.md` and `docs/configuration.md` describe the
  current parse/transaction/editing owners and label
  `project_config_service.py` as a compatibility-only facade; the server
  `AGENTS.md` records the import boundary.
- Checks (Linux, worktree at 3d0aec9a plus this checkpoint's uncommitted
  changes): `uv run --project apps/aflow_app/server pytest -q
  apps/aflow_app/server/tests/test_project_config_service.py
  apps/aflow_app/server/tests/test_global_config_patch.py
  apps/aflow_app/server/tests/test_guided_config.py
  apps/aflow_app/server/tests/test_mcp.py -k 'config'` (134 passed, 55
  deselected) and `uv run pytest -q tests/test_config_pair.py
  tests/test_config.py` (225 passed, 7 subtests). Primary log:
  `tmp/aflow-maintainability-20261006/03/checkpoint-2-verify.log`.
  Cumulative final-verification gates (lint, full regression, wheel, UI
  smoke, publication) remain with the final checkpoint.

## 2026-10-09 — multiprocess fixture respects transient journal lock contention (test-only)

- CI 37880564817 at 05ca3c6e failed macOS
  `tests/test_execution_resources.py::
  test_real_multiprocess_fifo_exclusion_and_independence` because
  `_mp_first_holder` asserted `record_completion(...).state == "released"`
  directly. The store contract treats `contended` (journal lock briefly held
  by a sibling process) as a supported nonterminal result, so the assertion
  crashed the A child and `a_released` timed out after 60s. No production
  change: this is a fixture misuse of a documented result.
- Added `_mp_settle` in `tests/test_execution_resources.py`: repeats the same
  operation (same owner/invocation/arguments) while the outcome is
  `contended`, within the existing 60-second fixture budget with 25 ms bounded
  polling on `time.monotonic`. It returns the first non-contended outcome
  unchanged; unexpected terminal outcomes and raised exceptions reach the
  caller immediately, and a deadline failure names the operation and last
  outcome/reason. The clock and sleep are injectable, so the new
  `TestMultiprocessContentionRetry` class proves retry, no-retry-on-unexpected,
  deadline diagnostics, and exception propagation with a fake clock (no real
  60-second waits).
- All fixture lifecycle mutations that assumed immediate success
  (`_mp_first_holder`, `_mp_second_waiter`, `_mp_independent_holder`:
  enqueue/try_acquire/mark_launching/register_child/record_completion) now go
  through `_mp_settle` and explicitly assert their expected terminal states,
  so A/B/C release receipts and B enrollment readiness are published only
  after confirmed durable outcomes. B's existing bounded waiting-acquisition
  loop (which already tolerates `contended` and `queued` waiting states) is
  unchanged.
- The parent's release-before-acquisition assertion no longer depends on A's
  post-release Event, which A sets only after the durable release and could
  theoretically be preempted before B's acquisition observes it. B now records
  durable journal ownership evidence at acquisition; the parent asserts the
  confirmed `("A-release", "released")` receipt plus B being the durable
  owner, with B's ticket (2) still ordered after A's (1). Mutual exclusion is
  unchanged (B can only acquire while no owner exists); documented by the
  deterministic `test_release_then_acquire_leaves_second_invocation_as_durable_owner`.
- Checks (Linux, worktree at 39c9dfc5): `uv run pytest
  tests/test_execution_resources.py -q -k 'MultiprocessContentionRetry or
  real_multiprocess_fifo_exclusion_and_independence'`, `uv run ruff check
  tests/test_execution_resources.py`, `git diff --check`. Proof kept under
  `/tmp/mp-release-contention-fixture-ci-20261009-proof`. Cross-platform CI
  (macOS) remains a separate delivery gate.
- Review repair (same plan, repair overlay cp01-v01): `_mp_second_waiter` initialized `owner` only inside the
  successful-acquisition branch, but the acquisition receipt always read it.
  When the bounded waiting loop expired (supported `contended`/`queued`
  outcomes until the 60-second deadline), the child raised
  `UnboundLocalError` and lost its `("B-acquire", "timeout", None, None)`
  receipt. `owner = None` now precedes the branch; journal reads stay
  acquisition-only, and the timeout path still emits no B-release and no
  completion call. Added the deterministic
  `TestMultiprocessContentionRetry::
  test_second_waiter_timeout_emits_receipt_without_owner` (fake store/queue/
  events, fake monotonic clock, parameterized over both waiting outcomes) so
  the timeout receipt is proven without any real 60-second wait. Re-ran the
  focused pytest filter, Ruff, and `git diff --check` against the repaired
  state; evidence appended to the same proof log.

## 2026-10-09 — configuration fixture repair: portable EIO path and clean zcode fixture repo

- CI 37875182064 (4d88e447) and 37875345919 (3bfd998c) failed the reviewed
  configuration-pair change's fixtures. `tests/test_config.py::
  test_public_loader_fails_closed_on_record_inspection_io_error` subclassed
  abstract `pathlib.Path`; on Python 3.11 constructing that subclass raises
  `AttributeError` before the injected `lstat` EIO is ever reached, so the
  intended fail-closed `ConfigError` was never exercised. The fixture now
  subclasses the concrete platform path type (`type(tmp_path)`) and keeps the
  `lstat` EIO injection and every original production-loader assertion
  (exception cause, exact-byte preservation of both documents and the record).
  No production code changed.
- `tests/test_zcode.py::test_zcode_completes_through_public_runner` failed on
  all four core jobs because `load_workflow_config` creates the runtime pair
  lock (`.aflow-config-pair.lock`, `aflow.config_pair.PAIR_LOCK_NAME`) in the
  fixture git repository, making startup see a dirty tree and ask for dirty
  confirmation. The fixture `.gitignore` now lists the exact lock name
  (imported from the authoritative constant, alongside `.aflow/`) before the
  initial commit, and the test asserts `git status --porcelain` is empty after
  `load_workflow_config` and before the real `prepare_startup`. The
  fake-provider integration assertions (PreparedRun, transition_end, actual
  fake zcode invocation, plan completion, result label) are unchanged. No
  blanket ignore and no dirty-confirmation bypass.
- Verification: `uv run --python 3.11 --isolated --with pytest --with
  pytest-subtests --with rich --with tomlkit pytest -q
  tests/test_config.py::test_public_loader_fails_closed_on_record_inspection_io_error
  tests/test_zcode.py::test_zcode_completes_through_public_runner` (2 passed)
  and the equivalent `--python 3.12` command (2 passed), with isolated
  basetemp under `/tmp/aflow-config-fixture-ci-20261009-cp1/`. The exact-SHA
  CI matrix for the new revision remains an outstanding coordinator delivery
  gate.

## 2026-10-09 — managed-stop FIFO fixture: barrier now requires durable tickets

- CI37875345919 at3bfd998c (macOS Python3.11) failed
  `TestManagedStopReconcileE2E::test_automatic_reconcile_managed_stop_grants_fifo_head`
  with only waiter W1 visible in the journal after both run records exposed
  `execution_resource_wait`. Root cause: the pre-stop barrier keyed only on
  the wait record's presence. Production
  `ExecutionResourceAdmission.admit` intentionally publishes
  `on_waiting(reason, ticket=None)` when the journal lock is contended and
  only enqueues (and publishes the ticket) on the retry, so a ticketless
  wait record does not prove durable enqueue. No production defect is
  claimed; the exact scheduler timing on macOS was not captured and is not
  asserted here.
- `tests/test_exclusive_execution.py` now requires the state the case
  asserts. The per-waiter predicate demands a positive integer ticket with
  the matching resource and a non-empty invocation identity from each
  waiter's actual run record (trace draining and immediate child-error
  failure unchanged; the bounded `_wait_until` budgets are unchanged). The
  pre-stop journal boundary uses the new `_e2e_waiter_enrollment` barrier,
  which rereads the resource journal and returns the `{label: ticket}` map
  only when the owner is still the captured original owner and the queue
  holds exactly one well-formed claim per intended waiter whose ticket and
  invocation identity match the waiter's own durable wait record. Incomplete
  enrollment (absent wait, ticketless lock-contended wait, missing claim)
  returns `None`; a malformed or foreign claim, an identity mismatch, a
  duplicate claim, or a replaced owner raises immediately. The integration
  now drives this barrier through the narrow
  `_e2e_wait_for_waiter_enrollment` polling helper, which retains the
  first successful `{label: ticket}` snapshot from inside the bounded
  wait, validates its exact two-entry length, and returns it without an
  unguarded reread: an already-enrolled waiter can legitimately publish
  `ticket=None` when its next admission pass hits journal-lock
  contention, and that later ticketless run record must not invalidate
  the accepted snapshot. Head/tail are derived from ticket order
  (smallest ticket is head) on the retained snapshot. All subsequent real
  owner-stop, predecessor-cessation, head/tail and non-overlap checks are
  unchanged, and no manual reconcile call was introduced.
- New `TestFifoTicketBarrier` regression exercises the barrier behaviorally
  against durable run/journal state (no broker or controller mocking):
  absent wait, ticketless lock-contended wait, wrong-resource wait, and a
  single enrolled claim are all unready; two matching durable claims are
  ready with ticket-order head/tail selection; malformed tickets, foreign
  claims, invocation mismatches, and a replaced owner fail closed. A new
  deterministic regression wraps the enrollment observer so that its next
  observation regresses one wait record to ticketless immediately after
  the first successful snapshot: the polling helper must return the
  accepted two-ticket snapshot with ticket-order head/tail even though a
  subsequent direct observation now returns `None`, and the successful
  observation must not be repeated after success.
- Verification: `uv run pytest -q tests/test_exclusive_execution.py -k
  'ManagedStopReconcileE2E or ManagedStopReconcileCrash or FifoTicketBarrier'`
  (12 passed, including the new snapshot-retention regression) and
  `uv run ruff check tests/test_exclusive_execution.py` both pass on the
  local Linux platform; compact proof in
  /tmp/aflow-fifo-ticket-ci-20261009-cp1/. No production code changed.
  The exact-SHA macOS Python3.11 CI gate for the new combined revision
  remains an outstanding coordinator delivery gate and is not claimed from
  the local Linux run.

## 2026-10-09 — fixture child reap: confirmed absence is completed cleanup

- `_e2e_reap_pid` in `tests/test_exclusive_execution.py` now observes
  liveness once before every signal attempt: a confirmed-absent child is
  treated as completed cleanup and the helper returns without probing birth
  or signalling. This fixes the cleanup-boundary race that failed
  `TestManagedStopReconcileCrash::test_automatic_reconcile_controller_crash_live_child_waits`
  on macOS/Python3.11 in CI run37867626559 (the cleanup assertion demanded a
  present PID while liveness had already reported absent). The exact race
  timing on macOS is not established and is not claimed here; Linux and
  macOS 3.12 passed the same unchanged test.
- Fail-closed behaviour is unchanged and is revalidated per signal stage:
  for a present child with a captured birth, exact process-birth matching is
  required before TERM and again before KILL; unknown liveness, missing
  birth, and mismatched/reused identities raise without signalling,
  `ProcessLookupError` remains a completed disappearance, and a stubborn
  matching child still produces the bounded "did not cease" failure after
  exactly one TERM and one KILL. No production code changed.
- New `TestFixtureChildReap` focused tests use controlled liveness/birth/kill
  observations and assert observable signals: zero signals (and no birth
  lookup) on confirmed absence; TERM only, returning after confirmed exit;
  no KILL when absence is observed at the escalation boundary; KILL with a
  fresh birth check for a still-present matching child; no unsafe signal on
  unknown/missing/mismatched identities in either signalling stage;
  tolerated `ProcessLookupError`; and bounded failure for a stubborn child.
  The real integration assertion (peer stays queued while the crashed
  controller's live child owns the resource, automatic admission only after
  confirmed cessation) is unchanged and runs on the local platform without
  mocking the owner or broker away.
- Verification: `uv run pytest -q tests/test_exclusive_execution.py -k
  'FixtureChildReap or automatic_reconcile_controller_crash_live_child_waits'`
  (12 passed) and `uv run ruff check tests/test_exclusive_execution.py`
  both pass on the local Linux platform. The exact-SHA macOS Python3.11 CI
  gate for the new revision remains an outstanding coordinator delivery
  gate and is not claimed from the local Linux run.

## 2026-10-09 — AFLOW-MAINT-20261006-14 CP3: migrate backups, control-plane writes, and resource journals to the shared file primitives

- Migrated the remaining selected persistence callers to the audited
  `aflow/file_io.py` primitives, one caller at a time, while each domain keeps
  its own validation, locks, schema checks, and transaction ordering:
  - `plan_backups._write_atomic_json` now uses `atomic_replace_file` at mode
    `0o600` and keeps its tolerant `_fsync_directory` domain adapter (open and
    fsync failures tolerated).
  - `control_plane.persistence._write_exclusive_json` now uses
    `create_exclusive_file` at mode `0o600` (hard-link exclusive publish, no
    overwrite); `_write_atomic_bytes` now uses `atomic_replace_file` at mode
    `0o600`. Both keep the open-only-tolerant `_fsync_directory` adapter and
    the parent `mkdir`.
  - `execution_resources._write_journal` now uses `atomic_replace_file` at
    mode `0o600` (no directory sync, as before) and keeps its journal size
    cap, revision/claim ordering, and lock ownership.
- Removed the now-unused per-caller `tempfile.mkstemp` temporary writers; the
  control-plane event journal's append-only path stays with its journal owner
  for the later plan 13 handoff and is not folded into the atomic replacement
  primitives.
- Updated `docs/persistence-primitives.md` caller contract table and migration
  status for the migrated callers.

## 2026-10-09 — owner-stopped checkpoint-review-to-final-review resume admission

- A graceful owner stop that lands on the checkpoint-review-to-final-review
  edge can now be resumed to run the configured first cumulative review, even
  with a complete original plan and no active implementation scope. A new
  narrow classifier (`_owner_stopped_pending_final_review_evidence` in
  `aflow/cli.py`) verifies owner_stopped status/end reason, no unresolved
  boundary decision and no active scope, an exact finalized checkpoint-reviewer
  turn with a completed zero-exit receipt and complete `snapshot_after`, and a
  declared transition to the configured cumulative review role (architect, or
  senior_architect where configured) in the current workflow metadata.
- The verified boundary is carried as the pending cumulative-review
  continuation (`ResumeContext.pending_cumulative_review`), so the managed
  `can_resume` preview and the successor bootstrap agree and both start exactly
  that cumulative review with no checkpoint implementation or review replayed.
  The existing finalized-worker checkpoint-review branch is preserved unchanged,
  and ordinary complete plans and already-delivered plans are not newly
  rerouted to review.
- This boundary requires the current owner-stop intent to be cleared before
  admission: the classifier reads the source's current `overrides.toml`
  directly through `load_override_request` (no consumed-digest shortcut) and
  rejects a missing, unreadable, or invalid current request, or one that still
  carries `owner_stop = true`, even when the original stop digest was already
  accepted and consumed. With the stop uncleared, the managed preview reports
  `can_resume=false` and the shared bootstrap refuses before any successor is
  reserved; a supported revision-checked clear restores admission.
- Tests: positive first-final-review admission (architect and senior_architect)
  and focused negative cases, including uncleared/absent/invalid current stop
  requests, in `tests/test_run_state.py`; a real-controller fixture in
  `tests/test_control_plane_resume.py` runs implementation and checkpoint
  review, requests a graceful stop, asserts the preview and managed resume
  refuse with no successor artifacts before the supported clear, then clears
  it, resumes, and asserts the first successor invocation is the cumulative
  review with idempotent replay and retained lineage. No real harness
  requests.
- Verification: `uv run pytest -q tests/test_run_state.py
  tests/test_control_plane_resume.py tests/test_resume_pending_review.py
  tests/test_resume_checkpoint_repair.py tests/test_resume_review_repair.py`
  passes; `uv run ruff check aflow/cli.py tests/test_run_state.py
  tests/test_control_plane_resume.py` passes.

## 2026-10-07 — AFLOW-MAINT-20261006-02 final-review repair (cp01 v02): selected aliases read the target's canonical pair

- `load_workflow_config` no longer reconstructs the supplied leaf's basename
  inside the canonical target directory: when
  `config_pair.canonical_pair_directory` proves the selected leaf resolves to
  the target's canonical `aflow.toml`, that canonical file is selected under
  the same target pair lock. A noncanonical supplied basename (for example
  `alias/custom.toml` -> `real/aflow.toml`) therefore loads the exact target
  pair whether `real/custom.toml` is absent or is a valid unrelated document
  with a distinct model, instead of silently returning empty defaults or the
  unrelated file's configuration.
- `tests/test_config.py` adds a parameterized regression over both public
  loaders (`load_workflow_config`, `load_config`) for canonical, missing
  target-side same-name, and unrelated target-side same-name selections;
  ordinary regular files, separate siblings, and missing inputs are
  unchanged.
- `apps/aflow_app/server/tests/test_config_pair_parity.py` extends the
  existing fresh-process alias recovery coverage with the noncanonical
  supplied basename: prepared interruption returns the exact old pair,
  committed interruption returns the exact new pair, an unknown edit fails
  closed with a chained `ConfigError` preserving edit/journal bytes, and the
  no-journal overlapping save remains excluded by the target lock (asserted
  child exits 37/0/2, interpreted models/steps, and exact bytes).
- `docs/configuration.md` and `ARCHITECTURE.md` note that the target's
  canonical `aflow.toml` is selected regardless of the supplied leaf's name.

## 2026-10-07 — AFLOW-MAINT-20261006-02 final-review repair (cp01 v01): general reads honor the canonical pair behind supported aliases

- `load_workflow_config` now recognizes the supported whole-pair alias —
  both the selected `aflow.toml` leaf and its sibling resolving to the
  canonical pair in one other directory — through the new
  `config_pair.canonical_pair_directory` helper, and locks, recovers, and
  parses through that target directory's existing transaction owner. A
  prepared interruption on the target therefore restores the exact old pair
  before an alias read interprets it, a committed interruption installs the
  exact new pair, and an unknown edit under a pending target journal fails
  closed with a bounded chained `ConfigError` while preserving edit and
  journal bytes. One effective lock acquisition per canonical read is kept;
  live/snapshot reentry is unchanged.
- The supplied directory's pending-journal safety check runs before any leaf
  is dereferenced: a pending local record with symlink leaves still fails
  closed, a confirmed absent record is the only case that may redirect the
  transaction owner, and an initially absent target journal never bypasses
  the target lock. Regular files, relative paths, missing inputs, missing
  siblings, and a separately selected sibling keep their existing behavior.
- `tests/test_config.py` adds fresh-process alias regressions (prepared and
  committed recovery, unknown edit, alias-directory journal guard, separate
  sibling selection) plus the `load_config` alias over a canonical pair.
- `apps/aflow_app/server/tests/test_config_pair_parity.py` adds real
  `GlobalConfigService.save` child-process alias regressions: prepared and
  committed interruptions read through the aliases, a pending manual edit
  fails closed with preserved bytes, and a no-journal alias read overlapping
  a killed save proves the target lock covers recovery and both parses
  (asserted child exits 37/0/2).

## 2026-10-07 — AFLOW-MAINT-20261006-02 CP2 repair (v01): general reads hold the pair lock before journal inspection

- `load_workflow_config` no longer skips the pair lock when an unlocked
  record inspection reports no journal: every existing-file read now acquires
  the pair lock before inspecting recovery state and holds it through
  recovery, both document reads, parsing and validation. A save that starts
  after a reader observed no journal, or between its two document parses,
  therefore cannot return a mixed generation. The unlocked
  `_has_pending_config_pair_record` bypass was removed; a truly missing input
  with a confirmed absent record still returns the historical empty defaults
  without creating its parent directory, and any other journal inspection
  failure fails closed through the bounded `ConfigError` contract with the
  original reason chained.
- `tests/test_config.py` adds fail-closed core regressions for a record
  inspection I/O error, a dangling record link, and a pending unsafe document
  link, all preserving exact document and record bytes.
- `apps/aflow_app/server/tests/test_config_pair_parity.py` adds coordinated
  subprocess regressions over disposable directories: a no-journal general
  reader overlapping a real `GlobalConfigService.save` killed after the first
  replacement (the reader waits on the pair lock, recovers the complete old
  pair, and cleans the record; real child exit 37), and a reader paused after
  its first document parse while a full save requests the lock (the saver
  cannot replace either document until the reader finishes the complete old
  pair, then commits the complete new pair; real child exit 0). Both use
  explicit pipe/file barriers and assert the interpreted model/workflow
  combination plus exact on-disk bytes.

## 2026-10-07 — AFLOW-MAINT-20261006-02 CP2 repair: general filesystem reads recover before parsing

- The public `load_workflow_config` filesystem reader now runs the pair
  transaction boundary before parsing: it selects the same supplied or
  default path, inspects the canonical record without following any
  document leaf, and when a transaction is pending it locks the pair
  directory, completes recovery, and only then reads and parses the pair
  through the private already-locked parsing helper. Recovery failures
  become a bounded `ConfigError` with the original reason chained, preserving
  the record and document bytes; a pending transaction with an unknown edit,
  unsafe link, or malformed record therefore fails closed instead of being
  parsed through.
- With no pending transaction the historical behavior is unchanged: missing
  inputs keep the empty defaults without creating the parent directory,
  supported symlinks, split-file parsing, validation, and the `load_config`
  alias delegation are intact. Readers that already hold the pair lock
  (live configuration, run snapshots) re-enter the re-entrant lock, keeping
  one effective flock and one loader invocation.
- `tests/test_config.py` gains fresh-process core-reader regressions:
  prepared/committed recovery with exact pair digests, fail-closed unknown
  edits and malformed records, no-journal missing/default/symlink behavior,
  and alias/default-source delegation. `apps/aflow_app/server/tests/
  test_config_pair_parity.py` exercises a real `GlobalConfigService.save`
  killed after the first replacement observed first by a fresh
  `load_workflow_config` reader (old complete generation plus record
  cleanup), committed recovery (new generation), and a pending valid manual
  edit rejected without changing either document or the record.

## 2026-10-07 — AFLOW-MAINT-20261006-02 CP2: remote configuration pair parity on the transaction owner

- The global configuration service no longer stages and renames files
  itself: `save`, `patch`, and `read` now run recovery and commit the pair
  through the core transaction owner (`aflow/config_pair.py`, CP1). REST
  saves, MCP authoring-tool saves, and normal launches therefore share one
  crash-safe commit and one recovery path. The service keeps its public
  contracts: `GlobalConfigSnapshot` fields, `ProjectConfigError` / `ProjectConfigRevisionConflict`,
  audit line schema, and redaction are unchanged.
- Recovery is now reachable from every supported read path: the REST read,
  the live configuration watcher, and the run snapshot all parse only
  after `load_workflow_config` completes any pending transaction.
  `apps/aflow_app/server/tests/test_config_pair_parity.py` proves the new
  parity with a real prompt rename: a killed save at each durable boundary
  is observed by fresh REST and live reader processes and always yields the
  complete old or new pair (the rename and its workflow references move
  together). A manual edit during a pending recovery preserves the edited
  bytes and the record and fails closed on both boundaries; stale revisions
  and invalid candidates preserve exact bytes with the same public errors
  and audit outcomes over REST and MCP.
  A killed save is also observed through the real HTTP `GET /api/config`
  boundary (disposable server/config directory) plus a fresh-process live
  read on the same directory.
- Docs: `docs/configuration.md` (Remote configuration editing) describes the
  write-ahead transaction, read-path recovery, and the manual-edit
  conflict; `ARCHITECTURE.md` gains the `config_pair.py` section.
- Final verification (cumulative gate): `uv run ruff check aflow
  apps/aflow_app/server/src` clean; `uv run pytest -q tests/test_config_pair.py
  tests/test_config.py tests/test_live_config.py tests/test_live_config_runtime.py
  tests/test_run_config_snapshot.py tests/test_runtime.py` (588 passed);
  `uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_project_config_service.py
  apps/aflow_app/server/tests/test_global_config_patch.py apps/aflow_app/server/tests/test_guided_config.py
  apps/aflow_app/server/tests/test_mcp.py apps/aflow_app/server/tests/test_config_pair_parity.py`
  (194 passed); `git diff --check` clean; `uv build --wheel` plus
  `scripts/smoke_ui.py` on the built wheel: SMOKE PASSED.

## 2026-10-07 — AFLOW-MAINT-20261006-01 CP2: required dashboard web lint CI step

- The Dashboard CI job now runs `npm run lint` (existing web baseline,
  `eslint src --ext ts,tsx`) immediately after `npm ci` and before the web
  test and build steps, so every pull request exercises the same web lint
  baseline before build. No other CI gate changed: the existing OS/Python
  matrix, root test, ruff hygiene, browser (Chromium/WebKit) and packaging
  jobs are all preserved as-is.
- README "Local development" now documents the web baseline commands: the
  required `npm --prefix apps/aflow_app/web run lint` CI step plus the
  unchanged `npm test -- --run` and `npm run build` commands. It also records
  that tests moved by later maintainability plans keep explicit imports and
  focused fixtures, with no project-wide Git or path overrides to make tests
  pass.
- Verification: `npm --prefix apps/aflow_app/web run lint` exits 0 (same 11
  pre-existing warnings, no errors); `git diff --check` exits 0; the workflow
  YAML parses and the lint step is non-optional in the dashboard job. The
  required lint step running on an exact commit is confirmed by the delivery
  CI run after publication.

## 2026-10-07 — issue #76 release repair CP2: atomic same-plan startup-record publish

- CI 37611601058 also failed
  `test_distinct_start_keys_cannot_launch_the_same_plan_concurrently` on both
  native macOS jobs: the loser got a base `project_admission_error`
  (`safe_message` "startup record is unreadable") instead of the intended
  `project_plan_claim_conflict`. Linux passed, so the defect is a
  platform-scheduling-dependent race, not a logic bug.
- Root cause (demonstrated deterministically, not inferred from the outer
  code): the winner's `DaemonService._create_record` published
  `.aflow/start-requests/<run_id>.json` non-atomically — `os.open(O_CREAT|O_EXCL)`
  created an empty file at the final name, then wrote it. The launch manifest
  (`launches/<run_id>.json`) is published atomically first, which makes the
  run_id visible to the loser's admission evidence scan
  (`_all_run_evidence` -> `get_run_status` -> `_startup_record`). A concurrent
  loser reading the in-progress empty record got `RepositorySchemaError`
  ("startup record is unreadable"), which `_all_run_evidence` wrapped into the
  base `ProjectAdmissionSafetyError` (`project_admission_error`) before the
  reservation-based `ProjectPlanClaimConflict` check could run. On macOS the
  APFS/scheduling window reliably overlapped (2/2 jobs); on Linux it is rare.
  This is a separate admission artifact-publish race, not the CP1 Darwin
  process-identity cause.
- Fix (owning boundary, `aflow/daemon.py` `_create_record`): publish the
  startup record atomically — write the full record to a temp file in the same
  directory, then `os.link` it onto the final name (a hard link is atomic
  no-replace, preserving the "identity is already reserved" conflict). The
  final name is now created only with complete content, so a concurrent reader
  observes either no record or a complete one; a failed publish leaves no
  partial/empty file. The manifest and `launches/<id>.state.json` already use
  atomic temp+replace/link; no reader was weakened, so a genuinely
  malformed/unreadable record still fails closed with the safety error.
- New deterministic regression
  `test_startup_record_publish_is_atomic_and_complete`
  (`tests/test_aflowd.py`) pins the demonstrated contract: an injected
  mid-write failure leaves no file at the final name (the old
  create-then-write left an empty one), and a successful publish is
  immediately and completely readable by both the daemon reader and the
  control-plane reader the admission evidence scan uses.
- Verification: focused suites (`test_aflowd.py` 48 passed,
  `test_project_admission.py` 49 passed, control-plane/persistence 444 passed)
  plus a 60-iteration barrier stress of the real threaded concurrent start
  where every loser got `project_plan_claim_conflict` (no
  `project_admission_error`). The native macOS CI run of the CP2 commit
  remains the required release gate for the real-scheduling validation; the
  defect is proven by the deterministic reproduction, not by Linux timing.

## 2026-10-07 — issue #76 release repair CP1: truthful bounded macOS process cessation

- CI 37611601058 failed nine stop/teardown tests on both native macOS
  Python 3.11/3.12 jobs while Linux passed. Root cause: after KILL the
  provider is often an unreaped zombie (its parent controller is itself an
  unreaped zombie, so launchd never adopts it), and the Darwin
  observations misreported it. `process_group_state` used `kill(-pgid, 0)`,
  which stays alive while a zombie occupies the numeric group, so
  `session_ceased`/`_group_absent` could never confirm cessation ("owned
  session did not terminate"); `process_liveness` used `ps -o pid=`, which
  reports a zombie as present; and `_darwin_session_members` never read the
  per-process state, so zombies were captured with a usable birth and could
  anchor and revalidate as live members.
- Fix (Darwin only, Linux path untouched): `process_group_state` now runs
  one bounded `ps -axo pid=,pgid=,stat=` scan — a live (non-zombie) member
  makes the group present, a complete inventory with no members or only
  zombies is positively absent, and any malformed, over-bounded, failed,
  timed-out, or deadline-expired listing stays unknown. `process_liveness`
  reads `ps -o pid=,stat=` and reports a zombie as absent (a row without its
  state field is unknown, never present). Session inventories read the same
  state field and capture zombie members with no birth, so they can never
  anchor, revalidate, or be signalled. Ownership capture, TERM grace,
  fresh pre-KILL revalidation, direct-child reaping, and the shared
  kill-phase deadline (never renewed) are unchanged; no skips, xfails,
  timeout increases, or unknown-as-absent conversions were introduced.
- New deterministic native-Darwin-shaped regressions in
  `tests/test_process_identity.py` reproduce the failing contract on any
  host before the fix (zombie-only group kept "present"/unterminated) and
  pin the corrected behavior: live/zombie/absent group states, malformed,
  failed, over-bounded, timed-out, and budget-exhausted (expired) scans stay
  unknown, zombie liveness is absent, zombie session members carry no birth,
  `_group_absent` confirms a zombie-only group inside the shared kill
  deadline while a live member polls to the deadline unconfirmed, and
  `session_ceased` treats a zombie-only owned session as ceased. The
  existing Darwin-shaped fixture tests were extended to the new native
  `pid=,pgid=,stat=` listing contract. Native macOS CI remains the required
  release gate for real-process validation.
- Verification: focused suites (`test_process_identity.py`,
  `test_execution_resource_processes.py`, `test_systemd_units.py`,
  `test_persistent_units.py`, `test_exclusive_execution.py`) — 277 passed;
  `ruff check` on the touched modules/tests; `git diff --check` clean.

## 2026-10-07 — CP2 v10 repair: cover owned subprocess startup with exception teardown (issue #76)

- Extended the v09 owned-group cleanup boundary in `_run_process` from the
polling loop to the whole post-spawn lifetime: identity observation
(`owned_child_binding`), lease child binding, startup `banner.update`,
out/in stream setup, polling, and the synchronous stream joins now all run
inside one `try` whose single `except BaseException` handler calls
`_reap_owned_process` exactly once and rethrows the original exception with
its identity and type. The teardown group target is established before the
potentially interruptible birth observation: on Linux/macOS
`process_group=0` already makes the child lead its own group, so
`proc.pid` is the pre-observation target and `terminate_owned_group`
freshly revalidates the directly owned child/group before signalling; the
verified observed group replaces it afterwards, and unsupported platforms
keep `None` so no parent group is signalled.
- The bind-child failure keeps its established contract: the typed
`ResourceLeaseError("register_child", ...)` and the unconfirmed transition
are unchanged, and the outer handler now performs the single teardown for
it instead of the branch's own call, so overlapping binding/outer
exception paths clean exactly once and a failed binding is never completed
as a success release (the handler releases only when a child was
positively bound and the group is positively absent).
- New real-process regressions in
`tests/test_execution_resource_processes.py` (`owner_stop_unleased_startup`):
(a) an isolated fixture controller in its own session runs the real
unleased `_run_process` with the real `BannerRenderer` against a full
native output pipe, so `banner.update` is verifiably blocked (Linux wchan
guard; portable POSIX readiness barrier elsewhere) before the polling
loop; a SIGINT to that exact foreground group must end the owned provider
group with positive cessation and direct-child reaping before the 130
exit while an unrelated decoy session survives; (b) a real spawn whose
binding observation is interrupted by an injected `KeyboardInterrupt`
must propagate that identical interrupt after exactly one delegated call
to the real owned-group teardown, with the directly owned child
positively gone. Existing polling-interrupt, callback-stop, leased
bind-failure/success/unconfirmed, and FIFO tests are retained unchanged.
- Verification: four `owner_stop_unleased_startup` regressions, the eight
focused gates (including `TestManagedStopReconcileE2E` /
`TestManagedStopReconcileCrash` and the retained
`.issue76-proof/reviewer-fifo-race-v02.py`), the original checkpoint
gates (focused suites, scoped Ruff, `git diff --check`), and the retained
hotplug profile-drift test (the accepted-main versus retained
`tests/test_hotplug.py` difference is exactly `_occupy_resource`'s owner
identity: the retained live birth-matching identity is required because
admission now reconciles proven-dead owners, so the dead `999999`
identity from accepted main would be reclaimed and could not keep the
resource busy; the retained version at HEAD preserves both intended
behaviors) all pass, followed by one cumulative gate on the final source
state. See `.issue76-proof/cp2-fix-v10-verification.log` for exact argv,
cwd/HEAD, fingerprints, exit statuses, and elapsed times.

## 2026-10-07 — CP2 v09 repair: tear down the owned group on unleased interruption (issue #76)

- Repaired the v08-admitted unleased interruption regression without
touching completed adapter, deadline, startup, portability, FIFO, or
accepted-main work. `_run_process`'s exception path guarded
`_reap_owned_process` with `lease is not None`, while the stable
`process_group=0` spawn applies to every ordinary Linux/macOS subprocess:
a foreground Ctrl-C of a nonexclusive controller (`run_workflow` passes
`turn_lease=None`) reached the controller group, the newly separated
provider never received the interrupt, and the controller exited leaving
the provider orphaned. The exception path now calls
`_reap_owned_process(proc, process_group)` exactly once outside the lease
guard, uses its positive-absence result for the leased complete/unconfirmed
decision only when a lease exists, and rethrows the original exception.
`terminate_owned_group` has no broker/store involvement, so an unleased run
creates no exclusive state and an unrelated session is untouched.
- New regressions in `tests/test_execution_resource_processes.py` (real
`_run_process` and real owned-group teardown, no mocks for the positive
cases): an isolated fixture controller in its own session runs the real
unleased `_run_process`; a SIGINT to that exact foreground group must end
the provider group with positive cessation and reaping before the 130
exit, while an unrelated decoy session survives; and an unleased
`control_callback` stop must propagate the callback exception with
preserved identity and type, cease the owned child, and create no broker
journal. Linux-only subreaper reaping is guarded; native probes and
bounded readiness/cessation deadlines are used throughout.
- The hotplug profile-drift isolation from the previous cumulative run is
confirmed by the retained `tests/test_hotplug.py` already present at HEAD
(live birth-matching busy-owner identity); the accepted-main
`pid=999999`/`foreign-birth` form would be reclaimed under CP1 and is not
introduced here. Rerun evidence follows in the v09 verification log.

## 2026-10-07 — CP2 v08 repair: keep a timed-out startup wrapper observation unconfirmed (issue #76)

- Repaired the v07 bounded-entry startup classification defect without
touching completed adapter, deadline, portability, FIFO, or accepted-main
work. `PersistentUnitManager.stop` passed its entry operation deadline to
`_observe`, but `_observe` mapped a timed-out wrapper birth observation to
`inactive/startup_lost`, and stop returned that false inactive answer while
the recorded wrapper was still live and could finish startup. The
stop-specific startup observation now uses a new tri-state bounded birth
proof (`process_identity.process_birth_proof`): a positively observed live
wrapper keeps `active/start-post`, a positively absent wrapper keeps the
genuine `inactive/startup_lost`, and an expired deadline, timed-out or
inaccessible probe, missing/malformed wrapper identity, or a reused
still-present PID yields `inactive/startup_unconfirmed`, which stop converts
to a typed `PersistentUnitError` with no signal and no `stopped.json`
inside the original entry operation (the probe never renews the budget or
spends the generic five-second window). Ordinary `get` keeps the generic
observation contract, and trusted nonce-bound stopped/exit receipts remain
authoritative.
- The accepted-main `tests/test_hotplug.py` `_occupy_resource` helper was
integrated by keeping the CP1 retained form: the simulated busy owner uses
the store's provably live, birth-matching controller identity, so the
foreign claim keeps the resource busy under CP1's automatic dead-owner
reconciliation (the accepted-main dead `pid=999999` fixture would be
reclaimed and breaks
`test_run_resume_recovered_session_resource_identity_survives_profile_drift`).
Both intended behaviors are preserved by the retained helper; no other
accepted-main test changes were needed.
- New regressions in `tests/test_persistent_units.py` (fake clocks, native
Darwin `ps` output/timeouts, recorded signals; the final `_observe`,
`_bounded_process_alive`, and stop decision are not mocked): a timed-out
live-wrapper birth probe fails typed inside a one-second stop entry while
`get` stays `active/start-post`; missing and malformed wrapper identity fail
typed without any probe; a reused wrapper PID fails typed; a positively
absent wrapper returns `startup_lost`; a nonexpired live wrapper keeps
`active/start-post`; and matching trusted stopped/exit receipts authorize
their terminal answers without any wrapper probe. All eight focused gates,
the five original checkpoint gates, and the cumulative gate were rerun at
the final fingerprint.

## 2026-10-07 — CP2 v07 repair: complete stop/teardown deadline propagation and portable fixtures (issue #76)

- Repaired the two v06 root causes without changing completed adapter/
  deadline behavior. (1) Deadline propagation now reaches every stop/
  teardown decision: the persistent stop creates its outer deadline before
  any identity observation (the entry observation and identity recheck stay
  inside even a one-second stop window, and an unconfirmed entry identity
  fails typed with no signal and no `stopped.json`); both unit adapters
  create one absolute initial operation deadline (<=2s) before the first
  snapshot and thread that same value to the initial inventory, the
  original-controller revalidation, and the initial anchor/group signal
  proof (the outer deadline is reserved for later rescans/escalation and is
  never renewed after the inventory); the local exited-controller drain
  bounds every group observation to one operation that never passes the
  outer deadline and rechecks it before TERM, KILL, and a successful
  absence result; the owned-group teardown uses ONE kill-phase deadline for
  the ownership proof, group KILL, direct-child KILL/wait, and final group
  cessation (no renewed `kill_seconds` allowance), and
  `workflow._owned_group_absent` passes the existing deadline to the native
  group probe and rechecks it before positive completion. (2) The five new
  fixtures are host-portable: the three synthetic `test_linux_*deadline*`
  cases pin `process_identity.sys.platform` to `linux` so their fake procfs
  is exercised on every host, and the two real `subprocess_unit_stop_unknown`
  tests inject the failure at the host's native birth observation (Linux
  procfs suffix; on Darwin the controller's `ps -o lstart= -p PID` probe
  times out while every other probe delegates to the real function).
- New regressions (required behavior, fake clocks + recorded signals, no
  mocked final ownership decisions): a bounded persistent stop entry whose
  full-timeout native reads stay inside a one-second stop window (typed
  `PersistentUnitError`, no signal, no `stopped.json`); initial-budget
  threading in both adapters (inventory, controller revalidation, and
  initial proof all receive the one shared <=2s value, never the larger
  outer deadline); a positively exited local controller drain whose KILL
  gate's fresh group read completes after the outer deadline (only the
  in-budget initial TERM is issued, typed failure, no success); and an
  owned teardown whose final Linux group-absence scan (40ms) outlives the
  30ms kill-phase deadline (returns False, the claim stays unconfirmed, no
  renewed time). All eight v06 focused gates and the five checkpoint
  verification commands pass on the repaired tree.
- Evidence: primary log
  `.issue76-scratch/cp2-fix-v07-out/primary-v07.log` (cwd, exact argv, HEAD,
  dirty fingerprints, elapsed times); focused suites `test_process_identity.py`
  + `test_persistent_units.py` + `test_execution_resource_processes.py`
  = 149 passed. Implementation left uncommitted for independent checkpoint
  review; no plan state edited.

## 2026-10-07 — CP2 v06 repair: unknown-topology local stop, shared observation deadline, zombie cessation assertions (issue #76)

- `SubprocessUnitManager.stop` no longer treats every non-session contract
  as an exited leader. Only a directly owned Popen with a positive `poll()`
  result may use the owned controller-group drain; a live controller whose
  topology is unknown (timed-out/inaccessible birth, SID, or PGID
  observation) raises a typed `RuntimeError` before any TERM/KILL, retains
  the unit, and issues no group signal on a reusable numeric PID.
  `shutdown` shares the same failure behavior. The stop entry now uses the
  bounded birth probe instead of the generic five-second one.
- The shared stop deadline is now created once per operation before the
  work it bounds: the contract birth probe is bounded by the two-second
  observation window (never the entire outer deadline), and each rescan in
  `stop_session_groups` creates its operation deadline before the inventory
  and reuses that same absolute deadline for the post-inventory anchor,
  group proof, and cessation decision (no renewed window after a snapshot).
  `_bounded_process_alive` delegates to `process_birth_bounded` instead of
  duplicating native birth logic.
- Expired native evidence is rejected across the assigned stop paths:
  Linux procfs birth, liveness, inventory, and group-scan reads that finish
  at/after the shared deadline report no birth / `unknown` instead of
  presence, absence, or identity; the pre-signal ownership revalidation
  rechecks the operation and outer deadlines immediately before every
  TERM/KILL. Positive nonexpired zombie cessation and fail-closed
  inaccessible/malformed evidence are preserved.
- New regressions: `subprocess_unit_stop_unknown` local cases (real
  `_run_process` controller, separate-group provider, failed native birth
  observation, typed stop/shutdown failure with unit retained and decoy
  surviving, then successful ordinary stop once observation is restored);
  an owned-subreaper case where a deliberately unreaped zombie provider is
  positively ceased (`process_liveness == "absent"` while `kill(0)`
  succeeds); and deadline cases covering a 10-second outer Darwin contract
  probe (<=2s), a rescan inventory (0.8s) whose post-inventory proof is
  constrained to the same original two-second deadline, an ownership proof
  finishing at the deadline authorizing no signal, and Linux reads
  finishing exactly at/after expiry (including 30ms remaining) yielding no
  birth authority, no presence/absence, and no successful cessation.
- The new adapter tests now assert immediate positive provider cessation
  through `process_liveness(provider_pid) == "absent"` (an unreaped Linux
  zombie is positively ceased) instead of an immediate ESRCH from
  `kill(0)`, which raced asynchronous reaping; adopted fixture children are
  reaped only in cleanup using owned identities.
- Accepted-main integration (issue #78 lint baseline now resolved): the
  reviewed lint repairs were integrated and the exact full Ruff command passes.
  One cumulative-suite regression was isolated to
  `tests/test_hotplug.py::_occupy_resource`. Accepted main 5d3a0be0 occupies a
  foreign resource with a dead identity (`pid=999999, birth="foreign-birth"`),
  which this worktree's CP1 dead-owner reclamation automatically reclaims, so
  the resource is never held busy and
  `test_run_resume_recovered_session_resource_identity_survives_profile_drift`
  failed. The retained base fd488d62 helper uses a provably-live, birth-matching
  controller identity, which reclamation leaves alone. The smallest proven
  integration fix keeps the retained `test_hotplug.py` (git-clean vs base),
  preserving both intended behaviors (resource stays occupied; busy owner is not
  reclaimed). Cumulative validation on the reconciled tree (patch
  `76091b5d...`): full suite 3543 passed / 244 subtests, MCP stop 6 passed,
  broad Ruff, compileall and whitespace all exit 0 (see the primary log).

## 2026-10-06 — CP2 v05 repair: local-adapter session stop and shared stop deadline (issue #76)

- `SubprocessUnitManager` (the local control-plane adapter) now uses the same
  proven-session stop algorithm as the persistent and workflow adapters instead
  of signalling only the controller group. It proves the controller's exact
  birth and session-leader topology, then ends the controller group and any
  separate-group provider in the session (TERM, then bounded KILL escalation)
  and requires positive cessation of every owned member before reporting
  success. This closes the local-adapter gap where the separate harness group
  (`process_group=0`) left a provider child alive after a managed local stop.
  When the controller leader has already exited, the adapter drains its owned
  group with the same positive-absence requirement, and `shutdown` applies the
  same session stop to every managed unit.
- The shared stop algorithm in `persistent_units.py` is now a module-level set
  of functions (`session_contract`, `session_snapshot`, `session_anchor`,
  `session_ceased`, `stop_controller_group`, `stop_session_groups`) that all
  three adapters call with their own signal and deadline parameters; the
  per-adapter instance methods delegate to it.
- All stop-path revalidation shares one outer stop deadline. The local
  adapter's deadline is the configured stop timeout plus a bounded KILL grace
  so a verified TERM survivor is escalated and observed inside the window.
- Added `subprocess_unit_stop` selections that exercise a real local controller
  running `_run_process` with a separate-group provider: ordinary stop and
  shutdown tear down both the controller and the provider, a TERM-ignoring
  provider is escalated to KILL, and an unrelated decoy in another session
  survives.

## 2026-10-06 — CP2 v04 repair: pre-signal ownership proof, anchored post-inventory
revalidation, TERM-first bounded escalation (issue #76)

- Fixed the initial-snapshot replacement race in `stop()`: after the bounded
  session snapshot, the controller group is re-proved (exact recorded
  PID/birth, session ID, group identity) before any signal; a replaced or
  missing controller leaves the initial capture untrusted, no group is
  signalled, and the stop fails typed. The initial capture is now anchored by
  the verified session-leader contract and only same-session descendants are
  signalled from it.
- `_stop_session_groups` now revalidates a live proven anchor AFTER each
  inventory completes: a fresh inventory can never prove its own ancestry, so
  with no post-inventory live anchor no newly observed member is adopted,
  signalled, or used to clear prior uncertainty. A numeric-SID inventory is
  authorized only while a proven member is live (no unanchored inventories),
  and incomplete observations stay pending until a subsequent complete
  anchored observation clears them. Every ownership proof, group proof,
  anchor selection, and cessation decision shares one operation deadline of
  at most two seconds (or the remaining outer deadline), never a fresh
  per-member window; groups whose members are all positively dead (zombies)
  are never signalled.
- Escalation is now TERM-first and bounded per group: each verified group
  receives one TERM, and is escalated to KILL only after its own TERM grace
  elapses inside the outer deadline and a live proven member of that group is
  revalidated immediately before the KILL. The legacy controller-group stop
  observes the controller through a bounded TERM grace phase, then re-proves
  ownership with a fresh budget bounded by the still-live outer deadline
  before the KILL — an expired TERM-phase deadline is never passed as the KILL
  budget and the outer bound is never extended for a late signal.
- Fixed `_darwin_session_members` passing a relative remaining budget to
  `_ps_lstart_batch`, which takes the absolute shared stop deadline (the birth
  batch always failed closed on Darwin); added native Darwin `ps` output
  contract tests (padded PIDs, no numeric `sid` keyword, separate groups,
  vanished/inaccessible members, byte/member limits, expired evidence).
- Added focused regressions for all three reported defects: the reused initial
  target (no snapshot, no signal), the lost anchor (replacement inventory never
  signalled), the shared group-proof budget (one two-second window for a
  three-member group), and the legacy TERM-survivor KILL inside the outer
  deadline, plus real-process escalations for a TERM-ignoring provider and a
  surviving descendant after wrapper exit.

## 2026-10-06 — CP2 v03 repair: contract separation, bounded stop-path revalidation (issue #76)

- Separated the explicit-stop contract decision from the legacy fallback in
  `persistent_units.py`: `_session_contract` now returns a typed topology
  (`session`, `legacy`, or `unknown`) derived from the session-leader proof.
  An uncertain modern stop — identity loss during validation, a failed
  observation, or an unreadable birth — is `unknown` and fails typed with no
  signal, while a genuine legacy controller-group receipt retains its
  controller-only stop contract.
- Made every controller-group fallback signal re-prove ownership: TERM and
  the KILL escalation each require a fresh bounded `controller_group_owned`
  proof (exact recorded PID/birth, matching session ID, and group identity),
  so a changed identity after TERM is never escalated.
- Added `process_birth_bounded` (native Linux procfs read; bounded Darwin `ps`
  probe capped at the remaining stop budget, never the generic five-second
  fallback) and `controller_group_owned` to `process_identity.py`, and wired an
  optional shared `deadline` through `process_liveness`, `session_member_live`,
  `process_group_state`, `_owned_group_anchor_live`, and the `terminate_owned_group`
  escalation so stop-path revalidation cannot exceed the stop deadline.
- Marked the unreaped-zombie ownership test Linux-only: its `ps -o lstart`
  contract is a Linux observation, and Darwin reports unowned reaped zombies
  as `Z<tab>` without the full field set.
- Added recorded-signal regressions for loss during contract validation,
  failed snapshot with a replaced controller, changed/unknown identity after
  TERM, and valid legacy success (only the controller's own group signalled,
  no additional provider claim); added fake-clock regressions proving bounded
  Darwin revalidation and inaccessible Linux identity stay within the shared
  stop budget with no serial five-second probes and no signal after the
  deadline.
- Completed the FIFO ordering acceptance in the managed-stop E2E test: the
  shared completion wrapper's `reaped` trace timestamp (verified
  `os.waitpid` of the provider child) must precede the tail provider entry,
  and `_e2e_reap_pid` revalidates the fixture birth before every cleanup
  signal, including a later KILL.

## 2026-10-06 — CP2 repair: grace deadline, anchored session stop, FIFO test (issue #76)

- `terminate_owned_group` now revalidates the captured group's ownership after
  the snapshot and before the first TERM (a group that changed ownership in the
  capture window is never signalled), and the grace deadline is monotonic from
  the first TERM: the wrapper's own exit no longer ends the grace while a
  verified descendant survives. At the deadline it revalidates the live
  captured survivor immediately before the bounded KILL, and the final group
  check uses the bounded kill budget.
- `_stop_session_groups` now revalidates one live anchor from the captured
  identities (exact birth, PGID and SID) before every same-session rescan and
  before every signal: without a live anchor it inventories and signals
  nothing; every signal target is revalidated live just before the signal;
  zombie-only groups are never signalled (they cannot be observed); and an
  incomplete observation leaves a pending uncertainty that a later complete
  anchored observation must clear before a success receipt. Success still
  requires positive cessation of every captured member.
- The managed-stop FIFO test now hands the slot through a predecessor-verified
  FIFO: each waiter starts only after the bound record's controller, child and
  group are positively absent, the captured owner identity is read before the
  stop, and cessation evidence is the child's recorded phase time (deterministic
  across processes) instead of a wall-clock poll; the crash test revalidates
  births before reaping or killing.
- New regression tests: a delayed provider holds the grace until its positive
  cessation and a TERM-ignoring provider is KILLed only after grace;
  session-stop tests for a reused initial target (never signalled), a lost
  anchor (replacement inventory never signalled), a failed rescan that keeps
  pending uncertainty, an unknown-birth member (never signalled), a valid
  anchored late descendant (signalled), an unknown identity (retains failure),
  and an unreaped owned zombie (ceases with zero signals).
- Verification (measured, final state): `tests/test_process_identity.py`
  (42 passed, including the two new grace tests); the
  owner-stop/terminate/bind/descendant subset (12 passed); the managed-stop
  FIFO E2E and crash tests (2 passed, three consecutive runs plus the
  reviewer's forced-interleaving repro where the handoff completes before the
  caller reads the owner); full persistent-unit and resource-process suites
  (69 passed, including the seven new session-stop tests); the selected
  reconciliation/owner-stop subset (39 passed); control-plane stop tests (3
  passed); scoped Ruff on the five production files (clean); broad Ruff
  (139 baseline diagnostics, byte-identical identities to HEAD, zero
  introduced — the gate remains an explicit failing blocker, issue #78);
  cumulative full suite once: `uv run pytest -q` → 3425 passed, 244 subtests
  passed in 386 s; server `tests/test_mcp.py` stop subset (6 passed);
  `python -m compileall -q aflow` (clean); `git diff --check` (clean).

## 2026-10-06 — Existing defect recorded: ruff gate fails on a clean tree

- The broad ruff gate (`ruff check aflow tests apps/aflow_app/server/src
  apps/aflow_app/server/tests`) fails on a clean tree with 139 pre-existing
  findings (mostly unused imports/locals in `tests/_support.py`,
  `tests/test_cli.py`, `test_process_identity.py`, and scattered F841s).
  The exclusive-stop CP2 patch adds zero new findings; the scoped CP2 check
  on the five production files passes. Recorded as issue #78.

## 2026-10-06 — Exclusive stop and dead-owner reclamation (issue #76)

- Waiting admission now runs the broker's bounded safe reconciliation for the
  exact resource at most once per control interval (including the initial
  wait), after stop/config controls and before acquisition. A proven-dead
  owner (controller gone with the bound child and process group positively
  absent, or the confirmed host-reboot proof) and dead queued predecessors are
  reclaimed automatically, so the FIFO head advances without an operator;
  controller loss alone, a surviving/reused/unobservable child, revision
  changes, lock contention and corrupt journals all remain held or fail
  closed.
- Harness children now lead their own process group (`process_group=0`) inside
  the controller's owned session, eliminating the race where a `timeout`
  wrapper changes its group after the parent recorded it. Owned teardown
  targets the verified group (TERM, then bounded KILL escalation), and a lease
  completes only when the child and the owned group are both positively gone;
  a wrapper exit with surviving descendants keeps the claim `unconfirmed`.
- The managed unit stop is session-scoped: it verifies the nonce, the live
  controller birth and the session-leader contract, snapshots session members
  in one bounded pass (bounded members/bytes; ambiguity counts as unknown,
  never empty), sends one TERM per verified group with bounded KILL
  escalation, rescan-captures same-session descendants while a birth-matching
  live member anchors the session, and requires positive cessation of every
  captured member before the stopped receipt. Without session proof it falls
  back to the legacy controller-group stop and never claims extra cleanup;
  unrelated sessions and deliberately escaped providers remain untouched and
  explicit.
- Real-process acceptance covers a timeout-shaped wrapper group with a
  long-lived provider (including TERM-ignoring escalation, a surviving
  descendant after wrapper exit, PID reuse and observation failure), an
  unrelated decoy session, and an end-to-end managed stop with two FIFO
  waiters where the head acquires automatically with zero provider overlap;
  a controller crash with a live child keeps the resource occupied until the
  child verifiably ceases. Shutdown remains a no-op and cross-server-restart
  stop tests are retained.
- The README's existing stop statements (unit-level `owner_stop`, UI
  shutdown no-op) remain accurate, so the README is unchanged.
- Verification at that time: persistent-unit and
  resource-process suites (61 passed); selected reconciliation/owner-stop
  admission tests (39 passed); control-plane owner-stop/stop tests (3
  passed); `tests/test_process_identity.py` (33 passed); server
  `tests/test_mcp.py` (58 passed); scoped Ruff on the five production files
  (clean; the broad gate fails with 139 baseline diagnostics, issue #78);
  `git diff --check`; and the cumulative full suite once: `uv run pytest -q`
  → 3408 passed, 244 subtests passed in 393 s. Primary log:
  `.issue76-proof/cp2-verification.log` (fingerprinted with HEAD and patch
  sha256). A later cumulative run exposed one intermittent failure in the
  managed-stop FIFO test (owner teardown raced the waiter's acquisition),
  which the CP2 repair entry above fixes.


## 2026-10-07 — Refuse symlinked intermediate runs before failed-review admission (issue #79 v15)

- Defect remaining after v14, demonstrated by
  `.issue79-proof/reviewer-v15-lineage.py`: in an A→B→C failed-review chain
  (A original worker turn 1 plus failed review, B and C review-only
  failures), moving B's physical run directory out and placing a directory
  symlink at B's recorded run location was admitted at CLI bootstrap and
  managed preview/resume. `_worker_owner_chain` computed
  `dir_exists = is_dir() and not is_symlink()` — true for a symlink to a
  directory — marked the link absent, then followed the linked `run.json`
  through `resumed_from_run_id` to A, so admission bound A's worker receipt
  through an unowned physical dependency, allocated one successor/unit, and
  dispatched a reviewer.
- Fix: `aflow/workflow.py` `_worker_owner_chain` now refuses an existing
  run-directory symlink in the walk before classifying existence, applying
  the absent-source seed, or calling `_chain_predecessor_kind`. A present
  link is not absence and never supplies owned lineage evidence; the
  genuinely-absent current-run prompt-reference seed path, the metadata-link
  refusal, and all other lineage gates are unchanged. No new reader,
  abstraction, fallback, or persistence.
- Tests: `tests/test_control_plane_resume.py`
  `test_daemon_resume_failed_pending_review_symlinked_intermediate_refuses`
  asserts CLI refusal, managed preview `can_resume=false`, managed refusal,
  zero new run/manifest/unit/provider allocations, and unchanged A/C
  artifacts for the symlinked intermediate, paired with the owned control
  admitted at review/budget40, selecting A's exact readable worker, retaining
  the chain under `keep_runs=1`, and replaying one successor/unit on the same
  key.
- Docs: `docs/runtime-behavior.md` and `ARCHITECTURE.md` record that a run
  directory existing only as a symlink (including an intermediate chain run)
  is refused before its `run.json` is followed.

## 2026-10-07 — Retain the validated binding's producer dependencies, ordinal and legacy (issue #79 v14)

- Defect remaining after v13, demonstrated by
  `.issue79-proof/review-v14-retention.py` at `keep_runs=1` for a genuinely
  ordinal-absent owner-history record: the A→B→C→D sequence (A original worker
  turn 1 plus completed repair turn 3 / ordinal 2, B a genuine rejection of
  that unique predecessor turn then an interrupted repair, C completes the
  repair as local turn 1 / ordinal 3 then fails at review) admitted D through
  CLI and managed preview, prepared review/budget40, and created one
  successor/unit. But the v13 retention block extended the preserved chain to
  the producer with a separate ordinal-only scan of the owner's
  `review_rejection_history` (`reviewed_attempt_ordinal = worker ordinal − 1`),
  so when the ordinal was genuinely absent the producer B was pruned, D's
  prompt reported `Worker artifact unavailable`, and a subsequent real
  bootstrap refused with a binding error. The ordinal-bearing control at
  `keep_runs=1` was retained only by that duplicated, unvalidated scan.
- Fix: `aflow/workflow.py` extends the existing strict binding to publish the
  physical dependencies it already validates. `_selected_worker_allowed_plans`
  adds the selected producing rejection's `source_run_id` to an internal
  collector only after the existing relationship/producing-receipt validation
  succeeds, sharing one collection point for the ordinal and the legacy
  ordinal-absent selection. `_bind_worker_receipt` takes an optional
  `required_run_ids` collector and, only after the worker receipt and
  owner-attempt validation fully succeed, publishes the worker owner, the
  validated producer, and the owned links connecting the resumed-from run to
  those owners from the existing chain walk; a refusal publishes nothing. The
  `failed_pending_review_step` preservation block now passes that collector to
  the strict binding and adds the validated set to `preserved_resume_run_ids`
  before `create_run_paths`, replacing the duplicate ordinal-only producer
  scan and its redundant walks. No new selection rule, no loosened validation,
  no global pruning disable; preservation never grants eligibility or
  substitutes for validation, and the now-orphaned `_resume_predecessor_id`
  helper is removed.
- Regression: `tests/test_control_plane_resume.py` adds
  `_inherited_repair_source` (a source that completes an original and a repair
  worker before its reviewer failure, giving a unique predecessor turn) and
  `test_daemon_resume_failed_pending_review_inherited_producer_retained`,
  parameterized for `keep_runs=[20, 1]` and `history=["ordinal", "legacy"]`.
  It produces A/B/C normally with a unique predecessor turn, omits only the
  disposable legacy history's ordinal, and asserts actual CLI/managed
  preparation, the exact readable worker, producer retention/bytes, review
  first, budget40, one same-key successor/unit, and an executed further retry
  after reviewer failure. The existing local-collision and negative controls
  are unchanged.
- Focused verification: 146 `failed_pending_review` tests, 92
  resume/owner/budget tests, 11 `worker_artifact` tests, Ruff, the v13
  retention (both keep_runs), the four v14 observations (ordinal and legacy at
  keep_runs 20 and 1, all retaining the producer, keeping the artifact
  readable, and `next_retry_refuses=false`), and all carried v07–v12
  diagnostics pass.

## 2026-10-07 — Preserve the producing rejection's source run through keep_runs=1 (issue #79 v13)

- Defect demonstrated by `.issue79-proof/review-v13-retention.py` with
  `keep_runs=1`: the A→B→C→D sequence (A original worker, B rejection +
  interrupted repair, C completes repair as local turn 1 / ordinal 2 then
  fails at review) admitted D through CLI and managed preview, prepared
  review/budget40, and created one successor/unit. However, D's runtime
  deleted B before the reviewer dispatched: the prompt reported
  `Worker artifact unavailable`, `producer_retained=false`, and a subsequent
  real bootstrap from D refused with a binding error. With `keep_runs=20`
  the same sequence retained B and all assertions passed. The retention
  chain walked from the resumed-from run up to the worker owner (C) and
  stopped, never extending to the producing rejection's source run (B).
- Fix: `aflow/workflow.py` extends the existing `failed_pending_review_step`
  preservation block. After the chain walk to the worker owner, when the
  selected worker is a repair (attempt ordinal > 1), the owner's
  `review_rejection_history` is scanned for the unique producing rejection
  (scope-local ordinal = worker ordinal − 1). Its `source_run_id` is then
  walked from the worker owner through owned predecessors, adding every
  intermediate link to `preserved_resume_run_ids` before
  `create_run_paths`. No new selection rule, no loosened validation, no
  global pruning disable; the strict binding already validated this
  producer during `_bind_worker_receipt`. (Superseded by v14: the ordinal-only
  producer scan and its redundant walks were replaced by the strict binding
  publishing its validated producer dependencies, so a genuinely
  ordinal-absent legacy producer is retained through the same validated
  relationship.)
- Regression: `tests/test_control_plane_resume.py` adds
  `test_daemon_resume_failed_pending_review_producer_retained`,
  parameterized for `keep_runs=[20, 1]`. It runs the full A/B/C/D sequence
  through real `run_workflow`, managed preview/resume/manifest/
  worker-preparation, an intentional D reviewer failure, and a subsequent
  real retry, asserting exact worker prompt path/bytes, producer
  receipt/bytes, no worker before review, budget40, one idempotent
  successor/unit, and `next_retry_refuses=false`.
- Focused verification: 142 `failed_pending_review` tests, 88
  resume/owner/budget tests, 11 `worker_artifact` tests, Ruff, v13
  retention (both keep_runs), and all carried v07–v12 diagnostics pass.

## 2026-10-07 — Correlate the producing receipt with its selected relationship (issue #79 v12)

- Defect remaining after v11, demonstrated admitted on resume from B
  (CLI bootstrap and managed preview true, one run/successor and unit, review
  prepared at budget40, reviewer dispatched with the exact repair receipt) by
  `.issue79-proof/reviewer-v12-diagnostics.py`: the producing-review helper
  correlated the receipt's recorded rejection only by repair path, accepted
  any decoded post-snapshot and any nonblank transition, and ignored the
  other required receipt relationships. Eleven independent producer-backed
  mutations of only B's disposable producing-review receipt
  (`turns/turn-001/result.json`) each still admitted: the recorded rejection's
  scope, source run, reviewed predecessor ordinal, or checkpoint foreign;
  the receipt's selector or original plan foreign; its `snapshot_before`
  removed; its `snapshot_after` total checkpoint count increased; its
  `chosen_transition` set to `review` instead of the repair worker's step;
  its `NEW_PLAN_EXISTS` condition false; and its return code boolean false.
  Separately, the existing relative-path contract compared raw `Path`
  values, so the equivalent absolute spelling of the same overlay
  (`local_receipt_equivalent_repair`) refused.
- Fix: `aflow/workflow.py` completes the existing shared strict binding in
  `_producing_rejection_review_validates()` (no new recovery path, no
  admission-policy change, no persistence). The receipt's recorded
  `review_rejection` is now correlated field by field with the selected
  owner-history rejection — scope, rejection number, producing
  source/step/turn, checkpoint identity, reviewer selector, reviewed
  predecessor turn/selector/team, and historical repair — never raw object
  equality, so a genuinely ordinal-absent owner-history record stays
  admissible when the uniquely recorded predecessor and producing receipt
  establish the same relationship. A present reviewed ordinal must agree
  with the selected predecessor. The receipt's actual selector must be the
  producing reviewer and its original plan the immutable scope original
  plan. The finalization must be producer-supported: an integer non-boolean
  zero return code, decoded before/after snapshots that agree (the original
  snapshot does not change during a scoped rejection),
  `NEW_PLAN_EXISTS=true`, `MAX_TURNS_REACHED=false`, `DONE` equal to the
  original snapshot's completion, and the produced transition to the selected
  repair worker's actual step. The receipt rejection's repair path, the
  selected rejection's repair path, and the `new_plan_path` are compared as
  repository-resolved identities, so equivalent relative/absolute spellings
  of the same overlay admit while genuinely foreign paths refuse. The caller
  passes the existing selected worker step, scope original plan, selected
  rejection, and predecessor ordinal; the same binding drives CLI bootstrap
  and managed preview/resume through `_bind_worker_receipt()`.
- Valid local, ordinal-absent, and equivalent-repair controls still resume at
  review with the exact worker, budget40, lineage, retention, and same-key
  idempotency. New producer-backed CLI and managed regressions cover the
  eleven demonstrated receipt mutations with real negative preview/refusal
  and allocation/source invariants, plus the equivalent-repair admission
  control with the exact worker artifact.

## 2026-10-07 — Bind the producing receipt to its finalized rejection (issue #79 v11)

- Defect remaining after v10, demonstrated admitted on resume from B
  (managed preview true, one successor and unit, review prepared at budget40,
  reviewer dispatched with the exact repair receipt) by
  `.issue79-proof/reviewer-v11-diagnostics.py`: the new producing-review
  helper accepted the named completed reviewer receipt without checking the
  rejection and finalization it actually produced. Four independent
  producer-backed mutations of only B's disposable producing-review receipt
  (`turns/turn-001/result.json`) each still admitted: its `review_rejection`
  removed, its recorded `repair_plan_path` changed to a foreign overlay, its
  `snapshot_after` set to null, and its `chosen_transition` set to null.
- Fix: `aflow/workflow.py` completes the existing shared strict binding in
  `_producing_rejection_review_validates()` (no new recovery path, no
  admission-policy change, no persistence). A completed reviewer status at the
  named step/turn alone no longer proves the relationship. The producing
  receipt must additionally carry the finalized rejection it actually
  produced: a `review_rejection` naming the same repair overlay as the
  rejection under validation (only that field is compared, never raw object
  equality, so a genuinely ordinal-absent owner-history record stays
  admissible), a decoded post-snapshot, a produced nonblank transition, and a
  `new_plan_path` equal to that overlay (resolved against the repository; the
  consumed overlay need not still exist). Missing or contradictory finalization
  evidence refuses cleanly before any successor, manifest, unit, or provider is
  allocated. The same validated decision drives CLI bootstrap and managed
  preview/resume through `_bind_worker_receipt()`.
- Valid local and ordinal-absent producing-rejection controls still resume at
  review with the exact worker, budget40, lineage, retention, and same-key
  idempotency. New producer-backed CLI and managed regressions cover the four
  demonstrated producing-receipt mutations with real negative preview/refusal
  and allocation/source invariants.

## 2026-10-07 — Bind the selected repair to its producing review (issue #79 v10)

- Defect remaining after v09, demonstrated admitted on resume from B
  (managed preview true, one successor and unit, review prepared at budget40,
  reviewer dispatched with the exact repair receipt) by
  `.issue79-proof/reviewer-v10-diagnostics.py`: the selected repair's
  producing rejection was authorized from ordinal/path metadata without
  binding the relationship to the actual owned producing review. Five
  independent producer-backed mutations each still admitted: the
  rejection's `source_run_id` foreign to the recorded lineage, its
  `review_step_name` not the producing reviewer step, its
  `review_turn_number` with no producing receipt, its `checkpoint_index`
  not the awaiting scope's checkpoint, and the recorded predecessor worker
  removed from the scope's attempt sequence.
- Fix: `aflow/workflow.py` completes the existing shared strict binding (no
  new recovery path, no admission-policy change, no persistence). The
  selected repair must have its predecessor recorded in the scope's
  attempt sequence — a missing or ambiguous predecessor is a refusal, never
  a skip; a legacy rejection without an ordinal may use its reviewed turn
  number only when it uniquely identifies the predecessor. The new
  `_producing_rejection_review_validates()` helper binds the producing
  rejection to its actual owned producing review: the `source_run_id` must
  be the worker's owner run or an owned `resumed_from_run_id` ancestor in
  the recorded lineage, that run must hold a finalized completed reviewer
  receipt (owned `turns`/turn directories, an owned regular receipt) at the
  recorded `review_turn_number`/`review_step_name`, the rejection's
  checkpoint identity must agree with the selected immutable awaiting scope
  when both sides carry it, and a local producing review must precede the
  selected worker in that run. The same validated decision drives CLI
  bootstrap and managed preview/resume through `_bind_worker_receipt()`;
  missing or contradictory producing relationship evidence refuses cleanly
  before any successor, manifest, unit, or provider is allocated.
- Valid original, inherited, local, twice-repaired, and ordinal-absent
  legacy producing-rejection controls still resume at review with the exact
  worker, budget40, lineage, retention, and same-key idempotency. New
  producer-backed CLI and managed regressions cover the five demonstrated
  mutations with real negative preview/refusal and allocation/source
  invariants, plus the ordinal-absent local control through the actual
  successor's first reviewer naming the exact local worker receipt.

## 2026-10-07 — Bind repair identity, owner scope, and owner attempt (issue #79 v09)

- Defects remaining after v08: four bounded semantic gaps, each demonstrated
  admitted on resume from B (managed preview true, one successor and unit,
  review prepared at budget40) by
  `.issue79-proof/reviewer-v09-diagnostics.py`. (1) Repair identity: a repair
  worker was admitted if it named the original plan, a stale same-scope
  overlay from an earlier repair, or any unrelated plan, because the allowed
  active plans were the set of all overlays established by the scope's
  rejection history rather than the exact overlay produced by the rejection
  for which the worker is the repair. (2) Owner scope: the owner's active
  scope was compared only on scope id, checkpoint name, original-plan path,
  and envelope reference, never on checkpoint index, and the envelope
  artifact's bytes were never validated against the recorded hash, scope,
  checkpoint, digest, and canonical form. (3) Owner attempt: the first
  plausible recorded row won — a stale ordinal, a higher contradictory
  ordinal, an ambiguous legacy duplicate, or a mixed malformed-and-absent
  record all passed because no recorded row was required to be the unique
  strong match. (4) Debug dependency: an unconditional `open("/tmp/dbg.txt",
  "a")` in the CLI's resume reconstruction raised `PermissionError` for every
  resume route whenever that scratch path was unwritable.
- Fix: `aflow/workflow.py` tightens the existing strict binding (no new
  recovery path, no admission-policy change, no persistence).
  `_selected_worker_allowed_plans()` (replacing
  `_established_scope_plan_paths()`) binds the selected worker's active plan
  to the original plan for an original worker, or to the exact overlay
  produced by the unique rejection for which the worker is the repair for a
  repair worker (the rejection whose reviewed ordinal is exactly one less
  than the worker's ordinal and whose reviewed worker identity matches the
  predecessor turn; a missing, ambiguous, or contradictory rejection record
  refuses). `_owner_scope_identity_matches()` now compares the scope's
  checkpoint index, and the new `_owner_scope_envelope_validates()` resolves
  the envelope artifact within the owner's run directory and validates its
  bytes through the shared scope-envelope validator (hash, scope id,
  checkpoint index and name, plan digest, canonical encoding). Both are
  applied in strict `_bind_worker_receipt()` mode. `_owned_worker_attempt_recorded()`
  now requires a unique strong match among the scope's recorded attempts:
  exactly one well-formed ordinal equal to the selected ordinal with no
  other recorded ordinal, or, for a legacy owner, exactly one ordinal-less
  attempt with no recorded ordinal anywhere; a malformed present ordinal,
  a recorded ordinal higher than the selected one, an ambiguous legacy
  duplicate, and an owner with no matching worker attempt all refuse. The
  higher-ordinal contradiction check preserves the v07 rule that an earlier
  inherited attempt with a lower ordinal is ignored when the source records
  the exact selected attempt. `aflow/cli.py` removes the debug-file write
  from `_reconstruct_resume_context()` and passes the scope's checkpoint
  index and envelope to the strict binding. The same validated decision
  drives admission, prompt path, and retention.
- Tests: `tests/test_control_plane_resume.py` adds
  `test_daemon_resume_failed_pending_review_v09_binding_refuses`,
  parametrized over the seven demonstrated mutations (owner checkpoint
  index, owner envelope bytes, owner conflicting duplicate ordinal, owner
  ambiguous legacy duplicate, repair worker naming the original plan, repair
  worker naming the stale first overlay after two real local repairs, and a
  mismatched rejection ordinal); each refuses at managed preview/resume
  before any successor, manifest, start request, unit, or provider, with
  preserved A/B source bytes (only the append-only audit journal may grow)
  and zero new runs. `test_daemon_resume_failed_pending_review_v09_controls_admit`
  keeps the two valid producers admitted (two real local repairs selecting
  worker turn 4, ordinal 3, bound to the second consumed overlay; and a
  unique legacy owner attempt with no ordinal) and asserts the exact
  selected worker receipt and its exact intended active plan.
  `test_daemon_resume_failed_pending_review_debug_file_independent` denies
  every `open` of `/tmp/dbg.txt` inside `aflow.cli` and asserts the same
  valid review-first bootstrap still succeeds. The v07 and v08 negative
  tests are repaired: the broad `except Exception: pass` swallowing the
  daemon setup and the preview-exception-only evidence is removed, the
  symlinked-metadata mutation is isolated to its `RepositoryError` boundary,
  and every other mutation now requires a successful daemon setup, a real
  `can_resume=false` managed preview, a raising clean refusal, zero unit
  starts, zero new runs, and preserved source bytes (only the append-only
  journal may grow). A focused probe
  (`.issue79-proof/reviewer-v09-auxiliary.py`) confirms both repaired
  negative tests now fail on an injected independent preview regression
  instead of passing on a swallowed exception.
- Docs: `docs/runtime-behavior.md` and `ARCHITECTURE.md` record the
  exact-producing-overlay repair binding, the owner checkpoint-index and
  envelope-bytes validation, the unique-strong-match owner attempt rule,
  and the removed debug-file dependency. The v07 source-first repair
  behavior and the v08 finalized-worker/active-plan/owner-identity behavior
  are preserved and re-verified.

## 2026-10-07 — Validate worker finalization, active-plan, and owner scope identity (issue #79 v08)

- Defects remaining after v07: three semantic gaps in the strict failed-review
  worker binding. (1) Snapshots were only checked as JSON objects, so an
  empty `snapshot_after={}` or a malformed `snapshot_before` (a non-object such
  as `[1]`) passed as finalized; the produced `chosen_transition` was only
  checked for presence, so a blank string `"   "` passed. (2) The receipt's
  `active_plan_path` was ignored entirely, so an unrelated
  `active_plan_path=<root>/foreign-plan.md` with no original-plan or repair
  evidence was admitted. (3) Owner scope/ordinal evidence was only partially
  validated: a present but malformed `attempt_ordinal` (a non-integer such as
  `"999"` or a boolean `true`) was treated as a legacy absence, and the owner's
  active-scope checkpoint name was never compared to the selected immutable
  awaiting scope, so a contradictory `checkpoint_name` was admitted. Each of
  these was demonstrated admitted on resume from B (managed preview true, one
  successor and unit, review prepared at budget40) by
  `.issue79-proof/reviewer-v08-diagnostics.py`.
- Fix: `aflow/workflow.py` tightens the existing strict binding (no new
  recovery path, no admission-policy change, no persistence).
  `_worker_receipt_is_finalized_worker()` now decodes both worker snapshots
  with `_snapshot_from_review_result()` (an empty or malformed mapping is
  invalid), requires a nonblank string produced transition, and — with a new
  `allowed_active_plans` parameter — binds the receipt's nonempty
  `active_plan_path` to the original plan for an original worker or the
  historical repair overlay established by that scope's recorded rejection/
  repair evidence for a repair worker (an unrelated, missing, or malformed
  active plan refuses; a consumed overlay need not exist or equal the original
  plan). `_owned_worker_attempt_recorded()` now refuses a present but malformed
  `attempt_ordinal` (non-integer, boolean, or below 1) instead of treating it
  as a legacy absence. New `_established_scope_plan_paths()` collects the
  original plan plus the scope's repair overlays; new
  `_owner_scope_identity_matches()` binds the owner's active scope to the
  selected immutable awaiting scope (scope id, checkpoint name, original-plan
  path, and, when present, the exact envelope reference), so an attempts-map
  key alone does not prove matching scope ownership. `_bind_worker_receipt()`
  strict mode loads the owner metadata, checks scope identity, computes the
  allowed active plans, and applies the finalized-worker and owner-attempt
  checks; the ordinary/legacy review-recovery path is unchanged. `aflow/cli.py`
  `_failed_pending_review_step()` extracts the scope's checkpoint name,
  original plan, and envelope and passes them to the strict binding. The same
  validated decision drives admission, prompt path, and retention.
- Tests: `tests/test_control_plane_resume.py` adds
  `test_daemon_resume_failed_pending_review_v08_binding_refuses`, parametrized
  over the seven demonstrated mutations (empty post-snapshot, malformed
  before-snapshot, blank transition, foreign active plan, non-integer ordinal,
  boolean ordinal, and foreign owner checkpoint name); each refuses at managed
  preview/resume before any successor, manifest, start request, unit, or
  provider, with preserved A/B source bytes and zero new runs, and does not
  treat a swallowed preview exception as the sole passing evidence (the resume
  must refuse). The carried-forward local-repair, repeated-review-only, and
  inherited-worker positives remain the over-refusal guards.
- Docs: `docs/runtime-behavior.md` and `ARCHITECTURE.md` record the decoded
  snapshot and nonblank transition requirement, the active-plan binding,
  the owner scope-identity comparison, and the malformed-present-ordinal
  refusal. The v07 source-first repair behavior (valid local repair and
  inherited workers) is preserved and re-verified.

## 2026-10-07 — Complete scoped worker receipt finalization and containment (issue #79 v07)

- Defects in the v06 shared binding: (1) it accepted foreign scope/attempt
  ownership and incomplete or contradictory worker finalization — a worker
  receipt naming an unrelated step, a completed receipt with
  `snapshot_after=null`, a completed receipt with `chosen_transition=null`, a
  nonzero completed `returncode`, an owner whose attempts/active scope were
  re-keyed to a foreign scope, and a conflicting scope-local ordinal all
  passed managed preview and dispatched a reviewer with that exact invalid
  receipt; and (2) it followed a symlinked `turns` parent outside the owned
  run, so a selected turn whose `turns` directory was a symlink escaped the
  owned run. The v06 helper checked only role, completed status, local turn,
  and raw original-plan path; it never bound the selected scope/attempt,
  successful return code, produced snapshots/transition, or the `turns`
  parent, and it left the binding call inside a predecessor-only guard.
- Fix: `aflow/workflow.py` replaces `_worker_receipt_is_valid()` with
  `_worker_receipt_is_finalized_worker()`, which requires a successfully
  finalized worker receipt (integer, non-boolean zero return code, decoded
  before/after snapshot evidence, and a produced selected transition) plus an
  exact step/role/turn and original-plan identity. `_bind_worker_receipt()`
  gains an optional `strict` mode (the proven failed-pending-review route):
  in strict mode it validates the `turns` parent, the selected-turn directory,
  the regular receipt and metadata files (a symlinked `turns` parent, turn,
  result, or metadata file refuses), and — through the new
  `_owned_worker_attempt_recorded()` helper — requires the owner's recorded
  attempts under the same immutable scope to establish the selected
  step/role/turn/ordinal (a missing owner attempt under the resumed scope, a
  foreign re-keyed scope, or a conflicting ordinal refuses rather than falling
  back to a weaker legacy check). Ordinary/legacy review recovery keeps the
  original loose turn/role/status/plan check (`strict=False`) so its behavior
  is unchanged. `aflow/cli.py` moves the binding out of the predecessor-only
  guard so every proven failed-review source (including an initial source
  without a predecessor) is bound, and refuses a source whose owner records no
  worker attempt under the resumed scope. The same strict binding drives
  admission, the exact prompt path, and retention.
- Tests: `tests/test_control_plane_resume.py` adds
  `test_daemon_resume_failed_pending_review_v07_worker_binding_refuses` (the
  seven demonstrated mutations plus a missing owner attempt and a symlinked
  metadata file; each refuses at managed preview/resume or repository
  reconciliation before any successor, manifest, start request, unit, or
  provider, with preserved A/B source bytes and zero new runs). `test_cli.py`
  adds `test_bootstrap_failed_pending_review_inherited_worker_stays_admitted`
  (A produces the checkpoint-1 worker and a failed reviewer; B resumes
  review-first; the managed successor C is admitted at budget40 and names A's
  exact inherited worker receipt, with A/B source bytes preserved and the two
  source runs independent) as the over-refusal guard; the complementary
  local-repair-worker positive control remains
  `test_daemon_resume_failed_pending_review_local_repair_worker`.
- Docs: `docs/runtime-behavior.md`, `ARCHITECTURE.md`, and this DEVLOG record
  the finalized-worker binding, the owner-attempt and scope-ordinal
  requirement, the `turns`-parent symlink escape, and the removal of the
  predecessor-only guard. The v06 source-first repair behavior (valid local
  repair and inherited workers) is preserved and re-verified.

## 2026-10-07 — Validate the latest scoped worker owner, including workers in the source run (issue #79 v06)

- Defects in the v05 binding: (1) admission started at the source run's
  `resumed_from_run_id` (its predecessor), so a genuine new local repair
  worker in the source run was skipped and the resume was refused; and (2) the
  ancestor lookup accepted any JSON object with `step_role == 'worker'` at the
  selected local turn number, admitting foreign-plan, non-finalized,
  wrong-turn, and symlinked receipts. The two cases were handled with
  different rules, so valid local workers and invalid inherited receipts could
  not be decided consistently.
- Fix: `aflow/workflow.py` replaces `_find_worker_receipt_owner()` with a single
  source-first rule. `_bind_worker_receipt()` checks the source run before
  following the `resumed_from_run_id` chain; `_worker_receipt_is_valid()`
  requires producer-supported, successful, finalized worker evidence with an
  exact turn/step/role and original-plan identity (a JSON object or file
  existence proves nothing); and `_resume_predecessor_id()` validates the owned
  predecessor run ID, refusing symlinked, foreign, or cyclic lineage. A
  malformed candidate worker is refused rather than replaced by an older
  worker, and an intermediate review-only run carrying a reviewer receipt at
  the same local turn is walked through to its owned predecessor. The same
  validated decision drives `_review_worker_artifact_reference` (no historical
  single-hop reviewer-file fallback), the `preserved_resume_run_ids`
  retention, and `aflow/cli.py` admission. Invalid or ambiguous evidence
  refuses before any successor, manifest, unit, or provider is allocated.
- Tests: `tests/test_control_plane_resume.py` adds
  `test_daemon_resume_failed_pending_review_local_repair_worker` (A → B
  review rejection → real B repair worker at scope-local ordinal 2 → genuine B
  reviewer failure → C, through real bootstrap, managed preview/resume/
  manifest/`_worker_prepared` with same-key idempotency and budget40, review
  first, the exact latest B worker receipt, ordinary remaining-checkpoint
  progression to a successful outcome, and B source preservation under
  `keep_runs=20`/`1`),
  `test_daemon_resume_failed_pending_review_worker_owner_refuses` (foreign
  plan, non-finalized status, wrong turn number, symlinked turn directory,
  non-object receipt, and malformed JSON each refuse at managed preview/resume
  with no allocation or provider and preserved A/B source bytes), and extends
  `test_daemon_resume_failed_pending_review_repeated_failure_selects_worker_receipt`
  to also cover managed C preview/resume/manifest/`_worker_prepared`, same-key
  replay, B artifact preservation, and successful ordinary progression under
  both retention settings while keeping the exact A worker identity/byte
  assertions.
- Docs: `docs/runtime-behavior.md`, `ARCHITECTURE.md`, and this DEVLOG record
  the source-first receipt validation, the refusal before allocation, and the
  removal of the single-hop reviewer-file fallback.

## 2026-10-07 — Bind and retain the actual worker receipt through repeated failed-review resumes (issue #79 v05)

- Defect: after a resumed reviewer fails again, a second resume selected the
  immediate predecessor's reviewer result as the worker artifact. With
  `keep_runs=1`, it also deleted the original worker's run before review.
  Both effects stemmed from assuming the immediate predecessor owns the
  inherited worker attempt.
- Fix: `aflow/workflow.py` gains `_find_worker_receipt_owner()`, which walks
  the `resumed_from_run_id` chain to find the run that owns a finalized
  worker receipt at the given turn number. `_review_worker_artifact_reference`
  uses this binding for the prompt's artifact reference/location; the
  `preserved_resume_run_ids` section for `failed_pending_review_step` uses
  the same binding to preserve every predecessor up to the worker owner.
  `aflow/cli.py` `_failed_pending_review_step` gains an admission-level check
  that verifies the worker receipt can be bound through the chain before
  admitting the successor; a missing, foreign, cyclic, or symlinked lineage
  refuses cleanly with no allocation or provider call.
- Tests: `tests/test_control_plane_resume.py` adds
  `test_daemon_resume_failed_pending_review_repeated_failure_selects_worker_receipt`
  (A → failed-review B → C, parameterized `keep_runs=20`/`1`, asserting the
  prompt's exact selected file is A's worker receipt with `step_role ==
  'worker'` and byte-identical producer bytes, and A/B source artifacts remain
  available and unchanged),
  `test_daemon_resume_failed_pending_review_repair_worker_precedence`
  (a newer same-scope repair worker takes precedence over an ancestor
  worker), and
  `test_daemon_resume_failed_pending_review_missing_lineage_refuses`
  (missing worker lineage refuses at admission without allocation or
  provider calls).
- Docs: `docs/runtime-behavior.md` documents the worker-owner binding and
  chain-walking preservation.

## 2026-10-07 — Resume a failed reviewer at the pending review instead of the first implementation step (issue #79)

- Defect: when a worker completed its checkpoint, the controller advanced to
  the configured reviewer, and the reviewer harness failed, the run ended
  `failed` with `current_step_name` on the reviewer and the original
  implementation scope still awaiting review. Resume then fell back to the
  workflow's first implementation step (or, for a complete single-checkpoint
  snapshot, was rejected by the candidate mismatch check), so the successor
  re-ran the already-completed implementation first. For multi-checkpoint
  sources the scope-reconciliation path refused the resume outright.
- Root cause: `_bootstrap_resume_invocation`/`_reconstruct_resume_context`
  had no route for a failed terminal source whose last turn is a finalized
  unsuccessful reviewer receipt. `_interrupted_resume_step` only handles
  `environment_preflight`, so the effective start step was the workflow's
  first step, and `_reconcile_verified_resume_scope` treated the retained
  awaiting-review scope as a reconciliation blocker.
- Fix: `aflow/cli.py` gains `_failed_pending_review_step()`, which accepts
  the route only when every artifact agrees — failed status with no
  terminal `end_reason`, the saved current step is the configured reviewer,
  `active_turn == turns_completed + 1`, a finalized `harness-failed` (or
  producer-supported `owner-stopped`) reviewer receipt whose turn/step/role
  match the metadata, and the original awaiting-review scope with a positive
  checkpoint index. `ResumeContext` carries the new
  `failed_pending_review_step` field; the bootstrap selects it as the start
  step, suppresses the already-complete-snapshot candidate rejection only
  for this proven shape, and bypasses the cumulative-review and
  scope-reconciliation paths that would close the retained scope. The route
  yields to the repair, budget, owner-stopped, and cumulative-review routes;
  once the candidate shape is recognized, any missing, malformed,
  unreadable, or contradictory evidence is a clean resume refusal before any
  successor is allocated, while sources outside the candidate shape keep the
  legacy admission behavior (including its clean refusals) and never
  dispatch a provider.
- Tests: `tests/test_control_plane_resume.py` adds a daemon-level positive
  case (producer-backed via the real controller, both single- and
  multi-checkpoint plans) proving the successor starts at the reviewer with
  zero worker invocations, the inherited turn budget, an idempotent resume
  receipt, and untouched source artifacts. A second daemon-level test covers
  `keep_runs=1` retention, conditional review transitions for remaining
  checkpoints, and the rejection → repair → approval progression. `tests/test_cli.py`
  adds `TestFailedPendingReviewResumeRouting` with positive controls
  (including a genuine producer-backed source whose original checkpoint
  remains unchecked) and producer-backed negative mutations
  (missing/mismatched/unfinalized/unknown-status receipt, non-reviewer role
  or step, turn-count mismatch, absent awaiting scope, active source,
  unresolved hotplug, wrong/missing/blank/non-string receipt plan
  identities, invalid/inconsistent post-snapshot, turn-directory and result
  symlink escapes) that must not acquire the route. A managed daemon-level
  identity-negative test proves the invalid receipt produces a clean refusal
  through managed admission with zero successor unit starts and no provider.
  A relocation case maps the historical receipt identities through a
  `ResumeRelocation` and requires the pending review to be recovered against
  the current authorized plan, while an unmapped or out-of-lineage plan is
  refused. Both failed on the baseline and pass with the fix.
- Receipt binding: the route validates that `turns` and the selected turn
  directory are owned directories (not symlink escapes), `result.json` is an
  owned regular file, the receipt's `original_plan_path` and
  `active_plan_path` are both required nonempty strings that match the
  current authorized plan after relocation mapping (a missing, blank,
  non-string, unmapped, or foreign identity is a clean refusal, never
  permission to skip the comparison), the saved snapshot may be the same
  awaiting scope with the original checkpoint unchecked, in which case its
  checkpoint name must equal the scope's immutable checkpoint name, and the
  produced
  `snapshot_after` decodes to a valid snapshot consistent with the source's
  `last_snapshot`.
- Retention: the failed-review predecessor is added to the preserved resume
  run-ID set before `create_run_paths` so `keep_runs=1` does not delete it
  before the first review or during successor completion.
- Docs: `docs/runtime-behavior.md` documents the new admission shape,
  receipt binding, and retention; `ARCHITECTURE.md` records the routing
  precedence.

## 2026-10-06 — Give the macOS Python 3.12 dashboard CI job a 35-minute budget (Refs evrenesat/aworkflow#78)

- On exact SHA `2f15a235b624e7553a456c7bc4e71d02c87fac2e`, CI run
  37538357182 job 112524953238 (macOS, Python 3.12 dashboard) passed 724 web
  tests, reached ~91% of the server tests at 22:26:22 UTC, and was cancelled
  by the 25-minute job limit at 22:31:05 with no recorded assertion failure.
  The same matrix job passed in 21m27s on the preceding `b0b7e7f6`, and
  `c01fb1da` also needed a timeout retry, so the evidence is inadequate
  timing margin rather than a specific slow-test defect.
- `.github/workflows/ci.yml`: the dashboard `timeout-minutes` is now
  `${{ matrix.python-version == '3.12' && 35 || 25 }}`, so macOS 3.12 gains
  the same 35-minute budget Ubuntu 3.12 already had (Ubuntu 3.12 additionally
  runs the WebKit suite); both 3.13 jobs remain at 25 minutes. The matrix,
  `UV_PYTHON` pinning, browser installs, the Linux 3.12 WebKit invocation,
  artifact upload, and every other job are unchanged.
- The dashboard `Run server tests` step now runs
  `uv run pytest -q --durations=20`, printing at most 20 slow-test durations
  as diagnostics. Durations do not alter test selection or outcomes; a
  failing pytest command still fails CI. Rationale: if the 35-minute budget
  is again insufficient, per-test durations localize the remaining cost
  without re-running broad suites for this YAML-only change.
- Delivery remains pending the exact-SHA CI run on `origin/main` and matching
  live p100 SHA/health/readiness; 35 minutes is a conservative finite
  allowance based on observed 21-25+ minute runtimes, not a measured
  completion time. If it still times out, preserve duration evidence and
  diagnose rather than raising the budget again.

## 2026-10-06 — Managed-resume worker fixtures bind the worker bootstrap to their admitted application (issue #55)

- Root cause of the issue #55 macOS CI failures: a test-only composition
  mismatch, not a production defect. `tests/test_budget_exit_recovery.py`
  admits runs through a fixture daemon whose `InMemoryUnitManager` proves
  the exact source unit inactive, but `aflow.daemon.worker_main()`
  recomposes the control plane with the default `SystemdUnitManager`. On
  macOS the unavailable systemctl makes the fresh ownership guard fail
  closed ("current managed inactivity is not proven for the exact source
  unit"), stopping the seven integration cases before the successor
  provider invocation; on a live systemd host the same missing unit reads
  `inactive`/`dead`, masking the platform leak.
- `tests/test_budget_exit_recovery.py`: new `_worker_boot_composition()`
  binds `aflow.daemon.compose_control_plane` to the exact fixture daemon's
  application during each real `worker_main()` invocation, verifies the
  requested repo/config identity, and fails loudly if the scoped boot
  consults `SystemdUnitManager.get` on any host. Applied to the
  drift/clean durable-recovery case (each half receives its own
  application), both historical-shape MCP dispatch variants, and all four
  retained-worker-control variants. New
  `test_worker_boot_refuses_unknown_source_unit_despite_nonce` proves the
  fresh ownership guard still rejects an unknown exact source unit after a
  valid reservation with zero provider dispatch. The scoped native-query
  guard raises `pytest.fail.Exception` (a `BaseException`, not an
  `Exception`), so fail-closed production handlers cannot swallow it into
  an expected ownership rejection on any host. Production behavior is
  unchanged; no production files modified.
- Verification: native-guard acceptance probe exits 0 with restoration
  asserted; `uv run pytest -q tests/test_budget_exit_recovery.py` 37
  passed; scoped `uv run ruff check` and `git diff --check` pass. Adjacent
  `tests/test_resume_checkpoint_repair.py` 172-pass evidence reused
  unchanged.
- Refs evrenesat/aworkflow#55

## 2026-10-06 — Repair the server browser-test lint baseline (Refs evrenesat/aworkflow#78)

- `apps/aflow_app/server/tests`: the eleven `*_browser.py` modules import the
  shared `control_client` fixture from `test_control_plane_api` purely for
  pytest fixture discovery. Made those re-exports explicit with redundant
  aliases (`control_client as control_client`) so Ruff no longer reports
  F401/F811, replacing the now-redundant per-line `# noqa: F401` suppressions
  and the three `# noqa: F811` parameter suppressions in
  `test_run_progress_browser.py`. The exact
  fixture name, lifetime, and collection are unchanged (134 collected node
  IDs identical before/after; `pytest --setup-plan` shows the original
  `control_client` fixture graph).
- Removed genuinely unused imports in the reported modules (`os` in
  `test_launch_confirmation_browser.py`, `TOKEN` in
  `test_launch_confirmation_browser.py` and `test_settings_reload_browser.py`).
- Documented the explicit broad Python lint command
  (`uv run ruff check aflow tests apps/aflow_app/server/src
  apps/aflow_app/server/tests`) in the README validation section. No rule,
  exclusion, fixture, assertion, or runtime changes. That broad command now
  exits 0.

## 2026-10-06 — Repair the Python test lint baseline (Refs evrenesat/aworkflow#78)

- `tests/_support.py`: made the intentional shared export surface explicit to
  Ruff with redundant aliases (`io as io`, `X as X`, ...). The dynamic
  `__all__` and every runtime name are unchanged; the exported-name capture is
  byte-identical before/after.
- Removed genuinely unused local imports at the individual reported test sites
  and dropped unused result bindings (F841) while preserving every evaluated
  call, setup side effect, context manager behavior, and assertion. No noqa
  additions, no rule/exclusion changes, no assertion removals, no fixture or
  collection changes.
- Evidence in `tmp/issue78-python-lint/`: before/after export captures, before
  Ruff JSON (98 findings at base bef7fda6), and the focused pytest log (762
  passed, 225 subtests; plus 172 passed in the adjacent
  `tests/test_resume_checkpoint_repair.py` site repair). `uv run ruff check
  tests` now exits 0.

## 2026-10-06 — Prevent self-matching verification waits

- Execution and review skills now require command exit evidence, foreground or
  exact-child/session waiting, and supported timeout options. They explain why
  broad command-text polling can match itself and keep a finished test waiting.
- Assistant and observer guidance distinguish liveness, concrete progress and
  inference utilization; authorized recovery must verify the exact owned wait
  and resumed progress while preserving controller and lease ownership.
- Skill-only documentation change; no runtime, scheduling or admission change.
  Validate skill documents, inspect scope/authority, and verify installed bytes.

## 2026-10-06 — Fresh managed inactivity now requires an affirmative terminal unit observation (issue #55, cp01-v04)

- `aflow/daemon.py`: `DaemonService._managed_inactivity_check()` no longer
  treats `is_active == False` as an inactivity answer. A present exact unit
  must affirm a terminal inactive observation (`inactive`/`failed`
  active_state) before the admission preview authority is consulted;
  `unknown`, transitional (`activating`, `deactivating`, `reloading`),
  active, foreign, empty/malformed, and unavailable observations all fail
  closed, and historical owner-stop status never overrides a current
  nonterminal observation. The read-only `can_resume` preview now reuses the
  same fresh identity-bound query, so preview, reservation/replay, and
  nonce-bearing worker boot all apply one rule to the exact source and every
  managed-stopped ancestor. `UnitState.is_active` and the unit manager are
  unchanged.
- `tests/test_resume_checkpoint_repair.py`: permanent regressions prove that
  observed `unknown`/`activating`/`deactivating` source units (transitional
  modeled with a live MainPID) give `can_resume=False` and refuse resume
  before allocation in both in-place and worktree modes; that a
  nonterminal/unknown exact unit after a successful reservation makes
  nonce-bearing `worker_main()` return 1 with zero provider dispatch; that a
  stopped successor with an unknown/transitional exact ancestor refuses
  preview/resume before allocation and refuses worker-boot dispatch after
  reservation; and that present exact `inactive` and `failed` units under
  matching managed-stop authority stay admitted. Existing absent-unit
  nonportable source/ancestor positives, portable receipt blocking, pending
  stop, issue #74 lineage, idempotency, and integration compatibility
  coverage are preserved.
- `ARCHITECTURE.md` and `docs/runtime-behavior.md`: the fresh-inactivity
  explanations now require an affirmative terminal observed state and state
  that unknown/transitional observations are never converted into
  inactivity. Refs evrenesat/aworkflow#55

## 2026-10-06 — Managed-stop inactivity is now proven by fresh identity-bound queries (issue #55, cp01-v03)

- `aflow/cli.py`: the checkpoint-repair resume path no longer accepts a
  caller-supplied boolean `managed_ownership_validated` as inactivity proof.
  `_bootstrap_resume_invocation` and `_resume_pending_checkpoint_repair()`
  now take an optional `managed_inactivity_check: Callable[[str], bool]`
  bound to the exact daemon application. The common rule, applied to the
  exact source and every managed-stopped ancestor, is: (1) any portable
  worker evidence that is not `confirmed_inactive()` always blocks;
  (2) otherwise a managed query must succeed when one is available, proving
  the exact source unit is inactive (identity- and ownership-bound via the
  live `UnitManager` plus the same stop preview rule);
  (3) without a managed query the CLI refuses managed-stopped sources with
  no portable receipts, directing callers to managed resume. A failed,
  timed-out, or unobservable query fails closed. A consumed reservation
  nonce proves nothing about unit inactivity: the same query runs again at
  worker boot before any provider dispatch.
- `aflow/daemon.py`: `DaemonService` gained `_managed_inactivity_check()`
  which re-observes the exact `aflow-run-{id}.service` unit through its live
  `UnitManager` and applies the stop-preview inactivity rule, failing closed
  on any observation problem. `_resume_bootstrap` (preview/reservation) and
  `_worker_prepared` (worker boot, via `worker_main`) now thread this
  authority into the shared bootstrap, so preview, reservation, and worker
  boot all re-verify the exact source and managed-stopped ancestors.
- `tests/test_resume_checkpoint_repair.py`: new permanent regressions prove
  a nonportable managed-stopped source (no portable receipts) is admitted by
  the fresh managed query and a real `worker_main()` boot (consumed
  reservation nonce) repairs the interrupted checkpoint then reaches review
  while preserving source bytes, original plan, scope, rejection history,
  lineage, worktree and dirty edits; an exact source unit that becomes active
  after a successful reservation is refused before any provider dispatch;
  a stopped successor with its own inactive receipts and a nonportable
  ancestor is admitted by the managed path (CLI refuses the same evidence
  without authority); and nonportable ancestors that are freshly active,
  foreign-owned, or present with untrusted receipts refuse before allocation
  with zero unit starts and immutable sources.
- `tests/test_control_plane_resume.py`: the legacy-resume test now expects
  the inactivity gate refusal (the gate precedes the schema check), and the
  budget test's direct `_worker_prepared()` call supplies an explicit fresh
  test authority because its stopped source has no portable receipts.
- No checkpoint commits; all changes are left uncommitted on the inherited
  dirty baseline (inherited diff SHA-256
  `9874989a5f9223c4924b3083f8d28cf4db53e140fcdbf214e12fe6e24c40f004`).
  Focused verification: 183 passed
  (`tests/test_resume_checkpoint_repair.py tests/test_control_plane_resume.py`),
  104 passed across the related compatibility modules
  (`test_resume_pending_review.py`, `test_worker_diagnostics.py`,
  `test_control_plane_capabilities.py`, `test_control_plane_reconciliation.py`,
  `test_control_plane_repository.py`, `test_control_plane_services.py`),
  `ruff check` clean, `git diff --check` clean.

## 2026-10-06 — Resume managed-stop killed pending final checkpoint repairs (issue #55)

- `aflow/cli.py`: a managed owner stop that kills the controller mid-repair
  turn leaves run metadata `running`, a complete final-checkpoint snapshot,
  and an unfinalized `starting` worker repair receipt. The checkpoint-repair
  resume classifier now admits exactly that proven shape: managed-stop launch
  phase plus run-journal stop event, cleared stop intent, the rejected
  checkpoint's envelope/rejection/reviewed-attempt bindings, and the active
  `starting` receipt bound to the pending designated repair override (no
  fabricated terminal receipt). The complete-snapshot refusal is opened only
  by this pre-classifier and the authoritative classifier remains the single
  admission gate; a pre-classifier pass that the authoritative classifier
  rejects fails closed instead of falling through to ordinary resume paths.
  The review receipt's `DONE` condition now mirrors snapshot completeness, so
  final-checkpoint repairs with a complete original plan are admitted while
  non-final interrupted repairs keep the exact issue #74 behavior. Daemon
  admission needed no changes: `owner_stopped` was already an admissible
  status, and the CLI bootstrap was the single missing gate. The interactive
  `aflow run` detection path stays conservative.
- `tests/test_resume_checkpoint_repair.py`: new source-shape fixture runs a
  real workflow in a child controller (in-place and worktree), applies a real
  managed owner stop through the daemon, and SIGKILLs the controller inside
  the repair turn, then verifies the produced `running`/`starting` evidence.
  Coverage: pending-repair scope preservation with immutable source bytes,
  successor repair-then-review dispatch with lineage/worktree/dirty-edit
  survival, managed resume idempotency, pending stop intent blocking and
  clearing admission, active unit blocking, and nine focused negative
  damages (missing/external overlay, missing envelope, changed original,
  forged rejection, finalized repair, malformed turn identity, extra turn,
  contradictory status).
- Defect repairs (both reproduced in `.issue55-stop-proof/test_review_probes.py`
  before the fix): (1) `_managed_owner_stop_evidence()` alone was treated as
  inactivity proof, so CLI bootstrap could allocate a successor for a stopped
  source whose portable worker ownership was unknown (`active=None,
  observation=untrusted_receipts`) or live. `_bootstrap_resume_invocation`
  now refuses any non-`confirmed_inactive` portable worker evidence before
  allocation, so the CLI public path, managed preview, and managed resume all
  fail closed; the CLI refusal directs callers to managed resume. (2) The
  `_resume_pending_checkpoint_repair()` lineage walk required every ancestor
  to be `failed`, so a valid stopped first-turn successor (zero completed
  turns) was unrepairable. The walk now admits proven managed-stopped
  ancestors, refuses unproven stopped ancestors without converting them into
  failed evidence, and preserves the issue #74 failed contract.
- `tests/test_resume_checkpoint_repair.py`: new permanent regressions convert
  the probe scenarios into assertions of the repaired behavior: unknown
  portable ownership refusal (managed preview/resume/CLI bootstrap, zero
  allocations, immutable source), live ownership refusal, confirmed-inactive
  receipts repairing then reviewing, a stopped first-turn successor repairing
  then reviewing with inherited rejection/overlay/worktree/dirty edits
  (in-place and worktree, child-controller second stop), and mixed
  stopped/failed lineage admission plus unproven-stopped-ancestor refusal.
- `tests/test_resume_pending_review.py`: the pending-review fixture now writes
  a fully valid terminal unit receipt (with `at`), so the ownership gate sees
  proven inactive ownership for the pending-review contract.
- Docs: `ARCHITECTURE.md` and `docs/runtime-behavior.md` describe the
  stopped-shape admission, the ownership gate, and successor behavior.
- Verification: `uv run pytest -q tests/test_resume_checkpoint_repair.py
  tests/test_control_plane_resume.py` (151 passed) plus the resume, repair,
  exclusive-execution, CLI and guard-daemon suites (354 passed); Ruff, git
  diff --check. See `.issue55-stop-proof/verification.log`.

Refs evrenesat/aworkflow#55

### 2026-10-06 — cp01-v02: common stopped-run inactivity ownership rule

- Defect repair (reproduced in `.issue55-stop-proof/test_review_v02_probes.py`
  before the fix): two directly related paths still admitted a stopped run
  without current inactivity proof. (1) `_bootstrap_resume_invocation()`
  checked portable ownership only when `worker_evidence()` returned a mapping;
  a managed-stopped source with no portable receipts (no units directory)
  passed even when its managed unit was active. The CLI now refuses a
  managed-stopped source without portable receipts and directs the caller to
  managed resume; the managed caller passes `managed_ownership_validated=True`
  after validating unit state, preserving legitimate managed nonportable
  resumes. (2) The `_resume_pending_checkpoint_repair()` ancestor walk admitted
  stopped ancestors using `_managed_owner_stop_evidence()` alone; it now
  requires confirmed-inactive portable evidence for every managed-stopped
  ancestor. Absence of trusted receipts is never converted into confirmed
  inactivity.
- `aflow/cli.py`: added `managed_ownership_validated` parameter to
  `_bootstrap_resume_invocation()`; the no-portable-receipt refusal applies
  only to the stopped-repair shape (failed sources and other terminal shapes
  keep their existing contract). The ancestor walk in
  `_resume_pending_checkpoint_repair()` now calls `worker_evidence()` and
  `confirmed_inactive()` for each managed-stopped ancestor.
- `aflow/daemon.py`: `_resume_bootstrap()` and `_worker_prepared()` pass
  `managed_ownership_validated=True` to preserve managed nonportable resumes.
- `tests/test_resume_checkpoint_repair.py`: the `stopped_repair` fixture now
  writes stopped unit receipts (the normal shape after a clean managed stop);
  `_stop_stopped_successor()` writes receipts for each new stopped run. New
  permanent regressions: (a) no portable receipts + stopped source → CLI
  refuses actionably; (b) no portable receipts + active managed unit → both
  managed and CLI refuse with zero dispatch; (c) stopped successor with
  confirmed-inactive receipts + ancestor with no receipts → refuse; (d)
  stopped successor with confirmed-inactive receipts + ancestor with untrusted
  receipts → refuse. Existing negative tests updated to clear fixture receipts
  before simulating unknown/live ownership.
- Docs: `ARCHITECTURE.md` and `docs/runtime-behavior.md` describe the common
  ownership rule, the CLI limitation for no-portable-receipt stopped sources,
  and the ancestor inactivity requirement.
- Verification: `uv run pytest -q tests/test_resume_checkpoint_repair.py
  tests/test_control_plane_resume.py` (171 passed); `uv run ruff check
  aflow/cli.py aflow/review_repair_resume.py aflow/daemon.py` (clean);
  `git diff --check` (clean).

Refs evrenesat/aworkflow#55

## 2026-10-06 — Resume interrupted rejected-checkpoint repairs (issue #74)

- Bind a pending checkpoint repair to the retained scope, captured original
  checkpoint bytes, exact rejection and reviewed attempt, owned overlay,
  terminal worker receipt and consumable designated repair override before
  treating the original snapshot as scope progression. Preserve the envelope,
  rejection count/history, original ledger, dirty edits and predecessor receipts.
- Retain the immutable predecessor chain when successor startup prunes older
  runs, including repeated interruptions before the next repair turn finalizes.
  Fully checked overlays, forged attribution, live sources and unresolved
  partition/control state continue to fail closed.
- Regression fixtures run real implementation/reviewer turns through two
  rejections and a terminal AFLOW_STOP, then verify read-only preview,
  managed bootstrap/idempotency and actual upgraded repair/reviewer dispatch
  in both in-place and worktree execution, with checkpoint and flat overlays.
- Verification: targeted resume, repair-upgrade and managed-resume suites;
  production Ruff, compileall and git diff --check.

## 2026-10-06 — CLI fallback fixture isolates ROOT cwd (issue #69)

- `tests/test_concierge_chat_tick.py`: `TickTestCase.setUp` now captures
  `ROOT` in the existing `_orig` dictionary and points it at the per-test
  temporary directory, so supervised CLI fallback children launch from a
  test-owned working directory instead of the production checkout path
  (which does not exist on CI). The existing restoration loop restores
  `ROOT` before the temporary directory is cleaned up; `CLI_FALLBACK`
  restoration in the fallback tests' `finally` blocks is unchanged.
- `test_fallback_child_runs_same_chat_args_and_stdin` now also has the real
  shell child write `pwd -P` to a separate `child-cwd.txt` and asserts the
  resolved child cwd equals the resolved fixture directory (resolved paths
  keep the assertion portable across macOS temporary-path aliases). All
  existing arguments/stdin assertions and both budget tests' real-child
  execution are preserved unchanged.
- No production, configuration or CI changes.
- Verification (all passing): `uv run pytest -q
  tests/test_concierge_chat_tick.py`; `uv run ruff check
  tests/test_concierge_chat_tick.py`; `git diff --check`.

## 2026-10-05 — Shared prose-aware MCP credential filter (issue #52)

- Added pure `aflow/mcp_credentials.py` with `contains_mcp_credential`, the one
  recursive detector used by both the HTTP prevalidation middleware and the
  shared `_tool_result`/`_resource_result` guards. It replaces the duplicated
  `bearer <word>` regexes in `main.py` and `mcp_control_plane.py`.
- The policy keeps the existing `token=`/`access_token=`/`authorization=`
  assignment/query rule, rejects explicit `Authorization: Bearer …` headers
  (including serialized quoted labels), whole `Bearer <value>` strings, and
  token-shaped candidates carrying digits or internal token punctuation, while
  accepting ordinary credential-handling prose such as “Keep bearer
  credentials out of prompts” and “explicit environment bearer support”.
- `tests/test_mcp_credentials.py` covers each rule, the exact #52 sentences,
  NBSP/tab/punctuation/mixed-case variants, nested mapping/list/tuple/set
  recursion, direct `_tool_result`/`_resource_result` benign success and exact
  `operation_rejected` rejection with zero operation invocation. Mounted
  `tests/test_mcp.py` adds `test_mcp_credential_prose_authoring_parity` and
  `test_mcp_credential_payload_rejection` on both `/mcp` and `/mcp/`; the
  rejection test asserts no synthetic value in responses, captured logs or the
  fixture audit output, zero unit launches, rejected updates leaving bytes and
  revision unchanged for every negative form, and an actual nested JSON
  argument rejected at HTTP prevalidation.
- Verification (all passing): `uv run pytest tests/test_mcp_credentials.py -q`;
  `uv run --directory apps/aflow_app/server pytest tests/test_mcp.py -q -k
  'credential_prose or credential_payload or stateless_http_auth or plan_authoring
  or trailing_slash'`; `uv run ruff check` over the three touched source files;
  `git diff --check`.

## 2026-10-05 — Exclusive execution resources: durable FIFO broker and neutral waiting status

- Model-switch admission tests inspect one durable wait snapshot per polling
  predicate. Cancellation can clear the old wait before requeueing, so reading
  separate snapshots for the presence guard and resource assertion races that
  normal transition. Exact resource and pre-admission no-dispatch checks remain.
- Harness profiles can mark a resolved `(harness, model, effort)` combination
  as exclusive (`exclusive = true` in `aflow.toml` or Settings → Agents & Roles'
  `Exclusive` checkbox). The derived identity excludes profile, role, team,
  and project, so marked aliases share one account-local capacity-one FIFO
  resource per host/account, and any model/effort edit changes identity.
- `execution_resources.py` adds the durable broker: nonce-checked
  `queued -> reserved -> launching -> running -> removed` claims bound to
  controller process lifetime, positive-evidence release, conservative
  `unconfirmed` owner diagnosis, and fail-closed journal handling. Deleting
  journal files or restarting a controller is not safe release evidence.
- Workflow turns and auxiliary model calls gate on the shared control-aware
  admission loop (owner stop and live config revalidation serviced while
  queued; cancel-and-reprepare on live changes); probe-free paths such as
  git-only fast-forward merges never consume a resource. A busy selected
  worker waits without moving through the upgrade chain.
- Final-review repairs confirm claim withdrawal before rerouting and order
  manager launch intent before start/correction accounting. The paired-controller
  matrix uses real IPC-gated local harness children in disposable Git worktrees,
  verifying child binding/reaping, FIFO yielding and independent review overlap.
- CI follow-up preserves the three-argument owned-session interface for
  unmarked drivers and passes marked leases through the optional `lifecycle`
  keyword, including keyword-only and `**kwargs` drivers.
- Process-lifetime tests synchronize owner stop with the local child's
  readiness signal and identify failed registration through its actual bound
  PID, retaining the live-child and reaped-child assertions on slower hosts.
- Waiting-status browser checks retain exact scroll equality and publish
  before/after geometry and screenshots to CI artifacts, including failure
  capture for investigating the intermittent macOS tablet result.
- macOS CI captures identified an initial-load measurement race: the test
  took its scroll baseline while history and checkpoint detail still showed
  loading notices. The unchanged-refresh check now waits for initial dashboard
  admission and populated checkpoint detail before opening the result and
  measuring scroll; exact equality, retained focus/disclosure and zero-launch
  assertions remain in force.
- Admission now publishes the allocated queue ticket when an enqueue retries
  successfully after journal-lock contention, replacing its provisional wait
  record. A deterministic real-lock regression covers that transition; the
  subsequent-round fairness check waits for ticket 3 before asserting it,
  while preserving the worker-call and release ordering checks.
- The independent-process broker test receives its acquisition record before
  releasing the first holder; a process readiness event alone does not flush
  the multiprocessing queue. Joined producers' final receipts and successful
  exits remain part of its exclusion and ordering checks.
- The settings browser journey waits for an enabled exclusive checkbox before
  keyboard activation, then asserts the checked state and enabled Save action.
  A controlled delayed initial read reproduced the prior no-op Space input on
  a disabled form; persistence, untouched-row and no-launch checks remain.
- The controller persists an `execution_resource_wait` record with private
  controller identity in `run.json`; the bounded, redacted read-only projection surfaces a neutral
  `Waiting for resource` status with the exact resource label only while the
  controller is active and running, with terminal/stop/unknown precedence.
  The web run rows, All runs, and Current work render the same message;
  acquisition clears it without remounting populated content.
- Docs: runtime-behavior and cli-usage sections, architecture boundary
  record, and a README configuration pointer.

## 2026-10-05 — Explicit managed successor budgets (issue #71)

- Added optional strict positive `successor_max_turns` to REST and MCP resume.
  Persist the choice across idempotent replay, uncertain publication and worker
  boot; reject changed requests and invalid values before reservation.
- The successor budget supersedes inherited budget choices while preserving
  source files, explicit role selectors and existing admission/control checks.
- Regression coverage exercises a real 48-turn source and one-turn successor:
  rejected review stops before another implementation worker. Omitted budgets
  retain the existing continuation behavior. REST/MCP contracts exercise real
  bootstrap, ordinary resume and durable recovery.

- Integration preserves the existing deployed recovery commit: a lost worker
  is inactive only when nonce-bound wrapper, child and process-group probes
  all positively confirm absence. Uncertain or present processes still block.
- Verified locally: 78 focused runtime tests, 153 REST/MCP contract tests,
  production Ruff checks and Python compilation passed.

Refs https://github.com/evrenesat/aworkflow/issues/71.

## 2026-10-05 — Storage and evidence hygiene in bundled skills

- Added a compact role-appropriate "Storage and evidence hygiene" section to
  the eight bundled skills (`aflow-plan`, `aflow-execute-plan`,
  `aflow-execute-checkpoint`, `aflow-review-checkpoint`, `aflow-review-final`,
  `aflow-review-squash`, `aflow-assistant`, `aflow-manager`): planned output
  locations and conservative peak/reserve estimates, bounded worker recovery
  before `AFLOW_STOP`, owned-disposable cleanup boundaries, inspectable compact
  proof retention, evidence reuse by reviewers, and read-only manager
  supervision. Existing workflow contracts are unchanged.
- Instructions are generic: no hostnames, inventories, credentials, incident
  records, or machine-specific paths in the public bundled skills.
- Verified: `uv run pytest -q tests/test_skill_store.py tests/test_skill_install.py
  tests/test_skill_refresh.py` passes; `git diff --check` passes.

## 2026-10-04 — Canonical worker-artifact integration fixture roots (issue #70)

- Resolve the fixture root before deriving primary-repository and worktree
  paths, matching macOS `/var` and `/private/var` aliases without changing the
  production artifact behavior delivered by #64.
- Reuse the complete workflow scenario through an unresolved directory symlink.
  Before the correction, it reproduced the worktree cwd containment failure
  after both real reviewer subprocess reads; all original assertions remain.
- Verified: 18 focused runtime tests and all 12 pending-review tests pass;
  `git diff --check` passes. Remote CI and p100 activation remain pending.

Sources: https://github.com/evrenesat/aworkflow/issues/70,
https://github.com/evrenesat/aworkflow/issues/64.

## 2026-10-04 — Checkpoint reviewers receive the exact worker-artifact read location (issue #64)

- `_CheckpointReviewPromptTarget` now carries the selected worker result's
  concrete `Path` alongside its unchanged primary-relative reference, and
  `_review_worker_artifact_reference` returns the pair. Locations are computed
  only from existing controller-owned paths: matching worker turn-history
  records, the current `run_dir`, the validated `resumed-from` predecessor for
  active scopes; the typed `source_run_dir` for pending cumulative reviews and
  recovered finalized boundaries; and the evidence `source_run_dir` for
  scope-less recovery. A malformed or unbound source identity yields an
  explicitly unavailable location, never another source.
- Resolved and ambiguous review contexts share one renderer that keeps the
  `- Worker artifact reference:` line, appends
  `- Worker artifact path (read this exact file): <absolute path>` with a note
  that it is the read location independent of the execution directory, and
  states explicit `Worker artifact unavailable` when the expected file is
  absent or uninspectable. Availability never changes target selection,
  ambiguity, or review transitions; non-reviewer prompts are unchanged. Both
  normal and retry prompt assembly use the same rendering.
- New `worker_artifact` regressions in `tests/test_runtime.py` cover matching
  history with a decoy under the current run, current-run-file fallback,
  missing plain-turn fallback, resumed fallback, malformed resumed source,
  missing selected history despite a plausible replacement, recovered
  finalized worker, scope-less predecessor, and ambiguous target with an
  existing artifact; each resolves the emitted path and reads/asserts the
  selected result bytes. An injected-runner worktree regression asserts the
  reviewer cwd is the execution worktree, extracts the path from the actual
  prompt, opens the primary result through a subprocess rooted at that cwd,
  and verifies worker turn/role and unique content on both the ordinary and
  inconsistent-plan retry invocations with the same target.
- The same-root and relocated pending-review tests in
  `tests/test_resume_pending_review.py` now have their actual reviewer runners
  read the exact predecessor artifact bytes, with conflicting sibling-run and
  old-root decoys present during the read; source hashes and relocation
  assertions are preserved.
- Verified: `uv run pytest -q tests/test_runtime.py -k 'worker_artifact or
  reviewer_prompt or checkpoint_review_context or worker_review_loop or
  scope_less_resume'`, the full `tests/test_resume_pending_review.py`, the
  cumulative resume suites, `uv run ruff check aflow/workflow.py`, and
  `git diff --check` all pass. Live acceptance on the next already-authorized
  managed review remains pending for issue #64.

Source: https://github.com/evrenesat/aworkflow/issues/64.

## 2026-10-04 — Budget-only controls preserve an existing worker hotplug (issue #68)

- `workflow.py::_apply_boundary_override` now compares each supplied role
  against the current effective routing (run-local `role_selectors` first,
  then the ordinary role resolver) before deciding whether a nonterminal
  transaction conflicts. A retained worker selector accepts the budget
  revision without creating a duplicate transaction, incrementing its number,
  or emitting another requested event. A genuine selector change still
  triggers the all-or-nothing rejection.
- Worker-transaction creation is gated on an actual worker-selector change as
  well as the existing execution-change logic. A retained selector skips
  creation even when an active session still names the old source.
- Hotplug regressions cover retained-selector acceptance, genuine and
  mixed-map changes, and stale source sessions without duplicate switches.
- The managed integration regression now creates the worker switch through
  revision-checked MCP control, runs successful target work without sessions,
  applies a budget-only revision retaining that worker, and reaches the real
  budget exit with independent final review still pending. It then uses actual
  MCP resume and worker bootstrap to dispatch the configured final reviewer.
  The fake external reviewer explicitly stops instead of fabricating approval;
  no merge or publication occurs. Canonical and symlinked temporary roots,
  idempotent replay, and retry after an inactive zero-turn failed successor
  preserve source records, the original worktree/branch and transaction.

Source: https://github.com/evrenesat/aworkflow/issues/68.

## 2026-10-03 — Canonical budget-recovery fixture roots for macOS CI (issue #62)

- The reviewed issue #62 repair failed both macOS CI Python 3.11/3.12 test
  jobs: macOS exposes `TMPDIR` through the `/var` -> `/private/var`
  directory symlink, so `tests/test_budget_exit_recovery.py` built fixture
  roots from the unresolved temporary path while `_bootstrap_resume_invocation`
  and `RunRepository` canonicalize their inputs. Twelve bootstrap tests
  rejected their own runs as belonging to a different repo root, and managed
  MCP dispatch compared a canonical `/private/var/...` worker root against
  the fixture's `/var/...` root.
- All 31 temporary-root constructions in the module now apply `.resolve()`
  before any repo, config, plan, worktree, or provider path is derived, so
  every fixture identity reference shares the same canonical spelling the
  production entrypoints supply. No production code changed.
- Added the module-local `budget_temp_parent` fixture (canonical and symlink
  parameters) that creates a real directory symlink under the pytest
  `tmp_path` and routes `test_single_step_budget_exit_starts_work` and
  `test_mcp_durable_recovery_dispatch_of_historical_shape` through both
  spellings. The two tests now exercise real explicit bootstrap and real
  registered MCP dispatch under a symlinked temporary parent without
  depending on the host `TMPDIR` or mutating `tempfile.tempdir`.
- Verified on Linux: the full module passes normally, under the new
  canonical/symlink parameters, and under a fresh-process aliased `TMPDIR`
  reproduction. Exact-SHA CI (including both macOS test jobs) and the
  existing deployment gate remain outstanding delivery checks for the
  coordinator.

## 2026-10-02 — Keep unfinished budget boundaries out of delivery (issue #62)

- `_finish_normal_terminal` in `aflow/workflow.py` now takes an explicit
  `delivery_eligible` decision and checks it before any merge teardown, so
  an unfinished budget boundary performs no merge, no publication, and no
  done-plan move. `_perform_merge_teardown` and `_deliver_completed_plan` are
  both gated on it; delivery additionally requires a complete snapshot.
- A pre-turn budget cap always exits with `delivery_eligible=False`: reaching
  the limit can never approve an outstanding configured review or repair
  boundary, even when the ledger is already complete. A selected `END` that
  only matches because `MAX_TURNS_REACHED=true` (re-evaluating the same
  configured condition with `MAX_TURNS_REACHED=false`) is the same budget
  exit, including when the worker just completed the ledger; a fully reviewed
  completion whose ordinary non-budget transition reaches `END` still
  delivers normally at the numerical limit.
- The finalized-boundary replay applies the same rule to a saved budget-only
  `END` receipt and raises on an incomplete non-budget `END` snapshot instead
  of inferring state, so the replay path cannot merge unfinished work.
- Terminal accounting is preserved: live budget exits keep `status=completed`
  with the truthful `end_reason=max_turns_reached` (previously a budget-only
  `END` with `DONE=true` could be serialized as `done`), original/active/new
  plan paths, the final snapshot, and the pending review/repair step remain in
  `run.json`, and merge/publication markers stay absent when no delivery was
  attempted. Static-run failure contracts (pre-turn cap failure, incomplete
  non-limit `END` failure) are unchanged.
- `tests/test_budget_exit_recovery.py` adds fixture-based controller
  regressions: clean worktree with committed unfinished code, dirty
  incomplete worktree, reviewer repair overlay at the cap, complete ledger
  with the configured review still pending at a pre-turn cap, normal reviewed
  completion at the limit (delivery proceeds), and a finalized replay of an
  incomplete budget-only `END`. All unfinished cases spy on the lifecycle and
  publication entrypoints, assert zero calls, and verify files, branch,
  worktree, and plan locations remain intact.

## 2026-10-02 — Resume validated budget boundaries through managed APIs (issue #62)

- New `aflow/budget_resume.py` is the single shared classifier/reconstructor
  for unfinished budget boundaries. It is used by the explicit resume
  bootstrap, the daemon read-only `can_resume` preview, and managed launch
  revalidation; the automatic candidate scanner never consults it. It admits
  only two shapes: a new no-delivery budget exit (`status=completed`,
  `end_reason=max_turns_reached`) and the one narrow historical merge-failure
  shape (`status=failed`, incomplete strict original snapshot,
  `end_reason=max_turns_reached`, `merge_status=failed` with the canonical
  clean-state preflight signature, and an intact recorded execution
  branch/worktree). Everything else returns `None` and falls through to the
  existing completed/failed policies.
- The classifier re-validates the exact last finalized receipt (turn equal to
  the completed/active turn, successful finalized result, same snapshot,
  source step/role/selector, exact original/active/new plan paths, strict
  booleans, no later or in-flight turn) against the current configured graph.
  A saved `END` must be uniquely matched and selected by the saved conditions
  only while `MAX_TURNS_REACHED` is true; a saved nonterminal next edge stays
  authoritative. `NEW_PLAN_EXISTS=true` binds the owned repair overlay, which
  the successor uses as its active plan. A missing `review_rejection` credit is
  not an error and is never fabricated.
- The validated descriptor rides in `ResumeContext.budget_continuation`,
  distinct from generic incomplete manager-boundary replay. The successor
  re-selects only the budget-sensitive saved edge with unchanged
  `DONE`/`NEW_PLAN_EXISTS`; there is no replayed provider/reviewer call and no
  replay of a completed source manager decision. Ambiguous or incompatibly
  changed graph evidence is rejected with a bounded reason before any provider
  launch.
- Successor budget resolves through the existing precedence (accepted run max
  override, then explicit invocation limit, then live default) and starts at
  zero: the source turn count is evidence, not turns already spent. A historical
  48/4 shape starts with `MAX_TURNS_REACHED=false` and at most four new turns;
  the override is never erased nor silently restored.
- Threaded through the daemon read-only `can_resume`, ordinary explicit resume,
  and explicit durable-evidence replacement admission without bypassing any
  liveness, unit/nonce/project/caller/workspace/scope, or idempotency check.
  The predecessor stays immutable (only the existing append-only request-audit
  events may be appended). Worktree runs bind their in-worktree active plan as
  an absolute worktree-file evidence reference re-verified against the bound
  workspace fingerprint, since that file lives outside the primary checkout.
- `tests/test_budget_exit_recovery.py` adds integrated temporary-repository
  regressions using the real canonical bootstrap and controller: explicit resume
  of a pre-turn cap starts the reviewer first, explicit resume of a reviewer
  overlay starts the repair, the classifier rejects a reviewed completion and
  tampered shapes, automatic scanning never classifies a budget exit, and a
  daemon durable-evidence recovery of the historical 48/4 shape produces one
  successor/one launch with the repair worker reading the exact overlay and no
  premature merge. The daemon preview and resume admit a budget exit and reject
  a reviewed completion.
- No new public API or UI change; setup and flags are unchanged. Selector
  restoration uses existing controls only.
- Repair (cp02 review finding): `_recovery_active_plan_path` preferred the
  primary checkout's active plan whenever that file existed, so a worktree
  resume bound and fingerprinted the primary copy instead of the worktree
  plan the successor reads. Because the boundary syncs the worktree plan
  back to the primary checkout after every turn, both files hold equal
  bytes at admission and post-admission worktree plan drift left the
  workspace fingerprint unchanged, defeating the prelaunch rejection. The
  mapper now always binds the recorded worktree mirror for worktree runs
  and rejects missing or ambiguous mappings before reservation; branch and
  no-worktree behavior is unchanged. A dedicated regression proves the
  worktree-plan binding, the post-admission drift rejection with no
  provider call, and the unchanged plan reaching the pending reviewer.
  Also stabilizes one pre-existing launch-manifest assertion in the MCP
  dispatch regression whose sorted-actual list was compared against an
  unsorted source/successor expectation, a coin flip on the random
  run-id suffix.
- Repair (cp02 review finding): the classifier's numeric-equality branches
  admitted boolean impostors because Python evaluates `True == 1` and
  `False == 0`, so a finalized receipt with `"turn_number": true`, a run
  record with `"active_turn": true`, or a receipt `"returncode": false`
  passed the strict finalized-turn contract on a single-turn budget exit.
  The receipt turn number and a present `active_turn` now require a strict
  positive integer before the equality comparison, and the successful
  finalized returncode requires a strict integer zero; the remaining
  completed-status, snapshot, selector, condition, transition, and
  workspace validations are unchanged. The existing tamper regression now
  proves `turn_number: true`, `active_turn: true`, and `returncode: false`
  each reject through the classifier, the read-only preview, explicit
  bootstrap, and managed replacement before any successor reservation or
  provider launch, while the intact single-turn and historical fixtures
  stay admitted everywhere.

## 2026-10-02 — Establish plan relevance before startup ambiguity (issue #63)

- `_candidate_match` in `aflow/control_plane/startup_context.py` now decides
  relevance before ambiguity. A candidate run is potentially relevant when its
  normalized recorded plan path equals the selected path, a recorded identity
  field claims the selected identity, or its durable ownership history
  contains the selected identity. A different recorded path with no recorded
  identity claiming the selection and a successful ownership lookup excluding
  it is unrelated - including an empty owner set and multiple other owners -
  so it can no longer make every selected plan startup ambiguous, even with a
  foreign explicit identity, an identity-less legacy run, dirty preserved
  work, or an active controller on the other plan.
- Ownership reads distinguish an unavailable lookup from a verified result:
  `plan_identity_alias_owners` and `plan_identity_lexical_owners` return
  `None` when ownership cannot be established (missing or malformed
  provenance, an unresolvable or invalid alias) and an empty `frozenset`
  after a successful lookup with no owner for that spelling. An unavailable
  lookup is not proof of disjointness and stays related/uncertain. Ordinary
  safe paths use alias ownership; the lexical lookup applies to a recorded
  suffix crossing a symlink below the verified root, and a symlink never
  borrows its target's durable ownership. For potentially relevant runs the
  conservative decisions are preserved: conflicting identity fields, shared
  or reused aliases, symlink-crossing paths, and unavailable ownership all
  stay `review_previous_run` or `prior_work_unverified`. The identity-less
  Git Tracking fallback and the bounded scan behavior are unchanged, and the
  same matching decision feeds both projection and admission.
- `tests/test_startup_context.py` gained focused regression coverage for the
  new unrelated decisions and the preserved conservative decisions, and
  `tests/test_startup_plan_relevance.py` adds end-to-end coverage with a real
  repository, real multi-plan history, and the real `prepare_startup` /
  `require_safe_fresh_worktree` path (only the unit/provider boundary is
  faked): disjoint history stays selectable, protected same-plan work blocks
  before any provider call or worktree creation, idempotent startup replays
  the same run, capacity rejection stays separate from history relevance, and
  unrelated startup never rewrites the protected plan's durable records.

## 2026-10-02 — Measure launch-review refresh stability independently of screenshot restore

- The `test_ui_demo_new_run_background_preflight_preserves_review` helper
  restored every full-page screenshot to the initial review's `scrollY` and
  waited for exact equality. The intentionally changed and failed preflight
  phases add height and can clamp the document, so the original offset became
  unattainable and the exact `scrollY ===` wait timed out in CI (observed on the
  1280x720-light case: initial `scrollY` 1451 versus the failure phase's
  document maximum 1215). The measurement also conflated screenshot side
  effects with the product refresh.
- The test now captures three distinct bounded samples (phase, current
  scroll, document maximum scroll, and focus token) for every full-page
  capture: immediately before the screenshot, immediately after it returns
  and before any restoration, and after the clamped restore. All three
  samples plus the capture's own target are stored in the artifact manifest
  as `capture_diagnostics`, so screenshot composition movement is visible
  instead of being hidden by restoration. Restore belongs only to screenshot
  handling: it instant-scrolls back to that capture's own position clamped to
  the current document range with a one CSS pixel tolerance, and verifies
  focus was not stolen (never refocusing to hide a refresh regression).
  Every held background read — including the unchanged phase — establishes a
  real settled baseline after prior screenshot handling and immediately before
  the held request; the pre-screenshot `before` snapshot remains in the
  manifest only for diagnostic comparison. The unchanged phase keeps exact
  before/after scroll and focus, stable review geometry, unchanged visible text,
  no replacement/visibility/subtree mutations, and unchanged inputs, while
  changed/failed phases keep their warning, disabled-start, retained
  last-inspection/height, mounted-node, focus and draft assertions and allow
  the intentional changed-content geometry. No scroll is clamped or reset
  during the measured refresh interval.
- Both Chromium and WebKit pass all three viewport/theme cases. In every
  captured manifest the three samples agree for the unchanged phase, and the
  equal phase's baseline matches the state its own screenshot restore left
  behind (desktop light: baseline/held/settled `scrollY` 1451); the failure
  phase target is clamped to the document maximum (1216 desktop light, where
  the document shrank after the blocking warning). Focus is preserved across
  every capture, exactly one start request was intercepted with no started
  unit, and page errors are empty. The before/equal/changed/failure captures
  keep the same populated review node mounted; the preflight root grows with
  the changed and failed cues (304, 402, 476 px desktop light) and the
  reviewed draft text is unchanged in all phases.

## 2026-10-02 — Keep historically owned recorded paths related after a later symlink

- The `cp02-v01` repair review found that the checkpoint 1 symlink rule
  discarded durable evidence: when a recorded historical plan path was
  already owned by the durable identity history and the plan later moved
  (`move_plan_identity` retains every owned path) and the old path was
  replaced by a symlink, `_recorded_suffix_crosses_symlink` made the record
  unrelated, so startup reported a complete scan and a clean start over
  retained prior work.
- `aflow/control_plane/startup_context.py` now distinguishes the two cases:
  a recorded suffix that crosses a symlink below the root never borrows its
  target's ownership, but the recorded spelling itself is checked with a new
  read-only lexical projection (`aflow/plan_backups.py::
  plan_identity_lexical_owners`, exact `owned_paths` membership with no
  filesystem resolution) against the verified root's canonical identity plus
  the verbatim suffix. A spelling the current identity owned stays related
  but identity-uncertain, so startup still cannot prove a clean scan; a
  fresh descendant symlink spelling with no owned history stays unrelated,
  preserving every descendant-symlink decision, and current-plan no-follow
  containment is untouched.
- A Linux fixture records a prior run before the old path becomes a symlink,
  moves the plan with `move_plan_identity`, installs the symlink, and
  confirms the run stays a retained blocker with dirty and with missing
  prior work, in both the canonical and the parent-alias spelling of that
  historical path; the test fails on the pre-repair tree and passes after.

## 2026-10-02 — Match recorded startup roots by canonical directory identity

- The fresh-worktree prior-work guard compared recorded run roots, worktree
  paths, and historical plan paths against verified project roots using
  lexicographic string comparisons, so equivalent filesystem spellings of the
  same checkout (for example the macOS `/var` and `/private/var` aliases)
  were dropped as unrelated. Manifest-backed runs left the scan complete and
  the guard admitted a clean start; metadata-only runs left an unverifiable
  scan; post-bootstrap starts created a feature worktree over an existing
  alias-spelled worktree.
- `aflow/control_plane/startup_context.py` now compares recorded roots and
  verified roots by canonical directory identity (`Path.resolve()` plus a
  directory check), matches recorded worktree paths the same way, and
  resolves historical absolute plan paths by finding the first
  directory-ancestor prefix, walking from the filesystem root toward the
  path, that canonicalizes to a verified root while retaining the entire
  remaining suffix verbatim; a later root-equivalent prefix (for example a
  descendant directory symlink pointing back at the root) never replaces that
  suffix, and a recorded suffix that crosses a symlink below the root never
  borrows its target's durable ownership: a spelling the durable identity
  history owned stays related but identity-uncertain, and a fresh descendant
  symlink spelling with no owned history stays unrelated. The plan file and
  its descendants are never resolved, a missing historical file still
  matches through identity history, descendant symlinks gain no exact-match
  authority, traversal components are rejected, and a recorded root that
  cannot be canonicalized keeps the scan incomplete (fail-closed) instead of
  proving a clean start. Identity authority, uncertain decisions, unrelated
  different-project roots, and the unchanged-refresh evidence are untouched.
- Linux-reproducible tests use a symlinked parent directory as the alias:
  manifest-backed and metadata-only alias-spelled runs stay related with
  mixed canonical/alias spellings, same-basename/sibling-prefix/traversal
  paths stay unrelated, descendant symlinks gain no authority, a directory
  symlink below the root pointing back at the root cannot become an exact
  current-plan match in either alias or canonical spelling, alias paths
  keep conflicting-identity uncertainty and missing-history matching, and a
  post-bootstrap lifecycle start with alias-spelled unresolved evidence fails
  with `prior_work_unverified` before any feature worktree, feature branch,
  or worker execution.
- The canonicalization rule is documented in `ARCHITECTURE.md` and
  `docs/runtime-behavior.md`.

## 2026-10-02 — Preserve bootstrap ordering and verify the complete startup journey

- The fresh-worktree prior-work guard now defers only the Git-dependent
  starting-branch verification for supported non-Git directories and unborn
  repositories. The retained-run evidence scan still runs before
  initialization and fails closed on unresolved or unreadable evidence; the
  full Git-dependent decision re-runs after the verified initial commit and
  before a feature worktree is created or a normal worker executes.
  Established repositories keep the strict guard, including a missing
  configured starting branch. The boundary is documented in
  `ARCHITECTURE.md` and `docs/runtime-behavior.md`.
- Focused bootstrap tests cover a plain non-Git directory and an unborn
  repository reaching preparation once, retained and unreadable evidence
  blocking before initialization, the post-bootstrap recheck failing before
  any feature worktree is created, and the established missing-ref rejection.
- Added a real-browser decline journey
  (`test_inconsistent_plan_recovery_decline_stops_without_starting`): the
  inconsistent-plan warning and recovery question are shown, declining stops
  with `needs_attention` and the `Startup recovery declined` record, no worker
  starts, and the plan source bytes stay unchanged. The existing journeys
  cover the confirmation without rewrite and the preserved-work block.
- Visual matrix screenshots (desktop/mobile, light/dark, Chromium and WebKit)
  were captured and the guideline geometry assertions passed: two-row header
  within 112px with content starting by 128px on desktop, mobile menu and
  list/detail/Back, no horizontal overflow, no unauthorized scrollers,
  primary controls at least 43px tall, document scrolling, focus and scroll
  retention across passive refresh, and text enlargement. Physical-device
  keyboard/browser-toolbar acceptance remains owner pending.
- Verification: from the repository root, `uv run pytest -q
  tests/test_startup_context.py tests/test_plan_backups.py
  tests/test_plan_lifecycle.py tests/test_dirty_worktree_preflight.py
  tests/test_library_api.py tests/test_cli.py tests/test_aflowd.py
  tests/test_project_admission.py tests/test_control_plane_resume.py
  tests/test_resume_pending_review.py tests/test_live_config_runtime.py`
  (435 passed, 147 subtests); `npm --prefix apps/aflow_app/web test -- --run`
  (691 passed) and `npm --prefix apps/aflow_app/web run build` (clean); from
  `apps/aflow_app/server`, `uv run pytest -q tests/test_control_plane_api.py
  tests/test_mcp.py tests/test_startup_context_browser.py
  tests/test_plan_startup_browser.py tests/test_responsive_browser.py`
  (196 passed, Chromium) and the same with `AFLOW_TEST_BROWSER=webkit` on the
  three browser modules (53 passed). `git diff --check` is clean. Changes are
  left uncommitted for review.

## 2026-10-01 — Preserve ambiguous concierge create intent (Checkpoint 4 review fix)

- A local `create_plan` timeout did not cancel the server request, but the
  concierge treated a missing target path as proof that creation failed and
  left no durable intent. A late server-side create could therefore appear as
  a covering `todo` draft that no later tick could safely promote.
- `aflow/concierge.py` now persists a private `create_pending` record before
  consuming the one-attempt mutation budget and issuing `create_plan`. The
  record binds the exact claim, canonical issue identity/source hash,
  expected plan name/path, and generated document hash without asserting
  creation or ownership. A missing, unreadable, or conflicting immediate
  read-back reports `creation_unverified` and preserves the pending intent
  instead of claiming the create failed.
- Later ticks reconcile `create_pending` records read-only from complete MCP
  evidence. A record becomes `created` only after exact canonical path,
  `todo` status, document content hash, current revision, owner-issue
  identity/source, and duplicate-evidence checks match. Triage marks issues
  covered by private concierge provenance records, including pending records,
  so the same ambiguous claim is never created again while independent safe
  work remains eligible. `create_pending` drafts are not promotable.
- Added focused executor tests covering deferred server-side creation
  recovered and promoted on a later tick with only one `create_plan`,
  conflicting document evidence staying held, and independent safe work
  proceeding while a pending claim remains unresolved.
- Updated `ARCHITECTURE.md`, `aflow/concierge_prompt.md`, and
  `deploy/concierge/README.md` where the durable pending-create behavior
  differs from the previous failure interpretation.
- Verification: from the repository root, `uv run pytest
  tests/test_concierge.py tests/test_issue_intake_planner.py
  tests/test_publication.py tests/test_control_plane_capabilities.py -q`
  (331 passed) and `uv run ruff check aflow apps/aflow_app/server/src`
  (clean); `git diff --check` is clean. From `apps/aflow_app/server`,
  `uv run pytest tests/test_mcp.py tests/test_plan_store.py
  tests/test_control_plane_api.py -q` (189 passed). No UI change is included.
  Changes are left uncommitted for review.

## 2026-09-30 — Complete bounded concierge inventory and conservative occupancy (Checkpoint 2)

- The recovered concierge blocked on any unknown run activity and read an
  incomplete inventory: `list_plans` omits `failed` and `needs-plan-change`
  documents, and list rows deliberately carry no admission preview.
  `aflow/concierge.py` now builds a complete bounded inventory before
  triage: it pages `list_runs` with the opaque `next_cursor`, pages
  `list_plans` by exact last path until the final empty page (including an
  exact page-size multiple), joins lifecycle rows with `list_plan_documents`
  by exact canonical path (normalizing underscore/hyphen status spellings
  once, preserving legacy `draft` as held), and reads every document with
  `read_plan`, retaining exact path, normalized status, revision, content,
  and modification timestamp. Missing or malformed pages/rows, duplicate
  identities, repeated or non-advancing cursors, and an unconsumed
  continuation at the page cap produce a bounded `evidence_unavailable`
  report with zero mutations; a partial inventory is never treated as
  complete.
- One conservative occupancy classifier
  (`classify_run_occupancy`/`_occupancy_report`) now serves initial triage,
  fresh selection, post-planner checks, and timeout reconciliation. Active
  activity, `unit_active=true`, or `preparation_active=true` blocks all
   dispatch even over a terminal label. Canonical terminal statuses
   (`completed`, `failed`, `interrupted`, `owner_stopped`) with a canonical
   `unknown` or `inactive` activity and no contradictory active evidence are
   historical and never grant resume; a terminal row whose activity is
   missing or malformed is not provably historical and stays blocking until
   fresh canonical `get_run` detail resolves it. Nonterminal unknown rows are
   refreshed through matching `get_run` detail,
  and a still-uncertain unit blocks regardless of age. A validated
  pre-execution startup question or preparation-stage startup failure
   (manifest-only phase, `no_agent_started=true`,
   `unit_observation=missing`, no active preparation/unit) holds only its own
   plan while fresh canonical queue capacity permits another implementation,
   and only when the fresh `get_run` detail carries a nonempty plan path
   that exactly matches the list row's plan path; a missing or mismatched
   identity cannot establish which plan the startup record holds and keeps
   the run blocking. A validated startup hold is never answered, discarded,
   or retried. Paused, waiting, malformed,
  or missing-evidence rows remain blocking. Every mutation path re-checks
  fresh canonical queue capacity (one slot, automatic consumption disabled)
  before advancing work.
- GitHub issue evidence completeness is a global fail-closed gate: a
  missing repository identity, a feed read error, a repeated page, or an
  unconsumed continuation at the preserved five-page cap reports
  `github_evidence_unavailable` with zero mutations before defect filing,
  resume, start, or plan authoring, and never permits selection from
  partial issue evidence.
- `FakeMcp` now models lexical run/path ordering, opaque returned run
  continuations, list rows without `can_resume`, full lifecycle document
  coverage, revisioned plan reads, matching detail reads, and real queue
  outcomes. New regressions cover the observed 258-run/235-document
  inventory (complete, initially defers with zero writes), exact page-size
  multiples, malformed rows, duplicate identities, repeated cursors,
   non-advancing plan pages, page-limit exhaustion, terminal history with a
   verified startup hold permitting one independent action, unverified
   startup candidates, startup holds whose detail plan path is missing or
   mismatches the list row, active-evidence precedence over terminal
   labels, and incomplete GitHub inventories (partial page and feed read
   error) that report with zero writes for otherwise admissible `start`
   and `resume` candidates.
- Verification: from the repository root, `uv run pytest
  tests/test_concierge.py -q` (192 passed) and `uv run ruff check
  aflow/concierge.py` (clean); from `apps/aflow_app/server`, `uv run pytest
  tests/test_mcp.py tests/test_plan_store.py -q` (95 passed); `git diff
  --check` is clean. No UI change is included.

## 2026-09-30 — Accept bounded full-path plan cursors in mounted MCP (Checkpoint 1)

- The mounted MCP `list_plans` tool shared the run cursor's 64-character
  `_bounded_cursor` limit with `list_runs`, so the live pagination sequence
  from issue #56 broke: the 64-character first-page continuation
  (`plans/done/macos-plan-lifecycle-path-alias-ci-repair-20260925.md`,
  exactly 64 characters) succeeded, while the 71-character second-page
  continuation returned `operation_rejected`. `aflow/mcp_control_plane.py`
  now has a plan-specific `_bounded_plan_cursor` used only by `list_plans`:
  it accepts full plan paths of at most 4,096 characters, preserves `None`
  and empty-string start cursors, rejects C0 control characters and DEL with
  the secret-safe fixed `operation_rejected` code (never echoing the
  supplied value), and passes the cursor through unchanged as a
  lexicographic continuation key. `list_runs`, `_bounded_cursor`, the
  64-character run cursor limit, response shapes, authentication, and
  credential rejection are unchanged.
- Repository coverage walks 235 regular plan files with the exact
  64-character issue path at the page-1 boundary and a 71-character path at
  the page-2 boundary; following each returned final path yields every
  record exactly once in repository order and terminates on the empty page.
  Authenticated mounted-MCP coverage (`_mcp_tool`/`_mcp_tool_error`)
  reproduces the corrected live sequence across real 100-item boundaries —
  the 64-character first-page continuation succeeds and the returned
  71-character second-page continuation now succeeds — plus a 4,096-character
  cursor accepted with an empty page, rejection at 4,097 characters,
  rejection of C0/DEL control characters without echo, non-string cursor
  schema rejection, and a run-pagination non-regression (valid run cursors
  follow `next_cursor`; a 65-character run cursor is still rejected). The
  new MCP test fails with the old shared 64-character limit and passes with
  the fix.
- Verification: from the repository root, `uv run pytest
  tests/test_control_plane_repository.py -q` (38 passed) and `uv run ruff
  check aflow/mcp_control_plane.py` (clean); from `apps/aflow_app/server`,
  `uv run pytest tests/test_mcp.py -q` (44 passed); `git diff --check` is
  clean. No UI change is included.

## 2026-09-30 — Canonicalize trusted package root in assertion confirmation

- The macOS CI failure was a path-alias mismatch: temporary paths under
  `/var` canonicalize to `/private/var`, and `engine_assertion_confirmation`
  resolved the candidate source file but compared it against the
  uncanonicalized `_PACKAGE_SOURCE_ROOT`, so an aliased package root failed
  containment and silently suppressed the bounded confirmation. The root is
  now resolved symmetrically with the candidate whether supplied as
  `package_root` or taken from `_PACKAGE_SOURCE_ROOT`; classification
  semantics, signature inputs, the symlink/file checks, and the six-field
  output contract are unchanged.
- A new regression loads the fixture engine through a directory-symlink alias
  of the package root and asserts the bounded confirmation is recorded; it
  fails before the fix (no confirmation) and passes after on any platform
  with directory symlinks. The positive internal and negative external
  assertion tests are retained unchanged in behavior.
- Verification: focused assertion tests pass (3 passed); full suite 2556
  passed; `ruff check aflow apps/aflow_app/server/src` clean and no new
  findings in the two modified files. No UI change is included.

## 2026-09-30 — Project trusted defect confirmation through REST and MCP (Checkpoint 2)

- Canonical `RunStatus` gains an optional typed `defect_confirmation`
  (`DefectConfirmation`, the fixed six-field contract) and the repository
  projects it only from validated control-plane-owned `run.json` metadata via
  the checkpoint-1 strict validator. Malformed, legacy, and unconfirmed runs
  project `null` without blocking status reads; status, activity, worker
  receipts, resume evidence, and history behavior are unchanged.
- `RunStatusResponse` mirrors the field with strict literal and pattern
  validation, so REST `get_run`, the runs list, and MCP `get_run`/`list_runs`
  expose the identical optional value through the existing tools with no new
  registry, mutation surface, or raw diagnostics.
- Focused tests cover the repository projection (valid, unconfirmed, sixteen
  malformed shapes, legacy runs), the transport round trip and out-of-contract
  rejection, and REST/MCP parity for both a valid record and a broken one.
- Verification: 173 passed across `tests/test_control_plane_repository.py`,
  `apps/aflow_app/server/tests/test_mcp.py`, and
  `apps/aflow_app/server/tests/test_control_plane_api.py`; `git diff --check`
  is clean. No UI visual change is included.
## 2026-09-30 — Concierge trusted defect reporting and exact-SHA delivery gate

- Imported the scoped concierge draft from the preserved source worktree
  (approved `1cff55ff`/`42c60758` plus its seven-file checkpoint-3 draft) and
  completed the two review-blocking behaviors on current main.
- The defect classifier now files only from the live MCP
  `get_run.defect_confirmation` projection, strictly validated against the
  fixed six-field contract (schema version 1, `engine_internal_assertion`,
  `controller`, bounded package-relative component/site, 64-hex signature).
  Missing, malformed, or unconfirmed data files nothing; the trusted
  signature deduplicates across different run IDs, so one confirmed defect
  files exactly one sanitized issue. A failed dedup search stays report-only,
  and an already-filed defect never displaces the eligible safe action on a
  later tick.
- The exact-SHA delivery gate resolves the remote `main` tip with a bounded
  read-only `git ls-remote` (never a stale local tracking ref, no fetch, no
  shared-ref mutation) and projects CI (`green`/`red`/`pending`) from only
  the deploy poller's qualifying CI workflow runs for that SHA (exact
  `head_sha`, `event=push`, `head_branch=main`,
  `path=.github/workflows/ci.yml`): the latest applicable attempt by
  `(run_number, run_attempt)` decides, matching the deployed poller — latest
  success is green, latest failure/cancelled is red, latest queued/running
  is pending, and earlier attempts never override it. The deploy `phase`
  comes from `/var/lib/aflowd/deploy/status.json` and the live release
  (`installed`/`stale`/`missing`) from the `/opt/aflowd/current` release
  name; missing evidence projects to `pending`, never success. A failed
  exact-SHA delivery blocks fresh `start`/`plan_and_start` dispatch with
  `delivery_gate_failed` until repaired and never blocks a verified
  `resume`. CI and the live release are reported separately, so green CI
  with a stale live release is `pending`, not `ok`.
- The concierge GitHub client gained bounded run-number/attempt-aware
  `workflow_runs` plus `search_issues` and `create_issue`; the injectable
  `DeliveryGate` (production `LocalDeliveryGate`) keeps the gate testable
  without the live host. The advisory prompt, the concierge deployment
  runbook, and the architecture notes describe the implemented behavior.
  Live p100 host binding and timer activation remain with the queued
  binding plan.
- Verification: focused concierge, planner, and MCP suites pass; `ruff check
  aflow apps/aflow_app/server/src` is clean; `systemd-analyze verify` passes
  for the concierge service and timer. Changes are left uncommitted for
  review.
## 2026-09-27 — Startup context browser and recovery evidence (Checkpoint 6)

- Added disposable real-browser journeys for a partial-plan start at the live
  default with no explicit step, a legacy no-agent question, preserved earlier
  dirty/unmerged work and its recorded macOS blocker, exact-run cancellation,
  and failure/refresh while that cancellation is pending. Reads and navigation
  preserve the synthetic prior worktrees; no extra start or resume occurs.
  Changed, invalid and missing plans, two possible predecessors, and an older
  server without startup context retain safe explanations.
- Chromium and WebKit each passed the startup journey at 320×568, 390×844,
  768×1024, 844×390, 1280×720, 1440×900 and 390×420 in light/dark, plus
  enlarged text. Inspected populated captures; the checkpoint title wraps,
  the action remains readable, and measured primary target height is 44px at
  every viewport. Document scroll, focus, disclosure/Expand all, compact Back,
  and unchanged-refresh retention passed. The 30 screenshots and two manifests
  are in `/root/code/evidence/aflow-startup-context-cp6-20260927/` via the
  browser artifact format used by CI.
- Verification: 368 Python tests and 147 subtests passed; 689 web tests passed
  on a standalone rerun after one 5-second history-refresh timeout during a
  parallel run; the web build passed. The final integrated server command
  passed 156 tests (an earlier rerun was interrupted by SIGTERM without a
  pytest failure). The full WebKit startup/responsive command passed 48 tests after
  updating disposable fixtures: the family launch plan now has a real
  checkpoint, and fake workers clear the inherited AFlow reservation nonce.
  `git diff --check` passed.
- Physical phone keyboard/browser-toolbar checks, owner comprehension, live
  pending-run read-only inspection, exact-SHA CI/deployment and activation
  remain post-publication checks. The frozen demo and UI guidelines stay as
  their existing design baseline; other reference docs need no change because
  this checkpoint changes browser evidence and clarifies delivered startup
  behavior without changing setup, API routes, or layout rules.

## 2026-09-27 — Verify bounded saturated scans (Checkpoint 1)

- The reported saturated project had 45 in-progress plans and 180 uncertain
  historical runs against two slots. The current base already contains the
  same-pass full-capacity cache and verified linked-worktree skip from
  `47a85e08`; this pass retains those admission and identity boundaries.
- Strengthened the focused consumer tests to verify one primary scan per pass,
  no linked-worktree scanner or expected error, visible unsafe registry errors,
  and the locked admission rejection when another actor fills capacity after
  the scan's observation. The existing test covers one snapshot for ten stable
  plans at full capacity and a later pass admitting after release.
- Verification: 90 passed across consumer, admission, and settings on the
  default interpreter; 65 passed across consumer and admission on Python 3.12;
  Ruff passed for the consumer and its tests. The tracked diff contains only
  focused tests and these two notes. The ignored local plan records checkpoint
  progress for review.

## 2026-09-27 — Trace CP8 review readiness after held preflight

- Exact-SHA CI run `36195706894` opened Review with the selected plan,
  workflow, team and default turn limit, but working-tree inspection was still
  pending at the five-second assertion. Eight isolated Chromium repeats on
  Python 3.12 did not reproduce a stuck review. A controlled post-review
  inspection exposed a separate safety gap: the panel showed loading while
  the shell's Start run button was briefly enabled by its passive slot update.
- The dashboard now synchronizes that header button's disabled state before
  paint and sends its click through the latest launch guard. The exact held
  request keeps the review blocked until it finishes; the server remains the
  final admission authority.
- The CP8 browser fixture now captures the post-click readiness failure,
  including sanitized request identities, response/terminal events, review
  focus, current status, final-action state and zero-start evidence. If a
  current request is pending, it waits for that exact request, follows a
  recorded successor after cancellation, and still requires ready Review and
  enabled Start run. The 390×844 held-refresh case joins desktop coverage.
  A component regression checks a new inspection while Review is open, and
  the held browser journey directly checks the post-review pending/ready
  transition.
- The final controlled journey passed three isolated desktop Chromium repeats
  and desktop/mobile WebKit, with mobile Chromium included in the full suite.
  All 680 web tests, the production build and all 638 Python 3.12 server tests
  passed on the final behavior. Exact-SHA CI remains a reviewer/delivery gate.

## 2026-09-27 — Cumulative verification handoff (Checkpoint 3)

- The original plan records two September 24 server-suite examples at 594.17s
  and 771.19s, both with failures. Its September 27 web examples are 2–3s
  for a focused two-test repair run and 34.37s for a 671-test suite. These
  differ in scope and are reference timings, not a measured speedup from this
  policy change.
- After publication, green CI, and safe activation of the installed
  `checkpoint_review_then_final` default, audit the first three terminal runs
  launched without an explicit workflow that resolve to that default. Include
  failed runs. Record run IDs and source/config revisions, then use existing
  turn logs to total synchronous checkpoint test seconds by worker and reviewer,
  count full-suite calls before final review and unexplained repeats, and time
  final review with its regression result. Report exceptions and missing
  evidence; target zero routine pre-final full-suite calls and zero unexplained
  repeats. The cumulative `uv run pytest -q` gate remains with final review.

## 2026-09-27 — Behavior-based packaged workflow IDs (Checkpoint 2)

- Packaged definitions now name their review stages. `ralph`,
  `review_implement_review`, `review_implement_cp_review`, `hard`, and `medium`
  remain aliases with equivalent resolved behavior; the packaged default uses
  `plan_review_then_final_squash`. The live-only mapping and staged activation
  are documented in README; no installed configuration or active run changed.

## 2026-09-27 — Phase-specific verification guidance (Checkpoint 1)

- Bundled planning guidance now requires focused checkpoint commands and a
  separate cumulative `Final Verification` set. Workers follow existing plan
  commands, checkpoint reviewers inspect coverage and reuse valid primary
  evidence, and final reviewers own the complete regression gate. Packaged
  prompts echo this division; workflows and live installed skills remain
  unchanged in this checkpoint.

## 2026-09-27 — Lean All-runs progress detail (Checkpoint 4)

- Run-detail REST accepts `include_resume_preview`, defaulting to true. Only
  All-runs display enrichment requests false; selected-run and MCP detail
  reads keep the full default. The lean response omits `can_resume` while
  retaining fresh status, activity, worker and progress fields. The overview's
  four-request pump, generation checks and cancellation remain unchanged.
- On an identical three-run browser fixture with a controlled 0.7s delay in
  each resume preview, first usable rows took 0.17–0.24s and complete raw
  coverage took 0.19–0.27s across Chromium/WebKit desktop and mobile. Visible
  progress finished in 0.98–1.04s with full previews and 0.19–0.27s with
  lean detail, a 74–81% reduction; preview calls fell from 3–6 to zero.
  The delay isolates a scan-dominated case. Equal, changed and failed refresh
  journeys passed in both engines, and populated screenshots were reviewed.
- The earlier live baseline showed first rows at 2.72s with many progress
  rows still loading at 47.73s during administrative recovery. It is not
  directly comparable to this controlled fixture. Exact-SHA deployment,
  same-corpus live timing and complete-page acceptance remain coordinator
  delivery checks. If live completion is still slow, measure the remaining
  detail-read and coverage costs before specifying any persisted projection.

## 2026-09-27 — Independent plan-list readiness (Checkpoint 3)

- PlanPanel now publishes the fresh list when that read settles, independently
  of queue evidence. Initial queue state has no capacity or run claims. A failed
  queue read clears its projection while leaving the list and edited document
  usable. Project and refresh generations reject late responses.
- The pre-change live HTTP baseline was 5.34s for queue and 1.58s/1.88s for
  recent ten-run reads without/with rich progress. Those were captured before
  this UI change and under different live conditions. The controlled WebKit
  browser runs with queue held for at least 5s showed fresh plan-list response
  to visible row in 34–101ms in WebKit and 35–46ms in Chromium across
  populated desktop/mobile light/dark and 390×420 cases. Controlled complete
  page readiness, including the changed queue reason, was 5.20–5.24s in
  WebKit and 5.15–5.17s in Chromium. Both engines verified
  opening and editing during the hold, then applying changed queue evidence
  without replacing the editor node, focus, selection, scroll or draft.
- These are local controlled timings, not deployed page-completion results.
  The coordinator must measure live queue, recent-run list, first usable content
  and complete page readiness on the same corpus after exact-SHA activation.
  No deployed comparison or physical mobile keyboard check is claimed here.

## 2026-09-27 — Reuse fresh admission evidence per operation (Checkpoint 2)

- Admission now resolves verified roots, run identities, and canonical status
  once within each lock operation. Queue receives capacity, claims, and roots
  together; plan owner records are loaded once for the displayed documents.
  A full consumer pass reads capacity once, then checks it again on the next
  pass or after a successful launch. Verified linked registrations defer to
  the primary scanner without logging an expected identity error.
- Read-only in-process queue replay on the same current 223-plan primary
  corpus (194 occupied runs, 63 verified roots) measured separate admission
  calls at 2.373s/2.491s and combined calls at 1.256s/1.082s, with plan
  owner batching active in both paths. The two-sample means differ by 52%.
  Before owner batching, plan identity reads consumed 1.280s of a 2.262s
  combined queue projection; after batching, the remaining local projection
  was about 1.1–1.3s. This replay suppressed admission writes and lifecycle
  recovery. The earlier 5.34s HTTP queue baseline involved different live
  conditions, so exact-SHA deployment, HTTP timing, and complete page
  readiness still need coordinator measurement.

## 2026-09-27 — Selected-run refresh delivery gate repair (Checkpoint 1)

- The macOS dashboard failure had no changed-phase event request. The browser
  probe released equal-phase responses, waited 250 ms, then dispatched the next
  event without observing completion. The loader also discarded background
  events received during an active page refresh. It now remembers one pending
  background pass, consumes it after the current pass, and lets an already
  queued explicit refresh cover it. Epoch, selection, and visibility guards
  still gate the read.
- The selected-run browser probe now waits for the visible document and initial
  list/status/events/context completions, then tracks completion of each held
  equal-phase request before dispatching a changed event. It retains changed
  event, read failure, DOM identity, focus, scroll, disclosure, and no-write
  assertions. A frontend race test verifies one follow-up read after an event
  burst during a held selected-run read.
- Focused dashboard tests passed 162/162; the web build passed. The full web
  suite passed 671/671 on an isolated rerun after one 5-second history-test
  timeout during concurrent browser runs. The selected-run browser journey
  passed 4/4 in Chromium and 4/4 in WebKit on Linux. The named macOS failure
  did not reproduce in the local baseline run after the bundle was built;
  exact-SHA macOS CI, publication, and deployment remain coordinator checks.

## 2026-09-27 — Retain terminal proof through run pruning (Checkpoint 1)

- `keep_runs` now preserves managed directories whose outcome is active or
  uncertain. Before pruning a proven terminal run it stores a small, versioned,
  manifest-bound summary outside the run directory and verifies the write.
  Fresh repository and admission reads can then retain the raw terminal outcome
  and release capacity. Missing or invalid summaries remain uncertain; no old
  launch marker is promoted to success. Recovery and delivery predecessors
  still keep their full artifacts.

## 2026-09-25 — Keep changed All runs progress moving (Checkpoint 1)

- The integrated invisible-refresh change kept the overview generation stable,
  but its effect snapshot keyed tokens only by project/run ID. A changed raw
  row could therefore depend on an incidental admission transition to restart
  detail enrichment. Tokens now follow reconciled run objects: equal rows retain
  their token; changed rows enqueue one matching detail read. Existing generation,
  result identity, four-request cap, and stale-response checks remain in force.
- A changed row keeps its last matching projection visibly marked stale until
  detail settles. Focused tests cover equal siblings, one replacement read,
  delayed old detail, canonical mismatch, and failure. The populated All runs
  browser probe uses a changed raw turn without changing the row's group;
  Chromium/WebKit desktop 1280×720 and mobile 390×844 kept equal-row nodes,
  preview, focus, and scroll, made no writes or extra equal-row reads, and
  settled the changed row to 2/10 approved after one detail read. Before/equal/
  changed screenshots and DOM identity evidence were inspected. Physical mobile
  keyboard and browser chrome, CI, publication, and live activation are outside
  this checkpoint's local verification.

## 2026-09-25 — Automatic durable cross-harness repair handover

- Run `20260925t152928z-45800a35` failed at the first Sol High → DS4.1 repair
  boundary because Codex has no provider-enforced read-only teardown. Its
  predecessor worker session and recorded reviewer rejection remain untouched.
- At a completed worker boundary, an exact active source session and successful
  target preflight now allow a controller-authored brief from bounded Full
  manager and durable controller facts. No source-session method runs. The
  existing provider-enforced read-only path remains available. Both paths keep
  three hash-bound artifacts and fail closed on missing evidence or workspace
  drift; controller fingerprints include already-dirty file contents.
- Verified `uv run pytest -q tests/test_hotplug.py tests/test_repair_upgrades.py
  tests/test_live_config_runtime.py tests/test_harness_sessions.py` (269 passed),
  `uv run pytest -q tests/test_aflowd.py tests/test_config.py` (200 passed,
  7 subtests), scoped Ruff, and `git diff --check`. Publication, CI, and live
  activation remain separate coordinator gates.

## 2026-09-25 — Count reviewer overlays after a checked original checkpoint

- A checkpoint reviewer who creates a focused repair overlay after the worker
  checked the original checkpoint now records the same scoped rejection as an
  unchecked-original review. The record uses the awaiting scope and latest
  worker attempt, and can trigger the configured first-repair upgrade. Clean
  approvals, final architect follow-ups, changed original snapshots, and
  unrelated overlays remain excluded. Existing cross-harness handover guards
  still apply after the repair team is selected.

## 2026-09-25 — Full-app invisible refresh verification (Checkpoint 2)

- Added real-browser held-read journeys for Projects, Plans, and New run using
  actual Element identity and scoped mutations. Projects keeps the selected
  row and focused Add project draft through equal, changed, and failed registry
  reads, then survives a viewport resize. Plans keeps a newer dirty editor,
  cursor, selection, scroll, and node through real post-save list/queue reads;
  changed queue cues and failed-read warnings stay visible. Explicit conflict
  reload discards the draft only after confirmation, and compact Back/reopen
  plus resize retains the selected document. New run keeps exact Plan,
  Workflow, Team, turns, stage, start step, instructions, and review across
  equal/changed/failed background preflight, changed/failed capability reads,
  and an explicit inspection. Changed blockers disable Start; a final double click
  sends exactly one reviewed request to a disposable intercepted endpoint.
- Reproduced one New run failure defect: the last successful worktree result
  remained in state but was hidden after a failed recheck, collapsing the
  populated section and document position. `NewRunPage` now retains those
  checkout facts as labelled stale evidence, keeps the acknowledgement
  visible but disabled, and labels prior blockers as prior evidence. The
  current failure remains explicit and Start stays blocked until a successful
  inspection. Full-page Playwright screenshots temporarily mutate inline input
  styles and can move a sticky action row; capture-only mutations are drained,
  and New run capture-induced scroll is restored before the next network read.
  Demo HTML stays at SHA-256 `2465c0ac2bfef90af2b98a537ad5ac3e931b927c23a78379a8c532ce4dcbadf4`,
  identical to `b667e47b`. Populated reference and production Projects, Plans,
  and New run captures were inspected in wide/phone light/dark states. The
  production Project row emphasizes registry readiness/path where the demo
  uses run counts; New run has real preflight/review evidence beyond the
  conceptual demo. These existing presentation differences are for the final
  combined visual gate, not refresh regressions. Physical mobile keyboard and
  deployed output were not verified by emulation.
- Verification: web Vitest 667/667; production build; Chromium relevant
  browser selection 60/60 and WebKit selection 60/60; exact fidelity module
  30/30 in each engine after the screenshot isolation fix; final scoped
  Projects 3/3, Plans 2/2, and New run 3/3 refinements passed in each engine.
  After the production repair, Chromium/WebKit each passed the affected
  launch/navigation browser selection 7/7, and the fidelity module 30/30.
  `git diff --check` passed.
  The 60-test browser selection used the full
  `test_ui_demo_fidelity_browser.py`, `test_plan_backup_browser.py`,
  `test_plan_pagination_browser.py`, `test_plan_startup_browser.py`,
  `test_launch_confirmation_browser.py`, `test_run_navigation_browser.py`,
  and `test_responsive_browser.py` modules, with this exact pytest filter:
  `not responsive_route_matrix and not responsive_focus_resize_and_screenshots and not responsive_team_family_journey and not responsive_live_controls_and_restart and not stop_after_current_turn_delayed_worker_journey and not durable_recovery_ui_journey and not global_run_overview and not runs_header_filter_legibility and not responsive_action_hit_test and not team_family_stage_click and not responsive_focus_resize_and_screenshots`.
  Re-run with `AFLOW_TEST_BROWSER=webkit` for WebKit. The plan/startup and
  navigation modules contain a few explicitly Chromium-only cases; the new
  checkpoint probes use `_browser` in both engines.
- Persistent artifacts: `/tmp/aflow-cp2-artifacts/chromium/` and
  `/tmp/aflow-cp2-artifacts/webkit/` contain the `cp2-projects-*`,
  `cp2-plans-*`, and `cp2-new-run-*` PNG/JSON evidence; the frozen demo route
  captures are under `/tmp/aflow-cp2-artifacts/reference/`.

## 2026-09-25 — Guided first-repair threshold (Checkpoint 2)

- Guided actions and REST/MCP config writes now accept strict integer zero.
  Both canonical and invalid-draft projections preserve explicit zero through
  global, workflow, and base-workflow inheritance; `None` still clears a
  workflow override. Threshold edits leave team role inheritance untouched.
- Workflows Settings allows zero, explains its first reviewer-requested repair
  effect, and keeps an unsaved zero across selection and preview refresh.
  Save and reload continue to distinguish zero from blank inheritance.
- Verification: 195 guided/API/MCP tests, 664 web tests, 195 focused core tests
  plus 7 subtests, web build, scoped Ruff, and `git diff --check` passed.
  Chromium and WebKit passed the 320×568 save/reload/inherit journey; light
  and dark screenshots showed readable threshold details without overflow.

## 2026-09-25 — First repair worker upgrade threshold (Checkpoint 1)

- Core workflow config and repair policy accept explicit nonnegative thresholds.
  The default stays at one; zero takes one `upgrade_to` edge for the first
  reviewer-requested repair after an authoritative initial rejection. Positive
  thresholds still count failed repairs on the current team. Missing or
  ambiguous rejection evidence, operational retries, and reviewer invocations
  do not trigger an upgrade.
- The route remains worker-only and preserves inherited checkpoint and final
  reviewer roles. Live config changes apply at newly resolved boundaries and
  do not rewrite prior attempts. Guided Settings and p100 workflow opt-in are
  separate work after this checkpoint.

## 2026-09-25 — Stabilize grouped Run history Back focus check

- Exact-SHA `10e5bdfb` CI run `36141982543` failed the same immediate
  `activeElement` read in macOS Dashboard Python 3.12 and 3.13 after the
  document scroll had already matched. `SidebarEditorLayout` focuses the
  captured row in the next animation frame. The unchanged populated case
  confirmed `history-119` focus and scroll in local Chromium and WebKit.
- The browser case now waits up to five seconds for the visible grouped row
  itself to receive focus, then checks restored document scroll. No UI focus
  behavior, fixture, pagination, or grouped disclosure changed. macOS CI and
  live activation remain delivery gates after review and publication.
- Verification: the focused case passed three Chromium and two WebKit runs;
  the complete navigation module passed 4/4; the web suite passed 659/659
  when run alone; the production build and `git diff --check` passed. A web
  refresh test timed out once while browser suites ran concurrently, then
  passed alone and in the isolated full-suite rerun. Inspected 390×844 light
  and dark captures for recent rows, `100 loaded`, and one older-run group.

## 2026-09-25 — Recent-first project Runs history (Checkpoint 1)

- Added opt-in `order=recent` to repository, service, and REST history pages. The
  default remains oldest-first for existing clients and All runs. Recent pages
  filter before reversal and limit, continue after the exact last identity, and
  reject a cursor outside the selected history. Runs requests recent order on
  initial load, refresh, and Load more; it preserves server page order.
- Repository coverage exercises 125 mixed visible/archived/deleted identities,
  a legacy timestamp ID, a new insertion ahead of the cursor, and complete
  duplicate-free traversal. Browser coverage uses 120 older runs with no
  recorded outcome plus recent active, failed, and completed runs. Chromium
  and WebKit show the newest three in the first rows after one Runs history
  request; All runs continues to request the default order.
- Verification: repository/history pytest 25 passed; API and Chromium run
  navigation pytest 67 passed; WebKit run navigation pytest 4 passed; web
  Vitest 649 passed; web production build and `git diff --check` passed.
  The exact-SHA CI gate for base `be28adc` was still in progress at handoff.
  This checkpoint is uncommitted for review; publication and live activation
  remain coordinator gates.

## 2026-09-25 — Repair failed-plan moves through parent path aliases

- Explicit requeue now compares the source run's recorded original path with
  the lifecycle journal's original through verified parent directories. An
  aliased parent preserves source-run lineage and the stable replay key;
  outside-root, mismatched-name, and symlinked final entries remain rejected.
  Verification: plan-store 51 passed; focused runtime/lifecycle 18 passed;
  symlinked temporary-root runtime 2 passed; Python 3.12 runtime/lifecycle
  325 passed and 45 subtests passed. Ruff passes on edited Python files.
  The unchanged `tests/test_runtime.py` still has seven baseline Ruff findings
  at `d443267e98c9ff8b07f3bc74b04db39c7084c298`.
- Terminal failure classification now compares verified canonical parent
  directories for the source plan, run, and recorded original plan. Lifecycle
  moves, prepared recovery, and record lookup use the same parent identity while
  retaining final-entry symlink rejection, revision checks, and admission gates.
- Reproduced both macOS runtime failures with a symlinked Linux temporary root;
  the two runtime cases pass after the repair. Added prepared-recovery and
  symlinked-final-entry regression coverage. Focused tests: 18 passed; the
  two alias-root runtime tests: 2 passed; Python 3.12 runtime/lifecycle suite:
  325 passed and 45 subtests passed. Ruff passes for the changed code and
  lifecycle tests; its full planned command still reports seven unchanged
  findings in `tests/test_runtime.py` at the base commit.

## 2026-09-25 — Keep publication locking outside execution checkouts

- Moved the repository-wide publication lock to the verified Git common
  directory. Acquiring it now leaves clean checkouts clean even when `.aflow/`
  is not ignored, while linked worktrees still share the exclusive lock.

## 2026-09-25 — Wait for compact Run history focus at the browser transition

- Release `ab4e1cb7` failed macOS Python 3.12 CI run `36115732349` at the
  immediate focus assertion after a deep Run history row became visible.
  Browser instrumentation at 390×844 recorded row-button focus followed by
  detail `H3` focus without another action: about 4 ms later in Chromium and
  46 ms later in WebKit. The layout focus effect remained unchanged.
- The focused navigation test now waits up to five seconds for focus inside
  the visible Run history detail, then asserts that state. It covers the
  repeated All runs entry, the 130-row selection, and light/dark compact
  cases. Existing exact URL, Back focus/document scroll, wide layout, and
  refresh assertions remain. Page-error checks on row selection passed in
  both engines; a broader temporary listener observed WebKit access-control
  errors from requests interrupted by unrelated navigations/reloads.
- `npm --prefix apps/aflow_app/web test -- --run`: 649 passed;
  `npm --prefix apps/aflow_app/web run build`: passed. The focused command
  `uv run --directory apps/aflow_app/server pytest -q
  tests/test_run_navigation_browser.py -k
  test_run_navigation_scroll_selection_and_history` passed in Chromium and
  with `AFLOW_TEST_BROWSER=webkit`. Replacing `uv run` with
  `uv run --python 3.12` in both commands also passed on local Linux Python
  3.12. `git diff --check` passed. macOS CI and exact-SHA deployment remain
  coordinator gates.

## 2026-09-25 — Verify Runs filter keyboard focus across native pickers (follow-up v01)

- Published `aeafd165` failed only the Dashboard macOS Python 3.12/3.13 jobs in
  run `36087876569`: after `ArrowDown`, Chromium still exposed the native
  select's `visible` value. The subsequent ArrowDown/Enter test also assumed
  ArrowDown advanced the macOS native menu, which is not guaranteed. The
  corrected journey uses real Tab and Shift+Tab keys to verify focus moves from
  the history select to New run and back without changing its value. Playwright
  `select_option` verifies `Archived` and `All history`, their selected option
  text, and the corresponding 1/40 recorded counts; it does not prove direct
  keyboard selection. Existing row identity, geometry, screenshots, resize,
  touch, focus, and More-menu checks remain.
- Follow-up verification: focused Chromium and WebKit journeys passed on local
  Python 3.12 and 3.13; the web suite passed 648/648, the production build
  passed, and the full Python 3.13 dashboard/server suite passed 508/508 with
  existing dependency deprecation warnings. Inspected all twelve 320px
  light/dark screenshots across both engines and three filter values: labels
  and 39/1/40 counts remain readable without clipping. Physical keyboard
  behavior and native macOS picker selection remain unverified locally.
- After review and publication, the coordinator must verify the exact commit's
  Dashboard macOS Python 3.12/3.13 jobs and Linux dashboard jobs, then report
  CI and live activation separately. Local approval alone does not establish
  that remote gate.

## 2026-09-25 — Use one contextual run disclosure button (Checkpoint 1)

- The run history now derives one bulk action from its existing section and
  recorded-event state plus parent Diagnostics and saved settings when present.
  Mixed state offers Expand all; fully open state offers Collapse all. The same
  button node and existing read-only handler serve both actions. Absent delivery
  and saved-settings sections do not block Collapse all.
- Web tests passed 648/648 including the equal-refresh assertion, and the
  production build passed. The failed-review
  browser case passed 12/12 in Chromium and 12/12 in WebKit across light/dark
  320×568, 390×844, 768×1024, 844×390, 1280×720, and 1440×900. It verifies
  one 44px mobile control on the Explore row, no horizontal overflow or write
  requests, and button identity/focus through label changes. Captures are under
  `/tmp/aflow-contextual-disclosure-{chromium,webkit}-artifacts/` as
  `failed-review-{engine}-{theme}-{width}x{height}.png` and `-expanded.png`.
- Compared 320px and 1280px light captures with the frozen demo and the prior
  combined WebKit capture. The extra disclosure-control row is gone in both
  engines. The failed-run fixture still differs from the demo's running-run
  content and includes extra disclosures and a follow-up draft; those are
  existing domain differences outside this checkpoint. Demo SHA-256 remains
  `2465c0ac2bfef90af2b98a537ad5ac3e931b927c23a78379a8c532ce4dcbadf4`.

## 2026-09-25 — Document the managed plan lifecycle and disable packaged managers

- Packaged workflows now inherit `manager_enabled = false`; starter workflows
  remain disabled by omission. Explicit per-workflow opt-in and checkpoint/final
  review remain available.
- Documented the default-on plan consumer, five lifecycle directories,
  receipt-backed sequence dependencies, shared capacity and claims, explicit
  requeue, and manager-independent repair upgrades in operator and bundled
  assistant guidance. Preserved the accepted UI description rather than
  carrying the failed source worktree's stale layout claim.
- Ported the isolated queue, delivery, failure, and restart scenario from the
  failed run's uncommitted Checkpoint 8 work. The focused config, docs, consumer,
  and repair suite passed 209/209, and all packaged workflows resolved with
  managers disabled. Full integrated and browser gates remain Checkpoint 6.

## 2026-09-24 — Keep the Runs history filter readable at 320px (Checkpoint 1)

- Reproduced the populated 320×568 defect before the edit: the native select
  measured 69.05px in Chromium and 54.05px in WebKit, with `Visible` clipped
  while New run and More were 44px high and the page had no horizontal overflow.
  At widths through 359px, hide only the on-screen `Run history` label and keep
  the accessible native select at a non-shrinking 128px. Wider layouts retain
  their visible label and existing spacing.
- The disposable browser fixture archives one of 40 runs. At 320px, both engines
  show all of `Visible`, `Archived`, and `All history` with the expected 39/1/40
  results. The select is 128×44px, New run is 84.14×44px in Chromium and
  90.5×44px in WebKit, More is 48×44px, and the header row is 54px high.
  `All history` has 40.65px of measured arrow room in Chromium and 30.05px in
  WebKit after text, padding, and borders. No horizontal overflow appeared at
  320×568, 390×844, 844×390, 1280×720, or 1440×900, light or dark. The original
  local Linux keyboard selection check passed, but the portable follow-up above
  now verifies keyboard focus/navigation separately. Touch access, focus and
  selection after resize, an open More menu after resize, and selection/trigger
  focus after an equal Refresh passed.
  Screenshots were inspected in both engines; artifacts are
  under `/tmp/aflow-mobile-runs-filter-cp1-20260924/` as
  `runs-filter-{chromium|webkit}-{light|dark}-{visible|archived|all}-320x568.png`,
  plus `all` captures for the four wider viewports.
- The focused responsive case passed in Chromium and WebKit. The existing
  populated Runs follow-up fidelity journeys passed 5/5, web tests passed
  633/633, the production build passed, and `git diff --check` was clean.
  Reviewed integration with the sidebar, exact-SHA CI, and live activation
  remain coordinator gates.

## 2026-09-24 — Prove CP8 held-request termination before Review (follow-up v01)

- The held-refresh fixture now matches Playwright requests by object identity
  and records `requestfinished` or `requestfailed` for every routed preflight.
  It releases the held route, unregisters the handler, waits for those terminal
  events, then rechecks ready Review for the restored default launch. Success
  and failure evidence carry bounded route roles, identities, and outcomes.
  In Chromium and WebKit captures the held request was canceled, the `16`-turn
  and restored-default requests finished, dirty acknowledgement stayed checked,
  and Review opened with zero Start calls.
- An initial Chromium full run timed out on `networkidle` even though all six
  routed requests had terminal events and Review was ready; background traffic
  continued. The final fixture waits on the recorded terminal events instead.
  Web tests passed 633/633 and the build passed. All five CP8 cases passed in
  both engines on Python 3.12 and on Python 3.13 in Chromium. The held case
  passed five isolated repeats per engine. Exact-SHA CI and live activation
  remain unverified coordinator gates.

## 2026-09-24 — Drain overlapping CP8 held-refresh routes (Checkpoint 1)

- CI run `36069910428` (Ubuntu Python 3.12, `desktop-dark-held-refresh`)
  intercepted two preflight requests and continued only one; Review stayed in
  `loading`. The local capture reproduced four routed requests in the controlled
  window: one held, an additional default-identity request, a `16`-turn request,
  and the restored-default request. The latter three continued and returned
  ready responses. The route fixture now holds only the first request, releases
  it and unregisters the handler on every exit, and records a hashed plan path
  with workflow, team, and turn identity in its sanitized trace. It requires
  the restored request's completed response, dirty acknowledgement, and visible
  ready Review state before the read-only click. Production code was unchanged.
- Web tests passed 633/633 and the production build passed. All five CP8 cases
  passed in Chromium and WebKit on Python 3.12, and in Chromium on Python 3.13.
  The held-refresh case passed five isolated repeats per engine on Python 3.12.
  Reviewed publication, exact-SHA CI, and live activation remain coordinator
  gates.

## 2026-09-24 — Preserve a new follow-up filename after run selection

- Move the follow-up draft reset and selected-run ref update before paint when
  the selected run changes. CI caught a fast filename edit being erased by the
  later passive reset, causing the default name to be submitted instead.
- Web tests passed 633/633, the production build passed, and the failed-run
  follow-up browser journey passed.

## 2026-09-24 — Keep New run preflight steady during passive refresh

- A same-selection background refresh now keeps the last ready working-tree
  inspection visible and Review start available until the new response arrives.
  Selection or committed-default changes still block on a fresh inspection;
  explicit Refresh still shows loading. A changed result replaces the old one
  and applies its confirmation requirement.
- A held-response component test covers the pending and changed-result states.
  Web tests passed 633/633, the production build passed, the launch-review
  browser matrix passed 12/12 in Chromium, and two previously failing cases
  passed in WebKit.
- CI then showed the browser check could accept an implicit-workflow inspection
  before the explicit workflow selection settled. The check now waits for the
  preflight response with the exact plan and workflow. It also waits for the
  changed-files disclosure to close and verifies every path is hidden. The
  launch-review matrix passed 12/12 in Chromium, with four selected cases
  passing in WebKit after these test changes.

## 2026-09-24 — Native Strands worker adapter

- Installed and verified Strands CLI 0.1.2 with a DeepSeek Flash live ACP
  prompt. Added a fresh-turn ACP adapter, discovery, skill-target detection,
  and fake-peer coverage for success, stderr pressure, provider errors,
  interruption, timeout, and process cleanup. The adapter passes the private
  provider env-file path to Strands without recording API keys in AFlow turns.
- Kept native usage events in the raw structured channel; no token estimate or
  resume capability is inferred. The account's DS4.1f credential source is
  Reasonix's private `.env` file with Strands-compatible provider variable
  names.

## 2026-09-24 — Repair the CP8 pre-click evidence race

- CP8 now takes its contractual pre-click snapshot from the bounded predicate
  after screenshot and anchor work, when the visible preflight is ready, the
  required acknowledgement is checked, and Review is enabled. It compares
  stable launch choices across review opening while retaining exact choices,
  consequence, heading focus, sanitized failure evidence, and zero Start calls.
- A desktop-dark browser case holds a same-choice preflight refresh after the
  first ready capture, observes loading, releases the response, and requires
  the final readiness predicate before the ordinary click. Both Chromium and
  WebKit passed all five CP8 cases and repeated desktop-dark cases. The
  desktop-light-dirty changed-files case passed in both engines. Inspected
  desktop and 390px review captures in both engines; the review, consequence,
  choices, and focused heading remained visible with zero allocation. Captures
  are under `/tmp/aflow-cp8-race-captures-{chromium,webkit}`.
- Web tests passed 632/632 and the production build passed. Full server suites
  passed 493/493 on Python 3.13 and, on repeat, 493/493 on Python 3.12 (three
  warnings each). The first 3.12 full run had 492 passes and one intermittent
  `landscape-light-dirty` follow-up test failure: preflight refreshed back to
  loading between its ready check and changed-files assertion. That exact case
  passed in isolation. It remains a separate timing risk outside this focused
  CP8 repair. `git diff --check` passed; changes are uncommitted for review.

## 2026-09-24 — Stabilize New run review and disclosure CI cases (Checkpoint 1)

- Reviewed the exact failing CI logs: the Ubuntu CP8 case timed out waiting for
  Review start after its click, while the macOS case read the native `open`
  attribute immediately after closing Show changed files. CP8 browser evidence
  now records the visible launch form, preflight status and request identity,
  committed-config/default read summaries, dirty acknowledgement, Review state,
  region visibility, feedback, and focus before and after the click. Readiness
  and click failures save a screenshot and sanitized JSON trace in test
  artifacts; no auth headers or private response bodies are recorded.
- The Python 3.12 full suite exposed a timeout in the CP8 desktop-light case at
  the ready-and-Review-enabled wait. The fixture now scopes the preflight and
  dirty-worktree checkbox to the visible launch form, waits for the required
  checkbox to be visible and checked, and then waits for ready preflight and an
  enabled Review action. The passing pre/post trace keeps the same launch
  inputs, opens the visible review with heading focus and consequence text, and
  records zero Start run calls. The evidence points to fixture synchronization;
  no product source change was needed. The existing held-response component
  regression also verifies that an invalidated review stays visible with
  actionable feedback and can be reopened against refreshed defaults.
- The changed-files test now waits for the native disclosure to close and
  checks that every returned file path is hidden again. It adds no delay and
  retains the opening, path, and acknowledgement checks.
- Validation: web tests passed 632/632 across 29 files; the production build
  passed with its existing large-chunk warning; full server suites passed
  492/492 on Python 3.12 (3 warnings, 1082.61s) and Python 3.13 (3 warnings,
  972.80s). The focused CP8 desktop-dark case passed twice each in Chromium and
  WebKit on Python 3.13; the CP8 desktop-light case passed twice in Chromium on
  Python 3.12. The changed-files desktop-light-dirty case passed twice each in
  Chromium and WebKit on Python 3.13. `git diff --check` passed. Inspected the
  final 1280×720 desktop and 390×844 phone review captures: the review and
  consequence remain visible, focus stays on its heading, and the final Start
  run action remains separate from opening the review. No run is allocated by
  review. Checkpoint changes remain uncommitted for review.

## 2026-09-25 — Scheduling and repair policy API contracts (Checkpoint 6)

- Added authenticated, revisioned project scheduling REST/MCP endpoints over
  the shared settings service, plus plan queue projections with identity,
  claims, reason, dependency, run, and effective capacity. Existing failed
  plan requeue remains the shared revision-checked REST/MCP operation.
- Added typed global and per-workflow repair-threshold actions under the
  existing pair lock. Null removes a workflow override; guided projections
  show declared and effective values with inheritance provenance.
- Contract tests cover defaults, auth, stale revisions, read purity, queue
  parity, and threshold inheritance across REST and MCP.

## 2026-09-24 — Background automatic plan consumption (Checkpoint 5)

- Added server-owned, per-project scanner election and stable two-scan
  observation for direct in-progress plans. API mutations and filesystem
  plan/run artifacts wake the scanner; periodic passes cover missed events.
- Automatic starts use the managed control-plane path with a durable plan key;
  the daemon resolves the current global default workflow/team and requires
  isolated worktree lifecycle.
  Admission rechecks plan bytes and opt-out under the shared project lock.
- Added focused process, restart, capacity, opt-out, file-change, invalid-plan,
  and transport-boundary tests. Scanner shutdown leaves workflow units running.
- Serialized shared-main publication with a separate primary-root lock; the
  admission lock is never held for fetch, merge, or push.

## 2026-09-24 — Admit numbered plans after delivered predecessors (Checkpoint 4)

- Added a durable series inventory at the shared admission lock. Known lower
  members survive deletion, and duplicate or malformed members hold their
  series while unrelated plans retain capacity.
- Linked-worktree launches include numbered files in the verified launching
  checkout and coalesce copies of the same filename across checkouts. A linked
  member clears only with matching receipt-backed delivery evidence, including
  when its successor is launched later from the primary checkout.
- Delivery credit requires the same plan identity's Done ownership and a
  matching valid published receipt with a completed lifecycle record.

## 2026-09-24 — Resume requeued plans through the supported control-plane transport

- Requeue now uses the control-plane REST default, which maps both HTTP and MCP
  requests to the same project bearer scope before daemon resume.

## 2026-09-24 — Release repaired publication lineage gates

- Publication keeps historical failed receipts and blocks unrelated delivery
  until a descendant in the recorded resume lineage has a valid published
  receipt for the same configured target. Pending, failed, malformed, and
  unrelated receipts do not release the gate.

## 2026-09-24 — Repair managed requeue and lifecycle crash access

- HTTP and MCP requeue now carry the checked journal source through the plan
  service, using a stable request key for retries and reporting rejected resume
  admission while retaining the corrected plan for retry.
- Normal plan access and direct controller entry recover prepared lifecycle
  moves before reading affected paths. Recovery keeps exact byte, inode, and
  identity checks and leaves colliding files untouched.

## 2026-09-24 — Classify failed plans and requeue corrected originals (Checkpoint 3)

- Added a journaled, revision-checked lifecycle move for original plans. It
  preserves stable identity and run provenance, rejects destination collisions,
  and recovers interrupted moves without accepting replacement files.
- Invalid content enters `needs-plan-change`; confirmed inactive terminal
  execution failures enter `failed`. Failed publication retains its claim and
  gates unrelated publication until the owning lineage repairs its receipt.
  Authenticated REST and MCP requeue validate corrected content and resume a
  recorded eligible run through shared admission. Owner-stopped runs remain
  ineligible.

## 2026-09-24 — Check direct resume ownership at admission (Checkpoint 2 repair)

- A validated CLI resume marker retains its source and plan provenance role,
  while shared admission now checks current source inactivity under the lock
  before exempting the source's historical plan claim. Active and uncertain
  sources cannot start a second controller for the same plan.
- Marked-resume regressions cover live ownership, uncertain ownership, and a
  running-looking source with a confirmed worker exit. Synthetic resume tests
  now record terminal source ownership where continuation is expected.

## 2026-09-24 — Keep one unresolved run claim per plan (Checkpoint 2 repair)

- Shared admission now records a verified plan identity and checks live,
  uncertain, reserved, and pending-startup owners under the project lock.
  Distinct request keys cannot launch the same plan concurrently; exact replay,
  confirmed inactive retries, supported continuations, and parallel different
  plans remain available. Daemon, worker fallback, and direct controllers pass
  their plan paths to the same boundary. Server conflicts use a bounded
  `project_plan_claim_conflict` response with HTTP 409.
- Added process-race, daemon, and direct-controller regressions for duplicate
  plan claims and prelaunch cleanup.

## 2026-09-24 — Align resume availability with admission (Checkpoint 2 repair)

- The daemon's read-only resume preview now shares admission's authoritative
  predecessor inactivity check. Stopping a unit while controller metadata still
  says running leaves resume unavailable; terminal evidence permits it. The
  mutation still rechecks under the project lock.
- Server fixtures now use terminal evidence for successful resumes, retain an
  uncertain-source rejection regression, and check the exact nonce-bearing
  worker environment alongside project isolation and the configured secret.

## 2026-09-24 — Prevent duplicate continuation ownership (Checkpoint 2 repair)

- A project admission now rejects a distinct successor while another live,
  held, or uncertain successor claims the same predecessor. The check runs
  under the existing project lock and recovers lineage from launch, startup,
  and controller artifacts when a journal entry is absent. Exact retries and
  confirmed inactive successor handoffs remain available.
- Verification: 333 runtime/repair tests and 327 admission/daemon/CLI/resume/
  settings tests passed; Ruff and whitespace checks passed.

## 2026-09-24 — Add shared project launch admission (Checkpoint 2)

- Added a primary-project reservation journal and lock shared by managed starts,
  resumes, daemon workers, and direct controllers. Canonical run evidence keeps
  uncertain launches charged until confirmed inactive; pending startup input
  retains its plan claim and reacquires capacity before launch.
- Closed the rejected-start and rejected-resume transient instruction leak by
  publishing those instructions only after admission succeeds. Focused tests
  cover repeated capacity rejections and a subsequent admitted launch. The
  server returns HTTP 409 for the bounded capacity conflict. Verification passed:
  333 runtime/repair tests, 319 admission/daemon/CLI/resume/settings tests, the
  focused REST capacity test, Ruff, compilation, and whitespace checks.

## 2026-09-24 — Restore New run preparation and review on the published baseline (Checkpoint 1)

- Advanced the managed worktree to published `fdc9100f` and ported the preserved
  New run presentation while retaining current run refresh, Actions, Teams,
  Settings, and history behavior. The preparation form leads with Plan,
  Workflow, Team, and Maximum turns; secondary choices and full preflight paths
  remain behind named disclosures. Review keeps allocation behind its final
  Start run action and returns focus to the preparation trigger on Cancel.
- Browser capture checks now wait for the focused review heading to settle in
  the viewport and verify the consequence text is visible before capturing.
  Clean and 12-change desktop, 390×844 phone, and 844×390 landscape states were
  captured in light/dark Chromium and WebKit. The frozen reference remains at
  SHA-256 `2465c0ac2bfef90af2b98a537ad5ac3e931b927c23a78379a8c532ce4dcbadf4`;
  it depicts Runs detail rather than New run, so New run visuals were checked
  against the accepted hierarchy and interaction criteria. No material gap was
  found in the inspected captures; physical devices were not tested.
- Validation: web tests passed 632/632 across 29 files; the production build
  passed with its existing large-chunk warning; the full server suite passed
  492/492; the broad WebKit responsive/demo/follow-up/launch/pagination suite
  passed 77/77. After the final capture-wait refinement, launch-review cases
  passed 12/12 in Chromium and 12/12 in WebKit, and the history completion
  pointer case passed 2/2 in each engine. Captures are in
  `/tmp/aflow-cp3-port-20260924/chromium-final-captures-v3` and
  `/tmp/aflow-cp3-port-20260924/webkit-final-captures-v3`.
- The old evidence worktree remains unchanged at `d23cb6f8` with its six-file
  diff SHA-256 `5c5254e62eebaefa37d091cb1d01dff49133ad86364c32abac9addcb41c3a4b7`.
  Checkpoint changes are left uncommitted for normal review.

## 2026-09-24 — Revalidate Teams browser journeys on compact-row candidate (Checkpoint 1)

- Reproduced the stale desktop `Inherited roles` locator on `b81563a`: it
  matched six nested summaries in the assignment-first DOM. Updated browser
  checks to target the visible primary Worker/Reviewer rows and named nested
  disclosures while retaining inheritance source, override/restore, save/reload,
  launch identity, and viewport assertions. The stale compact-progress check now
  verifies both the accessible full label and visible `1/2 approved` text.
- The 320×568 held-pointer case uses real wheel and keyboard input to reveal the
  stage control, verifies bounds and `elementFromPoint`, and dispatches real
  pointer events. It retains the 0.5px geometry limit and waits for three stable
  animation frames before measurement; no product source changed.
- Validation: web tests passed 629/629; build passed; focused Teams matrices
  passed 10/10 in Chromium and 10/10 in WebKit; the full server suite passed
  480/480; broader WebKit responsive/progress/fidelity tests passed 70/70.
  An earlier full-server attempt had one transient CP8 mobile-dark review timeout;
  that case passed alone and the final full-server rerun was clean.
- Inspected populated 1280×720 desktop and 320×568 phone light/dark captures in
  both engines. Primary assignments remain visible before collapsed Advanced
  details, keyboard focus is visible, and the responsive checks report no
  horizontal overflow. The production build retains its existing 534.64kB
  chunk-size warning. Scope is the browser test module, DEVLOG, and this plan;
  no checkpoint commit was created.

## 2026-09-24 — Add local macOS AFlow dashboard launcher

- Added `scripts/aflow_ui` for an explicit open-or-update choice when the local
  dashboard is running. An update fast-forwards the local source, refreshes its
  editable tool, synchronizes the p100 workflow pair with a backup, and opens
  the dashboard. The script stops only the recorded UI process and refuses to
  update while local workflow workers are active.

## 2026-09-23 — Prioritize Teams assignments in the selected editor (Checkpoint 1)

- Teams now presents effective Worker and Reviewer assignments first for legacy,
  family, standalone, and inherited stages. Secondary roles, prompts, routing,
  identity, conversion, and declared TOML remain reachable in closed Advanced
  details; inherited primary values stay nonmutating until Override is chosen.
- Compact Settings headers retain accessible section labels and Save all changes
  semantics while showing shorter labels at compact widths/heights. Existing
  reload, draft, route, prompt, conversion, and exact save ownership remain
  intact; the reload browser selector was updated for the new disclosure nesting.
- Validation: focused web tests passed 148/148, the full web suite passed 610/610,
  the production build passed, and the selected-editor plus Settings reload
  browser gate passed 12/12 in Chromium and 12/12 in WebKit. Screenshots for
  the initial legacy editor were inspected at 390x844 and 1280x720 in light
  theme; publication, CI, and live activation remain coordinator-owned.

## 2026-09-23 — Repair progressive run-detail CI gate (Checkpoint 1)

- Fast-forwarded to reviewed baseline `32f267f112253b415c91bc7fdeda5a37e94f9e46`.
  Updated only the three existing browser modules to use the labelled Actions →
  Adjust path, direct named disclosure summaries, truthful missing/partial
  evidence, and separate Current work lead/executor facts. Existing request,
  count, identity, safety, held-history geometry/scroll/focus/hit-target, and
  refresh assertions remain intact.
- Scoped Current work and Recent activity spacing/composition in `RunOverview.tsx`
  and `styles.css` removes redundant section/content gaps while keeping the
  existing readable font size. Mobile event disclosures retain 44px targets;
  their time/label/action row and full-width facts row preserve all three
  source-backed events without truncation. Final representative captures are
  86px Current work and 156px Recent activity on mobile, with 72px and 87px on
  desktop.
- Reproduced the original Recent activity failure at 220.6875px in Chromium and
  215.875px in WebKit, then fixed it without changing the 200px bound. Final
  validation: web tests passed 605/605 across 29 files; the production build
  passed; both Chromium and WebKit target browser suites passed 70/70; and the
  full server suite passed 469/469. Screenshots covered light/dark desktop and
  mobile captures, including the repaired event disclosure layout. Checkpoint 1
  is verified; no checkpoint commit was created.

## 2026-09-23 — Keep preview accessibility and partial totals truthful (Checkpoint 2 review repair v05)

- Preview checkpoint strips now omit an unavailable aggregate-approval clause
  from their `aria-label`, title, and screen-reader text while retaining known
  checkpoint-state evidence and known aggregate counts. Detail strips continue
  to disclose the full aggregate uncertainty.
- Compact rows and anchored previews now qualify partial totals in checkpoint
  positions, such as `CP5 of at least 11`; complete totals, known approval
  counts, terminal rows, and detail presentation remain unchanged.
- Validation: focused web tests 83/83, full web tests 609/609, production build,
  Python compilation, and Chromium/WebKit `run_rows` checks passed. Fresh
  populated light/dark desktop and mobile screenshots were inspected, and the
  frozen demo remains byte-identical. Settings, launch cancellation, request
  replay, the shared editable runtime, publication, CI, and live activation
  remain coordinator-owned.

## 2026-09-23 — Omit unknown-count filler from row previews (Checkpoint 2 review repair v04)

- Anchored row previews now remove only the `approval unknown` or `total unknown`
  clause from one-known-count progress summaries. The known aggregate remains
  visible as `N checkpoints` or `N approved`, alongside known checkpoint
  position, turn usage, run identity, and partial/actionable notices.
- Added progress and shared-row regressions for valid-total/unknown-approved and
  valid-approved/unknown-total states. Detail mode still renders the complete
  truthful uncertainty strings and compact-row projection remains unchanged.
- Validation: focused web tests 80/80, full web tests 606/606, production build,
  Python compilation, and Chromium/WebKit `run_rows` checks passed. Fresh
  populated light/dark desktop and mobile screenshots were inspected, and the
  frozen demo remains byte-identical. Settings, launch cancellation, request
  replay, the shared editable runtime, publication, CI, and live activation
  remain coordinator-owned.

## 2026-09-23 — Keep Escape-closed row previews closed (Checkpoint 2 review repair v03)

- Selection-opener restoration now arms a one-use local focus guard before
  moving focus and consumes it in the matching row focus event. Escape restores
  the selection control without scheduling the focus preview to reopen, while
  ordinary focus, pointer hover, and explicit-toggle behavior remain unchanged.
- Component and populated Chromium/WebKit regressions move focus from the
  selection control to its sibling preview toggle, press Escape, verify selection
  focus, and wait beyond the 300ms open delay to prove the preview stays closed.
- Validation: focused web tests 76/76, full web tests 602/602, production build,
  Python compilation, and Chromium/WebKit `run_rows` checks passed. Fresh
  populated light/dark desktop and mobile screenshots were inspected, and the
  frozen demo remains byte-identical. Settings, launch cancellation, request
  replay, the shared editable runtime, publication, CI, and live activation
  remain coordinator-owned.

## 2026-09-23 — Keep dated rows compact and pointer focus stable (Checkpoint 2 review repair v02)

- Pointer-opened row previews no longer record a focus-restoration opener, so
  hovering the selection control and pressing Escape preserves focus on an
  unrelated search/control. Focus-opened selection previews and explicit-toggle
  previews retain their existing opener-specific Escape restoration.
- Path-derived dates remain in the selection control's accessible name and the
  anchored preview, but no longer add a third visible line to compact rows.
  Component and populated Chromium/WebKit checks cover the 56–72px desktop
  height, readable mobile rows, date reachability, and all three focus paths.
- Validation: focused web tests 76/76, full web tests 602/602, production build,
  Python compilation, and Chromium/WebKit `run_rows` checks passed. Populated
  light/dark desktop and mobile screenshots were inspected, and the frozen demo
  remains byte-identical. This is a scoped Runs/All runs repair; Settings,
  launch cancellation, request replay, the shared editable runtime, publication,
  CI, and live activation remain coordinator-owned.

## 2026-09-23 — Preserve row preview focus and loading truth (Checkpoint 2 review repair)

- Non-modal row previews now remember the actual selection opener for focus- and
  pointer-triggered opens, keep unrelated search/control focus in place, and
  restore the preview control only after an explicit toggle open. Loading
  enrichment is included in the selection button's accessible name while the
  compact row continues to omit visual missing-progress filler.
- Added focused component/global loading regressions and extended the populated
  `run_rows` journey for selection, pointer, and explicit-toggle Escape focus
  behavior across the existing viewport/theme/engine matrix. The frozen demo
  remains byte-identical.
- Validation: focused web tests 75/75, full web tests 601/601, production
  build, Python compilation, and Chromium/WebKit `run_rows` checks passed;
  populated light/dark desktop and mobile screenshots were inspected. This is
  a scoped Runs/All runs review repair; Settings, launch cancellation, request
  replay, the shared editable runtime, publication, CI, and live activation
  remain coordinator-owned.

## 2026-09-23 — Keep deployed Runs rows compact and within the viewport (Checkpoint 2)

- Shared run rows now give the full title its own primary line, retain status and
  one meaningful progress fact, bound long global project identity, and omit
  unknown duration/approval/count filler while preserving zero, partial, stale,
  and failed evidence. The existing hover/focus/touch preview keeps full title,
  project, stable ID, and known facts without shifting siblings; Escape restores
  the preview control's focus.
- Added component regressions and a populated `run_rows` browser matrix using
  the deployed long project label at 320, 390, 768, 844×390, 1280, 1440, and
  390×420 in light/dark Chromium/WebKit. Screenshots were inspected against the
  frozen reference; the reference remains byte-identical.
- Validation: focused web tests 72/72, full web tests 598/598, production build,
  Python compilation, and the Chromium/WebKit `run_rows` checks passed. This
  remains Runs/All runs presentation only; Settings, launch cancellation,
  request replay, and the shared editable runtime remain preserved. Publication,
  CI, and live activation remain coordinator-owned.

## 2026-09-23 — Reorder deployed Runs detail around current work (Checkpoint 1)

- The Runs detail overview now leads with the readable run identity, truthful
  status/timing, current task and recorded executor facts, then an actual latest
  result and three meaningful timestamped activity events. Lifecycle noise,
  empty result/payload placeholders, expected future delivery states and the
  duplicate compatibility block are omitted from the default view without
  removing their recorded evidence.
- Checkpoint history, delivery receipts, time details, counts and compatibility
  progress are closed named disclosures. Expand all retains checkpoint selection,
  exposes the full evidence, and sends no operational writes. Adjust run and
  restart-admission detail stay behind their existing labelled action paths.
- Added component/presentation regressions and the populated ten-checkpoint
  `run_detail` browser fixture. Chromium and WebKit light/dark desktop/mobile
  captures were inspected against the frozen reference; the reference remains
  byte-identical. This checkpoint changes Runs presentation only; Settings,
  launch cancellation, request replay and the shared editable runtime remain
  outside its scope. Publication, CI and live activation remain coordinator-owned.

## 2026-09-23 — Preserve concurrent Settings refresh failures

- Cached detail failures are now tracked by skill name and aggregated with
  list-level errors only at render time. A successful refresh clears only its
  own failure, so another stale skill's old content, revision mismatch, and
  actionable error remain visible until that skill recovers.
- Deferred coverage fails one cached refresh, lets another changed skill
  succeed afterward, verifies the failed error and old content remain, then
  retries the failed skill and checks the new revision on Save.
- Validation: focused Settings tests passed 86/86; the full web suite had
  584 passed and one unrelated RunDashboard failure in the concurrent
  cancellation-owned area; the production build passed; and the Settings
  browser journey passed 1/1 in Chromium and 1/1 in WebKit. Review,
  publication, CI, and live activation remain coordinator gates.

## 2026-09-23 — Retry stale skills after failed reload detail reads

- Settings now compares each cached skill detail revision with the current
  summary revision when selecting or resolving the selected-skill effect. A
  failed newer-revision read keeps the old editor usable and the actionable
  error visible, but the mismatch remains stale so a later selection retries.
- The deferred regression loads two skills, fails a changed non-selected
  refresh, retries it on selection, preserves old content while pending, and
  verifies the successful new revision is used by Save.
- Validation: focused Settings tests passed 85/85, full web tests passed
  584/584, production build passed, and the Settings browser journey passed
  1/1 in Chromium and 1/1 in WebKit. Review, publication, CI, and live
  activation remain coordinator gates.

## 2026-09-23 — Reconcile cached skill baselines during reload

- Clean Settings reloads now refresh every previously loaded skill whose list
  revision changed, while retaining the visible old content until each detail
  read settles. An accepted detail response removes only a draft still equal to
  the superseded baseline; genuinely newer drafts remain owned by GlobalSettings.
- Deferred regressions cover non-selected revision refresh, equal and failed
  detail reads, the no-op-draft changed-reload path, revision-sensitive save
  requests, and existing stale-response/discard behavior.
- Validation: focused Settings tests passed 84/84, full web tests passed 581/583
  with two unrelated RunDashboard failures in the concurrent cancellation-owned
  area, the production build passed, and the Settings browser journey passed
  1/1 in Chromium and 1/1 in WebKit. Review, publication, CI, and live
  activation remain coordinator gates.

## 2026-09-23 — Preserve Settings content during clean reload

- Settings reloads now retain loaded config, server, and skill domains while
  replacement reads are pending, equivalent, or failed. Changed responses
  reconcile through the existing owners; confirmed dirty reloads still discard
  only the authorized drafts and invalidate superseded skill reads.
- Deferred unit coverage verifies visible TOML values, independent-domain
  failure, dirty discard, and stale skill responses. A disposable built-app
  journey holds /api/config and passes in Chromium and WebKit, covering
  editor identity, selection/scroll/disclosure continuity, failure retention,
  changed guided values, and no config writes.
- Validation: focused Settings tests 81/81, full web tests 580/580, production
  build passed, and the new browser journey passed 1/1 in Chromium and 1/1 in
  WebKit. Review, publication, CI, and live activation remain coordinator
  gates.

## 2026-09-23 — Consume complete credentials in fidelity diagnostics

- Python failure-text and browser alert scrubbers now consume the complete
  value after `Authorization`/auth markers, including optional `Bearer` or
  `Basic` schemes, while retaining the bounded long-token fallback. Snapshot
  size limits and the existing artifact routing are unchanged.
- A focused browser regression covers short bearer, token, cookie, password,
  secret, and long-token values in both representations without exposing the
  synthetic values. The regression passed in Chromium and WebKit.
- Validation after the repair: web tests 568/568, production build passed,
  and the authenticated built-app fixture passed 5/5 in Chromium and 5/5 in
  WebKit. The combined WebKit gate reached 56 passed with one unrelated,
  transient phone live-controls timeout; its isolated rerun passed 1/1.

## 2026-09-23 — Make real-app fidelity readiness deterministic

- The populated fidelity fixture now points project readiness at the valid
  disposable control-plane configuration pair already created by
  `control_client`. A clean CI home previously rendered the truthful
  configuration-required notice at y=128 and displaced the dashboard host to
  y=219; the fixture now exercises the same ready state on every host without
  hiding or changing production warnings. The desktop `<=128` assertion and
  all existing themes, variants, widths, and anchor limits remain unchanged.
- Before that assertion, the real app now waits for `document.fonts.ready` and
  two render frames, then retains one bounded failure snapshot containing
  semantic/header geometry, rendered notices/alerts, scroll/focus, font state,
  configuration signals, and computed styles. Failure JSON and a viewport PNG
  use `AFLOW_BROWSER_ARTIFACT_DIR` (CI path:
  `artifacts/responsive-browser/ui-demo-fidelity-*-failure.{json,png}`), fall
  back to pytest `tmp_path` locally, and redact token-like text.
- Validation on Linux `codex`, source baseline `e7f4643005cf6bac5ad1d0883a81375c7aa45ae3` plus this uncommitted patch:
  web tests 568/568; build passed; Chromium fixture 5/5; WebKit fixture 5/5;
  full server suite 458 passed. The combined WebKit responsive/fidelity gate
  reached 55 passed but failed the pre-existing CP8 `desktop-light` case, and
  its focused rerun failed identically with preflight status `loading`; CP8
  was not changed. Disposable failure probes wrote the JSON/PNG pair under
  `/tmp/aflow-fidelity-readiness.QrvZPp` and verified the expected y=219
  warning evidence and redaction. Coordinator CI remains required.

## 2026-09-23 — Complete project plan pagination (Checkpoint 1)

- The control-plane plans route now accepts path cursors up to 512 characters;
  run-list cursors remain 64. The web client requests `limit=100`, follows each
  page's exact final path with `URLSearchParams`, preserves server order, requests
  the terminating empty page for exact multiples, and reports repeated
  cursor/path or missing-cursor responses as actionable incomplete loads. Failed
  page/auth/transport responses remain errors, while dashboard refresh preserves
  the prior plans and selected identity.
- API coverage exercised 1,101 records in 12 requests, 200 records in three
  requests including the empty third page, an empty first page, encoded
  Unicode/punctuation, repeated cursor/path, and later-page failure/retry.
  Control-plane tests passed 62/62, including the long-path, 512/513, run-bound,
  authentication, and project-containment cases.
- A disposable fixture seeded 120 completed plans before a Ready plan. The
  built app/server selected the exact path, completed read-only preflight, and
  opened review with zero starts at 1280×720 and 390×844 in Chromium and WebKit
  (2/2 in each engine). Focused web tests passed 164/164, the full web suite
  passed 577/577, and build/lint passed. Publication, CI, activation, and the
  real live picker remain coordinator-owned; this entry makes no deployment
  claim.

## 2026-09-23 — Retain Teams geometry failure artifacts in CI

- Teams geometry failures now select `AFLOW_BROWSER_ARTIFACT_DIR` when CI
  provides it, create that directory before writing, and retain the existing
  browser/viewport-specific JSON and PNG names. Ordinary local runs still use
  pytest `tmp_path`; the bounded snapshot contains geometry and selected
  computed/viewport/scroll/focus data only, with no tokens, cookies, or request
  bodies.
- The dashboard CI job creates
  `${{ github.workspace }}/artifacts/responsive-browser` before Chromium server
  tests. The job-scoped path reaches both Chromium and WebKit; every OS/Python
  matrix job uploads it under a unique artifact name with `always()`, including
  a Chromium failure that skips the later WebKit step.
- Validation: a disposable forced Chromium short-viewport failure returned a
  non-zero test result and wrote both artifacts inside
  `/tmp/aflow-cp02-probe.9Q8p2x`; its JSON contained the expected rectangles,
  computed styles, viewport, scroll, and focus fields without secret-like
  fields. The focused landscape/short pair passed 2/2, and the full
  team-family journey passed 7/7 in Chromium and 7/7 in WebKit. YAML syntax,
  workflow ordering/inheritance, Python compilation, and `git diff --check`
  passed. No production Teams markup/CSS or acceptance assertion changed.

## 2026-09-23 — Stabilize Teams family short-viewport geometry evidence

- The published Teams journey added three separate `Locator.bounding_box()`
  reads for the long family title, kind, and metadata. The source layout
  already declares a column flex row with wrapping, and the baseline passed
  five unchanged Chromium repetitions plus the focused WebKit pair locally;
  no production overlap was reproduced.
- A direct 390×420 Chromium/WebKit probe kept document scroll unchanged across
  sequential offscreen `bounding_box()` calls, so scrolling by that API alone
  is not claimed as the root cause. The responsive helper now waits for the
  visible descendants and font/frame readiness, reads all rectangles in one
  settled DOM evaluation with computed styles, viewport, scroll, and focus,
  and writes a bounded JSON snapshot plus full-page screenshot on an ordering
  failure. The original vertical ordering assertions and journey behavior are
  unchanged; no Teams component or CSS change was demonstrated or needed.
- Validation: the exact short/landscape command passed five post-change
  repetitions (2 tests each); the full team-family journey passed 7/7
  viewports in Chromium and 7/7 in WebKit; the focused TeamFamiliesSettings
  suite passed 11/11 tests; the production web build passed. Chromium and
  WebKit light/dark captures at 844×390 and 390×420 were inspected and showed
  readable stacked rows. The frozen demo checksum remains unchanged.

## 2026-09-23 — Repair delayed canonical checkpoint selection gate

- The managed branch was based on `9eb433e4`, so the required published redesign
  baseline `8e9187d9500f5da51ecc97ba65594132c3df4bb5` was merged cleanly before
  source changes. The merge preserved both histories and did not touch the
  primary checkout.
- CI run `35817652794` / Dashboard Ubuntu Python 3.13 job `107042531255`
  exposed a readiness race: canonical checkpoint rows can render when delayed
  `getRunContext` data arrives before `CheckpointHistory`'s selection
  reconciliation effect commits `aria-current`. The original immediate read at
  `RunDashboard.test.tsx:3487` could therefore observe `null`; the reconciliation
  itself preserves exact run/checkpoint identity and was left unchanged.
- The dashboard regression now holds `getRunContext` behind a deferred response,
  resolves it under `act`, and queries the current checkpoint inside `waitFor`
  until the real `aria-current="true"` contract is present. Existing action-order,
  evidence, explicit-selection, equal-refresh, and run-change assertions remain
  intact through the focused suite.
- The Plans-after-Settings-save journey now waits for the acknowledged
  `Workflow settings saved` status before navigating, closing the pending-save
  boundary behind the one-off `App.test.tsx:627` timeout without changing draft,
  conflict, or navigation-guard behavior.
- Verification: the required focused command passed 207/207 tests in five
  sequential successful repetitions (1,035 test executions); the full web suite
  passed 568/568 tests across 28 files; the web build passed; and the disposable
  UI fidelity browser suite passed 14/14 tests. Two earlier full-focused attempts
  exposed unrelated existing follow-up/recovery readiness flakes and passed when
  retried or isolated; no unrelated test was changed. `git diff --check` passed.
- Scope limit: only the two named test files and this log/plan are changed; no
  production selection code, backend/controller/config/workflow code, visual
  redesign scope, live mutation, or publication/CI claim is included.

## 2026-09-23 — Repair CP8 WebKit launch readiness

- The bounded CP8 trace showed that a late committed configuration/defaults
  response could trigger a legitimate replacement preflight whose request body
  was unchanged because default-following values are omitted. The old UI
  identity did not include the committed revision or resolved workflow, team,
  and turn limit, so separate browser reads could observe the earlier ready
  state and the replacement loading state. `RunDashboard` now includes those
  resolved values in the readiness identity while preserving default
  inheritance and changed-input reinspection.
- The CP8 browser journey records only bounded, redacted request identities,
  response statuses, and DOM state. It waits for the same current preflight and
  review eligibility in one atomic snapshot, then retains the exact review,
  cancel, and zero-start assertions. A deferred-response unit regression keeps
  a refreshed committed default from enabling a prior ready inspection.
- Final verification passed: focused web tests (185), full web tests (569),
  build, WebKit desktop-light repeated three times, the four-case WebKit CP8
  matrix, and the four-case Chromium CP8 matrix. Review captures for desktop
  and mobile light/dark states were inspected; the selected plan, `15 · server
  default`, and acknowledged working-tree state remain visible.

## 2026-09-23 — Verify populated calm-workspace fidelity

- Tightened the desktop workspace content boundary while retaining the mobile
  document-flow layout. Populated production captures now assert the ordered
  title/current/latest/recent/disclosure anchors, 56–72px run rows, the
  390×844 Latest result start, and no canonical-fixture legacy warning.
- Selecting a run closes a stale hover/touch preview before detail hit-testing;
  a live stop-review action remains visible but disabled while capabilities are
  pending, then follows the authoritative capability response.
- Chromium and WebKit each passed the final 18-capture running/paused/completed
  light/dark fixture matrix. Full web, build, server, responsive, run-progress,
  and fidelity gates are recorded in
  `docs/ui-reference/calm-workspace/verification.md` against the frozen demo
  checksum `2465c0ac2bfef90af2b98a537ad5ac3e931b927c23a78379a8c532ce4dcbadf4`.

## 2026-09-23 — Settings effective-value hierarchy

- Settings now leads with populated effective profiles, role assignments,
  workflow defaults, and existing content editors. Creation, inheritance,
  conversion, password, installation, and raw configuration controls remain
  available behind labelled native disclosures.
- Team family rows retain stable IDs and inherited-role provenance while the
  existing GlobalSettings draft/save/conflict/Undo coordinator and retained
  Skills editor remain the only state owners across tabs.
- Settings unit tests, the full web suite/build, responsive Settings/team
  journeys, and Chromium/WebKit light/dark desktop/mobile captures passed
  against the frozen reference checksum.

## 2026-09-22 — Add compact run navigation and anchored previews

- Mapped the approved calm-workspace shell tokens into light/dark themes while
  retaining the two-row desktop header, mobile list/detail navigation and 44px
  touch targets.
- Added the shared `RunListItem` for project and All runs lists. Stable exact
  run identities, concise status/checkpoint facts and richer anchored previews
  now share one selection/Back-preserving implementation; row progress stays
  compact while detailed evidence remains available in the preview and run
  detail.
- Extended the disposable fidelity smoke with row height, sibling geometry,
  hover/focus/touch-toggle/Escape checks and six light/dark captures. The final
  Chromium run passed with no page errors or legacy-manifest warnings.

## 2026-09-22 — Repair calm-workspace fidelity capture evidence

- Bound every disposable running, paused and completed fixture to its canonical
  launch manifest and launch phase, and added real bounded turn activity so the
  authenticated capture exercises control-plane identity and recent activity.
- Replaced the broad progress wrapper with distinct latest-result, recent-
  activity and disclosure anchors; desktop/mobile light/dark captures now
  require visible ordered anchors and reject legacy ownership warnings.
- Chromium and WebKit focused captures passed with six production manifests;
  the remaining dense pre-redesign shell is intentionally left for the original
  full-app fidelity checkpoints.

## 2026-09-22 — Freeze calm-workspace fidelity reference

- Added the byte-checked owner demo, local ownership rules, capture matrix,
  anchor contract, intentional-difference manifest, and deterministic running,
  paused, and completed disposable run fixtures.
- Added a real-browser harness that renders the literal reference fragment and
  captures the authenticated built app at the approved desktop/mobile widths
  and light/dark themes. Screenshots and capture manifests remain disposable
  pytest artifacts; later checkpoints will add production fidelity assertions.
- Preserved the approved September 22 UI expectations in `UI_GUIDELINES.md`.

## 2026-09-20 — Reset the same-step cap on plan progress

- Fixed cumulative workflows consuming the same-step safety budget while they
  were advancing checkpoint state. The guard now resets on verified forward
  plan-snapshot movement and still terminates genuinely stalled same-node loops;
  regression coverage exercises both productive traversal and post-progress
  stalling. Refs evrenesat/aworkflow#44.

## 2026-09-14 — Repair assistant documentation CI coverage

- Updated the installed-use documentation check to the assistant's current
  optional-source guidance; the prior wording and heading were removed by
  the interactive operating guide rewrite.
- Check that all four linked reference guides exist in the bundled skill.

## 2026-09-14 — Teach interactive agents current AFlow operation

- Expanded the optional `aflow-assistant` skill from run triage into an
  interactive operating guide, with focused MCP and troubleshooting references.
- Documented UI-hosted discovery, plan/config authoring, startup questions,
  revision/idempotency handling, stop modes, successor recovery, and delivery
  evidence. Live schemas remain authoritative for the connected deployment.
- Removed obsolete standalone-daemon guidance and corrected canonical-store
  installation semantics; customized skill trees remain protected on refresh.
- Validation: skill validator, 93 skill store/install/refresh tests, 35 MCP
  contract tests, reference-link checks, and an isolated full-tree install.

## 2026-09-13 — Keep Settings tabs visible on desktop

- Settings now shows its section tabs from 1200 CSS pixels instead of collapsing
  ordinary desktop windows into a selector. Short desktop windows retain tabs;
  narrower windows keep the selector and existing drafts and navigation state.
- Reduced tab padding preserves full-height targets and the two-row header.

## 2026-09-13 — Inactive owner-only GitHub issue intake

- Added the metadata-only relay, disabled Actions/TOML examples, and activation
  handoff documentation. No workflow, runner, service, credential, or live
  issue was installed or exercised.
- Simplified intake configuration to one schema and retained a single
  transport-owned POST retry budget. Saved start idempotency keys are always
  sent as headers; exhausted mutations use exact read-back or durable attention.
- Cumulative review fixes now start the planner deadline at process launch,
  feed stdin concurrently, and clean only its detached process group on timeout,
  overflow, or SIGTERM. Generated plans and recovered artifacts must have
  pristine checkpoint/step progress; attached plans retain their existing
  partial-progress behavior.
- Focused fixtures cover runner-through-transport retry identity, exact import
  recovery, and real relay-to-host/API/Codex attached and absent routes with
  redelivery. No live provider or activation was used.
- v02 closes the leader-exit descendant gap with bounded group escalation and
  verifies timeout and SIGTERM cleanup without touching an unrelated process.
- Import recovery now drives the real four-attempt transport budget for both
  create and promote, with exact, absent, and conflicting read-back fixtures.

## 2026-09-13 — Expose safe extra-instruction validation errors

- Overlength or otherwise semantically invalid daemon extra instructions now
  return the fixed `invalid_extra_instructions` code, field, and constraint
  message through REST and MCP before run allocation; generic failures remain
  opaque and submitted text is never exposed.

## 2026-09-13 — Admit visible progress after raw traversal settlement

- Deferred the bounded All runs exact-detail pump until every current project
  cursor chain has completed or failed. Raw rows remain immediately searchable
  and navigable; queue reconciliation, stale cancellation and retained progress
  bookkeeping continue while admission is closed.
- Partial project failure releases enrichment for usable current rows while the
  page keeps its incomplete/error presentation. Refresh generations close the
  gate again, and exact generation/row guards prevent obsolete work from
  releasing or populating the new view.
- The change addresses measured enrichment contention during raw traversal; the
  external held-dispatch diagnostic is directional evidence, not a claim that
  the shipped live timing target has been reached.

## 2026-09-13 — Complete raw run coverage with bounded visible progress

- Added compatible `include_progress=false` lists with full daemon authority,
  history pagination and validated canonical original-plan identity. Default
  lists and exact-run GETs remain rich; the raw identity resolver avoids rich
  checkpoint/history reduction and preserves active overlay paths.
- All runs searches complete raw coverage and enriches only rendered exact
  project/run rows with at most four concurrent GETs. Generation, raw-row and
  snapshot guards reject contradictory or obsolete progress visibly as stale.
- Hidden documents suspend admission and cancel work, including late raw-page
  arrivals. Visibility recovery resumes unfinished work; settled mismatches
  remain terminal until a new refresh generation or raw row.
- Observable loading/stale/failed/settled progress states distinguish visible
  enrichment from raw cursor completion. Focused identity/API and deferred web
  tests, build, Ruff and Chromium/WebKit journeys passed; live timing and
  displayed-progress acceptance remain coordinator-owned.

## 2026-09-13 — Reconcile raw inclusive statuses once

- Reconciliation now requests progress-free statuses from inclusive repository
  pages and classifies each returned identity once through a shared private
  boundary. Single-run reconciliation uses the same fresh raw read; unit,
  manifest, controller-terminal and startup evidence remain authoritative.
- `RunRepository.list_runs` accepts a keyword-only progress opt-out while its
  default projected behavior, inclusive legacy/deleted coverage, cursor order,
  and persistence/deduplication semantics remain unchanged. Focused tests cover
  raw/default parity, exact paginated read counts, persistence bytes and fresh
  evidence; the retained cold profile remains coordinator-owned.

## 2026-09-13 — Require dirty acknowledgement in startup browser coverage

- The corrected-plan browser fixture now carries a force-tracked modified
  sentinel and runs with both visible and repository-locally ignored plan
  files. Each variant proves all three startup attempts require one current,
  visible acknowledgement while preserving the full rejection and correction
  journey.
- This demonstrates the prior environment-dependent coverage gap. The exact CI
  skipped-ack timing remains inferred, and no product acknowledgement-reset
  defect has been demonstrated.

## 2026-09-13 — Confirm absent Linux processes before the ps fallback

- A missing Linux `/proc/<pid>/stat` entry now gets a fresh parseability check
  of `/proc/self/stat` and one read-only `os.kill(pid, 0)` query. Only
  `ProcessLookupError` confirms absence and skips `ps`; permission, malformed,
  non-Linux and otherwise uncertain observations retain the existing fallback,
  identity tokens and ownership semantics.
- Focused deterministic tests cover the error matrix, valid-path no-probe
  behavior and a process appearing after a prior miss. This narrow optimization
  addresses the retained profile's 80 identity calls/0.377s; no live profile or
  end-to-end latency claim is made here.

## 2026-09-13 — Separate selected run title and date

- Selected run detail headings now keep the readable plan title and its
  machine-derived date on separate lines at desktop and phone widths. Existing
  run IDs, list rows, and title/date values remain unchanged.
- Focused dashboard/build and disposable responsive browser checks cover the
  scoped visual correction; CI and live screenshots remain coordinator-owned.

## 2026-09-12 — Retain compact run navigation during readiness

- An authorized Runs shell now remains mounted through same-project selection
  while initial configuration is pending. Compact selection keeps its detail
  intent, shows the exact pending run identity, and reveals content only after
  that run's direct snapshot is accepted; Back still restores its row.
- Initial admission consistently disables both boundary and immediate stop,
  including an already-open confirmation, while readable running and pending
  stop evidence remains visible. Focused component and browser regressions
  cover held configuration without changing delayed-worker stop semantics.

## 2026-09-12 — Bind persistent receipt observation to daemon roots

- Persistent unit managers composed for registered projects now resolve reads
  directly under each daemon's validated repository root. Missing receipts are
  still missing and newly written receipts are visible on the next read, while
  unbound restart reattachment retains its existing discovery behavior.
- Focused receipt ownership, collision, late-observation, foreign-start and
  server-composition tests preserve nonce/process identity and terminal-state
  validation without claiming the coordinator's later live performance target.

## 2026-09-12 — Render accepted selected runs before history completes

- Runs now keys project-access evidence and accepted direct snapshots by the
  exact project/run identity. An accepted detail remains readable with its
  header, copy and navigation while history or configuration loading continues;
  stale project/run responses, deleted records and denied project access cannot
  reveal it.
- The history list explicitly reports pending or failed incomplete coverage and
  its loaded count, never treating a pinned selected row as an empty or complete
  history. Launch and configuration-dependent run controls remain gated until
  their existing dashboard context is ready.
- Focused deferred component cases and Chromium/WebKit desktop/phone browser
  journeys hold the history response, prove exact direct detail visibility, then
  release it and retain selection plus usable Refresh. Live latency measurement,
  CI, publication and activation remain coordinator-owned.

## 2026-09-12 — Preserve status projection and composite browser identity

- Recovery test doubles now forward the explicit progress-projection keyword and
  strip synthetic progress only when requested. Global run-row checks qualify
  duplicate full run IDs by exact project label, preserving distinct parent and
  worktree identities without changing production behavior.

## 2026-09-12 — Reduce run loading work and publish partial coverage

- Run-list reads scan filtered history identities, then let the daemon
  reconcile each selected run and attach progress once to its final status.
  Direct repository callers retain progress-bearing defaults and history
  semantics. Isolated profiling records 100 projections per 100-row page.

- All runs now receives cumulative project pages as they arrive. Request
  generations reject callbacks from superseded history, project, refresh and
  unmount lifecycles; a completed project replaces its prior rows so vanished
  runs are removed, while pending or failed coverage retains usable rows with
  an explicit incomplete/stale state.
- Focused deferred-page tests cover progressive visibility, full cursor
  traversal, refresh retention/removal, failures, aborts, identity collisions,
  loaded-only search and genuinely empty complete coverage. Live timing and
  deployment acceptance remain coordinator-owned.

## 2026-09-12 — Keep delivery checks on accessible run identity

- Approved compact presentation no longer exposes full run IDs as row prose;
  focused delivery tests now select and assert the preserved exact accessible
  identity instead. Product presentation and behavior are unchanged.

## 2026-09-12 — Make run history readable without losing evidence

- Compact run rows and terminal summaries show truthful checkpoint approval,
  turn usage, duration and finish evidence. Details retain full identities,
  counters and provenance; explicit selection reveals the loaded row while
  refresh preserves selection and disclosures. Machine title dates and local
  timestamp zones remain distinct from stored identities.
- Canonical history distinguishes checkpoint, whole-plan, outside-page and
  unassigned evidence, with exact response-limit omissions where known.
  Approval deduplication preserves distinct checkpoint/decision records;
  unknown turn ceilings remain unknown.
- Compact timeline disclosures retain individual events. Delivery reports
  review, merge, publication, CI and live evidence separately. Time details
  deduplicate invocation identities, including legacy review/approval pairs,
  and use interval union coverage without inventing missing timing.
- Focused canonical, transport and web checks, the production build and
  retained Chromium/WebKit journeys cover the 16 acceptance mappings,
  screenshot-shaped two-turn run, responsive navigation and 101-run density.
  The reviewer timing is 2m 26s with 6m 13s union coverage. Publication,
  exact-SHA CI and live acceptance remain coordinator-owned delivery gates.

## 2026-09-12 — Discover UI data latency without product changes

- Added bounded browser and control-plane profiling with fixed seed 20260912,
  100/1,000-run fixtures, independent 8/80-event histories, five warm samples,
  labelled fresh processes and ten in-app selection cycles. Useful milestones
  require selected data and accepted request generations; analysis uses one
  browser action clock and identifies Refresh configuration prerequisites.
- Retained sanitized raw samples, canonical projection counts, CPU/wall/IO
  profiles and causal counterfactuals under
  `plans/research/ui-data-latency-20260912/`. Duplicate list progress work is
  actionable; event pagination remains gated on a same-fixture HTTP experiment.
- Separate unexecuted handoff:
  `plans/in-progress/ui-data-latency-improvements-20260912.md`. The owner-facing
  four-second symptom remains unmeasured pending URL/credentials/reference
  identity. Discovery makes no claim of improved product latency.
- Verification: 15 focused runner/browser regressions, Ruff, retained sample
  and profile checks, and whitespace validation passed. Product source, live
  services and shared installations are unchanged. Readability work remains
  independent; coordinator owns integration, publication, full CI and live checks.

## 2026-09-12 — Retain unfinished family wizard across guided tabs

- GlobalSettings now keeps the existing TeamFamiliesSettings owner mounted for
  every guided Settings tab, exposing it only on Teams through native hidden
  semantics. The wizard remains a local draft owner, stays in the shared dirty
  guard, and still resets on explicit reload.
- Added focused GlobalSettings and desktop Chromium/390×844 WebKit coverage for
  exact wizard name, IDs, source, stage, hidden accessibility and reset behavior.

## 2026-09-12 — Capture startup admission failure evidence

- Added failure-only evidence around the existing Chromium startup boundary.
  A single bounded DOM snapshot records the selected plan/workflow, worktree
  preflight state, dirty confirmation, Start button state, and visible startup
  messages; a diagnostic failure cannot replace the original assertion. The
  existing actions, timeout behavior, and admission semantics are unchanged,
  preserving the unexplained CI34675905355 boundary without claiming a product
  fix.
- Verification: the exact corrected-plan Chromium journey passed (`1 passed`);
  an external real-Chromium forced-failure probe retained `forced enabled
  assertion` and emitted 549-byte JSON; the production web build passed. No
  production/runtime/UI changes, publication, release CI, or live activation
  were performed.

## 2026-09-12 — Establish held-context capture before responsive trial

- The responsive browser regression now records the intercepted context route and
  acknowledges capture before the trial click, then releases the real response with
  `route.continue_()` only after the pre-release geometry and hit checks. This fixes
  the test-only `held_context.append((route, route.fetch()))` fetch-before-capture
  ordering and avoids teardown-disposed fetch errors.
- The scroller assertion now permits only the actual bounded `combobox-listbox`
  `role=listbox` popover. No product code changed.

## 2026-09-12 — Make responsive hit snapshots atomic after context growth

- The browser hit helper now resolves one exact visible `ElementHandle` before
  scrolling and trial-clicking. Its final single evaluation rejects detached
  targets, synchronously centers that same element with instant scrolling, and
  captures its rect, viewport dimensions and center hit identity together.
  Positive dimensions, viewport bounds and target-or-descendant hit checks, as
  well as normal trial-click actionability, remain strict.
- A real 320×568 fixture regression holds the run-context response until after
  trial-click, then observes the same Diagnostics element move below the
  viewport before the repaired helper recovers its center. The reversible
  original-helper probe failed at `y=744.34375` against the 568px viewport;
  exact CI scheduling remains inferred. Detached/replaced and covered real DOM
  targets still fail.
- Focused helper nodes passed in Chromium and WebKit (2 each), and the exact
  `phone-portrait` route passed in both engines (1 each). The missing local web
  assets required one prerequisite build; no product files changed. Full CI
  matrix and coordinator deployment/live checks remain downstream gates.

## 2026-09-12 — Reject stale clean settings previews

- A delayed zero-action preview reconciliation now checks the synchronous
  coordinator's latest declarations before invalidating or restoring the saved
  baseline, and the functional React updater applies the same semantic guard.
  A newer custom effort therefore remains the owning draft, while a genuine
  edit-to-baseline reversion still restores saved projections and clears dirty
  state.
- Focused coverage now uses a bounded test-local React effect scheduler to
  release the actual clean callback after the fieldset is enabled and custom
  effort is typed without Enter or blur. The post-fix case retains the
  canonical declaration, preserves pending/error ownership, and asserts one
  effort-only `upsert_profile` with the expected revision, omitting untouched
  fields and server-settings writes. The independent pure transition coverage
  also exercises the functional updater, genuine reversion and late-response
  guard.
- A reversible pre-fix run of the same component case failed at the released
  callback: the old branch changed the indicator to `Preview settled` and
  disabled Save while the typed input remained visible. Its private failure
  log is retained outside the repository; exact historical CI scheduling
  remains inferred.

## 2026-09-12 — Preserve receipts across child-exit cleanup races

- The `aflow ui-worker` startup-receipt failure path now polls its direct child
  before signalling the owned process group and re-polls after any signal
  error. It suppresses an error only when that same `Popen` proves terminal;
  a still-live permission failure remains visible.
- Deterministic coverage exercises exit-before-signal, exit-during-signal,
  and live-signal-denial cases without emitting an OS signal. The exact
  historical macOS scheduling remains unproven; ownership and receipt schema
  are unchanged, and full-suite/build/browser checks remain downstream gates.
- Verification: the focused worker-diagnostics module passed all 17 tests,
  including the original real-child cases; Ruff and `git diff --check` passed.

## 2026-09-12 — Preserve explicit dashboard actions during reconciliation

- Removed the controls-default effect's redundant restart lifecycle reset.
  Deferred initial status now preserves successor confirmation, with existing
  exact owner-stop, inactivity, idempotency and lineage assertions retained.
- Consolidated checkpoint selection and its notice into one run-owned state
  reconciled by a pure functional update. Valid explicit selections survive
  defaults and refresh; actual run changes reset and removed entries fall back
  with the existing notice. Current checkpoint remains an explicit action.
- Verification: 10 restart/successor tests, 15 checkpoint-history tests, two
  dashboard full-context tests and the TypeScript/Vite build passed. Controlled
  CP4/stale-CP5 reconciliation proves the mechanism, not historical CI network
  timing. Full suites, combined integration and live browser acceptance remain
  downstream gates.

## 2026-09-12 — Wait for the exact restart action in dashboard tests

- The CI failure in `ci-378278-mac-failed.log` showed the restart journey
  reaching `RunDashboard.test.tsx:1749` while `Start run` was already rendered
  but `Confirm stop and start successor` was not. The shared preflight helper
  therefore could report readiness at the wrong action boundary.
- Kept production behavior and request assertions unchanged. The test helper
  now requires the intended enabled action, and every restart confirmation path
  explicitly waits for the successor action while ordinary launches wait for
  `Start run`.
- Verification: the bounded `RunDashboard.test.tsx` command passed all 114
  tests and `git diff --check` passed. No build, browser, or full-suite repeat
  is needed for this test-only correction.

## 2026-09-12 — Keep family stage clicks stable during preview refresh

- Replaced the in-flow settings preview paragraph with one shared hosted/fallback
  status fragment: a fixed-size visible indicator, reduced-motion static state,
  and a layout-neutral full live announcement. Existing dirty wording, preview
  errors, draft guards and Save ownership remain unchanged.
- Added a held-response pointer regression for the Base reviewer projection.
  Chromium and WebKit passed 320px reduced-motion phone, tablet and desktop
  cases; the controlled click selected Stronger worker, retained its child
  reviewer override, and returned to Base with `codex.review_final`. The retained
  seven-viewport family journeys also passed in both engines.
- Focused component tests selected 15 cases and the production web build passed.
  Screenshots and geometry artifacts are retained under
  `/root/code/evidence/aflow-dogfood-20260909/family-preview-cp1-*-final-*`.

## 2026-09-12 — Observe startup errors through wrapper exit

- Stabilized persistent-unit startup failure coverage with bounded eventual
  inactivity observation and a held test wrapper proving that `error.json` is
  not process-exit evidence. Production observation remains authoritative on
  the recorded process-birth identity; no production lifecycle or schema
  changed.

## 2026-09-12 — Gate CI UI interactions on actual readiness

- Reproduced the two CI ordering boundaries with held API responses: the
  RunDashboard follow-up section can be visible from the run list while the
  selected-run detail is still pending, and GlobalSettings can render the
  projected effort field while the initial load still keeps its fieldset and
  Save action disabled.
- Updated only the affected component tests to release those real responses,
  re-query connected controls, preserve the corrected follow-up filename, and
  assert the exact `effort: null` save action. This is a deterministic,
  test-only repair; production behavior and expected payloads are unchanged.

## 2026-09-12 — Keep dirty settings headers within two rows

- Use the compact settings section selector through 1399px so all seven
  destinations and dirty/save actions fit across engines; retain the existing
  list/detail breakpoint and draft owner.
- Align inherited browser journeys with family/stage selectors and Team families
  list ownership, retaining exact launch identities, dirty drafts, viewport/theme,
  scroll and focus checks. Exercise the selected Chromium or WebKit engine and
  cover draft/section retention across header presentation changes.

## 2026-09-11 — Preserve execution-summary compatibility beside canonical history

- Added bounded `data.execution_progress` to context bundles using the existing
  manager context, while retaining canonical `data.progress` as the sole
  history, approval and delivery authority.
- Updated the dashboard to prefer the explicit compatibility projection,
  preserve current/finished and recovery-complete prose beside canonical
  checkpoint history, and keep old-only/canonical-only payload handling
  truthful.
- Focused context and RunDashboard regressions cover the coexistence contract,
  old-only unavailable evidence, canonical approval history, and recovery
  completion. Full suites and browser-engine coverage remain CI gates.

## 2026-09-11 — Repair integrated refresh stop-control acceptance

- Updated the checkpoint-history refresh assertion to retain full diagnostics,
  selected checkpoint and restart coverage while asserting the delivered
  `Stop now…` and `Stop after current turn` controls. The focused RunDashboard
  filter passed all 3 selected cases: `inspects selected checkpoint history
  while preserving full diagnostics on refresh`, `requests a boundary stop
  through revisioned control and keeps the immediate stop separate`, and `keeps
  Stop now on the immediate endpoint and confirms its terminal response`; 106
  cases were filtered and `git diff --check` passed. No production behavior
  changed; publication, CI and live activation remain downstream gates.

## 2026-09-11 — Visual run progress and checkpoint history

- Added bounded, read-only canonical progress shared by repository/status,
  REST/MCP and selected Lite/Full context. List summaries carry no history
  arrays; detail retains original checkpoint lineage, evidence-qualified
  counts, historical executor identity, applied/pending changes and truthful
  delivery stages without changing controller or manager authority.
- Added compact global/project progress and responsive checkpoint timelines,
  preserving selection and Back navigation. Follow-up fixes retain stable
  identities, review/repartition lineage, unknown history and exact retry
  roles; proven retry completion references link to matching invocations.
- Cumulative review covered all five checkpoints and eighteen fix versions.
  Focused progress, transport, component, build and lint checks passed, as did
  exact Chromium/WebKit history journeys. Retained viewport/theme evidence
  covers the unchanged layout; CI owns full suites.
- Publication, exact-SHA CI, live activation and physical mobile keyboard /
  browser-toolbar checks remain separate downstream verification.

## 2026-09-11 — Wrap finalized run-summary prose on narrow screens

- Added the focused boundary `.run-detail .dashboard-section > p { overflow-wrap: anywhere; }`. This keeps the complete finalized summary visible without changing stored evidence, native input scrolling, or the explicitly scrollable raw report.
- Added `test_run_summary_wraps_without_document_overflow`, which uses a disposable completed run and a synthetic unbroken commit token in the actual Run details shell. It checks the exact token, the existing run-actions menu, and document/body geometry at 320×568, 390×844, and 1280×720. Controlled no-wrap/fixed results were Chromium 1521→320, 1521→390, 2005→1280px and WebKit 1730→320, 1730→390, 2244→1280px; the summary paragraph scroll/client widths matched at 246, 316, and 934px after wrapping.
- Verification passed with `npm --prefix apps/aflow_app/web run build`, then:

  ```text
  AFLOW_TEST_BROWSER=chromium AFLOW_BROWSER_ARTIFACT_DIR=/root/code/evidence/aflow-dogfood-20260909/run-summary-wrapping-review-20260911/chromium-final-artifacts uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_run_progress_browser.py::test_run_summary_wraps_without_document_overflow --basetemp=/root/code/evidence/aflow-dogfood-20260909/run-summary-wrapping-review-20260911/chromium-final-basetemp
  AFLOW_TEST_BROWSER=webkit AFLOW_BROWSER_ARTIFACT_DIR=/root/code/evidence/aflow-dogfood-20260909/run-summary-wrapping-review-20260911/webkit-final-artifacts uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_run_progress_browser.py::test_run_summary_wraps_without_document_overflow --basetemp=/root/code/evidence/aflow-dogfood-20260909/run-summary-wrapping-review-20260911/webkit-final-basetemp
  ```

  Both browser invocations passed 3 cases. Evidence is retained under `/root/code/evidence/aflow-dogfood-20260909/run-summary-wrapping-review-20260911/`.

## 2026-09-11 — Team-family integrated acceptance

- Completed the family creation, direct inheritance, override restoration,
  legacy conversion, reference-safe stage removal, conflict retention and
  exact-ID launch journeys against disposable configuration.
- Added the inherited-worker runtime regression proving one target attempt while
  reviewer/manager routing stays on the baseline and the next checkpoint
  returns to the baseline worker. Responsive acceptance covers the existing
  seven viewport matrix, light/dark themes, enlarged text, keyboard/focus,
  document scroll and compact list/detail navigation; physical mobile keyboard
  behavior remains unverified.
- Browser screenshots and pytest temporary artifacts are retained under the
  external CP7 evidence directory recorded in the implementation plan. No live
  owner configuration, team, provider or shared service was changed.

## 2026-09-11 — Gate immediate stop on selected-run control admission

- The focused immediate-stop test now holds the admitted capabilities response,
  proves that Stop now is unavailable before selected-run controls initialize,
  releases the deferred response, and awaits the existing control-admission
  boundary before opening confirmation.
- The test still proves no owner-stop request before explicit confirmation,
  exactly one request with the selected project/run/revision and idempotency
  key, the immediate endpoint remains separate from boundary control, and the
  terminal response is rendered. This is a test-ordering repair; no production
  timing or timeout changed.
- The active-plan command passed: `npm --prefix apps/aflow_app/web test --
  --run src/components/RunDashboard.test.tsx -t 'keeps Stop now on the
  immediate endpoint|requests a stop after'` (1 targeted test passed; the
  remaining 101 cases were filtered). `git diff --check` and scoped status/
  stat checks also passed. CI remains responsible for full suites; the
  historical macOS scheduling cause remains unproven.

## 2026-09-11 — Stabilize stop and compact-header readiness checks

- The compact-header test now holds `listProjects` with a deferred response,
  proving the `Projects` context is available before registry identity resolves
  and awaiting the exact full project heading before its title/ARIA checks.
- The delayed stop journey now holds the admitted capabilities response on a
  fresh browser read, proves the exact `Stop now…` control is absent while that
  prerequisite is unresolved, then releases it and waits for exactly one
  control before releasing the fake provider. Existing status, pending-review,
  no-stop, and owner-stopped assertions remain unchanged.
- Controlled probes identify the admitted safe `owner_stop` capability as the
  additional Stop now prerequisite; the historical macOS request-order cause
  remains unproven. No production behavior or timeout changed; cross-browser
  coverage remains.
- Focused App test, web build, and the exact delayed-worker journey passed in
  Chromium and WebKit. Evidence remains under
  `/root/code/evidence/aflow-dogfood-20260909/ci-stop-header-review-20260911/`.

## 2026-09-11 — Run evidence follow-up drafting (Issue 10)

- Added the failed/attention-needed Run details action that shows the proposed
  `followup-<run-id>.md` filename, preserves corrected names after collision or
  permission/unavailable errors, blocks duplicate submissions, and opens the
  exact returned draft in Plans without changing the source run or launching.
- Preserved selected-run response guards, the App navigation guard, and
  PlanPanel's unsaved-edit confirmation while adding the existing MCP sequence:
  `get_run` → `get_run_context` → `create_plan_from_run` → `read_plan` →
  `update_plan` → `promote_plan` → `preflight_run` → `start_run`.
- Automatic adaptation, scoring, rollback, and broad issue closure are not part
  of this simplified evidence-to-plan scope. Focused component/API tests and
  the disposable browser journey cover source-link inspection, completion
  criteria editing, promotion, source-byte preservation, and launch only after
  the explicit Run action; Chromium and WebKit evidence is retained under the
  issue-10 review artifact directory.
- Kept copied evidence inside its quoted Markdown fence even with valid
  1–3-space-indented tilde closers, reported only canonical finalized turns,
  and labeled current-step data as unfinalized. Canonical nested receipt
  diagnostics now remain available with the existing redaction and 4 KiB
  bound.
- Follow-up creation now releases only its own busy state after a stale
  success or failure, preserving stale-response suppression while allowing
  the next draft action. Focused verification passed: 13 `from_run` tests, 3
  MCP/auth contract tests, 143 web component/API tests, the web build, and
  the follow-up browser journey in Chromium and WebKit. Evidence is retained
  under `/root/code/evidence/aflow-dogfood-20260909/issue10-review-20260911/`.

## 2026-09-11 — Stop and recovery integration

- Preserve both reviewed stop-after-turn controls and cumulative failure recovery.
  Validated owner-stopped reviewer boundaries bypass failed-run-only recovery
  classification, including when a worker advances to the next checkpoint.
- Combined resume checks, focused dashboard tests and browser stop/recovery
  journeys verify the integration; exact-revision CI still gates deployment.

## 2026-09-11 — Deterministic read-only guard reports (issue 4)

- Added bounded, ownership-matched canonical/legacy report inputs and a bundled
  Pillow renderer for deterministic external A4 PNG/JSON reports. Explicit `:vr`
  routing preserves silent healthy ticks and grants no recovery authority.
- Preserve finalized-turn evidence, unknown cause and duplicate-operation risk;
  canonical startup/input/override states name the required owner action.
- Bundled fonts, visible truncation and separated fields keep reports readable.
  Install/refresh checks preserve edited skill trees. Focused tests and visual
  checks passed; full suites and activation remain delivery-gate responsibilities.

## 2026-09-11 — Verify the modern multi-project UI boundary (Issue 6, Checkpoint 1)

- Added focused REST and MCP coverage for two independently registered Git
  roots using the same plan basename. Reads, edits, promotions, launches, and
  histories stay bound to the requested project; unknown IDs, traversal,
  foreign roots, and unsafe registration paths are rejected without registry
  mutation.
- Verified the existing shared global workflow pair and server-selected
  executable, environment file, and release identity at each project's
  immutable launch/start boundary. Client executable, environment-file, and
  arbitrary-environment overrides are absent from the transport schema;
  malformed transport errors now use the compact redacted envelope so
  rejected payload values are not echoed.
- Extended the existing disposable installed-wheel smoke to create and
  promote the same plan in two projects, exercise REST and MCP, keep one fake
  worker active across a UI restart, owner-stop it for cleanup, and verify both
  histories with Chromium. No live UI, provider, editable install, or
  production deployment was used.
- The old root-owned `projects.toml`, standalone service, and mandatory frozen
  per-run configuration requirements remain superseded; current execution
  uses the shared live global pair and keeps any launch snapshot diagnostic.
- Smoke teardown now retains each invocation-owned run as soon as it launches,
  owner-stops active runs before stopping the disposable UI, and falls back to
  the installed runtime's exact receipt/nonce-validated persistent-unit stop
  when the temporary UI is unavailable. Failed journeys retain their HOME
  evidence, including partial-launch cleanup.
- Verification passed: the focused REST/MCP/registry nodes, Ruff,
  `uv build`, `git diff --check`, and the installed-wheel smoke with
  `--browser --playwright-python apps/aflow_app/server/.venv/bin/python`.
  Pytest basetemps and smoke temporary HOME were supplied by the invocation's
  private evidence root.

## 2026-09-11 — Separate pending user acceptance from checkpoint gates (Issue 39)

- Bundled `aflow-plan` guidance now keeps executable checkpoint task lists to
  agent-owned implementation and verification. Owner-only physical or mobile
  checks live under a top-level `## User Acceptance Pending` handoff section
  with an owner, prerequisites, exact action, expected result, and pending
  status.
- Execution and review guidance reports implementation delivery separately from
  user acceptance. Pending user checks do not trigger worker retries or reject
  implementation review, while implementation defects, required automated
  failures, and explicit release or approval gates remain blocking.
- The existing parser boundary remains unchanged; the representative plan
  probe confirms that a completed automated checkpoint parses complete while
  the ordinary-bullet manual section remains byte-for-byte pending. Bundled
  delivery is verified through the isolated skill-install test; no canonical
  account-local skill store was refreshed.

## 2026-09-11 — Render truthful All runs loading (Issue 40, Checkpoint 1)

- All runs now waits for the current registry/project/history identity before
  making zero-count or empty-state claims. Initial pending results expose one
  accessible animated status with `aria-busy`; registry and complete/partial
  run failures exit loading with actionable errors, while successful rows stay
  visible during ordinary refresh. Reduced motion leaves the status spinner
  static and visible.
- Added deferred component coverage for registry/run ordering, populated and
  empty success, complete/partial failure, history selection, and refresh
  retention. Added the focused real-browser node
  `test_global_run_overview_loading_journey` with deterministic delayed,
  empty, populated, and error responses plus desktop/phone screenshot output.
- Verification passed: `npm --prefix apps/aflow_app/web test -- --run
  src/components/GlobalRunOverview.test.tsx src/globalRuns.test.ts` (16
  tests), `npm --prefix apps/aflow_app/web run build`, and the exact browser
  node under Chromium and WebKit. The browser commands were:

  ```text
  AFLOW_TEST_BROWSER=chromium uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py::test_global_run_overview_loading_journey --basetemp=/root/code/evidence/aflow-dogfood-20260909/issue40-review-20260911/chromium-final2
  AFLOW_TEST_BROWSER=webkit uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py::test_global_run_overview_loading_journey --basetemp=/root/code/evidence/aflow-dogfood-20260909/issue40-review-20260911/webkit-final2
  ```

  Both passed, and artifacts are retained under the invocation roots
  `/root/code/evidence/aflow-dogfood-20260909/issue40-review-20260911/`.

## 2026-09-11 — Isolate pending-review fixture Git state

- The pending-review fixture now commits a narrow `.aflow/` ignore and binds
  Git's local excludes file to an empty fixture-owned file, so its clean
  worktree proof does not depend on `/root/.gitignore` or another host global.
- Its commit helper also records the generated plan-backup body and provenance
  sidecar rather than hiding them with a broad ignore.
- The scoped module passed with `GIT_CONFIG_GLOBAL=/dev/null`; changed
  worktrees and relocated review evidence retain their existing rejection and
  mapping checks. Production validation is unchanged.

## 2026-09-11 — Synchronize startup inspection readiness (Checkpoint 1)

- Controlled deferred component coverage established the readiness race: the
  global Managed default launches one inspection, explicit Managed launches a
  new identity, and an old result cannot replace the selected request. A
  same-identity refresh now keeps Start blocked and removes the actionable
  dirty acknowledgment until the current result completes; the acknowledgment
  remains available for the refreshed dirty result.
- The preflight panel exposes bounded loading/ready/error state, and launch
  admission compares the stored inspection identity with the current request
  before authorizing Start. Pagination merging, unrelated draft edits,
  launch-time server recheck, startup questions, and stale-response rejection
  remain unchanged. The browser helper waits for explicit ready state rather
  than inferring readiness from a checkbox or clean text.
- Verification: focused RunDashboard filter passed (12 tests), production
  build passed, and the exact Chromium Ready/correction journey passed. The
  prior CI log demonstrated the ordering failure; it did not establish a
  platform-specific cause. Evidence is retained under the invocation-owned
  `/root/code/evidence/aflow-dogfood-20260909/startup-preflight-review-20260911/`.


## 2026-09-11 — Keep recovery browser screenshots portable

- `test_durable_recovery_ui_journey` now writes its unchanged screenshot and
  printed artifact reference beneath pytest's `tmp_path`; retained Chromium
  and WebKit evidence is selected through separate `--basetemp` invocations.
- Built the unchanged production bundle once because this checkout had no
  existing `web/dist`; no production behavior or recovery assertions changed.


## 2026-09-11 — Expose graceful stop and preserve pending review

- Exposed the existing revisioned owner_stop intent through REST, MCP and
  Stop after current turn. Saved intent remains visibly pending until the
  current worker/reviewer call finalizes; Stop now retains exact-unit
  interruption. Neither action implies checkpoint approval.
- Ordinary continuation restores the validated pending reviewer and scope,
  including when the worker completed the final checkpoint. Complete plans
  without pending-review evidence remain rejected; replacement recovery
  remains separate.
- Real delayed-provider Chromium/WebKit journeys verify pending refresh,
  finalized evidence and no unit stop or next invocation. Focused runtime,
  daemon, transport and dashboard checks verify continuation, concurrency and
  terminal controls; production build passed. CI owns full suites.

## 2026-09-11 — Recover explicitly with a replacement worker (issue 36)

- Added durable-evidence recovery to canonical resume, REST, MCP, and the
  dashboard. The owner selects a current configured worker; ordinary Resume
  remains primary. The replacement starts fresh without source inference or
  private session context, preserving exact worktree, checkpoints, pending
  review and successor lineage.
- Successor-owned intent and events bind exact evidence hashes. Admission and
  worker preparation share fingerprint inputs and reject active, unknown or
  unresolved operations. Ordinary continuation rejects unconsumed recovery;
  the first session-capable result persists consumption and its exact session
  together, protecting both sides of the crash boundary.
- The dashboard retains rejected drafts and distinguishes requested recovery,
  worker operation start and recorded session identity. REST/MCP expose bounded
  actionable errors. Existing backup wrapper interfaces remain compatible.
- Cumulative review resolved F1–F6. Independent final checks passed: 40 recovery
  tests, 3 affected session tests, Ruff and whitespace validation. Retained
  REST/MCP, web component/API, production build and Chromium/WebKit fake-provider
  journeys remain valid; full suites belong to CI. No live provider calls or
  historical-run mutations were used. Private evidence remains under
  /root/code/evidence/aflow-dogfood-20260909/issue36-review-20260911/.

## 2026-09-11 — Preserve Unicode JSONL record boundaries (Checkpoint 1)

- Changed shared session extraction, canonical session parsing, and Reasonix
  ACP framing to split JSONL only on LF; CRLF remains accepted JSON whitespace,
  while U+0085, U+2028, and U+2029 remain string data.
- Added focused regressions for LF/CRLF records, trailing empty lines, strict
  malformed/nonobject diagnostics, exact assistant text, and tool-only semantic
  exclusion. Test source uses ASCII code-point construction.
- Read-only proof parsed the two retained captures (182 and 522 records),
  preserved both source files byte-for-byte, and passed the final-result
  contract. Evidence is retained at
  `/root/code/evidence/aflow-dogfood-20260909/jsonl-boundary-review-20260911/`.
- Verification: 22 focused session tests, Ruff, and `git diff --check` passed.

## 2026-09-11 — Reconcile cumulative resume scope and pending full review

- Resume now recognizes only a strict terminal worker transport failure that
  advances exactly one checkpoint: the immutable scope envelope, worker
  receipt, before/after snapshots, and owned plan copy must all agree.
- The successor closes only one-hop scope/routing state, preserves budgets and
  history, opens the exact next worker scope through normal capture, and keeps
  the predecessor run from `keep_runs` pruning. Pending review, partition,
  active-owner, malformed, and changed-artifact cases fail closed.
- Completed cumulative work can resume directly to its configured full reviewer
  after strict worker, plan, clean branch/HEAD, and inactive-unit validation.
  Rejection preserves the focused repair flow; relocation maps immutable worker
  receipt paths before validating them. Source receipts remain unchanged.
- Verification: 50 focused pending-review, relocation, and scope regressions,
  Ruff, and whitespace checks passed. Retained manager/runtime/context and
  daemon/REST/MCP idempotency proof remains applicable. Guard-report eligibility
  was inspected read-only; actual recovery belongs to the coordinator.

## 2026-09-11 — Explain plan backup provenance (issue34)

- Preserve existing backup bytes/names and byte-identical deduplication while
  recording atomic capture metadata, exact source/original associations and
  available run/turn identity. Service and controller lifecycle moves retain
  selected-owner evidence, including cleaned-up follow-ups.
- Capture the initial Ready baseline at supported draft promotion with exact
  revision checks and rollback. Fresh plan identities isolate reused paths and
  shared bodies; legacy or unavailable provenance never invents a baseline.
- Expose authenticated paginated backup history in Plans with readable labels,
  owner-specific capture details and editor draft preservation. Restore/reset
  and automatic deletion remain deferred.
- Verified focused helper, lifecycle, service, daemon and MCP regressions,
  component/build checks and isolated Chromium/WebKit backup journeys.

## 2026-09-11 — Await exact selected run identity in browser helper

- `_assert_run_detail` now retries the exact readable heading and Copy run ID
  with Playwright expectations, then waits for the exact `run` URL query value;
  a previously visible detail cannot satisfy a later navigation assertion.
- Audited the responsive and run-navigation browser helpers for the same stale
  detail assumption; no other identical case was found. Production behavior,
  scenarios and existing identity/geometry assertions remain unchanged.
- Verification: production web build; the exact `desktop` and `desktop-tall`
  responsive cases passed in Chromium and WebKit (2 each); `git diff --check`
  passed. Logs are retained under
  `/root/code/evidence/aflow-dogfood-20260909/run-detail-readiness-review-20260911/`.

## 2026-09-11 — Bind pristine copied plans to managed worktree branches

- Restored the narrow initial managed-worktree rebinding path for pristine
  plans whose recorded branch is blank or exactly the known lifecycle source
  branch. Resume paths and unrelated established or started-plan identities
  remain authoritative.
- Focused lifecycle coverage now verifies exact feature-branch metadata before
  the first harness, source/backup byte preservation, and negative identity
  cases. Full cross-platform verification remains CI-owned.
- Focused node IDs in `tests/test_runtime.py`:
  `WorkflowLifecycleRuntimeTests.test_worktree_accepts_tracked_modified_original_plan`,
  `WorkflowLifecycleRuntimeTests.test_worktree_preserves_unrelated_pristine_plan_branch`,
  `WorkflowLifecycleRuntimeTests.test_worktree_preserves_started_plan_branch_identity`,
  `WorkflowLifecycleRuntimeTests.test_worktree_rewrites_plan_branch_to_feature_branch_before_turn`,
  `WorkflowLifecycleRuntimeTests.test_preflight_worktree_refreshes_primary_and_execution_plan_before_first_turn`,
  `WorkflowPreflightTests.test_existing_git_tracking_section_is_not_reinserted`,
  `WorkflowPreflightTests.test_review_workflow_accepts_numbered_blank_git_tracking_before_run_reservation`,
  `WorkflowPreflightTests.test_valid_git_tracking_identity_is_authoritative_on_in_place_start`,
  `WorkflowPreflightTests.test_preflight_blocks_started_handoff_base_head_mismatch`, and
  `WorkflowPreflightTests.test_preflight_blocks_started_handoff_with_empty_base_head`.
  All 10 passed; `uv run ruff check aflow` and `git diff --check` also passed.

## 2026-09-11 — Complete startup metadata and bound neutral recovery (issue 9)

- Normalize missing or blank Git Tracking support fields for pristine review
  plans through canonical startup preparation, preserving explicit identity,
  plan content, backups and typed rejection of incomplete started plans.
- Add the guard's one durable attempt only for positively proven terminal,
  owned, zero-work pre-controller metadata failure. Preserve observer boundaries
  and use the selected normal launcher, including web/MCP for server-owned runs.
- Keep predecessor request lineage and claim one distinct replacement key.
  Record acknowledged, failed or uncertain external outcomes without relaunch;
  reject conflicting successors and keys. Unknown evidence remains ineligible.
- Focused metadata, CLI/REST/MCP, guard, daemon, installer/refresh and Ruff
  checks passed. Cumulative review resolved F1/F2 and approved the handoff;
  full suites and integrated deployment verification remain CI/coordinator-owned.

## 2026-09-11 — Trust current assistant output for control signals

- Fixed the dogfood failure where a successful review stopped because stderr
  contained an old stop line read from a tool artifact. Agent control signals
  use accepted assistant output; command invocations retain both result streams.
- Turn artifacts distinguish final text from structured transport. Normalized
  final text preserves JSON examples and fences; raw session transport remains
  separate. Analyzer, manager context, and progress summaries share this boundary.
- Recorded terminal outcomes and raw evidence remain intact. Fake runner/session
  tests cover workflow advance, current controls, historical examples, and
  normalized artifacts; authenticated REST/MCP fixtures verify context parity.
- Verification: 538 core tests and 52 subtests, 59 server tests, lint, whitespace
  checks, and the independent prior-finding reproduction passed without providers.

## 2026-09-11 — Align progress browser title assertions (Checkpoint 1)

- Replaced the two stale filename-as-heading expectations with the approved
  `Repair overlay` and `Missing evidence` titles, while asserting each exact
  selected fixture run ID in the current detail and retaining technical path,
  progress, turn, and unavailable-state evidence.
- Audited all browser-test heading and Markdown-path assumptions; no other stale
  presentation contract was confirmed. Web build, both progress browser cases
  in Chromium and WebKit, and the full Chromium server suite passed. Logs and
  screenshots are retained under `/root/code/evidence/aflow-dogfood-20260909/progress-title-review-20260911/`.

## 2026-09-11 — Align readable run-title browser contracts (Checkpoint 1)

- Preserved the shipped readable run presentation while updating browser
  contracts to select fixture records by exact run ID and assert the readable
  detail title separately from the technical ID and URL. Corrected only stale
  filename-as-heading expectations, including the startup-plan browser cases.
- Strengthened All runs navigation coverage for exact project/status/title/ID
  rows and retained the existing draft, payload, revision, geometry, viewport,
  focus, scroll, and action assertions. Chromium/WebKit evidence is retained
  under `/root/code/evidence/aflow-dogfood-20260909/readable-run-browser-review-20260911/`.

## 2026-09-11 — Simplify New Run and verify browser journeys (Checkpoint 3)

- Kept Plan, Workflow, and Team as labelled launch choices with resolved
  defaults; added concise worker-upgrade and reviewer summaries and moved
  verbose membership/step tables under keyboard-accessible Details. Pending
  configuration now says Loading configuration instead of implying a default.
- Extended disposable Chromium/WebKit journeys at 1440×900 and 390×844 in
  light/dark modes for exact run back navigation, history search/filter,
  Ready-plan launch/default and explicit team selection, settings edit/tab/save/
  reload, long mobile Projects context, document scrolling, focus, and
  overflow. Screenshots were inspected; physical mobile keyboard remains
  unverified.

## 2026-09-11 — Preserve action feedback and recover passive read failures

- HISTORY: CI evidence in `/root/code/evidence/aflow-dogfood-20260909/ci-36c7b483-failed.log` showed a rejected control alert disappearing during a later passive snapshot refresh and a landscape hit test using coordinates measured before its DOM query.
- `RunDashboard` keeps project discovery, dashboard reads, selected-run reads, and mutation/action feedback in separate local state. Guarded successful current responses clear only their own read error; passive SSE/poll refreshes preserve rejected control feedback and its draft. Intentional actions and explicit selection clear stale action feedback and failed-start links.
- Refresh retries failed or unresolved discovery through the existing guarded discovery effect. Deferred recovery and repeated-failure regressions verify that only successful discovery clears its error and rejected action feedback remains visible.
- The controlled regression uses deferred subscription refresh delivery: an accepted max-turns 12 control is followed by rejected draft 13, a late snapshot, an independent refresh failure and recovery, and an explicit retry. It preserves the exact `expected_revision` 1/2 payloads and draft 13. The new browser helper waits for real actionability with `trial=True` and checks bounds plus exact target/descendant hit identity in one DOM observation.
- Reviewer verification passed: focused dashboard suite (94 tests), full web suite (341 tests across 22 files), production build, and the exact `phone-landscape` plus `phone-live-controls` cases in Chromium (2 passed) and WebKit (2 passed), with isolated browser-test homes and ports. `git diff --check` passed. Private source/output is retained in `/root/code/evidence/aflow-dogfood-20260909/passive-refresh-review-v02-20260911/`.
- HISTORY: The controlled late-refresh regression fails against the unchanged pre-handoff base at the missing rejection alert. The earlier discovery-recovery proof remains in `/root/code/evidence/aflow-dogfood-20260909/passive-refresh-review-20260911/`. The full macOS timing failure was not locally reproduced; physical mobile keyboard behavior remains unverified.

## 2026-09-11 — Truthful repair progress and historical authority compatibility

- Added read-only original-checkpoint progress to REST/MCP context and the run
  dashboard: repair overlays remain separate, missing evidence has no invented
  total, and current turns stay distinct from the last finalized result.
- New live manager contexts use validated original checkpoint authority while
  retaining overlay task identity. A durable boundary version preserves exact
  pre-change historical reconstruction; routing and turn selection are intact.
- Verified observer, context and runtime regressions, authenticated REST/MCP
  parity, 336 web tests, the production build, and desktop/390px Chromium and
  WebKit progress journeys. Repeat review passed 451 Python tests and 43
  subtests; two host-ignore fixture failures passed with process-local Git
  configuration isolation. Historical analysis preserved payloads and bytes.
- Physical mobile keyboard remains unverified. Publication, exact-SHA CI,
  live activation and issue acceptance remain coordinator-owned.

## 2026-09-11 — Align detached UI MCP discovery (Checkpoint 1)

- HISTORY: CI evidence in `/root/code/evidence/aflow-dogfood-20260909/ci-2053c498-failed.log` showed the real detached web server returning seven approved plan/config authoring tools beyond the stale 14-name UI discovery expectation.
- Updated only the detached server's exact `tools/list` expectation to 21 names. The 14 core names, authenticated request, three resource templates, health, second-start, and owned-stop assertions remain unchanged; no production or core-registry contract changed.
- Verification passed: `uv run pytest -q tests/test_ui_cli.py -k test_daemon_start_health_and_stop` (`1 passed, 14 deselected in 4.62s`) and `uv run pytest -q tests/test_ui_cli.py` (`15 passed in 5.05s`).

## 2026-09-11 — Align CLI startup diagnostic acceptance (Checkpoint 1)

- HISTORY: CI evidence in `/root/code/evidence/aflow-dogfood-20260909/ci-1ae095c-failed.log` showed the non-TTY startup-recovery test still expected the former `inconsistent checkpoint state` wording; production emitted the approved `Plan checkpoint state is inconsistent...` safe message.
- Updated only that stderr assertion to the approved `plan checkpoint state is inconsistent` phrase. The explicit interactive-confirmation, exit-1, no-input, and neighboring parser/recovery wording assertions remain unchanged.
- Verification passed: `uv run pytest -q tests/test_cli.py -k test_cli_requires_tty_for_startup_recovery` (`1 passed, 156 deselected in 0.56s`); `uv run pytest -q tests/test_cli.py` (`157 passed, 129 subtests passed in 5.92s`).

## 2026-09-11 — Complete web MCP authoring (Checkpoint 3)

- Added the authenticated HTTP journey covering tool discovery, global settings
  edits, draft creation, revisioned update, promotion, both plan-list views,
  and launch through the existing fixture-controlled run service.
- Documented the seven web authoring tools, approval and revision behavior,
  stale-write recovery by rereading, global safe-boundary/resume semantics,
  and the separate browser-only project registration and skill installation
  actions. No arbitrary file or credential-bearing MCP surface was added.
- Verification used disposable config/HOME/projects/ports; the current web
  bundle was rebuilt before the server regression suite. Publication, CI, and
  live activation remain coordinator-owned.

## 2026-09-10 — Isolate real CLI test run artifacts (Checkpoint 1)

- `test_cli_workflow_override` now inspects its durable `run.json` to verify
  the run stays beneath the temporary root, records workflow `other`, and
  references the test-owned plan; its existing cwd/HOME restoration remains
  unchanged.
- Verification passed: targeted and full CLI tests (157 tests, 129 subtests),
  `git diff --check`, and a disposable outer-Git-checkout subprocess proof
  (1 passed, with no caller `.aflow` created).

## 2026-09-10 — Await Settings navigation and header actions (Checkpoint 1)

- HISTORY: CI failure evidence in `/root/code/evidence/aflow-dogfood-20260909/ci-fdc9ff2-failed.log` showed the mobile Skills journey waiting for a desktop `tab` after the responsive combobox count was observed as zero, and the macOS Changelog journey observed zero Save buttons immediately after the Advanced TOML editor appeared.
- HISTORY: Source inspection established both as render-order boundaries: `GlobalSettings` conditionally renders the named combobox or tabs, while `useHeaderSlots` registers the hosted header actions in an effect that can settle after the editor itself is visible. No local reproduction is claimed.
- The browser helper now waits for the visible union of the requested combobox and tab before selecting the actual rendered control. The Advanced TOML journey waits for the visible Save action in the hosted header before retaining its exact count assertion; paging, draft bytes, geometry, and read-only assertions are unchanged.
- Verification passed with `cd apps/aflow_app/web && npm ci && npm run build`, `cd apps/aflow_app/server && uv sync --frozen --group dev`, and the complete `tests/test_settings_browser.py` file under both `AFLOW_TEST_BROWSER=chromium` (5 passed) and `AFLOW_TEST_BROWSER=webkit` (5 passed). Only the Changelog and dirty mobile Skills journeys use the selected engine; the toolbar, draft-template, and skill-save/install tests in this same file remain hard-coded Chromium. `git diff --check` also passed.
## 2026-09-10 — Make Ready state and startup corrections truthful (Checkpoint 3)

- Ready remains a lifecycle state: the hosted plan editor now explains that startup
  checks run when the user starts the plan, without claiming an earlier validation.
- Start failures keep the existing plan/workflow draft and show the bounded structured
  server message; a reserved run remains linkable, while a new attempt clears any stale
  failure link when no new run identity is returned.
- Bundled `aflow-plan` guidance and the draft template recommend the exact unnumbered
  `## Git Tracking` heading and its two controller-owned fields, while documenting the
  supported single numbered form for existing plans.

## 2026-09-10 — Stabilize hosted browser ordering and Dashboard CI Python (Checkpoint 1)

- HISTORY: A controlled browser probe held the Restore fetch after the server
  response and before the client acknowledgement. `RunDashboard.tsx` left
  Delete enabled, allowed its confirmation to open, then unconditionally cleared
  it when Restore settled; the output was Delete enabled and Confirm delete
  visible/disabled before release, then hidden after release. This established a
  reachable busy/confirmation ownership race.
- History actions now remain unavailable during a history mutation, and Restore
  cannot supersede an open confirmation. The browser journey retains the exact
  archive disappearance, restore identity, explicit delete, deleted-record
  reload, navigation, geometry, and revision/idempotency contracts while waiting
  on observable settled state instead of fixed delays in the touched flow.
- Live-control admission now uses retrying enabled/value predicates. Dashboard
  CI retains root 3.11/3.12, runs the server on 3.12/3.13 with `UV_PYTHON`
  pinned from the matrix, and fails before server tests if the executed minor
  differs from the declared job.
- Verification corrections were retained: the first hosted probe required a
  fresh web build because `dist` was absent; the first browser pair exposed the
  positional `wait_for_function` argument and then a stale bundle, both fixed
  before rerunning. The component regression used the repository's native
  `.disabled` assertion style after its unsupported matcher failed, and the
  labeled YAML check corrected an initial shell-quoting-only validator failure;
  the final shell-safe pass also used the workflow's actual `Build web app`
  step name before passing.
- HISTORY: The later Ubuntu WebKit evidence from CI `34523507986` (changelog
  source `main884b899`) failed when workflow selection was followed immediately
  by the Advanced options click and the extra-instructions control was absent.
  The route matrix now waits for the exact selected workflow and the disclosure
  `aria-expanded`/textarea visibility predicates. Chromium and WebKit each
  passed all seven route-matrix cases, so the controlled evidence did not
  justify a production disclosure change.
- HISTORY: The original restore-ordering source and output remain in
  `/root/code/evidence/aflow-dogfood-20260909/STATUS.md` and
  `ci-be19c4f-failed.log`. The earlier 13-test WebKit invocation was mixed
  engine coverage because both navigation tests hard-coded Chromium; both now
  use the existing `AFLOW_TEST_BROWSER` selector and its Chromium-only
  `--no-sandbox` behavior.
- Final evidence: the rebuilt web assets passed; the explicit Chromium and
  WebKit commands each ran all 13 selected tests with no skips, including both
  navigation tests and the held restore/delete/reload path. Focused RunDashboard
  85 tests, full web 316 tests, YAML matrix/interpreter checks, and
  `git diff --check` also pass. `actionlint` was unavailable in the environment.

## 2026-09-10 — Complete receipt-backed plan delivery (Checkpoint 2)

- Routed approved publication, CP1 lifecycle finalization, and any resulting
  bookkeeping publication through one terminal delivery boundary; a missing
  bookkeeping commit does not trigger a redundant push.
- Recorded the completion phase on delivery failure so resume can retry the
  exact receipt-backed terminal work without replaying checkpoints or a removed
  worktree. Repeated resumes follow the complete predecessor lineage to the one
  receipt owner and retain every required run under low run-history limits. The
  local bare-remote regression covers the original final-push failure plus
  three rejected terminal retries, clean Done storage, remote receipt identity,
  and preserved failure status.

## 2026-09-10 — Record completed-plan lifecycle ownership (Checkpoint 1)

- Added a receipt-backed completed-plan lifecycle helper that verifies exact
  source and Done bytes, preserves ignored Done storage, and records only the
  owned source/destination Git paths in a narrow `chore/plans` commit.
- Refuses unrelated staged changes and ambiguous ownership while preserving
  unrelated unstaged data; recorded move and commit phases resume idempotently.
- Exact pathspecs are now literal, and receipt-backed staging intent verifies
  index identities after interruptions at either lifecycle staging boundary.
- Destination identity follows Git's clean conversion (including autocrlf)
  while raw on-disk plan bytes remain unchanged through move and retry.
- Focused publication/lifecycle coverage passes, including unusual filenames,
  pre-existing Done copies, conflicts, untracked plans, and interruption cases.

## 2026-09-10 — Responsive release CI readiness repair, Checkpoint 1

- Reproduced the starter ordering with a deferred pure-form response: the empty
  form remains in its loading state until the response settles, then the React
  default effect populates `implement` and `trunk`. The production component
  remains correct; its test now waits for the settled defaults before preserving
  the exact starter action and draft callbacks.
- Controlled live-control ordering with list/detail revision 0, an acknowledged
  revision 1 response, and a delayed old detail read settling afterward. The
  exact first payload remains `expected_revision: 0`, `max_turns: 12`,
  `team: fast__team`, and worker selector `reasonix.new`; the second remains
  only `expected_revision: 1`, `max_turns: 13`. `RunDashboard` now ignores a
  lower revision only for the same run identity; equal revisions still carry
  progress/status updates, and different run IDs are never compared.
- The responsive fixture now persists the acknowledged override and its
  `control_changed` event, so list/detail/mutation and background event sources
  no longer disagree about revision 0 after the first write.
- The first exact full web command recorded 313/315. Its isolated green runs
  were diagnostic only and did not establish unrelatedness or readiness. The
  two demonstrated boundaries were readiness races: the logout case asserted
  the hosted `Runs` heading before the workspace finished mounting, and the
  colliding-role case saved before the existing control-admission wait observed
  an enabled selector. The follow-up adds only those observable waits; exact
  assertions and raw request keys remain unchanged.
- Verification after the correction: `npm --prefix apps/aflow_app/web test --
  --run src/App.test.tsx src/components/GuidedConfigForm.test.tsx
  src/components/RunDashboard.test.tsx` passed 163 tests; the required
  `npm --prefix apps/aflow_app/web test -- --run` passed 315 tests across 18
  files; and `npm --prefix apps/aflow_app/web run build` plus `git diff --check`
  passed. The already-valid Chromium and WebKit commands
  (`uv run --project apps/aflow_app/server pytest -q
  apps/aflow_app/server/tests/test_responsive_browser.py` and its
  `AFLOW_TEST_BROWSER=webkit` variant) each passed 11 tests and were reused
  because this follow-up changes only unit-test readiness fixtures. Durable
  review evidence remains in `plans/reviews/latest_review.md`,
  `plans/reviews/responsive-readiness-cp01-ordering.txt`, and
  `/root/code/agent_flow/.aflow/runs/20260910t185404z-3b1d522d/turns/turn-001/transport.stdout`
  with its adjacent `result.json`. No sleeps, retries, assertion weakening, UI
  redesign, live configuration, publication, CI, or deployment was changed.

## 2026-09-10 — Show release changes in Settings

- Added a read-only Settings → Changelog tab backed by the generated release
  artifact, with date groups, escaped titles, and 20-entry paging.
- Preserved the shared settings/skills draft owner across Changelog navigation
  and covered the responsive desktop/mobile journeys in both browser engines.

## 2026-09-10 — Keep Changelog actions read-only

- Hide the shared Save, reload, and skill-install actions while Changelog is
  displayed, while retaining the truthful unsaved indicator and Advanced TOML
  navigation.
- Preserve General and Skills drafts without writes, then restore their shared
  Save owner when returning to an editable settings section.

## 2026-09-10 — Generate compact release changelog from DEVLOG

- Added deterministic build-time JSON and Markdown changelog generation from
  dated DEVLOG headings, with the source and generator included in sdist builds.
- Added cache fingerprint coverage and focused generator/asset tests without
  changing the existing UI deployment pipeline.

## 2026-09-10 — Approve issue38 checkpoint 1

- Review reran the retained four-case comparison: all passed; both setups preserved
  confirmation before/after detail settlement. No setup-specific failure reproduced.
- Retained complete command output, verified baseline setup provenance, and passed
  diff check. Reused documented post-comparison focused83/full296/build results.
- Zero material findings; checkpoint approved locally. Publication, exact-SHA CI
  and live verification remain coordinator-owned.

## 2026-09-10 — Retain issue38 baseline/pending readiness comparison

- Added four controlled comparison cases to `RunDashboard.test.tsx`: accepted
  baseline versus pending location/clipboard setup, each exercised before and after
  direct-detail settlement. Every case observes list settlement, selected-detail
  settlement, visible/enabled Resume, and Confirm visibility after clicking.
- The comparison source/provenance is recorded in
  `plans/notes/issue-38-stable-dashboard-clipboard-cp01-v03-comparison-source.md`,
  with exact verbose output in the paired `-output.txt` artifact. Both setups behaved
  identically; the setup-specific failure did not reproduce, so no clipboard causal
  link or new production correction is claimed.
- Location and clipboard state are restored per case. Existing URL/privacy, source
  and continuation identity, resume-key, API-call, and confirmation assertions remain
  intact. The pending clipboard implementation and responsive work are unchanged.
- Verification after the added comparison coverage passed once: focused RunDashboard
  tests (83), full web tests (296 across 16 files), web production build, and
  `git diff --check`.

## 2026-09-10 — Resolve issue38 confirmed-selection readiness

- The uncertain-resume test now controls list and direct-detail promises, waits for
  the settled selected-run output (`Review · 2`), verifies that Resume is enabled,
  and enters confirmation only after confirmed selection readiness.
- Existing source/continuation identities, confirmation requirements, uncertain-error
  assertion, API call count, and reused idempotency key remain intact. The correction
  is test-only; the production diff remains limited to clipboard request generations.
- `HISTORY:` The earlier review's 291-pass/one-failure result was the missing Confirm
  resume transition. The corrected controlled journey now passes without retries,
  sleeps, timeout changes, or weakened assertions.
- Verification: focused RunDashboard tests passed (79); full web tests passed (292
  across 16 files); web production build passed; `git diff --check` passed. Responsive
  work, exact sanitized URLs, error/privacy assertions, and cleanup ownership remain
  preserved.

## 2026-09-10 — Isolate issue38 full-suite resume verification gap

- HISTORY: The current-worktree review reported 291 full-web tests passing and
  one failure in `reuses a resume key after an uncertain failure` at the
  missing `Confirm resume` assertion; the focused RunDashboard file passed 79.
- A temporary controlled experiment deferred the list and direct-detail
  promises, waited for the visible Resume action, clicked it, and verified the
  visible Confirm action survived direct-detail settlement. It passed 1 test
  with 79 skipped, so the reported failure was not reproduced by the relevant
  readiness ordering.
- The failing resume journey precedes the modified clipboard test in the file;
  the production diff only guards clipboard completion identity, and the test
  cleanup restores location and clipboard descriptors. No causal clipboard
  fixture defect was demonstrated, so no restart/control repair was made.
  Approval remains withheld pending the required full-suite verification.

## 2026-09-10 — Bind dashboard clipboard feedback to the selected run (Issue 38, Checkpoint 1)

- The focused clipboard journey now releases list, direct-detail, and
  clipboard promises in order, waits for the observed selected-run callback
  and settled detail output, and restores location and clipboard ownership in
  test cleanup. It still asserts the exact success text, one exact sanitized
  project/run URL, call count, rejection text, and secret/URL exclusions.
- A controlled pending-copy selection change reproduced stale success from the
  old run. `RunDashboard` now invalidates copy request generations on selection
  changes and ignores late success or failure from an older request. No URL
  contract, responsive work, or control/restart behavior changed.
- Verification: focused RunDashboard tests passed (79); full web tests passed
  (292 across 16 files); web build passed; `git diff --check` passed. The
  pre-fix stale-feedback reproduction is retained by the regression test.

## 2026-09-10 — Wait for admitted dashboard controls before interaction (Checkpoint 2)

- The rejected-control and capability-admitted selector tests now wait for the
  canonical control baseline and enabled admission, confirm the selected draft,
  and wait for the Save action to be enabled before interacting. The rejection
  test still checks the exact run, revision, error, retained draft, and running
  status.
- Shared launch readiness now requires the clean preflight result and an
  enabled actionable button. The demonstrated successor-restart test also
  verifies the latest preflight request matches its selected plan, workflow,
  and source before preserving the owner-stop, inactive-proof, and lineage
  assertions. No production UI behavior changed.
- Verification: focused RunDashboard tests passed (78); full web tests passed
  (291); the web build passed; the full Python suite passed (1,748 tests and
  223 subtests); `git diff --check` passed.

## 2026-09-10 — Keep synthetic Git maintenance inside fixture ownership (Checkpoint 1)

- CI34493456633 at `24ab54e` failed
  `WorkflowArtifactTests.test_terminal_backup_recovery_uses_original_team_for_merge_teardown`
  during temporary-repository cleanup because `.git/objects` remained nonempty.
  The recorded process trace showed `git maintenance run --auto --no-quiet`
  children; the targeted test itself passed, so this was isolated as a fixture
  lifetime race rather than a production failure.
- Shared synthetic repository builders now set `maintenance.auto=false` and
  `gc.auto=0` with repository-local Git configuration immediately after init.
  Existing user identity, normal Git operations, and workflow/recovery/merge
  assertions remain unchanged; no production architecture change is claimed.
- Verification: clean-Git-config targeted test passed; the process trace showed
  zero automatic-maintenance children and 20 local fixture-config commands.
  The full runtime suite passed with 289 tests and 43 subtests, followed by a
  clean diff check.

## 2026-09-10 — Supply engine-owned merge handoff context (Checkpoint 1)

- Merge teardown now always gives the `aflow-merge` worker exact branch, root,
  worktree, and original/active/new plan paths in a labelled JSON block,
  including when `merge_prompt` is empty. Optional custom instructions remain
  appended in their existing order and cannot replace lifecycle identity.
- Added disposable captured-invocation coverage for ordinary completion,
  terminal integration-only resume, custom prompt ordering, and escaped unusual
  path values. No real provider, live service, or global configuration is used.
- Verification: runtime/config `422 passed` plus `50 subtests passed`, docs
  `23 passed`, Ruff for `aflow`, and `git diff --check` passed.

## 2026-09-10 — Keep MCP on the UI server after standalone removal

- The standalone `aflow daemon` command and `aflowd` executable are removed;
  the existing authenticated MCP registry remains mounted by the UI server at
  `/mcp` and `/mcp/`.
- All 14 tools, three resource templates, bearer-header authentication,
  idempotency/revision contracts, live configuration behavior, UI background
  controls, and independently owned workflow workers remain in the existing
  shared services.
- The retained `aflowd.service` deployment still runs `aflow-app-server`; no
  standalone stdio transport or compatibility alias is provided.

## 2026-09-10 — Exempt untracked lifecycle backups from confirmation (Checkpoint 1)

- Phase-B branch-only preflight was classifying AFlow's newly created
  `plans/backups/` copy as ordinary plan dirtiness, so clean Git environments
  stopped before the first turn. Confirmation classification now ignores only
  untracked records wholly under that exact boundary while retaining raw status
  items, dirty facts, conflicts and tracked/rename protection.
- Added synthetic preflight coverage for backup admission, source and plan
  dirtiness, sibling-path boundaries, tracked/renamed backups and conflicts;
  strengthened branch-only resume coverage to verify the original source bytes
  and backup survive the failed first turn and successful resume.
- Verification passed: 97 focused tests plus 21 subtests, 1,779 full-suite
  tests plus 219 subtests, Ruff and the diff check. Exact-SHA CI and live
  deployment remain post-review coordinator gates; local verification makes no
  deployment claim.

## 2026-09-10 — Prefer parallel development with owned integration

- Persist coordinator guidance to run independent plans in isolated worktrees,
  preserve both sides of overlapping changes, and serialize main integration.
- Require prompt per-plan publication and exact CI/live evidence; isolate shared
  runtime/test resources and keep checkpoint workers within their assigned scope.
- This is operating guidance, not a new scheduler or workflow implementation.

## 2026-09-10 — Record compact UI acceptance requirements

- Record the owner-approved two-row desktop header, mobile hamburger navigation,
  document scrolling and full mobile editing in UI_GUIDELINES.md. Root/web agent
  guidance now references this contract; architecture distinguishes the current
  bounded-pane limitation from the planned replacement. No UI implementation is
  claimed by this documentation update.

## 2026-09-10 — Expose dirty-worktree preflight (CP8 implementation)

- Added authenticated REST and read-only MCP preflight over the shared CP7
  status result, with bounded pages and repository-relative status items.
- Fresh daemon launches now accept the explicit dirty-worktree choice across
  transport, replay, and preparation boundaries while retaining the existing
  structured startup question and conflict/in-progress-operation refusals.

## 2026-09-10 — Repair dirty-worktree lifecycle preflight (CP7 review)

- Lifecycle startup now defers strict Git status inspection only for the
  existing non-Git/unborn bootstrap path, while inspection failures in real
  checkouts remain blocking.
- In-progress Git operation markers are scoped to the selected checkout, so a
  clean linked worktree is not blocked by a merge in the primary checkout;
  conflicts and operations in the selected checkout remain blocking.

## 2026-09-10 — Approve live turn configuration (Checkpoint 3)

- Each source-backed turn reloads configuration before controls and limits; persistent partial choices and retry context survive refresh and resume.
- Limit-driven termination preserves incomplete plan progress; independent incomplete END still fails. Owner stop precedes configuration parsing.
- CP3 approved after 341 runtime/state/runlog tests and 43 subtests, plus 2 focused CLI tests and 8 subtests. Session and supervision refresh remains CP4.

## 2026-09-10 — Approve live launch and resume configuration (Checkpoint 2)

- CLI and daemon startup/resume use the current source; snapshots are optional diagnostics. Explicit choices retain provenance, while execution identity and resume progress remain authoritative.
- Resolved the CP2 metadata writer regression and explicit start-step correction equal to the original start. Verified 293 tests and 131 subtests across CP2/API and focused metadata/replay checks.
- CP2 approved; per-turn reload remains subsequent checkpoint work.

## 2026-09-10 — Close the publication gap and exercise CI portability

- Add explicitly repository-authorized publication before successful workflow
  completion, including in-place and resumed boundaries. Preserve active trees
  and remote history; record the delivered SHA or failed delivery in the run.
- Build web assets and install Chromium before server browser tests; preserve
  the correct platform browser cache when using disposable HOME. Drain PTY output before closing
  its slave, handle protected macOS executables in the no-Node packaging smoke,
  use the macOS boot-session identity, and confirm listener conflicts while
  preserving address reuse for clean restarts. Filesystem identity tests now exercise aliases on case-insensitive disks.
- Probe live loopback listeners before wildcard binding on BSD, and give the
  workspace-shell plan-list mock a valid empty response after project creation.

## 2026-09-10 — Release approved work through main

- Publish the completed Skills/settings, manager-context, and readable CLI work
  to main. Remove the invalid empty CI matrix exclusion so CI can run and the
  existing exact-SHA deployment gate can advance the live UI.
- The active live-configuration plan remains in its execution worktree until
  complete. Local plan approval alone is not evidence of publication or deployment.

## 2026-09-10 — Store manager history outside the live context (Checkpoint 4)

- Added an additive content-addressed JSON evidence kind for complete manager
  history, including turn/decision coverage, implementation attempts,
  active-scope rejections, repartition records, and detailed latest-turn
  semantic diagnostics without embedding raw stdout/stderr bodies.
- Unrecognized structured-stream fallbacks remain reference-only with truthful
  extraction metadata, and legacy decisions without turn associations sort
  deterministically without inventing a turn.
- Schema-v3 live contexts now retain only current decision facts, numeric
  `history_summary`, immutable references, and byte-safe latest-turn/error
  summaries; historical compatibility containers are explicitly empty. Read-only
  reconstruction reuses exact recorded references and reports missing or
  tampered artifacts without writing.
- Updated the bundled manager contract and runtime architecture documentation
  to distinguish the 16 KiB normal target from the 40 KiB hard prelaunch guard.

## 2026-09-10 — Integrate readable CLI status output

- Wired the readable status renderer into the real runner lifecycle and
  removed only the duplicate pre-banner run-ID line; existing resume and error
  reporting paths remain unchanged.
- Added synthetic runner coverage for normal and two-checkpoint progress,
  stderr-only harness failure, pre-turn failure, waiting, owner stop, turn
  limits, observer ordering, and durable run metadata.
- Documented the block layout, TTY-only heading emphasis, plain fallback,
  artifact paths, machine interfaces, no-refresh behavior, and fixture-based
  before/after examples.

## 2026-09-09 — Document prompt template variables (Checkpoint 5)

- Added read-only template-variable metadata to the guided configuration
  projection, sourced from both ordinary and merge prompt renderers. The web
  prompt editors now show scope, fallback behavior, and illustrative examples;
  role/team overrides explicitly remain literal text.
- Verification: guided-config 65 passed; renderer prompt regression 7 passed;
  focused web settings tests 75 passed plus 8 save-draft tests; web build and
  `git diff --check` clean.
  Disposable Chromium checks confirmed the reference below the editor and
  within both 1280px desktop and 390px mobile viewports with no project selected.

## 2026-09-09 — Verify complete manager boundaries (Checkpoint 4)

- Reviewer approved CP4 through `cp4 v01`: 153 manager/context/runlog tests,
  43 runtime manager tests and 7 subtests passed with disposable configuration.
  Read-only incident decision-20 reconstruction measured 37,979 pretty bytes
  versus 32,324 current wire bytes; decision artifact hashes/mtimes stayed
  unchanged. These are reconstruction measurements, not original wire bytes.
- Added sanitized later-boundary fixtures with multibyte summaries, long
  artifact references, active review rejection, Lite-to-Full escalation, and
  executor-provided repartition history. Read-only decision-20 reconstruction
  now asserts unchanged artifacts and exact prompt bytes before and after.
- Added a real deterministic fake-harness worktree integration proving that
  manager prompts stay within the hard limit while primary repository/run
  evidence and turn references remain resolvable.

## 2026-09-09 — Approve budget-failure diagnostics (Checkpoint 3)

- Retain rejected manager input with bounded atomic diagnostics and accurate prelaunch budget reports, preserving checkpoint/turn references and terminal incident evidence.
- Approved CP3 through `cp3 v02`; 203 tests and 7 subtests passed with disposable configuration, plus the diff check. CP4 integration coverage remains pending.

## 2026-09-09 — Bound optional manager history (Checkpoint 2)

- Approved deterministic v3 history projection, bounded omission descriptors,
  and exact-byte reduction while retaining current boundary authority.
  Repartition history participates in final prompt measurement.
- Verification: 111 manager/context tests, 39 runtime manager tests and 7
  subtests passed with disposable configuration roots; no material findings.
  Budget-failure diagnostics remain assigned to Checkpoint 3.

## 2026-09-09 — Keep Skills edits safe during install refresh

- Settings keeps the Skills editor disabled until the shared install result,
  refreshed registry, and selected skill content have all settled. Refresh
  failures remain visible and controls recover without discarding a draft.
- Added deferred unit and Chromium coverage for the post-install list boundary,
  selected-content refresh, failed reload recovery, and real linked saves.

## 2026-09-09 — Replace stale instructions during CLI resume

- Explicit CLI resume now replaces saved extra instructions when text follows
  `--`, clears them with a bare `--`, and inherits them when omitted. Keep
  automatic candidate matching strict and preserve predecessor evidence.
- Doublangu's CP7 reviewer repeated CP6 review after checkpoint-specific
  restart instructions were replayed as run-wide text. This change permits
  correction without a fresh worktree or manual run-state edits. Explicit
  review targeting and scoped recovery notes are planned separately.
- Verification: 153 CLI tests and 127 subtests passed, including replacement,
  clearing, preserved predecessor text and downstream resume admission.

## 2026-09-09 — Validate resumed launches against their frozen configuration

- Prepared-launch validation now loads the verified per-run configuration
  snapshot, preserving its origin for fingerprint computation, instead of
  comparing a resumed run against the daemon's current global configuration.
- Regression covers a global model edit after reservation and continued
  rejection when the saved snapshot is modified.
- Verification: 74 snapshot/control-plane/daemon CLI tests passed; Ruff and
  `git diff --check` passed.

## 2026-09-09 — Bind copied scope evidence on repeated resume

- Rebind inherited schema-v2 envelope paths in memory to the immediate source
  run's copied evidence before resume validation. Envelopes remain unchanged;
  digest, size, containment, UTF-8 and checkpoint-span validation still apply.
- Doublangu's second resume otherwise looked for its first run's paths and
  rejected valid copied evidence. Extend the continuation regression to remove
  the ancestor evidence, bind the local copy, and reject a corrupted copy.
- Verification: 191 relocation/resume/CLI tests and 128 subtests passed;
  Ruff and `git diff --check` passed.

## 2026-09-09 — Resume recorded controller failures

- Allow an explicit daemon resume when the controller has durably finished,
  even if the detached launch receipt still says `launch_started`. The former
  guard incorrectly classified Doublangu's recorded checkpoint-review failure
  as a killed process; reconciliation intentionally retained its terminal state.
- Keep inactive-worker/unit checks and resume bootstrap validation. A launched
  process without controller terminal evidence still requires reconciliation.
- Extend resume tests across failed/interrupted controller states and verify
  unreconciled killed launches cannot allocate a successor.
- Verification: 28 resume/repository/reconciliation/service tests passed;
  Ruff and `git diff --check` passed.

## 2026-09-08 — Avoid formatting-induced manager context failures

- Serialize schema-v3 manager prompts as compact UTF-8 JSON. Preserve every
  field and the 32768-byte hard limit; legacy serialization remains unchanged.
- Reconstructed Doublangu decision 20 from its saved boundary: pretty JSON was
  33611 bytes, compact JSON 28754 bytes, with identical decoded content. The
  original failure reported 33646 bytes; the read-only reconstruction is not
  claimed byte-identical to the original prompt.
- Added ASCII/Unicode regressions where formatting alone exceeds the cap and
  verified existing oversized-input rejection. Manager/context tests: 98 passed.

## 2026-09-09 — Skills settings, workflow supervision, and draft template

- Manager instructions now come from live skill Markdown: the three prompt
  builders contribute only structured runtime data while `aflow-manager`
  (ordinary, note-correction) and `aflow-repartition-checkpoint` (proposal,
  validation, bounded correction) skills supply the system text, read once per
  invocation with no caching. Missing/invalid skills fail before any provider
  starts. Verified with builder parity tests and a fake run proving a save
  between two decisions affects only the later invocation/artifact.
- Replaced global `[manager].enabled` with per-workflow `manager_enabled`
  (optional declared flag, `False` shipped default, workflow override →
  concrete base → defaults → `false` presence-based precedence). Only real
  TOML booleans are accepted; step-level flags and the old global are
  rejected with a targeted message. `resolve_manager_role` takes the selected
  workflow explicitly; frozen run snapshots keep their value while live edits
  affect new runs. Covered by parser/precedence/validation, fake-run routing,
  and snapshot-freeze tests.
- Authenticated Skills API (`GET /api/skills`, detail, `PUT`, batched
  `POST /api/skills/validate` with per-entry verdicts, `POST
  /api/skills/install`) shares the canonical store and the CLI installer
  service (absolute directory symlinks over the exact eleven-harness map,
  refresh-preserving-edits, structured partial results). Lone-surrogate
  validate content returns a bounded per-entry `skill_invalid` verdict (HTTP
  200) instead of an opaque error; covered by an ASCII-escaped `\ud800`
  regression test.
- Settings gained a Skills editor (bundled registry, `SKILL.md` textarea,
  parent-owned drafts/revisions, save coordinator ordering workflow config →
  skills → credentials with per-domain acknowledgement) plus Workflows
  supervision controls (Defaults Enabled/Disabled; per-workflow
  Inherit/Enabled/Disabled with effective value and source). New web drafts
  start from a packaged parser-valid checkpoint template; explicit content
  stays byte-for-byte and no new plan validator exists.
- Verified with the handoff suites (core, server incl. Chromium skills/draft
  smokes, full web suite, production build, ruff, `git diff --check`) except
  pre-existing environmental failures proven identical on the untouched base.
  Automated checks prove filesystem propagation through links, not native
  discovery inside the external harness CLIs (not installed here).

## 2026-09-08 — Refresh unedited skill bundles without losing edits

- Added an explicit `SkillStore.refresh_skills` path alongside revisioned
  saves: missing canonical trees initialize from the package, trees matching
  their recorded baseline refresh to incoming package files (including added
  and removed resources with permission intent), and the baseline advances
  only after a successful refresh.
- Edited skill trees are preserved whole — saved `SKILL.md` plus every
  supporting file — and reported `preserved_edited`; exact reversion to the
  baseline makes a skill unedited again. Trees without metadata are adopted
  only on an exact package match and otherwise protected with an unknown
  baseline.
- Refreshes stage and validate the incoming tree first, mutate under a
  store-owned transaction marker with rollback material for touched files and
  metadata, and restore the prior tree and baseline on ordinary I/O errors.
  Interrupted transactions make reads and saves of that skill fail with an
  incomplete-refresh error until the next explicit refresh verifies the
  rollback; uncertain markers are never silently retired.
- Documented the preserve-whole-edited-skill policy in `docs/installation.md`.
  Verified 41 skill-store tests, 18 refresh tests, and ruff on the store;
  the full store/install suites pass with the injected v1→v2 loader and the
  real bundled package.

## 2026-09-08 — Preserve history browsing and acknowledgement retries

- Refresh every loaded run-history page, reconcile removals, and retain the
  refreshed range cursor; discard superseded list responses.
- Clear history mutation intents after definitive rejection so a newly
  confirmed active-workflow acknowledgement is sent. Uncertain failures retain
  the original key and payload for exact replay.
- Verified 222 web tests, production build, and Chromium with 130 history rows
  retained across refresh at desktop/mobile sizes in both themes; diff check passed.

## 2026-09-08 — Fix Agents & Roles profile overflow

- Narrowed the settings overflow selector to top-level panels and prevented
  profile cards from shrinking inside the shared Agents & Roles scroller.
- Chromium reproduced 236px content clipped into 48px cards before the fix.
  Expanded coverage now checks full card height and access to the last profile
  at desktop/mobile sizes in both themes.
- Verified 220 web tests, both Chromium layout tests, web build and diff check.

## 2026-09-08 — Run history controls and sidebar navigation

- Added revisioned Archive/Restore/Delete record metadata without removing
  workflow evidence or changing launch/recovery idempotency. Active work requires
  explicit acknowledgement; deleted external reads return 410.
- Projected current activity and status reasons from controller/worker evidence
  and versioned preparation-owner identity. Cold and cached API reads stay
  read-only; uncertain outcomes have their own overview group.
- Added separate Runs/settings navigation and editor scrolling, settings-wide
  Advanced TOML, parent-owned editor selections, and discovery deduplication.
  Team/max turns are visible directly; clearing effort preserves the unset action.
- Verified: 78 core tests, 108 server/API tests, 220 web tests, two Chromium
  regressions (desktop/mobile, light/dark), production web build, and diff check.
  Additional project-registry coverage passed with the server suite. Existing
  user workflows and records were not changed; no deployment or tool reinstall.

## 2026-09-08 — Resolve manager evidence outside the execution worktree

- Schema-v3 manager context now declares absolute primary-repository and run
  artifact roots. Prompt instructions resolve repository-relative evidence and
  run-relative reviewer output against those roots, not the execution worktree.
- Verified the stopped Doublangu run's checkpoint hash and reviewer approval
  through the corrected roots without changing historical artifacts. Both Lite
  and Full regressions read exact evidence from a separate working directory.
- Verification: 96 manager/context tests plus 37 manager runtime tests and
  7 subtests passed. No review result is fabricated or inlined as a workaround.

## 2026-09-08 — Preserve frozen identity during worker Resume

- Fixed controller Resume rejecting a copied snapshot because it compared the
  predecessor's snapshot path against the original global configuration path.
  Validated snapshots preserve the predecessor's recorded identity path, while
  configuration fingerprints and continuation identity remain checked.
- Added controller-entry regressions for both CLI/original and worker/snapshot
  identity paths, with invalid live configuration and rejected fingerprint drift.
  The running Doublangu workflow and its historical receipts remain untouched.
- Verification: snapshot, runtime, control-plane Resume and daemon suites passed
  (309 tests, 43 subtests). Reinstating the old path selection in an isolated
  test plugin reproduced the worker/snapshot regression; `git diff --check` passed.

## 2026-09-08 — Global Settings, prompt editing, recovery, and All runs

- Added global domain tabs with one Save all action and changed-only revisioned
  configuration PATCH. Server fields save after workflow configuration; partial
  failures retain unsaved drafts without resending acknowledged changes.
- Added custom model/effort entry, named prompt CRUD/reference-aware rename, and
  role/team text overrides. Advanced TOML shares the draft and preserves invalid
  text for correction. No prompt store or workflow graph editor was introduced.
- Fixed retained-dashboard handoffs and hidden streams. Added failed-source
  restart admission/options, immutable UI source identity, durable sibling
  reservation locking, and same-workflow retries with unchanged plan progress.
- Added the default all-project run view with complete pagination, bounded fetch
  concurrency, ongoing/recent classification, and browser-local recent count.
- Verification uses disposable repositories and in-memory units. Existing user
  runs and backups remain untouched; Reset Plan stays deferred to issue #34.

## 2026-09-08 — Runs navigation, durable startup failures and appearance

- Split New run from run history, removed Overview, and kept global Settings
  available without project selection. Retained drafts, idempotency keys,
  startup-answer generations and successor recovery across navigation.
- Persisted bounded, redacted startup errors and exposed their reason and
  reserved request identity through REST. Run timing now uses controller start
  and terminal evidence; preparation failures never receive a running timer.
- Added system-aware Light/Dark palettes, browser persistence and cross-tab
  synchronization. Simplified details, diagnostics and relevant run controls.
- Verified a real dirty-checkout rejection through the browser and after reload,
  then completed a separate installed-entry-point NO-OP workflow in one turn.
  All smoke artifacts belong to a disposable project; Doublangu was untouched.

## 2026-09-08 — Reusable NO-OP test plan

- Added `aflow noop-plan` with a packaged checkpoint template and preserved-by-
  default external playbooks. Output/count/state overrides and explicit reset
  support repeated and isolated harness/team/workflow experiments.
- Added `aflow noop-step` for deterministic sleep, marker, cleanup, failure,
  reviewer-rejection, and recoverable worker-incompletion actions. Real agents
  still perform normal plan and
  review bookkeeping; the fixture makes no application changes.
- Added CLI/action tests for defaults, live playbook edits, failure diagnostics,
  preservation/reset, validation-before-actions, and scoped file handling.
- The first live test exposed DSH's private sandbox `/tmp`. DSH now uses
  full-access mode, matching AFlow's VM-oriented unattended harness policy.
  The fixture explicitly forbids agents from recreating inaccessible state.
- Validation: 297 tests plus wheel-resource execution passed. Live
  `AstraGLMMuse` clean run `20260908t125833z-5ce674eb` completed DSH implementation
  and Astra approval. Separate run `20260908t125833z-6ff0ddf3` completed the
  labeled incomplete-worker → Astra rejection → manager-selected Muse upgrade
  → Muse success → Astra approval path. Real fixture/setup failures were
  diagnosed separately and were not counted as mock-test passes.

## 2026-09-08 — DSH ACP model selection

- Added DSH ACP session discovery and execution with provider-qualified models,
  dependent effort selection, exact-session output, and capability-gated resume.
- Added timed pipe reads, unattended permission responses, session cleanup,
  and contract tests for selection failures, resume, and transport bursts.
- Cross-harness target preflight now checks the selected session invocation,
  allowing ACP-only model selection without a headless fallback.
- p100 model/team configuration remains host-local; DSH ACP requires its own
  provider bundle and an inherited ZAI_API_KEY for Z.AI Coding Plan requests.

## 2026-09-06 — Bounded continuous-deployment poller for validated main

- Added `deploy/aflowd/continuous-deploy.py`, a standard-library one-shot
  poller that deploys origin main of the fixed public repository only when the
  exact fetched commit descends from the current release and GitHub Actions
  shows a completed successful `ci.yml` push/main run for that exact SHA.
  Missing, pending, failed, or identity-mismatched CI, divergence, unknown
  releases, and network errors defer with a truthful sanitized status and
  leave the running service untouched; candidate code never executes before
  every gate passes.
- The poller keeps its own dedicated clone under
  `/var/lib/aflowd/deploy/source`, refuses unexpected or dirty sources without
  ever resetting or cleaning, holds an exclusive nonblocking invocation lock,
  and reuses the candidate's own `preflight.sh` and `install.sh` with the
  production defaults (release root `/opt/aflowd`, loopback backend, registry
  and managed root unchanged). Attempted deployments preserve the preflight
  snapshot and installer log under `/var/lib/aflowd/deploy/attempts/`; blocked
  preflight temporary directories are discarded. A failed rollout records
  `last_failed_commit` in the atomic `status.json` and is never retried
  automatically until a new candidate appears or an operator passes
  `--retry-failed`.
- Review repairs to the same slice: a successful install records the
  installed candidate as `current_commit` immediately; a nonzero preflight
  defers only with a valid schema-1 snapshot recording
  `safe_to_rollout: false`, while missing, malformed, or safe-but-failed
  output fails the poll; an installer that outlives its bounded timeout has
  its whole process group terminated before the poll reports failure, even
  for a direct `--retry-failed` run; and a failed final status write makes
  the poll exit nonzero instead of reporting nominal success over a stale
  `status.json`.
- Added `aflowd-deploy.service`/`aflowd-deploy.timer` (oneshot;
  `OnBootSec=2min`, `OnUnitInactiveSec=5min`) plus
  `install-continuous-deploy.sh`, which is dry-run by default and with
  `--apply` installs exactly those two units and enables the timer — no broad
  service restarts. `status.sh` now reports the timer state and the saved
  deployment status, gracefully handling their absence. Tests cover candidate
  gates, deferrals, suppression, exclusive invocation, and installer dry-run
  behavior with temporary repositories and mocked CI responses only.

## 2026-09-06 — Rolling browser session login for the remote web client

- The web client now logs in once by posting the deployment bearer to
  `POST /api/session`; the server sets the signed HttpOnly session cookie and
  the token is cleared from memory and never stored in the browser.
- On load the client checks `GET /api/session`: a valid cookie restores the
  workspace without a prompt, a definitive 401 shows Login, and a network
  failure offers Retry instead of a false signed-out state.
- Visible page restoration renews the session before loading projects; real
  pointer/keyboard/focus activity marks the next authenticated
  REST request `X-AFlow-Activity: 1` (at most once per minute, in memory only),
  which the server uses to roll the 30-day session forward; polling and SSE
  never renew it.
- A 401 during ordinary use or the event stream switches to a session-expired
  sign-in that preserves the current project and view after re-login; logout
  waits for `DELETE /api/session` confirmation before clearing the workspace
  and aborts pending requests so late results cannot refill a signed-out
  workspace. Failed logout offers retryable feedback. Header-only REST and MCP clients
  are unchanged.

## 2026-09-06 — Remove remote agent planning and retain plan management

- Removed the remote provider client, interactive planning surface, and its
  transport dependencies; provider choice remains an engine harness concern.
- Added registry-scoped Markdown plan CRUD with SHA-256 expected revisions,
  bounded regular-file checks, atomic updates, and one-way lifecycle moves.
- Rebuilt the web plan editor around that contract and kept durable run REST,
  SSE, and MCP behavior unchanged.

## 2026-09-06 -- Preserve uncomposable project save guards

- Uncomposable project run snapshots now include the durable frozen config path,
  keeping failed migrated runs and nonterminal blockers on the same save path.

## 2026-09-06 -- Preserve historical config-path resume safety

- Project config saves now ignore a failed or interrupted control-plane run only
  when its durable frozen configuration path is known and differs from the
  canonical project config path. Matching or missing paths remain blocked.

## 2026-09-06 — Run progress, live controls, and guided workflow restart UI

- Rebuilt the web run dashboard on the typed checkpoint-5 contracts: the
  overview now renders `selected_start_step`/`skipped_steps`,
  `restarted_from_run_id` lineage, checkpoint name/index/count and bounded
  manager/harness outcomes from the lite context, and live overrides read from
  the `control_changed` journal event.
- The start form gained capability-admitted per-workflow start-step choices
  with explicit skip explanations, bounded extra-instruction lines, and
  boolean answers for `confirm_recovery`/`confirm_worktree_dirty` questions;
  idempotency-key reuse and draft preservation are unchanged.
- Live controls offer only capability-admitted team/selector values, keep
  local edits on stale revisions, and describe next-safe-boundary semantics.
- A workflow change is a guided confirmation, owner stop with CAS, a bounded
  wait for exact `owner_stopped` status and launch phase, then a successor
  start with `restarted_from_run_id`; normal guided restart is available only
  for active owned runs. A lost successor response freezes the exact request and
  idempotency key for successor-only recovery without another owner stop.
- SSE handling keeps the last snapshot through disconnects, resumes from the
  last sequence, deduplicates replay, refreshes canonical status once on
  reconnect, and never maps a network failure to a run transition.


## 2026-09-06 - Retain uncertain successor recovery across navigation

- Keep the exact pending successor request in the workspace shell until the server resolves it. Inspecting another run or switching views preserves the original project, request and idempotency key, with a visible route back to recovery.

## 2026-09-06 - Stop daemon-owned runs before unit startup

- The daemon owner-stop path now creates the standard run artifact directory
  only after an exact control-plane manifest, caller scope, and initial
  revision are verified. It then uses the existing revisioned control,
  event-journal, unit-stop, and terminal launch-phase path without fabricating
  run metadata.
- An owner-stopped launch phase remains authoritative over a persisted startup
  question during status reads, idempotent start replay, and later answers, so
  a stopped pre-start run cannot create a workflow unit.

## 2026-09-05 - Align manager contract and prelaunch failures

- Updated the bundled manager contract and inline precedence rules for live
  schema-v3 reference-only contexts: read the declared checkpoint evidence
  first, inspect the active/full plan reference only when needed, and never
  search alternate plan files.
- Lite manager contexts now persist the already-supported escalate_to_full
  action alongside the prompt's effective eligibility; Full routing remains
  controller-owned and unchanged.
- Context, evidence-capture, and prompt-budget failures now produce one
  bounded invalid manager decision artifact before the controller records the
  truthful run failure. No provider-start event is emitted and failed
  artifacts do not copy plan bodies.

## 2026-09-05 -- Complete explicit resume rehome and team continuation

- Added the fail-closed `--resume RUN_ID --resume-rehome-worktree PATH` path.
  It validates the current primary branch, recorded feature branch and base
  commit, exact registered replacement worktree, and Git operation state before
  allocation. Only contained resume paths are remapped; the source run remains
  byte-identical and the continuation records `resume_relocation`.
- Bound schema-v2 plan/checkpoint evidence before `keep_runs` pruning and
  copied it into the continuation with in-memory reference rebasing, preserving
  envelope bytes, hashes, scope IDs, and source artifacts.
- Added the named configured `--team` continuation override with exact blockers
  for pending routing state and `resumed_from_team`/`resume_team_override`
  provenance. Automatic and ordinary resumes retain strict team matching.

## 2026-09-06 — Add typed workflow starts and successor lineage

- Added canonical launch options across REST, MCP, daemon, and worker boundaries,
  including validated workflow steps, bounded ephemeral instructions, and
  durable request digests without prompt content.
- Expanded capabilities and status with declared, executable, excluded, and
  skipped steps plus configured teams, roles, admitted selectors, and the
  public status vocabulary.
- Added owner-stopped successor starts with immutable predecessor lineage while
  preserving strict resume checks, idempotency, unit exclusivity, and frozen
  configuration controls.

## 2026-09-05 — Project, config, and plan web workspace

- Rebuilt the web shell around one canonical registered project with
  Projects / Configuration / Plans / Runs navigation, registry-backed
  readiness pills (`ready`, `configuration_required`, `blocked`), and guided
  setup: a new or newly registered project lands in the configuration view,
  and a ready configuration links on to plan selection.
- Added typed create/register and non-destructive unregister flows: relative
  managed-root path, display name, main branch, optional initial
  workflow/team, explicit initialize-Git confirmation for register mode, and
  confirmations that state files, history, and plans are preserved.
- Added a two-tab plain-text editor for `aflow.toml` / `workflows.toml` with
  the shared combined revision, validate-only and atomic save calls,
  stale-revision recovery that keeps local text until an explicit
  discard-and-reload, active-run blocker lists, and unsaved-edit guards
  (navigation confirmation and beforeunload).
- Plan create/read/edit/promote now recover from revision conflicts the same
  way, and every failure path preserves the local draft.
- Plan drafts now use the same shell navigation guard as configuration edits;
  list return requires an explicit discard, and lifecycle moves stay disabled
  until the draft is saved. Project creation and ready-config navigation keep
  their successful server result when a follow-up registry refresh fails.
- Removed the dead `transcribeAudio` client method that targeted the deleted
  transcription route; no session/provider/audio UI remains.

## 2026-09-05 - Add revisioned remote project configuration

- Added the authenticated two-document project configuration contract for
  aflow.toml and workflows.toml, with combined revisions, private-pair
  validation, bounded diagnostics, placeholder readiness, and redacted audit
  metadata.
- Saves use per-project compare-and-swap locking and rollback-safe staged
  replacement. Control-plane-owned active or resume-compatible runs block
  edits; terminal and legacy history remain read-only and do not block idle
  configuration.
- Successful saves reload future-run capabilities while preserving existing
  workflow units and durable run state. Focused server/API and frozen-config
  tests cover validation, concurrency, lockout, rollback, and capability
  reload.

## 2026-09-06 - Preserve failed-turn logs in plain output

- Emit the complete stderr artifact path in final plain status records, including
  failures with no stdout artifact. Added a failed-turn regression with a long path.

## 2026-09-05 — Plain append-only CLI status output

- Replaced the Rich live dashboard, alternate-screen viewport, and cbreak
  terminal input with one plain renderer: append-only `key=value` status
  records on stderr, emitted on meaningful transitions, turn finalizations,
  and one final summary, deduplicating identical consecutive snapshots.
- `aflow show` now prints plain ASCII workflow graphs, roles, and teams with
  explicit `[executable]`/`[excluded]` step words, `[terminal]` END markers,
  and `when` transition conditions instead of panels and color.
- Removed `aflow/terminal_viewport.py`, every Rich import and render object,
  cursor restoration, background refresh/input threads, and all dual-mode
  fallbacks. Dropped the direct `rich` dependency from `pyproject.toml`;
  `rich` remains in the lock only as a transitive dependency of the MCP
  transport and is never imported by AFlow.
- Records bound display values without truncating durable artifact references,
  flatten control bytes, preserve Unicode content, and produce identical
  ordered output for TTY, redirected, `TERM=dumb`, and narrow terminals.
  Engine exit codes, stdout machine results, observer events, and workflow
  state transitions are unchanged. The `BannerRenderer` name is retained
  because workflow call sites and the `banner_files_limit` config option are
  unchanged; the implementation is the single plain renderer.

## 2026-09-06 — Native ZCode harness adapter

- Registered ZCode for noninteractive workflow turns with explicit workspace,
  yolo mode, plaintext final output, and literal prompt arguments.
- ZCode 0.16.5 has no per-invocation model/effort flag. Empty AFlow profiles use
  ZCode's own project configuration; unsupported overrides fail validation and
  direct invocation construction rather than silently selecting another model.
- Verified the public runner with a fixture CLI, configuration rejection,
  literal prompt arguments, and recorded turn identity. Full suite: 1,403 tests
  plus 216 subtests; production Ruff, compilation, and wheel/sdist build pass.
  This does not claim a paid live provider or model-selection test.
- Provider usage accounting and the delegation benchmark remain separate work.


## 2026-09-02 — Manager context references and prompt budget

- A 340 KB manager context (incident `20260902t053828z-5cfe3386`, decision
  `manager/decision-005`) failed before Reasonix started with `errno 7:
  Argument list too long`. The same 63,580-byte plan was duplicated across
  `active_plan_content`, `original_plan_content`, `envelope.plan_text`,
  envelope base64, and checkpoint payloads.
- Added a run-local content-addressed evidence store
  (`.aflow/runs/<run-id>/evidence/{plans,checkpoints}/<sha256>.md`) with
  idempotent atomic writes and fail-closed reads; each distinct plan/checkpoint
  byte sequence is stored once per run.
- Scope envelopes now have a reference-only schema v2 (no plan/checkpoint text,
  no copied source-block text); new scopes write v2, v1 artifacts remain
  strictly parseable and readable. Drift validation and repartition rendering
  resolve referenced evidence bytes and materialize source blocks by verified
  byte spans.
- Manager-context schema v3 (selector >= 4) is reference-only: no plan bodies,
  no base64 evidence, no raw reviewer transcript. Runtime boundaries capture
  evidence once by content hash; historical rebuilds never write.
- The inline manager prompt targets 16 KiB with a deterministic 32 KiB hard
  limit enforced before any provider process starts; non-sensitive prompt
  metrics persist in the manager result and analysis labels referenced
  artifact bytes as not model input.
- Manager adapters must advertise the fail-closed `manager_workspace_read`
  capability for reference-only contexts; Reasonix one-shot prompts moved from
  argv to stdin so they can never fail `execve` with `E2BIG`.


## 2026-09-02 — Respect ACP exact-resume capability

- Owned session execution no longer implies that a harness can resume a prior
  session in a new process. AFlow now supplies a persisted session ID only when
  the driver explicitly advertises `resume_with_model`.
- Same-harness hotplug retains its exact-resume requirement, while ordinary
  Reasonix turns start fresh ACP sessions and replace the persisted role
  session reference after success.
- Added a workflow-level regression for a process-local owned ACP executor.

## 2026-09-01 — Allow bounded Reasonix ACP workspace initialization

- Raised Reasonix session-open and configuration-update requests from the
  transport's 10-second default to a bounded 60 seconds, preventing larger
  worktrees from failing before a prompt while retaining the separate
  long-running prompt timeout.
- Enforced one overall request deadline across ACP notifications so progress
  traffic cannot reset the timeout indefinitely.
- Accepted Reasonix's exact same-session `config_option_update` notification
  as the configuration result, while retaining complete-state acknowledgement
  checks before any prompt.
- Ignore the single late correlated response for a request already settled by
  that notification; all unknown response IDs remain terminal mismatches.
- Added focused coverage for new-session and resumed-session negotiation.

## 2026-09-01 — Canonical dynamic project registry

- Replaced runtime static control-plane project tables and broad catalog
  discovery with one atomic, versioned exact-root registry under a canonical
  managed-projects directory.
- Centralized release executable, identity, environment file, and child
  environment at server composition; project records can no longer override
  process inputs.
- Made project daemons lazy and isolated per-project readiness failures so a
  registry update is usable without restarting the remote server.
- Restored exact-root validation clears transient registration failures, so a
  temporarily unavailable cached project recovers without a server restart.

## 2026-09-01 — Audit public documentation scope and accuracy

- Removed the host-specific p100 deployment from public README navigation and
  remote-app setup guidance while retaining it as an explicitly scoped operator
  runbook beside the environment-specific scripts.
- Corrected command and harness inventories, lifecycle-neutral resume behavior,
  automatic startup metadata refresh, current profile examples, subprocess
  stdin behavior, library exports, architecture layout, package metadata, and
  remote-app configuration and authentication boundaries.
- Reframed daemon and deployment documentation around portable product
  boundaries instead of one host's paths, address, and operating procedure;
  removed private deployment assumptions from the bundled guard guidance.

## 2026-08-31 — Enforce production static hygiene (PR-16)

- Base SHA: `ab56e821`; the measured baseline was `38 F401 / 0 F811 / 6 F821
  / 10 F841`.
- Preserved the intentional seven-model API re-exports with an explicit
  `__all__` contract.
- Added pinned Ruff `0.16.5` as a production-only CI gate.
- Final verification: root `1,354 passed, 216 subtests passed`; app server
  `186 passed`; web `34 passed`; compile, package build, lock, and clean-wheel
  checks passed.

## 2026-08-31 — Stop shipping test support and pytest at runtime (PR-15)

- Base SHA: `059b1fb5ad6654f18935b20db7538663361310ad`.
- Moved the shared helper bundle from `aflow/_test_support.py` to
  `tests/_support.py`, removed its 7,369 blank padding lines and dead direct
  execution guard, and preserved the 19-helper wildcard re-export contract.
  The helper changed from 7,644 physical/275 nonblank lines to 504/457.
- Moved `pytest>=8.0.0` from runtime dependencies to the default `dev` group;
  the lockfile keeps it development-only. Root collection stayed at `1,353`
  and the full suite passed with `1353 passed, 216 subtests passed`.
- The rebuilt wheel omits the test helper and `pytest` runtime requirement; a
  fresh Python 3.11 wheel-only install printed `runtime-package-clean`.

## 2026-08-30 — Lift repartition application coordinator out of the workflow closure (PR-14)

- Lifted the 274-line pending-repartition reconciliation and application
  closure, replacing its 12 captured names with eight stable coordinator
  fields and five per-call path/step/team values.
- Added a small live-state bridge returning and assigning the active plan path
  and current step, preserving the unchanged manager-gate and startup-resume
  consumers. Consolidated cycle and application persistence through one
  explicit `_persist_pending_repartition()` helper, preserving the
  post-application path/step-before-final-write and event ordering.
- Base SHA: `3474eeb65e4b80b2561844e72378f666c184dcb2`.
- Verification: repartition `100 passed`; repartition-filtered runtime `7
  passed, 249 deselected, 6 subtests passed`; control-plane resume `4 passed`;
  manager `42 passed`; manager-filtered runtime `35 passed, 221 deselected, 7
  subtests passed`; root `1353 passed, 216 subtests passed`; app server `186
  passed` with three existing dependency warnings; web `34 passed`; web lint
  zero errors with five known React hook warnings; structural AST and
  behavioral-equivalence ledgers passed; Python compileall, package build, and
  web production build passed.

## 2026-08-28 — Lift repartition cycle executor out of the workflow closure (PR-13)

- Lifted the 462-line nested repartition proposal, mechanical-validation, and
  semantic-validation cycle into one private, frozen, module-level
  `_RepartitionCycleExecutor`.
- Replaced its ten captured names with eight stable constructor fields plus the
  two per-call plan paths, `original_plan_path` and `active_plan_path`.
- Kept proposal, correction, candidate, verdict, artifact, persistence, and
  durable transaction contracts unchanged. Invocation, persistence, and plan
  application remain live nested callbacks; automatic repartition remains
  opt-in.
- Base SHA: `388838a794bc713ae9f7c8da8126273a7ff72eb0`.
- Verification: repartition `100 passed`; repartition-filtered runtime
  `7 passed, 249 deselected, 6 subtests passed`; control-plane resume `4
  passed`; manager `42 passed`; manager-filtered runtime `35 passed, 221
  deselected, 7 subtests passed`; root `1353 passed, 216 subtests passed`;
  app server `186 passed` with three existing deprecation warnings; web `34
  passed`; web lint zero errors with five existing React hook warnings; Python
  compileall, package build, and web production build passed. Structural
  ledger and diff checks passed: one executor construction and callback
  injection, eight executor fields, manager gate 11 fields and three manager
  calls, manager executor 11 fields and five call sites, unchanged 274-line
  application helper, and zero legacy closure references.
- No live workflow, editable-tool retarget, service restart, deployment, or
  unrelated file change occurred.

## 2026-08-28 — Refresh manager executor team after live overrides

- Removed `baseline_team_name` from the frozen `_ManagerCallExecutor`
  constructor and passed the current boundary team into all five manager calls,
  so manager metadata and Lite/Full profile resolution follow a team override
  accepted after the workflow has started.
- Strengthened the manager team-override regression to apply the override after
  turn one and verify the manager model changes from the base team to the live
  team while upgrade routing remains correct.

## 2026-08-28 — Refresh manager gate baseline after team overrides (PR-12 follow-up)

- Removed `baseline_team_name` from the frozen `_ManagerGateCoordinator`
  constructor and passed the current boundary value into all three `run()`
  call sites, preserving team-override updates for manager context, upgrade
  eligibility, and target routing.
- Added a manager-enabled team-override regression covering the hotplug chain
  and the executed upgrade selector.

## 2026-08-28 — Lift manager gate coordinator out of the workflow closure (PR-12)

- Lifted the 436-line nested manager gate closure into one private, frozen,
  module-level `_ManagerGateCoordinator`. Its 11 stable constructor fields
  exclude the mutable baseline team; the five boundary inputs are passed per
  call: `baseline_team_name`, `original_plan_path`, `active_plan_path`,
  `new_plan_path`, and `runtime_current_step_name`.
- Kept the manager level helper module-level with an explicit stalled-turn
  threshold, while repartition execution remains nested and automatic
  repartition remains opt-in. Manager action eligibility, Lite/Full selection,
  recovery, resume, durable artifacts, events, and routing contracts are
  unchanged.
- Base SHA: `03992439d006fda19a86f96c73e86ccaae4a8b3b`.
- Focused verification: manager `42 passed`; manager-filtered runtime `34
  passed, 221 deselected, 7 subtests passed`; repartition/control-plane resume
  `104 passed`; runtime/runlog/CLI `434 passed, 171 subtests passed`;
  compileall, structural ledger, callback-equivalence, and diff checks passed.
- Full verification: root `1352 passed, 216 subtests passed`; app server `186
  passed` with three existing dependency deprecation warnings; web `34 passed`;
  web lint zero errors with five existing React hook warnings; Python
  compileall, package build, and web production build passed. Structural
  ledger: one coordinator class/construction and three coordinator calls with
  11 fields and five per-call mutable inputs; one module helper; unchanged
  nested repartition callbacks; one executor construction and five executor
  calls.

## 2026-08-27 — Lift manager call execution out of the workflow closure (PR-11)

- Lifted the 388-line nested `_run_manager_call()` closure into one private,
  frozen, module-level `_ManagerCallExecutor`. Its 16 captured names are now
  12 stable constructor fields plus three per-call mutable inputs:
  `original_plan_path`, `active_plan_path`, and `current_step_name`.
- Lifted the repository fingerprint helper with explicit repository and plan
  paths. Lite/Full decisions, correction eligibility, note scopes, artifacts,
  events, history, failure handling, repartition, and resume contracts remain
  unchanged; gate policy remains inside `run_workflow()`.
- Base SHA: `b674915759b6389498bf370a74fa1e02bf2b92f1`.
- Verification: manager `42 passed`; manager-filtered runtime `34 passed,
  221 deselected, 7 subtests passed`; repartition/control-plane resume
  `104 passed`; runtime/runlog/CLI `434 passed, 171 subtests passed`; root
  `1352 passed, 216 subtests passed`; app server `186 passed` with three
  existing dependency deprecation warnings; web `34 passed`; web lint zero
  errors with five existing React hook warnings; compileall, package build, and
  web production build passed. Structural ledger: one executor class and
  construction, five executor calls, four fingerprint calls, and no
  `_run_manager_call` references.

## 2026-08-27 — Consolidate standard workflow failure finalization (PR-10)

- Consolidated the 13 standard post-context terminal failure sequences into one
  private finalizer, preserving summaries, plan identity, snapshots, banner
  ordering, and all five exception cause chains; the two writer-fallback
  snapshots remain implicit at their original call sites.
- Left the 15 special failed writes explicit, including startup, preflight,
  manager/event, merge, pruning, and max-turn responsibilities.
- Base SHA: `1a9e8b1df478d0ccf8d79b3e2234da80f07a43a4`.
- Focused verification: runtime/runlog/CLI `434 passed, 171 subtests passed`;
  manager/repartition/control-plane resume `146 passed`; compileall and diff
  checks passed.
- Full verification: root `1352 passed, 216 subtests passed`; app server `186
  passed` with three existing dependency deprecation warnings; web `34 passed`;
  web lint completed with zero errors and five existing React hook warnings;
  package and web production builds passed.

## 2026-08-26 — Bind workflow run-metadata persistence (PR-09)

- Replaced 56 workflow calls that repeated run paths, controller config/state,
  workflow name, and predecessor run ID with one frozen `RunMetadataWriter`;
  mutable plan paths, execution context, current step, snapshots, counters,
  failures, and terminal metadata remain explicit at every write.
- Preserved schema-v2 validation and atomic replacement, resolved-team
  precedence, lifecycle fields, manager/hotplug state, repartition, resume, and
  terminal persistence contracts. The standalone writer function and alias
  surface were removed.
- Base SHA: `aaa8dbc67fcac522023d910c4213679e18cd909d` (merged PR #21).
- Focused verification: runlog `28 passed`; runtime/CLI `405 passed, 171
  subtests passed`; manager/repartition/control-plane/runlog `174 passed`;
  compileall and diff checks passed.
- Full verification: root `1351 passed, 216 subtests passed`; app server `186
  passed`; web `34 passed`; web lint completed with zero errors and five known
  hook warnings; Python/package and web production builds passed.

## 2026-08-23 — Require complete current resume state (PR-08)

- Bumped durable controller metadata to schema version 2. Fresh writers now
  emit the authoritative original plan, explicit team/lifecycle fields, frozen
  configuration identity, complete manager state, hotplug state, and active
  scope envelope references.
- Explicit, `--resume` auto-selected, and daemon resumes admit only exact
  integer schema 2 with complete metadata. Older metadata remains readable for
  analysis but is rejected before plan lookup, startup questions, allocation,
  or continuation; no migration or rewrite is attempted.
- Base SHA after PR 7: `30213fc4ed70b5847e1cf78154f943f5b3a957f9`; PR 7 merge SHA:
  `30213fc4ed70b5847e1cf78154f943f5b3a957f9` (PR-19).

## 2026-08-23 — Restore the supported cross-platform test matrix (F-07)

- The documented root suite now has a truthful Linux/macOS boundary: Linux runs
  the p100 deployment tests, while macOS skips only that 15-test Linux module.
- Repaired ownership guidance, executable Reasonix, Rich transition, and
  AFlow-owned terminal-state test expectations without changing production
  behavior.
- Added reusable Ubuntu/macOS Python 3.11 CI and made package publication wait
  for the same workflow before building or uploading.

## 2026-08-23 — Make local daemon worker status portable and truthful (F-06)

- Local daemon status preserves Linux procfs ownership inspection and uses one
  bounded, strict process-table snapshot on systems without usable procfs.
- Only exact direct `daemon-worker` children for the verified repository are
  counted; malformed or untrusted ownership evidence returns explicit
  ambiguity instead of a false zero, with a daemon birth-identity recheck
  before success output.
- Added real forced-fallback and deterministic parser coverage for spaced paths,
  multiple workers, unrelated/legacy processes, malformed snapshots, and
  ownership races.

## 2026-08-22 — Match resumed legacy controllers by durable lineage (F-05)

- Resumed legacy guards now bind a successor's durable predecessor to one exact
  explicit `--resume` CLI option.
- Free run-ID substrings and post-`--` text no longer establish ownership.
- Wrapper deduplication, descendant selection, classifications, and observer-only
  authority remain unchanged.
- Deterministic tests cover lineage positives, wrappers, descendants, duplicates,
  and collision/spoof cases.

## 2026-08-22 — Bootstrap Git Tracking before run allocation (F-03)

- Fresh pristine review plans now receive one minimal Git Tracking section
  before run identity, launch persistence, start events, or run paths exist.
- The controller preserves the exact pre-insertion backup, atomically writes
  and reloads the plan, verifies its checkpoint snapshot, and records either
  current `HEAD` or the verified empty-repository bootstrap commit.
- Startup rejects prepared-plan snapshot drift before writing, lifecycle branch
  synchronization rewrites only the parsed live section, and daemon starts run
  the same normalization boundary before reserving launch identity. Workers
  recompute an empty-repository deferred base fill after daemon preparation.
- Removed the unreachable base-HEAD confirmation enum and dispatch paths;
  existing pristine-section refresh remains automatic and noninteractive.
- Added deterministic plan, runtime, lifecycle, public-library, and CLI
  coverage with injected runners only; no model or provider call is required.

## 2026-08-22 — Fail-closed headless Reasonix ACP (F-02)

- Owned new and resumed Reasonix sessions now negotiate the exact
  `tool_approval=yolo` select option before model, effort, and prompt.
- Every configuration update must return a complete, unambiguous state that
  still acknowledges all applied values; missing, rejected, or reset approval
  closes and terminalizes the run before prompting without retry.
- Added deterministic new/resume ordering, malformed and rejected state, reset
  detection, process-close, and durable workflow-failure coverage. No live or
  paid model prompt was run.

## 2026-08-22 — Truthful turn and run termination (F-01/F-04)

- Finalize ordinary catchable post-start exceptions into one terminal turn and
  a failed, resumable run with bounded, redacted evidence and no generic retry.
- Require a complete post-turn original-plan snapshot before `END`, merge,
  worktree removal, or successful completion; incomplete max-turn and ordinary
  `END` preserve routing evidence and lifecycle state but fail.
- Added owned-session, observer, hotplug-resume, manager-on/off, CLI, and real
  worktree regressions. Harness negotiation, Git Tracking bootstrap, guard,
  daemon/API/UI, and architecture rewrites remain intentionally excluded.

## 2026-08-23 — Remove deprecated unscoped execution routes

- Removed `POST /api/executions`, `GET /api/executions/{run_id}`, and
  `GET /api/executions/{run_id}/events`, including the `find_run()`
  cross-project compatibility scan and adapter models; no redirect, tombstone,
  or compatibility alias remains.
- Lifecycle REST is now project-scoped under `/api/control-plane`; clients
  carry `project_id`, while REST and MCP continue to share the same durable
  control-plane application and services.

## 2026-08-11 — Add lightweight stdio MCP daemon

- Added `aflow daemon start|status|stop` for a single local project, with stdio
  MCP by default and optional loopback HTTP, separate from production `aflowd`.
- Added exact subprocess-unit ownership, process-group drain/escalation, and an
  atomic mode-0600 pidfile bound to process-birth identity.
- Moved the 13-tool, three-resource MCP registry into the core package while
  retaining the FastAPI adapter's header-auth and safe-error boundary.
- Added config validation, split-config preservation, public FastMCP parity,
  and stdio initialization/EOF cleanup regressions.
- Live restart proof fixed reconciliation precedence so a durable terminal
  controller record and matching launch phase supersede an older daemon
  observation instead of being misreported as needs_attention.

## 2026-08-10 — Document daemon-backed control-plane boundary

- Documented the release-pinned p100 service, strict project allowlist,
  header-only bearer transport, and the separation between daemon workflow
  ownership and HTTP/MCP/UI transports.
- Recorded reconciliation semantics: a collected or failed workflow unit is
  `needs_attention`, never automatic completion or restart; owner stop and
  explicit linked resume remain durable operator actions.
- Corrected remote-app documentation that described the retired in-memory
  execution map and query-token SSE behavior.
- Production-installed the exact release on p100 and exercised authenticated
  REST/MCP parity, idempotent start, safe-boundary CAS steering, startup
  questions, daemon restart, hard workflow death and explicit resume, owner
  stop, token rotation, legacy-run read integrity, and versioned rollback.
- Live failures led to focused fixes for writable daemon state, Tailscale
  address inspection, TOML validation, readiness rollback, Python safe-path
  entrypoints, hard-kill reconciliation, startup-question status,
  cross-surface bearer ownership, and release-pinned rollback.
- Review transitions now enter the rejection ledger only when a new repair
  plan exists, preventing approvals from influencing worker-upgrade routing.


## 2026-08-09 — Durable live worker role hotplug

- Added provider-neutral worker selector hotplug with durable transaction
  stages, exact same-harness resume, and cross-harness read-only operational
  handover.
- Resume copies and verifies handover/projection/Full-context artifacts before
  pruning; ambiguous provider boundaries remain waiting unless a durable
  operation result proves a matching target completion or not-started retry.
- Status/events/analyze expose safe transaction stages, selectors, capability
  path, relative artifact references, and hashes while omitting prompts, notes,
  environment, and raw session identifiers.
- Capability evidence was protocol/fixture based; no paid live Reasonix prompt
  smoke is claimed.

## 2026-08-08 — Forward Reasonix effort independently

- Reasonix profiles now pass their configured effort through the CLI's native
  `--effort` option instead of requiring a nonexistent model-name variant.
- Adapter coverage fixes the DeepSeek V4 Flash/max invocation contract; the
  deployment configuration selects Sol 5.6/medium for `ds4_flash_max` reviews.

## 2026-08-08 — Prefer fastest-safe guard completion

- Guarded recovery now chooses the smallest reversible, scope-reducing option
  when it preserves the plan's usable outcome and does not weaken safety,
  authorization, acceptance, or approved cost boundaries.
- Plan-less exact `aflow run --resume RUN_ID` controllers are recognized only
  when the resume ID matches durable lineage and the controller cwd matches the
  guarded repository; self-tests reject the same argv from another repository.

## 2026-08-07 — Add role-scoped workflow prompts and material review

- Added validated global and team role prompts with team replacement, global
  fallback, retry persistence, and frozen-run identity coverage.
- Ordinary workflow turns now persist resolved role guidance as their system
  prompt; manager and lifecycle invocations retain their existing prompt paths.
- Bundled `material-code-review` as a default skill and enabled its guidance for
  the packaged reviewer role.

## 2026-08-07 — Complete and resume every lifecycle mode

- Branch-only merge teardown now switches a clean primary checkout from the
  expected feature branch to `main` before attempting the engine-owned
  fast-forward, while unrelated checked-out branches still fail closed.
- Explicit and interactive resume now reconstruct no-lifecycle, branch-only,
  and linked-worktree execution contexts from mode-specific durable metadata.
- Regression coverage exercises ordinary branch-only completion plus
  environment-preflight recovery without synthetic worktree paths.

## 2026-08-07 — Preserve guard replacement lineage

- Replacement linkage now retains the original recovery fingerprint, validates
  complete predecessor/successor identity and one live successor controller,
  and migrates unsafe same-successor linkage without rewriting observation
  history.
- Missing or malformed identity fields and absent original plans fail closed
  before guardian state is written.

## 2026-08-07 — Add adapter-neutral harness environment preflight

- Added a bounded, secret-safe executable check for real harness launches and
  an optional adapter capability for local prerequisites.
- Reasonix now uses `reasonix doctor --json` to detect enforced bash sandboxing
  without guessing configuration files; missing `bwrap` is reported with fixed
  remediation and no synthetic turn or manager artifact.
- Workflow, manager, correction, repartition, recovery, bootstrap, and
  agent-required merge boundaries share terminal run-level handling. Injected
  runners remain ready by default; explicit resume re-evaluates the pending
  invocation. AFlow reports prerequisites but does not install packages or
  validate provider health.
- The guardian remains the fallback for legacy and unsupported failures.

## 2026-08-06 — Make clean-END manager eligibility deterministic

- Clean controller-proposed `END` boundaries now omit `stop` from Lite
  eligibility while preserving the existing action set for Full.
- Ineligible or invalid Lite output and explicit Lite escalation restore Full
  eligibility for one decision from the same finalized turn; accepted Full and
  non-clean-boundary `stop` decisions still fail through the existing report,
  event, and banner path.
- Focused manager and runtime regressions cover Lite `stop` to Full `continue`,
  both Full-stop fallback routes, direct Full selection, and ordinary non-END
  Lite `stop` without interpreting manager prose.
## 2026-08-02 — Correct manager plan-selection notes at one boundary

- An otherwise valid manager response whose only authority defect is
  plan-selection wording now receives one same-profile correction sub-attempt
  before persistence; only advisory notes may change.
- Original and corrected traces remain under one decision directory, while
  routing, history, observer events, and turn accounting retain one logical
  manager decision. Mutation, boundary drift, immutable-field changes, launch
  failure, invalid JSON, and a second authority failure stop without retry.
- Verification passed: focused context (`1` test), focused runtime (`6` tests,
  `5` subtests), manager/context (`74` tests), broader manager runtime (`26`
  tests, `7` subtests), and documentation (`20` tests), plus compilation and
  diff hygiene.

## 2026-08-02 — Trust turn diagnostics by stream and outcome

- Text-signal evidence now records semantic stdout separately from
  failure-eligible stderr. A successful turn's stderr transcript cannot turn
  echoed plan, branch, merge, or owner-action phrases into structural
  failures; explicit stops and structured failure evidence remain unchanged.
- Lite manager context intentionally omits plan prose, labels that redaction,
  and gives durable controller-owned plan/workspace facts precedence over
  contradictory text. The additive provenance and disclosure fields preserve
  existing sorted signal-name lists and schema-v1 output.
- A managed-worktree runtime regression confirms the incident-shaped turn gets
  one accepted post-turn decision, launches checkpoint review, and retains the
  implementation change and worktree without a false stop or cleanup.

## 2026-08-02 — Bundle same-task AFlow guard

- Moved `aflow-guard-development-run` into AFlow's canonical bundled-skill
  inventory and made it a default install for supported harnesses.
- Guard heartbeats and actionable reports now stay in the task that requested
  supervision; the bundled helper retains pinned-run and provenance checks.

## 2026-08-02 — Bind pending repartition resume identity and paths

- Non-reset explicit and AUTO resume now require the restored active scope,
  envelope, manager decision boundary, derived generation ID, executable target,
  and canonical artifact keys before startup; internal symlink aliases fail
  closed, while reset scope keeps pending state opaque.

## 2026-08-02 — Fail-closed pending repartition resume validation

- Explicit and AUTO resume now reject present malformed or stage-incomplete
  pending repartition state, validate every carried artifact reference, and
  bind its bytes before startup; reset-scope resume continues to discard this
  checkpoint-scoped state without interpreting its metadata or artifacts.

## 2026-08-02 — Complete resume context before startup

- Explicit and AUTO resume now decode the selected run's complete worktree,
  lifecycle, manager, scope, pending-artifact, and override context before
  startup questions; post-startup checks reuse that same loaded context.

## 2026-08-02 — Plan-optional durable resume bootstrap

- Resume now resolves one explicit or shell-selected durable run before startup
  preparation, reconstructs omitted plan and invocation identity from `run.json`,
  and rejects conflicting repeats or unsafe metadata without creating state.
- `original_plan_path` is authoritative, with `plan_path` retained only as a
  legacy fallback. Fresh runs still require `plan_file`.
- Resume also preserves an explicitly empty `--` instruction suffix as caller
  input and rejects path-shaped run IDs before loading durable metadata.

## 2026-08-02 — Frozen configuration identity on resume

- Schema-versioned resume metadata now requires a complete `frozen_config` and
  rejects workflow, canonical config-path, or configuration-fingerprint drift
  before startup questions or new durable state.
- The execution boundary repeats the comparison after reloading configuration,
  while schema-less legacy metadata without a frozen identity retains its older
  scalar/lifecycle compatibility path.

## 2026-08-02

- Standardized p100 self-hosted development on an editable uv tool installation:
  run `uv tool install -e . --force` from the intended AFlow checkout, then invoke
  `aflow` directly. `uv run aflow` is not a supported development launcher;
  `uv run` remains available for tests and other project-scoped checks.

## 2026-08-01 — Codex prompt stdin transport

- Checkpoint 1 moves Codex effective prompts from argv into stdin while keeping
  prompt artifacts and injected-runner behavior observable.
- Large-prompt and early-stdin-close runtime fixtures pass; launch-failure
  normalization remains planned for Checkpoint 2.

## 2026-08-01 — Harness launch failures use terminal paths

- Checkpoint 2 normalizes process-creation `OSError`s from default and injected
  harness execution into bounded nonzero results: 127 for missing executables
  and 126 for other launch failures.
- Manager artifacts, worker turn artifacts, and lifecycle failure metadata now
  use their existing nonzero-result handling without storing a traceback or
  prompt-bearing launch diagnostic.

### Browser verification: live status and manager display

- Config saves immediately update the shell readiness badge.
- Healthy event streams refresh canonical run and bounded context summaries, coalescing bursts and ignoring stale requests while preserving the last snapshot on failure.
- Manager outcomes read the canonical run-extract schema; completed plans display completed checkpoints.
- Verified real-provider browser start, live controls, owner stop and successor completion; added regressions for the observed display gaps.

- Final browser screenshot check: native dropdowns now inherit text styling and use the dark color scheme, keeping selected values readable.

### 2026-09-06 — Clarify remote interfaces and private entry point

- Documented canonical REST/SSE, optional MCP, deferred remote ACP, and Codex as an optional engine harness. Root usage and architecture now point to the private deployment runbook and MagicDNS HTTPS discovery through Tailscale Serve status.

### 2026-09-06 — Report applied live turn limits

- HTTPS browser verification found that status retained the initial turn limit after a safe override. Control-plane projections now use the engine-recorded effective limit; pending override writes alone do not change the reported value.

## 2026-09-06: Combine guided settings with rolling login

- Integrated approved guided configuration backend e59721c with deployed browser login 6076caf. Retained both session routes and guided-form routes when resolving their shared insertion point.
- Verified the combined server suite (204 passed), engine configuration tests (135 passed, 7 subtests), and Ruff. UI and deployment automation integration remain pending.

### 2026-09-06 — Dashboard changes participate in CI

- CI gained a required Ubuntu dashboard job (Python 3.12, Node 22 with npm cache
  keyed to the web lockfile) so main's success also covers the shipped app server
  and web UI.
- The job runs the server's frozen dev sync and full pytest suite, then npm ci,
  the full vitest suite, and the production web build in their project
  directories; it runs for PRs, main pushes, and reusable workflow calls.
- Existing engine test matrix and package build job are unchanged; no publishing
  trigger or credentials were added.
- Local verification baseline: server `uv run pytest -q` 163 passed (3 known
  deprecation warnings); web tests 87 passed with known React `act(...)` stderr
  notices; `npm run build` and `git diff --check` clean.

- Deployment owner repair: all timed-out poller subprocesses now terminate their process group, including children left after parent exit; focused regressions cover TERM-ignoring descendants and both error/deferral results.

- Browser preview found that pure empty-draft validation could override missing-file readiness. The settings report now preserves saved readiness for unchanged documents and explains the starter/save step before offering Plans.

- Browser acceptance repairs: label ZCode models as externally configured, distinguish unspecified values from missing profiles, and preserve the selected project name on narrow screens.
### 2026-09-06 — Preview exact dashboard runs (checkpoint 5)

- Plans now explain Draft, Ready and Done, and a saved Ready plan opens the exact launch draft.
- The compact launch form offers configured workflow/team choices and previews each materialized step's role, selected profile, team/global source and model/effort details. ZCode settings are identified as external.
- Only Ready plans with a resolved workflow preview and valid turn limit can start. Invalid extra-instruction guidance stays visible when Advanced options is closed.
- Retained startup-answer, idempotency and stop-before-replacement behavior. The owner corrected a test callback option typo after stopping the repair controller; validation and approval are recorded in the original checkpoint ledger.

- Narrow-browser inspection found the selected Runs tab clipped offscreen. Mobile navigation now shows Projects on its own row and all four project views together without horizontal scrolling.

- Plan promotion buttons now use the same Ready/Done names as the surrounding guidance; settings explain the shared save in plain language.

- Run setup and live-control help now explain Ready plans, available choices, and Pending changes in plain language.

- Run lists now show the newest returned run first using canonical launch time (or the timestamp in older run IDs), while refresh preserves an explicit selection. The API itself remains stably paginated in ascending identity order.
- Checkpoint 6 adds project/run links, progress-first run views and Technical details. Owner repair removes URL fragments from canonical links, constructs clipboard links from validated identities, and retains the selected run across oldest-first page refreshes.

- Run progress and editable settings now use plain labels; internal bounded-event and revision terminology remains in technical details.

- Completed the dashboard App journey contract test: discovery, Add/Open, guided role assignment, failed-save recovery, Ready plan preview/launch and exact-link remount. Updated the remote-app guide and architecture for discovery, shared drafts, suggestions, run links and the separate deployed owner acceptance recipe.

- CI registration preservation checks now compare the pre-existing Git status with global excludes disabled; a user’s ignored .aflow directory no longer hides the fixture’s untracked configuration.
- Fixed the continuous deployer’s real preflight handoff: reserve a unique snapshot path, then release the empty directory so preflight creates it. The deployment fixture now rejects existing paths like the production script; the success regression fails before the fix.

- CI process-cleanup assertions now recognize both a missing proc file and a process disappearing during the read as termination, with deterministic coverage of both Linux outcomes.

## 2026-09-08 — Web UI usability follow-up (plan: aflow-web-ui-usability-review-followup-20260908)

- Implemented the full review follow-up in `apps/aflow_app`: settings repair (mistyped prompt tables keep Advanced TOML usable with production diagnostics; confirmed reload truly discards every pending edit including the password, with epoch-guarded late responses), secondary prompt actions (More menu, Delete prompt… with confirm/Undo, "Used by" disclosures), one-click Diagnostics full context (acknowledgement removed, `level=full&full_scope=true` sent directly, stale responses rejected), compact project/run lists (semantic rows, row-as-open, Unregister behind a More menu), `AFlow · <project>` header without the project bar, sticky Settings toolbar with tabs+Save, visible launch defaults (`checkpoint_delivery · Default` style) with request-omission semantics for followed defaults, Team members + full worker upgrade chain previews, an explicit Add team form (draft-only until Save all, Enter support, inline validation), and typed `set_team_upgrade` edits of `[teams.<name>].upgrade_to` with cycles/missing targets surfaced field-level and validated by production graph checks at save.
- Server: guided projection guards malformed prompt shapes (form omitted with unchanged documents instead of HTTP 500), hardened prompt-reference traversal (step arrays and string/array `merge_prompt`), `GuidedTeamSummary.upgrade_to`, and the closed `set_team_upgrade` action; `add_team` actions are ordered before dependent edits in net batches.
- Verification: 212 web tests, 155 app-server tests across the prescribed suites, 145 core tests (+7 subtests), full server+core sweep 1762 passed (+222 subtests), `git diff --check` clean, `ruff` clean, `tsc`/vite build clean, and 33 headless-Chromium checks against the live deployment at `http://p100.tail0fad23.ts.net:8766` (header, compact lists, sticky tabs, prompt menus, Add team draft/discard, launch defaults before focus, one-click Diagnostics, mobile tablist). No real configuration was saved, no password rotated, no project unregistered, no run started during checks.

### 2026-09-08 — Worker failure diagnostics follow-up

Implemented exit-aware read-only status and guarded recovery, bounded redacted detached-child capture, early exception stages, unified dashboard refresh and summary/raw diagnostics. Fixed prompt Undo across tabs/multiple deletions and moved recent count editing exclusively to General. Preserved the existing dirty UI work and historical run artifacts.

Verification exercises installed `aflow ui-worker` → real controlled child → receipts → repository/service → authenticated REST and Chromium, without restarting the service or page. Also exercises installed `aflow daemon-worker` configuration-load failure, high-volume stdout/stderr, redaction/caps, exit zero, signal termination, stale/malformed receipts and controller authority. No real owner workflow was launched or resumed.

Owner follow-up: fixed the Settings toolbar disappearing on long scrolls by preventing its containing flex item from shrinking below its content. Chromium reproduced the original failure and verifies tabs/Save at 25%, 50%, 90% and full scroll on 390px/1365px light/dark layouts. Added `test_settings_browser.py` for the real layout boundary.

Final verification: web tests (213), all server tests including Chromium layout (233), required worker/control-plane tests plus real-process diagnostics (82), and required API/auth/MCP/manager-context tests (89) passed. Web production build, Ruff and diff whitespace checks passed.

## 2026-09-09 — Manager context recovery headroom

- Raise the manager inline user-prompt hard limit from 32 to 40 KiB so the completed-checkpoint continuation can reach review after a 33,576-byte context rejection. Retain the 16 KiB target and prelaunch UTF-8 byte enforcement. Compact summaries with disk-backed history are planned separately.

- Recovery also recognizes a durable manager context-budget prelaunch failure after a completed worker turn. Resume replays its validated manager boundary rather than rejecting the completed plan or skipping pending review; unrelated completed-run and consumed-boundary gates remain intact.
- Preserve original configuration identity during direct CLI resume from a validated predecessor snapshot; retain fingerprint checks and reject unrelated paths.

## 2026-09-09 — Readable machine labels

- Added one shared sentence-style formatter for snake_case identifiers, including
  repeated-underscore collapsing, empty values, custom names and explicit acronym
  casing.
- Applied it at prompt, workflow, role, step, event and generated-settings label
  boundaries while keeping prompt keys, selector values, API payloads, URLs,
  DOM identity and technical details exact. Search accepts both raw identifiers
  and readable labels; colliding readable labels retain raw secondary text.
- Verification: 270 web tests, the production build, and four disposable
  Chromium checks across desktop/mobile settings and run layouts passed. The
  browser check covered keyboard selection of a readable workflow label and
  restored the original draft value; no live configuration was changed.

## 2026-09-10 — CP6 collision disambiguation repair

- Added sibling-aware presentation labels so colliding team, workflow and role
  names remain separately identifiable in settings navigation, native selects,
  prompt override destinations, guided configuration and live-run controls.
  Raw option values, keys, callbacks and save actions remain unchanged; custom
  display names remain intact.
- Added regression coverage for colliding and unique labels, including the
  exact `fast__team` upgrade action and native selector values.
- Verification: 277 web tests, the production build, and four disposable
  Chromium/browser checks passed; `git diff --check` passed and no live
  configuration was changed.

- Follow-up repair disambiguates colliding `code_review`/`code__review` role
  controls in global settings, team settings and live-run controls, while
  retaining raw keys in exact save and control payloads. Verification: 279 web
  tests, production build and four disposable Chromium checks passed; no live
  configuration was changed.

## 2026-09-10 — Shared dirty-worktree preflight

- Added one typed, NUL-aware Git status preflight shared by startup and both
  lifecycle allocation checks. It preserves rename/source paths and ordered
  status items, excludes plan/lifecycle-owned dirt for fresh worktrees, and
  reports conflicts, in-progress operations, and inspection failures explicitly.
- Dirty non-plan paths now use the existing startup confirmation for eligible
  worktree runs. The acknowledgment is carried through prepared daemon and
  controller inputs; final pre-allocation validation rechecks current dirt
  without relaxing branch, identity, conflict, or teardown protections.
- Verification: focused dirty-worktree tests (8), Git-status tests (26), CLI
  tests (154 plus 125 subtests), runtime tests (276 plus 43 subtests), and
  library startup tests (33 plus 6 subtests) passed; Ruff and diff whitespace
  checks passed. No real provider or global configuration was used.

## 2026-09-10 — Live configuration without snapshot gates

- Completed the legacy recovery pass: CLI and daemon resume now use the
  relocated current configuration source, while malformed, absent, edited, or
  hash-disagreeing compatibility snapshots remain diagnostic only. Removed
  stale config-save and frozen-configuration admission contracts while
  retaining lifecycle, plan, ownership, idempotency, and security checks.
- Added synthetic CLI/daemon resume and snapshot-corruption coverage, updated
  user-facing and bundled-engine documentation, and isolated ordinary tests
  from this checkout's explicit publication grant without weakening the
  dedicated publication tests.
- Verification: 2,058 Python tests plus 219 subtests, 291 web tests, Ruff,
  web production build, wheel inspection, and browser smoke passed. No public
  push, real provider, or live configuration change was used.

## 2026-09-10 — Responsive document-scroll checkpoint 1

- Removed root/body/shell/workspace viewport locks and the detail/editor scroll
  ownership rules. Runs, Settings and plan content now grow in document flow;
  wide navigation alone retains a sticky bounded local list exception, while
  compact and short layouts stack natural-height blocks until the next
  list/detail checkpoint.
- Removed `SidebarEditorLayout` selection-time detail resets and PlanPanel
  inline height/overflow ownership. Added targeted minimum-width and wrapping
  rules without changing draft, save, history or request identities.
- Replaced independent-pane browser expectations with document wheel movement,
  positive detail dimensions, reachable final controls, natural long Settings
  and plan-list scrolling, and exact long-plan text persistence.
- Verification: 279 web tests, production build, four disposable Chromium
  browser checks and `git diff --check` passed. No live configuration or
  production run was changed.

## 2026-09-10 — Responsive list/detail checkpoint 2

- Added compact list → detail → Back presentation to the shared
  `SidebarEditorLayout` for Skills, Teams, Workflows, Prompts and Run history.
  The compact helper follows the CSS media query through a cleaned-up
  `matchMedia` listener; wide layouts retain both columns. Domain consumers
  continue to own exact selections, drafts, saves, URL state and requests.
- Compact selection captures document position and the clicked machine identity
  before opening, focuses the detail heading once, and Back restores the row or
  the labelled list surface if refresh removed it. Both surfaces stay mounted,
  with the inactive one removed from keyboard/accessibility traversal. A run URL
  opens detail explicitly; passive refresh does not reset focus or scroll, and
  resize preserves an opened detail.
- Added seven focused layout tests and extended disposable Chromium Settings and
  run journeys for compact deep links, list/detail/Back, focus, position and
  re-entry. Verification for this checkpoint: 174 targeted web tests, a
  production build, four Chromium browser checks and `git diff --check` passed.
  No live configuration, production run, publication or CI deployment was
  changed.

## 2026-09-10 — CP2 run-entry intent repair

- Separated App-owned explicit run URL/browser-navigation intent from the
  synchronized selected-run URL. Ordinary default selection therefore remains
  list-first on compact screens while direct links and browser navigation still
  open the exact run detail; existing selection, URL and request behavior is
  unchanged.
- All runs and successful launch handoffs now mark that existing explicit
  intent before moving to their exact Runs URL, so compact detail opens even
  when the dashboard was not visible or was locally backed out of.
- Added compact App and disposable Chromium regressions for fresh and repeated
  All runs selection, alongside passive default synchronization, explicit
  initial links, later browser navigation, row Back, identity and focus
  restoration.
- Verification: the CP2 subset passed 178 web tests; the production build and
  all four disposable Chromium browser checks passed; and `git diff --check`
  passed. No live configuration, production run, publication, CI, or live
  activation was changed.

## 2026-09-10 — Responsive shell checkpoint 3

- Added registered header slots so the shell owns compact two-row navigation
  while pages retain their existing action handlers and draft/request owners.
  Settings now places section navigation, Save all changes and secondary actions
  in the shared row; Skills installation is an in-flow disclosure opened from
  More instead of a permanent card.
- Compact navigation uses an accessible in-flow Menu with current context,
  existing destinations and Logout, including Escape/focus return. Settings
  uses one active native section selector on compact layouts and keyboard tabs
  on wide layouts without fixed section-count assumptions.
- Verification: the CP3 web tests, production build, disposable Settings
  Chromium suite and `git diff --check` passed. No live configuration,
  production run, publication, CI or live activation was changed.

## 2026-09-10 — CP3 repair evidence

- Kept the list/detail breakpoint unchanged while switching Settings to its
  labelled section selector before the shared header can wrap at 960–1024px.
  More → Install skills now completes the guarded Advanced TOML → Guided
  transition, preserving valid and invalid raw drafts; Hide controls only the
  disclosure while retained success/failure outcomes remain available on reopen.
- Updated compact run-navigation coverage for the shared Menu and header More
  actions. Verification: 100 targeted web tests, production build, both
  disposable Chromium modules (4 passed), and `git diff --check` passed. No
  live configuration, production run, publication, CI or live activation was
  changed.

## 2026-09-10 — CP3 hosted recovery selector correction

- Updated the hosted successor-recovery regression to exercise the shared
  More → Cancel action after the header migration; standalone dashboard Cancel
  coverage remains unchanged and exact request/owner-stop assertions remain
  intact.
- Verification: the focused recovery test, full 295-test web suite, production
  build, two disposable Chromium modules, and `git diff --check` passed. No
  live configuration, publication, CI or live activation changed.

## 2026-09-10 — Responsive editor checkpoint 4

- Added a shared native text editor for settings, skills, prompts, plans and
  connection TOML. It defaults to soft visual wrapping with a labelled
  `Wrap lines` control; disabling wrapping keeps horizontal overflow inside
  the editor, while native vertical scrolling, desktop resize and exact draft
  bytes remain owned by the existing callers.
- Reflowed profile rows into readable name/model/effort columns with compact
  stacked fields, and kept combobox suggestions bounded and lower-edge safe.
  Existing draft, conflict, partial-save, acknowledgement and Undo paths stay
  in ordinary document flow.
- Verification: the exact CP4 web suite passed 120 tests, the full web suite
  passed 297 tests, the production build passed, the Settings Chromium module
  passed 3 tests including 20,000-character Markdown/TOML and partial-save
  journeys, and `git diff --check` passed. No publication, CI, or live
  activation was performed.

## 2026-09-10 — CP4 native editor overlay repair

- Moved the shared `Wrap lines` control into normal document flow and removed
  the overlay-only textarea padding. Skills places the shared toolbar beside
  its existing title/`SKILL.md` label, leaving the native textarea as the sole
  editor scroll and hit-test surface while preserving the 44px control and
  16px editor text requirements.
- Extended the disposable Skills Chromium journey to verify 1280×720 and
  390×844 geometry, actual client editing height, toolbar/editor non-overlap,
  post-scroll hit-testing, caret movement, exact wrap-toggle bytes, saved
  content, and retained partial-save acknowledgements. Inspected the captured
  dark-theme scrolled-editor screenshot.
- Verification: full web suite (297 tests), production build, both required
  Chromium modules (4 tests), and `git diff --check` passed. Original CP4
  approval remains with the reviewer; no publication, CI, or live activation
  was performed.

## 2026-09-10 — Responsive browser acceptance checkpoint 6

- Added the disposable responsive-browser module covering 320×568, 390×844,
  768×1024, 844×390, 1280×720, 1440×900 and 390×420. It exercises the shared
  header budget, document scrolling, list/detail/Back and focus restoration,
  long plans/runs/configuration, 200% text reflow, keyboard menu behavior,
  resize-retained drafts, validation and failed-save retention, touch-sized
  primary controls, safe-area-aware sticky controls, and light/dark screenshots.
- The module rejects unsupported browser names and runs unchanged with
  `AFLOW_TEST_BROWSER=webkit`. The Ubuntu/Python 3.12 dashboard job installs
  WebKit, runs this module, and uploads its screenshot artifacts. Physical
  mobile keyboard/browser-toolbar behavior remains unverified.
- Repair revalidation persists the existing `aflow.appearance` preference
  before every screenshot navigation and asserts the loaded theme. Its test
  zoom snapshots computed visible text/control sizes before applying doubled
  inline sizes, proving the Skills editor text—not only the root—reflows at
  200%. Chromium and WebKit screenshots were regenerated and inspected.
- Repair verification: full web tests (297), production build, combined
  Chromium browser checks (13), responsive Chromium (8), responsive WebKit
  (8), and `git diff --check` passed. Original CP6 remains unapproved; no
  publication, remote CI receipt, or live activation was performed.

## 2026-09-10 — Responsive UI integration checkpoint 1

- HISTORY: Integrated approved responsive source `61d5988` with accepted main
  `5259740` using a no-commit merge. `MERGE_HEAD` remains the source commit for
  reviewer-owned approval; the source worktree and the separate issue38 work
  remain untouched.
- HISTORY: Preserved both implementation histories and retained current main's
  live configuration, dirty-worktree preflight, restart/successor controls,
  API semantics, merge context and delivery gates alongside the responsive
  header, document scrolling, list/detail navigation and native editors.
- HISTORY: The first combined backend/browser attempt exposed the fixture's
  unacknowledged dirty-worktree preflight. The acceptance test now waits for a
  loaded inspection result, checks the real confirmation control, and then
  asserts that Start is enabled; no sleep, timeout inflation or retry-only
  acceptance was added.
- Combined verification passed: web suite (309 tests), production build, full
  backend suite (2043 passed, 223 subtests), Chromium combined journeys (13),
  WebKit responsive journeys (8), Ruff, and `git diff --check`.
- Fresh Chromium and WebKit screenshots for Skills, run detail, New Run and
  Plans were inspected in light/dark and landscape states; New Run evidence
  shows the loaded preflight result rather than a loading state. Physical mobile
  keyboard/browser-toolbar behavior remains unverified. Publication, exact CI
  and live activation remain downstream delivery checks.

## HISTORY: 2026-09-10 — Combined responsive integration repair

- HISTORY: Added a rendered-plan-row readiness wait and hosted-shell browser coverage for live max-turn, team and role-selector controls, rejected drafts, list/detail navigation, resize retention, owner stop, dirty preflight and exact unresolved-successor retry.
- HISTORY: Responsive Chromium and WebKit each passed 11 tests; the combined Settings, run-navigation and responsive Chromium command passed 16 tests. Fresh light/dark live-control and restart-state captures, plus existing Skills, profile, Plans and New Run captures, were inspected. Physical keyboard/browser-toolbar behavior remains unverified.
- HISTORY: Web build and full backend verification passed (`2046` tests, `223` subtests); Ruff passed. The first parallel Vitest suite invocation had one existing RunDashboard worker-order failure at 308/309, while the exact rerun, focused file, and serial diagnostic all passed 309/309. No skip, timeout inflation, retry-only dismissal, production change or external mutation was used.
- HISTORY: This repair remains uncommitted; the earlier pending-merge state is superseded by the authorized recovery snapshot and the current verification record below. No publication, CI receipt or live activation is claimed.

## HISTORY: 2026-09-10 — Recovery snapshot acceptance follow-up

HISTORY: The authorized snapshot `5b52d3d` preserves accepted base `5259740` and responsive source `61d5988`; `.git/MERGE_HEAD` is intentionally absent and no history rewrite, checkpoint commit or second merge was performed.
HISTORY: The focused acceptance repair now waits for the hosted capabilities to expose `No team`, both colliding labels and raw `fast__team` before selecting it, and asserts successor confirmation through its rendered container and enabled action.
HISTORY: The first post-edit full backend invocation recorded a real readiness gap: native `<option>` locators were awaited as visible and all three new live-control viewports timed out, with `2043 passed`, `3 failed`, `223 subtests`. The waits were corrected to exact attachment state without delay, timeout, skip or assertion weakening.
HISTORY: The corrected combined Chromium browser command passed 16 tests, responsive WebKit passed 11 tests, the full backend command passed `2046`, `223 subtests`, the web suite passed `309`, the production build passed and Ruff passed. Physical keyboard and browser-toolbar behavior remain unverified.
HISTORY: Published clipboard history `c14f1f2`/`cd78d53` remains separate and must be preserved by coordinator-owned delivery; no publication, CI receipt or live activation is claimed.

## 2026-09-10 — Dashboard recovery transition readiness

- The CI failure was a cross-surface ordering mistake: after `Resolve pending successor`, the preserved retry control can render before the destination's effect-registered `Start run` header. The recovery test now waits for that destination button, then asserts its disabled state without changing exact request, body, key, retry, owner-stop, or existing-run assertions.
- `openNewRun` now performs one navigation action and awaits the New-run form. Removed 18 conditional second-navigation fallbacks in RunDashboard tests; App New-run handoffs already wait for their destination heading and selected-plan field, so no App or production changes were needed.
- Verification: focused App/RunDashboard tests passed (136), the normal parallel web suite passed (324), production build passed, and `git diff --check` passed. Browser/server/CI jobs were not rerun because the change is test-only; no publication or live activation occurred.

## 2026-09-10 — Preserve live mobile Skills editor

- Retained the single Skills presentation instance across Settings sections
  under native hidden semantics, preserving its open/list state and wrap choice
  without moving draft or save ownership out of `GlobalSettings`.
- Shared compact Back with the Skills title/status heading. The full Back name
  and 44px touch target remain, while Chromium and WebKit both keep a dirty
  390×844 editor at or above the required geometry with no horizontal overflow.
- Added disposable dirty `aflow-plan` Changelog round-trip coverage, including
  hidden-focus/scroll rejection, Back/list retention, light/dark screenshots,
  all required viewports, enlarged text, and read-only Changelog paging.
- Verification: web suite (325), production build, Chromium browser journeys
  (16), WebKit browser journeys (16), and `git diff --check` passed. Physical
  mobile keyboard behavior, live CI, activation, and the coordinator's live
  unsaved-draft smoke remain downstream checks.

## 2026-09-10 — Registered worktree project presentation (Checkpoint 2)

- Grouped only read-projected registered worktrees beneath their registered
  primary project in a collapsed, keyboard-accessible Worktrees disclosure.
  Selected and search-matching children reveal automatically; ungroupable
  registrations remain independent, and child selection/actions retain exact
  project IDs and paths.
- Added parent/worktree context to the app header and global run rows without
  changing URL identity or flattening global run enumeration. Documented the
  normal internal-checkout launch path and legacy registration recovery.
- Verification is recorded with the checkpoint handoff after web unit tests,
  production build, Chromium/WebKit responsive journeys, and `git diff --check`.

## 2026-09-10 — Dashboard control-refresh readiness

- The named `RunDashboard.test.tsx` saved-options regression now controls the
  initial run list, capabilities, and selected-run detail independently. Its
  observed sequence is exact: with list/capabilities pending the max-turns
  control is absent; after the list settles but capabilities remain pending it
  is still absent; after capabilities settle while detail remains pending the
  selected `run-owned` control is enabled at baseline `8`; after detail settles
  it remains `8`; the real change handler then sets draft `17`.
- On visibility restoration the test waits for rendered `saved-team` and
  `harness/saved` options before confirming exact draft `17` and `run-owned`
  remain. The controlled ordering did not reproduce the historical `17` to `8`
  failure, so it establishes safe preconditions and post-readiness preservation,
  not a causal explanation of that historical incident. Production control
  ownership is unchanged.
- Verification passed for focused RunDashboard (85 tests), the normal parallel
  web suite (331 tests), the production build, and `git diff --check`.
  Backend/browser evidence is reused; CI, publication, activation, and
  physical-device behavior remain downstream.

## 2026-09-10 — Canonical CLI isolation fixture paths

- Canonicalized the existing `test_cli_workflow_override` temporary home before
  deriving its plan path. The exact run ownership, run-count, workflow and
  durable plan-path assertions remain unchanged; this aligns the fixture with
  macOS's `/private/var` resolution of its `/var` temporary-directory alias.
- The independent disposable proof ran from an outer Git directory with
  `TMPDIR` set to a symlink, using the checkout's `.venv/bin/python` and the
  absolute `tests/test_cli.py` path. `tempfile` used the symlink alias, the
  focused test passed, and the outer caller remained free of `.aflow`.
- Verification: focused CLI test passed (`1 passed, 156 deselected`); full CLI
  module passed (`157 passed, 129 subtests`); no production changes were made.

## 2026-09-12 — Readable browser acceptance contracts

- Migrated browser acceptance selectors to readable run titles, exact full-ID
  accessibility controls and explicit event-disclosure interaction. Exact URLs,
  project/worktree context, status, recovery lineage and retry evidence remain
  asserted without restoring private paths or abbreviated identities to row text.

## 2026-09-12 — Projects refresh readiness test repair

- A controlled return from blocked Runs to Projects held the remounted project
  discovery request and reproduced a connected, disabled Refresh action while
  the registry remained at one read. The acceptance journey now scopes Refresh
  to `More project actions`, waits for the current connected action to become
  enabled, re-queries it, and clicks once.
- Focused App and ProjectPicker verification confirms that the single ready
  click performs exactly one additional registry read and the normal discovery
  refresh, without changing production loading guards.

## 2026-09-12 — Stable live controls during history completion

- A controlled held-history trace measured the Adjust run summary moving from
  y=593.078125 to y=540.078125 when the transient 53px incomplete-history notice
  disappeared above the selected controls. The pointer left at the original
  center then hit a different disabled button and the native disclosure stayed
  closed.
- The single truthful pending/error history status now follows the run
  list/detail layout in document flow, so completing history removes content
  below selected controls. Owned live runs awaiting initial admission report
  pending controls instead of falsely claiming legacy ownership.
- Verification passed: focused RunDashboard (121 tests), production web build,
  Chromium held-history/early-detail/live-control journeys (5 tests), and WebKit
  phone held-history/live-control journeys (2 tests). These cover early exact
  detail, pointer identity, one native click, compact navigation, draft retention
  and existing restart semantics.

## 2026-09-12 — Owner controls before canonical checkpoint evidence

- Split selected-run identity from canonical checkpoint evidence and placed the
  existing Adjust run, stop, resume and restart actions between them. Late
  canonical detail and selection reconciliation now grow below those controls
  without changing their admission, labels, handlers or draft ownership.
- The held-history browser proof now waits for initial compact focus, real
  checkpoint layout, and the fixture's exact `Unassigned history` current/detail
  reconciliation before enforcing the original 1px pointer, scroll and focus
  checks and issuing one native click. Focused component tests also retain full
  canonical evidence and prove the owner-action DOM order.

## 2026-09-13 — New Run controls before preflight

- Controlled responsive evidence measured the preflight panel growing from
  81px to 228.375px and moving the closed Advanced disclosure from y=950 to
  y=1097.375 under a parked pointer; the original node was then hit by a
  paragraph and stayed closed. Existing Advanced controls now render before
  preview and preflight, with state ownership, ARIA wiring and admission
  unchanged.
- Added deferred component coverage and real Chromium/WebKit tablet/phone
  pointer journeys. RunDashboard (123 tests), the web build, and the two
  engine-specific three-case browser checks passed; the existing tablet route
  journey remains included.

## 2026-09-13 — Compact configuration request ownership

- The compact pending-configuration journey now finishes setup in a separate
  page, closes that page, and installs its hold on a fresh page in the same
  authenticated context. The held POST therefore belongs to the document whose
  selection, Back and focus behavior the test exercises.
- A trace directly observed the setup document's held request being cancelled
  by the later navigation. The CI diagnosis that its first-POST hold captured
  that older request remains an inference from the request ordering and timeout.
- Release acceptance is bound to the exact stored request and requires its 200
  response to finish. The preserved row focus is followed by an enabled current
  Refresh menu assertion without triggering another configuration request.

## 2026-09-13 — Settings header breakpoint browser fixtures

- Settings browser assertions now use the shipped width-only navigation rule:
  the labelled selector below 1200px and exact selected tabs at 1200px and
  above. Boundary coverage at 1199×720, 1200×720 and 1200×500 preserves the
  dirty profile draft while the short-height case independently confirms the
  static action row. Existing list/detail, sticky, geometry, scroll, save,
  Changelog and Advanced TOML checks remain in their original domains.
- The production build passed. Chromium passed both named journeys (`2 passed`).
  The initial WebKit pair passed Changelog but exposed that the newly added
  boundary points temporarily inherited the original viewport geometry check;
  after limiting that check to its unchanged 960/1024/1280/1440 cases, the
  affected toolbar journey passed. The retained final evidence contains 18
  screenshots per engine (14 Changelog and four toolbar); representative light,
  dark, compact and desktop captures were inspected with the expected controls,
  selected section, dirty state and readable content.

## 2026-09-13 — Resume ordinary safe owner stops

- Separated ordinary owner-stopped resume admission from the artifact-backed
  pending-review exception at both CLI validation passes. Ordinary admission
  requires matching `owner_stopped` status/reason, no awaiting-review scope, and
  the existing strict current metadata decoder; saved continue boundaries remain
  subject to canonical target matching and are not added to automatic scanning.
- Added fixture-owned three-checkpoint coverage for exact pending-boundary
  reconstruction and consumption, malformed/completed/lifecycle rejection, and
  real daemon-backed REST/MCP ordinary and durable resume through a registered
  managed worktree. Exact-key replay allocates and launches one successor while
  predecessor run metadata and plan bytes remain unchanged.
- Verification: CLI `11 passed, 150 deselected, 26 subtests passed`; daemon `5
  passed, 34 deselected`; REST `7 passed, 54 deselected`; MCP `6 passed, 29
  deselected`. Production Ruff and `git diff --check` passed. The server tests
  emitted only the existing Starlette/httpx deprecation warning.

## 2026-09-23 — Revocable launch confirmation (Checkpoint 1)

- Defect: a final Start confirmation could continue after Cancel while its
  committed-configuration read was held, because the async continuation could
  retain authority and submit one start request. This was reproduced with
  mocked/deferred component APIs; no live run was started.
- Remedy: RunDashboard now owns a generation-scoped confirmation token and
  synchronous in-flight lock. Review, page, project, visibility, draft and
  unmount changes revoke it; revalidation checks it after each read and before
  submission. NewRunPage exposes a separate read-only revalidation state so
  Cancel remains available. Existing preflight, startup-question,
  uncertain-result and idempotency-key paths remain intact.
- Verification: the focused web command passed 195 tests; the full web suite
  passed 579 tests across 28 files; and the production build passed. The
  disposable launch-confirmation journey passed in Chromium and WebKit. The
  combined browser gate passed 5 tests with 10 deselected in each engine; the
  cancellation journey observed zero start POST requests and zero disposable
  unit-manager starts after Cancel/release.
- No deployment, publication, live activation, physical-device or full visual
  redesign acceptance is claimed. The concurrent diagnostic helper file was
  not changed.

## 2026-09-23 — Preserve skill draft revisions during reload (Checkpoint 1)

- Replaced text-only Settings skill drafts with `{ content, expectedRevision }`
  pairs. The first meaningful edit captures the loaded revision; accepted
  background reads remove only no-op artifacts and cannot silently rebase a
  genuine draft. Save freezes each pair for both validation and PUT, while
  conflicts retain the draft and its original revision.
- Added deferred component coverage for the R1/R2 pending-read conflict,
  equal-refresh editing, refreshed no-op editing, cached non-selected refresh,
  superseded reads, and existing acknowledgement/error behavior. Added a
  disposable Chromium/WebKit journey that holds the skill detail read, edits
  the visible editor, intercepts validation and PUT, and verifies the R1
  revision plus retained conflict draft. No real skill files are written.
- Preserved prerequisite `6f7a72cc85d067653743f639eb8e8f495c3e88b6` and
  published `af8ae222c906449b1243e93d11d8e869e8ec60fa` in the branch history.
- Verification from the repository root/server:
  - `npm --prefix apps/aflow_app/web test -- --run src/components/GlobalSettings.test.tsx` — 88 passed.
  - `npm --prefix apps/aflow_app/web test -- --run` — 601 passed across 28 files.
  - `npm --prefix apps/aflow_app/web run build` — passed; Vite emitted only the existing chunk-size warning.
  - `uv run pytest -q tests/test_settings_reload_browser.py` — 2 passed in Chromium.
  - `AFLOW_TEST_BROWSER=webkit uv run pytest -q tests/test_settings_reload_browser.py` — 2 passed in WebKit.
  - `git diff --check` — passed.

## 2026-09-24 — Compact run-row progress facts (Checkpoint 1)

- Reproduced the density regression on the built `bb510409` candidate: the
  three populated 1280×720 fixture rows were 234px wide and 79.375px tall as
  the long approval sentence wrapped. Collapsed rows now use concise exact
  ratios, preserve meaningful zero, qualify partial lower bounds, and omit
  unavailable facts. Full approval wording, checkpoint titles, run identity,
  and state remain available in previews and accessible names. Desktop row
  spacing keeps the status and progress together without reducing touch size;
  the shared `GlobalRunOverview` expectations now assert the compact text.
- Inspected Chromium and WebKit captures in light and dark at 1280×720,
  1440×900, and 390×844. Populated desktop rows measured 63.375px and mobile
  rows 64.375px. Preview bounds, sibling stability, explicit touch access, and
  no-horizontal-overflow checks passed.
- Verification: full web suite — 629 tests across 29 files; production build
  passed; the authenticated built-app fidelity test passed in Chromium and
  WebKit; `git diff --check` passed. The build retains its existing chunk-size
  warning. Full server suite, CI, publication, and live activation remain
  coordinator-owned; this checkpoint is left uncommitted for review.

## 2026-09-24 — Synchronize dashboard CI browser state (Checkpoint 1)

- Inspected CI run `36003918987` on release SHA
  `eafc3c5861217283507edd63679de4786cb2323e`: Ubuntu 3.12 saw the Save button
  after Advanced → Guided and a loading row where stale was expected; macOS
  3.13 failed the selected-tab assertion. The local built-app baseline focused
  pair passed once, so the CI timing failures did not reproduce in that run.
- Settings browser assertions now wait for the responsive selected tab or
  section selector, visible Changelog heading/content, and Save-button absence
  before confirming read-only state. Existing draft, save, responsive, theme,
  and overflow assertions remain; the journey also checks for page errors.
- The run-row journey waits for settled initial enrichment and available
  progress before Refresh, records the list revision increase, verifies the
  exact project/run detail request is held, then waits for released response
  bodies and a render turn before confirming the stale notice remains. Existing
  no-write, density, preview, partial/zero facts, viewport, overflow and page
  error checks remain intact.
- Inspected built-app screenshots at 1280×720 and 320×568 in WebKit. The
  desktop Changelog shows its selected tab with no Save action; row evidence
  shows the stale notice on desktop and readable Running status on phone with
  no horizontal overflow. The phone Changelog capture was taken after the
  document-scroll assertion; its selected compact selector was verified by
  the browser assertion.
- Verification: `uv run --directory apps/aflow_app/server pytest -q
  tests/test_settings_browser.py::test_changelog_settings_responsive_journey
  tests/test_ui_followup_fidelity_browser.py::test_ui_followup_run_rows` passed
  three consecutive Chromium runs and one `AFLOW_TEST_BROWSER=webkit` run
  (`2 passed` each). `npm --prefix apps/aflow_app/web test -- --run` passed
  629 tests across 29 files; `npm --prefix apps/aflow_app/web run build` passed
  with the existing chunk-size warning. `uv run --directory
  apps/aflow_app/server pytest -q` passed once (`480 passed`) before the final
  response-body wait cleanup; the final-state run had `479 passed` and one
  unrelated CP8 Review modal timeout, whose isolated rerun passed.
- `AFLOW_TEST_BROWSER=webkit uv run --directory apps/aflow_app/server pytest -q
  tests/test_settings_browser.py tests/test_ui_followup_fidelity_browser.py
  tests/test_run_progress_browser.py` on final-state tests had `23 passed` and one
  repeatable unrelated failure: the existing dirty mobile Skills editor test
  measured the textarea at y=`282.5625px`, exceeding its approved 280px limit.
  This was reproduced four times. An earlier broad run also exposed row-page
  access errors while using `Response.finished()`; after switching to response
  body completion, the row test passed alone and in the final broader run.
- The initial attempt left Checkpoint 1 open because the broader WebKit gate
  exposed the Skills layout defect, outside the test-only scope. No checkpoint
  commit, publication, or deployment was made; the scoped implementation stayed
  uncommitted for review.

## 2026-09-24 — Restore mobile Skills editor viewport (Checkpoint 1)

- Reproduced the WebKit 390×844 overflow: heading y=178/h=104.56; Back
  x=16/y=178/w=84.17/h=48; title y=178/h=60.56 with a 36px status; control
  y=238.56/h=44; editor y=282.56/h=421.98. The 265.8px title column forced
  the status to wrap. Chromium kept the status at 18px and placed the editor
  at y=264.56/h=422. At 320px both engines wrapped naturally: control
  y=238.56/h=44 and editor y=282.56/h≈422, without horizontal overflow.
- Reduced the mobile Back button's horizontal padding from 16px to 8px. At
  390px WebKit now measures heading y=178/h=86.56; Back x=16/y=178/w=68.17/
  h=48; title y=178/h=42.56 and status y=202.56/h=18; control y=220.56/h=44;
  editor y=264.56/h=421.98. Chromium has the same heading, title, status,
  control, and editor positions, with a 67.02px Back width and 422px editor
  height. At 320px the status still wraps naturally, the Wrap and Back targets
  remain 44px or taller, and the editor remains about 422px high.
- Inspected light and dark 390×844 captures in Chromium and WebKit, plus desktop
  1280×720 captures. The desktop editor starts at y=174.08 with 360px of
  height. At 390×420 the document scrolled 172px to its end and restored to the
  top; coordinate hit testing on Back returned to the Skills list at compact
  widths in both engines.
- Verification: focused Skills journey passed in Chromium and WebKit; the full
  Settings and responsive browser modules passed in both engines (48 passed
  each); the web suite passed (629 tests across 29 files); and the production
  build passed with its existing chunk-size warning. Physical mobile keyboard
  and browser-toolbar behavior remains unverified. The changes are uncommitted
  for checkpoint review.

## 2026-09-24 — Resume dashboard CI browser gate (Checkpoint 1)

- Verified the independently Sol/Astra-approved mobile commit
  `6d0c273c9f0275e8eb9d9772f8fe014b41c60abb` has parent
  `eafc3c5861217283507edd63679de4786cb2323e` and changes only this log plus the
  mobile Back padding in `styles.css`. Fast-forwarded the managed branch to the
  approved commit (no new commit), restored the two browser-test files
  byte-for-byte from the predecessor backup, and retained both DEVLOG entries.
- Combined verification on the worktree passed: web suite 629/29 files; build
  passed with its existing chunk-size warning; focused Changelog/run-row pair
  passed three consecutive Chromium runs and one WebKit run (2 passed each);
  full server suite 480 passed; broader WebKit Settings/row/progress suite
  24 passed; `git diff --check` passed. The existing deprecation warnings remain.
- Inspected fresh WebKit captures: desktop Changelog shows its selected tab and
  no Save action; the 320px page shows Changelog content without Save. The
  compact selector's Playwright visibility/value assertions pass, though that
  saved after-scroll phone capture does not visibly show the selector; this is
  surfaced for Sol/Astra review. Desktop row evidence shows the stale notice;
  phone row evidence keeps Running status readable. Browser checks report no
  page errors, writes, or horizontal overflow. Skills light/dark 390×844 captures
  show the editor starting near y=265; geometry and Back/Wrap target assertions
  pass. Physical mobile keyboard and browser-toolbar behavior remains
  unverified.
- Checkpoint 1 is ready for normal Sol/Astra review, with its changes uncommitted
  on top of approved HEAD `6d0c273c`. No publish or primary-checkout change was
  made.

## 2026-09-24 — Make manual run Refresh authoritative (Checkpoint 1)

- Read exact-SHA CI run `36024455704`: the only failing test was
  `test_run_progress_transport_and_browser_parity[mobile-390]` in the macOS
  Python 3.12 dashboard job. After the disposable fixture closes its repair
  scope, direct API reads return CP5; the displayed CP4 came from the client
  returning the already-running `pageRefreshRef` promise to a manual click.
- Kept passive refreshes coalesced. When an explicit Refresh arrives during a
  page refresh, it now waits for that pass and starts one fresh page pass. The
  request uses the existing refresh epoch and selected-run guards; a failed
  older pass does not prevent the manual pass from running.
- Added a deferred component regression for CP4 → CP5. It verifies a stale
  passive snapshot cannot satisfy the click, a later direct read displays CP5,
  and a failed passive read still permits the explicit refresh to succeed. The
  selected row, mounted detail, open disclosure and passive-read focus remain
  stable; request counts stop after the fresh pass.
- Strengthened the issue35 browser journey to check the More menu restores focus
  and preserves document scroll while CP5 replaces CP4. Inspected Chromium and
  WebKit desktop and 390px captures: Current work shows CP5 and detail stays
  mounted without a blank state.
- Verification: `RunDashboard.test.tsx` 149 passed; full web suite 630 tests
  across 29 files; production build passed with its existing chunk-size warning;
  Chromium parity 2 passed; WebKit parity plus responsive suite 44 passed;
  full server suite 480 passed; final Chromium and WebKit parity runs with the
  added scroll/focus assertions passed 2 each. Existing Starlette/httpx and
  WebSockets deprecation warnings remain.
- At worker handoff, the checkpoint remained uncommitted for Sol/Astra review.
  Hosted CI for the resulting SHA and deployment activation remain with the
  coordinator.

## 2026-09-24 — Failed review history and compact run evidence (Checkpoint 2)

- A failed reviewer run now leads with the last recorded checkpoint and turn. A short recorded cause appears when available; a bare exit code or manager Markdown stays in the raw disclosure, with a direct Diagnostics link when the cause is unresolved. The short activity list suppresses a run lifecycle duplicate of the same finished turn.
- Unreached delivery placeholders stay out of the collapsed page and delivery disclosure. Recorded delivery failures still retain their warning and receipt; a recorded stage with an unknown status uses `-`.
- Keyboard-open row previews keep the selection control as their Escape target even if the pointer enters afterward. A regression exercises that focus/pointer order.
- Added a disposable owned-run failed review beside active, paused and completed history, with real API reads and Chromium/WebKit light/dark viewport captures at 320×568, 390×844, 768×1024, 844×390, 1280×720 and 1440×900. The existing row journey checks 56–72px desktop rows where possible, viewport-contained previews, focus restoration, stable refresh and read-only navigation.
- Visual review against the frozen demo shows the same overview reading order. The real authenticated shell, failure actions, follow-up draft, and fuller evidence add height, especially at 320px; the frozen demo is a simpler representative surface. The owner's defect screenshots were described in the plan but were not present as files in this worktree, so comparison to them is limited to those recorded symptoms. Browser emulation does not verify a physical mobile keyboard or browser toolbar. Live deployment remains outside this checkpoint worker's scope.
- Verification on the final code: web 644 tests across 29 files; production build passed with its existing large-chunk warning; populated Chromium and WebKit browser files passed 29 tests each at the requested light/dark viewport matrix; full server suite passed 504 tests with three dependency deprecation warnings. Older browser assertions were updated to expect `-` in recorded delivery slots with unknown status. Disposable captures showed no horizontal overflow, clipped preview, unwanted write or page error; WebKit wraps the compact 320px heading and recent-activity labels more than Chromium while preserving readable text.

## 2026-09-24 — Settle residual launch and follow-up CI races (Checkpoint 1)

- Fast-forwarded the clean managed worktree to reviewed base `ca89865e` and
  confirmed its ancestry. CI run `36066404378` showed launch assertions seeing
  `ready` before a later `Inspecting…` render; the recorded `36058346101`
  follow-up test also asserted its deferred detail request before that call was
  observed. The launch guard remains tied to the current preflight identity;
  no persistent product state failure was reproduced locally.
- The launch browser journey now waits for the matching plan/workflow preflight
  response and the complete visible ready state, correct dirty count, and
  Review button state together. It checks the effective no-team and 15-turn
  defaults in the focused read-only review. Added clean and dirty 320×568
  cases. The follow-up component test waits for the exact project/run/signal
  detail call before resolving its deferred response; its one-draft and exact
  returned-path assertions remain.
- Web suite: 633 passed across 29 files. Production build passed with the
  existing chunk-size warning. Chromium and WebKit follow-up browser modules
  each passed 19 tests; Chromium's 14-case launch matrix passed separately.
  Inspected light clean and dark dirty 320×568 review captures in both engines:
  focus, effective choices, and read-only consequence were visible; no new
  mismatch with the approved demo was identified.
- Full server suite: Python 3.12 passed 495; Python 3.13 passed 495 on rerun.
  The first 3.13 full pass had one intermittent focus-restoration failure in
  the existing run-row browser journey (494 passed, 1 failed); the complete
  follow-up browser module and subsequent full suite both passed without a
  product or test edit for that row case. All runs used separate temporary
  configuration and pytest/browser artifact directories. Hosted CI and live
  deployment remain coordinator gates. After the effective-choice assertions,
  the full WebKit follow-up module passed again (19), and focused Python 3.12
  clean desktop/dirty 320×568 launch cases passed (2). `git diff --check`
  passed.

## 2026-09-25 — Carry reviewed resume evidence fix onto current main (Checkpoint 1)

- Fast-forwarded the clean managed worktree from `9eb433e4` to published
  `origin/main` `e3156de0`. The manager-context source and test files had no
  newer-main overlap; their applied patch ID matches the reviewed `f47630a0`
  two-file patch exactly.
- Manager context now binds immutable schema-v2 plan and checkpoint references
  to validated digest artifacts in the current run after resume. Live resumed
  boundaries reject invalid copied evidence before a manager decision. The
  focused tests cover one and two hops, missing/corrupt evidence, wrong kind,
  hash, size, symlink/escape, and scope mismatch; existing schema-v1 tests pass.
- Verification: focused manager/resume suite 130 passed; isolated full core
  suite 2,205 passed and 239 subtests passed under Python 3.13.14;
  `git diff --check` passed. The scoped source/test diff is 2 files,
  190 insertions and 8 deletions. No original core run or worktree was resumed
  or edited. Changes remain uncommitted for checkpoint review.

## 2026-09-25 — Align the run disclosure control on narrow phones (Checkpoint 1)

- Fast-forwarded the clean managed worktree to published `d2b07244`. CI run
  `36119713331`, Ubuntu Python 3.12 job `108022074541`, measured a 21px
  label/button center mismatch in dark 390×844: label y=840.14, h=18;
  button y=848.14, h=44. The existing flex row allowed wrapping. Local
  pre-change Chromium and WebKit runs had loaded fonts and 0px center
  difference at 375 and 390, so the CI wrap is environment dependent; the
  test already waited for font readiness and two render frames.
- Replaced only that flex row with `minmax(0, 1fr) auto` grid columns. The
  label can wrap in its cell; the one intrinsic-width button remains mounted
  and centered. The browser test now includes 375px, records font/layout boxes,
  checks label clipping, and exercises 24px root text at 390×420.
- After the fix, loaded-font control captures in light/dark Chromium and
  WebKit at 320/375/390 show 0px center difference and a 44px phone button.
  At 320, row width is 238px; label/button widths are 130.61/99.39px in
  Chromium and 122.83/107.17px in WebKit. At 390, row width is 308px;
  label/button widths are 200.61/99.39px and 192.83/107.17px respectively.
  The row height is 48px including its top padding. Inspected light/dark
  screenshots at 320 and 390 plus enlarged 390×420 screenshots in both
  engines: the control remains readable with no side scroll; enlarged text
  wraps the label within its cell. The broader enlarged page has tighter
  wrapping in recent activity, outside this control's scope. Physical mobile
  keyboard and hosted Ubuntu font behavior remain unverified locally.
- Verification: web suite 649 passed across 29 files; production build passed
  with its existing chunk-size warning; focused Chromium and WebKit browser
  matrices passed 14 each; combined follow-up and demo fidelity browser suite
  passed 49; `git diff --check` passed. Existing dependency deprecation
  warnings remain. Exact-SHA CI, publication, and live activation are
  coordinator gates; checkpoint changes remain uncommitted for review.

## 2026-09-25 — Measure disclosure controls in one browser frame (Checkpoint 1)

- Fast-forwarded the clean managed worktree to published `6d44263d`. CI run
  `36122613852` read label y=662.656 h=18 and button y=686.656 h=44 in
  separate calls, implying a 37px center gap. Its later atomic JSON instead
  read label y=699.656 and button y=686.656: both centers were 708.656.
  The focused test also had separate enlarged-text box reads.
- The focused browser test now reads the disclosure row, label, button, font
  state and clipping in one scoped DOM evaluation per layout state. The strict
  `<2px` center check, loaded-font check, one-button interaction, phone target,
  overflow, no-write and page-error assertions remain. A temporary one-column
  grid at light 390px creates a real second row; the same predicate rejects its
  39px center gap, and the inline style is removed before normal screenshots.
- Populated light/dark Chromium and WebKit JSON captures at 320, 375, 390,
  768, 844, 1280 and 1440px all show 0px center gaps. At enlarged 390×420,
  the label wraps to 54px, the button remains 44px high, and centers still
  agree in both engines. Inspected normal 390px and 768px light screenshots
  from both engines plus enlarged 390px screenshots; the disclosure control
  remains readable and aligned. Artifacts are in
  `/tmp/aflow-ui-disclosure-cp1-artifacts/` for this local run.
- Verification: `npm --prefix apps/aflow_app/web test -- --run` passed 649;
  `npm --prefix apps/aflow_app/web run build` passed; the focused
  `uv run --directory apps/aflow_app/server pytest -q tests/test_ui_followup_fidelity_browser.py -k failed_review_history`
  passed 14 in Chromium and 14 with `AFLOW_TEST_BROWSER=webkit`; the combined
  `uv run --directory apps/aflow_app/server pytest -q tests/test_ui_followup_fidelity_browser.py tests/test_ui_demo_fidelity_browser.py`
  passed 49; `git diff --check` passed. Browser commands used
  `AFLOW_BROWSER_ARTIFACT_DIR=/tmp/aflow-ui-disclosure-cp1-artifacts`. Local
  verification does not establish hosted CI, publication or live activation.

## 2026-09-25 — Make large project run history concise (Checkpoint 2)

- Replaced the tall off-page selection notice with one compact pinned run row.
  When Load more reaches that identity, the row moves into its normal position
  while preserving selection, focus, and document scroll. Grouped loaded
  `outcome-unrecorded` rows under a counted disclosure after ordinary runs;
  selecting a gap opens it, and user disclosure choice survives refresh.
  Quiet gap rows omit repeated status pills while retaining raw status, full ID,
  title, and known facts in accessible names and previews.
- Populated light/dark Chromium and WebKit captures at 320×568, 390×844,
  1280×720, and 1440×900 were inspected against the frozen calm-workspace
  reference. Three recent runs remain above the fold; 97 older outcome gaps
  occupy one disclosure. No material sidebar hierarchy or clipping gap was
  found. The authenticated header and richer run detail remain existing
  differences. The browser fixture checks touch preview, 44px targets,
  overflow, page errors, Back/focus/scroll, and Load more identity uniqueness.
- Updated two existing browser fixtures exposed by the broader gate: the
  paged service wrapper now forwards Checkpoint 1's `order`; the late-context
  hit test accepts a settled layout that stays in place; WebKit's canceled
  same-origin detail reads during deliberate navigation are excluded from its
  page-error assertion. The new sidebar fixture still asserts no page errors.
- Verification: web suite 653 passed; production build passed with the
  existing chunk-size warning; focused Chromium and WebKit browser suites
  passed 37 each; full server suite passed 512 with 3 dependency warnings;
  `git diff --check` passed. The published `be28adcb` base CI gate succeeded.
  Checkpoint changes remain uncommitted for review; publication and live
  activation remain separate gates.
- Review follow-up: the 123-run fixture's first page previously called its
  100 loaded rows `100 recorded`. The Project runs heading now uses only
  loaded-page membership and says `100 loaded` while a cursor remains, then
  `123 recorded` after Load more exhausts it; a pinned direct-link row does
  not inflate either count. Web tests passed 653, the production build passed,
  the focused Chromium and WebKit browser cases passed 1 each, and
  `git diff --check` passed. The current `origin/main` gate is red, so this
  local correction remains uncommitted and unpublished for review.
- Checkpoint 2 review: the isolated timeout case and all 653 web tests with one
  Vitest worker passed; the default parallel suite timed out in that case twice.
  `origin/main` `46224dfa` subsequently passed all 12 CI jobs. Integration,
  publication, and live comparison remain separate delivery steps.

## 2026-09-25 — Answer strict owned Reasonix ACP permission requests

- Verified `tool_approval=yolo` prompts now answer only same-session,
  well-formed `session/request_permission` with an offered `allow_once` choice.
  The stdio seam bounds message size and request count, retains prompt
  correlation and deadline, and omits raw permission payloads from turn output.
- Added fake stdio coverage for valid interleaving, invalid requests, limits,
  and existing config-notification settlement. A real-pipe regression covers
  coalesced notification and permission frames; a persistent bounded byte
  reader also rejects permission frames buffered before the prompt. The
  separate failed history repair run remains available for supported recovery
  after delivery.

## 2026-09-25 — History list resume preview performance (fresh DS4.1 repair, Checkpoint 1)

- Pre-change live baseline, recorded in the original plan: the p100 100-row
  visible history request timed out at 90 seconds both with and without progress
  enrichment. This pass did not repeat an authenticated live timing.
- Scope was ported from the held successor's uncommitted diff as read-only
  evidence and re-verified here, rather than copied wholesale. The held worktree
  was inspected with `git status`/`git diff` only and remains byte-for-byte
  unchanged; the failed lineage's `in_flight` state was not touched.
- `DaemonService.run_status` gained `include_resume_preview` (default `True`, so
  every existing caller including selected-run detail and mutations keeps full
  status). `ControlPlaneService.list_runs` passes `False` per row and omits
  `evidence.can_resume` instead of forging `false`; `include_progress` stays
  independent.
- `apps/aflow_app/server/tests/test_control_plane_api.py::test_history_pages_skip_resume_preview_but_detail_keeps_it`
  seeds 123 `needs_attention` history records and counts both `_can_resume` and
  `predecessor_inactive_for_preview` calls: zero across the first and next pages,
  both `oldest` and `recent` cursor orders, and both progress modes, while a
  direct detail read makes exactly one preview and returns `can_resume: false`.
- `runPresentation.ts::isUnrecordedOutcome` now treats an omitted list preview as
  unknown rather than a denial (`evidence.can_resume !== true`) while keeping the
  same fully evidenced missing-outcome shape; explicit `can_resume === true` and
  ambiguous activity/unit/metadata/reason shapes stay “Needs attention.” The
  projection is display-only — the resume action gate in `RunDashboard.tsx` still
  requires an explicit `true` and detail rechecks admission.
- Verification actually run: core `tests/test_aflowd.py tests/test_control_plane_repository.py`
  63 passed; server `tests/test_control_plane_api.py tests/test_mcp.py` 118
  passed; the populated 123-record Chromium and WebKit
  `test_recent_runs_are_on_first_history_page_without_extra_fetch` cases both
  passed with `Older runs without a recorded outcome · 97 loaded` collapsed and
  the recent rows actionable at 320/390/1280/1440 widths without overflow; web
  unit suite 664 passed (29 files, including 23 focused `runPresentation` cases);
  `npm run build` succeeded; Ruff and `git diff --check` clean.
- Deployed throughput remains unproven: the 90-second live baseline was not
  re-timed here, so no speed or activation claim is made. If the deployed list is
  still slow, the remaining cost needs its own focused profile.

## 2026-09-25 — Separate completed review turns from delivery failure (Checkpoint 1)

- For terminal `controller_failed` runs, the Runs overview now recognizes only
  a matching, ordered `final_review` start/finish pair with `outcome=completed`
  and a controller-stage worker exit. It says the review invocation finished,
  delivery is blocked, and publication is unconfirmed. The known dirty primary
  checkout merge reason becomes a short cause; other controller reasons stay
  generic. Neither the event outcome nor reviewer text is treated as approval.
  A controller failure without the completed event remains a generic failure;
  reviewer-harness failures retain their earlier warning. Exact reasons remain
  in Diagnostics, and action admission is unchanged.
- Inspected read-only evidence for run `20260925t092319z-acdc180d`: ordered
  final-review start/finish events, a completed invocation, failed canonical
  status, and a merge-handoff dirty-checkout controller error. The receipt's
  exit time is rounded to the same second as the finished event, so the UI
  relies on event order and terminal controller stage rather than a strict
  timestamp comparison.
- Populated Chromium and WebKit captures at 320×568 and 1280×720 in light and
  dark were inspected. The issue cause, coordinator step, failed status, and
  unconfirmed publication read clearly without raw paths or horizontal
  overflow; Diagnostics reveals the exact cause and reads cause no writes.
  The existing browser fixture varies in when detailed checkpoint history
  finishes loading, so those screenshots are evidence for the overview, not
  for complete history. Physical mobile keyboard and live deployment remain
  unverified coordinator checks.
- Verification: `npm --prefix apps/aflow_app/web test -- --run` passed 667;
  `npm --prefix apps/aflow_app/web run build` passed;
  `uv run --directory apps/aflow_app/server pytest -q tests/test_ui_followup_fidelity_browser.py`
  passed 37; `AFLOW_TEST_BROWSER=webkit uv run --directory apps/aflow_app/server pytest -q tests/test_ui_followup_fidelity_browser.py`
  passed 37; `env -u AFLOW_ADMISSION_RESERVATION_NONCE uv run --directory apps/aflow_app/server pytest -q`
  passed 559; and `git diff --check` passed. The AFlow worker inherited
  `AFLOW_ADMISSION_RESERVATION_NONCE`, which made two unrelated in-process
  worker browser tests fail with a nonce mismatch. Running the full server
  command with only that inherited variable removed passed; both failures
  also passed in isolation with the same environment correction. The checked
  out `origin/main` SHA `6cecbdce` had a successful exact-SHA CI run before
  this checkpoint's local verification. Publication and live activation of
  this checkpoint are still coordinator gates. `origin/main` advanced to
  `4ce0d0d1` during the work; reviewer integration must account for its
  separate history-list projection and DEVLOG changes.

## 2026-09-25 — Wait for the Settings section control in browser checks (Checkpoint 1)

- [CI run 36164919770](https://github.com/evrenesat/aworkflow/actions/runs/36164919770)
  failed at the first Workflows selection after opening Settings at 320×568:
  Ubuntu Python 3.13 Chromium had 1 failure among 560 server tests, and Ubuntu
  Python 3.12 WebKit had 1 failure among 44 responsive checks. Both timed out
  after 30 seconds waiting for the desktop `Workflows` tab. The failed journey
  produced no threshold screenshot because selection failed before its captures.
- The helper's immediate `Settings section` combobox `count()` could run before
  the Settings header slot mounted, returning zero and committing to a desktop
  tab that cannot appear at 320px. `GlobalSettings` uses a width breakpoint
  below 1200px for its section selector. The helper now waits for the Settings
  heading and the expected visible labelled selector or named desktop tab; a
  missing control reports the expected viewport-specific control. No product
  code changed.
- Exact checkpoint checks passed: the focused zero/save/reload/light/dark/
  inherit test passed once each in Chromium and WebKit; the required
  `-k 'settings or repair_threshold'` command passed once per engine (1 selected,
  43 deselected each); and `git diff --check` passed. Additional route-matrix
  checks at 320×568 and 1280×720 passed in both engines (2 each), covering
  selector and tab navigation. Chromium and WebKit light/dark threshold
  screenshots were inspected: the section selector, zero value, and effective
  value explanation were visible. The journey asserted no page errors and no
  horizontal overflow. Published exact-SHA CI and deployment remain delivery
  gates.

## 2026-09-25 — Full-app invisible refresh verification, Checkpoint 3

- Started clean at reviewed `ea27758f` (merged base `b667e47b`); this checkpoint
  remains uncommitted for review. Held real Settings reads while users edited
  Teams' family wizard, Agents & Roles, Workflows, Prompts, Skills and General.
  The strict WeakMap probe verified visible editor identity and focus across
  equal, changed and failed responses. Skills coverage retained two drafts by
  exact name and the original revision; a changed skill still reports the
  expected conflict. A concurrent workflow edit now keeps its prior revision,
  and Save all surfaces the real 409 instead of silently losing the draft.
- Reproduced and fixed two read-boundary defects: a reload disabled the entire
  editor fieldset, and a changed configuration response replaced an edit made
  while the read was pending. Read-only reloads now leave the editor enabled;
  save remains guarded. Changed reads preserve dirty domain state and show a
  conflict warning. A changed/failed warning now anchors the focused editor
  in view when inserted above it. The existing single save coordinator, Undo,
  prompt deletion recovery and per-skill acknowledgement paths remain in place.
- The combined six-section Settings capture ran in Chromium and WebKit at
  320×568, 390×844, 844×390, 1280×720, 1440×900 and 390×420, plus 390×844
  with 125% root text. It checked strict visible roots, document ownership,
  horizontal overflow, compact touch target height, keyboard focus, page
  errors and mutation requests. Paired frozen-demo and real populated Settings
  frames are under `/tmp/aflow-cp3-full-final/` and
  `/tmp/aflow-cp3-webkit-final-matrix/` (the `test_ui_demo_cp9_settings_effe*` folders).
  Representative pairs: `...effe0/cp9-reference-settings-teams-light-1280x720.png`
  with `...effe0/cp9-settings-teams-light-1280x720.png`, and
  `...effe3/cp9-reference-settings-skills-dark-390x844.png` with
  `...effe3/cp9-settings-skills-dark-390x844.png` in the WebKit root. A
  separate combined six-screen journey covers Runs, All runs, Projects, an
  open Plan editor, New run and Settings at the same matrix points. It uses
  ordinary UI navigation, strict probes, populated fixtures and paired demo
  captures under the `test_ui_demo_cp9_combined_six_*` folders; for example,
  `...six_7/cp9-six-app-plans-dark-390x420.png` and its
  `cp9-six-demo-plans-dark-390x420.png` counterpart in the WebKit root.
- Visual follow-up: the frozen demo's Settings surface uses a single header
  and a simple left tab rail with placeholder fields. The populated app uses
  the later approved two-row header, top tabs or compact selector, family
  list/detail and full domain editors. Those topology/content differences are
  material, so this checkpoint does not claim pixel parity. A focused owner
  follow-up should approve a current Settings reference for all six sections
  or explicitly accept the recorded divergence; preserve the frozen demo bytes
  until that decision. Its SHA-256 remains
  `2465c0ac2bfef90af2b98a537ad5ac3e931b927c23a78379a8c532ce4dcbadf4`.
- Verification: web test 667/667 and build passed. The first full server run
  had 594 passed and 3 failed: two in-process worker journeys inherited
  `AFLOW_ADMISSION_RESERVATION_NONCE`, as previously documented in this log;
  one paused-run refresh probe missed its event read but passed alone. With
  only the inherited nonce removed, both worker tests passed alone and the
  full server suite then passed 597/597. After adding the combined six-screen
  matrix, the final full server suite passed 606/606 in 819.14 s. The specified
  final WebKit fidelity/Settings selection passed 71/71 in 294.48 s, using
  separate `/tmp/aflow-cp3-webkit-final-matrix/` artifacts. `git diff --check`
  passed.
  Physical mobile keyboard/browser chrome and exact-SHA CI/deployed comparison
  remain coordinator or owner checks; no live activation is claimed.

## 2026-09-25 — Confirmed Settings discard follow-up

- Fixed a confirmed Reload server settings read discarding an edit entered after
  confirmation. The confirmation now clears the old dirty guards immediately;
  the subsequent workflow-config, server-settings and project-scheduling reads
  protect new edits, while a clean changed response still updates its domain.
  The existing revision remains attached to a retained draft, so a concurrent
  workflow change produces the normal save conflict instead of a silent rebase.
- Added disposable real-browser cases for equal, changed and failed held reads
  in all three domains after confirmation, plus clean changed-read acceptance.
  They check that the old edit is discarded, the new editor node, focus and
  Save all remain usable, failed reads retain content, and no incidental writes
  occur. The real workflow conflict path retains the new value.
- Verification: web 667/667 and build; Settings browser 39/39 in Chromium and
  WebKit; populated demo-fidelity browser 44/44 in both engines; full server
  618/618 with only the inherited `AFLOW_ADMISSION_RESERVATION_NONCE` removed
  from that test process; `git diff --check` passed. One WebKit fidelity attempt
  had two navigation/paused-run timing failures outside Settings; both passed
  alone and the complete suite passed on rerun. A preceding WebKit attempt was
  terminated externally (143) before completion.
- Inspected representative populated Workflow Settings captures at desktop and
  mobile sizes in light and dark against the frozen reference. The previously
  recorded layout/content divergence remains; this repair adds no new layout
  change. The approved demo SHA-256 is unchanged at
  `2465c0ac2bfef90af2b98a537ad5ac3e931b927c23a78379a8c532ce4dcbadf4`.
  Deployment and physical-device checks remain separate gates.

## 2026-09-25 — Final-review repair evidence, checkpoint 1

- A completed three-checkpoint plan now records a validated final-review fix
  overlay as a separate cumulative rejection tied to its exact final worker
  attempt. Checkpoint approval state stays closed, and retrying the same review
  cannot add repair credit.
- Resume accepts the focused overlay when the persisted rejection and worker
  evidence agree. Clean approval, unmatched overlays, changed original
  checkpoint state and missing worker evidence create no rejection.
- Focused verification: 88 tests passed across repair upgrades and both resume
  suites. Worker routing remains the next checkpoint's work.

## 2026-09-25 — Final-review repair routing, checkpoint 2

- Historical run `20260925t171339z-3b11d2a6` was inspected read-only: turn 7
  was an Astra `final_review` with a valid focused overlay, while turn 8 used
  baseline Sol High. The completed checkpoint scope had already closed.
- The verified cumulative rejection now feeds the configured repair policy at
  the post-review boundary. Threshold 0 selects the one-hop repair team for
  the immediate follow-up; higher thresholds count distinct failed workers.
  The selected selector is persisted before launch, follow-up attempts extend
  the cumulative lineage, and normal hotplug handles cross-harness handover.
- Focused routing, handover and resume verification passed 191 tests. The
  first full Python run had 2479 passed and one unrelated admission-test
  failure from inherited `AFLOW_ADMISSION_RESERVATION_NONCE`; that test passed
  alone with the variable removed. The full rerun with only that variable
  removed passed 2480 tests and 241 subtests. `git diff --check` and scoped
  Ruff passed. Publication, exact-SHA CI and live activation remain separate
  coordinator gates.

## 2026-09-25 — Managed-worktree final-review repair follow-up

- Cumulative repair validation now checks the focused overlay in its execution
  worktree while retaining primary-root ledger identities. Resume admission
  reuses the validated lifecycle context before the ordinary worker boundary.
- Temporary Git-worktree regressions cover both manager modes, a stopped-run
  resume using the selected DS4.1 worker, and rejection of a missing execution
  overlay even when a primary-checkout copy exists.

## 2026-09-27 — History-gap focus CI repair, checkpoint 1

- CI 36307782108 caught a race in the pinned gap migration test: it focused the row before the initial deep-linked detail had finished opening, so the detail heading's pending focus effect could run afterward. The adjacent ordinary-row test had the same ordering gap.
- Both tests now hold the selected-detail response, confirm the detail and pin are absent while pending, release it, and wait for the detail heading's initial focus before focusing the pinned row and loading the next page. They retain exact selection, outcome-group visibility, and replacement-row focus assertions. No product focus change was needed after readiness.
- Verification: the focused two-test command passed three consecutive runs; the full web suite passed 671/671; the production build and `git diff --check` passed.

## 2026-09-27 — Explicit owner-stop for unscoped historical runs

- Added a boolean acknowledgement to REST and MCP owner-stop. Only a validated
  null-scope manifest with the exact registered root, run, and intended unit
  can use the narrow daemon exception; other lifecycle controls retain strict
  scope checks.
- Bound acknowledgement to control idempotency, recorded it in owner-stop
  evidence, and kept exact-unit stop and bounded timeout behavior. Fixture
  tests cover authorization, replay, and failed unit observations. Real-record
  reconciliation belongs to the coordinator after deployment.

## 2026-09-27 — Compact selected-run focus delivery repair

- CI 36308511775 exposed a product race: opening a compact history row while
  its exact detail was pending focused the temporary “Loading run details”
  heading. Replacing that heading dropped focus outside the visible detail.
  A deferred-response component test reproduced the row → loading heading →
  lost-focus sequence before the repair.
- The shared layout now focuses its retained detail surface while the selected
  response is pending, then its durable heading if focus has stayed there.
  Back, inactive surfaces, and deliberate focus changes cancel that transfer;
  settled refreshes do not request it again. The dashboard marks a selected
  detail ready only after its exact response is accepted.
- Deferred selection, Back with a late response, and deliberate-focus tests
  passed. The full web suite passed 673 tests; the production build and the
  existing Chromium and WebKit navigation journey passed. Publication,
  exact-SHA CI, and live activation remain coordinator gates.

## 2026-09-27 — Recovery pruning fixture CI repair

- The pending-artifact recovery fixture now uses a legacy direct CLI run ID,
  which is eligible for pruning without a launch manifest. A paired retention
  regression keeps a canonical missing-manifest run while pruning the legacy
  CLI run. Production retention policy is unchanged.

## 2026-09-27 — Browser readiness ordering CI repair

- CI 36311583627 exposed two probe assumptions: a visible plan row did not
  prove the independent queue request had reached its interceptor, and the
  selected-run probe grouped other clients' run-list reads with the dashboard's
  `order=recent` read by path alone. A separate initial run-list request was
  observed without a terminal event while the dashboard's own read finished.
- The queue fixture now starts the list request first, waits for the queue
  interceptor to hold a real response, then releases the list and measures
  response-to-render time. The five-second queue hold and editor assertions
  remain intact. The selected-run fixture traces exact request start, response,
  finish or cancellation by phase, releases context/events/status out of order,
  and requires the latest read of every selected class to settle before UI
  continuity checks. Changed and failed phases also wait for their read outcomes.
- The nine affected browser cases passed in Chromium and WebKit locally, as did
  the web build, Ruff checks, and `git diff --check`. No product source changed;
  exact-SHA macOS CI and deployment remain coordinator gates.

## 2026-09-27 — Dashboard WebKit job budget repair

- CI 36313739241 passed all 44 WebKit checks in 379.77 seconds, then cancelled
  the Ubuntu Python 3.12 dashboard job at its 25-minute limit. Raised only that
  matrix cell's job budget to 35 minutes; the other dashboard cells retain 25.

## 2026-09-27 — Session tampering fixture CI repair

- CI 36315525783 failed the macOS Python 3.13 auth test when replacing the last
  two Base64 signature characters changed only unused padding bits, leaving the
  decoded HMAC unchanged. The fixture now flips a decoded signature byte before
  re-encoding it; production session verification is unchanged.

## 2026-09-30 — Live event-stream responsiveness

- A p100 sample attributed 338 of 370 GIL-held samples to the event polling
  path; resume admission scanned the project history on the async HTTP loop.
  Event reads now skip that preview, and initial plus periodic SSE reads use
  the existing thread pool. Explicit run-detail admission hints remain intact.
- Regression checks cover skipped previews, unchanged event delivery, deleted
  run visibility, and health responses while either SSE read is blocked.

## 2026-10-01 — Concierge staged plan lifecycle and mutation budget

- Replaced the combined `plan_and_start` tick action with separate
  `create_plan` and `promote_plan` actions so owner-issue plan work stages
  across ticks: first tick authors and creates a `todo` plan, a subsequent
  tick promotes the proven draft to `in_progress`, and a later tick starts
  the now-ready plan through normal selection and preflight.
- Added a tick-local `MutationBudget` that enforces at most one external
  mutation (MCP write or GitHub issue creation) per concierge tick. The
  budget is consumed immediately before issuing the request; timeouts,
  rejections, and failures consume it, and subsequent reconciliation is
  read-only.
- Added `ConciergeDraftRecord` private provenance records under
  `planner-records/` that preserve canonical issue identity, source hash,
  generated document hash, and plan identity/revision for safe promotion.
- Updated triage selection priority to: (1) defer on occupancy, (2) repair
  failed delivery, (3) resume safe failed lineage, (4) start ready
  `in_progress` plan, (5) promote proven concierge draft, (6) create plan
  for oldest uncovered owner issue.
- Updated `concierge_prompt.md`, `deploy/concierge/README.md`, and
  `ARCHITECTURE.md` to describe the staged lifecycle and mutation budget.

## 2026-10-04 — Persistent-chat wakeup ownership visibility repair (issue 69)

- Checked in the p100 bootstrap wakeup wrapper as
  `scripts/concierge/codex_chat_tick.py`, the host-specific adapter that wakes
  the pinned concierge chat through the local Codex app-server Unix socket
  under the cron launcher's bootstrap flock.
- Replaced the one-shot ownership lookup with bounded reconciliation that
  tolerates empty history, delayed `userMessage.clientId` visibility, lost
  start replies, reconnects, buffered completion events, and capped
  pagination (10 pages per pass). Ownership is proven only by an exact nonce
  match; an accepted turn ID alone never grants ownership, and unproven turns
  are never interrupted.
- Kept the wrapper in the foreground under one 780-second service-start
  deadline (with oneshot service-start monotonic adoption) through visibility
  waits, supervised execution, the minute-12 exact-owned advisory, and the
  final five-second cleanup window, so the parent flock is held for the whole
  supervised lifecycle. The no-socket exact-chat CLI fallback now runs a
  supervised foreground child within the remaining budget instead of an
  unbounded `execv`.
- Added mock-only deterministic tests in `tests/test_concierge_chat_tick.py`
  (real temporary Unix WebSocket servers, injected clock, harmless fake CLI
  children, isolated flock/subprocess lifecycle assertions) covering delayed
  visibility, buffered completion, start races, pagination, cleanup races,
  deadline bounds, prior-tick recovery, CLI fallback, and receipt privacy.
- Real idle wakeup acceptance on p100 remains pending coordinator evidence;
  host installation is coordinator-only after exact-SHA CI on `origin/main`.
