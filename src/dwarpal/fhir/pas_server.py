"""A small Da Vinci PAS-style FHIR server for prior-authorisation inquiries.

It stands in for a payor's FHIR PA API (the kind CMS-0057-F requires for Medicare Advantage,
Medicaid/CHIP managed care and federal-exchange plans from January 2027), so the demo's FHIR rail
makes real HTTP calls with real FHIR-shaped resources. Point the rail at a real sandbox with
DWARPAL_PAYOR_FHIR_URL and DWARPAL_PAYOR_FHIR_TOKEN instead.

Endpoints:
  GET  /metadata             CapabilityStatement
  POST /Claim/$inquire       PAS inquiry Bundle in, response Bundle with a ClaimResponse out

The response follows the PAS pattern: ClaimResponse.item carries a reviewAction extension whose
code comes from X12 code list 306 (A1 certified, A3 not certified, A4 pended). This is a teaching
implementation, not a conformant PAS server.
"""

from __future__ import annotations

import hmac
import uuid
from typing import Any, Callable

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

FHIR_JSON = "application/fhir+json"
REVIEW_ACTION_URL = "http://hl7.org/fhir/us/davinci-pas/StructureDefinition/extension-reviewAction"
X12_306 = "https://codesystem.x12.org/005010/306"
SERVED_PLANS = {"MA"}  # only the plan lines that have a FHIR API


def outcome(status: int, code: str, text: str) -> JSONResponse:
    return JSONResponse(status_code=status, media_type=FHIR_JSON, content={
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": code, "diagnostics": text}]})


def build_inquiry(*, auth_number: str, member_id: str, member_dob: str, npi: str, cpt: str | None = None) -> dict:
    """The request a provider-side system (or Dwarpal's FHIR rail) sends."""
    claim = {
        "resourceType": "Claim", "id": "inquiry-1", "status": "active", "use": "preauthorization",
        "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type", "code": "professional"}]},
        "patient": {"reference": "Patient/member-1"},
        "provider": {"identifier": {"system": "http://hl7.org/fhir/sid/us-npi", "value": npi}},
        "identifier": [{"system": "urn:brightline:auth", "value": auth_number}],
        "item": [{"sequence": 1, "productOrService": {
            "coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": cpt or "unknown"}]}}],
    }
    patient = {"resourceType": "Patient", "id": "member-1",
               "identifier": [{"system": "urn:brightline:member", "value": member_id}], "birthDate": member_dob}
    return {"resourceType": "Bundle", "type": "collection", "id": str(uuid.uuid4()),
            "entry": [{"resource": claim}, {"resource": patient}]}


def read_review_action(bundle: dict) -> tuple[str | None, str]:
    """Pull the reviewAction code and the ClaimResponse outcome out of a PAS response Bundle."""
    for e in bundle.get("entry") or []:
        r = e.get("resource") or {}
        if r.get("resourceType") != "ClaimResponse":
            continue
        for item in r.get("item") or []:
            for ext in item.get("extension") or []:
                if ext.get("url") == REVIEW_ACTION_URL:
                    for sub in ext.get("extension") or []:
                        for c in ((sub.get("valueCodeableConcept") or {}).get("coding") or []):
                            if c.get("system") == X12_306:
                                return c.get("code"), r.get("outcome", "")
        return None, r.get("outcome", "")
    return None, ""


def pas_router(get_data: Callable[[], dict[str, Any]], *, token: str, is_down: Callable[[], bool]) -> APIRouter:
    """get_data returns the payor's records (auths, members, providers)."""
    router = APIRouter()

    @router.get("/metadata")
    async def metadata():
        return JSONResponse(media_type=FHIR_JSON, content={
            "resourceType": "CapabilityStatement", "status": "active", "kind": "instance", "fhirVersion": "4.0.1",
            "format": ["json"], "implementationGuide": ["http://hl7.org/fhir/us/davinci-pas/ImplementationGuide/hl7.fhir.us.davinci-pas"],
            "rest": [{"mode": "server", "resource": [{"type": "Claim", "operation": [
                {"name": "inquire", "definition": "http://hl7.org/fhir/us/davinci-pas/OperationDefinition/Claim-inquiry"}]}]}]})

    @router.post("/Claim/$inquire")
    async def inquire(request: Request, authorization: str | None = Header(default=None)):
        if is_down():
            return outcome(503, "transient", "Prior-auth API temporarily unavailable.")
        if not authorization or not hmac.compare_digest(authorization, f"Bearer {token}"):
            return outcome(401, "security", "Missing or invalid bearer token.")
        try:
            bundle = await request.json()
            claim = next(e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Claim")
            patient = next(e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient")
            auth_number = claim["identifier"][0]["value"]
            npi = claim["provider"]["identifier"]["value"]
            member_id = patient["identifier"][0]["value"]
            dob = patient.get("birthDate")
        except (KeyError, StopIteration, IndexError, TypeError, ValueError):
            return outcome(400, "invalid", "Expected a PAS inquiry Bundle with a Claim and a Patient.")
        data = get_data()
        auth, member = data["auths"].get(auth_number), data["members"].get(member_id)
        if (not auth or not member or auth["member"] != member_id or member["dob"] != dob or auth["npi"] != npi
                or member["plan"] not in SERVED_PLANS):
            return outcome(404, "not-found", "No matching prior authorisation.")
        response = {
            "resourceType": "ClaimResponse", "id": str(uuid.uuid4()), "status": "active", "use": "preauthorization",
            "outcome": "queued" if auth["review"] == "A4" else "complete", "preAuthRef": auth_number,
            "patient": {"reference": "Patient/member-1"},
            "insurer": {"display": "Brightline Health Plan"},
            "item": [{"itemSequence": 1, "extension": [{"url": REVIEW_ACTION_URL, "extension": [
                {"url": "code", "valueCodeableConcept": {"coding": [{"system": X12_306, "code": auth["review"]}]}}]}]}],
        }
        if auth.get("valid_from"):
            response["preAuthPeriod"] = {"start": auth["valid_from"], "end": auth["valid_to"]}
        return JSONResponse(media_type=FHIR_JSON, content={
            "resourceType": "Bundle", "type": "collection", "entry": [{"resource": response}]})

    return router
