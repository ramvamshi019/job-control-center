"""
services/notifier.py
--------------------
Alert the moment a fresh, high-value job appears, so you can apply within
minutes instead of discovering it a day later with 200 other applicants.

TWO transports, tried in order:

  1. ntfy  — an HTTP POST to a topic URL. Works on the Linux droplet and pushes
     to your phone. No account, no API key: pick an unguessable topic, install
     the ntfy app, subscribe. Enabled by setting NTFY_TOPIC in backend/.env.
  2. osascript — native macOS banner, for running the stack on the laptop.

This used to be osascript ONLY, which silently stopped working the moment the
stack moved into Docker on Linux: `osascript` doesn't exist in the container, so
every "ALERT: N new fresh-sponsor jobs" line was logged and then thrown away.
The crawler thought it was alerting; nothing ever arrived.

Never raises — a failed alert must not break a crawl cycle.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import requests

from app.config import settings
from app.utils.logging import get_logger

log = get_logger("notifier")


def _ntfy(title: str, message: str, url: str | None = None) -> bool:
    """POST to an ntfy topic. Returns True if it was accepted."""
    topic = (settings.ntfy_topic or "").strip()
    if not topic:
        return False
    endpoint = topic if topic.startswith("http") else f"https://ntfy.sh/{topic}"
    headers = {"Title": title, "Priority": "high", "Tags": "briefcase"}
    if url:
        # Tapping the notification opens the posting directly.
        headers["Click"] = url
    try:
        r = requests.post(endpoint, data=message.encode("utf-8"),
                          headers=headers, timeout=8)
        if r.status_code < 300:
            return True
        log.warning("ntfy rejected the alert: HTTP %s", r.status_code)
    except Exception as exc:  # noqa: BLE001 - alerts are best-effort
        log.warning("ntfy notification failed: %s", exc)
    return False


def _macos(title: str, message: str, sound: str) -> bool:
    if not shutil.which("osascript"):
        return False
    try:
        script = (
            f"display notification {json.dumps(message)} "
            f"with title {json.dumps(title)} sound name {json.dumps(sound)}"
        )
        subprocess.run(["osascript", "-e", script], timeout=5, check=False,
                       capture_output=True)
        return True
    except Exception as exc:  # noqa: BLE001 - alerts are best-effort
        log.warning("macOS notification failed: %s", exc)
    return False


def notify(title: str, message: str, sound: str = "Glass", url: str | None = None) -> None:
    """Send an alert by whichever transport is available. Logs loudly when NONE
    is — silent failure here is what made this useless for weeks."""
    if _ntfy(title, message, url):
        return
    if _macos(title, message, sound):
        return
    log.warning("ALERT NOT DELIVERED (no transport): set NTFY_TOPIC in "
                "backend/.env to receive these. %s — %s", title, message)
