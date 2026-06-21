"""
routes/export.py
----------------
Trigger a CSV export over the API (the dashboard's "Export" button calls this).
"""

from __future__ import annotations

import os
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app.database import get_session
from app.services import exporter

router = APIRouter(prefix="/export", tags=["export"])

# When the API runs from backend/, project root is one level up.
_API_EXPORT_PATH = os.path.join("..", "exports", "daily_jobs.csv")


@router.post("/")
def export_csv(
    session: Session = Depends(get_session),
    statuses: Optional[List[str]] = Query(default=None, description="Statuses to export"),
):
    path, count = exporter.export_jobs(session, statuses=statuses, path=_API_EXPORT_PATH)
    return {"path": os.path.abspath(path), "count": count}
