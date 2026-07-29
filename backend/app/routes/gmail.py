"""
routes/gmail.py
---------------
Endpoints backing the ⚙️ Gmail Settings and 📬 Inbox pages.

  GET  /gmail/settings          -> status + guessed metadata
  POST /gmail/settings          -> save {email, app_password} + test IMAP login
  POST /gmail/settings/clear    -> delete settings file
  POST /gmail/poll              -> manually kick a poll (also runs every 15 min from discovery_loop)
  GET  /gmail/messages          -> list matched messages, joined with job + company

Settings live at /app/backend/data/db/gmail_settings.json on the persistent
volume (never in git, never in the image). Same trust boundary as jobs.db.
"""
from __future__ import annotations

import imaplib
import json
import os
import ssl
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app.database import engine

router = APIRouter(prefix="/gmail", tags=["gmail"])

SETTINGS_PATH = "/app/backend/data/db/gmail_settings.json"
STATE_PATH    = "/app/backend/data/db/gmail_watcher_state.json"


class GmailSettingsIn(BaseModel):
    email: str
    app_password: str


def _load_settings() -> dict:
    if not os.path.exists(SETTINGS_PATH):
        return {}
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_settings(d: dict):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(d, f)
    # keep tight -- credentials
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except Exception:  # noqa: BLE001
        pass


def _load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _count_matched() -> int:
    """Total messages ever matched. 0 if job_messages table doesn't exist yet."""
    with engine.connect() as c:
        try:
            return c.execute(text("SELECT COUNT(*) FROM job_messages")).scalar() or 0
        except Exception:  # noqa: BLE001
            return 0


@router.get("/settings")
def get_settings():
    s = _load_settings()
    state = _load_state()
    return {
        "configured": bool(s.get("email") and s.get("app_password")),
        "email": s.get("email", ""),
        "last_poll": state.get("last_poll"),
        "total_matched": _count_matched(),
    }


@router.post("/settings")
def save_settings(payload: GmailSettingsIn):
    email = payload.email.strip()
    pw = payload.app_password.replace(" ", "")
    if not (email and pw):
        return {"ok": False, "error": "email and app_password required"}
    # Test IMAP login before persisting so a bad password is caught immediately.
    login_test = "not attempted"
    try:
        ctx = ssl.create_default_context()
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ctx)
        M.login(email, pw)
        M.logout()
        login_test = "OK"
    except imaplib.IMAP4.error as e:
        return {"ok": False, "error": f"IMAP login failed: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"connection failed: {e}"}
    _save_settings({"email": email, "app_password": pw,
                     "saved_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()})
    return {"ok": True, "login_test": login_test}


@router.post("/settings/clear")
def clear_settings():
    for p in (SETTINGS_PATH, STATE_PATH):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True}


@router.post("/poll")
def poll_now():
    """Fire an ad-hoc poll from the dashboard. In steady state discovery_loop
    calls the same run() every 15 min automatically."""
    import sys
    sys.path.insert(0, "/app/backend/scripts")
    try:
        from gmail_watcher import run
    except Exception as e:  # noqa: BLE001
        return {"error": f"import gmail_watcher failed: {e}"}
    try:
        summary = run()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    # Stamp last_poll on state.
    st = _load_state()
    st["last_poll"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(st, f)
    except Exception:  # noqa: BLE001
        pass
    return summary


@router.get("/messages")
def list_messages(limit: int = 200):
    """Recent matched messages joined with job + company for display."""
    with engine.connect() as c:
        try:
            rows = c.execute(text(f"""
                SELECT
                    m.id, m.received_at, m.classification,
                    m.from_addr, m.from_domain, m.subject, m.snippet,
                    m.job_id, j.title AS job_title, j.company_name AS company_name
                FROM job_messages m
                LEFT JOIN jobs j ON j.id = m.job_id
                ORDER BY m.received_at DESC
                LIMIT {int(limit)}
            """)).all()
        except Exception:  # noqa: BLE001
            # table doesn't exist yet -- first poll will create it
            return []
    return [dict(r._mapping) for r in rows]
