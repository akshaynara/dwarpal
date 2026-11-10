"""Every demo scenario, over real A2A JSON-RPC, against the full app (ADK agent on the scripted model)."""

import httpx
import pytest

from dwarpal.app import create_app
from dwarpal.config import Settings
from dwarpal.demo import scenarios as sc


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(llm_mode="mock", data_dir=tmp_path, public_url="http://dwarpal.test"))


async def run(app, scenario_id):
    rt = app.state.runtime
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dwarpal.test") as http:
        return await sc.run_scenario(scenario_id, rt.world.callers(http, "http://dwarpal.test"))


@pytest.mark.parametrize("scenario_id", list(sc.BY_ID))
async def test_scenario_outcomes(app, scenario_id):
    result = await run(app, scenario_id)
    assert result["outcomes"] == result["expect"], result["steps"]
    assert app.state.runtime.gateway.audit.verify()["ok"]


async def test_reschedule_changes_clinic_state_and_returns_verifiable_receipt(app):
    result = await run(app, "reschedule")
    rt = app.state.runtime
    apt = rt.clinic.data["appointments"]["APT-501"]
    assert apt["slot"] == "2026-11-14T10:00"
    receipt = result["steps"][1]["payload"]["receipt"]
    claims = rt.gateway.audit.verify_receipt(receipt)
    assert claims["intent"] == "reschedule_appointment" and claims["principal"] == "P-1001"


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
    assert all(s["payload"]["data"]["error"] == "injection" for s in result["steps"])


async def test_dispute_reuses_ticket(app):
    result = await run(app, "dispute")
    t1, t2 = (s["payload"]["ticket_id"] for s in result["steps"])
    assert t1 == t2
    assert len(app.state.runtime.gateway.escalations.tickets) == 1


async def test_agent_card_and_dashboard(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dwarpal.test") as http:
        card = (await http.get("/.well-known/agent-card.json")).json()
        assert "agentPassport" in card["securitySchemes"]
        assert {s["id"] for s in card["skills"]} == {"appointment_status", "list_slots", "reschedule_appointment",
                                                     "cancel_appointment", "refund_copay"}
        assert "Dwarpal" in (await http.get("/")).text
        assert (await http.get("/api/info")).json()["llm_mode"] == "mock"


async def test_tamper_endpoint_breaks_chain(app):
    await run(app, "reschedule")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dwarpal.test") as http:
        res = (await http.post("/api/demo/tamper")).json()
        assert res["verify"]["ok"] is False
