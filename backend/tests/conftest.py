"""
tests/conftest.py
-----------------
Pytest bootstrap: make `backend/app/*` importable without installing.
Runs from `backend/` -- `pytest -q` picks tests from tests/ automatically.
"""
from __future__ import annotations

import os
import sys

# `backend/` is one level up from this file
_HERE = os.path.dirname(__file__)
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
