"""Characterisation against frozen copies of the real lead stores.

The baseline databases live in ``data/characterisation/`` (gitignored; create
them with ``python tests/characterisation/freeze.py``). Every test opens a
*copy*: LeadStore writes its schema on open, and the originals are precious.
The committed snapshots (golden/real_*.json) hold only ids, scores, tiers,
counts and row hashes.

When the baseline is absent (CI, a fresh clone) these tests skip.
"""

import json

import pytest

from snapshot_lib import GOLDEN_DIR, PROFILES, baseline_dir, real_snapshot, updating

pytestmark = pytest.mark.characterisation


@pytest.fixture(scope="module", params=sorted(PROFILES))
def snapshots(request, tmp_path_factory):
    profile = request.param
    baseline = baseline_dir() / PROFILES[profile][2]
    if not baseline.exists():
        pytest.skip(f"no frozen baseline at {baseline} (python tests/characterisation/freeze.py)")
    golden_path = GOLDEN_DIR / f"real_{profile}.json"
    actual = real_snapshot(profile, baseline, tmp_path_factory.mktemp(profile))
    if updating():
        golden_path.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n",
                               encoding="utf-8")
    if not golden_path.exists():
        pytest.fail(f"{golden_path.name} missing -- run freeze.py --snapshot-only")
    expected = json.loads(golden_path.read_text(encoding="utf-8"))
    return profile, json.loads(json.dumps(actual)), expected


def test_snapshot_was_taken_from_this_baseline(snapshots):
    _, actual, expected = snapshots
    assert actual["baseline_fingerprint"] == expected["baseline_fingerprint"], (
        "the frozen baseline DB is not the one this snapshot was taken from; "
        "re-freeze with freeze.py rather than editing the snapshot")


def test_export_columns_and_row_count(snapshots):
    _, actual, expected = snapshots
    assert actual["export"]["headers"] == expected["export"]["headers"]
    assert actual["export"]["lead_count"] == expected["export"]["lead_count"]


def test_export_rows_and_order(snapshots):
    """Every exported row, in export order, hashes the same (sort order included)."""
    _, actual, expected = snapshots
    got = actual["export"]["row_digests_in_export_order"]
    want = expected["export"]["row_digests_in_export_order"]
    first_diff = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), None)
    assert got == want, f"{len(got)} rows vs {len(want)}; first differing position: {first_diff}"


def test_scores(snapshots):
    _, actual, expected = snapshots
    got, want = actual["export"]["scores"], expected["export"]["scores"]
    changed = {lead_id: (want.get(lead_id), score) for lead_id, score in got.items()
               if want.get(lead_id) != score}
    assert not changed, f"{len(changed)} scores changed, e.g. {dict(list(changed.items())[:5])}"


def test_venue_tiers(snapshots):
    profile, actual, expected = snapshots
    if "tiers" not in expected["export"]:
        pytest.skip(f"{profile} is not a venue run")
    got, want = actual["export"]["tiers"], expected["export"]["tiers"]
    changed = {lead_id: (want.get(lead_id), tier) for lead_id, tier in got.items()
               if want.get(lead_id) != tier}
    assert not changed, f"{len(changed)} tiers changed, e.g. {dict(list(changed.items())[:5])}"


def test_dedup_replay(snapshots):
    """Replaying the store into an empty one makes the same merge/reject decisions."""
    _, actual, expected = snapshots
    assert actual["dedup"] == expected["dedup"]
