"""
tests/test_filter_engine.py
---------------------------
Regression tests for services/filter_engine.evaluate(). The filter drops
~99% of crawled jobs (76k/day -> 800 survive) so any silent change here
massively distorts what the user sees. These tests LOCK IN the current
behavior -- if you deliberately change a rule, update the expected result.
"""
from __future__ import annotations

import pytest

from app.models.job import Job
from app.services import filter_engine


def make_job(**overrides) -> Job:
    """Cheap Job factory with sensible defaults for a US tech role."""
    base = dict(
        company_id=1,
        title="Data Engineer",
        company_name="Acme Corp",
        location="San Francisco, CA",
        employment_type="Full-time",
        job_url="https://example.com/jobs/1",
        source="greenhouse",
        description="Build data pipelines. Python, SQL, AWS.",
        raw_data_hash="abc123",
        match_score=0,
        sponsorship_risk="unknown",
        status="New",
        rejection_reason="",
        fit_reason="",
        risk_reason="",
        resume_notes="",
        cover_letter="",
    )
    base.update(overrides)
    return Job(**base)


# ---- HAPPY PATH ---------------------------------------------------------

def test_target_data_role_passes():
    r = filter_engine.evaluate(make_job(title="Data Engineer", location="Austin, TX"))
    assert r.passed, f"Expected pass but got: {r.reason}"


def test_swe_role_passes():
    r = filter_engine.evaluate(make_job(title="Software Engineer", location="Remote, USA"))
    assert r.passed, r.reason


def test_ml_role_passes():
    r = filter_engine.evaluate(make_job(title="Machine Learning Engineer",
                                        location="Seattle, WA"))
    assert r.passed, r.reason


# ---- SENIORITY BLOCKS ---------------------------------------------------

@pytest.mark.parametrize("title", [
    "Senior Software Engineer",
    "Staff Data Engineer",
    "Principal Data Scientist",
    "Lead Machine Learning Engineer",
    "Engineering Manager",
    "Director of Engineering",
])
def test_senior_titles_rejected(title):
    r = filter_engine.evaluate(make_job(title=title))
    assert not r.passed, f"Expected {title!r} rejected but passed"


def test_engineer_ii_rejected_as_too_senior():
    # "II" / "III" / "IV" are level indicators; block if they signal mid-senior
    r = filter_engine.evaluate(make_job(title="Software Engineer III"))
    assert not r.passed


# ---- NON-TECH TITLES ----------------------------------------------------

@pytest.mark.parametrize("title", [
    "Marketing Manager",
    "Registered Nurse",
    "Sales Representative",
    "Account Executive",
    "Recruiter",
    "Copywriter",
])
def test_non_tech_titles_rejected(title):
    r = filter_engine.evaluate(make_job(title=title))
    assert not r.passed, f"Expected {title!r} rejected but passed"


def test_non_target_engineer_rejected():
    # Bare "Engineer" without a tech qualifier catches Quality/Field/Mechanical
    r = filter_engine.evaluate(make_job(title="Field Engineer"))
    assert not r.passed


# ---- US LOCATION GATE ---------------------------------------------------

@pytest.mark.parametrize("location", [
    "Bangalore, Karnataka, in",
    "London, UK",
    "Berlin, Germany",
    "Toronto, ON, ca",
    "Sydney, Australia",
])
def test_non_us_locations_rejected(location):
    r = filter_engine.evaluate(make_job(location=location))
    assert not r.passed
    assert "Location" in r.reason or "location" in r.reason.lower()


@pytest.mark.parametrize("location", [
    "New York, NY",
    "Austin, TX",
    "United States",
    "Remote, USA",
    "San Francisco Bay Area",
    "Chicago",
])
def test_us_locations_pass(location):
    r = filter_engine.evaluate(make_job(location=location))
    assert r.passed, f"Expected {location!r} to pass but rejected: {r.reason}"


# ---- EMPLOYMENT TYPE ----------------------------------------------------

@pytest.mark.parametrize("etype", ["Contract", "Part-time", "C2C", "1099", "Temporary"])
def test_bad_employment_types_rejected(etype):
    r = filter_engine.evaluate(make_job(employment_type=etype))
    assert not r.passed


def test_fulltime_passes():
    r = filter_engine.evaluate(make_job(employment_type="Full-time"))
    assert r.passed, r.reason


# ---- OPT-BENCH SCAM SIGNALS --------------------------------------------

@pytest.mark.parametrize("title", [
    "ETL/Snowflake/Databricks/AWS training opportunity for OPT candidates",
    "OPT/CPT training and placement",
])
def test_opt_bench_shops_rejected(title):
    r = filter_engine.evaluate(make_job(title=title))
    assert not r.passed


# ---- ENTRY-LEVEL EXEMPTION FROM YEARS BLOCK ----------------------------

def test_entry_title_survives_years_line_in_desc():
    """An explicitly entry-level title should NOT be killed by a stray
    '5+ years preferred' line in the description -- that's typically the
    generic company bar, not a hard gate for an Associate role."""
    r = filter_engine.evaluate(make_job(
        title="Associate Data Engineer",
        description="5+ years preferred. Build pipelines in Python and SQL."))
    assert r.passed, r.reason


def test_non_entry_title_blocked_by_years():
    """A vanilla 'Data Engineer' title WITH '10+ years required' should
    be filtered."""
    r = filter_engine.evaluate(make_job(
        title="Data Engineer",
        description="Requires 10+ years of production ETL experience."))
    assert not r.passed
