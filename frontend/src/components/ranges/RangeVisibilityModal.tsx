import { useEffect, useState } from 'react'
import { Lock, Users, Globe, Loader2 } from 'lucide-react'
import Modal, { ModalBody, ModalFooter } from '../common/Modal'
import { rangesApi, usersApi } from '../../services/api'
import type { RangeVisibility, User } from '../../services/api'

interface Props {
  isOpen: boolean
  onClose: () => void
  rangeId: string
  rangeName: string
  /** Called after a successful save so the caller can refresh. */
  onSaved?: () => void
}

const OPTIONS: {
  value: RangeVisibility
  label: string
  help: string
  icon: typeof Lock
}[] = [
  {
    value: 'private',
    label: 'Private',
    help: 'Only you and administrators can see this range.',
    icon: Lock,
  },
  {
    value: 'shared',
    label: 'Shared',
    help: 'People you choose below, plus anyone holding a matching tag.',
    icon: Users,
  },
  {
    value: 'public',
    label: 'Public',
    help: 'Any signed-in user can see this range.',
    icon: Globe,
  },
]

/**
 * Who can see a range.
 *
 * Seeing is not operating: sharing a range shows it to someone, it does not let
 * them tear it down or open its consoles. Both of those stay with the owner and
 * administrators, so the copy here says "see" rather than "access".
 */
export function RangeVisibilityModal({ isOpen, onClose, rangeId, rangeName, onSaved }: Props) {
  const [visibility, setVisibility] = useState<RangeVisibility>('private')
  const [sharedWith, setSharedWith] = useState<string[]>([])
  const [users, setUsers] = useState<User[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!isOpen) return
    setLoading(true)
    setError(null)
    Promise.all([rangesApi.getVisibility(rangeId), usersApi.list().catch(() => ({ data: [] }))])
      .then(([vis, all]) => {
        setVisibility(vis.data.visibility)
        setSharedWith(vis.data.shared_with.map((s) => s.user_id))
        setUsers(all.data as User[])
      })
      .catch((e) => setError(e?.response?.data?.detail || 'Could not load visibility'))
      .finally(() => setLoading(false))
  }, [isOpen, rangeId])

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      // Only send the share list when it is meaningful. For private and public
      // it has no effect, and sending it would quietly discard grants the owner
      // may want back when switching to shared again.
      await rangesApi.setVisibility(
        rangeId,
        visibility,
        visibility === 'shared' ? sharedWith : undefined
      )
      onSaved?.()
      onClose()
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } } }
      setError(err.response?.data?.detail || 'Could not save visibility')
    } finally {
      setSaving(false)
    }
  }

  const toggle = (userId: string) =>
    setSharedWith((prev) =>
      prev.includes(userId) ? prev.filter((u) => u !== userId) : [...prev, userId]
    )

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Who can see this range" size="lg">
      <ModalBody>
        {loading ? (
          <div className="flex items-center gap-2 py-6 text-sm text-gray-600">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading...
          </div>
        ) : (
          <>
            <p className="mb-4 text-sm text-gray-600">
              <span className="font-medium text-gray-900">{rangeName}</span> — sharing shows the
              range to someone. It does not let them change it or open its consoles.
            </p>

            <div className="space-y-2">
              {OPTIONS.map((opt) => {
                const Icon = opt.icon
                const active = visibility === opt.value
                return (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => setVisibility(opt.value)}
                    className={`flex w-full items-start gap-3 rounded-lg border p-3 text-left ${
                      active ? 'border-blue-600 bg-blue-50' : 'border-gray-200 hover:bg-gray-50'
                    }`}
                  >
                    <Icon
                      className={`mt-0.5 h-4 w-4 shrink-0 ${
                        active ? 'text-blue-600' : 'text-gray-400'
                      }`}
                    />
                    <span>
                      <span className="block text-sm font-medium text-gray-900">{opt.label}</span>
                      <span className="block text-xs text-gray-600">{opt.help}</span>
                    </span>
                  </button>
                )
              })}
            </div>

            {visibility === 'shared' && (
              <div className="mt-4">
                <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">
                  Share with
                </p>
                {users.length === 0 ? (
                  <p className="text-sm text-gray-500">No other users to share with.</p>
                ) : (
                  <div className="max-h-48 space-y-1 overflow-y-auto rounded-lg border border-gray-200 p-2">
                    {users.map((u) => (
                      <label
                        key={u.id}
                        className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-sm hover:bg-gray-50"
                      >
                        <input
                          type="checkbox"
                          checked={sharedWith.includes(u.id)}
                          onChange={() => toggle(u.id)}
                          className="rounded border-gray-300"
                        />
                        <span className="text-gray-900">{u.username}</span>
                        <span className="text-xs text-gray-500">{u.email}</span>
                      </label>
                    ))}
                  </div>
                )}
              </div>
            )}

            {error && <p className="mt-3 text-sm text-red-600">{error}</p>}
          </>
        )}
      </ModalBody>
      <ModalFooter>
        <button
          onClick={onClose}
          disabled={saving}
          className="rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          onClick={save}
          disabled={saving || loading}
          className="ml-2 rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {saving ? 'Saving...' : 'Save'}
        </button>
      </ModalFooter>
    </Modal>
  )
}

export default RangeVisibilityModal
