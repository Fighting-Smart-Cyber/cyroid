#!/usr/bin/env python3
import asyncio, sys
from kubernetes_asyncio import client as k8s
from proving_ground.capability import Delivery, PlacementRequest, Scope, resolve_placement
from proving_ground.capability.kubernetes_client import KubernetesApiClient
from proving_ground.capability.lifecycle import RangeLifecycle

BUSYBOX = ("docker.io/library/busybox@sha256:"
           "73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662")

def placement_for(learner, cohort="lifecycle", team=False):
    return resolve_placement(PlacementRequest(
        scopes=frozenset({Scope.SHARED if team else Scope.PER_LEARNER}),
        delivery=Delivery.TEAM_EXERCISE if team else Delivery.SELF_PACED,
        cohort_key=cohort, learner_key=None if team else learner))

async def pod(core, ns, name, cmd):
    await core.create_namespaced_pod(namespace=ns, body=k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(name=name, labels={"app": name}),
        spec=k8s.V1PodSpec(restart_policy="Never", containers=[k8s.V1Container(
            name="c", image=BUSYBOX, command=["/bin/sh","-c",cmd])])))

async def wait_running(core, ns, name, tries=60):
    for _ in range(tries):
        p = await core.read_namespaced_pod(name=name, namespace=ns)
        if p.status.phase in ("Running","Succeeded","Failed"): return p
        await asyncio.sleep(2)
    return p

async def main():
    kube = await KubernetesApiClient.from_kubeconfig()
    core = k8s.CoreV1Api(kube._api)
    apps = k8s.AppsV1Api(kube._api)
    life = RangeLifecycle(kube)
    ok = True
    a = placement_for("learner-a"); b = placement_for("learner-b")
    vc = placement_for(None, cohort="teamex", team=True)
    print(f"RANGE A     {a.isolation.value} {a.namespace}")
    print(f"RANGE B     {b.isolation.value} {b.namespace}")
    print(f"RANGE VC    {vc.isolation.value} {vc.namespace} vcluster={vc.vcluster}")
    try:
        # --- criterion 2: create under both resolutions, concurrently
        await asyncio.gather(life.create(a), life.create(b))
        print("CREATE      both namespace ranges up")

        await apps.create_namespaced_deployment(namespace=a.namespace, body=k8s.V1Deployment(
            metadata=k8s.V1ObjectMeta(name="svc"),
            spec=k8s.V1DeploymentSpec(replicas=1, selector=k8s.V1LabelSelector(match_labels={"app":"svc"}),
              template=k8s.V1PodTemplateSpec(metadata=k8s.V1ObjectMeta(labels={"app":"svc"}),
                spec=k8s.V1PodSpec(containers=[k8s.V1Container(name="c", image=BUSYBOX,
                  command=["/bin/sh","-c","sleep 3600"])])))))
        await asyncio.sleep(6)

        # --- criterion 2: stop / start preserve rather than delete
        stopped = await life.stop(a)
        await asyncio.sleep(4)
        d = await apps.read_namespaced_deployment(name="svc", namespace=a.namespace)
        print(f"STOP        scaled {stopped} workload(s); replicas now {d.spec.replicas}")
        ok &= stopped >= 1 and d.spec.replicas == 0
        started = await life.start(a)
        await asyncio.sleep(4)
        d = await apps.read_namespaced_deployment(name="svc", namespace=a.namespace)
        print(f"START       scaled {started}; replicas now {d.spec.replicas}")
        ok &= d.spec.replicas == 1
        ok &= await kube.namespace_exists(a.namespace)  # stop must not destroy

        # --- criterion 5: are concurrent ranges actually isolated?
        # The target must actually LISTEN, or the probe fails for want of a listener and the
        # test proves nothing about isolation while looking reassuringly green.
        await pod(core, b.namespace, "target", "while true; do echo hi | nc -l -p 8080; done & sleep 600")
        tb = await wait_running(core, b.namespace, "target")
        target_ip = tb.status.pod_ip
        await asyncio.sleep(5)
        # Sanity-check the listener from inside its own namespace first: if THIS fails, the
        # cross-range result below is meaningless.
        await pod(core, b.namespace, "selfprobe", f"nc -w 3 -z {target_ip} 8080 2>&1; echo EXIT=$?; sleep 5")
        await wait_running(core, b.namespace, "selfprobe")
        await asyncio.sleep(3)
        selflog = await core.read_namespaced_pod_log(name="selfprobe", namespace=b.namespace)
        listener_up = "EXIT=0" in selflog
        print(f"CONTROL     same-namespace probe to {target_ip}:8080: "
              f"{'listener reachable' if listener_up else 'LISTENER DOWN - isolation result below is meaningless'}")
        ok &= listener_up
        await pod(core, a.namespace, "prober", f"nc -w 3 -z {target_ip} 8080 2>&1; echo EXIT=$?; sleep 5")
        pa = await wait_running(core, a.namespace, "prober")
        await asyncio.sleep(3)
        logs = await core.read_namespaced_pod_log(name="prober", namespace=a.namespace)
        reachable = "EXIT=0" in logs
        print(f"ISOLATION   range A -> range B pod {target_ip}:8080: "
              f"{'REACHABLE - ranges are NOT network-isolated' if reachable else 'blocked'}")
        print(f"            raw: {logs.strip()[:80]}")
        ok &= not reachable

        # --- criterion 2 + 4: vcluster resolution, then teardown residue
        await life.create(vc)
        print("CREATE      vcluster range requested (HelmRelease applied)")
        for _ in range(60):
            hr = await kube.get_custom_object(group="helm.toolkit.fluxcd.io", version="v2",
                plural="helmreleases", namespace=vc.namespace, name="vcluster")
            cond = [c for c in (hr or {}).get("status",{}).get("conditions",[]) if c["type"]=="Ready"]
            if cond and cond[0]["status"] == "True":
                print(f"VCLUSTER    Ready=True reason={cond[0].get('reason')}"); break
            if cond and cond[0].get("reason") in ("InstallFailed","ArtifactFailed"):
                print(f"VCLUSTER    FAILED {cond[0].get('message','')[:150]}"); ok=False; break
            await asyncio.sleep(5)
        else:
            print("VCLUSTER    timed out"); ok=False

        for p in (a, b, vc):
            await life.destroy(p)
        print("DESTROY     all three requested")
        for _ in range(60):
            if not any([await kube.namespace_exists(p.namespace) for p in (a,b,vc)]): break
            await asyncio.sleep(5)
        for p in (a, b, vc):
            r = await life.residue(p)
            print(f"RESIDUE     {p.namespace}: {r.describe()}")
            ok &= r.is_clean
    except Exception as e:
        print(f"ERROR       {type(e).__name__}: {str(e)[:300]}"); ok = False
    finally:
        for p in (a,b,vc):
            try: await kube.delete_namespace(p.namespace)
            except Exception: pass
        await kube.close()
    print("\n" + ("LIFECYCLE SMOKE PASSED" if ok else "LIFECYCLE SMOKE FAILED"))
    return 0 if ok else 1

sys.exit(asyncio.run(main()))
