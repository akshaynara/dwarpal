"""What a pilot needs to show: computed from the audit chain, so the numbers survive restarts and match
the signed record exactly."""

from __future__ import annotations

from collections import Counter
from typing import Any

ATTACK_CODES = {"replay", "body_tampered", "untrusted_issuer", "bad_signature", "key_mismatch", "lane_mismatch",
                "issuer_mismatch", "wrong_audience", "malformed_token", "stale_proof", "expired_passport",
                "revoked_passport", "weak_signature", "malformed_signature"}
REPEAT_CODES = {"already_open", "session_with_human"}
OUTCOME_ORDER = ["completed", "step_up_required", "escalated", "rejected", "denied", "needs_structure"]
OUTCOME_LABEL = {"completed": "Completed", "step_up_required": "Waiting on the customer",
                 "escalated": "Handed to a person", "rejected": "Business said no", "denied": "Refused at the gate",
                 "needs_structure": "Asked to use structure", "error": "Error"}


def compute(records: list[dict[str, Any]], *, avg_call_minutes: float) -> dict[str, Any]:
    requests = [r["data"] for r in records if r.get("event") == "agent_request"]
    outcomes = Counter(d.get("outcome") for d in requests)
    channels = Counter(d.get("channel") or "a2a" for d in requests)
    tiers = Counter(d.get("tier") or "none" for d in requests)

    def codes(d: dict[str, Any]) -> set[str]:
        return set(d.get("reason_codes") or [])

    attacks = [d for d in requests
               if (d.get("checks") or {}).get("security") == "fail" or codes(d) & ATTACK_CODES]
    phone = [d for d in requests if d.get("channel") == "voice"]
    ai_on_phone = [d for d in phone if (d.get("signals") or {}).get("declared_agent")
                   or "ai_voice_undisclosed" in ((d.get("signals") or {}).get("restrictions") or [])]
    agent_requests = channels.get("a2a", 0) + len(ai_on_phone)
    completed_a2a = sum(1 for d in requests if d.get("outcome") == "completed" and (d.get("channel") or "a2a") == "a2a")
    step_factors = [r["data"] for r in records if r.get("event") == "step_up_factor"]
    approved = {d.get("challenge") for d in step_factors if d.get("status") == "approved"}
    asked = {d.get("step_up") for d in requests if d.get("outcome") == "step_up_required" and d.get("step_up")}
    rails = Counter(r["data"].get("rail") or "human" for r in records
                    if r.get("event") == "business_lookup" and r["data"].get("action") in {"pa_status", "eligibility",
                                                                                            "pa_status_handoff",
                                                                                            "eligibility_handoff"})
    total = len(requests)
    return {
        "requests": total,
        "agent_requests": agent_requests,
        "agent_share": round(agent_requests / total, 3) if total else 0.0,
        "completed": outcomes.get("completed", 0),
        "completed_share": round(outcomes.get("completed", 0) / total, 3) if total else 0.0,
        "outcomes": [{"key": k, "label": OUTCOME_LABEL[k], "count": outcomes.get(k, 0)} for k in OUTCOME_ORDER
                     if outcomes.get(k, 0)],
        "channels": {"a2a": channels.get("a2a", 0), "phone": channels.get("voice", 0)},
        "tiers": dict(tiers),
        "lane_offers": sum(1 for d in requests if d.get("lane_offered")),
        "lane_switches": sum(1 for d in requests if d.get("continued_from_call")),
        "attacks_stopped": len(attacks),
        "injection_blocked": sum(1 for d in attacks if (d.get("checks") or {}).get("security") == "fail"),
        "step_ups_asked": len(asked),
        "step_ups_approved": len(approved & asked) if asked else len(approved),
        "handed_to_person": len({d.get("ticket") for d in requests if d.get("ticket")}),
        "repeats_absorbed": sum(1 for d in requests if codes(d) & REPEAT_CODES),
        "rails": dict(rails),
        "avg_call_minutes": avg_call_minutes,
        "phone_minutes_avoided": round(completed_a2a * avg_call_minutes),
        "signed_records": len(records),
    }
