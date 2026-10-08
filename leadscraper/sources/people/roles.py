"""Who counts as a decision-maker, and what a person's name looks like.

Titles fall into families: **owner** (founder, owner, partner, chairman),
**executive** (CEO, MD, director, GM, COO, CFO, president), **technology**
(CTO, CIO, chief digital officer, heads and VPs of engineering, technology,
IT or digital) and **it** (IT manager, IT head, systems manager). Advisors,
investors, mentors, interns and non-executive or independent directors are
nobody's buyer and are left out.
"""

from __future__ import annotations

import re
import unicodedata
from types import SimpleNamespace

FAMILIES = ("owner", "executive", "technology", "it")
FAMILY_TITLES = {"owner": "Owner / founder", "executive": "Executive",
                 "technology": "Technology lead", "it": "IT"}

_NOT_BUYERS = re.compile(r"\b(?:advis[eo]r[sy]?|advisory|investor|mentor|intern|trainee|"
                         r"non[- ]executive|independent director|board member|volunteer|"
                         r"student|alumni|ambassador|assistant to|"
                         # titles that only look like an owner's or an executive's
                         r"product owner|process owner|channel partner|delivery partner|"
                         r"implementation partner|partner \w+ (?:manager|executive|lead)|"
                         r"partner success|founder'?s office|(?:assistant|associate|deputy) "
                         r"director|(?:art|creative|music|casting|film) director|"
                         r"director of photography|instructor|trainer|teacher|"
                         # no longer in the role
                         r"former|ex-|ex\s|retired|emeritus|late)\b", re.I)
_PATTERNS = (
    ("technology", re.compile(
        r"\b(?:cto|cio|cdo|cdio|ciso|chief (?:technology|technical|information|digital|data|"
        r"product|information security) officer|(?:vp|vice[- ]president|head|director|svp|evp)"
        r"[\s,]+(?:of\s+|-\s*|–\s*)?(?:engineering|technology|tech|it\b|digital|data|"
        r"product|software|information technology|platform|infrastructure)|"
        r"(?:engineering|technology|technical|digital) (?:head|director|lead)|"
        r"technical director)\b", re.I)),
    ("it", re.compile(
        r"\b(?:it|information technology|systems|mis|erp)\s*(?:-\s*)?(?:manager|head|lead|"
        r"in-?charge|director|administrator)|(?:head|manager)\s*(?:of\s+|-\s*|–\s*)?(?:it|"
        r"information technology|systems|mis)\b", re.I)),
    ("owner", re.compile(
        r"\b(?:co-?\s?founder|founder|founding partner|owner|proprietor|propriter|"
        r"managing partner|partner|chairman|chairperson|chairwoman|chair|"
        r"llp (?:designated )?member)\b|^\s*principal\s*$", re.I)),
    ("executive", re.compile(
        r"\b(?:ceo|coo|cfo|cmo|cbo|md|chief (?:executive|operating|financial|marketing|"
        r"business|commercial|strategy) officer|managing director|executive director|"
        r"whole[- ]time director|director|general manager|gm|president|vice[- ]president|"
        r"vp|country (?:head|manager)|business head|head of (?:operations|business))\b", re.I)),
)


#: Words of marketing copy, never of a job title ("Extensive Partner Ecosystem").
_NOT_TITLES = re.compile(r"\b(?:our|your|we|explore|discover|learn|ecosystem|expertise|"
                         r"offerings?|services|solutions|industries|programs?|network|"
                         r"benefits|opportunit(?:y|ies)|click|read|view|more)\b", re.I)


def family_of(title: str) -> str | None:
    """The decision-maker family a job title belongs to, or None (not a decision-maker).

    A title is short (at most 12 words) and isn't marketing copy; "Principal" alone is
    an owner, a "Principal Software Architect" an engineer.
    """
    title = " ".join((title or "").split())
    if not title or len(title) > 120 or len(title.split()) > 12 or \
            _NOT_BUYERS.search(title) or _NOT_TITLES.search(title):
        return None
    for family, pattern in _PATTERNS:
        if pattern.search(title):
            return family
    return None


def rank(title: str) -> int:
    """Sort key: owners and chief executives first, then technology, IT, the rest."""
    family = family_of(title)
    if family == "owner":
        return 0
    if family == "executive" and re.search(r"\b(?:ceo|md|managing director|chief executive)\b",
                                           title or "", re.I):
        return 0
    return {"technology": 1, "it": 2, "executive": 3}.get(family or "", 9)


_HONORIFIC = re.compile(r"^(?:dr|mr|mrs|ms|miss|prof|er|ca|adv|shri|smt|sri)\.?\s+", re.I)
_PARTICLES = frozenset({"de", "da", "van", "von", "der", "bin", "binti", "al", "el", "la", "le",
                        "di", "du", "dos", "das"})
#: Words that make a short phrase a heading, a service or a company -- not a person.
_NOT_NAMES = frozenset("""
team our meet the about us leadership management board founders founder director directors
ceo cto cio md partner partners services service solutions solution contact read more view
profile linkedin email call home careers career blog news privacy policy terms copyright
rights reserved pvt ltd limited llp inc corp company group technologies technology systems
software digital studio labs india mumbai pune delhi bengaluru london new york welcome
customer customers clients client testimonial testimonials vision mission values story
office offices address phone mobile follow join apply submit send message subscribe login
sign up get started learn explore discover why what how who when where gallery portfolio
projects project products product pricing faq faqs support help manager head chief officer
executive engineering marketing sales operations finance human resources hr president
co-founder cofounder chairman chairperson owner proprietor principal vp svp evp cxo coo cfo
cmo gm ai assistant application applications development expertise extensive ecosystem deep
data cloud platform enterprise industry industries insights case studies study success global
offerings innovation consulting strategy design quality security network analytics automation
intelligence machine learning smart future value open source events press investor investors
relations governance sustainability culture life people partner partners overview awards
""".split())


#: Mailboxes of a role, not a person: they reveal no pattern, and
#: are never a person's own address.
ROLE_MAILBOXES = frozenset("""
info contact contacts contactus hello hi hey admin administrator support help helpdesk office
enquiry enquiries enquire inquiry inquiries mail email team hr careers career jobs job
recruitment talent accounts account billing finance invoice invoices payments marketing sales
service services booking bookings reservations reservation events press media pr news
newsletter noreply no-reply donotreply webmaster web website care customercare customer
customers feedback orders order shop store reception frontdesk general legal privacy
compliance security abuse partners partner business bd biz connect query queries
""".split())



def is_role_mailbox(address: str) -> bool:
    return (address or "").split("@", 1)[0].casefold() in ROLE_MAILBOXES


def _fold(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(ch))


def person_name(text: str) -> str | None:
    """``text`` as a person's name ("Priya Shah", "Dr. R. K. Menon"), or None.

    Two to four words, each capitalised (lower-case particles such as "de" or
    "van" between them are fine), not only initials, no digits, and none of
    the words a heading or company would use. ALL-CAPS names are title-cased.
    """
    text = " ".join((text or "").replace(" ", " ").split()).strip(" ,;:|-–—•·")
    if re.search(r"[^\W\d_]{2}\.$", text):
        text = text[:-1]                  # a sentence's full stop, not an initial's
    if not text or len(text) > 60 or any(ch.isdigit() for ch in text) or "@" in text:
        return None
    bare = _HONORIFIC.sub("", text)
    words = bare.split()
    if not 2 <= len(words) <= 4:
        return None
    if bare.isupper():
        words = [w.capitalize() if len(w.rstrip(".")) > 1 else w for w in words]
    if not bare.isupper() and any(len(w.strip(".,")) > 1 and w.strip(".,").isupper()
                                  for w in words):
        return None                       # an acronym ("CIB", "LLP") isn't part of a name
    initials = 0
    for index, word in enumerate(words):
        core = word.strip(".,'’")
        if not core:
            return None
        if core.casefold() in _PARTICLES and 0 < index < len(words) - 1:
            continue
        if core.casefold() in _NOT_NAMES:
            return None
        if not re.fullmatch(r"[^\W\d_](?:[^\W\d_]|['’.-])*", core) or not core[0].isupper():
            return None
        if len(core) == 1:
            initials += 1
    if initials >= len(words):
        return None                       # "R. K. M." is not enough of a name
    if len(words[-1].strip(".")) < 2:
        return None                       # a surname, not an initial, ends a name
    prefix = text[: len(text) - len(bare)]
    return (prefix + " ".join(words)).strip()


def name_parts(name: str) -> tuple[str, str] | None:
    """(first, last) in lower-case ASCII letters, honorifics and initials dropped, or None."""
    words = [re.sub(r"[^a-z]", "", _fold(w).casefold())
             for w in _HONORIFIC.sub("", name or "").split()]
    words = [w for w in words if len(w) > 1 and w not in _PARTICLES]
    if len(words) < 2:
        return None
    return words[0], words[-1]


def same_company(a: str, b: str) -> float:
    """0-100: how alike two company names are, as identity matching compares them."""
    from ...core.identity import name_similarity
    from ...core.normalize import name_key

    return name_similarity(SimpleNamespace(name_key=name_key(a or "")),
                           SimpleNamespace(name_key=name_key(b or "")))


_EMPLOYER = re.compile(r"(?:,|\bat\b|@)\s*(?P<org>[^,]+?)\s*$", re.I)


def employer_of(title: str) -> str | None:
    """The organisation a title names as the person's employer ("CIO, Rosendin" ->
    "Rosendin"; "CTO at Acme" -> "Acme"), or None. Testimonials on a vendor's site
    name their customers' executives this way."""
    match = _EMPLOYER.search(" ".join((title or "").split()))
    if match is None:
        return None
    org = match.group("org").strip(" .")
    if not org or family_of(org) is not None or not org[0].isupper():
        return None                       # "Founder, CEO" names two roles, not an employer
    return org
