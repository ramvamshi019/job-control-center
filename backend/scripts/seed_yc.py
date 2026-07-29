"""
scripts/seed_yc.py
------------------
Seed active US-relevant YC companies into the roster.

The public yc-oss/api GitHub mirror gives us `name + website` per company
(no scraping / no ToS violation — same JSON their own site is built from).
We already have `fingerprint_ats(url)` from auto_discover for detecting the
ATS behind a careers page, so this script is small:

    for (name, website) in csv:
        if name already in roster -> skip
        hit `website` -> if it 200s + fingerprint_ats detects an ATS
                       -> insert Company row (active, priority=medium)

Idempotent. Safe to re-run. Runs on the droplet:

    docker exec -w /app/backend -d job-control-center-backend-1 sh -c \
      'python scripts/seed_yc.py \
         --src /app/backend/data/db/scripts/yc_us_active.csv \
         --workers 24 \
       > /app/backend/data/db/scripts/yc_seed.log 2>&1'
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from sqlmodel import select  # noqa: E402

from app.database import session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402
from auto_discover import fingerprint_ats  # noqa: E402
from enrich_h1b import norm  # noqa: E402

log = get_logger("seed_yc")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "JobControlCenter/1.0 (+personal-job-search; yc-seed)"})
SESSION.mount("https://", HTTPAdapter(pool_connections=64, pool_maxsize=64))
SESSION.mount("http://", HTTPAdapter(pool_connections=64, pool_maxsize=64))
CHECK_TIMEOUT = 6


def _probe(name: str, website: str) -> dict | None:
    """Fetch the website + let fingerprint_ats detect an ATS. Return the hit
    dict on success or None on any failure (dead site, no ATS detected, etc.)."""
    try:
        resp = SESSION.get(website, timeout=CHECK_TIMEOUT, allow_redirects=True)
    except Exception:  # noqa: BLE001
        return None
    if resp.status_code >= 400 or len(resp.text) < 400:
        return None
    r = fingerprint_ats(resp.url)
    if not r:
        return None
    ats, career_url = r
    return {"name": name.strip(), "ats": ats, "career_url": career_url, "website": website}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="CSV with columns: name, website (+ optional others)")
    ap.add_argument("--priority", default="medium", choices=["high", "medium", "low"])
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--limit", type=int, default=0, help="cap probes (testing)")
    args = ap.parse_args()

    with open(args.src, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [(r["name"].strip(), r["website"].strip()) for r in reader
                if r.get("name") and r.get("website", "").startswith("http")]
    log.info("read %d YC companies from %s", len(rows), args.src)

    # Skip anything already active on a known board -- exactly like seed_uscis_url_guess.
    with session_scope() as s:
        cos = s.exec(select(Company)).all()
        already_active = {norm(c.name) for c in cos if c.career_url and c.ats_type and c.is_active}
        by_name = {norm(c.name): c for c in cos}
        known_boards = {
            (c.ats_type or "", (c.career_url or "").strip().lower())
            for c in cos if c.career_url
        }

    todo = [(n, w) for n, w in rows if norm(n) not in already_active]
    if args.limit:
        todo = todo[: args.limit]
    log.info("probing %d new names (skipping %d already active)", len(todo), len(rows) - len(todo))

    started = time.time()
    hits: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_probe, n, w): (n, w) for n, w in todo}
        for fut in as_completed(futures):
            done += 1
            try:
                res = fut.result()
            except Exception:  # noqa: BLE001
                res = None
            if res and (res["ats"], res["career_url"].lower()) not in known_boards:
                hits.append(res)
                known_boards.add((res["ats"], res["career_url"].lower()))
            if done % 250 == 0:
                elapsed = time.time() - started
                rate = done / max(1, elapsed)
                remain = (len(todo) - done) / max(0.1, rate)
                log.info("  %d/%d probed, %d hits (%.1f/s, ~%.0f min left)",
                         done, len(todo), len(hits), rate, remain / 60)

    log.info("probe complete: %d/%d -> %d ATS hits", done, len(todo), len(hits))
    if not hits:
        return 0

    stamp = datetime.utcnow().strftime("%Y-%m-%d")
    inserted = 0
    activated = 0
    with session_scope() as s:
        cos = s.exec(select(Company)).all()
        by_name = {norm(c.name): c for c in cos}
        pending: list[Company] = []
        for h in hits:
            existing = by_name.get(norm(h["name"]))
            if existing is not None and not existing.is_active:
                existing.career_url = h["career_url"]
                existing.ats_type = h["ats"]
                existing.priority = args.priority
                existing.is_active = True
                existing.notes = (
                    (existing.notes or "")
                    + f" | YC seed {stamp} via {h['website']}"
                ).strip(" |")
                s.add(existing)
                activated += 1
            elif existing is not None:
                continue  # already active on some other board
            else:
                pending.append(Company(
                    name=h["name"], career_url=h["career_url"], ats_type=h["ats"],
                    h1b_history_score=40,  # neutral 'unknown' baseline; USCIS enrichment can lift later
                    priority=args.priority, is_active=True,
                    notes=f"YC seed {stamp}; detected via {h['website']}",
                ))
        if pending:
            s.add_all(pending)
            inserted += len(pending)
        s.commit()
    log.info("ACTIVATED %d, INSERTED %d -- both at priority=%s", activated, inserted, args.priority)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
