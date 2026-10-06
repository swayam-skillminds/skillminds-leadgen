"""Run-config schema, legacy importer and presets (APP-BUILD-PROMPT Phase 1.5)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from leadscraper.config import Config
from leadscraper.config.compile import compile_dict
from leadscraper.config.importer import (
    apply_legacy_defaults, dump_document, import_legacy_file, legacy_to_document,
)
from leadscraper.config.schema import RunConfig, run_config_json_schema
from leadscraper.paths import PROJECT_ROOT

BASELINE = PROJECT_ROOT / "tests" / "characterisation" / "baseline_configs"
PRESETS = {
    "config.yaml": PROJECT_ROOT / "presets" / "mumbai-event-vendors.yaml",
    "config.nashik.yaml": PROJECT_ROOT / "presets" / "nashik-venues.yaml",
}


def minimal(**overrides) -> dict:
    doc = {
        "name": "Austin dentists", "project": "austin-dentists",
        "geography": {"country": "US", "places": ["Austin"]},
        "lead_types": {"local_business": {"categories": [{"name": "Dentists"}]}},
    }
    doc.update(overrides)
    return doc


# --- the two legacy presets ----------------------------------------------------

@pytest.mark.parametrize("legacy_name", sorted(PRESETS))
def test_legacy_preset_loads(legacy_name):
    config = Config.load(PRESETS[legacy_name])
    assert config.run_config is not None
    assert config.run_config.schema_version == 1
    assert config.localities and config.categories


@pytest.mark.parametrize("legacy_name", sorted(PRESETS))
def test_preset_compiles_back_to_the_legacy_file_key_for_key(legacy_name):
    """Legacy file -> preset -> compiled view is lossless, so behaviour is identical."""
    original = yaml.safe_load((BASELINE / legacy_name).read_text(encoding="utf-8"))
    expected = apply_legacy_defaults(original)
    compiled = Config.load(PRESETS[legacy_name])._data
    assert {key: compiled.get(key) for key in expected} == expected
    # The legacy view also gains the names a profile used to imply.
    assert compiled["profile"] == str(original.get("profile") or "vendors")
    assert compiled["score_weights"] == {"fit": 1.0, "intent": 0.0, "reach": 0.0}


@pytest.mark.parametrize("legacy_name", sorted(PRESETS))
def test_committed_presets_match_the_importer(legacy_name):
    """A preset edited by hand (or a stale one) shows up here, not in a run."""
    live = PROJECT_ROOT / legacy_name
    document = RunConfig.model_validate(import_legacy_file(live)).to_document()
    assert yaml.safe_load(PRESETS[legacy_name].read_text(encoding="utf-8")) == \
        yaml.safe_load(dump_document(document))


@pytest.mark.parametrize("legacy_name", sorted(PRESETS))
def test_preset_and_legacy_file_run_identically(legacy_name):
    via_preset = Config.load(PRESETS[legacy_name])
    via_legacy = Config.load(BASELINE / legacy_name)
    assert via_preset.name_noise() == via_legacy.name_noise()
    assert via_preset.place_words == via_legacy.place_words
    assert via_preset.verticals == via_legacy.verticals
    assert via_preset.geo_center == via_legacy.geo_center
    assert [l.name for l in via_preset.localities] == [l.name for l in via_legacy.localities]


def test_project_names_follow_the_legacy_profile():
    assert import_legacy_file(BASELINE / "config.yaml")["project"] == "Mumbai event vendors"
    assert import_legacy_file(BASELINE / "config.nashik.yaml")["project"] == "Nashik venues"


def test_legacy_defaults_are_explicit_in_the_preset():
    """Nothing about Mumbai is left implicit in code: the preset spells it out."""
    doc = yaml.safe_load(PRESETS["config.yaml"].read_text(encoding="utf-8"))
    assert doc["geography"]["country"] == "IN"
    assert doc["geography"]["center"] == [19.076, 72.8777]
    assert doc["geography"]["admin_area"] == "Maharashtra"
    assert doc["geography"]["place_noise"] == ["mumbai"]


def test_unknown_legacy_top_level_keys_survive_the_round_trip():
    raw = {"city": "Pune", "categories": [{"name": "X", "queries": ["x"]}],
           "sources": {}, "some_future_key": {"a": 1}}
    rc = RunConfig.model_validate(legacy_to_document(raw))
    assert compile_dict(rc)["some_future_key"] == {"a": 1}


# --- a new config ----------------------------------------------------------------

def test_country_place_and_one_category_are_enough():
    rc = RunConfig.model_validate(minimal())
    config = Config(compile_dict(rc))
    assert config.country == "US"
    assert config.city == "Austin"
    assert [l.name for l in config.localities] == ["Austin"]
    assert config.categories[0].queries == ["Dentists"]
    # Sources left out = every source at polite defaults; packs decide applicability.
    assert config.source_enabled("google_maps") and config.source_enabled("website")
    assert config.delays("google_search") == (12.0, 28.0)
    assert config.currency == "USD"
    assert config.get("score_weights") == {"fit": 0.4, "intent": 0.4, "reach": 0.2}


def test_a_new_project_never_writes_over_the_legacy_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADSCRAPER_DATA_DIR", str(tmp_path))
    config = Config(compile_dict(RunConfig.model_validate(minimal())))
    csv_path = config.path_for("csv_export", "data/leads.csv")
    assert csv_path.is_relative_to(tmp_path)
    assert "austin-dentists" in str(csv_path)


def test_schema_rejects_what_cannot_run():
    with pytest.raises(ValidationError, match="ISO 3166"):
        RunConfig.model_validate(minimal(geography={"country": "XX", "places": ["A"]}))
    with pytest.raises(ValidationError, match="unknown lead type"):
        RunConfig.model_validate(minimal(lead_types={"astrology": {}}))
    with pytest.raises(ValidationError, match="at least one category"):
        RunConfig.model_validate(minimal(lead_types={"local_business": {"categories": []}}))
    with pytest.raises(ValidationError):
        RunConfig.model_validate(minimal(geography={"country": "IN", "places": []}))
    with pytest.raises(ValidationError, match="Extra inputs"):
        RunConfig.model_validate(minimal(colour="blue"))


def test_json_schema_documents_every_lead_type_section():
    schema = run_config_json_schema()
    assert schema["title"] == "Run config"
    assert {"geography", "lead_types", "scoring", "output"} <= set(schema["properties"])
    lead_types = schema["$defs"]["LeadTypes"]
    assert "local_business" in lead_types["properties"]
    assert "LocalBusinessConfig" in schema["$defs"]


def test_document_round_trips_through_yaml():
    rc = RunConfig.model_validate(minimal(time_budget_minutes=90))
    back = RunConfig.model_validate(yaml.safe_load(dump_document(rc.to_document())))
    assert compile_dict(back) == compile_dict(rc)
    assert back.time_budget_minutes == 90


def test_config_load_reads_a_run_config_file(tmp_path):
    path = tmp_path / "austin.yaml"
    path.write_text(dump_document(RunConfig.model_validate(minimal()).to_document()))
    config = Config.load(path)
    assert config.run_config.name == "Austin dentists"
    assert config.country == "US"
