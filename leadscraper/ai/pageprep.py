"""Token-lean page preparation (Phase 5.6), before any extraction call.

Raw HTML is mostly scripts, styles, navigation, tracking and markup. A model
needs the words, the contact links and the business's own structured data:

* drop ``script`` / ``style`` / ``svg`` / ``iframe`` / forms' inputs, navigation
  (``nav``, ``role=navigation``) and boilerplate blocks (cookie banners, modals,
  newsletter pop-ups, breadcrumbs, share bars) -- but keep headers and footers,
  where businesses put their phone numbers;
* convert what is left to compact Markdown: headings, paragraphs, list items,
  table rows; a link keeps its target only when the target carries information
  (``mailto:``, ``tel:``, WhatsApp, social profiles, other sites);
* keep the page's business JSON-LD (name, telephone, email, address, url,
  sameAs) as one compact line;
* drop repeated lines (menus and footers often appear twice);
* split the result into chunks that fit the model's context.

:attr:`PreparedPage.source_text` keeps the *original* page text plus every
href, unshortened: that is what the grounding check (``ai.grounding``) holds AI
answers against.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

from .prices import estimate_tokens

_DROP_TAGS = ("script", "style", "noscript", "svg", "template", "iframe", "canvas",
              "object", "embed", "picture", "video", "audio", "map", "button", "select",
              "input", "textarea", "link", "meta")
_BOILERPLATE = re.compile(
    r"cookie|consent|gdpr|popup|pop-up|modal|newsletter|subscribe|breadcrumb|share-?bar|"
    r"social-?share|skip-?link|navbar|nav-menu|menu-toggle|offcanvas|search-form|"
    r"back-to-top|ad-slot|advert", re.I)
_BLOCKS = {"p", "div", "section", "article", "main", "header", "footer", "aside", "address",
           "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "dd", "dt", "blockquote", "figcaption",
           "form", "fieldset", "ul", "ol", "table", "tbody", "thead", "dl", "br", "hr", "pre"}
_INFORMATIVE = re.compile(r"^(mailto:|tel:|sms:|https?://(wa\.me|api\.whatsapp|(www\.)?"
                          r"(instagram|facebook|linkedin|twitter|x|youtube)\.com))", re.I)
_JSONLD_KEYS = ("@type", "name", "telephone", "email", "address", "url", "sameAs",
                "streetAddress", "addressLocality", "addressRegion", "postalCode",
                "addressCountry", "openingHours", "priceRange")
_SPACES = re.compile(r"[ \t ]+")


@dataclass
class PreparedPage:
    markdown: str
    chunks: list[str] = field(default_factory=list)
    #: Original visible text + every href + JSON-LD: what answers are grounded in.
    source_text: str = ""
    title: str = ""
    raw_tokens: int = 0

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.markdown)

    @property
    def saving(self) -> float:
        """Fraction of the raw HTML's tokens saved (0.0 – 1.0)."""
        return 1.0 - self.tokens / self.raw_tokens if self.raw_tokens else 0.0


def _class_text(tag: Tag) -> str:
    attrs = getattr(tag, "attrs", None) or {}
    classes = attrs.get("class") or []
    if isinstance(classes, str):
        classes = [classes]
    return " ".join([*classes, str(attrs.get("id") or ""), str(attrs.get("role") or ""),
                     str(attrs.get("aria-label") or "")])


_SHARER = re.compile(r"sharer|/share\b|intent/tweet|shareArticle|/send\?", re.I)


def _carries_contacts(tag: Tag) -> bool:
    """A block holding the business's own contact links (tel:, mailto:, its profiles)."""
    for link in tag.find_all("a", href=True):
        href = str(link["href"])
        if _INFORMATIVE.match(href) and not _SHARER.search(href):
            return True
    return False


def _is_boilerplate(tag: Tag) -> bool:
    attrs = getattr(tag, "attrs", None) or {}
    if attrs.get("aria-hidden") == "true" or attrs.get("hidden") is not None:
        return True
    style = str(attrs.get("style") or "").replace(" ", "").lower()
    if "display:none" in style:
        return True
    boilerplate = (tag.name == "nav" or attrs.get("role") == "navigation"
                   or bool(_BOILERPLATE.search(_class_text(tag))))
    # A menu or "follow us" bar that links the business's phone, email or
    # profiles is kept: those links are what extraction is for.
    return boilerplate and not _carries_contacts(tag)


def _jsonld(soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, TypeError):
            continue
        nodes = data if isinstance(data, list) else [data]
        stack = list(nodes)
        while stack:
            node = stack.pop(0)
            if isinstance(node, dict):
                if "@graph" in node:
                    stack.extend(node["@graph"] if isinstance(node["@graph"], list) else [])
                    continue
                kept = _keep_jsonld(node)
                if kept and any(k in kept for k in ("telephone", "email", "address")):
                    out.append(json.dumps(kept, ensure_ascii=False, separators=(",", ":")))
    return out


def _keep_jsonld(node):
    if isinstance(node, dict):
        kept = {k: _keep_jsonld(v) for k, v in node.items() if k in _JSONLD_KEYS}
        return {k: v for k, v in kept.items() if v not in (None, "", [], {})}
    if isinstance(node, list):
        return [_keep_jsonld(v) for v in node][:10]
    return node


def _link(tag: Tag, base_url: str | None, text: str) -> str:
    href = str(tag.get("href") or "").strip()
    if not href or href.startswith(("#", "javascript:")):
        return text
    if base_url and not href.startswith(("mailto:", "tel:", "sms:")):
        href = urljoin(base_url, href)
    informative = bool(_INFORMATIVE.match(href))
    if not informative and base_url and href.startswith("http"):
        informative = urlparse(href).netloc.lower() != urlparse(base_url).netloc.lower()
    if not informative:
        return text
    target = href.removeprefix("mailto:").removeprefix("tel:")
    if not text or text.strip().lower() == target.strip().lower():
        return f"<{href}>"
    return f"[{text}]({href})"


def _render(node, base_url: str | None, out: list[str], line: list[str]) -> None:
    for child in node.children:
        if isinstance(child, NavigableString):
            if child.__class__.__name__ in ("Comment", "Doctype", "ProcessingInstruction",
                                            "CData", "Declaration"):
                continue
            text = _SPACES.sub(" ", str(child))
            if text.strip():
                line.append(text)
            elif line and not line[-1].endswith(" "):
                line.append(" ")
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name
        if name in _DROP_TAGS or _is_boilerplate(child):
            continue
        if name == "img":
            continue
        if name == "a":
            inner: list[str] = []
            _render(child, base_url, [], inner)
            line.append(_link(child, base_url, "".join(inner).strip()))
            continue
        block = name in _BLOCKS
        if block:
            _flush(out, line)
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            inner = []
            _render(child, base_url, [], inner)
            heading = "".join(inner).strip()
            if heading:
                out.append("#" * int(name[1]) + " " + heading)
            continue
        if name == "li":
            inner = []
            sub: list[str] = []
            _render(child, base_url, sub, inner)
            item = "".join(inner).strip()
            if item:
                out.append("- " + item)
            out.extend(sub)
            continue
        if name == "tr":
            cells = []
            for cell in child.find_all(("td", "th"), recursive=False):
                inner = []
                _render(cell, base_url, [], inner)
                cells.append(" ".join("".join(inner).split()))
            if any(cells):
                out.append(" | ".join(cells))
            continue
        _render(child, base_url, out, line)
        if block:
            _flush(out, line)


def _flush(out: list[str], line: list[str]) -> None:
    text = " ".join("".join(line).split())
    line.clear()
    if text:
        out.append(text)


def prepare(html: str, url: str | None = None, *, chunk_tokens: int = 3000) -> PreparedPage:
    """`html` as compact Markdown, chunked, plus the text to ground answers in."""
    html = html or ""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    jsonld = _jsonld(soup)

    hrefs = [str(a.get("href")) for a in soup.find_all("a") if a.get("href")]
    full = BeautifulSoup(html, "lxml")
    for tag in full(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    source_text = " ".join([full.get_text(" ", strip=True), *hrefs, *jsonld])

    body = soup.body or soup
    lines: list[str] = []
    pending: list[str] = []
    _render(body, url, lines, pending)
    _flush(lines, pending)

    seen: set[str] = set()
    kept: list[str] = []
    if title:
        kept.append(f"Title: {title}")
        seen.add(title)
    for line in lines:
        key = line.strip()
        if not key or (key in seen and not key.startswith("#")):
            continue
        seen.add(key)
        kept.append(key)
    if jsonld:
        kept.append("Structured data: " + " ".join(jsonld))
    markdown = "\n".join(kept)
    return PreparedPage(markdown=markdown, chunks=chunk(markdown, chunk_tokens),
                        source_text=source_text, title=title,
                        raw_tokens=estimate_tokens(html))


def chunk(markdown: str, max_tokens: int = 3000) -> list[str]:
    """Split at line boundaries (headings preferred) into pieces under `max_tokens`."""
    if estimate_tokens(markdown) <= max_tokens:
        return [markdown] if markdown else []
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in markdown.split("\n"):
        tokens = estimate_tokens(line) + 1
        heading = line.startswith("#")
        if current and (size + tokens > max_tokens or (heading and size > max_tokens * 0.6)):
            chunks.append("\n".join(current))
            current, size = [], 0
        while tokens > max_tokens:              # one enormous line: hard-split it
            cut = max_tokens * 4
            chunks.append(line[:cut])
            line = line[cut:]
            tokens = estimate_tokens(line) + 1
        current.append(line)
        size += tokens
    if current:
        chunks.append("\n".join(current))
    return chunks
