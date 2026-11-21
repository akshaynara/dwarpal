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
    AgentExtension,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    HTTPAuthSecurityScheme,
    Role,
    SecurityScheme,
)
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct

# The Dwarpal agent-passport extension. The URI points at its specification.
EXTENSION_URI = "https://github.com/akshaynara/dwarpal/blob/main/docs/extension-agent-passport.md"

from .gateway import Gateway, InboundRequest
from .httpsig import INTERNAL_HEADER, passport_from_authorization, read_internal
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
    http_sig = read_internal(headers.get(INTERNAL_HEADER))
    if http_sig is not None and passport != passport_from_authorization(headers.get("authorization")):
        # Signed under the header passport; the metadata passport must be the same token.
        http_sig = {**http_sig, "passport_sha": "mismatch"}
    try:
        extensions = list(ctx.requested_extensions or []) + list(msg.extensions)
    except Exception:  # noqa: BLE001
        extensions = list(msg.extensions)
    return InboundRequest(session_id=ctx.context_id or "no-context", body=envelope, text=text,
                          passport=passport, proof=proof, step_up=meta.get("dwarpal_step_up"),
                          lane_code=meta.get("dwarpal_lane_code"), http_sig=http_sig,
                          extensions=sorted(set(extensions)), tag=meta.get("dwarpal_tag"))


class GatewayExecutor(AgentExecutor):
    def __init__(self, gateway: Gateway):
        self.gateway = gateway

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        decision = await self.gateway.handle(parse_a2a_request(context))
        payload = {
            "dwarpal": "1",
            "request_id": decision.request_id,
            "outcome": decision.outcome,
            "tier": decision.tier,
            "agent_outcome": decision.agent_outcome,
            "customer_message": decision.customer_message,
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in decision.checks],
            "data": decision.data,
            "receipt": decision.receipt,
            "ticket_id": decision.ticket_id,
            "join_code": decision.join_code,
            "continued_from_call": decision.continued_from_call,
            "step_up": decision.step_up,
            "audit_seq": decision.audit_seq,
        }
        reply = new_message([new_text_part(decision.reply), new_data_part(payload)],
                            context_id=context.context_id, role=Role.ROLE_AGENT)
        await event_queue.enqueue_event(reply)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:  # nothing long-running
        return None


def passport_extension(policy: Policy, catalog: list[dict[str, Any]]) -> AgentExtension:
    params = Struct()
    params.update({
        "version": "1",
        "audience": policy.business_id,
        "trusted_issuers": [i.get("id") for i in policy.trusted_issuers],
        "passport": {"header": "Authorization: AgentPassport <jws>", "metadata_key": "dwarpal_passport",
                     "typ": "agent-passport+jwt", "alg": "EdDSA"},
        "request_signing": {"profile": "RFC 9421", "alg": "ed25519", "tag": "dwarpal-agent",
                            "covered": ["@method", "@authority", "@path", "content-digest", "content-type",
                                        "authorization"],
                            "fallback": "dwarpal_proof metadata (agent-proof+jwt)"},
        "metadata_keys": ["dwarpal_passport", "dwarpal_step_up", "dwarpal_lane_code", "dwarpal_proof"],
        "tiers": {i["intent"]: i["tier"] for i in catalog},
        "step_up": {"required_strength": policy.step_up.required_strength, "resend_with": "dwarpal_step_up"},
    })
    return AgentExtension(
        uri=EXTENSION_URI,
        description="Dwarpal agent passport: who the agent acts for (signed passport), proof of possession "
                    "(RFC 9421 request signatures), action tiers, customer step-up and lane codes. Needed for "
                    "account and binding actions; open actions work without it.",
        required=False, params=params)


def build_agent_card(policy: Policy, public_url: str, catalog: list[dict[str, Any]]) -> AgentCard:
    return AgentCard(
        name=f"{policy.business_name} (via Dwarpal)",
        description=(f"{policy.description} Each action declares the proof it needs: open actions need none, "
                     "account actions need a Dwarpal agent passport from a trusted issuer with every request "
                     "signed, binding actions also need the customer's own approval, and contested matters go "
                     "to a person. Structured intents only."),
        version="1.0.0",
        provider=AgentProvider(organization=policy.business_name, url=public_url),
        supported_interfaces=[AgentInterface(url=f"{public_url.rstrip('/')}/a2a", protocol_binding="JSONRPC",
                                             protocol_version="1.0")],
        capabilities=AgentCapabilities(streaming=False, extensions=[passport_extension(policy, catalog)]),
        security_schemes={
            "agentPassport": SecurityScheme(http_auth_security_scheme=HTTPAuthSecurityScheme(
                scheme="AgentPassport", bearer_format="agent-passport+jwt",
                description="EdDSA-signed passport from a trusted issuer, bound to the agent key. Send in "
                            "message metadata 'dwarpal_passport' plus a per-request proof in 'dwarpal_proof'.")),
        },
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json", "text/plain"],
        skills=[AgentSkill(id=i["intent"], name=i["intent"].replace("_", " ").title(),
                           description=(f"{i['description']} Tier: {i['tier']}."
                                        + (f" Scope: {i['scope']}." if i["scope"] else "")
                                        + f" Params: {', '.join(i['params'])}."),
                           tags=[f"tier:{i['tier']}"] + ([i["scope"]] if i["scope"] else []),
                           input_modes=["application/json"])
                for i in catalog if i["tier"] != "human"],
    )
