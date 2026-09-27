// frontend/src/components/admin/RegistryTab.tsx
import { useEffect, useState, useCallback } from 'react'
import { isAxiosError } from 'axios'
import {
  registryApi,
  cacheApi,
  type RegistryImage,
  type RegistryStats,
} from '../../services/api'
import type { CachedImage } from '../../types'
import { toast } from '../../stores/toastStore'
import { useCapabilitiesStore } from '../../stores/capabilitiesStore'
import { featureGate } from '../../lib/featureGate'
import { ConfirmDialog } from '../common/ConfirmDialog'
import {
  Loader2,
  RefreshCw,
  Upload,
  Box,
  CheckCircle2,
  XCircle,
  HelpCircle,
  Server,
  Tag,
  Database,
  Trash2,
  UploadCloud,
} from 'lucide-react'
import clsx from 'clsx'

/**
 * Why a panel has nothing to show.
 *
 * `substrate` is the distinction this tab was getting wrong. Every route behind it reads the host
 * Docker daemon and the registry container beside it, so on an install that has neither they
 * refuse by design. That is a fact about the install, not a fault, and dressing it as one sent an
 * operator looking for a broken registry that was never meant to exist.
 */
export type LoadFailure = { kind: 'substrate' | 'error'; message: string }

/**
 * The `detail` FastAPI puts on a refusal, when it carries one a person can read.
 *
 * Only a non-empty string is accepted. A 422 answers with a list of objects, and rendering that
 * straight into JSX throws in React rather than telling anyone anything.
 */
export function responseDetail(reason: unknown): string | null {
  if (!isAxiosError(reason)) return null
  const body = reason.response?.data as { detail?: unknown } | undefined
  return typeof body?.detail === 'string' && body.detail.trim() !== '' ? body.detail : null
}

/**
 * What to tell an operator about one failed request.
 *
 * The server's own `detail` comes first because it is the only text that knows which route failed
 * and why -- the substrate guard names the substrate in its 501. Axios contributes "Request
 * failed with status code 500", which is not an explanation of anything.
 *
 * Only 501 means "this install does not have one". The app-level Docker handler answers 501 just
 * when the install is Kubernetes and 503 when a Docker host has lost its daemon, so the two cases
 * stay apart. A 404 must not join them, because any one of the three failures here replaces the
 * whole tab: a single 404 would tell a Docker operator that their host has no local registry and
 * that a cluster pulls range images instead, while the registry container sits running beside it.
 */
export function describeFailure(reason: unknown, label: string): LoadFailure {
  const detail = responseDetail(reason)
  const status = isAxiosError(reason) ? reason.response?.status : undefined

  if (status === 501) {
    return { kind: 'substrate', message: detail ?? `${label} is not available on this install.` }
  }
  if (detail) return { kind: 'error', message: detail }
  if (isAxiosError(reason) && !reason.response) {
    return { kind: 'error', message: `${label} could not be read: the API did not answer.` }
  }
  if (status) return { kind: 'error', message: `${label} could not be read (HTTP ${status}).` }
  return { kind: 'error', message: `${label} could not be read.` }
}

/** A tag a registry push can actually resolve to a repository. */
export function pushableTag(image: CachedImage): string | null {
  const tag = (image.tags ?? []).find((t) => t && !t.startsWith('<none>'))
  return tag ?? null
}

export default function RegistryTab() {
  const features = useCapabilitiesStore((s) => s.features)
  const substrateLabel = useCapabilitiesStore((s) => s.substrateLabel)
  const gate = featureGate(features, 'docker_registry')

  // Stats state
  const [stats, setStats] = useState<RegistryStats | null>(null)
  const [statsFailure, setStatsFailure] = useState<LoadFailure | null>(null)
  const [statsLoading, setStatsLoading] = useState(true)

  // Images list state
  const [images, setImages] = useState<RegistryImage[]>([])
  const [imagesFailure, setImagesFailure] = useState<LoadFailure | null>(null)
  const [imagesLoading, setImagesLoading] = useState(true)

  // Host images for push dropdown
  const [hostImages, setHostImages] = useState<CachedImage[]>([])
  const [hostImagesFailure, setHostImagesFailure] = useState<LoadFailure | null>(null)
  const [hostImagesLoading, setHostImagesLoading] = useState(true)

  // Push state
  const [selectedImage, setSelectedImage] = useState<string>('')
  const [pushing, setPushing] = useState(false)

  // Delete state
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<string | null>(null)

  // Bulk push state
  const [pushingAll, setPushingAll] = useState(false)
  const [pushProgress, setPushProgress] = useState<{ current: number; total: number } | null>(null)

  // Refresh state
  const [refreshing, setRefreshing] = useState(false)

  const fetchStats = useCallback(async () => {
    try {
      const res = await registryApi.getStats()
      setStats(res.data)
      setStatsFailure(null)
    } catch (err: unknown) {
      // A failed request is not an unhealthy registry. Reporting `healthy: false` here is how the
      // tab came to say "Unhealthy" about a registry it had never managed to ask.
      setStats(null)
      setStatsFailure(describeFailure(err, 'Registry status'))
    }
  }, [])

  const fetchImages = useCallback(async () => {
    try {
      const res = await registryApi.listImages()
      setImages(res.data)
      setImagesFailure(null)
    } catch (err: unknown) {
      setImages([])
      setImagesFailure(describeFailure(err, 'The registry contents'))
    }
  }, [])

  const fetchHostImages = useCallback(async () => {
    try {
      const res = await cacheApi.listImages()
      setHostImages(res.data)
      setHostImagesFailure(null)
    } catch (err: unknown) {
      // Stated next to the control it disables rather than thrown as a toast: a toast on page
      // load is gone by the time the operator looks at the empty dropdown it was explaining.
      setHostImages([])
      setHostImagesFailure(describeFailure(err, "This host's cached images"))
    }
  }, [])

  const fetchAll = useCallback(async (showLoading = true) => {
    if (showLoading) {
      setStatsLoading(true)
      setImagesLoading(true)
      setHostImagesLoading(true)
    }
    setRefreshing(true)

    try {
      await Promise.all([
        fetchStats(),
        fetchImages(),
        fetchHostImages(),
      ])
    } finally {
      setStatsLoading(false)
      setImagesLoading(false)
      setHostImagesLoading(false)
      setRefreshing(false)
    }
  }, [fetchStats, fetchImages, fetchHostImages])

  useEffect(() => {
    // Only ask an install that has a registry. Admin gates the tab as well; this is the second
    // line, for a render that reaches the component some other way.
    if (gate !== 'allow') return
    fetchAll()
  }, [gate, fetchAll])

  const handlePush = async () => {
    if (!selectedImage) {
      toast.warning('Select an image to push')
      return
    }

    setPushing(true)
    try {
      const res = await registryApi.pushImage(selectedImage)
      if (res.data.operation_id) {
        toast.success(res.data.message || `Push started for ${selectedImage}`)
        setSelectedImage('')
        // Refresh the registry images list
        await fetchAll(false)
      } else {
        toast.error(res.data.message || 'The push could not be started.')
      }
    } catch (err: unknown) {
      toast.error(responseDetail(err) ?? 'The push could not be started.')
    } finally {
      setPushing(false)
    }
  }

  const handleRefresh = () => {
    fetchAll(false)
  }

  const handleDeleteImage = async () => {
    const imageTag = deleteTarget
    if (!imageTag) return
    setDeleting(imageTag)
    try {
      const res = await registryApi.deleteImage(imageTag)
      if (res.data.success) {
        toast.success(res.data.message || `Deleted ${imageTag} from the registry`)
        setDeleteTarget(null)
        await fetchAll(false)
      } else {
        toast.error(res.data.message || `${imageTag} could not be deleted.`)
        setDeleteTarget(null)
      }
    } catch (err: unknown) {
      toast.error(responseDetail(err) ?? `${imageTag} could not be deleted.`)
      setDeleteTarget(null)
    } finally {
      setDeleting(null)
    }
  }

  // Only tagged images can be pushed: the server resolves the target repository from the tag, so
  // an untagged image offered here would fail on submit with nothing in the UI to explain it.
  const pushableImages = hostImages.filter((img) => pushableTag(img) !== null)

  const handlePushAllMissing = async () => {
    if (pushableImages.length === 0) {
      toast.warning('There are no tagged images on this host to push')
      return
    }

    setPushingAll(true)
    setPushProgress({ current: 0, total: 0 })

    try {
      // Check status of each host image to find ones that need pushing
      const imagesToPush: string[] = []

      for (const img of pushableImages) {
        const imageTag = pushableTag(img)
        if (!imageTag) continue

        try {
          const statusRes = await registryApi.getImageStatus(imageTag)
          if (statusRes.data.needs_push) {
            imagesToPush.push(imageTag)
          }
        } catch {
          // A status check that fails tells us nothing either way; skip rather than push blind.
          continue
        }
      }

      if (imagesToPush.length === 0) {
        toast.info('Every tagged image on this host is already in the registry')
        return
      }

      setPushProgress({ current: 0, total: imagesToPush.length })
      let successCount = 0
      let failCount = 0

      for (let i = 0; i < imagesToPush.length; i++) {
        const imageTag = imagesToPush[i]
        setPushProgress({ current: i + 1, total: imagesToPush.length })

        try {
          const res = await registryApi.pushImage(imageTag)
          if (res.data.operation_id) {
            successCount++
          } else {
            failCount++
          }
        } catch {
          failCount++
        }
      }

      if (failCount === 0) {
        toast.success(`Started ${successCount} push${successCount === 1 ? '' : 'es'} to the registry`)
      } else if (successCount > 0) {
        toast.warning(`Started ${successCount} push${successCount === 1 ? '' : 'es'}; ${failCount} could not be started`)
      } else {
        toast.error(`None of the ${failCount} pushes could be started`)
      }

      await fetchAll(false)
    } catch (err: unknown) {
      toast.error(responseDetail(err) ?? 'The pushes could not be started.')
    } finally {
      setPushingAll(false)
      setPushProgress(null)
    }
  }

  // Waiting on /system/capabilities: render nothing rather than a registry panel this install may
  // not have, which would then have to disappear.
  if (gate === 'pending') {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-8 w-8 animate-spin text-primary-600" />
      </div>
    )
  }

  // A refusal that names the reason, in place of a page of panels that can only fail. The gate is
  // the install's own answer; a 501 from any of the three requests is the same answer, later.
  const refusal: LoadFailure | null =
    gate === 'deny'
      ? {
          kind: 'substrate',
          message: `${substrateLabel ? `This ${substrateLabel} install` : 'This install'} has no local image registry.`,
        }
      : [statsFailure, imagesFailure, hostImagesFailure].find((f) => f?.kind === 'substrate') ?? null

  if (refusal) {
    return (
      <div className="bg-white rounded-lg shadow p-8 text-center">
        <Server className="mx-auto h-12 w-12 text-gray-400" />
        <h3 className="mt-4 text-lg font-medium text-gray-900">No image registry on this install</h3>
        <p className="mt-2 text-sm text-gray-600 max-w-xl mx-auto">{refusal.message}</p>
        <p className="mt-2 text-sm text-gray-500 max-w-xl mx-auto">
          Range images are pulled by the cluster from the blueprint that names them, so there is
          nothing here to push to or prune.
        </p>
      </div>
    )
  }

  const isLoading = statsLoading && imagesLoading && hostImagesLoading

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-8 w-8 animate-spin text-primary-600" />
        <span className="ml-2 text-gray-600">Reading the registry...</span>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-medium text-gray-900">Local Image Registry</h3>
          <p className="mt-1 text-sm text-gray-500">
            Images pushed here are served to the ranges running on this host, so a range deploys
            without reaching out to the internet.
          </p>
        </div>
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="inline-flex items-center px-3 py-1.5 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50"
        >
          <RefreshCw className={clsx('h-4 w-4 mr-2', refreshing && 'animate-spin')} />
          Refresh
        </button>
      </div>

      {/* Stats Card */}
      <div className="bg-white rounded-lg shadow p-6">
        <h4 className="text-sm font-medium text-gray-900 mb-4 flex items-center gap-2">
          <Server className="h-4 w-4" />
          Registry Status
        </h4>
        {statsFailure && (
          <p className="mb-4 text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-md p-3">
            {statsFailure.message}
          </p>
        )}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          {/* Health Status */}
          <div className="flex items-center p-4 bg-gray-50 rounded-lg">
            <div className={clsx(
              'flex-shrink-0 h-10 w-10 rounded-full flex items-center justify-center',
              !stats ? 'bg-gray-200' : stats.healthy ? 'bg-green-100' : 'bg-red-100'
            )}>
              {!stats ? (
                <HelpCircle className="h-5 w-5 text-gray-500" />
              ) : stats.healthy ? (
                <CheckCircle2 className="h-5 w-5 text-green-600" />
              ) : (
                <XCircle className="h-5 w-5 text-red-600" />
              )}
            </div>
            <div className="ml-4">
              <p className="text-sm font-medium text-gray-900">Health</p>
              <p className={clsx(
                'text-lg font-bold',
                !stats ? 'text-gray-500' : stats.healthy ? 'text-green-600' : 'text-red-600'
              )}>
                {!stats ? 'Unknown' : stats.healthy ? 'Healthy' : 'Unhealthy'}
              </p>
            </div>
          </div>

          {/* Total Images */}
          <div className="flex items-center p-4 bg-gray-50 rounded-lg">
            <div className="flex-shrink-0 h-10 w-10 rounded-full bg-blue-100 flex items-center justify-center">
              <Box className="h-5 w-5 text-blue-600" />
            </div>
            <div className="ml-4">
              <p className="text-sm font-medium text-gray-900">Images</p>
              <p className="text-lg font-bold text-gray-900">
                {stats ? stats.image_count : '-'}
              </p>
            </div>
          </div>

          {/* Total Tags */}
          <div className="flex items-center p-4 bg-gray-50 rounded-lg">
            <div className="flex-shrink-0 h-10 w-10 rounded-full bg-purple-100 flex items-center justify-center">
              <Tag className="h-5 w-5 text-purple-600" />
            </div>
            <div className="ml-4">
              <p className="text-sm font-medium text-gray-900">Tags</p>
              <p className="text-lg font-bold text-gray-900">
                {stats ? stats.tag_count : '-'}
              </p>
            </div>
          </div>
        </div>
      </div>

      {/* Manual Push Section */}
      <div className="bg-white rounded-lg shadow p-6">
        <h4 className="text-sm font-medium text-gray-900 mb-4 flex items-center gap-2">
          <Upload className="h-4 w-4" />
          Push an Image to the Registry
        </h4>
        <div className="flex items-end gap-4">
          <div className="flex-1">
            <label htmlFor="image-select" className="block text-sm font-medium text-gray-700 mb-1">
              Image cached on this host
            </label>
            <select
              id="image-select"
              value={selectedImage}
              onChange={(e) => setSelectedImage(e.target.value)}
              disabled={hostImagesLoading || pushing || pushableImages.length === 0}
              className="block w-full border border-gray-300 rounded-md shadow-sm px-3 py-2 focus:ring-primary-500 focus:border-primary-500 sm:text-sm disabled:bg-gray-100"
            >
              <option value="">-- Select an image --</option>
              {pushableImages.map((img) => {
                const tag = pushableTag(img)
                return tag ? (
                  <option key={img.id} value={tag}>
                    {tag}
                  </option>
                ) : null
              })}
            </select>
            {hostImagesFailure ? (
              <p className="mt-1 text-xs text-amber-700">{hostImagesFailure.message}</p>
            ) : (
              pushableImages.length === 0 && !hostImagesLoading && (
                <p className="mt-1 text-xs text-gray-500">
                  No tagged images are cached on this host. Pull one from the Image Cache page
                  first.
                </p>
              )
            )}
          </div>
          <button
            onClick={handlePush}
            disabled={!selectedImage || pushing}
            className="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-primary-600 hover:bg-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-primary-500 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {pushing ? (
              <>
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                Pushing...
              </>
            ) : (
              <>
                <Upload className="h-4 w-4 mr-2" />
                Push to Registry
              </>
            )}
          </button>
        </div>

        {/* Push All Missing Button */}
        <div className="mt-4 pt-4 border-t border-gray-200">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium text-gray-700">Bulk Push</p>
              <p className="text-xs text-gray-500">
                Push every tagged image on this host that is not yet in the registry.
              </p>
            </div>
            <button
              onClick={handlePushAllMissing}
              disabled={pushingAll || pushableImages.length === 0}
              className="inline-flex items-center px-4 py-2 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-primary-500 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {pushingAll ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  {pushProgress
                    ? `Pushing ${pushProgress.current}/${pushProgress.total}...`
                    : 'Checking...'}
                </>
              ) : (
                <>
                  <UploadCloud className="h-4 w-4 mr-2" />
                  Push All Missing
                </>
              )}
            </button>
          </div>
        </div>
      </div>

      {/* Registry Images Table */}
      <div className="bg-white rounded-lg shadow overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-200">
          <h4 className="text-sm font-medium text-gray-900 flex items-center gap-2">
            <Database className="h-4 w-4" />
            Registry Images
            <span className="ml-2 px-2 py-0.5 bg-gray-100 text-gray-600 rounded-full text-xs">
              {images.length}
            </span>
          </h4>
        </div>
        {imagesLoading ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="h-6 w-6 animate-spin text-gray-400" />
          </div>
        ) : imagesFailure ? (
          // An empty list and an unreadable registry look identical; saying "No Images in
          // Registry" about a request that failed sends an operator to push images that are
          // already there.
          <div className="text-center py-12">
            <XCircle className="mx-auto h-12 w-12 text-amber-400" />
            <h3 className="mt-4 text-sm font-medium text-gray-900">
              The registry contents could not be read
            </h3>
            <p className="mt-2 text-sm text-gray-600 max-w-lg mx-auto">{imagesFailure.message}</p>
            <button
              onClick={handleRefresh}
              disabled={refreshing}
              className="mt-4 inline-flex items-center px-3 py-1.5 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50"
            >
              <RefreshCw className={clsx('h-4 w-4 mr-2', refreshing && 'animate-spin')} />
              Try again
            </button>
          </div>
        ) : images.length === 0 ? (
          <div className="text-center py-12">
            <Box className="mx-auto h-12 w-12 text-gray-400" />
            <h3 className="mt-4 text-sm font-medium text-gray-900">No Images in Registry</h3>
            <p className="mt-2 text-sm text-gray-500">
              Push an image from this host to make it available to the ranges running here.
            </p>
          </div>
        ) : (
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Image Name
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Tags
                </th>
                <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-gray-200">
              {images.map((image) => (
                <tr key={image.name} className="hover:bg-gray-50">
                  <td className="px-6 py-4 whitespace-nowrap">
                    <div className="flex items-center">
                      <Box className="h-5 w-5 text-gray-400 mr-3" />
                      <span className="text-sm font-medium text-gray-900 font-mono">
                        {image.name}
                      </span>
                    </div>
                  </td>
                  <td className="px-6 py-4">
                    <div className="flex flex-wrap gap-1">
                      {image.tags.map((tag) => (
                        <span
                          key={tag}
                          className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-blue-100 text-blue-800"
                        >
                          {tag}
                        </span>
                      ))}
                      {image.tags.length === 0 && (
                        <span className="text-xs text-gray-400">No tags</span>
                      )}
                    </div>
                  </td>
                  <td className="px-6 py-4 text-right">
                    <div className="flex justify-end gap-2">
                      {image.tags.length > 0 ? (
                        image.tags.map((tag) => {
                          const imageTag = `${image.name}:${tag}`
                          const isDeleting = deleting === imageTag
                          return (
                            <button
                              key={tag}
                              onClick={() => setDeleteTarget(imageTag)}
                              disabled={isDeleting}
                              title={`Delete ${imageTag}`}
                              className="inline-flex items-center p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                              {isDeleting ? (
                                <Loader2 className="h-4 w-4 animate-spin" />
                              ) : (
                                <Trash2 className="h-4 w-4" />
                              )}
                            </button>
                          )
                        })
                      ) : (
                        <span className="text-xs text-gray-400">-</span>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <ConfirmDialog
        isOpen={deleteTarget !== null}
        title="Delete image from the registry"
        message={`Delete ${deleteTarget ?? ''} from the registry? A range that pulls it will fail until it is pushed again.`}
        confirmLabel="Delete"
        variant="danger"
        onConfirm={handleDeleteImage}
        onCancel={() => setDeleteTarget(null)}
        isLoading={deleting !== null}
      />
    </div>
  )
}
