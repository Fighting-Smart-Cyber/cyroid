// frontend/src/pages/StudentLab.tsx
import { useEffect, useState, useCallback, useRef } from 'react'
import { useParams } from 'react-router-dom'
import { BookOpen, Loader2, AlertCircle, ChevronRight, GripVertical, X } from 'lucide-react'
import { rangesApi, vmsApi, walkthroughApi, mselApi } from '../services/api'
import { MSEL, Range, VM, Walkthrough } from '../types'
import { WalkthroughPanel } from '../components/walkthrough'
import { VMSelector, ConsoleEmbed, KubernetesLabPanel } from '../components/lab'
import { useRangeWorkloads } from '../hooks/useRangeWorkloads'
import { VmClipboardProvider } from '../contexts/VmClipboardContext'
import { pageTitle } from '../lib/branding'
import { resolveMachineTarget } from '../lib/machineTarget'

/**
 * The one guide a learner sees, from whichever place the instructor wrote it.
 *
 * A range could carry a guide in two places and the product never said which one counted: a
 * Student Guide linked from the Content Library, and a `walkthrough:` block inside the MSEL
 * imported for the range. The second was parsed, stored and returned by /msel/{range_id} and then
 * read by nothing, so an instructor who wrote one watched their learners open a lab with no guide
 * in it and no explanation. Both are now the same mechanism -- the range's guide -- with the
 * linked Student Guide winning because linking one is a deliberate choice and importing an MSEL
 * is not.
 */
async function resolveGuide(rangeId: string, linked: Walkthrough | null): Promise<Walkthrough | null> {
  if (linked) return linked
  try {
    const res = await mselApi.get(rangeId)
    // The response carries the parsed walkthrough; the MSEL type in ../types predates it.
    const msel = res.data as MSEL & { walkthrough?: Walkthrough | null }
    return msel.walkthrough ?? null
  } catch {
    // Most ranges have no MSEL at all and that route answers 404. Not having one is not an error.
    return null
  }
}

export default function StudentLab() {
  const { rangeId } = useParams<{ rangeId: string }>()
  const [range, setRange] = useState<Range | null>(null)
  const [vms, setVMs] = useState<VM[]>([])
  const [walkthrough, setWalkthrough] = useState<Walkthrough | null>(null)
  const [selectedVmId, setSelectedVmId] = useState<string | null>(null)
  const [isCollapsed, setIsCollapsed] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [targetNotice, setTargetNotice] = useState<string | null>(null)

  // Panel width state (percentage) - default 40% walkthrough, 60% console
  const [walkthroughWidth, setWalkthroughWidth] = useState(() => {
    const saved = localStorage.getItem('student-lab-width')
    return saved ? parseInt(saved, 10) : 40
  })
  const [isDragging, setIsDragging] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  const token = localStorage.getItem('token') || ''
  const selectedVm = vms.find(vm => vm.id === selectedVmId) || null

  // A Kubernetes range has no VM rows; its machines and applications come from the cluster.
  const { data: k8s, isKubernetes, isLoading: loadingWorkloads } = useRangeWorkloads(rangeId, range?.status)

  useEffect(() => {
    if (!rangeId) {
      setError('No range ID provided')
      setLoading(false)
      return
    }

    const loadData = async () => {
      try {
        const [rangeRes, vmsRes, walkthroughRes] = await Promise.all([
          rangesApi.get(rangeId),
          vmsApi.list(rangeId),
          walkthroughApi.get(rangeId),
        ])

        setRange(rangeRes.data)
        setVMs(vmsRes.data)
        setWalkthrough(await resolveGuide(rangeId, walkthroughRes.data.walkthrough))

        // Auto-select first running VM
        const runningVm = vmsRes.data.find(vm => vm.status === 'running')
        if (runningVm) {
          setSelectedVmId(runningVm.id)
        }

        setLoading(false)
      } catch (err: unknown) {
        const error = err as { response?: { data?: { detail?: string } } }
        setError(error.response?.data?.detail || 'Failed to load lab')
        setLoading(false)
      }
    }

    loadData()
  }, [rangeId])

  // Update document title
  useEffect(() => {
    if (range) {
      document.title = pageTitle(`Lab: ${range.name}`)
    }
    return () => {
      document.title = pageTitle()
    }
  }, [range])

  const handleOpenMachine = (rawName: string) => {
    const target = resolveMachineTarget(rawName, {
      isKubernetes,
      workloads: k8s?.workloads ?? [],
      vms,
    })
    if (target.kind === 'select') {
      setTargetNotice(null)
      setSelectedVmId(target.vmId)
      return
    }
    setTargetNotice(target.message)
  }

  // Trigger resize on iframes (for VNC to recalculate dimensions)
  const triggerIframeResize = useCallback(() => {
    const iframes = document.querySelectorAll('iframe')
    iframes.forEach(iframe => {
      try {
        iframe.contentWindow?.dispatchEvent(new Event('resize'))
      } catch {
        // Cross-origin iframe, can't dispatch event directly
      }
    })
    window.dispatchEvent(new Event('resize'))
  }, [])

  // Trigger resize when walkthrough is collapsed/expanded
  useEffect(() => {
    const timer = setTimeout(triggerIframeResize, 150)
    return () => clearTimeout(timer)
  }, [isCollapsed, triggerIframeResize])

  useEffect(() => {
    if (!isDragging) return

    const handleMouseMove = (e: MouseEvent) => {
      if (!containerRef.current) return
      const containerRect = containerRef.current.getBoundingClientRect()
      const newWidth = ((e.clientX - containerRect.left) / containerRect.width) * 100
      // Clamp between 20% and 60%
      const clampedWidth = Math.max(20, Math.min(60, newWidth))
      setWalkthroughWidth(clampedWidth)
    }

    const handleMouseUp = () => {
      setIsDragging(false)
      // Save to localStorage
      localStorage.setItem('student-lab-width', walkthroughWidth.toString())
      // Trigger iframe resize after layout settles
      setTimeout(triggerIframeResize, 100)
    }

    document.addEventListener('mousemove', handleMouseMove)
    document.addEventListener('mouseup', handleMouseUp)

    return () => {
      document.removeEventListener('mousemove', handleMouseMove)
      document.removeEventListener('mouseup', handleMouseUp)
    }
  }, [isDragging, walkthroughWidth, triggerIframeResize])

  if (loading) {
    return (
      <div className="h-screen w-screen bg-gray-900 flex items-center justify-center">
        <div className="text-center">
          <Loader2 className="w-8 h-8 text-blue-500 animate-spin mx-auto mb-2" />
          <p className="text-gray-400">Loading lab...</p>
        </div>
      </div>
    )
  }

  if (error || !range) {
    return (
      <div className="h-screen w-screen bg-gray-900 flex items-center justify-center">
        <div className="text-center max-w-md px-4">
          <AlertCircle className="w-12 h-12 text-red-400 mx-auto mb-3" />
          <p className="text-red-400 mb-2">{error || 'Range not found'}</p>
          <a href="/student-portal" className="text-blue-400 hover:underline">
            Return to your labs
          </a>
        </div>
      </div>
    )
  }

  // A missing guide is not a missing lab. This used to take over the whole screen, so a learner
  // whose instructor had not written a walkthrough could not reach their machines at all.
  const showGuide = !!walkthrough && !isCollapsed

  return (
    <VmClipboardProvider>
    <div className="h-screen w-screen bg-gray-900 flex flex-col">
      <div ref={containerRef} className="flex-1 flex overflow-hidden">
        {/* Walkthrough Panel - Guide on the left */}
        {showGuide && (
          <>
            <div
              className="h-full bg-gray-900 overflow-hidden flex-shrink-0"
              style={{ width: `${walkthroughWidth}%` }}
            >
              <WalkthroughPanel
                rangeId={rangeId!}
                walkthrough={walkthrough!}
                onOpenVM={handleOpenMachine}
                onCollapse={() => setIsCollapsed(true)}
              />
            </div>

            {/* Resize Handle - z-[9999] needed to be above VNC iframe stacking context */}
            <div
              className={`w-3 flex-shrink-0 cursor-col-resize flex items-center justify-center transition-colors relative z-[9999] ${
                isDragging ? 'bg-blue-500' : 'bg-gray-700 hover:bg-blue-500'
              }`}
              onMouseDown={(e) => {
                e.preventDefault()
                e.stopPropagation()
                setIsDragging(true)
              }}
              style={{ pointerEvents: 'auto' }}
            >
              <GripVertical className={`w-4 h-4 pointer-events-none ${isDragging ? 'text-white' : 'text-gray-500'}`} />
            </div>
          </>
        )}

        {/* The learner's machines on the right: a VNC iframe on Era A, the cluster's workloads
            and applications on Kubernetes. */}
        <div className="flex-1 h-full flex flex-col relative bg-gray-900 min-w-0">
          {/* Expand button when collapsed */}
          {isCollapsed && walkthrough && (
            <button
              onClick={() => setIsCollapsed(false)}
              className="absolute left-2 top-2 z-10 p-2 bg-gray-800 rounded hover:bg-gray-700 flex items-center gap-2 border border-gray-700"
              title="Show the guide"
            >
              <BookOpen className="w-5 h-5 text-blue-400" />
              <ChevronRight className="w-4 h-4 text-gray-400" />
            </button>
          )}

          {/* Floated rather than placed in flow: the console below is an iframe that has to be
              told to resize, and the expand button already owns the top-left corner. */}
          {targetNotice && (
            <div className="absolute top-2 right-2 z-[9999] max-w-sm flex items-start gap-2 px-3 py-2 rounded border border-amber-700 bg-amber-900/90 text-amber-100 text-sm shadow-lg">
              <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0 text-amber-400" />
              <span className="flex-1">{targetNotice}</span>
              <button
                onClick={() => setTargetNotice(null)}
                className="p-0.5 text-amber-300 hover:text-white rounded"
                title="Dismiss"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          )}

          {isKubernetes ? (
            <KubernetesLabPanel
              rangeId={rangeId!}
              workloads={k8s?.workloads ?? []}
              apps={k8s?.apps ?? []}
              isLoading={loadingWorkloads}
            />
          ) : (
            <>
              {/* Console */}
              <div className="flex-1 min-h-0">
                <ConsoleEmbed
                  vmId={selectedVmId}
                  vmHostname={selectedVm?.hostname || null}
                  token={token}
                />
              </div>

              {/* VM Selector */}
              <VMSelector
                vms={vms}
                selectedVmId={selectedVmId}
                onSelectVM={setSelectedVmId}
              />
            </>
          )}
        </div>
      </div>

      {/* Drag overlay to prevent iframe from capturing mouse events */}
      {isDragging && (
        <div className="fixed inset-0 z-50 cursor-col-resize" />
      )}
    </div>
    </VmClipboardProvider>
  )
}
