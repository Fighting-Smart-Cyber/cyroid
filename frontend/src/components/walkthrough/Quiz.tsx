// frontend/src/components/walkthrough/Quiz.tsx
import { useEffect, useState } from 'react'
import { Check, X, HelpCircle } from 'lucide-react'
import { QuizQuestion } from '../../types'

interface QuizProps {
  questions: QuizQuestion[]
  onComplete?: () => void
}

export function Quiz({ questions, onComplete }: QuizProps) {
  // Selected option id per question id. Locked once chosen.
  const [selected, setSelected] = useState<Record<string, string>>({})

  const total = questions.length
  const answeredCount = Object.keys(selected).length
  const allAnswered = total > 0 && answeredCount === total
  const score = questions.reduce((acc, q) => {
    const opt = q.options.find(o => o.id === selected[q.id])
    return acc + (opt?.correct ? 1 : 0)
  }, 0)

  // Mark the step complete once every question has been answered.
  useEffect(() => {
    if (allAnswered) onComplete?.()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allAnswered])

  const handleSelect = (qid: string, oid: string) => {
    setSelected(prev => (prev[qid] ? prev : { ...prev, [qid]: oid }))
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

      {questions.map((q, qi) => {
        const sel = selected[q.id]
        const answered = !!sel
        const selectedCorrect = q.options.find(o => o.id === sel)?.correct
        return (
          <div key={q.id} className="space-y-2">
            <p className="text-gray-100 font-medium">
              {qi + 1}. {q.prompt}
            </p>
            <div className="space-y-2">
              {q.options.map(o => {
                const isSelected = sel === o.id
                let cls = 'border-gray-600 bg-gray-800 text-gray-200 hover:bg-gray-700'
                let icon: React.ReactNode = null
                if (answered) {
                  if (o.correct) {
                    cls = 'border-green-500 bg-green-900/30 text-green-200'
                    icon = <Check className="w-4 h-4 text-green-400 shrink-0" />
                  } else if (isSelected) {
                    cls = 'border-red-500 bg-red-900/30 text-red-200'
                    icon = <X className="w-4 h-4 text-red-400 shrink-0" />
                  } else {
                    cls = 'border-gray-700 bg-gray-800/50 text-gray-500'
                  }
                }
                return (
                  <button
                    key={o.id}
                    type="button"
                    disabled={answered}
                    onClick={() => handleSelect(q.id, o.id)}
                    className={`w-full flex items-center justify-between gap-2 text-left px-3 py-2 rounded border text-sm transition-colors ${cls} ${
                      answered ? 'cursor-default' : 'cursor-pointer'
                    }`}
                  >
                    <span>{o.text}</span>
                    {icon}
                  </button>
                )
              })}
            </div>
            {answered && (
              <div
                className={`text-sm rounded px-3 py-2 ${
                  selectedCorrect
                    ? 'bg-green-900/20 text-green-300'
                    : 'bg-red-900/20 text-red-200'
                }`}
              >
                <strong>{selectedCorrect ? 'Correct. ' : 'Not quite. '}</strong>
                {q.explanation || ''}
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
