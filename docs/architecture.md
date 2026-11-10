# Dwarpal architecture

## Threat model

Dwarpal protects a business AI agent (the *callee*) from AI agents acting for customers (*callers*). Callers may be:

- **Legitimate but over-reaching**: a real assistant asking for more than its user delegated
- **Compromised**: a valid, verified agent that has been prompt-injected upstream and now carries hostile instructions
- **Impersonators**: agents claiming to act for someone they don't
- **Replayers**: anyone who captured a valid request in transit
- **Runaway**: two agents stuck in a loop, or an agent pushing a contested matter until something gives

What Dwarpal does not try to do is decide contested matters between agents. Disputes have competing interests and no agreed process, so two agents arguing only burns compute. Those go to a person, with full context.

## Request lifecycle

```
caller ──A2A──► parse ─► identity ─► limits ─► protocol ─► security ─► delegation ─► human-only? ─┐
                                                                                                   │
                         audit + receipt ◄── data guard ◄── business agent (ADK) ◄──── no ─────────┤
                                                                         human queue ◄──── yes ────┘
```

Each stage appends a `Check(name, status, detail)`. The first `fail` or `handoff` ends the pipeline. Every request, refused or not, is written to the audit chain, so the record of what was *attempted* is as complete as the record of what was done.

### 1. Identity: passport + proof

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

`ai_disclosed` is required. Disclosure is the foundation, so a caller that won't say it's an AI isn't served.

**Request proof** (`typ: agent-proof+jwt`) signed by the key in `cnf` for every request: `aud`, `iat`, a random `nonce`, and `bh`, the SHA-256 of the canonical request body. The gateway checks the key matches the passport, the proof is under 60 seconds old, the nonce is unseen and the body hash matches. This is the same idea as OAuth DPoP: a passport alone is not enough, and a captured request can't be replayed or edited.

### 2. Limits

Per agent and A2A context: requests per minute, turns per session, a compute budget (one unit per model call), repeated identical requests (loops) and repeated refusals (probing). A breach locks the session and opens a human ticket. Later requests get the ticket id back instead of reaching the business agent.

### 3. Protocol

Agents must send a structured intent from the policy catalog. A free-text-only request gets the catalog back (`needs_structure`), which is the agent equivalent of *press 1 for appointments*, and costs nothing. Parameters not whitelisted for the intent are stripped. The optional `note` is length-limited and is never forwarded.

### 4. Security

The shield scans every caller-controlled string, including stripped parameters, since an attempt is worth flagging even if it would have been discarded:

1. **Rules**: NFKC normalisation, zero-width characters removed, base64 segments decoded and re-scanned. Attack families are instruction override, role hijack, system-prompt exfiltration, delimiter injection, authority claims, tool coercion, data exfiltration, social engineering and exfiltration channels. Independent signals compound: `score = 1 − Π(1 − wᵢ)`.
2. **Gemini classifier**: catches paraphrases the rules miss (Gemini mode).
3. **Model Armor**: Google Cloud's managed prompt-injection and jailbreak filter.

The more important control is architectural: the business agent only ever receives the gateway's *verified task*. Even an attack that evaded every layer has no free-text channel into the agent's context.

### 5. Delegation

The required scope comes from policy, and the passport must grant it (`appointments:*` wildcards supported). For money intents, the effective cap is the lower of the passport's `max_amount_inr` and the policy's `max_amount_inr`. Object-level authorization, meaning whether *this* appointment belongs to *this* principal, happens inside the business tools, using the principal from ADK session state set by the gateway, never from the request. "Not found" and "not yours" return the same answer.

### 6. Escalation to a person

Intents marked `mode: human` in policy (disputes, clinical records) always open a ticket. A second push for the same intent in the same session returns the existing ticket rather than opening another.

### 7. Data guard

Every outbound response has `never_disclose_fields` removed at any depth, and `redact_patterns` (phone numbers, email, Aadhaar) applied to all strings.

### 8. Audit and receipts

Each record holds `seq, ts, event, data, prev` and gets `hash = sha256(canonical(record))` plus `sig = Ed25519(hash)` from the gateway key. Verification walks the chain and reports the first record whose link, content or signature fails. Editing, deleting, reordering or re-hashing without the key are all detected (see `tests/test_audit_and_guard.py`).

For completed actions the caller receives a receipt (`typ: dwarpal-receipt+jwt`) containing the request hash, result hash, intent, principal and the audit chain head at that moment, so both sides hold the same evidence.

## Why ADK and A2A

- **A2A** gives a standard way for any caller agent to discover the gateway (agent card with skills and the `agentPassport` security scheme) and talk to it, whatever framework the caller was built with.
- **ADK** runs the business agent with tools and session state. The gateway passes the verified principal through session state, which keeps authorization out of the prompt. The same backend interface can forward to an agent deployed separately on Vertex AI Agent Engine over A2A.
