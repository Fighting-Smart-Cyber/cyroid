"""pg-gateway: refuse before proxying, and hand the application nothing of the platform's.

This replaces two Traefik CRDs that cannot be expressed on Azure Application Gateway, the AWS
Load Balancer Controller, GKE's ingress or OpenShift's router. What has to survive the move is
every refusal the ForwardAuth middleware made, plus the prefix strip -- and what improves is
that the platform's ticket cookie stops being forwarded to the training workload.
"""

import uuid

import httpx
import pytest
from starlette.testclient import TestClient

from proving_ground.gateway.app import GatewayConfig, create_app
from proving_ground.gateway.routing import Route
from proving_ground.utils import app_tokens

APPS_HOST = "apps.example.com"
PREFIX = "/pg/apps"


class Table:
    def __init__(self, routes):
        self._routes = routes

    def lookup(self, range_id, app):
        return self._routes.get((range_id, app))


class Upstream:
    """Stands in for the capability's Service, and records what it was handed.

    A real ASGI app rather than httpx.MockTransport: the gateway reads the upstream body with
    `aiter_raw`, which is what a proxy must use -- it forwards still-encoded bytes, so a
    `Content-Encoding` header it passes on stays truthful. MockTransport cannot serve that.
    """

    def __init__(self):
        self.seen = []

    def asgi(self):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route as _R

        async def endpoint(request):
            self.seen.append(request)
            return PlainTextResponse("from the application")

        return Starlette(
            routes=[_R("/{p:path}", endpoint, methods=["GET", "POST", "PUT", "DELETE"])]
        )

    def transport(self):
        return httpx.ASGITransport(app=self.asgi())


@pytest.fixture
def rid():
    return uuid.uuid4()


@pytest.fixture
def upstream():
    return Upstream()


@pytest.fixture
def client(rid, upstream):
    route = Route(namespace="pg-range-x", service="podinfo", port=9898)
    app = create_app(
        GatewayConfig(apps_host=APPS_HOST, path_prefix=PREFIX, secure_cookie=True),
        Table({(rid, "podinfo"): route}),
        httpx.AsyncClient(transport=upstream.transport()),
    )
    return TestClient(app, base_url=f"https://{rid}.{APPS_HOST}")


def ticket(rid, user=None):
    return app_tokens.create_app_ticket(rid, user or uuid.uuid4())


class TestItRefusesBeforeItProxies:
    def test_no_credential_is_refused(self, client, upstream):
        r = client.get(f"{PREFIX}/podinfo/", follow_redirects=False)
        assert r.status_code == 401
        assert upstream.seen == [], "nothing may reach the application before authorisation"

    def test_a_ticket_for_another_range_is_refused(self, client, rid, upstream):
        client.cookies.set(app_tokens.APP_COOKIE_NAME, ticket(uuid.uuid4()))
        r = client.get(f"{PREFIX}/podinfo/", follow_redirects=False)
        assert r.status_code == 401
        assert upstream.seen == []

    def test_a_host_that_names_no_range_is_refused(self, rid, upstream):
        app = create_app(
            GatewayConfig(apps_host=APPS_HOST, path_prefix=PREFIX),
            Table({}),
            httpx.AsyncClient(transport=upstream.transport()),
        )
        c = TestClient(app, base_url=f"https://nope.{APPS_HOST}")
        assert c.get(f"{PREFIX}/podinfo/", follow_redirects=False).status_code == 404
        assert upstream.seen == []

    def test_an_unpublished_application_is_refused(self, client, upstream):
        client.cookies.set(app_tokens.APP_COOKIE_NAME, ticket(uuid.uuid4()))
        r = client.get(f"{PREFIX}/not-published/", follow_redirects=False)
        assert r.status_code in (401, 404)
        assert upstream.seen == []

    def test_a_path_outside_the_prefix_is_refused(self, client, upstream):
        r = client.get("/elsewhere/podinfo/", follow_redirects=False)
        assert r.status_code == 404
        assert upstream.seen == []


class TestTheOneUseHandoff:
    def test_a_code_is_exchanged_for_a_cookie_and_redirected_without_it(self, client, rid):
        code = app_tokens.create_app_code(rid, uuid.uuid4())
        r = client.get(
            f"{PREFIX}/podinfo/page?{app_tokens.APP_CODE_QUERY}={code}&keep=1",
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert app_tokens.APP_CODE_QUERY not in r.headers["location"]
        assert "keep=1" in r.headers["location"], "other query parameters must survive"
        assert str(rid) in r.headers["location"], "must return to the range's own host"
        assert app_tokens.APP_COOKIE_NAME in r.headers["set-cookie"]
        assert "HttpOnly" in r.headers["set-cookie"]
        assert "Domain" not in r.headers["set-cookie"], "host-only, or range A's ticket reaches B"

    def test_a_replayed_code_is_refused(self, client, rid, upstream):
        code = app_tokens.create_app_code(rid, uuid.uuid4())
        first = client.get(
            f"{PREFIX}/podinfo/?{app_tokens.APP_CODE_QUERY}={code}", follow_redirects=False
        )
        assert first.status_code == 302
        second = client.get(
            f"{PREFIX}/podinfo/?{app_tokens.APP_CODE_QUERY}={code}", follow_redirects=False
        )
        assert second.status_code == 403
        assert upstream.seen == []

    def test_a_code_for_another_range_is_refused(self, client, rid):
        code = app_tokens.create_app_code(uuid.uuid4(), uuid.uuid4())
        r = client.get(
            f"{PREFIX}/podinfo/?{app_tokens.APP_CODE_QUERY}={code}", follow_redirects=False
        )
        assert r.status_code == 403


class TestWhatTheApplicationActuallyReceives:
    def test_the_prefix_is_stripped(self, client, rid, upstream):
        client.cookies.set(app_tokens.APP_COOKIE_NAME, ticket(rid))
        assert client.get(f"{PREFIX}/podinfo/a/b?q=1").status_code == 200
        assert upstream.seen[0].url.path == "/a/b"
        assert upstream.seen[0].url.query == "q=1"

    def test_the_application_root_is_a_slash(self, client, rid, upstream):
        client.cookies.set(app_tokens.APP_COOKIE_NAME, ticket(rid))
        client.get(f"{PREFIX}/podinfo")
        assert upstream.seen[0].url.path == "/"

    def test_it_reaches_the_service_in_the_ranges_namespace(self, client, rid, upstream):
        client.cookies.set(app_tokens.APP_COOKIE_NAME, ticket(rid))
        client.get(f"{PREFIX}/podinfo/")
        assert upstream.seen[0].headers["host"] == "podinfo.pg-range-x.svc.cluster.local:9898"

    def test_the_platform_cookie_is_not_forwarded(self, client, rid, upstream):
        """HttpOnly stops the app's JavaScript reading it. It does not stop its SERVER."""
        client.cookies.set(app_tokens.APP_COOKIE_NAME, ticket(rid))
        client.cookies.set("app_own_cookie", "keep-me")
        client.get(f"{PREFIX}/podinfo/")

        forwarded = upstream.seen[0].headers.get("cookie", "")
        assert app_tokens.APP_COOKIE_NAME not in forwarded
        assert "keep-me" in forwarded, "the application's own cookies must still reach it"

    def test_the_response_body_comes_back(self, client, rid):
        client.cookies.set(app_tokens.APP_COOKIE_NAME, ticket(rid))
        assert client.get(f"{PREFIX}/podinfo/").text == "from the application"


class TestAnUpstreamThatIsNotAnswering:
    def test_it_is_reported_rather_than_crashing(self, rid):
        def refuse(request):
            raise httpx.ConnectError("connection refused")

        app = create_app(
            GatewayConfig(apps_host=APPS_HOST, path_prefix=PREFIX),
            Table({(rid, "podinfo"): Route("pg-range-x", "podinfo", 9898)}),
            httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
        )
        c = TestClient(app, base_url=f"https://{rid}.{APPS_HOST}")
        c.cookies.set(app_tokens.APP_COOKIE_NAME, ticket(rid))
        assert c.get(f"{PREFIX}/podinfo/").status_code == 502
