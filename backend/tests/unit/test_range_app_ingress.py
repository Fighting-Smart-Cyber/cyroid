"""A range's applications live on a host of their own -- SEC-001.

The defect this covers: an application published on the platform's own host is same-origin with
the console, so the software the learner is being trained against can read the operator's session
out of the browser. The fix is a host per range, and an install with no host for its applications
publishes none of them. These tests pin both halves -- what the Ingress says when there is a host,
and that there is nothing at all when there is not -- and the handoff that gets a cookie onto an
origin the platform cannot write to directly.
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

APPS_HOST = "apps.example.invalid"


def web_config():
    config = v2_config()
    config["capabilities"][0]["web"] = {"service": "podinfo", "port": 9898}
    return config


@pytest.fixture
def cluster(monkeypatch):
    """An install with an ingress class but no applications host -- the default."""
    settings = get_settings()
    # The fixture blueprint installs a chart from this repository, and an install only installs
    # charts from repositories it trusts.
    monkeypatch.setattr(settings, "chart_repository", "https://stefanprodan.github.io/podinfo")
    monkeypatch.setattr(settings, "range_ingress_class", "traefik")
    monkeypatch.setattr(settings, "range_ingress_path_prefix", "")
    monkeypatch.setattr(settings, "range_apps_host", "")
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


@pytest.fixture
def hosted(cluster, monkeypatch):
    """...and the same install once it has been given one."""
    monkeypatch.setattr(get_settings(), "range_apps_host", APPS_HOST)
    return cluster


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
def rng(db_session, cluster):
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


def forwarded(host, uri, cookie=None, proto="https"):
    headers = [
        (b"x-forwarded-host", host.encode()),
        (b"x-forwarded-uri", uri.encode()),
        (b"x-forwarded-proto", proto.encode()),
    ]
    if cookie:
        headers.append((b"cookie", f"{apps.APP_COOKIE_NAME}={cookie}".encode()))
    return Request(
        {"type": "http", "method": "GET", "path": "/", "headers": headers, "query_string": b""}
    )


def code_in(url):
    return url.partition(f"?{apps.APP_CODE_QUERY}=")[2]


class TestTheIngressIsOnItsOwnHost:
    async def test_the_rule_carries_the_ranges_host_and_the_application_is_the_path(
        self, db_session, rng, hosted
    ):
        result = await svc.deploy_range_on_kubernetes(db_session, rng.id)
        ingress = hosted.ingresses[(result["namespace"], "pg-app-podinfo")]
        rule = ingress["spec"]["rules"][0]
        assert rule["host"] == f"{rng.id}.{APPS_HOST}"
        assert rule["http"]["paths"][0]["path"] == "/podinfo"
        assert rule["http"]["paths"][0]["pathType"] == "Prefix"
        # A class NO controller serves. The object declares that the application is published;
        # pg-gateway reads it, and the chart's single wildcard Ingress is what answers.
        assert ingress["spec"]["ingressClassName"] == "pg-gateway"

    async def test_the_url_it_reports_is_absolute_on_that_host(self, db_session, rng, hosted):
        result = await svc.deploy_range_on_kubernetes(db_session, rng.id)
        assert result["apps"] == {"podinfo": f"https://{rng.id}.{APPS_HOST}/podinfo/"}

    async def test_it_is_one_plain_object_with_no_controller_specific_anything(
        self, db_session, rng, hosted
    ):
        """This was three objects, two of them traefik.io/v1alpha1 Middlewares.

        Authorisation and prefix-stripping both moved into pg-gateway because no other ingress
        controller can express them -- Application Gateway, the AWS Load Balancer Controller,
        GKE's ingress and OpenShift's router have no external-authorisation middleware at all.
        What survives has to be an object any of them would accept, which means no annotations.
        """
        result = await svc.deploy_range_on_kubernetes(db_session, rng.id)
        ns = result["namespace"]

        assert not [key for key in hosted.objects if key[0] == "middlewares"]

        ingress = hosted.ingresses[(ns, "pg-app-podinfo")]
        assert not ingress["metadata"].get("annotations")

    async def test_it_is_labelled_with_the_range_and_the_application(self, db_session, rng, hosted):
        """So the gateway reads labels rather than parsing the host and path back apart -- and
        so a malformed host cannot move an application into another range's table."""
        result = await svc.deploy_range_on_kubernetes(db_session, rng.id)
        labels = hosted.ingresses[(result["namespace"], "pg-app-podinfo")]["metadata"]["labels"]
        assert labels["pg.app/name"] == "podinfo"
        assert labels["pg.range/id"] == str(rng.id)


class TestWithoutAHostNothingIsPublished:
    async def test_the_deploy_creates_no_ingress_at_all(self, db_session, rng, cluster):
        result = await svc.deploy_range_on_kubernetes(db_session, rng.id)
        assert result["apps"] == {}
        assert not cluster.ingresses
        assert not [k for k in cluster.objects if k[0] == "middlewares"]

    def test_the_ticket_refuses_and_names_the_setting(self, db_session, rng):
        with pytest.raises(HTTPException) as exc:
            apps.issue_app_ticket(rng.id, "podinfo", db_session, rng.learner, Response())
        assert exc.value.status_code == 409 and "RANGE_APPS_HOST" in exc.value.detail

    def test_the_listing_says_why_rather_than_offering_a_dead_link(self, db_session, rng):
        rng.status = RangeStatus.RUNNING
        listed = apps.apps_of(db_session, rng)
        assert listed[0]["name"] == "podinfo"
        assert listed[0]["url"] is None and listed[0]["published"] is False
        assert "RANGE_APPS_HOST" in listed[0]["reason"]

    def test_the_forwardauth_refuses_everything(self, db_session, rng):
        with pytest.raises(HTTPException) as exc:
            apps.authorize_app_request(forwarded(f"{rng.id}.{APPS_HOST}", "/podinfo/"), db_session)
        assert exc.value.status_code == 403

    def test_a_host_that_is_not_a_dns_name_counts_as_no_host(self, db_session, rng, monkeypatch):
        # The obvious operator mistake is a URL. Left alone it reaches an Ingress rule as
        # `<range-id>.https://apps.example.invalid`, which the cluster refuses -- failing the
        # whole range's deploy in a worker's log rather than saying so where the setting is.
        monkeypatch.setattr(get_settings(), "range_apps_host", "https://apps.example.invalid")
        assert svc.apps_host() == ""
        rng.status = RangeStatus.RUNNING
        listed = apps.apps_of(db_session, rng)
        assert listed[0]["url"] is None and "RANGE_APPS_HOST" in listed[0]["reason"]


class TestTheTicket:
    def test_an_entitled_user_gets_an_absolute_url_carrying_a_code(self, db_session, rng, hosted):
        body = apps.issue_app_ticket(rng.id, "podinfo", db_session, rng.learner, Response())
        assert body["url"].startswith(f"https://{rng.id}.{APPS_HOST}/podinfo/?")
        assert apps.verify_app_code(code_in(body["url"]), rng.id) == rng.learner.id

    def test_the_session_itself_never_appears_in_the_url(self, db_session, rng, hosted):
        # The long-lived credential is the cookie the exchange sets, not anything in a query
        # string that an access log or the browser's history keeps.
        body = apps.issue_app_ticket(rng.id, "podinfo", db_session, rng.learner, Response())
        assert apps.verify_app_ticket(code_in(body["url"]), rng.id) is None

    def test_a_stranger_is_refused(self, db_session, rng, hosted):
        with pytest.raises(HTTPException) as exc:
            apps.issue_app_ticket(rng.id, "podinfo", db_session, user(db_session), Response())
        assert exc.value.status_code == 403

    def test_an_unknown_app_is_404(self, db_session, rng, hosted):
        with pytest.raises(HTTPException) as exc:
            apps.issue_app_ticket(rng.id, "nope", db_session, rng.owner, Response())
        assert exc.value.status_code == 404

    def test_the_listing_reports_the_running_range_as_available(self, db_session, rng, hosted):
        rng.status = RangeStatus.RUNNING
        assert apps.apps_of(db_session, rng) == [
            {
                "name": "podinfo",
                "url": f"https://{rng.id}.{APPS_HOST}/podinfo/",
                "published": True,
                "reason": None,
            }
        ]


class TestTheHandoff:
    def _code(self, db_session, rng):
        return code_in(
            apps.issue_app_ticket(rng.id, "podinfo", db_session, rng.learner, Response())["url"]
        )

    def test_a_code_is_traded_for_a_cookie_on_the_applications_host(self, db_session, rng, hosted):
        code = self._code(db_session, rng)
        host = f"{rng.id}.{APPS_HOST}"
        response = apps.authorize_app_request(
            forwarded(host, f"/podinfo/?{apps.APP_CODE_QUERY}={code}"), db_session
        )
        assert response.status_code == 302
        assert response.headers["location"] == f"https://{host}/podinfo/"
        cookie = response.headers["set-cookie"]
        assert apps.APP_COOKIE_NAME in cookie and "HttpOnly" in cookie and "Path=/" in cookie
        # Host-only: one range's ticket must never be offered to another range's application.
        assert "Domain=" not in cookie

    def test_the_redirect_goes_back_to_the_port_the_browser_asked_on(self, db_session, rng, hosted):
        # The port is not part of the range's name, but it is part of where the browser is: an
        # install reached on anything else would be sent to the default port, which answers
        # nothing.
        code = self._code(db_session, rng)
        host = f"{rng.id}.{APPS_HOST}"
        response = apps.authorize_app_request(
            forwarded(f"{host}:8443", f"/podinfo/?{apps.APP_CODE_QUERY}={code}"), db_session
        )
        assert response.headers["location"] == f"https://{host}:8443/podinfo/"

    def test_the_cookie_it_sets_then_passes(self, db_session, rng, hosted):
        code = self._code(db_session, rng)
        host = f"{rng.id}.{APPS_HOST}"
        response = apps.authorize_app_request(
            forwarded(host, f"/podinfo/?{apps.APP_CODE_QUERY}={code}"), db_session
        )
        ticket = response.headers["set-cookie"].split("=", 1)[1].split(";")[0]
        assert apps.authorize_app_request(forwarded(host, "/podinfo/", ticket), db_session) == {
            "ok": True
        }

    def test_a_code_is_spent_once(self, db_session, rng, hosted):
        code = self._code(db_session, rng)
        host = f"{rng.id}.{APPS_HOST}"
        uri = f"/podinfo/?{apps.APP_CODE_QUERY}={code}"
        apps.authorize_app_request(forwarded(host, uri), db_session)
        with pytest.raises(HTTPException) as exc:
            apps.authorize_app_request(forwarded(host, uri), db_session)
        assert exc.value.status_code == 403

    def test_a_code_for_one_range_does_not_open_another(self, db_session, rng, hosted):
        code = self._code(db_session, rng)
        other = uuid.uuid4()
        with pytest.raises(HTTPException) as exc:
            apps.authorize_app_request(
                forwarded(f"{other}.{APPS_HOST}", f"/podinfo/?{apps.APP_CODE_QUERY}={code}"),
                db_session,
            )
        assert exc.value.status_code == 403

    def test_a_ticket_for_one_range_does_not_open_another(self, db_session, rng, hosted):
        ticket = apps.create_app_ticket(rng.id, rng.learner.id)
        with pytest.raises(HTTPException) as exc:
            apps.authorize_app_request(
                forwarded(f"{uuid.uuid4()}.{APPS_HOST}", "/podinfo/", ticket), db_session
            )
        assert exc.value.status_code == 403

    def test_a_console_ticket_is_not_an_app_ticket(self, db_session, rng, hosted):
        from proving_ground.utils.security import create_console_ticket

        ticket = create_console_ticket(rng.id, rng.learner.id)
        with pytest.raises(HTTPException):
            apps.authorize_app_request(
                forwarded(f"{rng.id}.{APPS_HOST}", "/podinfo/", ticket), db_session
            )

    def test_no_credential_at_all_is_refused(self, db_session, rng, hosted):
        with pytest.raises(HTTPException) as exc:
            apps.authorize_app_request(forwarded(f"{rng.id}.{APPS_HOST}", "/podinfo/"), db_session)
        assert exc.value.status_code == 403


class TestTheHostIsTheWholeRoutingDecision:
    @pytest.mark.parametrize(
        "host",
        [
            "",
            APPS_HOST,
            f"not-a-uuid.{APPS_HOST}",
            f"{uuid.uuid4()}.elsewhere.invalid",
            f"a.{uuid.uuid4()}.{APPS_HOST}",
            f"{uuid.uuid4()}{APPS_HOST}",
        ],
    )
    def test_an_unexpected_host_fails_closed(self, db_session, rng, hosted, host):
        ticket = apps.create_app_ticket(rng.id, rng.learner.id)
        with pytest.raises(HTTPException) as exc:
            apps.authorize_app_request(forwarded(host, "/podinfo/", ticket), db_session)
        assert exc.value.status_code == 403

    def test_a_port_on_the_host_header_is_not_part_of_the_name(self, db_session, rng, hosted):
        ticket = apps.create_app_ticket(rng.id, rng.learner.id)
        request = forwarded(f"{rng.id}.{APPS_HOST}:8443", "/podinfo/", ticket)
        assert apps.authorize_app_request(request, db_session) == {"ok": True}

    def test_the_key_round_trips_and_only_in_its_canonical_spelling(self):
        range_id = uuid.uuid4()
        assert svc.range_id_from_key(svc.range_key(range_id)) == range_id
        assert svc.range_id_from_key(str(range_id).replace("-", "")) is None
        assert svc.range_id_from_key(f"{{{range_id}}}") is None
        assert svc.range_id_from_key("pg-range") is None
