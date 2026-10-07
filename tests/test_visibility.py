"""Every registered source must be accounted for in the log, run after run.

google_search, justdial, facebook and linkedin_serp were enabled in config and
never executed once: `_build` dropped unrequested entries with a bare
`continue`, so ~900 lines of scraper left no trace anywhere. These tests pin
the log lines that make a silent skip impossible to repeat.
"""

import inspect
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # sibling helpers
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from leadscraper.leadtypes.local_business import pipeline as pipeline_mod
from leadscraper.net.http import HttpClient
from leadscraper.leadtypes.local_business.pipeline import Pipeline
from leadscraper.sources.base import Enricher, Source
from test_pipeline import make_config


class StubSource(Source):
    name = "stub"

    def discover(self, locality, category):
        yield self.make_lead(business_name="Royal Caterers", category=category.name,
                             locality=locality.name, phones=["+919820012345"])


class OtherSource(Source):
    """Registered alongside the stub -- the one that must not vanish silently."""

    name = "broken"

    def discover(self, locality, category):
        return iter(())


class StubEnricher(Enricher):
    name = "stub_enricher"

    def targets(self, store):
        return []

    def enrich(self, lead):
        return None


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES",
                        {"stub": StubSource, "broken": OtherSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES",
                        {"stub_enricher": StubEnricher})
    return make_config(tmp_path)


def build(config, **kwargs):
    return Pipeline(config, localities=config.localities,
                    categories=config.categories, **kwargs)


def messages(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def test_not_requested_source_says_so_and_says_what_was_requested(wired, caplog):
    caplog.set_level(logging.INFO)
    build(wired, source_names=["stub"], enricher_names=[])
    lines = [m for m in messages(caplog) if "broken" in m and "not requested" in m]
    assert lines, "a registered source was skipped without a word in the log"
    # The cause has to be readable without opening the CLI invocation.
    assert any("stub" in line for line in lines)


def test_disabled_source_says_so(wired, caplog):
    caplog.set_level(logging.INFO)
    wired.set("sources.broken.enabled", False)
    build(wired, source_names=["stub", "broken"], enricher_names=[])
    assert any("broken" in m and "disabled in config" in m for m in messages(caplog))


def test_built_sources_are_logged_too(wired, caplog):
    caplog.set_level(logging.INFO)
    build(wired, source_names=["stub"], enricher_names=[])
    assert any("stub" in m and "built" in m for m in messages(caplog))


def test_run_start_names_registered_requested_built_and_skipped(wired, caplog):
    pipe = build(wired, source_names=["stub"], enricher_names=[])
    caplog.set_level(logging.INFO)
    pipe.run()
    line = next(m for m in messages(caplog) if m.startswith("run start:"))
    for word in ("registered", "requested", "built", "skipped"):
        assert word in line, f"run start line never says {word!r}: {line}"
    assert "stub" in line and "broken" in line
    # Enrichers used to go unmentioned entirely.
    assert "enrichers registered" in line and "stub_enricher" in line


# --- dead-domain wiring, which lands in http.py/store.py separately --------

class StoreWithoutDeadDomains:
    """A store from before the dead-domain cache existed."""

    def __init__(self, db_path, scorer=None):
        self.db_path, self.scorer = db_path, scorer

    def close(self) -> None:
        pass


class StoreWithDeadDomains(StoreWithoutDeadDomains):
    def is_dead_domain(self, host: str) -> bool:
        return False

    def mark_dead_domain(self, host: str, reason: str) -> None:
        pass

    def dead_domain_count(self) -> int:
        return 7


def test_pipeline_constructs_when_the_store_has_no_dead_domain_cache(
    wired, monkeypatch, caplog
):
    """The guard must hold while the store half of this change is still in flight."""
    monkeypatch.setattr(pipeline_mod, "LeadStore", StoreWithoutDeadDomains)
    caplog.set_level(logging.INFO)
    pipe = build(wired, source_names=["stub"], enricher_names=[])
    assert isinstance(pipe.store, StoreWithoutDeadDomains)
    assert any("dead-domain skipping off" in m for m in messages(caplog))


def test_dead_domain_hooks_are_passed_once_both_halves_exist(wired, monkeypatch, caplog):
    monkeypatch.setattr(pipeline_mod, "LeadStore", StoreWithDeadDomains)
    caplog.set_level(logging.INFO)
    pipe = build(wired, source_names=["stub"], enricher_names=[])
    hooks = pipe._dead_domain_hooks()
    # Branching on the real signature rather than the expected one: this file
    # must not fail while HttpClient is being taught the kwargs elsewhere.
    if "dead_domain_check" in inspect.signature(HttpClient.__init__).parameters:
        assert hooks["dead_domain_check"] == pipe.store.is_dead_domain
        assert hooks["on_dead_domain"] == pipe.store.mark_dead_domain
        assert any("dead-domain cache: 7 host(s)" in m for m in messages(caplog))
    else:
        assert hooks == {}
        assert any("no dead-domain hooks" in m for m in messages(caplog))
