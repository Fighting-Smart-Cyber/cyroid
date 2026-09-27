# proving_ground/services/docker_service.py
"""
Docker orchestration service for managing containers and networks.

Supports three VM/container types:
1. Container: Basic Docker containers for lightweight Linux workloads
2. Linux VM: Full Linux VMs via qemux/qemu (desktop & server environments)
3. Windows VM: Full Windows VMs via dockur/windows

Both qemux/qemu and dockur/windows provide:
- KVM acceleration for near-native performance
- Web-based VNC console on port 8006
- Persistent storage and golden image support
- Auto-download of OS images (24+ Linux distros, all Windows versions)

DinD (Docker-in-Docker) Isolation:
When DIND_ISOLATION_ENABLED=true, each range deploys inside its own DinD
container, providing complete network namespace isolation. This eliminates
IP conflicts between concurrent range instances using identical blueprint IPs.
"""
import docker
from docker.errors import APIError, NotFound, ImageNotFound, DockerException
from typing import Optional, Dict, List, Any, Callable, TYPE_CHECKING
import logging
import ipaddress
import os
import shutil

from proving_ground.utils.arch import IS_ARM, HOST_ARCH, requires_emulation
from proving_ground.utils.image_ref import is_digest_ref, strip_registry
from proving_ground.config import get_settings, infrastructure_network_names

if TYPE_CHECKING:
    from proving_ground.services.dind_service import DinDService

logger = logging.getLogger(__name__)

# The root of the Docker SDK's exception tree, named here so the HTTP layer can
# turn "this install has no daemon" into a legible refusal without importing the
# SDK into a module that holds no client -- CLAUDE.md rule 1, and the app factory
# has no business holding one. Everything the SDK raises descends from this,
# including APIError, so one handler registered against it covers the tree.
DOCKER_SDK_ERROR: type[Exception] = DockerException

# A Windows disk is mostly holes: 64 GiB apparent, ~9 GiB allocated. Neither
# tar nor shutil.copy2 knows that -- Docker's archive endpoint reads holes as
# zeros, and copy2 uses os.sendfile, which writes them as real blocks. Either
# way a 9 GB image lands fully allocated at 64 GB, and every range cloned from
# it pays the same. Measured: copy2 turns a 64 MiB hole into 64 MiB of
# allocated blocks; seeking over all-zero chunks keeps it at 0.
_SPARSE_CHUNK = 1 << 20


def _write_sparse(src_fileobj, dst_path: str, total_size: int) -> None:
    """Copy src_fileobj to dst_path, leaving all-zero chunks as holes."""
    with open(dst_path, "wb") as dst:
        while True:
            chunk = src_fileobj.read(_SPARSE_CHUNK)
            if not chunk:
                break
            # strip() is a C-level scan, cheaper than building a zero buffer to
            # compare against, and it handles a short final chunk unchanged.
            if chunk.strip(b"\0"):
                dst.write(chunk)
            else:
                dst.seek(len(chunk), os.SEEK_CUR)
        # A file ending in a hole stays short until the size is set explicitly.
        dst.truncate(total_size)


def _copy_sparse(src: str, dst: str) -> None:
    """shutil.copy2 equivalent that preserves holes.

    Asks the filesystem where the data is instead of reading the whole file
    and looking for zeros. A captured Windows disk is 64 GiB apparent with
    ~12 GiB of real data in a few hundred extents, so scanning it end to end
    costs a 52-second read plus a CPU pass over 52 GiB of zeros -- measured at
    168 seconds for one clone, 85% of a range's whole deploy time.

    Falls back to the scanning copy where SEEK_DATA is unsupported (it needs
    Linux 3.1+ and a filesystem that implements it; some do not).
    """
    size = os.path.getsize(src)
    try:
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            src_fd = fsrc.fileno()
            pos = 0
            while pos < size:
                try:
                    data_start = os.lseek(src_fd, pos, os.SEEK_DATA)
                except OSError:
                    break  # no more data: the rest of the file is a hole
                try:
                    data_end = os.lseek(src_fd, data_start, os.SEEK_HOLE)
                except OSError:
                    data_end = size
                fsrc.seek(data_start)
                fdst.seek(data_start)
                remaining = data_end - data_start
                while remaining > 0:
                    chunk = fsrc.read(min(_SPARSE_CHUNK, remaining))
                    if not chunk:
                        break
                    fdst.write(chunk)
                    remaining -= len(chunk)
                pos = data_end
            # A trailing hole leaves the file short until the size is set.
            fdst.truncate(size)
        shutil.copystat(src, dst)
        return
    except (AttributeError, OSError) as e:
        # SEEK_DATA missing or refused by this filesystem.
        logger.debug(f"SEEK_DATA copy unavailable for {src} ({e}); scanning instead")

    with open(src, "rb") as fsrc:
        _write_sparse(fsrc, dst, size)
    shutil.copystat(src, dst)


def _copytree_sparse(src: str, dst: str) -> None:
    """shutil.copytree equivalent that preserves holes."""
    os.makedirs(dst, exist_ok=True)
    for entry in os.listdir(src):
        s_path = os.path.join(src, entry)
        d_path = os.path.join(dst, entry)
        if os.path.isdir(s_path):
            _copytree_sparse(s_path, d_path)
        elif os.path.isfile(s_path):
            _copy_sparse(s_path, d_path)
    shutil.copystat(src, dst)


class ContainerNotFoundError(Exception):
    """No such container on the target daemon."""


class ResourceUpdateRejectedError(Exception):
    """The daemon refused a live resource change."""


class DockerService:
    """
    Service for managing Docker containers and networks.

    With DinD isolation enabled:
    - Host operations: Uses local Docker daemon (for PROVING GROUND infrastructure)
    - Range operations: Uses Docker daemon inside range's DinD container

    Without DinD isolation (legacy mode):
    - All operations use local Docker daemon directly
    """

    def __init__(self, dind_service: Optional["DinDService"] = None):
        """
        Initialize Docker service.

        Args:
            dind_service: Optional DinD service for range isolation.
                         If None and DinD is enabled, will be created on demand.
        """
        self.client = docker.from_env()
        self._dind_service = dind_service
        self._verify_connection()

    @property
    def dind_service(self) -> "DinDService":
        """Get DinD service, creating lazily if needed."""
        if self._dind_service is None:
            from proving_ground.services.dind_service import get_dind_service

            self._dind_service = get_dind_service()

        return self._dind_service

    async def get_range_client(
        self, range_id: str, docker_url: Optional[str] = None
    ) -> docker.DockerClient:
        """
        Get Docker client for a range's DinD container.

        Returns client connected to the range's inner Docker daemon.

        Args:
            range_id: Range identifier
            docker_url: Optional Docker URL (if known). If not provided,
                       will query DinD service for the URL.

        Returns:
            DockerClient for operating on the range
        """
        if docker_url:
            return self.dind_service.get_range_client(str(range_id), docker_url)

        # Get container info to find Docker URL
        container_info = await self.dind_service.get_container_info(str(range_id))
        if not container_info or not container_info.get("docker_url"):
            raise ValueError(f"Range {range_id} has no active DinD container")

        return self.dind_service.get_range_client(str(range_id), container_info["docker_url"])

    def get_range_client_sync(self, range_id: str, docker_url: str) -> docker.DockerClient:
        """
        Synchronous version of get_range_client (for use when URL is known).

        Args:
            range_id: Range identifier
            docker_url: Docker URL (tcp://ip:port)

        Returns:
            DockerClient for operating on the range
        """
        return self.dind_service.get_range_client(str(range_id), docker_url)

    def _merge_container_config(self, base_config: dict, container_config: Optional[dict]) -> dict:
        """Merge container_config into base host_config, handling lists specially.

        Args:
            base_config: The base host_config dict being built
            container_config: Optional container runtime config from BaseImage

        Returns:
            Merged config dict with lists extended (avoiding duplicates) and scalars overridden
        """
        if not container_config:
            return base_config

        result = base_config.copy()
        list_keys = {"cap_add", "cap_drop", "devices", "security_opt", "dns", "dns_search"}
        # Catalog metadata carried in container_config that is NOT a docker-run
        # param (consumed elsewhere, e.g. console routing) — never pass to Docker.
        skip_keys = {"console_port", "console_scheme"}

        for key, value in container_config.items():
            if key in skip_keys:
                continue
            if key in list_keys and isinstance(value, list):
                # Extend lists, avoid duplicates
                existing = result.get(key, [])
                result[key] = list(dict.fromkeys(existing + value))
            else:
                # Override scalar values
                result[key] = value

        return result

    def _verify_connection(self) -> None:
        """Verify connection to Docker daemon.

        Raises the SDK's own exception rather than a RuntimeError so that a host
        whose socket answers but whose daemon does not ping lands in the same
        app-level refusal as a host with no socket at all. A RuntimeError here
        reached the browser as a bare 500 with nothing in the body.
        """
        try:
            self.client.ping()
            logger.info("Connected to Docker daemon")
        except Exception as e:
            logger.error(f"Failed to connect to Docker daemon: {e}")
            raise DockerException("Cannot connect to Docker daemon") from e

    # Network Operations

    def create_network(
        self,
        name: str,
        subnet: str,
        gateway: str,
        internal: bool = True,
        labels: Optional[Dict[str, str]] = None,
        use_vyos_gateway: bool = True,
    ) -> str:
        """
        Create a Docker network with the specified configuration.

        Args:
            name: Network name
            subnet: CIDR notation (e.g., "10.0.1.0/24")
            gateway: Gateway IP address (used by VyOS, not Docker bridge)
            internal: If True, no external connectivity (isolation)
            labels: Optional labels for the network
            use_vyos_gateway: If True, don't assign gateway to Docker bridge (VyOS will be gateway)

        Returns:
            Network ID
        """
        if use_vyos_gateway:
            # Docker bridge uses .254, leaving .1 available for VyOS
            import ipaddress

            subnet_obj = ipaddress.ip_network(subnet, strict=False)
            hosts = list(subnet_obj.hosts())
            # Use last usable host (.254 for /24) for Docker bridge
            bridge_ip = str(hosts[-1]) if hosts else gateway

            ipam_pool = docker.types.IPAMPool(
                subnet=subnet, gateway=bridge_ip  # Docker bridge uses .254, not .1
            )
        else:
            # Legacy mode: Docker bridge is the gateway
            ipam_pool = docker.types.IPAMPool(subnet=subnet, gateway=gateway)
        ipam_config = docker.types.IPAMConfig(pool_configs=[ipam_pool])

        try:
            network = self.client.networks.create(
                name=name,
                driver="bridge",
                internal=internal,
                ipam=ipam_config,
                labels=labels or {},
                attachable=True,
            )
            logger.info(
                f"Created network: {name} ({network.id[:12]}) [VyOS gateway mode: {use_vyos_gateway}]"
            )
            return network.id
        except APIError as e:
            logger.error(f"Failed to create network {name}: {e}")
            raise

    def delete_network(self, network_id: str) -> bool:
        """Delete a Docker network."""
        try:
            network = self.client.networks.get(network_id)
            network.remove()
            logger.info(f"Deleted network: {network_id[:12]}")
            return True
        except NotFound:
            logger.warning(f"Network not found: {network_id}")
            return False
        except APIError as e:
            logger.error(f"Failed to delete network {network_id}: {e}")
            raise

    def get_network(self, network_id: str) -> Optional[Dict[str, Any]]:
        """Get network information."""
        try:
            network = self.client.networks.get(network_id)
            return {
                "id": network.id,
                "name": network.name,
                "created": network.attrs.get("Created"),
                "scope": network.attrs.get("Scope"),
                "driver": network.attrs.get("Driver"),
                "containers": list(network.attrs.get("Containers", {}).keys()),
            }
        except NotFound:
            return None

    def get_container_logs(self, container_id: str, tail: int = 100) -> list[str]:
        """
        Get last N lines of container logs.

        Args:
            container_id: Docker container ID
            tail: Number of lines to retrieve

        Returns:
            List of log lines with timestamps
        """
        try:
            container = self.client.containers.get(container_id)
            logs = container.logs(tail=tail, timestamps=True).decode("utf-8")
            return logs.strip().split("\n") if logs.strip() else []
        except NotFound:
            return ["Container not found - it may have been removed"]
        except APIError as e:
            return [f"Error fetching logs: {e}"]

    def _connect_to_traefik_network(self, container_id: str) -> None:
        """
        DEPRECATED: VMs should NOT be connected to traefik-routing for security.
        Instead, traefik is connected to range networks via connect_traefik_to_network().

        This method is kept for backwards compatibility but logs a warning.
        """
        logger.warning(
            "_connect_to_traefik_network is deprecated - VMs should not be on management network"
        )
        # Do nothing - VMs should not be on traefik-routing

    def connect_traefik_to_network(self, network_id: str) -> bool:
        """
        Connect the traefik container to a range network.
        This allows traefik to route to VMs on that network without exposing
        the management network to VMs.

        Traefik is assigned .253 in the subnet, leaving .1 available for VyOS
        and .254 for the Docker bridge.

        Args:
            network_id: Docker network ID to connect traefik to

        Returns:
            True if successful, False if traefik not found or already connected
        """
        try:
            import ipaddress

            # Find the traefik container
            traefik_container = None
            for container in self.client.containers.list():
                if "traefik" in container.name.lower():
                    traefik_container = container
                    break

            if not traefik_container:
                logger.warning("Traefik container not found - VNC routing may not work")
                return False

            # Get the network
            network = self.client.networks.get(network_id)

            # Check if traefik is already connected
            connected_containers = network.attrs.get("Containers", {})
            if traefik_container.id in connected_containers:
                logger.debug(f"Traefik already connected to network {network.name}")
                return True

            # Calculate Traefik IP (.253 in the subnet)
            # This leaves .1 for VyOS and .254 for Docker bridge
            ipam_config = network.attrs.get("IPAM", {}).get("Config", [])
            traefik_ip = None
            if ipam_config:
                subnet_str = ipam_config[0].get("Subnet")
                if subnet_str:
                    subnet_obj = ipaddress.ip_network(subnet_str, strict=False)
                    hosts = list(subnet_obj.hosts())
                    if len(hosts) >= 3:
                        # .253 is second-to-last usable host
                        traefik_ip = str(hosts[-2])

            # Connect traefik to the network with specific IP
            if traefik_ip:
                network.connect(traefik_container.id, ipv4_address=traefik_ip)
                logger.info(f"Connected traefik to network {network.name} at {traefik_ip}")
            else:
                network.connect(traefik_container.id)
                logger.info(f"Connected traefik to network {network.name} for VM routing")
            return True

        except NotFound as e:
            logger.warning(f"Network not found when connecting traefik: {e}")
            return False
        except APIError as e:
            logger.warning(f"Failed to connect traefik to network: {e}")
            return False

    def disconnect_traefik_from_network(self, network_id: str) -> bool:
        """
        Disconnect the traefik container from a range network.
        Called during network teardown.

        Args:
            network_id: Docker network ID to disconnect traefik from

        Returns:
            True if successful
        """
        try:
            # Find the traefik container
            traefik_container = None
            for container in self.client.containers.list():
                if "traefik" in container.name.lower():
                    traefik_container = container
                    break

            if not traefik_container:
                return True  # Nothing to disconnect

            # Get the network
            network = self.client.networks.get(network_id)

            # Disconnect traefik from the network
            network.disconnect(traefik_container.id)
            logger.info(f"Disconnected traefik from network {network.name}")
            return True

        except NotFound:
            return True  # Already disconnected
        except APIError as e:
            logger.warning(f"Failed to disconnect traefik from network: {e}")
            return False

    def setup_network_isolation(self, network_id: str, subnet: str) -> bool:
        """
        Set up iptables rules to isolate a range network from the host and PROVING GROUND infrastructure.

        This prevents VMs from:
        - Accessing the Docker host (localhost, host gateway)
        - Accessing PROVING GROUND services (backend, database, traefik-routing network)
        - Accessing other range networks

        Args:
            network_id: Docker network ID (used for rule comments/identification)
            subnet: Network subnet in CIDR notation (e.g., "10.0.1.0/24")

        Returns:
            True if successful
        """
        import subprocess

        try:
            # Get the network to find its bridge interface
            network = self.client.networks.get(network_id)
            network_name = network.name

            # Get host IPs to block (Docker gateway IPs and host interfaces)
            # These are common Docker/host IPs that should be blocked
            blocked_destinations = [
                "172.17.0.0/16",  # Default Docker bridge network
                "172.18.0.0/16",  # Docker networks range
                "172.19.0.0/16",  # Docker networks range
                "172.20.0.0/16",  # Docker networks range
                "127.0.0.0/8",  # Localhost
                "10.0.0.0/8",  # Private networks (except our subnet)
                "192.168.0.0/16",  # Private networks
            ]

            # Create a unique chain for this network
            chain_name = f"PROVING GROUND-{network_id[:12]}"

            # Create the chain (ignore error if exists)
            subprocess.run(["iptables", "-N", chain_name], capture_output=True)

            # Flush existing rules in the chain
            subprocess.run(["iptables", "-F", chain_name], capture_output=True)

            # Add rules to block access to infrastructure
            for dest in blocked_destinations:
                # Skip if destination overlaps with our own subnet
                if self._subnets_overlap(subnet, dest):
                    continue

                subprocess.run(
                    ["iptables", "-A", chain_name, "-s", subnet, "-d", dest, "-j", "DROP"],
                    check=True,
                    capture_output=True,
                )

            # Block access to host's physical interfaces
            # Get host IP addresses
            result = subprocess.run(["hostname", "-I"], capture_output=True, text=True)
            if result.returncode == 0:
                host_ips = result.stdout.strip().split()
                for host_ip in host_ips:
                    if host_ip and not host_ip.startswith(subnet.split("/")[0].rsplit(".", 1)[0]):
                        subprocess.run(
                            [
                                "iptables",
                                "-A",
                                chain_name,
                                "-s",
                                subnet,
                                "-d",
                                f"{host_ip}/32",
                                "-j",
                                "DROP",
                            ],
                            capture_output=True,
                        )

            # Allow traffic within the same subnet (for VM-to-VM communication)
            subprocess.run(
                ["iptables", "-I", chain_name, "1", "-s", subnet, "-d", subnet, "-j", "ACCEPT"],
                check=True,
                capture_output=True,
            )

            # Add jump to our chain from DOCKER-USER (at the beginning)
            # First check if rule already exists
            check_result = subprocess.run(
                ["iptables", "-C", "DOCKER-USER", "-s", subnet, "-j", chain_name],
                capture_output=True,
            )

            if check_result.returncode != 0:
                # Rule doesn't exist, add it
                subprocess.run(
                    ["iptables", "-I", "DOCKER-USER", "1", "-s", subnet, "-j", chain_name],
                    check=True,
                    capture_output=True,
                )

            logger.info(f"Set up network isolation for {network_name} ({subnet})")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to set up network isolation: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to set up network isolation: {e}")
            return False

    def teardown_network_isolation(self, network_id: str, subnet: str) -> bool:
        """
        Remove iptables rules for a range network.

        Args:
            network_id: Docker network ID
            subnet: Network subnet in CIDR notation

        Returns:
            True if successful
        """
        import subprocess

        try:
            chain_name = f"PROVING GROUND-{network_id[:12]}"

            # Remove jump from DOCKER-USER
            subprocess.run(
                ["iptables", "-D", "DOCKER-USER", "-s", subnet, "-j", chain_name],
                capture_output=True,
            )  # Ignore errors if rule doesn't exist

            # Flush and delete the chain
            subprocess.run(["iptables", "-F", chain_name], capture_output=True)
            subprocess.run(["iptables", "-X", chain_name], capture_output=True)

            logger.info(f"Removed network isolation rules for {subnet}")
            return True

        except Exception as e:
            logger.warning(f"Failed to remove network isolation rules: {e}")
            return False

    def _subnets_overlap(self, subnet1: str, subnet2: str) -> bool:
        """Check if two subnets overlap."""
        import ipaddress

        try:
            net1 = ipaddress.ip_network(subnet1, strict=False)
            net2 = ipaddress.ip_network(subnet2, strict=False)
            return net1.overlaps(net2)
        except ValueError:
            return False

    def connect_traefik_to_management_network(self) -> bool:
        """
        Connect the traefik container to the management network.
        This allows traefik to route to VyOS routers and potentially
        route VNC traffic through the management network.

        Returns:
            True if successful
        """
        from proving_ground.config import get_settings

        settings = get_settings()

        try:
            # Find the traefik container
            traefik_container = None
            for container in self.client.containers.list():
                if "traefik" in container.name.lower():
                    traefik_container = container
                    break

            if not traefik_container:
                logger.warning("Traefik container not found")
                return False

            # Get the management network
            network_name = settings.management_network_name
            try:
                networks = self.client.networks.list(names=[network_name])
                mgmt_network = None
                for network in networks:
                    if network.name == network_name:
                        mgmt_network = network
                        break

                if not mgmt_network:
                    logger.warning(f"Management network {network_name} not found")
                    return False
            except NotFound:
                logger.warning(f"Management network {network_name} not found")
                return False

            # Check if traefik is already connected
            connected_containers = mgmt_network.attrs.get("Containers", {})
            if traefik_container.id in connected_containers:
                logger.debug("Traefik already connected to management network")
                return True

            # Connect traefik to the management network
            mgmt_network.connect(traefik_container.id)
            logger.info(f"Connected traefik to management network {network_name}")
            return True

        except APIError as e:
            logger.warning(f"Failed to connect traefik to management network: {e}")
            return False

    # Container Operations (Linux VMs)

    def create_container(
        self,
        name: str,
        image: str,
        network_id: str,
        ip_address: str,
        cpu_limit: int = 2,
        memory_limit_mb: int = 2048,
        volumes: Optional[Dict[str, Dict]] = None,
        environment: Optional[Dict[str, str]] = None,
        labels: Optional[Dict[str, str]] = None,
        privileged: bool = False,
        hostname: Optional[str] = None,
        linux_username: Optional[str] = None,
        linux_password: Optional[str] = None,
        linux_user_sudo: bool = True,
        dns_servers: Optional[str] = None,
        dns_search: Optional[str] = None,
        container_config: Optional[dict] = None,
    ) -> str:
        """
        Create a Docker container for a Linux VM.

        Args:
            name: Container name
            image: Docker image (e.g., "ubuntu:22.04")
            network_id: Network to attach to
            ip_address: Static IP address in the network
            cpu_limit: CPU core limit
            memory_limit_mb: Memory limit in MB
            volumes: Volume bindings
            environment: Environment variables
            labels: Container labels
            privileged: Run in privileged mode
            hostname: Container hostname
            linux_username: Linux username (for KasmVNC/LinuxServer containers)
            linux_password: Linux password (for KasmVNC/LinuxServer containers)
            linux_user_sudo: Grant sudo privileges (for LinuxServer containers)
            dns_servers: Comma-separated DNS servers (e.g., "8.8.8.8,8.8.4.4")
            dns_search: DNS search domain (e.g., "corp.local")
            container_config: Optional container runtime config from BaseImage

        Returns:
            Container ID
        """
        # Pull image if not present
        self._ensure_image(image)

        # Initialize environment dict if not provided
        if environment is None:
            environment = {}

        # Configure user settings based on container image type
        if "kasmweb/" in image:
            # KasmVNC containers - use hardcoded VNC password for seamless auto-login
            # The linux_password field is for the actual OS user, not VNC auth
            environment["VNC_PW"] = "vncpassword"
            # Disable audio services to save memory (~100MB per container)
            environment["KASM_SVC_AUDIO"] = "0"
            environment["KASM_SVC_AUDIO_INPUT"] = "0"
            logger.info(f"Set default VNC password for KasmVNC container {name} (audio disabled)")
        elif "linuxserver/" in image or "lscr.io/linuxserver" in image:
            # LinuxServer containers (webtop, etc.)
            if linux_username:
                environment["CUSTOM_USER"] = linux_username
            if linux_password:
                environment["PASSWORD"] = linux_password
            if linux_user_sudo:
                environment["SUDO_ACCESS"] = "true"
            logger.info(
                f"Set user config for LinuxServer container {name}: user={linux_username}, sudo={linux_user_sudo}"
            )

        # Get Range network for attachment
        # NOTE: VMs are created ONLY on the range network for security.
        # Traefik connects to range networks to route traffic - VMs never see traefik-routing.
        try:
            range_network = self.client.networks.get(network_id)
        except NotFound as exc:
            raise ValueError(f"Network not found: {network_id}") from exc

        # Create container directly on the range network with static IP
        # This ensures VMs cannot access the management network
        networking_config = self.client.api.create_networking_config(
            {range_network.name: self.client.api.create_endpoint_config(ipv4_address=ip_address)}
        )

        # Parse DNS configuration
        dns_list = None
        dns_search_list = None
        if dns_servers:
            dns_list = [s.strip() for s in dns_servers.split(",") if s.strip()]
        else:
            dns_list = ["8.8.8.8", "8.8.4.4"]  # Default external DNS
        if dns_search:
            dns_search_list = [s.strip() for s in dns_search.split(",") if s.strip()]

        # Create container
        try:
            host_config_args = {
                "nano_cpus": int(cpu_limit * 1e9),
                "mem_limit": f"{memory_limit_mb}m",
                "binds": volumes,
                "privileged": privileged,
                "cap_add": ["NET_ADMIN"],  # Required for VyOS gateway routing
                "restart_policy": {"Name": "unless-stopped"},
                "dns": dns_list,
            }
            if dns_search_list:
                host_config_args["dns_search"] = dns_search_list

            # Merge container_config from BaseImage
            host_config_args = self._merge_container_config(host_config_args, container_config)

            container = self.client.api.create_container(
                image=image,
                name=name,
                hostname=hostname or name,
                detach=True,
                tty=True,
                stdin_open=True,
                networking_config=networking_config,
                host_config=self.client.api.create_host_config(**host_config_args),
                environment=environment,
                labels=labels or {},
            )
            container_id = container["Id"]
            logger.info(
                f"Created container: {name} ({container_id[:12]}) on {range_network.name} with IP {ip_address}"
            )

            return container_id
        except APIError as e:
            logger.error(f"Failed to create container {name}: {e}")
            raise

    def create_windows_container(
        self,
        name: str,
        network_id: str,
        ip_address: str,
        cpu_limit: int = 4,
        memory_limit_mb: int = 8192,
        disk_size_gb: int = 64,
        windows_version: str = "11",
        labels: Optional[Dict[str, str]] = None,
        iso_path: Optional[str] = None,
        iso_url: Optional[str] = None,
        storage_path: Optional[str] = None,
        clone_from: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        display_type: str = "desktop",
        # Network configuration
        use_dhcp: bool = False,
        gateway: Optional[str] = None,
        dns_servers: Optional[str] = None,
        dns_search: Optional[str] = None,
        # Extended dockur/windows configuration
        disk2_gb: Optional[int] = None,
        disk3_gb: Optional[int] = None,
        enable_shared_folder: bool = False,
        shared_folder_path: Optional[str] = None,
        enable_global_shared: bool = False,
        global_shared_path: Optional[str] = None,
        language: Optional[str] = None,
        keyboard: Optional[str] = None,
        region: Optional[str] = None,
        manual_install: bool = False,
        oem_script_path: Optional[str] = None,
        # Target architecture (x86_64 or arm64, defaults to host)
        arch: Optional[str] = None,
        # Container runtime config from BaseImage
        container_config: Optional[dict] = None,
    ) -> str:
        """
        Create a Windows VM container using dockur/windows or dockur/windows-arm.

        Supported Windows versions:
        - x86_64 (dockur/windows): 11, 11l, 11e, 10, 10l, 10e, 8e, 2025, 2022, 2019, 2016, 2012, 2008, 7u, vu, xp, 2k, 2003
        - arm64 (dockur/windows-arm): 11, 11pro, 11ent, 11ltsc, 10, 10pro, 10ent, 10ltsc

        Args:
            name: Container name
            network_id: Network to attach to
            ip_address: Static IP address
            cpu_limit: CPU core limit (minimum 4 recommended)
            memory_limit_mb: Memory limit in MB (minimum 4096 recommended)
            disk_size_gb: Virtual disk size in GB
            windows_version: Windows version code (11, 10, 2022, etc.)
            labels: Container labels
            iso_path: Path to local Windows ISO (bind mount, skips download)
            iso_url: URL to custom Windows ISO (remote download)
            storage_path: Path to persistent storage for Windows installation
            clone_from: Path to golden image storage to clone from
            username: Optional Windows username (default: Docker)
            password: Optional Windows password (default: empty)
            display_type: Display mode - 'desktop' (VNC/web console on ports 8006/5900)
                         or 'server' (headless/RDP only)
            use_dhcp: Allow Windows to request IP via DHCP instead of static
            disk2_gb: Size of second disk in GB (appears as D: drive)
            disk3_gb: Size of third disk in GB (appears as E: drive)
            enable_shared_folder: Enable per-VM shared folder mount
            shared_folder_path: Host path for per-VM shared folder
            enable_global_shared: Mount global shared folder (read-only)
            global_shared_path: Host path for global shared folder
            language: Windows display language (e.g., "French", "German")
            keyboard: Keyboard layout (e.g., "en-US", "de-DE")
            region: Regional settings (e.g., "en-US", "fr-FR")
            manual_install: Enable manual/interactive installation mode
            oem_script_path: Path to OEM directory containing install.bat

        Returns:
            Container ID
        """
        import os
        from proving_ground.config import get_settings

        settings = get_settings()

        # Determine target architecture (use specified arch or default to host)
        target_arch = arch or HOST_ARCH

        # Select image based on target architecture
        if target_arch == "arm64":
            image = "dockurr/windows-arm"
        else:
            image = "dockurr/windows"

        self._ensure_image(image)

        try:
            network = self.client.networks.get(network_id)
        except NotFound as exc:
            raise ValueError(f"Network not found: {network_id}") from exc

        # Create networking config
        networking_config = self.client.api.create_networking_config(
            {network.name: self.client.api.create_endpoint_config(ipv4_address=ip_address)}
        )

        # Environment for dockur/windows
        # See: https://github.com/dockur/windows for full documentation
        environment = {
            "VERSION": windows_version,
            "DISK_SIZE": f"{disk_size_gb}G",
            "CPU_CORES": str(cpu_limit),
            "RAM_SIZE": f"{memory_limit_mb}M",
        }

        # Optional username/password for Windows setup
        if username:
            environment["USERNAME"] = username
        if password:
            environment["PASSWORD"] = password

        # Network configuration
        if use_dhcp:
            environment["DHCP"] = "Y"
        else:
            # Static IP configuration - gateway and DNS
            if gateway:
                environment["GATEWAY"] = gateway
            if dns_servers:
                # dockur/windows accepts comma-separated DNS servers
                environment["DNS"] = dns_servers

        # Additional disks
        if disk2_gb:
            environment["DISK2_SIZE"] = f"{disk2_gb}G"
        if disk3_gb:
            environment["DISK3_SIZE"] = f"{disk3_gb}G"

        # Localization
        if language:
            environment["LANGUAGE"] = language
        if keyboard:
            environment["KEYBOARD"] = keyboard
        if region:
            environment["REGION"] = region

        # Manual installation mode
        if manual_install:
            environment["MANUAL"] = "Y"

        # Display type configuration
        # 'desktop' = web VNC console on port 8006 + VNC on port 5900 (default)
        # 'server' = headless mode, RDP only (no VNC/web console)
        if display_type == "server":
            environment["DISPLAY"] = "none"  # Headless mode for server environments
            logger.info(f"Windows VM {name} configured in server mode (RDP only)")
        else:
            environment["DISPLAY"] = "web"  # Web VNC console (default)
            logger.info(f"Windows VM {name} configured in desktop mode (VNC/web console)")

        # Custom ISO URL (dockur downloads from this URL)
        if iso_url:
            environment["BOOT"] = iso_url
            logger.info(f"Using custom ISO URL: {iso_url}")

        # Check if KVM is available for hardware acceleration
        kvm_available = os.path.exists("/dev/kvm")

        # Check if emulation is required (target arch differs from host arch)
        emulated = requires_emulation(target_arch)

        if kvm_available and not emulated:
            environment["KVM"] = "Y"
            logger.info(f"KVM acceleration enabled for Windows VM (native {target_arch})")
        else:
            environment["KVM"] = "N"
            if emulated:
                arch_display = "ARM64" if target_arch == "arm64" else "x86_64"
                host_display = "ARM64" if IS_ARM else "x86_64"
                logger.warning(
                    f"Windows VM '{name}' targets {arch_display} but host is {host_display}. "
                    "Running via emulation. Expect significantly slower performance (10-20x)."
                )
            else:
                logger.warning("KVM not available, Windows VM will run in software emulation mode")

        # Setup volume bindings
        binds = []

        # Check for local ISO bind mount (takes priority over URL)
        # Use os.path.isfile() to ensure it's a file, not a directory
        if iso_path and os.path.isfile(iso_path):
            binds.append(f"{iso_path}:/boot.iso:ro")
            logger.info(f"Using local ISO: {iso_path}")
        elif not iso_url:
            # Check for default ISO in cache directory (only if no URL provided)
            windows_iso_dir = os.path.join(settings.iso_cache_dir, "windows-isos")
            cached_iso = os.path.join(windows_iso_dir, f"windows-{windows_version}.iso")
            if os.path.isfile(cached_iso):
                binds.append(f"{cached_iso}:/boot.iso:ro")
                logger.info(f"Using cached ISO: {cached_iso}")

        # Setup persistent storage
        if storage_path:
            os.makedirs(storage_path, exist_ok=True)

            # Clone from golden image if specified
            if clone_from and os.path.exists(clone_from):
                if not os.listdir(storage_path):  # Only clone if empty
                    logger.info(f"Cloning golden image from {clone_from} to {storage_path}")
                    for item in os.listdir(clone_from):
                        src = os.path.join(clone_from, item)
                        dst = os.path.join(storage_path, item)
                        # Sparse-aware: a plain copytree/copy2 inflates a
                        # 9 GB golden image to its full 64 GB apparent size for
                        # every range cloned from it.
                        if os.path.isdir(src):
                            _copytree_sparse(src, dst)
                        else:
                            _copy_sparse(src, dst)

            binds.append(f"{storage_path}:/storage")
            logger.info(f"Using persistent storage: {storage_path}")

            # Additional disk storage (uses same parent directory)
            if disk2_gb:
                storage2_path = os.path.join(os.path.dirname(storage_path), "storage2")
                os.makedirs(storage2_path, exist_ok=True)
                binds.append(f"{storage2_path}:/storage2")
                logger.info(f"Using secondary storage: {storage2_path}")

            if disk3_gb:
                storage3_path = os.path.join(os.path.dirname(storage_path), "storage3")
                os.makedirs(storage3_path, exist_ok=True)
                binds.append(f"{storage3_path}:/storage3")
                logger.info(f"Using tertiary storage: {storage3_path}")

        # Shared folder (per-VM)
        if enable_shared_folder and shared_folder_path:
            os.makedirs(shared_folder_path, exist_ok=True)
            binds.append(f"{shared_folder_path}:/shared")
            logger.info(f"Using per-VM shared folder: {shared_folder_path}")

        # Global shared folder (read-only for safety)
        if enable_global_shared and global_shared_path:
            os.makedirs(global_shared_path, exist_ok=True)
            binds.append(f"{global_shared_path}:/global:ro")
            logger.info(f"Using global shared folder: {global_shared_path}")

        # Post-install script from template config_script (OEM directory)
        if oem_script_path and os.path.exists(oem_script_path):
            binds.append(f"{oem_script_path}:/oem:ro")
            logger.info(f"Using OEM script directory: {oem_script_path}")

        # Parse DNS configuration for Docker container
        dns_list = None
        dns_search_list = None
        if dns_servers:
            dns_list = [s.strip() for s in dns_servers.split(",") if s.strip()]
        else:
            dns_list = ["8.8.8.8", "8.8.4.4"]  # Default external DNS
        if dns_search:
            dns_search_list = [s.strip() for s in dns_search.split(",") if s.strip()]

        # Windows containers need privileged mode for KVM
        try:
            host_config_args = {
                "nano_cpus": int(cpu_limit * 1e9),
                "mem_limit": f"{memory_limit_mb}m",
                "privileged": True,
                "cap_add": ["NET_ADMIN"],
                "restart_policy": {"Name": "unless-stopped"},
                "dns": dns_list,
            }
            if dns_search_list:
                host_config_args["dns_search"] = dns_search_list
            if kvm_available:
                host_config_args["devices"] = ["/dev/kvm:/dev/kvm"]
            if binds:
                host_config_args["binds"] = binds

            # Merge container_config from BaseImage
            host_config_args = self._merge_container_config(host_config_args, container_config)

            container = self.client.api.create_container(
                image=image,
                name=name,
                hostname=name,
                detach=True,
                tty=True,
                stdin_open=True,
                networking_config=networking_config,
                host_config=self.client.api.create_host_config(**host_config_args),
                environment=environment,
                labels=labels or {},
            )
            container_id = container["Id"]
            logger.info(f"Created Windows container: {name} ({container_id[:12]}) on range network")
            # NOTE: No traefik-routing connection - traefik connects to range networks for routing

            return container_id
        except APIError as e:
            logger.error(f"Failed to create Windows container {name}: {e}")
            raise

    def create_macos_container(
        self,
        name: str,
        network_id: str,
        ip_address: str,
        cpu_limit: int = 4,
        memory_limit_mb: int = 8192,
        disk_size_gb: int = 64,
        macos_version: str = "sequoia",
        labels: Optional[Dict[str, str]] = None,
        storage_path: Optional[str] = None,
        display_type: str = "desktop",
        container_config: Optional[dict] = None,
    ) -> str:
        """
        Create a macOS VM container using dockur/macos.

        Supported macOS versions:
        - sequoia (macOS 15 - latest)
        - sonoma (macOS 14)
        - ventura (macOS 13)
        - monterey (macOS 12)
        - big-sur (macOS 11)
        - catalina (macOS 10.15)
        - mojave (macOS 10.14)
        - high-sierra (macOS 10.13)

        Note: macOS VMs require KVM and only work on x86_64 hosts.
        ARM (Apple Silicon) macOS is not supported via dockur/macos.

        Args:
            name: Container name
            network_id: Network to attach to
            ip_address: Static IP address
            cpu_limit: CPU core limit (minimum 4 recommended)
            memory_limit_mb: Memory limit in MB (minimum 8192 recommended)
            disk_size_gb: Virtual disk size in GB
            macos_version: macOS version code
            labels: Container labels
            storage_path: Path to persistent storage for macOS installation
            display_type: Display mode - 'desktop' (VNC/web console)

        Returns:
            Container ID
        """
        import os
        from proving_ground.config import get_settings

        get_settings()

        # macOS via dockur/macos only works on x86_64 hosts
        if HOST_ARCH == "arm64":
            raise ValueError(
                "macOS VMs are only supported on x86_64 hosts. "
                "ARM hosts cannot run macOS VMs via dockur/macos."
            )

        image = "dockurr/macos"
        self._ensure_image(image)

        try:
            network = self.client.networks.get(network_id)
        except NotFound as exc:
            raise ValueError(f"Network not found: {network_id}") from exc

        # Create networking config
        networking_config = self.client.api.create_networking_config(
            {network.name: self.client.api.create_endpoint_config(ipv4_address=ip_address)}
        )

        # Environment for dockur/macos
        # See: https://github.com/dockur/macos for full documentation
        environment = {
            "VERSION": macos_version,
            "DISK_SIZE": f"{disk_size_gb}G",
            "CPU_CORES": str(cpu_limit),
            "RAM_SIZE": f"{memory_limit_mb}M",
        }

        # Display type - macOS always uses web VNC
        environment["DISPLAY"] = "web"
        logger.info(f"macOS VM {name} configured with web VNC console")

        # Check if KVM is available for hardware acceleration
        kvm_available = os.path.exists("/dev/kvm")
        if kvm_available:
            environment["KVM"] = "Y"
            logger.info("KVM acceleration enabled for macOS VM")
        else:
            environment["KVM"] = "N"
            logger.warning(
                "KVM not available, macOS VM will run in software emulation mode. "
                "Performance will be severely degraded."
            )

        # Setup volume bindings
        binds = []

        # Setup persistent storage
        if storage_path:
            os.makedirs(storage_path, exist_ok=True)
            binds.append(f"{storage_path}:/storage")
            logger.info(f"Using persistent storage: {storage_path}")

        # Parse DNS configuration
        dns_list = ["8.8.8.8", "8.8.4.4"]  # Default external DNS

        # macOS containers need privileged mode for KVM
        try:
            host_config_args = {
                "nano_cpus": int(cpu_limit * 1e9),
                "mem_limit": f"{memory_limit_mb}m",
                "privileged": True,
                "cap_add": ["NET_ADMIN"],
                "restart_policy": {"Name": "unless-stopped"},
                "dns": dns_list,
            }
            if kvm_available:
                host_config_args["devices"] = ["/dev/kvm:/dev/kvm"]
            if binds:
                host_config_args["binds"] = binds

            # Merge container_config from BaseImage
            host_config_args = self._merge_container_config(host_config_args, container_config)

            container = self.client.api.create_container(
                image=image,
                name=name,
                hostname=name,
                detach=True,
                tty=True,
                stdin_open=True,
                networking_config=networking_config,
                host_config=self.client.api.create_host_config(**host_config_args),
                environment=environment,
                labels=labels or {},
            )
            container_id = container["Id"]
            logger.info(f"Created macOS container: {name} ({container_id[:12]}) on range network")

            return container_id
        except APIError as e:
            logger.error(f"Failed to create macOS container {name}: {e}")
            raise

    def create_linux_vm_container(
        self,
        name: str,
        network_id: str,
        ip_address: str,
        cpu_limit: int = 2,
        memory_limit_mb: int = 2048,
        disk_size_gb: int = 64,
        linux_distro: str = "ubuntu",
        labels: Optional[Dict[str, str]] = None,
        iso_path: Optional[str] = None,
        iso_url: Optional[str] = None,
        storage_path: Optional[str] = None,
        clone_from: Optional[str] = None,
        display_type: str = "desktop",
        # Network configuration (for reference - requires manual config in VM)
        gateway: Optional[str] = None,
        dns_servers: Optional[str] = None,
        dns_search: Optional[str] = None,
        # Extended qemux/qemu configuration
        boot_mode: str = "uefi",
        disk_type: str = "scsi",
        disk2_gb: Optional[int] = None,
        disk3_gb: Optional[int] = None,
        enable_shared_folder: bool = False,
        shared_folder_path: Optional[str] = None,
        enable_global_shared: bool = False,
        global_shared_path: Optional[str] = None,
        # Linux user configuration (for cloud-init)
        linux_username: Optional[str] = None,
        linux_password: Optional[str] = None,
        linux_user_sudo: bool = True,
        # Target architecture (x86_64 or arm64, defaults to host)
        arch: Optional[str] = None,
        # Container runtime config from BaseImage
        container_config: Optional[dict] = None,
    ) -> str:
        """
        Create a Linux VM container using qemux/qemu.

        Provides full Linux desktop/server VMs with KVM acceleration and web VNC console,
        mirroring the dockur/windows approach for Windows VMs.

        Supported Linux distributions (via BOOT env var):
        Desktop: ubuntu, debian, fedora, alpine, arch, manjaro, opensuse, mint,
                 zorin, elementary, popos, kali, parrot, tails, rocky, alma
        Server: Any of the above work in server mode, or use custom ISO

        See: https://github.com/qemux/qemu for full documentation

        Args:
            name: Container name
            network_id: Network to attach to
            ip_address: Static IP address
            cpu_limit: CPU core limit (default 2)
            memory_limit_mb: Memory limit in MB (default 2048)
            disk_size_gb: Virtual disk size in GB
            linux_distro: Linux distribution to boot (ubuntu, debian, fedora, etc.)
            labels: Container labels
            iso_path: Path to local Linux ISO (bind mount, skips download)
            iso_url: URL to custom Linux ISO (remote download)
            storage_path: Path to persistent storage for Linux installation
            clone_from: Path to golden image storage to clone from
            display_type: Display mode - 'desktop' (VNC/web console on port 8006)
                         or 'server' (headless mode)
            boot_mode: Boot mode - 'uefi' (default) or 'legacy' (BIOS)
            disk_type: Disk interface - 'scsi' (default), 'blk', or 'ide'
            disk2_gb: Size of second disk in GB
            disk3_gb: Size of third disk in GB
            enable_shared_folder: Enable per-VM shared folder mount (via 9pfs)
            shared_folder_path: Host path for per-VM shared folder
            enable_global_shared: Mount global shared folder (read-only)
            global_shared_path: Host path for global shared folder

        Returns:
            Container ID
        """
        import os
        from proving_ground.config import get_settings

        settings = get_settings()

        image = "qemux/qemu"
        self._ensure_image(image)

        try:
            network = self.client.networks.get(network_id)
        except NotFound as exc:
            raise ValueError(f"Network not found: {network_id}") from exc

        # Create networking config
        networking_config = self.client.api.create_networking_config(
            {network.name: self.client.api.create_endpoint_config(ipv4_address=ip_address)}
        )

        # Environment for qemux/qemu
        # See: https://github.com/qemux/qemu for full documentation
        environment = {
            "BOOT": linux_distro,
            "DISK_SIZE": f"{disk_size_gb}G",
            "CPU_CORES": str(cpu_limit),
            "RAM_SIZE": f"{memory_limit_mb}M",
            "BOOT_MODE": boot_mode.upper(),
            "DISK_TYPE": disk_type,
        }

        # Custom ISO URL (qemu downloads from this URL)
        if iso_url:
            environment["BOOT"] = iso_url
            logger.info(f"Using custom ISO URL: {iso_url}")

        # Additional disks
        if disk2_gb:
            environment["DISK2_SIZE"] = f"{disk2_gb}G"
        if disk3_gb:
            environment["DISK3_SIZE"] = f"{disk3_gb}G"

        # Display type configuration
        # 'desktop' = web VNC console on port 8006 (default)
        # 'server' = headless mode, SSH/console only
        if display_type == "server":
            environment["DISPLAY"] = "none"  # Headless mode for server environments
            logger.info(f"Linux VM {name} configured in server mode (headless)")
        else:
            environment["DISPLAY"] = "web"  # Web VNC console (default)
            logger.info(f"Linux VM {name} configured in desktop mode (VNC/web console)")

        # Check if KVM is available for hardware acceleration
        kvm_available = os.path.exists("/dev/kvm")

        # Determine target architecture (use specified arch or default to host)
        target_arch = arch or HOST_ARCH

        # Determine if emulation is needed
        # Emulation required when target arch differs from host arch
        emulated = requires_emulation(target_arch)

        if kvm_available:
            if emulated:
                logger.warning(
                    f"Linux VM '{name}' ({linux_distro}) will run {target_arch} via emulation on {HOST_ARCH} host. "
                    "Expect significantly slower performance (10-20x)."
                )
            else:
                logger.info(f"KVM acceleration enabled for Linux VM (native {target_arch})")
        else:
            logger.warning("KVM not available, Linux VM will run in software emulation mode")

        # Setup volume bindings
        binds = []

        # Check for local ISO bind mount (takes priority over URL)
        if iso_path and os.path.isfile(iso_path):
            binds.append(f"{iso_path}:/boot.iso:ro")
            logger.info(f"Using local ISO: {iso_path}")
        elif not iso_url:
            # Check for default ISO in cache directory (only if no URL provided)
            # Try architecture-specific filename first, then legacy format
            linux_iso_dir = os.path.join(settings.iso_cache_dir, "linux-isos")

            # Priority 1: Architecture-specific filename for target arch (e.g., linux-kali-arm64.iso)
            arch_specific_iso = os.path.join(
                linux_iso_dir, f"linux-{linux_distro}-{target_arch}.iso"
            )
            # Priority 2: Legacy filename (e.g., linux-kali.iso)
            legacy_iso = os.path.join(linux_iso_dir, f"linux-{linux_distro}.iso")

            cached_iso = None
            if os.path.isfile(arch_specific_iso):
                cached_iso = arch_specific_iso
                logger.info(f"Using cached ISO (arch-specific {target_arch}): {cached_iso}")
            elif os.path.isfile(legacy_iso):
                cached_iso = legacy_iso
                logger.info(f"Using cached ISO (legacy): {cached_iso}")

            if cached_iso:
                binds.append(f"{cached_iso}:/boot.iso:ro")

        # Setup persistent storage
        if storage_path:
            os.makedirs(storage_path, exist_ok=True)

            # Clone from golden image if specified
            if clone_from and os.path.exists(clone_from):
                if not os.listdir(storage_path):  # Only clone if empty
                    logger.info(f"Cloning golden image from {clone_from} to {storage_path}")
                    for item in os.listdir(clone_from):
                        src = os.path.join(clone_from, item)
                        dst = os.path.join(storage_path, item)
                        # Sparse-aware: a plain copytree/copy2 inflates a
                        # 9 GB golden image to its full 64 GB apparent size for
                        # every range cloned from it.
                        if os.path.isdir(src):
                            _copytree_sparse(src, dst)
                        else:
                            _copy_sparse(src, dst)

            binds.append(f"{storage_path}:/storage")
            logger.info(f"Using persistent storage: {storage_path}")

            # Additional disk storage
            if disk2_gb:
                storage2_path = os.path.join(os.path.dirname(storage_path), "storage2")
                os.makedirs(storage2_path, exist_ok=True)
                binds.append(f"{storage2_path}:/storage2")
                logger.info(f"Using secondary storage: {storage2_path}")

            if disk3_gb:
                storage3_path = os.path.join(os.path.dirname(storage_path), "storage3")
                os.makedirs(storage3_path, exist_ok=True)
                binds.append(f"{storage3_path}:/storage3")
                logger.info(f"Using tertiary storage: {storage3_path}")

        # Shared folder (per-VM) via 9pfs
        if enable_shared_folder and shared_folder_path:
            os.makedirs(shared_folder_path, exist_ok=True)
            binds.append(f"{shared_folder_path}:/shared")
            logger.info(f"Using per-VM shared folder: {shared_folder_path}")

        # Global shared folder (read-only for safety)
        if enable_global_shared and global_shared_path:
            os.makedirs(global_shared_path, exist_ok=True)
            binds.append(f"{global_shared_path}:/global:ro")
            logger.info(f"Using global shared folder: {global_shared_path}")

        # Cloud-init user configuration
        # Creates a seed ISO with user-data for automatic user setup during Linux installation
        if linux_username and linux_password and storage_path:
            import subprocess
            import crypt

            cloud_init_dir = os.path.join(storage_path, "cloud-init")
            os.makedirs(cloud_init_dir, exist_ok=True)

            # Generate password hash for security
            password_hash = crypt.crypt(linux_password, crypt.mksalt(crypt.METHOD_SHA512))

            # Create user-data file for cloud-init
            sudo_config = "sudo: ALL=(ALL) NOPASSWD:ALL" if linux_user_sudo else ""
            groups_config = "sudo,adm,cdrom,plugdev" if linux_user_sudo else "cdrom,plugdev"

            user_data = f"""#cloud-config
users:
  - name: {linux_username}
    hashed_passwd: {password_hash}
    lock_passwd: false
    shell: /bin/bash
    {sudo_config}
    groups: {groups_config}

hostname: {name}

# Disable cloud-init after first run
runcmd:
  - touch /etc/cloud/cloud-init.disabled
"""
            user_data_path = os.path.join(cloud_init_dir, "user-data")
            with open(user_data_path, "w") as f:
                f.write(user_data)

            # Create meta-data file
            meta_data = f"""instance-id: {name}
local-hostname: {name}
"""
            meta_data_path = os.path.join(cloud_init_dir, "meta-data")
            with open(meta_data_path, "w") as f:
                f.write(meta_data)

            # Generate cloud-init seed ISO
            seed_iso_path = os.path.join(cloud_init_dir, "seed.iso")
            try:
                subprocess.run(
                    [
                        "genisoimage",
                        "-output",
                        seed_iso_path,
                        "-volid",
                        "cidata",
                        "-joliet",
                        "-rock",
                        user_data_path,
                        meta_data_path,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                # Mount cloud-init ISO for the VM
                binds.append(f"{seed_iso_path}:/cloud-init.iso:ro")
                # Add QEMU argument to attach cloud-init ISO as a CD-ROM drive
                if "ARGUMENTS" in environment:
                    environment["ARGUMENTS"] += " -cdrom /cloud-init.iso"
                else:
                    environment["ARGUMENTS"] = "-cdrom /cloud-init.iso"
                logger.info(f"Created cloud-init configuration for user: {linux_username}")
            except subprocess.CalledProcessError as e:
                logger.warning(
                    f"Failed to create cloud-init ISO: {e.stderr}. User will need manual setup."
                )
            except FileNotFoundError:
                logger.warning(
                    "genisoimage not found. Cloud-init ISO creation skipped. User will need manual setup."
                )

        # Parse DNS configuration for Docker container
        dns_list = None
        dns_search_list = None
        if dns_servers:
            dns_list = [s.strip() for s in dns_servers.split(",") if s.strip()]
        else:
            dns_list = ["8.8.8.8", "8.8.4.4"]  # Default external DNS
        if dns_search:
            dns_search_list = [s.strip() for s in dns_search.split(",") if s.strip()]

        # Linux VM containers need privileged mode for KVM
        try:
            host_config_args = {
                "nano_cpus": int(cpu_limit * 1e9),
                "mem_limit": f"{memory_limit_mb}m",
                "privileged": True,
                "cap_add": ["NET_ADMIN"],
                "restart_policy": {"Name": "unless-stopped"},
                "dns": dns_list,
            }
            if dns_search_list:
                host_config_args["dns_search"] = dns_search_list
            if kvm_available:
                host_config_args["devices"] = ["/dev/kvm:/dev/kvm"]
            if binds:
                host_config_args["binds"] = binds

            # Merge container_config from BaseImage
            host_config_args = self._merge_container_config(host_config_args, container_config)

            container = self.client.api.create_container(
                image=image,
                name=name,
                hostname=name,
                detach=True,
                tty=True,
                stdin_open=True,
                networking_config=networking_config,
                host_config=self.client.api.create_host_config(**host_config_args),
                environment=environment,
                labels=labels or {},
            )
            container_id = container["Id"]
            logger.info(
                f"Created Linux VM container: {name} ({container_id[:12]}) on range network"
            )
            # NOTE: No traefik-routing connection - traefik connects to range networks for routing

            return container_id
        except APIError as e:
            logger.error(f"Failed to create Linux VM container {name}: {e}")
            raise

    def start_container(self, container_id: str) -> bool:
        """Start a container."""
        try:
            self.client.api.start(container_id)
            logger.info(f"Started container: {container_id[:12]}")
            return True
        except NotFound:
            logger.warning(f"Container not found: {container_id}")
            return False
        except APIError as e:
            logger.error(f"Failed to start container {container_id}: {e}")
            raise

    def stop_container(self, container_id: str, timeout: int = 10) -> bool:
        """Stop a container."""
        try:
            self.client.api.stop(container_id, timeout=timeout)
            logger.info(f"Stopped container: {container_id[:12]}")
            return True
        except NotFound:
            logger.warning(f"Container not found: {container_id}")
            return False
        except APIError as e:
            logger.error(f"Failed to stop container {container_id}: {e}")
            raise

    def restart_container(self, container_id: str, timeout: int = 10) -> bool:
        """Restart a container."""
        try:
            self.client.api.restart(container_id, timeout=timeout)
            logger.info(f"Restarted container: {container_id[:12]}")
            return True
        except NotFound:
            logger.warning(f"Container not found: {container_id}")
            return False
        except APIError as e:
            logger.error(f"Failed to restart container {container_id}: {e}")
            raise

    def remove_container(self, container_id: str, force: bool = True) -> bool:
        """Remove a container."""
        try:
            self.client.api.remove_container(container_id, force=force, v=True)
            logger.info(f"Removed container: {container_id[:12]}")
            return True
        except NotFound:
            logger.warning(f"Container not found: {container_id}")
            return False
        except APIError as e:
            logger.error(f"Failed to remove container {container_id}: {e}")
            raise

    def get_container_status(self, container_id: str) -> Optional[str]:
        """Get container status."""
        try:
            container = self.client.containers.get(container_id)
            return container.status
        except NotFound:
            return None

    def get_container_info(self, container_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed container information."""
        try:
            container = self.client.containers.get(container_id)
            return {
                "id": container.id,
                "name": container.name,
                "status": container.status,
                "image": container.image.tags[0] if container.image.tags else None,
                "created": container.attrs.get("Created"),
                "ports": container.ports,
                "labels": container.labels,
            }
        except NotFound:
            return None

    def get_container_networks(self, container_id: str) -> Optional[List[Dict[str, Any]]]:
        """
        Get all network interfaces for a container.

        Returns:
            List of network interface dicts with:
            - network_id: Docker network ID
            - network_name: Network name
            - ip_address: IP address on this network
            - mac_address: MAC address
            - gateway: Gateway IP (if available)
            - is_management: True if this is the traefik-routing (management) network
        """
        try:
            container = self.client.containers.get(container_id)
            networks_settings = container.attrs.get("NetworkSettings", {}).get("Networks", {})

            interfaces = []
            for net_name, net_config in networks_settings.items():
                interface = {
                    "network_id": net_config.get("NetworkID", ""),
                    "network_name": net_name,
                    "ip_address": net_config.get("IPAddress", ""),
                    "mac_address": net_config.get("MacAddress", ""),
                    "gateway": net_config.get("Gateway", ""),
                    "is_management": net_name == "traefik-routing",
                }
                interfaces.append(interface)

            return interfaces
        except NotFound:
            return None
        except APIError as e:
            logger.error(f"Failed to get network interfaces for container {container_id}: {e}")
            return None

    def connect_container_to_network(
        self, container_id: str, network_id: str, ip_address: Optional[str] = None
    ) -> bool:
        """
        Connect a container to an additional network.

        Args:
            container_id: Container ID
            network_id: Docker network ID to connect to
            ip_address: Optional static IP address

        Returns:
            True if successful
        """
        try:
            network = self.client.networks.get(network_id)
            if ip_address:
                network.connect(container_id, ipv4_address=ip_address)
            else:
                network.connect(container_id)
            logger.info(
                f"Connected container {container_id[:12]} to network {network.name} with IP {ip_address or 'DHCP'}"
            )
            return True
        except NotFound as e:
            logger.error(f"Container or network not found: {e}")
            return False
        except APIError as e:
            logger.error(f"Failed to connect container to network: {e}")
            raise

    def disconnect_container_from_network(self, container_id: str, network_id: str) -> bool:
        """
        Disconnect a container from a network.

        Args:
            container_id: Container ID
            network_id: Docker network ID to disconnect from

        Returns:
            True if successful
        """
        try:
            network = self.client.networks.get(network_id)
            network.disconnect(container_id)
            logger.info(f"Disconnected container {container_id[:12]} from network {network.name}")
            return True
        except NotFound as e:
            logger.warning(f"Container or network not found: {e}")
            return False
        except APIError as e:
            logger.error(f"Failed to disconnect container from network: {e}")
            raise

    def get_container_stats(self, container_id: str) -> Optional[Dict[str, Any]]:
        """
        Get real-time resource statistics for a container.

        Returns:
            Dict with cpu_percent, memory_mb, memory_limit_mb, network_rx_bytes, network_tx_bytes
        """
        try:
            container = self.client.containers.get(container_id)
            if container.status != "running":
                return None

            stats = container.stats(stream=False)

            # CPU calculation
            cpu_delta = (
                stats["cpu_stats"]["cpu_usage"]["total_usage"]
                - stats["precpu_stats"]["cpu_usage"]["total_usage"]
            )
            system_delta = (
                stats["cpu_stats"]["system_cpu_usage"] - stats["precpu_stats"]["system_cpu_usage"]
            )
            # Normalize to 0-100% (average across all cores) instead of 0-N*100%
            cpu_percent = (cpu_delta / system_delta) * 100.0 if system_delta > 0 else 0.0

            # Memory
            memory_usage = stats["memory_stats"].get("usage", 0)
            memory_limit = stats["memory_stats"].get("limit", 0)
            memory_mb = memory_usage / (1024 * 1024)
            memory_limit_mb = memory_limit / (1024 * 1024)

            # Network
            network_stats = stats.get("networks", {})
            rx_bytes = sum(n.get("rx_bytes", 0) for n in network_stats.values())
            tx_bytes = sum(n.get("tx_bytes", 0) for n in network_stats.values())

            return {
                "cpu_percent": round(cpu_percent, 2),
                "memory_mb": round(memory_mb, 2),
                "memory_limit_mb": round(memory_limit_mb, 2),
                "memory_percent": (
                    round((memory_usage / memory_limit) * 100, 2) if memory_limit > 0 else 0
                ),
                "network_rx_bytes": rx_bytes,
                "network_tx_bytes": tx_bytes,
            }
        except NotFound:
            return None
        except (KeyError, ZeroDivisionError) as e:
            logger.warning(f"Failed to calculate stats for {container_id}: {e}")
            return None

    def exec_command(
        self,
        container_id: str,
        command: str,
        user: str = "root",
        workdir: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
    ) -> tuple[int, str]:
        """
        Execute a command in a running container.

        Returns:
            Tuple of (exit_code, output)
        """
        try:
            container = self.client.containers.get(container_id)
            exec_result = container.exec_run(
                command, user=user, workdir=workdir, environment=environment, demux=True
            )
            stdout = exec_result.output[0] or b""
            stderr = exec_result.output[1] or b""
            output = (stdout + stderr).decode("utf-8", errors="replace")
            return exec_result.exit_code, output
        except NotFound as exc:
            raise ValueError(f"Container not found: {container_id}") from exc
        except APIError as e:
            logger.error(f"Failed to exec in container {container_id}: {e}")
            raise

    def configure_default_route(self, container_id: str, gateway_ip: str) -> bool:
        """
        Configure the default route in a container to use VyOS as gateway.

        This is needed because Docker networks are created without a gateway
        (VyOS is the actual gateway), so containers don't have a default route.

        Args:
            container_id: Container ID
            gateway_ip: Gateway IP address (VyOS router)

        Returns:
            True if successful
        """
        try:
            # First check if a default route already exists
            exit_code, output = self.exec_command(container_id, "ip route show default")

            if exit_code == 0 and "default" in output:
                # Delete existing default route
                self.exec_command(container_id, "ip route del default")

            # Add default route via VyOS gateway
            exit_code, output = self.exec_command(
                container_id, f"ip route add default via {gateway_ip}"
            )

            if exit_code != 0:
                logger.warning(
                    f"Failed to set default route in container {container_id[:12]}: {output}"
                )
                return False

            logger.info(
                f"Configured default route via {gateway_ip} in container {container_id[:12]}"
            )
            return True
        except Exception as e:
            logger.warning(
                f"Failed to configure default route in container {container_id[:12]}: {e}"
            )
            return False

    def set_linux_user_password(self, container_id: str, username: str, password: str) -> bool:
        """
        Set the password for a Linux user in a running container.
        Uses chpasswd to change the password.

        Args:
            container_id: The container ID
            username: The Linux username (e.g., 'kasm-user')
            password: The new password

        Returns:
            True if successful, False otherwise
        """
        try:
            # Use chpasswd to set the password
            command = f'echo "{username}:{password}" | chpasswd'
            exit_code, output = self.exec_command(
                container_id, f"/bin/sh -c '{command}'", user="root"
            )
            if exit_code == 0:
                logger.info(f"Set password for user {username} in container {container_id[:12]}")
                return True
            else:
                logger.warning(f"Failed to set password for {username}: {output}")
                return False
        except Exception as e:
            logger.warning(f"Failed to set password for {username} in {container_id[:12]}: {e}")
            return False

    def grant_sudo_privileges(
        self, container_id: str, username: str, nopasswd: bool = False
    ) -> bool:
        """
        Grant sudo privileges to a Linux user in a running container.

        Args:
            container_id: The container ID
            username: The Linux username (e.g., 'kasm-user')
            nopasswd: If True, allows sudo without password. Default False requires password.

        Returns:
            True if successful, False otherwise
        """
        try:
            if nopasswd:
                sudoers_line = f"{username} ALL=(ALL) NOPASSWD: ALL"
            else:
                sudoers_line = f"{username} ALL=(ALL) ALL"

            # Add user to sudoers file
            command = f'echo "{sudoers_line}" >> /etc/sudoers'
            exit_code, output = self.exec_command(
                container_id, f"/bin/sh -c '{command}'", user="root"
            )
            if exit_code == 0:
                logger.info(
                    f"Granted sudo privileges to {username} in container {container_id[:12]}"
                )
                return True
            else:
                logger.warning(f"Failed to grant sudo to {username}: {output}")
                return False
        except Exception as e:
            logger.warning(f"Failed to grant sudo to {username} in {container_id[:12]}: {e}")
            return False

    def copy_to_container(self, container_id: str, src_path: str, dest_path: str) -> bool:
        """Copy a file or directory to a container."""
        import tarfile
        import io
        import os

        try:
            # Create tar archive
            tar_stream = io.BytesIO()
            with tarfile.open(fileobj=tar_stream, mode="w") as tar:
                tar.add(src_path, arcname=os.path.basename(src_path))
            tar_stream.seek(0)

            # Put archive into container
            self.client.api.put_archive(container_id, dest_path, tar_stream)
            logger.info(f"Copied {src_path} to {container_id[:12]}:{dest_path}")
            return True
        except (NotFound, APIError) as e:
            logger.error(f"Failed to copy to container: {e}")
            raise

    # Snapshot Operations

    def create_snapshot(
        self, container_id: str, snapshot_name: str, labels: Optional[Dict[str, str]] = None
    ) -> str:
        """
        Create a snapshot (Docker image) from a container.

        Returns:
            Image ID
        """
        try:
            container = self.client.containers.get(container_id)
            image = container.commit(
                repository=snapshot_name,
                tag="latest",
                message=f"Snapshot of {container.name}",
                conf={"Labels": labels or {}},
            )
            logger.info(f"Created snapshot: {snapshot_name} ({image.id[:12]})")
            return image.id
        except NotFound as exc:
            raise ValueError(f"Container not found: {container_id}") from exc
        except APIError as e:
            logger.error(f"Failed to create snapshot: {e}")
            raise

    def restore_snapshot(
        self, image_id: str, container_name: str, network_id: str, ip_address: str, **kwargs
    ) -> str:
        """Restore a container from a snapshot image."""
        try:
            image = self.client.images.get(image_id)
        except NotFound as exc:
            raise ValueError(f"Image not found: {image_id}") from exc

        return self.create_container(
            name=container_name,
            image=image.tags[0] if image.tags else image.id,
            network_id=network_id,
            ip_address=ip_address,
            **kwargs,
        )

    def delete_snapshot(self, image_id: str) -> bool:
        """Delete a snapshot image."""
        try:
            self.client.images.remove(image_id, force=True)
            logger.info(f"Deleted snapshot: {image_id[:12]}")
            return True
        except NotFound:
            logger.warning(f"Image not found: {image_id}")
            return False
        except APIError as e:
            logger.error(f"Failed to delete snapshot: {e}")
            raise

    # Utility Methods

    def _ensure_image(self, image: str) -> None:
        """Pull image if not present locally."""
        try:
            self.client.images.get(image)
            logger.debug(f"Image already present: {image}")
        except ImageNotFound:
            logger.info(f"Pulling image: {image}")
            self.client.images.pull(image)
            logger.info(f"Successfully pulled: {image}")

    def list_containers(
        self, labels: Optional[Dict[str, str]] = None, all: bool = True
    ) -> List[Dict[str, Any]]:
        """List containers, optionally filtered by labels."""
        filters = {}
        if labels:
            filters["label"] = [f"{k}={v}" for k, v in labels.items()]

        containers = self.client.containers.list(all=all, filters=filters)
        return [
            {
                "id": c.id,
                "name": c.name,
                "status": c.status,
                "image": c.image.tags[0] if c.image.tags else None,
                "labels": c.labels,
            }
            for c in containers
        ]

    def list_networks(self, labels: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """List networks, optionally filtered by labels."""
        filters = {}
        if labels:
            filters["label"] = [f"{k}={v}" for k, v in labels.items()]

        networks = self.client.networks.list(filters=filters)
        return [
            {
                "id": n.id,
                "name": n.name,
                "driver": n.attrs.get("Driver"),
                "scope": n.attrs.get("Scope"),
                "labels": n.attrs.get("Labels", {}),
            }
            for n in networks
        ]

    def cleanup_range(self, range_id: str) -> Dict[str, int]:
        """
        Remove all containers and networks for a range.

        Returns:
            Dict with counts of removed containers and networks
        """
        labels = {"pg.range_id": range_id}

        # Stop and remove containers
        containers = self.list_containers(labels=labels)
        removed_containers = 0
        for container in containers:
            if self.remove_container(container["id"]):
                removed_containers += 1

        # Remove networks - disconnect traefik first to avoid "network has active endpoints" error
        networks = self.list_networks(labels=labels)
        removed_networks = 0
        for network in networks:
            network_id = network["id"]
            # Disconnect traefik before removing network
            self.disconnect_traefik_from_network(network_id)
            # Also teardown iptables isolation rules if any
            try:
                network_info = self.get_network(network_id)
                if network_info:
                    # Get subnet from network config for isolation cleanup
                    docker_net = self.client.networks.get(network_id)
                    ipam_config = docker_net.attrs.get("IPAM", {}).get("Config", [])
                    if ipam_config:
                        subnet = ipam_config[0].get("Subnet")
                        if subnet:
                            self.teardown_network_isolation(network_id, subnet)
            except Exception as e:
                logger.warning(f"Failed to cleanup isolation for network {network_id}: {e}")

            try:
                if self.delete_network(network_id):
                    removed_networks += 1
            except Exception as e:
                logger.error(f"Failed to delete network {network_id}: {e}")

        logger.info(
            f"Cleaned up range {range_id}: {removed_containers} containers, {removed_networks} networks"
        )
        return {"containers": removed_containers, "networks": removed_networks}

    def cleanup_all_proving_ground_resources(
        self, include_ready_pool_members: bool = False
    ) -> Dict[str, Any]:
        """
        Nuclear option: Remove ALL PROVING GROUND-managed Docker resources.

        This cleans up:
        - All containers with proving_ground.* labels
        - Range-shaped warm-pool containers (I3, see below)
        - All networks with proving_ground.* labels (except infrastructure networks)
        - Disconnects Traefik from networks before removal
        - Tears down iptables isolation rules

        Args:
            include_ready_pool_members: if False (default), a pool member
                still sitting in the warm pool's ready set (pg:pool:ready) is
                left alone even though it matches the range-shaped match
                below - see the I3 note in Step 1 for why. Pass True only
                when the caller explicitly wants a full teardown that also
                empties the warm pool.

        Returns:
            Dict with counts of removed resources and any errors
        """
        results = {"containers_removed": 0, "networks_removed": 0, "errors": []}

        # Step 1: Remove all PROVING GROUND VM/range containers (not infrastructure)
        logger.info("Cleanup: Removing all PROVING GROUND range containers...")

        # I3: a pool member claimed for a range is renamed to pg-range-* on
        # claim, but Docker labels are immutable on a running container (see
        # dind_service._find_container_by_range_id's docstring) - it keeps
        # pg.type=pool and never gains pg.range_id. Matching only
        # pg.range_id/pg.vm_id therefore makes a claimed-and-orphaned (or
        # otherwise DB-untracked) warm range invisible to this "remove ALL
        # resources" nuke: the trainee-root container stays running while
        # this call reports success. Also match pg.type=pool containers
        # whose name has the pg-range- prefix.
        #
        # That prefix alone can't tell an orphan apart from a perfectly
        # healthy, unclaimed member still sitting in the warm pool - a fresh
        # member is ALSO named pg-range-{short_id} (dind_service's
        # _get_container_name has no separate naming scheme for pool vs.
        # range). Cross-check against the pool's own bookkeeping
        # (pg:pool:ready, the Redis set claim() spop()s from) to tell them
        # apart: a container id still IN that set is a live, claimable
        # member, so leave it alone unless the caller passed
        # include_ready_pool_members=True. Destroying a ready member isn't a
        # security problem (nothing was ever handed to a trainee) but it
        # would needlessly empty a working warm pool during what is meant to
        # be an orphan sweep (see admin.py's cleanup_all_resources, which
        # calls this as its final catch-all step after DB-tracked ranges and
        # dind_service.list_range_containers() orphans are already handled).
        ready_pool_member_ids: set = set()
        if not include_ready_pool_members:
            try:
                from proving_ground.services.range_pool_service import (
                    get_pool_service,
                    POOL_READY_KEY,
                )

                pool_redis = get_pool_service().redis
                ready_pool_member_ids = {
                    m.decode() if isinstance(m, bytes) else m
                    for m in pool_redis.smembers(POOL_READY_KEY)
                }
            except Exception as e:
                logger.warning(f"Cleanup: could not read pool ready set, treating it as empty: {e}")

        try:
            all_containers = self.client.containers.list(all=True)
            for container in all_containers:
                labels = container.labels or {}
                # Skip PROVING GROUND infrastructure containers (api, worker, db, etc.)
                container_name = container.name or ""
                if any(
                    infra in container_name
                    for infra in [
                        "pg-api",
                        "pg-worker",
                        "pg-db",
                        "pg-redis",
                        "pg-traefik",
                        "pg-frontend",
                        "pg-minio",
                    ]
                ):
                    continue

                is_tracked_range = bool(labels.get("pg.range_id") or labels.get("pg.vm_id"))
                is_range_shaped_pool_member = labels.get(
                    "pg.type"
                ) == "pool" and container_name.startswith("pg-range-")

                if not (is_tracked_range or is_range_shaped_pool_member):
                    continue

                if (
                    is_range_shaped_pool_member
                    and not is_tracked_range
                    and container.id in ready_pool_member_ids
                ):
                    logger.debug(f"Cleanup: skipping {container_name} - healthy ready pool member")
                    continue

                # Remove: a DB-trackable range container, or an
                # orphaned/claimed pool member matched above (I3). A
                # healthy ready pool member already `continue`d above.
                try:
                    logger.info(f"Cleanup: Removing container {container.name}")
                    container.remove(force=True)
                    results["containers_removed"] += 1
                except Exception as e:
                    error_msg = f"Failed to remove container {container.name}: {e}"
                    logger.error(error_msg)
                    results["errors"].append(error_msg)
        except Exception as e:
            error_msg = f"Failed to list containers: {e}"
            logger.error(error_msg)
            results["errors"].append(error_msg)

        # Step 2: Remove all PROVING GROUND networks (except management network)
        logger.info("Cleanup: Removing all PROVING GROUND networks...")
        try:
            all_networks = self.client.networks.list()
            for network in all_networks:
                network_name = network.name or ""

                # Never touch the platform's own networks.
                if network_name in infrastructure_network_names():
                    continue

                # Only remove networks we created for a range. Matching on the "pg-"
                # name prefix alone would also catch unrelated host networks.
                if "pg.range_id" not in (network.attrs.get("Labels") or {}):
                    continue

                try:
                    # Disconnect traefik first
                    self.disconnect_traefik_from_network(network.id)

                    # Teardown iptables isolation if applicable
                    try:
                        ipam_config = network.attrs.get("IPAM", {}).get("Config", [])
                        if ipam_config:
                            subnet = ipam_config[0].get("Subnet")
                            if subnet:
                                self.teardown_network_isolation(network.id, subnet)
                    except Exception as e:
                        logger.warning(f"Failed to teardown isolation for {network_name}: {e}")

                    # Remove the network
                    logger.info(f"Cleanup: Removing network {network_name}")
                    if self.delete_network(network.id):
                        results["networks_removed"] += 1
                except Exception as e:
                    error_msg = f"Failed to remove network {network_name}: {e}"
                    logger.error(error_msg)
                    results["errors"].append(error_msg)
        except Exception as e:
            error_msg = f"Failed to list networks: {e}"
            logger.error(error_msg)
            results["errors"].append(error_msg)

        logger.info(
            f"Cleanup complete: {results['containers_removed']} containers, "
            f"{results['networks_removed']} networks removed, "
            f"{len(results['errors'])} errors"
        )
        return results

    def get_system_info(self) -> Dict[str, Any]:
        """Get Docker system information."""
        info = self.client.info()
        return {
            "containers": info.get("Containers", 0),
            "containers_running": info.get("ContainersRunning", 0),
            "containers_paused": info.get("ContainersPaused", 0),
            "containers_stopped": info.get("ContainersStopped", 0),
            "images": info.get("Images", 0),
            "docker_version": info.get("ServerVersion"),
            "os": info.get("OperatingSystem"),
            "architecture": info.get("Architecture"),
            "cpus": info.get("NCPU"),
            "memory_bytes": info.get("MemTotal"),
        }

    # Image Caching Methods

    def cache_linux_image(self, image: str) -> Dict[str, Any]:
        """
        Pre-pull and cache a Linux container image.

        Args:
            image: Docker image name (e.g., "ubuntu:22.04")

        Returns:
            Dict with image info
        """
        logger.info(f"Caching Linux image: {image}")
        pulled_image = self.client.images.pull(image)
        return {
            "id": pulled_image.id,
            "tags": pulled_image.tags,
            "size_bytes": pulled_image.attrs.get("Size", 0),
            "created": pulled_image.attrs.get("Created"),
        }

    def list_cached_images(self) -> List[Dict[str, Any]]:
        """List all cached Docker images."""
        images = self.client.images.list()
        return [
            {
                "id": img.id,
                "tags": img.tags,
                "size_bytes": img.attrs.get("Size", 0),
                "created": img.attrs.get("Created"),
            }
            for img in images
            if img.tags  # Only show tagged images
        ]

    def prune_images(self) -> Dict[str, Any]:
        """
        Prune unused Docker images (dangling and unreferenced images).

        Returns:
            Dict with images_deleted count and space_reclaimed bytes
        """
        try:
            # Prune dangling images first
            result = self.client.images.prune(filters={"dangling": True})
            images_deleted = len(result.get("ImagesDeleted", []) or [])
            space_reclaimed = result.get("SpaceReclaimed", 0)

            # Also prune unused images (not used by containers)
            result2 = self.client.images.prune(filters={"dangling": False})
            images_deleted += len(result2.get("ImagesDeleted", []) or [])
            space_reclaimed += result2.get("SpaceReclaimed", 0)

            logger.info(
                f"Pruned {images_deleted} images, reclaimed {space_reclaimed / (1024**3):.2f} GB"
            )

            return {"images_deleted": images_deleted, "space_reclaimed": space_reclaimed}
        except Exception as e:
            logger.error(f"Failed to prune images: {e}")
            raise

    def get_windows_iso_cache_status(self) -> Dict[str, Any]:
        """
        Check status of cached Windows ISOs.

        Returns:
            Dict with cached ISOs and their sizes
        """
        import os
        from proving_ground.config import get_settings

        settings = get_settings()

        cached_isos = []
        # Windows ISOs are stored in a subdirectory
        windows_iso_dir = os.path.join(settings.iso_cache_dir, "windows-isos")

        if os.path.exists(windows_iso_dir):
            for filename in os.listdir(windows_iso_dir):
                if filename.endswith(".iso"):
                    filepath = os.path.join(windows_iso_dir, filename)
                    cached_isos.append(
                        {
                            "filename": filename,
                            "path": filepath,
                            "size_bytes": os.path.getsize(filepath),
                            "size_gb": round(os.path.getsize(filepath) / (1024**3), 2),
                        }
                    )

        return {"cache_dir": windows_iso_dir, "isos": cached_isos, "total_count": len(cached_isos)}

    def get_linux_iso_cache_status(self) -> Dict[str, Any]:
        """
        Check status of cached Linux ISOs.

        Returns:
            Dict with cached ISOs and their sizes
        """
        import os
        from proving_ground.config import get_settings

        settings = get_settings()

        cached_isos = []
        # Linux ISOs are stored in a subdirectory
        linux_iso_dir = os.path.join(settings.iso_cache_dir, "linux-isos")

        if os.path.exists(linux_iso_dir):
            for filename in os.listdir(linux_iso_dir):
                if (
                    filename.endswith(".iso")
                    or filename.endswith(".img")
                    or filename.endswith(".qcow2")
                ):
                    filepath = os.path.join(linux_iso_dir, filename)
                    cached_isos.append(
                        {
                            "filename": filename,
                            "path": filepath,
                            "size_bytes": os.path.getsize(filepath),
                            "size_gb": round(os.path.getsize(filepath) / (1024**3), 2),
                        }
                    )

        return {"cache_dir": linux_iso_dir, "isos": cached_isos, "total_count": len(cached_isos)}

    def get_all_iso_cache_status(self) -> Dict[str, Any]:
        """
        Get combined status of all ISO caches (Windows and Linux).

        Returns:
            Dict with both Windows and Linux ISO caches
        """
        windows_cache = self.get_windows_iso_cache_status()
        linux_cache = self.get_linux_iso_cache_status()

        return {
            "windows": windows_cache,
            "linux": linux_cache,
            "total_count": windows_cache["total_count"] + linux_cache["total_count"],
            "total_size_gb": round(
                sum(iso["size_gb"] for iso in windows_cache["isos"])
                + sum(iso["size_gb"] for iso in linux_cache["isos"]),
                2,
            ),
        }

    def get_golden_images_status(self, os_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Check status of golden images (pre-installed VM templates).
        Works for both Windows (dockur/windows) and Linux (qemux/qemu) VMs.

        Args:
            os_type: Filter by OS type ('windows', 'linux', or None for all)

        Returns:
            Dict with golden images and their sizes
        """
        import os
        from proving_ground.config import get_settings

        settings = get_settings()

        golden_images = []
        template_dir = settings.template_storage_dir

        if os.path.exists(template_dir):
            for dirname in os.listdir(template_dir):
                dirpath = os.path.join(template_dir, dirname)
                if os.path.isdir(dirpath):
                    # Determine OS type from directory name or metadata
                    detected_os = "windows" if dirname.startswith("win") else "linux"

                    # Filter by OS type if specified
                    if os_type and detected_os != os_type:
                        continue

                    # Calculate total size of the golden image
                    total_size = 0
                    for root, _dirs, files in os.walk(dirpath):
                        for f in files:
                            total_size += os.path.getsize(os.path.join(root, f))

                    golden_images.append(
                        {
                            "name": dirname,
                            "path": dirpath,
                            "size_bytes": total_size,
                            "size_gb": round(total_size / (1024**3), 2),
                            "os_type": detected_os,
                        }
                    )

        return {
            "template_dir": template_dir,
            "golden_images": golden_images,
            "total_count": len(golden_images),
        }

    def create_golden_image_from_range(
        self,
        range_id: str,
        docker_url: str,
        container_id: str,
        golden_image_name: str,
        os_type: str = "windows",
    ) -> Dict[str, Any]:
        """
        Capture a golden image from a VM running inside a range's DinD daemon.

        create_golden_image() cannot do this. It resolves the container on the
        HOST daemon and then reads the /storage mount straight off the host
        filesystem. Under DinD range isolation neither holds: the host daemon
        has never heard of the inner container, and the mount source is a path
        inside the DinD's own filesystem. Capture simply fails.

        The archive is streamed out of the container rather than copied by a
        helper container, because a range network is isolated by default and
        cannot pull an image to do the copying with.

        Args:
            range_id: Range the VM belongs to
            docker_url: The range's DinD daemon URL (tcp://ip:port)
            container_id: VM container id *inside* that daemon
            golden_image_name: Name for the golden image
            os_type: 'windows' or 'linux'

        Returns:
            Dict with golden image info
        """
        import io
        import os
        import tarfile

        settings = get_settings()
        dind = self.get_range_client_sync(str(range_id), docker_url)

        try:
            container = dind.containers.get(container_id)
        except NotFound as e:
            raise ValueError(
                f"Container {container_id} not found in range {range_id}'s daemon"
            ) from e

        has_storage = any(
            m.get("Destination") == "/storage" for m in container.attrs.get("Mounts", [])
        )
        if not has_storage:
            raise ValueError("Container does not have a /storage mount")

        if container.status == "running":
            logger.warning(
                "Capturing golden image from a RUNNING container (%s). The guest disk "
                "may be mid-write; stop the VM first for a consistent image.",
                container_id[:12],
            )

        # Record the runtime this disk was captured against. A disk-based
        # golden image is an installed OS and expects the virtual hardware it
        # was installed on; deploying it against a newer dockur/QEMU sent
        # Windows into Automatic Repair (QEMU 10.0.11 at capture vs 11.1.0 on
        # the clone). Resolving a digest here keeps the capture reproducible.
        runtime_image_digest = None
        try:
            _img = dind.images.get(container.attrs["Config"]["Image"])
            _digests = _img.attrs.get("RepoDigests") or []
            # No RepoDigest means the image never came from a registry
            # (built or side-loaded), so there is nothing a remote deploy
            # could pin to; the local ID is still stable on this host.
            # Stored without a registry: the RepoDigest is qualified with
            # whatever mirror it came from, which is meaningless on another
            # deployment and also defeats both the warm pool and the host
            # pull here. The registry is applied when pulling instead.
            runtime_image_digest = strip_registry(_digests[0]) if _digests else _img.id
            logger.info(f"Golden image runtime pinned to {runtime_image_digest}")
        except Exception as e:
            logger.warning(
                "Could not resolve the runtime image digest for %s: %s. The image "
                "will deploy against the floating tag instead.",
                container_id[:12],
                e,
            )

        if not golden_image_name.startswith(("win", "linux-")):
            prefix = "win-" if os_type == "windows" else "linux-"
            golden_image_name = f"{prefix}{golden_image_name}"

        golden_dir = os.path.join(settings.template_storage_dir, golden_image_name)
        os.makedirs(golden_dir, exist_ok=True)

        logger.info(
            f"Capturing {os_type} golden image '{golden_image_name}' from range {range_id} "
            f"container {container_id[:12]} -> {golden_dir}"
        )

        bits, _stat = container.get_archive("/storage")

        class _ChunkStream(io.RawIOBase):
            """Adapt the chunk generator to a file object tarfile can stream."""

            def __init__(self, chunks):
                # iter() so any iterable works, not only a generator
                self._chunks = iter(chunks)
                self._buf = b""

            def readable(self) -> bool:
                return True

            def readinto(self, b) -> int:
                while not self._buf:
                    try:
                        self._buf = next(self._chunks)
                    except StopIteration:
                        return 0
                n = min(len(b), len(self._buf))
                b[:n] = self._buf[:n]
                self._buf = self._buf[n:]
                return n

        # get_archive prefixes every member with the directory name ("storage/").
        # Strip it so the golden image has the same shape as the volume itself,
        # which is what clone_from copies back in.
        #
        # SECURITY: this archive comes out of a range VM, which is hostile by
        # construction -- that is what a cyber range is. A member name or a
        # symlink target containing ".." would otherwise let the guest write
        # anywhere the worker can reach (tar slip, CVE-2007-4559 class). Every
        # member is resolved and confirmed to stay inside golden_dir, and links
        # that point outside it are dropped rather than followed.
        dest_root = os.path.realpath(golden_dir)

        def _is_within(path: str) -> bool:
            resolved = os.path.realpath(path)
            return resolved == dest_root or resolved.startswith(dest_root + os.sep)

        # Docker's archive endpoint reads a sparse file's holes as zeros, and
        # tarfile writes those zeros back as real blocks. The Windows disk is
        # 64 GiB apparent but ~9 GiB allocated, so a straight extract lands a
        # fully allocated 64 GiB file -- and every range cloned from it
        # inherits that. Seeking over all-zero chunks keeps it sparse.
        def _extract_sparse(tar_obj, member) -> None:
            target = os.path.join(golden_dir, member.name)
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            src = tar_obj.extractfile(member)
            if src is not None:
                _write_sparse(src, target, member.size)

        extracted = 0
        skipped = 0
        with tarfile.open(fileobj=_ChunkStream(bits), mode="r|*") as tar:
            for member in tar:
                parts = member.name.split("/", 1)
                if len(parts) == 1:
                    continue
                member.name = parts[1]
                if not member.name:
                    continue

                # Reject absolute paths and any traversal out of the target.
                if os.path.isabs(member.name) or not _is_within(
                    os.path.join(dest_root, member.name)
                ):
                    logger.warning(
                        "Golden image capture: refusing member outside the target " "directory: %r",
                        member.name,
                    )
                    skipped += 1
                    continue

                # A link is only kept if its target also stays inside.
                if member.issym() or member.islnk():
                    link_base = os.path.dirname(os.path.join(dest_root, member.name))
                    if not _is_within(os.path.join(link_base, member.linkname)):
                        logger.warning(
                            "Golden image capture: refusing link %r -> %r "
                            "(escapes the target directory)",
                            member.name,
                            member.linkname,
                        )
                        skipped += 1
                        continue

                # Devices, FIFOs and sockets have no place in a disk template.
                if member.isdev() or member.ischr() or member.isblk() or member.isfifo():
                    skipped += 1
                    continue

                if member.isreg():
                    _extract_sparse(tar, member)
                else:
                    tar.extract(member, path=golden_dir, set_attrs=False)
                extracted += 1

        if skipped:
            logger.warning(
                "Golden image capture: skipped %d unsafe or unsupported archive members",
                skipped,
            )

        # Two different numbers, and conflating them is how a 9 GB image gets
        # reported as 64 GB: apparent size is the disk the guest sees, while
        # allocated blocks are what this actually costs us on storage. Capacity
        # planning wants the latter, so size_bytes is the allocated figure.
        total_size = 0
        apparent_size = 0
        for root, _dirs, files in os.walk(golden_dir):
            for f in files:
                fp = os.path.join(root, f)
                if os.path.isfile(fp):
                    st = os.stat(fp)
                    apparent_size += st.st_size
                    total_size += st.st_blocks * 512

        logger.info(
            f"Golden image '{golden_image_name}' captured: {extracted} entries, "
            f"{round(total_size / (1024 ** 3), 2)} GB on disk "
            f"({round(apparent_size / (1024 ** 3), 2)} GB apparent)"
        )

        return {
            "name": golden_image_name,
            "path": golden_dir,
            "size_bytes": total_size,
            "size_gb": round(total_size / (1024**3), 2),
            "apparent_size_bytes": apparent_size,
            "os_type": os_type,
            "entries": extracted,
            "runtime_image_digest": runtime_image_digest,
        }

    def create_golden_image(
        self, container_id: str, golden_image_name: str, os_type: str = "windows"
    ) -> Dict[str, Any]:
        """
        Create a golden image from a running VM container.
        This saves the /storage directory for reuse.
        Works for both Windows (dockur/windows) and Linux (qemux/qemu) VMs.

        Args:
            container_id: ID of the VM container with completed installation
            golden_image_name: Name for the golden image
            os_type: Type of OS ('windows' or 'linux')

        Returns:
            Dict with golden image info
        """
        import os
        from proving_ground.config import get_settings

        settings = get_settings()

        # Get container info
        container = self.client.containers.get(container_id)
        mounts = container.attrs.get("Mounts", [])

        # Find the /storage mount
        storage_mount = None
        for mount in mounts:
            if mount.get("Destination") == "/storage":
                storage_mount = mount.get("Source")
                break

        if not storage_mount:
            raise ValueError("Container does not have a /storage mount")

        # Create golden image directory (prefix with OS type for organization)
        if not golden_image_name.startswith(("win", "linux-")):
            prefix = "win-" if os_type == "windows" else "linux-"
            golden_image_name = f"{prefix}{golden_image_name}"

        golden_dir = os.path.join(settings.template_storage_dir, golden_image_name)
        os.makedirs(golden_dir, exist_ok=True)

        # Copy storage to golden image
        logger.info(f"Creating {os_type} golden image from {storage_mount} to {golden_dir}")
        for item in os.listdir(storage_mount):
            src = os.path.join(storage_mount, item)
            dst = os.path.join(golden_dir, item)
            # Sparse-aware for the same reason as the DinD capture: the disk
            # is mostly holes and a plain copy writes them as real blocks.
            if os.path.isdir(src):
                _copytree_sparse(src, dst)
            else:
                _copy_sparse(src, dst)

        # Calculate size
        total_size = 0
        for root, _dirs, files in os.walk(golden_dir):
            for f in files:
                total_size += os.path.getsize(os.path.join(root, f))

        return {
            "name": golden_image_name,
            "path": golden_dir,
            "size_bytes": total_size,
            "size_gb": round(total_size / (1024**3), 2),
            "os_type": os_type,
        }

    def create_container_snapshot(
        self, container_id: str, snapshot_name: str, tag: str = "latest"
    ) -> Dict[str, Any]:
        """
        Create a Docker image snapshot from a running container using docker commit.
        Works for any container type (Linux, custom, etc.).

        Args:
            container_id: ID of the container to snapshot
            snapshot_name: Name for the new image (e.g., "proving_ground/mytemplate")
            tag: Tag for the image (default: "latest")

        Returns:
            Dict with snapshot image info
        """
        # Get container
        container = self.client.containers.get(container_id)

        # Create the snapshot image
        full_tag = f"{snapshot_name}:{tag}"
        logger.info(f"Creating container snapshot: {full_tag} from container {container_id}")

        # Commit the container to create a new image
        image = container.commit(
            repository=snapshot_name,
            tag=tag,
            message=f"Snapshot created from container {container_id}",
            author="proving_ground",
        )

        # Get image details
        image_info = self.client.images.get(image.id)
        size_bytes = image_info.attrs.get("Size", 0)

        return {
            "name": full_tag,
            "id": image.id,
            "short_id": image.short_id,
            "size_bytes": size_bytes,
            "size_gb": round(size_bytes / (1024**3), 2),
            "type": "docker",
        }

    def get_all_snapshots(self) -> Dict[str, Any]:
        """
        Get all snapshots - both Windows golden images and Docker container snapshots.

        Returns:
            Dict with both types of snapshots
        """
        from proving_ground.config import get_settings

        get_settings()

        # Get Windows golden images
        golden_images = self.get_golden_images_status()

        # Get Docker snapshots (images with proving_ground/ prefix or proving_ground labels)
        docker_snapshots = []
        for image in self.client.images.list():
            tags = image.tags
            labels = image.labels or {}

            # Check if it's a proving_ground snapshot
            is_snapshot = False
            for tag in tags:
                if tag.startswith("proving_ground/snapshot:") or tag.startswith(
                    "proving-ground-snapshot/"
                ):
                    is_snapshot = True
                    break

            # Also check labels
            if labels.get("pg.snapshot") == "true":
                is_snapshot = True

            if is_snapshot:
                docker_snapshots.append(
                    {
                        "id": image.id,
                        "short_id": image.short_id,
                        "tags": tags,
                        "size_bytes": image.attrs.get("Size", 0),
                        "size_gb": round(image.attrs.get("Size", 0) / (1024**3), 2),
                        "created": image.attrs.get("Created"),
                        "type": "docker",
                    }
                )

        return {
            "windows_golden_images": golden_images["golden_images"],
            "docker_snapshots": docker_snapshots,
            "total_windows": golden_images["total_count"],
            "total_docker": len(docker_snapshots),
            "template_dir": golden_images["template_dir"],
        }

    # =========================================================================
    # DinD Range Operations (operate inside range's DinD container)
    # =========================================================================

    async def create_range_network_dind(
        self,
        range_id: str,
        docker_url: str,
        name: str,
        subnet: str,
        gateway: Optional[str] = None,
        internal: bool = True,
        labels: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Create Docker network inside a range's DinD container.

        Uses EXACT subnet from blueprint - no IP translation needed.
        This is the DinD-aware version of create_network().

        Args:
            range_id: Range identifier
            docker_url: Docker URL for the range's DinD
            name: Network name
            subnet: CIDR notation (exact blueprint subnet)
            gateway: Gateway IP (VyOS will use this)
            internal: If True, no external connectivity
            labels: Optional labels

        Returns:
            Network ID
        """
        range_client = self.get_range_client_sync(range_id, docker_url)

        # Calculate bridge IP (.254) leaving .1 for VyOS
        subnet_obj = ipaddress.ip_network(subnet, strict=False)
        hosts = list(subnet_obj.hosts())
        bridge_ip = str(hosts[-1]) if hosts else gateway

        ipam_pool = docker.types.IPAMPool(subnet=subnet, gateway=bridge_ip)
        ipam_config = docker.types.IPAMConfig(pool_configs=[ipam_pool])

        try:
            network = range_client.networks.create(
                name=name,
                driver="bridge",
                internal=internal,
                ipam=ipam_config,
                labels=labels or {},
                attachable=True,
            )
            logger.info(f"Created network '{name}' ({subnet}) in range {range_id} DinD")
            return network.id
        except APIError as e:
            logger.error(f"Failed to create network {name} in DinD: {e}")
            raise

    async def delete_range_network_dind(
        self, range_id: str, docker_url: str, network_id: str
    ) -> bool:
        """Delete Docker network inside a range's DinD container."""
        range_client = self.get_range_client_sync(range_id, docker_url)

        try:
            network = range_client.networks.get(network_id)
            network.remove()
            logger.info(f"Deleted network {network_id[:12]} from range {range_id} DinD")
            return True
        except NotFound:
            logger.warning(f"Network {network_id} not found in DinD")
            return False
        except APIError as e:
            logger.error(f"Failed to delete network in DinD: {e}")
            raise

    async def list_range_networks_dind(
        self, range_id: str, docker_url: str
    ) -> List[Dict[str, Any]]:
        """List all networks in a range's DinD container."""
        range_client = self.get_range_client_sync(range_id, docker_url)
        networks = range_client.networks.list()

        return [
            {
                "id": n.id,
                "name": n.name,
                "driver": n.attrs.get("Driver"),
                "subnet": (n.attrs.get("IPAM", {}).get("Config", [{}])[0].get("Subnet")),
            }
            for n in networks
            if n.name not in ("bridge", "host", "none")
        ]

    def get_container_networks_dind(
        self, range_id: str, docker_url: str, container_id: str
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Get all network interfaces for a container inside DinD.

        This is the DinD-aware version of get_container_networks().
        Issue #78: Network Interfaces panel didn't work for DinD ranges
        because host Docker daemon can't see containers inside DinD.

        Returns:
            List of network interface dicts with:
            - network_id: Docker network ID (inside DinD)
            - network_name: Network name
            - ip_address: IP address on this network
            - mac_address: MAC address
            - gateway: Gateway IP (if available)
            - is_management: True if this is traefik-routing network
        """
        try:
            range_client = self.get_range_client_sync(range_id, docker_url)
            container = range_client.containers.get(container_id)
            networks_settings = container.attrs.get("NetworkSettings", {}).get("Networks", {})

            interfaces = []
            for net_name, net_config in networks_settings.items():
                interface = {
                    "network_id": net_config.get("NetworkID", ""),
                    "network_name": net_name,
                    "ip_address": net_config.get("IPAddress", ""),
                    "mac_address": net_config.get("MacAddress", ""),
                    "gateway": net_config.get("Gateway", ""),
                    "is_management": net_name == "traefik-routing",
                }
                interfaces.append(interface)

            return interfaces
        except NotFound:
            return None
        except APIError as e:
            logger.error(
                f"Failed to get network interfaces for container {container_id} in DinD: {e}"
            )
            return None

    def connect_container_to_network_dind(
        self,
        range_id: str,
        docker_url: str,
        container_id: str,
        network_name: str,
        ip_address: Optional[str] = None,
    ) -> bool:
        """
        Connect a container to a network inside DinD.

        Issue #79: Uses network name instead of ID because DinD networks
        have different IDs than what's stored in the database.

        Args:
            range_id: Range identifier
            docker_url: Docker URL for the range's DinD
            container_id: Container ID (inside DinD)
            network_name: Network name to connect to
            ip_address: Optional static IP address

        Returns:
            True if successful
        """
        try:
            range_client = self.get_range_client_sync(range_id, docker_url)

            # Find network by name inside DinD
            networks = range_client.networks.list(names=[network_name])
            if not networks:
                logger.error(f"Network '{network_name}' not found in DinD range {range_id}")
                return False

            network = networks[0]
            if ip_address:
                network.connect(container_id, ipv4_address=ip_address)
            else:
                network.connect(container_id)
            logger.info(
                f"Connected container {container_id[:12]} to network {network_name} in DinD with IP {ip_address or 'DHCP'}"
            )
            return True
        except NotFound as e:
            logger.error(f"Container or network not found in DinD: {e}")
            return False
        except APIError as e:
            logger.error(f"Failed to connect container to network in DinD: {e}")
            raise

    def disconnect_container_from_network_dind(
        self, range_id: str, docker_url: str, container_id: str, network_name: str
    ) -> bool:
        """
        Disconnect a container from a network inside DinD.

        Issue #79: Uses network name instead of ID because DinD networks
        have different IDs than what's stored in the database.

        Args:
            range_id: Range identifier
            docker_url: Docker URL for the range's DinD
            container_id: Container ID (inside DinD)
            network_name: Network name to disconnect from

        Returns:
            True if successful
        """
        try:
            range_client = self.get_range_client_sync(range_id, docker_url)

            # Find network by name inside DinD
            networks = range_client.networks.list(names=[network_name])
            if not networks:
                logger.warning(f"Network '{network_name}' not found in DinD range {range_id}")
                return False

            network = networks[0]
            network.disconnect(container_id)
            logger.info(
                f"Disconnected container {container_id[:12]} from network {network_name} in DinD"
            )
            return True
        except NotFound as e:
            logger.warning(f"Container or network not found in DinD: {e}")
            return False
        except APIError as e:
            logger.error(f"Failed to disconnect container from network in DinD: {e}")
            raise

    async def create_range_container_dind(
        self,
        range_id: str,
        docker_url: str,
        name: str,
        image: str,
        network_name: str,
        ip_address: str,
        cpu_limit: int = 2,
        memory_limit_mb: int = 2048,
        volumes: Optional[Dict[str, Dict]] = None,
        environment: Optional[Dict[str, str]] = None,
        labels: Optional[Dict[str, str]] = None,
        privileged: bool = False,
        cap_add: Optional[List[str]] = None,
        hostname: Optional[str] = None,
        dns_servers: Optional[str] = None,
        dns_search: Optional[str] = None,
        sysctls: Optional[Dict[str, str]] = None,
        devices: Optional[List[str]] = None,
        arch: Optional[str] = None,
        container_config: Optional[dict] = None,
    ) -> str:
        """
        Create a container inside a range's DinD container.

        Uses EXACT IP from blueprint - no IP translation needed.

        Args:
            range_id: Range identifier
            docker_url: Docker URL for the range's DinD
            name: Container name
            image: Docker image
            network_name: Network to attach to (inside DinD)
            ip_address: Static IP address (exact blueprint IP)
            arch: Target architecture ("x86_64" or "arm64"), None = host default
            Other args same as create_container()

        Returns:
            Container ID
        """
        range_client = self.get_range_client_sync(range_id, docker_url)

        # Map arch to Docker platform string
        platform = None
        if arch:
            platform = "linux/amd64" if arch == "x86_64" else "linux/arm64"

        # Verify image exists in DinD (should already be transferred in stage 3)
        # Don't pass platform here — stage 3 handles platform-aware transfers.
        # This is only a fallback check; passing platform to pull breaks local images.
        try:
            range_client.images.get(image)
        except ImageNotFound:
            logger.info(f"Image {image} not in DinD, pulling (fallback)")
            range_client.images.pull(image)

        # Get network
        try:
            network = range_client.networks.get(network_name)
        except NotFound as exc:
            raise ValueError(f"Network {network_name} not found in DinD") from exc

        # Create networking config
        networking_config = range_client.api.create_networking_config(
            {network.name: range_client.api.create_endpoint_config(ipv4_address=ip_address)}
        )

        # Parse DNS
        dns_list = (
            [s.strip() for s in dns_servers.split(",") if s.strip()]
            if dns_servers
            else ["8.8.8.8", "8.8.4.4"]
        )
        dns_search_list = (
            [s.strip() for s in dns_search.split(",") if s.strip()] if dns_search else None
        )

        if environment is None:
            environment = {}

        # Merge capabilities - always include NET_ADMIN plus any custom caps
        all_caps = ["NET_ADMIN"]
        if cap_add:
            all_caps = list(set(all_caps + cap_add))

        host_config_args = {
            "nano_cpus": int(cpu_limit * 1e9),
            "mem_limit": f"{memory_limit_mb}m",
            "binds": volumes,
            "privileged": privileged,
            "cap_add": all_caps,
            "restart_policy": {"Name": "unless-stopped"},
            "dns": dns_list,
        }
        if dns_search_list:
            host_config_args["dns_search"] = dns_search_list
        if sysctls:
            host_config_args["sysctls"] = sysctls
        if devices:
            host_config_args["devices"] = devices

        # Anything the base image declares (devices, sysctls, security_opt, extra
        # caps) is merged last. Callers that pass explicit args keep them - list
        # keys extend rather than replace. Without this the DinD path silently
        # drops container_config entries that the non-DinD path honours, which
        # is how /dev/net/tun went missing from a VM whose image asked for it.
        host_config_args = self._merge_container_config(host_config_args, container_config)

        try:
            # Note: platform is NOT passed to create_container — it's a strict
            # verification check that rejects if the cached image doesn't match.
            # The platform constraint is enforced at pull time instead.
            create_kwargs = dict(
                image=image,
                name=name,
                hostname=hostname or name,
                detach=True,
                tty=True,
                stdin_open=True,
                networking_config=networking_config,
                host_config=range_client.api.create_host_config(**host_config_args),
                environment=environment,
                labels=labels or {},
            )

            container = range_client.api.create_container(**create_kwargs)
            container_id = container["Id"]
            logger.info(
                f"Created container '{name}' in range {range_id} DinD with IP {ip_address} (platform={platform})"
            )
            return container_id
        except APIError as e:
            logger.error(f"Failed to create container in DinD: {e}")
            raise

    async def start_range_container_dind(
        self, range_id: str, docker_url: str, container_id: str
    ) -> bool:
        """Start a container inside a range's DinD."""
        range_client = self.get_range_client_sync(range_id, docker_url)
        try:
            range_client.api.start(container_id)
            logger.info(f"Started container {container_id[:12]} in range {range_id} DinD")
            return True
        except NotFound:
            return False
        except APIError as e:
            logger.error(f"Failed to start container in DinD: {e}")
            raise

    async def stop_range_container_dind(
        self, range_id: str, docker_url: str, container_id: str, timeout: int = 10
    ) -> bool:
        """Stop a container inside a range's DinD."""
        range_client = self.get_range_client_sync(range_id, docker_url)
        try:
            range_client.api.stop(container_id, timeout=timeout)
            logger.info(f"Stopped container {container_id[:12]} in range {range_id} DinD")
            return True
        except NotFound:
            return False
        except APIError as e:
            logger.error(f"Failed to stop container in DinD: {e}")
            raise

    async def remove_range_container_dind(
        self, range_id: str, docker_url: str, container_id: str, force: bool = True
    ) -> bool:
        """Remove a container inside a range's DinD."""
        range_client = self.get_range_client_sync(range_id, docker_url)
        try:
            range_client.api.remove_container(container_id, force=force, v=True)
            logger.info(f"Removed container {container_id[:12]} from range {range_id} DinD")
            return True
        except NotFound:
            return False
        except APIError as e:
            logger.error(f"Failed to remove container in DinD: {e}")
            raise

    def update_container_resources(
        self,
        container_id: str,
        cpu_limit: Optional[int] = None,
        memory_limit_mb: Optional[int] = None,
        range_id: Optional[str] = None,
        docker_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Change a running container's CPU and memory limits in place.

        Docker applies both to a live container without recreating it, so a
        trainee's session survives the change - unlike environment or device
        edits, which need apply-config.

        Sends the engine's update API directly rather than going through
        container.update(). The SDK's update_container() exposes cpu_quota and
        cpu_period but NOT nano_cpus, and containers here are created with
        NanoCpus - the daemon rejects the combination:

            409 Conflict: Conflicting options: Nano CPUs and CPU Period
            cannot both be set

        Verified against Docker Engine v1.55: raw NanoCpus returns 200, and the
        new limit survives a container restart.

        MemorySwap is set to twice Memory, which is what the daemon does at
        create time when only mem_limit is given. Leaving it out makes a raise
        fail whenever the new Memory would exceed the old MemorySwap.

        The daemon enforces no floor on these values - it accepts a NanoCpus of
        1000 (a millionth of a core). Callers must validate; VMResourceUpdate
        carries the bounds.

        Args:
            container_id: Container to update.
            cpu_limit: Whole CPU cores, or None to leave CPU alone.
            memory_limit_mb: Memory in MB, or None to leave memory alone.
            range_id: Range owning the container, when it lives in DinD.
            docker_url: The range's DinD URL. With range_id, targets that
                daemon; without, targets the host daemon.

        Returns:
            The fields that were sent, as {"cpu": int, "ram_mb": int}.

        Raises:
            ContainerNotFoundError: No such container.
            ResourceUpdateRejectedError: The daemon refused the update.
        """
        payload: Dict[str, Any] = {}
        applied: Dict[str, Any] = {}

        if cpu_limit is not None:
            payload["NanoCpus"] = int(cpu_limit * 1e9)
            applied["cpu"] = cpu_limit

        if memory_limit_mb is not None:
            memory_bytes = int(memory_limit_mb) * 1024 * 1024
            payload["Memory"] = memory_bytes
            payload["MemorySwap"] = memory_bytes * 2
            applied["ram_mb"] = memory_limit_mb

        if not payload:
            return {}

        if range_id and docker_url:
            client = self.get_range_client_sync(range_id, docker_url)
        else:
            client = self.client

        response = client.api._post_json(
            client.api._url("/containers/{0}/update", container_id),
            data=payload,
        )
        try:
            client.api._raise_for_status(response)
        except NotFound as e:
            raise ContainerNotFoundError(str(e)) from e
        except APIError as e:
            raise ResourceUpdateRejectedError(str(e)) from e

        logger.info(f"Updated resources on container {container_id[:12]}: {applied}")
        return applied

    async def get_range_container_status_dind(
        self, range_id: str, docker_url: str, container_id: str
    ) -> Optional[str]:
        """Get container status inside a range's DinD."""
        range_client = self.get_range_client_sync(range_id, docker_url)
        try:
            container = range_client.containers.get(container_id)
            return container.status
        except NotFound:
            return None

    async def list_range_containers_dind(
        self, range_id: str, docker_url: str
    ) -> List[Dict[str, Any]]:
        """List all containers in a range's DinD."""
        range_client = self.get_range_client_sync(range_id, docker_url)
        containers = range_client.containers.list(all=True)

        result = []
        for container in containers:
            networks = {}
            for net_name, net_info in (
                container.attrs.get("NetworkSettings", {}).get("Networks", {}).items()
            ):
                if net_name not in ("bridge", "host", "none"):
                    networks[net_name] = net_info.get("IPAddress")

            result.append(
                {
                    "id": container.id,
                    "name": container.name,
                    "status": container.status,
                    "image": container.image.tags[0] if container.image.tags else None,
                    "networks": networks,
                }
            )

        return result

    def _pull_platform_image(self, image: str, platform: str):
        """Pull a specific platform variant of a multi-platform image by resolving its digest.

        On ARM hosts, `docker pull --platform linux/amd64 <image>` may return the ARM
        variant from cache. This method resolves the platform-specific manifest digest
        and pulls by digest to guarantee the correct architecture, then re-tags it.

        Args:
            image: Image name (e.g., "dockurr/windows:latest")
            platform: Docker platform string (e.g., "linux/amd64")

        Returns:
            docker.Image or None if the image is not multi-platform
        """
        import subprocess

        target_arch = "amd64" if platform == "linux/amd64" else "arm64"

        try:
            # Use docker manifest inspect to find the platform-specific digest
            result = subprocess.run(
                ["docker", "manifest", "inspect", image],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.debug(f"manifest inspect failed for '{image}': {result.stderr}")
                return None

            import json

            manifest = json.loads(result.stdout)
            manifests = manifest.get("manifests", [])

            digest = None
            for m in manifests:
                plat = m.get("platform", {})
                if plat.get("architecture") == target_arch and plat.get("os") == "linux":
                    digest = m.get("digest")
                    break

            if not digest:
                logger.info(f"No {target_arch} variant found in manifest for '{image}'")
                return None

            # Parse repo and tag
            if ":" in image:
                repo = image.rsplit(":", 1)[0]
            else:
                repo = image

            # Pull by digest (bypasses local cache)
            logger.info(f"Pulling '{repo}@{digest}' (platform={platform})")
            pulled = self.client.images.pull(f"{repo}@{digest}")

            # Verify architecture
            pulled_arch = pulled.attrs.get("Architecture", "")
            if pulled_arch != target_arch:
                logger.warning(f"Digest pull got {pulled_arch}, expected {target_arch}")
                return None

            # Re-tag so the image is accessible by its original name
            if ":" in image:
                tag_repo, tag_tag = image.rsplit(":", 1)
            else:
                tag_repo, tag_tag = image, "latest"
            pulled.tag(tag_repo, tag_tag)
            logger.info(f"Tagged {target_arch} image as '{tag_repo}:{tag_tag}'")

            return self.client.images.get(image)

        except Exception as e:
            logger.warning(f"Platform-specific pull failed for '{image}': {e}")
            return None

    async def transfer_image_to_dind(
        self,
        range_id: str,
        docker_url: str,
        image: str,
        pull_if_missing: bool = True,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        arch: Optional[str] = None,
    ) -> bool:
        """
        Transfer a Docker image from host to a DinD container.

        This method exports the image from the host Docker daemon and imports
        it into the DinD container's Docker daemon. This works for:
        - Locally built images
        - Snapshots saved as images
        - Images without registry access in DinD

        Args:
            range_id: Range identifier
            docker_url: DinD Docker URL (tcp://ip:port)
            image: Image name/tag to transfer
            pull_if_missing: If True, pull image to host if not found locally
            progress_callback: Optional callback for progress reporting.
                Signature: (transferred: int, total: int, status: str) -> None
                Status values: 'starting', 'found_on_host', 'pulling_to_host',
                'pulled_to_host', 'already_exists', 'pushing_to_registry',
                'pulling_from_registry', 'transferring', 'complete', 'error'
            arch: Target architecture ("x86_64" or "arm64"), None = host default

        Returns:
            True if transfer succeeded, False otherwise
        """
        # Map arch to Docker platform string
        platform = None
        if arch:
            platform = "linux/amd64" if arch == "x86_64" else "linux/arm64"

        # Helper to safely call progress callback
        def report_progress(transferred: int, total: int, status: str) -> None:
            if progress_callback:
                try:
                    progress_callback(transferred, total, status)
                except Exception as e:
                    logger.warning(f"Progress callback error: {e}")

        logger.info(
            f"Transferring image '{image}' to DinD for range {range_id} (platform={platform})"
        )
        report_progress(0, 0, "starting")

        image_size = 0

        # Get DinD client first - we'll need it for all paths
        try:
            range_client = self.get_range_client_sync(range_id, docker_url)
        except Exception as e:
            logger.error(f"Failed to connect to DinD at {docker_url}: {e}")
            report_progress(0, 0, "error")
            return False

        # Check if image already exists in DinD (early return)
        # If a specific platform is requested, verify the cached image matches
        try:
            existing = range_client.images.get(image)
            if platform:
                img_arch = existing.attrs.get("Architecture", "")
                expected_arch = "amd64" if platform == "linux/amd64" else "arm64"
                if img_arch != expected_arch:
                    logger.info(
                        f"Image '{image}' in DinD is {img_arch}, need {expected_arch} — re-transferring"
                    )
                else:
                    logger.info(
                        f"Image '{image}' already exists in DinD with correct arch, skipping transfer"
                    )
                    report_progress(0, 0, "already_exists")
                    return True
            else:
                logger.info(f"Image '{image}' already exists in DinD, skipping transfer")
                report_progress(0, 0, "already_exists")
                return True
        except docker.errors.ImageNotFound:
            pass  # Need to transfer

        # Try registry-first: if image is in PROVING GROUND registry, pull directly into DinD
        # This allows deployment even when image isn't on the host Docker daemon
        try:
            from proving_ground.services.registry_service import get_registry_service

            registry = get_registry_service()

            # Already dead for a digest reference rather than broken: the split
            # below yields a repository containing "@sha256", and no registry
            # catalog name can contain "@", so the match never succeeds and this
            # falls through having spent a list_images() round trip. Skipping it
            # costs nothing and keeps both registry entry points consistent --
            # the second one does not merely fail to match, it errors.
            if not is_digest_ref(image) and await registry.is_healthy():
                # Check if image is already in registry
                registry_images = await registry.list_images()
                # Parse image name (e.g., "proving_ground/samba-dc:latest" -> repo="proving_ground/samba-dc", tag="latest")
                if ":" in image:
                    img_repo, img_tag = image.rsplit(":", 1)
                else:
                    img_repo, img_tag = image, "latest"

                # Check if this image+tag is in registry
                image_in_registry = False
                for reg_img in registry_images:
                    if reg_img.get("name") == img_repo and img_tag in reg_img.get("tags", []):
                        image_in_registry = True
                        break

                if image_in_registry:
                    logger.info(f"Image '{image}' found in registry, pulling directly into DinD")
                    report_progress(0, 0, "pulling_from_registry")

                    registry_tag = registry.get_registry_tag(image)
                    try:
                        # Don't pass platform for local registry pulls — the registry
                        # stores single-platform images, not multi-arch manifests.
                        range_client.images.pull(registry_tag)

                        # Retag to original name — use api.tag for reliability
                        # (images.get can fail for multi-platform manifests)
                        try:
                            pulled_image = range_client.images.get(registry_tag)
                            pulled_image.tag(img_repo, img_tag)
                        except docker.errors.ImageNotFound:
                            # Pull succeeded but get-by-tag fails (multi-platform manifest)
                            # Try tagging via API directly
                            range_client.api.tag(registry_tag, img_repo, img_tag)

                        logger.info(f"Successfully pulled '{image}' from registry into DinD")
                        report_progress(0, 0, "complete")
                        return True
                    except Exception as pull_err:
                        logger.warning(
                            f"Failed to pull from registry: {pull_err}, falling back to host transfer"
                        )
        except ImportError:
            logger.debug("Registry service not available")
        except Exception as e:
            logger.warning(f"Registry-first check failed: {e}, falling back to host transfer")

        # Check if image exists on host - try with and without :latest tag
        host_image = None
        image_variants = [image]
        if ":" not in image:
            image_variants.append(f"{image}:latest")

        for img_name in image_variants:
            try:
                host_image = self.client.images.get(img_name)
                image_size = host_image.attrs.get("Size", 0)
                logger.info(
                    f"Image '{img_name}' found on host (size: {image_size / 1024 / 1024:.1f} MB, tags: {host_image.tags})"
                )

                # If a specific platform is requested, verify the cached image matches.
                # On ARM hosts, "dockurr/windows:latest" may be cached as ARM even though
                # we need amd64. Multi-platform images require digest-based re-pull.
                if platform:
                    img_arch = host_image.attrs.get("Architecture", "")
                    expected_arch = "amd64" if platform == "linux/amd64" else "arm64"
                    if img_arch != expected_arch:
                        logger.warning(
                            f"Image '{img_name}' on host is {img_arch}, need {expected_arch} — "
                            f"re-pulling with platform digest"
                        )
                        host_image = self._pull_platform_image(image, platform)
                        if host_image:
                            image_size = host_image.attrs.get("Size", 0)
                            logger.info(
                                f"Re-pulled '{image}' as {expected_arch} ({image_size / 1024 / 1024:.1f} MB)"
                            )
                        else:
                            logger.error(f"Failed to re-pull '{image}' as {expected_arch}")
                            report_progress(0, 0, "error")
                            return False

                report_progress(0, image_size, "found_on_host")
                break
            except docker.errors.ImageNotFound:
                logger.debug(f"Image '{img_name}' not found on host")
                continue

        if host_image is None:
            if pull_if_missing:
                logger.info(f"Image '{image}' not on host, pulling (platform={platform})...")
                report_progress(0, 0, "pulling_to_host")
                try:
                    if platform:
                        # Use digest-based pull for cross-platform to avoid ARM cache issues
                        host_image = self._pull_platform_image(image, platform)
                        if not host_image:
                            host_image = self.client.images.pull(image, platform=platform)
                    else:
                        host_image = self.client.images.pull(image)
                    image_size = host_image.attrs.get("Size", 0)
                    logger.info(
                        f"Pulled '{image}' to host (size: {image_size / 1024 / 1024:.1f} MB)"
                    )
                    report_progress(image_size, image_size, "pulled_to_host")
                except Exception as e:
                    logger.error(f"Failed to pull '{image}' to host: {e}")
                    report_progress(0, 0, "error")
                    return False
            else:
                logger.error(f"Image '{image}' not found on host (tried: {image_variants})")
                report_progress(0, 0, "error")
                return False

        try:
            # Try registry-based transfer from host (much faster with layer caching)
            try:
                from proving_ground.services.registry_service import get_registry_service

                registry = get_registry_service()

                # A digest reference has no tag to push to. get_registry_tag()
                # splits on the last colon, so repo@sha256:0dfe... becomes the
                # repository "repo@sha256" with tag "0dfe...": the registry
                # 404s the tags list, the push errors, and the transfer falls
                # through to tar anyway -- having spent a round trip and logged
                # an ERROR that reads like a registry fault. Every pinned image
                # takes that path, and ADR-0007 makes pinning the rule, so go
                # straight to the tar route that actually completes.
                if is_digest_ref(image):
                    logger.info(
                        f"Image '{image}' is a digest reference; transferring by tar "
                        "(a digest cannot be pushed to a registry)."
                    )
                elif await registry.is_healthy():
                    report_progress(0, image_size, "pushing_to_registry")

                    # Ensure image is in registry (push-on-demand)
                    if await registry.ensure_image_in_registry(image):
                        registry_tag = registry.get_registry_tag(image)
                        report_progress(0, image_size, "pulling_from_registry")

                        # Pull from registry into DinD (no platform — local registry is single-arch)
                        try:
                            range_client.images.pull(registry_tag)

                            # Retag to original name
                            if ":" in image:
                                repo, tag = image.rsplit(":", 1)
                            else:
                                repo, tag = image, "latest"
                            try:
                                pulled_image = range_client.images.get(registry_tag)
                                pulled_image.tag(repo, tag)
                            except docker.errors.ImageNotFound:
                                range_client.api.tag(registry_tag, repo, tag)

                            logger.info(f"Successfully transferred '{image}' via registry")
                            report_progress(image_size, image_size, "complete")
                            return True
                        except Exception as e:
                            logger.warning(
                                f"Failed to pull from registry, falling back to tar: {e}"
                            )
                    else:
                        logger.warning("Failed to push to registry, falling back to tar")
                else:
                    logger.info("Registry not healthy, using tar transfer")
            except ImportError:
                logger.info("Registry service not available, using tar transfer")
            except Exception as e:
                logger.warning(f"Registry transfer failed, falling back to tar: {e}")

            # Export image from host and import to DinD (tar-based fallback)
            logger.info(
                f"Exporting image '{image}' ({image_size / 1024 / 1024:.1f} MB) from host..."
            )
            report_progress(0, image_size, "transferring")

            # Get image as tar stream from host
            # named=True preserves the image tags
            try:
                logger.info(f"Starting image export (tags: {host_image.tags})...")
                image_data = host_image.save(named=True)
                logger.info(f"Image export stream created, loading into DinD at {docker_url}...")
            except Exception as save_err:
                logger.error(
                    f"Failed to export image '{image}' from host: {type(save_err).__name__}: {save_err}"
                )
                import traceback

                logger.error(f"Export traceback: {traceback.format_exc()}")
                report_progress(0, image_size, "error")
                return False

            # Wrap the generator to track progress
            import time

            transferred_bytes = [0]
            last_progress_report = [time.time()]
            start_time = time.time()

            def progress_wrapper(data_generator):
                """Wrap generator to track bytes transferred and report progress."""
                for chunk in data_generator:
                    transferred_bytes[0] += len(chunk)
                    # Report progress every 2 seconds to avoid spamming
                    now = time.time()
                    if now - last_progress_report[0] >= 2.0:
                        last_progress_report[0] = now
                        if image_size > 0:
                            pct = min(100, int((transferred_bytes[0] / image_size) * 100))
                            elapsed = int(now - start_time)
                            transferred_mb = transferred_bytes[0] / 1024 / 1024
                            total_mb = image_size / 1024 / 1024
                            report_progress(transferred_bytes[0], image_size, f"transferring:{pct}")
                            logger.info(
                                f"Transfer progress: {transferred_mb:.1f}/{total_mb:.1f} MB ({pct}%) - {elapsed}s elapsed"
                            )
                    yield chunk

            # Load into DinD - images.load accepts an iterator of bytes
            try:
                logger.info("Loading image into DinD (this may take a while for large images)...")
                result = range_client.images.load(progress_wrapper(image_data))
                elapsed = int(time.time() - start_time)
                logger.info(f"Image load completed in {elapsed}s")
            except Exception as load_err:
                logger.error(
                    f"Failed to load image '{image}' into DinD: {type(load_err).__name__}: {load_err}"
                )
                import traceback

                logger.error(f"Load traceback: {traceback.format_exc()}")
                # Check for common issues
                if "connection" in str(load_err).lower() or "timeout" in str(load_err).lower():
                    logger.error(
                        "Connection issue during image transfer - DinD may have restarted or network issue"
                    )
                report_progress(0, image_size, "error")
                return False

            if result:
                loaded_images = [img.tags[0] if img.tags else img.id for img in result]
                logger.info(f"Successfully transferred to DinD: {loaded_images}")

                # Retag images inside DinD to bare name (without registry prefix)
                # The tar export preserves all host tags like "127.0.0.1:5000/proving_ground/foo:latest"
                # but the VM creation code expects "proving_ground/foo:latest"
                if ":" in image:
                    bare_repo, bare_tag = image.rsplit(":", 1)
                else:
                    bare_repo, bare_tag = image, "latest"

                for loaded_img in result:
                    if loaded_img.tags:
                        try:
                            loaded_img.tag(bare_repo, bare_tag)
                            logger.info(f"Retagged to '{bare_repo}:{bare_tag}' inside DinD")
                            break
                        except Exception as tag_err:
                            logger.warning(f"Failed to retag image in DinD: {tag_err}")
            else:
                logger.info(f"Successfully transferred '{image}' to DinD (no result metadata)")

            report_progress(image_size, image_size, "complete")
            return True

        except docker.errors.APIError as e:
            logger.error(f"Docker API error transferring image '{image}' to DinD: {e}")
            report_progress(0, image_size, "error")
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error transferring image '{image}' to DinD: {type(e).__name__}: {e}"
            )
            import traceback

            logger.error(f"Traceback: {traceback.format_exc()}")
            report_progress(0, image_size, "error")
            return False

    async def pull_image_to_dind(
        self,
        range_id: str,
        docker_url: str,
        image: str,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        arch: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Pull/transfer a Docker image into a range's DinD container, tracking source.

        First attempts to transfer from host/registry, falls back to pulling directly
        into DinD if transfer fails. When pulling from internet, automatically caches
        the image to the local registry for future deployments.

        Args:
            range_id: Range identifier
            docker_url: DinD Docker URL (tcp://ip:port)
            image: Image name/tag to transfer
            progress_callback: Optional callback for progress reporting.
                Signature: (transferred: int, total: int, status: str) -> None
            arch: Target architecture ("x86_64" or "arm64"), None = host default

        Returns:
            Dict with:
                - success: bool - whether the image was made available in DinD
                - source: 'registry' | 'internet' | 'error' - where image came from
                - cached_to_registry: bool - if pulled from internet, whether it was cached
                - image: str - the image name
        """
        result = {
            "success": False,
            "source": "error",
            "cached_to_registry": False,
            "image": image,
        }

        # Map arch to Docker platform string for direct pull fallback
        platform = None
        if arch:
            platform = "linux/amd64" if arch == "x86_64" else "linux/arm64"

        # Try host-to-DinD transfer first (handles local images, snapshots, and registry)
        if await self.transfer_image_to_dind(
            range_id, docker_url, image, progress_callback=progress_callback, arch=arch
        ):
            result["success"] = True
            result["source"] = "registry"
            return result

        # Check if this is a local-only image (no registry prefix like docker.io/, ghcr.io/, etc.)
        # Local images like "proving_ground/redteam-kali" can't be pulled from a registry
        is_local_only = (
            "/" in image
            and not any(
                image.startswith(prefix)
                for prefix in [
                    "docker.io/",
                    "ghcr.io/",
                    "gcr.io/",
                    "quay.io/",
                    "registry.",
                    "localhost:",
                    "127.0.0.1:",
                ]
            )
            and "." not in image.split("/")[0]
        )

        if is_local_only:
            # Before failing, check if we can auto-build from a Dockerfile project
            # Images like "<namespace>/<project>:latest" may have Dockerfiles in
            # /data/images/<project>/. Accept the configured namespace and legacy
            # ones (e.g. "proving_ground/") so upstream catalog images still auto-build.
            built = False
            _ns = get_settings()
            if any(
                image.startswith(f"{n}/")
                for n in [_ns.image_namespace, *_ns.legacy_image_namespaces]
            ):
                import os

                project_name = image.split("/", 1)[1].split(":")[0]
                dockerfile_path = f"/data/images/{project_name}/Dockerfile"
                if os.path.isfile(dockerfile_path):
                    logger.info(
                        f"Image '{image}' not found, but Dockerfile exists at "
                        f"/data/images/{project_name}/ — attempting auto-build"
                    )
                    if progress_callback:
                        progress_callback(0, 0, "building_from_dockerfile")
                    try:
                        host_client = docker.from_env()
                        _img, build_logs = host_client.images.build(
                            path=f"/data/images/{project_name}",
                            tag=image,
                            rm=True,
                            forcerm=True,
                        )
                        for log_line in build_logs:
                            if "stream" in log_line:
                                logger.debug(log_line["stream"].strip())
                        logger.info(f"Auto-built image '{image}' from Dockerfile")
                        built = True
                    except Exception as build_err:
                        logger.warning(
                            f"Auto-build of '{image}' from Dockerfile failed: {build_err}"
                        )

            if built:
                # Retry transfer now that image exists on host
                if await self.transfer_image_to_dind(
                    range_id,
                    docker_url,
                    image,
                    progress_callback=progress_callback,
                    arch=arch,
                ):
                    result["success"] = True
                    result["source"] = "registry"
                    return result

            # Don't try to pull local-only images - they need to be transferred
            raise RuntimeError(
                f"Image '{image}' appears to be a local image that could not be transferred to DinD. "
                f"Ensure the image exists on the host with 'docker images | grep {image.split('/')[0]}'"
            )

        # Fallback: try direct pull into DinD (requires internet in DinD)
        logger.info(f"Falling back to direct pull of '{image}' into DinD (platform={platform})")
        try:
            range_client = self.get_range_client_sync(range_id, docker_url)
            range_client.images.pull(image, platform=platform)
            result["success"] = True

            # Where it actually came from. A reference already qualified with
            # our own mirror was pulled FROM the mirror, not the internet --
            # the host-side transfer failed first only because the host daemon
            # speaks HTTPS to a plain-HTTP mirror. Calling that an internet
            # pull sent it on to be cached back into the registry it just came
            # from, which then failed on every deploy and logged a warning that
            # looked like a broken cache.
            from proving_ground.services.registry_service import RegistryService

            _mirror = f"{RegistryService.REGISTRY_IP}:{RegistryService.REGISTRY_PORT}"
            from_mirror = image.startswith(f"{_mirror}/")

            if from_mirror:
                logger.info(f"Pulled '{image}' into DinD from the local registry")
                result["source"] = "registry"
                result["cached_to_registry"] = True
                return result

            logger.info(f"Successfully pulled '{image}' into DinD from internet")
            result["source"] = "internet"

            # Auto-cache to registry for future deployments. A digest reference
            # cannot be pushed -- docker push takes a tag -- so there is nothing
            # to cache and no point reporting a failure.
            if "@sha256:" in image:
                logger.info(
                    f"Not caching '{image}': a digest reference cannot be pushed to a registry"
                )
                return result

            cached = await self._cache_dind_image_to_registry(range_id, docker_url, image)
            result["cached_to_registry"] = cached
            if cached:
                logger.info(f"Cached internet-pulled image '{image}' to registry")
            else:
                logger.warning(f"Failed to cache internet-pulled image '{image}' to registry")

            return result

        except docker.errors.APIError as e:
            if "pull access denied" in str(e) or "repository does not exist" in str(e):
                raise RuntimeError(
                    f"Cannot pull image '{image}' into DinD: image not found in registry. "
                    f"If this is a local image, ensure it exists on the host Docker daemon."
                ) from e
            raise

    async def _cache_dind_image_to_registry(
        self, range_id: str, docker_url: str, image: str
    ) -> bool:
        """Export image from DinD and push to registry for future use.

        This is used when an image was pulled from the internet into DinD.
        We export it and push to registry so future deployments use registry.

        Args:
            range_id: Range identifier
            docker_url: DinD Docker URL (tcp://ip:port)
            image: Image name/tag to cache

        Returns:
            True if successfully cached to registry
        """
        try:
            from proving_ground.services.registry_service import get_registry_service

            registry = get_registry_service()

            # Check if registry is healthy
            if not await registry.is_healthy():
                logger.warning("Registry not healthy, skipping DinD image caching")
                return False

            # Check if image already exists in registry
            if await registry.image_exists(image):
                logger.info(f"Image '{image}' already in registry, skipping cache")
                return True

            # Get the range client for DinD
            range_client = self.get_range_client_sync(range_id, docker_url)

            # Get the image from DinD
            try:
                dind_image = range_client.images.get(image)
            except docker.errors.ImageNotFound:
                logger.warning(f"Image '{image}' not found in DinD, cannot cache")
                return False

            # Export the image from DinD (as tar stream)
            logger.info(f"Exporting image '{image}' from DinD for registry caching")
            image_data = dind_image.save(named=True)

            # Load to host Docker daemon temporarily
            logger.info(f"Loading image '{image}' to host for registry push")
            loaded_images = self.client.images.load(image_data)
            if not loaded_images:
                logger.warning(f"Failed to load image '{image}' to host")
                return False

            # Push to registry and cleanup host
            logger.info(f"Pushing image '{image}' to registry")
            try:
                await registry.push_and_cleanup(image, progress_callback=None)
                logger.info(f"Successfully cached '{image}' to registry")
                return True
            except Exception as push_err:
                logger.warning(f"Failed to push '{image}' to registry: {push_err}")
                # Clean up the loaded image from host
                try:
                    self.client.images.remove(image, force=False)
                except Exception:
                    pass  # Ignore cleanup errors
                return False

        except ImportError:
            logger.warning("Registry service not available, skipping DinD image caching")
            return False
        except Exception as e:
            logger.warning(f"Error caching DinD image to registry: {type(e).__name__}: {e}")
            return False

    async def create_snapshot_dind(
        self,
        range_id: str,
        docker_url: str,
        container_id: str,
        snapshot_name: str,
        labels: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Create a snapshot (Docker image) from a container inside DinD.

        Returns:
            Image ID
        """
        range_client = self.get_range_client_sync(range_id, docker_url)
        try:
            container = range_client.containers.get(container_id)
            image = container.commit(
                repository=snapshot_name,
                tag="latest",
                message=f"Snapshot of {container.name}",
                conf={"Labels": labels or {}},
            )
            logger.info(f"Created DinD snapshot: {snapshot_name} ({image.id[:12]})")
            return image.id
        except NotFound as exc:
            raise ValueError(f"Container not found in DinD: {container_id}") from exc
        except APIError as e:
            logger.error(f"Failed to create DinD snapshot: {e}")
            raise

    async def copy_to_container_dind(
        self, range_id: str, docker_url: str, container_id: str, src_path: str, dst_path: str
    ) -> bool:
        """
        Copy a file to a container inside DinD.

        Args:
            range_id: Range identifier
            docker_url: Docker URL for the range's DinD
            container_id: Container ID
            src_path: Local source file path
            dst_path: Destination path in container

        Returns:
            True if successful
        """
        import os
        import tarfile
        import io

        range_client = self.get_range_client_sync(range_id, docker_url)

        try:
            container = range_client.containers.get(container_id)

            # Create tar archive with the file
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w") as tar:
                tar.add(src_path, arcname=os.path.basename(src_path))
            data.seek(0)

            # Copy to container
            container.put_archive(dst_path, data)
            logger.info(f"Copied {src_path} to {container_id}:{dst_path} in DinD")
            return True

        except NotFound as exc:
            raise ValueError(f"Container not found in DinD: {container_id}") from exc
        except APIError as e:
            logger.error(f"Failed to copy to container in DinD: {e}")
            raise

    def exec_in_range_container_dind(
        self,
        range_id: str,
        docker_url: str,
        container_id: str,
        command: str,
        user: str = "root",
    ) -> tuple[int, str]:
        """
        Execute a command in a container running inside DinD.

        Args:
            range_id: Range UUID
            docker_url: DinD Docker URL (tcp://host:port)
            container_id: Container ID inside DinD
            command: Command to execute
            user: User to run command as (default: root)

        Returns:
            Tuple of (exit_code, output)
        """
        range_client = self.get_range_client_sync(range_id, docker_url)

        try:
            container = range_client.containers.get(container_id)
            exec_result = container.exec_run(command, user=user, demux=True)
            stdout = exec_result.output[0] or b""
            stderr = exec_result.output[1] or b""
            output = (stdout + stderr).decode("utf-8", errors="replace")
            return exec_result.exit_code, output

        except NotFound as exc:
            raise ValueError(f"Container not found in DinD: {container_id}") from exc
        except APIError as e:
            logger.error(f"Failed to exec in container in DinD: {e}")
            raise

    def set_linux_user_password_dind(
        self, range_id: str, docker_url: str, container_id: str, username: str, password: str
    ) -> bool:
        """
        Set the password for a Linux user in a container running inside DinD.
        """
        try:
            command = f"/bin/sh -c 'echo \"{username}:{password}\" | chpasswd'"
            exit_code, output = self.exec_in_range_container_dind(
                range_id, docker_url, container_id, command, user="root"
            )
            if exit_code == 0:
                logger.info(
                    f"Set password for user {username} in DinD container {container_id[:12]}"
                )
                return True
            else:
                logger.warning(f"Failed to set password for {username} in DinD: {output}")
                return False
        except Exception as e:
            logger.warning(
                f"Failed to set password for {username} in DinD {container_id[:12]}: {e}"
            )
            return False

    def grant_sudo_privileges_dind(
        self,
        range_id: str,
        docker_url: str,
        container_id: str,
        username: str,
        nopasswd: bool = False,
    ) -> bool:
        """
        Grant sudo privileges to a Linux user in a container running inside DinD.
        """
        try:
            if nopasswd:
                sudoers_line = f"{username} ALL=(ALL) NOPASSWD: ALL"
            else:
                sudoers_line = f"{username} ALL=(ALL) ALL"

            command = f"/bin/sh -c 'echo \"{sudoers_line}\" >> /etc/sudoers'"
            exit_code, output = self.exec_in_range_container_dind(
                range_id, docker_url, container_id, command, user="root"
            )
            if exit_code == 0:
                logger.info(
                    f"Granted sudo privileges to {username} in DinD container {container_id[:12]}"
                )
                return True
            else:
                logger.warning(f"Failed to grant sudo to {username} in DinD: {output}")
                return False
        except Exception as e:
            logger.warning(f"Failed to grant sudo to {username} in DinD {container_id[:12]}: {e}")
            return False


# Singleton instance
_docker_service: Optional[DockerService] = None


def get_docker_service() -> DockerService:
    """Get the Docker service singleton."""
    global _docker_service
    if _docker_service is None:
        _docker_service = DockerService()
    return _docker_service
