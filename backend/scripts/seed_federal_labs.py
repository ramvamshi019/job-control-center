"""
scripts/seed_federal_labs.py
----------------------------
Seed US federal research labs + national labs. Like universities, most
are cap-exempt H-1B sponsors (they don't compete for the 85k lottery),
so sponsorship odds are much higher than a private-sector role.
Real data-eng / SWE / ML roles exist in climate modeling, particle
physics, biomedical informatics, defense-adjacent research.

Notable ATSes:
  - USA Jobs (federal-wide): usajobs.gov (has its own API — separate crawler)
  - Workday: increasingly common at national labs
  - Some use custom .gov career sites

For now: seed the org rows with best-known career URLs. auto_discover
fingerprints the ATS on next weekly pass. Where the site is truly
custom (usajobs.gov style), a future dedicated crawler can hook in.
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

log = get_logger("seed_federal_labs")

# National labs + federal research orgs. career_url is the best-known
# careers landing; auto_discover walks it to detect the underlying ATS.
LABS: list[tuple[str, str]] = [
    ("NASA Jet Propulsion Laboratory", "jobs.jpl.nasa.gov"),
    ("NASA Goddard Space Flight Center","careers.nasa.gov"),
    ("NASA Ames Research Center",       "careers.nasa.gov"),
    ("NIH National Institutes of Health","hr.nih.gov/jobs"),
    ("NIST",                            "nist.gov/careers"),
    ("Lawrence Berkeley National Lab",  "jobs.lbl.gov"),
    ("Lawrence Livermore National Lab", "careers-llnl.ttcportals.com"),
    ("Los Alamos National Lab",         "jobs.lanl.gov"),
    ("Oak Ridge National Lab",          "jobs.ornl.gov"),
    ("Argonne National Lab",            "careers.anl.gov"),
    ("Pacific Northwest National Lab",  "careers.pnnl.gov"),
    ("Sandia National Laboratories",    "sandia.jobs"),
    ("Fermilab",                        "fermilab.wd1.myworkdayjobs.com/FermiJobs"),
    ("SLAC National Accelerator Lab",   "careersearch.stanford.edu/jobs/search?keywords=SLAC"),
    ("NREL National Renewable Energy Lab","nrel.wd5.myworkdayjobs.com/NREL"),
    ("Idaho National Lab",              "inl.wd1.myworkdayjobs.com/INLCareers"),
    ("Brookhaven National Lab",         "jobs.bnl.gov"),
    ("Jefferson Lab",                   "jlab.org/careers"),
    ("MITRE Corporation",               "careers.mitre.org"),
    ("Aerospace Corporation",           "careers.aerospace.org"),
    ("SRI International",               "sri.wd1.myworkdayjobs.com/careers"),
    ("Battelle Memorial Institute",     "battelle.wd5.myworkdayjobs.com/BattelleCareers"),
    ("Institute for Defense Analyses",  "ida.org/careers"),
    ("Software Engineering Institute (CMU)","sei.cmu.edu/careers"),
    ("National Radio Astronomy Observatory","nrao.edu/careers"),
    ("National Center for Atmospheric Research","ucar.edu/careers"),
    ("Woods Hole Oceanographic Institution","careers.whoi.edu"),
]


def run() -> dict:
    now = utcnow_naive()
    inserted = existing = 0
    with engine.begin() as c:
        c.execute(text("PRAGMA busy_timeout = 30000"))
        for name, url in LABS:
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
                  (:n, '', :u, 0, 'medium', 65, :note, :t, :t)
            """), {
                "n": name, "u": career_url, "t": now,
                "note": "Federal research lab — cap-exempt H-1B, OPT-friendly",
            })
            inserted += 1
    log.info("seed_federal_labs: inserted=%d existing=%d", inserted, existing)
    return {"inserted": inserted, "existing": existing, "candidates": len(LABS)}


if __name__ == "__main__":
    raise SystemExit(0 if run() is not None else 1)
