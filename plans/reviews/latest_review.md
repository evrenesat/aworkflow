# Issue 36 — cumulative approval review v05, 2026-09-11

Review base: `fbdd438f3e81c5bccea44960deeb213eac32d9ca` (unchanged Pre-Handoff Base HEAD).
Reviewed implementation HEAD: `8d866e023bcd8fcda2e336eab83a446ffe7e3a8a`.
Coverage: **1 new / 8 total commits**, all 31 implementation files, all four original checkpoints and every follow-up: cp1 v01 `ed74914a`, cp2 v01 `c9bd258e`, cp3 v01 `87baefed`, cp4 v01 `40dca3ee`, cp4 v02 `28dd2a4d`, cp4 v03 `86846302`, cp4 v04 `14867c09`, cp4 v05 `8d866e02`.

Read the original plan, active cp01-v04 overlay and prior findings before reviewing the complete base-to-HEAD implementation. Applied the material finding admission gate, exclusions and proportionate-fix discipline. No material findings remain.

## Previous findings

- F1 resolved: admission and worker preparation share normalized Markdown fingerprint inputs, retaining exact schema-v2 scope artifact digests and worktree checks.
- F2 resolved: unresolved/malformed recovery operations reject at explicit and ordinary admission, canonical reconstruction, prelaunch replay and worker preparation. Exact already-started replay remains idempotent.
- F3 resolved: replacement status requires operation/session evidence for the selected successor and worker. Retained browser journeys invoke worker_main and the canonical controller with fake providers and a registered worktree; controller launch alone does not establish session startup.
- F4 resolved: bounded typed admission reasons propagate through REST/MCP, and rejected UI drafts retain their selection.
- F5 resolved: ordinary continuation rejects pending recovery at all four continuation boundaries; explicit initial preparation and pending review retain the brief and guard.
- F6 resolved by cp4 v05: HarnessSessionRefV1 is validated and installed before the first consumed write. One atomic run.json snapshot contains both consumption and the exact session. The real-writer crash-boundary regression confirms pre-write reconstruction rejects in-flight state and post-write canonical continuation reuses the exact replacement session without a source call.

## Cumulative assessment

CP1 preserves explicit-only intent, current target configuration, inactive ownership, exact evidence, idempotency and successor-owned provenance. CP2 supplies bounded durable context to a fresh worker session, preserves review ordering and lineage, and guards the first provider operation. CP3 routes REST/MCP through the same continuation service and preserves authentication and ordinary payload compatibility. CP4 retains primary ordinary Resume, explicit replacement selection, actionable rejection and truthful context-loss/session status. No automatic source inference or provider-pair fallback was introduced. Existing backup wrapper interfaces, metadata normalization and semantic marker handling remain compatible.

## Verification

Independently executed on the reviewed implementation:

```text
uv run pytest -q tests/test_durable_provider_recovery.py tests/test_durable_provider_recovery_runtime.py tests/test_hotplug.py::test_same_harness_native_resume_uses_exact_session_id tests/test_hotplug.py::test_live_run_workflow_consumes_semantic_session_output tests/test_hotplug.py::test_live_run_workflow_activates_target_after_source_and_resumes_exact_session
```

Result: **43 passed** (40 recovery tests and 3 affected session regressions), including `test_recovery_consumption_and_session_identity_share_one_durable_snapshot`.

`uv run ruff check aflow` and `git diff --check fbdd438f HEAD` passed.

Reused retained unaffected evidence: ordinary resume/relocation/current-config/hotplug/session checks; 11 focused transport cases from review v04; 121 web component/API tests, production build, and one real fake-provider journey each in Chromium and WebKit from cp4 v02. The latest fix changes only session persistence ordering, its regression and documentation, so transport/browser repetition was unnecessary. Full suites remain CI-owned. No live providers, shared runtime changes, historical-run mutations, public posts or issue closure.

## Approval finalization

Approved for the single unpublished handoff commit anchored at the unchanged base. Compact the handoff DEVLOG entries and include this intentional tracked reviewer record in that commit. Preserve every other reviewed implementation blob. Verify exactly one commit after the base and no tracked edits outside it. The final SHA belongs in original-plan tracking and private finalization evidence, avoiding a self-referential follow-up commit.

The original plan stays in-progress with completed checkpoints for engine finalization. The resolved cp01-v04 overlay is preserved byte-for-byte in private evidence and removed from in-progress; no new fix plan is needed. Previous latest review rotated unchanged to `plans/reviews/260911_1347.md`. Ignored private plans, review archives and evidence are not force-added. Evidence root: `/root/code/evidence/aflow-dogfood-20260909/issue36-review-20260911/`.

Merge, origin/main publication, exact-SHA CI and live activation are subsequent controller-owned delivery gates, not claimed by this review approval.

No material findings

---

# JSONL record boundaries cumulative review - 2026-09-11

Original and active plan: `plans/in-progress/jsonl-record-boundaries-20260911.md`.
Branch: `aflow-jsonl-record-boundaries-20260911-20260911-122140`.
Unchanged Pre-Handoff Base HEAD: `fcfb7ec4450165682d1b031e3e20809821315442`.
Reviewed HEAD: `4c8416822f571af011282b97354b031899332ee4`.
Coverage: **1 new / 1 total commit**, `cp1 v01`, all checkpoint 1 requirements
and the complete cumulative diff in session.py, reasonix.py, tests and DEVLOG.

## Previous findings

None for this handoff; no follow-up overlay exists. The previous latest report
concerns other handoffs and was archived byte-for-byte.

## Findings and evidence

No admitted material findings. Applied the material-code-review admission gate,
exclusions and proportionate-fix discipline. All three equivalent framing sites
split only on LF. JSON parsing, object validation, blank-record handling, order,
physical record diagnostics, identity checks and assistant/tool selection retain
their established behavior. CRLF whitespace is accepted by json.loads; Unicode
string characters remain payload data. No production code was edited in review.

Independently ran 22 focused cases in tests/test_harness_sessions.py: all passed
in 0.25s. Exact pytest node suffixes (each prefixed by
`tests/test_harness_sessions.py::`, invoked with `uv run pytest -q`):

- test_jsonl_parser_keeps_wire_events_separate
- test_jsonl_parser_preserves_unicode_string_data_and_lf_framing
- test_jsonl_parser_rejects_invalid_records_at_physical_lf_line
- test_codex_structured_output_preserves_unicode_assistant_text_and_semantics
- test_reasonix_acp_jsonl_preserves_unicode_string_data
- test_codex_structured_output_extracts_one_session_and_final_response
- test_codex_structured_output_excludes_tools_and_echoed_prompts
- test_codex_structured_assistant_stop_remains_terminal_marker
- test_codex_structured_output_requires_assistant_final_and_preserves_failure
- test_structured_output_rejects_missing_ambiguous_or_malformed_identity
- test_reasonix_acp_parses_open_prompt_streams_and_final_output
- test_reasonix_acp_rejects_mismatched_error_or_malformed_wire

`uv run ruff check aflow`, `git diff --check` and the cumulative
`git diff --check fcfb7ec4450165682d1b031e3e20809821315442 HEAD` passed.
Reused the authorized retained read-only capture proof at
`/root/code/evidence/aflow-dogfood-20260909/jsonl-boundary-review-20260911/capture-proof.md`:
182 and 522 LF records parse, each capture contains one NEL, final-result
contracts pass and source bytes remain unchanged. Synthetic tests additionally
cover U+2028/U+2029, LF/CRLF, trailing empty records, actual malformed JSON,
nonobjects, exact final text and exclusion of tool-only semantic controls.
No whole suites or controller/recovery mutations; CI owns full suites.

## Disposition

Approved for one unpublished final handoff commit after the unchanged base,
including this tracked report and preserving all reviewed implementation blobs.
DEVLOG already has one handoff entry. No fix plan is needed or stale overlay
present. Original plan remains for engine finalization; final approved SHA and
post-commit checks are recorded there without a self-referential amendment.
Private plans, archive and external proof are not force-added. Require exactly
one final commit and no tracked edits outside it. Managed controller/coordinator
merge, origin/main publication, exact-SHA CI and live activation remain separate
pending delivery gates.

No material findings
