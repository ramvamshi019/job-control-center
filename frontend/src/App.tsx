import { useEffect, useState } from "react"
import { ExternalLink, Flame, Sparkles } from "lucide-react"
import BestMatches from "@/pages/BestMatches"
import PostedToday from "@/pages/PostedToday"
import { cn } from "@/lib/utils"

// Only two live pages so far. The rest link into the legacy Streamlit
// dashboard on the sibling subdomain -- that way every workflow Ram had
// yesterday keeps working from day one of the new UI, and the sidebar
// stops being a wall of "SOON" placeholders.
type Route = "best" | "posted"

// Legacy Streamlit dashboard: same origin, different subdomain, same auth.
// Anything not built here yet routes there for a live version.
const LEGACY = "https://143.198.188.116.sslip.io"

function currentRoute(): Route {
  const h = window.location.hash.replace(/^#\/?/, "").split("/")[0]
  return h === "posted" ? "posted" : "best"
}

export default function App() {
  const [route, setRoute] = useState<Route>(currentRoute)

  useEffect(() => {
    const onHash = () => setRoute(currentRoute())
    window.addEventListener("hashchange", onHash)
    return () => window.removeEventListener("hashchange", onHash)
  }, [])

  const nav = (r: Route) => {
    window.location.hash = `#/${r}`
    setRoute(r)
  }

  return (
    <div className="flex h-screen w-screen">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-border bg-card md:flex">
        <div className="flex items-center gap-2 border-b border-border px-4 py-4">
          <span className="text-lg">🎯</span>
          <span className="font-semibold">Job Control</span>
        </div>

        <nav className="flex flex-col gap-0.5 p-2">
          <div className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/70">
            New UI
          </div>
          <NavItem
            label="Best Matches"
            icon={<Sparkles className="h-4 w-4" />}
            active={route === "best"}
            onClick={() => nav("best")}
          />
          <NavItem
            label="Posted Today"
            icon={<Flame className="h-4 w-4 text-rose-400" />}
            active={route === "posted"}
            onClick={() => nav("posted")}
          />

          <div className="mt-4 px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/70">
            Full dashboard
          </div>
          <LegacyLink emoji="🔥" label="Fresh (apply now)" path="#page=%F0%9F%94%A5+Fresh+(apply+now)" />
          <LegacyLink emoji="⚡" label="Fast Apply" path="#page=%E2%9A%A1+Fast+Apply" />
          <LegacyLink emoji="🔍" label="Need Review" path="#page=Need+Review" />
          <LegacyLink emoji="✅" label="Applied" path="#page=Applied" />
          <LegacyLink emoji="🏢" label="Companies" path="#page=Companies" />
          <LegacyLink emoji="📊" label="Stats" path="#page=Stats" />
        </nav>

        <div className="mt-auto border-t border-border/50 p-3 text-[10px] leading-snug text-muted-foreground">
          Phase 1 · React + shadcn/ui
          <br />
          Full dashboard opens the legacy Streamlit UI (same login).
        </div>
      </aside>

      <main className="min-w-0 flex-1">
        {route === "posted" ? <PostedToday /> : <BestMatches />}
      </main>
    </div>
  )
}

function NavItem({
  label,
  icon,
  active,
  onClick,
}: {
  label: string
  icon: React.ReactNode
  active?: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex items-center gap-2 rounded-md px-2.5 py-2 text-sm transition-colors",
        active
          ? "bg-accent font-medium text-foreground"
          : "text-muted-foreground hover:bg-accent/60 hover:text-foreground"
      )}
    >
      {icon}
      <span>{label}</span>
    </button>
  )
}

function LegacyLink({
  emoji,
  label,
  path,
}: {
  emoji: string
  label: string
  path: string
}) {
  return (
    <a
      href={`${LEGACY}/${path}`}
      target="_blank"
      rel="noreferrer"
      className="group flex items-center justify-between rounded-md px-2.5 py-2 text-sm text-muted-foreground/80 transition-colors hover:bg-accent/60 hover:text-foreground"
      title="Opens in the legacy dashboard"
    >
      <span className="flex items-center gap-2">
        <span className="text-base">{emoji}</span>
        <span>{label}</span>
      </span>
      <ExternalLink className="h-3 w-3 opacity-0 transition-opacity group-hover:opacity-60" />
    </a>
  )
}
