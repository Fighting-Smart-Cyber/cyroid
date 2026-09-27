import { describe, it, expect } from 'vitest'
import {
  describeFailure,
  dockerFailure,
  logPaneState,
  logsNotice,
  panelFrom,
  updateCheckState,
} from './InfrastructureTab'
import type { PlatformUpdateCheck, ServiceLogsResponse } from '../../services/api'

/**
 * Axios recognises its own errors by the `isAxiosError` flag alone, so a literal is a faithful
 * stand-in for one -- no network, no adapter.
 */
const axiosError = (status: number, detail?: string) => ({
  isAxiosError: true,
  message: `Request failed with status code ${status}`,
  response: { status, data: detail === undefined ? {} : { detail } },
})

const noAnswer = { isAxiosError: true, message: 'Network Error', response: undefined }

const logs = (entries: number, filters: Record<string, unknown> = {}): ServiceLogsResponse => ({
  service: 'worker',
  logs: Array.from({ length: entries }, (_, i) => ({ message: `line ${i}`, raw: `line ${i}` })),
  total_lines: entries,
  has_more: false,
  filters_applied: filters,
})

describe('panelFrom', () => {
  it('keeps the panel that answered when a sibling rejects', async () => {
    // ADMIN-045: under Promise.all the one endpoint that works on Kubernetes was discarded with
    // the four that do not.
    const [failed, ok] = await Promise.allSettled([
      Promise.reject(axiosError(500)),
      Promise.resolve({ data: { cpu: 12 } }),
    ])

    expect(panelFrom(ok, 'Resource metrics').data).toEqual({ cpu: 12 })
    expect(panelFrom(ok, 'Resource metrics').failure).toBeNull()
    expect(panelFrom(failed, 'Service health').data).toBeNull()
    expect(panelFrom(failed, 'Service health').failure).toEqual({
      kind: 'error',
      message: 'Service health could not be loaded (HTTP 500).',
      unexplained: true,
    })
  })
})

describe('describeFailure', () => {
  it('prefers the server detail to the axios message', () => {
    expect(describeFailure(axiosError(500, 'The Docker daemon is not reachable'), 'X')).toEqual({
      kind: 'error',
      message: 'The Docker daemon is not reachable',
    })
  })

  it('treats a 501 as a statement about the install, not a fault', () => {
    const detail =
      'This install runs on the Kubernetes substrate. GET /api/v1/admin/infrastructure/docker ' +
      'belongs to the Docker substrate and is not available here.'
    expect(describeFailure(axiosError(501, detail), 'The Docker overview')).toEqual({
      kind: 'substrate',
      message: detail,
    })
  })

  it('names the substrate itself when a 501 carries no detail', () => {
    expect(describeFailure(axiosError(501), 'The Docker overview')).toEqual({
      kind: 'substrate',
      message: 'The Docker overview is not available on this install.',
    })
  })

  it('says the API did not answer rather than quoting a status it never got', () => {
    expect(describeFailure(noAnswer, 'Resource metrics')).toEqual({
      kind: 'error',
      message: 'Resource metrics could not be loaded: the API did not answer.',
    })
  })

  it('still says something useful about a failure that is not an axios error', () => {
    expect(describeFailure(new Error('boom'), 'Resource metrics')).toEqual({
      kind: 'error',
      message: 'Resource metrics could not be loaded.',
    })
  })
})

describe('dockerFailure', () => {
  it('restates a bare failure as the substrate fact once Docker is known to be absent', () => {
    const failure = describeFailure(axiosError(500), 'The Docker overview')
    expect(dockerFailure(failure, 'The Docker overview', false, 'Kubernetes')).toEqual({
      kind: 'substrate',
      message: 'The Docker overview reads the Docker daemon, which this Kubernetes install does not have.',
    })
  })

  it('leaves the failure alone while the substrate is still unknown', () => {
    // features is null until /system/capabilities answers. Claiming a substrate we have not been
    // told about is the same class of mistake as claiming there are no logs.
    const failure = describeFailure(axiosError(500), 'The Docker overview')
    expect(dockerFailure(failure, 'The Docker overview', null, null)).toBe(failure)
  })

  it('leaves the failure alone on a Docker install, where it is a real fault', () => {
    const failure = describeFailure(axiosError(500), 'The Docker overview')
    expect(dockerFailure(failure, 'The Docker overview', true, 'Docker')).toBe(failure)
  })

  it('does not blame the substrate when the API never answered', () => {
    // An operator clicking Refresh while the API restarts -- which the update card on this very
    // tab causes -- would otherwise be told four panels are missing because there is no Docker.
    const failure = describeFailure(noAnswer, 'Service health')
    expect(dockerFailure(failure, 'Service health', false, 'Kubernetes')).toBe(failure)
  })

  it('keeps the server own reason rather than replacing it with a substrate claim', () => {
    const failure = describeFailure(axiosError(500, 'relation "ranges" does not exist'), 'X')
    expect(dockerFailure(failure, 'X', false, 'Kubernetes')).toBe(failure)
  })

  it('keeps the server 501 wording, which already names the route and the substrate', () => {
    const failure = describeFailure(axiosError(501, 'Kubernetes install; this route is Docker.'), 'X')
    expect(dockerFailure(failure, 'X', false, 'Kubernetes')).toBe(failure)
  })

  it('passes a success through untouched', () => {
    expect(dockerFailure(null, 'X', false, 'Kubernetes')).toBeNull()
  })
})

describe('logPaneState', () => {
  it('reports a failed read as a failure, never as an empty log', () => {
    // ADMIN-046: the pane swallowed the error and fell through to "No logs found", so an admin
    // read a failed request as proof the service had printed nothing.
    const failure = describeFailure(axiosError(500, 'Docker daemon unavailable'), 'The log viewer')
    expect(logPaneState({ loading: false, failure, logs: null })).toEqual({ kind: 'failed', failure })
  })

  it('prefers the failure even when a stale response is still in hand', () => {
    const failure = describeFailure(axiosError(500), 'The log viewer')
    expect(logPaneState({ loading: false, failure, logs: logs(3) }).kind).toBe('failed')
  })

  it('says there are no logs only when the server returned a list', () => {
    expect(logPaneState({ loading: false, failure: null, logs: logs(0) })).toEqual({ kind: 'empty' })
  })

  it('renders entries when there are entries', () => {
    const state = logPaneState({ loading: false, failure: null, logs: logs(2) })
    expect(state.kind).toBe('entries')
    expect(state.kind === 'entries' && state.entries).toHaveLength(2)
  })

  it('claims nothing before the first answer', () => {
    expect(logPaneState({ loading: false, failure: null, logs: null })).toEqual({ kind: 'idle' })
    expect(logPaneState({ loading: true, failure: null, logs: null })).toEqual({ kind: 'loading' })
  })
})

describe('logsNotice', () => {
  it('surfaces a 200 that reports it could not find the container', () => {
    expect(logsNotice(logs(0, { error: 'Container not found' }))).toEqual({
      kind: 'error',
      message: 'These logs could not be read: Container not found',
    })
  })

  it('leaves a genuinely empty log alone', () => {
    expect(logsNotice(logs(0, { level: 'error' }))).toBeNull()
    expect(logsNotice(logs(0))).toBeNull()
  })
})

const updateCheck = (over: Partial<PlatformUpdateCheck> = {}): PlatformUpdateCheck => ({
  checked: true,
  update_available: false,
  behind: null,
  branch: null,
  current_sha: null,
  current_version: '0.53.0',
  latest_tag: '0.53.0',
  detail: null,
  ...over,
})

describe('updateCheckState', () => {
  it('does not call a host that is not on a release "up to date"', () => {
    // pg-devtest, 2026-09-25: the panel read "Up to date (vfea4a743)" with the button greyed out,
    // while the endpoint was saying the version is not a release and naming the one to move to.
    // The sentence was dropped because it only rendered for checked=false.
    const state = updateCheckState(
      updateCheck({
        current_version: 'fea4a743',
        detail:
          "This install reports version 'fea4a743', which is not a release. " +
          'The newest release is 0.53.0; move it there deliberately.',
      }),
      false
    )
    expect(state.kind).toBe('nothing-to-pull')
    expect(state.kind === 'nothing-to-pull' && state.detail).toContain('0.53.0')
  })

  it('does not call a remote with no release tags "up to date" either', () => {
    const state = updateCheckState(
      updateCheck({ latest_tag: null, detail: 'No vX.Y.Z release tags on the remote yet.' }),
      false
    )
    expect(state.kind).toBe('nothing-to-pull')
  })

  it('is up to date only when the endpoint had nothing to add', () => {
    expect(updateCheckState(updateCheck(), false)).toEqual({ kind: 'current' })
  })

  it('keeps "could not look" distinct from both', () => {
    expect(
      updateCheckState(updateCheck({ checked: false, detail: 'Could not reach the code remote' }), false)
    ).toEqual({ kind: 'failed', detail: 'Could not reach the code remote' })
  })

  it('reports an available update whatever else the answer carries', () => {
    const state = updateCheckState(updateCheck({ update_available: true, detail: 'aside' }), false)
    expect(state.kind).toBe('available')
  })

  it('shows the in-flight check, and nothing before the first answer', () => {
    expect(updateCheckState(null, true)).toEqual({ kind: 'checking' })
    expect(updateCheckState(updateCheck(), true)).toEqual({ kind: 'checking' })
    expect(updateCheckState(null, false)).toEqual({ kind: 'unknown' })
  })
})
