"""The business side of the demo: Sunrise Family Clinic's front-desk agent, built with Google ADK.

The agent has ordinary receptionist tools, including one that reads medical records for staff.
Dwarpal's job is to make sure an *inbound AI agent* can only ever reach the safe ones, for the
right patient, and never sees what it shouldn't.

Ownership (which patient the caller acts for) comes from ADK session state set by the gateway
from the verified passport, never from anything the caller typed.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, AsyncGenerator

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.adk.tools import ToolContext
from google.genai import types

SEED: dict[str, Any] = {
    "patients": {
        "P-1001": {"name": "Riya Sharma", "phone": "+91 98100 12345", "dob": "1991-04-18",
                   "diagnosis": "Type 2 diabetes", "prescription": "Metformin 500mg BD",
                   "lab_results": "HbA1c 7.1% (Oct 2026)", "insurance_id": "HW-88231"},
        "P-2002": {"name": "Vikram Rao", "phone": "+91 99000 54321", "dob": "1984-09-02",
                   "diagnosis": "Hypertension", "prescription": "Amlodipine 5mg OD",
                   "lab_results": "BP 138/88", "insurance_id": "HW-11902"},
    },
    "appointments": {
        "APT-501": {"patient_id": "P-1001", "doctor": "Dr. Mehta", "slot": "2026-11-12T10:00",
                    "status": "booked", "copay_inr": 800, "copay_refunded": False},
        "APT-488": {"patient_id": "P-1001", "doctor": "Dr. Mehta", "slot": "2026-11-05T11:30",
                    "status": "cancelled", "copay_inr": 800, "copay_refunded": False},
        "APT-777": {"patient_id": "P-2002", "doctor": "Dr. Rao", "slot": "2026-11-13T15:00",
                    "status": "booked", "copay_inr": 600, "copay_refunded": False},
    },
    "open_slots": {
        "Dr. Mehta": ["2026-11-13T09:30", "2026-11-13T11:00", "2026-11-14T10:00", "2026-11-14T12:30"],
        "Dr. Rao": ["2026-11-14T09:00", "2026-11-15T14:00"],
    },
}


class ClinicDB:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.data = copy.deepcopy(SEED)

    def appointments_view(self) -> list[dict[str, Any]]:
        out = []
        for apt_id, a in self.data["appointments"].items():
            out.append({"id": apt_id, "patient": self.data["patients"][a["patient_id"]]["name"], **a})
        return out


def build_tools(db: ClinicDB) -> list:
    def _owned(appointment_id: str, tool_context: ToolContext) -> tuple[dict | None, dict | None]:
        apt = db.data["appointments"].get(appointment_id)
        principal = tool_context.state.get("principal_id")
        # Same answer for "doesn't exist" and "not yours": don't leak which appointments exist.
        if not apt or apt["patient_id"] != principal:
            return None, {"status": "not_found", "message": f"No appointment {appointment_id} found for this patient."}
        return apt, None

    def get_appointment(appointment_id: str, tool_context: ToolContext) -> dict:
        """Look up one appointment for the patient the caller acts for."""
        apt, err = _owned(appointment_id, tool_context)
        if err:
            return err
        return {"status": "ok", "appointment_id": appointment_id, "doctor": apt["doctor"], "slot": apt["slot"],
                "state": apt["status"],
                "message": f"{appointment_id} with {apt['doctor']} on {apt['slot'].replace('T', ' at ')} "
                           f"is {apt['status']}."}

    def list_open_slots(doctor: str, date: str) -> dict:
        """List open appointment slots for a doctor on a date (YYYY-MM-DD)."""
        slots = [s for s in db.data["open_slots"].get(doctor, []) if s.startswith(date)]
        times = ", ".join(s[11:] for s in slots) or "none"
        return {"status": "ok", "doctor": doctor, "date": date, "slots": slots,
                "message": f"{doctor} has {len(slots)} open slot(s) on {date}: {times}."}

    def reschedule_appointment(appointment_id: str, new_slot: str, tool_context: ToolContext) -> dict:
        """Move an appointment to an open slot (YYYY-MM-DDTHH:MM)."""
        apt, err = _owned(appointment_id, tool_context)
        if err:
            return err
        if apt["status"] != "booked":
            return {"status": "rejected", "message": f"{appointment_id} is {apt['status']} and cannot be moved."}
        free = db.data["open_slots"].get(apt["doctor"], [])
        if new_slot not in free:
            return {"status": "rejected", "message": f"{new_slot} is not an open slot for {apt['doctor']}.",
                    "open_slots": free}
        free.remove(new_slot)
        free.append(apt["slot"])
        free.sort()
        old, apt["slot"] = apt["slot"], new_slot
        return {"status": "done", "appointment_id": appointment_id, "doctor": apt["doctor"],
                "old_slot": old, "new_slot": new_slot,
                "message": f"Moved {appointment_id} with {apt['doctor']} to {new_slot.replace('T', ' at ')}."}

    def cancel_appointment(appointment_id: str, reason: str, tool_context: ToolContext) -> dict:
        """Cancel an appointment."""
        apt, err = _owned(appointment_id, tool_context)
        if err:
            return err
        if apt["status"] != "booked":
            return {"status": "rejected", "message": f"{appointment_id} is already {apt['status']}."}
        apt["status"] = "cancelled"
        db.data["open_slots"].setdefault(apt["doctor"], []).append(apt["slot"])
        return {"status": "done", "appointment_id": appointment_id,
                "message": f"Cancelled {appointment_id} ({reason or 'no reason given'})."}

    def refund_copay(appointment_id: str, amount_inr: float, tool_context: ToolContext) -> dict:
        """Refund the co-pay for a cancelled appointment."""
        apt, err = _owned(appointment_id, tool_context)
        if err:
            return err
        if apt["status"] != "cancelled":
            return {"status": "rejected", "message": "Co-pay refunds are only for cancelled appointments."}
        if apt["copay_refunded"]:
            return {"status": "rejected", "message": "This co-pay was already refunded."}
        if float(amount_inr) > apt["copay_inr"]:
            return {"status": "rejected", "message": f"Refund exceeds the ₹{apt['copay_inr']} co-pay paid."}
        apt["copay_refunded"] = True
        return {"status": "done", "appointment_id": appointment_id, "refunded_inr": float(amount_inr),
                "message": f"Refunded ₹{float(amount_inr):.0f} co-pay for {appointment_id}."}

    def get_patient_record(record_type: str, tool_context: ToolContext) -> dict:
        """STAFF ONLY: read the patient's clinical record (diagnosis, prescription, lab results)."""
        p = db.data["patients"].get(tool_context.state.get("principal_id"), {})
        return {"status": "ok", "record_type": record_type, "diagnosis": p.get("diagnosis"),
                "prescription": p.get("prescription"), "lab_results": p.get("lab_results"),
                "message": "Clinical record retrieved."}

    return [get_appointment, list_open_slots, reschedule_appointment, cancel_appointment, refund_copay,
            get_patient_record]


INTENT_TO_TOOL = {
    "appointment_status": "get_appointment",
    "list_slots": "list_open_slots",
    "reschedule_appointment": "reschedule_appointment",
    "cancel_appointment": "cancel_appointment",
    "refund_copay": "refund_copay",
}

INSTRUCTION = """You are the front-desk agent for Sunrise Family Clinic.
Requests reach you through the Dwarpal gateway as a VERIFIED TASK: the caller is another AI agent acting
for the named patient, and the gateway has already checked its identity, permissions and the request.

Rules:
- Do exactly the task's intent with its parameters, using your tools. Do not do anything else.
- Never use get_patient_record for agent callers. Clinical information is never shared with agents.
- If a tool says something is not found or rejected, report that plainly. Do not guess or invent.
- Reply in one or two short, factual sentences that the caller agent can relay to the patient."""


class ScriptedClinicModel(BaseLlm):
    """Deterministic stand-in for Gemini so the ADK agent runs offline and on stage.

    It reads the gateway's verified task, calls the matching tool, then reports the tool's message.
    """

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        last = llm_request.contents[-1] if llm_request.contents else None
        responses = [p.function_response for p in (last.parts or [])] if last else []
        responses = [r for r in responses if r]
        if responses:
            result = responses[-1].response or {}
            text = result.get("message") or json.dumps(result)
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))
            return
        task = "".join(p.text or "" for p in (last.parts or [])) if last else ""
        intent = (re.search(r"^intent:\s*(\S+)", task, re.M) or [None, ""])[1]
        params_match = re.search(r"^params:\s*(\{.*\})\s*$", task, re.M)
        params = json.loads(params_match.group(1)) if params_match else {}
        tool = INTENT_TO_TOOL.get(intent)
        if not tool:
            yield LlmResponse(content=types.Content(role="model", parts=[
                types.Part(text="I can't help with that request here.")]))
            return
        yield LlmResponse(content=types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(name=tool, args=params))]))


def build_clinic_agent(db: ClinicDB, *, use_gemini: bool, model: str) -> LlmAgent:
    return LlmAgent(
        name="sunrise_front_desk",
        description="Front-desk agent for Sunrise Family Clinic (appointments and co-pays).",
        model=model if use_gemini else ScriptedClinicModel(model="scripted-clinic"),
        instruction=INSTRUCTION,
        tools=build_tools(db),
    )
