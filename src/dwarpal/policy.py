"""Business policy: what callers' agents may ask for, and the limits around it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml


@dataclass
class IntentRule:
    name: str
    description: str
    scope: str
    mode: str  # "a2a" | "human"
    risk: str = "low"
    params: list[str] = field(default_factory=list)
    amount_param: str | None = None
    max_amount_inr: float | None = None


@dataclass
class Limits:
    max_turns_per_session: int = 8
    max_cost_units_per_session: int = 20
    max_requests_per_minute: int = 20
    max_identical_requests: int = 3
    max_denials_per_session: int = 3
    max_note_chars: int = 400


@dataclass
class DataPolicy:
    never_disclose_fields: list[str] = field(default_factory=list)
    redact_patterns: dict[str, re.Pattern[str]] = field(default_factory=dict)


@dataclass
class Policy:
    business_id: str
    business_name: str
    description: str
    trusted_issuers: list[dict[str, str]]
    intents: dict[str, IntentRule]
    limits: Limits
    data: DataPolicy

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Policy":
        biz = raw["business"]
        intents = {
            name: IntentRule(
                name=name,
                description=spec.get("description", ""),
                scope=spec["scope"],
                mode=spec.get("mode", "a2a"),
                risk=spec.get("risk", "low"),
                params=list(spec.get("params") or []),
                amount_param=spec.get("amount_param"),
                max_amount_inr=spec.get("max_amount_inr"),
            )
            for name, spec in (raw.get("intents") or {}).items()
        }
        for rule in intents.values():
            if rule.mode not in {"a2a", "human"}:
                raise ValueError(f"Intent {rule.name}: mode must be 'a2a' or 'human'.")
        d = raw.get("data_policy") or {}
        return cls(
            business_id=biz["id"],
            business_name=biz.get("name", biz["id"]),
            description=biz.get("description", ""),
            trusted_issuers=list(raw.get("trusted_issuers") or []),
            intents=intents,
            limits=Limits(**(raw.get("limits") or {})),
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
            {"intent": r.name, "description": r.description, "scope": r.scope, "mode": r.mode, "params": r.params}
            for r in self.intents.values()
        ]
