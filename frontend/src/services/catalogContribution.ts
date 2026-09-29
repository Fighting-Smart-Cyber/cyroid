// frontend/src/services/catalogContribution.ts
//
// Contributing a locally-improved blueprint back to the catalog it came from
// (PG-147). This lives outside api.ts on purpose: that file is being replaced
// by a client generated from the OpenAPI schema (UX-2), so new surface area
// does not belong in it.

import { api } from './api';

export interface BlueprintFieldChange {
  /** Opaque stable identifier from the API; send this back to select the change. */
  key: string;
  /** Location in blueprint.yaml, segment by segment. */
  path: string[];
  kind: 'changed' | 'added' | 'removed';
  label: string;
  before: unknown;
  after: unknown;
}

export interface BlueprintCatalogOrigin {
  source_id: string;
  source_name: string;
  source_url: string;
  source_branch: string | null;
  item_id: string;
  item_name: string;
  installed_version: string;
  item_path: string;
}

export interface BlueprintCatalogDiff {
  origin: BlueprintCatalogOrigin;
  changes: BlueprintFieldChange[];
  /** Local changes that cannot be expressed in blueprint.yaml. */
  notes: string[];
  has_changes: boolean;
}

export interface BlueprintContribution {
  origin: BlueprintCatalogOrigin;
  patch: string;
  blueprint_yaml: string;
  applied: string[];
  notes: string[];
  /** False when the patch will not apply cleanly with `git apply`. */
  applies_to_source: boolean;
  suggested_filename: string;
}

export const catalogContributionApi = {
  /** Which fields differ from the catalog version this blueprint came from. */
  diff: async (blueprintId: string): Promise<BlueprintCatalogDiff> => {
    const response = await api.get<BlueprintCatalogDiff>(
      `/blueprints/${blueprintId}/catalog-diff`
    );
    return response.data;
  },

  /**
   * Render the selected changes as a patch against the catalog's blueprint.yaml.
   * Pass no keys to contribute every change. Nothing is sent anywhere: the
   * patch comes back to the caller.
   */
  build: async (
    blueprintId: string,
    changes?: string[]
  ): Promise<BlueprintContribution> => {
    const response = await api.post<BlueprintContribution>(
      `/blueprints/${blueprintId}/catalog-contribution`,
      { changes: changes ?? null }
    );
    return response.data;
  },
};
