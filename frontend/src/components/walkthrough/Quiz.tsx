// frontend/src/components/walkthrough/Quiz.tsx
import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { isAxiosError } from 'axios'
import { Check, X, HelpCircle, AlertCircle, Loader2 } from 'lucide-react'
import { QuizQuestion } from '../../types'
import { api } from '../../services/api'

// One graded answer, as the server returns it and as it comes back embedded in
// a question the learner has already answered.
interface QuizAnswerRecord {
  question_id: string
  step_id?: string | null
  selected_option_id: string
  correct: boolean
  correct_option_id?: string | null
  explanation?: string | null
  answered_at?: string | null
}

// What actually arrives from GET /ranges/{id}/walkthrough: the options carry no
// `correct` flag and there is no `explanation`, because the answer key is not
// sent to the browser. A question the learner has already answered carries the
// one answer they have seen.
type DeliveredQuestion = QuizQuestion & { answered?: QuizAnswerRecord }

interface QuizProps {
  questions: QuizQuestion[]
  onComplete?: () => void
}

function apiErrorDetail(err: unknown, fallback: string): string {
  if (isAxiosError(err)) {
    const detail = err.response?.data?.detail
    if (typeof detail === 'string') return detail
  }
  return fallback
}

function seedResults(questions: QuizQuestion[]): Record<string, QuizAnswerRecord> {
  const seeded: Record<string, QuizAnswerRecord> = {}
  for (const q of questions as DeliveredQuestion[]) {
    if (q.answered) seeded[q.id] = q.answered
  }
  return seeded
}

export function Quiz({ questions, onComplete }: QuizProps) {
  // The range whose walkthrough this quiz belongs to. StudentLab mounts at
  // /lab/:rangeId, which is the only route that renders a walkthrough.
  const { rangeId } = useParams<{ rangeId: string }>()

  // Graded answers by question id. Seeded from the payload so a reload shows
  // the feedback the learner already saw rather than an unanswered quiz.
  const [results, setResults] = useState<Record<string, QuizAnswerRecord>>(() =>
    seedResults(questions)
  )
  const [pending, setPending] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const total = questions.length
  const answeredCount = Object.keys(results).length
  const allAnswered = total > 0 && answeredCount === total
  const score = Object.values(results).filter(r => r.correct).length

  // One component instance serves every step's quiz, so state has to follow
  // the step. Without this, walking to the next knowledge check would show the
  // previous one's answers already filled in.
  useEffect(() => {
    setResults(seedResults(questions))
    setError(null)
  }, [questions])

  // Mark the step complete once every question has been answered.
  useEffect(() => {
    if (allAnswered) onComplete?.()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allAnswered, questions])

  // The server grades. It holds the answer key, it writes the attempt to the
  // learner record, and only then does it say whether the pick was right --
  // a score this component worked out for itself would be evidence of nothing.
  const handleSelect = async (qid: string, oid: string) => {
    if (results[qid] || pending) return
    if (!rangeId) {
      setError('This quiz is not attached to a lab, so answers cannot be recorded.')
      return
    }

    setPending(qid)
    setError(null)
    try {
      const res = await api.post<QuizAnswerRecord>(`/ranges/${rangeId}/walkthrough/quiz`, {
        question_id: qid,
        option_id: oid,
      })
      setResults(prev => ({ ...prev, [qid]: res.data }))
    } catch (err: unknown) {
      setError(apiErrorDetail(err, 'Could not record that answer. It has not been saved.'))
    } finally {
      setPending(null)
    }
  }

  return (
    <div className="mt-6 border-t border-gray-700 pt-4 space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-white font-semibold">
          <HelpCircle className="w-5 h-5 text-blue-400" />
          Knowledge Check
        </div>
        <div
          className={`text-sm px-2 py-1 rounded ${
            allAnswered ? 'bg-green-900/40 text-green-300' : 'text-gray-400'
          }`}
        >
          Score: {score}/{total}
        </div>
      </div>

      {error && (
        <div className="flex items-start gap-2 rounded bg-red-900/30 border border-red-500/40 px-3 py-2 text-sm text-red-200">
          <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {questions.map((q, qi) => {
        const result = results[q.id]
        const answered = !!result
        return (
          <div key={q.id} className="space-y-2">
            <p className="text-gray-100 font-medium">
              {qi + 1}. {q.prompt}
            </p>
            <div className="space-y-2">
              {q.options.map(o => {
                const isSelected = result?.selected_option_id === o.id
                const isCorrectOption = result?.correct_option_id === o.id
                const isPending = pending === q.id
                let cls = 'border-gray-600 bg-gray-800 text-gray-200 hover:bg-gray-700'
                let icon: React.ReactNode = null
                if (answered) {
                  if (isCorrectOption) {
                    cls = 'border-green-500 bg-green-900/30 text-green-200'
                    icon = <Check className="w-4 h-4 text-green-400 shrink-0" />
                  } else if (isSelected) {
                    cls = 'border-red-500 bg-red-900/30 text-red-200'
                    icon = <X className="w-4 h-4 text-red-400 shrink-0" />
                  } else {
                    cls = 'border-gray-700 bg-gray-800/50 text-gray-500'
                  }
                } else if (isPending) {
                  cls = 'border-gray-600 bg-gray-800 text-gray-400'
                }
                return (
                  <button
                    key={o.id}
                    type="button"
                    disabled={answered || pending !== null}
                    onClick={() => handleSelect(q.id, o.id)}
                    className={`w-full flex items-center justify-between gap-2 text-left px-3 py-2 rounded border text-sm transition-colors ${cls} ${
                      answered ? 'cursor-default' : 'cursor-pointer'
                    }`}
                  >
                    <span>{o.text}</span>
                    {isPending && !answered ? (
                      <Loader2 className="w-4 h-4 animate-spin shrink-0" />
                    ) : (
                      icon
                    )}
                  </button>
                )
              })}
            </div>
            {result && (
              <div
                className={`text-sm rounded px-3 py-2 ${
                  result.correct
                    ? 'bg-green-900/20 text-green-300'
                    : 'bg-red-900/20 text-red-200'
                }`}
              >
                <strong>{result.correct ? 'Correct. ' : 'Not quite. '}</strong>
                {result.explanation || ''}
              </div>
            )}
          </div>
        )
      })}

      {allAnswered && (
        <div className="rounded-lg bg-blue-900/20 border border-blue-500/40 px-4 py-3 text-blue-100 text-sm">
          You scored <strong>{score}/{total}</strong>.{' '}
          {score === total
            ? 'Perfect — every answer correct.'
            : 'Review the explanations above, then continue.'}
        </div>
      )}
    </div>
  )
}
