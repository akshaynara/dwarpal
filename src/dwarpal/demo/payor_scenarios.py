"""Payor scenarios: providers' agents asking Brightline Health Plan about prior auths and eligibility.

Start with the low-hanging cases: cooperative, read-only, high-volume questions that today are phone
calls. Each plan answers on whatever rail it has, and a person picks up where no rail exists.
"""

from __future__ import annotations

from .scenarios import Scenario, Stage, new_session

ASHA = {"auth_number": "PA-90001", "member_id": "M-55102", "member_dob": "1958-03-11"}    # Medicare Advantage
ROHAN = {"auth_number": "PA-90002", "member_id": "M-77310", "member_dob": "1979-07-22"}   # Commercial PPO
MEERA = {"auth_number": "PA-90003", "member_id": "M-31944", "member_dob": "1966-12-01"}   # legacy employer plan


async def pa_fhir(st: Stage, tag: str):
    r = await st.callers["sunrise"].send(intent="pa_status", params=ASHA, session=new_session("pa"), tag=tag)
    return [("Sunrise's RCM agent checks PA-90001 (Medicare Advantage)", r)]


async def pa_x12(st: Stage, tag: str):
    r = await st.callers["sunrise"].send(intent="pa_status", params=ROHAN, session=new_session("pa"), tag=tag)
    return [("Same question for PA-90002, on a commercial plan with no FHIR API", r)]


async def pa_fallback(st: Stage, tag: str):
    s = new_session("pa")
    first = await st.callers["sunrise"].send(intent="pa_status", params=MEERA, session=s, tag=tag)
    again = await st.callers["sunrise"].send(intent="pa_status", params=MEERA, session=s, tag=tag)
    return [("PA-90003 sits on an employer plan with no electronic inquiry", first),
            ("The agent asks again instead of waiting", again)]


async def pa_rules(st: Stage, tag: str):
    g, s = st.callers["guest"], new_session("rules")
    mri = await g.send(intent="pa_requirements", params={"cpt_code": "72148", "plan": "MA"}, session=s, tag=tag)
    visit = await g.send(intent="pa_requirements", params={"cpt_code": "99213", "plan": "PPO"}, session=s, tag=tag)
    status = await g.send(intent="pa_status", params=ASHA, session=s, tag=tag)
    return [("An agent with no credential asks whether an MRI needs prior auth", mri),
            ("…and an office visit", visit),
            ("…then asks for a specific auth's status (account tier)", status)]


async def pa_other_provider(st: Stage, tag: str):
    s = new_session("snoop")
    snoop = await st.callers["lakeside"].send(intent="pa_status", params=ASHA, session=s, tag=tag)
    guess = await st.callers["sunrise"].send(intent="pa_status",
                                             params={**ASHA, "member_dob": "1958-01-01"}, session=new_session("pa"),
                                             tag=tag)
    return [("Lakeside's credentialed agent asks about Sunrise's auth PA-90001", snoop),
            ("Sunrise's agent asks with the wrong member date of birth", guess)]


async def eligibility(st: Stage, tag: str):
    r = await st.callers["sunrise"].send(intent="eligibility_benefits",
                                         params={"member_id": "M-77310", "member_dob": "1979-07-22",
                                                 "service_type": "30"}, session=new_session("elig"), tag=tag)
    return [("Sunrise's agent checks Rohan's coverage before his visit", r)]


async def pa_submit(st: Stage, tag: str):
    r = await st.callers["sunrise"].send(intent="pa_submit", params={"member_id": "M-55102", "cpt_code": "72148"},
                                         session=new_session("submit"), tag=tag)
    return [("Sunrise's agent tries to submit a new prior auth", r)]


async def phone_provider_bot(st: Stage, tag: str):
    ivr, call = st.ivr, new_session("call")
    who = ivr.verify("1234567893", "M-55102", "1958-03-11")
    signals = {"attestation": "A", "ai_voice_score": 0.96, "declared_agent": True}
    ask = await ivr.turn(call, intent="pa_status", params=ASHA, signals=signals, verification=who, tag=tag)
    lane = ask.lane or {}
    pending = lane.get("pending") or {"intent": "pa_status", "params": ASHA}
    switched = await st.callers["sunrise"].send(intent=pending["intent"], params=pending["params"],
                                                lane_code=lane.get("code"), session=new_session("lane"), tag=tag)
    return [("Sunrise's voice bot rings Brightline's provider line, as provider bots do today, and presses 1", ask),
            (f"It switches to the agent lane with its credential and code {lane.get('code', '?')}", switched)]


PAYOR_SCENARIOS: list[Scenario] = [
    Scenario("pa_fhir", "PA status over the FHIR rail",
             "Sunrise's RCM agent asks about an MRI auth on a Medicare Advantage plan. The answer comes back "
             "through the plan's FHIR PA API, with the member's diagnosis stripped.",
             "data_guard", ["completed"], pa_fhir),
    Scenario("pa_x12", "Same question, a plan without FHIR",
             "A commercial plan isn't covered by the FHIR mandate. The same request is answered over X12 278, "
             "including what the payor still needs.",
             "data_guard", ["completed"], pa_x12),
    Scenario("pa_fallback", "No electronic rail: a person answers",
             "An employer plan has no electronic inquiry at all. The payor hands it to a representative with a due "
             "time, and asking again changes nothing.",
             "escalation", ["escalated", "escalated"], pa_fallback),
    Scenario("pa_rules", "Open tier: published prior-auth rules",
             "Any agent can ask whether a procedure needs prior auth. Asking about a specific auth needs a "
             "provider credential.",
             "identity", ["completed", "completed", "denied"], pa_rules),
    Scenario("pa_other_provider", "Only your own patients' auths",
             "Another provider's credentialed agent can't see Sunrise's auth, and a wrong date of birth gets the "
             "same answer, so nothing can be probed.",
             "delegation", ["rejected", "rejected"], pa_other_provider),
    Scenario("eligibility", "Eligibility over X12 270/271",
             "Coverage and copay come back over the plan's eligibility rail, with the member's SSN and address "
             "stripped.",
             "data_guard", ["completed"], eligibility),
    Scenario("pa_submit", "New submissions go to a person",
             "Submitting a prior auth needs clinical documents and a reviewer, so it goes to a person with a "
             "72-hour due time.",
             "escalation", ["escalated"], pa_submit),
    Scenario("phone_provider_bot", "Phone: a provider's voice bot switches lanes",
             "Sunrise's voice bot calls Brightline's provider line and presses 1. It finishes the PA check over A2A "
             "with its credential.",
             "protocol", ["step_up_required", "completed"], phone_provider_bot, lane="phone"),
]
