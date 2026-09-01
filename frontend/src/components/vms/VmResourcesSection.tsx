import { useEffect, useState } from 'react'
import { Check, Cpu, Info, MemoryStick } from 'lucide-react'
import { vmsApi } from '../../services/api'
import type { VM } from '../../types'

interface Props {
  vm: VM
  /** Called after a successful change so the parent can refetch. */
  onChanged: () => void
}

/** Mirrors the server bounds in VMResourceUpdate. The Docker daemon enforces
 *  none of its own — it accepts a CPU limit of one millionth of a core — so
 *  these are the only limits between a typo and a unusable container. */
const CPU_MIN = 1
const CPU_MAX = 32
const RAM_MIN_MB = 512
const RAM_MAX_MB = 131072

const gb = (mb: number) => (mb / 1024).toFixed(mb % 1024 === 0 ? 0 : 1)

/**
 * CPU and memory controls for a VM, applied to the running container.
 *
 * Deliberately NOT the two-step Save -> Apply flow the environment editor uses.
 * Docker changes CPU and memory on a live container without recreating it, so
 * there is nothing to lose and no reason to make an operator confirm a
 * destructive step that will not happen. The values are saved either way; when
 * no container is running they take effect the next time it starts.
 */
export function VmResourcesSection({ vm, onChanged }: Props) {
  const [cpu, setCpu] = useState<number>(vm.cpu)
  const [ramMb, setRamMb] = useState<number>(vm.ram_mb)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<{ live: boolean; detail: string } | null>(null)

  // Re-seed when the parent refetches and hands us a newer VM.
  useEffect(() => {
    setCpu(vm.cpu)
    setRamMb(vm.ram_mb)
  }, [vm.cpu, vm.ram_mb])

  const changed = cpu !== vm.cpu || ramMb !== vm.ram_mb
  const cpuValid = Number.isFinite(cpu) && cpu >= CPU_MIN && cpu <= CPU_MAX
  const ramValid = Number.isFinite(ramMb) && ramMb >= RAM_MIN_MB && ramMb <= RAM_MAX_MB

  const apply = async () => {
    setSaving(true)
    setError(null)
    setResult(null)
    try {
      // Send only what moved, so an unchanged field is never restated.
      const payload: { cpu?: number; ram_mb?: number } = {}
      if (cpu !== vm.cpu) payload.cpu = cpu
      if (ramMb !== vm.ram_mb) payload.ram_mb = ramMb

      const { data } = await vmsApi.updateResources(vm.id, payload)
      setResult({ live: data.applied_live, detail: data.detail })
      onChanged()
    } catch (e) {
      // Typed structurally rather than as `any`: the lint budget is a ratchet
      // that may fall and never rise. FastAPI returns `detail` as a string for
      // our own HTTPExceptions but as an array of objects for 422s, so only a
      // string is worth showing.
      const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
      setError(typeof detail === 'string' ? detail : 'Failed to update resources')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="border-t border-gray-200 px-6 py-4">
      <div className="mb-3 flex items-start justify-between gap-4">
        <div>
          <h3 className="text-sm font-medium text-gray-900">Resources</h3>
          <p className="mt-1 text-xs text-gray-500">
            Applied to the running container immediately. No restart, and nothing inside the
            container is lost.
          </p>
        </div>
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <label className="block">
          <span className="mb-1 flex items-center gap-1.5 text-xs font-medium text-gray-700">
            <Cpu className="h-3.5 w-3.5" />
            CPU cores
          </span>
          <input
            type="number"
            min={CPU_MIN}
            max={CPU_MAX}
            value={Number.isFinite(cpu) ? cpu : ''}
            onChange={e => setCpu(parseInt(e.target.value, 10))}
            className={`w-28 rounded-md border px-3 py-2 text-sm ${
              cpuValid ? 'border-gray-300' : 'border-red-400 bg-red-50'
            }`}
          />
        </label>

        <label className="block">
          <span className="mb-1 flex items-center gap-1.5 text-xs font-medium text-gray-700">
            <MemoryStick className="h-3.5 w-3.5" />
            Memory (MB)
          </span>
          <input
            type="number"
            min={RAM_MIN_MB}
            max={RAM_MAX_MB}
            step={512}
            value={Number.isFinite(ramMb) ? ramMb : ''}
            onChange={e => setRamMb(parseInt(e.target.value, 10))}
            className={`w-32 rounded-md border px-3 py-2 text-sm ${
              ramValid ? 'border-gray-300' : 'border-red-400 bg-red-50'
            }`}
          />
        </label>

        <div className="pb-2 text-xs text-gray-500">
          {ramValid ? `${gb(ramMb)} GB` : `${RAM_MIN_MB}–${RAM_MAX_MB} MB`}
        </div>

        <button
          onClick={apply}
          disabled={!changed || !cpuValid || !ramValid || saving}
          className="ml-auto rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white
                     hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-gray-300"
        >
          {saving ? 'Applying…' : 'Apply resources'}
        </button>
      </div>

      {!cpuValid && (
        <p className="mt-2 text-xs text-red-600">
          CPU must be between {CPU_MIN} and {CPU_MAX} cores.
        </p>
      )}
      {!ramValid && (
        <p className="mt-2 text-xs text-red-600">
          Memory must be between {RAM_MIN_MB} and {RAM_MAX_MB} MB.
        </p>
      )}

      {error && (
        <div className="mt-3 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {result && (
        <div
          className={`mt-3 flex gap-2 rounded-md border p-3 text-sm ${
            result.live
              ? 'border-green-300 bg-green-50 text-green-800'
              : 'border-amber-300 bg-amber-50 text-amber-800'
          }`}
        >
          {result.live ? (
            <Check className="mt-0.5 h-4 w-4 shrink-0" />
          ) : (
            <Info className="mt-0.5 h-4 w-4 shrink-0" />
          )}
          <span>{result.detail}</span>
        </div>
      )}
    </div>
  )
}
