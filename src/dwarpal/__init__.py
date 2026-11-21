"""Dwarpal (द्वारपाल): gate the action, not the caller.

A policy and enforcement layer for AI agents calling a business. Every action declares the proof it
needs (open, account, binding, human), and the gateway checks it the same way whoever is asking:

  Identity    signed agent passports and per-request proofs (optional for open-tier actions)
  Protocol    structured A2A intents only; the caller's text never reaches the business agent
  Security    layered prompt-injection shield
  Delegation  scopes and money limits from the passport and the business policy
  Step-up     binding actions need the customer's own approval of that exact action
  Escalation  contested matters and runaway agents go to a person, with a due time and join code
  Data guard  fields that must never leave are dropped, PII redacted
  Audit       hash-chained, signed log; receipts that cover what the customer was told
"""

__version__ = "0.2.0"
