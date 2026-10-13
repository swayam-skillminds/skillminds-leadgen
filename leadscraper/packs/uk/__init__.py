"""United Kingdom pack: Companies House officers as decision-makers (decision_makers),
newly incorporated companies (registrations), and public tenders from Find a Tender and
Contracts Finder (tenders)."""

from ..base import Pack
from .companies_house import CompaniesHouseDirectors, CompaniesHouseIncorporations
from .contracts_finder import ContractsFinderTenders
from .find_a_tender import FindATenderTenders

PACK = Pack(
    key="uk",
    title="United Kingdom",
    description="Companies House: a company's current directors and new incorporations, "
                "from the official API (free key). Find a Tender and Contracts Finder: public "
                "tenders (no key).",
    applicable_countries=frozenset({"GB"}),
    registries=(CompaniesHouseDirectors,),
    tenders=(FindATenderTenders, ContractsFinderTenders),
    incorporations=(CompaniesHouseIncorporations,),
)
