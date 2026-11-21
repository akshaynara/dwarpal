"""Human handoff queue.

Contested, sensitive or runaway agent sessions land here with full context. The calling agent gets
back a case id, when to expect an answer and a "don't retry before" time, so it stops calling.
The customer gets a one-time join code that connects them to the case without re-verifying, so
they never have to start the story over.
"""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

JOIN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I, easy to read out loud


@dataclass
class Ticket:
    id: str
    created_at: float
    session_id: str
    agent_id: str
    agent_name: str
    principal: dict[str, str]
    intent: str
    params: dict[str, Any]
    reason_code: str
    reason: str
    risk: str
    expected_by: float
    join_code: str
    join_code_expires: float
    history: list[dict[str, Any]] = field(default_factory=list)
    status: str = "open"  # open | resolved
    resolution: str | None = None
    resolved_at: float | None = None
    customer_joined_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EscalationQueue:
    COLL = "tickets"

    def __init__(self, store: Any = None) -> None:
        self.store = store
        self.tickets: dict[str, Ticket] = {}
        if store is not None:
            for d in store.all(self.COLL):
                t = Ticket(**d)
                self.tickets[t.id] = t

    def _save(self, t: Ticket) -> None:
        if self.store is not None:
            self.store.put(self.COLL, t.id, t.to_dict())

    def open(self, *, response_hours: float, join_ttl_hours: float, **kwargs: Any) -> Ticket:
        now = time.time()
        ticket = Ticket(
            id=f"HUM-{uuid.uuid4().hex[:6].upper()}", created_at=now, expected_by=now + response_hours * 3600,
            join_code="".join(secrets.choice(JOIN_ALPHABET) for _ in range(6)),
            join_code_expires=now + join_ttl_hours * 3600, **kwargs)
        self.tickets[ticket.id] = ticket
        self._save(ticket)
        return ticket

    def join(self, code: str, now: float | None = None) -> tuple[str, Ticket | None]:
        """Customer joins their case. Returns (code, ticket): ok | unknown | used | expired."""
        now = time.time() if now is None else now
        code = code.strip().upper()
        t = next((t for t in self.tickets.values() if t.join_code == code), None)
        if t is None:
            return "unknown", None
        if t.customer_joined_at is not None:
            return "used", t
        if now > t.join_code_expires:
            return "expired", t
        t.customer_joined_at = now
        self._save(t)
        return "ok", t

    def resolve(self, ticket_id: str, resolution: str) -> Ticket | None:
        t = self.tickets.get(ticket_id)
        if t:
            t.status, t.resolution, t.resolved_at = "resolved", resolution, time.time()
            self._save(t)
        return t

    def list(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in sorted(self.tickets.values(), key=lambda t: -t.created_at)]

    def clear(self) -> None:
        self.tickets.clear()
        if self.store is not None:
            self.store.clear(self.COLL)
