# Dwarpal architecture

## Principle: gate the action, not the caller

Telling humans from agents, or good agents from bad ones, is probabilistic. Good and bad agents make the same requests (a claim status check can be routine or the first step of fraud), and some AI voices are people speaking for themselves. So Dwarpal never grants anything based on a guess about the caller. Each action declares the proof it needs, and that proof is checked the same way whoever asks:

| Tier | Proof | Clinic examples |
|---|---|---|
| open | none | `clinic_info`, `list_slots` |
| account | verified passport with the right scope | `appointment_status`, `reschedule_appointment` |
| binding | passport + the customer's own approval of this exact action | `cancel_appointment`, `refund_copay` |
| human | a person decides | `billing_dispute`, `medical_records` |

Signals about the caller (for example an AI-voice detection score or carrier attestation, on the voice roadmap) may add friction. They never unlock an action. A wrong guess therefore costs a little friction and nothing more, because the action still needs its proof.

## Threat model

Callers may be:

- **Legitimate but over-reaching**: a real assistant asking for more than its user delegated
- **Compromised**: a verified agent carrying hostile instructions from upstream
- **Impersonators**: agents claiming to act for someone they don't
- **Replayers**: anyone who captured a valid request in transit
- **Runaway**: agents stuck in a loop, or pushing a contested matter until something gives
- **Pretenders**: agents that won't say they're agents

Dwarpal does not decide contested matters between agents. Disputes have competing interests and no agreed process, so two agents arguing only burns compute. Those go to a person at once.

## Request lifecycle

```
caller ──A2A──► identity ─► limits ─► protocol ─► security ─► delegation ─► tier?
                                                                             │
          human ◄────────────────────────────────────────────────────────────┤
          (ticket, due time, join code)                                      │
                                                                             ▼
          binding ──► step-up: approved for this exact action? ──no──► step_up_required
                                    │ yes
                                    ▼
          open/account ──► business agent (ADK) ──► business service (shared with staff)
                                    │
                                    ▼
                    data guard ─► agent outcome + customer message ─► receipt ─► audit
```

Each stage appends a `Check(name, status, detail)`. The first `fail` or `handoff` ends the pipeline. Every request, refused or not, goes into the audit chain, so the record of what was *attempted* is as complete as the record of what was done.

### Identity: passport + proof

**Agent passport** (compact JWS, `typ: agent-passport+jwt`, EdDSA), issued by an identity provider the business trusts:

```json
{
  "iss": "idp.healthwallet.example",
  "sub": "agent:riya-assistant",
  "aud": "clinic.sunrise.example",
  "principal": {"id": "P-1001", "name": "Riya Sharma", "type": "person"},
  "scopes": ["appointments:read", "appointments:write", "billing:write"],
  "limits": {"max_amount_inr": 1000},
  "ai_disclosed": true,
  "cnf": {"kid": "…", "pub": "<agent's Ed25519 public key>"},
  "iat": 1762790000, "exp": 1762793600, "jti": "…"
}
```

Disclosure is part of the credential (`ai_disclosed`), not good manners: a passport that doesn't declare an AI isn't accepted.

**Request proof** (`typ: agent-proof+jwt`) signed by the key in `cnf` for every request: `aud`, `iat`, a random `nonce`, and `bh`, the SHA-256 of the canonical request body (including any step-up id). The gateway checks the key matches the passport, the proof is under 60 seconds old, the nonce is unseen and the body hash matches. Same idea as OAuth DPoP.

Open-tier requests may come without a passport. If a passport *is* presented it must verify; a bad credential is never silently downgraded to anonymous.

### Limits

Per agent and A2A context: requests per minute (anonymous callers share one bucket), turns per session, a compute budget (one unit per model call), repeated identical requests (loops) and repeated refusals (probing). A breach locks the session and opens a human ticket.

### Protocol and security

Structured intents from the policy catalog only. Free text gets the catalog back (open intents only, for anonymous callers). Non-whitelisted parameters are stripped. The shield scans every caller-controlled string, including stripped parameters, through normalised rules, then Gemini, then Model Armor. The bigger control is architectural: the business agent only receives the gateway's *verified task*, never the caller's words.

### Delegation

The passport must grant the intent's scope. For money, the effective cap is the lower of the passport's and the policy's limit. This runs **before** step-up, so customers are never asked to approve something that would be refused anyway.

### Step-up (binding tier)

Modelled on OpenID CIBA: the agent asks, the customer approves on their own device.

1. The gateway creates a challenge whose `action_hash` covers the principal, agent, intent and parameters, and returns `step_up_required` with the challenge id, a customer message to relay and `retry_after_seconds`.
2. The customer completes one or more methods. Each has a strength in policy (`app_biometric` 2, `sms_code` 1, `keypad_pin` 1, `voice_liveness` 1). The challenge is approved once the total reaches `required_strength` (2).
3. The agent resends the identical request with the challenge id. The gateway checks it is approved, unexpired, unused and that the action hash matches, then consumes it.

Rules that hold whatever the policy file says:

- **A voiceprint is capped at strength 1 in code.** A clean 8–12 second sample is enough to clone a voice, so a voice can never approve a binding action alone.
- **An approval covers one exact action.** Using it for a different action, amount, appointment or agent is refused (`step_up_mismatch`).
- **Approvals are single-use and expire** (300 seconds by default).
- **There is always a path that needs no speech** (app, SMS, keypad), so people who can't or don't speak aren't shut out.

### Human handoff

Human-tier intents, and sessions that hit a limit, open a ticket with the recent history, a due time (`response_hours` per intent, shorter for runaway sessions) and a one-time **join code**. The calling agent gets the case id, `expected_by` and a `retry_after_seconds` that runs until then, so it stops calling. A second push for the same intent returns the same ticket. The customer can join the case with the code without re-verifying, so they never have to start the story over.

### No privileged path

`ClinicService` holds every business rule: ownership checks ("not found" and "not yours" give the same answer), slot availability, refund ceilings, and "clinical records only to staff". The ADK agent's tools are thin wrappers over it with `Actor(channel="agent")`; the staff desk endpoint calls it with `Actor(channel="staff")`. Every write is recorded with its channel and appended to the audit chain. The gateway module has no reference to the service (a test enforces this).

### Two messages

- **Agent outcome**: `decision`, `reason_codes`, `next_steps`, `retry_after_seconds`, `expected_by`, `relay_customer_message`.
- **Customer message**: rendered from the policy's `customer_messages` templates, looked up as `<outcome>.<intent>`, then `<outcome>.<reason>`, then `<outcome>`, and passed through the data guard. Security refusals use a neutral template.

### Audit and receipts

Each record holds `seq, ts, event, data, prev` and gets `hash = sha256(canonical(record))` plus `sig = Ed25519(hash)` from the gateway key. Events include every decision, every step-up factor, declines, business writes, customer joins and human resolutions. Verification reports the first record whose link, content or signature fails.

For completed actions by verified agents, the receipt (`typ: dwarpal-receipt+jwt`) contains the request hash, result hash, tier, intent, principal, step-up evidence, the audit chain head, and `customer_message_sha256`. `POST /api/receipts/verify` checks a receipt and, optionally, whether a relayed message matches what the business said.

## The phone lane

The business's voice platform calls `POST /voice/turn` on each turn, authenticated by a channel key, with the recognised intent, the IVR's verification of the patient (if done), and signals: carrier attestation, an AI-voice score and whether the caller declared itself an agent. The contract is in [voice-integration.md](voice-integration.md).

- **Proof on the phone.** The IVR's verification is the phone equivalent of a passport for account-tier actions, exactly as at the front desk today. Binding actions need step-up, as everywhere. A strong signal never replaces verification.
- **Signals restrict, never grant.** Undisclosed AI voice adds step-up to account-tier actions. A declared agent is offered the agent lane and meets the same friction on the call. Weak attestation adds step-up to account-tier changes. Open-tier actions are never affected, and no signal blocks.
- **Disclosure never costs more than being caught.** Tested as an invariant.
- **Accessibility.** Step-up always has paths without speech, and the spoken messages never mention how the caller sounds, so a person using a speech-generating device hears exactly what anyone else hears.
- **Scores only.** Audio, recordings and transcripts are dropped on arrival; the audit records the scores and the names of dropped fields.
- **Lane switch.** A declared agent gets a six-character lane code, spoken and in the response. Over A2A, the agent presents its passport plus the code. The gateway checks the passport's patient matches the call's verified patient, links the sessions, withdraws pending phone prompts, and returns the call's earlier requests. The code links context and grants nothing.

**Residual risk, stated plainly:** an agent that pretends to be human, isn't detected, and knows the patient's details gets what a human caller gets today: account-tier actions after IVR verification. Binding actions still need the patient's own step-up, which the agent can't complete. Detection quality, and moving sensitive reads to the binding tier in policy, are the levers for narrowing this.

## Many businesses, one gateway

Everything business-specific is plugged in as a kit (`businesses.py`): the policy file, the business service, the ADK agent in front of it, and the demo identities and scenarios. Each kit is mounted under its own prefix with its own A2A agent card (`{prefix}/.well-known/agent-card.json`), JSON-RPC endpoint, voice endpoint, signing key, audit chain and dashboard. A passport is bound to one business by its audience, so a credential for the clinic is useless at the payor.

The business service can also decide that a person must answer (`status: needs_human`), for example a payor plan with no electronic rail. The gateway then opens a ticket with the service's due time and handles repeats exactly as for human-tier actions. See [The payor desk](payor-desk.md).

## Why ADK and A2A

- **A2A** gives any caller agent a standard way to discover the gateway (agent card with each action's tier and the `agentPassport` scheme) and talk to it, whatever framework it was built with.
- **ADK** runs the business agent with tools and session state. The verified principal and caller travel in session state, which keeps authorisation out of the prompt. The same backend interface can forward to an agent deployed separately on Vertex AI Agent Engine.
