"""The websocket routes authorise, and the fan-out honours the answer.

SEC-021. Four sockets read the caller's JWT and then handed over whatever the
URL named. Reading a token establishes who is asking; it never established
whether they may. On Era A that made any VM id an interactive root shell. On
both substrates it made every range's live event stream readable by every
account, because the notification feed was mounted for every logged-in user.

Two halves, and the second is the one that is easy to miss: authorising the
subscription is worth nothing while the connection manager delivers the global
channel to every connected socket regardless of what it subscribed to. The
leak was in the fan-out, so the tests go after both.
"""

import json
import uuid
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from proving_ground.api import websocket as ws
from proving_ground.database import get_db
from proving_ground.models.event import EventParticipant, TrainingEvent
from proving_ground.models.network import Network
from proving_ground.models.range import Range, RangeStatus, RangeVisibility
from proving_ground.models.user import User, UserAttribute, UserRole
from proving_ground.models.vm import VM
from proving_ground.services import event_broadcaster as eb
from proving_ground.services.event_broadcaster import (
    EVENTS_CHANNEL,
    RANGE_CHANNEL_PREFIX,
    ConnectionManager,
)
from proving_ground.utils.security import WS_TICKET_COOKIE, create_ws_ticket


def make_user(db, *roles: str, is_active: bool = True) -> User:
    """A user whose ABAC role attributes are set, because `is_admin` and
    `is_student_only` read those and not the deprecated `role` column."""
    tag = uuid.uuid4().hex[:8]
    user = User(
        username=f"u-{tag}",
        email=f"u-{tag}@x.invalid",
        hashed_password="x",
        role=UserRole.ADMIN if "admin" in roles else UserRole.STUDENT,
        is_active=is_active,
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
    """Two private ranges with different owners, and a learner assigned to one.

    Private is the default a range is created with, so this is the ordinary
    case rather than a hardened one.
    """
    owner = make_user(db_session, "engineer")
    other_owner = make_user(db_session, "engineer")
    admin = make_user(db_session, "admin")
    learner = make_user(db_session, "student")
    stranger = make_user(db_session, "engineer")

    range_a = Range(
        name="a",
        status=RangeStatus.DRAFT,
        visibility=RangeVisibility.PRIVATE,
        created_by=owner.id,
        assigned_to_user_id=learner.id,
    )
    range_b = Range(
        name="b",
        status=RangeStatus.DRAFT,
        visibility=RangeVisibility.PRIVATE,
        created_by=other_owner.id,
    )
    db_session.add_all([range_a, range_b])
    db_session.flush()

    network = Network(range_id=range_a.id, name="lan", subnet="10.0.0.0/24", gateway="10.0.0.1")
    db_session.add(network)
    db_session.flush()
    vm = VM(
        range_id=range_a.id,
        network_id=network.id,
        hostname="web",
        ip_address="10.0.0.10",
        cpu=1,
        ram_mb=1024,
        disk_gb=10,
        container_id="deadbeef",
    )
    db_session.add(vm)
    db_session.commit()

    return {
        "owner": owner,
        "admin": admin,
        "learner": learner,
        "stranger": stranger,
        "range_a": range_a,
        "range_b": range_b,
        "vm": vm,
    }


@pytest.fixture
def client(db_session, monkeypatch):
    app = FastAPI()
    app.include_router(ws.router)
    app.dependency_overrides[get_db] = lambda: db_session
    # The websocket routes call get_db() directly rather than through Depends,
    # and they close the session they are handed.
    db_session.close = lambda: None
    monkeypatch.setattr(ws, "get_db", lambda: iter([db_session]))
    return TestClient(app)


def refusal_code(client, url: str) -> int:
    """Open the socket and report the code the server closed it with."""
    with client.websocket_connect(url) as socket:
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_json()
    return exc.value.code


def as_user(client, user: User, kind: str, resource_id=None) -> None:
    """Give the client the ticket cookie the handshake now carries.

    The session JWT is no longer accepted in the socket URL -- it used to be,
    which put a full API credential in every access log between the browser
    and here. Each test therefore mints the ticket for the one socket it is
    about to open, exactly as the client does.
    """
    client.cookies.set(
        WS_TICKET_COOKIE,
        create_ws_ticket(user.id, kind, str(resource_id) if resource_id else None),
    )


class TestTheEventFeedRefusesARangeItsCallerCannotRead:
    def test_a_stranger_is_refused(self, client, world):
        as_user(client, world["stranger"], "events")
        url = f"/ws/events?range_id={world['range_a'].id}"
        assert refusal_code(client, url) == ws.WS_FORBIDDEN

    def test_an_unknown_range_is_not_found_rather_than_forbidden(self, client, world):
        as_user(client, world["owner"], "events")
        url = f"/ws/events?range_id={uuid.uuid4()}"
        assert refusal_code(client, url) == ws.WS_NOT_FOUND

    def test_a_bad_token_is_refused_before_any_range_is_looked_up(self, client, world):
        assert refusal_code(client, "/ws/events?token=nope") == ws.WS_UNAUTHENTICATED

    @pytest.mark.parametrize("who", ["owner", "admin", "learner"])
    def test_the_owner_an_admin_and_the_assigned_learner_are_admitted(self, client, world, who):
        as_user(client, world[who], "events")
        url = f"/ws/events?range_id={world['range_a'].id}"
        with client.websocket_connect(url) as socket:
            assert socket.receive_json()["type"] == "connected"

    def test_a_subscribe_action_is_authorised_on_its_own(self, client, world):
        """The URL is not the only way in. A socket admitted to its own range
        used to be able to name any other and be subscribed to it."""
        as_user(client, world["owner"], "events")
        url = f"/ws/events?range_id={world['range_a'].id}"
        with client.websocket_connect(url) as socket:
            socket.receive_json()
            socket.send_json({"action": "subscribe", "range_id": str(world["range_b"].id)})
            refused = socket.receive_json()
        assert refused["type"] == "subscribe_denied"
        assert refused["channel"] == f"range:{world['range_b'].id}"

    def test_a_subscribe_action_for_a_readable_range_succeeds(self, client, world):
        as_user(client, world["admin"], "events")
        url = "/ws/events"
        with client.websocket_connect(url) as socket:
            socket.receive_json()
            socket.send_json({"action": "subscribe", "range_id": str(world["range_b"].id)})
            accepted = socket.receive_json()
        assert accepted == {"type": "subscribed", "channel": f"range:{world['range_b'].id}"}

    def test_a_range_scoped_socket_takes_nothing_from_the_global_channel(
        self, client, world, monkeypatch
    ):
        """Asking for one range's feed should not also hand over the platform's."""
        manager = ConnectionManager()
        monkeypatch.setattr(eb, "_connection_manager", manager)
        as_user(client, world["owner"], "events")
        url = f"/ws/events?range_id={world['range_a'].id}"
        with client.websocket_connect(url) as socket:
            socket.receive_json()
            registered = next(iter(manager._connections.values()))
            assert registered.may_receive is eb._receives_nothing
            channel = f"{RANGE_CHANNEL_PREFIX}{world['range_a'].id}"
            assert manager._channel_subscribers[channel]

    def test_the_notification_socket_gets_the_share_of_the_feed_it_is_entitled_to(
        self, client, world, monkeypatch
    ):
        manager = ConnectionManager()
        monkeypatch.setattr(eb, "_connection_manager", manager)
        as_user(client, world["owner"], "events")
        with client.websocket_connect("/ws/events") as socket:
            socket.receive_json()
            may_receive = next(iter(manager._connections.values())).may_receive
            assert may_receive({"range_id": str(world["range_a"].id)}) is True
            assert may_receive({"range_id": str(world["range_b"].id)}) is False

    def test_a_vm_subscription_answers_to_its_range(self, client, world):
        as_user(client, world["stranger"], "events")
        url = "/ws/events"
        with client.websocket_connect(url) as socket:
            socket.receive_json()
            socket.send_json({"action": "subscribe_vm", "vm_id": str(world["vm"].id)})
            refused = socket.receive_json()
        assert refused["type"] == "subscribe_denied"


class TestADeactivatedAccountLosesItsSockets:
    """Deactivating someone closed every HTTP route to them and left the
    sockets open. `get_current_user` raises 403 on an inactive account; the
    websocket authenticator did not, and the token already issued stays valid
    for the rest of the hour."""

    def test_the_event_feed_refuses_it(self, client, db_session, world):
        world["owner"].is_active = False
        db_session.flush()
        as_user(client, world["owner"], "events")
        url = "/ws/events"
        assert refusal_code(client, url) == ws.WS_FORBIDDEN

    def test_the_console_of_its_own_range_refuses_it(self, client, db_session, world):
        """Owning the range is no longer the question once the account is off."""
        world["owner"].is_active = False
        db_session.flush()
        as_user(client, world["owner"], "console", world["vm"].id)
        url = f"/ws/console/{world['vm'].id}"
        assert refusal_code(client, url) == ws.WS_FORBIDDEN


class TestTheStatusFeedAndTheConsolesRefuseAStranger:
    def test_status_is_a_read_and_applies_the_read_rule(self, client, world):
        as_user(client, world["stranger"], "status", world["range_a"].id)
        url = f"/ws/status/{world['range_a'].id}"
        assert refusal_code(client, url) == ws.WS_FORBIDDEN

    def test_the_shell_console_refuses_before_it_touches_the_container(self, client, world):
        """Nothing mocks Docker here. Reaching it at all would be the defect."""
        as_user(client, world["stranger"], "console", world["vm"].id)
        url = f"/ws/console/{world['vm'].id}"
        assert refusal_code(client, url) == ws.WS_FORBIDDEN

    def test_the_vnc_proxy_refuses_before_it_dials_the_framebuffer(self, client, world):
        as_user(client, world["stranger"], "vnc", world["vm"].id)
        url = f"/ws/vnc/{world['vm'].id}"
        assert refusal_code(client, url) == ws.WS_FORBIDDEN

    def test_an_unknown_vm_is_not_found(self, client, world):
        absent = uuid.uuid4()
        as_user(client, world["owner"], "console", absent)
        url = f"/ws/console/{absent}"
        assert refusal_code(client, url) == ws.WS_NOT_FOUND

    def test_the_dind_range_console_is_administrator_only(self, client, world):
        """A privileged exec on the host daemon. The owner of the range is not
        enough, and the check reads the ABAC roles rather than the deprecated
        `role` column it used to consult."""
        as_user(client, world["owner"], "range-console", world["range_a"].id)
        url = f"/ws/range-console/{world['range_a'].id}"
        assert refusal_code(client, url) == ws.WS_FORBIDDEN


class TestWhoIsAdmittedToAConsole:
    """The endpoint's guard, asked directly -- past it lies Docker."""

    class _Socket:
        def __init__(self):
            self.closed = None

        async def close(self, code=None, reason=None):
            self.closed = (code, reason)

    async def test_the_owner_the_assigned_learner_and_an_admin_are_admitted(
        self, db_session, world
    ):
        for who in ("owner", "learner", "admin"):
            socket = self._Socket()
            assert await ws.require_console_access(socket, world["vm"], world[who], db_session)
            assert socket.closed is None

    async def test_an_event_participant_is_admitted(self, db_session, world):
        event = TrainingEvent(
            name="ex",
            start_datetime=__import__("datetime").datetime.now(),
            created_by_id=world["owner"].id,
        )
        db_session.add(event)
        db_session.flush()
        participant = make_user(db_session, "student")
        db_session.add(EventParticipant(event_id=event.id, user_id=participant.id))
        world["range_a"].training_event_id = event.id
        db_session.flush()

        socket = self._Socket()
        assert await ws.require_console_access(socket, world["vm"], participant, db_session)

    async def test_a_stranger_is_closed_with_a_reason(self, db_session, world):
        socket = self._Socket()
        assert not await ws.require_console_access(
            socket, world["vm"], world["stranger"], db_session
        )
        code, reason = socket.closed
        assert code == ws.WS_FORBIDDEN and reason


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(data)


def event_for(range_id) -> str:
    return json.dumps(
        {
            "event_type": "deployment_step",
            "range_id": str(range_id),
            "message": "namespace pg-range-... created",
        }
    )


class TestTheFanOut:
    """Authorising a subscription decides nothing if every socket gets
    everything anyway. These use a manager that was never started, so no Redis
    is involved -- routing is the part under test."""

    async def test_a_message_for_one_range_does_not_reach_another_ranges_socket(self):
        manager = ConnectionManager()
        a, b = FakeSocket(), FakeSocket()
        await manager.connect("a", a)
        await manager.connect("b", b)
        await manager.subscribe_to_range("a", "range-a")
        await manager.subscribe_to_range("b", "range-b")

        payload = event_for("range-a")
        await manager._route_message(f"{RANGE_CHANNEL_PREFIX}range-a", payload)

        assert a.sent == [payload]
        assert b.sent == []

    async def test_the_global_channel_no_longer_reaches_every_socket(self):
        """This is the leak itself: the same payload is published to the global
        channel as well, and it used to be delivered to everyone."""
        manager = ConnectionManager()
        b = FakeSocket()
        await manager.connect("b", b)
        await manager.subscribe_to_range("b", "range-b")

        await manager._route_message(EVENTS_CHANNEL, event_for("range-a"))

        assert b.sent == []

    async def test_a_connection_that_names_no_entitlement_receives_nothing_global(self):
        manager = ConnectionManager()
        socket = FakeSocket()
        await manager.connect("c", socket)

        await manager._route_message(EVENTS_CHANNEL, event_for("range-a"))

        assert socket.sent == []

    async def test_an_entitled_connection_receives_it_once(self):
        manager = ConnectionManager()
        entitled, subscribed = FakeSocket(), FakeSocket()
        await manager.connect("entitled", entitled, lambda event: True)
        await manager.connect("subscribed", subscribed, lambda event: True)
        await manager.subscribe_to_range("subscribed", "range-a")

        payload = event_for("range-a")
        await manager._route_message(f"{RANGE_CHANNEL_PREFIX}range-a", payload)
        await manager._route_message(EVENTS_CHANNEL, payload)

        assert entitled.sent == [payload]
        # Not twice: it already had it on the range channel.
        assert subscribed.sent == [payload]

    async def test_an_entitlement_check_that_raises_refuses_rather_than_broadcasts(self):
        manager = ConnectionManager()
        socket = FakeSocket()

        def broken(_event):
            raise RuntimeError("no database")

        await manager.connect("c", socket, broken)
        await manager._route_message(EVENTS_CHANNEL, event_for("range-a"))

        assert socket.sent == []

    async def test_an_unparseable_payload_is_dropped_rather_than_delivered(self):
        manager = ConnectionManager()
        socket = FakeSocket()
        await manager.connect("c", socket, lambda event: True)

        await manager._route_message(EVENTS_CHANNEL, "{not json")

        assert socket.sent == []


class TestTheGlobalFeedsEntitlementRule:
    """What one connection's share of the global channel is."""

    def test_a_range_event_follows_the_ranges_read_rule(self, client, world):
        for who, expected in (("owner", True), ("admin", True), ("stranger", False)):
            may_receive = ws._event_entitlement(world[who])
            assert may_receive({"range_id": str(world["range_a"].id)}) is expected

    def test_a_deactivated_account_stops_receiving_its_own_ranges_events(
        self, client, db_session, world
    ):
        may_receive = ws._event_entitlement(world["owner"])
        world["owner"].is_active = False
        db_session.flush()
        assert may_receive({"range_id": str(world["range_a"].id)}) is False

    def test_a_notification_addressed_to_one_user_reaches_only_them(self, client, world):
        event = {"event_type": "notification", "data": {"user_id": str(world["owner"].id)}}
        assert ws._event_entitlement(world["owner"])(event) is True
        assert ws._event_entitlement(world["stranger"])(event) is False
        assert ws._event_entitlement(world["admin"])(event) is False

    def test_a_notification_addressed_to_a_role_reaches_that_role(self, client, world):
        event = {"event_type": "notification", "data": {"target_role": "engineer"}}
        assert ws._event_entitlement(world["stranger"])(event) is True
        assert ws._event_entitlement(world["learner"])(event) is False

    def test_a_role_notification_does_not_reach_an_admin_outside_that_role(self, client, world):
        """The bell lists a role notification for the holders of that role and
        nobody else. A toast an admin can never find again is worse than none."""
        event = {"event_type": "notification", "data": {"target_role": "engineer"}}
        assert ws._event_entitlement(world["admin"])(event) is False

    def test_an_event_addressed_to_nobody_is_platform_wide_and_goes_to_admins(self, client, world):
        event = {"event_type": "system.update", "message": "restarting"}
        assert ws._event_entitlement(world["admin"])(event) is True
        assert ws._event_entitlement(world["owner"])(event) is False


def notification(**audience) -> dict:
    """The shape notification_service publishes: no range_id, audience in data.

    Getting this wrong is why the resource-scoped case went missing -- a
    notification about a range carries no `range_id` at all.
    """
    data = {"notification_type": "system_alert", "title": "t"}
    data.update(audience)
    return {"event_type": "notification", "message": "m", "range_id": None, "data": data}


class TestANotificationReachesTheAudienceTheBellShowsItTo:
    """`notify_range_users` and `notify_event_participants` address the
    resource, not a user or a role. The live feed ignored that and the
    air-gap warning an image was pulled from the internet reached only
    admins -- while the bell went on listing it for the range's owner."""

    def test_a_range_scoped_notification_reaches_the_ranges_people(self, client, world):
        event = notification(resource_type="range", resource_id=str(world["range_a"].id))
        assert ws._event_entitlement(world["owner"])(event) is True
        assert ws._event_entitlement(world["learner"])(event) is True
        assert ws._event_entitlement(world["admin"])(event) is True
        assert ws._event_entitlement(world["stranger"])(event) is False

    def test_an_event_scoped_notification_reaches_its_participants(self, client, db_session, world):
        training_event = TrainingEvent(
            name="ex", start_datetime=datetime.now(), created_by_id=world["owner"].id
        )
        db_session.add(training_event)
        db_session.flush()
        participant = make_user(db_session, "student")
        db_session.add(EventParticipant(event_id=training_event.id, user_id=participant.id))
        db_session.flush()

        event = notification(resource_type="event", resource_id=str(training_event.id))
        assert ws._event_entitlement(participant)(event) is True
        assert ws._event_entitlement(world["stranger"])(event) is False

    def test_an_unknown_resource_type_addresses_nobody(self, client, world):
        event = notification(resource_type="blueprint", resource_id=str(uuid.uuid4()))
        assert ws._event_entitlement(world["owner"])(event) is False
        assert ws._event_entitlement(world["admin"])(event) is False
