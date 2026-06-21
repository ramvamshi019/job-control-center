"""
crawlers/gem.py
---------------
Gem ATS public job board (no key needed). Gem hosts client-rendered career
boards at jobs.gem.com/{slug} backed by a single GraphQL endpoint:

    POST https://jobs.gem.com/api/public/graphql/batch

CRITICAL: the request MUST carry the header {'batch': 'true'} and the body MUST
be a JSON ARRAY of operations; the endpoint returns a JSON array of results.
(Without the batch header the server falls back to a non-batch handler that does
not understand the array body.)

The `boardId` GraphQL variable IS the vanity slug from the board URL, e.g.
"retool" from https://jobs.gem.com/retool — the listing query passes the same
value to both oatsExternalJobPostings(boardId:) and
jobBoardExternal(vanityUrlPath:). We accept any of these in company.career_url:
    retool
    https://jobs.gem.com/retool
    https://jobs.gem.com/retool/4003629005   (a per-job URL -> slug = "retool")

Two queries (exact bodies lifted from the live jobBoards JS bundle and verified
against the retool + gem boards):
  * JobBoardList($boardId)         -> titles / locations / extIds (the LIVE path)
  * ExternalJobPostingQuery(...)   -> descriptionHtml + firstPublishedTsSec

The list response has NO date or description, so we stamp those from the
per-posting detail query in enrich_posted_date(), which the pipeline calls for
NEW jobs only (post-dedupe) — same light-live-path pattern as Rippling/BambooHR.
posted_at comes from firstPublishedTsSec (epoch SECONDS).

Gem boards skew to name-brand US tech (Retool, Gem, ...), exactly the
data/software-engineer segment, and these postings often reach aggregators late.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List
from urllib.parse import quote, unquote

from app.crawlers.base import BaseCrawler
from app.config import settings
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

GRAPHQL = "https://jobs.gem.com/api/public/graphql/batch"
JOB_URL = "https://jobs.gem.com/{slug}/{ext_id}"

# Exact operation bodies from the live jobBoards bundle (trimmed to the fields we
# use). Keep the operationName so server-side logging/persisted-query routing is
# happy and the shape matches what the real SPA sends.
LIST_QUERY = """
query JobBoardList($boardId: String!) {
  oatsExternalJobPostings(boardId: $boardId) {
    jobPostings {
      id
      extId
      title
      locations { name city isoCountry isRemote }
      job {
        locationType
        employmentType
        department { name }
      }
    }
  }
  jobBoardExternal(vanityUrlPath: $boardId) {
    id
    teamDisplayName
  }
}
"""

DETAIL_QUERY = """
query ExternalJobPostingQuery($boardId: String!, $extId: String!) {
  oatsExternalJobPosting(boardId: $boardId, extId: $extId) {
    id
    title
    descriptionHtml
    firstPublishedTsSec
    startDateTs
    job { employmentType }
  }
}
"""


def extract_token(career_url: str) -> str:
    """Get the Gem vanity slug from a board/job URL or a bare slug.

    jobs.gem.com/retool                -> retool
    jobs.gem.com/retool/4003629005     -> retool   (per-job URL)
    https://jobs.gem.com/retool/       -> retool
    retool                             -> retool
    """
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    m = re.search(r"jobs\.gem\.com/([^/?#]+)", s)
    if m:
        return m.group(1)
    if "/" not in s and "." not in s:
        return s  # already a bare slug
    return s.split("/")[-1]


class GemCrawler(BaseCrawler):
    source_name = "gem"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "gem" or "jobs.gem.com" in s

    def _graphql(self, query: str, variables: Dict[str, Any], op_name: str) -> Dict[str, Any]:
        """POST a single operation through Gem's batch endpoint.

        The endpoint demands the {'batch': 'true'} header and an ARRAY body, and
        replies with an ARRAY. We send one op and return its `data` dict (or {}).
        """
        time.sleep(max(0.0, settings.crawl_delay_seconds))
        payload = [{"operationName": op_name, "variables": variables, "query": query}]
        resp = self.session.post(
            GRAPHQL,
            json=payload,
            headers={"batch": "true", "Content-Type": "application/json"},
            timeout=settings.request_timeout_seconds,
        )
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, list) or not body:
            return {}
        return body[0].get("data") or {}

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        slug = extract_token(company.career_url)
        if not slug:
            return []
        data = self._graphql(LIST_QUERY, {"boardId": slug}, "JobBoardList")
        postings = ((data.get("oatsExternalJobPostings") or {}).get("jobPostings")) or []
        # Stash the slug on each raw posting so normalize_job can build the URL
        # without re-parsing career_url.
        for p in postings:
            p["_slug"] = slug
        return postings

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        slug = raw.get("_slug") or extract_token(company.career_url)
        ext_id = str(raw.get("extId") or "")
        title = (raw.get("title") or "").strip()

        locs = raw.get("locations") or []
        location = ", ".join(
            (l.get("name") or "").strip() for l in locs if l.get("name")
        )
        if not location and any(l.get("isRemote") for l in locs):
            location = "Remote"

        job_url = JOB_URL.format(slug=slug, ext_id=quote(ext_id, safe="")) if ext_id else ""
        employment_type = str((raw.get("job") or {}).get("employmentType") or "")

        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type=employment_type,
            job_url=job_url,
            source=self.source_name,
            description="",      # filled by enrich_posted_date (detail call)
            posted_at=None,      # filled by enrich_posted_date (firstPublishedTsSec)
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )

    def enrich_posted_date(self, job: Job) -> Job:
        """List query has no date/description — fetch them from the per-posting
        detail query. Called by the pipeline for NEW jobs only (post-dedupe), so
        it stays cheap in steady state. On failure the job keeps posted_at=None
        (kept, not pruned) so a transient detail error never loses a posting."""
        m = re.search(r"jobs\.gem\.com/([^/]+)/([^/?#]+)", job.job_url or "")
        if not m:
            return job
        slug, ext_id = m.group(1), m.group(2)
        # ext_id was URL-encoded into the path; decode for the GraphQL variable.
        ext_id = unquote(ext_id)
        try:
            data = self._graphql(
                DETAIL_QUERY,
                {"boardId": slug, "extId": ext_id},
                "ExternalJobPostingQuery",
            )
        except Exception:  # noqa: BLE001 - keep None on failure
            return job
        jp = data.get("oatsExternalJobPosting") or {}
        if not jp:
            return job

        # firstPublishedTsSec is epoch SECONDS; startDateTs is a rare fallback.
        posted = parse_date(jp.get("firstPublishedTsSec")) or parse_date(jp.get("startDateTs"))
        if posted:
            job.posted_at = posted

        desc = jp.get("descriptionHtml") or ""
        if desc:
            job.description = truncate(clean_html(desc))

        et = str((jp.get("job") or {}).get("employmentType") or "")
        if et:
            job.employment_type = et
        return job
