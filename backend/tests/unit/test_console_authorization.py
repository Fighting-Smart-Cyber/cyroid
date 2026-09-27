"""The VNC data path checks who is asking.

SEC-10's second half. The route was:

    PathPrefix(`/vnc/{vm_id}`) -> strip prefix -> inject Basic auth -> VNC

with nothing authenticating anyone. Traefik supplied KasmVNC's credentials on
the caller's behalf, so **knowing a VM's id was full console access with no
login at all** — worse than the range endpoints, which at least required a
session. `can_access_vm_console` gated only `/vnc-info`, the metadata endpoint
that hands out the URL, and the URL was never the secret.

The console loads in an iframe, and a browser navigation carries no
Authorization header — the JWT lives in localStorage. So the data path
structurally cannot present the app's token, which is why it was left open.
A short-lived, VM-scoped cookie can travel that path, and Traefik's forwardAuth
makes the API decide before it proxies anything.
"""

import inspect
import re
from uuid import uuid4

from proving_ground.api import vms as vms_api
from proving_ground.services import traefik_route_service as trs
from proving_ground.utils.security import create_console_ticket, verify_console_ticket


class TestTheTicketIsBoundToOneVm:
    def test_a_valid_ticket_returns_its_user(self):
        vm, user = uuid4(), uuid4()
        assert verify_console_ticket(create_console_ticket(vm, user), vm) == user

    def test_a_ticket_for_one_vm_does_not_open_another(self):
        """Otherwise any console you may open is every console you may open."""
        ticket = create_console_ticket(uuid4(), uuid4())
        assert verify_console_ticket(ticket, uuid4()) is None

    def test_an_access_token_is_not_a_console_ticket(self):
        """Both are signed with the same key. Without the type claim, a stolen
        session token would be a console key for any VM."""
        from proving_ground.utils.security import create_access_token

        assert verify_console_ticket(create_access_token(uuid4()), uuid4()) is None

    def test_an_expired_ticket_is_refused(self):
        ticket = create_console_ticket(uuid4(), uuid4(), minutes=-1)
        assert verify_console_ticket(ticket, uuid4()) is None

    def test_a_tampered_ticket_is_refused(self):
        vm, user = uuid4(), uuid4()
        ticket = create_console_ticket(vm, user)
        assert verify_console_ticket(ticket[:-4] + "AAAA", vm) is None

    def test_rubbish_is_refused_rather_than_raising(self):
        """The verifier runs on unauthenticated input; it must not 500."""
        for junk in ("", "x", "a.b.c", "not a token"):
            assert verify_console_ticket(junk, uuid4()) is None


class TestForwardAuthIsFirstInTheChain:
    """Ordering is the whole fix.

    Everything after it strips the prefix and injects the Basic credentials, so
    a check placed later authorises a request whose credentials were already
    supplied.
    """

    def test_the_authz_middleware_is_added_before_the_others(self):
        src = inspect.getsource(trs.TraefikRouteService)
        assert "route_middlewares = [authz_middleware_name, middleware_name]" in src, (
            "forwardAuth is not first; the auth header would be injected for "
            "an unauthorised caller."
        )

    def test_the_route_uses_forwardauth_at_all(self):
        src = inspect.getsource(trs.TraefikRouteService)
        assert "forwardAuth" in src
        assert "console-authz" in src or "authz_address" in src

    def test_the_address_is_internal_not_the_public_host(self):
        """The check must not depend on the deployment being reachable from
        outside itself."""
        svc = trs.TraefikRouteService(routes_dir="/tmp")
        assert svc.authz_address.startswith("http://"), svc.authz_address
        assert "://api:" in svc.authz_address or "127.0.0.1" in svc.authz_address


class TestTheAuthzEndpointAcceptsOnlyTheTicket:
    def test_it_takes_no_current_user(self):
        """The request being authorised is the iframe's, which has no
        Authorization header. Requiring a session here would refuse every
        legitimate console."""
        sig = inspect.signature(vms_api.authorize_console_request)
        assert "current_user" not in sig.parameters

    def test_it_reads_the_vm_from_the_forwarded_uri(self):
        src = inspect.getsource(vms_api.authorize_console_request)
        assert "X-Forwarded-Uri" in src

    def test_it_refuses_when_no_cookie_is_present(self):
        src = inspect.getsource(vms_api.authorize_console_request)
        assert "if not ticket" in src and "HTTP_403_FORBIDDEN" in src

    def test_it_refuses_a_uri_it_cannot_parse(self):
        """A URI that is not /vnc/{uuid} must fail closed, not fall through."""
        src = inspect.getsource(vms_api.authorize_console_request)
        assert "if not m:" in src

    def test_the_uri_pattern_matches_real_console_paths_and_rejects_others(self):
        src = inspect.getsource(vms_api.authorize_console_request)
        pattern = re.search(r'r"(\^/vnc/[^"]+)"', src)
        assert pattern, "could not find the URI pattern to test"
        rx = re.compile(pattern.group(1))
        vm = str(uuid4())
        assert rx.match(f"/vnc/{vm}/")
        assert rx.match(f"/vnc/{vm}/websockify")
        assert rx.match(f"/vnc/{vm}?autoconnect=1")
        # Not a console path, and a traversal attempt.
        assert not rx.match("/vnc/../admin")
        assert not rx.match("/api/v1/vms")


class TestIssuingATicketIsGated:
    def test_it_uses_the_console_entitlement_check(self):
        """Not check_range_access, which was the original mistake: that is a
        visibility model and grants an untagged range to everyone."""
        src = inspect.getsource(vms_api.issue_console_ticket)
        assert "check_console_access" in src
        assert "check_range_access" not in src, (
            "the ticket gate is back on the read model, where an untagged "
            "range is open to any non-student account"
        )

    def test_vnc_info_uses_the_same_check(self):
        """It hands out the console path, so it answers the same question."""
        src = inspect.getsource(vms_api.get_vm_vnc_info)
        assert "check_console_access" in src
        assert "check_range_access" not in src

    def test_the_cookie_is_scoped_to_one_vm_and_not_script_readable(self):
        src = inspect.getsource(vms_api.issue_console_ticket)
        assert 'path=f"/vnc/{vm_id}"' in src, "cookie is not path-scoped to the VM"
        assert "httponly=True" in src
        assert "samesite=" in src

    def test_the_route_is_declared_before_the_dynamic_one(self):
        """FastAPI matches in declaration order. Below /{vm_id}, every
        forwardAuth call would arrive as get_vm(vm_id="console-authz") and each
        console would be refused."""
        src = inspect.getsource(vms_api)
        authz = src.index('@router.get("/console-authz")')
        dynamic = src.index('@router.get("/{vm_id}", response_model=VMResponse)')
        assert authz < dynamic, "/console-authz is shadowed by /{vm_id}"


def _code_of(fn) -> str:
    """Source with the docstring removed.

    These checks are about what the function DOES. Its docstring quotes the
    very identifiers that caused the bug, in order to explain it.
    """
    src = inspect.getsource(fn)
    parts = src.split('"""')
    return parts[0] + "".join(parts[2:]) if len(parts) >= 3 else src


class TestConsoleEntitlementIsExplicit:
    """Reported live: a user holding the engineer role opened an admin-owned
    range's console by pasting the URL, having no relationship to it.

    The forwardAuth fix authenticated the path correctly and then authorised
    the wrong thing. The ticket gate ran check_resource_access, a VISIBILITY
    model whose rule is "no tags = public" -- and every range on that host was
    untagged, so every non-student account was entitled to every console. A
    console is interactive control of a machine, not a read.
    """

    def test_it_does_not_consult_tags(self):
        # Code only. The docstring names check_resource_access to explain what
        # was wrong, and that explanation is worth keeping.
        src = _code_of(vms_api.check_console_access)
        assert "ResourceTag" not in src and "has_any_tag" not in src, (
            "console access is back on the tag model, where an untagged range "
            "is open to everyone"
        )
        assert "check_resource_access" not in src

    def test_a_role_alone_grants_nothing(self):
        """can_access_vm_console returns True for any engineer or evaluator on
        any range. Holding a role is not a relationship to someone else's
        exercise, so the entitlement check must not ask about roles."""
        src = _code_of(vms_api.check_console_access)
        assert "has_any_role" not in src

    def test_entitlement_is_owner_admin_assigned_or_participant(self):
        src = inspect.getsource(vms_api.check_console_access)
        for expected in (
            "user.is_admin",
            "range_obj.created_by == user.id",
            "range_obj.assigned_to_user_id == user.id",
            "EventParticipant",
        ):
            assert expected in src, f"entitlement path missing: {expected}"

    def test_a_missing_range_is_refused_not_allowed(self):
        """can_access_vm_console returned True when the range was gone. An
        orphaned VM must not be a console anyone can open."""
        src = inspect.getsource(vms_api.check_console_access)
        head = src[: src.index("entitled =")]
        assert "HTTP_403_FORBIDDEN" in head, "a missing range falls through instead of refusing"

    def test_visibility_still_applies_to_the_entitled(self):
        """Being entitled to the range is not being entitled to every VM in it:
        a VM hidden for the exercise stays hidden."""
        src = inspect.getsource(vms_api.check_console_access)
        assert "can_access_vm_console" in src

    def test_both_console_entry_points_use_it(self):
        for fn in (vms_api.issue_console_ticket, vms_api.get_vm_vnc_info):
            assert "check_console_access" in inspect.getsource(fn), fn.__name__


class TestTheTicketWindowIsBounded:
    """Nothing at the proxy knows who is logged in -- forwardAuth sees a cookie,
    not a session. So a ticket outlives the session that obtained it: whoever
    holds the browser next can use it, and a user whose access is revoked keeps
    it until it expires. The window is what bounds both."""

    def test_the_ticket_is_short_lived(self):
        from proving_ground.utils import security

        assert security.CONSOLE_TICKET_MINUTES <= 15, (
            f"tickets last {security.CONSOLE_TICKET_MINUTES} minutes; that is "
            "how long a revoked user keeps console access"
        )

    def test_the_cookie_does_not_outlive_the_browser(self):
        src = inspect.getsource(vms_api.issue_console_ticket)
        assert "max_age=" not in src, (
            "a max_age cookie leaves a working console key on a shared machine "
            "after the browser closes"
        )


class TestATicketIsNotASession:
    """Every ticket here is signed with the platform's one key, so a valid
    signature says nothing about what the token is for.

    `decode_access_token` read `sub` and stopped. The app ticket lasts eight
    hours and reaches the browser as a cookie on a path Traefik forwards into a
    third-party training workload, which can read it off the Cookie header and
    replay it as `Authorization: Bearer` -- a full session for that user, for
    the rest of the class. The console ticket was the same trade over ten
    minutes. `verify_console_ticket` and `verify_app_ticket` had always refused
    an access token; nothing did the inverse until now.
    """

    def test_a_console_ticket_is_not_an_access_token(self):
        from proving_ground.utils.security import decode_access_token

        assert decode_access_token(create_console_ticket(uuid4(), uuid4())) is None

    def test_an_app_ticket_is_not_an_access_token(self):
        from proving_ground.api.kubernetes_apps import create_app_ticket
        from proving_ground.utils.security import decode_access_token

        assert decode_access_token(create_app_ticket(uuid4(), uuid4())) is None

    def test_an_access_token_still_decodes(self):
        from proving_ground.utils.security import create_access_token, decode_access_token

        user = uuid4()
        assert decode_access_token(create_access_token(user)) == user

    def test_a_token_minted_before_the_type_claim_still_decodes(self):
        """Refusing these would log every signed-in operator out at upgrade.
        Absent is accepted; present-and-wrong is refused."""
        from datetime import datetime, timedelta, timezone

        from jose import jwt

        from proving_ground.config import get_settings
        from proving_ground.utils.security import decode_access_token

        settings = get_settings()
        user = uuid4()
        legacy = jwt.encode(
            {"sub": str(user), "exp": datetime.now(timezone.utc) + timedelta(minutes=60)},
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
        )
        assert decode_access_token(legacy) == user

    def test_a_console_ticket_still_opens_its_own_console(self):
        """The refusal above must not cost the ticket its actual job."""
        vm, user = uuid4(), uuid4()
        assert verify_console_ticket(create_console_ticket(vm, user), vm) == user

    def test_a_replayed_ticket_does_not_reach_a_route(self, db_session):
        """The helper is not where the replay lands -- `get_current_user` is.

        The workload reads the cookie off the Cookie header and sends it back as
        `Authorization: Bearer`, so what has to return 401 is a real request to a
        real dependency, not just the decode helper it happens to call today.
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from proving_ground.api.deps import CurrentUser
        from proving_ground.api.kubernetes_apps import create_app_ticket
        from proving_ground.database import get_db
        from proving_ground.models.user import User, UserRole
        from proving_ground.utils.security import create_access_token

        operator = User(
            username="op",
            email="op@x.invalid",
            hashed_password="x",
            role=UserRole.ADMIN,
            is_active=True,
            is_approved=True,
        )
        db_session.add(operator)
        db_session.commit()

        app = FastAPI()

        @app.get("/whoami")
        def whoami(current_user: CurrentUser):
            return {"user": str(current_user.id)}

        app.dependency_overrides[get_db] = lambda: db_session
        client = TestClient(app)

        def status_for(token: str) -> int:
            return client.get("/whoami", headers={"Authorization": f"Bearer {token}"}).status_code

        assert status_for(create_access_token(operator.id)) == 200
        assert status_for(create_app_ticket(uuid4(), operator.id)) == 401
        assert status_for(create_console_ticket(uuid4(), operator.id)) == 401
