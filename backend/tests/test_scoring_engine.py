"""
tests/test_scoring_engine.py
----------------------------
Sanity tests for scoring_engine.score(). Scoring drives which jobs land
in "New" (>=50) vs "Need Review" and controls Best Matches ordering.
We don't lock exact numbers (small tweaks are fine) but we DO lock the
directional ordering: strong-fit MUST outrank weak-fit MUST outrank
wrong-fit.
"""
from __future__ import annotations

from app.models.company import Company
from app.models.job import Job
from app.services import scoring_engine


def job(**overrides) -> Job:
    base = dict(
        company_id=1, title="Data Engineer", company_name="Acme",
        location="Austin, TX", employment_type="Full-time",
        job_url="https://example.com/1", source="greenhouse",
        description="Build ETL pipelines with Python, SQL, Spark, Airflow.",
        raw_data_hash="h", match_score=0, sponsorship_risk="unknown",
        status="New", rejection_reason="", fit_reason="", risk_reason="",
        resume_notes="", cover_letter="",
    )
    base.update(overrides)
    return Job(**base)


def _score(**kw) -> int:
    """Extract just the numeric score, ignore fit_reason string."""
    s, _reason = scoring_engine.score(job(**kw))
    return s


# ---- ORDERING (the invariants that must not break) ---------------------

def test_data_engineer_scores_higher_than_generic_swe():
    """Ram's primary target is data engineering -- Data Engineer role must
    outrank a generic SWE for scoring purposes."""
    de = _score(title="Data Engineer",
                description="Python, SQL, Spark, Airflow, dbt, AWS.")
    swe = _score(title="Software Engineer",
                 description="Write backend services in Go.")
    assert de > swe, f"DE({de}) should beat SWE({swe})"


def test_target_title_beats_containing_title():
    """'Data Engineer' should beat 'Data Center Engineer' -- containment
    is a red flag for wrong-subrole, not a match signal."""
    data_eng = _score(title="Data Engineer")
    data_center = _score(title="Data Center Engineer")
    assert data_eng > data_center


def test_solutions_architect_penalized():
    """Solutions Architect contains 'engineer'-adjacent tokens but is a
    fundamentally different role -- must be penalized hard."""
    de = _score(title="Data Engineer")
    sa = _score(title="Solutions Architect")
    assert de > sa + 20  # meaningful gap, not just numeric noise


def test_senior_level_reduces_score():
    """A 'Senior' title still gets scored (may not be filtered depending on
    config) but must score LOWER than the mid/entry version."""
    mid = _score(title="Data Engineer")
    senior = _score(title="Senior Data Engineer II")
    assert mid > senior


# ---- SANITY BOUNDS ------------------------------------------------------

def test_score_is_bounded_0_to_100():
    """Score is used everywhere as a 0-100 range; must clamp."""
    s = _score(title="Data Engineer",
               description="Python SQL Spark Airflow dbt Kubernetes Kafka AWS.")
    assert 0 <= s <= 100


def test_totally_off_target_scores_low():
    """A nurse posting somehow slipping past the filter should score near 0."""
    s = _score(title="Registered Nurse",
               description="Direct patient care in ICU setting.")
    assert s < 30
