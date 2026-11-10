"""The Dwarpal pipeline. Every inbound agent request goes through the same ordered checks:

    identity → escalation limits → protocol → security → delegation → handoff rules
             → business agent → data guard → audit + receipt

Each check records a pass / fail / warn / handoff / skip with a human-readable reason, which is
what the caller gets back, what the audit log stores and what the dashboard shows.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from .audit import AuditLog
from .business.backend import BusinessBackend, BusinessTask
from .data_guard import filter_outbound
from .escalation import EscalationQueue
from .events import EventBus
from .guard import SessionGuard, SessionState
from .identity import IdentityError, IdentityVerifier, VerifiedAgent, body_hash
from .policy import Policy
from .shield import Shield

log = logging.getLogger("dwarpal.gateway")

CHECKS = ["identity", "protocol", "security", "delegation", "escalation", "data_guard", "audit"]
LABELS = {
    "identity": "Identity",
    "protocol": "Protocol",
    "security": "Security",
    "delegation": "Delegation",
    "escalation": "Escalation",
    "data_guard": "Data guard",
    "audit": "Audit",
}


@dataclass
class Check:
    name: str
    status: str  # pass | fail | warn | handoff | skip
    detail: str

    @property
    def label(self) -> str:
        return LABELS.get(self.name, self.name)


@dataclass
class InboundRequest:
    session_id: str
    body: dict[str, Any] | None
    text: str = ""
    passport: str | None = None
    proof: str | None = None
    tag: str | None = None  # e.g. demo scenario name

    @property
    def signed_body(self) -> dict[str, Any]:
        """Exactly what the caller's request proof must cover."""
        return {"envelope": self.body, "text": self.text}


@dataclass
class Decision:
    request_id: str
    session_id: str
    outcome: str  # completed | rejected | denied | escalated | needs_structure | error
    checks: list[Check]
    reply: str
    agent: dict[str, Any] | None = None
    intent: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    receipt: str | None = None
    ticket_id: str | None = None
    audit_seq: int | None = None
    latency_ms: int = 0
    tag: str | None = None
    ts: float = field(default_factory=time.time)

    def check(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["checks"] = [{**asdict(c), "label": c.label} for c in self.checks]
        return d


class Gateway:
    def __init__(
        self,
        *,
        policy: Policy,
        verifier: IdentityVerifier,
        shield: Shield,
        guard: SessionGuard,
        escalations: EscalationQueue,
        audit: AuditLog,
        backend: BusinessBackend,
        events: EventBus | None = None,
    ):
        self.policy = policy
        self.verifier = verifier
        self.shield = shield
        self.guard = guard
        self.escalations = escalations
        self.audit = audit
        self.backend = backend
        self.events = events or EventBus()
        self.history: dict[str, list[dict[str, Any]]] = defaultdict(list)

    # ------------------------------------------------------------------ public

    async def handle(self, req: InboundRequest) -> Decision:
        t0 = time.perf_counter()
        try:
            decision = await self._pipeline(req)
        except Exception as exc:  # noqa: BLE001
            log.exception("gateway error")
            decision = Decision(request_id=self._rid(), session_id=req.session_id, outcome="error",
                                checks=[Check("identity", "skip", "Internal error before checks completed.")],
                                reply="The gateway hit an internal error. Nothing was done.")
            decision.data = {"error": type(exc).__name__}
        decision.tag = req.tag
        decision.latency_ms = int((time.perf_counter() - t0) * 1000)
        self._finalise(req, decision)
        return decision

    def catalog(self) -> list[dict[str, Any]]:
        return [i for i in self.policy.public_summary() if i["mode"] == "a2a"]

    # ------------------------------------------------------------------ pipeline

    async def _pipeline(self, req: InboundRequest) -> Decision:
        rid = self._rid()
        checks: list[Check] = []

        def done(outcome: str, reply: str, **kw: Any) -> Decision:
            names = {c.name for c in checks}
            for n in CHECKS[:-1]:
                if n not in names:
                    checks.append(Check(n, "skip", "Not reached."))
            checks.sort(key=lambda c: CHECKS.index(c.name))
            return Decision(request_id=rid, session_id=req.session_id, outcome=outcome, checks=checks, reply=reply,
                            **kw)

        # 1. Identity -------------------------------------------------------------------------
        try:
            agent = self.verifier.verify_passport(req.passport)
            self.verifier.verify_proof(agent, req.proof, req.signed_body)
        except IdentityError as e:
            checks.append(Check("identity", "fail", f"{e.message} [{e.code}]"))
            attempted = req.body.get("intent") if isinstance(req.body, dict) else None
            return done("denied", f"Identity check failed: {e.message} This attempt has been logged.",
                        intent=str(attempted) if attempted else None, data={"error": e.code})
        checks.append(Check(
            "identity", "pass",
            f"AI agent “{agent.agent_name}” acting for {agent.principal.name} ({agent.principal.id}), "
            f"passport from {agent.issuer}; request signed by the bound key, nonce fresh."))
        agent_info = {"id": agent.agent_id, "name": agent.agent_name, "issuer": agent.issuer,
                      "principal": asdict(agent.principal), "scopes": agent.scopes, "limits": agent.limits}
        s = self.guard.session(req.session_id, agent.agent_id, agent.principal.id)
        body = req.body if isinstance(req.body, dict) else None
        intent = body.get("intent") if body else None
        params = dict(body.get("params") or {}) if body else {}
        note = str(body.get("note") or "") if body else ""

        # 2. Escalation limits (session already with a person, rate, turns, cost, loops, probing) --
        if s.locked_reason:
            checks.append(Check("escalation", "handoff",
                                f"Session already handed to a person ({s.ticket_id}): {s.locked_reason}"))
            return done("escalated", f"This conversation is now with a person at {self.policy.business_name} "
                                     f"(ticket {s.ticket_id}). Further automated requests are paused.",
                        agent=agent_info, intent=intent, params=params, ticket_id=s.ticket_id)
        rate = self.guard.check_rate(agent.agent_id)
        if rate:
            checks.append(Check("escalation", "fail", rate.reason))
            return done("denied", "Too many requests. Slow down and retry later.", agent=agent_info,
                        intent=intent, params=params, data={"error": rate.code})
        breach = self.guard.begin_turn(s, intent or "free_text", params if body else {"text": req.text})
        if breach:
            ticket = self._open_ticket(s, agent, intent or "free_text", params, breach.code, breach.reason, "high")
            self.guard.lock(s, breach.reason, ticket.id)
            checks.append(Check("escalation", "handoff", f"{breach.reason} Handed to a person ({ticket.id})."))
            return done("escalated", f"I've handed this to a person at {self.policy.business_name} "
                                     f"(ticket {ticket.id}). They'll follow up with {agent.principal.name}.",
                        agent=agent_info, intent=intent, params=params, ticket_id=ticket.id)

        # 3. Protocol: structured intents only -----------------------------------------------
        if not intent:
            verdict = await self.shield.scan({"text": req.text})
            if verdict.blocked:
                self.guard.record_denial(s)
                s.flagged = True
                checks.append(Check("protocol", "warn", "Free text instead of a structured intent."))
                checks.append(Check("security", "fail", f"Injection blocked (score {verdict.score}): {verdict.summary}"))
                return done("denied", "Request blocked: it contains instructions aimed at our agent. "
                                      "This has been logged.", agent=agent_info, data={"error": "injection",
                                                                                    "families": verdict.families})
            checks.append(Check("protocol", "warn",
                                "Free-text request. Agents talk in structured intents here: sent back the catalog."))
            return done("needs_structure",
                        f"{self.policy.business_name} accepts structured A2A requests from agents. "
                        "Pick one of the intents in the catalog and resend.",
                        agent=agent_info, data={"intents": self.catalog(), "your_text_received": bool(req.text)})
        rule = self.policy.intents.get(intent)
        if rule is None:
            self.guard.record_denial(s)
            checks.append(Check("protocol", "fail", f"Unknown intent “{intent}”."))
            return done("denied", f"“{intent}” isn't something this business handles over A2A.",
                        agent=agent_info, intent=intent, params=params,
                        data={"error": "unknown_intent", "intents": self.catalog()})
        stripped = sorted(set(params) - set(rule.params))
        params = {k: v for k, v in params.items() if k in rule.params}
        if len(note) > self.policy.limits.max_note_chars:
            self.guard.record_denial(s)
            checks.append(Check("protocol", "fail",
                                f"Note is {len(note)} chars (max {self.policy.limits.max_note_chars})."))
            return done("denied", "The free-text note is too long. Put the request in structured parameters.",
                        agent=agent_info, intent=intent, params=params, data={"error": "note_too_long"})
        detail = f"Structured intent “{intent}” ({rule.risk} risk) with params {sorted(params) or '[]'}."
        if stripped:
            detail += f" Stripped non-whitelisted params {stripped}."
        checks.append(Check("protocol", "warn" if stripped else "pass", detail))

        # 4. Security: injection aimed at the business agent ---------------------------------
        verdict = await self.shield.scan({"params": params, "note": note, "text": req.text,
                                          "stripped": {k: (body or {}).get("params", {}).get(k) for k in stripped}})
        if verdict.blocked:
            self.guard.record_denial(s)
            s.flagged = True
            checks.append(Check("security", "fail",
                                f"Injection blocked (score {verdict.score}, layers {'+'.join(verdict.layers)}): "
                                f"{verdict.summary}"))
            return done("denied", "Request blocked: it contains instructions aimed at our agent. "
                                  "This has been logged and the agent flagged.",
                        agent=agent_info, intent=intent, params=params,
                        data={"error": "injection", "families": verdict.families, "score": verdict.score})
        checks.append(Check("security", "warn" if verdict.score > 0 else "pass",
                            f"Clean (score {verdict.score}, layers {'+'.join(verdict.layers)})."
                            if verdict.score == 0 else f"Low-confidence signal, allowed: {verdict.summary}"))

        # 5. Delegation: is this agent allowed to do this, for this much? ---------------------
        if not agent.has_scope(rule.scope):
            self.guard.record_denial(s)
            checks.append(Check("delegation", "fail",
                                f"Needs scope “{rule.scope}”; passport grants {agent.scopes}."))
            return done("denied", f"{agent.principal.name} hasn't authorized this agent to do that "
                                  f"(needs {rule.scope}).", agent=agent_info, intent=intent, params=params,
                        data={"error": "insufficient_scope", "required_scope": rule.scope})
        if rule.amount_param:
            try:
                amount = float(params.get(rule.amount_param, 0))
            except (TypeError, ValueError):
                amount = float("inf")
            caps = {"passport": agent.limits.get("max_amount_inr"), "business policy": rule.max_amount_inr}
            caps = {k: float(v) for k, v in caps.items() if v is not None}
            if caps and amount > min(caps.values()):
                which = min(caps, key=caps.get)
                self.guard.record_denial(s)
                checks.append(Check("delegation", "fail",
                                    f"₹{amount:,.0f} exceeds the {which} limit of ₹{caps[which]:,.0f}."))
                return done("denied", f"₹{amount:,.0f} is above what this agent may request "
                                      f"(limit ₹{caps[which]:,.0f}). A person can review larger amounts.",
                            agent=agent_info, intent=intent, params=params, data={"error": "over_limit"})
            checks.append(Check("delegation", "pass",
                                f"Scope “{rule.scope}” granted; ₹{amount:,.0f} within limit ₹{min(caps.values()):,.0f}."
                                if caps else f"Scope “{rule.scope}” granted."))
        else:
            checks.append(Check("delegation", "pass", f"Scope “{rule.scope}” granted by {agent.principal.name}."))

        # 6. Handoff rules: contested / sensitive intents go to a person ---------------------
        if rule.mode == "human":
            existing = s.open_tickets.get(intent)
            if existing and self.escalations.tickets.get(existing) and \
                    self.escalations.tickets[existing].status == "open":
                checks.append(Check("escalation", "handoff",
                                    f"Ticket {existing} is already open for “{intent}”. Repeat push not forwarded; "
                                    "pressing again doesn't change who decides."))
                return done("escalated", f"Ticket {existing} is already with a person at "
                                         f"{self.policy.business_name}. Repeating the request won't speed it up; "
                                         f"they'll contact {agent.principal.name}.",
                            agent=agent_info, intent=intent, params=params, ticket_id=existing)
            ticket = self._open_ticket(s, agent, intent, params, "human_only_intent",
                                       f"“{intent}” is {rule.risk}-risk and always decided by a person.", rule.risk)
            s.open_tickets[intent] = ticket.id
            checks.append(Check("escalation", "handoff",
                                f"“{intent}” is human-only by policy. Ticket {ticket.id} opened with full context."))
            return done("escalated", f"A person at {self.policy.business_name} handles {intent.replace('_', ' ')}. "
                                     f"Ticket {ticket.id} is open and they'll contact {agent.principal.name}.",
                        agent=agent_info, intent=intent, params=params, ticket_id=ticket.id)
        L = self.policy.limits
        checks.append(Check("escalation", "pass",
                            f"Turn {s.turns}/{L.max_turns_per_session}, cost {s.cost_units}/"
                            f"{L.max_cost_units_per_session}, no loop, {s.denials} refusals so far."))

        # 7. Business agent (ADK) ------------------------------------------------------------
        task = BusinessTask(session_id=req.session_id, agent_id=agent.agent_id, agent_name=agent.agent_name,
                            principal_id=agent.principal.id, principal_name=agent.principal.name,
                            intent=intent, params=params)
        result = await self.backend.run(task)
        s.cost_units += max(result.model_calls, 1)

        # 8. Data guard ----------------------------------------------------------------------
        clean_data, r1 = filter_outbound(result.data, self.policy.data)
        clean_msg, r2 = filter_outbound(result.message, self.policy.data)
        redactions = r1 + r2
        checks.append(Check("data_guard", "warn" if redactions else "pass",
                            "; ".join(redactions) if redactions else
                            f"Response clean. Tools used: {result.tool_calls or 'none'}."
                            + (" (Primary model unavailable: answered by the scripted fallback.)"
                               if result.fallback else "")))
        status = str(clean_data.get("status", "ok"))
        outcome = "rejected" if status in {"rejected", "not_found"} else "completed"

        receipt = None
        if outcome == "completed":
            receipt = self.audit.issue_receipt({
                "rid": rid, "business": self.policy.business_id, "agent": agent.agent_id,
                "principal": agent.principal.id, "intent": intent, "params": params,
                "request_hash": body_hash(req.signed_body), "result_hash": body_hash(clean_data),
                "result_status": status,
            })
        return done(outcome, clean_msg or "Done.", agent=agent_info, intent=intent, params=params,
                    data=clean_data, receipt=receipt)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _rid() -> str:
        return f"req_{uuid.uuid4().hex[:10]}"

    def _open_ticket(self, s: SessionState, agent: VerifiedAgent, intent: str, params: dict[str, Any],
                     code: str, reason: str, risk: str):
        return self.escalations.open(
            session_id=s.session_id, agent_id=agent.agent_id, agent_name=agent.agent_name,
            principal={"id": agent.principal.id, "name": agent.principal.name}, intent=intent, params=params,
            reason_code=code, reason=reason, risk=risk,
            history=list(self.history.get(self._hkey(s.session_id, agent.agent_id), []))[-10:],
        )

    @staticmethod
    def _hkey(session_id: str, agent_id: str | None) -> str:
        return f"{agent_id}::{session_id}"

    def _finalise(self, req: InboundRequest, d: Decision) -> None:
        record = self.audit.append(
            "agent_request",
            rid=d.request_id, session=d.session_id, outcome=d.outcome,
            agent=(d.agent or {}).get("id"), principal=((d.agent or {}).get("principal") or {}).get("id"),
            intent=d.intent, params=d.params, ticket=d.ticket_id,
            request_hash=body_hash(req.signed_body),
            checks={c.name: c.status for c in d.checks},
            reasons={c.name: c.detail for c in d.checks if c.status in {"fail", "handoff"}},
            receipt=bool(d.receipt),
        )
        d.audit_seq = record["seq"]
        d.checks.append(Check("audit", "pass",
                              f"Record #{record['seq']} chained to {record['prev'][:10]}… and signed"
                              + ("; signed receipt returned to the caller." if d.receipt else ".")))
        if d.agent:
            self.history[self._hkey(d.session_id, d.agent["id"])].append(
                {"rid": d.request_id, "intent": d.intent, "outcome": d.outcome, "reply": d.reply[:200]})
        self.events.publish({"type": "decision", "decision": d.to_dict()})
