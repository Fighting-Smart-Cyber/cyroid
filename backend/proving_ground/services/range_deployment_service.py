# backend/proving_ground/services/range_deployment_service.py
"""
Range deployment service with DinD isolation.

This service handles the lifecycle of range deployments using DinD
(Docker-in-Docker) for complete network isolation. Each range runs in its
own DinD container, allowing multiple ranges to use identical IP spaces
without conflicts.
"""

import logging
import os
from datetime import datetime
from typing import Optional, Dict, Any, List, Callable
from uuid import UUID

from sqlalchemy.orm import Session

from proving_ground.config import get_settings
from proving_ground.utils.arch import requires_emulation
from proving_ground.services.registry_service import RegistryService
from proving_ground.services.image_resolution import resolve_vm_image
from proving_ground.models import Range, Network, VM, RangeStatus
from proving_ground.models.vm import VMStatus
from proving_ground.models.vm_enums import VMType
from proving_ground.models.base_image import BaseImage
from proving_ground.models.golden_image import GoldenImage
from proving_ground.models.snapshot import Snapshot
from proving_ground.models.event_log import EventType
from proving_ground.models.vm_network import VMNetwork
from proving_ground.services.event_service import EventService
from proving_ground.services.dind_service import DinDService, get_dind_service
from proving_ground.services.docker_service import (
    DockerService,
    get_docker_service,
    _copytree_sparse,
)
from proving_ground.services.range_pool_service import (
    RangePoolService,
    get_pool_service,
)
from proving_ground.services.traefik_route_service import get_traefik_route_service
from proving_ground.services.notification_service import notify_range_users
from proving_ground.models.notification import NotificationType, NotificationSeverity

logger = logging.getLogger(__name__)

# Stored runtime references carry no registry so they mean the same thing on
# every deployment. An isolated range can only pull from the local mirror, so
# it is applied here, at the point of use.
_REGISTRY_MIRROR = f"{RegistryService.REGISTRY_IP}:{RegistryService.REGISTRY_PORT}"
settings = get_settings()


# The memory budget lives in utils.memory so the API's up-front validation and
# this deploy path cannot drift apart on what QEMU costs. Re-exported under the
# original name because both this module's call sites and its guard tests use
# it.
from proving_ground.utils.memory import (  # noqa: E402
    QEMU_OVERHEAD_MB as _QEMU_OVERHEAD_MB,
    vm_container_memory_mb as _vm_container_memory_mb,
    parse_memory_to_mb as _parse_memory_to_mb,
)

__all__ = ["_QEMU_OVERHEAD_MB", "_vm_container_memory_mb"]


def _install_iso_mb(environment: dict, volumes: Optional[dict]) -> Optional[int]:
    """Size of the ISO this VM is installing from, in MiB, or None.

    Both container-creation paths signal an ISO boot the same way: the file is
    bind-mounted and BOOT points at it. A VM booting a disk it already has sets
    neither, gets None, and is sized exactly as it was before.

    Returns None rather than raising if the file cannot be measured -- the ISO
    allowance is an optimisation on top of a working budget, so losing it must
    not fail a deploy.
    """
    boot = environment.get("BOOT") if environment else None
    if not boot or not volumes:
        return None
    for host_path, spec in volumes.items():
        if isinstance(spec, dict) and spec.get("bind") == boot:
            try:
                return int(os.path.getsize(host_path) / (1024 * 1024))
            except OSError as e:
                logger.warning(f"Could not size install ISO {host_path}: {e}")
                return None
    return None


def _has_usable_disk(storage_dir: str) -> bool:
    """Whether this VM already has a disk worth keeping.

    Deliberately not "is the directory non-empty". dockur creates scratch
    entries such as tmp/ before it has a disk, and a failed deploy leaves them
    behind -- which made the clone believe a learner's work was already there
    and skip it. The VM then booted an empty /storage, dockur fell through to
    downloading Windows from Microsoft, and on an isolated range that hangs
    with nothing in the VM's own logs to explain it. Observed exactly that.

    A disk image is what makes storage worth preserving.
    """
    if not os.path.isdir(storage_dir):
        return False
    for entry in os.listdir(storage_dir):
        if entry.endswith((".img", ".qcow2")):
            path = os.path.join(storage_dir, entry)
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return True
    return False


class RangeDeploymentService:
    """
    Manages range deployment lifecycle with DinD-based isolation.

    Each range runs inside its own Docker-in-Docker container:
    - Networks and VMs are created inside the DinD container
    - Provides complete network namespace isolation
    - Allows identical IP spaces across concurrent ranges
    """

    def __init__(
        self,
        docker_service: Optional[DockerService] = None,
        dind_service: Optional[DinDService] = None,
        pool_service: Optional[RangePoolService] = None,
    ):
        self._docker_service = docker_service
        self._dind_service = dind_service
        self._pool_service = pool_service

    @property
    def docker_service(self) -> DockerService:
        if self._docker_service is None:
            self._docker_service = get_docker_service()
        return self._docker_service

    @docker_service.setter
    def docker_service(self, value: DockerService) -> None:
        self._docker_service = value

    @property
    def dind_service(self) -> DinDService:
        if self._dind_service is None:
            self._dind_service = get_dind_service()
        return self._dind_service

    @dind_service.setter
    def dind_service(self, value: DinDService) -> None:
        self._dind_service = value

    @property
    def pool_service(self) -> RangePoolService:
        if self._pool_service is None:
            self._pool_service = get_pool_service()
        return self._pool_service

    @pool_service.setter
    def pool_service(self, value: RangePoolService) -> None:
        self._pool_service = value

    async def _acquire_dind(
        self,
        range_id: str,
        range_name: str,
        required_images: List[str],
        memory_limit: str,
        cpu_limit: float,
        progress_callback: Optional[Callable[[str], None]],
        on_claimed: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        """Claim a pre-warmed range if one fits, else provision cold.

        Returns the same dict shape either way, plus 'volume_name'. The pool
        is never a correctness dependency (see range_pool_service module
        docstring): pool_service.claim() already catches everything and
        returns None on any problem, so a claim failure here always falls
        through to cold provisioning rather than failing the deploy.

        I5: self.pool_service is a property that lazily constructs
        RangePoolService() (-> docker.from_env(), which on docker-py 7.1.0
        calls _retrieve_server_version() and raises DockerException when
        the socket is unreachable) the first time it's accessed - BEFORE
        claim()'s own try/except ever runs. That construction is guarded
        here too, not just the claim() call, so a pool-side Docker problem
        still falls through to cold provisioning instead of failing the
        deploy.

        on_claimed, if given, is invoked (no args) only after a SUCCESSFUL
        claim - callers use it to log the claim and enqueue a pool refill
        without this helper needing to know about events or Dramatiq.
        """
        if settings.range_pool_enabled:
            try:
                claimed = self.pool_service.claim(
                    range_id=range_id,
                    range_name=range_name,
                    required_images=required_images,
                )
            except Exception as e:
                logger.warning(f"Pool service unavailable, falling back to cold provisioning: {e}")
                claimed = None
            if claimed:
                if on_claimed:
                    on_claimed()
                return claimed
        return await self.dind_service.create_range_container(
            range_id=range_id,
            range_name=range_name,
            memory_limit=memory_limit,
            cpu_limit=cpu_limit,
            progress_callback=progress_callback,
        )

    def _resolve_range_images(self, db: Session, range_obj: Range) -> "dict[str, Optional[str]]":
        """Resolve the container image each of this range's VMs needs.

        This is the single source of truth for "what images does this range
        need" - used both to ask the warm pool for a matching member (via
        _acquire_dind's required_images) and, unchanged, by Stage 3 below to
        pull those same images into the DinD container. Computed once so the
        pool claim and the actual pull can never resolve a different set.

        It shares resolve_vm_image() with the deploy and sync paths. It used to
        reimplement the same rules, which is how a pinned runtime ended up
        being resolved here but never pulled -- container creation then failed
        on an image the range did not have.
        """
        vms = db.query(VM).filter(VM.range_id == range_obj.id).all()
        unique_images: dict[str, Optional[str]] = {}
        for vm in vms:
            resolved = resolve_vm_image(vm, mirror=_REGISTRY_MIRROR)
            if resolved.runtime:
                unique_images[resolved.runtime] = resolved.arch
                logger.info(f"Stage 3: VM {vm.hostname} needs image {resolved.runtime}")
        return unique_images

    async def deploy_range(
        self,
        db: Session,
        range_id: UUID,
        memory_limit: Optional[str] = None,
        cpu_limit: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Deploy a range instance inside a DinD container.

        Steps:
        1. Create DinD container for the range
        2. Create Docker networks inside DinD
        3. Pull required images into DinD
        4. Create VM containers inside DinD

        Args:
            db: Database session
            range_id: Range UUID
            memory_limit: Optional memory limit for DinD container
            cpu_limit: Optional CPU limit for DinD container

        Returns:
            Deployment result dict
        """
        # Get range from database
        range_obj = db.query(Range).filter(Range.id == range_id).first()
        if not range_obj:
            raise ValueError(f"Range {range_id} not found")

        range_obj.status = RangeStatus.DEPLOYING
        db.commit()

        try:
            result = await self._deploy_with_dind(db, range_obj, memory_limit, cpu_limit)

            # Update range status
            range_obj.status = RangeStatus.RUNNING
            range_obj.deployed_at = datetime.utcnow()
            range_obj.started_at = datetime.utcnow()
            range_obj.error_message = None
            db.commit()

            return result

        except Exception as e:
            logger.error(f"Failed to deploy range {range_id}: {e}")
            range_obj.status = RangeStatus.ERROR
            range_obj.error_message = str(e)[:1000]
            db.commit()
            raise

    async def _kvm_for_range(self, range_id: str, target_arch: "Optional[str]" = None) -> str:
        """dockur's KVM env value for a guest in this range: "Y" or "N".

        Asks the range's own DinD container, not the host and not this process.
        The API and worker containers do not have /dev/kvm mapped in, so the
        obvious os.path.exists("/dev/kvm") answers about the wrong machine and
        returns False on a host that has it -- which is how the DinD path came
        to hardcode "N" and cost every KVM-capable host a ~10x slowdown.

        Fails closed. KVM="Y" where KVM is not usable makes QEMU refuse to
        start; a slow guest beats one that never boots.
        """
        setting = get_settings().range_kvm

        if setting == "off":
            return "N"

        if target_arch and requires_emulation(target_arch):
            # Cross-architecture guests are emulated whatever the host offers.
            return "N"

        if setting == "on":
            return "Y"

        try:
            exit_code, _ = await self.dind_service.exec_in_container(
                str(range_id), ["test", "-e", "/dev/kvm"]
            )
        except Exception as e:  # noqa: BLE001 - probe failure must not fail a deploy
            logger.warning(f"KVM probe failed for range {range_id}, assuming none: {e}")
            return "N"

        if exit_code == 0:
            logger.info(f"KVM acceleration enabled for range {range_id}")
            return "Y"
        logger.info(f"No /dev/kvm in the range container for {range_id}; guests will be emulated")
        return "N"

    # dockur reads this to decide whether the host is virtualised, and it is the
    # same signal used here: an Azure VM reports the Hyper-V clocksource.
    _HYPERV_CLOCKSOURCE = "hyperv_clocksource_tsc_page"
    _CLOCKSOURCE_PATH = "/sys/devices/system/clocksource/clocksource0/current_clocksource"

    async def _host_is_hyperv_guest(self, range_id: str) -> bool:
        """Is the machine running this range itself a Hyper-V guest?

        Read from inside the range container for the same reason as the KVM
        probe: this process is not on the machine that runs the guest.
        """
        try:
            exit_code, out = await self.dind_service.exec_in_container(
                str(range_id), ["cat", self._CLOCKSOURCE_PATH]
            )
        except Exception as e:  # noqa: BLE001 - probe failure must not fail a deploy
            logger.warning(f"Clocksource probe failed for range {range_id}: {e}")
            return False
        return exit_code == 0 and self._HYPERV_CLOCKSOURCE in (out or "")

    async def _virt_env_for_range(
        self, range_id: str, target_arch: "Optional[str]" = None
    ) -> "dict[str, str]":
        """KVM and Hyper-V settings for a guest in this range.

        Decided together because HV only means anything when KVM is on. dockur
        passes Hyper-V enlightenments through by default under KVM, and on a
        host that is itself a Hyper-V guest that makes a Windows guest shut
        down seconds after the boot manager starts -- silently, with nothing
        logged by QEMU or KVM, so it reads as a broken image rather than a
        broken flag.
        """
        kvm = await self._kvm_for_range(range_id, target_arch)
        env = {"KVM": kvm}
        if kvm != "Y":
            return env

        setting = get_settings().range_hyperv
        if setting == "on":
            return env
        if setting == "off":
            env["HV"] = "false"
            return env

        if await self._host_is_hyperv_guest(range_id):
            logger.info(
                f"Range {range_id}: host is a Hyper-V guest, disabling nested "
                f"Hyper-V enlightenments"
            )
            env["HV"] = "false"
        return env

    async def _deploy_with_dind(
        self,
        db: Session,
        range_obj: Range,
        memory_limit: Optional[str],
        cpu_limit: Optional[float],
    ) -> Dict[str, Any]:
        """Deploy range inside a DinD container."""
        range_id = str(range_obj.id)
        range_uuid = range_obj.id

        logger.info(f"Deploying range {range_id} with DinD isolation")

        # Initialize event service for progress logging
        event_service = EventService(db)

        # Use default limits if not specified
        memory_limit = memory_limit or getattr(settings, "range_default_memory", "8g")
        cpu_limit = cpu_limit or getattr(settings, "range_default_cpu", 4.0)

        # Resolve required images once, up front, so the pool claim below and
        # Stage 3's pull loop always agree on what this range needs (see
        # _resolve_range_images docstring).
        unique_images = self._resolve_range_images(db, range_obj)

        # Define deployment stages for progress tracking
        total_stages = 4
        current_stage = 1

        # 1. Create DinD container with progress reporting
        event_service.log_event(
            range_id=range_uuid,
            event_type=EventType.DEPLOYMENT_STEP,
            message=f"[Stage {current_stage}/{total_stages}] Creating DinD container...",
        )
        event_service.log_event(
            range_id=range_uuid,
            event_type=EventType.ROUTER_CREATING,
            message="Creating DinD container for range isolation...",
        )

        def dind_progress_callback(msg: str) -> None:
            """Report DinD creation progress as deployment step events."""
            event_service.log_event(
                range_id=range_uuid,
                event_type=EventType.DEPLOYMENT_STEP,
                message=msg,
            )

        def on_pool_claim() -> None:
            """Only called after a SUCCESSFUL pool claim (see _acquire_dind)."""
            event_service.log_event(
                range_id=range_uuid,
                event_type=EventType.DEPLOYMENT_STEP,
                message="Claimed a pre-warmed range from the pool",
            )
            from proving_ground.tasks.pool import enqueue_pool_refill

            enqueue_pool_refill()

        dind_info = await self._acquire_dind(
            range_id=range_id,
            range_name=range_obj.name,
            required_images=list(unique_images.keys()),
            memory_limit=memory_limit,
            cpu_limit=cpu_limit,
            progress_callback=dind_progress_callback,
            on_claimed=on_pool_claim,
        )

        event_service.log_event(
            range_id=range_uuid,
            event_type=EventType.ROUTER_CREATED,
            message=f"DinD container ready at {dind_info['mgmt_ip']}",
        )

        # Store DinD info in range
        range_obj.dind_container_id = dind_info["container_id"]
        range_obj.dind_container_name = dind_info["container_name"]
        range_obj.dind_mgmt_ip = dind_info["mgmt_ip"]
        range_obj.dind_docker_url = dind_info["docker_url"]
        range_obj.dind_volume_name = dind_info.get("volume_name")
        db.commit()

        docker_url = dind_info["docker_url"]

        # 2. Create networks inside DinD
        current_stage = 2
        networks = db.query(Network).filter(Network.range_id == range_obj.id).all()
        network_ids = {}

        if networks:
            event_service.log_event(
                range_id=range_uuid,
                event_type=EventType.DEPLOYMENT_STEP,
                message=f"[Stage {current_stage}/{total_stages}] Creating {len(networks)} network(s)...",
            )

        for network in networks:
            event_service.log_event(
                range_id=range_uuid,
                event_type=EventType.NETWORK_CREATING,
                message=f"Creating network '{network.name}' ({network.subnet})...",
                network_id=network.id,
            )

            labels = {
                "pg.range_id": range_id,
                "pg.network_id": str(network.id),
            }

            network_docker_id = await self.docker_service.create_range_network_dind(
                range_id=range_id,
                docker_url=docker_url,
                name=network.name,
                subnet=network.subnet,
                gateway=network.gateway,
                internal=network.is_isolated,
                labels=labels,
            )

            network.docker_network_id = network_docker_id
            network_ids[str(network.id)] = network_docker_id

            event_service.log_event(
                range_id=range_uuid,
                event_type=EventType.NETWORK_CREATED,
                message=f"Network '{network.name}' created",
                network_id=network.id,
            )

        db.commit()

        # 2b. Set up iptables network isolation inside DinD
        network_names = [network.name for network in networks]
        # Networks with internet_enabled=True get NAT/outbound access
        allow_internet = [network.name for network in networks if network.internet_enabled]

        await self.dind_service.setup_network_isolation_in_dind(
            range_id=range_id,
            docker_url=docker_url,
            networks=network_names,
            allow_internet=allow_internet,
        )

        # 3. Pull required images into DinD
        current_stage = 3
        vms = db.query(VM).filter(VM.range_id == range_obj.id).all()
        # unique_images was already resolved above (_resolve_range_images),
        # before the pool claim, so it can't drift from what was claimed for.

        if unique_images:
            event_service.log_event(
                range_id=range_uuid,
                event_type=EventType.DEPLOYMENT_STEP,
                message=f"[Stage {current_stage}/{total_stages}] Transferring {len(unique_images)} image(s) to DinD...",
            )

        for idx, (image, image_arch) in enumerate(unique_images.items(), 1):
            # Create a progress callback that emits events for this image
            last_status = [None]  # Use list to allow modification in closure
            last_pct = [0]  # Track last reported percentage

            # image/idx/last_status/last_pct are bound as defaults rather than
            # captured: the closure is defined in a loop, so a late-bound
            # reference would resolve to the FINAL iteration's values if this
            # callback were ever held and invoked after the loop. It is awaited
            # within the same iteration today, so behaviour is unchanged -- this
            # just stops that from being load-bearing.
            def image_progress_callback(
                transferred: int,
                total: int,
                status: str,
                image: str = image,
                idx: int = idx,
                last_status: list = last_status,
                last_pct: list = last_pct,
            ) -> None:
                # Handle percentage progress updates (format: 'transferring:XX')
                if status.startswith("transferring:"):
                    pct = int(status.split(":")[1])
                    # Emit progress on every update (every 2 seconds) to keep GUI responsive
                    if pct > last_pct[0]:
                        last_pct[0] = pct
                        size_mb = total / 1024 / 1024
                        transferred_mb = transferred / 1024 / 1024
                        event_service.log_event(
                            range_id=range_uuid,
                            event_type=EventType.DEPLOYMENT_STEP,
                            message=f"Transferring {image}: {pct}% ({transferred_mb:.0f}/{size_mb:.0f} MB)",
                        )
                    return

                # Only emit event if status changed (avoid spamming)
                if status != last_status[0]:
                    last_status[0] = status
                    if status == "found_on_host":
                        size_mb = total / 1024 / 1024
                        event_service.log_event(
                            range_id=range_uuid,
                            event_type=EventType.DEPLOYMENT_STEP,
                            message=f"Image {idx}/{len(unique_images)}: {image} ({size_mb:.1f} MB)",
                        )
                    elif status == "pushing_to_registry":
                        event_service.log_event(
                            range_id=range_uuid,
                            event_type=EventType.DEPLOYMENT_STEP,
                            message=f"Ensuring {image} is in registry...",
                        )
                    elif status == "pulling_from_registry":
                        event_service.log_event(
                            range_id=range_uuid,
                            event_type=EventType.DEPLOYMENT_STEP,
                            message=f"Pulling {image} from registry into DinD...",
                        )
                    elif status == "transferring":
                        event_service.log_event(
                            range_id=range_uuid,
                            event_type=EventType.DEPLOYMENT_STEP,
                            message=f"Transferring {image} via tar (fallback)...",
                        )
                    elif status == "already_exists":
                        event_service.log_event(
                            range_id=range_uuid,
                            event_type=EventType.DEPLOYMENT_STEP,
                            message=f"Image {image} already in DinD, skipping",
                        )
                    elif status == "pulling_to_host":
                        event_service.log_event(
                            range_id=range_uuid,
                            event_type=EventType.DEPLOYMENT_STEP,
                            message=f"Pulling {image} to host...",
                        )

            try:
                pull_result = await self.docker_service.pull_image_to_dind(
                    range_id=range_id,
                    docker_url=docker_url,
                    image=image,
                    progress_callback=image_progress_callback,
                    arch=image_arch,
                )

                # Notify users if image was pulled from internet (not from local registry)
                if pull_result["source"] == "internet":
                    event_service.log_event(
                        range_id=range_uuid,
                        event_type=EventType.DEPLOYMENT_STEP,
                        message=f"WARNING: Image {image} pulled from internet (not from local registry)",
                    )
                    cache_msg = (
                        "Cached to registry for future deployments."
                        if pull_result["cached_to_registry"]
                        else "Failed to cache - future deployments may also pull from internet."
                    )
                    notify_range_users(
                        db=db,
                        range_id=range_uuid,
                        title="Image Pulled from Internet",
                        message=f"Image {image} was pulled from the internet (not cached locally). {cache_msg}",
                        notification_type=NotificationType.SYSTEM_ALERT,
                        severity=NotificationSeverity.WARNING,
                    )

                event_service.log_event(
                    range_id=range_uuid,
                    event_type=EventType.DEPLOYMENT_STEP,
                    message=f"Image {idx}/{len(unique_images)} ready: {image}",
                )
            except Exception as e:
                error_msg = f"Failed to transfer image {image}: {e}"
                logger.error(error_msg)
                event_service.log_event(
                    range_id=range_uuid,
                    event_type=EventType.DEPLOYMENT_STEP,
                    message=f"ERROR: {error_msg}",
                )
                # Re-raise to fail the deployment with a clear message
                raise RuntimeError(error_msg) from e

        # 4. Create VMs inside DinD
        current_stage = 4
        total_vms = len(vms)

        if total_vms > 0:
            event_service.log_event(
                range_id=range_uuid,
                event_type=EventType.DEPLOYMENT_STEP,
                message=f"[Stage {current_stage}/{total_stages}] Creating {total_vms} VM(s)...",
            )

        failed_vms = []
        for vm_idx, vm in enumerate(vms, 1):
            try:
                logger.info(f"VM {vm.hostname}: arch={vm.arch}, base_image_id={vm.base_image_id}")
                event_service.log_event(
                    range_id=range_uuid,
                    event_type=EventType.VM_CREATING,
                    message=f"Creating VM {vm_idx}/{total_vms}: '{vm.hostname}' ({vm.ip_address}, arch={vm.arch})...",
                    vm_id=vm.id,
                )

                # What this VM runs, and the disk it boots from. Shared with
                # sync_range and the pull list, so the image resolved here is
                # always the image that was fetched.
                _resolved = resolve_vm_image(vm, mirror=_REGISTRY_MIRROR)
                container_image = _resolved.runtime
                golden_clone_src = _resolved.clone_from
                logger.info(
                    f"VM {vm.hostname}: arch={vm.arch}, image={container_image}, "
                    f"clone_from={golden_clone_src}"
                )

                if not container_image:
                    error_msg = f"VM {vm.hostname} has no container image configured"
                    logger.warning(error_msg)
                    vm.status = VMStatus.ERROR
                    vm.error_message = error_msg
                    event_service.log_event(
                        range_id=range_uuid,
                        event_type=EventType.VM_ERROR,
                        message=error_msg,
                        vm_id=vm.id,
                    )
                    failed_vms.append(vm.hostname)
                    continue

                # Get all network interfaces for this VM (multi-NIC support)
                interfaces = (
                    db.query(VMNetwork)
                    .filter(VMNetwork.vm_id == vm.id)
                    .order_by(VMNetwork.is_primary.desc())  # Primary first
                    .all()
                )

                if interfaces:
                    # New multi-NIC path: use VMNetwork records
                    primary_iface = interfaces[0]
                    primary_network = (
                        db.query(Network).filter(Network.id == primary_iface.network_id).first()
                    )
                    primary_ip = primary_iface.ip_address
                    secondary_interfaces = interfaces[1:]
                else:
                    # Legacy fallback: use vm.network_id directly for backwards compatibility
                    primary_network = db.query(Network).filter(Network.id == vm.network_id).first()
                    primary_ip = vm.ip_address
                    secondary_interfaces = []

                if not primary_network:
                    error_msg = f"VM {vm.hostname} has no network assigned"
                    logger.warning(error_msg)
                    vm.status = VMStatus.ERROR
                    vm.error_message = error_msg
                    event_service.log_event(
                        range_id=range_uuid,
                        event_type=EventType.VM_ERROR,
                        message=error_msg,
                        vm_id=vm.id,
                    )
                    failed_vms.append(vm.hostname)
                    continue

                labels = {
                    "pg.range_id": range_id,
                    "pg.vm_id": str(vm.id),
                }

                # Set up environment variables: blueprint env vars first, then image-type overrides
                environment = dict(vm.environment) if vm.environment else {}
                volumes: Dict[str, Dict] = {}
                privileged = False
                cap_add = None
                sysctls = None
                devices = None

                # Apply container_config from BaseImage (VM Library settings)
                # This allows users to set privileged, cap_add, sysctls, devices per-image
                if vm.base_image and vm.base_image.container_config:
                    config = vm.base_image.container_config
                    if config.get("privileged"):
                        privileged = True
                        logger.info(
                            f"VM {vm.hostname}: privileged=True from base_image.container_config"
                        )
                    if config.get("cap_add"):
                        cap_add = config["cap_add"]
                        logger.info(
                            f"VM {vm.hostname}: cap_add={cap_add} from base_image.container_config"
                        )
                    if config.get("sysctls"):
                        sysctls = config["sysctls"]
                        logger.info(
                            f"VM {vm.hostname}: sysctls={sysctls} from base_image.container_config"
                        )
                    if config.get("devices"):
                        devices = config["devices"]
                        logger.info(
                            f"VM {vm.hostname}: devices={devices} from base_image.container_config"
                        )

                # macOS VM (dockur/macos or dockurr/macos) - requires VERSION env and privileged mode for KVM
                if (
                    "dockur/macos" in container_image.lower()
                    or "dockurr/macos" in container_image.lower()
                ):
                    # Map version number to dockur version name
                    macos_version_map = {
                        "15": "sequoia",
                        "14": "sonoma",
                        "13": "ventura",
                        "12": "monterey",
                        "11": "big-sur",
                    }
                    version_name = macos_version_map.get(
                        vm.macos_version, "sonoma"
                    )  # Default to Sonoma
                    environment["VERSION"] = version_name
                    privileged = True  # Required for KVM access
                    labels["pg.vm_type"] = "macos"

                # Windows VM (dockur/windows or dockurr/windows) - set VERSION if specified
                elif (
                    "dockur/windows" in container_image.lower()
                    or "dockurr/windows" in container_image.lower()
                ):
                    win_version = vm.windows_version or "11"
                    environment["VERSION"] = win_version

                    # Fall back to a pre-cached ISO so the range never has to
                    # download Windows from Microsoft. Range networks are isolated
                    # by default and cannot resolve the download hosts, which is
                    # the usual reason a Windows lab never comes up. The host ISO
                    # cache is bind-mounted into the DinD daemon (see
                    # dind_service), so this path resolves inside the range.
                    #
                    # This mirrors sync_range, which has had the same handling for
                    # some time. Deploy did not, so a cached ISO was only ever used
                    # on a sync -- never on the deploy that actually creates the VM.
                    # Booting from a captured golden image clones an already
                    # installed disk; booting an ISO runs the installer. They
                    # are mutually exclusive -- mounting the ISO as well would
                    # start a fresh install over the top of the clone.
                    # Resolved once, above, alongside the runtime image -- so
                    # the disk cloned and the image run always come from the
                    # same decision.
                    golden_src = golden_clone_src

                    if golden_src and os.path.isdir(golden_src):
                        # Both template-storage and vm-storage are bind-mounted
                        # into the DinD daemon, so these host paths resolve
                        # inside the range (see dind_service).
                        vm_storage = os.path.join(settings.vm_storage_dir, str(vm.id), "storage")
                        if not _has_usable_disk(vm_storage):
                            os.makedirs(vm_storage, exist_ok=True)
                            logger.info(f"Cloning golden image {golden_src} -> {vm_storage}")
                            # Sparse-aware: the disk is 64 GiB apparent against
                            # ~9 GiB allocated, and a plain copy inflates every
                            # clone to the full apparent size.
                            _copytree_sparse(golden_src, vm_storage)
                        else:
                            logger.info(f"Reusing existing VM storage: {vm_storage}")
                        volumes[vm_storage] = {"bind": "/storage", "mode": "rw"}
                        logger.info(
                            f"VM {vm.hostname}: booting from golden image, installer skipped"
                        )
                    else:
                        # Fall back to a pre-cached ISO so the range never has to
                        # download Windows from Microsoft. Range networks are
                        # isolated by default and cannot resolve the download
                        # hosts, which is the usual reason a Windows lab never
                        # comes up.
                        iso_path = vm.iso_path
                        if not iso_path and not vm.iso_url:
                            _cached_iso = os.path.join(
                                settings.iso_cache_dir,
                                "windows-isos",
                                f"windows-{win_version}.iso",
                            )
                            if os.path.isfile(_cached_iso):
                                iso_path = _cached_iso
                                logger.info(f"Using cached Windows ISO: {_cached_iso}")

                        if iso_path and os.path.isfile(iso_path):
                            volumes[iso_path] = {"bind": "/boot.iso", "mode": "ro"}
                            environment["BOOT"] = "/boot.iso"
                            logger.info(f"Mounting Windows ISO: {iso_path} -> /boot.iso")

                    # dockur sizes the guest from these; without them it uses its
                    # own defaults and silently ignores the VM's configured shape.
                    environment["CPU_CORES"] = str(vm.cpu or 2)
                    environment["RAM_SIZE"] = f"{vm.ram_mb or 4096}M"
                    environment["DISK_SIZE"] = f"{vm.disk_gb or 64}G"

                    environment.update(await self._virt_env_for_range(range_obj.id))
                    privileged = True  # Required for KVM access
                    labels["pg.vm_type"] = "windows"

                # Linux VM (qemux/qemu) - uses VERSION for distro
                elif "qemux/qemu" in container_image.lower():
                    privileged = True  # Required for KVM access
                    labels["pg.vm_type"] = "linux"

                event_service.log_event(
                    range_id=range_uuid,
                    event_type=EventType.DEPLOYMENT_STEP,
                    message=f"Creating container for '{vm.hostname}' using image {container_image}...",
                    vm_id=vm.id,
                )

                container_id = await self.docker_service.create_range_container_dind(
                    range_id=range_id,
                    docker_url=docker_url,
                    name=vm.hostname,
                    image=container_image,
                    network_name=primary_network.name,
                    ip_address=primary_ip,
                    cpu_limit=vm.cpu or 2,
                    memory_limit_mb=(
                        _vm_container_memory_mb(
                            vm.ram_mb or 4096,
                            install_iso_mb=_install_iso_mb(environment, volumes),
                            range_cap_mb=_parse_memory_to_mb(memory_limit),
                        )
                        if privileged
                        else (vm.ram_mb or 2048)
                    ),
                    hostname=vm.hostname,
                    labels=labels,
                    dns_servers=primary_network.dns_servers,
                    dns_search=primary_network.dns_search,
                    environment=environment if environment else None,
                    volumes=volumes if volumes else None,
                    privileged=privileged,
                    cap_add=cap_add,
                    sysctls=sysctls,
                    devices=devices,
                    arch=vm.arch,
                )

                vm.container_id = container_id
                # Container built from vm.environment -> desired and applied are
                # in sync. See VM.applied_environment for why this is tracked.
                vm.applied_environment = dict(vm.environment) if vm.environment else None

                # Attach secondary networks before starting (multi-NIC support)
                if secondary_interfaces:
                    event_service.log_event(
                        range_id=range_uuid,
                        event_type=EventType.DEPLOYMENT_STEP,
                        message=f"Attaching {len(secondary_interfaces)} additional network(s) to '{vm.hostname}'...",
                        vm_id=vm.id,
                    )
                    for iface in secondary_interfaces:
                        sec_network = (
                            db.query(Network).filter(Network.id == iface.network_id).first()
                        )
                        if sec_network:
                            self.docker_service.connect_container_to_network_dind(
                                range_id=range_id,
                                docker_url=docker_url,
                                container_id=container_id,
                                network_name=sec_network.name,
                                ip_address=iface.ip_address,
                            )
                            logger.info(
                                f"Attached secondary NIC to {vm.hostname}: {sec_network.name} ({iface.ip_address})"
                            )

                event_service.log_event(
                    range_id=range_uuid,
                    event_type=EventType.DEPLOYMENT_STEP,
                    message=f"Container created for '{vm.hostname}', starting...",
                    vm_id=vm.id,
                )

                # Start the container
                await self.docker_service.start_range_container_dind(
                    range_id=range_id,
                    docker_url=docker_url,
                    container_id=container_id,
                )

                # Mark VM as running after successful start (Issue #75)
                vm.status = VMStatus.RUNNING

                # Build network info for the event message
                network_info = f"{primary_network.name} ({primary_ip})"
                if secondary_interfaces:
                    for iface in secondary_interfaces:
                        sec_network = (
                            db.query(Network).filter(Network.id == iface.network_id).first()
                        )
                        if sec_network:
                            network_info += f", {sec_network.name} ({iface.ip_address})"

                event_service.log_event(
                    range_id=range_uuid,
                    event_type=EventType.VM_STARTED,
                    message=f"VM '{vm.hostname}' running on {network_info}",
                    vm_id=vm.id,
                )

            except Exception as e:
                # Log VM-specific error and continue with other VMs
                error_msg = f"Failed to create VM '{vm.hostname}': {str(e)[:200]}"
                logger.error(error_msg)
                vm.status = VMStatus.ERROR
                vm.error_message = str(e)[:500]
                event_service.log_event(
                    range_id=range_uuid,
                    event_type=EventType.VM_ERROR,
                    message=error_msg,
                    vm_id=vm.id,
                )
                failed_vms.append(vm.hostname)

        db.commit()

        # Log summary if some VMs failed
        if failed_vms:
            event_service.log_event(
                range_id=range_uuid,
                event_type=EventType.DEPLOYMENT_STEP,
                message=f"Warning: {len(failed_vms)} VM(s) failed to create: {', '.join(failed_vms)}",
            )

        # 4b. Set up inter-network routing between all range networks
        # Enable routing between all network pairs so VMs on different subnets
        # can communicate. Internet isolation is controlled separately by the
        # internet_enabled flag on each network.
        all_network_names = [n.name for n in networks]
        routing_pairs: set[tuple[str, str]] = set()

        # Add all network pairs for full inter-network routing
        if len(all_network_names) > 1:
            for i, net_a in enumerate(all_network_names):
                for net_b in all_network_names[i + 1 :]:
                    pair = tuple(sorted([net_a, net_b]))
                    routing_pairs.add(pair)

        if routing_pairs:
            event_service.log_event(
                range_id=range_uuid,
                event_type=EventType.DEPLOYMENT_STEP,
                message=f"Enabling inter-network routing for {len(routing_pairs)} network pair(s)...",
            )
            try:
                await self.dind_service.setup_inter_network_routing(
                    range_id=range_id,
                    docker_url=docker_url,
                    network_pairs=list(routing_pairs),
                )
            except Exception as e:
                logger.warning(f"Failed to set up inter-network routing: {e}")

        # 5. Set up VNC port forwarding using iptables DNAT (replaces nginx proxy)
        vm_ports = []
        for vm in vms:
            if not vm.container_id:
                continue

            # Get primary IP for VNC (multi-NIC support)
            primary_iface = (
                db.query(VMNetwork)
                .filter(VMNetwork.vm_id == vm.id, VMNetwork.is_primary == True)
                .first()
            )
            vnc_ip = primary_iface.ip_address if primary_iface else vm.ip_address

            if not vnc_ip:
                continue

            # Determine VNC port based on VM type and image
            vnc_port = 8006  # Default for QEMU/Windows

            # Get base image name for VNC port detection
            base_image = ""
            vm_type = None
            if vm.base_image_id and vm.base_image:
                base_image = vm.base_image.docker_image_tag or ""
                vm_type = vm.base_image.vm_type
            elif vm.golden_image_id and vm.golden_image:
                base_image = vm.golden_image.docker_image_tag or ""
                vm_type = vm.golden_image.vm_type
            elif vm.snapshot_id and vm.snapshot:
                base_image = vm.snapshot.docker_image_tag or ""
                vm_type = vm.snapshot.vm_type

            # Check if it's a container type for VNC port detection
            if vm_type == "container" or vm_type == VMType.CONTAINER:
                # An image may declare its console port explicitly via image.yaml
                # (container_config.console_port) — honor it over name heuristics
                # so custom webtop/kasm-derived images (tagged proving-ground/...)
                # route to the right port.
                _declared_cfg = (
                    getattr(vm.base_image, "container_config", None) if vm.base_image else None
                )
                _declared_port = (_declared_cfg or {}).get("console_port")
                if _declared_port:
                    vnc_port = int(_declared_port)
                elif (
                    "dockur/windows" in base_image.lower()
                    or "dockurr/windows" in base_image.lower()
                ):
                    vnc_port = 8006  # dockur/windows uses QEMU VNC port
                elif "dockur/macos" in base_image.lower() or "dockurr/macos" in base_image.lower():
                    vnc_port = 8006  # dockur/macos uses QEMU VNC port
                elif "kasmweb" in base_image:
                    vnc_port = 6901
                elif "linuxserver/" in base_image or "lscr.io/linuxserver" in base_image:
                    vnc_port = 3000
                else:
                    vnc_port = 6901  # Default for containers

            vm_ports.append(
                {
                    "vm_id": str(vm.id),
                    "hostname": vm.hostname,
                    "vnc_port": vnc_port,
                    "ip_address": vnc_ip,
                    "image": base_image,
                }
            )

        if vm_ports:
            event_service.log_event(
                range_id=range_uuid,
                event_type=EventType.DEPLOYMENT_STEP,
                message=f"Setting up VNC console access for {len(vm_ports)} VM(s)...",
            )
            try:
                port_mappings = await self.dind_service.setup_vnc_port_forwarding(
                    range_id=range_id,
                    vm_ports=vm_ports,
                )
                # Enrich port_mappings with VNC auth credentials from VM environment
                for vm in vms:
                    vm_id_str = str(vm.id)
                    if vm_id_str in port_mappings and vm.environment:
                        port_mappings[vm_id_str]["vnc_user"] = vm.environment.get("CUSTOM_USER")
                        port_mappings[vm_id_str]["vnc_password"] = vm.environment.get("PASSWORD")

                range_obj.vnc_proxy_mappings = port_mappings
                db.commit()
                logger.info(
                    f"Set up VNC port forwarding for range {range_id} with {len(vm_ports)} ports"
                )

                # Generate Traefik routes for VNC console access
                traefik_service = get_traefik_route_service()
                route_file = traefik_service.generate_vnc_routes(range_id, port_mappings)
                if route_file:
                    logger.info(f"Generated Traefik VNC routes for range {range_id}: {route_file}")
                else:
                    logger.warning(f"Could not generate Traefik VNC routes for range {range_id}")

            except Exception as e:
                logger.warning(f"Failed to set up VNC port forwarding for range {range_id}: {e}")
                # Don't fail the deployment if VNC forwarding fails - console will be unavailable

        return {
            "range_id": range_id,
            "status": "deployed",
            "isolation": "dind",
            "dind_container": dind_info["container_name"],
            "mgmt_ip": dind_info["mgmt_ip"],
            "docker_url": docker_url,
            "networks_created": len(networks),
            "vms_created": len([vm for vm in vms if vm.container_id]),
            "vnc_proxy_ports": len(vm_ports),
        }

    async def destroy_range(
        self,
        db: Session,
        range_id: UUID,
    ) -> Dict[str, Any]:
        """
        Destroy a range instance by deleting its DinD container.

        This cleans up everything inside the DinD container automatically.
        """
        range_obj = db.query(Range).filter(Range.id == range_id).first()
        if not range_obj:
            raise ValueError(f"Range {range_id} not found")

        range_id_str = str(range_id)

        # Remove Traefik VNC routes first
        traefik_service = get_traefik_route_service()
        traefik_service.remove_vnc_routes(range_id_str)

        # Delete the DinD container (cleans up everything inside)
        logger.info(f"Destroying range {range_id_str} DinD container")
        await self.dind_service.delete_range_container(
            range_id_str, volume_name=range_obj.dind_volume_name
        )

        # C1(a): the teardown half of "refill on every claim and every
        # teardown" (tasks/pool.py's module docstring already asserted this;
        # it was never wired up before this fix wave).
        try:
            from proving_ground.tasks.pool import enqueue_pool_refill

            enqueue_pool_refill()
        except Exception as e:
            logger.warning(f"Could not enqueue pool refill: {e}")

        # Clear DinD info
        range_obj.dind_container_id = None
        range_obj.dind_container_name = None
        range_obj.dind_mgmt_ip = None
        range_obj.dind_docker_url = None
        range_obj.dind_volume_name = None
        range_obj.vnc_proxy_mappings = None

        # Clear VM container IDs
        vms = db.query(VM).filter(VM.range_id == range_id).all()
        for vm in vms:
            vm.container_id = None

        # Clear network Docker IDs
        networks = db.query(Network).filter(Network.range_id == range_id).all()
        for network in networks:
            network.docker_network_id = None

        # Update range status
        range_obj.status = RangeStatus.STOPPED
        range_obj.stopped_at = datetime.utcnow()
        db.commit()

        return {
            "range_id": range_id_str,
            "status": "destroyed",
        }

    async def sync_range(
        self,
        db: Session,
        range_id: UUID,
    ) -> Dict[str, Any]:
        """
        Sync new networks and VMs to an existing deployed range.

        This provisions resources that were added after initial deployment:
        - Networks without docker_network_id → created in DinD
        - VMs without container_id → created in DinD

        Does NOT recreate the DinD container.
        """
        range_obj = db.query(Range).filter(Range.id == range_id).first()
        if not range_obj:
            raise ValueError(f"Range {range_id} not found")

        if not range_obj.dind_container_id or not range_obj.dind_docker_url:
            raise ValueError(f"Range {range_id} is not deployed (missing DinD container)")

        range_id_str = str(range_id)
        docker_url = range_obj.dind_docker_url

        logger.info(f"Syncing range {range_id_str} - provisioning new resources to DinD")

        result = {
            "range_id": range_id_str,
            "networks_created": 0,
            "vms_created": 0,
            "images_pulled": 0,
            "network_details": [],
            "vm_details": [],
        }

        # 1. Create any new networks
        networks = db.query(Network).filter(Network.range_id == range_id).all()
        new_networks = [n for n in networks if not n.docker_network_id]

        for network in new_networks:
            logger.info(f"Creating network {network.name} in DinD")
            labels = {
                "pg.range_id": range_id_str,
                "pg.network_id": str(network.id),
            }

            network_docker_id = await self.docker_service.create_range_network_dind(
                range_id=range_id_str,
                docker_url=docker_url,
                name=network.name,
                subnet=network.subnet,
                gateway=network.gateway,
                internal=network.is_isolated,
                labels=labels,
            )

            network.docker_network_id = network_docker_id
            result["networks_created"] += 1
            result["network_details"].append(
                {
                    "name": network.name,
                    "subnet": network.subnet,
                    "docker_id": network_docker_id[:12],
                }
            )

        db.commit()

        # Update iptables if we added networks
        if new_networks:
            network_names = [n.name for n in networks]
            allow_internet = [n.name for n in networks if n.internet_enabled]
            await self.dind_service.setup_network_isolation_in_dind(
                range_id=range_id_str,
                docker_url=docker_url,
                networks=network_names,
                allow_internet=allow_internet,
            )

        # 2. Pull images for new VMs
        vms = db.query(VM).filter(VM.range_id == range_id).all()
        new_vms = [v for v in vms if not v.container_id]

        # Collect unique images needed for new VMs
        unique_images = set()
        for vm in new_vms:
            if vm.base_image_id:
                base_img = db.query(BaseImage).filter(BaseImage.id == vm.base_image_id).first()
                if base_img:
                    if base_img.image_type == "container":
                        # Container-based VMs use the docker image directly
                        image_tag = base_img.docker_image_tag or base_img.docker_image_id
                        # Override dockurr/windows image based on vm.arch
                        if (
                            image_tag
                            and vm.arch
                            and (
                                "dockurr/windows" in image_tag.lower()
                                or "dockur/windows" in image_tag.lower()
                            )
                        ):
                            if vm.arch == "x86_64":
                                image_tag = "dockurr/windows:latest"
                            elif vm.arch == "arm64":
                                image_tag = "dockurr/windows-arm:latest"
                        if image_tag:
                            unique_images.add(image_tag)
                    elif base_img.image_type == "iso":
                        # ISO-based VMs use QEMU images based on vm_type
                        # vm.arch overrides base_img.native_arch when set
                        target_arch = vm.arch or base_img.native_arch
                        if base_img.vm_type == VMType.WINDOWS_VM:
                            if target_arch == "arm64":
                                unique_images.add("dockurr/windows-arm:latest")
                            else:
                                unique_images.add("dockurr/windows:latest")
                        elif base_img.vm_type == VMType.LINUX_VM:
                            unique_images.add("qemux/qemu:latest")
                        elif base_img.vm_type == VMType.MACOS_VM:
                            unique_images.add("dockurr/macos:latest")
            elif vm.golden_image_id:
                golden_img = (
                    db.query(GoldenImage).filter(GoldenImage.id == vm.golden_image_id).first()
                )
                if golden_img:
                    image_tag = golden_img.docker_image_tag or golden_img.docker_image_id
                    if image_tag:
                        unique_images.add(image_tag)
            elif vm.snapshot_id:
                snapshot = db.query(Snapshot).filter(Snapshot.id == vm.snapshot_id).first()
                if snapshot:
                    image_tag = snapshot.docker_image_tag or snapshot.docker_image_id
                    if image_tag:
                        unique_images.add(image_tag)

        # Pull images into DinD
        for image in unique_images:
            logger.info(f"Pulling image {image} into DinD for sync")
            pull_result = await self.docker_service.pull_image_to_dind(
                range_id_str, docker_url, image
            )

            # Notify users if image was pulled from internet (not from local registry)
            if pull_result["source"] == "internet":
                logger.warning(
                    f"Image {image} pulled from internet (not from local registry) during sync"
                )
                cache_msg = (
                    "Cached to registry for future deployments."
                    if pull_result["cached_to_registry"]
                    else "Failed to cache - future deployments may also pull from internet."
                )
                notify_range_users(
                    db=db,
                    range_id=range_id,
                    title="Image Pulled from Internet",
                    message=f"Image {image} was pulled from the internet (not cached locally). {cache_msg}",
                    notification_type=NotificationType.SYSTEM_ALERT,
                    severity=NotificationSeverity.WARNING,
                )

            result["images_pulled"] += 1

        # 3. Create new VMs
        created_vms = []  # Track successfully created VMs for VNC setup

        for vm in new_vms:
            logger.info(f"Creating VM {vm.hostname} in DinD")
            try:
                # What this VM runs, and the disk it boots from. The same
                # resolution the deploy path and the pull list use -- these
                # were three separate implementations, and every place they
                # disagreed was a defect.
                _resolved = resolve_vm_image(vm, mirror=_REGISTRY_MIRROR)
                image_tag = _resolved.runtime
                golden_clone_src = _resolved.clone_from
                vm_type = vm.effective_vm_type
                base_img = vm.base_image
                logger.info(f"VM {vm.hostname}: image={image_tag} clone_from={golden_clone_src}")

                if not image_tag:
                    logger.warning(f"No image found for VM {vm.hostname}, skipping")
                    vm.status = VMStatus.ERROR
                    vm.error_message = "No image configured"
                    continue

                # Get all network interfaces for this VM (multi-NIC support)
                interfaces = (
                    db.query(VMNetwork)
                    .filter(VMNetwork.vm_id == vm.id)
                    .order_by(VMNetwork.is_primary.desc())  # Primary first
                    .all()
                )

                if interfaces:
                    # New multi-NIC path: use VMNetwork records
                    primary_iface = interfaces[0]
                    primary_network = (
                        db.query(Network).filter(Network.id == primary_iface.network_id).first()
                    )
                    primary_ip = primary_iface.ip_address
                    secondary_interfaces = interfaces[1:]
                else:
                    # Legacy fallback: use vm.network_id directly for backwards compatibility
                    primary_network = db.query(Network).filter(Network.id == vm.network_id).first()
                    primary_ip = vm.ip_address
                    secondary_interfaces = []

                if not primary_network or not primary_network.docker_network_id:
                    logger.warning(f"Network not provisioned for VM {vm.hostname}, skipping")
                    vm.status = VMStatus.ERROR
                    vm.error_message = "Network not provisioned"
                    continue

                labels = {
                    "pg.range_id": range_id_str,
                    "pg.vm_id": str(vm.id),
                }

                # Set up environment variables: blueprint env vars first, then image-type overrides
                environment = dict(vm.environment) if vm.environment else {}
                privileged = False
                volumes = {}

                # Get ISO path if this is an ISO-based VM
                iso_path = None
                if base_img and base_img.image_type == "iso" and base_img.iso_path:
                    iso_path = base_img.iso_path

                # macOS VM (dockurr/macos) - requires VERSION env and privileged mode for KVM
                if "dockurr/macos" in image_tag.lower():
                    macos_version_map = {
                        "15": "sequoia",
                        "14": "sonoma",
                        "13": "ventura",
                        "12": "monterey",
                        "11": "big-sur",
                    }
                    version_name = macos_version_map.get(vm.macos_version, "sonoma")
                    environment["VERSION"] = version_name
                    privileged = True
                    labels["pg.vm_type"] = "macos"

                # Windows VM (dockurr/windows) - set VERSION if specified
                elif "dockurr/windows" in image_tag.lower():
                    if vm.windows_version:
                        environment["VERSION"] = vm.windows_version
                    # Fall back to a pre-cached ISO so the range never has to
                    # download Windows from Microsoft. The range's isolated
                    # network frequently cannot resolve/reach the download hosts,
                    # which is the usual reason a Windows lab never comes up. The
                    # host ISO cache is bind-mounted into the DinD daemon (see
                    # dind_service), so this path resolves inside the range.
                    # A golden image clone and an installer ISO are mutually
                    # exclusive: mounting /boot.iso over an already-installed
                    # disk starts a fresh install on top of it.
                    if golden_clone_src and os.path.isdir(golden_clone_src):
                        vm_storage = os.path.join(settings.vm_storage_dir, str(vm.id), "storage")
                        if not _has_usable_disk(vm_storage):
                            os.makedirs(vm_storage, exist_ok=True)
                            logger.info(f"Cloning golden image {golden_clone_src} -> {vm_storage}")
                            _copytree_sparse(golden_clone_src, vm_storage)
                        else:
                            logger.info(f"Reusing existing VM storage: {vm_storage}")
                        volumes[vm_storage] = {"bind": "/storage", "mode": "rw"}
                        logger.info(
                            f"VM {vm.hostname}: booting from golden image, installer skipped"
                        )
                    else:
                        if not iso_path and vm.windows_version:
                            _cached_iso = os.path.join(
                                settings.iso_cache_dir,
                                "windows-isos",
                                f"windows-{vm.windows_version}.iso",
                            )
                            if os.path.isfile(_cached_iso):
                                iso_path = _cached_iso
                                logger.info(f"Using cached Windows ISO: {_cached_iso}")
                        # For Windows ISO VMs, mount the ISO file directly
                        if iso_path:
                            # Mount ISO file directly to /boot.iso (same as start_vm)
                            volumes[iso_path] = {"bind": "/boot.iso", "mode": "ro"}
                            environment["BOOT"] = "/boot.iso"
                            logger.info(f"Mounting Windows ISO: {iso_path} -> /boot.iso")
                    # Set resource limits
                    environment["CPU_CORES"] = str(vm.cpu or 2)
                    environment["RAM_SIZE"] = f"{vm.ram_mb or 4096}M"
                    environment["DISK_SIZE"] = f"{vm.disk_gb or 64}G"
                    environment.update(await self._virt_env_for_range(range_obj.id))
                    privileged = True
                    labels["pg.vm_type"] = "windows"

                # Linux VM (qemux/qemu) - uses VERSION for distro or BOOT for custom ISO
                elif "qemux/qemu" in image_tag.lower():
                    # For Linux ISO VMs, mount the ISO file directly and set BOOT
                    if iso_path:

                        # Mount ISO file directly to /boot.iso (same as start_vm)
                        volumes[iso_path] = {"bind": "/boot.iso", "mode": "ro"}
                        environment["BOOT"] = "/boot.iso"
                        logger.info(f"Mounting Linux ISO: {iso_path} -> /boot.iso")
                    # Set resource limits
                    environment["CPU_CORES"] = str(vm.cpu or 2)
                    environment["RAM_SIZE"] = f"{vm.ram_mb or 2048}M"
                    environment["DISK_SIZE"] = f"{vm.disk_gb or 20}G"
                    environment.update(await self._virt_env_for_range(range_obj.id))
                    privileged = True
                    labels["pg.vm_type"] = "linux"

                # Create container with primary network (same pattern as deploy_range)
                container_id = await self.docker_service.create_range_container_dind(
                    range_id=range_id_str,
                    docker_url=docker_url,
                    name=vm.hostname,
                    image=image_tag,
                    network_name=primary_network.name,
                    ip_address=primary_ip,
                    cpu_limit=vm.cpu or 2,
                    memory_limit_mb=(
                        _vm_container_memory_mb(
                            vm.ram_mb or 4096,
                            install_iso_mb=_install_iso_mb(environment, volumes),
                            range_cap_mb=_parse_memory_to_mb(
                                getattr(settings, "range_default_memory", None)
                            ),
                        )
                        if privileged
                        else (vm.ram_mb or 2048)
                    ),
                    hostname=vm.hostname,
                    labels=labels,
                    dns_servers=primary_network.dns_servers,
                    dns_search=primary_network.dns_search,
                    environment=environment if environment else None,
                    privileged=privileged,
                    volumes=volumes if volumes else None,
                )

                vm.container_id = container_id
                # Container built from vm.environment -> desired and applied are
                # in sync. See VM.applied_environment for why this is tracked.
                vm.applied_environment = dict(vm.environment) if vm.environment else None

                # Attach secondary networks before starting (multi-NIC support)
                for iface in secondary_interfaces:
                    sec_network = db.query(Network).filter(Network.id == iface.network_id).first()
                    if sec_network and sec_network.docker_network_id:
                        self.docker_service.connect_container_to_network_dind(
                            range_id=range_id_str,
                            docker_url=docker_url,
                            container_id=container_id,
                            network_name=sec_network.name,
                            ip_address=iface.ip_address,
                        )
                        logger.info(
                            f"Attached secondary NIC to {vm.hostname}: {sec_network.name} ({iface.ip_address})"
                        )

                # Start the container
                await self.docker_service.start_range_container_dind(
                    range_id=range_id_str,
                    docker_url=docker_url,
                    container_id=container_id,
                )

                vm.status = VMStatus.RUNNING

                # Determine VNC port based on image type
                vnc_port = 8006  # Default for QEMU/Windows
                if vm_type == "container" or vm_type == VMType.CONTAINER:
                    # Honor an explicitly declared console port (image.yaml
                    # container_config.console_port) over name heuristics.
                    _declared_cfg = (
                        getattr(vm.base_image, "container_config", None) if vm.base_image else None
                    )
                    _declared_port = (_declared_cfg or {}).get("console_port")
                    if _declared_port:
                        vnc_port = int(_declared_port)
                    elif (
                        "dockur/windows" in image_tag.lower()
                        or "dockurr/windows" in image_tag.lower()
                    ):
                        vnc_port = 8006  # dockur/windows uses QEMU VNC port
                    elif (
                        "dockur/macos" in image_tag.lower() or "dockurr/macos" in image_tag.lower()
                    ):
                        vnc_port = 8006  # dockur/macos uses QEMU VNC port
                    elif "kasmweb" in image_tag:
                        vnc_port = 6901
                    elif "linuxserver/" in image_tag or "lscr.io/linuxserver" in image_tag:
                        vnc_port = 3000
                    else:
                        vnc_port = 6901  # Default for containers

                created_vms.append(
                    {
                        "vm_id": str(vm.id),
                        "hostname": vm.hostname,
                        "vnc_port": vnc_port,
                        "ip_address": primary_ip,  # Use primary IP for VNC
                        "container_id": container_id,
                        "image": image_tag,
                    }
                )

                result["vms_created"] += 1
                result["vm_details"].append(
                    {
                        "hostname": vm.hostname,
                        "container_id": container_id[:12],
                        "vnc_port": vnc_port,
                    }
                )

            except Exception as e:
                logger.error(f"Failed to create VM {vm.hostname}: {e}")
                vm.status = VMStatus.ERROR
                vm.error_message = str(e)[:500]

        db.commit()

        # 4. Set up VNC port forwarding for newly created VMs
        if created_vms:
            traefik_service = get_traefik_route_service()
            try:
                # Get existing mappings to calculate next available port
                existing_mappings = range_obj.vnc_proxy_mappings or {}

                port_mappings = await self.dind_service.setup_vnc_port_forwarding(
                    range_id=range_id_str,
                    vm_ports=created_vms,
                    existing_mappings=existing_mappings,
                )
                # Enrich port_mappings with VNC auth credentials from VM environment
                for vm in vms:
                    vm_id_str2 = str(vm.id)
                    if vm_id_str2 in port_mappings and vm.environment:
                        port_mappings[vm_id_str2]["vnc_user"] = vm.environment.get("CUSTOM_USER")
                        port_mappings[vm_id_str2]["vnc_password"] = vm.environment.get("PASSWORD")

                # Merge new port mappings with existing ones
                existing_mappings.update(port_mappings)
                range_obj.vnc_proxy_mappings = existing_mappings
                db.commit()

                # Generate Traefik routes for VNC console access
                route_file = traefik_service.generate_vnc_routes(range_id_str, existing_mappings)
                if route_file:
                    logger.info(f"Updated Traefik VNC routes for range {range_id_str}")
            except Exception as e:
                logger.error(f"Failed to set up VNC forwarding for synced VMs: {e}")

        logger.info(
            f"Sync complete for range {range_id_str}: {result['networks_created']} networks, {result['vms_created']} VMs"
        )

        return result

    async def get_range_status(
        self,
        db: Session,
        range_id: UUID,
    ) -> Dict[str, Any]:
        """Get detailed status of a range including DinD container info."""
        range_obj = db.query(Range).filter(Range.id == range_id).first()
        if not range_obj:
            raise ValueError(f"Range {range_id} not found")

        range_id_str = str(range_id)
        result = {
            "range_id": range_id_str,
            "name": range_obj.name,
            "status": range_obj.status.value if range_obj.status else "unknown",
            "deployed_at": (range_obj.deployed_at.isoformat() if range_obj.deployed_at else None),
        }

        if range_obj.dind_container_id:
            # Get DinD container status
            dind_info = await self.dind_service.get_container_info(range_id_str)
            if dind_info:
                result["dind"] = {
                    "container_name": dind_info["container_name"],
                    "container_status": dind_info["status"],
                    "mgmt_ip": dind_info["mgmt_ip"],
                    "docker_url": dind_info.get("docker_url"),
                }

                # Get VMs inside DinD if running
                if dind_info["status"] == "running" and dind_info.get("docker_url"):
                    try:
                        vms = await self.docker_service.list_range_containers_dind(
                            range_id=range_id_str,
                            docker_url=dind_info["docker_url"],
                        )
                        result["vms"] = vms
                    except Exception as e:
                        logger.warning(f"Could not list VMs for range {range_id_str}: {e}")
                        result["vms"] = []
            else:
                result["dind"] = None
        else:
            result["dind"] = None
            result["vms"] = []

        return result


# Singleton instance
_range_deployment_service: Optional[RangeDeploymentService] = None


def get_range_deployment_service() -> RangeDeploymentService:
    """Get the range deployment service singleton."""
    global _range_deployment_service
    if _range_deployment_service is None:
        _range_deployment_service = RangeDeploymentService()
    return _range_deployment_service
