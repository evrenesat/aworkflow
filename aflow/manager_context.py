"""Versioned, plan-safe semantic evidence for manager supervision.

The builder deliberately reads turn artifacts and result metadata only. Lite
never exposes prompt artifacts or active-plan prose; runtime may provide a
captured plan solely to derive bounded controller-owned scope metadata.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal, Mapping

from .analyzer import (
    analyze_progress_tail,
    classify_turn_text_signals,
    extract_text_signals,
    snapshot_signature,
)
from .plan import PlanParseError, load_plan_tolerant
from .repartition import parse_envelope_bytes
from .scope_pressure import has_scope_pressure
from .stop_marker import extract_stop_markers


MANAGER_CONTEXT_SCHEMA_VERSION = 1
MANAGER_CONTEXT_SCHEMA_VERSION_V2 = 2
MANAGER_CONTEXT_SCHEMA_VERSION_V3 = 3
DIAGNOSTIC_LIMIT = 2_000
MAX_MANAGER_NOTE_SCOPE_PATHS = 32
MAX_MANAGER_NOTE_SCOPE_PATH_LENGTH = 240
MAX_MANAGER_NOTE_SCOPE_IDENTITY_LENGTH = 512
# Prompt budgets: the inline manager user manifest targets 16 KiB and is
# hard-limited to 32 KiB before any provider process starts.
MANAGER_INLINE_CONTEXT_TARGET_BYTES = 16 * 1024
MANAGER_INLINE_CONTEXT_MAX_BYTES = 32 * 1024
MANAGER_SUMMARY_MAX_CHARS = 2_000
MANAGER_RUN_EXTRACT_MAX_RECORDS = 12
# One shared deterministic truncation marker for every bounded semantic
# field in schema-v3 contexts (decisions, rejections, diagnostics).
TRUNCATION_MARKER = "\n[evidence excerpt truncated]"
ManagerLevel = Literal["lite", "full"]
_ALLOWED_SCOPE_DIRECTIVE_RE = re.compile(
    r"^(?:may\s+(?:create(?:\s+(?:or\s+)?modify|/modify)?|modify(?:\s+only)?)|"
    r"allowed\s+files?)\s*:?\s*(?P<paths>.*)$",
    re.IGNORECASE,
)
_PROHIBITED_SCOPE_DIRECTIVE_RE = re.compile(
    r"^(?:must\s+not\s+touch|do\s+not\s+modify)\s*:?\s*(?P<paths>.*)$",
    re.IGNORECASE,
)
_LIST_ITEM_RE = re.compile(r"^[-*]\s+(.+)$")
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_EXPLICIT_RELATIVE_PATH_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$"
)
_SCOPE_PATH_SEPARATOR_RE = re.compile(r"\s*(?:,|\band\b)\s*", re.IGNORECASE)


@dataclass(frozen=True)
class RawArtifactReference:
    path: str
    byte_size: int
    status: str


@dataclass(frozen=True)
class SemanticTurnOutcome:
    extraction: str
    result: str
    fallback: bool


@dataclass(frozen=True)
class ProgressSignals:
    unchanged_snapshot_turns: int
    same_step_stall_turns: int
    alternating_two_step_tail: bool
    reviewer_rejection_count: int
    reviewer_non_convergence: bool


@dataclass(frozen=True)
class StructuredPlanState:
    original_plan_path: str | None
    active_plan_path: str | None
    active_repair_plan: bool
    checkpoints: tuple[dict[str, Any], ...]
    current_checkpoint: dict[str, Any] | None
    is_complete: bool | None
    parse_error: str | None


@dataclass(frozen=True)
class CompactRunRecord:
    kind: Literal["workflow_turn", "manager_decision"]
    number: int
    status: str | None
    step_name: str | None
    team: str | None
    selector: str | None
    semantic_summary: str | None
    plan_delta: bool | None
    signals: tuple[str, ...]
    routing: dict[str, Any]


@dataclass(frozen=True)
class ManagerContextV1:
    schema_version: int
    run_id: str
    decision_number: int
    level: ManagerLevel
    trigger: str
    finished_turn: dict[str, Any]
    run_extract: tuple[dict[str, Any], ...]
    plan_state: dict[str, Any]
    controller_state: dict[str, Any]
    active_plan_content: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ManagerContextV2:
    schema_version: int
    run_id: str
    decision_number: int
    level: ManagerLevel
    trigger: str
    finished_turn: dict[str, Any]
    run_extract: tuple[dict[str, Any], ...]
    plan_state: dict[str, Any]
    controller_state: dict[str, Any]
    active_plan_content: str | None = None
    original_plan_content: str | None = None
    plan_content_disclosure: dict[str, str] | None = None
    envelope: dict[str, Any] | None = None
    active_scope_rejection_ledger: tuple[dict[str, Any], ...] = ()
    implementation_attempts: dict[str, Any] | None = None
    manager_decisions: tuple[dict[str, Any], ...] = ()
    change_surface_evidence: dict[str, Any] | None = None
    manager_note_scope: dict[str, Any] | None = None
    retry_manager_note_scope: dict[str, Any] | None = None
    scope_pressure_detected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ManagerContextV3:
    """Reference-only schema-v3 manager context.

    Deliberately defines none of the v1/v2 body fields. Plan and checkpoint
    bodies live only in the run-local content-addressed evidence store and
    are exposed as validated references; the manager reads them from disk
    only when the compact manifest is insufficient.
    """

    schema_version: int
    run_id: str
    decision_number: int
    level: ManagerLevel
    trigger: str
    finished_turn: dict[str, Any]
    run_extract: tuple[dict[str, Any], ...]
    plan_state: dict[str, Any]
    controller_state: dict[str, Any]
    evidence: dict[str, Any]
    plan_content_disclosure: dict[str, str]
    active_scope_rejection_ledger: tuple[dict[str, Any], ...] = ()
    implementation_attempts: dict[str, Any] | None = None
    manager_decisions: tuple[dict[str, Any], ...] = ()
    change_surface_evidence: dict[str, Any] | None = None
    manager_note_scope: dict[str, Any] | None = None
    retry_manager_note_scope: dict[str, Any] | None = None
    scope_pressure_detected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def _artifact_ref(run_dir: Path, path: Path) -> RawArtifactReference:
    try:
        size = path.stat().st_size
        status = "present"
    except OSError:
        size = 0
        status = "missing"
    return RawArtifactReference(path=str(path.relative_to(run_dir)), byte_size=size, status=status)


def _bounded(text: str) -> str:
    if len(text) <= DIAGNOSTIC_LIMIT:
        return text
    return text[:DIAGNOSTIC_LIMIT] + "\n[diagnostic excerpt truncated]"


def _text_content(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_text_content(item) for item in value]
        joined = "".join(part for part in parts if part)
        return joined or None
    if isinstance(value, dict):
        for key in ("text", "content", "output_text"):
            candidate = _text_content(value.get(key))
            if candidate:
                return candidate
    return None


def _structured_final_assistant_result(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("role") == "assistant":
            candidate = _text_content(value.get("content", value.get("text")))
            if candidate:
                return candidate
        for key in ("result", "final", "final_output", "output", "message", "item"):
            candidate = _structured_final_assistant_result(value.get(key))
            if candidate:
                return candidate
        candidates = [_structured_final_assistant_result(item) for item in value.get("messages", [])] if isinstance(value.get("messages"), list) else []
        return next((item for item in reversed(candidates) if item), None)
    if isinstance(value, list):
        candidates = [_structured_final_assistant_result(item) for item in value]
        return next((item for item in reversed(candidates) if item), None)
    return None


def extract_semantic_result(stdout: str) -> SemanticTurnOutcome:
    """Extract a complete final assistant response from common JSON/event streams."""
    candidates: list[Any] = []
    try:
        candidates.append(json.loads(stdout))
    except json.JSONDecodeError:
        pass
    for line in stdout.splitlines():
        try:
            candidates.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    for candidate in reversed(candidates):
        result = _structured_final_assistant_result(candidate)
        if result:
            return SemanticTurnOutcome("structured_stream", result, False)
    if candidates:
        return SemanticTurnOutcome("unrecognized_structured_stream", stdout, True)
    return SemanticTurnOutcome("plain_text", stdout, False)


_REJECTION_FALLBACK = (
    "Reviewer rejected this implementation; see the review stdout artifact for details."
)


def _bounded_normalized_text(text: str) -> str:
    normalized = " ".join(" ".join(line.split()) for line in text.splitlines() if line.split()).strip()
    return normalized[:479] + "…" if len(normalized) > 480 else normalized


def summarize_review_rejection(stdout: str) -> str:
    """Return a deterministic, Rich-safe compact reviewer summary."""
    summary = _bounded_normalized_text(extract_semantic_result(stdout).result)
    return summary or _REJECTION_FALLBACK


def summarize_repair_plan(path: Path | None) -> str | None:
    """Extract only the first Summary section without treating plan text as markup."""
    if path is None:
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    heading: tuple[int, int] | None = None
    body: list[str] = []
    for line in lines:
        match = re.match(r"^(#{1,3})\s+Summary\s*$", line, re.IGNORECASE)
        if heading is None:
            if match:
                heading = (len(match.group(1)), 0)
            continue
        next_heading = re.match(r"^(#{1,3})\s+", line)
        if next_heading and len(next_heading.group(1)) <= heading[0]:
            break
        body.append(line)
    summary = _bounded_normalized_text("\n".join(body))
    return summary or None


def _load_turns(run_dir: Path) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    turns_dir = run_dir / "turns"
    if not turns_dir.is_dir():
        return turns
    for turn_dir in sorted(path for path in turns_dir.iterdir() if path.is_dir()):
        result_path = turn_dir / "result.json"
        if not result_path.is_file():
            continue
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        result["_turn_dir"] = turn_dir
        turns.append(result)
    return turns


def _path_from_metadata(run_dir: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value)
    if candidate.is_file():
        return candidate
    relative = run_dir.parent.parent.parent / candidate
    return relative if relative.is_file() else None


def _resolve_run_artifact(run_dir: Path, artifact_path: str) -> Path | None:
    """Resolve a controller artifact path inside this run, rejecting escapes.

    Turn records historically use repository-relative display paths while
    scope artifacts use run-relative paths.  Accept both representations only
    when they resolve underneath the selected run directory.
    """
    run_root = run_dir.resolve()
    for candidate in (
        run_dir / artifact_path,
        run_dir.parent.parent.parent / artifact_path,
    ):
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(run_root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    return None


def _resolve_validated_envelope(
    run_dir: Path,
    artifact_path: str,
    expected_artifact_sha256: str,
    expected_canonical_sha256: str,
    active_scope: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Resolve, read, hash-check, and parse a validated envelope.

    Returns the envelope payload dict on success, or ``None`` when the
    artifact is missing, malformed, path-escaping, or hash-invalid.  The
    caller must treat ``None`` as ineligible envelope evidence rather than
    promoting invalid data as validated context.
    """
    scope_id = active_scope.get("scope_id") if active_scope is not None else None
    checkpoint_index = (
        active_scope.get("checkpoint_index") if active_scope is not None else None
    )
    checkpoint_name = (
        active_scope.get("checkpoint_name") if active_scope is not None else None
    )
    if (
        not isinstance(scope_id, str)
        or not scope_id
        or not isinstance(checkpoint_index, int)
        or isinstance(checkpoint_index, bool)
        or not isinstance(checkpoint_name, str)
    ):
        return None
    expected_scope_digest = hashlib.sha256(scope_id.encode("utf-8")).hexdigest()
    if artifact_path != f"scopes/{expected_scope_digest}/envelope.json":
        return None
    if (
        re.fullmatch(r"[0-9a-f]{64}", expected_artifact_sha256) is None
        or re.fullmatch(r"[0-9a-f]{64}", expected_canonical_sha256) is None
    ):
        return None
    resolved = _resolve_run_artifact(run_dir, artifact_path)
    if resolved is None:
        return None
    try:
        raw = resolved.read_bytes()
    except OSError:
        return None
    actual_artifact_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_artifact_sha256 != expected_artifact_sha256:
        return None
    try:
        parsed = parse_envelope_bytes(raw)
    except ValueError:
        return None
    if (
        parsed.scope_id != scope_id
        or parsed.scope_digest != expected_scope_digest
        or parsed.checkpoint_index != checkpoint_index
        or parsed.checkpoint_name != checkpoint_name
        or parsed.canonical_envelope_sha256 != expected_canonical_sha256
    ):
        return None
    payload = parsed.to_dict()
    payload.update({
        "artifact_path": artifact_path,
        "artifact_sha256": expected_artifact_sha256,
        "canonical_envelope_sha256": expected_canonical_sha256,
        "available": True,
        "validated": True,
    })
    return payload


def _unavailable_envelope(reason: str) -> dict[str, Any]:
    """Return an explicit non-authoritative verdict for missing evidence."""
    return {"available": False, "validated": False, "reason": reason}


def _v3_run_paths(run_dir: Path):
    """Build runlog RunPaths from a run directory without new-run creation."""
    from .runlog import RunPaths

    return RunPaths(
        repo_root=run_dir.parent.parent.parent,
        runs_root=run_dir.parent,
        run_dir=run_dir,
        turns_dir=run_dir / "turns",
        manager_dir=run_dir / "manager",
        run_json=run_dir / "run.json",
    )


def _v3_bounded_text(value: Any) -> str | None:
    """Bound one schema-v3 semantic field with the shared truncation marker."""
    if not isinstance(value, str):
        return None
    if len(value) <= MANAGER_SUMMARY_MAX_CHARS:
        return value
    return value[:MANAGER_SUMMARY_MAX_CHARS] + TRUNCATION_MARKER


def _v3_ref_dict(reference: Any) -> dict[str, Any]:
    if reference is None:
        return {}
    if isinstance(reference, Mapping):
        return {
            "kind": reference.get("kind"),
            "path": reference.get("path"),
            "sha256": reference.get("sha256"),
            "byte_size": reference.get("byte_size"),
        }
    return {
        "kind": getattr(reference, "kind", None),
        "path": getattr(reference, "path", None),
        "sha256": getattr(reference, "sha256", None),
        "byte_size": getattr(reference, "byte_size", None),
    }


def _v3_unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason}


def _v3_reference_entry(reference: Any) -> dict[str, Any]:
    if reference is None:
        return _v3_unavailable("referenced content is unavailable")
    return {"available": True, "reference": _v3_ref_dict(reference)}


def _capture_v3_evidence(
    run_dir: Path,
    *,
    capture: bool,
    boundary: Mapping[str, Any],
    run_json: Mapping[str, Any],
    finished: Mapping[str, Any],
    plan_state_payload: Mapping[str, Any],
    validated_envelope: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    """Capture/reuse content-addressed plan and checkpoint evidence.

    ``capture`` is true only for live runtime boundaries; historical rebuilds
    never write. Equal active/original bytes share one plan artifact. A
    repair overlay differing from the envelope original gets its own artifact.
    Returns (evidence, plan_content_disclosure, checkpoint_info).
    """
    from .runlog import (
        capture_checkpoint_evidence,
        capture_plan_evidence,
        resolve_evidence_artifact,
    )
    from .repartition import slice_checkpoint_source

    paths = _v3_run_paths(run_dir)
    unavailable_plan = _v3_unavailable(
        "the exact active plan bytes are unavailable at this boundary"
    )

    def available_reference(reference: Any) -> dict[str, Any] | None:
        if reference is None:
            return None
        try:
            resolve_evidence_artifact(paths, reference)
        except ValueError:
            return None
        return _v3_ref_dict(reference)

    # --- Exact active plan bytes ---
    active_text: str | None = None
    active_from = boundary.get("active_plan_content")
    if isinstance(active_from, str):
        active_text = active_from
    else:
        active_meta = plan_state_payload.get("active_plan_path")
        if isinstance(active_meta, str):
            active_candidate = _path_from_metadata(run_dir, active_meta)
            if active_candidate is not None:
                active_text = _read_text(active_candidate) or None
    original_text: str | None = None
    envelope_plan_ref: Any = None
    if validated_envelope is not None and validated_envelope.get("available"):
        envelope_plan_ref = validated_envelope.get("plan_ref")
        if not isinstance(envelope_plan_ref, Mapping):
            # Schema-v1 envelope payload embeds the immutable plan text.
            embedded = validated_envelope.get("plan_text")
            original_text = embedded if isinstance(embedded, str) else None
    if original_text is None and envelope_plan_ref is None:
        # The boundary persists the exact original bytes captured at runtime
        # so historical rebuilds never depend on later file mutations.
        boundary_original = boundary.get("original_plan_content")
        if isinstance(boundary_original, str):
            original_text = boundary_original
        else:
            original_value = (
                boundary.get("original_plan_path")
                or run_json.get("original_plan_path")
                or run_json.get("plan_path")
            )
            original_candidate = _path_from_metadata(run_dir, original_value)
            if original_candidate is not None:
                original_text = _read_text(original_candidate) or None

    active_plan_entry = unavailable_plan
    if active_text is not None:
        try:
            active_bytes = active_text.encode("utf-8")
            if capture:
                active_ref = capture_plan_evidence(paths, active_text)
                active_plan_entry = _v3_reference_entry(active_ref)
            else:
                digest = hashlib.sha256(active_bytes).hexdigest()
                from .runlog import evidence_reference

                active_ref = evidence_reference(paths, "plan", digest, len(active_bytes))
                resolved = available_reference(active_ref)
                if resolved is not None:
                    active_plan_entry = _v3_reference_entry(active_ref)
        except ValueError as exc:
            active_plan_entry = _v3_unavailable(str(exc))

    # --- Original plan: envelope authority first, then file bytes ---
    original_entry: dict[str, Any] = _v3_unavailable(
        "no distinct original plan is available at this boundary"
    )
    if envelope_plan_ref is not None:
        resolved = available_reference(envelope_plan_ref)
        if resolved is not None:
            original_entry = {
                "available": True,
                "reference": resolved,
                "source": "scope_envelope",
            }
    if original_entry.get("available") is not True and original_text is not None:
        if active_text is not None and original_text == active_text:
            original_entry = {
                "available": True,
                "shared_with": "active_plan",
            }
        else:
            try:
                if capture:
                    original_ref = capture_plan_evidence(paths, original_text)
                    original_entry = {
                        "available": True,
                        "reference": _v3_ref_dict(original_ref),
                        "source": "boundary_plan_file",
                    }
                else:
                    digest = hashlib.sha256(original_text.encode("utf-8")).hexdigest()
                    from .runlog import evidence_reference

                    original_ref = evidence_reference(
                        paths, "plan", digest, len(original_text.encode("utf-8"))
                    )
                    resolved = available_reference(original_ref)
                    if resolved is not None:
                        original_entry = {
                            "available": True,
                            "reference": _v3_ref_dict(original_ref),
                            "source": "boundary_plan_file",
                        }
            except ValueError as exc:
                original_entry = _v3_unavailable(str(exc))

    # --- Current checkpoint of the active plan ---
    checkpoint_entry: dict[str, Any] = _v3_unavailable(
        "the current checkpoint bytes are unavailable at this boundary"
    )
    checkpoint_info: dict[str, Any] = {}
    scope = boundary.get("active_implementation_scope")
    scope_mapping = scope if isinstance(scope, Mapping) else None
    checkpoint_index: Any = None
    if scope_mapping is not None:
        checkpoint_index = scope_mapping.get("checkpoint_index")
    if not isinstance(checkpoint_index, int):
        current = plan_state_payload.get("current_checkpoint")
        if isinstance(current, Mapping):
            checkpoint_index = current.get("index")
    if active_text is not None and isinstance(checkpoint_index, int):
        try:
            source_slice = slice_checkpoint_source(
                active_text, checkpoint_index=checkpoint_index
            )
        except ValueError:
            source_slice = None
        if source_slice is not None:
            checkpoint_text = source_slice.full_text
            try:
                if capture:
                    checkpoint_ref = capture_checkpoint_evidence(paths, checkpoint_text)
                    plan_bytes = active_text.encode("utf-8")
                    line_end = (
                        plan_bytes[: source_slice.checkpoint_byte_end]
                        .decode("utf-8", "strict")
                        .count("\n")
                        + 1
                    )
                    checkpoint_info = {
                        "checkpoint_index": source_slice.checkpoint_index,
                        "checkpoint_name": source_slice.checkpoint_name,
                        "line_start": source_slice.heading_line,
                        "line_end": line_end,
                        "byte_start": source_slice.checkpoint_byte_start,
                        "byte_end": source_slice.checkpoint_byte_end,
                    }
                    checkpoint_entry = {
                        "available": True,
                        "reference": _v3_ref_dict(checkpoint_ref),
                        **checkpoint_info,
                    }
                else:
                    digest = hashlib.sha256(checkpoint_text.encode("utf-8")).hexdigest()
                    from .runlog import evidence_reference

                    checkpoint_ref = evidence_reference(
                        paths, "checkpoint", digest, len(checkpoint_text.encode("utf-8"))
                    )
                    if available_reference(checkpoint_ref) is not None:
                        plan_bytes = active_text.encode("utf-8")
                        line_end = (
                            plan_bytes[: source_slice.checkpoint_byte_end]
                            .decode("utf-8", "strict")
                            .count("\n")
                            + 1
                        )
                        checkpoint_info = {
                            "checkpoint_index": source_slice.checkpoint_index,
                            "checkpoint_name": source_slice.checkpoint_name,
                            "line_start": source_slice.heading_line,
                            "line_end": line_end,
                            "byte_start": source_slice.checkpoint_byte_start,
                            "byte_end": source_slice.checkpoint_byte_end,
                        }
                        checkpoint_entry = {
                            "available": True,
                            "reference": _v3_ref_dict(checkpoint_ref),
                            **checkpoint_info,
                        }
            except (ValueError, UnicodeDecodeError) as exc:
                checkpoint_entry = _v3_unavailable(str(exc))
                line_end = (
                    plan_bytes[: source_slice.checkpoint_byte_end]
                    .decode("utf-8", "strict")
                    .count("\n")
                    + 1
                )
                checkpoint_info = {
                    "checkpoint_index": source_slice.checkpoint_index,
                    "checkpoint_name": source_slice.checkpoint_name,
                    "line_start": source_slice.heading_line,
                    "line_end": line_end,
                    "byte_start": source_slice.checkpoint_byte_start,
                    "byte_end": source_slice.checkpoint_byte_end,
                }
                checkpoint_entry = {
                    "available": True,
                    "reference": _v3_ref_dict(checkpoint_ref),
                    **checkpoint_info,
                }
            except (ValueError, UnicodeDecodeError) as exc:
                checkpoint_entry = _v3_unavailable(str(exc))

    # --- Reviewer stdout: durable turn-artifact reference, never a copy ---
    reviewer_entry: dict[str, Any] | None = None
    if finished.get("step_role") == "reviewer":
        turn_dir = Path(finished["_turn_dir"])
        stdout_path = turn_dir / "stdout.txt"
        if stdout_path.is_file():
            try:
                relative = stdout_path.relative_to(run_dir).as_posix()
                reviewer_entry = {
                    "artifact_path": relative,
                    "available": True,
                    "byte_size": stdout_path.stat().st_size,
                }
            except (OSError, ValueError):
                reviewer_entry = None
    evidence: dict[str, Any] = {
        "active_plan": active_plan_entry,
        "original_plan": original_entry,
        "checkpoint": checkpoint_entry,
    }
    if reviewer_entry is not None:
        evidence["reviewer_stdout"] = reviewer_entry
    # Plan disclosure mirrors what the compact manifest actually contains.
    disclosure: dict[str, str] = {}
    disclosure["active_plan"] = (
        "referenced"
        if active_plan_entry.get("available") is True
        else "unavailable"
    )
    disclosure["original_plan"] = (
        "referenced"
        if original_entry.get("available") is True
        else "omitted" if original_entry.get("shared_with") == "active_plan"
        else "unavailable"
    )
    disclosure["checkpoint"] = (
        "referenced"
        if checkpoint_entry.get("available") is True
        else "unavailable"
    )
    return evidence, disclosure, checkpoint_info


def build_manager_note_scope(
    *,
    active_plan_identity: str | None,
    active_plan_content: str | None,
) -> dict[str, Any]:
    """Return bounded controller-owned scope facts without exposing plan prose."""
    allowed_paths: list[str] = []
    prohibited_paths: list[str] = []
    mode: str | None = None
    constraints_complete = isinstance(active_plan_content, str)
    mode_has_path = False

    def collect_paths(text: str, target: list[str]) -> bool:
        nonlocal constraints_complete
        raw_paths, parsed_completely = _scope_path_candidates(text)
        if not parsed_completely:
            constraints_complete = False
        found_path = False
        for raw_path in raw_paths:
            found_path = True
            path = raw_path
            if path in target:
                continue
            if len(target) >= MAX_MANAGER_NOTE_SCOPE_PATHS:
                constraints_complete = False
                continue
            target.append(path)
        return found_path

    if isinstance(active_plan_content, str):
        for raw_line in active_plan_content.splitlines():
            line = raw_line.strip()
            if line.startswith("#"):
                if mode is not None and not mode_has_path:
                    constraints_complete = False
                mode = None
                mode_has_path = False
                continue
            item = _LIST_ITEM_RE.match(line)
            content = item.group(1) if item is not None else line
            allowed = _ALLOWED_SCOPE_DIRECTIVE_RE.match(content)
            prohibited = _PROHIBITED_SCOPE_DIRECTIVE_RE.match(content)
            if allowed is not None:
                mode = "allowed"
                mode_has_path = collect_paths(allowed.group("paths"), allowed_paths)
                continue
            if prohibited is not None:
                mode = "prohibited"
                mode_has_path = collect_paths(prohibited.group("paths"), prohibited_paths)
                continue
            if mode is None:
                continue
            if item is None:
                if line:
                    if not mode_has_path:
                        constraints_complete = False
                    mode = None
                    mode_has_path = False
                continue
            target = allowed_paths if mode == "allowed" else prohibited_paths
            mode_has_path = collect_paths(content, target) or mode_has_path
        if mode is not None and not mode_has_path:
            constraints_complete = False
    return {
        "active_plan_identity": _bounded_scope_identity(active_plan_identity),
        "allowed_paths": allowed_paths,
        "prohibited_paths": prohibited_paths,
        "authority": "controller_owned",
        "constraints_complete": constraints_complete,
    }


def _is_explicit_scope_path(path: str) -> bool:
    return (
        0 < len(path) <= MAX_MANAGER_NOTE_SCOPE_PATH_LENGTH
        and _is_relative_path_candidate(path)
    )


def _is_relative_path_candidate(path: str) -> bool:
    return (
        _EXPLICIT_RELATIVE_PATH_RE.fullmatch(path) is not None
        and all(part not in {".", ".."} for part in path.split("/"))
    )


def _scope_path_candidates(text: str) -> tuple[list[str], bool]:
    """Parse every explicit scope-list item or report that extraction is incomplete."""
    raw_candidates = list(_CODE_SPAN_RE.findall(text))
    plain_text = _CODE_SPAN_RE.sub(" ", text)
    for segment in _SCOPE_PATH_SEPARATOR_RE.split(plain_text):
        candidate = segment.strip().strip(".,;()[]{}")
        if not candidate:
            continue
        if ":" in candidate:
            candidate = candidate.split(":", 1)[0].strip()
            if not candidate:
                continue
        raw_candidates.append(candidate)

    paths: list[str] = []
    complete = True
    for raw_candidate in raw_candidates:
        candidate = raw_candidate.strip().strip(".,:;()[]{}")
        if not _is_explicit_scope_path(candidate):
            complete = False
            continue
        paths.append(candidate)
    return paths, complete


def _bounded_scope_identity(identity: str | None) -> str | None:
    if not isinstance(identity, str) or len(identity) <= MAX_MANAGER_NOTE_SCOPE_IDENTITY_LENGTH:
        return identity
    return "sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _plan_state(run_dir: Path, run_json: dict[str, Any], finished_turn: dict[str, Any]) -> tuple[StructuredPlanState, Path | None]:
    # ``run_json`` carries boundary overrides applied immediately above. Prefer
    # it over the finalized turn, whose active path is necessarily pre-routing.
    active_value = (
        run_json.get("active_plan_path")
        or finished_turn.get("active_plan_path")
        or run_json.get("plan_path")
    )
    original_value = (
        run_json.get("original_plan_path")
        or finished_turn.get("original_plan_path")
        or run_json.get("plan_path")
    )
    active_path = _path_from_metadata(run_dir, active_value)
    checkpoints: tuple[dict[str, Any], ...] = ()
    current: dict[str, Any] | None = None
    complete: bool | None = None
    parse_error: str | None = None
    if active_path is not None:
        try:
            loaded = load_plan_tolerant(active_path)
            checkpoints = tuple({
                "index": index,
                "name": section.name,
                "heading_checked": section.heading_checked,
                "checked_step_count": section.checked_step_count,
                "unchecked_step_count": section.unchecked_step_count,
            } for index, section in enumerate(loaded.parsed_plan.sections, start=1))
            snapshot = loaded.parsed_plan.snapshot
            complete = snapshot.is_complete
            if snapshot.current_checkpoint_index is not None:
                current = next((item for item in checkpoints if item["index"] == snapshot.current_checkpoint_index), None)
            parse_error = str(loaded.parse_error) if loaded.parse_error else None
        except (OSError, PlanParseError, ValueError) as exc:
            parse_error = str(exc)
    return StructuredPlanState(
        original_plan_path=str(original_value) if isinstance(original_value, str) else None,
        active_plan_path=str(active_value) if isinstance(active_value, str) else None,
        active_repair_plan=bool(active_value and original_value and active_value != original_value),
        checkpoints=checkpoints,
        current_checkpoint=current,
        is_complete=complete,
        parse_error=parse_error,
    ), active_path


def analyze_manager_progress(
    turns: list[dict[str, Any]],
    *,
    legacy_reviewer_logic: bool = False,
) -> ProgressSignals:
    progress = analyze_progress_tail(turns)
    reviewer_rejection_count = (
        progress["legacy_reviewer_rejection_count"]
        if legacy_reviewer_logic
        else progress["reviewer_rejection_count"]
    )
    return ProgressSignals(
        unchanged_snapshot_turns=progress["unchanged_snapshot_turns"],
        same_step_stall_turns=progress["same_step_stall_turns"],
        alternating_two_step_tail=progress["alternating_two_step_tail"],
        reviewer_rejection_count=reviewer_rejection_count,
        reviewer_non_convergence=reviewer_rejection_count >= 2,
    )


def scoped_reviewer_rejection_count(
    run_dir: Path,
    scope: Mapping[str, Any],
) -> int | None:
    """Recompute one active scope's rejection count from durable turn artifacts."""
    opened_turn_number = scope.get("opened_turn_number")
    if not isinstance(opened_turn_number, int):
        return None
    scoped_turns = [
        turn
        for turn in _load_turns(Path(run_dir))
        if int(turn.get("turn_number", 0) or 0) >= opened_turn_number
    ]
    progress = analyze_manager_progress(scoped_turns)
    carried_rejections = int(
        scope.get("carried_reviewer_rejection_count", 0) or 0
    )
    return carried_rejections + progress.reviewer_rejection_count


def _duration_seconds(turn: dict[str, Any]) -> float | None:
    explicit = turn.get("duration_seconds")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return float(explicit)
    started = turn.get("started_at")
    finished = turn.get("finished_at")
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    try:
        return (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
    except ValueError:
        return None


def _compact_turn(run_dir: Path, turn: dict[str, Any]) -> CompactRunRecord:
    turn_dir = Path(turn["_turn_dir"])
    semantic = extract_semantic_result(_read_text(turn_dir / "stdout.txt") or str(turn.get("stdout", "")))
    before = snapshot_signature(turn.get("snapshot_before"))
    after = snapshot_signature(turn.get("snapshot_after"))
    return CompactRunRecord(
        kind="workflow_turn",
        number=int(turn.get("turn_number", 0)),
        status=turn.get("status") if isinstance(turn.get("status"), str) else None,
        step_name=turn.get("step_name") if isinstance(turn.get("step_name"), str) else None,
        team=turn.get("team") if isinstance(turn.get("team"), str) else None,
        selector=turn.get("selector") if isinstance(turn.get("selector"), str) else None,
        semantic_summary=_bounded(semantic.result),
        plan_delta=before is not None and after is not None and before != after,
        signals=tuple(sorted(set(extract_text_signals(semantic.result) + (["explicit_stop"] if extract_stop_markers(semantic.result) else [])))),
        routing={"chosen_transition": turn.get("chosen_transition"), "recovery_action": turn.get("recovery_action")},
    )


def _manager_records(run_dir: Path, *, before_decision_number: int | None = None) -> list[dict[str, Any]]:
    root = run_dir / "manager"
    records: list[dict[str, Any]] = []
    if not root.is_dir():
        return records
    for decision_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        result_path = decision_dir / "result.json"
        if not result_path.is_file():
            continue
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        decision_number = result.get("decision_number", len(records) + 1)
        if before_decision_number is not None and isinstance(decision_number, int) and decision_number >= before_decision_number:
            continue
        records.append({
            "kind": "manager_decision", "number": decision_number,
            "status": result.get("status"), "step_name": None, "team": None, "selector": None,
            "semantic_summary": result.get("reason"), "plan_delta": None, "signals": [],
            "routing": {"action": result.get("action")},
        })
    return records


def build_manager_context(
    run_dir: Path,
    *,
    level: ManagerLevel = "lite",
    trigger: str = "post_turn",
    decision_number: int | None = None,
    turns: list[dict[str, Any]] | None = None,
    run_metadata: dict[str, Any] | None = None,
    boundary: dict[str, Any] | None = None,
    active_plan_content: str | None = None,
    capture_evidence: bool = False,
) -> dict[str, Any]:
    """Build deterministic manager context from durable run artifacts.

    The exact same function is intentionally suitable for runtime and later
    analysis.  Full is the sole path that reads the active plan text.

    ``capture_evidence`` is true only for live runtime boundaries: it writes
    content-addressed plan/checkpoint evidence (idempotently, reusing any
    existing artifact) before the schema-v3 manifest is assembled. Historical
    rebuilds never write and disclose evidence as unavailable when the exact
    bytes are not already stored.
    """
    run_dir = Path(run_dir)
    run_json_path = run_dir / "run.json"
    if run_metadata is not None:
        run_json = dict(run_metadata)
    else:
        try:
            run_json = json.loads(run_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            run_json = {}
    boundary_was_supplied = boundary is not None
    boundary = dict(boundary or {})
    turns = list(turns) if turns is not None else _load_turns(run_dir)
    if turns:
        finished = turns[-1]
    elif trigger == "pending_notes_invalid":
        # Restored notes are corrected before the resumed run has launched a
        # worker, so no local turn artifact exists yet. The boundary itself is
        # the durable controller evidence for this nonterminal manager call.
        finished = {
            "_turn_dir": str(run_dir),
            "turn_number": boundary.get("finalized_turn_number"),
            "step_name": boundary.get("current_step"),
            "step_role": boundary.get("current_role"),
            "selector": boundary.get("actual_selector"),
            "status": "prelaunch-note-correction",
        }
    else:
        raise ValueError(f"{run_dir}: no finalized workflow turn artifacts found")
    finished_dir = Path(finished["_turn_dir"])
    stdout = _read_text(finished_dir / "stdout.txt") or str(finished.get("stdout", ""))
    stderr = _read_text(finished_dir / "stderr.txt") or str(finished.get("stderr", ""))
    semantic = extract_semantic_result(stdout)
    legacy_boundary = (
        boundary_was_supplied
        and "context_schema_version" not in boundary
    )
    # Boundary inputs are finalized before manager invocation.  They take
    # precedence over mutable run metadata so the same durable inputs rebuild
    # the exact runtime context later.
    if boundary.get("original_plan_path") is not None:
        run_json["original_plan_path"] = boundary["original_plan_path"]
    if boundary.get("active_plan_path") is not None:
        run_json["active_plan_path"] = boundary["active_plan_path"]
    plan_state, active_plan_path = _plan_state(run_dir, run_json, finished)
    captured_plan_state = boundary.get("captured_plan_state")
    if isinstance(captured_plan_state, dict):
        # The controller captured this before invoking the manager.  Reuse it
        # during historical analysis so later plan edits cannot alter the
        # boundary that the manager actually saw.
        plan_state_payload = captured_plan_state
    else:
        plan_state_payload = asdict(plan_state)
    whole_run_progress = analyze_manager_progress(
        turns,
        legacy_reviewer_logic=legacy_boundary,
    )
    if "active_implementation_scope" in boundary:
        scope_was_explicit = True
        scope = boundary.get("active_implementation_scope")
    elif not boundary_was_supplied and "active_implementation_scope" in run_json:
        scope_was_explicit = True
        scope = run_json.get("active_implementation_scope")
    else:
        scope_was_explicit = False
        scope = None
    if legacy_boundary:
        scope_was_explicit = False
        scope = None
    progress_scope = None
    scoped_progress = whole_run_progress
    if isinstance(scope, dict):
        opened_turn_number = scope.get("opened_turn_number")
        if isinstance(opened_turn_number, int):
            scoped_turns = [
                turn for turn in turns
                if int(turn.get("turn_number", 0) or 0) >= opened_turn_number
            ]
            scoped_progress = analyze_manager_progress(scoped_turns)
            carried_rejections = int(
                scope.get("carried_reviewer_rejection_count", 0) or 0
            )
            if carried_rejections:
                scoped_progress = ProgressSignals(
                    unchanged_snapshot_turns=scoped_progress.unchanged_snapshot_turns,
                    same_step_stall_turns=scoped_progress.same_step_stall_turns,
                    alternating_two_step_tail=scoped_progress.alternating_two_step_tail,
                    reviewer_rejection_count=(
                        carried_rejections
                        + scoped_progress.reviewer_rejection_count
                    ),
                    reviewer_non_convergence=(
                        carried_rejections
                        + scoped_progress.reviewer_rejection_count
                    ) >= 2,
                )
            progress_scope = {
                "scope_id": scope.get("scope_id"),
                "opened_turn_number": opened_turn_number,
            }
    elif scope_was_explicit:
        scoped_progress = ProgressSignals(
            unchanged_snapshot_turns=whole_run_progress.unchanged_snapshot_turns,
            same_step_stall_turns=whole_run_progress.same_step_stall_turns,
            alternating_two_step_tail=whole_run_progress.alternating_two_step_tail,
            reviewer_rejection_count=0,
            reviewer_non_convergence=False,
        )
    progress = ProgressSignals(
        unchanged_snapshot_turns=whole_run_progress.unchanged_snapshot_turns,
        same_step_stall_turns=scoped_progress.same_step_stall_turns,
        alternating_two_step_tail=whole_run_progress.alternating_two_step_tail,
        reviewer_rejection_count=scoped_progress.reviewer_rejection_count,
        reviewer_non_convergence=scoped_progress.reviewer_non_convergence,
    )
    progress_payload = asdict(progress)
    if legacy_boundary:
        progress_payload.pop("same_step_stall_turns", None)
    raw_artifacts = [asdict(_artifact_ref(run_dir, finished_dir / name)) for name in ("stdout.txt", "stderr.txt")]
    finished_turn = {
        "turn_number": finished.get("turn_number"), "step_name": finished.get("step_name"),
        "role": finished.get("step_role"), "team": finished.get("team") or run_json.get("team"),
        "selector": finished.get("selector"), "status": finished.get("status"),
        "returncode": finished.get("returncode"),
        "duration_seconds": (
            finished.get("duration_seconds")
            if legacy_boundary
            else _duration_seconds(finished)
        ),
        "semantic_result": asdict(semantic), "error": finished.get("error"),
        "snapshot_before": finished.get("snapshot_before"), "snapshot_after": finished.get("snapshot_after"),
        "snapshot_changed": snapshot_signature(finished.get("snapshot_before")) != snapshot_signature(finished.get("snapshot_after")),
        "proposed_transition": boundary.get("proposed_transition", finished.get("chosen_transition")),
        "recovery": finished.get("recovery_action"),
        "conditions": finished.get("conditions"), "detected_stop": extract_stop_markers(stdout) + extract_stop_markers(stderr),
        "diagnostics": {
            "signals": sorted(set(extract_text_signals("\n".join((stdout, stderr))))),
            "stdout_excerpt": _bounded(stdout),
            "stderr_excerpt": _bounded(stderr),
        },
        "raw_artifacts": raw_artifacts,
    }
    manager_records = _manager_records(run_dir, before_decision_number=decision_number)
    extract = [asdict(_compact_turn(run_dir, turn)) for turn in turns]
    extract.extend(manager_records)
    extract.sort(key=lambda item: (item["number"], item["kind"] != "workflow_turn"))
    context = ManagerContextV1(
        schema_version=MANAGER_CONTEXT_SCHEMA_VERSION,
        run_id=run_dir.name,
        decision_number=decision_number if decision_number is not None else len(manager_records) + 1,
        level=level,
        trigger=trigger,
        finished_turn=finished_turn,
        run_extract=tuple(extract),
        plan_state=plan_state_payload,
        controller_state={
            "baseline_team": boundary.get("baseline_team", run_json.get("team")),
            "actual_team": boundary.get("actual_team"),
            "actual_selector": boundary.get("actual_selector"),
            "turn_budget": {"completed": run_json.get("turns_completed"), "maximum": run_json.get("max_turns")},
            "same_node_counter": (
                progress.unchanged_snapshot_turns
                if legacy_boundary else progress.same_step_stall_turns
            ),
            "semantic_stall_count": (
                progress.unchanged_snapshot_turns
                if legacy_boundary else progress.same_step_stall_turns
            ),
            "reviewer_rejection_count": progress.reviewer_rejection_count, "reviewer_non_convergence": progress.reviewer_non_convergence,
            "progress": progress_payload,
            "proposed_action": boundary.get("proposed_action", "transition"),
            "proposed_next_step": boundary.get("proposed_transition", finished.get("chosen_transition")),
            "terminal": bool(boundary.get("terminal", False)),
            "retry_safely": bool(boundary.get("safely_retryable", False)),
            "operational_failure": bool(boundary.get("operational_failure", False)),
            "backup_route": {
                "team": boundary.get("backup_team"), "selector": boundary.get("backup_selector"),
            },
            "eligible_upgrade": boundary.get("implementation_upgrade"),
            "active_implementation_scope": boundary.get("active_implementation_scope"),
            "eligible_actions": list(boundary.get("eligible_actions", [])),
            "lite_evidence": boundary.get("evidence"),
            "workspace_state": boundary.get("workspace_state", {}),
        },
        active_plan_content=(
            active_plan_content if level == "full"
            else None
        ) if active_plan_content is not None else (
            _read_text(active_plan_path) if level == "full" and active_plan_path is not None else None
        ),
    )
    if not legacy_boundary:
        context.controller_state["progress_scope"] = progress_scope
    # Determine whether to produce schema v2 (boundary selector >= 3).
    boundary_schema_version = boundary.get("context_schema_version")
    use_v2 = (
        isinstance(boundary_schema_version, int)
        and boundary_schema_version >= 3
    )
    if not use_v2:
        # Exact schema-v1 output preserved for selector-2 and selector-absent.
        return json.loads(json.dumps(context.to_dict(), sort_keys=True))
    # --- Schema v2 additions ---
    signal_evidence = classify_turn_text_signals(
        stdout,
        stderr,
        finished.get("status"),
        finished.get("returncode"),
    )
    context.finished_turn["diagnostics"]["signals"] = sorted(
        {item.name for item in signal_evidence}
    )
    context.finished_turn["diagnostics"]["signal_provenance"] = [
        asdict(item) for item in signal_evidence
    ]
    if level == "lite":
        # Lite retains compact routing evidence, never verbatim reviewer output.
        reviewer_turn_numbers = {
            turn.get("turn_number")
            for turn in turns
            if turn.get("step_role") == "reviewer"
        }
        for record in context.run_extract:
            if record.get("number") in reviewer_turn_numbers:
                record["semantic_summary"] = (
                    "Reviewer output withheld from Lite; see the review stdout artifact."
                )
        if finished.get("step_role") == "reviewer":
            semantic_result = context.finished_turn.get("semantic_result")
            if isinstance(semantic_result, dict):
                semantic_result["result"] = (
                    "Reviewer output withheld from Lite; see the review stdout artifact."
                )
            diagnostics = context.finished_turn.get("diagnostics")
            if isinstance(diagnostics, dict):
                diagnostics["stdout_excerpt"] = (
                    "Reviewer output withheld from Lite; see the review stdout artifact."
                )
    scope_pressure = has_scope_pressure(stdout, stderr)
    # Expose scope pressure in controller_state so level selection can force Full.
    context.controller_state["scope_pressure_detected"] = scope_pressure
    # Also carry the exact scope-pressure reason from the boundary when present.
    boundary_pressure_reason = boundary.get("scope_pressure_reason")
    if isinstance(boundary_pressure_reason, str) and boundary_pressure_reason:
        context.controller_state["scope_pressure_reason"] = boundary_pressure_reason

    active_scope = boundary.get("active_implementation_scope")
    active_scope_mapping = active_scope if isinstance(active_scope, Mapping) else None
    active_scope_id = (
        active_scope_mapping.get("scope_id")
        if active_scope_mapping is not None
        else None
    )

    # --- Envelope: resolve, read, hash-check, parse, and include validated payload ---
    validated_envelope: dict[str, Any] | None = None
    envelope_artifact_path = boundary.get("envelope_artifact_path")
    envelope_artifact_sha256 = boundary.get("envelope_artifact_sha256")
    envelope_canonical_sha256 = boundary.get("envelope_canonical_sha256")
    complete_envelope_reference = (
        isinstance(envelope_artifact_path, str) and envelope_artifact_path
        and isinstance(envelope_artifact_sha256, str) and envelope_artifact_sha256
        and isinstance(envelope_canonical_sha256, str) and envelope_canonical_sha256
    )
    if complete_envelope_reference:
        validated_envelope = _resolve_validated_envelope(
            run_dir, envelope_artifact_path,
            envelope_artifact_sha256, envelope_canonical_sha256,
            active_scope_mapping,
        )
    if validated_envelope is not None:
        context.controller_state["repartition_evidence"] = {"status": "validated"}
        envelope = (
            validated_envelope
            if level == "full"
            else {
                "available": True,
                "validated": True,
                "content_included": False,
                "artifact_path": envelope_artifact_path,
                "artifact_sha256": envelope_artifact_sha256,
                "canonical_envelope_sha256": envelope_canonical_sha256,
            }
        )
    else:
        if active_scope_mapping is None:
            envelope_reason = "no active implementation scope was captured at this boundary"
        elif not complete_envelope_reference:
            envelope_reason = "the active implementation scope has incomplete immutable envelope references"
        else:
            envelope_reason = "the captured immutable envelope is unavailable or failed validation"
        context.controller_state["repartition_evidence"] = {
            "status": "unavailable",
            "reason": envelope_reason,
        }
        envelope = _unavailable_envelope(envelope_reason)

    # --- Active-scope rejection ledger: ordered, complete rejection records ---
    rejection_history = boundary.get("review_rejection_history")
    active_rejections: list[dict[str, Any]] = []
    if isinstance(rejection_history, list) and active_scope_id:
        for item in rejection_history:
            if isinstance(item, dict) and item.get("scope_id") == active_scope_id:
                active_rejections.append({
                    "rejection_number": item.get("rejection_number"),
                    "source_run_id": item.get("source_run_id"),
                    "review_turn_number": item.get("review_turn_number"),
                    "review_step_name": item.get("review_step_name"),
                    "reviewer_selector": item.get("reviewer_selector"),
                    "checkpoint_index": item.get("checkpoint_index"),
                    "checkpoint_name": item.get("checkpoint_name"),
                    "reviewed_implementation_turn_number": item.get("reviewed_implementation_turn_number"),
                    "reviewed_worker_team": item.get("reviewed_worker_team"),
                    "reviewed_worker_selector": item.get("reviewed_worker_selector"),
                    "review_summary": item.get("review_summary"),
                    "repair_plan_summary": item.get("repair_plan_summary"),
                    "review_stdout_artifact_path": item.get("review_stdout_artifact_path"),
                    "repair_plan_path": item.get("repair_plan_path"),
                })

    # --- Latest full-rejection detail (for Full level only) ---
    latest_full_rejection: dict[str, Any] | None = None
    if level == "full" and active_rejections:
        latest = active_rejections[-1]
        artifact_path = latest.get("review_stdout_artifact_path")
        latest_full_rejection = dict(latest)
        if isinstance(artifact_path, str) and artifact_path:
            resolved = _resolve_run_artifact(run_dir, artifact_path)
            if resolved is not None:
                latest_full_rejection["exact_reviewer_output"] = _read_text(resolved) or None
            else:
                latest_full_rejection["exact_reviewer_output"] = None
        else:
            latest_full_rejection["exact_reviewer_output"] = None

    # --- Implementation attempts for the active scope ---
    boundary_attempts = boundary.get("implementation_attempts")
    scoped_attempts: dict[str, Any] | None = None
    if isinstance(boundary_attempts, dict) and active_scope_id:
        raw_attempts = boundary_attempts.get(active_scope_id)
        if isinstance(raw_attempts, list):
            scoped_attempts = {
                "scope_id": active_scope_id,
                "attempts": [
                    {
                        "turn_number": a.get("turn_number"),
                        "step_name": a.get("step_name"),
                        "role": a.get("role"),
                        "team": a.get("team"),
                        "selector": a.get("selector"),
                        "outcome": a.get("outcome"),
                        "manager_decision_number": a.get("manager_decision_number"),
                    }
                    for a in raw_attempts if isinstance(a, dict)
                ],
            }

    # --- Manager decisions: only those strictly before the decision being built ---
    manager_decisions = [
        {
            "decision_number": item.get("decision_number") or item.get("number"),
            "action": item.get("action") or (item.get("routing", {}).get("action") if isinstance(item.get("routing"), dict) else None),
            "reason": item.get("reason") or item.get("semantic_summary"),
            "level": item.get("level"),
        }
        for item in _manager_records(run_dir, before_decision_number=decision_number)
    ]

    # --- Change-surface evidence from progress signals ---
    change_surface = {
        "unchanged_snapshot_turns": progress.unchanged_snapshot_turns,
        "same_step_stall_turns": progress.same_step_stall_turns,
        "alternating_two_step_tail": progress.alternating_two_step_tail,
        "reviewer_rejection_count": progress.reviewer_rejection_count,
        "reviewer_non_convergence": progress.reviewer_non_convergence,
    }

    active_plan_identity = boundary.get("target_plan_identity") or boundary.get(
        "checkpoint_identity"
    )
    if not isinstance(active_plan_identity, str):
        active_path = plan_state_payload.get("active_plan_path")
        checkpoint = plan_state_payload.get("current_checkpoint")
        checkpoint_index = checkpoint.get("index") if isinstance(checkpoint, dict) else None
        active_plan_identity = (
            f"{active_path}::checkpoint-{checkpoint_index}"
            if isinstance(active_path, str) and isinstance(checkpoint_index, int)
            else active_path if isinstance(active_path, str) else None
        )
    supplied_scope = boundary.get("manager_note_scope")
    if isinstance(supplied_scope, Mapping):
        # Runtime captures controller-owned scope facts before a manager call.
        # Reusing that immutable evidence keeps artifact reconstruction stable
        # even if either route's plan changes later.
        manager_note_scope = dict(supplied_scope)
    else:
        scope_source = active_plan_content
        if scope_source is None:
            supplied_scope_source = boundary.get("active_plan_content")
            scope_source = (
                supplied_scope_source
                if isinstance(supplied_scope_source, str)
                else context.active_plan_content
            )
        manager_note_scope = build_manager_note_scope(
            active_plan_identity=active_plan_identity,
            active_plan_content=scope_source,
        )
    supplied_retry_scope = boundary.get("retry_manager_note_scope")
    retry_manager_note_scope = (
        dict(supplied_retry_scope)
        if isinstance(supplied_retry_scope, Mapping)
        else None
    )

    # --- Original plan content (Full only) ---
    original_plan_content: str | None = None
    if level == "full":
        immutable_plan_text = (
            validated_envelope.get("plan_text")
            if validated_envelope is not None
            else None
        )
        if isinstance(immutable_plan_text, str):
            original_plan_content = immutable_plan_text
        else:
            original_value = (
                boundary.get("original_plan_path")
                or run_json.get("original_plan_path")
                or run_json.get("plan_path")
            )
            original_path = _path_from_metadata(run_dir, original_value)
            if original_path is not None:
                original_plan_content = _read_text(original_path) or None

    v2_context = ManagerContextV2(
        schema_version=MANAGER_CONTEXT_SCHEMA_VERSION_V2,
        run_id=context.run_id,
        decision_number=context.decision_number,
        level=context.level,
        trigger=context.trigger,
        finished_turn=context.finished_turn,
        run_extract=context.run_extract,
        plan_state=context.plan_state,
        controller_state=context.controller_state,
        active_plan_content=context.active_plan_content,
        original_plan_content=original_plan_content,
        plan_content_disclosure={
            "active_plan_content": (
                "intentionally_omitted"
                if level == "lite"
                else "included" if context.active_plan_content else "unavailable"
            ),
            "original_plan_content": (
                "intentionally_omitted"
                if level == "lite"
                else "included" if original_plan_content else "unavailable"
            ),
        },
        envelope=envelope,
        active_scope_rejection_ledger=tuple(active_rejections),
        implementation_attempts=scoped_attempts,
        manager_decisions=tuple(manager_decisions),
        change_surface_evidence=change_surface,
        manager_note_scope=manager_note_scope,
        retry_manager_note_scope=retry_manager_note_scope,
        scope_pressure_detected=scope_pressure,
    )
    # Attach latest-full-rejection to controller_state for Full only.
    if level == "full" and latest_full_rejection is not None:
        context.controller_state["latest_full_rejection"] = latest_full_rejection
    v2_context_dict = v2_context.to_dict()
    if retry_manager_note_scope is None:
        # The retry destination is meaningful only when it differs from the
        # proposed route. Preserve the flat proposed-route field for legacy
        # consumers without advertising a nonexistent second target.
        v2_context_dict.pop("retry_manager_note_scope", None)
    context_dict = json.loads(json.dumps(v2_context_dict, sort_keys=True))
    # Carry the controller_state additions through the round-trip.
    if level == "full" and latest_full_rejection is not None:
        context_dict["controller_state"]["latest_full_rejection"] = latest_full_rejection
    # --- Schema v3 (selector >= 4): reference-only compact manifest ---
    use_v3 = (
        use_v2
        and isinstance(boundary_schema_version, int)
        and boundary_schema_version >= 4
    )
    if not use_v3:
        return context_dict
    evidence, plan_content_disclosure, checkpoint_info = _capture_v3_evidence(
        run_dir,
        capture=capture_evidence,
        boundary=boundary,
        run_json=run_json,
        finished=finished,
        plan_state_payload=plan_state_payload,
        validated_envelope=validated_envelope,
    )
    finished_turn = context.finished_turn
    semantic_payload = finished_turn.get("semantic_result")
    semantic_payload = dict(semantic_payload) if isinstance(semantic_payload, Mapping) else {}
    if finished.get("step_role") == "reviewer":
        semantic_payload["result"] = (
            "Reviewer output is referenced by artifact; see the review stdout artifact."
        )
        semantic_payload["fallback"] = False
    else:
        semantic_payload["result"] = _v3_bounded_text(semantic_payload.get("result")) or ""
        semantic_payload["fallback"] = bool(semantic_payload.get("fallback"))
    v3_finished_turn = {
        "turn_number": finished_turn.get("turn_number"),
        "step_name": finished_turn.get("step_name"),
        "role": finished_turn.get("role"),
        "team": finished_turn.get("team"),
        "selector": finished_turn.get("selector"),
        "status": finished_turn.get("status"),
        "returncode": finished_turn.get("returncode"),
        "duration_seconds": finished_turn.get("duration_seconds"),
        "semantic_result": semantic_payload,
        "error": _v3_bounded_text(finished_turn.get("error")),
        "snapshot_before": finished_turn.get("snapshot_before"),
        "snapshot_after": finished_turn.get("snapshot_after"),
        "snapshot_changed": finished_turn.get("snapshot_changed"),
        "proposed_transition": finished_turn.get("proposed_transition"),
        "recovery": finished_turn.get("recovery"),
        "conditions": finished_turn.get("conditions"),
        "detected_stop": finished_turn.get("detected_stop"),
        "diagnostics": {
            "signals": (
                finished_turn.get("diagnostics", {}).get("signals")
                if isinstance(finished_turn.get("diagnostics"), Mapping)
                else []
            ),
            "signal_provenance": (
                finished_turn.get("diagnostics", {}).get("signal_provenance")
                if isinstance(finished_turn.get("diagnostics"), Mapping)
                else None
            ),
            "stdout_excerpt": (
                "Reviewer output is referenced by artifact; see the review stdout artifact."
                if finished.get("step_role") == "reviewer"
                else _v3_bounded_text(stdout)
            ),
            "stderr_excerpt": _v3_bounded_text(stderr),
        },
        "raw_artifacts": finished_turn.get("raw_artifacts"),
    }
    v3_controller_state = {
        key: value
        for key, value in context.controller_state.items()
        if key not in {"latest_full_rejection", "repartition_evidence"}
    }
    if validated_envelope is not None:
        summary: dict[str, Any] = {
            "available": True,
            "validated": True,
            "artifact_path": envelope_artifact_path,
            "artifact_sha256": envelope_artifact_sha256,
            "canonical_envelope_sha256": envelope_canonical_sha256,
            "schema_version": validated_envelope.get("schema_version"),
            "scope_id": validated_envelope.get("scope_id"),
            "checkpoint_index": validated_envelope.get("checkpoint_index"),
            "checkpoint_name": validated_envelope.get("checkpoint_name"),
        }
        for field in (
            "checkpoint_line_start", "checkpoint_line_end",
            "checkpoint_byte_start", "checkpoint_byte_end",
        ):
            if field in validated_envelope:
                summary[field] = validated_envelope[field]
        if isinstance(validated_envelope.get("plan_ref"), Mapping):
            summary["plan_ref"] = _v3_ref_dict(validated_envelope.get("plan_ref"))
            summary["checkpoint_ref"] = _v3_ref_dict(
                validated_envelope.get("checkpoint_ref")
            )
        else:
            summary["plan_sha256"] = validated_envelope.get("plan_sha256")
            summary["checkpoint_sha256"] = validated_envelope.get("checkpoint_sha256")
        v3_controller_state["repartition_evidence"] = {
            "status": "validated",
            "envelope_summary": summary,
        }
    else:
        envelope_reason = "no active implementation scope was captured at this boundary"
        if active_scope_mapping is not None:
            envelope_reason = (
                "the captured immutable envelope is unavailable or failed validation"
                if complete_envelope_reference
                else "the active implementation scope has incomplete immutable envelope references"
            )
        v3_controller_state["repartition_evidence"] = {
            "status": "unavailable",
            "reason": envelope_reason,
        }
    if latest_full_rejection is not None:
        summary_rejection = {
            key: value for key, value in latest_full_rejection.items()
            if key != "exact_reviewer_output"
        }
        for key in ("review_summary", "repair_plan_summary"):
            if key in summary_rejection:
                summary_rejection[key] = _v3_bounded_text(summary_rejection[key])
        v3_controller_state["latest_full_rejection"] = summary_rejection
    v3_ledger = tuple({
        "rejection_number": row.get("rejection_number"),
        "source_run_id": row.get("source_run_id"),
        "review_turn_number": row.get("review_turn_number"),
        "review_step_name": row.get("review_step_name"),
        "reviewer_selector": row.get("reviewer_selector"),
        "checkpoint_index": row.get("checkpoint_index"),
        "checkpoint_name": row.get("checkpoint_name"),
        "reviewed_implementation_turn_number": row.get("reviewed_implementation_turn_number"),
        "reviewed_worker_team": row.get("reviewed_worker_team"),
        "reviewed_worker_selector": row.get("reviewed_worker_selector"),
        "review_summary": _v3_bounded_text(row.get("review_summary")),
        "repair_plan_summary": _v3_bounded_text(row.get("repair_plan_summary")),
        "review_stdout_artifact_path": row.get("review_stdout_artifact_path"),
        "repair_plan_path": row.get("repair_plan_path"),
    } for row in active_rejections)
    v3_manager_decisions = tuple({
        "decision_number": row.get("decision_number"),
        "action": row.get("action"),
        "reason": _v3_bounded_text(row.get("reason")),
        "level": row.get("level"),
    } for row in manager_decisions)
    v3_run_extract = context.run_extract
    reviewer_turn_numbers = {
        turn.get("turn_number")
        for turn in turns
        if turn.get("step_role") == "reviewer"
    }
    bounded_extract: list[dict[str, Any]] = []
    for record in v3_run_extract:
        record = dict(record)
        if record.get("kind") == "manager_decision":
            record["semantic_summary"] = _v3_bounded_text(record.get("semantic_summary"))
        elif record.get("number") in reviewer_turn_numbers:
            record["semantic_summary"] = (
                "Reviewer output is referenced by artifact; see the review stdout artifact."
            )
        else:
            record["semantic_summary"] = _v3_bounded_text(record.get("semantic_summary"))
        bounded_extract.append(record)
    bounded_extract.sort(key=lambda item: (item.get("number", 0), item.get("kind") != "workflow_turn"))
    v3_run_extract = tuple(bounded_extract[-MANAGER_RUN_EXTRACT_MAX_RECORDS:])
    v3_context = ManagerContextV3(
        schema_version=MANAGER_CONTEXT_SCHEMA_VERSION_V3,
        run_id=context.run_id,
        decision_number=context.decision_number,
        level=context.level,
        trigger=context.trigger,
        finished_turn=v3_finished_turn,
        run_extract=v3_run_extract,
        plan_state=context.plan_state,
        controller_state=v3_controller_state,
        evidence=evidence,
        plan_content_disclosure=plan_content_disclosure,
        active_scope_rejection_ledger=v3_ledger,
        implementation_attempts=scoped_attempts,
        manager_decisions=v3_manager_decisions,
        change_surface_evidence=change_surface,
        manager_note_scope=manager_note_scope,
        retry_manager_note_scope=retry_manager_note_scope,
        scope_pressure_detected=scope_pressure,
    )
    v3_dict = v3_context.to_dict()
    if retry_manager_note_scope is None:
        v3_dict.pop("retry_manager_note_scope", None)
    return json.loads(json.dumps(v3_dict, sort_keys=True))
