// frontend/src/lib/apiError.ts
/**
 * The message a refusal carries, in one place.
 *
 * A FastAPI schema refusal arrives as a list of `{loc, msg}` objects rather than a string.
 * `toast.error(err.response?.data?.detail)` therefore handed a toast an array: React renders it
 * as "Objects are not valid as a React child" and takes the panel down, or prints
 * "[object Object]" — either way the one thing the user needed to read is the one thing hidden.
 * Every catch in the app should go through this rather than reach into the response itself.
 */
import { isAxiosError } from 'axios'

function asRecord(value: unknown): Record<string, unknown> | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

function text(value: unknown): string | null {
  if (typeof value === 'string' && value.trim() !== '') return value
  if (typeof value === 'number') return String(value)
  return null
}

/** A `detail` of any shape as one readable string, or null when it says nothing. */
export function formatErrorDetail(detail: unknown): string | null {
  if (typeof detail === 'string' && detail.trim() !== '') return detail
  if (Array.isArray(detail)) {
    const lines = detail.flatMap((raw) => {
      const entry = asRecord(raw)
      if (!entry) return typeof raw === 'string' ? [raw] : []
      const message = text(entry.msg)
      if (!message) return []
      const where = (Array.isArray(entry.loc) ? entry.loc : [])
        .map((part) => text(part))
        .filter((part): part is string => part !== null)
        .join('.')
      return [where ? `${where}: ${message}` : message]
    })
    if (lines.length > 0) return lines.join('\n')
  }
  if (detail !== null && detail !== undefined) {
    const record = asRecord(detail)
    const message = record ? text(record.msg) ?? text(record.detail) : null
    if (message) return message
  }
  return null
}

/** What went wrong, from an axios error, or `fallback` when the server said nothing useful. */
export function apiErrorDetail(err: unknown, fallback: string): string {
  if (!isAxiosError(err)) return fallback
  return formatErrorDetail(err.response?.data?.detail) ?? fallback
}

/** The HTTP status, when there was a response at all. */
export function apiErrorStatus(err: unknown): number | null {
  return isAxiosError(err) ? err.response?.status ?? null : null
}
