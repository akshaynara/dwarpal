"""The payor desk: rails per plan, provider-scoped lookups, business handoff, and two businesses on one gateway."""

import httpx
import pytest

from dwarpal.app import create_app, stage_for
from dwarpal.business.payor import Caller
from dwarpal.config import Settings
from dwarpal.demo import scenarios as sc
from dwarpal.demo.payor_scenarios import ASHA, MEERA, PAYOR_SCENARIOS, ROHAN

BASE = "http://dwarpal.test"
SUNRISE = Caller("agent", "agent:sunrise-rcm", "1234567893")
LAKESIDE = Caller("agent", "agent:lakeside-pa", "1245319599")


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(llm_mode="mock", data_dir=tmp_path, public_url=BASE))


@pytest.fixture
def svc(app):
    """The payor service as the app wires it: FHIR rail pointed at the bundled PAS server."""
    return app.state.runtimes["payor"].service


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


@pytest.mark.parametrize("scenario_id", [s.id for s in PAYOR_SCENARIOS])
async def test_payor_scenarios(app, scenario_id):
    rt = app.state.runtimes["payor"]
    async with client(app) as http:
        result = await sc.run_scenario(scenario_id, stage_for(rt, http), PAYOR_SCENARIOS)
    assert result["outcomes"] == result["expect"], result["steps"]
    assert rt.gateway.audit.verify()["ok"]


async def test_rails_follow_the_plan(svc):
    assert (await svc.pa_status(SUNRISE, **ASHA))["rail"] == "fhir_pas"     # Medicare Advantage
    assert (await svc.pa_status(SUNRISE, **ROHAN))["rail"] == "x12_278"     # commercial: no FHIR API
    legacy = await svc.pa_status(SUNRISE, **MEERA)
    assert legacy["status"] == "needs_human" and legacy["rail"] == "human"


async def test_rail_order_is_configuration(svc):
    svc.rails_by_plan["MA"]["pa_status"] = ["x12_278", "fhir_pas"]
    assert (await svc.pa_status(SUNRISE, **ASHA))["rail"] == "x12_278"
    svc.rails_by_plan["MA"]["pa_status"] = []
    assert (await svc.pa_status(SUNRISE, **ASHA))["status"] == "needs_human"


async def test_fhir_outage_falls_through_and_all_rails_down_goes_to_a_person(svc):
    svc.fhir_down = True
    r = await svc.pa_status(SUNRISE, **ASHA)
    assert r["rail"] == "x12_278" and "HTTP 503" in r["rail_trace"][0]
    svc.rails_by_plan["MA"]["pa_status"] = ["fhir_pas"]
    r = await svc.pa_status(SUNRISE, **ASHA)
    assert r["status"] == "needs_human" and "electronic rails answered" in r["message"]


async def test_fhir_rail_needs_the_token(svc):
    svc.fhir_token = "wrong"
    r = await svc.pa_status(SUNRISE, **ASHA)
    assert r["rail"] == "x12_278" and "HTTP 401" in r["rail_trace"][0]


async def test_pas_server_speaks_fhir(app):
    from dwarpal.fhir.pas_server import build_inquiry, read_review_action

    svc = app.state.runtimes["payor"].service
    async with client(app) as http:
        meta = (await http.get("/payor/fhir/metadata")).json()
        assert meta["resourceType"] == "CapabilityStatement" and meta["fhirVersion"] == "4.0.1"
        bundle = build_inquiry(auth_number="PA-90001", member_id="M-55102", member_dob="1958-03-11",
                               npi="1234567893", cpt="72148")
        r = await http.post("/payor/fhir/Claim/$inquire", json=bundle,
                            headers={"Authorization": f"Bearer {svc.fhir_token}"})
        assert r.headers["content-type"].startswith("application/fhir+json")
        assert read_review_action(r.json()) == ("A1", "complete")
        other = build_inquiry(auth_number="PA-90001", member_id="M-55102", member_dob="1958-03-11", npi="1245319599")
        r = await http.post("/payor/fhir/Claim/$inquire", json=other,
                            headers={"Authorization": f"Bearer {svc.fhir_token}"})
        assert r.status_code == 404 and r.json()["resourceType"] == "OperationOutcome"


async def test_only_your_own_auths_and_no_probing(svc):
    other = await svc.pa_status(LAKESIDE, **ASHA)
    wrong_dob = await svc.pa_status(SUNRISE, **{**ASHA, "member_dob": "1900-01-01"})
    missing = await svc.pa_status(SUNRISE, auth_number="PA-00000", member_id="M-55102", member_dob="1958-03-11")
    assert other["status"] == wrong_dob["status"] == missing["status"] == "not_found"
    assert other["message"] == wrong_dob["message"]  # identical, so nothing can be inferred
    assert (await svc.pa_status(Caller("staff", "rep:1"), **ASHA))["status"] == "ok"


async def test_phi_never_leaves(app):
    rt = app.state.runtimes["payor"]
    async with client(app) as http:
        sunrise = rt.world.callers(http, BASE + "/payor")["sunrise"]
        pa = await sunrise.send(intent="pa_status", params=ASHA)
        el = await sunrise.send(intent="eligibility_benefits",
                                params={"member_id": "M-77310", "member_dob": "1979-07-22", "service_type": "30"})
    for r in (pa, el):
        blob = str(r.payload)
        for leak in ("M54.16", "XXX-XX-", "Hill St", "Asha Verma", "Rohan Das"):
            assert leak not in blob
    assert pa.data["rail"] == "fhir_pas" and pa.data["member"] == "A. V."


async def test_two_businesses_one_gateway(app):
    async with client(app) as http:
        clinic = (await http.get("/.well-known/agent-card.json")).json()
        payor = (await http.get("/payor/.well-known/agent-card.json")).json()
        assert clinic["name"].startswith("Sunrise") and payor["name"].startswith("Brightline")
        assert payor["supportedInterfaces"][0]["url"].endswith("/payor/a2a")
        assert {s["id"] for s in payor["skills"]} == {"pa_requirements", "plan_contacts", "pa_status",
                                                      "eligibility_benefits"}
        info = (await http.get("/payor/api/info")).json()
        assert info["business"]["slug"] == "payor" and not info["live"]
        assert {b["slug"] for b in info["businesses"]} == {"clinic", "payor"}
        assert "Prior authorisations" == (await http.get("/payor/api/book")).json()["title"]
        assert (await http.get("/payor/")).status_code == 200


async def test_a_clinic_passport_is_useless_at_the_payor(app):
    clinic_rt, payor_rt = app.state.runtimes["clinic"], app.state.runtimes["payor"]
    async with client(app) as http:
        riya = clinic_rt.world.callers(http, BASE + "/payor")["riya"]  # passport audience is the clinic
        r = await riya.send(intent="pa_status", params=ASHA)
    assert r.outcome == "denied" and r.data["error"] in {"untrusted_issuer", "wrong_audience"}
    assert payor_rt.gateway.audit.verify()["ok"]


async def test_audit_logs_are_separate(app, tmp_path):
    assert app.state.runtimes["clinic"].gateway.audit.path != app.state.runtimes["payor"].gateway.audit.path
