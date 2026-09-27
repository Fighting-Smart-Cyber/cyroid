// frontend/src/components/scenarios/ScenarioPickerModal.tsx
/**
 * Pick the scenario to apply to a range.
 *
 * Two things this list used to hide. A failed request was caught, logged to the console and drawn
 * as "No scenarios found", so an authorisation failure or a server error looked like an empty
 * library. And a scenario file declaring a category the product does not know -- "purple-team",
 * which the app's own file template once suggested -- indexed a lookup table to undefined and
 * took the modal down with it when its icon was read.
 */
import { useEffect, useState } from 'react'
import { isAxiosError } from 'axios'
import { scenariosApi } from '../../services/api'
import type { Scenario, ScenariosListResponse } from '../../types'
import { Loader2, Target, Shield, UserX, Clock, Zap, Search, AlertTriangle, FileWarning } from 'lucide-react'
import clsx from 'clsx'
import { Modal, ModalBody } from '../common/Modal'

interface ScenarioPickerModalProps {
  onSelect: (scenario: Scenario) => void
  onClose: () => void
}

/** A file in the scenarios directory the API could not read as a scenario. */
interface ScenarioProblem {
  file: string
  error: string
}

// The API reports unreadable files alongside the readable ones so an author is not left
// wondering which of their files is missing from this list.
type ScenariosListPayload = ScenariosListResponse & { problems?: ScenarioProblem[] }

const categoryConfig = {
  'red-team': {
    label: 'Red Team',
    icon: Target,
    color: 'text-red-600',
    bgColor: 'bg-red-100',
    borderColor: 'border-red-200',
  },
  'blue-team': {
    label: 'Blue Team',
    icon: Shield,
    color: 'text-blue-600',
    bgColor: 'bg-blue-100',
    borderColor: 'border-blue-200',
  },
  'insider-threat': {
    label: 'Insider Threat',
    icon: UserX,
    color: 'text-yellow-600',
    bgColor: 'bg-yellow-100',
    borderColor: 'border-yellow-200',
  },
}

const unknownCategory = {
  label: 'Uncategorised',
  icon: Target,
  color: 'text-gray-600',
  bgColor: 'bg-gray-100',
  borderColor: 'border-gray-200',
}

const difficultyConfig = {
  beginner: { label: 'Beginner', color: 'bg-green-100 text-green-800' },
  intermediate: { label: 'Intermediate', color: 'bg-yellow-100 text-yellow-800' },
  advanced: { label: 'Advanced', color: 'bg-red-100 text-red-800' },
}

const unknownDifficulty = { label: 'Unrated', color: 'bg-gray-100 text-gray-800' }

/** Whatever the server said, in the words it said it. */
function loadFailureText(err: unknown): string {
  if (isAxiosError(err)) {
    const detail = err.response?.data?.detail
    if (typeof detail === 'string' && detail) return detail
    if (Array.isArray(detail)) {
      const messages = detail
        .map((d) => (d && typeof d === 'object' && typeof d.msg === 'string' ? d.msg : null))
        .filter((m): m is string => m !== null)
      if (messages.length) return messages.join('; ')
    }
    if (!err.response) return 'No answer from the server.'
  }
  if (err instanceof Error && err.message) return err.message
  return 'Could not load the scenario library.'
}

export default function ScenarioPickerModal({ onSelect, onClose }: ScenarioPickerModalProps) {
  const [scenarios, setScenarios] = useState<Scenario[]>([])
  const [problems, setProblems] = useState<ScenarioProblem[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [searchQuery, setSearchQuery] = useState('')
  const [categoryFilter, setCategoryFilter] = useState<string>('')

  useEffect(() => {
    const fetchScenarios = async () => {
      setLoading(true)
      try {
        const response = await scenariosApi.list(categoryFilter || undefined)
        const payload = response.data as ScenariosListPayload
        setScenarios(payload.scenarios)
        setProblems(payload.problems ?? [])
        setLoadError(null)
      } catch (err: unknown) {
        setScenarios([])
        setProblems([])
        setLoadError(loadFailureText(err))
      } finally {
        setLoading(false)
      }
    }
    fetchScenarios()
  }, [categoryFilter])

  const filteredScenarios = scenarios.filter((s) =>
    s.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
    s.description.toLowerCase().includes(searchQuery.toLowerCase())
  )

  return (
    <Modal
      isOpen={true}
      onClose={onClose}
      title="Add Training Scenario"
      size="full"
      className="max-w-3xl max-h-[85vh] overflow-hidden"
    >
      {/* Filter Section */}
      <div className="p-4 border-b bg-gray-50">
        <div className="flex gap-3">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-gray-400" />
            <input
              type="text"
              placeholder="Search scenarios..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-9 pr-3 py-2 rounded-md border-gray-300 shadow-sm focus:border-primary-500 focus:ring-primary-500 sm:text-sm"
            />
          </div>
          <select
            value={categoryFilter}
            onChange={(e) => setCategoryFilter(e.target.value)}
            className="rounded-md border-gray-300 shadow-sm focus:border-primary-500 focus:ring-primary-500 sm:text-sm"
          >
            <option value="">All Categories</option>
            <option value="red-team">Red Team</option>
            <option value="blue-team">Blue Team</option>
            <option value="insider-threat">Insider Threat</option>
          </select>
        </div>
      </div>

      {/* Scenario List */}
      <ModalBody className="overflow-y-auto max-h-[calc(85vh-140px)]">
        {problems.length > 0 && (
          <div className="mb-4 p-3 bg-amber-50 border border-amber-200 rounded-md text-sm text-amber-900">
            <p className="flex items-center font-medium">
              <FileWarning className="h-4 w-4 mr-1.5" />
              {problems.length} file{problems.length !== 1 ? 's' : ''} in the scenario library
              could not be read
            </p>
            <ul className="mt-1 ml-5 list-disc space-y-0.5 text-amber-800">
              {problems.map((p) => (
                <li key={p.file}>
                  <span className="font-mono">{p.file}</span> — {p.error}
                </li>
              ))}
            </ul>
          </div>
        )}

        {loading ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="h-8 w-8 animate-spin text-primary-600" />
          </div>
        ) : loadError ? (
          <div className="text-center py-12">
            <AlertTriangle className="mx-auto h-10 w-10 text-red-400" />
            <h4 className="mt-2 text-sm font-medium text-gray-900">
              The scenario library could not be loaded
            </h4>
            <p className="mt-1 text-sm text-gray-500">{loadError}</p>
          </div>
        ) : filteredScenarios.length === 0 ? (
          <div className="text-center py-12">
            <p className="text-sm text-gray-500">
              {searchQuery || categoryFilter
                ? 'No scenario matches these filters.'
                : 'No scenarios yet. Add one from the Training Scenarios page.'}
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {filteredScenarios.map((scenario) => {
              // A scenario file names its own category and difficulty, and nothing constrains
              // either to the ones drawn here. An unknown value used to index these tables to
              // undefined and take the modal down when its icon was read.
              const catConfig = categoryConfig[scenario.category] || unknownCategory
              const diffConfig = difficultyConfig[scenario.difficulty] || unknownDifficulty
              const CategoryIcon = catConfig.icon

              return (
                <button
                  key={scenario.id}
                  onClick={() => onSelect(scenario)}
                  className={clsx(
                    "text-left p-4 rounded-lg border-2 hover:border-primary-500 transition-colors",
                    catConfig.borderColor
                  )}
                >
                  <div className="flex items-start">
                    <div className={clsx("flex-shrink-0 rounded-md p-2", catConfig.bgColor)}>
                      <CategoryIcon className={clsx("h-5 w-5", catConfig.color)} />
                    </div>
                    <div className="ml-3 flex-1">
                      <div className="flex items-center justify-between">
                        <h4 className="text-sm font-medium text-gray-900">{scenario.name}</h4>
                        <span className={clsx("text-xs px-2 py-0.5 rounded", diffConfig.color)}>
                          {diffConfig.label}
                        </span>
                      </div>
                      <p className="mt-1 text-xs text-gray-500 line-clamp-2">
                        {scenario.description}
                      </p>
                      <div className="mt-2 flex items-center text-xs text-gray-400 space-x-3">
                        <span className="flex items-center">
                          <Clock className="h-3 w-3 mr-1" />
                          {scenario.duration_minutes} min
                        </span>
                        <span className="flex items-center">
                          <Zap className="h-3 w-3 mr-1" />
                          {scenario.event_count} events
                        </span>
                      </div>
                    </div>
                  </div>
                </button>
              )
            })}
          </div>
        )}
      </ModalBody>
    </Modal>
  )
}
