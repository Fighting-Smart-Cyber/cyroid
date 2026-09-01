import { useState } from 'react'
import { HardDriveDownload, Loader2, AlertCircle, AlertTriangle } from 'lucide-react'
import { Modal, ModalBody, ModalFooter } from '../common/Modal'
import { cacheApi } from '../../services/api'
import { toast } from '../../stores/toastStore'

interface Props {
  rangeId: string
  vmId: string
  hostname: string
  osType?: string
  /** Used only to warn about crash-consistent captures -- see below. */
  vmStatus?: string
  isOpen: boolean
  onClose: () => void
  onSuccess: () => void
}

/**
 * Capture a bootable golden image from a VM inside a range.
 *
 * This is deliberately NOT the snapshot action. A snapshot is `docker commit`,
 * which excludes volume contents by design -- and a dockur Windows VM keeps its
 * disk in a /storage VOLUME. Committing one yields an image that carries the
 * dockur runtime and an empty disk, and reports success while doing it. This
 * copies /storage itself, so the installed OS actually comes along.
 */
export function CaptureGoldenImageModal({
  rangeId,
  vmId,
  hostname,
  osType,
  vmStatus,
  isOpen,
  onClose,
  onSuccess,
}: Props) {
  const defaultName = `${hostname}-${new Date().toISOString().split('T')[0]}`

  const [name, setName] = useState(defaultName)
  const [description, setDescription] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Capturing a running guest copies its disk mid-write: the image boots as if
  // the machine had lost power. Windows usually recovers, but every range
  // cloned from it inherits that state, so it is worth stopping first.
  const isRunning = vmStatus === 'running'

  const handleCapture = async () => {
    if (!name.trim()) {
      setError('Image name is required')
      return
    }

    setLoading(true)
    setError(null)

    try {
      const res = await cacheApi.captureRangeGoldenImage(rangeId, {
        vm_id: vmId,
        name: name.trim(),
        description: description.trim() || undefined,
        os_type: osType || 'windows',
      })

      toast.success(`Golden image "${res.data.name}" captured (${res.data.size_gb} GB)`)
      onSuccess()
      onClose()
    } catch (err) {
      // Narrowed rather than `any`: frontend:lint ratchets the
      // no-explicit-any count and it may fall, never rise.
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      const message = detail || 'Failed to capture golden image'
      setError(message)
      toast.error(message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Capture Golden Image"
      description={`Capture a reusable, bootable image from ${hostname}`}
      size="md"
    >
      <ModalBody className="space-y-4">
        <p className="text-sm text-gray-600">
          Capture the disk of <span className="font-medium text-gray-900">{hostname}</span> as a
          golden image. New VMs can then boot from it in minutes instead of running a full install.
        </p>

        {error && (
          <div className="flex items-center gap-2 text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg">
            <AlertCircle className="w-4 h-4 flex-shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {isRunning && (
          <div className="flex items-start gap-2 text-sm text-amber-800 bg-amber-50 border border-amber-200 px-3 py-2 rounded-lg">
            <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
            <span>
              This VM is running. Capturing now copies the disk mid-write, so the image boots as if
              the machine had lost power &mdash; and every range cloned from it inherits that state.
              Stop the VM first for a clean image.
            </span>
          </div>
        )}

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Image Name <span className="text-red-500">*</span>
          </label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g., win11-baseline-2026-08-27"
            className="w-full px-3 py-2 border border-gray-300 rounded-lg shadow-sm focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
            disabled={loading}
            autoFocus
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Description <span className="text-gray-400">(optional)</span>
          </label>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="e.g., Windows 11 with updates applied, ready for lab exercises"
            rows={3}
            className="w-full px-3 py-2 border border-gray-300 rounded-lg shadow-sm focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 resize-none"
            disabled={loading}
          />
        </div>

        <div className="bg-blue-50 border border-blue-200 rounded-lg px-4 py-3">
          <p className="text-sm text-blue-800">
            Copying a multi-gigabyte disk takes a while and the page will wait for it. If the
            connection drops the capture still finishes on the server &mdash; check{' '}
            <span className="font-medium">VM Library &rarr; Golden Images</span> before retrying, so
            you don't capture twice.
          </p>
        </div>
      </ModalBody>

      <ModalFooter>
        <button
          onClick={onClose}
          disabled={loading}
          className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          onClick={handleCapture}
          disabled={loading || !name.trim()}
          className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-indigo-600 rounded-lg hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {loading ? (
            <>
              <Loader2 className="w-4 h-4 animate-spin" />
              Capturing...
            </>
          ) : (
            <>
              <HardDriveDownload className="w-4 h-4" />
              Capture Image
            </>
          )}
        </button>
      </ModalFooter>
    </Modal>
  )
}
