import { describe, it, expect } from 'vitest'
import {
  CHANGELOG,
  compareVersions,
  releasesSince,
  userFacingReleases,
  type Release,
} from './changelog'

/**
 * These functions decide whether a modal interrupts someone. Nothing covered
 * them before — `frontend:test` ran with `--passWithNoTests`.
 *
 * The behavioural tests use FIXTURES, not the live CHANGELOG. Written against
 * the real data they passed whether or not the filtering worked: deleting every
 * `userFacing: false` left them iterating empty sets and asserting nothing. The
 * checks that do read CHANGELOG are integrity checks on the data itself, where
 * that coupling is the point.
 */

const rel = (version: string, userFacing?: boolean): Release => ({
  version,
  date: '2026-01-01',
  title: `release ${version}`,
  ...(userFacing === undefined ? {} : { userFacing }),
  highlights: [{ kind: 'fix', text: 'x' }],
})

// Newest first, mirroring CHANGELOG's own ordering.
const FIXTURE: Release[] = [
  rel('1.3.0', false), // infrastructure only
  rel('1.2.0', false), // infrastructure only
  rel('1.1.0'), // the last thing a user would care about
  rel('1.0.0'),
]

describe('compareVersions', () => {
  it('orders by number, not by string', () => {
    // "0.9.0" > "0.10.0" lexically, which is the classic way this goes wrong.
    expect(compareVersions('0.10.0', '0.9.0')).toBeGreaterThan(0)
    expect(compareVersions('0.42.3', '0.42.10')).toBeLessThan(0)
  })

  it('treats equal versions as equal', () => {
    expect(compareVersions('1.2.3', '1.2.3')).toBe(0)
  })

  it('does not throw on malformed input', () => {
    // A corrupted localStorage value must not take the app shell down.
    expect(() => compareVersions('', 'x.y.z')).not.toThrow()
    expect(compareVersions('1', '1.0.0')).toBe(0)
  })
})

describe('userFacingReleases', () => {
  it('drops releases marked not user-facing', () => {
    expect(userFacingReleases(FIXTURE).map((r) => r.version)).toEqual(['1.1.0', '1.0.0'])
  })

  it('keeps releases with the flag omitted', () => {
    expect(userFacingReleases([rel('2.0.0')])).toHaveLength(1)
  })

  it('returns nothing when every release is internal', () => {
    expect(userFacingReleases([rel('2.0.0', false)])).toHaveLength(0)
  })
})

describe('releasesSince', () => {
  it('shows nothing when the only newer releases are not user-facing', () => {
    // The case this feature exists for: shipping 1.2.0 and 1.3.0 must not
    // interrupt someone who has already seen 1.1.0.
    expect(releasesSince('1.1.0', FIXTURE)).toHaveLength(0)
  })

  it('shows a user-facing release the viewer has not seen', () => {
    expect(releasesSince('1.0.0', FIXTURE).map((r) => r.version)).toEqual(['1.1.0'])
  })

  it('shows only the latest user-facing release to a first-time viewer', () => {
    // Not the whole history, and not an internal release even though 1.3.0 is
    // newest.
    expect(releasesSince(null, FIXTURE).map((r) => r.version)).toEqual(['1.1.0'])
  })

  it('shows nothing to a viewer already on the newest user-facing version', () => {
    expect(releasesSince('1.1.0', FIXTURE)).toHaveLength(0)
  })

  it('would show the release if the flag were removed', () => {
    // Proves the previous assertions are about the flag and not a coincidence
    // of the fixture.
    const noFlags = FIXTURE.map((r) => rel(r.version))
    expect(releasesSince('1.1.0', noFlags).map((r) => r.version)).toEqual(['1.3.0', '1.2.0'])
  })
})

describe('the CHANGELOG itself', () => {
  it('is ordered newest first', () => {
    // releasesSince and the modal both slice from the front.
    for (let i = 1; i < CHANGELOG.length; i++) {
      expect(compareVersions(CHANGELOG[i - 1].version, CHANGELOG[i].version)).toBeGreaterThan(0)
    }
  })

  it('gives every release a title, a date and at least one highlight', () => {
    for (const r of CHANGELOG) {
      expect(r.title.trim().length, `${r.version} has no title`).toBeGreaterThan(0)
      expect(r.date, `${r.version} has a malformed date`).toMatch(/^\d{4}-\d{2}-\d{2}$/)
      expect(r.highlights.length, `${r.version} has no highlights`).toBeGreaterThan(0)
    }
  })

  it('uses only kinds the modal can render', () => {
    // An unknown kind indexes KIND_META to undefined and crashes the modal.
    const known = ['feature', 'improvement', 'fix']
    for (const r of CHANGELOG) {
      for (const h of r.highlights) {
        expect(known, `${r.version} uses kind "${h.kind}"`).toContain(h.kind)
      }
    }
  })

  it('has no duplicate versions', () => {
    const versions = CHANGELOG.map((r) => r.version)
    expect(new Set(versions).size).toBe(versions.length)
  })

  it('still has something to show a user', () => {
    // If every release were marked internal the modal would never open again,
    // which is a mistake rather than a decision.
    expect(userFacingReleases().length).toBeGreaterThan(0)
  })
})
