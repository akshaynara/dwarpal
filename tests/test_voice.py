"""The phone lane: signals restrict, proof grants; scores only; the lane switch links context, never grants."""

import json

import httpx
import pytest

from dwarpal.app import create_app, stage_for
from dwarpal.config import Settings
from dwarpal.demo import scenarios as sc
from dwarpal.policy import Policy
from dwarpal.voice import VoiceContext, evaluate_signals, restriction_applies

BASE = "http://dwarpal.test"
RIYA = {"principal_id": "P-1001", "name": "Riya Sharma", "method": "patient_id+dob"}


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(llm_mode="mock", data_dir=tmp_path, public_url=BASE), businesses=["clinic"])


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


async def turn(http, app, **body):
    body.setdefault("call_id", "call-test")
    r = await http.post("/voice/turn", json=body, headers={"X-Dwarpal-Channel-Key": app.state.runtime.channel_key})
    assert r.status_code == 200, r.text
    return r.json()


async def run(app, scenario_id):
    rt = app.state.runtime
    async with client(app) as http:
        return await sc.run_scenario(scenario_id, stage_for(rt, http))


async def test_voice_endpoint_needs_the_channel_key(app):
    async with client(app) as http:
        r = await http.post("/voice/turn", json={"call_id": "x", "intent": "clinic_info"})
        assert r.status_code == 401
        r = await http.post("/voice/turn", json={"call_id": "x", "intent": "clinic_info"},
                            headers={"X-Dwarpal-Channel-Key": "guess"})
        assert r.status_code == 401


async def test_open_tier_on_the_phone_needs_nothing(app):
    async with client(app) as http:
        r = await turn(http, app, intent="clinic_info", params={"topic": "hours"})
    assert r["outcome"] == "completed" and "8 am" in r["say"] and r["next"] == "continue"


def signals_matrix():
    p = Policy.load().voice
    def restr(**kw):
        return {r.code for r in evaluate_signals(VoiceContext(call_id="c", **kw), p)}
    return p, restr


def test_signals_never_grant_and_disclosure_never_costs_more_than_lying():
    p, restr = signals_matrix()
    assert restr(attestation="A", ai_voice_score=0.0) == set()            # strong signals add nothing
    caught = restr(attestation="A", ai_voice_score=0.95)
    honest = restr(attestation="A", ai_voice_score=0.95, declared_agent=True)
    assert caught == {"ai_voice_undisclosed"} and honest == {"declared_agent"}
    # Same friction for account actions either way; the honest caller also gets the agent lane.
    for tier, scope in [("account", "appointments:read"), ("account", "appointments:write")]:
        c = any(restriction_applies(r, tier, scope) for r in evaluate_signals(
            VoiceContext(call_id="c", attestation="A", ai_voice_score=0.95), p))
        h = any(restriction_applies(r, tier, scope) for r in evaluate_signals(
            VoiceContext(call_id="c", attestation="A", ai_voice_score=0.95, declared_agent=True), p))
        assert h <= c
    # Signals never touch open-tier actions.
    for r in evaluate_signals(VoiceContext(call_id="c", attestation="none", ai_voice_score=1.0), p):
        assert not restriction_applies(r, "open", None)
    # Weak attestation only adds friction to changes.
    weak = evaluate_signals(VoiceContext(call_id="c", attestation="C", ai_voice_score=0.0), p)
    assert not any(restriction_applies(r, "account", "appointments:read") for r in weak)
    assert any(restriction_applies(r, "account", "appointments:write") for r in weak)


async def test_audio_and_transcripts_never_stored(app):
    await run(app, "phone_person")
    audit = app.state.runtime.gateway.audit.path.read_text()
    assert "checking my appointment" not in audit and "gs://ivr" not in audit
    records = [json.loads(line) for line in audit.splitlines() if line.strip()]
    sig = next(r["data"]["signals"] for r in records if r["data"].get("channel") == "voice")
    assert set(sig["dropped_fields"]) == {"transcript", "recording_url"}


async def test_speech_device_user_hears_nothing_different(app):
    result = await run(app, "phone_speech_device")
    said = result["steps"][0]["payload"]["say"].lower()
    for word in ("bot", "robot", "ai ", "artificial", "synthetic", "automated"):
        assert word not in said
    human_like = None
    async with client(app) as http:
        human_like = await turn(http, app, call_id="call-h", intent="cancel_appointment",
                                params={"appointment_id": "APT-501", "reason": "x"},
                                signals={"attestation": "A", "ai_voice_score": 0.01}, verification=RIYA)
    assert human_like["say"] == result["steps"][0]["payload"]["say"]  # same words as any caller


async def test_lane_code_links_context_but_passport_must_match(app):
    rt = app.state.runtime
    async with client(app) as http:
        offer = await turn(http, app, call_id="call-v", intent="appointment_status",
                           params={"appointment_id": "APT-777"},
                           signals={"declared_agent": True, "attestation": "A"},
                           verification={"principal_id": "P-2002", "name": "Vikram Rao"})
        code = offer["agent_lane"]["code"]
        riya = rt.world.callers(http, BASE)["riya"]  # Riya's agent, not Vikram's
        wrong = await riya.send(intent="appointment_status", params={"appointment_id": "APT-501"}, lane_code=code)
        assert wrong.outcome == "denied" and wrong.data["error"] == "lane_mismatch"
        medifetch = rt.world.callers(http, BASE)["medifetch"]  # holds a passport for Vikram
        ok = await medifetch.send(intent="appointment_status", params={"appointment_id": "APT-777"}, lane_code=code)
        assert ok.outcome == "completed"
        assert ok.payload["continued_from_call"]["call_id"] == "call-v"
        again = await medifetch.send(intent="appointment_status", params={"appointment_id": "APT-777"},
                                     lane_code=code)
        assert again.data["error"] == "lane_used"


async def test_lane_code_grants_nothing_on_its_own(app):
    rt = app.state.runtime
    async with client(app) as http:
        offer = await turn(http, app, call_id="call-x", intent="appointment_status",
                           params={"appointment_id": "APT-501"}, signals={"declared_agent": True}, verification=RIYA)
        guest = rt.world.callers(http, BASE)["guest"]  # no passport
        r = await guest.send(intent="appointment_status", params={"appointment_id": "APT-501"},
                             lane_code=offer["agent_lane"]["code"])
    assert r.outcome == "denied" and r.data["error"] == "missing_passport"


async def test_switching_lanes_withdraws_the_phone_approval(app):
    result = await run(app, "phone_honest")
    challenges = app.state.runtime.gateway.stepup.list()
    assert challenges and all(c["status"] != "pending" for c in challenges)
    assert result["steps"][1]["payload"]["continued_from_call"]["withdrew_phone_approvals"]


async def test_unverified_caller_is_asked_to_verify(app):
    async with client(app) as http:
        r = await turn(http, app, intent="cancel_appointment", params={"appointment_id": "APT-501"},
                       signals={"attestation": "A", "ai_voice_score": 0.0})
    assert r["outcome"] == "denied" and r["next"] == "verify_caller"


async def test_human_tier_on_the_phone_transfers(app):
    async with client(app) as http:
        r = await turn(http, app, intent="billing_dispute", params={"invoice_id": "INV-1", "reason": "twice"},
                       signals={"attestation": "A"}, verification=RIYA)
    assert r["outcome"] == "escalated" and r["next"] == "transfer_to_person" and r["ticket_id"]
