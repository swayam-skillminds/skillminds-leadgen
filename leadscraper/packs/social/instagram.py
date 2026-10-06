"""Instagram business-profile enrichment, at deliberately low volume.

Instagram rate-limits logged-out fetches hard, so this runs last, on a hard
per-run cap, and only for leads where some other source already gave us a
handle. We read the public profile HTML for the bio block, which is where
event vendors put their booking phone and email.

Profiles behind the login wall are logged as skips.
"""

from __future__ import annotations

import json
import logging
import re

from ...core.lead import Lead
from ...core.normalize import clean_text, extract_emails, extract_phones, extract_whatsapp, normalize_url
from ...core.parsing import meta_content
from ...net.ratelimit import BlockedError
from ...sources.base import Enricher

log = logging.getLogger(__name__)

PROFILE_URL = "https://www.instagram.com/{handle}/"

_LOGIN_WALL = ("login_and_signup_page", "loginform", "log in to instagram",
               "please wait a few minutes before you try again")

# The bio text appears in the embedded JSON as "biography":"..."
_BIO_JSON = re.compile(r'"biography"\s*:\s*"((?:[^"\\]|\\.)*)"')
_EXTERNAL_URL = re.compile(r'"external_url"\s*:\s*"((?:[^"\\]|\\.)*)"')
_BUSINESS_EMAIL = re.compile(r'"business_email"\s*:\s*"((?:[^"\\]|\\.)*)"')
_BUSINESS_PHONE = re.compile(r'"business_phone_number"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _unescape_json_fragment(raw: str) -> str:
    """Decode a captured JSON string body (handles \\u escapes)."""
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw.encode("utf-8", "ignore").decode("unicode_escape", "ignore")


class InstagramEnricher(Enricher):
    name = "instagram"
    priority = 30
    fills = frozenset({"phones", "emails", "website"})

    def __init__(self, ctx):
        super().__init__(ctx)
        self._fetched = 0
        self._cap = int(self.cfg.get("max_profiles_per_run", 150))

    def rate_keys(self) -> list[str]:
        return ["instagram", "www.instagram.com"]

    def targets(self, store) -> list:
        """Leads with a known handle that are still missing an email or phone."""
        return store.instagram_enrichment_targets()

    def enrich(self, lead: Lead) -> Lead | None:
        if not lead.instagram:
            return None
        if self._fetched >= self._cap:
            self.fail("run_cap_reached", business=lead.business_name,
                      detail=f"instagram cap of {self._cap} profiles reached this run")
            return None

        handle = lead.instagram.lstrip("@")
        url = PROFILE_URL.format(handle=handle)
        try:
            response = self.ctx.http.get(url, rate_key="www.instagram.com")
        except BlockedError as exc:
            self.fail("blocked", url=url, business=lead.business_name, detail=str(exc))
            return None
        except Exception as exc:
            self.fail("fetch_failed", url=url, business=lead.business_name, detail=repr(exc))
            return None
        finally:
            self._fetched += 1

        body = response.text
        if any(marker in body[:20_000].lower() for marker in _LOGIN_WALL):
            self.fail("login_wall", url=url, business=lead.business_name,
                      detail="profile requires login; no bio contact details read")
            return None

        bio = self._bio(body)
        soup = self.ctx.http.soup(body)
        description = meta_content(soup, "og:description", "description")
        blob = f"{bio} {description}"

        harvested = Lead(business_name=lead.business_name, instagram=handle)
        harvested.emails = extract_emails(blob)[:2]
        harvested.phones = extract_phones(blob, self.region)[:2]
        harvested.whatsapp = extract_whatsapp(body, self.region)

        email_match = _BUSINESS_EMAIL.search(body)
        if email_match:
            harvested.emails.extend(extract_emails(_unescape_json_fragment(email_match.group(1))))
        phone_match = _BUSINESS_PHONE.search(body)
        if phone_match:
            harvested.phones.extend(extract_phones(_unescape_json_fragment(phone_match.group(1)),
                                                   self.region))

        link_match = _EXTERNAL_URL.search(body)
        if link_match:
            candidate = normalize_url(_unescape_json_fragment(link_match.group(1)))
            from ...core.normalize import is_vendor_website
            if candidate and is_vendor_website(candidate):
                harvested.website = candidate

        harvested.emails = list(dict.fromkeys(harvested.emails))
        harvested.phones = list(dict.fromkeys(harvested.phones))

        if not (harvested.emails or harvested.phones or harvested.website or harvested.whatsapp):
            self.fail("nothing_found", url=url, business=lead.business_name,
                      detail="profile read but bio had no contact details")
            return None

        harvested.sources = [self.name]
        harvested.source_urls = [url]
        return harvested

    @staticmethod
    def _bio(body: str) -> str:
        match = _BIO_JSON.search(body)
        return clean_text(_unescape_json_fragment(match.group(1))) if match else ""
