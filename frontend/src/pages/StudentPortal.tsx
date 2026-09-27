// frontend/src/pages/StudentPortal.tsx
/**
 * The learner's landing page: the labs assigned to whoever is signed in, and the training events
 * they take part in.
 *
 * Everything on this page is scoped to the caller by the server -- `/ranges/my-ranges` resolves
 * assignments and event participation for `current_user`, and `/training-events/my-events` reads
 * that same participant list. There is therefore nothing here that one account could learn about
 * another, which is why the page is written to be openable by anyone entitled to see a learner's
 * view rather than by one role. An administrator or an instructor who opens it is checking the
 * page a learner will meet, which is their job; refusing them meant the learner-facing landing
 * page could not be looked at by anybody setting the product up on a fresh install, because the
 * one account that always exists there is not a learner. The route's own gate lives in
 * App.tsx; this file assumes only "some signed-in person is looking", so it is correct whether
 * that gate names one role or none.
 *
 * The consequence for the copy is that no sentence here may assume the reader is enrolled. An
 * empty page has to explain what would appear and how it gets there, for a learner who expected
 * a lab and for an administrator who has assigned themselves nothing.
 *
 * Substrate: a Kubernetes range has no VM or Network rows, so the card's old "0 VMs / 0 Networks"
 * was false beside a lab whose machines were running. `/ranges/my-ranges` now fills both counts
 * from the blueprint on that substrate and says which substrate produced them, and the live
 * contents come from the same hook the learner's lab uses. The card names the unit only once it
 * knows which substrate is answering -- the counts and the substrate answer arrive on separate
 * requests, and guessing Docker in the gap printed "VMs" on a Kubernetes install.
 */
import { useState, useEffect, useMemo, useCallback } from 'react'
import { Link } from 'react-router-dom'
import { useAuthStore } from '../stores/authStore'
import { useSubstrate } from '../stores/capabilitiesStore'
import { useRangeWorkloads } from '../hooks/useRangeWorkloads'
import { trainingEventsApi, rangesApi, TrainingEventListItem } from '../services/api'
import type { Range } from '../types'
import {
  AlertCircle,
  BookOpen,
  Calendar,
  ChevronLeft,
  ChevronRight,
  Loader2,
  MapPin,
  Monitor,
  RefreshCw,
  Server,
  Users,
} from 'lucide-react'
import clsx from 'clsx'

/** Whether a learner can open a lab now, and what to tell them when they cannot. */
export interface LabAvailability {
  /** The word on the pill. A learner's word for the state, not the deployment's. */
  label: string
  canOpen: boolean
  /** Shown in place of the open control. Null when the lab can be opened. */
  note: string | null
  /**
   * Whether the lab's guide is still worth offering when the lab itself cannot be opened. The
   * lab page draws the guide whether or not there is a console beside it, so a learner whose lab
   * is stopped between sessions can still read it. False for a state where there is nothing left
   * to read, and for a state this build does not recognise.
   */
  guideReachable: boolean
}

/**
 * A range's status, as it matters to the person assigned to it.
 *
 * The card used to print the raw status and offer "View Details" for every one of them, which
 * led to an empty lab for a range that had never been deployed. A learner cannot act on
 * "draft" or "error"; they can act on being told the lab is not ready and who to ask. Anything
 * unrecognised is treated as not openable, because offering a control that then fails is worse
 * than saying so.
 */
export function labAvailability(status: string): LabAvailability {
  switch (status) {
    case 'running':
      return { label: 'Ready', canOpen: true, note: null, guideReachable: true }
    case 'deploying':
      return {
        label: 'Starting',
        canOpen: false,
        note: 'This lab is still being built. It is usually ready within a few minutes — use Refresh to check.',
        guideReachable: true,
      }
    case 'stopped':
      return {
        label: 'Stopped',
        canOpen: false,
        note: 'This lab is not running at the moment. Your instructor starts it when the session begins.',
        guideReachable: true,
      }
    case 'draft':
      return {
        label: 'Not started',
        canOpen: false,
        note: 'This lab has not been set up yet. It will be ready before your session.',
        guideReachable: true,
      }
    case 'archived':
      return {
        label: 'Closed',
        canOpen: false,
        note: 'This lab has been closed and can no longer be opened.',
        guideReachable: false,
      }
    case 'error':
      return {
        label: 'Unavailable',
        canOpen: false,
        note: 'This lab could not be started. Tell your instructor — they can see what went wrong.',
        guideReachable: true,
      }
    default:
      return {
        label: 'Unavailable',
        canOpen: false,
        note: 'This lab cannot be opened at the moment. Tell your instructor if you were expecting to work in it.',
        guideReachable: false,
      }
  }
}

/** What an event card may offer for the lab assigned to the person reading it. */
export interface EventLabOffer {
  /** The lab to open, or null when there is nothing that can be opened. */
  openRangeId: string | null
  /** Shown in place of the control. Null when there is nothing to say. */
  note: string | null
}

/**
 * The event card offered a green "Open lab" for any running event with a lab assigned, without
 * consulting the lab's own state -- so an event that had started before its lab did sent the
 * learner to a lab with no machines and no explanation, directly beneath a card in "My labs"
 * that said the same lab was still starting. The two halves of this page now answer the same way.
 *
 * `statusByRangeId` is what "My labs" already loaded. A lab missing from it is not evidence of
 * anything: the request may still be in flight or may have failed, and withdrawing the learner's
 * only way in on a guess is worse than offering one that turns out to be early.
 */
export function eventLabOffer(
  eventStatus: string,
  myRangeId: string | undefined,
  statusByRangeId: Record<string, string | undefined>
): EventLabOffer {
  if (eventStatus !== 'running' || !myRangeId) return { openRangeId: null, note: null }
  const known = statusByRangeId[myRangeId]
  if (known === undefined) return { openRangeId: myRangeId, note: null }
  const availability = labAvailability(known)
  return availability.canOpen
    ? { openRangeId: myRangeId, note: null }
    : { openRangeId: null, note: availability.note }
}

const LAB_PILL: Record<string, string> = {
  Ready: 'bg-green-100 text-green-800',
  Starting: 'bg-yellow-100 text-yellow-800',
  Stopped: 'bg-gray-100 text-gray-700',
  'Not started': 'bg-gray-100 text-gray-700',
  Closed: 'bg-gray-100 text-gray-600',
  Unavailable: 'bg-red-100 text-red-800',
}

/** What a lab holds, in the words of the substrate it actually runs on. */
export type LabContents =
  | { substrate: 'kubernetes'; machines: number; applications: number }
  | { substrate: 'dind'; vms: number; networks: number }

function quantity(n: number, noun: string): string | null {
  if (!Number.isFinite(n) || n <= 0) return null
  return `${n} ${noun}${n === 1 ? '' : 's'}`
}

/**
 * The one line under a lab's name saying what is in it.
 *
 * Returns null rather than "0 machines" so that a lab whose contents are not yet known, or which
 * genuinely declares none, says nothing instead of asserting emptiness. A learner reads "0
 * machines" as a broken lab; the lab page itself is where the real answer lives.
 */
export function labSummary(contents: LabContents): string | null {
  const parts =
    contents.substrate === 'kubernetes'
      ? [quantity(contents.machines, 'machine'), quantity(contents.applications, 'application')]
      : [quantity(contents.vms, 'VM'), quantity(contents.networks, 'network')]
  const shown = parts.filter((part): part is string => part !== null)
  return shown.length > 0 ? shown.join(' · ') : null
}

/** An event's status as a learner would say it. "Running" is a deployment's word, not theirs. */
export function eventStatusLabel(status: string): string {
  if (status === 'running') return 'In progress'
  if (status === 'scheduled') return 'Scheduled'
  return status.charAt(0).toUpperCase() + status.slice(1)
}

const EVENT_PILL: Record<string, string> = {
  scheduled: 'bg-blue-100 text-blue-800',
  running: 'bg-green-100 text-green-800',
}

/**
 * The cells of a month grid: leading nulls for the blank days before the 1st, then the dates.
 *
 * `month` is zero-based, as `Date` has it.
 */
export function calendarCells(year: number, month: number): (number | null)[] {
  const startingDay = new Date(year, month, 1).getDay()
  const daysInMonth = new Date(year, month + 1, 0).getDate()
  const cells: (number | null)[] = new Array(startingDay).fill(null)
  for (let day = 1; day <= daysInMonth; day++) {
    cells.push(day)
  }
  return cells
}

/**
 * The events that start on one day of the grid.
 *
 * Both bounds are built from separate `Date` values on purpose: the previous version called
 * `setHours` twice on one object, so the end of the day was derived from an object the start had
 * already mutated. It happened to agree here, and would not have survived anyone moving the two
 * lines apart.
 */
export function eventsOnDay<T extends { start_datetime: string }>(
  events: T[],
  year: number,
  month: number,
  day: number
): T[] {
  const start = new Date(year, month, day, 0, 0, 0, 0).getTime()
  const end = new Date(year, month, day, 23, 59, 59, 999).getTime()
  return events.filter((event) => {
    const at = new Date(event.start_datetime).getTime()
    return at >= start && at <= end
  })
}

const MONTH_NAMES = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
]
const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

interface LabCardProps {
  range: Range
  /**
   * The install's substrate, or null until `/system/capabilities` has answered.
   *
   * Null is a state of its own and not "Docker for now". The range rows arrive on their own
   * request and routinely land first, and on a Kubernetes install their counts are real numbers
   * read from the blueprint -- so treating the unanswered moment as Docker printed "2 VMs · 1
   * network" under a Kubernetes lab and then rewrote it. That is the Era A word on the substrate
   * that does not use it, which is the whole defect this card exists to stop repeating.
   */
  substrate: string | null
  /** Changing this refetches, so the page's Refresh reaches the cluster answer too. */
  reloadToken: number
}

function LabCard({ range, substrate, reloadToken }: LabCardProps) {
  const readFromCluster = substrate === 'kubernetes'
  const { data } = useRangeWorkloads(
    readFromCluster ? range.id : undefined,
    `${range.status}:${reloadToken}`
  )
  const availability = labAvailability(range.status)

  // A Kubernetes install can still hold a range whose blueprint is not deployable there, and the
  // backend says so per range. Trust that answer over the install-wide one, and say nothing at
  // all until one of the two has spoken.
  let summary: string | null = null
  if (data?.substrate === 'kubernetes') {
    summary = labSummary({
      substrate: 'kubernetes',
      machines: data.workloads.length,
      applications: data.apps?.length ?? 0,
    })
  } else if (substrate === 'dind' || data?.substrate === 'dind') {
    summary = labSummary({
      substrate: 'dind',
      vms: range.vm_count,
      networks: range.network_count,
    })
  }

  return (
    <div className="border border-gray-200 rounded-lg p-4 bg-white flex flex-col hover:shadow-md transition-shadow">
      <div className="flex items-start justify-between gap-2">
        <h3 className="font-semibold text-gray-900 line-clamp-2">{range.name}</h3>
        <span
          className={clsx(
            'px-2 py-1 text-xs font-medium rounded-full whitespace-nowrap',
            LAB_PILL[availability.label] ?? 'bg-gray-100 text-gray-700'
          )}
        >
          {availability.label}
        </span>
      </div>

      {range.description && (
        <p className="mt-2 text-sm text-gray-600 line-clamp-2">{range.description}</p>
      )}

      {summary && <p className="mt-2 text-xs text-gray-500">{summary}</p>}

      <div className="mt-4 pt-3 border-t border-gray-100">
        {availability.canOpen ? (
          <Link
            to={`/lab/${range.id}`}
            className="inline-flex items-center px-3 py-1.5 text-sm font-medium text-white bg-green-600 rounded-md hover:bg-green-700 transition-colors"
          >
            <Monitor className="h-4 w-4 mr-1.5" />
            Open lab
          </Link>
        ) : (
          <div className="space-y-2">
            <p className="text-sm text-gray-500">{availability.note}</p>
            {/* The guide is not the machines. A learner could previously reach their reading
                material while the lab was shut, because the lab page draws the guide with or
                without a console beside it; removing every way in took that with it. Offered
                only where the range names a guide, so the link cannot lead to a bare screen. */}
            {availability.guideReachable && range.student_guide_id && (
              <Link
                to={`/lab/${range.id}`}
                className="inline-flex items-center text-sm font-medium text-primary-600 hover:text-primary-700"
              >
                <BookOpen className="h-4 w-4 mr-1.5" />
                Read the guide
              </Link>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

export default function StudentPortal() {
  const { user } = useAuthStore()
  const [events, setEvents] = useState<TrainingEventListItem[]>([])
  const [myRanges, setMyRanges] = useState<Range[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [isLoadingRanges, setIsLoadingRanges] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [rangesError, setRangesError] = useState<string | null>(null)
  const [activeView, setActiveView] = useState<'list' | 'calendar'>('list')
  const [calendarDate, setCalendarDate] = useState(new Date())
  const [reloadToken, setReloadToken] = useState(0)

  // Null until /system/capabilities answers, and the cards depend on that third state.
  const { substrate } = useSubstrate()

  const loadMyEvents = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const response = await trainingEventsApi.getMyEvents()
      // A learner is here to work, not to browse history: only what is on or coming up.
      setEvents(
        response.data.filter((e) => e.status === 'scheduled' || e.status === 'running')
      )
    } catch (err: unknown) {
      // The server's own wording is for an operator. A learner gets told what it means for them
      // and how to get out of it.
      setError('Your training events could not be loaded just now. Try again in a moment.')
      console.error('Error loading events:', err)
    } finally {
      setIsLoading(false)
    }
  }, [])

  const loadMyRanges = useCallback(async () => {
    setIsLoadingRanges(true)
    setRangesError(null)
    try {
      const response = await rangesApi.getMyRanges()
      setMyRanges(response.data)
    } catch (err: unknown) {
      // A failed request used to be swallowed into the console, so the page then showed the
      // "nothing is assigned to you" state -- a learner with a lab was told they had none.
      setRangesError('Your labs could not be loaded just now. Try again in a moment.')
      console.error('Error loading labs:', err)
    } finally {
      setIsLoadingRanges(false)
    }
  }, [])

  useEffect(() => {
    void loadMyEvents()
    void loadMyRanges()
  }, [loadMyEvents, loadMyRanges])

  const refreshAll = useCallback(() => {
    setReloadToken((token) => token + 1)
    void loadMyEvents()
    void loadMyRanges()
  }, [loadMyEvents, loadMyRanges])

  const formatDate = (dateStr: string) => {
    const date = new Date(dateStr)
    return date.toLocaleDateString('en-US', {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    })
  }

  const formatTime = (dateStr: string) => {
    const date = new Date(dateStr)
    return date.toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
    })
  }

  const calendarDays = useMemo(
    () => calendarCells(calendarDate.getFullYear(), calendarDate.getMonth()),
    [calendarDate]
  )

  // "My labs" above already knows the state of every lab this person can reach, including the
  // ones they hold through an event. The event cards read it rather than asking again.
  const rangeStatusById = useMemo(
    () => Object.fromEntries(myRanges.map((range) => [range.id, range.status])),
    [myRanges]
  )

  const navigateMonth = (direction: 'prev' | 'next') => {
    setCalendarDate((prev) => {
      const newDate = new Date(prev)
      newDate.setDate(1)
      newDate.setMonth(newDate.getMonth() + (direction === 'prev' ? -1 : 1))
      return newDate
    })
  }

  const isRefreshing = isLoading || isLoadingRanges

  return (
    <div className="space-y-6">
      {/* Welcome Header */}
      <div className="bg-gradient-to-r from-primary-600 to-primary-700 rounded-lg shadow-lg p-6 text-white">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold">Welcome, {user?.username}</h1>
            <p className="mt-1 text-primary-100">
              Your labs and training events are here. Open a lab to start work — everything
              assigned to you appears on this page.
            </p>
          </div>
          <button
            onClick={refreshAll}
            disabled={isRefreshing}
            className="flex-shrink-0 inline-flex items-center px-3 py-1.5 text-sm font-medium rounded-md bg-white/10 hover:bg-white/20 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <RefreshCw className={clsx('h-4 w-4 mr-1.5', isRefreshing && 'animate-spin')} />
            Refresh
          </button>
        </div>
      </div>

      {/* My Labs */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-200">
          <h2 className="text-lg font-semibold text-gray-900 flex items-center gap-2">
            <Server className="w-5 h-5 text-primary-600" />
            My labs
          </h2>
        </div>

        {isLoadingRanges ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-6 w-6 animate-spin text-primary-600" />
          </div>
        ) : rangesError ? (
          <div className="m-4 bg-red-50 border border-red-200 rounded-lg p-4">
            <div className="flex items-start gap-2 text-red-800">
              <AlertCircle className="h-5 w-5 flex-shrink-0 mt-0.5 text-red-500" />
              <div>
                <p>{rangesError}</p>
                <p className="mt-1 text-sm text-red-700">
                  Nothing has been lost. Any work already saved in a lab is still there.
                </p>
                <button
                  onClick={() => void loadMyRanges()}
                  className="mt-2 text-sm font-medium text-red-700 underline hover:text-red-900"
                >
                  Try again
                </button>
              </div>
            </div>
          </div>
        ) : myRanges.length === 0 ? (
          <div className="p-8 text-center max-w-xl mx-auto">
            <Server className="mx-auto h-10 w-10 text-gray-400" />
            <h3 className="mt-3 text-gray-900 font-medium">No labs assigned yet</h3>
            <p className="mt-2 text-sm text-gray-500">
              A lab is your own environment for a course — the machines and applications you work
              in, alongside the guide that walks you through them.
            </p>
            <p className="mt-2 text-sm text-gray-500">
              Nothing is assigned to this account at the moment. A lab appears here as soon as an
              instructor assigns one to you, or adds you to a training event that has one. If you
              were expecting one today, check your training events below or ask your instructor.
            </p>
          </div>
        ) : (
          <div className="p-4">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {myRanges.map((range) => (
                <LabCard
                  key={range.id}
                  range={range}
                  substrate={substrate}
                  reloadToken={reloadToken}
                />
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Training events */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-200 flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-lg font-semibold text-gray-900 flex items-center gap-2">
            <Calendar className="w-5 h-5 text-primary-600" />
            Your training events
          </h2>
          <div className="flex items-center gap-4">
            <Link
              to="/events"
              className="text-sm font-medium text-primary-600 hover:text-primary-700"
            >
              Browse all events
            </Link>
            {/* The toggle only appears once there is something to toggle between: an empty
                calendar behind an empty list is two ways of being told the same nothing. */}
            {!isLoading && !error && events.length > 0 && (
              <div className="flex items-center gap-2 bg-gray-100 p-1 rounded-lg">
                <button
                  onClick={() => setActiveView('list')}
                  aria-pressed={activeView === 'list'}
                  className={clsx(
                    'px-3 py-1 text-sm font-medium rounded-md transition-colors',
                    activeView === 'list'
                      ? 'bg-white shadow text-gray-900'
                      : 'text-gray-600 hover:text-gray-900'
                  )}
                >
                  List
                </button>
                <button
                  onClick={() => setActiveView('calendar')}
                  aria-pressed={activeView === 'calendar'}
                  className={clsx(
                    'px-3 py-1 text-sm font-medium rounded-md transition-colors',
                    activeView === 'calendar'
                      ? 'bg-white shadow text-gray-900'
                      : 'text-gray-600 hover:text-gray-900'
                  )}
                >
                  Calendar
                </button>
              </div>
            )}
          </div>
        </div>

        {isLoading && (
          <div className="flex justify-center py-12">
            <Loader2 className="h-8 w-8 animate-spin text-primary-600" />
          </div>
        )}

        {!isLoading && error && (
          <div className="m-4 bg-red-50 border border-red-200 rounded-lg p-4">
            <div className="flex items-start gap-2 text-red-800">
              <AlertCircle className="h-5 w-5 flex-shrink-0 mt-0.5 text-red-500" />
              <div>
                <p>{error}</p>
                <button
                  onClick={() => void loadMyEvents()}
                  className="mt-2 text-sm font-medium text-red-700 underline hover:text-red-900"
                >
                  Try again
                </button>
              </div>
            </div>
          </div>
        )}

        {!isLoading && !error && events.length === 0 && (
          <div className="p-8 text-center max-w-xl mx-auto">
            <Calendar className="mx-auto h-10 w-10 text-gray-400" />
            <h3 className="mt-3 text-gray-900 font-medium">No training events</h3>
            <p className="mt-2 text-sm text-gray-500">
              Events you are taking part in show here with their dates, so you know when your next
              session runs and can open its lab from the same place.
            </p>
            <p className="mt-2 text-sm text-gray-500">
              Nothing is scheduled for this account.{' '}
              <Link to="/events" className="text-primary-600 hover:text-primary-700 font-medium">
                Browse all events
              </Link>{' '}
              to see what is planned.
            </p>
          </div>
        )}

        {/* List View */}
        {!isLoading && !error && events.length > 0 && activeView === 'list' && (
          <div className="p-4">
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              {events.map((event) => {
                const labOffer = eventLabOffer(event.status, event.my_range_id, rangeStatusById)
                return (
                  <div
                    key={event.id}
                    className="border border-gray-200 rounded-lg overflow-hidden hover:shadow-md transition-shadow bg-white"
                  >
                    <div className="p-4">
                      <div className="flex items-start justify-between gap-2">
                        <h3 className="font-semibold text-gray-900 line-clamp-2">{event.name}</h3>
                        <span
                          className={clsx(
                            'px-2 py-1 text-xs font-medium rounded-full whitespace-nowrap',
                            EVENT_PILL[event.status] ?? 'bg-gray-100 text-gray-800'
                          )}
                        >
                          {eventStatusLabel(event.status)}
                        </span>
                      </div>

                      {event.description && (
                        <p className="mt-2 text-sm text-gray-600 line-clamp-2">
                          {event.description}
                        </p>
                      )}

                      <div className="mt-3 space-y-1.5 text-sm text-gray-500">
                        <div className="flex items-center">
                          <Calendar className="h-4 w-4 mr-2 text-gray-400" />
                          <span>
                            {formatDate(event.start_datetime)}
                            {!event.is_all_day && ` at ${formatTime(event.start_datetime)}`}
                          </span>
                        </div>

                        {event.location && (
                          <div className="flex items-center">
                            <MapPin className="h-4 w-4 mr-2 text-gray-400" />
                            <span className="truncate">{event.location}</span>
                          </div>
                        )}

                        {event.organization && (
                          <div className="flex items-center">
                            <Users className="h-4 w-4 mr-2 text-gray-400" />
                            <span className="truncate">{event.organization}</span>
                          </div>
                        )}
                      </div>
                    </div>

                    <div className="px-4 py-3 bg-gray-50 border-t border-gray-200 flex items-center justify-between gap-2">
                      <Link
                        to={`/events/${event.id}`}
                        className="text-sm text-primary-600 hover:text-primary-700 font-medium flex items-center"
                      >
                        <BookOpen className="h-4 w-4 mr-1" />
                        View details
                      </Link>

                      {/* Offered only when there is a lab to open and it is open: an event running
                          without one assigned to this person has nothing behind the button, and one
                          whose lab has not started yet leads to a lab with nothing in it. */}
                      {labOffer.openRangeId && (
                        <Link
                          to={`/lab/${labOffer.openRangeId}`}
                          className="inline-flex items-center px-3 py-1.5 text-sm font-medium text-white bg-green-600 rounded-md hover:bg-green-700 transition-colors"
                        >
                          <Monitor className="h-4 w-4 mr-1" />
                          Open lab
                        </Link>
                      )}
                      {labOffer.note && (
                        <p className="text-sm text-gray-500 text-right">{labOffer.note}</p>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* Calendar View */}
        {!isLoading && !error && events.length > 0 && activeView === 'calendar' && (
          <div className="p-4">
            <div className="flex items-center justify-between mb-4">
              <button
                onClick={() => navigateMonth('prev')}
                aria-label="Previous month"
                className="p-2 hover:bg-gray-100 rounded-lg transition-colors"
              >
                <ChevronLeft className="w-5 h-5 text-gray-600" />
              </button>
              <h3 className="text-lg font-semibold text-gray-900">
                {MONTH_NAMES[calendarDate.getMonth()]} {calendarDate.getFullYear()}
              </h3>
              <button
                onClick={() => navigateMonth('next')}
                aria-label="Next month"
                className="p-2 hover:bg-gray-100 rounded-lg transition-colors"
              >
                <ChevronRight className="w-5 h-5 text-gray-600" />
              </button>
            </div>

            <div className="border border-gray-200 rounded-lg overflow-hidden">
              <div className="grid grid-cols-7 bg-gray-50 border-b border-gray-200">
                {DAY_NAMES.map((day) => (
                  <div
                    key={day}
                    className="px-2 py-3 text-center text-xs font-semibold text-gray-600 uppercase"
                  >
                    {day}
                  </div>
                ))}
              </div>

              <div className="grid grid-cols-7">
                {calendarDays.map((day, index) => {
                  const dayEvents = day
                    ? eventsOnDay(
                        events,
                        calendarDate.getFullYear(),
                        calendarDate.getMonth(),
                        day
                      )
                    : []
                  const today = new Date()
                  const isToday =
                    day !== null &&
                    today.getDate() === day &&
                    today.getMonth() === calendarDate.getMonth() &&
                    today.getFullYear() === calendarDate.getFullYear()

                  return (
                    <div
                      key={index}
                      className={clsx(
                        'min-h-[80px] p-1 border-b border-r border-gray-200',
                        index % 7 === 6 && 'border-r-0',
                        !day && 'bg-gray-50'
                      )}
                    >
                      {day && (
                        <>
                          <div
                            className={clsx(
                              'text-sm font-medium mb-1 w-6 h-6 flex items-center justify-center rounded-full',
                              isToday ? 'bg-primary-600 text-white' : 'text-gray-700'
                            )}
                          >
                            {day}
                          </div>
                          <div className="space-y-1">
                            {dayEvents.slice(0, 2).map((event) => (
                              <Link
                                key={event.id}
                                to={`/events/${event.id}`}
                                className={clsx(
                                  'block text-xs px-1.5 py-0.5 rounded truncate',
                                  event.status === 'running'
                                    ? 'bg-green-100 text-green-800 hover:bg-green-200'
                                    : 'bg-blue-100 text-blue-800 hover:bg-blue-200'
                                )}
                                title={event.name}
                              >
                                {event.name}
                              </Link>
                            ))}
                            {dayEvents.length > 2 && (
                              <span className="text-xs text-gray-500 px-1.5">
                                +{dayEvents.length - 2} more
                              </span>
                            )}
                          </div>
                        </>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>

            <div className="mt-4 flex items-center gap-4 text-xs text-gray-600">
              <div className="flex items-center gap-1.5">
                <span className="w-3 h-3 rounded bg-blue-100 border border-blue-200"></span>
                <span>Scheduled</span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="w-3 h-3 rounded bg-green-100 border border-green-200"></span>
                <span>In progress</span>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
