// frontend/src/components/blueprints/ImportBlueprintModal.tsx
/**
 * Importing a blueprint package, and describing what is in it before anyone commits to it.
 *
 * Two things were wrong here beyond the wording. The file picker accepted `.tar.gz`, which both
 * import endpoints refuse outright, so choosing one produced a 400 and nothing else. And the
 * review step offered a "Template Conflict Strategy" for templates that have not existed since
 * the v2.0 format, over a validator that reports an empty list and an importer that reads the
 * option nowhere -- a control whose only effect was to make the user think they had made a
 * decision.
 *
 * A package now says which era wrote the blueprint inside it, and the review step names its
 * workloads and its capability scopes rather than reporting a Kubernetes package as empty.
 */
import { useState, useRef, useEffect, useCallback } from 'react';
import {
  blueprintsApi,
  BlueprintImportValidation,
  BlueprintImportOptions,
  BlueprintImportJobStatus,
} from '../../services/api';
import { apiErrorDetail } from '../../lib/blueprints';
import {
  Upload,
  Loader2,
  CheckCircle,
  XCircle,
  AlertTriangle,
  FileArchive,
  Ban,
} from 'lucide-react';
import Modal, { ModalBody, ModalFooter } from '../common/Modal';

interface Props {
  onClose: () => void;
  onSuccess: () => void;
}

type Step = 'upload' | 'validating' | 'review' | 'importing' | 'done';

/**
 * What the validator reports about a Kubernetes package. `BlueprintImportValidation` in
 * services/api.ts is hand-written and predates these fields; it is read here rather than widened
 * there because that file is being replaced by a generated client.
 */
interface PackageEra {
  blueprint_schema_version?: number;
  included_networks?: string[];
  included_workloads?: string[];
  included_capabilities?: string[];
}

const SCHEMA_VERSION_K8S = 2;

/** What each package format is, for the one line that names it. */
const PACKAGE_FORMATS: Record<string, string> = {
  '2.0': 'Legacy Range Export',
  '3.0': 'Blueprint Export',
  '4.0': 'Unified Range Blueprint',
  '5.0': 'Kubernetes Range Blueprint',
};

export default function ImportBlueprintModal({ onClose, onSuccess }: Props) {
  const [step, setStep] = useState<Step>('upload');
  const [file, setFile] = useState<File | null>(null);
  const [validation, setValidation] = useState<(BlueprintImportValidation & PackageEra) | null>(null);
  const [newName, setNewName] = useState('');
  const [contentStrategy, setContentStrategy] = useState<'skip' | 'rename' | 'use_existing'>('skip');
  const [error, setError] = useState<string | null>(null);
  const [importResult, setImportResult] = useState<{
    blueprintName?: string;
    dockerfilesExtracted?: string[];
    imagesBuilt?: string[];
    contentImported?: boolean;
    artifactsImported?: string[];
    warnings: string[];
  } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Async import state
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState<BlueprintImportJobStatus | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Clean up polling on unmount
  useEffect(() => {
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
      }
    };
  }, []);

  // Poll for import status
  const startPolling = useCallback((id: string) => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
    }

    const poll = async () => {
      try {
        const status = await blueprintsApi.getImportStatus(id);
        setJobStatus(status);

        if (status.status === 'completed') {
          if (pollRef.current) clearInterval(pollRef.current);

          // Extract result from job status
          const result = status.result;
          if (result) {
            setImportResult({
              blueprintName: result.blueprint_name,
              dockerfilesExtracted: result.dockerfiles_extracted,
              imagesBuilt: result.images_built,
              contentImported: result.content_imported,
              artifactsImported: result.artifacts_imported,
              warnings: result.warnings || [],
            });
          }
          setStep('done');
        } else if (status.status === 'failed') {
          if (pollRef.current) clearInterval(pollRef.current);
          setError(status.error || 'Import failed');
          setStep('review');
          setJobId(null);
          setJobStatus(null);
        } else if (status.status === 'cancelled') {
          if (pollRef.current) clearInterval(pollRef.current);
          setError('Import was cancelled');
          setStep('review');
          setJobId(null);
          setJobStatus(null);
          setCancelling(false);
        }
      } catch {
        // Ignore transient polling errors
      }
    };

    // Poll immediately, then every 2 seconds
    poll();
    pollRef.current = setInterval(poll, 2000);
  }, []);

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFile = e.target.files?.[0];
    if (!selectedFile) return;

    // Both import endpoints refuse anything that is not a .zip, so accepting a .tar.gz here only
    // bought the user a 400 they had no way to predict.
    if (!selectedFile.name.endsWith('.zip')) {
      setError('Please select a ZIP file');
      return;
    }

    setFile(selectedFile);
    setError(null);
    setStep('validating');

    try {
      const result = await blueprintsApi.validateImport(selectedFile);
      setValidation(result);
      setNewName(result.blueprint_name);
      setStep('review');
    } catch (err: unknown) {
      setError(apiErrorDetail(err, 'Failed to validate blueprint'));
      setStep('upload');
    }
  };

  const handleImport = async () => {
    if (!file) return;

    setStep('importing');
    setError(null);

    try {
      const options: BlueprintImportOptions = {
        content_conflict_strategy: contentStrategy,
      };

      // Only set new_name if it's different from the original
      if (validation && newName !== validation.blueprint_name) {
        options.new_name = newName;
      }

      // Start async import
      const { job_id } = await blueprintsApi.importStart(file, options);
      setJobId(job_id);
      startPolling(job_id);
    } catch (err: unknown) {
      setError(apiErrorDetail(err, 'Failed to start import'));
      setStep('review');
    }
  };

  const handleCancel = async () => {
    if (!jobId) return;
    setCancelling(true);
    try {
      await blueprintsApi.cancelImport(jobId);
    } catch {
      // Cancellation request sent, polling will pick up the status change
    }
  };

  const handleDone = () => {
    onSuccess();
    onClose();
  };

  const resetModal = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    setFile(null);
    setValidation(null);
    setNewName('');
    setContentStrategy('skip');
    setError(null);
    setImportResult(null);
    setJobId(null);
    setJobStatus(null);
    setCancelling(false);
    setStep('upload');
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  };

  // Determine if close should be disabled (during async operations)
  const isProcessing = step === 'validating' || step === 'importing';

  // Dynamic description based on step
  const getDescription = () => {
    switch (step) {
      case 'upload':
        return 'Upload a blueprint package';
      case 'validating':
        return 'Validating...';
      case 'review':
        return 'Review import';
      case 'importing':
        return jobStatus?.step || 'Importing...';
      case 'done':
        return 'Import complete';
      default:
        return undefined;
    }
  };

  // Compute progress percentage for the bar
  const progressPercent = jobStatus && jobStatus.total_steps > 0
    ? Math.round((jobStatus.progress / jobStatus.total_steps) * 100)
    : 0;

  const workloads = validation?.included_workloads ?? [];
  const capabilities = validation?.included_capabilities ?? [];
  const isKubernetesPackage = validation?.blueprint_schema_version === SCHEMA_VERSION_K8S;

  return (
    <Modal
      isOpen={true}
      onClose={onClose}
      title="Import Blueprint"
      description={getDescription()}
      size="lg"
      showCloseButton={!isProcessing}
      closeOnBackdrop={!isProcessing}
      closeOnEscape={!isProcessing}
    >
      <ModalBody>
        {/* Upload Step */}
        {step === 'upload' && (
          <div className="space-y-4">
            <div
              className="border-2 border-dashed border-gray-300 rounded-lg p-8 text-center cursor-pointer hover:border-indigo-400"
              onClick={() => fileInputRef.current?.click()}
            >
              <FileArchive className="mx-auto h-12 w-12 text-gray-400" />
              <p className="mt-2 text-sm text-gray-600">
                Click to select a blueprint package
              </p>
              <p className="text-xs text-gray-500 mt-1">
                A .zip written by Export Blueprint on this or another install
              </p>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              accept=".zip"
              onChange={handleFileSelect}
              className="hidden"
            />
            {error && (
              <div className="flex items-center text-red-600 text-sm">
                <XCircle className="h-4 w-4 mr-2" />
                {error}
              </div>
            )}
          </div>
        )}

        {/* Validating Step */}
        {step === 'validating' && (
          <div className="flex flex-col items-center py-8">
            <Loader2 className="h-8 w-8 animate-spin text-indigo-600" />
            <p className="mt-4 text-sm text-gray-600">Validating blueprint package...</p>
          </div>
        )}

        {/* Review Step */}
        {step === 'review' && validation && (
          <div className="space-y-4">
            {/* Validation Status */}
            <div
              className={`flex items-center p-3 rounded-md ${
                validation.valid
                  ? 'bg-green-50 text-green-700'
                  : 'bg-red-50 text-red-700'
              }`}
            >
              {validation.valid ? (
                <CheckCircle className="h-5 w-5 mr-2" />
              ) : (
                <XCircle className="h-5 w-5 mr-2" />
              )}
              <span className="text-sm font-medium">
                {validation.valid ? 'Blueprint is valid' : 'Blueprint has issues'}
              </span>
            </div>

            {/* Format Version */}
            {validation.manifest_version && (
              <div className="text-xs text-gray-500 -mt-2">
                Package format: v{validation.manifest_version}
                {PACKAGE_FORMATS[validation.manifest_version] &&
                  ` (${PACKAGE_FORMATS[validation.manifest_version]})`}
              </div>
            )}

            {/* Blueprint Name */}
            <div>
              <label className="block text-sm font-medium text-gray-700">
                Blueprint Name
              </label>
              <input
                type="text"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                className="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
              />
              {validation.conflicts.length > 0 && (
                <p className="mt-1 text-xs text-amber-600">
                  A blueprint with this name already exists. Change the name to import.
                </p>
              )}
            </div>

            {/* Package Contents Summary */}
            <div className="bg-gray-50 rounded-md p-3">
              <h4 className="text-sm font-medium text-gray-700 mb-2">Package Contents</h4>
              <div className="grid grid-cols-2 gap-2 text-sm">
                {validation.included_networks && validation.included_networks.length > 0 && (
                  <div className="flex items-center text-gray-600">
                    <span className="w-2 h-2 bg-green-400 rounded-full mr-2" />
                    {validation.included_networks.length}{' '}
                    {isKubernetesPackage ? 'network attachment(s)' : 'network(s)'}
                  </div>
                )}
                {workloads.length > 0 && (
                  <div className="flex items-center text-gray-600">
                    <span className="w-2 h-2 bg-green-400 rounded-full mr-2" />
                    {workloads.length} machine(s)
                  </div>
                )}
                {validation.msel_included && (
                  <div className="flex items-center text-gray-600">
                    <span className="w-2 h-2 bg-green-400 rounded-full mr-2" />
                    MSEL / Injects
                  </div>
                )}
                {validation.included_dockerfiles && validation.included_dockerfiles.length > 0 && (
                  <div className="flex items-center text-gray-600">
                    <span className="w-2 h-2 bg-green-400 rounded-full mr-2" />
                    {validation.included_dockerfiles.length} Dockerfile(s)
                  </div>
                )}
                {validation.content_included && (
                  <div className="flex items-center text-gray-600">
                    <span className="w-2 h-2 bg-green-400 rounded-full mr-2" />
                    Content Library
                  </div>
                )}
                {validation.included_artifacts && validation.included_artifacts.length > 0 && (
                  <div className="flex items-center text-gray-600">
                    <span className="w-2 h-2 bg-green-400 rounded-full mr-2" />
                    {validation.included_artifacts.length} Artifact(s)
                  </div>
                )}
              </div>
            </div>

            {/* Capability packages. Scope is required on every capability and has no default, so
                it is shown here: it decides whether a reset touches one learner or all of them. */}
            {capabilities.length > 0 && (
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  Capability Packages ({capabilities.length})
                </label>
                <div className="bg-gray-50 rounded-md p-3 max-h-32 overflow-y-auto">
                  <ul className="text-sm text-gray-600 space-y-1">
                    {capabilities.map((capability) => (
                      <li key={capability} className="flex items-center">
                        <span className="w-2 h-2 bg-indigo-400 rounded-full mr-2" />
                        {capability}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            )}

            {/* Content Library Info & Conflict */}
            {validation.content_included && (
              <div className="bg-blue-50 rounded-md p-3">
                <h4 className="text-sm font-medium text-blue-800 mb-2">
                  Content Library Item Included
                </h4>
                {validation.content_conflict ? (
                  <div className="space-y-3">
                    <p className="text-sm text-amber-700">
                      Warning: Content with this title already exists: "{validation.content_conflict}"
                    </p>
                    <div>
                      <label className="block text-sm font-medium text-gray-700 mb-1">
                        Content Conflict Strategy
                      </label>
                      <select
                        value={contentStrategy}
                        onChange={(e) =>
                          setContentStrategy(e.target.value as 'skip' | 'rename' | 'use_existing')
                        }
                        className="block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
                      >
                        <option value="use_existing">Use existing content (don't import)</option>
                        <option value="rename">Import with new name (add suffix)</option>
                        <option value="skip">Skip content import entirely</option>
                      </select>
                    </div>
                  </div>
                ) : (
                  <p className="text-sm text-blue-700">
                    Content will be imported as a new item
                  </p>
                )}
              </div>
            )}

            {/* Errors */}
            {validation.errors.length > 0 && (
              <div className="bg-red-50 rounded-md p-3">
                <h4 className="text-sm font-medium text-red-800 mb-2">Errors</h4>
                <ul className="text-sm text-red-700 space-y-1">
                  {validation.errors.map((err, i) => (
                    <li key={i}>{err}</li>
                  ))}
                </ul>
              </div>
            )}

            {/* Warnings */}
            {validation.warnings.length > 0 && (
              <div className="bg-amber-50 rounded-md p-3">
                <h4 className="text-sm font-medium text-amber-800 mb-2 flex items-center">
                  <AlertTriangle className="h-4 w-4 mr-1" />
                  Warnings
                </h4>
                <ul className="text-sm text-amber-700 space-y-1">
                  {validation.warnings.map((warn, i) => (
                    <li key={i}>{warn}</li>
                  ))}
                </ul>
              </div>
            )}

            {error && (
              <div className="flex items-center text-red-600 text-sm">
                <XCircle className="h-4 w-4 mr-2" />
                {error}
              </div>
            )}
          </div>
        )}

        {/* Importing Step — with progress */}
        {step === 'importing' && (
          <div className="space-y-4 py-4">
            {/* Progress bar */}
            <div>
              <div className="flex justify-between text-sm text-gray-600 mb-1">
                <span>{jobStatus?.step || 'Starting import...'}</span>
                <span>Step {jobStatus?.progress || 0} of {jobStatus?.total_steps || 5}</span>
              </div>
              <div className="w-full bg-gray-200 rounded-full h-2.5">
                <div
                  className="bg-indigo-600 h-2.5 rounded-full transition-all duration-500"
                  style={{ width: `${progressPercent}%` }}
                />
              </div>
            </div>

            {/* Current item detail */}
            {jobStatus?.current_item && (
              <div className="flex items-center text-sm text-gray-500">
                <Loader2 className="h-4 w-4 animate-spin mr-2 text-indigo-500" />
                {jobStatus.current_item}
              </div>
            )}

            {/* Spinner when no detail */}
            {!jobStatus?.current_item && (
              <div className="flex justify-center">
                <Loader2 className="h-6 w-6 animate-spin text-indigo-600" />
              </div>
            )}
          </div>
        )}

        {/* Done Step */}
        {step === 'done' && importResult && (
          <div className="space-y-4">
            <div className="flex items-center p-3 rounded-md bg-green-50 text-green-700">
              <CheckCircle className="h-5 w-5 mr-2" />
              <span className="text-sm font-medium">
                Blueprint "{importResult.blueprintName}" imported successfully
              </span>
            </div>

            {/* Import Summary */}
            <div className="bg-gray-50 rounded-md p-3">
              <h4 className="text-sm font-medium text-gray-700 mb-2">Import Summary</h4>
              <div className="grid grid-cols-2 gap-2 text-sm text-gray-600">
                {workloads.length > 0 && (
                  <div className="flex items-center">
                    <CheckCircle className="h-3 w-3 text-green-500 mr-2" />
                    {workloads.length} machine(s)
                  </div>
                )}
                {capabilities.length > 0 && (
                  <div className="flex items-center">
                    <CheckCircle className="h-3 w-3 text-green-500 mr-2" />
                    {capabilities.length} capability package(s)
                  </div>
                )}
                {importResult.dockerfilesExtracted && importResult.dockerfilesExtracted.length > 0 && (
                  <div className="flex items-center">
                    <CheckCircle className="h-3 w-3 text-green-500 mr-2" />
                    {importResult.dockerfilesExtracted.length} Dockerfile(s)
                  </div>
                )}
                {importResult.imagesBuilt && importResult.imagesBuilt.length > 0 && (
                  <div className="flex items-center">
                    <CheckCircle className="h-3 w-3 text-green-500 mr-2" />
                    {importResult.imagesBuilt.length} Image(s) built
                  </div>
                )}
                {importResult.contentImported && (
                  <div className="flex items-center">
                    <CheckCircle className="h-3 w-3 text-green-500 mr-2" />
                    Content Library item
                  </div>
                )}
                {importResult.artifactsImported && importResult.artifactsImported.length > 0 && (
                  <div className="flex items-center">
                    <CheckCircle className="h-3 w-3 text-green-500 mr-2" />
                    {importResult.artifactsImported.length} Artifact(s)
                  </div>
                )}
              </div>
            </div>

            {importResult.warnings.length > 0 && (
              <div className="bg-amber-50 rounded-md p-3">
                <h4 className="text-sm font-medium text-amber-800 mb-2">Warnings</h4>
                <ul className="text-sm text-amber-700 space-y-1">
                  {importResult.warnings.map((warn, i) => (
                    <li key={i}>{warn}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </ModalBody>

      {/* Footer */}
      <ModalFooter>
        {step === 'upload' && (
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50"
          >
            Cancel
          </button>
        )}

        {step === 'review' && (
          <>
            <button
              type="button"
              onClick={resetModal}
              className="px-4 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50"
            >
              Back
            </button>
            <button
              type="button"
              onClick={handleImport}
              disabled={
                !newName ||
                !validation?.valid ||
                ((validation?.conflicts.length ?? 0) > 0 && newName === validation?.blueprint_name)
              }
              className="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50"
            >
              <Upload className="h-4 w-4 mr-2" />
              Import Blueprint
            </button>
          </>
        )}

        {step === 'importing' && (
          <button
            type="button"
            onClick={handleCancel}
            disabled={cancelling}
            className="inline-flex items-center px-4 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50"
          >
            <Ban className="h-4 w-4 mr-2" />
            {cancelling ? 'Cancelling...' : 'Cancel Import'}
          </button>
        )}

        {step === 'done' && (
          <button
            type="button"
            onClick={handleDone}
            className="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700"
          >
            <CheckCircle className="h-4 w-4 mr-2" />
            Done
          </button>
        )}
      </ModalFooter>
    </Modal>
  );
}
