import { useEffect, useState } from "react"

interface Handlers {
  onApply: (id: number) => void
  onArchive: (id: number) => void
  onOpen: (url: string) => void
}

/**
 * Turns Best Matches into a keyboard-driven apply loop.
 *
 *   j / ArrowDown  -> next row
 *   k / ArrowUp    -> previous row
 *   a              -> mark focused row applied
 *   x              -> archive focused row
 *   o / Enter      -> open posting in new tab
 *
 * Shortcuts are ignored while the user is typing into an input/textarea, so
 * the sliders and checkboxes stay usable. Focused row index is auto-clamped
 * when the list shrinks (e.g. after apply hides the row).
 */
export function useJobKeyboardNav<T extends { id: number; job_url: string }>(
  rows: T[],
  handlers: Handlers
): { focusedIndex: number; setFocusedIndex: (i: number) => void } {
  const [focusedIndex, setFocusedIndex] = useState(0)

  useEffect(() => {
    if (focusedIndex >= rows.length) setFocusedIndex(Math.max(0, rows.length - 1))
  }, [rows.length, focusedIndex])

  useEffect(() => {
    const isTypingTarget = (el: EventTarget | null) => {
      if (!(el instanceof HTMLElement)) return false
      const tag = el.tagName
      return (
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        tag === "SELECT" ||
        el.isContentEditable
      )
    }

    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      if (isTypingTarget(e.target)) return
      const key = e.key

      const move = (delta: number) => {
        e.preventDefault()
        setFocusedIndex((i) =>
          Math.min(Math.max(0, i + delta), Math.max(0, rows.length - 1))
        )
      }

      if (key === "j" || key === "ArrowDown") return move(1)
      if (key === "k" || key === "ArrowUp") return move(-1)

      const row = rows[focusedIndex]
      if (!row) return

      if (key === "a") {
        e.preventDefault()
        handlers.onApply(row.id)
        return
      }
      if (key === "x") {
        e.preventDefault()
        handlers.onArchive(row.id)
        return
      }
      if (key === "o" || key === "Enter") {
        e.preventDefault()
        handlers.onOpen(row.job_url)
        return
      }
      if (key === "g") {
        // Vim-style "gg" — jump to top. Cheap variant: single-press g.
        e.preventDefault()
        setFocusedIndex(0)
        return
      }
      if (key === "G") {
        e.preventDefault()
        setFocusedIndex(Math.max(0, rows.length - 1))
      }
    }

    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [rows, focusedIndex, handlers])

  return { focusedIndex, setFocusedIndex }
}
