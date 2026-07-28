import BestMatches from "@/pages/BestMatches"

export default function App() {
  return (
    <div className="flex h-screen w-screen">
      {/* Sidebar */}
      <aside className="hidden w-56 shrink-0 flex-col border-r border-border bg-card md:flex">
        <div className="flex items-center gap-2 border-b border-border px-4 py-4">
          <span className="text-lg">🎯</span>
          <span className="font-semibold">Job Control</span>
        </div>
        <nav className="flex flex-col p-2">
          <NavItem active label="Best Matches" icon="🎯" />
          <NavItem label="Fresh (24h)" icon="🔥" hint="soon" />
          <NavItem label="Fast Apply" icon="⚡" hint="soon" />
          <NavItem label="Posted Today" icon="🔴" hint="soon" />
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
        <BestMatches />
      </main>
    </div>
  )
}

function NavItem({
  label,
  icon,
  active,
  hint,
}: {
  label: string
  icon: string
  active?: boolean
  hint?: string
}) {
  return (
    <button
      disabled={!active}
      className={
        active
          ? "flex items-center justify-between rounded-md bg-accent px-3 py-2 text-sm font-medium"
          : "flex items-center justify-between rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-accent/60 disabled:opacity-50"
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
