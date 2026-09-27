import { describe, it, expect } from 'vitest'

import { age, contextLines } from './Feedback'
import type { Feedback } from '../services/api'

const report = (over: Partial<Feedback> = {}): Feedback =>
  ({
    id: 'f1',
    kind: 'bug',
    status: 'open',
    title: 'The console went black',
    description: null,
    source: 'app',
    source_ref: null,
    range_id: null,
    content_id: null,
    context: {},
    author_id: 'u1',
    created_at: '2026-09-26T00:00:00Z',
    ...over,
  }) as Feedback

describe('age', () => {
  const now = new Date('2026-09-26T12:00:00Z').getTime()

  it('does not put a number on something that just happened', () => {
    expect(age('2026-09-26T11:59:30Z', now)).toBe('just now')
  })

  it('counts minutes, then hours, then days — coarsest unit that is still true', () => {
    expect(age('2026-09-26T11:30:00Z', now)).toBe('30m ago')
    expect(age('2026-09-26T08:00:00Z', now)).toBe('4h ago')
    expect(age('2026-09-22T12:00:00Z', now)).toBe('4d ago')
  })

  it('never reports a negative age for a clock that is slightly ahead', () => {
    expect(age('2026-09-26T12:00:30Z', now)).toBe('just now')
  })
})

describe('contextLines', () => {
  it('reads the guide and the step as one line', () => {
    const lines = contextLines(
      report({ context: { content_title: 'Getting Started', step: 'Step 3' } })
    )
    expect(lines).toContain('Guide: Getting Started — Step 3')
  })

  it('prefers the range NAME, which is what the dialog now captures', () => {
    // The submit dialog looks the name up so this is not a UUID; before that it always fell
    // through to the raw route, which is the least useful thing to show a human.
    const lines = contextLines(report({ context: { range_name: 'gateway-check-0531' } }))
    expect(lines).toContain('Range: gateway-check-0531')
  })

  it('surfaces where in the source it happened', () => {
    expect(contextLines(report({ source_ref: 'intro/step-2' }))).toContain('At: intro/step-2')
  })

  it('says nothing rather than rendering empty labels', () => {
    expect(contextLines(report())).toEqual([])
  })

  it('tolerates a report whose context key we never whitelisted', () => {
    // The server whitelists on write, but a row written by an older build is still a row.
    const lines = contextLines(report({ context: { nonsense: 'x' } as never }))
    expect(lines).toEqual([])
  })
})
