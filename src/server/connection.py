"""
WebSocket connection manager.

Tracks connected clients and provides broadcasting functionality.
"""

import asyncio
import time
from typing import Optional
from fastapi import WebSocket


class ConnectionManager:
    """Manages WebSocket connections and broadcasts."""

    def __init__(self):
        self._connections: dict[str, WebSocket] = {}  # user_id -> ws
        self._table_users: dict[str, set[str]] = {}   # table_id -> {user_ids}
        self._user_tables: dict[str, str] = {}        # user_id -> table_id
        # Per-user write lock — Starlette/uvicorn does not serialize concurrent
        # ws.send_json calls; without this, JSON frames from parallel coroutines
        # (broadcasts, ACTION_REQUEST, timer fires) interleave on the wire and
        # the client's decoder silently drops them.
        self._locks: dict[str, asyncio.Lock] = {}
        # Wall-clock of the last inbound message per user. The reaper uses this
        # to close sockets that look alive at the TCP layer but are actually
        # backgrounded/dead clients (iOS suspends sockets without sending FIN).
        self._last_seen: dict[str, float] = {}

    def _get_lock(self, user_id: str) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    async def connect(self, user_id: str, websocket: WebSocket) -> None:
        """
        Register a new connection.

        Handles reconnect by closing any existing connection for this user.
        """
        # Disconnect existing connection if any (handles reconnect)
        if user_id in self._connections:
            old_ws = self._connections[user_id]
            try:
                await old_ws.close()
            except Exception:
                pass
        self._connections[user_id] = websocket
        self._last_seen[user_id] = time.monotonic()

    def disconnect(self, user_id: str) -> None:
        """Remove a connection."""
        self._connections.pop(user_id, None)
        self._locks.pop(user_id, None)
        self._last_seen.pop(user_id, None)
        table_id = self._user_tables.pop(user_id, None)
        if table_id and table_id in self._table_users:
            self._table_users[table_id].discard(user_id)

    def mark_seen(self, user_id: str) -> None:
        """Stamp the user's last_seen timestamp. Called on inbound messages."""
        if user_id in self._connections:
            self._last_seen[user_id] = time.monotonic()

    def get_stale_users(self, idle_threshold_seconds: float) -> list[tuple[str, WebSocket]]:
        """Return (user_id, websocket) pairs whose last_seen is older than the threshold."""
        cutoff = time.monotonic() - idle_threshold_seconds
        stale: list[tuple[str, WebSocket]] = []
        for user_id, ws in self._connections.items():
            ts = self._last_seen.get(user_id)
            if ts is None or ts <= cutoff:
                stale.append((user_id, ws))
        return stale

    def join_table(self, user_id: str, table_id: str) -> None:
        """Track user joining a table for broadcasts."""
        self._user_tables[user_id] = table_id
        if table_id not in self._table_users:
            self._table_users[table_id] = set()
        self._table_users[table_id].add(user_id)

    def leave_table(self, user_id: str) -> None:
        """Track user leaving a table."""
        table_id = self._user_tables.pop(user_id, None)
        if table_id and table_id in self._table_users:
            self._table_users[table_id].discard(user_id)

    def get_user_table(self, user_id: str) -> Optional[str]:
        """Get the table_id for a user, or None if not at a table."""
        return self._user_tables.get(user_id)

    async def send_to_user(self, user_id: str, message: dict) -> bool:
        """
        Send a message to a specific user.

        Serialized per-user via an asyncio.Lock so concurrent producers
        (broadcasts, ACTION_REQUEST, timer callbacks) don't interleave frames
        on the same WebSocket. Returns True if sent successfully, False if user
        not connected.
        """
        ws = self._connections.get(user_id)
        if ws:
            lock = self._get_lock(user_id)
            async with lock:
                # Re-fetch under lock — disconnect could have run between the
                # initial read and acquiring the lock.
                ws = self._connections.get(user_id)
                if ws is None:
                    return False
                try:
                    await ws.send_json(message)
                    return True
                except Exception:
                    self.disconnect(user_id)
        return False

    async def broadcast_to_table(
        self, table_id: str, message: dict, exclude: Optional[str] = None
    ) -> None:
        """Broadcast a message to all users at a table."""
        user_ids = self._table_users.get(table_id, set()).copy()
        for user_id in user_ids:
            if user_id != exclude:
                await self.send_to_user(user_id, message)

    def get_table_users(self, table_id: str) -> set[str]:
        """Get all user_ids at a table."""
        return self._table_users.get(table_id, set()).copy()

    def is_connected(self, user_id: str) -> bool:
        """Check if a user is currently connected."""
        return user_id in self._connections
