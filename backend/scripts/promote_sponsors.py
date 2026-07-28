"""
scripts/promote_sponsors.py
---------------------------
Promote confirmed H-1B sponsors into the `high` crawl tier so their jobs stay
fresh.

The gap this closes:
    enrich_h1b.py stamps a real h1b_history_score onto every company, and
    seed_h1b_sponsors.py inserts brand-new sponsor *names* at high priority. But
    a company that was already in the roster at priority low/medium and only
    LATER got scored as a sponsor never gets promoted -- its priority is left
    untouched. So a strong sponsor can sit in the `low` tier (re-crawled once a
    day), and its postings surface a day stale. For an F-1/OPT search the sponsor
    boards are exactly the ones worth crawling often, so this lifts them to high.

Capacity is the catch. The `high` tier dominates crawl load (every high company
is scanned 6x/day at the 4h interval), and oversubscribing it silently starves
the low tier -- the exact failure commit fd7fd84 fixed by moving high 3h -> 4h.
So this script is DRY-RUN by default: it reports how many companies would move
and the resulting scans/day per tier vs the measured ceiling (~54k/day), and
only writes when you pass --apply. Tune --min-score to keep total demand under
capacity: 78 = ">=5 USCIS approvals", 88 = ">=20", 95 = ">=100" (see
enrich_h1b.score_for).

    python scripts/promote_sponsors.py                       # report (dry run)
    python scripts/promote_sponsors.py --min-score 88        # report, stricter
    python scripts/promote_sponsors.py --min-score 78 --apply  # actually promote
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlmodel import select  # noqa: E402

from app.database import session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.services.scheduler import PRIORITY_INTERVALS  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("promote_sponsors")

# Measured crawl ceiling from commit fd7fd84 ("100 companies per ~160s cycle").
# The box has since been resized (2->4 vCPU), so real headroom is higher; this
# is the conservative reference the report warns against.
CAPACITY_PER_DAY = 54_000

# Only these tiers are eligible for promotion. `high` is already there; `skip`
# was deliberately disabled and must not be silently reactivated.
PROMOTABLE = {"low", "medium"}


def scans_per_day(priority: str) -> float:
    """How many times/day one company at this tier is crawled, from the single
    source of truth in scheduler.PRIORITY_INTERVALS (skip -> never -> 0)."""
    interval = PRIORITY_INTERVALS.get(priority)
    if not interval:
        return 0.0
    return 86_400.0 / interval.total_seconds()


def _demand(counts: dict[str, int]) -> float:
    return sum(scans_per_day(p) * n for p, n in counts.items())


def one_pass(min_score: int, apply: bool) -> dict:
    with session_scope() as s:
        companies = s.exec(select(Company).where(Company.is_active == True)).all()  # noqa: E712

        # Current tier breakdown (active only -- inactive rows are never crawled).
        before: dict[str, int] = {}
        for c in companies:
            before[c.priority] = before.get(c.priority, 0) + 1

        # A promotion target: a confirmed-enough sponsor sitting below `high`.
        targets = [c for c in companies
                   if c.priority in PROMOTABLE and (c.h1b_history_score or 0) >= min_score]

        after = dict(before)
        for c in targets:
            after[c.priority] -= 1
            after["high"] = after.get("high", 0) + 1

        summary = {
            "eligible": len(targets),
            "high_before": before.get("high", 0),
            "high_after": after.get("high", 0),
            "demand_before": _demand(before),
            "demand_after": _demand(after),
            "promoted": 0,
        }

        log.info("promote sponsors (score >= %d):", min_score)
        log.info("  companies to promote to high : %d", summary["eligible"])
        log.info("  high tier      : %d -> %d", summary["high_before"], summary["high_after"])
        log.info("  scans/day total: %d -> %d   (ceiling ~%d/day)",
                 round(summary["demand_before"]), round(summary["demand_after"]),
                 CAPACITY_PER_DAY)
        if summary["demand_after"] > CAPACITY_PER_DAY:
            log.warning("  ⚠ projected demand EXCEEDS the ~%d/day ceiling by %d scans/day. "
                        "Raise --min-score (88 = >=20 approvals, 95 = >=100) to promote fewer.",
                        CAPACITY_PER_DAY, round(summary["demand_after"] - CAPACITY_PER_DAY))

        if not apply:
            log.info("DRY RUN: nothing written. Re-run with --apply to promote.")
            # Show the strongest sponsors that would move, so the choice is informed.
            for c in sorted(targets, key=lambda c: -(c.h1b_history_score or 0))[:15]:
                log.info("   would promote %-30s  score=%d  %s->high",
                         (c.name or "")[:30], c.h1b_history_score or 0, c.priority)
            return summary

        for c in targets:
            c.priority = "high"
            c.notes = ((c.notes or "") +
                       f" | promoted->high {datetime.utcnow():%Y-%m-%d} "
                       f"(sponsor score {c.h1b_history_score})").strip(" |")
            s.add(c)
        s.commit()
        summary["promoted"] = len(targets)
        log.info("promoted %d sponsor companies into the high tier", summary["promoted"])
        return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-score", type=int, default=78,
                    help="minimum h1b_history_score to promote (default 78 = >=5 USCIS "
                         "approvals; 88 = >=20; 95 = >=100)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write the promotions (default is a dry-run report)")
    a = ap.parse_args()
    if a.min_score < 50:
        log.warning("min-score %d is below the sponsor threshold (50); you'd promote "
                    "non-sponsors too. Continuing anyway.", a.min_score)
    one_pass(a.min_score, a.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
