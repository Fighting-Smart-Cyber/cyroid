// frontend/src/pages/ExecutionConsole.tsx
import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Range, VM, MSEL, Network as NetworkType } from '../types'
import { rangesApi, vmsApi, mselApi, networksApi } from '../services/api'
import { VMGrid } from '../components/execution/VMGrid'
import { EventLogComponent } from '../components/execution/EventLog'
import { MSELUpload } from '../components/execution/MSELUpload'
import { InjectTimeline } from '../components/execution/InjectTimeline'
import { NetworkInterfaces } from '../components/execution/NetworkInterfaces'
import { WorkloadGrid } from '../components/execution/WorkloadGrid'
import { KubeVirtConsole } from '../components/console/KubeVirtConsole'
import { ErrorBoundary } from '../components/common/ErrorBoundary'
import { useRangeWorkloads, type RangeApp } from '../hooks/useRangeWorkloads'
import { useSubstrate } from '../stores/capabilitiesStore'
import { Activity, AlertCircle, Server, ArrowLeft, RefreshCw, X, FileText } from 'lucide-react'
import clsx from 'clsx'

type RightPanelTab = 'events' | 'injects'

export default function ExecutionConsole() {
  const { rangeId } = useParams<{ rangeId: string }>()
  const navigate = useNavigate()
  const [range, setRange] = useState<Range | null>(null)
  const [vms, setVMs] = useState<VM[]>([])
  const [networks, setNetworks] = useState<NetworkType[]>([])
  const [msel, setMSEL] = useState<MSEL | null>(null)
  const [loading, setLoading] = useState(true)

  // A Kubernetes range's machines come from the cluster, not from VM rows.
  const {
    data: k8s,
    error: workloadsError,
    isKubernetes: rangeOnKubernetes,
    refresh: refreshWorkloads,
  } = useRangeWorkloads(rangeId, range?.status)
  const { substrate, isKubernetes: installOnKubernetes } = useSubstrate()
  const workloads = k8s?.workloads ?? []
  const apps = k8s?.apps ?? []
  const [k8sConsole, setK8sConsole] = useState<string | null>(null)
  const [appError, setAppError] = useState<string | null>(null)

  // Which product this page is looking at. Either answer is enough to keep the Docker grid off
  // the screen: the install's, because its controls cannot work here at all, and the range's,
  // because it arrives first on a cold load.
  const onKubernetes = installOnKubernetes || rangeOnKubernetes

  // A Kubernetes install answering "dind" for a range means the range is defined the Era A way,
  // which this install cannot deploy. It has no machines here and never will, and saying so
  // beats an empty grid that looks like a slow load.
  const rangeIsEraA = installOnKubernetes && k8s !== null && !rangeOnKubernetes

  // Neither question has been answered yet, so any grid drawn now is a guess. Both guesses are
  // wrong in a way the user can read: before the install's answer arrives the default is Era A,
  // so a Kubernetes install draws the Docker grid and then swaps it; and before the workloads
  // answer arrives the Kubernetes grid states that the range declares no machines, about a range
  // whose machines have simply not been read yet. The cluster read is the slower of the two, so
  // that second one is not a rare race -- it is every cold load of this page.
  const machinesUndecided = substrate === null || (onKubernetes && k8s === null && !workloadsError)

  // The application's URL is on the cluster's ingress, authorised by a cookie the API mints for
  // this range only. A refused ticket used to do nothing at all: the instructor clicked Open and
  // no window appeared, with nothing anywhere saying why.
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
        const body = (await res.json().catch(() => null)) as { detail?: unknown } | null
        const detail = typeof body?.detail === 'string' ? body.detail : null
        throw new Error(detail ?? `${app.name} could not be opened. Check that the range is running.`)
      }
      const { url } = (await res.json()) as { url: string }
      // The ticket round-trip outlives the click that started it, so the browser no longer
      // counts this as user-initiated and a popup blocker may refuse it. That refusal is
      // silent -- the button looks dead -- unless the null return is read.
      if (!window.open(url, `app_${rangeId}_${app.name}`)) {
        throw new Error(
          `${app.name} is ready, but the browser blocked the window. Allow pop-ups for this site and try again.`
        )
      }
    } catch (err) {
      setAppError(err instanceof Error ? err.message : `${app.name} could not be opened`)
    }
  }

  const [rightPanelTab, setRightPanelTab] = useState<RightPanelTab>('events')

  useEffect(() => {
    if (rangeId) {
      loadRangeData()
      loadMSEL()
      const interval = setInterval(loadRangeData, 10000)
      return () => clearInterval(interval)
    }
  }, [rangeId])

  const loadRangeData = async () => {
    if (!rangeId) return
    try {
      const [rangeData, vmsData, networksData] = await Promise.all([
        rangesApi.get(rangeId),
        vmsApi.list(rangeId),
        networksApi.list(rangeId),
      ])
      setRange(rangeData.data)
      setVMs(vmsData.data)
      setNetworks(networksData.data)
    } catch (error) {
      console.error('Failed to load range data:', error)
    } finally {
      setLoading(false)
    }
  }

  const loadMSEL = async () => {
    if (!rangeId) return
    try {
      const response = await mselApi.get(rangeId)
      setMSEL(response.data)
    } catch {
      // No MSEL exists yet, that's okay
      setMSEL(null)
    }
  }

  const handleMSELLoaded = (newMSEL: MSEL) => {
    setMSEL(newMSEL)
    setRightPanelTab('injects')
  }

  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const handleOpenConsole = (vmId: string, _hostname: string, type: 'vnc' | 'terminal') => {
    // Open in new window by default
    const width = type === 'vnc' ? 1280 : 900
    const height = type === 'vnc' ? 800 : 600
    const left = (window.screen.width - width) / 2
    const top = (window.screen.height - height) / 2
    window.open(
      `/console/${vmId}?type=${type}`,
      `console_${vmId}_${type}`,
      `width=${width},height=${height},left=${left},top=${top},menubar=no,toolbar=no,location=no,status=no,resizable=yes,scrollbars=no`
    )
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500" />
      </div>
    )
  }

  if (!range || !rangeId) {
    return <div className="text-center py-8">Range not found</div>
  }

  const runningMachines = onKubernetes
    ? workloads.filter(w => w.status === 'Running').length
    : vms.filter(vm => vm.status === 'running').length
  const machineTotal = onKubernetes ? workloads.length : vms.length
  const machineNoun = onKubernetes ? 'machines' : 'VMs'

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="bg-white border-b px-6 py-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <button
              onClick={() => navigate(`/ranges/${rangeId}`)}
              className="p-2 hover:bg-gray-100 rounded"
            >
              <ArrowLeft className="w-5 h-5" />
            </button>
            <div>
              <h1 className="text-xl font-semibold">{range.name}</h1>
              <p className="text-sm text-gray-500">{range.description}</p>
            </div>
          </div>
          <div className="flex items-center gap-6">
            <div className="flex items-center gap-2">
              <Server className="w-5 h-5 text-gray-400" />
              <span className="text-sm">
                {machinesUndecided ? (
                  <span className="text-gray-500">Reading machines…</span>
                ) : (
                  <>
                    <span className="font-medium">{runningMachines}</span>
                    <span className="text-gray-500">/{machineTotal} {machineNoun}</span>
                  </>
                )}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <Activity className={clsx('w-5 h-5', range.status === 'running' ? 'text-green-500' : 'text-gray-400')} />
              <span className="text-sm capitalize">{range.status}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div className="flex-1 flex overflow-hidden min-h-0">
        {/* Left panel - the range's machines, whichever substrate they live on */}
        <div className="flex-1 min-w-0 p-4 lg:p-6 overflow-y-auto">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-medium">
              {machinesUndecided || onKubernetes ? 'Machines' : 'Virtual Machines'}
            </h2>
            <MSELUpload rangeId={rangeId} onMSELLoaded={handleMSELLoaded} />
          </div>
          {machinesUndecided ? (
            <div className="border rounded-lg bg-white p-8 flex items-center justify-center gap-3">
              <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-blue-500" />
              <span className="text-sm text-gray-500">Reading this range's machines…</span>
            </div>
          ) : onKubernetes ? (
            rangeIsEraA ? (
              <div className="border rounded-lg bg-white p-8 text-center">
                <Server className="w-8 h-8 text-gray-400 mx-auto mb-2" />
                <p className="text-gray-700">This range has no machines on this install.</p>
                <p className="text-sm text-gray-500 mt-1">
                  It is defined the Docker way, as VMs and networks, and this install runs ranges
                  on Kubernetes. Rebuild it from a Kubernetes blueprint to run it here.
                </p>
              </div>
            ) : workloadsError && !k8s ? (
              <div className="border rounded-lg bg-white p-8 text-center">
                <AlertCircle className="w-8 h-8 text-red-400 mx-auto mb-2" />
                <p className="text-gray-700">Could not read this range's machines.</p>
                <p className="text-sm text-gray-500 mt-1">{workloadsError}</p>
                <button
                  type="button"
                  onClick={refreshWorkloads}
                  className="mt-4 inline-flex items-center gap-1 px-3 py-1.5 text-sm border rounded-md hover:bg-gray-50"
                >
                  <RefreshCw className="w-4 h-4" />
                  Try again
                </button>
              </div>
            ) : (
              <>
                {workloadsError && (
                  <p className="mb-3 text-xs text-amber-700 bg-amber-50 rounded px-2 py-1">
                    Showing the last good answer — the latest refresh failed: {workloadsError}
                  </p>
                )}
                {appError && (
                  <p className="mb-3 text-sm text-red-600">{appError}</p>
                )}
                <WorkloadGrid
                  rangeId={rangeId!}
                  workloads={workloads}
                  apps={apps}
                  onRefresh={refreshWorkloads}
                  onOpenConsole={setK8sConsole}
                  onOpenApp={(app) => void openApp(app)}
                />
              </>
            )
          ) : (
            <>
              <VMGrid
                vms={vms}
                onRefresh={loadRangeData}
                onOpenConsole={handleOpenConsole}
              />

              {/* Network Interfaces - Below VM Grid: addresses live on the workload cards on
                  the Kubernetes path, where there are no Network rows to enumerate. */}
              <div className="mt-6">
                <NetworkInterfaces rangeId={rangeId} vms={vms} networks={networks} />
              </div>
            </>
          )}
        </div>

        {/* Right Panel - Tabbed View */}
        <div className="w-[280px] lg:w-[360px] xl:w-[420px] shrink-0 border-l bg-gray-50 flex flex-col">
          {/* Tabs */}
          <div className="flex border-b bg-white">
            <button
              onClick={() => setRightPanelTab('events')}
              className={clsx(
                'flex-1 flex items-center justify-center gap-2 px-4 py-3 text-sm font-medium border-b-2 transition',
                rightPanelTab === 'events'
                  ? 'border-blue-500 text-blue-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700'
              )}
            >
              <Activity className="w-4 h-4" />
              Events
            </button>
            <button
              onClick={() => setRightPanelTab('injects')}
              className={clsx(
                'flex-1 flex items-center justify-center gap-2 px-4 py-3 text-sm font-medium border-b-2 transition',
                rightPanelTab === 'injects'
                  ? 'border-blue-500 text-blue-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700'
              )}
            >
              <FileText className="w-4 h-4" />
              Injects
              {msel && (
                <span className="ml-1 px-1.5 py-0.5 text-xs bg-gray-200 rounded">
                  {(msel.injects ?? []).filter(i => i.status === 'pending').length}
                </span>
              )}
            </button>
          </div>

          {/* Tab Content */}
          <div className="flex-1 overflow-y-auto p-4">
            {rightPanelTab === 'events' && (
              <EventLogComponent rangeId={rangeId} maxHeight="calc(100vh - 280px)" />
            )}
            {rightPanelTab === 'injects' && (
              msel ? (
                // The timeline renders an inject's stored actions, whose shape is the scenario
                // author's rather than ours. A throw in there used to take the whole application
                // with it, mid-exercise; here it costs the panel only.
                <ErrorBoundary
                  resetKey={msel.id}
                  fallback={(err, retry) => (
                    <div className="bg-white border border-red-200 rounded-lg p-4">
                      <p className="text-sm font-medium text-gray-900">
                        The inject timeline could not be drawn
                      </p>
                      <p className="text-xs text-gray-500 mt-1 break-words">{err.message}</p>
                      <button
                        type="button"
                        onClick={retry}
                        className="mt-3 inline-flex items-center gap-1 px-2 py-1 text-xs border rounded hover:bg-gray-50"
                      >
                        <RefreshCw className="w-3 h-3" />
                        Try again
                      </button>
                    </div>
                  )}
                >
                  <InjectTimeline msel={msel} onInjectUpdate={loadMSEL} />
                </ErrorBoundary>
              ) : (
                <div className="text-center py-8 text-gray-500">
                  <FileText className="w-12 h-12 mx-auto mb-3 opacity-50" />
                  <p className="text-sm">No MSEL loaded</p>
                  <p className="text-xs mt-1">Use the "Import MSEL" button above to load a scenario</p>
                </div>
              )
            )}
          </div>
        </div>
      </div>

      {/* The machine's console, in a modal. Era A opens its consoles in a pop-out window
          instead -- see handleOpenConsole. */}
      {k8sConsole && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-lg shadow-xl w-full h-full max-w-6xl max-h-[90vh] flex flex-col overflow-hidden">
            <div className="flex items-center justify-between px-4 py-2 border-b">
              <h3 className="font-medium">Machine console — {k8sConsole}</h3>
              <button
                onClick={() => setK8sConsole(null)}
                className="p-1 rounded hover:bg-gray-100"
                title="Close"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
            <div className="flex-1 min-h-0">
              <KubeVirtConsole key={k8sConsole} rangeId={rangeId!} workload={k8sConsole} fullscreen />
            </div>
          </div>
        </div>
      )}

    </div>
  )
}
