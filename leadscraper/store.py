"""SQLite-backed lead store with dedup-on-write.

Dedup strategy, in priority order:
  1. Shared phone number      -> definitely the same business (strongest signal)
  2. Same Google Maps place   -> same business, when phones don't contradict
  3. Identical canonical name -> same business, when phones don't contradict
  4. Fuzzy name >= threshold within the same locality -> same business

Rules 1 and 2 can merge across localities (a caterer listed in both Andheri
and Dadar is one business; Maps returns the same place to searches run from
neighbouring suburbs). Rules 3 and 4 are deliberately more conservative
because "Royal Banquets" in two suburbs may well be two companies.

A venue run tightens and extends this through `DedupOptions`:
  * a shared phone only merges when the names are also similar -- one
    StayVista reservations number fronts dozens of unrelated villas;
  * two records whose coordinates sit within a few hundred metres merge when
    their names are loosely similar, which catches "Mokshah" (StayVista) vs
    "Mokshah Villa Igatpuri" (Maps) even when their localities disagree.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from rapidfuzz import fuzz

from .errors import is_error_title
from .models import Lead
from .normalize import maps_place_key, normalize_name
from .scoring import Scorer
from .venues import TIER_RANK, haversine_km

log = logging.getLogger(__name__)

UpsertAction = Literal["new", "merged", "unchanged"]

FUZZY_NAME_THRESHOLD = 92


@dataclass
class DedupOptions:
    """Per-run dedup knobs. The defaults reproduce the vendor run exactly."""

    #: When set, a phone match also needs this name similarity (0-100).
    phone_match_min_similarity: int | None = None
    #: When set, records within this many metres are merge candidates...
    geo_match_radius_m: float | None = None
    #: ...provided their names reach this similarity.
    geo_match_min_similarity: int = 70
    #: "vendors": locality -> category -> score. "venues": tier -> score.
    sort: str = "vendors"
    #: Let a distinctive identical name merge across localities (venue runs:
    #: Airbnb files a villa under "Nashik", Booking under "Igatpuri").
    cross_locality_names: bool = False

    @classmethod
    def from_config(cls, config) -> "DedupOptions":
        if config is None:
            return cls()
        get = config.get
        venue_run = str(get("profile", "vendors") or "vendors").lower() == "venues"
        phone_sim = get("dedup.phone_match_min_similarity", 60 if venue_run else None)
        geo_radius = get("dedup.geo_match_radius_m", 250 if venue_run else None)
        return cls(
            phone_match_min_similarity=int(phone_sim) if phone_sim is not None else None,
            geo_match_radius_m=float(geo_radius) if geo_radius else None,
            geo_match_min_similarity=int(get("dedup.geo_match_min_similarity", 70)),
            sort="venues" if venue_run else "vendors",
            cross_locality_names=bool(get("dedup.cross_locality_names", venue_run)),
        )

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name_key     TEXT NOT NULL,
    locality     TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT '',
    score        REAL NOT NULL DEFAULT 0,
    data         TEXT NOT NULL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_name   ON leads(name_key);
CREATE INDEX IF NOT EXISTS idx_leads_group  ON leads(locality, category, score DESC);

CREATE TABLE IF NOT EXISTS phone_index (
    phone   TEXT PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS place_index (
    place   TEXT PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS seen_urls (
    url        TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dead_domains (
    host       TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,
    first_seen REAL NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS unit_stats (
    source          TEXT NOT NULL,
    area            TEXT NOT NULL,
    category        TEXT NOT NULL,
    runs            INTEGER NOT NULL DEFAULT 0,
    total_seconds   REAL NOT NULL DEFAULT 0,
    total_new_leads INTEGER NOT NULL DEFAULT 0,
    last_run        REAL NOT NULL,
    PRIMARY KEY (source, area, category)
);

CREATE TABLE IF NOT EXISTS enriched (
    lead_id  INTEGER NOT NULL,
    enricher TEXT NOT NULL,
    done_at  REAL NOT NULL,
    PRIMARY KEY (lead_id, enricher)
);
"""


class LeadStore:
    """Thread-safe store. All writes go through `upsert`."""

    def __init__(self, db_path: str | Path, scorer: Scorer | None = None,
                 options: DedupOptions | None = None):
        self.options = options or DedupOptions()
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.scorer = scorer or Scorer()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._backfill_place_index()
        self._conn.commit()
        self.stats = {"new": 0, "merged": 0, "unchanged": 0}

    def _backfill_place_index(self) -> None:
        """Index Maps places for a store written before the index existed.

        Runs once: an empty index over a non-empty store is the only state
        this touches. Lowest id wins a contested place, as on a live write.
        """
        if self._conn.execute("SELECT 1 FROM place_index LIMIT 1").fetchone():
            return
        for row in self._conn.execute("SELECT id, data FROM leads ORDER BY id").fetchall():
            self._index_place(row["id"], Lead.from_json(row["data"]).gmaps_url)

    # --- lookup -----------------------------------------------------------
    #
    # Every helper below takes the lock itself rather than trusting its caller
    # to hold it: the lock is an RLock, so the nested acquire inside
    # `find_match`/`upsert` is free, and no helper is left unsafe when called
    # on its own from a worker thread.

    def _find_by_phone(self, phones: Iterable[str], lead: Lead | None = None) -> int | None:
        phones = [p for p in phones if p]
        if not phones:
            return None
        if self.options.phone_match_min_similarity is not None and lead is not None:
            return self._find_by_phone_and_name(phones, lead)
        placeholders = ",".join("?" * len(phones))
        with self._lock:
            row = self._conn.execute(
                f"SELECT lead_id FROM phone_index WHERE phone IN ({placeholders}) LIMIT 1",
                phones,
            ).fetchone()
        return row["lead_id"] if row else None

    def _find_by_phone_and_name(self, phones: list[str], lead: Lead) -> int | None:
        """Phone match that also demands a similar name.

        The phone index keeps only the FIRST lead to claim a number, which is
        exactly wrong for a shared reservations line, so candidates come from a
        scan of the stored records instead. Venue runs are hundreds of rows,
        not hundreds of thousands, so the scan is cheap.
        """
        threshold = self.options.phone_match_min_similarity or 0
        wanted = set(phones)
        with self._lock:
            rows = self._conn.execute("SELECT id, data FROM leads").fetchall()
        best_id, best_score = None, -1.0
        for row in rows:
            existing = Lead.from_json(row["data"])
            if not wanted & set(existing.phones):
                continue
            similarity = self._name_similarity(lead, existing)
            if similarity >= threshold and similarity > best_score:
                best_id, best_score = row["id"], similarity
        return best_id

    def _find_by_location(self, lead: Lead) -> int | None:
        """Records pinned within `geo_match_radius_m` whose names loosely agree."""
        radius = self.options.geo_match_radius_m
        if not radius or lead.latitude is None or lead.longitude is None:
            return None
        with self._lock:
            rows = self._conn.execute("SELECT id, data FROM leads").fetchall()
        best_id, best_score = None, -1.0
        for row in rows:
            existing = Lead.from_json(row["data"])
            if existing.latitude is None or existing.longitude is None:
                continue
            metres = 1000 * haversine_km(lead.latitude, lead.longitude,
                                         existing.latitude, existing.longitude)
            if metres > radius:
                continue
            similarity = self._name_similarity(lead, existing)
            if similarity >= self.options.geo_match_min_similarity and similarity > best_score:
                best_id, best_score = row["id"], similarity
        return best_id

    @staticmethod
    def _name_similarity(a: Lead, b: Lead) -> float:
        """0-100; containment counts as a full match ("Mokshah" in "Mokshah Villa")."""
        key_a, key_b = a.name_key, b.name_key
        if not key_a or not key_b:
            return 0.0
        if len(min(key_a, key_b, key=len)) >= 4 and (key_a in key_b or key_b in key_a):
            return 100.0
        return float(max(fuzz.token_sort_ratio(key_a, key_b), fuzz.token_set_ratio(key_a, key_b)))

    def _find_by_place(self, lead: Lead) -> int | None:
        """Same Google Maps place, guarded against contradicting phones."""
        place = maps_place_key(lead.gmaps_url)
        if not place:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT l.id, l.data FROM place_index p JOIN leads l ON l.id = p.lead_id"
                " WHERE p.place = ?", (place,)
            ).fetchone()
        if row is None or self._phones_contradict(lead, Lead.from_json(row["data"])):
            return None
        return row["id"]

    def _find_by_name(self, lead: Lead) -> int | None:
        """Exact canonical-name match, guarded against contradicting phones."""
        key = lead.name_key
        if not key or len(key) < 3:
            return None
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, data, locality FROM leads WHERE name_key = ?", (key,)
            ).fetchall()
        for row in rows:
            existing = Lead.from_json(row["data"])
            if self._phones_contradict(lead, existing):
                continue
            if self._locality_compatible(lead, existing) or self._distinctive_match(lead, existing):
                return row["id"]
        return None

    def _distinctive_match(self, a: Lead, b: Lead) -> bool:
        """Same distinctive name in different localities, pins (if any) close by."""
        if not self.options.cross_locality_names:
            return False
        key = a.name_key
        if len(key) < 10 or len(key.split()) < 2:
            return False
        if None not in (a.latitude, a.longitude, b.latitude, b.longitude):
            return haversine_km(a.latitude, a.longitude, b.latitude, b.longitude) <= 15
        return True

    def _find_by_fuzzy_name(self, lead: Lead) -> int | None:
        """Near-identical name within the same locality."""
        key = lead.name_key
        if not key or len(key) < 5 or not lead.locality:
            return None
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name_key, data FROM leads WHERE locality = ?", (lead.locality,)
            ).fetchall()
        best_id, best_score = None, 0.0
        for row in rows:
            ratio = fuzz.token_sort_ratio(key, row["name_key"])
            if ratio >= FUZZY_NAME_THRESHOLD and ratio > best_score:
                existing = Lead.from_json(row["data"])
                if self._phones_contradict(lead, existing):
                    continue
                best_id, best_score = row["id"], ratio
        return best_id

    @staticmethod
    def _phones_contradict(a: Lead, b: Lead) -> bool:
        """True when both have phones and none overlap -- i.e. different firms."""
        if not a.phones or not b.phones:
            return False
        return not (set(a.phones) & set(b.phones))

    @staticmethod
    def _locality_compatible(a: Lead, b: Lead) -> bool:
        """Same-name records merge only when locality agrees or is unknown."""
        if not a.locality or not b.locality:
            return True
        return normalize_name(a.locality) == normalize_name(b.locality)

    def find_match(self, lead: Lead) -> int | None:
        with self._lock:
            return (
                self._find_by_phone(lead.phones, lead)
                or self._find_by_place(lead)
                or self._find_by_name(lead)
                or self._find_by_fuzzy_name(lead)
                or self._find_by_location(lead)
            )

    # --- write ------------------------------------------------------------

    def upsert(self, lead: Lead) -> tuple[int, UpsertAction, list[str]]:
        """Insert or merge. Returns (lead_id, action, changed_fields)."""
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
                lead_id = self._insert(lead)
                self.stats["new"] += 1
                return lead_id, "new", ["*new*"]

            row = self._conn.execute(
                "SELECT data FROM leads WHERE id = ?", (match_id,)
            ).fetchone()
            existing = Lead.from_json(row["data"])
            # `merge` keeps the first gmaps_url, so a second place that turned
            # out to be this business is indexed here or it would split later.
            self._index_place(match_id, lead.gmaps_url)
            changed = existing.merge(lead)
            if not changed:
                self._conn.commit()
                self.stats["unchanged"] += 1
                return match_id, "unchanged", []
            self._update(match_id, existing)
            self.stats["merged"] += 1
            return match_id, "merged", changed

    def _insert(self, lead: Lead) -> int:
        self.scorer.apply(lead)
        now = time.time()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO leads (name_key, locality, category, score, data, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lead.name_key, lead.locality, lead.category, lead.score,
                 lead.to_json(), lead.first_seen or now, now),
            )
            lead_id = int(cursor.lastrowid)
            self._index_phones(lead_id, lead.phones)
            self._index_place(lead_id, lead.gmaps_url)
            self._conn.commit()
        return lead_id

    def _update(self, lead_id: int, lead: Lead) -> None:
        self.scorer.apply(lead)
        with self._lock:
            self._conn.execute(
                "UPDATE leads SET name_key=?, locality=?, category=?, score=?, data=?, updated_at=?"
                " WHERE id=?",
                (lead.name_key, lead.locality, lead.category, lead.score,
                 lead.to_json(), time.time(), lead_id),
            )
            self._index_phones(lead_id, lead.phones)
            self._index_place(lead_id, lead.gmaps_url)
            self._conn.commit()

    def _index_phones(self, lead_id: int, phones: Iterable[str]) -> None:
        with self._lock:
            for phone in phones:
                if not phone:
                    continue
                # INSERT OR IGNORE: the first lead to claim a phone keeps it, which
                # prevents two records ping-ponging the same number.
                self._conn.execute(
                    "INSERT OR IGNORE INTO phone_index (phone, lead_id) VALUES (?, ?)",
                    (phone, lead_id),
                )

    def _index_place(self, lead_id: int, gmaps_url: str | None) -> None:
        place = maps_place_key(gmaps_url)
        if not place:
            return
        with self._lock:
            # First claim keeps the place, exactly like the phone index.
            self._conn.execute(
                "INSERT OR IGNORE INTO place_index (place, lead_id) VALUES (?, ?)",
                (place, lead_id),
            )

    # --- url memo (resume support) ---------------------------------------

    def mark_seen(self, url: str, source: str) -> None:
        if not url:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO seen_urls (url, source, fetched_at) VALUES (?, ?, ?)",
                (url, source, time.time()),
            )
            self._conn.commit()

    def already_seen(self, url: str) -> bool:
        if not url:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen_urls WHERE url = ? LIMIT 1", (url,)
            ).fetchone()
        return row is not None

    # --- dead domains -----------------------------------------------------

    def mark_dead_domain(self, host: str, reason: str) -> None:
        """Remember a host that is gone or permanently walled off.

        Idempotent: a repeat only bumps `hits`. The first diagnosis is kept
        because it is the one that came with a live response to look at.
        """
        if not host:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO dead_domains (host, reason, first_seen, hits) VALUES (?, ?, ?, 1)"
                " ON CONFLICT(host) DO UPDATE SET hits = hits + 1",
                (host, reason, time.time()),
            )
            self._conn.commit()

    def is_dead_domain(self, host: str) -> bool:
        if not host:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM dead_domains WHERE host = ? LIMIT 1", (host,)
            ).fetchone()
        return row is not None

    def dead_domains(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT host, reason FROM dead_domains").fetchall()
        return {row["host"]: row["reason"] for row in rows}

    # --- unit stats (scheduling feedback) ---------------------------------

    def record_unit(self, source: str, area: str, category: str,
                    seconds: float, new_leads: int) -> None:
        """Log what one (source, area, category) unit cost and returned."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO unit_stats"
                " (source, area, category, runs, total_seconds, total_new_leads, last_run)"
                " VALUES (?, ?, ?, 1, ?, ?, ?)"
                " ON CONFLICT(source, area, category) DO UPDATE SET"
                " runs = runs + 1,"
                " total_seconds = total_seconds + excluded.total_seconds,"
                " total_new_leads = total_new_leads + excluded.total_new_leads,"
                " last_run = excluded.last_run",
                (source, area, category, float(seconds), int(new_leads), time.time()),
            )
            self._conn.commit()

    def unit_stat(self, source: str, area: str, category: str) -> tuple[float, float] | None:
        """(avg_seconds, avg_new_leads) for one unit, None when never run."""
        with self._lock:
            row = self._conn.execute(
                "SELECT runs, total_seconds, total_new_leads FROM unit_stats"
                " WHERE source = ? AND area = ? AND category = ?",
                (source, area, category),
            ).fetchone()
        return self._averages(row)

    def unit_stats_for_source(self, source: str) -> tuple[float, float] | None:
        """Same averages rolled up over every area/category.

        The fallback for a unit with no history of its own: a brand-new
        locality still scrapes roughly like its source's other localities.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT SUM(runs) AS runs, SUM(total_seconds) AS total_seconds,"
                " SUM(total_new_leads) AS total_new_leads FROM unit_stats WHERE source = ?",
                (source,),
            ).fetchone()
        return self._averages(row)

    @staticmethod
    def _averages(row: sqlite3.Row | None) -> tuple[float, float] | None:
        # SUM() over no rows yields a row of NULLs, hence the falsy check.
        if row is None or not row["runs"]:
            return None
        runs = float(row["runs"])
        return row["total_seconds"] / runs, row["total_new_leads"] / runs

    # --- enrichment memo --------------------------------------------------

    def mark_enriched(self, lead_id: int, enricher: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO enriched (lead_id, enricher, done_at) VALUES (?, ?, ?)",
                (int(lead_id), enricher, time.time()),
            )
            self._conn.commit()

    def is_enriched(self, lead_id: int, enricher: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM enriched WHERE lead_id = ? AND enricher = ? LIMIT 1",
                (int(lead_id), enricher),
            ).fetchone()
        return row is not None

    # --- read -------------------------------------------------------------

    def get(self, lead_id: int) -> Lead | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM leads WHERE id = ?", (lead_id,)
            ).fetchone()
        return Lead.from_json(row["data"]) if row else None

    def all_leads(self) -> list[Lead]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM leads").fetchall()
        return [Lead.from_json(row["data"]) for row in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"])

    def rescore_all(self) -> None:
        """Recompute every score (weights may have changed between runs)."""
        with self._lock:
            rows = self._conn.execute("SELECT id, data FROM leads").fetchall()
            for row in rows:
                lead = Lead.from_json(row["data"])
                self.scorer.apply(lead)
                self._conn.execute(
                    "UPDATE leads SET score=?, data=? WHERE id=?",
                    (lead.score, lead.to_json(), row["id"]),
                )
            self._conn.commit()

    def sorted_leads(self) -> list[Lead]:
        """One running table.

        Vendor run: grouped by locality then category, score desc.
        Venue run: tier (Luxury first), then score desc -- the best venues in
        the whole region surface first, and Locality stays a filter column.
        """
        leads = self.all_leads()
        for lead in leads:
            self.scorer.apply(lead)
        if self.options.sort == "venues":
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

    def all_with_ids(self) -> list[tuple[int, Lead]]:
        """(id, Lead) pairs -- enrichment needs the id to merge in place."""
        with self._lock:
            rows = self._conn.execute("SELECT id, data FROM leads ORDER BY id").fetchall()
        return [(row["id"], Lead.from_json(row["data"])) for row in rows]

    def merge_into(self, lead_id: int, partial: Lead) -> list[str]:
        """Merge a partial Lead into a KNOWN lead id.

        Enrichment must not go through `upsert`: an enricher often discovers a
        phone number the stored record doesn't have yet, and `upsert`'s
        "different phones means different business" guard would then split the
        lead in two. Here we already know they're the same record.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM leads WHERE id = ?", (lead_id,)
            ).fetchone()
            if row is None:
                return []
            existing = Lead.from_json(row["data"])
            partial = partial.normalized()
            self._index_place(lead_id, partial.gmaps_url)
            changed = existing.merge(partial)
            if changed:
                self._update(lead_id, existing)
                self.stats["merged"] += 1
            else:
                self._conn.commit()
            return changed

    def update_fields(self, lead_id: int, **fields) -> list[str]:
        """Overwrite specific fields on a known lead.

        Used by the AI polish pass, which legitimately *replaces* a bad
        SERP-derived name rather than merging alongside it -- `merge` is the
        wrong tool there because it deliberately protects the existing name.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM leads WHERE id = ?", (lead_id,)
            ).fetchone()
            if row is None:
                return []
            lead = Lead.from_json(row["data"])
            changed = []
            for name, value in fields.items():
                if not hasattr(lead, name):
                    continue
                if getattr(lead, name) != value:
                    setattr(lead, name, value)
                    changed.append(name)
            if changed:
                self._update(lead_id, lead)
            return changed

    def website_enrichment_targets(self) -> list[tuple[int, Lead]]:
        """Leads with a website whose email we still don't have."""
        return [(lead_id, lead) for lead_id, lead in self.all_with_ids()
                if lead.website and not lead.emails]

    def instagram_enrichment_targets(self) -> list[tuple[int, Lead]]:
        """Leads with a known handle but missing an email or phone."""
        return [(lead_id, lead) for lead_id, lead in self.all_with_ids()
                if lead.instagram and not (lead.emails and lead.phones)]

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()
