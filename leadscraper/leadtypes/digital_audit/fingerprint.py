"""What a site runs, by the rules in ``fingerprints.yaml``, and whether it is outdated."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml

RULES_PATH = Path(__file__).with_name("fingerprints.yaml")


@dataclass
class Detected:
    name: str
    category: str
    version: str = ""
    outdated: bool = False
    #: Why it is outdated (the rule's ``eol`` note), when it is.
    why: str = ""
    #: What gave it away: "meta generator", "header server", "script", "html".
    evidence: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} {self.version}".strip()


@dataclass
class _Rule:
    name: str
    category: str
    meta: list[re.Pattern]
    html: list[re.Pattern]
    scripts: list[re.Pattern]
    headers: dict[str, re.Pattern]
    outdated_below: str | None
    outdated: bool
    eol: str


def _compile(patterns: Any) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in (patterns or [])]


@lru_cache(maxsize=1)
def rules() -> tuple[str, tuple[_Rule, ...]]:
    """(verified date, rules) from the data file."""
    data = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8")) or {}
    out = []
    for name, spec in (data.get("technologies") or {}).items():
        out.append(_Rule(
            name=name, category=str(spec.get("category") or ""),
            meta=_compile(spec.get("meta_generator")), html=_compile(spec.get("html")),
            scripts=_compile(spec.get("scripts")),
            headers={k.lower(): re.compile(v, re.I) for k, v in (spec.get("headers") or {}).items()},
            outdated_below=str(spec["outdated_below"]) if spec.get("outdated_below") else None,
            outdated=bool(spec.get("outdated")), eol=str(spec.get("eol") or "")))
    return str(data.get("verified") or ""), tuple(out)


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(piece) for piece in re.findall(r"\d+", version or "")[:4])


def _older(version: str, floor: str) -> bool:
    have, want = version_tuple(version), version_tuple(floor)
    if not have or not want:
        return False
    width = max(len(have), len(want))
    return have + (0,) * (width - len(have)) < want + (0,) * (width - len(want))


def _version(match: re.Match) -> str:
    return (match.group(1) if match.groups() else "") or ""


def detect(html: str, headers: Mapping[str, str] | None = None, *,
           generators: list[str] | None = None, script_urls: list[str] | None = None,
           thresholds: Mapping[str, str] | None = None) -> list[Detected]:
    """Every technology the page shows, in rule order, each with its version and verdict.

    ``generators``: the page's ``<meta name="generator">`` contents;
    ``script_urls``: its ``<script src>`` and stylesheet ``href`` URLs;
    ``thresholds``: config overrides of ``outdated_below`` by technology name.
    """
    headers = {k.lower(): str(v) for k, v in (headers or {}).items()}
    generators = [g.strip() for g in generators or []]
    script_urls = script_urls or []
    overrides = {k.casefold(): v for k, v in (thresholds or {}).items()}
    found: list[Detected] = []
    for rule in rules()[1]:
        version, evidence = "", ""
        for pattern in rule.meta:
            for generator in generators:
                match = pattern.search(generator)
                if match:
                    evidence = evidence or "meta generator"
                    version = version or _version(match)
        for header, pattern in rule.headers.items():
            match = pattern.search(headers[header]) if header in headers else None
            if match:
                evidence = evidence or f"header {header}"
                version = version or _version(match)
        for pattern in rule.scripts:
            for url in script_urls:
                match = pattern.search(url)
                if match:
                    evidence = evidence or "script"
                    version = version or _version(match)
        if not evidence and any(pattern.search(html) for pattern in rule.html):
            evidence = "html"
        if not evidence:
            continue
        version = version.strip(".")
        floor = overrides.get(rule.name.casefold(), rule.outdated_below)
        outdated = rule.outdated or bool(version and floor and _older(version, floor))
        why = ""
        if outdated:
            why = (rule.eol if rule.outdated or floor == rule.outdated_below
                   else f"older than {floor} (this run's threshold)")
        found.append(Detected(rule.name, rule.category, version, outdated, why, evidence))
    return found
