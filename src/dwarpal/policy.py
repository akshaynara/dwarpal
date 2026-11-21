"""Business policy: what each action requires, whoever is asking, and the limits around it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

TIERS = ("open", "account", "binding", "human")
TIER_LABELS = {
    "open": "Open: anyone may ask",
    "account": "Account: needs a verified delegation",
    "binding": "Binding: needs the customer's own approval",
    "human": "Human: always decided by a person",
}


@dataclass
class IntentRule:
    name: str
    description: str
    tier: str
    scope: str | None = None
    params: list[str] = field(default_factory=list)
    summary: str = ""
    amount_param: str | None = None
    max_amount_inr: float | None = None
    response_hours: float | None = None

    @property
    def needs_passport(self) -> bool:
        return self.tier != "open"


@dataclass
class Limits:
    max_turns_per_session: int = 8
    max_cost_units_per_session: int = 20
    max_requests_per_minute: int = 20
    max_anonymous_requests_per_minute: int = 60
    max_identical_requests: int = 3
    max_denials_per_session: int = 3
    max_note_chars: int = 400


@dataclass
class StepUpMethod:
    id: str
    label: str
    strength: int
    accessible: bool


@dataclass
class StepUpPolicy:
    required_strength: int = 2
    ttl_seconds: int = 300
    methods: dict[str, StepUpMethod] = field(default_factory=dict)


@dataclass
class HandoffPolicy:
    default_response_hours: float = 24
    runaway_response_hours: float = 4
    join_code_ttl_hours: float = 72


@dataclass
class VoicePolicy:
    verified_scopes: list[str] = field(default_factory=lambda: ["appointments:read"])
    max_amount_inr: float | None = None
    ai_voice_threshold: float = 0.8
    low_attestation: list[str] = field(default_factory=lambda: ["C", "none"])
    lane_code_ttl_seconds: int = 600
    drop_fields: list[str] = field(default_factory=lambda: ["audio", "recording_url", "transcript"])


@dataclass
class DataPolicy:
    never_disclose_fields: list[str] = field(default_factory=list)
    redact_patterns: dict[str, re.Pattern[str]] = field(default_factory=dict)


@dataclass
class Policy:
    business_id: str
    business_name: str
    description: str
    contact: str
    trusted_issuers: list[dict[str, str]]
    intents: dict[str, IntentRule]
    limits: Limits
    step_up: StepUpPolicy
    handoff: HandoffPolicy
    voice: VoicePolicy
    customer_messages: dict[str, str]
    data: DataPolicy

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Policy":
        biz = raw["business"]
        intents: dict[str, IntentRule] = {}
        for name, spec in (raw.get("intents") or {}).items():
            tier = spec.get("tier")
            if tier is None:  # v0.1 policies used `mode`
                tier = "human" if spec.get("mode") == "human" else "account"
            if tier not in TIERS:
                raise ValueError(f"Intent {name}: tier must be one of {TIERS}.")
            if tier in {"account", "binding"} and not spec.get("scope"):
                raise ValueError(f"Intent {name}: {tier}-tier intents need a scope.")
            intents[name] = IntentRule(
                name=name, description=spec.get("description", ""), tier=tier, scope=spec.get("scope"),
                params=list(spec.get("params") or []), summary=spec.get("summary", name.replace("_", " ")),
                amount_param=spec.get("amount_param"), max_amount_inr=spec.get("max_amount_inr"),
                response_hours=spec.get("response_hours"),
            )
        su = raw.get("step_up") or {}
        methods = {
            mid: StepUpMethod(id=mid, label=m.get("label", mid), strength=int(m.get("strength", 1)),
                              accessible=bool(m.get("accessible", True)))
            for mid, m in (su.get("methods") or {}).items()
        }
        d = raw.get("data_policy") or {}
        return cls(
            business_id=biz["id"],
            business_name=biz.get("name", biz["id"]),
            description=biz.get("description", ""),
            contact=biz.get("contact", "the business directly"),
            trusted_issuers=list(raw.get("trusted_issuers") or []),
            intents=intents,
            limits=Limits(**(raw.get("limits") or {})),
            step_up=StepUpPolicy(required_strength=int(su.get("required_strength", 2)),
                                 ttl_seconds=int(su.get("ttl_seconds", 300)), methods=methods),
            handoff=HandoffPolicy(**(raw.get("handoff") or {})),
            voice=VoicePolicy(**(raw.get("voice") or {})),
            customer_messages=dict(raw.get("customer_messages") or {}),
            data=DataPolicy(
                never_disclose_fields=[f.lower() for f in d.get("never_disclose_fields") or []],
                redact_patterns={k: re.compile(v) for k, v in (d.get("redact_patterns") or {}).items()},
            ),
        )

    @classmethod
    def load(cls, path: Path | None = None) -> "Policy":
        if path is None:
            text = resources.files("dwarpal.policies").joinpath("sunrise_clinic.yaml").read_text(encoding="utf-8")
        else:
            text = Path(path).read_text(encoding="utf-8")
        return cls.from_dict(yaml.safe_load(text))

    def public_summary(self) -> list[dict[str, Any]]:
        return [
            {"intent": r.name, "description": r.description, "tier": r.tier, "scope": r.scope, "params": r.params}
            for r in self.intents.values()
        ]
