"""Which chart repositories a blueprint may name -- SEC-025.

A blueprint is authored over the API by any approved account, and the chart repository it names
is handed to helm-controller, which fetches it and reconciles whatever chart it finds with the
permissions Flux holds. So the repository is a trust decision, and the install makes it, not the
author. These tests hold the two halves of that: the allow-list is applied wherever a
`CapabilitySpec` is built -- not only in the API route, which the catalog installer and the
seeder do not go through -- and the refusal names both the repository and the setting that would
permit it.
"""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from proving_ground import config
from proving_ground.capability.blueprint import read_blueprint
from proving_ground.capability.flux import FluxChartInstaller
from proving_ground.capability.models import Isolation, Placement, Scope
from proving_ground.capability.runtime import CapabilitySpec, ChartRef, HookSpec, ImageRef
from proving_ground.capability.specs import (
    REPOSITORY_SETTING,
    ChartRepositoryPolicy,
    capabilities_from_blueprint_config,
    capability_from_config,
    repository_policy,
)

from .test_kubernetes_runtime import FakeKube

DIGEST = "registry.example/logc2@sha256:" + "e" * 64
MIRROR = "https://charts.internal.example/mirror"


@dataclass
class FakeSettings:
    """Only the two fields the policy reads, so a drift in either is visible here."""

    capability_chart_repositories: str = ""
    chart_repository: str = ""


@pytest.fixture
def settings(monkeypatch):
    """Configure the install the way an operator's settings would."""

    def configure(repositories: str = "", *, platform: str = "") -> None:
        monkeypatch.setattr(
            config,
            "get_settings",
            lambda: FakeSettings(
                capability_chart_repositories=repositories, chart_repository=platform
            ),
        )

    return configure


def entry(repository: str = MIRROR, **kw):
    base = {
        "name": "logc2",
        "version": "1.0.0",
        "scope": "per-learner",
        "chart": {"name": "logc2", "version": "1.4.2", "repository": repository},
        "hooks": {"verify": {"image": DIGEST, "command": ["/bin/verify"]}},
    }
    base.update(kw)
    return base


def blueprint(repository: str = MIRROR, *, schema_version: int = 2):
    return {"schemaVersion": schema_version, "capabilities": [entry(repository)]}


def permitted(*repositories: str) -> ChartRepositoryPolicy:
    return ChartRepositoryPolicy(allowed=repositories)


class TestNothingIsPermittedUntilSomethingIs:
    def test_an_unconfigured_install_permits_no_repository(self, settings):
        # Air-gap is a constraint now, not later (ADR-0007): an install reaches the repositories
        # somebody configured and no others, so the empty list is a refusal, not a pass.
        settings("")
        with pytest.raises(ValueError, match="permits no chart repositories"):
            capability_from_config(entry())

    def test_settings_that_predate_the_setting_still_refuse(self, monkeypatch):
        # An install upgraded from before the setting existed must deny, not raise AttributeError
        # out of every blueprint parse.
        monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(chart_repository=""))
        with pytest.raises(ValueError, match=REPOSITORY_SETTING):
            capability_from_config(entry())

    def test_the_refusal_names_the_repository_and_the_setting(self, settings):
        settings("https://charts.example")
        with pytest.raises(ValueError) as refused:
            capability_from_config(entry("https://charts.evil.test/logc2"))
        message = str(refused.value)
        assert "https://charts.evil.test/logc2" in message
        assert REPOSITORY_SETTING in message
        assert "capability 'logc2'" in message


class TestWhatTheAllowListCovers:
    def test_an_allowed_repository_passes(self):
        spec = capability_from_config(entry(), policy=permitted(MIRROR))
        assert spec.chart.repository == MIRROR

    def test_an_entry_covers_the_charts_under_it(self):
        spec = capability_from_config(
            entry("https://charts.example/logc2/stable"),
            policy=permitted("https://charts.example/logc2"),
        )
        assert spec.chart.repository == "https://charts.example/logc2/stable"

    def test_an_oci_registry_is_a_repository(self):
        spec = capability_from_config(
            entry("oci://registry.example/charts/logc2"),
            policy=permitted("oci://registry.example/charts"),
        )
        assert spec.chart.repository == "oci://registry.example/charts/logc2"

    def test_a_look_alike_host_is_not_under_the_entry(self):
        # `startswith` on the whole URL would read charts.example.evil.test as permitted.
        with pytest.raises(ValueError, match="does not permit"):
            capability_from_config(
                entry("https://charts.example.evil.test/logc2"),
                policy=permitted("https://charts.example"),
            )

    def test_userinfo_does_not_smuggle_a_host_past_the_entry(self):
        # The host of `https://charts.example@evil.test/c` is evil.test; only the text before the
        # `@` looks permitted.
        with pytest.raises(ValueError, match="does not permit"):
            capability_from_config(
                entry("https://charts.example@evil.test/c"),
                policy=permitted("https://charts.example"),
            )

    def test_a_sibling_path_is_not_under_the_entry(self):
        with pytest.raises(ValueError, match="does not permit"):
            capability_from_config(
                entry("https://charts.example/logc2-mirror"),
                policy=permitted("https://charts.example/logc2"),
            )

    def test_a_different_scheme_to_the_same_host_is_not_the_same_repository(self):
        with pytest.raises(ValueError, match="a https:// or oci:// URL"):
            capability_from_config(
                entry("http://charts.example/logc2"), policy=permitted("https://charts.example")
            )

    def test_a_dot_dot_segment_does_not_climb_out_of_the_entry(self):
        # The prefix match reads `/approved/../unapproved` as sitting under `/approved`, while
        # the server answering the fetch resolves it to `/unapproved`. An operator who lists one
        # directory of a shared mirror has to get the directory they listed.
        with pytest.raises(ValueError, match="would not make it one"):
            capability_from_config(
                entry("https://mirror.internal/approved/../unapproved/evil"),
                policy=permitted("https://mirror.internal/approved"),
            )

    def test_a_percent_encoded_dot_dot_is_the_same_climb(self):
        with pytest.raises(ValueError, match="would not make it one"):
            capability_from_config(
                entry("https://mirror.internal/approved/%2e%2e/unapproved/evil"),
                policy=permitted("https://mirror.internal/approved"),
            )

    def test_the_chart_name_cannot_carry_a_path_out_of_the_repository(self):
        # An OCI reference is the repository plus the chart name, so a name holding a path is a
        # second way to choose where the chart comes from, and the allow-list never sees it.
        permitted_entry = entry("oci://registry.example/charts")
        permitted_entry["chart"]["name"] = "../attacker/evil"
        with pytest.raises(ValueError, match="one segment, not a path"):
            capability_from_config(
                permitted_entry, policy=permitted("oci://registry.example/charts")
            )

    def test_a_file_url_is_refused_and_told_the_allow_list_will_not_help(self):
        # source-controller would read its own filesystem; listing it would not make it a
        # repository, so the refusal says so rather than pointing at the setting as a remedy.
        with pytest.raises(ValueError, match="would not make it one"):
            capability_from_config(
                entry("file:///var/charts"), policy=permitted("file:///var/charts")
            )

    def test_a_repository_with_no_host_is_refused(self):
        with pytest.raises(ValueError, match="a https:// or oci:// URL"):
            capability_from_config(entry("charts.example/logc2"), policy=permitted(MIRROR))


class TestItIsEnforcedWhereverSpecsAreBuilt:
    def test_a_blueprints_capability_list_is_checked(self, settings):
        settings("https://charts.example")
        with pytest.raises(ValueError, match="does not permit"):
            capabilities_from_blueprint_config({"capabilities": [entry(MIRROR)]})

    def test_the_second_capability_is_checked_too(self, settings):
        settings(MIRROR)
        with pytest.raises(ValueError, match="does not permit"):
            capabilities_from_blueprint_config(
                {"capabilities": [entry(MIRROR), entry("https://charts.evil.test", name="x")]}
            )

    def test_reading_a_blueprint_refuses_it(self, settings):
        # Every path into a deploy -- the API, the catalog installer, the seeder -- reads the
        # blueprint through here, which is why the check is not in the route.
        settings("https://charts.example")
        with pytest.raises(ValueError, match="does not permit"):
            read_blueprint(blueprint("https://charts.evil.test/logc2"))

    def test_a_legacy_blueprint_is_checked_as_well(self, settings):
        # Capabilities are era-neutral and are read out of a v1 blueprint too, so a v1 config is
        # not a way round the policy.
        settings("https://charts.example")
        with pytest.raises(ValueError, match="does not permit"):
            read_blueprint(blueprint("https://charts.evil.test/logc2", schema_version=1))

    def test_a_permitted_blueprint_still_reads(self, settings):
        settings(f"https://charts.example, {MIRROR}")
        spec = read_blueprint(blueprint())
        assert [c.chart.repository for c in spec.capabilities] == [MIRROR]

    def test_a_blueprint_declaring_no_capabilities_needs_no_policy(self, settings):
        settings("")
        assert read_blueprint({"schemaVersion": 2}).capabilities == ()

    def test_the_blueprints_own_errors_are_reported_before_the_policy(self, settings):
        # A blueprint with several things wrong hears about its own mistakes first; being told
        # about the install's allow-list while the verify hook is missing helps nobody.
        settings("")
        with pytest.raises(ValueError, match="must declare a verify hook"):
            capability_from_config(entry(hooks={}))


class TestThePolicyComesFromTheInstallsSettings:
    def test_the_setting_is_a_comma_separated_list(self, settings):
        settings(f"https://charts.example , {MIRROR} ,")
        assert repository_policy().allowed == ("https://charts.example", MIRROR)

    def test_the_platforms_own_chart_repository_is_permitted_without_restating_it(self, settings):
        # An air-gapped install mirrors every chart into the registry it already pulls the
        # platform's own chart from.
        settings("", platform="oci://registry.example/pg/charts")
        spec = capability_from_config(entry("oci://registry.example/pg/charts/logc2"))
        assert spec.chart.repository == "oci://registry.example/pg/charts/logc2"

    def test_a_scheme_less_platform_repository_is_not_listed_as_permitted(self, settings):
        # What the chart actually ships: `chartRepository` is an OCI path with no scheme,
        # because api/kubernetes_update.py splits a registry host off the front of it. Carrying
        # it into the allow-list permits nothing, and an operator reading "Permitted:
        # registry.../proving-ground" on a host that has set no allow-list at all would go
        # looking for a fault that is not there.
        settings("", platform="registry.example/org/charts/proving-ground")
        assert repository_policy().allowed == ()
        with pytest.raises(ValueError, match="permits no chart repositories"):
            capability_from_config(entry())


class TestTheInstallerRefusesBeforeItTouchesTheCluster:
    def spec(self, repository: str) -> CapabilitySpec:
        return CapabilitySpec(
            name="logc2",
            version="1.0.0",
            chart=ChartRef(name="logc2", version="1.4.2", repository=repository),
            scope=Scope.PER_LEARNER,
            verify=HookSpec(image=ImageRef(DIGEST), command=("/bin/verify",)),
        )

    def placement(self) -> Placement:
        return Placement(isolation=Isolation.NAMESPACE, namespace="pg-alpha-jones", reason="test")

    async def test_a_cleartext_repository_is_refused_with_nothing_applied(self):
        # A spec built in code rather than parsed from a blueprint has not met the allow-list, so
        # the sink checks the one thing that needs no configuration.
        kube = FakeKube()
        with pytest.raises(ValueError, match="only a https:// or oci://"):
            await FluxChartInstaller(kube, timeout_seconds=1).install(
                self.spec("http://charts.example/logc2"), self.placement()
            )
        assert kube.calls == []

    async def test_a_repository_of_the_right_shape_still_installs(self):
        kube = FakeKube()
        result = await FluxChartInstaller(kube, timeout_seconds=1).install(
            self.spec(MIRROR), self.placement()
        )
        assert result.namespace == "pg-alpha-jones"
        assert [c[1] for c in kube.calls if c[0] == "apply_custom_object"] == [
            "helmrepositories",
            "helmreleases",
        ]
