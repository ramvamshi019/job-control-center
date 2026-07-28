"""
crawlers/personio.py
--------------------
Personio public careers XML feed (no key needed):
    https://{token}.jobs.personio.com/xml?language=en   -> <workzag-jobs>

`token` is the company subdomain. Accepts a bare token or any *.jobs.personio.com
URL. `?language=en` keeps English copy where a company offers it (the feed
otherwise defaults to German). The public job URL is not a feed field — it is
derivable from each position's id as
    https://{token}.jobs.personio.com/job/{id}?language=en

Feed shape (verified live 2026-07-27):
    <workzag-jobs>
      <position>
        <id>123456</id>
        <name>Customer Service Specialist (f/m/d)</name>
        <office>Hybrid - Paris, France</office>
        <department>...</department>
        <employmentType>permanent</employmentType>
        <createdAt>2026-06-11T15:58:25+00:00</createdAt>
        <jobDescriptions>
          <jobDescription><name>About</name><value><![CDATA[ HTML ]]></value></jobDescription>
          ...
        </jobDescriptions>
      </position>
      ...
    </workzag-jobs>
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

API = "https://{token}.jobs.personio.com/xml?language=en"
JOB_URL = "https://{token}.jobs.personio.com/job/{job_id}?language=en"


def extract_token(career_url: str) -> str:
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    m = re.search(r"([A-Za-z0-9_-]+)\.jobs\.personio\.(?:com|de)", s)
    if m:
        return m.group(1)
    if "/" not in s and "." not in s:
        return s
    return s.split("/")[-1]


def _position_id(pos: ET.Element) -> str:
    """id is usually an <id> child; tolerate it being an attribute instead."""
    return (pos.findtext("id") or pos.get("id") or "").strip()


def _description(pos: ET.Element) -> str:
    """Join every <jobDescription><value> body under <jobDescriptions>."""
    chunks: List[str] = []
    for jd in pos.findall("./jobDescriptions/jobDescription"):
        heading = (jd.findtext("name") or "").strip()
        body = (jd.findtext("value") or "").strip()
        if heading and body:
            chunks.append(f"{heading}: {body}")
        elif body:
            chunks.append(body)
    return " ".join(chunks)


class PersonioCrawler(BaseCrawler):
    source_name = "personio"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "personio" or "jobs.personio." in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        token = extract_token(company.career_url)
        if not token:
            return []
        xml = self._get(API.format(token=token)).content
        root = ET.fromstring(xml)  # <workzag-jobs>
        # Positions can sit at the root or under a wrapper depending on the feed.
        positions = root.findall(".//position")
        out: List[Dict[str, Any]] = []
        for pos in positions:
            out.append({
                "token": token,
                "id": _position_id(pos),
                "name": (pos.findtext("name") or "").strip(),
                "office": (pos.findtext("office") or "").strip(),
                "employmentType": (pos.findtext("employmentType") or "").strip(),
                "createdAt": (pos.findtext("createdAt") or "").strip(),
                "description": _description(pos),
            })
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = raw.get("name") or ""
        location = raw.get("office") or ""
        job_id = raw.get("id") or ""
        job_url = JOB_URL.format(token=raw.get("token"), job_id=job_id) if job_id else ""
        description = truncate(clean_html(raw.get("description") or title))
        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type=raw.get("employmentType") or "",
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=parse_date(raw.get("createdAt")),  # real publish date
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )
