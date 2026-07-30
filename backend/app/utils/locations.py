"""
utils/locations.py
------------------
Canonicalize the mess of location strings coming out of ATS APIs so
downstream code (US filter, dashboard grouping, location display) can
compare and group reliably.

Real-world variance we're taming (all these are the SAME place):
    "New York, NY"
    "New York City"
    "NYC"
    "New York, New York, United States"
    "New York, NY (Remote)"
    "Remote — NY"

Design goals:
- Cheap (no external API calls, no geo data files)
- Zero-config (works out of the box for the top ~150 US metros/states)
- Symmetric with utils/geo.is_non_us_location -- what the geo module
  keeps, this module normalizes; what geo drops, this ignores.

Returns a (canonical_display, US_state_code, is_remote) tuple. state=None
means we couldn't confidently identify a state.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

# US state abbreviations + names. Order matters for _STATE_RE: longer names
# first so "North Carolina" wins over "Carolina" (also handles "New York" vs "York").
_STATE_TABLE = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

# City -> canonical (city, state) for the common variations.
_CITY_ALIASES = {
    "nyc":              ("New York", "NY"),
    "new york city":    ("New York", "NY"),
    "manhattan":        ("New York", "NY"),
    "brooklyn":         ("New York", "NY"),
    "sf":               ("San Francisco", "CA"),
    "san francisco bay area": ("San Francisco", "CA"),
    "bay area":         ("San Francisco", "CA"),
    "silicon valley":   ("San Francisco", "CA"),
    "la":               ("Los Angeles", "CA"),
    "los angeles":      ("Los Angeles", "CA"),
    "dc":               ("Washington", "DC"),
    "washington dc":    ("Washington", "DC"),
    "washington, dc":   ("Washington", "DC"),
}

_STATE_NAMES_LC = {v.lower(): k for k, v in _STATE_TABLE.items()}
# Sort by length desc so longer names ("north carolina") match before shorter ("carolina").
_STATE_NAME_RE = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in sorted(_STATE_NAMES_LC, key=len, reverse=True)) + r")\b",
    re.I,
)
# Match ", XX" uppercase 2-letter US state suffix (won't collide with country codes like ", de" — that's a separate regex in utils/geo)
_STATE_CODE_RE = re.compile(r",\s*([A-Z]{2})\b")

_REMOTE_RE = re.compile(r"\b(remote|work[\s-]?from[\s-]?home|wfh|distributed|anywhere)\b", re.I)


def canonicalize(location: str | None) -> Tuple[Optional[str], Optional[str], bool]:
    """Parse an ATS location string into (display, state_code, is_remote).

    display: cleaned "City, ST" or "State" or "Remote" or "" -- never None
             (empty string when we can't parse anything).
    state_code: 2-letter US state code ("CA") or None if not confidently US.
    is_remote: True when the string contains a remote-work signal.
    """
    if not location:
        return "", None, False
    s = location.strip()
    if not s:
        return "", None, False

    lc = s.lower()
    remote = bool(_REMOTE_RE.search(lc))

    # 1) City alias hit -- unambiguous
    for alias, (city, st) in _CITY_ALIASES.items():
        if alias in lc:
            disp = f"{city}, {st}" + (" (Remote)" if remote else "")
            return disp, st, remote

    # 2) Uppercase state code suffix -- most ATSes produce "City, ST"
    m = _STATE_CODE_RE.search(s)
    if m:
        code = m.group(1).upper()
        if code in _STATE_TABLE:
            city = s[:m.start()].split(",")[-1].strip() or s[:m.start()].strip()
            city = city.split("(")[0].strip().rstrip(",")
            disp = (f"{city}, {code}" if city else _STATE_TABLE[code])
            if remote and "(Remote)" not in disp:
                disp += " (Remote)"
            return disp, code, remote

    # 3) Full state name mentioned in the string
    m2 = _STATE_NAME_RE.search(lc)
    if m2:
        code = _STATE_NAMES_LC[m2.group(1).lower()]
        disp = _STATE_TABLE[code]
        if remote:
            disp += " (Remote)"
        return disp, code, remote

    # 4) Pure remote with no state hint
    if remote:
        return "Remote", None, True

    # 5) Can't confidently identify -- return the original stripped string
    return s, None, remote
