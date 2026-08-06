from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Literal, Mapping, Protocol

from .base import HarnessAdapter, HarnessInvocation

PREFLIGHT_TIMEOUT_SECONDS = 5.0
PREFLIGHT_CLASSIFICATION = "harness_environment_preflight"
MISSING_EXECUTABLE_REMEDIATION = (
    "Install the trusted package that provides the required executable, then "
    "verify it is executable in the AFlow execution environment."
)
REASONIX_BWRAP_REMEDIATION = (
    "Install the trusted host package that provides `bwrap`, then verify "
    "`bwrap` is executable in the AFlow execution environment."
)
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9_.:@+/ \-]{1,240}$")
_SAFE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,48}$")


@dataclass(frozen=True)
class HarnessDiagnosticResult:
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class HarnessPreflightContext:
    invocation_kind: str
    cwd: Path
    env: Mapping[str, str]
    invocation: HarnessInvocation


@dataclass(frozen=True)
class HarnessEnvironmentBlocker:
    classification: str
    reason_code: str
    harness: str
    required_executable: str
    checked_command: tuple[str, ...]
    remediation: str
    safe_diagnostics: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.classification != PREFLIGHT_CLASSIFICATION:
            raise ValueError("invalid environment-preflight classification")
        if any(not isinstance(v, str) or not v.strip() for v in (
            self.reason_code, self.harness, self.required_executable, self.remediation
        )):
            raise ValueError("environment-preflight blocker fields must be non-empty")
        if not self.checked_command or any(
            not isinstance(v, str) or not v.strip() for v in self.checked_command
        ):
            raise ValueError("checked_command must contain non-empty strings")

    def to_dict(self, *, invocation_kind: str | None = None) -> dict[str, object]:
        return {
            "schema_version": 1,
            "classification": self.classification,
            "reason_code": self.reason_code,
            "harness": self.harness,
            "invocation_kind": invocation_kind,
            "required_executable": self.required_executable,
            "checked_command": list(self.checked_command),
            "remediation": self.remediation,
            "safe_diagnostics": dict(self.safe_diagnostics),
        }


@dataclass(frozen=True)
class HarnessEnvironmentPreflight:
    status: Literal["ready", "blocked"]
    blocker: HarnessEnvironmentBlocker | None = None

    def __post_init__(self) -> None:
        if self.status not in {"ready", "blocked"}:
            raise ValueError("invalid environment-preflight status")
        if self.status == "ready" and self.blocker is not None:
            raise ValueError("ready preflight cannot carry a blocker")
        if self.status == "blocked" and self.blocker is None:
            raise ValueError("blocked preflight requires a blocker")


class HarnessPreflightProbe(Protocol):
    def resolve_executable(self, command: str, *, env: Mapping[str, str]) -> str | None: ...
    def run_diagnostic(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> HarnessDiagnosticResult | subprocess.CompletedProcess[str] | None: ...


class OSHarnessPreflightProbe:
    def resolve_executable(
        self,
        command: str,
        *,
        env: Mapping[str, str],
        cwd: Path | None = None,
    ) -> str | None:
        if not command:
            return None
        if os.path.isabs(command) or os.sep in command:
            candidate = Path(command)
            if not candidate.is_absolute():
                candidate = (cwd or Path.cwd()) / candidate
            return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
        return shutil.which(command, path=env.get("PATH"))

    def run_diagnostic(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> HarnessDiagnosticResult:
        try:
            completed = subprocess.run(
                list(argv), cwd=str(cwd), env=dict(env), capture_output=True,
                text=True, check=False, timeout=min(timeout_seconds, PREFLIGHT_TIMEOUT_SECONDS),
            )
        except subprocess.TimeoutExpired:
            return HarnessDiagnosticResult(None, timed_out=True)
        except (OSError, ValueError):
            return HarnessDiagnosticResult(None)
        return HarnessDiagnosticResult(completed.returncode, completed.stdout or "", completed.stderr or "")


class NoOpHarnessPreflightProbe:
    def resolve_executable(self, command: str, *, env: Mapping[str, str]) -> str | None:
        return command or None

    def run_diagnostic(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> None:
        return None


def _safe(value: str) -> str:
    value = Path(value).name if "/" in value or "\\" in value else value
    return value if _SAFE_TEXT.fullmatch(value) else "<redacted>"


def _safe_diagnostics(values: Mapping[str, object] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in (values or {}).items():
        if (
            isinstance(key, str) and _SAFE_KEY.fullmatch(key)
            and isinstance(value, str) and value.strip()
            and len(value) <= 120 and "\n" not in value and "\r" not in value
            and "/" not in value and "\\" not in value
        ):
            result[key] = value.strip()
    return result


def _validated(
    blocker: HarnessEnvironmentBlocker,
    invocation_kind: str,
) -> HarnessEnvironmentBlocker:
    diagnostics = _safe_diagnostics(blocker.safe_diagnostics)
    diagnostics["invocation_kind"] = _safe(invocation_kind)
    return HarnessEnvironmentBlocker(
        PREFLIGHT_CLASSIFICATION,
        _safe(blocker.reason_code),
        _safe(blocker.harness),
        _safe(blocker.required_executable),
        tuple(_safe(value) for value in blocker.checked_command),
        blocker.remediation.strip()[:240],
        diagnostics,
    )


def evaluate_harness_environment(
    context: HarnessPreflightContext,
    adapter: HarnessAdapter,
    probe: HarnessPreflightProbe,
) -> HarnessEnvironmentPreflight:
    if not context.invocation.argv:
        blocker = HarnessEnvironmentBlocker(
            PREFLIGHT_CLASSIFICATION, "harness_executable_missing",
            context.invocation.label, "unknown", ("<missing>",),
            MISSING_EXECUTABLE_REMEDIATION, {},
        )
        return HarnessEnvironmentPreflight("blocked", _validated(blocker, context.invocation_kind))
    command = context.invocation.argv[0]
    if isinstance(probe, OSHarnessPreflightProbe):
        resolved_primary = probe.resolve_executable(
            command, env=context.env, cwd=context.cwd
        )
    else:
        resolved_primary = probe.resolve_executable(command, env=context.env)
    if resolved_primary is None:
        blocker = HarnessEnvironmentBlocker(
            PREFLIGHT_CLASSIFICATION, "harness_executable_missing",
            context.invocation.label, _safe(command), (_safe(command),),
            MISSING_EXECUTABLE_REMEDIATION, {},
        )
        return HarnessEnvironmentPreflight("blocked", _validated(blocker, context.invocation_kind))
    capability = getattr(adapter, "preflight_environment", None)
    if capability is None:
        return HarnessEnvironmentPreflight("ready")
    try:
        result = capability(context, probe)
    except (OSError, NotImplementedError, TimeoutError, TypeError, ValueError):
        return HarnessEnvironmentPreflight("ready")
    if result is None:
        return HarnessEnvironmentPreflight("ready")
    blocker = result.blocker if isinstance(result, HarnessEnvironmentPreflight) else result
    if not isinstance(blocker, HarnessEnvironmentBlocker):
        return HarnessEnvironmentPreflight("ready")
    return HarnessEnvironmentPreflight("blocked", _validated(blocker, context.invocation_kind))


def diagnostic_fields(result: Any) -> tuple[int | None, str, bool]:
    if result is None:
        return None, "", False
    if isinstance(result, HarnessDiagnosticResult):
        return result.returncode, result.stdout, result.timed_out
    if isinstance(result, tuple):
        return (
            result[0] if len(result) > 0 and isinstance(result[0], int) else None,
            result[1] if len(result) > 1 and isinstance(result[1], str) else "",
            False,
        )
    return (
        result.returncode if isinstance(getattr(result, "returncode", None), int) else None,
        result.stdout if isinstance(getattr(result, "stdout", None), str) else "",
        bool(getattr(result, "timed_out", False)),
    )


__all__ = [
    "HarnessDiagnosticResult", "HarnessEnvironmentBlocker",
    "HarnessEnvironmentPreflight", "HarnessPreflightContext",
    "HarnessPreflightProbe", "MISSING_EXECUTABLE_REMEDIATION",
    "NoOpHarnessPreflightProbe", "OSHarnessPreflightProbe",
    "PREFLIGHT_CLASSIFICATION", "PREFLIGHT_TIMEOUT_SECONDS",
    "REASONIX_BWRAP_REMEDIATION", "diagnostic_fields",
    "evaluate_harness_environment",
]
