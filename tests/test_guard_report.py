from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys
import tomllib

import pytest
from PIL import Image, ImageDraw


SCRIPT_DIR = (
    Path(__file__).resolve().parents[1]
    / "aflow"
    / "bundled_skills"
    / "aflow-guard-development-run"
    / "scripts"
)
INPUT_HELPER_PATH = SCRIPT_DIR / "aflow_guard_report_input.py"
RENDERER_PATH = SCRIPT_DIR / "aflow_guard_report.py"


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _helper():
    return _module(INPUT_HELPER_PATH, "guard_report_input_for_renderer_tests")


def _renderer():
    return _module(RENDERER_PATH, "guard_report_renderer_for_tests")


def _report_input(tmp_path: Path) -> dict[str, object]:
    helper = _helper()
    repository = tmp_path / "guarded-repository"
    repository.mkdir()
    snapshot = {
        "schema_version": 3,
        "observed_at": "2026-09-11T12:01:00+00:00",
        "repo": str(repository),
        "run_id": "20260911T120000Z-abcd1234",
        "ownership": "legacy",
        "activity": "inactive",
        "classification": "terminal_success",
        "changed_since_previous": True,
        "notification_already_sent": False,
        "run": {
            "status": "completed",
            "current_step": "implement",
            "last_snapshot": {
                "current_checkpoint_index": 1,
                "current_checkpoint_name": "Checkpoint 1: Normalize",
                "total_checkpoint_count": 3,
                "is_complete": True,
            },
        },
        "progress": {
            "availability": "available",
            "checkpoint": {"index": 1, "name": "Checkpoint 1: Normalize"},
            "total": 3,
            "last_finished_turn": {
                "turn_number": 1,
                "step": "implement",
                "status": "completed",
                "summary": "implemented the bounded change",
            },
            "current_turn": None,
        },
        "latest_result": {
            "name": "turn-001",
            "path": "turns/turn-001/result.json",
            "turn_number": 1,
            "status": "completed",
            "finished_at": "2026-09-11T12:00:05+00:00",
            "finalized": True,
            "step": "implement",
            "summary": "implemented the bounded change",
            "selector": "codex.worker",
        },
        "processes": {"controller_count": 0, "child_pids": [101, 102]},
    }
    return helper.build_report_input(snapshot)


def _write_input(tmp_path: Path, value: dict[str, object]) -> Path:
    path = tmp_path / "report-input.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _repo_files(repository: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(repository)): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file()
    }


def test_pillow_is_dev_only_and_standalone_dependency_is_declared() -> None:
    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert all("pillow" not in dependency.casefold() for dependency in project["project"]["dependencies"])
    assert "pillow>=11.0.0" in project["dependency-groups"]["dev"]
    assert '# dependencies = ["pillow>=11.0.0"]' in RENDERER_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("case", "status", "activity"),
    (
        ("healthy", "completed", "inactive"),
        ("failed", "failed", "inactive"),
        ("unknown", None, "unknown"),
        ("minimal", None, "unknown"),
    ),
)
def test_render_healthy_failed_unknown_and_minimal_inputs(
    tmp_path: Path, case: str, status: str | None, activity: str
) -> None:
    renderer = _renderer()
    value = _report_input(tmp_path)
    value["status"] = status
    value["activity"] = activity
    if case == "minimal":
        for key in (
            "checkpoint_index",
            "checkpoint_name",
            "checkpoint_total",
            "step",
            "finalized_turn_summary",
            "worker_selector",
            "worker_model",
            "worker_effort",
            "ownership",
        ):
            value[key] = None
        value["diagnosis"] = {
            "confirmed_facts": [],
            "likely_cause": None,
            "supporting_evidence": [],
            "contradicting_evidence": [],
            "unknowns": [],
            "confidence": "unknown",
            "duplicate_operation_risk": None,
            "owner_action": None,
        }
    input_path = _write_input(tmp_path, value)
    repository = Path(str(value["repository"]))
    before = _repo_files(repository)

    png_path, json_path = renderer.render_report(input_path, tmp_path / f"{case}-artifacts")

    assert png_path.name == "guard-report.png"
    assert json_path.name == "guard-report.json"
    with Image.open(png_path) as image:
        assert image.size == (1240, 1754)
        assert image.mode == "RGB"
        assert image.info == {}
    normalized = json.loads(json_path.read_text(encoding="utf-8"))
    assert normalized == value
    assert list(normalized) == list(renderer.INPUT_KEYS)
    assert list(normalized["diagnosis"]) == list(renderer.DIAGNOSIS_KEYS)
    assert _repo_files(repository) == before


def test_long_text_is_bounded_on_canvas_and_preserved_in_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = _renderer()
    value = _report_input(tmp_path)
    long_text = "long display field " + ("x" * 1_000)
    owner_action = "Inspect this action: " + ("x" * 1_000)
    value["checkpoint_name"] = long_text
    value["finalized_turn_summary"] = long_text
    value["diagnosis"]["owner_action"] = owner_action
    value["diagnosis"]["confirmed_facts"] = [long_text] * 8
    captured: dict[str, list[str]] = {}
    original_draw_lines = renderer._draw_lines

    def capture_draw_lines(draw, display_value, x, y, width, **kwargs):
        if display_value in {long_text, owner_action}:
            captured[str(display_value)] = renderer._fit_lines(
                draw,
                display_value,
                kwargs["font"],
                width,
                kwargs.get("max_lines", 2),
            )
        return original_draw_lines(draw, display_value, x, y, width, **kwargs)

    monkeypatch.setattr(renderer, "_draw_lines", capture_draw_lines)
    input_path = _write_input(tmp_path, value)
    png_path, json_path = renderer.render_report(input_path, tmp_path / "long-artifacts")

    assert json.loads(json_path.read_text(encoding="utf-8")) == value
    assert captured[long_text][-1].endswith("…")
    assert captured[owner_action][-1].endswith("…")
    with Image.open(png_path) as image:
        assert image.size == (renderer.WIDTH, renderer.HEIGHT)
    draw_image = Image.new("RGB", (500, 100), "white")
    try:
        draw = ImageDraw.Draw(draw_image)
        font = renderer._font(18)
        assert renderer._ellipsize(draw, "x" * 500, font, 220).endswith("…")
    finally:
        draw_image.close()


def test_fit_lines_marks_wrapped_and_word_truncation_visibly() -> None:
    renderer = _renderer()
    image = Image.new("RGB", (600, 200), "white")
    try:
        draw = ImageDraw.Draw(image)
        font = renderer._font(18)
        ordinary = renderer._fit_lines(
            draw,
            "one short phrase then another phrase that must wrap beyond two visible lines",
            font,
            130,
            2,
        )
        long_word = renderer._fit_lines(draw, "x" * 500, font, 130, 2)
        short_final = renderer._fit_lines(
            draw,
            "a deliberately long first line then tail",
            font,
            100,
            1,
        )
        assert ordinary[-1].endswith("…")
        assert long_word[-1].endswith("…")
        assert short_final[-1].endswith("…")
    finally:
        image.close()


def test_header_and_checkpoint_fields_have_disjoint_drawn_extents(tmp_path: Path) -> None:
    renderer = _renderer()
    value = _report_input(tmp_path)
    value["run_id"] = "run-" + ("a" * 112)
    value["checkpoint_name"] = (
        "Checkpoint validates the canonical ownership-bound observation before rendering "
        "the bounded report"
    )
    image = Image.new("RGB", (renderer.WIDTH, renderer.HEIGHT), "white")
    base_draw = ImageDraw.Draw(image)
    calls: list[tuple[str, tuple[int, int, int, int]]] = []

    class RecordingDraw:
        def text(self, xy, text, *, font=None, **kwargs):
            calls.append((str(text), base_draw.textbbox(xy, str(text), font=font)))
            return base_draw.text(xy, text, font=font, **kwargs)

        def __getattr__(self, name):
            return getattr(base_draw, name)

    try:
        draw = RecordingDraw()
        renderer._draw_header(draw, value)
        renderer._draw_progress(draw, value)
        run_extent = next(extent for text, extent in calls if text.startswith("run-"))
        observed_extent = next(
            extent for text, extent in calls if text == str(value["observed_at"])
        )
        assert run_extent[2] < observed_extent[0]

        checkpoint_label = next(
            index for index, (text, _) in enumerate(calls) if text == "CHECKPOINT NAME"
        )
        step_label = next(index for index, (text, _) in enumerate(calls) if text == "STEP")
        checkpoint_extents = [extent for _, extent in calls[checkpoint_label + 1 : step_label]]
        assert len(checkpoint_extents) == 2
        step_extent = calls[step_label][1]
        assert max(extent[3] for extent in checkpoint_extents) < step_extent[1]
    finally:
        image.close()


def test_unicode_and_missing_glyphs_render_without_changing_json(tmp_path: Path) -> None:
    renderer = _renderer()
    value = _report_input(tmp_path)
    value["checkpoint_name"] = "Révision 東京 🚦"
    value["diagnosis"]["owner_action"] = "Inspect 東京 🚦 evidence — do not relaunch blindly."
    original = copy.deepcopy(value)
    input_path = _write_input(tmp_path, value)

    png_path, json_path = renderer.render_report(input_path, tmp_path / "unicode-artifacts")

    assert value == original
    assert json.loads(json_path.read_text(encoding="utf-8")) == original
    display = renderer._safe_display_text("Révision 東京 🚦 …", renderer._font(18))
    assert "🚦" not in display
    assert "…" in display
    assert png_path.stat().st_size > 1_000


def test_repeat_render_is_byte_identical_and_layout_is_fixed(tmp_path: Path) -> None:
    renderer = _renderer()
    value = _report_input(tmp_path)
    input_path = _write_input(tmp_path, value)
    first_png, first_json = renderer.render_report(input_path, tmp_path / "first-artifacts")
    second_png, second_json = renderer.render_report(input_path, tmp_path / "second-artifacts")

    assert first_png.read_bytes() == second_png.read_bytes()
    assert first_json.read_bytes() == second_json.read_bytes()
    rects = renderer.section_rects()
    assert tuple(rects) == renderer.SECTION_ORDER
    assert [rects[name][1] for name in renderer.SECTION_ORDER] == sorted(
        rects[name][1] for name in renderer.SECTION_ORDER
    )
    assert max(y + height for _, y, _, height in rects.values()) < renderer.HEIGHT
    json_text = first_json.read_text(encoding="utf-8")
    assert json_text.index('"repository"') < json_text.index('"diagnosis"')
    assert json_text.index('"confidence"') < json_text.index('"owner_action"')


def test_invalid_schema_and_repository_output_rejection_preserve_valid_report(
    tmp_path: Path,
) -> None:
    renderer = _renderer()
    value = _report_input(tmp_path)
    input_path = _write_input(tmp_path, value)
    output_dir = tmp_path / "valid-artifacts"
    png_path, json_path = renderer.render_report(input_path, output_dir)
    valid_png = png_path.read_bytes()
    valid_json = json_path.read_bytes()

    invalid = copy.deepcopy(value)
    invalid["schema_version"] = 99
    invalid_path = tmp_path / "invalid-input.json"
    invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(renderer.ReportRenderError, match="rejected"):
        renderer.render_report(invalid_path, output_dir)
    assert png_path.read_bytes() == valid_png
    assert json_path.read_bytes() == valid_json

    malformed_path = tmp_path / "malformed-input.json"
    malformed_path.write_text("{not valid JSON", encoding="utf-8")
    with pytest.raises(renderer.ReportRenderError, match="unreadable"):
        renderer.render_report(malformed_path, output_dir)
    assert png_path.read_bytes() == valid_png
    assert json_path.read_bytes() == valid_json

    repository = Path(str(value["repository"]))
    with pytest.raises(renderer.ReportRenderError, match="outside"):
        renderer.render_report(input_path, repository / "reports")
    assert _repo_files(repository) == {}

    symlink_target = tmp_path / "symlink-target"
    symlink_target.mkdir()
    symlink_path = tmp_path / "symlink-output"
    try:
        symlink_path.symlink_to(symlink_target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable in this test environment")
    with pytest.raises(renderer.ReportRenderError, match="symlink"):
        renderer.render_report(input_path, symlink_path)
    assert not (symlink_target / "guard-report.png").exists()
