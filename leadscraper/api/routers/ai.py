"""AI (Phase 5): providers and keys, config assist, candidate sources, usage.

Config assist (ICP builder, planner, source discovery) calls an AI provider
synchronously and returns a draft or suggestions for confirmation; nothing is
saved or run. Nothing here scrapes.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.engine import Engine

from ...services import ai
from ...services.workspace import Workspace
from .. import schemas as s
from ..deps import current_workspace, get_engine

router = APIRouter(prefix="/ai")


@router.get("/providers", response_model=s.AIProvidersOut, tags=["ai"],
            summary="AI providers: capabilities, models, whether a key is configured")
def providers(engine: Engine = Depends(get_engine), ws: Workspace = Depends(current_workspace)):
    return ai.providers(engine, ws)


@router.post("/providers/{name}/test", response_model=s.ProviderTestOut, tags=["ai"],
             summary="Connectivity test: one tiny call (costs a fraction of a cent)")
def test_provider(name: str, engine: Engine = Depends(get_engine),
                  ws: Workspace = Depends(current_workspace)):
    return ai.test_provider(engine, ws, name)


@router.get("/offerings", response_model=s.OfferingsOut, tags=["ai"],
            summary="Skillminds offerings the ICP builder starts from")
def offerings():
    return {"items": ai.offerings()}


@router.post("/icp", response_model=s.ICPDraftOut, tags=["ai"],
             summary="ICP builder: a brief -> a draft run config (returned, never run)")
def icp(body: s.ICPRequest, engine: Engine = Depends(get_engine),
        ws: Workspace = Depends(current_workspace)):
    return ai.build_icp(engine, ws, brief=body.brief, offering=body.offering,
                        country=body.country, places=body.places)


@router.post("/plan", response_model=s.PlanOut, tags=["ai"],
             summary="Planner: areas, query synonyms, title variants (suggestions only)")
def plan(body: s.PlanRequest, engine: Engine = Depends(get_engine),
         ws: Workspace = Depends(current_workspace)):
    return ai.plan_config(engine, ws, body.config, roles=body.roles)


@router.post("/source-discovery", response_model=s.SourceDiscoveryOut, tags=["ai"],
             summary="Propose directories and feeds; they land as candidate sources")
def source_discovery(body: s.SourceDiscoveryRequest, engine: Engine = Depends(get_engine),
                     ws: Workspace = Depends(current_workspace)):
    return ai.discover_sources(engine, ws, country=body.country, categories=body.categories,
                               places=body.places, industry=body.industry, limit=body.limit)


@router.get("/candidate-sources", response_model=s.Page[s.CandidateSourceOut], tags=["ai"],
            summary="Candidate sources (proposed / approved / rejected)")
def list_candidates(status: Literal["proposed", "approved", "rejected"] | None = None,
                    country: str | None = None, limit: int = Query(50, ge=1, le=500),
                    offset: int = Query(0, ge=0), engine: Engine = Depends(get_engine),
                    ws: Workspace = Depends(current_workspace)):
    return ai.list_candidates(engine, ws, status=status, country=country, limit=limit,
                              offset=offset).to_dict()


@router.post("/candidate-sources", response_model=s.CandidateSourceOut, status_code=201,
             tags=["ai"], summary="Add a candidate source yourself (it still needs approving)")
def add_candidate(body: s.CandidateCreate, engine: Engine = Depends(get_engine),
                  ws: Workspace = Depends(current_workspace)):
    return ai.add_candidate(engine, ws, url=body.url, name=body.name, country=body.country,
                            place=body.place, category=body.category, rationale=body.rationale)


@router.post("/candidate-sources/{candidate_id}/decision", response_model=s.CandidateSourceOut,
             tags=["ai"], summary="Approve or reject a candidate source")
def decide(candidate_id: int, body: s.CandidateDecision, engine: Engine = Depends(get_engine),
           ws: Workspace = Depends(current_workspace)):
    return ai.decide_candidate(engine, ws, candidate_id, body.status)


@router.delete("/candidate-sources/{candidate_id}", response_model=s.CandidateSourceOut,
               tags=["ai"], summary="Delete a candidate source")
def delete_candidate(candidate_id: int, engine: Engine = Depends(get_engine),
                     ws: Workspace = Depends(current_workspace)):
    return ai.delete_candidate(engine, ws, candidate_id)


@router.get("/usage", response_model=s.AIUsageOut, tags=["ai"],
            summary="AI spend from the ledger, by provider and feature (one run, or all)")
def usage(run_id: int | None = None, engine: Engine = Depends(get_engine),
          ws: Workspace = Depends(current_workspace)):
    return ai.usage(engine, ws, run_id=run_id)
