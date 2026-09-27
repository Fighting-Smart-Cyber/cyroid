// frontend/src/hooks/useRangeWorkloads.ts
/**
 * A range's workloads and applications on the Kubernetes substrate.
 *
 * On that substrate a range has no Network or VM rows -- its machines are KubeVirt
 * VirtualMachines built from the blueprint, and their live state comes from the cluster. Three
 * screens need exactly this (the range page, the instructor's Execution Console and the student's
 * Lab), and each had been fetching Era A VM rows and rendering an empty page. One hook, one
 * shape, one place to change.
 *
 * `substrate` in the response is the backend's answer for THIS range, so a caller can render the
 * Docker view unchanged when it says "dind".
 */
import { useCallback, useEffect, useState } from 'react'

export interface Workload {
  name: string
  os_family: string
  os_version: string
  cpus: number
  memory_mb: number
  status: string
  addresses: Record<string, string>
  console_available: boolean
}

export interface RangeApp {
  name: string
  /** Absolute: a range's applications answer on a host of their own, not on this origin. */
  url: string | null
  /**
   * The application has somewhere to answer: an applications host, an ingress class, and a
   * running range. It is NOT a statement that anything is answering -- nothing reads a pod, a
   * replica or a Helm release after deploy. Do not render this as "reachable".
   */
  published: boolean
  /** Why it is not published, in the backend's own words. Null when it is. */
  reason: string | null
}

export interface RangeWorkloads {
  substrate: 'dind' | 'kubernetes'
  namespace?: string
  workloads: Workload[]
  apps?: RangeApp[]
}

interface Result {
  data: RangeWorkloads | null
  isLoading: boolean
  error: string | null
  /** True once the backend has answered and said this range is on Kubernetes. */
  isKubernetes: boolean
  refresh: () => void
}

/**
 * @param rangeId the range to read
 * @param watch a value that, when it changes, refetches -- pass the range's status so the list
 *        refreshes the moment a deploy finishes and consoles become available.
 */
export function useRangeWorkloads(rangeId: string | undefined, watch?: unknown): Result {
  const [data, setData] = useState<RangeWorkloads | null>(null)
  const [isLoading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!rangeId) return
    const token = localStorage.getItem('token') || ''
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`/api/v1/ranges/${rangeId}/workloads`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!res.ok) throw new Error(`Could not read this range's workloads (${res.status})`)
      setData((await res.json()) as RangeWorkloads)
    } catch (err) {
      // Leave the last good answer on screen: a failed refresh should not blank a working page.
      setError(err instanceof Error ? err.message : 'Could not read this range')
    } finally {
      setLoading(false)
    }
  }, [rangeId])

  useEffect(() => {
    void load()
  }, [load, watch])

  return {
    data,
    isLoading,
    error,
    isKubernetes: data?.substrate === 'kubernetes',
    refresh: load,
  }
}
