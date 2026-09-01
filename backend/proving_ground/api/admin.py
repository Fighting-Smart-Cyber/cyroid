# backend/proving_ground/api/admin.py
"""
Administrative API endpoints.

These endpoints require admin privileges and provide system-wide operations
like cleanup, diagnostics, and maintenance.
"""
import logging
import os
import platform
import re
import sys
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Optional

import psutil
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from proving_ground.api.deps import require_admin, get_db
from proving_ground.config import get_settings, infrastructure_network_names
from proving_ground.models.user import User
from proving_ground.models.range import Range, RangeStatus
from proving_ground.models.vm import VM, VMStatus
from proving_ground.models.network import Network
from proving_ground.models.blueprint import RangeInstance
from proving_ground.services.docker_service import get_docker_service
from proving_ground.services.dind_service import get_dind_service
from proving_ground.services import platform_secret_service
from proving_ground.models.platform_secret import GIT_CREDENTIAL_KEY
from proving_ground.schemas.infrastructure import (
    ServiceHealth,
    InfrastructureServicesResponse,
    LogEntry,
    ServiceLogsResponse,
    DockerContainerOverview,
    DockerNetworkOverview,
    DockerVolumeOverview,
    DockerImageOverview,
    DockerSummary,
    DockerOverviewResponse,
    HostMetrics,
    DatabaseMetrics,
    TaskQueueMetrics,
    StorageMetrics,
    InfrastructureMetricsResponse,
    MigrationInfo,
    ConfigItem,
    SystemInfoResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# Type aliases
DBSession = Annotated[Session, Depends(get_db)]
AdminUser = Annotated[User, Depends(require_admin())]


class CleanupResult(BaseModel):
    """Result of a cleanup operation."""

    ranges_cleaned: int
    dind_containers_removed: int
    containers_removed: int
    networks_removed: int
    database_records_updated: int
    database_records_deleted: int
    errors: List[str]
    orphaned_resources_cleaned: int


class CleanupMode(str):
    """Cleanup operation mode."""

    RESET_TO_DRAFT = "reset_to_draft"  # Stop DinD containers, reset DB to draft state
    PURGE_RANGES = "purge_ranges"  # Delete DinD containers AND DB records


class CleanupRequest(BaseModel):
    """Options for cleanup operation."""

    mode: str = CleanupMode.RESET_TO_DRAFT  # "reset_to_draft" or "purge_ranges"
    # Legacy fields for backwards compatibility
    clean_database: bool = True
    delete_database_records: bool = False
    force: bool = False


@router.post("/cleanup-all", response_model=CleanupResult)
def cleanup_all_resources(
    db: DBSession,
    admin_user: AdminUser,
    options: Optional[CleanupRequest] = None,
):
    """
    Cleanup PROVING GROUND range resources with two modes:

    **reset_to_draft**: Stop all DinD containers, reset ranges to draft state (keeps range definitions)
    **purge_ranges**: Delete all DinD containers AND range records from database (keeps templates, ISOs)

    **Requires admin privileges.**
    """
    import asyncio

    if options is None:
        options = CleanupRequest()

    # Handle legacy options
    if options.delete_database_records:
        options.mode = CleanupMode.PURGE_RANGES
    elif options.clean_database:
        options.mode = CleanupMode.RESET_TO_DRAFT

    docker = get_docker_service()
    dind = get_dind_service()
    result = CleanupResult(
        ranges_cleaned=0,
        dind_containers_removed=0,
        containers_removed=0,
        networks_removed=0,
        database_records_updated=0,
        database_records_deleted=0,
        errors=[],
        orphaned_resources_cleaned=0,
    )

    logger.info(f"Admin cleanup initiated by user {admin_user.email}, mode={options.mode}")

    # Step 1: Get all ranges from database
    ranges = db.query(Range).all()

    for range_obj in ranges:
        try:
            range_id = str(range_obj.id)

            # Delete DinD container if exists (this cleans up all VMs/networks inside)
            if range_obj.dind_container_id or range_obj.dind_container_name:
                try:
                    # Run async delete synchronously
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(
                            dind.delete_range_container(
                                range_id, volume_name=range_obj.dind_volume_name
                            )
                        )
                        result.dind_containers_removed += 1
                        try:
                            from proving_ground.tasks.pool import enqueue_pool_refill

                            enqueue_pool_refill()
                        except Exception as refill_err:
                            logger.warning(f"Could not enqueue pool refill: {refill_err}")
                    finally:
                        loop.close()
                except Exception as e:
                    logger.warning(f"Could not delete DinD container for range {range_id}: {e}")

            # Also try legacy cleanup (for any pre-DinD containers/networks)
            try:
                cleanup_result = docker.cleanup_range(range_id)
                result.containers_removed += cleanup_result.get("containers", 0)
                result.networks_removed += cleanup_result.get("networks", 0)
            except Exception as e:
                logger.debug(f"Legacy cleanup for range {range_id}: {e}")

            result.ranges_cleaned += 1

            if options.mode == CleanupMode.PURGE_RANGES:
                # Delete all range data from database
                # First delete range_instances that reference this range
                range_instances = (
                    db.query(RangeInstance).filter(RangeInstance.range_id == range_obj.id).all()
                )
                for instance in range_instances:
                    db.delete(instance)
                    result.database_records_deleted += 1

                # Delete VMs
                for vm in range_obj.vms:
                    db.delete(vm)
                    result.database_records_deleted += 1

                # Delete networks
                for network in range_obj.networks:
                    db.delete(network)
                    result.database_records_deleted += 1

                # Delete router if exists
                if range_obj.router:
                    db.delete(range_obj.router)
                    result.database_records_deleted += 1

                # Delete range
                db.delete(range_obj)
                result.database_records_deleted += 1

            else:  # RESET_TO_DRAFT
                # Reset range to draft state
                range_obj.status = RangeStatus.DRAFT
                range_obj.error_message = None
                range_obj.dind_container_id = None
                range_obj.dind_container_name = None
                range_obj.dind_mgmt_ip = None
                range_obj.dind_docker_url = None
                range_obj.deployed_at = None
                range_obj.started_at = None
                range_obj.stopped_at = None
                result.database_records_updated += 1

                # Reset all VMs
                for vm in range_obj.vms:
                    vm.status = VMStatus.PENDING
                    vm.container_id = None
                    vm.error_message = None
                    result.database_records_updated += 1

                # Reset networks
                for network in range_obj.networks:
                    network.docker_network_id = None
                    result.database_records_updated += 1

                # Reset router if exists
                if range_obj.router:
                    range_obj.router.container_id = None
                    range_obj.router.status = "pending"
                    result.database_records_updated += 1

        except Exception as e:
            error_msg = f"Failed to cleanup range {range_obj.name}: {e}"
            logger.error(error_msg)
            result.errors.append(error_msg)

    # Step 2: Clean up orphaned DinD containers (not tracked in database)
    logger.info("Cleaning up orphaned DinD containers...")
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            orphan_containers = loop.run_until_complete(dind.list_range_containers())
            for container in orphan_containers:
                try:
                    container_name = container.get("container_name", "")
                    # Force remove orphaned container
                    host_container = dind.host_client.containers.get(container_name)
                    host_container.stop(timeout=5)
                    host_container.remove(force=True)
                    result.orphaned_resources_cleaned += 1
                    logger.info(f"Removed orphaned DinD container: {container_name}")
                except Exception as e:
                    logger.warning(f"Could not remove orphan container: {e}")
        finally:
            loop.close()
    except Exception as e:
        error_msg = f"Failed orphan DinD cleanup: {e}"
        logger.warning(error_msg)

    # Step 3: Clean up any orphaned legacy Docker resources
    logger.info("Cleaning up orphaned legacy Docker resources...")
    try:
        # Wire the operator-facing force flag to the pool-drain opt-in: default
        # (force=False) must not destroy healthy ready pool members, only
        # claimed/orphaned range containers (see I3 in docker_service.py).
        orphan_cleanup = docker.cleanup_all_proving_ground_resources(
            include_ready_pool_members=options.force
        )
        result.orphaned_resources_cleaned += orphan_cleanup.get(
            "containers_removed", 0
        ) + orphan_cleanup.get("networks_removed", 0)
        result.errors.extend(orphan_cleanup.get("errors", []))
    except Exception as e:
        logger.debug(f"Legacy orphan cleanup: {e}")

    # Commit database changes
    try:
        db.commit()
    except Exception as e:
        error_msg = f"Failed to commit database changes: {e}"
        logger.error(error_msg)
        result.errors.append(error_msg)
        db.rollback()

    logger.info(
        f"Admin cleanup complete: {result.ranges_cleaned} ranges, "
        f"{result.dind_containers_removed} DinD containers, "
        f"{result.containers_removed} legacy containers, "
        f"{result.networks_removed} networks"
    )

    return result


@router.get("/docker-status")
def get_docker_status(admin_user: AdminUser):
    """
    Get current Docker resource status for PROVING GROUND.

    Returns counts of containers, networks, and volumes managed by PROVING GROUND.
    **Requires admin privileges.**
    """
    docker = get_docker_service()

    # Count PROVING GROUND resources
    containers = []
    networks = []

    try:
        all_containers = docker.client.containers.list(all=True)
        for c in all_containers:
            labels = c.labels or {}
            if labels.get("pg.range_id") or labels.get("pg.vm_id"):
                containers.append(
                    {
                        "name": c.name,
                        "status": c.status,
                        "range_id": labels.get("pg.range_id"),
                        "vm_id": labels.get("pg.vm_id"),
                    }
                )
    except Exception as e:
        logger.error(f"Failed to list containers: {e}")

    try:
        all_networks = docker.client.networks.list()
        for n in all_networks:
            if n.name.startswith("pg-") and n.name not in infrastructure_network_names():
                networks.append(
                    {
                        "name": n.name,
                        "id": n.id[:12],
                    }
                )
    except Exception as e:
        logger.error(f"Failed to list networks: {e}")

    return {
        "containers": containers,
        "container_count": len(containers),
        "networks": networks,
        "network_count": len(networks),
        "system_info": docker.get_system_info(),
    }


# =============================================================================
# Infrastructure Observability Endpoints
# =============================================================================

# Infrastructure service names and display names
INFRASTRUCTURE_SERVICES = {
    "api": "API Server",
    "worker": "Task Worker",
    "db": "PostgreSQL",
    "redis": "Redis",
    "minio": "MinIO",
    "traefik": "Traefik",
    "frontend": "Frontend",
}


def _format_uptime(seconds: int) -> str:
    """Convert seconds to human-readable uptime string."""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        return f"{minutes}m"
    elif seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days}d {hours}h"


def _find_container_by_service(docker, service_name: str):
    """Find a container by service name pattern."""
    try:
        containers = docker.client.containers.list(all=True)
        patterns = [
            f"pg-{service_name}-1",
            f"proving_ground_{service_name}_1",
            f"pg-{service_name}",
            f"proving_ground_{service_name}",
            service_name,
        ]
        for container in containers:
            for pattern in patterns:
                if container.name == pattern or container.name.endswith(f"-{service_name}-1"):
                    return container
        return None
    except Exception as e:
        logger.error(f"Error finding container for {service_name}: {e}")
        return None


def _get_service_health(docker, service_name: str, display_name: str) -> ServiceHealth:
    """Get health status for a service."""
    now = datetime.now(timezone.utc)
    container = _find_container_by_service(docker, service_name)

    if not container:
        return ServiceHealth(
            name=service_name,
            display_name=display_name,
            status="unknown",
            last_checked=now,
        )

    # Get container status
    container_status = container.status
    health_status = "unknown"

    if container_status == "running":
        # Check if container has health check
        health = container.attrs.get("State", {}).get("Health", {})
        if health:
            health_state = health.get("Status", "")
            if health_state == "healthy":
                health_status = "healthy"
            elif health_state == "unhealthy":
                health_status = "unhealthy"
            else:
                health_status = "degraded"
        else:
            # No health check, assume healthy if running
            health_status = "healthy"
    elif container_status in ["exited", "dead"]:
        health_status = "unhealthy"
    else:
        health_status = "degraded"

    # Calculate uptime
    started_at = container.attrs.get("State", {}).get("StartedAt", "")
    uptime_seconds = None
    uptime_human = None
    if started_at and container_status == "running":
        try:
            # Parse Docker timestamp
            start_time = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            uptime_seconds = int((now - start_time).total_seconds())
            uptime_human = _format_uptime(uptime_seconds)
        except Exception:
            pass

    # Get resource stats
    cpu_percent = None
    memory_mb = None
    memory_limit_mb = None
    memory_percent = None

    if container_status == "running":
        try:
            stats = docker.get_container_stats(container.id)
            if stats:
                cpu_percent = stats.get("cpu_percent", 0.0)
                memory_mb = stats.get("memory_mb", 0.0)
                memory_limit_mb = stats.get("memory_limit_mb", 0.0)
                if memory_limit_mb and memory_limit_mb > 0:
                    memory_percent = (memory_mb / memory_limit_mb) * 100
        except Exception as e:
            logger.debug(f"Could not get stats for {service_name}: {e}")

    # Get ports
    ports = []
    try:
        port_bindings = container.attrs.get("NetworkSettings", {}).get("Ports", {})
        for container_port, host_bindings in port_bindings.items():
            if host_bindings:
                for binding in host_bindings:
                    host_port = binding.get("HostPort", "")
                    if host_port:
                        ports.append(f"{host_port}->{container_port}")
    except Exception:
        pass

    # Get health check output
    health_output = None
    health = container.attrs.get("State", {}).get("Health", {})
    if health:
        log = health.get("Log", [])
        if log:
            last_check = log[-1]
            health_output = last_check.get("Output", "")[:200]  # Truncate

    return ServiceHealth(
        name=service_name,
        display_name=display_name,
        status=health_status,
        container_id=container.id[:12],
        container_status=container_status,
        uptime_seconds=uptime_seconds,
        uptime_human=uptime_human,
        cpu_percent=round(cpu_percent, 2) if cpu_percent is not None else None,
        memory_mb=round(memory_mb, 2) if memory_mb is not None else None,
        memory_limit_mb=round(memory_limit_mb, 2) if memory_limit_mb is not None else None,
        memory_percent=round(memory_percent, 2) if memory_percent is not None else None,
        ports=ports,
        health_check_output=health_output,
        last_checked=now,
    )


@router.get("/infrastructure/services", response_model=InfrastructureServicesResponse)
def get_infrastructure_services(admin_user: AdminUser):
    """
    Get health status of all PROVING GROUND infrastructure services.

    Returns status, uptime, and resource usage for API, Worker, DB, Redis, MinIO, Traefik, and Frontend.
    **Requires admin privileges.**
    """
    docker = get_docker_service()
    services = []

    for service_name, display_name in INFRASTRUCTURE_SERVICES.items():
        service_health = _get_service_health(docker, service_name, display_name)
        services.append(service_health)

    # Determine overall status
    statuses = [s.status for s in services]
    if all(s == "healthy" for s in statuses):
        overall_status = "healthy"
    elif any(s == "unhealthy" for s in statuses):
        overall_status = "unhealthy"
    else:
        overall_status = "degraded"

    return InfrastructureServicesResponse(
        services=services,
        overall_status=overall_status,
        checked_at=datetime.now(timezone.utc),
    )


@router.get("/infrastructure/logs", response_model=ServiceLogsResponse)
def get_infrastructure_logs(
    admin_user: AdminUser,
    service: str = Query(
        ..., description="Service name (api, worker, db, redis, minio, traefik, frontend)"
    ),
    level: Optional[str] = Query(
        None, description="Log level filter (error, warning, info, debug)"
    ),
    search: Optional[str] = Query(None, description="Search text in logs"),
    since: Optional[str] = Query(None, description="Start time (ISO format)"),
    until: Optional[str] = Query(None, description="End time (ISO format)"),
    limit: int = Query(100, ge=1, le=1000, description="Number of log lines"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
):
    """
    Get logs for a PROVING GROUND infrastructure service.

    Supports filtering by log level, text search, and time range.
    **Requires admin privileges.**
    """
    if service not in INFRASTRUCTURE_SERVICES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid service. Must be one of: {', '.join(INFRASTRUCTURE_SERVICES.keys())}",
        )

    docker = get_docker_service()
    container = _find_container_by_service(docker, service)

    if not container:
        return ServiceLogsResponse(
            service=service,
            logs=[],
            total_lines=0,
            has_more=False,
            filters_applied={"error": "Container not found"},
        )

    # Get logs from container
    try:
        # Calculate tail count (we need extra for filtering)
        tail_count = (offset + limit) * 3 if (level or search) else (offset + limit + 100)
        tail_count = min(tail_count, 5000)  # Cap at 5000 lines

        since_dt = None
        until_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                pass
        if until:
            try:
                until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
            except ValueError:
                pass

        raw_logs = container.logs(
            tail=tail_count,
            timestamps=True,
            since=since_dt,
            until=until_dt,
        ).decode("utf-8", errors="replace")

        lines = raw_logs.strip().split("\n") if raw_logs.strip() else []

        # Parse and filter logs
        log_entries = []
        level_pattern = re.compile(r"\b(ERROR|WARN(?:ING)?|INFO|DEBUG)\b", re.IGNORECASE)

        for line in lines:
            if not line.strip():
                continue

            # Parse timestamp (Docker format: 2024-01-18T14:32:01.123456789Z)
            timestamp = None
            message = line
            try:
                if len(line) > 30 and line[4] == "-" and line[10] == "T":
                    ts_str = line[:30].split()[0]
                    timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    message = line[31:].strip() if len(line) > 31 else line
            except Exception:
                pass

            # Detect log level
            detected_level = None
            level_match = level_pattern.search(message[:100])
            if level_match:
                detected_level = level_match.group(1).upper()
                if detected_level == "WARNING":
                    detected_level = "WARN"

            # Apply level filter
            if level:
                level_upper = level.upper()
                if level_upper == "ERROR" and detected_level != "ERROR":
                    continue
                elif level_upper == "WARNING" and detected_level not in ["ERROR", "WARN"]:
                    continue
                elif level_upper == "INFO" and detected_level not in ["ERROR", "WARN", "INFO"]:
                    continue
                # DEBUG includes all

            # Apply search filter
            if search and search.lower() not in message.lower():
                continue

            log_entries.append(
                LogEntry(
                    timestamp=timestamp,
                    level=detected_level,
                    message=message,
                    raw=line,
                )
            )

        total_lines = len(log_entries)
        # Apply pagination
        paginated_entries = log_entries[offset : offset + limit]
        has_more = (offset + limit) < total_lines

        return ServiceLogsResponse(
            service=service,
            logs=paginated_entries,
            total_lines=total_lines,
            has_more=has_more,
            filters_applied={
                "level": level,
                "search": search,
                "since": since,
                "until": until,
                "limit": limit,
                "offset": offset,
            },
        )

    except Exception as e:
        logger.error(f"Error getting logs for {service}: {e}")
        return ServiceLogsResponse(
            service=service,
            logs=[],
            total_lines=0,
            has_more=False,
            filters_applied={"error": str(e)},
        )


@router.get("/infrastructure/docker", response_model=DockerOverviewResponse)
def get_docker_overview(admin_user: AdminUser):
    """
    Get comprehensive Docker resource overview.

    Returns containers, networks, volumes, and images with PROVING GROUND-specific annotations.
    **Requires admin privileges.**
    """
    docker = get_docker_service()

    # Get containers
    containers = []
    proving_ground_vms = 0
    proving_ground_infra = 0
    running_count = 0
    stopped_count = 0

    try:
        all_containers = docker.client.containers.list(all=True)
        for c in all_containers:
            labels = c.labels or {}
            is_vm = bool(labels.get("pg.vm_id"))
            is_infra = c.name.startswith("pg-") or c.name.startswith("proving_ground_")

            if is_vm:
                proving_ground_vms += 1
            if is_infra and not is_vm:
                proving_ground_infra += 1

            if c.status == "running":
                running_count += 1
            else:
                stopped_count += 1

            # Get ports
            ports = []
            try:
                port_bindings = c.attrs.get("NetworkSettings", {}).get("Ports", {})
                for container_port, host_bindings in (port_bindings or {}).items():
                    if host_bindings:
                        for binding in host_bindings:
                            host_port = binding.get("HostPort", "")
                            if host_port:
                                ports.append(f"{host_port}->{container_port}")
            except Exception:
                pass

            # Parse created time
            created = None
            try:
                created_str = c.attrs.get("Created", "")
                if created_str:
                    created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            except Exception:
                pass

            containers.append(
                DockerContainerOverview(
                    id=c.id[:12],
                    name=c.name,
                    image=c.image.tags[0] if c.image.tags else c.image.id[:12],
                    status=c.status,
                    state=c.attrs.get("State", {}).get("Status", "unknown"),
                    created=created,
                    ports=ports,
                    labels=labels,
                    is_proving_ground_infra=is_infra and not is_vm,
                    is_proving_ground_vm=is_vm,
                )
            )
    except Exception as e:
        logger.error(f"Error listing containers: {e}")

    # Get networks
    networks = []
    proving_ground_networks = 0

    try:
        all_networks = docker.client.networks.list()
        for n in all_networks:
            is_proving_ground = n.name.startswith("pg-")

            if is_proving_ground:
                proving_ground_networks += 1

            # Get IPAM config
            subnet = None
            gateway = None
            try:
                ipam = n.attrs.get("IPAM", {}).get("Config", [])
                if ipam:
                    subnet = ipam[0].get("Subnet")
                    gateway = ipam[0].get("Gateway")
            except Exception:
                pass

            # Count connected containers
            container_count = len(n.attrs.get("Containers", {}) or {})

            networks.append(
                DockerNetworkOverview(
                    id=n.id[:12],
                    name=n.name,
                    driver=n.attrs.get("Driver", "unknown"),
                    scope=n.attrs.get("Scope", "local"),
                    internal=n.attrs.get("Internal", False),
                    subnet=subnet,
                    gateway=gateway,
                    container_count=container_count,
                    is_proving_ground_range=is_proving_ground
                    and n.name not in infrastructure_network_names(),
                )
            )
    except Exception as e:
        logger.error(f"Error listing networks: {e}")

    # Get volumes
    volumes = []
    try:
        all_volumes = docker.client.volumes.list()
        for v in all_volumes:
            # Parse created time
            created = None
            try:
                created_str = v.attrs.get("CreatedAt", "")
                if created_str:
                    created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            except Exception:
                pass

            volumes.append(
                DockerVolumeOverview(
                    name=v.name,
                    driver=v.attrs.get("Driver", "local"),
                    mountpoint=v.attrs.get("Mountpoint", ""),
                    created=created,
                    labels=v.attrs.get("Labels", {}) or {},
                )
            )
    except Exception as e:
        logger.error(f"Error listing volumes: {e}")

    # Get images
    images = []
    try:
        all_images = docker.client.images.list()
        for img in all_images:
            # Check if PROVING GROUND-related
            tags = img.tags or []
            is_proving_ground = any(
                "proving_ground" in t.lower()
                or "qemu" in t.lower()
                or "dockur" in t.lower()
                or "kasmweb" in t.lower()
                or "linuxserver" in t.lower()
                or "vyos" in t.lower()
                for t in tags
            )

            size_bytes = img.attrs.get("Size", 0)
            size_human = _format_size(size_bytes)

            # Parse created time
            created = None
            try:
                created_str = img.attrs.get("Created", "")
                if created_str:
                    created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            except Exception:
                pass

            images.append(
                DockerImageOverview(
                    id=img.id.split(":")[1][:12] if ":" in img.id else img.id[:12],
                    tags=tags,
                    size_bytes=size_bytes,
                    size_human=size_human,
                    created=created,
                    is_proving_ground_related=is_proving_ground,
                )
            )
    except Exception as e:
        logger.error(f"Error listing images: {e}")

    return DockerOverviewResponse(
        containers=containers,
        networks=networks,
        volumes=volumes,
        images=images,
        summary=DockerSummary(
            total_containers=len(containers),
            running_containers=running_count,
            stopped_containers=stopped_count,
            proving_ground_vms=proving_ground_vms,
            proving_ground_infra=proving_ground_infra,
            total_networks=len(networks),
            proving_ground_networks=proving_ground_networks,
            total_volumes=len(volumes),
            total_images=len(images),
        ),
    )


def _format_size(size_bytes: int) -> str:
    """Format bytes to human-readable size."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def _get_directory_size(path: str) -> tuple[float, int]:
    """Get directory size in MB and file count."""
    total_size = 0
    file_count = 0
    try:
        if os.path.exists(path):
            for dirpath, _dirnames, filenames in os.walk(path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    try:
                        total_size += os.path.getsize(fp)
                        file_count += 1
                    except (OSError, IOError):
                        pass
    except Exception:
        pass
    return total_size / (1024 * 1024), file_count


@router.get("/infrastructure/metrics", response_model=InfrastructureMetricsResponse)
def get_infrastructure_metrics(admin_user: AdminUser, db: DBSession):
    """
    Get resource metrics for host, database, task queue, and storage.

    **Requires admin privileges.**
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)

    # Host metrics
    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    load_avg = None
    try:
        load_avg = list(os.getloadavg())
    except (OSError, AttributeError):
        pass  # Windows doesn't have getloadavg

    host_metrics = HostMetrics(
        cpu_count=psutil.cpu_count() or 1,
        cpu_percent=cpu_percent,
        memory_total_mb=memory.total / (1024 * 1024),
        memory_used_mb=memory.used / (1024 * 1024),
        memory_available_mb=memory.available / (1024 * 1024),
        memory_percent=memory.percent,
        disk_total_gb=disk.total / (1024 * 1024 * 1024),
        disk_used_gb=disk.used / (1024 * 1024 * 1024),
        disk_free_gb=disk.free / (1024 * 1024 * 1024),
        disk_percent=disk.percent,
        load_average=load_avg,
    )

    # Database metrics
    db_metrics = DatabaseMetrics()
    try:
        # Connection count
        result = db.execute(
            text(
                "SELECT count(*) as total, "
                "count(*) FILTER (WHERE state = 'active') as active, "
                "count(*) FILTER (WHERE state = 'idle') as idle "
                "FROM pg_stat_activity WHERE datname = current_database()"
            )
        )
        row = result.fetchone()
        if row:
            db_metrics.connection_count = row[0] or 0
            db_metrics.active_connections = row[1] or 0
            db_metrics.idle_connections = row[2] or 0

        # Database size
        result = db.execute(text("SELECT pg_database_size(current_database())"))
        row = result.fetchone()
        if row and row[0]:
            size_bytes = row[0]
            db_metrics.database_size_mb = size_bytes / (1024 * 1024)
            db_metrics.database_size_human = _format_size(size_bytes)

        # Table count
        result = db.execute(
            text("SELECT count(*) FROM information_schema.tables " "WHERE table_schema = 'public'")
        )
        row = result.fetchone()
        if row:
            db_metrics.table_count = row[0] or 0

        # Largest tables
        result = db.execute(
            text(
                "SELECT relname as table_name, "
                "pg_total_relation_size(c.oid) as size "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                "ORDER BY pg_total_relation_size(c.oid) DESC "
                "LIMIT 5"
            )
        )
        largest = []
        for row in result:
            largest.append(
                {
                    "name": row[0],
                    "size_bytes": row[1],
                    "size_human": _format_size(row[1]),
                }
            )
        db_metrics.largest_tables = largest

    except Exception as e:
        logger.error(f"Error getting database metrics: {e}")

    # Task queue metrics (Redis/Dramatiq)
    queue_metrics = TaskQueueMetrics()
    try:
        import redis

        redis_client = redis.from_url(settings.redis_url)

        # Get Dramatiq queue lengths
        # Default queue is 'default'
        queue_length = redis_client.llen("dramatiq:default")
        delayed_length = redis_client.zcard("dramatiq:default.DQ")

        queue_metrics.queue_length = queue_length or 0
        queue_metrics.delayed_messages = delayed_length or 0

        # Count total messages (approximation)
        total_keys = 0
        for _key in redis_client.scan_iter("dramatiq:*"):
            total_keys += 1
        queue_metrics.messages_total = total_keys

    except Exception as e:
        logger.debug(f"Error getting task queue metrics: {e}")

    # Storage metrics
    storage_metrics = StorageMetrics()

    # ISO cache
    iso_size, iso_count = _get_directory_size(settings.iso_cache_dir)
    storage_metrics.iso_cache_size_mb = round(iso_size, 2)
    storage_metrics.iso_cache_files = iso_count

    # Template storage
    template_size, template_count = _get_directory_size(settings.template_storage_dir)
    storage_metrics.template_storage_size_mb = round(template_size, 2)
    storage_metrics.template_storage_files = template_count

    # VM storage
    vm_size, _ = _get_directory_size(settings.vm_storage_dir)
    storage_metrics.vm_storage_size_mb = round(vm_size, 2)
    # Count directories (each VM gets a directory)
    try:
        if os.path.exists(settings.vm_storage_dir):
            storage_metrics.vm_storage_dirs = len(
                [
                    d
                    for d in os.listdir(settings.vm_storage_dir)
                    if os.path.isdir(os.path.join(settings.vm_storage_dir, d))
                ]
            )
    except Exception:
        pass

    # MinIO metrics
    try:
        from minio import Minio

        minio_client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        buckets = list(minio_client.list_buckets())
        storage_metrics.minio_bucket_count = len(buckets)

        total_objects = 0
        total_size = 0
        for bucket in buckets:
            try:
                for obj in minio_client.list_objects(bucket.name, recursive=True):
                    total_objects += 1
                    total_size += obj.size or 0
            except Exception:
                pass
        storage_metrics.minio_total_objects = total_objects
        storage_metrics.minio_total_size_mb = round(total_size / (1024 * 1024), 2)
    except Exception as e:
        logger.debug(f"Error getting MinIO metrics: {e}")

    return InfrastructureMetricsResponse(
        host=host_metrics,
        database=db_metrics,
        task_queue=queue_metrics,
        storage=storage_metrics,
        collected_at=now,
    )


@router.get("/infrastructure/system", response_model=SystemInfoResponse)
def get_system_info(admin_user: AdminUser, db: DBSession):
    """
    Get system information including version, migrations, and configuration.

    **Requires admin privileges.**
    """
    settings = get_settings()
    docker = get_docker_service()

    # Get Docker version
    docker_version = None
    try:
        docker_info = docker.client.version()
        docker_version = docker_info.get("Version", "unknown")
    except Exception:
        pass

    # Get architecture
    arch = platform.machine()
    is_arm = arch in ["arm64", "aarch64"]

    # Get current migration revision
    db_revision = None
    migrations = []
    try:
        result = db.execute(text("SELECT version_num FROM alembic_version"))
        row = result.fetchone()
        if row:
            db_revision = row[0]
    except Exception as e:
        logger.debug(f"Could not get migration revision: {e}")

    # Get migration history from alembic
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        # Find alembic.ini
        alembic_ini = os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")
        if os.path.exists(alembic_ini):
            config = Config(alembic_ini)
            script = ScriptDirectory.from_config(config)

            for rev in script.walk_revisions():
                migrations.append(
                    MigrationInfo(
                        revision=rev.revision[:12],
                        description=rev.doc or "No description",
                        applied=db_revision is not None
                        and script.get_revision(db_revision) is not None,
                    )
                )
            # Limit to last 10
            migrations = migrations[:10]
    except Exception as e:
        logger.debug(f"Could not load migration history: {e}")

    # Non-sensitive config items
    config_items = [
        ConfigItem(key="app_name", value=settings.app_name, source="config"),
        ConfigItem(key="debug", value=str(settings.debug), source="config"),
        ConfigItem(key="iso_cache_dir", value=settings.iso_cache_dir, source="config"),
        ConfigItem(
            key="template_storage_dir", value=settings.template_storage_dir, source="config"
        ),
        ConfigItem(key="vm_storage_dir", value=settings.vm_storage_dir, source="config"),
        ConfigItem(key="vyos_image", value=settings.vyos_image, source="config"),
        ConfigItem(
            key="management_network", value=settings.management_network_name, source="config"
        ),
    ]

    return SystemInfoResponse(
        version=settings.app_version,
        commit=settings.git_commit,
        build_date=settings.build_date,
        app_name=settings.app_name,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        docker_version=docker_version,
        architecture=arch,
        is_arm=is_arm,
        database_revision=db_revision,
        migrations=migrations,
        config=config_items,
    )


# Range Debug Response Models
class VMDebugInfo(BaseModel):
    id: str
    hostname: str
    status: str
    container_id: Optional[str] = None
    ip_address: Optional[str] = None
    base_image: Optional[str] = None
    error_message: Optional[str] = None


class RangeDebugInfo(BaseModel):
    id: str
    name: str
    status: str
    dind_container_id: Optional[str] = None
    dind_container_name: Optional[str] = None
    dind_docker_url: Optional[str] = None
    dind_mgmt_ip: Optional[str] = None
    vnc_proxy_mappings: Optional[Dict[str, Any]] = None
    vms: List[VMDebugInfo] = []
    network_count: int = 0
    router_container_id: Optional[str] = None
    router_status: Optional[str] = None


class RangeDebugResponse(BaseModel):
    ranges: List[RangeDebugInfo]
    total_count: int
    dind_containers_in_docker: List[str] = []


@router.get("/infrastructure/ranges", response_model=RangeDebugResponse)
def get_range_debug_info(admin_user: AdminUser, db: DBSession):
    """
    Get debug information for all ranges including DinD, VNC, and VM status.

    Shows database state for ranges, VMs, networks, and VNC proxy mappings.
    Useful for debugging console access and deployment issues.

    **Requires admin privileges.**
    """
    from proving_ground.models.range import Range
    from proving_ground.models.router import RangeRouter
    from proving_ground.models.base_image import BaseImage

    # Get all ranges
    ranges = db.query(Range).all()

    range_infos = []
    for r in ranges:
        # Get VMs for this range
        vms = db.query(VM).filter(VM.range_id == r.id).all()
        vm_infos = []
        for vm in vms:
            base_image_tag = None
            if vm.base_image_id:
                bi = db.query(BaseImage).filter(BaseImage.id == vm.base_image_id).first()
                if bi:
                    base_image_tag = bi.docker_image_tag

            vm_infos.append(
                VMDebugInfo(
                    id=str(vm.id),
                    hostname=vm.hostname,
                    status=str(vm.status.value) if vm.status else "unknown",
                    container_id=vm.container_id,
                    ip_address=vm.ip_address,
                    base_image=base_image_tag,
                    error_message=vm.error_message,
                )
            )

        # Get network count
        network_count = db.query(Network).filter(Network.range_id == r.id).count()

        # Get router info
        router = db.query(RangeRouter).filter(RangeRouter.range_id == r.id).first()

        range_infos.append(
            RangeDebugInfo(
                id=str(r.id),
                name=r.name,
                status=str(r.status.value) if r.status else "unknown",
                dind_container_id=r.dind_container_id,
                dind_container_name=r.dind_container_name,
                dind_docker_url=r.dind_docker_url,
                dind_mgmt_ip=r.dind_mgmt_ip,
                vnc_proxy_mappings=r.vnc_proxy_mappings,
                vms=vm_infos,
                network_count=network_count,
                router_container_id=router.container_id if router else None,
                router_status=str(router.status.value) if router and router.status else None,
            )
        )

    # Get actual DinD containers from Docker
    docker = get_docker_service()
    dind_containers = []
    try:
        containers = docker.client.containers.list(
            all=True, filters={"label": "com.proving_ground.type=dind"}
        )
        dind_containers = [c.name for c in containers]
    except Exception as e:
        logger.warning(f"Could not list DinD containers: {e}")

    return RangeDebugResponse(
        ranges=range_infos,
        total_count=len(range_infos),
        dind_containers_in_docker=dind_containers,
    )


# ---------------------------------------------------------------------------
# Platform update
#
# The awkward part: this restarts the container serving the request. The API
# cannot run the update itself -- it would be killed partway through and the
# response would never arrive, leaving the caller unable to tell a successful
# update from a crash.
#
# So the work runs in a SEPARATE container that is not part of the compose
# project. `docker compose up` therefore does not touch it, and it survives the
# restart it causes. The API only launches it and reports on it afterwards.
# ---------------------------------------------------------------------------

UPDATE_LABEL = "pg.platform_update"
UPDATE_IMAGE = "docker:27-cli"


class UpdateStartResponse(BaseModel):
    started: bool
    job_id: Optional[str] = None
    message: str
    from_sha: Optional[str] = None


class UpdateStatusResponse(BaseModel):
    state: str  # idle | running | succeeded | failed
    job_id: Optional[str] = None
    exit_code: Optional[int] = None
    log_tail: Optional[str] = None
    current_sha: Optional[str] = None
    started_at: Optional[str] = None


def _host_repo_root(docker) -> Optional[str]:
    """The repository's path on the host, as the host sees it.

    The API container has the backend bind-mounted at /app, so the repo root is
    that mount's parent. Paths inside this container are useless to a sibling
    container -- it needs the host's view.
    """
    try:
        own_id = os.environ.get("HOSTNAME")
        container = docker.client.containers.get(own_id)
        for mount in container.attrs.get("Mounts", []):
            if mount.get("Destination") == "/app" and mount.get("Type") == "bind":
                return os.path.dirname(mount["Source"].rstrip("/"))
    except Exception as e:  # noqa: BLE001 - reported to the caller below
        logger.warning(f"Could not determine the host repository path: {e}")
    return None


class UpdateCheckResponse(BaseModel):
    # checked=False means the remote could not be consulted. The UI must not
    # render that as "up to date": not knowing is not the same as having
    # nothing to pull.
    checked: bool
    update_available: bool = False
    behind: Optional[int] = None
    branch: Optional[str] = None
    current_sha: Optional[str] = None
    current_version: Optional[str] = None
    latest_tag: Optional[str] = None
    detail: Optional[str] = None


# Runs before any git command in a repo container: the ownership exception, the
# working directory, and a credential helper reading the token from the
# environment (the repository's own helper shells out to glab, which is not
# installed there and has no token there either).
_GIT_SETUP = (
    'git config --global --add safe.directory "$PG_REPO_ROOT"\n'
    'cd "$PG_REPO_ROOT"\n'
    'if [ -n "${GIT_ASKPASS_TOKEN:-}" ]; then\n'
    "  git config --global credential.helper "
    "'!f() { echo username=$GIT_ASKPASS_USER; echo password=$GIT_ASKPASS_TOKEN; }; f'\n"
    "fi\n"
)


def _repo_container_spec(docker, db) -> tuple[dict, str]:
    """Everything needed to run a container against the repo on the host.

    Shared by the update and the update check so the two cannot drift apart.

    The mount path is not a detail. Bound anywhere other than the host's own
    path, `docker compose` inside the container resolves the compose files'
    relative bind mounts (./config/registry-config.yml, ./backend, ./data/...)
    against that container's filesystem and hands the results to the HOST's
    daemon, which creates the missing ones as directories. That took the whole
    platform down once: config/registry-config.yml became a directory and the
    registry died on "not a directory".

    Raises HTTPException if the repo path or the stored credential is unusable.
    """
    repo_root = _host_repo_root(docker)
    if not repo_root:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Could not determine the repository path on the host. Update "
                "from a shell instead: scripts/pg-update.sh"
            ),
        )

    # Run as the user that owns the repository, not root. Two reasons, and the
    # first was found the hard way: git refuses to operate on a repository owned
    # by someone else ("detected dubious ownership"), which failed the update
    # before it read even the branch name. And a root-owned file written into
    # the working tree cannot afterwards be removed by the owner.
    #
    # The numeric ids are what matter -- a bind mount preserves them, so what
    # this container sees on /app is what the host sees on the repository.
    try:
        repo_stat = os.stat("/app")
        sock_gid = os.stat("/var/run/docker.sock").st_gid
        run_as = f"{repo_stat.st_uid}:{repo_stat.st_gid}"
        extra_groups = [str(sock_gid)]
    except OSError as e:
        logger.warning(f"Could not resolve ownership for the repo container: {e}")
        run_as, extra_groups = None, None

    # The remote needs a credential the host holds in a helper this container
    # cannot reach. Read the stored one and hand it over through the
    # environment -- never the command line, which shows up in `ps` and in the
    # container's own Config.Cmd.
    git_token, cred_row = platform_secret_service.get_secret(db, GIT_CREDENTIAL_KEY)
    if cred_row is not None and git_token is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The stored update credential cannot be decrypted, which usually "
                "means the JWT secret changed since it was saved. Set the token "
                "again under Update credential."
            ),
        )

    env = {"HOME": "/tmp", "PG_REPO_ROOT": repo_root}
    if git_token:
        env["GIT_ASKPASS_USER"] = cred_row.public_part or "oauth2"
        env["GIT_ASKPASS_TOKEN"] = git_token

    spec = {
        "user": run_as,
        "group_add": extra_groups,
        # HOME must be writable for `git config --global`; the repo owner has no
        # home directory inside this image.
        "environment": env,
        "volumes": {
            "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
            repo_root: {"bind": repo_root, "mode": "rw"},
        },
        "working_dir": repo_root,
    }
    return spec, repo_root


def _find_update_job(docker):
    """The most recent update container, running or finished."""
    try:
        jobs = docker.client.containers.list(all=True, filters={"label": UPDATE_LABEL})
        return (
            sorted(jobs, key=lambda c: c.attrs.get("Created", ""), reverse=True)[0]
            if jobs
            else None
        )
    except Exception:
        return None


class GitCredentialRequest(BaseModel):
    username: str
    token: str


class GitCredentialStatus(BaseModel):
    configured: bool
    username: Optional[str] = None
    updated_at: Optional[str] = None
    readable: bool = True


@router.put("/infrastructure/update/credential", response_model=GitCredentialStatus)
def set_update_credential(body: GitCredentialRequest, admin_user: AdminUser, db: DBSession):
    """Store the credential the update uses to fetch from the code remote.

    **Requires admin privileges.** The token is encrypted at rest and is never
    returned by the API -- the read endpoint reports only whether one is set.

    It lives here rather than in the environment for the reason the whole
    feature exists: an operator with a browser and no shell cannot edit a .env.
    Compose also loads .env into every container, so a token there would be
    readable from any service, not only the one that needs it.
    """
    if not body.token.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The token is empty.",
        )
    row = platform_secret_service.set_secret(
        db,
        GIT_CREDENTIAL_KEY,
        body.token.strip(),
        public_part=body.username.strip() or None,
        user_id=admin_user.id,
    )
    return GitCredentialStatus(
        configured=True,
        username=row.public_part,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


@router.get("/infrastructure/update/credential", response_model=GitCredentialStatus)
def get_update_credential(admin_user: AdminUser, db: DBSession):
    """Whether an update credential is set. Never returns the token itself."""
    value, row = platform_secret_service.get_secret(db, GIT_CREDENTIAL_KEY)
    if row is None:
        return GitCredentialStatus(configured=False)
    return GitCredentialStatus(
        configured=True,
        username=row.public_part,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        # False when the stored value cannot be decrypted, which usually means
        # the JWT secret changed. The remedy is to set it again, so say so
        # rather than reporting it as configured and letting the update fail.
        readable=value is not None,
    )


@router.delete("/infrastructure/update/credential", status_code=status.HTTP_204_NO_CONTENT)
def delete_update_credential(admin_user: AdminUser, db: DBSession):
    """Remove the stored update credential. **Requires admin privileges.**"""
    platform_secret_service.delete_secret(db, GIT_CREDENTIAL_KEY)


@router.post("/infrastructure/update", response_model=UpdateStartResponse)
def start_platform_update(admin_user: AdminUser, db: DBSession):
    """Pull the latest code on the current branch and redeploy.

    **Requires admin privileges.** Restarts the API and workers, so requests in
    flight will fail and the UI reconnects once the new containers are healthy.

    Deliberately takes no branch or ref argument. Updating means running
    whatever the pull brings, so accepting a ref from the request would turn
    this into arbitrary code execution on the host chosen by the caller. It
    fast-forwards the branch the host is already on; changing branches remains
    a deliberate act at a shell.
    """
    docker = get_docker_service()

    # A deploy in flight would be killed mid-way by the worker restart, leaving
    # a half-built range whose database row claims it is deploying.
    deploying = db.query(Range).filter(Range.status == RangeStatus.DEPLOYING).count()
    if deploying:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{deploying} range(s) are deploying. Updating now restarts the "
                f"deploy worker and would leave them half-built. Wait for them "
                f"to finish, then update."
            ),
        )

    existing = _find_update_job(docker)
    if existing is not None and existing.status == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An update is already running.",
        )

    spec, repo_root = _repo_container_spec(docker, db)

    from_sha = None
    try:
        from_sha = (
            docker.client.containers.get(os.environ.get("HOSTNAME"))
            .attrs.get("Config", {})
            .get("Labels", {})
            .get("org.opencontainers.image.revision")
        )
    except Exception:
        pass

    # Remove a finished job so its logs do not shadow this run's.
    if existing is not None:
        try:
            existing.remove(force=True)
        except Exception:
            pass

    script = (
        "set -eu\n" + _GIT_SETUP + 'branch="$(git rev-parse --abbrev-ref HEAD)"\n'
        'echo "==> updating $branch"\n'
        "git fetch --prune origin\n"
        'git pull --ff-only origin "$branch"\n'
        'echo "    now at $(git rev-parse --short HEAD)"\n'
        'echo "==> redeploying"\n'
        # scripts/compose.sh is the single definition of the overlay chain.
        # Spelling it out here again is how the two paths drifted before.
        "./scripts/compose.sh up -d --build\n"
        'echo "==> update complete"\n'
    )

    try:
        job = docker.client.containers.run(
            UPDATE_IMAGE,
            command=["sh", "-c", script],
            detach=True,
            labels={UPDATE_LABEL: "1"},
            **spec,
            # Not part of the compose project, so `compose up` below does not
            # stop the very container running it.
            name=f"pg-platform-update-{int(datetime.now(timezone.utc).timestamp())}",
        )
    except Exception as e:  # noqa: BLE001 - surfaced to the admin
        logger.error(f"Failed to launch the platform update: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not launch the update: {e}",
        ) from e

    logger.warning(
        f"Platform update started by {admin_user.username} (job {job.id[:12]}); "
        f"the API will restart"
    )
    return UpdateStartResponse(
        started=True,
        job_id=job.id[:12],
        from_sha=from_sha,
        message=(
            "Update started. The API and workers restart as part of it, so this "
            "page will lose contact for a minute or two and then reconnect."
        ),
    )


CHECK_LABEL = "pg.platform_update_check"
CHECK_TIMEOUT_SECONDS = 60


@router.get("/infrastructure/update/check", response_model=UpdateCheckResponse)
def check_for_platform_update(admin_user: AdminUser, db: DBSession):
    """Whether the code remote has anything this host does not.

    **Requires admin privileges.** Consults the remote, so it needs the stored
    credential and takes a second or two. Not something to poll.

    Runs in a short-lived container for the same reason the update does: the
    API container has only the backend bind-mounted at /app, so it cannot see
    the repository's .git at all.

    `checked` is the load-bearing field. A failure to reach the remote reports
    checked=false with the reason and NEVER update_available=false -- "I could
    not look" and "there is nothing to pull" must not render the same, or the
    update goes quiet exactly when something is wrong.
    """
    docker = get_docker_service()
    current_version = get_settings().app_version

    try:
        spec, _ = _repo_container_spec(docker, db)
    except HTTPException as e:
        return UpdateCheckResponse(
            checked=False, current_version=current_version, detail=str(e.detail)
        )

    script = (
        "set -eu\n" + _GIT_SETUP + 'branch="$(git rev-parse --abbrev-ref HEAD)"\n'
        # Branch refs only, and stderr kept.
        #
        # `--tags` force-updates every tag and fails the WHOLE fetch with
        # "would clobber existing tag" whenever a local tag disagrees with the
        # remote's -- the standing state of this repository, where a run of
        # tags could never be pushed (CLAUDE.md, commit-author email
        # restriction). The fetch exited non-zero, `set -eu` aborted the
        # script, and the discarded stderr left the endpoint reporting only
        # "The check produced no output."
        #
        # Whether this host is behind is a question about commits. A tag
        # disagreement must not be able to answer it "I do not know".
        "git fetch --prune origin\n"
        # The latest tag is a nicety on top of the commit count, so fetching
        # tags is best effort and explicitly never fatal.
        "git fetch --tags origin >/dev/null 2>&1 || true\n"
        'echo "BRANCH=$branch"\n'
        'echo "SHA=$(git rev-parse --short HEAD)"\n'
        'echo "BEHIND=$(git rev-list --count HEAD..origin/$branch)"\n'
        'echo "TAG=$(git describe --tags --abbrev=0 origin/$branch 2>/dev/null || echo none)"\n'
    )

    container = None
    try:
        container = docker.client.containers.run(
            UPDATE_IMAGE,
            command=["sh", "-c", script],
            detach=True,
            labels={CHECK_LABEL: "1"},
            **spec,
        )
        result = container.wait(timeout=CHECK_TIMEOUT_SECONDS)
        output = container.logs().decode("utf-8", errors="replace")
        exit_code = result.get("StatusCode", 1)
    except Exception as e:  # noqa: BLE001 - reported to the admin
        logger.warning(f"Update check failed: {e}")
        return UpdateCheckResponse(
            checked=False,
            current_version=current_version,
            detail=f"Could not reach the code remote: {e}",
        )
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass

    fields = {}
    for line in output.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep:
            fields[key] = value

    if exit_code != 0 or "BEHIND" not in fields:
        # Logged, not merely returned. The first failure of this endpoint left
        # no trace anywhere on the server, so working out why meant reproducing
        # it by hand against a live host.
        logger.warning(f"Update check exited {exit_code}; output tail: {output.strip()[-600:]}")
        return UpdateCheckResponse(
            checked=False,
            current_version=current_version,
            detail=(output.strip()[-400:] or "The check produced no output."),
        )

    try:
        behind = int(fields["BEHIND"])
    except ValueError:
        logger.warning(f"Update check gave a non-numeric commit count: {fields.get('BEHIND')!r}")
        return UpdateCheckResponse(
            checked=False,
            current_version=current_version,
            detail=f"Unexpected output from the check: {output.strip()[-200:]}",
        )

    tag = fields.get("TAG")
    return UpdateCheckResponse(
        checked=True,
        update_available=behind > 0,
        behind=behind,
        branch=fields.get("BRANCH"),
        current_sha=fields.get("SHA"),
        current_version=current_version,
        latest_tag=None if tag in (None, "none", "") else tag,
    )


@router.get("/infrastructure/update/status", response_model=UpdateStatusResponse)
def get_platform_update_status(admin_user: AdminUser):
    """Progress of the most recent platform update.

    **Requires admin privileges.** Safe to poll across the restart: the update
    container is outside the compose project, so its logs survive it.
    """
    docker = get_docker_service()
    job = _find_update_job(docker)
    if job is None:
        return UpdateStatusResponse(state="idle")

    try:
        job.reload()
    except Exception:
        pass

    state_raw = job.attrs.get("State", {})
    running = state_raw.get("Running", False)
    exit_code = state_raw.get("ExitCode")
    if running:
        state = "running"
    elif exit_code == 0:
        state = "succeeded"
    else:
        state = "failed"

    try:
        log_tail = job.logs(tail=40).decode("utf-8", errors="replace")
    except Exception:
        log_tail = None

    return UpdateStatusResponse(
        state=state,
        job_id=job.id[:12],
        exit_code=None if running else exit_code,
        log_tail=log_tail,
        started_at=state_raw.get("StartedAt"),
    )
