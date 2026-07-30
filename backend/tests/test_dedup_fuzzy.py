"""
tests/test_dedup_fuzzy.py
-------------------------
The fuzzy dedup logic lives in dashboard/app.py (Streamlit runtime) so
we can't import it in the backend test container. Mirror the pure
functions here and lock in behavior. Update this file when the ones in
dashboard/app.py change.
"""
import re
import pytest


def _dedup_key(job):
    def _norm(s):
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    company_raw = (job.get("company_name") or "").lower()
    company_raw = re.sub(
        r"\b(inc|incorporated|corp|corporation|llc|l\.l\.c\.?|ltd|limited|"
        r"company|co|holdings|group|plc|pbc|the)\b\.?", " ", company_raw)
    company = _norm(company_raw)
    title_raw = (job.get("title") or "").lower()
    title_clean = re.sub(
        r"\b(senior|sr\.?|junior|jr\.?|staff|principal|lead|associate|"
        r"i{1,3}\b|iv|v|\d{1,2})\b", " ", title_raw)
    title = _norm(title_clean)
    loc = (job.get("location") or "").lower()
    city = re.match(r"[a-z]+", loc)
    city_key = city.group(0) if city else ""
    return (company, title, city_key)


def _collapse_duplicates(jobs):
    if len(jobs) < 2:
        return jobs
    seen = {}
    order = []
    for j in jobs:
        k = _dedup_key(j)
        if not k[0] or not k[1]:
            k = ("__ungrouped__", str(j.get("id", "")), "")
        current = seen.get(k)
        if current is None:
            seen[k] = j
            order.append(k)
        else:
            cs, js = current.get("match_score") or 0, j.get("match_score") or 0
            if js > cs or (js == cs and (j.get("discovered_at") or "") > (current.get("discovered_at") or "")):
                seen[k] = j
    return [seen[k] for k in order]


def _j(id, title, company, location="Austin, TX", score=50, discovered="2026-07-29T12:00"):
    return {
        "id": id, "title": title, "company_name": company, "location": location,
        "match_score": score, "discovered_at": discovered,
    }


# ---- _dedup_key correctness --------------------------------------------

def test_key_ignores_level_words():
    """Senior/Junior/Staff/II/III don't affect the dedup group -- these are
    the SAME conceptual role, just at different levels."""
    a = _dedup_key(_j(1, "Senior Software Engineer II", "Stripe"))
    b = _dedup_key(_j(2, "Software Engineer", "Stripe"))
    assert a == b


def test_key_case_and_punctuation_insensitive():
    """'Stripe, Inc.' vs 'stripe inc' vs 'Stripe' should all group."""
    a = _dedup_key(_j(1, "Software Engineer", "Stripe, Inc."))
    b = _dedup_key(_j(2, "Software Engineer", "stripe inc"))
    c = _dedup_key(_j(3, "Software Engineer", "Stripe"))
    assert a == b == c


def test_city_only_first_word():
    """'Austin, TX' and 'Austin, Texas' collapse; 'New York, NY' vs 'Boston' don't."""
    a = _dedup_key(_j(1, "SWE", "Acme", location="Austin, TX"))
    b = _dedup_key(_j(2, "SWE", "Acme", location="Austin, Texas"))
    c = _dedup_key(_j(3, "SWE", "Acme", location="Boston, MA"))
    assert a == b
    assert a != c


def test_different_titles_dont_collapse():
    """Data Engineer and Software Engineer should stay separate."""
    a = _dedup_key(_j(1, "Data Engineer", "Acme"))
    b = _dedup_key(_j(2, "Software Engineer", "Acme"))
    assert a != b


# ---- _collapse_duplicates behavior --------------------------------------

def test_collapse_higher_score_wins():
    """Two Stripe SWE rows -- keep the one with the higher match_score."""
    jobs = [
        _j(1, "Software Engineer", "Stripe", score=60),
        _j(2, "Senior Software Engineer II", "Stripe", score=85),
    ]
    out = _collapse_duplicates(jobs)
    assert len(out) == 1
    assert out[0]["id"] == 2  # score 85 wins


def test_collapse_score_tie_newer_wins():
    """Same score: newer discovered_at wins."""
    jobs = [
        _j(1, "SWE", "Acme", score=70, discovered="2026-07-01T00:00"),
        _j(2, "SWE", "Acme", score=70, discovered="2026-07-29T00:00"),
    ]
    out = _collapse_duplicates(jobs)
    assert len(out) == 1
    assert out[0]["id"] == 2  # newer wins


def test_no_collapse_when_all_unique():
    """Three unique (company, title) combos stay as three."""
    jobs = [
        _j(1, "SWE", "Stripe"),
        _j(2, "SWE", "Ramp"),
        _j(3, "Data Eng", "Stripe"),
    ]
    out = _collapse_duplicates(jobs)
    assert len(out) == 3


def test_preserves_order():
    """Kept rows should stay in original list order (feed order matters)."""
    jobs = [
        _j(1, "SWE", "Ramp",   score=50),  # kept
        _j(2, "SWE", "Stripe", score=60),  # first Stripe
        _j(3, "SWE", "Stripe", score=90),  # replaces #2 in the same slot
        _j(4, "DE",  "Ramp",   score=50),  # kept
    ]
    out = _collapse_duplicates(jobs)
    ids = [j["id"] for j in out]
    assert ids == [1, 3, 4]  # Ramp SWE first, then winning Stripe SWE, then Ramp DE


def test_missing_company_or_title_passes_through():
    """Rows without company or title can't be safely deduped -- keep them all."""
    jobs = [
        _j(1, "", "Stripe"),
        _j(2, "", "Stripe"),
        _j(3, "SWE", ""),
    ]
    out = _collapse_duplicates(jobs)
    assert len(out) == 3  # nothing collapses


def test_short_input_no_op():
    assert _collapse_duplicates([]) == []
    one = [_j(1, "SWE", "Acme")]
    assert _collapse_duplicates(one) == one
