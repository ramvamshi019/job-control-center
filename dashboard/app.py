"""
dashboard/app.py
----------------
Streamlit dashboard. Talks to the FastAPI backend over HTTP, so START THE
BACKEND FIRST (uvicorn app.main:app --reload from backend/).

Run from the project root:
    streamlit run dashboard/app.py

Pages:
  1. Today's Best Jobs   2. Need Review   3. Approved   4. Applied
  5. Rejected           6. Companies     7. Stats
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

# Read API_BASE_URL from backend/.env if present, else default.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", ".env"))
API = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")

# "Today" means Ram's local calendar day, NOT the server's. The container runs
# in UTC, so a bare datetime.now() would roll "today" over at UTC midnight (7pm
# Central) -- postings from the evening would wrongly count as tomorrow's. Pin
# to an explicit zone (override with JCC_LOCAL_TZ). If the zone can't be loaded
# we fall back to None, i.e. the old system-local behaviour, so the page never
# crashes over a timezone lookup.
try:
    LOCAL_TZ: ZoneInfo | None = ZoneInfo(os.getenv("JCC_LOCAL_TZ", "America/Chicago"))
except (ZoneInfoNotFoundError, ValueError):
    LOCAL_TZ = None


def local_today():
    """Today's date in Ram's zone (or system-local if the zone failed to load)."""
    return datetime.now(LOCAL_TZ).date()


def to_local_date(dt_utc: datetime):
    """Date of a naive-UTC timestamp, seen from Ram's zone."""
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).date()

st.set_page_config(page_title="Job Control Center", page_icon="🎯", layout="wide")

JOB_STATUSES = ["New", "Need Review", "Approved", "Applied", "Follow-up", "Rejected", "Archived"]

# ---------- small API helpers ----------
# The backend shares a 2-vCPU box with the crawler. During a heavy livewatch
# wave the API can stall for tens of seconds even though the query itself is
# ~80ms, so a short timeout turns a slow moment into a red error. Wait longer
# and retry once instead.
API_TIMEOUT = 90
API_RETRIES = 2


def api_get(path: str, **params):
    last_exc = None
    for attempt in range(API_RETRIES):
        try:
            r = requests.get(f"{API}{path}", params=params, timeout=API_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as exc:
            # A 4xx/5xx is a real answer from the API — retrying won't change it.
            st.error(f"API GET {path} failed: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001  (timeout / connection error)
            last_exc = exc
    st.error(
        f"API GET {path} failed after {API_RETRIES} attempts: {last_exc}\n\n"
        "The backend is likely busy behind a crawl wave — retry in a moment."
    )
    return None


def api_patch(path: str, payload: dict):
    try:
        r = requests.patch(f"{API}{path}", json=payload, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        st.error(f"API PATCH {path} failed: {exc}")
        return None


def api_post(path: str, payload: dict | None = None, **params):
    try:
        r = requests.post(f"{API}{path}", json=payload, params=params, timeout=120)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        st.error(f"API POST {path} failed: {exc}")
        return None


def jobs_df(status=None, min_score=0, sponsorship_risk=None, feed_only=False):
    """DataFrame of jobs. `feed_only=True` drops walled + non-US rows for
    discovery views; leave False on history views (Applied/Approved/Rejected)
    so already-actioned records are never hidden retroactively."""
    data = api_get("/jobs/", status=status, min_score=min_score, sponsorship_risk=sponsorship_risk) or []
    if feed_only:
        data = filter_feed(data)
    return pd.DataFrame(data)


# Your REAL résumé file(s) for applications live here (same machine as the
# dashboard). Drop your actual résumé in resumes/master/ — used as-is, no edits.
MASTER_DIR = os.path.join(os.path.dirname(__file__), "..", "resumes", "master")


def my_profile():
    """Your standard application fields, fetched once and cached for the session."""
    if "profile" not in st.session_state:
        st.session_state["profile"] = api_get("/resume/profile") or {}
    return st.session_state["profile"]


def file_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:  # noqa: BLE001
        return None


def master_resume():
    """Your real résumé — the actual file you upload to applications, unchanged.
    Returns the first PDF and DOCX found in resumes/master/."""
    import glob
    pdfs = sorted(glob.glob(os.path.join(MASTER_DIR, "*.pdf")))
    docxs = sorted(glob.glob(os.path.join(MASTER_DIR, "*.docx")))
    return {"pdf": pdfs[0] if pdfs else None, "docx": docxs[0] if docxs else None}


# How heavily each source is aggregated by the big job boards (LinkedIn/Indeed/
# etc.) — i.e. how many other applicants likely see the same posting. Lower =
# better odds for you.
_COMPETITION = {
    "greenhouse": ("high", "🔴", 3), "lever": ("high", "🔴", 3),
    "ashby": ("medium", "🟡", 2),
    "bamboohr": ("low", "🟢", 1), "icims": ("low", "🟢", 1),
    "workday": ("low", "🟢", 1), "smartrecruiters": ("low", "🟢", 1),
    "recruitee": ("low", "🟢", 1), "workable": ("low", "🟢", 1),
}


def competition(source: str):
    """(label, emoji, rank) — rank 1=low competition .. 3=high."""
    return _COMPETITION.get((source or "").lower(), ("unknown", "⚪", 2))


def _parse_dt(s):
    """Parse an ISO timestamp string (naive UTC) → datetime, or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", ""))
    except ValueError:
        return None


def posted_today(job: dict) -> bool:
    """True only when the job's ORIGINAL posting date is *today* (your local
    day). Deliberately NOT the crawler's discovery/pull time.

    Guard against crawl-time fallbacks: several sources (workday, bamboohr)
    stamp posted_at = now() when the ATS doesn't expose a real post date, so
    posted_at ends up microsecond-identical to discovered_at. Those are the pull
    timestamp masquerading as a post date — we do NOT count them as 'today'.
    Sources with no post date at all (icims, most smartrecruiters) have
    posted_at=None and are likewise never flagged."""
    posted = _parse_dt(job.get("posted_at"))
    if posted is None:
        return False
    disc = _parse_dt(job.get("discovered_at"))
    if disc is not None and abs((disc - posted).total_seconds()) < 5:
        return False  # crawl-time fallback stamp, not a real posted date
    # posted_at is stored as naive UTC; compare calendar dates in Ram's zone.
    return to_local_date(posted) == local_today()


def _discovered_today(job: dict) -> bool:
    """True if the crawler FIRST saw this posting during your local day."""
    disc = _parse_dt(job.get("discovered_at"))
    if disc is None:
        return False
    return to_local_date(disc) == local_today()


def posted_freshness(job: dict) -> str | None:
    """Classify how confident we are that a job is fresh TODAY:

      "confirmed" - the source stated a real posting date and it's today.
      "likely"    - the source hides the date (NULL or a crawl-time fallback
                    stamp), but the posting FIRST appeared today on a board we
                    were already crawling (`board_known`). A new posting on an
                    established board is almost certainly newly-posted; we
                    surface it clearly marked as inferred.
      "new_board" - discovered today, but the company itself was added in the
                    last ~2 days so we don't yet have `board_known`. Could be a
                    genuinely-new posting OR a first-crawl backfill from that
                    just-added board -- we can't tell. Surfaced so tonight's
                    HN/YC seeds' fresh jobs don't stay hidden for two days;
                    tagged distinctly (🟢 NEW BOARD) so you know why.
      None        - none of the above; don't show on Posted Today.

    Half of all sources (iCIMS, SmartRecruiters, Workday, BambooHR) never expose
    a usable posting date; without "likely"/"new_board" they'd be invisible
    here even when genuinely brand-new."""
    if posted_today(job):
        return "confirmed"
    if not job.get("board_known"):
        # Recently-added company: earlier this returned None, hiding every job
        # from any board seeded in the last 2 days. Now we surface with a
        # different tag so you can eyeball them separately.
        return "new_board" if _discovered_today(job) else None
    posted = _parse_dt(job.get("posted_at"))
    disc = _parse_dt(job.get("discovered_at"))
    date_is_usable = posted is not None and (
        disc is None or abs((disc - posted).total_seconds()) >= 5)
    if date_is_usable:
        return None  # it HAS a real date and that date wasn't today -> genuinely old
    return "likely" if _discovered_today(job) else None


def years_required(row: dict):
    """Smallest 'N years' figure mentioned in the title/description, or None if
    no experience requirement is stated. Mirrors the scoring engine so the
    dashboard filter agrees with how jobs were scored.

    Feed views fetch with `slim=true`, which drops the (huge) description and
    ships this figure precomputed — so prefer it and only parse when absent."""
    if "years_required" in row:
        return row["years_required"]
    text = f"{row.get('title') or ''} {row.get('description') or ''}".lower()
    nums = [int(n) for n in re.findall(r"(\d{1,2})\+?\s*years?", text)]
    return min(nums) if nums else None


# Experience bands shared by "Posted Today" and "Live Feed". Years are parsed
# from the title/description; most entry-level posts state no number at all, so
# "not stated" is its own band rather than being silently dropped.
EXP_SECTIONS = [
    ("🎓 No experience stated", lambda y: y is None),
    ("① 0–2 years", lambda y: y is not None and y <= 2),
    ("② 3–5 years", lambda y: y is not None and 3 <= y <= 5),
    ("③ 5+ years", lambda y: y is not None and y > 5),
]


def set_status(job_id: int, status: str, reason: str = ""):
    """Change a job's status. Also stashes the (job_id, previous_status)
    pair in session_state so the '🔙 Undo' sidebar link + page can revert
    the change with one click. Adds ONE extra API call per status change
    to fetch the current status first -- cheap, worth the undo safety net."""
    # Snapshot the previous status before mutating so undo can restore it.
    prev = None
    try:
        current = api_get(f"/jobs/{job_id}")
        if current:
            prev = current.get("status")
    except Exception:  # noqa: BLE001
        prev = None
    payload = {"status": status}
    if reason:
        payload["rejection_reason"] = reason
    api_patch(f"/jobs/{job_id}", payload)
    # Stash undo entry (keep last 20 per session; older ones drop off).
    if prev and prev != status:
        stack = st.session_state.setdefault("undo_stack", [])
        stack.append({
            "job_id": job_id,
            "title": (current or {}).get("title", ""),
            "company": (current or {}).get("company_name", ""),
            "old_status": prev,
            "new_status": status,
            "when": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
        })
        # Rolling window
        del stack[:-20]


def handle_grid_edits(edited, status_by_id: dict) -> bool:
    """Reconcile the checkboxes in a data_editor result against the DB.

    Every discovery grid used to have its own copy of this loop -- Posted
    Today, Best Matches, Last 24 Hours, Posted This Week, Live Feed, Entry
    Level. One helper replaces 5 near-identical copies (~30 LOC each).

    Semantics:
      - `dismiss=True`  -> status set to Archived (drops from all discovery views)
      - `applied=True`  when current != Applied  -> set to Applied
      - `applied=False` when current == Applied  -> revert to New (undo apply)

    Returns True on the first change so the caller can `st.rerun()` and
    re-fetch the grid with the new state visible.
    """
    for jid, r in edited.iterrows():
        cur = status_by_id.get(jid)
        # Dismiss wins over apply -- if both are set, the user is dumping the row.
        if "dismiss" in r and bool(r.get("dismiss")):
            set_status(int(jid), "Archived")
            return True
        if "applied" in r:
            want = bool(r.get("applied"))
            if want and cur != "Applied":
                set_status(int(jid), "Applied")
                return True
            if not want and cur == "Applied":
                set_status(int(jid), "New")
                return True
    return False


def undo_last_status_change() -> str:
    """Pop the most recent undo entry and restore the job's old status.
    Returns a human-readable summary of what happened, or '' if nothing
    to undo. Called by sidebar 'Undo' button + the '🔙 Undo' page."""
    stack = st.session_state.get("undo_stack") or []
    if not stack:
        return ""
    entry = stack.pop()
    # Revert without triggering another undo push (would create a loop).
    api_patch(f"/jobs/{entry['job_id']}", {"status": entry["old_status"]})
    return (f"Reverted **{entry['title']}** at **{entry['company']}** "
            f"from {entry['new_status']} → {entry['old_status']}")


# Sources whose apply page can't actually be opened. Himalayas is an aggregator
# behind Cloudflare — its job page never clears "security verification" in Chrome
# (verified: even curl gets HTTP 403 + the challenge), and its API only ever hands
# back himalayas.app URLs, so there's no real employer link to store. For these we
# route the user to the employer's OWN careers page via search instead of a link
# that just spins forever.
_WALLED_HOSTS = ("himalayas.app",)


def is_walled(job: dict) -> bool:
    return any(h in (job.get("job_url") or "") for h in _WALLED_HOSTS)


def apply_url(job: dict) -> str:
    """Best *working* apply link for a job. Falls back to an employer-careers
    search for Cloudflare-walled aggregator rows; otherwise the raw posting URL."""
    u = (job.get("job_url") or "").strip()
    if is_walled(job):
        from urllib.parse import quote_plus
        q = quote_plus(f"{job.get('company_name', '')} {job.get('title', '')} careers")
        return "https://www.google.com/search?q=" + q
    return u


def _simplify_title(title: str) -> str:
    """Strip level/seniority words from a job title so LinkedIn people search
    returns employees at ANY level in the same role area -- widens the hit
    set considerably vs. a literal 'Senior Software Engineer II' query."""
    simple = re.sub(
        r"\b(senior|sr\.?|junior|jr\.?|staff|principal|lead|manager|director|"
        r"associate|entry.?level|new.?grad|intern(ship)?|i{1,3}\b|iv|v|\d{1,2})\b",
        " ", title, flags=re.I)
    return re.sub(r"\s+", " ", simple).strip() or "engineer"


def referral_url(job: dict) -> str:
    """Primary LinkedIn people-search URL. Kept for backwards compat with
    existing pages -- newer code should use referral_search_urls() for
    the multi-angle 3-search set."""
    from urllib.parse import quote as _q
    company = (job.get("company_name") or "").strip()
    keywords = f"{company} {_simplify_title(job.get('title') or '')}"
    return ("https://www.linkedin.com/search/results/people/?"
            f"keywords={_q(keywords)}&origin=GLOBAL_SEARCH_HEADER")


def referral_search_urls(job: dict, school: str = "") -> dict:
    """Three complementary LinkedIn searches for warm-intro discovery:

      role      -- people currently at the company doing this role.
                   Best for peer referrals ("hey I'm applying, could you
                   forward my resume to hiring manager?").
      recruiter -- people at the company with 'recruiter' / 'talent'
                   in title. Direct outreach to the hiring pipeline.
      alumni    -- same-school people at the company. Historically the
                   highest-response-rate cold outreach on LinkedIn.
                   Skipped when we don't know the user's school.

    Returns {kind: url}. Empty dict entries are fine -- caller decides
    which to render.
    """
    from urllib.parse import quote as _q
    company = (job.get("company_name") or "").strip()
    simple = _simplify_title(job.get("title") or "")
    base = "https://www.linkedin.com/search/results/people/?origin=GLOBAL_SEARCH_HEADER&keywords="
    urls = {
        "role":      base + _q(f"{company} {simple}"),
        "recruiter": base + _q(f"{company} recruiter OR talent OR sourcer"),
    }
    if school:
        urls["alumni"] = base + _q(f"{company} {school}")
    return urls


def referral_intro_message(job: dict, my_name: str = "there") -> str:
    """Auto-drafted LinkedIn DM the user can paste when reaching out for
    a referral. Keeps it short (LinkedIn char cap) and specific to the
    role. Meant to be a starting draft -- user personalizes 1-2 lines."""
    title = (job.get("title") or "the role").strip()
    company = (job.get("company_name") or "your team").strip()
    return (
        f"Hi! I saw {company} is hiring for {title} and I'm applying "
        f"this week. Wondering if you'd be open to a quick chat about "
        f"the team + what makes candidates stand out -- happy to send "
        f"my resume over too. Either way, thanks so much!\n\n— {my_name}"
    )


# --- Feed hygiene: hide rows the user can't act on ---------------------------
# The discovery pages (Posted Today, Live Feed, Best Matches, Fresh, Fast Apply,
# Entry Level, Find Jobs, JobRight Gap) all share the same two failure modes:
#
#   1. Himalayas rows: the "open" link either dead-ends on Cloudflare or bounces
#      you to a Google search — you can't actually apply from here.
#   2. Non-US postings: SmartRecruiters/Workday/Greenhouse/etc. don't filter by
#      country, so any multinational employer (Boschgroup, etc.) leaks European /
#      Indian / Canadian roles into a feed built for a US-based F-1 job search.
#
# Both get dropped at the DASHBOARD (not the API) so history pages
# (Applied/Approved/Rejected/Archived) keep the full record — this only strips
# the ACTIVE discovery views.

# Non-US country names, matched as whole words case-insensitively. Kept narrow
# to countries that actually show up in our sources; adding more here is safe.
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

# Trailing 2-letter ISO country codes SmartRecruiters/Workday emit in lowercase,
# e.g. "Wernau (Neckar), BW, de" -> Germany, "Toronto, ON, ca" -> Canada,
# "Bangalore, Karnataka, in" -> India. MATCHED CASE-SENSITIVELY so we never
# eat US state codes ("San Francisco, CA", "Wilmington, DE") which are uppercase
# in every source we ingest. Verified against a sample of the live DB.
_NON_US_LC_SUFFIX_RE = re.compile(
    r",\s*(de|uk|gb|fr|nl|be|ch|at|se|no|fi|dk|pl|cz|sk|hu|ro|bg|gr|tr|ru|ua|"
    r"cn|jp|kr|in|pk|bd|vn|th|ph|sg|hk|tw|au|nz|mx|br|ar|cl|co|il|ae|sa|qa|"
    r"eg|za|ng|ke|ie|pt|es|it|my|ca|id)\s*$"
)


def is_non_us(job: dict) -> bool:
    """True when a job's location clearly names a non-US country. Ambiguous
    or empty locations (`"Remote"`, `""`, `"United States"`, US-state format)
    return False so US-remote and unstated-country roles stay visible."""
    loc = job.get("location") or ""
    if not loc.strip():
        return False
    if _NON_US_LC_SUFFIX_RE.search(loc):
        return True
    if _NON_US_COUNTRY_RE.search(loc):
        return True
    return False


# Parser for the [LCA:{...}] prefix that push_lca_to_notes.py stamps on
# companies.notes. Lets the dashboard show recent-year LCA filings + median
# wage per company WITHOUT needing a new backend API endpoint (backend
# restart is the expensive move -- kills the running seeders).
_LCA_JSON_RE = re.compile(r"\[LCA:(\{[^}]*\})\]")


def parse_lca_from_notes(notes: str | None) -> dict:
    """Return {c, p, w, t} from a company's notes prefix, or empty dict.
    Zero-cost when no LCA prefix present."""
    if not notes:
        return {}
    m = _LCA_JSON_RE.search(notes)
    if not m:
        return {}
    try:
        import json as _j
        return _j.loads(m.group(1))
    except Exception:  # noqa: BLE001
        return {}


@st.cache_data(ttl=60)
def _company_lca_lookup() -> dict[str, dict]:
    """Map company_name.lower() -> {c, p, w, t} LCA data. Refreshed
    every 60s. Empty dict if no company has LCA data yet."""
    cos = api_get("/companies/") or []
    out: dict[str, dict] = {}
    for c in cos:
        lca = parse_lca_from_notes(c.get("notes"))
        if lca:
            out[(c.get("name") or "").lower()] = lca
    return out


@st.cache_data(ttl=60)
def _frozen_companies() -> set[str]:
    """Names of companies user marked as ❄️ frozen (recent layoffs, hiring freeze).
    Refreshes every 60s. Frozen = priority='skip' -- the crawler already stops
    pulling from them; this cache lets the dashboard hide their existing stored
    jobs from discovery views too."""
    data = api_get("/companies/") or []
    return {(c.get("name") or "").lower() for c in data
            if (c.get("priority") or "").lower() == "skip"}


def hide_from_feed(job: dict) -> bool:
    """Rows that shouldn't appear on discovery pages: aggregators you can't
    apply through (Cloudflare-walled), postings clearly outside the US, or
    jobs at companies you've marked ❄️ frozen (recent layoffs / hiring freeze).
    """
    if is_walled(job) or is_non_us(job):
        return True
    name = (job.get("company_name") or "").lower()
    return bool(name) and name in _frozen_companies()


# ---- Salary parsing: extract min USD from a job description --------------
# CA/NY/WA laws mandate salary in the posting -- this parser catches the
# common patterns: "$120,000 - $180,000", "$120K to $180K", "starting at $150K".
_SALARY_RANGE_RE = re.compile(
    r"\$\s*(\d{2,3})[,]?(\d{3})?\s*[kK]?\s*(?:[-–—to]{1,3})\s*"
    r"\$?\s*(\d{2,3})[,]?(\d{3})?\s*[kK]?",
)
_SALARY_SINGLE_RE = re.compile(
    r"(?:starting\s+at|from|minimum|min|base)\s*[:\s]*"
    r"\$\s*(\d{2,3})[,]?(\d{3})?\s*[kK]?",
    re.I,
)
_SALARY_SIMPLE_RE = re.compile(r"\$\s*(\d{2,3})[,]?(\d{3})?\s*[kK]?")


def _to_dollars(major: str, minor: str | None) -> int:
    """Turn regex groups back into an integer USD. '120' + '000' -> 120000,
    '150' alone -> 150000 (assume K when 3 digits). Handles '$180K' too."""
    n = int(major)
    if minor:
        n = n * 1000 + int(minor)
    elif n < 500:  # bare "150" or "$180K" almost always means thousands
        n = n * 1000
    return n


def parse_min_salary(job: dict) -> int | None:
    """Return the minimum USD salary named in the JD, or None if unparseable.
    Prefers a range's lower bound, falls back to 'starting at' phrases, then
    the first bare `$` amount that looks like a salary (>= 40K, <= 500K)."""
    desc = (job.get("description") or "")[:5000]  # cap for perf
    if not desc:
        return None
    m = _SALARY_RANGE_RE.search(desc)
    if m:
        low = _to_dollars(m.group(1), m.group(2))
        if 40_000 <= low <= 500_000:
            return low
    m = _SALARY_SINGLE_RE.search(desc)
    if m:
        v = _to_dollars(m.group(1), m.group(2))
        if 40_000 <= v <= 500_000:
            return v
    # Last-ditch: first $ amount in the sane salary range
    for m in _SALARY_SIMPLE_RE.finditer(desc):
        v = _to_dollars(m.group(1), m.group(2))
        if 60_000 <= v <= 500_000:  # tighter range for the fallback to avoid false matches
            return v
    return None


def filter_feed(jobs: list) -> list:
    """Drop walled + non-US + frozen rows from a jobs list, THEN collapse
    cross-source duplicates. No-op on empty/None."""
    if not jobs:
        return []
    kept = [j for j in jobs if not hide_from_feed(j)]
    return _collapse_duplicates(kept)


def _dedup_key(job: dict) -> tuple:
    """Fingerprint for cross-source dedup. Same (company, title-core, city)
    across Greenhouse/LinkedIn/Indeed is almost always the SAME posting."""
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    # Strip corporate suffixes (Inc/LLC/Corp/etc.) so "Stripe" and
    # "Stripe, Inc." collapse to the same key. Do this BEFORE _norm which
    # removes punctuation.
    company_raw = (job.get("company_name") or "").lower()
    company_raw = re.sub(
        r"\b(inc|incorporated|corp|corporation|llc|l\.l\.c\.?|ltd|limited|"
        r"company|co|holdings|group|plc|pbc|the)\b\.?", " ", company_raw)
    company = _norm(company_raw)
    # Title with level words stripped so "Senior Software Engineer II" and
    # "Software Engineer" don't collapse but "Software Engineer" from two
    # sources DO collapse.
    title_raw = (job.get("title") or "").lower()
    title_clean = re.sub(
        r"\b(senior|sr\.?|junior|jr\.?|staff|principal|lead|associate|"
        r"i{1,3}\b|iv|v|\d{1,2})\b", " ", title_raw)
    title = _norm(title_clean)
    # First city word only (handles "Austin, TX" vs "Austin, Texas")
    loc = (job.get("location") or "").lower()
    city = re.match(r"[a-z]+", loc)
    city_key = city.group(0) if city else ""
    return (company, title, city_key)


def _collapse_duplicates(jobs: list) -> list:
    """Keep the strongest representative per _dedup_key group:
        - highest match_score wins
        - tie-break: most recent discovered_at
        - preserve original list order otherwise"""
    if len(jobs) < 2:
        return jobs
    seen: dict[tuple, dict] = {}
    order: list[tuple] = []
    for j in jobs:
        k = _dedup_key(j)
        if not k[0] or not k[1]:
            # Missing company or title -- can't confidently group; pass through
            k = ("__ungrouped__", str(j.get("id", "")), "")
        current = seen.get(k)
        if current is None:
            seen[k] = j
            order.append(k)
        else:
            # Winner: higher score, or (if tied) newer discovered_at
            cs, js = current.get("match_score") or 0, j.get("match_score") or 0
            if js > cs or (js == cs and (j.get("discovered_at") or "") > (current.get("discovered_at") or "")):
                seen[k] = j
    return [seen[k] for k in order]


def render_apply_kit(job: dict):
    """Apply panel: open the job, download YOUR real résumé to upload, copy your
    details into the form, and mark it Applied."""
    with st.expander("🚀 Application kit", expanded=True):
        if job.get("job_url"):
            if is_walled(job):
                st.link_button("🔎 Apply on employer site (Himalayas is Cloudflare-walled)", apply_url(job))
                st.caption("ℹ️ This job is listed via **Himalayas**, whose page won't load in Chrome "
                           "(Cloudflare bot-wall). This opens the employer's own careers page instead.")
            else:
                st.link_button("🚀 Apply — open this job in a new tab", apply_url(job))

        m = master_resume()
        st.markdown("**1 · Your résumé** — upload this file on the job page:")
        if not m["pdf"] and not m["docx"]:
            st.warning("No résumé in `resumes/master/`. Drop your real résumé file there.")
        else:
            d = st.columns(2)
            if m["pdf"]:
                b = file_bytes(m["pdf"])
                if b:
                    d[0].download_button("⬇️ Résumé (.pdf)", b, file_name=os.path.basename(m["pdf"]),
                                         key=f"mr_pdf_{job['id']}", mime="application/pdf")
            if m["docx"]:
                b = file_bytes(m["docx"])
                if b:
                    d[1].download_button("⬇️ Résumé (.docx)", b, file_name=os.path.basename(m["docx"]),
                                         key=f"mr_docx_{job['id']}",
                                         mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

        st.markdown("**2 · Copy your details into the form** (click the copy icon on each):")
        p = my_profile()
        f1, f2 = st.columns(2)
        with f1:
            for label, key in [("Full name", "name"), ("Email", "email"),
                               ("Phone", "phone"), ("LinkedIn", "linkedin")]:
                st.caption(label); st.code(p.get(key) or "—", language=None)
        with f2:
            for label, key in [("Location", "location"), ("Work authorization", "work_authorization"),
                               ("Top skills", "top_skills")]:
                st.caption(label); st.code(p.get(key) or "—", language=None)

        if job.get("status") != "Applied":
            if st.button("✅ Mark as Applied", key=f"applied_{job['id']}"):
                set_status(job["id"], "Applied")
                st.rerun()


# ---------- sidebar ----------
st.sidebar.title("🎯 Job Control Center")
st.sidebar.caption(f"API: {API}")
health = api_get("/health")
if health:
    st.sidebar.success("Backend connected")
else:
    st.sidebar.error("Backend NOT reachable")

page = st.sidebar.radio(
    "Pages",
    ["⚡ Fast Apply", "🚀 AI-Ranked Queue", "🔎 Find Jobs", "🎯 Best Matches", "🎓 Entry Level",
     "🔥 Fresh (apply now)",
     "🕵️ JobRight Gap", "🎯 Sponsors Watchlist", "🔴 Posted Today", "📆 Last 24 Hours",
     "📅 Posted This Week", "🟢 Live Feed", "Today's Best Jobs",
     "📊 Analytics", "🧠 Skills Gap", "🎓 Interview Prep", "📈 Ops Health",
     "📬 Inbox", "🔙 Undo",
     "⚙️ Gmail Settings", "❄️ Frozen Companies",
     "Need Review", "Approved",
     "Applied", "🗑️ Deleted", "Rejected", "Companies", "Stats",
     "📋 Daily Audit"],
)

# Quick live counter in the sidebar (jobs first seen in the last 24h).
# SQL COUNT, not a 3000-row fetch: Streamlit re-runs this whole script on every
# interaction, so anything here is paid on every click of every page.
_recent_count = (api_get("/jobs/count", discovered_within_hours=24,
                         exclude_rejected=True) or {}).get("count", 0)
st.sidebar.metric("🆕 New in last 24h", _recent_count)

if st.sidebar.button("📤 Export Approved → CSV"):
    res = api_post("/export/")
    if res:
        st.sidebar.success(f"Exported {res['count']} jobs to {res['path']}")

# ---- Sidebar UNDO: one-click revert of the last status change ----------
# The stack is per-session (Streamlit session_state), so it survives page
# navigation but not tab close. Full history + bulk undo lives on 🔙 Undo.
_undo_stack = st.session_state.get("undo_stack") or []
if _undo_stack:
    _last = _undo_stack[-1]
    st.sidebar.caption(
        f"🔙 Last: **{(_last['title'] or '')[:32]}** at **{_last['company']}** "
        f"→ {_last['new_status']}"
    )
    if st.sidebar.button(f"↩️ Undo (revert to {_last['old_status']})", key="sidebar_undo"):
        msg = undo_last_status_change()
        if msg:
            st.sidebar.success(msg[:100])
            st.rerun()


# ---------- reusable job card ----------
def render_job_card(job: dict, actions=("Approve", "Reject", "Review")):
    with st.container(border=True):
        # HIGHLIGHT confirmed H-1B sponsors (company has real USCIS sponsor
        # history). These are the high-yield applications for an F-1.
        if job.get("sponsor_confirmed") or (job.get("sponsor_score") or 0) >= 50:
            st.markdown(
                "<span style='background:#1a7f37;color:#fff;padding:3px 10px;"
                "border-radius:6px;font-weight:700;font-size:0.8em;'>✅ H-1B SPONSOR"
                f" · history {job.get('sponsor_score', 0)}</span>",
                unsafe_allow_html=True,
            )
        c1, c2 = st.columns([3, 1])
        with c1:
            st.markdown(f"### {job.get('title','(no title)')}")
            st.markdown(
                f"**{job.get('company_name','')}** · {job.get('location','—')} · "
                f"_{job.get('employment_type') or 'type unknown'}_"
            )
            posted = job.get("posted_at") or "unknown"
            st.caption(f"Posted: {posted} · Source: {job.get('source','')}")
            if job.get("job_url"):
                _lbl = "🔎 Apply on employer site" if is_walled(job) else "🔗 Open job posting"
                st.markdown(f"[{_lbl}]({apply_url(job)})")
        with c2:
            st.metric("Match score", job.get("match_score", 0))
            risk = job.get("sponsorship_risk", "unknown")
            color = {"low": "🟢", "medium": "🟡", "high": "🟠", "reject": "🔴"}.get(risk, "⚪")
            st.markdown(f"**Sponsorship:** {color} {risk}")
            clabel, cemoji, _ = competition(job.get("source"))
            st.markdown(f"**Competition:** {cemoji} {clabel}")

        with st.expander("Why it fits / risk"):
            st.write("**Fit:**", job.get("fit_reason") or "—")
            st.write("**Risk:**", job.get("risk_reason") or "—")

        if job.get("resume_notes"):
            with st.expander("📝 Resume tailoring notes"):
                st.markdown(job["resume_notes"])
        if job.get("cover_letter"):
            with st.expander("✉️ Cover letter draft"):
                st.text(job["cover_letter"])

        cols = st.columns(len(actions) + 2)
        for i, action in enumerate(actions):
            if cols[i].button(action, key=f"{action}_{job['id']}"):
                mapping = {"Approve": "Approved", "Reject": "Rejected", "Review": "Need Review",
                           "Mark Applied": "Applied", "Follow-up": "Follow-up", "Archive": "Archived",
                           "Restore": "New"}
                set_status(job["id"], mapping.get(action, "New"))
                st.rerun()
        # Dedicated "I applied" tracker on EVERY card — click after you apply.
        already = job.get("status") == "Applied"
        if cols[-2].button("✅ Applied ✓" if already else "✅ I Applied",
                           key=f"didapply_{job['id']}", disabled=already,
                           type="secondary" if already else "primary"):
            set_status(job["id"], "Applied")
            st.toast("Marked as Applied ✓")
            st.rerun()
        if cols[-1].button("🚀 Apply", key=f"apply_{job['id']}"):
            st.session_state[f"show_apply_{job['id']}"] = True

        if st.session_state.get(f"show_apply_{job['id']}"):
            render_apply_kit(job)


# ---------- pages ----------
if page == "🔎 Find Jobs":
    st.header("🔎 Find Jobs")
    st.caption("Search & filter every US job in the system (rejected ones hidden by default).")

    c1, c2, c3 = st.columns([3, 2, 2])
    query = c1.text_input("Search title / company / location", placeholder="e.g. data engineer, Snowflake, Remote")
    window = c2.selectbox("Posted within", ["Any time", "Last 24 hours", "Last 3 days", "Last 7 days", "Last 30 days"])
    sort = c3.selectbox("Sort by", ["Best match", "Newest posted", "Recently discovered"])

    c4, c5, c6, c7 = st.columns([2, 3, 2, 2])
    min_score = c4.slider("Min score", 0, 100, 0, step=5)
    risks = c5.multiselect("Sponsorship risk", ["low", "medium", "high", "unknown"], default=["low", "medium"])
    hide_rejected = c6.checkbox("Hide rejected", value=True)
    fj_hide_applied = c7.checkbox("Hide applied", value=True,
                                  help="Applied jobs are on the Applied page; hide from search to avoid re-applying.")

    hours = {"Any time": None, "Last 24 hours": 24, "Last 3 days": 72,
             "Last 7 days": 168, "Last 30 days": 720}[window]
    order = {"Best match": "score", "Newest posted": "posted", "Recently discovered": "discovered"}[sort]

    params = dict(min_score=min_score, exclude_rejected=hide_rejected, order_by=order, limit=300)
    if query:
        params["q"] = query
    if hours:
        params["posted_within_hours"] = hours
    data = filter_feed(api_get("/jobs/", **params))
    # Client-side risk filter (API takes one risk; we allow several).
    if risks:
        data = [j for j in data if j.get("sponsorship_risk") in risks]
    if fj_hide_applied:
        data = [j for j in data if j.get("status") != "Applied"]

    st.success(f"{len(data)} matching jobs")
    for job in data[:150]:
        render_job_card(job, actions=("Approve", "Review", "Reject"))
    if len(data) > 150:
        st.info(f"Showing first 150 of {len(data)}. Narrow your search to see more.")

elif page == "🔥 Fresh (apply now)":
    st.header("🔥 Fresh — apply within hours")
    st.caption("Jobs POSTED most recently (not just discovered). Applying in the first "
               "few hours dramatically raises your callback odds — beat the flood.")
    cA, cB, cC = st.columns([2, 2, 2])
    win = cA.selectbox("Posted within", ["Last 6 hours", "Last 12 hours", "Last 24 hours", "Last 3 days"], index=2)
    only_best = cB.checkbox("Only strong matches (New)", value=True)
    low_comp = cC.checkbox("Low-competition sources only", value=False,
                           help="bamboohr / iCIMS / workday — boards the big aggregators skip")
    fh = {"Last 6 hours": 6, "Last 12 hours": 12, "Last 24 hours": 24, "Last 3 days": 72}[win]
    params = dict(order_by="posted", posted_within_hours=fh, exclude_rejected=True, limit=300)
    if only_best:
        params["status"] = "New"
    data = filter_feed(api_get("/jobs/", **params))
    if low_comp:
        data = [j for j in data if competition(j.get("source"))[2] == 1]
    # Always hide Applied on Fresh — the whole point is "you haven't done this yet".
    data = [j for j in data if j.get("status") != "Applied"]
    st.success(f"{len(data)} jobs posted in the {win.lower()}")
    if not data:
        st.info("Nothing posted in this window yet — widen the window or check back soon.")
    for job in data[:150]:
        render_job_card(job, actions=("Approve", "Review", "Reject"))

elif page == "🎯 Sponsors Watchlist":
    st.header("🎯 Sponsors Watchlist")
    st.caption("**Every confirmed H-1B sponsor job you haven't applied to yet**, ranked "
               "by match score. This is where you should spend your morning triage time — "
               "sponsor-confirmed = the company has real USCIS approval history, dramatically "
               "raising your conversion odds vs cold applications to unknown-sponsor rows.")

    STATUS_BADGE_SW = {
        "Applied": "✅ APPLIED", "Follow-up": "📌 Follow-up", "Approved": "👍 Approved",
        "Need Review": "🔍 Review", "New": "🆕 New",
    }
    c1, c2, c3, c4 = st.columns(4)
    sw_min_score = c1.slider("Min match score", 0, 90, 30, step=5, key="sw_min_score",
                             help="30 = default; raise for tighter fit, lower for broader queue.")
    sw_fresh_days = c2.slider("Discovered within N days", 0, 90, 30, step=5, key="sw_fresh_days",
                              help="0 = any age. Sponsor jobs are worth pursuing even older, "
                                   "but recency matters for callback rates.")
    sw_min_salary = c3.slider("Min salary ($K)", 0, 300, 0, step=10, key="sw_min_salary",
                              help="0 = show all (jobs without a parsed salary are always kept). "
                                   "Bump to 120 to hide sub-$120K roles when the JD mentions salary. "
                                   "Only filters jobs where a salary was FOUND in the description -- "
                                   "unparseable ones stay so you don't miss real opportunities.")
    sw_group = c4.checkbox("Group by experience", value=True, key="sw_group")

    def sponsors_watchlist_feed():
        # For salary filter to work, we need JD text -- NOT slim.
        params = dict(min_score=sw_min_score, exclude_rejected=True,
                      order_by="score", limit=3000,
                      slim=(sw_min_salary == 0))  # only pull descriptions when filter is active
        if sw_fresh_days > 0:
            params["discovered_within_hours"] = sw_fresh_days * 24
        data = filter_feed(api_get("/jobs/", **params))
        # THE core filter: sponsor-confirmed only.
        data = [j for j in data if j.get("sponsor_confirmed")]
        # Never show already-applied on this page (whole point is TRIAGE).
        data = [j for j in data if j.get("status") not in ("Applied", "Archived")]

        # Salary filter -- only drops rows where we PARSED a salary and it's
        # below the floor. Unparseable rows stay to avoid false-negatives.
        if sw_min_salary > 0:
            floor = sw_min_salary * 1000
            data = [j for j in data
                    if (parse_min_salary(j) or floor) >= floor]

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("🎯 Sponsor jobs", len(data))
        m2.metric("🆕 New (strong)", sum(1 for j in data if j.get("status") == "New"))
        m3.metric("🔍 Need review", sum(1 for j in data if j.get("status") == "Need Review"))
        m4.metric("Avg score", f"{sum(j.get('match_score') or 0 for j in data)//len(data)}" if data else "—")

        if not data:
            st.info("Nothing sponsor-confirmed matches these filters. Lower min-score or "
                    "widen the discovery window with the sliders above.")
            return

        # LCA lookup: recent-year real H-1B filings per company (from the
        # enrich_lca batch). Cached 60s at module level.
        lca_by_company = _company_lca_lookup()

        def render_sw(subset, key_prefix):
            def _lca(j):
                return lca_by_company.get((j.get("company_name") or "").lower(), {})
            rows = [{
                "id": j.get("id"),
                "applied": False,   # This page hides Applied, so always start unticked
                "dismiss": False,
                "status": STATUS_BADGE_SW.get(j.get("status"), j.get("status") or ""),
                "posted": (j.get("posted_at") or "")[:10]
                          or ("~" + (j.get("discovered_at") or "")[:10]),
                "score": j.get("match_score"),
                "title": j.get("title"),
                "company": j.get("company_name"),
                "location": j.get("location"),
                "sponsor_score": j.get("sponsor_score", 0),
                # NEW: real recent-year LCA data if we have it
                "lca_2024": _lca(j).get("p", 0),   # filings_prior = 2024
                "lca_2025": _lca(j).get("c", 0),   # filings_current = 2025
                "lca_wage": _lca(j).get("w", 0),
                "salary": parse_min_salary(j) or 0,
                "risk": j.get("sponsorship_risk"),
                "open": apply_url(j),
                "referral": referral_url(j),
            } for j in subset]
            df = pd.DataFrame(rows).set_index("id")
            grid_h = min(len(rows) + 1, 26) * 35 + 3
            editor_key = f"sw_ed_{key_prefix}_" + str(abs(hash(tuple(r["id"] for r in rows))))
            edited = st.data_editor(
                df, key=editor_key, hide_index=True, use_container_width=True, height=grid_h,
                disabled=["status", "posted", "score", "title", "company", "location",
                          "sponsor_score", "lca_2024", "lca_2025", "lca_wage",
                          "salary", "risk", "open", "referral"],
                column_order=["dismiss", "status", "sponsor_score", "lca_2024", "lca_2025",
                              "lca_wage", "posted", "score", "salary",
                              "title", "company", "location", "risk", "open",
                              "referral", "applied"],
                column_config={
                    "applied": st.column_config.CheckboxColumn(
                        "✅ Applied?", help="Tick when you've applied — drops from this list."),
                    "dismiss": st.column_config.CheckboxColumn(
                        "🗑️", help="Tick to dismiss — files under 🗑️ Deleted and drops from feeds."),
                    "sponsor_score": st.column_config.NumberColumn(
                        "sponsor", format="%d",
                        help="Company H-1B history score (higher = more USCIS approvals)."),
                    "lca_2024": st.column_config.NumberColumn(
                        "H1B '24", format="%d",
                        help="Actual H-1B LCA filings in 2024 (from DOL data). "
                             "0 = no data yet (LCA enrichment still running)."),
                    "lca_2025": st.column_config.NumberColumn(
                        "H1B '25", format="%d",
                        help="Actual H-1B LCA filings in 2025 YTD (from DOL data)."),
                    "lca_wage": st.column_config.NumberColumn(
                        "median $", format="$%d",
                        help="Median H-1B wage filed at this company (from DOL data). "
                             "Reality check on the JD's salary range."),
                    "salary": st.column_config.NumberColumn(
                        "JD min $", format="$%d",
                        help="Minimum salary parsed from the JD ($0 = not stated / unparseable). "
                             "CA/NY/WA require salary in the posting."),
                    "posted": st.column_config.TextColumn(
                        "posted", help="Real posting date (or ~first-seen if source hides it)."),
                    "score": st.column_config.NumberColumn("score", format="%d"),
                    "open": st.column_config.LinkColumn("open", display_text="open ↗"),
                    "referral": st.column_config.LinkColumn(
                        "🤝 referral", display_text="LinkedIn ↗",
                        help="LinkedIn people search for company + role — DM 1st-degree "
                             "for a warm intro BEFORE applying."),
                },
            )
            status_by_id = {j.get("id"): j.get("status") for j in subset}
            return handle_grid_edits(edited, status_by_id)

        st.caption(f"👉 {len(data)} sponsor-confirmed jobs open · sorted by score · "
                   f"tick **✅ Applied?** or **🗑️** to file · **🤝 referral** links open "
                   f"LinkedIn people search")

        if sw_group:
            yrs_by_id = {j.get("id"): years_required(j) for j in data}
            for idx, (label, belongs) in enumerate(EXP_SECTIONS):
                subset = [j for j in data if belongs(yrs_by_id.get(j.get("id")))]
                if not subset:
                    continue
                st.subheader(f"{label}  ·  {len(subset)} jobs")
                if render_sw(subset, idx):
                    st.rerun()
        elif render_sw(data, "all"):
            st.rerun()

    sponsors_watchlist_feed()

elif page == "🕵️ JobRight Gap":
    st.header("🕵️ JobRight Gap")
    st.caption("Jobs **JobRight likely never showed you** — niche-ATS postings from "
               "lesser-known companies that the big boards (LinkedIn/Indeed → JobRight) "
               "rarely scrape. Apply here for far less competition. Fully automatic, no "
               "JobRight login.")

    TIERS = {
        "🟢 Exclusive — JobRight likely MISSED these": "exclusive",
        "🟡 Likely on JobRight (syndicated)": "likely",
        "🔴 Common — JobRight surely has these": "common",
    }
    c1, c2, c3 = st.columns([3, 2, 2])
    tier_label = c1.selectbox("Coverage tier", list(TIERS.keys()))
    tier = TIERS[tier_label]
    min_score = c2.slider("Min match score", 0, 100, 50, step=5)
    window = c3.selectbox("Discovered within", ["All", "Last 24 hours", "Last 3 days", "Last 7 days"])
    whours = {"All": None, "Last 24 hours": 24, "Last 3 days": 72, "Last 7 days": 168}[window]
    only_sponsors = st.checkbox("✅ Confirmed H-1B sponsors only", value=False)

    params = dict(jobright_tier=tier, min_score=min_score, exclude_rejected=True,
                  order_by="exclusivity", limit=1000)
    if whours:
        params["discovered_within_hours"] = whours
    data = filter_feed(api_get("/jobs/", **params))
    if only_sponsors:
        data = [j for j in data if j.get("sponsor_confirmed")]
    data = [j for j in data if j.get("status") != "Applied"]

    m1, m2, m3 = st.columns(3)
    m1.metric("Jobs in this gap", len(data))
    m2.metric("✅ H-1B sponsors", sum(1 for j in data if j.get("sponsor_confirmed")))
    m3.metric("🆕 Fresh (<24h)", sum(1 for j in data if (j.get("jobright_exclusivity") or 0) >= 90))

    if not data:
        st.info("No jobs in this view yet. Loosen the score/recency filters, or let the crawler run.")
    else:
        st.caption(f"👉 Sorted by **exclusivity** (how likely JobRight missed it). "
                   f"Tick **✅ Applied?** to mark + remove a row. {tier_label}.")
        rows = [{
            "id": j.get("id"),
            "applied": False,
            "edge": j.get("jobright_exclusivity"),
            "sponsor": "✅ H-1B" if j.get("sponsor_confirmed") else "",
            "score": j.get("match_score"),
            "title": j.get("title"),
            "company": j.get("company_name"),
            "source": j.get("source"),
            "location": j.get("location"),
            "why": j.get("jobright_reason"),
            "open": apply_url(j),
        } for j in data]
        df = pd.DataFrame(rows).set_index("id")
        editor_key = "gap_ed_" + str(abs(hash(tuple(r["id"] for r in rows))))
        edited = st.data_editor(
            df, key=editor_key, hide_index=True, use_container_width=True,
            disabled=["edge", "sponsor", "score", "title", "company", "source",
                      "location", "why", "open"],
            column_order=["edge", "sponsor", "score", "title", "company",
                          "source", "location", "why", "open", "applied"],
            column_config={
                "applied": st.column_config.CheckboxColumn(
                    "✅ Applied?", help="Tick when you've applied — it's marked Applied and drops out."),
                "edge": st.column_config.ProgressColumn(
                    "JobRight-miss", help="Confidence JobRight never showed this (higher = better edge).",
                    min_value=0, max_value=100, format="%d"),
                "open": st.column_config.LinkColumn("open", display_text="apply ↗"),
                "score": st.column_config.NumberColumn("score", format="%d"),
            },
        )
        changed = False
        for jid, r in edited.iterrows():
            if bool(r["applied"]):
                set_status(int(jid), "Applied"); changed = True
        if changed:
            st.rerun()

elif page == "🎯 Best Matches":
    st.header("🎯 Best Matches")
    st.caption("Your **full applyable backlog** — sponsor-safe, US, on-target roles you "
               "haven't applied to yet, ranked by match score. Unlike 🔴 Posted Today (last "
               "24h only), this is the whole open pool to actually work from. Widen or tighten "
               "with the sliders below. Citizenship/clearance/foreign roles are already excluded.")

    STATUS_BADGE = {
        "Applied": "✅ APPLIED", "Follow-up": "📌 Follow-up", "Approved": "👍 Approved",
        "Need Review": "🔍 Review", "New": "🆕 New",
    }
    c1, c2, c3 = st.columns(3)
    bm_min_score = c1.slider("Min match score", 0, 90, 40, step=5,
                             help="Lower surfaces more (weaker) matches; raise for only the "
                                  "strongest. 40 ≈ solid, 50+ ≈ strong.")
    bm_fresh_days = c2.slider("First seen within N days", 0, 60, 14, step=1,
                              help="0 = any age. Filters on when the crawler first SAW the job — "
                                   "populated for every source, unlike the posted-date used by "
                                   "Posted Today (which drops iCIMS/SmartRecruiters nulls).")
    bm_group = c3.checkbox("Group by experience", value=True,
                           help="Split into experience bands (no-experience-stated · 0–2 · 3–5 · "
                                "5+ yrs) parsed from each posting. Untick for one flat ranked list.")
    bm_hide_applied = st.checkbox("Hide jobs I've already applied to", value=True,
                                  key="bm_hide_applied")

    def best_matches_feed():
        params = dict(min_score=bm_min_score, exclude_rejected=True, order_by="score",
                      limit=3000, slim=True)
        if bm_fresh_days > 0:
            params["discovered_within_hours"] = bm_fresh_days * 24
        data = filter_feed(api_get("/jobs/", **params))
        # exclude_rejected already drops Rejected + Archived server-side.
        if bm_hide_applied:
            data = [j for j in data if j.get("status") != "Applied"]

        n_sponsor = sum(1 for j in data if j.get("sponsor_confirmed"))
        m1, m2, m3 = st.columns(3)
        m1.metric("🎯 Matches", len(data))
        m2.metric("✅ H-1B sponsors", n_sponsor)
        m3.metric("🆕 New (unreviewed)", sum(1 for j in data if j.get("status") == "New"))
        if not data:
            st.info("No matches in this range — lower the min score or widen the freshness "
                    "window with the sliders above.")
            return

        def render_section(subset, key_prefix):
            rows = [{
                "id": j.get("id"),
                "applied": j.get("status") == "Applied",
                "dismiss": False,
                "status": STATUS_BADGE.get(j.get("status"), j.get("status") or ""),
                "sponsor": "✅ H-1B" if j.get("sponsor_confirmed") else "",
                "seen": "~" + (j.get("discovered_at") or "")[:10] if j.get("discovered_at") else "—",
                "posted": (j.get("posted_at") or "")[:10] or "—",
                "score": j.get("match_score"),
                "title": j.get("title"),
                "company": j.get("company_name"),
                "location": j.get("location"),
                "risk": j.get("sponsorship_risk"),
                "open": apply_url(j),
                "referral": referral_url(j),
            } for j in subset]
            df = pd.DataFrame(rows).set_index("id")
            editor_key = f"bm_ed_{key_prefix}_" + str(abs(hash(tuple(r["id"] for r in rows))))
            # Explicit height so all rows are reachable in ONE grid (up to ~25 visible,
            # then the grid scrolls internally) instead of the default ~10-row viewport.
            grid_h = min(len(rows) + 1, 26) * 35 + 3
            edited = st.data_editor(
                df, key=editor_key, hide_index=True, use_container_width=True, height=grid_h,
                disabled=["status", "sponsor", "seen", "posted", "score", "title", "company",
                          "location", "risk", "open", "referral"],
                column_order=["dismiss", "status", "sponsor", "seen", "posted",
                              "score", "title", "company", "location", "risk",
                              "open", "referral", "applied"],
                column_config={
                    "applied": st.column_config.CheckboxColumn(
                        "✅ Applied?", help="Tick when you've applied — it's kept (never pruned)."),
                    "dismiss": st.column_config.CheckboxColumn(
                        "🗑️", help="Tick to dismiss — files it under 🗑️ Archived and drops it here."),
                    "seen": st.column_config.TextColumn(
                        "first seen", help="~date the crawler first saw this posting."),
                    "posted": st.column_config.TextColumn(
                        "posted", help="Source-stated posting date (blank for sources that hide it)."),
                    "open": st.column_config.LinkColumn("open", display_text="open ↗"),
                    "referral": st.column_config.LinkColumn(
                        "🤝 referral", display_text="LinkedIn ↗",
                        help="LinkedIn people search for this company + role. Message a "
                             "1st-degree connection for a warm intro BEFORE applying -- "
                             "referred apps get 5-10x more callbacks than cold ones."),
                    "score": st.column_config.NumberColumn("score", format="%d"),
                },
            )
            status_by_id = {j.get("id"): j.get("status") for j in subset}
            return handle_grid_edits(edited, status_by_id)

        st.caption(f"👉 Tick **✅ Applied?** to mark applied · 🗑️ to dismiss · "
                   f"{len(data)} matches · {n_sponsor} sponsor-confirmed in view")

        if bm_group:
            yrs_by_id = {j.get("id"): years_required(j) for j in data}
            for idx, (label, belongs) in enumerate(EXP_SECTIONS):
                subset = [j for j in data if belongs(yrs_by_id.get(j.get("id")))]
                if not subset:
                    continue
                n_sp = sum(1 for j in subset if j.get("sponsor_confirmed"))
                st.subheader(f"{label}  ·  {len(subset)} jobs"
                             + (f"  ·  {n_sp} ✅ H-1B" if n_sp else ""))
                if render_section(subset, idx):
                    st.rerun()
        elif render_section(data, "all"):
            st.rerun()

    best_matches_feed()

elif page == "🎓 Entry Level":
    st.header("🎓 Entry Level")
    st.caption("Every job whose TITLE explicitly signals entry-level — "
               "**Junior / Entry Level / Associate / New Grad / Engineer I / Level 1**. "
               "No match-score gate: this is the raw volume pool. Use it "
               "when you want to apply broadly rather than pick the top match.")

    STATUS_BADGE_EL = {
        "Applied": "✅ APPLIED", "Follow-up": "📌 Follow-up", "Approved": "👍 Approved",
        "Need Review": "🔍 Review", "New": "🆕 New",
    }

    c1, c2, c3, c4 = st.columns(4)
    el_min_score = c1.slider(
        "Min match score", 0, 90, 0, step=5,
        help="Default 0 = show all entry-level jobs regardless of match. "
             "Bump to 30-40 for tighter fit-first list.",
    )
    el_fresh_days = c2.slider(
        "First seen within N days", 0, 60, 30, step=1,
        help="0 = any age. Entry-level pool is smaller than Best Matches so a "
             "wider window is fine.",
    )
    el_scope = c3.radio(
        "Role type",
        ["Tech only", "Non-tech only", "Both"],
        index=0, horizontal=False,
        help="Tech = SWE / data / cloud / ML / infra / QA / security -- the "
             "same allowlist the filter uses at ingest. Non-tech surfaces the "
             "'Associate Attorney' / 'Junior Marketing' style entries the "
             "filter normally hides. Both = no title-role filter.",
    )
    el_hide_applied = c4.checkbox(
        "Hide jobs I've already applied to", value=True,
    )

    def entry_level_feed():
        params = dict(
            entry_level_only=True,
            min_score=el_min_score,
            exclude_rejected=True,
            order_by="discovered",
            limit=3000,
            slim=True,
        )
        if el_scope == "Tech only":
            params["tech_only"] = "true"
        elif el_scope == "Non-tech only":
            params["tech_only"] = "false"
        # else "Both" -> tech_only omitted, no title-role filter
        if el_fresh_days > 0:
            params["discovered_within_hours"] = el_fresh_days * 24
        data = filter_feed(api_get("/jobs/", **params))
        if el_hide_applied:
            data = [j for j in data if j.get("status") != "Applied"]

        n_sponsor = sum(1 for j in data if j.get("sponsor_confirmed"))
        m1, m2, m3 = st.columns(3)
        m1.metric("🎓 Entry-level jobs", len(data))
        m2.metric("✅ H-1B sponsors", n_sponsor)
        m3.metric("🆕 New (unreviewed)",
                  sum(1 for j in data if j.get("status") == "New"))
        if not data:
            st.info("No entry-level jobs in this window. Try widening the freshness "
                    "range to 60 days or dropping the min score.")
            return

        rows = [{
            "id": j.get("id"),
            "applied": j.get("status") == "Applied",
            "dismiss": False,
            "block": False,
            "status": STATUS_BADGE_EL.get(j.get("status"), j.get("status") or ""),
            "sponsor": "✅ H-1B" if j.get("sponsor_confirmed") else "",
            "seen": "~" + (j.get("discovered_at") or "")[:10]
                    if j.get("discovered_at") else "—",
            "posted": (j.get("posted_at") or "")[:10] or "—",
            "score": j.get("match_score"),
            "title": j.get("title"),
            "company": j.get("company_name"),
            "location": j.get("location"),
            "open": apply_url(j),
        } for j in data]
        df = pd.DataFrame(rows).set_index("id")
        editor_key = "el_ed_" + str(abs(hash(tuple(r["id"] for r in rows))))
        grid_h = min(len(rows) + 1, 30) * 35 + 3
        edited = st.data_editor(
            df, key=editor_key, hide_index=True, use_container_width=True,
            height=grid_h,
            disabled=["status", "sponsor", "seen", "posted", "score",
                      "title", "company", "location", "open"],
            column_order=["dismiss", "block", "status", "sponsor",
                          "seen", "posted", "score", "title", "company",
                          "location", "open", "applied"],
            column_config={
                "applied": st.column_config.CheckboxColumn(
                    "✅ Applied?",
                    help="Tick when you've applied — kept forever, never pruned."),
                "dismiss": st.column_config.CheckboxColumn(
                    "🗑️", help="Dismiss this one posting."),
                "block": st.column_config.CheckboxColumn(
                    "🚫", help="BLOCK the company. Adds to the blocklist and "
                              "flips every one of their jobs to Rejected. "
                              "Reversible via data/company_blocklist.txt."),
                "open": st.column_config.LinkColumn("open", display_text="open ↗"),
                "score": st.column_config.NumberColumn("score", format="%d"),
            },
        )

        status_by_id = {j.get("id"): j.get("status") for j in data}
        company_by_id = {j.get("id"): j.get("company_name") for j in data}
        for jid, r in edited.iterrows():
            cur = status_by_id.get(jid)
            if bool(r["block"]):
                cname = company_by_id.get(jid) or ""
                res = api_post("/companies/block", {"name": cname}) or {}
                st.toast(f"🚫 Blocked {cname} — {res.get('jobs_flipped_to_rejected', 0)} "
                         f"jobs Rejected")
                st.rerun()
                return
            if bool(r["dismiss"]):
                set_status(int(jid), "Archived"); st.rerun(); return
            want = bool(r["applied"])
            if want and cur != "Applied":
                set_status(int(jid), "Applied"); st.rerun(); return
            if not want and cur == "Applied":
                set_status(int(jid), "New"); st.rerun(); return

    entry_level_feed()

elif page == "🔴 Posted Today":
    st.header("🔴 Posted Today")
    st.caption("Jobs that are fresh **today** (your local day). "
               "🔴 **CONFIRMED** = the source stated a posting date of today. "
               "🟡 **LIKELY** = the source hides the date, but this posting first appeared "
               "today on a board we already crawl — almost always newly-posted. "
               "🟢 **NEW BOARD** = discovered today from a company we just added (past 2 days) "
               "— could be a genuinely-fresh post or a first-crawl backfill; eyeball to tell. "
               "Citizenship/clearance/foreign roles excluded automatically. Window: last 48h. "
               "Auto-refreshes every 20s.")

    STATUS_BADGE = {
        "Applied": "✅ APPLIED", "Follow-up": "📌 Follow-up", "Approved": "👍 Approved",
        "Need Review": "🔍 Review", "New": "🆕 New",
    }
    c1, c2, c3 = st.columns(3)
    hide_applied = c1.checkbox("Hide jobs I've already applied to", value=True)
    confirmed_only = c2.checkbox("Only date-confirmed (hide 🟡 LIKELY)", value=False,
                                 help="Tick for the strict old behaviour: only jobs whose "
                                      "source printed today's date.")
    group_by_exp = c3.checkbox("Group by experience", value=True,
                               help="Split today's jobs into experience bands "
                                    "(no-experience-stated · 0–2 · 3–5 · 5+ years), parsed "
                                    "from each posting. Untick for one flat ranked list.")

    @st.fragment(run_every=20)
    def posted_today_feed():
        # ONE net, ordered by discovery. A job posted today is ALWAYS first seen
        # <=30h ago (you can't discover a posting before it exists), so this net
        # is a superset of both "confirmed" (real date today) and "likely" (date
        # hidden, first seen today on a known board) -- no second query needed,
        # which keeps this 20s-auto-refreshing page at one request per tick on
        # the small VPS. exclude_rejected drops citizenship/clearance up front.
        # NOTE: a plain `posted_within_hours` net would MISS the hidden-date rows
        # entirely -- SQL `posted_at >= cutoff` skips NULLs -- so discovery-order
        # is the only net that surfaces iCIMS/SmartRecruiters/Workday postings.
        # limit=3000 (API cap): on a busy day >1000 jobs land in 30h, so the old
        # 1000 truncated the oldest-in-window rows BEFORE the freshness filter ran
        # -- i.e. genuinely-today jobs were silently dropped off this page.
        raw = filter_feed(api_get("/jobs/", order_by="discovered", discovered_within_hours=48,
                                  exclude_rejected=True, limit=3000, slim=True))
        data = []
        for j in raw:
            fresh = posted_freshness(j)
            if fresh:
                j["_freshness"] = fresh
                data.append(j)
        if confirmed_only:
            data = [j for j in data if j["_freshness"] == "confirmed"]
        if hide_applied:
            data = [j for j in data if j.get("status") != "Applied"]
        # Jobs you dismissed (🗑️ -> Archived) never belong on Posted Today.
        data = [j for j in data if j.get("status") != "Archived"]
        # Sort: CONFIRMED → LIKELY → NEW BOARD, then by match score within each.
        # Keeps trustworthy rows on top, tentative rows at the bottom.
        _fresh_rank = {"confirmed": 0, "likely": 1, "new_board": 2}
        data.sort(key=lambda j: (_fresh_rank.get(j["_freshness"], 9),
                                 -(j.get("match_score") or 0)))

        n_conf = sum(1 for j in data if j["_freshness"] == "confirmed")
        n_likely = sum(1 for j in data if j["_freshness"] == "likely")
        n_new_board = sum(1 for j in data if j["_freshness"] == "new_board")
        m1, m2, m3 = st.columns(3)
        m1.metric("🔴 Fresh today", len(data),
                  help=f"{n_conf} confirmed · {n_likely} likely · {n_new_board} new board")
        m2.metric("✅ H-1B sponsors", sum(1 for j in data if j.get("sponsor_confirmed")))
        m3.metric("🆕 New (strong match)", sum(1 for j in data if j.get("status") == "New"))
        if not data:
            st.info("Nothing fresh today yet in the sponsor-safe set. This auto-updates — "
                    "new postings will pop in as the crawler finds them.")
            return

        FRESH_BADGE = {"confirmed": "🔴 CONFIRMED", "likely": "🟡 LIKELY",
                       "new_board": "🟢 NEW BOARD"}

        # One table renderer, reused per experience band (or once for the flat
        # list). Returns True on the first tick change so the caller can rerun.
        def render_section(subset, key_prefix):
            rows = [{
                "id": j.get("id"),
                "applied": j.get("status") == "Applied",
                # Always starts unticked: Archived jobs are filtered out above, so
                # a row here is never already dismissed.
                "dismiss": False,
                "fresh": FRESH_BADGE.get(j["_freshness"], ""),
                "status": STATUS_BADGE.get(j.get("status"), j.get("status") or ""),
                "sponsor": "✅ H-1B" if j.get("sponsor_confirmed") else "",
                # Confirmed rows show the real posting date; likely rows have no
                # trustworthy date, so we show when we FIRST saw it, marked "~".
                "posted": ((j.get("posted_at") or "")[:10] if j["_freshness"] == "confirmed"
                           else "~" + (j.get("discovered_at") or "")[:10]) or "—",
                "score": j.get("match_score"),
                "title": j.get("title"),
                "company": j.get("company_name"),
                "location": j.get("location"),
                "risk": j.get("sponsorship_risk"),
                "open": apply_url(j),
            } for j in subset]
            df = pd.DataFrame(rows).set_index("id")
            editor_key = f"today_ed_{key_prefix}_" + str(abs(hash(tuple(r["id"] for r in rows))))
            edited = st.data_editor(
                df, key=editor_key, hide_index=True, use_container_width=True,
                disabled=["fresh", "status", "sponsor", "posted", "score", "title", "company",
                          "location", "risk", "open"],
                column_order=["dismiss", "fresh", "status", "sponsor", "posted", "score",
                              "title", "company", "location", "risk",
                              "open", "applied"],
                column_config={
                    "applied": st.column_config.CheckboxColumn(
                        "✅ Applied?", help="Tick when you've applied — it's kept (never pruned)."),
                    "dismiss": st.column_config.CheckboxColumn(
                        "🗑️", help="Tick to dismiss — removes it from Posted Today and files it "
                                  "under 🗑️ Archived. Restore any time from the Archived page."),
                    "fresh": st.column_config.TextColumn(
                        "fresh", help="🔴 CONFIRMED = source stated today's date · "
                                      "🟡 LIKELY = source hides the date but it first appeared "
                                      "today on a board we already crawl · "
                                      "🟢 NEW BOARD = discovered today from a company we "
                                      "just added (past 2 days). Could be a new posting OR "
                                      "a first-crawl backfill from that board -- eyeball to tell."),
                    "posted": st.column_config.TextColumn(
                        "posted", help="Real posting date (confirmed rows) or ~first-seen date "
                                       "(likely rows)."),
                    "open": st.column_config.LinkColumn("open", display_text="open ↗"),
                    "score": st.column_config.NumberColumn("score", format="%d"),
                },
            )
            status_by_id = {j.get("id"): j.get("status") for j in subset}
            for jid, r in edited.iterrows():
                cur = status_by_id.get(jid)
                # Dismiss wins: file it under Archived and drop it from the feed.
                if bool(r["dismiss"]):
                    set_status(int(jid), "Archived"); return True
                want = bool(r["applied"])
                if want and cur != "Applied":
                    set_status(int(jid), "Applied"); return True
                if not want and cur == "Applied":
                    set_status(int(jid), "New"); return True
            return False

        n_sponsor = sum(1 for j in data if j.get("sponsor_confirmed"))
        st.caption(f"👉 Tick **✅ Applied?** to mark a job applied · 🔴 CONFIRMED date · "
                   f"🟡 LIKELY (first seen today, established board) · 🟢 NEW BOARD "
                   f"(company added in last 2 days) · {n_conf} confirmed / {n_likely} likely "
                   f"/ {n_new_board} new-board · {n_sponsor} sponsor-confirmed in view")

        if group_by_exp:
            # Bucket by stated experience. years_required() reads title+description,
            # which the API already returned, so this costs nothing extra.
            yrs_by_id = {j.get("id"): years_required(j) for j in data}
            changed = False
            for idx, (label, belongs) in enumerate(EXP_SECTIONS):
                subset = [j for j in data if belongs(yrs_by_id.get(j.get("id")))]
                if not subset:
                    continue
                n_sp = sum(1 for j in subset if j.get("sponsor_confirmed"))
                st.subheader(f"{label}  ·  {len(subset)} jobs"
                             + (f"  ·  {n_sp} ✅ H-1B" if n_sp else ""))
                if render_section(subset, idx):
                    changed = True
                    break
            if changed:
                st.rerun()
        elif render_section(data, "all"):
            st.rerun()

    posted_today_feed()

elif page == "📆 Last 24 Hours":
    st.header("📆 Last 24 Hours")
    st.caption("**Every tech-relevant job the crawler pulled in the last 24 hours** — "
               "broader than 🔴 Posted Today because it drops the 'posted today' proof "
               "requirement. Includes Need-Review borderline matches, so you see all "
               "~800/day tech jobs (vs Posted Today's ~60 confirmed-fresh subset). "
               "Same tick-to-apply / tick-to-dismiss workflow.")

    STATUS_BADGE_24 = {
        "Applied": "✅ APPLIED", "Follow-up": "📌 Follow-up", "Approved": "👍 Approved",
        "Need Review": "🔍 Review", "New": "🆕 New",
    }
    c1, c2, c3, c4 = st.columns(4)
    h_hide_applied  = c1.checkbox("Hide applied", value=True, key="h_hide_applied")
    h_only_sponsors = c2.checkbox("Only ✅ sponsors", value=False, key="h_only_sponsors")
    h_only_new      = c3.checkbox("Only 🆕 New (strong match)", value=False, key="h_only_new",
                                  help="Hide the borderline Need-Review rows; show only "
                                       "the strong-match subset already surfacing on Posted Today.")
    h_group         = c4.checkbox("Group by experience", value=True, key="h_group")

    def last_24h_feed():
        raw = filter_feed(api_get("/jobs/", order_by="discovered", discovered_within_hours=24,
                                  exclude_rejected=True, limit=3000, slim=True))
        data = [j for j in raw if j.get("status") not in ("Archived",)]
        if h_hide_applied:
            data = [j for j in data if j.get("status") != "Applied"]
        if h_only_sponsors:
            data = [j for j in data if j.get("sponsor_confirmed")]
        if h_only_new:
            data = [j for j in data if j.get("status") == "New"]
        # Best match first, sponsor wins ties.
        data.sort(key=lambda j: ((j.get("match_score") or 0),
                                 bool(j.get("sponsor_confirmed"))), reverse=True)

        n_new       = sum(1 for j in data if j.get("status") == "New")
        n_review    = sum(1 for j in data if j.get("status") == "Need Review")
        n_sponsor   = sum(1 for j in data if j.get("sponsor_confirmed"))
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("📆 Total in 24h", len(data))
        m2.metric("🆕 Strong (New)", n_new)
        m3.metric("🔍 Need Review", n_review)
        m4.metric("✅ H-1B sponsors", n_sponsor)
        if not data:
            st.info("Nothing surviving filters in the last 24h. Check the crawler is running.")
            return

        def render_24h_section(subset, key_prefix):
            rows = [{
                "id": j.get("id"),
                "applied": j.get("status") == "Applied",
                "dismiss": False,
                "status": STATUS_BADGE_24.get(j.get("status"), j.get("status") or ""),
                "sponsor": "✅ H-1B" if j.get("sponsor_confirmed") else "",
                "posted": (j.get("posted_at") or "")[:10] or ("~" + (j.get("discovered_at") or "")[:10]),
                "score": j.get("match_score"),
                "title": j.get("title"),
                "company": j.get("company_name"),
                "location": j.get("location"),
                "risk": j.get("sponsorship_risk"),
                "open": apply_url(j),
            } for j in subset]
            df = pd.DataFrame(rows).set_index("id")
            grid_h = min(len(rows) + 1, 26) * 35 + 3
            editor_key = f"h24_ed_{key_prefix}_" + str(abs(hash(tuple(r["id"] for r in rows))))
            edited = st.data_editor(
                df, key=editor_key, hide_index=True, use_container_width=True, height=grid_h,
                disabled=["status", "sponsor", "posted", "score", "title", "company",
                          "location", "risk", "open"],
                column_order=["dismiss", "status", "sponsor", "posted", "score", "title",
                              "company", "location", "risk", "open", "applied"],
                column_config={
                    "applied": st.column_config.CheckboxColumn(
                        "✅ Applied?", help="Tick when you've applied — kept, never pruned."),
                    "dismiss": st.column_config.CheckboxColumn(
                        "🗑️", help="Tick to dismiss — files to 🗑️ Archived."),
                    "posted": st.column_config.TextColumn(
                        "posted", help="Real posting date (or ~first-seen if source hides date)."),
                    "open": st.column_config.LinkColumn("open", display_text="open ↗"),
                    "score": st.column_config.NumberColumn("score", format="%d"),
                },
            )
            status_by_id = {j.get("id"): j.get("status") for j in subset}
            return handle_grid_edits(edited, status_by_id)

        st.caption(f"👉 {len(data)} jobs · {n_new} strong / {n_review} borderline "
                   f"· {n_sponsor} sponsor-confirmed · tick **✅ Applied?** or **🗑️** to file")

        if h_group:
            yrs_by_id = {j.get("id"): years_required(j) for j in data}
            for idx, (label, belongs) in enumerate(EXP_SECTIONS):
                subset = [j for j in data if belongs(yrs_by_id.get(j.get("id")))]
                if not subset:
                    continue
                n_sp = sum(1 for j in subset if j.get("sponsor_confirmed"))
                st.subheader(f"{label}  ·  {len(subset)} jobs"
                             + (f"  ·  {n_sp} ✅ H-1B" if n_sp else ""))
                if render_24h_section(subset, idx):
                    st.rerun()
        elif render_24h_section(data, "all"):
            st.rerun()

    last_24h_feed()

elif page == "📅 Posted This Week":
    st.header("📅 Posted This Week")
    st.caption("Every fresh job the crawler pulled in the **last 7 days**, ranked by match "
               "score. Broader than 🔴 Posted Today (48h with freshness proof) — catches jobs "
               "posted over the weekend or that Posted Today's freshness filter passed on. "
               "Same tick-to-apply + tick-to-dismiss workflow.")

    STATUS_BADGE_W = {
        "Applied": "✅ APPLIED", "Follow-up": "📌 Follow-up", "Approved": "👍 Approved",
        "Need Review": "🔍 Review", "New": "🆕 New",
    }
    c1, c2, c3 = st.columns(3)
    w_hide_applied = c1.checkbox("Hide jobs I've already applied to", value=True, key="w_hide_applied")
    w_only_sponsors = c2.checkbox("Only ✅ H-1B sponsor-confirmed", value=False, key="w_only_sponsors")
    w_group = c3.checkbox("Group by experience", value=True, key="w_group",
                          help="Split into experience bands parsed from each posting.")

    def posted_this_week_feed():
        # 168h = 7 days. No freshness filter (unlike Posted Today) -- we want
        # everything the crawler discovered this week, not just "today".
        raw = filter_feed(api_get("/jobs/", order_by="discovered", discovered_within_hours=168,
                                  exclude_rejected=True, limit=3000, slim=True))
        data = [j for j in raw if j.get("status") not in ("Archived",)]
        if w_hide_applied:
            data = [j for j in data if j.get("status") != "Applied"]
        if w_only_sponsors:
            data = [j for j in data if j.get("sponsor_confirmed")]
        # Best match first (score desc), sponsor wins ties.
        data.sort(key=lambda j: ((j.get("match_score") or 0),
                                 bool(j.get("sponsor_confirmed"))), reverse=True)

        m1, m2, m3 = st.columns(3)
        m1.metric("📅 Jobs this week", len(data))
        m2.metric("✅ H-1B sponsors", sum(1 for j in data if j.get("sponsor_confirmed")))
        m3.metric("🆕 New (strong match)", sum(1 for j in data if j.get("status") == "New"))
        if not data:
            st.info("No jobs discovered in the last 7 days matched the filters. Loosen the "
                    "sliders above or check the crawler is running.")
            return

        def render_wk_section(subset, key_prefix):
            rows = [{
                "id": j.get("id"),
                "applied": j.get("status") == "Applied",
                "dismiss": False,
                "status": STATUS_BADGE_W.get(j.get("status"), j.get("status") or ""),
                "sponsor": "✅ H-1B" if j.get("sponsor_confirmed") else "",
                "posted": (j.get("posted_at") or "")[:10] or ("~" + (j.get("discovered_at") or "")[:10]),
                "score": j.get("match_score"),
                "title": j.get("title"),
                "company": j.get("company_name"),
                "location": j.get("location"),
                "risk": j.get("sponsorship_risk"),
                "open": apply_url(j),
            } for j in subset]
            df = pd.DataFrame(rows).set_index("id")
            grid_h = min(len(rows) + 1, 26) * 35 + 3
            editor_key = f"wk_ed_{key_prefix}_" + str(abs(hash(tuple(r["id"] for r in rows))))
            edited = st.data_editor(
                df, key=editor_key, hide_index=True, use_container_width=True, height=grid_h,
                disabled=["status", "sponsor", "posted", "score", "title", "company",
                          "location", "risk", "open"],
                column_order=["dismiss", "status", "sponsor", "posted", "score", "title",
                              "company", "location", "risk", "open", "applied"],
                column_config={
                    "applied": st.column_config.CheckboxColumn(
                        "✅ Applied?", help="Tick when you've applied — kept, never pruned."),
                    "dismiss": st.column_config.CheckboxColumn(
                        "🗑️", help="Tick to dismiss — files to 🗑️ Archived and drops it from this list."),
                    "posted": st.column_config.TextColumn(
                        "posted", help="Real posting date (or ~first-seen if source hides date)."),
                    "open": st.column_config.LinkColumn("open", display_text="open ↗"),
                    "score": st.column_config.NumberColumn("score", format="%d"),
                },
            )
            status_by_id = {j.get("id"): j.get("status") for j in subset}
            return handle_grid_edits(edited, status_by_id)

        n_sponsor = sum(1 for j in data if j.get("sponsor_confirmed"))
        st.caption(f"👉 {len(data)} jobs from the last 7 days · {n_sponsor} sponsor-confirmed "
                   f"· tick **✅ Applied?** or **🗑️** to file")

        if w_group:
            yrs_by_id = {j.get("id"): years_required(j) for j in data}
            for idx, (label, belongs) in enumerate(EXP_SECTIONS):
                subset = [j for j in data if belongs(yrs_by_id.get(j.get("id")))]
                if not subset:
                    continue
                n_sp = sum(1 for j in subset if j.get("sponsor_confirmed"))
                st.subheader(f"{label}  ·  {len(subset)} jobs"
                             + (f"  ·  {n_sp} ✅ H-1B" if n_sp else ""))
                if render_wk_section(subset, idx):
                    st.rerun()
        elif render_wk_section(data, "all"):
            st.rerun()

    posted_this_week_feed()

elif page == "🟢 Live Feed":
    st.header("🟢 Live Feed")
    st.caption("Newest jobs the crawler has detected, **ranked best-match first** and grouped "
               "into experience sections. Shows the full firehose, including Workday/iCIMS roles "
               "that hide their posting date (those now also surface on Posted Today as 🟡 LIKELY "
               "when freshly seen).")

    colA, colB, colC = st.columns([2, 2, 2])
    feed_window = colA.selectbox("Show jobs discovered within", ["Last 24 hours", "Last 3 days", "Last 7 days", "All"])
    show_filter = colB.selectbox("Show", ["Not applied yet", "Everything", "Applied only"],
                                 help="Default is 'Not applied yet' so you don't accidentally re-apply. "
                                      "'Everything' shows all jobs including already-applied.")
    auto = colC.checkbox("🔄 Auto-refresh (every 30s)", value=True)
    fhours = {"Last 24 hours": 24, "Last 3 days": 72, "Last 7 days": 168, "All": None}[feed_window]

    # Ranking + de-junking. The feed used to come back in crawl order, which put
    # score-0 noise above real matches; sorting by score and hiding the zeros is
    # what makes this page scannable like "Posted Today".
    colD, colE = st.columns([2, 4])
    sort_mode = colD.selectbox("Sort by", ["Best match first", "Newest first"], index=0)
    min_score = colE.slider(
        "Hide jobs scoring below", 0, 80, 1,
        help="Most of the feed scores 0 (off-target roles the filters didn't hard-reject). "
             "Drag to 0 to see absolutely everything.")

    # Experience bands come from the shared module-level EXP_SECTIONS.

    # Map each job's tracking status to a scannable badge so it's obvious at a
    # glance what you've already actioned vs. what's still untouched.
    STATUS_BADGE = {
        "Applied": "✅ APPLIED",
        "Follow-up": "📌 Follow-up",
        "Approved": "👍 Approved",
        "Need Review": "🔍 Review",
        "New": "🆕 New",
    }

    @st.fragment(run_every=(30 if auto else None))
    def live_feed():
        # Fetch a wider slice than we show: the API orders by discovery, so with a
        # small limit "Best match first" would only rank the newest handful and
        # genuinely good older-in-the-window jobs would never surface. 400 matches
        # what Posted Today already pulls comfortably (~0.25s warm).
        params = dict(order_by="discovered", exclude_rejected=True, limit=400, slim=True)
        if fhours:
            params["discovered_within_hours"] = fhours
        data = filter_feed(api_get("/jobs/", **params))
        if show_filter == "Not applied yet":
            data = [j for j in data if j.get("status") != "Applied"]
        elif show_filter == "Applied only":
            data = [j for j in data if j.get("status") == "Applied"]

        n_fetched = len(data)
        data = [j for j in data if (j.get("match_score") or 0) >= min_score]
        if sort_mode == "Best match first":
            # Sponsor-confirmed wins ties: same score, the H-1B employer is the
            # better use of your time.
            data.sort(key=lambda j: ((j.get("match_score") or 0),
                                     bool(j.get("sponsor_confirmed"))), reverse=True)

        n_applied = sum(1 for j in data if j.get("status") == "Applied")
        n_today = sum(1 for j in data if posted_today(j))
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Jobs shown", len(data))
        m2.metric("🔴 Posted today", n_today)
        m3.metric("✅ Applied", n_applied)
        m4.metric("Hidden (low score)", n_fetched - len(data))
        if not data:
            st.info(f"Nothing scores ≥ {min_score} in this window. Lower the score slider, "
                    "or widen the time window — the crawler adds new jobs continuously.")
            return
        # Same tick-to-apply table as "Posted Today", minus the wide `discovered`
        # timestamp that made this page sprawl sideways. Rendered once per
        # experience section so each block stays short enough to actually scan.
        def render_section(subset, key_prefix):
            rows = [{
                "id": j.get("id"),
                "applied": j.get("status") == "Applied",
                "🔴": "🔴 TODAY" if posted_today(j) else "",
                "status": STATUS_BADGE.get(j.get("status"), j.get("status") or ""),
                "sponsor": "✅ H-1B" if j.get("sponsor_confirmed") else "",
                "posted": (j.get("posted_at") or "")[:10] or "—",
                "score": j.get("match_score"),
                "title": j.get("title"),
                "company": j.get("company_name"),
                "location": j.get("location"),
                "risk": j.get("sponsorship_risk"),
                "open": apply_url(j),
            } for j in subset]

            df = pd.DataFrame(rows).set_index("id")
            # Re-key on the visible id-set: when the live crawler reshuffles rows
            # the editor resets instead of applying a stale tick to a moved row.
            editor_key = f"live_ed_{key_prefix}_" + str(abs(hash(tuple(r["id"] for r in rows))))
            edited = st.data_editor(
                df, key=editor_key, hide_index=True, use_container_width=True,
                disabled=["🔴", "status", "sponsor", "posted", "score",
                          "title", "company", "location", "risk", "open"],
                column_order=["🔴", "status", "sponsor", "posted",
                              "score", "title", "company", "location", "risk",
                              "open", "applied"],
                column_config={
                    "applied": st.column_config.CheckboxColumn(
                        "✅ Applied?", help="Tick when you've applied — it leaves the "
                        "“Not applied yet” view and is kept (never pruned)."),
                    "🔴": st.column_config.TextColumn(
                        "🔴", help="🔴 TODAY = the job's ORIGINAL posting date is today "
                        "(your local day), not when the crawler pulled it. Blank when the "
                        "posting is older, or when the source doesn't expose a real post date."),
                    "posted": st.column_config.TextColumn(
                        "posted", help="Original posting date from the source (— if unknown)."),
                    "open": st.column_config.LinkColumn("open", display_text="open ↗"),
                    "score": st.column_config.NumberColumn("score", format="%d"),
                },
            )

            # Reconcile the ticks with stored status. Idempotent — only a real
            # change (newly ticked, or un-ticked an applied one) hits the API.
            status_by_id = {j.get("id"): j.get("status") for j in subset}
            for jid, r in edited.iterrows():
                want = bool(r["applied"])
                cur = status_by_id.get(jid)
                if want and cur != "Applied":
                    set_status(int(jid), "Applied"); return True
                if not want and cur == "Applied":
                    set_status(int(jid), "New"); return True
            return False

        n_sponsor = sum(1 for j in data if j.get("sponsor_confirmed"))
        st.caption(f"👉 Tick **✅ Applied?** on any row to mark it applied (it drops out of "
                   f"“Not applied yet”) · **🔴 TODAY** = original posting is dated today "
                   f"(not the crawler pull) · ✅ H-1B = confirmed sponsor · "
                   f"{n_sponsor} of {len(data)} sponsor-confirmed in view")

        # Bucket by stated experience. years_required() reads the title+description,
        # so this costs nothing extra — the API already returned both.
        yrs_by_id = {j.get("id"): years_required(j) for j in data}
        changed = False
        for idx, (label, belongs) in enumerate(EXP_SECTIONS):
            subset = [j for j in data if belongs(yrs_by_id.get(j.get("id")))]
            if not subset:
                continue
            n_sp = sum(1 for j in subset if j.get("sponsor_confirmed"))
            st.subheader(f"{label}  ·  {len(subset)} jobs" + (f"  ·  {n_sp} ✅ H-1B" if n_sp else ""))
            if render_section(subset, idx):
                changed = True
                break
        if changed:
            st.rerun()

    live_feed()

elif page == "🚀 AI-Ranked Queue":
    st.header("🚀 AI-Ranked Apply Queue")
    st.caption("Yesterday's missed backlog, ranked by Claude for fit against "
               "your F-1/OPT + data-engineer profile. Wakes up at 05:30 UTC "
               "each day, so morning-you gets a ready-to-attack pitch list.")

    resp = api_get("/ai_rank/queue", limit=40, min_fit=0) or {}
    items = resp.get("items") or []

    if not items:
        st.info(resp.get("message") or "No AI rankings yet — check back after 05:30 UTC.")
        st.markdown("**How this works:**")
        st.markdown(
            "- Every night at 05:30 UTC, Claude reads yesterday's un-actioned "
            "sponsor jobs (24-48h old, still `New`/`Need Review`)\n"
            "- Scores each 0-100 against your profile\n"
            "- Writes a per-job pitch line you can paste into a cover letter\n"
            "- Flags red flags (mismatch, senior-only, no-sponsor language)\n"
            "- Top 5 land in your 06:00 UTC morning brief email"
        )
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("In queue", len(items))
        c2.metric("Avg AI fit", round(sum(i.get("fit_score") or 0 for i in items) / len(items)))
        c3.metric("≥ 80 fit", sum(1 for i in items if (i.get("fit_score") or 0) >= 80))

        min_fit = st.slider("Min AI fit score", 0, 100, 50, step=5)
        shown = [i for i in items if (i.get("fit_score") or 0) >= min_fit]
        st.caption(f"Showing {len(shown)} of {len(items)}")

        for i in shown:
            fit = i.get("fit_score") or 0
            color = "#1a7f37" if fit >= 80 else "#9a6700" if fit >= 60 else "#666"
            with st.container(border=True):
                head_cols = st.columns([1, 4, 2])
                head_cols[0].markdown(
                    f"<div style='background:{color};color:#fff;font-weight:700;"
                    f"font-size:22px;text-align:center;padding:12px;border-radius:6px'>"
                    f"{fit}</div>", unsafe_allow_html=True)
                sal = i.get("salary") or {}
                sal_line = ""
                if sal.get("min") or sal.get("max"):
                    lo = sal.get("min") or 0
                    hi = sal.get("max") or 0
                    if lo and hi:
                        sal_line = f" · 💰 ${lo//1000}k-${hi//1000}k"
                    elif lo:
                        sal_line = f" · 💰 ${lo//1000}k+"
                    elif hi:
                        sal_line = f" · 💰 up to ${hi//1000}k"
                    if sal.get("note") and sal["note"] != "unstated":
                        sal_line += f" ({sal['note']})"
                head_cols[1].markdown(
                    f"**{i['title']}**  \n"
                    f"{i['company_name']} · {i.get('location','?')} · "
                    f"H-1B score {i.get('h1b_score',0)}/100 · heuristic {i.get('match_score',0)}"
                    f"{sal_line}")
                head_cols[2].markdown(f"[Open job →]({i['job_url']})")
                if i.get("pitch_line"):
                    st.markdown(
                        f"<blockquote style='border-left:3px solid #0969da;padding:8px 14px;"
                        f"margin:8px 0;background:#f0f7ff;color:#111;font-style:italic'>"
                        f"&ldquo;{i['pitch_line']}&rdquo;</blockquote>",
                        unsafe_allow_html=True)
                kw = i.get("keywords") or []
                if kw:
                    tags = "".join(
                        f"<span style='display:inline-block;background:#eef2ff;color:#3730a3;"
                        f"padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600;"
                        f"margin:2px 4px 2px 0'>{k}</span>"
                        for k in kw
                    )
                    st.markdown(f"<div style='margin:6px 0'>🔑 {tags}</div>", unsafe_allow_html=True)
                dm = (i.get("referral_dm") or "").strip()
                if dm:
                    with st.expander("💬 LinkedIn DM (copy-paste ready) + 🔗 find someone to send it to"):
                        st.text_area(
                            "Message", value=dm, height=110,
                            key=f"dm_{i['id']}", label_visibility="collapsed",
                        )
                        from urllib.parse import quote as _q
                        co = i["company_name"]
                        # LinkedIn's current-company + role People search.
                        # geoUrn is LinkedIn's internal ID for "United States"
                        # (103644278). Keywords covers recruiter / hiring manager
                        # / talent acquisition variants.
                        ln_url = (
                            "https://www.linkedin.com/search/results/people/?"
                            "keywords=" + _q(f'recruiter OR "hiring manager" OR "talent acquisition"') +
                            "&company=" + _q(co) +
                            "&geoUrn=%5B%22103644278%22%5D"
                        )
                        # Google-hack fallback: some users don't have LinkedIn login;
                        # Google reveals public LinkedIn recruiter profiles anonymously.
                        gg_url = (
                            "https://www.google.com/search?q=" +
                            _q(f'site:linkedin.com/in "{co}" recruiter')
                        )
                        col_a, col_b = st.columns(2)
                        col_a.link_button("🔗 LinkedIn recruiters at " + co[:20], ln_url,
                                          use_container_width=True)
                        col_b.link_button("🔎 Google (no login needed)", gg_url,
                                          use_container_width=True)
                        st.caption(
                            "Click a recruiter → paste the DM above → send. "
                            "60s outreach loop. Sonnet-drafted DM, tailored to this JD."
                        )
                reasons = i.get("reasons") or []
                red_flags = i.get("red_flags") or []
                if reasons or red_flags:
                    rc, fc = st.columns(2)
                    if reasons:
                        rc.markdown("**✅ Fit reasons**")
                        for r in reasons:
                            rc.markdown(f"- {r}")
                    if red_flags:
                        fc.markdown("**⚠️ Red flags**")
                        for r in red_flags:
                            fc.markdown(f"- {r}")

elif page == "⚡ Fast Apply":
    st.header("⚡ Fast Apply")
    st.caption("The queue that actually matters: jobs that already passed every filter "
               "and are still un-actioned, sponsor-confirmed first. Load the autofill "
               "bookmarklet once, then it's ~45s per application instead of ~5min.")

    # Workday and iCIMS make you create an ACCOUNT per employer, so they can't be
    # autofilled and are slow by nature. Hidden by default so the queue stays
    # made of applications you can finish in under a minute.
    ACCOUNT_ATS = {"workday", "icims"}

    c1, c2, c3 = st.columns([2, 2, 2])
    sponsors_only = c1.checkbox("✅ H-1B sponsors only", value=True)
    include_slow = c2.checkbox("Include Workday/iCIMS", value=False,
                               help="These require creating an account per employer — "
                                    "no autofill possible, several minutes each.")
    min_sc = c3.slider("Min score", 0, 90, 40)

    data = filter_feed(api_get("/jobs/", exclude_rejected=True, order_by="score", limit=1000))
    queue = [j for j in data
             if j.get("status") in ("New", "Need Review")
             and (j.get("match_score") or 0) >= min_sc
             and (include_slow or (j.get("source") or "") not in ACCOUNT_ATS)
             and (not sponsors_only or j.get("sponsor_confirmed"))]
    queue.sort(key=lambda j: (bool(j.get("sponsor_confirmed")), j.get("match_score") or 0),
               reverse=True)

    m1, m2, m3 = st.columns(3)
    m1.metric("In your queue", len(queue))
    m2.metric("✅ H-1B sponsors", sum(1 for j in queue if j.get("sponsor_confirmed")))
    m3.metric("🔴 Posted today", sum(1 for j in queue if posted_today(j)))

    # ---- the bookmarklet: profile is injected HERE, at render time, so no
    # personal data ever lives in the repo. ----
    with st.expander("① Set up the autofill bookmarklet (one time)", expanded=not queue):
        prof = my_profile()
        js_path = os.path.join(os.path.dirname(__file__), "autofill.js")
        try:
            with open(js_path) as fh:
                js = fh.read().replace("__PROFILE_JSON__", json.dumps(prof))
            bookmarklet = "javascript:" + quote(js, safe="")
            st.markdown(
                "**Drag this button to your bookmarks bar** (or right-click → copy link, "
                "then make a new bookmark and paste it as the URL):")
            components.html(
                f'<a href="{bookmarklet}" '
                'style="display:inline-block;padding:10px 18px;background:#16a34a;color:#fff;'
                'border-radius:8px;font:600 15px system-ui;text-decoration:none">'
                '⚡ Fill Application</a>'
                '<p style="font:13px system-ui;color:#666;margin-top:10px">'
                'On any Greenhouse / Lever / Ashby / SmartRecruiters application page, click it '
                'once and your details drop in.</p>', height=110)
            st.caption(
                "It **fills and stops**: it never clicks Submit, never touches the résumé "
                "upload (browsers forbid scripting file inputs), never answers "
                "race/gender/veteran/disability questions, and never handles passwords. "
                "You review and submit every application yourself.")
        except FileNotFoundError:
            st.error("autofill.js not found next to app.py — can't build the bookmarklet.")

    with st.expander("② Your answers to the usual screening questions"):
        prof = my_profile()
        st.code(
            f"Full name:            {prof.get('name','')}\n"
            f"Email:                {prof.get('email','')}\n"
            f"Phone:                {prof.get('phone','')}\n"
            f"LinkedIn:             {prof.get('linkedin','')}\n"
            f"Location:             {prof.get('location','')}\n"
            f"Work authorization:   {prof.get('work_authorization','')}\n"
            "Authorized to work in the US?          Yes (F-1 OPT, STEM extension eligible)\n"
            "Will you require sponsorship?          Yes\n"
            "Earliest start date:                   Immediately",
            language="text")
        st.caption("Salary expectations and all EEO/demographic questions are left blank on "
                   "purpose — those are strategic or personal, not something to automate.")

    st.subheader("③ Work the queue")
    if not queue:
        st.info("Nothing matches. Lower the min score, untick sponsors-only, or include "
                "Workday/iCIMS.")
    else:
        st.caption(f"👉 Open ↗, click **⚡ Fill Application**, upload your résumé, submit, "
                   f"then tick **Applied?** here. Résumé: `resumes/master/`")

        # Pull AI fit scores for jobs in queue so Ram sees Claude's opinion
        # inline. Bulk-query /ai_rank/queue up to 200 items and index by job_id.
        ai_map = {}
        try:
            _ai = api_get("/ai_rank/queue", limit=200) or {}
            for it in _ai.get("items") or []:
                ai_map[it["id"]] = it.get("fit_score")
        except Exception:
            pass

        rows = [{
            "id": j.get("id"),
            "applied": j.get("status") == "Applied",
            "sponsor": "✅ H-1B" if j.get("sponsor_confirmed") else "",
            "score": j.get("match_score"),
            "ai": ai_map.get(j.get("id")),
            "title": j.get("title"),
            "company": j.get("company_name"),
            "location": j.get("location"),
            "ats": j.get("source"),
            "open": apply_url(j),
        } for j in queue[:150]]
        df = pd.DataFrame(rows).set_index("id")
        edited = st.data_editor(
            df, key="fastapply_ed_" + str(abs(hash(tuple(r["id"] for r in rows)))),
            hide_index=True, use_container_width=True,
            disabled=["sponsor", "score", "ai", "title", "company", "location", "ats", "open"],
            column_order=["sponsor", "score", "ai", "title", "company", "location",
                          "ats", "open", "applied"],
            column_config={
                "applied": st.column_config.CheckboxColumn(
                    "✅ Applied?", help="Tick once you've actually submitted."),
                "open": st.column_config.LinkColumn("open", display_text="open ↗"),
                "score": st.column_config.NumberColumn("score", format="%d"),
                "ai": st.column_config.NumberColumn(
                    "🚀 AI", format="%d",
                    help="Claude's fit score (nightly, only for AI-ranked pool). "
                         "Blank means not yet ranked — check tomorrow's 05:30 UTC batch."),
            },
        )
        status_by_id = {j.get("id"): j.get("status") for j in queue}
        for jid, r in edited.iterrows():
            if bool(r["applied"]) and status_by_id.get(jid) != "Applied":
                set_status(int(jid), "Applied")
                st.rerun()

elif page == "Today's Best Jobs":
    _thr = (api_get("/jobs/stats/summary") or {}).get("good_threshold")
    st.header(f"Today's Best Jobs (score ≥ {_thr})" if _thr else "Today's Best Jobs")
    # "New" status is already gated by the configured threshold in the scheduler,
    # so don't re-filter by score here (that double-filtering hid most matches).
    df = jobs_df(status="New", min_score=0, feed_only=True)

    # Experience filter — hide roles that demand more years than you have.
    EXP_CAPS = {
        "Any": None,
        "Entry only (0–2 yrs)": 2,
        "≤ 3 years": 3,
        "≤ 5 years": 5,
    }
    c1, c2, c3 = st.columns([1, 1, 2])
    exp_choice = c1.selectbox("Max experience required", list(EXP_CAPS.keys()), index=0)
    sort_choice = c2.selectbox("Sort by", ["Best match", "Lowest competition first"], index=0)
    keep_unstated = c3.checkbox(
        "Keep jobs that don't state a year requirement", value=True,
        help="Most entry-level roles never list a number. Uncheck to show ONLY jobs that explicitly fit.")
    total = len(df)
    cap = EXP_CAPS[exp_choice]
    if cap is not None and not df.empty:
        yrs = df.apply(lambda r: years_required(r.to_dict()), axis=1)
        mask = yrs.isna() & keep_unstated
        mask |= yrs.notna() & (yrs <= cap)
        df = df[mask]

    # Sort: best-match (score) or low-competition first (then score within tier).
    if not df.empty and {"source", "match_score"} <= set(df.columns):
        df = df.assign(_comp=df["source"].map(lambda s: competition(s)[2]))
        if sort_choice == "Lowest competition first":
            df = df.sort_values(["_comp", "match_score"], ascending=[True, False])
        else:
            df = df.sort_values("match_score", ascending=False)
        df = df.drop(columns=["_comp"])  # internal sort key, don't pass to cards

    if cap is None:
        st.caption(f"{len(df)} jobs · sorted by {sort_choice.lower()}")
    else:
        st.caption(f"{len(df)} of {total} jobs · ≤ {cap} yrs · sorted by {sort_choice.lower()}")
    if df.empty:
        st.info("No strong matches yet. Run a crawl: `python scripts/crawl_all.py`")
    else:
        for _, row in df.iterrows():
            render_job_card(row.to_dict(), actions=("Approve", "Review", "Reject"))

elif page == "Need Review":
    st.header("Need Review (unclear sponsorship / level / years)")
    df = jobs_df(status="Need Review", feed_only=True)
    st.caption(f"{len(df)} jobs")
    for _, row in df.iterrows():
        render_job_card(row.to_dict(), actions=("Approve", "Reject"))

elif page == "Approved":
    st.header("Approved — ready to apply")
    df = jobs_df(status="Approved")
    st.caption(f"{len(df)} jobs")
    for _, row in df.iterrows():
        render_job_card(row.to_dict(), actions=("Mark Applied", "Reject"))

elif page == "Applied":
    st.header("Applied")
    st.caption("Jobs you've applied to — auto-suggested **follow-up emails** below. Studies "
               "put follow-up response rates 30-50% higher than cold applications; the tool "
               "guesses `careers@company.com`, drafts a tailored message, and opens Gmail "
               "compose in a new tab. Review + hit Send.")

    df = jobs_df(status="Applied")
    if df.empty:
        st.info("Nothing applied yet.")
    else:
        from urllib.parse import quote, urlparse
        prof = my_profile()
        my_name = (prof.get("full_name") or prof.get("name") or "").strip() or "there"

        def _guess_domain(row) -> str:
            """Best-guess employer domain for a careers@ / hiring@ email."""
            # 1) If the job_url points to the company's own site (not the ATS), use it.
            job_url = (row.get("job_url") or "").strip()
            if job_url:
                host = urlparse(job_url).netloc.lower()
                if host and not any(ats in host for ats in
                        ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
                         "icims.com", "bamboohr.com", "smartrecruiters.com", "workable.com",
                         "rippling.com", "recruitee.com", "himalayas.app", "jobvite.com",
                         "gem.com", "workday.com", "eightfold.ai", "themuse.com")):
                    return host.replace("www.", "")
            # 2) Fallback: squash company name -> .com. Same trick USCIS URL-guesser uses.
            name = (row.get("company_name") or "").lower()
            name = re.sub(r"\b(inc|corp|corporation|llc|ltd|limited|company|co|group)\b", " ", name)
            name = re.sub(r"[^a-z0-9]+", "", name)
            return f"{name}.com" if 3 <= len(name) <= 30 else ""

        def _days_since(row) -> int:
            """Rough days-since-applied via updated_at (proxy; DB has no explicit
            applied_at column and updated_at only bumps on status change once we
            mark Applied, so it's a solid approximation)."""
            u = _parse_dt(row.get("updated_at"))
            if not u: return 0
            return max(0, (datetime.now(timezone.utc).replace(tzinfo=None) - u.replace(tzinfo=None)).days)

        def _extract_hook(desc: str) -> str:
            """Pull one company-specific hook from the job description that reads
            like actual research: prefer a distinctive tech/product mention,
            else the first sentence. Falls back to '' if nothing usable."""
            if not desc: return ""
            # Look for distinctive proper-noun tech/product mentions
            distinctive = re.findall(
                r"\b(Snowflake|Databricks|BigQuery|Redshift|Airflow|dbt|Kafka|"
                r"Spark|PySpark|Kubernetes|Terraform|Iceberg|Delta Lake|Kinesis|"
                r"Flink|dagster|Prefect|Snowpark|Materialize|Trino|Presto|"
                r"Fivetran|Airbyte|dagster|Grafana|Prometheus|Datadog)\b",
                desc, re.I,
            )
            if distinctive:
                # De-dupe preserving order, take up to 3
                seen, uniq = set(), []
                for t in distinctive:
                    if t.lower() not in seen:
                        seen.add(t.lower()); uniq.append(t)
                    if len(uniq) >= 3: break
                return f"the {'/'.join(uniq)} stack you're building on"
            # Fallback: first sentence, cleaned
            first = re.split(r"(?<=[.!?])\s+", desc.strip(), maxsplit=1)[0]
            first = re.sub(r"\s+", " ", first).strip()
            if 20 <= len(first) <= 220:
                return first.rstrip(".") + "."
            return ""

        def _followup_url(row) -> str:
            """Gmail compose URL with a tailored draft. Opens in browser -> user
            reviews + sends. Includes a company-specific hook (mentioned tech
            stack or first-sentence of JD) so it reads like real research."""
            domain = _guess_domain(row)
            days = _days_since(row)
            title = (row.get("title") or "the role").strip()
            company = (row.get("company_name") or "your team").strip()
            hook = _extract_hook(row.get("description") or "")
            # Build a "specific detail" sentence to slot into every template.
            detail = ""
            if hook:
                # If hook mentions tech stack (starts with "the"), phrase as "excited about"
                if hook.startswith("the "):
                    detail = f" I'm particularly excited about {hook}"
                else:
                    detail = f" What caught my eye: {hook}"

            # Message tone shifts with days-since-applied.
            if days <= 3:
                subj = f"Introduction: {my_name} for {title}"
                body = (
                    f"Hi {company} team,\n\n"
                    f"I recently submitted my application for the {title} role and wanted "
                    f"to briefly introduce myself.{detail}\n\n"
                    f"I'm a data engineer with hands-on experience building the kind of "
                    f"pipelines this role calls for, and I'd love the chance to talk about "
                    f"how my background fits the team.\n\n"
                    f"Happy to send over additional context or answer questions.\n\n"
                    f"Best,\n{my_name}"
                )
            elif days <= 10:
                subj = f"Following up: {title} application"
                body = (
                    f"Hi {company} team,\n\n"
                    f"I'm following up on my application for the {title} role, submitted "
                    f"about a week ago.{detail}\n\n"
                    f"I remain very interested and would welcome any update on where things "
                    f"stand or what the next steps look like. Let me know if anything else "
                    f"would help move it forward.\n\n"
                    f"Thanks for your time.\n\n"
                    f"Best,\n{my_name}"
                )
            else:
                subj = f"Checking in one last time: {title}"
                body = (
                    f"Hi {company} team,\n\n"
                    f"Circling back on my application for {title}, submitted {days} days "
                    f"ago.{detail}\n\n"
                    f"If the role is still open I'd love to be considered; if the team "
                    f"has moved on, a quick note would help me plan accordingly.\n\n"
                    f"Either way, thanks for your consideration.\n\n"
                    f"Best,\n{my_name}"
                )
            to = f"careers@{domain}" if domain else ""
            return (
                "https://mail.google.com/mail/?view=cm&fs=1"
                f"&to={quote(to)}&su={quote(subj)}&body={quote(body)}"
            )

        df = df.copy()
        df["days_ago"] = df.apply(lambda r: _days_since(r), axis=1)
        df["apply"] = df.apply(lambda r: apply_url(r.to_dict()), axis=1)
        df["follow_up"] = df.apply(lambda r: _followup_url(r.to_dict()), axis=1)
        df["referral"] = df.apply(lambda r: referral_url(r.to_dict()), axis=1)
        df["email_guess"] = df.apply(lambda r: f"careers@{_guess_domain(r.to_dict())}" if _guess_domain(r.to_dict()) else "(couldn't guess)", axis=1)

        due = int((df["days_ago"] >= 4).sum())
        m1, m2, m3 = st.columns(3)
        m1.metric("Total applied", len(df))
        m2.metric("Due for follow-up", due, help="Applied 4+ days ago with no explicit response yet.")
        m3.metric("Avg days since apply", f"{df['days_ago'].mean():.1f}")

        show = ["days_ago", "title", "company_name", "location", "match_score",
                "sponsorship_risk", "email_guess", "follow_up", "referral", "apply"]
        st.dataframe(
            df[[c for c in show if c in df.columns]].sort_values("days_ago", ascending=False),
            use_container_width=True, hide_index=True,
            column_config={
                "days_ago":       st.column_config.NumberColumn("days ago", format="%d"),
                "match_score":    st.column_config.NumberColumn("score", format="%d"),
                "email_guess":    st.column_config.TextColumn("suggested to", help="Best-guess address — verify before sending."),
                "follow_up":      st.column_config.LinkColumn("📧 draft follow-up", display_text="draft ↗",
                                    help="Opens Gmail compose in a new tab with a tailored message pre-filled. Tone shifts with days-since-applied (day 1-3 intro / day 4-10 follow-up / day 11+ last check-in)."),
                "referral":       st.column_config.LinkColumn("🤝 find referral", display_text="LinkedIn ↗",
                                    help="Opens LinkedIn people search for this company + role. Referred applications get 5-10x more callbacks than cold ones. Message a 1st-degree connection at the company for a warm intro before the recruiter sees your app."),
                "apply":          st.column_config.LinkColumn("apply page", display_text="open ↗"),
            }
        )
        st.caption("📧 **draft ↗** opens Gmail compose · 🤝 **LinkedIn ↗** finds people at "
                   "the company you might know for a warm intro. Send the follow-up email "
                   "AFTER you've reached out to a connection — recruiters recognize referrals "
                   "and prioritize them.")

        # ---- Referral outreach kit: pre-drafted DMs + multi-angle search links ----
        with st.expander("💬 Referral outreach kit — pre-drafted LinkedIn DMs per applied job"):
            st.caption("Copy the message, click a search link, DM someone. Referral apps "
                       "get 5-10x more callbacks than cold ones -- this is the biggest "
                       "single lift on your response rate.")
            for _, row in df.sort_values("days_ago", ascending=False).head(20).iterrows():
                job_row = row.to_dict()
                title = job_row.get("title") or "(no title)"
                company = job_row.get("company_name") or "?"
                with st.container(border=True):
                    st.markdown(f"**{title}** — {company}  ·  applied {int(job_row.get('days_ago', 0))}d ago")
                    urls = referral_search_urls(job_row)
                    link_bits = [
                        f"[👥 role]({urls['role']})",
                        f"[🎯 recruiter]({urls['recruiter']})",
                    ]
                    st.markdown("Search LinkedIn: " + "  ·  ".join(link_bits))
                    st.code(referral_intro_message(job_row, my_name), language="text")

elif page == "Rejected":
    st.header("Rejected")
    df = jobs_df(status="Rejected")
    st.caption(f"{len(df)} jobs")
    if not df.empty:
        show = ["title", "company_name", "rejection_reason", "match_score", "job_url"]
        st.dataframe(df[[c for c in show if c in df.columns]], use_container_width=True)

elif page == "🗑️ Deleted":
    st.header("🗑️ Deleted")
    st.caption("Jobs you dismissed with the 🗑️ box (on Posted Today and other lists). They're "
               "kept out of your feeds and protected from re-scoring, so they won't come back. "
               "Tick **Restore** to send one back to New.")
    df = jobs_df(status="Archived")
    if df.empty:
        st.info("Nothing archived. Dismiss a job with its 🗑️ box on Posted Today to file it here.")
    else:
        df = df.copy()
        df["restore"] = False
        df["apply"] = df.apply(lambda r: apply_url(r.to_dict()), axis=1)
        show = ["restore", "title", "company_name", "location", "match_score",
                "sponsorship_risk", "apply"]
        view = df[[c for c in show if c in df.columns]]
        view.index = df["id"]
        edited = st.data_editor(
            view, hide_index=True, use_container_width=True,
            disabled=[c for c in view.columns if c != "restore"],
            column_config={
                "restore": st.column_config.CheckboxColumn(
                    "Restore", help="Tick to send this job back to New (it'll reappear in your feeds)."),
                "apply": st.column_config.LinkColumn("apply", display_text="open ↗"),
                "match_score": st.column_config.NumberColumn("score", format="%d"),
            },
        )
        for jid, r in edited.iterrows():
            if bool(r["restore"]):
                set_status(int(jid), "New")
                st.rerun()

elif page == "Companies":
    st.header("Companies")
    data = api_get("/companies/") or []
    df = pd.DataFrame(data)
    if df.empty:
        st.info("No companies. Run `python scripts/seed_companies.py`.")
    else:
        show = ["name", "ats_type", "priority", "h1b_history_score", "is_active", "last_checked_at", "notes"]
        st.dataframe(df[[c for c in show if c in df.columns]], use_container_width=True)
        st.subheader("Crawl one company now")
        opt = {f"{c['name']} (#{c['id']})": c["id"] for c in data}
        pick = st.selectbox("Company", list(opt.keys()))
        if st.button("Crawl now"):
            res = api_post(f"/companies/{opt[pick]}/crawl")
            if res:
                st.success(res)

    st.subheader("Add a company")
    with st.form("add_company"):
        name = st.text_input("Name")
        career_url = st.text_input("Career URL or ATS token")
        ats_type = st.selectbox("ATS type", ["greenhouse", "lever", "ashby", "workday", "smartrecruiters"])
        h1b = st.slider("H-1B history score", 0, 100, 0)
        priority = st.selectbox("Priority", ["high", "medium", "low", "skip"], index=1)
        notes = st.text_input("Notes")
        if st.form_submit_button("Add"):
            res = api_post("/companies/", {
                "name": name, "career_url": career_url, "ats_type": ats_type,
                "h1b_history_score": h1b, "priority": priority, "is_active": True, "notes": notes,
            })
            if res:
                st.success(f"Added {res['name']}")
                st.rerun()

elif page == "Stats":
    st.header("Stats")
    s = api_get("/jobs/stats/summary")
    if s:
        c1, c2, c3 = st.columns(3)
        c1.metric("Total jobs", s["total_jobs"])
        _t = s.get("good_threshold", "—")
        c2.metric(f"Score ≥ {_t}", s.get("above_threshold", 0))
        c3.metric("Approved", s["by_status"].get("Approved", 0))
        st.subheader("By status")
        st.bar_chart(pd.Series(s["by_status"]))
        st.subheader("Top sources")
        st.write(s["top_sources"])
        st.subheader("Top companies")
        st.write(s["top_companies"])
        st.subheader("Common rejection reasons")
        st.write(s["common_rejection_reasons"])

elif page == "📋 Daily Audit":
    st.header("📋 Daily Audit")
    st.caption("Automatically generated at 06:00 UTC by scripts/daily_audit.py — "
               "roster growth, capacity, discovery rate, dead-weight, sponsor "
               "coverage, top rejections and storage. Use it to see whether the "
               "pipeline is delivering more jobs each day.")

    listing = api_get("/audit/") or {"reports": [], "latest": None}
    dates = listing.get("reports") or []
    latest = listing.get("latest")

    if not dates:
        st.warning(
            "No audit reports on disk yet — the cron fires at 06:00 UTC each day. "
            "Run this once to backfill today:\n\n"
            "```\ndocker exec -w /app/backend job-control-center-backend-1 "
            "python scripts/daily_audit.py\n```"
        )
    else:
        # Date picker: default to latest, let user browse history.
        col1, col2 = st.columns([1, 3])
        pick = col1.selectbox("Report", dates, index=0,
                              help=f"{len(dates)} report(s) on disk. Newest first.")
        col2.caption(f"Latest available: **{latest}** · "
                     f"tomorrow's report writes at 06:00 UTC.")

        # Three views: the pre-rendered text (fast to skim), structured
        # JSON tables (for numeric comparison across days), and the live
        # registry snapshot + would-be maintenance changes.
        tab_text, tab_data, tab_registry = st.tabs(
            ["📝 Report", "🔢 Structured", "🏢 Registry"]
        )

        with tab_text:
            txt = api_get(f"/audit/{pick}/txt") or {}
            body = txt.get("content", "")
            if body:
                st.code(body, language="text")
                st.download_button("Download .txt", body, file_name=f"audit-{pick}.txt")
            else:
                st.error(f"No text report available for {pick}.")

        with tab_data:
            data = api_get(f"/audit/{pick}") or {}
            if not data:
                st.error(f"No JSON report available for {pick}.")
            else:
                cap = data.get("capacity") or {}
                roster = data.get("roster") or {}
                disc = data.get("discovery_24h") or {}
                dead = data.get("dead_weight") or {}
                sp = data.get("sponsors") or {}

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Active companies", roster.get("active_total", "—"))
                c2.metric("Scans/day", f"{cap.get('demand_per_day', 0):,}")
                c3.metric("Jobs discovered (24h)", disc.get("total", "—"))
                c4.metric("Sponsors (score≥50)", sp.get("confirmed_total", "—"))

                st.subheader("Roster by tier")
                st.bar_chart(pd.Series(roster.get("by_tier") or {}))

                st.subheader("Discovery last 24h by ATS")
                by_ats = disc.get("by_ats") or {}
                if by_ats:
                    st.bar_chart(pd.Series(by_ats))

                st.subheader("Dead-weight (active low-tier, 0 jobs ever)")
                st.metric("Total", dead.get("total", 0))
                st.metric("Prune-ready (>60d, still 0 after 24h)", dead.get("prune_ready_confident", 0))

                st.subheader("Top rejection reasons (24h)")
                rej = data.get("top_rejections_24h") or []
                if rej:
                    st.dataframe(pd.DataFrame(rej), use_container_width=True, hide_index=True)

                with st.expander("Raw JSON"):
                    st.json(data)

        with tab_registry:
            st.caption("Live company roster snapshot + a preview of what "
                       "the weekly registry-maintenance sweep would change. "
                       "Numbers here refresh on page load — they're not tied "
                       "to the daily audit file.")

            stats = api_get("/audit/registry/stats") or {}
            preview = api_get("/audit/registry/preview") or {}

            r1, r2, r3, r4 = st.columns(4)
            r1.metric("Active companies", stats.get("active_total", "—"))
            r2.metric("Archived", stats.get("archived", "—"))
            r3.metric("Hiring last 24h", stats.get("companies_hiring_last_24h", "—"))
            r4.metric("Would-be changes",
                      (preview.get("promoted", 0)
                       + preview.get("demoted", 0)
                       + preview.get("archived", 0)))

            st.subheader("Roster by tier")
            by_tier = stats.get("by_tier") or {}
            if by_tier:
                st.bar_chart(pd.Series(by_tier))

            st.subheader("Lifecycle distribution")
            lc = preview.get("lifecycle_counts") or {}
            if lc:
                st.bar_chart(pd.Series(lc))
                st.caption(
                    "discovered = just added, awaiting first crawl · "
                    "validated = queued · hiring = produced ≥1 job in 30d · "
                    "dormant = active board, no jobs in 30d · "
                    "archived = is_active=False"
                )

            st.subheader("Weekly maintenance preview (dry-run)")
            st.caption(
                "What `registry_maintenance.py --apply` would change this "
                "week. Runs automatically inside the discovery container "
                "after harvest + auto_discover. Sponsors are never "
                "auto-archived — hiring velocity is a proxy, not a substitute."
            )
            c1, c2, c3 = st.columns(3)
            c1.metric("Promote → higher tier", preview.get("promoted", 0))
            c2.metric("Demote → lower tier", preview.get("demoted", 0))
            c3.metric("Archive", preview.get("archived", 0))

            st.subheader("Active companies by ATS platform")
            by_ats = stats.get("by_ats") or {}
            if by_ats:
                # Top 20 by count for readability.
                top = dict(sorted(by_ats.items(),
                                   key=lambda kv: -(kv[1] or 0))[:20])
                st.dataframe(
                    pd.DataFrame(top.items(), columns=["ATS", "companies"]),
                    hide_index=True, use_container_width=True,
                )

elif page == "⚙️ Gmail Settings":
    st.header("⚙️ Gmail Settings")
    st.caption("Give JCC read+send access to your Gmail so it can (a) detect recruiter "
               "responses to Applied jobs, and (b) send follow-up emails as you. Uses a "
               "**Gmail App Password** (not your regular Google password). Stored in a "
               "gitignored file inside the persistent DB volume on your droplet — same "
               "trust boundary as the DB. Never leaves the droplet.")

    settings = api_get("/gmail/settings") or {}
    if settings.get("configured"):
        st.success(f"✅ Configured for **{settings.get('email')}**. Last poll: "
                   f"**{settings.get('last_poll') or 'not yet'}**  ·  "
                   f"Matched messages ever: **{settings.get('total_matched', 0)}**")
    else:
        st.warning("Not configured yet. Follow the 2-step setup below.")

    st.subheader("① Create a Gmail App Password (one time, 2 min)")
    st.markdown(
        "1. Make sure **2-Step Verification is ON** on your Google account "
        "([enable here](https://myaccount.google.com/signinoptions/two-step-verification)).\n"
        "2. Go to [**App Passwords**](https://myaccount.google.com/apppasswords).\n"
        "3. Type a name like `Job Control Center` → **Create**.\n"
        "4. Copy the 16-character password (spaces don't matter).\n"
        "5. Paste it below."
    )

    st.subheader("② Paste it here")
    with st.form("gmail_setup"):
        gm_email = st.text_input("Your Gmail address",
                                 value=settings.get("email", ""),
                                 placeholder="you@gmail.com")
        gm_pw    = st.text_input("App password (16 chars, spaces OK)",
                                 type="password", placeholder="xxxx xxxx xxxx xxxx")
        submitted = st.form_submit_button("💾 Save & test connection")
    if submitted:
        if not gm_email or not gm_pw:
            st.error("Fill both fields.")
        else:
            resp = api_post("/gmail/settings",
                            {"email": gm_email.strip(), "app_password": gm_pw.replace(" ", "")})
            if resp and resp.get("ok"):
                st.success(f"✅ Saved. IMAP login test: **{resp.get('login_test')}**. "
                           f"First poll fires within 15 min.")
            else:
                err = (resp or {}).get("error") or "unknown error"
                st.error(f"❌ Save/test failed: {err}")

    if settings.get("configured"):
        st.divider()
        st.subheader("③ Manual poll")
        if st.button("🔄 Run gmail poll now"):
            with st.spinner("Polling Gmail…"):
                r = api_post("/gmail/poll", {}) or {}
            if r.get("matched") is not None:
                st.success(f"Matched **{r['matched']}** new messages "
                           f"(interviews: {r.get('interview',0)}, rejections: {r.get('rejection',0)})")
            else:
                st.error(f"Poll failed: {r.get('error') or r}")
        st.divider()
        st.subheader("④ Danger zone")
        if st.button("🗑️ Remove Gmail settings"):
            api_post("/gmail/settings/clear", {})
            st.rerun()

elif page == "📬 Inbox":
    st.header("📬 Inbox — recruiter responses")
    st.caption("Messages the Gmail watcher matched to your Applied jobs. Auto-classified "
               "as interview / rejection / ack / other. Configure Gmail first at "
               "**⚙️ Gmail Settings** if this page is empty.")

    msgs = api_get("/gmail/messages") or []
    if not msgs:
        st.info("No matched messages yet. Either Gmail isn't set up, or no recruiters "
                "have replied yet. First poll runs within 15 min of setup.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        n_int = sum(1 for m in msgs if m.get("classification") == "interview")
        n_rej = sum(1 for m in msgs if m.get("classification") == "rejection")
        n_ack = sum(1 for m in msgs if m.get("classification") == "ack")
        c1.metric("📬 Total", len(msgs))
        c2.metric("🎯 Interviews", n_int)
        c3.metric("❌ Rejections", n_rej)
        c4.metric("📥 Acks", n_ack)

        klass_filter = st.multiselect(
            "Show", ["interview", "rejection", "ack", "other"],
            default=["interview", "rejection", "ack", "other"],
        )
        filtered = [m for m in msgs if m.get("classification") in klass_filter]
        badge = {"interview": "🎯 INTERVIEW", "rejection": "❌ REJECTION",
                 "ack": "📥 ACK", "other": "◼️ OTHER"}
        def _gmail_search_url(m: dict) -> str:
            """Build a Gmail search URL that opens the actual email in the user's
            inbox. Prefers a subject-based search (highly specific); falls back
            to from-address search."""
            from urllib.parse import quote as _q
            subj = (m.get("subject") or "").strip()
            frm = (m.get("from_addr") or "").strip()
            # Extract just the email address from "Name <addr@example.com>"
            frm_addr = frm
            m2 = re.search(r"<([^>]+)>", frm)
            if m2:
                frm_addr = m2.group(1)
            if subj:
                # Quote the subject for exact-phrase matching
                q = f'subject:"{subj}"'
            elif frm_addr:
                q = f'from:{frm_addr}'
            else:
                return ""
            return f"https://mail.google.com/mail/u/0/#search/{_q(q)}"

        rows = [{
            "when":    (m.get("received_at") or "")[:16].replace("T", " "),
            "type":    badge.get(m.get("classification"), m.get("classification") or ""),
            "from":    m.get("from_addr") or "",
            "job":     m.get("job_title") or "",
            "company": m.get("company_name") or "",
            "subject": m.get("subject") or "",
            "preview": (m.get("snippet") or "")[:140],
            "gmail":   _gmail_search_url(m),
        } for m in sorted(filtered, key=lambda x: x.get("received_at") or "", reverse=True)]
        st.dataframe(
            pd.DataFrame(rows), hide_index=True, use_container_width=True,
            column_config={
                "gmail": st.column_config.LinkColumn(
                    "📧 open", display_text="Gmail ↗",
                    help="Opens Gmail search for this exact email subject in a new tab."),
            },
        )

elif page == "🔙 Undo":
    st.header("🔙 Undo — recent status changes this session")
    st.caption("Every time you tick ✅ Applied / 🗑️ Delete / any status button, JCC "
               "snapshots the previous status. Click Undo to revert. Session-scoped — "
               "when you close the tab, this history clears.")
    stack = list(reversed(st.session_state.get("undo_stack") or []))
    if not stack:
        st.info("No status changes yet this session — click ✅ Applied or 🗑️ Delete "
                "on any job and it'll show up here for one-click undo.")
    else:
        st.metric("Actions in this session", len(stack))
        for i, entry in enumerate(stack):
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 2, 1])
                c1.markdown(f"**{(entry['title'] or '(untitled)')[:70]}**  \n"
                            f"_{entry['company']}_ · at {entry['when'][11:16]} UTC")
                c2.markdown(f"→ **{entry['new_status']}**  \n"
                            f"was: {entry['old_status']}")
                if c3.button("↩️ Undo", key=f"undo_page_{i}"):
                    # This entry might not be the top of stack -- find + pop it
                    real_stack = st.session_state.get("undo_stack") or []
                    if entry in real_stack:
                        real_stack.remove(entry)
                    api_patch(f"/jobs/{entry['job_id']}", {"status": entry["old_status"]})
                    st.success(f"Reverted → {entry['old_status']}")
                    st.rerun()
        if st.button("🗑️ Clear session history"):
            st.session_state["undo_stack"] = []
            st.rerun()

elif page == "📊 Analytics":
    st.header("📊 Application Analytics")
    st.caption("The feedback loop: what's actually working? Applies over time, response "
               "rates per source, funnel from applied → response → interview. Requires "
               "**Gmail integration** for response tracking (⚙️ Gmail Settings). Without "
               "it, this page only shows applied counts.")

    # Pull applied jobs + all matched Gmail messages.
    applied = api_get("/jobs/", status="Applied", limit=3000) or []
    msgs = api_get("/gmail/messages", limit=2000) or []
    # Index messages by job_id -> list of {classification, received_at}
    msg_by_job: dict[int, list[dict]] = {}
    for m in msgs:
        jid = m.get("job_id")
        if jid is None: continue
        msg_by_job.setdefault(jid, []).append(m)

    if not applied:
        st.info("Nothing applied yet — go apply to a few jobs first, then this page "
                "will show your funnel + response rates.")
    else:
        # ---- top-line metrics ----
        n_total = len(applied)
        n_with_response = sum(1 for j in applied if j.get("id") in msg_by_job)
        n_interview = sum(1 for j in applied
                          if any(m.get("classification") == "interview"
                                 for m in msg_by_job.get(j.get("id"), [])))
        n_reject = sum(1 for j in applied
                       if any(m.get("classification") == "rejection"
                              for m in msg_by_job.get(j.get("id"), [])))
        n_ack = sum(1 for j in applied
                    if any(m.get("classification") == "ack"
                           for m in msg_by_job.get(j.get("id"), [])))
        response_rate = (n_with_response * 100 // n_total) if n_total else 0
        interview_rate = (n_interview * 100 // n_total) if n_total else 0

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Total applied", n_total)
        m2.metric("Any response", f"{n_with_response} ({response_rate}%)")
        m3.metric("🎯 Interviews", n_interview, help=f"{interview_rate}% of applied")
        m4.metric("❌ Rejections", n_reject)
        m5.metric("📥 Acks only", n_ack)

        if not msg_by_job:
            st.info("💡 No recruiter responses tracked yet. Set up Gmail integration "
                    "in ⚙️ Gmail Settings to auto-detect responses in your inbox.")

        st.divider()

        # ---- applies-per-day (last 30 days) ----
        st.subheader("📅 Applies per day (last 30 days)")
        today = datetime.now(timezone.utc).date()
        thirty_ago = today - timedelta(days=30)
        by_day: dict = {}
        for j in applied:
            u = _parse_dt(j.get("updated_at"))
            if not u: continue
            d = u.date()
            if d < thirty_ago: continue
            by_day[d] = by_day.get(d, 0) + 1
        # Fill in zero days so the chart is continuous
        days = [thirty_ago + timedelta(days=i) for i in range(31)]
        chart_df = pd.DataFrame({"date": days,
                                 "applied": [by_day.get(d, 0) for d in days]})
        st.bar_chart(chart_df.set_index("date"), height=220)

        st.divider()

        # ---- funnel by source ----
        st.subheader("🎯 Funnel by source")
        st.caption("Which crawler sources are actually converting? A high applied count "
                   "with a low response rate = you're wasting time on that source.")
        by_src: dict[str, dict] = {}
        for j in applied:
            src = j.get("source") or "unknown"
            b = by_src.setdefault(src, {"applied": 0, "responded": 0,
                                        "interviews": 0, "rejections": 0})
            b["applied"] += 1
            resp = msg_by_job.get(j.get("id"), [])
            if resp:
                b["responded"] += 1
                if any(m.get("classification") == "interview" for m in resp):
                    b["interviews"] += 1
                if any(m.get("classification") == "rejection" for m in resp):
                    b["rejections"] += 1
        src_rows = [{
            "source": s,
            "applied": v["applied"],
            "responded": v["responded"],
            "interviews": v["interviews"],
            "rejections": v["rejections"],
            "response_rate_%": (v["responded"] * 100 // v["applied"]) if v["applied"] else 0,
            "interview_rate_%": (v["interviews"] * 100 // v["applied"]) if v["applied"] else 0,
        } for s, v in sorted(by_src.items(), key=lambda kv: -kv[1]["applied"])]
        st.dataframe(pd.DataFrame(src_rows), hide_index=True, use_container_width=True)

        st.divider()

        # ---- funnel by sponsor status ----
        st.subheader("✅ Sponsor vs non-sponsor conversion")
        st.caption("If sponsor-confirmed jobs convert MUCH better than unknown-sponsor "
                   "ones, tighten your Best Matches to sponsor-only.")
        n_spon_applied = sum(1 for j in applied if j.get("sponsor_confirmed"))
        n_spon_resp = sum(1 for j in applied if j.get("sponsor_confirmed")
                          and j.get("id") in msg_by_job)
        n_nonspon_applied = n_total - n_spon_applied
        n_nonspon_resp = n_with_response - n_spon_resp
        spon_df = pd.DataFrame([
            {"cohort": "✅ H-1B sponsor-confirmed",
             "applied": n_spon_applied, "responded": n_spon_resp,
             "response_rate_%": (n_spon_resp * 100 // n_spon_applied) if n_spon_applied else 0},
            {"cohort": "❓ Non-confirmed",
             "applied": n_nonspon_applied, "responded": n_nonspon_resp,
             "response_rate_%": (n_nonspon_resp * 100 // n_nonspon_applied) if n_nonspon_applied else 0},
        ])
        st.dataframe(spon_df, hide_index=True, use_container_width=True)

        st.divider()

        # ---- days to response distribution ----
        if msg_by_job:
            st.subheader("⏱️ Days from apply → first response")
            gaps = []
            for j in applied:
                applied_at = _parse_dt(j.get("updated_at"))
                resp = msg_by_job.get(j.get("id"), [])
                if not (applied_at and resp): continue
                # Earliest response for this job
                earliest = min((_parse_dt(m.get("received_at")) for m in resp
                                if _parse_dt(m.get("received_at"))), default=None)
                if not earliest: continue
                delta = (earliest - applied_at).days
                if 0 <= delta <= 90:  # sanity clamp
                    gaps.append(delta)
            if gaps:
                gap_df = pd.DataFrame({"days_to_response": gaps})
                st.bar_chart(gap_df["days_to_response"].value_counts().sort_index(), height=200)
                st.caption(f"Median: **{sorted(gaps)[len(gaps)//2]} days** · "
                           f"Mean: **{sum(gaps)/len(gaps):.1f} days** · "
                           f"({len(gaps)} responses tracked)")

        # ---- top companies by score that got NO response (retry targets) ----
        st.divider()
        st.subheader("🎯 High-score applied jobs with NO response yet (retry targets)")
        no_response = [j for j in applied if j.get("id") not in msg_by_job
                       and (j.get("match_score") or 0) >= 50]
        no_response.sort(key=lambda j: -(j.get("match_score") or 0))
        if no_response:
            rows = [{
                "score": j.get("match_score"),
                "title": j.get("title"),
                "company": j.get("company_name"),
                "sponsor": "✅" if j.get("sponsor_confirmed") else "",
                "days_ago": (datetime.now(timezone.utc).replace(tzinfo=None) - _parse_dt(j.get("updated_at"))).days
                            if _parse_dt(j.get("updated_at")) else 0,
            } for j in no_response[:20]]
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        else:
            st.info("Every high-score applied job has a tracked response. Nice.")

elif page == "🎓 Interview Prep":
    st.header("🎓 AI Interview Prep")
    st.caption("Pick an applied job → Claude generates ~10 behavioral questions "
               "(STAR-format answer templates) + 5 technical questions grounded in "
               "the JD's actual tech stack + suggested talking points. Costs ~$0.02 "
               "per generation on Anthropic (Sonnet 5). Bookmark or save results — "
               "regenerating burns another $0.02.")

    # Only surface Applied jobs -- no point prepping for stuff you haven't applied to.
    applied = api_get("/jobs/", status="Applied", limit=500) or []
    if not applied:
        st.info("Apply to a job first, then it'll appear here for interview prep.")
        st.stop()

    # Newest applied at the top
    applied.sort(key=lambda j: (j.get("updated_at") or ""), reverse=True)
    labels = [f"{j.get('title','?')[:60]}  @  {j.get('company_name','?')[:30]}"
              for j in applied]
    idx = st.selectbox("Job to prep for", range(len(applied)),
                        format_func=lambda i: labels[i])
    job = applied[idx]

    st.markdown(f"**{job.get('title')}** — {job.get('company_name')} · "
                f"{job.get('location', '—')}")
    if job.get("job_url"):
        st.markdown(f"[Open posting ↗]({job['job_url']})")

    # Prefer overnight-cached prep if available (interview_prep_batch.py
    # writes to ai_interview_prep for any Applied job in the last 7 days).
    cache = api_get(f"/ai_rank/interview_prep/{job['id']}") or {}
    if cache.get("cached") and cache.get("content_md"):
        st.success(f"✨ Cached prep from {cache.get('generated_at','?')[:16]} UTC — no wait, no new tokens.")
        st.markdown(cache["content_md"])
        st.divider()
        if st.button("🔄 Regenerate with Claude (~$0.02, 15s)"):
            st.session_state["_force_regen"] = True

    force_regen = st.session_state.pop("_force_regen", False)
    if force_regen or (not cache.get("cached") and st.button("🎯 Generate interview prep with Claude")):
        # Direct call to Anthropic (dashboard has API key via env_file now)
        try:
            import anthropic  # bundled in the shared image
        except ImportError:
            st.error("anthropic SDK not installed in dashboard container. "
                     "Rebuild + restart with the updated docker-compose.")
            st.stop()

        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            st.error("ANTHROPIC_API_KEY not set in the environment. Check "
                     "backend/.env on the droplet and ensure dashboard has "
                     "env_file: ./backend/.env in docker-compose.yml.")
            st.stop()

        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
        prof = my_profile()
        resume_summary = ""
        # If a résumé file is available, embed a short excerpt for context
        m = master_resume()
        if m.get("pdf") or m.get("docx"):
            resume_summary = "(User's résumé is on file; assume standard SWE/DE background.)"

        prompt = f"""You're an interview coach helping someone prep for the below role. Generate a focused prep sheet in Markdown. Be specific and grounded in the JD's actual language — no generic "tell me about yourself" filler unless clearly relevant.

# Role
Title: {job.get('title')}
Company: {job.get('company_name')}
Location: {job.get('location', 'N/A')}

# Job Description (truncated)
{(job.get('description') or '')[:3500]}

# Candidate profile
{resume_summary or 'F-1 OPT candidate, data engineering background, 2 yrs experience.'}

# Output format (Markdown)

## 🧠 10 Behavioral Questions (STAR-ready)
For each: the question + a **1-line hook** on which experience to lead with (based on typical DE background — pipelines, migrations, incident response, cross-team work). No full answers, just prompts.

## 🔧 5 Technical Questions (from THIS JD's stack)
Grounded in what the JD actually mentions. Include one system-design question if the JD hints at scale. For each: the question + a 1-line hint on how to approach it.

## 🎯 3 Company-Specific Talking Points
What to research + what to bring up as "I've been following your work on X". Extract from the JD if possible, else infer from company name.

## ❓ 3 Smart Questions to Ask THEM at the end
Not generic — tied to something the JD mentions. Ex: 'You mentioned migrating to Iceberg — what's the timeline and what's the current bottleneck?'
"""

        with st.spinner("Claude is thinking (10-15 sec)..."):
            try:
                client = anthropic.Anthropic(api_key=api_key)
                resp = client.messages.create(
                    model=model,
                    max_tokens=2500,
                    messages=[{"role": "user", "content": prompt}],
                )
                # Sonnet 5 with extended thinking returns ThinkingBlock(s)
                # before the TextBlock. Skip anything without .text.
                out = ""
                for block in resp.content:
                    if hasattr(block, "text"):
                        try:
                            out = block.text
                            break
                        except AttributeError:
                            continue
                if not out:
                    st.error("Claude returned no text content. Try again.")
                    st.stop()
            except Exception as e:  # noqa: BLE001
                st.error(f"Anthropic API call failed: {e}")
                st.stop()

        st.markdown(out)
        st.download_button(
            "💾 Save prep sheet (.md)",
            data=out,
            file_name=f"prep_{(job.get('company_name') or 'company').replace(' ', '_')}_"
                      f"{(job.get('title') or 'role').replace(' ', '_')[:40]}.md",
            mime="text/markdown",
        )

elif page == "📈 Ops Health":
    st.header("📈 Ops Health")
    st.caption("Lightweight observability without the Prometheus/Grafana stack. Shows the "
               "vitals: job pipeline throughput, company crawl freshness, sponsor coverage, "
               "top rejection reasons. Auto-refreshes on manual reload.")

    # ---- backend reachable? ----
    hlth = api_get("/health") or {}
    if not hlth:
        st.error("❌ Backend not reachable. Check the droplet.")
        st.stop()
    st.success(f"✅ Backend OK · reported: {hlth.get('status', 'ok')}")

    # ---- pipeline throughput (last 24h) ----
    st.subheader("① Job pipeline (last 24 hours)")
    d24 = api_get("/jobs/count", discovered_within_hours=24) or {}
    d24_ex = api_get("/jobs/count", discovered_within_hours=24, exclude_rejected=True) or {}
    n_total = d24.get("count", 0)
    n_survived = d24_ex.get("count", 0)
    n_rejected = n_total - n_survived
    reject_pct = (n_rejected * 100 // n_total) if n_total else 0
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Crawled", f"{n_total:,}")
    m2.metric("Survived filters", f"{n_survived:,}")
    m3.metric("Rejected", f"{n_rejected:,}", f"{reject_pct}%")
    m4.metric("New (strong match)",
              (api_get("/jobs/count", status="New", discovered_within_hours=24) or {}).get("count", 0))

    # ---- company roster health ----
    st.divider()
    st.subheader("② Company roster")
    cstats = api_get("/jobs/stats/summary") or {}
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total companies", f"{cstats.get('total_companies', 0):,}")
    m2.metric("Active", f"{cstats.get('active_companies', 0):,}")
    m3.metric("H-1B sponsors", f"{cstats.get('sponsors', 0):,}")
    m4.metric("Total jobs stored", f"{cstats.get('total_jobs', 0):,}")

    # By priority tier (if the API returns it)
    if cstats.get("by_priority"):
        st.caption("**Scan cadence tiers** — how often each priority is re-checked:")
        pri_rows = [
            {"tier": "high",   "companies": cstats["by_priority"].get("high", 0),
             "scan every": "2 hrs  (12x/day)"},
            {"tier": "medium", "companies": cstats["by_priority"].get("medium", 0),
             "scan every": "6 hrs  (4x/day)"},
            {"tier": "low",    "companies": cstats["by_priority"].get("low", 0),
             "scan every": "24 hrs (1x/day)"},
            {"tier": "skip",   "companies": cstats["by_priority"].get("skip", 0),
             "scan every": "never (frozen)"},
        ]
        st.dataframe(pd.DataFrame(pri_rows), hide_index=True, use_container_width=True)

    # ---- top rejection reasons ----
    st.divider()
    st.subheader("③ Top rejection reasons (what the filter's killing)")
    st.caption("If some reason is much higher than expected, the filter may be misfiring. "
               "Currently the filter drops ~99% of crawled jobs (76k/day → 800 relevant).")
    rej = cstats.get("top_rejection_reasons") or []
    if rej:
        rows = [{"count": r.get("count", 0), "reason": (r.get("reason") or "")[:100]}
                for r in rej[:12]]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    else:
        st.info("No rejection breakdown available (/jobs/stats/summary didn't return that field).")

    # ---- by-source health ----
    st.divider()
    st.subheader("④ Discovery by source (ATS platform)")
    by_ats = cstats.get("by_ats") or {}
    if by_ats:
        top = sorted(by_ats.items(), key=lambda kv: -(kv[1] or 0))[:20]
        st.dataframe(pd.DataFrame(top, columns=["ATS", "companies"]),
                     hide_index=True, use_container_width=True)

    # ---- freshness check ----
    st.divider()
    st.subheader("⑤ Freshness — 'new in last N hours' pulse")
    cols = st.columns(4)
    for i, hrs in enumerate([1, 6, 24, 168]):
        c = (api_get("/jobs/count", discovered_within_hours=hrs,
                     exclude_rejected=True) or {}).get("count", 0)
        cols[i].metric(f"Last {hrs}h", f"{c:,}")

    st.caption("If 'last 1h' shows 0 for long stretches, livewatch may be stalled. Normally "
               "shows several hundred jobs/hour during business hours.")

    # ---- overnight cron status ----
    st.divider()
    st.subheader("⑥ Overnight cron jobs")
    st.caption("Each nightly script writes to a database artifact table on success. "
               "Green = ran in the last 26h, red = missed. Cron times are UTC.")
    ov = api_get("/ai_rank/overnight_status") or {}
    ov_jobs = ov.get("jobs") or {}
    if ov_jobs:
        rows = []
        for name, info in ov_jobs.items():
            fresh = info.get("rows_last_24h", 0) > 0
            rows.append({
                "job": name,
                "status": "✅" if fresh else "❌ stale",
                "last_run_utc": (info.get("last_run") or "never")[:16],
                "rows_last_26h": info.get("rows_last_24h", 0),
                "cron": info.get("cron", "?"),
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    else:
        st.info("Overnight status endpoint not returning data (backend may need restart).")

elif page == "🧠 Skills Gap":
    st.header("🧠 Skills Gap — what to learn next")
    st.caption("For every job you scored **just below the 'strong match' threshold** "
               "(marginal), this parses the JD for the tech skills that show up most "
               "and that YOU don't already have on your resume. Actionable: the skill "
               "at the top of the list, if you added it, would unlock the most new matches.")

    c1, c2, c3 = st.columns(3)
    lo = c1.slider("Score range low", 0, 90, 20, step=5, key="sg_lo",
                    help="Ignore junk-tier below this score.")
    hi = c2.slider("Score range high", 5, 100, 60, step=5, key="sg_hi",
                    help="Focus on marginal jobs -- close-to-fit ones that a skill or two "
                         "could push above the strong-match threshold (usually 50-60).")
    max_days = c3.slider("Discovered within N days", 1, 60, 30, step=1, key="sg_days")
    if hi <= lo:
        st.warning("Score range high must be > low.")
        st.stop()

    # Pull FULL (non-slim) job data so we have descriptions to scan.
    with st.spinner("Fetching jobs + parsing skills..."):
        data = filter_feed(api_get(
            "/jobs/", min_score=lo, exclude_rejected=True, order_by="score",
            limit=3000, discovered_within_hours=max_days * 24,
        ))
        data = [j for j in data if (j.get("match_score") or 0) < hi]

    st.metric("Marginal-match jobs in scope", len(data))
    if not data:
        st.info("No marginal jobs in this range. Widen the score/day sliders above.")
        st.stop()

    # ---- User's current skills (from resume profile) ----
    prof = my_profile()
    # profile shape may be dict or None; look for 'skills' key, else pull from raw
    my_skills_raw = prof.get("skills") if isinstance(prof, dict) else []
    if not my_skills_raw:
        # Fallback: use a reasonable default reflecting Ram's data-eng focus
        my_skills_raw = ["python", "sql", "aws", "azure", "spark", "pyspark",
                         "airflow", "dbt", "kafka", "snowflake", "docker",
                         "kubernetes", "terraform", "postgresql", "databricks"]
    my_skills = {s.strip().lower() for s in my_skills_raw if s and s.strip()}

    with st.expander(f"Your current skills ({len(my_skills)})", expanded=False):
        st.write(", ".join(sorted(my_skills)))

    # ---- The skill vocabulary to scan for ----
    # Curated data/cloud/ML/infra terms. Add here to widen coverage.
    KNOWN_SKILLS = [
        # Data engineering / warehousing / lake
        "snowflake", "databricks", "bigquery", "redshift", "synapse", "clickhouse",
        "iceberg", "delta lake", "hudi", "duckdb",
        # Orchestration
        "airflow", "dagster", "prefect", "luigi", "step functions",
        # Stream / message
        "kafka", "kinesis", "pulsar", "flink", "spark streaming", "beam",
        # ETL / transformation
        "dbt", "fivetran", "airbyte", "singer", "stitch", "matillion",
        # Compute
        "spark", "pyspark", "hadoop", "hive", "presto", "trino", "athena",
        # Cloud
        "aws", "azure", "gcp", "google cloud", "s3", "emr", "glue", "lambda",
        "sagemaker", "vertex ai", "adf", "data factory", "dataproc",
        # Container / infra
        "docker", "kubernetes", "helm", "terraform", "pulumi", "ansible",
        "jenkins", "circleci", "github actions", "gitlab ci",
        # Data stores
        "postgresql", "mysql", "sql server", "oracle", "mongodb", "cassandra",
        "redis", "elasticsearch", "dynamodb", "cockroachdb",
        # Languages
        "python", "java", "scala", "go", "rust", "typescript", "javascript",
        "r ", "julia",  # trailing space on "r" to avoid matching "sr"
        # ML / AI
        "pytorch", "tensorflow", "sklearn", "scikit-learn", "xgboost", "lightgbm",
        "huggingface", "langchain", "openai", "anthropic", "mlflow", "kubeflow",
        "vector database", "pinecone", "weaviate", "chroma", "faiss",
        # BI / vis
        "tableau", "power bi", "looker", "metabase", "superset", "grafana",
        # Observability / ops
        "prometheus", "datadog", "splunk", "new relic", "sentry",
        # API / backend
        "fastapi", "flask", "django", "express", "node.js", "graphql", "grpc",
        # Misc
        "kafka connect", "great expectations", "monte carlo", "collibra",
        "informatica", "talend", "ssis", "sas", "cognos",
    ]

    # Compile ONE regex for all skills (longest-first so 'spark streaming' wins
    # over 'spark'). Word-boundary style so 'r' doesn't match everywhere.
    alts = "|".join(re.escape(s) for s in sorted(KNOWN_SKILLS, key=len, reverse=True))
    skill_re = re.compile(r"(?<![a-z0-9])(" + alts + r")(?![a-z0-9])", re.I)

    # ---- Scan every marginal job's description ----
    from collections import Counter
    counts: Counter = Counter()
    per_skill_jobs: dict[str, list] = {}
    for j in data:
        desc = (j.get("description") or "").lower()
        if not desc: continue
        found = set(m.group(1).lower().strip() for m in skill_re.finditer(desc))
        # Skills you DON'T have
        gaps = found - my_skills
        for g in gaps:
            counts[g] += 1
            per_skill_jobs.setdefault(g, []).append(j)

    if not counts:
        st.success("No skill gaps detected in this range — every marginal job's required "
                   "skills are already in your profile. The blocker isn't skills, it's "
                   "score. Try widening the score-range window.")
        st.stop()

    # ---- The top-10 gaps ----
    st.subheader(f"🎯 Top skills you're missing (from {len(data)} marginal jobs)")
    st.caption("Ranked by how many marginal-match jobs mention the skill AND you don't "
               "have it. Adding the top one would unlock the most postings.")
    top = counts.most_common(15)
    top_df = pd.DataFrame(top, columns=["skill", "jobs mentioning it"])
    top_df["% of scope"] = (top_df["jobs mentioning it"] * 100 // len(data)).astype(str) + "%"
    st.dataframe(top_df, hide_index=True, use_container_width=True)

    # ---- Drill-in: pick a skill, see the jobs that need it ----
    st.subheader("🔍 Drill in: pick a skill to see which jobs would open up")
    pick = st.selectbox("Skill", [s for s, _ in top], key="sg_pick")
    if pick and pick in per_skill_jobs:
        rows = per_skill_jobs[pick][:30]
        st.caption(f"{len(per_skill_jobs[pick])} marginal jobs mention **{pick}**. "
                   f"Showing top {len(rows)} by score:")
        rows.sort(key=lambda j: -(j.get("match_score") or 0))
        df = pd.DataFrame([{
            "score": j.get("match_score"),
            "sponsor": "✅" if j.get("sponsor_confirmed") else "",
            "title": j.get("title"),
            "company": j.get("company_name"),
            "location": j.get("location"),
            "open": apply_url(j),
        } for j in rows])
        st.dataframe(df, hide_index=True, use_container_width=True,
                     column_config={
                         "open": st.column_config.LinkColumn("open", display_text="open ↗"),
                         "score": st.column_config.NumberColumn("score", format="%d"),
                     })

elif page == "❄️ Frozen Companies":
    st.header("❄️ Frozen Companies")
    st.caption("Mark companies as **frozen** when you see layoffs / hiring freeze news. "
               "Frozen companies get `priority=skip` (the crawler stops pulling from them) "
               "AND their existing stored jobs are hidden from every discovery view (Posted "
               "Today, Best Matches, Fast Apply, etc.). Keeps you from wasting applications "
               "on companies that just froze. Unfreeze anytime to bring them back.")

    all_cos = api_get("/companies/") or []
    frozen = sorted([c for c in all_cos if (c.get("priority") or "").lower() == "skip"],
                    key=lambda c: (c.get("name") or "").lower())
    non_frozen = [c for c in all_cos if (c.get("priority") or "").lower() != "skip"]

    m1, m2, m3 = st.columns(3)
    m1.metric("❄️ Frozen", len(frozen))
    m2.metric("Active roster", len(non_frozen))
    m3.metric("Total", len(all_cos))

    st.subheader("① Freeze a company")
    tab_search, tab_bulk = st.tabs(["🔎 Search & mark", "📋 Bulk paste"])

    with tab_search:
        q = st.text_input("Search company name",
                          placeholder="e.g. meta, coinbase, stripe...",
                          key="freeze_search")
        if q and len(q) >= 2:
            ql = q.lower()
            hits = [c for c in non_frozen if ql in (c.get("name") or "").lower()][:20]
            if not hits:
                st.info(f"No non-frozen company matches '{q}'.")
            else:
                st.caption(f"Top {len(hits)} matches — click ❄️ to freeze:")
                for c in hits:
                    col1, col2, col3, col4 = st.columns([3, 2, 2, 2])
                    col1.write(f"**{c.get('name')}**")
                    col2.write(f"{c.get('ats_type', '') or '—'}")
                    col3.write(f"priority: {c.get('priority', '')}")
                    if col4.button("❄️ Freeze", key=f"fr_{c['id']}"):
                        stamp = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
                        api_patch(f"/companies/{c['id']}", {
                            "priority": "skip",
                            "notes": ((c.get("notes") or "")
                                      + f" | ❄️ frozen {stamp} (layoffs/freeze)").strip(" |"),
                        })
                        st.cache_data.clear()
                        st.rerun()

    with tab_bulk:
        st.caption("Paste multiple company names (one per line OR comma-separated) — "
                   "we'll fuzzy-match each and freeze all matches.")
        blob = st.text_area("Company names", key="freeze_bulk",
                            placeholder="Meta\nCoinbase\nBooking.com, DocuSign, Snap")
        if st.button("❄️ Freeze all matches") and blob.strip():
            names = [n.strip() for line in blob.splitlines() for n in line.split(",")]
            names = [n for n in names if n]
            frozen_here, missing = [], []
            stamp = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
            for name in names:
                nl = name.lower()
                m = next((c for c in non_frozen if nl in (c.get("name") or "").lower()), None)
                if not m:
                    missing.append(name); continue
                api_patch(f"/companies/{m['id']}", {
                    "priority": "skip",
                    "notes": ((m.get("notes") or "")
                              + f" | ❄️ frozen {stamp} (bulk)").strip(" |"),
                })
                frozen_here.append(m.get("name"))
            st.cache_data.clear()
            st.success(f"Froze {len(frozen_here)} companies: {', '.join(frozen_here[:8])}"
                       + ("…" if len(frozen_here) > 8 else ""))
            if missing:
                st.warning(f"No match for: {', '.join(missing[:10])}"
                           + ("…" if len(missing) > 10 else ""))
            st.rerun()

    st.divider()
    st.subheader(f"② Currently frozen ({len(frozen)})")
    if not frozen:
        st.info("Nothing frozen yet. When you see layoffs news, freeze the company here "
                "and its jobs stop appearing in your feeds.")
    else:
        rows = [{
            "id": c["id"],
            "unfreeze": False,
            "name": c.get("name"),
            "ats": c.get("ats_type") or "—",
            "notes": (c.get("notes") or "")[:80],
        } for c in frozen]
        df = pd.DataFrame(rows).set_index("id")
        edited = st.data_editor(
            df, key=f"frozen_ed_{len(frozen)}", hide_index=True, use_container_width=True,
            disabled=["name", "ats", "notes"],
            column_config={
                "unfreeze": st.column_config.CheckboxColumn(
                    "🔥 Unfreeze",
                    help="Tick to remove from frozen list — company returns to normal crawl rotation."),
            },
        )
        for jid, r in edited.iterrows():
            if bool(r["unfreeze"]):
                api_patch(f"/companies/{int(jid)}", {"priority": "low"})
                st.cache_data.clear()
                st.rerun()
