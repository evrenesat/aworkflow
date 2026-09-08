from pathlib import Path
import subprocess

import pytest

from aflow.cli import main
from aflow.noop_plan import NoopError, execute_noop_step, install_noop_plan
from aflow.plan import load_plan


@pytest.fixture
def fixture(tmp_path):
    state = tmp_path / "state"
    plan = tmp_path / "noop.md"
    install_noop_plan(plan, state_dir=state)
    return plan, state


def test_installed_plan_records_current_branch_for_in_place_workflow(tmp_path):
    subprocess.run(
        ["git", "init", "-q", "-b", "smoke-branch", str(tmp_path)], check=True
    )
    plan = tmp_path / "plans/noop.md"
    install_noop_plan(plan, state_dir=tmp_path / "external")
    assert "- Plan Branch: `smoke-branch`" in plan.read_text()


def test_cli_initializes_parseable_plan_in_current_directory(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    state = tmp_path / "external state"
    assert main(["noop-plan", "--state-dir", str(state), "--checkpoints", "2"]) == 0
    plan = tmp_path / "aflow-noop-plan.md"
    parsed = load_plan(plan)
    assert parsed.snapshot.total_checkpoint_count == 2
    assert parsed.snapshot.unchecked_checkpoint_count == 2
    assert "{{" not in plan.read_text()
    assert str(state) in plan.read_text()
    assert (state / "worker.playbook").is_file()
    assert (state / "reviewer.playbook").is_file()
    assert "aflow run --plan" in capsys.readouterr().out


def test_defaults_complete_without_sleep(fixture, monkeypatch):
    plan, state = fixture
    before = plan.read_bytes()
    monkeypatch.setattr(
        "aflow.noop_plan.subprocess.run",
        lambda *a, **kw: pytest.fail("unexpected sleep"),
    )
    assert execute_noop_step("worker", 1, state_dir=state)[0] == 0
    assert (state / "checkpoints/CP_1.txt").is_file()
    assert execute_noop_step("reviewer", 1, state_dir=state)[0] == 0
    assert execute_noop_step("reviewer", 99, state_dir=state)[0] == 0
    assert plan.read_bytes() == before  # The agent owns plan bookkeeping.


def test_cleanup_is_scoped_and_success_recreates_current_marker(fixture):
    _, state = fixture
    marks = state / "checkpoints"
    for name in ["CP_1.txt", "CP_2.txt", "keep.txt", "CP_notes.txt"]:
        (marks / name).touch()
    (state / "worker.playbook").write_text("CP1: cleanup; done\n")
    assert execute_noop_step("worker", 1, state_dir=state)[0] == 0
    assert sorted(p.name for p in marks.iterdir()) == [
        "CP_1.txt",
        "CP_notes.txt",
        "keep.txt",
    ]


def test_sleep_runs_system_command_and_playbooks_are_reloaded(fixture, monkeypatch):
    _, state = fixture
    calls = []
    monkeypatch.setattr(
        "aflow.noop_plan.subprocess.run", lambda *a, **kw: calls.append((a, kw))
    )
    book = state / "reviewer.playbook"
    book.write_text("CP1: sleep 60; pass\nCP2: sleep 0; pass\n")
    execute_noop_step("reviewer", 1, state_dir=state)
    execute_noop_step("reviewer", 2, state_dir=state)
    assert calls == [
        ((["sleep", "60"],), {"check": True}),
        ((["sleep", "0"],), {"check": True}),
    ]
    book.write_text("CP1: fail BOOO\n")
    assert execute_noop_step("reviewer", 1, state_dir=state) == (
        1,
        "AFLOW_STOP: AFLOW_NOOP_MOCK_FAILURE CP1: BOOO",
    )


def test_worker_failure_removes_stale_marker_and_rejection_preserves_evidence(fixture):
    _, state = fixture
    execute_noop_step("worker", 3, state_dir=state)
    (state / "reviewer.playbook").write_text("CP3: reject repair please\n")
    assert execute_noop_step("reviewer", 3, state_dir=state) == (
        2,
        "AFLOW_NOOP_REJECT CP3: repair please",
    )
    assert (state / "checkpoints/CP_3.txt").exists()
    (state / "worker.playbook").write_text("CP3: fail BLAH BLAH\n")
    assert execute_noop_step("worker", 3, state_dir=state) == (
        1,
        "AFLOW_STOP: AFLOW_NOOP_MOCK_FAILURE CP3: BLAH BLAH",
    )
    assert not (state / "checkpoints/CP_3.txt").exists()


@pytest.mark.parametrize(
    "text",
    [
        "CP1: sleep -1",
        "CP1: sleep 1; rm /tmp/foo",
        "CP1: done\nCP1: fail duplicate",
        "CP0: done",
        "CP1: done;",
        "CP1: fail error; done",
        "CP1: reject forbidden",
        "CP1: sleep nan",
    ],
)
def test_invalid_playbook_has_no_side_effects(fixture, text):
    _, state = fixture
    marker = state / "checkpoints/CP_1.txt"
    marker.touch()
    (state / "worker.playbook").write_text(text)
    with pytest.raises(NoopError):
        execute_noop_step("worker", 1, state_dir=state)
    assert marker.exists()


def test_reinstallation_preserves_playbooks_and_reset_is_explicit(fixture):
    plan, state = fixture
    book = state / "worker.playbook"
    book.write_text("CP1: fail custom\n")
    marker = state / "checkpoints/CP_1.txt"
    marker.touch()
    with pytest.raises(NoopError, match="exists"):
        install_noop_plan(plan, state_dir=state, reset=True)
    assert book.read_text() == "CP1: fail custom\n"
    install_noop_plan(plan, state_dir=state, force=True)
    assert marker.exists() and book.read_text() == "CP1: fail custom\n"
    keep = state / "checkpoints/keep.txt"
    keep.touch()
    install_noop_plan(plan, state_dir=state, force=True, reset=True)
    assert not marker.exists() and keep.exists()
    assert execute_noop_step("worker", 1, state_dir=state)[0] == 0


def test_symlink_marker_cannot_touch_external_file(fixture, tmp_path):
    _, state = fixture
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (state / "checkpoints/CP_1.txt").symlink_to(victim)
    with pytest.raises(NoopError, match="symlink"):
        execute_noop_step("worker", 1, state_dir=state)
    assert victim.read_text() == "keep"


def test_cli_failure_codes_and_missing_state(tmp_path, fixture, capsys):
    _, state = fixture
    (state / "reviewer.playbook").write_text("CP1: reject NOPE\n")
    assert main(["noop-step", "reviewer", "1", "--state-dir", str(state)]) == 2
    assert "AFLOW_NOOP_REJECT CP1: NOPE" in capsys.readouterr().out
    assert (
        main(["noop-step", "worker", "1", "--state-dir", str(tmp_path / "missing")])
        == 1
    )
    error = capsys.readouterr().err
    assert "AFLOW_STOP" in error
    assert "AFLOW_NOOP_MOCK_FAILURE" not in error


def test_recoverable_worker_failure_is_distinct_from_real_errors(fixture):
    _, state = fixture
    (state / "worker.playbook").write_text("CP1: incomplete MOCK_CAPABILITY_FAILURE\n")
    assert execute_noop_step("worker", 1, state_dir=state) == (
        3,
        "AFLOW_NOOP_INCOMPLETE CP1: MOCK_CAPABILITY_FAILURE",
    )
    assert not (state / "checkpoints/CP_1.txt").exists()
    (state / "worker.playbook").unlink()
    with pytest.raises(NoopError, match="fixture missing"):
        execute_noop_step("worker", 1, state_dir=state)


@pytest.mark.parametrize("count", [0, -1, 101])
def test_invalid_checkpoint_count_creates_nothing(tmp_path, count):
    with pytest.raises(NoopError):
        install_noop_plan(
            tmp_path / "plan.md", state_dir=tmp_path / "state", checkpoints=count
        )
    assert list(tmp_path.iterdir()) == []
