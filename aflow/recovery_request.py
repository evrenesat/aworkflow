"""Transport-neutral request contract for explicit continuation recovery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


RECOVERY_MODE_DURABLE_EVIDENCE = "durable_evidence"
_MAX_TEXT = 512


class RecoveryValidationError(ValueError):
    """A recovery request is malformed or incomplete."""


def _safe_text(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise RecoveryValidationError(f"recovery field '{field}' must be a string")
    if not value or value != value.strip() or len(value) > _MAX_TEXT:
        raise RecoveryValidationError(
            f"recovery field '{field}' is empty, padded, or oversized"
        )
    if "\x00" in value or "\n" in value or "\r" in value:
        raise RecoveryValidationError(
            f"recovery field '{field}' contains control characters"
        )
    return value


@dataclass(frozen=True)
class RecoveryRequest:
    """The only accepted explicit continuation recovery choice."""

    mode: Literal["durable_evidence"]
    worker_selector: str

    def __post_init__(self) -> None:
        if self.mode != RECOVERY_MODE_DURABLE_EVIDENCE:
            raise RecoveryValidationError(
                "recovery mode must be 'durable_evidence'"
            )
        _safe_text(self.worker_selector, field="worker_selector")

    def to_dict(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "worker_selector": self.worker_selector,
        }

    @classmethod
    def from_value(cls, value: object) -> "RecoveryRequest | None":
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise RecoveryValidationError("recovery must be an object")
        expected = {"mode", "worker_selector"}
        keys = set(value)
        if any(not isinstance(key, str) for key in keys):
            raise RecoveryValidationError("recovery field names must be strings")
        unknown = sorted(keys - expected)
        missing = sorted(expected - keys)
        if unknown:
            raise RecoveryValidationError(
                "recovery contains unsupported fields: " + ", ".join(unknown)
            )
        if missing:
            raise RecoveryValidationError(
                "recovery is missing required fields: " + ", ".join(missing)
            )
        mode = value.get("mode")
        if mode != RECOVERY_MODE_DURABLE_EVIDENCE:
            raise RecoveryValidationError(
                "recovery mode must be 'durable_evidence'"
            )
        worker_selector = _safe_text(
            value.get("worker_selector"), field="worker_selector"
        )
        return cls(mode=RECOVERY_MODE_DURABLE_EVIDENCE, worker_selector=worker_selector)


DurableEvidenceRecoveryRequest = RecoveryRequest


__all__ = [
    "DurableEvidenceRecoveryRequest",
    "RECOVERY_MODE_DURABLE_EVIDENCE",
    "RecoveryRequest",
    "RecoveryValidationError",
]
