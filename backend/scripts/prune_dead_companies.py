"""
scripts/prune_dead_companies.py
-------------------------------
Deactivate long-time dead-weight companies to free crawl capacity.

The problem this closes:
    auto_discover.py grows the roster automatically -- every USCIS sponsor name
    is probed against 6 ATS platforms and any 200-OK board is seeded at
    priority=low. Many of those boards return zero postings, ever (a real ATS
    subdomain that happens to be dormant, a board hidden behind a login, an
    iCIMS instance that publishes only via a different feed, etc.). Each of
    those dead companies still costs 1 crawl/day, and at 8k+ of them we are
    burning ~8k scans/day probing boards that don't answer.

Criteria (default): a company is "dead" iff
    is_active = True
    priority  = 'low'          (never touch high/medium sponsors)
    has NEVER produced a job row
    created_at is older than --min-age-days (default 30)

The age gate is the safety catch: an auto_discover run last night that added
a real board doesn't get pruned before its first weekly posting lands. We only
retire companies that have had a full month to prove themselves and produced
nothing.

Reversal: sets is_active=False (not DELETE). Restore with a single UPDATE.

    python scripts/prune_dead_companies.py                       # dry run
    python scripts/prune_dead_companies.py --min-age-days 60     # stricter
    python scripts/prune_dead_companies.py --apply               # write
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlmodel import select  # noqa: E402

from app.database import session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.models.job import Job  # noqa: E402
from app.services.scheduler import PRIORITY_INTERVALS  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("prune_dead_companies")

# The measured throughput ceiling that the fd7fd84 commit was tuned against.
# Post the 4-vCPU resize + query fixes real capacity is higher, so this is a
# conservative headline number for the report.
CAPACITY_PER_DAY = 54_000


def scans_per_day(priority: str) -> float:
    interval = PRIORITY_INTERVALS.get(priority)
    if not interval:
        return 0.0
    return 86_400.0 / interval.total_seconds()


def _tier_counts(companies) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in companies:
        out[c.priority] = out.get(c.priority, 0) + 1
    return out


def _demand(counts: dict[str, int]) -> float:
    return sum(scans_per_day(p) * n for p, n in counts.items())


def one_pass(min_age_days: int, apply: bool) -> dict:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=min_age_days)

    with session_scope() as s:
        # Every currently-active low-tier company plus a flag for whether it has
        # ever produced a job. `session.exec` streams; we materialise once.
        active_low = s.exec(
            select(Company).where(Company.is_active == True,  # noqa: E712
                                  Company.priority == "low")
        ).all()

        # A single query for "who has ever produced a job" beats N per-company
        # lookups: pull the set of company_ids that appear in jobs at least once.
        # SQLModel returns scalars here (int), not row tuples.
        producers = set(s.exec(select(Job.company_id).distinct()).all())

        targets = [
            c for c in active_low
            if c.id not in producers
            and c.created_at is not None
            and c.created_at < cutoff
        ]

        # Roster BEFORE (all active, by tier), so demand math is honest.
        all_active = s.exec(
            select(Company).where(Company.is_active == True)  # noqa: E712
        ).all()
        before = _tier_counts(all_active)

        after = dict(before)
        after["low"] = after.get("low", 0) - len(targets)

        # Breakdown by ATS -- shows which platform is contributing the dead weight.
        by_ats: dict[str, int] = {}
        for c in targets:
            by_ats[c.ats_type or "(unknown)"] = by_ats.get(c.ats_type or "(unknown)", 0) + 1

        summary = {
            "eligible": len(targets),
            "low_before": before.get("low", 0),
            "low_after": after.get("low", 0),
            "demand_before": _demand(before),
            "demand_after": _demand(after),
            "pruned": 0,
            "by_ats": by_ats,
            "cutoff": cutoff.isoformat(timespec="seconds"),
        }

        log.info("prune dead companies (created before %s, never returned a job):",
                 summary["cutoff"])
        log.info("  eligible for deactivation : %d", summary["eligible"])
        log.info("  low tier      : %d -> %d", summary["low_before"], summary["low_after"])
        log.info("  scans/day total: %d -> %d   (ceiling ~%d/day)",
                 round(summary["demand_before"]), round(summary["demand_after"]),
                 CAPACITY_PER_DAY)
        log.info("  by ATS:")
        for ats, n in sorted(by_ats.items(), key=lambda kv: -kv[1]):
            log.info("    %-20s  %d", ats, n)

        if not apply:
            log.info("DRY RUN: nothing written. Re-run with --apply to deactivate.")
            for c in targets[:15]:
                log.info("   would deactivate  %-30s  ats=%-12s  created=%s",
                         (c.name or "")[:30], (c.ats_type or "?")[:12],
                         c.created_at.isoformat(timespec="seconds") if c.created_at else "?")
            return summary

        for c in targets:
            c.is_active = False
            c.notes = ((c.notes or "") +
                       f" | deactivated {datetime.now(timezone.utc).replace(tzinfo=None):%Y-%m-%d} "
                       f"dead-weight (never returned a job in "
                       f"{min_age_days}+ days)").strip(" |")
            s.add(c)
        s.commit()
        summary["pruned"] = len(targets)
        log.info("deactivated %d dead-weight companies", summary["pruned"])
        return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-age-days", type=int, default=30,
                    help="only prune companies added more than this many days ago "
                         "(default 30; the safety gate against pruning fresh "
                         "auto_discover finds)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write the deactivations (default is a dry-run "
                         "report). Reversible: sets is_active=False, not DELETE.")
    a = ap.parse_args()
    if a.min_age_days < 7:
        log.warning("min-age-days %d is very low; you'll prune companies that "
                    "haven't had a fair crawl window.", a.min_age_days)
    one_pass(a.min_age_days, a.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
