"""Wires everything into one FastAPI app. Each protected business is mounted under its own prefix with its
own A2A agent card and endpoint, voice endpoint, dashboard API and dashboard."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

import httpx
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__
from .a2a_server import GatewayExecutor, build_agent_card
from .audit import AuditLog
from .business.backend import ADKBackend
from .business.service import Actor
from .businesses import CLINIC, KITS, BusinessKit
from .config import Settings
from .demo import scenarios as sc
from .escalation import EscalationQueue
from .events import EventBus
from .gateway import Gateway
from .guard import SessionGuard
from .identity import IdentityVerifier, KeyPair, TrustStore, public_key_from_b64
from .llm import GeminiJudge
from .messages import message_hash
from .policy import TIER_LABELS, Policy
from .shield import ModelArmorClient, Shield
from .httpsig import HttpSignatureMiddleware
from .store import Store
from .stepup import StepUpService
from .voice import LaneService, VoiceContext, sanitise

log = logging.getLogger("dwarpal")


@dataclass
class Runtime:
    settings: Settings
    kit: BusinessKit
    policy: Policy
    gateway: Gateway
    service: Any
    world: Any
    trust: TrustStore
    live: dict[str, dict[str, Any]] = field(default_factory=dict)  # challenge id -> pending live request
    channel_key: str = ""  # shared secret for the business's own voice platform

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


def build_runtime(settings: Settings | None = None, kit: BusinessKit = CLINIC) -> Runtime:
    settings = settings or Settings()
    # ADK logs a full traceback when a model call fails; the backend already falls back and logs one line.
    for noisy in ("google_adk", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)
    # The default business keeps the top-level data directory; others get a subfolder each.
    data_dir = settings.data_dir if kit.prefix == "" else settings.data_dir / kit.slug
    data_dir.mkdir(parents=True, exist_ok=True)
    policy_path = settings.policy_path if (kit is CLINIC and settings.policy_path) else kit.policy_file
    policy = Policy.load(policy_path)

    trust = TrustStore()
    # Issuers with a public key in the policy file are always trusted. Demo issuers only in demo mode.
    for iss in policy.trusted_issuers:
        if iss.get("public_key"):
            trust.add(iss["id"], public_key_from_b64(iss["public_key"]), label=iss.get("label", ""))
    world = kit.build_world(data_dir, policy.business_id)
    if not settings.production:
        world.trust(trust)
    store = Store(data_dir / "state.db") if settings.state == "sqlite" else None

    gateway_key = KeyPair.load_or_create(data_dir / "gateway.pem")
    judge = GeminiJudge(settings)
    armor = ModelArmorClient(settings.model_armor_template) if settings.model_armor_template else None

    # The business service is shared by the AI agent's tools and the human front desk.
    # The gateway never gets a reference to it.
    service = kit.build_service(policy)
    agent = kit.build_agent(service, use_gemini=settings.use_gemini, model=settings.model)
    fallback = kit.build_agent(service, use_gemini=False, model="") if settings.use_gemini else None
    audit = AuditLog(data_dir / "audit.jsonl", gateway_key)
    event = getattr(service, "audit_event", "business_write")
    service.on_write = lambda change: audit.append(event, **change)

    verifier = IdentityVerifier(trust, policy.business_id)
    verifier.nonces.store = store
    gateway = Gateway(
        policy=policy,
        verifier=verifier,
        shield=Shield(judge=judge, model_armor=armor),
        guard=SessionGuard(policy.limits),
        escalations=EscalationQueue(store),
        stepup=StepUpService(policy.step_up, store),
        audit=audit,
        backend=ADKBackend(agent, fallback, timeout=settings.llm_timeout if settings.use_gemini else None),
        events=EventBus(),
        lanes=LaneService(policy.voice, store),
    )
    key_file = data_dir / "channel.key"
    channel_key = os.getenv("DWARPAL_CHANNEL_KEY") or (key_file.read_text().strip() if key_file.exists() else "")
    if not channel_key:
        channel_key = secrets.token_urlsafe(24)
        key_file.write_text(channel_key)
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
    return Runtime(settings=settings, kit=kit, policy=policy, gateway=gateway, service=service, world=world,
                   trust=trust, channel_key=channel_key)


class ResolveBody(BaseModel):
    resolution: str = "Handled by front-desk staff."


class ApproveBody(BaseModel):
    methods: list[str]


class ReceiptCheckBody(BaseModel):
    receipt: str
    message: str | None = None


class VoiceSignals(BaseModel):
    attestation: str = "none"           # STIR/SHAKEN level from the carrier: A | B | C | none
    ai_voice_score: float | None = None  # from an AI-voice / deepfake detection vendor, 0..1
    declared_agent: bool = False         # caller pressed 1 or said it is an AI agent


class VoiceVerification(BaseModel):
    principal_id: str
    name: str | None = None
    method: str = "patient_id+dob"


class VoiceTurn(BaseModel):
    """One turn of a phone call, sent by the business's voice platform (IVR, CX Agent Studio tool/webhook)."""
    model_config = {"extra": "allow"}
    call_id: str
    intent: str | None = None
    params: dict[str, Any] = {}
    signals: VoiceSignals = VoiceSignals()
    verification: VoiceVerification | None = None  # set once the IVR has verified the patient
    step_up: str | None = None


VOICE_NEXT = {"completed": "continue", "rejected": "continue", "step_up_required": "collect_step_up",
              "escalated": "transfer_to_person", "needs_structure": "continue", "error": "transfer_to_person"}


class DeskActionBody(BaseModel):
    staff_id: str = "desk:priya"
    action: str  # reschedule | cancel | refund_copay
    appointment_id: str
    new_slot: str | None = None
    amount_inr: float | None = None
    reason: str = ""


def create_app(settings: Settings | None = None, businesses: list[str] | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.check()
    slugs = businesses or [b.strip() for b in os.getenv("DWARPAL_BUSINESSES", "clinic,payor").split(",") if b.strip()]
    app = FastAPI(title="Dwarpal", version=__version__,
                  description="Gate the action, not the caller: a policy and enforcement layer for AI agents "
                              "calling a business, on the phone or over A2A (Google ADK).")
    app.add_middleware(HttpSignatureMiddleware)  # RFC 9421 checks on every */a2a endpoint
    if settings.admin_token:
        app.add_middleware(AdminAuthMiddleware, token=settings.admin_token)
    runtimes = {slug: build_runtime(settings, KITS[slug]) for slug in slugs}
    app.state.runtimes = runtimes
    app.state.runtime = runtimes.get("clinic") or next(iter(runtimes.values()))

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True, "version": __version__,
                "businesses": {slug: rt.gateway.audit.verify()["ok"] for slug, rt in runtimes.items()}}
    for rt in runtimes.values():
        mount(app, rt, runtimes)
    return app


def mount(app: FastAPI, rt: Runtime, runtimes: dict[str, Runtime]) -> None:
    s, gw, kit = rt.settings, rt.gateway, rt.kit
    prefix = kit.prefix
    base_url = s.public_url.rstrip("/") + prefix
    router = APIRouter(prefix=prefix)

    card = build_agent_card(rt.policy, base_url, gw.catalog())
    handler = DefaultRequestHandler(agent_executor=GatewayExecutor(gw), task_store=InMemoryTaskStore(),
                                    agent_card=card)
    add_a2a_routes_to_fastapi(
        app, agent_card_routes=create_agent_card_routes(card, card_url=f"{prefix}/.well-known/agent-card.json"),
        jsonrpc_routes=create_jsonrpc_routes(handler, f"{prefix}/a2a"))

    def in_process_http() -> httpx.AsyncClient:
        """Demo callers make real A2A JSON-RPC calls to this same app, in process."""
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=s.public_url, timeout=120)

    # ---------------------------------------------------------------- read-only API

    @router.get("/api/info")
    async def info():
        return {
            "version": __version__,
            "business": {"id": rt.policy.business_id, "name": rt.policy.business_name, "slug": kit.slug},
            "businesses": [{"slug": r.kit.slug, "name": r.policy.business_name, "href": (r.kit.prefix or "") + "/"}
                           for r in runtimes.values()],
            "live": kit.live,
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

    @router.get("/api/audit")
    async def audit(limit: int = 50):
        return {"records": gw.audit.records(limit), "verify": gw.audit.verify()}

    @router.get("/api/audit/verify")
    async def audit_verify():
        return gw.audit.verify()

    @router.get("/api/escalations")
    async def escalations():
        return {"tickets": gw.escalations.list()}

    @router.post("/api/escalations/{ticket_id}/resolve")
    async def resolve(ticket_id: str, body: ResolveBody):
        t = gw.escalations.resolve(ticket_id, body.resolution)
        if not t:
            raise HTTPException(404, "No such ticket")
        gw.audit.append("human_resolution", ticket=ticket_id, resolution=body.resolution)
        gw.events.publish({"type": "ticket", "ticket": t.to_dict()})
        return t.to_dict()

    @router.get("/api/decisions")
    async def decisions():
        return {"decisions": [e["decision"] for e in gw.events.history if e.get("type") == "decision"]}

    @router.get("/api/events")
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

    @router.get("/api/metrics")
    async def metrics():
        """Pilot metrics, computed from the signed audit chain."""
        from .metrics import compute

        return compute(gw.audit.records(), avg_call_minutes=float(
            (rt.policy.raw.get("metrics") or {}).get("avg_call_minutes", 6)))

    @router.get("/api/book")
    async def book():
        """The business's own records, for the dashboard."""
        return rt.service.records_view()

    # ---------------------------------------------------------------- phone lane

    @router.post("/voice/turn")
    async def voice_turn(raw: dict[str, Any], x_dwarpal_channel_key: str | None = Header(default=None)):
        """The phone lane. Only the business's own voice platform may call this (channel key), because it
        vouches for what the IVR verified. Callers never reach it directly."""
        if not x_dwarpal_channel_key or not hmac.compare_digest(x_dwarpal_channel_key, rt.channel_key):
            raise HTTPException(401, "Unknown voice channel.")
        clean, dropped = sanitise(raw, rt.policy.voice)
        try:
            turn = VoiceTurn.model_validate(clean)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(422, str(exc)) from None
        vc = VoiceContext(
            call_id=turn.call_id, attestation=turn.signals.attestation, ai_voice_score=turn.signals.ai_voice_score,
            declared_agent=turn.signals.declared_agent,
            verified_principal=turn.verification.principal_id if turn.verification else None,
            verified_name=turn.verification.name if turn.verification else None,
            verification_method=turn.verification.method if turn.verification else None,
            dropped_fields=dropped)
        from .gateway import InboundRequest  # local import keeps app wiring readable

        body = {"intent": turn.intent, "params": turn.params} if turn.intent else None
        d = await gw.handle(InboundRequest(session_id=turn.call_id, body=body, channel="voice", voice=vc,
                                           step_up=turn.step_up, tag=clean.get("tag")))
        nxt = VOICE_NEXT.get(d.outcome, "continue")
        if d.outcome == "denied" and (d.data or {}).get("error") == "needs_verification":
            nxt = "verify_caller"
        return {
            "say": d.customer_message,             # spoken to the caller, written by the business
            "next": nxt,
            "reply": d.reply,                       # for the voice platform's logs, not spoken
            "outcome": d.outcome,
            "tier": d.tier,
            "step_up": d.step_up,
            "agent_lane": d.agent_lane,
            "ticket_id": d.ticket_id,
            "receipt": d.receipt,
            "reason_codes": d.agent_outcome.get("reason_codes"),
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in d.checks],
            "request_id": d.request_id,
        }

    @router.get("/api/lanes")
    async def lanes():
        return {"lanes": gw.lanes.list()}

    # ---------------------------------------------------------------- customer side

    @router.get("/api/stepup")
    async def stepup_list():
        return {"challenges": gw.stepup.list(), "methods": gw.stepup.methods_public(),
                "required_strength": rt.policy.step_up.required_strength}

    def _approval_allowed(request: Request) -> None:
        """Approvals come from the customer's identity provider (its key), or from the demo phone card."""
        idp = request.headers.get("x-dwarpal-idp-key", "")
        if s.idp_key and hmac.compare_digest(idp, s.idp_key):
            return
        if s.enable_demo_endpoints and (not s.admin_token or _bearer_ok(request, s.admin_token)):
            return
        raise HTTPException(401, "Approvals must come from the customer's identity provider.")

    @router.post("/api/stepup/{challenge_id}/approve")
    async def stepup_approve(challenge_id: str, body: ApproveBody, request: Request):
        _approval_allowed(request)
        try:
            return rt.approve(challenge_id, body.methods)
        except KeyError:
            raise HTTPException(404, "No such approval request") from None

    @router.post("/api/stepup/{challenge_id}/decline")
    async def stepup_decline(challenge_id: str, request: Request):
        _approval_allowed(request)
        try:
            return rt.decline(challenge_id)
        except KeyError:
            raise HTTPException(404, "No such approval request") from None

    @router.post("/api/join/{code}")
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

    @router.post("/api/receipts/verify")
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

    @router.post("/api/desk/action", include_in_schema=kit.slug == "clinic")
    async def desk_action(body: DeskActionBody):
        """A person at the front desk acting through the same service the AI agent uses: same validation,
        same audit trail."""
        _demo_guard()
        if kit.slug != "clinic":
            raise HTTPException(404, "No staff desk for this business in the demo.")
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

    @router.get("/api/scenarios")
    async def scenarios():
        return {"scenarios": sc.describe(kit.scenarios)}

    @router.post("/api/scenarios/{scenario_id}/run")
    async def run(scenario_id: str):
        _demo_guard()
        if scenario_id not in {x.id for x in kit.scenarios}:
            raise HTTPException(404, "Unknown scenario")
        rt.service.reset()  # every scenario starts from the same records
        async with in_process_http() as http:
            result = await sc.run_scenario(scenario_id, stage_for(rt, http), kit.scenarios)
        gw.events.publish({"type": "scenario_done", "scenario": {k: v for k, v in result.items() if k != "steps"}})
        return result

    @router.post("/api/live/start")
    async def live_start():
        if not kit.live:
            raise HTTPException(404, "No live flow for this business.")
        """Stage moment: Riya's assistant asks to cancel; the audience watches Riya approve on her phone."""
        _demo_guard()
        rt.service.reset()
        params = {"appointment_id": "APT-501", "reason": "Riya is travelling"}
        session = sc.new_session("live")
        async with in_process_http() as http:
            riya = rt.world.callers(http, base_url)["riya"]
            res = await riya.send(intent="cancel_appointment", params=params, session=session, tag="live")
        cid = (res.payload.get("step_up") or {}).get("challenge_id")
        if cid:
            rt.live[cid] = {"session": session, "intent": "cancel_appointment", "params": params}
        return {"outcome": res.outcome, "challenge_id": cid, "customer_message": res.payload.get("customer_message")}

    @router.post("/api/live/{challenge_id}/resume")
    async def live_resume(challenge_id: str):
        _demo_guard()
        pending = rt.live.pop(challenge_id, None)
        if not pending:
            raise HTTPException(404, "No live request waiting on that approval.")
        async with in_process_http() as http:
            riya = rt.world.callers(http, base_url)["riya"]
            res = await riya.send(intent=pending["intent"], params=pending["params"], session=pending["session"],
                                  step_up=challenge_id, tag="live")
        return {"outcome": res.outcome, "reply": res.reply, "customer_message": res.payload.get("customer_message")}

    @router.post("/api/demo/tamper")
    async def tamper():
        _demo_guard()
        seq = gw.audit.tamper_for_demo()
        verdict = gw.audit.verify()
        gw.events.publish({"type": "audit", "verify": verdict, "tampered_seq": seq})
        return {"tampered_seq": seq, "verify": verdict}

    @router.post("/api/demo/reset")
    async def reset():
        _demo_guard()
        rt.service.reset()
        rt.live.clear()
        gw.audit.reset()
        gw.escalations.clear()
        gw.stepup.clear()
        gw.lanes.clear()
        if gw.verifier.nonces.store is not None:
            gw.verifier.nonces.store.clear_nonces()
        gw.guard.sessions.clear()
        gw.guard._rate.clear()
        gw.history.clear()
        gw.events.history.clear()
        gw.events.publish({"type": "reset"})
        return {"ok": True}

    # ---------------------------------------------------------------- dashboard

    @router.get("/", response_class=HTMLResponse)
    async def dashboard():
        return resources.files("dwarpal.web").joinpath("index.html").read_text(encoding="utf-8")

    app.include_router(router)
    if kit.on_mount:
        kit.on_mount(app, rt)


def _bearer_ok(request: Any, token: str) -> bool:
    auth = request.headers.get("authorization", "")
    got = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("access_token", "")
    return bool(got) and hmac.compare_digest(got, token)


class AdminAuthMiddleware:
    """Protects the dashboard API with a bearer token (or ?access_token= for the event stream). The A2A and
    voice endpoints have their own authentication; join codes and receipt checks stay public; step-up
    callbacks are checked by the endpoint itself."""
    OPEN = ("/api/join/", "/api/receipts/verify", "/api/stepup/")

    def __init__(self, app: Any, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and "/api/" in path and not any(o in path for o in self.OPEN):
            from starlette.requests import Request as _Req

            if not _bearer_ok(_Req(scope), self.token):
                body = b'{"detail":"Admin token required."}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def stage_for(rt: Runtime, http: httpx.AsyncClient) -> sc.Stage:
    """Everything a demo scenario needs to act out a business's story, all through real endpoints."""
    base_url = rt.settings.public_url.rstrip("/") + rt.kit.prefix
    return sc.Stage(callers=rt.world.callers(http, base_url), approve=rt.approve, join=rt.join, service=rt.service,
                    ivr=sc.IVR(http, rt.channel_key, rt.kit.ivr_verify(rt.service),
                               path=f"{rt.kit.prefix}/voice/turn"))
