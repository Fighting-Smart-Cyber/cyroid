import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import { vmsApi } from '../../services/api'
import { VmResourcesSection } from './VmResourcesSection'
import type { AllowedDevice } from '../../types'
import type { VM } from '../../types'

interface Props {
  vm: VM
  onClose: () => void
  /** Called after any change that alters server state, so the parent can refetch. */
  onChanged: () => void
}

interface Row {
  /** Stable identity for React keys — a row's key text is editable, so it
   *  cannot be used as the key without remounting the input on every keystroke
   *  and losing focus. */
  id: number
  key: string
  value: string
}

let nextRowId = 1
const toRows = (env: Record<string, string> | null): Row[] =>
  Object.entries(env ?? {}).map(([key, value]) => ({ id: nextRowId++, key, value }))

/**
 * Editor for a VM's container environment variables.
 *
 * The two-step Save -> Apply flow is deliberate. Docker bakes environment into
 * a container at create time and never re-reads it, so applying an edit means
 * destroying and recreating the container — which loses anything not on a
 * mounted volume. Doing that silently on Save would be able to wipe a student's
 * in-progress work mid-exercise, so recreation is always an explicit action.
 */
export function VmEnvironmentModal({ vm, onClose, onChanged }: Props) {
  const [rows, setRows] = useState<Row[]>(() => toRows(vm.environment))
  const [devices, setDevices] = useState<string[]>(() => vm.devices ?? [])
  const [allowedDevices, setAllowedDevices] = useState<AllowedDevice[]>([])
  const [saving, setSaving] = useState(false)
  const [applying, setApplying] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmApply, setConfirmApply] = useState(false)

  // Re-seed when the parent refetches and hands us a newer VM.
  useEffect(() => {
    setRows(toRows(vm.environment))
  }, [vm.environment])

  useEffect(() => {
    setDevices(vm.devices ?? [])
  }, [vm.devices])

  // The allow-list comes from the API rather than a local constant, so the UI
  // can never offer a device the server would reject.
  useEffect(() => {
    let cancelled = false
    vmsApi
      .getAllowedDevices()
      .then(r => { if (!cancelled) setAllowedDevices(r.data) })
      .catch(() => { if (!cancelled) setAllowedDevices([]) })
    return () => { cancelled = true }
  }, [])

  const pending = vm.environment_pending_keys ?? []
  // Either half being stale means the container needs recreating, so the
  // Apply affordance keys off both rather than env alone.
  const hasPending = pending.length > 0 || vm.devices_pending
  const busy = saving || applying

  const duplicateKeys = useMemo(() => {
    const seen = new Set<string>()
    const dupes = new Set<string>()
    for (const r of rows) {
      const k = r.key.trim()
      if (!k) continue
      if (seen.has(k)) dupes.add(k)
      seen.add(k)
    }
    return dupes
  }, [rows])

  // A blank key would silently drop the row on save; a duplicate would silently
  // win over its twin. Block both rather than letting the user think it saved.
  const invalid = duplicateKeys.size > 0 || rows.some(r => !r.key.trim() && r.value.trim())

  const setRow = (id: number, patch: Partial<Row>) =>
    setRows(rs => rs.map(r => (r.id === id ? { ...r, ...patch } : r)))

  const handleSave = async () => {
    setSaving(true)
    setError(null)
    try {
      const env: Record<string, string> = {}
      for (const r of rows) {
        const k = r.key.trim()
        if (k) env[k] = r.value
      }
      await vmsApi.updateVmConfig(vm.id, { environment: env, devices })
      onChanged()
    } catch (e: any) {
      setError(e.response?.data?.detail || 'Failed to save configuration')
    } finally {
      setSaving(false)
    }
  }

  const handleApply = async () => {
    setApplying(true)
    setError(null)
    try {
      await vmsApi.applyConfig(vm.id)
      setConfirmApply(false)
      onChanged()
    } catch (e: any) {
      setError(e.response?.data?.detail || 'Failed to apply configuration')
    } finally {
      setApplying(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto">
      <div className="flex min-h-screen items-center justify-center p-4">
        <div className="fixed inset-0 bg-gray-500 bg-opacity-75" onClick={busy ? undefined : onClose} />

        <div className="relative w-full max-w-2xl rounded-lg bg-white shadow-xl">
          <div className="flex items-center justify-between border-b px-6 py-4">
            <div>
              <h3 className="text-lg font-medium text-gray-900">Resources & Environment</h3>
              <p className="text-sm text-gray-500">{vm.hostname}</p>
            </div>
            <button onClick={onClose} disabled={busy} className="text-gray-400 hover:text-gray-600 disabled:opacity-50">
              <X className="h-5 w-5" />
            </button>
          </div>

          {pending.length > 0 && (
            <div className="mx-6 mt-4 rounded-md border border-amber-300 bg-amber-50 p-3">
              <div className="flex gap-2">
                <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
                <div className="text-sm">
                  <p className="font-medium text-amber-800">
                    {pending.length} pending change{pending.length === 1 ? '' : 's'} not yet live
                  </p>
                  <p className="mt-1 text-amber-700">
                    <span className="font-mono">{pending.join(', ')}</span>
                  </p>
                  <p className="mt-1 text-amber-700">
                    The running container still has the old values. Docker only reads environment
                    when a container is created, so restarting will not help — the container must
                    be recreated.
                  </p>
                </div>
              </div>
            </div>
          )}

          {error && (
            <div className="mx-6 mt-4 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-700">
              {error}
            </div>
          )}

          <VmResourcesSection vm={vm} onChanged={onChanged} />

          <div className="max-h-96 overflow-y-auto px-6 py-4">
            {rows.length === 0 && (
              <p className="py-6 text-center text-sm text-gray-500">
                No environment variables set. Add one below.
              </p>
            )}

            <div className="space-y-2">
              {rows.map(row => {
                const dupe = duplicateKeys.has(row.key.trim())
                const isPending = pending.includes(row.key.trim())
                return (
                  <div key={row.id} className="flex items-center gap-2">
                    <input
                      value={row.key}
                      onChange={e => setRow(row.id, { key: e.target.value })}
                      placeholder="VPN_SERVER"
                      spellCheck={false}
                      className={`w-2/5 rounded-md border px-3 py-2 font-mono text-sm ${
                        dupe ? 'border-red-400 bg-red-50' : isPending ? 'border-amber-400' : 'border-gray-300'
                      }`}
                    />
                    <input
                      value={row.value}
                      onChange={e => setRow(row.id, { value: e.target.value })}
                      placeholder="vpn.example.mil"
                      spellCheck={false}
                      className={`flex-1 rounded-md border px-3 py-2 font-mono text-sm ${
                        isPending ? 'border-amber-400' : 'border-gray-300'
                      }`}
                    />
                    <button
                      onClick={() => setRows(rs => rs.filter(r => r.id !== row.id))}
                      className="p-2 text-gray-400 hover:text-red-600"
                      title="Remove"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                )
              })}
            </div>

            {duplicateKeys.size > 0 && (
              <p className="mt-2 text-sm text-red-600">
                Duplicate key{duplicateKeys.size === 1 ? '' : 's'}:{' '}
                <span className="font-mono">{[...duplicateKeys].join(', ')}</span>
              </p>
            )}

            <button
              onClick={() => setRows(rs => [...rs, { id: nextRowId++, key: '', value: '' }])}
              className="mt-3 flex items-center gap-1 text-sm text-primary-600 hover:text-primary-700"
            >
              <Plus className="h-4 w-4" /> Add variable
            </button>

            {allowedDevices.length > 0 && (
              <div className="mt-6 border-t pt-4">
                <div className="flex items-center justify-between">
                  <h4 className="text-sm font-medium text-gray-900">Devices</h4>
                  {vm.devices_pending && (
                    <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800">
                      pending
                    </span>
                  )}
                </div>
                <p className="mt-1 text-xs text-gray-500">
                  Host devices mapped into this container. Like environment
                  variables, these are fixed when the container is created.
                </p>

                <div className="mt-3 space-y-2">
                  {allowedDevices.map(d => {
                    const checked = devices.includes(d.key)
                    return (
                      <label
                        key={d.key}
                        className="flex cursor-pointer items-start gap-2 rounded-md border border-gray-200 p-2 hover:bg-gray-50"
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={busy}
                          onChange={e =>
                            setDevices(ds =>
                              e.target.checked
                                ? [...ds, d.key].sort()
                                : ds.filter(x => x !== d.key)
                            )
                          }
                          className="mt-0.5 h-4 w-4 rounded border-gray-300 text-primary-600"
                        />
                        <span className="text-sm">
                          <span className="font-medium text-gray-900">{d.label}</span>
                          <code className="ml-2 text-xs text-gray-500">{d.path.split(':')[0]}</code>
                          <span className="block text-xs text-gray-500">{d.help}</span>
                        </span>
                      </label>
                    )
                  })}
                </div>
              </div>
            )}
          </div>

          <div className="flex items-center justify-between gap-3 border-t bg-gray-50 px-6 py-4">
            <div className="text-xs text-gray-500">
              {vm.container_id
                ? 'Saving stores values; the container keeps its current env until applied.'
                : 'No container yet — these apply when the VM is first started.'}
            </div>
            <div className="flex gap-2">
              <button
                onClick={onClose}
                disabled={busy}
                className="rounded-md border border-gray-300 px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 disabled:opacity-50"
              >
                Close
              </button>
              <button
                onClick={handleSave}
                disabled={busy || invalid}
                className="rounded-md bg-primary-600 px-4 py-2 text-sm text-white hover:bg-primary-700 disabled:opacity-50"
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
              {hasPending && vm.container_id && (
                <button
                  onClick={() => setConfirmApply(true)}
                  disabled={busy}
                  className="flex items-center gap-1 rounded-md bg-amber-600 px-4 py-2 text-sm text-white hover:bg-amber-700 disabled:opacity-50"
                >
                  <RefreshCw className={`h-4 w-4 ${applying ? 'animate-spin' : ''}`} />
                  {applying ? 'Applying…' : 'Apply & Recreate'}
                </button>
              )}
            </div>
          </div>
        </div>

        {confirmApply && (
          <div className="fixed inset-0 z-[60] flex items-center justify-center p-4">
            <div className="fixed inset-0 bg-gray-900 bg-opacity-50" onClick={() => setConfirmApply(false)} />
            <div className="relative w-full max-w-md rounded-lg bg-white p-6 shadow-xl">
              <div className="flex gap-3">
                <AlertTriangle className="mt-0.5 h-6 w-6 shrink-0 text-amber-600" />
                <div>
                  <h4 className="font-medium text-gray-900">Recreate {vm.hostname}?</h4>
                  <p className="mt-2 text-sm text-gray-600">
                    The container will be destroyed and rebuilt so the new environment takes
                    effect. <strong>Anything inside it that is not on a mounted volume will be
                    lost.</strong>
                  </p>
                  <p className="mt-2 text-sm text-gray-600">
                    {vm.status === 'running'
                      ? 'The VM is running and will be started again automatically.'
                      : 'The VM is stopped and will stay stopped.'}
                  </p>
                </div>
              </div>
              <div className="mt-5 flex justify-end gap-2">
                <button
                  onClick={() => setConfirmApply(false)}
                  disabled={applying}
                  className="rounded-md border border-gray-300 px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  onClick={handleApply}
                  disabled={applying}
                  className="rounded-md bg-amber-600 px-4 py-2 text-sm text-white hover:bg-amber-700 disabled:opacity-50"
                >
                  {applying ? 'Recreating…' : 'Recreate container'}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
