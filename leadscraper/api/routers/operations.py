"""Suppression lists, schedules, notification targets."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.engine import Engine

from ...services import notifications, schedules, suppression
from ...services.workspace import Workspace
from .. import schemas as s
from ..deps import current_workspace, get_engine

router = APIRouter()


# --- suppression lists --------------------------------------------------------------------------

@router.get("/suppression-lists", response_model=s.Page[s.SuppressionListOut],
            tags=["suppression"], summary="Suppression lists (sort: name, id, created_at)")
def list_lists(q: str | None = None, sort: str | None = None,
               limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
               engine: Engine = Depends(get_engine), ws: Workspace = Depends(current_workspace)):
    return suppression.list_lists(engine, ws, q=q, sort=sort, limit=limit,
                                  offset=offset).to_dict()


@router.post("/suppression-lists", response_model=s.SuppressionListOut, status_code=201,
             tags=["suppression"],
             summary="Create a list; attach it to configs by name (filters.suppression_lists)")
def create_list(body: s.SuppressionListCreate, engine: Engine = Depends(get_engine),
                ws: Workspace = Depends(current_workspace)):
    return suppression.create_list(engine, ws, name=body.name, mode=body.mode,
                                   entries=[e.model_dump(exclude_none=True)
                                            for e in body.entries])


@router.get("/suppression-lists/{list_id}", response_model=s.SuppressionListOut,
            tags=["suppression"], summary="One list")
def get_list(list_id: int, engine: Engine = Depends(get_engine),
             ws: Workspace = Depends(current_workspace)):
    return suppression.get_list(engine, ws, list_id)


@router.patch("/suppression-lists/{list_id}", response_model=s.SuppressionListOut,
              tags=["suppression"], summary="Rename, or switch between exclude and flag")
def update_list(list_id: int, body: s.SuppressionListUpdate, engine: Engine = Depends(get_engine),
                ws: Workspace = Depends(current_workspace)):
    return suppression.update_list(engine, ws, list_id, name=body.name, mode=body.mode)


@router.delete("/suppression-lists/{list_id}", response_model=s.SuppressionListOut,
               tags=["suppression"], summary="Delete a list and its entries")
def delete_list(list_id: int, engine: Engine = Depends(get_engine),
                ws: Workspace = Depends(current_workspace)):
    return suppression.delete_list(engine, ws, list_id)


@router.get("/suppression-lists/{list_id}/entries", response_model=s.Page[s.SuppressionEntryOut],
            tags=["suppression"], summary="A list's entries")
def list_entries(list_id: int, kind: Literal["name", "domain", "phone"] | None = None,
                 q: str | None = None, limit: int = Query(50, ge=1, le=500),
                 offset: int = Query(0, ge=0), engine: Engine = Depends(get_engine),
                 ws: Workspace = Depends(current_workspace)):
    return suppression.list_entries(engine, ws, list_id, kind=kind, q=q, limit=limit,
                                    offset=offset).to_dict()


@router.post("/suppression-lists/{list_id}/entries", response_model=s.EntriesResult,
             tags=["suppression"], summary="Add entries (duplicates are skipped)")
def add_entries(list_id: int, body: s.EntriesAdd, engine: Engine = Depends(get_engine),
                ws: Workspace = Depends(current_workspace)):
    return suppression.add_entries(engine, ws, list_id,
                                   [e.model_dump(exclude_none=True) for e in body.entries])


@router.delete("/suppression-lists/{list_id}/entries/{entry_id}", status_code=204,
               tags=["suppression"], summary="Remove one entry",
               responses={204: {"description": "Removed."}})
def delete_entry(list_id: int, entry_id: int, engine: Engine = Depends(get_engine),
                 ws: Workspace = Depends(current_workspace)):
    suppression.delete_entry(engine, ws, list_id, entry_id)


@router.post("/suppression-lists/{list_id}/import", response_model=s.EntriesResult,
             tags=["suppression"],
             summary="Import names, domains and phones from a CSV or XLSX upload")
async def import_entries(list_id: int, file: UploadFile = File(...),
                         engine: Engine = Depends(get_engine),
                         ws: Workspace = Depends(current_workspace)):
    return suppression.import_file(engine, ws, list_id, await file.read(),
                                   file.filename or "upload.csv")


# --- schedules -------------------------------------------------------------------------------------

@router.get("/schedules", response_model=s.Page[s.ScheduleOut], tags=["schedules"],
            summary="Recurring runs of saved configs (sort: id, name, next_run_at)")
def list_schedules(saved_config_id: int | None = None, enabled: bool | None = None,
                   sort: str | None = None, limit: int = Query(50, ge=1, le=500),
                   offset: int = Query(0, ge=0), engine: Engine = Depends(get_engine),
                   ws: Workspace = Depends(current_workspace)):
    return schedules.list_schedules(engine, ws, saved_config_id=saved_config_id,
                                    enabled=enabled, sort=sort, limit=limit,
                                    offset=offset).to_dict()


@router.post("/schedules", response_model=s.ScheduleOut, status_code=201, tags=["schedules"],
             summary="Schedule a saved config (cron, in a timezone); the worker queues runs")
def create_schedule(body: s.ScheduleCreate, engine: Engine = Depends(get_engine),
                    ws: Workspace = Depends(current_workspace)):
    return schedules.create_schedule(engine, ws, **body.model_dump())


@router.get("/schedules/{schedule_id}", response_model=s.ScheduleOut, tags=["schedules"],
            summary="One schedule, with its next firing")
def get_schedule(schedule_id: int, engine: Engine = Depends(get_engine),
                 ws: Workspace = Depends(current_workspace)):
    return schedules.get_schedule(engine, ws, schedule_id)


@router.patch("/schedules/{schedule_id}", response_model=s.ScheduleOut, tags=["schedules"],
              summary="Change, enable or disable a schedule")
def update_schedule(schedule_id: int, body: s.ScheduleUpdate,
                    engine: Engine = Depends(get_engine),
                    ws: Workspace = Depends(current_workspace)):
    return schedules.update_schedule(engine, ws, schedule_id,
                                     **body.model_dump(exclude_none=True))


@router.delete("/schedules/{schedule_id}", response_model=s.ScheduleOut, tags=["schedules"],
               summary="Delete a schedule (its runs stay in history)")
def delete_schedule(schedule_id: int, engine: Engine = Depends(get_engine),
                    ws: Workspace = Depends(current_workspace)):
    return schedules.delete_schedule(engine, ws, schedule_id)


@router.post("/schedules/{schedule_id}/run", response_model=s.QueuedRun, status_code=201,
             tags=["schedules"], summary="Queue the schedule's run now")
def run_schedule(schedule_id: int, engine: Engine = Depends(get_engine),
                 ws: Workspace = Depends(current_workspace)):
    return {"run_id": schedules.run_now(engine, ws, schedule_id)}


# --- notifications -----------------------------------------------------------------------------------

@router.get("/notifications", response_model=s.Page[s.NotificationOut], tags=["notifications"],
            summary="Where the workspace is told about finished runs")
def list_targets(sort: str | None = None, limit: int = Query(50, ge=1, le=500),
                 offset: int = Query(0, ge=0), engine: Engine = Depends(get_engine),
                 ws: Workspace = Depends(current_workspace)):
    return notifications.list_targets(engine, ws, sort=sort, limit=limit,
                                      offset=offset).to_dict()


@router.post("/notifications", response_model=s.NotificationOut, status_code=201,
             tags=["notifications"],
             summary="Add a desktop, webhook (Slack/Teams) or SMTP email target")
def create_target(body: s.NotificationCreate, engine: Engine = Depends(get_engine),
                  ws: Workspace = Depends(current_workspace)):
    return notifications.create_target(engine, ws, name=body.name, kind=body.kind,
                                       config=body.config, events=body.events,
                                       enabled=body.enabled)


@router.get("/notifications/{target_id}", response_model=s.NotificationOut,
            tags=["notifications"], summary="One target (webhook URLs masked)")
def get_target(target_id: int, engine: Engine = Depends(get_engine),
               ws: Workspace = Depends(current_workspace)):
    return notifications.get_target(engine, ws, target_id)


@router.patch("/notifications/{target_id}", response_model=s.NotificationOut,
              tags=["notifications"], summary="Change a target")
def update_target(target_id: int, body: s.NotificationUpdate, engine: Engine = Depends(get_engine),
                  ws: Workspace = Depends(current_workspace)):
    return notifications.update_target(engine, ws, target_id,
                                       **body.model_dump(exclude_none=True))


@router.delete("/notifications/{target_id}", response_model=s.NotificationOut,
               tags=["notifications"], summary="Remove a target")
def delete_target(target_id: int, engine: Engine = Depends(get_engine),
                  ws: Workspace = Depends(current_workspace)):
    return notifications.delete_target(engine, ws, target_id)


@router.post("/notifications/{target_id}/test", response_model=s.TestResult,
             tags=["notifications"], summary="Send a test message")
def test_target(target_id: int, engine: Engine = Depends(get_engine),
                ws: Workspace = Depends(current_workspace)):
    return notifications.send_test(engine, ws, target_id)
