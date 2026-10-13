"""CPPP (India's Central Public Procurement Portal): the latest active tenders list.

``GET https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata`` is a public HTML
table, ten tenders a page, newest first, with a "Next" link: published date, closing
date, opening date, title / reference / tender id (linking the tender's full view)
and the organisation. No API, no codes, no values, no buyer contacts; the lead
type matches titles by keyword. The keyword search on the site sits behind a
CAPTCHA, so only the plain list is read (never the search).

Terms (verified 2026-10-13): no robots.txt (404); the portal's copyright policy says
its material "maybe reproduced free of charge after taking proper permission from the
respective Organisation / Department". **Off by default**: reading the list for our
own leads is the owner's call.
"""

from __future__ import annotations

from datetime import timedelta, timezone
from urllib.parse import urljoin

from ...sources.tenders.base import Buyer, Tender, clean, parse_time

#: The portal writes Indian Standard Time without a zone.
IST = timezone(timedelta(hours=5, minutes=30), "IST")
HOST = "eprocure.gov.in"
LIST = "https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata"


def parse_page(http, html: str, base: str = LIST) -> tuple[list[Tender], str | None]:
    """(the page's tenders, the next page's URL)."""
    soup = http.soup(html)
    table = soup.find("table", id="table")
    out: list[Tender] = []
    for row in (table.find_all("tr") if table is not None else []):
        cells = row.find_all("td")
        if len(cells) < 6:
            continue
        link = cells[4].find("a")
        title = clean(link.get_text(" ", strip=True) if link else "", 500)
        whole = clean(cells[4].get_text(" ", strip=True), 800)
        tender_id = whole.rsplit("/", 1)[-1].strip() if "/" in whole else ""
        organisation = clean(cells[5].get_text(" ", strip=True), 300)
        href = link.get("href") if link else None
        if not organisation or not (tender_id or title):
            continue
        # "<title>/<reference>/<tender id>": the reference is what sits between.
        reference = whole[len(title):].strip(" /") if whole.startswith(title) else ""
        if tender_id and reference.endswith(tender_id):
            reference = reference[:-len(tender_id)].strip(" /")
        out.append(Tender(
            source="cppp", notice_id=tender_id or title[:120], title=title or tender_id,
            url=urljoin(base, href) if href else LIST,
            buyer=Buyer(name=organisation, country="IN"),
            description=f"Ref. {reference}" if reference else "",
            published_at=parse_time(clean(cells[1].get_text(" ", strip=True)), zone=IST),
            deadline=parse_time(clean(cells[2].get_text(" ", strip=True)), end_of_day=True,
                                zone=IST),
            notice_type="Active tender"))
    following = next((a.get("href") for a in soup.find_all("a")
                      if "next" in a.get_text(" ", strip=True).lower() and a.get("href")), None)
    return out, urljoin(base, following) if following else None


class CpppTenders:
    name = "cppp"
    title = "CPPP (India Central Public Procurement Portal)"
    host = HOST
    applicable_countries = frozenset({"IN"})
    codes = None
    needs_key = False
    #: Off unless a config enables it (its copyright policy asks for permission).
    default_enabled = False

    def search(self, http, query, *, key: str | None = None) -> list[Tender]:
        """The newest active tenders, ``query.max_pages`` pages of ten (the lead type keeps
        the ones whose titles carry its keywords). Stops at tenders older than
        ``query.posted_from``."""
        url: str | None = LIST
        out: list[Tender] = []
        for _ in range(max(1, query.max_pages)):
            if url is None:
                break
            page, url = parse_page(http, http.get(url, rate_key=HOST).text)
            out.extend(page)
            if query.posted_from is not None and page and all(
                    t.published_at is not None and t.published_at < query.posted_from
                    for t in page):
                break
        return out
