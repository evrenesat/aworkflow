"""One-shot, model-free intake of owner-authored GitHub issues.

GitHub Actions (or another private relay) supplies a small event envelope.
This command independently reads the current issue, reserves a durable local
receipt, imports a verified plan through the existing plan API, and starts the
existing control-plane workflow.  It has no daemon or background queue.
"""

from __future__ import annotations

import argparse
import base64
import binascii
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import tomllib
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .issue_intake_store import (
    IssueIntakeStore,
    IntakeStoreError,
    RECEIPT_SCHEMA_VERSION,
)
from .plan import PlanParseError, parse_plan_text


OWNER_ID = 591691
GITHUB_DEFAULT_API_URL = "https://api.github.com"
HTTP_TIMEOUT_SECONDS = 15.0
MAX_HTTP_INPUT_BYTES = 256 * 1024
MAX_EVENT_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 128 * 1024
MAX_CREDENTIAL_BYTES = 8 * 1024
MAX_RECEIPT_TEXT_BYTES = 4 * 1024
MAX_OUTPUT_BYTES = 16 * 1024
MAX_PLAN_NAME_LENGTH = 200
PLAN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.md$")
PROJECT_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REPOSITORY_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
EXECUTABLE_ACTIONS = frozenset({"opened", "edited"})
TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class IntakeFailure(RuntimeError):
    """A bounded failure that can be safely reported by the command."""

    exit_code = 2

    def __init__(self, reason: str, *, exit_code: int | None = None) -> None:
        self.reason = reason
        if exit_code is not None:
            self.exit_code = exit_code
        super().__init__(reason)


class ConfigFailure(IntakeFailure):
    """The operator configuration is missing or unsafe."""


class AdmissionFailure(IntakeFailure):
    """The event or canonical issue cannot be admitted."""


class AttentionFailure(IntakeFailure):
    """A human must reconcile an otherwise well-formed intake."""


class TransportFailure(IntakeFailure):
    """A bounded request exhausted its transport attempts."""

    exit_code = 1


class HTTPRequestFailure(RuntimeError):
    """A private or GitHub request failed without exposing response details."""

    def __init__(
        self,
        reason: str,
        *,
        status_code: int | None = None,
        uncertain: bool = False,
    ) -> None:
        self.reason = reason
        self.status_code = status_code
        self.uncertain = uncertain
        super().__init__(reason)


def _is_positive_int(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value <= (2**63 - 1)
    )


def _bounded_bytes(value: bytes, *, label: str, limit: int = MAX_HTTP_INPUT_BYTES) -> bytes:
    if len(value) > limit:
        raise AttentionFailure(f"{label}_too_large")
    try:
        value.decode("utf-8")
    except UnicodeError as exc:
        raise AttentionFailure(f"{label}_invalid_utf8") from exc
    return value


def _json_object_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _compact_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise IntakeFailure("json_encoding_failed") from exc


def source_hash(title: str, body: str | None) -> str:
    """Hash the relay/host canonical representation of issue source text."""
    if not isinstance(title, str) or not isinstance(body, (str, type(None))):
        raise ValueError("title and body must be text")
    canonical = json.dumps(
        [title, body or ""],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_claim(repository_id: int, issue_number: int, event_source_sha256: str) -> str:
    payload = _compact_json(
        [repository_id, issue_number, event_source_sha256]
    )
    return _sha256_bytes(payload)


def _hash_start_key(
    repository_id: int,
    issue_number: int,
    claim_sha256: str,
    project_id: str,
    plan_path: str,
    plan_revision: str,
) -> str:
    payload = _compact_json(
        {
            "claim_sha256": claim_sha256,
            "issue_number": issue_number,
            "plan_path": plan_path,
            "plan_revision": plan_revision,
            "project_id": project_id,
            "repository_id": repository_id,
        }
    )
    return _sha256_bytes(payload)


def _read_bounded_file(path: Path, *, limit: int, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ConfigFailure(f"{label}_unavailable")
    try:
        metadata = path.stat()
        if metadata.st_size > limit:
            raise ConfigFailure(f"{label}_too_large")
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except ConfigFailure:
        raise
    except OSError as exc:
        raise ConfigFailure(f"{label}_unavailable") from exc
    if len(data) > limit:
        raise ConfigFailure(f"{label}_too_large")
    return data


@dataclass(frozen=True)
class RepositoryMapping:
    repository_id: int
    full_name: str
    project_id: str


@dataclass(frozen=True)
class IntakeConfig:
    enabled: bool
    state_root: Path
    private_api_url: str
    private_credential_file: Path
    github_api_url: str
    github_credential_file: Path
    repositories: tuple[RepositoryMapping, ...]
    deferred_labels: tuple[str, ...]
    workflow_name: str
    team: str
    start_step: str | None = None
    max_turns: int | None = None
    owner_id: int = OWNER_ID

    def mapping_for(self, repository_id: int, full_name: str) -> RepositoryMapping | None:
        for mapping in self.repositories:
            if mapping.repository_id == repository_id and mapping.full_name == full_name:
                return mapping
        return None


def _absolute_config_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ConfigFailure(f"{label}_invalid")
    path = Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise ConfigFailure(f"{label}_must_be_absolute")
    return path


def _url_origin(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or any(char in value for char in "\r\n\x00"):
        raise ConfigFailure(f"{label}_invalid")
    if len(value) > 512:
        raise ConfigFailure(f"{label}_too_large")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigFailure(f"{label}_invalid")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigFailure(f"{label}_invalid")
    try:
        host = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ConfigFailure(f"{label}_invalid") from exc
    if not host:
        raise ConfigFailure(f"{label}_invalid")
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _parse_repository_mapping(value: object) -> RepositoryMapping:
    if not isinstance(value, Mapping):
        raise ConfigFailure("repository_mapping_invalid")
    if set(value) != {"repository_id", "full_name", "project_id"}:
        raise ConfigFailure("unknown_repository_mapping_field")
    repository_id_value = value["repository_id"]
    if not _is_positive_int(repository_id_value):
        raise ConfigFailure("repository_id_invalid")
    full_name = value["full_name"]
    if not isinstance(full_name, str) or REPOSITORY_NAME_RE.fullmatch(full_name) is None:
        raise ConfigFailure("repository_full_name_invalid")
    project_id = value.get("project_id")
    if (
        not isinstance(project_id, str)
        or len(project_id) > 128
        or PROJECT_ID_RE.fullmatch(project_id) is None
    ):
        raise ConfigFailure("project_id_invalid")
    return RepositoryMapping(repository_id_value, full_name, project_id)


def _parse_repository_mappings(value: object) -> tuple[RepositoryMapping, ...]:
    if not isinstance(value, list):
        raise ConfigFailure("repository_mappings_invalid")
    mappings = [_parse_repository_mapping(item) for item in value]
    if not mappings:
        raise ConfigFailure("repository_mappings_empty")
    ids = {mapping.repository_id for mapping in mappings}
    names = {mapping.full_name for mapping in mappings}
    if len(ids) != len(mappings) or len(names) != len(mappings):
        raise ConfigFailure("repository_mappings_ambiguous")
    return tuple(mappings)


def load_config(path: Path) -> IntakeConfig:
    """Load and fail-closed validate the host-only TOML configuration."""
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ConfigFailure("config_unavailable")
    data = _read_bounded_file(path, limit=MAX_CONFIG_BYTES, label="config")
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigFailure("config_invalid_toml") from exc
    if not isinstance(raw, dict) or set(raw) != {"issue_intake"}:
        raise ConfigFailure("config_invalid")
    section = raw.get("issue_intake")
    if not isinstance(section, dict):
        raise ConfigFailure("missing_config_issue_intake")

    fields = {
        "enabled",
        "state_root",
        "private_api_url",
        "private_credential_file",
        "github_api_url",
        "github_credential_file",
        "repositories",
        "deferred_labels",
        "workflow",
        "team",
        "start_step",
        "max_turns",
        "owner_id",
    }
    unknown = set(section) - fields
    if unknown:
        raise ConfigFailure("unknown_config_field")

    def required(name: str) -> object:
        if name not in section:
            raise ConfigFailure(f"missing_config_{name}")
        return section[name]

    enabled = required("enabled")
    if not isinstance(enabled, bool):
        raise ConfigFailure("enabled_config_invalid")
    state_root = _absolute_config_path(required("state_root"), label="state_root")
    private_api_url = _url_origin(
        required("private_api_url"),
        label="private_api_url",
    )
    private_credential_file = _absolute_config_path(
        required("private_credential_file"),
        label="private_credential_file",
    )
    github_api_url = _url_origin(
        section.get("github_api_url", GITHUB_DEFAULT_API_URL),
        label="github_api_url",
    )
    github_credential_file = _absolute_config_path(
        required("github_credential_file"),
        label="github_credential_file",
    )
    repositories = _parse_repository_mappings(required("repositories"))
    labels_value = required("deferred_labels")
    if not isinstance(labels_value, list) or len(labels_value) > 64:
        raise ConfigFailure("deferred_labels_invalid")
    deferred_labels: list[str] = []
    for label in labels_value:
        if not isinstance(label, str) or not label or len(label.encode("utf-8")) > 256:
            raise ConfigFailure("deferred_labels_invalid")
        deferred_labels.append(label.casefold())
    workflow_name = required("workflow")
    team = required("team")
    if not isinstance(workflow_name, str) or not workflow_name or len(workflow_name) > 128:
        raise ConfigFailure("workflow_invalid")
    if (
        not isinstance(team, str)
        or not team
        or len(team) > 128
        or any(char in team for char in "\x00\r\n")
    ):
        raise ConfigFailure("team_invalid")
    if any(char in workflow_name for char in "\x00\r\n"):
        raise ConfigFailure("workflow_invalid")
    start_step = section.get("start_step")
    if start_step is not None and (
        not isinstance(start_step, str)
        or not start_step
        or len(start_step) > 128
        or any(char in start_step for char in "\x00\r\n")
    ):
        raise ConfigFailure("start_step_invalid")
    max_turns = section.get("max_turns")
    if max_turns is not None and not _is_positive_int(max_turns):
        raise ConfigFailure("max_turns_invalid")
    owner_id = section.get("owner_id")
    if owner_id is not None and owner_id != OWNER_ID:
        raise ConfigFailure("owner_id_cannot_be_widened")
    return IntakeConfig(
        enabled=enabled,
        state_root=state_root,
        private_api_url=private_api_url,
        private_credential_file=private_credential_file,
        github_api_url=github_api_url,
        github_credential_file=github_credential_file,
        repositories=repositories,
        deferred_labels=tuple(deferred_labels),
        workflow_name=workflow_name,
        team=team,
        start_step=start_step,
        max_turns=max_turns,
        owner_id=OWNER_ID,
    )


def _read_credential(path: Path, *, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ConfigFailure(f"{label}_unavailable")
    try:
        metadata = path.stat()
        if metadata.st_size > MAX_CREDENTIAL_BYTES:
            raise ConfigFailure(f"{label}_too_large")
        value = path.read_text(encoding="utf-8")
    except ConfigFailure:
        raise
    except (OSError, UnicodeError) as exc:
        raise ConfigFailure(f"{label}_unavailable") from exc
    value = value.strip()
    if not value or any(char in value for char in "\x00\r\n"):
        raise ConfigFailure(f"{label}_invalid")
    return value


class IntakeHTTPClient(Protocol):
    def get_json(self, service: str, path: str) -> object:
        ...

    def get_bytes(self, service: str, path: str) -> bytes:
        ...

    def post_json(
        self,
        service: str,
        path: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> object:
        ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, new: str):
        return None


def _read_response(response: Any) -> bytes:
    data = response.read(MAX_HTTP_INPUT_BYTES + 1)
    if len(data) > MAX_HTTP_INPUT_BYTES:
        raise HTTPRequestFailure("response_too_large")
    return data


class FixedOriginHTTPClient:
    """Small urllib client with no redirect following and bounded responses."""

    def __init__(
        self,
        *,
        private_api_url: str,
        private_token: str,
        github_api_url: str,
        github_token: str,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._origins = {
            "private": (_url_origin(private_api_url, label="private_api_url"), private_token),
            "github": (_url_origin(github_api_url, label="github_api_url"), github_token),
        }
        self._sleep = sleep
        self._opener = build_opener(_NoRedirectHandler())

    def _request(
        self,
        service: str,
        method: str,
        path: str,
        payload: bytes | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> bytes:
        if service not in self._origins:
            raise HTTPRequestFailure("unknown_http_service")
        if not path.startswith("/") or any(char in path for char in "\r\n\x00"):
            raise HTTPRequestFailure("http_path_invalid")
        base, token = self._origins[service]
        url = base + path
        origin = urlsplit(base)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "aflow-issue-intake/1",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        last: HTTPRequestFailure | None = None
        for attempt in range(4):
            request = Request(url, data=payload, headers=headers, method=method)
            try:
                with self._opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                    actual = urlsplit(response.geturl())
                    if (actual.scheme, actual.netloc) != (origin.scheme, origin.netloc):
                        raise HTTPRequestFailure("redirect_origin_rejected")
                    status_code = int(response.getcode() or 0)
                    data = _read_response(response)
                    if status_code < 200 or status_code >= 300:
                        raise HTTPRequestFailure(
                            "http_status",
                            status_code=status_code,
                            uncertain=method == "POST",
                        )
                    return data
            except HTTPRequestFailure as exc:
                last = exc
            except HTTPError as exc:
                try:
                    data = exc.read(MAX_HTTP_INPUT_BYTES + 1)
                except OSError:
                    data = b""
                if len(data) > MAX_HTTP_INPUT_BYTES:
                    last = HTTPRequestFailure(
                        "response_too_large",
                        status_code=exc.code,
                        uncertain=method == "POST",
                    )
                else:
                    last = HTTPRequestFailure(
                        "http_status",
                        status_code=exc.code,
                        uncertain=method == "POST",
                    )
            except (OSError, URLError, TimeoutError):
                last = HTTPRequestFailure("transport_error", uncertain=method == "POST")
            if last is None:
                last = HTTPRequestFailure("transport_error", uncertain=method == "POST")
            if last.status_code not in TRANSIENT_HTTP_STATUSES and last.reason != "transport_error":
                raise last
            if attempt == 3:
                break
            self._sleep((1.0, 5.0, 15.0)[attempt])
        assert last is not None
        raise HTTPRequestFailure(
            "request_exhausted",
            status_code=last.status_code,
            uncertain=last.uncertain,
        )

    def get_bytes(self, service: str, path: str) -> bytes:
        return self._request(service, "GET", path)

    def get_json(self, service: str, path: str) -> object:
        data = self.get_bytes(service, path)
        try:
            return json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_json_object_no_duplicates,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPRequestFailure("malformed_json_response") from exc

    def post_json(
        self,
        service: str,
        path: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> object:
        data = _compact_json(payload)
        response = self._request(
            service,
            "POST",
            path,
            data,
            idempotency_key=idempotency_key,
        )
        try:
            return json.loads(
                response.decode("utf-8"),
                object_pairs_hook=_json_object_no_duplicates,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPRequestFailure("malformed_json_response", uncertain=True) from exc


@dataclass(frozen=True)
class EventEnvelope:
    repository_id: int
    repository_full_name: str
    issue_number: int
    actor_id: int
    action: str
    delivery_id: str
    title_body_sha256: str
    kind: str = "issues"

    @classmethod
    def from_mapping(cls, value: object) -> "EventEnvelope":
        if not isinstance(value, Mapping):
            raise AdmissionFailure("event_envelope_invalid")
        allowed = {
            "repository_id",
            "repo_id",
            "repository_full_name",
            "repo_full_name",
            "issue_number",
            "actor_id",
            "sender_id",
            "action",
            "delivery_id",
            "delivery",
            "title_body_sha256",
            "source_sha256",
            "kind",
            "schema_version",
            "repository",
            "issue",
        }
        if set(value) - allowed:
            raise AdmissionFailure("event_envelope_unknown_field")

        def one(names: Sequence[str], *, label: str, required: bool = True) -> object:
            found = [value[name] for name in names if name in value]
            if len(found) > 1:
                raise AdmissionFailure(f"event_{label}_ambiguous")
            if not found:
                if required:
                    raise AdmissionFailure(f"event_{label}_missing")
                return None
            return found[0]

        repository_id = one(("repository_id", "repo_id"), label="repository_id", required=False)
        repository_full_name = one(
            ("repository_full_name", "repo_full_name"),
            label="repository_full_name",
            required=False,
        )
        nested_repository = value.get("repository")
        if nested_repository is not None:
            if not isinstance(nested_repository, Mapping) or set(nested_repository) - {"id", "full_name"}:
                raise AdmissionFailure("event_repository_invalid")
            if repository_id is not None or repository_full_name is not None:
                raise AdmissionFailure("event_repository_ambiguous")
            repository_id = nested_repository.get("id")
            repository_full_name = nested_repository.get("full_name")
        issue_number = one(("issue_number",), label="issue_number", required=False)
        nested_issue = value.get("issue")
        if nested_issue is not None:
            if not isinstance(nested_issue, Mapping) or set(nested_issue) - {"number"}:
                raise AdmissionFailure("event_issue_invalid")
            if issue_number is not None:
                raise AdmissionFailure("event_issue_ambiguous")
            issue_number = nested_issue.get("number")
        actor_id = one(("actor_id", "sender_id"), label="actor_id")
        action = one(("action",), label="action")
        delivery_id = one(("delivery_id", "delivery"), label="delivery_id")
        event_hash = one(
            ("title_body_sha256", "source_sha256"),
            label="title_body_sha256",
        )
        kind = value.get("kind", "issues")
        schema_version = value.get("schema_version", 1)
        if schema_version != 1 or kind != "issues":
            raise AdmissionFailure("event_kind_invalid")
        if not _is_positive_int(repository_id):
            raise AdmissionFailure("event_repository_id_invalid")
        if not isinstance(repository_full_name, str) or REPOSITORY_NAME_RE.fullmatch(repository_full_name) is None:
            raise AdmissionFailure("event_repository_full_name_invalid")
        if not _is_positive_int(issue_number):
            raise AdmissionFailure("event_issue_number_invalid")
        if not _is_positive_int(actor_id):
            raise AdmissionFailure("event_actor_id_invalid")
        if not isinstance(action, str) or ACTION_RE.fullmatch(action) is None:
            raise AdmissionFailure("event_action_invalid")
        if (
            not isinstance(delivery_id, str)
            or not delivery_id
            or len(delivery_id.encode("utf-8")) > 256
        ):
            raise AdmissionFailure("event_delivery_id_invalid")
        if any(char in delivery_id for char in "\x00\r\n"):
            raise AdmissionFailure("event_delivery_id_invalid")
        if not isinstance(event_hash, str) or SHA256_RE.fullmatch(event_hash) is None:
            raise AdmissionFailure("event_source_hash_invalid")
        return cls(
            repository_id=repository_id,
            repository_full_name=repository_full_name,
            issue_number=issue_number,
            actor_id=actor_id,
            action=action,
            delivery_id=delivery_id,
            title_body_sha256=event_hash,
            kind="issues",
        )


@dataclass(frozen=True)
class CanonicalIssue:
    repository_id: int
    issue_number: int
    title: str
    body: str
    title_body_sha256: str
    author_id: int
    state: str
    labels: tuple[str, ...]
    is_pull_request: bool = False


@dataclass(frozen=True)
class PlanDocument:
    status: str
    name: str
    path: str
    content: bytes
    revision: str


@dataclass(frozen=True)
class PlanArtifact:
    path: str
    name: str
    revision: str
    content_sha256: str
    provenance: Mapping[str, object]


@dataclass(frozen=True)
class IntakeOutcome:
    status: str
    reason: str | None = None
    project_id: str | None = None
    plan_path: str | None = None
    run_id: str | None = None
    startup_question: Mapping[str, object] | None = None
    reused: bool = False
    question: str | None = None

    @property
    def exit_code(self) -> int:
        if self.status in {"started", "ignored"}:
            return 0
        if self.status == "transport_error":
            return 1
        return 2

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"status": self.status}
        for key, value in (
            ("reason", self.reason),
            ("project_id", self.project_id),
            ("plan_path", self.plan_path),
            ("run_id", self.run_id),
            ("startup_question", self.startup_question),
            ("question", self.question),
            ("reused", self.reused),
        ):
            if value is not None:
                result[key] = value
        return result


def _outcome_from_receipt(receipt: Mapping[str, object], *, reused: bool = False) -> IntakeOutcome:
    plan = receipt.get("plan")
    plan_path = plan.get("path") if isinstance(plan, Mapping) else None
    result = receipt.get("result")
    run_id = result.get("run_id") if isinstance(result, Mapping) else None
    question = result.get("startup_question") if isinstance(result, Mapping) else None
    planning = receipt.get("planning")
    planning_question = planning.get("question") if isinstance(planning, Mapping) else None
    return IntakeOutcome(
        status=str(receipt.get("state", "needs_attention")),
        reason=receipt.get("reason") if isinstance(receipt.get("reason"), str) else None,
        project_id=receipt.get("project_id") if isinstance(receipt.get("project_id"), str) else None,
        plan_path=plan_path if isinstance(plan_path, str) else None,
        run_id=run_id if isinstance(run_id, str) else None,
        startup_question=question if isinstance(question, Mapping) else None,
        question=planning_question if isinstance(planning_question, str) else None,
        reused=reused,
    )


def _receipt_base(envelope: EventEnvelope, mapping: RepositoryMapping, canonical: CanonicalIssue) -> dict[str, object]:
    claim_sha256 = _hash_claim(
        envelope.repository_id,
        envelope.issue_number,
        envelope.title_body_sha256,
    )
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "state": "claimed",
        "claim_accepted": True,
        "owner_id": OWNER_ID,
        "repository_id": envelope.repository_id,
        "repository_full_name": mapping.full_name,
        "issue_number": envelope.issue_number,
        "claim_sha256": claim_sha256,
        "source_sha256": envelope.title_body_sha256,
        "event": {
            "actor_id": envelope.actor_id,
            "action": envelope.action,
            "delivery_id": envelope.delivery_id,
            "source_sha256": envelope.title_body_sha256,
        },
        "canonical": {
            "author_id": canonical.author_id,
            "state": canonical.state,
            "is_pull_request": canonical.is_pull_request,
        },
        "project_id": mapping.project_id,
        "updated_at": _now(),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_accepted_claim(receipt: Mapping[str, object] | None) -> bool:
    return bool(receipt and receipt.get("claim_accepted") is True and receipt.get("claim_sha256"))


def _receipt_with_state(
    receipt: Mapping[str, object],
    state: str,
    *,
    reason: str | None = None,
) -> dict[str, object]:
    result = deepcopy(dict(receipt))
    result["state"] = state
    result["updated_at"] = _now()
    if reason is not None:
        result["reason"] = reason
    return result


def _parse_canonical_issue(
    repository_payload: object,
    issue_payload: object,
    *,
    mapping: RepositoryMapping,
    issue_number: int,
) -> CanonicalIssue:
    if not isinstance(repository_payload, Mapping):
        raise AttentionFailure("canonical_repository_invalid")
    repository_id = repository_payload.get("id")
    if not _is_positive_int(repository_id) or repository_id != mapping.repository_id:
        raise AttentionFailure("canonical_repository_identity_mismatch")
    full_name = repository_payload.get("full_name")
    if full_name is not None and full_name != mapping.full_name:
        raise AttentionFailure("canonical_repository_name_mismatch")
    if not isinstance(issue_payload, Mapping):
        raise AttentionFailure("canonical_issue_invalid")
    if "pull_request" in issue_payload:
        is_pull_request = True
    else:
        is_pull_request = False
    user = issue_payload.get("user")
    if not isinstance(user, Mapping) or not _is_positive_int(user.get("id")):
        raise AttentionFailure("canonical_author_invalid")
    author_id = user["id"]
    title = issue_payload.get("title")
    body = issue_payload.get("body")
    if not isinstance(title, str) or "\x00" in title:
        raise AttentionFailure("canonical_title_invalid")
    if body is not None and (not isinstance(body, str) or "\x00" in body):
        raise AttentionFailure("canonical_body_invalid")
    try:
        source = source_hash(title, body)
        encoded_title = title.encode("utf-8")
        encoded_body = (body or "").encode("utf-8")
    except (UnicodeError, ValueError) as exc:
        raise AttentionFailure("canonical_source_invalid") from exc
    if len(encoded_title) + len(encoded_body) > MAX_HTTP_INPUT_BYTES:
        raise AttentionFailure("canonical_source_too_large")
    state = issue_payload.get("state")
    if not isinstance(state, str) or state.casefold() not in {"open", "closed"}:
        raise AttentionFailure("canonical_state_invalid")
    labels_payload = issue_payload.get("labels", [])
    if not isinstance(labels_payload, list) or len(labels_payload) > 128:
        raise AttentionFailure("canonical_labels_invalid")
    labels: list[str] = []
    for item in labels_payload:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            raise AttentionFailure("canonical_labels_invalid")
        labels.append(item["name"])
    issue_repo = issue_payload.get("repository")
    if isinstance(issue_repo, Mapping) and issue_repo.get("id") is not None:
        if issue_repo.get("id") != mapping.repository_id:
            raise AttentionFailure("canonical_repository_identity_mismatch")
    response_number = issue_payload.get("number")
    if response_number is not None and response_number != issue_number:
        raise AttentionFailure("canonical_issue_number_mismatch")
    return CanonicalIssue(
        repository_id=repository_id,
        issue_number=issue_number,
        title=title,
        body=body or "",
        title_body_sha256=source,
        author_id=author_id,
        state=state.casefold(),
        labels=tuple(labels),
        is_pull_request=is_pull_request,
    )


def _api_path_for_repository(full_name: str) -> str:
    return f"/repos/{quote(full_name, safe='/')}"


def _fetch_canonical_issue(
    client: IntakeHTTPClient,
    mapping: RepositoryMapping,
    issue_number: int,
) -> CanonicalIssue:
    try:
        repository_payload = client.get_json(
            "github", _api_path_for_repository(mapping.full_name)
        )
        issue_payload = client.get_json(
            "github",
            f"{_api_path_for_repository(mapping.full_name)}/issues/{issue_number}",
        )
    except HTTPRequestFailure as exc:
        if exc.status_code == 404:
            raise AttentionFailure("canonical_issue_inaccessible") from exc
        raise TransportFailure("github_request_exhausted") from exc
    except (OSError, TimeoutError) as exc:
        raise TransportFailure("github_transport_error") from exc
    return _parse_canonical_issue(
        repository_payload,
        issue_payload,
        mapping=mapping,
        issue_number=issue_number,
    )


def _admission(envelope: EventEnvelope, canonical: CanonicalIssue, config: IntakeConfig) -> tuple[str, str]:
    if canonical.is_pull_request:
        return "ignored", "pull_request"
    if canonical.author_id != config.owner_id:
        return "ignored", "non_owner_author"
    if canonical.state != "open":
        return "ignored", "closed_issue"
    if any(label.casefold() in config.deferred_labels for label in canonical.labels):
        return "ignored", "deferred_issue"
    if envelope.actor_id != config.owner_id:
        if envelope.action in EXECUTABLE_ACTIONS:
            return "attention", "non_owner_event_actor"
        return "ignored", "non_owner_non_content_event"
    if envelope.action not in EXECUTABLE_ACTIONS:
        return "ignored", "non_content_event"
    if canonical.title_body_sha256 != envelope.title_body_sha256:
        return "attention", "unverified_source_revision"
    return "accepted", "accepted"


def _extract_markers(body: str) -> tuple[str | None, str | None]:
    plan_values: list[str] = []
    digest_values: list[str] = []
    plan_lines: list[int] = []
    digest_lines: list[int] = []
    for line_number, raw_line in enumerate(body.splitlines()):
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        plan_match = re.fullmatch(r"[ \t]*AFlow-Plan:[ \t]*(.*?)[ \t]*", line)
        digest_match = re.fullmatch(r"[ \t]*AFlow-Plan-SHA256:[ \t]*(.*?)[ \t]*", line)
        if plan_match:
            plan_values.append(plan_match.group(1))
            plan_lines.append(line_number)
        elif digest_match:
            digest_values.append(digest_match.group(1))
            digest_lines.append(line_number)
    if not plan_values and not digest_values:
        return None, None
    if len(plan_values) != 1:
        raise AttentionFailure("plan_marker_count_invalid")
    if len(digest_values) != 1 or SHA256_RE.fullmatch(digest_values[0]) is None:
        raise AttentionFailure("plan_digest_invalid")
    if abs(plan_lines[0] - digest_lines[0]) != 1:
        raise AttentionFailure("plan_digest_not_adjacent")
    marker = plan_values[0]
    if not marker or len(marker.encode("utf-8")) > 1024 or any(char.isspace() for char in marker):
        raise AttentionFailure("plan_marker_invalid")
    return marker, digest_values[0]


def _plan_path_parts(marker: str) -> tuple[str, str] | None:
    if marker.startswith("/") or "\\" in marker or "\x00" in marker:
        return None
    parts = marker.split("/")
    if len(parts) != 3 or parts[0] != "plans" or parts[1] not in {"todo", "in-progress"}:
        return None
    name = parts[2]
    if PLAN_NAME_RE.fullmatch(name) is None:
        return None
    return parts[1], name


def _document_path(project_id: str, status: str, name: str) -> str:
    return (
        f"/api/projects/{quote(project_id, safe='')}/plans/"
        f"{quote(status, safe='')}/{quote(name, safe='')}"
    )


def _status_directory_name(status: str) -> str:
    return "in-progress" if status == "in_progress" else status


def _document_from_payload(
    payload: object,
    *,
    expected_project_id: str | None = None,
    expected_status: str,
    expected_name: str,
) -> PlanDocument:
    if not isinstance(payload, Mapping):
        raise AttentionFailure("plan_response_invalid")
    if expected_project_id is not None and payload.get("project_id", expected_project_id) != expected_project_id:
        raise AttentionFailure("plan_project_identity_mismatch")
    content = payload.get("content")
    if not isinstance(content, str) or "\x00" in content:
        raise AttentionFailure("plan_content_invalid")
    try:
        content_bytes = content.encode("utf-8")
    except UnicodeError as exc:
        raise AttentionFailure("plan_content_invalid_utf8") from exc
    if len(content_bytes) > MAX_HTTP_INPUT_BYTES:
        raise AttentionFailure("plan_content_too_large")
    revision = payload.get("revision", _sha256_bytes(content_bytes))
    if not isinstance(revision, str) or SHA256_RE.fullmatch(revision) is None:
        raise AttentionFailure("plan_revision_invalid")
    if revision != _sha256_bytes(content_bytes):
        raise AttentionFailure("plan_revision_mismatch")
    status_value = payload.get("status", expected_status)
    name = payload.get("name", expected_name)
    path = payload.get("path", f"plans/{expected_status}/{expected_name}")
    expected_path = f"plans/{_status_directory_name(expected_status)}/{expected_name}"
    if status_value != expected_status or name != expected_name or path != expected_path:
        raise AttentionFailure("plan_identity_mismatch")
    return PlanDocument(
        status=expected_status,
        name=expected_name,
        path=path,
        content=content_bytes,
        revision=revision,
    )


def _get_plan(
    client: IntakeHTTPClient,
    project_id: str,
    status: str,
    name: str,
    *,
    optional: bool = False,
) -> PlanDocument | None:
    try:
        payload = client.get_json("private", _document_path(project_id, status, name))
    except HTTPRequestFailure as exc:
        if optional and exc.status_code == 404:
            return None
        if exc.status_code == 404:
            raise AttentionFailure("plan_not_found") from exc
        raise TransportFailure("private_api_request_exhausted") from exc
    except (OSError, TimeoutError) as exc:
        raise TransportFailure("private_api_transport_error") from exc
    return _document_from_payload(
        payload,
        expected_project_id=project_id,
        expected_status=status,
        expected_name=name,
    )


def _validate_unfinished_plan(content: bytes, *, source_name: str) -> None:
    _bounded_bytes(content, label="plan_content")
    try:
        text = content.decode("utf-8")
        parsed = parse_plan_text(text, source_path=Path(source_name))
    except (UnicodeError, PlanParseError) as exc:
        raise AttentionFailure("plan_structure_invalid") from exc
    if parsed.snapshot.is_complete or parsed.snapshot.current_checkpoint_unchecked_step_count <= 0:
        raise AttentionFailure("plan_has_no_unfinished_checkpoint")


def _external_blob_parts(marker: str, repository_full_name: str) -> tuple[str, str] | None:
    parsed = urlsplit(marker)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() != "github.com"
        or port is not None
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    pieces = [unquote(piece) for piece in parsed.path.strip("/").split("/")]
    if len(pieces) < 5 or pieces[2] != "blob":
        return None
    if f"{pieces[0]}/{pieces[1]}" != repository_full_name:
        return None
    commit_sha = pieces[3]
    if COMMIT_SHA_RE.fullmatch(commit_sha) is None:
        return None
    path_pieces = pieces[4:]
    if not path_pieces:
        return None
    decoded_path = "/".join(path_pieces)
    if any(part in {"", ".", ".."} for part in decoded_path.split("/")):
        return None
    if not decoded_path.casefold().endswith(".md") or len(decoded_path.encode("utf-8")) > 1024:
        return None
    if decoded_path.casefold() == "plans/done" or decoded_path.casefold().startswith("plans/done/"):
        return None
    return commit_sha, decoded_path


def _decode_contents_response(data: bytes) -> bytes:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _bounded_bytes(data, label="plan_blob")
    if not isinstance(parsed, Mapping) or "content" not in parsed:
        return _bounded_bytes(data, label="plan_blob")
    if parsed.get("encoding") != "base64" or not isinstance(parsed.get("content"), str):
        raise AttentionFailure("plan_blob_encoding_invalid")
    compact = "".join(parsed["content"].split())
    try:
        decoded = base64.b64decode(compact.encode("ascii"), validate=True)
    except (UnicodeError, binascii.Error) as exc:
        raise AttentionFailure("plan_blob_encoding_invalid") from exc
    return _bounded_bytes(decoded, label="plan_blob")


def _post_json(
    client: IntakeHTTPClient,
    service: str,
    path: str,
    payload: Mapping[str, object],
    *,
    idempotency_key: str | None = None,
) -> object:
    try:
        if idempotency_key is None:
            return client.post_json(service, path, payload)
        return client.post_json(
            service,
            path,
            payload,
            idempotency_key=idempotency_key,
        )
    except HTTPRequestFailure as exc:
        raise exc
    except (OSError, TimeoutError) as exc:
        raise HTTPRequestFailure("transport_error", uncertain=True) from exc


def _import_plan(
    client: IntakeHTTPClient,
    *,
    project_id: str,
    issue_number: int,
    claim_sha256: str,
    content: bytes,
    provenance: Mapping[str, object],
) -> PlanArtifact:
    name = f"issue-{issue_number}-{claim_sha256[:12]}.md"
    if len(name) > MAX_PLAN_NAME_LENGTH or PLAN_NAME_RE.fullmatch(name) is None:
        raise AttentionFailure("import_plan_name_invalid")
    text = content.decode("utf-8")
    todo = _get_plan(client, project_id, "todo", name, optional=True)
    in_progress = _get_plan(client, project_id, "in_progress", name, optional=True)
    expected_revision = _sha256_bytes(content)
    if in_progress is not None:
        if in_progress.content != content or in_progress.revision != expected_revision:
            raise AttentionFailure("import_destination_conflict")
        _validate_unfinished_plan(in_progress.content, source_name=name)
        return PlanArtifact(
            path=in_progress.path,
            name=name,
            revision=in_progress.revision,
            content_sha256=expected_revision,
            provenance=provenance,
        )
    if todo is not None:
        if todo.content != content or todo.revision != expected_revision:
            raise AttentionFailure("import_destination_conflict")
    else:
        try:
            response = _post_json(
                client,
                "private",
                "/api/projects/" + quote(project_id, safe="") + "/plans",
                {"name": name, "content": text},
            )
            if isinstance(response, Mapping):
                returned_content = response.get("content")
                if returned_content is not None and returned_content != text:
                    raise AttentionFailure("import_response_content_mismatch")
        except AttentionFailure:
            raise
        except HTTPRequestFailure as first:
            todo = _get_plan(client, project_id, "todo", name, optional=True)
            if todo is None:
                if first.status_code is not None and first.status_code not in TRANSIENT_HTTP_STATUSES:
                    raise AttentionFailure("import_destination_conflict") from first
                raise TransportFailure("private_api_request_exhausted") from first
            if todo is None:
                raise TransportFailure("private_api_request_exhausted") from first
        todo = _get_plan(client, project_id, "todo", name, optional=False)
    assert todo is not None
    if todo.content != content or todo.revision != expected_revision:
        raise AttentionFailure("import_content_mismatch")
    _validate_unfinished_plan(todo.content, source_name=name)
    promote_path = _document_path(project_id, "todo", name) + "/promote"
    promote_payload = {"expected_revision": todo.revision}
    try:
        _post_json(client, "private", promote_path, promote_payload)
    except HTTPRequestFailure as first:
        source_after = _get_plan(client, project_id, "todo", name, optional=True)
        target_after = _get_plan(client, project_id, "in_progress", name, optional=True)
        if target_after is not None:
            if target_after.content != content or target_after.revision != expected_revision:
                raise AttentionFailure("promotion_destination_conflict") from first
            return PlanArtifact(
                path=target_after.path,
                name=name,
                revision=target_after.revision,
                content_sha256=expected_revision,
                provenance=provenance,
            )
        if source_after is None or source_after.content != content or source_after.revision != expected_revision:
            raise AttentionFailure("promotion_conflict") from first
        if first.status_code is not None and first.status_code not in TRANSIENT_HTTP_STATUSES:
            raise AttentionFailure("promotion_conflict") from first
        raise TransportFailure("private_api_request_exhausted") from first
    target = _get_plan(client, project_id, "in_progress", name, optional=False)
    assert target is not None
    if target.content != content or target.revision != expected_revision:
        raise AttentionFailure("promotion_content_mismatch")
    return PlanArtifact(
        path=target.path,
        name=name,
        revision=target.revision,
        content_sha256=expected_revision,
        provenance=provenance,
    )


def _resolve_attached_plan(
    client: IntakeHTTPClient,
    *,
    config: IntakeConfig,
    mapping: RepositoryMapping,
    canonical: CanonicalIssue,
    claim_sha256: str,
) -> PlanArtifact | None:
    marker, digest = _extract_markers(canonical.body)
    if marker is None:
        return None
    assert digest is not None
    local = _plan_path_parts(marker)
    if local is not None:
        status, name = local
        document = _get_plan(client, mapping.project_id, "in_progress" if status == "in-progress" else "todo", name)
        assert document is not None
        if _sha256_bytes(document.content) != digest:
            raise AttentionFailure("plan_digest_mismatch")
        _validate_unfinished_plan(document.content, source_name=name)
        provenance = {
            "kind": "same_project",
            "marker": marker,
            "source_path": document.path,
            "source_revision": document.revision,
            "content_sha256": digest,
        }
        if status == "in-progress":
            return PlanArtifact(
                path=document.path,
                name=name,
                revision=document.revision,
                content_sha256=digest,
                provenance=provenance,
            )
        return _import_plan(
            client,
            project_id=mapping.project_id,
            issue_number=canonical.issue_number,
            claim_sha256=claim_sha256,
            content=document.content,
            provenance=provenance,
        )

    external = _external_blob_parts(marker, mapping.full_name)
    if external is None:
        raise AttentionFailure("plan_origin_untrusted")
    commit_sha, blob_path = external
    contents_path = (
        f"{_api_path_for_repository(mapping.full_name)}/contents/"
        f"{quote(blob_path, safe='/')}?{urlencode({'ref': commit_sha})}"
    )
    try:
        blob_payload = client.get_bytes("github", contents_path)
    except HTTPRequestFailure as exc:
        if exc.status_code == 404:
            raise AttentionFailure("plan_reference_inaccessible") from exc
        raise TransportFailure("github_request_exhausted") from exc
    except (OSError, TimeoutError) as exc:
        raise TransportFailure("github_transport_error") from exc
    content = _decode_contents_response(blob_payload)
    if _sha256_bytes(content) != digest:
        raise AttentionFailure("plan_digest_mismatch")
    name = Path(blob_path).name
    if PLAN_NAME_RE.fullmatch(name) is None:
        raise AttentionFailure("plan_name_invalid")
    _validate_unfinished_plan(content, source_name=name)
    provenance = {
        "kind": "github_blob",
        "marker": marker,
        "repository": mapping.full_name,
        "commit_sha": commit_sha,
        "path": blob_path,
        "content_sha256": digest,
    }
    return _import_plan(
        client,
        project_id=mapping.project_id,
        issue_number=canonical.issue_number,
        claim_sha256=claim_sha256,
        content=content,
        provenance=provenance,
    )


def _artifact_from_receipt(
    client: IntakeHTTPClient,
    receipt: Mapping[str, object],
) -> PlanArtifact:
    plan = receipt.get("plan")
    if not isinstance(plan, Mapping):
        raise AttentionFailure("plan_receipt_missing")
    project_id = receipt.get("project_id")
    path = plan.get("path")
    revision = plan.get("revision")
    content_sha256 = plan.get("content_sha256", revision)
    if not isinstance(project_id, str) or not isinstance(path, str):
        raise AttentionFailure("plan_receipt_invalid")
    if not isinstance(revision, str) or SHA256_RE.fullmatch(revision) is None:
        raise AttentionFailure("plan_receipt_invalid")
    if not isinstance(content_sha256, str) or SHA256_RE.fullmatch(content_sha256) is None:
        raise AttentionFailure("plan_receipt_invalid")
    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "plans" or parts[1] not in {"todo", "in-progress"} or PLAN_NAME_RE.fullmatch(parts[2]) is None:
        raise AttentionFailure("plan_receipt_invalid")
    document = _get_plan(client, project_id, "in_progress" if parts[1] == "in-progress" else "todo", parts[2])
    assert document is not None
    if document.revision != revision or _sha256_bytes(document.content) != content_sha256:
        raise AttentionFailure("plan_revision_changed")
    _validate_unfinished_plan(document.content, source_name=parts[2])
    provenance = plan.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {"kind": "verified_intake_artifact"}
    return PlanArtifact(
        path=document.path,
        name=parts[2],
        revision=document.revision,
        content_sha256=content_sha256,
        provenance=provenance,
    )


def _verify_project_and_capabilities(
    client: IntakeHTTPClient,
    config: IntakeConfig,
    mapping: RepositoryMapping,
) -> None:
    try:
        projects_payload = client.get_json("private", "/api/control-plane/projects")
        capabilities_payload = client.get_json(
            "private",
            f"/api/control-plane/projects/{quote(mapping.project_id, safe='')}/capabilities",
        )
    except HTTPRequestFailure as exc:
        if exc.status_code in {403, 404}:
            raise AttentionFailure("project_registry_unavailable") from exc
        raise TransportFailure("private_api_request_exhausted") from exc
    except (OSError, TimeoutError) as exc:
        raise TransportFailure("private_api_transport_error") from exc
    projects: object = projects_payload
    if isinstance(projects_payload, Mapping):
        projects = projects_payload.get("projects")
    if not isinstance(projects, list):
        raise AttentionFailure("project_registry_response_invalid")
    matching = [
        item
        for item in projects
        if isinstance(item, Mapping)
        and item.get("project_id", item.get("id")) == mapping.project_id
    ]
    if len(matching) != 1:
        raise AttentionFailure("project_registry_identity_mismatch")
    if not isinstance(capabilities_payload, Mapping):
        raise AttentionFailure("project_capabilities_invalid")
    workflows = capabilities_payload.get("workflows")
    teams = capabilities_payload.get("teams")
    if not isinstance(workflows, (list, tuple)) or config.workflow_name not in workflows:
        raise AttentionFailure("workflow_not_configured")
    if not isinstance(teams, (list, tuple)) or config.team not in teams:
        raise AttentionFailure("team_not_configured")
    details = capabilities_payload.get("workflow_details")
    if isinstance(details, Mapping) and config.workflow_name in details:
        detail = details[config.workflow_name]
        if isinstance(detail, Mapping):
            executable = detail.get("executable_steps")
            excluded = detail.get("excluded_steps", ())
            if (
                not isinstance(executable, (list, tuple))
                or not executable
                or not isinstance(excluded, (list, tuple))
            ):
                raise AttentionFailure("workflow_has_no_executable_steps")
            if config.start_step is not None and (
                config.start_step not in executable or config.start_step in excluded
            ):
                raise AttentionFailure("start_step_not_configured")
            for key, expected in (
                ("lifecycle_setup", ("worktree", "branch")),
                ("setup", ("worktree", "branch")),
            ):
                lifecycle = detail.get(key)
                if lifecycle is not None and tuple(lifecycle) != expected:
                    raise AttentionFailure("workflow_lifecycle_mismatch")
            for key, expected in (
                ("lifecycle_teardown", ("merge", "rm_worktree")),
                ("teardown", ("merge", "rm_worktree")),
            ):
                lifecycle = detail.get(key)
                if lifecycle is not None and tuple(lifecycle) != expected:
                    raise AttentionFailure("workflow_lifecycle_mismatch")


def _preflight(
    client: IntakeHTTPClient,
    config: IntakeConfig,
    mapping: RepositoryMapping,
    launch: Mapping[str, object],
) -> None:
    payload = {key: value for key, value in launch.items() if key != "idempotency_key"}
    payload.update({"offset": 0, "limit": 1})
    try:
        response = client.post_json(
            "private",
            f"/api/control-plane/projects/{quote(mapping.project_id, safe='')}/runs/preflight",
            payload,
        )
    except HTTPRequestFailure as exc:
        if exc.status_code in {400, 403, 404, 409, 422}:
            raise AttentionFailure("preflight_rejected") from exc
        raise TransportFailure("private_api_request_exhausted") from exc
    except (OSError, TimeoutError) as exc:
        raise TransportFailure("private_api_transport_error") from exc
    if not isinstance(response, Mapping):
        raise AttentionFailure("preflight_response_invalid")
    blockers = response.get("blockers", ())
    dirty = response.get("dirty")
    requires_confirmation = response.get("requires_confirmation")
    execution_mode = response.get("execution_mode")
    if (
        dirty is not False
        or requires_confirmation is not False
        or not isinstance(blockers, (list, tuple))
        or blockers
    ):
        raise AttentionFailure("preflight_blocked")
    if execution_mode != "new_worktree":
        raise AttentionFailure("managed_worktree_lifecycle_unresolved")


def _question_record(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise AttentionFailure("startup_question_invalid")
    question_id = value.get("question_id")
    kind = value.get("kind")
    message = value.get("message")
    if not isinstance(question_id, str) or not RUN_ID_RE.fullmatch(question_id):
        raise AttentionFailure("startup_question_invalid")
    if not isinstance(kind, str) or not kind or len(kind) > 128:
        raise AttentionFailure("startup_question_invalid")
    if not isinstance(message, str) or len(message.encode("utf-8")) > MAX_RECEIPT_TEXT_BYTES:
        raise AttentionFailure("startup_question_invalid")
    choices = value.get("choices", ())
    if not isinstance(choices, (list, tuple)) or len(choices) > 64 or not all(
        isinstance(item, str) and len(item.encode("utf-8")) <= 256 for item in choices
    ):
        raise AttentionFailure("startup_question_invalid")
    options = value.get("options", {})
    if not isinstance(options, Mapping) or len(options) > 64:
        raise AttentionFailure("startup_question_invalid")
    clean_options: dict[str, str] = {}
    for key, option in options.items():
        if not isinstance(key, str) or not isinstance(option, str) or len(option.encode("utf-8")) > 512:
            raise AttentionFailure("startup_question_invalid")
        clean_options[key] = option
    run_id = value.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None):
        raise AttentionFailure("startup_question_invalid")
    return {
        "question_id": question_id,
        "kind": kind,
        "message": message,
        "choices": list(choices),
        "options": clean_options,
        "run_id": run_id,
    }


def _start_result(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise AttentionFailure("start_response_invalid")
    result = value.get("result")
    question = value.get("startup_question")
    if (result is None) == (question is None):
        raise AttentionFailure("start_response_invalid")
    if question is not None:
        return {"startup_question": _question_record(question)}
    if not isinstance(result, Mapping):
        raise AttentionFailure("start_response_invalid")
    run_id = result.get("run_id")
    status = result.get("status")
    created = result.get("created")
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise AttentionFailure("start_response_invalid")
    if not isinstance(status, str) or not status or len(status) > 128:
        raise AttentionFailure("start_response_invalid")
    if not isinstance(created, bool):
        raise AttentionFailure("start_response_invalid")
    return {"run_id": run_id, "status": status, "created": created}


class IssueIntakeRunner:
    """Execute one envelope while holding its per-issue receipt lock."""

    def __init__(
        self,
        config: IntakeConfig,
        *,
        http_client: IntakeHTTPClient,
        store: IssueIntakeStore | None = None,
        planner: Any | None = None,
    ) -> None:
        self.config = config
        self.client = http_client
        self.store = store or IssueIntakeStore(config.state_root)
        self._planner_instance = planner

    def _write(self, envelope: EventEnvelope, payload: Mapping[str, object]) -> None:
        self.store.write(envelope.repository_id, envelope.issue_number, payload)

    def _admission_outcome(
        self,
        envelope: EventEnvelope,
        mapping: RepositoryMapping,
        canonical: CanonicalIssue,
        existing: Mapping[str, object] | None,
        kind: str,
        reason: str,
    ) -> IntakeOutcome:
        if _is_accepted_claim(existing):
            if kind == "attention":
                updated = _receipt_with_state(existing, "needs_attention", reason=reason)
                updated["pending_event"] = {
                    "actor_id": envelope.actor_id,
                    "action": envelope.action,
                    "delivery_id": envelope.delivery_id,
                    "source_sha256": envelope.title_body_sha256,
                }
                self._write(envelope, updated)
                return _outcome_from_receipt(updated)
            return IntakeOutcome("ignored", reason, mapping.project_id, reused=True)
        if kind == "ignored":
            receipt: dict[str, object] = {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "state": "ignored",
                "claim_accepted": False,
                "owner_id": OWNER_ID,
                "repository_id": envelope.repository_id,
                "repository_full_name": mapping.full_name,
                "issue_number": envelope.issue_number,
                "source_sha256": canonical.title_body_sha256,
                "event": {
                    "actor_id": envelope.actor_id,
                    "action": envelope.action,
                    "delivery_id": envelope.delivery_id,
                    "source_sha256": envelope.title_body_sha256,
                },
                "reason": reason,
                "updated_at": _now(),
            }
            self._write(envelope, receipt)
            return _outcome_from_receipt(receipt)
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "state": "needs_attention",
            "claim_accepted": False,
            "owner_id": OWNER_ID,
            "repository_id": envelope.repository_id,
            "repository_full_name": mapping.full_name,
            "issue_number": envelope.issue_number,
            "source_sha256": canonical.title_body_sha256,
            "event": {
                "actor_id": envelope.actor_id,
                "action": envelope.action,
                "delivery_id": envelope.delivery_id,
                "source_sha256": envelope.title_body_sha256,
            },
            "reason": reason,
            "updated_at": _now(),
        }
        self._write(envelope, receipt)
        return _outcome_from_receipt(receipt)

    def _boundary_ok(
        self,
        envelope: EventEnvelope,
        mapping: RepositoryMapping,
    ) -> CanonicalIssue:
        canonical = _fetch_canonical_issue(self.client, mapping, envelope.issue_number)
        kind, reason = _admission(envelope, canonical, self.config)
        if kind != "accepted":
            raise AttentionFailure(
                "source_boundary_changed" if reason == "unverified_source_revision" else f"source_boundary_{reason}"
            )
        return canonical

    def _planner(self) -> Any:
        if self._planner_instance is None:
            from .issue_intake_planner import IssueIntakePlanner

            self._planner_instance = IssueIntakePlanner(
                client=self.client,
                state_root=self.config.state_root,
            )
        return self._planner_instance

    def _plan_record(
        self,
        artifact: PlanArtifact,
    ) -> dict[str, object]:
        return {
            "path": artifact.path,
            "name": artifact.name,
            "revision": artifact.revision,
            "content_sha256": artifact.content_sha256,
            "provenance": dict(artifact.provenance),
        }

    def _save_planner_attention(
        self,
        envelope: EventEnvelope,
        receipt: Mapping[str, object],
        *,
        reason: str,
        question: str | None = None,
    ) -> IntakeOutcome:
        current = self.store.read(envelope.repository_id, envelope.issue_number)
        source = current if current is not None else receipt
        updated = _receipt_with_state(source, "needs_attention", reason=reason)
        if question is not None:
            planning = updated.get("planning")
            planning_record = dict(planning) if isinstance(planning, Mapping) else {}
            planning_record["question"] = question
            updated["planning"] = planning_record
        self._write(envelope, updated)
        return _outcome_from_receipt(updated)

    def _author_absent_plan(
        self,
        envelope: EventEnvelope,
        mapping: RepositoryMapping,
        receipt: Mapping[str, object],
        canonical: CanonicalIssue,
    ) -> tuple[Mapping[str, object], PlanArtifact] | IntakeOutcome:
        from .issue_intake_planner import PlannerFailure

        receipt_holder: list[Mapping[str, object]] = [receipt]

        def persist_workspace(record: Mapping[str, object]) -> None:
            if not isinstance(record, Mapping):
                raise PlannerFailure("planner_workspace_invalid")
            updated = _receipt_with_state(receipt_holder[0], "planning")
            updated["planning"] = dict(record)
            self._write(envelope, updated)
            receipt_holder[0] = updated

        try:
            result = self._planner().plan(
                repository_id=envelope.repository_id,
                repository_full_name=mapping.full_name,
                issue_number=envelope.issue_number,
                project_id=mapping.project_id,
                claim_sha256=str(receipt["claim_sha256"]),
                canonical=canonical,
                persist_workspace=persist_workspace,
            )
        except PlannerFailure as exc:
            return self._save_planner_attention(
                envelope,
                receipt_holder[0],
                reason=exc.reason,
            )
        status = getattr(result, "status", None)
        if not isinstance(status, str):
            return self._save_planner_attention(
                envelope,
                receipt_holder[0],
                reason="planner_result_invalid",
            )
        if status == "needs_attention":
            question = getattr(result, "question", None)
            if not isinstance(question, str) or not question.strip():
                return self._save_planner_attention(
                    envelope,
                    receipt_holder[0],
                    reason="planner_result_invalid",
                )
            return self._save_planner_attention(
                envelope,
                receipt_holder[0],
                reason="planner_question",
                question=question,
            )
        markdown = getattr(result, "markdown", None)
        if status != "plan" or not isinstance(markdown, str):
            return self._save_planner_attention(
                envelope,
                receipt_holder[0],
                reason="planner_result_invalid",
            )
        try:
            content = markdown.encode("utf-8")
        except UnicodeError:
            return self._save_planner_attention(
                envelope,
                receipt_holder[0],
                reason="planner_result_invalid_utf8",
            )
        try:
            artifact = _import_plan(
                self.client,
                project_id=mapping.project_id,
                issue_number=envelope.issue_number,
                claim_sha256=str(receipt["claim_sha256"]),
                content=content,
                provenance=(
                    getattr(result, "provenance", None)
                    if isinstance(getattr(result, "provenance", None), Mapping)
                    else {"kind": "codex_planner"}
                ),
            )
        except AttentionFailure as exc:
            return self._save_planner_attention(
                envelope,
                receipt_holder[0],
                reason=exc.reason,
            )
        updated = _receipt_with_state(receipt_holder[0], "plan_ready")
        updated["plan"] = self._plan_record(artifact)
        self._write(envelope, updated)
        return updated, artifact

    def _recover_absent_plan(
        self,
        envelope: EventEnvelope,
        mapping: RepositoryMapping,
        receipt: Mapping[str, object],
    ) -> tuple[Mapping[str, object], PlanArtifact] | IntakeOutcome:
        from .issue_intake_planner import PlannerFailure

        try:
            result = self._planner().recover(
                record=receipt.get("planning"),
                repository_id=envelope.repository_id,
                issue_number=envelope.issue_number,
                project_id=mapping.project_id,
                claim_sha256=str(receipt["claim_sha256"]),
            )
        except PlannerFailure as exc:
            return self._save_planner_attention(
                envelope,
                receipt,
                reason=exc.reason,
            )
        status = getattr(result, "status", None)
        if not isinstance(status, str):
            return self._save_planner_attention(
                envelope,
                receipt,
                reason="planner_result_invalid",
            )
        if status == "needs_attention":
            question = getattr(result, "question", None)
            if not isinstance(question, str) or not question.strip():
                return self._save_planner_attention(
                    envelope,
                    receipt,
                    reason="planner_result_invalid",
                )
            return self._save_planner_attention(
                envelope,
                receipt,
                reason="planner_question",
                question=question,
            )
        markdown = getattr(result, "markdown", None)
        if status != "plan" or not isinstance(markdown, str):
            return self._save_planner_attention(
                envelope,
                receipt,
                reason="planner_result_invalid",
            )
        try:
            artifact = _import_plan(
                self.client,
                project_id=mapping.project_id,
                issue_number=envelope.issue_number,
                claim_sha256=str(receipt["claim_sha256"]),
                content=markdown.encode("utf-8"),
                provenance=(
                    getattr(result, "provenance", None)
                    if isinstance(getattr(result, "provenance", None), Mapping)
                    else {"kind": "codex_planner", "recovered": True}
                ),
            )
        except (AttentionFailure, UnicodeError) as exc:
            reason = exc.reason if isinstance(exc, AttentionFailure) else "planner_result_invalid_utf8"
            return self._save_planner_attention(
                envelope,
                receipt,
                reason=reason,
            )
        updated = _receipt_with_state(receipt, "plan_ready")
        updated["plan"] = self._plan_record(artifact)
        self._write(envelope, updated)
        return updated, artifact

    def _launch_payload(self, mapping: RepositoryMapping, artifact: PlanArtifact) -> dict[str, object]:
        return {
            "plan_path": artifact.path,
            "workflow_name": self.config.workflow_name,
            "team": self.config.team,
            "start_step": self.config.start_step,
            "max_turns": self.config.max_turns,
            "extra_instructions": [],
            "restarted_from_run_id": None,
            "dirty_worktree_confirmed": False,
        }

    def _dispatch(
        self,
        envelope: EventEnvelope,
        mapping: RepositoryMapping,
        receipt: Mapping[str, object],
        artifact: PlanArtifact,
    ) -> IntakeOutcome:
        launch = receipt.get("launch")
        if launch is None:
            launch = self._launch_payload(mapping, artifact)
            start_key = _hash_start_key(
                envelope.repository_id,
                envelope.issue_number,
                str(receipt["claim_sha256"]),
                mapping.project_id,
                artifact.path,
                artifact.revision,
            )
            launch = dict(launch)
            launch["idempotency_key"] = start_key
            updated = _receipt_with_state(receipt, "dispatching")
            updated["project_id"] = mapping.project_id
            updated["plan"] = {
                "path": artifact.path,
                "name": artifact.name,
                "revision": artifact.revision,
                "content_sha256": artifact.content_sha256,
                "provenance": dict(artifact.provenance),
            }
            updated["launch"] = launch
            self._write(envelope, updated)
            receipt = updated
        elif not isinstance(launch, Mapping):
            raise AttentionFailure("launch_receipt_invalid")
        else:
            expected = self._launch_payload(mapping, artifact)
            if set(launch) != set(expected) | {"idempotency_key"}:
                raise AttentionFailure("launch_receipt_invalid")
            for key, value in expected.items():
                if launch.get(key) != value:
                    raise AttentionFailure("launch_receipt_mismatch")
            if not isinstance(launch.get("idempotency_key"), str) or SHA256_RE.fullmatch(launch["idempotency_key"]) is None:
                raise AttentionFailure("launch_key_invalid")
            expected_key = _hash_start_key(
                envelope.repository_id,
                envelope.issue_number,
                str(receipt["claim_sha256"]),
                mapping.project_id,
                artifact.path,
                artifact.revision,
            )
            if launch["idempotency_key"] != expected_key:
                raise AttentionFailure("launch_key_mismatch")

        _preflight(self.client, self.config, mapping, launch)
        recorded = _artifact_from_receipt(self.client, receipt)
        if recorded.path != artifact.path or recorded.revision != artifact.revision:
            raise AttentionFailure("plan_changed_before_start")
        self._boundary_ok(envelope, mapping)
        start_path = f"/api/control-plane/projects/{quote(mapping.project_id, safe='')}/runs"
        try:
            response = _post_json(
                self.client,
                "private",
                start_path,
                {key: value for key, value in launch.items() if key != "idempotency_key"},
                idempotency_key=str(launch["idempotency_key"]),
            )
        except HTTPRequestFailure as first:
            if first.status_code is not None and first.status_code not in TRANSIENT_HTTP_STATUSES:
                raise AttentionFailure("start_rejected") from first
            updated = _receipt_with_state(receipt, "needs_attention", reason="start_result_uncertain")
            self._write(envelope, updated)
            raise AttentionFailure("start_result_uncertain") from first
        result = _start_result(response)
        updated = _receipt_with_state(receipt, "started")
        updated["result"] = result
        self._write(envelope, updated)
        return _outcome_from_receipt(updated)

    def process(self, envelope: EventEnvelope) -> IntakeOutcome:
        mapping = self.config.mapping_for(
            envelope.repository_id,
            envelope.repository_full_name,
        )
        if mapping is None:
            return IntakeOutcome("needs_attention", "repository_not_configured")
        with self.store.issue_lock(envelope.repository_id, envelope.issue_number):
            existing = self.store.read(envelope.repository_id, envelope.issue_number)
            canonical = _fetch_canonical_issue(
                self.client,
                mapping,
                envelope.issue_number,
            )
            kind, reason = _admission(envelope, canonical, self.config)
            if kind != "accepted":
                return self._admission_outcome(
                    envelope,
                    mapping,
                    canonical,
                    existing,
                    kind,
                    reason,
                )
            if _is_accepted_claim(existing):
                if existing.get("source_sha256") != envelope.title_body_sha256:
                    updated = _receipt_with_state(
                        existing,
                        "needs_attention",
                        reason="source_revision_requires_reconciliation",
                    )
                    updated["pending_source_sha256"] = envelope.title_body_sha256
                    updated["pending_event"] = {
                        "actor_id": envelope.actor_id,
                        "action": envelope.action,
                        "delivery_id": envelope.delivery_id,
                        "source_sha256": envelope.title_body_sha256,
                    }
                    self._write(envelope, updated)
                    return _outcome_from_receipt(updated)
                if existing.get("state") == "started":
                    return _outcome_from_receipt(existing, reused=True)
                if existing.get("state") == "needs_attention":
                    return _outcome_from_receipt(existing)
                receipt: Mapping[str, object] = existing
            else:
                receipt = _receipt_base(envelope, mapping, canonical)
                self._write(envelope, receipt)

            try:
                _verify_project_and_capabilities(self.client, self.config, mapping)
                if receipt.get("project_id", mapping.project_id) != mapping.project_id:
                    raise AttentionFailure("project_receipt_identity_mismatch")
                if receipt.get("state") == "planning":
                    recovered = self._recover_absent_plan(envelope, mapping, receipt)
                    if isinstance(recovered, IntakeOutcome):
                        return recovered
                    receipt, artifact = recovered
                elif receipt.get("plan") is not None:
                    artifact = _artifact_from_receipt(self.client, receipt)
                else:
                    boundary = self._boundary_ok(envelope, mapping)
                    artifact = _resolve_attached_plan(
                        self.client,
                        config=self.config,
                        mapping=mapping,
                        canonical=boundary,
                        claim_sha256=str(receipt["claim_sha256"]),
                    )
                    if artifact is None:
                        planning_boundary = self._boundary_ok(envelope, mapping)
                        authored = self._author_absent_plan(
                            envelope,
                            mapping,
                            receipt,
                            planning_boundary,
                        )
                        if isinstance(authored, IntakeOutcome):
                            return authored
                        receipt, artifact = authored
                    else:
                        updated = _receipt_with_state(receipt, "plan_ready")
                        updated["plan"] = self._plan_record(artifact)
                        self._write(envelope, updated)
                        receipt = updated
                return self._dispatch(envelope, mapping, receipt, artifact)
            except AttentionFailure as exc:
                current = self.store.read(envelope.repository_id, envelope.issue_number)
                if current is None:
                    current = receipt
                updated = _receipt_with_state(current, "needs_attention", reason=exc.reason)
                self._write(envelope, updated)
                return _outcome_from_receipt(updated)


def read_event(path_value: str, *, stdin: Any = None) -> EventEnvelope:
    if path_value == "-":
        stream = stdin if stdin is not None else sys.stdin
        source = getattr(stream, "buffer", stream)
        data = source.read(MAX_EVENT_BYTES + 1)
        if isinstance(data, str):
            data = data.encode("utf-8")
    else:
        data = _read_bounded_file(Path(path_value), limit=MAX_EVENT_BYTES, label="event")
    if len(data) > MAX_EVENT_BYTES:
        raise AdmissionFailure("event_too_large")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_json_object_no_duplicates,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise AdmissionFailure("event_invalid_json") from exc
    return EventEnvelope.from_mapping(value)


def _json_output(outcome: IntakeOutcome) -> str:
    encoded = json.dumps(
        outcome.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise IntakeFailure("output_too_large")
    return encoded.decode("utf-8")


def _failure_outcome(exc: IntakeFailure) -> IntakeOutcome:
    if isinstance(exc, TransportFailure):
        return IntakeOutcome("transport_error", exc.reason)
    return IntakeOutcome("needs_attention", exc.reason)


def run_command(
    config_path: Path,
    event_path: str,
    *,
    http_client: IntakeHTTPClient | None = None,
    stdin: Any = None,
) -> IntakeOutcome:
    if not config_path.is_absolute():
        raise ConfigFailure("config_must_be_absolute")
    config = load_config(config_path)
    envelope = read_event(event_path, stdin=stdin)
    if not config.enabled:
        return IntakeOutcome("ignored", "disabled")
    if http_client is None:
        private_token = _read_credential(
            config.private_credential_file,
            label="private_credential",
        )
        github_token = _read_credential(
            config.github_credential_file,
            label="github_credential",
        )
        http_client = FixedOriginHTTPClient(
            private_api_url=config.private_api_url,
            private_token=private_token,
            github_api_url=config.github_api_url,
            github_token=github_token,
        )
    runner = IssueIntakeRunner(config, http_client=http_client)
    try:
        return runner.process(envelope)
    except IntakeFailure:
        raise
    except IntakeStoreError as exc:
        raise ConfigFailure("receipt_store_unavailable") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Admit one owner-authored GitHub issue to AFlow")
    parser.add_argument("--config", required=True, help="absolute host intake TOML")
    parser.add_argument("--event-file", required=True, help="relay envelope path, or - for stdin")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    http_client: IntakeHTTPClient | None = None,
    stdin: Any = None,
    stdout: Any = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output = stdout if stdout is not None else sys.stdout
    try:
        outcome = run_command(
            Path(args.config),
            args.event_file,
            http_client=http_client,
            stdin=stdin,
        )
    except IntakeFailure as exc:
        outcome = _failure_outcome(exc)
    except (OSError, ValueError) as exc:
        outcome = IntakeOutcome("needs_attention", "intake_failed")
        del exc
    print(_json_output(outcome), file=output)
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "OWNER_ID",
    "CanonicalIssue",
    "EventEnvelope",
    "FixedOriginHTTPClient",
    "HTTPRequestFailure",
    "IntakeConfig",
    "IntakeOutcome",
    "IntakeHTTPClient",
    "IssueIntakeRunner",
    "RepositoryMapping",
    "load_config",
    "main",
    "read_event",
    "run_command",
    "source_hash",
]
