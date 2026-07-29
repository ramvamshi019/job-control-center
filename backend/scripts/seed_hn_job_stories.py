"""
scripts/seed_hn_job_stories.py
------------------------------
Fetch the HN Firebase /jobstories.json endpoint -- a rolling live list of
~30 job posts (mostly YC startups posting individual roles) -- and seed
new employers into the roster.

Different from seed_hn_who_is_hiring.py: THAT parses the monthly Ask-HN
thread. THIS parses the always-updating /jobstories.json which changes
throughout the day.

Idempotent, cheap (~30 requests total per run), safe to run every 10 min.
"""
from __future__ import annotations

import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from sqlmodel import select  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.database import session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402
from enrich_h1b import norm  # noqa: E402

log = get_logger("seed_hn_job_stories")

HN = requests.Session()
HN.headers["User-Agent"] = "JobControlCenter/1.0 (+personal-job-search; hn-job-stories)"
HN.mount("https://", HTTPAdapter(pool_connections=16, pool_maxsize=16))

_ATS_FROM_HOST = {
    "greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "ashbyhq.com": "ashby",
    "myworkdayjobs.com": "workday",
    "icims.com": "icims",
    "bamboohr.com": "bamboohr",
    "smartrecruiters.com": "smartrecruiters",
    "workable.com": "workable",
    "rippling.com": "rippling",
    "recruitee.com": "recruitee",
}
_SLUG_PATS = {
    "greenhouse":      re.compile(r"greenhouse\.io/([a-zA-Z0-9_-]+)", re.I),
    "lever":           re.compile(r"lever\.co/([a-zA-Z0-9_-]+)", re.I),
    "ashby":           re.compile(r"ashbyhq\.com/([a-zA-Z0-9_-]+)", re.I),
    "workday":         re.compile(r"([a-zA-Z0-9_-]+)\.wd\d+\.myworkdayjobs\.com", re.I),
    "icims":           re.compile(r"careers[-_]?([a-zA-Z0-9_-]+)\.icims\.com", re.I),
    "bamboohr":        re.compile(r"([a-zA-Z0-9_-]+)\.bamboohr\.com", re.I),
    "smartrecruiters": re.compile(r"smartrecruiters\.com/([a-zA-Z0-9_-]+)", re.I),
    "workable":        re.compile(r"workable\.com/([a-zA-Z0-9_-]+)", re.I),
    "rippling":        re.compile(r"rippling\.com/([a-zA-Z0-9_-]+)", re.I),
    "recruitee":       re.compile(r"([a-zA-Z0-9_-]+)\.recruitee\.com", re.I),
}
_COMPANY_RE = re.compile(r"^\s*(.+?)\s*(?:\([^)]*\))?\s*(?:Is Hiring|is Hiring|- Hiring|hiring)", re.I)


def run() -> dict:
    ids = HN.get("https://hacker-news.firebaseio.com/v0/jobstories.json", timeout=10).json() or []
    if not ids:
        return {"fetched": 0, "new": 0}

    def _fetch(iid):
        try: return HN.get(f"https://hacker-news.firebaseio.com/v0/item/{iid}.json", timeout=8).json()
        except Exception: return None

    items = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for it in ex.map(_fetch, ids):
            if it and not it.get("deleted"):
                items.append(it)

    stamp = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
    hits: list[dict] = []
    for it in items:
        title = it.get("title", "")
        url = (it.get("url") or "").strip()
        if not url:
            continue
        m = _COMPANY_RE.match(title)
        name = (m.group(1).strip() if m else title.split(" - ")[0].strip())
        name = re.sub(r"\s*\(YC\s+[A-Z]\d+\)\s*$", "", name).strip()
        if not name or len(name) > 80:
            continue
        host_m = re.match(r"https?://([^/]+)", url)
        if not host_m:
            continue
        host = host_m.group(1).lower()
        ats = next((a for h, a in _ATS_FROM_HOST.items() if h in host), None)
        if not ats:
            continue  # skip URLs that aren't a recognised ATS
        slug_m = _SLUG_PATS[ats].search(url)
        if not slug_m:
            continue
        slug = slug_m.group(1)
        if not slug or len(slug) >= 60:
            continue
        hits.append({"name": name, "ats": ats, "career_url": slug})

    activated = inserted = 0
    for attempt in range(5):
        try:
            with session_scope() as s:
                s.exec(text("PRAGMA busy_timeout = 30000"))
                cos = s.exec(select(Company)).all()
                by_name = {norm(c.name): c for c in cos}
                known_boards = {
                    (c.ats_type or "", (c.career_url or "").strip().lower())
                    for c in cos if c.career_url
                }
                for h in hits:
                    if (h["ats"], h["career_url"].lower()) in known_boards:
                        continue
                    existing = by_name.get(norm(h["name"]))
                    if existing is not None:
                        if existing.is_active:
                            continue
                        existing.career_url = h["career_url"]
                        existing.ats_type = h["ats"]
                        existing.priority = "high"  # live HN posts are actively hiring right now
                        existing.is_active = True
                        existing.notes = ((existing.notes or "") + f" | HN Job Stories {stamp}").strip(" |")
                        s.add(existing); activated += 1
                    else:
                        s.add(Company(
                            name=h["name"], career_url=h["career_url"], ats_type=h["ats"],
                            h1b_history_score=42, priority="high", is_active=True,
                            notes=f"HN Job Stories seed {stamp}",
                        ))
                        inserted += 1
                s.commit()
            break
        except Exception as e:  # noqa: BLE001
            if "locked" in str(e).lower() and attempt < 4:
                time.sleep(4 * (attempt + 1))
            else:
                raise
    total_new = activated + inserted
    log.info("HN Job Stories: fetched=%d hits=%d new_active=%d", len(items), len(hits), total_new)
    return {"fetched": len(items), "hits": len(hits), "new": total_new}


if __name__ == "__main__":
    run()
