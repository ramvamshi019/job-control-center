"""
crawlers/hn_hiring.py
---------------------
Hacker News "Ask HN: Who is hiring?" — a SENTINEL / aggregator source (ONE DB
row, not per-company). Many roles are posted ONLY here, frequently with explicit
visa / OPT / sponsorship mentions, so it's a high-signal source-first feed.

How it works (no key needed, public HN Algolia API):
    1. Find the newest "Who is hiring?" STORY. The official monthly thread is
       always posted by the `whoishiring` account and titled
       "Ask HN: Who is hiring? (Month Year)". The plain relevance-ranked search
       endpoint does NOT reliably surface the latest thread (it can rank an old
       high-traffic thread first), so we query the DATE-sorted endpoint scoped
       to the `whoishiring` author and take the newest story whose title says
       "who is hiring" (NOT "who wants to be hired", the job-SEEKER thread):
           https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&query=who%20is%20hiring
       If that ever returns nothing we fall back to the plain search endpoint
       named in the original spec:
           https://hn.algolia.com/api/v1/search?query=Ask%20HN%20Who%20is%20hiring&tags=story
    2. Pull that story's full item tree and take its top-level children
       (the comments — each comment is one company's posting):
           https://hn.algolia.com/api/v1/items/{story_id}

Each comment's first line is, by HN convention:
    Company | Role | LOCATION | REMOTE | $salary
We parse that header by `|`: field 0 = employer, field 1 = role/title,
field 2 = location. company_name is the PARSED employer (NOT company.name),
job_url is the comment permalink, description is the cleaned comment HTML,
posted_at is the comment's created_at. Comments with no parseable employer
(meta chatter, "[flagged]", section markers, etc.) are skipped.

make_hash(employer, title, location, job_url) so the same posting dedupes
across re-crawls even though the sentinel Company row never changes.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

# Date-sorted, scoped to the official monthly-thread account -> newest first.
SEARCH_BY_DATE = (
    "https://hn.algolia.com/api/v1/search_by_date"
    "?tags=story,author_whoishiring&query=who%20is%20hiring&hitsPerPage=20"
)
# Fallback (relevance-ranked) endpoint named in the original spec.
SEARCH = "https://hn.algolia.com/api/v1/search?query=Ask%20HN%20Who%20is%20hiring&tags=story"
ITEM = "https://hn.algolia.com/api/v1/items/{id}"

# Headers / lines that are clearly not a job post.
_SKIP_PREFIXES = ("[flagged", "[dead", "[deleted")


def _newest_hiring_story_id(hits: List[Dict[str, Any]]) -> str:
    """From the search hits, return the objectID of the newest genuine
    "Who is hiring?" story (employer thread), ignoring the "Who wants to be
    hired?" seeker thread. Algolia's default ranking is by relevance, not date,
    so we sort the candidates by created_at_i descending ourselves."""
    candidates = []
    for h in hits or []:
        title = (h.get("title") or "").lower()
        if "who is hiring" not in title:
            continue
        if "wants to be hired" in title:  # seeker thread — not employers
            continue
        oid = h.get("objectID")
        if not oid:
            continue
        # created_at_i is an epoch int; fall back to 0 so it sorts last.
        candidates.append((h.get("created_at_i") or 0, str(oid)))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _header_line(text_html: str) -> str:
    """The first line of a comment = everything before the first <p>, with
    anchor text preserved and other tags stripped, entities decoded."""
    if not text_html:
        return ""
    head = re.split(r"<p\b[^>]*>", text_html, maxsplit=1)[0]
    # keep the visible text of links, drop the rest of the markup
    head = re.sub(r"<a\b[^>]*>(.*?)</a>", r"\1", head, flags=re.I | re.S)
    head = re.sub(r"<[^>]+>", " ", head)
    head = unescape(head)
    return re.sub(r"\s+", " ", head).strip()


def _parse_header(header_text: str):
    """Return (employer, role, location) parsed from the pipe-delimited header,
    or None if no employer is parseable (=> skip the comment)."""
    if not header_text:
        return None
    low = header_text.lower()
    if low.startswith(_SKIP_PREFIXES):
        return None
    parts = [p.strip() for p in header_text.split("|")]
    parts = [p for p in parts if p]
    # Need at least an employer + one more field to be a real posting header.
    if len(parts) < 2:
        return None
    employer = parts[0]
    if not employer:
        return None
    role = parts[1] if len(parts) > 1 else ""
    location = parts[2] if len(parts) > 2 else ""
    return employer, role, location


class HNHiringCrawler(BaseCrawler):
    source_name = "hn_hiring"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "hn_hiring"

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        # IGNORE company.career_url — this is a sentinel source.
        # Primary: date-sorted, author-scoped -> the genuine newest thread.
        search = self._get(SEARCH_BY_DATE).json()
        story_id = _newest_hiring_story_id(search.get("hits", []) or [])
        if not story_id:
            # Fallback to the relevance endpoint if the scoped query is empty.
            search = self._get(SEARCH).json()
            story_id = _newest_hiring_story_id(search.get("hits", []) or [])
        if not story_id:
            return []
        item = self._get(ITEM.format(id=story_id)).json()
        children = item.get("children", []) or []
        # Only top-level comments are company postings; keep ones with text.
        return [c for c in children if c and c.get("text")]

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        text_html = raw.get("text") or ""
        parsed = _parse_header(_header_line(text_html))
        if not parsed:
            # No employer parseable -> not a job post. Raise so base.crawl()
            # logs+skips this one (same contract as other crawlers).
            raise ValueError("no parseable employer in HN comment")
        employer, role, location = parsed

        comment_id = raw.get("id")
        job_url = f"https://news.ycombinator.com/item?id={comment_id}"
        title = role or "(see posting)"
        description = truncate(clean_html(text_html))
        posted_at = parse_date(raw.get("created_at"))

        return Job(
            company_id=company.id,
            title=title,
            company_name=employer,            # PARSED employer, not company.name
            location=location,
            employment_type="",
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=posted_at,
            raw_data_hash=make_hash(employer, title, location, job_url),
        )
