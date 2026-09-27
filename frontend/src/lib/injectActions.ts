// frontend/src/lib/injectActions.ts
/**
 * Reading an inject's stored actions without trusting their shape.
 *
 * The MSEL parser writes `{action_type, parameters: {...}}` and the API hands that JSON straight
 * back, while the frontend's `InjectAction` type declares `{type, target_vm, path, command}`. The
 * timeline rendered `action.type.replace(...)`, which for every inject the product actually
 * produces is `undefined.replace` -- a TypeError thrown during render, and with no error boundary
 * above it that blanks the entire application in the middle of an exercise.
 *
 * These read either shape and return something printable for anything else, so an action the
 * parser learns to emit tomorrow renders as text rather than taking the page down.
 */

const ACTION_LABELS: Record<string, string> = {
  place_file: 'Place file',
  run_command: 'Run command',
}

export interface InjectActionView {
  /** What the action does, in words. */
  label: string
  /** The machine it targets, or null when the stored action names none. */
  target: string | null
  path: string | null
  command: string | null
  filename: string | null
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

function text(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed
}

function label(kind: string | null): string {
  if (!kind) return 'Action'
  return ACTION_LABELS[kind] ?? kind.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase())
}

/** One stored action, flattened to what the timeline prints. */
export function describeInjectAction(action: unknown): InjectActionView {
  const raw = asRecord(action)
  const params = asRecord(raw.parameters)
  return {
    label: label(text(raw.action_type) ?? text(raw.type)),
    target: text(params.target_vm) ?? text(raw.target_vm),
    path: text(params.target_path) ?? text(params.path) ?? text(raw.path),
    command: text(params.command) ?? text(raw.command),
    filename: text(params.filename) ?? text(raw.filename),
  }
}

/** Every action on an inject, for a field that may be absent, null or not a list at all. */
export function describeInjectActions(actions: unknown): InjectActionView[] {
  return Array.isArray(actions) ? actions.map(describeInjectAction) : []
}

/**
 * Why an inject did not run, read out of the per-action results.
 *
 * The execute route answers 200 whatever happened and puts the outcome in the body, so a refusal
 * -- "this substrate has no guest-side path", "placing a file was never implemented" -- reached
 * the timeline as a red row and nothing else. The reason is the whole point of a refusal, and it
 * was being dropped on the floor by the one screen that shows it.
 *
 * The service reports an error either directly on the entry or under the action's `result`, so
 * both are read; the first reason found is the one the instructor needs to act on.
 */
export function injectRefusalText(results: unknown): string | null {
  if (!Array.isArray(results)) return null
  for (const entry of results) {
    const row = asRecord(entry)
    const reason = text(row.error) ?? text(asRecord(row.result).error)
    if (reason) return reason
  }
  return null
}

/**
 * An inject's offset from exercise start as `T+HH:MM`.
 *
 * A missing or non-numeric offset used to render `T+NaN:NaN`, which reads like a defect in the
 * scenario rather than a gap in it.
 */
export function formatInjectTime(minutes: unknown): string {
  if (typeof minutes !== 'number' || !Number.isFinite(minutes) || minutes < 0) return 'T+--:--'
  const whole = Math.floor(minutes)
  const hours = Math.floor(whole / 60)
  const mins = whole % 60
  return `T+${hours.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}`
}
