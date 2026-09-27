// frontend/src/components/blueprints/ContributeToCatalogModal.tsx
import { isAxiosError } from 'axios';
import { useEffect, useState } from 'react';
import {
  AlertCircle,
  Check,
  Copy,
  Download,
  GitPullRequest,
  Info,
  Loader2,
} from 'lucide-react';
import {
  BlueprintCatalogDiff,
  BlueprintContribution,
  BlueprintFieldChange,
  catalogContributionApi,
} from '../../services/catalogContribution';
import { toast } from '../../stores/toastStore';
import { Modal, ModalBody, ModalFooter } from '../common/Modal';

interface Props {
  blueprintId: string;
  blueprintName: string;
  onClose: () => void;
}

const kindStyles: Record<BlueprintFieldChange['kind'], string> = {
  changed: 'bg-amber-100 text-amber-800',
  added: 'bg-green-100 text-green-800',
  removed: 'bg-red-100 text-red-800',
};

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function apiErrorDetail(err: unknown, fallback: string): string {
  if (isAxiosError(err)) {
    const detail = err.response?.data?.detail;
    if (typeof detail === 'string') return detail;
  }
  return fallback;
}

export default function ContributeToCatalogModal({
  blueprintId,
  blueprintName,
  onClose,
}: Props) {
  const [diff, setDiff] = useState<BlueprintCatalogDiff | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [building, setBuilding] = useState(false);
  const [contribution, setContribution] = useState<BlueprintContribution | null>(null);

  useEffect(() => {
    let cancelled = false;
    catalogContributionApi
      .diff(blueprintId)
      .then((result) => {
        if (cancelled) return;
        setDiff(result);
        setSelected(new Set(result.changes.map((c) => c.key)));
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setLoadError(
          apiErrorDetail(
            err,
            'Could not compare this blueprint with its catalog.'
          )
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [blueprintId]);

  const toggle = (key: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const setAll = (on: boolean) =>
    setSelected(on ? new Set((diff?.changes ?? []).map((c) => c.key)) : new Set());

  const handleBuild = async () => {
    setBuilding(true);
    try {
      setContribution(await catalogContributionApi.build(blueprintId, [...selected]));
    } catch (err: unknown) {
      toast.error(apiErrorDetail(err, 'Could not build the contribution'));
    } finally {
      setBuilding(false);
    }
  };

  const handleCopy = async (text: string, what: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success(`${what} copied to clipboard`);
    } catch {
      toast.error('Could not copy to clipboard');
    }
  };

  const handleDownload = (text: string, filename: string) => {
    const url = window.URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    window.URL.revokeObjectURL(url);
    document.body.removeChild(a);
  };

  const busy = building;

  return (
    <Modal
      isOpen={true}
      onClose={onClose}
      title="Contribute to Catalog"
      description={`Send your changes to ${blueprintName} back to the catalog it came from`}
      size="xl"
      showCloseButton={!busy}
      closeOnBackdrop={!busy}
      closeOnEscape={!busy}
    >
      <ModalBody className="space-y-4">
        {loading && (
          <div className="flex items-center justify-center py-10 text-gray-500">
            <Loader2 className="h-5 w-5 animate-spin mr-2" />
            Comparing with the catalog version…
          </div>
        )}

        {!loading && loadError && (
          <div className="flex items-start p-3 rounded-lg bg-amber-50 text-amber-800">
            <AlertCircle className="h-5 w-5 mr-2 flex-shrink-0 mt-0.5" />
            <p className="text-sm">{loadError}</p>
          </div>
        )}

        {!loading && diff && !contribution && (
          <>
            <div className="bg-gray-50 rounded-lg p-3 text-sm text-gray-700">
              <p className="font-medium text-gray-900">{diff.origin.item_name}</p>
              <p className="text-xs text-gray-500 mt-0.5">
                {diff.origin.source_name} · {diff.origin.item_path} · installed v
                {diff.origin.installed_version}
              </p>
            </div>

            {diff.notes.map((note) => (
              <div
                key={note}
                className="flex items-start p-3 rounded-lg bg-blue-50 text-blue-800"
              >
                <Info className="h-4 w-4 mr-2 flex-shrink-0 mt-0.5" />
                <p className="text-xs">{note}</p>
              </div>
            ))}

            {!diff.has_changes ? (
              <div className="flex items-center justify-center py-8 text-gray-500 text-sm">
                <Check className="h-5 w-5 mr-2 text-green-500" />
                This blueprint matches its catalog version — nothing to contribute.
              </div>
            ) : (
              <>
                <div className="flex items-center justify-between">
                  <p className="text-sm text-gray-600">
                    Select the changes to include:
                  </p>
                  <div className="text-xs space-x-3">
                    <button
                      type="button"
                      onClick={() => setAll(true)}
                      className="text-indigo-600 hover:text-indigo-800"
                    >
                      Select all
                    </button>
                    <button
                      type="button"
                      onClick={() => setAll(false)}
                      className="text-indigo-600 hover:text-indigo-800"
                    >
                      Clear
                    </button>
                  </div>
                </div>

                <div className="space-y-2 max-h-80 overflow-y-auto">
                  {diff.changes.map((change) => (
                    <label
                      key={change.key}
                      className="flex items-start p-3 border rounded-lg hover:bg-gray-50 cursor-pointer"
                    >
                      <input
                        type="checkbox"
                        checked={selected.has(change.key)}
                        onChange={() => toggle(change.key)}
                        className="h-4 w-4 mt-0.5 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded"
                      />
                      <div className="ml-3 flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="font-medium text-sm text-gray-900">
                            {change.label}
                          </span>
                          <span
                            className={`px-1.5 py-0.5 rounded text-[10px] uppercase font-medium ${kindStyles[change.kind]}`}
                          >
                            {change.kind}
                          </span>
                        </div>
                        <p className="text-xs text-gray-500 mt-1 font-mono break-all">
                          {change.kind !== 'added' && (
                            <span className="text-red-600">{renderValue(change.before)}</span>
                          )}
                          {change.kind === 'changed' && <span className="mx-1">→</span>}
                          {change.kind !== 'removed' && (
                            <span className="text-green-700">{renderValue(change.after)}</span>
                          )}
                        </p>
                      </div>
                    </label>
                  ))}
                </div>
              </>
            )}
          </>
        )}

        {contribution && (
          <>
            {!contribution.applies_to_source && (
              <div className="flex items-start p-3 rounded-lg bg-amber-50 text-amber-800">
                <AlertCircle className="h-5 w-5 mr-2 flex-shrink-0 mt-0.5" />
                <p className="text-xs">
                  This patch will not apply cleanly with <code>git apply</code>. Use the
                  full blueprint.yaml below instead.
                </p>
              </div>
            )}

            <div>
              <div className="flex items-center justify-between mb-2">
                <p className="text-sm font-medium text-gray-900">
                  Patch · {contribution.origin.item_path}
                </p>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => handleCopy(contribution.patch, 'Patch')}
                    className="inline-flex items-center px-2 py-1 text-xs border rounded hover:bg-gray-50"
                  >
                    <Copy className="h-3 w-3 mr-1" /> Copy
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      handleDownload(contribution.patch, contribution.suggested_filename)
                    }
                    className="inline-flex items-center px-2 py-1 text-xs border rounded hover:bg-gray-50"
                  >
                    <Download className="h-3 w-3 mr-1" /> Download
                  </button>
                </div>
              </div>
              <pre className="bg-gray-900 text-gray-100 text-xs p-3 rounded-lg overflow-auto max-h-72">
                {contribution.patch}
              </pre>
            </div>

            <div className="bg-gray-50 rounded-lg p-3 space-y-2">
              <p className="text-sm font-medium text-gray-900">Next steps</p>
              <ol className="text-xs text-gray-600 list-decimal list-inside space-y-1">
                <li>
                  Clone the catalog:{' '}
                  <code className="font-mono">git clone {contribution.origin.source_url}</code>
                </li>
                <li>
                  Create a branch from{' '}
                  <code className="font-mono">
                    {contribution.origin.source_branch || 'the default branch'}
                  </code>
                </li>
                <li>
                  {contribution.applies_to_source ? (
                    <>
                      Apply the patch:{' '}
                      <code className="font-mono">
                        git apply {contribution.suggested_filename}
                      </code>
                    </>
                  ) : (
                    <>
                      Replace{' '}
                      <code className="font-mono">{contribution.origin.item_path}</code> with
                      the full file below
                    </>
                  )}
                </li>
                <li>Commit, push, and open a pull request on the catalog</li>
              </ol>
              <button
                type="button"
                onClick={() =>
                  handleCopy(contribution.blueprint_yaml, 'blueprint.yaml')
                }
                className="inline-flex items-center px-2 py-1 text-xs border rounded bg-white hover:bg-gray-50"
              >
                <Copy className="h-3 w-3 mr-1" /> Copy full blueprint.yaml
              </button>
            </div>
          </>
        )}
      </ModalBody>

      <ModalFooter>
        <button
          type="button"
          onClick={onClose}
          disabled={busy}
          className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50"
        >
          {contribution ? 'Done' : 'Cancel'}
        </button>
        {!contribution && (
          <button
            type="button"
            onClick={handleBuild}
            disabled={busy || loading || !diff?.has_changes || selected.size === 0}
            className="inline-flex items-center px-4 py-2 text-sm font-medium text-white bg-indigo-600 border border-transparent rounded-md hover:bg-indigo-700 disabled:opacity-50"
          >
            {building ? (
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            ) : (
              <GitPullRequest className="h-4 w-4 mr-2" />
            )}
            Generate patch
            {selected.size > 0 && ` (${selected.size})`}
          </button>
        )}
      </ModalFooter>
    </Modal>
  );
}
