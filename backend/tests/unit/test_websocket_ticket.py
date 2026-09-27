"""The session never travels in a websocket URL.

SEC-035. `new WebSocket(url)` takes a URL and nothing else, so every `/ws/`
route read the caller's session JWT out of `?token=`. A URL is not a header: it
is written to the browser's history, to the access log of every proxy and
ingress between the browser and the API, and into a Referer if the page links
out. Anyone reading one of those back held a full API bearer token until it
expired -- the very problem the VNC console ticket was built to close, reopened
with a worse credential.

The fix is the console ticket's own shape: an authenticated request exchanges
its session for a short-lived ticket naming ONE socket, delivered as a cookie
scoped to that socket's path. The handshake sends the cookie by itself, so
nothing secret is in the URL at all.

These tests go after the three things that make that true: the ticket is bound
to one socket, an access token is not a ticket (and a ticket is not an access
token), and the routes take the cookie and nothing else.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from proving_ground.api import websocket as ws
from proving_ground.api.deps import get_current_user
from proving_ground.config import get_settings
from proving_ground.database import get_db
from proving_ground.models.network import Network
from proving_ground.models.range import Range, RangeStatus, RangeVisibility
from proving_ground.models.user import User, UserAttribute, UserRole
from proving_ground.models.vm import VM
from proving_ground.utils.security import (
    WS_TICKET_COOKIE,
    create_access_token,
    create_ws_ticket,
    decode_access_token,
    verify_ws_ticket,
)


def make_user(db, *roles: str) -> User:
    """A user whose ABAC role attributes are set, because `is_admin` reads
    those and not the deprecated `role` column."""
    tag = uuid.uuid4().hex[:8]
    user = User(
        username=f"u-{tag}",
        email=f"u-{tag}@x.invalid",
        hashed_password="x",
        role=UserRole.ADMIN if "admin" in roles else UserRole.STUDENT,
        is_active=True,
        is_approved=True,
    )
    db.add(user)
    db.flush()
    for role in roles:
        db.add(UserAttribute(user_id=user.id, attribute_type="role", attribute_value=role))
    db.flush()
    return user


@pytest.fixture
def world(db_session):
    """One owner with two private ranges and a machine in each."""
    owner = make_user(db_session, "engineer")
    stranger = make_user(db_session, "engineer")

    ranges = []
    vms = []
    for name in ("a", "b"):
        rng = Range(
            name=name,
            status=RangeStatus.DRAFT,
            visibility=RangeVisibility.PRIVATE,
            created_by=owner.id,
        )
        db_session.add(rng)
        db_session.flush()
        network = Network(range_id=rng.id, name="lan", subnet="10.0.0.0/24", gateway="10.0.0.1")
        db_session.add(network)
        db_session.flush()
        vm = VM(
            range_id=rng.id,
            network_id=network.id,
            hostname=f"h-{name}",
            ip_address="10.0.0.10",
            cpu=1,
            ram_mb=1024,
            disk_gb=10,
            container_id="deadbeef",
        )
        db_session.add(vm)
        ranges.append(rng)
        vms.append(vm)
    db_session.commit()

    return {
        "owner": owner,
        "stranger": stranger,
        "range_a": ranges[0],
        "range_b": ranges[1],
        "vm_a": vms[0],
        "vm_b": vms[1],
    }


@pytest.fixture
def client(db_session, monkeypatch, world):
    """The websocket router alone, signed in as the ranges' owner.

    The cookie is minted over plain http here, so the Secure attribute has to
    be off or httpx would drop it on the floor and every test would pass for
    the wrong reason.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("CONSOLE_COOKIE_SECURE", "false")

    app = FastAPI()
    app.include_router(ws.router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: world["owner"]
    # The websocket routes call get_db() directly rather than through Depends,
    # and they close the session they are handed.
    db_session.close = lambda: None
    monkeypatch.setattr(ws, "get_db", lambda: iter([db_session]))
    yield TestClient(app)
    get_settings.cache_clear()


def mint(client, kind: str, resource_id=None):
    body = {"kind": kind, "resource_id": str(resource_id) if resource_id else None}
    return client.post("/ws/ticket", json=body)


def refusal_code(client, url: str) -> int:
    """Open the socket and report the code the server closed it with."""
    with client.websocket_connect(url) as socket:
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_json()
    return exc.value.code


class TestTheTicketIsBoundToOneSocket:
    def test_a_valid_ticket_returns_its_user(self):
        user, vm = uuid.uuid4(), uuid.uuid4()
        assert verify_ws_ticket(create_ws_ticket(user, "console", str(vm)), "console", str(vm)) == (
            user
        )

    def test_a_ticket_for_one_resource_does_not_open_another(self):
        """Otherwise any console you may open is every console you may open."""
        ticket = create_ws_ticket(uuid.uuid4(), "console", str(uuid.uuid4()))
        assert verify_ws_ticket(ticket, "console", str(uuid.uuid4())) is None

    def test_a_ticket_for_one_route_does_not_open_another(self):
        """A read of a range's status is not a shell on the host that runs it."""
        range_id = str(uuid.uuid4())
        ticket = create_ws_ticket(uuid.uuid4(), "status", range_id)
        assert verify_ws_ticket(ticket, "range-console", range_id) is None

    def test_an_expired_ticket_is_refused(self):
        ticket = create_ws_ticket(uuid.uuid4(), "events", minutes=-1)
        assert verify_ws_ticket(ticket, "events") is None

    def test_a_tampered_ticket_is_refused(self):
        ticket = create_ws_ticket(uuid.uuid4(), "events")
        assert verify_ws_ticket(ticket[:-4] + "AAAA", "events") is None

    def test_rubbish_is_refused_rather_than_raising(self):
        """The verifier runs on unauthenticated input; it must not 500."""
        for junk in (None, "", "x", "a.b.c", "not a token"):
            assert verify_ws_ticket(junk, "events") is None


class TestATicketAndAnAccessTokenAreNotInterchangeable:
    """Both are signed with the same key, so only `typ` keeps them apart."""

    def test_an_access_token_is_not_a_ws_ticket(self):
        assert verify_ws_ticket(create_access_token(uuid.uuid4()), "events") is None

    def test_a_ws_ticket_is_not_an_access_token(self):
        """A ticket reaches the browser as a cookie and will be presented to
        this API. If it decoded as a session it would be a full bearer token
        for its whole life, which is the mistake the console ticket already
        made once."""
        assert decode_access_token(create_ws_ticket(uuid.uuid4(), "events")) is None


class TestTheRoutesTakeTheCookieAndNothingElse:
    def test_an_access_token_in_the_url_is_refused(self, client, world):
        """The defect itself: the session used to be the credential here."""
        token = create_access_token(str(world["owner"].id))
        url = f"/ws/console/{world['vm_a'].id}?token={token}"
        assert refusal_code(client, url) == ws.WS_UNAUTHENTICATED

    def test_an_access_token_in_the_cookie_is_refused(self, client, world):
        """Moving the same token out of the URL does not make it a ticket."""
        client.cookies.set(WS_TICKET_COOKIE, create_access_token(str(world["owner"].id)))
        url = f"/ws/console/{world['vm_a'].id}"
        assert refusal_code(client, url) == ws.WS_UNAUTHENTICATED

    def test_no_ticket_at_all_is_refused(self, client, world):
        assert refusal_code(client, "/ws/events") == ws.WS_UNAUTHENTICATED

    def test_the_refusal_says_what_to_do(self, client, world):
        """A closed socket with no reason is indistinguishable from a broken
        one, and this refusal has a specific remedy."""
        with client.websocket_connect("/ws/events") as socket:
            with pytest.raises(WebSocketDisconnect) as exc:
                socket.receive_json()
        assert "ticket" in exc.value.reason.lower()
        assert "/ws/ticket" in exc.value.reason

    def test_an_expired_ticket_is_refused_by_the_route(self, client, world):
        client.cookies.set(
            WS_TICKET_COOKIE, create_ws_ticket(world["owner"].id, "events", None, -1)
        )
        assert refusal_code(client, "/ws/events") == ws.WS_UNAUTHENTICATED

    def test_a_minted_ticket_opens_its_own_socket(self, client, world):
        assert mint(client, "events").status_code == 200
        with client.websocket_connect("/ws/events") as socket:
            assert socket.receive_json()["type"] == "connected"

    def test_a_ticket_for_one_vm_does_not_open_another(self, client, world):
        """The cookie is path-scoped, so a browser would not even offer it --
        but the path is delivery, not the decision, and the route has to refuse
        a ticket that is carried across by hand."""
        assert mint(client, "console", world["vm_a"].id).status_code == 200
        client.cookies.set(
            WS_TICKET_COOKIE, create_ws_ticket(world["owner"].id, "console", str(world["vm_a"].id))
        )
        assert refusal_code(client, f"/ws/console/{world['vm_b'].id}") == ws.WS_UNAUTHENTICATED


class TestTheCookieIsScopedToTheOneSocket:
    def test_it_is_set_on_the_socket_path_and_not_above_it(self, client, world):
        """Scoped to "/ws/console/<id>" the browser offers it to that handshake
        and to nothing else. Scoped to "/" one console ticket would be offered
        to every socket in the API."""
        response = mint(client, "console", world["vm_a"].id)
        cookie = response.headers["set-cookie"]
        assert f"Path=/ws/console/{world['vm_a'].id}" in cookie

    def test_the_path_carries_the_mount_prefix_the_app_uses(self, db_session, monkeypatch, world):
        """The app mounts this router under /api/v1, so the browser dials
        "/api/v1/ws/console/<id>" and a cookie scoped to "/ws/console/<id>"
        would never be offered to it -- every console would fail closed with
        nothing in the logs to say why. Mounted bare, as the tests above do,
        both spellings agree and the mistake is invisible."""
        get_settings.cache_clear()
        monkeypatch.setenv("CONSOLE_COOKIE_SECURE", "false")
        app = FastAPI()
        app.include_router(ws.router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_current_user] = lambda: world["owner"]
        db_session.close = lambda: None
        mounted = TestClient(app)

        response = mounted.post(
            "/api/v1/ws/ticket",
            json={"kind": "console", "resource_id": str(world["vm_a"].id)},
        )
        assert response.status_code == 200
        assert f"Path=/api/v1/ws/console/{world['vm_a'].id}" in response.headers["set-cookie"]

        # And the cookie the browser now holds opens that socket.
        monkeypatch.setattr(ws, "get_db", lambda: iter([db_session]))
        with mounted.websocket_connect(f"/api/v1/ws/console/{world['vm_a'].id}") as socket:
            # The VM has a container id but no docker daemon behind it here, so
            # the socket fails on the exec rather than on the credential. 4001
            # would mean the ticket was not accepted, which is what this asserts.
            with pytest.raises(WebSocketDisconnect) as exc:
                socket.receive_text()
        assert exc.value.code != ws.WS_UNAUTHENTICATED
        get_settings.cache_clear()

    def test_it_is_httponly(self, client, world):
        """A training workload that got script execution on this origin must
        not be able to read a console key back out."""
        assert "HttpOnly" in mint(client, "events").headers["set-cookie"]


class TestMintingAppliesTheSocketsOwnRule:
    def test_a_console_ticket_for_an_unknown_vm_is_not_found(self, client):
        assert mint(client, "console", uuid.uuid4()).status_code == 404

    def test_a_status_ticket_for_an_unknown_range_is_not_found(self, client):
        assert mint(client, "status", uuid.uuid4()).status_code == 404

    def test_the_host_shell_ticket_is_administrator_only(self, client, world):
        """`/ws/range-console` is a privileged exec on the host daemon. The
        socket refuses a non-admin, so minting must too -- otherwise the ticket
        endpoint is a second, softer door onto the same room."""
        assert mint(client, "range-console", world["range_a"].id).status_code == 403

    def test_a_resource_scoped_kind_must_name_its_resource(self, client):
        """Without this a ticket scoped to "console:" alone would verify
        against no VM at all."""
        assert mint(client, "console").status_code == 422

    def test_an_unknown_kind_is_rejected_rather_than_minted(self, client):
        assert mint(client, "everything").status_code == 422

    def test_a_stranger_cannot_mint_for_someone_elses_range(self, db_session, monkeypatch, world):
        """The ticket carries the entitlement, so the check has to happen where
        it is issued as well as where it is spent."""
        get_settings.cache_clear()
        monkeypatch.setenv("CONSOLE_COOKIE_SECURE", "false")
        app = FastAPI()
        app.include_router(ws.router)
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_current_user] = lambda: world["stranger"]
        db_session.close = lambda: None
        stranger_client = TestClient(app)
        assert (
            stranger_client.post(
                "/ws/ticket", json={"kind": "status", "resource_id": str(world["range_a"].id)}
            ).status_code
            == 403
        )
        get_settings.cache_clear()

    def test_the_assigned_learner_is_still_admitted(self, db_session, monkeypatch, world):
        """Every test above describes somebody being refused, and a credential
        change that refused everybody would satisfy all of them. The learner a
        range is assigned to is neither its owner nor an admin and holds no
        role that grants anything on its own, so they are the account a
        stricter door shuts first -- and the console of the machine they are
        being trained on is the last thing that should stop working.

        They are admitted because minting calls the same `check_console_access`
        the HTTP console route calls, rather than a second rule written to
        resemble it.
        """
        get_settings.cache_clear()
        monkeypatch.setenv("CONSOLE_COOKIE_SECURE", "false")
        learner = make_user(db_session, "student")
        world["range_a"].assigned_to_user_id = learner.id
        db_session.commit()

        app = FastAPI()
        app.include_router(ws.router)
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_current_user] = lambda: learner
        db_session.close = lambda: None
        monkeypatch.setattr(ws, "get_db", lambda: iter([db_session]))
        learner_client = TestClient(app)

        response = learner_client.post(
            "/ws/ticket", json={"kind": "console", "resource_id": str(world["vm_a"].id)}
        )
        assert response.status_code == 200
        assert f"Path=/ws/console/{world['vm_a'].id}" in response.headers["set-cookie"]

        # And the cookie opens the socket. Nothing mocks Docker here, so the
        # connection dies reaching the daemon; 4001 or 4003 would mean it died
        # on the credential or on the rule, which is the regression this guards.
        with learner_client.websocket_connect(f"/ws/console/{world['vm_a'].id}") as socket:
            with pytest.raises(WebSocketDisconnect) as exc:
                socket.receive_json()
        assert exc.value.code not in (ws.WS_UNAUTHENTICATED, ws.WS_FORBIDDEN)

        # The machine next door is still not theirs.
        assert (
            learner_client.post(
                "/ws/ticket", json={"kind": "console", "resource_id": str(world["vm_b"].id)}
            ).status_code
            == 403
        )
        get_settings.cache_clear()


class TestTheOtherDoorIsShutToo:
    """A session token in a query string, by any route.

    SEC-035 was written about the websockets, but the same credential was reaching the same
    places through the download endpoints -- and the frontend really was sending it there, so
    this was live rather than theoretical: `blueprintsApi.export` appended
    `?token=<session JWT>` to every blueprint export, on top of the `Authorization` header the
    shared axios instance already set. The parameter bought nothing and cost a full API
    credential in browser history and in every access log on the way.
    """

    def test_the_download_dependency_is_the_ordinary_session_check(self):
        from proving_ground.api import deps

        # `DownloadUser` used to resolve a token out of the query string. It is now the same
        # dependency every other authenticated route uses, so there is no second, weaker way in.
        assert deps.DownloadUser.__metadata__[0].dependency is deps.get_current_user
        assert not hasattr(deps, "get_current_user_from_token_param")

    def test_no_route_takes_a_token_as_a_request_parameter(self):
        """An AST walk, not a grep: `lock_token` on a body model is not this defect.

        The first version of this guard matched any `token: ... = None` in a module and fired on
        `files.py`'s file-lock token, which is a field on a Pydantic model and never goes near a
        URL. What matters is a parameter named `token` on a function FastAPI routes to, because
        that is what becomes a query parameter.
        """
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "proving_ground" / "api"
        offenders = []
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                routed = any(
                    "router" in ast.dump(d) and "websocket" not in ast.dump(d)
                    for d in node.decorator_list
                )
                if not routed:
                    continue
                args = node.args
                for arg in [*args.args, *args.kwonlyargs]:
                    if arg.arg == "token":
                        offenders.append(f"{path.name}:{node.name}")
        assert not offenders, (
            "these routes take a token as a request parameter, which puts a credential in a "
            f"URL: {offenders}. A download that cannot set a header wants a ticket, as the "
            "console and the websockets do."
        )
