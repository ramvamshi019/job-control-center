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

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

# Read API_BASE_URL from backend/.env if present, else default.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", ".env"))
API = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")

st.set_page_config(page_title="Job Control Center", page_icon="🎯", layout="wide")

JOB_STATUSES = ["New", "Need Review", "Approved", "Applied", "Follow-up", "Rejected", "Archived"]

# ---------- small API helpers ----------
def api_get(path: str, **params):
    try:
        r = requests.get(f"{API}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        st.error(f"API GET {path} failed: {exc}")
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


def render_apply_kit(job: dict):
    """Apply panel: open the job, download YOUR real résumé to upload, copy your
    details into the form, and mark it Applied."""
    with st.expander("🚀 Application kit", expanded=True):
        if job.get("job_url"):
            st.link_button("🚀 Apply — open this job in a new tab", job["job_url"])

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
    ["📄 Match My Résumé", "🔎 Find Jobs", "🔥 Fresh (apply now)", "🟢 Live Feed",
     "Today's Best Jobs", "Need Review", "Approved", "Applied", "Rejected", "Companies", "Stats"],
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
                st.markdown(f"[🔗 Open job posting]({job['job_url']})")
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

        cols = st.columns(len(actions) + 1)
        for i, action in enumerate(actions):
            if cols[i].button(action, key=f"{action}_{job['id']}"):
                mapping = {"Approve": "Approved", "Reject": "Rejected", "Review": "Need Review",
                           "Mark Applied": "Applied", "Follow-up": "Follow-up", "Archive": "Archived"}
                set_status(job["id"], mapping.get(action, "New"))
                st.rerun()
        if cols[-1].button("🚀 Apply", key=f"apply_{job['id']}"):
            st.session_state[f"show_apply_{job['id']}"] = True

        if st.session_state.get(f"show_apply_{job['id']}"):
            render_apply_kit(job)


# ---------- pages ----------
if page == "📄 Match My Résumé":
    st.header("📄 Match My Résumé")
    st.caption("Upload your résumé → get jobs ranked by how well they fit YOUR skills, "
               "filtered to your experience level and work-authorization needs.")

    up = st.file_uploader("Upload résumé (PDF, DOCX, or TXT)", type=["pdf", "docx", "txt", "md"])

    c1, c2, c3 = st.columns([2, 2, 2])
    levels = c1.multiselect("Experience level", ["entry", "mid", "senior"], default=["entry", "mid"],
                            help="Filters jobs by the experience THEY require.")
    POSTED_WINDOWS = {"Any time": 0, "Last 24 hours": 24, "Last 3 days": 72,
                      "Last 7 days": 168, "Last 30 days": 720}
    posted_window = c2.selectbox("Posted within", list(POSTED_WINDOWS.keys()), index=0,
                                 help="Only jobs posted in this window (uses the real posting date).")
    min_skills = c3.slider("Min matching skills", 1, 10, 3)

    c4, c5 = st.columns([2, 4])
    usa_only = c4.checkbox("🇺🇸 USA only", value=True,
                           help="Require a US (or remote-US) location.")
    sponsor_only = c5.checkbox("Sponsor-friendly only", value=True,
                               help="Hide jobs that require US citizenship / security clearance or explicitly don't sponsor.")

    if up is not None and st.button("🔍 Find my best matches", type="primary"):
        with st.spinner("Reading your résumé and ranking jobs…"):
            try:
                resp = requests.post(
                    f"{API}/resume/match",
                    files={"file": (up.name, up.getvalue())},
                    data={"experience_levels": ",".join(levels) or "entry,mid",
                          "sponsor_only": str(sponsor_only).lower(),
                          "usa_only": str(usa_only).lower(),
                          "posted_within_hours": POSTED_WINDOWS[posted_window],
                          "min_skills": min_skills, "limit": 100},
                    timeout=120,
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Match failed: {exc}")
                payload = None
        if payload:
            st.session_state["resume_match"] = payload

    payload = st.session_state.get("resume_match")
    if payload:
        p = payload["profile"]
        lvl_emoji = {"entry": "🟢 entry", "mid": "🟡 mid", "senior": "🟠 senior"}.get(p["level"], p["level"])
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Skills detected", len(p["skills"]))
        m2.metric("Experience", lvl_emoji)
        m3.metric("Needs sponsorship", "Yes" if p["needs_sponsorship"] else "No")
        m4.metric("Matching jobs", payload["count"])
        with st.expander(f"Skills read from your résumé ({len(p['skills'])})"):
            st.write(", ".join(p["skills"]) or "—")
        if payload.get("note"):
            st.warning(payload["note"])

        st.subheader(f"Best {min(len(payload['jobs']), 100)} jobs for your résumé")
        for j in payload["jobs"]:
            with st.container(border=True):
                cA, cB = st.columns([3, 1])
                with cA:
                    st.markdown(f"### {j['title']}")
                    st.markdown(f"**{j['company_name']}** · {j.get('location') or '—'} · "
                                f"_{j['experience_level']}-level_")
                    st.caption(f"Posted: {(j.get('posted_at') or 'unknown')[:10]} · Source: {j['source']}")
                    if j.get("job_url"):
                        st.markdown(f"[🔗 Open job posting]({j['job_url']})")
                    clabel, cemoji, _ = competition(j.get("source"))
                    st.write(f"**Matched skills ({j['fit_count']}):** {', '.join(j['matched_skills'])}")
                with cB:
                    st.metric("Résumé fit", f"{j['fit_pct']}%")
                    risk = j.get("sponsorship_risk", "?")
                    rc = {"low": "🟢", "medium": "🟡", "high": "🟠", "reject": "🔴"}.get(risk, "⚪")
                    st.markdown(f"**Sponsorship:** {rc} {risk}")
                    st.markdown(f"**Competition:** {cemoji} {clabel}")
                bc = st.columns(3)
                if bc[0].button("Approve", key=f"rm_app_{j['id']}"):
                    set_status(j["id"], "Approved"); st.rerun()
                if bc[1].button("Review", key=f"rm_rev_{j['id']}"):
                    set_status(j["id"], "Need Review"); st.rerun()
                if bc[2].button("Reject", key=f"rm_rej_{j['id']}"):
                    set_status(j["id"], "Rejected"); st.rerun()
    elif up is None:
        st.info("⬆️ Upload your résumé to see your best-fit jobs. Tip: a PDF export works best.")

elif page == "🔎 Find Jobs":
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

elif page == "🟢 Live Feed":
    st.header("🟢 Live Feed")
    st.caption("Newest jobs the crawler has detected. Auto-refreshes so new US jobs appear live.")

    colA, colB = st.columns([2, 2])
    feed_window = colA.selectbox("Show jobs discovered within", ["Last 24 hours", "Last 3 days", "Last 7 days", "All"])
    auto = colB.checkbox("🔄 Auto-refresh (every 30s)", value=True)
    fhours = {"Last 24 hours": 24, "Last 3 days": 72, "Last 7 days": 168, "All": None}[feed_window]

    @st.fragment(run_every=(30 if auto else None))
    def live_feed():
        params = dict(order_by="discovered", exclude_rejected=True, limit=200)
        if fhours:
            params["discovered_within_hours"] = fhours
        data = api_get("/jobs/", **params) or []
        st.metric("Jobs detected in window", len(data))
        if not data:
            st.info("No new jobs in this window yet. The live crawler adds them as it finds them.")
            return
        rows = [{
            "discovered": (j.get("discovered_at") or "")[:19],
            "score": j.get("match_score"),
            "title": j.get("title"),
            "company": j.get("company_name"),
            "location": j.get("location"),
            "risk": j.get("sponsorship_risk"),
            "url": j.get("job_url"),
        } for j in data]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                     column_config={"url": st.column_config.LinkColumn("url")})

    live_feed()

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
        show = ["title", "company_name", "location", "match_score", "sponsorship_risk", "job_url"]
        st.dataframe(df[[c for c in show if c in df.columns]], use_container_width=True,
                     hide_index=True, column_config={"job_url": st.column_config.LinkColumn("job_url")})
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
