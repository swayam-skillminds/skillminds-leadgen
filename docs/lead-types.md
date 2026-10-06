# Lead types

A **lead type** is one way of finding organisations worth selling to:
businesses by category and place (`local_business`), companies hiring tech
roles (`hiring`, Phase 6), public tenders (`tenders`, Phase 7), and so on.
Each one is a self-contained plugin, and all of them resolve what they find
to the same deduplicated **organisation** in a project.

Built today: **`local_business`**. The rest of the A.7 table arrives in
Phases 6 to 8, each as its own plugin under this contract.

## The contract (`leadscraper/leadtypes/base.py`)

```python
class LeadType(ABC):
    key: str                                   # "local_business"
    title: str
    description: str
    config_model: type[BaseModel]              # validates lead_types.<key> in a run config
    applicable_countries: frozenset[str] | None   # None = anywhere

    def sources(self, cfg, geo=None) -> dict[str, list[str]]   # {"discovery": [...], "enrichment": [...]}
    def plan(self, cfg, geo=None, depth="balanced") -> list[WorkUnit]
    def signal_specs(self) -> list[SignalSpec]   # type, title, default strength, half-life
    def columns(self, cfg) -> list[ColumnSpec]   # Records-tab columns, with kind and fields toggle
    def score(self, org, signals, cfg) -> ScoreParts   # fit / intent / reach, 0..1, with reasons
    def run(self, context) -> LeadTypeResult     # execute its units; must not raise
```

| Type | Fields |
|---|---|
| `WorkUnit` | `lead_type`, `source`, `area`, `category`, `scope` (`run` / `locality` / `combo`). `key` is a stable id. |
| `SignalSpec` | `type`, `title`, `default_strength` (0..1), `half_life_days` (None = never ages), `description` |
| `ColumnSpec` | `key`, `header`, `kind` (`text` `number` `url` `phone` `email` `date` `score` `flag` `money`), `description`, `field` (the `fields.include` toggle, or None) |
| `ScoreParts` (`core/scoring.py`) | `lead_type`, `fit`, `intent`, `reach` (each 0..1 or None = "not my concern"), `reasons` |
| `LeadTypeResult` | `units_planned`, `units_done`, `new`, `merged`, `failed`, `error`, `stats` |

`cfg` is whatever the lead type reads its settings from. For `local_business`
that's the compiled `Config` view (see below). A new lead type would normally
take its own `config_model` instance.

## Rules every lead type keeps

1. **It depends only on core interfaces** (`core`, `sources`, `packs`, `db`),
   never on another lead type.
2. **Enabling, disabling or deleting it never changes another's output.**
   `tests/test_phase1_acceptance.py` proves this for `local_business`. With a
   second lead type enabled, disabled, or crashing, local_business's rows,
   score parts and evidence are byte-for-byte the same.
3. **A failure inside it is contained to its own work units.**
   `engine/runner.py` records the error and the other lead types still run.
4. **Its config is namespaced:** `lead_types.<key>.*`, its source settings
   included.
5. **Everything it writes is tagged with its key.** The store stamps
   `organizations.lead_types`, `evidence.lead_type`, `signals.lead_type` and
   `run_organizations.lead_type` with the active lead type.

## How a run uses it

```
RunConfig ──validate──► lead_types.<key> checked against registry.config_model(key)
          ──compile───► Config (the runtime view)
engine/runner.py: one project store, one Run row
   for each enabled lead type:
       store.lead_type = key
       lead_type.run(LeadTypeContext(config, store, run_id, …, failure_sink, unit_sink))
   finish_run(stats per lead type)
```

Scoring combines every lead type's `ScoreParts` with the run's weights
(`scoring.weights: {fit, intent, reach}`), see `core/scoring.py`:

- A component that no enabled lead type provides drops out, and the
  remaining weights are renormalised. A run with no intent signals isn't
  capped at 60.
- When two lead types score the same component, the stronger evidence wins
  (max), because they assess different things.
- Reasons from every lead type are kept, in order.

## `local_business`

Businesses by category and location. This is the original scraper, wrapped:

| Part | Where |
|---|---|
| Plugin object | `leadtypes/local_business/plugin.py` (`LOCAL_BUSINESS`) |
| Config model | `leadtypes/local_business/config.py` (`LocalBusinessConfig`), plus `DEFAULT_SOURCES` for configs that leave `sources` out |
| Source selection and unit order | `leadtypes/local_business/planning.py`, shared by `plan()` and the pipeline, so what's reported is what runs (`test_plan_lists_the_units_the_pipeline_executes_in_order`). A source whose `fills` the run's `fields.include` leaves out is skipped as "not needed" |
| Execution | `leadtypes/local_business/pipeline.py` (`Pipeline`): discovery → (venues: AI classification and brand roll-up) → name polish → enrichment → AI extraction → finalise |
| Scoring | `leadtypes/local_business/scoring.py` (`Scorer`) |
| Hospitality vertical | `packs/hospitality/venues.py`: venue types, tiers, prices |

Its config section:

```yaml
lead_types:
  local_business:
    enabled: true
    verticals: [events]            # events | hospitality; hospitality = the venue layout and tiers
    categories:
      - name: Caterers
        queries: [catering services, wedding caterers]   # default: [name]
        directory_slugs: {wedmegood: wedding-catering}
        sources: []                # only these sources (empty = all)
        only_areas: []             # only these areas (empty = all)
        indiamart: true            # pack-specific keys pass through
    sources:                       # leave out = every applicable source at polite defaults
      google_maps: {enabled: true, min_delay: 2.5, max_delay: 6.0, max_results_per_query: 25}
    scoring: {weights: {maps_popularity: 0.4, …}}    # the legacy component weights (its fit)
    dedup: {phone_match_min_similarity: 60, domain_match: true, …}
    hospitality: {brands: […], venue_tiers: {…}, premium_platforms: […], platform_searches: {…}}
```

- **Signals:** none. Ratings and listings are fit evidence, not a reason to
  buy now.
- **Score:**
  - *fit* is the legitimacy score for event vendors, or the venue score for
    the hospitality vertical (/100);
  - *reach* is contact completeness;
  - *intent* isn't provided.

  The imported legacy presets weight `{fit: 1, intent: 0, reach: 0}`, so
  their rankings equal the original ones exactly (the characterisation
  suite pins every score). Reasons look like "Google rating 4.6 from 310
  reviews", "listed by 3 sources (google_maps, justdial, wedmegood)" and
  "reachable by phone, email, website".
- **Columns:** the vendor layout (`Lead.to_row`) or the venue layout
  (`Lead.to_venue_row`). Prices are in the run's currency. Phase 4 assembles
  the Records tab from these `ColumnSpec`s.

## Adding a lead type

1. Create `leadscraper/leadtypes/<key>/` with a `config.py` (pydantic model
   with `enabled: bool = True`) and a `plugin.py` defining a `LeadType`
   subclass and a module-level instance.
2. Register it in `leadtypes/registry.py:_load_builtins`. The run-config
   schema and its JSON Schema pick it up automatically.
3. Put its sources in `leadscraper/sources/` if they apply everywhere, or in
   a pack if they only make sense in some countries or verticals.
4. Write through `context.store`; the store tags rows with the key.
5. Tests: fixture-based tests for its sources, signals, columns and score,
   plus an isolation test like `test_enabling_or_disabling_a_lead_type_leaves_local_business_unchanged`.
