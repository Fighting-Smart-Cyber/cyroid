// frontend/src/pages/TrainingScenarios.tsx
/**
 * The scenario library: every scenario file the platform can read, and every one it cannot.
 *
 * This page used to print the server's own storage path under the heading -- a directory inside
 * a container that no user can open and that tells a reader where the platform keeps its files.
 * What an author actually needs is the count, which files are unreadable and why, and whether a
 * scenario can be applied on this substrate at all.
 */
import { useEffect, useState, useRef } from 'react'
import { isAxiosError } from 'axios'
import { scenariosApi } from '../services/api'
import type { Scenario, ScenariosListResponse } from '../types'
import { Loader2, Target, Shield, UserX, Clock, Zap, AlertTriangle, Upload, RefreshCw, Trash2, FileWarning, FolderEdit, List } from 'lucide-react'
import clsx from 'clsx'
import { toast } from '../stores/toastStore'
import { ConfirmDialog } from '../components/common/ConfirmDialog'
import { FileBrowser } from '../components/files/FileBrowser'
import { useSubstrate } from '../stores/capabilitiesStore'

type TabType = 'scenarios' | 'files'

/** A file in the scenario library the API could not read as a scenario. */
interface ScenarioProblem {
  file: string
  error: string
}

// The list endpoint names the files it could not read alongside the ones it could, so a broken
// file is one named entry here rather than a scenario that silently never appears.
type ScenariosListPayload = ScenariosListResponse & { problems?: ScenarioProblem[] }

const categoryConfig = {
  'red-team': {
    label: 'Red Team',
    icon: Target,
    color: 'text-red-600',
    bgColor: 'bg-red-100',
  },
  'blue-team': {
    label: 'Blue Team',
    icon: Shield,
    color: 'text-blue-600',
    bgColor: 'bg-blue-100',
  },
  'insider-threat': {
    label: 'Insider Threat',
    icon: UserX,
    color: 'text-yellow-600',
    bgColor: 'bg-yellow-100',
  },
}

const difficultyConfig = {
  beginner: { label: 'Beginner', color: 'bg-green-100 text-green-800' },
  intermediate: { label: 'Intermediate', color: 'bg-yellow-100 text-yellow-800' },
  advanced: { label: 'Advanced', color: 'bg-red-100 text-red-800' },
}

// A scenario file names its own category and difficulty, and nothing constrains either to the
// values drawn here -- the product's own file template used to suggest "purple-team" and
// "expert". This page fell back to red-team and intermediate, so a file saying "expert" was
// labelled "Intermediate": an author has no reason to suspect the field was ignored when the
// page confidently prints a value the file never declared.
const unknownCategory = {
  label: 'Uncategorised',
  icon: Target,
  color: 'text-gray-600',
  bgColor: 'bg-gray-100',
}

const unknownDifficulty = { label: 'Unrated', color: 'bg-gray-100 text-gray-800' }

/** Whatever the server said went wrong, or a fallback that at least names the operation. */
function reason(err: unknown, fallback: string): string {
  if (isAxiosError(err)) {
    const detail = err.response?.data?.detail
    if (typeof detail === 'string' && detail) return detail
    // A rejected body comes back as a list of field errors; rendering that array into JSX is
    // what blanks a page with "Objects are not valid as a React child".
    if (Array.isArray(detail)) {
      const messages = detail
        .map((d) => (d && typeof d === 'object' && typeof d.msg === 'string' ? d.msg : null))
        .filter((m): m is string => m !== null)
      if (messages.length) return messages.join('; ')
    }
    if (!err.response) return `${fallback} — no answer from the server`
  }
  if (err instanceof Error && err.message) return err.message
  return fallback
}

export default function TrainingScenarios() {
  const { isKubernetes } = useSubstrate()
  const [activeTab, setActiveTab] = useState<TabType>('scenarios')
  const [scenarios, setScenarios] = useState<Scenario[]>([])
  const [problems, setProblems] = useState<ScenarioProblem[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [categoryFilter, setCategoryFilter] = useState<string>('')
  const [searchQuery, setSearchQuery] = useState('')
  const [deleteConfirm, setDeleteConfirm] = useState<{ open: boolean; id: string; name: string }>({
    open: false,
    id: '',
    name: '',
  })
  const fileInputRef = useRef<HTMLInputElement>(null)

  const tabs = [
    { id: 'scenarios' as const, name: 'Scenarios', icon: List },
    { id: 'files' as const, name: 'Scenario Files', icon: FolderEdit },
  ]

  const fetchScenarios = async () => {
    try {
      const response = await scenariosApi.list(categoryFilter || undefined)
      const payload = response.data as ScenariosListPayload
      setScenarios(payload.scenarios)
      setProblems(payload.problems ?? [])
      setLoadError(null)
    } catch (err: unknown) {
      const detail = reason(err, 'Failed to load scenarios')
      setScenarios([])
      setProblems([])
      setLoadError(detail)
      toast.error(detail)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchScenarios()
  }, [categoryFilter])

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return

    setUploading(true)
    try {
      const response = await scenariosApi.upload(file, false)
      toast.success(`Uploaded scenario: ${response.data.name}`)
      fetchScenarios()
    } catch (err: unknown) {
      toast.error(reason(err, 'Failed to upload scenario'))
    } finally {
      setUploading(false)
      if (fileInputRef.current) {
        fileInputRef.current.value = ''
      }
    }
  }

  const handleRefresh = async () => {
    setRefreshing(true)
    try {
      const response = await scenariosApi.refresh()
      toast.success(`Refreshed: ${response.data.total} scenarios found`)
      fetchScenarios()
    } catch (err: unknown) {
      toast.error(reason(err, 'Failed to refresh scenarios'))
    } finally {
      setRefreshing(false)
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await scenariosApi.delete(id)
      toast.success('Scenario deleted')
      fetchScenarios()
    } catch (err: unknown) {
      toast.error(reason(err, 'Failed to delete scenario'))
    }
    setDeleteConfirm({ open: false, id: '', name: '' })
  }

  const filteredScenarios = scenarios.filter((s) =>
    s.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
    s.description.toLowerCase().includes(searchQuery.toLowerCase())
  )

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-8 w-8 animate-spin text-primary-600" />
      </div>
    )
  }

  return (
    <div>
      <div className="sm:flex sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Training Scenarios</h1>
          <p className="mt-2 text-sm text-gray-700">
            Each scenario is a timeline of injects addressed to the roles it names.
          </p>
          {/* The category filter is applied by the server, so `scenarios` holds that category
              alone while `problems` covers the whole directory. Counting the two together under
              one word would report a library that is mostly broken whenever a narrow filter is
              set, so the readable count says which set it is counting. */}
          <p className="mt-1 text-xs text-gray-500">
            {scenarios.length} scenario{scenarios.length !== 1 ? 's' : ''}
            {categoryFilter ? ' in this category' : ' readable'}
            {problems.length > 0 &&
              `, ${problems.length} file${problems.length !== 1 ? 's' : ''} in the library unreadable`}
            {' · edit the files on the Scenario Files tab'}
          </p>
        </div>
        {activeTab === 'scenarios' && (
          <div className="mt-4 sm:mt-0 flex gap-2">
            <input
              type="file"
              ref={fileInputRef}
              onChange={handleUpload}
              accept=".yaml,.yml"
              className="hidden"
            />
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
              className="inline-flex items-center px-4 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50"
            >
              {uploading ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : (
                <Upload className="h-4 w-4 mr-2" />
              )}
              Upload YAML
            </button>
            <button
              onClick={handleRefresh}
              disabled={refreshing}
              className="inline-flex items-center px-3 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50"
              title="Re-read the scenario files"
            >
              <RefreshCw className={clsx("h-4 w-4", refreshing && "animate-spin")} />
            </button>
          </div>
        )}
      </div>

      {/* Tabs */}
      <div className="mt-6 border-b border-gray-200">
        <nav className="-mb-px flex space-x-8">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={clsx(
                'flex items-center py-4 px-1 border-b-2 font-medium text-sm',
                activeTab === tab.id
                  ? 'border-primary-500 text-primary-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
              )}
            >
              <tab.icon className="h-5 w-5 mr-2" />
              {tab.name}
            </button>
          ))}
        </nav>
      </div>

      {/* Scenarios Tab */}
      {activeTab === 'scenarios' && (
        <>
          {/* Files that are meant to be scenarios and are not. Named here, with the parser's own
              message, because the alternative is an author staring at a list their file is
              missing from. */}
          {problems.length > 0 && (
            <div className="mt-6 p-4 bg-amber-50 border border-amber-200 rounded-md">
              <p className="flex items-center text-sm font-medium text-amber-900">
                <FileWarning className="h-4 w-4 mr-1.5" />
                {problems.length} file{problems.length !== 1 ? 's' : ''} in the scenario library
                could not be read
              </p>
              <ul className="mt-2 ml-5 list-disc space-y-1 text-sm text-amber-800">
                {problems.map((p) => (
                  <li key={p.file}>
                    <span className="font-mono">{p.file}</span> — {p.error}
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-xs text-amber-700">
                Fix or delete these on the Scenario Files tab. Until then they appear nowhere else
                in the product.
              </p>
            </div>
          )}

          {loadError && (
            <div className="mt-6 p-4 bg-red-50 border border-red-200 rounded-md text-sm text-red-800">
              {loadError}
            </div>
          )}

          {/* Filters */}
      <div className="mt-6 flex flex-col sm:flex-row gap-4">
        <input
          type="text"
          placeholder="Search scenarios..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="flex-1 rounded-md border-gray-300 shadow-sm focus:border-primary-500 focus:ring-primary-500 sm:text-sm"
        />
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

      {filteredScenarios.length === 0 ? (
        <div className="mt-8 text-center">
          <AlertTriangle className="mx-auto h-12 w-12 text-gray-400" />
          <h3 className="mt-2 text-sm font-medium text-gray-900">No scenarios found</h3>
          <p className="mt-1 text-sm text-gray-500">
            {searchQuery || categoryFilter
              ? 'Try adjusting your filters.'
              : 'Upload a YAML file, or create one from the Training Scenario template on the Scenario Files tab.'}
          </p>
        </div>
      ) : (
        <div className="mt-8 grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-3">
          {filteredScenarios.map((scenario) => {
            const catConfig = categoryConfig[scenario.category] || unknownCategory
            const diffConfig = difficultyConfig[scenario.difficulty] || unknownDifficulty
            const CategoryIcon = catConfig.icon

            return (
              <div
                key={scenario.id}
                className="bg-white rounded-lg shadow overflow-hidden hover:shadow-md transition-shadow"
              >
                <div className="p-5">
                  <div className="flex items-start justify-between">
                    <div className="flex items-center">
                      <div className={clsx("flex-shrink-0 rounded-md p-2", catConfig.bgColor)}>
                        <CategoryIcon className={clsx("h-6 w-6", catConfig.color)} />
                      </div>
                      <div className="ml-3">
                        <h3 className="text-sm font-medium text-gray-900">{scenario.name}</h3>
                        <span className={clsx("inline-block mt-1 text-xs px-2 py-0.5 rounded", diffConfig.color)}>
                          {diffConfig.label}
                        </span>
                      </div>
                    </div>
                    <button
                      onClick={() => setDeleteConfirm({ open: true, id: scenario.id, name: scenario.name })}
                      className="text-gray-400 hover:text-red-500 p-1"
                      title="Delete scenario"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>

                  <p className="mt-3 text-sm text-gray-500 line-clamp-3">
                    {scenario.description}
                  </p>

                  <div className="mt-4 flex items-center text-xs text-gray-500 space-x-4">
                    <span className="flex items-center">
                      <Clock className="h-3.5 w-3.5 mr-1" />
                      {scenario.duration_minutes} min
                    </span>
                    <span className="flex items-center">
                      <Zap className="h-3.5 w-3.5 mr-1" />
                      {scenario.event_count} events
                    </span>
                  </div>

                  <div className="mt-3">
                    <p className="text-xs text-gray-400 mb-1">Required roles:</p>
                    <div className="flex flex-wrap gap-1">
                      {scenario.required_roles.map((role) => (
                        <span
                          key={role}
                          className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-700"
                        >
                          {role}
                        </span>
                      ))}
                    </div>
                  </div>
                </div>

                <div className="bg-gray-50 px-5 py-3">
                  {/* Applying a scenario binds its roles to the range's VM rows, which only the
                      Docker substrate writes. Telling a Kubernetes user to go and do it on a
                      range's page sends them after a button that install does not show. */}
                  <p className="text-xs text-gray-500">
                    {isKubernetes
                      ? 'Readable here. Applying a scenario to a range is not available on this substrate yet.'
                      : "Apply this scenario from a running range's detail page."}
                  </p>
                </div>
              </div>
            )
          })}
        </div>
      )}
        </>
      )}

      {/* Scenario Files Tab */}
      {activeTab === 'files' && (
        <div className="mt-6">
          <div className="mb-4">
            <p className="text-sm text-gray-500">
              Browse and edit scenario YAML files directly. Changes show up in the list after a
              refresh.
            </p>
          </div>
          <FileBrowser basePath="scenarios" title="" />
        </div>
      )}

      <ConfirmDialog
        isOpen={deleteConfirm.open}
        onCancel={() => setDeleteConfirm({ open: false, id: '', name: '' })}
        onConfirm={() => handleDelete(deleteConfirm.id)}
        title="Delete Scenario"
        message={`Are you sure you want to delete "${deleteConfirm.name}"? This deletes the scenario's YAML file.`}
        confirmLabel="Delete"
        variant="danger"
      />
    </div>
  )
}
