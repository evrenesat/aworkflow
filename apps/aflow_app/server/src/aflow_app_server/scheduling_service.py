"""Shared REST/MCP projection of project scheduling and plan admission."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from aflow.plan_backups import plan_identity_for_path
from aflow.plan_dependencies import PlanDependencies, PlanDependencyError, parse_sequence_name
from aflow.project_admission import ProjectAdmission
from aflow.project_settings import ProjectSettingsService

from .plan_service import PlanProjectNotFound, PlanService
from .project_registry import ProjectRegistry, ProjectRegistryError


class SchedulingService:
    """Delegate durable settings and admission reads to their canonical owners."""

    def __init__(
        self,
        registry: ProjectRegistry,
        plans: PlanService,
        *,
        reason: Callable[[Path, str], str | None] | None = None,
        wake: Callable[[str], None] | None = None,
    ) -> None:
        self._registry = registry
        self._plans = plans
        self._reason = reason
        self._wake = wake

    def _root(self, project_id: str) -> Path:
        try:
            _, root = self._registry.resolve(project_id)
        except ProjectRegistryError as exc:
            raise PlanProjectNotFound("project not found") from exc
        return root

    def read(self, project_id: str) -> dict[str, object]:
        return ProjectSettingsService(self._root(project_id)).read().to_dict()

    def update(
        self, project_id: str, *, expected_revision: str,
        changes: Mapping[str, object],
    ) -> dict[str, object]:
        saved = ProjectSettingsService(self._root(project_id)).update(
            expected_revision=expected_revision, **dict(changes),
        ).to_dict()
        if self._wake is not None:
            self._wake(project_id)
        return saved

    def queue(self, project_id: str) -> dict[str, object]:
        root = self._root(project_id)
        settings = ProjectSettingsService(root).read()
        admission = ProjectAdmission(root)
        capacity = admission.snapshot().to_dict()
        claims: dict[str, tuple[str, str, bool]] = {}
        for plan_key, run_id, state, retained in admission.plan_claims():
            previous = claims.get(plan_key)
            if previous is None or (previous[2] and not retained):
                claims[plan_key] = (run_id, state, retained)
        documents = self._plans.list(project_id)
        dependencies = PlanDependencies(admission.primary_root)
        roots = admission.project_roots()
        plans: list[dict[str, object]] = []
        for document in documents:
            claim = claims.get(document.path)
            reason = self._reason(root, document.name) if self._reason is not None else None
            dependency: str | None = None
            dependency_evidence_available = True
            if document.status == "in_progress":
                try:
                    dependency = dependencies.blocking_predecessor(
                        document.path, evidence_roots=roots,
                    )
                except PlanDependencyError:
                    reason = "dependency_evidence_unavailable"
                    dependency_evidence_available = False
                if dependency_evidence_available:
                    try:
                        sequence = parse_sequence_name(document.name)
                    except PlanDependencyError:
                        sequence = None
                        reason = "dependency"
                    if sequence is not None:
                        if dependencies.observed_sequence_conflict(
                            document.name, (item.name for item in documents),
                        ):
                            dependency = "sequence_conflict"
                        if dependency is None:
                            for predecessor in documents:
                                try:
                                    earlier = parse_sequence_name(predecessor.name)
                                except PlanDependencyError:
                                    continue
                                if earlier is None or earlier[0] != sequence[0] or earlier[1] >= sequence[1]:
                                    continue
                                try:
                                    delivered = predecessor.status == "done" and dependencies.observed_done_delivered(
                                        predecessor.name, checkout_root=root, evidence_roots=roots,
                                    )
                                except PlanDependencyError:
                                    reason = "dependency_evidence_unavailable"
                                    break
                                if not delivered:
                                    dependency = predecessor.name
                                    break
            if document.status in {"failed", "needs_plan_change", "done", "todo"}:
                outcome = "held"
                reason = (
                    str((document.lifecycle or {}).get("reason_code") or document.status)
                    if document.status in {"failed", "needs_plan_change"}
                    else document.status
                )
            elif claim is not None:
                outcome = "held" if claim[2] or claim[1] == "uncertain" else "running"
                reason = "claim_retained" if claim[2] else ("uncertain" if claim[1] == "uncertain" else None)
            elif not settings.auto_consume_plans:
                outcome, reason = "held", "automatic_disabled"
            elif dependency is not None or reason not in {None, "unstable"}:
                outcome = "blocked"
                reason = "dependency" if dependency is not None else reason
            elif capacity["available_slots"] == 0:
                outcome, reason = "blocked", "capacity"
            else:
                outcome = "queued"
            plans.append({
                "name": document.name,
                "path": document.path,
                "status": document.status,
                "identity": plan_identity_for_path(root, root / document.path),
                "outcome": outcome,
                "reason": reason,
                "dependency": dependency,
                "run_id": claim[0] if claim is not None else (document.lifecycle or {}).get("source_run_id"),
                "revision": document.revision,
            })
        return {
            "project_id": project_id,
            "settings": settings.to_dict(),
            "capacity": capacity,
            "plans": plans,
        }
