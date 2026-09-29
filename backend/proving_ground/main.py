# backend/proving_ground/main.py
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from proving_ground.config import get_settings
from proving_ground.services.docker_service import DOCKER_SDK_ERROR
from proving_ground.api import kubernetes_ranges
from proving_ground.api.auth import router as auth_router
from proving_ground.api.users import router as users_router
from proving_ground.api.ranges import router as ranges_router
from proving_ground.api.networks import router as networks_router
from proving_ground.api.vms import router as vms_router
from proving_ground.api.websocket import router as websocket_router
from proving_ground.api.kubernetes_console import router as kubernetes_console_router
from proving_ground.api.feedback import router as feedback_router
from proving_ground.api.kubernetes_apps import router as kubernetes_apps_router
from proving_ground.api.artifacts import router as artifacts_router
from proving_ground.api.snapshots import router as snapshots_router
from proving_ground.api.events import router as events_router
from proving_ground.api.connections import router as connections_router
from proving_ground.api.msel import router as msel_router
from proving_ground.api.walkthrough import router as walkthrough_router
from proving_ground.api.cache import router as cache_router
from proving_ground.api.system import router as system_router
from proving_ground.api.capabilities import router as capabilities_router
from proving_ground.api.blueprints import router as blueprints_router
from proving_ground.api.instances import router as instances_router
from proving_ground.api.scenarios import router as scenarios_router
from proving_ground.api.admin import router as admin_router
from proving_ground.api.files import router as files_router
from proving_ground.api.content import router as content_router
from proving_ground.api.training_events import router as training_events_router
from proving_ground.api.images import router as images_router
from proving_ground.api.notifications import router as notifications_router
from proving_ground.api.catalog import router as catalog_router
from proving_ground.api.registry import router as registry_router

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events for startup and shutdown."""
    # Before anything else: an install that kept a credential this repository ships is not an
    # install, and finding out at startup beats finding out from an incident.
    from proving_ground.config import require_production_secrets

    require_production_secrets(settings)

    # The capability contract is the licence boundary (ADR-0011) and imports nothing from the
    # engine, so the engine hands it this cluster's pod and service CIDRs rather than the
    # contract reaching out for settings. Without this the guard keeps k3s's ranges, which are
    # wrong on every other distribution in both directions.
    from proving_ground.capability.networking import set_reserved_cidrs

    set_reserved_cidrs(settings.cluster_reserved_cidrs)

    # Startup
    from proving_ground.services.event_broadcaster import get_connection_manager, get_broadcaster
    from proving_ground.services.scenario_filesystem import get_scenarios_dir

    # Log scenarios directory (filesystem-based, populated via catalog install)
    scenarios_dir = get_scenarios_dir()
    logger.info(f"Scenarios directory: {scenarios_dir} (exists: {scenarios_dir.exists()})")

    logger.info("Starting real-time event services...")
    connection_manager = get_connection_manager()
    await connection_manager.start()

    broadcaster = get_broadcaster()
    await broadcaster.connect()

    logger.info("Real-time event services started")

    # Seed the warm range pool (C1): refill_pool_task is otherwise only
    # ever enqueued after a successful claim or a teardown, so an empty
    # pool at boot (e.g. a fresh deploy) would never bootstrap itself -
    # see range_pool_service.py's module docstring. A broker hiccup here
    # must not stop the API from booting.
    if settings.range_pool_enabled:
        try:
            from proving_ground.tasks.pool import refill_pool_task

            refill_pool_task.send()
            logger.info("Enqueued initial warm pool refill")
        except Exception as e:
            logger.warning(f"Could not enqueue initial pool refill at startup: {e}")

    yield

    # Shutdown
    logger.info("Stopping real-time event services...")
    await connection_manager.stop()
    await broadcaster.disconnect()
    logger.info("Real-time event services stopped")


# The product runs on one of two substrates and they do not describe themselves the same
# way. One text naming Docker, containers and a create-range / add-networks / add-VMs quick
# start is wrong on a Kubernetes install in the one document a reader consults to find out
# what is true -- and every step of that quick start is refused there.
_KUBERNETES = settings.range_substrate == "kubernetes"

_CONCEPTS_KUBERNETES = """
# PROVING GROUND - Cyber Range Orchestrator

PROVING GROUND is a platform for training people on capabilities, using disposable,
mission-relevant environments. **This install runs ranges on Kubernetes.**

## Concepts

- **Blueprint**: what a range is instantiated from -- its networks, its workloads and its
  capabilities. On this substrate it is the only way a range is composed.
- **Range**: a placement decision -- a namespace, or a virtual cluster, holding one instance of
  a blueprint
- **Workload**: a machine in the range, run by KubeVirt, attached to the range's networks
- **Capability**: the software being trained against, installed from an unmodified upstream Helm
  chart, with seed / reset / verify hooks and a scope

## Quick Start

1. **List blueprints**: `GET /api/v1/blueprints`
2. **Deploy an instance**: `POST /api/v1/blueprints/{id}/deploy`
3. **Watch it come up**: `GET /api/v1/ranges/{id}/workloads`
4. **Open a console**: the WebSocket endpoints or the UI
5. **Tear it down**: `DELETE /api/v1/ranges/{id}`

Creating networks and virtual machines directly is a Docker-substrate path and is refused here:
a range's composition is declared in its blueprint.
"""

_CONCEPTS_DOCKER = """
# PROVING GROUND - Cyber Range Orchestrator In Docker

PROVING GROUND is a platform for creating and managing cyber training ranges using Docker containers and VMs.

## Concepts

- **Range**: A complete training environment containing networks and VMs
- **Network**: An isolated network segment (e.g., 172.16.0.0/24) for VM communication
- **VM**: A virtual machine (container or QEMU VM) running in the range
- **Image Library**: Three-tier image management (Base Images, Golden Images, Snapshots)

## Quick Start

1. **Create a range**: `POST /api/v1/ranges`
2. **Add networks**: `POST /api/v1/networks`
3. **Add VMs**: `POST /api/v1/vms`
4. **Deploy**: `POST /api/v1/ranges/{id}/deploy`
5. **Access consoles**: Use the WebSocket endpoints or UI

"""


def _concepts(kubernetes: bool) -> str:
    """The half of the description that differs by substrate. A function, not a branch at
    import: a test that wants the other answer would otherwise have to reload this module and
    clear the settings cache, and clearing that cache hands every module that captured
    `get_settings()` at import a different object than the one a later test patches."""
    return _CONCEPTS_KUBERNETES if kubernetes else _CONCEPTS_DOCKER


API_DESCRIPTION = _concepts(_KUBERNETES) + """
## Authentication

All endpoints (except `/health` and `/api/v1/auth/*`) require a JWT token.
Include it in the `Authorization` header: `Bearer <token>`

## API Documentation

- **Swagger UI**: `/docs` (interactive API explorer)
- **ReDoc**: `/redoc` (alternative documentation view)
- **OpenAPI JSON**: `/openapi.json` (machine-readable schema)
- **AI Context**: `/api/v1/schema/ai-context` (condensed guide for AI assistants)

## Health Checks

- `/health` - Basic health check
- `/api/health` - API health check (alias)
- `/api/v1/health` - Versioned health check (alias)
"""

app = FastAPI(
    title=settings.app_name,
    description=API_DESCRIPTION,
    version=settings.app_version,
    lifespan=lifespan,
    # Interactive docs are gated on debug. They are unauthenticated by nature:
    # anyone who can reach the host gets the full API surface, including the
    # destructive range endpoints. That is fine on a developer machine and not
    # fine on an internet-reachable deployment, which is what pg-ec2 is.
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
    openapi_tags=[
        {"name": "auth", "description": "Authentication and user management"},
        {"name": "users", "description": "User account management"},
        {"name": "ranges", "description": "Range lifecycle management"},
        {"name": "networks", "description": "Network configuration"},
        {"name": "vms", "description": "Virtual machine management"},
        {"name": "artifacts", "description": "File and artifact management"},
        {"name": "snapshots", "description": "VM snapshot management"},
        {"name": "events", "description": "Event logging and monitoring"},
        {"name": "msel", "description": "Master Scenario Events List"},
        {"name": "scenarios", "description": "Training scenarios for cyber exercises"},
        {"name": "walkthrough", "description": "Lab walkthrough and student progress"},
        {"name": "content", "description": "Training content and materials"},
        {"name": "training-events", "description": "Training event scheduling and management"},
        {"name": "Notifications", "description": "User-scoped notifications and alerts"},
        {"name": "catalog", "description": "Content catalog browsing and installation"},
        {"name": "system", "description": "System configuration and status"},
    ],
)

# Parse CORS origins from config (comma-separated string or "*" for all)
cors_origins = settings.cors_origins
if cors_origins == "*":
    cors_allow_origins = ["*"]
else:
    cors_allow_origins = [origin.strip() for origin in cors_origins.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(DOCKER_SDK_ERROR)
async def docker_unavailable(request: Request, exc: Exception) -> JSONResponse:
    """Answer a request that needed a Docker daemon and could not have one.

    Around thirty routes build a client the moment they are called -- the
    infrastructure and system pages, the whole image cache, range validate. On
    the Kubernetes substrate there is no socket to build it against, so all of
    them answered a bare `Internal Server Error` with nothing in the body, and
    the one fact that resolves the confusion -- that this install has no Docker
    -- was legible only to someone holding kubectl.

    Gating those routers per substrate is the real fix. This is the floor
    beneath it, so that a route nobody has reached yet still refuses in a way
    the person reading it can act on, and so that a Docker host which loses its
    socket says so rather than looking like a bug in the route.
    """
    # Starlette routes a websocket endpoint's exception to this same handler,
    # and a WebSocket carries no `method` -- reading one here would replace the
    # failure with an AttributeError raised inside the handler itself.
    route = f"{request.scope.get('method', 'WS')} {request.url.path}"
    if kubernetes_ranges.is_kubernetes():
        logger.warning("Docker SDK error on %s: %s", route, exc)
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={
                "detail": (
                    "This install runs on the Kubernetes substrate. "
                    f"{route} belongs to the Docker substrate and is not available here."
                )
            },
        )
    # On a Docker host the same exception is a fault rather than an absence.
    # Starlette logged the traceback itself while nothing handled it; handling
    # it here has to keep that, or the only record of which call failed is gone.
    # The detail carries the SDK's own message because the cause is not always a
    # missing socket -- a daemon that answers and refuses raises from the same
    # tree, and "could not reach it" would be a false explanation.
    logger.error("Docker SDK error on %s: %s", route, exc, exc_info=exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "detail": (
                f"{route} needs the Docker daemon and the call failed: {exc}. Check that "
                "the daemon is running and that the API can reach its socket."
            )
        },
    )


# Include routers
app.include_router(auth_router, prefix="/api/v1")
app.include_router(feedback_router, prefix="/api/v1")
app.include_router(users_router, prefix="/api/v1")
app.include_router(ranges_router, prefix="/api/v1")
app.include_router(networks_router, prefix="/api/v1")
app.include_router(vms_router, prefix="/api/v1")
app.include_router(websocket_router, prefix="/api/v1")
app.include_router(kubernetes_console_router, prefix="/api/v1")
app.include_router(kubernetes_apps_router, prefix="/api/v1")
app.include_router(artifacts_router, prefix="/api/v1")
app.include_router(snapshots_router, prefix="/api/v1")
app.include_router(events_router, prefix="/api/v1")
app.include_router(connections_router, prefix="/api/v1")
app.include_router(msel_router, prefix="/api/v1")
app.include_router(walkthrough_router, prefix="/api/v1")
app.include_router(cache_router, prefix="/api/v1")
app.include_router(system_router, prefix="/api/v1")
app.include_router(capabilities_router, prefix="/api/v1")
app.include_router(blueprints_router, prefix="/api/v1")
app.include_router(instances_router, prefix="/api/v1")
app.include_router(scenarios_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
app.include_router(files_router, prefix="/api/v1")
app.include_router(content_router, prefix="/api/v1")
app.include_router(training_events_router, prefix="/api/v1")
app.include_router(images_router, prefix="/api/v1")
app.include_router(notifications_router, prefix="/api/v1")
app.include_router(catalog_router, prefix="/api/v1")
app.include_router(registry_router, prefix="/api/v1")


@app.get("/health")
@app.get("/api/health")
@app.get("/api/v1/health")
async def health_check():
    """Health check endpoint for load balancers and monitoring."""
    return {"status": "healthy", "app": settings.app_name}


@app.get("/api/v1/version")
async def get_version():
    """Return application version information."""
    return {
        "version": settings.app_version,
        "commit": settings.git_commit,
        "build_date": settings.build_date,
        "api_version": "v1",
        "app_name": settings.app_name,
    }


@app.get("/api/v1/branding")
async def get_branding():
    """What this deployment calls itself.

    **Deliberately unauthenticated.** The sign-in page renders the product name
    before anyone has credentials, so gating this would leave the first screen
    anybody sees unable to name the product.

    Nothing here is sensitive: it is the name, the tagline and optional colour
    overrides — the same things printed on the page that serves it.
    """
    return {
        "product_name": settings.branding_product_name,
        "tagline": settings.branding_tagline,
        "primary_palette": settings.branding_primary_palette,
    }


_AI_OVERVIEW_KUBERNETES = """# PROVING GROUND API Quick Reference (for AI Assistants)

## Overview
This install runs ranges on Kubernetes. A range is an instance of a blueprint, placed in a
namespace (or a virtual cluster); its machines are KubeVirt workloads on Multus network
attachments, and the software being trained against is installed from an unmodified upstream
Helm chart with seed / reset / verify hooks.

## Core Workflow
1. GET  /api/v1/blueprints - List blueprints
2. POST /api/v1/blueprints/{id}/deploy - Deploy an instance; this is how a range is created
3. GET  /api/v1/ranges/{id}/workloads - Machines, their phase and their addresses
4. POST /api/v1/ranges/{id}/start | /stop - Start or stop the range's machines
5. DELETE /api/v1/ranges/{id} - Destroy the namespace and everything in it

The Docker-substrate endpoints below (networks, VMs, the image cache, snapshots) are refused on
this install with 501 or 409: composition is declared in the blueprint.
"""

_AI_OVERVIEW_DOCKER = """# PROVING GROUND API Quick Reference (for AI Assistants)

## Overview
PROVING GROUND creates Docker-based cyber training ranges with isolated networks and VMs.
Uses a three-tier Image Library: Base Images (containers/ISOs), Golden Images (configured VMs), Snapshots (forks).

## Core Workflow
1. POST /api/v1/ranges - Create range (name, description)
2. POST /api/v1/networks - Add networks to range (name, subnet, gateway, is_isolated)
3. POST /api/v1/vms - Add VMs to range (hostname, base_image_id, network_id, ip_address)
4. POST /api/v1/ranges/{id}/deploy - Deploy the range
5. POST /api/v1/ranges/{id}/start - Start a stopped range
6. POST /api/v1/ranges/{id}/stop - Stop a running range
7. POST /api/v1/ranges/{id}/teardown - Destroy and reset to draft

"""


def _ai_overview(kubernetes: bool) -> str:
    """As `_concepts`, for the guide an assistant reads before generating calls."""
    return _AI_OVERVIEW_KUBERNETES if kubernetes else _AI_OVERVIEW_DOCKER


AI_CONTEXT = _ai_overview(_KUBERNETES) + """
## Key Endpoints

### Ranges
- GET /api/v1/ranges - List all ranges
- POST /api/v1/ranges - Create range {"name": "string", "description": "string"}
- GET /api/v1/ranges/{id} - Get range details
- DELETE /api/v1/ranges/{id} - Delete range

### Networks
- GET /api/v1/networks?range_id={id} - List networks in range
- POST /api/v1/networks - Create network
  ```json
  {
    "range_id": "uuid",
    "name": "internal",
    "subnet": "172.16.1.0/24",
    "gateway": "172.16.1.1",
    "is_isolated": true
  }
  ```

### VMs
- GET /api/v1/vms?range_id={id} - List VMs in range
- POST /api/v1/vms - Create VM (use base_image_id, golden_image_id, or snapshot_id)
  ```json
  {
    "range_id": "uuid",
    "base_image_id": "uuid",
    "network_id": "uuid",
    "hostname": "webserver",
    "ip_address": "172.16.1.10",
    "cpu": 2,
    "ram_mb": 2048
  }
  ```
- POST /api/v1/vms/{id}/start - Start VM
- POST /api/v1/vms/{id}/stop - Stop VM
- POST /api/v1/vms/{id}/networks/{network_id}?ip_address=x.x.x.x - Add network interface

### Image Library (VM Library)
- GET /api/v1/cache/base-images - List base images (containers, ISOs)
- GET /api/v1/cache/golden-images - List golden images (configured VMs)
- GET /api/v1/cache/snapshots - List snapshots (VM forks)
- POST /api/v1/cache/pull - Pull Docker image to cache
- POST /api/v1/cache/build/{project} - Build Dockerfile from /data/images/{project}/

### Blueprints (Reusable Range Templates)
- GET /api/v1/blueprints - List all blueprints
- POST /api/v1/blueprints - Create blueprint from existing range
- POST /api/v1/blueprints/{id}/deploy - Deploy new instance from blueprint
- GET /api/v1/blueprints/{id}/export - Export blueprint (with Dockerfiles, MSEL, content)
- POST /api/v1/blueprints/import - Import blueprint from export file

## Network Isolation Modes
- is_isolated=false: Network has internet access via VyOS NAT router
- is_isolated=true: Air-gapped network, no external access

## VM Types (based on image)
- Linux containers (KasmVNC for GUI, Docker exec for terminal)
- Windows VMs (via dockur/windows, VNC console)
- Linux VMs (via QEMU ISO boot, VNC console)

## Common Patterns

### Red Team Lab
```json
{
  "networks": [
    {"name": "internet", "subnet": "172.16.0.0/24", "is_isolated": false},
    {"name": "dmz", "subnet": "172.16.1.0/24", "is_isolated": true},
    {"name": "internal", "subnet": "172.16.2.0/24", "is_isolated": true}
  ],
  "vms": [
    {"hostname": "kali", "network": "internet", "base_image_tag": "proving_ground/kali-attack:latest"},
    {"hostname": "webserver", "network": "dmz", "base_image_tag": "proving_ground/redteam-lab-wordpress:latest"},
    {"hostname": "dc01", "network": "internal", "base_image_tag": "proving_ground/samba-dc:latest"}
  ]
}
```

## Authentication
All API calls require: `Authorization: Bearer <jwt_token>`
Get token via: POST /api/v1/auth/login {"username": "x", "password": "y"}

## Status Values
- Range: draft, deploying, running, stopped, error
- VM: pending, creating, running, stopped, error
- Network: pending, provisioned

## Tips
- Always deploy range after adding all networks and VMs
- Use base_image_id (UUID) or base_image_tag (docker tag) to specify VM image
- IP addresses must be within the network's subnet
- VMs can have multiple network interfaces via POST /vms/{id}/networks/{network_id}
- Use Blueprints for reusable range configurations
"""


@app.get("/api/v1/schema/ai-context", tags=["system"])
async def get_ai_context():
    """
    Get a condensed API guide optimized for AI assistants.

    This endpoint returns a markdown document that provides AI coding assistants
    (Claude, GPT, Copilot, etc.) with the context needed to generate valid
    PROVING GROUND API calls without access to source code.
    """
    return {
        "content": AI_CONTEXT,
        "format": "markdown",
        "version": settings.app_version,
    }
