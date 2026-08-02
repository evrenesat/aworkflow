from __future__ import annotations

import pytest

from aflow.git_status import (
    classify_dirtiness_by_prefix,
    is_lifecycle_owned_path,
    porcelain_status_path,
    porcelain_status_paths,
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".aflow", True),
        (".aflow/runs/old/run.json", True),
        (".aflow-copy/file", False),
        ("src/.aflow/file", False),
        ("plans/in-progress/plan.md", False),
        ("notes.txt", False),
    ],
)
def test_aflow_lifecycle_owned_path_boundary(path: str, expected: bool) -> None:
    assert is_lifecycle_owned_path(path) is expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('?? ".aflow/runs/old/run.json"', ".aflow/runs/old/run.json"),
        ("R  old.txt -> .aflow/runs/new.json", ".aflow/runs/new.json"),
        ('R  "old name.txt" -> ".aflow/runs/new name.json"', ".aflow/runs/new name.json"),
        ('?? "src/quoted\\tname.txt"', "src/quoted\tname.txt"),
    ],
)
def test_porcelain_status_path_normalizes_quotes_and_renames(
    line: str,
    expected: str,
) -> None:
    assert porcelain_status_path(line) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('?? "foo -> .aflow/runs/x"', ("foo -> .aflow/runs/x",)),
        (' M "foo -> .aflow/runs/x"', ("foo -> .aflow/runs/x",)),
        (
            'R  "old -> name.txt" -> ".aflow/new -> name.txt"',
            ("old -> name.txt", ".aflow/new -> name.txt"),
        ),
        (
            'C  ".aflow/source -> old" -> "plans/copy -> new.md"',
            (".aflow/source -> old", "plans/copy -> new.md"),
        ),
        ('R  notes.txt -> "unterminated', None),
        ('R  notes.txt', None),
    ],
)
def test_porcelain_status_paths_preserves_complete_records(
    line: str,
    expected: tuple[str, ...] | None,
) -> None:
    assert porcelain_status_paths(line) == expected


def test_aflow_owned_dirtiness_is_ignored_only_when_requested() -> None:
    porcelain = "\n".join(
        (
            "?? .aflow",
            "?? .aflow/runs/old/run.json",
            "?? .aflow-copy/file",
            "?? src/.aflow/file",
            "?? plans/in-progress/plan.md",
            " M notes.txt",
        )
    )

    plan_paths, non_plan_paths = classify_dirtiness_by_prefix(
        porcelain,
        ignore_lifecycle_owned=True,
    )

    assert plan_paths == ["plans/in-progress/plan.md"]
    assert non_plan_paths == [".aflow-copy/file", "src/.aflow/file", "notes.txt"]
    _, unfiltered_paths = classify_dirtiness_by_prefix(porcelain)
    assert ".aflow" in unfiltered_paths
    assert ".aflow/runs/old/run.json" in unfiltered_paths


@pytest.mark.parametrize(
    ("line", "expected_plan", "expected_non_plan"),
    [
        ('?? "foo -> .aflow/runs/x"', [], ["foo -> .aflow/runs/x"]),
        (' M "foo -> .aflow/runs/x"', [], ["foo -> .aflow/runs/x"]),
        ("R  .aflow/old -> .aflow/new", [], []),
        (
            "R  plans/p.md -> .aflow/p.md",
            ["plans/p.md"],
            [],
        ),
        (
            "R  notes.txt -> .aflow/x",
            [],
            ["notes.txt"],
        ),
        (
            "R  .aflow/x -> notes.txt",
            [],
            ["notes.txt"],
        ),
        (
            'R  "notes -> old.txt" -> ".aflow/new -> x"',
            [],
            ["notes -> old.txt"],
        ),
        (
            'R  notes.txt -> "unterminated',
            [],
            ['notes.txt -> "unterminated'],
        ),
    ],
)
def test_lifecycle_owned_dirtiness_evaluates_every_record_path(
    line: str,
    expected_plan: list[str],
    expected_non_plan: list[str],
) -> None:
    assert classify_dirtiness_by_prefix(
        line,
        ignore_lifecycle_owned=True,
    ) == (expected_plan, expected_non_plan)
