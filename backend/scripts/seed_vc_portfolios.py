"""
scripts/seed_vc_portfolios.py
-----------------------------
Bulk-seed VC portfolio companies into the roster so the auto_discover
fingerprint scanner can pick them up next weekly pass. Sources:

  1. Y Combinator recent batches (W24, S24, W25, S25, W26, S26) — most
     likely to be actively hiring. Pulls yc-oss/api mirror.
  2. TechCrunch RSS — recent "raised $X" posts → mine company names
     mentioned in the title (crude but zero-cost signal for newly-funded
     companies about to hire).
  3. Extensible list — add more sources by appending to SOURCES.

Each source outputs {name, website?}. For each:
  - If a company row with that name already exists, skip
  - Otherwise INSERT with is_active=False + priority='low' + ats_type=None
  - Discovery loop's fingerprint scan will detect the ATS on next pass
    (already runs weekly; bumped to 1.5GB so it completes now)

Note: sources like a16z / Sequoia / Founders Fund publish portfolios on
JS-heavy pages that don't render server-side, so scraping them is
brittle. yc-oss + TechCrunch RSS are the reliable pipes.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Iterable

import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from sqlalchemy import text, func  # noqa: E402

from app.database import engine  # noqa: E402
from app.utils.dates import utcnow_naive  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("seed_vc_portfolios")

YC_URL = "https://raw.githubusercontent.com/yc-oss/api/main/companies/all.json"
TC_RSS = "https://techcrunch.com/tag/funding/feed/"
# Recent-batch focus — earlier batches are already deep in our roster.
YC_TARGET_BATCHES = {
    "Winter 2024", "Summer 2024", "Fall 2024",
    "Winter 2025", "Summer 2025", "Fall 2025", "Spring 2025",
    "Winter 2026", "Summer 2026", "Fall 2026", "Spring 2026",
}


def _pull_yc() -> list[dict]:
    """Filter yc-oss/api down to the recent-batch US/Remote pool."""
    try:
        data = requests.get(YC_URL, timeout=30).json()
    except Exception as e:  # noqa: BLE001
        log.warning("YC fetch failed: %s", e)
        return []
    hits = [
        {"name": c.get("name") or "", "website": (c.get("website") or "").strip(),
         "note": f"YC {c.get('batch','?')} — {c.get('industry','?')}"}
        for c in data
        if (c.get("batch") or "") in YC_TARGET_BATCHES
        and c.get("status") == "Active"
        and (c.get("website") or "").startswith("http")
        and (
            any("United States" in r or "Remote" in r for r in (c.get("regions") or []))
            or "USA" in (c.get("all_locations") or "")
            or "United States" in (c.get("all_locations") or "")
        )
    ]
    log.info("YC recent batches (%s): %d Active US/Remote", ",".join(sorted(YC_TARGET_BATCHES)), len(hits))
    return hits


# Loose company-name matcher from TC headlines: "Acme raises $50M in Series B"
_TC_CAP = re.compile(
    r"\b([A-Z][A-Za-z0-9&.'\-]{1,30}(?:\s[A-Z][A-Za-z0-9&.'\-]{1,30}){0,2})\s+"
    r"(?:raises|raised|secures|secured|closes|closed|nabs|lands|scores)"
    r"\s+\$[\d.]+[MB]?",
    re.IGNORECASE,
)


def _pull_techcrunch() -> list[dict]:
    """Very loose parse of TC's funding RSS feed. Extracts company names from
    'X raises $Y' patterns. No website — auto_discover will URL-guess."""
    try:
        xml = requests.get(TC_RSS, timeout=20, headers={"User-Agent": "JCC/1.0"}).text
    except Exception as e:  # noqa: BLE001
        log.warning("TC fetch failed: %s", e)
        return []
    titles = re.findall(r"<title><!\[CDATA\[(.+?)\]\]></title>", xml)
    hits = []
    seen = set()
    for t in titles:
        m = _TC_CAP.search(t)
        if not m:
            continue
        name = m.group(1).strip()
        # Skip obvious garbage (funding round names, VC firms)
        if len(name) < 3 or name.lower() in ("series", "seed", "round", "vc", "venture"):
            continue
        if name in seen:
            continue
        seen.add(name)
        hits.append({"name": name, "website": "", "note": f"TC funding: {t[:100]}"})
    log.info("TechCrunch funding RSS: %d company mentions", len(hits))
    return hits


def _insert_if_new(candidates: Iterable[dict]) -> dict:
    inserted = existing = 0
    now = utcnow_naive()
    with engine.begin() as c:
        c.execute(text("PRAGMA busy_timeout = 30000"))
        for cand in candidates:
            name = (cand.get("name") or "").strip()
            if len(name) < 2:
                continue
            already = c.execute(text(
                "SELECT id FROM companies WHERE LOWER(name) = :n LIMIT 1"
            ), {"n": name.lower()}).scalar()
            if already:
                existing += 1
                continue
            website = (cand.get("website") or "").strip()
            # Strip scheme + www + trailing slash for career_url storage —
            # matches how the rest of the roster is normalized.
            career_url = website
            if career_url.startswith(("http://", "https://")):
                career_url = career_url.split("://", 1)[1]
            career_url = career_url.rstrip("/").replace("www.", "")
            c.execute(text("""
                INSERT INTO companies
                  (name, ats_type, career_url, is_active, priority,
                   h1b_history_score, notes, created_at, updated_at)
                VALUES
                  (:n, '', :u, 0, 'low', 0, :note, :t, :t)
            """), {"n": name, "u": career_url, "note": (cand.get("note") or "")[:200], "t": now})
            inserted += 1
    return {"inserted": inserted, "existing": existing}


def run() -> dict:
    all_cands: list[dict] = []
    all_cands.extend(_pull_yc())
    all_cands.extend(_pull_techcrunch())
    log.info("total candidates: %d", len(all_cands))
    if not all_cands:
        return {"inserted": 0, "existing": 0, "candidates": 0}
    result = _insert_if_new(all_cands)
    result["candidates"] = len(all_cands)
    log.info("seed_vc_portfolios: candidates=%d inserted=%d existing=%d",
             result["candidates"], result["inserted"], result["existing"])
    return result


if __name__ == "__main__":
    raise SystemExit(0 if run() is not None else 1)
