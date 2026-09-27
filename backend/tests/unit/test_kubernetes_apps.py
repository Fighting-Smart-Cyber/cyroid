"""A range's applications in the browser -- PG-62: the handoff, the ForwardAuth target, and the
listing. The ingress objects themselves are covered in test_range_lifecycle, and the host split
and its failure modes in test_range_app_ingress.

Everything here turns on one fact: the application answers on a host of its own, so the platform
cannot set a cookie for it. The URL the platform hands out carries a one-use code instead, and
the ForwardAuth target on the far side is what turns that into the cookie.
"""

import uuid

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from proving_ground.api import kubernetes_apps as apps
from proving_ground.api import kubernetes_console as console
from proving_ground.api import kubernetes_ranges
from proving_ground.config import get_settings
from proving_ground.models.blueprint import RangeBlueprint, RangeInstance
from proving_ground.models.range import Range, RangeStatus
from proving_ground.models.user import User, UserRole
from proving_ground.services import kubernetes_range_service as svc

from .test_kubernetes_range_service import v2_config
from .test_kubernetes_runtime import FakeKube


def web_config():
    config = v2_config()
    config["capabilities"][0]["web"] = {"service": "podinfo", "port": 9898}
    return config


APPS_HOST = "apps.example.invalid"


@pytest.fixture
def routed(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "range_ingress_class", "traefik")
    monkeypatch.setattr(settings, "range_apps_host", APPS_HOST)
    monkeypatch.setattr(settings, "range_ingress_path_prefix", "")
    monkeypatch.setattr(
        settings, "range_ingress_authz_url", "http://pg-api:8000/api/v1/range-apps/authz"
    )
    fake = FakeKube()

    async def connect(*a, **k):
        return fake

    monkeypatch.setattr(svc.KubernetesApiClient, "connect", connect)
    monkeypatch.setattr(console.KubernetesApiClient, "connect", connect)
    monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: True)
    monkeypatch.setattr(console, "is_kubernetes", lambda: True)
    return fake


def user(db, role=UserRole.STUDENT):
    tag = uuid.uuid4().hex[:8]
    u = User(
        username=f"u-{tag}",
        email=f"u-{tag}@x.invalid",
        hashed_password="x",
        role=role,
        is_active=True,
        is_approved=True,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def rng(db_session, routed):
    owner, learner = user(db_session, UserRole.ADMIN), user(db_session)
    bp = RangeBlueprint(name="bp", version=1, config=web_config(), created_by=owner.id)
    r = Range(
        name="r", status=RangeStatus.DRAFT, created_by=owner.id, assigned_to_user_id=learner.id
    )
    db_session.add_all([bp, r])
    db_session.flush()
    db_session.add(
        RangeInstance(
            name="i",
            blueprint_id=bp.id,
            blueprint_version=1,
            subnet_offset=0,
            instructor_id=owner.id,
            range_id=r.id,
        )
    )
    db_session.commit()
    r.owner, r.learner = owner, learner
    return r


def forwarded(host, uri="/podinfo/", cookie=None):
    """The request Traefik's ForwardAuth makes: the host names the range, the path does not."""
    headers = [(b"x-forwarded-host", host.encode()), (b"x-forwarded-uri", uri.encode())]
    if cookie:
        headers.append((b"cookie", f"{apps.APP_COOKIE_NAME}={cookie}".encode()))
    return Request(
        {"type": "http", "method": "GET", "path": "/", "headers": headers, "query_string": b""}
    )


def app_host(range_id):
    return f"{range_id}.{APPS_HOST}"


class TestDeployRoutesTheApp:
    async def test_the_route_lands_in_the_range_namespace_and_nothing_else_does(
        self, db_session, rng, routed
    ):
        """It used to be an Ingress plus two traefik.io Middlewares. The middlewares are gone --
        authorisation and prefix-stripping are pg-gateway's now -- so a range namespace carries
        one plain object per application and no CRD only one controller defines."""
        result = await svc.deploy_range_on_kubernetes(db_session, rng.id)
        assert result["apps"] == {"podinfo": f"https://{app_host(rng.id)}/podinfo/"}
        ns = result["namespace"]
        assert (ns, "pg-app-podinfo") in routed.ingresses
        assert not [key for key in routed.objects if key[0] == "middlewares"]

    async def test_without_an_ingress_class_nothing_is_routed_and_deploy_still_succeeds(
        self, db_session, rng, routed, monkeypatch
    ):
        monkeypatch.setattr(get_settings(), "range_ingress_class", "")
        result = await svc.deploy_range_on_kubernetes(db_session, rng.id)
        assert result["apps"] == {} and not routed.ingresses

    async def test_without_an_applications_host_nothing_is_routed_and_deploy_still_succeeds(
        self, db_session, rng, routed, monkeypatch
    ):
        # The range still comes up; it simply has no application, because publishing one here
        # would put it on the console's own origin.
        monkeypatch.setattr(get_settings(), "range_apps_host", "")
        result = await svc.deploy_range_on_kubernetes(db_session, rng.id)
        assert result["apps"] == {} and not routed.ingresses


class TestTicket:
    def test_an_entitled_user_gets_a_url_on_the_ranges_own_host(self, db_session, rng):
        resp = Response()
        body = apps.issue_app_ticket(rng.id, "podinfo", db_session, rng.learner, resp)
        assert body["url"].startswith(f"https://{app_host(rng.id)}/podinfo/?")
        assert f"{apps.APP_CODE_QUERY}=" in body["url"]
        # No cookie here, and this is the point rather than an omission: this origin cannot set
        # one for the application's. The response must also not be cached -- the body is a
        # credential.
        assert "set-cookie" not in resp.headers
        assert resp.headers["Cache-Control"] == "no-store"

    def test_a_stranger_is_refused(self, db_session, rng):
        with pytest.raises(HTTPException) as exc:
            apps.issue_app_ticket(rng.id, "podinfo", db_session, user(db_session), Response())
        assert exc.value.status_code == 403

    def test_an_unknown_app_is_404(self, db_session, rng):
        with pytest.raises(HTTPException) as exc:
            apps.issue_app_ticket(rng.id, "nope", db_session, rng.owner, Response())
        assert exc.value.status_code == 404


class TestForwardAuth:
    def test_a_valid_ticket_for_this_range_passes(self, db_session, rng):
        ticket = apps.create_app_ticket(rng.id, rng.learner.id)
        assert apps.authorize_app_request(
            forwarded(app_host(rng.id), cookie=ticket), db_session
        ) == {"ok": True}

    def test_a_ticket_for_another_range_is_refused(self, db_session, rng):
        ticket = apps.create_app_ticket(uuid.uuid4(), rng.learner.id)
        with pytest.raises(HTTPException) as exc:
            apps.authorize_app_request(forwarded(app_host(rng.id), cookie=ticket), db_session)
        assert exc.value.status_code == 403

    def test_a_console_ticket_is_not_an_app_ticket(self, db_session, rng):
        from proving_ground.utils.security import create_console_ticket

        ticket = create_console_ticket(rng.id, rng.learner.id)
        with pytest.raises(HTTPException):
            apps.authorize_app_request(forwarded(app_host(rng.id), cookie=ticket), db_session)

    def test_no_cookie_and_no_code_is_refused(self, db_session, rng):
        with pytest.raises(HTTPException) as exc:
            apps.authorize_app_request(forwarded(app_host(rng.id)), db_session)
        assert exc.value.status_code == 403

    def test_a_host_that_names_no_range_is_refused(self, db_session, rng):
        # The host is now the whole of the routing decision, so it fails closed on anything
        # that is not exactly one range-key label beneath the configured suffix.
        ticket = apps.create_app_ticket(rng.id, rng.learner.id)
        for host in (APPS_HOST, f"not-a-uuid.{APPS_HOST}", f"{rng.id}.evil.invalid"):
            with pytest.raises(HTTPException):
                apps.authorize_app_request(forwarded(host, cookie=ticket), db_session)

    def test_a_code_is_exchanged_for_a_cookie_on_the_applications_host(self, db_session, rng):
        code = apps.create_app_code(rng.id, rng.learner.id)
        uri = f"/podinfo/?{apps.APP_CODE_QUERY}={code}"
        resp = apps.authorize_app_request(forwarded(app_host(rng.id), uri=uri), db_session)
        assert resp.status_code == 302
        assert resp.headers["Location"] == f"https://{app_host(rng.id)}/podinfo/"
        cookie = resp.headers["set-cookie"]
        assert apps.APP_COOKIE_NAME in cookie and "HttpOnly" in cookie
        # Host-only: no Domain, so one range's ticket is never offered to another's application.
        assert "Domain=" not in cookie

    def test_a_code_is_spent_once(self, db_session, rng):
        code = apps.create_app_code(rng.id, rng.learner.id)
        uri = f"/podinfo/?{apps.APP_CODE_QUERY}={code}"
        apps.authorize_app_request(forwarded(app_host(rng.id), uri=uri), db_session)
        with pytest.raises(HTTPException) as exc:
            apps.authorize_app_request(forwarded(app_host(rng.id), uri=uri), db_session)
        assert exc.value.status_code == 403


class TestListing:
    async def test_workloads_include_the_apps(self, db_session, rng):
        r = await console.workloads_of(db_session, rng)
        assert r["apps"] == [
            {
                "name": "podinfo",
                "url": f"https://{app_host(rng.id)}/podinfo/",
                "published": False,
                "reason": "Available once the range is running.",
            }
        ]
        rng.status = RangeStatus.RUNNING
        running = (await console.workloads_of(db_session, rng))["apps"][0]
        assert running["published"] is True and running["reason"] is None
