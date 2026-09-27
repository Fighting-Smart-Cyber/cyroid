// frontend/src/components/scenarios/VMMappingModal.tsx
/**
 * Bind every role a scenario names to a machine in the range, then apply it.
 *
 * A scenario's injects address machines by role and a range holds machines by id, so this modal
 * is the join between the two. On the Kubernetes substrate there is nothing to join: a range's
 * machines come from its blueprint's workloads, and applying a scenario binds roles to the VM
 * rows only the Docker substrate writes. Saying that is the second line of defence behind the
 * gate on the button that opens this -- an empty dropdown reads as "add machines first", which
 * sends the user hunting for a control this substrate does not have.
 */
import { useState } from 'react'
import { isAxiosError } from 'axios'
import type { Scenario, VM } from '../../types'
import { Loader2, Server, ChevronDown, AlertTriangle } from 'lucide-react'
import { Modal, ModalBody, ModalFooter } from '../common/Modal'
import { useSubstrate } from '../../stores/capabilitiesStore'

interface VMMappingModalProps {
  scenario: Scenario
  vms: VM[]
  onApply: (roleMapping: Record<string, string>) => Promise<void>
  onBack: () => void
  onClose: () => void
}

// Convert role slug to display name
function formatRoleName(role: string): string {
  return role
    .split('-')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

/** Whatever the server said, in the words it said it. */
function applyFailureText(err: unknown, fallback: string): string {
  if (isAxiosError(err)) {
    const detail = err.response?.data?.detail
    if (typeof detail === 'string' && detail) return detail
    // A rejected request body comes back as a list of field errors. Putting that array into JSX
    // is what "Objects are not valid as a React child" means, and it blanks the whole page
    // instead of showing the user which field the server would not take.
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

export default function VMMappingModal({
  scenario,
  vms,
  onApply,
  onBack,
  onClose,
}: VMMappingModalProps) {
  const { isKubernetes } = useSubstrate()
  const machine = isKubernetes ? 'machine' : 'VM'
  const machines = isKubernetes ? 'machines' : 'VMs'

  const [roleMapping, setRoleMapping] = useState<Record<string, string>>({})
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleVMSelect = (role: string, vmId: string) => {
    setRoleMapping((prev) => ({ ...prev, [role]: vmId }))
  }

  const allRolesMapped = scenario.required_roles.every((role) => roleMapping[role])
  const canApply = !isKubernetes && allRolesMapped

  const handleApply = async () => {
    if (!canApply) return

    setSubmitting(true)
    setError(null)
    try {
      await onApply(roleMapping)
    } catch (err: unknown) {
      setError(applyFailureText(err, 'Failed to apply scenario'))
      setSubmitting(false)
    }
  }

  // Check if a machine is already assigned to another role
  const getVMAssignment = (vmId: string): string | null => {
    for (const [role, id] of Object.entries(roleMapping)) {
      if (id === vmId) return role
    }
    return null
  }

  return (
    <Modal
      isOpen={true}
      onClose={onClose}
      title={`Configure: ${scenario.name}`}
      size="lg"
    >
      <ModalBody>
        {isKubernetes ? (
          <div className="p-4 bg-amber-50 border border-amber-200 rounded-md">
            <div className="flex">
              <AlertTriangle className="h-5 w-5 text-amber-600 flex-shrink-0" />
              <div className="ml-3">
                <p className="text-sm font-medium text-amber-900">
                  Scenarios cannot be applied on this substrate yet
                </p>
                <p className="mt-1 text-sm text-amber-800">
                  A scenario binds each of its roles to a machine the range builder recorded. A
                  Kubernetes range keeps no such list — its machines are the workloads its
                  blueprint declares — and nothing maps a scenario role onto one of those yet.
                </p>
                <p className="mt-2 text-sm text-amber-800">
                  The scenario itself is unaffected — its timeline is still readable on the
                  Training Scenarios page.
                </p>
              </div>
            </div>
          </div>
        ) : (
          <>
            {scenario.required_roles.length === 0 ? (
              <p className="text-sm text-gray-600 mb-4">
                This scenario names no roles, so there is nothing to map. Applying it builds its
                event timeline against this range with no {machine} targeted.
              </p>
            ) : (
              <p className="text-sm text-gray-600 mb-4">
                This scenario requires {scenario.required_roles.length} target system
                {scenario.required_roles.length !== 1 ? 's' : ''}.
                Map each role to a {machine} in your range:
              </p>
            )}

            {error && (
              <div className="mb-4 p-3 bg-red-50 text-red-700 rounded-md text-sm">
                {error}
              </div>
            )}

            <div className="space-y-3">
              {scenario.required_roles.map((role) => (
                <div key={role} className="flex items-center justify-between">
                  <label className="text-sm font-medium text-gray-700 w-1/3">
                    {formatRoleName(role)}
                  </label>
                  <div className="relative w-2/3">
                    <select
                      value={roleMapping[role] || ''}
                      onChange={(e) => handleVMSelect(role, e.target.value)}
                      disabled={vms.length === 0}
                      className="block w-full rounded-md border-gray-300 shadow-sm focus:border-primary-500 focus:ring-primary-500 sm:text-sm appearance-none pr-8 disabled:bg-gray-50 disabled:text-gray-400"
                    >
                      <option value="">Select a {machine}...</option>
                      {vms.map((vm) => {
                        const assignedTo = getVMAssignment(vm.id)
                        const isAssignedElsewhere = Boolean(assignedTo && assignedTo !== role)
                        return (
                          <option
                            key={vm.id}
                            value={vm.id}
                            disabled={isAssignedElsewhere}
                          >
                            {vm.hostname}
                            {vm.ip_address ? ` (${vm.ip_address})` : ''}
                            {isAssignedElsewhere && ` → ${formatRoleName(assignedTo!)}`}
                          </option>
                        )
                      })}
                    </select>
                    <ChevronDown className="absolute right-2 top-1/2 transform -translate-y-1/2 h-4 w-4 text-gray-400 pointer-events-none" />
                  </div>
                </div>
              ))}
            </div>

            {vms.length === 0 && scenario.required_roles.length > 0 && (
              <div className="mt-4 p-3 bg-yellow-50 text-yellow-700 rounded-md text-sm">
                <Server className="inline h-4 w-4 mr-1" />
                This range has no {machines} to map these roles to. Add {machines} to the range
                first, then apply the scenario.
              </div>
            )}
          </>
        )}
      </ModalBody>

      <ModalFooter className="justify-between">
        <button
          type="button"
          onClick={onBack}
          className="px-4 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50"
        >
          Back
        </button>
        <div className="flex space-x-3">
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50"
          >
            {isKubernetes ? 'Close' : 'Cancel'}
          </button>
          {/* No Apply button on Kubernetes: the request it would send cannot be satisfied there,
              and a disabled button invites the user to look for what would enable it. */}
          {!isKubernetes && (
            <button
              type="button"
              onClick={handleApply}
              disabled={!canApply || submitting}
              className="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-primary-600 hover:bg-primary-700 disabled:opacity-50"
            >
              {submitting && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              Apply Scenario
            </button>
          )}
        </div>
      </ModalFooter>
    </Modal>
  )
}
