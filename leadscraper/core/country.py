"""Per-country facts: phone region, currency, locale, postal codes, name noise.

Everything country-shaped that used to be hard-coded for India (+91, ₹, the
6-digit PIN, "en-IN", "Asia/Kolkata", Google's ``gl=in``) is looked up here
from the run's ``geography.country``. Countries not in the table still work:
phones parse through ``phonenumbers`` for any ISO code, and the rest falls
back to neutral defaults (no currency symbol, UTC, a generic postal code).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

import phonenumbers


@dataclass(frozen=True)
class CountryInfo:
    code: str                               # ISO 3166-1 alpha-2
    name: str
    #: Words that only say "this country" in a business name ("India", "USA").
    name_noise: tuple[str, ...] = ()
    currency: str = ""                      # ISO 4217
    #: How prices are written there, most specific first ("₹", "Rs", "INR").
    currency_symbols: tuple[str, ...] = ()
    #: Display prefix for amounts ("₹25,000", "$250", "AED 500").
    money_prefix: str = ""
    locale: str = "en-US"
    timezone: str = "UTC"
    google_gl: str = ""
    ddg_region: str = "wt-wt"
    #: Regex for a postal code as it ends an address line.
    postal_code: str = r"[A-Z0-9][A-Z0-9 \-]{2,9}"
    #: Digit-count bounds for the permissive phone regex (the strict
    #: phonenumbers matcher still decides validity).
    phone_digits: tuple[int, int] = (8, 13)
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def calling_code(self) -> int:
        return phonenumbers.country_code_for_region(self.code)

    @property
    def accept_language(self) -> str:
        lang = self.locale.split("-")[0]
        return f"{self.locale},en-GB;q=0.9,{lang};q=0.8" if lang == "en" else \
            f"{self.locale},{lang};q=0.9,en;q=0.8"


_TABLE: dict[str, CountryInfo] = {c.code: c for c in (
    CountryInfo("IN", "India", ("india",), "INR", ("₹", "&#8377;", "INR", "Rs."),
                "₹", "en-IN", "Asia/Kolkata", "in", "in-en", r"\d{6}", (10, 12)),
    CountryInfo("US", "United States", ("usa", "united states"), "USD", ("US$", "$", "USD"),
                "$", "en-US", "America/Chicago", "us", "us-en", r"\d{5}(?:-\d{4})?", (10, 11)),
    CountryInfo("GB", "United Kingdom", ("uk", "united kingdom"), "GBP", ("£", "GBP"),
                "£", "en-GB", "Europe/London", "uk", "uk-en",
                r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}", (10, 13)),
    CountryInfo("AE", "United Arab Emirates", ("uae", "united arab emirates"), "AED",
                ("AED", "Dhs", "د.إ"), "AED ", "en-AE", "Asia/Dubai", "ae", "xa-en",
                r"\d{5,6}", (9, 12)),
    CountryInfo("AU", "Australia", ("australia",), "AUD", ("A$", "AU$", "AUD", "$"),
                "A$", "en-AU", "Australia/Sydney", "au", "au-en", r"\d{4}", (9, 11)),
    CountryInfo("CA", "Canada", ("canada",), "CAD", ("C$", "CA$", "CAD", "$"),
                "C$", "en-CA", "America/Toronto", "ca", "ca-en",
                r"[A-Z]\d[A-Z]\s?\d[A-Z]\d", (10, 11)),
    CountryInfo("SG", "Singapore", ("singapore",), "SGD", ("S$", "SGD", "$"),
                "S$", "en-SG", "Asia/Singapore", "sg", "sg-en", r"\d{6}", (8, 10)),
    CountryInfo("IE", "Ireland", ("ireland",), "EUR", ("€", "EUR"),
                "€", "en-IE", "Europe/Dublin", "ie", "ie-en", r"[A-Z]\d{2}\s?[A-Z\d]{4}", (9, 12)),
    CountryInfo("NZ", "New Zealand", ("new zealand",), "NZD", ("NZ$", "NZD", "$"),
                "NZ$", "en-NZ", "Pacific/Auckland", "nz", "nz-en", r"\d{4}", (8, 11)),
    CountryInfo("ZA", "South Africa", ("south africa",), "ZAR", ("R", "ZAR"),
                "R", "en-ZA", "Africa/Johannesburg", "za", "za-en", r"\d{4}", (9, 11)),
    CountryInfo("SA", "Saudi Arabia", ("saudi arabia", "ksa"), "SAR", ("SAR", "SR"),
                "SAR ", "en-SA", "Asia/Riyadh", "sa", "xa-en", r"\d{5}", (9, 12)),
    CountryInfo("DE", "Germany", ("germany", "deutschland"), "EUR", ("€", "EUR"),
                "€", "de-DE", "Europe/Berlin", "de", "de-de", r"\d{5}", (9, 13)),
    CountryInfo("FR", "France", ("france",), "EUR", ("€", "EUR"),
                "€", "fr-FR", "Europe/Paris", "fr", "fr-fr", r"\d{5}", (9, 12)),
    CountryInfo("NL", "Netherlands", ("netherlands", "nederland"), "EUR", ("€", "EUR"),
                "€", "nl-NL", "Europe/Amsterdam", "nl", "nl-nl", r"\d{4}\s?[A-Z]{2}", (9, 12)),
)}


class UnknownCountry(ValueError):
    pass


def is_known_region(code: str) -> bool:
    """True for any ISO code phonenumbers can parse numbers for."""
    return (code or "").upper() in phonenumbers.SUPPORTED_REGIONS


@lru_cache(maxsize=None)
def country(code: str) -> CountryInfo:
    """Facts for an ISO 3166-1 alpha-2 code; neutral defaults when not tabled."""
    code = (code or "").strip().upper()
    if code in _TABLE:
        return _TABLE[code]
    if not is_known_region(code):
        raise UnknownCountry(f"unknown country code {code!r} (expected ISO 3166-1 alpha-2)")
    return CountryInfo(code=code, name=code, google_gl=code.lower())


def tabled_countries() -> list[str]:
    return sorted(_TABLE)


@lru_cache(maxsize=None)
def currency_pattern(currency: str) -> str:
    """Regex fragment for a currency marker written before an amount."""
    info = next((c for c in _TABLE.values() if c.currency == currency), None)
    symbols = info.currency_symbols if info else ((currency,) if currency else ())
    if not symbols:
        return r"(?!)"   # matches nothing: no currency, no prices
    parts = []
    for symbol in symbols:
        escaped = re.escape(symbol)
        # Letter codes need word boundaries ("INR", "Rs."); signs do not ("₹").
        if symbol[0].isalpha():
            escaped = r"\b" + escaped + (r"\b" if symbol[-1].isalpha() else "")
            if symbol == "Rs.":
                escaped = r"\bRs\.?"
        parts.append(escaped)
    return r"(?:" + "|".join(parts) + r")\s?"


def format_money(amount: float, currency: str) -> str:
    """'₹25,000' / '$250' / 'AED 500' -- amounts as people there write them."""
    info = next((c for c in _TABLE.values() if c.currency == currency), None)
    prefix = info.money_prefix if info else (f"{currency} " if currency else "")
    return f"{prefix}{amount:,.0f}"
