// frontend/src/services/catalogInstall.ts
//
// One-click catalog install with dependency resolution and progress (PG-149).
// Outside api.ts on purpose: that file is being replaced by a generated client
// (UX-2), so new surface does not belong in it.

import { isAxiosError } from 'axios';
import { api } from './api';

export type InstallStepKind = 'base_image' | 'image' | 'content' | 'blueprint';
export type InstallJobState =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled';

export const TERMINAL_STATES: InstallJobState[] = ['completed', 'failed', 'cancelled'];

export interface InstallStep {
  key: string;
  kind: InstallStepKind;
  ref: string;
  name: string;
  /** What the progress log says while this step runs. */
  label: string;
  /** Already installed; will be skipped. */
  satisfied: boolean;
  /** False when the catalog does not contain it. */
  available: boolean;
  note: string;
}

export interface InstallPlan {
  item_id: string;
  steps: InstallStep[];
  /** Steps that will actually run. */
  total_steps: number;
  warnings: string[];
  summary: string;
}

export interface InstallJobStarted {
  job_id: string;
  status: InstallJobState;
  total_steps: number;
  message: string;
}

export interface InstallLogLine {
  message: string;
  level: 'info' | 'step' | 'skip' | 'warning' | 'error';
  at?: string | null;
}

export interface InstallJobResult {
  blueprint_id?: string | null;
  installed?: string[];
  skipped?: string[];
  missing?: string[];
  warnings?: string[];
  content_ids?: string[];
  failed_step?: string;
}

export interface InstallJobStatus {
  status: InstallJobState;
  step: string;
  progress: number;
  total_steps: number;
  current_item: string;
  error: string;
  result?: InstallJobResult | null;
  log: InstallLogLine[];
  updated_at?: string | null;
}

export function installErrorDetail(err: unknown, fallback: string): string {
  if (isAxiosError(err)) {
    const detail = err.response?.data?.detail;
    if (typeof detail === 'string') return detail;
  }
  return fallback;
}

export const catalogInstallApi = {
  /** What installing this blueprint will do, without doing any of it. */
  plan: async (sourceId: string, itemId: string): Promise<InstallPlan> => {
    const response = await api.get<InstallPlan>(
      `/catalog/items/${sourceId}/${encodeURIComponent(itemId)}/install-plan`
    );
    return response.data;
  },

  /** Queue the install. Returns a job id to poll. */
  start: async (
    itemId: string,
    sourceId: string,
    buildImages = true
  ): Promise<InstallJobStarted> => {
    const response = await api.post<InstallJobStarted>(
      `/catalog/items/${encodeURIComponent(itemId)}/install/start`,
      { source_id: sourceId, build_images: buildImages }
    );
    return response.data;
  },

  status: async (jobId: string): Promise<InstallJobStatus> => {
    const response = await api.get<InstallJobStatus>(`/catalog/install/${jobId}/status`);
    return response.data;
  },

  cancel: async (jobId: string): Promise<void> => {
    await api.post(`/catalog/install/${jobId}/cancel`);
  },
};
