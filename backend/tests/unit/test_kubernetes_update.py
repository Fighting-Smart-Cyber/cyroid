"""The in-cluster update: check against the chart registry, advance the HelmRelease, report it.

Driven against the fake cluster and an injected version list; the registry HTTP call is the one
thing not exercised here (it is exercised by clicking Update on pg-devtest).
"""

import json

import pytest
from fastapi import HTTPException

from proving_ground.api import kubernetes_update as upd
from proving_ground.config import get_settings

from .test_kubernetes_runtime import FakeKube

HR = ("helmreleases", "pg-system", "pg")


@pytest.fixture
def env(monkeypatch, tmp_path):
    creds = tmp_path / ".dockerconfigjson"
    creds.write_text(
        json.dumps({"auths": {"registry.example": {"username": "u", "password": "p"}}})
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "chart_repository", "registry.example/g/p/charts/proving-ground")
    monkeypatch.setattr(settings, "registry_credentials_file", str(creds))
    monkeypatch.setattr(settings, "pod_namespace", "pg-system")
    monkeypatch.setattr(settings, "helm_release_name", "pg")
    return settings


@pytest.fixture
def kube(monkeypatch):
    fake = FakeKube()
    fake.objects[HR] = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"name": "pg", "namespace": "pg-system"},
        "spec": {"chart": {"spec": {"chart": "proving-ground", "version": "0.48.0"}}},
    }
    fake.helm_status = {
        "conditions": [{"type": "Ready", "status": "True", "message": "Helm install succeeded"}],
        "history": [{"chartVersion": "0.48.0"}],
    }

    async def connect(*a, **k):
        return fake

    monkeypatch.setattr(upd.KubernetesApiClient, "connect", connect)
    return fake


class TestCheck:
    def test_a_newer_published_chart_is_an_update(self, env):
        r = upd.check("0.48.0", versions=["0.47.0", "0.48.0", "0.49.0"])
        assert r["checked"] and r["update_available"] and r["latest_tag"] == "0.49.0"

    def test_the_newest_is_chosen_numerically(self, env):
        r = upd.check("0.9.0", versions=["0.9.0", "0.10.0"])
        assert r["latest_tag"] == "0.10.0"

    def test_up_to_date_is_reported_as_such(self, env):
        r = upd.check("0.49.0", versions=["0.48.0", "0.49.0"])
        assert r["checked"] and not r["update_available"]

    def test_prereleases_are_not_offered(self, env):
        r = upd.check("0.48.0", versions=["0.48.0", "0.49.0-rc1"])
        assert not r["update_available"]

    def test_no_credentials_is_checked_false_not_up_to_date(self, env, monkeypatch):
        # "I could not look" must never render as "nothing to pull".
        monkeypatch.setattr(env, "registry_credentials_file", "/nonexistent")
        r = upd.check("0.48.0", versions=["0.49.0"])
        assert r["checked"] is False and "pull secret" in r["detail"]

    def test_no_repository_configured_is_checked_false(self, env, monkeypatch):
        monkeypatch.setattr(env, "chart_repository", "")
        assert upd.check("0.48.0", versions=["0.49.0"])["checked"] is False


class TestStart:
    def test_advances_the_helmrelease_to_the_newest_release(self, env, kube, monkeypatch):
        monkeypatch.setattr(upd, "list_chart_versions", lambda r: ["0.48.0", "0.49.0"])
        r = upd.start("0.48.0", "jon")
        assert r["started"] and "0.49.0" in r["message"]
        release = kube.objects[HR]
        assert release["spec"]["chart"]["spec"]["version"] == "0.49.0"
        assert release["metadata"]["annotations"]["pg.update/requested-by"] == "jon"

    def test_already_current_is_a_409(self, env, kube, monkeypatch):
        monkeypatch.setattr(upd, "list_chart_versions", lambda r: ["0.48.0"])
        with pytest.raises(HTTPException) as exc:
            upd.start("0.48.0", "jon")
        assert exc.value.status_code == 409

    def test_no_helmrelease_means_the_install_was_not_adopted(self, env, kube, monkeypatch):
        monkeypatch.setattr(upd, "list_chart_versions", lambda r: ["0.49.0"])
        del kube.objects[HR]
        with pytest.raises(HTTPException) as exc:
            upd.start("0.48.0", "jon")
        assert exc.value.status_code == 503 and "install-k8s.sh" in exc.value.detail

    def test_the_target_is_never_taken_from_the_caller(self):
        # The signature is the guarantee: there is no parameter to carry a ref.
        import inspect

        assert list(inspect.signature(upd.start).parameters) == ["current_version", "started_by"]


class TestStatus:
    def test_deployed_and_ready_is_succeeded(self, env, kube):
        assert upd.status_of()["state"] == "succeeded"

    def test_asked_for_a_version_not_yet_deployed_is_running(self, env, kube):
        kube.objects[HR]["spec"]["chart"]["spec"]["version"] = "0.49.0"
        r = upd.status_of()
        assert r["state"] == "running" and r["job_id"] == "helmrelease/pg@0.49.0"

    def test_ready_false_on_the_asked_for_version_is_failed(self, env, kube):
        kube.objects[HR]["spec"]["chart"]["spec"]["version"] = "0.49.0"
        kube.helm_status = {
            "conditions": [{"type": "Ready", "status": "False", "message": "upgrade failed: x"}],
            "history": [{"chartVersion": "0.48.0"}],
            "lastAttemptedRevision": "0.49.0",
        }
        r = upd.status_of()
        assert r["state"] == "failed" and "upgrade failed" in r["log_tail"]

    def test_no_helmrelease_is_idle(self, env, kube):
        del kube.objects[HR]
        assert upd.status_of()["state"] == "idle"


class TestCredential:
    def test_reports_the_pull_secrets_user(self, env):
        assert upd.credential() == {"configured": True, "readable": True, "username": "u"}

    def test_changing_it_here_is_refused(self):
        with pytest.raises(HTTPException) as exc:
            upd.refuse_credential_change()
        assert exc.value.status_code == 400
