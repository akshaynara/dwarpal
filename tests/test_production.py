"""Production mode: admin auth, IdP-only approvals, policy-configured issuers, persistence across restarts."""

import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

from dwarpal.app import create_app
from dwarpal.config import Settings
from dwarpal.demo.callers import CallerAgent
from dwarpal.identity import KeyPair, Principal, issue_passport
from dwarpal.policy import Policy

BASE = "http://dwarpal.test"


def client(app, **kw):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, **kw)


def prod_settings(tmp_path, **kw):
    return Settings(llm_mode="mock", data_dir=tmp_path, public_url=BASE, mode="production", admin_token="adm1n",
                    enable_demo_endpoints=False, idp_key="idp-secret", **kw)


def test_production_needs_an_admin_token(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(Settings(llm_mode="mock", data_dir=tmp_path, mode="production", admin_token=None))


async def test_admin_api_needs_the_token(tmp_path):
    app = create_app(prod_settings(tmp_path), businesses=["clinic"])
    async with client(app) as http:
        assert (await http.get("/api/audit")).status_code == 401
        assert (await http.get("/api/audit", headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await http.get("/api/audit", headers={"Authorization": "Bearer adm1n"})).status_code == 200
        assert (await http.get("/api/metrics?access_token=adm1n")).status_code == 200
        assert (await http.get("/")).status_code == 200            # the page itself holds no data
        assert (await http.get("/.well-known/agent-card.json")).status_code == 200
        assert (await http.post("/api/receipts/verify", json={"receipt": "x"})).status_code == 200  # public
        # demo endpoints are off in production, even with the token
        r = await http.post("/api/scenarios/reschedule/run", headers={"Authorization": "Bearer adm1n"})
        assert r.status_code == 404


async def test_production_trusts_only_policy_issuers(tmp_path):
    app = create_app(prod_settings(tmp_path), businesses=["clinic"])
    rt = app.state.runtime
    async with client(app) as http:
        riya = rt.world.callers(http, BASE)["riya"]  # demo issuer: not trusted in production
        r = await riya.send(intent="appointment_status", params={"appointment_id": "APT-501"})
    assert r.outcome == "denied" and r.data["error"] == "untrusted_issuer"


async def test_pilot_workflow_with_a_policy_issuer_and_idp_approvals(tmp_path):
    issuer, agent = KeyPair.generate(), KeyPair.generate()
    raw = yaml.safe_load(Path(Policy.load.__code__.co_filename).parent.joinpath(
        "policies/sunrise_clinic.yaml").read_text())
    raw["trusted_issuers"] = [{"id": "idp.real.example", "label": "Real IdP", "public_key": issuer.public_b64}]
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(yaml.safe_dump(raw))
    app = create_app(prod_settings(tmp_path, policy_path=policy_file), businesses=["clinic"])
    passport = issue_passport(issuer, issuer="idp.real.example", agent_key=agent, agent_id="agent:pilot",
                              agent_name="Pilot agent", principal=Principal("P-1001", "Riya Sharma"),
                              audience="clinic.sunrise.example", scopes=["appointments:read", "appointments:write"])
    async with client(app) as http:
        caller = CallerAgent(name="Pilot agent", key=agent, passport=passport, audience="clinic.sunrise.example",
                             http=http, gateway_url=BASE)
        ok = await caller.send(intent="appointment_status", params={"appointment_id": "APT-501"})
        assert ok.outcome == "completed"
        params = {"appointment_id": "APT-501", "reason": "x"}
        ask = await caller.send(intent="cancel_appointment", params=params, session="s1")
        cid = ask.challenge_id
        # the dashboard (admin token) can't approve in production; only the identity provider can
        r = await http.post(f"/api/stepup/{cid}/approve", json={"methods": ["app_biometric"]},
                            headers={"Authorization": "Bearer adm1n"})
        assert r.status_code == 401
        r = await http.post(f"/api/stepup/{cid}/approve", json={"methods": ["app_biometric"]},
                            headers={"X-Dwarpal-IdP-Key": "idp-secret"})
        assert r.status_code == 200 and r.json()["challenge"]["status"] == "approved"
        done = await caller.send(intent="cancel_appointment", params=params, session="s1", step_up=cid)
    assert done.outcome == "completed"


async def test_state_survives_a_restart(tmp_path):
    settings = Settings(llm_mode="mock", data_dir=tmp_path, public_url=BASE)
    app = create_app(settings, businesses=["clinic"])
    rt = app.state.runtime
    async with client(app) as http:
        callers = rt.world.callers(http, BASE)
        ticket = await callers["disputebot"].send(intent="billing_dispute",
                                                  params={"invoice_id": "INV-1", "reason": "twice"})
        ask = await callers["riya"].send(intent="cancel_appointment", params={"appointment_id": "APT-501", "reason": "x"})
        rt.approve(ask.challenge_id, ["app_biometric"])
        sent = callers["riya"]
    # "restart": a new app on the same data directory
    app2 = create_app(Settings(llm_mode="mock", data_dir=tmp_path, public_url=BASE), businesses=["clinic"])
    rt2 = app2.state.runtime
    assert ticket.payload["ticket_id"] in rt2.gateway.escalations.tickets
    assert rt2.gateway.stepup.challenges[ask.challenge_id].status == "approved"
    async with client(app2) as http2:
        sent.http = http2
        sent._client = None
        replay = await sent.replay_last()  # the nonce was persisted, so a replay after restart still fails
    assert replay.outcome == "denied" and replay.data["error"] == "replay"


def test_keygen_and_issue_passport_cli(tmp_path):
    exe = [sys.executable, "-m", "dwarpal.cli"]
    out = subprocess.run(exe + ["keygen", "--out", str(tmp_path / "issuer.pem")], capture_output=True, text=True)
    assert out.returncode == 0 and "public_key:" in out.stdout
    subprocess.run(exe + ["keygen", "--out", str(tmp_path / "agent.pem")], capture_output=True, text=True, check=True)
    tok = subprocess.run(exe + ["issue-passport", "--issuer-key", str(tmp_path / "issuer.pem"), "--issuer", "idp.x",
                                "--audience", "clinic.sunrise.example", "--agent-id", "agent:a",
                                "--agent-key", str(tmp_path / "agent.pem"), "--principal-id", "P-1001",
                                "--scopes", "appointments:read"], capture_output=True, text=True, check=True)
    assert tok.stdout.count(".") == 2
