"""
routes/audit.py
---------------
Serves the daily audit reports written by scripts/daily_audit.py so the
dashboard can render them without SSHing into the droplet.

Reports live at data/db/reports/audit-YYYY-MM-DD.{json,txt} inside the
persistent volume. Two endpoints:

  GET /audit/           -> list of available report dates (newest first)
  GET /audit/{date}     -> the JSON snapshot for that date
                           (date = "latest" or YYYY-MM-DD)
  GET /audit/{date}/txt -> the human-readable text report for that date
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.database import get_session
from app.services import registry

router = APIRouter(prefix="/audit", tags=["audit"])

REPORT_DIR = Path("data/db/reports")


def _find_report(date: str, ext: str) -> Optional[Path]:
    """Resolve a date string to a report file. "latest" maps to latest.{ext}."""
    if date == "latest":
        p = REPORT_DIR / f"latest.{ext}"
        return p if p.exists() else None
    p = REPORT_DIR / f"audit-{date}.{ext}"
    return p if p.exists() else None


@router.get("/")
def list_reports() -> dict:
    """Enumerate available report dates, newest first."""
    if not REPORT_DIR.is_dir():
        return {"reports": [], "latest": None}
    dates = []
    for p in REPORT_DIR.glob("audit-*.json"):
        # File name is `audit-YYYY-MM-DD.json`.
        try:
            d = p.stem.split("-", 1)[1]
            dates.append(d)
        except IndexError:
            continue
    dates.sort(reverse=True)
    return {"reports": dates, "latest": dates[0] if dates else None}


@router.get("/{date}")
def get_report_json(date: str) -> dict:
    """Machine-readable snapshot. Pass 'latest' or 'YYYY-MM-DD'."""
    p = _find_report(date, "json")
    if not p:
        raise HTTPException(404, f"No report for {date}")
    try:
        return json.loads(p.read_text())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Could not parse {p.name}: {exc}") from exc


@router.get("/{date}/txt")
def get_report_text(date: str) -> dict:
    """Pre-rendered human-readable report body -- what the cron produces."""
    p = _find_report(date, "txt")
    if not p:
        raise HTTPException(404, f"No text report for {date}")
    return {"date": date, "content": p.read_text()}


@router.get("/registry/stats")
def registry_stats_endpoint(session: Session = Depends(get_session)) -> dict:
    """Live registry roster snapshot: active / archived / by tier / by ATS /
    hiring in the last 24h. Cheap enough to serve on every dashboard render."""
    return registry.registry_stats(session)


@router.get("/registry/preview")
def registry_preview(session: Session = Depends(get_session)) -> dict:
    """Dry-run the maintenance sweep: what a `--apply` would change.
    Purely read-only; never writes."""
    delta = registry.compute_deltas(session, dry_run=True)
    return delta.as_dict()
