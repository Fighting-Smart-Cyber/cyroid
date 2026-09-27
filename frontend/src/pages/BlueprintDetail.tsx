// frontend/src/pages/BlueprintDetail.tsx
import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import {
  blueprintsApi,
  instancesApi,
  contentApi,
  BlueprintDetail as BlueprintDetailType,
  Instance,
  InstanceDeploy,
  ContentListItem,
} from '../services/api';
import {
  LayoutTemplate,
  Loader2,
  ArrowLeft,
  AlertTriangle,
  Rocket,
  Network,
  Server,
  Copy,
  Trash2,
  ExternalLink,
  Download,
  BookOpen,
  Boxes,
  Pencil,
  GitPullRequest,
} from 'lucide-react';
import clsx from 'clsx';
import { toast } from '../stores/toastStore';
import { ConfirmDialog } from '../components/common/ConfirmDialog';
import {
  ContributeToCatalogModal,
  DeployInstanceModal,
  EditBlueprintModal,
  ExportBlueprintModal,
  VisualBlueprintEditor,
} from '../components/blueprints';
import { BlueprintCatalogDiff, catalogContributionApi } from '../services/catalogContribution';
import {
  apiErrorDetail,
  blueprintCapabilities,
  blueprintLegacyVms,
  blueprintNetworks,
  blueprintSchemaVersion,
  blueprintWorkloads,
  isKubernetesBlueprint,
  usesVisualEditor,
} from '../lib/blueprints';

const statusColors: Record<string, string> = {
  draft: 'bg-gray-100 text-gray-800',
  deploying: 'bg-yellow-100 text-yellow-800',
  running: 'bg-green-100 text-green-800',
  stopped: 'bg-gray-100 text-gray-800',
  error: 'bg-red-100 text-red-800',
};

export default function BlueprintDetail() {
  const { id } = useParams<{ id: string }>();
  const [blueprint, setBlueprint] = useState<BlueprintDetailType | null>(null);
  const [instances, setInstances] = useState<Instance[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<'overview' | 'instances'>('overview');
  const [showDeployModal, setShowDeployModal] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState<{
    instance: Instance | null;
    isLoading: boolean;
  }>({ instance: null, isLoading: false });
  const [showExportModal, setShowExportModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showContributeModal, setShowContributeModal] = useState(false);
  const [catalogDiff, setCatalogDiff] = useState<BlueprintCatalogDiff | null>(null);
  const [contentItems, setContentItems] = useState<ContentListItem[]>([]);
  const [linkedContentIds, setLinkedContentIds] = useState<string[]>([]);
  const [savingContent, setSavingContent] = useState(false);

  // A failed load has to leave a page behind. This cleared `loading` without ever setting
  // `blueprint`, so the render below fell through to the spinner and stayed there: one toast,
  // then an apparent hang, with nothing saying what went wrong or how to get back.
  //
  // Only a first load can fail that way. Every later call runs behind a deploy, a clone, a
  // delete or a save that has already happened, so its failure is a stale page rather than no
  // page: tearing the blueprint down and drawing the load error instead would blame the action
  // the user just watched succeed.
  const loaded = useRef(false);

  const fetchData = useCallback(async () => {
    if (!id) return;
    if (!loaded.current) setLoading(true);
    setLoadError(null);
    try {
      const [bpRes, instRes, contentRes] = await Promise.all([
        blueprintsApi.get(id),
        blueprintsApi.listInstances(id),
        contentApi.list({ published_only: true }),
      ]);
      setBlueprint(bpRes.data);
      setInstances(instRes.data);
      setContentItems(contentRes.data);
      setLinkedContentIds(bpRes.data.content_ids || []);
      loaded.current = true;
    } catch (err: unknown) {
      const message = apiErrorDetail(err, 'The server did not say why.');
      if (loaded.current) {
        toast.error(message);
      } else {
        setBlueprint(null);
        setLoadError(message);
      }
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    // Another blueprint is a first load again: its failure has to draw the error panel rather
    // than leave the previous blueprint on screen under this one's id.
    loaded.current = false;
    setBlueprint(null);
    fetchData();
  }, [fetchData]);

  // Whether this blueprint has a catalog to contribute back to, and how far it
  // has drifted from it. A 404 just means it was authored locally.
  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    catalogContributionApi
      .diff(id)
      .then((result) => {
        if (!cancelled) setCatalogDiff(result);
      })
      .catch(() => {
        if (!cancelled) setCatalogDiff(null);
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  const handleDeploy = async (data: InstanceDeploy) => {
    if (!id) return;
    try {
      await blueprintsApi.deploy(id, data);
      toast.success('Instance deployed');
      setShowDeployModal(false);
      fetchData();
    } catch (err: unknown) {
      toast.error(apiErrorDetail(err, 'Failed to deploy'));
    }
  };

  const handleClone = async (instance: Instance) => {
    try {
      await instancesApi.clone(instance.id);
      toast.success('Instance cloned');
      fetchData();
    } catch (err: unknown) {
      toast.error(apiErrorDetail(err, 'Failed to clone'));
    }
  };

  const handleDelete = async () => {
    if (!deleteConfirm.instance) return;
    setDeleteConfirm((prev) => ({ ...prev, isLoading: true }));
    try {
      await instancesApi.delete(deleteConfirm.instance.id);
      toast.success('Instance deleted');
      setDeleteConfirm({ instance: null, isLoading: false });
      fetchData();
    } catch (err: unknown) {
      setDeleteConfirm({ instance: null, isLoading: false });
      toast.error(apiErrorDetail(err, 'Failed to delete'));
    }
  };

  const handleToggleContent = async (contentId: string) => {
    if (!id) return;
    const newIds = linkedContentIds.includes(contentId)
      ? linkedContentIds.filter((cid) => cid !== contentId)
      : [...linkedContentIds, contentId];

    setLinkedContentIds(newIds);
    setSavingContent(true);
    try {
      await blueprintsApi.update(id, { content_ids: newIds });
      toast.success('Linked content updated');
    } catch (err: unknown) {
      // Revert on error
      setLinkedContentIds(linkedContentIds);
      toast.error(apiErrorDetail(err, 'Failed to update linked content'));
    } finally {
      setSavingContent(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-8 w-8 animate-spin text-primary-600" />
      </div>
    );
  }

  if (!blueprint) {
    return (
      <div>
        <Link
          to="/blueprints"
          className="inline-flex items-center text-sm text-gray-500 hover:text-gray-700 mb-4"
        >
          <ArrowLeft className="h-4 w-4 mr-1" />
          Back to Blueprints
        </Link>
        <div className="bg-white shadow rounded-lg p-8 text-center">
          <AlertTriangle className="mx-auto h-10 w-10 text-amber-500" />
          <h3 className="mt-3 text-sm font-medium text-gray-900">
            This blueprint could not be loaded
          </h3>
          <p className="mt-2 text-sm text-gray-600 whitespace-pre-wrap">
            {loadError ?? 'The server returned no blueprint.'}
          </p>
          <button
            onClick={fetchData}
            className="mt-4 inline-flex items-center px-4 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50"
          >
            Try again
          </button>
        </div>
      </div>
    );
  }

  const schemaVersion = blueprintSchemaVersion(blueprint.config);
  const isKubernetes = isKubernetesBlueprint(blueprint.config);
  const networks = blueprintNetworks(blueprint.config);
  const workloads = blueprintWorkloads(blueprint.config);
  const legacyVms = blueprintLegacyVms(blueprint.config);
  const capabilities = blueprintCapabilities(blueprint.config);

  return (
    <div>
      {/* Header */}
      <div className="mb-6">
        <Link
          to="/blueprints"
          className="inline-flex items-center text-sm text-gray-500 hover:text-gray-700 mb-4"
        >
          <ArrowLeft className="h-4 w-4 mr-1" />
          Back to Blueprints
        </Link>
        <div className="flex items-center justify-between">
          <div className="flex items-center">
            <div className="bg-indigo-100 rounded-md p-3">
              <LayoutTemplate className="h-8 w-8 text-indigo-600" />
            </div>
            <div className="ml-4">
              <div className="flex items-center gap-2">
                <h1 className="text-2xl font-bold text-gray-900">{blueprint.name}</h1>
                <span
                  title={
                    isKubernetes
                      ? 'Declares workloads and capabilities — deployable on Kubernetes'
                      : 'Declares networks and VMs — deployable on Docker'
                  }
                  className="px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-700"
                >
                  schema v{schemaVersion}
                </span>
              </div>
              <p className="text-sm text-gray-500">
                Version {blueprint.version} · Created by {blueprint.created_by_username}
              </p>
            </div>
          </div>
          <div className="flex space-x-3">
            <button
              onClick={() => setShowEditModal(true)}
              className="inline-flex items-center px-4 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50"
            >
              <Pencil className="h-4 w-4 mr-2" />
              Edit
            </button>
            <button
              onClick={() => setShowExportModal(true)}
              className="inline-flex items-center px-4 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50"
            >
              <Download className="h-4 w-4 mr-2" />
              Export
            </button>
            {catalogDiff && (
              <button
                onClick={() => setShowContributeModal(true)}
                title={`Contribute your changes back to ${catalogDiff.origin.source_name}`}
                className="inline-flex items-center px-4 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50"
              >
                <GitPullRequest className="h-4 w-4 mr-2" />
                Contribute
                {catalogDiff.has_changes && (
                  <span className="ml-2 px-1.5 py-0.5 rounded-full text-xs bg-amber-100 text-amber-800">
                    {catalogDiff.changes.length}
                  </span>
                )}
              </button>
            )}
            <button
              onClick={() => setShowDeployModal(true)}
              className="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700"
            >
              <Rocket className="h-4 w-4 mr-2" />
              Deploy Instance
            </button>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-gray-200 mb-6">
        <nav className="-mb-px flex space-x-8">
          <button
            onClick={() => setActiveTab('overview')}
            className={clsx(
              'py-4 px-1 border-b-2 font-medium text-sm',
              activeTab === 'overview'
                ? 'border-indigo-500 text-indigo-600'
                : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
            )}
          >
            Overview
          </button>
          <button
            onClick={() => setActiveTab('instances')}
            className={clsx(
              'py-4 px-1 border-b-2 font-medium text-sm',
              activeTab === 'instances'
                ? 'border-indigo-500 text-indigo-600'
                : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
            )}
          >
            Instances ({instances.length})
          </button>
        </nav>
      </div>

      {/* Tab Content */}
      {activeTab === 'overview' ? (
        <div className="bg-white shadow rounded-lg p-6">
          <h3 className="text-lg font-medium text-gray-900 mb-4">Configuration</h3>

          {/* Both eras declare networks. Everything below them differs: a v2 blueprint has
              workloads and capability packages and no `vms` key at all, so reading v1 fields
              here showed an empty VMs table for the machine the blueprint plainly declares. */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div>
              <h4 className="text-sm font-medium text-gray-700 mb-2 flex items-center">
                <Network className="h-4 w-4 mr-2" />
                Networks ({networks.length})
              </h4>
              {networks.length === 0 ? (
                <p className="text-sm text-gray-500">None declared</p>
              ) : (
                <ul className="space-y-2">
                  {networks.map((net, i) => (
                    <li key={i} className="bg-gray-50 rounded p-2 text-sm">
                      <span className="font-medium">{net.name}</span>
                      <span className="text-gray-500 ml-2">{net.subnet}</span>
                      {net.gateway && (
                        <span className="text-gray-400 ml-2">gw {net.gateway}</span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {isKubernetes ? (
              <div>
                <h4 className="text-sm font-medium text-gray-700 mb-2 flex items-center">
                  <Server className="h-4 w-4 mr-2" />
                  Machines ({workloads.length})
                </h4>
                {workloads.length === 0 ? (
                  <p className="text-sm text-gray-500">None declared</p>
                ) : (
                  <ul className="space-y-2">
                    {workloads.map((workload, i) => (
                      <li key={i} className="bg-gray-50 rounded p-2 text-sm">
                        <div>
                          <span className="font-medium">{workload.name}</span>
                          <span className="text-gray-500 ml-2">
                            {[workload.osFamily, workload.osVersion].filter(Boolean).join(' ')}
                          </span>
                        </div>
                        <div className="text-xs text-gray-500 mt-1 space-x-3">
                          {workload.cpus !== null && <span>{workload.cpus} vCPU</span>}
                          {workload.memoryMb !== null && <span>{workload.memoryMb} MB</span>}
                          {workload.disks.map((disk) => (
                            <span key={disk.name}>
                              {disk.name} {disk.sizeGb ?? '?'} GB{disk.boot ? ' (boot)' : ''}
                            </span>
                          ))}
                        </div>
                        {workload.interfaces.length > 0 && (
                          <div className="text-xs text-gray-500 mt-1 space-x-3 font-mono">
                            {workload.interfaces.map((iface, j) => (
                              <span key={j}>
                                {iface.network} {iface.ip ?? 'dhcp'}
                                {iface.primary ? '*' : ''}
                              </span>
                            ))}
                          </div>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ) : (
              <div>
                <h4 className="text-sm font-medium text-gray-700 mb-2 flex items-center">
                  <Server className="h-4 w-4 mr-2" />
                  VMs ({legacyVms.length})
                </h4>
                {legacyVms.length === 0 ? (
                  <p className="text-sm text-gray-500">None declared</p>
                ) : (
                  <ul className="space-y-2">
                    {legacyVms.map((vm, i) => (
                      <li key={i} className="bg-gray-50 rounded p-2 text-sm">
                        <span className="font-medium">{vm.hostname}</span>
                        <span className="text-gray-500 ml-2">{vm.ipAddress}</span>
                        {vm.image && <span className="text-gray-400 ml-2">({vm.image})</span>}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>

          {capabilities.length > 0 && (
            <div className="mt-6 pt-6 border-t">
              <h4 className="text-sm font-medium text-gray-700 mb-2 flex items-center">
                <Boxes className="h-4 w-4 mr-2" />
                Capabilities ({capabilities.length})
              </h4>
              <ul className="space-y-2">
                {capabilities.map((capability, i) => (
                  <li key={i} className="bg-gray-50 rounded p-2 text-sm">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{capability.name}</span>
                      {capability.version && (
                        <span className="text-gray-500">{capability.version}</span>
                      )}
                      {/* Scope is required of every capability with no default, and reset() and
                          verify() both key off it, so an undeclared one is worth naming here. */}
                      <span
                        className={clsx(
                          'px-1.5 py-0.5 rounded text-xs',
                          capability.scope
                            ? 'bg-indigo-100 text-indigo-700'
                            : 'bg-red-100 text-red-700'
                        )}
                      >
                        {capability.scope ?? 'no scope declared'}
                      </span>
                    </div>
                    <div className="text-xs text-gray-500 mt-1">
                      chart {capability.chartName ?? '—'} {capability.chartVersion ?? ''}
                      {capability.repository && ` · ${capability.repository}`}
                      {capability.hooks.length > 0 && ` · hooks: ${capability.hooks.join(', ')}`}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Linked Content */}
          <div className="mt-6 pt-6 border-t">
            <h4 className="text-sm font-medium text-gray-700 mb-2 flex items-center">
              <BookOpen className="h-4 w-4 mr-2" />
              Linked Content
              {savingContent && <Loader2 className="h-3 w-3 ml-2 animate-spin" />}
            </h4>
            <p className="text-xs text-gray-500 mb-3">
              When this blueprint is selected for a training event, these content items will be auto-selected.
            </p>
            <div className="border border-gray-200 rounded-md max-h-48 overflow-y-auto">
              {contentItems.length === 0 ? (
                <p className="p-3 text-sm text-gray-500">No published content available</p>
              ) : (
                contentItems.map((item) => (
                  <label
                    key={item.id}
                    className="flex items-center px-3 py-2 hover:bg-gray-50 cursor-pointer"
                  >
                    <input
                      type="checkbox"
                      checked={linkedContentIds.includes(item.id)}
                      onChange={() => handleToggleContent(item.id)}
                      disabled={savingContent}
                      className="h-4 w-4 text-primary-600 focus:ring-primary-500 border-gray-300 rounded"
                    />
                    <span className="ml-3 text-sm text-gray-700">{item.title}</span>
                    <span className="ml-2 text-xs text-gray-500 capitalize">
                      ({item.content_type.replace('_', ' ')})
                    </span>
                  </label>
                ))
              )}
            </div>
          </div>
        </div>
      ) : (
        <div className="bg-white shadow rounded-lg overflow-hidden">
          {instances.length === 0 ? (
            <div className="p-8 text-center">
              <Rocket className="mx-auto h-12 w-12 text-gray-400" />
              <h3 className="mt-2 text-sm font-medium text-gray-900">No instances</h3>
              <p className="mt-1 text-sm text-gray-500">
                Deploy your first instance from this blueprint.
              </p>
              <button
                onClick={() => setShowDeployModal(true)}
                className="mt-4 inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700"
              >
                <Rocket className="h-4 w-4 mr-2" />
                Deploy Instance
              </button>
            </div>
          ) : (
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                    Instance
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                    #
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                    Status
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                    Instructor
                  </th>
                  <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {instances.map((instance) => (
                  <tr key={instance.id}>
                    <td className="px-6 py-4 whitespace-nowrap">
                      <div className="text-sm font-medium text-gray-900">
                        {instance.name}
                      </div>
                      <div className="text-xs text-gray-500">
                        v{instance.blueprint_version}
                        {instance.blueprint_version < blueprint.version && (
                          <span className="ml-1 text-amber-500">(outdated)</span>
                        )}
                      </div>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                      #{instance.subnet_offset + 1}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap">
                      <span
                        className={clsx(
                          'px-2 py-1 text-xs font-medium rounded-full',
                          statusColors[instance.range_status || 'draft']
                        )}
                      >
                        {instance.range_status || 'unknown'}
                      </span>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                      {instance.instructor_username}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-right text-sm font-medium space-x-2">
                      <Link
                        to={`/ranges/${instance.range_id}`}
                        className="text-indigo-600 hover:text-indigo-900"
                        title="Open Range"
                      >
                        <ExternalLink className="h-4 w-4 inline" />
                      </Link>
                      <button
                        onClick={() => handleClone(instance)}
                        className="text-gray-400 hover:text-gray-600"
                        title="Clone"
                      >
                        <Copy className="h-4 w-4 inline" />
                      </button>
                      <button
                        onClick={() =>
                          setDeleteConfirm({ instance, isLoading: false })
                        }
                        className="text-gray-400 hover:text-red-600"
                        title="Delete"
                      >
                        <Trash2 className="h-4 w-4 inline" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Deploy Modal */}
      {showDeployModal && (
        <DeployInstanceModal
          blueprint={blueprint}
          onClose={() => setShowDeployModal(false)}
          onDeploy={handleDeploy}
        />
      )}

      {/* Delete Confirmation */}
      <ConfirmDialog
        isOpen={deleteConfirm.instance !== null}
        title="Delete Instance"
        message={`Are you sure you want to delete "${deleteConfirm.instance?.name}"? This will delete the range and all its VMs. This cannot be undone.`}
        confirmLabel="Delete"
        variant="danger"
        onConfirm={handleDelete}
        onCancel={() => setDeleteConfirm({ instance: null, isLoading: false })}
        isLoading={deleteConfirm.isLoading}
      />

      {/* Contribute to Catalog Modal */}
      {showContributeModal && blueprint && (
        <ContributeToCatalogModal
          blueprintId={blueprint.id}
          blueprintName={blueprint.name}
          onClose={() => setShowContributeModal(false)}
        />
      )}

      {/* Export Blueprint Modal */}
      {showExportModal && (
        <ExportBlueprintModal
          blueprint={blueprint}
          onClose={() => setShowExportModal(false)}
        />
      )}

      {/* The visual editor draws networks and VMs and writes them back, so it cannot open a v2
          blueprint — it would read fields that are not there and save away the workloads and
          capabilities it never saw. The JSON editor is the authoring path for those until a
          structured one exists, and until then it is the only one there is. */}
      {showEditModal &&
        (usesVisualEditor(blueprint.config) ? (
          <VisualBlueprintEditor
            blueprint={blueprint}
            isOpen={true}
            onClose={() => setShowEditModal(false)}
            onSaved={fetchData}
          />
        ) : (
          <EditBlueprintModal
            blueprint={blueprint}
            isOpen={true}
            onClose={() => setShowEditModal(false)}
            onSaved={fetchData}
          />
        ))}
    </div>
  );
}
