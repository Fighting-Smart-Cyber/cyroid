/** Pop-out window for a KubeVirt workload's console -- the Era B twin of StandaloneConsole. */
import { useEffect } from 'react'
import { useParams } from 'react-router-dom'
import { KubeVirtConsole } from '../components/console/KubeVirtConsole'
import { pageTitle } from '../lib/branding'

export default function KubeVirtStandaloneConsole() {
  const { rangeId, workload } = useParams<{ rangeId: string; workload: string }>()

  useEffect(() => {
    if (workload) document.title = pageTitle(`Console: ${workload}`)
  }, [workload])

  if (!rangeId || !workload) {
    return <div className="p-6 text-red-600">No workload given</div>
  }
  return <KubeVirtConsole rangeId={rangeId} workload={workload} fullscreen />
}
