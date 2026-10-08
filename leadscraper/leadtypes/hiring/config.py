"""``lead_types.hiring``: which roles, where, how recent, from which sources.

```yaml
lead_types:
  hiring:
    enabled: true
    roles: [developer, qa, devops, data, it_support, digital_marketing]   # families
    keywords: []               # extra title words that count as a tech role
    seniority: []              # intern | junior | mid | senior | lead | manager; empty = any
    remote: include            # include | only | exclude
    posted_within_days: 30
    min_roles: 1               # fewer matching open roles: not a lead
    boards: [greenhouse:acme, lever:acme-labs]   # known ATS boards to check
    sources:
      ats: {enabled: true, kinds: [greenhouse, lever, ashby]}   # + workable, smartrecruiters (opt-in)
      hn_whos_hiring: {enabled: true, months: 1}
      careers_pages: {enabled: true, audit: run, max_orgs: 200}
      board_search: {enabled: false}     # SERP for ATS boards in the run's places (opt-in)
```
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Family = Literal["developer", "qa", "devops", "data", "it_support", "digital_marketing",
                 "design", "security", "erp", "product"]
ALL_FAMILIES: list[str] = ["developer", "qa", "devops", "data", "it_support",
                           "digital_marketing", "design", "security", "erp", "product"]
DEFAULT_FAMILIES: list[str] = ["developer", "qa", "devops", "data", "it_support",
                               "digital_marketing"]
ATS_KINDS = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters")
AtsKind = Literal["greenhouse", "lever", "ashby", "workable", "smartrecruiters"]
#: ATSs whose public job-board API is documented as public. Workable (an
#: undocumented widget endpoint) and SmartRecruiters (whose docs now describe
#: key-based access) are opt-in: A.2.4 is the project owner's call per source.
DEFAULT_ATS: list[str] = ["greenhouse", "lever", "ashby"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Source(_Strict):
    enabled: bool = True
    min_delay: float = Field(1.0, ge=0, description="Seconds between requests to its host.")
    max_delay: float = Field(2.5, ge=0)


class ATSSource(_Source):
    max_postings: int = Field(500, ge=1, description="Postings read per board.")
    kinds: list[AtsKind] = Field(
        default_factory=lambda: list(DEFAULT_ATS),
        description="ATSs read (configured boards and boards found on careers pages). "
                    "workable and smartrecruiters are opt-in: their public endpoints are "
                    "not documented as public APIs.")


class HNSource(_Source):
    months: int = Field(1, ge=1, le=6, description="Monthly 'Who is hiring?' threads read, "
                                                   "newest first.")


class CareersSource(_Source):
    audit: Literal["run", "project"] = Field(
        "run", description="Whose organisations' careers pages: this run's or the project's.")
    max_orgs: int = Field(200, ge=1, description="Sites looked at per run.")
    rescan_after_days: float = Field(7, ge=0, description="Skip sites scanned more recently "
                                                          "(0: always).")
    min_delay: float = Field(1.5, ge=0)
    max_delay: float = Field(4.0, ge=0)


class SearchSource(_Source):
    enabled: bool = False
    max_results: int = Field(20, ge=1, le=50, description="Search results read per place.")
    min_delay: float = Field(12.0, ge=0)
    max_delay: float = Field(28.0, ge=0)


class HiringSources(_Strict):
    ats: ATSSource = Field(default_factory=ATSSource)
    hn_whos_hiring: HNSource = Field(default_factory=HNSource)
    careers_pages: CareersSource = Field(default_factory=CareersSource)
    board_search: SearchSource = Field(default_factory=SearchSource)


class HiringConfig(_Strict):
    enabled: bool = True
    roles: list[Family] = Field(default_factory=lambda: list(DEFAULT_FAMILIES),
                                description="Role families that count as tech hiring.")
    keywords: list[str] = Field(default_factory=list,
                                description="Extra job-title words or phrases that count "
                                            "(matched as whole words).")
    seniority: list[Literal["intern", "junior", "mid", "senior", "lead", "manager"]] = Field(
        default_factory=list, description="Only these levels; empty = any.")
    remote: Literal["include", "only", "exclude"] = Field(
        "include", description="Remote roles open to the run's country: count them too "
                               "(include), only them (only), or not (exclude).")
    posted_within_days: int = Field(30, ge=1, description="Older postings are ignored.")
    min_roles: int = Field(1, ge=1, description="An organisation needs at least this many "
                                                "matching open roles.")
    boards: list[str] = Field(default_factory=list,
                              description="Known ATS boards, as kind:token "
                                          "(greenhouse:acme, lever:acme, ashby:acme, "
                                          "workable:acme, smartrecruiters:Acme1).")
    sources: HiringSources = Field(default_factory=HiringSources)

    @field_validator("boards")
    @classmethod
    def _boards(cls, value: list[str]) -> list[str]:
        out = []
        for board in value:
            kind, _, token = board.partition(":")
            if kind.strip().lower() not in ATS_KINDS or not token.strip():
                raise ValueError(f"board {board!r}: write kind:token, kind one of "
                                 f"{', '.join(ATS_KINDS)}")
            out.append(f"{kind.strip().lower()}:{token.strip()}")
        return list(dict.fromkeys(out))
