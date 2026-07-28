"""
scripts/harvest_company_sources.py
----------------------------------
Discover COMPANIES (not jobs) from public sources and drop them into a
shared pending queue for auto_discover.py to probe against known ATSes.

The existing pipeline was one-dimensional: auto_discover.py assumed every
input company name was already in the USCIS H-1B sponsor list. Everything
else -- non-sponsor startups, newer companies, non-visa-history employers --
was invisible. This script is the sourcing layer that answers "which
companies exist that we've never seen?"

WHY NOT VC PORTFOLIO PAGES:
    a16z / Sequoia / Bessemer et al render their portfolio via client-side
    JS. Raw HTML exposes ~20 of ~500 companies. Full extraction needs a
    headless browser (Playwright), which is a 2-3h infra investment and
    fragile against DOM changes. Skipped.

SOURCES (curl-verified, all public, no auth):
    1. hiring-without-whiteboards (github/poteto)
       Maintained markdown list, ~770 tech companies with website URLs.
       The single richest low-effort source.

    2. Wikipedia S&P 500 tech constituents
       Static HTML table of the big public US tech companies.

    3. Wikipedia list of largest tech companies by revenue
       Overlaps with S&P 500 but adds international-listed tech co's HQ'd in US.

    4. yc-oss/api/companies (bonus)
       Already used by yc_waas crawler, but exposes 6k YC companies we can
       cross-reference / seed at the roster level for non-hiring ones so
       they auto-appear if they later start posting.

    5. Static seed CSVs shipped with the repo
       data/seeds/*.csv drop-in files -- one row per company. Ram or a
       future contributor can drop new source lists here without changing
       code (Inc 5000, Forbes Cloud 100, etc.).

FLOW:
    harvest -> data/discovered_companies.jsonl  (append-only pending queue)
    auto_discover --seed-file <that jsonl>       (probes ATS platforms)
    valid hits -> companies table                 (crawlers take over)

IDEMPOTENCE:
    The pending JSONL is a UNION across runs, deduped by lowercase name.
    Re-running this script is safe -- it will not double-insert.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.utils.logging import get_logger  # noqa: E402

log = get_logger("harvest_sources")

REPO_ROOT = Path(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
OUT_FILE = REPO_ROOT / "data" / "discovered_companies.jsonl"
SEEDS_DIR = REPO_ROOT / "data" / "seeds"
TIMEOUT = 20

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "JobControlCenter/1.0 (+personal-job-search; harvest)",
    "Accept": "text/html, text/markdown, application/json",
})


@dataclass
class Discovery:
    name: str
    website: Optional[str]  # may be None if we only got a name
    source: str             # tag so we can audit which feed produced this

    def key(self) -> str:
        """Case-insensitive identity used for dedupe across sources."""
        n = re.sub(r"[^a-z0-9]+", "", (self.name or "").lower())
        # Strip trailing numeric suffixes -- some seeds have "Acme2" duplicates.
        return re.sub(r"\d+$", "", n) or n


# ---------- source feeders ----------

def source_hww() -> Iterable[Discovery]:
    """hiring-without-whiteboards markdown -> (name, url).

    Row shape: `- [Company](https://url) | Location | notes...`
    Occasional variants nest the link inside `**bold**` or a heading; the
    regex handles both.
    """
    url = "https://raw.githubusercontent.com/poteto/hiring-without-whiteboards/master/README.md"
    try:
        r = SESSION.get(url, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("hww fetch failed: %s", exc)
        return
    text = r.text
    # The README has a "Discussion and other reads" preamble with article
    # links, THEN the actual company list, THEN a "Duds" section. Slice to
    # keep only the company-alphabet section. It starts with the "## 0 - 9"
    # heading and ends at "## Duds" or end-of-file.
    m_start = re.search(r"^## 0 ?- ?9\b", text, re.M)
    m_end = re.search(r"^## Duds\b", text, re.M)
    if m_start:
        text = text[m_start.start(): (m_end.start() if m_end else len(text))]
    seen: set[str] = set()
    pat = re.compile(r"^\s*[-*]\s*\[?\*{0,2}\[([^\]]+)\]\(([^)]+)\)\*{0,2}", re.M)
    # Domains that are clearly NOT direct company sites.
    _bad_domains = ("news.ycombinator.com", "theoutline.com", "web.archive.org",
                    "medium.com", "reddit.com", "twitter.com", "x.com",
                    "youtube.com", "github.com", "wikipedia.org")
    for m in pat.finditer(text):
        name = (m.group(1) or "").strip()
        url = (m.group(2) or "").strip()
        if not name or not url.startswith(("http://", "https://")):
            continue
        if len(name) < 2 or len(name) > 80:
            continue
        # Article names contain sentence-fragments the parser can't distinguish
        # from a long company name — skip anything with sentence markers.
        if any(c in name for c in " ,.:'-?!") and len(name.split()) > 6:
            continue
        # URL must not be an article / social / archive.
        if any(bd in url.lower() for bd in _bad_domains):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        yield Discovery(name=name, website=url, source="hiring_without_whiteboards")


def source_sp500() -> Iterable[Discovery]:
    """Wikipedia's S&P 500 constituent list — parseable with regex on the
    HTML table rows. Filters to tech-adjacent sectors."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    try:
        html = SESSION.get(url, timeout=TIMEOUT).text
    except Exception as exc:  # noqa: BLE001
        log.warning("sp500 fetch failed: %s", exc)
        return
    # The constituents table lists <tr>...<td>SYMBOL</td><td><a>Company</a></td>...
    # We match the company link + the sector cell that follows.
    row_re = re.compile(
        r'<td[^>]*>\s*<a[^>]*rel="nofollow"[^>]*>([A-Z][A-Z.]+)</a>\s*</td>\s*'
        r'<td[^>]*>\s*<a[^>]*>([^<]{2,80})</a>[^<]*</td>\s*'
        r'<td[^>]*>([^<]+)</td>',
        re.I | re.S,
    )
    tech_sectors = (
        "information technology", "communication services",
        "consumer discretionary", "financials",
    )
    for m in row_re.finditer(html):
        _ticker, name, sector = (
            m.group(1).strip(), m.group(2).strip(), m.group(3).strip().lower()
        )
        if not any(s in sector for s in tech_sectors):
            continue
        yield Discovery(name=name, website=None, source="sp500_tech")


def source_yc() -> Iterable[Discovery]:
    """yc-oss/api companies dump. Seeds ALL YC companies (not just hiring)
    so that when they later start hiring the roster already has them and
    the yc_waas crawler picks them up on the next sweep. NON-US companies
    are dropped by the sector filter at the auto_discover stage."""
    url = "https://yc-oss.github.io/api/companies/all.json"
    try:
        data = SESSION.get(url, timeout=TIMEOUT).json()
    except Exception as exc:  # noqa: BLE001
        log.warning("yc-oss fetch failed: %s", exc)
        return
    if not isinstance(data, list):
        return
    for co in data:
        if not isinstance(co, dict):
            continue
        name = (co.get("name") or "").strip()
        website = (co.get("website") or "").strip()
        if not name:
            continue
        yield Discovery(name=name, website=website or None, source="yc_oss")


def source_static_seeds() -> Iterable[Discovery]:
    """data/seeds/*.csv — anyone can drop a new CSV in without editing code.

    Expected columns: `name` (required), `website` (optional). Header row
    required, extra columns ignored. This is the "Inc. 5000 / Forbes Cloud
    100 / Fortune 500 / your handwritten list" drop point.
    """
    if not SEEDS_DIR.is_dir():
        return
    for path in sorted(SEEDS_DIR.glob("*.csv")):
        try:
            with path.open() as fh:
                reader = csv.DictReader(fh)
                if "name" not in (reader.fieldnames or []):
                    log.warning("seed %s missing 'name' column, skipped", path.name)
                    continue
                for row in reader:
                    name = (row.get("name") or "").strip()
                    website = (row.get("website") or "").strip() or None
                    if not name:
                        continue
                    yield Discovery(
                        name=name, website=website,
                        source=f"seed:{path.stem}",
                    )
        except Exception as exc:  # noqa: BLE001
            log.warning("seed %s parse failed: %s", path.name, exc)


SOURCES: dict[str, Callable[[], Iterable[Discovery]]] = {
    "hww": source_hww,
    "sp500": source_sp500,
    "yc": source_yc,
    "seeds": source_static_seeds,
}


# ---------- persistence ----------

def load_existing(path: Path) -> dict[str, Discovery]:
    """Rehydrate the pending queue so re-runs are idempotent."""
    out: dict[str, Discovery] = {}
    if not path.exists():
        return out
    with path.open() as fh:
        for line in fh:
            try:
                d = json.loads(line)
                disc = Discovery(name=d["name"], website=d.get("website"),
                                 source=d.get("source", "?"))
                out[disc.key()] = disc
            except Exception:
                continue
    return out


def clean_website(url: Optional[str]) -> Optional[str]:
    """Strip URL params, keep origin + first-level path.

    hiring-without-whiteboards frequently links straight to a company's
    /careers or /jobs URL. We keep the FULL URL when it names a careers
    path -- auto_discover downstream uses it directly instead of guessing.
    """
    if not url:
        return None
    try:
        p = urlparse(url)
        if not p.scheme or not p.netloc:
            return None
        # Drop query and fragment; keep the path (may include /careers).
        return f"{p.scheme}://{p.netloc}{p.path or '/'}".rstrip("/")
    except Exception:
        return None


def write_jsonl(path: Path, records: Iterable[Discovery]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w") as fh:
        for d in records:
            fh.write(json.dumps({
                "name": d.name,
                "website": d.website,
                "source": d.source,
            }) + "\n")
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", nargs="+", default=list(SOURCES.keys()),
                    help=f"which sources to run (default all): {list(SOURCES.keys())}")
    ap.add_argument("--out", default=str(OUT_FILE),
                    help="output JSONL path (default data/discovered_companies.jsonl)")
    ap.add_argument("--reset", action="store_true",
                    help="ignore existing output, start fresh (default: merge)")
    args = ap.parse_args()

    out_path = Path(args.out)
    merged = {} if args.reset else load_existing(out_path)
    initial = len(merged)
    log.info("merging into existing queue: %d entries", initial)

    counters: dict[str, int] = {}
    for name in args.sources:
        fn = SOURCES.get(name)
        if fn is None:
            log.warning("unknown source: %s (skipping)", name)
            continue
        n_new = 0
        for d in fn():
            d.website = clean_website(d.website)
            key = d.key()
            if key not in merged:
                merged[key] = d
                n_new += 1
        counters[name] = n_new
        log.info("source %-8s : +%d new companies", name, n_new)

    total = write_jsonl(out_path, merged.values())
    log.info("--- summary ---")
    log.info("started with        : %d", initial)
    for name, n in counters.items():
        log.info("  +%-8s          : %d", name, n)
    log.info("total in queue      : %d  (written to %s)", total, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
