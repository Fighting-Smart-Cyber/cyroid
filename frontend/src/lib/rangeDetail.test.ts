/// <reference types="vite/client" />
import { describe, it, expect } from 'vitest'
import { detailToMessage, needsResourceSync } from './rangeDetail'
// `?raw` rather than node:fs: there is no @types/node here, and importing the page itself pulls
// in the terminal emulator and the auth store, neither of which loads outside a browser.
import page from '../pages/RangeDetail.tsx?raw'

/**
 * The range page told a Kubernetes user two contradictory things at once: a Workloads panel
 * listing a running machine, and beneath it "No networks configured" with a live Add button whose
 * row existed nowhere on the cluster.
 *
 * The two helpers are covered directly. The rest of that fix is which JSX renders, and there is
 * no DOM test environment here, so those gates are asserted against the page's source the way the
 * backend's authorization and naming guards are. A textual guard is weaker than a rendered one,
 * but it does fail when someone removes a gate, which is the regression worth catching.
 */

describe('detailToMessage', () => {
  it('uses the reason the server gave', () => {
    expect(detailToMessage('Range is missing DinD configuration', 'unused')).toBe(
      'Range is missing DinD configuration'
    )
  })

  it('keeps every part of a structured validation refusal', () => {
    const message = detailToMessage(
      {
        message: 'Deployment validation failed',
        errors: ['Network "lan" has no subnet', 'VM "web" has no image'],
        hint: 'Add a network first',
      },
      'Failed to deploy range'
    )
    expect(message).toContain('Deployment validation failed')
    expect(message).toContain('Network "lan" has no subnet')
    expect(message).toContain('VM "web" has no image')
    expect(message).toContain('Add a network first')
  })

  it('stays on one line, because the toast paragraph collapses newlines', () => {
    const message = detailToMessage(
      { message: 'Sync validation failed', errors: ['a', 'b'], hint: 'c' },
      'unused'
    )
    expect(message).not.toContain('\n')
  })

  it('falls back when the server said nothing legible', () => {
    expect(detailToMessage(undefined, 'Failed to sync range')).toBe('Failed to sync range')
    expect(detailToMessage('', 'Failed to sync range')).toBe('Failed to sync range')
    // A FastAPI request-validation error is a list of objects, which is not something to show.
    expect(detailToMessage([{ loc: ['body'], msg: 'field required' }], 'Failed to deploy')).toBe(
      'Failed to deploy'
    )
  })
})

describe('needsResourceSync', () => {
  it('is true where a row has not reached the host', () => {
    expect(needsResourceSync(true, [{ docker_network_id: null }], [])).toBe(true)
    expect(needsResourceSync(true, [], [{ container_id: null }])).toBe(true)
  })

  it('is false once everything is provisioned', () => {
    expect(needsResourceSync(true, [{ docker_network_id: 'net' }], [{ container_id: 'c' }])).toBe(
      false
    )
  })

  it('is false where a range is not composed from rows, whatever the rows say', () => {
    // An orphan row left by a POST that should have been refused must not put a Sync button on a
    // running Kubernetes range, where the only possible answer is "deploy the range first".
    expect(needsResourceSync(false, [{ docker_network_id: null }], [{ container_id: null }])).toBe(
      false
    )
  })
})

describe('the range page gates its Era A surfaces', () => {
  it('reports failures through the toast store, not a browser dialog', () => {
    expect(page).not.toMatch(/\balert\s*\(/)
  })

  it('does not fetch the image library alongside the range', () => {
    // Three of the six requests shared one Promise.all, so any one of them rejecting left the
    // range null -- which renders "Range not found" about a range that is running.
    const fetchData = page.slice(page.indexOf('  const fetchData = '))
    expect(fetchData.slice(0, fetchData.indexOf('\n  }'))).not.toContain('imagesApi')
  })

  it('fetches the image library only where a range is composed from rows', () => {
    const callSite = page.indexOf('imagesApi.listBaseImages')
    expect(callSite).toBeGreaterThan(-1)
    const effect = page.slice(page.lastIndexOf('useEffect(', callSite), callSite)
    expect(effect).toContain('if (!showComposition) {')
  })

  it('renders the composition sections, the blueprint writebacks and Diagnostics behind a flag', () => {
    for (const gate of [
      // Networks and Virtual Machines, and with them Add Network and Add VM.
      /\{showComposition && \(\s*<>/,
      // Update Blueprint overwrites a Kubernetes blueprint with what the Era A extractor can see
      // of a Kubernetes range, which is nothing.
      /\{showComposition && range\.blueprint_instance && \(/,
      // The DinD shell, its VNC processes and its network interfaces.
      /\{activeTab === 'diagnostics' && showDiagnostics && \(/,
    ]) {
      expect(page).toMatch(gate)
    }
  })

  it('never opens the Diagnostics body on the tab alone', () => {
    expect(page).not.toMatch(/activeTab === 'diagnostics' && \(/)
  })

  it('leaves a deploy or sync refusal on screen long enough to read', () => {
    // Both replaced a dialog the user had to dismiss, and both answer with a list of things to
    // go and fix. At the toast store's default five seconds the reason is gone before it is read.
    for (const fallback of ['Failed to deploy range', 'Failed to sync range']) {
      expect(page).toMatch(new RegExp(`'${fallback}'\\s*\\)\\s*,\\s*REFUSAL_TOAST_MS\\s*\\)`))
    }
  })
})
