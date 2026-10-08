"""``lead_types.decision_makers``: whose people, which roles, from which sources.

```yaml
lead_types:
  decision_makers:
    enabled: true
    scope: run                 # run | project: whose organisations to look into
    roles: [owner, executive, technology, it]
    max_people: 5              # per organisation, owners and chief executives first
    max_orgs: 200              # per run
    refresh_after_days: 30     # looked into more recently: skipped (--force-enrich re-reads)
    sources:
      team_pages: {enabled: true, max_pages: 3}     # the company's own team / about pages
      registry: {enabled: true}                     # company registries in country packs (UK)
      linkedin_search: {enabled: false}             # search-indexed profiles (SERP): opt-in
      github: {enabled: false}                      # public org members (AUP): opt-in
    email_pattern: {enabled: false}                 # labelled inference; MX check only
```
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Family = Literal["owner", "executive", "technology", "it"]
ALL_FAMILIES: list[str] = ["owner", "executive", "technology", "it"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Source(_Strict):
    enabled: bool = True
    min_delay: float = Field(1.5, ge=0, description="Seconds between requests to its host.")
    max_delay: float = Field(4.0, ge=0)


class TeamPagesSource(_Source):
    max_pages: int = Field(3, ge=1, le=10, description="Team / about pages read per site "
                                                       "(besides the homepage).")


class RegistrySource(_Source):
    key_secret_name: str = Field("companies-house-api-key",
                                 description="The secret holding the UK Companies House API "
                                             "key; without it the registry is skipped.")
    min_delay: float = Field(0.6, ge=0.5)
    max_delay: float = Field(1.2, ge=0.5)

    @field_validator("key_secret_name")
    @classmethod
    def _own_secret(cls, value: str) -> str:
        # A registry key is only ever sent to its registry: no other secret can be named.
        if value and not value.startswith("companies-house"):
            raise ValueError("key_secret_name must name a companies-house-… secret")
        return value


class LinkedInSearchSource(_Source):
    enabled: bool = False
    max_results: int = Field(10, ge=1, le=30, description="Search results read per company.")
    min_delay: float = Field(12.0, ge=0)
    max_delay: float = Field(28.0, ge=0)


class GitHubSource(_Source):
    enabled: bool = False
    token_secret_name: str = Field("github-token",
                                   description="Optional GitHub token (5,000 requests an "
                                               "hour instead of 60).")
    max_members: int = Field(30, ge=1, le=100, description="Public members read per org.")
    min_delay: float = Field(1.0, ge=0)
    max_delay: float = Field(2.0, ge=0)

    @field_validator("token_secret_name")
    @classmethod
    def _own_secret(cls, value: str) -> str:
        if value and not value.startswith("github"):
            raise ValueError("token_secret_name must name a github-… secret")
        return value


class PeopleSources(_Strict):
    team_pages: TeamPagesSource = Field(default_factory=TeamPagesSource)
    registry: RegistrySource = Field(default_factory=RegistrySource)
    linkedin_search: LinkedInSearchSource = Field(default_factory=LinkedInSearchSource)
    github: GitHubSource = Field(default_factory=GitHubSource)


class EmailPattern(_Strict):
    enabled: bool = Field(False, description="Infer addresses for named people from the "
                                             "pattern of published ones (labelled, "
                                             "MX-checked only, never used for matching).")


class DecisionMakersConfig(_Strict):
    enabled: bool = True
    scope: Literal["run", "project"] = Field(
        "run", description="Whose organisations: this run's (any lead type's) or the "
                           "project's.")
    roles: list[Family] = Field(default_factory=lambda: list(ALL_FAMILIES),
                                description="Which decision-makers count: owner (founders, "
                                            "partners), executive (CEO, MD, directors), "
                                            "technology (CTO, CIO, heads of engineering or "
                                            "digital), it (IT managers).")
    max_people: int = Field(5, ge=1, le=50, description="People kept per organisation.")
    max_orgs: int = Field(200, ge=1, description="Organisations looked into per run.")
    refresh_after_days: float = Field(30, ge=0, description="Skip organisations looked into "
                                                            "more recently (0: always).")
    workers: int = Field(4, ge=1, le=16, description="Sites read at once (one request at a "
                                                     "time per site).")
    sources: PeopleSources = Field(default_factory=PeopleSources)
    email_pattern: EmailPattern = Field(default_factory=EmailPattern)
