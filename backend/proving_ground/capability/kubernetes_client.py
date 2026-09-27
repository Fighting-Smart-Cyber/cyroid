"""The real `KubeClient`, over `kubernetes_asyncio`.

Mechanism only. Every decision -- what to place where, what a scope entitles an operation to,
what counts as evidence -- lives in `KubernetesRuntime` and `placement.py`. If a policy question
starts to look like it belongs here, it doesn't.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.stream import WsApiClient

from .kube import JobOutcome, PodExec, VncStream

logger = logging.getLogger(__name__)

__all__ = [
    "CONTROL_PLANE_SERVICE_ACCOUNT",
    "RANGE_CLUSTER_ROLE",
    "RANGE_ROLE_BINDING",
    "KubernetesApiClient",
]

_MANAGED_BY = {"app.kubernetes.io/managed-by": "proving-ground"}

# The chart's own names, restated here because this is the side that has to write them into each
# range's namespace. `backend/tests/unit/test_cluster_posture.py` fails if the chart renames one.
CONTROL_PLANE_SERVICE_ACCOUNT = "pg"
RANGE_CLUSTER_ROLE = "pg-range"
RANGE_ROLE_BINDING = "pg-range"

# Every pod is given its own service account's namespace here by the kubelet. It is the only
# place this process can learn which namespace to name as the RoleBinding's subject without the
# deployment being asked to repeat it in configuration -- and a value repeated in configuration
# is one that can disagree with where the pod is actually running.
_SERVICE_ACCOUNT_NAMESPACE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


class KubernetesApiClient:
    """Implements the `KubeClient` port against a live cluster."""

    def __init__(self, api_client: client.ApiClient) -> None:
        self._api = api_client
        self._core = client.CoreV1Api(api_client)
        self._batch = client.BatchV1Api(api_client)
        self._custom = client.CustomObjectsApi(api_client)
        self._apps = client.AppsV1Api(api_client)
        self._rbac = client.RbacAuthorizationV1Api(api_client)
        self._net = client.NetworkingV1Api(api_client)
        # Only a client that *is* the control plane's service account grants that account
        # anything. A client built from a vcluster's exported kubeconfig is a different identity
        # inside a different cluster, where neither `pg-range` nor the `pg` service account
        # exists; binding there would write a dangling RoleBinding into every vcluster range.
        self._is_control_plane = False

    @classmethod
    async def in_cluster(cls) -> KubernetesApiClient:
        config.load_incluster_config()
        instance = cls(client.ApiClient())
        instance._is_control_plane = True
        return instance

    @classmethod
    async def connect(cls) -> KubernetesApiClient:
        """In-cluster when running as a pod, kubeconfig otherwise.

        The same code has to run from the seam harness on a developer's shell and from the API
        pod inside the cluster it manages. A pod has `KUBERNETES_SERVICE_HOST` and a mounted
        service-account token; a shell has `KUBECONFIG` or `~/.kube/config`. An explicit
        `KUBECONFIG` wins even inside a pod, so a pod can be pointed at another cluster
        deliberately -- which is what a vcluster's kubeconfig will be.
        """
        if os.environ.get("KUBERNETES_SERVICE_HOST") and not os.environ.get("KUBECONFIG"):
            return await cls.in_cluster()
        return await cls.from_kubeconfig()

    @classmethod
    async def from_kubeconfig(cls, path: str | None = None, context: str | None = None):
        await config.load_kube_config(config_file=path, context=context)
        return cls(client.ApiClient())

    @classmethod
    async def from_kubeconfig_dict(cls, kubeconfig: Mapping[str, object]) -> KubernetesApiClient:
        """A client for another cluster -- a vcluster's exported kubeconfig, in practice.

        Its own `Configuration`, not the process-wide default: the host client and the vcluster
        client coexist in one deploy, and the loader's default would make the second one
        silently replace the first.
        """
        configuration = client.Configuration()
        await config.load_kube_config_from_dict(
            config_dict=dict(kubeconfig), client_configuration=configuration
        )
        return cls(client.ApiClient(configuration))

    async def close(self) -> None:
        await self._api.close()

    # ------------------------------------------------------------- namespaces

    async def ensure_namespace(self, name: str, labels: Mapping[str, str]) -> None:
        body = client.V1Namespace(
            metadata=client.V1ObjectMeta(name=name, labels={**_MANAGED_BY, **dict(labels)})
        )
        try:
            await self._core.create_namespace(body=body)
        except ApiException as exc:
            if exc.status != 409:  # already exists -- ensure_* is idempotent by definition
                raise
            await self._core.patch_namespace(name=name, body=body)
        await self._ensure_range_role_binding(name)

    async def _ensure_range_role_binding(self, namespace: str) -> None:
        """Grant this control plane the range-scoped ClusterRole inside this namespace.

        Everything the runtime does to a range -- exec a hook, read a pod's log, scale a
        workload, write a NetworkAttachmentDefinition -- happens in one range's namespace, and
        `deploy/helm/proving-ground/templates/rbac.yaml` can hold those verbs either cluster-wide
        or here. This is what makes the second choice possible: the namespace carries the grant,
        so the API's token is not a cluster-wide exec key.

        A RoleBinding to a ClusterRole rather than a Role written from scratch, because RBAC
        refuses to let an account grant what it does not hold; `bind` on that one named
        ClusterRole is the way through, and the chart grants exactly that.
        """
        if not self._is_control_plane:
            return
        # Imported inside the function, as `capability/specs.py` does and for the same reason:
        # `proving_ground.capability` is the licence boundary (ADR-0011) and importing the
        # contract must not drag the engine's settings in behind it.
        from ..config import get_settings

        if get_settings().range_rbac_scope != "namespace":
            # The chart granted the range verbs cluster-wide, so this binding would grant nothing
            # -- and the chart does not grant the rolebinding write in that mode, so attempting it
            # is a 403 on every range creation. The chart tells the pod which posture it rendered.
            return
        try:
            with open(_SERVICE_ACCOUNT_NAMESPACE) as handle:
                own_namespace = handle.read().strip()
        except OSError:
            own_namespace = ""
        if not own_namespace:
            return

        body = client.V1RoleBinding(
            metadata=client.V1ObjectMeta(
                name=RANGE_ROLE_BINDING, namespace=namespace, labels=dict(_MANAGED_BY)
            ),
            role_ref=client.V1RoleRef(
                api_group="rbac.authorization.k8s.io", kind="ClusterRole", name=RANGE_CLUSTER_ROLE
            ),
            subjects=[
                client.RbacV1Subject(
                    kind="ServiceAccount",
                    name=CONTROL_PLANE_SERVICE_ACCOUNT,
                    namespace=own_namespace,
                )
            ],
        )
        try:
            await self._rbac.create_namespaced_role_binding(namespace=namespace, body=body)
        except ApiException as exc:
            if exc.status == 409:  # already bound; roleRef is immutable, so there is nothing to do
                return
            if exc.status == 403:
                # An install whose chart predates this grant. Say so rather than failing the
                # deploy: while the cluster-wide role still carries the range verbs the binding
                # is redundant, and refusing to create a range over a redundant grant is the
                # worse outcome. Once the chart is narrowed the next call fails with its own 403,
                # and this line is the one that explains it.
                logger.warning(
                    "not permitted to bind %s into %s (%s): the range will work only while "
                    "rbac.rangePermissions is 'cluster'; upgrade the chart to grant it",
                    RANGE_CLUSTER_ROLE,
                    namespace,
                    exc.reason,
                )
                return
            raise

    async def delete_namespace(self, name: str) -> None:
        try:
            await self._core.delete_namespace(name=name)
        except ApiException as exc:
            if exc.status != 404:
                raise

    async def namespace_exists(self, name: str) -> bool:
        try:
            await self._core.read_namespace(name=name)
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise
        return True

    # ------------------------------------------------------------------- jobs

    async def run_job(
        self,
        *,
        namespace: str,
        name: str,
        image: str,
        command: Sequence[str],
        timeout_seconds: int,
    ) -> JobOutcome:
        """Run a hook to completion and return what it said.

        The job is deleted before it is created so a re-run is not a 409, and its pods are kept
        (`propagation_policy="Orphan"` is not used -- see the read-then-delete order below) only
        long enough to read the logs. Logs are read *before* cleanup because a deleted job takes
        its pods, and with them the evidence, with it.
        """
        await self._delete_job(namespace=namespace, name=name)

        job = client.V1Job(
            metadata=client.V1ObjectMeta(name=name, labels=dict(_MANAGED_BY)),
            spec=client.V1JobSpec(
                backoff_limit=0,  # a hook that failed is a finding, not something to retry blindly
                ttl_seconds_after_finished=600,
                active_deadline_seconds=timeout_seconds,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={**_MANAGED_BY, "job-name": name}),
                    spec=client.V1PodSpec(
                        restart_policy="Never",
                        containers=[
                            client.V1Container(name="hook", image=image, command=list(command))
                        ],
                    ),
                ),
            ),
        )
        started = time.monotonic()
        await self._batch.create_namespaced_job(namespace=namespace, body=job)

        succeeded = await self._await_job(
            namespace=namespace, name=name, timeout_seconds=timeout_seconds
        )
        logs = await self._job_logs(namespace=namespace, name=name)
        await self._delete_job(namespace=namespace, name=name)

        return JobOutcome(
            succeeded=succeeded, logs=logs, duration_seconds=time.monotonic() - started
        )

    async def _await_job(self, *, namespace: str, name: str, timeout_seconds: int) -> bool:
        deadline = time.monotonic() + timeout_seconds + 15
        while time.monotonic() < deadline:
            job = await self._batch.read_namespaced_job_status(name=name, namespace=namespace)
            status = job.status
            if status and status.succeeded:
                return True
            if status and status.failed:
                return False
            await asyncio.sleep(1)
        return False  # the deadline itself is the finding; the logs say what it got stuck on

    async def _job_logs(self, *, namespace: str, name: str) -> str:
        pods = await self._core.list_namespaced_pod(
            namespace=namespace, label_selector=f"job-name={name}"
        )
        chunks = []
        for pod in pods.items:
            try:
                # Raw, not preloaded: the client JSON-decodes any body it can and `str()`s the
                # result, which turns a hook's `{"passed": true}` into `{'passed': True}` --
                # the repr of evidence rather than the evidence.
                response = await self._core.read_namespaced_pod_log(
                    name=pod.metadata.name, namespace=namespace, _preload_content=False
                )
                chunks.append(await response.text())
            except ApiException:
                # A pod that never started has no logs. Absence is not an error here -- the
                # runtime turns empty output into evidence rather than a silent pass.
                continue
        return "\n".join(c for c in chunks if c)

    async def _delete_job(self, *, namespace: str, name: str) -> None:
        try:
            await self._batch.delete_namespaced_job(
                name=name,
                namespace=namespace,
                body=client.V1DeleteOptions(propagation_policy="Background"),
            )
        except ApiException as exc:
            if exc.status != 404:
                raise

    # ------------------------------------------------------------------- pods

    async def exec_in_pod(
        self, *, namespace: str, selector: str, command: Sequence[str]
    ) -> PodExec:
        pods = await self._core.list_namespaced_pod(
            namespace=namespace,
            label_selector=selector,
            field_selector="status.phase=Running",
        )
        if not pods.items:
            raise RuntimeError(f"no running pod matches {selector!r} in {namespace}")

        async with WsApiClient() as ws:
            core = client.CoreV1Api(ws)
            stdout = await core.connect_get_namespaced_pod_exec(
                name=pods.items[0].metadata.name,
                namespace=namespace,
                command=list(command),
                stdout=True,
                stderr=True,
                stdin=False,
                tty=False,
            )
        # The websocket helper collapses the streams; exit status is not carried separately, so a
        # caller that needs one should have its hook report it rather than inferring from text.
        return PodExec(exit_code=0, stdout=stdout or "", stderr="")

    async def delete_workloads(self, *, namespace: str, selector: str) -> None:
        await self._batch.delete_collection_namespaced_job(
            namespace=namespace, label_selector=selector
        )
        await self._core.delete_collection_namespaced_pod(
            namespace=namespace, label_selector=selector
        )

    # -------------------------------------------------------- custom resources

    async def apply_custom_object(
        self,
        *,
        group: str,
        version: str,
        plural: str,
        namespace: str,
        name: str,
        body: Mapping[str, object],
    ) -> None:
        """Create, or replace if it already exists.

        Deliberately not a JSON-merge patch: a HelmRelease whose `values` shrink must actually
        shrink, and a merge patch cannot remove a key. Replace is the operation that makes the
        declared object the real one.
        """
        try:
            await self._custom.create_namespaced_custom_object(
                group=group, version=version, namespace=namespace, plural=plural, body=dict(body)
            )
            return
        except ApiException as exc:
            if exc.status != 409:
                raise

        existing = await self._custom.get_namespaced_custom_object(
            group=group, version=version, namespace=namespace, plural=plural, name=name
        )
        merged = dict(body)
        metadata = dict(merged.get("metadata") or {})
        metadata["resourceVersion"] = existing["metadata"]["resourceVersion"]
        merged["metadata"] = metadata
        await self._custom.replace_namespaced_custom_object(
            group=group, version=version, namespace=namespace, plural=plural, name=name, body=merged
        )

    async def get_custom_object(
        self, *, group: str, version: str, plural: str, namespace: str, name: str
    ) -> dict | None:
        try:
            return await self._custom.get_namespaced_custom_object(
                group=group, version=version, namespace=namespace, plural=plural, name=name
            )
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    async def delete_custom_object(
        self, *, group: str, version: str, plural: str, namespace: str, name: str
    ) -> None:
        try:
            await self._custom.delete_namespaced_custom_object(
                group=group, version=version, namespace=namespace, plural=plural, name=name
            )
        except ApiException as exc:
            if exc.status != 404:
                raise

    # ----------------------------------------------------------------- lifecycle

    async def scale_workloads(self, *, namespace: str, replicas: int) -> int:
        """Scale every Deployment and StatefulSet in the namespace.

        Returns the count actually changed. A workload already at the target is not counted --
        "stopped 0 workloads" is a meaningfully different answer from "stopped 4", and a caller
        that cannot tell them apart will report a no-op as a success.
        """
        changed = 0
        body = {"spec": {"replicas": replicas}}

        deployments = await self._apps.list_namespaced_deployment(namespace=namespace)
        for item in deployments.items:
            if (item.spec.replicas or 0) != replicas:
                await self._apps.patch_namespaced_deployment_scale(
                    name=item.metadata.name, namespace=namespace, body=body
                )
                changed += 1

        statefulsets = await self._apps.list_namespaced_stateful_set(namespace=namespace)
        for item in statefulsets.items:
            if (item.spec.replicas or 0) != replicas:
                await self._apps.patch_namespaced_stateful_set_scale(
                    name=item.metadata.name, namespace=namespace, body=body
                )
                changed += 1

        return changed

    async def set_virtual_machines_running(self, *, namespace: str, running: bool) -> int:
        """Stop or start every KubeVirt VM in the namespace, keeping its disks.

        `spec.running` is the declared state; virt-controller tears the VMI down or brings it
        back. Same counting rule as `scale_workloads`: a VM already in the target state is not
        a change.
        """
        listed = await self._custom.list_namespaced_custom_object(
            group="kubevirt.io", version="v1", namespace=namespace, plural="virtualmachines"
        )
        changed = 0
        for vm in listed.get("items", []):
            if (vm.get("spec") or {}).get("running", True) == running:
                continue
            # A JSON Patch, not a merge patch: the client sends the first content type the API
            # offers for custom objects, which is json-patch, and rejects a dict body with
            # "cannot unmarshal object into Go value of type []jsonPatchOp". `add` replaces an
            # existing value and creates a missing one, so a VM declared with a runStrategy
            # rather than `running` is handled too.
            await self._custom.patch_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
                name=vm["metadata"]["name"],
                body=[{"op": "add", "path": "/spec/running", "value": running}],
            )
            changed += 1
        return changed

    async def cluster_rbac_matching(self, needle: str) -> list[str]:
        """Cluster-scoped RBAC naming `needle`.

        Cluster-scoped objects do not go with a namespace, so this is where teardown residue
        hides. vcluster names its RBAC `vc-<release>-v-<namespace>`, which is why matching on the
        namespace finds it.
        """
        found = []
        roles = await self._rbac.list_cluster_role()
        found += [
            f"clusterrole/{r.metadata.name}" for r in roles.items if needle in r.metadata.name
        ]
        bindings = await self._rbac.list_cluster_role_binding()
        found += [
            f"clusterrolebinding/{b.metadata.name}"
            for b in bindings.items
            if needle in b.metadata.name
        ]
        return found

    async def list_persistent_volumes_matching(self, needle: str) -> list[str]:
        """PVs whose claimRef names the namespace. A Retain reclaim policy strands these."""
        volumes = await self._core.list_persistent_volume()
        return [
            f"pv/{v.metadata.name}"
            for v in volumes.items
            if v.spec.claim_ref and v.spec.claim_ref.namespace == needle
        ]

    @asynccontextmanager
    async def open_vnc(self, *, namespace: str, name: str) -> AsyncIterator[VncStream]:
        """The VMI's VNC subresource, as a websocket on the API server's own connection pool.

        Same TLS and the same bearer token the REST calls use; KubeVirt's virt-api upgrades the
        request and bridges it to the launcher pod's VNC socket. `plain.kubevirt.io` is the
        subprotocol virtctl asks for.
        """
        cfg = self._api.configuration
        token = await cfg.get_api_key_with_prefix("BearerToken")
        url = (
            f"{cfg.host}/apis/subresources.kubevirt.io/v1/namespaces/{namespace}"
            f"/virtualmachineinstances/{name}/vnc"
        )
        headers = {"Authorization": token} if token else {}
        async with self._api.rest_client.pool_manager.ws_connect(
            url, headers=headers, protocols=("plain.kubevirt.io",), max_msg_size=0
        ) as ws:
            yield _AiohttpVnc(ws)

    async def list_cluster_custom_objects(
        self, *, group: str, version: str, plural: str, label_selector: str
    ) -> list[dict]:
        """One call across every namespace, selected by label.

        The alternative -- asking each range's namespace in turn -- is a request per range on a
        page that loads for every user; this stays one request however many ranges exist.
        """
        listed = await self._custom.list_cluster_custom_object(
            group=group, version=version, plural=plural, label_selector=label_selector
        )
        return list(listed.get("items", []))

    async def get_secret(self, *, namespace: str, name: str) -> dict[str, str] | None:
        try:
            secret = await self._core.read_namespaced_secret(name=name, namespace=namespace)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        return {key: base64.b64decode(value).decode() for key, value in (secret.data or {}).items()}

    async def recent_warnings(self, *, namespace: str, limit: int = 10) -> list[str]:
        """The namespace's most recent Warning events, newest last, one line each."""
        events = await self._core.list_namespaced_event(
            namespace=namespace, field_selector="type=Warning"
        )
        ordered = sorted(
            events.items,
            key=lambda e: e.last_timestamp or e.event_time or e.metadata.creation_timestamp,
        )
        return [
            f"{e.involved_object.kind}/{e.involved_object.name} {e.reason}: "
            f"{(e.message or '').strip()[:300]}"
            for e in ordered[-limit:]
        ]

    async def apply_ingress(self, *, namespace: str, body: Mapping[str, object]) -> None:
        name = body["metadata"]["name"]  # type: ignore[index]
        try:
            await self._net.create_namespaced_ingress(namespace=namespace, body=dict(body))
        except ApiException as exc:
            if exc.status != 409:
                raise
            await self._net.replace_namespaced_ingress(
                name=name, namespace=namespace, body=dict(body)
            )

    async def list_ingresses_for_class(self, ingress_class: str) -> list[Mapping[str, object]]:
        """Cluster-wide, filtered in the caller rather than by the API server.

        `spec.ingressClassName` is not a field selector Kubernetes indexes, so asking for it
        server-side is not available; the list is one object per published application and is
        small enough that filtering here costs nothing worth naming.
        """
        listing = await self._net.list_ingress_for_all_namespaces()
        out: list[Mapping[str, object]] = []
        for item in listing.items:
            if (item.spec.ingress_class_name or "") != ingress_class:
                continue
            out.append(self._api.sanitize_for_serialization(item))
        return out

    async def apply_network_policy(self, *, namespace: str, body: Mapping[str, object]) -> None:
        name = body["metadata"]["name"]  # type: ignore[index]
        try:
            await self._net.create_namespaced_network_policy(namespace=namespace, body=dict(body))
        except ApiException as exc:
            if exc.status != 409:
                raise
            await self._net.replace_namespaced_network_policy(
                name=name, namespace=namespace, body=dict(body)
            )


class _AiohttpVnc:
    """`VncStream` over an aiohttp websocket."""

    def __init__(self, ws) -> None:
        self._ws = ws

    async def send(self, data: bytes) -> None:
        await self._ws.send_bytes(data)

    async def receive(self) -> bytes | None:
        msg = await self._ws.receive()
        if msg.type == 2:  # aiohttp.WSMsgType.BINARY
            return bytes(msg.data)
        if msg.type == 1:  # TEXT -- RFB is binary, but a text frame is still bytes on the wire
            return msg.data.encode()
        return None  # CLOSE, CLOSING, CLOSED, ERROR
