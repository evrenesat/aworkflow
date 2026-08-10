#!/usr/bin/env python3
"""Render bounded AFlow guard evidence as a compact multi-format report bundle."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from io import BytesIO
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - validated when image output is requested
    Image = ImageDraw = ImageFont = None

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
except ImportError:  # pragma: no cover - validated when PDF output is requested
    colors = TA_LEFT = A4 = ParagraphStyle = getSampleStyleSheet = mm = None
    ImageReader = canvas = Paragraph = SimpleDocTemplate = Spacer = None


ALLOWED_STATES = {"in_progress", "completed", "needs_owner_action", "reviewer_turn"}
SENSITIVE_KEYS = re.compile(
    r"(^|_)(api_?key|authorization|cookie|credential|password|private_?key|secret|token)(_|$)",
    re.IGNORECASE,
)
SENSITIVE_VALUES = re.compile(
    r"(?:RUNPOD_API_KEY\s*=|Authorization:\s*Bearer|-----BEGIN [A-Z ]*PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9_]{20,})",
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", required=True, help="Bounded report JSON, or - for stdin"
    )
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        help="Write Markdown, interactive HTML, mobile PNG, PDF, email HTML, and manifest",
    )
    parser.add_argument("--basename", default="aflow-guard-report")
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--html-out", type=Path)
    parser.add_argument("--image-out", type=Path)
    parser.add_argument("--pdf-out", type=Path)
    parser.add_argument("--email-html-out", type=Path)
    parser.add_argument("--manifest-out", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.basename):
        parser.error("--basename must be a safe filename stem")
    if args.bundle_dir:
        stem = args.bundle_dir / args.basename
        args.markdown_out = args.markdown_out or stem.with_suffix(".md")
        args.html_out = args.html_out or stem.with_suffix(".html")
        args.image_out = args.image_out or stem.with_suffix(".png")
        args.pdf_out = args.pdf_out or stem.with_suffix(".pdf")
        args.email_html_out = args.email_html_out or stem.with_name(
            f"{args.basename}-email.html"
        )
        args.manifest_out = args.manifest_out or stem.with_name(
            f"{args.basename}-manifest.json"
        )
    if not any(
        (
            args.markdown_out,
            args.html_out,
            args.image_out,
            args.pdf_out,
            args.email_html_out,
            args.manifest_out,
        )
    ):
        parser.error("provide --bundle-dir or at least one explicit output option")
    return args


def load_input(source: str) -> dict[str, Any]:
    raw = (
        sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    )
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError("report input must be a JSON object")
    return data


def reject_secrets(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if SENSITIVE_KEYS.search(str(key)):
                raise ValueError(f"sensitive field rejected at {path}.{key}")
            reject_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_secrets(child, f"{path}[{index}]")
    elif isinstance(value, str) and SENSITIVE_VALUES.search(value):
        raise ValueError(f"credential-like value rejected at {path}")


def validate(data: dict[str, Any]) -> None:
    if data.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    if data.get("state") not in ALLOWED_STATES:
        raise ValueError(
            "state must be in_progress, completed, needs_owner_action, or reviewer_turn"
        )
    for required in ("title", "generated_at", "run_id", "repository", "summary"):
        if not isinstance(data.get(required), str) or not data[required].strip():
            raise ValueError(f"{required} must be a non-empty string")
    if data["state"] == "needs_owner_action" and not data.get("owner_actions"):
        raise ValueError("needs_owner_action requires at least one owner action")
    if data["state"] == "reviewer_turn":
        for required in ("notification_id", "checkpoint", "verdict"):
            if not isinstance(data.get(required), str) or not data[required].strip():
                raise ValueError(f"reviewer_turn requires non-empty {required}")
    strategy = data.get("worker_strategy")
    if strategy is not None:
        if not isinstance(strategy, dict):
            raise ValueError("worker_strategy must be an object")
        for required in ("entry_team", "entry_worker"):
            if (
                not isinstance(strategy.get(required), str)
                or not strategy[required].strip()
            ):
                raise ValueError(
                    f"worker_strategy.{required} must be a non-empty string"
                )
        chain = strategy.get("configured_chain")
        if not isinstance(chain, list) or not chain:
            raise ValueError(
                "worker_strategy.configured_chain must be a non-empty list"
            )
        for index, member in enumerate(chain):
            if not isinstance(member, dict):
                raise TypeError(
                    f"worker_strategy.configured_chain[{index}] must be an object"
                )
            for required in ("team", "worker"):
                if (
                    not isinstance(member.get(required), str)
                    or not member[required].strip()
                ):
                    raise ValueError(
                        f"worker_strategy.configured_chain[{index}].{required} must be a non-empty string"
                    )
        observed = strategy.get("observed_workers", [])
        if not isinstance(observed, list):
            raise ValueError("worker_strategy.observed_workers must be a list")
        for index, member in enumerate(observed):
            if not isinstance(member, dict) or not isinstance(
                member.get("worker"), str
            ):
                raise TypeError(
                    f"worker_strategy.observed_workers[{index}] must identify a worker"
                )
            attempts = member.get("attempts")
            if (
                isinstance(attempts, bool)
                or not isinstance(attempts, int)
                or attempts < 0
            ):
                raise ValueError(
                    f"worker_strategy.observed_workers[{index}].attempts must be a non-negative integer"
                )
    reject_secrets(data)


def text(value: Any) -> str:
    return str(value).strip()


def model_outcome_sentence(row: dict[str, Any]) -> str:
    summary = f"{text(row.get('model', ''))}: {text(row.get('attempts', ''))} attempts"
    metrics = []
    if text(row.get("approved", "")):
        metrics.append(f"{text(row['approved'])} approved")
    if text(row.get("rejected", "")):
        metrics.append(f"{text(row['rejected'])} rejected")
    if metrics:
        summary += ", " + ", ".join(metrics)
    if text(row.get("median_seconds", "")):
        summary += f"; median {text(row['median_seconds'])} seconds"
    summary += "."
    if text(row.get("note", "")):
        summary += f" {text(row['note'])}"
    return summary


def markdown_list(title: str, values: Any) -> list[str]:
    if not values:
        return []
    return [
        f"## {title}",
        "",
        *[f"{i}. {text(item)}" for i, item in enumerate(values, 1)],
        "",
    ]


def markdown_table(title: str, rows: Any, columns: list[tuple[str, str]]) -> list[str]:
    if not rows:
        return []
    lines = [f"## {title}", "", "| " + " | ".join(label for _, label in columns) + " |"]
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    for row in rows:
        cells = [
            text(row.get(key, "")).replace("|", "\\|").replace("\n", " ")
            for key, _ in columns
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return [*lines, ""]


def markdown_worker_strategy(data: dict[str, Any]) -> list[str]:
    strategy = data.get("worker_strategy")
    if not strategy:
        return []
    chain = " -> ".join(
        f"{text(member['team'])} ({text(member['worker'])})"
        for member in strategy["configured_chain"]
    )
    observed = strategy.get("observed_workers") or []
    observed_text = (
        ", ".join(
            f"{text(member['worker'])}: {member['attempts']} attempt(s)"
            for member in observed
        )
        or "No finalized worker attempts"
    )
    lines = [
        "## Worker strategy",
        "",
        f"**Entry team:** {text(strategy['entry_team'])}  ",
        f"**Starting worker:** {text(strategy['entry_worker'])}  ",
        f"**Configured upgrade chain:** {chain}  ",
        f"**Observed worker sample:** {observed_text}",
        "",
    ]
    if strategy.get("note"):
        lines += [text(strategy["note"]), ""]
    return lines


def render_markdown(data: dict[str, Any]) -> str:
    state_label = state_name(data)
    lines = [
        f"# {text(data['title'])}",
        "",
        f"**State:** {state_label}  ",
        f"**Generated:** {text(data['generated_at'])}  ",
        f"**Run:** `{text(data['run_id'])}`  ",
        f"**Repository:** `{text(data['repository'])}`",
        "",
        text(data["summary"]),
        "",
    ]
    if data["state"] == "reviewer_turn":
        lines += [
            f"**Checkpoint:** {text(data['checkpoint'])}  ",
            f"**Verdict:** {text(data['verdict'])}",
            "",
        ]
    lines += markdown_table(
        "Checkpoints",
        data.get("checkpoints"),
        [("name", "Checkpoint"), ("status", "Status"), ("detail", "Evidence")],
    )
    lines += markdown_table(
        "Work lanes",
        data.get("lanes"),
        [("name", "Lane"), ("status", "Status"), ("detail", "Detail")],
    )
    if data.get("bottleneck"):
        lines += ["## Current bottleneck", "", text(data["bottleneck"]), ""]
    lines += markdown_list("Completed", data.get("completed"))
    lines += markdown_list("Remaining", data.get("remaining"))
    lines += markdown_list("Owner action", data.get("owner_actions"))
    lines += markdown_worker_strategy(data)
    lines += markdown_table(
        "Model outcomes",
        data.get("model_outcomes"),
        [
            ("model", "Model"),
            ("attempts", "Attempts"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("median_seconds", "Median seconds"),
            ("note", "Note"),
        ],
    )
    lines += markdown_list("Evidence", data.get("evidence"))
    return "\n".join(lines).rstrip() + "\n"


def state_name(data: dict[str, Any]) -> str:
    return {
        "in_progress": "In progress",
        "completed": "Completed",
        "needs_owner_action": "Owner action required",
        "reviewer_turn": f"{text(data.get('checkpoint', 'Checkpoint'))} review: {text(data.get('verdict', 'finalized'))}",
    }[data["state"]]


def state_accent(data: dict[str, Any]) -> str:
    return {
        "in_progress": "#1f67b1",
        "completed": "#16845b",
        "needs_owner_action": "#a95312",
        "reviewer_turn": "#a95312",
    }[data["state"]]


def render_html(data: dict[str, Any], markdown: str) -> str:
    state_label = state_name(data)
    accent = state_accent(data)

    def cards(rows: Any) -> str:
        return "".join(
            f"<article><strong>{html.escape(text(row.get('name', '')))}</strong>"
            f"<span>{html.escape(text(row.get('status', '')))}</span>"
            f"<p>{html.escape(text(row.get('detail', '')))}</p></article>"
            for row in (rows or [])
        )

    def items(values: Any) -> str:
        return "".join(f"<li>{html.escape(text(item))}</li>" for item in (values or []))

    model_rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(text(row.get(key, '')))}</td>"
            for key in (
                "model",
                "attempts",
                "approved",
                "rejected",
                "median_seconds",
                "note",
            )
        )
        + "</tr>"
        for row in (data.get("model_outcomes") or [])
    )
    strategy = data.get("worker_strategy") or {}
    configured_chain = strategy.get("configured_chain", [])
    strategy_parts = []
    for index, member in enumerate(configured_chain):
        strategy_parts.append(
            f'<div class="strategy-node"><strong>{html.escape(text(member.get("team", "")))}</strong>'
            f"<span>{html.escape(text(member.get('worker', '')))}</span></div>"
        )
        if index + 1 < len(configured_chain):
            strategy_parts.append('<div class="arrow">&#8594;</div>')
    strategy_nodes = "".join(strategy_parts)
    observed_workers = "".join(
        f"<li>{html.escape(text(member.get('worker', '')))}: "
        f"{html.escape(text(member.get('attempts', 0)))} attempt(s)</li>"
        for member in strategy.get("observed_workers", [])
    )
    strategy_html = ""
    if strategy:
        note_html = (
            f"<p>{html.escape(text(strategy.get('note', '')))}</p>"
            if strategy.get("note")
            else ""
        )
        strategy_html = (
            "<details open><summary>Worker strategy and sampled models</summary>"
            f"<p>Entry team <strong>{html.escape(text(strategy.get('entry_team', '')))}</strong>; "
            f"starting worker <strong>{html.escape(text(strategy.get('entry_worker', '')))}</strong>.</p>"
            f'<div class="strategy">{strategy_nodes}</div>'
            f"<h3>Observed worker sample</h3><ul>{observed_workers or '<li>No finalized worker attempts</li>'}</ul>"
            f"{note_html}</details>"
        )
    bottleneck_html = (
        f"<h2>Current bottleneck</h2><p>{html.escape(text(data['bottleneck']))}</p>"
        if data.get("bottleneck")
        else ""
    )
    completed_html = (
        f"<h2>Completed</h2><ol>{items(data.get('completed'))}</ol>"
        if data.get("completed")
        else ""
    )
    remaining_html = (
        f"<h2>Remaining</h2><ol>{items(data.get('remaining'))}</ol>"
        if data.get("remaining")
        else ""
    )
    owner_html = (
        f"<h2>Owner action</h2><ol>{items(data.get('owner_actions'))}</ol>"
        if data.get("owner_actions")
        else ""
    )
    models_html = (
        "<h2>Model outcomes</h2><table><thead><tr><th>Model</th><th>Attempts</th>"
        "<th>Approved</th><th>Rejected</th><th>Median seconds</th><th>Note</th>"
        f"</tr></thead><tbody>{model_rows}</tbody></table>"
        if model_rows
        else ""
    )
    evidence_html = (
        f"<h2>Evidence</h2><ol>{items(data.get('evidence'))}</ol>"
        if data.get("evidence")
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(text(data["title"]))}</title><style>
:root {{ color-scheme: light dark; --accent:{accent}; --panel:color-mix(in srgb, Canvas 94%, var(--accent)); }}
* {{ box-sizing:border-box }} body {{ margin:0; font:15px/1.5 system-ui,sans-serif; background:Canvas; color:CanvasText }}
main {{ max-width:1050px; margin:auto; padding:28px }} header {{ border-left:7px solid var(--accent); padding:4px 18px }}
h1 {{ margin:0 0 8px; font-size:clamp(24px,4vw,40px) }} h2 {{ margin:30px 0 12px }} .meta {{ color:GrayText }}
.badge {{ display:inline-block; padding:5px 10px; border-radius:999px; background:var(--accent); color:white; font-weight:700 }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:12px }}
article {{ padding:14px; border:1px solid color-mix(in srgb, CanvasText 18%, Canvas); border-radius:12px; background:var(--panel) }}
article strong, article span {{ display:block }} article span {{ color:var(--accent); font-weight:700 }} article p {{ margin:8px 0 0 }}
details {{ margin-top:26px; padding:14px; border:1px solid color-mix(in srgb, CanvasText 18%, Canvas); border-radius:12px }}
summary {{ cursor:pointer; font-size:18px; font-weight:750 }}
.strategy {{ display:flex; align-items:stretch; gap:10px; overflow-x:auto; padding:10px 0 }}
.strategy-node {{ min-width:190px; padding:12px; border-radius:10px; background:var(--panel) }}
.strategy-node span {{ display:block; color:GrayText; overflow-wrap:anywhere }} .arrow {{ align-self:center; font-size:24px }}
table {{ width:100%; border-collapse:collapse; overflow:auto; display:block }} th,td {{ padding:8px; border-bottom:1px solid color-mix(in srgb, CanvasText 18%, Canvas); text-align:left; white-space:nowrap }}
pre {{ white-space:pre-wrap; word-break:break-word; background:var(--panel); padding:16px; border-radius:12px }}
@media(max-width:600px) {{ main {{ padding:18px }} }}
</style></head><body><main><header><span class="badge">{state_label}</span>
<h1>{html.escape(text(data["title"]))}</h1><p>{html.escape(text(data["summary"]))}</p>
<p class="meta">{html.escape(text(data["generated_at"]))} · Run {html.escape(text(data["run_id"]))}</p></header>
<h2>Progress</h2><div class="grid">{cards(data.get("checkpoints"))}{cards(data.get("lanes"))}</div>
{bottleneck_html}{completed_html}{remaining_html}{owner_html}{strategy_html}
{models_html}{evidence_html}
<details><summary>Markdown source</summary><pre>{html.escape(markdown)}</pre></details>
</main></body></html>"""


def render_email_html(data: dict[str, Any]) -> str:
    """Render conservative one-column HTML for narrow Android mail clients."""
    esc = lambda value: html.escape(text(value))
    state_label = esc(state_name(data))

    def section(title: str, values: Any) -> str:
        if not values:
            return ""
        rows = "".join(
            '<li style="margin:0 0 10px 0;overflow-wrap:anywhere;word-break:break-word">'
            f"{esc(value)}</li>"
            for value in values
        )
        return (
            f'<h2 style="font-size:18px;line-height:1.3;margin:24px 0 10px">{html.escape(title)}</h2>'
            f'<ol style="padding-left:22px;margin:0">{rows}</ol>'
        )

    def stacked(title: str, rows: Any) -> str:
        if not rows:
            return ""
        blocks = "".join(
            '<div style="border-top:1px solid #d8d8d8;padding:12px 0">'
            f'<div style="font-weight:700;overflow-wrap:anywhere">{esc(row.get("name", ""))}</div>'
            f'<div style="color:#9a4c0d;font-weight:700;overflow-wrap:anywhere">{esc(row.get("status", ""))}</div>'
            f'<div style="margin-top:4px;overflow-wrap:anywhere;word-break:break-word">{esc(row.get("detail", ""))}</div>'
            "</div>"
            for row in rows
        )
        return f'<h2 style="font-size:18px;line-height:1.3;margin:24px 0 4px">{html.escape(title)}</h2>{blocks}'

    models = []
    for row in data.get("model_outcomes") or []:
        models.append(model_outcome_sentence(row))

    reviewer = ""
    if data["state"] == "reviewer_turn":
        reviewer = (
            '<div style="margin:16px 0;padding:12px;border-left:5px solid #b55b13;background:#fff5e9">'
            f"<div><strong>Checkpoint:</strong> {esc(data['checkpoint'])}</div>"
            f"<div><strong>Verdict:</strong> {esc(data['verdict'])}</div></div>"
        )

    bottleneck = ""
    if data.get("bottleneck"):
        bottleneck = (
            '<h2 style="font-size:18px;line-height:1.3;margin:24px 0 8px">Current bottleneck</h2>'
            f'<p style="margin:0;overflow-wrap:anywhere;word-break:break-word">{esc(data["bottleneck"])}</p>'
        )

    strategy_items = []
    strategy = data.get("worker_strategy") or {}
    if strategy:
        chain = " -> ".join(
            f"{text(member.get('team', ''))} ({text(member.get('worker', ''))})"
            for member in strategy.get("configured_chain", [])
        )
        observed = (
            ", ".join(
                f"{text(member.get('worker', ''))}: {text(member.get('attempts', 0))} attempt(s)"
                for member in strategy.get("observed_workers", [])
            )
            or "No finalized worker attempts"
        )
        strategy_items = [
            f"Entry: {text(strategy.get('entry_team', ''))} / {text(strategy.get('entry_worker', ''))}",
            f"Configured chain: {chain}",
            f"Observed sample: {observed}",
        ]
        if strategy.get("note"):
            strategy_items.append(text(strategy["note"]))

    return (
        '<div style="margin:0 auto;max-width:640px;padding:16px;color:#202124;'
        'background:#ffffff;font-family:Arial,sans-serif;font-size:16px;line-height:1.5">'
        f'<div style="display:inline-block;background:#9a4c0d;color:#ffffff;padding:5px 9px;font-weight:700">{state_label}</div>'
        f'<h1 style="font-size:24px;line-height:1.2;margin:14px 0 8px;overflow-wrap:anywhere">{esc(data["title"])}</h1>'
        f'<p style="margin:0 0 10px;overflow-wrap:anywhere;word-break:break-word">{esc(data["summary"])}</p>'
        f'<p style="margin:0;color:#5f6368;font-size:14px;overflow-wrap:anywhere;word-break:break-word">'
        f"{esc(data['generated_at'])}<br>Run {esc(data['run_id'])}</p>"
        f"{reviewer}{stacked('Checkpoints', data.get('checkpoints'))}{stacked('Work lanes', data.get('lanes'))}"
        f"{bottleneck}{section('Completed', data.get('completed'))}{section('Remaining', data.get('remaining'))}"
        f"{section('Owner action', data.get('owner_actions'))}{section('Worker strategy', strategy_items)}"
        f"{section('Model outcomes', models)}"
        f"{section('Evidence', data.get('evidence'))}</div>"
    )


FONT_REGULAR_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
FONT_BOLD_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def _image_font(size: int, *, bold: bool = False) -> Any:
    if ImageFont is None:
        raise ValueError("PNG output requires Pillow")
    candidates = FONT_BOLD_CANDIDATES if bold else FONT_REGULAR_CANDIDATES
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def _wrapped_lines(draw: Any, value: Any, font: Any, max_width: int) -> list[str]:
    source = text(value)
    if not source:
        return []
    lines: list[str] = []
    for paragraph in source.splitlines() or [source]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _render_image_legacy(data: dict[str, Any], destination: Path) -> None:
    """Render an Android-safe, single-column PNG summary."""
    if Image is None or ImageDraw is None:
        raise ValueError("PNG output requires Pillow")
    width, working_height = 1080, 16000
    image = Image.new("RGB", (width, working_height), "#f4f6fa")
    draw = ImageDraw.Draw(image)
    margin, content_width = 64, width - 128
    y = 58
    title_font = _image_font(46, bold=True)
    heading_font = _image_font(29, bold=True)
    body_font = _image_font(24)
    body_bold = _image_font(24, bold=True)
    small_font = _image_font(19)
    accent = state_accent(data)
    dark, muted, card = "#172033", "#586174", "#ffffff"

    def paragraph(
        value: Any, *, font: Any = body_font, color: str = dark, gap: int = 12
    ) -> None:
        nonlocal y
        lines = _wrapped_lines(draw, value, font, content_width)
        line_height = max(30, draw.textbbox((0, 0), "Ag", font=font)[3] + 8)
        for line in lines:
            draw.text((margin, y), line, font=font, fill=color)
            y += line_height
        y += gap

    def heading(label: str) -> None:
        nonlocal y
        y += 20
        draw.text((margin, y), label, font=heading_font, fill=dark)
        y += 48

    def card_block(name: Any, status: Any, detail: Any) -> None:
        nonlocal y
        name_lines = _wrapped_lines(draw, name, body_bold, content_width - 48)
        detail_lines = _wrapped_lines(draw, detail, small_font, content_width - 48)
        height = 30 + len(name_lines) * 34 + 34 + len(detail_lines) * 27 + 28
        draw.rounded_rectangle(
            (margin, y, width - margin, y + height),
            radius=18,
            fill=card,
            outline="#dce1ea",
        )
        cy = y + 22
        for line in name_lines:
            draw.text((margin + 24, cy), line, font=body_bold, fill=dark)
            cy += 34
        draw.text((margin + 24, cy), text(status), font=body_bold, fill=accent)
        cy += 38
        for line in detail_lines:
            draw.text((margin + 24, cy), line, font=small_font, fill=muted)
            cy += 27
        y += height + 14

    badge = state_name(data)
    badge_box = draw.textbbox((0, 0), badge, font=body_bold)
    badge_width = badge_box[2] + 36
    draw.rounded_rectangle(
        (margin, y, margin + badge_width, y + 45), radius=20, fill=accent
    )
    draw.text((margin + 18, y + 7), badge, font=body_bold, fill="white")
    y += 66
    for line in _wrapped_lines(draw, data["title"], title_font, content_width):
        draw.text((margin, y), line, font=title_font, fill=dark)
        y += 58
    y += 8
    paragraph(data["summary"], color=dark, gap=8)
    paragraph(
        f"{text(data['generated_at'])}  |  Run {text(data['run_id'])}",
        font=small_font,
        color=muted,
        gap=8,
    )

    progress_rows = [*(data.get("checkpoints") or []), *(data.get("lanes") or [])]
    if progress_rows:
        heading("Progress")
        for row in progress_rows:
            card_block(
                row.get("name", ""), row.get("status", ""), row.get("detail", "")
            )
    if data.get("bottleneck"):
        heading("Current bottleneck")
        paragraph(data["bottleneck"])

    strategy = data.get("worker_strategy") or {}
    if strategy:
        heading("Worker strategy")
        paragraph(
            f"Entry: {strategy['entry_team']} / {strategy['entry_worker']}",
            font=body_bold,
        )
        chain = "  ->  ".join(
            f"{member['team']} ({member['worker']})"
            for member in strategy["configured_chain"]
        )
        paragraph(f"Configured chain: {chain}")
        observed = (
            ", ".join(
                f"{member['worker']}: {member['attempts']} attempt(s)"
                for member in strategy.get("observed_workers", [])
            )
            or "No finalized worker attempts"
        )
        paragraph(f"Observed sample: {observed}")
        if strategy.get("note"):
            paragraph(strategy["note"], font=small_font, color=muted)

    next_items = [*(data.get("remaining") or []), *(data.get("owner_actions") or [])]
    if next_items:
        heading("Next")
        for index, item in enumerate(next_items, 1):
            paragraph(f"{index}. {text(item)}")
    if data.get("model_outcomes"):
        heading("Worker model outcomes")
        for row in data["model_outcomes"]:
            paragraph(model_outcome_sentence(row), font=small_font)
    y += 12
    draw.line((margin, y, width - margin, y), fill="#d3d9e4", width=2)
    y += 20
    paragraph(
        "Interactive detail: HTML  |  Downloadable detail: PDF",
        font=small_font,
        color=muted,
    )
    final_height = min(working_height, max(420, y + 34))
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.crop((0, 0, width, final_height)).save(
        destination, format="PNG", optimize=True
    )


def _render_pdf_legacy(data: dict[str, Any], destination: Path) -> None:
    """Render the complete bounded report as a downloadable PDF."""
    if SimpleDocTemplate is None or getSampleStyleSheet is None:
        raise ValueError("PDF output requires reportlab")
    destination.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="GuardTitle",
            parent=styles["Title"],
            fontSize=20,
            leading=22,
            spaceAfter=6,
        )
    )
    styles.add(
        ParagraphStyle(
            name="GuardMeta",
            parent=styles["BodyText"],
            textColor=colors.HexColor("#586174"),
            fontSize=8.5,
            leading=11,
            alignment=TA_LEFT,
        )
    )
    styles.add(
        ParagraphStyle(
            name="GuardBody",
            parent=styles["BodyText"],
            fontSize=8.6,
            leading=10.2,
            spaceAfter=0,
        )
    )
    styles.add(
        ParagraphStyle(
            name="GuardHeading",
            parent=styles["Heading2"],
            fontSize=12.5,
            leading=14,
            spaceBefore=0,
            spaceAfter=3,
        )
    )
    styles.add(
        ParagraphStyle(
            name="GuardStatus",
            parent=styles["Heading2"],
            textColor=colors.HexColor(state_accent(data)),
            fontSize=11,
            leading=13,
            spaceAfter=5,
        )
    )
    doc = SimpleDocTemplate(
        str(destination),
        pagesize=A4,
        rightMargin=14 * mm,
        leftMargin=14 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=text(data["title"]),
        author="AFlow guard",
    )
    story: list[Any] = [
        Paragraph(html.escape(text(data["title"])), styles["GuardTitle"]),
        Paragraph(html.escape(state_name(data)), styles["GuardStatus"]),
        Paragraph(html.escape(text(data["summary"])), styles["GuardBody"]),
        Spacer(1, 2 * mm),
        Paragraph(
            html.escape(
                f"{text(data['generated_at'])} | Run {text(data['run_id'])} | {text(data['repository'])}"
            ),
            styles["GuardMeta"],
        ),
    ]

    def add_section(title: str, values: Any) -> None:
        if not values:
            return
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(html.escape(title), styles["GuardHeading"]))
        for index, value in enumerate(values, 1):
            story.append(
                Paragraph(
                    f"<b>{index}.</b> {html.escape(text(value))}", styles["GuardBody"]
                )
            )
            story.append(Spacer(1, 0.7 * mm))

    progress = []
    for row in [*(data.get("checkpoints") or []), *(data.get("lanes") or [])]:
        progress.append(
            f"{text(row.get('name', ''))} - {text(row.get('status', ''))}: {text(row.get('detail', ''))}"
        )
    add_section("Progress", progress)
    if data.get("bottleneck"):
        add_section("Current bottleneck", [data["bottleneck"]])
    add_section("Completed", data.get("completed"))
    add_section("Remaining", data.get("remaining"))
    add_section("Owner action", data.get("owner_actions"))
    strategy = data.get("worker_strategy") or {}
    if strategy:
        chain = " -> ".join(
            f"{member['team']} ({member['worker']})"
            for member in strategy["configured_chain"]
        )
        observed = (
            ", ".join(
                f"{member['worker']}: {member['attempts']} attempt(s)"
                for member in strategy.get("observed_workers", [])
            )
            or "No finalized worker attempts"
        )
        strategy_values = [
            f"Entry: {strategy['entry_team']} / {strategy['entry_worker']}",
            f"Configured chain: {chain}",
            f"Observed sample: {observed}",
        ]
        if strategy.get("note"):
            strategy_values.append(strategy["note"])
        add_section("Worker strategy", strategy_values)
    model_values = [
        model_outcome_sentence(row) for row in (data.get("model_outcomes") or [])
    ]
    add_section("Worker model outcomes", model_values)
    add_section("Evidence", data.get("evidence"))
    doc.build(story)


def _fit_lines(
    draw: Any, value: Any, font: Any, max_width: int, max_lines: int
) -> list[str]:
    lines = _wrapped_lines(draw, value, font, max_width)
    if len(lines) <= max_lines:
        return lines
    fitted = lines[:max_lines]
    last = fitted[-1]
    while last and draw.textbbox((0, 0), f"{last}...", font=font)[2] > max_width:
        last = last[:-1].rstrip()
    fitted[-1] = f"{last}..." if last else "..."
    return fitted


def _dashboard_image(data: dict[str, Any]) -> Any:
    """Build the shared single-A4 dashboard used by PNG and PDF."""
    if Image is None or ImageDraw is None:
        raise ValueError("PNG output requires Pillow")
    width, height = 1240, 1754
    image = Image.new("RGB", (width, height), "#eef2f7")
    draw = ImageDraw.Draw(image)
    margin, gap = 38, 16
    content_width = width - 2 * margin
    title_font = _image_font(40, bold=True)
    heading_font = _image_font(24, bold=True)
    body_font = _image_font(18)
    body_bold = _image_font(18, bold=True)
    small_font = _image_font(15)
    tiny_font = _image_font(13)
    metric_font = _image_font(30, bold=True)
    accent = state_accent(data)
    dark, muted, card, border = "#172033", "#586174", "#ffffff", "#d9e0ea"
    status_colors = {
        "approved": "#17834f",
        "completed": "#17834f",
        "ready": "#17834f",
        "recovered": "#17834f",
        "in progress": "#1769aa",
        "active": "#1769aa",
        "pending": "#8a6a10",
        "blocked": "#b43c32",
        "failed": "#b43c32",
    }

    def status_color(value: Any) -> str:
        label = text(value).lower()
        return next(
            (color for key, color in status_colors.items() if key in label), muted
        )

    def box(x: int, y: int, w: int, h: int, title: str | None = None) -> None:
        draw.rounded_rectangle(
            (x, y, x + w, y + h),
            radius=15,
            fill=card,
            outline=border,
            width=2,
        )
        if title:
            draw.text((x + 18, y + 15), title, font=heading_font, fill=dark)

    def lines_at(
        value: Any,
        x: int,
        y: int,
        w: int,
        *,
        font: Any = body_font,
        color: str = dark,
        max_lines: int = 2,
        leading: int | None = None,
    ) -> int:
        line_height = leading or max(21, draw.textbbox((0, 0), "Ag", font=font)[3] + 5)
        for line in _fit_lines(draw, value, font, w, max_lines):
            draw.text((x, y), line, font=font, fill=color)
            y += line_height
        return y

    def numbered_items(
        values: Any,
        x: int,
        y: int,
        w: int,
        max_items: int,
        max_lines: int,
    ) -> None:
        items = values or []
        for index, item in enumerate(items[:max_items], 1):
            draw.rounded_rectangle((x, y + 2, x + 22, y + 24), radius=7, fill="#e8eef7")
            draw.text(
                (x + 11, y + 5),
                str(index),
                font=tiny_font,
                fill=accent,
                anchor="ma",
            )
            end_y = lines_at(
                item,
                x + 31,
                y,
                w - 31,
                font=small_font,
                max_lines=max_lines,
                leading=20,
            )
            y = max(y + 45, end_y + 9)
        if len(items) > max_items:
            draw.text(
                (x, y),
                f"+{len(items) - max_items} more in HTML",
                font=tiny_font,
                fill=muted,
            )

    progress_rows = [*(data.get("checkpoints") or []), *(data.get("lanes") or [])]
    approved = sum(
        1
        for row in progress_rows
        if text(row.get("status", "")).lower()
        in {"approved", "completed", "ready", "recovered"}
    )
    total = len(progress_rows)
    strategy = data.get("worker_strategy") or {}
    observed = strategy.get("observed_workers", []) if strategy else []
    attempts = sum(member.get("attempts", 0) for member in observed)
    active_row = next(
        (
            row
            for row in progress_rows
            if "progress" in text(row.get("status", "")).lower()
            or "active" in text(row.get("status", "")).lower()
        ),
        None,
    )

    box(margin, 28, content_width, 212)
    badge = state_name(data)
    badge_width = draw.textbbox((0, 0), badge, font=body_bold)[2] + 30
    draw.rounded_rectangle(
        (margin + 20, 47, margin + 20 + badge_width, 83),
        radius=16,
        fill=accent,
    )
    draw.text((margin + 35, 54), badge, font=body_bold, fill="white")
    draw.text(
        (width - margin - 20, 58),
        text(data["generated_at"]),
        font=tiny_font,
        fill=muted,
        anchor="ra",
    )
    lines_at(
        data["title"],
        margin + 20,
        96,
        content_width - 40,
        font=title_font,
        max_lines=1,
        leading=46,
    )
    lines_at(
        data["summary"],
        margin + 20,
        145,
        content_width - 40,
        max_lines=2,
        leading=23,
    )
    lines_at(
        f"Run {text(data['run_id'])}  |  {text(data['repository'])}",
        margin + 20,
        205,
        content_width - 40,
        font=tiny_font,
        color=muted,
        max_lines=1,
    )

    metric_y, metric_h = 256, 142
    metric_w = (content_width - 3 * gap) // 4
    metrics = [
        (
            "VERIFIED ITEMS",
            f"{approved}/{total}" if total else "-",
            "verified progress",
        ),
        (
            "ACTIVE POSITION",
            text(active_row.get("name")) if active_row else state_name(data),
            text(active_row.get("status")) if active_row else "report state",
        ),
        ("WORKER ATTEMPTS", str(attempts), f"{len(observed)} sampled model(s)"),
        (
            "ENTRY TEAM",
            text(strategy.get("entry_team", "-")),
            text(strategy.get("entry_worker", "not supplied")),
        ),
    ]
    for index, (label, value, note) in enumerate(metrics):
        x = margin + index * (metric_w + gap)
        box(x, metric_y, metric_w, metric_h)
        draw.text((x + 16, metric_y + 14), label, font=tiny_font, fill=muted)
        lines_at(
            value,
            x + 16,
            metric_y + 43,
            metric_w - 32,
            font=metric_font,
            max_lines=1,
            leading=37,
        )
        lines_at(
            note,
            x + 16,
            metric_y + 91,
            metric_w - 32,
            font=small_font,
            color=muted,
            max_lines=1,
        )
        if index == 0 and total:
            draw.rounded_rectangle(
                (x + 16, metric_y + 119, x + metric_w - 16, metric_y + 128),
                radius=4,
                fill="#dfe5ed",
            )
            fill_w = int((metric_w - 32) * approved / total)
            if fill_w:
                draw.rounded_rectangle(
                    (x + 16, metric_y + 119, x + 16 + fill_w, metric_y + 128),
                    radius=4,
                    fill="#17834f",
                )

    left_x, top_y = margin, 414
    left_w, right_w = 742, content_width - 742 - gap
    right_x = left_x + left_w + gap

    box(left_x, top_y, left_w, 500, "Progress map")
    plot_x, plot_y, plot_w = left_x + 18, top_y + 56, left_w - 36
    segments = [
        ("verified", approved, "#17834f"),
        (
            "active",
            sum(
                1
                for row in progress_rows
                if status_color(row.get("status")) == "#1769aa"
            ),
            "#1769aa",
        ),
        (
            "pending",
            sum(
                1
                for row in progress_rows
                if status_color(row.get("status")) == "#8a6a10"
            ),
            "#8a6a10",
        ),
        (
            "blocked",
            sum(
                1
                for row in progress_rows
                if status_color(row.get("status")) == "#b43c32"
            ),
            "#b43c32",
        ),
    ]
    if total:
        draw.rounded_rectangle(
            (plot_x, plot_y, plot_x + plot_w, plot_y + 16),
            radius=7,
            fill="#dfe5ed",
        )
        cursor = plot_x
        for _, count, color in segments:
            seg_w = round(plot_w * count / total)
            if seg_w:
                draw.rectangle(
                    (cursor, plot_y, cursor + seg_w, plot_y + 16), fill=color
                )
                cursor += seg_w
        legend_x = plot_x
        for label, count, color in segments:
            if count:
                draw.ellipse(
                    (legend_x, plot_y + 28, legend_x + 10, plot_y + 38),
                    fill=color,
                )
                draw.text(
                    (legend_x + 15, plot_y + 24),
                    f"{label} {count}",
                    font=tiny_font,
                    fill=muted,
                )
                legend_x += 110
    row_y = plot_y + 57
    for row in progress_rows[:6]:
        color = status_color(row.get("status"))
        draw.rounded_rectangle(
            (plot_x, row_y, plot_x + plot_w, row_y + 58),
            radius=9,
            fill="#f7f9fc",
        )
        draw.rectangle((plot_x, row_y, plot_x + 7, row_y + 58), fill=color)
        lines_at(
            row.get("name", ""),
            plot_x + 17,
            row_y + 8,
            250,
            font=body_bold,
            max_lines=1,
        )
        lines_at(
            row.get("status", ""),
            plot_x + 274,
            row_y + 8,
            105,
            font=small_font,
            color=color,
            max_lines=1,
        )
        lines_at(
            row.get("detail", ""),
            plot_x + 390,
            row_y + 7,
            plot_w - 400,
            font=tiny_font,
            color=muted,
            max_lines=2,
            leading=18,
        )
        row_y += 64

    box(right_x, top_y, right_w, 360, "Worker strategy")
    chain = strategy.get("configured_chain", []) if strategy else []
    cy = top_y + 58
    for index, member in enumerate(chain[:3]):
        node_color = accent if index == 0 else muted
        draw.rounded_rectangle(
            (right_x + 18, cy, right_x + right_w - 18, cy + 48),
            radius=10,
            fill="#f6f8fb",
            outline=node_color,
            width=2,
        )
        lines_at(
            member.get("team", ""),
            right_x + 30,
            cy + 7,
            145,
            font=body_bold,
            max_lines=1,
        )
        lines_at(
            member.get("worker", ""),
            right_x + 178,
            cy + 9,
            right_w - 214,
            font=small_font,
            color=muted,
            max_lines=1,
        )
        cy += 58
        if index < len(chain[:3]) - 1:
            draw.line(
                (
                    right_x + right_w // 2,
                    cy - 10,
                    right_x + right_w // 2,
                    cy,
                ),
                fill=muted,
                width=2,
            )
    if len(chain) > 3:
        draw.text(
            (right_x + 18, cy),
            f"+{len(chain) - 3} configured upgrades",
            font=tiny_font,
            fill=muted,
        )
        cy += 24
    draw.text((right_x + 18, cy + 2), "OBSERVED ATTEMPTS", font=tiny_font, fill=muted)
    cy += 28
    max_attempts = max((member.get("attempts", 0) for member in observed), default=1)
    for member in observed[:3]:
        label = text(member.get("worker", ""))
        value = member.get("attempts", 0)
        lines_at(
            label,
            right_x + 18,
            cy,
            155,
            font=tiny_font,
            max_lines=1,
        )
        bar_x, bar_w = right_x + 176, right_w - 220
        draw.rounded_rectangle(
            (bar_x, cy + 3, bar_x + bar_w, cy + 16),
            radius=6,
            fill="#dfe5ed",
        )
        fill_w = round(bar_w * value / max_attempts) if max_attempts else 0
        if fill_w:
            draw.rounded_rectangle(
                (bar_x, cy + 3, bar_x + fill_w, cy + 16),
                radius=6,
                fill=accent,
            )
        draw.text(
            (right_x + right_w - 20, cy),
            str(value),
            font=tiny_font,
            fill=dark,
            anchor="ra",
        )
        cy += 28
    if strategy.get("note"):
        lines_at(
            strategy["note"],
            right_x + 18,
            top_y + 306,
            right_w - 36,
            font=tiny_font,
            color=muted,
            max_lines=2,
            leading=17,
        )

    bottleneck_y = 930
    box(left_x, bottleneck_y, left_w, 180, "Current bottleneck")
    lines_at(
        data.get("bottleneck", "No current bottleneck reported."),
        left_x + 18,
        bottleneck_y + 58,
        left_w - 36,
        max_lines=4,
        leading=24,
    )

    box(right_x, top_y + 376, right_w, 320, "Worker outcomes")
    models = data.get("model_outcomes") or []
    my = top_y + 432
    if not models:
        lines_at(
            "No finalized worker-model outcomes supplied.",
            right_x + 18,
            my,
            right_w - 36,
            font=small_font,
            color=muted,
            max_lines=2,
        )
    max_model_attempts = max(
        (int(row.get("attempts", 0) or 0) for row in models), default=1
    )
    for row in models[:4]:
        model = text(row.get("model", ""))
        model_attempts = int(row.get("attempts", 0) or 0)
        approved_count = int(row.get("approved", 0) or 0)
        rejected_count = int(row.get("rejected", 0) or 0)
        lines_at(
            model,
            right_x + 18,
            my,
            right_w - 36,
            font=body_bold,
            max_lines=1,
        )
        bar_x, bar_y, bar_w = right_x + 18, my + 26, right_w - 78
        draw.rounded_rectangle(
            (bar_x, bar_y, bar_x + bar_w, bar_y + 16),
            radius=6,
            fill="#dfe5ed",
        )
        attempt_w = (
            round(bar_w * model_attempts / max_model_attempts)
            if max_model_attempts
            else 0
        )
        if attempt_w:
            draw.rounded_rectangle(
                (bar_x, bar_y, bar_x + attempt_w, bar_y + 16),
                radius=6,
                fill="#7890ae",
            )
        if model_attempts and (approved_count or rejected_count):
            approved_w = round(attempt_w * approved_count / model_attempts)
            rejected_w = round(attempt_w * rejected_count / model_attempts)
            if approved_w:
                draw.rectangle(
                    (bar_x, bar_y, bar_x + approved_w, bar_y + 16),
                    fill="#17834f",
                )
            if rejected_w:
                draw.rectangle(
                    (
                        bar_x + approved_w,
                        bar_y,
                        bar_x + approved_w + rejected_w,
                        bar_y + 16,
                    ),
                    fill="#b43c32",
                )
        draw.text(
            (right_x + right_w - 18, bar_y - 2),
            str(model_attempts),
            font=tiny_font,
            fill=dark,
            anchor="ra",
        )
        lines_at(
            row.get("note", ""),
            right_x + 18,
            my + 48,
            right_w - 36,
            font=tiny_font,
            color=muted,
            max_lines=1,
        )
        my += 68

    lower_y, lower_h = 1126, 536
    half_w = (left_w - gap) // 2
    box(left_x, lower_y, half_w, lower_h, "Completed")
    box(left_x + half_w + gap, lower_y, half_w, lower_h, "Next")
    box(right_x, lower_y, right_w, lower_h, "Evidence / owner action")
    numbered_items(data.get("completed"), left_x + 18, lower_y + 58, half_w - 36, 7, 4)
    next_items = [*(data.get("remaining") or []), *(data.get("owner_actions") or [])]
    numbered_items(
        next_items,
        left_x + half_w + gap + 18,
        lower_y + 58,
        half_w - 36,
        7,
        4,
    )
    evidence_items = [*(data.get("evidence") or [])]
    if data.get("owner_actions"):
        evidence_items = [
            f"OWNER: {item}" for item in data["owner_actions"]
        ] + evidence_items
    numbered_items(
        evidence_items,
        right_x + 18,
        lower_y + 58,
        right_w - 36,
        8,
        4,
    )

    draw.line((margin, 1690, width - margin, 1690), fill="#cfd8e5", width=2)
    draw.text(
        (margin, 1705),
        "AFlow guard - one-page evidence dashboard",
        font=tiny_font,
        fill=muted,
    )
    draw.text(
        (width - margin, 1705),
        "Interactive detail: HTML",
        font=tiny_font,
        fill=muted,
        anchor="ra",
    )
    return image


def render_image(data: dict[str, Any], destination: Path) -> None:
    """Render an Android-safe single-A4 dashboard PNG."""
    image = _dashboard_image(data)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True, dpi=(150, 150))


def render_pdf(data: dict[str, Any], destination: Path) -> None:
    """Render the same dashboard as exactly one A4 PDF page."""
    if canvas is None or ImageReader is None or A4 is None:
        raise ValueError("PDF output requires reportlab")
    destination.parent.mkdir(parents=True, exist_ok=True)
    image = _dashboard_image(data)
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    page_width, page_height = A4
    pdf = canvas.Canvas(str(destination), pagesize=A4)
    pdf.setTitle(text(data["title"]))
    pdf.setAuthor("AFlow guard")
    pdf.drawImage(
        ImageReader(buffer),
        0,
        0,
        width=page_width,
        height=page_height,
        preserveAspectRatio=True,
        anchor="c",
    )
    pdf.showPage()
    pdf.save()


def artifact_manifest(args: argparse.Namespace, data: dict[str, Any]) -> dict[str, Any]:
    paths = {
        "markdown": args.markdown_out,
        "interactive_html": args.html_out,
        "mobile_image": args.image_out,
        "pdf": args.pdf_out,
        "email_html": args.email_html_out,
    }
    return {
        "schema_version": 1,
        "state": data["state"],
        "run_id": data["run_id"],
        "artifacts": {
            key: str(path.expanduser().resolve())
            for key, path in paths.items()
            if path is not None
        },
    }


def main() -> int:
    args = parse_args()
    try:
        data = load_input(args.input)
        validate(data)
        markdown = render_markdown(data)
        if args.markdown_out:
            args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
            args.markdown_out.write_text(markdown, encoding="utf-8")
        if args.html_out:
            args.html_out.parent.mkdir(parents=True, exist_ok=True)
            args.html_out.write_text(render_html(data, markdown), encoding="utf-8")
        if args.image_out:
            render_image(data, args.image_out)
        if args.pdf_out:
            render_pdf(data, args.pdf_out)
        if args.email_html_out:
            args.email_html_out.parent.mkdir(parents=True, exist_ok=True)
            args.email_html_out.write_text(render_email_html(data), encoding="utf-8")
        manifest = artifact_manifest(args, data)
        if args.manifest_out:
            args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
            args.manifest_out.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        print(json.dumps(manifest, separators=(",", ":"), sort_keys=True))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"aflow_guard_report: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
