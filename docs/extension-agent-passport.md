# A2A extension: Dwarpal agent passport (v1)

**URI:** `https://github.com/akshaynara/dwarpal/blob/main/docs/extension-agent-passport.md`

This extension lets an A2A agent prove who it acts for and what it may do, so a business can serve it by action rather than by guessing. A server that supports it declares it in its agent card; a client activates it per request.

## Declaration

```json
"capabilities": {
  "extensions": [{
    "uri": "https://github.com/akshaynara/dwarpal/blob/main/docs/extension-agent-passport.md",
    "required": false,
    "params": {
      "version": "1",
      "audience": "clinic.sunrise.example",
      "trusted_issuers": ["idp.healthwallet.example"],
      "passport": {"header": "Authorization: AgentPassport <jws>", "metadata_key": "dwarpal_passport",
                   "typ": "agent-passport+jwt", "alg": "EdDSA"},
      "request_signing": {"profile": "RFC 9421", "alg": "ed25519", "tag": "dwarpal-agent",
                          "covered": ["@method", "@authority", "@path", "content-digest", "content-type", "authorization"]},
      "metadata_keys": ["dwarpal_passport", "dwarpal_step_up", "dwarpal_lane_code", "dwarpal_proof"],
      "tiers": {"clinic_info": "open", "appointment_status": "account", "cancel_appointment": "binding"},
      "step_up": {"required_strength": 2, "resend_with": "dwarpal_step_up"}
    }
  }]
}
```

`required` is `false` because open-tier actions need no credential. Account and binding actions do.

## Activation

The client lists the URI in the `A2A-Extensions` HTTP header and in `message.extensions`.

## Passport

A compact JWS (`typ: agent-passport+jwt`, `alg: EdDSA`) from an issuer listed in `trusted_issuers`:

| Claim | Meaning |
|---|---|
| `iss` | Issuer id, which must be trusted by the business |
| `sub` | The agent's id |
| `aud` | The business id from `params.audience`; a passport for one business is useless at another |
| `principal` | `{id, name, type}`: who the agent acts for (a person, or a provider organisation by NPI) |
| `scopes`, `limits` | What it may do, e.g. `appointments:write`, `{"max_amount_inr": 1000}` |
| `ai_disclosed` | Must be `true`. Disclosure is part of the credential |
| `cnf` | `{kid, pub}`: the agent's own Ed25519 key, which must sign every request |
| `iat`, `exp`, `jti` | Validity and revocation id |

Send it as `Authorization: AgentPassport <jws>`. It may also appear as `dwarpal_passport` in the message metadata; if both are present they must be the same token.

## Request signing (RFC 9421)

Every request carrying a passport is signed with HTTP Message Signatures by the key in `cnf`:

```
Content-Digest:  sha-256=:<base64 SHA-256 of the body>:
Signature-Input: sig1=("@method" "@authority" "@path" "content-digest" "content-type" "authorization");created=<unix>;keyid="<cnf kid>";alg="ed25519";nonce="<random>";tag="dwarpal-agent"
Signature:       sig1=:<base64 Ed25519 signature>:
```

The server rejects signatures that don't cover `@method`, `@authority`, `@path`, `content-digest` and `authorization`, are older than 60 seconds, reuse a nonce, use a key other than the passport's `cnf`, or whose digest doesn't match the body. Because the digest covers the whole JSON-RPC body, the envelope, step-up id and lane code are all protected.

*Fallback:* a client that can't sign HTTP requests may put a `dwarpal_proof` (an `agent-proof+jwt` JWS over `{envelope, text, step_up?, lane_code?}` with `aud`, `iat`, `nonce` and a body hash) in the metadata instead.

## Request

One DataPart with the structured intent, plus metadata:

```json
{"intent": "cancel_appointment", "params": {"appointment_id": "APT-501", "reason": "travelling"}, "note": "optional, short"}
```

| Metadata key | When |
|---|---|
| `dwarpal_step_up` | Resending a binding action after the customer approved; the challenge id |
| `dwarpal_lane_code` | Continuing a phone call in the agent lane; the code the call offered |

## Response

A TextPart for the agent, and a DataPart:

| Field | Meaning |
|---|---|
| `outcome` | `completed`, `rejected`, `denied`, `escalated`, `step_up_required`, `needs_structure`, `error` |
| `tier` | `open`, `account`, `binding` or `human` |
| `checks` | Every check (`identity`, `protocol`, `security`, `delegation`, `step_up`, `escalation`, `data_guard`, `audit`) with status and reason |
| `agent_outcome` | `decision`, `reason_codes`, `next_steps`, `retry_after_seconds`, `expected_by`, `relay_customer_message` |
| `customer_message` | What the customer should hear, written by the business. Relay it as-is |
| `receipt` | For completed actions: a signed `dwarpal-receipt+jwt` including `customer_message_sha256` |
| `ticket_id`, `join_code` | When a person takes over; give the join code to the customer |
| `step_up` | `{challenge_id, expires_at, required_strength, methods}` when the customer must approve |
| `continued_from_call` | When a lane code linked this session to a phone call |
| `data` | The business's answer, after the data guard |

## Step-up

`step_up_required` means the customer must approve this exact action on their own device. After approval, resend the identical request with `dwarpal_step_up`. Polling while approval is pending doesn't count toward session limits; honour `retry_after_seconds`.

## Reference implementation

[github.com/akshaynara/dwarpal](https://github.com/akshaynara/dwarpal): `httpsig.py` (RFC 9421), `identity.py` (passports), `a2a_server.py` (declaration), `demo/callers.py` (a client).
