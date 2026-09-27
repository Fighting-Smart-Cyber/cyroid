/**
 * What customers told us, and what we did about it.
 *
 * The submission half shipped in 0.53.0 and the reading half did not, which meant the only way
 * to see a report was `kubectl exec ... python -c "db.query(Feedback)"` — the instruction the
 * release's own test plan had to give. A channel nobody can read is not a channel; it is a
 * table that fills up.
 *
 * Staff-only by the route's `requiredRoles`, which match the server's own `STAFF_ROLES`
 * (admin, engineer). The server scopes rows on its side regardless: an author sees their own,
 * staff see everything, and asking for someone else's by id is a 404 rather than a 403 — whether
 * another person's report exists is not this account's business.
 *
 * Deliberately not a bug tracker. There is no assignee, no priority, no comment thread and no
 * duplicate detection, because the model has none of those and inventing them here would put the
 * product's triage state somewhere it cannot be queried. Status is the one thing a person needs
 * to change, so status is the one thing this page changes.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  AlertCircle,
  Bug,
  BookOpen,
  Inbox,
  Lightbulb,
  Loader2,
  RefreshCw,
  Server,
} from 'lucide-react'

import {
  feedbackApi,
  type Feedback,
  type FeedbackKind,
  type FeedbackStatus,
} from '../services/api'
import { toast } from '../stores/toastStore'

/** The four kinds, with the same words the submitter chose them by. */
const KIND: Record<FeedbackKind, { label: string; Icon: typeof Bug; tone: string }> = {
  bug: { label: 'Broken', Icon: Bug, tone: 'text-red-700 bg-red-50 border-red-200' },
  idea: { label: 'Idea', Icon: Lightbulb, tone: 'text-amber-700 bg-amber-50 border-amber-200' },
  content_problem: {
    label: 'Guide',
    Icon: BookOpen,
    tone: 'text-blue-700 bg-blue-50 border-blue-200',
  },
  range_problem: {
    label: 'Range',
    Icon: Server,
    tone: 'text-purple-700 bg-purple-50 border-purple-200',
  },
}

/**
 * Every status the model has, in the order work actually moves through them.
 *
 * `declined` last and separated in the UI: saying no to a customer's idea is a real outcome and
 * hiding it behind "resolved" loses the distinction the model bothered to make.
 */
const STATUSES: FeedbackStatus[] = [
  'open',
  'triaged',
  'planned',
  'in_progress',
  'resolved',
  'declined',
]

const STATUS_TONE: Record<FeedbackStatus, string> = {
  open: 'bg-gray-100 text-gray-800',
  triaged: 'bg-blue-100 text-blue-800',
  planned: 'bg-indigo-100 text-indigo-800',
  in_progress: 'bg-amber-100 text-amber-800',
  resolved: 'bg-green-100 text-green-800',
  declined: 'bg-gray-100 text-gray-500',
}

const STATUS_LABEL: Record<FeedbackStatus, string> = {
  open: 'Open',
  triaged: 'Triaged',
  planned: 'Planned',
  in_progress: 'In progress',
  resolved: 'Resolved',
  declined: 'Declined',
}

/** Age, to the coarsest unit that is still true. Triage cares about "old", not about seconds. */
export function age(iso: string, now: number = Date.now()): string {
  const seconds = Math.max(0, (now - new Date(iso).getTime()) / 1000)
  if (seconds < 90) return 'just now'
  const minutes = seconds / 60
  if (minutes < 90) return `${Math.round(minutes)}m ago`
  const hours = minutes / 60
  if (hours < 36) return `${Math.round(hours)}h ago`
  return `${Math.round(hours / 24)}d ago`
}

/**
 * The context a report carries, as lines rather than a JSON blob.
 *
 * The server already whitelisted the keys and truncated the values, so this is presentation
 * only — but a dev reading a list wants the guide and the step, not `{"route": "/ranges/..."}`.
 */
export function contextLines(item: Feedback): string[] {
  const c = (item.context ?? {}) as Record<string, string>
  return [
    c.content_title && `Guide: ${c.content_title}${c.step ? ` — ${c.step}` : ''}`,
    c.range_name && `Range: ${c.range_name}`,
    item.source_ref && `At: ${item.source_ref}`,
    c.app_version && `Version: ${c.app_version}`,
    c.route && `Page: ${c.route}`,
  ].filter(Boolean) as string[]
}

export default function FeedbackPage() {
  const [items, setItems] = useState<Feedback[] | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [kind, setKind] = useState<FeedbackKind | 'all'>('all')
  const [status, setStatus] = useState<FeedbackStatus | 'all'>('all')
  const [saving, setSaving] = useState<string | null>(null)

  const load = useCallback(async () => {
    setBusy(true)
    try {
      const { data } = await feedbackApi.list({
        kind: kind === 'all' ? undefined : kind,
        status: status === 'all' ? undefined : status,
      })
      setItems(data)
      setFailure(null)
    } catch {
      // Never render an empty list for a failed read: "nobody has told us anything" and "we
      // could not ask" must not look the same, which is the whole argument the update panel
      // had to have as well.
      setFailure('These could not be loaded.')
    } finally {
      setBusy(false)
    }
  }, [kind, status])

  useEffect(() => {
    void load()
  }, [load])

  const changeStatus = async (item: Feedback, next: FeedbackStatus) => {
    setSaving(item.id)
    try {
      const { data } = await feedbackApi.setStatus(item.id, next)
      setItems((prev) => (prev ?? []).map((f) => (f.id === data.id ? { ...f, ...data } : f)))
    } catch {
      toast.error('That status did not save. Nothing was changed.')
    } finally {
      setSaving(null)
    }
  }

  const counts = useMemo(() => {
    const by: Partial<Record<FeedbackStatus, number>> = {}
    for (const f of items ?? []) by[f.status] = (by[f.status] ?? 0) + 1
    return by
  }, [items])

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Feedback</h1>
          <p className="text-sm text-gray-600 mt-1">
            What people told us from inside the product, newest first.
          </p>
        </div>
        <button
          onClick={() => void load()}
          disabled={busy}
          className="inline-flex items-center gap-2 px-3 py-2 text-sm rounded-md border border-gray-300 bg-white hover:bg-gray-50 disabled:opacity-50"
        >
          <RefreshCw className={`h-4 w-4 ${busy ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value as FeedbackKind | 'all')}
          className="rounded-md border border-gray-300 px-3 py-2 text-sm bg-white"
          aria-label="Filter by kind"
        >
          <option value="all">Every kind</option>
          {(Object.keys(KIND) as FeedbackKind[]).map((k) => (
            <option key={k} value={k}>
              {KIND[k].label}
            </option>
          ))}
        </select>
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value as FeedbackStatus | 'all')}
          className="rounded-md border border-gray-300 px-3 py-2 text-sm bg-white"
          aria-label="Filter by status"
        >
          <option value="all">Every status</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {STATUS_LABEL[s]}
            </option>
          ))}
        </select>
        {items && items.length > 0 && (
          <span className="text-xs text-gray-500 ml-1">
            {STATUSES.filter((s) => counts[s]).map((s) => `${counts[s]} ${STATUS_LABEL[s].toLowerCase()}`).join(' · ')}
          </span>
        )}
      </div>

      {failure && (
        <div className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
          <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" />
          {failure}
        </div>
      )}

      {items === null && !failure && (
        <div className="flex items-center justify-center py-12 text-gray-600">
          <Loader2 className="h-6 w-6 animate-spin mr-2" />
          Loading…
        </div>
      )}

      {items !== null && items.length === 0 && !failure && (
        <div className="rounded-lg border border-gray-200 bg-white py-12 text-center">
          <Inbox className="h-8 w-8 text-gray-400 mx-auto" />
          <p className="mt-3 text-sm text-gray-600">
            {kind === 'all' && status === 'all'
              ? 'Nobody has sent anything yet.'
              : 'Nothing matches those filters.'}
          </p>
        </div>
      )}

      <div className="space-y-3">
        {(items ?? []).map((item) => {
          const { label, Icon, tone } = KIND[item.kind] ?? KIND.bug
          const lines = contextLines(item)
          return (
            <div key={item.id} className="rounded-lg border border-gray-200 bg-white p-4">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span
                      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-xs font-medium ${tone}`}
                    >
                      <Icon className="h-3 w-3" />
                      {label}
                    </span>
                    <span
                      className={`px-2 py-0.5 rounded text-xs font-medium ${STATUS_TONE[item.status]}`}
                    >
                      {STATUS_LABEL[item.status]}
                    </span>
                    <span className="text-xs text-gray-500">
                      {item.author_username ?? 'someone'} · {age(item.created_at)}
                    </span>
                  </div>
                  <p className="mt-2 text-sm font-medium text-gray-900 break-words">
                    {item.title}
                  </p>
                  {item.description && (
                    <p className="mt-1 text-sm text-gray-600 whitespace-pre-wrap break-words">
                      {item.description}
                    </p>
                  )}
                  {lines.length > 0 && (
                    <ul className="mt-2 text-xs text-gray-500 space-y-0.5">
                      {lines.map((l) => (
                        <li key={l}>{l}</li>
                      ))}
                    </ul>
                  )}
                  {item.range_id && (
                    <Link
                      to={`/ranges/${item.range_id}`}
                      className="mt-2 inline-block text-xs text-blue-600 hover:underline"
                    >
                      Open the range this is about →
                    </Link>
                  )}
                </div>

                <div className="shrink-0 flex items-center gap-2">
                  {saving === item.id && (
                    <Loader2 className="h-4 w-4 animate-spin text-gray-400" />
                  )}
                  <select
                    value={item.status}
                    disabled={saving === item.id}
                    onChange={(e) => void changeStatus(item, e.target.value as FeedbackStatus)}
                    className="rounded-md border border-gray-300 px-2 py-1.5 text-xs bg-white disabled:opacity-50"
                    aria-label={`Status of ${item.title}`}
                  >
                    {STATUSES.map((s) => (
                      <option key={s} value={s}>
                        {STATUS_LABEL[s]}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
