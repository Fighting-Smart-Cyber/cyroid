"""How long a credential lives, and what it is allowed to open.

Three defects with one shape: something that identifies a user was handed to a place that keeps
it -- a cookie in a browser nobody at the proxy can match to a session, a query string in an
ingress log, a listing that answers a question nobody asked.

  * SEC-036  the range application's cookie lived eight hours, unrevocable, because eight hours
             was "a class session". Nothing re-checks it inside that window, so its length IS the
             exposure.
  * SEC-062  GET /ranges/{id}/workloads answered the visibility rule, which says yes to every
             account on the install for a PUBLIC range. What it returns is the range's namespace,
             its machines' internal addresses and its published applications.
  * SEC-035  the KubeVirt console socket took the session JWT in its query string, and a URL is
             written to every proxy log it crosses and kept in the browser's history.

The fixtures come from the two modules that already build a Kubernetes range and a routed
application, so these assertions run against the same objects the endpoints normally see.
"""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException, Response
from jose import jwt
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from proving_ground.api import kubernetes_apps as apps
from proving_ground.api import kubernetes_console as console
from proving_ground.api.deps import check_resource_access
from proving_ground.config import get_settings
from proving_ground.models.range import RangeVisibility
from proving_ground.services import kubernetes_range_service as svc
from proving_ground.utils.security import (
    WS_TICKET_COOKIE,
    create_access_token,
    create_ws_ticket,
)

from .test_kubernetes_apps import app_host, forwarded, rng, routed  # noqa: F401
from .test_kubernetes_console import client, k8s_range, kube, user  # noqa: F401

__all__ = ["app_host", "client", "forwarded", "k8s_range", "kube", "rng", "routed", "user"]


def claims(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])


def seconds_of_life(token: str) -> float:
    return claims(token)["exp"] - datetime.now(timezone.utc).timestamp()


class TestTheApplicationTicketIsMinutesNotHours:
    """SEC-036. The cookie is the credential and nothing revokes it, so its life is the window."""

    def test_the_constant_is_not_a_working_day(self):
        assert apps.APP_TICKET_MINUTES <= 60, (
            "the application cookie cannot be re-checked or revoked while it lives, so an hour "
            "is the outside limit; re-entry is one click from the range"
        )

    def test_a_minted_ticket_expires_inside_it(self):
        life = seconds_of_life(apps.create_app_ticket(uuid.uuid4(), uuid.uuid4()))
        assert 0 < life <= apps.APP_TICKET_MINUTES * 60 + 5

    def test_the_handoff_code_stays_the_thirty_seconds_it_was(self):
        """The code is the half that travels in a URL. Shortening the cookie must not have
        loosened it by accident."""
        assert apps.APP_CODE_SECONDS <= 30
        assert 0 < seconds_of_life(apps.create_app_code(uuid.uuid4(), uuid.uuid4())) <= 35

    def test_an_expired_cookie_is_refused(self, db_session, rng):
        stale = apps.create_app_ticket(rng.id, rng.learner.id, minutes=-1)
        with pytest.raises(HTTPException) as exc:
            apps.authorize_app_request(forwarded(app_host(rng.id), cookie=stale), db_session)
        assert exc.value.status_code == 403

    def test_the_refusal_tells_an_expired_learner_what_to_do(self, db_session, rng):
        """ "No application ticket" reads as a broken application to someone who had one a
        minute ago. The two cases are different problems and say so."""
        stale = apps.create_app_ticket(rng.id, rng.learner.id, minutes=-1)
        with pytest.raises(HTTPException) as expired:
            apps.authorize_app_request(forwarded(app_host(rng.id), cookie=stale), db_session)
        with pytest.raises(HTTPException) as never:
            apps.authorize_app_request(forwarded(app_host(rng.id)), db_session)
        assert "Open it again from its range" in expired.value.detail
        assert expired.value.detail != never.value.detail

    def test_the_issued_url_reports_the_life_it_actually_has(self, db_session, rng):
        body = apps.issue_app_ticket(rng.id, "podinfo", db_session, rng.owner, Response())
        assert body["expires_in"] == apps.APP_TICKET_MINUTES * 60


class TestTheWorkloadListingIsNotAVisibilityQuestion:
    """SEC-062. The map of an exercise answers to the rule for working in it."""

    async def test_a_stranger_the_visibility_rule_admits_is_refused(self, db_session, k8s_range):
        k8s_range.visibility = RangeVisibility.PUBLIC
        stranger = user(db_session)
        db_session.commit()

        # The rule the endpoint used to carry says yes: a PUBLIC range is visible install-wide,
        # and that is the right answer to "may this appear in my list of ranges".
        assert (
            check_resource_access("range", k8s_range.id, stranger, db_session, k8s_range.created_by)
            is True
        )

        # It is the wrong answer to "hand me the namespace, the internal addresses and the
        # applications", which is what this endpoint returns.
        with pytest.raises(HTTPException) as exc:
            await console.list_workloads(k8s_range.id, db_session, stranger)
        assert exc.value.status_code == 403
        assert "assign" in exc.value.detail

    async def test_the_refusal_names_the_way_in(self, db_session, k8s_range):
        k8s_range.visibility = RangeVisibility.PUBLIC
        stranger = user(db_session)
        db_session.commit()
        with pytest.raises(HTTPException) as exc:
            await console.list_workloads(k8s_range.id, db_session, stranger)
        assert "training event" in exc.value.detail

    async def test_the_assigned_learner_still_gets_it(self, db_session, k8s_range):
        listed = await console.list_workloads(k8s_range.id, db_session, k8s_range.learner)
        assert [w["name"] for w in listed["workloads"]] == ["web"]

    async def test_the_owner_still_gets_it(self, db_session, k8s_range):
        listed = await console.list_workloads(k8s_range.id, db_session, k8s_range.owner)
        assert listed["namespace"] and [w["name"] for w in listed["workloads"]] == ["web"]

    async def test_a_range_that_does_not_exist_is_still_a_404(self, db_session, k8s_range):
        with pytest.raises(HTTPException) as exc:
            await console.list_workloads(uuid.uuid4(), db_session, k8s_range.owner)
        assert exc.value.status_code == 404


class TestTheDashboardTileCountsOnlyWhatTheCallerMaySee:
    """The same module's other read. Era A summed the caller's own ranges; the cluster-wide
    count replaced that with an inventory fact about the whole install."""

    async def test_a_stranger_is_told_nothing_about_someone_elses_range(
        self, db_session, k8s_range, kube
    ):
        await svc.deploy_range_on_kubernetes(db_session, k8s_range.id)
        stranger = user(db_session)
        db_session.commit()
        assert await console.machine_summary(db_session, stranger) == {
            "substrate": "kubernetes",
            "running": 0,
            "total": 0,
        }

    async def test_the_owner_still_counts_their_own(self, db_session, k8s_range, kube):
        await svc.deploy_range_on_kubernetes(db_session, k8s_range.id)
        assert await console.machine_summary(db_session, k8s_range.owner) == {
            "substrate": "kubernetes",
            "running": 1,
            "total": 1,
        }
        # Still one cluster call however many ranges there are.
        assert len([c for c in kube.calls if c[0] == "list_cluster_custom_objects"]) == 1


def hold(client, ticket: str) -> None:
    """Put a ws ticket in the test browser, as `Set-Cookie` would."""
    client.cookies.set(WS_TICKET_COOKIE, ticket)


def ticket_for(range_id, machine: str, user_id) -> str:
    """What `POST .../console-ticket` mints, without going through the route."""
    return create_ws_ticket(user_id, console.WS_TICKET_KIND, f"{range_id}/{machine}")


class TestTheConsoleSocketTakesATicketNotTheSession:
    """SEC-035. Nothing in the URL, and what is in the cookie opens one framebuffer."""

    def socket(self, range_obj, machine="web"):
        return console.workload_socket_path(range_obj.id, machine)

    def refusal(self, client, url):
        with client.websocket_connect(url) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_bytes()
            return exc.value.code

    def test_the_session_token_in_the_query_string_no_longer_opens_it(
        self, client, db_session, k8s_range, kube
    ):
        """The defect itself: the session JWT opened the socket, and the URL carrying it is in
        every proxy log between the browser and the API, and in the browser's history."""
        db_session.close = lambda: None
        token = create_access_token(str(k8s_range.owner.id))
        assert self.refusal(client, f"{self.socket(k8s_range)}?token={token}") == 4001
        assert not kube.vnc_opened

    def test_an_access_token_in_the_cookie_is_refused_too(
        self, client, db_session, k8s_range, kube
    ):
        """`typ` is what separates them. Same key, same signature, different purpose."""
        db_session.close = lambda: None
        hold(client, create_access_token(str(k8s_range.owner.id)))
        assert self.refusal(client, self.socket(k8s_range)) == 4001
        assert not kube.vnc_opened

    def test_no_ticket_at_all_is_refused(self, client, db_session, k8s_range, kube):
        db_session.close = lambda: None
        assert self.refusal(client, self.socket(k8s_range)) == 4001
        assert not kube.vnc_opened

    def test_a_ticket_for_this_machine_opens_it(self, client, db_session, k8s_range, kube):
        db_session.close = lambda: None
        kube.vnc_frames = [b"RFB 003.008\n"]
        hold(client, ticket_for(k8s_range.id, "web", k8s_range.owner.id))
        with client.websocket_connect(self.socket(k8s_range)) as ws:
            assert ws.receive_bytes() == b"RFB 003.008\n"
        assert kube.vnc_opened

    def test_a_ticket_for_another_machine_does_not_open_this_one(
        self, client, db_session, k8s_range, kube
    ):
        db_session.close = lambda: None
        hold(client, ticket_for(k8s_range.id, "db", k8s_range.owner.id))
        assert self.refusal(client, self.socket(k8s_range)) == 4001
        assert not kube.vnc_opened

    def test_a_ticket_for_another_range_is_refused(self, client, db_session, k8s_range, kube):
        db_session.close = lambda: None
        hold(client, ticket_for(uuid.uuid4(), "web", k8s_range.owner.id))
        assert self.refusal(client, self.socket(k8s_range)) == 4001
        assert not kube.vnc_opened

    def test_an_era_a_console_ticket_is_not_one_of_these(self, client, db_session, k8s_range, kube):
        """The Era A sockets mint from the same helper with their own kind, so the scope string
        is what keeps a VM's console ticket out of a KubeVirt machine's socket."""
        db_session.close = lambda: None
        hold(client, create_ws_ticket(k8s_range.owner.id, "vnc", str(k8s_range.id)))
        assert self.refusal(client, self.socket(k8s_range)) == 4001
        assert not kube.vnc_opened

    def test_an_expired_ticket_is_refused(self, client, db_session, k8s_range, kube):
        db_session.close = lambda: None
        hold(
            client,
            create_ws_ticket(
                k8s_range.owner.id,
                console.WS_TICKET_KIND,
                f"{k8s_range.id}/web",
                minutes=-1,
            ),
        )
        assert self.refusal(client, self.socket(k8s_range)) == 4001
        assert not kube.vnc_opened

    def test_entitlement_is_asked_again_when_the_ticket_is_spent(
        self, client, db_session, k8s_range, kube
    ):
        """Five minutes is short, but an instructor can revoke an assignment inside one. The
        ticket proves who is asking; it does not stand in for whether they may still open this."""
        db_session.close = lambda: None
        hold(client, ticket_for(k8s_range.id, "web", k8s_range.learner.id))
        k8s_range.assigned_to_user_id = None
        db_session.commit()
        assert self.refusal(client, self.socket(k8s_range)) == 4003
        assert not kube.vnc_opened

    def test_a_deactivated_account_loses_the_console_it_had_a_ticket_for(
        self, client, db_session, k8s_range, kube
    ):
        db_session.close = lambda: None
        hold(client, ticket_for(k8s_range.id, "web", k8s_range.learner.id))
        k8s_range.learner.is_active = False
        db_session.commit()
        assert self.refusal(client, self.socket(k8s_range)) == 4001
        assert not kube.vnc_opened

    def test_a_hidden_machine_is_still_refused_with_a_valid_ticket(
        self, client, db_session, k8s_range, kube
    ):
        db_session.close = lambda: None
        k8s_range.hidden_vm_ids = ["web"]
        db_session.commit()
        hold(client, ticket_for(k8s_range.id, "web", k8s_range.learner.id))
        assert self.refusal(client, self.socket(k8s_range)) == 4003
        assert not kube.vnc_opened


def minting_request(path: str) -> Request:
    return Request(
        {"type": "http", "method": "POST", "path": path, "headers": [], "query_string": b""}
    )


class TestMintingAConsoleTicket:
    """Where the entitlement is actually decided, and how what comes out reaches the browser."""

    PREFIXED = "/api/v1/ranges/{}/workloads/{}/console-ticket"

    def mint(self, db, range_obj, account, machine="web", prefix="/api/v1"):
        response = Response()
        body = console.issue_workload_console_ticket(
            range_obj.id,
            machine,
            minting_request(f"{prefix}/ranges/{range_obj.id}/workloads/{machine}/console-ticket"),
            response,
            db,
            account,
        )
        return body, response

    def cookie(self, response: Response) -> str:
        return response.headers["set-cookie"]

    def test_the_ticket_lives_minutes_not_a_session(self, db_session, k8s_range):
        body, response = self.mint(db_session, k8s_range, k8s_range.learner)
        assert body["expires_in"] <= 600
        value = self.cookie(response).split("=", 1)[1].split(";")[0]
        assert 0 < seconds_of_life(value) <= body["expires_in"] + 5

    def test_it_is_scoped_to_one_machine_of_one_range(self, db_session, k8s_range):
        _, response = self.mint(db_session, k8s_range, k8s_range.learner)
        payload = claims(self.cookie(response).split("=", 1)[1].split(";")[0])
        assert payload["ws"] == f"{console.WS_TICKET_KIND}:{k8s_range.id}/web"
        assert payload["typ"] == "ws"
        assert payload["sub"] == str(k8s_range.learner.id)

    def test_nothing_secret_is_in_the_body(self, db_session, k8s_range):
        """The cookie is the credential. A ticket in the body would be one the client could be
        tempted to put back in the URL, which is the thing this replaced."""
        body, _ = self.mint(db_session, k8s_range, k8s_range.learner)
        assert set(body) == {"path", "expires_in"}

    def test_the_cookie_is_scoped_to_the_socket_the_client_is_told_to_dial(
        self, db_session, k8s_range
    ):
        """A cookie scoped to the wrong path is never sent, and the socket then refuses a ticket
        the browser is holding -- which looks exactly like a broken console."""
        body, response = self.mint(db_session, k8s_range, k8s_range.learner)
        assert body["path"] == f"/api/v1/ws/vnc/k8s/{k8s_range.id}/web"
        assert f"Path={body['path']}" in self.cookie(response)

    def test_a_bare_mount_is_scoped_without_the_prefix(self, db_session, k8s_range):
        body, response = self.mint(db_session, k8s_range, k8s_range.learner, prefix="")
        assert body["path"] == f"/ws/vnc/k8s/{k8s_range.id}/web"
        assert f"Path={body['path']}" in self.cookie(response)

    def test_the_cookie_is_not_readable_by_a_page_on_this_origin(self, db_session, k8s_range):
        _, response = self.mint(db_session, k8s_range, k8s_range.learner)
        assert "HttpOnly" in self.cookie(response)

    def test_a_stranger_is_refused(self, db_session, k8s_range):
        k8s_range.visibility = RangeVisibility.PUBLIC
        stranger = user(db_session)
        db_session.commit()
        with pytest.raises(HTTPException) as exc:
            self.mint(db_session, k8s_range, stranger)
        assert exc.value.status_code == 403

    def test_a_machine_this_range_does_not_declare_is_a_404(self, db_session, k8s_range):
        with pytest.raises(HTTPException) as exc:
            self.mint(db_session, k8s_range, k8s_range.owner, machine="nope")
        assert exc.value.status_code == 404

    def test_a_hidden_machine_is_refused_before_a_ticket_exists(self, db_session, k8s_range):
        k8s_range.hidden_vm_ids = ["web"]
        db_session.commit()
        with pytest.raises(HTTPException) as exc:
            self.mint(db_session, k8s_range, k8s_range.learner)
        assert exc.value.status_code == 403
        assert "not available to you" in exc.value.detail

    def test_the_response_is_not_cached_anywhere(self, db_session, k8s_range):
        _, response = self.mint(db_session, k8s_range, k8s_range.learner)
        assert response.headers["Cache-Control"] == "no-store"


class TestTheTicketIsNotAnAccessToken:
    """The other direction of the same separation: `typ` keeps the credentials apart both ways."""

    def test_the_api_refuses_a_console_ticket_as_a_bearer_token(self, db_session, k8s_range):
        from proving_ground.utils.security import decode_access_token

        assert decode_access_token(ticket_for(k8s_range.id, "web", k8s_range.owner.id)) is None

    def test_the_socket_refuses_an_application_ticket(self, client, db_session, k8s_range, kube):
        db_session.close = lambda: None
        hold(client, apps.create_app_ticket(k8s_range.id, k8s_range.owner.id))
        with client.websocket_connect(console.workload_socket_path(k8s_range.id, "web")) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_bytes()
            assert exc.value.code == 4001
        assert not kube.vnc_opened
