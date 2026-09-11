"""Shared validation for persisted durable provider-recovery operation state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


RECOVERY_RUNTIME_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "source_run_id",
        "target_run_id",
        "source_selector",
        "target_selector",
        "intent_digest",
        "brief_sha256",
        "source_session_context_transferred",
        "consumed",
        "operation_state",
    }
)
RECOVERY_OPERATION_STATES = frozenset({"pending", "in_flight", "consumed"})
RECOVERY_OPERATION_UNRESOLVED_MESSAGE = (
    "Durable recovery has unknown liveness or prior turn evidence; "
    "reconcile the operation before retrying."
)


class RecoveryRuntimeValidationError(ValueError):
    """A persisted recovery operation cannot establish safe liveness."""


def validate_recovery_runtime(
    raw_runtime: object,
    *,
    expected_source_run_id: str | None = None,
    expected_target_run_id: str | None = None,
    expected_intent: Any | None = None,
    expected_intent_digest: str | None = None,
    allow_pending: bool = True,
) -> tuple[str, bool]:
    """Validate the canonical persisted one-shot recovery operation marker.

    ``allow_pending`` is deliberately caller-owned. Only the initial explicit
    recovery target preparation may retain a marker that was durably written
    before provider launch; ordinary continuation must not silently discard the
    recovery evidence brief and first-operation guard.
    """

    def reject() -> None:
        raise RecoveryRuntimeValidationError(RECOVERY_OPERATION_UNRESOLVED_MESSAGE)

    if not isinstance(raw_runtime, Mapping):
        reject()
    if set(raw_runtime) != RECOVERY_RUNTIME_FIELDS:
        reject()

    def text_field(name: str) -> str:
        value = raw_runtime.get(name)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            reject()
        return value

    source_run_id = text_field("source_run_id")
    target_run_id = text_field("target_run_id")
    source_selector = text_field("source_selector")
    target_selector = text_field("target_selector")
    for selector in (source_selector, target_selector):
        if "." not in selector:
            reject()
    for field_name in ("intent_digest", "brief_sha256"):
        digest = text_field(field_name)
        if len(digest) != 64 or digest != digest.lower() or any(
            character not in "0123456789abcdef" for character in digest
        ):
            reject()
    if (
        raw_runtime.get("schema_version") != 1
        or raw_runtime.get("mode") != "durable_evidence"
        or raw_runtime.get("source_session_context_transferred") is not False
    ):
        reject()
    if expected_source_run_id is not None and source_run_id != expected_source_run_id:
        reject()
    if expected_target_run_id is not None and target_run_id != expected_target_run_id:
        reject()
    if expected_intent is not None and (
        source_run_id != expected_intent.source_run_id
        or target_run_id != expected_intent.target_run_id
        or source_selector != expected_intent.source_selector
        or target_selector != expected_intent.target_selector
    ):
        reject()
    if expected_intent_digest is not None and raw_runtime.get("intent_digest") != expected_intent_digest:
        reject()

    operation_state = raw_runtime.get("operation_state")
    consumed = raw_runtime.get("consumed")
    if operation_state not in RECOVERY_OPERATION_STATES or not isinstance(consumed, bool):
        reject()
    if consumed != (operation_state == "consumed"):
        reject()
    if operation_state == "in_flight" or (
        operation_state == "pending" and not allow_pending
    ):
        reject()
    return operation_state, consumed


__all__ = [
    "RECOVERY_OPERATION_STATES",
    "RECOVERY_OPERATION_UNRESOLVED_MESSAGE",
    "RECOVERY_RUNTIME_FIELDS",
    "RecoveryRuntimeValidationError",
    "validate_recovery_runtime",
]
