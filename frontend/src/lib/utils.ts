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
  if (d === 1) return "1d"
  return `${d}d`
}
