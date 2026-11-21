"""The businesses this Dwarpal deployment protects.

The gateway is the same for every business; what differs is plugged in as a *kit*: the policy file,
the business service (its existing systems), the ADK agent in front of that service, and, for the
demo, the identities and scenarios. Each kit is mounted under its own path prefix with its own A2A
agent card, voice endpoint, audit log and dashboard.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .business.clinic import build_clinic_agent
from .business.payor import PayorService
from .business.payor_agent import build_payor_agent
from .business.service import ClinicService
from .demo.payor_scenarios import PAYOR_SCENARIOS
from .demo.payor_world import PayorWorld
from .demo.scenarios import SCENARIOS, Scenario
from .demo.world import DemoWorld
from .policy import Policy


@dataclass
class BusinessKit:
    slug: str
    prefix: str  # "" for the default business, "/payor" etc. for the rest
    policy_file: str
    build_service: Callable[[Policy], Any]
    build_agent: Callable[..., Any]
    build_world: Callable[[Path, str], Any]
    ivr_verify: Callable[[Any], Callable[..., dict[str, str] | None]]
    scenarios: list[Scenario] = field(default_factory=list)
    live: bool = False  # has the "approve on stage" flow
    on_mount: Callable[[Any, Any], None] | None = None  # extra wiring once the app exists


def _clinic_verify(service: ClinicService):
    def verify(patient_id: str, dob: str) -> dict[str, str] | None:
        p = service.data["patients"].get(patient_id)
        if p and p["dob"] == dob:
            return {"principal_id": patient_id, "name": p["name"], "method": "patient_id+dob"}
        return None
    return verify


def _payor_verify(service: PayorService):
    def verify(npi: str, member_id: str, member_dob: str) -> dict[str, str] | None:
        # How payor phone lines verify providers today: NPI plus the member's id and date of birth.
        prov = service.data["providers"].get(npi)
        member = service.data["members"].get(member_id)
        if prov and member and member["dob"] == member_dob:
            return {"principal_id": npi, "name": prov["name"], "method": "npi+member_id+dob"}
        return None
    return verify


def _payor_mount(app: Any, rt: Any) -> None:
    """Connect the payor's FHIR rail: a real FHIR PA API if configured, otherwise the bundled PAS server."""
    import httpx

    from .fhir.pas_server import pas_router

    svc = rt.service
    real = os.getenv("DWARPAL_PAYOR_FHIR_URL")
    if real:
        svc.fhir_base, svc.fhir_token = real, os.getenv("DWARPAL_PAYOR_FHIR_TOKEN", "")
        svc.fhir_client = lambda: httpx.AsyncClient()
        return
    token = os.getenv("DWARPAL_PAYOR_FHIR_TOKEN") or secrets.token_urlsafe(16)
    app.include_router(pas_router(lambda: svc.data, token=token, is_down=lambda: svc.fhir_down),
                       prefix=f"{rt.kit.prefix}/fhir")
    svc.fhir_base = rt.settings.public_url.rstrip("/") + f"{rt.kit.prefix}/fhir"
    svc.fhir_token = token
    svc.fhir_client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app))


CLINIC = BusinessKit(
    slug="clinic", prefix="", policy_file="sunrise_clinic.yaml",
    build_service=lambda policy: ClinicService(),
    build_agent=build_clinic_agent,
    build_world=DemoWorld.load,
    ivr_verify=_clinic_verify,
    scenarios=SCENARIOS, live=True,
)

PAYOR = BusinessKit(
    slug="payor", prefix="/payor", policy_file="brightline_payor.yaml",
    build_service=lambda policy: PayorService(rails_by_plan=policy.raw.get("rails") or {}),
    build_agent=build_payor_agent,
    build_world=PayorWorld.load,
    ivr_verify=_payor_verify,
    scenarios=PAYOR_SCENARIOS,
    on_mount=_payor_mount,
)

KITS: dict[str, BusinessKit] = {k.slug: k for k in (CLINIC, PAYOR)}
