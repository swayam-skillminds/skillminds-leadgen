"""Social pack: public Facebook pages, Instagram profiles, and LinkedIn URLs
that a search engine has already indexed (LinkedIn itself is never crawled)."""

from ..base import Pack
from .facebook import FacebookSource
from .instagram import InstagramEnricher
from .linkedin_serp import LinkedInSerpSource

PACK = Pack(
    key="social",
    title="Social",
    description="Facebook pages, Instagram profiles, search-indexed LinkedIn URLs.",
    discovery=(FacebookSource, LinkedInSerpSource),
    enrichment=(InstagramEnricher,),
)
