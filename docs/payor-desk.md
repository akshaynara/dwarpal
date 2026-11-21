# The payor desk

Providers' staff spend hours on the phone with payors checking prior-authorisation (PA) status and eligibility, and providers are now putting AI agents (often voice bots) on those calls. The questions are cooperative, rule-bound and high-volume: the low-hanging case for agent-to-agent traffic. The hard part is fragmentation and authorisation, not the conversation.

Dwarpal's second demo business is **Brightline Health Plan** (fictional), mounted at `/payor` on the same gateway as the clinic. The clinic from the first demo, Sunrise Family Clinic, is now the *provider* calling in.

## Who the caller acts for

At a payor, the caller's principal is a **provider organisation**, identified by NPI. A provider-credentialing issuer (`idp.providerpass.example` in the demo) vouches for which NPI an agent acts for and which scopes it holds (`pa:read`, `eligibility:read`, `pa:write`).

Every lookup is scoped twice:

- **The auth must belong to this provider.** The PA's requesting NPI must match the caller's NPI.
- **The member details must match.** Member id and date of birth, as a payor rep would ask on the phone.

A missing auth, another provider's auth and a wrong date of birth all get the identical answer, so the endpoint can't be used to probe. On the phone, the payor's IVR verifies NPI plus member id and date of birth, which is how provider lines verify callers today.

## What it answers

| Action | Tier | Notes |
|---|---|---|
| `pa_requirements` | open | Published PA rules by procedure code and plan. Any agent, no credential |
| `plan_contacts` | open | Hours, PA fax, how to submit |
| `pa_status` | account | Status, service, units, validity dates and what the payor still needs |
| `eligibility_benefits` | account | Coverage and copay |
| `pa_submit` | human | Needs clinical documents and a reviewer. 72-hour due time |
| `pa_appeal` | human | Appeals and peer-to-peer reviews |

Minimum necessary: members appear as initials, and the data guard drops diagnosis codes, SSNs and addresses even if a back-end system returns them.

## Rails per plan

Each plan's rails are declared in the payor's policy file and tried in order:

```yaml
rails:
  MA:     {pa_status: [fhir_pas, x12_278], eligibility: [x12_270]}
  PPO:    {pa_status: [x12_278],           eligibility: [x12_270]}
  LEGACY: {pa_status: [],                  eligibility: []}
```

| Plan | PA status rail | Why |
|---|---|---|
| Medicare Advantage | FHIR PA API (Da Vinci PAS-style `$inquire`), then X12 278 | CMS-0057-F requires FHIR PA APIs for Medicare Advantage, Medicaid/CHIP managed care and federal-exchange plans from 1 January 2027 |
| Commercial PPO | X12 278 | Commercial fully insured and self-funded employer plans aren't covered by the rule |
| Employer Classic | none | No electronic inquiry: the payor hands it to a representative |

When no rail can answer, the payor's service returns `needs_human`. The gateway opens a ticket with a due time and join code, tells the calling agent not to retry before then, and a repeat request returns the same ticket. Fragmentation becomes configuration: changing one plan's rails is a policy edit, not code.

The FHIR and X12 connectors in `business/payor.py` are **simulations** that build and parse FHIR- and X12-*shaped* messages so the flow is visible and testable. Production would call the payor's real FHIR endpoint, an X12 clearinghouse, or both. Review codes follow the X12 HCR / PAS convention (`A1` certified, `A3` not certified, `A4` pended), as a subset for illustration.

## Starting with a real client

As practitioners pointed out, what's worth chasing first is client-specific. The intended path is:

1. Pick the narrowest high-volume read (usually PA status for one line of business).
2. Declare that plan's actual rails, with a person as the fallback.
3. Turn on the phone lane first, since that's where provider bots already are, and offer the agent lane to those that declare themselves.
4. Measure: share of agent calls, lane split, how many answers came from each rail, and how many went to a person.
