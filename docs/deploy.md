# Deploying Dwarpal

## Before anything: run the doctor

```bash
dwarpal doctor          # installation, policies, audit chains, issuers, Gemini, one end-to-end scenario
dwarpal doctor --quick  # skip the scenario
```

With `GOOGLE_API_KEY` set, the doctor makes a real Gemini call and runs a scenario through the Gemini-backed agent. If it reports "scripted fallback answered", Gemini failed and the offline model covered for it; the line above it says why (often a wrong model name or key). If it reports "no reply", something on the network (a VPN, office proxy or firewall) is blocking `generativelanguage.googleapis.com`; every Gemini call times out after `DWARPAL_LLM_TIMEOUT` seconds, so the gateway keeps answering with the fallback instead of hanging.

## Demo mode vs production mode

| | `DWARPAL_MODE=demo` (default) | `DWARPAL_MODE=production` |
|---|---|---|
| Trusted issuers | Demo issuers plus any in the policy file | **Only** issuers with a `public_key` in the policy file |
| Scenario, live, desk, tamper, reset endpoints | On | Off (`DWARPAL_DEMO=true` to override) |
| Dashboard and admin API | Open | Bearer `DWARPAL_ADMIN_TOKEN` required (startup fails without it) |
| Step-up approvals | Dashboard phone card | Only your identity provider, with `X-Dwarpal-IdP-Key: $DWARPAL_IDP_KEY` |
| Public endpoints | | Agent cards, `/a2a` (passport + signature), `/voice/turn` (channel key), `/api/join/{code}`, `/api/receipts/verify`, `/healthz` |

Open the dashboard with `https://<host>/#token=<admin token>` once; the browser keeps it for the session only.

## Setting up a pilot

1. **Keys for the issuer** (your identity provider, or the business itself for a pilot):
   ```bash
   dwarpal keygen --out issuer.pem
   ```
   Put the printed `public_key` into the policy:
   ```yaml
   trusted_issuers:
     - id: idp.yourcompany.example
       label: Your identity provider
       public_key: <printed public_key>
   ```
2. **Each agent gets its own key and a passport:**
   ```bash
   dwarpal keygen --out agent.pem
   dwarpal issue-passport --issuer-key issuer.pem --issuer idp.yourcompany.example \
     --audience clinic.sunrise.example --agent-id agent:pilot --agent-key agent.pem \
     --principal-id P-1001 --principal-name "Riya Sharma" \
     --scopes appointments:read,appointments:write --ttl 86400
   ```
   The agent sends the passport in `Authorization: AgentPassport <jws>` and signs every request with RFC 9421 using its key (see [the extension spec](extension-agent-passport.md); `dwarpal/demo/callers.py` is a working client).
3. **Your voice platform** calls `/voice/turn` with `X-Dwarpal-Channel-Key` (see [voice integration](voice-integration.md)).
4. **Your identity provider** posts step-up results to `/api/stepup/{challenge_id}/approve` with `X-Dwarpal-IdP-Key` and the completed methods.
5. **Your business systems** replace the demo services: implement the same methods as `ClinicService` or `PayorService` against your scheduling, billing or FHIR systems, and register a kit in `businesses.py`.

## Google Cloud Run

```bash
gcloud run deploy dwarpal --source . --region asia-south1 \
  --set-env-vars DWARPAL_MODE=production,DWARPAL_PUBLIC_URL=https://<service-url>,DWARPAL_DATA_DIR=/data/dwarpal \
  --set-secrets DWARPAL_ADMIN_TOKEN=dwarpal-admin:latest,DWARPAL_IDP_KEY=dwarpal-idp:latest,DWARPAL_CHANNEL_KEY=dwarpal-channel:latest,GOOGLE_API_KEY=gemini-key:latest \
  --min-instances 1 --max-instances 1
```

**State, plainly:** Dwarpal keeps tickets, step-up approvals, lane codes and seen nonces in SQLite (`state.db`), the signed audit chain in `audit.jsonl`, and signing keys as PEM files, all under `DWARPAL_DATA_DIR`. Cloud Run's filesystem is ephemeral, so:

- For a pilot, run a single instance (`--max-instances 1`) with a persistent volume mounted at `DWARPAL_DATA_DIR`, or run on a VM or GKE with a persistent disk.
- To scale out, replace `store.Store` (four methods) with a shared store such as Cloud SQL or Firestore, ship the audit chain to Cloud Logging and BigQuery, and move signing keys to Cloud KMS. The interfaces are small for this reason.

Behind Cloud Run or a load balancer, `dwarpal serve` trusts forwarded headers, so RFC 9421's `@authority` matches the host agents signed.

## Health and monitoring

- `GET /healthz` returns `{"ok": true, "version": ..., "businesses": {"clinic": <audit chain intact>, ...}}`.
- `GET /api/metrics` (admin) returns pilot metrics computed from the audit chain.
- `dwarpal verify-audit` re-verifies the chain from disk.
