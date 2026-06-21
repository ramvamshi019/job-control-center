# Deploying Job Control Center to the cloud (24/7)

The point of moving to the cloud: a server **never sleeps**, so the crawler runs
truly 24/7 instead of freezing every time your Mac sleeps.

This stack is **3 long-running processes + a SQLite database**. That shape fits a
small **VPS** (DigitalOcean / Hetzner / Linode, ~$6–12/mo) much better than the
serverless platforms (Render/Railway/Fly), which fight you on persistent disk and
always-on background workers. Recommended: a 2 GB / 1–2 vCPU VPS running Docker.

---

## ⚠️ Read this first: the datacenter-IP problem

The crawler works great from your Mac because it has a **residential IP**. Many
job boards (anything behind **Cloudflare** — Himalayas was the example you hit —
plus Workday/iCIMS bot protection) **block or challenge datacenter IPs much more
aggressively.** On a cloud VPS you should expect:

- Direct ATS APIs (Greenhouse, Lever, Ashby, SmartRecruiters, BambooHR) → **fine**.
- Cloudflare-gated sources (Himalayas, some others) → **more 403s / challenges**.

This won't break the system (those sources just yield less), but **coverage of
protected sources may drop vs. your Mac.** If that matters, route the crawler
through a **residential proxy** (set it via `HTTPS_PROXY` in `backend/.env`).
Plan B: keep the crawler on your Mac and only move the API/dashboard — but then
you're back to the sleep problem, so the VPS is still the better call.

---

## 1. Prerequisites
- A VPS with **Docker** + **Docker Compose** installed.
- Your repo pushed to git (GitHub private repo is fine), or `scp` the folder up.
- The `.venv`, `*.db`, and `logs/` are gitignored — they are **not** uploaded
  (that's intentional; the host rebuilds the venv and the DB lives in a volume).

## 2. Get the code onto the VPS
```bash
git clone <your-private-repo> job-control-center
cd job-control-center
```

## 3. Create the environment file
```bash
cp backend/.env.example backend/.env
nano backend/.env     # fill in MY_SKILLS, MY_TARGET_ROLES, MY_WORK_AUTH, etc.
```
- `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are **optional** — leave blank and notes
  fall back to rule-based. (You dropped per-job AI résumés, so you don't need it.)
- Leave `DATABASE_URL` as-is; compose overrides it to the volume path.

## 4. Choose your database — two options

**Option A — Upload your existing DB (keeps your 250k jobs + Approved history):**
```bash
docker compose up -d --build            # creates the named volume
docker compose cp backend/data/jobs.db backend:/app/backend/data/db/jobs.db
docker compose restart                  # pick up the uploaded DB
```
(Your local `jobs.db` is ~900 MB — uploading takes a few minutes once.)

**Option B — Start fresh (clean DB, re-seed all companies):**
```bash
docker compose up -d --build
docker compose run --rm backend python scripts/seed_companies.py
```
The crawler refills jobs within a few hours. You lose your 6 Approved markers.
→ **Recommended: Option A** so you don't lose anything.

## 5. You're live
- Dashboard: `http://<vps-ip>:8501`
- API:       `http://<vps-ip>:8000`

---

## 6. Secure it (important)
The dashboard/API have **no login**. Do **not** leave ports 8000/8501 open to the
internet. Either:
- Bind them to localhost and reach the dashboard over an **SSH tunnel**:
  `ssh -L 8501:localhost:8501 user@vps` → open `http://localhost:8501`, **or**
- Put it behind a reverse proxy (Caddy/Nginx) with basic-auth + HTTPS, and
  firewall everything else (`ufw allow 22`, deny 8000/8501).

## 7. Operations
```bash
docker compose logs -f livewatch     # watch the crawler
docker compose ps                    # service health
docker compose pull && docker compose up -d --build   # deploy an update
docker compose down                  # stop (volume/DB persists)
```
`restart: unless-stopped` keeps every service alive across crashes and reboots —
the cloud equivalent of the launchd KeepAlive you have locally.

## 8. Run the digest on a schedule (optional)
On the VPS, a cron entry replaces the macOS launchd digest:
```bash
0 9,18 * * *  docker compose -f /path/to/docker-compose.yml exec -T backend python scripts/digest.py 24
```
