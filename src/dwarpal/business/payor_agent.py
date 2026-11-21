"""Brightline Health Plan's provider-services agent, built with Google ADK.

Its tools are thin wrappers over PayorService, the same service provider-services staff use. The
provider the caller acts for (its NPI) comes from ADK session state, set by the gateway from the
verified passport or the IVR's verification, never from anything the caller typed.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.tools import ToolContext

from .payor import Caller, PayorService
from ..llm import fast_config
from .scripted import ScriptedModel


def _caller(tool_context: ToolContext) -> Caller:
    st = tool_context.state
    return Caller(channel="agent", id=st.get("caller_agent_id") or "anonymous", npi=st.get("principal_id") or None)


def build_tools(service: PayorService) -> list:
    def pa_requirements(cpt_code: str, plan: str, tool_context: ToolContext) -> dict:
        """Does this CPT code need prior authorisation under this plan? Public information."""
        return service.pa_requirements(_caller(tool_context), cpt_code, plan)

    def plan_contacts(topic: str, tool_context: ToolContext) -> dict:
        """Provider-services hours, prior-auth fax, or how to submit. topic: hours | fax | portal."""
        return service.plan_contacts(_caller(tool_context), topic)

    async def pa_status(auth_number: str, member_id: str, member_dob: str, tool_context: ToolContext) -> dict:
        """Status of a prior authorisation this provider requested. The member's id and date of birth must match."""
        return await service.pa_status(_caller(tool_context), auth_number, member_id, member_dob)

    def eligibility(member_id: str, member_dob: str, service_type: str, tool_context: ToolContext) -> dict:
        """Eligibility and benefits for a member (X12 service type code, e.g. 30 for general benefits)."""
        return service.eligibility(_caller(tool_context), member_id, member_dob, service_type)

    return [pa_requirements, plan_contacts, pa_status, eligibility]


INTENT_TO_TOOL = {"pa_requirements": "pa_requirements", "plan_contacts": "plan_contacts",
                  "pa_status": "pa_status", "eligibility_benefits": "eligibility"}

INSTRUCTION = """You are Brightline Health Plan's provider-services agent.
Requests reach you through the Dwarpal gateway as a VERIFIED TASK: the gateway has already checked which
provider (NPI) is asking and what it may ask for.

Rules:
- Do exactly the task's intent with its parameters, using your tools. Do not do anything else.
- Answer only with what the tool returns. Never add diagnoses, member addresses or other details.
- If a tool says something isn't found or needs a person, say so plainly.
- Reply in one or two short, factual sentences."""


def build_payor_agent(service: PayorService, *, use_gemini: bool, model: str) -> LlmAgent:
    return LlmAgent(
        name="brightline_provider_services",
        description="Provider-services agent for Brightline Health Plan (prior-auth status, eligibility).",
        model=model if use_gemini else ScriptedModel(model="scripted-payor", intent_map=INTENT_TO_TOOL,
                                                     defaults={"plan_contacts": {"topic": "hours"},
                                                               "eligibility": {"service_type": "30"}}),
        instruction=INSTRUCTION,
        tools=build_tools(service),
        generate_content_config=fast_config(model) if use_gemini else None,
    )
