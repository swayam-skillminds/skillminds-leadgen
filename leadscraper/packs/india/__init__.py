"""India pack: local business directories that only list Indian businesses, and the
central public-procurement portals (tenders: CPPP, GeM; both opt-in)."""

from ..base import Pack
from .cppp import CpppTenders
from .gem import GemTenders
from .indiamart import IndiaMartSource
from .local_directories import JustdialSource, SulekhaSource

PACK = Pack(
    key="in",
    title="India",
    description="Justdial, Sulekha and IndiaMART: Indian local and B2B directories. CPPP "
                "and GeM: central public tenders (opt-in: their copyright policies ask for "
                "permission).",
    applicable_countries=frozenset({"IN"}),
    discovery=(IndiaMartSource, JustdialSource, SulekhaSource),
    tenders=(CpppTenders, GemTenders),
    city_slugs={
        # Justdial routes by its own spelling of a city ("Bangalore", not
        # "Bengaluru"); Sulekha uses lower-case slugs.
        "justdial": {"bengaluru": "Bangalore", "new delhi": "Delhi", "gurugram": "Gurgaon"},
        "sulekha": {"bengaluru": "bangalore", "new delhi": "delhi"},
    },
)
