"""
scripts/morning_brief.py
------------------------
Sends a single "morning brief" email at ~6am UTC summarizing what
happened in JCC while you slept. Different from notify_watcher which
fires every 15 min per-batch -- this is ONE aggregated brief you read
with coffee.

Contents:
  - Overnight discovery stats (new jobs, new companies, USCIS/LCA progress)
  - Inbox activity (responses received, jobs auto-triaged)
  - AI-curated top 5 jobs to apply to first (score + sponsor + freshness)
  - Optional: text roundup written by Claude if AI_PROVIDER=anthropic

Runs from cron on the droplet (/etc/cron.d/jcc-morning-brief) at 06:00
UTC daily. Reuses gmail_settings.json for SMTP send. Silent no-op if
Gmail not configured yet.
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

from app.config import settings  # noqa: E402
from app.database import engine  # noqa: E402
from app.utils.dates import utcnow_naive  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("morning_brief")

SETTINGS_PATH = "/app/backend/data/db/gmail_settings.json"
STATE_PATH    = "/app/backend/data/db/morning_brief_state.json"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

DASHBOARD_URL = os.environ.get("JCC_DASHBOARD_URL", "https://143.198.188.116.sslip.io/")


def _load_gmail() -> dict | None:
    if not os.path.exists(SETTINGS_PATH):
        return None
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _stats_last_24h() -> dict:
    """One-shot SQL over the DB to gather overnight numbers."""
    now = utcnow_naive()
    d24 = now - timedelta(hours=24)
    with engine.connect() as c:
        c.execute(text("PRAGMA busy_timeout = 30000"))
        total_jobs = c.execute(text("SELECT COUNT(*) FROM jobs")).scalar()
        crawled_24h = c.execute(
            text("SELECT COUNT(*) FROM jobs WHERE discovered_at >= :d"),
            {"d": d24}).scalar()
        survived_24h = c.execute(text("""
            SELECT COUNT(*) FROM jobs
            WHERE discovered_at >= :d AND status NOT IN ('Rejected','Archived')
        """), {"d": d24}).scalar()
        new_24h = c.execute(text("""
            SELECT COUNT(*) FROM jobs
            WHERE discovered_at >= :d AND status = 'New'
        """), {"d": d24}).scalar()
        active_companies = c.execute(text(
            "SELECT COUNT(*) FROM companies WHERE is_active = 1")).scalar()
        # Recent inbox events
        try:
            interviews = c.execute(text("""
                SELECT COUNT(*) FROM job_messages
                WHERE received_at >= :d AND classification = 'interview'
            """), {"d": d24}).scalar()
            rejections = c.execute(text("""
                SELECT COUNT(*) FROM job_messages
                WHERE received_at >= :d AND classification = 'rejection'
            """), {"d": d24}).scalar()
            acks = c.execute(text("""
                SELECT COUNT(*) FROM job_messages
                WHERE received_at >= :d AND classification = 'ack'
            """), {"d": d24}).scalar()
        except Exception:
            interviews = rejections = acks = 0
        # Top-5 fresh sponsor jobs to attack first
        top = c.execute(text("""
            SELECT j.id, j.title, j.company_name, j.location, j.job_url, j.match_score
            FROM jobs j
            JOIN companies co ON co.id = j.company_id
            WHERE j.discovered_at >= :d
              AND j.status = 'New'
              AND co.h1b_history_score >= 50
            ORDER BY j.match_score DESC, j.discovered_at DESC
            LIMIT 5
        """), {"d": d24}).all()
    return {
        "total_jobs": total_jobs, "crawled_24h": crawled_24h,
        "survived_24h": survived_24h, "new_24h": new_24h,
        "active_companies": active_companies,
        "interviews": interviews, "rejections": rejections, "acks": acks,
        "top_jobs": [dict(r._mapping) for r in top],
    }


def _ai_summary(stats: dict) -> str:
    """One-paragraph plain-English take on the overnight from Claude.
    Skipped if AI_PROVIDER isn't anthropic."""
    if settings.ai_provider != "anthropic" or not settings.anthropic_api_key:
        return ""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        prompt = f"""You're writing a 2-3 sentence morning wake-up brief for an F-1 OPT data engineer who runs a job-crawler. Voice: concise, punchy, useful. No jargon. No emojis. Data from the last 24h:

- {stats['crawled_24h']:,} jobs crawled, {stats['survived_24h']:,} survived filters
- {stats['new_24h']} tagged 'New' (strong match)
- {stats['active_companies']:,} active companies in the roster
- Recruiter emails: {stats['interviews']} interview signals, {stats['rejections']} rejections, {stats['acks']} acks
- Top-scoring fresh sponsor job: {stats['top_jobs'][0]['title'] if stats['top_jobs'] else 'none'} @ {stats['top_jobs'][0]['company_name'] if stats['top_jobs'] else '?'} (score {stats['top_jobs'][0]['match_score'] if stats['top_jobs'] else '?'})

Write the 2-3 sentence brief. Start with a verb. Focus on what MATTERS most today, not just a summary of numbers.
"""
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        # Sonnet 5 with extended thinking returns ThinkingBlock(s) + TextBlock(s).
        # Grab the FIRST text block; ignore ThinkingBlocks (they lack .text).
        for block in resp.content:
            if getattr(block, "type", None) == "text" or hasattr(block, "text"):
                try:
                    return block.text.strip()
                except AttributeError:
                    continue
        return ""
    except Exception as e:  # noqa: BLE001
        log.warning("AI summary failed: %s", e)
        return ""


def _build_email(stats: dict, ai_take: str, to_addr: str) -> EmailMessage:
    msg = EmailMessage()
    now_str = utcnow_naive().strftime("%A %b %-d")
    msg["Subject"] = (
        f"🌙 JCC morning brief · {now_str} · "
        f"{stats['new_24h']} new, {stats['interviews']} interviews, "
        f"{stats['rejections']} rejections"
    )
    msg["From"] = to_addr
    msg["To"] = to_addr

    # Plain-text fallback
    plain = [
        f"JCC morning brief for {now_str}",
        "",
        f"{ai_take}" if ai_take else "(AI summary disabled)",
        "",
        "-- Overnight numbers --",
        f"  Jobs crawled:      {stats['crawled_24h']:,}",
        f"  Survived filters:  {stats['survived_24h']:,}",
        f"  Strong 'New':      {stats['new_24h']}",
        f"  Active companies:  {stats['active_companies']:,}",
        "",
        "-- Inbox activity --",
        f"  🎯 Interviews:  {stats['interviews']}",
        f"  ❌ Rejections:  {stats['rejections']} (auto-moved to Rejected page)",
        f"  📥 Acks:        {stats['acks']}",
        "",
        "-- Top 5 fresh sponsor jobs to attack first --",
    ]
    for j in stats["top_jobs"]:
        plain.append(f"  [{j['match_score']}] {j['title']} @ {j['company_name']} — {j['location']}")
        plain.append(f"      {j['job_url']}")
    plain.append("")
    plain.append(f"Open dashboard: {DASHBOARD_URL}")
    msg.set_content("\n".join(plain))

    # Rich HTML version
    ai_html = (f"<blockquote style='border-left:3px solid #0969da;padding:8px 16px;"
               f"margin:16px 0;color:#333;background:#f0f7ff;border-radius:4px'>"
               f"{ai_take}</blockquote>") if ai_take else ""
    top_html = "".join(
        f"<tr>"
        f"<td style='padding:8px 10px;background:#1a7f37;color:#fff;font-weight:700;border-radius:4px'>{j['match_score']}</td>"
        f"<td style='padding:8px 12px'><b>{j['title']}</b><br>"
        f"<span style='color:#666'>{j['company_name']} · {j['location']}</span><br>"
        f"<a href='{j['job_url']}'>Apply →</a></td>"
        f"</tr>"
        for j in stats["top_jobs"]
    )
    html = f"""<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:680px;margin:0 auto;padding:20px;color:#24292f">
    <h2 style="margin-top:0">🌙 JCC morning brief</h2>
    <p style="color:#666;font-size:14px">{now_str}</p>
    {ai_html}
    <h3>📊 Overnight numbers</h3>
    <table style="border-collapse:collapse;font-size:14px">
      <tr><td style="padding:4px 12px;color:#666">Jobs crawled</td><td style="padding:4px 12px;font-weight:600">{stats['crawled_24h']:,}</td></tr>
      <tr><td style="padding:4px 12px;color:#666">Survived filters</td><td style="padding:4px 12px;font-weight:600">{stats['survived_24h']:,}</td></tr>
      <tr><td style="padding:4px 12px;color:#666">Strong 'New' matches</td><td style="padding:4px 12px;font-weight:600">{stats['new_24h']}</td></tr>
      <tr><td style="padding:4px 12px;color:#666">Active companies</td><td style="padding:4px 12px;font-weight:600">{stats['active_companies']:,}</td></tr>
    </table>
    <h3>📬 Inbox activity</h3>
    <table style="border-collapse:collapse;font-size:14px">
      <tr><td style="padding:4px 12px;color:#666">🎯 Interviews</td><td style="padding:4px 12px;font-weight:600;color:#1a7f37">{stats['interviews']}</td></tr>
      <tr><td style="padding:4px 12px;color:#666">❌ Rejections (auto-moved)</td><td style="padding:4px 12px;font-weight:600;color:#b91c1c">{stats['rejections']}</td></tr>
      <tr><td style="padding:4px 12px;color:#666">📥 Acks</td><td style="padding:4px 12px;font-weight:600">{stats['acks']}</td></tr>
    </table>
    <h3>🎯 Top 5 fresh sponsor jobs to attack first</h3>
    <table style="border-collapse:collapse;width:100%;font-size:14px">{top_html or '<tr><td>No new sponsor jobs in the last 24h — check ⚡ Fast Apply for the backlog.</td></tr>'}</table>
    <p style="margin-top:24px"><a href="{DASHBOARD_URL}" style="color:#0969da;text-decoration:none;font-weight:600">Open dashboard →</a></p>
    </body></html>
    """
    msg.add_alternative(html, subtype="html")
    return msg


def run() -> dict:
    gmail = _load_gmail()
    if not gmail or not gmail.get("email") or not gmail.get("app_password"):
        log.info("morning_brief: gmail not configured, skip")
        return {"configured": False}

    stats = _stats_last_24h()
    ai_take = _ai_summary(stats)
    msg = _build_email(stats, ai_take, gmail["email"])

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(gmail["email"], gmail["app_password"])
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        log.warning("morning_brief: SMTP send failed: %s", e)
        return {"configured": True, "sent": 0, "error": str(e)}

    log.info("morning_brief: sent to %s (crawled=%d, new=%d, interviews=%d, rejections=%d)",
             gmail["email"], stats["crawled_24h"], stats["new_24h"],
             stats["interviews"], stats["rejections"])
    return {"configured": True, "sent": 1, "ai_take_len": len(ai_take)}


if __name__ == "__main__":
    raise SystemExit(0 if run().get("sent") else 0)
