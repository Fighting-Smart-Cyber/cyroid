"""The gateway's request path: decide, strip, proxy.

Per request, in this order, because the order is the security property:

  1. Which range? From the Host header, fail closed.
  2. Which application, and what path should it see? From the URI, fail closed.
  3. Is this caller entitled? From the ticket cookie, or from a one-use handoff code which is
     exchanged for one.
  4. Only then, proxy -- to the capability's Service in the range's own namespace.

Step 3 before step 4 is what the ForwardAuth middleware did. Doing it in one process removes
the ordering hazard rather than relocating it: there is no chain to mis-order.

Two things happen here that a Traefik middleware chain could not express at all:

  * The ticket cookie is REMOVED before the request is proxied. The application is third party
    at best and hostile by design at worst, and `HttpOnly` only stops its JavaScript reading the
    cookie -- it does nothing about its server reading it off the wire. It has no use for the
    platform's credential.
  * Nothing else of the platform's is forwarded either. The upstream sees the learner's request
    and the headers a proxy must add, and that is all.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Optional
from uuid import UUID

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route as StarletteRoute

from proving_ground.gateway.routing import RouteTable, range_id_from_host, split_app_path
from proving_ground.utils import app_tokens

logger = logging.getLogger(__name__)

# Headers that describe one hop and must not be copied onto the next.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


class GatewayConfig:
    """What the gateway is told about the install it serves."""

    def __init__(self, *, apps_host: str, path_prefix: str, secure_cookie: bool = True) -> None:
        self.apps_host = apps_host
        self.path_prefix = path_prefix.rstrip("/")
        self.secure_cookie = secure_cookie


def _authority(request: Request) -> str:
    """The name the browser actually asked for.

    `X-Forwarded-Host` first: the gateway sits behind an ingress controller which rewrites Host
    to the backend service. The port is kept, because the handoff redirects to this same
    authority and an install reached on a non-default port must not be sent to the default one.
    """
    forwarded = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    return (forwarded or request.headers.get("host", "")).strip().lower()


def _scheme(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return forwarded if forwarded in ("http", "https") else request.url.scheme


def _proxy_headers(request: Request) -> dict[str, str]:
    """The learner's headers, minus the hop-by-hop ones and minus the platform's cookie."""
    out: dict[str, str] = {}
    for name, value in request.headers.items():
        lower = name.lower()
        if lower in _HOP_BY_HOP:
            continue
        if lower == "cookie":
            value = _cookies_without_ticket(value)
            if not value:
                continue
        out[name] = value
    return out


def _cookies_without_ticket(header: str) -> str:
    """The Cookie header with the platform's app ticket taken out.

    Impossible to express as Traefik middleware, and worth doing: the application has no use for
    the platform's credential and should never be handed it.
    """
    kept = [
        part
        for part in (p.strip() for p in header.split(";"))
        if part and not part.startswith(f"{app_tokens.APP_COOKIE_NAME}=")
    ]
    return "; ".join(kept)


def _refuse(message: str, status_code: int = 403) -> Response:
    return PlainTextResponse(
        message, status_code=status_code, headers={"Cache-Control": "no-store"}
    )


def _hand_over(request: Request, range_id: UUID, user_id: UUID, config: GatewayConfig) -> Response:
    """Exchange a one-use code for the ticket cookie, then redirect to the same URI without it.

    Absolute Location, built from the authority this request was matched against, so the browser
    is sent back to the range's own host rather than anywhere else.
    """
    uri = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    response = Response(
        status_code=302,
        headers={
            "Location": f"{_scheme(request)}://{_authority(request)}{app_tokens.uri_without_code(uri)}",
            "Cache-Control": "no-store",
        },
    )
    response.set_cookie(
        key=app_tokens.APP_COOKIE_NAME,
        value=app_tokens.create_app_ticket(range_id, user_id),
        path="/",
        httponly=True,
        samesite="lax",
        secure=config.secure_cookie,
    )
    logger.info("range %s: application session handed to user %s", range_id, user_id)
    return response


def _entitled(request: Request, range_id: UUID) -> Optional[UUID]:
    ticket = request.cookies.get(app_tokens.APP_COOKIE_NAME)
    return app_tokens.verify_app_ticket(ticket, range_id) if ticket else None


def create_app(config: GatewayConfig, routes: RouteTable, client: httpx.AsyncClient) -> Starlette:
    """The gateway, given what it serves, where things are, and what to proxy with.

    If the table maintains itself -- the Ingress-backed one refreshes on a timer -- it is
    started and stopped with the application, so a pod that is serving always has a table that
    is being kept current, and one that is shutting down is not left with a task behind it.
    """

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        start = getattr(routes, "start", None)
        if start is not None:
            await start()
        try:
            yield
        finally:
            stop = getattr(routes, "stop", None)
            if stop is not None:
                await stop()

    async def handle(request: Request) -> Response:
        range_id = range_id_from_host(_authority(request), config.apps_host)
        if range_id is None:
            return _refuse("This address does not name a range on this install.", 404)

        split = split_app_path(request.url.path, config.path_prefix)
        if split is None:
            return _refuse("This address does not name an application.", 404)
        app, downstream = split

        route = routes.lookup(range_id, app)
        if route is None:
            return _refuse("No such application in this range.", 404)

        # A handoff code is offered before the cookie is considered: it is how the first request
        # to a range's own host gets a cookie at all, since the platform cannot set one for it.
        uri = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        code = app_tokens.code_in(uri)
        if code:
            user_id = app_tokens.verify_app_code(code, range_id)
            if user_id is None:
                return _refuse(
                    "This link has been used already or has expired. Open the application "
                    "again from the range.",
                    403,
                )
            return _hand_over(request, range_id, user_id, config)

        if _entitled(request, range_id) is None:
            return _refuse("This application needs a session. Open it again from the range.", 401)

        return await _proxy(request, route, downstream, client)

    return Starlette(
        routes=[StarletteRoute("/{path:path}", handle, methods=None)], lifespan=lifespan
    )


async def _proxy(request: Request, route, downstream: str, client: httpx.AsyncClient) -> Response:
    url = httpx.URL(
        scheme="http",
        host=route.host,
        port=route.port,
        path=downstream,
        query=request.url.query.encode() if request.url.query else b"",
    )
    upstream = client.build_request(
        request.method,
        url,
        headers=_proxy_headers(request),
        content=request.stream(),
    )
    try:
        response = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        logger.warning("upstream %s:%s failed: %s", route.host, route.port, exc)
        return _refuse("This application is not answering.", 502)

    headers = {k: v for k, v in response.headers.items() if k.lower() not in _HOP_BY_HOP}
    return StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        headers=headers,
        background=BackgroundTask(response.aclose),
    )
