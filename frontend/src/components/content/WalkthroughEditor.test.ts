import { describe, it, expect } from 'vitest'
import { countLabel, editableGuide, machinesUsedIn, uniqueId } from './WalkthroughEditor'
import type { Walkthrough } from '../../types'

const guide = (...machines: (string | undefined)[]): Walkthrough => ({
  title: 'Guide',
  phases: [
    {
      id: 'phase1',
      name: 'Phase 1',
      steps: machines.map((vm, i) => ({
        id: `step1_${i + 1}`,
        title: `Step ${i + 1}`,
        content: '',
        vm,
      })),
    },
  ],
})

describe('machinesUsedIn', () => {
  it('lists each machine once, in the order it was first used', () => {
    expect(machinesUsedIn(guide('web-01', 'db-01', 'web-01'))).toEqual(['web-01', 'db-01'])
  })

  it('ignores steps with no machine and whitespace-only ones', () => {
    expect(machinesUsedIn(guide(undefined, '', '  ', 'web-01'))).toEqual(['web-01'])
  })
})

describe('uniqueId', () => {
  it('leaves a free id alone', () => {
    expect(uniqueId('phase2', new Set(['phase1']))).toBe('phase2')
  })

  // Deleting the first of two phases and adding another produced the survivor's id again, which
  // marked the wrong step complete in the learner's progress record.
  it('suffixes an id the walkthrough already uses', () => {
    expect(uniqueId('phase2', new Set(['phase2']))).toBe('phase2_2')
    expect(uniqueId('phase2', new Set(['phase2', 'phase2_2']))).toBe('phase2_3')
  })
})

describe('editableGuide', () => {
  // The YAML tab applies any parse that is an object, so a document the author is halfway
  // through retyping reaches the editor with no phases at all.
  it('gives a guide that lost its phases an empty list rather than nothing', () => {
    expect(editableGuide({ title: 'Half typed' } as Walkthrough)).toEqual({
      title: 'Half typed',
      phases: [],
    })
  })

  it('keeps a real guide as it was, including keys it does not read', () => {
    const real = { title: 'Day 1', phases: [], version: 2 } as unknown as Walkthrough
    expect(editableGuide(real)).toEqual(real)
  })

  // An absent title on a controlled input is React's uncontrolled-to-controlled warning, and a
  // non-string one would render as nothing with no sign of what happened.
  it('replaces a missing or non-string title with an empty one', () => {
    expect(editableGuide({ phases: [] } as unknown as Walkthrough).title).toBe('')
    expect(editableGuide({ title: 7, phases: [] } as unknown as Walkthrough).title).toBe('')
  })

  it('handles no guide at all', () => {
    expect(editableGuide(null)).toEqual({ title: '', phases: [] })
  })
})

describe('countLabel', () => {
  it('does not say "1 phases"', () => {
    expect(countLabel(1, 'phase')).toBe('1 phase')
    expect(countLabel(0, 'step')).toBe('0 steps')
    expect(countLabel(3, 'step')).toBe('3 steps')
  })
})
