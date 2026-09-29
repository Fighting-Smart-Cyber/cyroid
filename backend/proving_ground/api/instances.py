# backend/proving_ground/api/instances.py
from uuid import UUID
from fastapi import APIRouter, HTTPException, status
from sqlalchemy.orm import Session

from proving_ground.api import kubernetes_ranges
from proving_ground.api.deps import DBSession, CurrentUser, check_resource_control
from proving_ground.capability.blueprint import read_blueprint
from proving_ground.models import Range, RangeInstance, RangeStatus
from proving_ground.schemas.blueprint import InstanceResponse, BlueprintConfig
from proving_ground.services.blueprint_service import (
    create_range_from_blueprint,
    next_instance_ordinal,
)
from proving_ground.tasks.deployment import deploy_range_task, teardown_range_task

router = APIRouter(prefix="/instances", tags=["instances"])


@router.get("/{instance_id}", response_model=InstanceResponse)
def get_instance(instance_id: UUID, db: DBSession, current_user: CurrentUser):
    """Get instance details."""
    instance = db.query(RangeInstance).filter(RangeInstance.id == instance_id).first()
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    return _instance_to_response(instance, db)


@router.post("/{instance_id}/reset", response_model=InstanceResponse)
def reset_instance(instance_id: UUID, db: DBSession, current_user: CurrentUser):
    """Reset instance to initial state (same blueprint version)."""
    instance = db.query(RangeInstance).filter(RangeInstance.id == instance_id).first()
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    blueprint = instance.blueprint
    range_obj = _range_under_control(instance, db, current_user)

    if kubernetes_ranges.is_kubernetes():
        # Era B: the range's contents are its namespace, not Network and VM rows, so a reset is
        # a destroy and a fresh deploy of the same blueprint. The destroy is synchronous because
        # a redeploy landing on top of what is still coming down is how a namespace ends up half
        # of each.
        kubernetes_ranges.teardown_on_kubernetes(db, range_obj)
        deploy_range_task.send(str(range_obj.id))
        db.refresh(instance)
        return _instance_to_response(instance, db)

    # Teardown current range resources (async task)
    teardown_range_task.send(str(range_obj.id))

    # Redeploy from same config
    config = BlueprintConfig.model_validate(blueprint.config)

    # Delete VMs and networks from range
    from proving_ground.models import VM, Network

    db.query(VM).filter(VM.range_id == range_obj.id).delete()
    db.query(Network).filter(Network.range_id == range_obj.id).delete()
    db.flush()

    # Recreate from config
    _recreate_range_contents(db, range_obj, config)

    # Redeploy (queue async task)
    db.commit()
    deploy_range_task.send(str(range_obj.id))
    db.refresh(instance)

    return _instance_to_response(instance, db)


@router.post("/{instance_id}/redeploy", response_model=InstanceResponse)
def redeploy_instance(instance_id: UUID, db: DBSession, current_user: CurrentUser):
    """Redeploy instance from latest blueprint version."""
    instance = db.query(RangeInstance).filter(RangeInstance.id == instance_id).first()
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    blueprint = instance.blueprint
    range_obj = _range_under_control(instance, db, current_user)

    if kubernetes_ranges.is_kubernetes():
        # Era B: as in reset, but the instance moves to the blueprint's current version, which
        # is what the next deploy will read anyway.
        kubernetes_ranges.teardown_on_kubernetes(db, range_obj)
        instance.blueprint_version = blueprint.version
        db.commit()
        deploy_range_task.send(str(range_obj.id))
        db.refresh(instance)
        return _instance_to_response(instance, db)

    # Teardown first (sync for now to ensure cleanup before recreate)
    teardown_range_task.send(str(range_obj.id))

    # Get LATEST config from blueprint
    config = BlueprintConfig.model_validate(blueprint.config)

    # Delete VMs and networks from range
    from proving_ground.models import VM, Network

    db.query(VM).filter(VM.range_id == range_obj.id).delete()
    db.query(Network).filter(Network.range_id == range_obj.id).delete()
    db.flush()

    # Recreate from latest config
    _recreate_range_contents(db, range_obj, config)

    # Update instance to latest version
    instance.blueprint_version = blueprint.version

    # Redeploy (queue async task)
    db.commit()
    deploy_range_task.send(str(range_obj.id))
    db.refresh(instance)

    return _instance_to_response(instance, db)


@router.post(
    "/{instance_id}/clone", response_model=InstanceResponse, status_code=status.HTTP_201_CREATED
)
def clone_instance(instance_id: UUID, db: DBSession, current_user: CurrentUser):
    """Clone an instance (create new instance with next offset)."""
    instance = db.query(RangeInstance).filter(RangeInstance.id == instance_id).first()
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    blueprint = instance.blueprint
    _range_under_control(instance, db, current_user)

    if read_blueprint(blueprint.config).deployable_on_kubernetes:
        # Era B: networks and workloads are realised on the cluster from the config at deploy
        # time, so the range row is the clone's handle and nothing more.
        new_range = Range(
            name=f"{instance.name} (Clone)",
            description=f"Instance of blueprint '{blueprint.name}' (Kubernetes substrate)",
            created_by=current_user.id,
            status=RangeStatus.DRAFT,
        )
        db.add(new_range)
        db.flush()
    else:
        config = BlueprintConfig.model_validate(blueprint.config)

        # Create new range
        new_range = create_range_from_blueprint(
            db=db,
            config=config,
            range_name=f"{instance.name} (Clone)",
            created_by=current_user.id,
        )

    # Create new instance record
    new_instance = RangeInstance(
        name=f"{instance.name} (Clone)",
        blueprint_id=blueprint.id,
        blueprint_version=blueprint.version,
        subnet_offset=next_instance_ordinal(db, blueprint.id),
        instructor_id=current_user.id,
        range_id=new_range.id,
    )
    db.add(new_instance)
    db.commit()
    db.refresh(new_instance)

    return _instance_to_response(new_instance, db)


@router.delete("/{instance_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_instance(instance_id: UUID, db: DBSession, current_user: CurrentUser):
    """Delete an instance and its range."""
    instance = db.query(RangeInstance).filter(RangeInstance.id == instance_id).first()
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    range_obj = instance.range
    if range_obj is None:
        check_resource_control("instance", instance.id, current_user, db, instance.instructor_id)
        db.delete(instance)
        db.commit()
        return

    check_resource_control("range", range_obj.id, current_user, db, range_obj.created_by)

    if kubernetes_ranges.is_kubernetes():
        # Destroy before the rows go, and only keep going if it came down clean. Placement is
        # re-derived from the range row, so a worker handed the id after the row is gone has
        # nothing left to name the namespace with and it is orphaned for good.
        kubernetes_ranges.delete_on_kubernetes(db, range_obj)
    else:
        # Teardown range resources (async task)
        teardown_range_task.send(str(range_obj.id))

    # Delete range (cascades to VMs, networks)
    db.delete(range_obj)

    # Delete instance
    db.delete(instance)
    db.commit()


# ============ Helper Functions ============


def _range_under_control(instance: RangeInstance, db: Session, current_user) -> Range:
    """The instance's range, once the caller has been shown to control it.

    Every route here destroys the range and rebuilds it, so control is the rule and not
    visibility: an assignment is permission to use a lab, not to reset someone else's.
    """
    range_obj = instance.range
    if range_obj is None:
        raise HTTPException(status_code=404, detail="Instance has no range")
    check_resource_control("range", range_obj.id, current_user, db, range_obj.created_by)
    return range_obj


def _recreate_range_contents(db: Session, range_obj: Range, config: BlueprintConfig):
    """Recreate networks and VMs in an existing range, with the blueprint's own addresses.

    A redeploy used to renumber the range by applying the instance's subnet offset, while
    `create_range_from_blueprint` ignored that offset entirely -- so an instance's networks
    silently moved the first time it was redeployed. DinD isolation is what made the offset
    unnecessary; PG-122 removed it, and this path now agrees with creation.
    """
    from proving_ground.models import Network, VM

    # Create networks
    network_lookup = {}
    for net_config in config.networks:
        network = Network(
            range_id=range_obj.id,
            name=net_config.name,
            subnet=net_config.subnet,
            gateway=net_config.gateway,
            is_isolated=net_config.is_isolated,
        )
        db.add(network)
        db.flush()
        network_lookup[net_config.name] = network.id

    # Create VMs
    for vm_config in config.vms:
        network_id = network_lookup.get(vm_config.network_name)
        if not network_id:
            continue

        # Resolve image source from vm_config
        base_image_id = None
        golden_image_id = None
        snapshot_id = None

        if hasattr(vm_config, "base_image_id") and vm_config.base_image_id:
            base_image_id = vm_config.base_image_id
        elif hasattr(vm_config, "golden_image_id") and vm_config.golden_image_id:
            golden_image_id = vm_config.golden_image_id
        elif hasattr(vm_config, "snapshot_id") and vm_config.snapshot_id:
            snapshot_id = vm_config.snapshot_id
        else:
            # No image source found, skip this VM
            continue

        vm = VM(
            range_id=range_obj.id,
            network_id=network_id,
            base_image_id=base_image_id,
            golden_image_id=golden_image_id,
            snapshot_id=snapshot_id,
            hostname=vm_config.hostname,
            ip_address=vm_config.ip_address,
            cpu=vm_config.cpu,
            ram_mb=vm_config.ram_mb,
            disk_gb=vm_config.disk_gb,
            position_x=vm_config.position_x,
            position_y=vm_config.position_y,
        )
        db.add(vm)

    db.flush()


def _instance_to_response(instance: RangeInstance, db: Session) -> InstanceResponse:
    from proving_ground.models import User

    range_obj = instance.range
    instructor = db.query(User).filter(User.id == instance.instructor_id).first()

    return InstanceResponse(
        id=instance.id,
        name=instance.name,
        blueprint_id=instance.blueprint_id,
        blueprint_version=instance.blueprint_version,
        subnet_offset=instance.subnet_offset,
        instructor_id=instance.instructor_id,
        range_id=instance.range_id,
        created_at=instance.created_at,
        range_name=range_obj.name if range_obj else None,
        range_status=range_obj.status.value if range_obj else None,
        instructor_username=instructor.username if instructor else None,
    )
