# backend/proving_ground/utils/security.py
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from jose import JWTError, jwt
from passlib.context import CryptContext

from proving_ground.config import get_settings

settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


# ASD STIG APSC-DV-001680: the application must enforce a minimum 15-character password length.
# 15, not 8, and enforced in one place on purpose. Three endpoints accept a password - register,
# admin user-create, and change-password - and before this only change-password checked a length at
# all, so the minimum could be walked straight past by creating the account instead of changing it.
# A per-endpoint `if len(...) < N` is how that happened; anything new that accepts a password calls
# this instead.
PASSWORD_MIN_LENGTH = 15


def password_policy_error(password: Optional[str]) -> Optional[str]:
    """Return the reason a password is unacceptable, or None when it is fine.

    Returns a message rather than raising, so the HTTP layer keeps deciding status codes and this
    module stays free of framework imports.
    """
    if len(password or "") < PASSWORD_MIN_LENGTH:
        return "Password must be at least %d characters" % PASSWORD_MIN_LENGTH
    return None


def create_access_token(user_id: UUID, expires_delta: Optional[timedelta] = None) -> str:
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)

    to_encode = {
        "sub": str(user_id),
        "typ": "access",
        "exp": expire,
    }
    encoded_jwt = jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return encoded_jwt


def decode_access_token(token: str) -> Optional[UUID]:
    """The user a *session* token names, or None.

    Every ticket this platform issues is signed with the same key, so a
    signature alone says nothing about what a token is for. Without the type
    check below, the range-application ticket -- eight hours long, handed to the
    browser as a cookie on a path Traefik forwards into a third-party training
    workload -- was a full API bearer token for its whole life, and so was the
    console ticket.

    Absent is accepted; present-and-wrong is refused. Tokens minted before
    0.52 carry no `typ`, and requiring the claim would sign every operator out
    the moment the new image came up. Once no pre-0.52 token can still be alive
    -- jwt_expire_minutes after the upgrade -- this can tighten to requiring
    `typ == "access"`, and should.
    """
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        typ = payload.get("typ")
        if typ is not None and typ != "access":
            return None
        user_id: str = payload.get("sub")
        if user_id is None:
            return None
        return UUID(user_id)
    except JWTError:
        return None


# --- console tickets -------------------------------------------------------
#
# The VNC data path cannot present the app's JWT. The console loads in an
# iframe, and a browser navigation carries no Authorization header -- the token
# lives in localStorage, which the iframe's requests know nothing about. So the
# route was left open, and Traefik injected KasmVNC's Basic credentials for
# whoever arrived: knowing a VM's id was full console access, no login at all.
#
# A ticket closes that. An authenticated request exchanges its JWT for a
# short-lived ticket scoped to ONE vm, delivered as a cookie the browser then
# sends automatically on every request under /vnc/{vm_id} -- including the
# websocket and every asset, which a query parameter could not cover.

# Short on purpose. The cookie lives in the browser, and nothing at the proxy
# knows who is logged in -- forwardAuth sees a cookie, not a session. So a
# ticket issued to one user is usable by whoever holds that browser next, and
# a user whose access is revoked keeps it until the ticket dies. Ten minutes
# bounds both. The console re-tickets whenever it is opened, so this is not
# felt in normal use.
CONSOLE_TICKET_MINUTES = 10


def create_console_ticket(vm_id: UUID, user_id: UUID, minutes: int = CONSOLE_TICKET_MINUTES) -> str:
    """A ticket for ONE vm's console, bound to the user it was issued to.

    `vm` is in the payload and checked on use, so a ticket for a VM you may see
    is not a ticket for one you may not. `typ` keeps it from being accepted
    anywhere an access token is expected, and vice versa.
    """
    to_encode = {
        "sub": str(user_id),
        "vm": str(vm_id),
        "typ": "console",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=minutes),
    }
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def verify_console_ticket(token: str, vm_id: UUID) -> Optional[UUID]:
    """Return the user the ticket was issued to, or None.

    None on anything wrong: bad signature, expired, wrong vm, or an access
    token presented in place of a ticket. The caller turns that into a 403 and
    has no way to accidentally treat a failure as a pass.
    """
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None

    if payload.get("typ") != "console":
        return None
    if payload.get("vm") != str(vm_id):
        return None

    user_id = payload.get("sub")
    if not user_id:
        return None
    try:
        return UUID(user_id)
    except ValueError:
        return None


# --- websocket tickets -----------------------------------------------------
#
# A websocket handshake is a browser navigation as much as an iframe is:
# `new WebSocket(url)` takes a URL and nothing else, so there is no header to
# put an Authorization value in. Every `/ws/` route answered that by taking the
# session JWT as `?token=`, which re-opened the problem the console ticket
# exists to close -- and with a worse credential. A URL is not a header: it is
# written to the browser's history, to the access log of every proxy and
# ingress between the browser and the API, and into a Referer if the page ever
# links out. A session token read out of any of those is the whole API until it
# expires.
#
# So the console ticket's shape, one socket wider. An authenticated request
# exchanges its JWT for a ticket naming ONE socket; it is delivered as a cookie
# whose path is that socket's own path, so the handshake sends it by itself and
# the URL carries nothing secret at all.
#
# The scope is a (kind, resource) pair rather than a URL, deliberately: the
# route that checks it rebuilds the pair from its own path parameters, so no
# difference in percent-encoding or trailing slash between the minting request
# and the handshake can make a ticket verify against the wrong socket.

WS_TICKET_COOKIE = "pg_ws_ticket"

# Minutes, not hours. The ticket is spent at the handshake and the socket then
# lives as long as the page does, so a long life buys nothing and costs the
# usual thing: the cookie sits in the browser, and forwarding it or sitting
# down at the machine is enough to use it. Every client mints a fresh one
# immediately before it connects, including on every reconnect attempt, so
# five minutes is not felt.
WS_TICKET_MINUTES = 5


def _ws_scope(kind: str, resource_id: Optional[str]) -> str:
    """The one socket a ticket opens, as a single comparable string."""
    return f"{kind}:{resource_id}" if resource_id else kind


def create_ws_ticket(
    user_id: UUID,
    kind: str,
    resource_id: Optional[str] = None,
    minutes: int = WS_TICKET_MINUTES,
) -> str:
    """A ticket for ONE websocket, bound to the user it was issued to.

    `kind` names the route ("console", "vnc", "range-console", "status",
    "events") and `resource_id` the thing it opens, so a ticket for a machine
    you may console into is not a ticket for one you may not. A route whose
    path names no resource -- the event feed -- passes None and authorises what
    the socket asks for at connect time, which is what it already did.

    `typ` keeps it from being accepted where an access token is expected:
    `decode_access_token` refuses any token carrying a `typ` that is not
    "access", so a ws ticket is not an API bearer token.
    """
    to_encode = {
        "sub": str(user_id),
        "ws": _ws_scope(kind, resource_id),
        "typ": "ws",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=minutes),
    }
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def verify_ws_ticket(
    token: Optional[str], kind: str, resource_id: Optional[str] = None
) -> Optional[UUID]:
    """The user this ticket was issued to, or None.

    None on anything wrong -- absent, malformed, bad signature, expired,
    another socket, or an access token presented in place of a ticket. The
    caller turns that into a refusal and has no way to read a failure as a
    pass. It runs on unauthenticated input, so it returns rather than raises.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None

    if payload.get("typ") != "ws":
        return None
    if payload.get("ws") != _ws_scope(kind, resource_id):
        return None

    user_id = payload.get("sub")
    if not user_id:
        return None
    try:
        return UUID(user_id)
    except ValueError:
        return None


def ws_ticket_cookie_kwargs(path: str) -> dict:
    """`response.set_cookie(value=ticket, **ws_ticket_cookie_kwargs(path))`.

    One definition so that every route minting a ws ticket scopes its cookie
    the same way. `path` is the full request path of the socket the ticket
    opens, mount prefix included, so the browser offers the cookie to that
    socket and to nothing else -- a second console's handshake is a sibling
    path and never sees it.
    """
    return {
        "key": WS_TICKET_COOKIE,
        "path": path,
        # A session cookie on purpose: no max_age, so closing the browser
        # discards it rather than leaving a working key behind on a shared
        # machine. The token's own expiry bounds it either way.
        "expires": None,
        # Nothing in the page needs to read it back, and a training workload
        # that got script execution on this origin must not be able to either.
        "httponly": True,
        "samesite": "lax",
        # Set only over TLS in production. Left off when the deployment itself
        # is plain HTTP, or the browser would never return the cookie and every
        # socket would fail closed. Same switch as the console ticket, because
        # it is the same question about the same deployment.
        "secure": get_settings().console_cookie_secure,
    }
