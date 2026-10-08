"""Amounts and rounds as headlines state them.

"Rs 22 crore", "₹12.5 Cr", "$1.1 Mn", "USD 10 million", "€2.5M", "£1m",
"₹8-10 cr" (a range: its lower end), "22 crore rupees". The text is kept as
stated; ``value`` is the amount in units of its currency (a crore is 10^7, a
lakh 10^5), for sorting and thresholds only. Nothing is converted between
currencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CURRENCY = {"₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "rupees": "INR",
             "$": "USD", "us$": "USD", "usd": "USD", "dollars": "USD",
             "€": "EUR", "eur": "EUR", "euros": "EUR",
             "£": "GBP", "gbp": "GBP", "pounds": "GBP",
             "aed": "AED", "dh": "AED", "dhs": "AED", "dirhams": "AED",
             "s$": "SGD", "sgd": "SGD", "a$": "AUD", "aud": "AUD", "c$": "CAD", "cad": "CAD"}
_UNIT = {"crore": 1e7, "crores": 1e7, "cr": 1e7, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5,
         "lacs": 1e5, "million": 1e6, "mn": 1e6, "mln": 1e6, "m": 1e6, "billion": 1e9,
         "bn": 1e9, "b": 1e9, "thousand": 1e3, "k": 1e3}
#: Units that only India writes, so an amount in them without a currency is rupees.
_INDIAN = {"crore", "crores", "cr", "lakh", "lakhs", "lac", "lacs"}

_NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_UNITS = r"crores?|cr|lakhs?|lacs?|million|mn|mln|billion|bn|thousand|m|b|k"
_PREFIXED = re.compile(
    rf"(?<![\w$])(?P<cur>US\$|S\$|A\$|C\$|₹|\$|€|£|Rs\.?|INR|USD|EUR|GBP|AED|SGD|AUD|CAD|Dhs?)"
    rf"\s?(?P<num>{_NUM})(?:\s?(?:-|–|to)\s?(?P<num2>{_NUM}))?"
    rf"(?:\s?(?P<unit>{_UNITS})\b\.?)?", re.I)
_SUFFIXED = re.compile(
    rf"(?<![\w.,])(?P<num>{_NUM})(?:\s?(?:-|–|to)\s?(?P<num2>{_NUM}))?\s?(?P<unit>{_UNITS})\b"
    rf"(?:\s+(?P<cur>rupees|dollars|euros|pounds|dirhams))?", re.I)

_ROUND = re.compile(
    r"\b(?P<round>pre[- ]?seed|seed|angel|pre[- ]?series[- ][a-h]\b|series[- ][a-h]\b|"
    r"bridge|growth|venture debt|debt|pre[- ]?ipo|ipo)\b", re.I)


@dataclass(frozen=True)
class Amount:
    text: str
    value: float
    currency: str

    def as_dict(self) -> dict:
        return {"text": self.text, "value": self.value, "currency": self.currency}


def _number(text: str) -> float:
    return float(text.replace(",", ""))


def parse_amount(text: str) -> Amount | None:
    """The first amount stated in ``text``, or None."""
    text = text or ""
    candidates = [m for pattern in (_PREFIXED, _SUFFIXED) for m in pattern.finditer(text)]
    for match in sorted(candidates, key=lambda m: m.start()):
        unit = (match.group("unit") or "").lower()
        currency = _CURRENCY.get((match.group("cur") or "").lower(), "")
        if not currency and unit in _INDIAN:
            currency = "INR"
        if not currency:
            continue                           # "10 million users" is not money
        if not unit and _number(match.group("num")) < 1000:
            continue                           # "$5 off" is not a round
        value = _number(match.group("num")) * _UNIT.get(unit, 1.0)
        return Amount(" ".join(match.group(0).split()).rstrip(".,"), value, currency)
    return None


def parse_round(text: str) -> str | None:
    """"Series A", "Pre-Series A", "Seed", "Pre-Seed", "Debt"… as named, title-cased."""
    match = _ROUND.search(text or "")
    if match is None:
        return None
    words = re.sub(r"^pre(?=[a-z])", "pre ",
                   re.sub(r"[- ]+", " ", match.group("round").strip()).lower()).split()
    pre = words[0] == "pre" if len(words) > 1 else False
    words = words[1:] if pre else words
    named = " ".join(w.upper() if len(w) == 1 or w == "ipo" else w.capitalize() for w in words)
    return f"Pre-{named}" if pre else named
