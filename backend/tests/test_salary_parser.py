"""
tests/test_salary_parser.py
---------------------------
The salary parser lives in dashboard/app.py (Streamlit-runtime code) so
we can't import it in the backend test container. This file mirrors the
regex + logic so it stays behavior-locked. If dashboard/app.py changes
its regex, update this file to match.
"""
import re

_SALARY_RANGE_RE = re.compile(
    r"\$\s*(\d{2,3})[,]?(\d{3})?\s*[kK]?\s*(?:[-–—to]{1,3})\s*"
    r"\$?\s*(\d{2,3})[,]?(\d{3})?\s*[kK]?",
)
_SALARY_SINGLE_RE = re.compile(
    r"(?:starting\s+at|from|minimum|min|base)\s*[:\s]*"
    r"\$\s*(\d{2,3})[,]?(\d{3})?\s*[kK]?",
    re.I,
)
_SALARY_SIMPLE_RE = re.compile(r"\$\s*(\d{2,3})[,]?(\d{3})?\s*[kK]?")


def _to_dollars(major, minor):
    n = int(major)
    if minor:
        n = n * 1000 + int(minor)
    elif n < 500:
        n = n * 1000
    return n


def parse_min_salary(desc):
    if not desc:
        return None
    desc = desc[:5000]
    m = _SALARY_RANGE_RE.search(desc)
    if m:
        low = _to_dollars(m.group(1), m.group(2))
        if 40_000 <= low <= 500_000:
            return low
    m = _SALARY_SINGLE_RE.search(desc)
    if m:
        v = _to_dollars(m.group(1), m.group(2))
        if 40_000 <= v <= 500_000:
            return v
    for m in _SALARY_SIMPLE_RE.finditer(desc):
        v = _to_dollars(m.group(1), m.group(2))
        if 60_000 <= v <= 500_000:
            return v
    return None


def test_range_extracts_low_bound():
    assert parse_min_salary("Base salary $120,000 - $180,000 plus equity.") == 120000


def test_k_notation():
    assert parse_min_salary("Compensation: $150K to $200K") == 150000


def test_starting_at():
    assert parse_min_salary("Salary starting at $130,000 per year.") == 130000


def test_no_dollar_amount_returns_none():
    assert parse_min_salary("Competitive salary based on experience.") is None


def test_empty_returns_none():
    assert parse_min_salary("") is None
    assert parse_min_salary(None) is None


def test_ignores_out_of_range():
    # $10 tip in a JD shouldn't be treated as salary
    assert parse_min_salary("We provide $15 lunch stipends daily.") is None


def test_dash_variants():
    # em dash + en dash + hyphen all common in JDs
    assert parse_min_salary("$100,000–$150,000") == 100000
    assert parse_min_salary("$100,000 — $150,000") == 100000
    assert parse_min_salary("$100,000-$150,000") == 100000
