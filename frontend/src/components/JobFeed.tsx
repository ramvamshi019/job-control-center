import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  type SortingState,
  useReactTable,
} from "@tanstack/react-table"
import { useVirtualizer } from "@tanstack/react-virtual"
import {
  CheckCircle2,
  ExternalLink,
  Loader2,
  Trash2,
} from "lucide-react"
import { Badge } from "@/components/ui/Badge"
import { Button } from "@/components/ui/Button"
import { Checkbox } from "@/components/ui/Checkbox"
import { Slider } from "@/components/ui/Slider"
import { useJobKeyboardNav } from "@/hooks/useJobKeyboardNav"
import { fetchJobs, setStatus, type Job, type JobStatus } from "@/lib/api"
import { cn, daysAgo, formatDate } from "@/lib/utils"

const columnHelper = createColumnHelper<Job>()

export interface JobFeedProps {
  /** Page title shown in the header. */
  title: string
  /** Header icon (React element). */
  icon: React.ReactNode
  /** Short description paragraph under the title. */
  description: React.ReactNode
  /** Initial min match score (0-90). */
  defaultMinScore: number
  /** Initial "discovered within N days" window (0 = any). */
  defaultFreshDays: number
  /** Server-side sort order. */
  orderBy: "score" | "discovered_at" | "posted_at"
  /** Initial sort column & direction for the table. */
  defaultSort: SortingState
  /**
   * Extra "posted" column mode:
   *   "seen"   -> "Seen" (days-ago from discovered_at) — the Best Matches default.
   *   "posted" -> "Posted" date (formatDate posted_at) — Posted Today default.
   */
  dateMode?: "seen" | "posted"
  /** Show the freshness slider in the filter bar. */
  showFreshnessSlider?: boolean
  /** Show the min-score slider in the filter bar. */
  showScoreSlider?: boolean
  /** Include jobs marked Applied by default. */
  defaultHideApplied?: boolean
  /** Extra fixed query params (e.g. force-posted-today gating). */
  extraParams?: Record<string, unknown>
}

function ScoreBar({ v }: { v: number }) {
  const pct = Math.max(0, Math.min(100, v))
  const tone =
    v >= 60 ? "bg-emerald-500" : v >= 40 ? "bg-primary" : "bg-muted-foreground/40"
  return (
    <div className="flex items-center gap-2">
      <div className="tabular-nums w-7 text-right text-xs font-semibold">{v}</div>
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-muted">
        <div className={cn("h-full rounded-full", tone)} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

function StatusPill({ status }: { status: JobStatus }) {
  const map: Record<
    JobStatus,
    { label: string; variant: React.ComponentProps<typeof Badge>["variant"] }
  > = {
    New: { label: "New", variant: "default" },
    "Need Review": { label: "Review", variant: "muted" },
    Applied: { label: "Applied", variant: "sponsor" },
    Approved: { label: "Approved", variant: "sponsor" },
    Rejected: { label: "Rejected", variant: "risk_high" },
    Archived: { label: "Archived", variant: "muted" },
    "Follow-up": { label: "Follow-up", variant: "risk" },
  }
  const m = map[status] ?? { label: status, variant: "muted" }
  return <Badge variant={m.variant}>{m.label}</Badge>
}

function Kpi({
  label,
  value,
  tone,
}: {
  label: string
  value: number
  tone?: "sponsor" | "new"
}) {
  const color =
    tone === "sponsor"
      ? "text-emerald-400"
      : tone === "new"
      ? "text-primary"
      : "text-foreground"
  return (
    <div className="flex items-baseline gap-1.5">
      <span className={cn("tabular-nums text-lg font-semibold", color)}>
        {value.toLocaleString()}
      </span>
      <span className="text-xs text-muted-foreground">{label}</span>
    </div>
  )
}

function FilterField({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      {children}
    </div>
  )
}

export function JobFeed(props: JobFeedProps) {
  const {
    title,
    icon,
    description,
    defaultMinScore,
    defaultFreshDays,
    orderBy,
    defaultSort,
    dateMode = "seen",
    showFreshnessSlider = true,
    showScoreSlider = true,
    defaultHideApplied = true,
    extraParams = {},
  } = props

  const qc = useQueryClient()
  const [minScore, setMinScore] = useState(defaultMinScore)
  const [freshDays, setFreshDays] = useState(defaultFreshDays)
  const [hideApplied, setHideApplied] = useState(defaultHideApplied)
  const [sorting, setSorting] = useState<SortingState>(defaultSort)

  const params = useMemo(
    () => ({
      min_score: minScore,
      exclude_rejected: true,
      order_by: orderBy,
      limit: 3000,
      slim: true,
      discovered_within_hours: freshDays > 0 ? freshDays * 24 : undefined,
      ...extraParams,
    }),
    [minScore, freshDays, orderBy, extraParams]
  )

  const { data = [], isFetching, isLoading, error } = useQuery({
    queryKey: ["jobs", params],
    queryFn: () => fetchJobs(params),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  })

  const rows = useMemo(
    () => (hideApplied ? data.filter((j) => j.status !== "Applied") : data),
    [data, hideApplied]
  )

  const mut = useMutation({
    mutationFn: (v: { id: number; status: JobStatus }) => setStatus(v.id, v.status),
    onMutate: async ({ id, status }) => {
      await qc.cancelQueries({ queryKey: ["jobs"] })
      const prev = qc.getQueriesData<Job[]>({ queryKey: ["jobs"] })
      qc.setQueriesData<Job[]>({ queryKey: ["jobs"] }, (old) =>
        (old ?? []).map((j) => (j.id === id ? { ...j, status } : j))
      )
      return { prev }
    },
    onError: (_e, _v, ctx) => {
      ctx?.prev.forEach(([k, v]) => qc.setQueryData(k, v))
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  })

  const onApply = useCallback(
    (id: number) => mut.mutate({ id, status: "Applied" }),
    [mut]
  )
  const onArchive = useCallback(
    (id: number) => mut.mutate({ id, status: "Archived" }),
    [mut]
  )
  const onOpen = useCallback((url: string) => {
    if (url) window.open(url, "_blank", "noopener,noreferrer")
  }, [])
  const { focusedIndex, setFocusedIndex } = useJobKeyboardNav(rows, {
    onApply,
    onArchive,
    onOpen,
  })

  const cols = useMemo(
    () => [
      columnHelper.display({
        id: "action",
        header: "",
        size: 96,
        cell: ({ row }) => {
          const j = row.original
          const isApplied = j.status === "Applied"
          const isArchived = j.status === "Archived"
          return (
            <div className="flex items-center gap-1">
              <Button
                size="icon"
                variant={isApplied ? "default" : "ghost"}
                title={isApplied ? "Mark not applied (a)" : "Mark applied (a)"}
                onClick={() =>
                  mut.mutate({ id: j.id, status: isApplied ? "New" : "Applied" })
                }
              >
                <CheckCircle2 className="h-4 w-4" />
              </Button>
              <Button
                size="icon"
                variant={isArchived ? "destructive" : "ghost"}
                title="Archive (x)"
                onClick={() => mut.mutate({ id: j.id, status: "Archived" })}
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>
          )
        },
      }),
      columnHelper.accessor("status", {
        header: "Status",
        size: 100,
        cell: (info) => <StatusPill status={info.getValue()} />,
      }),
      columnHelper.display({
        id: "sponsor",
        header: "Sponsor",
        size: 90,
        cell: ({ row }) =>
          row.original.sponsor_confirmed ? (
            <Badge variant="sponsor">✅ H-1B</Badge>
          ) : null,
      }),
      dateMode === "posted"
        ? columnHelper.accessor("posted_at", {
            header: "Posted",
            size: 90,
            cell: (info) => (
              <span className="text-xs text-muted-foreground">
                {formatDate(info.getValue())}
              </span>
            ),
          })
        : columnHelper.accessor("discovered_at", {
            header: "Seen",
            size: 70,
            cell: (info) => (
              <span className="text-xs text-muted-foreground">
                {daysAgo(info.getValue())}
              </span>
            ),
          }),
      columnHelper.accessor("match_score", {
        header: "Score",
        size: 130,
        cell: (info) => <ScoreBar v={info.getValue() ?? 0} />,
      }),
      columnHelper.accessor("title", {
        header: "Title",
        cell: (info) => <span className="font-medium">{info.getValue()}</span>,
      }),
      columnHelper.accessor("company_name", {
        header: "Company",
        size: 180,
        cell: (info) => (
          <span className="text-muted-foreground">{info.getValue()}</span>
        ),
      }),
      columnHelper.accessor("location", {
        header: "Location",
        size: 200,
        cell: (info) => (
          <span className="truncate text-xs text-muted-foreground">
            {info.getValue()}
          </span>
        ),
      }),
      columnHelper.display({
        id: "open",
        header: "",
        size: 44,
        cell: ({ row }) => (
          <a
            href={row.original.job_url}
            target="_blank"
            rel="noreferrer"
            className="text-muted-foreground hover:text-foreground"
            title="Open posting (o)"
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </a>
        ),
      }),
    ],
    [mut, dateMode]
  )

  const table = useReactTable({
    data: rows,
    columns: cols,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  })

  const parentRef = useRef<HTMLDivElement>(null)
  const virtualizer = useVirtualizer({
    count: table.getRowModel().rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 44,
    overscan: 12,
  })

  useEffect(() => {
    virtualizer.scrollToIndex(focusedIndex, { align: "auto" })
  }, [focusedIndex, virtualizer])

  const totalSponsor = rows.filter((r) => r.sponsor_confirmed).length
  const totalNew = rows.filter((r) => r.status === "New").length

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border px-6 py-4">
        <div className="flex items-baseline justify-between">
          <div className="flex items-center gap-2">
            {icon}
            <h1 className="text-xl font-semibold">{title}</h1>
            {isFetching && !isLoading && (
              <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
            )}
          </div>
          <div className="flex items-center gap-6 text-sm">
            <Kpi label="Matches" value={rows.length} />
            <Kpi label="Sponsors" value={totalSponsor} tone="sponsor" />
            <Kpi label="New" value={totalNew} tone="new" />
          </div>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          {description}{" "}
          <span className="ml-2 whitespace-nowrap text-[10px] uppercase tracking-wide text-muted-foreground/70">
            <kbd className="rounded border border-border bg-muted px-1 text-[10px]">j</kbd>/
            <kbd className="rounded border border-border bg-muted px-1 text-[10px]">k</kbd> move ·{" "}
            <kbd className="rounded border border-border bg-muted px-1 text-[10px]">a</kbd> apply ·{" "}
            <kbd className="rounded border border-border bg-muted px-1 text-[10px]">x</kbd> archive ·{" "}
            <kbd className="rounded border border-border bg-muted px-1 text-[10px]">o</kbd> open
          </span>
        </p>
      </div>

      <div className="border-b border-border px-6 py-3">
        <div className="grid grid-cols-1 gap-6 md:grid-cols-3">
          {showScoreSlider ? (
            <FilterField label={`Min score: ${minScore}`}>
              <Slider min={0} max={90} step={5} value={[minScore]} onValueChange={(v) => setMinScore(v[0])} />
            </FilterField>
          ) : (
            <div />
          )}
          {showFreshnessSlider ? (
            <FilterField label={freshDays > 0 ? `Fresh within ${freshDays} d` : "Any age"}>
              <Slider min={0} max={60} step={1} value={[freshDays]} onValueChange={(v) => setFreshDays(v[0])} />
            </FilterField>
          ) : (
            <div />
          )}
          <div className="flex items-center gap-2">
            <Checkbox
              id="hide-applied"
              checked={hideApplied}
              onCheckedChange={(c) => setHideApplied(!!c)}
            />
            <label htmlFor="hide-applied" className="cursor-pointer text-sm">
              Hide jobs I&apos;ve already applied to
            </label>
          </div>
        </div>
      </div>

      <div ref={parentRef} className="relative flex-1 overflow-auto">
        {error ? (
          <div className="flex h-full items-center justify-center text-sm text-destructive">
            Backend unreachable — check the SSH tunnel:{" "}
            <code className="ml-2">ssh -L 8000:localhost:8000 root@143.198.188.116</code>
          </div>
        ) : isLoading ? (
          <div className="flex h-full items-center justify-center text-muted-foreground">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading matches…
          </div>
        ) : rows.length === 0 ? (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            Nothing matches these filters — try lowering the score or widening the window.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="sticky top-0 z-10 bg-card">
              {table.getHeaderGroups().map((hg) => (
                <tr key={hg.id} className="border-b border-border">
                  {hg.headers.map((h) => (
                    <th
                      key={h.id}
                      style={{ width: h.getSize() }}
                      onClick={h.column.getToggleSortingHandler()}
                      className={cn(
                        "h-9 px-3 text-left text-xs font-medium uppercase tracking-wide text-muted-foreground",
                        h.column.getCanSort() && "cursor-pointer select-none hover:text-foreground"
                      )}
                    >
                      {flexRender(h.column.columnDef.header, h.getContext())}
                      {h.column.getIsSorted() === "asc" && " ↑"}
                      {h.column.getIsSorted() === "desc" && " ↓"}
                    </th>
                  ))}
                </tr>
              ))}
            </thead>
            <tbody
              style={{
                height: `${virtualizer.getTotalSize()}px`,
                position: "relative",
                display: "block",
              }}
            >
              {virtualizer.getVirtualItems().map((vr) => {
                const row = table.getRowModel().rows[vr.index]
                const isFocused = vr.index === focusedIndex
                return (
                  <tr
                    key={row.id}
                    onClick={() => setFocusedIndex(vr.index)}
                    style={{
                      position: "absolute",
                      top: 0,
                      left: 0,
                      width: "100%",
                      transform: `translateY(${vr.start}px)`,
                      display: "table",
                      tableLayout: "fixed",
                    }}
                    className={cn(
                      "border-b border-border/50 cursor-pointer hover:bg-accent/40",
                      isFocused && "bg-accent/60 ring-1 ring-inset ring-primary/50"
                    )}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <td
                        key={cell.id}
                        style={{ width: cell.column.getSize() }}
                        className="truncate px-3 py-2 align-middle"
                      >
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </td>
                    ))}
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
