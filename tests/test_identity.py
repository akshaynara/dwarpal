import time

import pytest

from dwarpal.identity import (
    IdentityError,
    IdentityVerifier,
    KeyPair,
    Principal,
    TrustStore,
    body_hash,
    issue_passport,
    sign_request,
)

AUD = "clinic.test"


@pytest.fixture
def world():
    issuer, agent = KeyPair.generate(), KeyPair.generate()
    store = TrustStore()
    store.add("idp.test", issuer)
    return issuer, agent, IdentityVerifier(store, AUD)


def passport(issuer, agent, **kw):
    args = dict(issuer="idp.test", agent_key=agent, agent_id="agent:a", agent_name="A",
                principal=Principal("P-1", "Pat"), audience=AUD, scopes=["appointments:read"])
    args.update(kw)
    return issue_passport(issuer, **args)


def code(fn):
    with pytest.raises(IdentityError) as e:
        fn()
    return e.value.code


def test_valid_passport_and_proof(world):
    issuer, agent, v = world
    va = v.verify_passport(passport(issuer, agent))
    assert va.principal.id == "P-1" and va.has_scope("appointments:read") and not va.has_scope("records:read")
    body = {"envelope": {"intent": "x", "params": {"n": 800}}, "text": ""}
    v.verify_proof(va, sign_request(agent, audience=AUD, body=body), body)


def test_wildcard_scope(world):
    issuer, agent, v = world
    assert v.verify_passport(passport(issuer, agent, scopes=["appointments:*"])).has_scope("appointments:write")


def test_rejections(world):
    issuer, agent, v = world
    assert code(lambda: v.verify_passport(None)) == "missing_passport"
    assert code(lambda: v.verify_passport(passport(KeyPair.generate(), agent))) == "untrusted_issuer"
    assert code(lambda: v.verify_passport(passport(issuer, agent, audience="other"))) == "wrong_audience"
    assert code(lambda: v.verify_passport(passport(issuer, agent, ai_disclosed=False))) == "no_ai_disclosure"
    old = passport(issuer, agent, now=int(time.time()) - 7200, ttl_seconds=60)
    assert code(lambda: v.verify_passport(old)) == "expired_passport"
    assert code(lambda: v.verify_passport("not.a.token")) == "malformed_token"
    forged = passport(issuer, agent)
    h, p, s = forged.split(".")
    assert code(lambda: v.verify_passport(f"{h}.{p}.{s[:-4]}AAAA")) == "bad_signature"


def test_revocation(world):
    issuer, agent, v = world
    tok = passport(issuer, agent)
    v.revoked_passports.add(v.verify_passport(tok).passport_id)
    assert code(lambda: v.verify_passport(tok)) == "revoked_passport"


def test_proof_replay_tamper_and_key_binding(world):
    issuer, agent, v = world
    va = v.verify_passport(passport(issuer, agent))
    body = {"envelope": {"intent": "x"}, "text": ""}
    proof = sign_request(agent, audience=AUD, body=body)
    v.verify_proof(va, proof, body)
    assert code(lambda: v.verify_proof(va, proof, body)) == "replay"
    proof2 = sign_request(agent, audience=AUD, body=body)
    assert code(lambda: v.verify_proof(va, proof2, {"envelope": {"intent": "y"}, "text": ""})) == "body_tampered"
    thief = sign_request(KeyPair.generate(), audience=AUD, body=body)
    assert code(lambda: v.verify_proof(va, thief, body)) == "key_mismatch"
    stale = sign_request(agent, audience=AUD, body=body, now=int(time.time()) - 600)
    assert code(lambda: v.verify_proof(va, stale, body)) == "stale_proof"
    assert code(lambda: v.verify_proof(va, None, body)) == "missing_proof"


def test_body_hash_survives_protobuf_number_widening():
    assert body_hash({"amount": 800}) == body_hash({"amount": 800.0})
    assert body_hash({"amount": 800.5}) != body_hash({"amount": 800})
