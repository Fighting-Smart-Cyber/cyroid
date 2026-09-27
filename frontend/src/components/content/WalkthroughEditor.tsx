// frontend/src/components/content/WalkthroughEditor.tsx
import { useMemo, useState } from 'react'
import {
  Plus,
  Trash2,
  ChevronDown,
  ChevronRight,
  GripVertical,
  Code,
  Eye,
  AlertCircle,
} from 'lucide-react'
import clsx from 'clsx'
import type { Walkthrough, WalkthroughPhase, WalkthroughStep } from '../../types'
import yaml from 'js-yaml'

interface WalkthroughEditorProps {
  value: Walkthrough | null
  onChange: (data: Walkthrough) => void
}

const DEFAULT_WALKTHROUGH: Walkthrough = {
  title: '',
  phases: [],
}

const MACHINE_LIST_ID = 'walkthrough-step-machines'

export function countLabel(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? '' : 's'}`
}

/**
 * A guide with the shape the editor reads, whatever it was handed.
 *
 * The YAML tab applies any parse that is an object, and `title: x` on its own is one -- so an
 * author halfway through retyping the document handed this component a guide with no `phases`,
 * every read of `data.phases` threw, and the editor went white and took the unsaved work with it.
 */
export function editableGuide(value: Walkthrough | null): Walkthrough {
  return {
    ...DEFAULT_WALKTHROUGH,
    ...(value ?? {}),
    title: typeof value?.title === 'string' ? value.title : '',
    phases: Array.isArray(value?.phases) ? value.phases : [],
  }
}

/** Every machine already named by a step, in the order the author first used it. */
export function machinesUsedIn(data: Walkthrough): string[] {
  const seen: string[] = []
  for (const phase of data.phases) {
    for (const step of phase?.steps ?? []) {
      const name = step.vm?.trim()
      if (name && !seen.includes(name)) seen.push(name)
    }
  }
  return seen
}

/**
 * Give `candidate` a suffix until nothing in `taken` claims it.
 *
 * Ids are generated from a count, so deleting the first of two phases and adding another
 * reproduced the surviving one's id. Duplicate ids collide in the learner's progress record --
 * completing one step marks the other done -- and in React's keys, so the editor renders the
 * wrong step's content.
 */
export function uniqueId(candidate: string, taken: Set<string>): string {
  if (!taken.has(candidate)) return candidate
  let n = 2
  while (taken.has(`${candidate}_${n}`)) n += 1
  return `${candidate}_${n}`
}

export function WalkthroughEditor({ value, onChange }: WalkthroughEditorProps) {
  const [mode, setMode] = useState<'visual' | 'yaml'>('visual')
  const [yamlText, setYamlText] = useState('')
  const [yamlError, setYamlError] = useState<string | null>(null)
  const [expandedPhases, setExpandedPhases] = useState<Set<string>>(new Set())

  const data = useMemo(() => editableGuide(value), [value])

  // The target used to be a closed list of three fixed names from a Docker-era demo range, so on
  // every real range each step pointed at a machine that does not exist. A guide is authored in
  // the Content Library and linked to a range afterwards, so this editor has no range to read
  // the real names from: the field is free text, and the only honest suggestions are the
  // machines the author has already named in this guide.
  const machineSuggestions = useMemo(() => machinesUsedIn(data), [data])

  // Sync YAML when switching to YAML mode
  const handleModeChange = (newMode: 'visual' | 'yaml') => {
    if (newMode === 'yaml') {
      setYamlText(yaml.dump(data, { lineWidth: -1, quotingType: '"' }))
      setYamlError(null)
    }
    setMode(newMode)
  }

  // Parse YAML and update
  const handleYamlChange = (text: string) => {
    setYamlText(text)
    try {
      const parsed = yaml.load(text) as Walkthrough
      if (parsed && typeof parsed === 'object') {
        setYamlError(null)
        onChange(parsed)
      }
    } catch (err) {
      setYamlError(err instanceof Error ? err.message : 'Invalid YAML')
    }
  }

  // Visual mode handlers
  const updateTitle = (title: string) => {
    onChange({ ...data, title })
  }

  const addPhase = () => {
    const taken = new Set(data.phases.map(p => p.id))
    const newPhase: WalkthroughPhase = {
      id: uniqueId(`phase${data.phases.length + 1}`, taken),
      name: 'New Phase',
      steps: [],
    }
    onChange({ ...data, phases: [...data.phases, newPhase] })
    setExpandedPhases(prev => new Set([...prev, newPhase.id]))
  }

  const updatePhase = (index: number, updates: Partial<WalkthroughPhase>) => {
    const phases = [...data.phases]
    phases[index] = { ...phases[index], ...updates }
    onChange({ ...data, phases })
  }

  const deletePhase = (index: number) => {
    const phases = data.phases.filter((_, i) => i !== index)
    onChange({ ...data, phases })
  }

  const addStep = (phaseIndex: number) => {
    const phases = [...data.phases]
    const phase = phases[phaseIndex]
    // A step id has to be unique across the whole walkthrough, not just its phase: progress is
    // recorded as a flat list of completed step ids.
    const taken = new Set(data.phases.flatMap(p => p.steps.map(s => s.id)))
    const newStep: WalkthroughStep = {
      id: uniqueId(`step${phaseIndex + 1}_${phase.steps.length + 1}`, taken),
      title: 'New Step',
      content: '',
      vm: '',
    }
    phases[phaseIndex] = { ...phase, steps: [...phase.steps, newStep] }
    onChange({ ...data, phases })
  }

  const updateStep = (phaseIndex: number, stepIndex: number, updates: Partial<WalkthroughStep>) => {
    const phases = [...data.phases]
    const steps = [...phases[phaseIndex].steps]
    steps[stepIndex] = { ...steps[stepIndex], ...updates }
    phases[phaseIndex] = { ...phases[phaseIndex], steps }
    onChange({ ...data, phases })
  }

  const deleteStep = (phaseIndex: number, stepIndex: number) => {
    const phases = [...data.phases]
    phases[phaseIndex] = {
      ...phases[phaseIndex],
      steps: phases[phaseIndex].steps.filter((_, i) => i !== stepIndex),
    }
    onChange({ ...data, phases })
  }

  const togglePhase = (phaseId: string) => {
    setExpandedPhases(prev => {
      const next = new Set(prev)
      if (next.has(phaseId)) {
        next.delete(phaseId)
      } else {
        next.add(phaseId)
      }
      return next
    })
  }

  return (
    <div className="border rounded-lg overflow-hidden">
      {/* Mode Toggle */}
      <div className="flex items-center justify-between bg-gray-50 px-4 py-2 border-b">
        <div className="flex gap-2">
          <button
            onClick={() => handleModeChange('visual')}
            className={clsx(
              'px-3 py-1.5 text-sm font-medium rounded flex items-center gap-2',
              mode === 'visual'
                ? 'bg-white shadow text-gray-900'
                : 'text-gray-600 hover:text-gray-900'
            )}
          >
            <Eye className="w-4 h-4" />
            Visual
          </button>
          <button
            onClick={() => handleModeChange('yaml')}
            className={clsx(
              'px-3 py-1.5 text-sm font-medium rounded flex items-center gap-2',
              mode === 'yaml'
                ? 'bg-white shadow text-gray-900'
                : 'text-gray-600 hover:text-gray-900'
            )}
          >
            <Code className="w-4 h-4" />
            YAML
          </button>
        </div>
        <span className="text-xs text-gray-500">
          {countLabel(data.phases.length, 'phase')},{' '}
          {countLabel(data.phases.reduce((sum, p) => sum + p.steps.length, 0), 'step')}
        </span>
      </div>

      {/* Shared by every step's target field, so a machine typed once is offered everywhere. */}
      <datalist id={MACHINE_LIST_ID}>
        {machineSuggestions.map(name => (
          <option key={name} value={name} />
        ))}
      </datalist>

      {mode === 'visual' ? (
        <div className="p-4 space-y-4">
          {/* Title */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Guide Title
            </label>
            <input
              type="text"
              value={data.title}
              onChange={(e) => updateTitle(e.target.value)}
              placeholder="e.g., Day 1: First Login and Orientation"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 focus:ring-2 focus:ring-primary-500 focus:border-primary-500"
            />
            <p className="mt-1 text-xs text-gray-500">
              This is the guide a learner sees beside their machines when they open the lab on any
              range this content is linked to.
            </p>
          </div>

          {/* Phases */}
          <div className="space-y-3">
            {data.phases.map((phase, phaseIndex) => (
              <PhaseEditor
                key={phase.id}
                phase={phase}
                expanded={expandedPhases.has(phase.id)}
                onToggle={() => togglePhase(phase.id)}
                onUpdate={(updates) => updatePhase(phaseIndex, updates)}
                onDelete={() => deletePhase(phaseIndex)}
                onAddStep={() => addStep(phaseIndex)}
                onUpdateStep={(stepIndex, updates) => updateStep(phaseIndex, stepIndex, updates)}
                onDeleteStep={(stepIndex) => deleteStep(phaseIndex, stepIndex)}
              />
            ))}
          </div>

          {/* Add Phase Button */}
          <button
            onClick={addPhase}
            className="w-full py-2 border-2 border-dashed border-gray-300 rounded-lg text-gray-500 hover:border-primary-400 hover:text-primary-600 flex items-center justify-center gap-2"
          >
            <Plus className="w-4 h-4" />
            Add Phase
          </button>
        </div>
      ) : (
        <div className="p-4">
          {yamlError && (
            <div className="mb-3 p-3 bg-red-50 border border-red-200 rounded-lg flex items-center gap-2 text-red-700 text-sm">
              <AlertCircle className="w-4 h-4 flex-shrink-0" />
              {yamlError}
            </div>
          )}
          <textarea
            value={yamlText}
            onChange={(e) => handleYamlChange(e.target.value)}
            className="w-full h-96 font-mono text-sm border border-gray-300 rounded-lg p-3 focus:ring-2 focus:ring-primary-500 focus:border-primary-500"
            placeholder="# The guide's phases and steps, in YAML"
          />
          <p className="mt-2 text-xs text-gray-500">
            Edit the guide directly in YAML. Changes are applied automatically when valid. A step's{' '}
            <code className="font-mono">vm:</code> key is the name of the machine that step is
            about, as it appears on the range.
          </p>
        </div>
      )}
    </div>
  )
}

// Phase Editor Component
interface PhaseEditorProps {
  phase: WalkthroughPhase
  expanded: boolean
  onToggle: () => void
  onUpdate: (updates: Partial<WalkthroughPhase>) => void
  onDelete: () => void
  onAddStep: () => void
  onUpdateStep: (stepIndex: number, updates: Partial<WalkthroughStep>) => void
  onDeleteStep: (stepIndex: number) => void
}

function PhaseEditor({
  phase,
  expanded,
  onToggle,
  onUpdate,
  onDelete,
  onAddStep,
  onUpdateStep,
  onDeleteStep,
}: PhaseEditorProps) {
  return (
    <div className="border rounded-lg bg-white">
      {/* Phase Header */}
      <div className="flex items-center gap-2 p-3 bg-gray-50 border-b">
        <GripVertical className="w-4 h-4 text-gray-400 cursor-move" />
        <button onClick={onToggle} className="p-1 hover:bg-gray-200 rounded">
          {expanded ? (
            <ChevronDown className="w-4 h-4 text-gray-500" />
          ) : (
            <ChevronRight className="w-4 h-4 text-gray-500" />
          )}
        </button>
        <input
          value={phase.name}
          onChange={(e) => onUpdate({ name: e.target.value })}
          className="flex-1 bg-transparent font-medium text-gray-900 focus:outline-none focus:ring-2 focus:ring-primary-500 rounded px-2 py-1"
          placeholder="Phase name"
        />
        <span className="text-xs text-gray-500">{phase.steps.length} steps</span>
        <button
          onClick={onDelete}
          className="p-1.5 text-red-500 hover:bg-red-50 rounded"
          title="Delete phase"
        >
          <Trash2 className="w-4 h-4" />
        </button>
      </div>

      {/* Phase Content */}
      {expanded && (
        <div className="p-3 space-y-3">
          {phase.steps.map((step, stepIndex) => (
            <StepEditor
              key={step.id}
              step={step}
              onUpdate={(updates) => onUpdateStep(stepIndex, updates)}
              onDelete={() => onDeleteStep(stepIndex)}
            />
          ))}

          <button
            onClick={onAddStep}
            className="w-full py-2 text-sm text-primary-600 hover:bg-primary-50 rounded-lg flex items-center justify-center gap-1"
          >
            <Plus className="w-4 h-4" />
            Add Step
          </button>
        </div>
      )}
    </div>
  )
}

// Step Editor Component
interface StepEditorProps {
  step: WalkthroughStep
  onUpdate: (updates: Partial<WalkthroughStep>) => void
  onDelete: () => void
}

function StepEditor({ step, onUpdate, onDelete }: StepEditorProps) {
  const [showContent, setShowContent] = useState(false)

  return (
    <div className="border rounded-lg p-3 bg-gray-50 space-y-2">
      {/* Step Header */}
      <div className="flex items-center gap-2">
        <GripVertical className="w-4 h-4 text-gray-400 cursor-move" />
        <input
          value={step.title}
          onChange={(e) => onUpdate({ title: e.target.value })}
          className="flex-1 bg-white border border-gray-300 rounded px-2 py-1 text-sm focus:ring-2 focus:ring-primary-500 focus:border-primary-500"
          placeholder="Step title"
        />
        {/* A stored name with a space around it matches no machine, so it has to be trimmed --
            but trimming on every keystroke swallowed the space as the author typed it, and the
            name they were writing was rewritten under them. Trim once, when they leave the
            field, and only when that actually changes something, so merely tabbing through a
            step does not mark the content unsaved. */}
        <input
          list={MACHINE_LIST_ID}
          value={step.vm || ''}
          onChange={(e) => onUpdate({ vm: e.target.value || undefined })}
          onBlur={(e) => {
            const trimmed = e.target.value.trim()
            if (trimmed !== e.target.value) onUpdate({ vm: trimmed || undefined })
          }}
          placeholder="Machine (optional)"
          title="The machine this step is about, by the name it has on the range. Leave empty for a step that is not about one machine."
          className="w-40 border border-gray-300 rounded px-2 py-1 text-sm bg-white"
        />
        <button
          onClick={() => setShowContent(!showContent)}
          className="p-1.5 text-gray-500 hover:bg-gray-200 rounded"
          title={showContent ? 'Hide content' : 'Edit content'}
        >
          {showContent ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
        </button>
        <button
          onClick={onDelete}
          className="p-1.5 text-red-500 hover:bg-red-50 rounded"
          title="Delete step"
        >
          <Trash2 className="w-4 h-4" />
        </button>
      </div>

      {/* Step Content */}
      {showContent && (
        <div>
          <textarea
            value={step.content}
            onChange={(e) => onUpdate({ content: e.target.value })}
            className="w-full h-32 text-sm font-mono border border-gray-300 rounded p-2 focus:ring-2 focus:ring-primary-500 focus:border-primary-500"
            placeholder="Step content in Markdown..."
          />
          <p className="text-xs text-gray-500 mt-1">
            Supports Markdown formatting: **bold**, `code`, ```code blocks```
          </p>
        </div>
      )}
    </div>
  )
}

export default WalkthroughEditor
