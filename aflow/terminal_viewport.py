"""Viewport models/rendering plus the dashboard's POSIX input session.

The viewport and decoder types are terminal-independent and contain no
workflow concerns.  :class:`TerminalInputSession` is the deliberate exception:
it owns cbreak terminal attributes and a bounded input-reader thread so the
renderer can receive typed viewport work without rendering from the reader.
"""

from __future__ import annotations

import atexit
import os
import select
import sys
import threading
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from rich.console import Console, ConsoleOptions, RenderableType
    from rich.segment import Segment

try:
    from rich.console import Console, ConsoleOptions, RenderableType
    from rich.segment import Segment
    from rich.text import Text

    _RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by Rich-unavailable imports
    Console = Any  # type: ignore[assignment,misc]
    ConsoleOptions = Any  # type: ignore[assignment,misc]
    RenderableType = Any  # type: ignore[assignment,misc]
    Segment = Any  # type: ignore[assignment,misc]
    Text = Any  # type: ignore[assignment,misc]
    _RICH_AVAILABLE = False

if os.name == "posix":
    import termios
    import tty

    _POSIX_TERMINAL_AVAILABLE = True
else:  # pragma: no cover - exercised only on non-POSIX hosts
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]
    _POSIX_TERMINAL_AVAILABLE = False


class ViewportAction(Enum):
    """A navigation intent understood by the pure viewport model."""

    LINE_UP = "line_up"
    LINE_DOWN = "line_down"
    PAGE_UP = "page_up"
    PAGE_DOWN = "page_down"
    TOP = "top"
    BOTTOM = "bottom"

    # Short aliases keep callers that think in terms of arrow keys readable
    # without adding a second action type.
    UP = "line_up"
    DOWN = "line_down"


NavigationAction = ViewportAction


class ViewportEvent(Enum):
    """Non-navigation work that the input session can request."""

    RESIZE = "resize"


ViewportInput = ViewportAction | ViewportEvent


@dataclass
class ViewportModel:
    """Mutable, terminal-independent viewport state.

    ``viewport_height`` is the number of rows available to source content;
    the renderable reserves a separate row for its footer.  Content and
    viewport dimensions are updated by the renderable before every draw.
    """

    offset: int = 0
    follow_tail: bool = True
    content_height: int = 0
    viewport_height: int = 0

    def __post_init__(self) -> None:
        self.content_height = max(0, self.content_height)
        self.viewport_height = max(0, self.viewport_height)
        self._clamp_offset()

    @property
    def max_offset(self) -> int:
        return max(0, self.content_height - self.viewport_height)

    @property
    def bottom_offset(self) -> int:
        """Return the offset that displays the last source line."""

        return self.max_offset

    def update_dimensions(self, *, content_height: int, viewport_height: int) -> None:
        """Apply current dimensions and preserve the selected position."""

        self.content_height = max(0, content_height)
        self.viewport_height = max(0, viewport_height)
        self._clamp_offset()

    def set_content_height(self, content_height: int) -> None:
        self.content_height = max(0, content_height)
        self._clamp_offset()

    def set_viewport_height(self, viewport_height: int) -> None:
        self.viewport_height = max(0, viewport_height)
        self._clamp_offset()

    def apply(self, action: ViewportAction) -> None:
        """Apply one navigation action, including its follow-tail semantics."""

        action = ViewportAction(action)
        page_distance = max(1, self.viewport_height - 1)

        if action is ViewportAction.LINE_UP:
            self.follow_tail = False
            self.offset -= 1
        elif action is ViewportAction.LINE_DOWN:
            self.follow_tail = False
            self.offset += 1
        elif action is ViewportAction.PAGE_UP:
            self.follow_tail = False
            self.offset -= page_distance
        elif action is ViewportAction.PAGE_DOWN:
            self.follow_tail = False
            self.offset += page_distance
        elif action is ViewportAction.TOP:
            self.follow_tail = False
            self.offset = 0
        elif action is ViewportAction.BOTTOM:
            self.follow_tail = True
            self.offset = self.bottom_offset
        else:  # pragma: no cover - Enum exhaustiveness guard
            raise ValueError(f"Unsupported viewport action: {action!r}")

        self._clamp_offset()

    def _clamp_offset(self) -> None:
        if self.follow_tail:
            self.offset = self.bottom_offset
        else:
            self.offset = min(max(0, self.offset), self.max_offset)


# The explicit model name is useful at call sites that describe the object as
# a state rather than a controller.
ViewportState = ViewportModel


class IncrementalKeyDecoder:
    """Decode dashboard navigation keys from arbitrarily split byte chunks."""

    _MAX_SEQUENCE_BYTES = 64
    _CSI_INTRODUCERS = frozenset((ord("["), ord("O")))

    _SEQUENCES: dict[bytes, ViewportAction] = {
        b"\x1b[A": ViewportAction.LINE_UP,
        b"\x1b[B": ViewportAction.LINE_DOWN,
        b"\x1b[5~": ViewportAction.PAGE_UP,
        b"\x1b[6~": ViewportAction.PAGE_DOWN,
        b"\x1b[H": ViewportAction.TOP,
        b"\x1b[1~": ViewportAction.TOP,
        b"\x1b[F": ViewportAction.BOTTOM,
        b"\x1b[4~": ViewportAction.BOTTOM,
        b"\x1bOA": ViewportAction.LINE_UP,
        b"\x1bOB": ViewportAction.LINE_DOWN,
        b"\x1bOH": ViewportAction.TOP,
        b"\x1bOF": ViewportAction.BOTTOM,
    }
    _SINGLE_BYTES: dict[int, ViewportAction] = {
        ord("k"): ViewportAction.LINE_UP,
        ord("j"): ViewportAction.LINE_DOWN,
        ord("b"): ViewportAction.PAGE_UP,
        ord("f"): ViewportAction.PAGE_DOWN,
        ord(" "): ViewportAction.PAGE_DOWN,
        ord("g"): ViewportAction.TOP,
        ord("G"): ViewportAction.BOTTOM,
    }

    def __init__(self) -> None:
        self._pending = bytearray()

    @property
    def pending(self) -> bytes:
        """Expose buffered bytes for diagnostics without allowing mutation."""

        return bytes(self._pending)

    def feed(self, data: bytes | bytearray | memoryview | str) -> tuple[ViewportAction, ...]:
        """Decode all complete actions in ``data``.

        ASCII navigation keys are recognized directly.  Non-ASCII input and
        malformed escape sequences are discarded, so unrelated UTF-8 cannot
        leak into the dashboard or wedge the decoder.  A possible prefix is
        retained until a later call supplies its remaining bytes.
        """

        if isinstance(data, str):
            data = data.encode("utf-8", errors="ignore")
        actions: list[ViewportAction] = []

        # Process input one byte at a time so an unterminated sequence never
        # makes the retained buffer proportional to the size of one read.
        for byte in bytes(data):
            self._pending.append(byte)
            self._drain_pending(actions)

        return tuple(actions)

    def _drain_pending(self, actions: list[ViewportAction]) -> None:
        while self._pending:
            first = self._pending[0]
            if first != 0x1B:
                del self._pending[0]
                action = self._SINGLE_BYTES.get(first)
                if action is not None:
                    actions.append(action)
                continue

            pending = bytes(self._pending)
            if len(pending) == 1:
                # Escape alone is intentionally not an action.  It may be the
                # first byte of a sequence split across reads.
                break

            if self._pending[1] not in self._CSI_INTRODUCERS:
                # Escape followed by an ordinary byte is not a CSI/SS3
                # sequence.  Discard only Escape so the ordinary byte can be
                # handled normally (including a valid navigation key).
                del self._pending[0]
                continue

            status, boundary = self._scan_escape_sequence(bytes(self._pending))
            if status == "incomplete":
                if len(self._pending) >= self._MAX_SEQUENCE_BYTES:
                    # A syntactically valid but unterminated sequence is
                    # treated as malformed once it reaches the fixed bound.
                    self._pending.clear()
                break

            # Both complete and malformed sequences are discarded through
            # their boundary.  Only an exact supported complete sequence
            # yields an action; no byte in an unsupported sequence is replayed
            # as a standalone key.
            assert boundary is not None
            if status == "complete":
                sequence = bytes(self._pending[: boundary + 1])
                action = self._SEQUENCES.get(sequence)
                if action is not None:
                    actions.append(action)
            if status == "malformed" and self._pending[boundary] == 0x1B:
                # A fresh Escape can start a valid sequence immediately after
                # the malformed prefix, so leave it available for the next
                # scan rather than consuming it as malformed input.
                del self._pending[:boundary]
            else:
                del self._pending[: boundary + 1]

    @staticmethod
    def _scan_escape_sequence(pending: bytes) -> tuple[str, int | None]:
        """Classify a CSI/SS3 prefix and return its first boundary index."""

        saw_intermediate = False
        for index, byte in enumerate(pending[2:], start=2):
            if 0x40 <= byte <= 0x7E:
                return "complete", index
            if 0x30 <= byte <= 0x3F:
                if saw_intermediate:
                    return "malformed", index
                continue
            if 0x20 <= byte <= 0x2F:
                saw_intermediate = True
                continue
            return "malformed", index
        return "incomplete", None

    def decode(self, data: bytes | bytearray | memoryview | str) -> ViewportAction | None:
        """Return the one decoded action, or ``None`` for no/split input."""

        actions = self.feed(data)
        return actions[0] if len(actions) == 1 else None


KeyDecoder = IncrementalKeyDecoder


class TerminalInputSession:
    """Own a supported POSIX terminal's input mode and input reader.

    The reader deliberately has no rendering or workflow callback.  It only
    decodes navigation bytes, observes terminal-size changes during bounded
    ``select`` waits, and places typed work items on a queue for the renderer.
    """

    _SELECT_TIMEOUT_SECONDS = 0.1

    def __init__(
        self,
        console: Console,
        *,
        input_stream: Any | None = None,
        wake_event: threading.Event | None = None,
    ) -> None:
        self.console = console
        self.input_stream = input_stream if input_stream is not None else sys.stdin
        self.wake_event = wake_event or threading.Event()
        self._events: deque[ViewportInput] = deque()
        self._events_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._decoder = IncrementalKeyDecoder()
        self._thread: threading.Thread | None = None
        self._fd: int | None = None
        self._saved_attributes: list[Any] | None = None
        self._restored = False
        self._started = False
        self._atexit_registered = False
        self._last_size: tuple[int, int] | None = None

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    @property
    def fd(self) -> int | None:
        return self._fd

    @property
    def saved_attributes(self) -> list[Any] | None:
        return self._saved_attributes

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def is_restored(self) -> bool:
        return self._restored

    def _validate_capabilities(self) -> tuple[int, list[Any]]:
        if not _POSIX_TERMINAL_AVAILABLE or not _RICH_AVAILABLE:
            raise RuntimeError("interactive viewport requires POSIX and Rich")
        if not self.console.is_terminal:
            raise RuntimeError("dashboard console is not a terminal")
        if not self.input_stream.isatty():
            raise RuntimeError("dashboard input is not a TTY")
        fd = self.input_stream.fileno()
        if not isinstance(fd, int) or fd < 0:
            raise RuntimeError("dashboard input has no valid file descriptor")
        attributes = termios.tcgetattr(fd)
        return fd, attributes

    def start(self) -> None:
        """Enter cbreak mode and start the daemon input reader."""

        if self._started:
            return
        fd, attributes = self._validate_capabilities()
        self._fd = fd
        self._saved_attributes = attributes
        self._last_size = self._console_size()
        try:
            # tty.setcbreak keeps signal processing enabled on supported
            # Python versions.  Re-assert ISIG after it so the contract stays
            # explicit even on older implementations.
            tty.setcbreak(fd)
            cbreak_attributes = termios.tcgetattr(fd)
            cbreak_attributes[3] |= termios.ISIG
            termios.tcsetattr(fd, termios.TCSANOW, cbreak_attributes)
            self._started = True
            atexit.register(self.close)
            self._atexit_registered = True
            thread = threading.Thread(
                target=self._run,
                daemon=True,
                name="aflow-dashboard-input",
            )
            self._thread = thread
            thread.start()
        except Exception:
            self.close()
            raise

    def _console_size(self) -> tuple[int, int] | None:
        try:
            size = self.console.size
            return (int(size.width), int(size.height))
        except Exception:
            return None

    def _enqueue(self, event: ViewportInput) -> None:
        with self._events_lock:
            self._events.append(event)
        self.wake_event.set()

    def drain_events(self) -> tuple[ViewportInput, ...]:
        """Return all queued work and clear the wake when the queue is empty."""

        with self._events_lock:
            events = tuple(self._events)
            self._events.clear()
            if not self._events:
                self.wake_event.clear()
        return events

    def stop_reader(self) -> None:
        """Stop and join only the reader; leave terminal restoration separate."""

        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self._thread = None

    def restore(self) -> None:
        """Restore saved terminal attributes exactly once."""

        if self._restored:
            return
        self._restored = True
        if self._fd is not None and self._saved_attributes is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_attributes)
            except Exception:
                # A detached terminal can disappear during interpreter cleanup.
                # There is no useful recovery in that case, but cleanup stays
                # idempotent and never masks the workflow's original outcome.
                pass
        if self._atexit_registered:
            atexit.unregister(self.close)
            self._atexit_registered = False

    def close(self) -> None:
        """Stop the reader and restore terminal attributes idempotently."""

        self.stop_reader()
        self.restore()
        self._started = False

    def _run(self) -> None:
        fd = self._fd
        if fd is None:
            return
        while not self._stop_event.is_set():
            try:
                readable, _, _ = select.select(
                    [fd],
                    [],
                    [],
                    self._SELECT_TIMEOUT_SECONDS,
                )
            except (OSError, ValueError):
                return
            if self._stop_event.is_set():
                return
            if readable:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    return
                if not data:
                    return
                for action in self._decoder.feed(data):
                    self._enqueue(action)
                continue

            current_size = self._console_size()
            if current_size is not None and current_size != self._last_size:
                self._last_size = current_size
                self._enqueue(ViewportEvent.RESIZE)


class ScrollableViewport:
    """Render a Rich document through a segment-preserving viewport."""

    def __init__(
        self,
        renderable: RenderableType,
        *,
        model: ViewportModel | None = None,
    ) -> None:
        self.renderable = renderable
        self.model = model or ViewportModel()

    def apply(self, action: ViewportAction) -> None:
        self.model.apply(action)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> Iterable[Segment]:
        if not _RICH_AVAILABLE:  # pragma: no cover - defensive optional-dependency path
            return

        width = max(0, options.max_width)
        if width < 1:
            return

        source_lines = self._render_source_lines(console, options, width)
        terminal_height = max(0, options.max_height)
        body_height = max(0, terminal_height - 1)
        self.model.update_dimensions(
            content_height=len(source_lines),
            viewport_height=body_height,
        )

        selected_lines = source_lines[
            self.model.offset : self.model.offset + body_height
        ]
        rendered_lines: list[list[Segment]] = [
            Segment.adjust_line_length(line, width)
            for line in selected_lines
        ]
        rendered_lines.extend(
            [Segment.adjust_line_length([], width) for _ in range(body_height - len(rendered_lines))]
        )
        rendered_lines.append(self._render_footer(console, options, width))

        for index, line in enumerate(rendered_lines):
            yield from line
            if index < len(rendered_lines) - 1:
                yield Segment.line()

    def _render_source_lines(
        self,
        console: Console,
        options: ConsoleOptions,
        width: int,
    ) -> list[list[Segment]]:
        """Render the whole source to complete, width-bounded segment lines."""

        source_options = options.update(
            width=width,
            no_wrap=False,
            overflow="fold",
        )
        segments = console.render(self.renderable, source_options)  # type: ignore[attr-defined]
        return [
            list(line)
            for line in Segment.split_and_crop_lines(
                segments,
                width,
                pad=False,
                include_new_lines=False,
            )
        ]

    def _render_footer(
        self,
        console: Console,
        options: ConsoleOptions,
        width: int,
    ) -> list[Segment]:
        position = self._position_text()
        mode = "[follow]" if self.model.follow_tail else "[manual]"
        footer = Text(
            f"{position} {mode} | k/Up j/Down b/PageUp f/Space/PageDown g/Home G/End",
            style="dim",
        )
        footer_options = options.update(
            width=width,
            no_wrap=True,
            overflow="crop",
        )
        footer_segments = console.render(footer, footer_options)  # type: ignore[attr-defined]
        footer_lines = list(
            Segment.split_and_crop_lines(
                footer_segments,
                width,
                pad=False,
                include_new_lines=False,
            )
        )
        return Segment.adjust_line_length(footer_lines[0] if footer_lines else [], width)

    def _position_text(self) -> str:
        total = self.model.content_height
        if total == 0 or self.model.viewport_height == 0:
            return f"0/{total}"
        first = self.model.offset + 1
        last = min(total, self.model.offset + self.model.viewport_height)
        return f"{first}-{last}/{total}"


ViewportRenderable = ScrollableViewport
