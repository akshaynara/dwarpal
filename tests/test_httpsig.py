"""RFC 9421 HTTP Message Signatures: unit checks and end-to-end attacks."""

import base64
import json
import time

import httpx
import pytest

from dwarpal.app import create_app
from dwarpal.config import Settings
from dwarpal.httpsig import TAG, content_digest, signature_base, sign, verify
from dwarpal.identity import KeyPair, b64u

BASE = "http://dwarpal.test"


def signed(key, body=b'{"x":1}', **kw):
    headers = {"content-type": "application/json", "authorization": "AgentPassport abc"}
    headers.update(sign(key, method="POST", authority="dwarpal.test", path="/a2a", headers=headers, body=body, **kw))
    return headers


def check(key, headers, body=b'{"x":1}', path="/a2a", **kw):
    return verify(method="POST", authority="dwarpal.test", path=path, headers=headers, body=body,
                  key_for=lambda kid: key.public_key if kid == key.kid else None, **kw)


def test_content_digest_matches_rfc9530_shape():
    assert content_digest(b"hello") == "sha-256=:LPJNul+wow4m6DsqxbninhsWHlwfp0JecwQzYpOLmCQ=:"


def test_round_trip_and_profile():
    k = KeyPair.generate()
    h = signed(k)
    assert h["signature-input"].startswith('sig1=("@method" "@authority" "@path" "content-digest" "content-type" '
                                           '"authorization");created=')
    assert f'tag="{TAG}"' in h["signature-input"] and 'alg="ed25519"' in h["signature-input"]
    r = check(k, h)
    assert r["valid"] and r["kid"] == k.kid and r["nonce"]


def test_signature_base_format():
    base = signature_base({"@method": "POST", "@path": "/a2a"}, ["@method", "@path"], '("@method" "@path");created=1')
    assert base == b'"@method": POST\n"@path": /a2a\n"@signature-params": ("@method" "@path");created=1'


@pytest.mark.parametrize("mutate,code", [
    (lambda h, b: (h, b + b" "), "body_tampered"),
    (lambda h, b: ({**h, "authorization": "AgentPassport other"}, b), "bad_signature"),
    (lambda h, b: ({k: v for k, v in h.items() if k != "signature"}, b), "missing_signature"),
    (lambda h, b: ({**h, "signature": "sig2=" + h["signature"].split("=", 1)[1]}, b), "malformed_signature"),
])
def test_tampering_is_caught(mutate, code):
    k = KeyPair.generate()
    h, b = mutate(signed(k), b'{"x":1}')
    assert check(k, h, body=b)["error"] == code


def test_wrong_path_or_key_or_age():
    k = KeyPair.generate()
    assert check(k, signed(k), path="/payor/a2a")["error"] == "bad_signature"
    assert check(KeyPair.generate(), signed(k))["error"] == "key_mismatch"
    assert check(k, signed(k, now=int(time.time()) - 600))["error"] == "stale_proof"


def test_must_cover_the_required_components():
    k = KeyPair.generate()
    h = signed(k)
    weak = h["signature-input"].replace('"authorization"', "").replace("  ", " ").replace(" )", ")")
    assert check(k, {**h, "signature-input": weak})["error"] == "weak_signature"


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(llm_mode="mock", data_dir=tmp_path, public_url=BASE), businesses=["clinic"])


async def test_jws_callers_still_work(app):
    rt = app.state.runtime
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:
        riya = rt.world.callers(http, BASE)["riya"]
        riya.signing, riya.auth = "jws", None
        r = await riya.send(intent="appointment_status", params={"appointment_id": "APT-501"})
    assert r.outcome == "completed"
    assert "request proof signed" in next(c["detail"] for c in r.payload["checks"] if c["name"] == "identity")


async def test_forged_verified_header_is_stripped(app):
    """An attacker sends the internal 'already verified' header itself, with no real signature."""
    rt = app.state.runtime
    fake = b64u(json.dumps({"valid": True, "kid": "x", "nonce": "n", "created": int(time.time()),
                            "passport_sha": "x"}).encode())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE,
                                 headers={"X-Dwarpal-Httpsig": fake}) as http:
        riya = rt.world.callers(http, BASE)["riya"]
        riya.signing, riya.auth = "none", None  # no signature, no proof
        r = await riya.send(intent="appointment_status", params={"appointment_id": "APT-501"})
    assert r.outcome == "denied" and r.data["error"] == "missing_proof"


async def test_signed_with_one_passport_used_with_another(app):
    """Signature valid under the attacker's own passport header, victim's passport in the metadata."""
    rt = app.state.runtime
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:
        callers = rt.world.callers(http, BASE)
        attacker, victim = callers["medifetch"], callers["riya"]
        attacker.passport = attacker.passport  # attacker signs under its own passport (Authorization header)
        msg = attacker.prepare(intent="appointment_status", params={"appointment_id": "APT-501"})
        msg.metadata.update({"dwarpal_passport": victim.passport})  # but claims to be Riya's agent
        r = await attacker.deliver(msg)
    assert r.outcome == "denied" and r.data["error"] == "key_mismatch"


def test_signature_header_is_standard_base64():
    k = KeyPair.generate()
    sig = signed(k)["signature"]
    assert sig.startswith("sig1=:") and sig.endswith(":")
    base64.b64decode(sig[6:-1])


async def test_agent_card_declares_the_passport_extension(app):
    from dwarpal.a2a_server import EXTENSION_URI

    rt = app.state.runtime
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:
        card = (await http.get("/.well-known/agent-card.json")).json()
        ext = card["capabilities"]["extensions"][0]
        assert ext["uri"] == EXTENSION_URI and not ext.get("required", False)  # proto JSON omits false
        assert ext["params"]["request_signing"]["profile"] == "RFC 9421"
        assert ext["params"]["tiers"]["cancel_appointment"] == "binding"
        riya = rt.world.callers(http, BASE)["riya"]
        r = await riya.send(intent="appointment_status", params={"appointment_id": "APT-501"})
    assert "agent-passport A2A extension" in next(c["detail"] for c in r.payload["checks"] if c["name"] == "identity")
