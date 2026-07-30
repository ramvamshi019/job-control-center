"""
scripts/enrich_lca.py
---------------------
Enrich the companies table with RECENT-year H-1B LCA filings from
h1bdata.info (a public aggregator of DOL disclosure data). Turns the
sponsor scoring from binary "confirmed / not" into a per-year signal:

    - filings_2024 (int)      -- how many LCA filings that year
    - filings_2025 (int)
    - median_wage (int)       -- typical H-1B salary at that employer
    - top_titles (str)        -- most-common LCA job titles
    - lca_checked_at          -- timestamp so we skip fresh rows

Storage: new `company_lca` table (auto-created), no schema change to
`companies` -- so existing backend keeps running unmodified.

Runs standalone from inside the backend container:
    docker exec -w /app/backend -d job-control-center-backend-1 \
        python scripts/enrich_lca.py --priority high --limit 500

Rate-limits itself to 1 req/sec so we don't hammer h1bdata.info.
Idempotent: skips any company checked in the last 30 days.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta

import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from sqlalchemy import text  # noqa: E402
from sqlmodel import select  # noqa: E402

from app.database import engine, session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.utils.dates import utcnow_naive  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("enrich_lca")

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "Mozilla/5.0 (personal H-1B research)"

# h1bdata.info returns HTML tables. One row per LCA filing.
# Columns: EMPLOYER · JOB TITLE · BASE SALARY · LOCATION · SUBMIT DATE · START DATE
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<td[^>]*>(?:<a[^>]*>)?([^<]*)", re.S)


def ensure_schema():
    with engine.connect() as c:
        c.execute(text("PRAGMA busy_timeout = 30000"))
        c.execute(text("""
            CREATE TABLE IF NOT EXISTS company_lca (
                company_id      INTEGER PRIMARY KEY,
                filings_current INTEGER NOT NULL DEFAULT 0,
                filings_prior   INTEGER NOT NULL DEFAULT 0,
                median_wage     INTEGER,
                top_titles      TEXT NOT NULL DEFAULT '',
                lca_checked_at  DATETIME NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(id)
            )
        """))
        c.execute(text("CREATE INDEX IF NOT EXISTS ix_lca_checked ON company_lca(lca_checked_at)"))
        c.commit()


def _parse_wage(cell: str) -> int:
    """Extract integer USD from "300,000" or "150000" or "$200,000"."""
    m = re.search(r"[\d,]+", cell or "")
    if not m: return 0
    return int(m.group(0).replace(",", ""))


def fetch_lca_for_employer(name: str, year: int) -> list[dict]:
    """Query h1bdata.info for one employer + year. Returns list of dicts."""
    try:
        r = SESSION.get(
            "https://h1bdata.info/index.php",
            params={"em": name, "job": "", "city": "", "year": str(year)},
            timeout=10,
        )
    except (requests.RequestException,) as e:
        log.debug("fetch failed for %s/%d: %s", name, year, e)
        return []
    if r.status_code != 200:
        return []
    rows = _ROW_RE.findall(r.text)
    out = []
    for row in rows[1:]:  # skip header
        cells = _CELL_RE.findall(row)
        if len(cells) < 4: continue
        employer, title, wage_str, location = [c.strip() for c in cells[:4]]
        out.append({
            "employer": employer,
            "title": title,
            "wage": _parse_wage(wage_str),
            "location": location,
        })
    return out


def enrich_one(company: Company) -> dict | None:
    """Fetch + summarize LCA data for one company. Returns summary dict."""
    year_current = datetime.now(timezone.utc).year
    year_prior = year_current - 1
    # h1bdata search by employer name -- use a squashed, no-punct variant to
    # match more forgivingly (their fuzzy search handles "Anthropic PBC" vs "Anthropic")
    query_name = re.sub(r"[^\w\s]", "", company.name).strip()
    if not query_name:
        return None
    hits_curr = fetch_lca_for_employer(query_name, year_current)
    time.sleep(1.0)  # gentle rate-limit
    hits_prior = fetch_lca_for_employer(query_name, year_prior)
    time.sleep(1.0)
    all_hits = hits_curr + hits_prior
    if not all_hits:
        return {"company_id": company.id, "filings_current": 0, "filings_prior": 0,
                "median_wage": None, "top_titles": ""}
    wages = sorted(h["wage"] for h in all_hits if h["wage"] > 0)
    median = wages[len(wages) // 2] if wages else None
    titles = Counter(h["title"] for h in all_hits if h["title"]).most_common(3)
    return {
        "company_id": company.id,
        "filings_current": len(hits_curr),
        "filings_prior":   len(hits_prior),
        "median_wage":     median,
        "top_titles":      "; ".join(f"{t} ({n})" for t, n in titles),
    }


def upsert_lca(summary: dict):
    with engine.connect() as c:
        c.execute(text("PRAGMA busy_timeout = 30000"))
        c.execute(text("""
            INSERT INTO company_lca (company_id, filings_current, filings_prior,
                                     median_wage, top_titles, lca_checked_at)
            VALUES (:cid, :curr, :prior, :wage, :titles, :now)
            ON CONFLICT(company_id) DO UPDATE SET
                filings_current = excluded.filings_current,
                filings_prior   = excluded.filings_prior,
                median_wage     = excluded.median_wage,
                top_titles      = excluded.top_titles,
                lca_checked_at  = excluded.lca_checked_at
        """), {
            "cid": summary["company_id"],
            "curr": summary["filings_current"],
            "prior": summary["filings_prior"],
            "wage": summary.get("median_wage"),
            "titles": summary["top_titles"],
            "now": utcnow_naive(),
        })
        c.commit()


def rescore_from_lca(company: Company, summary: dict) -> int:
    """Recompute h1b_history_score using RECENT LCA data.

    Blend: heavy weight on current-year + prior-year filings, with a floor
    that preserves existing USCIS-based scores when h1bdata returns nothing
    (some real sponsors have data privacy quirks -- don't demote them)."""
    total = summary["filings_current"] + summary["filings_prior"]
    if total == 0:
        return company.h1b_history_score or 40  # unknown baseline
    # Current-year signal is strongest (they're actively sponsoring right now).
    if summary["filings_current"] >= 50: return 95
    if summary["filings_current"] >= 20: return 90
    if summary["filings_current"] >= 5:  return 85
    # Prior-year fallback -- 2025 data may be incomplete early in the year,
    # so heavy 2024 filings still indicate an active sponsor pipeline.
    if total >= 200: return 95      # mega-sponsor (Databricks/Stripe scale)
    if total >= 100: return 90      # very heavy sponsor
    if total >= 50:  return 85
    if total >= 20:  return 75
    if total >= 10:  return 65
    if total >= 3:   return 55
    return max(company.h1b_history_score or 0, 45)  # touchpoint but low volume


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--priority", default="high",
                    choices=["high", "medium", "low", "all"],
                    help="Which crawl-priority tier to enrich. Start with 'high' "
                         "(4700 confirmed sponsors) for biggest impact.")
    ap.add_argument("--limit", type=int, default=500,
                    help="Cap per run (rate-limit protection).")
    ap.add_argument("--min-existing-score", type=int, default=0,
                    help="Only enrich companies whose h1b_history_score is at least "
                         "this (0 = all). Set to 40+ to focus on known sponsors.")
    ap.add_argument("--refresh-days", type=int, default=30,
                    help="Skip companies checked within N days.")
    args = ap.parse_args()

    ensure_schema()
    cutoff = utcnow_naive() - timedelta(days=args.refresh_days)

    with session_scope() as s:
        q = select(Company).where(Company.is_active == True)  # noqa: E712
        if args.priority != "all":
            q = q.where(Company.priority == args.priority)
        if args.min_existing_score:
            q = q.where(Company.h1b_history_score >= args.min_existing_score)
        candidates = s.exec(q).all()
        # Filter out anything freshly checked
        with engine.connect() as c:
            fresh = {r[0] for r in c.execute(
                text("SELECT company_id FROM company_lca WHERE lca_checked_at >= :d"),
                {"d": cutoff}).all()}
        to_do = [c for c in candidates if c.id not in fresh][: args.limit]

    log.info("enrich_lca: %d candidates (priority=%s, min_score=%d), "
             "%d already checked in last %dd, %d remaining -- processing up to %d",
             len(candidates), args.priority, args.min_existing_score,
             len(fresh), args.refresh_days,
             len([c for c in candidates if c.id not in fresh]),
             len(to_do))

    if not to_do:
        log.info("nothing to enrich; exit")
        return 0

    started = time.time()
    updated = 0
    with_data = 0
    for i, company in enumerate(to_do, 1):
        try:
            summary = enrich_one(company)
        except Exception as e:  # noqa: BLE001 -- broad only at top-level per-company
            log.warning("enrich failed for %s: %s", company.name, e)
            continue
        if summary is None:
            continue
        upsert_lca(summary)
        # Also refresh companies.h1b_history_score based on new LCA signal.
        new_score = rescore_from_lca(company, summary)
        if new_score != (company.h1b_history_score or 0):
            with engine.connect() as c:
                c.execute(text("PRAGMA busy_timeout = 30000"))
                c.execute(text("UPDATE companies SET h1b_history_score = :s WHERE id = :id"),
                          {"s": new_score, "id": company.id})
                c.commit()
        updated += 1
        if summary["filings_current"] + summary["filings_prior"] > 0:
            with_data += 1
        if i % 25 == 0:
            elapsed = time.time() - started
            rate = i / max(1, elapsed)
            remain = (len(to_do) - i) / max(0.1, rate)
            log.info("  %d/%d enriched, %d with LCA data (%.1f/s, ~%.0f min left)",
                     i, len(to_do), with_data, rate, remain / 60)

    log.info("DONE: enriched=%d with_lca_data=%d", updated, with_data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
