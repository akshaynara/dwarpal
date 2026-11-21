"""Two messages in every response: one for the calling agent, one for the customer.

* The agent outcome is machine-readable: decision, reason codes, next steps, when to retry.
* The customer message is written by the business (templates in the policy file), in its own voice.
  The calling agent is asked to relay it as-is, and its hash goes into the signed receipt, so the
  customer, their app or the business can later check exactly what the business said.

Refusals stay neutral. When a request is blocked for security reasons the customer is usually
innocent (their assistant may be compromised), so the agent gets the technical reason and the
customer gets a way to reach the business directly.
"""

from __future__ import annotations

import hashlib
import re
import string
from typing import Any

from .business.service import human_slot
from .policy import IntentRule, Policy


class _Fields(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _fmt(template: str, fields: dict[str, Any]) -> str:
    out = string.Formatter().vformat(template, (), _Fields({k: v for k, v in fields.items() if v is not None}))
    return re.sub(r"\s{2,}", " ", out).strip()


def _tidy(v: Any) -> Any:
    # A2A carries data as protobuf Struct, which turns 800 into 800.0. Show it as people write it.
    return int(v) if isinstance(v, float) and v.is_integer() else v


def enrich(params: dict[str, Any]) -> dict[str, Any]:
    f = {k: _tidy(v) for k, v in params.items()}
    if "new_slot" in f:
        f["new_slot_human"] = human_slot(str(f["new_slot"]))
    return f


def action_summary(rule: IntentRule | None, params: dict[str, Any]) -> str:
    if rule is None:
        return "complete this request"
    return _fmt(rule.summary or rule.name.replace("_", " "), enrich(params))


def customer_message(policy: Policy, outcome: str, *, intent: str | None, reason: str | None,
                     fields: dict[str, Any]) -> str:
    keys = [k for k in (f"{outcome}.{intent}" if intent else None, f"{outcome}.{reason}" if reason else None,
                        outcome) if k]
    template = next((policy.customer_messages[k] for k in keys if k in policy.customer_messages), None)
    if template is None:
        return ""
    return _fmt(template, {"contact": policy.contact, "business": policy.business_name, **fields})


def message_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def agent_outcome(decision: str, reason_codes: list[str], *, next_steps: list[str] | None = None,
                  retry_after_seconds: int | None = None, expected_by: float | None = None,
                  relay: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {"decision": decision, "reason_codes": reason_codes, "next_steps": next_steps or [],
                           "relay_customer_message": relay}
    if retry_after_seconds is not None:
        out["retry_after_seconds"] = int(max(retry_after_seconds, 0))
    if expected_by is not None:
        out["expected_by"] = int(expected_by)
    return out
