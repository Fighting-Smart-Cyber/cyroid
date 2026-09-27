// frontend/src/components/catalog/InstallProgressModal.tsx
import { useEffect, useRef, useState } from 'react';
import {
  AlertCircle,
  AlertTriangle,
  Ban,
  Check,
  ChevronRight,
  Loader2,
  SkipForward,
} from 'lucide-react';
import {
  InstallJobStatus,
  InstallLogLine,
  TERMINAL_STATES,
  catalogInstallApi,
  installErrorDetail,
} from '../../services/catalogInstall';
import { toast } from '../../stores/toastStore';
import { Modal, ModalBody, ModalFooter } from '../common/Modal';

interface Props {
  jobId: string;
  itemName: string;
  /** Called once the job reaches a terminal state, so the caller can refresh. */
  onFinished?: (status: InstallJobStatus) => void;
  onClose: () => void;
}

const POLL_MS = 1000;

const lineStyles: Record<InstallLogLine['level'], string> = {
  info: 'text-gray-400',
  step: 'text-gray-100',
  skip: 'text-gray-500',
  warning: 'text-amber-400',
  error: 'text-red-400',
};

function LineIcon({ level }: { level: InstallLogLine['level'] }) {
  const cls = 'h-3.5 w-3.5 mr-2 flex-shrink-0 mt-0.5';
  if (level === 'error') return <AlertCircle className={`${cls} text-red-400`} />;
  if (level === 'warning') return <AlertTriangle className={`${cls} text-amber-400`} />;
  if (level === 'skip') return <SkipForward className={`${cls} text-gray-500`} />;
  if (level === 'step') return <ChevronRight className={`${cls} text-indigo-400`} />;
  return <span className={cls} />;
}

export default function InstallProgressModal({
  jobId,
  itemName,
  onFinished,
  onClose,
}: Props) {
  const [status, setStatus] = useState<InstallJobStatus | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const logEnd = useRef<HTMLDivElement | null>(null);
  const finishedRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;

    const poll = async () => {
      try {
        const next = await catalogInstallApi.status(jobId);
        if (cancelled) return;
        setStatus(next);
        setPollError(null);
        if (TERMINAL_STATES.includes(next.status)) {
          if (!finishedRef.current) {
            finishedRef.current = true;
            onFinished?.(next);
          }
          return; // stop polling
        }
      } catch (err: unknown) {
        if (cancelled) return;
        setPollError(installErrorDetail(err, 'Lost contact with the install job.'));
      }
      timer = setTimeout(poll, POLL_MS);
    };

    poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // onFinished is intentionally not a dependency: re-subscribing on every
    // parent render would restart the poll loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  useEffect(() => {
    logEnd.current?.scrollIntoView({ block: 'nearest' });
  }, [status?.log?.length]);

  const handleCancel = async () => {
    setCancelling(true);
    try {
      await catalogInstallApi.cancel(jobId);
      toast.success('Install cancelled');
    } catch (err: unknown) {
      toast.error(installErrorDetail(err, 'Could not cancel the install'));
    } finally {
      setCancelling(false);
    }
  };

  const state = status?.status ?? 'pending';
  const running = state === 'pending' || state === 'running';
  const total = status?.total_steps ?? 0;
  const done = status?.progress ?? 0;
  const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const result = status?.result;

  return (
    <Modal
      isOpen={true}
      onClose={running ? () => undefined : onClose}
      title="Installing from Catalog"
      description={itemName}
      size="lg"
      showCloseButton={!running}
      closeOnBackdrop={!running}
      closeOnEscape={!running}
    >
      <ModalBody className="space-y-4">
        <div>
          <div className="flex items-center justify-between text-sm mb-1">
            <span className="font-medium text-gray-900 flex items-center">
              {running && <Loader2 className="h-4 w-4 mr-2 animate-spin text-indigo-600" />}
              {state === 'completed' && <Check className="h-4 w-4 mr-2 text-green-600" />}
              {state === 'failed' && <AlertCircle className="h-4 w-4 mr-2 text-red-600" />}
              {state === 'cancelled' && <Ban className="h-4 w-4 mr-2 text-gray-500" />}
              {status?.step || 'Queued...'}
            </span>
            <span className="text-gray-500 tabular-nums">
              {total > 0 ? `${done} / ${total}` : ''}
            </span>
          </div>
          <div className="h-2 w-full bg-gray-200 rounded-full overflow-hidden">
            <div
              className={`h-full transition-all duration-300 ${
                state === 'failed'
                  ? 'bg-red-500'
                  : state === 'cancelled'
                    ? 'bg-gray-400'
                    : 'bg-indigo-600'
              }`}
              style={{ width: `${state === 'completed' ? 100 : percent}%` }}
            />
          </div>
        </div>

        {pollError && (
          <div className="flex items-start p-3 rounded-lg bg-amber-50 text-amber-800">
            <AlertTriangle className="h-4 w-4 mr-2 flex-shrink-0 mt-0.5" />
            <p className="text-xs">{pollError} Retrying…</p>
          </div>
        )}

        <div className="bg-gray-900 rounded-lg p-3 max-h-72 overflow-y-auto">
          {(status?.log ?? []).length === 0 ? (
            <p className="text-xs text-gray-500">Waiting for the worker to pick this up…</p>
          ) : (
            <ul className="space-y-1">
              {(status?.log ?? []).map((line, i) => (
                <li
                  key={`${i}-${line.at ?? ''}`}
                  className={`flex items-start text-xs font-mono ${lineStyles[line.level] ?? 'text-gray-400'}`}
                >
                  <LineIcon level={line.level} />
                  <span className="break-all">{line.message}</span>
                </li>
              ))}
            </ul>
          )}
          <div ref={logEnd} />
        </div>

        {state === 'failed' && status?.error && (
          <div className="flex items-start p-3 rounded-lg bg-red-50 text-red-800">
            <AlertCircle className="h-4 w-4 mr-2 flex-shrink-0 mt-0.5" />
            <div className="text-xs">
              <p className="font-medium">{status.error}</p>
              {result?.failed_step && (
                <p className="mt-1">
                  Dependencies installed before this point were kept, so re-running
                  will pick up where it stopped.
                </p>
              )}
            </div>
          </div>
        )}

        {state === 'completed' && result && (
          <div className="bg-gray-50 rounded-lg p-3 text-xs text-gray-700 space-y-1">
            <p className="font-medium text-gray-900 text-sm">Installed</p>
            <p>
              {(result.installed ?? []).length} item(s) installed
              {(result.skipped ?? []).length > 0 &&
                `, ${(result.skipped ?? []).length} already present`}
            </p>
            {(result.missing ?? []).length > 0 && (
              <p className="text-amber-700">
                Missing from the catalog: {(result.missing ?? []).join(', ')}
              </p>
            )}
            {(result.warnings ?? []).map((w) => (
              <p key={w} className="text-amber-700">
                {w}
              </p>
            ))}
          </div>
        )}
      </ModalBody>

      <ModalFooter>
        {running ? (
          <button
            type="button"
            onClick={handleCancel}
            disabled={cancelling}
            className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50"
          >
            {cancelling ? 'Cancelling…' : 'Cancel install'}
          </button>
        ) : (
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-white bg-indigo-600 border border-transparent rounded-md hover:bg-indigo-700"
          >
            Done
          </button>
        )}
      </ModalFooter>
    </Modal>
  );
}
