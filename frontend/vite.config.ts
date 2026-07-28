import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import path from "path"

// The FastAPI backend runs on the droplet at :8000, exposed only on
// 127.0.0.1. During dev we open an SSH tunnel (see README): the tunnel
// forwards localhost:8000 -> droplet:8000, and this proxy points at it.
// In production the frontend is a static bundle served by Caddy, and Caddy
// reverse-proxies /api/* to the backend on the same host -- no proxy here
// affects that path.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
})
