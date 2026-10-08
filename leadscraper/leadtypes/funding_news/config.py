"""``lead_types.funding_news``: which events, how recent, from which news sources.

```yaml
lead_types:
  funding_news:
    enabled: true
    events: [funding, launch, expansion, acquisition, digital_transformation,
             leadership_change]          # default: all
    keywords: []               # extra search words (added to every query)
    recency_days: 30           # older news is ignored (GDELT keeps 90 days)
    place_filter: headline     # headline | article | none (see below)
    create_new: true           # a company nobody holds becomes an organisation
    sources:
      gdelt: {enabled: true, max_records: 100}           # GDELT DOC API (open)
      google_news: {enabled: false}                      # terms forbid it: opt-in
      press_releases: {enabled: false}                   # GlobeNewswire country feed: opt-in
      feeds: {enabled: true, urls: []}                   # your own RSS / Atom feeds
```

``place_filter``: with places in the geography, ``headline`` keeps a story only
when its headline (or a feed's summary) names a place or area of the run;
``article`` also keeps stories whose text mentions it (GDELT matches the whole
article: noisier); ``none`` keeps the country's news.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EventKind = Literal["funding", "launch", "expansion", "acquisition", "digital_transformation",
                    "leadership_change"]
ALL_EVENTS: list[str] = ["funding", "launch", "expansion", "acquisition",
                         "digital_transformation", "leadership_change"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Source(_Strict):
    enabled: bool = True
    min_delay: float = Field(2.0, ge=0, description="Seconds between requests to its host.")
    max_delay: float = Field(4.0, ge=0)


class GdeltSource(_Source):
    max_records: int = Field(100, ge=1, le=250, description="Articles read per query.")
    #: GDELT asks for at most one request every 5 seconds.
    min_delay: float = Field(6.0, ge=5.0)
    max_delay: float = Field(9.0, ge=5.0)


class GoogleNewsSource(_Source):
    enabled: bool = False


class PressReleaseSource(_Source):
    enabled: bool = False


class FeedSource(_Source):
    urls: list[str] = Field(default_factory=list,
                            description="RSS or Atom feeds you are entitled to read.")

    @field_validator("urls")
    @classmethod
    def _urls(cls, value: list[str]) -> list[str]:
        out = []
        for url in value:
            url = url.strip()
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"feed {url!r}: an http(s) URL")
            out.append(url)
        return list(dict.fromkeys(out))


class NewsSources(_Strict):
    gdelt: GdeltSource = Field(default_factory=GdeltSource)
    google_news: GoogleNewsSource = Field(default_factory=GoogleNewsSource)
    press_releases: PressReleaseSource = Field(default_factory=PressReleaseSource)
    feeds: FeedSource = Field(default_factory=FeedSource)


class FundingNewsConfig(_Strict):
    enabled: bool = True
    events: list[EventKind] = Field(default_factory=lambda: list(ALL_EVENTS),
                                    description="Which kinds of news count.")
    keywords: list[str] = Field(default_factory=list,
                                description="Extra search words, added to every query "
                                            "(an industry, a technology).")
    recency_days: int = Field(30, ge=1, le=365, description="Older news is ignored.")
    place_filter: Literal["headline", "article", "none"] = Field(
        "headline", description="Keep a story only when its headline names a run place "
                                "(headline), when the article does (article), or always "
                                "(none).")
    create_new: bool = Field(True, description="A company the project doesn't hold yet "
                                               "becomes an organisation.")
    sources: NewsSources = Field(default_factory=NewsSources)
