"""
scripts/live_watch.py
---------------------
Run the crawler CONTINUOUSLY (24/7). Every cycle it crawls the companies that
are "due" (per their priority interval), runs the full pipeline, and auto-adds
any genuinely new jobs (dedupe skips ones already stored). New US jobs then show
up immediately in the dashboard's Live Feed and Find Jobs pages.

Run from backend/ (keep the window open, or use nohup / launchd for true 24/7):
    python scripts/live_watch.py
    python scripts/live_watch.py --interval 300        # check every 5 minutes
    nohup python scripts/live_watch.py > live_watch.log 2>&1 &   # background

Stop with Ctrl-C (or `kill` the nohup PID).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlmodel import select  # noqa: E402

from app.database import init_db, session_scope  # noqa: E402
from app.models.job import Job  # noqa: E402
from app.services import notifier, pruner, scheduler  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("live_watch")


def fresh_alert_jobs(session, max_age_hours: int = 48):
    """Alert-worthy jobs: Best bucket ('New'), confirmed-sponsor (low risk),
    posted within max_age_hours. Returns list of (id, title, company)."""
    cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
    rows = session.exec(
        select(Job).where(Job.status == "New",
                          Job.sponsorship_risk == "low",
                          Job.posted_at != None,  # noqa: E711
                          Job.posted_at >= cutoff)
        .order_by(Job.match_score.desc())
    ).all()
    return [(j.id, j.title, j.company_name) for j in rows]


def one_cycle(batch: int, workers: int = 8) -> dict:
    with session_scope() as session:
        # Keep the DB light first: drop stale postings.
        # Two retention passes, different questions. prune_old_jobs: "was this
        # posted too long ago?" — blind to the 43% of jobs with no posted_at.
        # prune_ghost_jobs: "is this still on the employer's board?" — the one
        # that actually removes filled/closed reqs.
        pruned = pruner.prune_old_jobs(session) + pruner.prune_ghost_jobs(session)
        due = scheduler.due_companies(session)
        total_due = len(due)
        if not due:
            return {"due": 0, "batch": 0, "found": 0, "new": 0, "pruned": pruned}
        # PRIORITY FIRST: the "high" watchlist (confirmed sponsors, 20-min
        # interval) must be serviced before the huge low-priority discovery
        # backlog — otherwise never-checked low companies starve the re-checks
        # and freshness collapses. Within a priority tier, most-overdue first
        # (never-checked have None).
        due.sort(key=lambda c: (scheduler.priority_rank(c),
                                c.last_checked_at is not None,
                                c.last_checked_at or 0))
        # Then INTERLEAVE across ATS types so each batch spans every source
        # (greenhouse, lever, ashby, bamboohr, workday, …) instead of draining
        # one ATS by insertion order before reaching the next. Without this, a
        # cold start crawls all greenhouse first and bamboohr/workday jobs never
        # appear until ~10k companies later.
        ats_rank: dict = {}
        rr: dict = {}
        for c in due:
            a = (c.ats_type or "").lower()
            ats_rank[c.id] = rr.get(a, 0)
            rr[a] = rr.get(a, 0) + 1
        # priority tier stays primary so the watchlist is always serviced first.
        due.sort(key=lambda c: (scheduler.priority_rank(c),
                                c.last_checked_at is not None,
                                ats_rank[c.id],
                                c.last_checked_at or 0))
        batch_companies = due[:batch]
        # RESERVE a slice of every cycle for never-checked companies so the
        # first-time backlog actually drains. Without this, the large high-tier
        # watchlist (re-checked every 15 min) consumes the whole batch and the
        # thousands of never-crawled boards never get their first crawl — i.e.
        # their jobs never appear at all. Reserve ~1/3 of the batch for them.
        never = [c for c in due if c.last_checked_at is None]
        if never:
            reserve = min(len(never), max(1, batch // 3))
            in_batch = {id(c) for c in batch_companies}
            extra = [c for c in never if id(c) not in in_batch][:reserve]
            if extra:
                # Drop the least-urgent tail of the batch to make room.
                batch_companies = batch_companies[: max(0, batch - len(extra))] + extra
        # Fetch companies concurrently (network-bound) then persist serially.
        summaries = scheduler.run_crawl_parallel(session, batch_companies, workers=workers)
    return {
        "due": total_due,
        "batch": len(batch_companies),
        "found": sum(s["found"] for s in summaries),
        "new": sum(s["new"] for s in summaries),
        "pruned": pruned,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=300,
                    help="seconds between cycles (default 300 = 5 min)")
    ap.add_argument("--batch", type=int, default=300,
                    help="max companies to crawl per cycle (default 300)")
    ap.add_argument("--workers", type=int, default=8,
                    help="companies fetched concurrently per cycle (default 8)")
    args = ap.parse_args()

    init_db()
    log.info("Live watch started. Cycle every %ds, batch %d, workers %d. Ctrl-C to stop.",
             args.interval, args.batch, args.workers)

    # Seed the alerted set with jobs that already exist, so the FIRST cycle
    # doesn't notify about the whole backlog — only genuinely-new fresh matches
    # found after startup trigger a banner.
    with session_scope() as session:
        alerted = {jid for jid, _, _ in fresh_alert_jobs(session)}
    log.info("Alerts armed. %d existing fresh-sponsor jobs pre-seeded (won't re-alert).", len(alerted))

    cycle = 0
    while True:
        cycle += 1
        try:
            r = one_cycle(args.batch, workers=args.workers)
            log.info("Cycle %d @ %s — due=%d crawled=%d found=%d NEW=%d pruned=%d",
                     cycle, datetime.now().strftime("%H:%M:%S"),
                     r["due"], r["batch"], r["found"], r["new"], r["pruned"])

            # Alert on any newly-appeared fresh, confirmed-sponsor matches.
            with session_scope() as session:
                fresh = fresh_alert_jobs(session)
            new_hits = [(t, c) for (jid, t, c) in fresh if jid not in alerted]
            alerted.update(jid for jid, _, _ in fresh)
            if new_hits:
                top_t, top_c = new_hits[0]
                title = f"🎯 {len(new_hits)} new sponsor job{'s' if len(new_hits) > 1 else ''}"
                msg = f"{top_t} — {top_c}" + (f"  (+{len(new_hits)-1} more)" if len(new_hits) > 1 else "")
                notifier.notify(title, msg)
                log.info("ALERT: %d new fresh-sponsor job(s) — top: %s @ %s", len(new_hits), top_t, top_c)
        except Exception as exc:  # noqa: BLE001
            log.warning("Cycle %d failed: %s", cycle, exc)
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    main()
