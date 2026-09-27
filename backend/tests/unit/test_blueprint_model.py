"""Versioned blueprint reading -- COSMOS PG-122.

The criteria that matter most here are the compatibility ones: a catalog blueprint written before
any of this existed must still parse, and a blueprint from a newer engine must be refused rather
than half-read.
"""

import pytest

from proving_ground.capability.blueprint import (
    SCHEMA_VERSION_K8S,
    SCHEMA_VERSION_LEGACY,
    read_blueprint,
    scopes_of,
)
from proving_ground.capability.models import Scope
from proving_ground.capability.workload import OSFamily

DIGEST = "registry.example/win@sha256:" + "a" * 64

# What a blueprint written before any of this existed looks like: no schemaVersion, DinD shape.
LEGACY = {
    "networks": [{"name": "lan", "subnet": "10.0.1.0/24", "gateway": "10.0.1.1"}],
    "vms": [{"hostname": "dc01", "windows_version": "2022", "cpu": 4, "ram_mb": 8192}],
    "msel": {"injects": []},
    "router": {"type": "vyos"},
}

K8S = {
    "schemaVersion": 2,
    "networks": [
        {"name": "dmz", "subnet": "172.30.10.0/24", "gateway": "172.30.10.1"},
        {"name": "internal", "subnet": "172.30.20.0/24"},
    ],
    "workloads": [
        {
            "name": "dc01",
            "os": {"family": "windows", "version": "2022"},
            "cpus": 4,
            "memoryMb": 8192,
            "bootImage": DIGEST,
            "disks": [{"name": "root", "sizeGb": 80, "boot": True}],
            "interfaces": [
                {"network": "dmz", "ip": "172.30.10.5", "primary": True},
                {"network": "internal", "ip": "172.30.20.5"},
            ],
        }
    ],
    "capabilities": [
        {
            "name": "logc2",
            "version": "1.0.0",
            "scope": "per-learner",
            "chart": {"name": "logc2", "version": "1.4.2", "repository": "https://charts.example"},
            "hooks": {"verify": {"image": DIGEST, "command": ["/bin/verify"]}},
        }
    ],
}


class TestOldBlueprintsKeepWorking:
    def test_a_blueprint_with_no_version_is_v1_by_definition(self):
        # Not sniffed from shape: a v2 blueprint that happens to omit workloads would otherwise
        # be silently read as Era A.
        assert read_blueprint(LEGACY).schema_version == SCHEMA_VERSION_LEGACY

    def test_a_legacy_blueprint_parses_rather_than_raising(self):
        spec = read_blueprint(LEGACY)
        assert spec.is_legacy

    def test_a_legacy_blueprint_is_not_half_translated(self):
        # A half-translation that looks like a k8s range is worse than an honest refusal.
        spec = read_blueprint(LEGACY)
        assert spec.networks == () and spec.workloads == ()

    def test_a_legacy_blueprint_says_it_is_not_k8s_deployable(self):
        # "no workloads" and "Era A blueprint" look identical from outside and mean very
        # different things.
        assert read_blueprint(LEGACY).deployable_on_kubernetes is False

    def test_the_original_config_is_kept_verbatim(self):
        assert read_blueprint(LEGACY).raw["router"] == {"type": "vyos"}

    def test_an_empty_or_missing_config_is_valid(self):
        assert read_blueprint({}).is_legacy
        assert read_blueprint(None).is_legacy

    def test_capabilities_are_read_even_from_a_legacy_blueprint(self):
        # A capability is era-neutral; it does not care which substrate runs it.
        legacy_with_cap = {**LEGACY, "capabilities": K8S["capabilities"]}
        assert len(read_blueprint(legacy_with_cap).capabilities) == 1


class TestTheRealCatalogShape:
    """Guards criterion 3 against the shape a real catalog blueprint actually has.

    Verified 2026-09-14 against `IL2 VPN Kiosk` as stored on pg-ec2 -- top-level keys exactly
    `msel`, `networks`, `router`, `vms`, and no `schemaVersion`. The values below are generic
    because CYROID is public and catalog content is not ours to publish; the *structure* is what
    this test exists to pin, and that is copied faithfully.
    """

    CATALOG_SHAPE = {
        "networks": [{"name": "kiosk-net", "subnet": "10.10.0.0/24", "gateway": "10.10.0.1"}],
        "vms": [{"hostname": "kiosk", "linux_distro": "ubuntu", "cpu": 2, "ram_mb": 4096}],
        "msel": {"injects": []},
        "router": {"type": "vyos"},
    }

    def test_it_parses(self):
        spec = read_blueprint(self.CATALOG_SHAPE)
        assert spec.schema_version == SCHEMA_VERSION_LEGACY

    def test_it_is_reported_as_era_a_rather_than_empty(self):
        spec = read_blueprint(self.CATALOG_SHAPE)
        assert spec.is_legacy and not spec.deployable_on_kubernetes

    def test_nothing_in_it_is_discarded(self):
        # The catalog resolves against this repository; dropping a key it relies on would break
        # the integration this repository exists to validate.
        spec = read_blueprint(self.CATALOG_SHAPE)
        assert sorted(spec.raw) == ["msel", "networks", "router", "vms"]


class TestK8sBlueprints:
    def test_networks_workloads_and_capabilities_all_parse(self):
        spec = read_blueprint(K8S)
        assert spec.schema_version == SCHEMA_VERSION_K8S
        assert [n.name for n in spec.networks] == ["dmz", "internal"]
        assert [w.name for w in spec.workloads] == ["dc01"]
        assert [c.name for c in spec.capabilities] == ["logc2"]

    def test_it_reports_itself_deployable(self):
        assert read_blueprint(K8S).deployable_on_kubernetes is True

    def test_workload_os_collapses_to_family_and_version(self):
        w = read_blueprint(K8S).workloads[0]
        assert w.os_family is OSFamily.WINDOWS and w.os_version == "2022"

    def test_interfaces_carry_addresses_and_a_primary(self):
        w = read_blueprint(K8S).workloads[0]
        assert w.primary_interface.network == "dmz"
        assert [i.ip_address for i in w.interfaces] == ["172.30.10.5", "172.30.20.5"]

    def test_scopes_are_exposed_for_placement(self):
        assert scopes_of(read_blueprint(K8S)) == frozenset({Scope.PER_LEARNER})


class TestRefusals:
    def test_a_future_version_is_refused_not_best_efforted(self):
        # Ignoring fields a newer blueprint declares would deploy a range missing whatever
        # they said.
        with pytest.raises(ValueError, match="not supported"):
            read_blueprint({"schemaVersion": 99})

    def test_a_non_integer_version_is_refused(self):
        with pytest.raises(ValueError, match="must be an integer"):
            read_blueprint({"schemaVersion": "2"})

    def test_duplicate_network_names_are_refused(self):
        # An interface naming a duplicated network is ambiguous, and the winner is whichever
        # entry happened to be read last.
        config = {
            "schemaVersion": 2,
            "networks": [
                {"name": "dmz", "subnet": "172.30.10.0/24"},
                {"name": "dmz", "subnet": "172.30.99.0/24"},
            ],
        }
        with pytest.raises(ValueError, match="duplicate network names: dmz"):
            read_blueprint(config)

    def test_a_missing_workload_field_names_the_workload(self):
        bad = {**K8S, "workloads": [{**K8S["workloads"][0]}]}
        del bad["workloads"][0]["cpus"]
        with pytest.raises(ValueError, match="workload 'dc01'.*'cpus'"):
            read_blueprint(bad)

    def test_an_unknown_os_family_lists_the_allowed_ones(self):
        bad = {
            **K8S,
            "workloads": [{**K8S["workloads"][0], "os": {"family": "plan9", "version": "4"}}],
        }
        with pytest.raises(ValueError, match="windows, linux, macos"):
            read_blueprint(bad)

    def test_networks_must_be_a_list(self):
        with pytest.raises(ValueError, match="'networks' must be a list"):
            read_blueprint({"schemaVersion": 2, "networks": {"name": "dmz"}})

    def test_a_cluster_overlapping_subnet_is_still_refused_here(self):
        # NetworkSpec enforces it; this asserts the blueprint layer does not bypass that.
        bad = {"schemaVersion": 2, "networks": [{"name": "clash", "subnet": "10.42.0.0/16"}]}
        with pytest.raises(ValueError, match="overlaps the cluster"):
            read_blueprint(bad)
