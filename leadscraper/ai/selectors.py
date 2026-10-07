"""Self-healing selectors (Phase 5.6): AI once per page layout, then rules for free.

For a listing or profile page with a repeating layout (the ``ai_directory``
source's approved directories, and the extraction fallback):

1. **Stored selectors first.** ``(host, page_type)`` has an active selector set:
   apply it with BeautifulSoup -- no AI call. If it still finds records, done.
2. **Stale.** It finds nothing (a redesign): a ``selectors_stale`` failure is
   recorded against the source, the set is retired, and selectors are
   regenerated -- once per (host, page type) per run.
3. **Generate.** The worker model reads the page (token-lean Markdown) and
   returns the records it sees; then it proposes CSS selectors from a trimmed
   HTML sample. They are accepted only if, applied by rule, they reproduce the
   AI's records on this page *and* on one more page of the same type (which the
   model also reads). Accepted selectors are stored with a version and date.
4. **Fall back.** No second page, invalid selectors, or a second failure: the
   model's own per-page reading is used, within the run's AI budget, and the
   event (``selectors_failed``) lands in the source's health.

Every record, whichever path produced it, is grounded: its name must appear on
the page and its contact details are kept only when the page carries them.

Selector syntax is CSS (soupsieve), with ``@attr`` to read an attribute:
``a.website@href``. XPath is not used: CSS covers listing layouts, and one
syntax is easier to validate.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field

from .grounding import SourceText, ground_contacts
from .pageprep import prepare

log = logging.getLogger(__name__)

FIELDS = ("name", "phone", "email", "address", "website", "profile_url")


# --- what the model returns -------------------------------------------------------

class ListingRecord(BaseModel):
    name: str = Field(..., description="The business's name exactly as the page shows it.")
    phone: str | None = Field(None, description="Copied verbatim, or null.")
    email: str | None = None
    address: str | None = None
    website: str | None = Field(None, description="The business's own site, if linked.")
    profile_url: str | None = Field(None, description="Link to its profile on this site.")
    category: str | None = None


class ListingExtraction(BaseModel):
    items: list[ListingRecord] = Field(default_factory=list)


class SelectorProposal(BaseModel):
    item: str = Field(..., description="CSS selector matching each record's container.")
    name: str = Field(..., description="CSS selector inside the item; '@attr' reads an "
                                       "attribute, e.g. 'a.title@href'.")
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    website: str | None = None
    profile_url: str | None = None
    next_page: str | None = Field(None, description="CSS selector of the next-page link "
                                                    "with @href, or null.")


EXTRACT_SYSTEM = (
    "You read a business directory or business profile page and list the businesses on "
    "it. For each, return its name and any phone, email, address, website and profile "
    "link that the page itself shows. Copy values verbatim from the page -- never invent, "
    "complete or guess a value; use null when a field is absent. Ignore adverts, "
    "navigation and 'related' blocks. Return ONLY JSON: {\"items\": [...]}."
)
SELECTOR_SYSTEM = (
    "You write CSS selectors (no XPath) for a page with a repeating layout. `item` must "
    "match each record's container element; every other selector is relative to the "
    "item. Append '@attr' to read an attribute (e.g. 'a.site@href'); otherwise the "
    "element's text is read. Prefer stable class names over positions. Use null for a "
    "field the layout doesn't have. The records you should match are listed after the "
    "HTML. Return ONLY JSON matching the schema."
)


# --- storage ----------------------------------------------------------------------------

@dataclass
class SelectorSet:
    host: str
    page_type: str
    selectors: dict[str, Any]
    version: int = 1
    id: int | None = None
    created_at: datetime | None = None


class SelectorStore(Protocol):
    def get(self, host: str, page_type: str) -> SelectorSet | None: ...

    def save(self, host: str, page_type: str, selectors: dict[str, Any],
             sample_url: str | None) -> SelectorSet: ...

    def retire(self, selector_set: SelectorSet, reason: str) -> None: ...


class MemorySelectorStore:
    """Selectors in memory (tests, dry runs)."""

    def __init__(self) -> None:
        self.sets: dict[tuple[str, str], list[SelectorSet]] = {}
        self.retired: list[tuple[SelectorSet, str]] = []

    def get(self, host: str, page_type: str) -> SelectorSet | None:
        versions = self.sets.get((host, page_type)) or []
        return versions[-1] if versions else None

    def save(self, host, page_type, selectors, sample_url) -> SelectorSet:
        versions = self.sets.setdefault((host, page_type), [])
        latest = max([s.version for s in versions] + [s.version for s, _ in self.retired
                                                       if (s.host, s.page_type) ==
                                                       (host, page_type)] + [0])
        item = SelectorSet(host, page_type, dict(selectors), version=latest + 1,
                           created_at=datetime.now(timezone.utc))
        versions.append(item)
        return item

    def retire(self, selector_set: SelectorSet, reason: str) -> None:
        versions = self.sets.get((selector_set.host, selector_set.page_type)) or []
        if selector_set in versions:
            versions.remove(selector_set)
        self.retired.append((selector_set, reason))


class DbSelectorStore:
    """Selectors in the ``selector_sets`` table, per workspace (survive runs)."""

    def __init__(self, engine, workspace_id: int):
        self.engine = engine
        self.workspace_id = workspace_id

    def _table(self):
        from ..db.models import SelectorSetRow

        return SelectorSetRow.__table__

    def get(self, host: str, page_type: str) -> SelectorSet | None:
        from sqlalchemy import select

        table = self._table()
        with self.engine.connect() as conn:
            row = conn.execute(select(table).where(
                table.c.workspace_id == self.workspace_id, table.c.host == host,
                table.c.page_type == page_type, table.c.status == "active")
                .order_by(table.c.version.desc()).limit(1)).mappings().first()
        if not row:
            return None
        return SelectorSet(host, page_type, dict(row["selectors"] or {}), version=row["version"],
                           id=row["id"], created_at=row["created_at"])

    def save(self, host, page_type, selectors, sample_url) -> SelectorSet:
        from sqlalchemy import func, select

        table = self._table()
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            latest = conn.execute(select(func.max(table.c.version)).where(
                table.c.workspace_id == self.workspace_id, table.c.host == host,
                table.c.page_type == page_type)).scalar() or 0
            new_id = conn.execute(table.insert().values(
                workspace_id=self.workspace_id, host=host[:255], page_type=page_type[:50],
                version=latest + 1, selectors=dict(selectors), status="active",
                sample_url=(sample_url or "")[:2000] or None, created_at=now,
                validated_at=now)).inserted_primary_key[0]
        return SelectorSet(host, page_type, dict(selectors), version=latest + 1, id=new_id,
                           created_at=now)

    def retire(self, selector_set: SelectorSet, reason: str) -> None:
        from sqlalchemy import update

        if selector_set.id is None:
            return
        table = self._table()
        with self.engine.begin() as conn:
            conn.execute(update(table).where(table.c.id == selector_set.id)
                         .values(status="stale", note=reason[:500]))


# --- applying selectors -------------------------------------------------------------

def _read(scope: Tag, selector: str | None, base_url: str | None) -> str | None:
    if not selector:
        return None
    css, _, attr = selector.partition("@")
    css = css.strip()
    try:
        node = scope.select_one(css) if css else scope
    except Exception:                 # an invalid selector the model wrote
        raise ValueError(f"invalid CSS selector {css!r}") from None
    if node is None:
        return None
    if attr:
        value = node.get(attr.strip())
        if isinstance(value, list):
            value = " ".join(value)
        if value and attr.strip() in ("href", "src") and base_url:
            value = urljoin(base_url, str(value))
        return str(value).strip() if value else None
    text = " ".join(node.get_text(" ", strip=True).split())
    return text or None


def apply(selectors: dict[str, Any], html: str, base_url: str | None = None) -> list[dict]:
    """Records the selectors find on `html`. Raises ValueError on an invalid selector."""
    soup = BeautifulSoup(html or "", "lxml")
    item_css = str(selectors.get("item") or "").strip()
    if not item_css:
        raise ValueError("no item selector")
    try:
        items = soup.select(item_css)
    except Exception:
        raise ValueError(f"invalid CSS selector {item_css!r}") from None
    records = []
    for item in items:
        record = {name: _read(item, selectors.get(name), base_url) for name in FIELDS}
        if record.get("name"):
            records.append(record)
    return records


def next_page(selectors: dict[str, Any], html: str, base_url: str | None) -> str | None:
    if not selectors.get("next_page"):
        return None
    soup = BeautifulSoup(html or "", "lxml")
    try:
        return _read(soup, selectors["next_page"], base_url)
    except ValueError:
        return None


def trim_html(html: str, max_chars: int = 24_000, keep_items: int = 4) -> str:
    """A sample of the page's markup for proposing selectors.

    Scripts, styles and most attributes go; class, id, href, itemprop and
    data-* stay (the selectors need them). Long runs of same-shaped siblings
    (the listing itself) are cut to the first few, and long texts are shortened.
    """
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "template", "iframe", "head",
                     "link", "meta", "img", "picture", "source", "form"]):
        tag.decompose()
    for tag in soup.find_all(True):
        tag.attrs = {k: v for k, v in tag.attrs.items()
                     if k in ("class", "id", "href", "itemprop", "itemtype", "rel")
                     or k.startswith("data-")}
        if tag.get("href") and len(str(tag["href"])) > 120:
            tag["href"] = str(tag["href"])[:120]
    for parent in soup.find_all(True):
        children = [c for c in parent.find_all(True, recursive=False)]
        shapes: dict[tuple, int] = {}
        for child in children:
            shape = (child.name, tuple(child.get("class") or ()))
            shapes[shape] = shapes.get(shape, 0) + 1
            if shapes[shape] > keep_items:
                child.decompose()
    for text in soup.find_all(string=True):
        if len(text) > 200:
            text.replace_with(text[:200] + "…")
    body = soup.body or soup
    out = re.sub(r"\n\s*\n+", "\n", str(body))
    return out[:max_chars]


# --- the engine -------------------------------------------------------------------------

def _key(name: str | None) -> str:
    return re.sub(r"[\W_]+", " ", (name or "").casefold()).strip()


def matches(rule_records: list[dict], ai_records: list[ListingRecord], *,
            threshold: float = 0.8) -> bool:
    """The selectors' records reproduce the model's on this page (names, phones)."""
    if not rule_records or not ai_records:
        return False
    rule_names = {_key(r.get("name")) for r in rule_records}
    ai_names = {_key(r.name) for r in ai_records}
    recall = len(ai_names & rule_names) / len(ai_names)
    precision = len(ai_names & rule_names) / len(rule_names)
    if recall < threshold or precision < threshold:
        return False
    by_name = {_key(r.get("name")): r for r in rule_records}
    digits = lambda s: re.sub(r"\D", "", s or "")[-8:]  # noqa: E731
    with_phone = [r for r in ai_records if r.phone and _key(r.name) in by_name]
    if with_phone:
        agree = sum(1 for r in with_phone
                    if digits(by_name[_key(r.name)].get("phone")) == digits(r.phone))
        if agree / len(with_phone) < threshold:
            return False
    return True


@dataclass
class Page:
    url: str
    html: str


@dataclass
class Extraction:
    records: list[dict] = field(default_factory=list)
    #: stored | generated | regenerated | ai_per_page | none
    path: str = "none"
    selector_version: int | None = None
    #: Contact details the grounding check dropped (hallucinations), for the log.
    dropped: list[str] = field(default_factory=list)


class SelfHealingExtractor:
    """Records from pages of one (host, page type), AI only when the layout is new."""

    def __init__(self, client, store: SelectorStore, *, failures=None, region: str | None = None,
                 source: str = "ai_directory", task: str = "ai_directory"):
        self.client = client
        self.store = store
        self.failures = failures
        self.region = region
        self.source = source
        self.task = task
        self._regenerated: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    # --- public ------------------------------------------------------------------

    def extract(self, host: str, page_type: str, page: Page,
                validation_page: Page | None = None) -> Extraction:
        stored = self.store.get(host, page_type)
        if stored is not None:
            try:
                records = apply(stored.selectors, page.html, page.url)
            except ValueError:
                records = []
            if records:
                return self._grounded(records, page, "stored", stored.version)
            self._record("selectors_stale", page.url,
                         f"{host} {page_type} selectors v{stored.version} matched nothing")
            self.store.retire(stored, "matched nothing (layout changed?)")
            with self._lock:
                if (host, page_type) in self._regenerated:
                    return self._per_page(page)
                self._regenerated.add((host, page_type))
            return self._generate(host, page_type, page, validation_page, "regenerated")
        return self._generate(host, page_type, page, validation_page, "generated")

    # --- internals -------------------------------------------------------------------

    def _ai_read(self, page: Page) -> list[ListingRecord] | None:
        if self.client is None:
            return None
        prepared = prepare(page.html, page.url)
        records: list[ListingRecord] = []
        for chunk in prepared.chunks[:4]:
            result = self.client.call(self.task, system=EXTRACT_SYSTEM,
                                      user=f"Page: {page.url}\n\n{chunk}",
                                      output=ListingExtraction)
            if result is None:
                return records or None
            records.extend(result.data.items)
        return records

    def _propose(self, page: Page, records: list[ListingRecord]) -> SelectorProposal | None:
        if self.client is None:
            return None
        sample = trim_html(page.html)
        names = "\n".join(f"- {r.name}" + (f" | {r.phone}" if r.phone else "")
                          for r in records[:12])
        result = self.client.call(self.task, system=SELECTOR_SYSTEM,
                                  user=f"Page: {page.url}\n\nHTML sample:\n{sample}\n\n"
                                       f"Records on this page:\n{names}",
                                  output=SelectorProposal, max_tokens=1500)
        return result.data if result else None

    def _generate(self, host: str, page_type: str, page: Page, validation_page: Page | None,
                  path: str) -> Extraction:
        reference = self._ai_read(page)
        if not reference:
            return Extraction(path="none")
        proposal = self._propose(page, reference)
        accepted = False
        if proposal is not None and validation_page is not None:
            selectors = proposal.model_dump()
            try:
                first = apply(selectors, page.html, page.url)
                accepted = matches(first, reference)
                if accepted:
                    second_reference = self._ai_read(validation_page)
                    second = apply(selectors, validation_page.html, validation_page.url)
                    accepted = bool(second_reference) and matches(second, second_reference)
            except ValueError as exc:
                log.info("proposed selectors for %s rejected: %s", host, exc)
                accepted = False
            if accepted:
                saved = self.store.save(host, page_type, selectors, page.url)
                log.info("selectors for %s %s accepted (v%d)", host, page_type, saved.version)
                return self._grounded(first, page, path, saved.version)
        reason = ("no second page of this type to validate on" if validation_page is None
                  else "proposed selectors did not reproduce the AI's records")
        self._record("selectors_failed", page.url, f"{host} {page_type}: {reason}")
        return self._grounded([r.model_dump() for r in reference], page, "ai_per_page", None)

    def _per_page(self, page: Page) -> Extraction:
        reference = self._ai_read(page)
        if not reference:
            return Extraction(path="none")
        return self._grounded([r.model_dump() for r in reference], page, "ai_per_page", None)

    def _grounded(self, records: list[dict], page: Page, path: str,
                  version: int | None) -> Extraction:
        source = SourceText(prepare(page.html, page.url).source_text, self.region)
        out = Extraction(path=path, selector_version=version)
        for record in records:
            name = (record.get("name") or "").strip()
            if not name or not source.has_text(name):
                out.dropped.append(f"name {name[:60]!r} not on the page")
                continue
            grounded = ground_contacts({"phones": record.get("phone"),
                                        "emails": record.get("email"),
                                        "address": record.get("address"),
                                        "website": record.get("website")}, source)
            out.dropped.extend(grounded.dropped)
            profile = record.get("profile_url")
            if profile and not str(profile).startswith("http"):
                profile = urljoin(page.url, str(profile))
            out.records.append({"name": name[:300], "phones": grounded.phones,
                                "emails": grounded.emails, "address": grounded.address,
                                "website": grounded.website, "profile_url": profile,
                                "category": record.get("category"), "source_url": page.url})
        if out.dropped:
            self._record("ai_ungrounded_dropped", page.url, "; ".join(out.dropped[:5]))
        return out

    def _record(self, reason: str, url: str, detail: str) -> None:
        if self.failures is not None:
            try:
                self.failures.record(self.source, reason, url=url, detail=detail[:400])
            except Exception as exc:
                log.debug("could not record %s: %s", reason, exc)
