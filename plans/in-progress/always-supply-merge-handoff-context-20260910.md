# Always supply exact lifecycle merge context

## Summary

The built-in merge instruction only says to use aflow-merge. `_build_merge_user_prompt` adds exact branch/path values only when optional user merge_prompt templates request them. With the default empty template list, the merge worker receives no exact target, feature branch, or plan context. A real terminal delivery resume20260910t141449z-6e944a4e stopped for missing exact merge handoff context. Supply mandatory engine facts independently of optional customization.

## Git Tracking

- Plan Branch: `aflow-always-supply-merge-handoff-context-20260910-20260910-145043`
- Pre-Handoff Base HEAD: `a4977dc4a8613862f6205f59c68ddf6f56a34041`

## Done Means

Every merge invocation includes the exact target/feature branches, primary/execution roots, optional feature worktree path and original/active/new plan paths from the current verified execution context, including when merge_prompt is empty. Existing custom prompt rendering/order and local-only merge semantics remain intact.

## Critical Invariants

Engine context is mandatory and comes from execution identity, not model inference, branch enumeration, stale prior prompts, or user templates. Preserve exact path bytes and distinguish absent worktree with null/explicit absence. Custom instructions cannot remove the required context. Do not change lifecycle branch selection, Git behavior, approval, or live configuration.

## Forbidden Implementations

No global config workaround, hardcoded project paths, new setting, duplicate prompt registry, remote fetch by merge workers, or change to worker chain. Do not fix unrelated manager zero-turn fallback behavior in this checkpoint; preserve its evidence for subsequent work.

## Checkpoints

### [x] Checkpoint 1: Add mandatory structured merge facts to the built-in handoff

**Goal:** The existing merge worker can act on exact context without optional templates.

**Context:** Read nearest AGENTS. Inspect `aflow/workflow.py` `_MERGE_BUILTIN_INSTRUCTION`, `_build_merge_user_prompt`, `render_merge_prompt`, `_execute_merge_handoff`, terminal integration-only resume; `aflow/bundled_skills/aflow-merge/SKILL.md`; existing merge prompt/lifecycle tests in `tests/test_runtime.py` and `tests/test_workflow_prompts.py` if present. Discover test locations with `rg -n 'merge_user_prompt|merge_prompt|terminal_integration' tests`.

**Scope:** Small merge prompt builder fix, directly related tests, DEVLOG and concise ownership docs. No new model/controller subsystem or broad prompt refactor.

**Steps:**

- [x] Add a compact labelled structured context block to the built-in merge prompt, containing main_branch, feature_branch, primary_repo_root, execution_repo_root, feature_worktree_path and original/active/new plan paths from supplied arguments. Serialize values safely so spaces/newlines/unusual branch/path text cannot corrupt field boundaries; do not change their values. This block is present even with zero custom merge_prompt entries.
- [x] Append existing rendered custom templates in their existing order. Preserve unresolved-template validation and current lifecycle profile/cwd selection. Do not rely on templates to re-supply mandatory fields or infer missing identity from Git.
- [x] Test the actual invocation captured by a fake adapter/runner with empty merge_prompt for ordinary completion and terminal integration-only resume. Assert exact current context values and primary cwd. Add custom-template retention/order and unusual path serialization coverage. Reuse existing lifecycle fixtures; no real provider or production merge.
- [x] Update DEVLOG and a concise existing architecture description to state engine-owned merge facts versus optional instructions. Change the bundled skill wording only if needed to name the supplied field format, preserving its local-only ownership rules. Root AGENTS unchanged.

**Dependencies:** None; may run independently of responsive UI and completed-plan bookkeeping, in an isolated workflow root. Carry accepted main changes forward at integration.

**Verification:**

- `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 uv run pytest -q tests/test_runtime.py tests/test_config.py --tb=short`
- Run any separately discovered prompt-test module explicitly.
- `uv run ruff check aflow`
- `git diff --check`

**Done When:** Default and custom merge prompts carry exact context in actual captured invocation; ordinary and zero-turn terminal resume tests pass. Report scoped diff and verification, then existing reviewed publication/CI/live workflow applies.

**Blockers:** Do not guess ambiguous runtime identities. Missing required execution context remains a bounded error; valid defaults must not depend on user-added templates.

## Behavioral Acceptance Tests

Given a valid execution context and empty merge_prompt, the merge worker gets all exact facts. Given custom templates, those instructions remain appended with current values. Given terminal resume, original run execution identity is supplied rather than an empty turn-zero context. Given a path with whitespace/newlines, serialized facts retain the original value.

## Plan-to-Verification Matrix

Mandatory context: captured adapter invocation. Custom behavior: rendering/order regression. Resume: terminal integration-only fake lifecycle. Safety: serialization/exact identity assertions.

## Assumptions And Defaults

Use the existing prompt builder and execution context. This is required context repair, not a new configurable feature. Manager fallback losing merge_failure when a resume has no finalized turns is a separate observed defect and must not expand this checkpoint.

## Review Log

- 2026-09-10: Checkpoint 1 approved through `cp1 v01`. Reviewed the immediately preceding worker turn-001 scope using the current-worktree fallback against `a4977dc4a8613862f6205f59c68ddf6f56a34041`; no checkpoint 1 commit existed. No material findings. Runtime/config/docs verification passed (445 tests, 50 subtests), as did `uv run ruff check aflow` and `git diff --check`. Reviewer owns the approval commit. Publication, exact-SHA CI, and live activation remain coordinator-owned and unverified; keep this plan in progress until delivery.
