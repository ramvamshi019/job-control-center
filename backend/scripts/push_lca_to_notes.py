"""
scripts/push_lca_to_notes.py
----------------------------
Push company_lca fields into companies.notes as a JSON blob prefix so
the DASHBOARD can display them WITHOUT needing a backend rebuild.

Format prepended to each company's notes:
    [LCA:{"c":381,"p":266,"w":160805,"t":"Software Engineer"}] | <existing notes>

The dashboard has a small parser that extracts this and renders the
data in the Sponsors Watchlist grid. Idempotent -- replaces any existing
[LCA:...] prefix each run.

Runs standalone from backend container:
    docker exec -w /app/backend job-control-center-backend-1 \
        python scripts/push_lca_to_notes.py
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text  # noqa: E402
from app.database import engine  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("push_lca_to_notes")

_LCA_PREFIX_RE = re.compile(r"^\s*\[LCA:[^\]]*\]\s*\|?\s*", re.S)


def run() -> int:
    with engine.connect() as c:
        c.execute(text("PRAGMA busy_timeout = 30000"))
        rows = c.execute(text("""
            SELECT l.company_id, l.filings_current, l.filings_prior,
                   l.median_wage, l.top_titles, c.notes
            FROM company_lca l
            JOIN companies c ON c.id = l.company_id
        """)).all()
        updated = 0
        for cid, curr, prior, wage, titles, notes in rows:
            # Strip any existing [LCA:...] prefix from notes
            clean = _LCA_PREFIX_RE.sub("", notes or "")
            # Build the new prefix (compact JSON to keep the notes column readable)
            top = ""
            if titles:
                # Take just the first title (before " ; ")
                top = titles.split(";")[0].split("(")[0].strip()
            blob = {
                "c": curr or 0,
                "p": prior or 0,
                "w": wage or 0,
                "t": top[:60],
            }
            prefix = f"[LCA:{json.dumps(blob, separators=(',', ':'))}]"
            new_notes = (f"{prefix} | {clean}" if clean else prefix).strip()
            c.execute(
                text("UPDATE companies SET notes = :n WHERE id = :id"),
                {"n": new_notes, "id": cid},
            )
            updated += 1
        c.commit()
    log.info("push_lca_to_notes: updated %d companies", updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
