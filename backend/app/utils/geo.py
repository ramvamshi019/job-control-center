"""
utils/geo.py
------------
Location-based US filter used at INGEST time so multi-country ATSes
(Workday/Greenhouse/Lever/Ashby/iCIMS/BambooHR/SmartRecruiters/Jobvite)
never persist non-US postings to the DB.

Mirrors dashboard/app.py's dashboard-side `is_non_us` -- same rules,
same country/suffix lists -- so what the crawler drops matches what
the dashboard would have hidden anyway. Doing it at ingest saves DB
writes + crawl cycles on rows the user will never see.

Rules:
  - Full non-US country name anywhere in location string -> drop
  - Trailing lowercase 2-letter ISO code (", de" / ", ca" / ", in") -> drop
    (case-sensitive; US state codes like ", CA" / ", DE" are uppercase in
    every ATS we ingest, verified against the live DB sample)
  - Empty / "Remote" / "United States" / US-state format -> keep
"""
from __future__ import annotations

import re

_NON_US_COUNTRIES = (
    "germany", "deutschland", "united kingdom", "england", "scotland", "wales",
    "ireland", "france", "spain", "italy", "portugal", "netherlands", "belgium",
    "luxembourg", "switzerland", "austria", "sweden", "norway", "finland",
    "denmark", "poland", "czechia", "czech republic", "slovakia", "hungary",
    "romania", "bulgaria", "greece", "turkey", "russia", "ukraine",
    "china", "japan", "south korea", "india", "pakistan", "bangladesh",
    "vietnam", "thailand", "philippines", "indonesia", "malaysia", "singapore",
    "hong kong", "taiwan", "australia", "new zealand", "canada", "mexico",
    "brazil", "argentina", "chile", "colombia", "peru", "israel",
    "united arab emirates", "saudi arabia", "qatar", "egypt",
    "south africa", "nigeria", "kenya",
)
_NON_US_COUNTRY_RE = re.compile(
    r"\b(" + "|".join(re.escape(c) for c in _NON_US_COUNTRIES) + r")\b", re.I,
)

# CASE-SENSITIVE trailing 2-letter ISO country codes. Lowercase = country
# per SmartRecruiters/Workday conventions; US states are uppercase.
_NON_US_LC_SUFFIX_RE = re.compile(
    r",\s*(de|uk|gb|fr|nl|be|ch|at|se|no|fi|dk|pl|cz|sk|hu|ro|bg|gr|tr|ru|ua|"
    r"cn|jp|kr|in|pk|bd|vn|th|ph|sg|hk|tw|au|nz|mx|br|ar|cl|co|il|ae|sa|qa|"
    r"eg|za|ng|ke|ie|pt|es|it|my|ca|id)\s*$"
)


def is_non_us_location(location: str | None) -> bool:
    """True when the location string clearly names a non-US country.
    Ambiguous / empty / US-remote / US-state locations return False so
    US-remote and unstated-country roles stay in the pipeline."""
    if not location:
        return False
    loc = location.strip()
    if not loc:
        return False
    if _NON_US_LC_SUFFIX_RE.search(loc):
        return True
    if _NON_US_COUNTRY_RE.search(loc):
        return True
    return False
