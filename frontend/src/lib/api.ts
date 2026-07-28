// Thin client over the FastAPI backend. All requests go to /api/*, which
// Vite proxies to http://localhost:8000 in dev (SSH tunnel to droplet) and
// Caddy proxies to the same host in production.

export type JobStatus =
  | "New"
  | "Need Review"
  | "Applied"
  | "Approved"
  | "Rejected"
  | "Archived"
  | "Follow-up"

export interface Job {
  id: number
  title: string
  company_name: string
  location: string
  employment_type?: string
  job_url: string
  source: string
  match_score: number
  sponsorship_risk?: string
  sponsor_confirmed?: boolean
  status: JobStatus
  posted_at?: string | null
  discovered_at?: string | null
  years_required?: number | null
  fit_reason?: string | null
  risk_reason?: string | null
}

export type ListParams = {
  min_score?: number
  exclude_rejected?: boolean
  order_by?: "score" | "discovered_at" | "posted_at"
  limit?: number
  slim?: boolean
  discovered_within_hours?: number
  status?: JobStatus | JobStatus[]
  [key: string]: unknown
}

function toQuery(params: Record<string, unknown>): string {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue
    if (Array.isArray(v)) v.forEach((x) => p.append(k, String(x)))
    else p.set(k, String(v))
  }
  return p.toString() ? `?${p}` : ""
}

// Same-origin fetches inherit the basic-auth session the user set up when
// they loaded the SPA. `credentials: "same-origin"` is the default, but we
// state it explicitly so a future move to a cross-origin API host is a
// one-line change (`include`) rather than a mystery breakage.
const AUTH: RequestInit = { credentials: "same-origin" }

async function throwIfBad(r: Response, what: string): Promise<never | void> {
  if (r.ok) return
  if (r.status === 401)
    throw new Error(
      "Your session expired — reload the page and sign in again."
    )
  throw new Error(`${what} failed: ${r.status}`)
}

export async function fetchJobs(params: ListParams): Promise<Job[]> {
  const r = await fetch(`/api/jobs/${toQuery(params)}`, AUTH)
  await throwIfBad(r, "GET /jobs")
  return r.json()
}

export async function fetchJobsCount(params: ListParams): Promise<number> {
  const r = await fetch(`/api/jobs/count${toQuery(params)}`, AUTH)
  if (!r.ok) return 0
  const j = await r.json()
  return j.count ?? 0
}

export async function setStatus(id: number, status: JobStatus): Promise<void> {
  const r = await fetch(`/api/jobs/${id}/status`, {
    ...AUTH,
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  })
  await throwIfBad(r, "status update")
}
