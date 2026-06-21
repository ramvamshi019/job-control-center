"""
services/notifier.py
--------------------
Fire a native macOS notification when a fresh, high-value job appears, so you
can apply within minutes instead of watching the dashboard.

Uses osascript (built into macOS) — no API keys, no SMTP, no dependencies. Safe
to call from the background launchd agent (it runs in your GUI session). Never
raises: a failed notification must not break a crawl cycle.
"""
from __future__ import annotations

import json
import subprocess

from app.utils.logging import get_logger

log = get_logger("notifier")


def notify(title: str, message: str, sound: str = "Glass") -> None:
    """Display a macOS banner. No-op (logged) if osascript is unavailable."""
    try:
        script = (
            f"display notification {json.dumps(message)} "
            f"with title {json.dumps(title)} sound name {json.dumps(sound)}"
        )
        subprocess.run(["osascript", "-e", script], timeout=5, check=False,
                       capture_output=True)
    except Exception as exc:  # noqa: BLE001 - alerts are best-effort
        log.warning("notification failed: %s", exc)
