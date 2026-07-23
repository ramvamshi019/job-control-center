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
from datetime import datetime, timezone
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


def jobs_df(status=None, min_score=0, sponsorship_risk=None):
    data = api_get("/jobs/", status=status, min_score=min_score, sponsorship_risk=sponsorship_risk) or []
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
                    were already crawling. A new posting on an established board
                    is almost certainly newly-posted, so we surface it -- clearly
                    marked as inferred, never conflated with a confirmed date.
      None        - neither; don't show on Posted Today.

    Half of all sources (iCIMS, SmartRecruiters, Workday, BambooHR) never expose
    a usable posting date; without "likely" they'd be invisible here even when
    genuinely brand-new."""
    if posted_today(job):
        return "confirmed"
    if not job.get("board_known"):
        return None  # can't trust "new = fresh" on a board we just started on
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
    dashboard filter agrees with how jobs were scored."""
    text = f"{row.get('title') or ''} {row.get('description') or ''}".lower()
    nums = [int(n) for n in re.findall(r"(\d{1,2})\+?\s*years?", text)]
    return min(nums) if nums else None


def set_status(job_id: int, status: str, reason: str = ""):
    payload = {"status": status}
    if reason:
        payload["rejection_reason"] = reason
    api_patch(f"/jobs/{job_id}", payload)


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
    ["⚡ Fast Apply", "🔎 Find Jobs", "🔥 Fresh (apply now)",
     "🕵️ JobRight Gap", "🔴 Posted Today", "🟢 Live Feed", "Today's Best Jobs",
     "Need Review", "Approved",
     "Applied", "Rejected", "Companies", "Stats"],
)

# Quick live counter in the sidebar (jobs first seen in the last 24h).
_recent = api_get("/jobs/", discovered_within_hours=24, exclude_rejected=True, limit=1000) or []
st.sidebar.metric("🆕 New in last 24h", len(_recent))

if st.sidebar.button("📤 Export Approved → CSV"):
    res = api_post("/export/")
    if res:
        st.sidebar.success(f"Exported {res['count']} jobs to {res['path']}")


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
                           "Mark Applied": "Applied", "Follow-up": "Follow-up", "Archive": "Archived"}
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

    c4, c5, c6 = st.columns([2, 3, 2])
    min_score = c4.slider("Min score", 0, 100, 0, step=5)
    risks = c5.multiselect("Sponsorship risk", ["low", "medium", "high", "unknown"], default=["low", "medium"])
    hide_rejected = c6.checkbox("Hide rejected", value=True)

    hours = {"Any time": None, "Last 24 hours": 24, "Last 3 days": 72,
             "Last 7 days": 168, "Last 30 days": 720}[window]
    order = {"Best match": "score", "Newest posted": "posted", "Recently discovered": "discovered"}[sort]

    params = dict(min_score=min_score, exclude_rejected=hide_rejected, order_by=order, limit=300)
    if query:
        params["q"] = query
    if hours:
        params["posted_within_hours"] = hours
    data = api_get("/jobs/", **params) or []
    # Client-side risk filter (API takes one risk; we allow several).
    if risks:
        data = [j for j in data if j.get("sponsorship_risk") in risks]

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
    data = api_get("/jobs/", **params) or []
    if low_comp:
        data = [j for j in data if competition(j.get("source"))[2] == 1]
    st.success(f"{len(data)} jobs posted in the {win.lower()}")
    if not data:
        st.info("Nothing posted in this window yet — widen the window or check back soon.")
    for job in data[:150]:
        render_job_card(job, actions=("Approve", "Review", "Reject"))

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
    data = api_get("/jobs/", **params) or []
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
            column_order=["applied", "edge", "sponsor", "score", "title", "company",
                          "source", "location", "why", "open"],
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

elif page == "🔴 Posted Today":
    st.header("🔴 Posted Today")
    st.caption("Jobs that are fresh **today** (your local day). "
               "🔴 **CONFIRMED** = the source stated a posting date of today. "
               "🟡 **LIKELY** = the source hides the date, but this posting first appeared "
               "today on a board we already crawl — almost always newly-posted. "
               "Citizenship/clearance roles are excluded automatically. Auto-refreshes every 20s.")

    STATUS_BADGE = {
        "Applied": "✅ APPLIED", "Follow-up": "📌 Follow-up", "Approved": "👍 Approved",
        "Need Review": "🔍 Review", "New": "🆕 New",
    }
    c1, c2 = st.columns(2)
    hide_applied = c1.checkbox("Hide jobs I've already applied to", value=True)
    confirmed_only = c2.checkbox("Only date-confirmed (hide 🟡 LIKELY)", value=False,
                                 help="Tick for the strict old behaviour: only jobs whose "
                                      "source printed today's date.")

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
        raw = api_get("/jobs/", order_by="discovered", discovered_within_hours=30,
                      exclude_rejected=True, limit=1000) or []
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
        # Confirmed first, then strongest match; keeps the trustworthy rows on top.
        data.sort(key=lambda j: (j["_freshness"] != "confirmed",
                                 -(j.get("match_score") or 0)))

        n_conf = sum(1 for j in data if j["_freshness"] == "confirmed")
        n_likely = len(data) - n_conf
        m1, m2, m3 = st.columns(3)
        m1.metric("🔴 Fresh today", len(data), help=f"{n_conf} confirmed · {n_likely} likely")
        m2.metric("✅ H-1B sponsors", sum(1 for j in data if j.get("sponsor_confirmed")))
        m3.metric("🆕 New (strong match)", sum(1 for j in data if j.get("status") == "New"))
        if not data:
            st.info("Nothing fresh today yet in the sponsor-safe set. This auto-updates — "
                    "new postings will pop in as the crawler finds them.")
            return

        FRESH_BADGE = {"confirmed": "🔴 CONFIRMED", "likely": "🟡 LIKELY"}
        rows = [{
            "id": j.get("id"),
            "applied": j.get("status") == "Applied",
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
        } for j in data]
        n_sponsor = sum(1 for r in rows if r["sponsor"])
        st.caption(f"👉 Tick **✅ Applied?** to mark a job applied · 🔴 CONFIRMED date vs "
                   f"🟡 LIKELY (first seen today) · {n_conf} confirmed / {n_likely} likely · "
                   f"{n_sponsor} sponsor-confirmed in view")

        df = pd.DataFrame(rows).set_index("id")
        editor_key = "today_ed_" + str(abs(hash(tuple(r["id"] for r in rows))))
        edited = st.data_editor(
            df, key=editor_key, hide_index=True, use_container_width=True,
            disabled=["fresh", "status", "sponsor", "posted", "score", "title", "company",
                      "location", "risk", "open"],
            column_order=["applied", "fresh", "status", "sponsor", "posted", "score", "title",
                          "company", "location", "risk", "open"],
            column_config={
                "applied": st.column_config.CheckboxColumn(
                    "✅ Applied?", help="Tick when you've applied — it's kept (never pruned)."),
                "fresh": st.column_config.TextColumn(
                    "fresh", help="🔴 CONFIRMED = source stated today's date · "
                                  "🟡 LIKELY = source hides the date but it first appeared "
                                  "today on a board we already crawl."),
                "posted": st.column_config.TextColumn(
                    "posted", help="Real posting date (confirmed rows) or ~first-seen date "
                                   "(likely rows)."),
                "open": st.column_config.LinkColumn("open", display_text="open ↗"),
                "score": st.column_config.NumberColumn("score", format="%d"),
            },
        )
        status_by_id = {j.get("id"): j.get("status") for j in data}
        changed = False
        for jid, r in edited.iterrows():
            want = bool(r["applied"])
            cur = status_by_id.get(jid)
            if want and cur != "Applied":
                set_status(int(jid), "Applied"); changed = True
            elif not want and cur == "Applied":
                set_status(int(jid), "New"); changed = True
        if changed:
            st.rerun()

    posted_today_feed()

elif page == "🟢 Live Feed":
    st.header("🟢 Live Feed")
    st.caption("Newest jobs the crawler has detected, **ranked best-match first** and grouped "
               "into experience sections. Shows the full firehose, including Workday/iCIMS roles "
               "that hide their posting date (those now also surface on Posted Today as 🟡 LIKELY "
               "when freshly seen).")

    colA, colB, colC = st.columns([2, 2, 2])
    feed_window = colA.selectbox("Show jobs discovered within", ["Last 24 hours", "Last 3 days", "Last 7 days", "All"])
    show_filter = colB.selectbox("Show", ["Everything", "Not applied yet", "Applied only"])
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

    # Experience sections, mirroring the buckets on "Today's Best Jobs". Years are
    # parsed from the title/description; most entry-level posts state no number at
    # all, so "not stated" is its own section rather than being silently dropped.
    EXP_SECTIONS = [
        ("🎓 No experience stated", lambda y: y is None),
        ("① 0–2 years", lambda y: y is not None and y <= 2),
        ("② 3–5 years", lambda y: y is not None and 3 <= y <= 5),
        ("③ 5+ years", lambda y: y is not None and y > 5),
    ]

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
        params = dict(order_by="discovered", exclude_rejected=True, limit=400)
        if fhours:
            params["discovered_within_hours"] = fhours
        data = api_get("/jobs/", **params) or []
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
                "open": apply_url(j),  # Himalayas rows → employer-careers search
            } for j in subset]

            df = pd.DataFrame(rows).set_index("id")
            # Re-key on the visible id-set: when the live crawler reshuffles rows
            # the editor resets instead of applying a stale tick to a moved row.
            editor_key = f"live_ed_{key_prefix}_" + str(abs(hash(tuple(r["id"] for r in rows))))
            edited = st.data_editor(
                df, key=editor_key, hide_index=True, use_container_width=True,
                disabled=["🔴", "status", "sponsor", "posted", "score",
                          "title", "company", "location", "risk", "open"],
                column_order=["applied", "🔴", "status", "sponsor", "posted",
                              "score", "title", "company", "location", "risk", "open"],
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

        if any("himalayas.app" in (j.get("job_url") or "") for j in data):
            st.caption("ℹ️ **Himalayas** rows open to the employer's own careers page "
                       "(the Himalayas page is Cloudflare-walled and won't load in Chrome).")

    live_feed()

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

    data = api_get("/jobs/", exclude_rejected=True, order_by="score", limit=1000) or []
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
        rows = [{
            "id": j.get("id"),
            "applied": j.get("status") == "Applied",
            "sponsor": "✅ H-1B" if j.get("sponsor_confirmed") else "",
            "score": j.get("match_score"),
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
            disabled=["sponsor", "score", "title", "company", "location", "ats", "open"],
            column_order=["applied", "sponsor", "score", "title", "company", "location",
                          "ats", "open"],
            column_config={
                "applied": st.column_config.CheckboxColumn(
                    "✅ Applied?", help="Tick once you've actually submitted."),
                "open": st.column_config.LinkColumn("open", display_text="open ↗"),
                "score": st.column_config.NumberColumn("score", format="%d"),
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
    df = jobs_df(status="New", min_score=0)

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
    df = jobs_df(status="Need Review")
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
    st.caption("Jobs you've applied to. These are never auto-pruned, so the record stays.")
    df = jobs_df(status="Applied")
    if df.empty:
        st.info("Nothing applied yet.")
    else:
        df = df.copy()
        df["apply"] = df.apply(lambda r: apply_url(r.to_dict()), axis=1)
        show = ["title", "company_name", "location", "match_score", "sponsorship_risk", "apply"]
        st.dataframe(df[[c for c in show if c in df.columns]], use_container_width=True,
                     hide_index=True, column_config={"apply": st.column_config.LinkColumn("apply", display_text="open ↗")})
        for _, row in df.iterrows():
            render_job_card(row.to_dict(), actions=("Follow-up", "Archive"))

elif page == "Rejected":
    st.header("Rejected")
    df = jobs_df(status="Rejected")
    st.caption(f"{len(df)} jobs")
    if not df.empty:
        show = ["title", "company_name", "rejection_reason", "match_score", "job_url"]
        st.dataframe(df[[c for c in show if c in df.columns]], use_container_width=True)

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
