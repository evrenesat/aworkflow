# CP7 review — shared dirty-worktree startup

Reviewed `cp7 v01` on `codex/aflow-dogfood-20260909` using the immediately preceding repaired worker worktree against approved CP6 `f77b5b1`. Worktree fallback was required: no pending CP7 commit existed, with zero intervening implementation commits. The older CP6 approval is the base, not the target.

Original plan: `plans/in-progress/live-configuration-without-snapshot-gates-20260909.md`. Active non-checkpoint repair overlay: `plans/in-progress/live-configuration-without-snapshot-gates-20260909-cp08-v01.md`, targeting original CP7 despite its filename.

Scope: typed NUL-aware status parsing and ordered preflight results; existing dirty confirmation; acknowledgment through prepared, daemon and controller handoffs; final lifecycle startup checks; focused tests and documentation. Both prior material findings are resolved: supported non-Git lifecycle bootstrap reaches preparation, and operation markers no longer contaminate a clean linked checkout. Existing-checkout inspection failures and selected-checkout conflicts remain blocking. Fresh-worktree setup preserves source bytes and uses committed content.

Verification:

- `uv run pytest tests/test_dirty_worktree_preflight.py tests/test_cli.py tests/test_runtime.py tests/test_git_status.py -q`: 467 passed, 168 subtests passed.
- `uv run pytest tests/test_library_api.py -q`: 33 passed, 6 subtests passed.
- `uv run ruff check aflow/api/startup.py aflow/git_status.py tests/test_dirty_worktree_preflight.py`: passed.
- `git diff --check`: passed.

The material finding admission gate, exclusions and proportionate-fix discipline yielded zero findings. CP7 is approved; only its original approval marker and review metadata advance. CP8 remains unchecked. The active repair overlay is resolved; no further fix plan is needed. The reviewer creates the `cp7 v01` approval commit in this turn without squashing prior checkpoints. Manager 40 KiB hard guard and compact 16 KiB summary remain unchanged. Verification used temporary fixtures and synthetic providers; no public push, real global configuration/skills or service edits.

No material findings
