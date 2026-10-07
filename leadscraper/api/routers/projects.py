"""Projects, their organisations, and erasure."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.engine import Engine

from ...services import exports, organizations, projects
from ...services.workspace import Workspace
from .. import schemas as s
from ..deps import current_workspace, get_engine

router = APIRouter()

EXPORT_RESPONSES = {200: {"description": "The export file: the Records column model in "
                                         "every format.", "content": {
    "text/csv": {"example": "Score,Name,Category,Lead Types,Locality,Primary Phone\n"
                            "78.5,Smile Dental,Dentists,Local businesses,Baner,+91 98200 12345\n"},
    "application/json": {"example": {
        "project": {"id": 3, "name": "Pune dentists"}, "run_id": 7, "scope": "run",
        "sorted_by": "score (highest first)", "count": 1,
        "columns": [{"key": "score", "header": "Score", "kind": "score", "group": "core"},
                    {"key": "name", "header": "Name", "kind": "text", "group": "core"}],
        "records": [{"id": 12, "score": 78.5, "values": {"score": 78.5, "name": "Smile Dental"},
                     "lead_types": ["local_business"],
                     "scores": {"total": 78.5, "fit": 0.79, "intent": None, "reach": 0.78,
                                "reasons": ["Google rating 4.7 from 210 reviews"]},
                     "signals": [], "people": [], "inferred_contacts": [], "change": "new",
                     "suppressed": []}],
        "summary": {"totals": {"records": 1, "new": 1, "updated": 0}}}},
    exports.MEDIA_TYPES["xlsx"]: {"example": "<xlsx workbook: Overview, Records>"}}}}


def file_response(filename: str, content: bytes, fmt: str) -> Response:
    return Response(content, media_type=exports.MEDIA_TYPES[fmt],
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/projects", response_model=s.Page[s.Project], tags=["projects"],
            summary="List projects (sort: name, created_at, updated_at, id; '-' = desc)")
def list_projects(q: str | None = None, sort: str | None = Query(None, examples=["-updated_at"]),
                  limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                  engine: Engine = Depends(get_engine), ws: Workspace = Depends(current_workspace)):
    return projects.list_projects(engine, ws, q=q, sort=sort, limit=limit,
                                  offset=offset).to_dict()


@router.post("/projects", response_model=s.Project, status_code=201, tags=["projects"],
             summary="Create a project (optional: a run creates its project on first use)")
def create_project(body: s.ProjectCreate, engine: Engine = Depends(get_engine),
                   ws: Workspace = Depends(current_workspace)):
    return projects.create_project(engine, ws, **body.model_dump())


@router.get("/projects/{project_id}", response_model=s.Project, tags=["projects"],
            summary="One project with its counts")
def get_project(project_id: int, engine: Engine = Depends(get_engine),
                ws: Workspace = Depends(current_workspace)):
    return projects.get_project(engine, ws, project_id)


@router.patch("/projects/{project_id}", response_model=s.Project, tags=["projects"],
              summary="Rename, describe, or set lead retention")
def update_project(project_id: int, body: s.ProjectUpdate, engine: Engine = Depends(get_engine),
                   ws: Workspace = Depends(current_workspace)):
    return projects.update_project(engine, ws, project_id,
                                   **body.model_dump(exclude_unset=True))


@router.delete("/projects/{project_id}", response_model=s.Project, tags=["projects"],
               summary="Delete a project, its organisations and its run history")
def delete_project(project_id: int, engine: Engine = Depends(get_engine),
                   ws: Workspace = Depends(current_workspace)):
    return projects.delete_project(engine, ws, project_id)


@router.get("/projects/{project_id}/export.{fmt}", tags=["exports"], response_class=Response,
            responses=EXPORT_RESPONSES,
            summary="The whole project as CSV, JSON or XLSX, regenerated from the database")
async def export_project(project_id: int, fmt: Literal["csv", "json", "xlsx"],
                         engine: Engine = Depends(get_engine),
                         ws: Workspace = Depends(current_workspace)):
    filename, content, _ = await run_in_threadpool(exports.export, engine, ws, fmt,
                                                   project_id=project_id, scope="project")
    return file_response(filename, content, fmt)


@router.get("/organizations", response_model=s.Page[s.OrganizationSummary],
            tags=["organizations"],
            summary="Organisations of a project or a run: filter, sort, page")
def list_organizations(
        project_id: int | None = None, run_id: int | None = None,
        q: str | None = Query(None, description="Name contains."),
        locality: str | None = None, category: str | None = None,
        lead_type: str | None = None,
        has: list[Literal["phone", "email", "website", "socials", "address"]] = Query(
            default_factory=list, description="Must have a contact of each kind."),
        min_score: float | None = None, max_score: float | None = None,
        domain: str | None = None,
        change: Literal["new", "updated", "unchanged"] | None = Query(
            None, description="With run_id: what that run did to it."),
        sort: str | None = Query(None, examples=["-score,name"],
                                 description="score, name, locality, category, created_at, "
                                             "updated_at, fit, intent, reach, id"),
        limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
        engine: Engine = Depends(get_engine), ws: Workspace = Depends(current_workspace)):
    return organizations.list_organizations(
        engine, ws, project_id=project_id, run_id=run_id, q=q, locality=locality,
        category=category, lead_type=lead_type, has=list(has), min_score=min_score,
        max_score=max_score, domain=domain, change=change, sort=sort, limit=limit,
        offset=offset).to_dict()


@router.get("/organizations/{org_id}", response_model=s.Organization, tags=["organizations"],
            summary="One organisation with contacts, people, signals and provenance")
def get_organization(org_id: int, engine: Engine = Depends(get_engine),
                     ws: Workspace = Depends(current_workspace)):
    return organizations.get_organization(engine, ws, org_id)


@router.post("/organizations/erase", response_model=s.EraseResult, tags=["organizations"],
             summary="Erase every organisation holding a domain or phone (data-subject request)")
def erase(body: s.EraseRequest, engine: Engine = Depends(get_engine),
          ws: Workspace = Depends(current_workspace)):
    return organizations.erase(engine, ws, domain=body.domain, phone=body.phone,
                               project_id=body.project_id, dry_run=body.dry_run,
                               suppress=body.suppress, region=body.region)
