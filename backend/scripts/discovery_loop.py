"""
scripts/discovery_loop.py
-------------------------
Master automated-discovery loop for the `discovery` container. Runs every
enabled source at its own useful cadence -- there is no single "check
every N seconds" because sources genuinely refresh on different clocks:

    HN Job Stories       -> every 10 min  (~30-item rolling live list)
    HN Who Is Hiring     -> every 6 hrs   (catches new posts to the current
                                           month's thread within 6h)
    YC directory refresh -> every 24 hrs  (new companies join daily-ish)
    Weekly harvest       -> every 7 days  (existing auto_discover + registry
                                           maintenance -- heavy pass)

Every source is IDEMPOTENT (checks known_boards + norm(name) before insert),
so re-running is safe and cheap. Missing / failing sources don't take down
the loop -- each stage is try/except-guarded.

WHY NOT "EVERY MINUTE"?
    Livewatch (a different container) already checks every KNOWN company
    every 60 s -- that's what puts new *jobs* into Posted Today within
    ~2 min of an employer publishing. This loop is about discovering new
    *companies*, which come from external feeds that only change every
    hours-to-days. Polling those every minute would exhaust API quotas
    and yield nothing 99% of the time.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app.utils.logging import get_logger  # noqa: E402

log = get_logger("discovery_loop")

# Cadences in seconds. Overridable via env for ops flexibility.
JOB_STORIES_EVERY   = int(os.environ.get("DISC_JOBSTORIES_EVERY_S",  60 * 10))       # 10 min
HN_HIRING_EVERY     = int(os.environ.get("DISC_HN_HIRING_EVERY_S",   60 * 60 * 6))   # 6 hrs
YC_EVERY            = int(os.environ.get("DISC_YC_EVERY_S",          60 * 60 * 24))  # 24 hrs
VC_EVERY            = int(os.environ.get("DISC_VC_EVERY_S",           60 * 60 * 24 * 2))  # 48 hrs
HARVEST_EVERY       = int(os.environ.get("DISC_HARVEST_EVERY_S",     60 * 60 * 24 * 7))  # 7 days
GMAIL_EVERY         = int(os.environ.get("DISC_GMAIL_EVERY_S",       60 * 15))       # 15 min
NOTIFY_EVERY        = int(os.environ.get("DISC_NOTIFY_EVERY_S",      60 * 15))       # 15 min -- email digest of top new sponsor jobs

# Sleep between the *loop's outer tick*. Short enough that per-source cadences
# are honored to within ~1 min. Doesn't drive API load -- source functions
# only fire when their own timer is due.
TICK_S = 60


def _safe_run(name: str, fn):
    """Log + swallow exceptions so one broken source doesn't kill the loop."""
    try:
        t0 = time.time()
        log.info("== %s: START ==", name)
        result = fn()
        log.info("== %s: OK (%.1fs) %s", name, time.time() - t0, result)
    except Exception as e:  # noqa: BLE001
        log.warning("== %s: FAILED -- %s", name, e)
        log.debug("%s", traceback.format_exc())


def main() -> int:
    log.info(
        "discovery loop starting. cadences: job_stories=%ds hn_hiring=%ds yc=%ds harvest=%ds gmail=%ds notify=%ds",
        JOB_STORIES_EVERY, HN_HIRING_EVERY, YC_EVERY, HARVEST_EVERY, GMAIL_EVERY, NOTIFY_EVERY,
    )

    # Per-source "next-run" timestamps. Start EVERY source at now so the first
    # tick runs each once and populates the DB with the current firehose;
    # after that each source waits its own interval.
    now = time.time()
    next_run = {
        "job_stories": now,
        "hn_hiring":   now,
        "yc":          now,
        "vc":          now + 30,  # small delay so YC + VC seeders don't stampede the DB
        "harvest":     now,
        "gmail":       now,
        "notify":      now + 60,  # slight delay so first tick doesn't fire on stale DB
    }

    def _due(key: str) -> bool:
        return time.time() >= next_run[key]

    while True:
        # 1. HN Job Stories (fast, small)
        if _due("job_stories"):
            _safe_run("HN Job Stories", _hn_job_stories)
            next_run["job_stories"] = time.time() + JOB_STORIES_EVERY

        # 2. HN Who Is Hiring monthly thread (most recent 1 month, cheap)
        if _due("hn_hiring"):
            _safe_run("HN Who Is Hiring", _hn_who_is_hiring)
            next_run["hn_hiring"] = time.time() + HN_HIRING_EVERY

        # 3. YC directory refresh
        if _due("yc"):
            _safe_run("YC directory", _yc_directory)
            next_run["yc"] = time.time() + YC_EVERY

        # 3b. VC portfolio bulk-seed (YC recent batches + TC funding feed)
        if _due("vc"):
            _safe_run("VC portfolios", _vc_portfolios)
            next_run["vc"] = time.time() + VC_EVERY

        # 4. Weekly heavy harvest (existing pipeline)
        if _due("harvest"):
            _safe_run("Weekly harvest", _harvest_and_discover)
            next_run["harvest"] = time.time() + HARVEST_EVERY

        # 5. Gmail watcher (IMAP poll for recruiter responses on applied jobs)
        if _due("gmail"):
            _safe_run("Gmail watcher", _gmail_watch)
            next_run["gmail"] = time.time() + GMAIL_EVERY

        # 6. Email digest of new top-score sponsor jobs (via same SMTP creds)
        if _due("notify"):
            _safe_run("Notify digest", _notify_watch)
            next_run["notify"] = time.time() + NOTIFY_EVERY

        time.sleep(TICK_S)


# ---------- source runners -----------------------------------------------

def _hn_job_stories():
    from seed_hn_job_stories import run
    return run()


def _hn_who_is_hiring():
    from seed_hn_who_is_hiring import run
    # Only the current month (avoids re-parsing years each tick; historical
    # months are only backfilled on first run of this loop or on demand).
    return run(months=1, workers=6)


def _yc_directory():
    from seed_yc_directory import run
    return run(workers=6)


def _vc_portfolios():
    """Bulk-seed recent YC batches + TechCrunch funding announcements.
    Companies get inserted as dormant; auto_discover fingerprints them
    into an ATS on the next weekly harvest pass."""
    from seed_vc_portfolios import run
    return run()


def _gmail_watch():
    """Poll Gmail for recruiter responses on applied jobs. No-op if the user
    hasn't set up Gmail credentials yet (returns {'configured': False})."""
    from gmail_watcher import run
    return run()


def _notify_watch():
    """Email digest of new top-score sponsor jobs to the user's own inbox
    via SMTP. Reuses the Gmail App Password. No-op if Gmail not configured."""
    from notify_watcher import run
    return run()


def _harvest_and_discover():
    """Existing weekly pipeline: refresh sources JSONL + auto_discover +
    registry_maintenance. Shells out because those scripts are heavy and
    tuned to run standalone."""
    import subprocess
    calls = [
        ["python", "scripts/harvest_company_sources.py",
         "--out", "/app/backend/data/db/discovered_companies.jsonl"],
        ["python", "scripts/auto_discover.py", "--workers", "16",
         "--seed-file", "/app/backend/data/db/discovered_companies.jsonl"],
        ["python", "scripts/registry_maintenance.py"],
    ]
    for cmd in calls:
        try:
            subprocess.run(cmd, cwd="/app/backend", check=False, timeout=60 * 60)
        except Exception as e:  # noqa: BLE001
            log.warning("harvest step %s failed: %s", cmd[1], e)
    return {"stage": "harvest+auto_discover+maintenance"}


if __name__ == "__main__":
    raise SystemExit(main())
