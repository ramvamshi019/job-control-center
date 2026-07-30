"""
scripts/seed_hn_who_is_hiring.py
--------------------------------
Auto-seeder for HN "Ask HN: Who is hiring?" monthly threads.

Meant to run on a schedule (from discovery_loop.py) -- fetches the N most
recent monthly threads via the Firebase HN API, extracts (company, ATS URL
or website) from each top-level comment, and pushes hits into the roster.
Idempotent: names already active get skipped; (ats, slug) pairs already
known get skipped. Safe to re-run continuously.

Two-stage seeder:
  1. Direct ATS-URL matches (Greenhouse/Lever/Ashby/etc. links in the
     comment) -> insert straight, no HTTP probing needed.
  2. Website-only comments -> hand off to seed_yc._probe which does the
     one-page fingerprint we already validated. (Reused as a library.)

CLI:
    python scripts/seed_hn_who_is_hiring.py --months 3 --workers 8
"""
from __future__ import annotations

import argparse
import csv
import html
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Iterable

import requests
from requests.adapters import HTTPAdapter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from sqlmodel import select  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.database import session_scope, engine  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.utils.ats import detect_ats  # noqa: E402  -- shared ATS URL patterns
from app.utils.logging import get_logger  # noqa: E402
from enrich_h1b import norm  # noqa: E402

# Reuse the same website-probe function seed_yc uses; guarantees fingerprint
# behavior stays identical across sources.
from seed_yc import _probe as probe_website  # noqa: E402

log = get_logger("seed_hn")

HN = requests.Session()
HN.headers["User-Agent"] = "JobControlCenter/1.0 (+personal-job-search; hn-seeder)"
HN.mount("https://", HTTPAdapter(pool_connections=32, pool_maxsize=32))

# Hosts that appear in HN comments but aren't the employer's careers page.
_SKIP_HOSTS = (
    "linkedin.com", "twitter.com", "x.com", "github.com", "ycombinator.com",
    "news.ycombinator.com", "notion.so", "notion.site", "airtable.com",
    "tally.so", "forms.gle", "docs.google.com", "wellfound.com",
)


def _latest_thread_ids(months: int) -> list[int]:
    """Return the N most recent 'Ask HN: Who is hiring?' thread IDs."""
    subs = HN.get("https://hacker-news.firebaseio.com/v0/user/whoishiring.json", timeout=10).json()
    ids: list[int] = []
    for iid in subs.get("submitted", []):
        if len(ids) >= months:
            break
        it = HN.get(f"https://hacker-news.firebaseio.com/v0/item/{iid}.json", timeout=10).json() or {}
        title = (it.get("title") or "").lower()
        if "who is hiring" in title or "who's hiring" in title:
            ids.append(iid)
    return ids


def _fetch_comment_ids(thread_ids: Iterable[int]) -> list[int]:
    out: list[int] = []
    for tid in thread_ids:
        t = HN.get(f"https://hacker-news.firebaseio.com/v0/item/{tid}.json", timeout=10).json() or {}
        out.extend(t.get("kids") or [])
    return out


def _fetch_item(kid: int) -> dict | None:
    """Fetch one HN comment. Returns None on any expected network / decode
    failure so callers can drop the row silently; deliberately does NOT
    catch KeyboardInterrupt or MemoryError so the process still exits
    cleanly when the user hits Ctrl-C or the OOM killer starts firing."""
    try:
        return HN.get(f"https://hacker-news.firebaseio.com/v0/item/{kid}.json", timeout=8).json()
    except (requests.RequestException, ValueError):
        # RequestException = timeout / connection / HTTP; ValueError = bad JSON
        return None


def _parse(comment: dict) -> dict | None:
    """Extract {name, ats, career_url} from one comment. `ats=''` means the
    comment only had a company website (needs probing later)."""
    text = html.unescape(comment.get("text", ""))
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain).strip()
    parts = plain.split("|")
    name = (parts[0].strip() if parts else "")
    if not name or len(name) > 80:
        return None
    lname = name.lower()
    if any(b in lname for b in ("stealth", "anonymous", "[flagged]", "[dead]")):
        return None
    if name.startswith("("):
        return None
    # Strip HN-comment cruft from names like "Distru ( https://www.distru.com )"
    name = name.split(" (")[0].split(" - ")[0].strip()
    if not name:
        return None
    hit = detect_ats(text)
    if hit:
        ats, slug = hit
        return {"name": name, "ats": ats, "career_url": slug}
    for url in re.findall(r"https?://[^\s<>\"')]+", text):
        host_m = re.match(r"https?://([^/]+)", url)
        if not host_m:
            continue
        host = host_m.group(1).lower()
        if any(sk in host for sk in _SKIP_HOSTS):
            continue
        return {"name": name, "ats": "", "career_url": f"https://{host}"}
    return None


def _persist(hits: list[dict], stamp: str, source_note: str, priority: str = "medium") -> tuple[int, int]:
    """Idempotent insert. Returns (activated, inserted)."""
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
                    key = (h["ats"], h["career_url"].lower())
                    if key in known_boards:
                        continue
                    existing = by_name.get(norm(h["name"]))
                    if existing is not None:
                        if existing.is_active:
                            continue
                        existing.career_url = h["career_url"]
                        existing.ats_type = h["ats"]
                        existing.priority = priority
                        existing.is_active = True
                        existing.h1b_history_score = max(existing.h1b_history_score or 0, 40)
                        existing.notes = ((existing.notes or "") + f" | {source_note} {stamp}").strip(" |")
                        s.add(existing); activated += 1
                    else:
                        s.add(Company(
                            name=h["name"], career_url=h["career_url"], ats_type=h["ats"],
                            h1b_history_score=40, priority=priority, is_active=True,
                            notes=f"{source_note} {stamp}",
                        ))
                        inserted += 1
                s.commit()
            return activated, inserted
        except Exception as e:  # noqa: BLE001
            if "locked" in str(e).lower() and attempt < 4:
                time.sleep(4 * (attempt + 1))
            else:
                raise
    return activated, inserted


def run(months: int = 3, workers: int = 8) -> dict:
    """One end-to-end pass. Returns summary counts."""
    stamp = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
    thread_ids = _latest_thread_ids(months)
    log.info("HN: fetching last %d months (%d threads)", months, len(thread_ids))
    if not thread_ids:
        return {"months": months, "direct_new": 0, "probe_new": 0}

    kids = _fetch_comment_ids(thread_ids)
    log.info("HN: %d top-level comments", len(kids))
    comments: list[dict] = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for c in ex.map(_fetch_item, kids):
            if c and c.get("text") and not c.get("deleted"):
                comments.append(c)
    log.info("HN: %d valid comments", len(comments))

    parsed = [p for p in (_parse(c) for c in comments) if p]
    direct: dict[tuple[str, str], dict] = {}
    probe: dict[str, dict] = {}
    for p in parsed:
        if p["ats"]:
            direct.setdefault((p["ats"], p["career_url"].lower()), p)
        else:
            probe.setdefault(p["career_url"].lower(), p)
    log.info("HN: parsed=%d direct=%d probe=%d", len(parsed), len(direct), len(probe))

    d_act, d_ins = _persist(list(direct.values()), stamp, "HN Who Is Hiring auto")
    log.info("HN direct: activated=%d inserted=%d", d_act, d_ins)

    # Probe website-only rows in parallel (rolling window)
    to_probe = [(p["name"], p["career_url"]) for p in probe.values()]
    probe_hits: list[dict] = []
    if to_probe:
        from concurrent.futures import wait, FIRST_COMPLETED
        from itertools import islice
        it = iter(to_probe)
        window = workers * 4
        with ThreadPoolExecutor(max_workers=workers) as ex:
            pending = {ex.submit(probe_website, n, w) for n, w in islice(it, window)}
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    try:
                        r = fut.result()
                    except Exception:  # noqa: BLE001
                        r = None
                    if r:
                        probe_hits.append(r)
                    nxt = next(it, None)
                    if nxt is not None:
                        pending.add(ex.submit(probe_website, *nxt))
    p_act, p_ins = _persist(probe_hits, stamp, "HN Who Is Hiring auto (probe)")
    log.info("HN probe:  fingerprinted=%d activated=%d inserted=%d",
             len(probe_hits), p_act, p_ins)

    return {
        "months": months,
        "direct_new": d_act + d_ins,
        "probe_new": p_act + p_ins,
        "total_new": d_act + d_ins + p_act + p_ins,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=3, help="how many recent monthly threads to scan")
    ap.add_argument("--workers", type=int, default=8, help="probe workers")
    args = ap.parse_args()
    summary = run(months=args.months, workers=args.workers)
    log.info("HN SEED COMPLETE: %s", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
