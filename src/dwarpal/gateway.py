"""The Dwarpal pipeline. Gate the action, not the caller.

Every inbound request is checked against what its *action* requires (policy tier), not against a
guess about who is calling:

    identity → limits → protocol → security → delegation → human-only? → step-up
             → business agent → data guard → messages → audit + receipt

  open     anyone may ask, anonymous callers included; no patient data comes back
  account  needs a verified passport with the right scope
  binding  also needs the patient's own approval (step-up) for this exact action
  human    always goes to a person

Each check records pass / fail / warn / handoff / skip with a plain reason. The caller gets those
back, the audit log stores them, and the dashboard shows them.
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
from .identity import IdentityError, IdentityVerifier, body_hash
from .messages import action_summary, agent_outcome, customer_message, enrich, message_hash
from .policy import Policy
from .shield import Shield
from .stepup import Challenge, StepUpService
from .voice import LaneService, VoiceContext, describe_signals, evaluate_signals, restriction_applies

log = logging.getLogger("dwarpal.gateway")

CHECKS = ["identity", "protocol", "security", "delegation", "step_up", "escalation", "data_guard", "audit"]
LABELS = {
    "identity": "Identity",
    "protocol": "Protocol",
    "security": "Security",
    "delegation": "Delegation",
    "step_up": "Step-up",
    "escalation": "Escalation",
    "data_guard": "Data guard",
    "audit": "Audit",
}
STEP_UP_POLL_SECONDS = 15


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
    step_up: str | None = None  # challenge id, when resending after the customer approved
    lane_code: str | None = None  # links an A2A session to the phone call that offered the code
    channel: str = "a2a"  # a2a | voice
    voice: VoiceContext | None = None
    tag: str | None = None  # e.g. demo scenario name

    @property
    def signed_body(self) -> dict[str, Any]:
        """Exactly what the caller's request proof must cover (A2A). Voice turns come from the business's
        own voice platform, authenticated by its channel key."""
        d: dict[str, Any] = {"envelope": self.body, "text": self.text}
        if self.step_up:
            d["step_up"] = self.step_up
        if self.lane_code:
            d["lane_code"] = self.lane_code
        return d


@dataclass
class Caller:
    agent_id: str
    name: str
    principal_id: str
    principal_name: str
    verified: bool
    info: dict[str, Any]

    @classmethod
    def anonymous(cls) -> "Caller":
        return cls("anonymous", "Anonymous caller", "", "", False,
                   {"id": "anonymous", "name": "Anonymous caller", "issuer": None, "principal": None,
                    "scopes": [], "limits": {}, "verified": False})


@dataclass
class Decision:
    request_id: str
    session_id: str
    # completed | rejected | denied | escalated | step_up_required | needs_structure | error
    outcome: str
    checks: list[Check]
    reply: str  # for the calling agent
    customer_message: str = ""  # for the customer, written by the business
    agent_outcome: dict[str, Any] = field(default_factory=dict)
    tier: str | None = None
    agent: dict[str, Any] | None = None
    intent: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    receipt: str | None = None
    ticket_id: str | None = None
    join_code: str | None = None
    step_up: dict[str, Any] | None = None
    channel: str = "a2a"
    signals: dict[str, Any] | None = None  # voice: attestation, AI-voice score, declaration, restrictions
    agent_lane: dict[str, Any] | None = None  # voice: the lane code offered to a declared agent
    continued_from_call: dict[str, Any] | None = None  # A2A: the call this session was switched from
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


def _when(ts: float) -> str:
    return time.strftime("%a %d %b, %H:%M", time.localtime(ts)).replace(" 0", " ")


class Gateway:
    def __init__(
        self,
        *,
        policy: Policy,
        verifier: IdentityVerifier,
        shield: Shield,
        guard: SessionGuard,
        escalations: EscalationQueue,
        stepup: StepUpService,
        audit: AuditLog,
        backend: BusinessBackend,
        events: EventBus | None = None,
        lanes: LaneService | None = None,
    ):
        self.policy = policy
        self.verifier = verifier
        self.shield = shield
        self.guard = guard
        self.escalations = escalations
        self.stepup = stepup
        self.audit = audit
        self.backend = backend
        self.events = events or EventBus()
        self.lanes = lanes or LaneService(policy.voice)
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
                                reply="The gateway hit an internal error. Nothing was done.",
                                customer_message=customer_message(self.policy, "denied", intent=None, reason=None,
                                                                  fields={}),
                                agent_outcome=agent_outcome("error", ["internal_error"], retry_after_seconds=60))
            decision.data = {"error": type(exc).__name__}
        decision.tag = req.tag
        decision.latency_ms = int((time.perf_counter() - t0) * 1000)
        self._finalise(req, decision)
        return decision

    def catalog(self, include_human: bool = True) -> list[dict[str, Any]]:
        return [i for i in self.policy.public_summary() if include_human or i["tier"] != "human"]

    # ------------------------------------------------------------------ pipeline

    async def _pipeline(self, req: InboundRequest) -> Decision:
        rid = self._rid()
        checks: list[Check] = []
        body = req.body if isinstance(req.body, dict) else None
        intent = str(body["intent"]) if body and body.get("intent") else None
        params: dict[str, Any] = dict(body.get("params") or {}) if body else {}
        note = str(body.get("note") or "") if body else ""
        rule = self.policy.intents.get(intent) if intent else None
        caller = Caller.anonymous()
        v = req.voice if req.channel == "voice" else None
        restrictions = evaluate_signals(v, self.policy.voice) if v else []
        signals = ({k: val for k, val in v.public().items() if k != "verified_principal"}
                   | {"restrictions": [r.code for r in restrictions]}) if v else None
        if v:
            caller = Caller(f"voice:{v.call_id}", "Phone caller", "", "", False,
                            {"id": f"voice:{v.call_id}", "name": "Phone caller", "issuer": None, "principal": None,
                             "scopes": [], "limits": {}, "verified": False, "channel": "voice"})

        def done(outcome: str, reply: str, *, reason: str | None = None, codes: list[str] | None = None,
                 next_steps: list[str] | None = None, retry_after: float | None = None,
                 expected_by: float | None = None, fields: dict[str, Any] | None = None,
                 cm: str | None = None, **kw: Any) -> Decision:
            names = {c.name for c in checks}
            for n in CHECKS[:-1]:
                if n not in names:
                    checks.append(Check(n, "skip", "Not reached."))
            checks.sort(key=lambda c: CHECKS.index(c.name))
            if v and v.declared_agent and "agent_lane" not in kw and outcome not in {"denied", "error"}:
                # Honest agents on the phone are always offered the fast lane.
                kw["agent_lane"] = self._lane_offer(v, caller, intent, params, pending=False)
            if cm is None:
                f = {**enrich(params), "action_summary": action_summary(rule, params), **(fields or {})}
                if kw.get("agent_lane"):
                    f["lane_code_spoken"] = kw["agent_lane"]["spoken"]
                cm = customer_message(self.policy, outcome, intent=intent, reason=reason, fields=f,
                                      channel=req.channel)
                cm, _ = filter_outbound(cm, self.policy.data)
            ao = agent_outcome(outcome, codes or ([reason] if reason else []), next_steps=next_steps,
                               retry_after_seconds=None if retry_after is None else int(retry_after),
                               expected_by=expected_by)
            kw.setdefault("agent", caller.info)
            return Decision(request_id=rid, session_id=req.session_id, outcome=outcome, checks=checks, reply=reply,
                            customer_message=cm, agent_outcome=ao, tier=rule.tier if rule else None,
                            intent=intent, params=params, channel=req.channel, signals=signals, **kw)

        # 1. Identity: required unless the action is open-tier -----------------------------
        continued: dict[str, Any] | None = None
        if v:
            sig = describe_signals(v)
            dropped = (f" Dropped {', '.join(v.dropped_fields)}: audio and transcripts are never stored."
                       if v.dropped_fields else "")
            if v.verified_principal:
                vp = self.policy.voice
                caller = Caller(f"voice:{v.call_id}", "Phone caller", v.verified_principal,
                                v.verified_name or v.verified_principal, True,
                                {"id": f"voice:{v.call_id}", "name": "Phone caller", "issuer": "clinic IVR",
                                 "principal": {"id": v.verified_principal, "name": v.verified_name or "",
                                               "type": "person"},
                                 "scopes": list(vp.verified_scopes),
                                 "limits": {"max_amount_inr": vp.max_amount_inr} if vp.max_amount_inr else {},
                                 "verified": True, "channel": "voice"})
                checks.append(Check("identity", "warn" if restrictions else "pass",
                                    f"Phone caller verified by the clinic's IVR ({v.verification_method or 'verified'}) "
                                    f"as {caller.principal_name} ({caller.principal_id}). Signals: {sig}."
                                    + "".join(f" {r.detail}" for r in restrictions) + dropped))
            elif rule is None or rule.tier == "open":
                checks.append(Check("identity", "warn",
                                    f"Caller not verified yet: open-tier actions only. Signals: {sig}.{dropped}"))
            else:
                checks.append(Check("identity", "fail",
                                    f"Caller not verified. “{intent}” is {rule.tier}-tier, so the IVR must verify the "
                                    f"patient first. Signals: {sig}. A strong signal never replaces verification. "
                                    f"[needs_verification]{dropped}"))
                return done("denied", f"Verify the caller before “{intent}”.", reason="needs_verification",
                            codes=["needs_verification"],
                            next_steps=["Collect the patient ID and date of birth, verify them, then resend"],
                            data={"error": "needs_verification"})
        elif req.passport:
            try:
                agent = self.verifier.verify_passport(req.passport)
                self.verifier.verify_proof(agent, req.proof, req.signed_body)
            except IdentityError as e:
                checks.append(Check("identity", "fail", f"{e.message} [{e.code}]"))
                return done("denied", f"Identity check failed: {e.message} This attempt has been logged.",
                            reason="identity", codes=["identity_failed", e.code], agent=None,
                            data={"error": e.code})
            caller = Caller(agent.agent_id, agent.agent_name, agent.principal.id, agent.principal.name, True,
                            {"id": agent.agent_id, "name": agent.agent_name, "issuer": agent.issuer,
                             "principal": asdict(agent.principal), "scopes": agent.scopes, "limits": agent.limits,
                             "verified": True, "channel": "a2a"})
            detail = (f"AI agent “{agent.agent_name}” acting for {agent.principal.name} ({agent.principal.id}), "
                      f"passport from {agent.issuer}; request signed by the bound key, nonce fresh.")
            if req.lane_code:
                res, lane = self.lanes.redeem(req.lane_code, principal_id=agent.principal.id, agent_id=agent.agent_id)
                if res != "ok" or lane is None:
                    why = {"unknown": "isn't a code we issued", "used": "was already used",
                           "expired": "has expired",
                           "mismatch": "was issued on a call for a different patient"}.get(res, res)
                    checks.append(Check("identity", "fail", f"Lane code {req.lane_code} {why}. [lane_{res}]"))
                    return done("denied", f"That lane code {why}. Nothing was done.", reason=f"lane_{res}",
                                codes=["identity_failed", f"lane_{res}"], data={"error": f"lane_{res}"})
                withdrawn = self.stepup.supersede(f"voice:{lane.call_id}")
                continued = {"call_id": lane.call_id, "earlier": lane.earlier, "pending": lane.pending,
                             "withdrew_phone_approvals": withdrawn}
                detail += (f" Continued from phone call {lane.call_id} with a lane code: the code links the "
                           "context, the passport grants.")
            checks.append(Check("identity", "pass", detail))
        elif rule is None or rule.tier == "open":
            checks.append(Check("identity", "warn",
                                "No passport. Anonymous callers get open-tier actions only, with no patient data."))
        else:
            checks.append(Check("identity", "fail",
                                f"No agent passport. “{intent}” is {rule.tier}-tier and needs a verified "
                                "delegation. [missing_passport]"))
            return done("denied", f"“{intent}” needs a verified agent passport. Open-tier actions don't.",
                        reason="missing_passport", codes=["identity_failed", "missing_passport"],
                        next_steps=["Present a passport from an issuer this business trusts",
                                    "Or use an open-tier action from the catalog"],
                        data={"error": "missing_passport", "intents": self._open_catalog()})

        s = self.guard.session(req.session_id, caller.agent_id, caller.principal_id)

        # 2. Limits: session already with a person, rate, turns, cost, loops, probing --------
        if s.locked_reason:
            t = self.escalations.tickets.get(s.ticket_id or "")
            checks.append(Check("escalation", "handoff",
                                f"Session already handed to a person ({s.ticket_id}): {s.locked_reason}"))
            return done("escalated", f"This conversation is with a person at {self.policy.business_name} "
                                     f"(ticket {s.ticket_id}). Automated requests are paused.",
                        reason="repeat", codes=["session_with_human"], ticket_id=s.ticket_id,
                        **self._ticket_kw(t, repeat=True))
        anon_limit = None if caller.verified else self.policy.limits.max_anonymous_requests_per_minute
        rate = self.guard.check_rate(caller.agent_id, limit=anon_limit)
        if rate:
            checks.append(Check("escalation", "fail", rate.reason))
            return done("denied", "Too many requests. Slow down and retry later.", reason="rate_limited",
                        retry_after=60, data={"error": rate.code})
        # Waiting on the customer isn't looping. A resend whose approval is still pending (and covers
        # exactly this action) is answered here without counting toward turn or loop limits. It is
        # still rate-limited, and it can never reach the business agent.
        if req.step_up and rule is not None and rule.tier in {"binding", "account"} and caller.verified:
            wanted = {k: v for k, v in params.items() if k in rule.params}
            code, c = self.stepup.check(req.step_up, principal_id=caller.principal_id, agent_id=caller.agent_id,
                                        intent=intent or "", params=wanted)
            if code == "pending" and c is not None:
                params = wanted
                checks.append(Check("step_up", "handoff",
                                    f"Still waiting for {caller.principal_name} to approve {c.id} "
                                    f"(strength {c.strength}/{c.required_strength}). Polls don't count toward "
                                    "session limits."))
                return done("step_up_required", f"Still waiting for {caller.principal_name}'s approval.",
                            reason="step_up_required", retry_after=STEP_UP_POLL_SECONDS,
                            step_up={"challenge_id": c.id, "expires_at": int(c.expires_at),
                                     "required_strength": c.required_strength,
                                     "methods": self.stepup.methods_public()},
                            data={"step_up_challenge": c.id})
        breach = self.guard.begin_turn(s, intent or "free_text", params if body else {"text": req.text})
        if breach:
            ticket = self._open_ticket(s, caller, intent or "free_text", params, breach.code, breach.reason, "high",
                                       self.policy.handoff.runaway_response_hours)
            self.guard.lock(s, breach.reason, ticket.id)
            checks.append(Check("escalation", "handoff", f"{breach.reason} Handed to a person ({ticket.id})."))
            return done("escalated", f"I've handed this to a person at {self.policy.business_name} "
                                     f"(ticket {ticket.id}). Stop retrying; they'll follow up.",
                        reason=breach.code, ticket_id=ticket.id, **self._ticket_kw(ticket))

        # 3. Protocol: structured intents only -------------------------------------------
        if not intent:
            verdict = await self.shield.scan({"text": req.text})
            if verdict.blocked:
                self.guard.record_denial(s)
                s.flagged = True
                checks.append(Check("protocol", "warn", "Free text instead of a structured intent."))
                checks.append(Check("security", "fail", f"Injection blocked (score {verdict.score}): {verdict.summary}"))
                return done("denied", "Request blocked: it contains instructions aimed at our agent. "
                                      "This has been logged.", reason="security",
                            codes=["security", *verdict.families], data={"error": "injection",
                                                                        "families": verdict.families})
            checks.append(Check("protocol", "warn",
                                "Free-text request. Agents use structured intents here: sent back the catalog."))
            catalog = self.catalog() if caller.verified else self._open_catalog()
            return done("needs_structure",
                        f"{self.policy.business_name} accepts structured A2A requests from agents. "
                        "Pick an intent from the catalog and resend.", reason="needs_structure",
                        next_steps=["Resend as a DataPart: {\"intent\": ..., \"params\": {...}}"],
                        data={"intents": catalog, "your_text_received": bool(req.text)})
        if rule is None:
            self.guard.record_denial(s)
            checks.append(Check("protocol", "fail", f"Unknown intent “{intent}”."))
            return done("denied", f"“{intent}” isn't something this business handles over A2A.",
                        reason="unknown_intent", data={"error": "unknown_intent", "intents": self.catalog()})
        raw_params = dict(params)
        stripped = sorted(set(params) - set(rule.params))
        params = {k: v for k, v in params.items() if k in rule.params}
        if len(note) > self.policy.limits.max_note_chars:
            self.guard.record_denial(s)
            checks.append(Check("protocol", "fail",
                                f"Note is {len(note)} chars (max {self.policy.limits.max_note_chars})."))
            return done("denied", "The free-text note is too long. Put the request in structured parameters.",
                        reason="note_too_long", data={"error": "note_too_long"})
        detail = f"Structured intent “{intent}”, {rule.tier} tier, params {sorted(params) or '[]'}."
        if stripped:
            detail += f" Stripped non-whitelisted params {stripped}."
        checks.append(Check("protocol", "warn" if stripped else "pass", detail))

        # 4. Security: injection aimed at the business agent -----------------------------
        verdict = await self.shield.scan({"params": params, "note": note, "text": req.text,
                                          "stripped": {k: raw_params.get(k) for k in stripped}})
        if verdict.blocked:
            self.guard.record_denial(s)
            s.flagged = True
            checks.append(Check("security", "fail",
                                f"Injection blocked (score {verdict.score}, layers {'+'.join(verdict.layers)}): "
                                f"{verdict.summary}"))
            return done("denied", "Request blocked: it contains instructions aimed at our agent. "
                                  "This has been logged and the agent flagged.",
                        reason="security", codes=["security", *verdict.families],
                        data={"error": "injection", "families": verdict.families, "score": verdict.score})
        checks.append(Check("security", "warn" if verdict.score > 0 else "pass",
                            f"Clean (score {verdict.score}, layers {'+'.join(verdict.layers)})."
                            if verdict.score == 0 else f"Low-confidence signal, allowed: {verdict.summary}"))

        # 5. Delegation: is this agent allowed to do this, for this much? -----------------
        if rule.tier == "open":
            checks.append(Check("delegation", "pass", "Open tier: anyone may ask, and no patient data comes back."))
        else:
            if not self._has_scope(caller, rule.scope or ""):
                self.guard.record_denial(s)
                checks.append(Check("delegation", "fail",
                                    f"Needs scope “{rule.scope}”; passport grants {caller.info['scopes']}."))
                return done("denied", f"{caller.principal_name} hasn't authorized this agent to do that "
                                      f"(needs {rule.scope}).", reason="insufficient_scope",
                            next_steps=[f"Ask {caller.principal_name} to grant {rule.scope} in their identity app"],
                            data={"error": "insufficient_scope", "required_scope": rule.scope})
            detail = (f"Scope “{rule.scope}” granted by {caller.principal_name}." if not v else
                      f"Scope “{rule.scope}” allowed for callers the IVR has verified.")
            if rule.amount_param:
                try:
                    amount = float(params.get(rule.amount_param, 0))
                except (TypeError, ValueError):
                    amount = float("inf")
                caps = {"passport": caller.info["limits"].get("max_amount_inr"),
                        "business policy": rule.max_amount_inr}
                caps = {k: float(v) for k, v in caps.items() if v is not None}
                if caps and amount > min(caps.values()):
                    which = min(caps, key=caps.get)
                    self.guard.record_denial(s)
                    checks.append(Check("delegation", "fail",
                                        f"₹{amount:,.0f} exceeds the {which} limit of ₹{caps[which]:,.0f}. "
                                        "Refused before asking the patient to approve anything."))
                    return done("denied", f"₹{amount:,.0f} is above what this agent may request "
                                          f"(limit ₹{caps[which]:,.0f}). A person can review larger amounts.",
                                reason="over_limit", data={"error": "over_limit"})
                if caps:
                    detail = f"Scope “{rule.scope}” granted; ₹{amount:,.0f} within limit ₹{min(caps.values()):,.0f}."
            checks.append(Check("delegation", "pass", detail))

        # 6. Human-only actions go to a person, before anyone is asked to approve anything -
        if rule.tier == "human":
            checks.append(Check("step_up", "skip", "Not needed: a person decides this."))
            existing = self.escalations.tickets.get(s.open_tickets.get(intent, ""))
            if existing and existing.status == "open":
                checks.append(Check("escalation", "handoff",
                                    f"Ticket {existing.id} is already open for “{intent}”. Repeat push not "
                                    "forwarded; pressing again doesn't change who decides."))
                return done("escalated", f"Ticket {existing.id} is already with a person at "
                                         f"{self.policy.business_name}. Don't retry; they'll contact "
                                         f"{caller.principal_name}.", reason="repeat",
                            codes=["human_only", "already_open"], ticket_id=existing.id,
                            **self._ticket_kw(existing, repeat=True))
            hours = rule.response_hours or self.policy.handoff.default_response_hours
            ticket = self._open_ticket(s, caller, intent, params, "human_only_intent",
                                       f"“{intent}” is always decided by a person.", "high", hours)
            s.open_tickets[intent] = ticket.id
            checks.append(Check("escalation", "handoff",
                                f"“{intent}” is human-only by policy. Ticket {ticket.id} opened with full context; "
                                f"answer due by {_when(ticket.expected_by)}."))
            return done("escalated", f"A person at {self.policy.business_name} handles "
                                     f"{intent.replace('_', ' ')}. Ticket {ticket.id} is open.",
                        reason="human_only", ticket_id=ticket.id, **self._ticket_kw(ticket))

        # 7. Step-up: binding actions need the customer's own approval for this action ----
        step_evidence: Challenge | None = None
        active = [r for r in restrictions if restriction_applies(r, rule.tier, rule.scope)]
        if rule.tier != "binding" and not active:
            checks.append(Check("step_up", "skip", f"Not needed: {rule.tier} tier."
                                + (" No signal on this call adds friction." if v else "")))
        else:
            summary = action_summary(rule, params)
            code, c = ("missing", None)
            if req.step_up:
                code, c = self.stepup.check(req.step_up, principal_id=caller.principal_id, agent_id=caller.agent_id,
                                            intent=intent, params=params)
            if code == "ok" and c:
                self.stepup.consume(c, rid)
                step_evidence = c
                checks.append(Check("step_up", "pass",
                                    f"{caller.principal_name} approved this exact action ({c.id}) with "
                                    f"{' + '.join(c.factors)}, strength {c.strength}/{c.required_strength}."
                                    + (f" Required because: {', '.join(r.code for r in active)}." if active
                                       and rule.tier != "binding" else "")))
            elif code == "mismatch" and c:
                self.guard.record_denial(s)
                checks.append(Check("step_up", "fail",
                                    f"Approval {c.id} covers “{c.summary}”, not this request. Approvals are bound "
                                    "to one exact action."))
                return done("denied", f"Approval {c.id} is for a different action. Nothing was done.",
                            reason="step_up_mismatch", data={"error": "step_up_mismatch"})
            elif code == "declined" and c:
                checks.append(Check("step_up", "fail", f"{caller.principal_name} declined {c.id}."))
                return done("denied", f"{caller.principal_name} declined this request. Nothing was done.",
                            reason="step_up_declined", data={"error": "step_up_declined"})
            else:
                fresh = code != "pending" or c is None
                if fresh:
                    c = self.stepup.create(agent_id=caller.agent_id, agent_name=caller.name,
                                           principal_id=caller.principal_id, principal_name=caller.principal_name,
                                           intent=intent, params=params, summary=summary)
                why = {"missing": "", "pending": " Still waiting for the patient.",
                       "expired": " The earlier approval request expired.", "used": " That approval was already used.",
                       "unknown": " That approval id isn't known."}.get(code, "")
                because = ("Binding action" if rule.tier == "binding" else
                           "Account action with a restricting signal (" + ", ".join(r.code for r in active) + ")")
                checks.append(Check("step_up", "handoff",
                                    f"{because}: asked {caller.principal_name} to approve “{summary}” on their "
                                    f"own device ({c.id}, needs strength {c.required_strength}).{why}"))
                declared = any(r.code == "declared_agent" for r in active)
                lane_kw = ({"agent_lane": self._lane_offer(v, caller, intent, params, pending=True)}
                           if v and declared else {})
                return done("step_up_required",
                            (f"Collect step-up from the caller (keypad PIN, SMS code or app), then resend this turn "
                             f"with step_up={c.id}." if v else
                             f"{caller.principal_name} must approve this on their own device. Resend the same "
                             f"request with dwarpal_step_up={c.id} once approved."),
                            reason="declared_agent" if lane_kw else "step_up_required",
                            retry_after=STEP_UP_POLL_SECONDS, **lane_kw,
                            next_steps=([f"Or switch to the agent lane: connect over A2A with your passport and "
                                         f"lane code {lane_kw['agent_lane']['code']}"] if lane_kw else [])
                            + [f"Relay the customer message to {caller.principal_name}",
                                        f"After approval, resend the identical request with "
                                        f"metadata dwarpal_step_up={c.id}",
                                        f"The approval expires at {_when(c.expires_at)}"],
                            step_up={"challenge_id": c.id, "expires_at": int(c.expires_at),
                                     "required_strength": c.required_strength,
                                     "methods": self.stepup.methods_public()},
                            data={"step_up_challenge": c.id})

        L = self.policy.limits
        checks.append(Check("escalation", "pass",
                            f"Turn {s.turns}/{L.max_turns_per_session}, cost {s.cost_units}/"
                            f"{L.max_cost_units_per_session}, no loop, {s.denials} refusals so far."))

        # 8. Business agent (ADK), which writes only through the shared business service --
        task = BusinessTask(session_id=req.session_id, agent_id=caller.agent_id, agent_name=caller.name,
                            principal_id=caller.principal_id, principal_name=caller.principal_name or "anonymous",
                            intent=intent, params=params, tier=rule.tier)
        result = await self.backend.run(task)
        s.cost_units += max(result.model_calls, 1)

        # 9. Data guard ------------------------------------------------------------------
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

        # 10. Customer message, then a receipt that covers it ----------------------------
        fields = {**enrich(params), "action_summary": action_summary(rule, params),
                  **{k: v for k, v in clean_data.items() if isinstance(v, (str, int, float))},
                  "business_reason": clean_data.get("message", "")}
        cm = customer_message(self.policy, outcome, intent=intent, reason=None, fields=fields, channel=req.channel)
        cm, _ = filter_outbound(cm, self.policy.data)
        receipt = None
        if outcome == "completed" and caller.verified:
            receipt = self.audit.issue_receipt({
                "rid": rid, "business": self.policy.business_id, "agent": caller.agent_id,
                "principal": caller.principal_id, "intent": intent, "tier": rule.tier, "params": params,
                "request_hash": body_hash(req.signed_body), "result_hash": body_hash(clean_data),
                "result_status": status, "customer_message_sha256": message_hash(cm), "channel": req.channel,
                "step_up": ({"challenge": step_evidence.id, "factors": step_evidence.factors,
                             "strength": step_evidence.strength} if step_evidence else None),
            })
        return done(outcome, clean_msg or "Done.", cm=cm,
                    codes=["ok"] if outcome == "completed" else ["business_rule", status],
                    data=clean_data, receipt=receipt, continued_from_call=continued)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _rid() -> str:
        return f"req_{uuid.uuid4().hex[:10]}"

    @staticmethod
    def _has_scope(caller: Caller, scope: str) -> bool:
        scopes = caller.info.get("scopes") or []
        return scope in scopes or f"{scope.split(':', 1)[0]}:*" in scopes

    def _lane_offer(self, v: VoiceContext, caller: Caller, intent: str | None, params: dict[str, Any],
                    pending: bool) -> dict[str, Any]:
        earlier = list(self.history.get(self._hkey(v.call_id, caller.agent_id), []))
        lane = self.lanes.offer(call_id=v.call_id, principal_id=caller.principal_id,
                                principal_name=caller.principal_name,
                                pending={"intent": intent, "params": params} if pending and intent else None,
                                earlier=earlier)
        return {"code": lane.code, "spoken": lane.spoken, "expires_at": int(lane.expires_at),
                "agent_card": "/.well-known/agent-card.json", "pending": lane.pending,
                "how": "Connect over A2A with your own passport and metadata dwarpal_lane_code. The code links "
                       "this call's context; your passport is what grants access, and its patient must match."}

    def _open_catalog(self) -> list[dict[str, Any]]:
        return [i for i in self.catalog() if i["tier"] == "open"]

    def _ticket_kw(self, t: Any, repeat: bool = False) -> dict[str, Any]:
        if t is None:
            return {}
        wait = max(t.expected_by - time.time(), 0)
        return {
            "join_code": None if repeat else t.join_code,
            "retry_after": wait,
            "expected_by": t.expected_by,
            "next_steps": ([f"Tell the customer their reference is {t.id}"]
                           + ([] if repeat else [f"Give them join code {t.join_code} to join the case directly"])
                           + [f"Don't retry before {_when(t.expected_by)}"]),
            "fields": {"ticket_id": t.id, "join_code": t.join_code, "expected_by_human": _when(t.expected_by)},
        }

    def _open_ticket(self, s: SessionState, caller: Caller, intent: str, params: dict[str, Any],
                     code: str, reason: str, risk: str, response_hours: float):
        return self.escalations.open(
            response_hours=response_hours, join_ttl_hours=self.policy.handoff.join_code_ttl_hours,
            session_id=s.session_id, agent_id=caller.agent_id, agent_name=caller.name,
            principal={"id": caller.principal_id, "name": caller.principal_name}, intent=intent, params=params,
            reason_code=code, reason=reason, risk=risk,
            history=list(self.history.get(self._hkey(s.session_id, caller.agent_id), []))[-10:],
        )

    @staticmethod
    def _hkey(session_id: str, agent_id: str | None) -> str:
        return f"{agent_id}::{session_id}"

    def _finalise(self, req: InboundRequest, d: Decision) -> None:
        record = self.audit.append(
            "agent_request",
            rid=d.request_id, session=d.session_id, outcome=d.outcome, tier=d.tier, channel=d.channel,
            signals=d.signals, lane_offered=(d.agent_lane or {}).get("code"),
            continued_from_call=(d.continued_from_call or {}).get("call_id"),
            agent=(d.agent or {}).get("id"), principal=((d.agent or {}).get("principal") or {}).get("id"),
            intent=d.intent, params=d.params, ticket=d.ticket_id,
            step_up=(d.step_up or {}).get("challenge_id") or (d.data or {}).get("step_up_challenge"),
            request_hash=body_hash(req.signed_body),
            customer_message_sha256=message_hash(d.customer_message) if d.customer_message else None,
            reason_codes=d.agent_outcome.get("reason_codes"),
            checks={c.name: c.status for c in d.checks},
            reasons={c.name: c.detail for c in d.checks if c.status in {"fail", "handoff"}},
            receipt=bool(d.receipt),
        )
        d.audit_seq = record["seq"]
        d.checks.append(Check("audit", "pass",
                              f"Record #{record['seq']} chained to {record['prev'][:10]}… and signed"
                              + ("; signed receipt (covering the customer message) returned." if d.receipt
                                 else ".")))
        if d.agent:
            self.history[self._hkey(d.session_id, d.agent["id"])].append(
                {"rid": d.request_id, "intent": d.intent, "outcome": d.outcome, "reply": d.reply[:200]})
        self.events.publish({"type": "decision", "decision": d.to_dict()})
