"""
scripts/seed_yc_directory.py
----------------------------
Pull the yc-oss/api mirror of the YC company directory (~6k companies,
updated daily from YC's own site) and seed any active US-relevant
companies we don't already have.

Fetch -> filter (Active + US/Remote + has http website) -> hand each
website URL to seed_yc._probe (fingerprint_ats). Idempotent.
Meant to run daily from discovery_loop.py.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile

import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app.utils.logging import get_logger  # noqa: E402

log = get_logger("seed_yc_directory")

FEED_URL = "https://raw.githubusercontent.com/yc-oss/api/main/companies/all.json"


def run(workers: int = 8) -> dict:
    """Fetch YC feed -> CSV in tmpdir -> hand off to seed_yc.main via CLI-style."""
    log.info("YC: fetching directory feed from yc-oss/api")
    try:
        data = requests.get(FEED_URL, timeout=30).json()
    except Exception as e:  # noqa: BLE001
        log.warning("YC: feed fetch failed: %s", e)
        return {"fetched": 0, "new": 0, "error": str(e)}

    us_active = [
        c for c in data
        if c.get("status") == "Active"
        and (c.get("website") or "").startswith("http")
        and (
            any("United States" in r or "Remote" in r for r in (c.get("regions") or []))
            or "USA" in (c.get("all_locations") or "")
            or "United States" in (c.get("all_locations") or "")
        )
    ]
    log.info("YC: %d active US/Remote companies with a website", len(us_active))

    tmpdir = tempfile.mkdtemp(prefix="yc_seed_")
    csv_path = os.path.join(tmpdir, "yc_us_active.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "website", "batch", "industry"])
        for c in us_active:
            w.writerow([c.get("name", ""), c.get("website", ""),
                        c.get("batch", ""), c.get("industry", "")])

    # Delegate to seed_yc.main via argv so the fingerprint + insert logic stays
    # in ONE place (that we already validated tonight).
    import seed_yc
    old_argv = sys.argv
    try:
        sys.argv = ["seed_yc.py", "--src", csv_path, "--priority", "medium",
                    "--workers", str(workers)]
        seed_yc.main()
    finally:
        sys.argv = old_argv
    return {"fetched": len(us_active), "csv": csv_path}


if __name__ == "__main__":
    run()
