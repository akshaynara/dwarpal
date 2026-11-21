# Connecting a voice platform

Most AI agents will reach businesses by phone for years. Dwarpal's phone lane lets the business's own voice platform ask the gateway the same question an A2A caller does, so one policy covers both.

Your voice platform keeps doing what it does today: answering the call, recognising the caller's intent, and verifying the patient. On each turn it calls Dwarpal, and Dwarpal says what to do next and what to say.

```
caller ──phone──► your voice platform ──POST /voice/turn──► Dwarpal ──► business agent ──► business service
                  (IVR / CX Agent Studio)    channel key                (same as A2A)
```

Typical places to make the call: a webhook or tool call from a CX Agent Studio or Dialogflow CX agent, or a data step in your contact-centre flow.

## Authentication

Only your voice platform may call `/voice/turn`, because it vouches for what the IVR verified. Send the channel key in a header:

```
X-Dwarpal-Channel-Key: <key>
```

The key is generated on first start in `.dwarpal/channel.key`, or set it with `DWARPAL_CHANNEL_KEY`. Callers never reach this endpoint directly.

## Request

```json
{
  "call_id": "call-8f2a91",
  "intent": "cancel_appointment",
  "params": {"appointment_id": "APT-501", "reason": "travelling"},
  "signals": {
    "attestation": "A",
    "ai_voice_score": 0.04,
    "declared_agent": false
  },
  "verification": {"principal_id": "P-1001", "name": "Riya Sharma", "method": "patient_id+dob"},
  "step_up": null
}
```

| Field | What to send |
|---|---|
| `call_id` | Your call or conversation id. It is the session for limits, history and lane codes |
| `intent`, `params` | What your NLU recognised, using the intent names in the policy file |
| `signals.attestation` | The carrier's STIR/SHAKEN attestation for the calling number (`A`, `B`, `C` or `none`), if your platform exposes it |
| `signals.ai_voice_score` | A 0–1 score from your AI-voice or deepfake detection vendor, if you use one |
| `signals.declared_agent` | `true` if the caller pressed 1 or said it is an AI agent |
| `verification` | Set once your IVR has verified the patient (for example patient id and date of birth). Omit it until then |
| `step_up` | The challenge id, when resending a turn after the caller completed step-up |

**Scores only.** Send numeric scores, never audio. If the payload includes `audio`, `recording_url`, `transcript` or `utterance_audio`, Dwarpal drops them before doing anything else and records only that they were dropped. The list is `voice.drop_fields` in the policy.

## Response

```json
{
  "say": "For your security, please enter your clinic PIN on the keypad. We're also sending a code to your registered number.",
  "next": "collect_step_up",
  "outcome": "step_up_required",
  "tier": "binding",
  "step_up": {"challenge_id": "SU-D2250B9B", "required_strength": 2, "methods": [...]},
  "agent_lane": null,
  "ticket_id": null,
  "receipt": null,
  "reason_codes": ["step_up_required"],
  "checks": [...],
  "reply": "Collect step-up from the caller (keypad PIN, SMS code or app), then resend this turn with step_up=SU-D2250B9B."
}
```

| `next` | What your platform does |
|---|---|
| `continue` | Speak `say` and carry on the conversation |
| `verify_caller` | Run your verification (patient id, date of birth), then resend with `verification` |
| `collect_step_up` | Collect the step-up methods listed (keypad PIN, SMS code, app approval), report them to the customer approval endpoint, then resend the turn with `step_up` |
| `transfer_to_person` | Transfer to a person. `ticket_id` carries the case, with the request history attached |

`say` is written by the business (templates under `customer_messages` in the policy, with `voice:` keys tried first). Speak it as-is.

## What the signals do

Signals restrict, proof grants. Nothing in `signals` ever unlocks an action, and nothing ever blocks a caller outright:

| Signal | Effect |
|---|---|
| AI-voice score at or above `ai_voice_threshold`, not declared | Account-tier actions also need step-up |
| `declared_agent: true` | The caller is offered the agent lane (a lane code in `agent_lane`, also spoken in `say`). On the call, account-tier actions need step-up |
| Attestation `C` or `none` | Account-tier changes need step-up; reading doesn't |
| Attestation `A`, low AI-voice score | Nothing extra. A strong signal never replaces verification |

Every step-up can be passed without speaking (keypad, SMS code, app). A person using a speech-generating device may be flagged as an AI voice; they hear exactly the same words as anyone else and are served as themselves once they complete step-up. Never tell a caller they "sound like a bot".

Telling the truth never costs more than being caught: a declared agent meets the same friction as a detected one, and is also offered the agent lane.

## The lane switch

When a caller declares it is an AI agent, the response carries:

```json
"agent_lane": {
  "code": "FLQJ9S",
  "spoken": "F, L, Q, J, nine, S",
  "expires_at": 1763749200,
  "agent_card": "/.well-known/agent-card.json",
  "pending": {"intent": "reschedule_appointment", "params": {"appointment_id": "APT-501", "new_slot": "2026-11-13T11:00"}}
}
```

The agent then connects over A2A with its own passport and `dwarpal_lane_code` in the message metadata (and in its signed body). Dwarpal checks that the passport's patient matches the one verified on the call, links the two sessions, withdraws any approval prompt still pending from the call, and returns `continued_from_call` with the earlier requests. The code links context; it never grants anything. An agent without a passport gets only open-tier actions, as on any other A2A request.

## Try it

```bash
dwarpal serve
KEY=$(cat .dwarpal/channel.key)
curl -s localhost:8080/voice/turn -H "X-Dwarpal-Channel-Key: $KEY" -H 'content-type: application/json' \
  -d '{"call_id":"c1","intent":"clinic_info","params":{"topic":"hours"}}'
```

The dashboard's phone-lane scenarios use a simulated clinic IVR (`dwarpal.demo.scenarios.IVR`) that calls this endpoint exactly as a real platform would.
