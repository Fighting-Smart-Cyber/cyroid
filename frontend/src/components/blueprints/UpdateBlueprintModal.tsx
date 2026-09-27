// frontend/src/components/blueprints/UpdateBlueprintModal.tsx
/**
 * "Update blueprint from this range" — rebuild a blueprint's config out of a running instance.
 *
 * It reads the range's network and VM rows, which only an Era A range has, so against a
 * Kubernetes blueprint the server answers 409 and nothing is written. That refusal is the whole
 * message here: it says why and what to edit instead, so it stays on the page rather than in a
 * toast that is gone before the user has read it.
 */
import { useState } from 'react';
import { blueprintsApi } from '../../services/api';
import { AlertTriangle, RefreshCw, Loader2 } from 'lucide-react';
import { toast } from '../../stores/toastStore';
import { apiErrorDetail } from '../../lib/blueprints';
import { Modal, ModalBody, ModalFooter } from '../common/Modal';

interface Props {
  blueprintId: string;
  blueprintName: string;
  currentVersion: number;
  rangeId: string;
  onClose: () => void;
  onSuccess: () => void;
}

export default function UpdateBlueprintModal({
  blueprintId,
  blueprintName,
  currentVersion,
  rangeId,
  onClose,
  onSuccess,
}: Props) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleUpdate = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await blueprintsApi.updateFromRange(blueprintId, rangeId);
      toast.success(`Blueprint updated to version ${currentVersion + 1}`);
      onSuccess();
      onClose();
    } catch (err: unknown) {
      setError(apiErrorDetail(err, 'The server refused the update and did not say why.'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      isOpen={true}
      onClose={onClose}
      title="Update Blueprint"
      description={`Update ${blueprintName} to a new version`}
      size="sm"
      closeOnBackdrop={!submitting}
      closeOnEscape={!submitting}
    >
      <ModalBody>
        <p className="text-sm text-gray-600 mb-4">
          Update <span className="font-medium">{blueprintName}</span> to version {currentVersion + 1}?
        </p>
        <p className="text-xs text-gray-500">
          Existing instances will remain on their original versions until redeployed.
        </p>

        {error && (
          <div className="mt-4 rounded-md bg-red-50 border border-red-100 p-3 flex items-start gap-2">
            <AlertTriangle className="h-4 w-4 text-red-600 mt-0.5 flex-shrink-0" />
            <div className="text-sm text-red-800 whitespace-pre-wrap">{error}</div>
          </div>
        )}
      </ModalBody>

      <ModalFooter>
        <button
          onClick={onClose}
          disabled={submitting}
          className="px-4 py-2 border border-gray-300 rounded-md text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50"
        >
          {error ? 'Close' : 'Cancel'}
        </button>
        <button
          onClick={handleUpdate}
          disabled={submitting}
          className="inline-flex items-center px-4 py-2 border border-transparent rounded-md text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50"
        >
          {submitting ? (
            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
          ) : (
            <RefreshCw className="h-4 w-4 mr-2" />
          )}
          Update Blueprint
        </button>
      </ModalFooter>
    </Modal>
  );
}
