# backend/proving_ground/config.py
import os
import platform
import subprocess
from pydantic_settings import BaseSettings
from pydantic import field_validator
from functools import lru_cache
from typing import ClassVar


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
    # The content bucket was a bare literal in two places in api/content.py with no setting
    # behind it, so an environment that pre-provisions its buckets could not name them. The
    # DEFAULT is a catalog integration point and does not change (CLAUDE.md); what changes is
    # that it is now nameable.
    minio_content_bucket: str = "proving-ground-content"
    minio_secure: bool = False
    # Whether PG may create a bucket it does not find. True suits the bundled MinIO; an
    # identity scoped to a pre-provisioned bucket -- IRSA, Workload Identity, an IL4 tenant --
    # has no s3:CreateBucket and should run with this off rather than failing at startup.
    minio_create_bucket: bool = True

    # The cluster's own pod and service CIDRs. A range network overlapping these is not an
    # isolated network -- it is intermittent, hard-to-attribute breakage in whichever the kernel
    # routes first. The DEFAULTS are k3s's, which is where this started; every other cluster
    # chooses its own and the chart passes them. AKS kubenet defaults to 10.244.0.0/16 pods and
    # 10.0.0.0/16 services; on Azure CNI the pod addresses come from the VNet, so on that profile
    # the VNet range belongs here too.
    cluster_reserved_cidrs: str = "10.42.0.0/16,10.43.0.0/16"

    # JWT. The default is a known string, so it is not a secret at all -- anyone holding it can
    # forge a token for any user on any install that kept it, and the `typ` typing that stops a
    # ticket being a session is moot against a forged one. `require_production_secrets()` refuses
    # to start with it when `debug` is off; the chart and scripts/deploy.sh both generate one.
    JWT_DEFAULT_SECRET: ClassVar[str] = "change-me-in-production"
    jwt_secret_key: str = JWT_DEFAULT_SECRET
    jwt_algorithm: str = "HS256"

    # The one-use handoff CODE that crosses from the platform's origin to a range's own is
    # signed with a key pg-gateway does NOT hold, so the most exposed process on the platform can
    # check it without being able to mint a console ticket or a platform access token. See
    # utils/app_tokens.py. The public half may be a public key PEM or a certificate PEM: the
    # chart mints the pair with Helm's genSelfSignedCert, which cannot emit a bare public key.
    app_token_private_key: str = ""
    app_token_public_key: str = ""
    app_token_algorithm: str = "RS256"

    # The range-application TICKET is a different problem and needs a different key.
    #
    # It is minted and verified by the same process -- pg-gateway redeems the code, sets the
    # cookie, and checks that cookie on every later request -- so it is not a cross-process
    # credential and asymmetry buys nothing. It was signed with the pair above anyway, and
    # because the gateway holds only the PUBLIC half it signed with the jwt_secret_key fallback
    # and then tried to verify with the public key. Every ticket it issued failed its own check,
    # so no range application could be opened at all (found on pg-devtest, 0.53.1).
    #
    # Reusing jwt_secret_key would work and would hand the gateway the ability to mint a platform
    # access token, which is the one thing this whole arrangement exists to prevent. So: its own
    # secret, shared with the API (which still serves the pre-gateway ForwardAuth path for ranges
    # deployed before the switch-over) and with nothing else. Unset, it falls back to
    # jwt_secret_key, which is what a developer install and the existing tests use.
    app_ticket_secret: str = ""
    jwt_expire_minutes: int = 60

    # App
    # The ENGINE's name, which is what an unconfigured install is. It reaches a public
    # downloader in three places -- the OpenAPI title at /docs, /api/v1/version, and the health
    # response -- so a default naming the distribution built on top of the engine is wrong in
    # all three. Same rule as `frontend/src/lib/branding.ts`, which already defaults to CYROID:
    # a distribution sets `APP_NAME` in its environment rather than editing this. (`APP_NAME`,
    # not `PROVING_GROUND_APP_NAME` -- this Settings class declares no env_prefix, so the
    # variable is the field name. Verified, because the obvious guess is wrong.)
    #
    # Renaming this is allowed where renaming the catalog-facing identifiers is not: nothing
    # resolves against it. The environment variable KEY is unchanged, as CLAUDE.md requires.
    app_name: str = "CYROID"
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
    # Which execution substrate a range deploys onto.
    #   "dind"        Era A -- the frozen Docker-in-Docker path (default, unchanged behaviour)
    #   "kubernetes"  Era B -- placement by policy, capabilities through CapabilityRuntime
    # Per-host and opt-in: a host that does not set it keeps the path it has always had.
    range_substrate: str = "dind"
    # Namespaces a range's default-deny NetworkPolicy lets in: where PROVING GROUND's own pods
    # and Flux's helm-controller run, both of which must reach a vcluster's API server inside
    # the range. Comma-separated. Empty on the DinD path, where there is no policy to speak of.
    control_plane_namespaces: str = ""
    # Student-facing ingress for a range's applications (PG-62). Empty class = no routing.
    # The authz URL is where the cluster's ingress asks, per request, whether the browser may
    # reach this range's application; the ingress controller's pods are let through the range's
    # default-deny by namespace + labels.
    range_ingress_class: str = ""
    # The DNS suffix a range's applications answer on -- "apps.example.com", bare, no scheme --
    # under which each range gets a label of its own: "<range-id>.apps.example.com".
    #
    # Empty is the default and it publishes no range application at all. A range's application
    # is upstream software the learner is being trained against; on a real deployment it is
    # third party, and in a red-team exercise it is hostile by design. Served from the
    # platform's own host it is same-origin with the console: the JavaScript it serves reads the
    # operator's token out of localStorage and drives the API as whoever opened it. Nothing
    # inside the platform can take that back, so an install that has not been given a second
    # origin gets no applications rather than applications on this one. Do not default it to the
    # platform's own host to make a demo work.
    range_apps_host: str = ""
    # An optional sub-path beneath that host. Empty is right for almost everyone: the host
    # already names the range, so "/<app>" is the whole path.
    range_ingress_path_prefix: str = ""
    range_ingress_authz_url: str = ""
    ingress_controller_namespace: str = "kube-system"
    ingress_controller_pod_labels: str = "app.kubernetes.io/name=traefik"
    # In-cluster updates (api/kubernetes_update.py): the OCI repository CI publishes the chart
    # to, the pull secret mounted into the pod, and the HelmRelease that is this install.
    chart_repository: str = ""
    # Which chart repositories a blueprint's capabilities may name (comma-separated; an entry
    # covers everything beneath it). Empty permits none, which is the conservative answer and the
    # one air-gap asks for (ADR-0007): anybody who can author a blueprint could otherwise choose
    # the chart helm-controller renders and applies with the permissions Flux holds. The
    # repository above is permitted without being restated. The chart ships a default here so a
    # fresh install can deploy the blueprint it seeds; an air-gapped install replaces it with its
    # own mirror. See capability/specs.py:ChartRepositoryPolicy.
    capability_chart_repositories: str = ""
    # Which RBAC posture the chart installed: "cluster" (the control plane holds the range verbs
    # cluster-wide, as every install before 0.52.0 did) or "namespace" (it holds them only inside
    # each range, through a RoleBinding it writes as it creates the namespace). The pod has to
    # know, because in cluster mode that RoleBinding is redundant AND unpermitted -- attempting
    # it would log a 403 on every range creation for no gain.
    range_rbac_scope: str = "cluster"
    registry_credentials_file: str = "/var/run/pg/registry/.dockerconfigjson"
    pod_namespace: str = "pg-system"
    helm_release_name: str = "pg"

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

    # --- Branding -------------------------------------------------------
    # A distribution is a theme, a capability package and content over the
    # engine (ADR-0011). Read at runtime so standing up a distribution is a
    # configuration change rather than a rebuild of the frontend.
    #
    # The default is the ENGINE's name. A distribution sets these; PROVING
    # GROUND does so in its own environment. Shipping the engine defaulted to a
    # distribution's name is the inversion MIG-8 exists to undo.
    # Send the console ticket cookie only over TLS. True is correct for any
    # real deployment; a local http:// one must set it false or the browser
    # never returns the cookie and every console fails closed.
    console_cookie_secure: bool = True

    # Whether guests receive Hyper-V enlightenments when KVM is on.
    #
    #   "auto" (default)  pass them through, except on a host that is itself a
    #                     Hyper-V guest, where they are disabled.
    #   "on"              always pass through (dockur's own default).
    #   "off"             never.
    #
    # dockur sets HV_FEATURES="hv_passthrough" whenever KVM is enabled. On a
    # host that is ITSELF a Hyper-V guest -- any Azure VM -- handing those
    # enlightenments to a nested Windows guest makes it shut down seconds after
    # the boot manager starts, with nothing logged by QEMU or KVM. Measured on
    # pg-ubu: identical image, ISO and resources, HV default exits every time
    # and HV=false installs and runs for days.
    #
    # Only consulted when KVM is on; dockur ignores it under emulation.
    range_hyperv: str = "auto"

    @field_validator("range_hyperv", mode="after")
    @classmethod
    def validate_range_hyperv(cls, v: str) -> str:
        allowed = {"auto", "on", "off"}
        got = (v or "").strip().lower()
        if got not in allowed:
            raise ValueError(f"range_hyperv must be one of {sorted(allowed)}, got {v!r}")
        return got

    # Whether Windows/Linux guests get hardware virtualisation.
    #
    #   "off" (default)  no hardware acceleration, as the DinD path always did.
    #   "auto"           ask the range's own DinD container whether /dev/kvm is
    #                    there, because that is where the guest actually runs.
    #   "on"             force it, for a host where the probe is wrong.
    #
    # Defaults to "off", not "auto", and that is a deliberate retreat.
    #
    # The DinD path hardcoded KVM off for Docker Desktop and hosts without
    # nested virt, silently costing ~10x on every host that had it. Detection
    # fixes that. But a Windows golden image built under TCG does not survive
    # being handed real hardware virtualisation: measured on pg-ubu with a
    # pristine disk copied from the golden image, the guest boots and shuts
    # down again after ~17s, forever, and never reaches RDP.
    #
    # So "auto" as the default would break every existing Windows range on any
    # KVM-capable host the moment it was deployed. Turning KVM on is safe only
    # once the golden images for that host have been rebuilt under KVM, which
    # is a deliberate act, not a default.
    range_kvm: str = "off"

    @field_validator("range_kvm", mode="after")
    @classmethod
    def validate_range_kvm(cls, v: str) -> str:
        allowed = {"auto", "on", "off"}
        got = (v or "").strip().lower()
        if got not in allowed:
            raise ValueError(f"range_kvm must be one of {sorted(allowed)}, got {v!r}")
        return got

    # Which remote state the in-UI update follows.
    #
    #   "release"  the highest semver v* tag on the remote. The host lands on a
    #              commit someone deliberately released, so what is deployed is
    #              reproducible and app_version keeps describing the code.
    #   "branch"   the tip of the branch the host is on. Every merge is offered
    #              as an update. Right for a dev host that wants master
    #              continuously; wrong anywhere the version number is a claim.
    #
    # Defaults to "release" because that is the safe answer for a host nobody
    # is watching: it will not pull an unreleased master on its own.
    update_channel: str = "release"

    @field_validator("update_channel", mode="after")
    @classmethod
    def validate_update_channel(cls, v: str) -> str:
        allowed = {"release", "branch"}
        got = (v or "").strip().lower()
        if got not in allowed:
            # Loud, not silently coerced. A typo'd channel that quietly fell
            # back to a default would change what a host deploys without
            # anyone being told.
            raise ValueError(f"update_channel must be one of {sorted(allowed)}, got {v!r}")
        return got

    branding_product_name: str = "CYROID"
    branding_tagline: str = "Cyber Range Orchestrator"
    # Optional per-shade overrides of the primary palette, keyed by Tailwind
    # shade: {"600": "#2563eb"}. Empty means the built-in palette. Only the
    # shades given are overridden, so a distribution can change the one colour
    # it cares about without restating ten.
    branding_primary_palette: dict[str, str] = {}

    class Config:
        env_file = ".env"


class InsecureDefaultError(RuntimeError):
    """A default that is safe on a laptop and is not a secret anywhere else."""


def require_production_secrets(settings: "Settings") -> None:
    """Refuse to start an install that kept a shipped credential.

    `jwt_secret_key` ships as a known string so `./scripts/dev-setup.sh` works with no
    configuration. On anything else that string is public: it signs every session token, so
    holding it forges any user -- and the `typ` claim that stops a ticket being a session says
    nothing about a token that was forged rather than replayed.

    Refusing at startup rather than warning, because a warning in a container log is read once,
    after the incident. `debug` is what distinguishes the laptop: the chart sets it false, and
    both the chart and scripts/deploy.sh generate a real secret, so no install that follows the
    documented path can reach this.
    """
    if settings.debug:
        return
    if settings.jwt_secret_key == Settings.JWT_DEFAULT_SECRET:
        raise InsecureDefaultError(
            "JWT_SECRET_KEY is still the value this repository ships, which is public and "
            "therefore forges any session on this install. Set it to a generated secret "
            "(the Helm chart generates and keeps one; scripts/deploy.sh generates one), or "
            "set DEBUG=true if this really is a development install."
        )


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
