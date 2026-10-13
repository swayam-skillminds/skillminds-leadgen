"""United States: federal contract opportunities from SAM.gov (free API key)."""

from ..base import Pack
from .sam_gov import SamGovTenders

PACK = Pack(
    key="us",
    title="United States",
    description="SAM.gov: federal contract opportunities, from the official Get "
                "Opportunities API (a free personal API key; 10 requests a day without a "
                "SAM.gov role).",
    applicable_countries=frozenset({"US"}),
    tenders=(SamGovTenders,),
)
