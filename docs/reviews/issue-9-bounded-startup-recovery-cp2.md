# Issue 9 — Checkpoint 2 implementation report

Date: 2026-09-11
Scope: one-shot neutral startup recovery guard exception
Status: implementation handoff; not reviewer approval

## Result

The bundled guard now has a separate receipt validator and durable attempt
claimer. It admits only an explicitly authorized launch with matched evidence of
a terminal pre-controller missing or blank Git Tracking failure, zero started
and finalized turns, no owned process/provider/branch/worktree activity,
unchanged plan and launch choices, and mechanically derived branch/base
identity. Unknown or changed evidence is rejected.

The claim is atomic in the existing external guard-state directory and precedes
the caller's normal launch. It records request and predecessor identity,
correction reason, original-plan digest, selected launch identity, and an
acknowledged successor. Repeated attempts launch nothing. Failed or uncertain
responses pause without replay; an uncertain response can only be reconciled
by the same idempotency key returning the already acknowledged successor.

Legacy runs retain their supported launcher. UI-server and aflowd runs use the
advertised authenticated web/MCP start and startup-answer path; the snapshot
helper and all other observer boundaries remain read-only. The installer and
refresh tests verify that the new helper is bundled and stale report resources
remain absent.

## Follow-up repair

The CP2 review found that the external claim/outcome sequence could not record
an acknowledged result and that reusing the predecessor idempotency key replayed
the daemon's typed failure. The repair now records acknowledged, failed, and
uncertain outcomes through one bounded transition API, preserves the consumed
attempt, and rejects changed keys or conflicting successors. Each claim keeps
the predecessor key as lineage and durably emits one distinct replacement key;
the daemon's original key still replays its failure while the replacement key
reaches normal preparation once. Focused CLI and in-memory daemon regressions
cover both paths.

## Focused verification

- `uv run pytest -q tests/test_guard_recovery.py tests/test_guard_snapshot.py tests/test_guard_daemon_ownership.py tests/test_guard_issue.py tests/test_docs.py` — passed.
- `uv run pytest -q tests/test_skill_install.py -k 'discover_bundled_skills_uses_package_resources or include_optional_links_supporting_resources_through_the_link' tests/test_skill_refresh.py -k 'real_guard_refresh_contains_recovery_helper_without_stale_bundle_files'` — passed.
- `uv run ruff check aflow/bundled_skills/aflow-guard-development-run/scripts/aflow_guard_recovery.py aflow/bundled_skills/aflow-guard-development-run/scripts/aflow_guard_snapshot.py aflow/bundled_skills/aflow-guard-development-run/scripts/aflow_guard_issue.py` — passed.
- `git diff --check` — passed.

Full suites and live guarded-run behavior are CI/coordinator-owned. No real
guarded run, provider, web/MCP endpoint, or installed user skill was changed.
