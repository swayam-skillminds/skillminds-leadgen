"""Scoring v2 (Phase 1.7): fit / intent / reach per lead type, with reasons."""

from __future__ import annotations

import pytest

from leadscraper.config import Config
from leadscraper.core.lead import Lead
from leadscraper.core.scoring import LEGACY_WEIGHTS, ScoreParts, combine
from leadscraper.leadtypes.local_business.plugin import LOCAL_BUSINESS
from leadscraper.leadtypes.local_business.scoring import Scorer

RICH = Lead(business_name="Royal Caterers", locality="Andheri", phones=["+919820012345"],
            emails=["hi@royal.in"], website="https://royal.in", gmaps_rating=4.6,
            gmaps_reviews=310, sources=["google_maps", "justdial", "wedmegood"],
            wmg_rating=4.8, wmg_reviews=52, wmg_featured=True)


def test_legacy_weights_reproduce_the_legacy_score_exactly():
    scorer = Scorer()                         # a config without score_weights = legacy
    legacy_total = round(scorer.breakdown(RICH).total, 2)
    assert scorer.score(RICH) == legacy_total
    assert scorer.score_weights == LEGACY_WEIGHTS


def test_parts_are_fit_and_reach_with_reasons():
    parts = Scorer().parts(RICH)
    assert parts.lead_type == "local_business"
    assert parts.fit == pytest.approx(round(Scorer().breakdown(RICH).total, 2) / 100)
    assert parts.intent is None
    assert parts.reach == pytest.approx(Scorer().contact_completeness(RICH))
    text = " | ".join(parts.reasons)
    assert "Google rating 4.6 from 310 reviews" in text
    assert "listed by 3 sources" in text
    assert "WedMeGood 4.8 from 52 reviews, featured" in text
    assert "reachable by phone, email, website" in text


def test_reasons_say_what_is_missing():
    bare = Lead(business_name="X", sources=["google_search"])
    reasons = Scorer().parts(bare).reasons
    assert "found on google_search only" in reasons
    assert "no phone, email or website yet" in reasons


def test_weights_combine_and_renormalise_over_components_in_play():
    parts = [ScoreParts("local_business", fit=0.6, reach=0.9)]
    # No lead type provides intent: its weight drops out instead of capping at 60.
    overall = combine(parts, {"fit": 0.4, "intent": 0.4, "reach": 0.2})
    assert overall.total == pytest.approx(100 * (0.4 * 0.6 + 0.2 * 0.9) / 0.6, abs=0.01)
    assert overall.intent is None


def test_each_lead_type_contributes_and_the_stronger_evidence_wins():
    overall = combine([ScoreParts("local_business", fit=0.6, reach=0.5, reasons=["a"]),
                       ScoreParts("hiring", intent=0.8, fit=0.3, reasons=["b"])],
                      {"fit": 0.4, "intent": 0.4, "reach": 0.2})
    assert (overall.fit, overall.intent, overall.reach) == (0.6, 0.8, 0.5)
    assert overall.contributions == {"local_business": {"fit": 0.6, "reach": 0.5},
                                     "hiring": {"fit": 0.3, "intent": 0.8}}
    assert overall.reasons == ["a", "b"]
    assert overall.total == pytest.approx(100 * (0.4 * 0.6 + 0.4 * 0.8 + 0.2 * 0.5))


def test_run_weights_from_config_change_the_ranking():
    by_reach = Scorer(Config({"score_weights": {"fit": 0, "intent": 0, "reach": 1}}))
    assert by_reach.score(RICH) == pytest.approx(100 * Scorer().contact_completeness(RICH),
                                                 abs=0.01)


def test_unknown_components_are_refused():
    with pytest.raises(ValueError, match="unknown score components"):
        combine([], {"fit": 1, "vibes": 2})


def test_the_plugin_scores_through_the_same_scorer():
    parts = LOCAL_BUSINESS.score(RICH, [], Config({}))
    assert parts == Scorer().parts(RICH)
