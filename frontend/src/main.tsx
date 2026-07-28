import React from "react"
import ReactDOM from "react-dom/client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import App from "./App"
import "./index.css"

const qc = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      // Auto-refresh so the counts don't go stale while the tab sits open.
      // 60s matches the livewatch crawler interval — sees each fresh batch as
      // it lands. `refetchIntervalInBackground: false` (default) pauses the
      // poll when the tab is hidden, so we don't burn the API for nothing.
      refetchInterval: 60_000,
      // And any time the user switches back to the tab, refetch immediately
      // so they see the newest count without waiting for the next tick.
      refetchOnWindowFocus: true,
      // Treat cached data as stale immediately -- a re-focus / interval tick
      // always hits the network, no "still fresh, skipping" gaps.
      staleTime: 0,
    },
  },
})

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={qc}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>
)
