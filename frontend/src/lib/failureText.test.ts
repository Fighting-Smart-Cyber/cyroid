/// <reference types="vite/client" />
// frontend/src/lib/failureText.test.ts
import { describe, it, expect } from 'vitest'
import { AxiosError, AxiosHeaders } from 'axios'
import { actionFailureMessage, failureReasonForStatus, recordedFailureReason } from './failureText'
// See tabs.test.ts for why these pages are read as source rather than rendered.
import admin from '../pages/Admin.tsx?raw'
import ranges from '../pages/Ranges.tsx?raw'
import rangeDetail from '../pages/RangeDetail.tsx?raw'

/** An axios rejection carrying `detail`, the way a FastAPI refusal reaches a catch block. */
function refusal(detail: unknown, status = 400): AxiosError {
  const err = new AxiosError('Request failed', 'ERR_BAD_REQUEST')
  err.response = {
    data: { detail },
    status,
    statusText: '',
    headers: new AxiosHeaders(),
    config: { headers: new AxiosHeaders() },
  }
  return err
}

describe('actionFailureMessage', () => {
  it('uses a plain string detail', () => {
    expect(actionFailureMessage(refusal('Range is already running'), 'Failed')).toBe(
      'Range is already running'
    )
  })

  // The whole point of a deploy refusal is the list of things to go and fix.
  it('keeps the errors and the hint out of a structured refusal', () => {
    const message = actionFailureMessage(
      refusal({
        message: 'Range cannot be deployed',
        errors: ['web-01 has no network', 'db-01 has no image'],
        hint: 'Add a network before deploying',
      }),
      'Failed to deploy range'
    )
    expect(message).toContain('Range cannot be deployed')
    expect(message).toContain('web-01 has no network')
    expect(message).toContain('db-01 has no image')
    expect(message).toContain('Add a network before deploying')
  })

  it('falls back when a structured refusal carries only a list', () => {
    expect(
      actionFailureMessage(refusal({ errors: ['name is taken'] }), 'Failed to create range')
    ).toContain('Failed to create range')
  })

  // A schema refusal is a list of objects. Handed to a toast unflattened it renders as
  // "[object Object]", or takes the panel down as an invalid React child.
  it('flattens a schema refusal into readable lines', () => {
    const message = actionFailureMessage(
      refusal([{ loc: ['body', 'subnet'], msg: 'value is not a valid IPv4 network' }], 422),
      'Failed to create network'
    )
    expect(message).toContain('subnet')
    expect(message).toContain('value is not a valid IPv4 network')
    expect(message).not.toContain('object Object')
  })

  it('falls back when the response says nothing useful', () => {
    expect(actionFailureMessage(refusal(undefined, 500), 'Failed to start range')).toBe(
      'Failed to start range'
    )
    expect(actionFailureMessage(refusal({}, 500), 'Failed to start range')).toBe(
      'Failed to start range'
    )
  })

  // Anything that is not an HTTP answer names our own internals and cannot be acted on.
  it('never puts a raw exception in front of the user', () => {
    expect(actionFailureMessage(new TypeError('vms is not iterable'), 'Failed to delete VM')).toBe(
      'Failed to delete VM'
    )
    expect(actionFailureMessage('boom', 'Failed to delete VM')).toBe('Failed to delete VM')
    expect(actionFailureMessage(null, 'Failed to delete VM')).toBe('Failed to delete VM')
  })
})

describe('recordedFailureReason', () => {
  it('returns the stored reason', () => {
    expect(recordedFailureReason('namespace pg-range-1 was not created')).toBe(
      'namespace pg-range-1 was not created'
    )
  })

  // The line breaks belong to the caller's CSS: the list row lets them collapse, the range page
  // keeps them. Rewriting the message here would take that choice away from both.
  it('keeps the shape of a reason that spans lines', () => {
    expect(recordedFailureReason('deploy failed:\n  image pull backoff\n')).toBe(
      'deploy failed:\n  image pull backoff'
    )
  })

  it('reports nothing rather than an empty line', () => {
    expect(recordedFailureReason('')).toBeNull()
    expect(recordedFailureReason('   \n  ')).toBeNull()
    expect(recordedFailureReason(null)).toBeNull()
    expect(recordedFailureReason(undefined)).toBeNull()
  })
})

describe('failureReasonForStatus', () => {
  it('shows the reason on a record that is still failed', () => {
    expect(failureReasonForStatus('error', 'image pull backoff')).toBe('image pull backoff')
    expect(failureReasonForStatus('ERROR', 'image pull backoff')).toBe('image pull backoff')
    expect(failureReasonForStatus('failed', 'image pull backoff')).toBe('image pull backoff')
  })

  // A Docker teardown sets a failed range to Draft and leaves the reason on the row; the DinD
  // recovery endpoint flips one to Running the same way. Printed under those badges the reason
  // contradicts them, and nothing on screen says which of the two is stale.
  it('says nothing once the record has left its failed state', () => {
    for (const status of ['draft', 'deploying', 'running', 'stopped', 'archived']) {
      expect(failureReasonForStatus(status, 'deploy failed: image pull backoff')).toBeNull()
    }
  })

  it('still says nothing when a failed record recorded no reason', () => {
    expect(failureReasonForStatus('error', '')).toBeNull()
    expect(failureReasonForStatus('error', '  ')).toBeNull()
    expect(failureReasonForStatus('error', null)).toBeNull()
  })

  it('says nothing when there is no status to judge it against', () => {
    expect(failureReasonForStatus(null, 'image pull backoff')).toBeNull()
    expect(failureReasonForStatus(undefined, 'image pull backoff')).toBeNull()
  })
})

describe.each([
  ['the Admin page', admin],
  ['the ranges list', ranges],
  ['the range page', rangeDetail],
])('%s reports failures through the shared helper', (_name, page) => {
  // Reaching into the response at the call site is what put an array in front of React and a
  // `{message, errors, hint}` object in front of a toast that printed "[object Object]".
  it('never reads the response body itself', () => {
    expect(page).not.toMatch(/response\?\.data\?\.detail/)
  })

  it('never types a caught error as any', () => {
    expect(page).not.toMatch(/catch\s*\(\s*\w+\s*:\s*any\s*\)/)
    expect(page).not.toMatch(/\(\s*\w+\s*:\s*any\s*\)\s*=>\s*\{\s*\n?\s*toast\.error/)
  })
})

describe('a failed range says why', () => {
  // The status badge said "error" and the reason travelled in the same payload unread.
  it('renders the reason in the list', () => {
    expect(ranges).toContain('<FailureReason status={range.status} message={range.error_message} />')
  })

  it('renders the reason on the range itself', () => {
    expect(rangeDetail).toContain('failureReasonForStatus(range.status, range.error_message)')
    expect(rangeDetail).toContain('{rangeFailureReason}')
  })

  // Both pages have to read the reason against the status. Reading it on its own is what puts a
  // red failure paragraph on a Draft range once a Docker teardown has left the field behind.
  it('reads the reason against the status, on both pages', () => {
    for (const page of [ranges, rangeDetail]) {
      expect(page).toContain('failureReasonForStatus')
      expect(page).not.toMatch(/recordedFailureReason\(\s*range\.error_message/)
    }
  })

  // "No ranges" and "Range not found" are claims about the install and about the range. After a
  // refused request neither page is in a position to make one.
  it('does not answer a failed load with an empty state', () => {
    for (const page of [ranges, rangeDetail]) {
      expect(page).toContain('setLoadError(')
    }
  })
})
