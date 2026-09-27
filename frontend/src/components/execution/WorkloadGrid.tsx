/**
 * The instructor's view of a Kubernetes range's machines, during an exercise.
 *
 * The Execution Console's VM grid reads Era A `VM` rows. A Kubernetes range has none, so an
 * instructor running an exercise saw "No VMs in this range" and "0/0 VMs" while the range's
 * machines were running — the operational screen showed nothing operational. This is the same
 * grid over what that range actually has, plus the range's applications, which an instructor
 * needs to reach as readily as a learner does.
 */
import { Cpu, ExternalLink, MemoryStick, Monitor, RefreshCw } from 'lucide-react'
import type { RangeApp, Workload } from '../../hooks/useRangeWorkloads'

const STATUS_STYLE: Record<string, string> = {
  Running: 'bg-green-100 text-green-800',
  Stopped: 'bg-gray-100 text-gray-700',
  Provisioning: 'bg-yellow-100 text-yellow-800',
  Starting: 'bg-yellow-100 text-yellow-800',
  Stopping: 'bg-yellow-100 text-yellow-800',
  absent: 'bg-gray-100 text-gray-500',
}

interface Props {
  rangeId: string
  workloads: Workload[]
  apps: RangeApp[]
  onRefresh: () => void
  onOpenConsole: (workload: string) => void
  onOpenApp: (app: RangeApp) => void
}

export function WorkloadGrid({ rangeId, workloads, apps, onRefresh, onOpenConsole, onOpenApp }: Props) {
  if (!workloads.length && !apps.length) {
    return (
      <div className="border rounded-lg bg-white p-8 text-center">
        <Monitor className="w-8 h-8 text-gray-400 mx-auto mb-2" />
        <p className="text-gray-700">This range declares no machines.</p>
        <p className="text-sm text-gray-500 mt-1">
          Its blueprint has no workloads — there is nothing to run or watch here.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {apps.length > 0 && (
        <div className="border rounded-lg bg-white px-4 py-3 flex flex-wrap items-center gap-3">
          <span className="text-sm font-medium text-gray-700">Applications</span>
          {/* Show the backend's reason rather than inventing one; it distinguishes no
              applications host, no ingress class, and a range that is not running. */}
          {apps.map((app) => (
            <button
              key={app.name}
              type="button"
              onClick={() => onOpenApp(app)}
              disabled={!app.published}
              title={app.published ? `Open ${app.name}` : (app.reason ?? `Open ${app.name}`)}
              className="inline-flex items-center gap-1 px-3 py-1.5 text-sm border rounded-md hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <ExternalLink className="w-4 h-4" />
              {app.name}
            </button>
          ))}
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {workloads.map((w) => (
          <div key={w.name} className="border rounded-lg bg-white p-4 flex flex-col gap-3">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="font-medium text-gray-900 truncate">{w.name}</div>
                <div className="text-sm text-gray-500 truncate">
                  {w.os_family} {w.os_version}
                </div>
              </div>
              <span className={`px-2 py-0.5 rounded-full text-xs whitespace-nowrap ${STATUS_STYLE[w.status] ?? 'bg-gray-100 text-gray-700'}`}>
                {w.status}
              </span>
            </div>

            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-gray-500">
              <span className="inline-flex items-center gap-1">
                <Cpu className="w-3.5 h-3.5" />
                {w.cpus}
              </span>
              <span className="inline-flex items-center gap-1">
                <MemoryStick className="w-3.5 h-3.5" />
                {w.memory_mb} MB
              </span>
              {Object.entries(w.addresses)
                .filter(([iface]) => iface !== 'default')
                .map(([iface, ip]) => (
                  <span key={iface} className="font-mono">
                    {iface} {ip}
                  </span>
                ))}
            </div>

            <button
              type="button"
              onClick={() => onOpenConsole(w.name)}
              disabled={!w.console_available}
              title={w.console_available ? `Open the console for ${w.name}` : `${w.name} is ${w.status.toLowerCase()}`}
              className="mt-auto inline-flex items-center justify-center gap-1 px-3 py-1.5 text-sm rounded-md bg-primary-600 text-white hover:bg-primary-700 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <Monitor className="w-4 h-4" />
              Console
            </button>
          </div>
        ))}
      </div>

      <button
        type="button"
        onClick={onRefresh}
        className="inline-flex items-center gap-1 text-sm text-gray-600 hover:text-gray-900"
        data-range={rangeId}
      >
        <RefreshCw className="w-4 h-4" />
        Refresh
      </button>
    </div>
  )
}
