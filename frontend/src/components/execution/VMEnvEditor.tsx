// frontend/src/components/execution/VMEnvEditor.tsx
// Edit a deployed VM's container environment (e.g. AVD/VPN target config)
// from the range/execution console. Saves via PUT /vms/{id}; changes apply on
// the next VM restart, so a "Save & Restart" path is offered.
import { useState } from 'react'
import { VM } from '../../types'
import { vmsApi } from '../../services/api'
import { X, Plus, Trash2, Save, RotateCcw } from 'lucide-react'

interface Props {
  vm: VM
  onClose: () => void
  onSaved: () => void
}

type Row = { key: string; value: string }

export function VMEnvEditor({ vm, onClose, onSaved }: Props) {
  const initial: Row[] = Object.entries(vm.environment || {}).map(([key, value]) => ({ key, value }))
  const [rows, setRows] = useState<Row[]>(initial.length ? initial : [{ key: '', value: '' }])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const setRow = (i: number, patch: Partial<Row>) =>
    setRows(rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)))
  const addRow = () => setRows([...rows, { key: '', value: '' }])
  const removeRow = (i: number) => setRows(rows.filter((_, idx) => idx !== i))

  const toEnv = (): Record<string, string> =>
    rows.reduce((acc, r) => {
      const k = r.key.trim()
      if (k) acc[k] = r.value
      return acc
    }, {} as Record<string, string>)

  const save = async (restart: boolean) => {
    setBusy(true)
    setError(null)
    try {
      await vmsApi.update(vm.id, { environment: toEnv() })
      if (restart && vm.status === 'running') {
        await vmsApi.restart(vm.id)
      }
      onSaved()
      onClose()
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Failed to save environment')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="bg-white rounded-lg shadow-xl w-full max-w-lg max-h-[85vh] flex flex-col">
        <div className="flex items-center justify-between px-4 py-3 border-b">
          <div>
            <h3 className="font-semibold">Target &amp; environment</h3>
            <p className="text-xs text-gray-500">{vm.hostname} — e.g. LAUNCH_URL, VPN_SERVER, ENDPOINT_HOST</p>
          </div>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded" title="Close">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-4 overflow-y-auto space-y-2">
          {rows.map((r, i) => (
            <div key={i} className="flex gap-2 items-center">
              <input
                className="flex-1 min-w-0 border rounded px-2 py-1 text-sm font-mono"
                placeholder="KEY"
                value={r.key}
                onChange={(e) => setRow(i, { key: e.target.value })}
              />
              <input
                className="flex-1 min-w-0 border rounded px-2 py-1 text-sm font-mono"
                placeholder="value"
                value={r.value}
                onChange={(e) => setRow(i, { value: e.target.value })}
              />
              <button onClick={() => removeRow(i)} className="p-1 hover:bg-red-100 rounded shrink-0" title="Remove">
                <Trash2 className="w-4 h-4 text-red-600" />
              </button>
            </div>
          ))}
          <button onClick={addRow} className="flex items-center gap-1 text-sm text-blue-600 hover:underline">
            <Plus className="w-4 h-4" /> Add variable
          </button>
          {error && <p className="text-sm text-red-600">{error}</p>}
          <p className="text-xs text-gray-400 pt-1">
            Changes apply on VM restart. Use “Save &amp; restart” to apply now.
          </p>
        </div>

        <div className="flex justify-end gap-2 px-4 py-3 border-t">
          <button
            onClick={() => save(false)}
            disabled={busy}
            className="flex items-center gap-1 px-3 py-1.5 text-sm border rounded hover:bg-gray-50 disabled:opacity-50"
          >
            <Save className="w-4 h-4" /> Save
          </button>
          <button
            onClick={() => save(true)}
            disabled={busy || vm.status !== 'running'}
            className="flex items-center gap-1 px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
            title={vm.status !== 'running' ? 'VM must be running to restart' : 'Save and restart to apply'}
          >
            <RotateCcw className="w-4 h-4" /> Save &amp; restart
          </button>
        </div>
      </div>
    </div>
  )
}
