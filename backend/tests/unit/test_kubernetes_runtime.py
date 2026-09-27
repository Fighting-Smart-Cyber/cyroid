"""`KubernetesRuntime` -- COSMOS PG #266.

Driven against a fake cluster so the runtime's own logic is what is under test: scope enforcement,
evidence extraction, and idempotent teardown. The real client is exercised separately against the
k3s cluster on pg-devtest.
"""

from dataclasses import dataclass, field

import pytest

from proving_ground.capability.kube import JobOutcome, KubeClient, PodExec
from proving_ground.capability.kubernetes_runtime import (
    KubernetesRuntime,
    ScopeViolation,
    _extract_evidence,
)
from proving_ground.capability.models import Isolation, Placement, Scope
from proving_ground.capability.runtime import CapabilitySpec, ChartRef, HookSpec, ImageRef

DIGEST = "registry.example/logc2@sha256:" + "b" * 64


@dataclass
class FakeKube:
    """A cluster that records what was asked of it."""

    job_logs: str = '{"passed": true, "convoy_id": "C-17"}'
    job_succeeds: bool = True
    namespaces: set = field(default_factory=set)
    calls: list = field(default_factory=list)
    objects: dict = field(default_factory=dict)
    scaled: list = field(default_factory=list)
    rbac: list = field(default_factory=list)
    pvs: list = field(default_factory=list)
    policies: list = field(default_factory=list)
    helm_status: dict = field(
        default_factory=lambda: {
            "conditions": [{"type": "Ready", "status": "True"}],
            "history": [{"version": 1}],
        }
    )
    # KubeVirt. A VM's `printableStatus` is served from its queue in order, the last value
    # sticking, so a test can script Provisioning -> Starting -> Running. A VM not listed here
    # is Running at once.
    vm_statuses: dict = field(default_factory=dict)
    vmi_interfaces: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    secrets: dict = field(default_factory=dict)
    closed: bool = False

    async def get_secret(self, *, namespace, name):
        return self.secrets.get((namespace, name))

    async def list_cluster_custom_objects(self, *, group, version, plural, label_selector):
        self.calls.append(("list_cluster_custom_objects", plural, label_selector))
        # As the cluster does: a VirtualMachineInstance exists for each VirtualMachine that is
        # running, and carries the VM's labels.
        stored = "virtualmachines" if plural == "virtualmachineinstances" else plural
        key, _, value = label_selector.partition("=")
        out = []
        for (p, _ns, _name), obj in self.objects.items():
            if p != stored:
                continue
            labels = (obj.get("metadata") or {}).get("labels") or {}
            if labels.get(key) == value or (not value and key in labels):
                out.append({**obj, "status": {"phase": self.vmi_phase}})
        return out

    vmi_phase: str = "Running"

    vnc_frames: list = field(default_factory=list)  # what the "VM" sends
    vnc_received: list = field(default_factory=list)  # what the "VM" got
    vnc_opened: list = field(default_factory=list)

    def open_vnc(self, *, namespace, name):
        fake = self

        class _Stream:
            """Sends its frames, then stays open until the browser has sent something (or a
            second passes), then hangs up -- so a test can see bytes go both ways without
            racing the close."""

            def __init__(self):
                self._frames = list(fake.vnc_frames)

            async def send(self, data):
                fake.vnc_received.append(bytes(data))

            async def receive(self):
                import asyncio

                if self._frames:
                    return self._frames.pop(0)
                for _ in range(100):
                    if fake.vnc_received:
                        break
                    await asyncio.sleep(0.01)
                return None

        class _Ctx:
            async def __aenter__(self):
                fake.vnc_opened.append((namespace, name))
                return _Stream()

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def recent_warnings(self, *, namespace, limit=10):
        return list(self.warnings)[-limit:]

    async def close(self):
        self.closed = True

    async def ensure_namespace(self, name, labels):
        self.calls.append(("ensure_namespace", name))
        self.namespaces.add(name)

    async def delete_namespace(self, name):
        self.calls.append(("delete_namespace", name))
        self.namespaces.discard(name)

    async def namespace_exists(self, name):
        return name in self.namespaces

    async def run_job(self, *, namespace, name, image, command, timeout_seconds):
        self.calls.append(("run_job", namespace, name))
        return JobOutcome(succeeded=self.job_succeeds, logs=self.job_logs, duration_seconds=1.5)

    async def exec_in_pod(self, *, namespace, selector, command):
        self.calls.append(("exec_in_pod", namespace, tuple(command)))
        return PodExec(exit_code=0, stdout="ok", stderr="")

    async def delete_workloads(self, *, namespace, selector):
        self.calls.append(("delete_workloads", namespace, selector))

    async def apply_custom_object(self, *, group, version, plural, namespace, name, body):
        self.calls.append(("apply_custom_object", plural, namespace, name))
        # As the API server: an object carrying server-managed fields is not something to create.
        if "resourceVersion" in (body.get("metadata") or {}) or "status" in body:
            raise RuntimeError("resourceVersion should not be set on objects to be created")
        self.objects[(plural, namespace, name)] = dict(body)
        if plural == "helmreleases" and name == "vcluster":
            # The chart exports the vcluster's kubeconfig as a secret once it is up.
            self.secrets.setdefault(
                (namespace, "vc-vcluster"),
                {
                    "config": f"apiVersion: v1\nkind: Config\nclusters:\n- name: vc\n  cluster:\n    server: https://vcluster.{namespace}:443\n"
                },
            )

    async def get_custom_object(self, *, group, version, plural, namespace, name):
        obj = self.objects.get((plural, namespace, name))
        if obj is not None and plural == "helmreleases":
            # What a GET returns: the stored object plus what the server added to it.
            metadata = {**obj.get("metadata", {}), "resourceVersion": "1", "uid": "u"}
            obj = {**obj, "metadata": metadata, "status": self.helm_status}
        if obj is not None and plural == "virtualmachines":
            queue = self.vm_statuses.setdefault(name, ["Running"])
            status = queue.pop(0) if len(queue) > 1 else queue[0]
            obj = {**obj, "status": {"printableStatus": status}}
        if plural == "virtualmachineinstances":
            vm = self.objects.get(("virtualmachines", namespace, name))
            if vm is None:
                return None
            return {
                "metadata": {"name": name, "namespace": namespace},
                "status": {"phase": "Running", "interfaces": self.vmi_interfaces.get(name, [])},
            }
        return obj

    async def delete_custom_object(self, *, group, version, plural, namespace, name):
        self.calls.append(("delete_custom_object", plural, name))
        self.objects.pop((plural, namespace, name), None)

    async def scale_workloads(self, *, namespace, replicas):
        self.calls.append(("scale_workloads", namespace, replicas))
        self.scaled.append((namespace, replicas))
        return 2

    @property
    def vm_running(self):
        return {
            name: obj["spec"].get("running", True)
            for (plural, _ns, name), obj in self.objects.items()
            if plural == "virtualmachines"
        }

    async def set_virtual_machines_running(self, *, namespace, running):
        self.calls.append(("set_virtual_machines_running", namespace, running))
        changed = 0
        for (plural, ns, _name), obj in self.objects.items():
            if plural == "virtualmachines" and ns == namespace:
                if obj["spec"].get("running", True) != running:
                    obj["spec"] = {**obj["spec"], "running": running}
                    changed += 1
        return changed

    async def cluster_rbac_matching(self, needle):
        return [r for r in self.rbac if needle in r]

    async def list_persistent_volumes_matching(self, needle):
        return [v for v in self.pvs if needle in v]

    ingresses: dict = field(default_factory=dict)

    async def apply_ingress(self, *, namespace, body):
        self.calls.append(("apply_ingress", namespace, body["metadata"]["name"]))
        self.ingresses[(namespace, body["metadata"]["name"])] = dict(body)

    async def list_ingresses_for_class(self, ingress_class):
        """What pg-gateway reads to learn what is published."""
        return [
            body
            for body in self.ingresses.values()
            if (body.get("spec") or {}).get("ingressClassName") == ingress_class
        ]

    async def apply_network_policy(self, *, namespace, body):
        self.calls.append(("apply_network_policy", namespace, body["metadata"]["name"]))
        self.policies.append((namespace, body))


def hook(cmd=("/bin/run",)):
    return HookSpec(image=ImageRef(DIGEST), command=cmd)


def spec(scope=Scope.PER_LEARNER, **kw):
    base = dict(
        name="logc2",
        version="1.0.0",
        chart=ChartRef(name="logc2", version="1.4.2", repository="https://charts.example"),
        scope=scope,
        verify=hook(),
        seed=hook(),
        reset=hook(),
    )
    base.update(kw)
    return CapabilitySpec(**base)


def ns_placement(namespace="pg-alpha-jones"):
    return Placement(isolation=Isolation.NAMESPACE, namespace=namespace, reason="test")


def vc_placement():
    return Placement(
        isolation=Isolation.VCLUSTER,
        namespace="pg-alpha",
        vcluster="pg-cohort-alpha",
        reason="test",
    )


@pytest.fixture
def kube():
    return FakeKube()


@pytest.fixture
def runtime(kube):
    return KubernetesRuntime(kube)


class TestScopeIsEnforcedNotJustDeclared:
    async def test_per_learner_reset_on_a_vcluster_is_refused(self, runtime):
        # The quiet version of this bug resets 29 other people's work.
        with pytest.raises(ScopeViolation, match="per-learner"):
            await runtime.reset(spec(Scope.PER_LEARNER), vc_placement())

    async def test_per_learner_verify_on_a_vcluster_is_refused(self, runtime):
        with pytest.raises(ScopeViolation):
            await runtime.verify(spec(Scope.PER_LEARNER), vc_placement())

    async def test_per_learner_in_its_own_namespace_is_allowed(self, runtime):
        result = await runtime.reset(spec(Scope.PER_LEARNER), ns_placement())
        assert result.succeeded

    async def test_per_cohort_in_a_bare_namespace_is_refused(self, runtime):
        # The inverse mistake: resets one learner, silently misses the cohort.
        with pytest.raises(ScopeViolation, match="per-cohort"):
            await runtime.reset(spec(Scope.PER_COHORT), ns_placement())

    async def test_per_cohort_on_its_vcluster_is_allowed(self, runtime):
        assert (await runtime.reset(spec(Scope.PER_COHORT), vc_placement())).succeeded

    async def test_shared_scope_is_allowed_anywhere(self, runtime):
        assert (await runtime.reset(spec(Scope.SHARED), ns_placement())).succeeded
        assert (await runtime.reset(spec(Scope.SHARED), vc_placement())).succeeded


class TestResetClearsBeforeReseeding:
    async def test_reset_deletes_workloads_then_runs_the_hook(self, runtime, kube):
        await runtime.reset(spec(), ns_placement())
        kinds = [c[0] for c in kube.calls]
        assert kinds.index("delete_workloads") < kinds.index("run_job")

    async def test_reset_is_scoped_to_the_placement_namespace(self, runtime, kube):
        await runtime.reset(spec(), ns_placement("pg-alpha-jones"))
        deletes = [c for c in kube.calls if c[0] == "delete_workloads"]
        assert deletes and all(c[1] == "pg-alpha-jones" for c in deletes)

    async def test_a_capability_with_no_reset_hook_succeeds_without_touching_anything(
        self, runtime, kube
    ):
        result = await runtime.reset(spec(reset=None), ns_placement())
        assert result.succeeded
        assert not any(c[0] == "run_job" for c in kube.calls)


class TestVerifyProducesEvidence:
    async def test_evidence_comes_from_the_hook_output(self, runtime):
        r = await runtime.verify(spec(), ns_placement())
        assert r.passed
        assert r.evidence["convoy_id"] == "C-17"
        assert r.observed_at

    async def test_a_failing_hook_does_not_pass(self, kube):
        kube.job_succeeds = False
        kube.job_logs = '{"passed": false, "reason": "convoy never dispatched"}'
        r = await KubernetesRuntime(kube).verify(spec(), ns_placement())
        assert not r.passed
        assert r.evidence["reason"] == "convoy never dispatched"

    async def test_a_hook_claiming_failure_is_believed_over_its_exit_code(self, kube):
        # Exit 0 with passed:false is a hook telling us the learner did not do the thing.
        kube.job_succeeds = True
        kube.job_logs = '{"passed": false}'
        assert not (await KubernetesRuntime(kube).verify(spec(), ns_placement())).passed

    async def test_a_silent_hook_still_yields_evidence(self, kube):
        kube.job_logs = ""
        r = await KubernetesRuntime(kube).verify(spec(), ns_placement())
        assert r.evidence["raw_output"] == "<no output>"


class TestEvidenceExtraction:
    def test_the_last_json_object_wins(self):
        logs = 'starting\n{"passed": false}\nretrying\n{"passed": true, "n": 2}\n'
        assert _extract_evidence(logs) == {"passed": True, "n": 2}

    def test_noise_around_the_json_is_tolerated(self):
        assert _extract_evidence('WARN deprecated\n{"passed": true}\ndone')["passed"] is True

    def test_unparseable_output_is_kept_verbatim(self):
        assert _extract_evidence("kaboom")["raw_output"] == "kaboom"

    def test_a_json_array_is_not_mistaken_for_evidence(self):
        assert "raw_output" in _extract_evidence("[1, 2, 3]")

    def test_a_bare_scalar_is_not_mistaken_for_evidence(self):
        assert "raw_output" in _extract_evidence("true")

    def test_a_pretty_printed_object_is_found(self):
        logs = 'checking...\n{\n  "passed": true,\n  "convoy_id": "C-17"\n}\n'
        assert _extract_evidence(logs) == {"passed": True, "convoy_id": "C-17"}

    def test_a_later_pretty_printed_object_beats_an_earlier_one(self):
        logs = '{\n  "passed": false\n}\nretry\n{\n  "passed": true\n}\n'
        assert _extract_evidence(logs) == {"passed": True}

    def test_a_brace_in_prose_does_not_break_extraction(self):
        logs = 'saw { in the config\n{"passed": true}\n'
        assert _extract_evidence(logs) == {"passed": True}


class TestLifecycle:
    async def test_install_creates_the_namespace_before_the_chart(self, runtime, kube):
        await runtime.install(spec(), ns_placement())
        kinds = [c[0] for c in kube.calls]
        assert kinds.index("ensure_namespace") < kinds.index("apply_custom_object")

    async def test_uninstall_of_something_never_installed_is_not_an_error(self, runtime):
        await runtime.uninstall(spec(), ns_placement("never-existed"))

    async def test_exec_requires_a_command(self, runtime):
        with pytest.raises(ValueError, match="command"):
            await runtime.exec(spec(), ns_placement(), [])

    async def test_exec_reaches_the_placement_namespace(self, runtime, kube):
        r = await runtime.exec(spec(), ns_placement("pg-alpha-jones"), ["ls"])
        assert r.exit_code == 0
        assert ("exec_in_pod", "pg-alpha-jones", ("ls",)) in kube.calls


def test_the_fake_satisfies_the_port():
    assert isinstance(FakeKube(), KubeClient)
