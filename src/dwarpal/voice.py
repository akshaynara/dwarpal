"""The phone lane: calls arriving through the business's own voice platform.

Voice stays the default channel for years. Nobody should be forced onto A2A, so Dwarpal applies the
same actions, tiers and checks to a phone call. The business's voice platform (an IVR or a CX Agent
Studio agent) calls the gateway on each turn with what it knows: the intent its NLU recognised,
whether it verified the patient, and *signals* about the call.

Signals restrict, proof grants
------------------------------
Telling humans from agents is probabilistic, and some AI voices are people speaking for themselves
(speech-generating devices). So no signal ever unlocks an action, and no signal ever blocks a
caller. Signals can only add friction, and every friction path can be passed without speaking:

  * AI voice detected, not declared   -> account-tier actions also need step-up
  * caller declares it is an AI agent -> it's offered the agent lane (bring a passport, go fast);
                                         on the call, account-tier actions need step-up. Telling
                                         the truth never costs more than being caught lying.
  * carrier attestation weak/missing  -> account-tier writes need step-up

A strong signal (attestation "A", a low AI-voice score) never skips anything either.

Scores only: Dwarpal takes numeric scores from detection vendors and never stores audio or
transcripts, even when the platform sends them.

The lane switch ("press 1 if you're a robot")
--------------------------------------------
A declared agent gets a short lane code. It connects over A2A with its own passport plus the code,
and the gateway links the two sessions so context carries over. The code links context; it never
grants anything. The passport does, and its patient must match the one verified on the call.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .policy import VoicePolicy

LANE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SPOKEN = {"A": "A", "B": "B", "C": "C", "D": "D", "E": "E", "F": "F", "G": "G", "H": "H", "J": "J", "K": "K",
          "L": "L", "M": "M", "N": "N", "P": "P", "Q": "Q", "R": "R", "S": "S", "T": "T", "U": "U", "V": "V",
          "W": "W", "X": "X", "Y": "Y", "Z": "Z", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six",
          "7": "seven", "8": "eight", "9": "nine"}


@dataclass
class VoiceContext:
    """What the business's voice platform tells the gateway about a call. Never audio, never transcripts."""
    call_id: str
    attestation: str = "none"           # STIR/SHAKEN level from the carrier: A | B | C | none
    ai_voice_score: float | None = None  # 0..1 from an AI-voice / deepfake detection vendor
    declared_agent: bool = False         # caller pressed 1, or said it is an AI agent
    verified_principal: str | None = None  # patient id the IVR verified, if any
    verified_name: str | None = None
    verification_method: str | None = None  # e.g. "patient_id+dob"
    dropped_fields: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("verified_name", None)
        return d


@dataclass
class Restriction:
    code: str      # ai_voice_undisclosed | declared_agent | low_attestation
    applies_to: str  # account | account_write
    detail: str


def evaluate_signals(v: VoiceContext, policy: VoicePolicy) -> list[Restriction]:
    out: list[Restriction] = []
    if v.declared_agent:
        out.append(Restriction("declared_agent", "account",
                               "Caller says it's an AI agent. On the call, account-tier actions need the patient's "
                               "step-up; with a passport it can switch to the agent lane instead."))
    elif v.ai_voice_score is not None and v.ai_voice_score >= policy.ai_voice_threshold:
        out.append(Restriction("ai_voice_undisclosed", "account",
                               f"AI-voice score {v.ai_voice_score:.2f} (threshold {policy.ai_voice_threshold}) and "
                               "not declared. Account-tier actions need step-up. This adds friction only: it "
                               "never blocks, and step-up works without speaking."))
    if (v.attestation or "none") in policy.low_attestation:
        out.append(Restriction("low_attestation", "account_write",
                               f"Carrier attestation “{v.attestation}”: the number may be spoofed. "
                               "Account-tier changes need step-up."))
    return out


def restriction_applies(r: Restriction, tier: str, scope: str | None) -> bool:
    if tier != "account":
        return False
    if r.applies_to == "account":
        return True
    return r.applies_to == "account_write" and bool(scope) and scope.endswith(":write")


def describe_signals(v: VoiceContext) -> str:
    parts = [f"carrier attestation {v.attestation or 'none'}"]
    parts.append("AI-voice score " + (f"{v.ai_voice_score:.2f}" if v.ai_voice_score is not None else "not provided"))
    parts.append("caller declared it's an AI agent" if v.declared_agent else "no AI declaration")
    return ", ".join(parts)


def sanitise(payload: dict[str, Any], policy: VoicePolicy) -> tuple[dict[str, Any], list[str]]:
    """Drop anything the gateway must never hold (audio, transcripts), recursively."""
    dropped: list[str] = []

    def walk(v: Any, path: str) -> Any:
        if isinstance(v, dict):
            out = {}
            for k, sub in v.items():
                if k.lower() in policy.drop_fields:
                    dropped.append(f"{path}.{k}" if path else k)
                    continue
                out[k] = walk(sub, f"{path}.{k}" if path else k)
            return out
        if isinstance(v, list):
            return [walk(x, path) for x in v]
        return v

    return walk(payload, ""), dropped


# --------------------------------------------------------------------------- lane codes


@dataclass
class LaneCode:
    code: str
    call_id: str
    principal_id: str
    principal_name: str
    created_at: float
    expires_at: float
    pending: dict[str, Any] | None = None   # the request the call was on when the code was offered
    earlier: list[dict[str, Any]] = field(default_factory=list)
    used_by: str | None = None
    used_at: float | None = None

    @property
    def spoken(self) -> str:
        return ", ".join(SPOKEN.get(ch, ch) for ch in self.code)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["spoken"] = self.spoken
        return d


class LaneService:
    def __init__(self, policy: VoicePolicy):
        self.policy = policy
        self.codes: dict[str, LaneCode] = {}

    def offer(self, *, call_id: str, principal_id: str, principal_name: str, pending: dict[str, Any] | None,
              earlier: list[dict[str, Any]], now: float | None = None) -> LaneCode:
        now = time.time() if now is None else now
        # One live code per call: re-offering returns the same code.
        for c in self.codes.values():
            if c.call_id == call_id and c.used_by is None and now <= c.expires_at:
                c.pending = pending or c.pending
                return c
        code = "".join(secrets.choice(LANE_ALPHABET) for _ in range(6))
        lc = LaneCode(code=code, call_id=call_id, principal_id=principal_id, principal_name=principal_name,
                      created_at=now, expires_at=now + self.policy.lane_code_ttl_seconds, pending=pending,
                      earlier=list(earlier)[-10:])
        self.codes[code] = lc
        return lc

    def redeem(self, code: str, *, principal_id: str, agent_id: str,
               now: float | None = None) -> tuple[str, LaneCode | None]:
        """Returns (result, lane): ok | unknown | used | expired | mismatch."""
        now = time.time() if now is None else now
        lc = self.codes.get(code.strip().upper())
        if lc is None:
            return "unknown", None
        if lc.used_by is not None:
            return "used", lc
        if now > lc.expires_at:
            return "expired", lc
        if lc.principal_id and lc.principal_id != principal_id:
            return "mismatch", lc
        lc.used_by, lc.used_at = agent_id, now
        return "ok", lc

    def list(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in sorted(self.codes.values(), key=lambda c: -c.created_at)]

    def clear(self) -> None:
        self.codes.clear()
