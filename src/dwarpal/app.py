"""Wires everything into one FastAPI app: the A2A gateway endpoint plus the demo dashboard API."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from importlib import resources

import httpx
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__
from .a2a_server import GatewayExecutor, build_agent_card
from .audit import AuditLog
from .business.backend import ADKBackend
from .business.clinic import ClinicDB, build_clinic_agent
from .config import Settings
from .demo import scenarios as sc
from .demo.world import DemoWorld
from .escalation import EscalationQueue
from .events import EventBus
from .gateway import Gateway
from .guard import SessionGuard
from .identity import IdentityVerifier, KeyPair, TrustStore
from .llm import GeminiJudge
from .policy import Policy
from .shield import ModelArmorClient, Shield

log = logging.getLogger("dwarpal")


@dataclass
class Runtime:
    settings: Settings
    policy: Policy
    gateway: Gateway
    clinic: ClinicDB
    world: DemoWorld
    trust: TrustStore


def build_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or Settings()
    # ADK logs a full traceback when a model call fails; the backend already falls back and logs one line.
    for noisy in ("google_adk", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    policy = Policy.load(settings.policy_path)

    trust = TrustStore()
    world = DemoWorld.load(settings.data_dir, policy.business_id)
    world.trust(trust)

    gateway_key = KeyPair.load_or_create(settings.data_dir / "gateway.pem")
    judge = GeminiJudge(settings)
    armor = ModelArmorClient(settings.model_armor_template) if settings.model_armor_template else None
    clinic = ClinicDB()
    agent = build_clinic_agent(clinic, use_gemini=settings.use_gemini, model=settings.model)
    fallback = build_clinic_agent(clinic, use_gemini=False, model="") if settings.use_gemini else None

    gateway = Gateway(
        policy=policy,
        verifier=IdentityVerifier(trust, policy.business_id),
        shield=Shield(judge=judge, model_armor=armor),
        guard=SessionGuard(policy.limits),
        escalations=EscalationQueue(),
        audit=AuditLog(settings.data_dir / "audit.jsonl", gateway_key),
        backend=ADKBackend(agent, fallback),
        events=EventBus(),
    )
    return Runtime(settings=settings, policy=policy, gateway=gateway, clinic=clinic, world=world, trust=trust)


class ResolveBody(BaseModel):
    resolution: str = "Handled by front-desk staff."


def create_app(settings: Settings | None = None) -> FastAPI:
    rt = build_runtime(settings)
    s, gw = rt.settings, rt.gateway
    app = FastAPI(title="Dwarpal", version=__version__,
                  description="Trust and security gateway for inbound AI-agent traffic (Google ADK + A2A).")
    app.state.runtime = rt

    card = build_agent_card(rt.policy, s.public_url, gw.catalog())
    handler = DefaultRequestHandler(agent_executor=GatewayExecutor(gw), task_store=InMemoryTaskStore(),
                                    agent_card=card)
    add_a2a_routes_to_fastapi(app, agent_card_routes=create_agent_card_routes(card),
                              jsonrpc_routes=create_jsonrpc_routes(handler, "/a2a"))

    # ---------------------------------------------------------------- read-only API

    @app.get("/api/info")
    async def info():
        return {
            "version": __version__,
            "business": {"id": rt.policy.business_id, "name": rt.policy.business_name},
            "llm_mode": s.resolved_llm_mode, "model": s.model if s.use_gemini else "scripted (offline)",
            "shield_layers": ["rules"] + (["gemini"] if gw.shield.judge and gw.shield.judge.available else [])
                             + (["model_armor"] if gw.shield.model_armor else []),
            "trusted_issuers": [{"issuer": i.issuer, "kid": i.kid, "label": i.label} for i in rt.trust.issuers()],
            "intents": rt.policy.public_summary(),
            "limits": rt.policy.limits.__dict__,
            "demo": s.enable_demo_endpoints,
        }

    @app.get("/api/audit")
    async def audit(limit: int = 50):
        return {"records": gw.audit.records(limit), "verify": gw.audit.verify()}

    @app.get("/api/audit/verify")
    async def audit_verify():
        return gw.audit.verify()

    @app.get("/api/escalations")
    async def escalations():
        return {"tickets": gw.escalations.list()}

    @app.post("/api/escalations/{ticket_id}/resolve")
    async def resolve(ticket_id: str, body: ResolveBody):
        t = gw.escalations.resolve(ticket_id, body.resolution)
        if not t:
            raise HTTPException(404, "No such ticket")
        gw.audit.append("human_resolution", ticket=ticket_id, resolution=body.resolution)
        gw.events.publish({"type": "ticket", "ticket": t.to_dict()})
        return t.to_dict()

    @app.get("/api/decisions")
    async def decisions():
        return {"decisions": [e["decision"] for e in gw.events.history if e.get("type") == "decision"]}

    @app.get("/api/events")
    async def events():
        async def stream():
            yield "retry: 2000\n\n"
            q = gw.events.subscribe()
            try:
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(ev, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                gw.events.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/clinic")
    async def clinic():
        return {"appointments": rt.clinic.appointments_view(), "open_slots": rt.clinic.data["open_slots"]}

    # ---------------------------------------------------------------- demo-only API

    def _demo_guard():
        if not s.enable_demo_endpoints:
            raise HTTPException(404, "Demo endpoints are disabled (DWARPAL_DEMO=false).")

    @app.get("/api/scenarios")
    async def scenarios():
        return {"scenarios": sc.describe()}

    @app.post("/api/scenarios/{scenario_id}/run")
    async def run(scenario_id: str):
        _demo_guard()
        if scenario_id not in sc.BY_ID:
            raise HTTPException(404, "Unknown scenario")
        # Callers make real A2A JSON-RPC calls to this same app, in process.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=s.public_url,
                                     timeout=120) as http:
            result = await sc.run_scenario(scenario_id, rt.world.callers(http, s.public_url))
        gw.events.publish({"type": "scenario_done", "scenario": {k: v for k, v in result.items() if k != "steps"}})
        return result

    @app.post("/api/demo/tamper")
    async def tamper():
        _demo_guard()
        seq = gw.audit.tamper_for_demo()
        verdict = gw.audit.verify()
        gw.events.publish({"type": "audit", "verify": verdict, "tampered_seq": seq})
        return {"tampered_seq": seq, "verify": verdict}

    @app.post("/api/demo/reset")
    async def reset():
        _demo_guard()
        rt.clinic.reset()
        gw.audit.reset()
        gw.escalations.clear()
        gw.guard.sessions.clear()
        gw.guard._rate.clear()
        gw.history.clear()
        gw.events.history.clear()
        gw.events.publish({"type": "reset"})
        return {"ok": True}

    # ---------------------------------------------------------------- dashboard

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        return resources.files("dwarpal.web").joinpath("index.html").read_text(encoding="utf-8")

    return app
