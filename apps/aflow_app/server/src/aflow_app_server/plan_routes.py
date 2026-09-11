"""Authenticated provider-independent plan routes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict

from .control_plane_service import ControlPlaneService
from .plan_service import PlanService

router = APIRouter(prefix="/api/projects/{project_id}/plans", tags=["plans"])
PlanStatusValue = Literal["todo", "in_progress", "done"]

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class PlanCreatePayload(StrictModel):
    name: str
    content: str | None = None


class PlanFromRunPayload(StrictModel):
    run_id: str
    name: str | None = None


class PlanUpdatePayload(StrictModel):
    content: str
    expected_revision: str

class PlanPromotePayload(StrictModel):
    expected_revision: str
    target_name: str | None = None

def _get_plan_service() -> PlanService:
    raise RuntimeError("plan service dependency was not configured")


def _get_control_plane_service() -> ControlPlaneService:
    raise RuntimeError("control-plane service dependency was not configured")


@router.get("")
def list_plans(
    project_id: str,
    plan_status: PlanStatusValue | None = Query(default=None, alias="status"),
    service: PlanService = Depends(_get_plan_service),
) -> list[dict[str, object]]:
    return [document.to_dict() for document in service.list(project_id, plan_status)]

@router.post("", status_code=status.HTTP_201_CREATED)
def create_plan(
    project_id: str,
    payload: PlanCreatePayload,
    service: PlanService = Depends(_get_plan_service),
) -> dict[str, object]:
    return service.create(project_id, payload.name, payload.content).to_dict()


@router.post("/from-run", status_code=status.HTTP_201_CREATED)
def create_plan_from_run(
    project_id: str,
    payload: PlanFromRunPayload,
    service: PlanService = Depends(_get_plan_service),
    control_plane: ControlPlaneService = Depends(_get_control_plane_service),
) -> dict[str, object]:
    return service.create_plan_from_run(
        project_id,
        payload.run_id,
        payload.name,
        run_status_reader=control_plane.run_status,
        run_context_reader=control_plane.context,
    ).to_dict()


@router.get("/{plan_status}/{name}")
def read_plan(
    project_id: str,
    plan_status: PlanStatusValue,
    name: str,
    service: PlanService = Depends(_get_plan_service),
) -> dict[str, object]:
    return service.read(project_id, plan_status, name).to_dict()


@router.get("/{plan_status}/{name}/backups")
def list_plan_backups(
    project_id: str,
    plan_status: PlanStatusValue,
    name: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    service: PlanService = Depends(_get_plan_service),
) -> dict[str, object]:
    """Read bounded provenance summaries for the exact authorized plan."""
    return service.list_backups(
        project_id,
        plan_status,
        name,
        offset=offset,
        limit=limit,
    )

@router.put("/{plan_status}/{name}")
def update_plan(
    project_id: str,
    plan_status: PlanStatusValue,
    name: str,
    payload: PlanUpdatePayload,
    service: PlanService = Depends(_get_plan_service),
) -> dict[str, object]:
    return service.update(project_id, plan_status, name, payload.content, payload.expected_revision).to_dict()

@router.post("/{plan_status}/{name}/promote")
def promote_plan(
    project_id: str,
    plan_status: PlanStatusValue,
    name: str,
    payload: PlanPromotePayload,
    service: PlanService = Depends(_get_plan_service),
) -> dict[str, object]:
    return service.promote(project_id, plan_status, name, payload.expected_revision, payload.target_name).to_dict()
