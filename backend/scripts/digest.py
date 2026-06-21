"""
scripts/digest.py
-----------------
Your "keep me updated" report: the freshest, best-fit, sponsor-viable jobs you
can apply to right now. Prints a ranked shortlist, writes it to logs/digest.md,
and fires a macOS desktop notification with the headline.

Run from backend/:
    ../.venv/bin/python scripts/digest.py [hours]   # default 24
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(__file__)
DB = os.path.join(HERE, "..", "data", "jobs.db")
OUT = os.path.join(HERE, "..", "..", "logs", "digest.md")

# DIRECT apply targets only — a real ATS form, no account, no Cloudflare wall,
# no aggregator redirect hop. EXCLUDES Himalayas/Remotive/Jobicy/RemoteOK/TheMuse/
# HN/YC (Cloudflare-gated or pure redirects to the employer) and Workday/iCIMS
# (require an account). These are the links you can actually apply on fast.
DIRECT_APPLY = ("greenhouse", "lever", "ashby", "smartrecruiters", "bamboohr",
                "rippling", "gem", "breezy", "workable", "recruitee", "jobvite",
                "eightfold")


def main() -> None:
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    win = f"-{hours} hours"

    ph = ",".join("?" * len(DIRECT_APPLY))
    fresh = con.execute(
        f"""SELECT match_score, source, title, company_name,
                  COALESCE(posted_at, discovered_at) AS dt, job_url
           FROM jobs
           WHERE status='New' AND sponsorship_risk IN ('low','medium')
             AND job_url <> ''
             AND source IN ({ph})
             AND discovered_at >= datetime('now', ?)
           ORDER BY match_score DESC, discovered_at DESC
           LIMIT 25""",
        (*DIRECT_APPLY, win),
    ).fetchall()

    total_good = con.execute(
        "SELECT COUNT(*) FROM jobs WHERE status IN ('New','Need Review')"
    ).fetchone()[0]
    new_24h = con.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='New' AND discovered_at >= datetime('now','-24 hours')"
    ).fetchone()[0]
    con.close()

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Job Control Center — digest {stamp}", "",
             f"- **{new_24h}** fresh good-fit jobs in the last 24h",
             f"- **{total_good}** good-fit jobs total in your pipeline", "",
             f"## Freshest good-fit (last {hours}h) — apply first", ""]
    if not fresh:
        lines.append("_No new good-fit jobs in this window — check back soon._")
    else:
        for r in fresh:
            lines.append(f"- **{r['title']}** — {r['company_name']} "
                         f"(score {r['match_score']}, {r['source']}, {str(r['dt'])[:10]})  \n  {r['job_url']}")

    report = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)

    # macOS desktop notification with the headline.
    top = f"{fresh[0]['title']} @ {fresh[0]['company_name']}" if fresh else "no new matches"
    msg = f"{new_24h} fresh good-fit jobs. Top: {top}".replace('"', "'")
    os.system(f'osascript -e \'display notification "{msg}" with title "Job Control Center"\' >/dev/null 2>&1')


if __name__ == "__main__":
    main()
