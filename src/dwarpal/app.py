"""Wires everything into one FastAPI app: the A2A gateway endpoint plus the demo dashboard API."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

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
from .business.clinic import build_clinic_agent
from .business.service import Actor, ClinicService
from .config import Settings
from .demo import scenarios as sc
from .demo.world import DemoWorld
from .escalation import EscalationQueue
from .events import EventBus
from .gateway import Gateway
from .guard import SessionGuard
from .identity import IdentityVerifier, KeyPair, TrustStore
from .llm import GeminiJudge
from .messages import message_hash
from .policy import TIER_LABELS, Policy
from .shield import ModelArmorClient, Shield
from .stepup import StepUpService

log = logging.getLogger("dwarpal")


@dataclass
class Runtime:
    settings: Settings
    policy: Policy
    gateway: Gateway
    service: ClinicService
    world: DemoWorld
    trust: TrustStore
    live: dict[str, dict[str, Any]] = field(default_factory=dict)  # challenge id -> pending live request

    # The patient's device approving a step-up. In production this is the identity provider calling
    # back (OpenID CIBA); here the dashboard's phone card and the demo scenarios call it.
    def approve(self, challenge_id: str, methods: list[str]) -> dict[str, Any]:
        gw = self.gateway
        c, message = gw.stepup.approve(challenge_id, methods)
        gw.audit.append("step_up_factor", challenge=c.id, methods=methods, strength=c.strength,
                        required=c.required_strength, status=c.status)
        gw.events.publish({"type": "step_up", "challenge": c.to_dict()})
        return {"challenge": c.to_dict(), "message": message}

    def decline(self, challenge_id: str) -> dict[str, Any]:
        c = self.gateway.stepup.decline(challenge_id)
        self.gateway.audit.append("step_up_declined", challenge=c.id)
        self.gateway.events.publish({"type": "step_up", "challenge": c.to_dict()})
        return {"challenge": c.to_dict(), "message": "Declined."}

    def join(self, code: str) -> dict[str, Any]:
        result, t = self.gateway.escalations.join(code)
        if t is not None and result == "ok":
            self.gateway.audit.append("customer_joined", ticket=t.id)
            self.gateway.events.publish({"type": "ticket", "ticket": t.to_dict()})
        return {"result": result, "ticket": t.to_dict() if t else None}


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

    # The business service is shared by the AI agent's tools and the human front desk.
    # The gateway never gets a reference to it.
    service = ClinicService()
    agent = build_clinic_agent(service, use_gemini=settings.use_gemini, model=settings.model)
    fallback = build_clinic_agent(service, use_gemini=False, model="") if settings.use_gemini else None
    audit = AuditLog(settings.data_dir / "audit.jsonl", gateway_key)
    service.on_write = lambda change: audit.append("business_write", **change)

    gateway = Gateway(
        policy=policy,
        verifier=IdentityVerifier(trust, policy.business_id),
        shield=Shield(judge=judge, model_armor=armor),
        guard=SessionGuard(policy.limits),
        escalations=EscalationQueue(),
        stepup=StepUpService(policy.step_up),
        audit=audit,
        backend=ADKBackend(agent, fallback),
        events=EventBus(),
    )
    return Runtime(settings=settings, policy=policy, gateway=gateway, service=service, world=world, trust=trust)


class ResolveBody(BaseModel):
    resolution: str = "Handled by front-desk staff."


class ApproveBody(BaseModel):
    methods: list[str]


class ReceiptCheckBody(BaseModel):
    receipt: str
    message: str | None = None


class DeskActionBody(BaseModel):
    staff_id: str = "desk:priya"
    action: str  # reschedule | cancel | refund_copay
    appointment_id: str
    new_slot: str | None = None
    amount_inr: float | None = None
    reason: str = ""


def create_app(settings: Settings | None = None) -> FastAPI:
    rt = build_runtime(settings)
    s, gw = rt.settings, rt.gateway
    app = FastAPI(title="Dwarpal", version=__version__,
                  description="Gate the action, not the caller: a policy and enforcement layer for AI agents "
                              "calling a business (Google ADK + A2A).")
    app.state.runtime = rt

    card = build_agent_card(rt.policy, s.public_url, gw.catalog())
    handler = DefaultRequestHandler(agent_executor=GatewayExecutor(gw), task_store=InMemoryTaskStore(),
                                    agent_card=card)
    add_a2a_routes_to_fastapi(app, agent_card_routes=create_agent_card_routes(card),
                              jsonrpc_routes=create_jsonrpc_routes(handler, "/a2a"))

    def in_process_http() -> httpx.AsyncClient:
        """Demo callers make real A2A JSON-RPC calls to this same app, in process."""
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=s.public_url, timeout=120)

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
            "tiers": TIER_LABELS,
            "intents": rt.policy.public_summary(),
            "step_up": {"required_strength": rt.policy.step_up.required_strength,
                        "methods": gw.stepup.methods_public()},
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
        return {"appointments": rt.service.appointments_view(), "open_slots": rt.service.data["open_slots"],
                "changes": rt.service.changes[-10:]}

    # ---------------------------------------------------------------- customer side

    @app.get("/api/stepup")
    async def stepup_list():
        return {"challenges": gw.stepup.list(), "methods": gw.stepup.methods_public(),
                "required_strength": rt.policy.step_up.required_strength}

    @app.post("/api/stepup/{challenge_id}/approve")
    async def stepup_approve(challenge_id: str, body: ApproveBody):
        try:
            return rt.approve(challenge_id, body.methods)
        except KeyError:
            raise HTTPException(404, "No such approval request") from None

    @app.post("/api/stepup/{challenge_id}/decline")
    async def stepup_decline(challenge_id: str):
        try:
            return rt.decline(challenge_id)
        except KeyError:
            raise HTTPException(404, "No such approval request") from None

    @app.post("/api/join/{code}")
    async def join(code: str):
        res = rt.join(code)
        if res["result"] == "unknown":
            raise HTTPException(404, "That join code isn't valid.")
        if res["result"] != "ok":
            raise HTTPException(410, f"That join code is {res['result']}.")
        t = res["ticket"]
        return {"ticket_id": t["id"], "expected_by": t["expected_by"],
                "message": f"You're connected to case {t['id']}. The team already has your assistant's request, "
                           "so you won't need to explain it again."}

    @app.post("/api/receipts/verify")
    async def receipt_verify(body: ReceiptCheckBody):
        """Check a receipt's signature and, optionally, whether a relayed message is what the business said."""
        try:
            claims = gw.audit.verify_receipt(body.receipt)
        except Exception:  # noqa: BLE001
            return {"valid": False, "reason": "Signature doesn't verify, or this isn't a Dwarpal receipt."}
        out: dict[str, Any] = {"valid": True, "claims": claims}
        if body.message is not None:
            out["message_matches"] = message_hash(body.message) == claims.get("customer_message_sha256")
        return out

    # ---------------------------------------------------------------- staff side

    @app.post("/api/desk/action")
    async def desk_action(body: DeskActionBody):
        """A person at the front desk acting through the same service the AI agent uses: same validation,
        same audit trail."""
        _demo_guard()
        actor = Actor(channel="staff", id=body.staff_id)
        if body.action == "reschedule":
            res = rt.service.reschedule(actor, body.appointment_id, body.new_slot or "")
        elif body.action == "cancel":
            res = rt.service.cancel(actor, body.appointment_id, body.reason)
        elif body.action == "refund_copay":
            res = rt.service.refund_copay(actor, body.appointment_id, body.amount_inr or 0)
        else:
            raise HTTPException(400, "Unknown action")
        return res

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
        async with in_process_http() as http:
            stage = sc.Stage(callers=rt.world.callers(http, s.public_url), approve=rt.approve, join=rt.join)
            result = await sc.run_scenario(scenario_id, stage)
        gw.events.publish({"type": "scenario_done", "scenario": {k: v for k, v in result.items() if k != "steps"}})
        return result

    @app.post("/api/live/start")
    async def live_start():
        """Stage moment: Riya's assistant asks to cancel; the audience watches Riya approve on her phone."""
        _demo_guard()
        params = {"appointment_id": "APT-501", "reason": "Riya is travelling"}
        session = sc.new_session("live")
        async with in_process_http() as http:
            riya = rt.world.callers(http, s.public_url)["riya"]
            res = await riya.send(intent="cancel_appointment", params=params, session=session, tag="live")
        cid = (res.payload.get("step_up") or {}).get("challenge_id")
        if cid:
            rt.live[cid] = {"session": session, "intent": "cancel_appointment", "params": params}
        return {"outcome": res.outcome, "challenge_id": cid, "customer_message": res.payload.get("customer_message")}

    @app.post("/api/live/{challenge_id}/resume")
    async def live_resume(challenge_id: str):
        _demo_guard()
        pending = rt.live.pop(challenge_id, None)
        if not pending:
            raise HTTPException(404, "No live request waiting on that approval.")
        async with in_process_http() as http:
            riya = rt.world.callers(http, s.public_url)["riya"]
            res = await riya.send(intent=pending["intent"], params=pending["params"], session=pending["session"],
                                  step_up=challenge_id, tag="live")
        return {"outcome": res.outcome, "reply": res.reply, "customer_message": res.payload.get("customer_message")}

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
        rt.service.reset()
        rt.live.clear()
        gw.audit.reset()
        gw.escalations.clear()
        gw.stepup.clear()
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
