// frontend/src/components/diagnostics/DiagnosticsTab.tsx
/**
 * The range's Diagnostics tab, on whichever substrate this install runs.
 *
 * Everything this tab used to offer answers a question about a Docker host: a shell into the
 * range's DinD container, VNC socat proxies and iptables rules, and health rows counted off the
 * Network and VM tables. A Kubernetes range has none of those, so on that substrate the tab
 * offered a console that closes 4000, six actions that 400, and "Networks (0) / VMs (0)" beside
 * a range with two networks and a running machine.
 *
 * The two substrates therefore get two surfaces rather than one hedged surface: Era A keeps
 * exactly what it had, and Kubernetes gets what an operator actually needs there -- the
 * namespace the range was placed in, its machines with their phases, sizes and the attachments
 * each one reports, and its capability applications with the reason any of them is not
 * reachable. The range's own failure events are the one panel both share.
 */
import { useState } from 'react'
import {
  AlertTriangle,
  Boxes,
  ExternalLink,
  Layers,
  Network as NetworkIcon,
  RefreshCw,
  Server,
  Terminal,
} from 'lucide-react'
import type { Range, Network, VM } from '../../types'
import { ComponentHealth } from './ComponentHealth'
import { ErrorTimeline } from './ErrorTimeline'
import { LogViewer } from './LogViewer'
import { RangeConsole } from '../console/RangeConsole'
import { VncStatus } from './VncStatus'
import { useCapabilitiesStore } from '../../stores/capabilitiesStore'
import { useRangeWorkloads, type RangeApp } from '../../hooks/useRangeWorkloads'
import {
  hasUnreportedInterfaces,
  phaseTone,
  rangeInterfaces,
  runningCount,
  type PhaseTone,
} from './namespaceView'

interface DiagnosticsTabProps {
  range: Range
  networks: Network[]
  vms: VM[]
}

/** KubeVirt's printableStatus, in the same colours the machine grids use. */
const PHASE_STYLE: Record<PhaseTone, string> = {
  running: 'bg-green-100 text-green-800',
  transitional: 'bg-yellow-100 text-yellow-800',
  failed: 'bg-red-100 text-red-800',
  idle: 'bg-gray-100 text-gray-700',
}

/**
 * Why an application is not reachable, when the backend says so.
 *
 * `/ranges/{id}/workloads` carries a `reason` beside `available`, and it is the whole value of
 * the row: "this install has no host to publish applications on" and "the range is not running"
 * are different problems with different fixes. The hook's `RangeApp` predates the field and the
 * hook is not this component's to change, so it is read defensively -- absent means the row
 * says only whether the application is reachable.
 */
function appReason(app: RangeApp): string | null {
  const answered = app as RangeApp & { reason?: unknown }
  return typeof answered.reason === 'string' && answered.reason ? answered.reason : null
}

export function DiagnosticsTab({ range, networks, vms }: DiagnosticsTabProps) {
  const [selectedVm, setSelectedVm] = useState<VM | null>(null)
  const [showRangeConsole, setShowRangeConsole] = useState(false)

  // Null until /system/capabilities answers. Unknown is not "Docker": guessing here draws the
  // whole Era A tab on a Kubernetes install for a moment, which is the defect being fixed, so
  // nothing substrate-specific renders until the answer is in.
  const features = useCapabilitiesStore((s) => s.features)
  const isLegacy = features?.legacy_range_console === true
  const isComposed = features?.range_composition === true

  const handleSelectVm = (vm: VM) => {
    // Toggle selection if clicking the same VM
    if (selectedVm?.id === vm.id) {
      setSelectedVm(null)
    } else {
      setSelectedVm(vm)
    }
  }

  const handleViewLogs = (vm: VM) => {
    setSelectedVm(vm)
  }

  const token = localStorage.getItem('token') || ''

  if (!features) {
    return (
      <div className="bg-white border border-gray-200 rounded-lg p-6 text-sm text-gray-500">
        Reading what this install can do...
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {isLegacy && (
        <>
          {/* Range Console toggle button */}
          <div className="flex justify-end">
            <button
              onClick={() => setShowRangeConsole(!showRangeConsole)}
              className={`inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-lg transition-colors ${
                showRangeConsole
                  ? 'bg-cyan-600 text-white hover:bg-cyan-700'
                  : 'bg-gray-700 text-gray-200 hover:bg-gray-600'
              }`}
            >
              <Terminal className="w-4 h-4" />
              {showRangeConsole ? 'Hide Range Console' : 'Open Range Console'}
            </button>
          </div>

          {/* Range Console - DinD shell access */}
          {showRangeConsole && (
            <div className="h-[500px]">
              <RangeConsole
                rangeId={range.id}
                rangeName={range.name}
                token={token}
                onClose={() => setShowRangeConsole(false)}
              />
            </div>
          )}

          {/* VNC Status - shows VNC configuration health */}
          <VncStatus rangeId={range.id} />
        </>
      )}

      {/* The cluster's view of the range: the only place in the product that shows it. */}
      {!isLegacy && <NamespaceView range={range} />}

      {/* Two-column layout for health and timeline */}
      <div className={isComposed ? 'grid grid-cols-1 lg:grid-cols-2 gap-4' : ''}>
        {isComposed && (
          <ComponentHealth
            range={range}
            networks={networks}
            vms={vms}
            onSelectVm={handleSelectVm}
            selectedVmId={selectedVm?.id ?? null}
          />
        )}
        <ErrorTimeline
          rangeId={range.id}
          vms={vms}
          onViewLogs={handleViewLogs}
        />
      </div>

      {/* Log viewer - shown when VM is selected */}
      {selectedVm && (
        <LogViewer
          vmId={selectedVm.id}
          vmHostname={selectedVm.hostname}
          onClose={() => setSelectedVm(null)}
        />
      )}
    </div>
  )
}

/**
 * The range as it exists on the cluster.
 *
 * Read live on every visit, because the point of the panel is to answer "what is actually
 * there": the range row's status is the platform's opinion of the range, and this is the
 * cluster's.
 */
function NamespaceView({ range }: { range: Range }) {
  const { data, isLoading, error, refresh } = useRangeWorkloads(range.id, range.status)

  if (!data) {
    return (
      <div className="bg-white border border-gray-200 rounded-lg p-6 text-sm">
        {error ? (
          <div className="flex items-start gap-2 text-red-700">
            <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
            <div>
              <p>{error}</p>
              <button
                onClick={refresh}
                className="mt-2 inline-flex items-center gap-1 text-xs text-primary-600 hover:text-primary-700"
              >
                <RefreshCw className="w-3 h-3" />
                Try again
              </button>
            </div>
          </div>
        ) : (
          <span className="text-gray-500">Reading this range from the cluster...</span>
        )}
      </div>
    )
  }

  if (data.substrate !== 'kubernetes') {
    return (
      <div className="bg-white border border-gray-200 rounded-lg p-6 text-sm text-gray-600">
        <p className="font-medium text-gray-900">This range has no namespace to read.</p>
        {/* The backend answers `dind` for a schema v1 blueprint even on this substrate. That is
            not the same as "declares nothing" -- a v1 blueprint can declare plenty -- and saying
            so would send an operator looking for an empty blueprint instead of an old one. */}
        <p className="mt-1">
          It was built from a schema v1 blueprint, and only v2 declares the workloads, networks
          and capabilities this substrate places on the cluster.
        </p>
      </div>
    )
  }

  const workloads = data.workloads ?? []
  const apps = data.apps ?? []
  const running = runningCount(workloads)
  const unreported = hasUnreportedInterfaces(workloads)

  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
      <div className="px-4 py-3 bg-gray-50 border-b border-gray-200 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <Layers className="w-4 h-4 text-primary-500 flex-shrink-0" />
          <h3 className="text-sm font-medium text-gray-900">Namespace</h3>
          <code className="text-xs font-mono text-gray-600 bg-white border border-gray-200 rounded px-1.5 py-0.5 truncate">
            {data.namespace || 'not recorded'}
          </code>
        </div>
        <div className="flex items-center gap-3 flex-shrink-0">
          <span className="text-xs text-gray-500">
            {running} of {workloads.length} machine{workloads.length === 1 ? '' : 's'} running
          </span>
          <button
            onClick={refresh}
            disabled={isLoading}
            className="p-1 text-gray-400 hover:text-gray-600 disabled:opacity-50"
            title="Refresh"
          >
            <RefreshCw className={`w-4 h-4 ${isLoading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      {error && (
        <div className="px-4 py-2 text-xs text-red-700 bg-red-50 border-b border-red-100">
          {error} — showing the last answer.
        </div>
      )}

      <div className="divide-y divide-gray-100">
        {/* Machines, each with the attachments it is reporting */}
        <section className="px-4 py-3">
          <div className="flex items-center gap-2 mb-2">
            <Server className="w-4 h-4 text-purple-500" />
            <h4 className="text-sm font-medium text-gray-700">Machines</h4>
            <span className="text-xs text-gray-400">({workloads.length})</span>
          </div>
          {workloads.length === 0 ? (
            <p className="text-xs text-gray-500">This range&apos;s blueprint declares no machines.</p>
          ) : (
            <div className="space-y-2">
              {workloads.map((workload) => {
                const interfaces = rangeInterfaces(workload)
                return (
                  <div
                    key={workload.name}
                    className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm"
                  >
                    <span
                      className={`px-2 py-0.5 rounded-full text-xs whitespace-nowrap ${
                        PHASE_STYLE[phaseTone(workload.status)]
                      }`}
                    >
                      {workload.status}
                    </span>
                    <span className="font-medium text-gray-800">{workload.name}</span>
                    <span className="text-xs text-gray-500">
                      {workload.os_family} {workload.os_version} · {workload.cpus} vCPU ·{' '}
                      {workload.memory_mb} MB
                    </span>
                    {interfaces.length > 0 ? (
                      interfaces.map(({ iface, address }) => (
                        <span key={iface} className="text-xs font-mono text-gray-600">
                          <NetworkIcon className="w-3 h-3 inline-block mr-1 text-green-500" />
                          {iface} {address}
                        </span>
                      ))
                    ) : (
                      <span className="text-xs text-gray-400">no attachment reported</span>
                    )}
                  </div>
                )
              })}
              {/* The attachments are listed per machine and never grouped across machines,
                  because the cluster names them by position: net1 is each machine's first
                  attachment, and two machines' net1 need not be the same range network. Saying
                  so beats a row headed "net1" that quietly asserts they are. */}
              <p className="pt-1 text-xs text-gray-500">
                net1, net2 … are a machine&apos;s attachments in the order its blueprint declares
                them. The cluster does not report which of the range&apos;s networks each one is,
                so they cannot be matched up between machines here.
                {unreported && ' It reports an attachment only while a machine is running.'}
              </p>
            </div>
          )}
        </section>

        {/* Capability applications */}
        <section className="px-4 py-3">
          <div className="flex items-center gap-2 mb-2">
            <Boxes className="w-4 h-4 text-blue-500" />
            <h4 className="text-sm font-medium text-gray-700">Capability applications</h4>
            <span className="text-xs text-gray-400">({apps.length})</span>
          </div>
          {apps.length === 0 ? (
            <p className="text-xs text-gray-500">
              This range&apos;s blueprint declares no capability with a web interface.
            </p>
          ) : (
            <div className="space-y-1">
              {apps.map((app) => {
                const reason = appReason(app)
                return (
                  <div key={app.name} className="flex flex-wrap items-baseline gap-x-2 text-sm">
                    <ExternalLink
                      className={`w-3 h-3 ${app.published ? 'text-green-500' : 'text-gray-400'}`}
                    />
                    <span className="font-medium text-gray-800">{app.name}</span>
                    <span
                      className={`text-xs ${app.published ? 'text-green-600' : 'text-gray-500'}`}
                    >
                      {app.published ? 'published' : 'not published'}
                    </span>
                    {!app.published && reason && (
                      <span className="text-xs text-gray-500">{reason}</span>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
