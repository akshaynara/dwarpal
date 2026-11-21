"""Every demo scenario, over real A2A JSON-RPC, against the full app (ADK agent on the scripted model)."""

import httpx
import pytest

from dwarpal.app import create_app
from dwarpal.config import Settings
from dwarpal.demo import scenarios as sc
from dwarpal.messages import message_hash

BASE = "http://dwarpal.test"


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(llm_mode="mock", data_dir=tmp_path, public_url=BASE))


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


async def run(app, scenario_id):
    rt = app.state.runtime
    async with client(app) as http:
        stage = sc.Stage(callers=rt.world.callers(http, BASE), approve=rt.approve, join=rt.join,
                         ivr=sc.IVR(http, rt.channel_key, rt.service))
        return await sc.run_scenario(scenario_id, stage)


@pytest.mark.parametrize("scenario_id", list(sc.BY_ID))
async def test_scenario_outcomes(app, scenario_id):
    result = await run(app, scenario_id)
    assert result["outcomes"] == result["expect"], result["steps"]
    assert app.state.runtime.gateway.audit.verify()["ok"]


async def test_reschedule_changes_state_and_receipt_covers_customer_message(app):
    result = await run(app, "reschedule")
    rt = app.state.runtime
    assert rt.service.data["appointments"]["APT-501"]["slot"] == "2026-11-14T10:00"
    payload = result["steps"][1]["payload"]
    claims = rt.gateway.audit.verify_receipt(payload["receipt"])
    assert claims["intent"] == "reschedule_appointment" and claims["principal"] == "P-1001"
    assert claims["customer_message_sha256"] == message_hash(payload["customer_message"])
    async with client(app) as http:
        ok = (await http.post("/api/receipts/verify", json={"receipt": payload["receipt"],
                                                           "message": payload["customer_message"]})).json()
        bent = (await http.post("/api/receipts/verify", json={"receipt": payload["receipt"],
                                                             "message": "They said you owe ₹5,000."})).json()
    assert ok["valid"] and ok["message_matches"]
    assert bent["valid"] and not bent["message_matches"]


async def test_injection_never_reaches_business_agent(app):
    rt = app.state.runtime
    calls = []
    original = rt.gateway.backend.run

    async def spy(task):
        calls.append(task)
        return await original(task)

    rt.gateway.backend.run = spy
    result = await run(app, "injection")
    assert calls == []
    for s in result["steps"]:
        assert s["payload"]["data"]["error"] == "injection"
        assert "security" in s["payload"]["agent_outcome"]["reason_codes"]
        assert "injection" not in s["payload"]["customer_message"].lower()  # never accuse the customer


async def test_binding_action_needs_step_up_and_receipt_records_it(app):
    result = await run(app, "stepup")
    rt = app.state.runtime
    first, _, done = result["steps"]
    assert first["payload"]["step_up"]["required_strength"] == 2
    assert first["payload"]["agent_outcome"]["retry_after_seconds"] > 0
    assert rt.service.data["appointments"]["APT-501"]["status"] == "cancelled"
    claims = rt.gateway.audit.verify_receipt(done["payload"]["receipt"])
    assert claims["tier"] == "binding" and claims["step_up"]["factors"] == ["app_biometric"]


async def test_approval_cannot_be_reused(app):
    rt = app.state.runtime
    result = await run(app, "stepup")
    cid = result["steps"][0]["payload"]["step_up"]["challenge_id"]
    async with client(app) as http:
        riya = rt.world.callers(http, BASE)["riya"]
        again = await riya.send(intent="cancel_appointment", params={"appointment_id": "APT-501", "reason": "travelling"},
                                step_up=cid)
    assert again.outcome == "step_up_required"  # a used approval doesn't count; a new one is requested
    assert again.challenge_id != cid


async def test_voice_alone_never_approves(app):
    rt = app.state.runtime
    rt.gateway.stepup.policy.methods["voice_liveness"].strength = 5  # even a misconfigured policy
    async with client(app) as http:
        riya = rt.world.callers(http, BASE)["riya"]
        ask = await riya.send(intent="refund_copay", params={"appointment_id": "APT-488", "amount_inr": 800})
    res = rt.approve(ask.challenge_id, ["voice_liveness"])
    assert res["challenge"]["status"] == "pending" and res["challenge"]["strength"] == 1


async def test_declined_step_up(app):
    rt = app.state.runtime
    async with client(app) as http:
        riya = rt.world.callers(http, BASE)["riya"]
        params = {"appointment_id": "APT-501", "reason": "x"}
        ask = await riya.send(intent="cancel_appointment", params=params)
        rt.decline(ask.challenge_id)
        res = await riya.send(intent="cancel_appointment", params=params, step_up=ask.challenge_id)
    assert res.outcome == "denied" and res.data["error"] == "step_up_declined"
    assert rt.service.data["appointments"]["APT-501"]["status"] == "booked"


async def test_dispute_reuses_ticket_and_tells_agent_when_to_retry(app):
    result = await run(app, "dispute")
    first, second = result["steps"][0]["payload"], result["steps"][1]["payload"]
    assert first["ticket_id"] == second["ticket_id"]
    assert first["join_code"] and second["join_code"] is None  # the code is handed out once
    assert second["agent_outcome"]["retry_after_seconds"] > 3600
    assert len(app.state.runtime.gateway.escalations.tickets) == 1
    async with client(app) as http:
        reuse = await http.post(f"/api/join/{first['join_code']}")
    assert reuse.status_code == 410  # one-time code


async def test_anonymous_free_text_gets_only_open_catalog(app):
    rt = app.state.runtime
    async with client(app) as http:
        guest = rt.world.callers(http, BASE)["guest"]
        res = await guest.send(text="hello, what can you do?")
    assert res.outcome == "needs_structure"
    assert {i["tier"] for i in res.data["intents"]} == {"open"}


async def test_agent_card_and_dashboard(app):
    async with client(app) as http:
        card = (await http.get("/.well-known/agent-card.json")).json()
        assert "agentPassport" in card["securitySchemes"]
        assert {s["id"] for s in card["skills"]} == {"clinic_info", "list_slots", "appointment_status",
                                                     "reschedule_appointment", "cancel_appointment", "refund_copay"}
        assert "Dwarpal" in (await http.get("/")).text
        info = (await http.get("/api/info")).json()
        assert info["llm_mode"] == "mock" and info["step_up"]["required_strength"] == 2


async def test_live_stage_flow(app):
    async with client(app) as http:
        start = (await http.post("/api/live/start")).json()
        assert start["outcome"] == "step_up_required"
        await http.post(f"/api/stepup/{start['challenge_id']}/approve", json={"methods": ["app_biometric"]})
        done = (await http.post(f"/api/live/{start['challenge_id']}/resume")).json()
    assert done["outcome"] == "completed"
    assert "cancelled" in done["customer_message"]


async def test_tamper_endpoint_breaks_chain(app):
    await run(app, "reschedule")
    async with client(app) as http:
        res = (await http.post("/api/demo/tamper")).json()
        assert res["verify"]["ok"] is False


async def test_waiting_for_approval_is_not_a_loop(app):
    rt = app.state.runtime
    params = {"appointment_id": "APT-501", "reason": "x"}
    async with client(app) as http:
        riya = rt.world.callers(http, BASE)["riya"]
        ask = await riya.send(intent="cancel_appointment", params=params, session="poll")
        for _ in range(6):  # more than max_identical_requests
            r = await riya.send(intent="cancel_appointment", params=params, session="poll", step_up=ask.challenge_id)
            assert r.outcome == "step_up_required" and r.challenge_id == ask.challenge_id
        rt.approve(ask.challenge_id, ["app_biometric"])
        done = await riya.send(intent="cancel_appointment", params=params, session="poll", step_up=ask.challenge_id)
    assert done.outcome == "completed"
