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
