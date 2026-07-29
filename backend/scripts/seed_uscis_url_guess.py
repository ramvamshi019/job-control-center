"""
scripts/seed_uscis_url_guess.py
-------------------------------
Second-pass USCIS discovery: for every named employer, guess a website URL
from the name, fetch it + `/careers`, and run HTML ATS fingerprinting.

Where seed_h1b_sponsors.py does name-token probing against 9 ATS APIs
(effective for standard tenant-slug ATSes like Greenhouse / Lever), THIS
script catches the Fortune-500-tenant type of ATS that names can't guess:
Workday / iCIMS / Eightfold hidden behind `careers.example.com`, embedded
via a script tag on the company's real careers page.

The hit rate is bounded by two things:
    1. Does `{squashed_name}.com` resolve to the real company site? For
       well-known companies yes; for LLC-style USCIS entries, ~20-30%.
    2. Does the careers page embed a detectable ATS script? ~30-50% of
       resolving sites hit one of our 12 fingerprint patterns.
Combined expected yield on 40-45k names: 500-1500 net new companies.

Runs detached on the droplet:
    docker exec -w /app/backend -d job-control-center-backend-1 sh -c \\
      'python scripts/seed_uscis_url_guess.py \\
         --src /tmp/uscis_fy2026.csv --priority medium --workers 24 \\
         > /tmp/uscis_url_guess.log 2>&1'

Idempotent: skips any name already in the roster and any (ats, token) pair
already known -- exactly like auto_discover. Safe to re-run.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import islice as _islice
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from sqlmodel import select  # noqa: E402

from app.database import session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402
# Reuse the SAME fingerprint patterns auto_discover already uses so this
# script's hits are indistinguishable from any other discovery pass.
from auto_discover import fingerprint_ats  # noqa: E402
from enrich_h1b import norm, score_for  # noqa: E402

log = get_logger("seed_uscis_url_guess")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "JobControlCenter/1.0 (+personal-job-search; url-guess)",
})
SESSION.mount("https://", HTTPAdapter(pool_connections=64, pool_maxsize=64))
SESSION.mount("http://", HTTPAdapter(pool_connections=64, pool_maxsize=64))
CHECK_TIMEOUT = 6

# Common approval columns for the FY2026 export.
_APPROVAL_COLS = (
    "New Employment Approval", "Continuation Approval",
    "Change with Same Employer Approval", "New Concurrent Approval",
    "Change of Employer Approval", "Amended Approval",
)


def _guess_hostnames(display_name: str) -> list[str]:
    """Guess plausible root hostnames from an employer name.

    Deliberately conservative -- tries 3-4 variants max per name. USCIS
    names have a lot of trailing "LLC / INC / CORP / SERVICES" garbage;
    the squasher below strips those before trying `.com`.
    """
    n = (display_name or "").lower()
    # Drop common legal suffixes.
    n = re.sub(r"\b(inc|corp|corporation|company|co|llc|l l c|ltd|limited|"
               r"holdings|services|solutions|group|plc|the)\b", " ", n)
    n = re.sub(r"[^a-z0-9 ]+", " ", n).strip()
    words = [w for w in n.split() if w]
    if not words:
        return []
    squashed = "".join(words)
    hyphen = "-".join(words)
    firstword = words[0]
    out = []
    if 3 <= len(squashed) <= 32:
        out.append(f"https://{squashed}.com")
    if hyphen != squashed and 3 <= len(hyphen) <= 40:
        out.append(f"https://{hyphen}.com")
    if firstword != squashed and 4 <= len(firstword) <= 24:
        out.append(f"https://{firstword}.com")
    # Also try `.io` for tech-heavy names.
    if 3 <= len(squashed) <= 20:
        out.append(f"https://{squashed}.io")
    # Dedupe, preserve order.
    seen = set()
    return [u for u in out if not (u in seen or seen.add(u))]


def _site_is_real(resp: requests.Response) -> bool:
    """Filter out parked domains, ad landing pages, DNS-provider placeholders."""
    if resp.status_code >= 400:
        return False
    body = resp.text.lower()
    if len(body) < 400:  # near-empty parked pages
        return False
    parked_markers = (
        "buy this domain", "domain is for sale", "this domain is parked",
        "godaddy.com/domainsearch", "sedoparking.com",
        "dan.com", "hugedomains.com", "namecheap.com/parkedpage",
    )
    return not any(m in body for m in parked_markers)


def _probe_one(name: str, appr: int) -> dict | None:
    """Return {'name','ats','career_url','approvals','via'} on first hit,
    or None. Best-effort: any transport / TLS failure just moves on."""
    for url in _guess_hostnames(name):
        try:
            resp = SESSION.get(url, timeout=CHECK_TIMEOUT, allow_redirects=True)
        except Exception:
            continue
        if not _site_is_real(resp):
            continue
        # Feed the resolved URL (post-redirect) to the shared fingerprint pass;
        # it also probes /careers, /jobs, etc.
        r = fingerprint_ats(resp.url)
        if r:
            ats, token = r
            return {
                "name": name.strip(),
                "ats": ats,
                "career_url": token,
                "approvals": appr,
                "via": url,
            }
    return None


def load_usc_rows(path: str) -> list[tuple[str, int]]:
    """[(display_name, total_approvals)] deduped by normalized name, sorted
    high-approval-first so if we run out of time we've hit the biggest fish."""
    approvals: dict[str, int] = {}
    display: dict[str, str] = {}
    with open(path, newline="", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        cols = [c for c in _APPROVAL_COLS if c in (reader.fieldnames or [])]
        if not cols:
            raise SystemExit(f"{path} lacks USCIS approval columns.")
        name_col = "Employer (Petitioner) Name" if "Employer (Petitioner) Name" in (reader.fieldnames or []) else "employer_name"
        for r in reader:
            raw = (r.get(name_col) or "").strip()
            key = norm(raw)
            if not key:
                continue
            a = 0
            for col in cols:
                try:
                    a += int(float(r.get(col) or 0))
                except (TypeError, ValueError):
                    pass
            approvals[key] = approvals.get(key, 0) + a
            display.setdefault(key, raw)
    return sorted(
        [(display[k], approvals[k]) for k in approvals],
        key=lambda t: -t[1],
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="USCIS FY export CSV")
    ap.add_argument("--priority", default="medium",
                    choices=["high", "medium", "low"],
                    help="tier to insert hits at. Default medium (6h re-crawl).")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--limit", type=int, default=0,
                    help="cap employers probed (testing).")
    ap.add_argument("--min-approvals", type=int, default=1,
                    help="only probe employers with >=N total approvals")
    args = ap.parse_args()

    rows = load_usc_rows(args.src)
    log.info("read %d distinct USCIS employers", len(rows))

    # Only skip companies we ALREADY have with a real board (career_url set).
    # Inactive reference rows (added by USCIS bulk import, no career_url yet)
    # are still fair game -- when this script detects an ATS for them, it
    # ACTIVATES the existing row instead of inserting a duplicate.
    with session_scope() as s:
        cos = s.exec(select(Company)).all()
        # name -> Company for update-on-hit lookup
        name_to_company: dict[str, Company] = {norm(c.name): c for c in cos}
        known_boards = {
            (c.ats_type or "", (c.career_url or "").strip().lower())
            for c in cos if c.career_url
        }
        # Skip names we've already probed AND resolved to a real board.
        already_resolved = {
            norm(c.name) for c in cos
            if c.career_url and c.ats_type and c.is_active
        }

    todo = [(name, appr) for name, appr in rows
            if appr >= args.min_approvals and norm(name) not in already_resolved]
    if args.limit:
        todo = todo[:args.limit]
    log.info("probing %d new names for URL+ATS (skipping %d already known)",
             len(todo), len(rows) - len(todo))

    started = time.time()
    hits: list[dict] = []
    done = 0
    # BOUNDED WINDOW pattern -- earlier version built all 40k futures upfront
    # ({ex.submit(...) for ...}), which held each name's (session ref + response
    # buffers as it landed) in memory simultaneously and blew a 1.5G container
    # cgroup at 48 workers. Instead we keep a rolling window sized to workers*4:
    # at any moment ~64 futures pending, ~1MB each = ~64MB. Same total probes,
    # same throughput, no OOM.
    from concurrent.futures import FIRST_COMPLETED, wait
    it = iter(todo)
    window_size = args.workers * 4
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        pending = {ex.submit(_probe_one, n, a) for n, a in _islice(it, window_size)}
        while pending:
            just_done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in just_done:
                done += 1
                try:
                    res = fut.result()
                except Exception:
                    res = None
                if res and (res["ats"], res["career_url"].lower()) not in known_boards:
                    hits.append(res)
                    known_boards.add((res["ats"], res["career_url"].lower()))
                if done % 500 == 0:
                    elapsed = time.time() - started
                    rate = done / max(1, elapsed)
                    remain = (len(todo) - done) / max(0.1, rate)
                    log.info("  %d/%d probed, %d hits (%.1f/s, ~%.0f min left)",
                             done, len(todo), len(hits), rate, remain / 60)
                # Top up: keep the window full while more work remains
                nxt = next(it, None)
                if nxt is not None:
                    pending.add(ex.submit(_probe_one, *nxt))

    log.info("probe complete: %d/%d names -> %d ATS hits", done, len(todo), len(hits))

    # Insert in batches.
    if not hits:
        log.info("no new hits, nothing to write.")
        return 0
    stamp = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
    inserted = 0
    activated = 0
    with session_scope() as s:
        # Re-read the name->company map fresh; ORM sessions don't share instances.
        cos = s.exec(select(Company)).all()
        by_name = {norm(c.name): c for c in cos}
        pending: list = []
        for h in hits:
            existing = by_name.get(norm(h["name"]))
            if existing is not None and not existing.is_active:
                # ACTIVATE the reference row we already had.
                existing.career_url = h["career_url"]
                existing.ats_type = h["ats"]
                existing.priority = args.priority
                existing.is_active = True
                existing.h1b_history_score = max(
                    existing.h1b_history_score or 0, score_for(h["approvals"]))
                existing.notes = (
                    (existing.notes or "")
                    + f" | url-guess activated {stamp} via {h['via']}"
                ).strip(" |")
                s.add(existing)
                activated += 1
            elif existing is not None:
                # Already active on a different board -- skip, don't dup.
                continue
            else:
                pending.append(Company(
                    name=h["name"], career_url=h["career_url"], ats_type=h["ats"],
                    h1b_history_score=score_for(h["approvals"]),
                    priority=args.priority, is_active=True,
                    notes=f"url-guess seeded {stamp}; {h['approvals']} approvals; "
                          f"detected via {h['via']}",
                ))
        if pending:
            s.add_all(pending); inserted += len(pending)
        s.commit()
    log.info("ACTIVATED %d existing reference rows, INSERTED %d new -- both at priority=%s",
             activated, inserted, args.priority)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
