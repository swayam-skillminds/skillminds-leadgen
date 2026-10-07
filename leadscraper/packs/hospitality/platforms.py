"""Villa and stay platforms: StayVista, SaffronStays, Elivaas, Airbnb, Booking.com.

Two shapes of source live here.

Sitemap platforms (StayVista, SaffronStays, Elivaas) run ONCE per run. Their
sitemaps list every property they manage nationally; we keep the URLs whose
slug names a place in the region, fetch each property page, and read the
structured data the page ships for SEO (JSON-LD, plus the Next.js page state).
Prices on these sites are fetched client-side for the visitor's dates, so a
page with no price in its HTML is rendered once in the browser to read it.
Discovery never depends on a listing-page URL scheme -- those 404'd for both
StayVista and SaffronStays at the time of writing, while the sitemaps did not.

Search platforms (Airbnb, Booking.com) run once per locality: one search page
already spans every venue type, so repeating it per category would fetch the
same cards seven times. Their cards give name, type, bedrooms, rating and a
dated price; neither exposes the owner, so contacts come later from Maps,
Instagram and the venue's own site when the names dedup together.

Everything a platform page yields is a partial Lead; the store merges it with
whatever Maps and the directories found for the same venue.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any, Iterator
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup

from ...config import Category, Locality
from ...core.lead import Lead
from ...core.normalize import (
    clean_text, domain_of, extract_emails, extract_facebook, extract_instagram,
    extract_linkedin, extract_phones, normalize_url, parse_review_count,
)
from ...core.parsing import _address_string, iter_jsonld, meta_content, visible_text
from ...net.ratelimit import BlockedError
from .venues import (
    BRAND_CATEGORY, as_float, as_int, detect_event_friendly, detect_pet_friendly, enrich_from_text,
    extract_amenities, nightly_prices, parse_bathrooms, parse_bedrooms, parse_guests,
    parse_inr, set_price,
)
from ...sources.base import Source

log = logging.getLogger(__name__)

# schema.org types that describe a bookable property.
_LODGING_TYPES = {
    "hotel", "lodgingbusiness", "vacationrental", "house", "accommodation", "resort",
    "product", "place", "touristaccommodation", "bedandbreakfast", "campground",
    "apartment", "singlefamilyresidence", "hostel", "motel", "eventvenue",
}

REGION_WIDE = "Region-wide"


def _types(node: dict) -> set[str]:
    raw = node.get("@type") or ""
    values = raw if isinstance(raw, list) else [raw]
    return {str(v).lower() for v in values if v}


def dig(data: Any, path: str) -> Any:
    """`dig(d, "a.b.0.c")` -- None instead of KeyError/IndexError anywhere."""
    node = data
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
        if node is None:
            return None
    return node


def split_tagline(title: str) -> tuple[str, str]:
    """'The Backyard Story | 3BHK Villa with Pool' -> ('The Backyard Story', '3BHK ...').

    Brand prefixes stay attached ("EKOSTAY Luxe - Hercules Villa" is one name):
    a first part that is only an operator's label keeps the rest with it.
    """
    parts = re.split(r"\s+[\|\-–]\s+", title or "", maxsplit=1)
    if len(parts) != 2 or len(parts[0]) < 3:
        return title, ""
    head, tail = parts
    if re.fullmatch(r"(?i)(?:ekostay(?:\s+luxe)?|saffronstays|stayvista|elivaas|vayavia|oyo)\s*", head):
        rest, _ = split_tagline(tail)
        return f"{head} - {rest}", tail[len(rest):].lstrip(" -|–")
    return head, tail


def next_data(soup: BeautifulSoup) -> dict | None:
    """The Next.js page state, when the page ships one."""
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not (tag.string or "").strip():
        return None
    try:
        return json.loads(tag.string)
    except json.JSONDecodeError:
        return None


def lodging_record(soup: BeautifulSoup) -> dict[str, Any]:
    """Merge every JSON-LD node that describes the property into one dict.

    Pages split a property across several nodes -- a `Product` carrying the
    rating, a `Hotel` carrying the address -- so fields are collected across
    all of them, first value winning.
    """
    out: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if value not in (None, "", [], {}) and key not in out:
            out[key] = value

    for node in iter_jsonld(soup):
        types = _types(node)
        if not (types & _LODGING_TYPES) or not node.get("name"):
            continue
        put("name", clean_text(node.get("name")))
        put("description", clean_text(node.get("description")))
        put("address", _address_string(node.get("address")))
        geo = node.get("geo") or {}
        if isinstance(geo, dict):
            put("lat", as_float(geo.get("latitude")))
            put("lng", as_float(geo.get("longitude")))
        agg = node.get("aggregateRating") or {}
        if isinstance(agg, dict):
            best = as_float(agg.get("bestRating")) or 5.0
            rating = as_float(agg.get("ratingValue"))
            if rating is not None and best and best != 5.0:
                rating = round(rating * 5.0 / best, 2)
            put("rating", rating)
            put("reviews", as_int(agg.get("reviewCount") or agg.get("ratingCount")))
        image = node.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")
        put("image", image if isinstance(image, str) else None)
        put("phone", node.get("telephone"))
        put("price_range", node.get("priceRange"))
        put("bedrooms", as_int(node.get("numberOfBedrooms") or node.get("numberOfRooms")))
        occupancy = node.get("occupancy")
        if isinstance(occupancy, dict):
            put("max_guests", as_int(occupancy.get("maxValue") or occupancy.get("value")))
        amenities = node.get("amenityFeature") or []
        if isinstance(amenities, list):
            names = [clean_text(a.get("name")) for a in amenities if isinstance(a, dict) and a.get("name")]
            put("amenities", names)
        star = node.get("starRating")
        if isinstance(star, dict):
            put("stars", as_int(star.get("ratingValue")))
    return out


def organization_record(soup: BeautifulSoup, region: str | None = None) -> dict[str, Any] | None:
    """The site operator's own Organization node: name, phones, socials."""
    for node in iter_jsonld(soup):
        if "organization" not in _types(node) or not node.get("name"):
            continue
        phones: list[str] = []
        points = node.get("contactPoint") or []
        for point in points if isinstance(points, list) else [points]:
            if isinstance(point, dict) and point.get("telephone"):
                phones.extend(extract_phones(str(point["telephone"]), region))
        same_as = node.get("sameAs") or []
        same_as_text = " ".join(same_as) if isinstance(same_as, list) else str(same_as)
        return {
            "name": clean_text(node.get("name")),
            "url": node.get("url"),
            "phones": phones,
            "emails": extract_emails(str(node.get("email") or "")),
            "address": _address_string(node.get("address")),
            "instagram": extract_instagram(same_as_text),
            "facebook": extract_facebook(same_as_text),
            "linkedin": extract_linkedin(same_as_text),
        }
    return None


class PlatformSource(Source):
    """Shared plumbing: fetch, render, brand record, lead assembly."""

    uses_browser = True
    #: Key recorded in `Lead.platforms`.
    platform: str = ""
    #: The operator that manages every property on this platform, if any
    #: (StayVista manages its villas; Airbnb is a marketplace and manages none).
    operator: str | None = None
    base_url: str = ""

    def __init__(self, ctx):
        super().__init__(ctx)
        self._brand_emitted = False
        self._blocked = False
        self._org: dict[str, Any] | None = None

    @property
    def host(self) -> str:
        return domain_of(self.base_url) or self.name

    def rate_keys(self) -> list[str]:
        return [self.name, self.host, f"www.{self.host}"]

    # --- fetching ---------------------------------------------------------

    def _get(self, url: str) -> str:
        return self.ctx.http.get(url, rate_key=self.host).text

    def _render(self, url: str, wait_ms: int = 2500) -> tuple[str, str]:
        """(visible text, html) of a fully rendered page."""
        with self.ctx.browser.page() as page:
            self.ctx.limiter.wait(self.host)
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception as exc:
                log.debug("%s: networkidle never settled for %s (%r)", self.name, url, exc)
            page.wait_for_timeout(wait_ms)
            self.ctx.browser.check_blocked(page)
            return page.inner_text("body"), page.content()

    # --- brand record -----------------------------------------------------

    def brand_lead(self) -> Lead | None:
        """One Brand row for the platform's operator, the first time we see it."""
        if self._brand_emitted or not self.operator:
            return None
        self._brand_emitted = True
        org = self._org or {}
        lead = self.make_lead(
            business_name=self.operator,
            category=BRAND_CATEGORY,
            locality=REGION_WIDE,
            phones=org.get("phones", []),
            emails=org.get("emails", []),
            website=normalize_url(org.get("url") or self.base_url),
            address=org.get("address", ""),
            instagram=org.get("instagram"),
            facebook=org.get("facebook"),
            linkedin=org.get("linkedin"),
            source_urls=[self.base_url],
        )
        lead.record_type = "Brand"
        lead.platforms = [self.platform]
        lead.property_type = "Villa rental operator"
        return lead

    # --- lead assembly ----------------------------------------------------

    def build_lead(self, fields: dict[str, Any], url: str, category_name: str) -> Lead | None:
        name = clean_text(fields.get("name"))
        if not name:
            return None
        lead = self.make_lead(
            business_name=name,
            category=category_name,
            address=clean_text(fields.get("address")),
            phones=extract_phones(" ".join(str(p) for p in fields.get("phones", []) if p),
                                  self.region)[:3],
            emails=[e for e in fields.get("emails", []) if e][:2],
            source_urls=[url],
        )
        lead.record_type = "Property"
        lead.platforms = [self.platform, *fields.get("extra_platforms", [])]
        lead.listing_urls = [url]
        lead.managed_by = self.operator or ""
        lead.property_type = clean_text(fields.get("property_type"))
        lead.description = clean_text(fields.get("description"))[:1500]
        lead.image_url = fields.get("image")
        lead.gmaps_url = fields.get("gmaps_url")
        lead.latitude, lead.longitude = as_float(fields.get("lat")), as_float(fields.get("lng"))
        if lead.latitude is None or lead.longitude is None:
            lead.latitude = lead.longitude = None
        lead.bedrooms = as_int(fields.get("bedrooms")) or None
        lead.bathrooms = as_float(fields.get("bathrooms")) or None
        lead.max_guests = as_int(fields.get("max_guests")) or None
        lead.hotel_class = as_int(fields.get("stars")) or None
        lead.amenities = [a for a in fields.get("amenities", []) if a]
        lead.checkin_checkout = clean_text(fields.get("checkin_checkout"))
        for flag in ("has_pool", "has_lawn", "event_friendly", "pet_friendly"):
            if fields.get(flag) is not None:
                setattr(lead, flag, bool(fields[flag]))
        rating = as_float(fields.get("rating"))
        if rating is not None and 0 < rating <= 5:
            lead.platform_ratings = {self.platform: [round(rating, 2), as_int(fields.get("reviews")) or 0]}
        if fields.get("price"):
            set_price(lead, fields["price"], fields.get("price_max") or fields["price"],
                      fields.get("price_basis", "per night"), self.platform)
        lead.notes.extend(fields.get("notes", []))
        # Free text last: it only fills what the structured data left empty.
        enrich_from_text(lead, " ".join(filter(None, [name, lead.description,
                                                      fields.get("text", "")])),
                         price_source=self.platform, currency=self.currency)
        return lead

    def finish(self, lead: Lead, url: str, fallback: Locality | None = None) -> Lead | None:
        """Region check + locality, shared by every platform. None = drop."""
        if self.out_of_region(lead):
            self.fail("out_of_region", url=url, business=lead.business_name,
                      detail=f"pin is beyond geo.radius_km of {self.ctx.config.city}")
            return None
        slug_words = urlparse(url).path.replace("-", " ").replace("/", " ")
        self.assign_locality(lead, fallback, slug_words)
        if not lead.locality:
            lead.locality = str(self.ctx.config.get("geo.default_locality", "Unassigned"))
        return lead


# ---------------------------------------------------------------------------
# Sitemap-driven platforms
# ---------------------------------------------------------------------------


class SitemapPlatformSource(PlatformSource):
    scope = "run"
    #: Sitemaps listing property pages. Overridable from config (`sitemaps:`).
    sitemaps: tuple[str, ...] = ()
    #: Property-page URLs, as opposed to collection / city / blog pages.
    property_pattern: re.Pattern[str] = re.compile(r"$^")
    #: Most platform pages ship their price in the HTML; a few only in JS.
    render_for_price_default: bool = False

    def discover(self, locality: Locality | None = None,
                 category: Category | None = None) -> Iterator[Lead]:
        urls = self.property_urls()
        if not urls:
            self.fail("no_properties", detail="sitemap listed no property in the region")
            return
        cap = int(self.cfg.get("max_properties", 400))
        refresh = bool(self.cfg.get("refresh", False))
        log.info("%s: %d regional properties in the sitemap%s", self.name, len(urls),
                 f" (capped at {cap})" if len(urls) > cap else "")
        category_name = str(self.cfg.get("category", "Platform Listings"))

        for url in urls[:cap]:
            if self._blocked:
                return
            if not refresh and self.ctx.store.already_seen(url):
                log.debug("%s: %s already seen; skipping", self.name, url)
                continue
            lead = self._read_property(url, category_name)
            if lead is None:
                continue
            self.ctx.store.mark_seen(url, self.name)
            yield lead
            brand = self.brand_lead()
            if brand:
                yield brand

    # --- discovery ----------------------------------------------------------

    def region_keywords(self) -> list[str]:
        words: set[str] = set(str(k).lower() for k in (self.ctx.config.get("geo.keywords") or []))
        for loc in self.ctx.config.localities:
            words.update(loc.keywords)
        return sorted(w for w in words if len(w) >= 3)

    def property_urls(self) -> list[str]:
        """Property URLs from the sitemaps whose slug names a regional place."""
        keywords = self.region_keywords()
        if not keywords:
            log.warning("%s: no region keywords configured (localities[].keywords); "
                        "cannot tell which properties are in the region", self.name)
            return []
        pattern = re.compile(r"(?<![a-z])(" + "|".join(re.escape(k) for k in keywords) + r")(?![a-z])")
        found: list[str] = []
        for sitemap in self.cfg.get("sitemaps") or self.sitemaps:
            for url in self._sitemap_locs(sitemap):
                slug = urlparse(url).path.lower()
                if self.property_pattern.search(url) and pattern.search(slug) and url not in found:
                    found.append(url)
        return found

    def _sitemap_locs(self, sitemap_url: str, depth: int = 0) -> list[str]:
        try:
            xml = self._get(sitemap_url)
        except BlockedError as exc:
            self.fail("blocked", url=sitemap_url, detail=str(exc))
            self._blocked = True
            return []
        except Exception as exc:
            self.fail("sitemap_failed", url=sitemap_url, detail=repr(exc))
            return []
        locs = [clean_text(loc) for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)]
        # A sitemap index lists more sitemaps; follow one level down.
        if depth == 0 and "<sitemapindex" in xml[:500].lower():
            nested: list[str] = []
            for loc in locs:
                nested.extend(self._sitemap_locs(loc, depth=1))
            return nested
        return locs

    # --- property pages -------------------------------------------------

    def _read_property(self, url: str, category_name: str) -> Lead | None:
        try:
            html = self._get(url)
        except BlockedError as exc:
            self.fail("blocked", url=url, detail=str(exc))
            self._blocked = True
            return None
        except Exception as exc:
            self.fail("property_failed", url=url, detail=repr(exc))
            return None

        soup = self.ctx.http.soup(html)
        if self._org is None:
            self._org = organization_record(soup, self.region)
        fields = self.generic_fields(soup)
        try:
            fields.update({k: v for k, v in self.platform_fields(soup, url).items()
                           if v not in (None, "", [], {})})
        except Exception as exc:
            # The platform-specific reader is the brittle part; the generic
            # one still gives name, rating and text, so keep going.
            log.info("%s: platform fields unreadable for %s (%r)", self.name, url, exc)

        render = bool(self.cfg.get("render_for_price", self.render_for_price_default))
        if render and not fields.get("price") and not self._blocked:
            rendered_text = ""
            try:
                rendered_text, _ = self._render(url)
            except BlockedError as exc:
                self.fail("blocked", url=url, detail=f"render: {exc}")
                self._blocked = True
            except Exception as exc:
                self.fail("render_failed", url=url, detail=repr(exc))
            # The booking widget comes before any "similar stays" strip, so the
            # first per-night price on the page is this property's.
            prices = nightly_prices(rendered_text, self.currency)
            if rendered_text:
                fields.update({k: v for k, v in self.rendered_fields(rendered_text).items()
                               if v not in (None, "", [], {})})
            if prices:
                fields["price"] = prices[0]
            elif rendered_text:
                self.fail("price_not_found", url=url, business=fields.get("name"),
                          detail="rendered page showed no per-night price")
            fields["text"] = f"{fields.get('text', '')} {rendered_text[:8000]}"

        lead = self.build_lead(fields, url, category_name)
        if lead is None:
            self.fail("no_property_data", url=url, detail="no property name on the page")
            return None
        return self.finish(lead, url)

    def generic_fields(self, soup: BeautifulSoup) -> dict[str, Any]:
        record = lodging_record(soup)
        fields: dict[str, Any] = {
            "name": record.get("name") or self.name_from_title(soup),
            "description": record.get("description") or meta_content(soup, "description", "og:description"),
            "address": record.get("address", ""),
            "lat": record.get("lat"), "lng": record.get("lng"),
            "rating": record.get("rating"), "reviews": record.get("reviews"),
            "image": record.get("image") or meta_content(soup, "og:image") or None,
            "bedrooms": record.get("bedrooms"), "max_guests": record.get("max_guests"),
            "amenities": record.get("amenities", []),
            "stars": record.get("stars"),
            "text": visible_text(soup, limit=15_000),
        }
        price = parse_inr(record.get("price_range")) if record.get("price_range") else None
        if price:
            fields["price"] = price
        return fields

    @staticmethod
    def name_from_title(soup: BeautifulSoup) -> str:
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        return re.split(r"\s+[\|\-–]\s+", title)[0].strip() if title else ""

    def platform_fields(self, soup: BeautifulSoup, url: str) -> dict[str, Any]:
        """Platform-specific structured fields. Override per platform."""
        return {}

    def rendered_fields(self, text: str) -> dict[str, Any]:
        """Fields only the rendered page shows. Override per platform."""
        return {}


class StayVistaSource(SitemapPlatformSource):
    name = "stayvista"
    priority = 60
    #: Lists Indian villas only, though the hospitality pack is worldwide.
    applicable_countries = frozenset({"IN"})
    platform = "stayvista"
    operator = "StayVista"
    base_url = "https://www.stayvista.com"
    sitemaps = ("https://www.stayvista.com/villas-sitemap.xml",)
    property_pattern = re.compile(r"stayvista\.com/villa/", re.I)
    # Tariffs load from an API for the visitor's dates; the HTML has none.
    render_for_price_default = True

    def platform_fields(self, soup: BeautifulSoup, url: str) -> dict[str, Any]:
        data = dig(next_data(soup), "props.pageProps.propertyDetailsObj.data") or {}
        detail = data.get("property_detail") or {}
        if not detail:
            return {}
        amenities = [clean_text(a.get("name")) for a in dig(data, "amenities.sorted") or []
                     if isinstance(a, dict)]
        amenities += [clean_text(u.get("name")) for u in data.get("usps") or [] if isinstance(u, dict)]
        tags = [clean_text(t.get("label") or t.get("name")) for t in detail.get("tags") or []
                if isinstance(t, dict)]
        faq_text = " ".join(f"{f.get('question', '')} {f.get('answer', '')}"
                            for f in data.get("faq") or [] if isinstance(f, dict))
        bathrooms = (as_int(detail.get("no_of_private_bathrooms")) or 0) + \
            (as_int(detail.get("no_of_shared_bathrooms")) or 0)
        notes = [f"StayVista tag: {t}" for t in tags if t]
        if detail.get("ultra_luxury_collection"):
            notes.append("StayVista Ultra Luxury Collection")
        elif detail.get("luxury_collection"):
            notes.append("StayVista Luxury Collection")
        if detail.get("agent_mode_note"):
            notes.append(f"StayVista note: {clean_text(detail['agent_mode_note'])}")
        city = clean_text(detail.get("city"))
        return {
            "name": clean_text(detail.get("vista_name")),
            "property_type": clean_text(detail.get("property_category") or "villa").title(),
            "address": ", ".join(p for p in (city.title(), clean_text(detail.get("state")).title()) if p),
            "lat": detail.get("latitude"), "lng": detail.get("longitude"),
            "rating": detail.get("ratings"), "reviews": detail.get("feedbacks_count"),
            "bedrooms": detail.get("number_of_rooms"),
            "bathrooms": bathrooms or None,
            "max_guests": detail.get("max_occupancy"),
            "amenities": [a for a in amenities if a],
            "has_pool": True if any("pool" in a.lower() for a in amenities + tags) else None,
            "has_lawn": True if any("lawn" in a.lower() for a in amenities + tags) else None,
            "event_friendly": detect_event_friendly(faq_text),
            "pet_friendly": detect_pet_friendly(faq_text),
            "checkin_checkout": " / ".join(filter(None, [detail.get("checkin_time"),
                                                         detail.get("checkout_time")])),
            "phones": [data.get("property_contact_number")],
            "text": f"{faq_text} {city} {detail.get('city_tags') or ''}",
            "notes": notes,
        }


class SaffronStaysSource(SitemapPlatformSource):
    name = "saffronstays"
    priority = 61
    #: Lists Indian villas only, though the hospitality pack is worldwide.
    applicable_countries = frozenset({"IN"})
    platform = "saffronstays"
    operator = "SaffronStays"
    base_url = "https://www.saffronstays.com"
    sitemaps = ("https://www.saffronstays.com/sitemap-properties.xml",)
    property_pattern = re.compile(r"saffronstays\.com/view/", re.I)

    def platform_fields(self, soup: BeautifulSoup, url: str) -> dict[str, Any]:
        listing = dig(next_data(soup), "props.pageProps.data.listing.data")
        if isinstance(listing, list):
            listing = listing[0] if listing else None
        if not isinstance(listing, dict):
            return {}
        core = listing.get("ListingList") or {}
        # "Amrapali - Igtpuri, Nashik, Maharashtra": the part before the dash
        # is the villa, the rest is where it is.
        reference = clean_text(core.get("file_reference"))
        name, _, place = reference.partition(" - ")
        amenities = [str(a) for a in (listing.get("AmenitiesList") or {}).values()]
        missing = " ".join(str(v) for v in (listing.get("NotAmnitiesList") or {}).values()).lower()
        suitable = [str(s) for s in (listing.get("SuitablesList") or {}).values()]
        rating = listing.get("RatingObj") or {}
        question_text = json.dumps(listing.get("QuestionsList") or {}, ensure_ascii=False)
        about = BeautifulSoup(core.get("about_villa") or "", "html.parser").get_text(" ", strip=True)
        seo = " ".join(str(core.get(k) or "") for k in ("seo_faq_usp", "seo_faq_ideal_for"))
        in_time = f"{core.get('in_time', '')} {core.get('in_time_format', '')}".strip()
        out_time = f"{core.get('out_time', '')} {core.get('out_time_format', '')}".strip()
        lat, lng = self._main_coordinates(listing, core)
        pet_friendly = False if "pet friendly" in missing else (
            True if any("pet" in a.lower() for a in amenities + suitable) else None)
        notes = []
        if core.get("signature"):
            notes.append(f"SaffronStays signature: {core['signature']}")
        if suitable:
            notes.append("SaffronStays suited for: " + ", ".join(suitable[:8]))
        return {
            "name": name.strip() or reference,
            "property_type": clean_text(core.get("category")),
            # The reference names the village ("Igtpuri, Nashik"); `location`
            # is only ever the district.
            "address": place or clean_text(core.get("location")),
            "lat": lat, "lng": lng,
            "price": as_float(core.get("per_night_charge")),
            "price_basis": "per night",
            "bedrooms": core.get("min_rooms") or core.get("no_of_beds"),
            "bathrooms": core.get("no_of_bathrooms"),
            "max_guests": core.get("max_capacity"),
            "rating": rating.get("rating_val"), "reviews": rating.get("review"),
            "amenities": amenities,
            "has_pool": True if any("pool" in a.lower() for a in amenities + suitable) else None,
            "has_lawn": True if any("lawn" in a.lower() for a in amenities) else None,
            "pet_friendly": pet_friendly,
            "event_friendly": detect_event_friendly(f"{question_text} {seo} {about}"),
            "checkin_checkout": " / ".join(filter(None, [in_time, out_time])),
            "gmaps_url": core.get("GMB_LINK") or None,
            "description": about[:1500] or seo,
            "text": f"{place} {seo} {about}",
            "notes": notes,
        }

    @staticmethod
    def _main_coordinates(listing: dict, core: dict) -> tuple[Any, Any]:
        for source in (core, listing):
            for lat_key, lng_key in (("LATITUDE", "LONGITUDE"), ("latitude", "longitude"),
                                     ("lat", "lng")):
                if source.get(lat_key) and source.get(lng_key):
                    return source[lat_key], source[lng_key]
        return None, None


class ElivaasSource(SitemapPlatformSource):
    name = "elivaas"
    priority = 62
    #: Lists Indian villas only, though the hospitality pack is worldwide.
    applicable_countries = frozenset({"IN"})
    platform = "elivaas"
    operator = "Elivaas"
    base_url = "https://www.elivaas.com"
    sitemaps = ("https://www.elivaas.com/sitemap.xml",)
    property_pattern = re.compile(r"elivaas\.com/villa-in-[\w-]+/", re.I)
    # Elivaas renders details client-side; the HTML is mostly the shell.
    render_for_price_default = True

    def platform_fields(self, soup: BeautifulSoup, url: str) -> dict[str, Any]:
        # "White House - 4-BHK Villa With Pool in Igatpuri"
        # "VK Villa | 10-BHK Villa with Two Private Pools, ... & Mountain View"
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        parts = re.split(r"\s+[\-|–]\s+", title, maxsplit=1)
        name, rest = (parts[0], parts[1]) if len(parts) == 2 else (title, "")
        place = re.search(r"\bin\s+([A-Z][\w\s]+)$", rest)
        return {
            "name": name.strip(),
            "property_type": "Villa" if re.search(r"villa", rest, re.I) else "",
            "bedrooms": parse_bedrooms(rest),
            "has_pool": True if re.search(r"pool", rest, re.I) else None,
            "address": place.group(1).strip() if place else "",
            "text": rest,
        }

    _PLACE_LINE = re.compile(r"^\s*([A-Z][\w .'-]{2,40}),\s*(Nashik|Igatpuri|Maharashtra)\s*$", re.M)
    _OVERALL = re.compile(r"Overall Rating\s*(\d\.\d)\s*\(([\d,]+)\)", re.I)

    def rendered_fields(self, text: str) -> dict[str, Any]:
        """'Trimbakeshwar, Nashik' and 'Overall Rating 4.8 (1,218)' from the page."""
        out: dict[str, Any] = {}
        place = self._PLACE_LINE.search(text)
        if place:
            out["address"] = f"{place.group(1).strip()}, {place.group(2)}"
        overall = self._OVERALL.search(text)
        if overall:
            out["rating"], out["reviews"] = as_float(overall.group(1)), as_int(overall.group(2))
        return out


# ---------------------------------------------------------------------------
# Search-driven platforms
# ---------------------------------------------------------------------------


class SearchPlatformSource(PlatformSource):
    scope = "locality"

    def search_place(self, locality: Locality) -> str | None:
        """The place string this platform should search for a locality.

        Mapped explicitly in config (`platform_searches:`) rather than derived:
        "Gangapur Dam Belt" means nothing to Airbnb's geocoder, and a wrong
        guess silently searches somewhere else.
        """
        mapping = self.ctx.config.get("platform_searches", {}) or {}
        place = mapping.get(locality.name)
        return str(place) if place else None

    def discover(self, locality: Locality | None,
                 category: Category | None = None) -> Iterator[Lead]:
        if locality is None or self._blocked:
            return
        place = self.search_place(locality)
        if not place:
            log.info("%s: no platform_searches entry for %s; skipping", self.name, locality.name)
            return
        category_name = str(self.cfg.get("category", "Platform Listings"))
        seen: set[str] = set()
        for card in self.cards(place, locality):
            url = card.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            lead = self.build_lead(card, url, category_name)
            if lead is None:
                continue
            lead = self.finish(lead, url, locality)
            if lead is not None:
                yield lead

    def cards(self, place: str, locality: Locality) -> Iterator[dict[str, Any]]:
        raise NotImplementedError

    def _render_cards(self, url: str, script: str) -> tuple[list[dict], str | None]:
        """(cards, next-page URL) from one rendered search page."""
        with self.ctx.browser.page() as page:
            self.ctx.limiter.wait(self.host)
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
            self.ctx.browser.check_blocked(page)
            result = page.evaluate(script) or {}
        return result.get("cards") or [], result.get("next")


_AIRBNB_SCRIPT = """
() => {
  const cards = Array.from(document.querySelectorAll('div[itemprop="itemListElement"]')).map(c => ({
    name: (c.querySelector('meta[itemprop="name"]') || {}).content || null,
    url: (c.querySelector('meta[itemprop="url"]') || {}).content || null,
    text: c.innerText || '',
  }));
  const next = document.querySelector('a[aria-label="Next"]');
  return {cards, next: next ? next.href : null};
}
"""

_AIRBNB_TYPE = re.compile(r"^(.{3,40}?)\s+in\s+(.{2,40})$")
_AIRBNB_RATING = (
    re.compile(r"(\d\.\d{1,2})\s*out of 5 average rating,?\s*([\d,]+)\s*reviews?", re.I),
    re.compile(r"\b([1-5]\.\d{1,2})\s*\(([\d,]+)\)"),
)


class AirbnbSource(SearchPlatformSource):
    name = "airbnb"
    priority = 63
    platform = "airbnb"
    base_url = "https://www.airbnb.co.in"

    def cards(self, place: str, locality: Locality) -> Iterator[dict[str, Any]]:
        slug = quote(re.sub(r"\s*,\s*", "--", place.strip()).replace(" ", "-"))
        room_filter = ("?room_types%5B%5D=Entire%20home%2Fapt"
                       if self.cfg.get("entire_homes_only", True) else "")
        url: str | None = f"{self.base_url}/s/{slug}/homes{room_filter}"
        for page_number in range(1, int(self.cfg.get("max_pages", 2)) + 1):
            if not url or self._blocked:
                return
            try:
                raw_cards, url = self._render_cards(url, _AIRBNB_SCRIPT)
            except BlockedError as exc:
                self.fail("blocked", locality=locality.name, detail=str(exc))
                self._blocked = True
                return
            except Exception as exc:
                self.fail("search_failed", locality=locality.name, detail=repr(exc))
                return
            if not raw_cards:
                if page_number == 1:
                    self.fail("no_results", locality=locality.name, detail=f"no cards for {place!r}")
                return
            for raw in raw_cards:
                card = self.parse_card(raw)
                if card:
                    yield card

    def parse_card(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        title = clean_text(raw.get("name"))
        path = raw.get("url") or ""
        if not title or "/rooms/" not in path:
            return None
        url = urljoin(self.base_url, path.split("?")[0])
        text = raw.get("text") or ""
        lines = [clean_text(line) for line in text.splitlines() if clean_text(line)]

        property_type, place = "", ""
        for line in lines:
            match = _AIRBNB_TYPE.match(line)
            if match and not line.lower().startswith("photo"):
                property_type, place = match.group(1), match.group(2)
                break

        name, tagline = split_tagline(title)

        rating = reviews = None
        for pattern in _AIRBNB_RATING:
            match = pattern.search(text)
            if match:
                rating, reviews = as_float(match.group(1)), as_int(match.group(2))
                break

        prices = nightly_prices(text, self.currency)
        notes = []
        if re.search(r"guest favourite|guest favorite", text, re.I):
            notes.append("Airbnb Guest favourite")
        extra = ["airbnb_luxe"] if re.search(r"\bluxe\b", text, re.I) else []
        return {
            "name": name,
            "url": url,
            "property_type": property_type,
            "address": place,
            "description": tagline,
            "bedrooms": parse_bedrooms(text),
            "bathrooms": parse_bathrooms(text),
            "max_guests": parse_guests(text),
            "rating": rating, "reviews": reviews,
            "price": prices[0] if prices else None,
            "price_basis": "per night",
            "has_pool": True if re.search(r"pool", title, re.I) else None,
            "extra_platforms": extra,
            "notes": notes,
            "text": f"{title} {place}",
        }


_BOOKING_SCRIPT = """
() => {
  const q = (c, s) => { const e = c.querySelector(s); return e ? e.innerText : null; };
  const cards = Array.from(document.querySelectorAll('div[data-testid="property-card"]')).map(c => {
    const stars = c.querySelector('[data-testid="rating-stars"], [data-testid="rating-squares"]');
    return {
      title: q(c, '[data-testid="title"]'),
      url: (c.querySelector('a[data-testid="title-link"]') || {}).href || null,
      score: q(c, '[data-testid="review-score"]'),
      price: q(c, '[data-testid="price-and-discounted-price"]'),
      address: q(c, '[data-testid="address"]'),
      distance: q(c, '[data-testid="distance"]'),
      stars_label: stars ? (stars.getAttribute('aria-label') || '') : '',
      stars_count: stars ? stars.children.length : 0,
      text: c.innerText || '',
    };
  });
  return {cards, next: null};
}
"""

# Booking.com property-type filter ids -> the type we record for the card.
DEFAULT_BOOKING_TYPES = {
    "213": "Villa",
    "220": "Holiday home",
    "206": "Resort",
    "204": "Hotel",
    "210": "Farm stay",
    "224": "Luxury tent",
}


class BookingSource(SearchPlatformSource):
    name = "booking"
    priority = 64
    platform = "booking"
    base_url = "https://www.booking.com"

    def search_url(self, place: str, type_id: str, offset: int = 0,
                   today: dt.date | None = None) -> str:
        days_ahead = int(self.cfg.get("days_ahead", 21))
        checkin = (today or dt.date.today()) + dt.timedelta(days=days_ahead)
        # A Saturday night: weekend rates are what a getaway venue charges.
        checkin += dt.timedelta(days=(5 - checkin.weekday()) % 7)
        checkout = checkin + dt.timedelta(days=1)
        return (f"{self.base_url}/searchresults.en-gb.html?ss={quote(place)}"
                f"&checkin={checkin.isoformat()}&checkout={checkout.isoformat()}"
                f"&group_adults=2&no_rooms=1&group_children=0"
                f"&nflt=ht_id%3D{type_id}&offset={offset}")

    def cards(self, place: str, locality: Locality) -> Iterator[dict[str, Any]]:
        types = self.cfg.get("property_types") or DEFAULT_BOOKING_TYPES
        pages = int(self.cfg.get("max_pages", 2))
        for type_id, type_name in dict(types).items():
            for page in range(pages):
                if self._blocked:
                    return
                url = self.search_url(place, str(type_id), offset=25 * page)
                try:
                    raw_cards, _ = self._render_cards(url, _BOOKING_SCRIPT)
                except BlockedError as exc:
                    self.fail("blocked", url=url, locality=locality.name, detail=str(exc))
                    self._blocked = True
                    return
                except Exception as exc:
                    self.fail("search_failed", url=url, locality=locality.name, detail=repr(exc))
                    break
                if not raw_cards:
                    break
                for raw in raw_cards:
                    card = self.parse_card(raw, str(type_name))
                    if card:
                        yield card
                if len(raw_cards) < 25:
                    break

    def parse_card(self, raw: dict[str, Any], type_name: str) -> dict[str, Any] | None:
        name = clean_text(raw.get("title"))
        href = raw.get("url") or ""
        if not name or "/hotel/" not in href:
            return None
        path = re.sub(r"\.[a-z]{2}(?:-[a-z]{2})?\.html$", ".html", urlparse(href).path)
        url = f"{self.base_url}{path}"

        rating = reviews = None
        score = raw.get("score") or ""
        match = re.search(r"(\d{1,2}(?:\.\d)?)", score)
        if match:
            value = float(match.group(1))
            rating = round(value / 2, 2) if value > 5 else value   # Booking scores out of 10
        match = re.search(r"([\d,]+)\s+reviews?", score, re.I)
        if match:
            reviews = parse_review_count(match.group(1))

        # Only the explicit "4 out of 5" label: counting the star elements
        # double-counts (each star renders as more than one node) and turned
        # ordinary resorts into 5-star hotels.
        stars = None
        label = re.search(r"([1-5])\s+out of\s+5", raw.get("stars_label") or "", re.I)
        if label:
            stars = int(label.group(1))

        # Search dates are one night, so the card price is the nightly rate.
        price = parse_inr(raw.get("price"))
        text = raw.get("text") or ""
        notes = [clean_text(raw["distance"])] if raw.get("distance") else []
        full_title = name
        name, tagline = split_tagline(name)
        return {
            "name": name,
            "description": tagline,
            "url": url,
            "property_type": type_name,
            "address": clean_text(raw.get("address")),
            "rating": rating, "reviews": reviews,
            "stars": stars if type_name in ("Hotel", "Resort") else None,
            "price": price if price and price >= 1_500 else None,
            "price_basis": "per night",
            "bedrooms": parse_bedrooms(full_title),
            "has_pool": True if re.search(r"pool", full_title, re.I) else None,
            "amenities": extract_amenities(text),
            "notes": notes,
            "text": full_title,
        }
