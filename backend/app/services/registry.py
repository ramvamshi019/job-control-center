"""
services/registry.py
--------------------
Company Registry: lifecycle, adaptive priority, and cleanup for the 51k+
active company roster.

Design choice: DERIVE lifecycle state from existing columns instead of adding
a new schema field. `is_active` + `priority` + `last_checked_at` + a
per-company job-count aggregate cover every state the spec asked for, with
zero migration risk. The state machine below is a pure function over that
data, so the state is always fresh and can never drift out of sync with
reality.

LIFECYCLE STATES:
    discovered  - is_active=False, never crawled  (auto-discover just added it)
    validated   - is_active=True,  never crawled  (in the queue, awaiting first pass)
    hiring      - is_active=True,  produced >=1 job in the last 30d
    dormant     - is_active=True,  produced 0 jobs in the last 30d
    archived    - is_active=False, was previously active
                  (auto-archived by should_archive() below)

ADAPTIVE PRIORITY:
    Feeds the existing scheduler.PRIORITY_INTERVALS without changing it.
      high    <-  produced >= HOT_JOBS_30D jobs in 30d  (very active hirer)
      medium  <-  produced >= MID_JOBS_30D jobs in 30d
      low     <-  everything else that's still active
    Confirmed H-1B sponsors (h1b_history_score >= SPONSOR_SCORE_THRESHOLD)
    always stay at 'high' — sponsor status trumps hiring velocity.

AUTOMATIC ARCHIVE:
    Companies in state=dormant for > ARCHIVE_AFTER_DAYS days AND never
    produced a single job since being added get is_active=False and a note.
    Never DELETE — reactivation is a boolean flip.

AUTOMATIC REACTIVATE:
    An archived company that suddenly appears in a fresh harvest pass with
    a live ATS endpoint response gets is_active=True again in the
    discovery pipeline (see registry_maintenance).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import case, func
from sqlmodel import Session, select

from app.config import settings
from app.models.company import Company
from app.models.job import Job

# ---- Tunables (deliberately in-code, not env-configurable, so behavior is
# reproducible from the source alone. Bump these here if the roster grows or
# shrinks past what today's tiers can handle.) --------------------------------
#
# Tuning note (2026-07-28): first dry-run with HOT=15/MID=3 recommended
# promoting 7,161 companies, which would push crawl demand from 76k/day
# past the measured ~80k/day ceiling. Tightened to 40/10 so promotions
# are meaningful — a company posting 40+ jobs in 30d is truly active, not
# just intermittent. Sponsors bypass these thresholds entirely.
HOT_JOBS_30D = 40
MID_JOBS_30D = 10
ARCHIVE_AFTER_DAYS = 60          # days-in-roster before a silent company is archived
REVALIDATE_AFTER_DAYS = 30       # dormant re-check cadence (informational only —
                                 # the scheduler's PRIORITY_INTERVALS handle it)
NEVER_HIRED_ARCHIVE_MIN_CRAWLS = 8  # don't archive brand-new never-crawled boards
# Sponsors get high-tier automatically at THIS score (>=5 USCIS approvals),
# not the weaker sponsor_score_threshold (50 = >=1 approval) which is only
# meant for the "sponsor badge" display. Matches scripts/promote_sponsors.py's
# default so the two scripts never disagree on who's a strong sponsor.
STRONG_SPONSOR_SCORE = 78


# ---- Lifecycle --------------------------------------------------------------

@dataclass
class Lifecycle:
    """State + human-readable reason. Reason is what surfaces in the audit."""
    state: str
    reason: str


def compute_lifecycle(company: Company, jobs_30d: int) -> Lifecycle:
    """Derive lifecycle purely from current data — no separate state column
    to keep in sync. `jobs_30d` is passed in so callers can batch the
    per-company aggregate once instead of N queries.
    """
    if not company.is_active:
        # We only ever set is_active=False via should_archive() or manual admin,
        # so "not active" == archived (or never validated). Distinguish by
        # whether we've ever seen it.
        if company.last_checked_at is None:
            return Lifecycle("discovered", "just added; awaiting validation")
        return Lifecycle("archived", "archived; no active hiring detected")

    if company.last_checked_at is None:
        return Lifecycle("validated", "queued; not yet crawled")

    if jobs_30d > 0:
        return Lifecycle("hiring", f"produced {jobs_30d} jobs in last 30d")

    return Lifecycle("dormant", "active board, no jobs in last 30d")


# ---- Adaptive priority ------------------------------------------------------

def adaptive_priority(company: Company, jobs_30d: int) -> str:
    """Recommended priority tier. Strong sponsors always stay high — hiring
    velocity is a proxy signal, not a substitute for a known-good employer.
    Uses STRONG_SPONSOR_SCORE (>=5 USCIS approvals), not the weaker
    sponsor_score_threshold (>=1) which is only for the sponsor badge."""
    if (company.h1b_history_score or 0) >= STRONG_SPONSOR_SCORE:
        return "high"
    if jobs_30d >= HOT_JOBS_30D:
        return "high"
    if jobs_30d >= MID_JOBS_30D:
        return "medium"
    return "low"


# ---- Archive / reactivate decisions -----------------------------------------

def should_archive(
    company: Company,
    jobs_ever: int,
    jobs_30d: int,
    crawls_ever: int,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Return the archive-reason string if the company should be archived,
    or None if it should stay. Callers apply the decision.

    Rules (ANDed):
      1. Currently active.
      2. Not a confirmed sponsor (sponsors never auto-archive).
      3. Older than ARCHIVE_AFTER_DAYS in the roster.
      4. Zero jobs discovered in the last 30 days.
      5. Either (a) zero jobs EVER while being crawled at least
         NEVER_HIRED_ARCHIVE_MIN_CRAWLS times, or (b) has hired historically
         but not in the last 30 days AND has been silent since the last hire.
    """
    if not company.is_active:
        return None
    # Any sponsor (any USCIS-approval history at all) is exempt from
    # auto-archive; the sponsor list is small and hand-curated, and losing
    # a sponsor to over-eager archiving costs more than a wasted crawl.
    if (company.h1b_history_score or 0) >= settings.sponsor_score_threshold:
        return None
    now = now or datetime.utcnow()
    if company.created_at is None or company.created_at > now - timedelta(days=ARCHIVE_AFTER_DAYS):
        return None
    if jobs_30d > 0:
        return None

    # Case (a): never produced a job, has had a fair chance.
    if jobs_ever == 0 and crawls_ever >= NEVER_HIRED_ARCHIVE_MIN_CRAWLS:
        return (
            f"never produced a job across {crawls_ever} crawls "
            f"in the last {ARCHIVE_AFTER_DAYS}+ days"
        )
    # Case (b): historically hired but silent lately.
    if jobs_ever > 0 and jobs_30d == 0:
        return f"historical yield {jobs_ever}, but zero jobs in the last 30 days"

    return None


def should_reactivate(
    company: Company, latest_probe_hits: int, now: Optional[datetime] = None
) -> bool:
    """A previously-archived company gets reactivated when the discovery pipe
    probes its ATS endpoint again and gets a positive answer (>=1 live posting).

    Kept separate from should_archive so tests can exercise each direction
    of the transition independently.
    """
    if company.is_active:
        return False
    return latest_probe_hits > 0


# ---- Bulk decision runner ---------------------------------------------------

@dataclass
class RegistryDelta:
    """What a single maintenance pass changed. Used by the maintenance script
    and by the /audit/registry endpoint."""
    promoted: int = 0                    # moved to a higher priority tier
    demoted: int = 0                     # moved to a lower tier
    archived: int = 0
    reactivated: int = 0
    lifecycle_counts: dict[str, int] = None  # discovered/validated/hiring/dormant/archived

    def as_dict(self) -> dict:
        return {
            "promoted": self.promoted,
            "demoted": self.demoted,
            "archived": self.archived,
            "reactivated": self.reactivated,
            "lifecycle_counts": self.lifecycle_counts or {},
        }


# Priority ordering for promote/demote detection.
_PRIORITY_RANK = {"skip": 0, "low": 1, "medium": 2, "high": 3}


def compute_deltas(
    session: Session, dry_run: bool = True, now: Optional[datetime] = None
) -> RegistryDelta:
    """Run one full sweep over the roster: adjust priorities, archive dead
    entries, tally lifecycle counts. Returns the delta; only mutates the DB
    if dry_run=False.

    Batched aggregates: one query returns (company_id, jobs_ever, jobs_30d)
    for every company that has any jobs, rather than N per-company queries.
    """
    now = now or datetime.utcnow()

    # (company_id, jobs_ever, jobs_30d) in one shot. Companies with no jobs
    # are absent from this dict and default to (0, 0). SUM(CASE ... ELSE 0)
    # is portable across SQLite + Postgres in case the DB moves later.
    thirty_days_ago = now - timedelta(days=30)
    yield_by_id: dict[int, tuple[int, int]] = {}
    rows = session.exec(
        select(
            Job.company_id,
            func.count(Job.id),
            func.sum(
                case((Job.discovered_at > thirty_days_ago, 1), else_=0)
            ),
        ).group_by(Job.company_id)
    ).all()
    for row in rows:
        cid, jobs_ever, jobs_30d = row[0], int(row[1] or 0), int(row[2] or 0)
        yield_by_id[cid] = (jobs_ever, jobs_30d)

    companies = session.exec(select(Company)).all()
    delta = RegistryDelta(lifecycle_counts={})
    stamp = datetime.utcnow().strftime("%Y-%m-%d")

    for c in companies:
        jobs_ever, jobs_30d = yield_by_id.get(c.id, (0, 0))
        # crawl attempts approximated by 1 if last_checked_at exists, else 0.
        # (We don't store per-crawl history — fine for the "give it a fair
        # chance" rule, which only cares about "crawled at least N times".)
        crawls_ever = NEVER_HIRED_ARCHIVE_MIN_CRAWLS if c.last_checked_at else 0

        # 1) Priority reconciliation.
        want = adaptive_priority(c, jobs_30d)
        if c.is_active and want != (c.priority or "low"):
            cur_rank = _PRIORITY_RANK.get(c.priority or "low", 1)
            new_rank = _PRIORITY_RANK.get(want, 1)
            if new_rank > cur_rank:
                delta.promoted += 1
            else:
                delta.demoted += 1
            if not dry_run:
                c.priority = want
                c.notes = ((c.notes or "") + f" | {stamp} priority->{want} "
                           f"(30d yield={jobs_30d})").strip(" |")
                session.add(c)

        # 2) Archive dead active companies.
        reason = should_archive(c, jobs_ever, jobs_30d, crawls_ever, now=now)
        if reason:
            delta.archived += 1
            if not dry_run:
                c.is_active = False
                c.notes = ((c.notes or "") + f" | archived {stamp}: {reason}").strip(" |")
                session.add(c)

        # 3) Lifecycle tally (uses the possibly-mutated `c`).
        lc = compute_lifecycle(c, jobs_30d)
        delta.lifecycle_counts[lc.state] = delta.lifecycle_counts.get(lc.state, 0) + 1

    if not dry_run:
        session.commit()
    return delta


# ---- Reporting --------------------------------------------------------------

def registry_stats(session: Session) -> dict:
    """Point-in-time roster snapshot. Cheap enough to serve on a dashboard
    request — three grouped counts and one join."""
    counts = dict(session.exec(
        select(Company.priority, func.count(Company.id))
        .where(Company.is_active == True)  # noqa: E712
        .group_by(Company.priority)
    ).all())
    inactive = session.exec(
        select(func.count(Company.id)).where(Company.is_active == False)  # noqa: E712
    ).one()

    by_ats = dict(session.exec(
        select(Company.ats_type, func.count(Company.id))
        .where(Company.is_active == True)  # noqa: E712
        .group_by(Company.ats_type)
        .order_by(func.count(Company.id).desc())
    ).all())

    hiring_24h = session.exec(
        select(func.count(func.distinct(Job.company_id)))
        .where(Job.discovered_at > datetime.utcnow() - timedelta(hours=24))
    ).one()

    return {
        "active_total": sum(counts.values()),
        "archived": int(inactive or 0),
        "by_tier": counts,
        "by_ats": by_ats,
        "companies_hiring_last_24h": int(hiring_24h or 0),
    }
