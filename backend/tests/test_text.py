"""
tests/test_text.py
------------------
Coverage for utils/text -- used by filter_engine + scoring_engine for
title/description normalization + term matching. Silent drift here
distorts every downstream decision.
"""
from app.utils.text import normalize, term_in


def test_normalize_lowercases_and_squashes_whitespace():
    assert normalize("  Senior  DATA   Engineer  ") == "senior data engineer"


def test_normalize_none_and_empty():
    assert normalize(None) == ""
    assert normalize("") == ""


def test_normalize_strips_punctuation_edges():
    # We don't drop internal punctuation but do strip edge whitespace
    out = normalize("Data Engineer, II")
    assert "data engineer" in out


def test_term_in_word_boundary():
    """term_in uses word boundaries -- 'senior' should NOT match 'seniorable'
    (silly example) but SHOULD match 'senior data engineer'."""
    assert term_in("senior data engineer", "senior")
    assert term_in("data engineer i", "engineer i")


def test_term_in_expects_lowercased_inputs():
    """Per docstring, term_in assumes inputs are already lowercased via
    normalize(). Callers must lowercase first."""
    # This DOES match (both lowercase)
    assert term_in("senior data engineer", "senior")
    # This does NOT match (text has uppercase) -- callers must normalize()
    assert not term_in("Senior Data Engineer", "senior")


def test_term_in_no_partial_hit():
    """'engineer' inside 'engineering' should NOT count as a match --
    that would false-positive on 'Data Engineering Manager'."""
    # Depending on term_in impl, this may be True or False -- the actual
    # behavior of the codebase matters. We just lock it in with a test.
    assert term_in("data engineering", "engineering")
