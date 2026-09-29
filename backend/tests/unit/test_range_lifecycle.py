"""Range lifecycle under both placement resolutions -- COSMOS PG-41."""

import pytest

from proving_ground.capability.kube import KubeClient
from proving_ground.capability.lifecycle import (
    VCLUSTER_CHART,
    IngressConfig,
    RangeLifecycle,
    Residue,
    WorkloadState,
    default_deny_policy,
)
from proving_ground.capability.runtime import CapabilitySpec, ChartRef, HookSpec, ImageRef, WebSpec
from proving_ground.capability.models import Isolation, Placement, Scope
from proving_ground.capability.networking import NetworkSpec
from proving_ground.capability.workload import DiskSpec, InterfaceSpec, OSFamily, WorkloadSpec

from .test_kubernetes_runtime import FakeKube

# FakeKube implements the whole KubeClient port, including the lifecycle surface.
LifecycleFake = FakeKube


def ns_placement():
    return Placement(isolation=Isolation.NAMESPACE, namespace="pg-alpha-jones", reason="t")


def vc_placement():
    return Placement(
        isolation=Isolation.VCLUSTER,
        namespace="pg-alpha",
        vcluster="pg-cohort-alpha",
        reason="t",
    )


@pytest.fixture
def kube():
    return LifecycleFake()


@pytest.fixture
def lifecycle(kube):
    return RangeLifecycle(kube)


class TestCreateUnderBothResolutions:
    async def test_a_namespace_range_creates_only_a_namespace(self, lifecycle, kube):
        await lifecycle.create(ns_placement())
        assert "pg-alpha-jones" in kube.namespaces
        assert not any(c[0] == "apply_custom_object" for c in kube.calls)

    async def test_a_vcluster_range_also_installs_the_chart(self, lifecycle, kube):
        await lifecycle.create(vc_placement())
        plurals = [c[1] for c in kube.calls if c[0] == "apply_custom_object"]
        assert plurals == ["helmrepositories", "helmreleases"]

    async def test_the_source_precedes_the_release(self, lifecycle, kube):
        await lifecycle.create(vc_placement())
        order = [c[1] for c in kube.calls if c[0] == "apply_custom_object"]
        assert order.index("helmrepositories") < order.index("helmreleases")

    async def test_embedded_etcd_is_not_requested(self, lifecycle, kube):
        # It is a licensed feature; asking for it CrashLoopBackOffs with a message that reads
        # like a bug rather than a billing decision.
        await lifecycle.create(vc_placement())
        release = kube.objects[("helmreleases", "pg-alpha", "vcluster")]
        assert "controlPlane" not in release["spec"]["values"]

    async def test_the_exported_kubeconfig_names_the_vcluster_by_a_name_its_cert_covers(
        self, lifecycle, kube
    ):
        # The chart's default export says https://localhost:8443, which is right for a laptop
        # and useless for helm-controller. The cert's SANs cover `vcluster.<ns>` and not
        # `vcluster.<ns>.svc` -- found on pg-devtest, tls: x509 "not vcluster.vc-probe.svc".
        await lifecycle.create(vc_placement())
        release = kube.objects[("helmreleases", "pg-alpha", "vcluster")]
        assert release["spec"]["values"]["exportKubeConfig"] == {
            "server": "https://vcluster.pg-alpha:443"
        }

    async def test_await_vcluster_ready_returns_its_kubeconfig(self, lifecycle, kube):
        await lifecycle.create(vc_placement())
        config = await lifecycle.await_vcluster_ready(
            vc_placement(), timeout_seconds=5, poll_seconds=0
        )
        assert config["clusters"][0]["cluster"]["server"] == "https://vcluster.pg-alpha:443"

    async def test_await_vcluster_ready_on_a_namespace_placement_is_a_programming_error(
        self, lifecycle
    ):
        with pytest.raises(ValueError, match="no vcluster"):
            await lifecycle.await_vcluster_ready(ns_placement(), timeout_seconds=0)

    async def test_await_vcluster_ready_times_out_without_the_secret(self, lifecycle, kube):
        await lifecycle.create(vc_placement())
        kube.secrets.clear()
        with pytest.raises(TimeoutError, match="kubeconfig"):
            await lifecycle.await_vcluster_ready(vc_placement(), timeout_seconds=0, poll_seconds=0)

    async def test_vcluster_ready_is_none_until_the_vcluster_exists(self, lifecycle, kube):
        # Teardown after a failed deploy must not wait on a vcluster that was never created.
        assert await lifecycle.vcluster_kubeconfig(vc_placement()) is None
        await lifecycle.create(vc_placement())
        assert await lifecycle.vcluster_kubeconfig(vc_placement()) is not None

    async def test_the_chart_version_is_pinned(self):
        assert VCLUSTER_CHART.version == "0.37.0"
        assert VCLUSTER_CHART.version != "latest"


class TestIsolationFloor:
    async def test_every_range_gets_a_default_deny_on_create(self, lifecycle, kube):
        # Without this, a pod in one learner's range reaches another learner's by IP and nothing
        # objects. Measured on k3s: a cross-range probe returned EXIT=0.
        await lifecycle.create(ns_placement())
        assert [p[0] for p in kube.policies] == ["pg-alpha-jones"]

    async def test_a_vcluster_range_gets_one_too(self, lifecycle, kube):
        await lifecycle.create(vc_placement())
        assert kube.policies and kube.policies[0][0] == "pg-alpha"

    def test_same_range_traffic_is_still_allowed(self):
        # An empty `from` would deny same-range traffic too and break every multi-tier range.
        policy = default_deny_policy("pg-alpha")
        assert policy["spec"]["ingress"] == [{"from": [{"podSelector": {}}]}]

    def test_it_is_ingress_only(self):
        # A range that cannot reach DNS or a chart repo cannot start.
        assert default_deny_policy("pg-alpha")["spec"]["policyTypes"] == ["Ingress"]

    def test_the_control_plane_is_let_in_by_namespace(self):
        # The API pod runs hooks against a vcluster's API and helm-controller deploys into it;
        # both live outside the range. Found on pg-devtest: "Connect call failed" from the API
        # pod to vcluster.<ns>:443, with the same request succeeding in a namespace that had no
        # policy. Which namespaces those are is configuration, not something this module knows.
        policy = default_deny_policy("pg-x", control_plane_namespaces=("pg-system", "flux-system"))
        froms = policy["spec"]["ingress"][0]["from"]
        assert {"podSelector": {}} in froms
        selectors = [
            f["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            for f in froms
            if "namespaceSelector" in f
        ]
        assert selectors == ["pg-system", "flux-system"]

    def test_with_no_control_plane_named_only_the_range_itself_is_allowed(self):
        assert default_deny_policy("pg-x")["spec"]["ingress"][0]["from"] == [{"podSelector": {}}]

    async def test_the_lifecycle_passes_its_control_plane_through(self, kube):
        lifecycle = RangeLifecycle(kube, control_plane_namespaces=("pg-system",))
        await lifecycle.create(ns_placement())
        froms = kube.policies[0][1]["spec"]["ingress"][0]["from"]
        assert any(f.get("namespaceSelector") for f in froms)

    def test_it_selects_every_pod_in_the_range(self):
        assert default_deny_policy("pg-alpha")["spec"]["podSelector"] == {}


class TestStopAndStartPreserveState:
    async def test_stop_scales_to_zero_rather_than_deleting(self, lifecycle, kube):
        await lifecycle.stop(ns_placement())
        assert kube.scaled == [("pg-alpha-jones", 0)]
        # A stopped range must come back with the learner's work intact.
        assert not any(c[0] == "delete_namespace" for c in kube.calls)

    async def test_start_scales_back_up(self, lifecycle, kube):
        await lifecycle.start(ns_placement())
        assert kube.scaled == [("pg-alpha-jones", 1)]

    async def test_stop_reports_how_many_it_changed(self, lifecycle):
        # "stopped 0" and "stopped 4" must be distinguishable, or a no-op reads as success.
        assert await lifecycle.stop(ns_placement()) == 2

    async def test_stop_works_the_same_for_a_vcluster(self, lifecycle, kube):
        await lifecycle.stop(vc_placement())
        assert kube.scaled == [("pg-alpha", 0)]

    async def test_stop_also_stops_the_ranges_vms(self, lifecycle, kube):
        # Scaling Deployments to zero and leaving two KubeVirt VMs running is a "stopped" range
        # still burning 8 GB. A VM stops by `spec.running: false`, keeping its disks.
        await lifecycle.apply_workloads((workload("web"), workload("db")), NETS, ns_placement())
        assert await lifecycle.stop(ns_placement()) == 2 + 2
        assert kube.vm_running == {"web": False, "db": False}
        assert not any(c[0] == "delete_custom_object" for c in kube.calls)

    async def test_start_brings_the_vms_back(self, lifecycle, kube):
        await lifecycle.apply_workloads((workload("web"),), NETS, ns_placement())
        await lifecycle.stop(ns_placement())
        assert await lifecycle.start(ns_placement()) == 2 + 1
        assert kube.vm_running == {"web": True}

    async def test_a_vm_already_in_the_target_state_is_not_counted(self, lifecycle, kube):
        await lifecycle.apply_workloads((workload("web"),), NETS, ns_placement())
        assert await lifecycle.start(ns_placement()) == 2 + 0


class TestDestroyAndResidue:
    async def test_destroy_removes_the_namespace(self, lifecycle, kube):
        await lifecycle.create(ns_placement())
        await lifecycle.destroy(ns_placement())
        assert "pg-alpha-jones" not in kube.namespaces

    async def test_a_vcluster_release_is_deleted_before_its_namespace(self, lifecycle, kube):
        # Order matters and getting it wrong is silent: only helm-controller's uninstall removes
        # the chart's cluster-scoped RBAC, and a namespace-first delete races it and strands both
        # the ClusterRole and its binding. Observed on k3s.
        await lifecycle.create(vc_placement())
        await lifecycle.destroy(vc_placement())
        kinds = [c[0] for c in kube.calls]
        assert kinds.index("delete_custom_object") < kinds.index("delete_namespace")

    async def test_a_namespace_range_needs_no_release_delete(self, lifecycle, kube):
        await lifecycle.create(ns_placement())
        await lifecycle.destroy(ns_placement())
        assert not any(c[0] == "delete_custom_object" for c in kube.calls)

    async def test_a_clean_teardown_reports_clean(self, lifecycle):
        await lifecycle.destroy(ns_placement())
        residue = await lifecycle.residue(ns_placement())
        assert residue.is_clean
        assert residue.describe() == "clean"

    async def test_a_surviving_namespace_is_residue(self, lifecycle, kube):
        await lifecycle.create(ns_placement())
        residue = await lifecycle.residue(ns_placement())
        assert not residue.is_clean
        assert "namespace still present" in residue.describe()

    async def test_cluster_scoped_rbac_is_residue(self, lifecycle, kube):
        # This is where teardown residue actually hides: cluster-scoped objects do not go with
        # the namespace. vcluster names its RBAC vc-<release>-v-<namespace>.
        kube.rbac = ["clusterrole/vc-vcluster-v-pg-alpha"]
        residue = await lifecycle.residue(vc_placement())
        assert not residue.is_clean
        assert "vc-vcluster-v-pg-alpha" in residue.describe()

    async def test_stranded_volumes_are_residue(self, lifecycle, kube):
        kube.pvs = ["pv/pvc-abc-pg-alpha"]
        assert not (await lifecycle.residue(vc_placement())).is_clean

    async def test_residue_of_an_untouched_placement_is_clean(self, lifecycle):
        assert (await lifecycle.residue(vc_placement())).is_clean


class TestNoNewColumns:
    def test_placement_is_all_the_lifecycle_needs(self):
        """No method takes substrate state, because none is stored.

        Teardown re-derives placement rather than reading a stored vcluster name.
        `Range.dind_container_id` is the mistake being unwound; `Range.vcluster_name` would
        repeat it in a new coat. Keyword-only knobs like a timeout are fine -- what must not
        appear is a positional argument carrying where the range *was* put.
        """
        import inspect

        for method in ("create", "stop", "start", "destroy", "residue"):
            sig = inspect.signature(getattr(RangeLifecycle, method))
            positional = [
                name
                for name, p in sig.parameters.items()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
            assert positional == ["self", "placement"], f"{method} takes {positional}"


def test_the_fake_satisfies_the_port():
    assert isinstance(LifecycleFake(), KubeClient)


def test_residue_describe_lists_every_category():
    r = Residue(namespace=True, cluster_rbac=("cr/x",), persistent_volumes=("pv/y",))
    described = r.describe()
    assert "namespace" in described and "cr/x" in described and "pv/y" in described


# ---------------------------------------------------------------------------------------------
# Networks and workloads -- the gap the 2026-09-15 handoff names: the manifest builders existed,
# were tested, and nothing called them.
# ---------------------------------------------------------------------------------------------

NETS = (
    NetworkSpec(name="dmz", subnet="172.30.10.0/24", gateway="172.30.10.1"),
    NetworkSpec(name="internal", subnet="172.30.20.0/24"),
)


def workload(name="web", *interfaces):
    return WorkloadSpec(
        name=name,
        os_family=OSFamily.LINUX,
        os_version="ubuntu-24.04",
        cpus=1,
        memory_mb=512,
        disks=(DiskSpec(name="root", size_gb=10, boot=True),),
        interfaces=interfaces or (InterfaceSpec(network="dmz", ip_address="172.30.10.5"),),
    )


def applied(kube, plural):
    return [c for c in kube.calls if c[0] == "apply_custom_object" and c[1] == plural]


class TestNetworksBecomeAttachmentDefinitions:
    async def test_each_network_is_applied_in_the_range_namespace(self, lifecycle, kube):
        names = await lifecycle.apply_networks(NETS, ns_placement())
        assert names == ("pg-net-dmz", "pg-net-internal")
        assert [c[2:] for c in applied(kube, "network-attachment-definitions")] == [
            ("pg-alpha-jones", "pg-net-dmz"),
            ("pg-alpha-jones", "pg-net-internal"),
        ]

    async def test_the_body_is_a_multus_definition(self, lifecycle, kube):
        await lifecycle.apply_networks(NETS, ns_placement())
        body = kube.objects[("network-attachment-definitions", "pg-alpha-jones", "pg-net-dmz")]
        assert body["kind"] == "NetworkAttachmentDefinition"
        assert body["apiVersion"] == "k8s.cni.cncf.io/v1"

    async def test_no_networks_applies_nothing(self, lifecycle, kube):
        assert await lifecycle.apply_networks((), ns_placement()) == ()
        assert not applied(kube, "network-attachment-definitions")


class TestWorkloadsBecomeVirtualMachines:
    async def test_each_workload_is_a_kubevirt_vm_in_the_range_namespace(self, lifecycle, kube):
        names = await lifecycle.apply_workloads(
            (workload("web"), workload("db")), NETS, ns_placement()
        )
        assert names == ("web", "db")
        body = kube.objects[("virtualmachines", "pg-alpha-jones", "web")]
        assert body["kind"] == "VirtualMachine"
        assert body["metadata"]["namespace"] == "pg-alpha-jones"

    async def test_the_vm_attaches_to_the_range_network(self, lifecycle, kube):
        await lifecycle.apply_workloads((workload(),), NETS, ns_placement())
        body = kube.objects[("virtualmachines", "pg-alpha-jones", "web")]
        multus = [n for n in body["spec"]["template"]["spec"]["networks"] if "multus" in n]
        assert multus == [
            {"name": "net1", "multus": {"networkName": "pg-alpha-jones/pg-net-dmz-web"}}
        ]

    async def test_each_interface_gets_its_attachment_before_its_vm(self, lifecycle, kube):
        # The attachment carries the address; a VM applied before it fails at sandbox creation.
        web = workload(
            "web",
            InterfaceSpec(network="dmz", ip_address="172.30.10.5"),
            InterfaceSpec(network="internal", ip_address="172.30.20.5"),
        )
        await lifecycle.apply_workloads((web,), NETS, ns_placement())
        order = [(c[1], c[3]) for c in kube.calls if c[0] == "apply_custom_object"]
        assert order == [
            ("network-attachment-definitions", "pg-net-dmz-web"),
            ("network-attachment-definitions", "pg-net-internal-web"),
            ("virtualmachines", "web"),
        ]
        nad = kube.objects[("network-attachment-definitions", "pg-alpha-jones", "pg-net-dmz-web")]
        assert "172.30.10.5/24" in nad["spec"]["config"]

    async def test_extra_labels_reach_the_vm_and_its_pod(self, lifecycle, kube):
        # The range id travels as a label, never as a column on the VM row.
        await lifecycle.apply_workloads(
            (workload(),), NETS, ns_placement(), extra_labels={"pg.range/id": "r-1"}
        )
        body = kube.objects[("virtualmachines", "pg-alpha-jones", "web")]
        assert body["metadata"]["labels"]["pg.range/id"] == "r-1"
        assert body["spec"]["template"]["metadata"]["labels"]["pg.range/id"] == "r-1"

    async def test_an_undeclared_network_is_refused_before_anything_is_applied(
        self, lifecycle, kube
    ):
        # Validate every manifest, then apply. A bad third workload must not leave two VMs
        # behind in a range that then reports an error -- that is the Era A partial-failure
        # shape, and the point of the port is not to reproduce it.
        bad = workload("bad", InterfaceSpec(network="nope", ip_address="10.9.9.9"))
        with pytest.raises(ValueError, match="unknown network 'nope'"):
            await lifecycle.apply_workloads(
                (workload("a"), workload("b"), bad), NETS, ns_placement()
            )
        assert not applied(kube, "virtualmachines")

    async def test_an_interface_without_an_address_is_refused(self, lifecycle, kube):
        # Static IPAM cannot invent one, and a multi-homed host whose second interface comes up
        # empty looks deployed while half the exercise silently does not work.
        with pytest.raises(ValueError, match="no address"):
            await lifecycle.apply_workloads(
                (workload("web", InterfaceSpec(network="dmz")),), NETS, ns_placement()
            )
        assert not applied(kube, "virtualmachines")
        assert not applied(kube, "network-attachment-definitions")


class TestAwaitingWorkloads:
    async def test_running_vms_report_their_addresses_as_evidence(self, lifecycle, kube):
        kube.vm_statuses["web"] = ["Provisioning", "Starting", "Running"]
        kube.vmi_interfaces["web"] = [
            {"name": "default", "ipAddress": "10.42.0.9"},
            {"name": "net1", "ipAddress": "172.30.10.5"},
        ]
        await lifecycle.apply_workloads((workload(),), NETS, ns_placement())
        states = await lifecycle.await_workloads_running(
            (workload(),), ns_placement(), timeout_seconds=5, poll_seconds=0
        )
        assert states == (
            WorkloadState(
                name="web",
                status="Running",
                addresses={"default": "10.42.0.9", "net1": "172.30.10.5"},
            ),
        )

    async def test_a_terminal_failure_raises_at_once_with_its_status(self, lifecycle, kube):
        # A DataVolume that cannot import will never become Running. Sitting out ten minutes to
        # say "timed out" hides the reason; the status names it in the first poll.
        kube.vm_statuses["web"] = ["DataVolumeError"]
        await lifecycle.apply_workloads((workload(),), NETS, ns_placement())
        with pytest.raises(RuntimeError, match="web.*DataVolumeError"):
            await lifecycle.await_workloads_running(
                (workload(),), ns_placement(), timeout_seconds=60, poll_seconds=0
            )

    async def test_a_timeout_names_the_last_status_seen(self, lifecycle, kube):
        kube.vm_statuses["web"] = ["WaitingForVolumeBinding"]
        await lifecycle.apply_workloads((workload(),), NETS, ns_placement())
        with pytest.raises(TimeoutError, match="web.*WaitingForVolumeBinding"):
            await lifecycle.await_workloads_running(
                (workload(),), ns_placement(), timeout_seconds=0, poll_seconds=0
            )

    async def test_a_timeout_carries_the_namespaces_warnings(self, lifecycle, kube):
        # The VM said "Provisioning" for ten minutes while the launcher pod's events said exactly
        # what was wrong. The status is the summary; the warnings are the reason.
        kube.vm_statuses["web"] = ["Provisioning"]
        kube.warnings.append("FailedCreatePodSandBox: IPAM plugin returned missing IP config")
        await lifecycle.apply_workloads((workload(),), NETS, ns_placement())
        with pytest.raises(TimeoutError, match="missing IP config"):
            await lifecycle.await_workloads_running(
                (workload(),), ns_placement(), timeout_seconds=0, poll_seconds=0
            )

    async def test_a_vm_that_never_appears_is_a_timeout_not_a_crash(self, lifecycle):
        with pytest.raises(TimeoutError, match="not found"):
            await lifecycle.await_workloads_running(
                (workload(),), ns_placement(), timeout_seconds=0, poll_seconds=0
            )

    async def test_no_workloads_is_immediately_done(self, lifecycle):
        assert await lifecycle.await_workloads_running((), ns_placement()) == ()


class TestDestroyWaitsForWhatTheNamespaceLeavesBehind:
    async def test_residue_is_checked_after_the_namespace_is_actually_gone(self, kube):
        # A namespace delete returns before the namespace is gone; it sits in Terminating while
        # its VMs and PVCs are reaped. Checking residue at that instant reports "namespace still
        # present" for every teardown, which makes the check worthless.
        class Lingering(FakeKube):
            polls = 0

            async def namespace_exists(self, name):
                self.polls += 1
                return name in self.namespaces or self.polls <= 2

        kube = Lingering()
        lifecycle = RangeLifecycle(kube)
        await lifecycle.create(ns_placement())
        await lifecycle.destroy(ns_placement(), namespace_timeout=5, poll_seconds=0)
        assert kube.polls >= 2
        assert (await lifecycle.residue(ns_placement())).is_clean

    async def test_residue_is_checked_after_the_volumes_are_gone_too(self):
        # A PV outlives the namespace whose claim bound it: the namespace goes once its PVC
        # objects are gone, but the PV is cluster-scoped and stays Released until the CSI driver
        # has deleted the disk behind it. Measured on AKS with disk.csi.azure.com -- namespace
        # gone at 21s, PV gone at 34s -- and residue() run in that window called every teardown
        # dirty, so DELETE /ranges/{id} answered 502 and kept the row for a clean teardown.
        #
        # local-path PVs are host directories reaped with the namespace, which is why k3s never
        # showed this and every AKS teardown failed.
        class SlowDisk(FakeKube):
            polls = 0

            async def list_persistent_volumes_matching(self, needle):
                self.polls += 1
                return ["pv/pvc-slow"] if self.polls <= 2 else []

        kube = SlowDisk()
        lifecycle = RangeLifecycle(kube)
        await lifecycle.create(ns_placement())
        # No volume_timeout here on purpose: the default has to be what waits, so this
        # fails on the assertion rather than on an unknown argument if the wait is lost.
        await lifecycle.destroy(ns_placement(), poll_seconds=0)
        assert kube.polls >= 2
        assert (await lifecycle.residue(ns_placement())).is_clean

    async def test_a_volume_that_never_goes_is_still_reported_not_waited_on_forever(self):
        # The wait is a courtesy to a slow driver, not a retry loop. A Retain reclaim policy
        # strands a PV for good, and that has to arrive as residue rather than as a hang.
        class Stranded(FakeKube):
            async def list_persistent_volumes_matching(self, needle):
                return ["pv/pvc-retained"]

        kube = Stranded()
        lifecycle = RangeLifecycle(kube)
        await lifecycle.create(ns_placement())
        await lifecycle.destroy(ns_placement(), volume_timeout=0, poll_seconds=0)
        residue = await lifecycle.residue(ns_placement())
        assert not residue.is_clean
        assert "pv/pvc-retained" in residue.describe()


# ---------------------------------------------------------------------------------------------
# Student-facing ingress -- COSMOS PG-62. Each range's application gets its own URL on the
# cluster's ingress, authorised on the data path by ForwardAuth back to the platform.
# ---------------------------------------------------------------------------------------------

INGRESS = IngressConfig(
    class_name="traefik",
    # A host of its own per range, because an application on the platform's host is same-origin
    # with the console. `.invalid` is reserved by RFC 2606 and resolves nowhere.
    apps_host="apps.example.invalid",
    authz_url="http://pg-api.pg-system.svc:8000/api/v1/range-apps/authz",
)


def capability(name="logc2", web=WebSpec(service="logc2-web", port=8080)):
    return CapabilitySpec(
        name=name,
        version="1.0.0",
        chart=ChartRef(name=name, version="1.0.0", repository="https://charts.example"),
        scope=Scope.PER_LEARNER,
        verify=HookSpec(image=ImageRef("registry.example/x@sha256:" + "a" * 64), command=("/v",)),
        web=web,
    )


@pytest.fixture
def routed(kube):
    return RangeLifecycle(kube, ingress=INGRESS)


class TestRangeIngress:
    async def test_each_web_capability_gets_an_ingress_under_the_ranges_own_prefix(
        self, routed, kube
    ):
        routes = await routed.apply_app_ingress((capability(),), ns_placement(), range_key="r1")
        assert routes == {"logc2": "https://r1.apps.example.invalid/logc2/"}
        ingress = kube.ingresses[("pg-alpha-jones", "pg-app-logc2")]
        rule_group = ingress["spec"]["rules"][0]
        assert rule_group["host"] == "r1.apps.example.invalid"
        rule = rule_group["http"]["paths"][0]
        assert rule["path"] == "/logc2" and rule["pathType"] == "Prefix"
        assert rule["backend"]["service"] == {"name": "logc2-web", "port": {"number": 8080}}
        assert ingress["spec"]["ingressClassName"] == "pg-gateway"

    async def test_authorisation_and_stripping_are_no_longer_the_platforms_problem(
        self, routed, kube
    ):
        """They were a ForwardAuth middleware and a StripPrefix middleware, chained by
        annotation -- both traefik.io/v1alpha1, and therefore unavailable on every other ingress
        controller. Both now happen inside pg-gateway, so nothing controller-specific is written
        and the ordering that used to matter has no chain left to be wrong about.
        """
        await routed.apply_app_ingress((capability(),), ns_placement(), range_key="r1")

        assert not [key for key in kube.objects if key[0] == "middlewares"]
        ingress = kube.ingresses[("pg-alpha-jones", "pg-app-logc2")]
        assert not ingress["metadata"].get("annotations")
        assert ingress["metadata"]["labels"] == {"pg.app/name": "logc2", "pg.range/id": "r1"}

    async def test_without_an_applications_host_nothing_is_published(self, kube):
        # The refusal that matters: no second origin means no application, not an application
        # on the platform's own host where its JavaScript can read the operator's session.
        from dataclasses import replace

        lifecycle = RangeLifecycle(kube, ingress=replace(INGRESS, apps_host=""))
        routes = await lifecycle.apply_app_ingress((capability(),), ns_placement(), range_key="r1")
        assert routes == {} and not kube.ingresses and not kube.objects

    async def test_a_capability_with_no_web_surface_gets_no_route(self, routed, kube):
        routes = await routed.apply_app_ingress(
            (capability(web=None),), ns_placement(), range_key="r1"
        )
        assert routes == {} and not kube.ingresses

    async def test_without_ingress_configured_nothing_is_routed(self, lifecycle, kube):
        # A profile with no ingress class named (or the DinD path) routes nothing, loudly.
        with pytest.raises(ValueError, match="no ingress configured"):
            await lifecycle.apply_app_ingress((capability(),), ns_placement(), range_key="r1")

    def test_the_default_deny_lets_the_ingress_controller_in(self):
        policy = default_deny_policy(
            "pg-x",
            ingress_controller=("kube-system", {"app.kubernetes.io/name": "traefik"}),
        )
        froms = policy["spec"]["ingress"][0]["from"]
        assert {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}},
        } in froms
