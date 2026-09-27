import { describe, it, expect, vi } from 'vitest'

// Importing the page pulls in the auth store, which reads localStorage at module scope. These
// tests cover pure functions and never touch it, but vitest runs in node, where there is none --
// so it has to exist before the import below is hoisted past this point.
vi.hoisted(() => {
  const entries = new Map<string, string>()
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    writable: true,
    value: {
      getItem: (key: string) => entries.get(key) ?? null,
      setItem: (key: string, value: string) => void entries.set(key, value),
      removeItem: (key: string) => void entries.delete(key),
      clear: () => entries.clear(),
      key: () => null,
      length: 0,
    },
  })
})

import {
  calendarCells,
  eventLabOffer,
  eventStatusLabel,
  eventsOnDay,
  labAvailability,
  labSummary,
} from './StudentPortal'

describe('labAvailability', () => {
  it('opens only a running lab', () => {
    expect(labAvailability('running').canOpen).toBe(true)
    for (const status of ['draft', 'deploying', 'stopped', 'archived', 'error']) {
      expect(labAvailability(status).canOpen).toBe(false)
    }
  })

  it('gives every closed state a reason the learner can act on', () => {
    for (const status of ['draft', 'deploying', 'stopped', 'archived', 'error']) {
      expect(labAvailability(status).note).toBeTruthy()
    }
    expect(labAvailability('running').note).toBeNull()
  })

  // A status this build has not heard of must not become an open control that then fails.
  it('refuses an unrecognised status rather than offering it', () => {
    const unknown = labAvailability('reconciling')
    expect(unknown.canOpen).toBe(false)
    expect(unknown.note).toBeTruthy()
  })

  // The lab page draws the guide with or without a console, so a shut lab still has something
  // in it to read. A closed or unrecognised one does not.
  it('keeps the guide reachable while the lab is merely not running', () => {
    for (const status of ['draft', 'deploying', 'stopped', 'error']) {
      expect(labAvailability(status).guideReachable).toBe(true)
    }
    expect(labAvailability('archived').guideReachable).toBe(false)
    expect(labAvailability('reconciling').guideReachable).toBe(false)
  })

  it('never shows the learner a deployment word', () => {
    const labels = ['running', 'draft', 'deploying', 'stopped', 'archived', 'error'].map(
      (s) => labAvailability(s).label
    )
    for (const label of labels) {
      expect(label).not.toMatch(/deploying|draft|archived/i)
    }
  })
})

describe('labSummary', () => {
  it('counts machines and applications on Kubernetes, never VMs or networks', () => {
    expect(labSummary({ substrate: 'kubernetes', machines: 2, applications: 1 })).toBe(
      '2 machines · 1 application'
    )
  })

  it('keeps the Era A words on the Docker substrate', () => {
    expect(labSummary({ substrate: 'dind', vms: 1, networks: 3 })).toBe('1 VM · 3 networks')
  })

  it('leaves out a zero rather than asserting emptiness', () => {
    expect(labSummary({ substrate: 'kubernetes', machines: 0, applications: 2 })).toBe(
      '2 applications'
    )
    expect(labSummary({ substrate: 'dind', vms: 2, networks: 0 })).toBe('2 VMs')
  })

  // The card renders nothing at all in this case; "0 machines" reads as a broken lab.
  it('says nothing when there is nothing to count', () => {
    expect(labSummary({ substrate: 'kubernetes', machines: 0, applications: 0 })).toBeNull()
    expect(labSummary({ substrate: 'dind', vms: 0, networks: 0 })).toBeNull()
  })
})

describe('eventLabOffer', () => {
  it('offers the lab when the event is running and the lab is too', () => {
    expect(eventLabOffer('running', 'r1', { r1: 'running' })).toEqual({
      openRangeId: 'r1',
      note: null,
    })
  })

  // The card used to offer this unconditionally, contradicting the same lab's own card above.
  it('refuses a lab that has not started, and says why', () => {
    const offer = eventLabOffer('running', 'r1', { r1: 'deploying' })
    expect(offer.openRangeId).toBeNull()
    expect(offer.note).toBe(labAvailability('deploying').note)
  })

  it('offers nothing at all when the event is not running or no lab is assigned', () => {
    expect(eventLabOffer('scheduled', 'r1', { r1: 'running' })).toEqual({
      openRangeId: null,
      note: null,
    })
    expect(eventLabOffer('running', undefined, {})).toEqual({ openRangeId: null, note: null })
  })

  // Not knowing is not evidence: "My labs" may still be loading, or may have failed outright.
  it('still offers the lab when its state is unknown', () => {
    expect(eventLabOffer('running', 'r1', {})).toEqual({ openRangeId: 'r1', note: null })
  })
})

describe('eventStatusLabel', () => {
  it('says what a running event means to the person attending it', () => {
    expect(eventStatusLabel('running')).toBe('In progress')
    expect(eventStatusLabel('scheduled')).toBe('Scheduled')
  })

  it('still renders a status it does not know', () => {
    expect(eventStatusLabel('completed')).toBe('Completed')
  })
})

describe('calendarCells', () => {
  // September 2026 starts on a Tuesday and has 30 days.
  it('pads the leading blanks and then counts the month out', () => {
    const cells = calendarCells(2026, 8)
    expect(cells.slice(0, 2)).toEqual([null, null])
    expect(cells[2]).toBe(1)
    expect(cells).toHaveLength(32)
    expect(cells[cells.length - 1]).toBe(30)
  })

  it('handles a leap February', () => {
    const cells = calendarCells(2028, 1)
    expect(cells.filter((c) => c !== null)).toHaveLength(29)
  })
})

describe('eventsOnDay', () => {
  const events = [
    { id: 'a', start_datetime: '2026-09-22T00:05:00' },
    { id: 'b', start_datetime: '2026-09-22T23:50:00' },
    { id: 'c', start_datetime: '2026-09-23T00:10:00' },
  ]

  it('takes both ends of the day, not just the middle of it', () => {
    expect(eventsOnDay(events, 2026, 8, 22).map((e) => e.id)).toEqual(['a', 'b'])
  })

  it('does not leak the next day in', () => {
    expect(eventsOnDay(events, 2026, 8, 23).map((e) => e.id)).toEqual(['c'])
  })

  it('returns nothing for a day with no events', () => {
    expect(eventsOnDay(events, 2026, 8, 24)).toEqual([])
  })
})
