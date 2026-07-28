import { useEffect, useState } from "react"
import BestMatches from "@/pages/BestMatches"
import PostedToday from "@/pages/PostedToday"

// Only two live pages so far. Rest are marked SOON.
// Route via the URL hash so back/forward + share-a-link both work; the hash
// approach also survives a hot reload without React Router.
type Route = "best" | "posted"

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
      <aside className="hidden w-56 shrink-0 flex-col border-r border-border bg-card md:flex">
        <div className="flex items-center gap-2 border-b border-border px-4 py-4">
          <span className="text-lg">🎯</span>
          <span className="font-semibold">Job Control</span>
        </div>
        <nav className="flex flex-col p-2">
          <NavItem
            label="Best Matches"
            icon="🎯"
            active={route === "best"}
            onClick={() => nav("best")}
          />
          <NavItem
            label="Posted Today"
            icon="🔴"
            active={route === "posted"}
            onClick={() => nav("posted")}
          />
          <NavItem label="Fresh (24h)" icon="🔥" hint="soon" />
          <NavItem label="Fast Apply" icon="⚡" hint="soon" />
          <NavItem label="Need Review" icon="🔍" hint="soon" />
          <NavItem label="Applied" icon="✅" hint="soon" />
          <NavItem label="Companies" icon="🏢" hint="soon" />
          <NavItem label="Stats" icon="📊" hint="soon" />
        </nav>
        <div className="mt-auto px-4 py-3 text-xs text-muted-foreground">
          Phase 1 · React + shadcn/ui
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
  hint,
  onClick,
}: {
  label: string
  icon: string
  active?: boolean
  hint?: string
  onClick?: () => void
}) {
  const disabled = !onClick && !active
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={
        active
          ? "flex items-center justify-between rounded-md bg-accent px-3 py-2 text-sm font-medium"
          : "flex items-center justify-between rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-accent/60 disabled:opacity-50 disabled:hover:bg-transparent"
      }
    >
      <span className="flex items-center gap-2">
        <span>{icon}</span>
        {label}
      </span>
      {hint && <span className="text-[10px] uppercase tracking-wide">{hint}</span>}
    </button>
  )
}
