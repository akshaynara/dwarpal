"""Human handoff queue. Contested, sensitive or runaway agent sessions land here with full context."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


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
    history: list[dict[str, Any]] = field(default_factory=list)
    status: str = "open"  # open | resolved
    resolution: str | None = None
    resolved_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EscalationQueue:
    def __init__(self) -> None:
        self.tickets: dict[str, Ticket] = {}

    def open(self, **kwargs: Any) -> Ticket:
        ticket = Ticket(id=f"HUM-{uuid.uuid4().hex[:6].upper()}", created_at=time.time(), **kwargs)
        self.tickets[ticket.id] = ticket
        return ticket

    def resolve(self, ticket_id: str, resolution: str) -> Ticket | None:
        t = self.tickets.get(ticket_id)
        if t:
            t.status, t.resolution, t.resolved_at = "resolved", resolution, time.time()
        return t

    def list(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in sorted(self.tickets.values(), key=lambda t: -t.created_at)]

    def clear(self) -> None:
        self.tickets.clear()
