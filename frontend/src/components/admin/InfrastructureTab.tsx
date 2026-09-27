// frontend/src/components/admin/InfrastructureTab.tsx
import { useState, useEffect, useCallback, useRef } from 'react'
import {
  RefreshCw,
  Server,
  Activity,
  Database,
  HardDrive,
  FileText,
  Settings,
  ChevronDown,
  ChevronUp,
  Loader2,
  CheckCircle,
  XCircle,
  AlertCircle,
  Clock,
  Cpu,
  MemoryStick,
  Container,
  Network,
  Box,
  Search,
  Download,
  Info,
} from 'lucide-react'
import clsx from 'clsx'
import { isAxiosError } from 'axios'
import type {
  PlatformUpdateStatus,
  PlatformUpdateCheck,
  GitCredentialStatus,
  LogEntry,
} from '../../services/api'
import {
  infrastructureApi,
  InfrastructureServicesResponse,
  ServiceLogsResponse,
  DockerOverviewResponse,
  InfrastructureMetricsResponse,
  SystemInfoResponse,
  RangeDebugResponse,
} from '../../services/api'
import { useCapabilitiesStore } from '../../stores/capabilitiesStore'
import { BRANDING } from '../../lib/branding'

/**
 * Why a panel has nothing to show.
 *
 * `substrate` is the distinction this tab was getting wrong. Four of its five endpoints read the
 * Docker daemon, so on a Kubernetes install they refuse by design: that is a fact about the
 * install, not a fault, and it must not be dressed as one or invite a retry.
 */
export type PanelFailure = {
  kind: 'substrate' | 'error'
  message: string
  /**
   * The server answered and gave no reason of its own.
   *
   * Only such a failure may be restated as a fact about the substrate. One carrying the server's
   * own words, and one where the API never answered at all, each know something this page does
   * not: an operator who clicks Refresh while the API is restarting must not be told that four
   * panels are missing because there is no Docker here.
   */
  unexplained?: true
}

/** A panel holds its data or the reason it has none -- never both, never neither in silence. */
export type Panel<T> = { data: T | null; failure: PanelFailure | null }

/** The `detail` FastAPI puts on a refusal, when it carries one. */
function responseDetail(reason: unknown): string | null {
  if (!isAxiosError(reason)) return null
  const body = reason.response?.data as { detail?: unknown } | undefined
  return typeof body?.detail === 'string' && body.detail.trim() !== '' ? body.detail : null
}

/**
 * What to tell an operator about one failed panel.
 *
 * The server's own `detail` is preferred because it is the only text that knows which route
 * failed and why -- the substrate guard names the substrate in its 501. Axios contributes
 * "Request failed with status code 500", which used to be the whole of what this page said.
 */
export function describeFailure(reason: unknown, label: string): PanelFailure {
  const detail = responseDetail(reason)
  const status = isAxiosError(reason) ? reason.response?.status : undefined

  if (status === 501) {
    return { kind: 'substrate', message: detail ?? `${label} is not available on this install.` }
  }
  if (detail) return { kind: 'error', message: detail }
  if (isAxiosError(reason) && !reason.response) {
    return { kind: 'error', message: `${label} could not be loaded: the API did not answer.` }
  }
  if (status) {
    return {
      kind: 'error',
      message: `${label} could not be loaded (HTTP ${status}).`,
      unexplained: true,
    }
  }
  return { kind: 'error', message: `${label} could not be loaded.` }
}

/**
 * Restates a Docker-only surface's failure as the fact it is on an install with no Docker.
 *
 * Where the backend refuses with a 501 it names the substrate itself and that text stands; this
 * covers a route that fails with a bare status and nothing else, so an operator on Kubernetes
 * reads one story rather than the truth mixed with "HTTP 500".
 *
 * Only an `unexplained` failure is restated. A reachable API that explains itself, and an API that
 * never answered, are both saying more than this page knows, and a network failure during the
 * restart the update card above causes would otherwise be reported as a missing Docker daemon.
 *
 * `hasDocker` is null until the capabilities endpoint answers. Not knowing is not the same as
 * knowing there is no Docker, and naming the wrong substrate is the mistake being fixed here.
 */
export function dockerFailure(
  failure: PanelFailure | null,
  label: string,
  hasDocker: boolean | null,
  substrateLabel: string | null
): PanelFailure | null {
  if (!failure || !failure.unexplained || hasDocker !== false) return failure
  const install = substrateLabel ? `this ${substrateLabel} install` : 'this install'
  return {
    kind: 'substrate',
    message: `${label} reads the Docker daemon, which ${install} does not have.`,
  }
}

/** One settled request becomes one panel, so a sibling's rejection cannot discard its data. */
export function panelFrom<T>(result: PromiseSettledResult<{ data: T }>, label: string): Panel<T> {
  if (result.status === 'fulfilled') return { data: result.value.data, failure: null }
  return { data: null, failure: describeFailure(result.reason, label) }
}

/**
 * What each panel is called when it has to explain itself. One place, because the fetch names the
 * panel in a failure and the render names it again when it restates one as a substrate fact.
 */
const PANEL = {
  services: 'Service health',
  docker: 'The Docker overview',
  metrics: 'Resource metrics',
  system: 'System information',
  ranges: 'Range debug info',
  logs: 'The log viewer',
} as const

/**
 * A 200 carrying no logs is not always an empty log. The logs endpoint answers with an empty list
 * and an `error` in `filters_applied` when it cannot find the container at all, which the pane
 * rendered as "No logs found" -- an admin reading that concluded the worker had printed nothing.
 */
export function logsNotice(data: ServiceLogsResponse): PanelFailure | null {
  const reported = data.filters_applied?.error
  if (typeof reported === 'string' && reported.trim() !== '') {
    return { kind: 'error', message: `These logs could not be read: ${reported}` }
  }
  return null
}

export type LogPane =
  | { kind: 'loading' }
  | { kind: 'failed'; failure: PanelFailure }
  | { kind: 'entries'; entries: LogEntry[] }
  | { kind: 'empty' }
  | { kind: 'idle' }

/**
 * What the log pane is entitled to say. A failed read and an empty log are different facts and
 * the pane reported the second for both; only a request that came back with a list may claim
 * there are no logs.
 */
export function logPaneState(input: {
  loading: boolean
  failure: PanelFailure | null
  logs: ServiceLogsResponse | null
}): LogPane {
  if (input.loading) return { kind: 'loading' }
  if (input.failure) return { kind: 'failed', failure: input.failure }
  if (!input.logs) return { kind: 'idle' }
  if (input.logs.logs.length > 0) return { kind: 'entries', entries: input.logs.logs }
  return { kind: 'empty' }
}

export type UpdateState =
  | { kind: 'unknown' }
  | { kind: 'checking' }
  | { kind: 'failed'; detail: string | null }
  | { kind: 'nothing-to-pull'; detail: string }
  | { kind: 'current' }
  | { kind: 'available' }

/**
 * What the update line is entitled to say. Four distinct facts, and the panel rendered three of
 * them as two: a check that RAN, found nothing to pull, and still carried a `detail` was drawn as
 * a green "Up to date" with the sentence thrown away.
 *
 * The backend sets `detail` on a checked=true answer for exactly one purpose -- to say WHICH
 * "nothing to pull" this is. Its own comment: "a host pointed at a remote with no tags reports a
 * reassuring 'up to date' forever". The other case is a host whose version is not a release at
 * all, where the button has nowhere to go and that sentence is the only place an operator is told
 * what to do instead.
 *
 * Seen on pg-devtest 2026-09-25: a source install at `fea4a743` read "Up to date (vfea4a743)"
 * with the update button greyed out, while the endpoint was saying "This install reports version
 * 'fea4a743', which is not a release. The newest release is 0.53.0; move it there deliberately."
 *
 * `failed` keeps a kind of its own because "I could not look" must never render as either.
 */
export function updateCheckState(
  check: PlatformUpdateCheck | null,
  checking: boolean
): UpdateState {
  if (checking) return { kind: 'checking' }
  if (!check) return { kind: 'unknown' }
  if (!check.checked) return { kind: 'failed', detail: check.detail }
  if (check.update_available) return { kind: 'available' }
  if (check.detail) return { kind: 'nothing-to-pull', detail: check.detail }
  return { kind: 'current' }
}

/** A panel's own failure, in its own box, so everything that answered still renders. */
function PanelNotice({ failure }: { failure: PanelFailure }) {
  const expected = failure.kind === 'substrate'
  const Icon = expected ? Info : AlertCircle
  return (
    <div
      className={clsx(
        'flex items-start gap-2 rounded-md border p-3 text-sm',
        expected
          ? 'border-gray-200 bg-gray-50 text-gray-600'
          : 'border-red-200 bg-red-50 text-red-700'
      )}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0" />
      <span>{failure.message}</span>
    </div>
  )
}

// Status badge component
function StatusBadge({ status }: { status: string }) {
  const config = {
    healthy: { color: 'bg-green-100 text-green-800', icon: CheckCircle },
    unhealthy: { color: 'bg-red-100 text-red-800', icon: XCircle },
    degraded: { color: 'bg-yellow-100 text-yellow-800', icon: AlertCircle },
    unknown: { color: 'bg-gray-100 text-gray-800', icon: AlertCircle },
    running: { color: 'bg-green-100 text-green-800', icon: CheckCircle },
    exited: { color: 'bg-red-100 text-red-800', icon: XCircle },
    stopped: { color: 'bg-gray-100 text-gray-800', icon: XCircle },
  }[status] || { color: 'bg-gray-100 text-gray-800', icon: AlertCircle }

  const Icon = config.icon

  return (
    <span className={clsx('inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium', config.color)}>
      <Icon className="h-3 w-3" />
      {status}
    </span>
  )
}

// Progress bar component
function ProgressBar({ value, max = 100, color = 'blue' }: { value: number; max?: number; color?: string }) {
  const percent = Math.min((value / max) * 100, 100)
  const colorClass = {
    blue: 'bg-blue-500',
    green: 'bg-green-500',
    yellow: 'bg-yellow-500',
    red: 'bg-red-500',
  }[color] || 'bg-blue-500'

  return (
    <div className="w-full bg-gray-200 rounded-full h-2">
      <div className={clsx('h-2 rounded-full transition-all', colorClass)} style={{ width: `${percent}%` }} />
    </div>
  )
}

// Collapsible section component
function CollapsibleSection({
  title,
  icon: Icon,
  defaultOpen = false,
  badge,
  children,
}: {
  title: string
  icon: typeof Server
  defaultOpen?: boolean
  badge?: string | number
  children: React.ReactNode
}) {
  const [isOpen, setIsOpen] = useState(defaultOpen)

  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center justify-between px-4 py-3 bg-gray-50 hover:bg-gray-100 transition-colors"
      >
        <div className="flex items-center gap-2">
          <Icon className="h-5 w-5 text-gray-500" />
          <span className="font-medium text-gray-900">{title}</span>
          {badge !== undefined && (
            <span className="px-2 py-0.5 bg-gray-200 text-gray-700 rounded-full text-xs">{badge}</span>
          )}
        </div>
        {isOpen ? <ChevronUp className="h-5 w-5 text-gray-400" /> : <ChevronDown className="h-5 w-5 text-gray-400" />}
      </button>
      {isOpen && <div className="p-4 border-t border-gray-200">{children}</div>}
    </div>
  )
}

export default function InfrastructureTab() {
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)

  // Data state. Each panel carries its own outcome: the five endpoints behind this tab do not
  // stand or fall together, and on Kubernetes only the metrics one answers at all.
  const [servicesPanel, setServicesPanel] = useState<Panel<InfrastructureServicesResponse>>({
    data: null,
    failure: null,
  })
  const [dockerPanel, setDockerPanel] = useState<Panel<DockerOverviewResponse>>({
    data: null,
    failure: null,
  })
  const [metricsPanel, setMetricsPanel] = useState<Panel<InfrastructureMetricsResponse>>({
    data: null,
    failure: null,
  })
  const [systemPanel, setSystemPanel] = useState<Panel<SystemInfoResponse>>({
    data: null,
    failure: null,
  })
  const [rangeDebugPanel, setRangeDebugPanel] = useState<Panel<RangeDebugResponse>>({
    data: null,
    failure: null,
  })

  // What this install can do, read as two primitives rather than through useFeature: that helper
  // answers false while the capabilities request is still in flight, and here the difference
  // between "no Docker" and "not known yet" decides whether a failure may name a substrate.
  const hasDocker = useCapabilitiesStore((s) => s.features?.docker_status ?? null)
  const substrateLabel = useCapabilitiesStore((s) => s.substrateLabel)

  // Platform update. Starting one restarts the API, so the request that starts
  // it is the last this connection completes; everything after is polling that
  // fails until the new API is up.
  const [updateState, setUpdateState] = useState<PlatformUpdateStatus['state']>('idle')
  const [updateLog, setUpdateLog] = useState<string | null>(null)
  const [updateBusy, setUpdateBusy] = useState(false)
  const [updateError, setUpdateError] = useState<string | null>(null)
  const [confirmUpdate, setConfirmUpdate] = useState(false)

  // Whether the remote has anything this host does not. Consulting the remote
  // spawns a container, so this runs on mount and on demand -- never on the
  // auto-refresh timer.
  const [check, setCheck] = useState<PlatformUpdateCheck | null>(null)
  const [checkBusy, setCheckBusy] = useState(false)

  // The credential the update uses to fetch. Write-only: the API reports
  // whether one is set and never returns the token, so there is nothing to
  // populate the field with and no value to leak back to the browser.
  const [cred, setCred] = useState<GitCredentialStatus | null>(null)
  const [credUser, setCredUser] = useState('')
  const [credToken, setCredToken] = useState('')
  const [credBusy, setCredBusy] = useState(false)
  const [credError, setCredError] = useState<string | null>(null)
  const [editingCred, setEditingCred] = useState(false)

  // Logs state
  const [selectedService, setSelectedService] = useState('api')
  const [logLevel, setLogLevel] = useState<string>('')
  const [logSearch, setLogSearch] = useState('')
  const [logs, setLogs] = useState<ServiceLogsResponse | null>(null)
  const [logsFailure, setLogsFailure] = useState<PanelFailure | null>(null)
  const [logsLoading, setLogsLoading] = useState(false)
  const logRequest = useRef(0)

  // Auto-refresh state
  const [autoRefresh, setAutoRefresh] = useState(false)
  const [refreshInterval, setRefreshInterval] = useState(30)

  const fetchData = useCallback(async (showLoading = true) => {
    if (showLoading) setLoading(true)
    setRefreshing(true)

    // allSettled, not all: four of these five read the Docker daemon and refuse on a Kubernetes
    // install, and under Promise.all that one rejection threw away the metrics answer with them --
    // host CPU, database size and queue depth, all of which work on both substrates, were never
    // drawn and the page rendered bare headings over "Request failed with status code 500".
    //
    // The finally is the floor under that: the whole tab is behind the `loading` spinner, so
    // anything that escaped here would leave the page spinning for good rather than showing one
    // broken panel.
    try {
      const [servicesRes, dockerRes, metricsRes, systemRes, rangeDebugRes] =
        await Promise.allSettled([
          infrastructureApi.getServices(),
          infrastructureApi.getDocker(),
          infrastructureApi.getMetrics(),
          infrastructureApi.getSystem(),
          infrastructureApi.getRangeDebug(),
        ])

      setServicesPanel(panelFrom(servicesRes, PANEL.services))
      setDockerPanel(panelFrom(dockerRes, PANEL.docker))
      setMetricsPanel(panelFrom(metricsRes, PANEL.metrics))
      setSystemPanel(panelFrom(systemRes, PANEL.system))
      setRangeDebugPanel(panelFrom(rangeDebugRes, PANEL.ranges))
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [])

  const fetchLogs = useCallback(async () => {
    // Which read the pane is entitled to speak for. The search box has no debounce, so every
    // keystroke starts another one; now that a failure reaches the pane rather than the console,
    // an answer arriving out of order would report an error against a query the user has already
    // replaced -- the same wrong fact this pane is being fixed for telling.
    const seq = ++logRequest.current
    setLogsLoading(true)
    try {
      const res = await infrastructureApi.getLogs({
        service: selectedService,
        level: logLevel || undefined,
        search: logSearch || undefined,
        limit: 200,
      })
      if (seq !== logRequest.current) return
      setLogs(res.data)
      // A 200 is not proof the logs were read: the endpoint reports a container it could not find
      // in filters_applied and returns an empty list beside it.
      setLogsFailure(logsNotice(res.data))
    } catch (err) {
      // The failure has to reach the pane. Swallowing it into console.error left the pane falling
      // through to "No logs found", which told an admin debugging a deploy that the worker had
      // printed nothing when in truth the request never succeeded.
      if (seq !== logRequest.current) return
      setLogs(null)
      setLogsFailure(describeFailure(err, PANEL.logs))
    } finally {
      if (seq === logRequest.current) setLogsLoading(false)
    }
  }, [selectedService, logLevel, logSearch])

  // Initial fetch
  useEffect(() => {
    fetchData()
  }, [fetchData])

  // Fetch logs when service or filters change
  useEffect(() => {
    fetchLogs()
  }, [fetchLogs])

  // Auto-refresh
  useEffect(() => {
    if (!autoRefresh) return

    const interval = setInterval(() => {
      fetchData(false)
      fetchLogs()
    }, refreshInterval * 1000)

    return () => clearInterval(interval)
  }, [autoRefresh, refreshInterval, fetchData, fetchLogs])

  const loadCredential = useCallback(async () => {
    try {
      const res = await infrastructureApi.getUpdateCredential()
      setCred(res.data)
      if (res.data.username) setCredUser(res.data.username)
    } catch {
      // Not fatal: the update card still works, it just cannot show whether a
      // credential is set.
    }
  }, [])

  const runUpdateCheck = useCallback(async () => {
    setCheckBusy(true)
    try {
      const res = await infrastructureApi.checkForUpdate()
      setCheck(res.data)
    } catch {
      // Report not-knowing rather than leaving a stale "up to date" on screen.
      setCheck({
        checked: false,
        update_available: false,
        behind: null,
        branch: null,
        current_sha: null,
        current_version: null,
        latest_tag: null,
        detail: 'The check could not be run.',
      })
    } finally {
      setCheckBusy(false)
    }
  }, [])

  useEffect(() => {
    loadCredential()
    runUpdateCheck()
  }, [loadCredential, runUpdateCheck])

  const saveCredential = async () => {
    setCredBusy(true)
    setCredError(null)
    try {
      const res = await infrastructureApi.setUpdateCredential(credUser.trim(), credToken)
      setCred(res.data)
      setCredToken('')
      setEditingCred(false)
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setCredError(detail || 'Could not save the credential')
    } finally {
      setCredBusy(false)
    }
  }

  const removeCredential = async () => {
    setCredBusy(true)
    setCredError(null)
    try {
      await infrastructureApi.deleteUpdateCredential()
      setCred({ configured: false, username: null, updated_at: null, readable: true })
      setCredToken('')
    } catch {
      setCredError('Could not remove the credential')
    } finally {
      setCredBusy(false)
    }
  }

  const runUpdate = async () => {
    setUpdateBusy(true)
    setUpdateError(null)
    try {
      const res = await infrastructureApi.startUpdate()
      setUpdateState('running')
      setUpdateLog(res.data.message)
      setConfirmUpdate(false)
      pollUpdate()
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setUpdateError(detail || 'Could not start the update')
      setUpdateBusy(false)
    }
  }

  // Polls across the restart. Failures are expected while the API is down, so
  // they are ignored rather than surfaced -- only a real result changes state.
  const pollUpdate = useCallback(() => {
    let tries = 0
    const tick = async () => {
      tries += 1
      try {
        const res = await infrastructureApi.getUpdateStatus()
        setUpdateState(res.data.state)
        if (res.data.log_tail) setUpdateLog(res.data.log_tail)
        if (res.data.state === 'running') {
          setTimeout(tick, 4000)
        } else {
          setUpdateBusy(false)
        }
        return
      } catch {
        // API is restarting; keep waiting.
      }
      if (tries < 90) setTimeout(tick, 4000)
      else {
        setUpdateBusy(false)
        setUpdateError('Lost contact with the API. Reload the page to check.')
      }
    }
    setTimeout(tick, 4000)
  }, [])

  // Only a CONFIRMED up-to-date disables the update button. A check that could
  // not run leaves it enabled: failing closed would take the update away
  // exactly when something is already wrong.
  const upToDate = check?.checked === true && !check.update_available
  const updateCheck = updateCheckState(check, checkBusy)

  // Each panel's data, or null where it has a failure to show instead.
  const services = servicesPanel.data
  const docker = dockerPanel.data
  const metrics = metricsPanel.data
  const system = systemPanel.data
  const rangeDebug = rangeDebugPanel.data

  // The Docker-only surfaces are restated here rather than at fetch time, so the wording follows
  // the capabilities answer whenever it lands instead of the page having to fetch all five
  // endpoints again to catch up with it.
  const asDocker = (failure: PanelFailure | null, label: string) =>
    dockerFailure(failure, label, hasDocker, substrateLabel)
  const servicesFailure = asDocker(servicesPanel.failure, PANEL.services)
  const dockerOverviewFailure = asDocker(dockerPanel.failure, PANEL.docker)
  const systemFailure = asDocker(systemPanel.failure, PANEL.system)
  const rangeDebugFailure = asDocker(rangeDebugPanel.failure, PANEL.ranges)
  const logPane = logPaneState({
    loading: logsLoading,
    failure: asDocker(logsFailure, PANEL.logs),
    logs,
  })

  const downloadLogs = () => {
    if (!logs) return
    const content = logs.logs.map((l) => l.raw).join('\n')
    const blob = new Blob([content], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${selectedService}-logs-${new Date().toISOString().split('T')[0]}.log`
    a.click()
    URL.revokeObjectURL(url)
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-8 w-8 animate-spin text-primary-600" />
        <span className="ml-2 text-gray-600">Loading infrastructure data...</span>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Platform update. Deliberately first and behind a confirm: it restarts
          every service, and this page loses contact while it runs. */}
      <div className="rounded-lg border border-gray-200 bg-white p-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3 className="text-sm font-medium text-gray-900">Platform update</h3>
            <p className="mt-1 text-sm text-gray-600">
              Pulls the latest code on the branch this host is on and redeploys. The
              API and workers restart, so this page loses contact for a minute or two.
              Running ranges are unaffected; the database, catalogs and images are kept.
            </p>
            {updateError && <p className="mt-2 text-sm text-red-600">{updateError}</p>}
            {updateState !== 'idle' && (
              <p className="mt-2 text-sm text-gray-700">
                Status: <span className="font-medium">{updateState}</span>
              </p>
            )}

            {/* What the remote has that this host does not. Four distinct states,
                deliberately, and no two of them may render the same: up to date, an
                update is waiting, nothing to pull for a reason the endpoint gives, and
                "could not look". `updateCheckState` owns the choice -- see its note. */}
            <div className="mt-3 flex items-center gap-2 text-sm">
              {updateCheck.kind === 'checking' ? (
                <span className="inline-flex items-center gap-1.5 text-gray-600">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Checking for updates...
                </span>
              ) : updateCheck.kind === 'nothing-to-pull' ? (
                <span className="inline-flex items-center gap-1.5 text-amber-700">
                  <AlertCircle className="h-3.5 w-3.5" />
                  Nothing to pull
                  {check?.current_version && ` (this host: ${check.current_version})`}
                </span>
              ) : updateCheck.kind === 'current' ? (
                <span className="inline-flex items-center gap-1.5 text-green-700">
                  <CheckCircle className="h-3.5 w-3.5" />
                  Up to date
                  {check?.current_version && ` (v${check.current_version})`}
                </span>
              ) : updateCheck.kind === 'available' ? (
                <span className="inline-flex items-center gap-1.5 text-amber-700">
                  <AlertCircle className="h-3.5 w-3.5" />
                  Update available
                  {typeof check?.behind === 'number' &&
                    ` - ${check.behind} commit${check.behind === 1 ? '' : 's'} behind`}
                  {check?.latest_tag && `, latest ${check.latest_tag}`}
                </span>
              ) : updateCheck.kind === 'failed' ? (
                <span className="inline-flex items-center gap-1.5 text-gray-600">
                  <XCircle className="h-3.5 w-3.5" />
                  Could not check for updates
                </span>
              ) : null}
              <button
                onClick={runUpdateCheck}
                disabled={checkBusy || updateBusy}
                className="text-xs text-blue-600 hover:underline disabled:opacity-50"
              >
                Check again
              </button>
            </div>
            {/* The reason, as text rather than a tooltip. When the check refuses because the
                registry asked for its auth token from an unexpected host, this sentence names
                the host, the one it expected and the setting that would permit it -- all of
                which was previously reachable only by hovering.

                Rendered for a check that SUCCEEDED too, in amber rather than red: a checked=true
                answer carrying a detail is the endpoint explaining why there is nothing to pull,
                and that sentence is the only place an operator is told what to do instead. */}
            {check?.detail && (
              <p className={`mt-2 text-sm ${check.checked ? 'text-amber-700' : 'text-red-600'}`}>
                {check.detail}
              </p>
            )}
            {check?.checked && check.current_sha && (
              <p className="mt-1 text-xs text-gray-500">
                On {check.branch ?? 'this branch'} at {check.current_sha}
              </p>
            )}

            {/* Credential for the code remote. The host's own git helper is not
                reachable from the update container, so the platform needs its
                own token. Write-only: the API never returns it. */}
            <div className="mt-4 border-t border-gray-100 pt-3">
              {editingCred ? (
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <input
                      type="text"
                      value={credUser}
                      onChange={(e) => setCredUser(e.target.value)}
                      placeholder="Username"
                      autoComplete="off"
                      className="w-40 rounded-md border border-gray-300 px-2 py-1 text-sm"
                    />
                    <input
                      type="password"
                      value={credToken}
                      onChange={(e) => setCredToken(e.target.value)}
                      placeholder="Access token"
                      autoComplete="new-password"
                      className="w-64 rounded-md border border-gray-300 px-2 py-1 text-sm"
                    />
                    <button
                      onClick={saveCredential}
                      disabled={credBusy || !credToken.trim() || !credUser.trim()}
                      className="rounded-md bg-primary-600 px-3 py-1 text-sm font-medium text-white hover:bg-primary-700 disabled:opacity-50"
                    >
                      Save
                    </button>
                    <button
                      onClick={() => {
                        setEditingCred(false)
                        setCredToken('')
                        setCredError(null)
                      }}
                      disabled={credBusy}
                      className="rounded-md border border-gray-300 px-3 py-1 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                    >
                      Cancel
                    </button>
                  </div>
                  <p className="text-xs text-gray-500">
                    A read-only token with permission to fetch this repository. Stored
                    encrypted and never shown again after saving.
                  </p>
                </div>
              ) : (
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="text-gray-600">Update credential:</span>
                  {cred?.configured && cred.readable && (
                    <span className="font-medium text-gray-900">
                      set{cred.username ? ` (${cred.username})` : ''}
                    </span>
                  )}
                  {cred?.configured && !cred.readable && (
                    <span className="font-medium text-amber-700">
                      unreadable - set it again
                    </span>
                  )}
                  {!cred?.configured && (
                    <span className="text-gray-500">
                      not set - the update cannot fetch without one
                    </span>
                  )}
                  <button
                    onClick={() => setEditingCred(true)}
                    className="rounded-md border border-gray-300 px-2 py-1 text-xs text-gray-700 hover:bg-gray-50"
                  >
                    {cred?.configured ? 'Replace' : 'Set token'}
                  </button>
                  {cred?.configured && (
                    <button
                      onClick={removeCredential}
                      disabled={credBusy}
                      className="rounded-md border border-gray-300 px-2 py-1 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                    >
                      Remove
                    </button>
                  )}
                </div>
              )}
              {credError && <p className="mt-2 text-sm text-red-600">{credError}</p>}
            </div>
          </div>
          {confirmUpdate ? (
            <div className="flex shrink-0 items-center gap-2">
              <button
                onClick={() => setConfirmUpdate(false)}
                disabled={updateBusy}
                className="px-3 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={runUpdate}
                disabled={updateBusy}
                className="px-3 py-2 text-sm font-medium text-white bg-red-600 rounded-lg hover:bg-red-700 disabled:opacity-50"
              >
                {updateBusy ? 'Updating...' : 'Yes, update and restart'}
              </button>
            </div>
          ) : (
            <button
              onClick={() => setConfirmUpdate(true)}
              disabled={updateBusy || upToDate}
              // The greyed-out button's own explanation. "Already on the latest commit" is true
              // of one of the two reasons it is greyed out and a lie about the other, so where
              // the endpoint gave a reason, that is the reason shown.
              title={
                updateCheck.kind === 'nothing-to-pull'
                  ? updateCheck.detail
                  : upToDate
                    ? 'Already on the latest commit for this branch'
                    : undefined
              }
              className="shrink-0 px-3 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 disabled:opacity-50"
            >
              Update platform
            </button>
          )}
        </div>
        {updateLog && (
          <pre className="mt-3 max-h-48 overflow-auto rounded bg-gray-900 p-3 text-xs text-gray-100">
            {updateLog}
          </pre>
        )}
      </div>

      {/* Header with refresh controls */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold text-gray-900">Infrastructure Observability</h2>
          {services && (
            <StatusBadge status={services.overall_status} />
          )}
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-sm text-gray-600">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              className="rounded border-gray-300 text-primary-600 focus:ring-primary-500"
            />
            Auto-refresh
          </label>
          {autoRefresh && (
            <select
              value={refreshInterval}
              onChange={(e) => setRefreshInterval(Number(e.target.value))}
              className="text-sm border-gray-300 rounded-md"
            >
              <option value={10}>10s</option>
              <option value={30}>30s</option>
              <option value={60}>60s</option>
            </select>
          )}
          <button
            onClick={() => { fetchData(); fetchLogs(); }}
            disabled={refreshing}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50"
          >
            <RefreshCw className={clsx('h-4 w-4', refreshing && 'animate-spin')} />
            Refresh
          </button>
        </div>
      </div>

      {/* Service Health Grid */}
      <div className="bg-white rounded-lg shadow p-4">
        <h3 className="text-sm font-medium text-gray-900 mb-4 flex items-center gap-2">
          <Server className="h-4 w-4" />
          Service Health
        </h3>
        {servicesFailure && <PanelNotice failure={servicesFailure} />}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {services?.services.map((service) => (
            <div
              key={service.name}
              className={clsx(
                'p-4 rounded-lg border',
                service.status === 'healthy' && 'border-green-200 bg-green-50',
                service.status === 'unhealthy' && 'border-red-200 bg-red-50',
                service.status === 'degraded' && 'border-yellow-200 bg-yellow-50',
                service.status === 'unknown' && 'border-gray-200 bg-gray-50'
              )}
            >
              <div className="flex items-center justify-between mb-2">
                <span className="font-medium text-gray-900">{service.display_name}</span>
                <StatusBadge status={service.status} />
              </div>
              {service.uptime_human && (
                <div className="flex items-center gap-1 text-xs text-gray-500 mb-2">
                  <Clock className="h-3 w-3" />
                  Uptime: {service.uptime_human}
                </div>
              )}
              {service.cpu_percent !== null && service.cpu_percent !== undefined && (
                <div className="mb-1">
                  <div className="flex items-center justify-between text-xs text-gray-500 mb-0.5">
                    <span className="flex items-center gap-1">
                      <Cpu className="h-3 w-3" />
                      CPU
                    </span>
                    <span>{service.cpu_percent.toFixed(1)}%</span>
                  </div>
                  <ProgressBar
                    value={service.cpu_percent}
                    color={service.cpu_percent > 80 ? 'red' : service.cpu_percent > 50 ? 'yellow' : 'green'}
                  />
                </div>
              )}
              {service.memory_percent !== null && service.memory_percent !== undefined && (
                <div>
                  <div className="flex items-center justify-between text-xs text-gray-500 mb-0.5">
                    <span className="flex items-center gap-1">
                      <MemoryStick className="h-3 w-3" />
                      Memory
                    </span>
                    <span>{service.memory_mb?.toFixed(0)} MB ({service.memory_percent.toFixed(1)}%)</span>
                  </div>
                  <ProgressBar
                    value={service.memory_percent}
                    color={service.memory_percent > 80 ? 'red' : service.memory_percent > 50 ? 'yellow' : 'green'}
                  />
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Log Viewer */}
      <div className="bg-white rounded-lg shadow p-4">
        <h3 className="text-sm font-medium text-gray-900 mb-4 flex items-center gap-2">
          <FileText className="h-4 w-4" />
          Log Viewer
        </h3>
        <div className="flex flex-wrap items-center gap-3 mb-4">
          <select
            value={selectedService}
            onChange={(e) => setSelectedService(e.target.value)}
            className="text-sm border-gray-300 rounded-md"
          >
            <option value="api">API Server</option>
            <option value="worker">Task Worker</option>
            <option value="db">PostgreSQL</option>
            <option value="redis">Redis</option>
            <option value="minio">MinIO</option>
            <option value="traefik">Traefik</option>
            <option value="frontend">Frontend</option>
          </select>
          <select
            value={logLevel}
            onChange={(e) => setLogLevel(e.target.value)}
            className="text-sm border-gray-300 rounded-md"
          >
            <option value="">All Levels</option>
            <option value="error">Error</option>
            <option value="warning">Warning</option>
            <option value="info">Info</option>
            <option value="debug">Debug</option>
          </select>
          <div className="relative flex-1 min-w-[200px]">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
            <input
              type="text"
              placeholder="Search logs..."
              value={logSearch}
              onChange={(e) => setLogSearch(e.target.value)}
              className="w-full pl-8 pr-3 py-1.5 text-sm border-gray-300 rounded-md"
            />
          </div>
          <button
            onClick={downloadLogs}
            disabled={!logs || logs.logs.length === 0}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50"
          >
            <Download className="h-4 w-4" />
            Download
          </button>
        </div>
        <div className="bg-gray-900 rounded-lg p-3 max-h-80 overflow-auto font-mono text-xs">
          {logPane.kind === 'loading' ? (
            <div className="flex items-center justify-center py-8 text-gray-400">
              <Loader2 className="h-5 w-5 animate-spin mr-2" />
              Loading logs...
            </div>
          ) : logPane.kind === 'failed' ? (
            <div
              className={clsx(
                'flex items-start justify-center gap-2 py-8 px-4 text-center',
                logPane.failure.kind === 'substrate' ? 'text-gray-400' : 'text-red-300'
              )}
            >
              {logPane.failure.kind === 'substrate' ? (
                <Info className="mt-0.5 h-4 w-4 shrink-0" />
              ) : (
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              )}
              <span className="font-sans">{logPane.failure.message}</span>
            </div>
          ) : logPane.kind === 'entries' ? (
            logPane.entries.map((entry, idx) => (
              <div key={idx} className="py-0.5 hover:bg-gray-800 rounded">
                {entry.timestamp && (
                  <span className="text-gray-500">{new Date(entry.timestamp).toLocaleTimeString()} </span>
                )}
                {entry.level && (
                  <span
                    className={clsx(
                      'font-semibold',
                      entry.level === 'ERROR' && 'text-red-400',
                      entry.level === 'WARN' && 'text-yellow-400',
                      entry.level === 'INFO' && 'text-blue-400',
                      entry.level === 'DEBUG' && 'text-gray-400'
                    )}
                  >
                    [{entry.level}]{' '}
                  </span>
                )}
                <span className="text-gray-200">{entry.message}</span>
              </div>
            ))
          ) : logPane.kind === 'empty' ? (
            <div className="text-gray-400 text-center py-8">
              No logs found{logLevel || logSearch ? ' for these filters' : ''}
            </div>
          ) : (
            <div className="text-gray-400 text-center py-8">No logs requested yet</div>
          )}
        </div>
        {logs && !logsFailure && (
          <div className="mt-2 text-xs text-gray-500">
            Showing {logs.logs.length} of {logs.total_lines} lines
            {logs.has_more && ' (more available)'}
          </div>
        )}
      </div>

      {/* Docker Overview */}
      <div className="bg-white rounded-lg shadow p-4">
        <h3 className="text-sm font-medium text-gray-900 mb-4 flex items-center gap-2">
          <Container className="h-4 w-4" />
          Docker Overview
        </h3>
        {dockerOverviewFailure && <PanelNotice failure={dockerOverviewFailure} />}
        {docker && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-4">
            <div className="text-center p-3 bg-gray-50 rounded-lg">
              <div className="text-2xl font-bold text-gray-900">{docker.summary.total_containers}</div>
              <div className="text-xs text-gray-500">Containers ({docker.summary.running_containers} running)</div>
            </div>
            <div className="text-center p-3 bg-gray-50 rounded-lg">
              <div className="text-2xl font-bold text-gray-900">{docker.summary.total_networks}</div>
              <div className="text-xs text-gray-500">Networks ({docker.summary.proving_ground_networks} {BRANDING.productName})</div>
            </div>
            <div className="text-center p-3 bg-gray-50 rounded-lg">
              <div className="text-2xl font-bold text-gray-900">{docker.summary.total_volumes}</div>
              <div className="text-xs text-gray-500">Volumes</div>
            </div>
            <div className="text-center p-3 bg-gray-50 rounded-lg">
              <div className="text-2xl font-bold text-gray-900">{docker.summary.total_images}</div>
              <div className="text-xs text-gray-500">Images</div>
            </div>
          </div>
        )}
        {docker && (
          <div className="space-y-3">
            <CollapsibleSection title="Containers" icon={Container} badge={docker?.containers.length}>
              <div className="overflow-x-auto">
                <table className="min-w-full text-sm">
                  <thead>
                    <tr className="border-b">
                      <th className="text-left py-2 px-2 font-medium text-gray-600">Name</th>
                      <th className="text-left py-2 px-2 font-medium text-gray-600">Image</th>
                      <th className="text-left py-2 px-2 font-medium text-gray-600">Status</th>
                      <th className="text-left py-2 px-2 font-medium text-gray-600">Type</th>
                    </tr>
                  </thead>
                  <tbody>
                    {docker?.containers.map((c) => (
                      <tr key={c.id} className="border-b border-gray-100 hover:bg-gray-50">
                        <td className="py-2 px-2 font-mono text-xs">{c.name}</td>
                        <td className="py-2 px-2 text-gray-600 max-w-xs truncate">{c.image}</td>
                        <td className="py-2 px-2">
                          <StatusBadge status={c.status} />
                        </td>
                        <td className="py-2 px-2">
                          {c.is_proving_ground_infra && <span className="text-xs bg-blue-100 text-blue-800 px-1.5 py-0.5 rounded">Infra</span>}
                          {c.is_proving_ground_vm && <span className="text-xs bg-purple-100 text-purple-800 px-1.5 py-0.5 rounded">VM</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CollapsibleSection>

            <CollapsibleSection title="Networks" icon={Network} badge={docker?.networks.length}>
              <div className="overflow-x-auto">
                <table className="min-w-full text-sm">
                  <thead>
                    <tr className="border-b">
                      <th className="text-left py-2 px-2 font-medium text-gray-600">Name</th>
                      <th className="text-left py-2 px-2 font-medium text-gray-600">Subnet</th>
                      <th className="text-left py-2 px-2 font-medium text-gray-600">Driver</th>
                      <th className="text-left py-2 px-2 font-medium text-gray-600">Containers</th>
                    </tr>
                  </thead>
                  <tbody>
                    {docker?.networks.map((n) => (
                      <tr key={n.id} className="border-b border-gray-100 hover:bg-gray-50">
                        <td className="py-2 px-2 font-mono text-xs">
                          {n.name}
                          {n.is_proving_ground_range && (
                            <span className="ml-2 text-xs bg-green-100 text-green-800 px-1.5 py-0.5 rounded">Range</span>
                          )}
                        </td>
                        <td className="py-2 px-2 text-gray-600">{n.subnet || '-'}</td>
                        <td className="py-2 px-2 text-gray-600">{n.driver}</td>
                        <td className="py-2 px-2 text-gray-600">{n.container_count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CollapsibleSection>

            <CollapsibleSection title="Images" icon={Box} badge={docker?.images.length}>
              <div className="overflow-x-auto">
                <table className="min-w-full text-sm">
                  <thead>
                    <tr className="border-b">
                      <th className="text-left py-2 px-2 font-medium text-gray-600">Tags</th>
                      <th className="text-left py-2 px-2 font-medium text-gray-600">Size</th>
                      <th className="text-left py-2 px-2 font-medium text-gray-600">Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {docker?.images.slice(0, 20).map((img) => (
                      <tr key={img.id} className="border-b border-gray-100 hover:bg-gray-50">
                        <td className="py-2 px-2">
                          {img.tags.length > 0 ? (
                            <span className="font-mono text-xs">{img.tags[0]}</span>
                          ) : (
                            <span className="text-gray-400 text-xs">&lt;none&gt;</span>
                          )}
                          {img.is_proving_ground_related && (
                            <span className="ml-2 text-xs bg-purple-100 text-purple-800 px-1.5 py-0.5 rounded">{BRANDING.productName}</span>
                          )}
                        </td>
                        <td className="py-2 px-2 text-gray-600">{img.size_human}</td>
                        <td className="py-2 px-2 text-gray-600 text-xs">
                          {img.created ? new Date(img.created).toLocaleDateString() : '-'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {docker && docker.images.length > 20 && (
                  <div className="text-xs text-gray-500 mt-2">
                    Showing 20 of {docker.images.length} images
                  </div>
                )}
              </div>
            </CollapsibleSection>
          </div>
        )}
      </div>

      {/* Resource Metrics */}
      <div className="bg-white rounded-lg shadow p-4">
        <h3 className="text-sm font-medium text-gray-900 mb-4 flex items-center gap-2">
          <Activity className="h-4 w-4" />
          Resource Metrics
        </h3>
        {metricsPanel.failure && <PanelNotice failure={metricsPanel.failure} />}
        {metrics && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            {/* Host Metrics */}
            <div className="space-y-3">
              <h4 className="font-medium text-gray-700 flex items-center gap-2">
                <Cpu className="h-4 w-4" />
                Host System
              </h4>
              <div>
                <div className="flex justify-between text-sm text-gray-600 mb-1">
                  <span>CPU ({metrics.host.cpu_count} cores)</span>
                  <span>{metrics.host.cpu_percent.toFixed(1)}%</span>
                </div>
                <ProgressBar
                  value={metrics.host.cpu_percent}
                  color={metrics.host.cpu_percent > 80 ? 'red' : metrics.host.cpu_percent > 50 ? 'yellow' : 'blue'}
                />
              </div>
              <div>
                <div className="flex justify-between text-sm text-gray-600 mb-1">
                  <span>Memory</span>
                  <span>
                    {(metrics.host.memory_used_mb / 1024).toFixed(1)} / {(metrics.host.memory_total_mb / 1024).toFixed(1)} GB
                  </span>
                </div>
                <ProgressBar
                  value={metrics.host.memory_percent}
                  color={metrics.host.memory_percent > 80 ? 'red' : metrics.host.memory_percent > 50 ? 'yellow' : 'blue'}
                />
              </div>
              <div>
                <div className="flex justify-between text-sm text-gray-600 mb-1">
                  <span>Disk</span>
                  <span>
                    {metrics.host.disk_used_gb.toFixed(1)} / {metrics.host.disk_total_gb.toFixed(1)} GB
                  </span>
                </div>
                <ProgressBar
                  value={metrics.host.disk_percent}
                  color={metrics.host.disk_percent > 80 ? 'red' : metrics.host.disk_percent > 50 ? 'yellow' : 'blue'}
                />
              </div>
              {metrics.host.load_average && (
                <div className="text-sm text-gray-600">
                  Load Average: {metrics.host.load_average.map((l) => l.toFixed(2)).join(' / ')}
                </div>
              )}
            </div>

            {/* Database Metrics */}
            <div className="space-y-3">
              <h4 className="font-medium text-gray-700 flex items-center gap-2">
                <Database className="h-4 w-4" />
                Database
              </h4>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div className="p-2 bg-gray-50 rounded">
                  <div className="text-gray-500">Size</div>
                  <div className="font-medium">{metrics.database.database_size_human}</div>
                </div>
                <div className="p-2 bg-gray-50 rounded">
                  <div className="text-gray-500">Tables</div>
                  <div className="font-medium">{metrics.database.table_count}</div>
                </div>
                <div className="p-2 bg-gray-50 rounded">
                  <div className="text-gray-500">Connections</div>
                  <div className="font-medium">{metrics.database.connection_count}</div>
                </div>
                <div className="p-2 bg-gray-50 rounded">
                  <div className="text-gray-500">Active</div>
                  <div className="font-medium">{metrics.database.active_connections}</div>
                </div>
              </div>
            </div>

            {/* Task Queue Metrics */}
            <div className="space-y-3">
              <h4 className="font-medium text-gray-700 flex items-center gap-2">
                <Activity className="h-4 w-4" />
                Task Queue
              </h4>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div className="p-2 bg-gray-50 rounded">
                  <div className="text-gray-500">Queue Length</div>
                  <div className="font-medium">{metrics.task_queue.queue_length}</div>
                </div>
                <div className="p-2 bg-gray-50 rounded">
                  <div className="text-gray-500">Delayed</div>
                  <div className="font-medium">{metrics.task_queue.delayed_messages}</div>
                </div>
              </div>
            </div>

            {/* Storage Metrics */}
            <div className="space-y-3">
              <h4 className="font-medium text-gray-700 flex items-center gap-2">
                <HardDrive className="h-4 w-4" />
                Storage
              </h4>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div className="p-2 bg-gray-50 rounded">
                  <div className="text-gray-500">ISO Cache</div>
                  <div className="font-medium">{metrics.storage.iso_cache_size_mb.toFixed(1)} MB</div>
                  <div className="text-xs text-gray-400">{metrics.storage.iso_cache_files} files</div>
                </div>
                <div className="p-2 bg-gray-50 rounded">
                  <div className="text-gray-500">Templates</div>
                  <div className="font-medium">{metrics.storage.template_storage_size_mb.toFixed(1)} MB</div>
                  <div className="text-xs text-gray-400">{metrics.storage.template_storage_files} files</div>
                </div>
                <div className="p-2 bg-gray-50 rounded">
                  <div className="text-gray-500">VM Storage</div>
                  <div className="font-medium">{metrics.storage.vm_storage_size_mb.toFixed(1)} MB</div>
                  <div className="text-xs text-gray-400">{metrics.storage.vm_storage_dirs} VMs</div>
                </div>
                <div className="p-2 bg-gray-50 rounded">
                  <div className="text-gray-500">MinIO</div>
                  <div className="font-medium">{metrics.storage.minio_total_size_mb.toFixed(1)} MB</div>
                  <div className="text-xs text-gray-400">{metrics.storage.minio_total_objects} objects</div>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* System Information */}
      <div className="bg-white rounded-lg shadow p-4">
        <h3 className="text-sm font-medium text-gray-900 mb-4 flex items-center gap-2">
          <Settings className="h-4 w-4" />
          System Information
        </h3>
        {systemFailure && <PanelNotice failure={systemFailure} />}
        {system && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div className="space-y-2">
              <div className="flex justify-between text-sm">
                <span className="text-gray-500">Version</span>
                <span className="font-medium">{system.version}</span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-gray-500">Commit</span>
                <span className="font-mono text-xs">{system.commit}</span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-gray-500">Architecture</span>
                <span className="font-medium">
                  {system.architecture}
                  {system.is_arm && <span className="ml-1 text-xs bg-yellow-100 text-yellow-800 px-1 rounded">ARM</span>}
                </span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-gray-500">Python</span>
                <span className="font-medium">{system.python_version}</span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-gray-500">Docker</span>
                <span className="font-medium">{system.docker_version || 'N/A'}</span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-gray-500">DB Revision</span>
                <span className="font-mono text-xs">{system.database_revision || 'N/A'}</span>
              </div>
            </div>
            <div className="space-y-2">
              <h4 className="font-medium text-gray-700 text-sm">Configuration</h4>
              {system.config.map((item) => (
                <div key={item.key} className="flex justify-between text-sm">
                  <span className="text-gray-500">{item.key}</span>
                  <span className="font-mono text-xs truncate max-w-[200px]" title={item.value}>
                    {item.value}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Range Debug Information */}
      <div className="bg-white rounded-lg shadow p-4">
        <h3 className="text-sm font-medium text-gray-900 mb-4 flex items-center gap-2">
          <Database className="h-4 w-4" />
          Range Debug Info
          {rangeDebug && (
            <span className="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full">
              {rangeDebug.total_count} ranges
            </span>
          )}
        </h3>
        {rangeDebugFailure && <PanelNotice failure={rangeDebugFailure} />}
        {rangeDebug && (
          <div className="space-y-4">
            {/* Summary */}
            <div className="flex gap-4 text-sm">
              <div className="p-2 bg-gray-50 rounded">
                <span className="text-gray-500">DinD Containers (Docker): </span>
                <span className="font-medium">{rangeDebug.dind_containers_in_docker.length}</span>
              </div>
              <div className="p-2 bg-gray-50 rounded">
                <span className="text-gray-500">Ranges in DB: </span>
                <span className="font-medium">{rangeDebug.total_count}</span>
              </div>
            </div>

            {/* Range Details */}
            <div className="space-y-3">
              {rangeDebug.ranges.map((range) => (
                <CollapsibleSection
                  key={range.id}
                  title={range.name}
                  icon={Server}
                  badge={range.status}
                >
                  <div className="space-y-4">
                    {/* DinD Info */}
                    <div>
                      <h5 className="text-xs font-medium text-gray-700 mb-2">DinD Container</h5>
                      <div className="grid grid-cols-2 gap-2 text-xs">
                        <div>
                          <span className="text-gray-500">Container ID: </span>
                          <span className={clsx('font-mono', !range.dind_container_id && 'text-red-500')}>
                            {range.dind_container_id?.slice(0, 12) || 'NOT SET'}
                          </span>
                        </div>
                        <div>
                          <span className="text-gray-500">Docker URL: </span>
                          <span className={clsx('font-mono', !range.dind_docker_url && 'text-red-500')}>
                            {range.dind_docker_url || 'NOT SET'}
                          </span>
                        </div>
                        <div>
                          <span className="text-gray-500">Container Name: </span>
                          <span className="font-mono">{range.dind_container_name || '-'}</span>
                        </div>
                        <div>
                          <span className="text-gray-500">Mgmt IP: </span>
                          <span className="font-mono">{range.dind_mgmt_ip || '-'}</span>
                        </div>
                      </div>
                    </div>

                    {/* Router Info */}
                    <div>
                      <h5 className="text-xs font-medium text-gray-700 mb-2">Router</h5>
                      <div className="text-xs">
                        <span className="text-gray-500">Container: </span>
                        <span className="font-mono">{range.router_container_id?.slice(0, 12) || 'None'}</span>
                        {range.router_status && (
                          <span className="ml-2">
                            <StatusBadge status={range.router_status.toLowerCase()} />
                          </span>
                        )}
                      </div>
                    </div>

                    {/* VNC Proxy Mappings */}
                    <div>
                      <h5 className="text-xs font-medium text-gray-700 mb-2">
                        VNC Proxy Mappings ({Object.keys(range.vnc_proxy_mappings || {}).length})
                      </h5>
                      {range.vnc_proxy_mappings && Object.keys(range.vnc_proxy_mappings).length > 0 ? (
                        <div className="overflow-x-auto">
                          <table className="min-w-full text-xs">
                            <thead>
                              <tr className="border-b text-left">
                                <th className="py-1 px-2 text-gray-500">VM ID</th>
                                <th className="py-1 px-2 text-gray-500">Proxy Port</th>
                                <th className="py-1 px-2 text-gray-500">Original Port</th>
                                <th className="py-1 px-2 text-gray-500">Host</th>
                              </tr>
                            </thead>
                            <tbody>
                              {Object.entries(range.vnc_proxy_mappings).map(([vmId, mapping]) => (
                                <tr key={vmId} className="border-b border-gray-100">
                                  <td className="py-1 px-2 font-mono">{vmId.slice(0, 8)}...</td>
                                  <td className="py-1 px-2">{mapping.proxy_port}</td>
                                  <td className="py-1 px-2">{mapping.original_port}</td>
                                  <td className="py-1 px-2">{mapping.proxy_host}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      ) : (
                        <p className="text-xs text-gray-500">No VNC proxy mappings configured</p>
                      )}
                    </div>

                    {/* VMs */}
                    <div>
                      <h5 className="text-xs font-medium text-gray-700 mb-2">VMs ({range.vms.length})</h5>
                      {range.vms.length > 0 ? (
                        <div className="overflow-x-auto">
                          <table className="min-w-full text-xs">
                            <thead>
                              <tr className="border-b text-left">
                                <th className="py-1 px-2 text-gray-500">Hostname</th>
                                <th className="py-1 px-2 text-gray-500">Status</th>
                                <th className="py-1 px-2 text-gray-500">Container ID</th>
                                <th className="py-1 px-2 text-gray-500">IP</th>
                                <th className="py-1 px-2 text-gray-500">Image</th>
                              </tr>
                            </thead>
                            <tbody>
                              {range.vms.map((vm) => (
                                <tr key={vm.id} className="border-b border-gray-100">
                                  <td className="py-1 px-2 font-medium">{vm.hostname}</td>
                                  <td className="py-1 px-2">
                                    <StatusBadge status={vm.status.toLowerCase()} />
                                  </td>
                                  <td className="py-1 px-2 font-mono">
                                    {vm.container_id?.slice(0, 12) || <span className="text-gray-400">-</span>}
                                  </td>
                                  <td className="py-1 px-2">{vm.ip_address || '-'}</td>
                                  <td className="py-1 px-2 truncate max-w-[150px]" title={vm.base_image || ''}>
                                    {vm.base_image || '-'}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      ) : (
                        <p className="text-xs text-gray-500">No VMs in this range</p>
                      )}
                    </div>
                  </div>
                </CollapsibleSection>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
