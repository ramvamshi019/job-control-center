"""
scripts/hn_buzz.py
------------------
For each company in the AI-ranked top-5 (yesterday's missed backlog),
pull recent Hacker News mentions from Algolia's public JSON API and
cache them. Morning brief then includes a "🗣️ HN buzz" section per
top job so Ram has fresh interview conversation material — e.g.
"You saw Snowflake was on the front page last week? Anything
interesting..."

Uses HN Algolia — no auth, no key, generous rate limit:
  https://hn.algolia.com/api/v1/search?query=<co>&tags=(story,poll)&numericFilters=created_at_i>X

Cron: 05:20 UTC (after filter_sanity 05:15, before ai_rank 05:30).
Requires ai_rank_queue to have run at least once so top-5 exists.
On first-morning-ever this is a no-op (empty ranking).
"""
from __future__ import annotations

import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from datetime import timedelta  # noqa: E402
from sqlalchemy import text  # noqa: E402
import json  # noqa: E402

from app.database import engine  # noqa: E402
from app.utils.dates import utcnow_naive  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("hn_buzz")

ALGOLIA_URL = "https://hn.algolia.com/api/v1/search"
LOOKBACK_DAYS = 90


def _ensure_table() -> None:
    with engine.begin() as c:
        c.execute(text("""
            CREATE TABLE IF NOT EXISTS hn_buzz (
                company_name TEXT PRIMARY KEY,
                mentions     TEXT,       -- JSON array of {title, url, points, created_at}
                total_hits   INTEGER,
                fetched_at   TIMESTAMP
            )
        """))


def _top5_companies() -> list[str]:
    """Pull distinct companies from tonight's AI top-5 (yesterday's backlog)."""
    now = utcnow_naive()
    d24 = now - timedelta(hours=24)
    d48 = now - timedelta(hours=48)
    with engine.connect() as c:
        try:
            rows = c.execute(text("""
                SELECT DISTINCT j.company_name
                FROM job_ai_ranking r
                JOIN jobs j ON j.id = r.job_id
                WHERE j.discovered_at >= :d48 AND j.discovered_at < :d24
                  AND j.status IN ('New', 'Need Review')
                ORDER BY r.fit_score DESC
                LIMIT 5
            """), {"d24": d24, "d48": d48}).all()
        except Exception:
            return []
    return [r[0] for r in rows if r[0]]


def _algolia_query(company: str) -> dict:
    since = int(time.time()) - (LOOKBACK_DAYS * 86400)
    params = {
        "query": f'"{company}"',   # exact-quoted so "Amazon" doesn't match "Amazon Basics" stories primarily
        "tags": "story",
        "numericFilters": f"created_at_i>{since}",
        "hitsPerPage": 15,  # Pull more; extract() filters to top 3 by points threshold
    }
    url = f"{ALGOLIA_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "JCC/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:  # noqa: BLE001
        log.warning("Algolia fetch failed for %s: %s", company, e)
        return {}


MIN_POINTS = 15   # Under 15pts is usually noise (single-vote self-posts, spam)


def _extract(hits: list[dict]) -> list[dict]:
    out = []
    for h in hits:
        if len(out) >= 3:
            break
        title = h.get("title") or h.get("story_title") or ""
        obj_id = h.get("objectID")
        points = h.get("points") or 0
        story_url = f"https://news.ycombinator.com/item?id={obj_id}" if obj_id else None
        if not title or not story_url or points < MIN_POINTS:
            continue
        out.append({
            "title": title[:180],
            "url": story_url,
            "points": points,
            "created_at": h.get("created_at") or "",
        })
    return out


def _store(company: str, mentions: list[dict], total: int) -> None:
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO hn_buzz (company_name, mentions, total_hits, fetched_at)
            VALUES (:c, :m, :t, :ts)
            ON CONFLICT(company_name) DO UPDATE SET
                mentions = excluded.mentions,
                total_hits = excluded.total_hits,
                fetched_at = excluded.fetched_at
        """), {"c": company, "m": json.dumps(mentions), "t": total, "ts": utcnow_naive()})


def run() -> dict:
    _ensure_table()
    companies = _top5_companies()
    log.info("hn_buzz: fetching for %d companies", len(companies))
    if not companies:
        return {"companies": 0}

    fetched = 0
    total_hits = 0
    for co in companies:
        data = _algolia_query(co)
        hits = data.get("hits") or []
        mentions = _extract(hits)
        _store(co, mentions, data.get("nbHits") or 0)
        fetched += 1
        if mentions:
            total_hits += len(mentions)
        time.sleep(0.6)   # be polite to Algolia
    log.info("hn_buzz: fetched=%d total_mentions=%d", fetched, total_hits)
    return {"companies": fetched, "mentions": total_hits}


if __name__ == "__main__":
    raise SystemExit(0 if run() is not None else 1)
