// frontend/src/components/wizard-v2/steps/UsersStep.tsx
import { Users, Info } from 'lucide-react';
import { useWizardStore } from '../../../stores/wizardStore';

/**
 * What this step used to be, and why it is a summary now.
 *
 * It offered team segments with per-team head counts, generated usernames from them, and listed
 * access rules for red team, blue team, white cell and observers. None of it was ever sent: the
 * deploy call creates a range, its networks and its machines, and there is no endpoint behind any
 * of those settings -- no range-scoped account, no group, no access rule. A user who set them got
 * a range with none of it, and nothing told them so.
 *
 * The controls are gone rather than disabled because a disabled control still describes a feature
 * that exists somewhere, and this one does not. What is left is the part of the same subject the
 * wizard does apply: the login the deploy actually puts on each machine.
 */

/** Sudo is a Linux field; the deploy sends it only for machines that are not Windows. */
function isWindowsFamily(osFamily: string | undefined): boolean {
  return (osFamily ?? '').startsWith('windows');
}

export function UsersStep() {
  const { networks } = useWizardStore();
  const machines = networks.vms;

  return (
    <div className="max-w-5xl mx-auto">
      <h2 className="text-2xl font-bold text-gray-900 mb-2">Users &amp; Access</h2>
      <p className="text-gray-600 mb-8">
        Review the logins this range will be built with. Nothing on this step needs your input.
      </p>

      <div className="flex items-start gap-3 p-4 mb-8 rounded-lg border border-blue-200 bg-blue-50">
        <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
        <div className="text-sm text-blue-800 space-y-2">
          <p>
            The platform does not create accounts inside a range. Who may open a range and its
            consoles is decided by platform accounts and by training-event participation, both
            administered outside this wizard &mdash; on the Admin page&apos;s Users tab and on the
            training event itself.
          </p>
          <p>
            The one login the wizard does set is the one on each machine, edited on the Networks
            step by selecting a machine. Those are listed below.
          </p>
        </div>
      </div>

      <h3 className="text-lg font-semibold text-gray-900 mb-4">
        Machine logins ({machines.length})
      </h3>

      {machines.length === 0 ? (
        <div className="text-center py-12 bg-gray-50 rounded-lg border border-dashed border-gray-300">
          <Users className="w-12 h-12 text-gray-300 mx-auto mb-3" />
          <p className="text-gray-500">This range has no machines yet.</p>
          <p className="text-xs text-gray-400 mt-1">
            Add them on the Networks step to set their logins.
          </p>
        </div>
      ) : (
        <div className="border border-gray-200 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Machine</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Network</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Username</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Sudo</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {machines.map((vm) => {
                const network = networks.segments.find((segment) => segment.id === vm.networkId);
                const windows = isWindowsFamily(vm.osFamily);
                return (
                  <tr key={vm.id} className="hover:bg-gray-50">
                    <td className="px-4 py-2 font-medium text-gray-900">{vm.hostname}</td>
                    <td className="px-4 py-2 text-gray-600">{network?.name ?? 'Unassigned'}</td>
                    <td className="px-4 py-2 font-mono text-gray-900">
                      {vm.username ? (
                        vm.username
                      ) : (
                        <span className="font-sans text-gray-500">Image default</span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-gray-600">
                      {windows ? 'Not applicable' : vm.sudoEnabled === false ? 'No' : 'Yes'}
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
