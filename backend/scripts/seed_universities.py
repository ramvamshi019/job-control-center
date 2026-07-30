"""
scripts/seed_universities.py
----------------------------
Seed the top ~40 US research universities into the roster. Universities
are:
  - Consistent H-1B sponsors (research grants + F-1 → H-1B pipeline)
  - Very OPT-friendly (F-1 → OPT → cap-exempt H-1B is standard)
  - Lower pay than FAANG but real DE/SWE/ML roles exist in IT + labs
  - Already on standard ATSes (Workday / iCIMS / Taleo / Oracle HCM)

Skipping HigherEdJobs scraping — their site has aggressive bot detection
that would require Playwright. Direct seeding is cheaper AND lets the
existing ATS crawlers pick up the jobs on their normal cadence.

Each entry: (name, careers_url). auto_discover fingerprint scan detects
the ATS and slots them into the right crawler.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from sqlalchemy import text  # noqa: E402

from app.database import engine  # noqa: E402
from app.utils.dates import utcnow_naive  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("seed_universities")

# Top-40 US research universities by CS ranking + research funding. Careers
# URL is the OFFICIAL careers landing (auto_discover will fingerprint the
# actual ATS subdomain from there). Some go direct to their ATS if it's
# known + stable.
UNIVERSITIES: list[tuple[str, str]] = [
    ("Stanford University",            "careersearch.stanford.edu"),
    ("MIT",                            "careers.mit.edu"),
    ("Carnegie Mellon University",     "cmu.wd5.myworkdayjobs.com/CMU"),
    ("UC Berkeley",                    "careerspub.universityofcalifornia.edu"),
    ("University of Washington",       "uwhires.admin.washington.edu"),
    ("Cornell University",             "career.cornell.edu"),
    ("Columbia University",            "opportunities.columbia.edu"),
    ("Princeton University",           "puwebp.princeton.edu"),
    ("University of Illinois Urbana-Champaign", "jobs.illinois.edu"),
    ("Georgia Institute of Technology","hr.gatech.edu/careers"),
    ("University of Michigan",         "careers.umich.edu"),
    ("UCLA",                           "ucla.wd1.myworkdayjobs.com/UCLACareers"),
    ("UCSD",                           "employment.ucsd.edu"),
    ("University of Texas Austin",     "utaustin.wd1.myworkdayjobs.com/UTstaff"),
    ("University of Pennsylvania",     "wd1.myworkdaysite.com/recruiting/upenn/careers-at-penn"),
    ("Yale University",                "your.yale.edu/work-yale/careers"),
    ("Harvard University",             "careers.harvard.edu"),
    ("Duke University",                "careers.duke.edu"),
    ("Northwestern University",        "careers.northwestern.edu"),
    ("Johns Hopkins University",       "jobs.jhu.edu"),
    ("Purdue University",              "careers.purdue.edu"),
    ("University of Wisconsin-Madison","jobs.hr.wisc.edu"),
    ("University of Maryland",         "ejobs.umd.edu"),
    ("Rice University",                "jobs.rice.edu"),
    ("Brown University",               "careers.brown.edu"),
    ("Vanderbilt University",          "vanderbilt.wd1.myworkdayjobs.com/VU_Careers"),
    ("Dartmouth College",              "searchjobs.dartmouth.edu"),
    ("New York University",            "uscareers-nyu.icims.com"),
    ("Boston University",              "bu.silkroad.com"),
    ("USC",                            "usc.wd5.myworkdayjobs.com/ExternalUSCCareers"),
    ("University of Chicago",          "uchicago.wd5.myworkdayjobs.com/External"),
    ("Rutgers University",             "jobs.rutgers.edu"),
    ("Ohio State University",          "hr.osu.edu/careers"),
    ("Penn State University",          "psu.wd1.myworkdayjobs.com/PSU_Staff"),
    ("Virginia Tech",                  "careers.vt.edu"),
    ("University of Minnesota",        "hr.umn.edu/Jobs"),
    ("University of Florida",          "explore.jobs.ufl.edu"),
    ("Arizona State University",       "asu.wd1.myworkdayjobs.com/ASUCareers"),
    ("Michigan State University",      "careers.msu.edu"),
    ("University of Colorado Boulder", "jobs.colorado.edu"),
    ("Notre Dame",                     "jobs.nd.edu"),
    ("Case Western Reserve University","case.edu/hr/careers"),
    ("Emory University",               "emory.wd1.myworkdayjobs.com/Emory_University_Careers"),
    ("Washington University in St. Louis", "jobs.wustl.edu"),
]


def run() -> dict:
    now = utcnow_naive()
    inserted = existing = 0
    with engine.begin() as c:
        c.execute(text("PRAGMA busy_timeout = 30000"))
        for name, url in UNIVERSITIES:
            already = c.execute(text(
                "SELECT id FROM companies WHERE LOWER(name) = :n LIMIT 1"
            ), {"n": name.lower()}).scalar()
            if already:
                existing += 1
                continue
            career_url = url.rstrip("/").replace("https://", "").replace("www.", "")
            c.execute(text("""
                INSERT INTO companies
                  (name, ats_type, career_url, is_active, priority,
                   h1b_history_score, notes, created_at, updated_at)
                VALUES
                  (:n, '', :u, 0, 'medium', 60, :note, :t, :t)
            """), {
                "n": name, "u": career_url, "t": now,
                "note": "Top-40 US research university — cap-exempt H-1B pipeline",
            })
            inserted += 1
    log.info("seed_universities: inserted=%d existing=%d", inserted, existing)
    return {"inserted": inserted, "existing": existing, "candidates": len(UNIVERSITIES)}


if __name__ == "__main__":
    raise SystemExit(0 if run() is not None else 1)
