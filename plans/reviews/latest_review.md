# Issue 4 — final cumulative review, 2026-09-11

Original plan: `plans/in-progress/issue-4-deterministic-guard-reports-20260909.md`.
Active overlay reviewed: `issue-4-deterministic-guard-reports-20260909-cp01-v02.md`.
Branch: `aflow-issue-4-deterministic-guard-reports-20260909-20260911-102356`.
Unchanged Pre-Handoff Base HEAD: `fcfb7ec4450165682d1b031e3e20809821315442`.
Reviewed implementation HEAD: `e01b9c0d58a2cf626311ebf9f467ae526db60e8c`.
Coverage: **1 new / 5 total commits**, CP1 v01 `c4994757`, CP2 v01 `e792dd4b`, CP3 v01 `4ed22753`, follow-up v01 `d7b33012`, follow-up cp01 v02 `e01b9c0d`.

Read the active overlay and prior findings first, verified their disposition, then reviewed the complete original-base-through-HEAD implementation across all 21 changed files and directly affected canonical/snapshot contracts. Applied material-code-review admission, exclusions and proportionate-fix discipline; the explicit AFlow contract governs finalization.

## Previous findings

- F1 resolved: unmodified schema-1 canonical get_run responses pass through explicit pinned CLI metadata to the renderer; wrong run/ownership are rejected.
- F2 resolved: compact results require terminal status and an aware finished_at, reject explicit identity conflicts, and exclude starting worker evidence.
- F3 resolved: repeated legacy orphan reports retain duplicate-operation risk without repeating cause inspection.
- F4 resolved: failed/incomplete/needs_attention reports request bounded owner inspection and a decision.
- F5 resolved: line-limit truncation always shows an ellipsis and preserves bounded JSON.
- F6 resolved: header and checkpoint fields have disjoint drawn extents; retained layout evidence remains valid.
- F7 resolved in e01b9c0d: real project_activity startup/input/override projections request the required owner action through the existing owning interface; healthy/success controls remain sensible and unknown states no longer assert no action.
- F8 resolved in e01b9c0d: the canonical running-metadata/unit_missing condition retains unresolved duplicate-operation risk and explicit no-blind-relaunch guidance without inventing a cause; known terminal provider evidence remains distinct.

No unresolved previous findings. No new material findings.

## Verification

- `uv run --with pillow pytest -q tests/test_guard_report_input.py tests/test_guard_report_routing.py --basetemp /root/code/evidence/aflow-dogfood-20260909/issue4-review-20260911/final-review-pytest`: **29 passed in 5.26s**. Includes real canonical projections, canonical/legacy CLI-to-PNG journeys, repeat PNG/JSON bytes, identity rejection, and repository nonmutation.
- `uv run ruff check aflow`, `git diff --check fcfb7ec4 HEAD`, and working diff check passed.
- Reused valid prior 40-test renderer/snapshot/ownership/install/refresh evidence and CP3 edited-tree preservation checks recorded in the previous review and `docs/reviews/issue-4-deterministic-guard-reports-cp3.md`. The final fix does not change renderer, snapshot, installer or refresh code. CI owns full suites.
- Visually inspected retained fix-v02 startup-answer and missing-unit PNGs: readable owner action, visible duplicate risk, correct unknown cause, no overlap. Current renderer bytes independently match retained startup-answer, missing-unit, long-field and failed PNGs; each retained repeated PNG/JSON pair matches exactly.
- Retained current PNG SHA-256: startup-answer `ad3edf7e6e54aad586be46f28a0a4f8f9aaed85edd498cbe26d043aa9f066815`; missing-unit `241b44f13f5c9b12471db36fd3f852f5813111c974a0365131002b4b9dc8ecce`.
- Private evidence remains external under the supplied issue4 evidence root. Portable tests use tmp_path; no private test paths were committed.
- Prompt-supplied worktree-relative turn-004/result.json was absent. Review used committed source/history, retained evidence and independent checks without altering run lineage or durable state.

## Approval and delivery boundary

Approved for one final accumulated handoff commit after the unchanged base, covering all five implementation/fix commits. Finalization includes this already-tracked reviewer record and one compact DEVLOG entry; the prior review is archived byte-for-byte without force-adding private artifacts. The superseded fix overlay is removed. No empty fix plan is created.

The original plan stays in place for engine finalization and records the final approved SHA after commit creation. Final verification must establish exactly one commit after the base, unchanged implementation blobs, and no remaining tracked reviewer edits. Publication to origin/main, exact-SHA CI and live activation belong to the existing controller/coordinator delivery gate and are not claimed by this local review. Publication configuration is origin/main. Unrelated integration work and active execution lineage are preserved.

No material findings

---

# Issue 39 — Cumulative squash approval

Original/active plan: `plans/in-progress/issue-39-manual-acceptance-handoff-20260911.md`.
Branch: `aflow-issue-39-manual-acceptance-handoff-20260911-20260911-151803`.
Unchanged Pre-Handoff Base HEAD: `db97b15b32802b59dc8badeb8a6498494ceb9e62`.
Reviewed HEAD: `59679819fe1e0edcdb58a9cf0593cf220dfd8200`.
Coverage: 1 new / 1 total commit, `cp1 v01`, all checkpoint 1 requirements and all nine changed files. No previous findings or fix overlays belong to this handoff; the prior latest review concerns issue 40.

## Findings

No material findings under the finding admission gate, exclusions, and proportionate-fix discipline. Bundled planning guidance separates agent-owned tasks from ordinary-bullet owner acceptance under a level-two heading after checkpoints. Execution/review guidance prevents retries for pending owner-only checks while preserving implementation defects, required automated verification and explicit approval/release gates. Handoffs must report both statuses and require owner evidence before claiming acceptance. README, CLI documentation and the single DEVLOG entry agree. Parser, controller and account-local skills are unchanged.

## Verification evidence

Reused successful completed command evidence from the parent repository's `.aflow/runs/20260911t151803z-d3ccdf43/turns/turn-001/transport.stdout` and completed `result.json`; relevant implementation files did not change after these checks:

```sh
uv run pytest -q tests/test_skill_install.py::test_manual_destination_links_default_skills_into_the_store
# 1 passed, exit 0
uv run pytest -q tests/test_plan.py::PlanParserTests::test_parser_global_section_after_last_checkpoint_does_not_affect_completion tests/test_plan.py::PlanParserTests::test_parser_non_checkpoint_heading_ends_step_counting
# 2 passed, exit 0
uv run pytest -q tests/test_docs.py::SkillDocsTests::test_plan_skill_uses_semantic_checkpoint_shaping_without_hard_stop tests/test_docs.py::SkillDocsTests::test_bundled_skills_keep_plan_worker_facing_and_commit_ownership_in_execution_roles
# 2 passed, exit 0
git diff --check db97b15b32802b59dc8badeb8a6498494ceb9e62 HEAD
# independently passed
```

Inspected existing parser boundary regressions and isolated installation assertions, including packaged aflow-plan byte equality. No duplicate parser regression is needed. Worker probe is retained at `/root/code/evidence/aflow-dogfood-20260909/issue39-review-20260911/representative-parse-probe.txt`. An independent `uv run python` probe, retained as `reviewer-parser-probe.txt` in the same external root, uses an exact automated test command and physical phone Settings acceptance actions. It confirms incomplete before, complete after checking only the automated checkpoint/task, byte-identical pending manual text, and rejection of a checked checkpoint with an unchecked automated step. The probe checks parsing, not physical-device acceptance. No full suites run; CI owns those. Evidence paths occur only in invocation-owned artifacts, never portable test code.

## Disposition and handoff

Approved for one unpublished accumulated commit after the unchanged base, including this already-tracked reviewer record. Preserve all nine reviewed implementation blobs. DEVLOG already contains one issue-39 entry. No fix plan is needed and no stale issue-39 fix plans exist. Preserve the ignored original plan for engine finalization and record the final approved SHA there after committing. Finalization checks require one commit after base, unchanged reviewed blobs, and clean tracked state.

Publication, exact-SHA CI, live activation and Settings refresh/install remain engine/coordinator delivery work. After reviewed deployment, the coordinator must use current Settings to refresh/install the bundled skill while preserving owner edits; verify the effective aflow-plan guidance includes User Acceptance Pending. Account-local skills were not changed by this review. Owner-only checks in generated plans remain pending until their named owner records actual evidence; the retained phone scenario is an example, not a claimed pass.

Prior latest review rotated byte-for-byte to `plans/reviews/260911_1531.md`. No ignored private archive, plan or evidence is force-added.

No material findings
