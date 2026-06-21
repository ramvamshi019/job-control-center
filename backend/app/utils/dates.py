"""
utils/dates.py
--------------
Forgiving date parsing. Career APIs return many formats; we never crash on a
bad date — we just return None.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from dateutil import parser as _parser


def parse_date(value) -> Optional[datetime]:
    """Parse almost any date string / epoch into a naive UTC datetime."""
    if value is None or value == "":
        return None
    try:
        # Epoch milliseconds (Ashby/Lever sometimes use these).
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e12 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
        dt = _parser.parse(str(value))
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def hours_since(dt: Optional[datetime]) -> Optional[float]:
    """How many hours ago was `dt`? None if unknown."""
    if dt is None:
        return None
    return (datetime.utcnow() - dt).total_seconds() / 3600.0
