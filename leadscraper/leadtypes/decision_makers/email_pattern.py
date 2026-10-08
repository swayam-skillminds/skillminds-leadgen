"""The optional email pattern: the only inference A.2.5 allows, always labelled.

Off by default. An address is inferred for a named person only when:

1. at least one **published personal** address at the same domain reveals the
   pattern -- its local part is exactly one pattern applied to a person we
   know by name ("priya.shah@" for Priya Shah: ``first.last``); role mailboxes
   (info@, sales@ …) reveal nothing, and published addresses that disagree
   about the pattern cancel it;
2. the domain is the company's own (not a free-mail or shared host) and its
   **MX records** say it receives mail (a null MX, RFC 7505, says it doesn't).
   That is the only check: no SMTP conversation, ever.

Inferred addresses go to the ``Inferred Email (unverified)`` column only:
never into published contacts, never into identity keys, never used to match.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ...core.identity import is_shared_host
from ...sources.people.roles import ROLE_MAILBOXES, name_parts, person_name

log = logging.getLogger(__name__)

#: pattern name -> local part from (first, last).
PATTERNS: dict[str, Callable[[str, str], str]] = {
    "first.last": lambda f, s: f"{f}.{s}",
    "firstlast": lambda f, s: f"{f}{s}",
    "first_last": lambda f, s: f"{f}_{s}",
    "first-last": lambda f, s: f"{f}-{s}",
    "flast": lambda f, s: f"{f[0]}{s}",
    "f.last": lambda f, s: f"{f[0]}.{s}",
    "first": lambda f, s: f,
    "firstl": lambda f, s: f"{f}{s[0]}",
    "first.l": lambda f, s: f"{f}.{s[0]}",
    "last.first": lambda f, s: f"{s}.{f}",
    "lastfirst": lambda f, s: f"{s}{f}",
    "last": lambda f, s: s,
}
@dataclass(frozen=True)
class Pattern:
    pattern: str
    domain: str
    #: The published addresses that revealed it.
    examples: tuple[str, ...] = ()

    def apply(self, name: str) -> str | None:
        parts = name_parts(name)
        if parts is None:
            return None
        return f"{PATTERNS[self.pattern](*parts)}@{self.domain}"

    @property
    def confidence(self) -> float:
        """0.6 from one published example, 0.75 from two or more that agree."""
        return 0.75 if len(self.examples) >= 2 else 0.6


@dataclass
class Learned:
    pattern: Pattern | None
    #: Why there is none: no_personal_address | conflicting_patterns | shared_domain.
    reason: str = ""
    seen: list[str] = field(default_factory=list)


def learn(emails: Iterable[str], people: Iterable[str], domain: str | None) -> Learned:
    """The pattern published addresses at ``domain`` reveal for these people, if one does."""
    domain = (domain or "").casefold().strip(".")
    if not domain or "." not in domain or is_shared_host(domain):
        return Learned(None, "shared_domain")
    names = [p for p in dict.fromkeys(people) if p and person_name(p) and name_parts(p)]
    evidence: dict[str, list[str]] = {}
    seen = []
    for email in dict.fromkeys(e.strip().casefold() for e in emails if e):
        local, _, host = email.partition("@")
        if host != domain or not local or local in ROLE_MAILBOXES:
            continue
        seen.append(email)
        hits = set()
        for name in names:
            first, last = name_parts(name)
            hits |= {key for key, render in PATTERNS.items() if render(first, last) == local}
        if len(hits) == 1:                  # one reading of the address, for one person
            evidence.setdefault(hits.pop(), []).append(email)
    if not evidence:
        return Learned(None, "no_personal_address", seen)
    if len(evidence) > 1:
        return Learned(None, "conflicting_patterns", seen)
    (pattern, examples), = evidence.items()
    return Learned(Pattern(pattern, domain, tuple(examples)), "", seen)


def mx_hosts(domain: str, *, resolve=None) -> list[str] | None:
    """The domain's mail exchangers; [] when it takes no mail (no MX, null MX, no such
    domain); None when DNS couldn't answer (try another day). No SMTP, ever."""
    try:
        import dns.exception
        import dns.resolver
    except ImportError:                              # pragma: no cover - a dependency
        log.warning("dnspython is not installed: the email pattern can't check MX records")
        return None
    resolve = resolve or (lambda name: dns.resolver.resolve(name, "MX", lifetime=8.0))
    try:
        answer = resolve(domain)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except (dns.exception.DNSException, OSError):
        return None
    hosts = sorted({str(r.exchange).rstrip(".") for r in answer})
    return [h for h in hosts if h]                   # a null MX ("." -> "") takes no mail
