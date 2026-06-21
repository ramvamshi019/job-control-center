"""
scripts/seed_companies.py
-------------------------
Load companies from data/companies_seed.csv into the database.

Run from the backend/ folder:
    python scripts/seed_companies.py

Re-running is safe: a company with the same name + career_url is skipped.
"""

from __future__ import annotations

import csv
import os
import sys

# Make `app` importable when run as a script.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlmodel import select  # noqa: E402

from app.database import init_db, session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("seed")

SEED_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "companies_seed.csv")


def _to_int(value, default=0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_bool(value, default=True) -> bool:
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y"):
        return True
    if s in ("0", "false", "no", "n"):
        return False
    return default


def main() -> None:
    init_db()
    # Optional CSV path arg lets us load discovered_*.csv without touching the
    # primary seed file; defaults to data/companies_seed.csv.
    path = sys.argv[1] if len(sys.argv) > 1 else SEED_PATH
    if not os.path.exists(path):
        log.error("Seed file not found: %s", path)
        sys.exit(1)

    added = skipped = 0
    with session_scope() as session, open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("name") or "").strip()
            career_url = (row.get("career_url") or "").strip()
            ats_type = (row.get("ats_type") or "").strip().lower()
            if not name or not career_url:
                continue
            # Dedup on (name, url, ATS): the SAME company often runs boards on
            # more than one ATS (e.g. Greenhouse + SmartRecruiters). Each board
            # is a distinct crawlable source, so ats_type must be part of the key
            # — otherwise cross-probed new-ATS boards get wrongly skipped.
            exists = session.exec(
                select(Company).where(
                    Company.name == name,
                    Company.career_url == career_url,
                    Company.ats_type == ats_type,
                )
            ).first()
            if exists:
                skipped += 1
                continue
            session.add(
                Company(
                    name=name,
                    career_url=career_url,
                    ats_type=ats_type,
                    h1b_history_score=_to_int(row.get("h1b_history_score"), 0),
                    priority=(row.get("priority") or "medium").strip().lower(),
                    is_active=_to_bool(row.get("is_active"), True),
                    notes=(row.get("notes") or "").strip(),
                )
            )
            added += 1
    log.info("Seed complete. Added %d, skipped %d (already existed).", added, skipped)


if __name__ == "__main__":
    main()
