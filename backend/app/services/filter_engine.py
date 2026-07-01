"""
services/filter_engine.py
-------------------------
HARD filters. These are pass/fail gates applied BEFORE scoring.

`evaluate(job)` returns a FilterResult:
    passed: bool
    reason: str   (why it was rejected, "" if passed)

A failed job is still stored, but marked Rejected with a reason so you can audit
why the system filtered it (Stats page + Rejected page).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.config import settings
from app.models.job import Job
from app.utils.text import normalize, term_in

# ---- Title kill-words: senior / management roles we don't want ----
TITLE_BLOCK = [
    "senior", "sr.", "sr ", "staff", "principal", "lead ", "manager",
    "director", "architect", "head of", "head,", "vp ", "vice president",
]

# ---- Level too senior for a 0–3yr candidate. "II" and below pass; "III"/"IV"
# (typically 5–8 yrs) do not. Matched as whole tokens (term_in) so "iii" can't
# hit inside another word, and "ii" is never blocked.
SENIOR_LEVEL = ["iii", "iv", "level 3", "level 4", "level iii", "level iv"]

# ---- Internships / co-ops — Ram wants full-time roles, not interns. ----
INTERN_SIGNALS = ["intern", "internship", "co-op", "coop"]

# ---- OPT/CPT "training & placement" body-shop listings — not real jobs.
# (e.g. "Java/UI/OBIEE/ETL training opportunity for OPT", "OBIEE/ETL job for
# OPT/CPT".) These crammed-tech bench listings flood in from staffing shells.
SCAM_SIGNALS = [
    "training and placement", "training & placement", "training/placement",
    "opt/cpt", "opt / cpt", "cpt/opt", "opt and cpt", "opt & cpt", "bench sales",
]

# ---- Description kill-phrases: citizenship / clearance / no-sponsorship ----
DESC_BLOCK = [
    "us citizen required", "u.s. citizen required", "must be a us citizen",
    "must be us citizen", "must be a u.s. citizen", "u.s. citizenship is required",
    "security clearance", "active clearance", "secret clearance", "top secret",
    "ts/sci",
    "no visa sponsorship", "we do not sponsor", "we are unable to sponsor",
    "sponsorship not available", "unable to sponsor",
    "must be authorized to work without sponsorship",
    "must be authorized to work in the u.s. without sponsorship",
    "now or in the future without sponsorship",
    "without the need for sponsorship",
    "c2c", "corp to corp", "corp-to-corp", "contract only", "1099",
    "temporary", "part-time", "part time",
]

# ---- Years-of-experience kill patterns ----
YEARS_BLOCK = [
    r"\b5\+?\s*years", r"\b6\+?\s*years", r"\b7\+?\s*years",
    r"\b8\+?\s*years", r"\b9\+?\s*years", r"\b1[0-9]\+?\s*years",
    r"minimum (of )?5 years", r"at least 5 years",
]
YEARS_BLOCK_RE = [re.compile(p) for p in YEARS_BLOCK]

# ---- US-only location gating -------------------------------------------------
# Countries / major cities that mean the role is NOT in the US. Matched as whole
# tokens (word boundaries) so "india" won't hit inside another word. A posting
# is only rejected when it names a non-US place AND has no US signal — that way
# multi-location roles like "London • New York, NY • United States" still pass.
NON_US_TERMS = [
    # countries
    "india", "canada", "singapore", "united kingdom", "japan", "australia",
    "ireland", "france", "germany", "netherlands", "spain", "italy", "brazil",
    "mexico", "china", "south korea", "korea", "israel", "poland", "sweden",
    "switzerland", "portugal", "argentina", "colombia", "philippines",
    "indonesia", "vietnam", "nigeria", "south africa", "united arab emirates",
    "new zealand", "norway", "denmark", "finland", "belgium", "austria",
    "czech", "czechia", "romania", "ukraine", "egypt", "malaysia", "thailand",
    "turkey", "greece", "hungary", "chile", "peru", "costa rica", "kenya",
    "pakistan", "bangladesh", "sri lanka", "taiwan", "scotland", "england",
    "iceland", "luxembourg", "estonia", "latvia", "lithuania", "croatia",
    "serbia", "bulgaria", "slovakia", "slovenia", "morocco", "qatar",
    "saudi arabia", "bahrain", "uruguay", "ecuador", "panama", "guatemala",
    # Canada provinces/territories (a frequent leak via Greenhouse multi-loc)
    "british columbia", "alberta", "ontario", "quebec", "manitoba",
    "saskatchewan", "nova scotia", "new brunswick", "newfoundland",
    "prince edward island", "yukon", "nunavut", "northwest territories",
    # cities / regions
    "london", "paris", "tokyo", "toronto", "bengaluru", "bangalore", "dublin",
    "sydney", "melbourne", "berlin", "munich", "amsterdam", "hyderabad",
    "mumbai", "delhi", "pune", "chennai", "gurgaon", "noida", "vancouver",
    "montreal", "ottawa", "sao paulo", "tel aviv", "seoul", "shanghai",
    "beijing", "shenzhen", "zurich", "geneva", "stockholm", "madrid",
    "barcelona", "milan", "warsaw", "lisbon", "oslo", "copenhagen", "helsinki",
    "brussels", "vienna", "prague", "bucharest", "athens", "budapest",
    "manila", "jakarta", "bangkok", "kuala lumpur", "istanbul", "cairo",
    "lagos", "nairobi", "dubai", "abu dhabi", "auckland", "edinburgh",
    "manchester", "glasgow", "cork", "hamburg", "frankfurt", "lyon", "taipei",
    "hong kong", "emea", "apac", "latam", "reykjavik", "luxembourg city",
    "krakow", "wroclaw", "gdansk", "porto", "valencia", "rotterdam",
    "the hague", "gothenburg", "malmo", "bergen", "aarhus", "tallinn",
    "riga", "vilnius", "zagreb", "belgrade", "sofia", "bratislava",
    # Canada cities
    "calgary", "edmonton", "winnipeg", "halifax", "waterloo", "kitchener",
    "mississauga", "brampton", "burnaby", "markham", "gatineau", "laval",
    "quebec city",
]

# ---- Clearly non-technical roles (Ram targets data/cloud/software eng) -------
# Kept as a backstop, but the PRIMARY relevance gate is now the positive
# TECH_TITLE allowlist below: a job must PROVE it's a tech role to pass. A
# blocklist alone is whack-a-mole — endless non-tech titles ("Facilities
# Maintenance Technician", "CRM Clinical Specialist", "Call Center Validation
# Agent", "Production Planner"…) slip through because they aren't listed.
NON_TECH_TITLE = [
    "recruiter", "sourcer", "talent acquisition", "designer", "marketing",
    "accountant", "bookkeeper", "auditor", "paralegal", "attorney", "counsel",
    "nurse", "physician", "teacher", "barista", "driver", "warehouse",
    "receptionist", "copywriter", "content writer", "social media",
    "account executive", "sales representative", "sales development",
    "business development", "customer success", "customer support",
    "payroll", "salesperson", "brand ", "public relations",
]

# ---- Positive TECH allowlist: a title must match >=1 of these to be relevant.
# Note: bare "engineer" is intentionally NOT here — it matches Quality/Field/
# Mechanical/Industrial/Sales engineers that aren't Ram's target. We require a
# software/data/cloud-qualified phrase instead.
TECH_TITLE = [
    # core software
    "software engineer", "software developer", "software development engineer",
    "software development", "developer", "programmer", "swe", "sde", "sdet",
    "application developer", "applications developer", "application engineer",
    "web developer", "web engineer", "frontend", "front end", "front-end",
    "backend", "back end", "back-end", "full stack", "full-stack", "fullstack",
    "mobile developer", "ios developer", "android developer", "ios engineer",
    "android engineer", "game developer", "embedded engineer", "embedded software",
    "firmware",
    # data
    "data engineer", "data engineering", "data scientist", "data science",
    "data analyst", "data analytics", "data architect", "data platform",
    "data pipeline", "data warehouse", "data infrastructure", "dataops",
    "analytics engineer", "etl developer", "etl engineer", "etl", "elt",
    "big data", "bi developer", "bi engineer", "bi analyst",
    "business intelligence", "database engineer", "database developer",
    "database administrator", "dba",
    # ml / ai
    "machine learning", "ml engineer", "mlops", "ai engineer",
    "artificial intelligence", "deep learning", "nlp engineer",
    "research engineer", "computer vision",
    # cloud / infra / devops
    "cloud engineer", "cloud architect", "cloud developer", "cloud infrastructure",
    "platform engineer", "infrastructure engineer", "systems engineer",
    "site reliability", "sre", "devops", "devsecops", "network engineer",
    "kubernetes", "automation engineer", "release engineer", "build engineer",
    # qa / security
    "qa engineer", "quality assurance engineer", "test engineer", "test automation",
    "security engineer", "application security", "cybersecurity engineer",
    "cyber security engineer", "information security engineer",
    # architecture / integration / other tech
    "solutions architect", "solution architect", "solutions engineer",
    "integration engineer", "integration developer", "salesforce developer",
    "computer scientist", "technical program manager",
]


def looks_tech(title: str) -> bool:
    """True if the (normalized) title names a software/data/cloud tech role."""
    return any(term_in(title, t) for t in TECH_TITLE)
_NON_US_RE = re.compile(
    r"(?<![a-z])(?:" + "|".join(re.escape(t) for t in NON_US_TERMS) + r")(?![a-z])"
)
_US_RE = re.compile(r"(?<![a-z])(?:usa|u\.s\.a|u\.s|us|united states|america)(?![a-z])")

# Major US cities / tech hubs that frequently appear WITHOUT a state code
# ("San Francisco", "Seattle", "Chicago"). Lets strict-US recognize them as US.
# Deliberately excludes names that collide with foreign cities (Cambridge,
# Birmingham, Manchester, Columbia-vs-Colombia handled by exact spelling).
_US_CITIES = [
    "new york", "san francisco", "san francisco bay area", "bay area",
    "silicon valley", "los angeles", "chicago", "seattle", "austin", "boston",
    "denver", "atlanta", "dallas", "houston", "miami", "philadelphia",
    "phoenix", "san diego", "san jose", "portland", "las vegas", "nashville",
    "charlotte", "raleigh", "durham", "pittsburgh", "minneapolis", "detroit",
    "baltimore", "sacramento", "kansas city", "columbus", "cincinnati",
    "cleveland", "milwaukee", "st louis", "st. louis", "saint louis", "tampa",
    "orlando", "jacksonville", "new orleans", "memphis", "oklahoma city",
    "tucson", "albuquerque", "omaha", "tulsa", "fresno", "long beach",
    "oakland", "anaheim", "santa clara", "sunnyvale", "mountain view",
    "palo alto", "menlo park", "cupertino", "redmond", "bellevue", "brooklyn",
    "manhattan", "boulder", "ann arbor", "madison", "plano", "irvine",
    "scottsdale", "reston", "herndon", "mclean", "research triangle",
]
_US_CITY_RE = re.compile(
    r"(?<![a-z])(?:" + "|".join(sorted(_US_CITIES, key=len, reverse=True)) + r")(?![a-z])"
)
# State code preceded by a separator (comma/dash/slash/paren) or at string start,
# so "Austin, TX", "Remote - CA" and "TX - Austin" all register as US.
_US_STATE_RE = re.compile(
    r"(?:^|[,\-/(]\s*)(?:al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|"
    r"mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|"
    r"va|wa|wv|wi|wy|dc)(?![a-z])"
)

# Generic filler that can accompany "remote" in a US posting without making it
# foreign ("USA Remote Worksite", "Remote Nationwide", "Remote - Home Office").
_REMOTE_GENERIC = {
    "us", "usa", "u", "s", "united", "states", "of", "america", "home", "office",
    "based", "anywhere", "nationwide", "hybrid", "onsite", "on", "site", "field",
    "flexible", "worksite", "all", "locations", "hq", "various", "multiple",
    "na", "n", "a", "the", "and", "or", "work", "from",
}


# Foreign country CODES the name-based list misses (e.g. "Brasília - BR",
# "London, GB"). Excludes 2-letter codes that collide with US state abbrevs
# (CA, DE, IN, CO, ID, LA, MA, OR, …) so we never reject a real US location.
FOREIGN_CODES = {
    "br", "gb", "uk", "sg", "au", "nl", "es", "it", "jp", "ie", "pl", "mx",
    "ph", "ae", "ch", "se", "be", "at", "dk", "no", "fi", "pt", "gr", "cz",
    "ro", "hu", "cn", "kr", "za", "ar", "nz", "my", "th", "vn", "tr",
}
_FOREIGN_CODE_END_RE = re.compile(r"[-,/]\s*([a-z]{2})\s*$")


def _has_foreign_code(loc: str) -> bool:
    m = _FOREIGN_CODE_END_RE.search(loc)
    return bool(m and m.group(1) in FOREIGN_CODES)


def _has_us_signal(loc: str) -> bool:
    return bool(_US_RE.search(loc) or _US_STATE_RE.search(loc) or "d.c." in loc)


# Full US state names (+ DC) for a STRICT positive-US check. ("georgia" can also
# be a country, but in job locations it overwhelmingly means the US state.)
_US_STATE_NAMES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
]
_US_STATE_NAME_RE = re.compile(
    r"(?<![a-z])(?:" + "|".join(sorted(_US_STATE_NAMES, key=len, reverse=True)) + r")(?![a-z])"
)


def looks_us_strict(location: str) -> bool:
    """STRICT 'USA only': require a POSITIVE US signal (state code/name, US
    country term, or remote) and no foreign marker. Unlike is_us_location, an
    unknown/foreign-city location (e.g. 'Gurugram') is REJECTED, not allowed."""
    loc = _strip_accents(normalize(location))
    if not loc:
        return False
    if _NON_US_RE.search(loc) or _has_foreign_code(loc):
        return False
    if _has_us_signal(loc) or _US_STATE_NAME_RE.search(loc) or _US_CITY_RE.search(loc):
        return True
    # Remote roles: accept ONLY when nothing foreign/unknown is left after
    # removing "remote". This rejects "Remote - Europe", "Remote UK",
    # "Remote (Buenos Aires)", "Türkiye, Remote" (a stray non-US place name)
    # while keeping bare "Remote" and "Remote - Home Office" as US.
    if "remote" in loc:
        leftover = loc.replace("remote", " ")
        if _has_us_signal(leftover) or _US_STATE_NAME_RE.search(leftover) or _US_CITY_RE.search(leftover):
            return True
        stray = [t for t in re.sub(r"[^a-z]+", " ", leftover).split() if t not in _REMOTE_GENERIC]
        return not stray
    return False


def _strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def is_us_location(location: str) -> bool:
    """Best-effort: is this posting in the US? Unknown/empty => True (let it through)."""
    loc = _strip_accents(normalize(location))
    if not loc:
        return True  # no location info — don't reject here; description may clarify
    if _has_us_signal(loc):
        return True
    if _NON_US_RE.search(loc) or _has_foreign_code(loc):
        return False
    return True  # no foreign marker and no US marker — give benefit of the doubt


@dataclass
class FilterResult:
    passed: bool
    reason: str = ""


def evaluate(job: Job) -> FilterResult:
    title = normalize(job.title)
    desc = normalize(job.description)
    etype = normalize(job.employment_type)

    # 0) US-only location gate. STRICT: require a positive US signal (state,
    # "United States", or remote). This rejects ambiguous Workday multi-location
    # postings ("2 Locations") and foreign cities the name list misses (Bogota,
    # MEX-Corporativo, IND-Trivandrum) instead of giving them benefit of the doubt.
    if settings.us_only and not looks_us_strict(job.location):
        return FilterResult(False, f"Location not clearly US: '{job.location}'")

    # 1) Title-based seniority block.
    for kw in TITLE_BLOCK:
        if kw in title:
            return FilterResult(False, f"Title contains blocked term: '{kw.strip()}'")

    # 1a) Level too senior (III/IV). "II" and below are fine for a 0–3yr fit.
    for kw in SENIOR_LEVEL:
        if term_in(title, kw):
            return FilterResult(False, f"Level too senior: '{kw}'")

    # 1a2) Internships are INCLUDED per Ram's request (he wants intern jobs too).

    # 1a3) OPT/CPT "training & placement" bench-shop listings (title or desc),
    # plus the "<crammed techs> training opportunity for OPT" pattern.
    for kw in SCAM_SIGNALS:
        if kw in title or kw in desc:
            return FilterResult(False, f"OPT-bench/training-placement listing: '{kw}'")
    # "...training" + OPT/CPT/placement, OR a crammed slash-separated tech list
    # ("ETL Informatica/Networking/Salesforce/Devops training") — the body-shop
    # tell. Boeing's "Rotorcraft Training Systems" (0 slashes) is spared.
    if "training" in title and (term_in(title, "opt") or term_in(title, "cpt")
                                or "placement" in title or title.count("/") >= 2):
        return FilterResult(False, "OPT-bench training/placement listing")

    # 1b) Non-technical roles (explicit blocklist backstop).
    for kw in NON_TECH_TITLE:
        if term_in(title, kw.strip()):
            return FilterResult(False, f"Non-technical role: '{kw.strip()}'")

    # 1c) PRIMARY relevance gate — the title must name a tech role we target.
    # Anything that isn't clearly software/data/cloud/ML/devops is dropped.
    if not looks_tech(title):
        return FilterResult(False, "Not a target tech role")

    # 2) Employment-type block. Internships are ALLOWED now (Ram wants intern
    # jobs) — "intern" removed. Contract/C2C/part-time/temp still blocked.
    bad_types = ["contract", "c2c", "part-time", "part time", "temporary", "1099"]
    for bt in bad_types:
        if bt in etype:
            return FilterResult(False, f"Employment type blocked: '{etype}'")

    # 3) Description kill-phrases.
    for phrase in DESC_BLOCK:
        if phrase in desc:
            return FilterResult(False, f"Description contains blocked phrase: '{phrase}'")

    # 4) Years of experience too high.
    for rx in YEARS_BLOCK_RE:
        if rx.search(desc):
            return FilterResult(False, f"Requires too many years: matched /{rx.pattern}/")

    return FilterResult(True, "")
