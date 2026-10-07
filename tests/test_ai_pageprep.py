"""Token-lean page preparation (Phase 5.6 acceptance): >= 50 % fewer input tokens than
raw HTML on the fixture pages, with no loss of the target fields.

The fixtures (``tests/fixtures/pages``) are fictional businesses -- ``.example``
domains, fictional phone ranges -- in the chrome real sites carry: inline CSS
and tag-manager scripts, JSON config blobs, navigation menus, cookie banners,
newsletter pop-ups, SVG icons, galleries, ads, JSON-LD.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from leadscraper.ai.grounding import SourceText
from leadscraper.ai.pageprep import chunk, prepare
from leadscraper.ai.prices import estimate_tokens

PAGES = Path(__file__).parent / "fixtures" / "pages"

#: page -> (region, the fields an extraction must still be able to read)
TARGETS = {
    "clinic_home.html": ("US", ["Lakeshore Family Dental", "(512) 555-0142",
                                "hello@lakeshoredental.example",
                                "4100 Lakeshore Blvd, Suite 210, Austin, TX 78701"]),
    "caterer_contact.html": ("IN", ["Saffron Leaf Caterers", "+91 98200 00417",
                                    "events [at] saffronleaf [dot] example",
                                    "Shop 4, Baner Road, Baner, Pune 411045"]),
    "directory_v1_page1.html": ("IN", ["Bright Smile Dental Studio", "98200 00101",
                                       "Hillside Dental Practice", "Pashan Road, Pashan, Pune",
                                       "https://brightsmile.example/"]),
    "directory_v1_page2.html": ("IN", ["Ivory Dental Lounge", "98200 00114",
                                       "Hinjewadi Phase 1, Pune"]),
    "directory_v2_page1.html": ("IN", ["Pearl Orthodontics", "98200 00104",
                                       "3rd Floor, FC Road, Shivajinagar, Pune"]),
}


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_preparation_halves_the_tokens_and_keeps_the_target_fields(name):
    html = (PAGES / name).read_text(encoding="utf-8")
    page = prepare(html, "https://site.example/")
    assert page.raw_tokens == estimate_tokens(html)
    assert page.tokens <= 0.5 * page.raw_tokens, (
        f"{name}: {page.tokens} tokens of {page.raw_tokens} ({page.saving:.0%} saved)")
    region, fields = TARGETS[name]
    for value in fields:
        assert value in page.markdown, f"{name}: lost {value!r}"
    # Phones and emails a model reads off the Markdown are the ones grounding accepts.
    source = SourceText(page.source_text, region)
    phone = next(v for v in fields if any(c.isdigit() for c in v) and "@" not in v
                 and "Blvd" not in v and "Road" not in v and "Floor" not in v
                 and "Phase" not in v and "http" not in v)
    assert source.has_phone(phone)


def test_saving_across_all_fixtures_is_reported():
    """The figure the Phase 5 report quotes."""
    raw = prepared = 0
    for name in TARGETS:
        page = prepare((PAGES / name).read_text(encoding="utf-8"), "https://site.example/")
        raw, prepared = raw + page.raw_tokens, prepared + page.tokens
    assert prepared / raw < 0.5


def test_boilerplate_goes_but_contacts_in_headers_and_footers_stay():
    page = prepare((PAGES / "clinic_home.html").read_text(encoding="utf-8"),
                   "https://lakeshoredental.example/")
    md = page.markdown
    assert "We use cookies" not in md and "Subscribe to our newsletter" not in md
    assert "gtm.js" not in md and "font-0.woff2" not in md and "M0 0" not in md
    assert "Sitemap" not in md and "Home &raquo;" not in md          # nav and breadcrumb
    assert "[(512) 555-0142](tel:+15125550142)" in md                 # header bar
    assert "Structured data:" in md and '"telephone":"+1-512-555-0142"' in md
    # Social profiles are informative links; in-site links keep their text only.
    assert "https://www.instagram.com/lakeshoredental_example/" in md
    assert "/services/0/" not in md
    # The original text, every href included, is what answers are grounded in.
    assert "mailto:hello@lakeshoredental.example" in page.source_text
    assert "We use cookies" in page.source_text


def test_repeated_lines_are_dropped():
    html = "<html><body>" + "<p>Call 98200 00417</p>" * 5 + "<h2>Menu</h2><h2>Menu</h2></body></html>"
    md = prepare(html).markdown
    assert md.count("Call 98200 00417") == 1
    assert md.count("## Menu") == 2                                   # headings are kept


def test_chunks_fit_the_context_and_lose_nothing():
    lines = [f"## Section {i}\n" + ("word " * 300) for i in range(40)]
    text = "\n".join(lines)
    pieces = chunk(text, max_tokens=1000)
    assert len(pieces) > 1
    assert all(estimate_tokens(p) <= 1000 for p in pieces)
    assert "".join(p.replace("\n", "") for p in pieces) == text.replace("\n", "")
    assert chunk("short", 1000) == ["short"] and chunk("", 1000) == []
