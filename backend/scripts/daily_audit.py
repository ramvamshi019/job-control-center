"""
scripts/daily_audit.py
----------------------
Daily health & dead-weight audit. Report-only -- writes JSON + a text summary
under data/reports/. Never touches the DB.

Runs from cron. Compares today's counts against the most-recent prior report so
each run shows deltas (jobs discovered, dead-weight resolved, DB growth). The
persistent report file is what makes the "prune ready" list stable across days:
a company only appears there after it has been dead for --min-age-days AND its
ATS endpoint returned 0 items on the last crawl (verified via
last_checked_at + is_active). Filling those criteria over N daily reports is
what lets a later prune step trust the list.

    docker exec -w /app/backend job-control-center-backend-1 \\
        python scripts/daily_audit.py

Writes:
    data/reports/audit-YYYY-MM-DD.json   (machine-readable snapshot)
    data/reports/audit-YYYY-MM-DD.txt    (human-readable, same info)
    data/reports/latest.json / latest.txt (symlinked to today's)
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings  # noqa: E402
from app.services.scheduler import PRIORITY_INTERVALS  # noqa: E402

# The 54k/day figure the fd7fd84 commit was tuned against. Real capacity is
# higher post 4-vCPU resize; kept as a headline for the report.
CAPACITY_PER_DAY = 54_000

REPORT_DIR = Path("data/reports")
DB_PATH = "data/db/jobs.db"


def scans_per_day(priority: str) -> float:
    iv = PRIORITY_INTERVALS.get(priority)
    return 0.0 if not iv else 86_400.0 / iv.total_seconds()


def _q(c, sql, params=()):
    return c.execute(sql, params).fetchall()


def _load_prior(today: str) -> dict | None:
    """Most recent prior audit that isn't today's, for delta reporting."""
    files = sorted(REPORT_DIR.glob("audit-*.json"))
    for p in reversed(files):
        if today not in p.name:
            try:
                return json.loads(p.read_text())
            except Exception:
                continue
    return None


def collect(today_iso: str) -> dict:
    c = sqlite3.connect(DB_PATH)
    r: dict = {
        "date": today_iso,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "config": {
            "prune_days": settings.prune_days,
            "sponsor_prune_days": settings.sponsor_prune_days,
            "sponsor_score_threshold": settings.sponsor_score_threshold,
        },
    }

    # --- Roster ---
    tiers = dict(_q(c, "SELECT priority, COUNT(*) FROM companies "
                       "WHERE is_active=1 GROUP BY priority"))
    r["roster"] = {
        "active_total": sum(tiers.values()),
        "inactive_total": _q(c, "SELECT COUNT(*) FROM companies "
                                "WHERE is_active=0")[0][0],
        "by_tier": tiers,
    }

    # --- Capacity ---
    demand = sum(scans_per_day(p) * n for p, n in tiers.items())
    r["capacity"] = {
        "demand_per_day": round(demand),
        "reference_ceiling": CAPACITY_PER_DAY,
        "subscription_pct": round(100 * demand / CAPACITY_PER_DAY, 1),
    }

    # --- Discovery (jobs added in the last 24 hours) ---
    (n24,) = _q(c, "SELECT COUNT(*) FROM jobs "
                   "WHERE discovered_at > datetime('now','-1 day')")[0]
    (n_new,) = _q(c, "SELECT COUNT(*) FROM jobs "
                     "WHERE discovered_at > datetime('now','-1 day') "
                     "AND status='New'")[0]
    (n_review,) = _q(c, "SELECT COUNT(*) FROM jobs "
                        "WHERE discovered_at > datetime('now','-1 day') "
                        "AND status='Need Review'")[0]
    (n_reject,) = _q(c, "SELECT COUNT(*) FROM jobs "
                        "WHERE discovered_at > datetime('now','-1 day') "
                        "AND status='Rejected'")[0]
    by_ats = _q(c, """SELECT co.ats_type, COUNT(j.id)
                        FROM jobs j JOIN companies co ON co.id = j.company_id
                       WHERE j.discovered_at > datetime('now','-1 day')
                       GROUP BY co.ats_type ORDER BY 2 DESC""")
    r["discovery_24h"] = {
        "total": n24,
        "new": n_new,
        "need_review": n_review,
        "rejected": n_reject,
        "by_ats": dict(by_ats),
    }

    # --- Dead-weight: active-low, never returned a job ---
    # LEFT JOIN + IS NULL is cheaper on this table than NOT EXISTS.
    dead = _q(c, """
        SELECT co.ats_type,
               CASE
                 WHEN datetime(co.created_at) > datetime('now','-7 days')  THEN 'a_fresh_lt7'
                 WHEN datetime(co.created_at) > datetime('now','-30 days') THEN 'b_7_30'
                 WHEN datetime(co.created_at) > datetime('now','-60 days') THEN 'c_30_60'
                 ELSE 'd_gt60'
               END bucket,
               COUNT(*)
          FROM companies co
          LEFT JOIN (SELECT company_id, MAX(discovered_at) FROM jobs
                     GROUP BY company_id) j ON j.company_id = co.id
         WHERE co.is_active=1 AND co.priority='low' AND j.company_id IS NULL
         GROUP BY 1, 2
    """)
    dead_by_ats: dict[str, dict[str, int]] = {}
    for ats, bucket, cnt in dead:
        dead_by_ats.setdefault(ats or "(null)", {})[bucket] = cnt

    (dead_total,) = _q(c, """SELECT COUNT(*) FROM companies co
        LEFT JOIN (SELECT company_id FROM jobs GROUP BY company_id) j
               ON j.company_id = co.id
        WHERE co.is_active=1 AND co.priority='low' AND j.company_id IS NULL""")[0]

    # Prune-ready = the confidence bucket: >60 days in roster, never returned a
    # job, AND crawled within the last 24 h (so we know the endpoint is dead
    # NOW, not just dormant). Nothing is deactivated here -- this is the list
    # a future prune step should use.
    (ready,) = _q(c, """
        SELECT COUNT(*) FROM companies co
        LEFT JOIN (SELECT company_id FROM jobs GROUP BY company_id) j
               ON j.company_id = co.id
        WHERE co.is_active=1 AND co.priority='low' AND j.company_id IS NULL
          AND datetime(co.created_at) < datetime('now','-60 days')
          AND datetime(co.last_checked_at) > datetime('now','-1 day')
    """)[0]

    r["dead_weight"] = {
        "total": dead_total,
        "prune_ready_confident": ready,
        "by_ats_and_age": dead_by_ats,
    }

    # --- Sponsor coverage ---
    r["sponsors"] = {
        "confirmed_total": _q(c, "SELECT COUNT(*) FROM companies "
                                 "WHERE is_active=1 AND h1b_history_score >= "
                                 f"{settings.sponsor_score_threshold}")[0][0],
        "high_tier_sponsors": _q(c, "SELECT COUNT(*) FROM companies "
                                    "WHERE is_active=1 AND priority='high' "
                                    "AND h1b_history_score >= "
                                    f"{settings.sponsor_score_threshold}")[0][0],
        "misplaced_below_high": _q(c, "SELECT COUNT(*) FROM companies "
                                      "WHERE is_active=1 AND priority IN "
                                      "('low','medium') AND h1b_history_score "
                                      f">= {settings.sponsor_score_threshold}"
                                      )[0][0],
    }

    # --- Top rejection reasons in last 24 h ---
    top_rej = _q(c, """SELECT rejection_reason, COUNT(*) FROM jobs
                        WHERE status='Rejected'
                          AND discovered_at > datetime('now','-1 day')
                        GROUP BY rejection_reason ORDER BY 2 DESC LIMIT 10""")
    r["top_rejections_24h"] = [{"reason": (a or "(null)")[:120], "count": b}
                                for a, b in top_rej]

    # --- Storage / host stats (best-effort, container-only) ---
    try:
        db_size = os.path.getsize(DB_PATH)
    except OSError:
        db_size = None
    r["storage"] = {
        "db_size_bytes": db_size,
        "db_size_mb": round(db_size / 1024 / 1024, 1) if db_size else None,
    }
    try:
        out = subprocess.check_output(["df", "-Ph", DB_PATH],
                                       text=True).splitlines()[-1].split()
        r["storage"]["fs_size"], r["storage"]["fs_used"] = out[1], out[2]
        r["storage"]["fs_avail"], r["storage"]["fs_pct"] = out[3], out[4]
    except Exception:
        pass

    c.close()
    return r


def render_text(r: dict, prior: dict | None) -> str:
    def delta(cur, key_path):
        if not prior:
            return ""
        cur_ref = cur
        prev_ref = prior
        for k in key_path:
            cur_ref = cur_ref.get(k, {}) if isinstance(cur_ref, dict) else None
            prev_ref = prev_ref.get(k, {}) if isinstance(prev_ref, dict) else None
        if isinstance(cur_ref, (int, float)) and isinstance(prev_ref, (int, float)):
            d = cur_ref - prev_ref
            sign = "+" if d >= 0 else ""
            return f"  ({sign}{d:,} vs {prior['date']})"
        return ""

    lines = []
    lines.append(f"=== JCC daily audit  {r['date']} ===")
    lines.append(f"generated {r['generated_at']}Z\n")

    lines.append("[ CONFIG ]")
    lines.append(f"  prune_days           = {r['config']['prune_days']}  "
                 f"(sponsor: {r['config']['sponsor_prune_days']})")
    lines.append("")

    lines.append("[ ROSTER ]")
    lines.append(f"  active total  : {r['roster']['active_total']:6}"
                 + delta(r, ['roster', 'active_total']))
    lines.append(f"  inactive      : {r['roster']['inactive_total']:6}"
                 + delta(r, ['roster', 'inactive_total']))
    for tier, n in r['roster']['by_tier'].items():
        lines.append(f"    {tier:6} = {n:6}")
    lines.append("")

    cap = r['capacity']
    lines.append("[ CAPACITY ]")
    lines.append(f"  demand   = {cap['demand_per_day']:,}/day"
                 + delta(r, ['capacity', 'demand_per_day']))
    lines.append(f"  ceiling  ~ {cap['reference_ceiling']:,}/day  "
                 f"({cap['subscription_pct']:.1f}% subscribed)")
    lines.append("")

    d = r['discovery_24h']
    lines.append("[ DISCOVERY (last 24h) ]")
    lines.append(f"  total jobs discovered : {d['total']:6}"
                 + delta(r, ['discovery_24h', 'total']))
    lines.append(f"    -> New (best)       : {d['new']:6}"
                 + delta(r, ['discovery_24h', 'new']))
    lines.append(f"    -> Need Review      : {d['need_review']:6}"
                 + delta(r, ['discovery_24h', 'need_review']))
    lines.append(f"    -> Rejected         : {d['rejected']:6}"
                 + delta(r, ['discovery_24h', 'rejected']))
    lines.append("  by ATS:")
    for ats, n in sorted(d['by_ats'].items(), key=lambda kv: -kv[1])[:12]:
        lines.append(f"    {(ats or '(null)'):15} {n:6}")
    lines.append("")

    dw = r['dead_weight']
    lines.append("[ DEAD-WEIGHT (active low-tier, 0 jobs ever) ]")
    lines.append(f"  total dead              : {dw['total']:6}"
                 + delta(r, ['dead_weight', 'total']))
    lines.append(f"  prune-ready (>60d old,  : {dw['prune_ready_confident']:6}"
                 + delta(r, ['dead_weight', 'prune_ready_confident']))
    lines.append("      still 0 jobs after live-crawl in last 24h)")
    lines.append("  by ATS x age:")
    lines.append(f"    {'ATS':15}  {'<7d':>6} {'7-30':>6} {'30-60':>6} {'>60':>6}")
    for ats, buckets in sorted(dw['by_ats_and_age'].items(),
                                key=lambda kv: -sum(kv[1].values())):
        a = buckets.get('a_fresh_lt7', 0)
        b = buckets.get('b_7_30', 0)
        cc = buckets.get('c_30_60', 0)
        dd = buckets.get('d_gt60', 0)
        lines.append(f"    {ats:15}  {a:6} {b:6} {cc:6} {dd:6}")
    lines.append("")

    sp = r['sponsors']
    lines.append("[ SPONSORS ]")
    lines.append(f"  confirmed (score>=50)      : {sp['confirmed_total']:5}"
                 + delta(r, ['sponsors', 'confirmed_total']))
    lines.append(f"    in high tier             : {sp['high_tier_sponsors']:5}"
                 + delta(r, ['sponsors', 'high_tier_sponsors']))
    lines.append(f"    misplaced (low/medium)   : {sp['misplaced_below_high']:5}"
                 " -- run promote_sponsors.py if > 0")
    lines.append("")

    lines.append("[ TOP REJECTIONS (24h) ]")
    for row in r['top_rejections_24h']:
        lines.append(f"  {row['count']:6}  {row['reason']}")
    lines.append("")

    st = r['storage']
    lines.append("[ STORAGE ]")
    lines.append(f"  DB size            : {st.get('db_size_mb','?')} MB"
                 + delta(r, ['storage', 'db_size_bytes']))
    if 'fs_size' in st:
        lines.append(f"  volume            : {st['fs_used']} used of "
                     f"{st['fs_size']} ({st['fs_pct']}), {st['fs_avail']} free")
    return "\n".join(lines)


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.utcnow().strftime("%Y-%m-%d")

    prior = _load_prior(today)
    r = collect(today)
    text = render_text(r, prior)

    (REPORT_DIR / f"audit-{today}.json").write_text(json.dumps(r, indent=2))
    (REPORT_DIR / f"audit-{today}.txt").write_text(text)
    # "latest" pointers are a plain copy, not symlink, so a docker-cp'd volume
    # or NFS mount doesn't break the reference.
    shutil.copy(REPORT_DIR / f"audit-{today}.json", REPORT_DIR / "latest.json")
    shutil.copy(REPORT_DIR / f"audit-{today}.txt", REPORT_DIR / "latest.txt")

    # Trim reports older than 90 days -- if history matters longer than that,
    # copy them out; the reports/ dir is intentionally not a permanent archive.
    horizon = datetime.utcnow() - timedelta(days=90)
    for p in REPORT_DIR.glob("audit-*"):
        try:
            d = datetime.strptime(p.name.split("-", 1)[1][:10], "%Y-%m-%d")
            if d < horizon:
                p.unlink()
        except Exception:
            pass

    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
