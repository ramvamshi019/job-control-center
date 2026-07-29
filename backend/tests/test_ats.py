"""
tests/test_ats.py
-----------------
Regression tests for utils/ats.detect_ats(). This is the shared ATS URL
regex used by every seeder + the dashboard. Silent drift here means
one seeder starts missing hits and no one notices for weeks.
"""
import pytest

from app.utils.ats import detect_ats, is_ats_host


@pytest.mark.parametrize("text, expected", [
    ("apply at https://boards.greenhouse.io/stripe/jobs/123", ("greenhouse", "stripe")),
    ("https://job-boards.greenhouse.io/anthropic",             ("greenhouse", "anthropic")),
    ("Working on Lever? See jobs.lever.co/bounteous",          ("lever",      "bounteous")),
    ("Cursor is hiring! jobs.ashbyhq.com/anysphere",           ("ashby",      "anysphere")),
    ("nvidia.wd5.myworkdayjobs.com/careers",                    ("workday",    "nvidia")),
    ("careers-microsoft.icims.com/jobs",                        ("icims",      "microsoft")),
    ("acme.bamboohr.com/careers",                               ("bamboohr",   "acme")),
    ("jobs.smartrecruiters.com/BoschGroup/12345",              ("smartrecruiters", "BoschGroup")),
    ("apply.workable.com/huggingface",                          ("workable",   "huggingface")),
    ("ats.rippling.com/rippling",                               ("rippling",   "rippling")),
    ("democo.recruitee.com",                                    ("recruitee",  "democo")),
])
def test_detect_ats_hits(text, expected):
    assert detect_ats(text) == expected


@pytest.mark.parametrize("text", [
    "",
    "https://google.com",                     # No ATS host
    "https://linkedin.com/jobs/view/123",     # LinkedIn is not an ATS we crawl
    "https://indeed.com/jobs/xyz",
    "check my personal blog at example.com",
])
def test_detect_ats_misses(text):
    assert detect_ats(text) is None


def test_is_ats_host_classifies_correctly():
    # Employer's own domain -> not an ATS
    assert not is_ats_host("stripe.com")
    assert not is_ats_host("www.stripe.com")
    # ATS platforms -> yes
    assert is_ats_host("boards.greenhouse.io")
    assert is_ats_host("jobs.lever.co")
    assert is_ats_host("jobs.ashbyhq.com")
    assert is_ats_host("company.myworkdayjobs.com")
    # Empty/none
    assert not is_ats_host("")
    assert not is_ats_host(None)
