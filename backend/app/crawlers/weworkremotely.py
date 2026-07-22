"""
crawlers/weworkremotely.py
--------------------------
We Work Remotely (weworkremotely.com) public RSS feeds — no key, no auth:

    all:      https://weworkremotely.com/remote-jobs.rss
    category: https://weworkremotely.com/categories/<slug>.rss

This is a SENTINEL / AGGREGATOR source (like Remotive / Jobicy / Himalayas): it
lives as a SINGLE company row in the DB (name="We Work Remotely",
career_url="weworkremotely", ats_type="weworkremotely"). fetch_jobs IGNORES
company.career_url entirely and hits the public feeds. Each item carries its OWN
employer, so normalize_job sets company_name from the PARSED employer — NOT
company.name — and the dedupe hash is built on employer + title + location + url
so two different employers can't collide.

FEED SHAPE (curl-verified 2026-07-22) — RSS 2.0 with custom, NON-namespaced
item children (region/country/state/type/…), so plain findtext() reaches them:

    <item>
      <title>Ameris Bank: Equipment Finance Regional Sales Manager</title>
      <region>California</region>
      <country>🇺🇸 United States of America</country>
      <state>Florida</state>
      <category>Management and Finance</category>
      <type>Full-Time</type>
      <description><![CDATA[ ...full HTML body... ]]></description>
      <pubDate>Wed, 22 Jul 2026 15:09:26 +0000</pubDate>
      <expires_at>Fri, 21 Aug 2026 15:09:26 +0000</expires_at>
      <link>https://weworkremotely.com/remote-jobs/ameris-bank-...</link>
    </item>

COMPANY: WWR has NO company element — the employer is prefixed onto <title> as
"Company: Job Title" (verified: 100/100 items in the main feed carry exactly one
colon in that position). We split on the FIRST colon; if an item ever lacks one,
the whole string stays as the title and company_name is left empty rather than
guessed at.

LOCATION: region/country/state are three separate free-text fields, any of which
may be empty ("Anywhere in the World" / "" / "Florida"). We join the non-empty
ones and strip the flag emoji WWR prefixes onto country names, so the downstream
filter_engine sees plain text ("California, United States of America").

COVERAGE: the main feed returns the newest 100 postings across all categories.
The per-category feeds carry different (partly older) slices, so we also pull
the tech-relevant ones and dedupe by listing URL — same trick remotive.py uses
with its category params.

posted_at comes from <pubDate> (RFC-822; parse_date handles it) — the real
PUBLISH date, NOT <expires_at> and never now(). If an item has no parseable
pubDate, posted_at stays None: a crawl-time fallback would make months-old
postings look brand new.

NO geo filter here: region/country/state are often a bare US state name
("California", "Pennsylvania") with no country at all, which a crude US-token
filter would wrongly drop. The shared filter_engine already runs on every
crawled job in the scheduler pipeline and recognises state names/codes.
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

MAIN_FEED = "https://weworkremotely.com/remote-jobs.rss"
CATEGORY_FEED = "https://weworkremotely.com/categories/{slug}.rss"

# Tech-relevant category feeds pulled alongside the main feed for extra
# coverage; results are deduped by listing URL. (There is no "data" category on
# WWR — data roles land in programming/full-stack/back-end.)
CATEGORY_SLUGS = (
    "remote-programming-jobs",
    "remote-full-stack-programming-jobs",
    "remote-back-end-programming-jobs",
    "remote-devops-sysadmin-jobs",
)

# WWR prefixes country names with a flag emoji ("🇺🇸 United States of America").
# Flags are pairs of REGIONAL INDICATOR SYMBOL codepoints (U+1F1E6..U+1F1FF).
_FLAG_RE = re.compile("[\U0001F1E6-\U0001F1FF]")


def _text(item: ET.Element, path: str) -> str:
    """Text of a child element, or "" if absent/empty."""
    return (item.findtext(path, default="") or "").strip()


def _split_title(raw_title: str) -> tuple[str, str]:
    """Split WWR's "Company: Job Title" into (employer, title).

    Splits on the FIRST colon only, so a colon inside the role name stays with
    the title. If there is no colon we cannot tell employer from title, so we
    return an empty employer and keep the string intact as the title.
    """
    employer, sep, title = raw_title.partition(":")
    if not sep:
        return "", raw_title.strip()
    return employer.strip(), title.strip()


def _location(item: ET.Element) -> str:
    """Join the non-empty region/country/state fields into one plain string."""
    parts = [_FLAG_RE.sub("", _text(item, f)).strip() for f in ("region", "country", "state")]
    return ", ".join(p for p in parts if p)


class WeWorkRemotelyCrawler(BaseCrawler):
    source_name = "weworkremotely"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "weworkremotely" or "weworkremotely.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        """Pull the main feed plus the tech category feeds. Ignores
        company.career_url. Deduped by listing URL across feeds.
        """
        feeds = [MAIN_FEED] + [CATEGORY_FEED.format(slug=s) for s in CATEGORY_SLUGS]
        seen_urls: set = set()
        out: List[Dict[str, Any]] = []
        for feed in feeds:
            xml = self._get(feed).content
            channel = ET.fromstring(xml).find("channel")
            items = channel.findall("item") if channel is not None else []
            for item in items:
                url = _text(item, "link") or _text(item, "guid")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                out.append(
                    {
                        "raw_title": _text(item, "title"),
                        "location": _location(item),
                        "job_type": _text(item, "type"),
                        "url": url,
                        "description": _text(item, "description"),
                        "pub_date": _text(item, "pubDate"),
                    }
                )
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        # Aggregator: the employer is parsed out of the item title, not company.name.
        employer, title = _split_title(raw.get("raw_title") or "")
        location = (raw.get("location") or "").strip()
        job_url = (raw.get("url") or "").strip()
        description = truncate(clean_html(raw.get("description") or ""))
        # REAL publish date only — pubDate, never expires_at, never now().
        # parse_date returns None on junk and we keep that None.
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
