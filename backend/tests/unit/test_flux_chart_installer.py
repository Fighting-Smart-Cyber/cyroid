"""`FluxChartInstaller` -- decided 2026-09-13, amends ADR-0012's cluster capability contract.

An upstream chart becomes a `HelmRelease`, reconciled by helm-controller. PROVING GROUND never
shells out to the `helm` binary.
"""

import pytest

from proving_ground.capability.flux import FluxChartInstaller
from proving_ground.capability.models import Isolation, Placement, Scope
from proving_ground.capability.runtime import CapabilitySpec, ChartRef, HookSpec, ImageRef

from .test_kubernetes_runtime import FakeKube

DIGEST = "registry.example/logc2@sha256:" + "c" * 64


def spec(**kw):
    base = dict(
        name="logc2",
        version="1.0.0",
        chart=ChartRef(name="logc2", version="1.4.2", repository="https://charts.example/logc2"),
        scope=Scope.PER_LEARNER,
        verify=HookSpec(image=ImageRef(DIGEST), command=("/bin/verify",)),
        values={"replicas": 2},
    )
    base.update(kw)
    return CapabilitySpec(**base)


def placement():
    return Placement(isolation=Isolation.NAMESPACE, namespace="pg-alpha-jones", reason="test")


def vc_placement():
    return Placement(
        isolation=Isolation.VCLUSTER, namespace="pg-alpha", vcluster="pg-cohort-alpha", reason="t"
    )


@pytest.fixture
def kube():
    return FakeKube()


@pytest.fixture
def installer(kube):
    return FluxChartInstaller(kube, timeout_seconds=1)


class TestItDeclaresRatherThanExecutes:
    async def test_a_helmrepository_and_a_helmrelease_are_created(self, installer, kube):
        await installer.install(spec(), placement())
        plurals = [c[1] for c in kube.calls if c[0] == "apply_custom_object"]
        assert plurals == ["helmrepositories", "helmreleases"]

    async def test_the_source_exists_before_the_release_that_references_it(self, installer, kube):
        await installer.install(spec(), placement())
        order = [c[1] for c in kube.calls if c[0] == "apply_custom_object"]
        assert order.index("helmrepositories") < order.index("helmreleases")

    async def test_the_chart_is_referenced_by_pinned_version(self, installer, kube):
        await installer.install(spec(), placement())
        release = kube.objects[("helmreleases", "pg-alpha-jones", "logc2")]
        chart = release["spec"]["chart"]["spec"]
        assert chart["chart"] == "logc2" and chart["version"] == "1.4.2"

    async def test_our_values_are_passed_and_the_chart_is_not_patched(self, installer, kube):
        await installer.install(spec(), placement())
        release = kube.objects[("helmreleases", "pg-alpha-jones", "logc2")]
        assert release["spec"]["values"] == {"replicas": 2}
        # A wrapper, never a fork: nothing in the declared object can modify the chart itself.
        assert not (set(release["spec"]["chart"]["spec"]) & {"patch", "patches", "overlay"})

    async def test_it_lands_in_the_placement_namespace(self, installer, kube):
        await installer.install(spec(), placement())
        assert all(c[2] == "pg-alpha-jones" for c in kube.calls if c[0] == "apply_custom_object")


class TestReadiness:
    async def test_a_ready_release_returns_its_revision(self, installer, kube):
        kube.helm_status = {
            "conditions": [{"type": "Ready", "status": "True"}],
            "history": [{"version": 4}],
        }
        assert (await installer.install(spec(), placement())).revision == 4

    async def test_a_failed_install_raises_with_the_controller_reason(self, installer, kube):
        kube.helm_status = {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": "InstallFailed",
                    "message": "chart not found in repository",
                }
            ]
        }
        with pytest.raises(RuntimeError, match="chart not found in repository"):
            await installer.install(spec(), placement())

    async def test_a_release_that_never_becomes_ready_times_out(self, installer, kube):
        kube.helm_status = {"conditions": [{"type": "Ready", "status": "Unknown"}]}
        with pytest.raises(TimeoutError):
            await installer.install(spec(), placement())


class TestUninstall:
    async def test_the_release_is_deleted_before_its_source(self, installer, kube):
        await installer.install(spec(), placement())
        await installer.uninstall(spec(), placement())
        # A HelmRelease whose HelmRepository has gone cannot finish its own uninstall.
        deleted = [c[1] for c in kube.calls if c[0] == "delete_custom_object"]
        assert deleted.index("helmreleases") < deleted.index("helmrepositories")


class TestInstallingIntoAVcluster:
    """The HelmRelease stays in the host namespace and is reconciled by the host's Flux; what
    changes is where it deploys. `spec.kubeConfig` points helm-controller at the vcluster's
    exported kubeconfig secret, so no second Flux runs inside the vcluster. Proven on
    pg-devtest 2026-09-16 (podinfo installed into a vcluster, its pod synced back to the host
    namespace as `…-x-<ns>-x-vcluster`)."""

    async def test_the_release_targets_the_vcluster_through_its_kubeconfig_secret(
        self, installer, kube
    ):
        await installer.install(spec(), vc_placement())
        release = kube.objects[("helmreleases", "pg-alpha", "logc2")]
        assert release["spec"]["kubeConfig"] == {
            "secretRef": {"name": "vc-vcluster", "key": "config"}
        }
        assert release["spec"]["targetNamespace"] == "pg-alpha"
        assert release["spec"]["install"]["createNamespace"] is True

    async def test_a_namespace_release_has_no_kubeconfig(self, installer, kube):
        await installer.install(spec(), placement())
        release = kube.objects[("helmreleases", "pg-alpha-jones", "logc2")]
        assert "kubeConfig" not in release["spec"]

    async def test_uninstall_waits_for_the_release_to_be_gone(self, kube):
        # Deleting the vcluster while helm-controller is still uninstalling from it strands the
        # HelmRelease on its finalizer, and the namespace with it. So uninstall is not done
        # until the object is.
        class Lingering(FakeKube):
            polls = 0

            async def delete_custom_object(self, *, group, version, plural, namespace, name):
                self.calls.append(("delete_custom_object", plural, name))
                self.lingering = (plural, namespace, name)

            async def get_custom_object(self, *, group, version, plural, namespace, name):
                if getattr(self, "lingering", None) == (plural, namespace, name):
                    self.polls += 1
                    if self.polls < 3:
                        return {"metadata": {"name": name}}
                    self.objects.pop((plural, namespace, name), None)
                    self.lingering = None
                    return None
                return await super().get_custom_object(
                    group=group, version=version, plural=plural, namespace=namespace, name=name
                )

        kube = Lingering()
        installer = FluxChartInstaller(kube, timeout_seconds=5, poll_seconds=0)
        await installer.install(spec(), vc_placement())
        await installer.uninstall(spec(), vc_placement())
        assert kube.polls >= 3
        assert ("helmreleases", "pg-alpha", "logc2") not in kube.objects
