"""Saved configs: CRUD, versions, clone, validate, import, export."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.engine import Engine

from ...services import configs
from ...services.errors import ServiceError
from ...services.workspace import Workspace
from .. import schemas as s
from ..deps import current_workspace, get_engine

router = APIRouter(tags=["configs"])


@router.get("/configs", response_model=s.Page[s.SavedConfigSummary],
            summary="Saved configs, each once at its latest version (sort: name, updated_at, "
                    "created_at, id)")
def list_configs(q: str | None = None, project: str | None = None,
                 sort: str | None = Query(None, examples=["-updated_at"]),
                 limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                 engine: Engine = Depends(get_engine), ws: Workspace = Depends(current_workspace)):
    return configs.list_configs(engine, ws, q=q, project=project, sort=sort, limit=limit,
                                offset=offset).to_dict()


@router.post("/configs", response_model=s.SavedConfig, status_code=201,
             summary="Save a new config (validated against the run-config schema)")
def create_config(body: s.ConfigCreate, engine: Engine = Depends(get_engine),
                  ws: Workspace = Depends(current_workspace)):
    return configs.create_config(engine, ws, body.document, name=body.name,
                                 description=body.description)


@router.post("/configs/validate", response_model=s.ConfigValidation,
             summary="Validate a document without saving it; lists settings not applied yet")
def validate_config(document: dict[str, Any] = Body(
        ..., openapi_examples={"minimal": {"summary": "A country, a place, a category",
                                           "value": s.EXAMPLE_CONFIG}})):
    try:
        rc = configs.validate_document(document)
    except ServiceError as exc:
        return {"valid": False, "document": None,
                "errors": exc.errors or [{"loc": [], "msg": exc.detail, "type": "value_error"}],
                "unapplied": []}
    return {"valid": True, "document": rc.to_document(), "errors": [],
            "unapplied": rc.unapplied()}


@router.post("/configs/import", response_model=s.SavedConfig, status_code=201,
             summary="Import YAML/JSON text (a legacy config.yaml is converted); an existing "
                     "name gets a new version")
def import_config(body: s.ConfigImport, engine: Engine = Depends(get_engine),
                  ws: Workspace = Depends(current_workspace)):
    return configs.import_config(engine, ws, body.text, filename=body.filename, name=body.name)


@router.post("/configs/import/file", response_model=s.SavedConfig, status_code=201,
             summary="Import an uploaded YAML/JSON file")
async def import_config_file(file: UploadFile = File(...), name: str | None = Form(None),
                             engine: Engine = Depends(get_engine),
                             ws: Workspace = Depends(current_workspace)):
    return configs.import_config(engine, ws, await file.read(), filename=file.filename,
                                 name=name)


@router.get("/configs/{config_id}", response_model=s.SavedConfig,
            summary="One saved config: its latest version, or `?version=`")
def get_config(config_id: int, version: int | None = None, engine: Engine = Depends(get_engine),
               ws: Workspace = Depends(current_workspace)):
    return configs.get_config(engine, ws, config_id, version)


@router.delete("/configs/{config_id}", response_model=s.SavedConfig,
               summary="Delete every version (runs keep their own snapshot)")
def delete_config(config_id: int, engine: Engine = Depends(get_engine),
                  ws: Workspace = Depends(current_workspace)):
    return configs.delete_config(engine, ws, config_id)


@router.patch("/configs/{config_id}", response_model=s.SavedConfig, summary="Rename")
def rename_config(config_id: int, body: s.ConfigRename, engine: Engine = Depends(get_engine),
                  ws: Workspace = Depends(current_workspace)):
    return configs.rename_config(engine, ws, config_id, body.name)


@router.get("/configs/{config_id}/versions", response_model=list[s.ConfigVersion],
            summary="Every version, oldest first")
def list_versions(config_id: int, engine: Engine = Depends(get_engine),
                  ws: Workspace = Depends(current_workspace)):
    return configs.list_versions(engine, ws, config_id)


@router.post("/configs/{config_id}/versions", response_model=s.SavedConfig, status_code=201,
             summary="Save a new version (an unchanged document returns the latest)")
def save_version(config_id: int, body: s.ConfigVersionCreate,
                 engine: Engine = Depends(get_engine), ws: Workspace = Depends(current_workspace)):
    return configs.save_version(engine, ws, config_id, body.document,
                                description=body.description)


@router.post("/configs/{config_id}/clone", response_model=s.SavedConfig, status_code=201,
             summary="Copy a version into a new saved config")
def clone_config(config_id: int, body: s.ConfigClone, engine: Engine = Depends(get_engine),
                 ws: Workspace = Depends(current_workspace)):
    return configs.clone_config(engine, ws, config_id, body.name, version=body.version)


@router.get("/configs/{config_id}/export", response_class=Response,
            summary="Download a version as YAML or JSON (secrets removed)",
            responses={200: {"description": "The config document.", "content": {
                "application/yaml": {"example": "schema_version: 1\nname: Pune dentists\n"},
                "application/json": {"example": s.EXAMPLE_CONFIG}}}})
def export_config(config_id: int, version: int | None = None,
                  format: Literal["yaml", "json"] = "yaml",
                  engine: Engine = Depends(get_engine), ws: Workspace = Depends(current_workspace)):
    filename, text = configs.export_config(engine, ws, config_id, version=version, fmt=format)
    media = "application/json" if format == "json" else "application/yaml"
    return Response(text, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
