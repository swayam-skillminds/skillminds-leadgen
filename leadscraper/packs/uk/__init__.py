"""United Kingdom pack: Companies House officers as decision-makers (decision_makers)."""

from ..base import Pack
from .companies_house import CompaniesHouseDirectors

PACK = Pack(
    key="uk",
    title="United Kingdom",
    description="Companies House: a company's current directors, from the official API "
                "(free key).",
    applicable_countries=frozenset({"GB"}),
    registries=(CompaniesHouseDirectors,),
)
