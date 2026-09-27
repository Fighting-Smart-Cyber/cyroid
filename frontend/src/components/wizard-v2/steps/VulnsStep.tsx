// frontend/src/components/wizard-v2/steps/VulnsStep.tsx
import { ShieldAlert, Info } from 'lucide-react';
import { useWizardStore } from '../../../stores/wizardStore';

/**
 * What this step used to be, and why it is a summary now.
 *
 * It offered four vulnerability profiles, a per-machine catalogue of twenty-odd weaknesses to
 * toggle, and a free-text attack narrative. None of it reached the platform: the deploy call
 * creates a range, its networks and its machines, and there is no vulnerability anywhere in the
 * API behind it -- nothing is injected, nothing is weakened, nothing records the narrative. A
 * user who picked "Advanced (20 vulns)" got exactly the same range as one who picked "None", and
 * the Review step then printed the profile back to them as though it had been applied.
 *
 * The controls are gone rather than disabled because a disabled control still describes a feature
 * that exists somewhere, and this one does not. What is left is the truth it was obscuring: a
 * machine's attack surface is whatever its image carries, so the images are what this step shows.
 */
export function VulnsStep() {
  const { networks } = useWizardStore();
  const machines = networks.vms;

  return (
    <div className="max-w-5xl mx-auto">
      <h2 className="text-2xl font-bold text-gray-900 mb-2">Attack Surface</h2>
      <p className="text-gray-600 mb-8">
        Review what this range will expose. Nothing on this step needs your input.
      </p>

      <div className="flex items-start gap-3 p-4 mb-8 rounded-lg border border-blue-200 bg-blue-50">
        <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
        <div className="text-sm text-blue-800 space-y-2">
          <p>
            The platform does not add weaknesses to a range. A machine is exactly the image it was
            built from, so its attack surface is chosen on the Services step, by choosing the
            image &mdash; a deliberately vulnerable image is vulnerable, a hardened one is not.
          </p>
          <p>
            The attack path a learner is meant to follow is written in the Content Library, which
            is the surface that reaches them; it is not a property of the range.
          </p>
        </div>
      </div>

      <h3 className="text-lg font-semibold text-gray-900 mb-4">
        Images in this range ({machines.length})
      </h3>

      {machines.length === 0 ? (
        <div className="text-center py-12 bg-gray-50 rounded-lg border border-dashed border-gray-300">
          <ShieldAlert className="h-12 w-12 text-gray-300 mx-auto mb-3" />
          <p className="text-gray-500">This range has no machines yet.</p>
          <p className="text-xs text-gray-400 mt-1">
            Add them on the Networks step to see what they will expose.
          </p>
        </div>
      ) : (
        <div className="border border-gray-200 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Machine</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Image</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Network</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Reachability</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {machines.map((vm) => {
                const network = networks.segments.find((segment) => segment.id === vm.networkId);
                return (
                  <tr key={vm.id} className="hover:bg-gray-50">
                    <td className="px-4 py-2 font-medium text-gray-900">{vm.hostname}</td>
                    <td className="px-4 py-2 text-gray-600">{vm.templateName || 'Not chosen'}</td>
                    <td className="px-4 py-2 text-gray-600">{network?.name ?? 'Unassigned'}</td>
                    <td className="px-4 py-2 text-gray-600">
                      {/* Isolation is the one thing on this screen that the deploy does send:
                          it becomes the network's is_isolated flag. */}
                      {network?.isolated ? 'Isolated network' : 'Routed network'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
