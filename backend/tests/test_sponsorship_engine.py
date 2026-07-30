"""
tests/test_sponsorship_engine.py
--------------------------------
Regression tests for services/sponsorship_engine.assess(). This decides
whether a job goes to Rejected (citizenship required) vs Low/Medium/High
risk -- direct impact on what surfaces in Best Matches / Posted Today.
"""
import pytest

from app.models.company import Company
from app.models.job import Job
from app.services import sponsorship_engine


def job(**overrides) -> Job:
    base = dict(
        company_id=1, title="Data Engineer", company_name="Acme",
        location="Austin, TX", employment_type="Full-time",
        job_url="https://example.com/1", source="greenhouse",
        description="Build ETL pipelines. Python, SQL, Spark.",
        raw_data_hash="h", match_score=0, sponsorship_risk="unknown",
        status="New", rejection_reason="", fit_reason="", risk_reason="",
        resume_notes="", cover_letter="",
    )
    base.update(overrides)
    return Job(**base)


def company(**overrides) -> Company:
    base = dict(
        name="Acme", career_url="https://example.com/careers", ats_type="greenhouse",
        h1b_history_score=0, priority="medium", is_active=True, notes="",
    )
    base.update(overrides)
    return Company(**base)


# ---- REJECT: explicit blockers -----------------------------------------

def test_us_citizen_only_rejected():
    r, reason = sponsorship_engine.assess(
        job(description="US citizenship required for this role."))
    assert r == "reject"
    assert "citizenship" in reason.lower() or "us citiz" in reason.lower()


def test_active_secret_clearance_rejected():
    r, _ = sponsorship_engine.assess(
        job(description="Active Secret clearance required. TS/SCI preferred."))
    assert r == "reject"


def test_no_visa_sponsorship_language_rejected():
    r, reason = sponsorship_engine.assess(
        job(description="We are unable to sponsor visas at this time."))
    assert r == "reject"


# ---- LOW: known sponsor OR positive language ---------------------------

def test_confirmed_sponsor_low_risk():
    r, reason = sponsorship_engine.assess(
        job(description="Standard tech role. Nothing unusual."),
        company=company(h1b_history_score=75))
    assert r == "low"
    assert "confirmed" in reason.lower() or "h-1b" in reason.lower() or "75" in reason


def test_explicit_sponsorship_friendly_low_risk():
    r, _ = sponsorship_engine.assess(
        job(description="We sponsor H-1B visas and green cards for the right candidates."))
    assert r == "low"


# ---- MEDIUM: unclear ----------------------------------------------------

def test_unknown_company_no_signals_medium():
    r, _ = sponsorship_engine.assess(
        job(description="Standard software role. Ship features. Own the roadmap."))
    assert r == "medium"


def test_matched_but_weak_sponsor_medium():
    """Company matched in USCIS data but very few recent approvals -> medium
    ('verify before applying')."""
    r, _ = sponsorship_engine.assess(
        job(description="Nothing unusual."),
        company=company(h1b_history_score=45))
    assert r == "medium"


# ---- HIGH: vague authorization + no history ----------------------------

def test_vague_authorization_high_risk():
    r, _ = sponsorship_engine.assess(
        job(description="Must be authorized to work in the US without any restrictions."),
        company=company(h1b_history_score=0))
    assert r == "high"
