from pathlib import Path

import pytest

from aflow.hotplug import (
    HOTPLUG_STAGES,
    HarnessSessionRefV1,
    HotplugTransactionV1,
    bounded_hotplug_history,
    hotplug_artifact_dir,
    hotplug_transaction_id,
    safe_hotplug_artifact_path,
    write_hotplug_artifact,
)
from aflow.plan import PlanSnapshot
from aflow.run_state import ControllerState, hotplug_resume_fields, hotplug_state_payload, load_override_request
from aflow.config import HarnessProfileConfig, TeamConfig, WorkflowHarnessConfig, WorkflowUserConfig
from aflow.run_state import PendingTeamOverride
from aflow.workflow import resolve_role_selector


def make_transaction(stage: str = "accepted") -> HotplugTransactionV1:
    digest = "a" * 64
    return HotplugTransactionV1(
        transaction_id=hotplug_transaction_id("run-1", digest, 1),
        run_id="run-1", accepted_override_digest=digest, transaction_number=1,
        source_role="worker", target_role="worker",
        source_selector="reasonix.flash", target_selector="codex.high",
        source_harness="reasonix", target_harness="codex",
        source_profile="flash", target_profile="high",
        source_model_display="reasonix / flash", target_model_display="codex / high",
        stage=stage,
    )


def test_every_transaction_stage_round_trips() -> None:
    for stage in HOTPLUG_STAGES:
        transaction = make_transaction(stage)
        assert HotplugTransactionV1.from_dict(transaction.to_dict()) == transaction


def test_session_reference_round_trips() -> None:
    session = HarnessSessionRefV1(
        session_id="session-1", role="worker", selector="codex.high",
        harness="codex", profile="high", model_display="codex / high",
    )
    assert HarnessSessionRefV1.from_dict(session.to_dict()) == session


def test_transaction_identity_is_digest_and_number_bound() -> None:
    first = make_transaction()
    second = HotplugTransactionV1(
        **{**first.to_dict(), "transaction_id": hotplug_transaction_id("run-1", "b" * 64, 2),
           "accepted_override_digest": "b" * 64, "transaction_number": 2}
    )
    assert first.transaction_id != second.transaction_id


def test_artifact_is_atomic_and_hash_bound(tmp_path: Path) -> None:
    relative, digest = write_hotplug_artifact(
        tmp_path, "hotplugs/hotplug-001/transaction.json", {"stage": "accepted"}
    )
    assert relative == "hotplugs/hotplug-001/transaction.json"
    assert len(digest) == 64
    assert hotplug_artifact_dir(tmp_path, 1).is_dir()
    assert (tmp_path / relative).read_text(encoding="utf-8") == '{\n  "stage": "accepted"\n}\n'
    with pytest.raises(FileExistsError):
        write_hotplug_artifact(tmp_path, relative, "duplicate")


@pytest.mark.parametrize("relative", ["/tmp/outside", "../outside", "a/../../outside"])
def test_artifact_paths_reject_escape(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ValueError):
        safe_hotplug_artifact_path(tmp_path, relative)


def test_history_is_bounded() -> None:
    assert len(bounded_hotplug_history([make_transaction() for _ in range(20)])) == 16


def test_hotplug_state_round_trips_and_malformed_state_fails_closed() -> None:
    transaction = make_transaction()
    state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
    state.role_selectors = {"worker": "codex.high"}
    state.current_hotplug_transaction = transaction
    state.pending_hotplug_transaction = transaction
    state.hotplug_transaction_number = 1
    state.hotplug_history = [transaction]
    restored = hotplug_resume_fields(hotplug_state_payload(state))
    assert restored["role_selectors"] == {"worker": "codex.high"}
    assert restored["current_hotplug_transaction"] == transaction
    assert restored["hotplug_history"] == (transaction,)
    with pytest.raises(ValueError, match="schema version"):
        hotplug_resume_fields({"hotplug_schema_version": 99})
    with pytest.raises(ValueError, match="transaction number"):
        hotplug_resume_fields({
            **hotplug_state_payload(state), "hotplug_transaction_number": 0,
        })


def test_pending_waiting_for_recovery_round_trips_as_nonterminal_state() -> None:
    transaction = make_transaction("waiting_for_hotplug_recovery")
    state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
    state.pending_hotplug_transaction = transaction
    state.hotplug_transaction_number = 1
    restored = hotplug_resume_fields(hotplug_state_payload(state))
    assert restored["pending_hotplug_transaction"] == transaction
    assert restored["pending_hotplug_transaction"].stage == "waiting_for_hotplug_recovery"


@pytest.mark.parametrize("field", [
    "transaction_id", "run_id", "accepted_override_digest", "source_role",
    "source_selector", "source_harness", "source_profile", "source_model_display",
])
def test_transaction_reader_rejects_non_string_authority(field: str) -> None:
    payload = make_transaction().to_dict()
    payload[field] = 7
    with pytest.raises(ValueError):
        HotplugTransactionV1.from_dict(payload)


def test_session_reader_rejects_non_string_authority() -> None:
    payload = HarnessSessionRefV1(
        session_id="session-1", role="worker", selector="codex.high",
        harness="codex", profile="high", model_display="codex / high",
    ).to_dict()
    payload["session_id"] = {"bad": True}
    with pytest.raises(ValueError):
        HarnessSessionRefV1.from_dict(payload)


@pytest.mark.parametrize("field", ["schema_version", "status"])
def test_modern_session_reader_requires_strict_discriminators(field: str) -> None:
    payload = HarnessSessionRefV1(
        session_id="session-1", role="worker", selector="codex.high",
        harness="codex", profile="high", model_display="codex / high",
    ).to_dict()
    payload.pop(field)
    with pytest.raises(ValueError):
        HarnessSessionRefV1.from_dict(payload, strict=True)
    payload[field] = True
    with pytest.raises(ValueError):
        HarnessSessionRefV1.from_dict(payload, strict=True)


@pytest.mark.parametrize("field", ["schema_version", "stage"])
def test_modern_transaction_reader_requires_strict_discriminators(field: str) -> None:
    payload = make_transaction().to_dict()
    payload.pop(field)
    with pytest.raises(ValueError):
        HotplugTransactionV1.from_dict(payload, strict=True)
    payload[field] = True
    with pytest.raises(ValueError):
        HotplugTransactionV1.from_dict(payload, strict=True)


@pytest.mark.parametrize("created_at", [
    "not-a-timestamp", "2026-08-09T00:00:00", "2026-08-09T00:00:00Z\ninvalid",
    "x" * 129,
])
def test_modern_transaction_reader_rejects_invalid_timestamps(created_at: str) -> None:
    payload = make_transaction().to_dict()
    payload["created_at"] = created_at
    with pytest.raises(ValueError):
        HotplugTransactionV1.from_dict(payload, strict=True)


def test_override_roles_are_strict_and_frozen_selector_bound(tmp_path: Path) -> None:
    path = tmp_path / "overrides.toml"
    path.write_text('[roles]\nworker = "codex.high"\n', encoding="utf-8")
    loaded = load_override_request(
        path,
        allowed_roles={"worker"},
        configured_selectors={"codex.high"},
    )
    assert loaded.status == "valid"
    assert loaded.request is not None
    assert loaded.request.role_selectors == {"worker": "codex.high"}


def test_override_roles_reject_unknown_role_or_selector(tmp_path: Path) -> None:
    path = tmp_path / "overrides.toml"
    path.write_text('[roles]\nreviewer = "codex.high"\n', encoding="utf-8")
    loaded = load_override_request(path, allowed_roles={"worker"})
    assert loaded.status == "invalid"
    path.write_text('[roles]\nworker = "codex.unknown"\n', encoding="utf-8")
    loaded = load_override_request(
        path, allowed_roles={"worker"}, configured_selectors={"codex.high"}
    )
    assert loaded.status == "invalid"


def test_selector_precedence_is_manager_then_run_local_then_team_then_global() -> None:
    config = WorkflowUserConfig(
        roles={"worker": "codex.low"},
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={"low": HarnessProfileConfig(model="low")}
            ),
            "reasonix": WorkflowHarnessConfig(
                profiles={"flash": HarnessProfileConfig(model="flash")}
            ),
        },
        teams={"team": TeamConfig(roles={"worker": "reasonix.flash"})},
    )
    pending = PendingTeamOverride(
        target_step="implement", role="worker", source_team="team",
        target_team="team", selector="codex.low", checkpoint_identity=None,
        decision_number=1,
    )
    assert resolve_role_selector("worker", "team", config, step_name="implement", pending_team_override=pending) == "codex.low"
    assert resolve_role_selector("worker", "team", config, step_name="other", pending_team_override=pending, run_local_role_selectors={"worker": "reasonix.flash"}) == "reasonix.flash"
    assert resolve_role_selector("worker", "team", config) == "reasonix.flash"
    assert resolve_role_selector("worker", None, config) == "codex.low"


@pytest.mark.parametrize("source", ["notes = ['keep']\n[roles]\n", "team = 'team'\n[roles]\n"])
def test_explicit_empty_roles_are_rejected_in_mixed_requests(tmp_path: Path, source: str) -> None:
    path = tmp_path / "overrides.toml"
    path.write_text(source, encoding="utf-8")
    assert load_override_request(path).status == "invalid"
