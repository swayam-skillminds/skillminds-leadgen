"""Runs: create, list, inspect, control, re-run, compare, events (SSE), exports."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy.engine import Engine

from ...db.runs import TERMINAL
from ...services import exports, runs
from ...services.workspace import Workspace
from .. import schemas as s
from ..deps import current_workspace, get_engine
from .projects import EXPORT_RESPONSES, file_response

router = APIRouter()

#: How long a stream keeps polling after the run reached a final state, for
#: the last events the run's writer thread is still persisting.
SETTLE_SECONDS = 5.0
KEEPALIVE_SECONDS = 15.0
#: A run_state event with one of these ends the stream: the run stopped
#: (a resumed run streams again from a new connection with Last-Event-ID).
STOPPED = frozenset(TERMINAL | {"paused", "queued"})


def _stops(event: dict) -> bool:
    return event["type"] == "run_state" and event.get("status") in STOPPED


@router.post("/runs", response_model=s.Run, status_code=201, tags=["runs"],
             summary="Queue a run of a config or a saved config; a worker executes it")
def create_run(body: s.RunCreate, engine: Engine = Depends(get_engine),
               ws: Workspace = Depends(current_workspace)):
    return runs.create_run(engine, ws, document=body.config,
                           saved_config_id=body.saved_config_id, version=body.version,
                           project_id=body.project_id,
                           overrides=body.options.model_dump(exclude_none=True))


@router.get("/runs", response_model=s.Page[s.RunSummary], tags=["runs"],
            summary="Run history (filter by project, status, kind, saved config; sort: id, "
                    "created_at, finished_at, status)")
def list_runs(project_id: int | None = None,
              status: list[s.RunStatus] = Query(default_factory=list),
              kind: Literal["run", "preview", "import"] | None = None,
              saved_config_id: int | None = None,
              sort: str | None = Query(None, examples=["-id"]),
              limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
              engine: Engine = Depends(get_engine), ws: Workspace = Depends(current_workspace)):
    return runs.list_runs(engine, ws, project_id=project_id, status=list(status), kind=kind,
                          saved_config_id=saved_config_id, sort=sort, limit=limit,
                          offset=offset).to_dict()


@router.get("/runs/{run_id}", response_model=s.Run, tags=["runs"],
            summary="Everything history keeps about a run: snapshot, timings, estimate vs "
                    "actual, counts per lead type and source, failures, AI usage, files")
def get_run(run_id: int, engine: Engine = Depends(get_engine),
            ws: Workspace = Depends(current_workspace)):
    return runs.get_run(engine, ws, run_id)


CONFLICT = {409: {"model": s.Message, "description": "Not possible in the run's state."}}


@router.post("/runs/{run_id}/pause", response_model=s.Run, tags=["runs"], responses=CONFLICT,
             summary="Pause: in-flight work finishes, then everything is flushed")
def pause_run(run_id: int, engine: Engine = Depends(get_engine),
              ws: Workspace = Depends(current_workspace)):
    return runs.control_run(engine, ws, run_id, "pause")


@router.post("/runs/{run_id}/resume", response_model=s.Run, tags=["runs"], responses=CONFLICT,
             summary="Resume a paused or failed run from its checkpoint")
def resume_run(run_id: int, engine: Engine = Depends(get_engine),
               ws: Workspace = Depends(current_workspace)):
    return runs.control_run(engine, ws, run_id, "resume")


@router.post("/runs/{run_id}/cancel", response_model=s.Run, tags=["runs"], responses=CONFLICT,
             summary="Cancel: in-flight work finishes, then everything is flushed")
def cancel_run(run_id: int, engine: Engine = Depends(get_engine),
               ws: Workspace = Depends(current_workspace)):
    return runs.control_run(engine, ws, run_id, "cancel")


@router.post("/runs/{run_id}/rerun", response_model=s.Run, status_code=201, tags=["runs"],
             summary="Queue a new run with the same config snapshot and options")
def rerun(run_id: int, body: s.RerunRequest | None = None, engine: Engine = Depends(get_engine),
          ws: Workspace = Depends(current_workspace)):
    options = body.options.model_dump(exclude_none=True) if body else {}
    return runs.rerun(engine, ws, run_id, options)


@router.get("/runs/{run_id}/failures", response_model=s.Page[s.Failure], tags=["runs"],
            summary="What could not be fetched or parsed, with reason codes")
def list_failures(run_id: int, source: str | None = None, reason: str | None = None,
                  sort: str | None = Query(None, examples=["reason"]),
                  limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                  engine: Engine = Depends(get_engine), ws: Workspace = Depends(current_workspace)):
    return runs.list_failures(engine, ws, run_id, source=source, reason=reason, sort=sort,
                              limit=limit, offset=offset).to_dict()


@router.get("/runs/{run_id}/units", response_model=s.Page[s.WorkUnit], tags=["runs"],
            summary="The run's work units (its checkpoint): status, timings, yield, skips")
def list_units(run_id: int, status: str | None = None, source: str | None = None,
               limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
               engine: Engine = Depends(get_engine), ws: Workspace = Depends(current_workspace)):
    return runs.list_units(engine, ws, run_id, status=status, source=source, limit=limit,
                           offset=offset).to_dict()


@router.get("/runs/{run_id}/diff", response_model=s.RunDiff, tags=["runs"],
            summary="New / updated / unchanged organisations against another run "
                    "(default: the project's previous run)")
def diff(run_id: int, against: int | None = None,
         change: Literal["new", "updated", "unchanged", "only_in_against"] | None = None,
         limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
         engine: Engine = Depends(get_engine), ws: Workspace = Depends(current_workspace)):
    return runs.diff_runs(engine, ws, run_id, against=against, change=change, limit=limit,
                          offset=offset)


@router.get("/runs/{run_id}/events/history", response_model=list[s.Event], tags=["runs"],
            summary="The run's events as a JSON list (after `after`); the SSE stream's replay")
def event_history(run_id: int, after: int = Query(0, ge=0), limit: int = Query(1000, le=10000),
                  engine: Engine = Depends(get_engine), ws: Workspace = Depends(current_workspace)):
    return runs.read_events(engine, ws, run_id, after=after, limit=limit)


def _sse(event: dict) -> str:
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"


@router.get("/runs/{run_id}/events", tags=["runs"], response_class=StreamingResponse,
            summary="Live progress as Server-Sent Events: replays from `after` (or "
                    "Last-Event-ID), then follows until the run stops",
            responses={200: {"description": "text/event-stream; one event per message, `id` = "
                                            "seq, `event` = type, `data` = the event as JSON.",
                             "content": {"text/event-stream": {"example": (
                                 'id: 5\nevent: unit_finished\ndata: {"type": "unit_finished",'
                                 ' "seq": 5, "run_id": 42, "status": "done", "new": 12}\n\n')}}}})
async def events(run_id: int, request: Request, after: int = Query(0, ge=0),
                 follow: bool = Query(True, description="Keep the stream open while the run "
                                                        "is active."),
                 poll: float = Query(1.0, ge=0.05, le=10),
                 last_event_id: str | None = Header(None, alias="Last-Event-ID"),
                 engine: Engine = Depends(get_engine), ws: Workspace = Depends(current_workspace)):
    runs.run_row(engine, ws, run_id)                       # 404 before the stream opens
    start = after
    if last_event_id and last_event_id.isdigit():
        start = max(start, int(last_event_id))

    async def stream():
        cursor = start
        last_sent = time.monotonic()
        settled_since: float | None = None
        while True:
            if await request.is_disconnected():
                return
            batch = await run_in_threadpool(runs.read_events, engine, ws, run_id, after=cursor,
                                            limit=500)
            for event in batch:
                cursor = event["seq"]
                last_sent = time.monotonic()
                yield _sse(event)
            if any(_stops(e) for e in batch):
                return
            if batch:
                continue
            if not follow:
                return
            row = await run_in_threadpool(runs.run_row, engine, ws, run_id)
            if row["status"] in TERMINAL or row["status"] == "paused":
                # The last events may still be on their way to the database.
                settled_since = settled_since or time.monotonic()
                if time.monotonic() - settled_since > SETTLE_SECONDS:
                    return
            if time.monotonic() - last_sent > KEEPALIVE_SECONDS:
                last_sent = time.monotonic()
                yield ": keep-alive\n\n"
            await asyncio.sleep(poll)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/runs/{run_id}/export.{fmt}", tags=["exports"], response_class=Response,
            responses=EXPORT_RESPONSES,
            summary="A run's results as CSV, JSON or XLSX, regenerated from the database "
                    "(scope: run, new, project)")
async def export_run(run_id: int, fmt: Literal["csv", "json", "xlsx"],
                     scope: Literal["run", "new", "project"] | None = Query(
                         None, description="Default: run (or new, for an only-new "
                                           "scheduled run)."),
                     against: int | None = Query(None, description="With scope=new: compare "
                                                                   "with this run."),
                     engine: Engine = Depends(get_engine),
                     ws: Workspace = Depends(current_workspace)):
    filename, content, _ = await run_in_threadpool(exports.export, engine, ws, fmt,
                                                   run_id=run_id, scope=scope, against=against)
    return file_response(filename, content, fmt)


@router.post("/runs/{run_id}/export/google-sheets", response_model=s.SheetsResult,
             tags=["exports"],
             summary="Write a run's results to its Google Sheet (service-account flow)")
async def export_sheets(run_id: int, scope: Literal["run", "new", "project"] | None = None,
                        engine: Engine = Depends(get_engine),
                        ws: Workspace = Depends(current_workspace)):
    return await run_in_threadpool(exports.to_google_sheets, engine, ws, run_id, scope=scope)
