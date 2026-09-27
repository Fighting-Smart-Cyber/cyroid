// frontend/src/pages/Ranges.tsx
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { rangesApi, RangeCreate } from '../services/api'
import type { Range } from '../types'
import { Plus, Loader2, Network, X, Play, Square, Trash2, Wand2, LayoutTemplate, AlertTriangle, RefreshCw } from 'lucide-react'
import clsx from 'clsx'
// ImportRangeWizard removed - use Blueprint Import instead (Issue #131)
import { ConfirmDialog } from '../components/common/ConfirmDialog'
import { toast } from '../stores/toastStore'
import { useCapabilitiesStore, useFeature } from '../stores/capabilitiesStore'
import { apiErrorDetail } from '../lib/apiError'
import { failureReasonForStatus } from '../lib/failureText'
import { machineNoun, pluralise } from '../lib/blueprints'

const statusColors: Record<string, string> = {
  draft: 'bg-gray-100 text-gray-800',
  deploying: 'bg-yellow-100 text-yellow-800',
  running: 'bg-green-100 text-green-800',
  stopped: 'bg-gray-100 text-gray-800',
  archived: 'bg-blue-100 text-blue-800',
  error: 'bg-red-100 text-red-800'
}

// A status the backend has and this table does not would otherwise reach clsx as undefined and
// paint an unstyled badge -- grey-on-white text with no pill, which reads as a render that broke
// rather than as a state nobody has taught the page about yet.
const UNKNOWN_STATUS_COLOR = 'bg-gray-100 text-gray-800'

/**
 * Why a range is in the state it is in, when it recorded one.
 *
 * The status badge alone says a range failed and not one word about why, although the reason
 * travelled in the same payload: a user reading "error" had to open the range, and on the
 * Kubernetes path the reason names the step that failed and whether anything is still in the
 * cluster. Clamped to two lines because a recorded reason can run to a thousand characters and
 * this is one row of a list; the whole of it is in the title attribute and on the range's page.
 *
 * Shown only against a status the reason still belongs to: the stored field outlives the failure
 * on the Docker path, so a reason printed under whatever badge the row carries turns up on draft
 * and running ranges that are fine.
 */
function FailureReason({
  status,
  message,
}: {
  status: string | null | undefined
  message: string | null | undefined
}) {
  const reason = failureReasonForStatus(status, message)
  if (!reason) return null
  return (
    <p className="mt-1 text-sm text-red-600 line-clamp-2" title={reason}>
      {reason}
    </p>
  )
}

export default function Ranges() {
  const [ranges, setRanges] = useState<Range[]>([])
  const [loading, setLoading] = useState(true)
  const [showModal, setShowModal] = useState(false)
  const [formData, setFormData] = useState<RangeCreate>({ name: '', description: '' })
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [deleteConfirm, setDeleteConfirm] = useState<{
    range: Range | null
    isLoading: boolean
  }>({ range: null, isLoading: false })

  // Assembling a range here from Network and VM rows is an Era A idea. On Kubernetes the range is
  // whatever its blueprint declares, so both entry points below would create something that
  // cannot be deployed -- and, before the wizard learned to clean up after itself, could not be
  // got rid of either.
  const canComposeRange = useFeature('range_composition')
  const substrateLabel = useCapabilitiesStore((state) => state.substrateLabel)

  const fetchRanges = async () => {
    try {
      const response = await rangesApi.list()
      setRanges(response.data)
      setLoadError(null)
    } catch (err) {
      console.error('Failed to fetch ranges:', err)
      // A listing that failed must not fall through to the empty state. "No ranges" and its
      // invitation to create one is a claim about the install, and after a failed request it is
      // a claim this page cannot make -- an operator whose token had expired was told their
      // ranges were gone.
      setLoadError(apiErrorDetail(err, 'The range list could not be loaded.'))
    } finally {
      setLoading(false)
    }
  }

  const retryFetchRanges = () => {
    setLoading(true)
    void fetchRanges()
  }

  useEffect(() => {
    fetchRanges()
  }, [])

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitting(true)
    setError(null)

    try {
      await rangesApi.create(formData)
      setShowModal(false)
      setFormData({ name: '', description: '' })
      fetchRanges()
    } catch (err) {
      setError(apiErrorDetail(err, 'The range could not be created. Check the name and try again.'))
    } finally {
      setSubmitting(false)
    }
  }

  const handleStart = async (range: Range) => {
    toast.info(`Starting "${range.name}"...`)
    try {
      await rangesApi.start(range.id)
      toast.success(`Range "${range.name}" started`)
      fetchRanges()
    } catch (err) {
      toast.error(apiErrorDetail(err, `"${range.name}" could not be started.`))
    }
  }

  const handleStop = async (range: Range) => {
    toast.info(`Stopping "${range.name}"...`)
    try {
      await rangesApi.stop(range.id)
      toast.success(`Range "${range.name}" stopped`)
      fetchRanges()
    } catch (err) {
      toast.error(apiErrorDetail(err, `"${range.name}" could not be stopped.`))
    }
  }

  const handleDelete = (range: Range) => {
    setDeleteConfirm({ range, isLoading: false })
  }

  const confirmDelete = async () => {
    if (!deleteConfirm.range) return
    const name = deleteConfirm.range.name
    setDeleteConfirm(prev => ({ ...prev, isLoading: true }))
    try {
      await rangesApi.delete(deleteConfirm.range.id)
      setDeleteConfirm({ range: null, isLoading: false })
      fetchRanges()
    } catch (err) {
      setDeleteConfirm({ range: null, isLoading: false })
      toast.error(apiErrorDetail(err, `"${name}" could not be deleted.`))
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-8 w-8 animate-spin text-primary-600" />
      </div>
    )
  }

  return (
    <div>
      <div className="sm:flex sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Cyber Ranges</h1>
          <p className="mt-2 text-sm text-gray-700">
            Create and manage your cyber training environments
          </p>
        </div>
        {canComposeRange ? (
          <div className="mt-4 sm:mt-0 flex space-x-3">
            <Link
              to="/ranges/new"
              className="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
            >
              <Wand2 className="h-4 w-4 mr-2" />
              Range Wizard
            </Link>
            {/* Import Range button removed - use Blueprint Import instead (Issue #131) */}
            <button
              onClick={() => setShowModal(true)}
              className="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-primary-600 hover:bg-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-primary-500"
            >
              <Plus className="h-4 w-4 mr-2" />
              New Range
            </button>
          </div>
        ) : (
          <div className="mt-4 sm:mt-0 flex space-x-3">
            <Link
              to="/blueprints"
              className="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-primary-600 hover:bg-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-primary-500"
            >
              <LayoutTemplate className="h-4 w-4 mr-2" />
              Deploy a Blueprint
            </Link>
          </div>
        )}
      </div>

      {loadError ? (
        <div role="alert" className="mt-8 rounded-md border border-red-200 bg-red-50 p-4">
          <div className="flex items-start gap-3">
            <AlertTriangle className="h-5 w-5 flex-shrink-0 text-red-400" aria-hidden="true" />
            <div className="flex-1">
              <h3 className="text-sm font-medium text-red-800">Your ranges could not be loaded</h3>
              <p className="mt-1 text-sm text-red-700">{loadError}</p>
            </div>
            <button
              onClick={retryFetchRanges}
              className="inline-flex flex-shrink-0 items-center px-3 py-1.5 border border-red-300 rounded-md text-sm font-medium text-red-700 bg-white hover:bg-red-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-red-500"
            >
              <RefreshCw className="h-4 w-4 mr-2" aria-hidden="true" />
              Try again
            </button>
          </div>
        </div>
      ) : ranges.length === 0 ? (
        <div className="mt-8 text-center">
          <Network className="mx-auto h-12 w-12 text-gray-400" />
          <h3 className="mt-2 text-sm font-medium text-gray-900">No ranges</h3>
          {canComposeRange ? (
            <>
              <p className="mt-1 text-sm text-gray-500">
                Get started quickly with a preset scenario or create a custom range from scratch.
              </p>
              <div className="mt-6 flex flex-col sm:flex-row items-center justify-center gap-3">
                <Link
                  to="/ranges/new"
                  className="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
                >
                  <Wand2 className="h-4 w-4 mr-2" />
                  Range Wizard
                </Link>
                <button
                  onClick={() => setShowModal(true)}
                  className="inline-flex items-center px-4 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-primary-500"
                >
                  <Plus className="h-4 w-4 mr-2" />
                  Empty Range
                </button>
              </div>
            </>
          ) : (
            <>
              <p className="mt-1 text-sm text-gray-500">
                On {substrateLabel ?? 'this substrate'} a range is deployed from a blueprint, which
                declares its networks, machines and capabilities together.
              </p>
              <div className="mt-6 flex justify-center">
                <Link
                  to="/blueprints"
                  className="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-primary-600 hover:bg-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-primary-500"
                >
                  <LayoutTemplate className="h-4 w-4 mr-2" />
                  Browse Blueprints
                </Link>
              </div>
            </>
          )}
        </div>
      ) : (
        <div className="mt-8 bg-white shadow overflow-hidden sm:rounded-md">
          <ul className="divide-y divide-gray-200">
            {ranges.map((range) => (
              <li key={range.id}>
                <div className="px-4 py-4 sm:px-6 flex items-center justify-between">
                  <Link to={`/ranges/${range.id}`} className="flex-1 min-w-0 hover:text-primary-600">
                    <div className="flex items-center">
                      <div className="flex-shrink-0">
                        <div className={clsx(
                          "rounded-md p-2",
                          range.status === 'running' ? 'bg-green-100' : 'bg-gray-100'
                        )}>
                          <Network className={clsx(
                            "h-6 w-6",
                            range.status === 'running' ? 'text-green-600' : 'text-gray-600'
                          )} />
                        </div>
                      </div>
                      <div className="ml-4 flex-1">
                        <div className="flex items-center">
                          <p className="text-sm font-medium text-gray-900 truncate">{range.name}</p>
                          <span className={clsx(
                            "ml-2 px-2 py-0.5 text-xs font-medium rounded-full",
                            statusColors[range.status.toLowerCase()] ?? UNKNOWN_STATUS_COLOR
                          )}>
                            {range.status}
                          </span>
                        </div>
                        <p className="mt-1 text-sm text-gray-500 truncate">
                          {range.description || 'No description'}
                        </p>
                        <FailureReason status={range.status} message={range.error_message} />
                        <div className="mt-1 flex items-center text-xs text-gray-400">
                          <span>{pluralise(range.network_count, 'network')}</span>
                          <span className="mx-2">•</span>
                          {/* Per range, not per install: a listing can hold both eras at once
                              while a Docker install is being migrated, and the count comes back
                              from the row rather than from the install. */}
                          <span>
                            {range.vm_count}{' '}
                            {machineNoun(null, range.substrate === 'kubernetes', range.vm_count)}
                          </span>
                        </div>
                      </div>
                    </div>
                  </Link>
                  <div className="ml-4 flex items-center space-x-2">
                    {range.status === 'stopped' || range.status === 'draft' ? (
                      <button
                        onClick={() => handleStart(range)}
                        className="p-2 text-gray-400 hover:text-green-600"
                        title="Start"
                      >
                        <Play className="h-5 w-5" />
                      </button>
                    ) : range.status === 'running' ? (
                      <button
                        onClick={() => handleStop(range)}
                        className="p-2 text-gray-400 hover:text-yellow-600"
                        title="Stop"
                      >
                        <Square className="h-5 w-5" />
                      </button>
                    ) : null}
                    <button
                      onClick={() => handleDelete(range)}
                      className="p-2 text-gray-400 hover:text-red-600"
                      title="Delete"
                    >
                      <Trash2 className="h-5 w-5" />
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Create Modal */}
      {showModal && (
        <div className="fixed inset-0 z-50 overflow-y-auto">
          <div className="flex items-center justify-center min-h-screen px-4">
            <div className="fixed inset-0 bg-gray-500 bg-opacity-75" onClick={() => setShowModal(false)} />

            <div className="relative bg-white rounded-lg shadow-xl max-w-md w-full">
              <div className="flex items-center justify-between p-4 border-b">
                <h3 className="text-lg font-medium text-gray-900">Create Range</h3>
                <button onClick={() => setShowModal(false)} className="text-gray-400 hover:text-gray-500">
                  <X className="h-5 w-5" />
                </button>
              </div>

              <form onSubmit={handleCreate} className="p-4 space-y-4">
                {error && (
                  <div className="p-3 bg-red-50 text-red-700 rounded-md text-sm">{error}</div>
                )}

                <div>
                  <label className="block text-sm font-medium text-gray-700">Name</label>
                  <input
                    type="text"
                    required
                    value={formData.name}
                    onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                    className="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-primary-500 focus:ring-primary-500 sm:text-sm"
                    placeholder="e.g., Active Directory Lab"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700">Description</label>
                  <textarea
                    rows={3}
                    value={formData.description || ''}
                    onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                    className="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-primary-500 focus:ring-primary-500 sm:text-sm"
                    placeholder="Describe the purpose of this range..."
                  />
                </div>

                <div className="flex justify-end space-x-3 pt-4">
                  <button
                    type="button"
                    onClick={() => setShowModal(false)}
                    className="px-4 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={submitting}
                    className="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-primary-600 hover:bg-primary-700 disabled:opacity-50"
                  >
                    {submitting && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
                    Create Range
                  </button>
                </div>
              </form>
            </div>
          </div>
        </div>
      )}

      {/* Delete Confirmation Dialog */}
      <ConfirmDialog
        isOpen={deleteConfirm.range !== null}
        title="Delete Range"
        message={`Are you sure you want to delete "${deleteConfirm.range?.name}"? This will permanently remove the range and all associated VMs and networks. This action cannot be undone.`}
        confirmLabel="Delete"
        variant="danger"
        onConfirm={confirmDelete}
        onCancel={() => setDeleteConfirm({ range: null, isLoading: false })}
        isLoading={deleteConfirm.isLoading}
      />
    </div>
  )
}
