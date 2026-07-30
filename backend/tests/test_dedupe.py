"""
tests/test_dedupe.py
--------------------
Tests for services/dedupe.find_duplicate() -- gates which jobs get
persisted vs treated as re-seen.
"""
import pytest

from app.models.job import Job
from app.services import dedupe


def _job(**kw) -> Job:
    base = dict(
        company_id=1, title="Data Engineer", company_name="Acme",
        location="Austin, TX", employment_type="Full-time",
        job_url="https://example.com/1", source="greenhouse",
        description="Build pipelines.", raw_data_hash="",
        match_score=0, sponsorship_risk="unknown",
        status="New", rejection_reason="", fit_reason="",
        risk_reason="", resume_notes="", cover_letter="",
    )
    base.update(kw)
    return Job(**base)


def test_dedupe_module_exports_find_duplicate():
    # Structural: the function every crawler pipeline relies on must exist.
    assert hasattr(dedupe, "find_duplicate")
    assert callable(dedupe.find_duplicate)
