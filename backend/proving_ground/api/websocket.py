# backend/proving_ground/api/websocket.py
"""WebSocket endpoints for real-time console and status updates.

A websocket carries no Authorization header -- `new WebSocket(url)` takes a URL
and nothing else -- so every route here used to take the session JWT as
`?token=`. That put the whole API credential in a URL: the browser's history,
the access log of every proxy and ingress on the way here, and a Referer if the
page ever links out. It is the same problem the VNC console ticket exists to
close, so it is closed the same way. A caller exchanges its session for a
short-lived ticket naming ONE socket (`POST /ws/ticket`), the ticket arrives as
a cookie scoped to that socket's path, and the handshake carries it on its own.
An access token is not accepted here at all; see `utils/security.py`.

Reading a ticket establishes *who* is asking and nothing else; each route still
has to answer *whether they may*, against the same rule its HTTP equivalent
uses. For a long time none of them did, and knowing a VM's id was an
interactive root shell in it. The rule is applied twice on purpose -- once when
the ticket is minted, once when the socket opens -- so that a ticket minted
before an entitlement was withdrawn does not outlive the withdrawal.

Which rule belongs where is the distinction `api/deps.py` exists to keep:
`check_console_access` for the console and VNC routes, because a console is
interactive control of a running machine; `check_resource_access` for the
status and event feeds, because those are reads. The event feed needs one more
thing than a check at subscribe time -- see `_event_entitlement`.
"""
import asyncio
import logging
import time
from typing import Any, Dict, Literal, Optional, Tuple
from uuid import UUID

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel
from starlette.websockets import WebSocketState
from sqlalchemy.orm import Session, joinedload
import websockets

from proving_ground.api.deps import CurrentUser, DBSession, check_resource_access
from proving_ground.api.vms import check_console_access
from proving_ground.database import get_db
from proving_ground.models.event import EventParticipant
from proving_ground.models.user import User
from proving_ground.models.vm import VM
from proving_ground.models.range import Range
from proving_ground.utils.security import (
    WS_TICKET_COOKIE,
    WS_TICKET_MINUTES,
    create_ws_ticket,
    verify_ws_ticket,
    ws_ticket_cookie_kwargs,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WebSocket"])

# VNC port for desktop VMs (noVNC websockify)
VNC_WEBSOCKET_PORT = 8006

# Close codes. 4001/4004 were already in use here and the Era B console uses
# 4003 for a refusal, so the three stay consistent across both substrates.
WS_UNAUTHENTICATED = 4001
WS_FORBIDDEN = 4003
WS_NOT_FOUND = 4004

# How long a global-feed entitlement decision is reused. See _event_entitlement.
ENTITLEMENT_TTL_SECONDS = 30

# The five sockets a ticket can be minted for, and the path each one lives at
# below this router's mount point. The path is built here from the kind and the
# validated resource id rather than taken from the request, because a path the
# caller supplies is a path the caller chooses: ask for a ticket scoped to
# "/ws/" and one ticket would open every socket in the API.
WsTicketKind = Literal["console", "vnc", "range-console", "status", "events"]

# The mount prefix is recovered by removing this from the minting request's own
# path, so the cookie is scoped to the path the browser will really dial --
# "/api/v1/ws/console/<id>" in the app, "/ws/console/<id>" in a test that
# mounts this router bare.
TICKET_ROUTE = "/ws/ticket"


def get_dind_docker_client(range_obj: Range):
    """Get a Docker client for a DinD range.

    Returns None if range is not a DinD deployment.
    """
    if not range_obj.dind_container_id or not range_obj.dind_docker_url:
        return None

    from proving_ground.services.dind_service import get_dind_service

    dind_service = get_dind_service()
    return dind_service.get_range_client(str(range_obj.id), range_obj.dind_docker_url)


async def get_current_user_ws(
    websocket: WebSocket, kind: str, resource_id: Optional[str], db: Session
):
    """Authenticate a websocket from its ticket cookie. Closes it if it cannot.

    Authentication only. Every caller must follow it with the authorization
    check that belongs to what it is about to hand over -- a ticket says who
    minted it, not that they are still entitled.

    The ticket is read from the cookie and never from the query string, which
    is the point of the change: a credential in a URL is a credential in the
    history, the logs and the Referer. An access token is refused outright,
    because `verify_ws_ticket` accepts only `typ == "ws"`.
    """
    user_id = verify_ws_ticket(websocket.cookies.get(WS_TICKET_COOKIE), kind, resource_id)
    if not user_id:
        # The reason travels to the browser, so it names the fix: the client
        # mints a ticket immediately before every connect, including every
        # reconnect, and a refusal here means it did not or the ticket aged out.
        await websocket.close(
            code=WS_UNAUTHENTICATED,
            reason="No valid websocket ticket -- POST /ws/ticket for this socket first",
        )
        return None

    user = db.query(User).options(joinedload(User.attributes)).filter(User.id == user_id).first()
    if not user:
        await websocket.close(code=WS_UNAUTHENTICATED, reason="User not found")
        return None

    if not user.is_active:
        # `get_current_user` refuses a deactivated account with a 403 and this
        # did not, so deactivating someone closed every HTTP route to them and
        # left the sockets open: the credential they already hold stays valid
        # until it expires, and a console is the last thing a revoked account
        # should keep.
        await websocket.close(code=WS_FORBIDDEN, reason="User account is deactivated")
        return None

    return user


class WsTicketRequest(BaseModel):
    """Which socket the caller wants to open, named by the server's own terms."""

    kind: WsTicketKind
    resource_id: Optional[UUID] = None


def _authorize_ws_ticket(
    kind: str, resource_id: Optional[UUID], user: User, db: Session
) -> Tuple[str, Optional[str]]:
    """Apply the socket's own access rule, and return (path, scoped resource).

    Every branch asks exactly what the socket asks when it opens, so a ticket
    can never be a way in through a door the handshake would have shut. It
    raises HTTPException on a refusal, so there is no value a caller could
    mistake for a pass.
    """
    if kind == "events":
        # The event feed's path names no resource: one socket multiplexes
        # whatever the caller then subscribes to, and every subscription is
        # authorised at the moment it is made. So the ticket says only who.
        return "/ws/events", None

    if resource_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"A '{kind}' ticket must name the resource it opens",
        )

    if kind in ("console", "vnc"):
        vm = db.query(VM).filter(VM.id == resource_id).first()
        if not vm:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VM not found")
        check_console_access(vm, user, db)
        return f"/ws/{kind}/{resource_id}", str(resource_id)

    range_obj = db.query(Range).filter(Range.id == resource_id).first()
    if not range_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Range not found")

    if kind == "range-console":
        # A privileged exec on the host daemon into the container holding every
        # range network and machine. Platform-operator, not range-owner; the
        # socket itself says the same thing.
        if not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required"
            )
        return f"/ws/range-console/{resource_id}", str(resource_id)

    check_resource_access("range", range_obj.id, user, db, range_obj.created_by)
    return f"/ws/status/{resource_id}", str(resource_id)


@router.post("/ws/ticket")
def issue_ws_ticket(
    body: WsTicketRequest,
    request: Request,
    response: Response,
    db: DBSession,
    current_user: CurrentUser,
):
    """Exchange an authenticated session for a ticket that opens one websocket.

    This is the only route here that takes the session, and it takes it in an
    Authorization header like every other API call. What comes back is a cookie
    scoped to the one socket path, which the handshake sends by itself -- so
    the websocket URL carries no credential and the session never reaches a
    log, a history entry or a Referer.
    """
    socket_path, scoped_to = _authorize_ws_ticket(body.kind, body.resource_id, current_user, db)

    # The router is mounted under /api/v1 in the app and bare in tests that
    # exercise it alone. Deriving the prefix from this request's own path keeps
    # the cookie's scope right in both without hard-coding either.
    mount_prefix = (
        request.url.path[: -len(TICKET_ROUTE)] if request.url.path.endswith(TICKET_ROUTE) else ""
    )

    ticket = create_ws_ticket(current_user.id, body.kind, scoped_to)
    response.set_cookie(value=ticket, **ws_ticket_cookie_kwargs(f"{mount_prefix}{socket_path}"))
    return {"expires_in": WS_TICKET_MINUTES * 60}


async def _refuse(websocket: WebSocket, exc: HTTPException) -> None:
    """Close with the reason the check gave. The protocol caps it at 123 bytes."""
    await websocket.close(code=WS_FORBIDDEN, reason=str(exc.detail)[:120])


async def require_console_access(websocket: WebSocket, vm: VM, user: User, db: Session) -> bool:
    """May this user open THIS VM's console? Closes the socket if not.

    `/ws/console` opens a shell in the machine and `/ws/vnc` proxies its
    framebuffer, so both ask exactly what `GET /vms/{id}/console-url` asks and
    get the same answer: admin, the range's owner, the learner it is assigned
    to, or a participant in its training event. The read rule is wrong here --
    it treats an untagged resource as public, and every range on a dev host is
    untagged, which entitled every non-student account to every console.
    """
    try:
        check_console_access(vm, user, db)
    except HTTPException as exc:
        await _refuse(websocket, exc)
        return False
    return True


async def require_range_read(
    websocket: WebSocket, range_obj: Range, user: User, db: Session
) -> bool:
    """May this user read this range's live state? Closes the socket if not.

    Status and events are a read, so this is the visibility rule the range's
    own GET applies -- a student assigned to the lab sees it, a stranger does
    not, and a private range stays private.
    """
    try:
        check_resource_access("range", range_obj.id, user, db, range_obj.created_by)
    except HTTPException as exc:
        await _refuse(websocket, exc)
        return False
    return True


def user_may_read_range(user_id: UUID, range_id: Any) -> bool:
    """The range read rule, asked with a session of this function's own.

    The event feed answers this long after its endpoint released its session,
    on the task that routes Redis messages, so it opens and closes one per
    decision. That cost is why the caller caches the answer.
    """
    try:
        target = UUID(str(range_id))
    except (TypeError, ValueError):
        return False

    db = next(get_db())
    try:
        user = (
            db.query(User).options(joinedload(User.attributes)).filter(User.id == user_id).first()
        )
        if user is None or not user.is_active:
            return False
        range_obj = db.query(Range).filter(Range.id == target).first()
        if range_obj is None:
            return False
        check_resource_access("range", range_obj.id, user, db, range_obj.created_by)
        return True
    except HTTPException:
        return False
    finally:
        db.close()


def user_may_read_vm(user_id: UUID, vm_id: Any) -> bool:
    """A VM has no owner of its own; whoever may read its range may read it."""
    try:
        target = UUID(str(vm_id))
    except (TypeError, ValueError):
        return False

    db = next(get_db())
    try:
        vm = db.query(VM).filter(VM.id == target).first()
        range_id = vm.range_id if vm else None
    finally:
        db.close()

    return range_id is not None and user_may_read_range(user_id, range_id)


def user_is_addressed_by_resource(user_id: UUID, resource_type: str, resource_id: Any) -> bool:
    """The resource-scoped half of a notification's audience.

    A notification says who it is for in three ways -- a user, a role, or the
    resource it concerns -- and `NotificationService._build_user_query` honours
    all three when it builds the list. The live feed has to answer the same
    question or the two disagree, which is exactly what happened to
    `notify_range_users`: the air-gap warning that an image was pulled from the
    internet reached the bell of the range's owner and never their screen.

    A range reaches whoever may read it; a training event reaches the people in
    it. Anything else is not resource-addressed.
    """
    try:
        target = UUID(str(resource_id))
    except (TypeError, ValueError):
        return False

    if resource_type == "range":
        return user_may_read_range(user_id, target)

    if resource_type != "event":
        return False

    db = next(get_db())
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None or not user.is_active:
            return False
        return (
            db.query(EventParticipant)
            .filter(EventParticipant.event_id == target, EventParticipant.user_id == user_id)
            .first()
            is not None
        )
    finally:
        db.close()


async def _deny_subscription(websocket: WebSocket, channel: str, reason: str) -> None:
    """Refuse one subscription without tearing the socket down.

    The connection is multiplexed and its other subscriptions were authorised,
    so a refused `subscribe` is a message, not a close. It is a message rather
    than silence because the client has no other way to tell a refusal from a
    range that is simply quiet.
    """
    logger.warning("Refused subscription to %s", channel)
    await websocket.send_json({"type": "subscribe_denied", "channel": channel, "reason": reason})


def _event_entitlement(user: User):
    """Build the per-event rule for one connection's share of the global feed.

    Every event is published to the global channel as well as to its range's
    own channel, and the connection manager used to deliver that channel to
    every connected socket. Authorising a subscription therefore decided
    nothing: a learner who subscribed to their own range still received every
    other range's deployment messages -- namespaces, addresses, whatever a
    capability seeded -- plus every notification addressed to someone else.

    An event that names a range answers to the range's read rule. A
    notification answers to the same three addressees the notification list
    reads -- a user, a role, or the resource it is about -- so that what a
    connection is told live and what it finds in the bell afterwards are the
    same set. An event addressed to nobody is platform-wide and goes to admins.

    Decisions are cached briefly because this runs on the single listener task
    shared by every connection, and an uncached check would put a synchronous
    query on that task for every event on every socket. The price is that a
    grant or a revocation takes up to ENTITLEMENT_TTL_SECONDS to be felt on an
    already-open feed.
    """
    user_id = user.id
    user_id_str = str(user.id)
    roles = list(user.roles)
    is_admin = user.is_admin
    cache: Dict[Tuple[str, str], Tuple[bool, float]] = {}

    def entitled_to(resource_type: str, resource_id: str) -> bool:
        key = (resource_type, resource_id)
        now = time.monotonic()
        decision = cache.get(key)
        if decision is None or decision[1] <= now:
            decision = (
                user_is_addressed_by_resource(user_id, resource_type, resource_id),
                now + ENTITLEMENT_TTL_SECONDS,
            )
            cache[key] = decision
        return decision[0]

    def may_receive(event: Dict[str, Any]) -> bool:
        range_id = event.get("range_id")
        if range_id:
            return entitled_to("range", str(range_id))

        data = event.get("data") or {}
        addressee = data.get("user_id")
        target_role = data.get("target_role")
        resource_type = data.get("resource_type")
        resource_id = data.get("resource_id")
        scoped = bool(resource_type) and bool(resource_id)

        # Naming an audience this rule cannot resolve is not the same as
        # naming none: only the second is platform-wide.
        if not (addressee or target_role or scoped):
            return is_admin
        if addressee and addressee == user_id_str:
            return True
        if target_role and target_role in roles:
            return True
        return scoped and entitled_to(resource_type, str(resource_id))

    return may_receive


@router.websocket("/ws/console/{vm_id}")
async def vm_console(
    websocket: WebSocket,
    vm_id: UUID,
):
    """
    WebSocket endpoint for VM console access.
    Provides interactive terminal to running containers.

    For DinD-isolated ranges, connects to the Docker daemon inside the DinD container.
    For non-DinD ranges (legacy), connects to the host Docker daemon.
    """
    await websocket.accept()

    # Get database session for auth and lookup only
    db = next(get_db())

    try:
        # Authenticate
        user = await get_current_user_ws(websocket, "console", str(vm_id), db)
        if not user:
            return

        # Get VM with range loaded
        vm = db.query(VM).filter(VM.id == vm_id).first()
        if not vm:
            await websocket.close(code=WS_NOT_FOUND, reason="VM not found")
            return

        if not await require_console_access(websocket, vm, user, db):
            return

        if not vm.container_id:
            await websocket.close(code=4000, reason="VM has no running container")
            return

        # Get the range to check if it's a DinD deployment
        range_obj = db.query(Range).filter(Range.id == vm.range_id).first()
        if not range_obj:
            await websocket.close(code=4000, reason="VM range not found")
            return

        # Extract what we need before releasing the session
        container_id = vm.container_id
        dind_container_id = range_obj.dind_container_id
        dind_docker_url = range_obj.dind_docker_url
        range_id_str = str(range_obj.id)

        # Release DB session - no longer needed for the streaming phase
        db.close()
        db = None

        # Get the appropriate Docker client (DinD or host)
        if dind_container_id and dind_docker_url:
            from proving_ground.services.dind_service import get_dind_service

            dind_service = get_dind_service()
            docker_client = dind_service.get_range_client(range_id_str, dind_docker_url)
            logger.debug(f"Using DinD Docker client for VM {vm_id}")
        else:
            # Non-DinD (legacy) - use host Docker client
            from proving_ground.services.docker_service import get_docker_service

            docker_client = get_docker_service().client
            logger.debug(f"Using host Docker client for VM {vm_id}")

        # Get container and create exec instance
        docker_client.containers.get(container_id)

        # Try /bin/bash first, fall back to /bin/sh
        # Use shell with login to get proper environment
        shell_cmd = [
            "/bin/sh",
            "-c",
            "if [ -x /bin/bash ]; then exec /bin/bash; else exec /bin/sh; fi",
        ]

        # Create interactive exec instance
        exec_instance = docker_client.api.exec_create(
            container_id,
            cmd=shell_cmd,
            stdin=True,
            tty=True,
            stdout=True,
            stderr=True,
        )

        # Start exec and get socket
        exec_socket = docker_client.api.exec_start(
            exec_instance["Id"],
            socket=True,
            tty=True,
        )

        # Keep socket in blocking mode but use select for async behavior
        exec_socket._sock.setblocking(False)

        # Track if connection is still alive
        connection_alive = True

        async def read_from_container():
            """Read output from container and send to WebSocket."""
            nonlocal connection_alive
            try:
                # Initial wait for shell to start
                await asyncio.sleep(0.1)

                while connection_alive:
                    try:
                        data = exec_socket._sock.recv(4096)
                        if data:
                            # Skip Docker stream header (8 bytes) if present
                            if len(data) > 8 and data[0] in (0, 1, 2):
                                data = data[8:]
                            if data:  # Check again after stripping header
                                await websocket.send_text(data.decode("utf-8", errors="replace"))
                        else:
                            # Empty data means socket closed
                            logger.info(f"Container socket closed for VM {vm_id}")
                            connection_alive = False
                            break
                    except BlockingIOError:
                        # No data available, wait a bit
                        await asyncio.sleep(0.05)
                    except OSError as e:
                        # Socket error (connection reset, etc.)
                        logger.warning(f"Socket error for VM {vm_id}: {e}")
                        connection_alive = False
                        break
            except Exception as e:
                logger.error(f"Error reading from container: {e}")
                connection_alive = False

        async def write_to_container():
            """Read input from WebSocket and send to container."""
            nonlocal connection_alive
            try:
                while connection_alive:
                    try:
                        data = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                        exec_socket._sock.send(data.encode())
                    except asyncio.TimeoutError:
                        # No input from user, continue loop
                        continue
            except WebSocketDisconnect:
                logger.info(f"WebSocket disconnected for VM {vm_id}")
                connection_alive = False
            except Exception as e:
                logger.error(f"Error writing to container: {e}")
                connection_alive = False

        # Run both tasks concurrently
        await asyncio.gather(
            read_from_container(),
            write_to_container(),
            return_exceptions=True,
        )

    except WebSocketDisconnect:
        logger.info(f"Console WebSocket disconnected for VM {vm_id}")
    except Exception as e:
        logger.error(f"Console WebSocket error for VM {vm_id}: {e}")
        await websocket.close(code=4000, reason=str(e))
    finally:
        if db is not None:
            db.close()


@router.websocket("/ws/vnc/{vm_id}")
async def vm_vnc_console(
    websocket: WebSocket,
    vm_id: UUID,
):
    """
    WebSocket proxy for VNC console access (noVNC).
    Proxies WebSocket traffic to the VM's noVNC server for graphical desktop access.
    Used for Windows VMs and Linux VMs with desktop environments.

    For DinD-isolated ranges:
        - Uses iptables DNAT port forwarding via the DinD management IP
        - VNC traffic: Traefik -> DinD mgmt IP:proxy_port -> iptables DNAT -> VM:vnc_port

    For non-DinD ranges (legacy):
        - Connects directly to the container's IP address on the host Docker network
    """
    await websocket.accept()

    db = next(get_db())
    vnc_ws = None

    try:
        # Authenticate
        user = await get_current_user_ws(websocket, "vnc", str(vm_id), db)
        if not user:
            return

        # Get VM
        vm = db.query(VM).filter(VM.id == vm_id).first()
        if not vm:
            await websocket.close(code=WS_NOT_FOUND, reason="VM not found")
            return

        if not await require_console_access(websocket, vm, user, db):
            return

        if not vm.container_id:
            await websocket.close(code=4000, reason="VM has no running container")
            return

        # Get the range to check for DinD and VNC proxy mappings
        range_obj = db.query(Range).filter(Range.id == vm.range_id).first()
        if not range_obj:
            await websocket.close(code=4000, reason="VM range not found")
            return

        # Extract what we need before releasing the session
        container_id = vm.container_id
        vnc_proxy_mappings = range_obj.vnc_proxy_mappings
        dind_docker_url = range_obj.dind_docker_url
        dind_container_id = range_obj.dind_container_id
        range_id_str = str(range_obj.id)

        # Release DB session - no longer needed for the streaming phase
        db.close()
        db = None

        # Determine VNC connection target
        vnc_host = None
        vnc_port = VNC_WEBSOCKET_PORT

        # Check if this is a DinD deployment with VNC proxy mappings
        vm_id_str = str(vm_id)
        if vnc_proxy_mappings and vm_id_str in vnc_proxy_mappings:
            # DinD deployment - use proxy mapping
            proxy_info = vnc_proxy_mappings[vm_id_str]
            vnc_host = proxy_info.get("proxy_host")
            vnc_port = proxy_info.get("proxy_port", VNC_WEBSOCKET_PORT)
            logger.debug(f"Using DinD VNC proxy for VM {vm_id}: {vnc_host}:{vnc_port}")
        elif dind_docker_url:
            # DinD deployment but no proxy mapping - get container IP from DinD
            if dind_container_id and dind_docker_url:
                from proving_ground.services.dind_service import get_dind_service

                dind_service = get_dind_service()
                dind_client = dind_service.get_range_client(range_id_str, dind_docker_url)
            else:
                dind_client = None
            if dind_client:
                try:
                    container = dind_client.containers.get(container_id)
                    # Get IP from the first network the container is attached to
                    networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
                    for _network_name, network_config in networks.items():
                        vnc_host = network_config.get("IPAddress")
                        if vnc_host:
                            break

                    # Determine VNC port based on image type
                    image_tags = container.image.tags if container.image.tags else []
                    image_name = image_tags[0] if image_tags else ""
                    if "kasmweb" in image_name:
                        vnc_port = 6901
                    elif "linuxserver/" in image_name or "lscr.io/linuxserver" in image_name:
                        vnc_port = 3000
                    elif "dockur" in image_name or "qemux" in image_name:
                        vnc_port = 8006
                    else:
                        vnc_port = 3000  # Default for containers

                    logger.debug(f"Using DinD container IP for VM {vm_id}: {vnc_host}:{vnc_port}")
                except Exception as e:
                    logger.error(f"Failed to get container info from DinD for VM {vm_id}: {e}")
                    await websocket.close(code=4000, reason="Container not found in DinD")
                    return
        else:
            # Non-DinD (legacy) - get container IP from host Docker
            from proving_ground.services.docker_service import get_docker_service

            docker = get_docker_service()

            try:
                container = docker.client.containers.get(container_id)
                # Get IP from the first network the container is attached to
                networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
                for _network_name, network_config in networks.items():
                    vnc_host = network_config.get("IPAddress")
                    if vnc_host:
                        break
            except Exception as e:
                logger.error(f"Failed to get container info for VM {vm_id}: {e}")
                await websocket.close(code=4000, reason="Container not found")
                return

        if not vnc_host:
            await websocket.close(code=4000, reason="Could not determine VNC connection target")
            return

        # Connect to the VNC WebSocket server
        vnc_url = f"ws://{vnc_host}:{vnc_port}/websockify"
        logger.info(f"Connecting to VNC at {vnc_url} for VM {vm_id}")

        try:
            vnc_ws = await websockets.connect(
                vnc_url,
                subprotocols=["binary"],
                ping_interval=None,  # Disable ping to avoid conflicts with noVNC
            )
        except Exception as e:
            logger.error(f"Failed to connect to VNC server for VM {vm_id}: {e}")
            await websocket.close(code=4000, reason=f"VNC connection failed: {str(e)}")
            return

        logger.info(f"VNC proxy established for VM {vm_id}")

        async def client_to_vnc():
            """Forward messages from client to VNC server."""
            try:
                while True:
                    data = await websocket.receive_bytes()
                    await vnc_ws.send(data)
            except WebSocketDisconnect:
                logger.info(f"Client disconnected from VNC proxy for VM {vm_id}")
            except Exception as e:
                logger.debug(f"Client->VNC error for VM {vm_id}: {e}")

        async def vnc_to_client():
            """Forward messages from VNC server to client."""
            try:
                async for message in vnc_ws:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)
            except websockets.exceptions.ConnectionClosed:
                logger.info(f"VNC server closed connection for VM {vm_id}")
            except Exception as e:
                logger.debug(f"VNC->Client error for VM {vm_id}: {e}")

        # Run both proxy tasks concurrently
        done, pending = await asyncio.wait(
            [
                asyncio.create_task(client_to_vnc()),
                asyncio.create_task(vnc_to_client()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel pending tasks
        for task in pending:
            task.cancel()

    except WebSocketDisconnect:
        logger.info(f"VNC WebSocket disconnected for VM {vm_id}")
    except Exception as e:
        logger.error(f"VNC WebSocket error for VM {vm_id}: {e}")
        try:
            await websocket.close(code=4000, reason=str(e))
        except Exception:
            pass
    finally:
        if db is not None:
            db.close()
        if vnc_ws:
            try:
                await vnc_ws.close()
            except Exception:
                pass


@router.websocket("/ws/range-console/{range_id}")
async def range_console(
    websocket: WebSocket,
    range_id: UUID,
):
    """
    WebSocket endpoint for Range Console - shell access to the DinD container.

    Provides interactive terminal to the Docker-in-Docker container that hosts
    the range's networks and VMs. Useful for:
    - Running docker commands (ps, logs, inspect)
    - Viewing network configuration (iptables, ip routes)
    - Troubleshooting range deployment issues
    - Debugging container networking

    Only available for DinD-based deployments.
    """
    await websocket.accept()

    db = next(get_db())

    try:
        # Authenticate
        user = await get_current_user_ws(websocket, "range-console", str(range_id), db)
        if not user:
            return

        # A privileged exec on the HOST daemon, into the container that holds
        # every range network and machine -- a platform-operator capability,
        # not a range-owner one, so it stays admin-only.
        #
        # It was gated on `user.role`, the deprecated column, against a role
        # name ("range_engineer") that is not in UserRole at all: the second
        # name matched nobody and the first answered from a field the rest of
        # the authorization surface stopped trusting. `is_admin` reads the ABAC
        # roles array, which is the one every other check here consults.
        if not user.is_admin:
            await websocket.close(code=WS_FORBIDDEN, reason="Administrator access required")
            return

        # Get range
        range_obj = db.query(Range).filter(Range.id == range_id).first()
        if not range_obj:
            await websocket.close(code=WS_NOT_FOUND, reason="Range not found")
            return

        # Check if range has DinD container
        if not range_obj.dind_container_id:
            await websocket.close(code=4000, reason="Range is not a DinD deployment")
            return

        # Extract what we need before releasing the session
        dind_container_id = range_obj.dind_container_id

        # Release DB session - no longer needed for the streaming phase
        db.close()
        db = None

        # Get the DinD container from the host Docker
        from proving_ground.services.docker_service import get_docker_service

        docker_service = get_docker_service()
        host_client = docker_service.client

        try:
            dind_container = host_client.containers.get(dind_container_id)
        except Exception as e:
            await websocket.close(code=4000, reason=f"DinD container not found: {e}")
            return

        # Check container is running
        if dind_container.status != "running":
            await websocket.close(code=4000, reason="DinD container is not running")
            return

        # Create interactive shell exec in the DinD container
        shell_cmd = [
            "/bin/sh",
            "-c",
            "if [ -x /bin/bash ]; then exec /bin/bash; else exec /bin/sh; fi",
        ]

        exec_instance = host_client.api.exec_create(
            dind_container.id,
            cmd=shell_cmd,
            stdin=True,
            tty=True,
            stdout=True,
            stderr=True,
            privileged=True,  # Allow full access for diagnostics
        )

        # Start exec and get socket
        exec_socket = host_client.api.exec_start(
            exec_instance["Id"],
            socket=True,
            tty=True,
        )

        exec_socket._sock.setblocking(False)

        connection_alive = True

        async def read_from_container():
            """Read output from DinD container and send to WebSocket."""
            nonlocal connection_alive
            try:
                await asyncio.sleep(0.1)  # Wait for shell to start

                while connection_alive:
                    try:
                        data = exec_socket._sock.recv(4096)
                        if data:
                            # Skip Docker stream header (8 bytes) if present
                            if len(data) > 8 and data[0] in (0, 1, 2):
                                data = data[8:]
                            if data:
                                await websocket.send_text(data.decode("utf-8", errors="replace"))
                        else:
                            logger.info(f"DinD container socket closed for range {range_id}")
                            connection_alive = False
                            break
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
                    except OSError as e:
                        logger.warning(f"Socket error for range {range_id}: {e}")
                        connection_alive = False
                        break
            except Exception as e:
                logger.error(f"Error reading from DinD container: {e}")
                connection_alive = False

        async def write_to_container():
            """Read input from WebSocket and send to DinD container."""
            nonlocal connection_alive
            try:
                while connection_alive:
                    try:
                        data = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                        exec_socket._sock.send(data.encode())
                    except asyncio.TimeoutError:
                        continue
            except WebSocketDisconnect:
                logger.info(f"Range console WebSocket disconnected for range {range_id}")
                connection_alive = False
            except Exception as e:
                logger.error(f"Error writing to DinD container: {e}")
                connection_alive = False

        # Run both tasks concurrently
        await asyncio.gather(
            read_from_container(),
            write_to_container(),
            return_exceptions=True,
        )

    except WebSocketDisconnect:
        logger.info(f"Range console WebSocket disconnected for range {range_id}")
    except Exception as e:
        logger.error(f"Range console WebSocket error for range {range_id}: {e}")
        try:
            await websocket.close(code=4000, reason=str(e))
        except Exception:
            pass
    finally:
        if db is not None:
            db.close()


@router.websocket("/ws/status/{range_id}")
async def range_status(
    websocket: WebSocket,
    range_id: UUID,
):
    """
    WebSocket endpoint for range status updates.
    Combines periodic polling with real-time event notifications.
    """
    await websocket.accept()

    db = next(get_db())
    connection_id = f"status_{range_id}_{id(websocket)}"

    try:
        user = await get_current_user_ws(websocket, "status", str(range_id), db)
        if not user:
            return

        from proving_ground.models.range import Range
        from proving_ground.services.event_broadcaster import get_connection_manager

        range_obj = db.query(Range).filter(Range.id == range_id).first()
        if not range_obj:
            await websocket.close(code=WS_NOT_FOUND, reason="Range not found")
            return

        if not await require_range_read(websocket, range_obj, user, db):
            return

        # Registered without an entitlement rule: this feed is one range, and
        # the subscription below is the only thing it should ever receive.
        connection_manager = get_connection_manager()
        await connection_manager.connect(connection_id, websocket)
        await connection_manager.subscribe_to_range(connection_id, str(range_id))

        # Send initial status immediately
        vms = db.query(VM).filter(VM.range_id == range_id).all()
        current_status = {str(vm.id): vm.status.value for vm in vms}
        await websocket.send_json(
            {
                "type": "status_update",
                "range_id": str(range_id),
                "range_status": range_obj.status.value,
                "vms": current_status,
            }
        )

        # Poll for status updates (as backup to real-time events)
        last_status = current_status.copy()
        while True:
            # Refresh data
            db.expire_all()
            vms = db.query(VM).filter(VM.range_id == range_id).all()
            current_status = {str(vm.id): vm.status.value for vm in vms}
            db.refresh(range_obj)

            # Check for changes
            if current_status != last_status:
                await websocket.send_json(
                    {
                        "type": "status_update",
                        "range_id": str(range_id),
                        "range_status": range_obj.status.value,
                        "vms": current_status,
                    }
                )
                last_status = current_status.copy()

            await asyncio.sleep(3)  # Poll every 3 seconds as backup

    except WebSocketDisconnect:
        logger.info(f"Status WebSocket disconnected for range {range_id}")
    except Exception as e:
        logger.error(f"Status WebSocket error for range {range_id}: {e}")
        try:
            await websocket.close(code=4000, reason=str(e))
        except Exception:
            pass
    finally:
        # Clean up connection
        try:
            connection_manager = get_connection_manager()
            await connection_manager.disconnect(connection_id)
        except Exception:
            pass
        db.close()


@router.websocket("/ws/events")
async def system_events(
    websocket: WebSocket,
    range_id: Optional[UUID] = Query(None),
):
    """
    WebSocket endpoint for real-time event streaming.

    Broadcasts deployment progress, VM status changes, errors, and notifications.
    Optionally filter by range_id to receive only events for a specific range.

    Message types received:
    - Event broadcasts (event_type, message, range_id, vm_id, data, timestamp)
    - Ping messages (type: "ping") for keepalive

    Client can send:
    - {"action": "subscribe", "range_id": "uuid"} - Subscribe to range events
    - {"action": "unsubscribe", "range_id": "uuid"} - Unsubscribe from range
    - {"action": "subscribe_vm", "vm_id": "uuid"} - Subscribe to VM events
    """
    await websocket.accept()

    db = next(get_db())
    connection_id = f"events_{id(websocket)}"

    try:
        user = await get_current_user_ws(websocket, "events", None, db)
        if not user:
            return

        # The subscription the URL asks for is settled before the socket joins
        # the feed at all: a caller refused their range never becomes a
        # recipient of anything.
        if range_id:
            range_obj = db.query(Range).filter(Range.id == range_id).first()
            if not range_obj:
                await websocket.close(code=WS_NOT_FOUND, reason="Range not found")
                return
            if not await require_range_read(websocket, range_obj, user, db):
                return

        user_id = user.id
        # A socket that named a range is that range's feed and takes nothing
        # from the global channel. A socket that named none is the
        # notification feed, and gets the share of the global channel its user
        # is entitled to.
        may_receive = None if range_id else _event_entitlement(user)

        # Release DB session - only needed for auth
        db.close()
        db = None

        from proving_ground.services.event_broadcaster import (
            get_connection_manager,
            RANGE_CHANNEL_PREFIX,
        )

        connection_manager = get_connection_manager()
        await connection_manager.connect(connection_id, websocket, may_receive)

        # If range_id specified, subscribe to that range
        if range_id:
            await connection_manager.subscribe_to_range(connection_id, str(range_id))

        # Send connected confirmation
        await websocket.send_json(
            {
                "type": "connected",
                "message": "Real-time events connected",
                "subscriptions": [f"range:{range_id}"] if range_id else [],
            }
        )

        # Listen for client messages (subscription changes) and keep alive
        while True:
            # Check if connection is still open
            if websocket.client_state != WebSocketState.CONNECTED:
                logger.debug(f"Events WebSocket {connection_id}: client disconnected")
                break

            try:
                # Wait for client message with timeout
                data = await asyncio.wait_for(websocket.receive_json(), timeout=30.0)

                action = data.get("action")

                if action == "subscribe" and "range_id" in data:
                    # Every subscribe is a fresh authorization decision. The
                    # socket asking is authenticated, which says nothing about
                    # the range it just named.
                    if user_may_read_range(user_id, data["range_id"]):
                        await connection_manager.subscribe_to_range(connection_id, data["range_id"])
                        await websocket.send_json(
                            {"type": "subscribed", "channel": f"range:{data['range_id']}"}
                        )
                    else:
                        await _deny_subscription(
                            websocket, f"range:{data['range_id']}", "You cannot read this range"
                        )

                elif action == "unsubscribe" and "range_id" in data:
                    channel = f"{RANGE_CHANNEL_PREFIX}{data['range_id']}"
                    await connection_manager.unsubscribe(connection_id, channel)
                    await websocket.send_json(
                        {"type": "unsubscribed", "channel": f"range:{data['range_id']}"}
                    )

                elif action == "subscribe_vm" and "vm_id" in data:
                    if user_may_read_vm(user_id, data["vm_id"]):
                        await connection_manager.subscribe_to_vm(connection_id, data["vm_id"])
                        await websocket.send_json(
                            {"type": "subscribed", "channel": f"vm:{data['vm_id']}"}
                        )
                    else:
                        await _deny_subscription(
                            websocket, f"vm:{data['vm_id']}", "You cannot read this VM's range"
                        )

                elif action == "ping":
                    await websocket.send_json({"type": "pong"})

            except asyncio.TimeoutError:
                # Send keepalive ping if still connected
                if websocket.client_state == WebSocketState.CONNECTED:
                    try:
                        await websocket.send_json({"type": "ping"})
                    except Exception:
                        # Connection closed during send, exit loop
                        break

    except WebSocketDisconnect:
        logger.debug(f"Events WebSocket disconnected: {connection_id}")
    except RuntimeError as e:
        # Handle "Cannot call receive while another coroutine is waiting"
        # This happens during rapid reconnection
        if "another coroutine" in str(e).lower():
            logger.debug(f"Events WebSocket {connection_id}: concurrent receive, closing")
        else:
            logger.warning(f"Events WebSocket runtime error ({type(e).__name__}): {e}")
    except Exception as e:
        # Log with exception type for better debugging
        error_msg = str(e) if str(e) else type(e).__name__
        logger.warning(f"Events WebSocket error ({type(e).__name__}): {error_msg}")
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close(code=4000, reason=error_msg[:120])
        except Exception:
            pass
    finally:
        # Clean up connection
        try:
            connection_manager = get_connection_manager()
            await connection_manager.disconnect(connection_id)
        except Exception:
            pass
        if db is not None:
            db.close()
