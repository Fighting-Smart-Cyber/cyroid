# backend/proving_ground/config.py
import os
import platform
import subprocess
from pydantic_settings import BaseSettings
from pydantic import field_validator
from functools import lru_cache


def _get_default_data_dir() -> str:
    """Get platform-appropriate data directory.

    On macOS, use ~/.proving_ground for Docker Desktop file sharing compatibility.
    On Linux, use /data/proving-ground for production deployments.
    """
    if platform.system() == "Darwin":
        # macOS - use home directory for Docker Desktop compatibility
        return os.path.expanduser("~/.proving_ground")
    else:
        # Linux - use /data/proving-ground (production)
        return "/data/proving-ground"


def _get_version() -> str:
    """Get version from VERSION file, env var, git tag, or fallback to 'dev'."""
    # First check environment variable (for Docker builds)
    # Skip if it's empty or the default "dev" value from Dockerfile
    if (version := os.environ.get("APP_VERSION")) and version not in ("", "dev"):
        return version

    # Try VERSION file locations (in order of preference)
    version_paths = [
        "/etc/proving-ground-version",  # Dev mode mount
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION"),  # backend/VERSION
    ]
    for version_file in version_paths:
        try:
            with open(version_file) as f:
                if version := f.read().strip():
                    return version
        except (FileNotFoundError, IOError):
            pass

    # Try git tag (works in local dev without Docker)
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            tag = result.stdout.strip()
            # Strip 'v' prefix if present (v0.9.0 -> 0.9.0)
            return tag.lstrip("v")
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    return "dev"


class Settings(BaseSettings):
    # Application version (from VERSION file, APP_VERSION env, git tag, or "dev")
    app_version: str = "dev"
    git_commit: str = os.environ.get("GIT_COMMIT", "dev")
    build_date: str = os.environ.get("BUILD_DATE", "")

    @field_validator("app_version", mode="after")
    @classmethod
    def resolve_app_version(cls, v: str) -> str:
        """If version is 'dev' or empty, try to get actual version from file/git."""
        if v in ("", "dev"):
            return _get_version()
        return v

    # Database
    database_url: str = "postgresql://proving_ground:proving_ground@db:5432/proving_ground"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # MinIO
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "proving_ground"
    minio_secret_key: str = "proving_ground123"
    minio_bucket: str = "proving-ground-artifacts"
    minio_secure: bool = False

    # JWT
    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # App
    app_name: str = "PROVING GROUND"
    debug: bool = True

    # CORS (comma-separated list of allowed origins, or "*" for all)
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # Image/ISO Cache (platform-aware defaults)
    iso_cache_dir: str = os.path.join(_get_default_data_dir(), "iso-cache")
    template_storage_dir: str = os.path.join(_get_default_data_dir(), "template-storage")

    # VM Storage (platform-aware defaults)
    vm_storage_dir: str = os.path.join(_get_default_data_dir(), "vm-storage")
    global_shared_dir: str = os.path.join(_get_default_data_dir(), "shared")

    # Catalog
    catalog_storage_dir: str = os.path.join(_get_default_data_dir(), "catalogs")

    # === Image namespace ===
    # Locally-built catalog images are tagged "<image_namespace>/<name>".
    # legacy_image_namespaces are ALSO recognized when matching or resolving
    # images, so content from upstream catalogs (e.g. cyroid-catalog, whose
    # built images are tagged "proving_ground/...") keeps working after the rename.
    # Override the primary namespace with the IMAGE_NAMESPACE env var.
    image_namespace: str = "proving-ground"
    legacy_image_namespaces: list[str] = ["cyroid"]

    # VyOS Router Configuration
    #
    # Only the legacy, non-DinD deploy path (deploy_range_task_legacy) uses
    # these. That task is not dispatched by any endpoint today, so the network
    # below will not exist on a running host - vyos_service creates it on
    # demand when a router is first built. Its absence is expected, not a bug.
    vyos_image: str = "2stacks/vyos:1.2.0-rc11"
    management_network_name: str = "pg-management"
    management_network_subnet: str = "10.0.0.0/16"
    management_network_gateway: str = "10.0.0.1"

    # === DinD (Docker-in-Docker) Configuration ===
    # Each range runs in its own DinD container for network isolation
    dind_image: str = "ghcr.io/jongodb/cyroid-dind:latest"
    dind_startup_timeout: int = 60  # Seconds to wait for inner Docker daemon
    dind_docker_port: int = 2375  # Docker daemon port inside DinD

    # === Network Configuration ===
    # Management network for PROVING GROUND infrastructure services
    proving_ground_mgmt_network: str = "pg-mgmt"
    proving_ground_mgmt_subnet: str = "172.30.0.0/24"

    # Network for range DinD containers (ranges connect here)
    proving_ground_ranges_network: str = "pg-ranges"
    proving_ground_ranges_subnet: str = "172.30.1.0/24"

    # === Range Defaults ===
    range_default_memory: str = "8g"  # Default memory limit for range DinD
    range_default_cpu: float = 4.0  # Default CPU limit for range DinD

    # === DinD Isolation ===
    # All ranges deploy inside DinD containers for complete IP isolation
    # This allows multiple ranges to use identical IP spaces without conflicts

    deploy_queue_name: str = "deploys"  # Dramatiq queue for range deploys; throttled separately

    # === Warm Range Pool ===
    # Pre-boots and pre-images idle DinD containers so a deploy can claim a
    # ready-made one instead of paying the full cold-provisioning cost.
    # A claimed container is renamed into the range and, per the security
    # invariant, is destroyed (never returned to the pool) when the range
    # ends - see range_pool_service.py.
    range_pool_enabled: bool = True
    range_pool_size: int = 2
    range_pool_images: list[str] = []  # empty = union of images across blueprints
    range_pool_member_ttl_hours: int = 24

    class Config:
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def infrastructure_network_names() -> set[str]:
    """Docker networks owned by the platform itself.

    These must never be treated as range networks: they carry the platform's own
    services, and deleting them takes the platform down. Derived from config so
    protection follows a rename instead of drifting out of sync with it.
    """
    s = get_settings()
    return {
        s.proving_ground_mgmt_network,
        s.proving_ground_ranges_network,
        s.management_network_name,
        "proving_ground_default",
    }
