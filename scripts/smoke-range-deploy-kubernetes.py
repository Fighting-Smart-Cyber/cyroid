#!/usr/bin/env python3
"""One range end to end on the Kubernetes substrate -- the 2026-09-15 handoff, steps 3 and 4.

A real `Range` row flows through the Era B branches of `deploy_range_task` and
`teardown_range_task`, against a real Postgres and a real cluster. The unit suite proves the
capability layer and the seam's ordering against a fake; this proves the cluster agrees:

    namespace created with its default-deny floor
    two NetworkAttachmentDefinitions applied
    a KubeVirt VirtualMachine Running with net1 and net2 on the addresses the blueprint declared
    the capability chart installed and seeded, verify() returning pass plus evidence
    stop: the VMI gone and the VM's disk kept; start: the VMI back and Running
    teardown through the task, residue() clean, the range back at DRAFT

Then the same for a TEAM EXERCISE -- a range on a training event with nobody assigned -- which
places into a vcluster: the vcluster Ready and its kubeconfig exported, the capability's
HelmRelease deploying INTO the vcluster (its pod synced back as `…-x-<ns>-x-vcluster`), the
verify hook running inside the vcluster, and a teardown that leaves no cluster-scoped RBAC
behind -- the residue a vcluster is known to strand when the order is wrong.

The task bodies are synchronous, because dramatiq calls them that way, so the harness is too;
the k8s assertions afterwards get their own loop.

Prerequisites: a Postgres at DATABASE_URL migrated to head, a cluster at KUBECONFIG with KubeVirt,
CDI, Multus and Flux's source- and helm-controller, and egress to quay.io and the chart repository.
Redis is not required -- event broadcast failures are logged and skipped.

    export DATABASE_URL=postgresql://... KUBECONFIG=$HOME/.kube/config
    alembic upgrade head
    python scripts/smoke-range-deploy-kubernetes.py
"""
import asyncio
import json
import os
import sys
import uuid

from kubernetes_asyncio import client as k8s
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from proving_ground.capability.blueprint import read_blueprint
from proving_ground.capability.kubernetes_client import KubernetesApiClient
from proving_ground.capability.kubernetes_runtime import KubernetesRuntime
from proving_ground.capability.lifecycle import RangeLifecycle
from datetime import datetime, timezone

from proving_ground.capability.flux import FluxChartInstaller
from proving_ground.capability.models import Isolation
from proving_ground.models.blueprint import RangeBlueprint, RangeInstance
from proving_ground.models.event import TrainingEvent
from proving_ground.models.event_log import EventLog
from proving_ground.models.range import Range, RangeStatus
from proving_ground.models.user import User, UserRole
from proving_ground.services.kubernetes_range_service import (
    placement_for_range,
    start_range_on_kubernetes,
    stop_range_on_kubernetes,
)
from proving_ground.tasks.deployment import _deploy_range_on_kubernetes, _teardown_range_on_kubernetes

BUSYBOX = "docker.io/library/busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
# quay.io/kubevirt/cirros-container-disk-demo:v1.9.0, by digest (ADR-0007).
CIRROS = "quay.io/kubevirt/cirros-container-disk-demo@sha256:ebdb8d8b9b480f6ee7664ed3fdde8428767664f507d98f94090edeff04d7ebf2"

CONFIG = {
    "schemaVersion": 2,
    "networks": [
        {"name": "dmz", "subnet": "172.30.10.0/24", "gateway": "172.30.10.1"},
        {"name": "internal", "subnet": "172.30.20.0/24"},
    ],
    "workloads": [
        {
            "name": "web",
            "os": {"family": "linux", "version": "cirros-0.6"},
            "cpus": 1,
            "memoryMb": 256,
            "bootImage": CIRROS,
            "disks": [{"name": "root", "sizeGb": 1, "boot": True}],
            "interfaces": [
                {"network": "dmz", "ip": "172.30.10.5", "primary": True},
                {"network": "internal", "ip": "172.30.20.5"},
            ],
        }
    ],
    "capabilities": [
        {
            "name": "podinfo",
            "version": "1.0.0",
            "scope": "per-learner",
            "chart": {
                "name": "podinfo",
                "version": "6.7.1",
                "repository": "https://stefanprodan.github.io/podinfo",
            },
            "values": {"replicaCount": 1},
            "hooks": {
                "seed": {
                    "image": BUSYBOX,
                    "command": ["/bin/sh", "-c", "echo seeding; echo '{\"seeded\": true}'"],
                },
                "verify": {
                    "image": BUSYBOX,
                    "command": ["/bin/sh", "-c", "echo '{\"passed\": true, \"convoy_id\": \"C-17\"}'"],
                },
            },
        }
    ],
}

WANT = {"net1": "172.30.10.5", "net2": "172.30.20.5"}

TEAM_CONFIG = json.loads(json.dumps(CONFIG))
TEAM_CONFIG["capabilities"][0]["scope"] = "shared"


def line(tag, msg):
    print(f"{tag:<12}{msg}")


async def assert_cluster(range_obj):
    """What the cluster says, read independently of what the task reported."""
    ok = True
    spec = read_blueprint(CONFIG)
    placement = placement_for_range(range_obj, list(spec.capabilities))
    ns = placement.namespace
    kube = await KubernetesApiClient.connect()
    try:
        line("NAMESPACE", f"{ns} exists={await kube.namespace_exists(ns)}")
        ok &= await kube.namespace_exists(ns)

        for n in spec.networks:
            nad = await kube.get_custom_object(
                group="k8s.cni.cncf.io", version="v1", plural="network-attachment-definitions",
                namespace=ns, name=n.attachment_name,
            )
            line("NAD", f"{n.attachment_name} present={nad is not None}")
            ok &= nad is not None

        vmi = await kube.get_custom_object(
            group="kubevirt.io", version="v1", plural="virtualmachineinstances", namespace=ns, name="web"
        )
        phase = ((vmi or {}).get("status") or {}).get("phase")
        line("VMI", f"web phase={phase}")
        ok &= phase == "Running"
        seen = {
            i.get("name"): i.get("ipAddress")
            for i in ((vmi or {}).get("status") or {}).get("interfaces") or []
        }
        for name, want in WANT.items():
            hit = seen.get(name) == want
            line("  " + ("OK" if hit else "MISS"), f"{name} want={want} got={seen.get(name)}")
            ok &= hit

        hr = await kube.get_custom_object(
            group="helm.toolkit.fluxcd.io", version="v2", plural="helmreleases", namespace=ns, name="podinfo"
        )
        ready = [c for c in (hr or {}).get("status", {}).get("conditions", []) if c["type"] == "Ready"]
        line("HELMRELEASE", f"podinfo Ready={ready[0]['status'] if ready else None}")
        ok &= bool(ready) and ready[0]["status"] == "True"

        verified = await KubernetesRuntime(kube).verify(spec.capabilities[0], placement)
        line("VERIFY", f"passed={verified.passed} evidence={dict(verified.evidence)}")
        ok &= verified.passed and verified.evidence.get("convoy_id") == "C-17"
    finally:
        await kube.close()
    return ok, placement


async def vmi_phase(placement, name="web"):
    kube = await KubernetesApiClient.connect()
    try:
        vmi = await kube.get_custom_object(
            group="kubevirt.io", version="v1", plural="virtualmachineinstances",
            namespace=placement.namespace, name=name,
        )
        pvc = await kube.get_custom_object(
            group="cdi.kubevirt.io", version="v1beta1", plural="datavolumes",
            namespace=placement.namespace, name=f"{name}-root",
        )
        return ((vmi or {}).get("status") or {}).get("phase"), pvc is not None
    finally:
        await kube.close()


async def stop_and_start(db, rng, placement):
    """A stopped range keeps its disks and burns no memory; a started one comes back."""
    ok = True
    stopped = await stop_range_on_kubernetes(db, rng.id)
    line("STOP", f"stopped={stopped['stopped']}")
    for _ in range(60):
        phase, disk = await vmi_phase(placement)
        if phase is None:
            break
        await asyncio.sleep(2)
    line("  VMI", f"phase={phase} (None = gone)  disk kept={disk}")
    ok &= phase is None and disk

    started = await start_range_on_kubernetes(db, rng.id)
    phase, _ = await vmi_phase(placement)
    line("START", f"started={started['started']} workloads={started['workloads']}")
    line("  VMI", f"phase={phase}")
    ok &= phase == "Running" and started["workloads"]["web"]["status"] == "Running"
    return ok


async def assert_residue(placement):
    kube = await KubernetesApiClient.connect()
    try:
        residue = await RangeLifecycle(kube).residue(placement)
        line("RESIDUE", residue.describe())
        return residue.is_clean
    finally:
        await kube.close()


async def assert_vcluster(range_obj):
    """The team-exercise placement, read independently of what the task reported."""
    ok = True
    spec = read_blueprint(TEAM_CONFIG)
    placement = placement_for_range(range_obj, list(spec.capabilities))
    ns = placement.namespace
    line("PLACEMENT", f"{placement.isolation.value} {ns} vcluster={placement.vcluster}")
    ok &= placement.isolation is Isolation.VCLUSTER
    kube = await KubernetesApiClient.connect()
    vc = None
    try:
        life = RangeLifecycle(kube)
        kubeconfig = await life.vcluster_kubeconfig(placement)
        line("VCLUSTER", f"kubeconfig exported={kubeconfig is not None} server={(kubeconfig or {}).get('clusters', [{}])[0].get('cluster', {}).get('server')}")
        ok &= kubeconfig is not None

        hr = await kube.get_custom_object(group="helm.toolkit.fluxcd.io", version="v2",
                                          plural="helmreleases", namespace=ns, name="podinfo")
        ready = [c for c in (hr or {}).get("status", {}).get("conditions", []) if c["type"] == "Ready"]
        kc = ((hr or {}).get("spec") or {}).get("kubeConfig", {}).get("secretRef", {}).get("name")
        line("HELMRELEASE", f"podinfo Ready={ready[0]['status'] if ready else None} kubeConfig.secretRef={kc}")
        ok &= bool(ready) and ready[0]["status"] == "True" and kc == "vc-vcluster"

        core = k8s.CoreV1Api(kube._api)
        pods = await core.list_namespaced_pod(namespace=ns)
        # vcluster syncs the pod back into the host namespace under a rewritten name -- and
        # shortens long ones with a hash, so the chart's name need not survive in it.
        synced = [p.metadata.name for p in pods.items if p.metadata.name.endswith("-x-vcluster")]
        line("SYNCED", f"pods synced into the host namespace by the vcluster: {synced}")
        ok &= bool(synced)

        vmi = await kube.get_custom_object(group="kubevirt.io", version="v1",
                                           plural="virtualmachineinstances", namespace=ns, name="web")
        line("VMI", f"web phase={((vmi or {}).get('status') or {}).get('phase')} (host namespace, beside the vcluster)")
        ok &= ((vmi or {}).get("status") or {}).get("phase") == "Running"

        vc = await KubernetesApiClient.from_kubeconfig_dict(kubeconfig)
        inside = await k8s.CoreV1Api(vc._api).list_namespaced_pod(namespace=ns)
        line("INSIDE", f"pods in the vcluster's {ns}: {[p.metadata.name for p in inside.items]}")
        runtime = KubernetesRuntime(vc, chart_installer=FluxChartInstaller(kube))
        verified = await runtime.verify(spec.capabilities[0], placement)
        line("VERIFY", f"(inside the vcluster) passed={verified.passed} evidence={dict(verified.evidence)}")
        ok &= verified.passed and verified.evidence.get("convoy_id") == "C-17"
    finally:
        if vc is not None:
            await vc.close()
        await kube.close()
    return ok, placement


def fixture(db, config, *, tag, learner=None, event=None):
    instructor = User(username=f"i-{tag}", email=f"i-{tag}@x.invalid", hashed_password="x",
                      role=UserRole.ADMIN, is_active=True, is_approved=True)
    db.add(instructor); db.flush()
    event_id = None
    if event:
        ev = TrainingEvent(name=f"ex-{tag}", start_datetime=datetime.now(timezone.utc),
                           created_by_id=instructor.id)
        db.add(ev); db.flush(); event_id = ev.id
    blueprint = RangeBlueprint(name=f"k8s-smoke-{tag}", version=1, config=config, created_by=instructor.id)
    rng = Range(name=f"k8s-smoke-{tag}", status=RangeStatus.DRAFT, created_by=instructor.id,
                assigned_to_user_id=learner.id if learner else None, training_event_id=event_id)
    db.add_all([blueprint, rng]); db.flush()
    db.add(RangeInstance(name=f"k8s-smoke-{tag}", blueprint_id=blueprint.id, blueprint_version=1,
                         subnet_offset=0, instructor_id=instructor.id, range_id=rng.id))
    db.commit()
    return rng


def team_exercise(db):
    """A range on an event with nobody assigned -> vcluster."""
    ok = True
    placement = None
    tag = uuid.uuid4().hex[:8]
    rng = fixture(db, TEAM_CONFIG, tag=f"team-{tag}", event=True)
    rid = rng.id
    line("FIXTURE", f"team-exercise range={rid} event={rng.training_event_id} (nobody assigned)")
    _deploy_range_on_kubernetes(str(rid))
    db.expire_all()
    rng = db.query(Range).filter(Range.id == rid).first()
    line("RANGE", f"status={rng.status.value} error={rng.error_message}")
    ok &= rng.status == RangeStatus.RUNNING
    if ok:
        vc_ok, placement = asyncio.run(assert_vcluster(rng))
        ok &= vc_ok
    _teardown_range_on_kubernetes(str(rid))
    db.expire_all()
    rng = db.query(Range).filter(Range.id == rid).first()
    line("TEARDOWN", f"status={rng.status.value} error={rng.error_message}")
    ok &= rng.status == RangeStatus.DRAFT
    if placement is not None:
        ok &= asyncio.run(assert_residue(placement))
    return ok


def main():
    db = sessionmaker(bind=create_engine(os.environ["DATABASE_URL"]))()
    ok = True
    placement = None
    try:
        tag = uuid.uuid4().hex[:8]
        learner = User(username=f"l-{tag}", email=f"l-{tag}@x.invalid", hashed_password="x",
                       is_active=True, is_approved=True)
        db.add(learner); db.flush()
        rng = fixture(db, CONFIG, tag=tag, learner=learner)
        rid = rng.id
        line("FIXTURE", f"range={rid} learner={learner.id}")

        _deploy_range_on_kubernetes(str(rid))

        db.expire_all()
        rng = db.query(Range).filter(Range.id == rid).first()
        line("RANGE", f"status={rng.status.value} error={rng.error_message}")
        ok &= rng.status == RangeStatus.RUNNING
        for e in db.query(EventLog).filter(EventLog.range_id == rid).order_by(EventLog.created_at):
            line("EVENT", f"{e.event_type.value}: {e.message[:110]}")
        done = [e for e in db.query(EventLog).filter(EventLog.range_id == rid)
                if e.event_type.value == "deployment_completed"]
        if done:
            extra = json.loads(done[0].extra_data or "{}")
            line("REPORTED", f"networks={extra.get('networks')} workloads={extra.get('workloads')}")

        if ok:
            cluster_ok, placement = asyncio.run(assert_cluster(rng))
            ok &= cluster_ok
        if ok:
            ok &= asyncio.run(stop_and_start(db, rng, placement))

        _teardown_range_on_kubernetes(str(rid))
        db.expire_all()
        rng = db.query(Range).filter(Range.id == rid).first()
        line("TEARDOWN", f"status={rng.status.value} error={rng.error_message}")
        ok &= rng.status == RangeStatus.DRAFT
        if placement is not None:
            ok &= asyncio.run(assert_residue(placement))

        print("\n--- team exercise -> vcluster")
        ok &= team_exercise(db)
    except Exception as e:
        line("ERROR", f"{type(e).__name__}: {e}")
        ok = False
    finally:
        db.close()
    print("\n" + ("K8S RANGE SMOKE PASSED" if ok else "K8S RANGE SMOKE FAILED"))
    return 0 if ok else 1


sys.exit(main())
