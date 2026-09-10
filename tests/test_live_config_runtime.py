"""Synthetic runtime coverage for current-source configuration boundaries."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aflow.config import load_workflow_config
from aflow.hotplug import HANDOVER_HEADINGS
from aflow.harnesses.base import HarnessInvocation
from aflow.harnesses.codex import CodexAdapter
from aflow.harnesses.session import SessionCapabilities, SessionResult
from aflow.run_state import ControllerConfig, resolve_resume_override
from aflow.workflow import WorkflowError, run_workflow
from tests._support import _BROKEN_PLAN, _COMPLETE_PLAN, _VALID_PLAN, _write_plan, _write_split_config


class RecordingAdapter:
    name = "codex"
    supports_effort = False
    manager_workspace_read = False

    def __init__(self) -> None:
        self.invocations: list[dict[str, object]] = []

    def build_invocation(
        self,
        *,
        repo_root: Path,
        model: str | None,
        system_prompt: str,
        user_prompt: str,
        effort: str | None = None,
    ) -> HarnessInvocation:
        del repo_root, effort
        self.invocations.append(
            {
                "model": model,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
            }
        )
        return HarnessInvocation(
            label="synthetic-codex",
            argv=("synthetic-codex",),
            env={},
            prompt_mode="synthetic",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            effective_prompt=f"{system_prompt}\n\n{user_prompt}",
        )


class RecordingSessionDriver:
    capabilities = SessionCapabilities(
        session_identity=True,
        followup_turn=True,
        resume_with_model=True,
    )

    def __init__(self) -> None:
        self.requests = []

    def build_invocation(self, request):
        self.requests.append(request)
        return HarnessInvocation(
            label="synthetic-session",
            argv=("synthetic-session", str(len(self.requests))),
            env={},
            prompt_mode="synthetic-session",
            system_prompt=request.system_prompt,
            user_prompt=request.user_prompt,
            effective_prompt=f"{request.system_prompt}\n\n{request.user_prompt}",
        )

    def parse_result(self, request, stdout, *, returncode=0):
        del stdout, returncode
        return SessionResult(
            session_id=request.session_id or "session-1",
            selector=request.selector,
            model=request.model,
            effort=request.effort,
            final_output="DONE" if len(self.requests) > 1 else "continue",
            capabilities=self.capabilities,
        )


def _live_config(
    *,
    max_turns: int = 3,
    team: str = "base",
    model: str = "model-base",
    effort: str | None = None,
    prompt: str = "old prompt {ACTIVE_PLAN_PATH}",
    future_prompt: str | None = None,
    retry_limit: int = 0,
    include_new_profile: bool = False,
) -> str:
    if future_prompt is None:
        future_prompt = prompt
    new_profile = (
        '\n[harness.codex.profiles.new]\nmodel = "model-new"\n'
        if include_new_profile
        else ""
    )
    effort_line = f'effort = "{effort}"\n' if effort is not None else ""
    return f'''\
[aflow]
default_workflow = "live"
max_turns = {max_turns}
retry_inconsistent_checkpoint_state = {retry_limit}

[harness.codex.profiles.base]
model = "{model}"
{effort_line}{new_profile}
[harness.codex.profiles.updated]
model = "model-updated"

[roles]
worker = "codex.base"

[teams.base]
worker = "codex.base"

[teams.updated]
worker = "codex.updated"

[prompts]
p = "{prompt}"
future = "{future_prompt}"
'''


def _live_workflows(
    *,
    team: str = "base",
    include_added_step: bool = False,
    remove_future: bool = False,
) -> str:
    added = (
        '\n[workflow.live.steps.added]\n'
        'role = "worker"\n'
        'prompts = ["p"]\n'
        'go = [{ to = "END", when = "DONE" }]\n'
        if include_added_step
        else ""
    )
    future = "" if remove_future else f'''\

[workflow.live.steps.future]
role = "worker"
prompts = ["future"]
go = [{{ to = "END", when = "DONE" }}, {{ to = "future" }}]
'''
    next_target = "END" if remove_future else "future"
    return f'''\
[workflow.live]
team = "{team}"

[workflow.live.steps.work]
role = "worker"
prompts = ["p"]
go = [{{ to = "END", when = "DONE" }}, {{ to = "{next_target}" }}]
{future}
{added}
'''


def _managed_live_config(
    *,
    manager_model: str = "manager-old",
    full_after_stalled_turns: int = 99,
) -> str:
    return f'''\
[aflow]
default_workflow = "live"
max_turns = 3

[harness.codex.profiles.worker]
model = "worker"

[harness.codex.profiles.manager_lite]
model = "{manager_model}"

[harness.codex.profiles.manager_full]
model = "manager-full"

[roles]
worker = "codex.worker"
manager_lite = "codex.manager_lite"
manager_full = "codex.manager_full"

[teams.base]
worker = "codex.worker"

[manager]
lite_role = "manager_lite"
full_role = "manager_full"
full_after_stalled_turns = {full_after_stalled_turns}

[prompts]
p = "Work from {{ACTIVE_PLAN_PATH}}."
'''


_MANAGED_LIVE_WORKFLOWS = '''\
[workflow.live]
team = "base"
manager_enabled = true

[workflow.live.steps.work]
role = "worker"
prompts = ["p"]
go = [{ to = "END", when = "DONE" }, { to = "work" }]
'''


def _cross_harness_live_config(*, target: bool, retry_limit: int = 0) -> str:
    if target:
        harnesses = (
            '[harness.reasonix.profiles.new]\n'
            'model = "reasonix-new"\n'
            'effort = "high"\n'
        )
        selector = "reasonix.new"
    else:
        harnesses = '[harness.codex.profiles.base]\nmodel = "codex-base"\n'
        selector = "codex.base"
    return f'''\
[aflow]
default_workflow = "live"
max_turns = 2
retry_inconsistent_checkpoint_state = {retry_limit}

{harnesses}
[roles]
worker = "{selector}"

[teams.base]
worker = "{selector}"

[prompts]
p = "Live worker prompt {{ACTIVE_PLAN_PATH}}."
future = "Live follow-up prompt {{ACTIVE_PLAN_PATH}}."
'''


def _live_handover() -> str:
    return "\n".join(
        f"## {heading}\n- bounded operational evidence"
        for heading in HANDOVER_HEADINGS
    )


def _make_live_source(
    tmp_path: Path,
    *,
    max_turns: int = 3,
    team: str = "base",
    model: str = "model-base",
    prompt: str = "old prompt {ACTIVE_PLAN_PATH}",
    retry_limit: int = 0,
    workflows: str | None = None,
    include_new_profile: bool = False,
) -> tuple[Path, Path]:
    config_path, workflows_path = _write_split_config(
        tmp_path / "config",
        _live_config(
            max_turns=max_turns,
            team=team,
            model=model,
            prompt=prompt,
            retry_limit=retry_limit,
            include_new_profile=include_new_profile,
        ),
        workflows or _live_workflows(team=team),
    )
    return config_path, workflows_path


def _run_config(
    config_path: Path,
    plan_path: Path,
    *,
    max_turns: int,
    team: str | None = None,
    team_explicit: bool = False,
    max_turns_explicit: bool = False,
) -> ControllerConfig:
    repo_root = config_path.parents[3]
    return ControllerConfig(
        repo_root=repo_root,
        plan_path=plan_path,
        max_turns=max_turns,
        team=team,
        team_explicit=team_explicit,
        max_turns_explicit=max_turns_explicit,
        start_step_explicit=False,
    )


def _run_live(
    config_path: Path,
    plan_path: Path,
    adapter: RecordingAdapter,
    runner,
    *,
    max_turns: int,
    team: str | None = None,
    team_explicit: bool = False,
    max_turns_explicit: bool = False,
    session_driver=None,
):
    return run_workflow(
        _run_config(
            config_path,
            plan_path,
            max_turns=max_turns,
            team=team,
            team_explicit=team_explicit,
            max_turns_explicit=max_turns_explicit,
        ),
        load_workflow_config(config_path),
        "live",
        config_dir=config_path,
        snapshot_config=False,
        adapter=adapter,
        runner=runner,
        session_driver=session_driver,
    )


def test_live_defaults_and_turn_inputs_refresh_at_the_loop_edge(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, _ = _make_live_source(tmp_path, max_turns=1)
    adapter = RecordingAdapter()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            config_path.write_text(
                _live_config(
                    max_turns=2,
                    team="updated",
                    model="model-live",
                    prompt="new prompt {ACTIVE_PLAN_PATH}",
                ),
                encoding="utf-8",
            )
        else:
            _write_plan(plan_path, _COMPLETE_PLAN)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    result = _run_live(
        config_path,
        plan_path,
        adapter,
        runner,
        max_turns=1,
    )

    assert result.turns_completed == 2
    assert [item["model"] for item in adapter.invocations] == [
        "model-base",
        "model-live",
    ]
    assert "old prompt" in str(adapter.invocations[0]["user_prompt"])
    assert "new prompt" in str(adapter.invocations[1]["user_prompt"])


def test_live_profile_edit_updates_model_effort_prompt_and_reuses_session(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, _ = _make_live_source(tmp_path, max_turns=2)
    adapter = RecordingAdapter()
    driver = RecordingSessionDriver()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            config_path.write_text(
                _live_config(
                    max_turns=2,
                    model="model-edited",
                    effort="high",
                    prompt="edited prompt {ACTIVE_PLAN_PATH}",
                ),
                encoding="utf-8",
            )
        else:
            _write_plan(plan_path, _COMPLETE_PLAN)
        return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

    result = _run_live(
        config_path,
        plan_path,
        adapter,
        runner,
        max_turns=2,
        session_driver=driver,
    )

    assert result.final_snapshot.is_complete
    assert len(driver.requests) == 2
    assert driver.requests[0].session_id is None
    assert driver.requests[1].session_id == "session-1"
    assert driver.requests[1].model == "model-edited"
    assert driver.requests[1].effort == "high"
    assert "edited prompt" in driver.requests[1].user_prompt
    assert "old prompt" not in driver.requests[1].user_prompt


@pytest.mark.parametrize("retry", [False, True], ids=("normal", "pending-retry"))
def test_live_mapping_change_hands_over_from_captured_source_session(
    tmp_path: Path,
    retry: bool,
) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, _ = _write_split_config(
        tmp_path / "config",
        _cross_harness_live_config(target=False, retry_limit=int(retry)),
        _live_workflows(),
    )

    class SourceDriver:
        capabilities = SessionCapabilities(
            session_identity=True,
            followup_turn=True,
            read_only_teardown=True,
        )

        def __init__(self) -> None:
            self.handovers = 0

        def handover(self, request, prompt):
            del request, prompt
            self.handovers += 1
            return _live_handover()

    class TargetDriver:
        capabilities = SessionCapabilities(
            session_identity=True,
            followup_turn=True,
            resume_with_model=True,
            idempotent_turn_start=True,
        )

        def __init__(self) -> None:
            self.requests = []
            self.turns = 0

        def build_invocation(self, request):
            self.requests.append(request)
            return HarnessInvocation(
                label="synthetic-target",
                argv=("synthetic-target", str(len(self.requests))),
                env={},
                prompt_mode="synthetic-session",
                system_prompt=request.system_prompt,
                user_prompt=request.user_prompt,
                effective_prompt=request.user_prompt,
            )

        def parse_result(self, request, stdout, *, returncode=0):
            del stdout, returncode
            self.turns += 1
            return SessionResult(
                session_id=f"target-{self.turns}",
                selector=request.selector,
                model=request.model,
                effort=request.effort,
                final_output="continue" if self.turns == 1 else "DONE",
                idempotency_key=request.idempotency_key,
                capabilities=self.capabilities,
            )

    source_driver = SourceDriver()
    target_driver = TargetDriver()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            config_path.write_text(
                _cross_harness_live_config(target=True, retry_limit=int(retry)),
                encoding="utf-8",
            )
            if retry:
                _write_plan(plan_path, _BROKEN_PLAN)
        else:
            _write_plan(plan_path, _COMPLETE_PLAN)
        return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

    result = run_workflow(
        _run_config(config_path, plan_path, max_turns=2),
        load_workflow_config(config_path),
        "live",
        config_dir=config_path,
        snapshot_config=False,
        adapter=CodexAdapter(),
        runner=runner,
        session_driver=target_driver,
        source_session_driver=source_driver,
    )

    assert result.final_snapshot.is_complete
    assert calls == 2
    assert source_driver.handovers == 1
    assert len(target_driver.requests) == 3  # initial turn, target preflight, target turn
    assert target_driver.requests[0].selector == "codex.base"
    assert target_driver.requests[1].selector == "reasonix.new"
    assert target_driver.requests[2].selector == "reasonix.new"
    assert target_driver.requests[2].model == "reasonix-new"
    assert target_driver.requests[2].effort == "high"
    assert "Source worker handover:" in target_driver.requests[2].user_prompt
    payload = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert len(payload["hotplug_history"]) == 1
    transaction = payload["hotplug_history"][0]
    assert transaction["stage"] == "applied"
    assert transaction["source_selector"] == "codex.base"
    assert transaction["source_harness"] == "codex"
    assert transaction["target_selector"] == "reasonix.new"
    assert transaction["target_harness"] == "reasonix"
    assert transaction["target_model_display"] == "reasonix / reasonix-new / high"


def test_live_manager_profile_refresh_preserves_decision_history(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, _ = _write_split_config(
        tmp_path / "config",
        _managed_live_config(),
        _MANAGED_LIVE_WORKFLOWS,
    )
    manager_models: list[str] = []
    worker_calls = 0

    manager_decision = json.dumps({
        "schema_version": 1,
        "action": "continue",
        "reason": "Synthetic manager continuation.",
        "next_step_notes": [],
        "stop_report": None,
    })

    def runner(argv, **kwargs):
        nonlocal worker_calls
        model = argv[argv.index("--model") + 1]
        if model.startswith("manager"):
            manager_models.append(model)
            if len(manager_models) == 1:
                config_path.write_text(
                    _managed_live_config(manager_model="manager-new"),
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(argv, 0, manager_decision, "")

        worker_calls += 1
        if worker_calls == 2:
            _write_plan(plan_path, _COMPLETE_PLAN)
        return subprocess.CompletedProcess(argv, 0, "worker output", "")

    result = run_workflow(
        _run_config(config_path, plan_path, max_turns=3),
        load_workflow_config(config_path),
        "live",
        config_dir=config_path,
        snapshot_config=False,
        adapter=CodexAdapter(),
        runner=runner,
    )

    assert result.final_snapshot.is_complete
    assert manager_models == ["manager-old", "manager-new"]
    payload = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert payload["manager_decision_number"] == 2
    assert [item["decision_number"] for item in payload["manager_history"]] == [1, 2]


def test_partial_overrides_retain_choices_and_consume_notes_once(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, _ = _make_live_source(tmp_path, max_turns=4)
    adapter = RecordingAdapter()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        run_dir = max(
            (config_path.parents[3] / ".aflow" / "runs").iterdir(),
            key=lambda path: path.stat().st_mtime_ns,
        )
        if calls == 1:
            (run_dir / "overrides.toml").write_text(
                'next_step = "work"\n'
                'team = "updated"\n'
                'notes = ["apply once"]\n',
                encoding="utf-8",
            )
        elif calls == 2:
            (run_dir / "overrides.toml").write_text(
                "max_turns = 4\n",
                encoding="utf-8",
            )
        elif calls == 4:
            _write_plan(plan_path, _COMPLETE_PLAN)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    result = _run_live(config_path, plan_path, adapter, runner, max_turns=4)

    assert result.turns_completed == 4
    assert [item["model"] for item in adapter.invocations] == [
        "model-base",
        "model-updated",
        "model-updated",
        "model-updated",
    ]
    prompts = [str(item["user_prompt"]) for item in adapter.invocations]
    assert "apply once" in prompts[1]
    assert "apply once" not in prompts[2]
    assert "apply once" not in prompts[3]

    payload = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    current = payload["override_result"]
    accepted = payload["last_accepted_override"]
    assert current["team"] is None
    assert current["max_turns"] == 4
    assert current["next_step"] is None
    assert current["has_notes"] is False
    assert accepted["team"] == "updated"
    assert accepted["max_turns"] == 4
    assert accepted["next_step"] is None
    assert accepted["has_notes"] is False


def test_team_only_override_keeps_accepted_limit_across_reload_and_resume(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, _ = _make_live_source(tmp_path, max_turns=5)
    adapter = RecordingAdapter()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        run_dir = max(
            (config_path.parents[3] / ".aflow" / "runs").iterdir(),
            key=lambda path: path.stat().st_mtime_ns,
        )
        if calls == 1:
            (run_dir / "overrides.toml").write_text(
                "max_turns = 4\n",
                encoding="utf-8",
            )
        elif calls == 2:
            config_path.write_text(
                _live_config(max_turns=6),
                encoding="utf-8",
            )
            (run_dir / "overrides.toml").write_text(
                'team = "updated"\n',
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    result = _run_live(config_path, plan_path, adapter, runner, max_turns=5)

    assert calls == 4
    assert result.status == "completed"
    assert result.end_reason == "max_turns_reached"
    assert result.turns_completed == 4
    assert not result.final_snapshot.is_complete
    assert plan_path.read_text(encoding="utf-8") == _VALID_PLAN
    assert [item["model"] for item in adapter.invocations] == [
        "model-base",
        "model-base",
        "model-updated",
        "model-updated",
    ]
    payload = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert payload["last_accepted_override"]["max_turns"] == 4
    assert payload["last_accepted_override"]["team"] == "updated"

    resolution = resolve_resume_override(
        result.run_dir,
        payload["override_result"],
        persisted_accepted_result=payload["last_accepted_override"],
    )
    assert resolution.source_run_dir is None
    assert resolution.last_accepted_override is not None
    assert resolution.last_accepted_override.max_turns == 4
    assert resolution.last_accepted_override.team == "updated"
    assert resolution.last_accepted_override.next_step is None
    assert resolution.last_accepted_override.has_notes is False

    persisted_only = resolve_resume_override(
        result.run_dir,
        None,
        persisted_accepted_result=payload["last_accepted_override"],
    )
    assert persisted_only.last_accepted_override is not None
    assert persisted_only.last_accepted_override.max_turns == 4
    assert persisted_only.last_accepted_override.team == "updated"
    assert persisted_only.last_accepted_override.next_step is None


@pytest.mark.parametrize(
    ("source_max_turns", "run_max_turns", "max_turns_explicit"),
    [(1, 1, False), (2, 1, True)],
)
def test_live_max_condition_uses_end_edge_before_normal_limit_termination(
    tmp_path: Path,
    source_max_turns: int,
    run_max_turns: int,
    max_turns_explicit: bool,
) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    workflows = '''\
[workflow.live]
team = "base"

[workflow.live.steps.work]
role = "worker"
prompts = ["p"]
go = [{ to = "END", when = "DONE || MAX_TURNS_REACHED" }, { to = "work" }]
'''
    config_path, _ = _make_live_source(
        tmp_path,
        max_turns=source_max_turns,
        workflows=workflows,
    )
    adapter = RecordingAdapter()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    result = _run_live(
        config_path,
        plan_path,
        adapter,
        runner,
        max_turns=run_max_turns,
        max_turns_explicit=max_turns_explicit,
    )

    assert calls == 1
    assert result.status == "completed"
    assert result.end_reason == "max_turns_reached"
    assert result.turns_completed == 1
    assert not result.final_snapshot.is_complete
    assert plan_path.read_text(encoding="utf-8") == _VALID_PLAN
    turn_payload = json.loads(
        (result.run_dir / "turns" / "turn-001" / "result.json").read_text(
            encoding="utf-8"
        )
    )
    assert turn_payload["status"] == "completed"
    assert turn_payload["conditions"] == {
        "DONE": False,
        "NEW_PLAN_EXISTS": False,
        "MAX_TURNS_REACHED": True,
    }
    assert turn_payload["chosen_transition"] == "END"
    assert turn_payload["chosen_transition_condition"] == "DONE || MAX_TURNS_REACHED"
    run_payload = json.loads(
        (result.run_dir / "run.json").read_text(encoding="utf-8")
    )
    assert run_payload["status"] == "completed"
    assert run_payload["end_reason"] == "max_turns_reached"
    assert run_payload["turns_completed"] == 1


@pytest.mark.parametrize(
    ("end_transition", "condition"),
    [
        ('{ to = "END" }', None),
        ('{ to = "END", when = "!DONE" }', "!DONE"),
    ],
    ids=["unconditional", "independently-true-condition"],
)
def test_live_incomplete_independent_end_at_cap_fails(
    tmp_path: Path,
    end_transition: str,
    condition: str | None,
) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    workflows = f'''\
[workflow.live]
team = "base"

[workflow.live.steps.work]
role = "worker"
prompts = ["p"]
go = [{end_transition}]
'''
    config_path, _ = _make_live_source(
        tmp_path,
        max_turns=1,
        workflows=workflows,
    )
    adapter = RecordingAdapter()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    with pytest.raises(WorkflowError, match="reached max turns limit of 1") as exc:
        _run_live(config_path, plan_path, adapter, runner, max_turns=1)

    assert calls == 1
    run_payload = json.loads(
        (exc.value.run_dir / "run.json").read_text(encoding="utf-8")
    )
    assert run_payload["status"] == "failed"
    assert "end_reason" not in run_payload
    assert "reached max turns limit of 1" in run_payload["failure_reason"]
    turn_payload = json.loads(
        (exc.value.run_dir / "turns" / "turn-001" / "result.json").read_text(
            encoding="utf-8"
        )
    )
    assert turn_payload["chosen_transition"] == "END"
    if condition is None:
        assert "chosen_transition_condition" not in turn_payload
    else:
        assert turn_payload["chosen_transition_condition"] == condition
    assert "end_reason" not in turn_payload


def test_live_limit_decrease_stops_before_next_provider_launch(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, _ = _make_live_source(tmp_path, max_turns=3)
    adapter = RecordingAdapter()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        config_path.write_text(_live_config(max_turns=1), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    result = _run_live(config_path, plan_path, adapter, runner, max_turns=3)

    assert calls == 1
    assert result.status == "completed"
    assert result.end_reason == "max_turns_reached"
    assert not result.final_snapshot.is_complete
    assert plan_path.read_text(encoding="utf-8") == _VALID_PLAN
    payload = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["end_reason"] == "max_turns_reached"
    assert payload["turns_completed"] == 1


@pytest.mark.parametrize("explicit", [False, True])
def test_live_default_team_changes_but_explicit_team_stays_selected(
    tmp_path: Path, explicit: bool
) -> None:
    case_dir = tmp_path / ("explicit" if explicit else "default")
    case_dir.mkdir()
    plan_path = case_dir / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, workflows_path = _make_live_source(case_dir, max_turns=2)
    adapter = RecordingAdapter()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            workflows_path.write_text(
                _live_workflows(team="updated"),
                encoding="utf-8",
            )
        else:
            _write_plan(plan_path, _COMPLETE_PLAN)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    _run_live(
        config_path,
        plan_path,
        adapter,
        runner,
        max_turns=2,
        team="base" if explicit else None,
        team_explicit=explicit,
    )

    assert adapter.invocations[0]["model"] == "model-base"
    assert adapter.invocations[1]["model"] == (
        "model-base" if explicit else "model-updated"
    )


def test_live_graph_addition_and_future_step_definition_are_used_next_turn(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, workflows_path = _make_live_source(tmp_path, max_turns=2)
    adapter = RecordingAdapter()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            workflows_path.write_text(
                _live_workflows(
                    include_added_step=True,
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                _live_config(
                    future_prompt="future prompt changed {ACTIVE_PLAN_PATH}"
                ),
                encoding="utf-8",
            )
        else:
            _write_plan(plan_path, _COMPLETE_PLAN)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    _run_live(config_path, plan_path, adapter, runner, max_turns=2)

    assert "future prompt changed" in str(adapter.invocations[1]["user_prompt"])


def test_removed_current_target_fails_before_next_synthetic_launch(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, workflows_path = _make_live_source(tmp_path, max_turns=3)
    adapter = RecordingAdapter()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        workflows_path.write_text(
            _live_workflows(remove_future=True),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    with pytest.raises(WorkflowError, match="current step 'future'.*current workflow"):
        _run_live(config_path, plan_path, adapter, runner, max_turns=3)

    assert calls == 1


def test_valid_next_step_correction_precedes_deleted_target_failure(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, workflows_path = _make_live_source(tmp_path, max_turns=3)
    adapter = RecordingAdapter()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            workflows_path.write_text(
                _live_workflows(remove_future=True),
                encoding="utf-8",
            )
            run_dir = max(
                (config_path.parents[3] / ".aflow" / "runs").iterdir(),
                key=lambda path: path.stat().st_mtime_ns,
            )
            (run_dir / "overrides.toml").write_text(
                'next_step = "work"\n',
                encoding="utf-8",
            )
        else:
            _write_plan(plan_path, _COMPLETE_PLAN)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    result = _run_live(config_path, plan_path, adapter, runner, max_turns=3)

    assert result.final_snapshot.is_complete
    assert calls == 2


def test_new_profile_in_current_config_can_be_selected_by_boundary_override(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, _ = _make_live_source(tmp_path, max_turns=2)
    adapter = RecordingAdapter()
    session_driver = CodexAdapter().session_driver(
        exec_help="codex exec --json resume",
        resume_help="resume [SESSION_ID] -m, --model MODEL",
    )
    calls = 0
    invocations: list[tuple[str, ...]] = []

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        invocations.append(tuple(argv))
        if calls == 1:
            config_path.write_text(
                _live_config(max_turns=2, include_new_profile=True),
                encoding="utf-8",
            )
            run_dir = max(
                (config_path.parents[3] / ".aflow" / "runs").iterdir(),
                key=lambda path: path.stat().st_mtime_ns,
            )
            (run_dir / "overrides.toml").write_text(
                '[roles]\nworker = "codex.new"\n',
                encoding="utf-8",
            )
        else:
            _write_plan(plan_path, _COMPLETE_PLAN)
        session_id = "live-session"
        output = "continue" if calls == 1 else "DONE"
        return subprocess.CompletedProcess(
            argv,
            0,
            "{\"type\":\"thread.started\",\"thread_id\":\""
            f"{session_id}\"}}\n"
            "{\"type\":\"message.completed\",\"thread_id\":\""
            f"{session_id}\",\"text\":\"{output}\"}}\n",
            "",
        )

    _run_live(
        config_path,
        plan_path,
        adapter,
        runner,
        max_turns=2,
        session_driver=session_driver,
    )

    assert any("model-new" in argument for argument in invocations[1])


def test_effectively_unchanged_selector_alias_reuses_session_without_hotplug(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, _ = _make_live_source(tmp_path, max_turns=2)
    adapter = RecordingAdapter()
    driver = RecordingSessionDriver()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            config_path.write_text(
                _live_config(max_turns=2)
                + '\n[harness.codex.profiles.alias]\nmodel = "model-base"\n',
                encoding="utf-8",
            )
            run_dir = max(
                (config_path.parents[3] / ".aflow" / "runs").iterdir(),
                key=lambda path: path.stat().st_mtime_ns,
            )
            (run_dir / "overrides.toml").write_text(
                '[roles]\nworker = "codex.alias"\n',
                encoding="utf-8",
            )
        else:
            _write_plan(plan_path, _COMPLETE_PLAN)
        return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

    result = _run_live(
        config_path,
        plan_path,
        adapter,
        runner,
        max_turns=2,
        session_driver=driver,
    )

    assert result.final_snapshot.is_complete
    assert driver.requests[1].selector == "codex.alias"
    assert driver.requests[1].session_id == "session-1"
    payload = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert payload["hotplug_history"] == []


def test_invalid_new_override_is_nonfatal_and_preserves_last_accepted_choice(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, _ = _make_live_source(tmp_path, max_turns=4)
    adapter = RecordingAdapter()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        run_dir = max(
            (config_path.parents[3] / ".aflow" / "runs").iterdir(),
            key=lambda path: path.stat().st_mtime_ns,
        )
        if calls == 1:
            (run_dir / "overrides.toml").write_text(
                'team = "updated"\n',
                encoding="utf-8",
            )
        elif calls == 2:
            (run_dir / "overrides.toml").write_text(
                'team = "missing"\n',
                encoding="utf-8",
            )
        else:
            _write_plan(plan_path, _COMPLETE_PLAN)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    result = _run_live(config_path, plan_path, adapter, runner, max_turns=4)

    assert [item["model"] for item in adapter.invocations] == [
        "model-base",
        "model-updated",
        "model-updated",
    ]
    payload = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert payload["override_result"]["status"] == "rejected"
    assert payload["last_accepted_override"]["team"] == "updated"


def test_pending_retry_renders_current_prompt_and_profile_from_saved_context(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, _ = _make_live_source(
        tmp_path,
        max_turns=2,
        retry_limit=1,
        prompt="old retry prompt {ACTIVE_PLAN_PATH}",
    )
    adapter = RecordingAdapter()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            _write_plan(plan_path, _BROKEN_PLAN)
            config_path.write_text(
                _live_config(
                    max_turns=2,
                    retry_limit=1,
                    model="model-retry-live",
                    prompt="new retry prompt {ACTIVE_PLAN_PATH}",
                ),
                encoding="utf-8",
            )
        else:
            _write_plan(plan_path, _COMPLETE_PLAN)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    _run_live(config_path, plan_path, adapter, runner, max_turns=2)

    assert adapter.invocations[1]["model"] == "model-retry-live"
    assert "new retry prompt" in str(adapter.invocations[1]["user_prompt"])
    assert "old retry prompt" not in str(adapter.invocations[1]["user_prompt"])


def test_malformed_live_config_is_an_actionable_pre_turn_failure(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, _ = _make_live_source(tmp_path, max_turns=3)
    adapter = RecordingAdapter()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        config_path.write_text("[aflow\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    with pytest.raises(WorkflowError, match="current live workflow configuration is unusable: invalid TOML"):
        _run_live(config_path, plan_path, adapter, runner, max_turns=3)
    assert calls == 1


def test_owner_stop_precedes_malformed_live_config_reload(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path, _ = _make_live_source(tmp_path, max_turns=3)
    adapter = RecordingAdapter()
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        config_path.write_text("[aflow\n", encoding="utf-8")
        run_dir = max(
            (config_path.parents[3] / ".aflow" / "runs").iterdir(),
            key=lambda path: path.stat().st_mtime_ns,
        )
        (run_dir / "overrides.toml").write_text(
            "owner_stop = true\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    result = _run_live(config_path, plan_path, adapter, runner, max_turns=3)

    assert calls == 1
    assert result.status == "owner_stopped"
