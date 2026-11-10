"""Exposes the gateway as an A2A agent. Callers' agents speak standard A2A (JSON-RPC) to Dwarpal;
Dwarpal is the only thing that ever talks to the business agent behind it.

Wire format for a caller:
  * one DataPart: {"intent": "...", "params": {...}, "note": "optional short context"}
  * message metadata: {"dwarpal_passport": "<passport JWS>", "dwarpal_proof": "<request proof JWS>"}
    (or headers: Authorization: AgentPassport <jws>, X-Dwarpal-Proof: <jws>)
The reply is a TextPart (human-readable) plus a DataPart with the outcome, every check, any data,
the signed receipt and the human ticket id.
"""

from __future__ import annotations

from typing import Any

from a2a.helpers.proto_helpers import get_data_parts, get_text_parts, new_data_part, new_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    HTTPAuthSecurityScheme,
    Role,
    SecurityScheme,
)
from google.protobuf.json_format import MessageToDict

from .gateway import Gateway, InboundRequest
from .policy import Policy


def _headers(ctx: RequestContext) -> dict[str, str]:
    try:
        raw = (ctx.call_context.state or {}).get("headers") or {}
        return {str(k).lower(): str(v) for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        return {}


def parse_a2a_request(ctx: RequestContext) -> InboundRequest:
    msg = ctx.message
    meta: dict[str, Any] = MessageToDict(msg.metadata) if msg.HasField("metadata") else {}
    headers = _headers(ctx)
    data = [d for d in get_data_parts(msg.parts) if isinstance(d, dict)]
    envelope = data[0] if data else None
    text = "\n".join(t for t in get_text_parts(msg.parts) if t)

    passport = meta.get("dwarpal_passport")
    auth = headers.get("authorization", "")
    if not passport and auth.lower().startswith("agentpassport "):
        passport = auth.split(" ", 1)[1].strip()
    proof = meta.get("dwarpal_proof") or headers.get("x-dwarpal-proof")
    return InboundRequest(session_id=ctx.context_id or "no-context", body=envelope, text=text,
                          passport=passport, proof=proof, tag=meta.get("dwarpal_tag"))


class GatewayExecutor(AgentExecutor):
    def __init__(self, gateway: Gateway):
        self.gateway = gateway

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        decision = await self.gateway.handle(parse_a2a_request(context))
        payload = {
            "dwarpal": "1",
            "request_id": decision.request_id,
            "outcome": decision.outcome,
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in decision.checks],
            "data": decision.data,
            "receipt": decision.receipt,
            "ticket_id": decision.ticket_id,
            "audit_seq": decision.audit_seq,
        }
        reply = new_message([new_text_part(decision.reply), new_data_part(payload)],
                            context_id=context.context_id, role=Role.ROLE_AGENT)
        await event_queue.enqueue_event(reply)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:  # nothing long-running
        return None


def build_agent_card(policy: Policy, public_url: str, catalog: list[dict[str, Any]]) -> AgentCard:
    return AgentCard(
        name=f"{policy.business_name} (via Dwarpal)",
        description=(f"{policy.description} Inbound AI agents must present a Dwarpal agent passport from a "
                     "trusted issuer and sign each request. Structured intents only; contested matters go to "
                     "a person."),
        version="0.1.0",
        provider=AgentProvider(organization=policy.business_name, url=public_url),
        supported_interfaces=[AgentInterface(url=f"{public_url.rstrip('/')}/a2a", protocol_binding="JSONRPC",
                                             protocol_version="1.0")],
        capabilities=AgentCapabilities(streaming=False),
        security_schemes={
            "agentPassport": SecurityScheme(http_auth_security_scheme=HTTPAuthSecurityScheme(
                scheme="AgentPassport", bearer_format="agent-passport+jwt",
                description="EdDSA-signed passport from a trusted issuer, bound to the agent key. Send in "
                            "message metadata 'dwarpal_passport' plus a per-request proof in 'dwarpal_proof'.")),
        },
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json", "text/plain"],
        skills=[AgentSkill(id=i["intent"], name=i["intent"].replace("_", " ").title(),
                           description=f"{i['description']} Scope: {i['scope']}. Params: {', '.join(i['params'])}.",
                           tags=[i["scope"]], input_modes=["application/json"])
                for i in catalog],
    )
