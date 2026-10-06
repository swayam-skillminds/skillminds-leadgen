"""SQLAlchemy models for the A.6 domain (APP-BUILD-PROMPT Section A.6).

Workspace -> Project -> Run, and the organisation-centric lead database:

* ``organizations`` -- one deduplicated organisation per row. ``record`` holds
  the full merged record (today: the local_business ``Lead``); the columns
  beside it are what queries, sorting and identity lookups need.
* ``identity_keys`` -- every phone, Maps place, domain and registry ID an
  organisation has carried, in claim order (``id``): "first claimant" is the
  lowest key id for a value, exactly as the original phone/place indexes.
* ``contacts`` / ``people`` / ``signals`` / ``evidence`` -- the facts, each
  with its source; ``evidence`` records every write: source, URL, fields, when.
* ``runs`` / ``run_organizations`` / ``work_units`` / ``failures`` /
  ``ai_usage`` -- run history; ``run_organizations`` powers "new since last run".
* ``suppression_*`` and ``schedules`` are created now and used from Phase 3.

Every user-owned table carries ``workspace_id`` (A.2.2): one default
workspace for now, so authentication can arrive later without a migration
of the data model.

Timestamps are timezone-aware UTC. The operational tables carried over from
the original store (seen URLs, dead domains, unit stats, enrichment marks)
keep epoch seconds, as they always had.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, MetaData, String, Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Stable constraint names, so Alembic can alter them on every backend.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _workspace_fk() -> Mapped[int]:
    return mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# --- tenancy and projects ------------------------------------------------------

class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = _created()


class Project(Base):
    """A long-lived lead database; repeated runs merge into it."""

    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("workspace_id", "slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class SavedConfig(Base):
    """A named, versioned run config; a new version never overwrites an old one."""

    __tablename__ = "saved_configs"
    __table_args__ = (UniqueConstraint("workspace_id", "name", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, default=1)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    document: Mapped[dict[str, Any]] = mapped_column(JSONType)
    created_at: Mapped[datetime] = _created()


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"),
                                            index=True)
    saved_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("saved_configs.id", ondelete="SET NULL"), nullable=True)
    #: queued | running | pausing | paused | stopping | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    #: Immutable copy of the config the run used.
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    kind: Mapped[str] = mapped_column(String(20), default="run")   # run | import | preview
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _created()


# --- organisations and their facts ----------------------------------------------

class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        Index("ix_org_project_name_key", "project_id", "name_key"),
        # (locality, category, score desc) is the order the fuzzy-name rule
        # walks a locality in -- the original store's index order.
        Index("ix_org_project_locality", "project_id", "locality", "category", "score"),
        Index("ix_org_project_lat", "project_id", "latitude"),
        Index("ix_org_project_legacy", "project_id", "legacy_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"),
                                            index=True)
    name: Mapped[str] = mapped_column(String(500))
    name_key: Mapped[str] = mapped_column(String(500), default="")
    locality: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(200), default="")
    country: Mapped[str] = mapped_column(String(2), default="")
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    #: Lead types that found or enriched it, e.g. ["local_business"].
    lead_types: Mapped[list[str]] = mapped_column(JSONType, default=list)
    #: The merged record itself (core.lead.Lead as a dict).
    record: Mapped[dict[str, Any]] = mapped_column(JSONType)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    fit: Mapped[float | None] = mapped_column(Float, nullable=True)
    intent: Mapped[float | None] = mapped_column(Float, nullable=True)
    reach: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_reasons: Mapped[list[str]] = mapped_column(JSONType, default=list)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Row id in the legacy SQLite store this organisation was imported from.
    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdentityKey(Base):
    __tablename__ = "identity_keys"
    __table_args__ = (
        UniqueConstraint("organization_id", "kind", "value"),
        Index("ix_identity_lookup", "project_id", "kind", "value", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    #: phone | place | domain | registry
    kind: Mapped[str] = mapped_column(String(20))
    value: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = _created()


class Contact(Base):
    """A phone, email, website, social link or address -- with where it came from."""

    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("organization_id", "channel", "value"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    #: phone | email | website | instagram | facebook | linkedin | whatsapp | address
    channel: Mapped[str] = mapped_column(String(20))
    value: Mapped[str] = mapped_column(String(1000))
    #: published (seen on a page) | inferred (Phase 6 email pattern; always labelled)
    kind: Mapped[str] = mapped_column(String(20), default="published")
    source: Mapped[str] = mapped_column(String(100), default="")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = _created()


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(300), default="")
    profile_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = _created()


class Signal(Base):
    """Dated evidence of buying intent or fit, from one lead type."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"),
                                               nullable=True)
    type: Mapped[str] = mapped_column(String(50))
    lead_type: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(500), default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    observed_at: Mapped[datetime] = _created()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    strength: Mapped[float] = mapped_column(Float, default=0.5)
    source_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)


class Evidence(Base):
    """Provenance: which source wrote which fields, from which URL, when."""

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"),
                                               nullable=True)
    lead_type: Mapped[str] = mapped_column(String(50), default="")
    source: Mapped[str] = mapped_column(String(100))
    url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    fields: Mapped[list[str]] = mapped_column(JSONType, default=list)
    fetched_at: Mapped[datetime] = _created()


class RunOrganization(Base):
    """Which organisations a run created or updated."""

    __tablename__ = "run_organizations"
    __table_args__ = (UniqueConstraint("run_id", "organization_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    #: new | updated -- the first thing that happened to it in this run.
    action: Mapped[str] = mapped_column(String(20))
    lead_type: Mapped[str] = mapped_column(String(50), default="")


class WorkUnitRow(Base):
    __tablename__ = "work_units"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    lead_type: Mapped[str] = mapped_column(String(50))
    source: Mapped[str] = mapped_column(String(100))
    area: Mapped[str | None] = mapped_column(String(200), nullable=True)
    category: Mapped[str | None] = mapped_column(String(200), nullable=True)
    scope: Mapped[str] = mapped_column(String(20), default="combo")
    #: pending | running | done | failed | skipped
    status: Mapped[str] = mapped_column(String(20), default="pending")
    found: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")


class FailureRow(Base):
    __tablename__ = "failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"),
                                               nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(100), index=True)
    url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    query: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    locality: Mapped[str | None] = mapped_column(String(200), nullable=True)
    category: Mapped[str | None] = mapped_column(String(200), nullable=True)
    business: Mapped[str | None] = mapped_column(String(500), nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _created()


class AIUsage(Base):
    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"),
                                               nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(100), default="")
    feature: Mapped[str] = mapped_column(String(50), default="")
    calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = _created()


class SuppressionList(Base):
    __tablename__ = "suppression_lists"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    name: Mapped[str] = mapped_column(String(200))
    #: exclude | flag
    mode: Mapped[str] = mapped_column(String(20), default="exclude")
    created_at: Mapped[datetime] = _created()


class SuppressionEntry(Base):
    __tablename__ = "suppression_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    list_id: Mapped[int] = mapped_column(ForeignKey("suppression_lists.id", ondelete="CASCADE"),
                                         index=True)
    #: name | domain | phone
    kind: Mapped[str] = mapped_column(String(20))
    value: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = _created()


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    saved_config_id: Mapped[int] = mapped_column(
        ForeignKey("saved_configs.id", ondelete="CASCADE"), index=True)
    cron: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    only_new: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


# --- operational state carried over from the original store ----------------------

class SeenUrl(Base):
    __tablename__ = "seen_urls"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"),
                                            primary_key=True)
    url: Mapped[str] = mapped_column(String(2000), primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    source: Mapped[str] = mapped_column(String(100))
    fetched_at: Mapped[float] = mapped_column(Float)


class DeadDomain(Base):
    """A host proved dead (DNS failure, loopback-only); shared by every project."""

    __tablename__ = "dead_domains"

    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"),
                                              primary_key=True)
    host: Mapped[str] = mapped_column(String(255), primary_key=True)
    reason: Mapped[str] = mapped_column(String(200))
    first_seen: Mapped[float] = mapped_column(Float)
    hits: Mapped[int] = mapped_column(Integer, default=1)


class UnitStat(Base):
    __tablename__ = "unit_stats"

    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"),
                                              primary_key=True)
    source: Mapped[str] = mapped_column(String(100), primary_key=True)
    area: Mapped[str] = mapped_column(String(200), primary_key=True)
    category: Mapped[str] = mapped_column(String(200), primary_key=True)
    runs: Mapped[int] = mapped_column(Integer, default=0)
    total_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    total_new_leads: Mapped[int] = mapped_column(Integer, default=0)
    last_run: Mapped[float] = mapped_column(Float)


class EnrichmentMark(Base):
    __tablename__ = "enrichment_marks"

    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    enricher: Mapped[str] = mapped_column(String(100), primary_key=True)
    workspace_id: Mapped[int] = _workspace_fk()
    done_at: Mapped[float] = mapped_column(Float)
