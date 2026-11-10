"""Dwarpal (द्वारपाल) — the gatekeeper for inbound AI-agent traffic.

When a customer's AI agent calls a business's AI agent, Dwarpal sits in between and answers:

1. Identity     — is the caller an AI agent, and who does it act for?
2. Delegation   — what is it authorized to do, and up to what limit?
3. Security     — is it trying to prompt-inject the business agent?
4. Protocol     — cooperative, rule-bound tasks go over structured A2A, not free text.
5. Escalation   — contested or high-stakes cases go to a human early; turn, cost and loop limits.
6. Audit        — a tamper-evident, signed record of what each agent asked for and agreed to.
"""

__version__ = "0.1.0"
