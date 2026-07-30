"""
scripts/fingerprint_recent.py
-----------------------------
Targeted fingerprint pass for companies seeded in the last 24h that
still don't have an ats_type. Avoids the full 1,300+ scan that
auto_discover would run — just processes the fresh additions from
seed_universities / seed_federal_labs / seed_vc_portfolios.

Sets ats_type + priority='high'/'medium' + is_active=1 for anything
where fingerprint_ats detects a known ATS. Silent skip for detected-
nothing (stays dormant, may reactivate later).
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import requests  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.database import engine  # noqa: E402
from app.utils.dates import utcnow_naive  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402
from auto_discover import fingerprint_ats  # noqa: E402

log = get_logger("fingerprint_recent")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "JobControlCenter/1.0 (+fingerprint-recent)"})
TIMEOUT = 8


def _fetch_final_url(url: str) -> str | None:
    """Follow redirects to the canonical URL fingerprint_ats will probe."""
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code >= 400 or len(r.text) < 200:
            return None
        return r.url
    except Exception:
        return None


def run() -> dict:
    cutoff = utcnow_naive() - timedelta(hours=24)
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, name, career_url FROM companies
            WHERE (ats_type IS NULL OR ats_type = '')
              AND career_url IS NOT NULL AND career_url != ''
              AND created_at >= :d
            ORDER BY created_at DESC
        """), {"d": cutoff}).all()
    log.info("fingerprint_recent: %d recent companies to probe", len(rows))
    if not rows:
        return {"probed": 0, "detected": 0}

    detected = 0
    for row in rows:
        cid, name, url = row.id, row.name, row.career_url
        final_url = _fetch_final_url(url)
        if not final_url:
            log.debug("skip %s: no reachable URL", name)
            continue
        result = fingerprint_ats(final_url)
        if not result:
            log.debug("skip %s: no ATS detected on %s", name, final_url)
            continue
        ats, career_url = result
        with engine.begin() as w:
            w.execute(text("""
                UPDATE companies
                SET ats_type = :a, career_url = :u, is_active = 1,
                    updated_at = :t
                WHERE id = :i
            """), {"a": ats, "u": career_url, "t": utcnow_naive(), "i": cid})
        detected += 1
        log.info("  detected %s → %s", name, ats)
    log.info("fingerprint_recent: %d/%d activated", detected, len(rows))
    return {"probed": len(rows), "detected": detected}


if __name__ == "__main__":
    raise SystemExit(0 if run() is not None else 1)
