"""Which job titles are tech hiring, at what level, and whether a posting is in the run's area.

Families are whole-word patterns over the title, so "Senior PHP Developer" is a
developer and "Sales Engineer" is nothing. ``keywords`` from the config count
as a family of their own ("custom"). Location: a posting counts when it names
one of the run's places (or areas), or -- unless remote roles are excluded --
when it is remote and open to the run's country.
"""

from __future__ import annotations

import re
from typing import Iterable

FAMILY_TITLES = {
    "developer": "Developer", "qa": "QA / testing", "devops": "DevOps / cloud",
    "data": "Data / ML", "it_support": "IT support / admin",
    "digital_marketing": "Digital marketing", "design": "UI/UX design",
    "security": "Security", "erp": "ERP", "product": "Product", "custom": "Keyword",
}

_LANG = (r"software|back[- ]?end|front[- ]?end|full[- ]?stack|web|mobile|android|ios|flutter|"
         r"react(?: native)?|node(?:\.?js)?|java|python|php|\.net|dotnet|golang|ruby(?: on rails)?|"
         r"rails|laravel|django|angular|vue(?:\.?js)?|javascript|typescript|c\+\+|c#|embedded|"
         r"firmware|game|salesforce|shopify|wordpress|magento|application|platform|api|"
         r"blockchain|unity|kotlin|swift|scala|rust|elixir")
FAMILY_PATTERNS: dict[str, re.Pattern] = {k: re.compile(v, re.I) for k, v in {
    "developer": rf"(?<![\w-])(?:{_LANG})\s+(?:developer|engineer|programmer|architect)\b|"
                 r"\b(?:developer|programmer|sde(?:\s*(?:i{1,3}|[1-3]))?|swe|"
                 r"engineering manager|tech(?:nical)? lead)\b",
    "qa": r"\b(?:qa|quality assurance|sdet|tester|test (?:engineer|analyst|lead|manager)|"
          r"automation test(?:er|ing)?|software testing)\b",
    "devops": r"\b(?:devops|dev ops|devsecops|mlops|site reliability|sre|platform engineer|"
              r"cloud (?:engineer|architect|administrator|ops)|infrastructure engineer|"
              r"kubernetes|release engineer|build engineer)\b",
    "data": r"\b(?:data (?:engineer|scientist|analyst|architect)|machine learning|ml engineer|"
            r"ai engineer|analytics engineer|bi (?:developer|analyst|engineer)|"
            r"business intelligence|big data|etl developer|"
            r"database (?:administrator|developer|engineer)|dba)\b",
    "it_support": r"\b(?:it (?:support|technician|administrator|executive|manager|engineer|"
                  r"specialist|officer)|help ?desk|service desk|desktop support|"
                  r"technical support|systems? administrator|sysadmin|"
                  r"network (?:engineer|administrator|admin)|noc engineer)\b",
    "digital_marketing": r"\b(?:digital marketing|seo|sem|ppc|performance marketing|"
                         r"social media (?:manager|executive|marketer|specialist)|"
                         r"content marketing|growth (?:marketer|marketing|hacker)|"
                         r"email marketing|marketing automation|paid (?:media|social|search))\b",
    "design": r"\b(?:ui|ux|ui/ux|ux/ui|product|web|visual|interaction) designer\b|"
              r"\bux (?:researcher|writer)\b",
    "security": r"\b(?:security (?:engineer|analyst|architect|specialist)|cyber ?security|"
                r"soc analyst|penetration tester|pentester|infosec|application security)\b",
    "erp": r"\b(?:sap|erp|oracle (?:apps|ebs|fusion)|odoo|netsuite|dynamics 365|navision)\b",
    "product": r"\b(?:product manager|product owner|technical product manager|scrum master|"
               r"technical program manager)\b",
}.items()}

SENIORITY = (
    ("intern", re.compile(r"\b(?:intern(?:ship)?|trainee|apprentice|graduate)\b", re.I)),
    ("manager", re.compile(r"\b(?:manager|director|head of|vp|vice president|cto|cio)\b", re.I)),
    ("lead", re.compile(r"\b(?:lead|principal|staff|architect)\b", re.I)),
    ("senior", re.compile(r"\b(?:senior|sr\.?)\b", re.I)),
    ("junior", re.compile(r"\b(?:junior|jr\.?|associate|entry[- ]level|fresher)\b", re.I)),
)

#: Country names a location may use, beyond the country's own name.
COUNTRY_ALIASES = {
    "US": ("usa", "u.s.", "united states", "us"),
    "GB": ("uk", "united kingdom", "england", "scotland", "wales", "britain"),
    "AE": ("uae", "united arab emirates", "dubai"), "IN": ("india",),
    "DE": ("germany", "deutschland"), "NL": ("netherlands",), "AU": ("australia",),
    "CA": ("canada",), "SG": ("singapore",),
}
_WORLDWIDE = re.compile(r"\b(?:global|worldwide|anywhere|any location|international)\b", re.I)
_REMOTE = re.compile(r"\b(?:remote|work from home|wfh|distributed)\b", re.I)
_REMOTE_QUALIFIER = re.compile(r"\bremote\b\s*(?:\(([^)]*)\)|[-–—:,/]\s*([a-z .&]+))", re.I)
_REGION_WORDS = re.compile(r"\b(?:us|usa|uk|eu|emea|europe|americas|apac|latam|canada|"
                           r"north america|united states|united kingdom)\b", re.I)


def _word_in(phrase: str, text: str) -> bool:
    phrase = phrase.strip().casefold()
    return bool(phrase) and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)",
                                      (text or "").casefold()) is not None


def classify(title: str, families: Iterable[str], keywords: Iterable[str] = ()) -> str | None:
    """The first wanted family the title belongs to, "custom" for a keyword, else None."""
    for family in families:
        pattern = FAMILY_PATTERNS.get(family)
        if pattern is not None and pattern.search(title or ""):
            return family
    if any(_word_in(k, title or "") for k in keywords):
        return "custom"
    return None


def families_in(text: str, families: Iterable[str],
                keywords: Iterable[str] = ()) -> list[tuple[str, str]]:
    """(family, the words that matched) for every role named in free text (an HN comment)."""
    found: list[tuple[str, str]] = []
    for family in families:
        pattern = FAMILY_PATTERNS.get(family)
        if pattern is not None:
            found.extend((family, m.group(0).strip()) for m in pattern.finditer(text or ""))
    found.extend(("custom", k) for k in keywords if _word_in(k, text or ""))
    return found


def seniority_of(title: str) -> str:
    for level, pattern in SENIORITY:
        if pattern.search(title or ""):
            return level
    return "mid"


def country_words(country: str, name: str = "") -> list[str]:
    words = [name.casefold()] if name else []
    words.extend(COUNTRY_ALIASES.get((country or "").upper(), ()))
    return [w for w in dict.fromkeys(words) if w]


def is_remote(location: str, flag: bool | None = None) -> bool:
    return bool(flag) or bool(_REMOTE.search(location or ""))


def remote_open_to(location: str, country_terms: list[str]) -> bool:
    """A remote posting is open to the run's country: it says so, or names no region at all."""
    text = location or ""
    if any(_word_in(word, text) for word in country_terms):
        return True
    for match in _REMOTE_QUALIFIER.finditer(text):
        qualifier = (match.group(1) or match.group(2) or "").strip()
        if not qualifier or _WORLDWIDE.search(qualifier):
            continue
        return False                 # "Remote (US)", "Remote - EU" for a run elsewhere
    return not _REGION_WORDS.search(text) or bool(_WORLDWIDE.search(text))


def location_match(location: str, *, remote: bool | None, places: list[str],
                   country_terms: list[str], policy: str = "include") -> str | None:
    """"place" (names a run place), "remote" (remote and open to the country) or None."""
    text = location or ""
    remote_ok = is_remote(text, remote) and remote_open_to(text, country_terms)
    if policy == "only":
        return "remote" if remote_ok else None
    if any(_word_in(place, text) for place in places):
        return "place"
    if policy == "exclude":
        return None
    return "remote" if remote_ok else None
