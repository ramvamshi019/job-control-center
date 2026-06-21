"""
scripts/reassess_sponsorship.py
-------------------------------
Catch "we don't sponsor / citizenship required / clearance" jobs that slipped
into your good-fit lists because the disclosure was TRUNCATED out of the stored
description (the engine only ever saw the first ~6000 chars, and these notices
live at the very bottom of a posting).

This re-FETCHES the live job page, scans the FULL text with the sponsorship
engine's reject rules, and rejects any job that is actually a dead-end for an
F-1/OPT candidate who needs sponsorship.

It is read-only on compute, then writes status flips via id-targeted UPDATEs
(crawler-safe — a row pruned mid-run just matches 0 rows). Manually-actioned
jobs (Approved / Applied / Follow-up) are NEVER touched.

Run from the backend/ folder:
    python scripts/reassess_sponsorship.py                  # New good-fit only
    python scripts/reassess_sponsorship.py --status "New,Need Review" --limit 2000
    python scripts/reassess_sponsorship.py --dry-run        # report, write nothing
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import requests  # noqa: E402
from sqlalchemy import func  # noqa: E402
from sqlalchemy import update as sa_update  # noqa: E402
from sqlmodel import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import init_db, session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.models.job import Job  # noqa: E402
from app.services import sponsorship_engine  # noqa: E402
from app.services.sponsorship_engine import REJECT_PHRASES, no_sponsorship  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402
from app.utils.text import clean_html, normalize  # noqa: E402

log = get_logger("reassess_sponsorship")

PROTECTED = {"Approved", "Applied", "Follow-up"}


def fetch_full_text(url: str) -> str | None:
    """GET the live posting and return its visible text, or None on failure."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": settings.user_agent},
            timeout=settings.request_timeout_seconds,
        )
        resp.raise_for_status()
    except Exception:  # noqa: BLE001 - unreachable/moved postings are just skipped
        return None
    return clean_html(resp.text)


def _strip_questions(text: str) -> str:
    """Drop interrogative clauses (anything ending in '?'). The live page bundles
    the JOB DESCRIPTION (declarative employer disclosures, end in '.') with the
    APPLICATION FORM's screening QUESTIONS ("Are you authorized to work without
    sponsorship?", "Will you now or in the future require sponsorship?") which are
    NEUTRAL — asked of everyone, even by companies that DO sponsor. Scanning them
    would wrongly reject sponsor-friendly jobs. Disclosures we want end in '.'."""
    return re.sub(r"[^.!?]*\?", " ", text)


def blocker_in(title: str, full_text: str) -> str | None:
    """Return the blocker snippet if the FULL posting text is a dead-end. Only
    declarative disclosure text is scanned — form questions are stripped first."""
    hay = normalize(title) + " " + normalize(_strip_questions(full_text))
    for p in REJECT_PHRASES:
        if p in hay:
            return f"citizenship/clearance/no-sponsor: '{p}'"
    snippet = no_sponsorship(hay)
    if snippet:
        return f"no visa sponsorship: '{snippet}'"
    return None


_NOTE_MARK = " [work-authorization notice]"


def recheck_rejected(args) -> None:
    """Re-verify jobs THIS script rejected (rejection_reason starts with
    'Re-verified live:'). Restore any the improved engine now finds clean —
    stripping the disclosure note we appended and recomputing their risk."""
    init_db()
    with session_scope() as session:
        rows = session.exec(
            select(Job.id, Job.title, Job.job_url, Job.description, Job.company_id)
            .where(Job.rejection_reason.like("Re-verified live:%"))
        ).all()
        companies = {c.id: c for c in session.exec(select(Company)).all()}
    print(f"Re-checking {len(rows)} previously-rejected job(s)…")

    def recheck(row):
        jid, title, url, desc, cid = row
        text = fetch_full_text(url) if url else None
        if text is None:
            return ("unreachable", jid, title, desc, cid, None)
        snip = blocker_in(title or "", text)
        return ("blocked" if snip else "clean", jid, title, desc, cid, snip)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        results = list(ex.map(recheck, rows))

    restore = [r for r in results if r[0] == "clean"]
    still = sum(1 for r in results if r[0] == "blocked")
    unreach = sum(1 for r in results if r[0] == "unreachable")
    print(f"  still blocked: {still}   unreachable(kept): {unreach}   RESTORE: {len(restore)}\n")
    for _, jid, title, _, _, _ in restore:
        print(f"  restore [{jid}] {(title or '')[:50]}")

    if args.dry_run or not restore:
        print("\n(dry-run or nothing to restore: no changes written)" if args.dry_run else "")
        return

    for _, jid, title, desc, cid, _ in restore:
        clean_desc = (desc or "").rsplit(_NOTE_MARK, 1)[0].rstrip()
        probe = Job(title=title or "", description=clean_desc)
        risk, reason = sponsorship_engine.assess(probe, companies.get(cid))
        status = "Rejected" if risk == "reject" else "New"
        with session_scope() as session:
            session.execute(
                sa_update(Job).where(Job.id == jid).values(
                    status=status,
                    rejection_reason="" if status != "Rejected" else reason,
                    sponsorship_risk=risk,
                    risk_reason=reason,
                    description=clean_desc,
                )
            )
    log.info("Restored %d job(s) to active.", len(restore))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", default="New",
                    help='comma-separated statuses to scan (default "New")')
    ap.add_argument("--limit", type=int, default=0, help="cap jobs scanned (0=all)")
    ap.add_argument("--min-score", type=int, default=None,
                    help="only scan jobs with match_score >= this (default: min_good_score)")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--recheck-rejected", action="store_true",
                    help="re-verify jobs this script previously rejected; restore "
                         "any that the (improved) engine now finds clean")
    args = ap.parse_args()

    if args.recheck_rejected:
        recheck_rejected(args)
        return

    statuses = [s.strip() for s in args.status.split(",") if s.strip()]
    min_score = args.min_score if args.min_score is not None else settings.min_good_score
    init_db()

    # --- Pass 1: gather candidates (id, title, url) ---
    with session_scope() as session:
        q = select(Job.id, Job.title, Job.job_url, Job.status).where(
            Job.status.in_(statuses), Job.match_score >= min_score
        ).order_by(Job.match_score.desc())
        rows = [r for r in session.exec(q).all() if r[3] not in PROTECTED and r[2]]
    if args.limit:
        rows = rows[: args.limit]
    log.info("Scanning %d job(s) (status=%s, score>=%d)…", len(rows), statuses, min_score)

    # --- Pass 2: fetch + check concurrently (network only, no DB held) ---
    def check(row):
        jid, title, url, _ = row
        text = fetch_full_text(url)
        if text is None:
            return ("unreachable", jid, title, url, None)
        snip = blocker_in(title or "", text)
        return ("blocked", jid, title, url, snip) if snip else ("clean", jid, title, url, None)

    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for i, res in enumerate(ex.map(check, rows), 1):
            results.append(res)
            if i % 100 == 0:
                log.info("  …%d/%d fetched", i, len(rows))

    blocked = [r for r in results if r[0] == "blocked"]
    unreachable = sum(1 for r in results if r[0] == "unreachable")
    clean = sum(1 for r in results if r[0] == "clean")

    print(f"\n=== reassess sponsorship: {len(rows)} scanned ===")
    print(f"  BLOCKED (dead-end, will reject): {len(blocked)}")
    print(f"  clean: {clean}   unreachable: {unreachable}\n")
    for _, jid, title, url, snip in blocked[:60]:
        print(f"  [{jid}] {(title or '')[:46]:46} {snip}")
    if len(blocked) > 60:
        print(f"  …and {len(blocked) - 60} more")

    if args.dry_run:
        print("\n(dry-run: no changes written)")
        return

    # --- Pass 3: reject blocked jobs via id-targeted UPDATEs ---
    ids = [jid for _, jid, _, _, _ in blocked]
    reasons = {jid: snip for _, jid, _, _, snip in blocked}
    written = 0
    BATCH = 500
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        try:
            with session_scope() as session:
                for jid in chunk:
                    # Append the disclosure to the stored description too, so a
                    # future rescore_all (which reads STORED text) re-catches it
                    # instead of resurrecting the dead-end from a truncated JD.
                    note = f" [work-authorization notice] {reasons[jid]}"
                    session.execute(
                        sa_update(Job).where(Job.id == jid).values(
                            status="Rejected",
                            rejection_reason=f"Re-verified live: {reasons[jid]}",
                            sponsorship_risk="reject",
                            risk_reason=reasons[jid],
                            description=func.coalesce(Job.description, "") + note,
                        )
                    )
            written += len(chunk)
        except Exception as exc:  # noqa: BLE001
            log.warning("batch %d failed: %s", i, exc)
    log.info("Rejected %d dead-end job(s).", written)


if __name__ == "__main__":
    main()
