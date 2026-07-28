import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—"
  return iso.slice(0, 10)
}

export function daysAgo(iso: string | null | undefined): string {
  if (!iso) return "—"
  const d = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000)
  if (d <= 0) return "today"
  if (d === 1) return "1d ago"
  return `${d}d ago`
}

/**
 * Human-friendly age with a real date fallback. "today" / "2d ago" for recent,
 * short month-day ("Jul 22") for older. Never returns raw ISO — that's what
 * was making Posted Today unreadable.
 */
export function relativeDate(iso: string | null | undefined): string {
  if (!iso) return "—"
  const then = new Date(iso).getTime()
  const days = Math.floor((Date.now() - then) / 86_400_000)
  if (days <= 0) return "today"
  if (days === 1) return "yesterday"
  if (days < 7) return `${days}d ago`
  return new Date(then).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  })
}
