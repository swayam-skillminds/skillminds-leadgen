"""Public ATS job-board APIs: Greenhouse, Lever, Ashby, Workable, SmartRecruiters.

Each is one company's published board, read through the JSON the ATS serves
for career sites (no key; verified live 2026-10-07, see docs/sources.md):

| kind | endpoint |
|---|---|
| greenhouse | ``boards-api.greenhouse.io/v1/boards/{token}/jobs`` (+ ``/{token}`` for the name) |
| lever | ``api.lever.co/v0/postings/{token}?mode=json`` |
| ashby | ``api.ashbyhq.com/posting-api/job-board/{token}`` |
| workable | ``apply.workable.com/api/v1/widget/accounts/{token}`` |
| smartrecruiters | ``api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset=…`` |

``fetch_board`` never raises: a missing board, a refusal or bad JSON comes back
as ``BoardResult.error`` with a reason code for the failure log.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote

from ...net.http import DeadDomainError
from ...net.ratelimit import BlockedError, PermanentError
from .base import BoardRef, Posting, parse_time

log = logging.getLogger(__name__)

#: kind -> the API host (rate-limiter key and engine host).
HOSTS = {"greenhouse": "boards-api.greenhouse.io", "lever": "api.lever.co",
         "ashby": "api.ashbyhq.com", "workable": "apply.workable.com",
         "smartrecruiters": "api.smartrecruiters.com"}
#: kind -> the public board page a person would open.
BOARD_PAGES = {"greenhouse": "https://boards.greenhouse.io/{token}",
               "lever": "https://jobs.lever.co/{token}",
               "ashby": "https://jobs.ashbyhq.com/{token}",
               "workable": "https://apply.workable.com/{token}/",
               "smartrecruiters": "https://careers.smartrecruiters.com/{token}"}


@dataclass
class BoardResult:
    ref: BoardRef
    company: str = ""
    postings: list[Posting] = field(default_factory=list)
    #: A failure reason code (board_not_found, blocked, fetch_failed, bad_response), or "".
    error: str = ""
    detail: str = ""

    @property
    def page(self) -> str:
        return BOARD_PAGES.get(self.ref.kind, "").format(token=self.ref.token)


def _title(token: str) -> str:
    """A readable company name from a board token ("acme-labs" -> "Acme Labs")."""
    return " ".join(w.capitalize() for w in token.replace("_", "-").split("-") if w) or token


def _get(http, url: str, host: str):
    return http.get(url, rate_key=host, headers={"Accept": "application/json"}).json()


def _greenhouse(http, ref: BoardRef, limit: int) -> BoardResult:
    host = HOSTS["greenhouse"]
    base = f"https://{host}/v1/boards/{quote(ref.token)}"
    data = _get(http, f"{base}/jobs", host)
    jobs = list(data.get("jobs") or [])[:limit]
    company = next((j.get("company_name") for j in jobs if j.get("company_name")), "")
    if not company:
        try:
            company = _get(http, base, host).get("name") or ""
        except Exception:
            company = ""
    company = company or _title(ref.token)
    postings = [Posting(
        title=str(j.get("title") or "").strip(), url=str(j.get("absolute_url") or ""),
        company=company, location=str((j.get("location") or {}).get("name") or ""),
        posted_at=parse_time(j.get("first_published") or j.get("updated_at")),
        board=ref.key, source="greenhouse") for j in jobs]
    return BoardResult(ref, company, postings)


def _lever(http, ref: BoardRef, limit: int) -> BoardResult:
    host = HOSTS["lever"]
    data = _get(http, f"https://{host}/v0/postings/{quote(ref.token)}?mode=json&limit={limit}",
                host)
    company = _title(ref.token)
    postings = []
    for j in list(data if isinstance(data, list) else [])[:limit]:
        categories = j.get("categories") or {}
        location = categories.get("location") or ", ".join(categories.get("allLocations") or [])
        workplace = str(j.get("workplaceType") or "").lower()
        postings.append(Posting(
            title=str(j.get("text") or "").strip(), url=str(j.get("hostedUrl") or ""),
            company=company, location=str(location or ""),
            remote=True if workplace == "remote" else None,
            posted_at=parse_time(j.get("createdAt")),
            department=str(categories.get("department") or categories.get("team") or ""),
            board=ref.key, source="lever"))
    return BoardResult(ref, company, postings)


def _ashby(http, ref: BoardRef, limit: int) -> BoardResult:
    host = HOSTS["ashby"]
    data = _get(http, f"https://{host}/posting-api/job-board/{quote(ref.token)}", host)
    company = _title(ref.token)
    postings = []
    for j in list(data.get("jobs") or []):
        if j.get("isListed") is False:
            continue
        workplace = str(j.get("workplaceType") or "").lower()
        postings.append(Posting(
            title=str(j.get("title") or "").strip(), url=str(j.get("jobUrl") or ""),
            company=company, location=str(j.get("location") or ""),
            remote=True if (j.get("isRemote") or workplace == "remote") else None,
            posted_at=parse_time(j.get("publishedAt")),
            department=str(j.get("department") or ""), board=ref.key, source="ashby"))
        if len(postings) >= limit:
            break
    return BoardResult(ref, company, postings)


def _workable(http, ref: BoardRef, limit: int) -> BoardResult:
    host = HOSTS["workable"]
    data = _get(http, f"https://{host}/api/v1/widget/accounts/{quote(ref.token)}", host)
    company = str(data.get("name") or "") or _title(ref.token)
    postings = []
    for j in list(data.get("jobs") or [])[:limit]:
        places = [p for p in (j.get("city"), j.get("state"), j.get("country")) if p]
        postings.append(Posting(
            title=str(j.get("title") or "").strip(), url=str(j.get("url") or ""),
            company=company, location=", ".join(places),
            remote=True if j.get("telecommuting") else None,
            posted_at=parse_time(j.get("published_on") or j.get("created_at")),
            department=str(j.get("department") or ""), board=ref.key, source="workable"))
    return BoardResult(ref, company, postings)


def _smartrecruiters(http, ref: BoardRef, limit: int) -> BoardResult:
    host = HOSTS["smartrecruiters"]
    company, postings, offset = "", [], 0
    while len(postings) < limit:
        data = _get(http, f"https://{host}/v1/companies/{quote(ref.token)}/postings"
                          f"?limit=100&offset={offset}", host)
        content = list(data.get("content") or [])
        for j in content:
            location = j.get("location") or {}
            company = company or str((j.get("company") or {}).get("name") or "")
            postings.append(Posting(
                title=str(j.get("name") or "").strip(),
                url=f"https://jobs.smartrecruiters.com/{quote(ref.token)}/{j.get('id')}",
                location=str(location.get("fullLocation") or ", ".join(
                    p for p in (location.get("city"), location.get("country")) if p)),
                remote=True if location.get("remote") else None,
                posted_at=parse_time(j.get("releasedDate")),
                department=str((j.get("department") or {}).get("label") or ""),
                board=ref.key, source="smartrecruiters"))
        offset += len(content)
        if not content or offset >= int(data.get("totalFound") or 0):
            break
    company = company or _title(ref.token)
    for posting in postings:
        posting.company = company
    return BoardResult(ref, company, postings[:limit])


FETCHERS: dict[str, Callable[[Any, BoardRef, int], BoardResult]] = {
    "greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "workable": _workable,
    "smartrecruiters": _smartrecruiters}


def fetch_board(http, ref: BoardRef, *, limit: int = 500) -> BoardResult:
    """One company's open postings on its ATS board. Never raises."""
    fetcher = FETCHERS.get(ref.kind)
    if fetcher is None:
        return BoardResult(ref, error="unknown_board", detail=f"no ATS {ref.kind!r}")
    try:
        result = fetcher(http, ref, limit)
    except DeadDomainError as exc:               # a PermanentError too: test it first
        return BoardResult(ref, error="fetch_failed", detail=str(exc)[:300])
    except PermanentError as exc:
        return BoardResult(ref, error="board_not_found", detail=str(exc)[:300])
    except BlockedError as exc:
        return BoardResult(ref, error="blocked", detail=str(exc)[:300])
    except ValueError as exc:                       # not JSON
        return BoardResult(ref, error="bad_response", detail=repr(exc)[:300])
    except Exception as exc:
        return BoardResult(ref, error="fetch_failed", detail=repr(exc)[:300])
    result.postings = [p for p in result.postings if p.title and p.url.startswith("http")]
    return result
