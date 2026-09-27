// frontend/src/pages/RangeWizardPage.tsx
import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { isAxiosError } from 'axios';
import { WizardLayout } from '../components/wizard-v2/WizardLayout';
import {
  EnvironmentStep,
  ServicesStep,
  NetworkStep,
  UsersStep,
  VulnsStep,
  ReviewStep,
} from '../components/wizard-v2/steps';
import { useWizardStore } from '../stores/wizardStore';
import { rangesApi, networksApi, vmsApi, imagesApi, blueprintsApi } from '../services/api';
import { toast } from '../stores/toastStore';
import type { BaseImage } from '../types';

const STEPS = [
  EnvironmentStep,
  ServicesStep,
  NetworkStep,
  UsersStep,
  VulnsStep,
  ReviewStep,
];

/** A toast is not a log, and a range can fail validation once per machine. */
const MAX_LISTED_ERRORS = 3;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asSentence(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return '';
  return /[.!?]$/.test(trimmed) ? trimmed : `${trimmed}.`;
}

/**
 * The reason a refusal carries, in whichever shape the deploy route answered with.
 *
 * Kubernetes refuses with a sentence, but the Docker image validator refuses with an object
 * holding a message, the machines that are missing an image, and the hint that resolves them --
 * and that is the substrate this page is still offered on. Reading only the string shape left the
 * user with the HTTP status and nothing else, which is the failure this helper exists to end.
 * FastAPI's own schema refusals arrive as a list of `{loc, msg}` for the same reason.
 */
function refusalDetail(detail: unknown): string | null {
  if (typeof detail === 'string') return detail.trim() || null;

  if (Array.isArray(detail)) {
    const lines = detail
      .map((entry) => (isRecord(entry) && typeof entry.msg === 'string' ? entry.msg : null))
      .filter((line): line is string => line !== null);
    return lines.length > 0 ? lines.map(asSentence).join(' ') : null;
  }

  if (isRecord(detail)) {
    const listed = Array.isArray(detail.errors)
      ? detail.errors.filter((entry): entry is string => typeof entry === 'string')
      : [];
    const overflow = listed.length - MAX_LISTED_ERRORS;
    const parts = [
      typeof detail.message === 'string' ? detail.message : null,
      ...listed.slice(0, MAX_LISTED_ERRORS),
      overflow > 0 ? `And ${overflow} more` : null,
      typeof detail.hint === 'string' ? detail.hint : null,
    ].filter((part): part is string => part !== null && part.trim() !== '');
    return parts.length > 0 ? parts.map(asSentence).join(' ') : null;
  }

  return null;
}

/**
 * The server's own reason for refusing. The failure toast used to say only that the deploy had
 * failed and to look in the browser console -- the one place a user cannot be asked to look.
 *
 * An axios error is an Error, so its own message must never stand in for the server's: that
 * message is "Request failed with status code 400", which repeats the status and tells the user
 * nothing they can act on. Only a refusal raised here rather than by the server speaks for itself.
 */
export function deployErrorDetail(err: unknown, fallback: string): string {
  if (isAxiosError(err)) {
    const detail = refusalDetail(err.response?.data?.detail);
    if (detail) return detail;
    if (!err.response) return 'The API did not answer.';
    return `The server answered HTTP ${err.response.status} without a reason.`;
  }
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

/**
 * What the user is told when the deploy fails partway through.
 *
 * The wizard has to create the range before it can find out the deploy will be refused, so a
 * failure always has something to undo. When the undo works there is nothing left to say; when it
 * does not, the range is sitting in the list and the user is the only one who can remove it, so
 * the message has to name it rather than let it accumulate unexplained.
 */
export function deployFailureMessage(
  rangeName: string,
  reason: string,
  leftBehind: boolean
): string {
  const head = `Could not deploy "${rangeName}": ${asSentence(reason)}`;
  if (!leftBehind) return head;
  return `${head} The range was created and is still listed on the Ranges page; delete it there.`;
}

export default function RangeWizardPage() {
  const navigate = useNavigate();
  const [isDeploying, setIsDeploying] = useState(false);
  const [baseImages, setBaseImages] = useState<BaseImage[]>([]);

  const {
    currentStep,
    rangeName,
    saveAsBlueprint,
    networks,
    reset,
  } = useWizardStore();

  // Load base images for mapping to VM creation
  useEffect(() => {
    const loadBaseImages = async () => {
      try {
        const response = await imagesApi.listBaseImages();
        setBaseImages(response.data);
      } catch (error) {
        console.error('Failed to load base images:', error);
      }
    };
    loadBaseImages();
  }, []);

  const handleDeploy = async () => {
    setIsDeploying(true);

    // Held outside the try because the failure path needs it: every step after step 1 fails with
    // a range already created, and each failed attempt used to leave that range and its networks
    // in the list for the user to find and delete one at a time.
    let createdRangeId: string | null = null;

    try {
      // Step 1: Create the range
      const rangeResponse = await rangesApi.create({
        name: rangeName,
        description: `Created via Range Wizard - ${networks.segments.length} networks, ${networks.vms.length} VMs`,
      });
      const rangeId = rangeResponse.data.id;
      createdRangeId = rangeId;

      // Step 2: Create networks
      const networkIdMap: Record<string, string> = {};
      for (const segment of networks.segments) {
        const networkResponse = await networksApi.create({
          range_id: rangeId,
          name: segment.name,
          subnet: segment.subnet,
          gateway: segment.gateway,
          is_isolated: segment.isolated,
          dhcp_enabled: segment.dhcp,
        });
        networkIdMap[segment.id] = networkResponse.data.id;
      }

      // Step 3: Create VMs
      let skippedNoImage = 0;
      let skippedNoNetwork = 0;
      for (const vm of networks.vms) {
        // Find base image by ID or name
        let baseImage = baseImages.find((img) => img.id === vm.baseImageId);
        if (!baseImage) {
          baseImage = baseImages.find((img) => img.name === vm.templateName);
        }
        // Fallback: match by docker_image_tag for container images
        if (!baseImage && vm.templateName) {
          baseImage = baseImages.find((img) => img.docker_image_tag === vm.templateName);
        }
        if (!baseImage) {
          console.warn(`Base image not found for VM ${vm.hostname}: ${vm.templateName || vm.baseImageId}`);
          toast.error(`Base image not found: ${vm.templateName || 'Unknown'}. Please ensure the image is cached.`);
          skippedNoImage++;
          continue;
        }

        const networkId = networkIdMap[vm.networkId];
        if (!networkId) {
          console.warn(`Network not found for VM ${vm.hostname}: ${vm.networkId}`);
          skippedNoNetwork++;
          continue;
        }

        // Detect OS type for field mapping
        const isWindows = baseImage.os_type === 'windows';

        await vmsApi.create({
          range_id: rangeId,
          network_id: networkId,
          base_image_id: baseImage.id,
          hostname: vm.hostname,
          ip_address: vm.ip,
          cpu: vm.cpu || baseImage.default_cpu,
          ram_mb: vm.ramMb || baseImage.default_ram_mb,
          disk_gb: vm.diskGb || baseImage.default_disk_gb,
          position_x: vm.position.x,
          position_y: vm.position.y,
          // Credentials - mapped to OS-specific fields
          ...(isWindows ? {
            windows_username: vm.username,
            windows_password: vm.password,
          } : {
            linux_username: vm.username,
            linux_password: vm.password,
            linux_user_sudo: vm.sudoEnabled,
          }),
          // Network settings
          use_dhcp: vm.useDhcp,
          gateway: vm.gateway,
          dns_servers: vm.dnsServers,
          // Storage
          disk2_gb: vm.disk2Gb || null,
          disk3_gb: vm.disk3Gb || null,
          // Shared folders
          enable_shared_folder: vm.enableSharedFolder,
          enable_global_shared: vm.enableGlobalShared,
          // Display and locale
          display_type: vm.displayType,
          language: vm.language || null,
          keyboard: vm.keyboard || null,
          region: vm.region || null,
        });
      }

      // Six steps of configuration that produced nothing is not a range worth deploying, and
      // deploying it anyway is how the wizard came to hand back an empty one after a row of red
      // toasts. Refuse here so the rollback below removes what was created. A machine skipped for
      // want of a network says so nowhere else, so the reason has to distinguish the two.
      const skippedVms = skippedNoImage + skippedNoNetwork;
      if (networks.vms.length > 0 && skippedVms === networks.vms.length) {
        throw new Error(
          skippedNoNetwork === 0
            ? 'None of the configured machines could be matched to a base image on this install.'
            : 'None of the configured machines could be created: they reference a base image or a network this install does not have.'
        );
      }

      // Step 4: Optionally save as blueprint
      if (saveAsBlueprint) {
        try {
          await blueprintsApi.create({
            range_id: rangeId,
            name: `${rangeName} Blueprint`,
            description: `Blueprint created from Range Wizard`,
          });
          toast.success('Blueprint created successfully');
        } catch (blueprintError) {
          console.error('Failed to create blueprint:', blueprintError);
          toast.error('Range created but blueprint creation failed');
        }
      }

      // Step 5: Deploy the range
      await rangesApi.deploy(rangeId);

      toast.success(`Range "${rangeName}" created and deployment started!`);
      reset();
      navigate(`/ranges/${rangeId}`);
    } catch (error) {
      console.error('Deployment failed:', error);
      const reason = deployErrorDetail(error, 'The server gave no reason.');

      let leftBehind = false;
      if (createdRangeId) {
        try {
          await rangesApi.delete(createdRangeId);
        } catch (cleanupError) {
          // Now the user owns the problem, so the message below has to say so.
          console.error('Failed to remove the range left by a failed deploy:', cleanupError);
          leftBehind = true;
        }
      }

      toast.error(deployFailureMessage(rangeName, reason, leftBehind));
    } finally {
      setIsDeploying(false);
    }
  };

  const CurrentStepComponent = STEPS[currentStep];

  return (
    <WizardLayout onDeploy={handleDeploy} isDeploying={isDeploying}>
      <CurrentStepComponent />
    </WizardLayout>
  );
}
