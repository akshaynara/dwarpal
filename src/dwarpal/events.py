"""In-process pub/sub so the dashboard can stream gateway decisions live (Server-Sent Events)."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any


class EventBus:
    def __init__(self, history: int = 200):
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.history: deque[dict[str, Any]] = deque(maxlen=history)

    def publish(self, event: dict[str, Any]) -> None:
        self.history.append(event)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)
