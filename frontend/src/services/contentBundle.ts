// frontend/src/services/contentBundle.ts
//
// The git-native content bundle (PG-104). Outside api.ts on purpose: that file
// is being replaced by a generated client (UX-2).

import { isAxiosError } from 'axios';
import { api } from './api';

export interface BundleConflict {
  field: string;
  local: unknown;
  incoming: unknown;
  summary: string;
  /** One-line human-readable form. */
  description: string;
}

export type BundleAction = 'create' | 'update' | 'unchanged' | 'conflict';

export interface BundleDecision {
  slug: string;
  title: string;
  action: BundleAction;
  content_id: string | null;
  conflicts: BundleConflict[];
  reason: string;
  /** Whether carrying this out changes anything. */
  writes: boolean;
}

export interface BundleImportResult {
  decision: BundleDecision;
  content_id: string | null;
  imported: boolean;
}

export function bundleErrorDetail(err: unknown, fallback: string): string {
  if (isAxiosError(err)) {
    const detail = err.response?.data?.detail;
    if (typeof detail === 'string') return detail;
  }
  return fallback;
}

export const contentBundleApi = {
  /** Download content as a reviewable bundle archive. */
  export: async (contentId: string): Promise<Blob> => {
    const response = await api.get(`/content/${contentId}/bundle`, {
      responseType: 'blob',
    });
    return response.data as Blob;
  },

  /** What importing this bundle would do, without writing anything. */
  preview: async (file: File, overwrite = false): Promise<BundleDecision> => {
    const form = new FormData();
    form.append('file', file);
    const response = await api.post<BundleDecision>(
      `/content/bundle/preview?overwrite=${overwrite}`,
      form,
      { headers: { 'Content-Type': 'multipart/form-data' } }
    );
    return response.data;
  },

  /** Import a bundle. Idempotent; conflicts are reported, not clobbered. */
  import: async (file: File, overwrite = false): Promise<BundleImportResult> => {
    const form = new FormData();
    form.append('file', file);
    const response = await api.post<BundleImportResult>(
      `/content/bundle/import?overwrite=${overwrite}`,
      form,
      { headers: { 'Content-Type': 'multipart/form-data' } }
    );
    return response.data;
  },
};
