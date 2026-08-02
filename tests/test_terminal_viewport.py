from __future__ import annotations

import errno
import os
import pty
import pytest
import subprocess
import sys
import termios
import time
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
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


def _drain_pty(fd: int, *, timeout: float = 0.5) -> bytes:
    os.set_blocking(fd, False)
    output = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            time.sleep(0.01)
            continue
        except OSError as exc:
            if exc.errno == errno.EIO:
                break
            raise
        if chunk:
            output.extend(chunk)
            continue
        time.sleep(0.01)
    return bytes(output)


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


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX terminal attributes")
def test_terminal_input_session_uses_cbreak_preserves_isig_and_restores_attributes() -> None:
    from aflow.terminal_viewport import TerminalInputSession

    master_fd, slave_fd = pty.openpty()

    class PtyInput:
        def fileno(self) -> int:
            return slave_fd

        def isatty(self) -> bool:
            return os.isatty(slave_fd)

    class FakeConsole:
        is_terminal = True
        size = SimpleNamespace(width=80, height=24)

    before = termios.tcgetattr(slave_fd)
    session = TerminalInputSession(FakeConsole(), input_stream=PtyInput())
    try:
        session.start()
        during = termios.tcgetattr(slave_fd)
        assert not during[3] & termios.ICANON
        assert not during[3] & termios.ECHO
        assert during[3] & termios.ISIG

        os.write(master_fd, b"\x1b[A")
        deadline = time.monotonic() + 1.0
        events: tuple[object, ...] = ()
        while time.monotonic() < deadline and not events:
            events = session.drain_events()
            time.sleep(0.01)
        assert events == (ViewportAction.LINE_UP,)
    finally:
        session.close()
        session.close()
        after = termios.tcgetattr(slave_fd)
        os.close(master_fd)
        os.close(slave_fd)

    assert after == before
    assert session.is_restored
    assert session.thread is None


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX terminal attributes")
def test_terminal_input_session_enqueues_only_real_size_changes() -> None:
    from aflow.terminal_viewport import TerminalInputSession, ViewportEvent

    master_fd, slave_fd = pty.openpty()

    class PtyInput:
        def fileno(self) -> int:
            return slave_fd

        def isatty(self) -> bool:
            return True

    class MutableConsole:
        is_terminal = True
        size = SimpleNamespace(width=80, height=24)

    console = MutableConsole()
    session = TerminalInputSession(console, input_stream=PtyInput())
    session._SELECT_TIMEOUT_SECONDS = 0.01
    try:
        session.start()
        time.sleep(0.03)
        assert session.drain_events() == ()
        console.size = SimpleNamespace(width=100, height=30)
        deadline = time.monotonic() + 1.0
        events: tuple[object, ...] = ()
        while time.monotonic() < deadline and not events:
            events = session.drain_events()
            time.sleep(0.01)
        assert events == (ViewportEvent.RESIZE,)
        time.sleep(0.03)
        assert session.drain_events() == ()
    finally:
        session.close()
        os.close(master_fd)
        os.close(slave_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX terminal attributes")
def test_terminal_input_session_restores_attributes_after_setup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from aflow.terminal_viewport import TerminalInputSession

    master_fd, slave_fd = pty.openpty()

    class PtyInput:
        def fileno(self) -> int:
            return slave_fd

        def isatty(self) -> bool:
            return True

    class FakeConsole:
        is_terminal = True
        size = SimpleNamespace(width=80, height=24)

    def fail_cbreak(fd: int) -> None:
        del fd
        raise OSError("synthetic cbreak failure")

    before = termios.tcgetattr(slave_fd)
    monkeypatch.setattr("aflow.terminal_viewport.tty.setcbreak", fail_cbreak)
    session = TerminalInputSession(FakeConsole(), input_stream=PtyInput())
    with pytest.raises(OSError, match="synthetic cbreak failure"):
        session.start()
    after = termios.tcgetattr(slave_fd)

    session.close()
    os.close(master_fd)
    os.close(slave_fd)

    assert after == before
    assert session.is_restored
    assert session.thread is None


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX terminal attributes")
def test_terminal_input_session_close_restores_after_interrupted_reader_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aflow.terminal_viewport import TerminalInputSession

    class FakeConsole:
        is_terminal = True

    session = TerminalInputSession(FakeConsole(), input_stream=object())
    session._fd = 17
    session._saved_attributes = ["saved"]
    session._started = True
    session._atexit_registered = True
    session._thread = object()
    stop_calls = 0
    restore_calls = 0
    unregister_calls = 0

    def stop_reader() -> None:
        nonlocal stop_calls
        stop_calls += 1
        if stop_calls == 1:
            raise KeyboardInterrupt("reader stop interrupt")
        session._thread = None

    def tcsetattr(fd: int, action: int, attributes: object) -> None:
        del fd, action, attributes
        nonlocal restore_calls
        restore_calls += 1

    def unregister(callback: object) -> None:
        del callback
        nonlocal unregister_calls
        unregister_calls += 1

    monkeypatch.setattr(session, "stop_reader", stop_reader)
    monkeypatch.setattr("aflow.terminal_viewport.termios.tcsetattr", tcsetattr)
    monkeypatch.setattr("aflow.terminal_viewport.atexit.unregister", unregister)

    with pytest.raises(KeyboardInterrupt, match="reader stop interrupt"):
        session.close()

    assert restore_calls == 1
    assert unregister_calls == 1
    assert stop_calls == 2
    assert session.is_restored
    assert session.thread is None
    assert session._atexit_registered is False
    assert session.is_started is False
    assert session.is_settled

    session.close()
    assert restore_calls == 1
    assert unregister_calls == 1


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX terminal attributes")
def test_terminal_input_session_close_retries_interrupted_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aflow.terminal_viewport import TerminalInputSession

    class FakeConsole:
        is_terminal = True

    session = TerminalInputSession(FakeConsole(), input_stream=object())
    session._fd = 23
    session._saved_attributes = ["saved"]
    session._started = True
    session._atexit_registered = True
    restore_calls = 0
    unregister_calls = 0

    def tcsetattr(fd: int, action: int, attributes: object) -> None:
        del fd, action, attributes
        nonlocal restore_calls
        restore_calls += 1
        if restore_calls == 1:
            raise KeyboardInterrupt("termios interrupt")

    def unregister(callback: object) -> None:
        del callback
        nonlocal unregister_calls
        unregister_calls += 1

    monkeypatch.setattr("aflow.terminal_viewport.termios.tcsetattr", tcsetattr)
    monkeypatch.setattr("aflow.terminal_viewport.atexit.unregister", unregister)

    with pytest.raises(KeyboardInterrupt, match="termios interrupt"):
        session.close()

    assert restore_calls == 2
    assert unregister_calls == 1
    assert session.is_restored
    assert session._atexit_registered is False
    assert session.is_started is False
    assert session.is_settled

    session.close()
    assert restore_calls == 2
    assert unregister_calls == 1


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX terminal attributes")
def test_terminal_input_session_close_retries_interrupted_callback_unregister(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aflow.terminal_viewport import TerminalInputSession

    class FakeConsole:
        is_terminal = True

    session = TerminalInputSession(FakeConsole(), input_stream=object())
    session._fd = 29
    session._saved_attributes = ["saved"]
    session._started = True
    session._atexit_registered = True
    restore_calls = 0
    unregister_calls = 0

    def tcsetattr(fd: int, action: int, attributes: object) -> None:
        del fd, action, attributes
        nonlocal restore_calls
        restore_calls += 1

    def unregister(callback: object) -> None:
        del callback
        nonlocal unregister_calls
        unregister_calls += 1
        if unregister_calls == 1:
            raise KeyboardInterrupt("unregister interrupt")

    monkeypatch.setattr("aflow.terminal_viewport.termios.tcsetattr", tcsetattr)
    monkeypatch.setattr("aflow.terminal_viewport.atexit.unregister", unregister)

    with pytest.raises(KeyboardInterrupt, match="unregister interrupt"):
        session.close()

    assert restore_calls == 1
    assert unregister_calls == 2
    assert session.is_restored
    assert session._atexit_registered is False
    assert session.is_started is False
    assert session.is_settled

    session.close()
    assert restore_calls == 1
    assert unregister_calls == 2


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX terminal attributes")
def test_banner_renderer_pty_normal_cleanup_restores_rich_screen_cursor_and_termios() -> None:
    from aflow.plan import PlanSnapshot
    from aflow.run_state import ControllerState
    import aflow.status as status_mod
    from aflow.terminal_viewport import TerminalInputSession

    master_fd, slave_fd = pty.openpty()
    inspect_fd = os.dup(slave_fd)
    slave_file = os.fdopen(os.dup(slave_fd), "w", buffering=1)

    class PtyInput:
        def fileno(self) -> int:
            return slave_fd

        def isatty(self) -> bool:
            return True

    class PtySession(TerminalInputSession):
        def __init__(self, console: object, *, wake_event: object) -> None:
            super().__init__(console, input_stream=PtyInput(), wake_event=wake_event)

    before = termios.tcgetattr(inspect_fd)
    console = Console(file=slave_file, force_terminal=True, width=80)
    state = ControllerState(last_snapshot=PlanSnapshot("state", 1, 1, False))
    renderer = status_mod.BannerRenderer(
        config_max_turns=10,
        config_plan_path=Path("/fake/plan.md"),
        console=console,
        refresh_interval_seconds=60.0,
        git_poll_interval_seconds=9999.0,
    )
    renderer._build = lambda _state, git_summary=None: Text("normal snapshot")  # type: ignore[method-assign]
    try:
        with patch.object(status_mod, "TerminalInputSession", PtySession):
            renderer.start(state)
            renderer.stop(state)
        slave_file.flush()
        output = _drain_pty(master_fd)
        after = termios.tcgetattr(inspect_fd)
    finally:
        renderer.stop(state)
        slave_file.close()
        os.close(inspect_fd)
        os.close(master_fd)
        os.close(slave_fd)

    assert after == before
    assert b"\x1b[?1049h" in output
    assert b"\x1b[?1049l" in output
    assert b"\x1b[?25l" in output
    assert b"\x1b[?25h" in output
    assert output.index(b"\x1b[?1049h") < output.index(b"\x1b[?1049l")
    assert output.index(b"\x1b[?25l") < output.index(b"\x1b[?25h")


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX terminal attributes")
def test_banner_renderer_pty_atexit_cleanup_restores_rich_screen_cursor_and_termios() -> None:
    master_fd, slave_fd = pty.openpty()
    inspect_fd = os.dup(slave_fd)
    before = termios.tcgetattr(inspect_fd)
    script = textwrap.dedent(
        """
        import sys
        import time
        from pathlib import Path

        from rich.console import Console
        from rich.text import Text

        from aflow.plan import PlanSnapshot
        from aflow.run_state import ControllerState
        from aflow.status import BannerRenderer

        console = Console(file=sys.stderr, force_terminal=True, width=80)
        renderer = BannerRenderer(
            config_max_turns=10,
            config_plan_path=Path("/fake/plan.md"),
            console=console,
            refresh_interval_seconds=60.0,
            git_poll_interval_seconds=9999.0,
        )
        renderer._build = lambda _state, git_summary=None: Text("atexit snapshot")
        renderer.start(ControllerState(last_snapshot=PlanSnapshot("state", 1, 1, False)))
        time.sleep(0.05)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)
    try:
        assert process.wait(timeout=5.0) == 0
        output = _drain_pty(master_fd)
        after = termios.tcgetattr(inspect_fd)
    finally:
        os.close(inspect_fd)
        os.close(master_fd)

    assert after == before
    assert b"\x1b[?1049h" in output
    assert b"\x1b[?1049l" in output
    assert b"\x1b[?25l" in output
    assert b"\x1b[?25h" in output
    assert output.index(b"\x1b[?1049h") < output.index(b"\x1b[?1049l")
    assert output.index(b"\x1b[?25l") < output.index(b"\x1b[?25h")
