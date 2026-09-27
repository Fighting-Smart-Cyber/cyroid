/**
 * Telling us something, from wherever you already are.
 *
 * Two things make this different from the mechanism it is modelled on:
 *
 * The context is READ, not asked. COSMOS's dialog asks "which project is this about?", which is
 * the one question a learner standing in a broken lab cannot answer. What they are doing is
 * already known -- the route, the range, the guide and the step -- so it is captured and shown
 * back as a line they can clear. Silent capture would be worse: this is a record a customer is
 * creating, and they should see what is in it before they send it.
 *
 * It does not navigate. A learner is inside a split view with a live console; going to a
 * /feedback route loses their place and, in the broken case, the thing they are reporting.
 */
import { useEffect, useMemo, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { AlertCircle, Check, Lightbulb, MessageSquarePlus, Bug, BookOpen, Server } from 'lucide-react'

import { Modal, ModalBody, ModalFooter } from '../common/Modal'
import { toast } from '../../stores/toastStore'
import {
  feedbackApi,
  rangesApi,
  type FeedbackContext,
  type FeedbackKind,
  type FeedbackSource,
} from '../../services/api'

/** What a caller that knows more than the route can supply. */
export interface FeedbackTarget {
  source?: FeedbackSource
  sourceRef?: string | null
  rangeId?: string | null
  contentId?: string | null
  context?: FeedbackContext
}

interface Props {
  isOpen: boolean
  onClose: () => void
  /** Supplied by a page that knows its own subject -- the lab knows its guide and step. */
  target?: FeedbackTarget
}

const KINDS: { value: FeedbackKind; label: string; hint: string; Icon: typeof Bug }[] = [
  { value: 'bug', label: 'Something is broken', hint: 'The platform did the wrong thing', Icon: Bug },
  { value: 'idea', label: 'An idea', hint: 'Something that would help', Icon: Lightbulb },
  {
    value: 'content_problem',
    label: 'A problem with the guide',
    hint: 'A step is wrong or unclear',
    Icon: BookOpen,
  },
  {
    value: 'range_problem',
    label: 'A problem with the range',
    hint: 'A machine or the environment',
    Icon: Server,
  },
]

const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i

/** What the route alone can tell us. A page that knows more passes it in. */
export function fromRoute(pathname: string): FeedbackTarget {
  const range = pathname.match(new RegExp(`/ranges/(${UUID.source})`))
  return {
    source: range ? 'range' : 'app',
    rangeId: range ? range[1] : null,
    context: { route: pathname },
  }
}

/**
 * What the report will carry, said plainly enough for the person sending it to decide.
 *
 * Deliberately not a dump of the payload: "Range: web-lab" is a thing a learner can agree to,
 * and a UUID is not.
 */
export function contextSummary(target: FeedbackTarget): string[] {
  const c = target.context ?? {}
  return [
    c.content_title && `Guide: ${c.content_title}${c.step ? ` — ${c.step}` : ''}`,
    c.range_name && `Range: ${c.range_name}`,
    !c.range_name && target.rangeId && 'The range you are looking at',
    c.route && `Page: ${c.route}`,
  ].filter(Boolean) as string[]
}

export function FeedbackDialog({ isOpen, onClose, target }: Props) {
  const location = useLocation()
  const [kind, setKind] = useState<FeedbackKind | null>(null)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [includeContext, setIncludeContext] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [reference, setReference] = useState<string | null>(null)
  const [rangeName, setRangeName] = useState<string | null>(null)

  const resolved = useMemo<FeedbackTarget>(() => {
    const base = fromRoute(location.pathname)
    return {
      ...base,
      ...target,
      context: { ...base.context, ...target?.context },
    }
  }, [location.pathname, target])

  useEffect(() => {
    if (!isOpen) return
    setKind(null)
    setTitle('')
    setDescription('')
    setIncludeContext(true)
    setReference(null)
    setRangeName(null)
  }, [isOpen])

  // `contextSummary` has always preferred "Range: <name>" over the UUID -- its own comment says
  // a name is a thing a learner can agree to and a UUID is not. Nothing ever supplied one, so
  // every report from a range page showed "The range you are looking at" over a raw id. The
  // route knows the id; one lookup turns it into the name. A failure is not worth surfacing:
  // the fallback line is what shipped, and the id still travels in the payload either way.
  useEffect(() => {
    const id = resolved.rangeId
    if (!isOpen || !id || resolved.context?.range_name) return
    let live = true
    rangesApi
      .get(id)
      .then(({ data }) => live && setRangeName(data.name ?? null))
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [isOpen, resolved.rangeId, resolved.context?.range_name])

  // What the submitter is about to attach, in their words rather than ours.
  const shown = useMemo<FeedbackTarget>(
    () =>
      rangeName && !resolved.context?.range_name
        ? { ...resolved, context: { ...resolved.context, range_name: rangeName } }
        : resolved,
    [resolved, rangeName]
  )
  const summary = useMemo(() => contextSummary(shown), [shown])

  /** Why Send is disabled, or null when it is not. Shown, never only implied. */
  const blocker = !kind
    ? 'Pick what kind of thing this is.'
    : !title.trim()
      ? 'A one-line summary is needed.'
      : null

  const submit = async () => {
    if (!kind || !title.trim() || submitting) return
    setSubmitting(true)
    try {
      const { data } = await feedbackApi.submit({
        kind,
        title: title.trim(),
        description: description.trim() || undefined,
        source: includeContext ? resolved.source ?? 'app' : 'app',
        source_ref: includeContext ? resolved.sourceRef ?? null : null,
        range_id: includeContext ? resolved.rangeId ?? null : null,
        content_id: includeContext ? resolved.contentId ?? null : null,
        context: includeContext ? shown.context ?? {} : {},
      })
      // COSMOS closes the dialog and shows nothing, which works only because submitting drops
      // you onto the board that now holds your item. From a dialog over a lab it just vanishes,
      // so say it landed and give them something to quote back.
      setReference(data.id)
    } catch {
      toast.error('That did not send. Nothing was recorded — please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  if (reference) {
    return (
      <Modal isOpen={isOpen} onClose={onClose} title="Thank you — that has been recorded" size="md">
        <ModalBody className="space-y-4">
          <div className="flex items-start gap-3">
            <Check className="h-5 w-5 text-green-600 mt-0.5 shrink-0" />
            <p className="text-sm text-gray-700">
              We have it. You can see it, and so can the people who work on it — nobody else.
            </p>
          </div>
          <p className="text-xs text-gray-500">
            Reference <span className="font-mono">{reference.slice(0, 8)}</span>
          </p>
        </ModalBody>
        <ModalFooter>
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 text-sm rounded-md bg-primary-600 text-white hover:bg-primary-700"
          >
            Close
          </button>
        </ModalFooter>
      </Modal>
    )
  }

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Tell us something" size="lg">
      {/* ModalBody/ModalFooter rather than a bare div: Modal deliberately pads only its header,
          and these are the wrappers that pad the rest. Nineteen other dialogs use them; this one
          did not, so every control sat flush against the modal's border. */}
      <ModalBody className="space-y-5">
        <fieldset>
          <legend className="text-sm font-medium text-gray-700 mb-2">What kind of thing?</legend>
          {/* items-stretch keeps a row even if a hint ever wraps again; it does NOT make the two
              ROWS match, which is why the hints above are each short enough for one line. */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 items-stretch">
            {KINDS.map(({ value, label, hint, Icon }) => (
              <button
                key={value}
                type="button"
                onClick={() => setKind(value)}
                aria-pressed={kind === value}
                className={`h-full text-left px-3 py-2 rounded-md border text-sm flex items-start gap-2 ${
                  kind === value
                    ? 'border-primary-500 bg-primary-50 text-primary-900'
                    : 'border-gray-300 hover:bg-gray-50'
                }`}
              >
                <Icon className="h-4 w-4 mt-0.5 shrink-0" />
                <span>
                  <span className="block font-medium">{label}</span>
                  <span className="block text-xs text-gray-500">{hint}</span>
                </span>
              </button>
            ))}
          </div>
        </fieldset>

        <div>
          <label htmlFor="feedback-title" className="block text-sm font-medium text-gray-700">
            In one line
          </label>
          <input
            id="feedback-title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            maxLength={200}
            placeholder="The console went black when I started the machine"
            autoFocus
            aria-required="true"
            className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 shadow-sm text-sm focus:border-primary-500 focus:ring-1 focus:ring-primary-500"
          />
        </div>

        <div>
          <label htmlFor="feedback-detail" className="block text-sm font-medium text-gray-700">
            Anything else <span className="text-gray-400">(optional)</span>
          </label>
          <textarea
            id="feedback-detail"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={4}
            maxLength={5000}
            placeholder="What you were doing, what you expected, what happened instead."
            className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 shadow-sm text-sm focus:border-primary-500 focus:ring-1 focus:ring-primary-500"
          />
        </div>

        {summary.length > 0 && (
          <div className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-xs font-medium text-gray-700">Sent with this report</p>
                <ul className="mt-1 text-xs text-gray-600 space-y-0.5">
                  {summary.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              </div>
              <label className="flex items-center gap-1.5 text-xs text-gray-600 shrink-0">
                <input
                  type="checkbox"
                  checked={includeContext}
                  onChange={(e) => setIncludeContext(e.target.checked)}
                  className="rounded border-gray-300 text-primary-600 focus:ring-primary-500"
                />
                Include
              </label>
            </div>
          </div>
        )}

        <p className="text-xs text-gray-500 flex items-center gap-1.5">
          <AlertCircle className="h-3.5 w-3.5 shrink-0" />
          {blocker ?? 'Only you and the people who work on this can see it.'}
        </p>
      </ModalBody>

      <ModalFooter>
        <button
          type="button"
          onClick={onClose}
          className="px-4 py-2 text-sm rounded-md border border-gray-300 bg-white hover:bg-gray-50"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={() => void submit()}
          disabled={blocker !== null || submitting}
          // A greyed button with no reason is the same defect as the update panel's
          // "Up to date" over a refusal: the one state where someone most needs telling
          // what to do is the one that looks most settled.
          title={blocker ?? undefined}
          className="px-4 py-2 text-sm rounded-md bg-primary-600 text-white hover:bg-primary-700 disabled:opacity-40 disabled:cursor-not-allowed inline-flex items-center gap-1.5"
        >
          <MessageSquarePlus className="h-4 w-4" />
          {submitting ? 'Sending…' : 'Send'}
        </button>
      </ModalFooter>
    </Modal>
  )
}
