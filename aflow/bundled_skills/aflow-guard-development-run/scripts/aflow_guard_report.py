#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=11.0.0"]
# ///
"""Render one validated AFlow guard input as a deterministic A4 PNG bundle."""

from __future__ import annotations

import argparse
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - exercised by the standalone runtime
    Image = ImageDraw = ImageFont = None

try:
    from aflow_guard_report_input import (
        MAX_REPORT_INPUT_BYTES,
        ReportInputError,
        validate_report_input,
    )
except ImportError:  # pragma: no cover - a broken installed bundle
    MAX_REPORT_INPUT_BYTES = 64 * 1024
    ReportInputError = ValueError
    validate_report_input = None


WIDTH = 1_240
HEIGHT = 1_754
MARGIN = 64
CONTENT_WIDTH = WIDTH - (MARGIN * 2)
OUTPUT_PNG = "guard-report.png"
OUTPUT_JSON = "guard-report.json"
SECTION_ORDER = (
    "run_identity",
    "progress_ownership",
    "worker_turn",
    "diagnosis_action",
)
SECTION_RECTS = {
    "run_identity": (MARGIN, 38, CONTENT_WIDTH, 190),
    "progress_ownership": (MARGIN, 248, CONTENT_WIDTH, 244),
    "worker_turn": (MARGIN, 512, CONTENT_WIDTH, 280),
    "diagnosis_action": (MARGIN, 812, CONTENT_WIDTH, 888),
}
INPUT_KEYS = (
    "schema_version",
    "repository",
    "run_id",
    "observed_at",
    "status",
    "activity",
    "ownership",
    "checkpoint_index",
    "checkpoint_name",
    "checkpoint_total",
    "step",
    "finalized_turn_summary",
    "worker_selector",
    "worker_model",
    "worker_effort",
    "diagnosis",
)
DIAGNOSIS_KEYS = (
    "confirmed_facts",
    "likely_cause",
    "supporting_evidence",
    "contradicting_evidence",
    "unknowns",
    "confidence",
    "duplicate_operation_risk",
    "owner_action",
)
FONT_REGULAR_CANDIDATES = (
    SCRIPT_DIR / "assets" / "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/local/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    r"C:\Windows\Fonts\arial.ttf",
)
FONT_BOLD_CANDIDATES = (
    SCRIPT_DIR / "assets" / "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/local/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
)
COLORS = {
    "background": "#F3F6FA",
    "card": "#FFFFFF",
    "border": "#D7E0EA",
    "ink": "#172033",
    "muted": "#5B687A",
    "faint": "#8A96A6",
    "blue": "#1769AA",
    "green": "#177A4B",
    "amber": "#8A6510",
    "red": "#B43C32",
    "blue_tint": "#E8F1FA",
    "green_tint": "#E7F4ED",
    "amber_tint": "#FFF5D9",
    "red_tint": "#FBE9E7",
    "number_tint": "#E9EFF7",
}
_FONT_CACHE: dict[tuple[int, bool], Any] = {}
_MISSING_GLYPH_CACHE: dict[int, tuple[tuple[Any, ...], ...]] = {}


class ReportRenderError(ValueError):
    """The input or destination cannot satisfy the renderer contract."""


def section_rects() -> dict[str, tuple[int, int, int, int]]:
    """Return the fixed section geometry used by the A4 layout."""
    return dict(SECTION_RECTS)


def _font(size: int, *, bold: bool = False) -> Any:
    if ImageFont is None:
        raise ReportRenderError(
            "PNG output requires Pillow; run with `uv run --script <renderer>`"
        )
    cache_key = (size, bold)
    if cache_key in _FONT_CACHE:
        return _FONT_CACHE[cache_key]
    candidates = FONT_BOLD_CANDIDATES if bold else FONT_REGULAR_CANDIDATES
    for candidate in candidates:
        if Path(candidate).is_file():
            try:
                loaded = ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
            _FONT_CACHE[cache_key] = loaded
            return loaded
    loaded = ImageFont.load_default()
    _FONT_CACHE[cache_key] = loaded
    return loaded


def _safe_display_text(value: object, font: Any) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
    safe: list[str] = []
    for char in text:
        if char in {"\x00", "\ufffd"} or not char.isprintable():
            safe.append("?")
            continue
        try:
            if _missing_glyph(font, char):
                raise UnicodeError("font has no glyph")
            font.getlength(char)
        except (UnicodeError, OSError, ValueError):
            safe.append("?")
        else:
            safe.append(char)
    return "".join(safe)


def _missing_glyph(font: Any, value: str) -> bool:
    """Use Pillow's deterministic .notdef mask as the missing-glyph marker."""
    try:
        cache_key = id(font)
        signature = _MISSING_GLYPH_CACHE.get(cache_key)
        if signature is None:
            mask, offset = font.getmask2("\U0010ffff")
            signature = ((mask.size[0], mask.size[1], *offset, bytes(mask)),)
            _MISSING_GLYPH_CACHE[cache_key] = signature
        mask, offset = font.getmask2(value)
        candidate = ((mask.size[0], mask.size[1], *offset, bytes(mask)),)
    except (AttributeError, TypeError, UnicodeError, OSError, ValueError):
        return False
    return candidate == signature


def _text_width(draw: Any, value: str, font: Any) -> float:
    try:
        return float(draw.textlength(value, font=font))
    except (UnicodeError, OSError, ValueError):
        return float(draw.textbbox((0, 0), value, font=font)[2])


def _ellipsize(draw: Any, value: str, font: Any, max_width: int) -> str:
    if _text_width(draw, value, font) <= max_width:
        return value
    return _truncate_with_ellipsis(draw, value, font, max_width)


def _truncate_with_ellipsis(draw: Any, value: str, font: Any, max_width: int) -> str:
    """Return a visibly truncated line even when its current width fits."""
    suffix = "…"
    if _text_width(draw, suffix, font) > max_width:
        suffix = "..."
    prefix = value
    while prefix and _text_width(draw, prefix + suffix, font) > max_width:
        prefix = prefix[:-1]
    return (prefix.rstrip() + suffix) if prefix else suffix


def _break_word(draw: Any, word: str, font: Any, max_width: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for char in word:
        candidate = current + char
        if current and _text_width(draw, candidate, font) > max_width:
            pieces.append(current)
            current = char
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces or [""]


def _wrapped_lines(draw: Any, value: object, font: Any, max_width: int) -> list[str]:
    source = _safe_display_text(value, font)
    if not source:
        return []
    lines: list[str] = []
    current = ""
    for word in source.split(" "):
        if not word:
            continue
        if _text_width(draw, word, font) > max_width:
            if current:
                lines.append(current)
                current = ""
            pieces = _break_word(draw, word, font, max_width)
            lines.extend(pieces[:-1])
            current = pieces[-1]
            continue
        candidate = f"{current} {word}" if current else word
        if current and _text_width(draw, candidate, font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _fit_lines(
    draw: Any,
    value: object,
    font: Any,
    max_width: int,
    max_lines: int,
) -> list[str]:
    if max_lines < 1:
        return []
    lines = _wrapped_lines(draw, value, font, max_width)
    if len(lines) <= max_lines:
        return lines
    fitted = lines[:max_lines]
    fitted[-1] = _truncate_with_ellipsis(draw, fitted[-1], font, max_width)
    return fitted


def _line_height(draw: Any, font: Any, minimum: int = 20) -> int:
    try:
        bottom = draw.textbbox((0, 0), "Ag", font=font)[3]
    except (UnicodeError, OSError, ValueError):
        bottom = minimum
    return max(minimum, int(bottom) + 5)


def _display_value(value: object, *, missing: str = "Unavailable") -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return missing
    return str(value)


def _status_color(data: Mapping[str, Any]) -> tuple[str, str]:
    status = str(data.get("status") or "").casefold()
    activity = str(data.get("activity") or "").casefold()
    if any(marker in status for marker in ("failed", "attention", "orphan", "error")):
        return COLORS["red"], COLORS["red_tint"]
    if any(marker in status for marker in ("completed", "success", "ready")):
        return COLORS["green"], COLORS["green_tint"]
    if activity == "active" or status in {"running", "active"}:
        return COLORS["blue"], COLORS["blue_tint"]
    if status in {"unknown", "unavailable", ""} or activity in {"unknown", "unavailable"}:
        return COLORS["amber"], COLORS["amber_tint"]
    return COLORS["blue"], COLORS["blue_tint"]


def _panel(draw: Any, rect: tuple[int, int, int, int]) -> None:
    x, y, width, height = rect
    draw.rounded_rectangle(
        (x, y, x + width, y + height),
        radius=18,
        fill=COLORS["card"],
        outline=COLORS["border"],
        width=2,
    )


def _section_title(draw: Any, rect: tuple[int, int, int, int], title: str) -> int:
    x, y, _, _ = rect
    font = _font(22, bold=True)
    draw.text((x + 24, y + 18), title, font=font, fill=COLORS["ink"])
    return y + 58


def _draw_lines(
    draw: Any,
    value: object,
    x: int,
    y: int,
    width: int,
    *,
    font: Any,
    color: str = COLORS["ink"],
    max_lines: int = 2,
    leading: int | None = None,
) -> int:
    leading = leading or _line_height(draw, font)
    for line in _fit_lines(draw, value, font, width, max_lines):
        draw.text((x, y), line, font=font, fill=color)
        y += leading
    return y


def _draw_label(draw: Any, value: str, x: int, y: int) -> None:
    draw.text((x, y), value, font=_font(12, bold=True), fill=COLORS["faint"])


def _draw_field(
    draw: Any,
    label: str,
    value: object,
    x: int,
    y: int,
    width: int,
    *,
    max_lines: int = 2,
    font: Any | None = None,
) -> int:
    _draw_label(draw, label, x, y)
    body_font = font or _font(17)
    return _draw_lines(
        draw,
        _display_value(value),
        x,
        y + 23,
        width,
        font=body_font,
        max_lines=max_lines,
        leading=_line_height(draw, body_font, 21),
    )


def _draw_header(draw: Any, data: Mapping[str, Any]) -> None:
    rect = SECTION_RECTS["run_identity"]
    _panel(draw, rect)
    x, y, width, _ = rect
    accent, accent_tint = _status_color(data)
    draw.rectangle((x, y, x + width, y + 8), fill=accent)
    title_font = _font(30, bold=True)
    draw.text((x + 24, y + 24), "AFlow Guard Report", font=title_font, fill=COLORS["ink"])

    badge_font = _font(15, bold=True)
    status = _safe_display_text(_display_value(data.get("status"), missing="UNKNOWN"), badge_font)
    activity = _safe_display_text(_display_value(data.get("activity"), missing="UNKNOWN"), badge_font)
    badge = _ellipsize(draw, f"{status.upper()}  ·  {activity.upper()}", badge_font, 380)
    badge_width = int(_text_width(draw, badge, badge_font)) + 30
    draw.rounded_rectangle(
        (x + 24, y + 84, x + 24 + badge_width, y + 123),
        radius=19,
        fill=accent,
    )
    draw.text((x + 39, y + 94), badge, font=badge_font, fill="#FFFFFF")

    meta_x = x + 24 + badge_width + 28
    observed_width = 286
    observed_x = x + width - 24 - observed_width
    meta_width = observed_x - meta_x - 24
    _draw_field(draw, "RUN ID", data.get("run_id"), meta_x, y + 78, meta_width, max_lines=1)
    _draw_field(
        draw,
        "OBSERVED AT",
        data.get("observed_at"),
        observed_x,
        y + 78,
        observed_width,
        max_lines=1,
    )
    draw.line((x + 24, y + 140, x + width - 24, y + 140), fill=COLORS["border"], width=1)
    _draw_field(
        draw,
        "REPOSITORY",
        data.get("repository"),
        x + 24,
        y + 151,
        width - 48,
        max_lines=1,
        font=_font(15),
    )


def _draw_progress(draw: Any, data: Mapping[str, Any]) -> None:
    rect = SECTION_RECTS["progress_ownership"]
    _panel(draw, rect)
    x, y, width, height = rect
    content_y = _section_title(draw, rect, "Progress and Ownership")
    column_width = (width - 72) // 2
    left_x = x + 24
    right_x = left_x + column_width + 24
    draw.line((x + width // 2, y + 60, x + width // 2, y + height - 24), fill=COLORS["border"], width=1)

    index = data.get("checkpoint_index")
    total = data.get("checkpoint_total")
    if index is not None and total is not None:
        checkpoint = f"{index} / {total}"
    elif index is not None:
        checkpoint = f"{index} / ?"
    else:
        checkpoint = "Unavailable"
    _draw_field(draw, "CHECKPOINT", checkpoint, left_x, content_y, column_width, max_lines=1)
    _draw_field(
        draw,
        "CHECKPOINT NAME",
        data.get("checkpoint_name"),
        left_x,
        content_y + 58,
        column_width,
        max_lines=2,
    )
    _draw_field(draw, "STEP", data.get("step"), left_x, content_y + 138, column_width, max_lines=1)

    _draw_field(draw, "OWNERSHIP", data.get("ownership"), right_x, content_y, column_width, max_lines=1)
    _draw_field(draw, "ACTIVITY", data.get("activity"), right_x, content_y + 66, column_width, max_lines=1)
    _draw_field(
        draw,
        "REPORT CONTRACT",
        f"Schema v{data.get('schema_version', '?')} · read-only observation",
        right_x,
        content_y + 124,
        column_width,
        max_lines=2,
    )


def _draw_worker(draw: Any, data: Mapping[str, Any]) -> None:
    rect = SECTION_RECTS["worker_turn"]
    _panel(draw, rect)
    x, y, width, _ = rect
    content_y = _section_title(draw, rect, "Worker and Finalized Turn Evidence")
    summary_font = _font(18)
    summary = data.get("finalized_turn_summary")
    _draw_lines(
        draw,
        _display_value(summary),
        x + 24,
        content_y,
        width - 48,
        font=summary_font,
        color=COLORS["ink"] if summary else COLORS["muted"],
        max_lines=3,
        leading=24,
    )
    divider_y = y + 145
    draw.line((x + 24, divider_y, x + width - 24, divider_y), fill=COLORS["border"], width=1)
    column_width = (width - 96) // 3
    for index, (label, key) in enumerate(
        (("SELECTOR", "worker_selector"), ("MODEL", "worker_model"), ("EFFORT", "worker_effort"))
    ):
        _draw_field(
            draw,
            label,
            data.get(key),
            x + 24 + index * (column_width + 24),
            divider_y + 22,
            column_width,
            max_lines=1,
            font=_font(16),
        )
    _draw_field(
        draw,
        "EVIDENCE BOUNDARY",
        "Only the latest finalized turn is displayed; an active starting turn is not final evidence.",
        x + 24,
        y + 220,
        width - 48,
        max_lines=1,
        font=_font(14),
    )


def _draw_list(
    draw: Any,
    title: str,
    values: object,
    x: int,
    y: int,
    width: int,
    *,
    max_items: int = 4,
) -> int:
    item_font = _font(14)
    _draw_label(draw, title, x, y)
    y += 25
    items = values if isinstance(values, list) else []
    if not items:
        draw.text((x, y), "None recorded", font=item_font, fill=COLORS["muted"])
        return y + 27
    for index, item in enumerate(items[:max_items], 1):
        draw.ellipse((x, y + 3, x + 20, y + 23), fill=COLORS["number_tint"])
        number_font = _font(11, bold=True)
        number = str(index)
        number_width = _text_width(draw, number, number_font)
        draw.text((x + 10 - number_width / 2, y + 5), number, font=number_font, fill=COLORS["blue"])
        end_y = _draw_lines(
            draw,
            item,
            x + 30,
            y,
            width - 30,
            font=item_font,
            color=COLORS["ink"],
            max_lines=2,
            leading=18,
        )
        y = max(y + 43, end_y + 7)
    if len(items) > max_items:
        draw.text(
            (x + 30, y),
            f"+{len(items) - max_items} more in JSON",
            font=_font(12),
            fill=COLORS["muted"],
        )
        y += 22
    return y + 8


def _draw_diagnosis(draw: Any, data: Mapping[str, Any]) -> None:
    rect = SECTION_RECTS["diagnosis_action"]
    _panel(draw, rect)
    x, y, width, height = rect
    diagnosis = data["diagnosis"]
    content_y = _section_title(draw, rect, "Diagnosis and Owner Action")
    accent, accent_tint = _status_color(data)
    action_rect = (x + 24, content_y - 4, width - 48, 130)
    ax, ay, aw, ah = action_rect
    draw.rounded_rectangle(
        (ax, ay, ax + aw, ay + ah),
        radius=12,
        fill=accent_tint,
        outline=accent,
        width=2,
    )
    _draw_label(draw, "OWNER ACTION", ax + 18, ay + 16)
    action_font = _font(18, bold=True)
    _draw_lines(
        draw,
        _display_value(diagnosis.get("owner_action"), missing="No owner action recorded"),
        ax + 18,
        ay + 43,
        aw - 36,
        font=action_font,
        color=COLORS["ink"],
        max_lines=3,
        leading=25,
    )

    metric_y = ay + ah + 22
    metric_width = (width - 96) // 3
    metrics = (
        ("LIKELY CAUSE", diagnosis.get("likely_cause")),
        ("CONFIDENCE", diagnosis.get("confidence")),
        (
            "DUPLICATE OPERATION RISK",
            "Yes"
            if diagnosis.get("duplicate_operation_risk") is True
            else "No"
            if diagnosis.get("duplicate_operation_risk") is False
            else "Unknown",
        ),
    )
    for index, (label, value) in enumerate(metrics):
        _draw_field(
            draw,
            label,
            value,
            x + 24 + index * (metric_width + 24),
            metric_y,
            metric_width,
            max_lines=2,
            font=_font(15, bold=index == 1),
        )

    list_y = metric_y + 92
    list_width = (width - 72) // 2
    left_x = x + 24
    right_x = left_x + list_width + 24
    draw.line((x + width // 2, list_y, x + width // 2, y + height - 22), fill=COLORS["border"], width=1)
    _draw_list(draw, "CONFIRMED FACTS", diagnosis.get("confirmed_facts"), left_x, list_y, list_width)
    _draw_list(draw, "SUPPORTING EVIDENCE", diagnosis.get("supporting_evidence"), left_x, list_y + 220, list_width)
    _draw_list(draw, "CONTRADICTING EVIDENCE", diagnosis.get("contradicting_evidence"), right_x, list_y, list_width)
    _draw_list(draw, "UNKNOWNS / EVIDENCE GAPS", diagnosis.get("unknowns"), right_x, list_y + 220, list_width)


def _render_png(data: Mapping[str, Any]) -> bytes:
    if Image is None or ImageDraw is None:
        raise ReportRenderError(
            "PNG output requires Pillow; run with `uv run --script <renderer>`"
        )
    image = Image.new("RGB", (WIDTH, HEIGHT), COLORS["background"])
    draw = ImageDraw.Draw(image)
    _draw_header(draw, data)
    _draw_progress(draw, data)
    _draw_worker(draw, data)
    _draw_diagnosis(draw, data)
    footer_y = HEIGHT - 44
    draw.line((MARGIN, footer_y - 10, WIDTH - MARGIN, footer_y - 10), fill=COLORS["border"], width=1)
    draw.text(
        (MARGIN, footer_y),
        "AFlow guard · deterministic A4 raster · read-only evidence",
        font=_font(12),
        fill=COLORS["faint"],
    )
    buffer = BytesIO()
    try:
        image.save(buffer, format="PNG", optimize=False, compress_level=9)
    finally:
        image.close()
    return buffer.getvalue()


def _canonicalize(data: Mapping[str, Any]) -> dict[str, Any]:
    if validate_report_input is None:
        raise ReportRenderError("the bundled report-input validator is unavailable")
    try:
        validated = validate_report_input(data)
    except (ReportInputError, TypeError, ValueError) as exc:
        raise ReportRenderError(f"validated report input rejected: {exc}") from exc
    diagnosis = validated.get("diagnosis")
    if not isinstance(diagnosis, Mapping):
        raise ReportRenderError("validated report input diagnosis is not an object")
    normalized = {key: validated[key] for key in INPUT_KEYS}
    normalized["diagnosis"] = {key: diagnosis[key] for key in DIAGNOSIS_KEYS}
    return normalized


def _read_input(path: Path) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReportRenderError(f"input is not a regular file: {path}")
    try:
        if path.stat().st_size > MAX_REPORT_INPUT_BYTES:
            raise ReportRenderError("input exceeds the 64 KiB report contract")
        value = json.loads(path.read_text(encoding="utf-8"))
    except ReportRenderError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReportRenderError(f"input JSON is unreadable: {path.name}") from exc
    if not isinstance(value, Mapping):
        raise ReportRenderError("input JSON must be an object")
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _has_symlink_component(path: Path) -> bool:
    absolute = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    for part in absolute.parts:
        if part == absolute.anchor:
            continue
        current /= part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _output_directory(repository: Path, requested: Path) -> Path:
    candidate = requested.expanduser()
    if _has_symlink_component(candidate):
        raise ReportRenderError("output directory may not contain symlink components")
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ReportRenderError("output directory cannot be resolved") from exc
    if _inside(resolved, repository):
        raise ReportRenderError("output directory must be outside the guarded repository")
    if candidate.exists() and not candidate.is_dir():
        raise ReportRenderError("output path is not a directory")
    return resolved


def _json_bytes(data: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _atomic_write(destination: Path, payload: bytes) -> None:
    temporary_name: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary_name = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            temporary_name.unlink(missing_ok=True)


def render_report(input_path: Path, output_dir: Path) -> tuple[Path, Path]:
    """Render validated input and return the two external artifact paths."""
    normalized = _canonicalize(_read_input(input_path))
    repository = Path(str(normalized["repository"])).resolve(strict=False)
    destination = _output_directory(repository, output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    json_payload = _json_bytes(normalized)
    png_payload = _render_png(normalized)
    png_path = destination / OUTPUT_PNG
    json_path = destination / OUTPUT_JSON
    _atomic_write(png_path, png_payload)
    _atomic_write(json_path, json_payload)
    return png_path, json_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="validated report-input JSON")
    parser.add_argument("--output-dir", type=Path, required=True, help="external artifact directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        render_report(args.input, args.output_dir)
    except (OSError, ReportRenderError) as exc:
        print(f"aflow guard report: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
