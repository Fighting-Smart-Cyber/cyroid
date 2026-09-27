// frontend/src/lib/failureText.ts
/**
 * The words a failure is shown with, whether it was just caught or was recorded earlier.
 *
 * Two different things end up in front of a user as "why that did not work": the error a request
 * came back with, and the reason a range stored when its deploy failed. Both used to be read out
 * of their carrier at the call site -- `err.response?.data?.detail || 'Failed to ...'` -- which
 * is how an object reached a toast and how a whole class of refusal reached nobody.
 */
import { isAxiosError } from 'axios'
import { formatErrorDetail } from './apiError'
import { detailToMessage } from './rangeDetail'

/** Whether a `detail` is the `{message, errors, hint}` shape deploy and sync refuse with. */
function isStructuredRefusal(detail: unknown): boolean {
  if (detail === null || typeof detail !== 'object' || Array.isArray(detail)) return false
  const shape = detail as { message?: unknown; errors?: unknown; hint?: unknown }
  return (
    typeof shape.message === 'string' || Array.isArray(shape.errors) || typeof shape.hint === 'string'
  )
}

/**
 * What to tell the user about a failed request, in one sentence they can act on.
 *
 * Most of the product's refusals are a string and go to the shared `apiErrorDetail` path. Deploy
 * and sync are the exception: they answer with an object carrying the list of things to go and
 * fix, and that list is the entire value of the message -- flattening it to `detail` would print
 * "[object Object]" and flattening it to the fallback would silently drop it.
 *
 * A failure that is not an HTTP response at all -- the request never left, or the server never
 * answered -- carries nothing but an exception string naming our own internals, so it gets the
 * caller's fallback instead. The user cannot act on `Network Error` either way, and the
 * difference between the two is in the console, where it belongs.
 */
export function actionFailureMessage(err: unknown, fallback: string): string {
  if (!isAxiosError(err)) return fallback
  const detail = err.response?.data?.detail
  if (isStructuredRefusal(detail)) return detailToMessage(detail, fallback)
  return formatErrorDetail(detail) ?? fallback
}

/**
 * The reason a record carries for its own failed state, or null when it carries none worth
 * showing.
 *
 * The distinction this makes is between a record with no reason and a record whose reason is
 * whitespace. The backend stores `str(exc)`, and an exception raised with no message stores the
 * empty string -- rendered, that paints an empty red line, which reads as a render that dropped
 * something rather than as a failure nobody wrote a reason for. Line breaks inside the message
 * are left alone: what collapses them is the caller's CSS, and the caller is the one that knows
 * whether it has one line to spend or a paragraph.
 */
export function recordedFailureReason(message: string | null | undefined): string | null {
  if (typeof message !== 'string') return null
  const trimmed = message.trim()
  return trimmed === '' ? null : trimmed
}

/** The statuses a recorded reason is still the reason for. Any other has moved on from it. */
const FAILED_STATUSES = new Set(['error', 'failed'])

/**
 * The recorded reason, but only where it still describes the state shown beside it.
 *
 * The stored reason is not reliably cleared when a record leaves its failed state. Every
 * successful Kubernetes transition nulls it; several Docker ones do not -- tearing a failed range
 * down sets it to Draft and leaves the reason on the row, and the DinD recovery endpoint flips a
 * range to Running the same way. Rendered under whatever status the row happens to carry, that
 * paints a red failure paragraph on a draft range and beneath a green Running badge, which is
 * worse than the silence it replaced: the badge and the sentence under it contradict each other
 * and nothing on screen says which one is stale. Gating on the status is what the machine rows on
 * the range page already do with their own `error_message`.
 */
export function failureReasonForStatus(
  status: string | null | undefined,
  message: string | null | undefined
): string | null {
  if (typeof status !== 'string' || !FAILED_STATUSES.has(status.toLowerCase())) return null
  return recordedFailureReason(message)
}
