// frontend/src/pages/Blueprints.tsx
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { blueprintsApi, Blueprint, BlueprintDetail, InstanceDeploy } from '../services/api';
import {
  LayoutTemplate,
  Loader2,
  Rocket,
  Trash2,
  Network,
  Server,
  Users,
  Upload,
  Pencil,
} from 'lucide-react';
import { ConfirmDialog } from '../components/common/ConfirmDialog';
import { toast } from '../stores/toastStore';
import DeployInstanceModal from '../components/blueprints/DeployInstanceModal';
import {
  EditBlueprintModal,
  ImportBlueprintModal,
  VisualBlueprintEditor,
} from '../components/blueprints';
import { useSubstrate } from '../stores/capabilitiesStore';
import { apiErrorDetail, machineNoun, pluralise, usesVisualEditor } from '../lib/blueprints';

/**
 * The list endpoint answers counts, not config, so a card cannot read `schemaVersion` itself. It
 * carries `schema_version` when the backend reports one; without it the substrate this install
 * runs on is the best available answer for what to call a machine.
 */
function listedSchemaVersion(blueprint: Blueprint): number | null {
  const declared = (blueprint as Blueprint & { schema_version?: unknown }).schema_version;
  return typeof declared === 'number' ? declared : null;
}

export default function Blueprints() {
  const { substrate, isKubernetes } = useSubstrate();
  const [blueprints, setBlueprints] = useState<Blueprint[]>([]);
  const [loading, setLoading] = useState(true);
  const [deleteConfirm, setDeleteConfirm] = useState<{
    blueprint: Blueprint | null;
    isLoading: boolean;
  }>({ blueprint: null, isLoading: false });
  const [deployModal, setDeployModal] = useState<Blueprint | null>(null);
  const [showImportModal, setShowImportModal] = useState(false);
  const [editModal, setEditModal] = useState<BlueprintDetail | null>(null);
  const [loadingEdit, setLoadingEdit] = useState<string | null>(null);

  const fetchBlueprints = async () => {
    try {
      const response = await blueprintsApi.list();
      setBlueprints(response.data);
    } catch (err) {
      console.error('Failed to fetch blueprints:', err);
      toast.error('Failed to load blueprints');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchBlueprints();
  }, []);

  const handleDelete = (blueprint: Blueprint) => {
    setDeleteConfirm({ blueprint, isLoading: false });
  };

  const confirmDelete = async () => {
    if (!deleteConfirm.blueprint) return;
    setDeleteConfirm((prev) => ({ ...prev, isLoading: true }));
    try {
      await blueprintsApi.delete(deleteConfirm.blueprint.id);
      setDeleteConfirm({ blueprint: null, isLoading: false });
      fetchBlueprints();
      toast.success('Blueprint deleted');
    } catch (err: unknown) {
      setDeleteConfirm({ blueprint: null, isLoading: false });
      toast.error(apiErrorDetail(err, 'Failed to delete blueprint'));
    }
  };

  const handleDeploy = async (data: InstanceDeploy) => {
    if (!deployModal) return;
    try {
      const response = await blueprintsApi.deploy(deployModal.id, data);
      toast.success(`Instance "${response.data.name}" created`);
      setDeployModal(null);
      fetchBlueprints();
    } catch (err: unknown) {
      toast.error(apiErrorDetail(err, 'Failed to deploy instance'));
    }
  };

  const handleEdit = async (blueprint: Blueprint) => {
    setLoadingEdit(blueprint.id);
    try {
      const response = await blueprintsApi.get(blueprint.id);
      setEditModal(response.data);
    } catch (err: unknown) {
      toast.error(apiErrorDetail(err, 'Failed to load blueprint'));
    } finally {
      setLoadingEdit(null);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-8 w-8 animate-spin text-primary-600" />
      </div>
    );
  }

  return (
    <div>
      <div className="sm:flex sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Range Blueprints</h1>
          <p className="mt-2 text-sm text-gray-700">
            Reusable range configurations for deploying multiple isolated instances
          </p>
        </div>
        <div className="mt-4 sm:mt-0">
          <button
            onClick={() => setShowImportModal(true)}
            className="inline-flex items-center px-4 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50"
          >
            <Upload className="h-4 w-4 mr-2" />
            Import Blueprint
          </button>
        </div>
      </div>

      {blueprints.length === 0 ? (
        <div className="mt-8 text-center">
          <LayoutTemplate className="mx-auto h-12 w-12 text-gray-400" />
          <h3 className="mt-2 text-sm font-medium text-gray-900">No blueprints</h3>
          {/* Withheld until the substrate is known rather than defaulted: saving a range as a
              blueprint reads network and VM rows, so on Kubernetes the server refuses it and
              following this instruction is a dead end. The Kubernetes wording names only routes
              that exist today -- there is no editor for a new one yet, and pointing at a button
              that is not on this page would be the same dead end wearing different words. */}
          {substrate !== null && (
            <p className="mt-1 text-sm text-gray-500">
              {isKubernetes
                ? 'A blueprint for this substrate declares workloads and capabilities rather than networks and VMs, so it cannot be saved from a range. Import one, or post its config to the API; from then on it is edited from its own page.'
                : 'Create a range first, then save it as a blueprint from the range detail page.'}
            </p>
          )}
        </div>
      ) : (
        <div className="mt-8 grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-3">
          {blueprints.map((blueprint) => (
            <div
              key={blueprint.id}
              className="bg-white rounded-lg shadow overflow-hidden hover:shadow-md transition-shadow"
            >
              <div className="p-5">
                <div className="flex items-start justify-between">
                  <div className="flex items-center">
                    <div className="flex-shrink-0 bg-indigo-100 rounded-md p-2">
                      <LayoutTemplate className="h-6 w-6 text-indigo-600" />
                    </div>
                    <div className="ml-3">
                      <Link
                        to={`/blueprints/${blueprint.id}`}
                        className="text-sm font-medium text-gray-900 hover:text-indigo-600"
                      >
                        {blueprint.name}
                      </Link>
                      <p className="text-xs text-gray-500">v{blueprint.version}</p>
                    </div>
                  </div>
                </div>

                {blueprint.description && (
                  <p className="mt-3 text-sm text-gray-500 line-clamp-2">
                    {blueprint.description}
                  </p>
                )}

                <div className="mt-4 flex items-center text-xs text-gray-500 space-x-4">
                  <span className="flex items-center">
                    <Network className="h-3.5 w-3.5 mr-1" />
                    {pluralise(blueprint.network_count, 'network')}
                  </span>
                  <span className="flex items-center">
                    <Server className="h-3.5 w-3.5 mr-1" />
                    {blueprint.vm_count}{' '}
                    {machineNoun(listedSchemaVersion(blueprint), isKubernetes, blueprint.vm_count)}
                  </span>
                  <span className="flex items-center">
                    <Users className="h-3.5 w-3.5 mr-1" />
                    {pluralise(blueprint.instance_count, 'instance')}
                  </span>
                </div>
              </div>

              <div className="bg-gray-50 px-5 py-3 flex justify-between items-center">
                <span className="text-xs text-gray-500">
                  {blueprint.is_seed && (
                    <span className="bg-blue-100 text-blue-700 px-1.5 py-0.5 rounded text-xs mr-2">
                      Built-in
                    </span>
                  )}
                  {blueprint.instance_count} instance{blueprint.instance_count !== 1 ? 's' : ''}
                </span>
                <div className="flex space-x-2">
                  <Link
                    to={`/blueprints/${blueprint.id}`}
                    className="inline-flex items-center px-3 py-1.5 text-xs font-medium rounded-md text-gray-700 bg-white border border-gray-300 hover:bg-gray-50"
                  >
                    View
                  </Link>
                  <button
                    onClick={() => handleEdit(blueprint)}
                    disabled={loadingEdit === blueprint.id}
                    className="inline-flex items-center px-3 py-1.5 text-xs font-medium rounded-md text-gray-700 bg-white border border-gray-300 hover:bg-gray-50 disabled:opacity-50"
                    title="Edit Blueprint"
                  >
                    {loadingEdit === blueprint.id ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Pencil className="h-3.5 w-3.5" />
                    )}
                  </button>
                  <button
                    onClick={() => setDeployModal(blueprint)}
                    className="inline-flex items-center px-3 py-1.5 text-xs font-medium rounded-md text-white bg-indigo-600 hover:bg-indigo-700"
                  >
                    <Rocket className="h-3.5 w-3.5 mr-1" />
                    Deploy
                  </button>
                  <button
                    onClick={() => handleDelete(blueprint)}
                    className="p-1.5 text-gray-400 hover:text-red-600"
                    title="Delete"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Delete Confirmation */}
      <ConfirmDialog
        isOpen={deleteConfirm.blueprint !== null}
        title="Delete Blueprint"
        message={`Are you sure you want to delete "${deleteConfirm.blueprint?.name}"? This cannot be undone.`}
        confirmLabel="Delete"
        variant="danger"
        onConfirm={confirmDelete}
        onCancel={() => setDeleteConfirm({ blueprint: null, isLoading: false })}
        isLoading={deleteConfirm.isLoading}
      />

      {/* Deploy Instance Modal */}
      {deployModal && (
        <DeployInstanceModal
          blueprint={deployModal}
          onClose={() => setDeployModal(null)}
          onDeploy={handleDeploy}
        />
      )}

      {/* Import Blueprint Modal */}
      {showImportModal && (
        <ImportBlueprintModal
          onClose={() => setShowImportModal(false)}
          onSuccess={() => {
            fetchBlueprints();
            toast.success('Blueprint imported successfully');
          }}
        />
      )}

      {/* The visual editor speaks networks and VMs, which a v2 blueprint does not have; opening
          one in it would save back a config stripped of the workloads and capabilities it never
          read. Those are edited as JSON until a structured editor exists for them. */}
      {editModal &&
        (usesVisualEditor(editModal.config) ? (
          <VisualBlueprintEditor
            blueprint={editModal}
            isOpen={true}
            onClose={() => setEditModal(null)}
            onSaved={fetchBlueprints}
          />
        ) : (
          <EditBlueprintModal
            blueprint={editModal}
            isOpen={true}
            onClose={() => setEditModal(null)}
            onSaved={fetchBlueprints}
          />
        ))}
    </div>
  );
}
