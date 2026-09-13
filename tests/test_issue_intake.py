from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import re
from threading import Event
from typing import Mapping
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError

import pytest

import aflow.issue_intake_planner as planner_module
from aflow.issue_intake import (
    CanonicalIssue,
    EventEnvelope,
    FixedOriginHTTPClient,
    HTTPRequestFailure,
    IntakeConfig,
    IntakeOutcome,
    IssueIntakeRunner,
    AttentionFailure,
    load_config,
    main,
    source_hash,
    TransportFailure,
    _import_plan,
)
from aflow.issue_intake_planner import PlannerFailure, PlanningResult


REPOSITORY_ID = 123
FULL_NAME = "owner/repo"
ISSUE_NUMBER = 7
PROJECT_ID = "project"
WORKFLOW = "cumulative_delivery"
TEAM = "executor"
COMMIT_SHA = "a" * 40
PLAN_TEXT = "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] implement\n"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plan_body(plan: bytes = PLAN_TEXT.encode("utf-8")) -> str:
    return f"AFlow-Plan: plans/todo/source.md\nAFlow-Plan-SHA256: {_digest(plan)}\n"


def _event(*, body: str = _plan_body(), actor_id: int = 591691, action: str = "opened", delivery_id: str = "delivery-1", **extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "repository_id": REPOSITORY_ID,
        "repository_full_name": FULL_NAME,
        "issue_number": ISSUE_NUMBER,
        "actor_id": actor_id,
        "action": action,
        "delivery_id": delivery_id,
        "title_body_sha256": source_hash("Request", body),
    }
    value.update(extra)
    return value


def _config_text(tmp_path: Path, *, enabled: bool = True, **overrides: object) -> str:
    values: dict[str, object] = {
        "enabled": enabled,
        "state_root": str(tmp_path / "state"),
        "private_api_url": "http://private.example",
        "private_credential_file": str(tmp_path / "private.token"),
        "github_api_url": "https://api.github.example",
        "github_credential_file": str(tmp_path / "github.token"),
        "deferred_labels": ["deferred"],
        "workflow": WORKFLOW,
        "team": TEAM,
    }
    values.update(overrides)
    def toml_value(value: object) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, list):
            return "[" + ", ".join(json.dumps(item) for item in value) + "]"
        return json.dumps(value)

    lines = ["[issue_intake]"]
    for key, value in values.items():
        if key != "repositories":
            lines.append(f"{key} = {toml_value(value)}")
    lines.extend(
        [
            "",
            "[[issue_intake.repositories]]",
            f"repository_id = {REPOSITORY_ID}",
            f"full_name = {json.dumps(FULL_NAME)}",
            f"project_id = {json.dumps(PROJECT_ID)}",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_config(tmp_path: Path, *, enabled: bool = True, **overrides: object) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "intake.toml"
    path.write_text(_config_text(tmp_path, enabled=enabled, **overrides), encoding="utf-8")
    return path


def _loaded_config(tmp_path: Path, *, enabled: bool = True, **overrides: object) -> IntakeConfig:
    return load_config(_write_config(tmp_path, enabled=enabled, **overrides))


class FakeHTTP:
    def __init__(self, *, body: str = _plan_body(), plan: bytes = PLAN_TEXT.encode("utf-8")) -> None:
        self.title = "Request"
        self.body = body
        self.plan = plan
        self.calls: list[tuple[str, str, object | None]] = []
        self.model_calls = 0
        self.start_calls: list[tuple[str, Mapping[str, object]]] = []
        self.create_calls = 0
        self.promote_calls = 0
        self.lose_create = False
        self.lose_promote = False
        self.lose_start_count = 0
        self.issue_call_count = 0
        self.change_issue_at: int | None = None
        self.blob_payload: bytes | None = None
        self.changed_title = "Changed"
        self.deferred = False
        self.closed = False
        self.author_id = 591691
        self.pull_request = False
        self.plans: dict[tuple[str, str], bytes] = {
            ("todo", "source.md"): plan,
        }
        self.start_response: object = {
            "result": {
                "run_id": "run-1",
                "created": True,
                "status": "launch_requested",
            }
        }

    def _record(self, method: str, path: str, payload: object | None = None) -> None:
        self.calls.append((method, path, payload))

    def _plan_key(self, path: str) -> tuple[str, str]:
        parts = path.split("/")
        return parts[-2], parts[-1].split("?", 1)[0]

    def get_json(self, service: str, path: str) -> object:
        self._record("GET", f"{service}:{path}")
        if service == "github":
            if path == "/repos/owner/repo":
                return {"id": REPOSITORY_ID, "full_name": FULL_NAME}
            if path.startswith("/repos/owner/repo/issues/"):
                self.issue_call_count += 1
                body = self.body
                title = self.title
                if self.change_issue_at is not None and self.issue_call_count >= self.change_issue_at:
                    title = self.changed_title
                issue: dict[str, object] = {
                    "number": ISSUE_NUMBER,
                    "title": title,
                    "body": body,
                    "user": {"id": self.author_id},
                    "state": "closed" if self.closed else "open",
                    "labels": ([{"name": "deferred"}] if self.deferred else []),
                }
                if self.pull_request:
                    issue["pull_request"] = {}
                return issue
            raise HTTPRequestFailure("not_found", status_code=404)
        if service != "private":
            raise AssertionError(service)
        if path == "/api/control-plane/projects":
            return {"projects": [{"project_id": PROJECT_ID, "root": "/registered/project", "schema_version": 1}]}
        if path.endswith("/capabilities"):
            return {
                "schema_version": 1,
                "workflows": [WORKFLOW],
                "teams": [TEAM],
                "workflow_details": {
                    WORKFLOW: {
                        "declared_steps": ["implement"],
                        "executable_steps": ["implement"],
                        "excluded_steps": [],
                        "first_step": "implement",
                        "default_team": TEAM,
                    }
                },
                "service_features": ["daemon_lifecycle"],
            }
        if "/plans/" in path and not path.endswith("/promote"):
            key = self._plan_key(path)
            value = self.plans.get(key)
            if value is None:
                raise HTTPRequestFailure("not_found", status_code=404)
            status, name = key
            return {
                "project_id": PROJECT_ID,
                "name": name,
                "path": f"plans/{'in-progress' if status == 'in_progress' else status}/{name}",
                "status": status,
                "revision": _digest(value),
                "size_bytes": len(value),
                "content": value.decode("utf-8"),
            }
        raise AssertionError((service, path))

    def get_bytes(self, service: str, path: str) -> bytes:
        self._record("GET_BYTES", f"{service}:{path}")
        if service == "github" and self.blob_payload is not None:
            return self.blob_payload
        raise AssertionError("no external blob configured")

    def post_json(
        self,
        service: str,
        path: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> object:
        self._record(
            "POST",
            f"{service}:{path}",
            {"payload": dict(payload), "idempotency_key": idempotency_key},
        )
        if service != "private":
            raise AssertionError(service)
        if path == f"/api/projects/{PROJECT_ID}/plans":
            self.create_calls += 1
            name = payload["name"]
            assert isinstance(name, str)
            content = payload["content"]
            assert isinstance(content, str)
            self.plans[("todo", name)] = content.encode("utf-8")
            response = {
                "project_id": PROJECT_ID,
                "name": name,
                "path": f"plans/todo/{name}",
                "status": "todo",
                "revision": _digest(content.encode("utf-8")),
                "size_bytes": len(content.encode("utf-8")),
                "content": content,
            }
            if self.lose_create:
                self.lose_create = False
                raise HTTPRequestFailure("request_exhausted", uncertain=True)
            return response
        if path.startswith(f"/api/projects/{PROJECT_ID}/plans/todo/") and path.endswith("/promote"):
            self.promote_calls += 1
            name = path.split("/")[-2]
            value = self.plans.pop(("todo", name), None)
            if value is None:
                raise HTTPRequestFailure("conflict", status_code=409, uncertain=True)
            assert payload["expected_revision"] == _digest(value)
            self.plans[("in_progress", name)] = value
            response = {
                "project_id": PROJECT_ID,
                "name": name,
                "path": f"plans/in-progress/{name}",
                "status": "in_progress",
                "revision": _digest(value),
                "size_bytes": len(value),
                "content": value.decode("utf-8"),
            }
            if self.lose_promote:
                self.lose_promote = False
                raise HTTPRequestFailure("request_exhausted", uncertain=True)
            return response
        if path == f"/api/control-plane/projects/{PROJECT_ID}/runs/preflight":
            assert payload["dirty_worktree_confirmed"] is False
            assert payload["extra_instructions"] == []
            return {
                "checkout_path": "/registered/project/.worktrees/intake",
                "execution_mode": "new_worktree",
                "dirty": False,
                "requires_confirmation": False,
                "blockers": [],
                "total_items": 0,
                "offset": 0,
                "limit": 1,
            }
        if path == f"/api/control-plane/projects/{PROJECT_ID}/runs":
            assert idempotency_key is not None
            self.start_calls.append((idempotency_key, dict(payload)))
            if self.lose_start_count:
                self.lose_start_count -= 1
                raise HTTPRequestFailure("request_exhausted", uncertain=True)
            return self.start_response
        raise AssertionError((service, path, payload))


class _NoopPlanner:
    def plan(self, **_kwargs: object) -> object:
        raise PlannerFailure("planner_not_available")

    def recover(self, **_kwargs: object) -> object:
        raise PlannerFailure("planner_not_available")


class _FakePlanner:
    def __init__(
        self,
        result: PlanningResult | None = None,
    ) -> None:
        self.result = result or PlanningResult(
            status="plan",
            markdown=PLAN_TEXT,
            provenance={"kind": "fixture-planner"},
        )
        self.plan_calls: list[dict[str, object]] = []
        self.recover_calls: list[dict[str, object]] = []

    def plan(self, **kwargs: object) -> PlanningResult:
        self.plan_calls.append(kwargs)
        return self.result

    def recover(self, **kwargs: object) -> PlanningResult:
        self.recover_calls.append(kwargs)
        return self.result


class _CrashAfterReservationPlanner(_FakePlanner):
    def plan(self, **kwargs: object) -> PlanningResult:
        self.plan_calls.append(kwargs)
        persist = kwargs["persist_workspace"]
        assert callable(persist)
        persist({"owned": True})
        raise RuntimeError("simulated planner interruption")


def _runner(
    tmp_path: Path,
    fake: FakeHTTP | None = None,
    *,
    planner: object | None = None,
) -> tuple[IssueIntakeRunner, FakeHTTP, EventEnvelope]:
    fake = fake or FakeHTTP()
    config = _loaded_config(tmp_path)
    envelope = EventEnvelope.from_mapping(_event(body=fake.body))
    return (
        IssueIntakeRunner(
            config,
            http_client=fake,
            planner=_NoopPlanner() if planner is None else planner,
        ),
        fake,
        envelope,
    )


def test_source_hash_uses_exact_unicode_json_encoding() -> None:
    expected = hashlib.sha256(
        '["T\u00e9","B\u00df"]'.encode("utf-8")
    ).hexdigest()
    assert source_hash("Té", "Bß") == expected
    assert source_hash("Title", None) == source_hash("Title", "")


def test_valid_attached_plan_imports_exact_bytes_promotes_and_starts_once(tmp_path: Path) -> None:
    runner, fake, envelope = _runner(tmp_path)
    outcome = runner.process(envelope)

    assert outcome == IntakeOutcome(
        "started",
        project_id=PROJECT_ID,
        plan_path=re.match(r"plans/in-progress/issue-7-[0-9a-f]{12}\.md", outcome.plan_path or "").group(0),
        run_id="run-1",
    )
    assert fake.create_calls == 1
    assert fake.promote_calls == 1
    assert len(fake.start_calls) == 1
    key, payload = fake.start_calls[0]
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert payload["dirty_worktree_confirmed"] is False
    assert payload["extra_instructions"] == []
    assert payload["restarted_from_run_id"] is None
    receipt = runner.store.read(REPOSITORY_ID, ISSUE_NUMBER)
    assert receipt is not None
    assert receipt["state"] == "started"
    assert receipt["owner_id"] == 591691
    assert "Request" not in json.dumps(receipt)
    assert fake.plan == PLAN_TEXT.encode("utf-8")
    assert fake.plans[("todo", "source.md")] == fake.plan


def test_attached_plan_never_calls_the_planner(tmp_path: Path) -> None:
    planner = _FakePlanner()
    runner, fake, envelope = _runner(tmp_path, planner=planner)

    outcome = runner.process(envelope)

    assert outcome.status == "started"
    assert planner.plan_calls == []
    assert planner.recover_calls == []
    assert fake.model_calls == 0


def test_absent_plan_calls_one_planner_then_uses_the_shared_dispatcher(
    tmp_path: Path,
) -> None:
    fake = FakeHTTP(body="")
    planner = _FakePlanner()
    runner, fake, envelope = _runner(tmp_path, fake, planner=planner)

    outcome = runner.process(envelope)

    assert outcome.status == "started"
    assert len(planner.plan_calls) == 1
    assert planner.recover_calls == []
    assert fake.model_calls == 0
    assert fake.issue_call_count == 4
    planning_canonical = planner.plan_calls[0]["canonical"]
    assert isinstance(planning_canonical, CanonicalIssue)
    assert planning_canonical.author_id == 591691
    assert planning_canonical.title_body_sha256 == source_hash("Request", "")
    assert fake.create_calls == 1
    assert fake.promote_calls == 1
    assert len(fake.start_calls) == 1
    receipt = runner.store.read(REPOSITORY_ID, ISSUE_NUMBER)
    assert receipt is not None
    assert receipt["state"] == "started"
    assert receipt["plan"]["provenance"]["kind"] == "fixture-planner"
    reused = runner.process(
        EventEnvelope.from_mapping(_event(body="", delivery_id="delivery-2"))
    )
    assert reused.status == "started"
    assert reused.reused is True
    assert len(planner.plan_calls) == 1


def test_runner_persists_attention_for_checked_generated_plan_without_replay(
    tmp_path: Path,
) -> None:
    fake = FakeHTTP(body="")
    generated = "# Plan\n\n### [ ] Checkpoint 1: First\n- [x] already done\n- [ ] finish\n"

    class InvalidGeneratedPlanner:
        def __init__(self) -> None:
            self.calls = 0

        def plan(self, **_kwargs: object) -> object:
            self.calls += 1
            return planner_module._parse_response_bytes(
                json.dumps({"status": "plan", "markdown": generated}).encode("utf-8")
            )

        def recover(self, **_kwargs: object) -> object:
            raise AssertionError("invalid generated output must not be recovered")

    planner = InvalidGeneratedPlanner()
    runner, fake, envelope = _runner(tmp_path, fake, planner=planner)
    first = runner.process(envelope)
    second = runner.process(EventEnvelope.from_mapping(_event(body="", delivery_id="delivery-2")))
    assert first.status == second.status == "needs_attention"
    assert first.reason == second.reason == "planner_output_progress_not_pristine"
    assert planner.calls == 1
    assert fake.create_calls == 0
    assert fake.start_calls == []


def test_planner_question_is_persisted_as_attention_without_dispatch(
    tmp_path: Path,
) -> None:
    fake = FakeHTTP(body="")
    planner = _FakePlanner(
        PlanningResult(
            status="needs_attention",
            question="Which scope should the plan cover?",
        )
    )
    runner, fake, envelope = _runner(tmp_path, fake, planner=planner)

    outcome = runner.process(envelope)

    assert outcome.status == "needs_attention"
    assert outcome.reason == "planner_question"
    assert outcome.question == "Which scope should the plan cover?"
    assert len(planner.plan_calls) == 1
    assert fake.create_calls == 0
    assert fake.start_calls == []


def test_plan_ready_reuses_generated_bytes_without_replanning(
    tmp_path: Path,
) -> None:
    fake = FakeHTTP(body="")
    planner = _FakePlanner()
    runner, fake, envelope = _runner(tmp_path, fake, planner=planner)
    original = fake.post_json
    fail_preflight = True

    def fail_once(
        service: str,
        path: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> object:
        nonlocal fail_preflight
        if path.endswith("/runs/preflight") and fail_preflight:
            fail_preflight = False
            raise HTTPRequestFailure("request_exhausted", uncertain=True)
        return original(
            service,
            path,
            payload,
            idempotency_key=idempotency_key,
        )

    fake.post_json = fail_once  # type: ignore[method-assign]
    with pytest.raises(Exception, match="private_api_request_exhausted"):
        runner.process(envelope)
    receipt = runner.store.read(REPOSITORY_ID, ISSUE_NUMBER)
    assert receipt is not None
    assert receipt["state"] == "dispatching"
    assert receipt["plan"]["content_sha256"] == _digest(PLAN_TEXT.encode("utf-8"))

    outcome = runner.process(
        EventEnvelope.from_mapping(_event(body="", delivery_id="delivery-2"))
    )

    assert outcome.status == "started"
    assert len(planner.plan_calls) == 1


def test_planning_receipt_recovers_without_replaying_the_planner(
    tmp_path: Path,
) -> None:
    fake = FakeHTTP(body="")
    planner = _CrashAfterReservationPlanner()
    runner, fake, envelope = _runner(tmp_path, fake, planner=planner)

    with pytest.raises(RuntimeError, match="simulated planner interruption"):
        runner.process(envelope)
    planning_receipt = runner.store.read(REPOSITORY_ID, ISSUE_NUMBER)
    assert planning_receipt is not None
    assert planning_receipt["state"] == "planning"
    assert planning_receipt["planning"] == {"owned": True}

    outcome = runner.process(
        EventEnvelope.from_mapping(_event(body="", delivery_id="delivery-2"))
    )

    assert outcome.status == "started"
    assert len(planner.plan_calls) == 1
    assert len(planner.recover_calls) == 1
    assert fake.start_calls


def test_duplicate_redelivery_reuses_started_result_and_key(tmp_path: Path) -> None:
    runner, fake, envelope = _runner(tmp_path)
    first = runner.process(envelope)
    call_count = len(fake.start_calls)
    second = runner.process(
        EventEnvelope.from_mapping(_event(body=fake.body, delivery_id="delivery-2"))
    )
    assert first.status == second.status == "started"
    assert second.reused is True
    assert len(fake.start_calls) == call_count == 1


def test_in_progress_source_is_reused_without_import(tmp_path: Path) -> None:
    fake = FakeHTTP()
    fake.body = f"AFlow-Plan: plans/in-progress/source.md\nAFlow-Plan-SHA256: {_digest(fake.plan)}\n"
    fake.plans = {("in_progress", "source.md"): fake.plan}
    runner, fake, envelope = _runner(tmp_path, fake)
    outcome = runner.process(envelope)
    assert outcome.status == "started"
    assert fake.create_calls == 0
    assert fake.promote_calls == 0
    assert outcome.plan_path == "plans/in-progress/source.md"


def test_pinned_same_repository_blob_is_imported_from_contents_api(tmp_path: Path) -> None:
    fake = FakeHTTP()
    external_path = f"https://github.com/{FULL_NAME}/blob/{COMMIT_SHA}/plans/todo/external.md"
    fake.body = f"AFlow-Plan: {external_path}\nAFlow-Plan-SHA256: {_digest(fake.plan)}\n"
    fake.blob_payload = json.dumps(
        {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(fake.plan).decode("ascii"),
        }
    ).encode("utf-8")
    runner, fake, envelope = _runner(tmp_path, fake)
    outcome = runner.process(envelope)
    assert outcome.status == "started"
    assert outcome.plan_path is not None
    assert "/contents/plans/todo/external.md?ref=" + COMMIT_SHA in " ".join(
        path for method, path, _ in fake.calls if method == "GET_BYTES"
    )
    assert fake.create_calls == 1


def test_external_blob_wrong_digest_is_attention_without_import_or_start(tmp_path: Path) -> None:
    fake = FakeHTTP()
    external_path = f"https://github.com/{FULL_NAME}/blob/{COMMIT_SHA}/plans/todo/external.md"
    fake.body = f"AFlow-Plan: {external_path}\nAFlow-Plan-SHA256: {'0' * 64}\n"
    fake.blob_payload = fake.plan
    runner, fake, envelope = _runner(tmp_path, fake)
    outcome = runner.process(envelope)
    assert outcome.status == "needs_attention"
    assert outcome.reason == "plan_digest_mismatch"
    assert fake.create_calls == 0
    assert fake.start_calls == []


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("", "planner_not_available"),
        ("AFlow-Plan: https://evil.example/owner/repo/blob/" + COMMIT_SHA + "/plan.md\nAFlow-Plan-SHA256: " + _digest(PLAN_TEXT.encode()), "plan_origin_untrusted"),
        ("AFlow-Plan: plans/todo/source.md\n", "plan_digest_invalid"),
        ("AFlow-Plan: plans/todo/source.md\n\nAFlow-Plan-SHA256: " + _digest(PLAN_TEXT.encode()), "plan_digest_not_adjacent"),
        ("AFlow-Plan: plans/todo/source.md\nAFlow-Plan-SHA256: " + "0" * 64, "plan_digest_mismatch"),
        ("AFlow-Plan: plans/done/source.md\nAFlow-Plan-SHA256: " + _digest(PLAN_TEXT.encode()), "plan_origin_untrusted"),
        ("AFlow-Plan: plans/todo/source.md\nAFlow-Plan: plans/todo/source.md\nAFlow-Plan-SHA256: " + _digest(PLAN_TEXT.encode()), "plan_marker_count_invalid"),
        ("AFlow-Plan-SHA256: " + _digest(PLAN_TEXT.encode()), "plan_marker_count_invalid"),
    ],
)
def test_invalid_or_absent_plan_marker_never_starts_or_calls_a_model(
    tmp_path: Path,
    body: str,
    reason: str,
) -> None:
    fake = FakeHTTP(body=body)
    runner, fake, envelope = _runner(tmp_path, fake)
    outcome = runner.process(envelope)
    assert outcome.status == "needs_attention"
    assert outcome.reason == reason
    assert fake.start_calls == []
    assert fake.model_calls == 0


def test_closed_and_deferred_issues_are_ignored(tmp_path: Path) -> None:
    for field in ("closed", "deferred"):
        fake = FakeHTTP()
        setattr(fake, field, True)
        runner, fake, envelope = _runner(tmp_path / field, fake)
        outcome = runner.process(envelope)
        assert outcome.status == "ignored"
        assert outcome.reason == ("closed_issue" if field == "closed" else "deferred_issue")
        assert fake.start_calls == []


def test_non_owner_canonical_author_is_rejected_even_with_owner_sender(tmp_path: Path) -> None:
    fake = FakeHTTP()
    fake.author_id = 42
    runner, fake, envelope = _runner(tmp_path, fake)
    outcome = runner.process(envelope)
    assert outcome.status == "ignored"
    assert outcome.reason == "non_owner_author"
    assert fake.start_calls == []


def test_non_owner_edit_of_owner_issue_requires_attention(tmp_path: Path) -> None:
    fake = FakeHTTP()
    runner, fake, _ = _runner(tmp_path, fake)
    envelope = EventEnvelope.from_mapping(_event(body=fake.body, actor_id=42, action="edited"))
    outcome = runner.process(envelope)
    assert outcome.status == "needs_attention"
    assert outcome.reason == "non_owner_event_actor"
    assert fake.start_calls == []


@pytest.mark.parametrize("action", ["labeled", "commented"])
def test_owner_label_or_comment_is_not_content_admission_and_later_open_event_can_claim(
    tmp_path: Path,
    action: str,
) -> None:
    fake = FakeHTTP()
    runner, fake, _ = _runner(tmp_path, fake)
    label_event = EventEnvelope.from_mapping(_event(body=fake.body, action=action))
    ignored = runner.process(label_event)
    assert ignored.status == "ignored"
    assert ignored.reason == "non_content_event"
    opened = runner.process(EventEnvelope.from_mapping(_event(body=fake.body, delivery_id="delivery-2")))
    assert opened.status == "started"
    assert len(fake.start_calls) == 1


def test_owner_label_on_non_owner_issue_is_not_admission(tmp_path: Path) -> None:
    fake = FakeHTTP()
    fake.author_id = 42
    runner, fake, _ = _runner(tmp_path, fake)
    envelope = EventEnvelope.from_mapping(_event(body=fake.body, action="labeled"))
    outcome = runner.process(envelope)
    assert outcome.status == "ignored"
    assert outcome.reason == "non_owner_author"
    assert fake.start_calls == []


def test_stale_owner_event_hash_never_starts(tmp_path: Path) -> None:
    fake = FakeHTTP()
    runner, fake, _ = _runner(tmp_path, fake)
    envelope = EventEnvelope.from_mapping(_event(body=fake.body, title_body_sha256="f" * 64))
    outcome = runner.process(envelope)
    assert outcome.status == "needs_attention"
    assert outcome.reason == "unverified_source_revision"
    assert fake.start_calls == []


def test_canonical_content_change_at_boundary_is_recorded_as_attention(tmp_path: Path) -> None:
    fake = FakeHTTP()
    fake.change_issue_at = 2
    runner, fake, envelope = _runner(tmp_path, fake)
    outcome = runner.process(envelope)
    assert outcome.status == "needs_attention"
    assert outcome.reason == "source_boundary_changed"
    receipt = runner.store.read(REPOSITORY_ID, ISSUE_NUMBER)
    assert receipt is not None
    assert receipt["state"] == "needs_attention"
    assert fake.start_calls == []


def test_canonical_content_change_immediately_before_start_is_recorded(tmp_path: Path) -> None:
    fake = FakeHTTP()
    fake.change_issue_at = 3
    runner, fake, envelope = _runner(tmp_path, fake)
    outcome = runner.process(envelope)
    assert outcome.status == "needs_attention"
    assert outcome.reason == "source_boundary_changed"
    assert fake.start_calls == []


def test_changed_source_after_prior_claim_requires_reconciliation(tmp_path: Path) -> None:
    fake = FakeHTTP()
    runner, fake, envelope = _runner(tmp_path, fake)
    assert runner.process(envelope).status == "started"
    fake.title = "Changed"
    new_event = EventEnvelope.from_mapping(
        _event(
            body=fake.body,
            delivery_id="delivery-2",
            title_body_sha256=source_hash("Changed", fake.body),
        )
    )
    outcome = runner.process(new_event)
    assert outcome.status == "needs_attention"
    assert outcome.reason == "source_revision_requires_reconciliation"
    assert len(fake.start_calls) == 1


@pytest.mark.parametrize("which", ["create", "promote"])
def test_lost_plan_write_response_is_recovered_only_from_exact_bytes(tmp_path: Path, which: str) -> None:
    fake = FakeHTTP()
    setattr(fake, f"lose_{which}", True)
    runner, fake, envelope = _runner(tmp_path, fake)
    outcome = runner.process(envelope)
    assert outcome.status == "started"
    assert fake.start_calls[0][1]["plan_path"].startswith("plans/in-progress/issue-7-")
    assert getattr(fake, f"{which}_calls") == 1


def test_lost_start_response_retains_saved_key_and_attention(tmp_path: Path) -> None:
    fake = FakeHTTP()
    fake.lose_start_count = 1
    runner, fake, envelope = _runner(tmp_path, fake)
    outcome = runner.process(envelope)
    assert outcome.status == "needs_attention"
    assert outcome.reason == "start_result_uncertain"
    assert len(fake.start_calls) == 1
    receipt = runner.store.read(REPOSITORY_ID, ISSUE_NUMBER)
    assert receipt is not None
    assert receipt["state"] == "needs_attention"
    assert receipt["launch"]["idempotency_key"] == fake.start_calls[0][0]


def test_start_adapter_never_falls_back_without_idempotency_key(tmp_path: Path) -> None:
    class BrokenStartAdapter(FakeHTTP):
        def __init__(self) -> None:
            super().__init__()
            self.start_attempts: list[str | None] = []

        def post_json(
            self,
            service: str,
            path: str,
            payload: Mapping[str, object],
            *,
            idempotency_key: str | None = None,
        ) -> object:
            if path.endswith("/runs"):
                self.start_attempts.append(idempotency_key)
                raise TypeError("broken keyword-aware start adapter")
            return super().post_json(service, path, payload, idempotency_key=idempotency_key)

    fake = BrokenStartAdapter()
    runner, _, envelope = _runner(tmp_path, fake)
    with pytest.raises(TypeError, match="broken keyword-aware"):
        runner.process(envelope)
    assert len(fake.start_attempts) == 1
    assert fake.start_attempts[0] is not None
    receipt = runner.store.read(REPOSITORY_ID, ISSUE_NUMBER)
    assert receipt is not None
    assert receipt["state"] == "dispatching"
    assert receipt["launch"]["idempotency_key"] == fake.start_attempts[0]


def test_restart_from_dispatching_receipt_reuses_saved_key_and_artifact(tmp_path: Path) -> None:
    fake = FakeHTTP()
    runner, fake, envelope = _runner(tmp_path, fake)
    original = fake.post_json
    fail_preflight = True

    def fail_once(
        service: str,
        path: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> object:
        nonlocal fail_preflight
        if path.endswith("/runs/preflight") and fail_preflight:
            fail_preflight = False
            raise HTTPRequestFailure("request_exhausted", uncertain=True)
        return original(service, path, payload, idempotency_key=idempotency_key)

    fake.post_json = fail_once  # type: ignore[method-assign]
    with pytest.raises(Exception, match="private_api_request_exhausted"):
        runner.process(envelope)
    receipt = runner.store.read(REPOSITORY_ID, ISSUE_NUMBER)
    assert receipt is not None
    assert receipt["state"] == "dispatching"
    saved_key = receipt["launch"]["idempotency_key"]

    outcome = runner.process(envelope)
    assert outcome.status == "started"
    assert fake.start_calls[0][0] == saved_key


def test_import_destination_conflict_never_overwrites_existing_bytes(tmp_path: Path) -> None:
    fake = FakeHTTP()
    runner, fake, envelope = _runner(tmp_path, fake)
    claim_sha = hashlib.sha256(
        json.dumps(
            [REPOSITORY_ID, ISSUE_NUMBER, envelope.title_body_sha256],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    name = f"issue-{ISSUE_NUMBER}-{claim_sha[:12]}.md"
    conflicting = b"# Different\n\n### [ ] Checkpoint 1\n- [ ] other\n"
    fake.plans[("in_progress", name)] = conflicting
    outcome = runner.process(envelope)
    assert outcome.status == "needs_attention"
    assert outcome.reason == "import_destination_conflict"
    assert fake.plans[("in_progress", name)] == conflicting
    assert fake.start_calls == []


def test_same_issue_concurrency_is_serialized_by_the_advisory_lock(tmp_path: Path) -> None:
    fake = FakeHTTP()
    entered = Event()
    release = Event()
    original = fake.get_json
    block_once = True

    def blocking_get_json(service: str, path: str) -> object:
        nonlocal block_once
        if service == "github" and "/issues/" in path and block_once:
            block_once = False
            entered.set()
            assert release.wait(5)
        return original(service, path)

    fake.get_json = blocking_get_json  # type: ignore[method-assign]
    config = _loaded_config(tmp_path)
    envelope = EventEnvelope.from_mapping(_event(body=fake.body))
    runner_one = IssueIntakeRunner(config, http_client=fake)
    runner_two = IssueIntakeRunner(config, http_client=fake)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(runner_one.process, envelope)
        assert entered.wait(5)
        second = executor.submit(runner_two.process, envelope)
        release.set()
        outcomes = [first.result(timeout=10), second.result(timeout=10)]
    assert {outcome.status for outcome in outcomes} == {"started"}
    assert len(fake.start_calls) == 1


def test_startup_question_is_saved_without_an_answer(tmp_path: Path) -> None:
    fake = FakeHTTP()
    fake.start_response = {
        "startup_question": {
            "question_id": "question-1",
            "kind": "pick_step",
            "message": "Select a step",
            "choices": ["implement"],
            "options": {},
            "run_id": "run-question",
        }
    }
    runner, fake, envelope = _runner(tmp_path, fake)
    outcome = runner.process(envelope)
    assert outcome.status == "started"
    assert outcome.startup_question is not None
    assert outcome.startup_question["question_id"] == "question-1"
    receipt = runner.store.read(REPOSITORY_ID, ISSUE_NUMBER)
    assert receipt is not None
    assert receipt["result"]["startup_question"]["question_id"] == "question-1"


def test_event_file_dash_reads_stdin_and_cli_emits_bounded_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = _write_config(tmp_path, enabled=False)
    event = json.dumps(_event(), ensure_ascii=False)
    code = main(
        ["--config", str(config_path), "--event-file", "-"],
        stdin=io.StringIO(event),
    )
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out) == {"reason": "disabled", "reused": False, "status": "ignored"}


def test_config_cannot_widen_owner_admission_and_requires_strict_event_fields(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, owner_id=42)
    with pytest.raises(Exception, match="owner_id_cannot_be_widened"):
        load_config(config_path)
    with pytest.raises(Exception, match="event_envelope_unknown_field"):
        EventEnvelope.from_mapping({**_event(), "issue_title": "raw text"})


def test_config_accepts_only_canonical_schema(tmp_path: Path) -> None:
    canonical = _config_text(tmp_path)
    path = tmp_path / "intake.toml"
    path.write_text(canonical, encoding="utf-8")
    assert load_config(path).mapping_for(REPOSITORY_ID, FULL_NAME) is not None

    invalid = {
        "api_url": canonical.replace("private_api_url", "api_url"),
        "api_token_file": canonical.replace("private_credential_file", "api_token_file"),
        "github_token_file": canonical.replace("github_credential_file", "github_token_file"),
        "workflow_name": canonical.replace("workflow =", "workflow_name ="),
        "team_name": canonical.replace("team =", "team_name ="),
        "repository_alias": canonical.replace("repository_id =", "id ="),
        "single_repository_table": canonical.replace("[[issue_intake.repositories]]", "[issue_intake.repositories]"),
        "keyed_repository_table": canonical.replace(
            "[[issue_intake.repositories]]", '[issue_intake.repositories."123"]'
        ),
    }
    for value in invalid.values():
        path.write_text(value, encoding="utf-8")
        with pytest.raises(Exception):
            load_config(path)

    duplicate = canonical + (
        "\n[[issue_intake.repositories]]\nrepository_id = 123\n"
        'full_name = "other/repo"\nproject_id = "other"\n'
    )
    path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(Exception, match="repository_mappings_ambiguous"):
        load_config(path)
    path.write_text(
        canonical.replace(
            "\n\n[[issue_intake.repositories]]", "\nowner_id = 42\n\n[[issue_intake.repositories]]"
        ),
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="owner_id_cannot_be_widened"):
        load_config(path)


def test_unconfigured_repository_is_attention_without_github_fetch(tmp_path: Path) -> None:
    fake = FakeHTTP()
    runner, fake, envelope = _runner(tmp_path, fake)
    event = EventEnvelope.from_mapping({**_event(), "repository_id": 999})
    outcome = runner.process(event)
    assert outcome.status == "needs_attention"
    assert outcome.reason == "repository_not_configured"
    assert fake.calls == []


def test_private_registry_and_lifecycle_are_required_before_start(tmp_path: Path) -> None:
    fake = FakeHTTP()
    runner, fake, envelope = _runner(tmp_path, fake)
    fake.start_response = {"result": {"run_id": "run-1", "created": True, "status": "launch_requested"}}
    original = fake.post_json
    def broken_preflight(service: str, path: str, payload: Mapping[str, object]) -> object:
        if path.endswith("/runs/preflight"):
            return {"execution_mode": "same_checkout", "dirty": False, "requires_confirmation": False, "blockers": []}
        return original(service, path, payload)
    def compatible_broken_preflight(
        service: str,
        path: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> object:
        return broken_preflight(service, path, payload)
    fake.post_json = compatible_broken_preflight  # type: ignore[method-assign]
    outcome = runner.process(envelope)
    assert outcome.status == "needs_attention"
    assert outcome.reason == "managed_worktree_lifecycle_unresolved"
    assert fake.start_calls == []


class _Response:
    def __init__(self, data: bytes, *, url: str, status: int = 200) -> None:
        self.data = data
        self.url = url
        self.status = status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def geturl(self) -> str:
        return self.url

    def getcode(self) -> int:
        return self.status

    def read(self, limit: int = -1) -> bytes:
        return self.data if limit < 0 else self.data[:limit]


class _Opener:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.requests: list[tuple[object, float]] = []

    def open(self, request: object, *, timeout: float) -> object:
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _RunnerTransportOpener:
    def __init__(self, fake: FakeHTTP, *, start_failures: int) -> None:
        self.fake = fake
        self.start_failures = start_failures
        self.requests: list[tuple[object, float]] = []
        self.start_requests: list[tuple[bytes | None, str | None]] = []

    def open(self, request: object, *, timeout: float) -> object:
        self.requests.append((request, timeout))
        full_url = request.full_url  # type: ignore[attr-defined]
        parsed = urlsplit(full_url)
        service = "private" if parsed.hostname == "private.example" else "github"
        path = parsed.path
        if parsed.query:
            path += "?" + parsed.query
        idempotency_key = request.get_header("Idempotency-key")  # type: ignore[attr-defined]
        if service == "private" and path.endswith("/runs"):
            self.start_requests.append((request.data, idempotency_key))  # type: ignore[attr-defined]
            if len(self.start_requests) <= self.start_failures:
                raise URLError("lost start response")
        payload: Mapping[str, object] | None = None
        if request.data is not None:  # type: ignore[attr-defined]
            payload = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        try:
            if payload is None:
                value = self.fake.get_json(service, path)
            else:
                value = self.fake.post_json(
                    service,
                    path,
                    payload,
                    idempotency_key=idempotency_key,
                )
        except HTTPRequestFailure as exc:
            raise HTTPError(
                full_url,
                exc.status_code or 503,
                "fixture failure",
                {},
                io.BytesIO(b""),
            ) from exc
        return _Response(
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
            url=full_url,
        )


class _ImportTransportOpener:
    def __init__(
        self,
        fake: FakeHTTP,
        *,
        which: str,
        readback: str,
        content: bytes,
    ) -> None:
        self.fake = fake
        self.which = which
        self.readback = readback
        self.content = content
        self.mutation_path: str | None = None
        self.mutation_requests: list[tuple[str, bytes | None, str | None]] = []
        self.post_requests: list[tuple[str, bytes | None, str | None]] = []
        self.get_paths: list[str] = []
        self.effect_count = 0

    def _target_path(self, path: str) -> bool:
        return (
            self.which == "create"
            and path == f"/api/projects/{PROJECT_ID}/plans"
        ) or (
            self.which == "promote"
            and path.startswith(f"/api/projects/{PROJECT_ID}/plans/todo/")
            and path.endswith("/promote")
        )

    def _apply_effect(self, path: str, payload: Mapping[str, object]) -> None:
        self.effect_count += 1
        if self.which == "create":
            name = payload["name"]
            assert isinstance(name, str)
            if self.readback == "exact":
                self.fake.plans[("todo", name)] = self.content
            elif self.readback == "conflicting":
                self.fake.plans[("todo", name)] = b"# conflicting\n"
            return
        name = path.split("/")[-2]
        if self.readback == "exact":
            value = self.fake.plans.pop(("todo", name))
            self.fake.plans[("in_progress", name)] = value
        elif self.readback == "conflicting":
            self.fake.plans[("in_progress", name)] = b"# conflicting\n"

    def open(self, request: object, *, timeout: float) -> object:
        del timeout
        full_url = request.full_url  # type: ignore[attr-defined]
        parsed = urlsplit(full_url)
        service = "private" if parsed.hostname == "private.example" else "github"
        path = parsed.path
        if parsed.query:
            path += "?" + parsed.query
        data = request.data  # type: ignore[attr-defined]
        idempotency_key = request.get_header("Idempotency-key")  # type: ignore[attr-defined]
        if data is not None:
            self.post_requests.append((path, data, idempotency_key))
        else:
            self.get_paths.append(path)
        payload: Mapping[str, object] | None = None
        if data is not None:
            payload = json.loads(data.decode("utf-8"))
        if payload is not None and self._target_path(path):
            self.mutation_path = path
            self.mutation_requests.append((path, data, idempotency_key))
            if len(self.mutation_requests) == 1:
                self._apply_effect(path, payload)
            if len(self.mutation_requests) > 4:
                raise AssertionError("mutation transport retried beyond its budget")
            raise HTTPError(
                full_url,
                503,
                "lost mutation response",
                {},
                io.BytesIO(b"lost"),
            )
        try:
            if payload is None:
                value = self.fake.get_json(service, path)
            else:
                value = self.fake.post_json(
                    service,
                    path,
                    payload,
                    idempotency_key=idempotency_key,
                )
        except HTTPRequestFailure as exc:
            raise HTTPError(
                full_url,
                exc.status_code or 503,
                "fixture failure",
                {},
                io.BytesIO(b""),
            ) from exc
        return _Response(
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
            url=full_url,
        )


def test_fixed_http_client_retries_only_transient_requests_with_bounded_timeout() -> None:
    opener = _Opener(
        [
            HTTPError("http://private.example/ready", 500, "", {}, io.BytesIO(b"error")),
            HTTPError("http://private.example/ready", 503, "", {}, io.BytesIO(b"error")),
            _Response(
                b'{"ready":true}',
                url="http://private.example/ready",
            ),
        ]
    )
    sleeps: list[float] = []
    client = FixedOriginHTTPClient(
        private_api_url="http://private.example",
        private_token="private-secret",
        github_api_url="https://api.github.example",
        github_token="github-secret",
        sleep=sleeps.append,
    )
    client._opener = opener  # type: ignore[assignment]
    assert client.get_json("private", "/ready") == {"ready": True}
    assert sleeps == [1.0, 5.0]
    assert [timeout for _, timeout in opener.requests] == [15.0, 15.0, 15.0]
    request = opener.requests[0][0]
    assert request.full_url == "http://private.example/ready"  # type: ignore[attr-defined]
    assert request.get_header("Authorization") == "Bearer private-secret"  # type: ignore[attr-defined]


@pytest.mark.parametrize(("start_failures", "expected"), [(4, "needs_attention"), (3, "started")])
def test_start_retry_budget_is_shared_with_transport(
    tmp_path: Path, start_failures: int, expected: str
) -> None:
    fake = FakeHTTP()
    opener = _RunnerTransportOpener(fake, start_failures=start_failures)
    sleeps: list[float] = []
    client = FixedOriginHTTPClient(
        private_api_url="http://private.example",
        private_token="private-secret",
        github_api_url="https://api.github.example",
        github_token="github-secret",
        sleep=sleeps.append,
    )
    client._opener = opener  # type: ignore[assignment]
    config = _loaded_config(tmp_path)
    runner = IssueIntakeRunner(config, http_client=client, planner=_NoopPlanner())
    envelope = EventEnvelope.from_mapping(_event(body=fake.body))
    outcome = runner.process(envelope)
    assert outcome.status == expected
    assert len(opener.start_requests) == 4
    assert sleeps == [1.0, 5.0, 15.0]
    bodies = [body for body, _ in opener.start_requests]
    keys = [key for _, key in opener.start_requests]
    assert bodies == [bodies[0]] * 4
    assert keys == [keys[0]] * 4
    assert keys[0] is not None
    if expected == "needs_attention":
        receipt = runner.store.read(REPOSITORY_ID, ISSUE_NUMBER)
        assert receipt is not None
        assert receipt["state"] == "needs_attention"
        assert receipt["reason"] == "start_result_uncertain"
        assert fake.start_calls == []
    else:
        assert len(fake.start_calls) == 1


@pytest.mark.parametrize(
    ("which", "readback", "expected"),
    [
        ("create", "exact", "started"),
        ("create", "absent", "transport"),
        ("create", "conflicting", "attention"),
        ("promote", "exact", "started"),
        ("promote", "absent", "transport"),
        ("promote", "conflicting", "attention"),
    ],
)
def test_import_recovery_does_not_restart_post_retry_budget(
    tmp_path: Path, which: str, readback: str, expected: str
) -> None:
    content = PLAN_TEXT.encode("utf-8")
    claim_sha256 = "c" * 64

    fake = FakeHTTP()
    name = f"issue-{ISSUE_NUMBER}-{claim_sha256[:12]}.md"
    if which == "promote":
        fake.plans[("todo", name)] = content
    opener = _ImportTransportOpener(
        fake,
        which=which,
        readback=readback,
        content=content,
    )
    sleeps: list[float] = []
    client = FixedOriginHTTPClient(
        private_api_url="http://private.example",
        private_token="private-secret",
        github_api_url="https://api.github.example",
        github_token="github-secret",
        sleep=sleeps.append,
    )
    client._opener = opener  # type: ignore[assignment]
    if expected == "started":
        artifact = _import_plan(
            client,
            project_id=PROJECT_ID,
            issue_number=ISSUE_NUMBER,
            claim_sha256=claim_sha256,
            content=content,
            provenance={"kind": "fixture"},
        )
        assert artifact.content_sha256 == _digest(content)
        assert artifact.revision == _digest(content)
        assert fake.plans[("in_progress", artifact.name)] == content
    else:
        with pytest.raises((TransportFailure, AttentionFailure)) as failure:
            _import_plan(
                client,
                project_id=PROJECT_ID,
                issue_number=ISSUE_NUMBER,
                claim_sha256=claim_sha256,
                content=content,
                provenance={"kind": "fixture"},
            )
        if expected == "transport":
            assert isinstance(failure.value, TransportFailure)
        else:
            assert isinstance(failure.value, AttentionFailure)
    assert sleeps == [1.0, 5.0, 15.0]
    assert opener.mutation_path is not None
    assert len(opener.mutation_requests) == 4
    assert opener.mutation_requests == [opener.mutation_requests[0]] * 4
    assert opener.effect_count == 1
    source_path = f"/api/projects/{PROJECT_ID}/plans/todo/{name}"
    destination_path = f"/api/projects/{PROJECT_ID}/plans/in_progress/{name}"
    assert set(opener.get_paths) <= {source_path, destination_path}
    assert source_path in opener.get_paths
    assert destination_path in opener.get_paths
    payloads = [json.loads(data.decode("utf-8")) for _, data, _ in opener.mutation_requests]
    assert payloads == [payloads[0]] * 4
    assert [key for _, _, key in opener.mutation_requests] == [None] * 4
    if which == "promote":
        assert payloads[0] == {"expected_revision": _digest(content)}
        create_requests = [
            request
            for request in opener.post_requests
            if request[0] == f"/api/projects/{PROJECT_ID}/plans"
        ]
        assert create_requests == []
    else:
        assert payloads[0] == {"content": content.decode("utf-8"), "name": name}
        promote_requests = [
            request
            for request in opener.post_requests
            if request[0].endswith("/promote")
        ]
        assert len(promote_requests) == (1 if expected == "started" else 0)
        if promote_requests:
            assert promote_requests[0][1] is not None
            assert json.loads(promote_requests[0][1].decode("utf-8")) == {
                "expected_revision": _digest(content)
            }
    if readback == "exact":
        assert fake.plans[("in_progress", name)] == content
        assert fake.plans.get(("todo", name)) is None
    elif readback == "conflicting":
        if which == "promote":
            assert fake.plans[("in_progress", name)] == b"# conflicting\n"
            assert fake.plans[("todo", name)] == content
        else:
            assert fake.plans[("todo", name)] == b"# conflicting\n"
            assert fake.plans.get(("in_progress", name)) is None
    elif which == "promote":
        assert fake.plans[("todo", name)] == content
        assert fake.plans.get(("in_progress", name)) is None
    else:
        assert fake.plans.get(("todo", name)) is None
        assert fake.plans.get(("in_progress", name)) is None


def test_fixed_http_client_does_not_follow_redirects_or_accept_oversized_input() -> None:
    redirect_opener = _Opener(
        [HTTPError("http://private.example/ready", 302, "", {"Location": "https://evil.example"}, io.BytesIO())]
    )
    client = FixedOriginHTTPClient(
        private_api_url="http://private.example",
        private_token="private-secret",
        github_api_url="https://api.github.example",
        github_token="github-secret",
    )
    client._opener = redirect_opener  # type: ignore[assignment]
    with pytest.raises(HTTPRequestFailure):
        client.get_bytes("private", "/ready")
    assert len(redirect_opener.requests) == 1

    oversized = _Opener(
        [_Response(b"x" * (256 * 1024 + 1), url="http://private.example/ready")]
    )
    client._opener = oversized  # type: ignore[assignment]
    with pytest.raises(HTTPRequestFailure, match="response_too_large"):
        client.get_bytes("private", "/ready")
