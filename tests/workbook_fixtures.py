"""Fixture runs for the workbook (Phase 4): fictional data only, never real leads.

Four scenarios, each a finished run in a fresh application database:

* ``local_business`` -- dentists and caterers in two places (Pune, Mumbai);
* ``mix`` -- three lead types in Austin: ``local_business`` plus two fixture
  lead types defined here (``demo_hiring``, ``demo_funding``) with signals,
  decision-makers and one inferred email;
* ``venues`` -- the hospitality vertical (the Nashik preset's settings);
* ``empty`` -- a run that found nothing, with failures to explain why.

Names are invented, websites use the reserved ``.example`` domain, phone
numbers sit in unallocated (India: 0000 exchanges) or fictional (US 555-01xx)
ranges. Every timestamp derives from ``FIXED_NOW``, so the same scenario
always builds the same workbook. ``tests/test_workbook.py`` pins the
structure; ``tools/build_sample_workbooks.py`` writes ``docs/samples/``.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from sqlalchemy import update

from leadscraper.core.lead import Lead
from leadscraper.core.scoring import ScoreParts
from leadscraper.db.models import AIUsage, Contact, Person, Run, Signal, WorkUnitRow
from leadscraper.db.runs import finish_run, record_failure, start_run
from leadscraper.leadtypes.base import ColumnSpec, LeadType, LeadTypeResult, SignalSpec

UTC = timezone.utc
FIXED_NOW = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
SCENARIOS = ("local_business", "mix", "venues", "empty")
ROOT = Path(__file__).resolve().parent.parent


def ago(days: float) -> datetime:
    return FIXED_NOW - timedelta(days=days)


def epoch(days: float) -> float:
    return ago(days).timestamp()


# --- two fixture lead types (the three-lead-type mix) ----------------------------------------

class DemoConfig(BaseModel):
    enabled: bool = True


class _DemoLeadType(LeadType):
    config_model = DemoConfig
    applicable_countries = None

    def sources(self, cfg: Any, geo: Any = None) -> dict[str, list[str]]:
        return {"discovery": [], "enrichment": []}

    def plan(self, cfg: Any, geo: Any = None, depth: str = "balanced") -> list:
        return []

    def run(self, context: Any) -> LeadTypeResult:
        return LeadTypeResult(lead_type=self.key)

    def _mine(self, org: Any) -> list[dict[str, Any]]:
        return [s for s in getattr(org, "signals", []) if s.get("lead_type") == self.key]


class DemoHiring(_DemoLeadType):
    key = "demo_hiring"
    title = "Hiring (fixture)"
    description = "Fixture lead type: companies posting tech jobs."

    def signal_specs(self) -> list[SignalSpec]:
        return [SignalSpec("job_posting", "Job posting", 0.6, half_life_days=45)]

    def columns(self, cfg: Any) -> list[ColumnSpec]:
        return [
            ColumnSpec("open_roles", "Open Tech Roles", "number", number_format="0", width=10,
                       description="Tech job posts found for the organisation (fixture)."),
            ColumnSpec("roles", "Roles Hiring", placement="tail", width=40),
        ]

    def column_values(self, org: Any, cfg: Any) -> dict[str, Any]:
        mine = self._mine(org)
        return {"open_roles": len(mine) or None,
                "roles": ", ".join(s["payload"].get("role", "") for s in mine) or None}

    def score(self, org: Any, signals: list, cfg: Any) -> ScoreParts:
        if not signals:
            return ScoreParts(self.key)
        latest = max(s["published_at"] for s in signals)
        plural = "s" if len(signals) > 1 else ""
        return ScoreParts(self.key, intent=min(1.0, 0.35 + 0.15 * len(signals)),
                          reasons=[f"hiring {len(signals)} tech role{plural} "
                                   f"(latest {latest:%d %b %Y})"])


class DemoFunding(_DemoLeadType):
    key = "demo_funding"
    title = "Funding news (fixture)"
    description = "Fixture lead type: funding rounds in the news."

    def signal_specs(self) -> list[SignalSpec]:
        return [SignalSpec("funding_round", "Funding round", 0.8, half_life_days=180)]

    def columns(self, cfg: Any) -> list[ColumnSpec]:
        return [
            ColumnSpec("last_round", "Last Round", width=12, breakdown=True),
            ColumnSpec("amount", "Amount Raised", "money", number_format='"$"#,##0', width=14),
            ColumnSpec("announced", "Announced", "date", number_format="dd-mmm-yy", width=11),
        ]

    def column_values(self, org: Any, cfg: Any) -> dict[str, Any]:
        mine = sorted(self._mine(org), key=lambda s: s["published_at"], reverse=True)
        if not mine:
            return {}
        top = mine[0]
        return {"last_round": top["payload"].get("round"), "amount": top["payload"].get("amount"),
                "announced": top["published_at"]}

    def score(self, org: Any, signals: list, cfg: Any) -> ScoreParts:
        if not signals:
            return ScoreParts(self.key)
        top = max(signals, key=lambda s: s["published_at"])
        amount = top["payload"].get("amount")
        money = f" (${amount / 1e6:.0f}M)" if amount else ""
        return ScoreParts(self.key, intent=0.8,
                          reasons=[f"raised {top['payload'].get('round')}{money} on "
                                   f"{top['published_at']:%d %b %Y}"])


LEAD_TYPES = [DemoHiring(), DemoFunding()]


def register_demo_lead_types() -> None:
    from leadscraper.leadtypes import registry

    for lead_type in LEAD_TYPES:
        registry.register(lead_type)


def unregister_demo_lead_types() -> None:
    from leadscraper.leadtypes import registry

    for lead_type in LEAD_TYPES:
        registry.unregister(lead_type.key)


# --- configs ----------------------------------------------------------------------------------

def _doc(name: str, country: str, places: list[str], lead_types: dict[str, Any], *,
         center: list[float], admin_area: str, **extra: Any) -> dict[str, Any]:
    return {"schema_version": 1, "name": name, "project": name,
            "geography": {"country": country, "places": places, "center": center,
                          "admin_area": admin_area},
            "lead_types": lead_types, **extra}


def venues_document() -> dict[str, Any]:
    """The Nashik preset's venue settings, as a fixture project (no paths, no Sheet)."""
    doc = copy.deepcopy(yaml.safe_load((ROOT / "presets" / "nashik-venues.yaml").read_text()))
    doc.update(name="Sample venues", project="Sample venues",
               description="Fixture: fictional venues in the Nashik region.")
    doc.pop("runtime", None)
    doc["output"] = {"excel": True, "csv": False, "json": False, "google_sheets": False}
    return doc


# --- writing a run ----------------------------------------------------------------------------

class _Run:
    def __init__(self, engine, document: dict[str, Any], country: str):
        from leadscraper.db.store import LeadStore
        from leadscraper.engine.runner import config_from_snapshot
        from leadscraper.leadtypes.local_business.scoring import Scorer
        from leadscraper.services.workspace import default_workspace

        self.engine = engine
        self.ws = default_workspace(engine)
        config = config_from_snapshot(document)
        config.apply_name_noise()
        self.store = LeadStore(engine, Scorer(config), project=document["project"],
                               country=country)
        self.run_id = start_run(engine, self.ws.id, self.store.project_id,
                                config_snapshot=document)
        self.store.run_id = self.run_id
        self.ids: dict[str, int] = {}

    def add(self, lead: Lead, lead_type: str = "local_business", days: float = 30) -> int:
        lead.first_seen = lead.last_updated = epoch(days)
        self.store.lead_type = lead_type
        org_id, _, _ = self.store.upsert(lead, now=epoch(1))
        self.ids[lead.business_name] = org_id
        return org_id

    def insert(self, table, **values) -> None:
        with self.engine.begin() as conn:
            conn.execute(table.__table__.insert().values(workspace_id=self.ws.id, **values))

    def signal(self, org: str, lead_type: str, type_: str, title: str, days: float,
               url: str, strength: float, **payload) -> None:
        self.insert(Signal, organization_id=self.ids[org], run_id=self.run_id, type=type_,
                    lead_type=lead_type, title=title, payload=payload, observed_at=ago(1),
                    published_at=ago(days), strength=strength, source_url=url)

    def unit(self, lead_type: str, source: str, area: str | None, category: str | None,
             status: str = "done", found: int = 0, new: int = 0, skip_reason: str = "") -> None:
        key = "|".join([lead_type, source, area or "*", category or "*"])
        self.insert(WorkUnitRow, run_id=self.run_id, lead_type=lead_type, source=source,
                    area=area, category=category, status=status, found=found, new=new,
                    unit_key=key, seconds=42.0, skip_reason=skip_reason)

    def failure(self, source: str, reason: str, times: int = 1) -> None:
        for i in range(times):
            record_failure(self.engine, self.ws.id,
                           {"source": source, "reason": reason, "url": None,
                            "detail": f"fixture failure {i + 1}"},
                           project_id=self.store.project_id, run_id=self.run_id)

    def finish(self, status: str = "completed", minutes: float = 47, error: str = "") -> int:
        new = self.store.stats["new"]
        self.store.close()
        finish_run(self.engine, self.run_id, status=status, error=error,
                   stats={"engine": {"new": new}})
        with self.engine.begin() as conn:
            conn.execute(update(Run.__table__).where(Run.__table__.c.id == self.run_id).values(
                started_at=ago(1), finished_at=ago(1) + timedelta(minutes=minutes),
                created_at=ago(1), active_seconds=minutes * 60))
        return self.run_id


# --- the scenarios ----------------------------------------------------------------------------

def _maps(n: int) -> str:
    return f"https://maps.google.com/?cid={1000000000000000000 + n}"


def local_business(engine) -> int:
    document = _doc("Sample dentists and caterers", "IN", ["Pune", "Mumbai"],
                    {"local_business": {"categories": [{"name": "Dentists"},
                                                       {"name": "Caterers"}]}},
                    center=[18.52, 73.85], admin_area="Maharashtra")
    run = _Run(engine, document, "IN")
    rows = [
        ("Bright Smile Dental Studio", "Dentists", "Pune", ["+912000000101"],
         ["hello@brightsmile.example"], "https://brightsmile.example", 4.8, 312,
         "brightsmile.sample", ["google_maps", "justdial", "website"]),
        ("Lotus Family Dentistry", "Dentists", "Pune", ["+912000000102", "+912000000112"],
         ["care@lotusdental.example", "frontdesk@lotusdental.example"],
         "https://lotusdental.example", 4.6, 128, None, ["google_maps", "website"]),
        ("Riverside Dental Care", "Dentists", "Pune", ["+912000000103"], [], None, 4.2, 37,
         None, ["google_maps"]),
        ("Pearl Orthodontics", "Dentists", "Mumbai", ["+912200000104"],
         ["smile@pearlortho.example"], "https://pearlortho.example", 4.9, 540,
         "pearlortho.sample", ["google_maps", "justdial", "website"]),
        ("Harbour Dental Clinic", "Dentists", "Mumbai", [], [], "https://harbourdental.example",
         3.9, 18, None, ["google_maps"]),
        ("Saffron Table Caterers", "Caterers", "Pune", ["+912000000106"],
         ["events@saffrontable.example"], "https://saffrontable.example", 4.5, 210,
         "saffrontable.sample", ["google_maps", "website"]),
        ("Green Leaf Kitchens", "Caterers", "Pune", ["+912000000107"], [], None, None, None,
         None, ["justdial"]),
        ("Coastal Feast Catering", "Caterers", "Mumbai", ["+912200000108"],
         ["book@coastalfeast.example"], "https://coastalfeast.example", 4.7, 96, None,
         ["google_maps", "justdial", "website"]),
        ("Monsoon Spice Caterers", "Caterers", "Mumbai", ["+912200000109"], [], None, 4.1, 12,
         None, ["google_maps"]),
        ("Silver Spoon Banquet Kitchen", "Caterers", "Mumbai", [],
         ["hello@silverspoon.example"], "https://silverspoon.example", None, None,
         "silverspoon.sample", ["website"]),
    ]
    for i, (name, category, place, phones, emails, site, rating, reviews, insta, sources) in \
            enumerate(rows, start=1):
        run.add(Lead(business_name=name, category=category, locality=place, phones=phones,
                     emails=emails, website=site, instagram=insta,
                     gmaps_url=_maps(i) if "google_maps" in sources else None,
                     gmaps_rating=rating, gmaps_reviews=reviews,
                     whatsapp="https://wa.me/912000000106" if i == 6 else None,
                     address=f"{10 + i} Sample Road, {place}", sources=sources,
                     notes=["also listed under category: Pediatric dentists"] if i == 2 else []),
                days=60 - i)
    for place in ("Pune", "Mumbai"):
        for category in ("Dentists", "Caterers"):
            run.unit("local_business", "google_maps", place, category, found=3, new=3)
            failed = (place, category) == ("Mumbai", "Dentists")
            run.unit("local_business", "justdial", place, category,
                     status="failed" if failed else "done", found=0 if failed else 1)
    run.failure("justdial", "blocked")
    return run.finish(minutes=38)


def mix(engine) -> int:
    register_demo_lead_types()
    document = _doc("Sample Austin software firms", "US", ["Austin"],
                    {"local_business": {"categories": [{"name": "Software companies"}]},
                     "demo_hiring": {"enabled": True}, "demo_funding": {"enabled": True}},
                    center=[30.2672, -97.7431], admin_area="Texas",
                    scoring={"weights": {"fit": 0.4, "intent": 0.4, "reach": 0.2}})
    run = _Run(engine, document, "US")
    firms = [
        ("Lonestar Ledger Software", "+15125550101", "https://lonestarledger.example", 4.6, 58),
        ("Bluebonnet Data Labs", "+15125550102", "https://bluebonnetdata.example", 4.8, 121),
        ("Congress Avenue Apps", "+15125550103", "https://congressapps.example", 4.3, 22),
        ("Hill Country Cloud Works", "+15125550104", None, 3.8, 9),
        ("Barton Springs Systems", "+15125550105", "https://bartonsystems.example", 4.5, 40),
        ("Pecan Street Robotics", "+15125550106", "https://pecanrobotics.example", None, None),
    ]
    for i, (name, phone, site, rating, reviews) in enumerate(firms, start=1):
        run.add(Lead(business_name=name, category="Software companies", locality="Austin",
                     phones=[phone], website=site, gmaps_url=_maps(100 + i),
                     gmaps_rating=rating, gmaps_reviews=reviews,
                     emails=[f"info@{site.split('//')[1]}"] if site and i % 2 else [],
                     linkedin=(f"https://www.linkedin.com/company/example-firm-{i:04d}"
                               if i < 4 else None),
                     address=f"{200 + i} Example Street, Austin, TX", sources=["google_maps"]),
                days=50 - i)
    # Found again by the fixture lead types (same phone: the same organisation).
    for name, phone in (("Bluebonnet Data Labs", "+15125550102"),
                        ("Congress Avenue Apps", "+15125550103"),
                        ("Barton Springs Systems", "+15125550105")):
        run.add(Lead(business_name=name, phones=[phone], sources=["demo_jobs"]),
                lead_type="demo_hiring", days=20)
    run.add(Lead(business_name="Bluebonnet Data Labs", phones=["+15125550102"],
                 sources=["demo_news"]), lead_type="demo_funding", days=15)
    # Only in the news.
    run.add(Lead(business_name="Mueller Health Analytics", website="https://muellerhealth.example",
                 locality="Austin", sources=["demo_news"]), lead_type="demo_funding", days=15)

    roles = {"Bluebonnet Data Labs": [("Senior Python Developer", 4), ("Data Engineer", 9),
                                      ("QA Automation Engineer", 33)],
             "Congress Avenue Apps": [("Mobile Developer (Flutter)", 12)],
             "Barton Springs Systems": [("DevOps Engineer", 75), ("IT Support Lead", 140)]}
    for org, posts in roles.items():
        for role, days in posts:
            run.signal(org, "demo_hiring", "job_posting", f"{role} posted", days,
                       f"https://jobs.example/{org.split()[0].lower()}/{days}", 0.6, role=role)
    run.signal("Bluebonnet Data Labs", "demo_funding", "funding_round",
               "Raised a $12M Series A", 20, "https://news.example/bluebonnet-series-a", 0.8,
               round="Series A", amount=12_000_000)
    run.signal("Mueller Health Analytics", "demo_funding", "funding_round",
               "Raised a $3M seed round", 52, "https://news.example/mueller-seed", 0.8,
               round="Seed", amount=3_000_000)
    for org, name, title, url in (
            ("Bluebonnet Data Labs", "Avery Example", "CTO",
             "https://www.linkedin.com/in/example-person-0001"),
            ("Bluebonnet Data Labs", "Jordan Sample", "Head of Engineering", None),
            ("Mueller Health Analytics", "Riley Placeholder", "Founder & CEO",
             "https://www.linkedin.com/in/example-person-0002")):
        run.insert(Person, organization_id=run.ids[org], name=name, title=title,
                   profile_url=url, source="website", created_at=FIXED_NOW)
    run.insert(Contact, organization_id=run.ids["Bluebonnet Data Labs"], channel="email",
               value="avery.example@bluebonnetdata.example", kind="inferred",
               source="email_pattern", confidence=0.6, created_at=FIXED_NOW)
    run.unit("local_business", "google_maps", "Austin", "Software companies", found=6, new=6)
    run.unit("demo_hiring", "demo_jobs", None, None, found=3)
    run.unit("demo_funding", "demo_news", None, None, found=2, new=1)
    return run.finish(minutes=22)


def venues(engine) -> int:
    run = _Run(engine, venues_document(), "IN")
    stays = [
        dict(business_name="Cloudline Hill Villa", locality="Igatpuri", property_type="Villa",
             price_min=32000.0, price_max=45000.0, price_basis="per night",
             price_source="platform", bedrooms=5, bathrooms=5.0, max_guests=14, has_pool=True,
             event_friendly=True, platforms=["airbnb"],
             amenities=["Private pool", "Lawn", "Mountain view"],
             listing_urls=["https://stays.example/cloudline"],
             platform_ratings={"airbnb": [4.9, 64]}, phones=["+912553000101"]),
        dict(business_name="Vineyard Courtyard Retreat",
             locality="Wine Country (Dindori / Ozar / Niphad)", property_type="Resort",
             price_min=14000.0, price_basis="per night", bedrooms=12, event_capacity=250,
             has_lawn=True, event_friendly=True, gmaps_rating=4.6, gmaps_reviews=410,
             website="https://vineyardcourtyard.example",
             emails=["stay@vineyardcourtyard.example"], phones=["+912530000102"]),
        dict(business_name="Gangapur Lakeside Lawns", locality="Gangapur Dam Belt",
             property_type="Banquet hall", price_min=1800.0, price_basis="per plate",
             event_capacity=800, has_lawn=True, event_friendly=True, gmaps_rating=4.4,
             gmaps_reviews=95, phones=["+912530000103"]),
        dict(business_name="Trimbak Valley Homestay", locality="Trimbakeshwar",
             property_type="Homestay", price_min=3200.0, price_basis="per night", bedrooms=2,
             max_guests=6, pet_friendly=True, gmaps_rating=4.1, gmaps_reviews=14,
             phones=["+912594000104"]),
        dict(business_name="Misty Peak Farm Stay", locality="Igatpuri",
             property_type="Farm stay", bedrooms=3, max_guests=10, has_pool=False,
             instagram="mistypeak.sample"),
        dict(business_name="Hilltop Villas Collective", locality="Region-wide",
             record_type="Brand", website="https://hilltopvillas.example",
             phones=["+912000000110"], emails=["book@hilltopvillas.example"]),
        dict(business_name="Ridge Estate Plot Sales", locality="Igatpuri",
             property_type="Real estate agency", phones=["+912553000111"],
             gmaps_rating=3.6, gmaps_reviews=7),
    ]
    for i, fields in enumerate(stays, start=1):
        fields.setdefault("record_type", "Property")
        fields.setdefault("category", "Villas and farm stays")
        on_maps = fields.get("gmaps_rating") is not None
        run.add(Lead(gmaps_url=_maps(200 + i) if on_maps else None,
                     address=f"Survey {i}, {fields['locality']}, Nashik district",
                     sources=["google_maps"] if on_maps else ["airbnb"], **fields),
                days=40 - i)
    for area in ("Igatpuri", "Trimbakeshwar", "Gangapur Dam Belt"):
        run.unit("local_business", "google_maps", area, "Villas and farm stays", found=2, new=2)
    run.unit("local_business", "airbnb", "Igatpuri", None, found=2, new=2)
    run.unit("local_business", "booking", "Nashik City", None, status="skipped",
             skip_reason="time budget spent")
    run.insert(AIUsage, run_id=run.run_id, provider="claude_cli", model="claude-haiku-4-5",
               feature="venues", calls=18, input_tokens=21000, output_tokens=3400,
               cost_usd=0.42, created_at=FIXED_NOW)
    return run.finish(minutes=95)


def empty(engine) -> int:
    document = _doc("Sample telescope makers", "IN", ["Nagpur"],
                    {"local_business": {"categories": [{"name": "Telescope makers"}]}},
                    center=[21.1458, 79.0882], admin_area="Maharashtra")
    run = _Run(engine, document, "IN")
    run.unit("local_business", "google_maps", "Nagpur", "Telescope makers")
    run.unit("local_business", "justdial", "Nagpur", "Telescope makers", status="failed")
    run.unit("local_business", "google_search", "Nagpur", "Telescope makers")
    run.failure("google_maps", "no_businesses_parsed", 2)
    run.failure("justdial", "blocked")
    return run.finish(minutes=4)


BUILDERS = {"local_business": local_business, "mix": mix, "venues": venues, "empty": empty}


def build(engine, scenario: str) -> int:
    """Write one scenario's run into `engine`; returns its run id."""
    return BUILDERS[scenario](engine)
