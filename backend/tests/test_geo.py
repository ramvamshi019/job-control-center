"""
tests/test_geo.py
-----------------
Regression tests for utils/geo.is_non_us_location(). This is a critical
piece: it drops foreign postings at INGEST time (in scheduler.py) AND at
DISPLAY time (dashboard). A silent change in the country lists would
double-hide legit US roles or leak in foreign ones.
"""
import pytest

from app.utils.geo import is_non_us_location


# ---- CLEARLY NON-US -> True ---------------------------------------------

@pytest.mark.parametrize("loc", [
    "Wernau (Neckar), BW, de",       # SmartRecruiters format, lowercase country
    "Toronto, ON, ca",                # Canada
    "Bangalore, Karnataka, in",       # India
    "London, gb",                     # UK short form
    "London, England, gb",            # UK with country
    "Berlin, Germany",                # Full country name
    "Mumbai, India",
    "Paris, fr",
    "Sydney, Australia",
    "Amsterdam, nl",
    "Warsaw, Poland",
])
def test_non_us_returns_true(loc):
    assert is_non_us_location(loc), f"Expected {loc!r} = non-US"


# ---- US -> False (must NOT eat US-state codes) --------------------------

@pytest.mark.parametrize("loc", [
    "San Francisco, CA",
    "Wilmington, DE",                 # Delaware -- collides with 'de'!
    "Los Angeles, CA",                # California -- collides with 'ca'!
    "Denver, CO",                     # Colorado -- collides with 'co'!
    "Indianapolis, IN",               # Indiana  -- collides with 'in'!
    "Little Rock, AR",                # Arkansas -- collides with 'ar'!
    "Boise, ID",                      # Idaho    -- collides with 'id'!
    "Miami, FL",
    "United States",
    "Remote, USA",
    "Sunnyvale, CA, us",              # SmartRecruiters US format
    "Remote",                         # Ambiguous -> not marked non-US
    "",                               # Empty -> not marked non-US
    None,                             # None -> not marked non-US
])
def test_us_and_ambiguous_return_false(loc):
    assert not is_non_us_location(loc), f"Expected {loc!r} = US or ambiguous"


# ---- CASE SENSITIVITY on the 2-letter suffix ----------------------------

def test_uppercase_de_is_delaware_not_germany():
    """The uppercase-vs-lowercase suffix rule is the whole trick that keeps
    US states from being false-positived. Regression-protect it."""
    assert not is_non_us_location("Wilmington, DE")   # US state
    assert is_non_us_location("Wernau, de")           # Germany
