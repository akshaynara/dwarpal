"""Demo identities: a trusted patient-identity issuer, legitimate agents, and attackers.

DEMO ONLY. In production the issuer is an external identity provider; the gateway only ever holds
issuer *public* keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from ..identity import KeyPair, Principal, TrustStore, issue_passport
from .callers import CallerAgent

ISSUER_ID = "idp.healthwallet.example"

RIYA = Principal(id="P-1001", name="Riya Sharma")
VIKRAM = Principal(id="P-2002", name="Vikram Rao")


@dataclass
class DemoWorld:
    audience: str
    issuer_key: KeyPair
    keys: dict[str, KeyPair]

    @classmethod
    def load(cls, data_dir: Path, audience: str) -> "DemoWorld":
        kdir = data_dir / "demo-keys"
        names = ["riya_assistant", "medifetch", "disputebot", "attacker", "attacker_issuer"]
        return cls(audience=audience, issuer_key=KeyPair.load_or_create(kdir / "healthwallet-issuer.pem"),
                   keys={n: KeyPair.load_or_create(kdir / f"{n}.pem") for n in names})

    def trust(self, store: TrustStore) -> None:
        store.add(ISSUER_ID, self.issuer_key, label="HealthWallet patient identity (demo)")

    def _passport(self, key: str, agent_id: str, name: str, principal: Principal, scopes: list[str],
                  limits: dict | None = None, issuer_key: KeyPair | None = None) -> str:
        return issue_passport(issuer_key or self.issuer_key, issuer=ISSUER_ID, agent_key=self.keys[key],
                              agent_id=agent_id, agent_name=name, principal=principal, audience=self.audience,
                              scopes=scopes, limits=limits or {}, ttl_seconds=3600)

    def callers(self, http: httpx.AsyncClient, gateway_url: str) -> dict[str, CallerAgent]:
        def make(key: str, name: str, passport: str | None) -> CallerAgent:
            return CallerAgent(name=name, key=self.keys[key], passport=passport, audience=self.audience,
                               http=http, gateway_url=gateway_url)

        return {
            # Riya's personal assistant: may manage appointments and small co-pay refunds.
            "riya": make("riya_assistant", "Riya's Assistant", self._passport(
                "riya_assistant", "agent:riya-assistant", "Riya's Assistant", RIYA,
                ["appointments:read", "appointments:write", "billing:write"], {"max_amount_inr": 1000})),
            # A third-party agent with a valid passport for Vikram, read-only, which turns hostile.
            "medifetch": make("medifetch", "MediFetch Bot", self._passport(
                "medifetch", "agent:medifetch", "MediFetch Bot", VIKRAM, ["appointments:read"])),
            # Vikram's billing agent that wants to fight a charge.
            "disputebot": make("disputebot", "ClaimFighter", self._passport(
                "disputebot", "agent:claimfighter", "ClaimFighter", VIKRAM, ["billing:write", "appointments:read"])),
            # An attacker who mints its own passport claiming to be Riya's agent.
            "forger": make("attacker", "Riya's Assistant (forged)", self._passport(
                "attacker", "agent:riya-assistant", "Riya's Assistant", RIYA,
                ["appointments:read", "appointments:write", "records:read"],
                issuer_key=self.keys["attacker_issuer"])),
            # No passport at all.
            "anonymous": make("attacker", "Anonymous caller", None),
        }
