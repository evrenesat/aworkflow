"""Fail-closed reconciliation of durable AFlow state and observed unit state."""

from __future__ import annotations

from threading import RLock

from .models import ReconciliationResult, RunStatus
from .persistence import append_run_event, read_events
from .repository import RunRepository
from .units import UnitManager, UnitState


_TERMINAL_STATUSES = frozenset({"completed", "done", "failed", "owner_stopped", "interrupted"})


class ReconciliationService:
    """Observe only; this service deliberately has no launch or resume dependency."""

    def __init__(self, repository: RunRepository, units: UnitManager) -> None:
        self._repository = repository
        self._units = units
        self._lock = RLock()

    def reconcile_startup(self) -> tuple[ReconciliationResult, ...]:
        return self.reconcile_all()

    def reconcile_periodic(self) -> tuple[ReconciliationResult, ...]:
        return self.reconcile_all()

    def reconcile_all(self) -> tuple[ReconciliationResult, ...]:
        with self._lock:
            results: list[ReconciliationResult] = []
            cursor: str | None = None
            while True:
                page = self._repository.list_runs(limit=1_000, cursor=cursor)
                results.extend(self.reconcile_run(status.run_id) for status in page.runs)
                if page.next_cursor is None:
                    return tuple(results)
                cursor = page.next_cursor

    def reconcile_run(self, run_id: str) -> ReconciliationResult:
        with self._lock:
            status = self._repository.get_run_status(run_id)
            if status.ownership == "legacy":
                return ReconciliationResult(
                    run_id=run_id,
                    status="interrupted",
                    reason="legacy run has no control-plane ownership evidence",
                    ownership="legacy",
                )
            result = self._classify_owned(status)
            self._append_deduplicated_observation(status, result)
            return result

    def _classify_owned(self, status: RunStatus) -> ReconciliationResult:
        expected_name = f"aflow-run-{status.run_id}.service"
        manifest = self._repository.get_launch_manifest(status.run_id)
        if manifest is None:
            # ``RunRepository`` currently classifies this as legacy, but keep
            # the invariant local if a future repository adds an owned variant.
            return ReconciliationResult(status.run_id, "needs_attention", "missing launch manifest")
        if manifest.intended_unit and manifest.intended_unit != expected_name:
            return ReconciliationResult(
                status.run_id,
                "needs_attention",
                "manifest unit identity does not match canonical run identity",
                unit_name=manifest.intended_unit,
            )

        observed = self._units.get(expected_name)
        if observed is not None and observed.name != expected_name:
            return ReconciliationResult(
                status.run_id,
                "needs_attention",
                "observed unit identity does not match canonical run identity",
                unit_name=expected_name,
                observed_unit_state=observed.active_state,
            )
        if observed is not None and observed.is_active:
            return ReconciliationResult(
                status.run_id,
                "running",
                "exact workflow unit is active",
                unit_name=expected_name,
                observed_unit_state=observed.active_state,
            )
        return self._classify_inactive(status, expected_name, observed)

    def _classify_inactive(
        self,
        status: RunStatus,
        unit_name: str,
        observed: UnitState | None,
    ) -> ReconciliationResult:
        observed_state = observed.active_state if observed is not None else "missing"
        if status.status == "manifest_only" and status.launch_phase in {None, "manifest_only"}:
            return ReconciliationResult(
                status.run_id,
                "manifest_only",
                "manifest exists and no child-start evidence exists",
                unit_name=unit_name,
                observed_unit_state=observed_state,
            )
        if status.launch_phase == "launch_requested":
            return ReconciliationResult(
                status.run_id,
                "needs_attention",
                "launch was requested but unit start is not proven",
                unit_name=unit_name,
                observed_unit_state=observed_state,
            )
        if status.status == "running" and observed is not None and observed.active_state == "failed":
            return ReconciliationResult(
                status.run_id,
                "needs_attention",
                "exact workflow unit failed; explicit resume is required",
                unit_name=unit_name,
                observed_unit_state=observed_state,
            )
        if status.status == "running":
            return ReconciliationResult(
                status.run_id,
                "needs_attention",
                "running state has no exact active workflow unit; explicit resume is required",
                unit_name=unit_name,
                observed_unit_state=observed_state,
            )
        if status.status in _TERMINAL_STATUSES:
            return ReconciliationResult(
                status.run_id,
                status.status,
                "terminal disk state retained after unit observation",
                unit_name=unit_name,
                observed_unit_state=observed_state,
            )
        return ReconciliationResult(
            status.run_id,
            "needs_attention",
            "launch or unit evidence is inactive or ambiguous",
            unit_name=unit_name,
            observed_unit_state=observed_state,
        )

    def _append_deduplicated_observation(
        self,
        status: RunStatus,
        result: ReconciliationResult,
    ) -> None:
        run_dir = self._repository.run_directory(status.run_id)
        if not run_dir.is_dir():
            return
        evidence = {
            "status": result.status,
            "reason": result.reason,
            "unit_name": result.unit_name,
            "observed_unit_state": result.observed_unit_state,
        }
        for event in reversed(read_events(run_dir, limit=1_000)):
            if event.event_type == "reconciled":
                if dict(event.data) == evidence:
                    return
                break
        append_run_event(run_dir, "reconciled", evidence)
