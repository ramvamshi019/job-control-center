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
        # Prefer AI-ranked top 5 from yesterday's MISSED backlog (jobs
        # posted 24-48h ago that Ram never actioned). Fall back to
        # heuristic top-5 fresh sponsor jobs if the ranker hasn't run.
        d48 = now - timedelta(hours=48)
        try:
            ai_top = c.execute(text("""
                SELECT j.id, j.title, j.company_name, j.location, j.job_url,
                       j.match_score, r.fit_score, r.reasons, r.pitch_line
                FROM job_ai_ranking r
                JOIN jobs j ON j.id = r.job_id
                WHERE j.discovered_at >= :d48 AND j.discovered_at < :d24
                  AND j.status IN ('New', 'Need Review')
                ORDER BY r.fit_score DESC, j.discovered_at DESC
                LIMIT 5
            """), {"d24": d24, "d48": d48}).all()
        except Exception:
            ai_top = []
        if ai_top:
            top = [dict(r._mapping) for r in ai_top]
            ai_ranked = True
            # Enrich each top job with HN buzz (populated by hn_buzz.py at 05:20 UTC)
            try:
                buzz_rows = c.execute(text(
                    "SELECT company_name, mentions FROM hn_buzz WHERE company_name IN "
                    "(" + ",".join(f":c{i}" for i in range(len(top))) + ")"
                ), {f"c{i}": t["company_name"] for i, t in enumerate(top)}).all()
                import json as _json
                buzz_map = {r[0]: _json.loads(r[1] or "[]") for r in buzz_rows}
                for t in top:
                    t["hn_buzz"] = buzz_map.get(t["company_name"], [])
            except Exception:
                for t in top:
                    t["hn_buzz"] = []
        else:
            top_raw = c.execute(text("""
                SELECT j.id, j.title, j.company_name, j.location, j.job_url, j.match_score
                FROM jobs j
                JOIN companies co ON co.id = j.company_id
                WHERE j.discovered_at >= :d
                  AND j.status = 'New'
                  AND co.h1b_history_score >= 50
                ORDER BY j.match_score DESC, j.discovered_at DESC
                LIMIT 5
            """), {"d": d24}).all()
            top = [dict(r._mapping) for r in top_raw]
            ai_ranked = False
        # Follow-up priority: Applied jobs >= 7 days old with no reply. Rank by
        # (match_score + days_stale, capped) so a strong-fit application from
        # 7d ago beats a weak-fit from 30d. LEFT JOIN drops any job that has
        # gotten a message in job_messages (recruiter response of any kind).
        d7 = now - timedelta(days=7)
        try:
            followups = c.execute(text("""
                SELECT j.id, j.title, j.company_name, j.location, j.job_url,
                       j.match_score, j.updated_at,
                       CAST(julianday(:now) - julianday(j.updated_at) AS INTEGER) AS days_stale
                FROM jobs j
                LEFT JOIN job_messages m ON m.job_id = j.id
                WHERE j.status = 'Applied'
                  AND j.updated_at < :d7
                  AND m.id IS NULL
                ORDER BY (j.match_score + MIN(CAST(julianday(:now) - julianday(j.updated_at) AS INTEGER), 30)) DESC
                LIMIT 5
            """), {"now": now, "d7": d7}).all()
        except Exception:
            followups = []
        # Total waiting count for the header line
        try:
            total_waiting = c.execute(text("""
                SELECT COUNT(*) FROM jobs j
                LEFT JOIN job_messages m ON m.job_id = j.id
                WHERE j.status = 'Applied' AND j.updated_at < :d7 AND m.id IS NULL
            """), {"d7": d7}).scalar() or 0
        except Exception:
            total_waiting = 0

        # Filter sanity check: last night's sample of rejected jobs Claude
        # thinks should have been kept (false-negatives). Table populated
        # by filter_sanity_check.py at 05:15 UTC.
        try:
            fs_since = now - timedelta(hours=6)  # only THIS morning's sample
            keep_rows = c.execute(text("""
                SELECT s.job_id, s.reason, j.title, j.company_name, j.job_url
                FROM filter_sanity_check s
                JOIN jobs j ON j.id = s.job_id
                WHERE s.verdict = 'keep' AND s.sampled_at >= :d
                ORDER BY s.sampled_at DESC
                LIMIT 5
            """), {"d": fs_since}).all()
            fs_total = c.execute(text(
                "SELECT COUNT(*) FROM filter_sanity_check WHERE sampled_at >= :d"
            ), {"d": fs_since}).scalar() or 0
            fs_keep = c.execute(text(
                "SELECT COUNT(*) FROM filter_sanity_check WHERE sampled_at >= :d AND verdict = 'keep'"
            ), {"d": fs_since}).scalar() or 0
            false_neg_rate = round(100 * fs_keep / fs_total, 1) if fs_total else 0
        except Exception:
            keep_rows, fs_total, fs_keep, false_neg_rate = [], 0, 0, 0

        # Application pace: apps today, this week, 7-day rolling avg, days since last app.
        # 'Applied' status flip is our proxy — updated_at is when it flipped.
        d1 = now - timedelta(days=1)
        d7 = now - timedelta(days=7)
        d14 = now - timedelta(days=14)
        try:
            apps_24h = c.execute(text(
                "SELECT COUNT(*) FROM jobs WHERE status='Applied' AND updated_at >= :d"
            ), {"d": d1}).scalar() or 0
            apps_7d = c.execute(text(
                "SELECT COUNT(*) FROM jobs WHERE status='Applied' AND updated_at >= :d"
            ), {"d": d7}).scalar() or 0
            apps_prev_7d = c.execute(text(
                "SELECT COUNT(*) FROM jobs WHERE status='Applied' "
                "AND updated_at >= :d14 AND updated_at < :d7"
            ), {"d14": d14, "d7": d7}).scalar() or 0
            last_app_row = c.execute(text(
                "SELECT updated_at FROM jobs WHERE status='Applied' "
                "ORDER BY updated_at DESC LIMIT 1"
            )).fetchone()
            if last_app_row and last_app_row[0]:
                # SQLite datetime column comes back as string. Parse via julianday to get days.
                days_since_last = c.execute(text(
                    "SELECT CAST(julianday(:now) - julianday(:t) AS INTEGER)"
                ), {"now": now, "t": last_app_row[0]}).scalar()
            else:
                days_since_last = None
        except Exception:
            apps_24h = apps_7d = apps_prev_7d = 0
            days_since_last = None
    return {
        "total_jobs": total_jobs, "crawled_24h": crawled_24h,
        "survived_24h": survived_24h, "new_24h": new_24h,
        "active_companies": active_companies,
        "interviews": interviews, "rejections": rejections, "acks": acks,
        "top_jobs": top, "ai_ranked": ai_ranked,
        "followups": [dict(r._mapping) for r in followups],
        "total_waiting": total_waiting,
        "filter_sanity": {
            "keep_rows": [dict(r._mapping) for r in keep_rows],
            "sampled": fs_total,
            "keep_count": fs_keep,
            "false_neg_rate": false_neg_rate,
        },
        "pace": {
            "apps_24h": apps_24h,
            "apps_7d": apps_7d,
            "apps_prev_7d": apps_prev_7d,
            "days_since_last": days_since_last,
        },
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

    pace = stats.get("pace") or {}
    drought_msg = ""
    if pace.get("days_since_last") is not None and pace["days_since_last"] >= 2:
        drought_msg = f"⚠️ DROUGHT: {pace['days_since_last']}d since your last application"
    elif pace.get("apps_7d", 0) < 5:
        drought_msg = f"⚠️ BEHIND PACE: only {pace['apps_7d']} apps in the last 7d"

    # Plain-text fallback
    plain = [
        f"JCC morning brief for {now_str}",
        "",
        f"{ai_take}" if ai_take else "(AI summary disabled)",
        "",
    ]
    if drought_msg:
        plain += [drought_msg, ""]
    plain += [
        "-- Application pace --",
        f"  Last 24h:    {pace.get('apps_24h', 0)}",
        f"  This week:   {pace.get('apps_7d', 0)}   (last week: {pace.get('apps_prev_7d', 0)})",
        f"  Days since last app: {pace.get('days_since_last', '?')}",
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
        f"-- Top 5 {'AI-ranked' if stats.get('ai_ranked') else 'heuristic'} sponsor jobs to attack first --",
    ]
    for j in stats["top_jobs"]:
        score_label = f"AI {j['fit_score']}" if stats.get("ai_ranked") and j.get("fit_score") is not None else str(j['match_score'])
        plain.append(f"  [{score_label}] {j['title']} @ {j['company_name']} — {j['location']}")
        plain.append(f"      {j['job_url']}")
        if stats.get("ai_ranked") and j.get("pitch_line"):
            plain.append(f"      Pitch: {j['pitch_line']}")
        for m in (j.get("hn_buzz") or [])[:2]:
            plain.append(f"      🗣️ HN ({m.get('points',0)}pt): {m['title']}")
            plain.append(f"         {m['url']}")
    plain.append("")
    fups = stats.get("followups") or []
    if fups:
        plain.append(f"-- 📮 Follow up on these today ({stats.get('total_waiting', 0)} total waiting >7d) --")
        for f in fups:
            plain.append(f"  [{f['days_stale']}d, score {f['match_score']}] {f['title']} @ {f['company_name']}")
            plain.append(f"      {f['job_url']}")
        plain.append("")
    fs = stats.get("filter_sanity") or {}
    if fs.get("sampled"):
        plain.append(f"-- 📊 Filter sanity ({fs['keep_count']}/{fs['sampled']} false-negatives = {fs['false_neg_rate']}%) --")
        if fs.get("keep_rows"):
            plain.append("  Rejected jobs Claude thinks you should see:")
            for r in fs["keep_rows"]:
                plain.append(f"    - {r['title']} @ {r['company_name']}")
                plain.append(f"      Why: {r['reason']}")
                plain.append(f"      {r['job_url']}")
        else:
            plain.append("  ✅ Filter looks tight — no false-negatives sampled.")
        plain.append("")
    plain.append(f"Open dashboard: {DASHBOARD_URL}")
    msg.set_content("\n".join(plain))

    # Rich HTML version
    ai_html = (f"<blockquote style='border-left:3px solid #0969da;padding:8px 16px;"
               f"margin:16px 0;color:#333;background:#f0f7ff;border-radius:4px'>"
               f"{ai_take}</blockquote>") if ai_take else ""
    ai_ranked = stats.get("ai_ranked")
    def _row(j):
        score = j.get('fit_score') if ai_ranked and j.get('fit_score') is not None else j.get('match_score')
        badge_bg = '#0969da' if ai_ranked else '#1a7f37'
        pitch = ""
        if ai_ranked and j.get('pitch_line'):
            pitch = (f"<div style='margin-top:6px;padding:6px 10px;background:#f6f8fa;"
                     f"border-left:2px solid #0969da;font-size:13px;color:#333;font-style:italic'>"
                     f"&ldquo;{j['pitch_line']}&rdquo;</div>")
        buzz = ""
        for m in (j.get("hn_buzz") or [])[:2]:
            buzz += (f"<div style='margin-top:4px;font-size:12px;color:#666'>"
                     f"🗣️ HN: <a href='{m['url']}' style='color:#0969da'>{m['title']}</a> "
                     f"<span style='color:#999'>({m.get('points',0)}pt)</span></div>")
        return (
            f"<tr><td style='padding:8px 10px;background:{badge_bg};color:#fff;"
            f"font-weight:700;border-radius:4px;vertical-align:top'>{score}</td>"
            f"<td style='padding:8px 12px'><b>{j['title']}</b><br>"
            f"<span style='color:#666'>{j['company_name']} · {j['location']}</span><br>"
            f"<a href='{j['job_url']}'>Apply →</a>{pitch}{buzz}</td></tr>"
        )
    top_html = "".join(_row(j) for j in stats["top_jobs"])
    top_heading = ("🚀 Top 5 AI-ranked apply queue"
                   if ai_ranked else "🎯 Top 5 fresh sponsor jobs to attack first")

    fups = stats.get("followups") or []
    total_waiting = stats.get("total_waiting", 0)
    fup_html = ""
    if fups:
        fup_rows = "".join(
            f"<tr>"
            f"<td style='padding:8px 10px;background:#9a6700;color:#fff;font-weight:700;"
            f"border-radius:4px;text-align:center;vertical-align:top'>"
            f"{f['days_stale']}d</td>"
            f"<td style='padding:8px 12px'><b>{f['title']}</b><br>"
            f"<span style='color:#666'>{f['company_name']} · {f['location']} · "
            f"score {f['match_score']}</span><br>"
            f"<a href='{f['job_url']}'>Open job →</a></td>"
            f"</tr>"
            for f in fups
        )
        fup_html = (
            f"<h3>📮 Follow up on these today "
            f"<span style='color:#666;font-weight:400;font-size:14px'>"
            f"({total_waiting} total waiting &gt;7d)</span></h3>"
            f"<table style='border-collapse:collapse;width:100%;font-size:14px'>{fup_rows}</table>"
        )

    fs = stats.get("filter_sanity") or {}
    fs_html = ""
    if fs.get("sampled"):
        rate = fs["false_neg_rate"]
        rate_color = "#1a7f37" if rate < 5 else "#9a6700" if rate < 15 else "#b91c1c"
        header = (f"<h3>📊 Filter sanity check "
                  f"<span style='color:{rate_color};font-weight:600'>"
                  f"({fs['keep_count']}/{fs['sampled']} false-negatives, {rate}%)</span></h3>")
        if fs.get("keep_rows"):
            body = "<p style='color:#666;font-size:13px'>Claude sampled rejected jobs and flagged these as ones you'd probably want to see:</p>"
            body += "".join(
                f"<div style='border-left:3px solid #9a6700;padding:8px 12px;margin:6px 0;"
                f"background:#fffbeb;font-size:14px'>"
                f"<b>{r['title']}</b> @ {r['company_name']}<br>"
                f"<span style='color:#666;font-style:italic'>{r['reason']}</span><br>"
                f"<a href='{r['job_url']}'>Open job →</a></div>"
                for r in fs["keep_rows"]
            )
        else:
            body = "<p style='color:#1a7f37;font-size:14px'>✅ Filter looks tight — Claude agreed with every reject in tonight's sample.</p>"
        fs_html = header + body
    # Drought banner
    drought_html = ""
    if drought_msg:
        drought_html = (f"<div style='background:#fee;border:2px solid #b91c1c;"
                        f"padding:12px 16px;border-radius:6px;margin:12px 0;"
                        f"color:#7f1d1d;font-weight:600'>{drought_msg}</div>")
    # Pace table
    delta = pace.get("apps_7d", 0) - pace.get("apps_prev_7d", 0)
    delta_arrow = "↑" if delta > 0 else "↓" if delta < 0 else "→"
    delta_color = "#1a7f37" if delta > 0 else "#b91c1c" if delta < 0 else "#666"
    pace_html = (
        f"<h3>⏱️ Application pace</h3>"
        f"<table style='border-collapse:collapse;font-size:14px'>"
        f"<tr><td style='padding:4px 12px;color:#666'>Last 24 hours</td>"
        f"<td style='padding:4px 12px;font-weight:600'>{pace.get('apps_24h', 0)}</td></tr>"
        f"<tr><td style='padding:4px 12px;color:#666'>This week</td>"
        f"<td style='padding:4px 12px;font-weight:600'>{pace.get('apps_7d', 0)} "
        f"<span style='color:{delta_color};font-size:12px'>({delta_arrow} {abs(delta)} vs last wk)</span></td></tr>"
        f"<tr><td style='padding:4px 12px;color:#666'>Days since last app</td>"
        f"<td style='padding:4px 12px;font-weight:600'>{pace.get('days_since_last', '?')}</td></tr>"
        f"</table>"
    )

    html = f"""<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:680px;margin:0 auto;padding:20px;color:#24292f">
    <h2 style="margin-top:0">🌙 JCC morning brief</h2>
    <p style="color:#666;font-size:14px">{now_str}</p>
    {drought_html}
    {ai_html}
    {pace_html}
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
    <h3>{top_heading}</h3>
    <table style="border-collapse:collapse;width:100%;font-size:14px">{top_html or '<tr><td>No new sponsor jobs in the last 24h — check ⚡ Fast Apply for the backlog.</td></tr>'}</table>
    {fup_html}
    {fs_html}
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
