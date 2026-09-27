/**
 * Which machines the assigned learner may open a console for.
 *
 * The panel used to disappear on every Kubernetes range: it listed VM rows, Era B creates none,
 * and it returned null when the list was empty. An instructor looking for somewhere to hide the
 * red-team box found nothing and concluded there was nothing to hide. The backend now answers
 * with the range's machines whichever substrate holds them, and an empty answer is drawn as an
 * empty answer rather than as an absent control.
 */
import { useState, useEffect, useCallback } from 'react'
import { Monitor, Eye, EyeOff, RefreshCw, Save } from 'lucide-react'
import { isAxiosError } from 'axios'
import { rangesApi, RangeVMVisibilityResponse } from '../../services/api'
import { toast } from '../../stores/toastStore'
import { useSubstrate } from '../../stores/capabilitiesStore'

interface RangeVMVisibilityControlProps {
  rangeId: string
  canManage: boolean
  onUpdate?: () => void
}

/** Whatever the server said went wrong, or a fallback that at least names the operation. */
function reason(err: unknown, fallback: string): string {
  if (isAxiosError(err)) {
    const detail = err.response?.data?.detail
    if (typeof detail === 'string' && detail) return detail
    if (!err.response) return `${fallback} — no answer from the server`
  }
  return fallback
}

export function RangeVMVisibilityControl({
  rangeId,
  canManage,
  onUpdate,
}: RangeVMVisibilityControlProps) {
  const { isKubernetes } = useSubstrate()
  const machine = isKubernetes ? 'machine' : 'VM'
  const machines = isKubernetes ? 'machines' : 'VMs'

  const [visibility, setVisibility] = useState<RangeVMVisibilityResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Track pending changes separately from server state
  const [pendingHiddenIds, setPendingHiddenIds] = useState<string[]>([])
  const [originalHiddenIds, setOriginalHiddenIds] = useState<string[]>([])

  // Check if there are unsaved changes
  const hasChanges =
    JSON.stringify([...pendingHiddenIds].sort()) !== JSON.stringify([...originalHiddenIds].sort())

  const loadVisibility = useCallback(async () => {
    setLoading(true)
    try {
      const response = await rangesApi.getVMVisibility(rangeId)
      setVisibility(response.data)
      setPendingHiddenIds(response.data.hidden_vm_ids || [])
      setOriginalHiddenIds(response.data.hidden_vm_ids || [])
      setError(null)
    } catch (err: unknown) {
      const message = reason(err, 'Could not load console visibility')
      setError(message)
      toast.error(message)
    } finally {
      setLoading(false)
    }
  }, [rangeId])

  useEffect(() => {
    loadVisibility()
  }, [loadVisibility])

  const handleToggle = (id: string) => {
    if (!canManage) return

    if (pendingHiddenIds.includes(id)) {
      setPendingHiddenIds(pendingHiddenIds.filter((each) => each !== id))
    } else {
      setPendingHiddenIds([...pendingHiddenIds, id])
    }
  }

  const handleSave = async () => {
    if (!visibility || !canManage || !hasChanges) return

    setSaving(true)
    try {
      const response = await rangesApi.updateVMVisibility(rangeId, pendingHiddenIds)
      // The server's answer, not the optimistic one: it is what decides which machines are
      // hidden, and a panel that shows the request rather than the result is how a control
      // comes to report a success it never had.
      setVisibility(response.data)
      setPendingHiddenIds(response.data.hidden_vm_ids || [])
      setOriginalHiddenIds(response.data.hidden_vm_ids || [])
      setError(null)
      toast.success('Console visibility saved')
      onUpdate?.()
    } catch (err: unknown) {
      const message = reason(err, 'Could not save console visibility')
      setError(message)
      toast.error(message)
    } finally {
      setSaving(false)
    }
  }

  const handleDiscard = () => {
    setPendingHiddenIds(originalHiddenIds)
  }

  const handleShowAll = () => {
    if (!visibility || !canManage) return
    setPendingHiddenIds([])
  }

  const handleHideAll = () => {
    if (!visibility || !canManage) return
    setPendingHiddenIds(visibility.vms.map((each) => each.id))
  }

  // On Docker a range with no VMs has nothing to say here and the panel stays out of the way,
  // as it always has. On Kubernetes the empty answer is itself the finding, so it is drawn.
  if (!isKubernetes && visibility && visibility.vms.length === 0) {
    return null
  }

  const total = visibility?.vms.length ?? 0
  const hiddenCount = pendingHiddenIds.length

  return (
    <div className="bg-white shadow rounded-lg p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-medium text-gray-900 flex items-center gap-2">
          <Monitor className="h-4 w-4" />
          Console visibility
        </h3>
        {visibility && (
          <button
            onClick={loadVisibility}
            disabled={loading || hasChanges}
            className="text-gray-400 hover:text-gray-600 disabled:opacity-50"
            title={hasChanges ? 'Save or discard changes first' : 'Refresh'}
          >
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        )}
      </div>

      <p className="text-xs text-gray-500 mb-4">
        Which {machines} the assigned learner can see and open a console for. An unchecked{' '}
        {machine} is hidden from them.
      </p>

      {loading && (
        <div className="text-center py-8 text-gray-500">
          <RefreshCw className="h-6 w-6 animate-spin mx-auto mb-2" />
          <p className="text-sm">Loading {machines}…</p>
        </div>
      )}

      {error && !loading && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </div>
      )}

      {visibility && !loading && total === 0 && (
        <p className="text-sm text-gray-600 py-4">
          This range declares no {machines}, so there is nothing to hide. Deploy it from a
          blueprint that declares {machines} and they will appear here.
        </p>
      )}

      {visibility && !loading && total > 0 && (
        <>
          <div className="border border-gray-200 rounded-md max-h-64 overflow-y-auto">
            {visibility.vms.map((each) => {
              const isHidden = pendingHiddenIds.includes(each.id)
              const wasHidden = originalHiddenIds.includes(each.id)
              const isChanged = isHidden !== wasHidden
              const state = (each.status || '').toLowerCase()
              return (
                <label
                  key={each.id}
                  className={`flex items-center px-3 py-2 border-b border-gray-100 last:border-b-0 cursor-pointer ${
                    isChanged ? 'bg-yellow-50' : 'hover:bg-gray-50'
                  } ${!canManage ? 'opacity-75 cursor-not-allowed' : ''}`}
                >
                  <input
                    type="checkbox"
                    checked={!isHidden}
                    onChange={() => handleToggle(each.id)}
                    disabled={!canManage || saving}
                    className="h-4 w-4 text-primary-600 focus:ring-primary-500 border-gray-300 rounded"
                  />
                  <span className="ml-3 flex-1">
                    <span className={`text-sm ${isHidden ? 'text-gray-400' : 'text-gray-900'}`}>
                      {each.hostname}
                    </span>
                    <span
                      className={`ml-2 text-xs ${
                        state === 'running'
                          ? 'text-green-600'
                          : state === 'stopped'
                            ? 'text-gray-500'
                            : 'text-yellow-600'
                      }`}
                    >
                      ({each.status})
                    </span>
                    {isChanged && (
                      <span className="ml-2 text-xs text-yellow-600 font-medium">(unsaved)</span>
                    )}
                  </span>
                  {isHidden ? (
                    <EyeOff className="h-4 w-4 text-gray-400" />
                  ) : (
                    <Eye className="h-4 w-4 text-green-500" />
                  )}
                </label>
              )
            })}
          </div>

          {canManage && (
            <div className="mt-4 flex flex-wrap gap-2">
              {hasChanges && (
                <>
                  <button
                    onClick={handleSave}
                    disabled={saving}
                    className="inline-flex items-center px-3 py-1.5 text-xs font-medium rounded-md text-white bg-primary-600 hover:bg-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <Save className="h-3 w-3 mr-1" />
                    {saving ? 'Saving…' : 'Save changes'}
                  </button>
                  <button
                    onClick={handleDiscard}
                    disabled={saving}
                    className="inline-flex items-center px-3 py-1.5 text-xs font-medium rounded-md text-gray-700 bg-gray-100 hover:bg-gray-200 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Discard
                  </button>
                </>
              )}

              <button
                onClick={handleShowAll}
                disabled={saving || hiddenCount === 0}
                className="inline-flex items-center px-3 py-1.5 text-xs font-medium rounded-md text-gray-700 bg-gray-100 hover:bg-gray-200 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <Eye className="h-3 w-3 mr-1" />
                Show all
              </button>
              <button
                onClick={handleHideAll}
                disabled={saving || hiddenCount === total}
                className="inline-flex items-center px-3 py-1.5 text-xs font-medium rounded-md text-gray-700 bg-gray-100 hover:bg-gray-200 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <EyeOff className="h-3 w-3 mr-1" />
                Hide all
              </button>
            </div>
          )}

          <div className="mt-4 text-xs text-gray-500">
            {hiddenCount === 0 ? (
              <span className="text-green-600">
                All {total} {machines} visible
              </span>
            ) : hiddenCount === total ? (
              <span className="text-red-600">All {machines} hidden</span>
            ) : (
              <span>
                {total - hiddenCount} of {total} {machines} visible
              </span>
            )}
            {hasChanges && <span className="ml-2 text-yellow-600">(unsaved changes)</span>}
          </div>
        </>
      )}
    </div>
  )
}
