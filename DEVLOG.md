# DEVLOG

## 2026-08-07 — Preserve guard replacement lineage

- Replacement linkage now retains the original recovery fingerprint, validates
  complete predecessor/successor identity and one live successor controller,
  and migrates unsafe same-successor linkage without rewriting observation
  history.
- Missing or malformed identity fields and absent original plans fail closed
  before guardian state is written.

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
