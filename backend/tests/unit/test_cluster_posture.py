"""What the platform and its ranges may reach on the cluster -- SEC-023, SEC-024.

Two findings, one subject: the blast radius of a range and of the control plane that makes one.

SEC-023. The control-plane ClusterRole granted `pods/exec` and `namespaces delete` cluster-wide,
so anything reaching the API pod's token could exec into any pod on the cluster -- including the
platform's own Postgres -- and delete any namespace. Everything that only ever operates inside a
range now also exists as `pg-range`, which the control plane binds into each range's namespace as
it creates it, and `rbac.rangePermissions` decides whether the cluster-wide role still carries
those verbs too.

SEC-024. The range NetworkPolicy was ingress-only, so a learner-controlled machine -- hostile by
design in a red-team exercise -- had open egress to the platform's Postgres, MinIO, Redis and API
by service name, and to every other range.

The chart half needs `helm` to render and skips without it; the checks that do not need a cluster
or a renderer do not skip, so a regression is caught in CI either way.
"""

import ipaddress
import logging
import pathlib
import shutil
import subprocess
from functools import lru_cache

import pytest
import yaml

from proving_ground.capability.kubernetes_client import (
    CONTROL_PLANE_SERVICE_ACCOUNT,
    RANGE_CLUSTER_ROLE,
    RANGE_ROLE_BINDING,
    KubernetesApiClient,
)
from proving_ground.capability.lifecycle import (
    EgressFloor,
    RangeLifecycle,
    default_deny_policy,
)
from proving_ground.capability.models import Isolation, Placement

from .test_kubernetes_runtime import FakeKube

ROOT = pathlib.Path(__file__).resolve().parents[3]
CHART = ROOT / "deploy" / "helm" / "proving-ground"
RBAC_TEMPLATE = CHART / "templates" / "rbac.yaml"

needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


@lru_cache(maxsize=4)
def render(range_permissions: str = "") -> tuple[dict, ...]:
    """The whole chart, as the cluster would receive it."""
    command = [
        "helm",
        "template",
        "pg",
        str(CHART),
        "-n",
        "pg-system",
        "--set",
        "image.tag=t",
        # image.registry and minio.image have no defaults any more -- the engine is public and
        # ships no images, so naming one organisation's registry in the chart was both useless to
        # everyone else and the thing that kept publish-public refusing. Any value renders.
        "--set",
        "image.registry=registry.example.invalid/cyroid",
        "--set",
        "minio.image=registry.example.invalid/pg-storage@sha256:" + "0" * 64,
    ]
    if range_permissions:
        command += ["--set", f"rbac.rangePermissions={range_permissions}"]
    rendered = subprocess.run(command, check=True, capture_output=True, text=True).stdout
    return tuple(doc for doc in yaml.safe_load_all(rendered) if doc)


def role(docs, kind, name):
    for doc in docs:
        if doc.get("kind") == kind and doc["metadata"]["name"] == name:
            return doc
    raise AssertionError(f"the chart renders no {kind} named {name}")


def resources_of(doc) -> set[str]:
    return {resource for rule in doc["rules"] for resource in rule["resources"]}


# Everything the control plane does to a range, it does inside that range's namespace, so all of
# this can be held by a Role there. Derived by reading every method on `KubeClient` and the API
# call behind it in `kubernetes_client.py`; a method added there without a rule added here fails
# against a narrowed cluster with a 403 on the first deploy.
RANGE_SCOPED = {
    "pods",
    "pods/log",
    "pods/exec",
    "events",
    "secrets",
    "jobs",
    "jobs/status",
    "deployments",
    "statefulsets",
    "deployments/scale",
    "statefulsets/scale",
    "networkpolicies",
    "ingresses",
    "virtualmachines",
    "virtualmachineinstances",
    "virtualmachineinstances/vnc",
    "datavolumes",
    "network-attachment-definitions",
    "helmreleases",
    "helmrepositories",
}

# And the reasons a verb genuinely cannot be held in a namespace: a namespace is itself a
# cluster-scoped object, so creating and deleting one has nowhere else to live; `residue()` reads
# PersistentVolumes and cluster RBAC, which outlive the namespace they served and are the whole
# point of the check; the dashboard's machine count is one labelled list across every namespace;
# and the RoleBinding delegating the rest is written into a namespace as it is created.
CLUSTER_SCOPED = {
    "namespaces",
    "persistentvolumes",
    "clusterroles",
    "clusterrolebindings",
    "virtualmachineinstances",
    "rolebindings",
}


class TestTheClusterRoleIsNarrowedToWhatOnlyItCanDo:
    @needs_helm
    def test_it_grants_no_verb_a_namespace_role_could_carry(self):
        narrowed = role(render("namespace"), "ClusterRole", "pg-control-plane")
        assert resources_of(narrowed) == CLUSTER_SCOPED

    @needs_helm
    def test_exec_is_the_one_that_mattered(self):
        # The finding, stated as its own test: a token that can exec into any pod on the cluster
        # turns any code-execution bug in the API into cluster compromise rather than range
        # compromise.
        narrowed = role(render("namespace"), "ClusterRole", "pg-control-plane")
        assert "pods/exec" not in resources_of(narrowed)

    @needs_helm
    def test_the_default_is_still_what_every_install_had(self):
        # An upgrade that narrows a live cluster's permissions before its existing ranges have a
        # binding of their own 403s every operation on them. The operator opts in; the chart does
        # not decide for them.
        current = role(render(), "ClusterRole", "pg-control-plane")
        assert RANGE_SCOPED <= resources_of(current)

    @needs_helm
    def test_a_value_that_is_neither_is_refused_rather_than_guessed(self):
        # Falling back to the broad grant on a typo is a control that lies: the operator reads
        # their own values file and believes the narrow one is in force.
        with pytest.raises(subprocess.CalledProcessError) as raised:
            render("namesapce")
        assert "rbac.rangePermissions" in raised.value.stderr

    def test_the_range_rules_are_written_once(self):
        # Without a single definition the two roles drift, and the way they drift is silent: the
        # cluster-wide role keeps working while the narrowed one is missing a verb.
        text = RBAC_TEMPLATE.read_text()
        assert '{{- define "pg.rangeRules" -}}' in text
        before, _, rest = text.partition('{{- define "pg.rangeRules" -}}')
        body, _, after = rest.partition("\n{{- end }}")
        assert "pods/exec" in body
        assert "pods/exec" not in _rules_of(before) + _rules_of(after)


def _rules_of(text: str) -> str:
    """Template text with its prose stripped, so a comment naming a verb is not a grant."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


class TestTheRangeRoleCarriesTheRest:
    @needs_helm
    def test_it_covers_exactly_what_the_runtime_uses_in_a_range(self):
        assert resources_of(role(render(), "ClusterRole", RANGE_CLUSTER_ROLE)) == RANGE_SCOPED

    @needs_helm
    def test_it_is_the_same_role_whichever_way_the_cluster_role_is_set(self):
        assert role(render(), "ClusterRole", RANGE_CLUSTER_ROLE) == role(
            render("namespace"), "ClusterRole", RANGE_CLUSTER_ROLE
        )

    @needs_helm
    def test_nothing_binds_it_cluster_wide(self):
        # A ClusterRoleBinding to this role puts every verb back where it was, and the chart
        # would still look narrowed.
        bindings = [d for d in render("namespace") if d.get("kind") == "ClusterRoleBinding"]
        assert bindings and all(b["roleRef"]["name"] != RANGE_CLUSTER_ROLE for b in bindings)

    @needs_helm
    def test_the_control_plane_may_bind_that_role_and_no_other(self):
        # `bind` is what lets an account grant a permission it does not itself hold, which is the
        # only way a narrowed control plane can delegate exec into a range. Unrestricted, it is a
        # way to grant itself anything.
        cluster = role(render("namespace"), "ClusterRole", "pg-control-plane")
        binds = [r for r in cluster["rules"] if "bind" in r["verbs"]]
        assert [r["resourceNames"] for r in binds] == [[RANGE_CLUSTER_ROLE]]

    @needs_helm
    def test_the_names_the_api_writes_are_the_names_the_chart_renders(self):
        # The API writes the RoleBinding, so it has to spell both names itself. A rename on
        # either side alone leaves every range namespace bound to a role that is not there.
        docs = render()
        assert role(docs, "ClusterRole", RANGE_CLUSTER_ROLE)
        accounts = {d["metadata"]["name"] for d in docs if d.get("kind") == "ServiceAccount"}
        assert CONTROL_PLANE_SERVICE_ACCOUNT in accounts
        bound = [d for d in docs if d.get("kind") == "ClusterRoleBinding"]
        # Two accounts hold a cluster-scoped binding, and the second is the interesting one.
        # pg-gateway terminates requests from the software a range trains against, so it runs
        # apart from the control plane with a permission set of exactly one read-only rule --
        # see the ClusterRole assertion below. A THIRD name appearing here is a posture change
        # somebody has to argue for.
        assert sorted({s["name"] for b in bound for s in b["subjects"]}) == [
            CONTROL_PLANE_SERVICE_ACCOUNT,
            "pg-gateway",
        ]

    @needs_helm
    def test_the_gateway_may_only_read_ingresses(self):
        """The most exposed process on the platform holds one rule, and it is read-only.

        It is the pod that terminates connections from a range's applications. What keeps a
        compromise there from becoming a cluster compromise is that there is nothing else in
        its ServiceAccount -- no exec, no secrets, no write verb anywhere.
        """
        docs = render()
        gateway = [
            d
            for d in docs
            if d.get("kind") == "ClusterRole" and d["metadata"]["name"].endswith("pg-gateway")
        ]
        assert len(gateway) == 1, "pg-gateway should hold exactly one ClusterRole"
        rules = gateway[0]["rules"]
        assert rules == [
            {
                "apiGroups": ["networking.k8s.io"],
                "resources": ["ingresses"],
                "verbs": ["get", "list", "watch"],
            }
        ]

    @needs_helm
    def test_the_platform_keeps_write_access_to_its_own_release(self):
        # The in-UI update rewrites this install's own HelmRelease, which lives in the release
        # namespace and in no range. Narrowing without this turns "update" into a bare 403.
        own = role(render("namespace"), "Role", "pg-self")
        assert own["metadata"]["namespace"] == "pg-system"
        assert resources_of(own) == {"helmreleases"}


class RbacStub:
    def __init__(self, error=None):
        self.created = []
        self._error = error

    async def create_namespaced_role_binding(self, *, namespace, body):
        self.created.append((namespace, body))
        if self._error is not None:
            raise self._error


class CoreStub:
    async def create_namespace(self, *, body):
        return body


def api_error(status):
    from kubernetes_asyncio.client.exceptions import ApiException

    return ApiException(status=status, reason="Forbidden")


def control_plane_client(tmp_path, monkeypatch, rbac, scope="namespace"):
    """The API pod's own client: in-cluster, with a service-account namespace to read.

    `scope` is the RBAC posture the chart rendered. The binding is written only in the narrow
    one -- in the broad one the control plane already holds these verbs cluster-wide and is not
    granted the rolebinding write, so attempting it would 403 on every range creation.
    """
    from proving_ground.config import get_settings

    monkeypatch.setattr(get_settings(), "range_rbac_scope", scope, raising=False)
    namespace_file = tmp_path / "namespace"
    namespace_file.write_text("pg-system\n")
    monkeypatch.setattr(
        "proving_ground.capability.kubernetes_client._SERVICE_ACCOUNT_NAMESPACE",
        str(namespace_file),
    )
    client = KubernetesApiClient.__new__(KubernetesApiClient)
    client._core = CoreStub()
    client._rbac = rbac
    client._is_control_plane = True
    return client


class TestTheNamespaceCarriesTheGrant:
    async def test_the_broad_posture_writes_no_binding_at_all(self, tmp_path, monkeypatch):
        # The default. The chart grants the range verbs cluster-wide and does NOT grant the
        # rolebinding write, so a binding attempt here would be a 403 logged on every range
        # creation, for a grant that would add nothing.
        rbac = RbacStub()
        client = control_plane_client(tmp_path, monkeypatch, rbac, scope="cluster")
        await client.ensure_namespace("pg-alpha-jones", {})
        assert rbac.created == []

    async def test_creating_a_range_binds_the_range_role_in_it(self, tmp_path, monkeypatch):
        rbac = RbacStub()
        client = control_plane_client(tmp_path, monkeypatch, rbac)
        await client.ensure_namespace("pg-alpha-jones", {})
        (namespace, body) = rbac.created[0]
        assert namespace == "pg-alpha-jones"
        assert body.metadata.name == RANGE_ROLE_BINDING
        assert (body.role_ref.kind, body.role_ref.name) == ("ClusterRole", RANGE_CLUSTER_ROLE)
        subject = body.subjects[0]
        assert (subject.kind, subject.name, subject.namespace) == (
            "ServiceAccount",
            CONTROL_PLANE_SERVICE_ACCOUNT,
            "pg-system",
        )

    async def test_a_second_deploy_into_the_same_namespace_is_not_an_error(
        self, tmp_path, monkeypatch
    ):
        # roleRef is immutable, so there is nothing to reconcile -- but ensure_namespace is
        # called on every deploy and a redeploy must not fail on the binding it already made.
        client = control_plane_client(tmp_path, monkeypatch, RbacStub(api_error(409)))
        await client.ensure_namespace("pg-alpha-jones", {})

    async def test_a_chart_that_has_not_granted_the_bind_does_not_stop_a_deploy(
        self, tmp_path, monkeypatch, caplog
    ):
        # Running new code on an older chart. While the cluster-wide role still carries the range
        # verbs the binding is redundant, so refusing to create the range would be the worse
        # outcome -- but it is said out loud, because once narrowed it is the explanation.
        client = control_plane_client(tmp_path, monkeypatch, RbacStub(api_error(403)))
        with caplog.at_level(logging.WARNING):
            await client.ensure_namespace("pg-alpha-jones", {})
        assert "rbac.rangePermissions" in caplog.text

    async def test_a_real_failure_is_not_swallowed(self, tmp_path, monkeypatch):
        from kubernetes_asyncio.client.exceptions import ApiException

        client = control_plane_client(tmp_path, monkeypatch, RbacStub(api_error(500)))
        with pytest.raises(ApiException):
            await client.ensure_namespace("pg-alpha-jones", {})

    async def test_a_vcluster_client_binds_nothing(self, tmp_path, monkeypatch):
        # Inside a vcluster there is no `pg` service account and no `pg-range` ClusterRole, so
        # this would write a dangling RoleBinding into every vcluster range.
        rbac = RbacStub()
        client = control_plane_client(tmp_path, monkeypatch, rbac)
        client._is_control_plane = False
        await client.ensure_namespace("pg-alpha", {})
        assert rbac.created == []

    async def test_a_client_outside_a_cluster_binds_nothing(self, tmp_path, monkeypatch):
        # The seam harness on a developer's shell: a kubeconfig identity, no service account.
        rbac = RbacStub()
        client = control_plane_client(tmp_path, monkeypatch, rbac)
        monkeypatch.setattr(
            "proving_ground.capability.kubernetes_client._SERVICE_ACCOUNT_NAMESPACE",
            str(tmp_path / "absent"),
        )
        await client.ensure_namespace("pg-alpha", {})
        assert rbac.created == []


# --------------------------------------------------------------------------------- SEC-024

PLATFORM_SERVICE = "10.43.0.1"  # a ClusterIP: the platform's Postgres, MinIO, API, and the
PLATFORM_POD = "10.42.3.7"  # kube-apiserver; and a pod address, which is how another range
NODE = "10.10.100.100"  # answers, and the node under both.
PUBLIC = "23.45.67.89"  # quay.io, where the range's own boot disk is imported from


def egress_permits(policy: dict, address: str) -> bool:
    """Whether the policy lets a range's pod open a connection to this address.

    Selector rules (the range itself, DNS) never match a bare address, so this reads only the
    `ipBlock` rules -- which is exactly the question "can the machine reach the platform".
    """
    ip = ipaddress.ip_address(address)
    for rule in policy["spec"].get("egress", []):
        for destination in rule["to"]:
            block = destination.get("ipBlock")
            if block is None or ip not in ipaddress.ip_network(block["cidr"]):
                continue
            if not any(ip in ipaddress.ip_network(e) for e in block.get("except", [])):
                return True
    return False


def floored(**kwargs) -> dict:
    return default_deny_policy(
        "pg-alpha-jones",
        control_plane_namespaces=("pg-system", "flux-system"),
        egress=EgressFloor(**kwargs),
    )


class TestARangeMayNotReachThePlatform:
    def test_the_policy_governs_egress_at_all(self):
        assert floored()["spec"]["policyTypes"] == ["Ingress", "Egress"]

    def test_the_platforms_services_are_unreachable(self):
        # pg-postgres:5432, pg-minio:9000, pg-redis:6379 and pg-api:8000 all answer on the
        # service network. A learner owns the guest OS of their machine, so "it has a password"
        # is the only thing between a hostile machine and the database without this.
        assert not egress_permits(floored(), PLATFORM_SERVICE)

    def test_the_platforms_pods_are_unreachable_too(self):
        # Blocking the service network alone leaves the pod addresses behind it, which DNS will
        # happily hand out.
        assert not egress_permits(floored(), PLATFORM_POD)

    def test_the_node_is_unreachable(self):
        assert not egress_permits(floored(), NODE)

    def test_the_control_plane_is_let_in_but_not_out_to(self):
        # The asymmetry is the point: helm-controller and the API open connections *to* a range,
        # and a range opening one back to them is the finding.
        policy = floored()
        allowed_in = [
            f["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            for f in policy["spec"]["ingress"][0]["from"]
            if "namespaceSelector" in f
        ]
        assert allowed_in == ["pg-system", "flux-system"]
        out = [
            destination.get("namespaceSelector", {}).get("matchLabels", {})
            for rule in policy["spec"]["egress"]
            for destination in rule["to"]
        ]
        assert not any(
            labels.get("kubernetes.io/metadata.name") in ("pg-system", "flux-system")
            for labels in out
        )

    def test_dns_is_permitted_or_nothing_starts(self):
        rules = [r for r in floored()["spec"]["egress"] if r.get("ports")]
        assert len(rules) == 1
        assert rules[0]["ports"] == [
            {"protocol": "UDP", "port": 53},
            {"protocol": "TCP", "port": 53},
        ]
        assert rules[0]["to"] == [
            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}
        ]

    def test_nothing_outside_the_range_is_reachable_on_an_unrestricted_port(self):
        # kube-system holds more than CoreDNS. An allowance for that namespace with no `ports`
        # would put the cluster's own components back within reach of a learner's machine.
        for rule in floored()["spec"]["egress"]:
            if any("namespaceSelector" in destination for destination in rule["to"]):
                assert rule.get("ports"), rule

    def test_the_range_may_still_reach_itself(self):
        # A multi-tier range is the normal case; same-namespace is the unit of trust.
        assert {"to": [{"podSelector": {}}]} in floored()["spec"]["egress"]

    def test_the_internet_stays_reachable(self):
        # A boot disk is imported from a registry by a CDI pod in the range's own namespace. Deny
        # this and every range fails to come up, with the reason in a pod nobody is looking at.
        assert egress_permits(floored(), PUBLIC)

    def test_an_install_can_name_something_private_it_needs(self):
        # An air-gapped profile's mirror is inside the space this closes by default.
        assert egress_permits(floored(extra_destinations=("10.10.20.101/32",)), "10.10.20.101")

    def test_without_a_floor_the_policy_is_what_it_always_was(self):
        # The shape the rest of the suite and every live range already has.
        policy = default_deny_policy("pg-alpha-jones")
        assert policy["spec"]["policyTypes"] == ["Ingress"]
        assert "egress" not in policy["spec"]


def ns_placement():
    return Placement(isolation=Isolation.NAMESPACE, namespace="pg-alpha-jones", reason="t")


def vc_placement():
    return Placement(
        isolation=Isolation.VCLUSTER, namespace="pg-alpha", vcluster="pg-cohort-a", reason="t"
    )


class TestTheFloorIsAppliedWhereItIsSafeTo:
    async def test_a_namespace_range_gets_it_without_being_asked(self):
        kube = FakeKube()
        await RangeLifecycle(kube).create(ns_placement())
        assert kube.policies[0][1]["spec"]["policyTypes"] == ["Ingress", "Egress"]

    async def test_a_vcluster_range_keeps_open_egress_and_says_which(self, caplog):
        # Its syncer must reach the host cluster's API server, at an address kube-proxy has
        # already rewritten by the time a policy sees it. Closing it would hang the deploy.
        kube = FakeKube()
        with caplog.at_level(logging.WARNING):
            await RangeLifecycle(kube).create(vc_placement())
        assert kube.policies[0][1]["spec"]["policyTypes"] == ["Ingress"]
        assert "vcluster" in caplog.text and "API server" in caplog.text

    async def test_the_range_still_gets_exactly_one_policy(self):
        kube = FakeKube()
        await RangeLifecycle(kube).create(ns_placement())
        assert [p[0] for p in kube.policies] == ["pg-alpha-jones"]


class TestTheDeployPlanIsUnchanged:
    """Everything a deploy applied before this lane still gets applied, in the same order.

    The floor and the RoleBinding are additions to a path that works today on pg-devtest; the
    thing that must not have moved is what a range is made of.
    """

    async def test_create_still_makes_a_namespace_and_one_policy(self):
        kube = FakeKube()
        await RangeLifecycle(kube).create(ns_placement())
        assert "pg-alpha-jones" in kube.namespaces
        assert len(kube.policies) == 1
        assert not any(c[0] == "apply_custom_object" for c in kube.calls)

    async def test_a_vcluster_range_still_installs_its_chart(self):
        kube = FakeKube()
        await RangeLifecycle(kube).create(vc_placement())
        plurals = [c[1] for c in kube.calls if c[0] == "apply_custom_object"]
        assert plurals == ["helmrepositories", "helmreleases"]

    async def test_the_ingress_half_of_the_floor_is_untouched(self):
        policy = default_deny_policy("pg-alpha-jones", control_plane_namespaces=("pg-system",))
        assert policy["spec"]["ingress"] == [
            {
                "from": [
                    {"podSelector": {}},
                    {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "pg-system"}
                        }
                    },
                ]
            }
        ]


# The image these pods run has no USER line, so every uid the chart asks for has to be one the
# chart supplies. `runAsNonRoot` alone reads the image, finds root, and refuses to start the
# container -- a hardening flag that turns into an outage, and one the renderer cannot see
# because the manifest it produces is perfectly valid YAML.
POD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}


def pod_specs(docs):
    """(name, podSpec) for every workload the chart renders, whatever wraps it."""
    for doc in docs:
        if doc.get("kind") not in POD_KINDS:
            continue
        spec = doc["spec"]
        if doc["kind"] == "CronJob":
            spec = spec["jobTemplate"]["spec"]
        yield doc["metadata"]["name"], spec["template"]["spec"]


def effective_security_context(pod_spec, container) -> dict:
    """What the kubelet decides with: the container's own fields over the pod's, per field.

    A container inherits `runAsUser` from the pod and may override it, and the same for
    `runAsNonRoot`, so neither half of the pair can be judged at one level alone.
    """
    return {**(pod_spec.get("securityContext") or {}), **(container.get("securityContext") or {})}


class TestNoWorkloadAsksForANonRootUserItCannotName:
    """0.53.0: `pg-gateway` declared runAsNonRoot with no runAsUser.

    The kubelet rejected the container with "container has runAsNonRoot and image will run as
    root", the pod never left CreateContainerConfigError, and the Helm upgrade failed on a
    five-minute rollout timeout -- after the rest of the platform had already rolled to the new
    version, so the install was half-applied with no rollback target.

    This is a property of the pair, not of either half, which is why nothing else caught it:
    both fields render, the manifest validates, and `helm template` is satisfied.
    """

    @needs_helm
    def test_every_container_that_refuses_root_names_the_uid_to_use_instead(self):
        offenders = [
            f"{name}/{container['name']}"
            for name, spec in pod_specs(render())
            for container in spec.get("containers", []) + spec.get("initContainers", [])
            if (ctx := effective_security_context(spec, container)).get("runAsNonRoot")
            and ctx.get("runAsUser") is None
        ]
        assert offenders == [], (
            "these containers refuse to run as root but name no uid, and the image has no USER, "
            f"so the kubelet will refuse to start them: {offenders}"
        )

    @needs_helm
    def test_the_gateway_is_the_one_that_regressed_and_is_covered(self):
        spec = dict(pod_specs(render()))["pg-gateway"]
        ctx = effective_security_context(spec, spec["containers"][0])
        assert ctx["runAsNonRoot"] is True
        assert ctx["runAsUser"] == 65534
