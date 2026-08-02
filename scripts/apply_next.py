#!/usr/bin/env python3
"""
apply_next.py — local orchestrator for the Fast Apply queue.

Run on your LAPTOP. For each iteration it:
  1. Pulls the top form-only job from the droplet's Fast Apply queue
     (sponsor-confirmed first, non-account ATS only, un-actioned).
  2. Triggers the droplet backend to build a tailored resume via Claude
     using the rules in resumes/tailor_system_prompt.md.
  3. scp's the generated PDF (and .docx as a fallback) into
     ~/Desktop/JCC-resumes/.
  4. Opens the application URL in your default browser and prints the
     autofill answers.
  5. Waits for you to submit the form (10-second CAPTCHA + Submit click),
     then marks the job Applied in JCC.

Usage:
  python scripts/apply_next.py                  # walk one job
  python scripts/apply_next.py --n 10           # walk up to 10 jobs
  python scripts/apply_next.py --min-score 60   # raise the score floor
  python scripts/apply_next.py --dry-run        # build + download but skip
                                                # marking applied (for testing)

Assumes:
  - SSH access to the droplet as root@143.198.188.116 works without a
    prompt (key-based auth).
  - The droplet has curl and the backend container is running healthy.
  - macOS `open` command is available (for launching the ATS page).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

DROPLET = os.environ.get("JCC_DROPLET", "root@143.198.188.116")
DROPLET_REPO = "/root/job-control-center"
API = "http://127.0.0.1:8000"
LAPTOP_RESUMES = Path.home() / "Desktop" / "JCC-resumes"

# The backend container returns paths under /app/... because that's where the
# code sees them. Those are bind-mounted from the droplet host at DROPLET_REPO,
# so scp needs the host-side path.
CONTAINER_PREFIX = "/app/"
HOST_PREFIX = f"{DROPLET_REPO}/"

ACCOUNT_ATS = {"workday", "icims"}  # need employer accounts; skip in Fast Apply


def to_host_path(container_path: str) -> str:
    """Translate a /app/... container path to its bind-mounted host path so scp
    can reach it. Any path that doesn't start with /app/ is returned unchanged
    (so future non-mounted paths still fail loudly rather than silently)."""
    if container_path.startswith(CONTAINER_PREFIX):
        return HOST_PREFIX + container_path[len(CONTAINER_PREFIX):]
    return container_path


def ssh(cmd: str, capture: bool = True) -> str:
    """Run a shell command on the droplet. Returns stdout stripped on capture."""
    full = ["ssh", "-o", "BatchMode=yes", DROPLET, cmd]
    if capture:
        r = subprocess.run(full, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(f"ssh failed ({r.returncode}): {r.stderr.strip()}\n$ {cmd}")
        return r.stdout.strip()
    subprocess.run(full, check=True, timeout=180)
    return ""


def api_get(path: str) -> list | dict:
    """GET a droplet API path via SSH-executed curl. Keeps the flow tunnel-free."""
    raw = ssh(f"curl -sSf {shlex.quote(API + path)}")
    return json.loads(raw)


def api_patch(path: str, payload: dict) -> dict:
    body = shlex.quote(json.dumps(payload))
    raw = ssh(
        f"curl -sSf -X PATCH -H 'Content-Type: application/json' "
        f"-d {body} {shlex.quote(API + path)}"
    )
    return json.loads(raw)


def api_post(path: str) -> dict:
    raw = ssh(f"curl -sSf -X POST {shlex.quote(API + path)}")
    return json.loads(raw)


def fetch_queue(min_score: int, sponsors_only: bool, include_slow: bool) -> list:
    """Pull the Fast Apply queue: New/Need-Review jobs above min_score, sorted
    by (sponsor, score). Mirrors the dashboard's Fast Apply filter so the CLI
    picks exactly what you would have picked in the UI."""
    jobs = api_get("/jobs/?exclude_rejected=true&order_by=score&limit=200")
    queue = [
        j for j in jobs
        if j.get("status") in ("New", "Need Review")
        and (j.get("match_score") or 0) >= min_score
        and (include_slow or (j.get("source") or "").lower() not in ACCOUNT_ATS)
        and (not sponsors_only or j.get("sponsor_confirmed"))
    ]
    queue.sort(
        key=lambda j: (bool(j.get("sponsor_confirmed")), j.get("match_score") or 0),
        reverse=True,
    )
    return queue


def build_resume(job_id: int) -> dict:
    """Ask the droplet to tailor + save. Returns the paths dict from the API."""
    print(f"  → building tailored resume via Claude…", flush=True)
    return api_post(f"/jobs/{job_id}/build-resume")


def scp_resumes(paths: dict, job: dict) -> list[Path]:
    """Copy the .pdf and .docx from the droplet into ~/Desktop/JCC-resumes/.
    Renamed to {Company}_{Title}_{JobID}.<ext> so the local folder is a clean,
    human-scannable archive rather than {id}_{slug}.pdf."""
    LAPTOP_RESUMES.mkdir(parents=True, exist_ok=True)
    company = _clean(job.get("company_name") or "company")
    title = _clean(job.get("title") or "role")
    stem = f"{company}_{title}_{job['id']}"[:120]

    landed: list[Path] = []
    for key, ext in [("pdf_path", "pdf"), ("docx_path", "docx"), ("md_path", "md")]:
        remote = paths.get(key)
        if not remote:
            continue
        local = LAPTOP_RESUMES / f"{stem}.{ext}"
        try:
            subprocess.run(
                ["scp", "-q", f"{DROPLET}:{to_host_path(remote)}", str(local)],
                check=True, timeout=60,
            )
            landed.append(local)
        except subprocess.CalledProcessError as exc:
            print(f"    ! scp {ext} failed: {exc}", file=sys.stderr)
    return landed


def _clean(s: str) -> str:
    """Filesystem-safe piece for a resume filename. Keeps letters/digits/spaces,
    collapses whitespace to underscores, drops the rest."""
    keep = "".join(c if c.isalnum() or c.isspace() else " " for c in s)
    return "_".join(keep.split())


def open_url(url: str) -> None:
    if not url:
        return
    try:
        subprocess.run(["open", url], check=True)
    except Exception as exc:  # noqa: BLE001
        print(f"    ! couldn't open {url}: {exc}")


def print_answers(job: dict) -> None:
    """Print the canned screener answers so you can paste any that the
    autofill bookmarklet misses (work-auth, sponsorship, start date)."""
    print("\n  copy-paste answers (bookmarklet fills the rest):")
    print("    Authorized to work in the US? → Yes (F-1 OPT, STEM eligible)")
    print("    Requires sponsorship?         → Yes")
    print("    Earliest start date?          → Immediately")


def mark_applied(job_id: int) -> None:
    api_patch(f"/jobs/{job_id}", {"status": "Applied"})


def walk(min_score: int, sponsors_only: bool, include_slow: bool,
         n: int, dry_run: bool) -> None:
    queue = fetch_queue(min_score, sponsors_only, include_slow)
    if not queue:
        print("Queue is empty. Try --min-score lower, --no-sponsors, or --include-slow.")
        return
    print(f"Queue: {len(queue)} jobs. Working the top {min(n, len(queue))}.\n")

    processed = 0
    for job in queue[:n]:
        jid = job["id"]
        sponsor = "✅" if job.get("sponsor_confirmed") else "  "
        print(f"[{processed+1}/{n}] {sponsor} #{jid} · {job['title']} @ "
              f"{job['company_name']} · score={job.get('match_score')} · "
              f"ats={job.get('source')}")
        try:
            paths = build_resume(jid)
            landed = scp_resumes(paths, job)
            for p in landed:
                print(f"    ✓ {p}")

            url = job.get("apply_url") or job.get("job_url") or ""
            print(f"  → opening {url}")
            open_url(url)
            print_answers(job)
        except Exception as exc:  # noqa: BLE001
            print(f"    ✗ build/download failed: {exc}", file=sys.stderr)
            processed += 1
            continue

        if dry_run:
            print("  (dry-run: NOT marking Applied)\n")
            processed += 1
            continue

        while True:
            ans = input("  submitted? [y=applied / s=skip / q=quit]: ").strip().lower()
            if ans == "y":
                mark_applied(jid)
                print(f"  ✓ marked Applied\n")
                break
            if ans == "s":
                print("  · skipped (job stays New; will re-appear next run)\n")
                break
            if ans == "q":
                print(f"\nDone. Processed {processed} of {n}.")
                return
        processed += 1

    print(f"\nDone. Processed {processed} of {n}.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Walk the Fast Apply queue.")
    ap.add_argument("--n", type=int, default=1, help="Max jobs to work (default 1).")
    ap.add_argument("--min-score", type=int, default=40,
                    help="Minimum match score (default 40, matches Fast Apply page).")
    ap.add_argument("--no-sponsors", action="store_true",
                    help="Drop the sponsor-confirmed filter.")
    ap.add_argument("--include-slow", action="store_true",
                    help="Include Workday/iCIMS (account-per-employer, slow).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build + download but don't mark Applied.")
    args = ap.parse_args()

    walk(min_score=args.min_score, sponsors_only=not args.no_sponsors,
         include_slow=args.include_slow, n=args.n, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
