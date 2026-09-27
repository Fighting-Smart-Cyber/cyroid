# backend/proving_ground/services/event_broadcaster.py
"""
Real-time event broadcasting via Redis pub/sub.

This service enables real-time UI updates by broadcasting events
to connected WebSocket clients through Redis pub/sub.
"""
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional, Dict, Set, Any
from uuid import UUID
from datetime import datetime

import redis.asyncio as redis
from pydantic import BaseModel

from proving_ground.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Redis channel names
EVENTS_CHANNEL = "proving_ground:events"
RANGE_CHANNEL_PREFIX = "proving_ground:range:"
VM_CHANNEL_PREFIX = "proving_ground:vm:"


class RealtimeEvent(BaseModel):
    """Event payload for real-time broadcasts."""

    event_type: str
    range_id: Optional[str] = None
    vm_id: Optional[str] = None
    network_id: Optional[str] = None
    message: str
    data: Optional[Dict[str, Any]] = None
    timestamp: str

    class Config:
        json_encoders = {UUID: str, datetime: lambda v: v.isoformat()}


class EventBroadcaster:
    """
    Broadcasts events to Redis pub/sub channels.

    Events are published to:
    - proving_ground:events (all events)
    - proving_ground:range:{range_id} (range-specific events)
    - proving_ground:vm:{vm_id} (VM-specific events)
    """

    _instance: Optional["EventBroadcaster"] = None
    _redis: Optional[redis.Redis] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def connect(self) -> None:
        """Connect to Redis."""
        if self._redis is None:
            self._redis = redis.from_url(settings.redis_url, decode_responses=True)
            logger.info("EventBroadcaster connected to Redis")

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        if self._redis:
            await self._redis.close()
            self._redis = None
            logger.info("EventBroadcaster disconnected from Redis")

    async def broadcast(
        self,
        event_type: str,
        message: str,
        range_id: Optional[UUID] = None,
        vm_id: Optional[UUID] = None,
        network_id: Optional[UUID] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Broadcast an event to all relevant channels.

        Args:
            event_type: Type of event (e.g., 'vm.status_changed')
            message: Human-readable message
            range_id: Associated range ID
            vm_id: Associated VM ID
            network_id: Associated network ID
            data: Additional event data
        """
        if self._redis is None:
            await self.connect()

        event = RealtimeEvent(
            event_type=event_type,
            range_id=str(range_id) if range_id else None,
            vm_id=str(vm_id) if vm_id else None,
            network_id=str(network_id) if network_id else None,
            message=message,
            data=data,
            timestamp=datetime.utcnow().isoformat(),
        )

        payload = event.model_dump_json()

        try:
            # Publish to global events channel
            await self._redis.publish(EVENTS_CHANNEL, payload)

            # Publish to range-specific channel
            if range_id:
                channel = f"{RANGE_CHANNEL_PREFIX}{range_id}"
                await self._redis.publish(channel, payload)

            # Publish to VM-specific channel
            if vm_id:
                channel = f"{VM_CHANNEL_PREFIX}{vm_id}"
                await self._redis.publish(channel, payload)

            logger.debug(f"Broadcast event: {event_type} - {message}")

        except Exception as e:
            logger.error(f"Failed to broadcast event: {e}")


def _receives_nothing(_event: Dict[str, Any]) -> bool:
    """The default entitlement: a connection that did not say who is behind it
    receives only the channels it explicitly subscribed to."""
    return False


@dataclass
class _Connection:
    """A live socket and the rule that decides which global events reach it."""

    websocket: Any
    may_receive: Callable[[Dict[str, Any]], bool]


class ConnectionManager:
    """
    Manages WebSocket connections and their subscriptions.

    Handles:
    - Connection lifecycle (connect, disconnect)
    - Subscription management (subscribe, unsubscribe)
    - Message routing to appropriate connections
    """

    def __init__(self):
        # Map of connection_id -> _Connection
        self._connections: Dict[str, _Connection] = {}
        # Map of connection_id -> set of subscribed channels
        self._subscriptions: Dict[str, Set[str]] = {}
        # Map of channel -> set of connection_ids
        self._channel_subscribers: Dict[str, Set[str]] = {}
        # Redis pubsub instance
        self._pubsub: Optional[redis.client.PubSub] = None
        self._listener_task: Optional[asyncio.Task] = None
        self._redis: Optional[redis.Redis] = None

    async def start(self) -> None:
        """Start the connection manager and Redis listener."""
        self._redis = redis.from_url(settings.redis_url, decode_responses=True)
        self._pubsub = self._redis.pubsub()
        # Subscribe to global events channel
        await self._pubsub.subscribe(EVENTS_CHANNEL)
        # Start listener task
        self._listener_task = asyncio.create_task(self._listen())
        logger.info("ConnectionManager started")

    async def stop(self) -> None:
        """Stop the connection manager."""
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

        if self._pubsub:
            await self._pubsub.close()

        if self._redis:
            await self._redis.close()

        logger.info("ConnectionManager stopped")

    async def connect(
        self,
        connection_id: str,
        websocket: Any,
        may_receive: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> None:
        """Register a new WebSocket connection.

        `may_receive` decides, event by event, whether this connection is
        entitled to something published on the global channel. It defaults to
        refusing everything, because the alternative default is the defect this
        parameter exists to close: the global channel used to be delivered to
        every connected socket, so authorising a subscription decided nothing.
        A caller that does not say who is on the other end of the socket gets
        only the channels it asks for by name.
        """
        self._connections[connection_id] = _Connection(
            websocket=websocket,
            may_receive=may_receive or _receives_nothing,
        )
        self._subscriptions[connection_id] = set()
        logger.info(f"WebSocket connected: {connection_id}")

    async def disconnect(self, connection_id: str) -> None:
        """Remove a WebSocket connection and its subscriptions."""
        if connection_id in self._connections:
            del self._connections[connection_id]

        # Remove from all channel subscriptions
        if connection_id in self._subscriptions:
            for channel in self._subscriptions[connection_id]:
                if channel in self._channel_subscribers:
                    self._channel_subscribers[channel].discard(connection_id)
                    # Unsubscribe from channel if no more subscribers
                    if not self._channel_subscribers[channel] and self._pubsub:
                        await self._pubsub.unsubscribe(channel)
            del self._subscriptions[connection_id]

        logger.info(f"WebSocket disconnected: {connection_id}")

    async def subscribe(self, connection_id: str, channel: str) -> None:
        """Subscribe a connection to a channel."""
        if connection_id not in self._subscriptions:
            return

        self._subscriptions[connection_id].add(channel)

        if channel not in self._channel_subscribers:
            self._channel_subscribers[channel] = set()
            # Subscribe to Redis channel if new
            if self._pubsub:
                await self._pubsub.subscribe(channel)

        self._channel_subscribers[channel].add(connection_id)
        logger.debug(f"Connection {connection_id} subscribed to {channel}")

    async def unsubscribe(self, connection_id: str, channel: str) -> None:
        """Unsubscribe a connection from a channel."""
        if connection_id in self._subscriptions:
            self._subscriptions[connection_id].discard(channel)

        if channel in self._channel_subscribers:
            self._channel_subscribers[channel].discard(connection_id)
            if not self._channel_subscribers[channel] and self._pubsub:
                await self._pubsub.unsubscribe(channel)

    async def subscribe_to_range(self, connection_id: str, range_id: str) -> None:
        """Subscribe to all events for a specific range.

        The caller must have established that this connection's user may read
        the range; nothing below this line checks.
        """
        await self.subscribe(connection_id, f"{RANGE_CHANNEL_PREFIX}{range_id}")

    async def subscribe_to_vm(self, connection_id: str, vm_id: str) -> None:
        """Subscribe to all events for a specific VM, on the same terms."""
        await self.subscribe(connection_id, f"{VM_CHANNEL_PREFIX}{vm_id}")

    async def _listen(self) -> None:
        """Listen for Redis pub/sub messages and route to connections."""
        try:
            async for message in self._pubsub.listen():
                if message["type"] == "message":
                    channel = message["channel"]
                    data = message["data"]
                    await self._route_message(channel, data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Redis listener error: {e}")

    async def _route_message(self, channel: str, data: str) -> None:
        """Route a message to the connections entitled to it.

        A range or VM channel was subscribed to by name, and the endpoint
        authorised that subscription, so every subscriber on it gets the
        message. The global channel is the one that leaked: every event is
        published to it as well, and it was delivered to every connected
        socket, so a learner's notification feed carried every other cohort's
        deployment messages and every notification addressed to someone else.
        It is now filtered per connection.

        A connection already subscribed to the event's own range or VM channel
        is skipped here, because the same payload reaches it on that channel --
        without this it arrived twice.
        """
        if channel != EVENTS_CHANNEL:
            for connection_id in list(self._channel_subscribers.get(channel, set())):
                await self._send(connection_id, data)
            return

        try:
            event = json.loads(data)
        except (TypeError, ValueError):
            logger.warning("Dropping an unparseable payload on %s", EVENTS_CHANNEL)
            return

        specific = self._channels_of(event)
        for connection_id, connection in list(self._connections.items()):
            if self._subscriptions.get(connection_id, set()) & specific:
                continue
            try:
                entitled = connection.may_receive(event)
            except Exception:
                # A broken entitlement check must not become a broadcast.
                logger.exception("Entitlement check failed for %s; refusing", connection_id)
                entitled = False
            if entitled:
                await self._send(connection_id, data)

    @staticmethod
    def _channels_of(event: Dict[str, Any]) -> Set[str]:
        """The specific channels this same event was also published to."""
        channels = set()
        if event.get("range_id"):
            channels.add(f"{RANGE_CHANNEL_PREFIX}{event['range_id']}")
        if event.get("vm_id"):
            channels.add(f"{VM_CHANNEL_PREFIX}{event['vm_id']}")
        return channels

    async def _send(self, connection_id: str, data: str) -> None:
        connection = self._connections.get(connection_id)
        if connection is None:
            return
        try:
            await connection.websocket.send_text(data)
        except Exception as e:
            logger.warning(f"Failed to send to {connection_id}: {e}")


# Singleton instances
_broadcaster: Optional[EventBroadcaster] = None
_connection_manager: Optional[ConnectionManager] = None


def get_broadcaster() -> EventBroadcaster:
    """Get the singleton EventBroadcaster instance."""
    global _broadcaster
    if _broadcaster is None:
        _broadcaster = EventBroadcaster()
    return _broadcaster


def get_connection_manager() -> ConnectionManager:
    """Get the singleton ConnectionManager instance."""
    global _connection_manager
    if _connection_manager is None:
        _connection_manager = ConnectionManager()
    return _connection_manager


async def broadcast_event(
    event_type: str,
    message: str,
    range_id: Optional[UUID] = None,
    vm_id: Optional[UUID] = None,
    network_id: Optional[UUID] = None,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Convenience function to broadcast an event.

    This is the primary entry point for broadcasting events from anywhere in the codebase.
    """
    broadcaster = get_broadcaster()
    await broadcaster.broadcast(
        event_type=event_type,
        message=message,
        range_id=range_id,
        vm_id=vm_id,
        network_id=network_id,
        data=data,
    )
