# JCC Frontend — Phase 1

Modern replacement for the Streamlit dashboard. Built with:

- **React 18** + TypeScript + Vite
- **Tailwind CSS** + shadcn-style components (Radix primitives)
- **TanStack Query** for cache-first data fetching (no full-page reruns)
- **TanStack Table** + Virtualizer for a 60fps grid over any row count

Currently ships one page: **🎯 Best Matches**. The other pages
(Fresh / Fast Apply / Posted Today / Need Review / Applied / Companies / Stats)
are stubbed in the sidebar; add them one at a time in follow-up phases and
retire the Streamlit dashboard when parity is reached.

## Local development

The FastAPI backend runs on the droplet at :8000 and is bound to 127.0.0.1
only. Open an SSH tunnel first so Vite's `/api/*` proxy can reach it:

```bash
ssh -N -L 8000:localhost:8000 root@143.198.188.116
```

Then in a second terminal:

```bash
cd frontend
npm install
npm run dev            # -> http://localhost:5173
```

The `/api/*` requests get proxied to `http://localhost:8000` per
`vite.config.ts`.

## Production

`npm run build` writes a static bundle to `dist/`. On the droplet, point
Caddy at that directory and reverse-proxy `/api/*` to the backend container
on the same host. Nothing else needs to change server-side.
