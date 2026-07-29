"""
tests/test_dates.py
-------------------
Coverage for utils/dates.
"""
from datetime import datetime, timezone

from app.utils.dates import parse_date, utcnow_naive


def test_utcnow_naive_returns_naive():
    n = utcnow_naive()
    assert n.tzinfo is None


def test_utcnow_naive_close_to_now():
    aware = datetime.now(timezone.utc).replace(tzinfo=None)
    n = utcnow_naive()
    # Within 2 seconds
    assert abs((n - aware).total_seconds()) < 2


def test_parse_date_none_and_empty():
    assert parse_date(None) is None
    assert parse_date("") is None


def test_parse_date_iso():
    d = parse_date("2026-07-29T12:34:56Z")
    assert d is not None
    assert d.year == 2026 and d.month == 7 and d.day == 29


def test_parse_date_epoch_int():
    d = parse_date(1751000000)
    assert d is not None
    assert d.year in (2025, 2026)


def test_parse_date_garbage_returns_none():
    assert parse_date("not-a-date-obviously") is None
    assert parse_date("2999-99-99") is None
