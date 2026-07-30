"""Public analysis helper for aflow run logs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from aflow.analyzer import analyze_corpus, analyze_single_run, collect_run_dirs, resolve_run_id
from aflow.analyzer import load_turns
from aflow.manager_context import build_manager_context

from .models import AnalyzeRequest


def _validated_repartition_history(
    run_dir: Path,
    boundary: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Validate compact captured repartition records against exact artifacts."""

    raw_history = boundary.get("repartition_history")
    if raw_history is None:
        return None
    if not isinstance(raw_history, list):
        raise ValueError("manager boundary repartition history is not a list")
    validated: list[dict[str, Any]] = []
    for raw in raw_history:
        if not isinstance(raw, dict):
            raise ValueError("manager boundary repartition record is not an object")
        record = dict(raw)
        artifact_hashes = {
            "envelope_artifact_path": record.get("envelope_artifact_sha256"),
            "proposal_artifact_path": record.get("proposal_sha256"),
            "candidate_artifact_path": record.get("candidate_plan_sha256"),
        }
        for path_key in (
            "envelope_artifact_path",
            "proposal_artifact_path",
            "candidate_artifact_path",
            "mechanical_validation_artifact_path",
            "semantic_verdict_artifact_path",
        ):
            relative = record.get(path_key)
            if not isinstance(relative, str) or not relative:
                raise ValueError(
                    f"manager boundary repartition record lacks {path_key}"
                )
            artifact = (run_dir / relative).resolve()
            try:
                artifact.relative_to(run_dir.resolve())
                artifact_bytes = artifact.read_bytes()
            except (ValueError, OSError) as exc:
                raise ValueError(
                    f"manager boundary repartition artifact is unavailable: {relative}"
                ) from exc
            expected_hash = artifact_hashes.get(path_key)
            if (
                isinstance(expected_hash, str)
                and hashlib.sha256(artifact_bytes).hexdigest() != expected_hash
            ):
                raise ValueError(
                    f"manager boundary repartition artifact hash drift: {relative}"
                )
        validated.append(record)
    return validated


def analyze_runs(request: AnalyzeRequest) -> dict[str, Any]:
    """Analyze one run or a corpus of runs using the same payload shape as the CLI."""

    repo_root = request.repo_root.resolve()
    runs_root = repo_root / ".aflow" / "runs"

    if request.all:
        if request.manager_context is not None or request.turn is not None:
            raise ValueError("--manager-context and --turn require a single run, not --all")
        if not runs_root.is_dir():
            raise ValueError(f"runs root does not exist: {runs_root}")
        run_dirs = collect_run_dirs(runs_root)
        if request.limit is not None and request.limit > 0:
            run_dirs = run_dirs[-request.limit :]
        return analyze_corpus(
            run_dirs=run_dirs,
            runs_root=runs_root,
            selection="corpus",
            include_noise=request.include_noise,
        )

    if request.run_id is not None:
        run_dir = runs_root / request.run_id
        selection = "explicit_run_id"
    else:
        resolved_run_dir, source = resolve_run_id(None, repo_root)
        if resolved_run_dir is None:
            raise ValueError(
                "no run ID specified and no last run ID found. "
                "Provide a run ID as an argument, use the current shell's last run, "
                "set AFLOW_LAST_RUN_ID environment variable, or ensure .aflow/last_run_id file exists."
            )
        selection = source or "unknown"
        run_dir = runs_root / resolved_run_dir.name

    if not (run_dir / "run.json").is_file():
        raise ValueError(f"run directory does not contain run.json: {run_dir}")

    if request.turn is not None and request.manager_context is None:
        raise ValueError("--turn requires --manager-context")
    if request.manager_context is not None:
        # Rebuild the selected decision from its immutable boundary inputs.
        # Stored context is evidence, not the analysis result: a mismatch is a
        # durable-artifact drift error rather than an opportunity to silently
        # return stale state.
        if request.turn is not None:
            manager_root = run_dir / "manager"
            if manager_root.is_dir():
                for decision_dir in sorted(path for path in manager_root.iterdir() if path.is_dir()):
                    result_path = decision_dir / "result.json"
                    context_path = decision_dir / "context.json"
                    boundary_path = decision_dir / "boundary.json"
                    try:
                        result = json.loads(result_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if result.get("finalized_turn_number") == request.turn and result.get("level") == request.manager_context:
                        try:
                            stored = json.loads(context_path.read_text(encoding="utf-8"))
                            durable = json.loads(boundary_path.read_text(encoding="utf-8"))
                            if not isinstance(durable, dict):
                                raise ValueError("manager boundary artifact is not an object")
                            boundary = dict(durable["boundary"])
                            if "context_schema_version" not in boundary:
                                stored_progress = (
                                    stored.get("controller_state", {}).get("progress", {})
                                    if isinstance(stored.get("controller_state"), dict)
                                    else {}
                                )
                                if (
                                    isinstance(stored_progress, dict)
                                    and "same_step_stall_turns" in stored_progress
                                ):
                                    boundary["context_schema_version"] = 2
                                if (
                                    "captured_plan_state" not in boundary
                                    and isinstance(stored.get("plan_state"), dict)
                                ):
                                    boundary["captured_plan_state"] = stored["plan_state"]
                            turns = [
                                turn for turn in load_turns(run_dir)
                                if isinstance(turn.get("turn_number"), int)
                                and turn["turn_number"] <= result.get("finalized_turn_number")
                            ]
                            rebuilt = build_manager_context(
                                run_dir,
                                level=request.manager_context,
                                trigger=str(durable["trigger"]),
                                decision_number=int(durable["decision_number"]),
                                run_metadata=dict(durable["run_metadata"]),
                                boundary=boundary,
                                turns=turns,
                                active_plan_content=(str(durable["active_plan_content"])
                                    if durable.get("active_plan_content") is not None else None),
                            )
                            repartition_history = _validated_repartition_history(
                                run_dir, boundary,
                            )
                            if repartition_history is not None:
                                controller_state = rebuilt.get("controller_state")
                                if isinstance(controller_state, dict):
                                    controller_state["checkpoint_repartitions"] = (
                                        repartition_history
                                    )
                            if rebuilt != stored:
                                differing_keys = sorted({*rebuilt, *stored} - {
                                    key for key in {*rebuilt, *stored}
                                    if rebuilt.get(key) == stored.get(key)
                                })
                                raise ValueError(
                                    f"manager context drift for decision-{int(durable['decision_number']):03d} "
                                    f"({', '.join(differing_keys)})"
                                )
                            return rebuilt
                        except (OSError, json.JSONDecodeError):
                            break
        finalized = [
            turn for turn in load_turns(run_dir)
            if turn.get("status") != "starting"
        ]
        if request.turn is not None:
            finalized = [turn for turn in finalized if turn.get("turn_number") == request.turn]
            if not finalized:
                raise ValueError(f"run has no finalized turn numbered {request.turn}")
            all_turns = [
                turn for turn in load_turns(run_dir)
                if turn.get("status") != "starting"
                and isinstance(turn.get("turn_number"), int)
                and turn["turn_number"] <= request.turn
            ]
        else:
            all_turns = finalized
        if not all_turns:
            raise ValueError("run has no finalized workflow turns for manager context")
        return build_manager_context(
            run_dir,
            level=request.manager_context,
            trigger="post_turn",
            turns=all_turns,
        )

    return analyze_single_run(
        run_dir=run_dir,
        runs_root=runs_root,
        selection=selection,
        include_noise=request.include_noise,
    )
