import { describe, it, expect } from 'vitest'
import {
  labState,
  lifecycleFailureMessage,
  rollupLabs,
  startFailureNotice,
  summarizeLabs,
} from './trainingEvents'

/**
 * Every lifecycle handler on both event pages ended at console.error, so an instructor pressing
 * "Start & Deploy Labs" against a blueprint this substrate will not deploy saw nothing at all.
 * These cover the two things that has to produce instead: the server's own reason, and an honest
 * statement of what is left behind.
 */

// `isAxiosError` is a shape check, so a literal of the same shape is enough to drive these.
const refusal = (status: number, detail: unknown) => ({
  isAxiosError: true,
  response: { status, data: { detail } },
})

describe('lifecycleFailureMessage', () => {
  it('carries the reason the server gave', () => {
    expect(
      lifecycleFailureMessage(
        'start',
        'Logistics 101',
        refusal(400, 'Range uses a v1 (Era A) blueprint')
      )
    ).toBe('Could not start "Logistics 101": Range uses a v1 (Era A) blueprint')
  })

  it('reads a schema refusal rather than printing [object Object]', () => {
    const message = lifecycleFailureMessage(
      'publish',
      'Logistics 101',
      refusal(422, [{ loc: ['body', 'name'], msg: 'field required' }])
    )
    expect(message).toBe('Could not publish "Logistics 101": body.name: field required')
  })

  it('falls back only when there is nothing to report', () => {
    expect(lifecycleFailureMessage('delete', 'Logistics 101', new Error('offline'))).toBe(
      'Could not delete "Logistics 101": the server gave no reason'
    )
  })

  it('names the action it was asked about', () => {
    for (const [action, verb] of [
      ['complete', 'complete'],
      ['cancel', 'cancel'],
      ['reactivate', 'reactivate'],
      ['join', 'join'],
    ] as const) {
      expect(lifecycleFailureMessage(action, 'E', refusal(400, 'no'))).toBe(
        `Could not ${verb} "E": no`
      )
    }
  })
})

describe('startFailureNotice', () => {
  it('promises the cohort is untouched only when the server refused', () => {
    const notice = startFailureNotice(refusal(400, 'Invalid blueprint configuration'))
    expect(notice.reason).toBe('Invalid blueprint configuration')
    expect(notice.aftermath).toBe('The event was not started and no labs were created.')
  })

  it('makes no such promise when the request never got an answer', () => {
    expect(startFailureNotice(new Error('Network Error')).aftermath).toMatch(/reload the page/)
  })

  it('makes no such promise on a server error, which may have been raised after the commit', () => {
    expect(startFailureNotice(refusal(500, null)).aftermath).toMatch(/reload the page/)
  })
})

describe('labState', () => {
  it('spins while a lab is still being built', () => {
    expect(labState('deploying')).toMatchObject({ label: 'Deploying', spinner: true })
    expect(labState('draft')).toMatchObject({ label: 'Queued', spinner: true })
  })

  it('calls a failed range failed', () => {
    expect(labState('error').label).toBe('Failed')
  })

  it('shows a status it does not know as itself', () => {
    expect(labState('quiescing').label).toBe('quiescing')
  })

  it('does not invent a state for a range whose status is missing', () => {
    expect(labState(undefined).label).toBe('Unknown')
    expect(labState(null).label).toBe('Unknown')
  })
})

describe('rollupLabs', () => {
  const cohort = [
    { role: 'instructor', user_id: 'i' },
    { role: 'student', range_id: 'a', range_status: 'running' },
    { role: 'student', range_id: 'b', range_status: 'deploying' },
    { role: 'student', range_id: 'c', range_status: 'draft' },
    { role: 'student', range_id: 'd', range_status: 'error' },
    { role: 'student', range_id: 'e', range_status: 'stopped' },
    { role: 'student' },
  ]

  it('counts students only, so an instructor is never a missing lab', () => {
    expect(rollupLabs(cohort).total).toBe(6)
  })

  it('separates the states an instructor has to act on differently', () => {
    expect(rollupLabs(cohort)).toEqual({
      total: 6,
      running: 1,
      deploying: 2,
      failed: 1,
      stopped: 1,
      missing: 1,
      other: 0,
    })
  })

  it('treats a range whose status is unreadable as its own case rather than as running', () => {
    const rollup = rollupLabs([{ role: 'student', range_id: 'a' }])
    expect(rollup.other).toBe(1)
    expect(rollup.running).toBe(0)
  })
})

describe('summarizeLabs', () => {
  it('reports a cohort where not every lab came up', () => {
    expect(
      summarizeLabs({
        total: 4,
        running: 2,
        deploying: 0,
        failed: 1,
        stopped: 0,
        missing: 1,
        other: 0,
      })
    ).toBe('2 running · 1 failed · 1 not deployed')
  })

  it('says so rather than rendering an empty line', () => {
    expect(
      summarizeLabs({
        total: 0,
        running: 0,
        deploying: 0,
        failed: 0,
        stopped: 0,
        missing: 0,
        other: 0,
      })
    ).toBe('No student labs')
  })
})
