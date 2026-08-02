"""Pure scrolling state and Rich rendering for the live dashboard.

This module deliberately has no terminal, thread, or workflow concerns.  A
future renderer can feed :class:`ViewportAction` values to
:class:`ViewportModel` and render the model through :class:`ScrollableViewport`.
"""

from __future__ import annotations

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
