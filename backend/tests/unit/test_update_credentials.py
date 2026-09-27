"""Where the update check is allowed to send the registry pull credentials.

The deploy token in the image pull secret is what pulls every image this install runs. The
update check hands it to whatever URL the registry's `WWW-Authenticate` header names, and that
header comes from the server being talked to -- so the realm is pinned to the registry's own
host (or its domain, or one named in UPDATE_REALM_HOSTS) before anything is sent.

Every test here asserts on what left the process, not only on the return value: a refusal that
raised after the credentials were already on the wire would pass a weaker check.
"""

import json

import httpx
import pytest
from fastapi import HTTPException

from proving_ground.api import kubernetes_update as upd
from proving_ground.config import get_settings

REGISTRY = "registry.example.com"
REPOSITORY = f"{REGISTRY}/g/p/charts/proving-ground"

# Bound once, so a test that replaces the module attribute cannot make Wire call itself.
_list_chart_versions = upd.list_chart_versions


@pytest.fixture(autouse=True)
def no_configured_realms(monkeypatch):
    """The escape hatch is off unless a test turns it on; a developer's shell must not decide."""
    monkeypatch.delenv(upd.REALM_HOSTS_ENV, raising=False)


@pytest.fixture
def env(monkeypatch, tmp_path):
    creds = tmp_path / ".dockerconfigjson"
    creds.write_text(json.dumps({"auths": {REGISTRY: {"username": "u", "password": "deploy-tok"}}}))
    settings = get_settings()
    monkeypatch.setattr(settings, "chart_repository", REPOSITORY)
    monkeypatch.setattr(settings, "registry_credentials_file", str(creds))
    return settings


class Wire:
    """A registry that challenges with `realm`, and a record of every request made to it."""

    def __init__(self, realm: str, repository: str = REPOSITORY):
        self.realm = realm
        self.host = repository.split("/", 1)[0]
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url).rstrip("/") == f"https://{self.host}/v2":
            return httpx.Response(
                401, headers={"www-authenticate": f'Bearer realm="{self.realm}",service="reg"'}
            )
        if request.url.path.endswith("/tags/list"):
            return httpx.Response(200, json={"tags": ["0.51.0", "0.52.0"]})
        return httpx.Response(200, json={"token": "issued-token"})

    @property
    def hosts_given_credentials(self) -> set[str]:
        """Hosts that received something derived from the deploy token."""
        given = set()
        for request in self.requests:
            authorization = request.headers.get("authorization", "")
            if authorization and not authorization.startswith("Bearer issued-token"):
                given.add(request.url.host)
            if "deploy-tok" in str(request.url):
                given.add(request.url.host)
        return given

    def versions(self) -> list[str]:
        return _list_chart_versions(upd.Registry.from_environment(), transport=self.transport)


class TestTheRealmIsPinned:
    def test_a_realm_on_another_host_is_refused(self, env):
        wire = Wire("https://harvester.attacker.test/jwt/auth")
        with pytest.raises(HTTPException) as exc:
            wire.versions()
        assert exc.value.status_code == 502
        # The point of the refusal: nothing was sent, not merely nothing was returned.
        assert wire.hosts_given_credentials == set()
        assert "harvester.attacker.test" not in {r.url.host for r in wire.requests}

    def test_the_refusal_names_the_host_it_expected(self, env):
        wire = Wire("https://harvester.attacker.test/jwt/auth")
        with pytest.raises(HTTPException) as exc:
            wire.versions()
        detail = exc.value.detail
        assert REGISTRY in detail and "harvester.attacker.test" in detail
        assert "example.com" in detail  # the domain it would also have accepted
        assert upd.REALM_HOSTS_ENV in detail  # and what to do if the host is legitimate

    def test_a_realm_on_the_registrys_own_host_is_followed(self, env):
        wire = Wire(f"https://{REGISTRY}/jwt/auth")
        assert wire.versions() == ["0.51.0", "0.52.0"]
        assert wire.hosts_given_credentials == {REGISTRY}

    def test_a_realm_on_the_registrys_own_domain_is_followed(self, env):
        # The deployment this is released from: the registry and the service that issues its
        # tokens are sibling names under one domain. Pinning to the exact host would turn every
        # update check on it into "could not check for updates".
        wire = Wire("https://code.example.com/jwt/auth")
        assert wire.versions() == ["0.51.0", "0.52.0"]
        assert wire.hosts_given_credentials == {"code.example.com"}

    def test_a_plaintext_realm_is_refused(self, env):
        wire = Wire(f"http://{REGISTRY}/jwt/auth")
        with pytest.raises(HTTPException) as exc:
            wire.versions()
        assert "not an https URL" in exc.value.detail
        assert wire.hosts_given_credentials == set()

    def test_a_realm_that_is_not_a_url_is_refused(self, env):
        # What a Basic challenge looks like through the same header parser.
        wire = Wire("Registry Realm")
        with pytest.raises(HTTPException):
            wire.versions()
        assert wire.hosts_given_credentials == set()

    def test_a_configured_host_is_followed(self, env, monkeypatch):
        monkeypatch.setenv(upd.REALM_HOSTS_ENV, "tokens.elsewhere.test, spare.example.net")
        wire = Wire("https://tokens.elsewhere.test/jwt/auth")
        assert wire.versions() == ["0.51.0", "0.52.0"]
        assert wire.hosts_given_credentials == {"tokens.elsewhere.test"}


class TestTheDomainRuleStopsWhereOrganisationsDo:
    def test_a_two_label_registry_has_no_siblings(self):
        # The parent of a two-label host is a suffix, not an organisation: allowing it would
        # allow every host on the internet that ends the same way.
        with pytest.raises(HTTPException) as exc:
            upd.authorized_realm("https://anything.test/token", "ghcr.test")
        assert "ghcr.test" in exc.value.detail
        assert upd.authorized_realm("https://ghcr.test/token", "ghcr.test")

    def test_a_port_on_the_registry_does_not_change_the_host(self):
        assert upd.authorized_realm("https://reg.test/token", "reg.test:5000")

    def test_a_host_that_merely_ends_with_the_domain_is_refused(self):
        with pytest.raises(HTTPException):
            upd.authorized_realm("https://notexample.com/token", "registry.example.com")

    def test_an_address_has_no_domain_to_share(self):
        # 10.0.0.1 read as a name would have the "domain" 0.0.1, which 1.0.0.1 sits beneath.
        with pytest.raises(HTTPException):
            upd.authorized_realm("https://1.0.0.1/token", "10.0.0.1:5000")
        assert upd.authorized_realm("https://10.0.0.1/token", "10.0.0.1:5000")

    def test_an_ipv4_mapped_address_has_no_domain_either(self):
        # It carries the same dotted tail as the name rule reads, so a digits-only test says
        # its "domain" is 0.0.1 and hands the token to anything ending that way.
        with pytest.raises(HTTPException):
            upd.authorized_realm("https://1.0.0.1/token", "[::ffff:10.0.0.1]:5000")

    def test_an_ipv6_registry_can_still_check_for_updates(self):
        # A bracketed authority split on the first colon leaves the expected host as "[", which
        # no realm equals: the refusal would then be permanent, and would name "[" as the
        # registry. The realm's host arrives unbracketed, so the two have to be compared that
        # way.
        assert upd.authorized_realm("https://[::1]/token", "[::1]:5000")
        with pytest.raises(HTTPException) as exc:
            upd.authorized_realm("https://harvester.attacker.test/token", "[::1]:5000")
        assert "::1" in exc.value.detail

    def test_a_realm_that_urlsplit_refuses_to_parse_is_a_named_refusal(self):
        # A bracketed authority that is not an address makes urlsplit raise. Escaping as a
        # ValueError it becomes a 500 on `start` and "could not reach the chart registry" on
        # `check` -- the registry's answer blamed on the network.
        with pytest.raises(HTTPException) as exc:
            upd.authorized_realm("https://[registry.example.com]/token", "registry.example.com")
        assert exc.value.status_code == 502
        assert "does not parse as a URL" in exc.value.detail
        assert REGISTRY in exc.value.detail


class TestTheCheckSaysWhyRatherThanSayingUpToDate:
    def test_a_refused_realm_is_reported_as_not_checked(self, env, monkeypatch):
        # "I would not send the credentials there" must reach the operator as a failed check.
        # Rendered as up-to-date it would hide both the missing update and the hostile registry.
        wire = Wire("https://harvester.attacker.test/jwt/auth")
        monkeypatch.setattr(upd, "list_chart_versions", lambda *_a, **_k: wire.versions())
        result = upd.check("0.52.0")
        assert result["checked"] is False
        assert "harvester.attacker.test" in result["detail"]
        assert result.get("update_available") is None
