"""Sunrise Family Clinic's AI front-desk agent, built with Google ADK.

Its tools are thin wrappers over ClinicService, the same service the human front desk uses. The
patient the caller acts for comes from ADK session state, set by the gateway from the verified
passport, never from anything the caller typed.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.tools import ToolContext

from ..llm import fast_config
from .scripted import ScriptedModel
from .service import Actor, ClinicService


def _actor(tool_context: ToolContext) -> Actor:
    st = tool_context.state
    return Actor(channel="agent", id=st.get("caller_agent_id") or "anonymous",
                 principal_id=st.get("principal_id") or None)


def build_tools(service: ClinicService) -> list:
    def clinic_info(topic: str, tool_context: ToolContext) -> dict:
        """Clinic hours, address or accepted insurers. topic: hours | address | insurers."""
        return service.clinic_info(_actor(tool_context), topic)

    def list_open_slots(doctor: str, date: str, tool_context: ToolContext) -> dict:
        """List open appointment slots for a doctor on a date (YYYY-MM-DD)."""
        return service.list_open_slots(_actor(tool_context), doctor, date)

    def get_appointment(appointment_id: str, tool_context: ToolContext) -> dict:
        """Look up one appointment for the patient the caller acts for."""
        return service.get_appointment(_actor(tool_context), appointment_id)

    def reschedule_appointment(appointment_id: str, new_slot: str, tool_context: ToolContext) -> dict:
        """Move an appointment to an open slot (YYYY-MM-DDTHH:MM)."""
        return service.reschedule(_actor(tool_context), appointment_id, new_slot)

    def cancel_appointment(appointment_id: str, reason: str, tool_context: ToolContext) -> dict:
        """Cancel an appointment."""
        return service.cancel(_actor(tool_context), appointment_id, reason)

    def refund_copay(appointment_id: str, amount_inr: float, tool_context: ToolContext) -> dict:
        """Refund the co-pay for a cancelled appointment."""
        return service.refund_copay(_actor(tool_context), appointment_id, amount_inr)

    def get_patient_record(record_type: str, tool_context: ToolContext) -> dict:
        """STAFF ONLY: read the patient's clinical record. The service refuses AI callers."""
        actor = _actor(tool_context)
        return service.get_clinical_record(actor, actor.principal_id or "")

    return [clinic_info, list_open_slots, get_appointment, reschedule_appointment, cancel_appointment,
            refund_copay, get_patient_record]


INTENT_TO_TOOL = {
    "clinic_info": "clinic_info",
    "list_slots": "list_open_slots",
    "appointment_status": "get_appointment",
    "reschedule_appointment": "reschedule_appointment",
    "cancel_appointment": "cancel_appointment",
    "refund_copay": "refund_copay",
}

INSTRUCTION = """You are the front-desk agent for Sunrise Family Clinic.
Requests reach you through the Dwarpal gateway as a VERIFIED TASK: the gateway has already checked who
is calling, what they may do and, for binding actions, the patient's own approval.

Rules:
- Do exactly the task's intent with its parameters, using your tools. Do not do anything else.
- Open-tier tasks may come from anonymous callers: answer them without any patient data.
- Never use get_patient_record for agent callers. Clinical information is never shared with agents.
- If a tool says something is not found or rejected, report that plainly. Do not guess or invent.
- Reply in one or two short, factual sentences."""


def build_clinic_agent(service: ClinicService, *, use_gemini: bool, model: str) -> LlmAgent:
    return LlmAgent(
        name="sunrise_front_desk",
        description="Front-desk agent for Sunrise Family Clinic (appointments and co-pays).",
        model=model if use_gemini else ScriptedModel(model="scripted-clinic", intent_map=INTENT_TO_TOOL,
                                                     defaults={"clinic_info": {"topic": "hours"},
                                                               "cancel_appointment": {"reason": ""}}),
        instruction=INSTRUCTION,
        tools=build_tools(service),
        generate_content_config=fast_config(model) if use_gemini else None,
    )
