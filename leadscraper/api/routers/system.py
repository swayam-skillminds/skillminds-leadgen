"""Health, schema, catalog, estimate/preview, presets, settings, bundles."""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine

from ...services import bundle, catalog, estimates, health, retention, schema_hints, settings
from ...services.runs import resolve_document
from ...services.workspace import Workspace
from .. import schemas as s
from ..deps import current_workspace, get_engine, read_upload

router = APIRouter()

ZIP = "application/zip"


@router.get("/health", response_model=s.Health, tags=["system"],
            summary="Can this installation run? (database, browser, disk, keys, worker)",
            responses={503: {"model": s.Health, "description": "The database is unreachable."}})
def get_health(engine: Engine = Depends(get_engine)):
    report = health.health(engine)
    if report["status"] == "down":
        return JSONResponse(status_code=503, content=s.Health.model_validate(report)
                            .model_dump(mode="json"))
    return report


@router.get("/schema/run-config", response_model=s.RunConfigSchema, tags=["system"],
            summary="Run-config JSON Schema with UI hints and per-country applicability")
def get_run_config_schema(country: str | None = Query(None, examples=["US"]),
                          verticals: list[str] = Query(default_factory=list)):
    return schema_hints.run_config_schema(country, verticals)


@router.get("/catalog/lead-types", response_model=list[s.LeadTypeInfo], tags=["catalog"],
            summary="Lead types: what each finds, its signals, columns and config schema")
def get_lead_types(country: str | None = Query(None, examples=["IN"])):
    return catalog.lead_types(country)


@router.get("/catalog/sources", response_model=list[s.SourceInfo], tags=["catalog"],
            summary="Sources: where each applies, terms notes, last live check, health")
def get_sources(country: str | None = Query(None, examples=["US"]),
                verticals: list[str] = Query(default_factory=list),
                kind: str | None = Query(None, pattern="^(discovery|enrichment)$"),
                applicable: bool | None = None,
                engine: Engine = Depends(get_engine), ws: Workspace = Depends(current_workspace)):
    items = catalog.sources(engine, ws, country=country, verticals=verticals)
    if kind:
        items = [i for i in items if i["kind"] == kind]
    if applicable is not None:
        items = [i for i in items if i["applicable"] == applicable]
    return items


@router.post("/estimate", response_model=s.Estimate, tags=["runs"],
             summary="Estimate a config without running it (no requests made)")
async def post_estimate(body: s.EstimateRequest, engine: Engine = Depends(get_engine),
                        ws: Workspace = Depends(current_workspace)):
    document, _ = resolve_document(engine, ws, document=body.config,
                                   saved_config_id=body.saved_config_id, version=body.version)
    return await run_in_threadpool(estimates.estimate, engine, ws, document,
                                   body.options.model_dump(exclude_none=True))


@router.post("/preview", response_model=s.Run, status_code=202, tags=["runs"],
             summary="Queue a ~2-minute preview run; its sample and estimate land on the run")
def post_preview(body: s.PreviewRequest, engine: Engine = Depends(get_engine),
                 ws: Workspace = Depends(current_workspace)):
    document, _ = resolve_document(engine, ws, document=body.config,
                                   saved_config_id=body.saved_config_id, version=body.version)
    return estimates.submit_preview(engine, ws, document, seconds=body.seconds, area=body.area,
                                    category=body.category)


@router.get("/presets", response_model=list[s.Preset], tags=["configs"],
            summary="Shipped presets and the user's own (from bundles)")
def get_presets():
    out = []
    for source, directory in zip(("shipped", "user"), bundle.preset_dirs()):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            geography = document.get("geography") or {}
            out.append({"file": path.name, "source": source, "name": document.get("name"),
                        "project": document.get("project"), "country": geography.get("country"),
                        "places": list(geography.get("places") or []), "document": document})
    return out


@router.get("/settings", response_model=s.SettingsOut, tags=["settings"],
            summary="Workspace settings: retention, keep-awake, robots.txt; data location")
def get_settings(engine: Engine = Depends(get_engine), ws: Workspace = Depends(current_workspace)):
    return settings.get_settings(engine, ws)


@router.patch("/settings", response_model=s.SettingsOut, tags=["settings"],
              summary="Change workspace settings (nested keys merge)")
def patch_settings(body: s.SettingsUpdate, engine: Engine = Depends(get_engine),
                   ws: Workspace = Depends(current_workspace)):
    return settings.update_settings(engine, ws, body.model_dump(exclude_none=True))


@router.post("/settings/purge", response_model=s.PurgeResult, tags=["settings"],
             summary="Apply the retention settings now (the worker also does, hourly)")
def post_purge(dry_run: bool = False, engine: Engine = Depends(get_engine),
               ws: Workspace = Depends(current_workspace)):
    return retention.purge(engine, ws, dry_run=dry_run)


@router.get("/bundle/export", tags=["bundles"], response_class=Response,
            summary="Download a bundle: configs, presets, lists, projects (+ leads, history)",
            responses={200: {"content": {ZIP: {"example": "<zip archive>"}},
                             "description": "A zip; never contains secrets."}})
async def get_bundle(project_id: list[int] | None = Query(None),
                     include_leads: bool = False, include_history: bool = False,
                     engine: Engine = Depends(get_engine),
                     ws: Workspace = Depends(current_workspace)):
    data = await run_in_threadpool(bundle.export_bundle, engine, ws, project_ids=project_id,
                                   include_leads=include_leads,
                                   include_history=include_history)
    return Response(data, media_type=ZIP, headers={
        "Content-Disposition": 'attachment; filename="leadscraper-bundle.zip"'})


@router.post("/bundle/import", response_model=s.BundleImportResult, tags=["bundles"],
             summary="Import a bundle (version-checked; additive and idempotent)")
async def post_bundle(file: UploadFile = File(...), leads: bool = True, history: bool = True,
                      engine: Engine = Depends(get_engine),
                      ws: Workspace = Depends(current_workspace)) -> Any:
    data = await read_upload(file)
    return await run_in_threadpool(bundle.import_bundle, engine, ws, data, leads=leads,
                                   history=history)
