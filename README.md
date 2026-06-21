# 🎯 Job Control Center

A **targeted job-search operating system** for an F-1/OPT candidate looking for
entry-level **Data Engineer / Cloud Engineer / Software Engineer** roles in the U.S.

It crawls company career pages (Greenhouse, Lever, Ashby in the MVP), removes
duplicates, applies hard filters, scores each job 0–100, estimates visa-sponsorship
risk, drafts honest resume notes + cover letters, and lets **you approve before
applying**. Nothing is auto-submitted.

> **This is not a mass auto-apply bot.** It's a system to help you make a small
> number of *high-quality* applications.

---

## What it does (and does NOT do)

**Does:** discover jobs from ATS APIs → store in SQLite → dedupe → hard-filter →
score → sponsorship risk → resume notes + cover letter → dashboard → you approve →
CSV export for manual apply.

**Does NOT (by design):** no LinkedIn/Indeed/Workday auto-submit, no CAPTCHA
bypass, no login automation, no fake applications, no 1000/day bot, no payments,
no Chrome extension. See *Future roadmap* for what comes later.

---

## Quick start (macOS)

You need **Python 3.11+**. Check with `python3 --version`.

```bash
# 0) Go to the project
cd ~/Desktop/job-control-center

# 1) Create + activate a virtual environment (one venv for the whole project)
python3 -m venv .venv
source .venv/bin/activate

# 2) Install backend + dashboard requirements
pip install --upgrade pip
pip install -r backend/requirements.txt
pip install -r dashboard/requirements.txt

# 3) Create your .env from the template (optional — works without it)
cp backend/.env.example backend/.env
#   then edit backend/.env to set MY_SKILLS and (optionally) an AI key

# 4) Seed companies + create the database  (run from backend/)
cd backend
python scripts/seed_companies.py

# 5) Run the crawler (first full crawl)
python scripts/crawl_all.py

# 6) Start the backend API (keep this terminal open)
uvicorn app.main:app --reload
#   API docs: http://127.0.0.1:8000/docs
```

Open a **second terminal** for the dashboard:

```bash
cd ~/Desktop/job-control-center
source .venv/bin/activate
streamlit run dashboard/app.py
#   Dashboard: http://localhost:8501
```

That's it. Crawl in terminal 1's script, browse/approve in the dashboard, export to
`exports/daily_jobs.csv`.

---

## Daily workflow

1. `cd backend && python scripts/crawl_priority.py` — crawl only companies that are *due*.
2. Open the dashboard → **Today's Best Jobs** (score ≥ 75).
3. Review fit + sponsorship risk, read the resume notes & cover letter draft.
4. **Approve** good ones, **Reject** bad ones, send unclear ones to **Need Review**.
5. Sidebar → **Export Approved → CSV**, or `python scripts/daily_export.py`.
6. Apply manually (or hand the CSV to an assistant). Mark **Applied**, set follow-up.

---

## How the pieces fit together

```
companies (DB)  ──>  registry picks crawler  ──>  crawler.crawl(company)
                                                        │  standardized Job
                                                        ▼
        dedupe ──> hard filters ──> scoring ──> sponsorship ──> resume/cover ──> SQLite
                                                                                    │
                              FastAPI (/jobs /companies /applications /export) <────┘
                                                        │
                                                  Streamlit dashboard
```

- **Crawlers** are pluggable: every source implements the same interface
  (`source_name`, `can_handle`, `fetch_jobs`, `normalize_job`) and returns the
  same standardized `Job`. Add a new source = add one file + one registry line.
- **Engines** (`filter_engine`, `scoring_engine`, `sponsorship_engine`) are pure
  functions you can test in isolation.
- **Scheduler** ties the pipeline together and handles priority-based timing.

---

## Project layout

```
job-control-center/
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI app
│   │   ├── config.py          # settings from .env
│   │   ├── database.py        # SQLite + SQLModel
│   │   ├── models/            # company, job, application
│   │   ├── crawlers/          # base, registry, greenhouse, lever, ashby, placeholders
│   │   ├── services/          # filter, scoring, sponsorship, tailor, cover, dedupe, export, scheduler
│   │   ├── routes/            # jobs, companies, applications, export
│   │   └── utils/             # text, dates, logging
│   ├── scripts/               # seed_companies, crawl_all, crawl_priority, daily_export
│   ├── data/                  # companies_seed.csv, jobs.db (created on first run)
│   ├── requirements.txt
│   └── .env.example
├── dashboard/                 # Streamlit app + requirements
├── resumes/                   # base_data_engineer.md, base_cloud_engineer.md, base_software_engineer.md
├── exports/                   # daily_jobs.csv
└── README.md
```

---

## Testing plan

| What | How |
|------|-----|
| DB creation | `python scripts/seed_companies.py` then check `backend/data/jobs.db` exists |
| Seed companies | `curl http://127.0.0.1:8000/companies/` (after backend up) shows ~22 rows |
| Crawler | `python scripts/crawl_all.py` — log shows `greenhouse stripe -> N jobs` |
| Filters | Crawl Palantir — many roles land in **Rejected** with a clearance reason |
| Scoring | `GET /jobs/?min_score=75` returns only strong matches |
| Sponsorship | `GET /jobs/?sponsorship_risk=reject` returns blocked jobs |
| Dashboard | `streamlit run dashboard/app.py`, sidebar shows "Backend connected" |
| Export | Sidebar **Export Approved** → check `exports/daily_jobs.csv` |
| Status updates | Click Approve/Reject; re-query the API to confirm the new status |

Quick engine sanity check without a server:

```bash
cd backend
python -c "from app.services.filter_engine import evaluate; from app.models.job import Job; print(evaluate(Job(title='Senior Data Engineer', company_name='X')))"
# -> FilterResult(passed=False, reason=\"Title contains blocked term: 'senior'\")
```

---

## Common errors & fixes

- **`ModuleNotFoundError: No module named 'app'`** — run from the `backend/` folder, or use the scripts (they fix `sys.path`). For uvicorn: `cd backend` first.
- **Wrong working directory** — crawl/seed scripts expect to run from `backend/`. The dashboard runs from the project root.
- **`Address already in use` (port 8000/8501)** — `lsof -ti:8000 | xargs kill` (or `:8501`), or run uvicorn with `--port 8001` and set `API_BASE_URL` to match.
- **SQLite path issue** — `DATABASE_URL=sqlite:///./data/jobs.db` is relative to `backend/`. Run scripts from there; the folder is auto-created.
- **`requests` blocked / 403** — some boards rate-limit. Increase `CRAWL_DELAY_SECONDS` in `.env`. A single failing company logs a warning and is skipped.
- **Empty crawler results** — the ATS token may be wrong/changed. Verify: `curl https://boards-api.greenhouse.io/v1/boards/<token>/jobs`. Fix `career_url` in the DB / seed CSV.
- **Streamlit not running** — `pip install -r dashboard/requirements.txt`, then `streamlit run dashboard/app.py` from the project root.
- **FastAPI not starting** — check the traceback; usually a missing dep or you're not in `backend/`. Re-run `pip install -r backend/requirements.txt`.
- **CORS issue** — the API already allows all origins for local use (`main.py`). If you changed it, re-add the dashboard origin.
- **API key missing** — totally fine. With `AI_PROVIDER=none` the system uses built-in rule-based notes/letters.

---

## 7-day build plan

- **Day 1** — setup, venv, install, `seed_companies.py`, confirm DB + companies.
- **Day 2** — Greenhouse + Lever crawlers; `crawl_all.py`; eyeball stored jobs.
- **Day 3** — Ashby crawler + dedupe + hard filters; confirm Rejected reasons.
- **Day 4** — scoring + sponsorship engines; tune weights for your profile.
- **Day 5** — dashboard pages working end to end.
- **Day 6** — resume notes + cover letters + CSV export.
- **Day 7** — real usage: crawl, review, approve, export, apply to 3–5 roles.

---

## Future pro roadmap

1. **Phase 2 crawlers** — Workday, SmartRecruiters, iCIMS, Jobvite, Workable, BambooHR, Zoho.
2. **Phase 3** — Taleo, SuccessFactors, ADP, UKG, Eightfold, JazzHR, Recruitee, Paylocity, Dayforce, Avature, Gem, Yello.
3. **Phase 4 (discovery only)** — LinkedIn/Indeed/Google Jobs/Glassdoor/ZipRecruiter/etc. to *find* company career links, never to auto-submit.
4. **Phase 5 APIs** — Adzuna, USAJOBS, JSearch, Arbeitnow, Remotive, SerpAPI, Unified.to.
5. **Enrichment** — real H-1B disclosure data to set `h1b_history_score` precisely.
6. **Outputs** — Google Sheets sync, email alerts, resume PDF/DOCX export, human-assistant export package.
7. **Assisted apply** — Mode C controlled autofill that **stops before final submit** (never auto-submits).
8. **Ops** — recruiter follow-up tracker, priority scheduler on launchd/cron, Postgres migration.

---

## A note on ethics & site rules

This tool only calls **public ATS JSON endpoints** that are meant to power public
job boards, with a polite delay between requests. It does **not** scrape behind
logins, bypass CAPTCHAs, or auto-submit applications. Keep it that way: the value
is quality applications you stand behind, not volume.
