// frontend/src/components/range/DeploymentProgress.tsx
import { useEffect, useState, useRef } from 'react'
import { Loader2, ChevronDown, ChevronUp } from 'lucide-react'
import { rangesApi, eventsApi } from '../../services/api'
import { useSubstrate } from '../../stores/capabilitiesStore'
import { DeploymentStatusResponse, EventLog, EventType } from '../../types'
import { ResourceSection } from './ResourceSection'
import { ResourceRow } from './ResourceRow'

interface Props {
  rangeId: string
  rangeStatus: string
  onDeploymentComplete?: () => void
}

const DEPLOYMENT_EVENT_TYPES: EventType[] = [
  'deployment_started',
  'deployment_step',
  'deployment_completed',
  'deployment_failed',
  'router_creating',
  'router_created',
  'network_creating',
  'network_created',
  'vm_creating',
  'vm_started',
  'vm_stopped',
]

/**
 * Statuses that mean the work this panel is watching has finished.
 *
 * 'running' was the only one, which was right while deploy was the only thing the panel showed.
 * Start and stop are shown here too now, and a stop that ends in 'stopped' would otherwise never
 * report completion, leaving the page showing a progress bar for work that finished.
 */
const SETTLED = ['running', 'stopped', 'draft']

/**
 * The settled statuses that are also a status an operation *begins* from.
 *
 * The page mounts this panel on a status it set optimistically, before the request that starts
 * the work has been answered, so the first poll can land while the range is still the draft or
 * stopped range the operator clicked Deploy on. Treating that as completion tears the panel down
 * in the same second it appeared and puts the Deploy button back while the deploy is queued --
 * and clicking Deploy again is then refused, because by then the range is DEPLOYING. So these
 * two settle the panel only once the server has confirmed the work is in flight. 'running' is
 * not among them: it is never the status a deploy or a start begins from.
 */
const SETTLED_ONLY_ONCE_IN_FLIGHT = ['stopped', 'draft']

/** Whether a status the server has just reported means the work this panel is watching is over. */
export function settlesThePanel(reported: string, confirmedInFlight: boolean): boolean {
  if (!SETTLED.includes(reported)) return false
  return confirmedInFlight || !SETTLED_ONLY_ONCE_IN_FLIGHT.includes(reported)
}

export function DeploymentProgress({
  rangeId,
  rangeStatus,
  onDeploymentComplete
}: Props) {
  const [status, setStatus] = useState<DeploymentStatusResponse | null>(null)
  const [events, setEvents] = useState<EventLog[]>([])
  const [showLog, setShowLog] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const logEndRef = useRef<HTMLDivElement>(null)
  const prevEventsLengthRef = useRef(0)
  // What the rows are called. A Kubernetes range's networks are Multus attachments and its
  // machines are KubeVirt VMs; calling them Networks and VMs here taught the operator the Era A
  // vocabulary for things that are not those.
  const { isKubernetes } = useSubstrate()
  // Whether the server has said 'deploying' yet. See SETTLED_ONLY_ONCE_IN_FLIGHT.
  const inFlightRef = useRef(false)

  useEffect(() => {
    inFlightRef.current = false
  }, [rangeId])

  // Poll for deployment status
  useEffect(() => {
    if (rangeStatus !== 'deploying') return

    const loadStatus = async () => {
      try {
        const response = await rangesApi.getDeploymentStatus(rangeId)
        setStatus(response.data)

        const reported = response.data.status
        if (reported === 'deploying') inFlightRef.current = true

        if (settlesThePanel(reported, inFlightRef.current)) {
          onDeploymentComplete?.()
        } else if (response.data.status === 'error') {
          // Find error details from failed resources
          const failedVm = response.data.vms.find(v => v.status === 'failed')
          const failedNet = response.data.networks.find(n => n.status === 'failed')
          const errorDetail = failedVm?.statusDetail || failedNet?.statusDetail || response.data.router?.statusDetail
          setError(errorDetail || 'Deployment failed')
        }
      } catch (err) {
        console.error('Failed to load deployment status:', err)
        setError('Failed to load deployment status')
      }
    }

    loadStatus()
    const interval = setInterval(loadStatus, 1000)
    return () => clearInterval(interval)
  }, [rangeId, rangeStatus, onDeploymentComplete])

  // Poll for events (for the log)
  useEffect(() => {
    if (rangeStatus !== 'deploying') return

    const loadEvents = async () => {
      try {
        const response = await eventsApi.getEvents(rangeId, {
          limit: 100,
          event_types: DEPLOYMENT_EVENT_TYPES
        })
        setEvents(response.data.events.reverse())
      } catch (err) {
        console.error('Failed to load events:', err)
      }
    }

    loadEvents()
    const interval = setInterval(loadEvents, 1000)
    return () => clearInterval(interval)
  }, [rangeId, rangeStatus])

  // Auto-scroll log
  useEffect(() => {
    if (events.length > prevEventsLengthRef.current && showLog) {
      logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
    prevEventsLengthRef.current = events.length
  }, [events.length, showLog])

  const formatTime = (timestamp: string) => {
    return new Date(timestamp).toLocaleTimeString()
  }

  const formatElapsed = (seconds: number) => {
    const mins = Math.floor(seconds / 60)
    const secs = seconds % 60
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`
  }

  if (error) {
    return (
      <div className="bg-red-900/50 rounded-lg p-4 text-red-200">
        {error}
      </div>
    )
  }

  if (!status) {
    return (
      <div className="bg-gray-800 rounded-lg p-8 flex items-center justify-center">
        <Loader2 className="w-6 h-6 text-blue-500 animate-spin" />
      </div>
    )
  }

  const routerCompleted = status.router?.status === 'running' ? 1 : 0
  const networksCompleted = status.networks.filter(n => n.status === 'created').length
  const vmsCompleted = status.vms.filter(v => v.status === 'running' || v.status === 'stopped').length
  // A range with nothing declared in it would divide by zero and render the bar as NaN%.
  const percent = status.summary.total
    ? Math.round((status.summary.completed / status.summary.total) * 100)
    : 0
  // The stage the backend named, which on Kubernetes is also what says whether this is a deploy,
  // a start or a stop -- the range has one transitional status for all three.
  const title = status.stageName ? `${status.stageName}...` : 'Deploying Range...'

  return (
    <div className="bg-gray-800 rounded-lg shadow-lg border border-gray-700 overflow-hidden">
      {/* Header */}
      <div className="bg-blue-900/50 px-6 py-4 border-b border-gray-700">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Loader2 className="w-5 h-5 text-blue-400 animate-spin" />
            <h3 className="text-lg font-semibold text-white">{title}</h3>
          </div>
          <span className="text-gray-400 text-sm">
            Elapsed: {formatElapsed(status.elapsedSeconds)}
          </span>
        </div>

        {/* Stage indicator */}
        {status.currentStage && status.totalStages && (
          <div className="mt-3 flex items-center gap-2">
            <div className="flex gap-1">
              {Array.from({ length: status.totalStages! }, (_, i) => (
                <div
                  key={i}
                  className={`h-2 w-8 rounded-full transition-all duration-300 ${
                    i + 1 < status.currentStage!
                      ? 'bg-green-500'
                      : i + 1 === status.currentStage!
                      ? 'bg-blue-500 animate-pulse'
                      : 'bg-gray-600'
                  }`}
                />
              ))}
            </div>
            <span className="text-sm text-blue-300">
              Stage {status.currentStage}/{status.totalStages}
            </span>
          </div>
        )}

        {/* Progress bar */}
        <div className="mt-3">
          <div className="flex items-center justify-between text-sm text-gray-400 mb-1">
            <span>{status.summary.completed} of {status.summary.total} resources</span>
            <span>{percent}%</span>
          </div>
          <div className="h-2 bg-gray-700 rounded-full overflow-hidden">
            <div
              className="h-full bg-blue-500 transition-all duration-300"
              style={{ width: `${percent}%` }}
            />
          </div>
          {/* Current step message */}
          {status.currentStep && (
            <p className="mt-2 text-sm text-blue-300 truncate" title={status.currentStep}>
              {status.currentStep}
            </p>
          )}
        </div>
      </div>

      {/* Resource Sections */}
      <div>
        {/* DinD Container */}
        {status.router && (
          <ResourceSection title="DinD Container" completed={routerCompleted} total={1}>
            <ResourceRow
              name={status.router.name}
              detail="Docker-in-Docker"
              status={status.router.status}
              statusDetail={status.router.statusDetail}
              durationMs={status.router.durationMs}
            />
          </ResourceSection>
        )}

        {/* Networks. Empty during a start or a stop, which touch neither; an empty section
            headed "0/0" reads as a failure rather than as something not being done. */}
        {status.networks.length > 0 && (
          <ResourceSection
            title={isKubernetes ? 'Network Attachments' : 'Networks'}
            completed={networksCompleted}
            total={status.networks.length}
          >
            {status.networks.map(network => (
              <ResourceRow
                key={network.id}
                name={network.name}
                detail={network.subnet}
                status={network.status}
                statusDetail={network.statusDetail}
                durationMs={network.durationMs}
              />
            ))}
          </ResourceSection>
        )}

        {/* VMs */}
        <ResourceSection title={isKubernetes ? 'Machines' : 'VMs'} completed={vmsCompleted} total={status.vms.length}>
          {status.vms.map(vm => (
            <ResourceRow
              key={vm.id}
              name={vm.hostname}
              detail={vm.ip}
              status={vm.status}
              statusDetail={vm.statusDetail}
              durationMs={vm.durationMs}
            />
          ))}
        </ResourceSection>
      </div>

      {/* Expandable Log */}
      <div className="border-t border-gray-700">
        <button
          onClick={() => setShowLog(!showLog)}
          className="w-full px-4 py-3 flex items-center justify-between text-sm text-gray-400 hover:bg-gray-700/50"
        >
          <span>Deployment Log ({events.length} events)</span>
          {showLog ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
        </button>

        {showLog && (
          <div className="max-h-64 overflow-y-auto bg-gray-900 p-4">
            {events.length === 0 ? (
              <p className="text-gray-500 text-sm">Waiting for deployment events...</p>
            ) : (
              <div className="space-y-1 font-mono text-xs">
                {events.map((event) => (
                  <div key={event.id} className="flex items-start gap-2">
                    <span className="text-gray-500 whitespace-nowrap">
                      [{formatTime(event.created_at)}]
                    </span>
                    <span className={
                      event.event_type === 'deployment_failed' ? 'text-red-400' :
                      event.event_type === 'deployment_completed' ? 'text-green-400' :
                      event.event_type.includes('created') || event.event_type === 'vm_started' ? 'text-green-400' :
                      event.event_type.includes('creating') ? 'text-yellow-400' :
                      'text-gray-300'
                    }>
                      {event.message}
                    </span>
                  </div>
                ))}
                <div ref={logEndRef} />
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
