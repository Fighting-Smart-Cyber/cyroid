#!/usr/bin/env python3
"""Prove range networks end to end on a real cluster — COSMOS PG-58, unblocked by PG-303.

Three NetworkAttachmentDefinitions, one pod attached to all three, each interface carrying the
address the blueprint declared. It asserts the addresses rather than merely that the pod runs:
before PG-303 was fixed the pod came up perfectly healthy with no additional interfaces at all,
which is the failure mode worth guarding — a range would deploy green with half its networks
missing.

Requires Multus on the cluster:  scripts/setup-multus-k3s.sh

    ssh pg-devtest
    export KUBECONFIG=$HOME/.kube/config
    python scripts/smoke-range-networks.py

If this 403s immediately after a k3s restart, the API server is still settling — wait and re-run.
"""
import asyncio
import json
import sys
from proving_ground.capability.networking import attachment_annotation, network_attachment_definition, NetworkSpec
from proving_ground.capability.workload import InterfaceSpec, OSFamily, WorkloadSpec, DiskSpec
from proving_ground.capability.kubernetes_client import KubernetesApiClient
from kubernetes_asyncio import client as k8s

NS = "pg-net-smoke"
NETS = [
    NetworkSpec(name="dmz",      subnet="172.30.10.0/24", gateway="172.30.10.1"),
    NetworkSpec(name="internal", subnet="172.30.20.0/24", gateway="172.30.20.1"),
    NetworkSpec(name="mgmt",     subnet="172.30.30.0/24"),
]
WL = WorkloadSpec(
    name="multihomed", os_family=OSFamily.LINUX, os_version="ubuntu-24.04",
    cpus=1, memory_mb=512, disks=(DiskSpec(name="root", size_gb=10, boot=True),),
    interfaces=(
        InterfaceSpec(network="dmz",      ip_address="172.30.10.5", primary=True),
        InterfaceSpec(network="internal", ip_address="172.30.20.5"),
        InterfaceSpec(network="mgmt",     ip_address="172.30.30.5"),
    ),
)

async def main():
    kube = await KubernetesApiClient.from_kubeconfig()
    core = k8s.CoreV1Api(kube._api)
    ok = True
    try:
        await kube.ensure_namespace(NS, {})
        for n in NETS:
            await kube.apply_custom_object(group="k8s.cni.cncf.io", version="v1",
                plural="network-attachment-definitions", namespace=NS,
                name=n.attachment_name, body=network_attachment_definition(n, NS))
        print(f"NADS        {[n.attachment_name for n in NETS]}")

        ann = attachment_annotation(WL, NETS, NS)
        print(f"ANNOTATION  {len(json.loads(ann['k8s.v1.cni.cncf.io/networks']))} attachments")

        await core.create_namespaced_pod(namespace=NS, body=k8s.V1Pod(
            metadata=k8s.V1ObjectMeta(name="multihomed", annotations=ann),
            spec=k8s.V1PodSpec(restart_policy="Never", containers=[k8s.V1Container(
                name="c", image="docker.io/library/busybox:1.36",
                command=["/bin/sh","-c","ip -4 -o addr show; sleep 300"])])))

        for _ in range(60):
            p = await core.read_namespaced_pod(name="multihomed", namespace=NS)
            if p.status.phase in ("Running","Failed","Succeeded"): break
            await asyncio.sleep(2)
        print(f"POD         phase={p.status.phase}")
        if p.status.phase != "Running":
            ev = await core.list_namespaced_event(namespace=NS)
            for e in ev.items[-4:]: print(f"  EVENT {e.reason}: {(e.message or '')[:150]}")
            ok = False
        else:
            await asyncio.sleep(3)
            logs = await core.read_namespaced_pod_log(name="multihomed", namespace=NS)
            print("INTERFACES:"); 
            for line in logs.strip().splitlines(): print(f"  {line}")
            for want in ("172.30.10.5","172.30.20.5","172.30.30.5"):
                hit = want in logs
                print(f"  {'OK ' if hit else 'MISS'} {want}")
                ok &= hit
    except Exception as e:
        print(f"ERROR       {type(e).__name__}: {str(e)[:300]}")
        ok = False
    finally:
        await kube.delete_namespace(NS); print("CLEANUP     namespace deleted"); await kube.close()
    print("\n" + ("NET SMOKE PASSED" if ok else "NET SMOKE FAILED"))
    return 0 if ok else 1

sys.exit(asyncio.run(main()))
