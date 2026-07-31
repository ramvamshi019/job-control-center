"""
scripts/enrich_job_extras.py
----------------------------
Regex-based extractor pulled from each job's description. NO Claude
tokens burned — pure pattern matching against text we already have.
Runs every 15 min on cron.

Extracted fields (stored in new job_extras table):
  years_min      int?      lowest 'N years' figure mentioned
  years_max      int?      highest 'N years' figure mentioned
  citizenship    bool      requires US citizenship
  clearance      bool      requires security clearance
  no_sponsor     bool      says 'no sponsorship' (still applyable on OPT)
  remote_ok      bool      mentions remote / WFH / hybrid
  salary_min     int?      lowest \$X annual figure
  salary_max     int?      highest \$X annual figure

Enricher picks up to N unenriched jobs in status='New'/'Need Review'/'Approved'
discovered in the last 48h. Older jobs are skipped (probably closed anyway).

Dashboard shows these as chips per-row on Date Browser + other pages.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from sqlalchemy import text  # noqa: E402

from app.database import engine  # noqa: E402
from app.utils.dates import utcnow_naive  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("enrich_job_extras")

BATCH_SIZE = int(os.environ.get("ENRICH_BATCH", "300"))
WINDOW_HOURS = int(os.environ.get("ENRICH_WINDOW_H", "72"))


def _ensure_table() -> None:
    with engine.begin() as c:
        c.execute(text("""
            CREATE TABLE IF NOT EXISTS job_extras (
                job_id       INTEGER PRIMARY KEY,
                years_min    INTEGER,
                years_max    INTEGER,
                citizenship  INTEGER DEFAULT 0,
                clearance    INTEGER DEFAULT 0,
                no_sponsor   INTEGER DEFAULT 0,
                remote_ok    INTEGER DEFAULT 0,
                salary_min   INTEGER,
                salary_max   INTEGER,
                enriched_at  TIMESTAMP,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            )
        """))


# --- Regex library ---------------------------------------------------------
_YEARS_RE = re.compile(
    r"(\d{1,2})\+?\s*(?:-\s*(\d{1,2})\s*)?\s*(?:\+\s*)?years?(?:\s+of)?"
    r"(?:\s+(?:relevant|professional|hands-?on|industry))?"
    r"\s+experience",
    re.IGNORECASE,
)
_CITIZEN_RE = re.compile(
    r"\b(us\s*citizen(?:ship)?|u\.s\.\s*citizen|must be a us citizen|"
    r"united states citizen|citizenship\s+required)\b",
    re.IGNORECASE,
)
_CLEARANCE_RE = re.compile(
    r"\b(ts/sci|top\s+secret|active\s+clearance|security\s+clearance|"
    r"secret\s+clearance|dod\s+clearance|public\s+trust)\b",
    re.IGNORECASE,
)
_NO_SPONSOR_RE = re.compile(
    r"\b(no\s+visa\s+sponsorship|we\s+do\s+not\s+sponsor|"
    r"without\s+sponsorship|unable\s+to\s+sponsor|"
    r"not\s+able\s+to\s+sponsor|no\s+sponsorship)\b",
    re.IGNORECASE,
)
_REMOTE_RE = re.compile(
    r"\b(fully\s+remote|100%\s+remote|remote(?:-?first|-?only)?|"
    r"work\s+from\s+home|wfh|hybrid|remote/on-?site|remote-?friendly)\b",
    re.IGNORECASE,
)
# Salary — matches "$120,000", "$120K", "120k-180k", "$120-180k" etc
_SALARY_RE = re.compile(
    r"\$\s*(\d{2,3})(?:,\d{3}|k)?"
    r"(?:\s*(?:-|to|–)\s*\$?\s*(\d{2,3})(?:,\d{3}|k)?)?"
    r"(?:\s*(?:per\s+year|/yr|/year|annually|annual))?",
    re.IGNORECASE,
)


def _extract_years(text_body: str) -> tuple[int | None, int | None]:
    """Find all 'N years' matches; return (min, max) or (None, None)."""
    mins, maxes = [], []
    for m in _YEARS_RE.finditer(text_body):
        low = int(m.group(1))
        high = int(m.group(2)) if m.group(2) else low
        if 0 <= low <= 30:
            mins.append(low)
        if 0 <= high <= 30:
            maxes.append(high)
    if not mins:
        return None, None
    return min(mins), max(maxes) if maxes else None


def _extract_salary(text_body: str) -> tuple[int | None, int | None]:
    """Best-effort salary parse. Returns (min, max) as annual USD or (None, None)."""
    best_low, best_high = None, None
    for m in _SALARY_RE.finditer(text_body):
        low = int(m.group(1))
        high = int(m.group(2)) if m.group(2) else None
        # Assume $XXk means $XX,000
        if low < 500:  # $120k form
            low *= 1000
        if high and high < 500:
            high *= 1000
        # Sanity clamp: annual salaries 30k-800k
        if not (30_000 <= low <= 800_000):
            continue
        if high and not (30_000 <= high <= 800_000):
            high = None
        if best_low is None or low > best_low:
            best_low, best_high = low, high
    return best_low, best_high


def _extract_all(desc: str, title: str) -> dict:
    body = (title + "  " + (desc or "")).strip()
    ymin, ymax = _extract_years(body)
    smin, smax = _extract_salary(body)
    return {
        "years_min": ymin,
        "years_max": ymax,
        "citizenship": 1 if _CITIZEN_RE.search(body) else 0,
        "clearance": 1 if _CLEARANCE_RE.search(body) else 0,
        "no_sponsor": 1 if _NO_SPONSOR_RE.search(body) else 0,
        "remote_ok": 1 if _REMOTE_RE.search(body) else 0,
        "salary_min": smin,
        "salary_max": smax,
    }


def _pick_candidates() -> list[dict]:
    cutoff = utcnow_naive() - timedelta(hours=WINDOW_HOURS)
    with engine.connect() as c:
        c.execute(text("PRAGMA busy_timeout = 30000"))
        rows = c.execute(text("""
            SELECT j.id, j.title, j.description
            FROM jobs j
            LEFT JOIN job_extras e ON e.job_id = j.id
            WHERE j.discovered_at >= :d
              AND j.status IN ('New', 'Need Review', 'Approved')
              AND e.job_id IS NULL
              AND LENGTH(COALESCE(j.description, '')) > 100
            ORDER BY j.discovered_at DESC
            LIMIT :n
        """), {"d": cutoff, "n": BATCH_SIZE}).all()
        return [dict(r._mapping) for r in rows]


def _store_many(rows: list[tuple]) -> None:
    if not rows:
        return
    with engine.begin() as c:
        c.execute(text("PRAGMA busy_timeout = 30000"))
        for r in rows:
            c.execute(text("""
                INSERT OR REPLACE INTO job_extras
                    (job_id, years_min, years_max, citizenship, clearance,
                     no_sponsor, remote_ok, salary_min, salary_max, enriched_at)
                VALUES (:jid, :ymin, :ymax, :cit, :cle, :nsp, :rem, :smin, :smax, :ts)
            """), r)


def run() -> dict:
    _ensure_table()
    cands = _pick_candidates()
    log.info("enrich_job_extras: %d unenriched jobs in window", len(cands))
    if not cands:
        return {"enriched": 0}
    now = utcnow_naive()
    rows = []
    for j in cands:
        x = _extract_all(j["description"] or "", j["title"] or "")
        rows.append({
            "jid": j["id"],
            "ymin": x["years_min"], "ymax": x["years_max"],
            "cit": x["citizenship"], "cle": x["clearance"],
            "nsp": x["no_sponsor"], "rem": x["remote_ok"],
            "smin": x["salary_min"], "smax": x["salary_max"],
            "ts": now,
        })
    _store_many(rows)
    log.info("enrich_job_extras: stored %d extras", len(rows))
    return {"enriched": len(rows)}


if __name__ == "__main__":
    raise SystemExit(0 if run() is not None else 1)
