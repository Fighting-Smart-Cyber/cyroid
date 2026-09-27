// frontend/src/components/blueprints/ExportBlueprintModal.tsx
/**
 * What leaves the install when a blueprint is exported, described in the words of the era that
 * wrote it.
 *
 * This offered "Dockerfiles" and sold "Docker Image Tarballs" as the way to reach an air-gapped
 * site. On Kubernetes neither exists: there is no /data/images to read, no daemon to save an
 * image from, and a range's images are digest-pinned references the cluster resolves for itself
 * (ADR-0007). Both options are therefore absent here rather than present and inert -- an export
 * that promises to carry images it cannot carry is worse than one that says what it carries.
 */
import { useState } from 'react';
import { Blueprint, BlueprintExportOptions, blueprintsApi } from '../../services/api';
import {
  apiErrorDetail,
  blueprintCapabilities,
  blueprintLegacyVms,
  blueprintNetworks,
  blueprintWorkloads,
  isKubernetesBlueprint,
} from '../../lib/blueprints';
import { useSubstrate } from '../../stores/capabilitiesStore';
import { Download, Loader2, AlertCircle, FileCode, Package, BookOpen, FileArchive, HardDrive, ShieldCheck } from 'lucide-react';
import { toast } from '../../stores/toastStore';
import { Modal, ModalBody, ModalFooter } from '../common/Modal';

interface Props {
  /** The detail response carries `config`; the list response does not, hence optional. */
  blueprint: Blueprint & { config?: unknown };
  onClose: () => void;
}

export default function ExportBlueprintModal({ blueprint, onClose }: Props) {
  const { isKubernetes } = useSubstrate();
  const config = blueprint.config;

  // Two different questions, and conflating them mislabels a blueprint.
  //
  // What the package will *contain* is the blueprint's own era and nothing else: a v1 blueprint
  // declares VMs and Docker networks wherever it happens to be sitting, and reading its machines
  // out of `workloads` on a Kubernetes host just returns nothing.
  const blueprintIsEraB = isKubernetesBlueprint(config);
  // What the exporter can *gather* is either one: Dockerfiles and image tarballs need Era A
  // inputs and a daemon to read them with, so a v1 blueprint on a Kubernetes host has neither.
  const eraB = blueprintIsEraB || isKubernetes;

  // Nothing in the UI writes `artifact_ids` today, so a blueprint that does not declare them
  // would export none however this were set. The option appears only when there is something
  // for it to collect.
  const artifactIds = (config as { artifact_ids?: unknown } | undefined)?.artifact_ids;
  const canExportArtifacts = Array.isArray(artifactIds) && artifactIds.length > 0;

  const [options, setOptions] = useState<BlueprintExportOptions>({
    include_msel: true,
    include_dockerfiles: !eraB,
    include_docker_images: false,
    include_content: true,
    include_artifacts: false,
  });
  const [exporting, setExporting] = useState(false);

  const handleExport = async () => {
    setExporting(true);
    try {
      // Sent explicitly rather than left to the API's defaults, and derived from the era rather
      // than from state: the substrate answer arrives asynchronously, so a checkbox that was
      // never rendered must not leave a stale `true` behind for the server to act on.
      const blob = await blueprintsApi.export(blueprint.id, {
        ...options,
        include_dockerfiles: eraB ? false : options.include_dockerfiles,
        include_docker_images: eraB ? false : options.include_docker_images,
        include_artifacts: canExportArtifacts ? options.include_artifacts : false,
      });
      // Create download link
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `blueprint-${blueprint.name.replace(/[^a-zA-Z0-9-_]/g, '_')}.zip`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
      toast.success('Blueprint exported successfully');
      onClose();
    } catch (err: unknown) {
      toast.error(apiErrorDetail(err, 'Failed to export blueprint'));
    } finally {
      setExporting(false);
    }
  };

  const toggleOption = (key: keyof BlueprintExportOptions) => {
    setOptions((prev) => ({
      ...prev,
      [key]: !prev[key],
    }));
  };

  const networkCount = blueprintNetworks(config).length;
  const machineCount = blueprintIsEraB
    ? blueprintWorkloads(config).length
    : blueprintLegacyVms(config).length;
  const capabilityCount = blueprintCapabilities(config).length;

  return (
    <Modal
      isOpen={true}
      onClose={onClose}
      title="Export Blueprint"
      description={`Export ${blueprint.name} v${blueprint.version}`}
      size="lg"
      showCloseButton={!exporting}
      closeOnBackdrop={!exporting}
      closeOnEscape={!exporting}
    >
      <ModalBody className="space-y-4">
        <p className="text-sm text-gray-500">
          {blueprint.name} v{blueprint.version}
        </p>
        <p className="text-sm text-gray-600">
          Select what to include in the export package:
        </p>

        {/* Always included */}
        <div className="bg-gray-50 rounded-lg p-3">
          <p className="text-xs font-medium text-gray-500 uppercase mb-2">Always Included</p>
          <div className="space-y-1 text-sm text-gray-700">
            <div className="flex items-center">
              <span className="w-5 h-5 text-green-500 mr-2">✓</span>
              {blueprintIsEraB ? 'Network attachments' : 'Network configuration'}
              {networkCount > 0 && <span className="ml-1 text-gray-500">({networkCount})</span>}
            </div>
            <div className="flex items-center">
              <span className="w-5 h-5 text-green-500 mr-2">✓</span>
              {blueprintIsEraB ? 'Machine definitions' : 'VM definitions'}
              {machineCount > 0 && <span className="ml-1 text-gray-500">({machineCount})</span>}
            </div>
            {capabilityCount > 0 && (
              <div className="flex items-center">
                <span className="w-5 h-5 text-green-500 mr-2">✓</span>
                Capability packages and their scopes
                <span className="ml-1 text-gray-500">({capabilityCount})</span>
              </div>
            )}
          </div>
        </div>

        {/* Optional items */}
        <div className="space-y-3">
          {/* MSEL */}
          <label className="flex items-start p-3 border rounded-lg hover:bg-gray-50 cursor-pointer">
            <input
              type="checkbox"
              checked={options.include_msel}
              onChange={() => toggleOption('include_msel')}
              className="h-4 w-4 mt-0.5 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded"
            />
            <div className="ml-3 flex-1">
              <div className="flex items-center">
                <FileArchive className="h-4 w-4 mr-2 text-gray-400" />
                <span className="font-medium text-sm text-gray-900">MSEL / Scenario Injects</span>
              </div>
              <p className="text-xs text-gray-500 mt-0.5">
                Master Scenario Events List for exercise execution
              </p>
            </div>
          </label>

          {/* Dockerfiles — Era A only: there is no /data/images on a Kubernetes install. */}
          {!eraB && (
            <label className="flex items-start p-3 border rounded-lg hover:bg-gray-50 cursor-pointer">
              <input
                type="checkbox"
                checked={options.include_dockerfiles}
                onChange={() => toggleOption('include_dockerfiles')}
                className="h-4 w-4 mt-0.5 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded"
              />
              <div className="ml-3 flex-1">
                <div className="flex items-center">
                  <FileCode className="h-4 w-4 mr-2 text-gray-400" />
                  <span className="font-medium text-sm text-gray-900">Dockerfiles</span>
                </div>
                <p className="text-xs text-gray-500 mt-0.5">
                  Source files for building custom images
                </p>
              </div>
            </label>
          )}

          {/* Content Library */}
          <label className="flex items-start p-3 border rounded-lg hover:bg-gray-50 cursor-pointer">
            <input
              type="checkbox"
              checked={options.include_content}
              onChange={() => toggleOption('include_content')}
              className="h-4 w-4 mt-0.5 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded"
            />
            <div className="ml-3 flex-1">
              <div className="flex items-center">
                <BookOpen className="h-4 w-4 mr-2 text-gray-400" />
                <span className="font-medium text-sm text-gray-900">Content Library Items</span>
              </div>
              <p className="text-xs text-gray-500 mt-0.5">
                Student guides, instructor materials, and walkthroughs
              </p>
            </div>
          </label>

          {/* Artifacts — only when the blueprint declares some to collect. */}
          {canExportArtifacts && (
            <label className="flex items-start p-3 border rounded-lg hover:bg-gray-50 cursor-pointer">
              <input
                type="checkbox"
                checked={options.include_artifacts}
                onChange={() => toggleOption('include_artifacts')}
                className="h-4 w-4 mt-0.5 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded"
              />
              <div className="ml-3 flex-1">
                <div className="flex items-center">
                  <Package className="h-4 w-4 mr-2 text-gray-400" />
                  <span className="font-medium text-sm text-gray-900">Artifacts</span>
                </div>
                <p className="text-xs text-gray-500 mt-0.5">
                  Tools, scripts, and evidence templates ({artifactIds.length})
                </p>
              </div>
            </label>
          )}

          {/* Image tarballs — Era A only. See the note below for what carries images here. */}
          {!eraB && (
            <label className="flex items-start p-3 border rounded-lg hover:bg-gray-50 cursor-pointer">
              <input
                type="checkbox"
                checked={options.include_docker_images}
                onChange={() => toggleOption('include_docker_images')}
                className="h-4 w-4 mt-0.5 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded"
              />
              <div className="ml-3 flex-1">
                <div className="flex items-center">
                  <HardDrive className="h-4 w-4 mr-2 text-gray-400" />
                  <span className="font-medium text-sm text-gray-900">Image tarballs</span>
                </div>
                <p className="text-xs text-gray-500 mt-0.5">
                  Pre-built images, so the package deploys without reaching a registry
                </p>
                {options.include_docker_images && (
                  <div className="flex items-center mt-2 p-2 bg-amber-50 rounded text-xs text-amber-700">
                    <AlertCircle className="h-3.5 w-3.5 mr-1.5 flex-shrink-0" />
                    <span>This may result in a very large file (several GB)</span>
                  </div>
                )}
              </div>
            </label>
          )}
        </div>

        {/* Two reasons the image options are absent, and only one of them is the digest story.
            An Era A blueprint names images by tag however it got here, so telling its author that
            everything it names is digest-pinned would be describing a different blueprint. */}
        {blueprintIsEraB && (
          <div className="flex items-start p-3 bg-blue-50 rounded-lg text-xs text-blue-800">
            <ShieldCheck className="h-4 w-4 mr-2 mt-0.5 flex-shrink-0 text-blue-500" />
            <span>
              The package carries image references, not images. Every image this blueprint names is
              pinned by digest, so an air-gapped site mirrors those digests into a registry its
              cluster can reach and the same blueprint resolves to the same artefacts there.
            </span>
          </div>
        )}
        {!blueprintIsEraB && eraB && (
          <div className="flex items-start p-3 bg-blue-50 rounded-lg text-xs text-blue-800">
            <ShieldCheck className="h-4 w-4 mr-2 mt-0.5 flex-shrink-0 text-blue-500" />
            <span>
              This is a Docker-era blueprint and this install runs ranges on Kubernetes, so there
              are no Dockerfiles or image tarballs here to gather. The definition still exports in
              full, and an install that runs Docker ranges can collect the images.
            </span>
          </div>
        )}
      </ModalBody>

      <ModalFooter>
        <button
          type="button"
          onClick={onClose}
          disabled={exporting}
          className="px-4 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          onClick={handleExport}
          disabled={exporting}
          className="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50"
        >
          {exporting ? (
            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
          ) : (
            <Download className="h-4 w-4 mr-2" />
          )}
          Export Blueprint
        </button>
      </ModalFooter>
    </Modal>
  );
}
