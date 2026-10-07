"""Self-healing selectors (Phase 5.6 acceptance), all three paths:

1. generated -- on first contact the worker model reads page 1, proposes CSS
   selectors, and they are accepted only after reproducing its records on
   page 1 *and* page 2 of the same layout;
2. reused -- another page of that layout is read by the stored selectors with
   zero AI calls;
3. regenerated -- after a simulated redesign the stored selectors match
   nothing (``selectors_stale``), and new ones are generated and validated.

The "model" is an OpenAI-compatible endpoint behind a fake transport: it reads
the page the way a model would (from the Markdown it is sent) and proposes
selectors for the layout in the HTML sample it is sent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from leadscraper.ai.client import AIClient
from leadscraper.ai.selectors import (
    EXTRACT_SYSTEM, SELECTOR_SYSTEM, DbSelectorStore, MemorySelectorStore, Page,
    SelfHealingExtractor, apply, trim_html,
)
from leadscraper.ai.settings import resolve
from leadscraper.config.legacy import Config

from ai_fakes import llm, local_provider

PAGES = Path(__file__).parent / "fixtures" / "pages"
HOST = "punebusinesspages.example"

V1 = {"item": "div.listing-card", "name": "h3.biz-name a", "phone": ".biz-phone",
      "email": None, "address": ".biz-address", "website": "a.biz-website@href",
      "profile_url": "h3.biz-name a@href", "next_page": "a[rel=next]@href"}
V2 = {"item": "article.provider", "name": ".provider__title", "phone": "[data-role=call]",
      "email": None, "address": ".provider__location", "website": "a.provider__site@href",
      "profile_url": ".provider__title a@href", "next_page": "a.more@href"}


def page(name: str) -> Page:
    return Page(url=f"https://{HOST}/pune/dentists/{name}",
                html=(PAGES / name).read_text(encoding="utf-8"))


def read_markdown(markdown: str) -> list[dict]:
    """What a model reads off a listing page's Markdown: name, address, phone, site."""
    records, current = [], None
    for line in markdown.splitlines():
        heading = re.match(r"^#{2,3} (.+)$", line)
        if heading and not line.startswith("# "):
            current = {"name": re.sub(r"\[(.+?)\]\(.+?\)", r"\1", heading.group(1)).strip()}
            records.append(current)
            continue
        if current is None:
            continue
        phone = re.search(r"\b98200 00\d{3}\b", line)
        if phone and "phone" not in current:
            current["phone"] = phone.group(0)
        site = re.search(r"\((https://[a-z]+\.example/)\)", line)
        if site and "Website" in line or (site and "Visit site" in line):
            current["website"] = site.group(1)
        if "Pune" in line and "address" not in current and not phone and "★" not in line:
            current["address"] = line.strip()
    return [r for r in records if "phone" in r]


class Model:
    """The scripted model; counts calls by task."""

    def __init__(self, invent: str | None = None):
        self.calls = {"extract": 0, "selectors": 0}
        self.invent = invent

    def __call__(self, system: str, user: str):
        if system == EXTRACT_SYSTEM:
            self.calls["extract"] += 1
            records = read_markdown(user)
            if self.invent:
                records.append({"name": self.invent, "phone": "98200 09999"})
            return {"items": records}
        if system == SELECTOR_SYSTEM:
            self.calls["selectors"] += 1
            return V2 if "provider__title" in user else V1
        raise AssertionError(f"unexpected prompt: {system[:60]}")


class Failures:
    def __init__(self):
        self.reasons: list[str] = []

    def record(self, source, reason, **kw):
        self.reasons.append(reason)


def extractor(monkeypatch, model: Model, store=None):
    llm(monkeypatch, model)
    client = AIClient(resolve(Config({"ai": {"enabled": True, "providers": [local_provider()],
                                             "features": ["source_discovery"]}})))
    failures = Failures()
    return SelfHealingExtractor(client, store or MemorySelectorStore(), failures=failures,
                                region="IN"), failures


def test_generated_reused_and_regenerated(monkeypatch):
    model = Model()
    ex, failures = extractor(monkeypatch, model)

    # 1. Generated: read page 1, propose, validate on page 1 and page 2.
    first = ex.extract(HOST, "listing", page("directory_v1_page1.html"),
                       page("directory_v1_page2.html"))
    assert first.path == "generated" and first.selector_version == 1
    assert model.calls == {"extract": 2, "selectors": 1}
    assert [r["name"] for r in first.records][:2] == ["Bright Smile Dental Studio",
                                                      "Lotus Family Dentistry"]
    assert len(first.records) == 8
    bright = first.records[0]
    assert bright["phones"] == ["+919820000101"]
    assert bright["website"] == "https://brightsmile.example/"
    assert bright["profile_url"].endswith("/pune/dentists/bright-smile-dental-studio/")
    stored = ex.store.get(HOST, "listing")
    assert stored.selectors["item"] == "div.listing-card" and stored.created_at is not None

    # 2. Reused: another page of the same layout, by rule -- zero AI calls.
    before = dict(model.calls)
    second = ex.extract(HOST, "listing", page("directory_v1_page2.html"))
    assert second.path == "stored" and model.calls == before
    assert len(second.records) == 6 and second.records[-1]["name"] == "Modern Dental Studio"

    # 3. Regenerated: the site is redesigned; the v1 selectors match nothing.
    third = ex.extract(HOST, "listing", page("directory_v2_page1.html"),
                       page("directory_v2_page2.html"))
    assert "selectors_stale" in failures.reasons
    assert third.path == "regenerated" and third.selector_version == 2
    assert model.calls["selectors"] == before["selectors"] + 1
    assert len(third.records) == 8 and third.records[3]["name"] == "Pearl Orthodontics"
    assert third.records[3]["phones"] == ["+919820000104"]
    # ...and the new selectors run for free from then on.
    before = dict(model.calls)
    fourth = ex.extract(HOST, "listing", page("directory_v2_page2.html"))
    assert fourth.path == "stored" and model.calls == before and len(fourth.records) == 6


def test_a_second_failure_in_one_run_falls_back_to_per_page_ai(monkeypatch):
    model = Model()
    ex, failures = extractor(monkeypatch, model)
    ex.extract(HOST, "listing", page("directory_v1_page1.html"), page("directory_v1_page2.html"))
    ex.extract(HOST, "listing", page("directory_v2_page1.html"), page("directory_v2_page2.html"))
    # Redesigned back again within the same run: regenerate only once.
    again = ex.extract(HOST, "listing", page("directory_v1_page2.html"))
    assert again.path == "ai_per_page" and len(again.records) == 6
    assert failures.reasons.count("selectors_stale") == 2


def test_selectors_are_not_accepted_without_a_second_page(monkeypatch):
    model = Model()
    ex, failures = extractor(monkeypatch, model)
    result = ex.extract(HOST, "listing", page("directory_v1_page1.html"))
    assert result.path == "ai_per_page" and len(result.records) == 8
    assert ex.store.get(HOST, "listing") is None
    assert "selectors_failed" in failures.reasons


def test_selectors_that_do_not_reproduce_the_records_are_rejected(monkeypatch):
    model = Model()
    original = model.__call__

    def wrong(system, user):
        if system == SELECTOR_SYSTEM:
            model.calls["selectors"] += 1
            return {**V1, "name": ".biz-address"}         # reads the wrong field
        return original(system, user)

    ex, failures = extractor(monkeypatch, wrong)
    result = ex.extract(HOST, "listing", page("directory_v1_page1.html"),
                        page("directory_v1_page2.html"))
    assert result.path == "ai_per_page" and ex.store.get(HOST, "listing") is None


def test_records_the_model_invents_are_dropped(monkeypatch):
    model = Model(invent="Phantom Dental Spa")
    ex, failures = extractor(monkeypatch, model)
    result = ex.extract(HOST, "listing", page("directory_v1_page1.html"))
    names = [r["name"] for r in result.records]
    assert "Phantom Dental Spa" not in names and len(names) == 8
    assert any("Phantom Dental Spa" in d for d in result.dropped)
    assert "ai_ungrounded_dropped" in failures.reasons


def test_stored_selectors_survive_between_runs(monkeypatch):
    from app_db import fresh_application_db
    from leadscraper.services.workspace import default_workspace

    engine = fresh_application_db()
    ws = default_workspace(engine)
    model = Model()
    ex, _ = extractor(monkeypatch, model, DbSelectorStore(engine, ws.id))
    ex.extract(HOST, "listing", page("directory_v1_page1.html"), page("directory_v1_page2.html"))
    calls = dict(model.calls)
    # A new run: a new extractor and client, the same database.
    later, _ = extractor(monkeypatch, model, DbSelectorStore(engine, ws.id))
    result = later.extract(HOST, "listing", page("directory_v1_page2.html"))
    assert result.path == "stored" and result.selector_version == 1 and model.calls == calls
    engine.dispose()


def test_invalid_css_is_refused_not_crashing():
    with pytest.raises(ValueError):
        apply({"item": "div[[["}, "<div></div>")
    assert apply(V1, "<html><body>nothing here</body></html>") == []


def test_the_selector_sample_is_trimmed():
    html = (PAGES / "directory_v1_page1.html").read_text(encoding="utf-8")
    sample = trim_html(html)
    assert len(sample) < len(html) / 4
    assert sample.count("listing-card") == 4                 # repeats cut to a few
    assert "gtm.js" not in sample and 'class="biz-phone"' in sample
