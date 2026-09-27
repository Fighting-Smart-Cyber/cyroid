// frontend/src/pages/TrainingEventDetail.tsx
import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  Save,
  ArrowLeft,
  Calendar,
  Clock,
  MapPin,
  Users,
  Plus,
  X,
  Tag,
  CheckCircle,
  XCircle,
  CalendarCheck,
  BookOpen,
  LayoutTemplate,
  Trash2,
  Monitor,
  Rocket,
  Loader2,
  ExternalLink,
  RotateCcw,
  AlertTriangle,
} from 'lucide-react'
import {
  api,
  trainingEventsApi,
  TrainingEventDetail as TrainingEventDetailType,
  EventCreate,
  EventUpdate,
  EventStatus,
  blueprintsApi,
  Blueprint,
  contentApi,
  ContentListItem,
  usersApi,
  User,
} from '../services/api'
import { useAuthStore } from '../stores/authStore'
import { toast } from '../stores/toastStore'
import { useSubstrate } from '../stores/capabilitiesStore'
import { apiErrorDetail, machineNoun } from '../lib/blueprints'
import {
  eventActionWords,
  labState,
  lifecycleFailureMessage,
  rollupLabs,
  startFailureNotice,
  summarizeLabs,
} from '../lib/trainingEvents'
import { VMVisibilityControl } from '../components/events/VMVisibilityControl'
import { format, parseISO } from 'date-fns'
import DOMPurify from 'dompurify'

const STATUS_LABELS: Record<EventStatus, string> = {
  draft: 'Draft',
  scheduled: 'Scheduled',
  running: 'Running',
  completed: 'Completed',
  cancelled: 'Cancelled',
}

type LifecycleAction = 'publish' | 'start' | 'complete' | 'cancel' | 'reactivate'

/**
 * How many labs a cohort gets, which is the only thing the two delivery modes differ in.
 *
 * The platform has always resolved a range with no assigned learner to an environment the whole
 * cohort shares, and nothing offered a way to create one -- every path that made a range under an
 * event assigned it to somebody -- so the mode the architecture turns on had never run.
 */
type EventDelivery = 'self-paced' | 'team-exercise'

const DELIVERY_MODES: { value: EventDelivery; label: string; effect: string }[] = [
  {
    value: 'self-paced',
    label: 'A lab each',
    effect:
      'Every student gets a lab of their own. They cannot see or disturb each other’s work.',
  },
  {
    value: 'team-exercise',
    label: 'One shared lab',
    effect:
      'The cohort works in a single lab: everyone sees the same machines and each other’s changes.',
  },
]

/**
 * The roles a person can hold *in this event*, and what each one actually changes.
 *
 * Not the same vocabulary as the platform roles under Access Control, which say who may open the
 * event at all -- the form used to render both as "roles" side by side, so the choice here read as
 * four interchangeable labels. The API accepts exactly this set and
 * backend/tests/unit/test_team_exercise.py compares the two, so adding a label here without
 * giving it a meaning there fails the build.
 */
const PARTICIPANT_ROLES: { value: string; label: string; effect: string }[] = [
  { value: 'student', label: 'Student', effect: 'Gets a lab, and sees the student briefing.' },
  {
    value: 'instructor',
    label: 'Instructor',
    effect: 'No lab. Sees the whole briefing, instructor notes included.',
  },
  {
    value: 'evaluator',
    label: 'Evaluator',
    effect: 'No lab. Sees the briefing except the instructor notes.',
  },
  {
    value: 'observer',
    label: 'Observer',
    effect: 'No lab. Sees only the student guide and reference material.',
  },
]

/**
 * The delivery fields the API serves on an event.
 *
 * Declared here rather than in the API client because that file is hand-written and scheduled for
 * replacement by a generated one (UX-2); this page should not be the reason it grows another
 * hand-maintained field.
 */
type EventWithDelivery = TrainingEventDetailType & {
  delivery?: EventDelivery
  team_range_id?: string | null
  team_range_status?: string | null
  team_range_name?: string | null
}

// Sanitize HTML to prevent XSS - uses DOMPurify for secure rendering
function sanitizeHtml(html: string): string {
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS: [
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'br', 'hr',
      'strong', 'b', 'em', 'i', 'u', 's', 'strike', 'code', 'pre',
      'ul', 'ol', 'li', 'blockquote',
      'a', 'img',
      'table', 'thead', 'tbody', 'tr', 'th', 'td',
      'div', 'span',
    ],
    ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'class', 'target', 'rel', 'colspan', 'rowspan'],
  })
}

export default function TrainingEventDetail() {
  const { id } = useParams()
  const navigate = useNavigate()
  const { user } = useAuthStore()
  const isNew = id === 'new'

  const [loading, setLoading] = useState(!isNew)
  const [saving, setSaving] = useState(false)
  const [event, setEvent] = useState<EventWithDelivery | null>(null)

  // Form state
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [startDatetime, setStartDatetime] = useState('')
  const [endDatetime, setEndDatetime] = useState('')
  const [isAllDay, setIsAllDay] = useState(false)
  const [timezone, setTimezone] = useState('UTC')
  const [organization, setOrganization] = useState('')
  const [location, setLocation] = useState('')
  const [blueprintId, setBlueprintId] = useState('')
  const [selectedContentIds, setSelectedContentIds] = useState<string[]>([])
  const [allowedRoles, setAllowedRoles] = useState<string[]>([])
  const [tags, setTags] = useState<string[]>([])
  const [tagInput, setTagInput] = useState('')

  // Dropdown data
  const [blueprints, setBlueprints] = useState<Blueprint[]>([])
  const [contentItems, setContentItems] = useState<ContentListItem[]>([])
  const [users, setUsers] = useState<User[]>([])

  // Participant management
  const [selectedUserId, setSelectedUserId] = useState('')
  const [participantRole, setParticipantRole] = useState('student')

  // Chosen before the event starts, because that is the moment it changes anything: it decides
  // whether the start creates one range per student or one the cohort shares.
  const [delivery, setDelivery] = useState<EventDelivery>('self-paced')

  // Briefing view
  const [showBriefing, setShowBriefing] = useState(false)
  const [briefingContent, setBriefingContent] = useState<{ title: string; html: string }[]>([])

  // Confirmation dialog for complete/cancel
  const [confirmAction, setConfirmAction] = useState<'complete' | 'cancel' | null>(null)

  // The lifecycle action in flight, so a second press cannot queue a second cohort of ranges.
  const [pending, setPending] = useState<LifecycleAction | null>(null)
  const [deleting, setDeleting] = useState(false)

  // A refused start has to stay on the screen. A toast is gone in five seconds and the reason --
  // which blueprint this substrate will not deploy, and why -- is the whole of what the
  // instructor has to act on.
  const [startError, setStartError] = useState<{ reason: string; aftermath: string } | null>(null)

  const { label: substrateLabel, isKubernetes } = useSubstrate()
  const machines = machineNoun(null, isKubernetes)

  const canManage = event
    ? user?.id === event.created_by_id || user?.roles?.includes('admin')
    : user?.roles?.includes('admin') || user?.roles?.includes('engineer')

  // The exact condition the participant picker renders under, the loaded event included: before
  // it arrives `canManage` is the new-event fallback, which is true for any engineer, including
  // one who is only reading somebody else's event.
  const participantPickerShown = !isNew && !!event && !!canManage

  // A team exercise is an event that owns one range nobody is assigned to; the server says so
  // rather than the page inferring it, because the same property is what places that range in a
  // vcluster of its own.
  const isTeamExercise = event?.delivery === 'team-exercise'
  const sharedLab = labState(event?.team_range_status)

  // Count ranges that will be deleted (used in delete confirmation). Counting participants would
  // promise to delete one lab per student in a team exercise, where there is only ever the one.
  const labsCount = isTeamExercise
    ? event?.team_range_id
      ? 1
      : 0
    : event?.participants?.filter(p => p.range_id).length || 0
  const labNoun = isTeamExercise ? 'shared lab' : 'student lab'
  const labs = rollupLabs(event?.participants || [])

  useEffect(() => {
    loadDropdownData()
    if (!isNew && id) {
      loadEvent(id)
    }
  }, [id, isNew])

  // The user list feeds the participant picker and nothing else, and `GET /users` answers 403 to
  // everyone but an admin. Asking for it on every view of an event put "Administrator access
  // required" in front of a student about a control they are never shown; sharing one Promise.all
  // with the other two lists, it also took the blueprint picker and the content library down with
  // it for every non-admin who opened an event.
  useEffect(() => {
    if (!participantPickerShown) return
    usersApi
      .list()
      .then((response) => setUsers(response.data))
      .catch((err) => {
        toast.error(
          `Could not load the users available to add: ${apiErrorDetail(err, 'the server gave no reason')}`
        )
      })
  }, [participantPickerShown])

  async function loadDropdownData() {
    // Settled independently rather than as a group: both fill controls that every viewer of the
    // event is shown, and either refusal used to blank the other as well.
    const [blueprintsRes, contentRes] = await Promise.allSettled([
      blueprintsApi.list(),
      contentApi.list({ published_only: true }),
    ])
    if (blueprintsRes.status === 'fulfilled') setBlueprints(blueprintsRes.value.data)
    if (contentRes.status === 'fulfilled') setContentItems(contentRes.value.data)
    // Silence here left the blueprint picker and the content list looking merely empty, which is
    // indistinguishable from an install that has neither -- and an event with no blueprint
    // selected cannot be started at all.
    if (blueprintsRes.status === 'rejected') {
      toast.error(
        `Could not load the blueprint list: ${apiErrorDetail(blueprintsRes.reason, 'the server gave no reason')}`
      )
    }
    if (contentRes.status === 'rejected') {
      toast.error(
        `Could not load the content library: ${apiErrorDetail(contentRes.reason, 'the server gave no reason')}`
      )
    }
  }

  // `showSpinner` is off when refreshing after an action: replacing the page with a spinner would
  // take the refusal banner off the screen the moment it was put there.
  async function loadEvent(eventId: string, showSpinner = true) {
    if (showSpinner) setLoading(true)
    try {
      const response = await trainingEventsApi.get(eventId)
      const data: EventWithDelivery = response.data
      setEvent(data)
      setName(data.name)
      setDescription(data.description || '')
      setStartDatetime(format(parseISO(data.start_datetime), "yyyy-MM-dd'T'HH:mm"))
      if (data.end_datetime) {
        setEndDatetime(format(parseISO(data.end_datetime), "yyyy-MM-dd'T'HH:mm"))
      }
      setIsAllDay(data.is_all_day)
      setTimezone(data.timezone)
      setOrganization(data.organization || '')
      setLocation(data.location || '')
      setBlueprintId(data.blueprint_id || '')
      setSelectedContentIds(data.content_ids || [])
      setAllowedRoles(data.allowed_roles || [])
      setTags(data.tags || [])
      // An event that already owns a shared range keeps that mode selected. A reactivated team
      // exercise whose lab was kept would otherwise come back with the selector on "a lab each",
      // and starting it again would give the cohort a lab each beside the one they share.
      if (data.delivery === 'team-exercise') setDelivery('team-exercise')
    } catch (err) {
      // Bouncing to the list with nothing said reads as a broken link rather than as the 403 or
      // 404 it usually is.
      toast.error(apiErrorDetail(err, 'Could not open that event'))
      navigate('/events')
    } finally {
      setLoading(false)
    }
  }

  async function handleSave() {
    if (!name.trim()) {
      toast.error('Name is required')
      return
    }
    if (!startDatetime) {
      toast.error('Start date/time is required')
      return
    }

    setSaving(true)
    try {
      if (isNew) {
        const data: EventCreate = {
          name,
          description: description || undefined,
          start_datetime: new Date(startDatetime).toISOString(),
          end_datetime: endDatetime ? new Date(endDatetime).toISOString() : undefined,
          is_all_day: isAllDay,
          timezone,
          organization: organization || undefined,
          location: location || undefined,
          blueprint_id: blueprintId || undefined,
          content_ids: selectedContentIds,
          allowed_roles: allowedRoles,
          tags,
        }
        const response = await trainingEventsApi.create(data)
        toast.success(`Created "${name}"`)
        navigate(`/events/${response.data.id}`)
      } else if (id) {
        const data: EventUpdate = {
          name,
          description: description || undefined,
          start_datetime: new Date(startDatetime).toISOString(),
          end_datetime: endDatetime ? new Date(endDatetime).toISOString() : undefined,
          is_all_day: isAllDay,
          timezone,
          organization: organization || undefined,
          location: location || undefined,
          blueprint_id: blueprintId || undefined,
          content_ids: selectedContentIds,
          allowed_roles: allowedRoles,
          tags,
        }
        await trainingEventsApi.update(id, data)
        toast.success('Event saved')
        await loadEvent(id, false)
      }
    } catch (err) {
      toast.error(apiErrorDetail(err, 'Failed to save event'))
    } finally {
      setSaving(false)
    }
  }

  function handleAddTag() {
    const tag = tagInput.trim()
    if (tag && !tags.includes(tag)) {
      setTags([...tags, tag])
      setTagInput('')
    }
  }

  function handleToggleRole(role: string) {
    if (allowedRoles.includes(role)) {
      setAllowedRoles(allowedRoles.filter((r) => r !== role))
    } else {
      setAllowedRoles([...allowedRoles, role])
    }
  }

  function handleToggleContent(contentId: string) {
    if (selectedContentIds.includes(contentId)) {
      setSelectedContentIds(selectedContentIds.filter((c) => c !== contentId))
    } else {
      setSelectedContentIds([...selectedContentIds, contentId])
    }
  }

  async function handleAddParticipant() {
    if (!selectedUserId || !id) return
    const username = users.find((u) => u.id === selectedUserId)?.username ?? 'that user'
    try {
      await trainingEventsApi.addParticipant(id, selectedUserId, participantRole)
      setSelectedUserId('')
      toast.success(`Added ${username} as ${participantRole}`)
      await loadEvent(id, false)
    } catch (err) {
      toast.error(apiErrorDetail(err, `Could not add ${username} to this event`))
    }
  }

  async function handleRemoveParticipant(userId: string) {
    if (!id) return
    const username =
      event?.participants.find((p) => p.user_id === userId)?.username ?? 'that participant'
    try {
      await trainingEventsApi.removeParticipant(id, userId)
      toast.success(`Removed ${username}`)
      await loadEvent(id, false)
    } catch (err) {
      toast.error(apiErrorDetail(err, `Could not remove ${username} from this event`))
    }
  }

  async function loadBriefing() {
    if (!id) return
    try {
      const response = await trainingEventsApi.getBriefing(id)
      setBriefingContent(
        response.data.content_items.map((item) => ({
          title: item.title,
          html: item.body_html || '',
        }))
      )
      setShowBriefing(true)
    } catch (err) {
      toast.error(apiErrorDetail(err, 'Could not load the briefing for this event'))
    }
  }

  async function handleStatusChange(action: LifecycleAction, autoDeploy = false) {
    if (!id) return

    // For complete/cancel, show confirmation first
    if ((action === 'complete' || action === 'cancel') && !confirmAction) {
      setConfirmAction(action)
      return
    }

    setPending(action)
    if (action === 'start') setStartError(null)
    try {
      switch (action) {
        case 'publish':
          await trainingEventsApi.publish(id)
          break
        case 'start':
          // Called through the shared axios client rather than `trainingEventsApi.start`: that
          // helper predates the delivery mode, and dropping the parameter here would leave the
          // instructor choosing between two buttons that send the same request.
          await api.post(`/training-events/${id}/start`, null, {
            params: { auto_deploy: autoDeploy, delivery },
          })
          break
        case 'complete':
          await trainingEventsApi.complete(id)
          break
        case 'cancel':
          await trainingEventsApi.cancel(id)
          break
        case 'reactivate':
          await trainingEventsApi.reactivate(id)
          break
      }
      toast.success(`${eventActionWords(action).done} "${event?.name ?? 'event'}"`)
    } catch (err) {
      if (action === 'start') setStartError(startFailureNotice(err))
      toast.error(lifecycleFailureMessage(action, event?.name ?? 'this event', err))
    } finally {
      setPending(null)
      setConfirmAction(null)
      // Reloaded either way, because the server is the only authority on what the event is now:
      // a refused start leaves it unchanged, and a complete whose teardown stopped partway has
      // already destroyed some of the labs the page is still listing.
      await loadEvent(id, false)
    }
  }

  function handleConfirmAction() {
    if (confirmAction) {
      handleStatusChange(confirmAction)
    }
  }

  async function handleDelete() {
    if (!id) return
    const rangesMsg = labsCount > 0
      ? `\n\nThis will permanently delete ${labsCount} ${labNoun}${labsCount > 1 ? 's' : ''} and all associated ${machines}.`
      : ''
    if (!confirm(`Are you sure you want to delete this event?${rangesMsg}\n\nThis action cannot be undone.`)) {
      return
    }
    setDeleting(true)
    try {
      await trainingEventsApi.delete(id)
      toast.success(`Deleted "${event?.name ?? 'event'}"`)
      navigate('/events')
    } catch (err) {
      toast.error(lifecycleFailureMessage('delete', event?.name ?? 'this event', err))
      // A teardown that stopped partway kept the event but destroyed some of its labs, so the
      // participant list on screen is already out of date.
      await loadEvent(id, false)
    } finally {
      setDeleting(false)
    }
  }

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-600"></div>
      </div>
    )
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Confirmation Dialog */}
      {confirmAction && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-md w-full mx-4 shadow-xl">
            <h3 className="text-lg font-semibold text-gray-900 mb-2">
              {confirmAction === 'complete' ? 'Complete Event?' : 'Cancel Event?'}
            </h3>
            <p className="text-gray-600 mb-4">
              {labsCount > 0 ? (
                <>
                  This will <span className="font-semibold text-red-600">permanently delete {labsCount} {labNoun}{labsCount > 1 ? 's' : ''}</span> and all associated {machines}.
                  {confirmAction === 'cancel' && ' The event will remain in cancelled status.'}
                </>
              ) : (
                `Are you sure you want to ${confirmAction} this event?`
              )}
            </p>
            <div className="flex justify-end space-x-3">
              <button
                onClick={() => setConfirmAction(null)}
                disabled={pending !== null}
                className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-md disabled:opacity-50"
              >
                Go Back
              </button>
              <button
                onClick={handleConfirmAction}
                disabled={pending !== null}
                className={`inline-flex items-center px-4 py-2 text-sm font-medium text-white rounded-md disabled:opacity-50 ${
                  confirmAction === 'cancel'
                    ? 'bg-red-600 hover:bg-red-700'
                    : 'bg-purple-600 hover:bg-purple-700'
                }`}
              >
                {pending !== null && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
                {confirmAction === 'complete' ? 'Complete & Delete Labs' : 'Cancel & Delete Labs'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-4">
          <button
            onClick={() => navigate('/events')}
            className="p-2 rounded-md hover:bg-gray-100"
          >
            <ArrowLeft className="h-5 w-5 text-gray-600" />
          </button>
          <h1 className="text-2xl font-semibold text-gray-900">
            {isNew ? 'Create Event' : 'Edit Event'}
          </h1>
          {event && (
            <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
              event.status === 'draft' ? 'bg-gray-100 text-gray-800' :
              event.status === 'scheduled' ? 'bg-blue-100 text-blue-800' :
              event.status === 'running' ? 'bg-green-100 text-green-800' :
              event.status === 'completed' ? 'bg-purple-100 text-purple-800' :
              'bg-red-100 text-red-800'
            }`}>
              {STATUS_LABELS[event.status]}
            </span>
          )}
        </div>
        <div className="flex items-center space-x-3">
          {!isNew && event && (
            <button
              onClick={loadBriefing}
              className="inline-flex items-center px-3 py-2 border border-gray-300 shadow-sm text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50"
            >
              <BookOpen className="h-4 w-4 mr-2" />
              View Briefing
            </button>
          )}
          {canManage && (!event || !['running', 'completed'].includes(event.status)) && (
            <button
              onClick={handleSave}
              disabled={saving}
              className="inline-flex items-center px-4 py-2 border border-transparent shadow-sm text-sm font-medium rounded-md text-white bg-primary-600 hover:bg-primary-700 disabled:opacity-50"
            >
              <Save className="h-4 w-4 mr-2" />
              {saving ? 'Saving...' : 'Save'}
            </button>
          )}
        </div>
      </div>

      {/* Status Actions */}
      {!isNew && event && canManage && (
        <div className="bg-white shadow rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-900 mb-3">Event Status</h3>
          {startError && (
            <div className="mb-3 flex items-start gap-2 rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">
              <AlertTriangle className="h-4 w-4 mt-0.5 flex-shrink-0" />
              <div className="min-w-0">
                <p className="font-medium">
                  {substrateLabel
                    ? `Could not start this event on ${substrateLabel}.`
                    : 'Could not start this event.'}
                </p>
                <p className="mt-1 whitespace-pre-line break-words">{startError.reason}</p>
                <p className="mt-1 text-red-700">{startError.aftermath}</p>
              </div>
            </div>
          )}
          {(event.status === 'draft' || event.status === 'scheduled') && (
            <div className="mb-3">
              <p className="text-xs font-medium text-gray-700 mb-1.5">Delivery</p>
              <div className="flex flex-wrap gap-2">
                {DELIVERY_MODES.map((mode) => (
                  <button
                    key={mode.value}
                    type="button"
                    onClick={() => setDelivery(mode.value)}
                    disabled={pending !== null}
                    aria-pressed={delivery === mode.value}
                    className={`inline-flex items-center px-3 py-1.5 border text-sm font-medium rounded disabled:opacity-50 ${
                      delivery === mode.value
                        ? 'border-primary-400 bg-primary-50 text-primary-800'
                        : 'border-gray-300 bg-white text-gray-600 hover:bg-gray-50'
                    }`}
                  >
                    {mode.value === 'team-exercise' ? (
                      <Users className="h-4 w-4 mr-1.5" />
                    ) : (
                      <Monitor className="h-4 w-4 mr-1.5" />
                    )}
                    {mode.label}
                  </button>
                ))}
              </div>
              <p className="mt-1.5 text-xs text-gray-500">
                {DELIVERY_MODES.find((mode) => mode.value === delivery)?.effect}
              </p>
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            {event.status === 'draft' && (
              <button
                onClick={() => handleStatusChange('publish')}
                disabled={pending !== null}
                className="inline-flex items-center px-3 py-1.5 border border-blue-300 text-sm font-medium rounded text-blue-700 bg-blue-50 hover:bg-blue-100 disabled:opacity-50"
              >
                <CalendarCheck className="h-4 w-4 mr-1" />
                {pending === 'publish' ? 'Publishing...' : 'Publish'}
              </button>
            )}
            {(event.status === 'draft' || event.status === 'scheduled') && (() => {
              const hasStudents = event.participants.some(p => p.role === 'student')
              const hasBlueprint = !!event.blueprint_id
              const canStart = hasStudents && hasBlueprint && pending === null
              const tooltip = !hasBlueprint
                ? 'Assign a blueprint to this event first'
                : !hasStudents
                ? 'Add at least one student participant first'
                : ''
              return (
                <div className="relative group">
                  <button
                    onClick={() => canStart && handleStatusChange('start', true)}
                    disabled={!canStart}
                    className={`inline-flex items-center px-3 py-1.5 border text-sm font-medium rounded ${
                      canStart
                        ? 'border-green-300 text-green-700 bg-green-50 hover:bg-green-100 cursor-pointer'
                        : 'border-gray-200 text-gray-400 bg-gray-50 cursor-not-allowed'
                    }`}
                  >
                    {pending === 'start' ? (
                      <Loader2 className="h-4 w-4 mr-1 animate-spin" />
                    ) : (
                      <Rocket className="h-4 w-4 mr-1" />
                    )}
                    {pending === 'start'
                      ? 'Starting...'
                      : delivery === 'team-exercise'
                      ? 'Start & Deploy Shared Lab'
                      : 'Start & Deploy Labs'}
                  </button>
                  {tooltip && (
                    <div className="absolute bottom-full left-1/2 transform -translate-x-1/2 mb-2 px-3 py-1.5 bg-gray-900 text-white text-xs rounded shadow-lg opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap z-10">
                      {tooltip}
                      <div className="absolute top-full left-1/2 transform -translate-x-1/2 border-4 border-transparent border-t-gray-900" />
                    </div>
                  )}
                </div>
              )
            })()}
            {event.status === 'running' && (
              <button
                onClick={() => handleStatusChange('complete')}
                disabled={pending !== null}
                className="inline-flex items-center px-3 py-1.5 border border-purple-300 text-sm font-medium rounded text-purple-700 bg-purple-50 hover:bg-purple-100 disabled:opacity-50"
              >
                <CheckCircle className="h-4 w-4 mr-1" />
                {pending === 'complete' ? 'Completing...' : 'Complete'}
              </button>
            )}
            {event.status !== 'completed' && event.status !== 'cancelled' && (
              <button
                onClick={() => handleStatusChange('cancel')}
                disabled={pending !== null}
                className="inline-flex items-center px-3 py-1.5 border border-red-300 text-sm font-medium rounded text-red-700 bg-red-50 hover:bg-red-100 disabled:opacity-50"
              >
                <XCircle className="h-4 w-4 mr-1" />
                {pending === 'cancel' ? 'Cancelling...' : 'Cancel'}
              </button>
            )}
            {event.status === 'cancelled' && (
              <button
                onClick={() => handleStatusChange('reactivate')}
                disabled={pending !== null}
                className="inline-flex items-center px-3 py-1.5 border border-amber-300 text-sm font-medium rounded text-amber-700 bg-amber-50 hover:bg-amber-100 disabled:opacity-50"
              >
                <RotateCcw className="h-4 w-4 mr-1" />
                {pending === 'reactivate' ? 'Reactivating...' : 'Reactivate'}
              </button>
            )}
            {/* Separator and Delete button */}
            <div className="w-px h-6 bg-gray-300 mx-2" />
            <button
              onClick={handleDelete}
              disabled={deleting || pending !== null}
              className="inline-flex items-center px-3 py-1.5 border border-red-300 text-sm font-medium rounded text-red-700 bg-red-50 hover:bg-red-100 disabled:opacity-50"
            >
              <Trash2 className="h-4 w-4 mr-1" />
              {deleting ? 'Deleting...' : 'Delete Event'}
            </button>
          </div>
          {/* Withheld until the event has been started or something was provisioned: on a draft
              event every student is legitimately without a lab, and saying so reads as a fault.
              A team exercise is reported as the one lab it is -- rolling the cohort up would
              read "12 running" about a single range twelve people are sharing. */}
          {isTeamExercise ? (
            <p className="mt-3 flex flex-wrap items-center gap-x-2 text-xs text-gray-500">
              <span>
                Shared lab{event.team_range_name ? ` "${event.team_range_name}"` : ''}:{' '}
                {sharedLab.label} &middot; {labs.total} student{labs.total === 1 ? '' : 's'}
              </span>
              {event.team_range_id && (
                <a
                  href={`/ranges/${event.team_range_id}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center text-primary-600 hover:text-primary-700"
                >
                  <ExternalLink className="h-3 w-3 mr-1" />
                  Open
                </a>
              )}
            </p>
          ) : (
            labs.total > 0 &&
            (labs.total > labs.missing || event.status === 'running') && (
              <p className="mt-3 text-xs text-gray-500">
                Student labs: {summarizeLabs(labs)}
              </p>
            )
          )}
        </div>
      )}

      {/* Main Form */}
      <div className="bg-white shadow rounded-lg p-6 space-y-6">
        {/* Basic Info */}
        <div>
          <h3 className="text-sm font-medium text-gray-900 mb-4">Basic Information</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="md:col-span-2">
              <label className="block text-sm font-medium text-gray-700 mb-1">Name *</label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                disabled={!canManage}
                className="w-full border border-gray-300 rounded-md py-2 px-3 focus:ring-primary-500 focus:border-primary-500 disabled:bg-gray-100"
              />
            </div>
            <div className="md:col-span-2">
              <label className="block text-sm font-medium text-gray-700 mb-1">Description</label>
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                disabled={!canManage}
                rows={3}
                className="w-full border border-gray-300 rounded-md py-2 px-3 focus:ring-primary-500 focus:border-primary-500 disabled:bg-gray-100"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Organization</label>
              <input
                type="text"
                value={organization}
                onChange={(e) => setOrganization(e.target.value)}
                disabled={!canManage}
                placeholder="e.g., Training Command, Cyber Operations"
                className="w-full border border-gray-300 rounded-md py-2 px-3 focus:ring-primary-500 focus:border-primary-500 disabled:bg-gray-100"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Location</label>
              <div className="relative">
                <MapPin className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-gray-400" />
                <input
                  type="text"
                  value={location}
                  onChange={(e) => setLocation(e.target.value)}
                  disabled={!canManage}
                  placeholder="e.g., Room 101, Virtual"
                  className="w-full pl-10 border border-gray-300 rounded-md py-2 px-3 focus:ring-primary-500 focus:border-primary-500 disabled:bg-gray-100"
                />
              </div>
            </div>
          </div>
        </div>

        {/* Schedule */}
        <div className="border-t pt-6">
          <h3 className="text-sm font-medium text-gray-900 mb-4">Schedule</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Start Date/Time *</label>
              <div className="relative">
                <Calendar className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-gray-400" />
                <input
                  type="datetime-local"
                  value={startDatetime}
                  onChange={(e) => setStartDatetime(e.target.value)}
                  disabled={!canManage}
                  className="w-full pl-10 border border-gray-300 rounded-md py-2 px-3 focus:ring-primary-500 focus:border-primary-500 disabled:bg-gray-100"
                />
              </div>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">End Date/Time</label>
              <div className="relative">
                <Clock className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-gray-400" />
                <input
                  type="datetime-local"
                  value={endDatetime}
                  onChange={(e) => setEndDatetime(e.target.value)}
                  disabled={!canManage}
                  className="w-full pl-10 border border-gray-300 rounded-md py-2 px-3 focus:ring-primary-500 focus:border-primary-500 disabled:bg-gray-100"
                />
              </div>
            </div>
            <div className="flex items-center">
              <input
                type="checkbox"
                id="isAllDay"
                checked={isAllDay}
                onChange={(e) => setIsAllDay(e.target.checked)}
                disabled={!canManage}
                className="h-4 w-4 text-primary-600 focus:ring-primary-500 border-gray-300 rounded"
              />
              <label htmlFor="isAllDay" className="ml-2 text-sm text-gray-700">
                All-day event
              </label>
            </div>
          </div>
        </div>

        {/* Blueprint & Content */}
        <div className="border-t pt-6">
          <h3 className="text-sm font-medium text-gray-900 mb-4">Lab & Content</h3>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                <LayoutTemplate className="inline h-4 w-4 mr-1" />
                Range Blueprint
              </label>
              <select
                value={blueprintId}
                onChange={(e) => {
                  const newBlueprintId = e.target.value
                  setBlueprintId(newBlueprintId)
                  // Auto-select linked content from blueprint
                  if (newBlueprintId) {
                    const selectedBlueprint = blueprints.find(bp => bp.id === newBlueprintId)
                    if (selectedBlueprint?.content_ids?.length) {
                      setSelectedContentIds(prev => {
                        const merged = new Set([...prev, ...selectedBlueprint.content_ids])
                        return Array.from(merged)
                      })
                    }
                  }
                }}
                disabled={!canManage}
                className="w-full border border-gray-300 rounded-md py-2 px-3 focus:ring-primary-500 focus:border-primary-500 disabled:bg-gray-100"
              >
                <option value="">No blueprint</option>
                {blueprints.map((bp) => (
                  <option key={bp.id} value={bp.id}>
                    {bp.name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                <BookOpen className="inline h-4 w-4 mr-1" />
                Linked Content
              </label>
              <div className="border border-gray-200 rounded-md max-h-48 overflow-y-auto">
                {contentItems.length === 0 ? (
                  <p className="p-3 text-sm text-gray-500">No published content available</p>
                ) : (
                  contentItems.map((item) => (
                    <label
                      key={item.id}
                      className="flex items-center px-3 py-2 hover:bg-gray-50 cursor-pointer"
                    >
                      <input
                        type="checkbox"
                        checked={selectedContentIds.includes(item.id)}
                        onChange={() => handleToggleContent(item.id)}
                        disabled={!canManage}
                        className="h-4 w-4 text-primary-600 focus:ring-primary-500 border-gray-300 rounded"
                      />
                      <span className="ml-3 text-sm text-gray-700">{item.title}</span>
                      <span className="ml-2 text-xs text-gray-500 capitalize">
                        ({item.content_type.replace('_', ' ')})
                      </span>
                    </label>
                  ))
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Access Control */}
        <div className="border-t pt-6">
          <h3 className="text-sm font-medium text-gray-900 mb-4">Access Control</h3>
          <div className="space-y-4">
            <div>
              {/* Named for what they are. These are the roles a user holds on this install, and
                  they decide who can open the event at all -- a different question, and a
                  different set, from the role someone is given *in* the event further down. */}
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Platform roles that can see this event
              </label>
              <div className="flex flex-wrap gap-2">
                {['student', 'engineer', 'evaluator', 'admin'].map((role) => (
                  <button
                    key={role}
                    type="button"
                    onClick={() => handleToggleRole(role)}
                    disabled={!canManage}
                    className={`inline-flex items-center px-3 py-1 rounded-full text-sm font-medium ${
                      allowedRoles.includes(role)
                        ? 'bg-primary-100 text-primary-800'
                        : 'bg-gray-100 text-gray-600'
                    } ${canManage ? 'cursor-pointer hover:opacity-80' : 'cursor-not-allowed'}`}
                  >
                    {role}
                  </button>
                ))}
              </div>
              <p className="mt-1 text-xs text-gray-500">
                Leave empty to make the event visible to all users. This is not the role a
                participant is given in the event &mdash; that is chosen under Participants.
              </p>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Tags</label>
              <div className="flex gap-2 mb-2 flex-wrap">
                {tags.map((tag) => (
                  <span
                    key={tag}
                    className="inline-flex items-center px-2 py-1 rounded text-xs bg-gray-100 text-gray-700"
                  >
                    {tag}
                    {canManage && (
                      <button
                        onClick={() => setTags(tags.filter((t) => t !== tag))}
                        className="ml-1 hover:text-red-500"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    )}
                  </span>
                ))}
              </div>
              {canManage && (
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={tagInput}
                    onChange={(e) => setTagInput(e.target.value)}
                    onKeyPress={(e) => e.key === 'Enter' && (e.preventDefault(), handleAddTag())}
                    placeholder="Add tag..."
                    className="flex-1 border border-gray-300 rounded-md py-1 px-2 text-sm"
                  />
                  <button
                    onClick={handleAddTag}
                    className="px-3 py-1 border border-gray-300 rounded-md text-sm hover:bg-gray-50"
                  >
                    <Tag className="h-4 w-4" />
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Participants */}
      {!isNew && event && (
        <div className="bg-white shadow rounded-lg p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-medium text-gray-900">
              <Users className="inline h-4 w-4 mr-1" />
              Participants ({event.participants.length})
            </h3>
          </div>
          {canManage && (
            <div className="mb-4 rounded-md border border-gray-200 p-3">
              <div className="flex gap-2">
                <select
                  aria-label="User to add"
                  value={selectedUserId}
                  onChange={(e) => setSelectedUserId(e.target.value)}
                  className="flex-1 border border-gray-300 rounded-md py-2 px-3 text-sm"
                >
                  <option value="">Select user...</option>
                  {users
                    .filter((u) => !event.participants.some((p) => p.user_id === u.id))
                    .map((u) => (
                      <option key={u.id} value={u.id}>
                        {u.username}
                      </option>
                    ))}
                </select>
                <select
                  aria-label="Role in this event"
                  value={participantRole}
                  onChange={(e) => setParticipantRole(e.target.value)}
                  className="border border-gray-300 rounded-md py-2 px-3 text-sm"
                >
                  {PARTICIPANT_ROLES.map((role) => (
                    <option key={role.value} value={role.value}>
                      {role.label}
                    </option>
                  ))}
                </select>
                <button
                  onClick={handleAddParticipant}
                  disabled={!selectedUserId}
                  className="inline-flex items-center px-3 py-2 border border-transparent text-sm font-medium rounded-md text-white bg-primary-600 hover:bg-primary-700 disabled:opacity-50"
                >
                  <Plus className="h-4 w-4" />
                </button>
              </div>
              {/* What the chosen role actually does. Without it an instructor is picking between
                  four labels whose consequences are invisible until the briefing is opened. */}
              <p className="mt-2 text-xs text-gray-500">
                {PARTICIPANT_ROLES.find((role) => role.value === participantRole)?.effect}
              </p>
            </div>
          )}
          {event.participants.length === 0 ? (
            <p className="text-sm text-gray-500">No participants yet</p>
          ) : (
            <div className="divide-y divide-gray-100">
              {event.participants.map((p) => (
                <div key={p.id} className="flex items-center justify-between py-3">
                  <div className="flex items-center gap-3">
                    <div>
                      <span className="text-sm font-medium text-gray-900">{p.username}</span>
                      <span className="ml-2 text-xs text-gray-500 capitalize">{p.role}</span>
                    </div>
                    {/* Per-participant deployment state: the event's own status says nothing
                        about whether this learner has anything to work on. */}
                    {p.range_id ? (() => {
                      const state = labState(p.range_status)
                      return (
                        <span
                          className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${state.tone}`}
                          title={p.range_name || undefined}
                        >
                          {state.spinner && <Loader2 className="h-3 w-3 mr-1 animate-spin" />}
                          {p.range_status === 'running' && <Monitor className="h-3 w-3 mr-1" />}
                          {p.range_status === 'error' && <AlertTriangle className="h-3 w-3 mr-1" />}
                          {state.label}
                          {/* Otherwise twelve students each showing "Running" reads as twelve
                              labs, when a team exercise is one lab twelve people are in. */}
                          {isTeamExercise && <span className="ml-1 font-normal opacity-70">(shared)</span>}
                        </span>
                      )
                    })() : p.role === 'student' && event.status === 'running' ? (
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-amber-100 text-amber-800">
                        <AlertTriangle className="h-3 w-3 mr-1" />
                        No lab
                      </span>
                    ) : null}
                  </div>
                  <div className="flex items-center gap-2">
                    {/* Open Console Button */}
                    {p.range_id && p.range_status === 'running' && (
                      <a
                        href={`/ranges/${p.range_id}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center px-2 py-1 text-xs font-medium text-primary-600 hover:text-primary-700 hover:bg-primary-50 rounded"
                      >
                        <ExternalLink className="h-3 w-3 mr-1" />
                        Open Lab
                      </a>
                    )}
                    {canManage && (
                      <button
                        onClick={() => handleRemoveParticipant(p.user_id)}
                        className="p-1 hover:bg-red-50 rounded text-red-500"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* VM Visibility Control - only show for running events with student participants */}
      {!isNew && event && event.status === 'running' && canManage && (
        <VMVisibilityControl
          event={event}
          canManage={canManage}
          onUpdate={() => loadEvent(id!, false)}
        />
      )}

      {/* Briefing Modal */}
      {showBriefing && (
        <div className="fixed inset-0 z-50 overflow-y-auto">
          <div className="flex items-center justify-center min-h-screen px-4">
            <div className="fixed inset-0 bg-gray-500 bg-opacity-75" onClick={() => setShowBriefing(false)} />
            <div className="relative bg-white rounded-lg shadow-xl max-w-3xl w-full max-h-[80vh] overflow-y-auto">
              <div className="sticky top-0 bg-white border-b px-6 py-4 flex items-center justify-between">
                <h3 className="text-lg font-medium text-gray-900">Event Briefing</h3>
                <button onClick={() => setShowBriefing(false)} className="p-1 hover:bg-gray-100 rounded">
                  <X className="h-5 w-5" />
                </button>
              </div>
              <div className="p-6 space-y-6">
                {briefingContent.length === 0 ? (
                  <p className="text-gray-500">No briefing content available for your role.</p>
                ) : (
                  briefingContent.map((item, idx) => (
                    <div key={idx}>
                      <h4 className="text-lg font-medium text-gray-900 mb-2">{item.title}</h4>
                      <div
                        className="prose prose-sm max-w-none"
                        dangerouslySetInnerHTML={{ __html: sanitizeHtml(item.html) }}
                      />
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
