"""The organisation store: one project's deduplicated leads, on SQLAlchemy.

Every write goes through ``upsert`` (a new finding) or ``merge_into`` (an
enrichment of a known record). Identity is decided by ``core.identity``; this
module answers its lookups (``CandidateIndex``) from indexed tables, in the
same row order the original SQLite store's indexes returned, so every
dedup decision is reproduced exactly.

Alongside the record each write leaves:

* identity keys (phones, Maps place, domain, registry IDs) in claim order;
* contacts, each with the source that first published it;
* an evidence row -- source, URL, fields changed, when;
* the run's membership row ("new" / "updated"), when a run is attached.

After an enrichment (``merge_into``) or a rename (``update_fields``) the
record is checked against the store again and folded into any record it now
matches: enrichment that gives two stored records the same phone used to
leave them as duplicates forever.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from dataclasses import asdict, fields as dataclass_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from sqlalchemy import and_, delete, func, select, tuple_, update
from sqlalchemy.engine import Connection, Engine

from ..core.errors import is_error_title
from ..core.identity import DedupOptions, IdentityResolver, identity_domain, identity_keys
from ..core.lead import Lead
from ..core.normalize import maps_place_key
from .engine import init_schema, is_sqlite, open_database, url_for
from .models import (
    Contact, DeadDomain, EnrichmentMark, Evidence, IdentityKey, Organization, OrgAttribute,
    Person, Project, RunOrganization, SeenUrl, Signal, UnitStat, Workspace,
)

__all__ = ["LeadStore", "DedupOptions", "UpsertAction", "slugify_project", "sort_leads",
           "store_target"]

log = logging.getLogger(__name__)

UpsertAction = Literal["new", "merged", "unchanged"]

ORGS = Organization.__table__
KEYS = IdentityKey.__table__
CONTACTS = Contact.__table__
PEOPLE = Person.__table__
EVIDENCE = Evidence.__table__
RUN_ORGS = RunOrganization.__table__
SIGNALS = Signal.__table__
SEEN = SeenUrl.__table__
DEAD = DeadDomain.__table__
UNITS = UnitStat.__table__
MARKS = EnrichmentMark.__table__
ATTRS = OrgAttribute.__table__

#: Fields whose change can make a record match another one.
_IDENTITY_FIELDS = {"phones", "gmaps_url", "website", "registry_ids", "latitude", "business_name"}
#: Channels a record's contacts are read from: (channel, Lead attribute).
_CONTACT_FIELDS = (("phone", "phones"), ("email", "emails"), ("website", "website"),
                   ("instagram", "instagram"), ("facebook", "facebook"),
                   ("linkedin", "linkedin"), ("whatsapp", "whatsapp"), ("address", "address"))
#: Sources whose contacts a model read from page text: still published on the
#: page (A.2.5), but held with less confidence than a parsed field.
_AI_SOURCES = {"ai_extract"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _epoch_to_dt(value: float | None) -> datetime:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return utcnow()


def slugify_project(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "default"


def _populated_fields(lead: Lead) -> list[str]:
    return [f.name for f in dataclass_fields(lead)
            if f.name not in ("first_seen", "last_updated", "score")
            and getattr(lead, f.name) not in (None, "", [], {}, False, 0, 0.0)]


class _LazyRecord:
    """A stored record fetched only when a rule actually looks inside it."""

    __slots__ = ("_store", "_id", "_lead")

    def __init__(self, store: "LeadStore", record_id: int):
        self._store, self._id, self._lead = store, record_id, None

    def __getattr__(self, name: str) -> Any:
        if self._lead is None:
            self._lead = self._store.get(self._id)
        return getattr(self._lead, name)


class LeadStore:
    """Thread-safe store for one project. All writes go through ``upsert`` / ``merge_into``.

    ``db`` is a file path (a SQLite application DB), a database URL, or an
    Engine shared with other stores. ``project`` names the lead database
    inside it; ``run_id`` attaches writes to a run's history.
    """

    def __init__(self, db: str | Path | Engine, scorer=None,
                 options: DedupOptions | None = None, *,
                 project: str = "default", workspace: str = "default",
                 project_description: str = "", run_id: int | None = None,
                 lead_type: str = "local_business", country: str = ""):
        if scorer is None:
            from ..leadtypes.local_business.scoring import Scorer

            scorer = Scorer()
        self.options = options or DedupOptions()
        self.scorer = scorer
        #: The lead type writes are tagged with. Per thread: the engine runs
        #: units of different lead types side by side against one store.
        self._tag = threading.local()
        self._default_lead_type = lead_type
        self.country = country
        self.run_id = run_id
        #: Set by an importer so contacts and evidence name the import, not
        #: the record's first source (which may not be where a value came from).
        self.source_override: str | None = None
        self._lock = threading.RLock()
        #: This store created its project (a dry run removes it again if unused).
        self.project_created = False
        if isinstance(db, Engine):
            self.engine, self._owns_engine = db, False
            init_schema(db)
        else:
            self.engine, self._owns_engine = open_database(db), True
        self.db_path = (Path(url_for(db).split("///", 1)[-1])
                        if not isinstance(db, Engine) and is_sqlite(str(self.engine.url))
                        else None)
        self._conn: Connection = self.engine.connect()
        self.workspace_id = self._ensure_workspace(workspace)
        self.project_id = self._ensure_project(project, project_description)
        self.project_name = project
        self._conn.commit()
        self.stats = {"new": 0, "merged": 0, "unchanged": 0, "consolidated": 0}

    @property
    def lead_type(self) -> str:
        return getattr(self._tag, "lead_type", None) or self._default_lead_type

    @lead_type.setter
    def lead_type(self, key: str) -> None:
        """Tag this thread's writes with `key`; also the default for new threads."""
        self._tag.lead_type = key
        self._default_lead_type = key

    def tag_thread(self, key: str) -> None:
        """Tag only the calling thread's writes (a worker running one lead type's unit)."""
        self._tag.lead_type = key

    # --- tenancy ---------------------------------------------------------------

    def _ensure_workspace(self, name: str) -> int:
        table = Workspace.__table__
        row = self._conn.execute(select(table.c.id).where(table.c.name == name)).first()
        if row:
            return int(row[0])
        return int(self._conn.execute(
            table.insert().values(name=name, created_at=utcnow())).inserted_primary_key[0])

    def _ensure_project(self, name: str, description: str) -> int:
        table = Project.__table__
        slug = slugify_project(name)
        row = self._conn.execute(select(table.c.id).where(
            table.c.workspace_id == self.workspace_id, table.c.slug == slug)).first()
        if row:
            return int(row[0])
        now = utcnow()
        self.project_created = True
        return int(self._conn.execute(table.insert().values(
            workspace_id=self.workspace_id, name=name, slug=slug, description=description,
            created_at=now, updated_at=now)).inserted_primary_key[0])

    # --- helpers -----------------------------------------------------------------

    def _insert_ignore(self, table, values: dict[str, Any] | list[dict[str, Any]]) -> None:
        if not values:
            return
        if self.engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        self._conn.execute(dialect_insert(table).on_conflict_do_nothing(), values)

    def _org_filter(self):
        return ORGS.c.project_id == self.project_id

    @staticmethod
    def _lead(record: dict[str, Any]) -> Lead:
        return Lead.from_dict(record)

    # --- CandidateIndex (what core.identity asks) ----------------------------------

    def first_claimant(self, kind: str, value: str, exclude: frozenset[int]) -> int | None:
        with self._lock:
            rows = self._conn.execute(
                select(KEYS.c.organization_id)
                .where(KEYS.c.project_id == self.project_id, KEYS.c.kind == kind,
                       KEYS.c.value == value)
                .order_by(KEYS.c.id)).all()
        for (org_id,) in rows:
            if org_id not in exclude:
                return int(org_id)
        return None

    def holders(self, kind: str, values: Iterable[str],
                exclude: frozenset[int]) -> list[tuple[int, Lead]]:
        values = sorted({v for v in values if v})
        if not values:
            return []
        with self._lock:
            ids = select(KEYS.c.organization_id).where(
                KEYS.c.project_id == self.project_id, KEYS.c.kind == kind,
                KEYS.c.value.in_(values))
            rows = self._conn.execute(
                select(ORGS.c.id, ORGS.c.record).where(ORGS.c.id.in_(ids))
                .order_by(ORGS.c.id)).all()
        return [(int(r.id), self._lead(r.record)) for r in rows if r.id not in exclude]

    def by_name_key(self, name_key: str, exclude: frozenset[int]) -> list[tuple[int, Lead]]:
        with self._lock:
            rows = self._conn.execute(
                select(ORGS.c.id, ORGS.c.record)
                .where(self._org_filter(), ORGS.c.name_key == name_key)
                .order_by(ORGS.c.id)).all()
        return [(int(r.id), self._lead(r.record)) for r in rows if r.id not in exclude]

    def in_locality(self, locality: str,
                    exclude: frozenset[int]) -> list[tuple[int, str, Any]]:
        # (category, score desc, id): the order the original store's
        # (locality, category, score DESC) index handed rows back in, which
        # decides ties between equally good fuzzy matches. Categories compare
        # bytewise, as in SQLite; a Postgres locale collation ("Decorators"
        # before "DJs") would break those ties differently on a server.
        category = (ORGS.c.category.collate("C")
                    if self.engine.dialect.name == "postgresql" else ORGS.c.category)
        with self._lock:
            rows = self._conn.execute(
                select(ORGS.c.id, ORGS.c.name_key)
                .where(self._org_filter(), ORGS.c.locality == locality)
                .order_by(category, ORGS.c.score.desc(), ORGS.c.id)).all()
        return [(int(r.id), r.name_key, _LazyRecord(self, int(r.id)))
                for r in rows if r.id not in exclude]

    def near(self, lat: float, lng: float, radius_m: float,
             exclude: frozenset[int]) -> list[tuple[int, Lead]]:
        # A generous box; the rule itself checks the exact distance.
        dlat = radius_m / 111_000.0 * 1.5 + 1e-4
        dlng = dlat / max(math.cos(math.radians(lat)), 0.01)
        with self._lock:
            rows = self._conn.execute(
                select(ORGS.c.id, ORGS.c.record)
                .where(self._org_filter(),
                       ORGS.c.latitude.between(lat - dlat, lat + dlat),
                       ORGS.c.longitude.between(lng - dlng, lng + dlng))
                .order_by(ORGS.c.id)).all()
        return [(int(r.id), self._lead(r.record)) for r in rows if r.id not in exclude]

    def get(self, lead_id: int) -> Lead | None:
        with self._lock:
            row = self._conn.execute(select(ORGS.c.record).where(
                ORGS.c.id == lead_id, self._org_filter())).first()
        return self._lead(row.record) if row else None

    # --- matching ----------------------------------------------------------------

    def find_match(self, lead: Lead, exclude: Iterable[int] = ()) -> int | None:
        with self._lock:
            return IdentityResolver(self.options).find_match(lead, self, exclude)

    # --- write -------------------------------------------------------------------

    def upsert(self, lead: Lead, *, legacy_id: int | None = None,
               now: float | None = None) -> tuple[int, UpsertAction, list[str]]:
        """Insert or merge. Returns (lead_id, action, changed_fields).

        `now` stamps a merge with a given time instead of the clock -- used
        when replaying old records, so they keep their own dates.
        """
        lead = lead.normalized()
        if not lead.business_name.strip():
            raise ValueError("refusing to store a lead with no business name")
        # A bot wall's <title> parses as a perfectly good name and then survives
        # dedup, so it has to be stopped here rather than cleaned up later.
        if is_error_title(lead.business_name):
            raise ValueError("refusing to store an error-page title as a business name: "
                             f"{lead.business_name!r}")

        with self._lock:
            match_id = self.find_match(lead)
            if match_id is None:
                lead_id = self._insert(lead, legacy_id=legacy_id)
                self.stats["new"] += 1
                return lead_id, "new", ["*new*"]

            existing = self.get(match_id)
            # `merge` keeps the first gmaps_url, so a second place that turned
            # out to be this business is indexed here or it would split later.
            self._claim(match_id, "place", [maps_place_key(lead.gmaps_url)])
            stamp = None if now is None else max(now, existing.last_updated)
            changed = existing.merge(lead, now=stamp)
            if not changed:
                # Found again with nothing to add: still one of this run's
                # organisations ("found in this run", "unchanged" in a diff).
                self._note_run(match_id, "unchanged")
                self._conn.commit()
                self.stats["unchanged"] += 1
                return match_id, "unchanged", []
            self._update(match_id, existing, source=self._source_of(lead),
                         url=(lead.source_urls or [None])[0], changed=changed)
            self.stats["merged"] += 1
            return match_id, "merged", changed

    def merge_into(self, lead_id: int, partial: Lead) -> list[str]:
        """Merge a partial Lead into a KNOWN lead id.

        Enrichment must not go through `upsert`: an enricher often discovers a
        phone number the stored record doesn't have yet, and `upsert`'s
        "different phones means different business" guard would then split the
        lead in two. Here we already know they're the same record -- and once
        it has merged, the record may now match another one, which is folded in.
        """
        with self._lock:
            existing = self.get(lead_id)
            if existing is None:
                return []
            partial = partial.normalized()
            self._claim(lead_id, "place", [maps_place_key(partial.gmaps_url)])
            changed = existing.merge(partial)
            if not changed:
                self._conn.commit()
                return changed
            self._update(lead_id, existing, source=self._source_of(partial),
                         url=(partial.source_urls or [None])[0], changed=changed)
            self.stats["merged"] += 1
            if _IDENTITY_FIELDS & set(changed):
                self.consolidate(lead_id)
            return changed

    def update_fields(self, lead_id: int, *, _source: str = "update", **fields) -> list[str]:
        """Overwrite specific fields on a known lead.

        Used by the AI polish pass, which legitimately *replaces* a bad
        SERP-derived name rather than merging alongside it -- `merge` is the
        wrong tool there because it deliberately protects the existing name.
        """
        with self._lock:
            lead = self.get(lead_id)
            if lead is None:
                return []
            changed = []
            for name, value in fields.items():
                if not hasattr(lead, name):
                    continue
                if getattr(lead, name) != value:
                    setattr(lead, name, value)
                    changed.append(name)
            if changed:
                self._update(lead_id, lead, source=_source, url=None, changed=changed)
                if _IDENTITY_FIELDS & set(changed):
                    self.consolidate(lead_id)
            return changed

    def consolidate(self, lead_id: int, max_rounds: int = 5) -> int:
        """Fold `lead_id` into any record it now matches (and repeat).

        The lower id survives, so a record's first-seen identity is stable.
        Returns the id the record lives under afterwards.
        """
        current = lead_id
        with self._lock:
            for _ in range(max_rounds):
                record = self.get(current)
                if record is None:
                    return current
                other = self.find_match(record, exclude={current})
                if other is None:
                    return current
                survivor, victim = sorted((current, other))
                self._fold(survivor, victim)
                current = survivor
        return current

    def fold_pair(self, a: int, b: int, *, note: str = "") -> bool:
        """Merge two records a judge (AI duplicate adjudication) found to be one.

        The identity rules kept them apart, so this is an explicit decision:
        the lower id survives, as in consolidation, and `note` is kept on it.
        """
        if a == b:
            return False
        with self._lock:
            survivor, victim = sorted((a, b))
            if self.get(survivor) is None or self.get(victim) is None:
                return False
            self._fold(survivor, victim)
            if note:
                kept = self.get(survivor)
                if kept is not None and note not in kept.notes:
                    kept.notes.append(note)
                    self._update(survivor, kept, source="ai_dedup", url=None,
                                 changed=["notes"])
            self._conn.commit()
            return True

    def _fold(self, survivor: int, victim: int) -> None:
        keep, gone = self.get(survivor), self.get(victim)
        if keep is None or gone is None:
            return
        keep.merge(gone)
        log.info("consolidated %r (#%d) into %r (#%d)", gone.business_name, victim,
                 keep.business_name, survivor)
        conn = self._conn
        # Keys: drop the victim's duplicates of the survivor's, re-point the
        # rest in place so their claim order is kept.
        held = select(KEYS.c.kind, KEYS.c.value).where(KEYS.c.organization_id == survivor)
        conn.execute(delete(KEYS).where(KEYS.c.organization_id == victim,
                                        tuple_(KEYS.c.kind, KEYS.c.value).in_(held)))
        conn.execute(update(KEYS).where(KEYS.c.organization_id == victim)
                     .values(organization_id=survivor))
        held_contacts = select(CONTACTS.c.channel, CONTACTS.c.value).where(
            CONTACTS.c.organization_id == survivor)
        conn.execute(delete(CONTACTS).where(
            CONTACTS.c.organization_id == victim,
            tuple_(CONTACTS.c.channel, CONTACTS.c.value).in_(held_contacts)))
        for table in (CONTACTS, PEOPLE, SIGNALS, EVIDENCE):
            conn.execute(update(table).where(table.c.organization_id == victim)
                         .values(organization_id=survivor))
        # AI attributes: the survivor's own win; the victim's fill its gaps.
        held_attrs = select(ATTRS.c.key).where(ATTRS.c.organization_id == survivor)
        conn.execute(delete(ATTRS).where(ATTRS.c.organization_id == victim,
                                         ATTRS.c.key.in_(held_attrs)))
        conn.execute(update(ATTRS).where(ATTRS.c.organization_id == victim)
                     .values(organization_id=survivor))
        runs_held = select(RUN_ORGS.c.run_id).where(RUN_ORGS.c.organization_id == survivor)
        conn.execute(delete(RUN_ORGS).where(RUN_ORGS.c.organization_id == victim,
                                            RUN_ORGS.c.run_id.in_(runs_held)))
        conn.execute(update(RUN_ORGS).where(RUN_ORGS.c.organization_id == victim)
                     .values(organization_id=survivor))
        marks = conn.execute(select(MARKS.c.enricher, MARKS.c.done_at).where(
            MARKS.c.organization_id == victim)).all()
        self._insert_ignore(MARKS, [{"organization_id": survivor, "enricher": m.enricher,
                                     "workspace_id": self.workspace_id, "done_at": m.done_at}
                                    for m in marks])
        conn.execute(delete(MARKS).where(MARKS.c.organization_id == victim))
        conn.execute(delete(ORGS).where(ORGS.c.id == victim))
        self._update(survivor, keep, source="consolidation", url=None,
                     changed=[f"merged #{victim}"])
        self.stats["consolidated"] += 1

    # --- row writers ---------------------------------------------------------------

    def _source_of(self, lead: Lead) -> str:
        if self.source_override:
            return self.source_override
        return lead.sources[0] if lead.sources else "unknown"

    def _score(self, lead: Lead) -> dict[str, Any]:
        """Score the lead (setting lead.score) and return the score columns."""
        self.scorer.apply(lead)
        explain = getattr(self.scorer, "explain", None)
        overall = explain(lead) if callable(explain) else None
        return {
            "score": lead.score,
            "fit": overall.fit if overall else None,
            "intent": overall.intent if overall else None,
            "reach": overall.reach if overall else None,
            "score_reasons": list(overall.reasons) if overall else [],
        }

    def _columns(self, lead: Lead) -> dict[str, Any]:
        return {
            "name": lead.business_name[:500],
            "name_key": lead.name_key[:500],
            "locality": lead.locality or "",
            "category": lead.category or "",
            "domain": identity_domain(lead.website),
            "record": asdict(lead),
            "latitude": lead.latitude,
            "longitude": lead.longitude,
        }

    def insert_record(self, lead: Lead, *, legacy_id: int | None = None,
                      index: bool = True) -> int:
        """Store a record as it is, without matching it against the store.

        For importers that already know the record is its own organisation.
        With ``index=False`` its identity keys are left for the caller to
        claim (``claim``/``index_record``), so an import can reproduce the
        original store's claim order.
        """
        with self._lock:
            return self._insert(lead.normalized(), legacy_id=legacy_id, index=index)

    def claim(self, lead_id: int, kind: str, values: Iterable[str | None]) -> None:
        self._claim(lead_id, kind, values)
        self._conn.commit()

    def index_record(self, lead_id: int) -> None:
        lead = self.get(lead_id)
        if lead is not None:
            with self._lock:
                self._sync_keys(lead_id, lead)
                self._conn.commit()

    def _insert(self, lead: Lead, *, legacy_id: int | None = None, index: bool = True) -> int:
        scores = self._score(lead)
        now = time.time()
        with self._lock:
            lead_id = int(self._conn.execute(ORGS.insert().values(
                workspace_id=self.workspace_id, project_id=self.project_id,
                country=self.country, lead_types=[self.lead_type], legacy_id=legacy_id,
                created_at=_epoch_to_dt(lead.first_seen or now), updated_at=_epoch_to_dt(now),
                **self._columns(lead), **scores,
            )).inserted_primary_key[0])
            if index:
                self._sync_keys(lead_id, lead)
            source = self._source_of(lead)
            self._sync_contacts(lead_id, lead, source)
            self._evidence(lead_id, source, (lead.source_urls or [None])[0],
                           _populated_fields(lead))
            self._note_run(lead_id, "new")
            self._conn.commit()
        return lead_id

    def _update(self, lead_id: int, lead: Lead, *, source: str, url: str | None,
                changed: list[str]) -> None:
        scores = self._score(lead)
        with self._lock:
            row = self._conn.execute(select(ORGS.c.lead_types).where(ORGS.c.id == lead_id)).first()
            lead_types = list(row.lead_types or []) if row else []
            if self.lead_type not in lead_types:
                lead_types.append(self.lead_type)
            self._conn.execute(update(ORGS).where(ORGS.c.id == lead_id).values(
                lead_types=lead_types, updated_at=utcnow(), **self._columns(lead), **scores))
            self._sync_keys(lead_id, lead)
            self._sync_contacts(lead_id, lead, source)
            self._evidence(lead_id, source, url, changed)
            self._note_run(lead_id, "updated")
            self._conn.commit()

    def _claim(self, lead_id: int, kind: str, values: Iterable[str | None]) -> None:
        """Index keys for a record; the first record to claim a value keeps it."""
        with self._lock:
            for value in values:
                if value:
                    self._insert_ignore(KEYS, {
                        "workspace_id": self.workspace_id, "project_id": self.project_id,
                        "organization_id": lead_id, "kind": kind, "value": value[:500],
                        "created_at": utcnow()})

    def _sync_keys(self, lead_id: int, lead: Lead) -> None:
        # Phones, then the Maps place: the order the original store indexed
        # them in, which decides who claims a value first.
        keys = identity_keys(lead)
        for kind in ("phone", "place", "domain", "registry"):
            self._claim(lead_id, kind, keys[kind])

    def _sync_contacts(self, lead_id: int, lead: Lead, source: str) -> None:
        wanted: list[tuple[str, str]] = []
        for channel, attr in _CONTACT_FIELDS:
            value = getattr(lead, attr)
            for item in (value if isinstance(value, list) else [value]):
                if item:
                    wanted.append((channel, str(item)[:1000]))
        existing = {(r.channel, r.value) for r in self._conn.execute(
            select(CONTACTS.c.channel, CONTACTS.c.value).where(
                CONTACTS.c.organization_id == lead_id)).all()}
        for channel, value in existing - set(wanted):
            self._conn.execute(delete(CONTACTS).where(
                CONTACTS.c.organization_id == lead_id, CONTACTS.c.channel == channel,
                CONTACTS.c.value == value))
        fresh = [{"workspace_id": self.workspace_id, "organization_id": lead_id,
                  "channel": channel, "value": value, "kind": "published", "source": source,
                  "confidence": 0.8 if source in _AI_SOURCES else 1.0, "created_at": utcnow()}
                 for channel, value in dict.fromkeys(wanted) if (channel, value) not in existing]
        self._insert_ignore(CONTACTS, fresh)
        if lead.contact_name:
            known = self._conn.execute(select(PEOPLE.c.id).where(
                PEOPLE.c.organization_id == lead_id, PEOPLE.c.name == lead.contact_name)).first()
            if not known:
                self._conn.execute(PEOPLE.insert().values(
                    workspace_id=self.workspace_id, organization_id=lead_id,
                    name=lead.contact_name[:300], source=source, created_at=utcnow()))

    def _evidence(self, lead_id: int, source: str, url: str | None, fields: list[str]) -> None:
        self._conn.execute(EVIDENCE.insert().values(
            workspace_id=self.workspace_id, organization_id=lead_id, run_id=self.run_id,
            lead_type=self.lead_type, source=source[:100], url=url[:2000] if url else None,
            fields=list(fields), fetched_at=utcnow()))

    def _note_run(self, lead_id: int, action: str) -> None:
        if self.run_id is None:
            return
        # The first thing that happened to an organisation in a run is what
        # the run did to it: new stays new however often it merges later. A
        # re-find that added nothing ("unchanged") is upgraded by a later write.
        self._insert_ignore(RUN_ORGS, {"workspace_id": self.workspace_id, "run_id": self.run_id,
                                       "organization_id": lead_id, "action": action,
                                       "lead_type": self.lead_type})
        if action == "updated":
            self._conn.execute(update(RUN_ORGS).where(
                RUN_ORGS.c.run_id == self.run_id, RUN_ORGS.c.organization_id == lead_id,
                RUN_ORGS.c.action == "unchanged").values(action="updated"))

    # --- attributes (Phase 5: AI industry tags, size estimates, summaries) -------------

    def set_attribute(self, lead_id: int, key: str, value: Any, *, origin: str = "ai",
                      estimated: bool = False, producer: str = "") -> bool:
        """Store an attribute that is not a published fact; True if it changed.

        Every write leaves an evidence row naming who produced it ("ai:provider/model").
        """
        with self._lock:
            row = self._conn.execute(select(ATTRS.c.id, ATTRS.c.value).where(
                ATTRS.c.organization_id == lead_id, ATTRS.c.key == key)).first()
            now = utcnow()
            if row is not None and row.value == value:
                self._conn.commit()
                return False
            if row is None:
                self._conn.execute(ATTRS.insert().values(
                    workspace_id=self.workspace_id, organization_id=lead_id, key=key[:50],
                    value=value, origin=origin[:20], estimated=bool(estimated),
                    producer=producer[:160], lead_type=self.lead_type, run_id=self.run_id,
                    created_at=now, updated_at=now))
            else:
                self._conn.execute(update(ATTRS).where(ATTRS.c.id == row.id).values(
                    value=value, origin=origin[:20], estimated=bool(estimated),
                    producer=producer[:160], run_id=self.run_id, updated_at=now))
            self._evidence(lead_id, f"{origin}:{producer}"[:100] if producer else origin, None,
                           [f"attribute:{key}"])
            self._note_run(lead_id, "updated")
            self._conn.commit()
            return True

    def attributes(self, lead_id: int) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(select(ATTRS.c.key, ATTRS.c.value).where(
                ATTRS.c.organization_id == lead_id)).all()
            self._conn.commit()
        return {row.key: row.value for row in rows}

    def attribute_times(self, key: str) -> dict[int, datetime]:
        """When each of this project's organisations last had attribute `key` written."""
        with self._lock:
            rows = self._conn.execute(
                select(ATTRS.c.organization_id, ATTRS.c.updated_at)
                .join(ORGS, ORGS.c.id == ATTRS.c.organization_id)
                .where(ATTRS.c.key == key, self._org_filter())).all()
            self._conn.commit()
        out = {}
        for org_id, when in rows:
            if when is not None and when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            out[int(org_id)] = when
        return out

    # --- signals and tags (Phase 6: lead types that assess organisations) ---------------

    def stamp(self, lead_id: int, *, source: str = "", url: str | None = None,
              fields: Iterable[str] = ()) -> bool:
        """Tag an organisation with this thread's lead type without touching its record.

        A lead type that assesses an organisation another one found (an audit,
        a job posting on its careers page) records that it did: the lead-type
        tag, the run's membership ("updated", unless the run created it) and,
        with ``source``, an evidence row. The record, its score and its
        contacts stay exactly as they were. False if there is no such organisation.
        """
        with self._lock:
            row = self._conn.execute(select(ORGS.c.lead_types).where(
                ORGS.c.id == lead_id, self._org_filter())).first()
            if row is None:
                self._conn.commit()
                return False
            lead_types = list(row.lead_types or [])
            if self.lead_type not in lead_types:
                self._conn.execute(update(ORGS).where(ORGS.c.id == lead_id).values(
                    lead_types=[*lead_types, self.lead_type]))
            if source:
                self._evidence(lead_id, source, url, list(fields))
            self._note_run(lead_id, "updated")
            self._conn.commit()
            return True

    def add_signal(self, lead_id: int, type_: str, *, key: str = "", title: str = "",
                   payload: dict[str, Any] | None = None, strength: float = 0.5,
                   published_at: datetime | None = None, expires_at: datetime | None = None,
                   source_url: str | None = None, source: str = "") -> bool:
        """Record a signal of this thread's lead type; True when it is new.

        A signal is identified by (organisation, lead type, type, ``key``): seen
        again, its row is refreshed (title, payload, strength, dates, when it was
        observed, the run) instead of a second one being added. The organisation
        is stamped with the lead type (``stamp``); its record is not touched.
        """
        now = utcnow()
        values = {"title": (title or "")[:500], "payload": dict(payload or {}),
                  "strength": max(0.0, min(1.0, float(strength))),
                  "published_at": published_at, "expires_at": expires_at,
                  "source_url": source_url[:2000] if source_url else None,
                  "observed_at": now, "run_id": self.run_id}
        with self._lock:
            if not self.stamp(lead_id, source=source or self.lead_type, url=source_url,
                              fields=[f"signal:{type_}"]):
                return False
            row = self._conn.execute(select(SIGNALS.c.id).where(
                SIGNALS.c.organization_id == lead_id, SIGNALS.c.lead_type == self.lead_type,
                SIGNALS.c.type == type_, SIGNALS.c.key == key[:300])
                .order_by(SIGNALS.c.id)).first()
            if row is None:
                self._conn.execute(SIGNALS.insert().values(
                    workspace_id=self.workspace_id, organization_id=lead_id, type=type_[:50],
                    lead_type=self.lead_type, key=key[:300], **values))
            else:
                self._conn.execute(update(SIGNALS).where(SIGNALS.c.id == row.id).values(**values))
            self._conn.commit()
            return row is None

    def prune_signals(self, lead_id: int, keep: Iterable[tuple[str, str]],
                      types: Iterable[str] | None = None) -> int:
        """Delete this lead type's signals on an organisation except the (type, key)s in ``keep``.

        For assessments that are a snapshot (an audit): a finding that no
        longer holds goes. ``types`` limits it to those signal types.
        """
        keep = {(t, k) for t, k in keep}
        with self._lock:
            query = select(SIGNALS.c.id, SIGNALS.c.type, SIGNALS.c.key).where(
                SIGNALS.c.organization_id == lead_id, SIGNALS.c.lead_type == self.lead_type)
            if types is not None:
                query = query.where(SIGNALS.c.type.in_(list(types)))
            gone = [r.id for r in self._conn.execute(query).all() if (r.type, r.key) not in keep]
            if gone:
                self._conn.execute(delete(SIGNALS).where(SIGNALS.c.id.in_(gone)))
            self._conn.commit()
        return len(gone)

    def signals(self, lead_id: int, lead_type: str | None = None) -> list[dict[str, Any]]:
        """An organisation's signals (this lead type's, or ``lead_type``'s), oldest first."""
        with self._lock:
            rows = self._conn.execute(select(SIGNALS).where(
                SIGNALS.c.organization_id == lead_id,
                SIGNALS.c.lead_type == (lead_type or self.lead_type))
                .order_by(SIGNALS.c.id)).mappings().all()
            self._conn.commit()
        return [dict(r) for r in rows]

    def tagged(self, lead_ids: Iterable[int] | None = None, *,
               run_id: int | None = None) -> list[tuple[int, Lead, list[str]]]:
        """(id, record, lead types) of this project's organisations, in id order.

        ``run_id``: only the run's own organisations (its membership rows);
        ``lead_ids``: only these.
        """
        query = select(ORGS.c.id, ORGS.c.record, ORGS.c.lead_types).where(self._org_filter())
        if run_id is not None:
            query = query.where(ORGS.c.id.in_(select(RUN_ORGS.c.organization_id).where(
                RUN_ORGS.c.run_id == run_id)))
        wanted = None if lead_ids is None else {int(i) for i in lead_ids}
        with self._lock:
            rows = self._conn.execute(query.order_by(ORGS.c.id)).all()
            self._conn.commit()
        return [(int(r.id), self._lead(r.record), list(r.lead_types or []))
                for r in rows if wanted is None or int(r.id) in wanted]

    # --- url memo (resume support) ---------------------------------------------------

    def mark_seen(self, url: str, source: str) -> None:
        if not url:
            return
        with self._lock:
            self._conn.execute(delete(SEEN).where(SEEN.c.project_id == self.project_id,
                                                  SEEN.c.url == url))
            self._conn.execute(SEEN.insert().values(
                project_id=self.project_id, url=url, workspace_id=self.workspace_id,
                source=source, fetched_at=time.time()))
            self._conn.commit()

    def already_seen(self, url: str) -> bool:
        if not url:
            return False
        with self._lock:
            row = self._conn.execute(select(SEEN.c.url).where(
                SEEN.c.project_id == self.project_id, SEEN.c.url == url)).first()
        return row is not None

    # --- dead domains (shared by the whole workspace) ----------------------------------

    def mark_dead_domain(self, host: str, reason: str) -> None:
        """Remember a host that is gone or permanently walled off.

        Idempotent: a repeat only bumps `hits`. The first diagnosis is kept
        because it is the one that came with a live response to look at.
        """
        if not host:
            return
        with self._lock:
            done = self._conn.execute(update(DEAD).where(
                DEAD.c.workspace_id == self.workspace_id, DEAD.c.host == host)
                .values(hits=DEAD.c.hits + 1)).rowcount
            if not done:
                self._conn.execute(DEAD.insert().values(
                    workspace_id=self.workspace_id, host=host, reason=reason,
                    first_seen=time.time(), hits=1))
            self._conn.commit()

    def is_dead_domain(self, host: str) -> bool:
        if not host:
            return False
        with self._lock:
            row = self._conn.execute(select(DEAD.c.host).where(
                DEAD.c.workspace_id == self.workspace_id, DEAD.c.host == host)).first()
        return row is not None

    def dead_domains(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(select(DEAD.c.host, DEAD.c.reason).where(
                DEAD.c.workspace_id == self.workspace_id)).all()
        return {row.host: row.reason for row in rows}

    # --- unit stats (scheduling feedback) ----------------------------------------------

    def record_unit(self, source: str, area: str, category: str,
                    seconds: float, new_leads: int) -> None:
        """Log what one (source, area, category) unit cost and returned."""
        key = and_(UNITS.c.workspace_id == self.workspace_id, UNITS.c.source == source,
                   UNITS.c.area == area, UNITS.c.category == category)
        with self._lock:
            seconds = float(seconds)
            done = self._conn.execute(update(UNITS).where(key).values(
                runs=UNITS.c.runs + 1, total_seconds=UNITS.c.total_seconds + seconds,
                total_sq_seconds=UNITS.c.total_sq_seconds + seconds * seconds,
                total_new_leads=UNITS.c.total_new_leads + int(new_leads),
                last_run=time.time())).rowcount
            if not done:
                self._conn.execute(UNITS.insert().values(
                    workspace_id=self.workspace_id, source=source, area=area, category=category,
                    runs=1, total_seconds=seconds, total_sq_seconds=seconds * seconds,
                    total_new_leads=int(new_leads), last_run=time.time()))
            self._conn.commit()

    def unit_stat(self, source: str, area: str, category: str) -> tuple[float, float] | None:
        """(avg_seconds, avg_new_leads) for one unit, None when never run."""
        with self._lock:
            row = self._conn.execute(select(UNITS.c.runs, UNITS.c.total_seconds,
                                            UNITS.c.total_new_leads).where(
                UNITS.c.workspace_id == self.workspace_id, UNITS.c.source == source,
                UNITS.c.area == area, UNITS.c.category == category)).first()
        return self._averages(row)

    def unit_stats_for_source(self, source: str) -> tuple[float, float] | None:
        """Same averages rolled up over every area/category.

        The fallback for a unit with no history of its own: a brand-new
        locality still scrapes roughly like its source's other localities.
        """
        with self._lock:
            row = self._conn.execute(select(
                func.sum(UNITS.c.runs).label("runs"),
                func.sum(UNITS.c.total_seconds).label("total_seconds"),
                func.sum(UNITS.c.total_new_leads).label("total_new_leads"),
            ).where(UNITS.c.workspace_id == self.workspace_id, UNITS.c.source == source)).first()
        return self._averages(row)

    @staticmethod
    def _averages(row) -> tuple[float, float] | None:
        # SUM() over no rows yields a row of NULLs, hence the falsy check.
        if row is None or not row.runs:
            return None
        runs = float(row.runs)
        return row.total_seconds / runs, row.total_new_leads / runs

    # --- enrichment memo -----------------------------------------------------------------

    def mark_enriched(self, lead_id: int, enricher: str) -> None:
        with self._lock:
            self._conn.execute(delete(MARKS).where(MARKS.c.organization_id == int(lead_id),
                                                   MARKS.c.enricher == enricher))
            self._conn.execute(MARKS.insert().values(
                organization_id=int(lead_id), enricher=enricher,
                workspace_id=self.workspace_id, done_at=time.time()))
            self._conn.commit()

    def enriched_ids(self, enricher: str) -> set[int]:
        """Organisations of this project `enricher` has already been through."""
        with self._lock:
            rows = self._conn.execute(
                select(MARKS.c.organization_id)
                .join(ORGS, ORGS.c.id == MARKS.c.organization_id)
                .where(MARKS.c.enricher == enricher, self._org_filter())).all()
        return {int(r[0]) for r in rows}

    def enriched_times(self, enricher: str) -> dict[int, datetime]:
        """When `enricher` last went through each of this project's organisations."""
        with self._lock:
            rows = self._conn.execute(
                select(MARKS.c.organization_id, MARKS.c.done_at)
                .join(ORGS, ORGS.c.id == MARKS.c.organization_id)
                .where(MARKS.c.enricher == enricher, self._org_filter())).all()
        return {int(r[0]): _epoch_to_dt(r[1]) for r in rows if r[1] is not None}

    def is_enriched(self, lead_id: int, enricher: str) -> bool:
        with self._lock:
            row = self._conn.execute(select(MARKS.c.enricher).where(
                MARKS.c.organization_id == int(lead_id), MARKS.c.enricher == enricher)).first()
        return row is not None

    # --- read ------------------------------------------------------------------------------

    def all_leads(self) -> list[Lead]:
        return [lead for _, lead in self.all_with_ids()]

    def all_with_ids(self) -> list[tuple[int, Lead]]:
        """(id, Lead) pairs in id order -- enrichment needs the id to merge in place."""
        with self._lock:
            rows = self._conn.execute(select(ORGS.c.id, ORGS.c.record)
                                      .where(self._org_filter()).order_by(ORGS.c.id)).all()
        return [(int(r.id), self._lead(r.record)) for r in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute(select(func.count()).select_from(ORGS)
                                          .where(self._org_filter())).scalar_one())

    def rescore_all(self) -> None:
        """Recompute every score (weights may have changed between runs)."""
        with self._lock:
            for lead_id, lead in self.all_with_ids():
                scores = self._score(lead)
                self._conn.execute(update(ORGS).where(ORGS.c.id == lead_id).values(
                    record=asdict(lead), latitude=lead.latitude, longitude=lead.longitude,
                    **scores))
            self._conn.commit()

    def sorted_leads(self) -> list[Lead]:
        """One running table, in the profile's order (see :func:`sort_leads`)."""
        leads = self.all_leads()
        for lead in leads:
            self.scorer.apply(lead)
        return sort_leads(leads, self.options.sort)

    def website_enrichment_targets(self) -> list[tuple[int, Lead]]:
        """Leads with a website whose email we still don't have."""
        return [(lead_id, lead) for lead_id, lead in self.all_with_ids()
                if lead.website and not lead.emails]

    def instagram_enrichment_targets(self) -> list[tuple[int, Lead]]:
        """Leads with a known handle but missing an email or phone."""
        return [(lead_id, lead) for lead_id, lead in self.all_with_ids()
                if lead.instagram and not (lead.emails and lead.phones)]

    def clear(self) -> int:
        """Delete every organisation of this project (and what hangs off them).

        Only the preview uses it: its scratch project starts empty each time.
        Run history stays.
        """
        with self._lock:
            count = self.count()
            self._conn.execute(delete(ORGS).where(self._org_filter()))
            self._conn.execute(delete(SEEN).where(SEEN.c.project_id == self.project_id))
            self._conn.commit()
        return count

    def drop_project_if_unused(self) -> bool:
        """Remove the project this store created, if nothing was written to it.

        A dry run (an estimate) opens the project to read learned costs; it
        must not leave an empty project behind.
        """
        if not self.project_created:
            return False
        from .models import Run

        with self._lock:
            runs = self._conn.execute(select(func.count()).select_from(Run.__table__).where(
                Run.__table__.c.project_id == self.project_id)).scalar_one()
            if runs or self.count():
                return False
            self._conn.execute(delete(Project.__table__).where(
                Project.__table__.c.id == self.project_id))
            self._conn.commit()
        return True

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            finally:
                if self._owns_engine:
                    self.engine.dispose()


def sort_leads(leads: list[Lead], mode: str = "vendors") -> list[Lead]:
    """The output order every export shares.

    Vendor run: grouped by locality then category, score desc.
    Venue run (``mode="venues"``): tier (Luxury first), then score desc -- the
    best venues in the whole region surface first, and Locality stays a
    filter column.
    """
    if mode == "venues":
        from ..packs.hospitality.venues import TIER_RANK

        return sorted(
            leads,
            key=lambda lead: (
                TIER_RANK.get(lead.venue_tier, len(TIER_RANK)),
                -lead.score,
                lead.business_name.lower(),
            ),
        )
    return sorted(
        leads,
        key=lambda lead: (
            lead.locality or "zz-unknown",
            lead.category or "zz-unknown",
            -lead.score,
            lead.business_name.lower(),
        ),
    )


def store_target(config) -> tuple[str | Path, dict[str, Any]]:
    """Where a run's records live: (database, LeadStore keyword arguments).

    A run config names a project, which lives in the application database
    (``runtime.db_url``, else ``LEADSCRAPER_DB_URL``, else the per-user default).
    A bare config without one -- tests, embedding code -- keeps using its
    ``runtime.db_path`` file as a one-project database.
    """
    from ..paths import default_db_url

    project = config.get("project")
    if project:
        return (config.get("runtime.db_url") or default_db_url(),
                {"project": str(project), "country": config.country,
                 "project_description": str(config.get("description") or "")})
    return config.path_for("db_path", "data/leads.sqlite3"), {}
