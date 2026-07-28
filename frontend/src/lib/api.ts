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

interface ListParams {
  min_score?: number
  exclude_rejected?: boolean
  order_by?: "score" | "discovered_at" | "posted_at"
  limit?: number
  slim?: boolean
  discovered_within_hours?: number
  status?: JobStatus | JobStatus[]
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

export async function fetchJobs(params: ListParams): Promise<Job[]> {
  const r = await fetch(`/api/jobs/${toQuery(params)}`)
  if (!r.ok) throw new Error(`GET /jobs failed: ${r.status}`)
  return r.json()
}

export async function fetchJobsCount(params: ListParams): Promise<number> {
  const r = await fetch(`/api/jobs/count${toQuery(params)}`)
  if (!r.ok) return 0
  const j = await r.json()
  return j.count ?? 0
}

export async function setStatus(id: number, status: JobStatus): Promise<void> {
  const r = await fetch(`/api/jobs/${id}/status`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  })
  if (!r.ok) throw new Error(`status update failed: ${r.status}`)
}
