from aflow._test_support import *  # noqa: F401,F403

class PlanParserTests(unittest.TestCase):

    def test_parser_counts_only_checkpoint_section_checkboxes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n- [ ] ignored outside sections\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n- [x] step two\n\n### [x] Checkpoint 2: Done\n- [x] step three\n\n')
            parsed = load_plan(plan_path)
            assert parsed.snapshot.current_checkpoint_name == 'Checkpoint 1: First'
            assert parsed.snapshot.unchecked_checkpoint_count == 1
            assert parsed.snapshot.current_checkpoint_unchecked_step_count == 1
            assert not parsed.snapshot.is_complete

    def test_parser_rejects_checked_checkpoint_with_unchecked_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: Broken\n- [ ] step one\n')
            with pytest.raises(PlanParseError) as exc_info:
                load_plan(plan_path)
            exc = exc_info.value
            assert exc.checkpoint_name == 'Checkpoint 1: Broken'
            assert exc.unchecked_step_count == 1
            assert exc.checkpoint_index == 1
            assert exc.total_checkpoint_count == 1

    def test_parser_rejects_files_without_checkpoint_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# No checkpoints\n- [ ] ignored\n')
            with pytest.raises(PlanParseError):
                load_plan(plan_path)

    def test_startup_tolerant_loader_builds_recovery_snapshot_from_inconsistent_checkpoint_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(
                plan_path,
                '# Plan\n\n'
                '### [x] Checkpoint 1: Broken\n- [ ] step one\n\n'
                '### [ ] Checkpoint 2: Later\n- [ ] step two\n',
            )
            result = load_plan_tolerant(plan_path)
            assert result.parse_error is not None
            assert result.parse_error.error_kind == 'inconsistent_checkpoint_state'
            snapshot = result.parsed_plan.snapshot
            assert snapshot.current_checkpoint_name == 'Checkpoint 1: Broken'
            assert snapshot.current_checkpoint_index == 1
            assert snapshot.current_checkpoint_unchecked_step_count == 1
            assert snapshot.unchecked_checkpoint_count == 2
            assert snapshot.total_checkpoint_count == 2
            assert not snapshot.is_complete
            assert len(result.parsed_plan.sections) == 2

    def test_parser_total_checkpoint_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n\n### [ ] Checkpoint 2: Second\n- [ ] step two\n\n### [x] Checkpoint 3: Done\n- [x] step three\n')
            parsed = load_plan(plan_path)
            assert parsed.snapshot.total_checkpoint_count == 3

    def test_parser_current_checkpoint_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: Done\n- [x] step one\n\n### [ ] Checkpoint 2: Current\n- [ ] step two\n\n### [ ] Checkpoint 3: Pending\n- [ ] step three\n')
            parsed = load_plan(plan_path)
            assert parsed.snapshot.current_checkpoint_index == 2

    def test_parser_current_checkpoint_index_none_when_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: Done\n- [x] step one\n\n### [x] Checkpoint 2: Done\n- [x] step two\n')
            parsed = load_plan(plan_path)
            assert parsed.snapshot.is_complete
            assert parsed.snapshot.current_checkpoint_index is None

    def test_parser_global_section_after_last_checkpoint_does_not_affect_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(
                plan_path,
                '# Plan\n\n'
                '### [x] Checkpoint 1: First\n'
                '- [x] step one\n\n'
                '## Final Checklist\n'
                '- [ ] cleanup item one\n'
                '- [ ] cleanup item two\n',
            )
            parsed = load_plan(plan_path)
            assert parsed.snapshot.is_complete
            assert parsed.snapshot.current_checkpoint_name is None
            assert parsed.snapshot.unchecked_checkpoint_count == 0

    def test_parser_non_checkpoint_heading_ends_step_counting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(
                plan_path,
                '# Plan\n\n'
                '### [ ] Checkpoint 1: First\n'
                '- [ ] real step\n\n'
                '## Constraints\n'
                '- [ ] global constraint one\n'
                '- [ ] global constraint two\n',
            )
            parsed = load_plan(plan_path)
            assert parsed.snapshot.current_checkpoint_unchecked_step_count == 1

    def test_parser_unchecked_items_between_checkpoints_under_global_heading_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(
                plan_path,
                '# Plan\n\n'
                '### [x] Checkpoint 1: Done\n'
                '- [x] step one\n\n'
                '## Global Notes\n'
                '- [ ] global note\n\n'
                '### [ ] Checkpoint 2: Current\n'
                '- [ ] step two\n',
            )
            parsed = load_plan(plan_path)
            assert parsed.sections[0].unchecked_step_count == 0
            assert parsed.sections[1].unchecked_step_count == 1
            assert parsed.snapshot.current_checkpoint_unchecked_step_count == 1


class PlanParserFenceTests(unittest.TestCase):

    def test_parser_ignores_step_checkboxes_inside_backtick_fence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] real step\n\n```\n- [ ] fake inside fence\n```\n')
            parsed = load_plan(plan_path)
            assert parsed.snapshot.current_checkpoint_unchecked_step_count == 1

    def test_parser_ignores_step_checkboxes_inside_tilde_fence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] real step\n\n~~~\n- [ ] fake inside tilde fence\n~~~\n')
            parsed = load_plan(plan_path)
            assert parsed.snapshot.current_checkpoint_unchecked_step_count == 1

    def test_parser_ignores_checkpoint_heading_inside_fence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: Real\n- [ ] step one\n\n```\n### [ ] Checkpoint 2: Fake\n- [ ] fake step\n```\n')
            parsed = load_plan(plan_path)
            assert parsed.snapshot.total_checkpoint_count == 1
            assert parsed.snapshot.current_checkpoint_name == 'Checkpoint 1: Real'

    def test_parser_reopens_fence_after_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n\n```\n- [ ] fake\n```\n\n- [ ] step two\n')
            parsed = load_plan(plan_path)
            assert parsed.snapshot.current_checkpoint_unchecked_step_count == 2

    def test_plan_has_git_tracking_detects_heading_outside_fence(self) -> None:
        from aflow.plan import plan_has_git_tracking
        assert plan_has_git_tracking('# Plan\n\n## Git Tracking\n\nBase: abc\n')
        assert not plan_has_git_tracking('# Plan\n\n## No Tracking Here\n')

    def test_plan_has_git_tracking_ignores_heading_inside_fence(self) -> None:
        from aflow.plan import plan_has_git_tracking
        text = '# Plan\n\n### [ ] Checkpoint 1\n\n```\n## Git Tracking\n```\n'
        assert not plan_has_git_tracking(text)

    def test_parse_git_tracking_metadata_extract_fields_outside_fence(self) -> None:
        from aflow.plan import parse_git_tracking_metadata, GitTrackingMetadata
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: `main`
            - Pre-Handoff Base HEAD: `abc123`
            - Last Reviewed HEAD: `none`
            - Review Log:
              - None yet.

            ### [ ] Checkpoint 1
            - [ ] step
        ''')
        metadata = parse_git_tracking_metadata(text)
        assert metadata is not None
        assert metadata.plan_branch == 'main'
        assert metadata.pre_handoff_base_head == 'abc123'
        assert metadata.last_reviewed_head == 'none'
        assert metadata.review_log_entries == ('None yet.',)

    def test_parse_git_tracking_metadata_ignores_content_inside_fence(self) -> None:
        from aflow.plan import parse_git_tracking_metadata
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: `main`
            - Pre-Handoff Base HEAD: `abc123`

            ```
            ## Git Tracking
            - Plan Branch: `fake`
            - Pre-Handoff Base HEAD: `fake123`
            ```

            ### [ ] Checkpoint 1
            - [ ] step
        ''')
        metadata = parse_git_tracking_metadata(text)
        assert metadata is not None
        assert metadata.plan_branch == 'main'
        assert metadata.pre_handoff_base_head == 'abc123'

    def test_parse_git_tracking_metadata_returns_none_when_no_section(self) -> None:
        from aflow.plan import parse_git_tracking_metadata
        text = '# Plan\n\n### [ ] Checkpoint 1\n- [ ] step\n'
        metadata = parse_git_tracking_metadata(text)
        assert metadata is None

    def test_parse_git_tracking_metadata_handles_empty_values(self) -> None:
        from aflow.plan import parse_git_tracking_metadata
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: ``
            - Pre-Handoff Base HEAD: ``
            - Last Reviewed HEAD: ``
            - Review Log:
              - None yet.

            ### [ ] Checkpoint 1
            - [ ] step
        ''')
        metadata = parse_git_tracking_metadata(text)
        assert metadata is not None
        assert metadata.plan_branch == ''
        assert metadata.pre_handoff_base_head == ''
        assert metadata.last_reviewed_head == ''

    def test_parse_git_tracking_metadata_allows_missing_optional_review_fields(self) -> None:
        from aflow.plan import parse_git_tracking_metadata
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: ``
            - Pre-Handoff Base HEAD: ``

            ### [ ] Checkpoint 1
            - [ ] step
        ''')
        metadata = parse_git_tracking_metadata(text)
        assert metadata is not None
        assert metadata.plan_branch == ''
        assert metadata.pre_handoff_base_head == ''
        assert metadata.last_reviewed_head is None
        assert metadata.review_log_entries == ()

    def test_is_handoff_pristine_for_base_refresh_detects_pristine_handoff(self) -> None:
        from aflow.plan import parse_git_tracking_metadata, is_handoff_pristine_for_base_refresh
        from aflow.plan import _collect_sections
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: `main`
            - Pre-Handoff Base HEAD: `old`
            - Last Reviewed HEAD: `none`
            - Review Log:
              - None yet.

            ### [ ] Checkpoint 1
            - [ ] step one
            - [ ] step two

            ### [ ] Checkpoint 2
            - [ ] step three
        ''')
        metadata = parse_git_tracking_metadata(text)
        assert metadata is not None
        sections = _collect_sections(text, source_path=Path('plan.md'))
        assert is_handoff_pristine_for_base_refresh(metadata, sections) is True

    def test_is_handoff_pristine_for_base_refresh_allows_missing_optional_review_fields(self) -> None:
        from aflow.plan import parse_git_tracking_metadata, is_handoff_pristine_for_base_refresh
        from aflow.plan import _collect_sections
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: ``
            - Pre-Handoff Base HEAD: `old`

            ### [ ] Checkpoint 1
            - [ ] step one
            - [ ] step two
        ''')
        metadata = parse_git_tracking_metadata(text)
        assert metadata is not None
        sections = _collect_sections(text, source_path=Path('plan.md'))
        assert is_handoff_pristine_for_base_refresh(metadata, sections) is True

    def test_is_handoff_pristine_for_base_refresh_detects_started_handoff_by_reviewed_head(self) -> None:
        from aflow.plan import parse_git_tracking_metadata, is_handoff_pristine_for_base_refresh
        from aflow.plan import _collect_sections
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: `main`
            - Pre-Handoff Base HEAD: `old`
            - Last Reviewed HEAD: `abc123`
            - Review Log:
              - None yet.

            ### [ ] Checkpoint 1
            - [ ] step
        ''')
        metadata = parse_git_tracking_metadata(text)
        assert metadata is not None
        sections = _collect_sections(text, source_path=Path('plan.md'))
        assert is_handoff_pristine_for_base_refresh(metadata, sections) is False

    def test_is_handoff_pristine_for_base_refresh_detects_started_handoff_by_review_log(self) -> None:
        from aflow.plan import parse_git_tracking_metadata, is_handoff_pristine_for_base_refresh
        from aflow.plan import _collect_sections
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: `main`
            - Pre-Handoff Base HEAD: `old`
            - Last Reviewed HEAD: `none`
            - Review Log:
              - Some entry

            ### [ ] Checkpoint 1
            - [ ] step
        ''')
        metadata = parse_git_tracking_metadata(text)
        assert metadata is not None
        sections = _collect_sections(text, source_path=Path('plan.md'))
        assert is_handoff_pristine_for_base_refresh(metadata, sections) is False

    def test_is_handoff_pristine_for_base_refresh_detects_checked_checkpoint(self) -> None:
        from aflow.plan import parse_git_tracking_metadata, is_handoff_pristine_for_base_refresh
        from aflow.plan import _collect_sections
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: `main`
            - Pre-Handoff Base HEAD: `old`
            - Last Reviewed HEAD: `none`
            - Review Log:
              - None yet.

            ### [x] Checkpoint 1
            - [x] step

            ### [ ] Checkpoint 2
            - [ ] step
        ''')
        metadata = parse_git_tracking_metadata(text)
        assert metadata is not None
        sections = _collect_sections(text, source_path=Path('plan.md'))
        assert is_handoff_pristine_for_base_refresh(metadata, sections) is False

    def test_is_handoff_pristine_for_base_refresh_detects_checked_step(self) -> None:
        from aflow.plan import parse_git_tracking_metadata, is_handoff_pristine_for_base_refresh
        from aflow.plan import _collect_sections
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: `main`
            - Pre-Handoff Base HEAD: `old`
            - Last Reviewed HEAD: `none`
            - Review Log:
              - None yet.

            ### [ ] Checkpoint 1
            - [x] step one
            - [ ] step two
        ''')
        metadata = parse_git_tracking_metadata(text)
        assert metadata is not None
        sections = _collect_sections(text, source_path=Path('plan.md'))
        assert is_handoff_pristine_for_base_refresh(metadata, sections) is False

    def test_is_handoff_pristine_for_base_refresh_detects_empty_base_as_refreshable(self) -> None:
        from aflow.plan import parse_git_tracking_metadata, is_handoff_pristine_for_base_refresh
        from aflow.plan import _collect_sections
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: `main`
            - Pre-Handoff Base HEAD: ``
            - Last Reviewed HEAD: `none`
            - Review Log:
              - None yet.

            ### [ ] Checkpoint 1
            - [ ] step
        ''')
        metadata = parse_git_tracking_metadata(text)
        assert metadata is not None
        assert metadata.pre_handoff_base_head == ''
        sections = _collect_sections(text, source_path=Path('plan.md'))
        assert is_handoff_pristine_for_base_refresh(metadata, sections) is True

    def test_parse_git_tracking_metadata_rejects_multiple_live_sections(self) -> None:
        from aflow.plan import parse_git_tracking_metadata
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: `main`
            - Pre-Handoff Base HEAD: `old`
            - Last Reviewed HEAD: `none`
            - Review Log:
              - None yet.

            ### [ ] Checkpoint 1
            - [ ] step

            ## Git Tracking

            - Plan Branch: `main`
            - Pre-Handoff Base HEAD: `other`
        ''')
        with pytest.raises(ValueError, match='git tracking metadata is ambiguous'):
            parse_git_tracking_metadata(text)

    def test_rewrite_git_tracking_field_updates_only_target_field(self) -> None:
        from aflow.plan import rewrite_git_tracking_field
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: `main`
            - Pre-Handoff Base HEAD: `oldsha`
            - Last Reviewed HEAD: `none`
            - Review Log:
              - None yet.

            ### [ ] Checkpoint 1
            - [ ] step
        ''')
        updated = rewrite_git_tracking_field(text, 'Pre-Handoff Base HEAD', 'newsha123')
        assert 'Pre-Handoff Base HEAD: `newsha123`' in updated
        assert 'Plan Branch: `main`' in updated
        assert 'Last Reviewed HEAD: `none`' in updated
        assert 'oldsha' not in updated

    def test_rewrite_git_tracking_field_ignores_fence_content(self) -> None:
        from aflow.plan import rewrite_git_tracking_field
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: `main`
            - Pre-Handoff Base HEAD: `oldsha`

            ```
            ## Git Tracking
            - Pre-Handoff Base HEAD: `fakesha`
            ```

            ### [ ] Checkpoint 1
            - [ ] step
        ''')
        updated = rewrite_git_tracking_field(text, 'Pre-Handoff Base HEAD', 'newsha')
        assert 'Pre-Handoff Base HEAD: `newsha`' in updated
        assert 'fakesha' in updated  # Should still be there inside fence

    def test_rewrite_git_tracking_field_rejects_multiple_live_sections(self) -> None:
        from aflow.plan import rewrite_git_tracking_field
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: `main`
            - Pre-Handoff Base HEAD: `oldsha`

            ### [ ] Checkpoint 1
            - [ ] step

            ## Git Tracking

            - Plan Branch: `alt`
            - Pre-Handoff Base HEAD: `othersha`
        ''')
        with pytest.raises(ValueError, match='git tracking metadata is ambiguous'):
            rewrite_git_tracking_field(text, 'Pre-Handoff Base HEAD', 'newsha')

    def test_rewrite_git_tracking_field_noop_when_field_not_found(self) -> None:
        from aflow.plan import rewrite_git_tracking_field
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: `main`
            - Pre-Handoff Base HEAD: `oldsha`

            ### [ ] Checkpoint 1
            - [ ] step
        ''')
        updated = rewrite_git_tracking_field(text, 'NonExistent Field', 'value')
        assert updated == text

    def test_rewrite_git_tracking_field_updates_empty_value(self) -> None:
        from aflow.plan import rewrite_git_tracking_field
        text = textwrap.dedent('''\
            # Plan

            ## Git Tracking

            - Plan Branch: `main`
            - Pre-Handoff Base HEAD: ``
            - Last Reviewed HEAD: `none`
            - Review Log:
              - None yet.

            ### [ ] Checkpoint 1
            - [ ] step
        ''')
        updated = rewrite_git_tracking_field(text, 'Pre-Handoff Base HEAD', 'newsha123')
        assert 'Pre-Handoff Base HEAD: `newsha123`' in updated
        assert 'oldsha' not in updated
        # Verify newline is preserved
        lines = updated.split('\n')
        for i, line in enumerate(lines):
            if 'Pre-Handoff Base HEAD: `newsha123`' in line:
                # The next line should be '- Last Reviewed HEAD: `none`'
                assert i + 1 < len(lines)
                assert 'Last Reviewed HEAD: `none`' in lines[i + 1]
                break

    def test_generate_new_plan_path_none_checkpoint_uses_cp01(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            original = Path(tmpdir) / 'plan.md'
            original.write_text('dummy', encoding='utf-8')
            result = generate_new_plan_path(original, checkpoint_index=None)
            assert result.name == 'plan-cp01-v01.md'

    def test_generate_new_plan_path_zero_checkpoint_uses_cp00(self) -> None:
        # Verifies `1 if checkpoint_index is None` - 0 is not None, keeps 0
        with tempfile.TemporaryDirectory() as tmpdir:
            original = Path(tmpdir) / 'plan.md'
            original.write_text('dummy', encoding='utf-8')
            result = generate_new_plan_path(original, checkpoint_index=0)
            assert result.name == 'plan-cp00-v01.md'


class ReadmeDerivationTests(unittest.TestCase):
    """Unit tests for derive_readme_content — pure function, no git or file I/O."""

    def test_readme_derivation_summary_section_extracted(self) -> None:
        plan = textwrap.dedent("""\
            # My Feature

            ## Summary

            This is a summary of what the feature does.
            It spans multiple lines.

            ## Git Tracking

            - Branch: main

            ### [ ] Checkpoint 1: First
            - [ ] do thing
        """)
        title, body = derive_readme_content(plan, "my-feature")
        assert title == "My Feature"
        assert "This is a summary of what the feature does." in body
        assert "spans multiple lines" in body
        assert "Git Tracking" not in body
        assert "Checkpoint" not in body

    def test_readme_derivation_falls_back_to_prose_paragraph(self) -> None:
        plan = textwrap.dedent("""\
            # My Feature

            This is a prose description of the feature.
            It continues here.

            - list item one
            - list item two

            ### [ ] Checkpoint 1: First
            - [ ] step
        """)
        title, body = derive_readme_content(plan, "my-feature")
        assert title == "My Feature"
        assert "prose description" in body
        assert "list item" not in body

    def test_readme_derivation_uses_fallback_sentence_for_structured_only(self) -> None:
        plan = textwrap.dedent("""\
            # My Feature

            - only list items here
            - no prose paragraph

            ### [ ] Checkpoint 1: First
            - [ ] step
        """)
        title, body = derive_readme_content(plan, "my-feature")
        assert title == "My Feature"
        assert 'being initialized from the aflow plan "My Feature"' in body

    def test_readme_derivation_humanizes_stem_when_no_h1(self) -> None:
        plan = textwrap.dedent("""\
            ## Summary

            A summary without a top-level heading.
        """)
        title, body = derive_readme_content(plan, "my-cool-feature")
        assert title == "My Cool Feature"
        assert "A summary without a top-level heading." in body

    def test_readme_derivation_skips_fenced_code_blocks(self) -> None:
        plan = textwrap.dedent("""\
            # My Feature

            ```
            This looks like prose but is inside a fence.
            It should be skipped.
            ```

            Actual prose paragraph here.

            ### [ ] Checkpoint 1: First
            - [ ] step
        """)
        title, body = derive_readme_content(plan, "my-feature")
        assert title == "My Feature"
        assert "Actual prose paragraph here." in body
        assert "looks like prose but is inside a fence" not in body

    def test_readme_derivation_skips_git_tracking_section(self) -> None:
        plan = textwrap.dedent("""\
            # My Feature

            ## Git Tracking

            Tracking info that should not appear in README.

            ## Another Section

            Real prose here that should be used.

            ### [ ] Checkpoint 1: First
            - [ ] step
        """)
        title, body = derive_readme_content(plan, "my-feature")
        assert "Tracking info" not in body
        assert "Real prose here" in body

    def test_readme_derivation_skips_critical_invariants_section(self) -> None:
        plan = textwrap.dedent("""\
            # My Feature

            ## Critical Invariants

            Must not appear in README.

            ## Overview

            This is the actual description.
        """)
        title, body = derive_readme_content(plan, "my-feature")
        assert "Must not appear" not in body
        assert "actual description" in body

    def test_readme_derivation_empty_summary_falls_back_to_prose(self) -> None:
        plan = textwrap.dedent("""\
            # My Feature

            ## Summary

            ## Overview

            This prose appears after an empty summary section.
        """)
        title, body = derive_readme_content(plan, "my-feature")
        assert "This prose appears" in body


class ActivePlanLifecycleTests(unittest.TestCase):

    def test_fix_plan_resets_to_original_after_review_without_new_plan(self) -> None:
        # 3-step workflow with all unconditional transitions so turn 3 always runs,
        # even after DONE becomes true at turn 2.  This lets us verify the invariant:
        # when implement completes the original plan but creates no new fix plan,
        # the following step sees original_plan as active (not the previous fix plan).
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            fix_plan = repo_root / 'plan-cp01-v01.md'
            captured_active: list[str] = []
            turn_counter = [0]

            def capturing_runner(argv, **kwargs):
                turn_counter[0] += 1
                prompt = ' '.join(argv)
                import re as _re
                m = _re.search(r'Active: (\S+)', prompt)
                if m:
                    captured_active.append(m.group(1).rstrip('.'))
                if turn_counter[0] == 1:
                    # review: create fix plan, original stays incomplete
                    fix_plan.write_text('# Fix\n\n### [x] CP: done\n- [x] s\n', encoding='utf-8')
                elif turn_counter[0] == 2:
                    # implement: work from fix plan, complete original — no new plan written
                    _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
                # turn 3 (second_review): does NOT create a new plan
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='m')})},
                workflows={'loop': WorkflowConfig(
                    steps={
                        'review': WorkflowStepConfig(
                            role='architect',
                            prompts=('rp',),
                            go=(GoTransition(to='implement'),),
                        ),
                        'implement': WorkflowStepConfig(
                            role='architect',
                            prompts=('ip',),
                            go=(GoTransition(to='second_review'),),
                        ),
                        'second_review': WorkflowStepConfig(
                            role='architect',
                            prompts=('rp',),
                            go=(GoTransition(to='END'),),
                        ),
                    },
                    first_step='review',
                )},
                prompts={
                    'rp': 'Active: {ACTIVE_PLAN_PATH}.',
                    'ip': 'Active: {ACTIVE_PLAN_PATH}.',
                },
            )
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            result = run_workflow(controller_config, wf_config, 'loop', config_dir=config_dir, adapter=CodexAdapter(), runner=capturing_runner)
            assert result.turns_completed == 3
            # Turn 1 (review): active should be original plan
            assert captured_active[0] == str(plan_path)
            # Turn 2 (implement): active should be fix plan (review created it in turn 1)
            assert captured_active[1] == str(fix_plan)
            # Turn 3 (second_review): active must reset to original — not the stale fix plan
            assert captured_active[2] == str(plan_path)

    def test_repair_plan_is_preserved_until_review_approves(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            fix_v01 = repo_root / 'plan-cp01-v01.md'
            fix_v02 = repo_root / 'plan-cp01-v02.md'
            captured_active: list[str] = []
            next_turn_run_state: dict[int, str] = {}
            turn_counter = [0]

            _write_plan(
                plan_path,
                '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n',
            )

            def capturing_runner(argv, **kwargs):
                turn_counter[0] += 1
                turn = turn_counter[0]
                prompt = ' '.join(argv)
                import re as _re
                match = _re.search(r'Active: (\S+)', prompt)
                assert match is not None
                captured_active.append(match.group(1).rstrip('.'))

                run_files = list((repo_root / '.aflow' / 'runs').glob('*/run.json'))
                if turn > 1 and run_files:
                    run_payload = json.loads(
                        run_files[0].read_text(encoding='utf-8')
                    )
                    next_turn_run_state[turn] = run_payload['active_plan_path']

                if turn == 1:
                    fix_v01.write_text('# Repair v01\n', encoding='utf-8')
                elif turn == 3:
                    fix_v02.write_text('# Repair v02\n', encoding='utf-8')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={
                    'codex': WorkflowHarnessConfig(
                        profiles={'default': HarnessProfileConfig(model='m')}
                    )
                },
                workflows={
                    'loop': WorkflowConfig(
                        steps={
                            'review': WorkflowStepConfig(
                                role='architect',
                                prompts=('p',),
                                go=(
                                    GoTransition(
                                        to='rework', when='NEW_PLAN_EXISTS'
                                    ),
                                    GoTransition(to='implement'),
                                ),
                            ),
                            'rework': WorkflowStepConfig(
                                role='architect',
                                prompts=('p',),
                                go=(
                                    GoTransition(
                                        to='review',
                                        preserve_active_plan=True,
                                    ),
                                ),
                            ),
                            'implement': WorkflowStepConfig(
                                role='architect',
                                prompts=('p',),
                                go=(GoTransition(to='END'),),
                            ),
                        },
                        first_step='review',
                    )
                },
                prompts={'p': 'Active: {ACTIVE_PLAN_PATH}.'},
            )

            result = run_workflow(
                ControllerConfig(
                    repo_root=repo_root,
                    plan_path=plan_path,
                    max_turns=6,
                ),
                wf_config,
                'loop',
                config_dir=repo_root,
                adapter=CodexAdapter(),
                runner=capturing_runner,
            )

            assert result.turns_completed == 6
            assert captured_active == [
                str(plan_path),
                str(fix_v01),
                str(fix_v01),
                str(fix_v02),
                str(fix_v02),
                str(plan_path),
            ]
            assert next_turn_run_state[3] == str(fix_v01)
            assert next_turn_run_state[5] == str(fix_v02)
            assert next_turn_run_state[6] == str(plan_path)

            run_dir = next((repo_root / '.aflow' / 'runs').iterdir())
            expected = [
                plan_path,
                fix_v01,
                fix_v01,
                fix_v02,
                fix_v02,
                plan_path,
            ]
            for turn, expected_path in enumerate(expected, 1):
                payload = json.loads(
                    (
                        run_dir
                        / 'turns'
                        / f'turn-{turn:03d}'
                        / 'result.json'
                    ).read_text(encoding='utf-8')
                )
                assert payload['active_plan_path'] == str(expected_path)
            for turn in (2, 4):
                payload = json.loads(
                    (
                        run_dir
                        / 'turns'
                        / f'turn-{turn:03d}'
                        / 'result.json'
                    ).read_text(encoding='utf-8')
                )
                assert payload['conditions']['NEW_PLAN_EXISTS'] is False

    def test_preserving_transition_rejects_missing_active_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            fix_plan = repo_root / 'plan-cp01-v01.md'
            calls = [0]
            _write_plan(
                plan_path,
                '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n',
            )

            def runner(argv, **kwargs):
                calls[0] += 1
                if calls[0] == 1:
                    fix_plan.write_text('# Repair\n', encoding='utf-8')
                elif calls[0] == 2:
                    fix_plan.unlink()
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={
                    'codex': WorkflowHarnessConfig(
                        profiles={'default': HarnessProfileConfig(model='m')}
                    )
                },
                workflows={
                    'loop': WorkflowConfig(
                        steps={
                            'review': WorkflowStepConfig(
                                role='architect',
                                prompts=('p',),
                                go=(GoTransition(to='rework'),),
                            ),
                            'rework': WorkflowStepConfig(
                                role='architect',
                                prompts=('p',),
                                go=(
                                    GoTransition(
                                        to='review',
                                        preserve_active_plan=True,
                                    ),
                                ),
                            ),
                        },
                        first_step='review',
                    )
                },
                prompts={'p': 'Active: {ACTIVE_PLAN_PATH}.'},
            )

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=4,
                    ),
                    wf_config,
                    'loop',
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=runner,
                )
            assert calls[0] == 2
            assert 'cannot preserve active plan' in str(ctx.value)
            assert str(fix_plan) in str(ctx.value)
