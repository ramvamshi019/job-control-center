"""
scripts/registry_maintenance.py
-------------------------------
Weekly registry sweep: adaptive priority + auto-archive + lifecycle tally.

Called by the discovery container after harvest_company_sources.py and
auto_discover.py, so a single weekly pass covers:
    1. New companies discovered (via harvest + auto_discover)
    2. Existing companies re-tiered based on 30-day hiring activity
    3. Dead-weight companies archived (never DELETED — reactivation stays cheap)

Dry-run by default. Writes only with --apply. Prints a delta report you can
diff against last week's audit.

    docker exec -w /app/backend job-control-center-backend-1 \
        python scripts/registry_maintenance.py            # dry-run
    docker exec -w /app/backend job-control-center-backend-1 \
        python scripts/registry_maintenance.py --apply    # write
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database import session_scope  # noqa: E402
from app.services.registry import compute_deltas, registry_stats  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("registry_maintenance")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default is a dry-run report)")
    args = ap.parse_args()

    with session_scope() as s:
        # Before-snapshot.
        before = registry_stats(s)
        log.info("--- BEFORE ---")
        log.info("  active total : %d", before["active_total"])
        log.info("  archived     : %d", before["archived"])
        log.info("  by tier      : %s", before["by_tier"])
        log.info("  hiring 24h   : %d", before["companies_hiring_last_24h"])

        # Delta pass.
        delta = compute_deltas(s, dry_run=not args.apply)
        mode = "APPLY" if args.apply else "DRY RUN"
        log.info("--- %s DELTA ---", mode)
        log.info("  promoted    (-> higher tier) : %d", delta.promoted)
        log.info("  demoted     (-> lower tier)  : %d", delta.demoted)
        log.info("  archived    (is_active=False): %d", delta.archived)
        log.info("  reactivated                  : %d", delta.reactivated)
        log.info("  lifecycle counts             : %s", delta.lifecycle_counts)

        if not args.apply:
            log.info("(dry run: nothing written)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
