from __future__ import annotations

import pytest
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from aflow.terminal_viewport import (
    IncrementalKeyDecoder,
    ScrollableViewport,
    ViewportAction,
    ViewportModel,
)


def _render_segments(viewport: ScrollableViewport, *, width: int, height: int) -> list[Segment]:
    console = Console(width=width, height=height, force_terminal=False)
    return list(console.render(viewport))


def _segment_lines(segments: list[Segment]) -> list[list[Segment]]:
    return [list(line) for line in Segment.split_lines(segments)]


def _line_text(line: list[Segment]) -> str:
    return "".join(segment.text for segment in line if not segment.control)


def test_model_defaults_to_follow_tail_and_clamps_dimensions() -> None:
    model = ViewportModel()
    assert model.follow_tail is True
    assert model.offset == 0

    model.update_dimensions(content_height=10, viewport_height=4)
    assert model.offset == 6
    assert model.max_offset == 6

    model.update_dimensions(content_height=2, viewport_height=8)
    assert model.offset == 0
    assert model.max_offset == 0


def test_line_and_page_actions_disable_follow_tail_and_use_page_distance() -> None:
    model = ViewportModel()
    model.update_dimensions(content_height=20, viewport_height=5)
    assert model.offset == 15

    model.apply(ViewportAction.LINE_UP)
    assert (model.offset, model.follow_tail) == (14, False)
    model.apply(ViewportAction.PAGE_UP)
    assert (model.offset, model.follow_tail) == (10, False)
    model.apply(ViewportAction.PAGE_DOWN)
    assert (model.offset, model.follow_tail) == (14, False)
    model.apply(ViewportAction.TOP)
    assert (model.offset, model.follow_tail) == (0, False)
    model.apply(ViewportAction.PAGE_DOWN)
    assert (model.offset, model.follow_tail) == (4, False)


def test_small_viewport_uses_at_least_one_row_for_page_actions() -> None:
    model = ViewportModel(offset=3, content_height=10, viewport_height=1, follow_tail=False)
    model.apply(ViewportAction.PAGE_UP)
    assert model.offset == 2
    model.apply(ViewportAction.PAGE_DOWN)
    assert model.offset == 3


def test_bottom_restores_follow_tail_after_manual_navigation() -> None:
    model = ViewportModel()
    model.update_dimensions(content_height=12, viewport_height=4)
    model.apply(ViewportAction.TOP)
    assert model.follow_tail is False
    model.apply(ViewportAction.BOTTOM)
    assert (model.offset, model.follow_tail) == (8, True)

    model.set_content_height(20)
    assert model.offset == 16


def test_follow_tail_moves_with_appended_content_but_manual_position_stays_stable() -> None:
    model = ViewportModel()
    model.update_dimensions(content_height=8, viewport_height=3)
    model.apply(ViewportAction.TOP)
    model.set_content_height(12)
    assert (model.offset, model.follow_tail) == (0, False)

    model.apply(ViewportAction.BOTTOM)
    model.set_content_height(16)
    assert (model.offset, model.follow_tail) == (13, True)


def test_renderable_reserves_footer_and_pads_short_content() -> None:
    viewport = ScrollableViewport(Text("one\ntwo"))
    lines = _segment_lines(_render_segments(viewport, width=20, height=5))
    assert len(lines) == 5
    assert _line_text(lines[0]).startswith("one")
    assert _line_text(lines[1]).startswith("two")
    assert _line_text(lines[2]) == " " * 20
    assert _line_text(lines[3]) == " " * 20
    assert "1-2/2 [follow]" in _line_text(lines[4])


def test_renderable_slices_long_content_and_g_end_reaches_new_tail() -> None:
    source = Text("\n".join(f"line-{index}" for index in range(8)))
    model = ViewportModel()
    viewport = ScrollableViewport(source, model=model)

    lines = _segment_lines(_render_segments(viewport, width=20, height=4))
    assert [line_text for line_text in map(_line_text, lines[:3])] == [
        "line-5".ljust(20),
        "line-6".ljust(20),
        "line-7".ljust(20),
    ]
    assert "6-8/8" in _line_text(lines[3])

    model.apply(ViewportAction.TOP)
    source.append("\nline-8")
    lines = _segment_lines(_render_segments(viewport, width=20, height=4))
    assert _line_text(lines[0]).startswith("line-0")
    assert model.follow_tail is False
    assert "1-3/9" in _line_text(lines[3])

    model.apply(ViewportAction.BOTTOM)
    lines = _segment_lines(_render_segments(viewport, width=20, height=4))
    assert _line_text(lines[2]).startswith("line-8")
    assert "7-9/9 [follow]" in _line_text(lines[3])


def test_renderable_wraps_source_lines_at_narrow_width_before_slicing() -> None:
    viewport = ScrollableViewport(Text("abcdefghij"))
    lines = _segment_lines(_render_segments(viewport, width=4, height=4))

    assert [_line_text(line) for line in lines[:3]] == ["abcd", "efgh", "ij  "]
    assert _line_text(lines[3]) == "1-3/"


def test_resize_clamps_manual_offset_and_keeps_footer_at_bottom() -> None:
    source = Text("\n".join(f"line-{index}" for index in range(10)))
    model = ViewportModel()
    viewport = ScrollableViewport(source, model=model)
    model.update_dimensions(content_height=10, viewport_height=2)
    model.apply(ViewportAction.TOP)

    lines = _segment_lines(_render_segments(viewport, width=18, height=6))
    assert len(lines) == 6
    assert model.viewport_height == 5
    assert _line_text(lines[-1]).startswith("1-5/10")

    model.offset = 99
    lines = _segment_lines(_render_segments(viewport, width=18, height=3))
    assert model.offset == 8
    assert _line_text(lines[-1]).startswith("9-10/10")


def test_renderable_preserves_segment_styles_without_string_round_trip() -> None:
    source = Text()
    source.append("red", style="red")
    source.append(" blue", style="blue")
    viewport = ScrollableViewport(source)
    lines = _segment_lines(_render_segments(viewport, width=20, height=3))

    content_segments = [segment for segment in lines[0] if segment.text.strip()]
    assert content_segments[0].text == "red"
    assert content_segments[0].style == Style(color="red")
    assert content_segments[1].text == " blue"
    assert content_segments[1].style == Style(color="blue")


def test_decoder_handles_single_keys_and_ignores_q_escape_and_utf8() -> None:
    decoder = IncrementalKeyDecoder()
    assert decoder.feed(b"kjbf gG") == (
        ViewportAction.LINE_UP,
        ViewportAction.LINE_DOWN,
        ViewportAction.PAGE_UP,
        ViewportAction.PAGE_DOWN,
        ViewportAction.PAGE_DOWN,
        ViewportAction.TOP,
        ViewportAction.BOTTOM,
    )
    assert decoder.feed("q\u2603") == ()
    assert decoder.feed(b"\x1b") == ()
    assert decoder.pending == b"\x1b"
    assert decoder.feed(b"j") == (ViewportAction.LINE_DOWN,)


def test_decoder_handles_split_arrow_page_home_and_end_sequences() -> None:
    decoder = IncrementalKeyDecoder()
    assert decoder.feed(b"\x1b") == ()
    assert decoder.feed(b"[") == ()
    assert decoder.feed(b"A") == (ViewportAction.LINE_UP,)
    assert decoder.feed(b"\x1b[5") == ()
    assert decoder.feed(b"~") == (ViewportAction.PAGE_UP,)
    assert decoder.feed(b"\x1b[6~\x1b[1~\x1b[4~") == (
        ViewportAction.PAGE_DOWN,
        ViewportAction.TOP,
        ViewportAction.BOTTOM,
    )
    assert decoder.feed(b"\x1b[Z") == ()
    assert decoder.feed(b"j") == (ViewportAction.LINE_DOWN,)


def test_decoder_discards_malformed_sequences_without_losing_following_keys() -> None:
    decoder = IncrementalKeyDecoder()
    assert decoder.feed(b"\x1b[999~j") == (ViewportAction.LINE_DOWN,)
    assert decoder.feed(b"\x1bO") == ()
    assert decoder.feed(b"B") == (ViewportAction.LINE_DOWN,)


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        (b"\x1b[A", ViewportAction.LINE_UP),
        (b"\x1b[B", ViewportAction.LINE_DOWN),
        (b"\x1b[5~", ViewportAction.PAGE_UP),
        (b"\x1b[6~", ViewportAction.PAGE_DOWN),
        (b"\x1b[H", ViewportAction.TOP),
        (b"\x1b[1~", ViewportAction.TOP),
        (b"\x1b[F", ViewportAction.BOTTOM),
        (b"\x1b[4~", ViewportAction.BOTTOM),
        (b"\x1bOA", ViewportAction.LINE_UP),
        (b"\x1bOB", ViewportAction.LINE_DOWN),
        (b"\x1bOH", ViewportAction.TOP),
        (b"\x1bOF", ViewportAction.BOTTOM),
    ],
)
def test_decoder_supported_sequences_are_chunk_boundary_invariant(
    sequence: bytes,
    expected: ViewportAction,
) -> None:
    assert IncrementalKeyDecoder().feed(sequence) == (expected,)

    for split in range(1, len(sequence)):
        decoder = IncrementalKeyDecoder()
        assert decoder.feed(sequence[:split]) == ()
        assert decoder.feed(sequence[split:]) == (expected,)


@pytest.mark.parametrize(
    "sequence",
    [
        b"\x1b[2G",
        b"\x1b[9j",
        b"\x1b[Z",
        b"\x1bOZ",
    ],
)
def test_decoder_discards_unsupported_sequences_atomically_across_splits(
    sequence: bytes,
) -> None:
    assert IncrementalKeyDecoder().feed(sequence) == ()

    for split in range(1, len(sequence)):
        decoder = IncrementalKeyDecoder()
        assert decoder.feed(sequence[:split]) == ()
        assert decoder.feed(sequence[split:]) == ()
        assert decoder.feed(b"j") == (ViewportAction.LINE_DOWN,)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\x1b[2Gg", (ViewportAction.TOP,)),
        (b"\x1bOZg", (ViewportAction.TOP,)),
        (b"\x1b[\x01g", (ViewportAction.TOP,)),
        (b"\x1b[\x1b[A", (ViewportAction.LINE_UP,)),
        ("☃j".encode("utf-8"), (ViewportAction.LINE_DOWN,)),
        (
            b"\x1b[A\x1b[2Gg\x1bOB",
            (ViewportAction.LINE_UP, ViewportAction.TOP, ViewportAction.LINE_DOWN),
        ),
    ],
)
def test_decoder_is_chunk_boundary_invariant_for_malformed_utf8_and_concatenated_input(
    data: bytes,
    expected: tuple[ViewportAction, ...],
) -> None:
    assert IncrementalKeyDecoder().feed(data) == expected

    for split in range(1, len(data)):
        decoder = IncrementalKeyDecoder()
        split_actions = decoder.feed(data[:split]) + decoder.feed(data[split:])
        assert split_actions == expected


def test_decoder_bounds_unterminated_escape_sequences_and_recovers() -> None:
    decoder = IncrementalKeyDecoder()
    oversized = b"\x1b[" + b"1" * decoder._MAX_SEQUENCE_BYTES

    assert decoder.feed(oversized) == ()
    assert decoder.pending == b""
    assert decoder.feed(b"j") == (ViewportAction.LINE_DOWN,)
