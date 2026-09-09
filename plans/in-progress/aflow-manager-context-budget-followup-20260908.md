# aflow manager context budget and failure evidence

Status: Checkpoint 1 reviewed and approved through `cp1 v01`; Checkpoints 2–6 remain pending. Existing compatible serialization work was retained.

Execution amendment (2026-09-09): retain the owner-approved 40 KiB hard guard already in the current baseline. The later readable-output plan adds the separate 16 KiB current-summary target. Historical incident sizes below remain historical evidence.

## Summary

Prevent ordinary run history and JSON formatting from stopping valid manager decisions. Keep the exact 40960-byte UTF-8 user-prompt limit, preserve decision-critical evidence, and retain truthful diagnostics when essential context cannot fit. This handoff supersedes `plans/aflow-manager-context-budget-followup-20260908.md` as the implementation ledger.

The handoff also adds prompt-variable help beneath editors and readable sentence-style labels throughout the web UI, preserving exact machine identifiers.

## Git Tracking

- Plan Branch: `codex/aflow-dogfood-20260909`
- Pre-Handoff Base HEAD: `93a54774d8f4a92e05d04f99b936f43c88ccf673`

### Review Log

- 2026-09-09: Approved Checkpoint 1 through `cp1 v01` using the current worktree fallback at base `93a5477`. Run `20260909t214357z-0efa55a8` identifies worker turn 1 and Checkpoint 1 as awaiting review; the next-checkpoint snapshot already points to Checkpoint 2. Retained the existing compact serializer and added exact final-wire boundary regression. Verification: 56 manager tests passed with disposable `Path.home()` configuration; `git diff --check` passed. No material findings. Checkpoints 2–6 remain unchecked.

## Context Bootstrap

Run from the assigned execution checkout, not a guessed primary repository path:

```sh
hostname
git rev-parse --show-toplevel
git status --short --branch
rg --files -g AGENTS.md -g ARCHITECTURE.md -g DEVLOG.md -g README.md
cat AGENTS.md aflow/AGENTS.md
rg -n 'build_manager_prompts|enforce_manager_inline_context_budget|manager_prompt_metrics' aflow/manager.py
rg -n 'MANAGER_RUN_EXTRACT_MAX_RECORDS|manager_decisions|artifact_roots' aflow/manager_context.py
rg -n '_manager_prelaunch_failure_context|prelaunch_failure|build_manager_prompts' aflow/workflow.py
```

Read nearer instructions before modifying their directories. Inspect the existing manager architecture and tests. Preserve unrelated changes; the named local serialization changes belong to this handoff and are not a blocker.

## Baseline Evidence

- Use the assigned execution checkout; on p100 the host is `codex`. HEAD at inspection: `3a4c4f9`, including the committed manager artifact-root correction. Check current Git state before edits; concurrent AFlow work may advance it.
- Preserve the current local changes in `aflow/manager.py`, `tests/test_manager.py`, `aflow/AGENTS.md`, and `DEVLOG.md`. They compact schema-v3 JSON with UTF-8 serialization and add lossless ASCII/Unicode regression tests. Do not redo or discard them.
- Doublangu run `20260908t203000z-1cec7085`, turn 17, Lite decision 20 failed before provider launch. Reported user prompt: 33646 bytes. Dominant fields included run extract (10267 compact bytes), finished turn (5278), manager decisions (4654), and controller state (3917).
- Read-only reconstruction from the saved boundary produced 33611 pretty-printed bytes and 28754 compact UTF-8 bytes, with identical decoded content. Reconstruction is not claimed byte-identical to the failed original prompt.
- `build_manager_prompts()` previously used indented JSON with default Unicode escaping. The local fix fits this incident without removing fields. It does not guarantee all future contexts fit.
- `build_manager_context()` limits the v3 run extract to 12 records, but builds `manager_decisions` from all earlier decisions. Per-string bounds do not establish a whole-prompt byte bound. Histories can also duplicate the same manager reason across sections.
- `_ManagerCallExecutor`'s prelaunch exception path replaces a successfully built oversized context with `_manager_prelaunch_failure_context()`. Its fallback says context was unavailable and loses useful checkpoint/turn references, even when construction succeeded and only prompt sizing failed.
- Local verification already passed: 98 manager/context tests and 37 manager runtime tests plus 7 subtests. The replacement run `20260908t232307z-9829e745` was observed running after normal Resume; that observation does not prove it completed or passed a later manager boundary.


## Done Means

- The incident-shaped input fits without losing fields; exact sent-byte accounting agrees with enforcement.
- Ordinary histories of 20, 100 and 1000 decisions fit using bounded optional-history projection and resolvable references.
- Current legal decision inputs, reviewer evidence and active rejection authority survive reduction unchanged.
- Irreducible overflow invokes no provider and retains bounded diagnostic evidence with an accurate report.
- All checkpoint tests and the final integration checks pass; documentation describes implemented behavior.

- Every supported prompt variable is explained under the applicable editor, including scope and empty-value behavior.
- User-facing snake_case labels become sentence-style text without changing stored identifiers or prompt text.

## Critical Invariants

- Maximum exact user-prompt size is 40960 UTF-8 bytes, including prefix and newline. The system prompt is outside this existing limit; do not change that contract.
- Serialize, measure and send the same payload. No reduction occurs when the bounded initial projection fits.
- Preserve current boundary authority, artifact paths/digests and existing validation. Never infer approval from missing evidence.
- Preserve schema-v1/v2 behavior; schema-v3 keeps plan and reviewer bodies referenced and declares correct repository/run roots.
- Omitted history remains recoverable through existing controller-owned artifacts; omission metadata itself is bounded and measured.
- Historical runs, managed worktrees, plan progress, frozen configuration and unrelated live controllers remain untouched during verification.

## Forbidden Implementations

- Raising or disabling the cap, estimating by characters, or enforcing a different string from the one sent.
- Blind string slicing, truncating JSON or paths, or dropping protected review/active-scope facts to fit.
- LLM-generated summaries, fabricated continue/review decisions, automatic retry loops, or a new storage service.
- Inlining complete plan/reviewer bodies, copying private run contents into committed fixtures, or resolving evidence against an arbitrary worktree.
- Claiming a fallback context is the original rejected payload, or overwriting historical failure artifacts during reconstruction.

## Checkpoints

### [x] Checkpoint 1: Exact compact serialization

**Goal:** Serialization round-trips every field, accepts exactly-at-limit input and rejects one-byte-over input before provider launch.

**Context:** Inspect the files below and the baseline above; preserve existing compatible behavior and local work.

**Scope:** May modify `aflow/manager.py`, `aflow/manager_context.py`, `tests/test_manager.py`, plus documentation directly describing this checkpoint. Must not modify live run artifacts, plan contents in other projects, transport contracts, provider selection, or unrelated runtime execution.

**Steps:**

- [x] Read root and `aflow/AGENTS.md`; inspect the current local diff and retain the compact v3 serializer. Use one shared serialization path for measurement and final provider input, including `MANAGER_CONTEXT_JSON:` and trailing newline. Avoid budget estimates based on Python character counts or separately serialized top-level fields.
- [x] Keep the hard limit at 40960 UTF-8 bytes and fail before provider invocation for irreducibly oversized input. Preserve decoded JSON exactly when no history reduction is needed.
- [x] Ensure prompt metrics measure the exact sent string. Keep per-field counts as diagnostic attribution, clearly distinct from the complete serialized prompt size.
- [x] Retain ASCII/Unicode regression coverage and add exact-limit and one-byte-over boundary checks against final wire serialization. Existing oversized-input rejection must continue passing.

**Dependencies:** None.

**Verification:**

- Run: `uv run pytest -q tests/test_manager.py`.
- Run: `git diff --check`.
- Observe: Serialization round-trips every field, accepts exactly-at-limit input and rejects one-byte-over input before provider launch.

**Done When:** Every checked step is supported by passing tests or observable evidence, changed files remain in scope, and the stated goal holds. Before handoff run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:** Stop and report if current-boundary authority cannot be distinguished from optional history without changing supported decision behavior, or overlapping unrelated edits make ownership ambiguous. Missing historical incident files are not a blocker for synthetic verification.

### [ ] Checkpoint 2: Bound optional manager history

**Goal:** Long-history fixtures fit deterministically while preserving protected authority and resolvable omitted-history references.

**Context:** Inspect the files below and the baseline above; preserve existing compatible behavior and local work.

**Scope:** May modify `aflow/manager_context.py`, `aflow/manager.py`, manager-call integration in `aflow/workflow.py`, `tests/test_manager_context.py`, `tests/test_manager.py`, plus documentation directly describing this checkpoint. Must not modify live run artifacts, plan contents in other projects, transport contracts, provider selection, or unrelated runtime execution.

**Steps:**

- [ ] Build a deterministic v3 history projection. Initially retain at most 12 recent manager decisions, matching the existing run-extract cap. Keep chronological order with explicit decision numbers and turn numbers; never mix the two numbering domains when selecting the newest records.
- [ ] Protect current boundary authority: finished-turn outcome and exact artifact references; eligible actions and proposed transition; current plan/checkpoint state; active scope/rejection state; current Lite-to-Full escalation evidence; current/retry note scopes; continuation and workspace state; validated evidence references and artifact roots. Historical copies of these facts must not replace their current authoritative fields.
- [ ] Give omitted history exact controller-owned artifact references and explicit omitted counts. Use the existing per-decision directories and turn artifacts rather than copying histories into a second database. Declare the relevant run/root for cross-run references. Do not inline historical prompt bodies or bulk raw reviewer output.
- [ ] Measure after all executor additions, including checkpoint repartition history. If the prompt still exceeds the cap, remove optional history oldest-first: duplicated historical manager records in run extract first, then older manager-decision history, then older workflow-turn history. Preserve any record required by active rejection/attempt authority in the protected sections before reducing its historical duplicate.
- [ ] Stop reducing when exact serialized input fits. Record which categories were reduced and their omitted counts; include that disclosure in the measured payload. Keep the latest relevant failure/rejection evidence and escalation reason. Do not truncate paths, digests, JSON structures, legal actions, or review verdicts to force a fit.
- [ ] If protected current evidence alone exceeds the cap, produce the explicit diagnosable failure in stage 3. Do not call the provider or treat missing information as approval.

History-reference contract: use bounded descriptors per source run and category containing the declared artifact root, existing relative directory/filename pattern, omitted count and numeric ranges. Merge adjacent ranges; do not add one inline reference for every omitted record. If descriptors or protected fields alone cannot fit, use Checkpoint 3's explicit failure. Preserve actual IDs and resolve only files owned by the declared run; never glob unrelated runs. This is reference metadata, not a second history store.

**Dependencies:** Checkpoint 1.

**Verification:**

- Run: `uv run pytest -q tests/test_manager_context.py tests/test_manager.py`.
- Run: `git diff --check`.
- Observe: Long-history fixtures fit deterministically while preserving protected authority and resolvable omitted-history references.

**Done When:** Every checked step is supported by passing tests or observable evidence, changed files remain in scope, and the stated goal holds. Before handoff run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:** Stop and report if current-boundary authority cannot be distinguished from optional history without changing supported decision behavior, or overlapping unrelated edits make ownership ambiguous. Missing historical incident files are not a blocker for synthetic verification.

### [ ] Checkpoint 3: Retain budget-failure diagnostics

**Goal:** Irreducible overflow launches no provider, reports the correct failure stage and retains known checkpoint/turn references and bounded diagnostic evidence.

**Context:** Inspect the files below and the baseline above; preserve existing compatible behavior and local work.

**Scope:** May modify manager-call handling in `aflow/workflow.py`, `aflow/manager.py`, artifact writing in `aflow/runlog.py`, `tests/test_runtime.py`, `tests/test_runlog.py`, plus documentation directly describing this checkpoint. Must not modify live run artifacts, plan contents in other projects, transport contracts, provider selection, or unrelated runtime execution.

**Steps:**

- [ ] Distinguish context construction failure from `ManagerInlineContextLimitError` after successful construction. Use a precise failure reason such as “Manager input exceeds its byte budget before provider launch.” Do not describe this as unavailable or invalid manager output.
- [ ] Preserve the successfully built diagnostic context and exact serialized candidate before substituting the provider-safe fallback. Store in the same decision directory using existing atomic artifact conventions; expose bounded references rather than embedding oversized content in events or reports. Apply a separate bounded diagnostic-storage cap (256 KiB); if exceeded, retain size/digest/per-field counts and explicitly mark body omission.
- [ ] Keep known original/active plan, current checkpoint, finished turn and stdout/stderr references in the failure report. Do not replace them with null or “unknown” merely because the input exceeded a limit.
- [ ] Record attempted bytes, permitted bytes, reduction counts, failure stage and diagnostic artifact references. On evidence-write failure, report that failure honestly; do not claim retained artifacts exist.
- [ ] Keep the rejected manager boundary resumable through existing durable state. Recovery must preserve unfinished work and revalidate review/manager decisions through existing admission; no fabricated continue decision and no automatic retry loop against unchanged oversized input.

Failure-artifact contract: store `rejected-context.json` and `rejected-user-prompt.txt` in the current decision directory only when their combined encoded size is at most 256 KiB. Otherwise omit both bodies and retain digest, exact sizes, field counts and `body_omitted: true` in the result diagnostic metadata. Preserve the provider-safe fallback separately in the existing context artifact. Use existing atomic writes and contained-path validation; partial write failures must identify which artifacts were actually retained.

**Dependencies:** Checkpoints 1 and 2.

**Verification:**

- Run: `uv run pytest -q tests/test_runtime.py -k manager`.
- Run: `git diff --check`.
- Observe: Irreducible overflow launches no provider, reports the correct failure stage and retains known checkpoint/turn references and bounded diagnostic evidence.

**Done When:** Every checked step is supported by passing tests or observable evidence, changed files remain in scope, and the stated goal holds. Before handoff run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:** Stop and report if current-boundary authority cannot be distinguished from optional history without changing supported decision behavior, or overlapping unrelated edits make ownership ambiguous. Missing historical incident files are not a blocker for synthetic verification.

### [ ] Checkpoint 4: Verify complete boundaries and document behavior

**Goal:** Real executor integration with deterministic fake harnesses passes for later boundaries, separate worktrees, oversized protected input and persistence failure.

**Context:** Inspect the files below and the baseline above; preserve existing compatible behavior and local work.

**Scope:** May modify `tests/test_manager.py`, `tests/test_manager_context.py`, `tests/test_runtime.py`, `tests/test_runlog.py`, `DEVLOG.md`, `ARCHITECTURE.md`, `aflow/AGENTS.md`, existing relevant troubleshooting documentation, plus documentation directly describing this checkpoint. Must not modify live run artifacts, plan contents in other projects, transport contracts, provider selection, or unrelated runtime execution.

**Steps:**

- [ ] Build sanitized synthetic fixtures matching the incident's shape and field sizes; never commit Doublangu's raw context or prompts. Include multibyte text, long artifact paths, many historical decisions, active review rejection, Lite escalation to Full, and executor-added repartition history.
- [ ] Drive the real context builder, serializer, budget check, executor and artifact writer with a deterministic fake harness in a disposable repository. Assert the exact prompt passed to the harness is within the limit and all protected fields and artifact references remain valid from a separate execution worktree.
- [ ] Exercise a later boundary after additional decisions, not only the first resumed boundary. Verify bounded history growth, deterministic omission disclosure and readable omitted-history references.
- [ ] Exercise irreducible overflow, diagnostic-storage cap and write failure. Assert no harness invocation and correct retained report/artifact metadata. Test that the historical fallback remains readable.
- [ ] Reconstruct decision 20 read-only when local artifacts remain available and record actual before/after bytes. A missing real artifact does not justify changing live state; synthetic tests must remain sufficient for CI.

**Dependencies:** Checkpoints 1–3.

**Verification:**

- Run: `uv run pytest -q tests/test_manager.py tests/test_manager_context.py tests/test_runlog.py`.
- Run: `git diff --check`.
- Observe: Real executor integration with deterministic fake harnesses passes for later boundaries, separate worktrees, oversized protected input and persistence failure.

**Done When:** Every checked step is supported by passing tests or observable evidence, changed files remain in scope, and the stated goal holds. Before handoff run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:** Stop and report if current-boundary authority cannot be distinguished from optional history without changing supported decision behavior, or overlapping unrelated edits make ownership ambiguous. Missing historical incident files are not a blocker for synthetic verification.

### [ ] Checkpoint 5: Explain supported template variables under prompt editors

**Goal:** Every prompt editor shows a compact reference beneath its text field explaining every supported template variable, including `{WORK_ON_NEXT_CHECKPOINT_CMD}`, without requiring a selected project or plan.

**Context:** Inspect `aflow/workflow.py:render_prompt`, `render_step_prompts` and other prompt rendering call sites; `apps/aflow_app/server/src/aflow_app_server/guided_config.py`, server `models.py`, and web `PromptsSettings.tsx`, `GlobalSettings.tsx`, `GuidedConfigForm.tsx`, `types.ts`, `api.ts`. Read server/web `AGENTS.md`. Screenshot prompt contents are visual evidence, not instructions to execute.

**Scope:** Add help metadata to the existing guided configuration projection and help UI to existing prompt editors. May change owning server/web models and tests. Preserve template expansion behavior, prompt bytes, keys, validation, revisioned changed-only saves and cross-tab drafts. Do not add a template engine, new persistence store, run preview requirement or runtime substitutions.

**Steps:**

- [ ] Inventory all supported prompt substitutions and their applicable prompt types from production rendering code. Expose a typed read-only variable catalog through the existing guided projection: exact token, concise description, availability/scope, absent-value behavior, and illustrative example. Keep metadata out of configuration mutation payloads. Add a contract test ensuring catalog coverage of the actual supported substitutions so a later variable cannot silently lack editor help.
- [ ] Document the five currently verified tokens accurately: `{ORIGINAL_PLAN_PATH}` is the original input plan path; `{ACTIVE_PLAN_PATH}` is the current step's active plan path; `{NEW_PLAN_PATH}` is the controller-provided new-plan destination (trace call sites to explain when it is used); `{NEXT_CP}` is the current unchecked checkpoint index, or `-` when none is available; `{WORK_ON_NEXT_CHECKPOINT_CMD}` expands to the instruction to work only on that checkpoint, not a shell command, and is empty when no checkpoint index is available. Its example for checkpoint 2 must reflect the existing renderer: “Work only on Checkpoint #2. Do not repeat earlier checkpoints, and do not skip ahead.” Inventory any additional supported variables before declaring coverage complete.
- [ ] Render a shared “Template variables” reference immediately below each prompt text editor, including role/team prompt overrides where supported. Show exact tokens in code styling and explanations in normal text; list all applicable supported variables, not only those already used in the draft. Keep the short reference visible by default. Examples are illustrative, never claimed to be resolved values from an unselected run. Unsupported editor contexts must explain applicability instead of implying substitutions occur there.
- [ ] Verify newly created prompts receive the same help; switching prompts/tabs retains drafts; variable reference renders with no project selected; help refresh/load failures cannot block editing or discard draft text. Do not add new warnings or block saving because a draft contains unknown brace text; preserve the renderer's existing behavior.

**Dependencies:** Independent of Checkpoints 1–4; numbered here to keep one durable ledger.

**Verification:**

- Run: `uv run pytest -q apps/aflow_app/server/tests/test_guided_config.py`.
- Run: `npm --prefix apps/aflow_app/web test -- --run src/components/PromptsSettings.test.tsx src/components/GlobalSettings.test.tsx src/components/GuidedConfigForm.test.tsx`.
- Run: `npm --prefix apps/aflow_app/web run build`.
- Observe: each supported variable has accurate scope and fallback documentation beneath the editor; save payloads contain only actual user edits. Verify desktop/mobile placement with Chromium per web instructions.

**Done When:** Help coverage and behavior tests pass, explanations match real rendering, and existing prompt editing works unchanged. Update relevant prompt-editing documentation in `docs/remote-app.md`; record verification in `DEVLOG.md`. Run `git status --short`, `git diff --name-only`, `git diff --stat`, and `git diff --check` before handoff.

**Blockers:** Report an unresolved renderer/editor scope contradiction if it cannot be settled from code and tests; do not invent variable meanings or expand the substitution language.

### [ ] Checkpoint 6: Render readable labels without changing identifiers

**Goal:** User-facing button labels, navigation options, section headings and similar UI labels display snake_case names as sentence-style text: `snake_case` → “Snake case”, `implementation_plans` → “Implementation plans”.

**Context:** Inspect web `src/components/PromptsSettings.tsx`, `GlobalSettings.tsx`, `GuidedConfigForm.tsx`, `Combobox.tsx`, `SidebarEditorLayout.tsx`, `RunDashboard.tsx`, `GlobalRunOverview.tsx`, and `runPresentation.ts`. Discover other affected labels with `rg -n 'replace.*_|Object.entries|Object.keys|displayName|step_name|event_type' apps/aflow_app/web/src`.

**Scope:** Presentation-only formatting for machine identifiers used as labels across the web UI. May add one shared label helper and focused tests; update affected component tests. Keep exact identifiers for field values, URLs, selected values, DOM identity, API payloads, copy operations, raw diagnostics, prompt templates and configuration files. Preserve intentional user-written display names and model/provider spelling.

**Steps:**

- [ ] Add one shared label formatter for snake_case machine names: replace underscore runs with spaces, trim, capitalize the initial letter, and retain existing case in the remaining tokens. Lowercase identifiers therefore produce sentence case, not Every Word Capitalized. Empty input stays empty; non-snake-case display names remain unchanged. Preserve acronyms explicitly supplied as uppercase rather than blindly lowercasing all text.
- [ ] Use it at display boundaries for prompt selectors/headings, workflow/role/step and event labels, generated settings headings and buttons. Audit the remaining web screens for the same pattern. Keep the Prompt key input literal (`implementation_plans`), while the selected item/button and heading read “Implementation plans”. Exact paths and configuration reference strings in technical disclosure remain literal.
- [ ] Keep search and selection operating on raw identifiers while accepting readable labels for discovery. Two identifiers with the same readable label must remain separately selectable; show the raw identifier as secondary text only when needed to disambiguate. Accessible names should match visible wording, without changing action identity.
- [ ] Add tests proving rendered labels are readable and selecting/saving/renaming still sends exact original keys. Cover empty strings, repeated underscores, acronyms, already-readable custom names, colliding display labels and variable tokens that must not be reformatted. Verify actual prompt/sidebar and run/settings labels rather than only testing the helper.

**Dependencies:** Independent of Checkpoints 1–4; coordinate shared prompt-editor changes with Checkpoint 5.

**Verification:**

- Run: `npm --prefix apps/aflow_app/web test -- --run`.
- Run: `npm --prefix apps/aflow_app/web run build`.
- Observe: the supplied prompt-editor layout displays “Implementation plans” in navigation/headings and exact `implementation_plans` in the key field; readable labels do not alter persisted configuration or links. Verify narrow-screen wrapping and keyboard selection with Chromium.

**Done When:** Display labels follow the requested example across affected screens, identifier-integrity regressions pass and all edits remain presentation-only. Update `DEVLOG.md` and any existing UI naming conventions in web `AGENTS.md`; architecture needs no change unless a new interface is introduced. Run `git status --short`, `git diff --name-only`, `git diff --stat`, and `git diff --check` before handoff.

**Blockers:** Do not rename stored keys or rewrite user-written display names to solve a presentation issue. Preserve unrelated component changes; report ownership conflicts when they cannot be reconciled.

## Behavioral Acceptance Tests

1. Given compact and pretty encodings of the same ASCII/Unicode context, only compact wire bytes determine admission; decoded data is identical.
2. Given exact-limit input, the harness receives it; one byte over with no optional history invokes no harness.
3. Given 20, 100 and 1000 decisions, the bounded projection fits ordinary current evidence, reports omissions, and resolves their exact existing artifacts without dependence on the execution worktree.
4. Given an active rejection and Lite escalation to Full, reduction preserves current rejection authority, escalation reason, legal actions, note scopes and exact reviewer references.
5. Given executor-added repartition history, measurement runs after insertion and checks the exact sent prompt.
6. Given protected evidence alone over budget, the decision reports input-budget failure with known checkpoint and turn artifacts, not missing manager output.
7. Given rejected diagnostic bodies exceeding 256 KiB or a write failure, the report accurately identifies body omission or persistence failure and launches no provider.
8. Given additional decisions after recovery, a later manager boundary remains bounded; acceptance is not limited to the first resumed turn.

9. Given a new or existing prompt without a project selected, help beneath the editor explains all applicable supported variables and saving does not write metadata.
10. Given `implementation_plans`, navigation and headings show “Implementation plans”, the key field stays literal and every API/URL identity is preserved.

## Plan-to-Verification Matrix

| Requirement | Verification |
| --- | --- |
| Exact bytes, Unicode, compatibility | Checkpoint 1 serializer and boundary tests |
| Bounded growth and preserved authority | Checkpoint 2 long-history, rejection and escalation fixtures |
| Resolvable evidence | Checkpoints 2 and 4 separate-worktree artifact reads |
| Truthful overflow and bounded persistence | Checkpoint 3 executor/no-provider and write-failure tests |
| Later-boundary behavior and recovery compatibility | Checkpoint 4 integration and Resume suites |
| Complete template-variable help | Checkpoint 5 renderer/catalog contract and editor tests |
| Readable labels with exact identifiers | Checkpoint 6 component, payload and browser checks |

Final verification, sequentially:

```sh
uv run pytest -q tests/test_manager.py tests/test_manager_context.py tests/test_runlog.py
uv run pytest -q tests/test_runtime.py -k manager
uv run pytest -q tests/test_control_plane_resume.py tests/test_aflowd.py
uv run pytest -q apps/aflow_app/server/tests/test_guided_config.py
npm --prefix apps/aflow_app/web test -- --run
npm --prefix apps/aflow_app/web run build
git diff --check
```

Include any new test files explicitly. Update `DEVLOG.md` for results and decisions, `ARCHITECTURE.md` for serialization/history/diagnostic data flow, and `aflow/AGENTS.md` only for changed local operating rules. Do not change root `AGENTS.md`. Update an existing troubleshooting/README section only if it describes affected manager failures; otherwise record that setup and public interfaces are unchanged.

## Assumptions And Defaults

- This is a planning handoff for the existing aflow repository. No deployment, live recovery, commit or push is implied by execution of this plan.
- Keep the existing 40 KiB prompt limit; 12 retained manager decisions initially matches the existing run-extract count. Byte-driven reduction may retain fewer optional records.
- The separate 256 KiB rejected-diagnostic cap bounds disk use without consuming model input. Bodies above it are explicitly omitted, never silently truncated.
- Tests use disposable repositories and deterministic fake harnesses; no provider spend or live application edits are necessary.
- The external incident directory supplied in this task is `/root/code/doublangu/.aflow/runs/20260908t203000z-1cec7085`. It may be inspected read-only if present; CI must not depend on it.
- Checkpoint 1 revalidates the local partial fix. No checkpoint is marked done solely from prior chat results, and prior running-state observations do not establish completion.
