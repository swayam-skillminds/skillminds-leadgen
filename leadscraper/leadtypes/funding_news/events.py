"""Headline rules: which event a headline reports, and which company it is about.

A business-news headline puts the company first and the event right after it:
"Pune-based fintech startup Definedge raises Rs 22 crore in pre-Series A",
"Acme Robotics opens new office in Pune", "Brightline Foods appoints Priya
Shah as CTO". So an event is a verb phrase from one of the tables below, and
the company is the run of name-like words immediately before it, after
descriptors ("Pune-based fintech startup") are dropped. A headline with no
such run before the verb ("Pune mosque offers prayers and raises funds for…")
names no company and is not an event. Exclusions keep "raises concerns",
"launches campaign" and the like out.

Press releases name their issuer (``issuer``): that is the company, whatever
the headline's grammar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .amounts import Amount, parse_amount, parse_round

TITLES = {"funding": "Funding round", "launch": "Launch", "expansion": "Expansion",
          "acquisition": "Acquisition", "digital_transformation": "Digital transformation",
          "leadership_change": "Leadership change"}

#: Words that describe a company rather than name it ("fintech startup Acme").
DESCRIPTORS = frozenset("""
startup startups start-up firm brand platform maker manufacturer provider player unicorn
soonicorn giant major marketplace company fintech edtech healthtech medtech agritech agtech
proptech insurtech deeptech foodtech cleantech climatetech legaltech martech adtech
traveltech spacetech saas d2c b2b b2c ecommerce e-commerce retailer operator lender nbfc
supplier exporter distributor
""".split())
#: Subjects that are public bodies, people in office or places -- not companies.
NOT_COMPANIES = frozenset("""
govt government ministry minister police court cm pm mla mp bjp congress aap party cabinet
collector district mosque temple church army navy sebi rbi parliament assembly council
municipal mayor state centre union india us usa uk china world
rto pmc pcmc pmrda bmc mmrda msrtc mhada cidco midc nhai uidai drdo isro railway railways
university college school collectorate corporator ward zilla panchayat tehsil
""".split())
_CONNECTORS = frozenset({"&", "and", "of", "de", "the", "for"})
_PREFIX = re.compile(r"^(?:[\w ]{2,30}?)\s*[:|]\s+(?=\S)")
_POSSESSIVE = re.compile(r"(?:'s|’s)$", re.I)
_MAX_NAME_WORDS = 6

_PRE = (r"(?i:(?:\s+(?:today|now|recently|officially|formally))?"
        r"(?:\s+(?:has|have|is set to|is to|plans to|to|will))?)")
FUNDING_VERB = re.compile(_PRE + r"\s+(?P<verb>raises?|raised|secures?|secured|bags?|bagged|"
                          r"closes?|closed|lands?|landed|nets?|netted|picks up|picked up|"
                          r"gets|got|receives?|received|attracts?|mops up|garners?|garnered)"
                          r"\b(?P<rest>.*)$", re.I)
_FUNDING_OBJECT = re.compile(r"funding|\bround\b|\bseed\b|\bseries [a-h]\b|investment|capital|"
                             r"financing|\bfund(?:ing)?\b|crore|\bcr\b|million|\bmn\b|billion|"
                             r"\bbn\b|lakh|[$₹€£]|\brs\b|\binr\b|\busd\b|\beur\b|\bgbp\b", re.I)
_FUNDING_NOT = re.compile(r"^\s*(?:the\s+)?(?:funds?|money)\s+for\b|^\s*(?:awareness|concerns?|"
                          r"questions?|alarm|eyebrows|stakes|prices?|rates?|tariffs?|fares?|the bar|"
                          r"(?:its\s+)?voice|objections?|demands?|hopes?|red flags?|fears?|doubts?|"
                          r"a toast|(?:its\s+)?guidance|forecast|outlook|limit|cap|dividend|"
                          r"salar(?:y|ies)|pay|issues?|the issue)\b", re.I)
ACQUIRED_BY = re.compile(_PRE + r"\s+(?:is\s+|was\s+|gets\s+|to be\s+)?(?P<verb>acquired)\s+by\s+"
                         r"(?P<rest>.*)$", re.I)
ACQUISITION_VERB = re.compile(_PRE + r"\s+(?P<verb>acquires?|acquired|buys?|bought|takes over|"
                              r"took over|snaps up|completes acquisition of|"
                              r"announces acquisition of|merges with)\b(?P<rest>.*)$", re.I)
_PLACE_NOUNS = (r"(?:office|offices|centre|center|campus|facility|plant|hub|headquarters|hq|"
                r"studio|store|stores|outlet|branch|unit|factory|warehouse|r&d|gcc|lab)")
#: What a company "launches" when it expands (a launched store is usually an app store).
_WORK_NOUNS = (r"(?:office|offices|centre|center|campus|facility|plant|hub|headquarters|hq|"
               r"r&d|gcc|lab|delivery centre|development centre)")
_FILLER = r"(?:(?!(?:on|in|at|for|with|to)\b)[\w&-]+\s+){0,4}?"
EXPANSION_VERB = re.compile(
    _PRE + r"\s+(?P<verb>(?:opens?|opened|inaugurates?|inaugurated)\s+"
    r"(?:its\s+|a\s+|an\s+|the\s+)?" + _FILLER + _PLACE_NOUNS + r"\b|"
    r"(?:launches?|launched|unveils?|unveiled)\s+(?:its\s+|a\s+|an\s+|the\s+)?" + _FILLER
    + _WORK_NOUNS + r"\b|"
    r"expands?(?:\s+(?:to|into|in|its|operations|presence|footprint|across))|expanded|"
    r"sets up|set up|establishes|forays? into|enters|entered|marks (?:its )?entry into)"
    r"(?P<rest>.*)$", re.I)
LAUNCH_VERB = re.compile(_PRE + r"\s+(?P<verb>launches?|launched|unveils?|unveiled|introduces?|"
                         r"introduced|rolls out|rolled out|debuts?|announces launch of)\b"
                         r"(?P<rest>.*)$", re.I)
_LAUNCH_NOT = re.compile(r"^\s*(?:a\s+|an\s+|the\s+|its\s+|new\s+)*(?:campaign|drive|initiative|"
                         r"probe|investigation|inquiry|scheme|contest|competition|offer|sale|"
                         r"discount|protest|attack|strike|crackdown|appeal|petition|ipo|"
                         r"manhunt|search|at|on (?:the )?(?:bourses?|nse|bse|nasdaq|nyse|"
                         r"stock|market|exchange|street))\b", re.I)
TRANSFORMATION_VERB = re.compile(
    _PRE + r"\s+(?P<verb>partners? with|partnered with|ties? up with|tied up with|teams? up with|"
    r"collaborates? with|selects?|selected|chooses?|chose|picks|deploys?|deployed|implements?|"
    r"implemented|adopts?|adopted|migrates? to|migrated to|moves? to|shifts? to|"
    r"goes live (?:on|with)|went live (?:on|with)|transitions? to|taps|signs (?:a |an )?"
    r"(?:deal|pact|agreement|mou) with|embarks on|accelerates|digiti[sz]es|digitali[sz]es)"
    r"\b(?P<rest>.*)$", re.I)
_TECH = re.compile(r"\b(?:digital|digiti[sz]\w*|digitali[sz]\w*|cloud|erp|sap|s/4hana|oracle|"
                   r"salesforce|microsoft|azure|aws|google cloud|ai|artificial intelligence|"
                   r"genai|automation|crm|moderni[sz]\w*|transformation|data platform|analytics|"
                   r"cybersecurity|software|it systems|odoo|zoho|servicenow|workday)\b", re.I)
# Person names are matched case-sensitively (capitalised words); verbs and roles are not.
_ROLE = (r"(?P<role>(?i:(?:group\s+|global\s+|new\s+|interim\s+)?(?:chief [\w ]{2,30}?officer|"
         r"c[etifmdo]o|cxo|cdio|managing director|md|president|vice[- ]president|svp|evp|"
         r"vp\b[\w ,&]{0,40}|head of [\w ]{2,30}|[\w ]{0,20}director|general manager|gm|"
         r"chairman|chairperson|chair|co-founder|partner))\b)")
_PERSON = (r"(?P<person>(?:(?i:dr|mr|ms|mrs)\.?\s+)?[A-Z][\w.'’-]+"
           r"(?:\s+[A-Z][\w.'’-]+){1,3})")
APPOINTS = re.compile(_PRE + r"\s+(?P<verb>(?i:appoints?|appointed|names?|named|elevates?|"
                      r"elevated|promotes?|promoted|ropes in|roped in|onboards?|hires?|hired|"
                      r"welcomes?|announces (?:the )?appointment of))\s+"
                      r"(?:(?i:former)\s+[\w ]{0,40}?\s+)?" + _PERSON +
                      r"(?:\s+(?i:as|to))?\s+(?:(?i:its|the|new)\s+)*" + _ROLE
                      + r"(?P<rest>.*)$")
NAMES_NEW = re.compile(_PRE + r"\s+(?P<verb>(?i:appoints?|names?|gets|hires?))\s+"
                       r"(?:(?i:a)\s+)?(?i:new)\s+" + _ROLE + r"(?P<rest>.*)$")
JOINS = re.compile(r"^" + _PERSON + r"\s+(?i:joins|joined|to join)\s+(?P<company>.+?)\s+"
                   r"(?i:as)\s+(?:(?i:its|the|new)\s+)*" + _ROLE + r"(?P<rest>.*)$")
_LEAD_ROLE = re.compile(r"\b(?:chief|c[etifmdo]o|cxo|cdio|managing director|md|president|"
                        r"vice[- ]president|svp|evp|vp|head|director|general manager|gm|chair\w*|"
                        r"co-founder|partner)\b", re.I)
_TECH_ROLE = re.compile(r"\b(?:cto|cio|cdo|cdio|ciso|technology|information|digital|"
                        r"engineering|it\b|data|product)", re.I)
_LED_BY = re.compile(r"\bled by\s+(?P<who>[^,;]+?)(?=\s*(?:,|;|\bat\b|\bwith\b|\bamid\b|$))",
                     re.I)
_FROM = re.compile(r"\bfrom\s+(?P<who>[A-Z][^,;]+?)(?=\s*(?:,|;|\bat\b|\bto\b|\bfor\b|$))")


@dataclass
class Event:
    kind: str
    company: str
    headline: str
    #: The verb phrase that matched ("raises Rs 22 crore in pre-Series A").
    evidence: str = ""
    amount: Amount | None = None
    round: str | None = None
    investors: str | None = None
    person: str | None = None
    role: str | None = None
    #: Acquisition target (or acquirer, for "acquired by"), partner or vendor.
    other: str | None = None
    #: What makes two stories about it the same event (with the kind and the month).
    detail: str = ""
    places: list[str] = field(default_factory=list)
    #: Set when AI read the headline (``signal_interpretation``): "provider/model".
    interpreted_by: str = ""
    #: AI's one-line summary of the item, when AI read it.
    summary: str = ""

    @property
    def title(self) -> str:
        return TITLES.get(self.kind, self.kind)

    @property
    def tech_role(self) -> bool:
        return bool(self.role and _TECH_ROLE.search(self.role))


def _clean(headline: str) -> str:
    text = " ".join((headline or "").replace(" ", " ").split())
    match = _PREFIX.match(text)
    if match and len(match.group(0).split()) <= 5:
        text = text[match.end():]
    return text.strip(" \"'“”")


def _name_like(token: str) -> bool:
    bare = token.strip("\"'“”‘’()[],")
    if not bare or not bare[0].isalnum() or not any(ch.isalpha() for ch in bare):
        return False
    return bare[0].isupper() or bare[0].isdigit() or any(ch.isupper() for ch in bare[1:])


def subject_company(prefix: str, places: Iterable[str] = ()) -> str | None:
    """The company named last before the verb, or None.

    Walks back from the verb over name-like words, stopping at a descriptor
    ("startup"), a "-based"/"-backed" word, a possessive ("Bengaluru's") or a
    plain lowercase word.
    """
    taken: list[str] = []
    for token in reversed(prefix.split()):
        word = token.strip("\"'“”‘’(),")
        low = word.casefold()
        if not word:
            break
        if low in DESCRIPTORS or re.search(r"-(?:based|backed|headquartered|led|owned)$", low) \
                or low in ("based", "backed", "headquartered"):
            break
        if _POSSESSIVE.search(word) and taken:
            break
        if low in _CONNECTORS and taken:
            taken.append(word)
            continue
        if not _name_like(word):
            break
        taken.append(word)
        if len(taken) > _MAX_NAME_WORDS + 2:
            return None
    while taken and taken[-1].casefold() in _CONNECTORS:
        taken.pop()
    while taken and taken[0].casefold() in _CONNECTORS:
        taken.pop(0)
    words = list(reversed(taken))
    place_words = {p.casefold() for p in places if p}
    while len(words) > 1 and _POSSESSIVE.sub("", words[0]).casefold() in place_words:
        words.pop(0)                                  # "Pune Acme" -> "Acme"
    if not words or len(words) > _MAX_NAME_WORDS:
        return None
    name = _POSSESSIVE.sub("", " ".join(words)).strip(" -–,:")
    if len(name) < 2 or any(w.casefold() in NOT_COMPANIES for w in name.split()):
        return None
    if name.casefold() in place_words:
        return None
    return name


def _lead_words(text: str, n: int = 3) -> str:
    words = re.findall(r"[\w&+.-]+", (text or "").casefold())
    filler = {"a", "an", "the", "its", "new", "first", "in", "at", "to", "for", "of", "with",
              "and", "on"}
    return " ".join([w for w in words if w not in filler][:n])


def _who(rest: str) -> str | None:
    match = _LED_BY.search(rest) or _FROM.search(rest)
    if match is None:
        return None
    who = " ".join(match.group("who").split()).strip(" .")
    return who[:200] or None


def _other(rest: str) -> str | None:
    """The name right after the verb ("acquires Beta Labs for $20M" -> "Beta Labs")."""
    words = []
    for token in rest.split():
        word = token.strip("\"'“”(),.:;")
        low = word.casefold()
        if not word or low in ("for", "to", "in", "at") or \
                (not _name_like(word) and low not in _CONNECTORS):
            break
        words.append(word)
    while words and words[-1].casefold() in _CONNECTORS:
        words.pop()
    return " ".join(words[:_MAX_NAME_WORDS]) or None


#: Verbs that are also words of names ("Monsoon Bags", "Green Lands"): in a headline they
#: lose to a plain verb when both match.
_SECONDARY = frozenset({"bag", "bags", "bagged", "land", "lands", "landed", "net", "nets",
                        "netted", "gets", "got", "close", "closes", "closed", "picks", "names",
                        "named", "taps", "enters"})


def _title_case(text: str) -> bool:
    words = [w for w in re.findall(r"[A-Za-z][\w'’-]*", text) if len(w) > 3]
    return bool(words) and sum(w[0].isupper() for w in words) >= 0.7 * len(words)


def _find(pattern: re.Pattern, text: str) -> re.Match | None:
    """The verb match that reads as the verb, not as part of the company's name.

    In a sentence-case headline a capitalised "verb" belongs to a name ("brand
    Monsoon Bags raises…"); in Title Case, a plain verb ("Raises") beats one that
    could be a name word ("Bags").
    """
    found: list[re.Match] = []
    position = 0
    while position <= len(text):
        match = pattern.search(text, position)
        if match is None:
            break
        found.append(match)
        position = match.start("verb") + 1
    titled = _title_case(text)
    plain = [m for m in found if titled or not m.group("verb")[:1].isupper()]
    if not plain:
        return None
    primary = [m for m in plain if m.group("verb").split()[0].casefold() not in _SECONDARY]
    return (primary or plain)[0]


def classify(headline: str, *, issuer: str = "", kinds: Iterable[str] = TITLES,
             places: Iterable[str] = ()) -> Event | None:
    """The event a headline reports, about which company; None when it reports none."""
    text = _clean(headline)
    wanted = set(kinds)
    places = [p for p in places if p]
    if not text:
        return None

    def company_before(match: re.Match) -> str | None:
        if issuer:
            return issuer.strip()
        return subject_company(text[:match.start()], places)

    # Order matters: a named appointment before "names new CTO"; "acquired by" before
    # "acquires"; funding before expansion ("raises … to expand"); "opens new office"
    # (expansion) before "launches" (launch).
    if "leadership_change" in wanted:
        match = JOINS.match(text)
        if match is not None:
            company = issuer or subject_company(match.group("company"), places)
            role = " ".join(match.group("role").split())
            if company and _LEAD_ROLE.search(role):
                return Event("leadership_change", company, text, match.group(0)[:200],
                             person=match.group("person"), role=role, detail=role.casefold())
        for pattern in (APPOINTS, NAMES_NEW):
            match = _find(pattern, text)
            if match is None:
                continue
            company = company_before(match)
            role = " ".join(match.group("role").split()).strip(" ,")
            if company and _LEAD_ROLE.search(role):
                return Event("leadership_change", company, text,
                             text[match.start("verb"):][:200],
                             person=match.groupdict().get("person"), role=role,
                             detail=role.casefold())
    if "acquisition" in wanted:
        for pattern, passive in ((ACQUIRED_BY, True), (ACQUISITION_VERB, False)):
            match = _find(pattern, text)
            if match is None:
                continue
            company = company_before(match)
            other = _other(match.group("rest"))
            if company and other and other.casefold() != company.casefold():
                return Event("acquisition", company, text, text[match.start("verb"):][:200],
                             amount=parse_amount(match.group("rest")),
                             other=f"{'acquired by ' if passive else ''}{other}",
                             detail=_lead_words(other, 3))
    if "funding" in wanted:
        match = _find(FUNDING_VERB, text)
        rest = match.group("rest") if match else ""
        if match is not None and not _FUNDING_NOT.search(rest) and \
                _FUNDING_OBJECT.search(" ".join(rest.split()[:10])):
            company = company_before(match)
            if company:
                amount = parse_amount(rest)
                stage = parse_round(rest)
                detail = stage.casefold() if stage else (
                    f"{amount.currency}{float(f'{amount.value:.2g}'):.0f}" if amount else "")
                return Event("funding", company, text, text[match.start("verb"):][:200],
                             amount=amount, round=stage, investors=_who(rest), detail=detail)
    if "expansion" in wanted:
        match = _find(EXPANSION_VERB, text)
        if match is not None:
            company = company_before(match)
            if company:
                named = mentions(text[match.start("verb"):], places)
                return Event("expansion", company, text, text[match.start("verb"):][:200],
                             detail=named[0].casefold() if named
                             else _lead_words(match.group("rest"), 2), places=named)
    if "launch" in wanted:
        match = _find(LAUNCH_VERB, text)
        if match is not None and not _LAUNCH_NOT.search(match.group("rest")):
            company = company_before(match)
            if company:
                return Event("launch", company, text, text[match.start("verb"):][:200],
                             detail=_lead_words(match.group("rest"), 3))
    if "digital_transformation" in wanted:
        match = _find(TRANSFORMATION_VERB, text)
        tech = _TECH.search(text[match.start("verb"):]) if match is not None else None
        if match is not None and tech is not None:
            company = company_before(match)
            if company:
                partner = _other(match.group("rest"))
                return Event("digital_transformation", company, text,
                             text[match.start("verb"):][:200], other=partner,
                             detail=(partner or tech.group(0)).casefold()[:60])
    return None


def mentions(text: str, names: Iterable[str]) -> list[str]:
    """Which of ``names`` the text names as whole words (case-folded; "Pune-based" counts)."""
    folded = (text or "").casefold()
    return [n for n in names
            if n and re.search(rf"(?<!\w){re.escape(n.casefold())}(?!\w)", folded)]
