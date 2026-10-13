"""European Union (and the EEA countries that publish on TED): public tenders from TED."""

from ..base import Pack
from .ted import COUNTRIES, TedTenders

PACK = Pack(
    key="eu",
    title="European Union",
    description="TED (Tenders Electronic Daily): public tenders of EU and EEA buyers, from "
                "the official Search API (no key).",
    applicable_countries=frozenset(COUNTRIES),
    tenders=(TedTenders,),
)
