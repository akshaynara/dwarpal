"""Session guard: turn, cost, rate, loop and probing limits for agent conversations.

Two capable agents can talk forever. These limits make sure they don't, and that a person takes
over before a conversation becomes expensive or adversarial.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field

from .identity import canonical_json
from .policy import Limits


@dataclass
class SessionState:
    session_id: str
    agent_id: str
    principal_id: str
    created_at: float = field(default_factory=time.time)
    turns: int = 0
    cost_units: int = 0
    denials: int = 0
    request_counts: Counter[str] = field(default_factory=Counter)
    locked_reason: str | None = None
    ticket_id: str | None = None
    flagged: bool = False
    open_tickets: dict[str, str] = field(default_factory=dict)  # intent -> ticket id


@dataclass
class LimitBreach:
    code: str
    reason: str


class SessionGuard:
    def __init__(self, limits: Limits):
        self.limits = limits
        self.sessions: dict[str, SessionState] = {}
        self._rate: dict[str, deque[float]] = defaultdict(deque)

    def session(self, session_id: str, agent_id: str, principal_id: str) -> SessionState:
        key = f"{agent_id}::{session_id}"
        if key not in self.sessions:
            self.sessions[key] = SessionState(session_id=session_id, agent_id=agent_id, principal_id=principal_id)
        return self.sessions[key]

    def check_rate(self, agent_id: str, now: float | None = None) -> LimitBreach | None:
        now = time.time() if now is None else now
        window = self._rate[agent_id]
        while window and window[0] < now - 60:
            window.popleft()
        window.append(now)
        if len(window) > self.limits.max_requests_per_minute:
            return LimitBreach("rate_limited", f"{len(window)} requests in the last minute "
                                               f"(limit {self.limits.max_requests_per_minute}).")
        return None

    def begin_turn(self, s: SessionState, intent: str, params: dict) -> LimitBreach | None:
        """Count the turn and check limits that should hand the session to a person."""
        s.turns += 1
        fingerprint = f"{intent}:{canonical_json(params).decode()}"
        s.request_counts[fingerprint] += 1
        L = self.limits
        if s.turns > L.max_turns_per_session:
            return LimitBreach("turn_limit", f"Session reached {s.turns} turns (limit {L.max_turns_per_session}).")
        if s.cost_units >= L.max_cost_units_per_session:
            return LimitBreach("cost_limit", f"Session used {s.cost_units} cost units "
                                             f"(budget {L.max_cost_units_per_session}).")
        if s.request_counts[fingerprint] > L.max_identical_requests:
            return LimitBreach("loop_detected", f"Same request repeated {s.request_counts[fingerprint]} times: "
                                                f"agents are looping.")
        if s.denials >= L.max_denials_per_session:
            return LimitBreach("probing", f"{s.denials} requests refused in this session: possible probing.")
        return None

    def record_denial(self, s: SessionState) -> None:
        s.denials += 1

    def lock(self, s: SessionState, reason: str, ticket_id: str) -> None:
        s.locked_reason = reason
        s.ticket_id = ticket_id
