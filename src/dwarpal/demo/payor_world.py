"""Demo identities for the payor: a provider-credentialing issuer and providers' agents.

DEMO ONLY. In production the issuer is an external credentialing service (or the payor's own provider
portal); the gateway only ever holds issuer public keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from ..identity import KeyPair, Principal, TrustStore, issue_passport
from .callers import CallerAgent

ISSUER_ID = "idp.providerpass.example"
SUNRISE = Principal(id="1234567893", name="Sunrise Family Clinic", type="provider_org")
LAKESIDE = Principal(id="1245319599", name="Lakeside Orthopaedics", type="provider_org")


@dataclass
class PayorWorld:
    audience: str
    issuer_key: KeyPair
    keys: dict[str, KeyPair]

    @classmethod
    def load(cls, data_dir: Path, audience: str) -> "PayorWorld":
        kdir = data_dir / "demo-keys"
        names = ["sunrise_rcm", "lakeside_agent", "guest", "forger", "forger_issuer"]
        return cls(audience=audience, issuer_key=KeyPair.load_or_create(kdir / "providerpass-issuer.pem"),
                   keys={n: KeyPair.load_or_create(kdir / f"{n}.pem") for n in names})

    def trust(self, store: TrustStore) -> None:
        store.add(ISSUER_ID, self.issuer_key, label="ProviderPass credentialing (demo)")

    def _passport(self, key: str, agent_id: str, name: str, principal: Principal, scopes: list[str],
                  issuer_key: KeyPair | None = None) -> str:
        return issue_passport(issuer_key or self.issuer_key, issuer=ISSUER_ID, agent_key=self.keys[key],
                              agent_id=agent_id, agent_name=name, principal=principal, audience=self.audience,
                              scopes=scopes, ttl_seconds=3600)

    def callers(self, http: httpx.AsyncClient, gateway_url: str) -> dict[str, CallerAgent]:
        def make(key: str, name: str, passport: str | None) -> CallerAgent:
            return CallerAgent(name=name, key=self.keys[key], passport=passport, audience=self.audience,
                               http=http, gateway_url=gateway_url)

        return {
            # Sunrise Family Clinic's revenue-cycle agent: it chases prior auths and checks eligibility.
            "sunrise": make("sunrise_rcm", "Sunrise RCM Agent", self._passport(
                "sunrise_rcm", "agent:sunrise-rcm", "Sunrise RCM Agent", SUNRISE,
                ["pa:read", "pa:write", "eligibility:read"])),
            # Another provider's agent, properly credentialed for its own practice only.
            "lakeside": make("lakeside_agent", "Lakeside PA Bot", self._passport(
                "lakeside_agent", "agent:lakeside-pa", "Lakeside PA Bot", LAKESIDE, ["pa:read"])),
            # Any agent, no credential: fine for published prior-auth rules.
            "guest": make("guest", "Guest agent", None),
        }
