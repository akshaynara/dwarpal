"""Brightline Health Plan (fictional): the payor side of the demo.

Providers' staff spend hours on the phone with payors checking prior-authorisation (PA) status and
eligibility, and providers are now putting AI agents on those calls. The questions are cooperative,
rule-bound and high-volume, which makes them the low-hanging case for agent-to-agent traffic.

The hard part is fragmentation. Different plans at the same payor sit on different rails:

  * Medicare Advantage: a FHIR PA API (HL7 Da Vinci PAS style). CMS-0057-F requires these APIs for
    Medicare Advantage, Medicaid/CHIP managed care and federal-exchange plans from 1 January 2027.
  * Commercial PPO: no FHIR API; X12 278 (PA inquiry) and X12 270/271 (eligibility) only.
  * A legacy employer plan: no electronic inquiry at all. A person has to look it up.

Each plan's rails are declared in the payor's policy file, tried in order, and the service falls
back to a person when no rail can answer. The gateway never touches any of this: it asks the payor's
agent, whose tools call this service, the same one provider-services staff use.

The FHIR and X12 connectors below are simulations that build and parse FHIR- and X12-*shaped*
messages so the flow is visible. Production would call the payor's real endpoints or a clearinghouse.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Any, Callable

from datetime import date

# X12 HCR01 / Da Vinci PAS review action codes (subset, for illustration)
REVIEW = {
    "A1": "approved (certified in total)",
    "A3": "denied (not certified)",
    "A4": "pended: more information needed",
}

SEED: dict[str, Any] = {
    "providers": {
        "1234567893": {"name": "Sunrise Family Clinic", "tin": "06-1234567"},
        "1245319599": {"name": "Lakeside Orthopaedics", "tin": "06-7654321"},
    },
    "members": {
        "M-55102": {"name": "Asha Verma", "dob": "1958-03-11", "plan": "MA", "active": True,
                    "ssn": "XXX-XX-4410", "address": "12 Lake Rd"},
        "M-77310": {"name": "Rohan Das", "dob": "1979-07-22", "plan": "PPO", "active": True,
                    "ssn": "XXX-XX-1187", "address": "4 Hill St"},
        "M-31944": {"name": "Meera Iyer", "dob": "1966-12-01", "plan": "LEGACY", "active": True,
                    "ssn": "XXX-XX-9921", "address": "9 Park Ave"},
    },
    "auths": {
        "PA-90001": {"member": "M-55102", "npi": "1234567893", "cpt": "72148", "service": "MRI lumbar spine",
                     "units": 1, "review": "A1", "valid_from": "2026-11-10", "valid_to": "2027-02-08",
                     "diagnosis": "M54.16", "note": ""},
        "PA-90002": {"member": "M-77310", "npi": "1234567893", "cpt": "97110", "service": "Therapeutic exercise",
                     "units": 12, "review": "A4", "valid_from": None, "valid_to": None, "diagnosis": "M25.561",
                     "note": "Send the last 30 days of therapy notes."},
        "PA-90003": {"member": "M-31944", "npi": "1234567893", "cpt": "29881", "service": "Knee arthroscopy",
                     "units": 1, "review": "A1", "valid_from": "2026-11-15", "valid_to": "2027-01-15",
                     "diagnosis": "M23.221", "note": ""},
        "PA-90004": {"member": "M-55102", "npi": "1245319599", "cpt": "27447", "service": "Total knee replacement",
                     "units": 1, "review": "A3", "valid_from": None, "valid_to": None, "diagnosis": "M17.11",
                     "note": ""},
    },
    "pa_list": {  # published prior-auth requirements by plan (open information)
        "72148": {"MA": True, "PPO": True, "LEGACY": True, "service": "MRI lumbar spine"},
        "97110": {"MA": False, "PPO": True, "LEGACY": False, "service": "Therapeutic exercise"},
        "99213": {"MA": False, "PPO": False, "LEGACY": False, "service": "Office visit"},
        "29881": {"MA": True, "PPO": True, "LEGACY": True, "service": "Knee arthroscopy"},
    },
    "info": {
        "fax": "Prior-auth fax: +1-555-0100. Clinical documents can also go through the provider portal.",
        "hours": "Provider services: Monday to Friday, 8 am to 6 pm Eastern.",
        "portal": "Submit new prior-auth requests through the provider portal or the FHIR PAS submission endpoint.",
    },
}

PLAN_NAMES = {"MA": "Brightline Medicare Advantage", "PPO": "Brightline Commercial PPO",
              "LEGACY": "Brightline Employer Classic"}


@dataclass(frozen=True)
class Caller:
    channel: str  # agent | staff
    id: str
    npi: str | None = None


def _date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d %b %Y").lstrip("0")


def _mask(name: str) -> str:
    """Minimum necessary: initials only, never the member's full name."""
    return " ".join(p[0] + "." for p in name.split())


# --------------------------------------------------------------------------- rails (simulated)


class FhirPasRail:
    """HL7 Da Vinci PAS-style inquiry: a Claim $inquire request answered with a ClaimResponse."""
    id = "fhir_pas"
    label = "FHIR PA API ($inquire)"

    def inquire(self, auth: dict[str, Any], auth_number: str) -> dict[str, Any]:
        request = {"resourceType": "Bundle", "type": "collection",
                   "entry": [{"resource": {"resourceType": "Claim", "use": "preauthorization",
                                           "identifier": [{"value": auth_number}]}}]}
        response = {"resourceType": "ClaimResponse", "outcome": "complete" if auth["review"] != "A4" else "queued",
                    "preAuthRef": auth_number,
                    "item": [{"adjudication": [{"category": {"text": "reviewAction"},
                                                "reason": {"coding": [{"code": auth["review"]}]}}]}]}
        return {"request": request, "response": response, "summary":
                f"Claim/$inquire for {auth_number} → ClaimResponse outcome {response['outcome']}, "
                f"reviewAction {auth['review']}"}


class X12PARail:
    """X12 278 request/response (Health Care Services Review), heavily simplified."""
    id = "x12_278"
    label = "X12 278 inquiry"

    def inquire(self, auth: dict[str, Any], auth_number: str) -> dict[str, Any]:
        req = f"ST*278*0001~BHT*0007*13~HL*1**20*1~NM1*X3*2*BRIGHTLINE~REF*BB*{auth_number}~SE*6*0001~"
        resp = f"ST*278*0002~HCR*{auth['review']}*{auth_number}~SE*3*0002~"
        return {"request": req, "response": resp, "summary": f"278 inquiry for {auth_number} → HCR*{auth['review']}"}


class X12EligibilityRail:
    """X12 270 request answered by a 271, heavily simplified."""
    id = "x12_270"
    label = "X12 270/271 eligibility"

    def inquire(self, member_id: str, member: dict[str, Any], service_type: str) -> dict[str, Any]:
        eb = "1" if member["active"] else "6"  # EB01: 1 active coverage, 6 inactive
        return {"request": f"ST*270*0001~NM1*IL*1*****MI*{member_id}~EQ*{service_type}~SE*4*0001~",
                "response": f"ST*271*0002~EB*{eb}**{service_type}~EB*B**{service_type}*****20~SE*4*0002~",
                "summary": f"270 for {member_id} → 271 EB*{eb}"}


RAILS: dict[str, Any] = {"fhir_pas": FhirPasRail(), "x12_278": X12PARail(), "x12_270": X12EligibilityRail()}


class PayorService:
    audit_event = "business_lookup"  # reads through the payor's rails, recorded in the audit chain

    def __init__(self, rails_by_plan: dict[str, dict[str, list[str]]] | None = None,
                 on_write: Callable[[dict[str, Any]], None] | None = None):
        # e.g. {"MA": {"pa_status": ["fhir_pas", "x12_278"], "eligibility": ["x12_270"]}, ...}
        self.rails_by_plan = rails_by_plan or {}
        self.on_write = on_write
        self.reset()

    def reset(self) -> None:
        self.data = copy.deepcopy(SEED)
        self.changes: list[dict[str, Any]] = []
        self.lookups: list[dict[str, Any]] = []

    def _record(self, caller: Caller, action: str, **details: Any) -> None:
        entry = {"ts": round(time.time(), 3), "action": action, "channel": caller.channel, "actor": caller.id,
                 "principal": caller.npi, **details}
        self.lookups.append(entry)
        if self.on_write:
            self.on_write(entry)

    def _rails(self, plan: str, kind: str) -> list[str]:
        return list((self.rails_by_plan.get(plan) or {}).get(kind) or [])

    # ------------------------------------------------------------------ open information

    def pa_requirements(self, caller: Caller, cpt_code: str, plan: str) -> dict:
        row = self.data["pa_list"].get(str(cpt_code))
        plan_key = next((k for k, v in PLAN_NAMES.items() if plan.upper() in (k, v.upper())), plan.upper())
        if not row or plan_key not in PLAN_NAMES:
            return {"status": "not_found", "message": f"No published rule for CPT {cpt_code} under plan {plan}."}
        needed = bool(row.get(plan_key))
        answer = (f"CPT {cpt_code} ({row['service']}) {'needs' if needed else 'does not need'} prior "
                  f"authorisation under {PLAN_NAMES[plan_key]}.")
        return {"status": "ok", "cpt_code": cpt_code, "plan": PLAN_NAMES[plan_key], "required": needed,
                "answer": answer, "message": answer}

    def plan_contacts(self, caller: Caller, topic: str = "hours") -> dict:
        info = self.data["info"]
        key = next((k for k in info if k in str(topic).lower()), "hours")
        return {"status": "ok", "topic": key, "answer": info[key], "message": info[key]}

    # ------------------------------------------------------------------ provider-specific

    def pa_status(self, caller: Caller, auth_number: str, member_id: str, member_dob: str) -> dict:
        auth = self.data["auths"].get(str(auth_number))
        member = self.data["members"].get(str(member_id))
        # Same answer whether the auth doesn't exist, belongs to another provider, or the member details
        # don't match: callers can't use this to probe.
        if (not auth or not member or auth["member"] != member_id or member["dob"] != member_dob
                or (caller.channel != "staff" and auth["npi"] != caller.npi)):
            return {"status": "not_found",
                    "message": f"No prior authorisation {auth_number} matches that member for this provider."}
        plan = member["plan"]
        trace: list[str] = []
        for rail_id in self._rails(plan, "pa_status"):
            rail = RAILS.get(rail_id)
            if rail is None:
                trace.append(f"{rail_id}: unknown rail")
                continue
            res = rail.inquire(auth, auth_number)
            trace.append(f"{rail.label}: {res['summary']}")
            label = REVIEW.get(auth["review"], auth["review"])
            dates = (f" Valid {_date(auth['valid_from'])} to {_date(auth['valid_to'])}."
                     if auth["valid_from"] else "")
            detail = f" {auth['note']}" if auth["note"] else ""
            self._record(caller, "pa_status", auth=auth_number, rail=rail.id, plan=plan)
            return {"status": "ok", "auth_number": auth_number, "member": _mask(member["name"]),
                    "plan": PLAN_NAMES[plan], "service": f"{auth['service']} (CPT {auth['cpt']})",
                    "units": auth["units"], "review_code": auth["review"], "status_label": label,
                    "valid": dates.strip(), "action_needed": auth["note"], "rail": rail.id, "rail_label": rail.label,
                    "rail_trace": trace, "diagnosis": auth["diagnosis"],  # dropped by the data guard
                    "message": f"{auth_number} for {auth['service']} is {label}.{dates}{detail}"}
        trace.append(f"No electronic PA inquiry for {PLAN_NAMES[plan]}")
        self._record(caller, "pa_status_handoff", auth=auth_number, plan=plan)
        return {"status": "needs_human", "auth_number": auth_number, "plan": PLAN_NAMES[plan], "rail": "human",
                "rail_trace": trace, "response_hours": 8,
                "message": f"{PLAN_NAMES[plan]} has no electronic PA inquiry, so a provider-services "
                           f"representative will look up {auth_number}."}

    def eligibility(self, caller: Caller, member_id: str, member_dob: str, service_type: str = "30") -> dict:
        member = self.data["members"].get(str(member_id))
        if not member or member["dob"] != member_dob:
            return {"status": "not_found", "message": "No member matches those details."}
        plan = member["plan"]
        for rail_id in self._rails(plan, "eligibility"):
            rail = RAILS.get(rail_id)
            if rail is None:
                continue
            res = rail.inquire(member_id, member, service_type)
            self._record(caller, "eligibility", member=member_id, rail=rail.id, plan=plan)
            state = "active" if member["active"] else "inactive"
            return {"status": "ok", "member": _mask(member["name"]), "plan": PLAN_NAMES[plan], "coverage": state,
                    "copay": "$20 office visit", "rail": rail.id, "rail_label": rail.label,
                    "rail_trace": [f"{rail.label}: {res['summary']}"],
                    "ssn": member["ssn"], "address": member["address"],  # dropped by the data guard
                    "message": f"Coverage is {state} under {PLAN_NAMES[plan]}; office-visit copay $20."}
        self._record(caller, "eligibility_handoff", member=member_id, plan=plan)
        return {"status": "needs_human", "plan": PLAN_NAMES[plan], "rail": "human", "response_hours": 8,
                "rail_trace": [f"No electronic eligibility inquiry for {PLAN_NAMES[plan]}"],
                "message": f"{PLAN_NAMES[plan]} has no electronic eligibility check, so a representative will "
                           "confirm coverage."}

    # ------------------------------------------------------------------ views

    def records_view(self) -> dict[str, Any]:
        rows = []
        for a_id, a in self.data["auths"].items():
            m = self.data["members"][a["member"]]
            rails = " → ".join(self._rails(m["plan"], "pa_status")) or "person only"
            short_plan = {"MA": "Medicare Adv.", "PPO": "Commercial", "LEGACY": "Employer"}[m["plan"]]
            rows.append([a_id, short_plan, REVIEW.get(a["review"], a["review"]).split(":")[0].split(" (")[0],
                         rails.replace("fhir_pas", "FHIR").replace("x12_278", "X12 278")])
        last = self.lookups[-1] if self.lookups else None
        return {"title": "Prior authorisations", "columns": ["Auth", "Plan", "Status", "Rails"],
                "rows": rows,
                "last_change": (f"Last lookup: {last['action'].replace('_', ' ')} by {last['actor']} via "
                                f"{RAILS[last['rail']].label if last.get('rail') in RAILS else 'a person'}, "
                                "through the same service provider-services staff use." if last else
                                "Agents and provider-services staff query the same service.")}
