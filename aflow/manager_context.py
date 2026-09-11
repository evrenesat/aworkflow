"""Versioned, plan-safe semantic evidence for manager supervision.

The builder deliberately reads turn artifacts and result metadata only. Lite
never exposes prompt artifacts or active-plan prose; runtime may provide a
captured plan solely to derive bounded controller-owned scope metadata.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
from .harnesses.session import (
    extract_structured_final_assistant_text,
    select_agent_semantic_output,
)
from .plan import PlanParseError, load_plan_tolerant, parse_plan_text
from .repartition import parse_envelope_bytes
from .scope_pressure import has_scope_pressure
from .stop_marker import (
    AGENT_OUTPUT_CONTRACT,
    FINAL_TEXT_OUTPUT_SOURCE,
    OutputContract,
    SemanticOutputSource,
    extract_trusted_stop_markers,
    resolve_output_contract,
    resolve_semantic_output_source,
)


MANAGER_CONTEXT_SCHEMA_VERSION = 1
MANAGER_CONTEXT_SCHEMA_VERSION_V2 = 2
MANAGER_CONTEXT_SCHEMA_VERSION_V3 = 3
ORIGINAL_CHECKPOINT_AUTHORITY_VERSION = 1
DIAGNOSTIC_LIMIT = 2_000
MAX_MANAGER_NOTE_SCOPE_PATHS = 32
MAX_MANAGER_NOTE_SCOPE_PATH_LENGTH = 240
MAX_MANAGER_NOTE_SCOPE_IDENTITY_LENGTH = 512
# Prompt budgets: the inline manager user manifest targets 16 KiB and is
# hard-limited to 40 KiB before any provider process starts.
MANAGER_INLINE_CONTEXT_TARGET_BYTES = 16 * 1024
MANAGER_INLINE_CONTEXT_MAX_BYTES = 40 * 1024
# Keep the latest human-readable turn summary small enough that current
# decision facts and immutable references remain the first-class input.
MANAGER_LATEST_TURN_SUMMARY_MAX_BYTES = 512
MANAGER_SUMMARY_MAX_CHARS = 2_000
MANAGER_RUN_EXTRACT_MAX_RECORDS = 12
MANAGER_HISTORY_ARTIFACT_SCHEMA_VERSION = 1
MANAGER_HISTORY_RELATIVE_PATH_PATTERN = (
    ".aflow/runs/{run_id}/evidence/manager-history/{sha256}.json"
)
# One shared deterministic truncation marker for every bounded semantic
# field in schema-v3 contexts (decisions, rejections, diagnostics).
TRUNCATION_MARKER = "\n[evidence excerpt truncated]"
REFERENCE_ONLY_SEMANTIC_RESULT = (
    "Semantic output is referenced by artifact; see the stdout artifact."
)
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
class _OriginalPlanAuthority:
    """Controller-validated source for checkpoint state in a live context."""

    path: Path | None = None
    text: str | None = None
    reason: str | None = None


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
    history_disclosure: dict[str, Any] = field(default_factory=dict)
    history_summary: dict[str, Any] = field(default_factory=dict)
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


def extract_semantic_result(
    stdout: str,
    *,
    output_source: SemanticOutputSource = FINAL_TEXT_OUTPUT_SOURCE,
) -> SemanticTurnOutcome:
    """Extract assistant text only when the artifact declares transport."""
    output_source = resolve_semantic_output_source(output_source)
    if output_source == FINAL_TEXT_OUTPUT_SOURCE:
        return SemanticTurnOutcome("plain_text", stdout, False)
    structured = extract_structured_final_assistant_text(stdout)
    if structured is not None:
        if structured:
            return SemanticTurnOutcome("structured_stream", structured, False)
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


def _turn_output_contract(turn: Mapping[str, Any]) -> OutputContract:
    """Read the explicit semantic-stream contract, with a legacy fallback."""
    turn_dir = turn.get("_turn_dir")
    artifact_dir = Path(turn_dir) if isinstance(turn_dir, (str, Path)) else None
    return resolve_output_contract(
        turn.get("output_contract"), artifact_dir=artifact_dir
    )


def _turn_semantic_output_source(turn: Mapping[str, Any]) -> SemanticOutputSource:
    """Resolve the format of the persisted stdout artifact."""
    turn_dir = turn.get("_turn_dir")
    artifact_dir = Path(turn_dir) if isinstance(turn_dir, (str, Path)) else None
    return resolve_semantic_output_source(
        turn.get("semantic_output_source"), artifact_dir=artifact_dir
    )


def _select_turn_semantic_output(
    turn: Mapping[str, Any],
    stdout: str,
    *,
    output_contract: OutputContract,
) -> str:
    if output_contract != AGENT_OUTPUT_CONTRACT:
        return stdout
    return select_agent_semantic_output(
        stdout, output_source=_turn_semantic_output_source(turn)
    )


def _extract_turn_semantic_result(
    turn: Mapping[str, Any],
    stdout: str,
    *,
    output_contract: OutputContract,
) -> SemanticTurnOutcome:
    if output_contract != AGENT_OUTPUT_CONTRACT:
        return extract_semantic_result(stdout)
    return extract_semantic_result(
        stdout, output_source=_turn_semantic_output_source(turn)
    )


def _turn_signal_evidence(
    turn: Mapping[str, Any],
    semantic_stdout: str,
    stderr: str,
    *,
    output_contract: OutputContract,
) -> list[Any]:
    """Classify selected semantic text while retaining command stream parity."""
    if output_contract == AGENT_OUTPUT_CONTRACT:
        return classify_turn_text_signals(
            semantic_stdout,
            stderr,
            turn.get("status"),
            turn.get("returncode"),
        )
    return classify_turn_text_signals(
        "\n".join((semantic_stdout, stderr)),
        "",
        turn.get("status"),
        turn.get("returncode"),
    )


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


def _scope_has_checkpoint_identity(scope: Mapping[str, Any] | None) -> bool:
    if not isinstance(scope, Mapping):
        return False
    scope_id = scope.get("scope_id")
    checkpoint_index = scope.get("checkpoint_index")
    checkpoint_name = scope.get("checkpoint_name")
    return (
        isinstance(scope_id, str)
        and bool(scope_id.strip())
        and isinstance(checkpoint_index, int)
        and not isinstance(checkpoint_index, bool)
        and checkpoint_index > 0
        and isinstance(checkpoint_name, str)
        and bool(checkpoint_name.strip())
    )


def _read_evidence_text(
    run_dir: Path,
    reference: Mapping[str, Any],
) -> str | None:
    """Read one validated content-addressed evidence reference as UTF-8."""
    from .runlog import resolve_evidence_artifact

    try:
        raw = resolve_evidence_artifact(_v3_run_paths(run_dir), reference)
        return raw.decode("utf-8", "strict")
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def _text_matches_evidence_reference(
    text: str,
    reference: Mapping[str, Any],
) -> bool:
    raw = text.encode("utf-8")
    return (
        reference.get("sha256") == hashlib.sha256(raw).hexdigest()
        and reference.get("byte_size") == len(raw)
    )


def _envelope_plan_text(
    run_dir: Path,
    envelope: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """Resolve the exact original-plan bytes from a validated envelope."""
    embedded = envelope.get("plan_text")
    if isinstance(embedded, str):
        return embedded, None
    if "plan_text" in envelope and embedded is not None:
        return None, "invalid_evidence"
    reference = envelope.get("plan_ref")
    if not isinstance(reference, Mapping):
        return None, "invalid_evidence"
    text = _read_evidence_text(run_dir, reference)
    if text is None:
        return None, "invalid_evidence"
    return text, None


def _resolve_original_plan_authority(
    run_dir: Path,
    *,
    run_json: Mapping[str, Any],
    finished_turn: Mapping[str, Any],
    boundary: Mapping[str, Any],
    scope: Mapping[str, Any] | None,
    validated_envelope: Mapping[str, Any] | None,
) -> _OriginalPlanAuthority | None:
    """Resolve original checkpoint authority without promoting a repair overlay.

    A complete envelope reference is authoritative when present.  A boundary
    copy may replace a pruned v2 evidence artifact only when its bytes match
    the envelope reference.  Without immutable envelope fields, the declared
    original path or exact boundary copy is the only fallback.  Invalid scope
    evidence deliberately returns an unavailable authority instead of
    falling through to the active overlay.
    """
    if not _scope_has_checkpoint_identity(scope):
        return None

    original_value = (
        boundary.get("original_plan_path")
        or run_json.get("original_plan_path")
        or finished_turn.get("original_plan_path")
        or run_json.get("plan_path")
    )
    original_path = _path_from_metadata(run_dir, original_value)
    envelope_values = tuple(
        boundary.get(key)
        for key in (
            "envelope_artifact_path",
            "envelope_artifact_sha256",
            "envelope_canonical_sha256",
        )
    )
    if any(value is not None for value in envelope_values):
        if not all(isinstance(value, str) and value.strip() for value in envelope_values):
            return _OriginalPlanAuthority(path=original_path, reason="invalid_evidence")
        if validated_envelope is None:
            return _OriginalPlanAuthority(path=original_path, reason="invalid_evidence")

        envelope_text, envelope_reason = _envelope_plan_text(
            run_dir, validated_envelope
        )
        if envelope_text is not None:
            return _OriginalPlanAuthority(path=original_path, text=envelope_text)

        # A resumed v2 run can retain the exact boundary copy after its source
        # evidence was pruned.  It is usable only when it still agrees with
        # the immutable envelope reference.
        boundary_text = boundary.get("original_plan_content")
        plan_reference = validated_envelope.get("plan_ref")
        if (
            isinstance(boundary_text, str)
            and isinstance(plan_reference, Mapping)
            and _text_matches_evidence_reference(boundary_text, plan_reference)
        ):
            return _OriginalPlanAuthority(path=original_path, text=boundary_text)
        return _OriginalPlanAuthority(
            path=original_path,
            reason=envelope_reason or "invalid_evidence",
        )

    boundary_text = boundary.get("original_plan_content")
    if isinstance(boundary_text, str):
        return _OriginalPlanAuthority(path=original_path, text=boundary_text)
    return _OriginalPlanAuthority(path=original_path)


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


def _utf8_prefix(value: str, max_bytes: int) -> str:
    """Return a valid-UTF-8 prefix no larger than ``max_bytes``."""
    if max_bytes <= 0:
        return ""
    return value.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def _v3_bounded_text(
    value: Any,
    *,
    max_bytes: int = MANAGER_LATEST_TURN_SUMMARY_MAX_BYTES,
) -> str | None:
    """Bound one schema-v3 semantic field by UTF-8 bytes.

    The marker is included in the limit.  Truncating encoded bytes and
    decoding with ``ignore`` deliberately drops only an incomplete final code
    point, so every returned string remains valid UTF-8.
    """
    if not isinstance(value, str):
        return None
    if len(value.encode("utf-8")) <= max_bytes:
        return value
    marker = TRUNCATION_MARKER
    marker_bytes = len(marker.encode("utf-8"))
    if marker_bytes >= max_bytes:
        return _utf8_prefix(marker, max_bytes)
    return _utf8_prefix(value, max_bytes - marker_bytes) + marker


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
    original_authority: _OriginalPlanAuthority | None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    """Capture/reuse content-addressed plan and checkpoint evidence.

    ``capture`` is true only for live runtime boundaries; historical rebuilds
    never write. Equal active/original bytes share one plan artifact. A
    repair overlay differing from the envelope original gets its own artifact.

    Fail-closed policy: when ``capture`` is true, evidence-store failures
    (storage, hash mismatch, symlink or containment violations) raise and
    abort context construction before any provider invocation — they are
    never downgraded to "unavailable" evidence. "Unavailable" is reserved for
    read-only historical reconstruction and for content that is legitimately
    absent at the boundary (no active plan bytes, no live checkpoint slice).

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
    authority_invalid = (
        original_authority is not None
        and original_authority.reason is not None
    )
    envelope_plan_ref: Any = None
    envelope_text_authoritative = False
    if (
        not authority_invalid
        and validated_envelope is not None
        and validated_envelope.get("available")
    ):
        envelope_plan_ref = validated_envelope.get("plan_ref")
        if not isinstance(envelope_plan_ref, Mapping):
            # Schema-v1 envelope payload embeds the immutable plan text.
            envelope_text_authoritative = True
            embedded = validated_envelope.get("plan_text")
            original_text = embedded if isinstance(embedded, str) else None
    if not authority_invalid and original_authority is not None:
        if original_authority.text is not None:
            original_text = original_authority.text
        elif capture and original_authority.path is not None:
            original_text = _read_text(original_authority.path) or None
    if (
        not authority_invalid
        and not envelope_text_authoritative
        and original_text is None
        and original_authority is None
    ):
        # The boundary persists the exact original bytes captured at runtime
        # so historical rebuilds never depend on later file mutations. This
        # also covers v2 envelopes whose referenced evidence cannot be
        # resolved from this run's store (e.g. a resumed run whose source
        # evidence was pruned): the durable boundary copy re-captures it.
        boundary_original = boundary.get("original_plan_content")
        if isinstance(boundary_original, str):
            original_text = boundary_original
        elif capture:
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
        if capture:
            # Live capture failures (storage, hash mismatch, symlink or
            # containment violation) abort context construction: they are
            # never downgraded to "unavailable" evidence before a provider.
            active_ref = capture_plan_evidence(paths, active_text)
            active_plan_entry = _v3_reference_entry(active_ref)
        else:
            digest = hashlib.sha256(active_text.encode("utf-8")).hexdigest()
            from .runlog import evidence_reference

            active_ref = evidence_reference(paths, "plan", digest, len(active_text.encode("utf-8")))
            if available_reference(active_ref) is not None:
                active_plan_entry = _v3_reference_entry(active_ref)

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
        elif capture:
            # Live capture failures propagate (never "unavailable" evidence).
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
            if available_reference(original_ref) is not None:
                original_entry = {
                    "available": True,
                    "reference": _v3_ref_dict(original_ref),
                    "source": "boundary_plan_file",
                }

    # --- Current checkpoint, always bound to the original scope authority ---
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

    def envelope_checkpoint_info(envelope: Mapping[str, Any]) -> dict[str, Any]:
        values = {
            "checkpoint_index": envelope.get("checkpoint_index"),
            "checkpoint_name": envelope.get("checkpoint_name"),
            "line_start": envelope.get("checkpoint_line_start"),
            "line_end": envelope.get("checkpoint_line_end"),
            "byte_start": envelope.get("checkpoint_byte_start"),
            "byte_end": envelope.get("checkpoint_byte_end"),
        }
        if (
            isinstance(values["checkpoint_index"], int)
            and isinstance(values["checkpoint_name"], str)
            and all(
                isinstance(values[key], int)
                for key in ("line_start", "line_end", "byte_start", "byte_end")
            )
        ):
            return values
        return {}

    def checkpoint_entry_for_text(
        checkpoint_text: str,
        info: dict[str, Any],
        expected_reference: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        raw = checkpoint_text.encode("utf-8")
        if expected_reference is not None and not _text_matches_evidence_reference(
            checkpoint_text, expected_reference
        ):
            return None
        if expected_reference is not None:
            resolved = available_reference(expected_reference)
            if resolved is not None:
                return {"available": True, "reference": resolved, **info}
        if capture:
            # Live checkpoint capture failures propagate and abort context
            # construction instead of binding evidence to the overlay.
            captured = capture_checkpoint_evidence(paths, checkpoint_text)
            captured_dict = _v3_ref_dict(captured)
            if expected_reference is not None and (
                captured_dict.get("sha256") != expected_reference.get("sha256")
                or captured_dict.get("byte_size") != expected_reference.get("byte_size")
            ):
                return None
            return {"available": True, "reference": captured_dict, **info}
        from .runlog import evidence_reference

        reference = evidence_reference(paths, "checkpoint", hashlib.sha256(raw).hexdigest(), len(raw))
        resolved = available_reference(reference)
        if resolved is None:
            return None
        return {"available": True, "reference": resolved, **info}

    if original_authority is not None and not authority_invalid:
        expected_checkpoint_reference: Mapping[str, Any] | None = None
        if validated_envelope is not None and validated_envelope.get("available"):
            envelope_info = envelope_checkpoint_info(validated_envelope)
            if envelope_info:
                checkpoint_info = envelope_info
            checkpoint_reference = validated_envelope.get("checkpoint_ref")
            if isinstance(checkpoint_reference, Mapping):
                expected_checkpoint_reference = checkpoint_reference
            else:
                embedded_checkpoint = validated_envelope.get("checkpoint_text")
                if isinstance(embedded_checkpoint, str) and checkpoint_info:
                    checkpoint_entry = checkpoint_entry_for_text(
                        embedded_checkpoint, checkpoint_info
                    ) or checkpoint_entry

        if original_text is not None and isinstance(checkpoint_index, int):
            try:
                source_slice = slice_checkpoint_source(
                    original_text, checkpoint_index=checkpoint_index
                )
            except ValueError:
                source_slice = None
            if source_slice is not None:
                source_info = {
                    "checkpoint_index": source_slice.checkpoint_index,
                    "checkpoint_name": source_slice.checkpoint_name,
                    "line_start": source_slice.heading_line,
                    "line_end": (
                        original_text.encode("utf-8")[: source_slice.checkpoint_byte_end]
                        .decode("utf-8", "strict")
                        .count("\n")
                        + 1
                    ),
                    "byte_start": source_slice.checkpoint_byte_start,
                    "byte_end": source_slice.checkpoint_byte_end,
                }
                if not checkpoint_info or source_info == checkpoint_info:
                    checkpoint_info = source_info
                    checkpoint_entry = checkpoint_entry_for_text(
                        source_slice.full_text,
                        checkpoint_info,
                        expected_checkpoint_reference,
                    ) or checkpoint_entry
    elif original_authority is None and active_text is not None and isinstance(checkpoint_index, int):
        # Preserve the legacy unscoped behavior.  A scoped repair never takes
        # this branch, so an unheaded overlay cannot masquerade as a checkpoint.
        try:
            source_slice = slice_checkpoint_source(
                active_text, checkpoint_index=checkpoint_index
            )
        except ValueError:
            source_slice = None
        if source_slice is not None:
            plan_bytes = active_text.encode("utf-8")
            checkpoint_info = {
                "checkpoint_index": source_slice.checkpoint_index,
                "checkpoint_name": source_slice.checkpoint_name,
                "line_start": source_slice.heading_line,
                "line_end": (
                    plan_bytes[: source_slice.checkpoint_byte_end]
                    .decode("utf-8", "strict")
                    .count("\n")
                    + 1
                ),
                "byte_start": source_slice.checkpoint_byte_start,
                "byte_end": source_slice.checkpoint_byte_end,
            }
            checkpoint_entry = checkpoint_entry_for_text(
                source_slice.full_text, checkpoint_info
            ) or checkpoint_entry

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


def _recorded_manager_history_reference(
    run_dir: Path,
    *,
    boundary: Mapping[str, Any],
    run_json: Mapping[str, Any],
    decision_number: int | None,
) -> Mapping[str, Any] | None:
    """Find the exact previously recorded history reference, if available."""
    candidates: list[Any] = [
        boundary.get("manager_history_reference"),
        run_json.get("manager_history_reference"),
    ]
    for container in (boundary.get("evidence"), run_json.get("evidence")):
        if isinstance(container, Mapping):
            candidates.append(container.get("manager_history"))

    # The decision context is the precise durable record for a completed
    # boundary.  Read only that decision's context; never search arbitrary
    # history artifacts for a plausible replacement.
    if isinstance(decision_number, int) and decision_number >= 1:
        context_path = (
            run_dir / "manager" / f"decision-{decision_number:03d}" / "context.json"
        )
        try:
            saved = json.loads(context_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            saved = None
        if isinstance(saved, Mapping):
            saved_evidence = saved.get("evidence")
            if isinstance(saved_evidence, Mapping):
                candidates.append(saved_evidence.get("manager_history"))

    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        reference = candidate.get("reference")
        if isinstance(reference, Mapping):
            return reference
        if all(key in candidate for key in ("kind", "path", "sha256", "byte_size")):
            return candidate
    return None


def _capture_manager_history_evidence(
    run_dir: Path,
    *,
    capture: bool,
    boundary: Mapping[str, Any],
    run_json: Mapping[str, Any],
    decision_number: int | None,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Store or resolve one immutable structured manager-history artifact."""
    from .runlog import (
        evidence_reference,
        resolve_evidence_artifact,
        store_evidence_artifact,
    )

    paths = _v3_run_paths(run_dir)
    data = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if capture:
        reference = store_evidence_artifact(
            paths,
            kind="manager_history",
            data=data,
        )
        return {
            "available": True,
            "reference": _v3_ref_dict(reference),
        }

    recorded = _recorded_manager_history_reference(
        run_dir,
        boundary=boundary,
        run_json=run_json,
        decision_number=decision_number,
    )
    if recorded is not None:
        try:
            resolve_evidence_artifact(paths, recorded)
        except (OSError, ValueError):
            return {
                "available": False,
                "reason": "the recorded manager history artifact is unavailable or failed validation",
                "source_run_id": run_dir.name,
                "artifact_root": str(run_dir.resolve()),
                "relative_path_pattern": MANAGER_HISTORY_RELATIVE_PATH_PATTERN,
            }
        return {
            "available": True,
            "reference": _v3_ref_dict(recorded),
        }

    digest = hashlib.sha256(data).hexdigest()
    expected = evidence_reference(
        paths,
        "manager_history",
        digest,
        len(data),
    )
    try:
        resolve_evidence_artifact(paths, expected)
    except (OSError, ValueError):
        return {
            "available": False,
            "reason": "the manager history artifact is unavailable in read-only mode",
            "source_run_id": run_dir.name,
            "artifact_root": str(run_dir.resolve()),
            "relative_path_pattern": MANAGER_HISTORY_RELATIVE_PATH_PATTERN,
            "expected_reference": _v3_ref_dict(expected),
        }
    return {
        "available": True,
        "reference": _v3_ref_dict(expected),
    }


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


def _plan_state_from_parsed(
    parsed: Any,
    *,
    parse_error: str | None,
    original_value: Any,
    active_value: Any,
    active_path: Path | None,
) -> tuple[StructuredPlanState, Path | None]:
    checkpoints = tuple({
        "index": index,
        "name": section.name,
        "heading_checked": section.heading_checked,
        "checked_step_count": section.checked_step_count,
        "unchecked_step_count": section.unchecked_step_count,
    } for index, section in enumerate(parsed.sections, start=1))
    snapshot = parsed.snapshot
    current = None
    if snapshot.current_checkpoint_index is not None:
        current = next(
            (
                item for item in checkpoints
                if item["index"] == snapshot.current_checkpoint_index
            ),
            None,
        )
    return StructuredPlanState(
        original_plan_path=(
            str(original_value) if isinstance(original_value, str) else None
        ),
        active_plan_path=(
            str(active_value) if isinstance(active_value, str) else None
        ),
        active_repair_plan=bool(
            active_value and original_value and active_value != original_value
        ),
        checkpoints=checkpoints,
        current_checkpoint=current,
        is_complete=snapshot.is_complete,
        parse_error=parse_error,
    ), active_path


def _plan_state(
    run_dir: Path,
    run_json: dict[str, Any],
    finished_turn: dict[str, Any],
    *,
    original_authority: _OriginalPlanAuthority | None = None,
) -> tuple[StructuredPlanState, Path | None]:
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
    source_path = (
        original_authority.path
        if original_authority is not None
        else active_path
    )
    if original_authority is not None and original_authority.reason is not None:
        return StructuredPlanState(
            original_plan_path=(
                str(original_value) if isinstance(original_value, str) else None
            ),
            active_plan_path=(
                str(active_value) if isinstance(active_value, str) else None
            ),
            active_repair_plan=bool(
                active_value and original_value and active_value != original_value
            ),
            checkpoints=(),
            current_checkpoint=None,
            is_complete=None,
            parse_error=original_authority.reason,
        ), active_path

    if original_authority is not None and original_authority.text is not None:
        try:
            parsed = parse_plan_text(
                original_authority.text,
                source_path=source_path or Path("<captured original plan>"),
            )
            return _plan_state_from_parsed(
                parsed,
                parse_error=None,
                original_value=original_value,
                active_value=active_value,
                active_path=active_path,
            )
        except (PlanParseError, ValueError) as exc:
            return StructuredPlanState(
                original_plan_path=(
                    str(original_value) if isinstance(original_value, str) else None
                ),
                active_plan_path=(
                    str(active_value) if isinstance(active_value, str) else None
                ),
                active_repair_plan=bool(
                    active_value and original_value and active_value != original_value
                ),
                checkpoints=(),
                current_checkpoint=None,
                is_complete=None,
                parse_error=str(exc),
            ), active_path

    if source_path is not None:
        try:
            loaded = load_plan_tolerant(source_path)
            return _plan_state_from_parsed(
                loaded.parsed_plan,
                parse_error=str(loaded.parse_error) if loaded.parse_error else None,
                original_value=original_value,
                active_value=active_value,
                active_path=active_path,
            )
        except (OSError, PlanParseError, ValueError) as exc:
            parse_error = str(exc)
    else:
        parse_error = None
    return StructuredPlanState(
        original_plan_path=(
            str(original_value) if isinstance(original_value, str) else None
        ),
        active_plan_path=(
            str(active_value) if isinstance(active_value, str) else None
        ),
        active_repair_plan=bool(
            active_value and original_value and active_value != original_value
        ),
        checkpoints=(),
        current_checkpoint=None,
        is_complete=None,
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
    stdout = _read_text(turn_dir / "stdout.txt") or str(turn.get("stdout", ""))
    stderr = _read_text(turn_dir / "stderr.txt") or str(turn.get("stderr", ""))
    output_contract = _turn_output_contract(turn)
    semantic_stdout = _select_turn_semantic_output(
        turn, stdout, output_contract=output_contract
    )
    semantic = _extract_turn_semantic_result(
        turn, stdout, output_contract=output_contract
    )
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
        signals=tuple(sorted(set(
            extract_text_signals(semantic_stdout)
            + (["explicit_stop"] if extract_trusted_stop_markers(
                semantic_stdout, stderr, output_contract=output_contract
            ) else [])
        ))),
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


def _manager_history_records(
    run_dir: Path, *, before_decision_number: int | None = None
) -> list[dict[str, Any]]:
    """Read manager history with explicit decision/turn domains for schema v3."""
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
        raw_number = result.get("decision_number")
        if isinstance(raw_number, int) and not isinstance(raw_number, bool):
            decision_number = raw_number
        else:
            decision_number = len(records) + 1
        if (
            before_decision_number is not None
            and decision_number >= before_decision_number
        ):
            continue
        raw_turn_number = result.get("finalized_turn_number")
        turn_number = (
            raw_turn_number
            if isinstance(raw_turn_number, int) and not isinstance(raw_turn_number, bool)
            else None
        )
        action = result.get("action")
        reason = result.get("reason")
        records.append({
            "kind": "manager_decision",
            "decision_number": decision_number,
            "turn_number": turn_number,
            "status": result.get("status") if isinstance(result.get("status"), str) else None,
            "level": result.get("level") if isinstance(result.get("level"), str) else None,
            "trigger": result.get("trigger") if isinstance(result.get("trigger"), str) else None,
            "action": action if isinstance(action, str) else None,
            "reason": reason if isinstance(reason, str) else None,
            "semantic_summary": reason if isinstance(reason, str) else None,
            "routing": {"action": action} if isinstance(action, str) else {},
            "artifact_path": str(decision_dir.relative_to(run_dir)),
        })
    records.sort(key=lambda item: item["decision_number"])
    return records


def _v3_workflow_history_records(
    run_dir: Path, turns: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Convert workflow turns to a v3 record with an explicit turn number."""
    records: list[dict[str, Any]] = []
    for turn in turns:
        compact = asdict(_compact_turn(run_dir, turn))
        turn_number = compact.pop("number")
        compact.update({
            "number": turn_number,
            "turn_number": turn_number,
            "artifact_path": f"turns/turn-{turn_number:03d}",
        })
        records.append(compact)
    records.sort(key=lambda item: item["turn_number"])
    return records


def _full_v3_workflow_history_records(
    run_dir: Path, turns: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build the complete structured turn collection for disk-backed history.

    Only parsed semantic output, bounded controller signals, and durable
    artifact paths are retained.  Raw stdout/stderr bodies never enter this
    artifact, and reviewer output remains available through its declared
    turn-artifact reference.
    """
    records = _v3_workflow_history_records(run_dir, turns)
    for record, turn in zip(records, sorted(turns, key=lambda item: item.get("turn_number", 0))):
        turn_dir = Path(turn["_turn_dir"])
        stdout = _read_text(turn_dir / "stdout.txt") or str(turn.get("stdout", ""))
        stderr = _read_text(turn_dir / "stderr.txt") or str(turn.get("stderr", ""))
        output_contract = _turn_output_contract(turn)
        semantic_stdout = _select_turn_semantic_output(
            turn, stdout, output_contract=output_contract
        )
        semantic = _extract_turn_semantic_result(
            turn, stdout, output_contract=output_contract
        )
        is_reviewer = turn.get("step_role") == "reviewer"
        record["semantic_summary"] = _v3_reference_safe_semantic_result(
            semantic, reviewer=is_reviewer
        )
        record["semantic_extraction"] = semantic.extraction
        record["semantic_fallback"] = semantic.fallback
        signal_evidence = _turn_signal_evidence(
            turn,
            semantic_stdout,
            stderr,
            output_contract=output_contract,
        )
        record["diagnostics"] = {
            "signals": sorted({item.name for item in signal_evidence}),
            "signal_provenance": [asdict(item) for item in signal_evidence],
        }
    return records


def _history_range(records: list[Mapping[str, Any]], key: str) -> dict[str, int] | None:
    values = [
        value
        for record in records
        for value in (record.get(key),)
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    if not values:
        return None
    return {"start": min(values), "end": max(values)}


def _attempt_records(attempts: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(attempts, Mapping):
        return []
    records = attempts.get("attempts")
    if not isinstance(records, list):
        return []
    return [dict(item) for item in records if isinstance(item, Mapping)]


def _history_coverage(
    *,
    workflow_records: list[dict[str, Any]],
    manager_records: list[dict[str, Any]],
    attempt_records: list[dict[str, Any]],
    rejection_records: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "turns": {
            "count": len(workflow_records),
            "range": _history_range(workflow_records, "turn_number"),
        },
        "decisions": {
            "count": len(manager_records),
            "range": _history_range(manager_records, "decision_number"),
        },
        "implementation_attempts": {
            "count": len(attempt_records),
            "range": _history_range(attempt_records, "turn_number"),
        },
        "active_scope_rejections": {
            "count": len(rejection_records),
            "range": _history_range(rejection_records, "rejection_number"),
        },
    }


def _history_summary(
    *,
    workflow_records: list[dict[str, Any]],
    manager_records: list[dict[str, Any]],
    attempt_records: list[dict[str, Any]],
    rejection_records: list[dict[str, Any]],
    history_entry: Mapping[str, Any],
) -> dict[str, Any]:
    latest_decision = manager_records[-1] if manager_records else {}
    latest_rejection = rejection_records[-1] if rejection_records else {}
    return {
        "total_turns": len(workflow_records),
        "total_decisions": len(manager_records),
        "total_implementation_attempts": len(attempt_records),
        "total_active_scope_rejections": len(rejection_records),
        "latest_decision_number": latest_decision.get("decision_number"),
        "latest_decision_action": latest_decision.get("action"),
        "latest_rejection_number": latest_rejection.get("rejection_number"),
        "reference_available": history_entry.get("available") is True,
        "coverage": _history_coverage(
            workflow_records=workflow_records,
            manager_records=manager_records,
            attempt_records=attempt_records,
            rejection_records=rejection_records,
        ),
    }


def _v3_reference_safe_semantic_result(
    semantic: SemanticTurnOutcome, *, reviewer: bool
) -> str:
    """Keep unrecognized structured streams reference-only in full history."""
    if reviewer:
        return "Reviewer output is referenced by artifact; see the review stdout artifact."
    if semantic.fallback:
        return REFERENCE_ONLY_SEMANTIC_RESULT
    return semantic.result


def _full_v3_latest_turn(
    finished_turn: Mapping[str, Any],
    *,
    finished: Mapping[str, Any],
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    """Return detailed current-turn semantics without transcript bodies."""
    output_contract = _turn_output_contract(finished)
    semantic_stdout = _select_turn_semantic_output(
        finished, stdout, output_contract=output_contract
    )
    semantic = _extract_turn_semantic_result(
        finished, stdout, output_contract=output_contract
    )
    is_reviewer = finished.get("step_role") == "reviewer"
    signal_evidence = classify_turn_text_signals(
        semantic_stdout,
        stderr,
        finished.get("status"),
        finished.get("returncode"),
    )
    result = asdict(semantic)
    result["result"] = _v3_reference_safe_semantic_result(
        semantic, reviewer=is_reviewer
    )
    payload = {
        key: finished_turn.get(key)
        for key in (
            "turn_number", "step_name", "role", "team", "selector", "status",
            "returncode", "duration_seconds", "error", "snapshot_before",
            "snapshot_after", "snapshot_changed", "proposed_transition", "recovery",
            "conditions", "detected_stop", "raw_artifacts",
        )
    }
    payload["error"] = finished.get("error")
    payload.update({
        "semantic_result": result,
        "diagnostics": {
            "signals": sorted({item.name for item in signal_evidence}),
            "signal_provenance": [asdict(item) for item in signal_evidence],
        },
    })
    return payload


def _manager_history_artifact_payload(
    *,
    run_id: str,
    decision_number: int | None,
    finalized_turn_number: Any,
    workflow_records: list[dict[str, Any]],
    manager_records: list[dict[str, Any]],
    attempt_records: list[dict[str, Any]],
    rejection_records: list[dict[str, Any]],
    latest_turn: Mapping[str, Any],
    repartition_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the canonical, named-section manager history artifact payload."""
    run_extract = [*workflow_records, *manager_records]
    run_extract.sort(key=_combined_history_sort_key)
    return {
        "schema_version": MANAGER_HISTORY_ARTIFACT_SCHEMA_VERSION,
        "source_run_id": run_id,
        "decision_number": decision_number,
        "finalized_turn_number": finalized_turn_number,
        "coverage": _history_coverage(
            workflow_records=workflow_records,
            manager_records=manager_records,
            attempt_records=attempt_records,
            rejection_records=rejection_records,
        ),
        "sections": {
            "run_extract": run_extract,
            "manager_decisions": manager_records,
            "implementation_attempts": attempt_records,
            "active_scope_rejection_ledger": rejection_records,
            "latest_turn": dict(latest_turn),
            "checkpoint_repartitions": repartition_records,
        },
    }


def _combined_history_sort_key(
    item: Mapping[str, Any],
) -> tuple[int, int, bool, int]:
    """Sort combined history without inventing a turn for legacy decisions."""
    turn_number = item.get("turn_number")
    decision_number = item.get("decision_number")
    if isinstance(turn_number, int) and not isinstance(turn_number, bool):
        ordering_number = turn_number
        missing_turn = 0
    else:
        # A decision number is only a deterministic placement key here. Keep
        # the record's explicit ``turn_number`` (including None) untouched.
        ordering_number = (
            decision_number
            if isinstance(decision_number, int) and not isinstance(decision_number, bool)
            else item.get("number")
            if isinstance(item.get("number"), int)
            and not isinstance(item.get("number"), bool)
            else 0
        )
        missing_turn = 1
    return (
        missing_turn,
        ordering_number,
        item.get("kind") != "workflow_turn",
        decision_number
        if isinstance(decision_number, int) and not isinstance(decision_number, bool)
        else 0,
    )


def _merge_history_ranges(
    numbers: list[int],
) -> list[dict[str, int]]:
    """Return compact inclusive ranges for a set of positive integers."""
    values = sorted(set(number for number in numbers if number >= 0))
    if not values:
        return []
    ranges: list[dict[str, int]] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append({"start": start, "end": previous})
        start = previous = value
    ranges.append({"start": start, "end": previous})
    return ranges


def _history_descriptor(
    run_dir: Path,
    *,
    category: str,
    relative_path_pattern: str,
    number_key: str,
    records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    numbers = [
        value
        for record in records
        for value in (record.get(number_key),)
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    if not numbers:
        return None
    return {
        "source_run_id": run_dir.name,
        "category": category,
        "artifact_root": str(run_dir.resolve()),
        "relative_path_pattern": relative_path_pattern,
        "omitted_count": len(set(numbers)),
        "omitted_ranges": _merge_history_ranges(numbers),
    }


def _v3_history_disclosure(
    run_dir: Path,
    *,
    workflow_records: list[dict[str, Any]],
    manager_records: list[dict[str, Any]],
    run_extract: list[dict[str, Any]],
    retained_manager_records: list[dict[str, Any]],
) -> dict[str, Any]:
    retained_workflow_numbers = {
        record.get("turn_number")
        for record in run_extract
        if record.get("kind") == "workflow_turn"
        and isinstance(record.get("turn_number"), int)
    }
    retained_extract_decisions = {
        record.get("decision_number")
        for record in run_extract
        if record.get("kind") == "manager_decision"
        and isinstance(record.get("decision_number"), int)
    }
    retained_manager_numbers = {
        record.get("decision_number")
        for record in retained_manager_records
        if isinstance(record.get("decision_number"), int)
    }
    omitted = []
    descriptor = _history_descriptor(
        run_dir,
        category="run_extract_manager_decisions",
        relative_path_pattern="manager/decision-{decision_number:03d}",
        number_key="decision_number",
        records=[
            record for record in manager_records
            if record.get("decision_number") not in retained_extract_decisions
        ],
    )
    if descriptor is not None:
        omitted.append(descriptor)
    descriptor = _history_descriptor(
        run_dir,
        category="manager_decisions",
        relative_path_pattern="manager/decision-{decision_number:03d}",
        number_key="decision_number",
        records=[
            record for record in manager_records
            if record.get("decision_number") not in retained_manager_numbers
        ],
    )
    if descriptor is not None:
        omitted.append(descriptor)
    descriptor = _history_descriptor(
        run_dir,
        category="workflow_turns",
        relative_path_pattern="turns/turn-{turn_number:03d}",
        number_key="turn_number",
        records=[
            record for record in workflow_records
            if record.get("turn_number") not in retained_workflow_numbers
        ],
    )
    if descriptor is not None:
        omitted.append(descriptor)
    return {
        "reduction_order": [],
        "reduced_categories": [],
        "retained_counts": {
            "run_extract": len(run_extract),
            "run_extract_manager_decisions": len(retained_extract_decisions),
            "manager_decisions": len(retained_manager_records),
            "workflow_turns": len(retained_workflow_numbers),
        },
        "omitted": omitted,
    }


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
    analysis. Historical v1/v2 shapes retain their stored body fields; live
    selector-4 boundaries emit reference-only schema-v3 contexts.

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
    output_contract = _turn_output_contract(finished)
    semantic_stdout = _select_turn_semantic_output(
        finished, stdout, output_contract=output_contract
    )
    semantic = _extract_turn_semantic_result(
        finished, stdout, output_contract=output_contract
    )
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
    boundary_schema_version = boundary.get("context_schema_version")
    active_scope = boundary.get("active_implementation_scope")
    active_scope_mapping = active_scope if isinstance(active_scope, Mapping) else None
    complete_envelope_reference = (
        isinstance(boundary.get("envelope_artifact_path"), str)
        and bool(boundary.get("envelope_artifact_path"))
        and isinstance(boundary.get("envelope_artifact_sha256"), str)
        and bool(boundary.get("envelope_artifact_sha256"))
        and isinstance(boundary.get("envelope_canonical_sha256"), str)
        and bool(boundary.get("envelope_canonical_sha256"))
    )
    validated_envelope: dict[str, Any] | None = None
    if (
        not legacy_boundary
        and isinstance(boundary_schema_version, int)
        and boundary_schema_version >= 3
        and complete_envelope_reference
    ):
        validated_envelope = _resolve_validated_envelope(
            run_dir,
            boundary["envelope_artifact_path"],
            boundary["envelope_artifact_sha256"],
            boundary["envelope_canonical_sha256"],
            active_scope_mapping,
        )
    captured_plan_state = boundary.get("captured_plan_state")
    current_authority_boundary = (
        isinstance(
            boundary.get("original_checkpoint_authority_version"),
            int,
        )
        and not isinstance(
            boundary.get("original_checkpoint_authority_version"),
            bool,
        )
        and boundary.get("original_checkpoint_authority_version")
        == ORIGINAL_CHECKPOINT_AUTHORITY_VERSION
    )
    historical_captured_boundary = (
        isinstance(captured_plan_state, dict)
        and not current_authority_boundary
    )
    original_authority = _resolve_original_plan_authority(
        run_dir,
        run_json=run_json,
        finished_turn=finished,
        boundary=boundary,
        scope=(
            active_scope_mapping
            if (
                not legacy_boundary
                and isinstance(boundary_schema_version, int)
                and boundary_schema_version >= 3
            )
            else None
        ),
        validated_envelope=validated_envelope,
    )
    authority_for_boundary = (
        None if historical_captured_boundary else original_authority
    )
    plan_state, active_plan_path = _plan_state(
        run_dir,
        run_json,
        finished,
        original_authority=authority_for_boundary,
    )
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
        "conditions": finished.get("conditions"),
        "detected_stop": extract_trusted_stop_markers(
            semantic_stdout, stderr, output_contract=output_contract
        ),
        "diagnostics": {
            "signals": sorted({
                item.name for item in _turn_signal_evidence(
                    finished,
                    semantic_stdout,
                    stderr,
                    output_contract=output_contract,
                )
            }),
            "stdout_excerpt": _bounded(semantic_stdout),
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
    use_v2 = (
        isinstance(boundary_schema_version, int)
        and boundary_schema_version >= 3
    )
    if not use_v2:
        # Exact schema-v1 output preserved for selector-2 and selector-absent.
        return json.loads(json.dumps(context.to_dict(), sort_keys=True))
    # --- Schema v2 additions ---
    signal_evidence = classify_turn_text_signals(
        semantic_stdout,
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
    scope_pressure = has_scope_pressure(
        semantic_stdout, stderr, output_contract=output_contract
    )
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

    # --- Envelope: include the authority validated before plan parsing ---
    envelope_artifact_path = boundary.get("envelope_artifact_path")
    envelope_artifact_sha256 = boundary.get("envelope_artifact_sha256")
    envelope_canonical_sha256 = boundary.get("envelope_canonical_sha256")
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
    full_active_rejections: list[dict[str, Any]] = []
    if isinstance(rejection_history, list) and active_scope_id:
        for item in rejection_history:
            if isinstance(item, dict) and item.get("scope_id") == active_scope_id:
                full_active_rejections.append(dict(item))
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
    full_scoped_attempts: dict[str, Any] | None = None
    if isinstance(boundary_attempts, dict) and active_scope_id:
        raw_attempts = boundary_attempts.get(active_scope_id)
        if isinstance(raw_attempts, list):
            full_scoped_attempts = {
                "scope_id": active_scope_id,
                "attempts": [
                    dict(a) for a in raw_attempts if isinstance(a, Mapping)
                ],
            }
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
        if authority_for_boundary is not None:
            if authority_for_boundary.reason is None:
                if isinstance(authority_for_boundary.text, str):
                    original_plan_content = authority_for_boundary.text
                elif authority_for_boundary.path is not None:
                    original_plan_content = _read_text(authority_for_boundary.path) or None
        else:
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
        original_authority=authority_for_boundary,
    )
    finished_turn = context.finished_turn
    semantic_payload = finished_turn.get("semantic_result")
    semantic_payload = dict(semantic_payload) if isinstance(semantic_payload, Mapping) else {}
    if finished.get("step_role") == "reviewer":
        semantic_payload["result"] = (
            "Reviewer output is referenced by artifact; see the review stdout artifact."
        )
    elif semantic_payload.get("fallback") is True:
        # The shared extractor deliberately retains its legacy fallback value
        # for callers that need it, but schema-v3 must not copy an unrecognized
        # JSON/event transcript into either the manifest or disk projection.
        semantic_payload["result"] = REFERENCE_ONLY_SEMANTIC_RESULT
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
        },
        "raw_artifacts": finished_turn.get("raw_artifacts"),
    }
    v3_controller_state = {
        key: value
        for key, value in context.controller_state.items()
        if key not in {"latest_full_rejection", "repartition_evidence"}
    }
    # Evidence is owned by the primary repository, not the manager's worktree.
    # Keep both bases explicit because turn references are run-relative while
    # content-addressed evidence references are repository-relative.
    v3_controller_state["artifact_roots"] = {
        "repository": str(_v3_run_paths(run_dir).repo_root.resolve()),
        "run": str(run_dir.resolve()),
    }
    if isinstance(v3_controller_state.get("lite_evidence"), str):
        v3_controller_state["lite_evidence"] = _v3_bounded_text(
            v3_controller_state["lite_evidence"]
        )
    boundary_repartition_history = boundary.get("repartition_history")
    if isinstance(boundary_repartition_history, (list, tuple)):
        # Full repartition history is in the immutable manager-history
        # artifact.  Keep the old field as an explicit empty compatibility
        # container while the current repartition evidence below remains
        # decision-critical.
        v3_controller_state["checkpoint_repartitions"] = []
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
        # Reviewer prose and repair-plan prose are history evidence.  Keep
        # their durable pointers and structural identifiers inline; the
        # complete ledger is available from evidence.manager_history.
        summary_rejection.pop("review_summary", None)
        summary_rejection.pop("repair_plan_summary", None)
        v3_controller_state["latest_full_rejection"] = summary_rejection
    v3_manager_history = _manager_history_records(
        run_dir, before_decision_number=decision_number
    )
    v3_workflow_history = _full_v3_workflow_history_records(run_dir, turns)
    full_attempt_records = _attempt_records(full_scoped_attempts)
    repartition_records = [
        dict(record)
        for record in (boundary_repartition_history or [])
        if isinstance(record, Mapping)
    ]
    full_latest_turn = _full_v3_latest_turn(
        v3_finished_turn,
        finished=finished,
        stdout=stdout,
        stderr=stderr,
    )
    history_payload = _manager_history_artifact_payload(
        run_id=context.run_id,
        decision_number=decision_number,
        finalized_turn_number=finished_turn.get("turn_number"),
        workflow_records=v3_workflow_history,
        manager_records=v3_manager_history,
        attempt_records=full_attempt_records,
        rejection_records=full_active_rejections,
        latest_turn=full_latest_turn,
        repartition_records=repartition_records,
    )
    manager_history_entry = _capture_manager_history_evidence(
        run_dir,
        capture=capture_evidence,
        boundary=boundary,
        run_json=run_json,
        decision_number=decision_number,
        payload=history_payload,
    )
    history_summary = _history_summary(
        workflow_records=v3_workflow_history,
        manager_records=v3_manager_history,
        attempt_records=full_attempt_records,
        rejection_records=full_active_rejections,
        history_entry=manager_history_entry,
    )
    # Keep the prior disclosure field readable for saved v3 consumers, while
    # making the new projection explicit: all historical rows are in the
    # named manager-history artifact, not in the inline manifest.
    history_disclosure = {
        "reduction_order": [],
        "reduced_categories": [],
        "retained_counts": {
            "run_extract": 0,
            "run_extract_manager_decisions": 0,
            "manager_decisions": 0,
            "workflow_turns": 0,
            "checkpoint_repartitions": 0,
            "active_scope_rejection_ledger": 0,
            "implementation_attempts": 0,
        },
        "omitted": [],
        "storage": "evidence.manager_history",
    }
    v3_context = ManagerContextV3(
        schema_version=MANAGER_CONTEXT_SCHEMA_VERSION_V3,
        run_id=context.run_id,
        decision_number=context.decision_number,
        level=context.level,
        trigger=context.trigger,
        finished_turn=v3_finished_turn,
        run_extract=(),
        plan_state=context.plan_state,
        controller_state=v3_controller_state,
        evidence={**evidence, "manager_history": manager_history_entry},
        plan_content_disclosure=plan_content_disclosure,
        history_disclosure=history_disclosure,
        history_summary=history_summary,
        active_scope_rejection_ledger=(),
        implementation_attempts={},
        manager_decisions=(),
        change_surface_evidence=change_surface,
        manager_note_scope=manager_note_scope,
        retry_manager_note_scope=retry_manager_note_scope,
        scope_pressure_detected=scope_pressure,
    )
    v3_dict = v3_context.to_dict()
    if retry_manager_note_scope is None:
        v3_dict.pop("retry_manager_note_scope", None)
    return json.loads(json.dumps(v3_dict, sort_keys=True))
