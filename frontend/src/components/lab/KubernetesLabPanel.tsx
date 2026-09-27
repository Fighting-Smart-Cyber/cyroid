/**
 * The student's machines and applications, on the Kubernetes substrate.
 *
 * The lab's right-hand side used to be a VNC iframe fed by an Era A `VM` row. A Kubernetes range
 * has no such rows, so a learner opening their lab got an empty panel and no way to reach
 * anything. This is the same idea rebuilt on what that range actually has: KubeVirt workloads,
 * each with a console, and the range's web applications on the cluster ingress.
 *
 * Applications matter as much as machines here. The capability a learner is being trained on is
 * often a web application rather than a machine, and before this there was no route to it from
 * the lab at all.
 */
import { useCallback, useState } from 'react'
import { ExternalLink, Monitor, Loader2 } from 'lucide-react'
import { KubeVirtConsole } from '../console/KubeVirtConsole'
import type { RangeApp, Workload } from '../../hooks/useRangeWorkloads'

interface Props {
  rangeId: string
  workloads: Workload[]
  apps: RangeApp[]
  isLoading: boolean
}

export function KubernetesLabPanel({ rangeId, workloads, apps, isLoading }: Props) {
  const runnable = workloads.filter((w) => w.console_available)
  const [selected, setSelected] = useState<string | null>(null)
  const [appError, setAppError] = useState<string | null>(null)
  const active = selected ?? runnable[0]?.name ?? null

  const openApp = useCallback(
    async (app: RangeApp) => {
      setAppError(null)
      const token = localStorage.getItem('token') || ''
      try {
        const res = await fetch(
          `/api/v1/ranges/${rangeId}/apps/${encodeURIComponent(app.name)}/ticket`,
          { method: 'POST', headers: { Authorization: `Bearer ${token}` }, credentials: 'same-origin' }
        )
        if (!res.ok) {
          const body = (await res.json().catch(() => ({}))) as { detail?: string }
          throw new Error(body.detail || 'Could not open this application')
        }
        const { url } = (await res.json()) as { url: string }
        window.open(url, `app_${rangeId}_${app.name}`)
      } catch (err) {
        setAppError(err instanceof Error ? err.message : 'Could not open this application')
      }
    },
    [rangeId]
  )

  return (
    <div className="flex-1 h-full flex flex-col min-h-0 bg-gray-900">
      <div className="flex-1 min-h-0">
        {isLoading && !workloads.length ? (
          <div className="h-full flex items-center justify-center text-gray-400">
            <Loader2 className="w-6 h-6 animate-spin mr-2" />
            Starting your lab…
          </div>
        ) : active ? (
          <KubeVirtConsole key={active} rangeId={rangeId} workload={active} fullscreen />
        ) : (
          <div className="h-full flex items-center justify-center px-6">
            <div className="text-center max-w-sm">
              <Monitor className="w-10 h-10 text-gray-600 mx-auto mb-3" />
              <p className="text-gray-300">
                {workloads.length
                  ? 'Your machines are still starting.'
                  : 'This lab has no machines of its own.'}
              </p>
              <p className="text-gray-500 text-sm mt-1">
                {workloads.length
                  ? 'The console opens by itself once one is running.'
                  : apps.length
                    ? 'Use the application below to begin.'
                    : 'Ask your instructor — nothing has been provisioned yet.'}
              </p>
            </div>
          </div>
        )}
      </div>

      {(workloads.length > 0 || apps.length > 0) && (
        <div className="flex-shrink-0 border-t border-gray-700 bg-gray-800 px-3 py-2 flex flex-wrap items-center gap-2">
          {workloads.map((w) => {
            const isActive = w.name === active
            return (
              <button
                key={w.name}
                type="button"
                onClick={() => setSelected(w.name)}
                disabled={!w.console_available}
                title={
                  w.console_available
                    ? `Open the console for ${w.name}`
                    : `${w.name} is ${w.status.toLowerCase()}`
                }
                className={`inline-flex items-center gap-2 px-3 py-1.5 rounded text-sm border transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
                  isActive
                    ? 'bg-blue-600 border-blue-500 text-white'
                    : 'bg-gray-900 border-gray-700 text-gray-200 hover:bg-gray-700'
                }`}
              >
                <Monitor className="w-4 h-4" />
                {w.name}
                <span className={`text-xs ${isActive ? 'text-blue-100' : 'text-gray-400'}`}>
                  {w.status}
                </span>
              </button>
            )
          })}

          {apps.length > 0 && <span className="mx-1 h-5 w-px bg-gray-700" aria-hidden />}

          {/* The title shows the backend's own reason. Substituting one here asserted "the lab
              is not running" whatever the cause -- including the common one on a default
              install, which is that no applications host is configured at all. */}
          {apps.map((app) => (
            <button
              key={app.name}
              type="button"
              onClick={() => void openApp(app)}
              disabled={!app.published}
              title={app.published ? `Open ${app.name}` : (app.reason ?? `Open ${app.name}`)}
              className="inline-flex items-center gap-2 px-3 py-1.5 rounded text-sm border border-gray-700 bg-gray-900 text-gray-200 hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <ExternalLink className="w-4 h-4" />
              {app.name}
            </button>
          ))}

          {appError && <span className="text-sm text-red-400 ml-2">{appError}</span>}
        </div>
      )}
    </div>
  )
}
