"""
crawlers/jobspresso.py
----------------------
Jobspresso (jobspresso.co) public RSS job feed — no key, no auth:

    list: https://jobspresso.co/?feed=job_feed&posts_per_page=100&paged=1

This is a SENTINEL / AGGREGATOR source (like Remotive / Jobicy / Himalayas): it
lives as a SINGLE company row in the DB (name="Jobspresso", career_url=
"jobspresso", ats_type="jobspresso"). fetch_jobs IGNORES company.career_url
entirely and hits the public feed. Each item carries its OWN employer, so
normalize_job sets company_name from the PARSED employer — NOT company.name —
and the dedupe hash is built on employer + title + location + url so two
different employers can't collide.

FEED SHAPE (curl-verified 2026-07-22). Jobspresso runs WordPress + WP Job
Manager, so this is a plain RSS 2.0 channel whose <item>s carry extra
job_listing fields in a site-specific namespace (xmlns:job_listing=
"https://jobspresso.co"):

    <item>
      <title>Content Reviewer &#8211; United States</title>
      <link>https://jobspresso.co/job/telus-digital-.../</link>
      <dc:creator><![CDATA[TELUS Digital<br>&#9906;&nbsp;USA]]></dc:creator>
      <pubDate>Wed, 22 Jul 2026 13:51:12 +0000</pubDate>
      <guid isPermaLink="false">https://jobspresso.co/?post_type=job_listing&p=163370</guid>
      <description><![CDATA[ ...excerpt... ]]></description>
      <content:encoded><![CDATA[ ...full HTML body... ]]></content:encoded>
      <job_listing:company>TELUS Digital</job_listing:company>
      <job_listing:location>USA</job_listing:location>
      <job_listing:job_type>Others, Support</job_listing:job_type>
      <job_listing:job_category>...</job_listing:job_category>
    </item>

COMPANY: taken from the dedicated <job_listing:company> element. dc:creator
holds the same name but glued to the location with markup ("TELUS Digital<br>
&#9906;&nbsp;USA"), so it is only a last-resort fallback (split on the <br>).

PAGINATION: the feed defaults to only 10 items, but WordPress honours
`posts_per_page` and `paged` on feed requests (both verified: posts_per_page=100
returns 100 items, paged=2/3 return further pages of older postings). We walk
pages newest-first and stop on a short/empty page.

posted_at comes from <pubDate> (RFC-822; parse_date handles it) — the real
PUBLISH date, never the channel's lastBuildDate and never now(). If an item has
no parseable pubDate, posted_at stays None rather than silently becoming crawl
time (a crawl-time fallback makes 2-year-old jobs look brand new).

description prefers <content:encoded> (the full HTML body) over <description>
(a short excerpt), then clean_html + truncate.

NO geo filter here: <job_listing:location> is free text ("USA", "Germany ,
Austria, Luxemburg", "Canada"), and the shared filter_engine already runs on
every crawled job in the scheduler pipeline and knows how to read those. A
crude crawler-side token filter would only add a second, worse copy of that
logic.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

FEED = "https://jobspresso.co/?feed=job_feed&posts_per_page={per_page}&paged={page}"
PER_PAGE = 100   # verified: WP honours posts_per_page on the feed (default is 10)
# Jobspresso is a small board: page 1 alone already reaches ~11 months back
# (measured 2026-07-22), and pages 3+ are entirely >1yr-old postings the pruner
# would drop anyway. 2 pages is plenty of headroom over the retention window.
MAX_PAGES = 2

# XML namespaces used by the item children. "job_listing" is Jobspresso's own
# site namespace (declared as xmlns:job_listing="https://jobspresso.co"), which
# is why the URI below is the bare site URL and not a spec URL.
NS = {
    "job_listing": "https://jobspresso.co",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def _text(item: ET.Element, path: str) -> str:
    """Text of a child element, or "" if absent/empty."""
    return (item.findtext(path, default="", namespaces=NS) or "").strip()


def _employer_from_creator(creator: str) -> str:
    """Fallback employer parse for items missing <job_listing:company>.

    dc:creator packs employer + location into one markup blob
    ("TELUS Digital<br>⚲&nbsp;USA"), so we keep only the part before the <br>.
    """
    head = re.split(r"<br\s*/?>", creator, maxsplit=1)[0]
    return clean_html(head).strip()


class JobspressoCrawler(BaseCrawler):
    source_name = "jobspresso"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "jobspresso" or "jobspresso.co" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        """Page the public RSS feed newest-first. Ignores company.career_url.

        Dedupes by listing URL across pages (a posting bumped between two page
        requests can otherwise show up twice) and stops early on a short/empty
        page so we don't hammer the site past the end of the feed.
        """
        seen_urls: set = set()
        out: List[Dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            xml = self._get(FEED.format(per_page=PER_PAGE, page=page)).content
            channel = ET.fromstring(xml).find("channel")
            items = channel.findall("item") if channel is not None else []
            if not items:
                break
            for item in items:
                url = _text(item, "link")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                out.append(
                    {
                        "title": _text(item, "title"),
                        "company": _text(item, "job_listing:company"),
                        "creator": _text(item, "dc:creator"),
                        "location": _text(item, "job_listing:location"),
                        "job_type": _text(item, "job_listing:job_type"),
                        "url": url,
                        # content:encoded is the full body; description is a
                        # short excerpt used only when the body is missing.
                        "description": _text(item, "content:encoded") or _text(item, "description"),
                        "pub_date": _text(item, "pubDate"),
                    }
                )
            if len(items) < PER_PAGE:
                break  # reached the end of the feed
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        # Aggregator: the employer comes from the PARSED item, not company.name.
        employer = raw.get("company") or _employer_from_creator(raw.get("creator") or "")
        title = (raw.get("title") or "").strip()
        location = (raw.get("location") or "").strip()
        job_url = (raw.get("url") or "").strip()
        description = truncate(clean_html(raw.get("description") or ""))
        # REAL publish date only. parse_date returns None on junk — keep that
        # None instead of falling back to now().
        posted_at = parse_date(raw.get("pub_date"))
        employment_type = (raw.get("job_type") or "").strip()

        return Job(
            company_id=company.id,
            title=title,
            company_name=employer,            # PARSED employer, not company.name
            location=location,
            employment_type=employment_type,
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=posted_at,
            raw_data_hash=make_hash(employer, title, location, job_url),
        )
