// frontend/src/lib/trainingEvents.ts
/**
 * What a training event's lifecycle actions say when they succeed, when they are refused, and
 * what state each learner's lab is actually in.
 *
 * Every handler on both event pages ended at `console.error`, so an instructor pressing "Start &
 * Deploy Labs" against a blueprint the substrate will not deploy met a button that did nothing --
 * no toast, no banner, no change -- with a diagnosable refusal sitting in the response. The
 * per-lab reading is here for the same reason: a started event reported one opaque "Running"
 * while each learner's range succeeded or failed on its own.
 */
import { isAxiosError } from 'axios'
import { apiErrorDetail } from './blueprints'

const ACTION_WORDS = {
  publish: { infinitive: 'publish', progressive: 'Publishing', done: 'Published' },
  start: { infinitive: 'start', progressive: 'Starting', done: 'Started' },
  complete: { infinitive: 'complete', progressive: 'Completing', done: 'Completed' },
  cancel: { infinitive: 'cancel', progressive: 'Cancelling', done: 'Cancelled' },
  reactivate: { infinitive: 'reactivate', progressive: 'Reactivating', done: 'Reactivated' },
  delete: { infinitive: 'delete', progressive: 'Deleting', done: 'Deleted' },
  join: { infinitive: 'join', progressive: 'Joining', done: 'Joined' },
} as const

export type EventAction = keyof typeof ACTION_WORDS

export function eventActionWords(action: EventAction) {
  return ACTION_WORDS[action]
}

/** What an instructor is told when the server refuses a lifecycle action. */
export function lifecycleFailureMessage(
  action: EventAction,
  eventName: string,
  err: unknown
): string {
  const reason = apiErrorDetail(err, 'the server gave no reason')
  return `Could not ${ACTION_WORDS[action].infinitive} "${eventName}": ${reason}`
}

/**
 * Two things an instructor needs after a refused start: the server's reason, and whether the
 * cohort was left alone.
 *
 * The server validates the blueprint and every learner's range before it commits anything, so a
 * refusal it sent means nothing was created and the blueprint can be fixed and the button pressed
 * again. A request that never got an answer promises nothing of the sort, and claiming it did
 * would send the instructor back to a page that has already moved on without them.
 */
export function startFailureNotice(err: unknown): { reason: string; aftermath: string } {
  const refused = isAxiosError(err) && !!err.response && err.response.status < 500
  return {
    reason: apiErrorDetail(err, 'The server gave no reason.'),
    aftermath: refused
      ? 'The event was not started and no labs were created.'
      : 'The event may have started anyway — reload the page to see what exists.',
  }
}

/** One participant's lab, as the instructor has to read it at a glance. */
export interface LabState {
  label: string
  tone: string
  spinner: boolean
}

const LAB_STATES: Record<string, LabState> = {
  draft: { label: 'Queued', tone: 'bg-blue-100 text-blue-700', spinner: true },
  deploying: { label: 'Deploying', tone: 'bg-yellow-100 text-yellow-700', spinner: true },
  running: { label: 'Running', tone: 'bg-green-100 text-green-700', spinner: false },
  stopped: { label: 'Stopped', tone: 'bg-gray-100 text-gray-700', spinner: false },
  archived: { label: 'Archived', tone: 'bg-gray-100 text-gray-700', spinner: false },
  error: { label: 'Failed', tone: 'bg-red-100 text-red-700', spinner: false },
}

/**
 * An unrecognised status is shown as itself rather than hidden: the two substrates do not share
 * a status vocabulary, and inventing a label for one this build has not seen would be the same
 * mistake as reporting a failed range as merely started.
 */
export function labState(rangeStatus: string | null | undefined): LabState {
  if (!rangeStatus) {
    return { label: 'Unknown', tone: 'bg-gray-100 text-gray-500', spinner: false }
  }
  return (
    LAB_STATES[rangeStatus] ?? {
      label: rangeStatus,
      tone: 'bg-gray-100 text-gray-700',
      spinner: false,
    }
  )
}

export interface LabRollup {
  total: number
  running: number
  deploying: number
  failed: number
  stopped: number
  missing: number
  other: number
}

export interface LabParticipant {
  role: string
  range_id?: string
  range_status?: string
}

/** Only students get a lab, so only students are counted -- an instructor is not a missing one. */
export function rollupLabs(participants: LabParticipant[]): LabRollup {
  const students = participants.filter((p) => p.role === 'student')
  const rollup: LabRollup = {
    total: students.length,
    running: 0,
    deploying: 0,
    failed: 0,
    stopped: 0,
    missing: 0,
    other: 0,
  }
  for (const student of students) {
    if (!student.range_id) rollup.missing += 1
    else if (student.range_status === 'running') rollup.running += 1
    else if (student.range_status === 'deploying' || student.range_status === 'draft') {
      rollup.deploying += 1
    } else if (student.range_status === 'error') rollup.failed += 1
    else if (student.range_status === 'stopped' || student.range_status === 'archived') {
      rollup.stopped += 1
    } else rollup.other += 1
  }
  return rollup
}

export function summarizeLabs(rollup: LabRollup): string {
  const parts: string[] = []
  if (rollup.running) parts.push(`${rollup.running} running`)
  if (rollup.deploying) parts.push(`${rollup.deploying} deploying`)
  if (rollup.failed) parts.push(`${rollup.failed} failed`)
  if (rollup.stopped) parts.push(`${rollup.stopped} stopped`)
  if (rollup.missing) parts.push(`${rollup.missing} not deployed`)
  if (rollup.other) parts.push(`${rollup.other} in another state`)
  return parts.length ? parts.join(' · ') : 'No student labs'
}
