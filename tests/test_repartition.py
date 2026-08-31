"""Tests for aflow.repartition — Checkpoint 1 primitives."""

from tests._support import *  # noqa: F401,F403

import json
import hashlib
import re
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from aflow.repartition import (
    CheckpointSourceSlice,
    DriftValidationResult,
    MechanicalValidationResult,
    PartitionSpecV1,
    RepartitionProposalV1,
    RepartitionVerdictV1,
    ScopeEnvelopeV1,
    SourceBlock,
    create_envelope,
    derive_generation_id,
    derive_partition_ids,
    extract_source_blocks,
    parse_proposal_json,
    parse_verdict_json,
    read_envelope,
    repartition_proposal_sha256,
    render_candidate_plan,
    slice_checkpoint_source,
    validate_candidate_mechanically,
    validate_envelope,
    validate_envelope_boundary_drift,
    write_envelope_atomic,
)

_EMPTY_MAPPING: Mapping[str, str] = {}


def test_repartition_identities_ignore_generated_wording() -> None:
    fixture = ReviewRejectionRegressionTests()
    envelope, proposal = fixture._envelope_and_proposal(fixture._plan())
    generation = derive_generation_id(
        scope_id=envelope.scope_id,
        decision_number=7,
        envelope_sha256=envelope.canonical_envelope_sha256,
        source_plan_sha256=envelope.plan_sha256,
    )
    rewritten = replace(
        proposal,
        rationale="Different wording.",
        children=tuple(
            replace(child, title=f"Renamed {index}", narrow_goal="Reworded.")
            for index, child in enumerate(proposal.children, start=1)
        ),
    )

    assert derive_partition_ids(
        generation_id=generation, proposal=proposal,
    ) == derive_partition_ids(generation_id=generation, proposal=rewritten)
    assert repartition_proposal_sha256(proposal) != repartition_proposal_sha256(rewritten)


def test_render_and_validate_use_allowed_boundary_source_offsets() -> None:
    fixture = ReviewRejectionRegressionTests()
    opening = fixture._plan(git_tracking=True)
    envelope, proposal = fixture._envelope_and_proposal(opening)
    boundary = opening.replace(
        "- Plan Branch: `a`", "- Plan Branch: `a-much-longer-controller-value`"
    )
    proposal = replace(
        proposal,
        source_plan_sha256=hashlib.sha256(boundary.encode("utf-8")).hexdigest(),
    )
    candidate = render_candidate_plan(
        envelope=envelope,
        proposal=proposal,
        source_plan_text=boundary,
        generation_id="generation-boundary",
        partition_ids=("partition-a", "partition-b"),
        repair_evidence_artifact_references={},
    )
    result = validate_candidate_mechanically(
        source_plan_text=boundary,
        candidate_plan_text=candidate,
        envelope=envelope,
        proposal=proposal,
        repair_evidence_artifact_references={},
        expected_generation_id="generation-boundary",
        expected_partition_ids=("partition-a", "partition-b"),
    )

    assert result.valid, result.issues
    assert "- Plan Branch: `a-much-longer-controller-value`" in candidate


# ---------------------------------------------------------------------------
# Source-slice tests
# ---------------------------------------------------------------------------

class SourceSliceTests(unittest.TestCase):

    def test_slice_finds_first_checkpoint(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First
            - [ ] step one

            ### [ ] Checkpoint 2: Second
            - [ ] step two
        """)
        result = slice_checkpoint_source(text, checkpoint_index=1)
        assert result is not None
        assert result.checkpoint_name == "Checkpoint 1: First"
        assert result.checkpoint_index == 1
        assert "step one" in result.full_text
        assert "step two" not in result.full_text
        assert result.heading_prefix.startswith("### [ ] Checkpoint 1: First")

    def test_slice_by_name(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First
            - [ ] step one

            ### [ ] Checkpoint 2: Second
            - [ ] step two
        """)
        result = slice_checkpoint_source(text, checkpoint_name="Checkpoint 2: Second")
        assert result is not None
        assert result.checkpoint_name == "Checkpoint 2: Second"
        assert "step two" in result.full_text

    def test_slice_returns_none_for_missing(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First
            - [ ] step one
        """)
        assert slice_checkpoint_source(text, checkpoint_index=99) is None
        assert slice_checkpoint_source(text, checkpoint_name="Nonexistent") is None

    def test_slice_ignores_checkpoints_inside_fences(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: Real
            - [ ] step one

            ```
            ### [ ] Checkpoint 2: Fake
            - [ ] fake step
            ```

            ### [ ] Checkpoint 2: Real Second
            - [ ] step two
        """)
        result = slice_checkpoint_source(text, checkpoint_index=2)
        assert result is not None
        assert result.checkpoint_name == "Checkpoint 2: Real Second"

    def test_slice_preserves_exact_bytes(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First
            - [ ] step one

            ### [ ] Checkpoint 2: Second
            - [ ] step two
        """)
        result = slice_checkpoint_source(text, checkpoint_index=1)
        assert result is not None
        plan_bytes = text.encode("utf-8")
        sliced = plan_bytes[result.checkpoint_byte_start:result.checkpoint_byte_end]
        assert sliced.decode("utf-8") == result.full_text

    def test_slice_with_unicode(self) -> None:
        text = "# Plan\n\n### [ ] Checkpoint 1: Café\n- [ ] étape une 🎉\n"
        result = slice_checkpoint_source(text, checkpoint_index=1)
        assert result is not None
        assert "Café" in result.checkpoint_name
        assert "🎉" in result.full_text

    def test_slice_last_checkpoint_to_end(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [x] Checkpoint 1: Done
            - [x] step one

            ### [ ] Checkpoint 2: Last
            - [ ] step two

            ## Final Notes
            More text here.
        """)
        result = slice_checkpoint_source(text, checkpoint_index=2)
        assert result is not None
        assert "step two" in result.full_text
        # The "## Final Notes" section should NOT be included
        assert "Final Notes" not in result.full_text

    def test_slice_heading_prefix_includes_blank_lines(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First


            - [ ] step one
        """)
        result = slice_checkpoint_source(text, checkpoint_index=1)
        assert result is not None
        assert result.heading_prefix.strip() == "### [ ] Checkpoint 1: First"
        # Blank lines after heading should be in heading_prefix
        assert result.heading_prefix.endswith("\n\n")


# ---------------------------------------------------------------------------
# Source-block extraction tests
# ---------------------------------------------------------------------------

class SourceBlockExtractionTests(unittest.TestCase):

    def _make_slice(self, plan_text: str, checkpoint_index: int = 1) -> CheckpointSourceSlice:
        result = slice_checkpoint_source(plan_text, checkpoint_index=checkpoint_index)
        assert result is not None
        return result

    def test_extracts_bold_sections(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:**

            Do something important.

            **Steps:**

            - [ ] step one
            - [ ] step two

            **Done When:**

            Tests pass.
        """)
        sl = self._make_slice(text)
        blocks = extract_source_blocks(sl, envelope_checkpoint_sha256="a" * 64, plan_text=text)
        assert len(blocks) >= 3
        labels = [b.section_label for b in blocks]
        assert "**Goal:**" in labels
        assert "**Steps:**" in labels
        assert "**Done When:**" in labels

    def test_extracts_list_items(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Steps:**

            - [ ] step one
            - [ ] step two
            - [ ] step three
        """)
        sl = self._make_slice(text)
        blocks = extract_source_blocks(sl, envelope_checkpoint_sha256="b" * 64, plan_text=text)
        # Should have at least the bold section + individual list items
        assert len(blocks) >= 2

    def test_extraction_preserves_fenced_content(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Context:**

            ```
            def foo():
                pass
            ```

            **Steps:**

            - [ ] implement foo
        """)
        sl = self._make_slice(text)
        blocks = extract_source_blocks(sl, envelope_checkpoint_sha256="c" * 64, plan_text=text)
        assert any("def foo():" in b.text for b in blocks)

    def test_blocks_concatenate_to_body(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:**

            Build feature X with these constraints.

            **Steps:**

            - [ ] create module
            - [ ] add tests

            **Verification:**

            - Run: `pytest`

            **Done When:**

            All tests pass.
        """)
        sl = self._make_slice(text)
        blocks = extract_source_blocks(sl, envelope_checkpoint_sha256="d" * 64, plan_text=text)
        reconstructed = "".join(b.text for b in blocks)
        assert reconstructed == sl.body_text

    def test_block_ids_are_unique(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **A:**

            Content A.

            **B:**

            Content B.

            **C:**

            Content C.
        """)
        sl = self._make_slice(text)
        blocks = extract_source_blocks(sl, envelope_checkpoint_sha256="e" * 64, plan_text=text)
        ids = [b.block_id for b in blocks]
        assert len(ids) == len(set(ids))

    def test_block_positions_are_absolute(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:**

            Do something.
        """)
        sl = self._make_slice(text)
        blocks = extract_source_blocks(sl, envelope_checkpoint_sha256="f" * 64, plan_text=text)
        assert len(blocks) > 0
        plan_bytes = text.encode("utf-8")
        for b in blocks:
            assert b.byte_start >= 0
            assert b.byte_end <= len(plan_bytes)
            block_bytes = plan_bytes[b.byte_start:b.byte_end]
            assert block_bytes.decode("utf-8") == b.text

    def test_multiline_list_items_stay_together(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            - [ ] step one with
              continuation line
            - [ ] step two
        """)
        sl = self._make_slice(text)
        blocks = extract_source_blocks(sl, envelope_checkpoint_sha256="g" * 64, plan_text=text)
        # The two list items should be separate blocks
        assert len(blocks) >= 2

    def test_unknown_formatting_is_conservative_block(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            Plain prose paragraph that has no bold label
            or list item marker.

            **Next Section:**

            Content.
        """)
        sl = self._make_slice(text)
        blocks = extract_source_blocks(sl, envelope_checkpoint_sha256="h" * 64, plan_text=text)
        assert len(blocks) >= 2
        assert any("Plain prose" in b.text for b in blocks)

    def test_summary_block_cannot_replace_coverage(self) -> None:
        """A summary text alone cannot satisfy source block coverage."""
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:**

            Build feature X.

            **Steps:**

            - [ ] create module
            - [ ] add tests
        """)
        sl = self._make_slice(text)
        blocks = extract_source_blocks(sl, envelope_checkpoint_sha256="i" * 64, plan_text=text)
        # Verify that blocks capture exact text, not summaries
        assert len(blocks) >= 3  # Goal + at least 2 list items
        goal_block = next(b for b in blocks if b.section_label == "**Goal:**")
        assert "Build feature X" in goal_block.text
        # The block text IS the exact source, not a summary
        assert goal_block.text.strip() != "Goal: Build feature X"  # must include the bold label


# ---------------------------------------------------------------------------
# Envelope tests
# ---------------------------------------------------------------------------

class EnvelopeTests(unittest.TestCase):

    def test_create_envelope_normalizes_absolute_and_relative_plan_paths(self) -> None:
        text = "# Plan\r\n\r\n### [ ] Checkpoint 1: Café\r\n- [ ] preserve bytes 🎉\r\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plans" / "plan.md"
            plan_path.parent.mkdir()
            plan_path.write_bytes(text.encode("utf-8"))
            absolute = create_envelope(
                scope_id="scope-paths",
                original_plan_path=plan_path,
                plan_text=text,
                checkpoint_index=1,
                repo_root=repo_root,
            )
            relative = create_envelope(
                scope_id="scope-paths",
                original_plan_path="plans/plan.md",
                plan_text=text,
                checkpoint_index=1,
                repo_root=repo_root,
            )
            assert absolute.original_plan_path == "plans/plan.md"
            assert relative.original_plan_path == "plans/plan.md"
            assert absolute.plan_bytes_b64 == relative.plan_bytes_b64
            with pytest.raises(ValueError, match="inside repository"):
                create_envelope(
                    scope_id="scope-paths",
                    original_plan_path=repo_root.parent / "outside.md",
                    plan_text=text,
                    checkpoint_index=1,
                    repo_root=repo_root,
                )

    def test_create_envelope_basic(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:**

            Do something.
        """)
        envelope = create_envelope(
            scope_id="scope-abc",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        assert envelope.scope_id == "scope-abc"
        assert envelope.schema_version == 1
        assert envelope.checkpoint_name == "Checkpoint 1: First"
        assert envelope.plan_sha256
        assert envelope.canonical_envelope_sha256
        assert len(envelope.source_blocks) > 0

    def test_envelope_validation_passes_valid(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:**

            Do something.
        """)
        envelope = create_envelope(
            scope_id="scope-xyz",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        issues = validate_envelope(envelope)
        assert issues == [], f"Unexpected issues: {issues}"

    def test_envelope_scope_digest(self) -> None:
        import hashlib
        scope_id = "my-scope-id"
        expected_digest = hashlib.sha256(scope_id.encode("utf-8")).hexdigest()

        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:** test
        """)
        envelope = create_envelope(
            scope_id=scope_id,
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        assert envelope.scope_digest == expected_digest

    def test_envelope_round_trip_json(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:**

            Do something.

            **Steps:**

            - [ ] step one
        """)
        envelope = create_envelope(
            scope_id="scope-roundtrip",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        d = envelope.to_dict()
        d["canonical_envelope_sha256"] = envelope.canonical_envelope_sha256
        restored = ScopeEnvelopeV1.from_dict(d)
        assert restored.scope_id == envelope.scope_id
        assert restored.canonical_envelope_sha256 == envelope.canonical_envelope_sha256
        assert restored.plan_sha256 == envelope.plan_sha256
        assert len(restored.source_blocks) == len(envelope.source_blocks)

    def test_write_and_read_envelope(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:** test
        """)
        envelope = create_envelope(
            scope_id="scope-io",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "scopes" / envelope.scope_digest
            path = write_envelope_atomic(envelope, artifact_dir)
            assert path.is_file()

            loaded = read_envelope(path)
            assert loaded is not None
            assert loaded.scope_id == envelope.scope_id
            assert loaded.canonical_envelope_sha256 == envelope.canonical_envelope_sha256

    def test_read_envelope_returns_none_for_missing(self) -> None:
        result = read_envelope(Path("/nonexistent/envelope.json"))
        assert result is None

    def test_envelope_with_nested_fences(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Example:**

            ```python
            def nested():
                ```
                this is not a real fence close
                ```
                pass
            ```
        """)
        envelope = create_envelope(
            scope_id="scope-fence",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        issues = validate_envelope(envelope)
        assert issues == [], f"Unexpected issues: {issues}"

    def test_envelope_with_unicode_and_emoji(self) -> None:
        text = "# Plan\n\n### [ ] Checkpoint 1: Café 🎉\n\n**Goal:** résumé ✅\n"
        envelope = create_envelope(
            scope_id="scope-unicode",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        issues = validate_envelope(envelope)
        assert issues == []


# ---------------------------------------------------------------------------
# Proposal parsing tests
# ---------------------------------------------------------------------------

class ProposalParsingTests(unittest.TestCase):

    def _valid_proposal_json(self) -> str:
        return json.dumps({
            "schema_version": 1,
            "envelope_sha256": "a" * 64,
            "source_plan_sha256": "b" * 64,
            "rationale": "Splitting because X is too large",
            "children": [
                {
                    "title": "Part 1: Core",
                    "narrow_goal": "Implement core logic",
                    "source_block_ids": ["abcdef01-b000"],
                    "implementation_steps": ["create module"],
                    "verification_commands": ["pytest -q"],
                    "done_criteria": ["Tests pass"],
                    "repair_evidence_ids": [],
                },
                {
                    "title": "Part 2: Integration",
                    "narrow_goal": "Integrate with system",
                    "source_block_ids": ["abcdef01-b001"],
                    "implementation_steps": ["wire up"],
                    "verification_commands": ["pytest -q"],
                    "done_criteria": ["Integration works"],
                    "repair_evidence_ids": [],
                },
              ],
              "current_disposition": "review_current_partition",
              "cross_cutting_source_reasons": {},
          })

    def _parse(self, json_text: str) -> RepartitionProposalV1:
        return parse_proposal_json(
            json_text,
            expected_envelope_sha256="a" * 64,
            expected_source_plan_sha256="b" * 64,
            valid_source_block_ids={"abcdef01-b000", "abcdef01-b001"},
            valid_repair_evidence_ids=set(),
        )

    def test_parses_valid_proposal(self) -> None:
        proposal = self._parse(self._valid_proposal_json())
        assert proposal.schema_version == 1
        assert len(proposal.children) == 2
        assert proposal.children[0].title == "Part 1: Core"
        assert proposal.current_disposition == "review_current_partition"

    def test_rejects_missing_schema_version(self) -> None:
        data = json.loads(self._valid_proposal_json())
        del data["schema_version"]
        with pytest.raises(ValueError, match="schema_version"):
            self._parse(json.dumps(data))

    def test_rejects_wrong_schema_version(self) -> None:
        data = json.loads(self._valid_proposal_json())
        data["schema_version"] = 99
        with pytest.raises(ValueError, match="schema_version must be 1"):
            self._parse(json.dumps(data))

    def test_rejects_unknown_field(self) -> None:
        data = json.loads(self._valid_proposal_json())
        data["extra_unknown_field"] = "bad"
        with pytest.raises(ValueError, match="Unknown field"):
            self._parse(json.dumps(data))

    def test_rejects_invalid_hash(self) -> None:
        data = json.loads(self._valid_proposal_json())
        data["envelope_sha256"] = "not-a-valid-hash"
        with pytest.raises(ValueError, match="envelope_sha256"):
            self._parse(json.dumps(data))

    def test_rejects_fewer_than_two_children(self) -> None:
        data = json.loads(self._valid_proposal_json())
        data["children"] = [data["children"][0]]
        with pytest.raises(ValueError, match="At least 2 children"):
            self._parse(json.dumps(data))

    def test_rejects_empty_children(self) -> None:
        data = json.loads(self._valid_proposal_json())
        data["children"] = []
        with pytest.raises(ValueError, match="At least 2 children"):
            self._parse(json.dumps(data))

    def test_rejects_empty_child_title(self) -> None:
        data = json.loads(self._valid_proposal_json())
        data["children"][0]["title"] = "   "
        with pytest.raises(ValueError, match="title must not be empty"):
            self._parse(json.dumps(data))

    def test_rejects_invalid_disposition(self) -> None:
        data = json.loads(self._valid_proposal_json())
        data["current_disposition"] = "invalid_disposition"
        with pytest.raises(ValueError, match="current_disposition"):
            self._parse(json.dumps(data))

    def test_rejects_missing_required_child_fields(self) -> None:
        data = json.loads(self._valid_proposal_json())
        del data["children"][0]["source_block_ids"]
        with pytest.raises(ValueError, match="source_block_ids"):
            self._parse(json.dumps(data))

    def test_accepts_implement_current_partition(self) -> None:
        data = json.loads(self._valid_proposal_json())
        data["current_disposition"] = "implement_current_partition"
        proposal = self._parse(json.dumps(data))
        assert proposal.current_disposition == "implement_current_partition"

    def test_rejects_invalid_json(self) -> None:
        with pytest.raises(ValueError, match="Invalid JSON"):
            self._parse("not json at all {{{")

    def test_rejects_non_object_json(self) -> None:
        with pytest.raises(ValueError, match="JSON object"):
            self._parse("[]")

    def test_rejects_empty_source_block_ids(self) -> None:
        data = json.loads(self._valid_proposal_json())
        data["children"][0]["source_block_ids"] = []
        with pytest.raises(ValueError, match="source_block_ids"):
            self._parse(json.dumps(data))

    def test_rejects_missing_implementation_steps(self) -> None:
        data = json.loads(self._valid_proposal_json())
        data["children"][0]["implementation_steps"] = []
        with pytest.raises(ValueError, match="implementation_steps"):
            self._parse(json.dumps(data))

    def test_repair_evidence_ids_can_be_empty(self) -> None:
        data = json.loads(self._valid_proposal_json())
        data["children"][0]["repair_evidence_ids"] = []
        proposal = self._parse(json.dumps(data))
        assert proposal.children[0].repair_evidence_ids == ()


# ---------------------------------------------------------------------------
# Verdict parsing tests
# ---------------------------------------------------------------------------

class VerdictParsingTests(unittest.TestCase):

    def _parse(self, json_text: str) -> RepartitionVerdictV1:
        return parse_verdict_json(
            json_text,
            expected_proposal_sha256="a" * 64,
            expected_candidate_sha256="b" * 64,
        )

    def test_parses_accept_verdict(self) -> None:
        verdict_json = json.dumps({
            "schema_version": 1,
            "proposal_sha256": "a" * 64,
            "candidate_sha256": "b" * 64,
            "verdict": "accept",
            "reason": "All checks pass",
            "findings": [],
        })
        verdict = self._parse(verdict_json)
        assert verdict.verdict == "accept"

    def test_parses_reject_verdict(self) -> None:
        verdict_json = json.dumps({
            "schema_version": 1,
            "proposal_sha256": "a" * 64,
            "candidate_sha256": "b" * 64,
            "verdict": "reject",
            "reason": "Semantic drift detected",
            "findings": ["Missing requirement X", "Weakened criterion Y"],
        })
        verdict = self._parse(verdict_json)
        assert verdict.verdict == "reject"
        assert len(verdict.findings) == 2

    def test_rejects_invalid_verdict_value(self) -> None:
        verdict_json = json.dumps({
            "schema_version": 1,
            "verdict": "maybe",
            "proposal_sha256": "a" * 64,
            "candidate_sha256": "b" * 64,
        })
        with pytest.raises(ValueError, match="verdict"):
            self._parse(verdict_json)


# ---------------------------------------------------------------------------
# Candidate rendering tests
# ---------------------------------------------------------------------------

class CandidateRenderingTests(unittest.TestCase):

    def test_rendered_candidate_preserves_prefix_suffix(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            Some intro text.

            ### [ ] Checkpoint 1: First

            **Goal:**

            Do something.

            ### [ ] Checkpoint 2: Second

            More work.

            ## Final Notes
        """)
        envelope = create_envelope(
            scope_id="scope-render",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        proposal = RepartitionProposalV1(
            schema_version=1,
            envelope_sha256=envelope.canonical_envelope_sha256,
            source_plan_sha256=envelope.plan_sha256,
            rationale="Split large checkpoint",
            children=(
                PartitionSpecV1(
                    title="Part A",
                    narrow_goal="Do part A",
                    source_block_ids=tuple(b.block_id for b in envelope.source_blocks),
                    implementation_steps=("implement part A",),
                    verification_commands=("pytest",),
                    done_criteria=("Part A works",),
                ),
                PartitionSpecV1(
                    title="Part B",
                    narrow_goal="Do part B",
                    source_block_ids=tuple(b.block_id for b in envelope.source_blocks),
                    implementation_steps=("implement part B",),
                    verification_commands=("pytest",),
                    done_criteria=("Part B works",),
                ),
            ),
            current_disposition="review_current_partition",
        )
        candidate = render_candidate_plan(
            envelope=envelope,
            proposal=proposal,
            source_plan_text=text,
            generation_id="gen-001",
            partition_ids=("part-a", "part-b"),
        )

        # Prefix (before checkpoint) preserved
        assert candidate.startswith("# Plan\n\nSome intro text.")

        # Suffix (after first checkpoint) preserved
        assert "### [ ] Checkpoint 2: Second" in candidate
        assert "## Final Notes" in candidate
        assert "More work." in candidate

        # Original checkpoint heading replaced
        assert "### [ ] Checkpoint 1: First / Partition 1/2: Part A" in candidate
        assert "### [ ] Checkpoint 1: First / Partition 2/2: Part B" in candidate

    def test_candidate_children_are_unchecked(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:** test
        """)
        envelope = create_envelope(
            scope_id="scope-unchecked",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        proposal = RepartitionProposalV1(
            schema_version=1,
            envelope_sha256=envelope.canonical_envelope_sha256,
            source_plan_sha256=envelope.plan_sha256,
            rationale="split",
            children=(
                PartitionSpecV1(
                    title="Child 1",
                    narrow_goal="First child",
                    source_block_ids=tuple(b.block_id for b in envelope.source_blocks),
                    implementation_steps=("step",),
                    verification_commands=("pytest",),
                    done_criteria=("works",),
                ),
                PartitionSpecV1(
                    title="Child 2",
                    narrow_goal="Second child",
                    source_block_ids=tuple(b.block_id for b in envelope.source_blocks),
                    implementation_steps=("step",),
                    verification_commands=("pytest",),
                    done_criteria=("works",),
                ),
            ),
            current_disposition="review_current_partition",
        )
        candidate = render_candidate_plan(
            envelope=envelope,
            proposal=proposal,
            source_plan_text=text,
            generation_id="gen-u",
            partition_ids=("c1", "c2"),
        )
        assert "### [ ] Checkpoint 1: First / Partition 1/2: Child 1" in candidate
        assert "### [ ] Checkpoint 1: First / Partition 2/2: Child 2" in candidate
        assert "### [x] Checkpoint" not in candidate

    def test_candidate_includes_authoritative_source_fence(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:**

            Build feature.
        """)
        envelope = create_envelope(
            scope_id="scope-fence",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        proposal = RepartitionProposalV1(
            schema_version=1,
            envelope_sha256=envelope.canonical_envelope_sha256,
            source_plan_sha256=envelope.plan_sha256,
            rationale="split",
            children=(
                PartitionSpecV1(
                    title="Child 1",
                    narrow_goal="First",
                    source_block_ids=tuple(b.block_id for b in envelope.source_blocks),
                    implementation_steps=("step",),
                    verification_commands=("pytest",),
                    done_criteria=("works",),
                ),
                PartitionSpecV1(
                    title="Child 2",
                    narrow_goal="Second",
                    source_block_ids=tuple(b.block_id for b in envelope.source_blocks),
                    implementation_steps=("step",),
                    verification_commands=("pytest",),
                    done_criteria=("works",),
                ),
            ),
            current_disposition="review_current_partition",
        )
        candidate = render_candidate_plan(
            envelope=envelope,
            proposal=proposal,
            source_plan_text=text,
            generation_id="gen-f",
            partition_ids=("c1", "c2"),
        )
        assert "~~~aflow-authoritative-source" in candidate
        assert "~~~aflow-repair-evidence" not in candidate  # no repair evidence

    def test_candidate_includes_repair_evidence_when_present(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:** test
        """)
        envelope = create_envelope(
            scope_id="scope-repair",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        repair_block = SourceBlock(
            block_id="ev-001",
            text="**Fix:** address missing edge case\n",
            byte_start=0,
            byte_end=30,
            line_start=1,
            line_end=2,
            section_label="**Fix:**",
            content_sha256=hashlib.sha256(b"**Fix:** address missing edge case\n").hexdigest(),
        )
        proposal = RepartitionProposalV1(
            schema_version=1,
            envelope_sha256=envelope.canonical_envelope_sha256,
            source_plan_sha256=envelope.plan_sha256,
            rationale="split",
            children=(
                PartitionSpecV1(
                    title="Child 1",
                    narrow_goal="First",
                    source_block_ids=tuple(b.block_id for b in envelope.source_blocks),
                    implementation_steps=("step",),
                    verification_commands=("pytest",),
                    done_criteria=("works",),
                    repair_evidence_ids=("ev-001",),
                ),
                PartitionSpecV1(
                    title="Child 2",
                    narrow_goal="Second",
                    source_block_ids=tuple(b.block_id for b in envelope.source_blocks),
                    implementation_steps=("step",),
                    verification_commands=("pytest",),
                    done_criteria=("works",),
                ),
            ),
            current_disposition="review_current_partition",
        )
        candidate = render_candidate_plan(
            envelope=envelope,
            proposal=proposal,
            source_plan_text=text,
            generation_id="gen-r",
            partition_ids=("c1", "c2"),
            repair_evidence_blocks=(repair_block,),
            repair_evidence_artifact_references={"ev-001": "review/rejection.txt"},
        )
        assert "~~~aflow-repair-evidence" in candidate
        assert "Non-authoritative corrective evidence" in candidate

    def test_metadata_comment_present(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:** test
        """)
        envelope = create_envelope(
            scope_id="scope-meta",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        proposal = RepartitionProposalV1(
            schema_version=1,
            envelope_sha256=envelope.canonical_envelope_sha256,
            source_plan_sha256=envelope.plan_sha256,
            rationale="split",
            children=(
                PartitionSpecV1(
                    title="Child 1",
                    narrow_goal="First",
                    source_block_ids=tuple(b.block_id for b in envelope.source_blocks),
                    implementation_steps=("step",),
                    verification_commands=("pytest",),
                    done_criteria=("works",),
                ),
                PartitionSpecV1(
                    title="Child 2",
                    narrow_goal="Second",
                    source_block_ids=tuple(b.block_id for b in envelope.source_blocks),
                    implementation_steps=("step",),
                    verification_commands=("pytest",),
                    done_criteria=("works",),
                ),
            ),
            current_disposition="review_current_partition",
        )
        candidate = render_candidate_plan(
            envelope=envelope,
            proposal=proposal,
            source_plan_text=text,
            generation_id="gen-meta-001",
            partition_ids=("p1", "p2"),
        )
        # The JSON metadata comment is the single machine-readable identity.
        assert "<!-- aflow-repartition-metadata " in candidate
        assert '"partition_id":"p1"' in candidate
        assert '"partition_id":"p2"' in candidate
        assert '"generation_id":"gen-meta-001"' in candidate


# ---------------------------------------------------------------------------
# Mechanical validation tests
# ---------------------------------------------------------------------------

class MechanicalValidationTests(unittest.TestCase):

    def _make_envelope_and_proposal(self, plan_text: str) -> tuple[ScopeEnvelopeV1, RepartitionProposalV1]:
        envelope = create_envelope(
            scope_id="scope-mech",
            original_plan_path="plan.md",
            plan_text=plan_text,
            checkpoint_index=1,
        )
        proposal = RepartitionProposalV1(
            schema_version=1,
            envelope_sha256=envelope.canonical_envelope_sha256,
            source_plan_sha256=envelope.plan_sha256,
            rationale="test",
            children=(
                PartitionSpecV1(
                    title="Part 1",
                    narrow_goal="Part 1 goal",
                    source_block_ids=tuple(b.block_id for b in envelope.source_blocks),
                    implementation_steps=("step",),
                    verification_commands=("pytest",),
                    done_criteria=("works",),
                ),
                PartitionSpecV1(
                    title="Part 2",
                    narrow_goal="Part 2 goal",
                    source_block_ids=tuple(b.block_id for b in envelope.source_blocks),
                    implementation_steps=("step",),
                    verification_commands=("pytest",),
                    done_criteria=("works",),
                ),
            ),
            current_disposition="review_current_partition",
        )
        return envelope, proposal

    def test_valid_candidate_passes(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:** test

            ### [ ] Checkpoint 2: Second

            More work.
        """)
        envelope, proposal = self._make_envelope_and_proposal(text)
        candidate = render_candidate_plan(
            envelope=envelope,
            proposal=proposal,
            source_plan_text=text,
            generation_id="gen-v",
            partition_ids=("p1", "p2"),
        )
        result = validate_candidate_mechanically(
            source_plan_text=text,
            candidate_plan_text=candidate,
            envelope=envelope,
            proposal=proposal,
            repair_evidence_artifact_references={},
            expected_generation_id="gen-v",
            expected_partition_ids=("p1", "p2"),
        )
        assert result.valid, f"Issues: {result.issues}"
        assert result.unchanged_prefix
        assert result.unchanged_suffix
        assert result.all_children_unchecked
        assert result.child_count == 2

    def test_detects_prefix_modification(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:** test

            ### [ ] Checkpoint 2: Second

            More work.
        """)
        envelope, proposal = self._make_envelope_and_proposal(text)
        candidate = render_candidate_plan(
            envelope=envelope,
            proposal=proposal,
            source_plan_text=text,
            generation_id="gen-p",
            partition_ids=("p1", "p2"),
        )
        # Modify prefix
        modified_candidate = "# Modified\n" + candidate[candidate.index("\n") + 1:]
        result = validate_candidate_mechanically(
            source_plan_text=text,
            candidate_plan_text=modified_candidate,
            envelope=envelope,
            proposal=proposal,
            repair_evidence_artifact_references={},
            expected_generation_id="gen-p",
            expected_partition_ids=("p1", "p2"),
        )
        assert not result.valid
        assert not result.unchanged_prefix

    def test_detects_missing_source_block_coverage(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:**

            Do A.

            **Steps:**

            - [ ] step one

            **Verification:**

            - Run: pytest
        """)
        envelope = create_envelope(
            scope_id="scope-cov",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        # Only cover some blocks
        covered_ids = [b.block_id for b in envelope.source_blocks[:1]]  # Only first block
        proposal = RepartitionProposalV1(
            schema_version=1,
            envelope_sha256=envelope.canonical_envelope_sha256,
            source_plan_sha256=envelope.plan_sha256,
            rationale="test",
            children=(
                PartitionSpecV1(
                    title="Part 1",
                    narrow_goal="Part 1",
                    source_block_ids=tuple(covered_ids),
                    implementation_steps=("step",),
                    verification_commands=("pytest",),
                    done_criteria=("works",),
                ),
                PartitionSpecV1(
                    title="Part 2",
                    narrow_goal="Part 2",
                    source_block_ids=tuple(covered_ids),
                    implementation_steps=("step",),
                    verification_commands=("pytest",),
                    done_criteria=("works",),
                ),
            ),
            current_disposition="review_current_partition",
        )
        candidate = render_candidate_plan(
            envelope=envelope,
            proposal=proposal,
            source_plan_text=text,
            generation_id="gen-cov",
            partition_ids=("p1", "p2"),
        )
        result = validate_candidate_mechanically(
            source_plan_text=text,
            candidate_plan_text=candidate,
            envelope=envelope,
            proposal=proposal,
            repair_evidence_artifact_references={},
            expected_generation_id="gen-cov",
            expected_partition_ids=("p1", "p2"),
        )
        assert not result.valid
        assert any("Source blocks not covered" in issue for issue in result.issues)

    def test_detects_checked_child(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:** test
        """)
        envelope, proposal = self._make_envelope_and_proposal(text)
        candidate = render_candidate_plan(
            envelope=envelope,
            proposal=proposal,
            source_plan_text=text,
            generation_id="gen-check",
            partition_ids=("p1", "p2"),
        )
        # Tamper: mark first child as checked
        modified = candidate.replace("### [ ] Checkpoint 1:", "### [x] Checkpoint 1:")
        result = validate_candidate_mechanically(
            source_plan_text=text,
            candidate_plan_text=modified,
            envelope=envelope,
            proposal=proposal,
            repair_evidence_artifact_references={},
            expected_generation_id="gen-check",
            expected_partition_ids=("p1", "p2"),
        )
        assert not result.valid
        assert not result.all_children_unchecked


# ---------------------------------------------------------------------------
# Drift validation tests
# ---------------------------------------------------------------------------

class DriftValidationTests(unittest.TestCase):

    def test_identical_plan_allowed(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:** test
        """)
        envelope = create_envelope(
            scope_id="scope-drift",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        result = validate_envelope_boundary_drift(
            envelope=envelope,
            boundary_plan_text=text,
        )
        assert result.allowed
        assert not result.issues

    def test_checkbox_change_allowed(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:** test

            - [ ] step one

            ### [ ] Checkpoint 2: Second

            More work.
        """)
        envelope = create_envelope(
            scope_id="scope-drift2",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        # Mark step one as checked
        boundary = text.replace("- [ ] step one", "- [x] step one")
        result = validate_envelope_boundary_drift(
            envelope=envelope,
            boundary_plan_text=boundary,
        )
        assert result.allowed, f"Issues: {result.issues}"

    def test_prose_change_detected(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:** test
        """)
        envelope = create_envelope(
            scope_id="scope-drift3",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        boundary = text.replace("test", "CHANGED GOAL")
        result = validate_envelope_boundary_drift(
            envelope=envelope,
            boundary_plan_text=boundary,
        )
        assert not result.allowed

    def test_length_change_detected(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:** test
        """)
        envelope = create_envelope(
            scope_id="scope-drift4",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        boundary = text + "\nExtra line.\n"
        result = validate_envelope_boundary_drift(
            envelope=envelope,
            boundary_plan_text=boundary,
        )
        assert not result.allowed

    def test_heading_checkbox_change_allowed(self) -> None:
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:** test
        """)
        envelope = create_envelope(
            scope_id="scope-drift5",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        boundary = text.replace("### [ ] Checkpoint 1:", "### [x] Checkpoint 1:")
        result = validate_envelope_boundary_drift(
            envelope=envelope,
            boundary_plan_text=boundary,
        )
        assert result.allowed, f"Issues: {result.issues}"


# ---------------------------------------------------------------------------
# Integration: summary cannot substitute for coverage
# ---------------------------------------------------------------------------

class SummaryCannotSubstituteTests(unittest.TestCase):

    def test_missing_source_block_fails_coverage_even_with_good_summary(self) -> None:
        """A misleading summary cannot satisfy source block coverage."""
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:**

            Build a complete authentication system with OAuth2, JWT, and session management.

            **Steps:**

            - [ ] implement OAuth2 flow
            - [ ] create JWT middleware
            - [ ] add session store

            **Verification:**

            - Run: `pytest tests/test_auth.py`
        """)
        envelope = create_envelope(
            scope_id="scope-summary",
            original_plan_path="plan.md",
            plan_text=text,
            checkpoint_index=1,
        )
        # A proposal that only covers 2 of 3+ blocks
        partial_ids = tuple(b.block_id for b in list(envelope.source_blocks)[:2])
        proposal = RepartitionProposalV1(
            schema_version=1,
            envelope_sha256=envelope.canonical_envelope_sha256,
            source_plan_sha256=envelope.plan_sha256,
            rationale="This summary says we covered all auth requirements but we really didn't",
            children=(
                PartitionSpecV1(
                    title="Auth Part 1",
                    narrow_goal="Implement auth (summary claims full coverage)",
                    source_block_ids=partial_ids,
                    implementation_steps=("do auth",),
                    verification_commands=("pytest",),
                    done_criteria=("auth works",),
                ),
                PartitionSpecV1(
                    title="Auth Part 2",
                    narrow_goal="Complete auth",
                    source_block_ids=partial_ids,
                    implementation_steps=("finish auth",),
                    verification_commands=("pytest",),
                    done_criteria=("auth complete",),
                ),
            ),
            current_disposition="review_current_partition",
        )
        candidate = render_candidate_plan(
            envelope=envelope,
            proposal=proposal,
            source_plan_text=text,
            generation_id="gen-sum",
            partition_ids=("p1", "p2"),
        )
        result = validate_candidate_mechanically(
            source_plan_text=text,
            candidate_plan_text=candidate,
            envelope=envelope,
            proposal=proposal,
            repair_evidence_artifact_references={},
            expected_generation_id="gen-sum",
            expected_partition_ids=("p1", "p2"),
        )
        assert not result.valid
        assert any("Source blocks not covered" in issue for issue in result.issues)


# ---------------------------------------------------------------------------
# Review-rejection regressions
# ---------------------------------------------------------------------------

class ReviewRejectionRegressionTests(unittest.TestCase):

    def _plan(self, *, git_tracking: bool = False, final: bool = False) -> str:
        tracking = (
            "## Git Tracking\n\n"
            "- Plan Branch: `a`\n"
            "- Pre-Handoff Base HEAD: `b`\n\n"
            if git_tracking else ""
        )
        suffix = "" if final else "\n### [ ] Checkpoint 2: Later\n- [ ] later\n"
        return (
            "# Plan\n\n" + tracking
            + "### [ ] Checkpoint 7: Café\n\n"
            + "**Goal:** keep exact scope\n\n"
            + "- ordinary first bullet\n"
            + "- ordinary second bullet\n"
            + suffix
        )

    def _envelope_and_proposal(self, text: str) -> tuple[ScopeEnvelopeV1, RepartitionProposalV1]:
        envelope = create_envelope(
            scope_id="review-regression",
            original_plan_path="plans/example.md",
            plan_text=text,
            checkpoint_index=1,
        )
        ids = tuple(block.block_id for block in envelope.source_blocks)
        proposal = RepartitionProposalV1(
            schema_version=1,
            envelope_sha256=envelope.canonical_envelope_sha256,
            source_plan_sha256=envelope.plan_sha256,
            rationale="Separate independently reviewable work.",
            children=(
                PartitionSpecV1(
                    title="First", narrow_goal="First narrow goal",
                    source_block_ids=ids, implementation_steps=("do first",),
                    verification_commands=("pytest",), done_criteria=("first works",),
                ),
                PartitionSpecV1(
                    title="Second", narrow_goal="Second narrow goal",
                    source_block_ids=ids, implementation_steps=("do second",),
                    verification_commands=("pytest",), done_criteria=("second works",),
                ),
            ),
            current_disposition="review_current_partition",
        )
        return envelope, proposal

    def _validate(
        self,
        text: str,
        candidate: str,
        envelope: ScopeEnvelopeV1,
        proposal: RepartitionProposalV1,
        *,
        generation_id: str = "generation-bound",
        partition_ids: tuple[str, ...] = ("partition-1", "partition-2"),
        repair_evidence_blocks: tuple[SourceBlock, ...] = (),
        repair_evidence_artifact_references: Mapping[str, str] = _EMPTY_MAPPING,
    ) -> MechanicalValidationResult:
        return validate_candidate_mechanically(
            source_plan_text=text,
            candidate_plan_text=candidate,
            envelope=envelope,
            proposal=proposal,
            repair_evidence_blocks=repair_evidence_blocks,
            repair_evidence_artifact_references=repair_evidence_artifact_references,
            expected_generation_id=generation_id,
            expected_partition_ids=partition_ids,
        )

    def _mutate_first_metadata(self, candidate: str, mutate: object) -> str:
        prefix = "<!-- aflow-repartition-metadata "
        start = candidate.index(prefix)
        end = candidate.index(" -->", start)
        metadata = json.loads(candidate[start + len(prefix):end])
        mutate(metadata)
        replacement = prefix + json.dumps(
            metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ) + " -->"
        return candidate[:start] + replacement + candidate[end + 4:]

    def _repair_fixture(
        self, text: str,
    ) -> tuple[ScopeEnvelopeV1, RepartitionProposalV1, SourceBlock]:
        envelope, base = self._envelope_and_proposal(text)
        repair_text = "**Fix:** preserve the exact reviewer correction.\n"
        repair = SourceBlock(
            block_id="repair-1", text=repair_text, byte_start=0,
            byte_end=len(repair_text.encode("utf-8")), line_start=1, line_end=2,
            section_label="**Fix:**",
            content_sha256=hashlib.sha256(repair_text.encode("utf-8")).hexdigest(),
        )
        proposal = replace(
            base,
            children=(
                replace(base.children[0], repair_evidence_ids=(repair.block_id,)),
                base.children[1],
            ),
        )
        return envelope, proposal, repair

    def test_protocol_parsers_reject_contextual_and_incomplete_inputs(self) -> None:
        source_ids = {"source-a", "source-b"}
        base = {
            "schema_version": 1,
            "envelope_sha256": "a" * 64,
            "source_plan_sha256": "b" * 64,
            "rationale": "A real reason",
            "cross_cutting_source_reasons": {},
            "children": [
                {
                    "title": "One", "narrow_goal": "One goal",
                    "source_block_ids": ["source-a"],
                    "repair_evidence_ids": [],
                    "implementation_steps": ["step"],
                    "verification_commands": ["pytest"],
                    "done_criteria": ["works"],
                    "unknown": "must fail",
                },
                {
                    "title": "Two", "narrow_goal": "Two goal",
                    "source_block_ids": ["source-b"],
                    "repair_evidence_ids": [],
                    "implementation_steps": ["step"],
                    "verification_commands": ["pytest"],
                    "done_criteria": ["works"],
                },
            ],
            "current_disposition": "review_current_partition",
        }
        with pytest.raises(ValueError):
            parse_proposal_json(
                json.dumps(base),
                expected_envelope_sha256="a" * 64,
                expected_source_plan_sha256="b" * 64,
                valid_source_block_ids=source_ids,
                valid_repair_evidence_ids=set(),
            )
        with pytest.raises(TypeError):
            parse_verdict_json(json.dumps({"schema_version": 1, "verdict": "accept"}))

    def test_mechanical_validation_reads_candidate_payload_not_proposal_ids(self) -> None:
        text = self._plan()
        envelope, proposal = self._envelope_and_proposal(text)
        source = text.encode("utf-8")
        fake = (
            source[:envelope.checkpoint_byte_start]
            + b"### [ ] Checkpoint 7: Fake one\n- [ ] fake\n\n"
            + b"### [ ] Checkpoint 7: Fake two\n- [ ] fake\n"
            + source[envelope.checkpoint_byte_end:]
        ).decode("utf-8")
        result = validate_candidate_mechanically(
            source_plan_text=text, candidate_plan_text=fake,
            envelope=envelope, proposal=proposal,
            repair_evidence_artifact_references={},
            expected_generation_id="fake-generation",
            expected_partition_ids=("fake-1", "fake-2"),
        )
        assert not result.valid
        assert not all(result.source_block_coverage.values())
        assert result.issues

    def test_renderer_preserves_unicode_external_bytes_and_parent_identity(self) -> None:
        text = "# Préface 🎉\n\n" + self._plan()
        envelope, proposal = self._envelope_and_proposal(text)
        candidate = render_candidate_plan(
            envelope=envelope, proposal=proposal, source_plan_text=text,
            generation_id="generation-1", partition_ids=("part-1", "part-2"),
        )
        source = text.encode("utf-8")
        rendered = candidate.encode("utf-8")
        assert rendered[:envelope.checkpoint_byte_start] == source[:envelope.checkpoint_byte_start]
        assert rendered[-len(source[envelope.checkpoint_byte_end:]):] == source[envelope.checkpoint_byte_end:]
        assert "Checkpoint 7: Café / Partition 1/2" in candidate
        assert "Checkpoint 1:" not in candidate

    def test_renderer_uses_a_fence_longer_than_embedded_source_fences(self) -> None:
        text = (
            "# Plan\n\n### [ ] Checkpoint 1: Fence\n\n"
            "**Context:**\n\n````\ninside\n````\n"
        )
        envelope, proposal = self._envelope_and_proposal(text)
        candidate = render_candidate_plan(
            envelope=envelope, proposal=proposal, source_plan_text=text,
            generation_id="generation-2", partition_ids=("part-1", "part-2"),
        )
        assert "`````aflow-authoritative-source" in candidate or "~~~~~aflow-authoritative-source" in candidate

    def test_envelope_artifacts_are_immutable_and_tampering_is_not_absence(self) -> None:
        envelope, _ = self._envelope_and_proposal(self._plan())
        with tempfile.TemporaryDirectory() as raw_dir:
            directory = Path(raw_dir)
            path = write_envelope_atomic(envelope, directory)
            assert write_envelope_atomic(envelope, directory) == path
            changed = replace(envelope, scope_id="different")
            with pytest.raises(ValueError):
                write_envelope_atomic(changed, directory)
            path.write_text("{bad json", encoding="utf-8")
            with pytest.raises(ValueError):
                read_envelope(path)

    def test_final_checkpoint_split_and_git_tracking_value_length_change_are_valid(self) -> None:
        final_text = self._plan(final=True)
        envelope, proposal = self._envelope_and_proposal(final_text)
        candidate = render_candidate_plan(
            envelope=envelope, proposal=proposal, source_plan_text=final_text,
            generation_id="generation-3", partition_ids=("part-1", "part-2"),
        )
        result = validate_candidate_mechanically(
            source_plan_text=final_text, candidate_plan_text=candidate,
            envelope=envelope, proposal=proposal,
            repair_evidence_artifact_references={},
            expected_generation_id="generation-3",
            expected_partition_ids=("part-1", "part-2"),
        )
        assert result.valid, result.issues

        tracked = self._plan(git_tracking=True)
        tracked_envelope = create_envelope(
            scope_id="tracked", original_plan_path="plans/example.md",
            plan_text=tracked, checkpoint_index=1,
        )
        boundary = tracked.replace("Plan Branch: `a`", "Plan Branch: `much-longer-branch-name`")
        assert validate_envelope_boundary_drift(
            envelope=tracked_envelope, boundary_plan_text=boundary,
        ).allowed

    def test_ordinary_top_level_bullets_are_distinct_blocks(self) -> None:
        text = self._plan(final=True)
        sl = slice_checkpoint_source(text, checkpoint_index=1)
        assert sl is not None
        blocks = extract_source_blocks(sl, envelope_checkpoint_sha256="d" * 64, plan_text=text)
        bullet_blocks = [block for block in blocks if "ordinary" in block.text]
        assert len(bullet_blocks) == 2

    def test_contextual_protocol_checks_reject_unknown_duplicate_and_uncovered_ids(self) -> None:
        data = {
            "schema_version": 1,
            "envelope_sha256": "a" * 64,
            "source_plan_sha256": "b" * 64,
            "rationale": "Preserve authority while splitting work.",
            "cross_cutting_source_reasons": {},
            "children": [
                {"title": "One", "narrow_goal": "One goal", "source_block_ids": ["s1"],
                 "repair_evidence_ids": ["r1"], "implementation_steps": ["one"],
                 "verification_commands": ["pytest"], "done_criteria": ["one works"]},
                {"title": "Two", "narrow_goal": "Two goal", "source_block_ids": ["s2"],
                 "repair_evidence_ids": [], "implementation_steps": ["two"],
                 "verification_commands": ["pytest"], "done_criteria": ["two works"]},
            ],
            "current_disposition": "review_current_partition",
        }
        kwargs = dict(
            expected_envelope_sha256="a" * 64, expected_source_plan_sha256="b" * 64,
            valid_source_block_ids={"s1", "s2"}, valid_repair_evidence_ids={"r1"},
        )
        assert parse_proposal_json(json.dumps(data), **kwargs).children[0].title == "One"
        with pytest.raises(ValueError, match="expected source plan"):
            parse_proposal_json(
                json.dumps(data),
                **{**kwargs, "expected_source_plan_sha256": "c" * 64},
            )
        duplicate = json.loads(json.dumps(data))
        duplicate["children"][1]["source_block_ids"] = ["s1"]
        with pytest.raises(ValueError):
            parse_proposal_json(json.dumps(duplicate), **kwargs)
        unknown = json.loads(json.dumps(data))
        unknown["children"][1]["source_block_ids"] = ["unknown"]
        with pytest.raises(ValueError):
            parse_proposal_json(json.dumps(unknown), **kwargs)
        with pytest.raises(ValueError, match="does not cover repair evidence"):
            parse_proposal_json(
                json.dumps(data),
                **{**kwargs, "valid_repair_evidence_ids": {"r1", "r2"}},
            )
        with pytest.raises(ValueError, match="unknown repair evidence"):
            parse_proposal_json(
                json.dumps(data),
                **{**kwargs, "valid_repair_evidence_ids": set()},
            )

    def test_protocol_context_is_required_and_hash_bound_for_all_verdicts(self) -> None:
        proposal_json = ProposalParsingTests()._valid_proposal_json()
        with pytest.raises(TypeError):
            parse_proposal_json(proposal_json)
        with pytest.raises(ValueError, match="expected envelope"):
            parse_proposal_json(
                proposal_json,
                expected_envelope_sha256="c" * 64,
                expected_source_plan_sha256="b" * 64,
                valid_source_block_ids={"abcdef01-b000", "abcdef01-b001"},
                valid_repair_evidence_ids=set(),
            )

        for verdict in ("accept", "reject"):
            verdict_json = json.dumps({
                "schema_version": 1, "proposal_sha256": "a" * 64,
                "candidate_sha256": "b" * 64, "verdict": verdict,
                "reason": "Bound semantic result", "findings": [],
            })
            with pytest.raises(TypeError):
                parse_verdict_json(verdict_json)
            with pytest.raises(ValueError, match="expected proposal"):
                parse_verdict_json(
                    verdict_json,
                    expected_proposal_sha256="c" * 64,
                    expected_candidate_sha256="b" * 64,
                )
            with pytest.raises(ValueError, match="expected candidate"):
                parse_verdict_json(
                    verdict_json,
                    expected_proposal_sha256="a" * 64,
                    expected_candidate_sha256="c" * 64,
                )

    def test_authoritative_fence_and_identity_tampering_is_rejected(self) -> None:
        text = self._plan()
        envelope, proposal = self._envelope_and_proposal(text)
        candidate = render_candidate_plan(
            envelope=envelope, proposal=proposal, source_plan_text=text,
            generation_id="generation-bound",
            partition_ids=("partition-1", "partition-2"),
        )
        assert self._validate(text, candidate, envelope, proposal).valid

        opener = re.search(
            r"(?m)^([`~]{3,})aflow-authoritative-source\r?$", candidate,
        )
        assert opener is not None
        marker = opener.group(1)
        close_start = candidate.index("\n" + marker + "\n", opener.end()) + 1
        close_end = close_start + len(marker)
        fence_body = candidate[opener.start():close_end + 1]

        mutations = {
            "removed opener": candidate[:opener.start()] + candidate[opener.end() + 1:],
            "short opener": candidate[:opener.start()] + marker[:-1]
            + candidate[opener.start() + len(marker):],
            "renamed opener": candidate[:opener.start()] + marker
            + "aflow-wrong-source" + candidate[opener.end():],
            "short close": candidate[:close_start] + marker[:-1] + candidate[close_end:],
            "duplicate fence": candidate[:close_end + 1] + fence_body + candidate[close_end + 1:],
            "payload outside fence": (
                candidate[:opener.start()] + candidate[opener.end() + 1:close_start]
                + candidate[close_end + 1:]
            ),
            "generation identity": candidate.replace(
                '"generation_id":"generation-bound"',
                '"generation_id":"generation-tampered"', 1,
            ),
            "partition identity": candidate.replace(
                '"partition_id":"partition-1"',
                '"partition_id":"partition-tampered"', 1,
            ),
            "duplicate metadata": candidate[:opener.start()]
            + candidate[candidate.rfind(
                "<!-- aflow-repartition-metadata ", 0, opener.start(),
            ):candidate.rfind("\n", 0, opener.start()) + 1]
            + candidate[opener.start():],
            "extra metadata key": self._mutate_first_metadata(
                candidate, lambda metadata: metadata.__setitem__("extra", True),
            ),
            "duplicate source metadata": self._mutate_first_metadata(
                candidate,
                lambda metadata: metadata["source_blocks"].append(
                    dict(metadata["source_blocks"][0]),
                ),
            ),
        }
        for label, mutated in mutations.items():
            result = self._validate(text, mutated, envelope, proposal)
            assert not result.valid, label
            assert result.issues, label

        assert not self._validate(
            text, candidate, envelope, proposal,
            generation_id="wrong-generation",
        ).valid
        assert not self._validate(
            text, candidate, envelope, proposal,
            partition_ids=("partition-2", "partition-1"),
        ).valid
        assert not self._validate(
            text, candidate, envelope, proposal,
            generation_id=" ",
        ).valid
        assert not self._validate(
            text, candidate, envelope, proposal,
            partition_ids=("partition-1", "partition-1"),
        ).valid

    def test_repair_evidence_requires_exact_separate_fenced_payload(self) -> None:
        text = self._plan()
        envelope, proposal, repair = self._repair_fixture(text)
        candidate = render_candidate_plan(
            envelope=envelope, proposal=proposal, source_plan_text=text,
            generation_id="generation-bound",
            partition_ids=("partition-1", "partition-2"),
            repair_evidence_blocks=(repair,),
            repair_evidence_artifact_references={
                repair.block_id: "review/latest-rejection.txt",
            },
        )
        assert self._validate(
            text, candidate, envelope, proposal,
            repair_evidence_blocks=(repair,),
            repair_evidence_artifact_references={
                repair.block_id: "review/latest-rejection.txt",
            },
        ).valid
        opener = re.search(r"(?m)^([`~]{3,})aflow-repair-evidence\r?$", candidate)
        assert opener is not None
        marker = opener.group(1)
        close_start = candidate.index("\n" + marker + "\n", opener.end()) + 1
        close_end = close_start + len(marker)
        mutations = (
            candidate[:opener.start()] + candidate[opener.end() + 1:],
            candidate[:opener.start()] + marker + "aflow-wrong-evidence"
            + candidate[opener.end():],
            candidate[:close_start] + marker[:-1] + candidate[close_end:],
            candidate.replace(
                "# Non-authoritative corrective evidence; authoritative scope is above.",
                "# Evidence label was weakened.", 1,
            ),
            candidate.replace(repair.text, repair.text + "tampered\n", 1),
        )
        for mutated in mutations:
            result = self._validate(
                text, mutated, envelope, proposal,
                repair_evidence_blocks=(repair,),
                repair_evidence_artifact_references={
                    repair.block_id: "review/latest-rejection.txt",
                },
            )
            assert not result.valid
            assert result.issues

    def test_crlf_rendering_and_drift_allowlist_are_byte_safe(self) -> None:
        text = (
            "# Plan\r\n\r\n## Git Tracking\r\n\r\n"
            "- Plan Branch: `short`\r\n\r\n"
            "### [ ] Checkpoint 4: Current\r\n\r\n"
            "- [ ] live task\r\n\r\n```\r\n- [ ] example only\r\n```\r\n\r\n"
            "### [ ] Checkpoint 5: Later\r\n- [ ] later task\r\n"
        )
        envelope, proposal = self._envelope_and_proposal(text)
        candidate = render_candidate_plan(
            envelope=envelope, proposal=proposal, source_plan_text=text,
            generation_id="generation-crlf", partition_ids=("crlf-1", "crlf-2"),
        )
        source = text.encode("utf-8")
        rendered = candidate.encode("utf-8")
        assert rendered[:envelope.checkpoint_byte_start] == source[:envelope.checkpoint_byte_start]
        assert rendered[-len(source[envelope.checkpoint_byte_end:]):] == source[envelope.checkpoint_byte_end:]
        assert validate_candidate_mechanically(
            source_plan_text=text, candidate_plan_text=candidate,
            envelope=envelope, proposal=proposal,
            repair_evidence_artifact_references={},
            expected_generation_id="generation-crlf",
            expected_partition_ids=("crlf-1", "crlf-2"),
        ).valid

        assert validate_envelope_boundary_drift(
            envelope=envelope,
            boundary_plan_text=text.replace("Plan Branch: `short`", "Plan Branch: `a-longer-controller-value`"),
        ).allowed
        assert validate_envelope_boundary_drift(
            envelope=envelope,
            boundary_plan_text=text.replace("- [ ] live task", "- [x] live task"),
        ).allowed
        assert not validate_envelope_boundary_drift(
            envelope=envelope,
            boundary_plan_text=text.replace("- [ ] example only", "- [x] example only"),
        ).allowed
        assert not validate_envelope_boundary_drift(
            envelope=envelope,
            boundary_plan_text=text.replace("- [ ] later task", "- [x] later task"),
        ).allowed
        assert not validate_envelope_boundary_drift(
            envelope=envelope,
            boundary_plan_text=text.replace("Plan Branch", "Plan Name"),
        ).allowed

    def test_shared_parent_metadata_tampering_is_rejected(self) -> None:
        text = self._plan()
        envelope, proposal = self._envelope_and_proposal(text)
        candidate = render_candidate_plan(
            envelope=envelope, proposal=proposal, source_plan_text=text,
            generation_id="gen-shared",
            partition_ids=("part-a", "part-b"),
        )
        assert self._validate(
            text, candidate, envelope, proposal,
            generation_id="gen-shared", partition_ids=("part-a", "part-b"),
        ).valid

        def tamper_shared_parents(metadata: dict[str, object]) -> None:
            metadata["shared_parent_source_block_ids"] = ["fake-id"]

        mutated = self._mutate_first_metadata(candidate, tamper_shared_parents)
        result = self._validate(
            text, mutated, envelope, proposal,
            generation_id="gen-shared", partition_ids=("part-a", "part-b"),
        )
        assert not result.valid
        assert any("child_shared_parent_mismatch" in issue for issue in result.issues)

    def test_repair_artifact_reference_tampering_is_rejected(self) -> None:
        text = self._plan()
        envelope, proposal, repair = self._repair_fixture(text)
        refs = {repair.block_id: "review/latest-rejection.txt"}
        candidate = render_candidate_plan(
            envelope=envelope, proposal=proposal, source_plan_text=text,
            generation_id="gen-refs",
            partition_ids=("part-a", "part-b"),
            repair_evidence_blocks=(repair,),
            repair_evidence_artifact_references=refs,
        )
        assert self._validate(
            text, candidate, envelope, proposal,
            repair_evidence_blocks=(repair,),
            repair_evidence_artifact_references=refs,
            generation_id="gen-refs", partition_ids=("part-a", "part-b"),
        ).valid

        def tamper_artifact_ref(metadata: dict[str, object]) -> None:
            metadata["repair_evidence"][0]["artifact_reference"] = "evil/path.txt"

        mutated = self._mutate_first_metadata(candidate, tamper_artifact_ref)
        result = self._validate(
            text, mutated, envelope, proposal,
            repair_evidence_blocks=(repair,),
            repair_evidence_artifact_references=refs,
            generation_id="gen-refs", partition_ids=("part-a", "part-b"),
        )
        assert not result.valid
        assert any("repair_metadata_mismatch" in issue for issue in result.issues)

        # Also verify that validation requires the controller mapping argument
        with pytest.raises(TypeError):
            validate_candidate_mechanically(
                source_plan_text=text, candidate_plan_text=candidate,
                envelope=envelope, proposal=proposal,
                repair_evidence_blocks=(repair,),
                expected_generation_id="gen-refs",
                expected_partition_ids=("part-a", "part-b"),
            )

    def test_authoritative_fence_placement_after_narrow_goal_is_rejected(self) -> None:
        text = self._plan()
        envelope, proposal = self._envelope_and_proposal(text)
        candidate = render_candidate_plan(
            envelope=envelope, proposal=proposal, source_plan_text=text,
            generation_id="gen-fpos",
            partition_ids=("part-a", "part-b"),
        )
        assert self._validate(
            text, candidate, envelope, proposal,
            generation_id="gen-fpos", partition_ids=("part-a", "part-b"),
        ).valid

        # Move the authoritative fence after the "**Narrow Goal:**" line
        opener = re.search(r"(?m)^([`~]{3,})aflow-authoritative-source\r?$", candidate)
        assert opener is not None
        marker = opener.group(1)
        close_start = candidate.index("\n" + marker + "\n", opener.end()) + 1
        close_end = close_start + len(marker)
        fence_block = candidate[opener.start():close_end + 1]

        # Remove the fence from its original position
        without_fence = candidate[:opener.start()] + candidate[close_end + 1:]

        # Find "**Narrow Goal:**" in the modified text
        narrow_idx = without_fence.find("\n**Narrow Goal:**")
        assert narrow_idx != -1

        # Insert the fence block after the narrow goal line
        narrow_goal_end = without_fence.index("\n", narrow_idx + 1)
        mutated = without_fence[:narrow_goal_end + 1] + "\n" + fence_block + without_fence[narrow_goal_end + 1:]

        result = self._validate(
            text, mutated, envelope, proposal,
            generation_id="gen-fpos", partition_ids=("part-a", "part-b"),
        )
        assert not result.valid
        assert any("narrow_goal" in issue for issue in result.issues)

    def test_repair_evidence_fence_before_authoritative_close_is_rejected(self) -> None:
        text = self._plan()
        envelope, proposal, repair = self._repair_fixture(text)
        refs = {repair.block_id: "review/latest-rejection.txt"}
        candidate = render_candidate_plan(
            envelope=envelope, proposal=proposal, source_plan_text=text,
            generation_id="gen-rpos",
            partition_ids=("part-a", "part-b"),
            repair_evidence_blocks=(repair,),
            repair_evidence_artifact_references=refs,
        )
        assert self._validate(
            text, candidate, envelope, proposal,
            repair_evidence_blocks=(repair,),
            repair_evidence_artifact_references=refs,
            generation_id="gen-rpos", partition_ids=("part-a", "part-b"),
        ).valid

        # Find the authoritative and repair evidence fence blocks
        auth_opener = re.search(r"(?m)^([`~]{3,})aflow-authoritative-source\r?$", candidate)
        assert auth_opener is not None
        a_marker = auth_opener.group(1)
        a_close_start = candidate.index("\n" + a_marker + "\n", auth_opener.end()) + 1
        a_close_end = a_close_start + len(a_marker)
        auth_fence_block = candidate[auth_opener.start():a_close_end + 1]

        repair_opener = re.search(r"(?m)^([`~]{3,})aflow-repair-evidence\r?$", candidate)
        assert repair_opener is not None
        r_marker = repair_opener.group(1)
        r_close_start = candidate.index("\n" + r_marker + "\n", repair_opener.end()) + 1
        r_close_end = r_close_start + len(r_marker)
        repair_fence_block = candidate[repair_opener.start():r_close_end + 1]

        # Swap: put repair evidence fence before authoritative fence
        prefix = candidate[:auth_opener.start()]
        between = candidate[a_close_end + 1:repair_opener.start()]
        suffix = candidate[r_close_end + 1:]
        mutated = prefix + repair_fence_block + between + auth_fence_block + suffix

        result = self._validate(
            text, mutated, envelope, proposal,
            repair_evidence_blocks=(repair,),
            repair_evidence_artifact_references=refs,
            generation_id="gen-rpos", partition_ids=("part-a", "part-b"),
        )
        assert not result.valid
        assert any("repair_evidence_fence_not_immediately_after_authoritative" in issue for issue in result.issues)

    # ------------------------------------------------------------------
    # Repair-regression tests for the six failure areas in cp02-v01
    # ------------------------------------------------------------------

    def test_authoritative_fence_relocated_elsewhere_before_narrow_goal_is_rejected(self) -> None:
        """Finding 1: moving the authoritative fence away from its canonical
        position (immediately after metadata) must fail, even if it still
        appears before ``**Narrow Goal:**``."""
        text = self._plan()
        envelope, proposal = self._envelope_and_proposal(text)
        candidate = render_candidate_plan(
            envelope=envelope, proposal=proposal, source_plan_text=text,
            generation_id="gen-reloc",
            partition_ids=("part-a", "part-b"),
        )
        assert self._validate(
            text, candidate, envelope, proposal,
            generation_id="gen-reloc", partition_ids=("part-a", "part-b"),
        ).valid

        # Locate the fence block.
        opener = re.search(r"(?m)^([`~]{3,})aflow-authoritative-source\r?$", candidate)
        assert opener is not None
        marker = opener.group(1)
        close_start = candidate.index("\n" + marker + "\n", opener.end()) + 1
        close_end = close_start + len(marker)
        fence_block = candidate[opener.start():close_end + 1]

        # Find the metadata line end (the \n\n after the JSON comment).
        meta_prefix = "<!-- aflow-repartition-metadata "
        meta_start = candidate.index(meta_prefix)
        meta_end = candidate.index("\n", meta_start)
        # Insert the fence block at a position well after metadata but still
        # before ``**Narrow Goal:**``, with extra blank lines in between.
        narrow_idx = candidate.index("\n**Narrow Goal:**")

        # Remove fence from original position.
        without_fence = candidate[:opener.start()] + candidate[close_end + 2:]
        # Insert fence just before ``**Narrow Goal:**`` with an extra line.
        mutated = without_fence[:narrow_idx] + "\n\n<!-- injected gap -->\n" + fence_block + without_fence[narrow_idx:]

        result = self._validate(
            text, mutated, envelope, proposal,
            generation_id="gen-reloc", partition_ids=("part-a", "part-b"),
        )
        assert not result.valid
        assert any(
            "authoritative_fence_not_immediately_after_metadata" in issue
            or "authoritative_fence_invalid" in issue
            for issue in result.issues
        )

    def test_shortened_safe_fence_is_rejected(self) -> None:
        """Finding 2: shortening both sides of a deterministic six-tilde fence
        to five must be rejected."""
        text = (
            "# Plan\n\n### [ ] Checkpoint 1: Fence\n\n"
            "**Context:**\n\n~~~~~\nfive tildes inside\n~~~~~\n"
        )
        envelope, proposal = self._envelope_and_proposal(text)
        candidate = render_candidate_plan(
            envelope=envelope, proposal=proposal, source_plan_text=text,
            generation_id="gen-fence",
            partition_ids=("part-a", "part-b"),
        )
        # The renderer must choose a six-tilde fence (payload has five).
        assert "~~~~~~aflow-authoritative-source" in candidate

        result = self._validate(
            text, candidate, envelope, proposal,
            generation_id="gen-fence", partition_ids=("part-a", "part-b"),
        )
        assert result.valid

        # Shorten both sides to five tildes.
        mutated = candidate.replace("~~~~~~aflow-authoritative-source", "~~~~~aflow-authoritative-source")
        mutated = mutated.replace("\n~~~~~~\n", "\n~~~~~\n")
        result2 = self._validate(
            text, mutated, envelope, proposal,
            generation_id="gen-fence", partition_ids=("part-a", "part-b"),
        )
        assert not result2.valid
        assert any(
            "authoritative_fence_opener_mismatch" in issue
            for issue in result2.issues
        )

    def test_text_inserted_between_authoritative_and_repair_fences_is_rejected(self) -> None:
        """Finding 4: inserting text between the authoritative fence closer and
        the repair evidence fence opener must be rejected."""
        text = self._plan()
        envelope, proposal, repair = self._repair_fixture(text)
        refs = {repair.block_id: "review/latest-rejection.txt"}
        candidate = render_candidate_plan(
            envelope=envelope, proposal=proposal, source_plan_text=text,
            generation_id="gen-between",
            partition_ids=("part-a", "part-b"),
            repair_evidence_blocks=(repair,),
            repair_evidence_artifact_references=refs,
        )
        assert self._validate(
            text, candidate, envelope, proposal,
            repair_evidence_blocks=(repair,),
            repair_evidence_artifact_references=refs,
            generation_id="gen-between", partition_ids=("part-a", "part-b"),
        ).valid

        # Find the repair evidence opener and insert text just before it.
        repair_opener = re.search(r"(?m)^([`~]{3,})aflow-repair-evidence\r?$", candidate)
        assert repair_opener is not None
        mutated = (
            candidate[:repair_opener.start()]
            + "<!-- injected -->\n"
            + candidate[repair_opener.start():]
        )
        result = self._validate(
            text, mutated, envelope, proposal,
            repair_evidence_blocks=(repair,),
            repair_evidence_artifact_references=refs,
            generation_id="gen-between", partition_ids=("part-a", "part-b"),
        )
        assert not result.valid
        assert any(
            "repair_evidence_fence_not_immediately_after_authoritative" in issue
            for issue in result.issues
        )

    def test_compact_identity_removed_only_json_metadata_remains(self) -> None:
        """Finding 3: the redundant compact <!-- aflow-repartition ... -->
        identity has been removed.  Only the JSON metadata comment is the
        machine-readable identity, so the two representations cannot
        contradict."""
        text = self._plan()
        envelope, proposal = self._envelope_and_proposal(text)
        candidate = render_candidate_plan(
            envelope=envelope, proposal=proposal, source_plan_text=text,
            generation_id="gen-compact",
            partition_ids=("part-a", "part-b"),
        )
        # The old compact comment must be absent.
        assert "<!-- aflow-repartition child=" not in candidate
        # The JSON metadata must be present and parseable.
        assert "<!-- aflow-repartition-metadata " in candidate
        # Validate still passes.
        assert self._validate(
            text, candidate, envelope, proposal,
            generation_id="gen-compact", partition_ids=("part-a", "part-b"),
        ).valid

    def test_repair_reference_mapping_is_required_and_exact(self) -> None:
        """Controller references are required and exactly match evidence IDs."""
        text = self._plan()
        envelope, proposal = self._envelope_and_proposal(text)
        candidate = render_candidate_plan(
            envelope=envelope, proposal=proposal, source_plan_text=text,
            generation_id="gen-req",
            partition_ids=("part-a", "part-b"),
        )
        # Passing {} explicitly works.
        assert validate_candidate_mechanically(
            source_plan_text=text, candidate_plan_text=candidate,
            envelope=envelope, proposal=proposal,
            repair_evidence_artifact_references={},
            expected_generation_id="gen-req",
            expected_partition_ids=("part-a", "part-b"),
        ).valid
        # Omitting the argument entirely must raise TypeError.
        with pytest.raises(TypeError):
            validate_candidate_mechanically(  # type: ignore[call-arg]
                source_plan_text=text, candidate_plan_text=candidate,
                envelope=envelope, proposal=proposal,
                expected_generation_id="gen-req",
                expected_partition_ids=("part-a", "part-b"),
            )

        # A nonempty mapping is rejected when there is no repair evidence.
        with pytest.raises(ValueError, match="exactly one entry"):
            self._validate(
                text, candidate, envelope, proposal,
                repair_evidence_artifact_references={
                    "unexpected": "review/unexpected.txt",
                },
                generation_id="gen-req",
                partition_ids=("part-a", "part-b"),
            )

        # Nonempty evidence also rejects missing, extra, and unsafe references.
        envelope, proposal, repair = self._repair_fixture(text)
        refs = {repair.block_id: "review/latest-rejection.txt"}
        candidate = render_candidate_plan(
            envelope=envelope, proposal=proposal, source_plan_text=text,
            generation_id="gen-nonempty-refs",
            partition_ids=("part-a", "part-b"),
            repair_evidence_blocks=(repair,),
            repair_evidence_artifact_references=refs,
        )
        for invalid_refs in (
            {},
            {**refs, "unexpected": "review/unexpected.txt"},
            {repair.block_id: "review/unsafe\nreference.txt"},
        ):
            with pytest.raises(ValueError):
                self._validate(
                    text, candidate, envelope, proposal,
                    repair_evidence_blocks=(repair,),
                    repair_evidence_artifact_references=invalid_refs,
                    generation_id="gen-nonempty-refs",
                    partition_ids=("part-a", "part-b"),
                )

    def test_adjacent_bold_labels_produce_distinct_blocks(self) -> None:
        """Finding 6: adjacent **Goal:** and **Context:** lines must
        produce two distinct source blocks whose texts concatenate exactly."""
        text = (
            "# Plan\n\n"
            "### [ ] Checkpoint 1: Adjacent\n\n"
            "**Goal:** first section\n"
            "**Context:** second section\n\n"
            "- bullet\n"
        )
        sl = slice_checkpoint_source(text, checkpoint_index=1)
        assert sl is not None
        envelope_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        blocks = extract_source_blocks(
            sl, envelope_checkpoint_sha256=envelope_sha256, plan_text=text,
        )
        assert [block.section_label for block in blocks] == [
            "**Goal:**", "**Context:**", None,
        ]
        goal_block, context_block, bullet_block = blocks
        assert goal_block.section_label == "**Goal:**"
        assert goal_block.text == "**Goal:** first section\n"
        assert context_block.section_label == "**Context:**"
        assert context_block.text == "**Context:** second section\n\n"
        assert goal_block.byte_start == sl.body_byte_start
        assert goal_block.byte_end == context_block.byte_start
        assert context_block.byte_end == bullet_block.byte_start
        plan_bytes = text.encode("utf-8")
        for block in blocks:
            assert plan_bytes[block.byte_start:block.byte_end].decode("utf-8") == block.text
            assert block.content_sha256 == hashlib.sha256(
                block.text.encode("utf-8"),
            ).hexdigest()

        reconstructed = "".join(b.text for b in blocks)
        assert reconstructed == sl.body_text
        assert sl.heading_prefix + reconstructed == sl.full_text


# ---------------------------------------------------------------------------
# Checkpoint 2: Envelope lifecycle and resume carry tests
# ---------------------------------------------------------------------------

from aflow.run_state import (
    ActiveImplementationScope,
    ControllerState,
    ResumeContext,
    manager_resume_fields,
    manager_state_payload,
    restore_manager_state,
)
from aflow.plan import PlanSnapshot


class EnvelopeLifecycleTests(unittest.TestCase):
    """Tests for ActiveImplementationScope envelope fields and serialization."""

    def test_has_envelope_false_for_legacy_scope(self) -> None:
        scope = ActiveImplementationScope(
            scope_id="plan.md::checkpoint-1::test",
            original_plan_path="plan.md",
            checkpoint_index=1,
            checkpoint_name="Test",
            opened_turn_number=1,
        )
        assert scope.has_envelope is False
        assert scope.envelope_artifact_path is None

    def test_has_envelope_true_only_for_complete_reference(self) -> None:
        scope = ActiveImplementationScope(
            scope_id="plan.md::checkpoint-1::test",
            original_plan_path="plan.md",
            checkpoint_index=1,
            checkpoint_name="Test",
            opened_turn_number=1,
            envelope_artifact_path="scopes/abc123/envelope.json",
            envelope_artifact_sha256="a" * 64,
            envelope_canonical_sha256="b" * 64,
        )
        assert scope.has_envelope is True

        partial = replace(scope, envelope_canonical_sha256=None)
        assert partial.has_envelope is False

    def test_envelope_path_round_trip_via_manager_state(self) -> None:
        """envelope_artifact_path survives manager_state_payload → restore_manager_state."""
        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        state.active_implementation_scope = ActiveImplementationScope(
            scope_id="plan.md::checkpoint-2::test",
            original_plan_path="plan.md",
            checkpoint_index=2,
            checkpoint_name="Test",
            opened_turn_number=3,
            envelope_artifact_path="scopes/def456/envelope.json",
            envelope_artifact_sha256="a" * 64,
            envelope_canonical_sha256="b" * 64,
        )
        payload = manager_state_payload(state)
        assert payload["active_implementation_scope"]["envelope_artifact_path"] == (
            "scopes/def456/envelope.json"
        )
        assert payload["active_implementation_scope"]["envelope_artifact_sha256"] == "a" * 64
        assert payload["active_implementation_scope"]["envelope_canonical_sha256"] == "b" * 64

        restored = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        restore_manager_state(restored, payload)
        assert restored.active_implementation_scope is not None
        assert restored.active_implementation_scope.has_envelope is True
        assert restored.active_implementation_scope.envelope_artifact_path == (
            "scopes/def456/envelope.json"
        )
        assert restored.active_implementation_scope.envelope_artifact_sha256 == "a" * 64
        assert restored.active_implementation_scope.envelope_canonical_sha256 == "b" * 64

    def test_current_metadata_rejects_scope_without_envelope_reference(self) -> None:
        """Current durable metadata cannot publish an active scope without its envelope."""
        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        state.active_implementation_scope = ActiveImplementationScope(
            scope_id="plan.md::checkpoint-1::missing-envelope",
            original_plan_path="plan.md",
            checkpoint_index=1,
            checkpoint_name="Missing envelope",
            opened_turn_number=1,
        )
        with pytest.raises(ValueError, match="complete envelope references"):
            manager_state_payload(state)

    def test_manager_resume_fields_preserves_envelope_path(self) -> None:
        """manager_resume_fields carries envelope_artifact_path through to ResumeContext."""
        payload: dict[str, object] = {
            "manager_decision_number": 0,
            "manager_history": [],
            "semantic_stall_count": 0,
            "reviewer_rejection_count": 0,
            "implementation_attempts": {},
            "active_implementation_scope": {
                "scope_id": "plan.md::checkpoint-3::test",
                "original_plan_path": "plan.md",
                "checkpoint_index": 3,
                "checkpoint_name": "Test",
                "opened_turn_number": 5,
                "awaiting_review": False,
                "carried_reviewer_rejection_count": 0,
                "envelope_artifact_path": "scopes/ghi789/envelope.json",
                "envelope_artifact_sha256": "a" * 64,
                "envelope_canonical_sha256": "b" * 64,
            },
            "review_rejection_history": [],
            "pending_manager_notes": None,
            "pending_step_team_override": None,
            "pending_boundary_decision": None,
            "last_manager_report_path": None,
        }
        fields = manager_resume_fields(payload)
        scope = fields["active_implementation_scope"]
        assert scope is not None
        assert scope.envelope_artifact_path == "scopes/ghi789/envelope.json"
        assert scope.has_envelope is True

    def test_restore_preserves_malformed_reference_for_resume_rejection(self) -> None:
        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        restore_manager_state(state, {
            "active_implementation_scope": {
                "scope_id": "plan.md::checkpoint-1::legacy",
                "original_plan_path": "plan.md",
                "checkpoint_index": 1,
                "checkpoint_name": "Legacy",
                "opened_turn_number": 1,
                "carried_reviewer_rejection_count": 0,
                "envelope_artifact_path": 7,
                "envelope_artifact_sha256": "a" * 64,
                "envelope_canonical_sha256": "b" * 64,
            },
        })
        assert state.active_implementation_scope is not None
        assert state.active_implementation_scope.envelope_artifact_path == 7

    def test_resume_context_carries_envelope_source_path(self) -> None:
        rc = ResumeContext(
            resumed_from_run_id="run-123",
            feature_branch="feat",
            worktree_path=Path("/tmp/wt"),
            main_branch="main",
            setup=("echo hi",),
            teardown=("echo bye",),
            scope_envelope_source_path="/tmp/.aflow/runs/oldrun/scopes/abc/envelope.json",
        )
        assert rc.scope_envelope_source_path == (
            "/tmp/.aflow/runs/oldrun/scopes/abc/envelope.json"
        )

    def test_resume_context_defaults_envelope_source_to_none(self) -> None:
        rc = ResumeContext(
            resumed_from_run_id="run-456",
            feature_branch="feat",
            worktree_path=Path("/tmp/wt"),
            main_branch="main",
            setup=(),
            teardown=(),
        )
        assert rc.scope_envelope_source_path is None


class EnvelopeCaptureTests(unittest.TestCase):
    """Tests for _capture_scope_envelope integration with temp directories."""

    def test_existing_legacy_scope_is_not_a_new_capture_authority(self) -> None:
        from aflow.workflow import _open_implementation_scope

        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        legacy = ActiveImplementationScope(
            scope_id="plan.md::checkpoint-1::legacy",
            original_plan_path="/repo/plan.md",
            checkpoint_index=1,
            checkpoint_name="Checkpoint 1: First",
            opened_turn_number=1,
        )
        state.active_implementation_scope = legacy
        scope, was_opened = _open_implementation_scope(
            state,
            original_plan_path=Path("/repo/plan.md"),
            original_snapshot=PlanSnapshot("Checkpoint 1: First", 1, 1, False),
            turn_number=2,
        )
        assert was_opened is False
        assert scope == legacy
        assert scope.has_envelope is False

    def test_capture_writes_envelope_artifact(self) -> None:
        from aflow.workflow import _capture_scope_envelope

        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:**

            Do something.

            **Steps:**

            - [ ] step one
        """)
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            plan_path = run_dir / "plan.md"
            plan_path.write_text(text, encoding="utf-8")

            state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
            state.active_implementation_scope = ActiveImplementationScope(
                scope_id="plan.md::checkpoint-1::first",
                original_plan_path="plan.md",
                checkpoint_index=1,
                checkpoint_name="Checkpoint 1: First",
                opened_turn_number=1,
            )

            _capture_scope_envelope(
                state,
                plan_text=text,
                primary_plan_path=plan_path,
                run_dir=run_dir,
                exec_ctx=None,
            )

            scope = state.active_implementation_scope
            assert scope is not None
            assert scope.has_envelope is True
            assert scope.envelope_artifact_path is not None
            assert scope.envelope_artifact_path.startswith("scopes/")
            assert scope.envelope_artifact_path.endswith("/envelope.json")

            # Verify the artifact is readable
            artifact_path = run_dir / scope.envelope_artifact_path
            assert artifact_path.is_file()
            loaded = read_envelope(artifact_path)
            assert loaded is not None
            assert loaded.scope_id == "plan.md::checkpoint-1::first"


    def _source_scope(self, root: Path) -> tuple[ActiveImplementationScope, Path, bytes]:
        scope_id = "plan.md::checkpoint-1::first"
        plan_text = "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n"
        envelope = create_envelope(
            scope_id=scope_id,
            original_plan_path="plan.md",
            plan_text=plan_text,
            checkpoint_index=1,
        )
        artifact = write_envelope_atomic(envelope, root / "scopes" / envelope.scope_digest)
        raw = artifact.read_bytes()
        return (
            ActiveImplementationScope(
                scope_id=scope_id,
                original_plan_path="/repo/plan.md",
                checkpoint_index=1,
                checkpoint_name="Checkpoint 1: First",
                opened_turn_number=1,
                envelope_artifact_path=f"scopes/{envelope.scope_digest}/envelope.json",
                envelope_artifact_sha256=hashlib.sha256(raw).hexdigest(),
                envelope_canonical_sha256=envelope.canonical_envelope_sha256,
            ),
            artifact,
            raw,
        )

    def test_resume_reference_rejects_missing_partial_and_unsafe_paths(self) -> None:
        from aflow.workflow import WorkflowError, load_scope_envelope_for_resume

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scope, artifact, raw = self._source_scope(root)
            assert load_scope_envelope_for_resume(root, scope) == raw

            for invalid in (
                replace(scope, envelope_artifact_path=None),
                replace(scope, envelope_artifact_path="/tmp/envelope.json"),
                replace(scope, envelope_artifact_path="scopes/../envelope.json"),
                replace(scope, envelope_artifact_path="scopes\\unsafe\\envelope.json"),
                replace(scope, envelope_artifact_path="scopes/not-the-scope/envelope.json"),
            ):
                with pytest.raises(WorkflowError, match="envelope reference"):
                    load_scope_envelope_for_resume(root, invalid)

            artifact.unlink()
            with pytest.raises(WorkflowError, match="missing"):
                load_scope_envelope_for_resume(root, scope)

    def test_resume_reference_rejects_tampered_bytes_and_scope_binding(self) -> None:
        from aflow.workflow import WorkflowError, load_scope_envelope_for_resume

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scope, artifact, raw = self._source_scope(root)
            artifact.write_bytes(b"{not json")
            corrupt_scope = replace(
                scope,
                envelope_artifact_sha256=hashlib.sha256(b"{not json").hexdigest(),
            )
            with pytest.raises(WorkflowError, match="corrupt envelope"):
                load_scope_envelope_for_resume(root, corrupt_scope)

            artifact.write_bytes(raw)
            with pytest.raises(WorkflowError, match="bytes hash mismatch"):
                load_scope_envelope_for_resume(
                    root,
                    replace(scope, envelope_artifact_sha256="a" * 64),
                )
            with pytest.raises(WorkflowError, match="canonical hash mismatch"):
                load_scope_envelope_for_resume(
                    root,
                    replace(scope, envelope_canonical_sha256="a" * 64),
                )

            wrong_scope_envelope = create_envelope(
                scope_id="different-scope",
                original_plan_path="plan.md",
                plan_text="# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n",
                checkpoint_index=1,
            )
            wrong_raw = write_envelope_atomic(
                wrong_scope_envelope,
                root / "wrong",
            ).read_bytes()
            artifact.write_bytes(wrong_raw)
            wrong_reference = replace(
                scope,
                envelope_artifact_sha256=hashlib.sha256(wrong_raw).hexdigest(),
                envelope_canonical_sha256=wrong_scope_envelope.canonical_envelope_sha256,
            )
            with pytest.raises(WorkflowError, match="scope_id mismatch"):
                load_scope_envelope_for_resume(root, wrong_reference)

    def test_resume_reference_rejects_symlink_escape(self) -> None:
        from aflow.workflow import WorkflowError, load_scope_envelope_for_resume

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scope, artifact, raw = self._source_scope(root)
            outside = root.parent / f"{root.name}-outside-envelope.json"
            outside.write_bytes(raw)
            artifact.unlink()
            artifact.symlink_to(outside)
            with pytest.raises(WorkflowError, match="escapes source run"):
                load_scope_envelope_for_resume(root, scope)

    def test_capture_is_idempotent(self) -> None:
        from aflow.workflow import WorkflowError, _capture_scope_envelope

        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:** test
        """)
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            plan_path = run_dir / "plan.md"
            plan_path.write_text(text, encoding="utf-8")

            state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
            state.active_implementation_scope = ActiveImplementationScope(
                scope_id="plan.md::checkpoint-1::first",
                original_plan_path="plan.md",
                checkpoint_index=1,
                checkpoint_name="Checkpoint 1: First",
                opened_turn_number=1,
            )

            # First capture
            _capture_scope_envelope(
                state, plan_text=text, primary_plan_path=plan_path,
                run_dir=run_dir, exec_ctx=None,
            )
            first_path = state.active_implementation_scope.envelope_artifact_path

            # Second capture (simulates repair overlay keeping same scope)
            _capture_scope_envelope(
                state, plan_text=text, primary_plan_path=plan_path,
                run_dir=run_dir, exec_ctx=None,
            )
            second_path = state.active_implementation_scope.envelope_artifact_path

            assert first_path == second_path
            (run_dir / second_path).write_bytes(b"{tampered")
            with pytest.raises(WorkflowError, match="bytes hash mismatch"):
                _capture_scope_envelope(
                    state, plan_text=text, primary_plan_path=plan_path,
                    run_dir=run_dir, exec_ctx=None,
                )

    def test_capture_noop_when_no_active_scope(self) -> None:
        from aflow.workflow import _capture_scope_envelope

        with tempfile.TemporaryDirectory() as tmpdir:
            state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
            _capture_scope_envelope(
                state, plan_text="", primary_plan_path=Path(tmpdir) / "plan.md",
                run_dir=Path(tmpdir), exec_ctx=None,
            )
            assert state.active_implementation_scope is None

    def test_envelope_outlives_scope_recreation(self) -> None:
        """The artifact persists on disk even when state is rebuilt from JSON."""
        text = textwrap.dedent("""\
            # Plan

            ### [ ] Checkpoint 1: First

            **Goal:** test
        """)
        from aflow.workflow import _capture_scope_envelope

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            plan_path = run_dir / "plan.md"
            plan_path.write_text(text, encoding="utf-8")

            state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
            state.active_implementation_scope = ActiveImplementationScope(
                scope_id="plan.md::checkpoint-1::first",
                original_plan_path="plan.md",
                checkpoint_index=1,
                checkpoint_name="Checkpoint 1: First",
                opened_turn_number=1,
            )
            _capture_scope_envelope(
                state, plan_text=text, primary_plan_path=plan_path,
                run_dir=run_dir, exec_ctx=None,
            )

            # Serialize and restore state
            payload = manager_state_payload(state)
            restored = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
            restore_manager_state(restored, payload)

            # The restored scope should still reference the artifact
            scope = restored.active_implementation_scope
            assert scope is not None
            assert scope.has_envelope is True
            artifact_path = run_dir / scope.envelope_artifact_path
            assert artifact_path.is_file()

            # The restored artifact should validate
            loaded = read_envelope(artifact_path)
            assert loaded is not None
            assert loaded.scope_id == "plan.md::checkpoint-1::first"
