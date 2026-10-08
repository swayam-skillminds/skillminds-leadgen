"""People on a company's own about / team / leadership pages.

What a page publishes, read four ways, most structured first:

1. **JSON-LD** ``Person`` objects (on their own, or as an Organization's
   ``founder`` / ``employee`` / ``member``) with a name and a ``jobTitle``;
2. **microdata** (``itemtype=…schema.org/Person`` with ``name`` / ``jobTitle``);
3. **cards**: a short text that is a person's name next to one that is a
   decision-maker's title, inside the same small block ("Priya Shah" /
   "Co-founder & CTO"), or both in one line ("Priya Shah, CTO");
4. **prose**: "founded by Priya Shah", "Priya Shah, our founder".

Navigation, header, footer, forms and scripts are ignored (``people_on``
removes them from the soup: read ``emails_on`` first). An email or LinkedIn
link published *inside* a person's card is theirs. Nothing is guessed: a name
without a title isn't a decision-maker, and a title without a name is nobody.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

from ...core.normalize import domain_of, extract_linkedin
from .roles import family_of, is_role_mailbox, person_name

#: (pattern, rank): team / leadership pages first, then about pages. A link counts when
#: its last path segment or its text *is* such a name ("/about/team", "Our Leadership"),
#: not when the words merely occur in it ("/services/development-and-management").
_TEAM_LINKS = (
    (re.compile(r"(?:our|the|meet|meet[-_ ]the|key)?[-_ ]?(?:leadership(?:[-_ ]team)?|"
                r"management(?:[-_ ]team)?|team|founders?|people|board(?:[-_ ]of[-_ ]"
                r"directors)?|directors|executives?|leaders)(?:[-_ ]us)?", re.I), 0),
    (re.compile(r"(?:about(?:[-_ ]us)?|who[-_ ]we[-_ ]are|our[-_ ]story|company(?:[-_ ]"
                r"(?:overview|profile))?|meet[-_ ]us|about[-_ ]the[-_ ]company)", re.I), 1),
)
_CHROME = ["nav", "header", "footer", "aside", "form", "script", "style", "noscript",
           "template", "svg", "iframe"]
_TEXT_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "span", "div", "li", "strong", "b",
              "em", "td", "dt", "dd", "figcaption", "a", "small", "label"]
#: Blocks: a tag holding one of these with text of its own is several lines, not one.
_BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "li", "td", "dt", "dd",
               "figcaption", "section", "article", "ul", "ol", "table", "tr"]
_SPLIT = re.compile(r"\s*(?:,|\||–|—|\s-\s|:)\s*")
_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
#: A name word: capitalised, no full stop inside (that ends a sentence), or an initial "R.".
_WORD = r"(?:[A-Z]\.|[A-Z][\w'’-]+)"
_NAME = rf"{_WORD}(?:\s+{_WORD}){{1,3}}"
_FOUNDED_BY = re.compile(
    r"\b(?:founded|co-founded|started|established)\s+(?:in\s+\d{4}\s+)?by\s+"
    rf"(?P<names>{_NAME}(?:\s*(?:,|and|&)\s*{_NAME})*)")
_NAME_COMMA_TITLE = re.compile(
    rf"(?P<name>{_NAME}),\s+(?i:our|the)\s+"
    r"(?P<title>(?i:(?:co-?\s?founder|founder|ceo|managing director|cto|chief [a-z]+ officer)"
    r"(?:\s*(?:&|and)\s*(?:ceo|cto|managing director|md))?))\b")


@dataclass
class Found:
    name: str
    title: str
    page: str
    #: jsonld | microdata | card | prose
    how: str
    email: str | None = None
    profile_url: str | None = None


def team_links(soup, base: str, limit: int = 3) -> list[str]:
    """Same-site links to team / leadership pages, then about pages, best first."""
    home = domain_of(base)
    ranked: dict[str, int] = {}
    for anchor in soup.find_all("a", href=True):
        url = urljoin(base, anchor["href"]).split("#", 1)[0]
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or domain_of(url) != home or \
                url.rstrip("/") == base.rstrip("/"):
            continue
        segment = parts.path.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        label = " ".join(anchor.get_text(" ", strip=True).split())
        for pattern, rank in _TEAM_LINKS:
            if pattern.fullmatch(segment) or pattern.fullmatch(label):
                ranked[url] = min(rank, ranked.get(url, rank))
                break
    return [u for u, _ in sorted(ranked.items(), key=lambda kv: kv[1])][:limit]


def _jsonld_people(soup) -> Iterable[tuple[str, str, str | None]]:
    def walk(node: Any):
        if isinstance(node, list):
            for item in node:
                yield from walk(item)
        elif isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "Person" in kinds and node.get("name"):
                same = node.get("sameAs")
                links = same if isinstance(same, list) else [same] if same else []
                yield (str(node.get("name")), str(node.get("jobTitle") or ""),
                       next((str(u) for u in links if "linkedin.com/in/" in str(u)), None))
            for key in ("@graph", "founder", "founders", "employee", "employees", "member",
                        "members", "itemListElement", "item"):
                if key in node:
                    yield from walk(node[key])
    for script in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            yield from walk(json.loads(script.string or script.get_text() or "null"))
        except (ValueError, TypeError):
            continue


def _microdata_people(soup) -> Iterable[tuple[str, str]]:
    for node in soup.find_all(attrs={"itemtype": re.compile(r"schema\.org/Person", re.I)}):
        name = node.find(attrs={"itemprop": "name"})
        title = node.find(attrs={"itemprop": "jobTitle"})
        if name is not None:
            yield (name.get_text(" ", strip=True),
                   title.get_text(" ", strip=True) if title is not None else "")


def _own_text(tag) -> str:
    """The tag's text when it is a leaf-ish block (no nested block holding the same text)."""
    text = " ".join(tag.get_text(" ", strip=True).split())
    if not text or len(text) > 120:
        return ""
    for child in tag.find_all(_TEXT_TAGS, recursive=False):
        if " ".join(child.get_text(" ", strip=True).split()) == text:
            return ""                      # the child says it; read it there
    if tag.name not in ("a", "span", "strong", "b", "em", "small", "label") and any(
            child.get_text(strip=True) for child in tag.find_all(_BLOCK_TAGS)):
        return ""                          # several lines: read each one
    return text


def _line_text(tag) -> str:
    """A one-line card's text without its links' labels ("Priya Shah, CEO Email" -> "Priya
    Shah, CEO")."""
    parts = [t for t in tag.find_all(string=True)
             if tag.name == "a" or t.find_parent("a") is None]
    return " ".join(" ".join(parts).split())


def _card(tag, other, levels: int = 4):
    """The smallest ancestor of ``tag`` (within ``levels``) that also holds ``other``."""
    node = tag
    for _ in range(levels):
        node = node.parent
        if node is None:
            return None
        if other is tag or other in node.descendants:
            return node
    return None


def _names_someone(text: str) -> bool:
    """A line that is, or starts with, a person's name ("Sanjay Kulkarni, IT Manager")."""
    if person_name(text):
        return True
    parts = [p for p in _SPLIT.split(text, maxsplit=1) if p]
    return len(parts) == 2 and person_name(parts[0]) is not None


def _one_person(card) -> bool:
    """A card is one person's: it holds a single name (a list of people isn't a card)."""
    names = {person_name(text) for text in (_own_text(t) for t in card.find_all(_TEXT_TAGS))
             if text and _names_someone(text)}
    return len(names - {None}) <= 1 and \
        sum(1 for t in card.find_all(_TEXT_TAGS) if _names_someone(_own_text(t))) <= 1


def _contacts(card) -> tuple[str | None, str | None]:
    """The email and LinkedIn profile a person's card links, if any."""
    email = profile = None
    if card is None:
        return None, None
    for anchor in card.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.lower().startswith("mailto:") and email is None:
            address = href[7:].split("?", 1)[0].strip().lower()
            if _EMAIL.fullmatch(address) and not is_role_mailbox(address):
                email = address                # sales@ / info@ are nobody's own
        elif "linkedin.com/in/" in href and profile is None:
            profile = extract_linkedin(href)
    return email, profile


def _about(sentence: str, company: str | None) -> bool:
    """Is "… founded by X" about this company (not a customer: "Google, which was founded
    by …")? Without a company name, any sentence counts."""
    if not company:
        return True
    lowered = re.split(r"[,;:–—]", sentence)[-1].casefold()   # the clause right before
    words = [w for w in re.findall(r"[a-z0-9]+", company.casefold()) if len(w) > 2
             and w not in ("the", "and", "pvt", "ltd", "limited", "private", "llp", "inc")]
    return bool(re.search(r"\b(?:we|our company|the company|it)\b", lowered)) or \
        any(w in lowered for w in words[:2])


def people_on(soup, page: str, company: str | None = None) -> list[Found]:
    """The people a page publishes, with titles; deduplicated by name, in page order.

    ``company``: whose page it is, so "founded by" prose about someone else (a client,
    a partner) doesn't count. Mutates ``soup``: page chrome (navigation, footer,
    forms…) is removed first.
    """
    found: dict[str, Found] = {}

    def add(name: str | None, title: str, how: str, email=None, profile=None) -> None:
        clean = person_name(name or "")
        title = " ".join((title or "").split()).strip(" ,;:|-–—")
        if not clean or not title or family_of(title) is None:
            return
        held = found.get(clean.casefold())
        if held is None:
            found[clean.casefold()] = Found(clean, title[:200], page, how, email, profile)
        else:
            held.email = held.email or email
            held.profile_url = held.profile_url or profile

    for name, title, profile in _jsonld_people(soup):
        add(name, title, "jsonld", profile=profile)
    for name, title in _microdata_people(soup):
        add(name, title, "microdata")
    for tag in soup.find_all(_CHROME):
        tag.decompose()
    blocks = [(tag, _own_text(tag)) for tag in soup.find_all(_TEXT_TAGS)]
    blocks = [(tag, text) for tag, text in blocks if text]
    for index, (tag, text) in enumerate(blocks):
        name = person_name(text)
        if name is None:
            # "Priya Shah, Co-founder & CTO" / "Priya Shah | CTO" in one line: its contacts
            # are the ones inside that line, never its neighbours'.
            parts = [p for p in _SPLIT.split(_line_text(tag), maxsplit=1) if p]
            if len(parts) == 2 and person_name(parts[0]) and family_of(parts[1]):
                add(parts[0], parts[1], "card", *_contacts(tag))
            continue
        for other, title in blocks[index + 1:index + 4]:
            if _names_someone(title):
                break                      # the next person's card (or line)
            if family_of(title) is None:
                continue
            card = _card(tag, other)
            if card is not None and len(card.get_text(" ", strip=True)) <= 600 and \
                    _one_person(card):
                add(name, title, "card", *_contacts(card))
                break
    text = " ".join(soup.get_text(" ").split())
    for match in _FOUNDED_BY.finditer(text):
        if not _about(re.split(r"(?<=[.!?])\s", text[:match.start()])[-1], company):
            continue
        for name in re.split(r"\s*(?:,|\band\b|&)\s*", match.group("names")):
            add(name, "Founder", "prose")
    for match in _NAME_COMMA_TITLE.finditer(text):
        add(match.group("name"), match.group("title"), "prose")
    return list(found.values())


def emails_on(soup) -> list[str]:
    """Every address the page publishes as a mailto link or in its text (the email pattern)."""
    out = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.lower().startswith("mailto:"):
            address = href[7:].split("?", 1)[0].strip().lower()
            if _EMAIL.fullmatch(address):
                out.append(address)
    for script in soup.find_all(["script", "style"]):
        script.extract()
    out.extend(m.group(0).lower().rstrip(".") for m in _EMAIL.finditer(soup.get_text(" ")))
    return list(dict.fromkeys(out))
