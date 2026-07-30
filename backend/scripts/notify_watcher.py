"""
scripts/notify_watcher.py
-------------------------
Poll the DB for newly-discovered top-quality sponsor jobs and email
you a digest via your existing Gmail account (reuses the same SMTP
credentials the Gmail-watcher uses to READ your inbox).

Runs from discovery_loop.py every N minutes. Idempotent -- tracks the
highest job_id it has notified about in a state file, so you never
get duplicate alerts.

CRITERIA for "worth waking up for":
    - discovered in last NOTIFY_WINDOW_MIN minutes
    - sponsor-confirmed (h1b_history_score >= 50)
    - match_score >= NOTIFY_MIN_SCORE
    - status = New (not Rejected/Applied/etc.)

Email format: single digest with links, not per-job spam.
Skips silently if Gmail settings not configured yet.
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
from datetime import timedelta
from email.message import EmailMessage

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from sqlalchemy import text  # noqa: E402

from app.database import engine  # noqa: E402
from app.utils.dates import utcnow_naive  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("notify_watcher")

SETTINGS_PATH = "/app/backend/data/db/gmail_settings.json"
STATE_PATH    = "/app/backend/data/db/notify_watcher_state.json"

# Tunables via env
NOTIFY_MIN_SCORE = int(os.environ.get("NOTIFY_MIN_SCORE", 70))
NOTIFY_WINDOW_MIN = int(os.environ.get("NOTIFY_WINDOW_MIN", 30))  # look-back window
NOTIFY_MAX_JOBS_PER_EMAIL = 15
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def _load_settings() -> dict | None:
    if not os.path.exists(SETTINGS_PATH):
        return None
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"last_notified_id": 0}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"last_notified_id": 0}


def _save_state(s: dict):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(s, f)
    except OSError:
        pass


def _build_digest_email(rows: list[dict], to_addr: str) -> EmailMessage:
    """Build the plain-text + HTML digest for the top N new sponsor jobs."""
    msg = EmailMessage()
    msg["Subject"] = f"🎯 {len(rows)} new H-1B sponsor job(s) — top match {rows[0]['match_score']}"
    msg["From"] = to_addr
    msg["To"] = to_addr

    plain_lines = [
        f"{len(rows)} new sponsor-confirmed jobs (last {NOTIFY_WINDOW_MIN} min, score>={NOTIFY_MIN_SCORE}):",
        "",
    ]
    for r in rows:
        plain_lines.append(f"• [{r['match_score']}] {r['title']} @ {r['company_name']}")
        plain_lines.append(f"  {r['location']}  ·  Apply: {r['job_url']}")
        plain_lines.append("")
    plain_lines.append("Open dashboard: https://143.198.188.116.sslip.io/")
    msg.set_content("\n".join(plain_lines))

    # HTML version for nicer rendering in Gmail
    html_rows = "".join(
        f"<tr>"
        f"<td style='padding:6px 10px;background:#1a7f37;color:#fff;font-weight:700;border-radius:4px'>{r['match_score']}</td>"
        f"<td style='padding:6px 10px'><b>{r['title']}</b><br>"
        f"<span style='color:#666'>{r['company_name']} · {r['location']}</span><br>"
        f"<a href='{r['job_url']}'>Apply →</a></td>"
        f"</tr>"
        for r in rows
    )
    html = f"""
    <html><body style='font-family:-apple-system,sans-serif;max-width:640px'>
    <h2>🎯 {len(rows)} new H-1B sponsor jobs</h2>
    <p style='color:#666'>Discovered in the last {NOTIFY_WINDOW_MIN} min · sponsor-confirmed · score ≥ {NOTIFY_MIN_SCORE}</p>
    <table style='border-collapse:collapse;width:100%'>{html_rows}</table>
    <p style='margin-top:24px'><a href='https://143.198.188.116.sslip.io/'>Open dashboard →</a></p>
    </body></html>
    """
    msg.add_alternative(html, subtype="html")
    return msg


def run() -> dict:
    settings = _load_settings()
    if not settings or not settings.get("email") or not settings.get("app_password"):
        log.info("notify: no gmail creds -- skip")
        return {"configured": False}

    state = _load_state()
    since = utcnow_naive() - timedelta(minutes=NOTIFY_WINDOW_MIN)

    # Query candidates -- must be sponsor-confirmed, high-score, discovered
    # recently, and NEWER than the last job_id we notified about.
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT j.id, j.title, j.company_name, j.location, j.job_url,
                   j.match_score, j.discovered_at
            FROM jobs j
            JOIN companies co ON co.id = j.company_id
            WHERE j.discovered_at >= :since
              AND j.match_score >= :min_score
              AND co.h1b_history_score >= 50
              AND j.status = 'New'
              AND j.id > :last_id
            ORDER BY j.match_score DESC, j.id DESC
            LIMIT :cap
        """), {
            "since": since,
            "min_score": NOTIFY_MIN_SCORE,
            "last_id": state.get("last_notified_id", 0),
            "cap": NOTIFY_MAX_JOBS_PER_EMAIL,
        }).all()

    if not rows:
        log.info("notify: 0 new sponsor jobs in last %d min above score %d",
                 NOTIFY_WINDOW_MIN, NOTIFY_MIN_SCORE)
        return {"configured": True, "sent": 0}

    dicts = [dict(r._mapping) for r in rows]
    msg = _build_digest_email(dicts, settings["email"])

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(settings["email"], settings["app_password"])
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        log.warning("notify: SMTP send failed: %s", e)
        return {"configured": True, "sent": 0, "error": str(e)}

    # Stamp state so we don't re-notify the same jobs
    state["last_notified_id"] = max(r["id"] for r in dicts)
    _save_state(state)
    log.info("notify: sent digest with %d jobs (max_id=%d)", len(dicts), state["last_notified_id"])
    return {"configured": True, "sent": len(dicts)}


if __name__ == "__main__":
    run()
