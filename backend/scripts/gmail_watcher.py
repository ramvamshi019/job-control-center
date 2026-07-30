"""
scripts/gmail_watcher.py
------------------------
IMAP poller that watches Gmail for recruiter responses to jobs the user has
marked Applied in JCC. Called by discovery_loop.py every ~15 min.

Auth: Gmail App Password (not OAuth). User creates one at:
    https://myaccount.google.com/apppasswords
Stored in /app/backend/data/db/gmail_settings.json (persistent volume, gitignored,
never in git). Same trust boundary as the DB.

What it does per poll:
  1. Read gmail_settings.json for (email, app_password)
  2. Connect to imap.gmail.com over SSL, select INBOX
  3. Fetch messages RECEIVED since the last poll timestamp (state.json)
  4. For each new message: extract From-domain + subject + snippet
  5. Match against Applied jobs' company domains
     (careers@X.com hit means recruiter@X.com response also matches)
  6. Classify as one of: interview | rejection | ack | other
  7. Persist to `job_messages` table (created on first run if missing)
  8. Optionally auto-move rejections to Rejected status

The classifier is intentionally rules-based (fast, no API cost) with clear
keywords -- upgrade to Claude/OpenAI later if you want smarter triage.
"""
from __future__ import annotations

import email as email_pkg
import imaplib
import json
import os
import re
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from sqlalchemy import text  # noqa: E402
from sqlmodel import select  # noqa: E402

from app.database import engine, session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.models.job import Job  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("gmail_watcher")

SETTINGS_PATH = "/app/backend/data/db/gmail_settings.json"
STATE_PATH    = "/app/backend/data/db/gmail_watcher_state.json"
IMAP_HOST     = "imap.gmail.com"
IMAP_PORT     = 993

# --- Rules-based classifier -------------------------------------------------
_REJECT_PATTERNS = (
    r"\bnot moving forward\b", r"\bnot proceed(ing)?\b", r"\bnot select(ed)?\b",
    r"\bother candidate", r"\bunfortunately\b", r"\bregret to inform\b",
    r"\bnot a (?:match|fit)\b", r"\bdecided not to\b", r"\bpursu(ing|e) other\b",
    r"\bdifferent direction\b",
)
_INTERVIEW_PATTERNS = (
    # STRICT patterns -- must actually indicate scheduling / invitation.
    # Loose "next step" was demoted because it false-positived on generic
    # "we received your application, next steps in our process are..." ACKs
    # (e.g. Thetradedesk auto-reply hit INTERVIEW).
    r"\bschedule (?:an? )?(?:call|chat|interview|meeting|conversation|screen)\b",
    r"\binterview (?:invitation|invite|loop|coming up)\b",
    r"\bphone screen\b", r"\btechnical (?:screen|interview)\b",
    r"\bwould (?:you|we) (?:like to )?(?:schedule|invite|meet|chat)\b",
    r"\bwould you be (?:available|free) (?:for|to)\b",
    r"\binvite you to (?:an? )?(?:interview|screen|call|chat)\b",
    r"\bcalendly\.com\b", r"\bcal\.com/\b", r"\bpick a time\b", r"\bbook a time\b",
    r"\bplease share (?:your|some) availability\b",
)
_ACK_PATTERNS = (
    # "next step(s)" moved here -- generic "next steps in the process"
    # language is usually part of an ack email, not an interview invite.
    r"\bthank you for (?:your interest|applying|your application|considering)\b",
    r"\bapplication received\b", r"\bwe.?ve received your\b",
    r"\bwill review\b", r"\bunder review\b",
    r"\bnext step(?:s)? (?:in (?:our|the) process|will be)\b",
    r"\bwe.?ll be in touch\b", r"\bhold on to your resume\b",
)


def classify(subject: str, body: str) -> str:
    """Return one of: 'interview', 'rejection', 'ack', 'other'. Rules-based.

    Order matters: rejection > ack > interview. Ack wins over interview
    when both match because auto-reply ACKs commonly include scheduling-
    looking phrases they don't actually mean ('we may reach out to schedule').
    A REAL interview invite typically has ONLY scheduling language, not
    'thank you for applying' preamble.
    """
    blob = f"{subject}\n{body[:2000]}".lower()
    if any(re.search(p, blob) for p in _REJECT_PATTERNS):
        return "rejection"
    ack_hit = any(re.search(p, blob) for p in _ACK_PATTERNS)
    interview_hit = any(re.search(p, blob) for p in _INTERVIEW_PATTERNS)
    # If both match, ack wins -- auto-replies often contain scheduling-like
    # language they don't actually mean.
    if ack_hit and interview_hit:
        return "ack"
    if interview_hit:
        return "interview"
    if ack_hit:
        return "ack"
    return "other"


# --- Schema bootstrap -------------------------------------------------------
def ensure_schema():
    """Create job_messages table if missing. Idempotent."""
    with engine.connect() as c:
        c.execute(text("PRAGMA busy_timeout = 30000"))
        c.execute(text("""
            CREATE TABLE IF NOT EXISTS job_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id      INTEGER,
                company_id  INTEGER,
                imap_uid    INTEGER,
                from_addr   TEXT,
                from_domain TEXT,
                subject     TEXT,
                snippet     TEXT,
                received_at DATETIME,
                classification TEXT,
                created_at  DATETIME DEFAULT (datetime('now'))
            )
        """))
        c.execute(text("CREATE INDEX IF NOT EXISTS ix_job_messages_job_id ON job_messages(job_id)"))
        c.execute(text("CREATE INDEX IF NOT EXISTS ix_job_messages_uid ON job_messages(imap_uid)"))
        c.execute(text("CREATE INDEX IF NOT EXISTS ix_job_messages_class ON job_messages(classification)"))
        c.commit()


# --- Settings / state -------------------------------------------------------
def _load_settings() -> dict | None:
    if not os.path.exists(SETTINGS_PATH):
        return None
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        log.warning("could not read %s: %s", SETTINGS_PATH, e)
        return None


def _load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"last_uid": 0}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"last_uid": 0}


def _save_state(state: dict):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


# --- Domain matcher ---------------------------------------------------------
def _domain_of_email(addr: str) -> str:
    m = re.search(r"@([\w.-]+)", (addr or "").lower())
    return (m.group(1) if m else "").strip(".")


def _load_applied_domains() -> dict[str, tuple[int, int]]:
    """Return {domain: (job_id, company_id)} for all Applied jobs.

    Domain is guessed from company career_url + job_url + squashed name --
    same heuristic as the Applied page's follow-up guess. Skips ATS hosts
    (greenhouse/lever/etc.) since those are the middleman, not the employer."""
    ats_hosts = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
                 "icims.com", "bamboohr.com", "smartrecruiters.com", "workable.com",
                 "rippling.com", "recruitee.com", "himalayas.app", "jobvite.com")

    def _from_url(url: str) -> str:
        m = re.search(r"https?://([^/]+)", url or "")
        host = (m.group(1).lower() if m else "").replace("www.", "")
        return "" if any(a in host for a in ats_hosts) else host

    def _from_name(name: str) -> str:
        n = re.sub(r"\b(inc|corp|corporation|llc|ltd|limited|company|co|group)\b", " ",
                   (name or "").lower())
        n = re.sub(r"[^a-z0-9]+", "", n)
        return f"{n}.com" if 3 <= len(n) <= 30 else ""

    domains: dict[str, tuple[int, int]] = {}
    with session_scope() as s:
        applied_jobs = s.exec(select(Job).where(Job.status == "Applied")).all()
        for j in applied_jobs:
            company = s.exec(select(Company).where(Company.id == j.company_id)).first()
            candidates = []
            if company and company.career_url:
                candidates.append(_from_url(company.career_url))
            candidates.append(_from_url(j.job_url or ""))
            candidates.append(_from_name(j.company_name or ""))
            for d in candidates:
                if d:
                    domains[d] = (j.id, j.company_id or 0)
                    break  # first non-empty wins per job
    return domains


# --- IMAP fetch -------------------------------------------------------------
def _decode(hdr: str) -> str:
    parts = decode_header(hdr or "")
    out = []
    for text_, enc in parts:
        if isinstance(text_, bytes):
            try:
                out.append(text_.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                out.append(text_.decode("utf-8", errors="replace"))
        else:
            out.append(text_)
    return "".join(out)


def _plain_body(msg) -> str:
    """Extract plaintext body from a Message. Prefers text/plain part."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(errors="replace")
                except Exception:  # noqa: BLE001
                    return ""
        # Fallback: strip HTML from text/html
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html_ = part.get_payload(decode=True).decode(errors="replace")
                    return re.sub(r"<[^>]+>", " ", html_)
                except Exception:  # noqa: BLE001
                    return ""
    else:
        try:
            return msg.get_payload(decode=True).decode(errors="replace")
        except Exception:  # noqa: BLE001
            return ""
    return ""


def run() -> dict:
    """One end-to-end poll. Returns summary counts."""
    settings = _load_settings()
    if not settings or not settings.get("email") or not settings.get("app_password"):
        log.info("no Gmail settings yet -- skipping poll (set up at /Gmail Settings)")
        return {"configured": False}

    ensure_schema()
    state = _load_state()
    last_uid = int(state.get("last_uid") or 0)

    applied_domains = _load_applied_domains()
    log.info("gmail poll: watching for messages from %d applied-company domains",
             len(applied_domains))
    if not applied_domains:
        return {"configured": True, "applied": 0}

    try:
        ctx = ssl.create_default_context()
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx)
        M.login(settings["email"], settings["app_password"])
    except Exception as e:  # noqa: BLE001
        log.warning("gmail login failed: %s", e)
        return {"configured": True, "error": "login_failed"}

    matched, new_reject, new_interview = 0, 0, 0
    try:
        M.select("INBOX", readonly=True)
        # Fetch UIDs newer than last seen -- Gmail assigns monotonic UIDs.
        criteria = f"UID {last_uid + 1}:*" if last_uid else "SINCE " + \
                   (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=14)).strftime("%d-%b-%Y")
        typ, data = M.uid("search", None, criteria)
        if typ != "OK":
            return {"configured": True, "error": "search_failed"}
        uids = data[0].split() if data and data[0] else []
        # Cap the batch so a long backlog doesn't stall the poll.
        uids = uids[-500:]
        max_uid_seen = last_uid
        for uid in uids:
            uid_i = int(uid)
            if uid_i <= last_uid:
                continue
            max_uid_seen = max(max_uid_seen, uid_i)
            typ, msg_data = M.uid("fetch", uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email_pkg.message_from_bytes(raw)
            from_addr = _decode(msg.get("From", ""))
            subject = _decode(msg.get("Subject", ""))
            from_domain = _domain_of_email(from_addr)
            if not from_domain:
                continue

            # Match: from_domain must match (or be a subdomain of) an applied
            # company's guessed domain. e.g. recruiter@stripe.com vs stripe.com.
            match = None
            for dom, (job_id, comp_id) in applied_domains.items():
                if from_domain == dom or from_domain.endswith("." + dom):
                    match = (job_id, comp_id)
                    break
            if not match:
                continue

            job_id, comp_id = match
            body = _plain_body(msg)
            snippet = re.sub(r"\s+", " ", body).strip()[:500]
            cls = classify(subject, body)
            date_hdr = msg.get("Date", "")
            try:
                received_at = email_pkg.utils.parsedate_to_datetime(date_hdr)
            except Exception:  # noqa: BLE001
                received_at = datetime.now(timezone.utc).replace(tzinfo=None)

            # Insert into job_messages if not already seen
            with engine.connect() as c:
                c.execute(text("PRAGMA busy_timeout = 30000"))
                dup = c.execute(
                    text("SELECT 1 FROM job_messages WHERE imap_uid = :u"),
                    {"u": uid_i},
                ).first()
                if dup:
                    continue
                c.execute(text("""
                    INSERT INTO job_messages
                      (job_id, company_id, imap_uid, from_addr, from_domain,
                       subject, snippet, received_at, classification)
                    VALUES
                      (:jid, :cid, :uid, :from_addr, :from_dom,
                       :subj, :snip, :rcv, :cls)
                """), {
                    "jid": job_id, "cid": comp_id, "uid": uid_i,
                    "from_addr": from_addr[:200], "from_dom": from_domain[:100],
                    "subj": subject[:300], "snip": snippet,
                    "rcv": received_at, "cls": cls,
                })
                c.commit()
            matched += 1
            if cls == "rejection":
                new_reject += 1
            elif cls == "interview":
                new_interview += 1

        if max_uid_seen > last_uid:
            state["last_uid"] = max_uid_seen
            _save_state(state)
    finally:
        try: M.logout()
        except Exception: pass  # noqa: BLE001

    log.info("gmail poll: matched=%d interview=%d rejection=%d (last_uid=%d)",
             matched, new_interview, new_reject, state.get("last_uid", 0))
    return {"configured": True, "matched": matched,
            "interview": new_interview, "rejection": new_reject}


if __name__ == "__main__":
    run()
