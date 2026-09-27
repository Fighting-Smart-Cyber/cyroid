"""A range's applications in the learner's browser -- COSMOS PG-62 (RNET-3).

An application belongs to a range and a range gets a host of its own: `deploy_range_on_kubernetes`
publishes each web-facing capability at `<range-id>.<RANGE_APPS_HOST>/<app>/`, behind a ForwardAuth
middleware that asks this module, per request, whether the browser may pass. That host is not the
platform's, deliberately. The application is software the learner is being trained against -- third
party at best, hostile by design in a red-team exercise -- and served from the platform's own host
its JavaScript would be same-origin with the console, free to read the operator's token out of
`localStorage` and call the API as whoever opened it. An install with no host for its applications
publishes none.

The browser's navigation carries no Authorization header, so the credential is a cookie. The
platform cannot set a cookie for a host it does not answer on -- that is the separation working --
so the session crosses the boundary as a one-time code in the URL, which the ForwardAuth target
trades for the cookie on the very first request and then never sees again:

    POST /ranges/{id}/apps/{app}/ticket    -> the absolute URL to open, carrying a handoff code
    GET  /range-apps/authz                 -> Traefik's ForwardAuth target: 200 with a good
                                              cookie, a 302 that sets one in exchange for a
                                              code, 403 otherwise (not under /ranges/: that
                                              prefix's {range_id} route would capture it and
                                              demand a session)

`typ` separates the three credentials: a console ticket is not an app ticket, a handoff code is not
either, and none of them is an access token.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status

from proving_ground.api.deps import CurrentUser, DBSession
from proving_ground.api.kubernetes_console import _blueprint_of, check_range_console_access
from proving_ground.capability.models import to_dns_label
from proving_ground.config import get_settings
from proving_ground.utils import app_tokens
from proving_ground.utils.range_hosts import range_id_from_host
from proving_ground.models.range import Range
from proving_ground.services import kubernetes_range_service as svc

logger = logging.getLogger(__name__)

router = APIRouter(tags=["kubernetes"])

APP_COOKIE_NAME = app_tokens.APP_COOKIE_NAME
# Re-exported: these moved to utils/app_tokens so pg-gateway can verify a credential without
# importing this module, which carries a Kubernetes client and the ORM with it.
create_app_ticket = app_tokens.create_app_ticket
verify_app_ticket = app_tokens.verify_app_ticket
create_app_code = app_tokens.create_app_code
verify_app_code = app_tokens.verify_app_code
_code_in = app_tokens.code_in
_uri_without_code = app_tokens.uri_without_code

# The cookie IS the credential for as long as it lives, and nothing at the ingress knows who is
# logged in -- ForwardAuth sees a cookie, not a session. So whoever holds the browser holds the
# application, and a learner removed from an exercise keeps it until the ticket dies. Eight hours,
# picked as "a class session", made that window most of a working day. The console bounds the same
# exposure at ten minutes by being short and re-ticketing on open; the platform mints a fresh
# handoff code every time Open is clicked, so re-entry here is one click too.
#
# Sliding renewal is the obvious alternative and is not available. Traefik copies headers off a
# 2xx ForwardAuth response only for the names in `authResponseHeaders`, which this install's
# middleware does not set (capability/lifecycle.py builds it with an address and nothing else), so
# a 200 cannot refresh the cookie. Renewing with a 302 the way first entry does would fire on
# subresources as well as navigations, and a browser that refused the new cookie would present the
# old one again, still inside the renewal window, on the redirected request -- a redirect loop
# with no state anywhere to break it. Short and re-entered is the version that cannot hang.
APP_TICKET_MINUTES = app_tokens.APP_TICKET_MINUTES

APP_CODE_QUERY = app_tokens.APP_CODE_QUERY
APP_CODE_SECONDS = app_tokens.APP_CODE_SECONDS

_NO_APPS_HOST = (
    "This install publishes no range applications: it has no host of its own to publish them "
    "on (RANGE_APPS_HOST). They are deliberately not served on this one -- a training "
    "application here could read your session."
)
_NO_INGRESS_CLASS = (
    "This install has no ingress class for range applications (RANGE_INGRESS_CLASS)."
)

__all__ = [
    "APP_COOKIE_NAME",
    "apps_of",
    "create_app_code",
    "create_app_ticket",
    "router",
    "verify_app_code",
    "verify_app_ticket",
]


def _prefix() -> str:
    return get_settings().range_ingress_path_prefix.rstrip("/")


def _app_url(range_id: UUID, app: str) -> str:
    return f"{svc.apps_scheme()}://{svc.app_host_for(range_id)}{_prefix()}/{app}/"


def apps_of(db, range_obj: Range) -> list[dict[str, Any]]:
    """The range's applications, where each answers, and why it does not."""
    blueprint = _blueprint_of(db, range_obj)
    host, ingress_class = svc.apps_host(), get_settings().range_ingress_class
    published = bool(host) and bool(ingress_class)
    apps = []
    for spec in blueprint.capabilities:
        if spec.web is None:
            continue
        name = to_dns_label(spec.name)
        if not host:
            reason: Optional[str] = _NO_APPS_HOST
        elif not ingress_class:
            reason = _NO_INGRESS_CLASS
        elif range_obj.status.value != "running":
            reason = "Available once the range is running."
        else:
            reason = None
        apps.append(
            {
                "name": name,
                "url": _app_url(range_obj.id, name) if published else None,
                # "published", not "available": this is derived from the apps host, the ingress
                # class and the range's status, and it reads no pod, no replica count and no
                # Helm release. It says the application has somewhere to answer, never that
                # anything is answering. Making that claim true is `verify()`, not a rename.
                "published": reason is None,
                "reason": reason,
            }
        )
    return apps


@router.post("/ranges/{range_id}/apps/{app}/ticket")
def issue_app_ticket(
    range_id: UUID, app: str, db: DBSession, current_user: CurrentUser, response: Response
):
    """Exchange the session for a URL on the range's own host that will let the browser in.

    No cookie is set here. This host cannot set one for the application's host, and the point of
    the separation is that it cannot: the URL carries a handoff code instead, and the ForwardAuth
    target on the far side turns it into the cookie. The body therefore holds a credential, one
    use and thirty seconds long, which nothing between here and the browser should keep.
    """
    range_obj = db.query(Range).filter(Range.id == range_id).first()
    if not range_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Range not found")
    check_range_console_access(range_obj, current_user, db)
    blueprint = _blueprint_of(db, range_obj)
    name = to_dns_label(app)
    if not any(c.web and to_dns_label(c.name) == name for c in blueprint.capabilities):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such application in this range"
        )
    if not svc.apps_host():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_NO_APPS_HOST)
    if not get_settings().range_ingress_class:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_NO_INGRESS_CLASS)
    code = create_app_code(range_id, current_user.id)
    url = f"{_app_url(range_id, name)}?{urlencode({APP_CODE_QUERY: code})}"
    response.headers["Cache-Control"] = "no-store"
    return {"url": url, "expires_in": APP_TICKET_MINUTES * 60}


def _host_and_range(request: Request) -> Optional[tuple[str, UUID]]:
    """The authority asked for and the range it names, or None -- which the caller refuses.

    The path names nothing now, so this is the whole of the routing decision and it fails
    closed: the host has to be exactly one label beneath the configured suffix, and that label
    has to be a range key.

    The port is not part of the name, so it comes off before the suffix is matched -- and goes
    back into what is returned, because the handoff redirects to this same authority and an
    install reached on anything but the default port would otherwise be sent to the default one.
    Nothing but digits survives that round trip: a port that is not `:<digits>` stays in the
    string the suffix is matched against, and fails.
    """
    suffix = svc.apps_host()
    if not suffix:
        return None
    authority = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip().lower()
    range_id = range_id_from_host(authority, suffix)
    return None if range_id is None else (authority, range_id)


@router.get("/range-apps/authz")
def authorize_app_request(request: Request, db: DBSession):
    """Traefik's ForwardAuth target: 2xx lets the request through, anything else stops it.

    No CurrentUser on purpose -- the request being authorised is the browser's navigation into
    the application, which has no Authorization header. The range is named by the host the
    browser asked for, because the path no longer names it; `X-Forwarded-Host` is what carries
    that, and Traefik sets it from a Host header it has already matched a route against.

    This must stay FIRST in the middleware chain. Everything after it rewrites the request it
    was asked about -- StripPrefix removes the segment that selects the application -- so a
    check placed later authorises a request that is no longer the one that arrived.

    Three answers. 200 when the cookie is good. A 302 that sets the cookie when the browser
    arrives holding a handoff code, which works because Traefik copies a non-2xx auth response
    back to the browser headers and all, and a cookie set on that response belongs to the
    application's host -- the one origin the platform cannot write to directly. 403 otherwise.
    """
    named = _host_and_range(request)
    if named is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    authority, range_id = named
    ticket = request.cookies.get(APP_COOKIE_NAME)
    if ticket and verify_app_ticket(ticket, range_id) is not None:
        # The ticket was minted after the entitlement check and nothing re-checks it here, so a
        # revoked user keeps the application until it expires. APP_TICKET_MINUTES is what bounds
        # that, and is the whole reason it is minutes rather than a working day.
        return {"ok": True}
    uri = request.headers.get("X-Forwarded-Uri", "")
    code = _code_in(uri)
    if not code:
        # A cookie that did not verify and no cookie at all are different problems to the person
        # reading the page: one is "your time ran out", the other is "you arrived the wrong way".
        # Telling an expired learner there is no ticket reads as a broken application.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Your session for this application has ended. Open it again from its range."
                if ticket
                else "No application ticket. Open this application from its range."
            ),
        )
    user_id = verify_app_code(code, range_id)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="That link has already been used or has expired. Open the application again.",
        )
    return _exchange(request, authority, range_id, user_id, uri)


def _exchange(
    request: Request, authority: str, range_id: UUID, user_id: UUID, uri: str
) -> Response:
    """Set the ticket cookie on the application's host and send the browser back without the code.

    The cookie carries no Domain, so it is host-only: one range's ticket is never offered to
    another range's application, which is what the host per range bought. The Location is
    absolute and has to be: Traefik resolves a relative one against the address it asked, which
    is this API's own service, so a relative Location would send the browser to the platform.
    It is built from the authority this request was already matched against, and from a scheme
    that is one of two words -- both arrive as headers, so neither is taken on trust.
    """
    forwarded = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
    scheme = forwarded if forwarded in ("http", "https") else svc.apps_scheme()
    response = Response(
        status_code=status.HTTP_302_FOUND,
        headers={
            "Location": f"{scheme}://{authority}{_uri_without_code(uri)}",
            "Cache-Control": "no-store",
        },
    )
    response.set_cookie(
        key=APP_COOKIE_NAME,
        value=create_app_ticket(range_id, user_id),
        path="/",
        httponly=True,
        samesite="lax",
        secure=get_settings().console_cookie_secure,
    )
    logger.info("range %s: application session handed to user %s", range_id, user_id)
    return response
