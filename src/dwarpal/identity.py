"""Identity and delegation for AI agents.

Two signed artifacts, both compact JWS (EdDSA / Ed25519):

* **Agent Passport** — issued by an identity provider the business trusts (a patient-identity
  wallet, a bank, an enterprise IdP). It says: *this agent* (sub) acts for *this principal*,
  towards *this business* (aud), with *these scopes and limits*, until *exp*, and that it
  discloses itself as an AI. It also binds the agent's own public key (``cnf``), so a stolen
  passport is useless on its own.

* **Request Proof** — signed by the agent's key for every single request. It covers a nonce,
  a timestamp, the audience and a hash of the exact request body. This is proof-of-possession
  (same idea as OAuth DPoP): captured requests cannot be replayed or edited in flight.

In production the passport would map onto OAuth 2.0 token exchange / verifiable credentials,
and the trust store onto your IdP's JWKS. The verification logic stays the same.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PASSPORT_TYP = "agent-passport+jwt"
PROOF_TYP = "agent-proof+jwt"


# --------------------------------------------------------------------------- encoding helpers


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _normalise_numbers(value: Any) -> Any:
    """A2A carries data parts as protobuf Struct, which turns every int into a float.
    Normalise integral floats so client and gateway hash the same body."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _normalise_numbers(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise_numbers(v) for v in value]
    return value


def body_hash(body: Any) -> str:
    return b64u(hashlib.sha256(canonical_json(_normalise_numbers(body))).digest())


# --------------------------------------------------------------------------- keys


class KeyPair:
    """An Ed25519 key pair with a stable key id."""

    def __init__(self, private_key: Ed25519PrivateKey):
        self.private_key = private_key
        self.public_key = private_key.public_key()

    @classmethod
    def generate(cls) -> "KeyPair":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, path: Path) -> "KeyPair":
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            assert isinstance(key, Ed25519PrivateKey)
            return cls(key)
        kp = cls.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            kp.private_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return kp

    @property
    def public_raw(self) -> bytes:
        return self.public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    @property
    def public_b64(self) -> str:
        return b64u(self.public_raw)

    @property
    def kid(self) -> str:
        return hashlib.sha256(self.public_raw).hexdigest()[:16]

    def sign(self, data: bytes) -> bytes:
        return self.private_key.sign(data)


def public_key_from_b64(data: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(b64u_decode(data))


# --------------------------------------------------------------------------- compact JWS


def jws_sign(key: KeyPair, payload: dict[str, Any], typ: str) -> str:
    header = {"alg": "EdDSA", "typ": typ, "kid": key.kid}
    signing_input = f"{b64u(canonical_json(header))}.{b64u(canonical_json(payload))}"
    return f"{signing_input}.{b64u(key.sign(signing_input.encode()))}"


def jws_parse(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    parts = token.split(".")
    if len(parts) != 3:
        raise IdentityError("malformed_token", "Token is not a compact JWS.")
    try:
        header = json.loads(b64u_decode(parts[0]))
        payload = json.loads(b64u_decode(parts[1]))
        signature = b64u_decode(parts[2])
    except Exception as exc:  # noqa: BLE001
        raise IdentityError("malformed_token", f"Token could not be decoded: {exc}") from exc
    return header, payload, signature, f"{parts[0]}.{parts[1]}".encode()


def jws_verify(public_key: Ed25519PublicKey, signature: bytes, signing_input: bytes) -> None:
    try:
        public_key.verify(signature, signing_input)
    except InvalidSignature as exc:
        raise IdentityError("bad_signature", "Signature does not verify.") from exc


# --------------------------------------------------------------------------- errors / results


class IdentityError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Principal:
    id: str
    name: str
    type: str = "person"


@dataclass
class VerifiedAgent:
    agent_id: str
    agent_name: str
    issuer: str
    principal: Principal
    scopes: list[str]
    limits: dict[str, Any]
    ai_disclosed: bool
    passport_id: str
    expires_at: int
    claims: dict[str, Any] = field(repr=False, default_factory=dict)

    def has_scope(self, scope: str) -> bool:
        if scope in self.scopes:
            return True
        # "appointments:*" grants "appointments:read"
        prefix = scope.split(":", 1)[0]
        return f"{prefix}:*" in self.scopes


# --------------------------------------------------------------------------- issuing


def issue_passport(
    issuer_key: KeyPair,
    *,
    issuer: str,
    agent_key: KeyPair,
    agent_id: str,
    agent_name: str,
    principal: Principal,
    audience: str,
    scopes: list[str],
    limits: dict[str, Any] | None = None,
    ttl_seconds: int = 3600,
    ai_disclosed: bool = True,
    now: int | None = None,
) -> str:
    now = int(now if now is not None else time.time())
    claims = {
        "iss": issuer,
        "sub": agent_id,
        "agent_name": agent_name,
        "aud": audience,
        "principal": {"id": principal.id, "name": principal.name, "type": principal.type},
        "scopes": scopes,
        "limits": limits or {},
        "ai_disclosed": ai_disclosed,
        "cnf": {"kid": agent_key.kid, "pub": agent_key.public_b64},
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": str(uuid.uuid4()),
    }
    return jws_sign(issuer_key, claims, PASSPORT_TYP)


def sign_request(agent_key: KeyPair, *, audience: str, body: Any, now: int | None = None) -> str:
    claims = {
        "aud": audience,
        "iat": int(now if now is not None else time.time()),
        "nonce": secrets.token_urlsafe(16),
        "bh": body_hash(body),
    }
    return jws_sign(agent_key, claims, PROOF_TYP)


# --------------------------------------------------------------------------- verifying


@dataclass
class TrustedIssuer:
    issuer: str
    public_key: Ed25519PublicKey
    kid: str
    label: str = ""


class TrustStore:
    """Identity providers whose passports this business accepts."""

    def __init__(self) -> None:
        self._by_kid: dict[str, TrustedIssuer] = {}

    def add(self, issuer: str, key: KeyPair | Ed25519PublicKey, label: str = "") -> None:
        if isinstance(key, KeyPair):
            pub, kid = key.public_key, key.kid
        else:
            raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            pub, kid = key, hashlib.sha256(raw).hexdigest()[:16]
        self._by_kid[kid] = TrustedIssuer(issuer=issuer, public_key=pub, kid=kid, label=label)

    def get(self, kid: str) -> TrustedIssuer | None:
        return self._by_kid.get(kid)

    def issuers(self) -> list[TrustedIssuer]:
        return list(self._by_kid.values())


class NonceCache:
    """Remembers seen request nonces for the proof window (replay protection)."""

    def __init__(self, window_seconds: int = 120):
        self.window = window_seconds
        self._seen: dict[str, float] = {}

    def check_and_add(self, nonce: str, now: float) -> bool:
        cutoff = now - self.window
        if len(self._seen) > 10_000:
            self._seen = {n: t for n, t in self._seen.items() if t > cutoff}
        if nonce in self._seen and self._seen[nonce] > cutoff:
            return False
        self._seen[nonce] = now
        return True


class IdentityVerifier:
    """Verifies passport + per-request proof. Raises IdentityError with a stable code."""

    def __init__(
        self,
        trust_store: TrustStore,
        audience: str,
        *,
        proof_max_age_seconds: int = 60,
        clock_skew_seconds: int = 30,
        require_ai_disclosure: bool = True,
    ):
        self.trust_store = trust_store
        self.audience = audience
        self.proof_max_age = proof_max_age_seconds
        self.skew = clock_skew_seconds
        self.require_ai_disclosure = require_ai_disclosure
        self.nonces = NonceCache(window_seconds=proof_max_age_seconds + clock_skew_seconds)
        self.revoked_passports: set[str] = set()

    def verify_passport(self, token: str | None, now: float | None = None) -> VerifiedAgent:
        now = time.time() if now is None else now
        if not token:
            raise IdentityError("missing_passport", "No agent passport presented. Anonymous agents are not served.")
        header, claims, sig, signing_input = jws_parse(token)
        if header.get("typ") != PASSPORT_TYP or header.get("alg") != "EdDSA":
            raise IdentityError("wrong_token_type", "Not an agent passport.")
        issuer = self.trust_store.get(header.get("kid", ""))
        if issuer is None:
            raise IdentityError("untrusted_issuer", f"Passport issuer key {header.get('kid')!r} is not trusted.")
        jws_verify(issuer.public_key, sig, signing_input)
        if claims.get("iss") != issuer.issuer:
            raise IdentityError("issuer_mismatch", "Passport 'iss' does not match the signing key's issuer.")
        if claims.get("aud") != self.audience:
            raise IdentityError("wrong_audience", f"Passport is for {claims.get('aud')!r}, not {self.audience!r}.")
        if claims.get("exp", 0) < now - self.skew:
            raise IdentityError("expired_passport", "Passport has expired.")
        if claims.get("iat", 0) > now + self.skew:
            raise IdentityError("not_yet_valid", "Passport issued in the future.")
        if claims.get("jti") in self.revoked_passports:
            raise IdentityError("revoked_passport", "Passport has been revoked.")
        if self.require_ai_disclosure and claims.get("ai_disclosed") is not True:
            raise IdentityError("no_ai_disclosure", "Caller does not disclose that it is an AI agent.")
        if not (claims.get("cnf") or {}).get("pub"):
            raise IdentityError("no_key_binding", "Passport is not bound to an agent key.")
        p = claims.get("principal") or {}
        return VerifiedAgent(
            agent_id=claims["sub"],
            agent_name=claims.get("agent_name", claims["sub"]),
            issuer=claims["iss"],
            principal=Principal(id=p.get("id", "?"), name=p.get("name", "?"), type=p.get("type", "person")),
            scopes=list(claims.get("scopes") or []),
            limits=dict(claims.get("limits") or {}),
            ai_disclosed=bool(claims.get("ai_disclosed")),
            passport_id=claims.get("jti", ""),
            expires_at=int(claims.get("exp", 0)),
            claims=claims,
        )

    def verify_proof(self, agent: VerifiedAgent, proof: str | None, body: Any, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if not proof:
            raise IdentityError("missing_proof", "Request is not signed by the agent key.")
        header, claims, sig, signing_input = jws_parse(proof)
        if header.get("typ") != PROOF_TYP:
            raise IdentityError("wrong_token_type", "Not a request proof.")
        cnf = agent.claims.get("cnf") or {}
        if header.get("kid") != cnf.get("kid"):
            raise IdentityError("key_mismatch", "Request was signed by a key the passport does not bind.")
        jws_verify(public_key_from_b64(cnf["pub"]), sig, signing_input)
        if claims.get("aud") != self.audience:
            raise IdentityError("wrong_audience", "Request proof is for a different audience.")
        age = now - claims.get("iat", 0)
        if age > self.proof_max_age or age < -self.skew:
            raise IdentityError("stale_proof", f"Request proof is {int(age)}s old.")
        if claims.get("bh") != body_hash(body):
            raise IdentityError("body_tampered", "Request body does not match what the agent signed.")
        if not self.nonces.check_and_add(claims.get("nonce", ""), now):
            raise IdentityError("replay", "This exact signed request was already used (replay).")
