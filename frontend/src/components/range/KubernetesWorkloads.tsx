/**
 * The operator's view of a Kubernetes range -- COSMOS PG-61.
 *
 * An Era B range has no Network or VM rows, so the Builder tab's tables show nothing for it; its
 * machines come from the blueprint and their state from the cluster. This panel asks
 * /ranges/{id}/workloads and renders nothing at all for an Era A range, so the page reads the
 * same as before on a Docker host.
 *
 * Three things the first version got wrong, and what replaces them:
 *
 * Freshness. It fetched once and then only when the *range's* status changed, which is a
 * different thing from a machine's status: a machine still importing its disk, and one that had
 * just crashed, both sat on screen looking exactly like a running one until somebody pressed
 * Refresh. It now polls, fast while any machine is mid-transition and slowly once they have all
 * settled, stops entirely while the tab is hidden, and says how old what you are looking at is
 * rather than implying it is live. The websocket carries EventLog rows only -- nothing in the
 * Kubernetes path emits one per machine -- so polling is the honest mechanism here, and the
 * header's "updated Ns ago" is the honest claim about it.
 *
 * Control. Stopping the whole range to clear one wedged machine is the only lever a KubeVirt
 * range had. Start, stop and restart are per machine here, over
 * `POST /ranges/{id}/workloads/{machine}/power`; on an install whose backend predates that route,
 * and for a viewer that route will only refuse, the controls disappear rather than render buttons
 * that cannot work.
 *
 * Legibility. A machine sitting in Provisioning is usually waiting on its boot image, which the
 * panel never showed, and the cluster's own words for a failure -- `ErrImagePull`, `absent` --
 * are not sentences anyone can act on. Both are translated, with the raw status kept in the
 * tooltip for whoever does want it.
 */
import { useCallback, useEffect, useState } from 'react'
import {
  AlertCircle,
  Cpu,
  ExternalLink,
  HardDrive,
  MemoryStick,
  Monitor,
  Play,
  RefreshCw,
  RotateCcw,
  Square,
} from 'lucide-react'
import type { RangeApp, Workload } from '../../hooks/useRangeWorkloads'

/**
 * What this panel reads beyond the shared shape.
 *
 * All three are optional because the backend may not report them yet: a field that is absent
 * hides its row of the display instead of rendering "undefined" at the operator.
 */
type Machine = Workload & {
  /** What the boot disk was built from -- the blueprint's `bootImage`. */
  boot_image?: string | null
  /** The boot disk's import, while one is running. Absent once there is nothing to import. */
  image_import?: { phase?: string | null; progress?: string | null } | null
  /** The actions the server will accept for this machine now. Absent means "infer from status". */
  actions?: string[] | null
}

interface WorkloadsResponse {
  substrate: 'dind' | 'kubernetes'
  namespace?: string
  workloads: Machine[]
  apps?: RangeApp[]
  /**
   * Whether this viewer may power the range's machines. The power endpoint is owner-or-admin,
   * while the listing an instructor or evaluator sees is not, so without this the controls are
   * offered to people whose every click can only be refused. Absent means "assume yes and let
   * the refusal say so", which is how it behaves on a backend that does not send it.
   */
  can_control?: boolean
}

export type MachineAction = 'start' | 'stop' | 'restart'

const ACTIONS: readonly MachineAction[] = ['start', 'stop', 'restart']

const ACTION_ICON = { start: Play, stop: Square, restart: RotateCcw }
const ACTION_VERB: Record<MachineAction, string> = {
  start: 'Start',
  stop: 'Stop',
  restart: 'Restart',
}
const ACTION_FAILURE: Record<MachineAction, string> = {
  start: 'The machine could not be started. Try again, or check the range on the cluster.',
  stop: 'The machine could not be stopped. Try again, or check the range on the cluster.',
  restart: 'The machine could not be restarted. Try again, or check the range on the cluster.',
}
/**
 * The server answers `changed: false` when the machine was already in the state asked for. Saying
 * so beats the alternative, which is a button that reports nothing and a row that never moves.
 */
const ACTION_NO_CHANGE: Record<MachineAction, string> = {
  start: 'That machine was already running.',
  stop: 'That machine was already stopped.',
  restart: 'Nothing changed on that machine.',
}

/** KubeVirt statuses that resolve on their own, given time. Their presence is what speeds polling up. */
const TRANSITIONAL = new Set([
  'Provisioning',
  'Starting',
  'Stopping',
  'Terminating',
  'Migrating',
  'WaitingForVolumeBinding',
  'Unknown',
])

/**
 * KubeVirt statuses that will not resolve on their own -- the same set the deploy path refuses on
 * (`capability/lifecycle.py` `_VM_TERMINAL`). A machine here is wedged, which is precisely when an
 * operator wants stop and restart.
 */
const FAILED = new Set([
  'CrashLoopBackOff',
  'DataVolumeError',
  'ErrImagePull',
  'ErrorDataVolumeNotFound',
  'ErrorPvcNotFound',
  'ErrorUnschedulable',
  'ImagePullBackOff',
])

const STATUS_STYLE: Record<string, string> = {
  Running: 'bg-green-100 text-green-800',
  Stopped: 'bg-gray-100 text-gray-700',
  Paused: 'bg-gray-100 text-gray-700',
  Provisioning: 'bg-yellow-100 text-yellow-800',
  Starting: 'bg-yellow-100 text-yellow-800',
  Stopping: 'bg-yellow-100 text-yellow-800',
  Terminating: 'bg-yellow-100 text-yellow-800',
  Migrating: 'bg-yellow-100 text-yellow-800',
  WaitingForVolumeBinding: 'bg-yellow-100 text-yellow-800',
  absent: 'bg-gray-100 text-gray-500',
}

/** The cluster's word for a state, in words an operator can act on. */
const STATUS_LABEL: Record<string, string> = {
  absent: 'Not created',
  WaitingForVolumeBinding: 'Waiting for storage',
  ErrImagePull: 'Image unavailable',
  ImagePullBackOff: 'Image unavailable',
  ErrorUnschedulable: 'No node has room',
  ErrorPvcNotFound: 'Disk missing',
  ErrorDataVolumeNotFound: 'Disk missing',
  DataVolumeError: 'Disk failed',
  CrashLoopBackOff: 'Crashing on start',
}

/** Why an action cannot be taken while a machine is mid-transition. */
const BLOCKED_WHILE: Record<string, string> = {
  Provisioning: 'This machine is still being created',
  Starting: 'This machine is starting',
  Stopping: 'This machine is stopping',
  Terminating: 'This machine is being removed',
  Migrating: 'This machine is moving to another node',
  WaitingForVolumeBinding: 'This machine is waiting for its disk',
  Unknown: 'The cluster has not reported this machine yet',
  absent: 'This machine has not been created — deploy the range first',
}

/** CDI's import phases, as something an operator reads rather than a phase name. */
const IMPORT_PHASE: Record<string, string> = {
  Pending: 'boot image import queued',
  PVCBound: 'boot image import starting',
  ImportScheduled: 'boot image import queued',
  ImportInProgress: 'importing boot image',
  CloneScheduled: 'copying boot image',
  CloneInProgress: 'copying boot image',
  Paused: 'boot image import paused',
  Failed: 'boot image import failed',
}

const ACTIVE_POLL_MS = 5_000
const SETTLED_POLL_MS = 30_000
const MAX_POLL_MS = 60_000
/** How often the "updated Ns ago" line recomputes. Cheap, and coarse enough not to lie. */
const AGE_TICK_MS = 5_000
/**
 * How long a machine this panel just powered stays on the fast cadence.
 *
 * KubeVirt does not move `printableStatus` while the request is open: the read taken the moment
 * a stop returns still says Running, and the status that replaces it lands seconds later. Falling
 * straight back to the settled cadence leaves the row unchanged for half a minute after the
 * button that moved it -- which is the staleness this panel exists to fix, at the one moment the
 * operator is watching for it.
 */
const ACTION_WATCH_MS = 30_000

export function isTransitional(status: string): boolean {
  return TRANSITIONAL.has(status)
}

/**
 * How long to wait before reading the cluster again.
 *
 * Fast while anything is still moving, slow once nothing is, and slower still after a failed
 * read -- a backend that is down should not be asked twelve times a minute by every open tab.
 */
export function pollDelayMs(statuses: readonly string[], consecutiveFailures = 0): number {
  const base = statuses.some(isTransitional) ? ACTIVE_POLL_MS : SETTLED_POLL_MS
  if (consecutiveFailures <= 0) return base
  return Math.min(base * 2 ** consecutiveFailures, MAX_POLL_MS)
}

export function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status
}

/**
 * The tooltip behind the state pill.
 *
 * The pill says what happened in English; an operator diagnosing a range still needs the word the
 * cluster used, so it is kept here rather than dropped. `absent` is this API's own token for a
 * workload with no VirtualMachine at all, which means nothing to anyone, so it gets a sentence.
 */
export function statusTitle(status: string): string {
  if (status === 'absent') return 'No machine has been created for this workload yet'
  if (status === 'Unknown') return 'The cluster has not reported a state for this machine'
  return `Cluster status: ${status}`
}

/** "linux" is the blueprint's word for the OS family; it is not a word to show anybody. */
export function osLabel(family: string, version: string): string {
  const name =
    family === 'macos'
      ? 'macOS'
      : family
        ? family.charAt(0).toUpperCase() + family.slice(1)
        : ''
  return [name, version].filter(Boolean).join(' ')
}

/**
 * What can be done to a machine in this state.
 *
 * `advertised` is the server's own answer and wins when it is given. The inference behind it
 * covers a backend that does not send one: a wedged machine counts as running, because stopping
 * and restarting it is the entire point of having these controls.
 */
export function allowedActions(
  status: string,
  advertised?: readonly string[] | null
): MachineAction[] {
  if (advertised) return ACTIONS.filter((a) => advertised.includes(a))
  if (status === 'absent' || isTransitional(status)) return []
  if (status === 'Stopped') return ['start']
  if (status === 'Running' || status === 'Paused' || FAILED.has(status)) return ['stop', 'restart']
  return []
}

/** The sentence on a control that is present but cannot be used. */
export function actionBlockedReason(status: string, action: MachineAction): string {
  const blocked = BLOCKED_WHILE[status]
  if (blocked) return blocked
  if (action === 'start') return 'This machine is already running'
  return `This machine is ${statusLabel(status).toLowerCase()}`
}

/**
 * The boot disk's import, or null when there is nothing to say about it.
 *
 * A machine stuck in Provisioning is usually waiting on exactly this, and CDI reports progress as
 * the literal string "N/A" until it knows the size -- which is not a number to show anyone.
 */
export function importLabel(imp?: { phase?: string | null; progress?: string | null } | null): string | null {
  const phase = imp?.phase?.trim()
  if (!phase || phase === 'Succeeded') return null
  const base = IMPORT_PHASE[phase] ?? 'preparing boot image'
  const progress = imp?.progress?.trim()
  return progress && progress !== 'N/A' ? `${base} · ${progress}` : base
}

/**
 * The boot image reference as an operator should read it, or null when there is none.
 *
 * CDI imports a container disk from `docker://<ref>`, and a blueprint is allowed to spell that
 * scheme itself -- `capability/kubevirt.py` `_registry_url` adds it only when it is absent. The
 * word is CDI's transport, not anything on this install, so it does not belong on a page whose
 * whole subject is a substrate that has no Docker in it.
 */
export function bootImageLabel(image?: string | null): string | null {
  const ref = image?.trim()
  if (!ref) return null
  return ref.replace(/^docker:\/\//i, '')
}

/**
 * Whether a refusal means "this install has no such endpoint" rather than "no such range".
 *
 * FastAPI answers an unmatched path with the literal `{"detail": "Not Found"}`, while every
 * handler here names what was not found. Telling the two apart is what lets the controls
 * disappear on an older backend instead of failing one click at a time.
 */
export function isRouteMissing(status: number, detail: string | null): boolean {
  if (status === 405) return true
  return status === 404 && (!detail || detail.trim().toLowerCase() === 'not found')
}

/**
 * The reason the server gave, or a sentence the user can act on when it gave none.
 *
 * Never the exception text: an error body that is not one of the shapes this API produces is a
 * shape nobody meant to show a user, and the fallback says more than a traceback fragment does.
 */
export function serverMessage(body: unknown, fallback: string): string {
  if (!body || typeof body !== 'object') return fallback
  const detail = (body as { detail?: unknown }).detail
  if (typeof detail === 'string') {
    const text = detail.trim()
    if (text && text.toLowerCase() !== 'not found') return text
    return fallback
  }
  if (Array.isArray(detail)) {
    const messages = detail
      .map((entry) => (entry && typeof entry === 'object' ? (entry as { msg?: unknown }).msg : null))
      .filter((msg): msg is string => typeof msg === 'string' && msg.trim() !== '')
    if (messages.length) return messages.join('; ')
    return fallback
  }
  if (detail && typeof detail === 'object') {
    const structured = detail as { message?: unknown; errors?: unknown; hint?: unknown }
    const parts: string[] = []
    if (typeof structured.message === 'string' && structured.message) parts.push(structured.message)
    if (Array.isArray(structured.errors)) {
      parts.push(...structured.errors.filter((e): e is string => typeof e === 'string' && e !== ''))
    }
    if (typeof structured.hint === 'string' && structured.hint) parts.push(structured.hint)
    if (parts.length) return parts.join(' · ')
  }
  return fallback
}

/**
 * What to tell the user when a power request is refused.
 *
 * The 4xx refusals name a fix -- "web is stopped; start it rather than restarting it" -- and are
 * worth repeating verbatim. A 5xx detail is the cluster client's exception text interpolated into
 * a string by the API; it names no fix and belongs in the server log, so the user gets a sentence
 * that does.
 */
export function actionFailureMessage(
  httpStatus: number,
  body: unknown,
  action: MachineAction
): string {
  if (httpStatus >= 500) return ACTION_FAILURE[action]
  return serverMessage(body, ACTION_FAILURE[action])
}

/** How stale the panel is, said plainly. The header claims this and nothing more. */
export function updatedAgo(ageMs: number): string {
  if (!Number.isFinite(ageMs) || ageMs < 10_000) return 'updated just now'
  const seconds = Math.floor(ageMs / 1000)
  if (seconds < 60) return `updated ${seconds}s ago`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `updated ${minutes}m ago`
  return `updated ${Math.floor(minutes / 60)}h ago`
}

export function KubernetesWorkloads({ rangeId, rangeStatus }: { rangeId: string; rangeStatus: string }) {
  const [data, setData] = useState<WorkloadsResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [appError, setAppError] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const [failures, setFailures] = useState(0)
  // Bumped after every completed read, successful or not: it is what re-arms the poll timer.
  const [reads, setReads] = useState(0)
  const [now, setNow] = useState(() => Date.now())
  const [visible, setVisible] = useState(true)
  const [pending, setPending] = useState<Record<string, MachineAction>>({})
  const [rowError, setRowError] = useState<Record<string, string>>({})
  const [rowNote, setRowNote] = useState<Record<string, string>>({})
  const [controlsSupported, setControlsSupported] = useState(true)
  const [controlsRefused, setControlsRefused] = useState(false)
  // When this panel last moved a machine, for as long as that is worth watching closely.
  const [actedAt, setActedAt] = useState<number | null>(null)

  const load = useCallback(
    async (silent = false) => {
      const token = localStorage.getItem('token') || ''
      if (!silent) setBusy(true)
      try {
        const res = await fetch(`/api/v1/ranges/${rangeId}/workloads`, {
          headers: { Authorization: `Bearer ${token}` },
        })
        if (!res.ok) {
          const body = (await res.json().catch(() => null)) as unknown
          throw new Error(serverMessage(body, 'The cluster could not be read just now'))
        }
        setData((await res.json()) as WorkloadsResponse)
        setUpdatedAt(Date.now())
        setLoadError(null)
        setFailures(0)
      } catch (err) {
        // The last good answer stays on screen -- a failed refresh should not blank a working
        // page -- but the header says it is stale, which is the part that was missing.
        setLoadError(err instanceof Error ? err.message : 'The cluster could not be read just now')
        setFailures((n) => n + 1)
      } finally {
        if (!silent) setBusy(false)
        setReads((n) => n + 1)
      }
    },
    [rangeId]
  )

  // Refetch when the range changes state -- a deploy finishing is what makes consoles appear.
  useEffect(() => {
    void load()
  }, [load, rangeStatus])

  // A hidden tab is a tab nobody is reading. Polling it costs the cluster an API call every few
  // seconds for nothing, so the timer stops and the first thing a return does is a fresh read.
  useEffect(() => {
    const onChange = () => {
      const isVisible = !document.hidden
      setVisible(isVisible)
      if (isVisible) {
        // The age line stopped ticking while the tab was hidden, so it would report the age it
        // had on the way out until the next tick -- which is the stale claim being fixed here.
        setNow(Date.now())
        void load(true)
      }
    }
    setVisible(!document.hidden)
    document.addEventListener('visibilitychange', onChange)
    return () => document.removeEventListener('visibilitychange', onChange)
  }, [load])

  const isKubernetes = data?.substrate === 'kubernetes'
  const statuses = data?.workloads.map((w) => w.status) ?? []
  const delay = pollDelayMs(statuses, failures)
  // A transition this panel started is worth watching closely even if the cluster has not caught
  // up enough to report a transitional status yet, and it stays worth watching for a while after
  // the request returns -- see ACTION_WATCH_MS.
  const acting = Object.keys(pending).length > 0
  const watching = actedAt !== null && now - actedAt < ACTION_WATCH_MS
  const pollDelay = acting || watching ? ACTIVE_POLL_MS : delay
  // Keep polling while the substrate is still unknown *because* the first read failed: the panel
  // renders nothing until it knows, so without this a single failed read hides it until reload.
  const shouldPoll = visible && (isKubernetes || (data === null && failures > 0))

  useEffect(() => {
    if (!shouldPoll) return
    const timer = setTimeout(() => void load(true), pollDelay)
    return () => clearTimeout(timer)
  }, [shouldPoll, pollDelay, reads, load])

  useEffect(() => {
    if (!isKubernetes || !visible) return
    const timer = setInterval(() => setNow(Date.now()), AGE_TICK_MS)
    return () => clearInterval(timer)
  }, [isKubernetes, visible])

  if (!data || !isKubernetes) return null

  // The application's URL is on the cluster's ingress, authorised by a cookie the API mints for
  // this range only. Get the cookie with the session, then open the URL -- the browser sends the
  // cookie on its own from there.
  const openApp = async (app: RangeApp) => {
    setAppError(null)
    const token = localStorage.getItem('token') || ''
    try {
      const res = await fetch(`/api/v1/ranges/${rangeId}/apps/${encodeURIComponent(app.name)}/ticket`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
        credentials: 'same-origin',
      })
      if (!res.ok) {
        const body = (await res.json().catch(() => null)) as unknown
        throw new Error(serverMessage(body, `${app.name} could not be opened. Check that the range is running.`))
      }
      const { url } = (await res.json()) as { url: string }
      window.open(url, `app_${rangeId}_${app.name}`)
    } catch (err) {
      setAppError(err instanceof Error ? err.message : `${app.name} could not be opened`)
    }
  }

  const openConsole = (machine: string) => {
    window.open(
      `/console/k8s/${rangeId}/${encodeURIComponent(machine)}`,
      `console_k8s_${rangeId}_${machine}`,
      'width=1280,height=800,menubar=no,toolbar=no,location=no,status=no'
    )
  }

  const act = async (machine: string, action: MachineAction) => {
    const token = localStorage.getItem('token') || ''
    setRowError((prev) => ({ ...prev, [machine]: '' }))
    setRowNote((prev) => ({ ...prev, [machine]: '' }))
    setPending((prev) => ({ ...prev, [machine]: action }))
    try {
      const res = await fetch(
        `/api/v1/ranges/${rangeId}/workloads/${encodeURIComponent(machine)}/power`,
        {
          method: 'POST',
          headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
          body: JSON.stringify({ action }),
        }
      )
      if (!res.ok) {
        const body = (await res.json().catch(() => null)) as { detail?: unknown } | null
        const detail = typeof body?.detail === 'string' ? body.detail : null
        if (isRouteMissing(res.status, detail)) {
          // Not a failure of this machine: the install has no per-machine controls at all, so
          // they come off the page rather than failing once per click for the rest of the session.
          setControlsSupported(false)
          return
        }
        if (res.status === 403) {
          // The power route is owner-or-admin while the listing is not, so a 403 here is about
          // the viewer and not the machine, and nothing about it can change while this page is
          // open. Leaving the buttons up offers a refusal per machine per click; taking them
          // down and saying who may use them is the answer the reader can act on.
          setControlsRefused(true)
          return
        }
        throw new Error(actionFailureMessage(res.status, body, action))
      }
      const { changed } = (await res.json().catch(() => ({}))) as { changed?: boolean }
      if (changed === false) {
        setRowNote((prev) => ({ ...prev, [machine]: ACTION_NO_CHANGE[action] }))
      } else {
        setActedAt(Date.now())
      }
      await load(true)
    } catch (err) {
      setRowError((prev) => ({
        ...prev,
        [machine]: err instanceof Error ? err.message : ACTION_FAILURE[action],
      }))
    } finally {
      setPending((prev) => {
        const next = { ...prev }
        delete next[machine]
        return next
      })
    }
  }

  const freshness = updatedAt === null ? 'not read yet' : updatedAgo(now - updatedAt)
  const mayControl = data.can_control !== false && !controlsRefused
  const showControls = controlsSupported && mayControl

  return (
    <div className="bg-white shadow rounded-lg mb-6">
      <div className="px-4 py-5 sm:px-6 flex items-center justify-between border-b">
        <div>
          <h3 className="text-lg font-medium text-gray-900">Workloads</h3>
          <p className="text-sm text-gray-500">
            Machines in this range{data.namespace ? ` · namespace ${data.namespace}` : ''} · {freshness}
          </p>
          {loadError && (
            <p className="mt-1 text-sm text-amber-700 inline-flex items-center gap-1">
              <AlertCircle className="h-4 w-4 shrink-0" />
              Showing the last state read. {loadError}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={busy}
          className="inline-flex items-center gap-1 px-3 py-1.5 text-sm border rounded-md text-gray-700 hover:bg-gray-50 disabled:opacity-50"
        >
          <RefreshCw className={`h-4 w-4 ${busy ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>
      {data.apps && data.apps.length > 0 && (
        <div className="px-4 py-3 sm:px-6 border-b bg-gray-50 flex flex-wrap items-center gap-3">
          <span className="text-sm font-medium text-gray-700">Applications</span>
          {data.apps.map((app) => (
            <button
              key={app.name}
              type="button"
              onClick={() => void openApp(app)}
              disabled={!app.published}
              // The backend distinguishes three reasons an application is not published -- no
              // applications host, no ingress class, range not running -- and says which.
              // Restating them here only got them wrong.
              title={
                app.published
                  ? `Open ${app.name} in a new window`
                  : app.reason ?? 'This application is not published yet'
              }
              className="inline-flex items-center gap-1 px-3 py-1.5 text-sm border rounded-md bg-white text-gray-800 hover:bg-gray-100 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <ExternalLink className="h-4 w-4" />
              {app.name}
            </button>
          ))}
          {appError && <span className="text-sm text-red-600">{appError}</span>}
        </div>
      )}
      {data.workloads.length === 0 ? (
        <div className="px-4 py-8 text-center text-sm text-gray-500">This range declares no machines.</div>
      ) : (
        <ul className="divide-y">
          {data.workloads.map((w) => {
            const inFlight = pending[w.name]
            const allowed = allowedActions(w.status, w.actions)
            const importing = importLabel(w.image_import)
            const bootImage = bootImageLabel(w.boot_image)
            return (
              <li key={w.name} className="px-4 py-4 sm:px-6">
                <div className="flex items-center justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-gray-900">{w.name}</span>
                      <span
                        title={statusTitle(w.status)}
                        className={`px-2 py-0.5 rounded-full text-xs ${
                          STATUS_STYLE[w.status] ?? (FAILED.has(w.status) ? 'bg-red-100 text-red-800' : 'bg-gray-100 text-gray-700')
                        }`}
                      >
                        {statusLabel(w.status)}
                      </span>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-gray-500">
                      <span>{osLabel(w.os_family, w.os_version)}</span>
                      <span className="inline-flex items-center gap-1"><Cpu className="h-3.5 w-3.5" />{w.cpus}</span>
                      <span className="inline-flex items-center gap-1"><MemoryStick className="h-3.5 w-3.5" />{w.memory_mb} MB</span>
                      {Object.entries(w.addresses)
                        .filter(([iface]) => iface !== 'default')
                        .map(([iface, ip]) => (
                          <span key={iface} className="font-mono text-xs">{iface} {ip}</span>
                        ))}
                    </div>
                    {(bootImage || importing) && (
                      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-gray-500">
                        <HardDrive className="h-3.5 w-3.5 shrink-0" />
                        {bootImage && <span className="font-mono break-all">{bootImage}</span>}
                        {importing && <span className="text-amber-700">{importing}</span>}
                      </div>
                    )}
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {showControls && (
                      <div className="inline-flex items-center rounded-md border divide-x overflow-hidden">
                        {ACTIONS.map((action) => {
                          const Icon = ACTION_ICON[action]
                          const enabled = allowed.includes(action) && !inFlight
                          return (
                            <button
                              key={action}
                              type="button"
                              onClick={() => void act(w.name, action)}
                              disabled={!enabled}
                              aria-label={`${ACTION_VERB[action]} ${w.name}`}
                              title={
                                inFlight
                                  ? `${ACTION_VERB[inFlight]} in progress`
                                  : allowed.includes(action)
                                    ? `${ACTION_VERB[action]} ${w.name}`
                                    : actionBlockedReason(w.status, action)
                              }
                              className="px-2.5 py-1.5 text-gray-700 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent"
                            >
                              <Icon
                                className={`h-4 w-4 ${inFlight === action ? 'animate-pulse' : ''}`}
                              />
                            </button>
                          )
                        })}
                      </div>
                    )}
                    <button
                      type="button"
                      onClick={() => openConsole(w.name)}
                      disabled={!w.console_available}
                      title={
                        w.console_available
                          ? `Open the console for ${w.name}`
                          : 'The console opens once the machine is running'
                      }
                      className="inline-flex items-center gap-1 px-3 py-1.5 text-sm rounded-md bg-primary-600 text-white hover:bg-primary-700 disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      <Monitor className="h-4 w-4" />
                      Console
                    </button>
                  </div>
                </div>
                {rowError[w.name] && (
                  <p className="mt-2 text-sm text-red-600 inline-flex items-start gap-1">
                    <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
                    {rowError[w.name]}
                  </p>
                )}
                {rowNote[w.name] && <p className="mt-2 text-sm text-gray-500">{rowNote[w.name]}</p>}
              </li>
            )
          })}
        </ul>
      )}
      {!controlsSupported ? (
        <div className="px-4 py-3 sm:px-6 border-t text-sm text-gray-500">
          This install has no per-machine controls. Stop or restart the whole range instead.
        </div>
      ) : (
        !showControls && (
          <div className="px-4 py-3 sm:px-6 border-t text-sm text-gray-500">
            Only the range&rsquo;s owner can power its machines. You can open their consoles.
          </div>
        )
      )}
    </div>
  )
}
