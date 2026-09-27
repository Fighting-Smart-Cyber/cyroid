import { describe, it, expect } from 'vitest'
import { settlesThePanel } from './DeploymentProgress'

/**
 * The panel is mounted on a status the range page sets optimistically, before the request that
 * starts the work has been answered. So the first poll routinely lands while the server still
 * reports the status the operation is starting *from* -- draft or stopped for a deploy, stopped
 * for a start. Acting on that tears the panel down in the second it appeared and offers Deploy
 * again on a range that is by then DEPLOYING, which refuses the second click.
 *
 * What is under test is the one decision that separates "the work has not started yet" from
 * "the work is over", both of which reach this panel as a settled-looking status.
 */
describe('settlesThePanel', () => {
  it('does not settle on the status a deploy is starting from', () => {
    expect(settlesThePanel('draft', false)).toBe(false)
    expect(settlesThePanel('stopped', false)).toBe(false)
  })

  it('settles on those once the server has confirmed the work is in flight', () => {
    expect(settlesThePanel('stopped', true)).toBe(true)
    expect(settlesThePanel('draft', true)).toBe(true)
  })

  it('settles on running whether or not a poll has seen deploying', () => {
    // A deploy is only offered from draft, stopped or error and a start only from stopped or
    // draft, so 'running' cannot be the status either of them began in -- it can only be the
    // one they reached. A fast operation that finished before the first poll still settles.
    expect(settlesThePanel('running', false)).toBe(true)
    expect(settlesThePanel('running', true)).toBe(true)
  })

  it('keeps polling while the work is running or has failed', () => {
    // 'error' is not settled here: the caller renders the reason instead of unmounting.
    expect(settlesThePanel('deploying', true)).toBe(false)
    expect(settlesThePanel('error', true)).toBe(false)
  })
})
