"""Sunrise Family Clinic's AI front-desk agent, built with Google ADK.

Its tools are thin wrappers over ClinicService, the same service the human front desk uses. The
patient the caller acts for comes from ADK session state, set by the gateway from the verified
passport, never from anything the caller typed.
"""

from __future__ import annotations

import json
import re
from typing import AsyncGenerator

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.adk.tools import ToolContext
from google.genai import types

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
        if tool == "clinic_info":
            params.setdefault("topic", "hours")
        if tool == "cancel_appointment":
            params.setdefault("reason", "")
        yield LlmResponse(content=types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(name=tool, args=params))]))


def build_clinic_agent(service: ClinicService, *, use_gemini: bool, model: str) -> LlmAgent:
    return LlmAgent(
        name="sunrise_front_desk",
        description="Front-desk agent for Sunrise Family Clinic (appointments and co-pays).",
        model=model if use_gemini else ScriptedClinicModel(model="scripted-clinic"),
        instruction=INSTRUCTION,
        tools=build_tools(service),
    )
