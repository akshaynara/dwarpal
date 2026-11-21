"""Customer step-up for binding actions.

A binding action (cancelling, refunding) needs the patient's own approval, not just a passport.
The flow is modelled on OpenID CIBA (Client-Initiated Backchannel Authentication):

  1. The agent sends the request. The gateway checks everything else, then opens a *challenge*
     bound to the exact action: patient, agent, intent and parameters, hashed together.
  2. The patient approves on their own device (here, the dashboard's phone card).
  3. The agent resends the same request with the challenge id. The gateway checks that the
     challenge is approved, unexpired, unused and covers *this* action, then lets it through once.

A delegated agent can't pass step-up by itself, so anything binding pulls the real customer back in.
An agent pretending to be human gains nothing either: the same approval is required.

Approval methods have strengths, set in policy. A voiceprint is capped at 1 in code, whatever the
policy file says, because a clean 8-12 second sample is enough to clone a voice.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .identity import body_hash
from .policy import StepUpPolicy

VOICE_MAX_STRENGTH = 1


def action_hash(principal_id: str, agent_id: str, intent: str, params: dict[str, Any]) -> str:
    return body_hash({"principal": principal_id, "agent": agent_id, "intent": intent, "params": params})


@dataclass
class Challenge:
    id: str
    created_at: float
    expires_at: float
    agent_id: str
    agent_name: str
    principal_id: str
    principal_name: str
    intent: str
    params: dict[str, Any]
    summary: str
    action_hash: str
    required_strength: int
    status: str = "pending"  # pending | approved | declined | used | expired | superseded
    factors: list[str] = field(default_factory=list)
    strength: int = 0
    approved_at: float | None = None
    used_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StepUpService:
    COLL = "step_ups"

    def __init__(self, policy: StepUpPolicy, store: Any = None):
        self.policy = policy
        self.store = store
        self.challenges: dict[str, Challenge] = {}
        if store is not None:
            for d in store.all(self.COLL):
                c = Challenge(**d)
                self.challenges[c.id] = c

    def _save(self, c: Challenge) -> None:
        if self.store is not None:
            self.store.put(self.COLL, c.id, c.to_dict())

    def method_strength(self, method_id: str) -> int:
        m = self.policy.methods.get(method_id)
        if not m:
            return 0
        return min(m.strength, VOICE_MAX_STRENGTH) if method_id.startswith("voice") else m.strength

    def create(self, *, agent_id: str, agent_name: str, principal_id: str, principal_name: str, intent: str,
               params: dict[str, Any], summary: str, now: float | None = None) -> Challenge:
        now = time.time() if now is None else now
        c = Challenge(
            id=f"SU-{secrets.token_hex(4).upper()}", created_at=now, expires_at=now + self.policy.ttl_seconds,
            agent_id=agent_id, agent_name=agent_name, principal_id=principal_id, principal_name=principal_name,
            intent=intent, params=dict(params), summary=summary,
            action_hash=action_hash(principal_id, agent_id, intent, params),
            required_strength=self.policy.required_strength,
        )
        # A newer request for the same action replaces an older pending one, so the customer sees one prompt.
        for old in self.challenges.values():
            if old.status == "pending" and old.agent_id == agent_id and old.action_hash == c.action_hash:
                old.status = "superseded"
                self._save(old)
        self.challenges[c.id] = c
        self._save(c)
        return c

    def _expire(self, c: Challenge, now: float) -> None:
        if c.status in {"pending", "approved"} and now > c.expires_at:
            c.status = "expired"
            self._save(c)

    def approve(self, challenge_id: str, methods: list[str], now: float | None = None) -> tuple[Challenge, str]:
        """Record the factors the patient completed. Returns the challenge and a plain-language result."""
        now = time.time() if now is None else now
        c = self.challenges.get(challenge_id)
        if c is None:
            raise KeyError(challenge_id)
        self._expire(c, now)
        if c.status != "pending":
            return c, f"This request is already {c.status}."
        for m in methods:
            if m in self.policy.methods and m not in c.factors:
                c.factors.append(m)
        c.strength = sum(self.method_strength(m) for m in c.factors)
        if c.strength >= c.required_strength:
            c.status, c.approved_at = "approved", now
            self._save(c)
            return c, "Approved."
        self._save(c)
        missing = c.required_strength - c.strength
        return c, (f"Not enough on its own: strength {c.strength} of {c.required_strength}. "
                   f"Add another factor worth {missing}, such as an SMS code or the keypad PIN.")

    def decline(self, challenge_id: str) -> Challenge:
        c = self.challenges[challenge_id]
        if c.status == "pending":
            c.status = "declined"
            self._save(c)
        return c

    def check(self, challenge_id: str, *, principal_id: str, agent_id: str, intent: str,
              params: dict[str, Any], now: float | None = None) -> tuple[str, Challenge | None]:
        """Returns (code, challenge). code: ok | unknown | mismatch | pending | declined | expired | used."""
        now = time.time() if now is None else now
        c = self.challenges.get(challenge_id)
        if c is None:
            return "unknown", None
        self._expire(c, now)
        if c.action_hash != action_hash(principal_id, agent_id, intent, params):
            return "mismatch", c
        if c.status == "approved":
            return "ok", c
        return c.status, c

    def supersede(self, agent_id: str) -> list[str]:
        """Withdraw pending approval requests from an agent (e.g. it switched from the phone to the agent lane),
        so the customer isn't left with stale prompts."""
        ids = []
        for c in self.challenges.values():
            if c.agent_id == agent_id and c.status == "pending":
                c.status = "superseded"
                self._save(c)
                ids.append(c.id)
        return ids

    def consume(self, c: Challenge, request_id: str) -> None:
        c.status, c.used_by = "used", request_id
        self._save(c)

    def list(self) -> list[dict[str, Any]]:
        now = time.time()
        for c in self.challenges.values():
            self._expire(c, now)
        return [c.to_dict() for c in sorted(self.challenges.values(), key=lambda c: -c.created_at)]

    def methods_public(self) -> list[dict[str, Any]]:
        return [{"id": m.id, "label": m.label, "strength": self.method_strength(m.id), "accessible": m.accessible}
                for m in self.policy.methods.values()]

    def clear(self) -> None:
        self.challenges.clear()
        if self.store is not None:
            self.store.clear(self.COLL)
