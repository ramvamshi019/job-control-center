"""
tests/test_locations.py
-----------------------
Regression tests for utils/locations.canonicalize().
"""
import pytest

from app.utils.locations import canonicalize


@pytest.mark.parametrize("loc, expected_state", [
    ("New York, NY",                  "NY"),
    ("San Francisco, CA",             "CA"),
    ("Austin, TX",                    "TX"),
    ("Boston, MA (Remote)",           "MA"),
    ("Remote — Texas",                "TX"),
    ("NYC",                           "NY"),
    ("SF Bay Area",                   "CA"),
    ("Washington, DC",                "DC"),
    ("Wilmington, DE",                "DE"),   # Delaware (US) not Germany
])
def test_us_locations_get_state_code(loc, expected_state):
    _, state, _ = canonicalize(loc)
    assert state == expected_state, f"Expected {expected_state} for {loc!r}, got {state}"


@pytest.mark.parametrize("loc", [
    "", None, "Anywhere", "Wernau (Neckar), BW, de", "London, gb",
])
def test_non_us_or_empty_no_state(loc):
    _, state, _ = canonicalize(loc)
    assert state is None


def test_remote_flag():
    _, _, remote = canonicalize("Remote")
    assert remote is True
    _, _, remote = canonicalize("New York, NY (Remote)")
    assert remote is True
    _, _, remote = canonicalize("Austin, TX")
    assert remote is False


def test_city_aliases_collapse():
    """'NYC' + 'New York, NY' + 'New York City' -> same canonical."""
    a, s, _ = canonicalize("NYC")
    b, s2, _ = canonicalize("New York City")
    c, s3, _ = canonicalize("New York, NY")
    # All should resolve to NY
    assert s == s2 == s3 == "NY"


def test_display_is_never_none():
    d, _, _ = canonicalize("")
    assert d == ""
    d, _, _ = canonicalize(None)
    assert d == ""
