"""Tests for AFLOW_SCOPE_PRESSURE parsing and stop-marker precedence."""

from aflow.scope_pressure import (
    SCOPE_PRESSURE_FALLBACK_REASON,
    SCOPE_PRESSURE_SENTINEL_PREFIX,
    detect_scope_pressure,
    extract_scope_pressure_markers,
    has_scope_pressure,
)
from aflow.stop_marker import detect_stop_marker


def test_extracts_real_scope_pressure() -> None:
    stdout = f"{SCOPE_PRESSURE_SENTINEL_PREFIX} checkpoint is too large for one pass\nsome output"
    markers = extract_scope_pressure_markers(stdout)
    assert markers == ["checkpoint is too large for one pass"]


def test_ignores_fenced_examples() -> None:
    stdout = (
        "```\n"
        f"{SCOPE_PRESSURE_SENTINEL_PREFIX} <reason>\n"
        "```\n"
        "real output\n"
    )
    markers = extract_scope_pressure_markers(stdout)
    assert markers == []


def test_ignores_placeholder_reason() -> None:
    stdout = f"{SCOPE_PRESSURE_SENTINEL_PREFIX} <reason>"
    markers = extract_scope_pressure_markers(stdout)
    assert markers == []


def test_empty_reason_gets_fallback() -> None:
    stdout = f"{SCOPE_PRESSURE_SENTINEL_PREFIX}   "
    markers = extract_scope_pressure_markers(stdout)
    assert markers == [SCOPE_PRESSURE_FALLBACK_REASON]


def test_detect_returns_first() -> None:
    stdout = f"{SCOPE_PRESSURE_SENTINEL_PREFIX} first\n{SCOPE_PRESSURE_SENTINEL_PREFIX} second"
    stderr = ""
    result = detect_scope_pressure(stdout, stderr)
    assert result == "first"


def test_stdout_before_stderr_priority() -> None:
    stdout = f"{SCOPE_PRESSURE_SENTINEL_PREFIX} from stdout"
    stderr = f"{SCOPE_PRESSURE_SENTINEL_PREFIX} from stderr"
    result = detect_scope_pressure(stdout, stderr)
    assert result == "from stdout"


def test_falls_back_to_stderr() -> None:
    stdout = "no pressure here"
    stderr = f"{SCOPE_PRESSURE_SENTINEL_PREFIX} from stderr"
    result = detect_scope_pressure(stdout, stderr)
    assert result == "from stderr"


def test_has_scope_pressure_true() -> None:
    assert has_scope_pressure(
        f"{SCOPE_PRESSURE_SENTINEL_PREFIX} real", ""
    ) is True


def test_has_scope_pressure_false() -> None:
    assert has_scope_pressure("plain output", "") is False


def test_stop_beats_scope_pressure() -> None:
    """When both AFLOW_STOP and AFLOW_SCOPE_PRESSURE appear, stop wins."""
    stdout = (
        f"{SCOPE_PRESSURE_SENTINEL_PREFIX} oversized scope\n"
        "AFLOW_STOP: real stop reason\n"
    )
    stderr = ""
    # Scope pressure is present ...
    assert has_scope_pressure(stdout, stderr) is True
    # ... but stop is detected first and takes precedence.
    stop_reason = detect_stop_marker(stdout, stderr)
    assert stop_reason == "real stop reason"


def test_scope_pressure_without_stop() -> None:
    """Scope pressure without stop is detected correctly."""
    stdout = f"{SCOPE_PRESSURE_SENTINEL_PREFIX} needs repartition\n"
    stderr = ""
    assert has_scope_pressure(stdout, stderr) is True
    assert detect_stop_marker(stdout, stderr) is None


def test_parse_scope_pressure_returns_canonical_result() -> None:
    """parse_scope_pressure returns ScopePressureResult with detected and reason."""
    from aflow.scope_pressure import parse_scope_pressure

    result = parse_scope_pressure("plain output", "")
    assert result.detected is False
    assert result.reason is None

    result = parse_scope_pressure(
        f"{SCOPE_PRESSURE_SENTINEL_PREFIX} checkpoint too large", ""
    )
    assert result.detected is True
    assert result.reason == "checkpoint too large"


def test_parse_scope_pressure_stdout_priority() -> None:
    """parse_scope_pressure returns first reason from stdout before stderr."""
    from aflow.scope_pressure import parse_scope_pressure

    result = parse_scope_pressure(
        f"{SCOPE_PRESSURE_SENTINEL_PREFIX} from stdout",
        f"{SCOPE_PRESSURE_SENTINEL_PREFIX} from stderr",
    )
    assert result.detected is True
    assert result.reason == "from stdout"


def test_parse_scope_pressure_falls_back_to_stderr() -> None:
    """parse_scope_pressure returns stderr reason when stdout has none."""
    from aflow.scope_pressure import parse_scope_pressure

    result = parse_scope_pressure(
        "no pressure",
        f"{SCOPE_PRESSURE_SENTINEL_PREFIX} from stderr",
    )
    assert result.detected is True
    assert result.reason == "from stderr"
